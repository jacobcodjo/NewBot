"""Bot Candle Range Trading (CRT) - stratégie et symboles de l'ancien crt-bot.
Références W1 / D1 / H4 : sweep de liquidité sur un TF de confirmation proportionnel,
cassure de structure + FVG et/ou Order Block. 50 actifs (forex, or, cryptos, Volatility, Step).
Données : API websocket Deriv | Alertes : Telegram
"""
import json, os, random, time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests
import websocket  # websocket-client
import tracker

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
    # Forex majeurs
    "frxEURUSD", "frxGBPUSD", "frxUSDJPY", "frxUSDCHF", "frxAUDUSD", "frxUSDCAD",
    # Forex mineures (paires croisées, sans USD)
    "frxEURGBP", "frxEURJPY", "frxEURCHF", "frxEURAUD", "frxEURCAD", "frxEURNZD",
    "frxGBPJPY", "frxGBPCHF", "frxGBPAUD", "frxGBPCAD", "frxGBPNZD",
    "frxAUDCAD", "frxAUDCHF",
    "frxCADCHF", "frxCADJPY",
    "frxCHFJPY",
    "frxNZDCAD", "frxNZDCHF",
    # Or
    "frxXAUUSD",
    # Cryptos
    "cryBTCUSD", "cryETHUSD", "cryLTCUSD", "cryXRPUSD",
]   # SYMBOLS : actifs du CRT = forex, or et cryptos (les indices synthétiques relèvent de l'Impulsion)

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
NY_ALIGNED = False                 # True : D1/H4 recalculés avec ouverture 17h New York (forex + or)
KILLZONES_NY = [                   # (nom, h début, min début, h fin, min fin) en heure de New York
    ("Londres", 3, 0, 6, 0),
    ("New York", 8, 30, 11, 30),
]
REQUIRE_KILLZONE_FOR_REAL_MARKETS = False  # True : sweep hors killzone ignoré (forex/or seulement)
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


def get_candles(api, cache, symbol, gran, force=False):
    """Cache : on ne re-télécharge que si la dernière bougie est clôturée."""
    key = f"{symbol}_{gran}"
    c = cache.get(key)
    if c and not force and time.time() < c[-1]["epoch"] + gran:
        return c
    fresh = api.candles(symbol, gran)
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
        h1 = get_candles(api, cache, sym, GRAN["H1"])
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
def weekly_range(d1):
    """Range W1 : haut/bas de la dernière semaine ISO complète, agrégé depuis les bougies D1."""
    if not d1 or len(d1) < 2:
        return None
    weeks = {}
    for c in d1[:-1]:  # exclut la bougie du jour en cours
        key = datetime.fromtimestamp(c["epoch"], tz=timezone.utc).isocalendar()[:2]
        weeks.setdefault(key, []).append(c)
    if len(weeks) < 2:
        return None
    wk = weeks[sorted(weeks)[-2]]  # la dernière clé est la semaine en cours (incomplète)
    return {"high": max(c["high"] for c in wk), "low": min(c["low"] for c in wk),
            "epoch": wk[-1]["epoch"]}


def ref_range(ref_n, htf):
    """Range de référence : W1 agrégé, ou dernière bougie D1/H4 clôturée."""
    if ref_n == "W1":
        return weekly_range(htf.get("D1"))
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


def structure_shift(ltf, si, d, w):
    """Après le sweep, une clôture casse le dernier swing dans le sens du retournement."""
    highs, lows = find_pivots(ltf[:si + 1], w, w)
    if d == "SELL":
        if not lows:
            return None
        lvl = ltf[lows[-1]]["low"]
        for j in range(si + 1, len(ltf)):
            if ltf[j]["close"] < lvl:
                return {"j": j, "level": lvl}
    else:
        if not highs:
            return None
        lvl = ltf[highs[-1]]["high"]
        for j in range(si + 1, len(ltf)):
            if ltf[j]["close"] > lvl:
                return {"j": j, "level": lvl}
    return None


def detect_fvg(cs, around, d, window=5):
    """Fair Value Gap proche de l'index donné (haussier si BUY, baissier si SELL)."""
    for i in range(max(1, around - window), min(len(cs) - 1, around + window)):
        if i + 1 >= len(cs):
            break
        c1, c3 = cs[i - 1], cs[i + 1]
        if d == "BUY" and c1["high"] < c3["low"]:
            return {"index": i, "top": c3["low"], "bottom": c1["high"]}
        if d == "SELL" and c1["low"] > c3["high"]:
            return {"index": i, "top": c1["low"], "bottom": c3["high"]}
    return None


def detect_order_block(cs, around, d, window=5):
    """Dernière bougie opposée au mouvement, en remontant depuis l'index donné."""
    for i in range(around, max(0, around - window) - 1, -1):
        c = cs[i]
        if (d == "BUY" and c["close"] < c["open"]) or (d == "SELL" and c["close"] > c["open"]):
            return {"index": i, "high": c["high"], "low": c["low"]}
    return None


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


def setup_keys(s):
    """Clés anti-doublon : un setup par range de référence et par TF de confirmation,
    plus un contrôle sur le contenu (mêmes niveaux = même message)."""
    t = s["trade"]
    return (f"{s['sym']}_{s['ref_n']}_{s['ref_epoch']}_{s['conf_n']}",
            f"content_{s['sym']}_{s['dir']}_{round(t['entry'], 6)}_{round(t['sl'], 6)}_{round(t['tp2'], 6)}")


def analyze(sym, htf, ltf_by_tf):
    """Cherche les setups CRT d'un symbole : sweep -> cassure de structure -> FVG et/ou OB."""
    info = trend_info(htf["D1"]) if htf.get("D1") else {"trend": None, "age": None}
    trend = info["trend"]
    check_gaps = sym.startswith(WEEKEND_GAP_PREFIXES)
    setups = []
    for ref_n, conf_list in REFERENCE_CONFIRMATION_MAP.items():
        rng = ref_range(ref_n, htf)
        if not rng:
            continue
        for conf_n in conf_list:
            full = ltf_by_tf.get(conf_n)
            if not full or len(full) < 20:
                continue
            ltf = full[:-1]  # bougies clôturées uniquement (la dernière est en formation)
            sweeps = detect_sweeps(ltf, rng, GRAN[conf_n], check_gaps)
            if not sweeps:
                continue
            pools = find_liquidity_pools(ltf)
            w = STRUCTURE_SWING_WINDOW.get(conf_n, DEFAULT_STRUCTURE_SWING_WINDOW)
            for s in sweeps:
                d, c = s["dir"], s["candle"]
                st = structure_shift(ltf, s["i"], d, w)
                if not st:
                    continue
                fvg = detect_fvg(ltf, st["j"], d)
                ob = detect_order_block(ltf, s["i"], d)
                if not fvg and not ob:
                    continue  # pas de confirmation avancée
                ext = c["high"] if d == "SELL" else c["low"]
                sw = {"dir": d, "i": s["i"], "sl": ext, "range": rng, "closed": ltf}
                bos = {"j": st["j"], "m": ltf, "epoch": ltf[st["j"]]["epoch"]}
                price = full[-1]["close"]
                t = build_trade(sym, sw, bos, price, pools)
                if t is None or is_stale(d, st["j"], t, full):
                    continue
                session = killzone(sym, c["epoch"])
                if session is None and REQUIRE_KILLZONE_FOR_REAL_MARKETS:
                    continue
                swept_pools = pools[0] if d == "SELL" else pools[1]
                setups.append({
                    "sym": sym, "ref_n": ref_n, "conf_n": conf_n, "ref_epoch": rng["epoch"],
                    "dir": d, "sw": sw, "fvg": fvg, "ob": ob, "trade": t, "ltf": ltf,
                    "price": price, "session": session,
                    "grabbed": near_level(ext, swept_pools),
                    "counter": (trend == "bullish" and d == "SELL") or (trend == "bearish" and d == "BUY"),
                    "trend": trend, "trend_age": info["age"],
                })
    return setups


def entry_zone(m, j, d):
    """Zone d'entrée au niveau de la cassure LTF (bougie j), par priorité :
    Order Block, sinon Breaker Block, sinon FVG (milieu). Retourne (nom, niveau, zone)."""
    bull = d == "BUY"
    opposite = (lambda c: c["close"] <= c["open"]) if bull else (lambda c: c["close"] >= c["open"])
    # 1) Order Block : dernière bougie opposée avant le mouvement qui casse la structure
    x = j - 1
    while x >= 0 and not opposite(m[x]):
        x -= 1
    if x >= 0 and j - x <= OB_LOOKBACK:
        z = (m[x]["low"], m[x]["high"])
        return "Order Block", (z[1] if bull else z[0]), z
    # 2) Breaker Block : dernière bougie dans le sens du trade avant le creux (sommet) de manipulation,
    #    dont la clôture de cassure franchit l'extrême
    seg = range(max(0, j - 12), j)
    if len(seg) >= 3:
        piv = min(seg, key=lambda t: m[t]["low"]) if bull else max(seg, key=lambda t: m[t]["high"])
        y = piv - 1
        while y >= 0 and opposite(m[y]):
            y -= 1
        if y >= 0 and ((m[j]["close"] > m[y]["high"]) if bull else (m[j]["close"] < m[y]["low"])):
            z = (m[y]["low"], m[y]["high"])
            return "Breaker Block", (z[1] if bull else z[0]), z
    # 3) FVG : milieu de l'écart laissé par le mouvement de cassure
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
        z = entry_zone(m, j, d)
        if not z:
            return None  # aucun OB, Breaker ni FVG : setup ignoré
        how, entry = z[0], z[1]
    hi, lo = rng["high"], rng["low"]
    ext = sw["sl"]  # extrême de la bougie de sweep
    buf = ext * STOP_LOSS_BUFFER_PCT / 100
    extra = (hi - lo) * TP_EXTENSION_PCT / 100
    mid = (hi + lo) / 2
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


def crt_message(s, price, dec=None):
    c = lambda v: code(v, dec)
    t, sw, d, r = s["trade"], s["sw"], s["dir"], s["sw"]["range"]
    lines = [
        f"{'🟢' if d == 'BUY' else '🔴'} {t['otype']} · {display_name(s['sym'])} · {s['conf_n']} · CRT",
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
    if s["fvg"]:
        lines.append(f"✅ FVG  {c(s['fvg']['bottom'])} - {c(s['fvg']['top'])}")
    if s["ob"]:
        lines.append(f"✅ OB  {c(s['ob']['low'])} - {c(s['ob']['high'])}")
    if t["ote"]:
        lines.append("✅ OTE (61,8-79 %)")
    if s["grabbed"]:
        lines.append("💧 Pool de liquidité balayé")
    if s["counter"]:
        lines.append("⚠️ Contre-tendance (D1)")
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
            for s in analyze(sym, htf, ltf_by_tf):
                k_range, k_content = setup_keys(s)
                if k_range in state or k_content in state:
                    continue
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
