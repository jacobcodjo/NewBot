"""Bot Candle Range Trading (CRT) - stratégie et symboles de l'ancien crt-bot.
Références W1 / D1 / H4 : sweep de liquidité sur un TF de confirmation proportionnel,
cassure de structure + FVG et/ou Order Block. 11 actifs (forex majeurs, or, cryptos).
Données : API websocket Deriv | Alertes : Telegram
"""
import json, os, random, time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests
import websocket  # websocket-client
import tracker


def _env_flag(name, default):
    """Variable d'environnement booléenne (1/true/oui) ; `default` si absente ou vide."""
    v = os.getenv(name)
    return default if v is None or v.strip() == "" else v.strip().lower() in ("1", "true", "oui", "yes", "vrai")


def _env_float(name, default):
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


APP_ID = os.getenv("DERIV_APP_ID", "1089")
# 1) nouvel endpoint public Deriv (sans authentification), 2) ancien endpoint en secours.
# On peut forcer un endpoint avec la variable d'environnement DERIV_WS_URL.
WS_URLS = [u for u in (
    os.getenv("DERIV_WS_URL"),
    "wss://api.derivws.com/trading/v1/options/ws/public",
    f"wss://ws.derivws.com/websockets/v3?app_id={APP_ID}",
) if u]
UA = "Mozilla/5.0 (compatible; crt-bot/1.0)"
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")

SYMBOLS = [
    # Forex majeurs uniquement (les paires croisées sans USD ont été retirées)
    "frxEURUSD", "frxGBPUSD", "frxUSDJPY", "frxUSDCHF", "frxAUDUSD", "frxUSDCAD",
    # Or
    "frxXAUUSD",
    # Cryptos
    "cryBTCUSD", "cryETHUSD", "cryLTCUSD", "cryXRPUSD",
]   # SYMBOLS : actifs du CRT = forex majeurs, or et cryptos (les indices synthétiques relèvent de l'Impulsion)

# Indices synthétiques : scannés par impulse.py (stratégie Impulsion), plus par le CRT.
SYNTHETIC_SYMBOLS = [
    # Volatility Index classiques (10/25/50/75/100) et variantes 1 seconde
    "R_10", "R_25", "R_50", "R_75", "R_100",
    "1HZ10V", "1HZ15V", "1HZ25V", "1HZ30V", "1HZ50V", "1HZ75V",
    "1HZ90V", "1HZ100V", "1HZ150V", "1HZ200V", "1HZ250V", "1HZ300V",
    # Step Index
    "stpRNG",
]
UNIVERSE = SYMBOLS + SYNTHETIC_SYMBOLS   # tous les actifs suivis (récapitulatif hebdomadaire, étude de prédictibilité)
GRAN = {"D1": 86400, "H4": 14400, "H1": 3600, "M15": 900, "M5": 300}
# Cascades (HTF, MTF, LTF) : utilisées uniquement par impulse.py. Le CRT n'en dépend plus.
CASCADES = [("D1", "H4", "H1"), ("H4", "H1", "M15"), ("H1", "M15", "M5")]

# --- Stratégie CRT (reprise de l'ancien crt-bot) ---
# Référence -> TF de confirmation proportionnel. Retirer une ligne désactive cette référence.
# W1 est dérivé des bougies D1 (pas de granularité hebdomadaire chez Deriv).
REFERENCE_CONFIRMATION_MAP = {"W1": ["H4"], "D1": ["H1"], "H4": ["M15"]}
# Fenêtre des swings pour la cassure de structure, par TF de confirmation
STRUCTURE_SWING_WINDOW = {"M15": 1}
DEFAULT_STRUCTURE_SWING_WINDOW = 2
# Tendance de fond (D1 pour le CRT, HTF pour l'Impulsion, D1 et W1 pour le récapitulatif)
TREND_MODES = ("structure", "deplacement", "swings")
TREND_MODE = os.getenv("TREND_MODE", "structure")   # structure (précoce) | deplacement (structure + cassure impulsive)
if TREND_MODE not in TREND_MODES:                   # | swings (ancienne règle, 3 swings + SMA 50, tardive)
    TREND_MODE = "structure"
TREND_DISP_BODY_MULT = 1.5         # mode deplacement : corps de la bougie de cassure >= 1,5 x corps moyen des 10 bougies précédentes
TREND_DISP_ATR_FRAC = 0.25         # ... et clôture au-delà du pivot d'au moins 0,25 ATR (14)
TREND_DISP_REQUIRE_BOTH = True     # True : les deux conditions (corps ET distance) ; False : l'une des deux (filtre très léger)
TREND_DISP_CONFIRM = 1             # la cassure est validée si la bougie de cassure ou la suivante est impulsive
TREND_MATURE_SWINGS = 3            # alignements successifs (sommets ET creux) à partir desquels la tendance est « mûre »
TREND_STRUCTURE_WINDOW = 3         # mode structure : un swing est confirmé 3 bougies après (4 pour l'ancien mode)
TREND_SWING_WINDOW = 4             # mode swings : fenêtre des swings
TREND_SWING_COUNT = 3              # mode swings : 3 sommets ET 3 creux consécutifs dans le même sens
TREND_SMA_PERIOD = 50              # mode swings : confirmée par la position du prix vs SMA 50
WEEKEND_GAP_PREFIXES = ("frx",)    # forex + or : bougie après un gap de weekend = pas un sweep
WEEKEND_GAP_MULTIPLIER = 1.5
LIQUIDITY_POOL_SWING_WINDOW = 3    # pools de liquidité (equal highs / equal lows)
LIQUIDITY_POOL_TOLERANCE_PCT = 0.1 # écart (% du prix) pour considérer deux niveaux comme égaux
STOP_LOSS_POOL_BUFFER_PCT = 0.1    # marge SL supplémentaire si le SL tombe sur un pool (% du prix)

NY = ZoneInfo("America/New_York")
NY_ALIGNED = _env_flag("NY_ALIGNED", False)   # True : D1/H4 recalculés avec ouverture 17h New York (forex + or)
H1_LONG_COUNT = 1500               # bougies H1 lues pour reconstruire D1/H4 en mode NY_ALIGNED (~60 jours)
KILLZONES_NY = [                   # (nom, h début, min début, h fin, min fin) en heure de New York
    ("Asie", 20, 0, 23, 59),
    ("Londres", 2, 0, 5, 0),
    ("New York", 7, 0, 10, 0),
    ("London Close", 10, 0, 12, 0),
]
REQUIRE_KILLZONE_FOR_REAL_MARKETS = _env_flag("CRT_REQUIRE_KILLZONE", False)  # True : sweep hors killzone ignoré (forex/or seulement)
KILLZONE_24_7 = False              # True : filtre aussi cryptos et indices synthétiques
SWING_N = 2                        # fractale utilisée par swings() (impulse.py)
COUNT = 200

# --- Entrées / sorties ---
STOP_LOSS_BUFFER_PCT = 0.05        # marge du SL au-delà de l'extrême du sweep (% du prix)
TP_EXTENSION_PCT = 50               # TP3 : bord opposé du range + 50 % de sa taille (tous les actifs)
MIN_RISK_REWARD = 3.0              # ratio minimum (1 pour 3), calculé sur TP2
ORDER_TYPE_TOLERANCE_PCT = 0.02    # écart (% du prix) sous lequel le prix est "déjà sur la zone" -> ordre au marché
OB_LOOKBACK = 8                    # l'OB doit se trouver à 8 bougies LTF au plus avant la cassure
REQUIRE_FIB_OTE = False            # True : l'entrée doit tomber dans l'OTE 61,8-79 % (sinon simple indication)

# --- Validation de C2 (la bougie de manipulation doit finir dans le range de C1) ---
REQUIRE_C2_CLOSE_INSIDE = True     # prix actuel dans le range ET aucune clôture LTF hors range après le sweep (sinon : vraie cassure)

# --- Sweep : profondeur, fraîcheur, un seul côté ---
SWEEP_MAX_RANGE_FRAC = 0.5         # profondeur maximale de la mèche de sweep (part du range de C1) ; au-delà : cassure, pas manipulation
SWEEP_MAX_ATR = 3.0                # ... et en ATR du timeframe de confirmation
SWEEP_MAX_AGE = {"H4": 24, "H1": 16, "M15": 16}   # ancienneté maximale du sweep, en bougies de confirmation
SKIP_BOTH_SIDES_SWEPT = True       # C2 doit balayer un seul côté : si les deux côtés sont balayés (outside bar), pas de CRT

# --- Qualité de C1 (taille du range par rapport à la volatilité du timeframe de référence) ---
RANGE_MIN_ATR = 0.6                # range inférieur à 0,6 ATR : bruit
RANGE_MAX_ATR = 2.5                # range supérieur à 2,5 ATR : SL large et TP2 irréaliste

# --- Mitigation, expiration, zone d'intérêt HTF ---
SKIP_MITIGATED = True              # ignore un setup dont l'entrée a déjà été retestée avant l'alerte
EXPIRY_REF_CANDLES = 3             # ordre valable jusqu'à la fin de C3 (3 bougies de référence depuis l'ouverture de C1)
REF_GRAN = {"W1": 7 * 86400, "D1": 86400, "H4": 4 * 3600}
HTF_FVG_LOOKBACK = 30              # FVG non comblés cherchés sur les 30 dernières bougies de la référence (D1 pour le W1)
HTF_FVG_TOL_ATR = 0.25             # tolérance autour de la zone (x ATR de la référence)
REQUIRE_HTF_POI = _env_flag("CRT_REQUIRE_HTF_POI", False)   # True : le sweep doit se faire dans un FVG HTF (sinon : indication et priorité)

# --- Mode CRT « strict » : C2 clôturée, entrée sur C2 pendant C3 ---
# loose  : mode historique (sweep détecté en direct sur le TF de confirmation, entrée sur OB/FVG LTF)
# strict : C1 = avant-dernière bougie de référence clôturée, C2 = dernière clôturée (a balayé un côté et clôturé dans le
#          range de C1), C3 = bougie en formation. Entrée selon STRICT_ENTRY :
#          ote         ordre limite au niveau STRICT_OTE_LEVEL du retracement de C2 (zone 62-79 %)
#          mid         ordre limite à 50 % de C2
#          ote_confirm zone OTE de C2 touchée, puis cassure de structure impulsive + OB/FVG sur le TF de confirmation
STRICT_ENTRIES = ("ote", "mid", "ote_confirm")


def _env_choice(name, default, allowed):
    v = (os.getenv(name) or "").strip().lower()
    return v if v in allowed else default


CRT_MODE = _env_choice("CRT_MODE", "loose", ("loose", "strict"))
STRICT_ENTRY = _env_choice("CRT_STRICT_ENTRY", "ote", STRICT_ENTRIES)
STRICT_OTE_ZONE = (0.62, 0.79)     # zone OTE du retracement de C2 (part du range de C2, mesurée depuis l'extrême opposé au sweep)
STRICT_OTE_LEVEL = 0.705           # niveau d'entrée de l'ordre limite dans la zone OTE
STRICT_MID_LEVEL = 0.5
STRICT_ENTRY_INSIDE_RANGE = True   # l'entrée doit rester dans le range de C1 (sinon : on entrerait au-delà du niveau balayé)
STRICT_SL_BUFFER_ATR = 0.05        # marge du SL au-delà de l'extrême de C2 : max(STOP_LOSS_BUFFER_PCT, 0,05 ATR de la référence) + spread

# --- Qualité de la confirmation ---
MSS_REQUIRE_DISPLACEMENT = True    # la bougie qui casse la structure doit être impulsive (corps >= MSS_BODY_MULT x corps moyen)
MSS_BODY_MULT = 1.2
FVG_MIN_ATR_FRAC = 0.1             # un FVG doit mesurer au moins 0,1 ATR du TF de confirmation
FVG_DISP_BODY_MULT = 1.0           # ... et sa bougie centrale doit être dirigée, avec un corps >= 1 x corps moyen
REQUIRE_FVG = _env_flag("CRT_REQUIRE_FVG", False)   # True : un FVG créé par le déplacement est obligatoire (l'OB seul ne suffit plus)

# --- Biais et zone (premium / discount) ---
BLOCK_COUNTER_TREND = _env_flag("CRT_BLOCK_COUNTER_TREND", False)  # True : un setup à contre-tendance D1 est ignoré (sinon : avertissement)
REQUIRE_PREMIUM_DISCOUNT = True    # achat : entrée sous le milieu du range (discount) ; vente : au-dessus (premium)

# --- Stop-loss adaptatif et coûts ---
STOP_LOSS_BUFFER_ATR = 0.15        # marge du SL : max(STOP_LOSS_BUFFER_PCT, 0,15 x ATR du TF de confirmation) + spread
SPREAD_PCT = {"frxXAU": 0.015, "frx": 0.003, "cry": 0.04}   # spread estimé (% du prix), préfixe le plus long retenu
SLIPPAGE_PCT = 0.005               # glissement estimé (% du prix), utilisé par le backtest

# --- News (forex et or) ---
NEWS_FILTER = _env_flag("CRT_NEWS_FILTER", True)
NEWS_BLOCK_BEFORE_MIN = 30         # pas d'alerte dans les 30 min avant ...
NEWS_BLOCK_AFTER_MIN = 30          # ... et après l'annonce
NEWS_INCLUDE_CRYPTO = True         # les annonces USD concernent aussi les cryptos
NEWS_WARN_MIN = 60                 # avertit d'une annonce dans l'heure quand un trade est ouvert
ROLLOVER_FILTER = True             # pas d'alerte forex/or pendant le rollover (spreads élargis)
ROLLOVER_NY = (16 * 60 + 45, 17 * 60 + 30)   # fenêtre en minutes depuis minuit, heure de New York
NEWS_FILE = "news.json"            # facultatif : [{"time": "2026-11-04T19:00:00Z", "currencies": ["USD"]}, ...] (FOMC, CPI, BCE...)
# Le NFP (premier vendredi du mois, 8h30 New York) est ajouté automatiquement.

# --- Risque ---
ACCOUNT_BALANCE = _env_float("ACCOUNT_BALANCE", 0.0)   # capital (variable de dépôt) : active la ligne « Taille » des alertes
RISK_PCT = _env_float("RISK_PCT", 1.0)                 # risque par trade (% du capital)
MAX_DAILY_LOSS_R = 3.0             # alertes suspendues si les trades résolus du jour (R géré) cumulent -3 R ou moins
MAX_WEEKLY_LOSS_R = 6.0            # ... ou -6 R sur la semaine
MAX_OPEN_TRADES = 6                # plafond global de trades suivis (en attente ou ouverts)
MAX_CORRELATED_OPEN = 2            # trades déjà ouverts/en attente sur le même côté d'un groupe corrélé (USD, cryptos)
CORRELATION_MODE = os.getenv("CRT_CORRELATION_MODE", "flag")   # flag : avertissement | block : alerte retenue tant que l'exposition est pleine
MANAGEMENT_NOTE = True             # ajoute la règle de gestion après TP1 (moitié à TP1, SL au point d'entrée)

MIN_INTERVAL = 0.6      # s entre deux requêtes (throttle)
MAX_RETRIES = 6
STATE_MAX_AGE_DAYS = 30  # purge des verrous de range (W1 = 7 jours, marge incluse)
CACHE_FILE = "cache.json"
STATE_FILE = "state.json"


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


# --------------------------------------------------------------------------
# Client Deriv robuste : throttle + backoff exponentiel + reconnexion auto
# --------------------------------------------------------------------------
class Deriv:
    def __init__(self):
        self.ws = None
        self.last = 0.0
        self.req_id = 0

    def _connect(self):
        self.close()
        for i in range(MAX_RETRIES):
            url = WS_URLS[i % len(WS_URLS)]
            name = url.split("?")[0]
            try:
                self.ws = websocket.create_connection(
                    url, timeout=20, header=[f"User-Agent: {UA}"])
                print(f"Connecté à {name}")
                return
            except Exception as e:
                self._sleep_backoff(i, f"connexion {name}: {str(e)[:60]}")
        raise RuntimeError("Impossible de se connecter à Deriv")

    def close(self):
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        self.ws = None

    @staticmethod
    def _sleep_backoff(attempt, why):
        delay = min(60, 2 ** attempt) + random.uniform(0, 1)  # jitter
        print(f"[retry {attempt+1}] {why} -> pause {delay:.1f}s")
        time.sleep(delay)

    def request(self, payload):
        for attempt in range(MAX_RETRIES):
            try:
                if self.ws is None:
                    self._connect()
                wait = MIN_INTERVAL - (time.time() - self.last)
                if wait > 0:
                    time.sleep(wait)
                self.req_id += 1
                payload["req_id"] = self.req_id
                self.ws.send(json.dumps(payload))
                self.last = time.time()
                while True:  # ignore les messages sans rapport
                    msg = json.loads(self.ws.recv())
                    if msg.get("req_id") == self.req_id:
                        break
                err = msg.get("error")
                if err:
                    code = err.get("code")
                    if code in ("RateLimit", "RateLimitExceeded"):
                        self._sleep_backoff(attempt + 2, "rate limit")
                        continue
                    if code in ("InvalidSymbol", "MarketIsClosed"):
                        print(f"Ignoré ({code}): {payload.get('ticks_history')}")
                        return None  # marché fermé / symbole indisponible
                    raise RuntimeError(f"{code}: {err.get('message')}")
                return msg
            except (websocket.WebSocketException, ConnectionError, OSError) as e:
                self.close()  # forcera une reconnexion propre
                self._sleep_backoff(attempt, f"socket: {e}")
        return None

    def candles(self, symbol, gran, count=COUNT, end="latest"):
        msg = self.request({
            "ticks_history": symbol, "style": "candles", "granularity": gran,
            "count": count, "end": end,
        })
        return msg.get("candles") if msg else None


def get_candles(api, cache, symbol, gran, force=False, count=COUNT):
    """Cache : on ne re-télécharge que si la dernière bougie est clôturée."""
    key = f"{symbol}_{gran}" + ("" if count == COUNT else f"_{count}")
    c = cache.get(key)
    if c and not force and time.time() < c[-1]["epoch"] + gran:
        return c
    fresh = api.candles(symbol, gran, count=count)
    if fresh:
        cache[key] = fresh
        return fresh
    return c


# --------------------------------------------------------------------------
# Alignement New York (ICT) : D1/H4 ouvrent à 17h NY, reconstruits depuis le H1
# --------------------------------------------------------------------------
def is_247(sym):
    """Cryptos et indices synthétiques : marchés 24/7, bougies natives Deriv."""
    return sym.startswith(("cry", "R_", "1HZ", "stp"))


def is_synthetic(sym):
    return sym.startswith(("R_", "1HZ", "stp"))


def display_name(sym):
    if sym == "stpRNG":
        return "Step Index"
    if sym.startswith("1HZ"):
        return f"Volatility {sym[3:-1]} 1s"
    if sym.startswith("R_"):
        return f"Volatility {sym[2:]}"
    return sym[3:]  # retire frx / cry


def aggregate_ny(h1, hours):
    out = {}
    for c in h1:
        dt = datetime.fromtimestamp(c["epoch"], NY)
        h = (dt.hour - 17) % 24
        start = int((dt.replace(minute=0, second=0, microsecond=0)
                     - timedelta(hours=h % hours)).timestamp())
        b = out.get(start)
        if b is None:
            out[start] = {"epoch": start, "open": c["open"], "high": c["high"],
                          "low": c["low"], "close": c["close"]}
        else:
            b["high"] = max(b["high"], c["high"])
            b["low"] = min(b["low"], c["low"])
            b["close"] = c["close"]
    return list(out.values())


def tf_candles(api, cache, sym, tf, force=False):
    if NY_ALIGNED and not is_247(sym) and tf in ("D1", "H4"):
        h1 = get_candles(api, cache, sym, GRAN["H1"], count=H1_LONG_COUNT)   # 200 H1 = 8 jours : trop court pour D1/W1/tendance
        return aggregate_ny(h1, 24 if tf == "D1" else 4) if h1 else None
    return get_candles(api, cache, sym, GRAN[tf], force=force)


def killzone(sym, epoch):
    """Nom de la session si dans une killzone, '24/7' pour un marché sans filtre, sinon None."""
    if is_247(sym) and not KILLZONE_24_7:
        return "24/7"
    dt = datetime.fromtimestamp(epoch, NY)
    mod = dt.hour * 60 + dt.minute
    for name, sh, sm, eh, em in KILLZONES_NY:
        if sh * 60 + sm <= mod <= eh * 60 + em:
            return name
    return None


# --------------------------------------------------------------------------
# Logique CRT en cascade
# --------------------------------------------------------------------------
def body_avg(cs):
    cs = [abs(c["close"] - c["open"]) for c in cs]
    return sum(cs) / len(cs) if cs else 0


def swings(m, kind, upto):
    """Indices des swings (fractales) confirmés, index <= upto - SWING_N."""
    out = []
    for s in range(SWING_N, upto - SWING_N + 1):
        win = [x[kind] for x in m[s - SWING_N:s + SWING_N + 1]]
        if kind == "high" and m[s]["high"] == max(win) and win.count(m[s]["high"]) == 1:
            out.append(s)
        if kind == "low" and m[s]["low"] == min(win) and win.count(m[s]["low"]) == 1:
            out.append(s)
    return out


# --------------------------------------------------------------------------
# Détection CRT (stratégie de l'ancien crt-bot)
# --------------------------------------------------------------------------
def week_key(epoch, ny=False):
    """Semaine ISO d'une bougie D1. En mode NY, la bougie ouvre à 17h New York : le dimanche 17h appartient
    déjà à la nouvelle semaine (décalage de 7 h pour la ramener à minuit)."""
    if ny:
        return (datetime.fromtimestamp(epoch, NY) + timedelta(hours=7)).isocalendar()[:2]
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isocalendar()[:2]


def complete_weeks(d1, ny=False):
    """Semaines ISO complètes (la semaine en cours est exclue), de la plus ancienne à la plus récente."""
    if not d1 or len(d1) < 2:
        return []
    weeks = {}
    for c in d1[:-1]:  # exclut la bougie du jour en cours
        weeks.setdefault(week_key(c["epoch"], ny), []).append(c)
    return [weeks[k] for k in sorted(weeks)[:-1]]  # la dernière clé est la semaine en cours (incomplète)


def weekly_range(d1, ny=False):
    """Range W1 : haut/bas de la dernière semaine ISO complète, agrégé depuis les bougies D1."""
    wks = complete_weeks(d1, ny)
    if not wks:
        return None
    wk = wks[-1]
    return {"high": max(c["high"] for c in wk), "low": min(c["low"] for c in wk),
            "epoch": wk[-1]["epoch"]}


def ref_atr(ref_n, htf, ny=False):
    """Volatilité typique d'une bougie de référence : ATR 14 (D1, H4) ou taille moyenne des 8 dernières semaines (W1)."""
    if ref_n == "W1":
        wks = complete_weeks(htf.get("D1"), ny)[-8:]
        sizes = [max(c["high"] for c in w) - min(c["low"] for c in w) for w in wks]
        return sum(sizes) / len(sizes) if sizes else 0.0
    cs = htf.get(ref_n)
    return _atr_before(cs, len(cs) - 1) if cs and len(cs) > 3 else 0.0


def htf_poi(ref_n, htf, d, ext, atr):
    """Le sweep (extrême ext) se fait-il dans un FVG HTF non comblé ? Achat : FVG haussier (demande) ; vente : baissier.
    FVG cherchés sur la référence (D1 pour le W1), avant la bougie en formation."""
    cs = htf.get("D1" if ref_n == "W1" else ref_n)
    if not cs or len(cs) < 6:
        return None
    closed, tol, up = cs[:-1], HTF_FVG_TOL_ATR * atr, d == "BUY"
    for i in range(len(closed) - 2, max(0, len(closed) - HTF_FVG_LOOKBACK) - 1, -1):
        c1, c3 = closed[i - 1], closed[i + 1]
        if up and c1["high"] < c3["low"]:
            bottom, top = c1["high"], c3["low"]
        elif not up and c1["low"] > c3["high"]:
            bottom, top = c3["high"], c1["low"]
        else:
            continue
        later = closed[i + 2:]
        if any((c["close"] < bottom) if up else (c["close"] > top) for c in later):
            continue  # comblé : une clôture a traversé toute la zone
        if bottom - tol <= ext <= top + tol:
            return {"bottom": bottom, "top": top}
    return None


def ref_range(ref_n, htf, ny=False):
    """Range de référence : W1 agrégé, ou dernière bougie D1/H4 clôturée."""
    if ref_n == "W1":
        return weekly_range(htf.get("D1"), ny)
    cs = htf.get(ref_n)
    if not cs or len(cs) < 2:
        return None
    r = cs[-2]
    return {"high": r["high"], "low": r["low"], "epoch": r["epoch"]}


def find_pivots(cs, left, right):
    """Swing points (indices des sommets, indices des creux)."""
    highs, lows = [], []
    for i in range(left, len(cs) - right):
        win = cs[i - left:i + right + 1]
        if cs[i]["high"] == max(c["high"] for c in win):
            highs.append(i)
        if cs[i]["low"] == min(c["low"] for c in win):
            lows.append(i)
    return highs, lows


def group_equal_levels(values):
    """Regroupe les niveaux quasi égaux ; ne garde que les groupes d'au moins 2 niveaux."""
    if not values:
        return []
    tol = LIQUIDITY_POOL_TOLERANCE_PCT / 100
    vals = sorted(values)
    groups, cur = [], [vals[0]]
    for v in vals[1:]:
        if abs(v - cur[-1]) / cur[-1] <= tol:
            cur.append(v)
        else:
            if len(cur) >= 2:
                groups.append(sum(cur) / len(cur))
            cur = [v]
    if len(cur) >= 2:
        groups.append(sum(cur) / len(cur))
    return groups


def find_liquidity_pools(cs):
    """Pools de liquidité (equal highs, equal lows) -> (pools de sommets, pools de creux)."""
    hi, lo = find_pivots(cs, LIQUIDITY_POOL_SWING_WINDOW, LIQUIDITY_POOL_SWING_WINDOW)
    return (group_equal_levels([cs[i]["high"] for i in hi]),
            group_equal_levels([cs[i]["low"] for i in lo]))


def near_level(price, levels):
    tol = LIQUIDITY_POOL_TOLERANCE_PCT / 100
    return any(abs(price - lvl) / lvl <= tol for lvl in levels)


def is_gap_candle(cs, i, gran):
    """Bougie qui suit un écart de temps anormal (fermeture de marché le weekend)."""
    if i == 0 or not gran:
        return True
    return cs[i]["epoch"] - cs[i - 1]["epoch"] > gran * WEEKEND_GAP_MULTIPLIER


def detect_sweeps(ltf, rng, gran, check_gaps):
    """Bougies du TF de confirmation qui dépassent le range puis referment dedans."""
    out = []
    for i, c in enumerate(ltf):
        if c["epoch"] <= rng["epoch"]:
            continue
        if check_gaps and is_gap_candle(ltf, i, gran):
            continue
        if c["high"] > rng["high"] and c["close"] < rng["high"]:
            out.append({"i": i, "dir": "SELL", "candle": c})
        if c["low"] < rng["low"] and c["close"] > rng["low"]:
            out.append({"i": i, "dir": "BUY", "candle": c})
    return out


def mss_displaced(m, j, d):
    """La bougie j est-elle impulsive dans le sens d ? Corps >= MSS_BODY_MULT x corps moyen des 10 précédentes."""
    c = m[j]
    if (c["close"] > c["open"]) != (d == "BUY"):
        return False
    avg = body_avg(m[max(0, j - 10):j])
    return avg > 0 and abs(c["close"] - c["open"]) >= MSS_BODY_MULT * avg


def structure_shift(ltf, si, d, w):
    """Après le sweep, une clôture casse le dernier swing dans le sens du retournement. Avec
    MSS_REQUIRE_DISPLACEMENT, la bougie de cassure doit être impulsive (sinon : simple glissement du prix)."""
    highs, lows = find_pivots(ltf[:si + 1], w, w)
    if d == "SELL":
        if not lows:
            return None
        lvl = ltf[lows[-1]]["low"]
        for j in range(si + 1, len(ltf)):
            if ltf[j]["close"] < lvl and (not MSS_REQUIRE_DISPLACEMENT or mss_displaced(ltf, j, d)):
                return {"j": j, "level": lvl}
    else:
        if not highs:
            return None
        lvl = ltf[highs[-1]]["high"]
        for j in range(si + 1, len(ltf)):
            if ltf[j]["close"] > lvl and (not MSS_REQUIRE_DISPLACEMENT or mss_displaced(ltf, j, d)):
                return {"j": j, "level": lvl}
    return None


def sweep_invalidated(ltf, si, d, rng):
    """Le sweep a échoué si, depuis la bougie de sweep, une clôture est ressortie du range côté balayé :
    ce n'est plus une manipulation mais une vraie cassure."""
    for c in ltf[si + 1:]:
        if (d == "SELL" and c["close"] > rng["high"]) or (d == "BUY" and c["close"] < rng["low"]):
            return True
    return False


def detect_fvg(cs, lo, hi, d):
    """Fair Value Gap créé par le déplacement entre le sweep (indice lo) et la cassure (indice hi), le plus récent
    d'abord. Il faut une vraie bougie de déplacement au centre et un écart d'au moins FVG_MIN_ATR_FRAC x ATR."""
    up = d == "BUY"
    for i in range(min(len(cs) - 2, hi), lo, -1):
        c1, c2, c3 = cs[i - 1], cs[i], cs[i + 1]
        if up and not c1["high"] < c3["low"]:
            continue
        if not up and not c1["low"] > c3["high"]:
            continue
        gap = (c3["low"] - c1["high"]) if up else (c1["low"] - c3["high"])
        if gap < FVG_MIN_ATR_FRAC * _atr_before(cs, i):
            continue
        avg = body_avg(cs[max(0, i - 10):i])
        if (c2["close"] > c2["open"]) != up or (avg and abs(c2["close"] - c2["open"]) < FVG_DISP_BODY_MULT * avg):
            continue
        if up:
            return {"index": i, "top": c3["low"], "bottom": c1["high"]}
        return {"index": i, "top": c1["low"], "bottom": c3["high"]}
    return None


def ob_index(m, j, d, lo=0):
    """Indice de l'Order Block : dernière bougie opposée avant la bougie de cassure j, après le sweep (indice lo)
    et à OB_LOOKBACK bougies au plus de la cassure. Même définition pour le filtre et pour l'entrée."""
    bull = d == "BUY"
    x = j - 1
    while x >= lo and not ((m[x]["close"] <= m[x]["open"]) if bull else (m[x]["close"] >= m[x]["open"])):
        x -= 1
    return x if x >= lo and j - x <= OB_LOOKBACK else None


def detect_order_block(cs, j, d, lo=0):
    x = ob_index(cs, j, d, lo)
    return None if x is None else {"index": x, "high": cs[x]["high"], "low": cs[x]["low"]}


def trend_pivots(closed, w):
    """Pivots confirmés ; deux pivots consécutifs au même prix forment un seul swing (sommet ou creux plat)."""
    hi, lo = find_pivots(closed, w, w)
    hi = [i for n, i in enumerate(hi) if n == 0 or closed[i]["high"] != closed[hi[n - 1]]["high"]]
    lo = [i for n, i in enumerate(lo) if n == 0 or closed[i]["low"] != closed[lo[n - 1]]["low"]]
    return hi, lo


def _atr_before(cs, end, n=14):
    """ATR simple des n bougies qui précèdent l'indice end."""
    trs = [max(cs[i]["high"] - cs[i]["low"], abs(cs[i]["high"] - cs[i - 1]["close"]),
               abs(cs[i]["low"] - cs[i - 1]["close"])) for i in range(max(1, end - n), end)]
    return sum(trs) / len(trs) if trs else 0.0


def break_displaced(closed, pivot_i, level, up):
    """La cassure du pivot (indice pivot_i, prix level) est-elle impulsive ? On prend la première clôture
    au-delà du niveau après le pivot ; elle est validée si elle, ou l'une des TREND_DISP_CONFIRM bougies
    suivantes qui clôture aussi au-delà, est dirigée dans le bon sens avec un corps >= TREND_DISP_BODY_MULT x
    le corps moyen des 10 bougies précédentes ET une clôture à au moins TREND_DISP_ATR_FRAC x ATR du niveau
    (l'une des deux seulement si TREND_DISP_REQUIRE_BOTH est faux).
    Une mèche qui dépasse le niveau sans clôture au-delà n'est pas une cassure."""
    n = len(closed)
    jb = next((j for j in range(pivot_i + 1, n)
               if (closed[j]["close"] > level if up else closed[j]["close"] < level)), None)
    if jb is None:
        return False
    for j in range(jb, min(jb + TREND_DISP_CONFIRM + 1, n)):
        c = closed[j]
        beyond = c["close"] - level if up else level - c["close"]
        if beyond <= 0 or (c["close"] > c["open"]) != up:
            continue
        prev = closed[max(0, j - 10):j]
        avg = sum(abs(p["close"] - p["open"]) for p in prev) / len(prev) if prev else 0.0
        big_body = avg > 0 and abs(c["close"] - c["open"]) >= TREND_DISP_BODY_MULT * avg
        far = beyond >= TREND_DISP_ATR_FRAC * _atr_before(closed, j)
        if (big_body and far) if TREND_DISP_REQUIRE_BOTH else (big_body or far):
            return True
    return False


def _rises(vals, extra=0):
    """Nombre de hausses consécutives en fin de liste de prix (+ extra si le cours a déjà dépassé le dernier)."""
    k = 0
    while k + 1 < len(vals) and vals[-1 - k] > vals[-2 - k]:
        k += 1
    return k + extra


def trend_structure_info(cs, displacement=False):
    """Tendance par structure de marché (précoce). Haussière si, sur les pivots confirmés :
    - les deux derniers creux montent (higher low) ;
    - ET soit les deux derniers sommets montent (higher high), soit le dernier cours clôture
      au-dessus du dernier sommet (cassure de structure : pas besoin d'attendre un second sommet) ;
    - ET le dernier cours reste au-dessus du dernier creux (sinon la structure est cassée : la
      tendance est déclarée finie dès la cassure, sans attendre un swing de plus).
    Avec displacement=True, la dernière cassure de sommet (celle qui a créé le dernier sommet plus haut, ou
    celle du dernier sommet par le cours) doit être impulsive (break_displaced) : sans cela, « range ».
    Baissière : miroir. Retourne {'trend', 'age', 'swings'} : age = naissante (1 alignement), établie (2),
    mûre (TREND_MATURE_SWINGS et plus), avec swings = nombre d'alignements successifs (sommets ET creux)."""
    closed = cs[:-1] if len(cs) > 1 else cs
    w = TREND_STRUCTURE_WINDOW
    none = {"trend": None, "age": None, "swings": None}
    if len(closed) < 4 * w:
        return none
    hi, lo = trend_pivots(closed, w)
    if len(hi) < 2 or len(lo) < 2:
        return none
    hv, lv = [closed[i]["high"] for i in hi[-8:]], [closed[i]["low"] for i in lo[-8:]]
    h1, h2, l1, l2 = hv[-1], hv[-2], lv[-1], lv[-2]
    px = closed[-1]["close"]

    if l1 > l2 and px > l1:
        up_ok = (h1 > h2 and (not displacement or break_displaced(closed, hi[-2], h2, True))) or \
                (px > h1 and (not displacement or break_displaced(closed, hi[-1], h1, True)))
        if up_ok:
            k = min(_rises(hv, 1 if px > h1 else 0), _rises(lv))
            return {"trend": "bullish", "age": _age(k), "swings": k}
    if h1 < h2 and px < h1:
        dn_ok = (l1 < l2 and (not displacement or break_displaced(closed, lo[-2], l2, False))) or \
                (px < l1 and (not displacement or break_displaced(closed, lo[-1], l1, False)))
        if dn_ok:
            k = min(_rises([-v for v in lv], 1 if px < l1 else 0), _rises([-v for v in hv]))
            return {"trend": "bearish", "age": _age(k), "swings": k}
    return {"trend": "range", "age": None, "swings": None}


def _age(k):
    return "naissante" if k <= 1 else ("établie" if k < TREND_MATURE_SWINGS else "mûre")


def detect_trend_swings(cs):
    """Ancienne règle (plus tardive) : 3 sommets ET 3 creux consécutifs dans le même sens + SMA 50."""
    closed = cs[:-1] if len(cs) > 1 else cs
    if len(closed) < TREND_SMA_PERIOD:
        return None
    hi, lo = trend_pivots(closed, TREND_SWING_WINDOW)
    if len(hi) < TREND_SWING_COUNT or len(lo) < TREND_SWING_COUNT:
        return None
    h = [closed[i]["high"] for i in hi[-TREND_SWING_COUNT:]]
    l = [closed[i]["low"] for i in lo[-TREND_SWING_COUNT:]]
    up = all(h[k] < h[k + 1] for k in range(len(h) - 1)) and all(l[k] < l[k + 1] for k in range(len(l) - 1))
    down = all(h[k] > h[k + 1] for k in range(len(h) - 1)) and all(l[k] > l[k + 1] for k in range(len(l) - 1))
    sma = sum(c["close"] for c in closed[-TREND_SMA_PERIOD:]) / TREND_SMA_PERIOD
    if up and closed[-1]["close"] > sma:
        return "bullish"
    if down and closed[-1]["close"] < sma:
        return "bearish"
    return "range"


def trend_info(cs, mode=None):
    """Tendance de fond avec son âge : {'trend': 'bullish'|'bearish'|'range'|None, 'age', 'swings'}.
    La dernière bougie de cs (en formation) est ignorée. Règle choisie par `mode` ou TREND_MODE ;
    le mode swings (ancienne règle) ne calcule pas d'âge."""
    mode = mode or TREND_MODE
    if mode == "swings":
        return {"trend": detect_trend_swings(cs), "age": None, "swings": None}
    return trend_structure_info(cs, displacement=(mode == "deplacement"))


def detect_trend(cs):
    """Tendance de fond : 'bullish', 'bearish', 'range' ou None (historique insuffisant)."""
    return trend_info(cs)["trend"]


def is_stale(d, j, t, cs):
    """Setup périmé : depuis la cassure (bougie j), le prix a déjà touché le SL ou atteint TP1."""
    for c in cs[j + 1:]:
        if d == "SELL" and (c["high"] >= t["sl"] or c["low"] <= t["tp1"]):
            return True
        if d == "BUY" and (c["low"] <= t["sl"] or c["high"] >= t["tp1"]):
            return True
    return False


def is_mitigated(d, j, entry, ltf):
    """L'entrée a-t-elle déjà été retestée avant l'alerte ? On regarde les bougies clôturées après la cassure, sauf la
    plus récente (un retest en cours de formation donne un ordre au marché légitime)."""
    for c in ltf[j + 1:-1]:
        if (d == "SELL" and c["high"] >= entry) or (d == "BUY" and c["low"] <= entry):
            return True
    return False


def setup_expiry(ref_n, rng):
    """Fin de C3 : l'expansion est attendue pendant la bougie de référence qui suit C2. Pour le W1, rng['epoch'] est
    l'ouverture de la dernière bougie D1 de la semaine."""
    c1_end = rng["epoch"] + (86400 if ref_n == "W1" else REF_GRAN[ref_n])
    return c1_end + (EXPIRY_REF_CANDLES - 1) * REF_GRAN[ref_n]


def setup_keys(s):
    """Clés anti-doublon : un setup par range de référence, par TF de confirmation et par sens,
    plus un contrôle sur le contenu (mêmes niveaux = même message)."""
    t = s["trade"]
    mode = s.get("mode", "loose")
    return (f"{s['sym']}_{s['ref_n']}_{s['ref_epoch']}_{s['conf_n']}_{s['dir']}" + ("" if mode == "loose" else f"_{mode}"),
            f"content_{s['sym']}_{s['dir']}_{round(t['entry'], 6)}_{round(t['sl'], 6)}_{round(t['tp2'], 6)}")


def spread_pct(sym):
    """Spread estimé (% du prix) : préfixe le plus long de SPREAD_PCT (frxXAU avant frx), 0 sinon."""
    for k in sorted(SPREAD_PCT, key=len, reverse=True):
        if sym.startswith(k):
            return SPREAD_PCT[k]
    return 0.0


_NEWS_CACHE = {}


def _news_file_events():
    """Événements de NEWS_FILE : liste de (epoch, {devises}). Lu une fois ; fichier absent ou invalide = aucun."""
    if "file" not in _NEWS_CACHE:
        out = []
        try:
            with open(NEWS_FILE, encoding="utf-8") as f:
                for e in json.load(f):
                    dt = datetime.fromisoformat(str(e["time"]).replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    out.append((dt.timestamp(), set(e.get("currencies") or ["USD"])))
        except Exception:
            pass
        _NEWS_CACHE["file"] = out
    return _NEWS_CACHE["file"]


def nfp_events(epoch):
    """NFP (premier vendredi du mois, 8h30 New York) du mois de `epoch` et des deux mois voisins."""
    dt, out = datetime.fromtimestamp(epoch, NY), []
    for k in (-1, 0, 1):
        y, m = dt.year + (dt.month - 1 + k) // 12, (dt.month - 1 + k) % 12 + 1
        first = datetime(y, m, 1, 8, 30, tzinfo=NY)
        out.append((first.replace(day=1 + (4 - first.weekday()) % 7).timestamp(), {"USD"}))
    return out


def news_blackout(sym, epoch):
    """True si `epoch` tombe dans la fenêtre d'une annonce concernant une devise du symbole (forex et or)."""
    return bool(NEWS_FILTER and _news_hit(sym, epoch, NEWS_BLOCK_BEFORE_MIN * 60, NEWS_BLOCK_AFTER_MIN * 60))


def _news_ccy(sym):
    if sym.startswith("frx"):
        return {sym[3:6], sym[6:9]}
    if sym.startswith("cry") and NEWS_INCLUDE_CRYPTO:
        return {"USD"}
    return set()


def _news_hit(sym, epoch, before_s, after_s):
    """Epoch de la première annonce dont la fenêtre [annonce - before_s, annonce + after_s] contient `epoch`, sinon None."""
    ccy = _news_ccy(sym)
    if not ccy:
        return None
    for ts, cur in _news_file_events() + nfp_events(epoch):
        if cur & ccy and ts - before_s <= epoch <= ts + after_s:
            return ts
    return None


def news_upcoming(sym, epoch, minutes):
    """Annonce touchant le symbole dans les `minutes` à venir (epoch de l'annonce) ou None."""
    ccy = _news_ccy(sym)
    for t, cur in _news_file_events() + nfp_events(epoch):
        if cur & ccy and epoch < t <= epoch + minutes * 60:
            return t
    return None


def rollover_blackout(sym, epoch):
    """Fenêtre de rollover (17h New York) : spreads élargis, pas d'alerte pour le forex et l'or."""
    if not ROLLOVER_FILTER or not sym.startswith("frx"):
        return False
    dt = datetime.fromtimestamp(epoch, NY)
    return ROLLOVER_NY[0] <= dt.hour * 60 + dt.minute <= ROLLOVER_NY[1]


def risk_gate(stats, now):
    """Raison de suspendre les nouvelles alertes (plafond de trades, perte du jour ou de la semaine en R géré), sinon None."""
    n_open = len(stats.get("pending", {}))
    if n_open >= MAX_OPEN_TRADES:
        return ("cap", f"{n_open} trades déjà suivis (plafond {MAX_OPEN_TRADES})")
    dt = datetime.fromtimestamp(now, NY)
    day0 = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    week0 = day0 - timedelta(days=day0.weekday())
    done = [h for h in stats.get("history", []) if h["result"] in ("TP", "SL")]
    day_r = sum(tracker.managed_r(h) for h in done if h.get("resolved_epoch", 0) >= day0.timestamp())
    week_r = sum(tracker.managed_r(h) for h in done if h.get("resolved_epoch", 0) >= week0.timestamp())
    if day_r <= -MAX_DAILY_LOSS_R:
        return ("day", f"perte du jour {day_r:+.1f} R (limite -{MAX_DAILY_LOSS_R:g} R)")
    if week_r <= -MAX_WEEKLY_LOSS_R:
        return ("week", f"perte de la semaine {week_r:+.1f} R (limite -{MAX_WEEKLY_LOSS_R:g} R)")
    return None


def exposure_group(sym, d):
    """(groupe, côté) de l'exposition : forex/or = côté du dollar, cryptos = sens du trade ; None sinon."""
    s = sym[3:]
    if sym.startswith("cry"):
        return ("CRYPTO", 1 if d == "BUY" else -1)
    if sym.startswith("frx"):
        if s.endswith("USD"):
            return ("USD", -1 if d == "BUY" else 1)   # acheter EURUSD ou l'or = vendre le dollar
        if s.startswith("USD"):
            return ("USD", 1 if d == "BUY" else -1)
    return None


def correlated_open(stats, sym, d):
    """Nombre de trades suivis (en attente ou ouverts) sur le même côté du même groupe corrélé."""
    g = exposure_group(sym, d)
    if g is None:
        return 0
    return sum(1 for t in stats.get("pending", {}).values() if exposure_group(t["symbol"], t["direction"]) == g)


REF_RANK = {"W1": 3, "D1": 2, "H4": 1}


def select_best(setups):
    """Un seul setup par symbole et par sens (les références W1/D1/H4 d'un même mouvement sont corrélées) :
    la référence la plus haute, puis sweep dans un FVG HTF, puis dans une killzone, puis dans la tendance, puis le meilleur ratio."""
    best = {}
    for s in setups:
        rank = (REF_RANK.get(s["ref_n"], 0), bool(s.get("poi")), s["session"] is not None, not s["counter"], s["trade"]["rr"])
        k = (s["sym"], s["dir"])
        if k not in best or rank > best[k][0]:
            best[k] = (rank, s)
    return [v[1] for v in best.values()]


def analyze(sym, htf, ltf_by_tf, now=None):
    """Cherche les setups CRT d'un symbole : sweep -> cassure de structure impulsive -> FVG et/ou OB.
    `now` : instant de l'analyse (le backtest rejoue le passé)."""
    now = time.time() if now is None else now
    if news_blackout(sym, now) or rollover_blackout(sym, now):
        return []   # pas de verrou : le setup sera renvoyé après la fenêtre s'il est toujours valide
    info = trend_info(htf["D1"]) if htf.get("D1") else {"trend": None, "age": None}
    trend = info["trend"]
    check_gaps = sym.startswith(WEEKEND_GAP_PREFIXES)
    ny = NY_ALIGNED and not is_247(sym)
    setups = []
    for ref_n, conf_list in REFERENCE_CONFIRMATION_MAP.items():
        rng = ref_range(ref_n, htf, ny)
        if not rng:
            continue
        expires = setup_expiry(ref_n, rng)
        if now > expires:
            continue  # C3 est terminée : l'expansion attendue n'a plus lieu d'être
        size, r_atr = rng["high"] - rng["low"], ref_atr(ref_n, htf, ny)
        if r_atr and not RANGE_MIN_ATR * r_atr <= size <= RANGE_MAX_ATR * r_atr:
            continue  # range de C1 trop petit (bruit) ou trop grand (SL large, TP2 irréaliste)
        for conf_n in conf_list:
            full = ltf_by_tf.get(conf_n)
            if not full or len(full) < 20:
                continue
            ltf = full[:-1]  # bougies clôturées uniquement (la dernière est en formation)
            price = full[-1]["close"]
            if REQUIRE_C2_CLOSE_INSIDE and not rng["low"] <= price <= rng["high"]:
                continue  # C2 ne clôture pas dans le range de C1 : pas de CRT
            sweeps = detect_sweeps(ltf, rng, GRAN[conf_n], check_gaps)
            if not sweeps:
                continue
            if SKIP_BOTH_SIDES_SWEPT and {x["dir"] for x in sweeps} == {"BUY", "SELL"}:
                continue  # les deux côtés balayés (outside bar) : ce n'est plus un CRT
            pools = find_liquidity_pools(ltf)
            w = STRUCTURE_SWING_WINDOW.get(conf_n, DEFAULT_STRUCTURE_SWING_WINDOW)
            for s in sweeps:
                d, c = s["dir"], s["candle"]
                if len(ltf) - 1 - s["i"] > SWEEP_MAX_AGE.get(conf_n, 10 ** 6):
                    continue  # sweep trop ancien
                depth = (c["high"] - rng["high"]) if d == "SELL" else (rng["low"] - c["low"])
                if depth > SWEEP_MAX_RANGE_FRAC * size or depth > SWEEP_MAX_ATR * _atr_before(ltf, s["i"]):
                    continue  # sweep trop profond : cassure plutôt que manipulation
                if REQUIRE_C2_CLOSE_INSIDE and sweep_invalidated(ltf, s["i"], d, rng):
                    continue
                st = structure_shift(ltf, s["i"], d, w)
                if not st:
                    continue
                fvg = detect_fvg(ltf, s["i"], st["j"], d)
                ob = detect_order_block(ltf, st["j"], d, s["i"])
                if not fvg and not ob:
                    continue  # pas de confirmation avancée
                if REQUIRE_FVG and not fvg:
                    continue
                counter = (trend == "bullish" and d == "SELL") or (trend == "bearish" and d == "BUY")
                if BLOCK_COUNTER_TREND and counter:
                    continue
                ext = c["high"] if d == "SELL" else c["low"]
                poi = htf_poi(ref_n, htf, d, ext, r_atr)
                if REQUIRE_HTF_POI and not poi:
                    continue
                sw = {"dir": d, "i": s["i"], "sl": ext, "range": rng, "closed": ltf, "fvg": fvg}
                bos = {"j": st["j"], "m": ltf, "epoch": ltf[st["j"]]["epoch"]}
                t = build_trade(sym, sw, bos, price, pools)
                if t is None or is_stale(d, st["j"], t, full):
                    continue
                if SKIP_MITIGATED and not is_synthetic(sym) and is_mitigated(d, st["j"], t["entry"], ltf):
                    continue  # l'entrée a déjà été retestée : le setup est moins fiable
                session = killzone(sym, c["epoch"])
                if session is None and REQUIRE_KILLZONE_FOR_REAL_MARKETS:
                    continue
                swept_pools = pools[0] if d == "SELL" else pools[1]
                setups.append({
                    "sym": sym, "ref_n": ref_n, "conf_n": conf_n, "ref_epoch": rng["epoch"],
                    "dir": d, "sw": sw, "fvg": fvg, "ob": ob, "trade": t, "ltf": ltf,
                    "price": price, "session": session,
                    "grabbed": near_level(ext, swept_pools),
                    "counter": counter, "poi": poi,
                    "expires": expires, "max_age": max(60, int(expires - now)),
                    "trend": trend, "trend_age": info["age"],
                })
    return setups


def legacy_key(s, k_range):
    """Ancien format de clé (avant l'ajout du sens), pour ne pas renvoyer un setup déjà alerté. Mode loose uniquement."""
    return k_range.rsplit("_", 1)[0] if s.get("mode", "loose") == "loose" else None


def current_variant():
    """Variante CRT active : « loose » ou le mode d'entrée strict choisi (ote, mid, ote_confirm)."""
    return "loose" if CRT_MODE == "loose" else STRICT_ENTRY


def variant_label(v):
    return {"loose": "CRT", "ote": "CRT strict · OTE de C2", "mid": "CRT strict · 50 % de C2",
            "ote_confirm": "CRT strict · OTE de C2 + confirmation"}[v]


def strict_candles(ref_n, htf, ny=False):
    """(C1, C2, début de C3, C3) pour la référence. C2 = dernière bougie clôturée, C1 = celle d'avant, C3 = en formation.
    W1 : semaines complètes agrégées depuis le D1 (C3 = D1 depuis la fin de C2). None si l'historique manque."""
    if ref_n == "W1":
        d1 = htf.get("D1")
        wks = complete_weeks(d1, ny)
        if len(wks) < 2:
            return None
        agg = lambda w: {"open": w[0]["open"], "high": max(c["high"] for c in w), "low": min(c["low"] for c in w),
                         "close": w[-1]["close"], "epoch": w[-1]["epoch"]}
        c1, c2 = agg(wks[-2]), agg(wks[-1])
        start = c2["epoch"] + 86400
        cur = [c for c in d1 if c["epoch"] >= start] or [d1[-1]]
        c3 = {"epoch": start, "open": cur[0]["open"], "high": max(c["high"] for c in cur),
              "low": min(c["low"] for c in cur), "close": cur[-1]["close"]}
        return c1, c2, start, c3
    cs = htf.get(ref_n)
    if not cs or len(cs) < 4:
        return None
    return cs[-3], cs[-2], cs[-1]["epoch"], cs[-1]


def build_strict_trade(sym, rng, c2, d, level, price, r_atr):
    """Ordre limite à `level` du range de C2 (mesuré depuis l'extrême opposé au sweep), SL au-delà de l'extrême de C2,
    TP1 au milieu de C1, TP2 au bord opposé de C1. None si le setup est ignoré."""
    buy = d == "BUY"
    size2 = c2["high"] - c2["low"]
    if size2 <= 0:
        return None
    entry = (c2["high"] - level * size2) if buy else (c2["low"] + level * size2)
    ext = c2["low"] if buy else c2["high"]
    hi, lo = rng["high"], rng["low"]
    if STRICT_ENTRY_INSIDE_RANGE and not lo < entry < hi:
        return None
    mid = (hi + lo) / 2
    if REQUIRE_PREMIUM_DISCOUNT and ((entry > mid) if buy else (entry < mid)):
        return None
    buf = max(ext * STOP_LOSS_BUFFER_PCT / 100, STRICT_SL_BUFFER_ATR * r_atr) + ext * spread_pct(sym) / 100
    extra = (hi - lo) * TP_EXTENSION_PCT / 100
    if buy:
        sl, tp1, tp2, tp3 = ext - buf, mid, hi, hi + extra
    else:
        sl, tp1, tp2, tp3 = ext + buf, mid, lo, lo - extra
    if not ((sl < entry < tp2) if buy else (sl > entry > tp2)):
        return None
    rr = abs(tp2 - entry) / abs(entry - sl)
    if rr < MIN_RISK_REWARD:
        return None
    pct = round(100 * level, 1)
    how = f"OTE de C2 ({pct:g} %)" if level >= STRICT_OTE_ZONE[0] else f"{pct:g} % de C2"
    return {"otype": order_type(d, entry, price), "entry": entry, "how": how, "sl": sl, "tp1": tp1, "tp2": tp2,
            "tp3": tp3, "rr": rr, "ote": STRICT_OTE_ZONE[0] <= level <= STRICT_OTE_ZONE[1]}


def analyze_strict(sym, htf, ltf_by_tf, now=None, entry=None):
    """CRT strict : C2 clôturée dans le range de C1 après avoir balayé un seul côté ; entrée sur C2 pendant C3 (voir
    STRICT_ENTRY). `entry` : ote, mid ou ote_confirm (défaut : STRICT_ENTRY)."""
    now = time.time() if now is None else now
    entry = entry or STRICT_ENTRY
    if news_blackout(sym, now) or rollover_blackout(sym, now):
        return []
    info = trend_info(htf["D1"]) if htf.get("D1") else {"trend": None, "age": None}
    trend = info["trend"]
    ny = NY_ALIGNED and not is_247(sym)
    setups = []
    for ref_n, conf_list in REFERENCE_CONFIRMATION_MAP.items():
        sc = strict_candles(ref_n, htf, ny)
        if not sc:
            continue
        c1, c2, c3_start, c3 = sc
        expires = c3_start + REF_GRAN[ref_n]   # fin de C3
        if now > expires:
            continue
        rng = {"high": c1["high"], "low": c1["low"], "epoch": c1["epoch"]}
        size, r_atr = rng["high"] - rng["low"], ref_atr(ref_n, htf, ny)
        if size <= 0 or (r_atr and not RANGE_MIN_ATR * r_atr <= size <= RANGE_MAX_ATR * r_atr):
            continue
        swept_hi, swept_lo = c2["high"] > c1["high"], c2["low"] < c1["low"]
        if swept_hi == swept_lo:
            continue   # aucun sweep, ou les deux côtés (outside bar)
        d = "SELL" if swept_hi else "BUY"
        if not c1["low"] <= c2["close"] <= c1["high"]:
            continue   # C2 n'a pas clôturé dans le range : vraie cassure
        ext2 = c2["high"] if d == "SELL" else c2["low"]
        depth = (c2["high"] - c1["high"]) if d == "SELL" else (c1["low"] - c2["low"])
        if depth > SWEEP_MAX_RANGE_FRAC * size:
            continue
        conf_n = next((c for c in conf_list if ltf_by_tf.get(c) and len(ltf_by_tf[c]) >= 20), None)
        if not conf_n:
            continue   # pas de données récentes (marché fermé) : on ne place pas d'ordre
        full = ltf_by_tf[conf_n]
        ltf, price = full[:-1], full[-1]["close"]
        after = [x for x in full if x["epoch"] >= c3_start]    # bougies de C3 (la dernière est en formation)
        if any((x["high"] > ext2) if d == "SELL" else (x["low"] < ext2) for x in after + [c3]):
            continue   # C3 a déjà dépassé l'extrême de C2 : la thèse est invalidée
        counter = (trend == "bullish" and d == "SELL") or (trend == "bearish" and d == "BUY")
        if BLOCK_COUNTER_TREND and counter:
            continue
        poi = htf_poi(ref_n, htf, d, ext2, r_atr)
        if REQUIRE_HTF_POI and not poi:
            continue
        pools = find_liquidity_pools(ltf)
        size2 = c2["high"] - c2["low"]
        z1, z2 = STRICT_OTE_ZONE
        zone = ((c2["low"] + z1 * size2, c2["low"] + z2 * size2) if d == "SELL"
                else (c2["high"] - z2 * size2, c2["high"] - z1 * size2))
        fvg = ob = None
        if entry == "ote_confirm":
            i0 = next((i for i, x in enumerate(ltf) if x["epoch"] >= c3_start
                       and ((x["high"] >= zone[0]) if d == "SELL" else (x["low"] <= zone[1]))), None)
            if i0 is None:
                continue   # l'OTE de C2 n'a pas encore été touchée
            w = STRUCTURE_SWING_WINDOW.get(conf_n, DEFAULT_STRUCTURE_SWING_WINDOW)
            st = structure_shift(ltf, i0, d, w)
            if not st:
                continue
            fvg, ob = detect_fvg(ltf, i0, st["j"], d), detect_order_block(ltf, st["j"], d, i0)
            if not fvg and not ob:
                continue
            seg = ltf[i0:st["j"] + 1]
            ext = max(x["high"] for x in seg) if d == "SELL" else min(x["low"] for x in seg)
            sw = {"dir": d, "i": i0, "sl": ext, "range": rng, "closed": ltf, "fvg": fvg}
            t = build_trade(sym, sw, {"j": st["j"], "m": ltf, "epoch": ltf[st["j"]]["epoch"]}, price, pools)
            if t is None or is_stale(d, st["j"], t, full):
                continue
            if SKIP_MITIGATED and is_mitigated(d, st["j"], t["entry"], ltf):
                continue
            session = killzone(sym, ltf[i0]["epoch"])
        else:
            level = STRICT_OTE_LEVEL if entry == "ote" else STRICT_MID_LEVEL
            t = build_strict_trade(sym, rng, c2, d, level, price, r_atr)
            if t is None:
                continue
            k = max((i for i, x in enumerate(full) if x["epoch"] < c3_start), default=-1)
            if is_stale(d, k, t, full) or is_stale(d, -1, t, [c3]):
                continue   # SL ou TP1 déjà touchés pendant C3
            if SKIP_MITIGATED and any((x["high"] >= t["entry"]) if d == "SELL" else (x["low"] <= t["entry"])
                                      for x in after[:-1]):
                continue   # l'entrée a déjà été touchée pendant C3 : l'ordre aurait été rempli avant l'alerte
            session = killzone(sym, int(now))
            sw = {"dir": d, "i": None, "sl": ext2, "range": rng, "closed": ltf, "fvg": None}
        swept_pools = pools[0] if d == "SELL" else pools[1]
        setups.append({
            "sym": sym, "ref_n": ref_n, "conf_n": conf_n, "ref_epoch": c2["epoch"], "mode": entry,
            "dir": d, "sw": sw, "fvg": fvg, "ob": ob, "trade": t, "ltf": ltf, "price": price, "session": session,
            "grabbed": near_level(ext2, swept_pools), "counter": counter, "poi": poi,
            "c2": c2, "ote_zone": zone, "expires": expires, "max_age": max(60, int(expires - now)),
            "trend": trend, "trend_age": info["age"],
        })
    return setups


def analyze_variant(sym, htf, ltf_by_tf, now=None, variant=None):
    """Point d'entrée unique : mode historique (loose) ou CRT strict avec l'entrée choisie."""
    variant = variant or current_variant()
    if variant == "loose":
        return analyze(sym, htf, ltf_by_tf, now=now)
    return analyze_strict(sym, htf, ltf_by_tf, now=now, entry=variant)


def entry_zone(m, j, d, lo=0, fvg=None):
    """Zone d'entrée au niveau de la cassure LTF (bougie j), par priorité :
    Order Block, sinon Breaker Block, sinon FVG (milieu). `lo` : indice du sweep (la zone doit lui être postérieure),
    `fvg` : le FVG de déplacement déjà détecté. Retourne (nom, niveau, zone)."""
    bull = d == "BUY"
    opposite = (lambda c: c["close"] <= c["open"]) if bull else (lambda c: c["close"] >= c["open"])
    # 1) Order Block : dernière bougie opposée avant le mouvement qui casse la structure (après le sweep)
    x = ob_index(m, j, d, lo)
    if x is not None:
        z = (m[x]["low"], m[x]["high"])
        return "Order Block", (z[1] if bull else z[0]), z
    # 2) Breaker Block : dernière bougie dans le sens du trade avant le creux (sommet) de manipulation,
    #    dont la clôture de cassure franchit l'extrême
    seg = range(max(lo, j - 12), j)
    if len(seg) >= 3:
        piv = min(seg, key=lambda t: m[t]["low"]) if bull else max(seg, key=lambda t: m[t]["high"])
        y = piv - 1
        while y >= 0 and opposite(m[y]):
            y -= 1
        if y >= 0 and ((m[j]["close"] > m[y]["high"]) if bull else (m[j]["close"] < m[y]["low"])):
            z = (m[y]["low"], m[y]["high"])
            return "Breaker Block", (z[1] if bull else z[0]), z
    # 3) FVG : milieu du FVG de déplacement (sinon de l'écart laissé par la bougie de cassure)
    if fvg:
        z = (fvg["bottom"], fvg["top"])
        return "FVG 50 %", (z[0] + z[1]) / 2, z
    for i in (j, j + 1):
        if 2 <= i < len(m):
            if bull and m[i]["low"] > m[i - 2]["high"]:
                z = (m[i - 2]["high"], m[i]["low"])
                return "FVG 50 %", (z[0] + z[1]) / 2, z
            if not bull and m[i]["high"] < m[i - 2]["low"]:
                z = (m[i]["high"], m[i - 2]["low"])
                return "FVG 50 %", (z[0] + z[1]) / 2, z
    return None


def order_type(d, entry, price):
    """Buy/Sell (au marché), Buy/Sell Limit ou Buy/Sell Stop, selon l'entrée et le prix actuel."""
    if abs(entry - price) <= price * ORDER_TYPE_TOLERANCE_PCT / 100:
        return d
    if d == "BUY":
        return "BUY LIMIT" if entry < price else "BUY STOP"
    return "SELL LIMIT" if entry > price else "SELL STOP"


def in_ote(sw, m, entry, d):
    """L'entrée tombe-t-elle dans le retracement 61,8-79 % du mouvement impulsif après le sweep ?"""
    t0 = sw["closed"][sw["i"]]["epoch"]
    seg = [c for c in m if c["epoch"] >= t0]
    if not seg:
        return False
    ext = sw["sl"]
    if d == "BUY":
        top = max(c["high"] for c in seg)
        leg, ratio = top - ext, (top - entry) / (top - ext) if top > ext else 0
    else:
        bottom = min(c["low"] for c in seg)
        leg, ratio = ext - bottom, (entry - bottom) / (ext - bottom) if ext > bottom else 0
    return 0.618 <= ratio <= 0.79


def build_trade(sym, sw, bos, price, pools=None):
    """Entrée, SL, TP1-TP3, type d'ordre et R:R. Retourne None si le setup est ignoré."""
    d, rng, m, j = sw["dir"], sw["range"], bos["m"], bos["j"]
    buy, synth = d == "BUY", is_synthetic(sym)
    if synth:   # indices synthétiques : entrée immédiate à la clôture de la bougie de cassure
        entry, how = m[j]["close"], "clôture de la cassure"
    else:
        z = entry_zone(m, j, d, lo=sw.get("i", 0), fvg=sw.get("fvg"))
        if not z:
            return None  # aucun OB, Breaker ni FVG : setup ignoré
        how, entry = z[0], z[1]
    hi, lo = rng["high"], rng["low"]
    ext = sw["sl"]  # extrême de la bougie de sweep
    # marge adaptative : au moins STOP_LOSS_BUFFER_PCT du prix, ou STOP_LOSS_BUFFER_ATR x ATR si plus grand, + spread
    buf = max(ext * STOP_LOSS_BUFFER_PCT / 100, STOP_LOSS_BUFFER_ATR * _atr_before(m, j + 1)) \
        + ext * spread_pct(sym) / 100
    extra = (hi - lo) * TP_EXTENSION_PCT / 100
    mid = (hi + lo) / 2
    if REQUIRE_PREMIUM_DISCOUNT and ((entry > mid) if buy else (entry < mid)):
        return None  # achat en premium / vente en discount : TP1 (mi-range) serait déjà derrière l'entrée
    if buy:
        sl, tp1, tp2, tp3 = ext - buf, mid, hi, hi + extra
    else:
        sl, tp1, tp2, tp3 = ext + buf, mid, lo, lo - extra
    if pools and near_level(sl, pools[1] if buy else pools[0]):
        # SL sur un pool de liquidité (creux égaux sous un achat, sommets égaux au-dessus d'une vente) :
        # marge supplémentaire pour ne pas être la liquidité chassée
        pb = sl * STOP_LOSS_POOL_BUFFER_PCT / 100
        sl = sl - pb if buy else sl + pb
    if not ((sl < entry < tp2) if buy else (sl > entry > tp2)):
        return None
    rr = abs(tp2 - entry) / abs(entry - sl)  # ratio calculé sur TP2
    ote = in_ote(sw, m, entry, d)
    if rr < MIN_RISK_REWARD or (REQUIRE_FIB_OTE and not ote):
        return None
    return {"otype": order_type(d, entry, price), "entry": entry, "how": how, "sl": sl,
            "tp1": tp1, "tp2": tp2, "tp3": tp3, "rr": rr, "ote": ote}


def decimals(cs, cap=6):
    """Nombre de décimales de l'actif, déduit de ses dernières bougies."""
    d = 0
    for c in cs[-100:]:
        for k in ("open", "high", "low", "close"):
            s = repr(float(c[k]))
            if "e" in s or "E" in s or "." not in s:
                continue
            d = max(d, len(s.split(".")[1].rstrip("0")))
    return min(d, cap)


def fmt(x, dec=None):
    if dec is None:
        return f"{x:.5f}".rstrip("0").rstrip(".")
    return f"{x:.{dec}f}"


def code(x, dec=None):
    """Prix en police à espacement fixe (copiable d'un toucher dans Telegram)."""
    return f"<code>{fmt(x, dec)}</code>"


def notify(text, html=False):
    """Envoie un message Telegram. Retourne True si Telegram l'a accepté."""
    if not (TG_TOKEN and TG_CHAT):
        print("ATTENTION : TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID vide -> message NON envoyé :")
        print(text); return False
    payload = {"chat_id": TG_CHAT, "text": text}
    if html:
        payload["parse_mode"] = "HTML"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json=payload, timeout=15,
        )
        if not r.ok:
            print(f"Telegram erreur {r.status_code}: {r.text[:150]}")
        return r.ok
    except Exception as e:
        print(f"Telegram injoignable: {str(e)[:100]}")
        return False


def ratio_txt(d, entry, sl, tp):
    """Ratio gain/risque d'une cible, ex. '   (1:4.9)'."""
    risk = abs(entry - sl)
    gain = (tp - entry) if d == "BUY" else (entry - tp)
    if risk <= 0:
        return ""
    return "   (dépassé)" if gain <= 0 else f"   (1:{gain / risk:.1f})"


def size_line(sym, entry, sl):
    """Taille de position pour RISK_PCT % de ACCOUNT_BALANCE (approximation : les contrats du courtier peuvent différer)."""
    if ACCOUNT_BALANCE <= 0 or abs(entry - sl) <= 0:
        return None
    risk, dist, s = ACCOUNT_BALANCE * RISK_PCT / 100, abs(entry - sl), sym[3:]
    units = risk * entry / dist if (s.startswith("USD") and not s.endswith("USD")) else risk / dist  # risque en devise cotée
    if sym.startswith("frxXAU"):
        txt = f"{units / 100:.2f} lot ({units:.1f} oz)"
    elif sym.startswith("frx"):
        txt = f"{units / 100000:.2f} lot"
    else:
        txt = f"{units:.4g} {s[:-3]}"
    return f"💼 Taille ≈ {txt} (risque {RISK_PCT:g} %)"


def crt_message(s, price, dec=None):
    c = lambda v: code(v, dec)
    t, sw, d, r = s["trade"], s["sw"], s["dir"], s["sw"]["range"]
    lines = [
        f"{'🟢' if d == 'BUY' else '🔴'} {t['otype']} · {display_name(s['sym'])} · {s['conf_n']} · {variant_label(s.get('mode', 'loose'))}",
        f"Session : {s['session'] or 'hors killzone'}", "",
        "────────────────", f"Prix actuel  {c(price)}", "────────────────", "",
        f"Entrée  {c(t['entry'])}  ({t['how']})",
        f"SL  {c(t['sl'])}",
        f"TP1  {c(t['tp1'])}{ratio_txt(d, t['entry'], t['sl'], t['tp1'])}",
        f"TP2  {c(t['tp2'])}{ratio_txt(d, t['entry'], t['sl'], t['tp2'])}",
        f"TP3  {c(t['tp3'])}{ratio_txt(d, t['entry'], t['sl'], t['tp3'])}", "",
        f"Range {s['ref_n']}  {c(r['low'])} - {c(r['high'])}",
        f"✅ Sweep  {c(sw['sl'])}",
    ]
    if s.get("c2"):
        lines.append("✅ C2 clôturée dans le range de C1")
        lines.append(f"OTE de C2  {c(s['ote_zone'][0])} - {c(s['ote_zone'][1])}")
    if s["fvg"]:
        lines.append(f"✅ FVG  {c(s['fvg']['bottom'])} - {c(s['fvg']['top'])}")
    if s["ob"]:
        lines.append(f"✅ OB  {c(s['ob']['low'])} - {c(s['ob']['high'])}")
    if t["ote"]:
        lines.append("✅ OTE (61,8-79 %)")
    if s["grabbed"]:
        lines.append("💧 Pool de liquidité balayé")
    if s.get("poi"):
        lines.append(f"🎯 Sweep dans un FVG {s['ref_n'] if s['ref_n'] != 'W1' else 'D1'}  {c(s['poi']['bottom'])} - {c(s['poi']['top'])}")
    if s["counter"]:
        lines.append("⚠️ Contre-tendance (D1)")
    if s.get("corr"):
        lines.append(f"⚠️ Corrélation : {s['corr']} trade(s) déjà ouvert(s) ou en attente sur le même côté ({exposure_group(s['sym'], d)[0]})")
    sz = size_line(s["sym"], t["entry"], t["sl"])
    if sz:
        lines.append(sz)
    if s.get("expires"):
        lines.append("⏳ Ordre valable jusqu'au " + datetime.fromtimestamp(s["expires"], timezone.utc).strftime("%d/%m %H:%M UTC"))
    if MANAGEMENT_NOTE:
        lines.append("📌 Gestion : la moitié à TP1, SL au point d'entrée, le reste vers TP2")
    return "\n".join(lines)


def check_symbols(api, symbols=None):
    """Journalise les symboles absents chez Deriv (diagnostic uniquement)."""
    symbols = SYMBOLS if symbols is None else symbols
    try:
        msg = api.request({"active_symbols": "brief"})
        names = {x.get("symbol") or x.get("underlying_symbol")
                 for x in (msg or {}).get("active_symbols", [])}
        if names:
            missing = [s for s in symbols if s not in names]
            print(f"Symboles Deriv : {len(names)} dispo, absents de ma liste : {missing or 'aucun'}")
    except Exception as e:
        print(f"check_symbols ignoré: {e}")


def main():
    cache, state, stats = load(CACHE_FILE), load(STATE_FILE), tracker.load_stats()
    api = Deriv()
    scanned = sent = 0
    conf_tfs = sorted({tf for l in REFERENCE_CONFIRMATION_MAP.values() for tf in l})
    if not (TG_TOKEN and TG_CHAT):
        print("ATTENTION : secret Telegram manquant ou vide (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID)")
    try:
        check_symbols(api)
        for sym in SYMBOLS:
            scanned += 1
            htf = {tf: tf_candles(api, cache, sym, tf) for tf in ("D1", "H4")}
            if not htf["D1"] and not htf["H4"]:
                continue
            ltf_by_tf = {}
            for tf in conf_tfs:
                cs = tf_candles(api, cache, sym, tf)
                if cs and time.time() - cs[-1]["epoch"] <= 3 * GRAN[tf]:  # sinon : marché fermé
                    ltf_by_tf[tf] = cs
            for s in select_best(analyze_variant(sym, htf, ltf_by_tf)):
                k_range, k_content = setup_keys(s)
                legacy = legacy_key(s, k_range)   # ancien format de clé (sans le sens) : évite de renvoyer un setup déjà alerté
                if k_range in state or k_content in state or (legacy and legacy in state):
                    continue
                gate = risk_gate(stats, int(time.time()))
                if gate:
                    day = datetime.now(NY).strftime("%Y%m%d")
                    print(f"{sym} {s['dir']} retenu : {gate[1]}")
                    if f"riskpause_{gate[0]}_{day}" not in state:   # un seul message par motif et par jour
                        state[f"riskpause_{gate[0]}_{day}"] = int(time.time())
                        notify(f"⏸ Alertes CRT suspendues : {gate[1]}.")
                    continue   # pas de verrou : renvoyé quand la limite n'est plus atteinte
                n_corr = correlated_open(stats, sym, s["dir"])
                if n_corr >= MAX_CORRELATED_OPEN:
                    if CORRELATION_MODE == "block":
                        print(f"{sym} {s['dir']} retenu : {n_corr} trades déjà sur le même côté ({exposure_group(sym, s['dir'])[0]})")
                        continue   # pas de verrou : renvoyé quand l'exposition baisse
                    s["corr"] = n_corr
                now = int(time.time())
                state[k_range] = state[k_content] = now
                t = s["trade"]
                # prix à jour pour le type d'ordre (le cache peut dater d'une bougie)
                fresh = tf_candles(api, cache, sym, s["conf_n"], force=True)
                price = fresh[-1]["close"] if fresh else s["price"]
                t["otype"] = order_type(s["dir"], t["entry"], price)
                sent += 1
                notify(crt_message(s, price, decimals(s["ltf"])), html=True)
                tracker.add_pending(stats, k_range, s, now)
        # trades ouverts : avertissement avant une annonce
        t_now = int(time.time())
        for key, tr in stats["pending"].items():
            if not tr["filled"]:
                continue
            ev = news_upcoming(tr["symbol"], t_now, NEWS_WARN_MIN)
            if ev and f"newswarn_{key}_{ev}" not in state:
                state[f"newswarn_{key}_{ev}"] = t_now
                notify(f"⚠️ Annonce dans {max(1, (ev - t_now) // 60)} min : {display_name(tr['symbol'])} {tr['direction']} ouvert. "
                       "Pense à sécuriser (SL au point d'entrée) ou à réduire.")
        # suivi des trades : remplissage, SL / TP2, expiration, purge
        lookup = lambda sym, tf: tf_candles(api, cache, sym, tf)
        done = tracker.resolve_pending(stats, lookup)
        expired, pruned = tracker.expire_stale_pending(stats), tracker.prune_history(stats)
        if done or expired or pruned:
            print(f"Suivi CRT : {len(done)} archivé(s), {expired} expiré(s), {pruned} purgé(s).")
        summary = tracker.summarize(stats)
        if os.getenv("NOTIFY_OK") == "1":
            notify(f"Bot CRT OK : {scanned} symboles scannés, {sent} alerte(s).\n{summary}")
        print(f"Scan terminé : {scanned} symboles, {sent} alerte(s).")
        print(summary)
    finally:
        api.close()
        cut_ts = time.time() - STATE_MAX_AGE_DAYS * 86400
        state = {k: v for k, v in state.items() if isinstance(v, (int, float)) and v > cut_ts}
        save(CACHE_FILE, cache)
        save(STATE_FILE, state)
        tracker.save_stats(stats)


if __name__ == "__main__":
    main()
