"""Bot Candle Range Trading (CRT) - forex majeurs, or, 4 cryptos.
Actifs : forex majeurs, or, 4 cryptos (les Volatility Index sont réservés à impulse.py ; CRT_SYNTHETIC=1 les réactive)
Cascade ICT : Référence (HTF) -> Manipulation & POI (MTF) -> Confirmation (LTF)
Données : API websocket Deriv | Alertes : Telegram | Suivi des trades : tracker.py | Garde-fous : guard.py
"""
import json, os, random, sys, time
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
import websocket  # websocket-client

import charts
import guard as G
import tracker as T

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
    "frxEURUSD", "frxGBPUSD", "frxUSDJPY", "frxUSDCHF",
    "frxAUDUSD", "frxUSDCAD", "frxNZDUSD",
    "frxXAUUSD",
    "cryBTCUSD", "cryETHUSD", "cryLTCUSD", "cryXRPUSD",
]
# Volatility Index (24/7) : traités par impulse.py. CRT_SYNTHETIC=1 les ajoute aussi au CRT.
SYNTH_SYMBOLS = ["R_10", "R_25", "R_50", "R_75", "R_100",
                 "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V"]
if os.getenv("CRT_SYNTHETIC") == "1":
    SYMBOLS = SYMBOLS + SYNTH_SYMBOLS
GRAN = {"D1": 86400, "H4": 14400, "H1": 3600, "M15": 900, "M5": 300}
# (Référence HTF, Manipulation & POI MTF, Confirmation LTF)
CASCADES = [("D1", "H4", "H1"), ("H4", "H1", "M15"), ("H1", "M15", "M5")]
FRESH = 2          # la cassure doit dater des 2 dernières bougies LTF clôturées

NY = ZoneInfo("America/New_York")
NY_ALIGNED = True                  # D1/H4 recalculés avec ouverture 17h New York (forex + or)
KILLZONES_NY = [                   # (nom, début, fin) en heure de New York
    ("Asie", 20, 24),
    ("Londres", 2, 5),
    ("New York", 7, 10),
    ("London Close", 10, 12),
]
KILLZONE_24_7 = False              # cryptos + indices synthétiques : pas de filtre de session
SWING_N = 2                        # fractale : 2 bougies de chaque côté
DISP_MULT = 1.2                    # corps de la bougie de cassure vs corps moyen
FVG_DISP_MULT = 1.5                # corps de la bougie centrale du FVG vs corps moyen
COUNT = 200

# --- Entrées / sorties (critères de l'ancien Crt-Bot) ---
STOP_LOSS_BUFFER_PCT = 0.05        # marge minimale du SL au-delà de l'extrême du sweep (% du prix)
SL_BUFFER_ATR = 0.1                # ... ou 0,1 x ATR MTF si plus grand (s'adapte à la volatilité de l'actif)
ATR_N = 14
RANGE_MIN_ATR = 0.5                # range de référence : au moins 0,5 x ATR HTF (écarte les inside bars)
RANGE_MAX_ATR = 3.0                # ... et au plus 3 x ATR HTF (écarte les bougies d'actualité géantes)
VOL_FAST, VOL_SLOW = 14, 100       # régime de volatilité : ATR 14 / ATR 100 du timeframe MTF
VOL_MIN, VOL_MAX = 0.6, 2.0        # ... doit rester entre 0,6 et 2 (écarte compression extrême et pics d'actualité)
EQ_TOL_ATR = 0.15                  # equal highs/lows : deux swings à moins de 0,15 ATR l'un de l'autre
REQUIRE_LIQUIDITY = False          # True : le sweep doit prendre une liquidité (PDH/PDL ou equal highs/lows)
COUNT_H1_NY = 1200                 # H1 téléchargés pour reconstruire D1/H4 en heure de New York (~50 jours)
PENDING_TTL_FAST = 24 * 3600       # validité d'un ordre en attente (LTF M5/M15)
PENDING_TTL_SLOW = 48 * 3600       # ... (LTF H1)
TP_EXTENSION_PCT = 50               # TP3 : bord opposé du range + 50 % de sa taille (tous les actifs)
MIN_RISK_REWARD = 3.0              # ratio minimum (1 pour 3), calculé sur TP2
ORDER_TYPE_TOLERANCE_PCT = 0.02    # écart (% du prix) sous lequel le prix est "déjà sur la zone" -> ordre au marché
OB_LOOKBACK = 8                    # l'OB doit se trouver à 8 bougies LTF au plus avant la cassure
REQUIRE_FIB_OTE = False            # True : l'entrée doit tomber dans l'OTE 61,8-79 % (sinon simple indication)

MIN_INTERVAL = 0.6      # s entre deux requêtes (throttle)
MAX_RETRIES = 6
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
    key = f"{symbol}_{gran}"
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
    return sym.startswith(("cry", "R_", "1HZ"))


def is_synthetic(sym):
    return sym.startswith(("R_", "1HZ", "stp"))


def display_name(sym):
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
    if NY_ALIGNED and not is_247(sym) and tf in ("D1", "H4", "H1"):
        # tous les accès H1 de ces actifs utilisent le même historique long (cache cohérent)
        h1 = get_candles(api, cache, sym, GRAN["H1"], force=force and tf == "H1", count=COUNT_H1_NY)
        if not h1 or tf == "H1":
            return h1
        return aggregate_ny(h1, 24 if tf == "D1" else 4)
    return get_candles(api, cache, sym, GRAN[tf], force=force)


def killzone(sym, epoch):
    """Nom de la session si dans une killzone, 'N/A' pour crypto sans filtre, sinon None."""
    if is_247(sym) and not KILLZONE_24_7:
        return "24/7"
    hour = datetime.fromtimestamp(epoch, NY).hour
    for name, a, b in KILLZONES_NY:
        if a <= hour < b:
            return name
    return None


# --------------------------------------------------------------------------
# Logique CRT en cascade
# --------------------------------------------------------------------------
def body_avg(cs):
    cs = [abs(c["close"] - c["open"]) for c in cs]
    return sum(cs) / len(cs) if cs else 0


def body(c):
    return abs(c["close"] - c["open"])


def atr(cs, n=ATR_N):
    cs = cs[-(n + 1):]
    trs = [max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"]))
           for p, c in zip(cs, cs[1:])]
    return sum(trs) / len(trs) if trs else 0


def find_sweep(htf, mtf):
    """Range = dernière bougie HTF clôturée. Sweep = bougie MTF (de la bougie
    HTF en cours) qui dépasse le range puis referme dedans."""
    if len(htf) < 3 or len(mtf) < 4:
        return None
    rng, cur = htf[-2], htf[-1]
    closed = mtf[:-1]
    for i, c in enumerate(closed):
        if c["epoch"] < cur["epoch"]:
            continue
        up, dn = c["high"] > rng["high"], c["low"] < rng["low"]
        if up and dn:
            continue  # bougie externe : les deux côtés du range sont pris, sens ambigu
        if up and rng["low"] <= c["close"] < rng["high"]:
            d, ext, tp = "SELL", c["high"], rng["low"]
            invalid = any(x["close"] > ext for x in closed[i + 1:])
        elif dn and rng["low"] < c["close"] <= rng["high"]:
            d, ext, tp = "BUY", c["low"], rng["high"]
            invalid = any(x["close"] < ext for x in closed[i + 1:])
        else:
            continue
        if invalid:
            continue  # ce sweep est invalidé : on cherche un sweep ultérieur (autre côté ou nouvelle tentative)
        return {"dir": d, "i": i, "sl": ext, "tp": tp, "range": rng, "closed": closed}
    return None


def in_zone_side(lo, hi, rng, direction):
    """Premium (moitié haute) pour SELL, Discount (moitié basse) pour BUY."""
    eq = (rng["high"] + rng["low"]) / 2
    mid = (lo + hi) / 2
    return mid >= eq if direction == "SELL" else mid <= eq


def find_poi(sw):
    """POI (MTF) formé après le sweep : FVG avec déplacement (+ OB), en premium/discount."""
    m, i, d, rng = sw["closed"], sw["i"], sw["dir"], sw["range"]
    for k in range(i + 2, len(m)):
        a, mid_c, c = m[k - 2], m[k - 1], m[k]
        if d == "SELL" and a["low"] > c["high"] and mid_c["close"] < mid_c["open"]:
            fvg = (c["high"], a["low"])
        elif d == "BUY" and a["high"] < c["low"] and mid_c["close"] > mid_c["open"]:
            fvg = (a["high"], c["low"])
        else:
            continue
        # déplacement : bougie centrale nettement plus grosse que la moyenne
        avg = body_avg(m[max(0, k - 11):k - 1])
        if avg and abs(mid_c["close"] - mid_c["open"]) < FVG_DISP_MULT * avg:
            continue
        if not in_zone_side(fvg[0], fvg[1], rng, d):
            continue
        ob = None  # dernière bougie opposée avant le déplacement (hors bougie de sweep)
        for j in range(k - 2, i, -1):
            x = m[j]
            if (d == "SELL" and x["close"] > x["open"]) or \
               (d == "BUY" and x["close"] < x["open"]):
                if in_zone_side(x["low"], x["high"], rng, d):
                    ob = (x["low"], x["high"])
                break
        return {"fvg": fvg, "ob": ob, "ready_at": c["epoch"] + sw["gran"]}
    return None


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


def confirm_ltf(ltf, poi, direction, sym):
    """Touche du POI, puis MSS : clôture au-delà du dernier swing, avec déplacement,
    pendant une killzone. Retourne {epoch, j, m} de la bougie de cassure ou None."""
    m = ltf[:-1]
    zones = [z for z in (poi["fvg"], poi["ob"]) if z]
    touch = None
    for t, c in enumerate(m):
        if c["epoch"] < poi["ready_at"]:
            continue
        if any(c["low"] <= hi and c["high"] >= lo for lo, hi in zones):
            touch = t; break
    if touch is None:
        return None  # POI jamais touché -> setup rejeté
    kind = "low" if direction == "SELL" else "high"
    for j in range(max(touch + 1, len(m) - FRESH), len(m)):
        cand = [s for s in swings(m, kind, j - 1) if s >= touch - 5]
        if not cand:
            continue
        level = m[cand[-1]][kind]
        brk = (m[j]["close"] < level and m[j - 1]["close"] >= level) if direction == "SELL" \
            else (m[j]["close"] > level and m[j - 1]["close"] <= level)
        if not brk:
            continue
        avg = body_avg(m[max(0, j - 8):j])
        if avg and abs(m[j]["close"] - m[j]["open"]) < DISP_MULT * avg:
            continue
        if killzone(sym, m[j]["epoch"]) is None:
            continue
        return {"epoch": m[j]["epoch"], "j": j, "m": m}
    return None


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


def build_trade(sym, sw, bos, price):
    """Entrée, SL, TP1-TP3, type d'ordre et R:R. Retourne None si le setup est ignoré."""
    d, rng, m, j = sw["dir"], sw["range"], bos["m"], bos["j"]
    buy, synth = d == "BUY", is_synthetic(sym)
    zone = None
    if synth:   # indices synthétiques : entrée immédiate à la clôture de la bougie de cassure
        entry, how = m[j]["close"], "clôture de la cassure"
    else:
        z = entry_zone(m, j, d)
        if not z:
            return None  # aucun OB, Breaker ni FVG : setup ignoré
        how, entry, zone = z[0], z[1], z[2]
    hi, lo = rng["high"], rng["low"]
    ext = sw["sl"]  # extrême de la bougie de sweep (MTF)
    buf = max(ext * STOP_LOSS_BUFFER_PCT / 100, SL_BUFFER_ATR * sw.get("atr", 0))
    extra = (hi - lo) * TP_EXTENSION_PCT / 100
    mid = (hi + lo) / 2
    if buy:
        sl, tp1, tp2, tp3 = ext - buf, mid, hi, hi + extra
        ok = sl < entry < tp2
    else:
        sl, tp1, tp2, tp3 = ext + buf, mid, lo, lo - extra
        ok = sl > entry > tp2
    if not ok:
        return None
    rr = abs(tp2 - entry) / abs(entry - sl)  # ratio calculé sur TP2
    ote = in_ote(sw, m, entry, d)
    if rr < MIN_RISK_REWARD or (REQUIRE_FIB_OTE and not ote):
        return None
    inval = None if zone is None else (zone[0] if buy else zone[1])   # bord de la zone d'entrée côté stop
    return {"otype": order_type(d, entry, price), "entry": entry, "how": how, "sl": sl,
            "tp1": tp1, "tp2": tp2, "tp3": tp3, "rr": rr, "ote": ote, "zone": zone, "inval": inval}


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


def tg_api(method, payload=None, files=None):
    """Appel à l'API Telegram ; retourne le JSON de la réponse ou None en cas d'échec."""
    if not (TG_TOKEN and TG_CHAT):
        return None
    try:
        if files:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=payload, files=files, timeout=30)
        else:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", json=payload, timeout=15)
        if not r.ok:
            print(f"Telegram {method} erreur {r.status_code}: {r.text[:150]}")
            return None
        return r.json()
    except Exception as e:
        print(f"Telegram injoignable ({method}): {str(e)[:100]}")
        return None


def notify(text, html=False, buttons=None):
    """Envoie un message ; retourne son identifiant (None si l'envoi échoue ou sans secrets Telegram)."""
    if not (TG_TOKEN and TG_CHAT):
        print("ATTENTION : TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID vide -> message NON envoyé :")
        print(text)
        return None
    payload = {"chat_id": TG_CHAT, "text": text}
    if html:
        payload["parse_mode"] = "HTML"
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    r = tg_api("sendMessage", payload)
    return r["result"]["message_id"] if r and r.get("ok") else None


def notify_photo(path, caption, html=True, buttons=None):
    """Envoie une image avec légende (1024 caractères au plus) ; retourne l'identifiant du message ou None."""
    if not (TG_TOKEN and TG_CHAT):
        return None
    payload = {"chat_id": TG_CHAT, "caption": caption}
    if html:
        payload["parse_mode"] = "HTML"
    if buttons:
        payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    with open(path, "rb") as f:
        r = tg_api("sendPhoto", payload, files={"photo": f})
    return r["result"]["message_id"] if r and r.get("ok") else None


def ratio_txt(d, entry, sl, tp):
    """Ratio gain/risque d'une cible, ex. '   (1:4.9)'."""
    risk = abs(entry - sl)
    gain = (tp - entry) if d == "BUY" else (entry - tp)
    if risk <= 0:
        return ""
    return "   (dépassé)" if gain <= 0 else f"   (1:{gain / risk:.1f})"


def mgmt_line():
    """Règle de gestion annoncée dans l'alerte (reflète les réglages de tracker.py)."""
    p1, p2 = T.PARTIAL_AT_TP1, T.PARTIAL_AT_TP2
    parts = []
    if p1:
        parts.append(f"{int(p1 * 100)} % à TP1" + (" (SL à l'entrée)" if T.BE_AT_TP1 else ""))
    if p2:
        parts.append(f"{int(p2 * 100)} % à TP2" + (" (SL à TP1)" if T.LOCK_AT_TP2 else ""))
    rest = round((1 - p1 - p2) * 100)
    if rest > 0:
        parts.append(f"{rest} % vers " + ("TP3" if p2 else "TP2"))
    if not parts:
        return None
    out = "Gestion : " + ", ".join(parts)
    exits = (["structure cassée"] if T.STRUCT_EXIT else []) + \
            ([f"TP1 non atteint en {T.TIME_STOP_BARS} bougies"] if T.TIME_STOP_BARS else [])
    return out + (". Sortie si " + " ou ".join(exits) if exits else "")


def crt_message(sym, ltf_n, session, price, sw, poi, t, dec=None, liq=None):
    c = lambda v: code(v, dec)
    d, r = sw["dir"], sw["range"]
    lines = [
        f"{'🟢' if d == 'BUY' else '🔴'} {t['otype']} · {display_name(sym)} · {ltf_n} · CRT",
        f"Session : {session}", "",
        "────────────────", f"Prix actuel  {c(price)}", "────────────────", "",
        f"Entrée  {c(t['entry'])}  ({t['how']})",
        f"SL  {c(t['sl'])}",
        f"TP1  {c(t['tp1'])}{ratio_txt(d, t['entry'], t['sl'], t['tp1'])}",
        f"TP2  {c(t['tp2'])}{ratio_txt(d, t['entry'], t['sl'], t['tp2'])}",
        f"TP3  {c(t['tp3'])}{ratio_txt(d, t['entry'], t['sl'], t['tp3'])}", "",
        f"Range  {c(r['low'])} - {c(r['high'])}",
        f"✅ Sweep  {c(sw['sl'])}",
        f"✅ FVG  {c(poi['fvg'][0])} - {c(poi['fvg'][1])}",
    ]
    if poi["ob"]:
        lines.append(f"✅ OB  {c(poi['ob'][0])} - {c(poi['ob'][1])}")
    if t["ote"]:
        lines.append("✅ OTE (61,8-79 %)")
    if liq and liq["swept"]:
        lines.append(f"✅ Liquidité prise : {', '.join(liq['swept'])}")
    if liq and liq.get("opp") is not None:
        lines.append(f"Liquidité opposée  {c(liq['opp'])}")
    if T.STRUCT_EXIT and t.get("inval") is not None:
        lines.append(f"Invalidation  {c(t['inval'])}  (clôture au-delà = sortie)")
    extra = [x for x in (G.lot_line(sym, t["entry"], t["sl"]), mgmt_line()) if x]
    if extra:
        lines += [""] + extra
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Suivi des trades (partagé avec impulse.py)
# --------------------------------------------------------------------------
def trade_event_message(t, e):
    c = lambda v: code(v, t.get("dec"))
    head = f"{t['name']} · {t['ltf']} · {t['bot']}" + (f" #{t['id']}" if t.get("id") else "")
    closed = t["status"] == "closed" and t.get("r") is not None
    res = f"\nRésultat  {t['r']:+.1f} R" if closed else ""
    if e == "filled":
        return f"▶️ Ordre touché · {t['otype']} · {head}\nEntrée  {c(t['entry'])}"
    if e == "tp1":
        sl_txt = f"SL à l'entrée {c(t['entry'])}" if T.BE_AT_TP1 else "SL inchangé"
        return (f"🎯 TP1 atteint · {head}\nClôture de {int(T.PARTIAL_AT_TP1 * 100)} %, {sl_txt}\n"
                f"TP1  {c(t['tp1'])}")
    if e == "tp2":
        if closed:
            return f"✅ TP2 atteint · {head}{res}"
        lock = f"SL remonté à {c(t['sl_now'])}" if T.LOCK_AT_TP2 else "SL inchangé"
        return (f"🎯 TP2 atteint · {head}\nClôture de {int(T.PARTIAL_AT_TP2 * 100)} %, {lock}, le reste vise TP3\n"
                f"TP2  {c(t['tp2'])}")
    if e == "tp3":
        return f"🏁 TP3 atteint · {head}{res}"
    if e == "sl":
        return f"❌ Stop touché · {head}{res}"
    if e == "be":
        return f"➖ Stop à l'entrée touché après TP1 · {head}{res}"
    if e == "lock":
        return f"🔒 Stop remonté touché après TP2 · {head}{res}"
    if e == "struct":
        return (f"🔻 Structure cassée · {head}\nUne bougie a clôturé au-delà de {c(t['inval'])}. "
                f"Ferme la position.{res}")
    if e == "time":
        return (f"⏱ Sortie au temps · {head}\nTP1 non atteint en {T.TIME_STOP_BARS} bougies. "
                f"Ferme la position.{res}")
    why = {"cancel_tp": "TP1 atteint sans exécution",
           "cancel_sl": "stop-loss franchi avant exécution",
           "expired": f"ordre expiré ({t['ttl'] // 3600} h sans exécution)"}[e]
    return f"⚠️ ANNULER · {t['otype']} · {head}\n{why}\nEntrée prévue  {c(t['entry'])}"


def track_trades(api, tr, bot, now):
    """Met à jour les trades du bot : exécution, TP1 à TP3, stop, sorties, annulation, expiration."""
    for key, t in list(tr["active"].items()):
        if t["bot"] != bot:
            continue
        since = max(t["seen"], t["t0"] // t["gran"] * t["gran"])
        need = min(5000, max(10, int((now - since) / t["gran"]) + 3))
        try:
            cs = api.candles(t["sym"], t["gran"], count=need)
        except Exception as ex:
            print(f"Suivi {t['name']} ignoré : {str(ex)[:80]}")
            continue
        if not cs:
            continue
        for e in T.advance(t, cs, now):
            notify(trade_event_message(t, e), html=True)
        if t["status"] not in T.ACTIVE:
            T.archive(tr, key)


def emit_alert(a, tr, g, sent, funnel, now, message):
    """Contrôles de risque, envoi de l'alerte (graphique + boutons Pris / Ignoré), puis enregistrement
    du trade pour le suivi."""
    sent[a["key"]] = int(now)   # un setup bloqué ou alerté n'est jamais réexaminé
    ok, why = G.risk_check(tr, a["sym"], a["dir"], now)
    if not ok:
        if why == "dedupe":
            funnel["dedupe"] += 1
        else:
            funnel["risk"] += 1
            print(f"Alerte bloquée {a['name']} {a['dir']} : {why}")
            ds = T.day_start(now)
            if why.startswith("perte journalière") and g.get("paused_since") != ds:
                g["paused_since"] = ds
                notify(f"⏸ Alertes en pause : {why}. Reprise à la prochaine journée de trading (17h New York).")
        return False
    funnel["alert"] += 1
    tid = g["next_id"] = g.get("next_id", 0) + 1
    text = f"{message}\n\nRéf. #{tid}"
    buttons = [[{"text": "✅ Pris", "callback_data": f"take:{tid}"},
                {"text": "❌ Ignoré", "callback_data": f"skip:{tid}"}]]
    delivered = False
    if a.get("chart") and os.getenv("CHARTS", "1") != "0" and TG_TOKEN and TG_CHAT:
        path = f"chart_{tid}.png"
        try:
            charts.render(a["chart"], path)
            if len(text) <= 1000:
                delivered = notify_photo(path, text, buttons=buttons) is not None
            else:   # légende trop longue : image avec titre, puis le texte complet avec les boutons
                delivered = notify_photo(path, text.split("\n")[0]) is not None
                if delivered:
                    notify(text, html=True, buttons=buttons)
        except Exception as ex:
            print(f"Graphique ignoré : {str(ex)[:100]}")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    if not delivered:
        notify(text, html=True, buttons=buttons)
    tr["active"][a["key"]] = T.new_trade(
        a["bot"], a["key"], a["sym"], a["name"], a["dir"], a["otype"], a["entry"], a["sl"],
        a["tp1"], a["tp2"], a["tp3"], a["ltf"], a["gran"], a["dec"], a["cascade"], now, a["ttl"],
        extra={"age": a.get("age"), "session": a.get("session"), "liq": a.get("liq"), "id": tid},
        inval=a.get("inval"), bar_secs=a["gran"])
    return True


# --------------------------------------------------------------------------
# Interactions Telegram : boutons « Pris / Ignoré » et commandes
# --------------------------------------------------------------------------
HELP = ("Commandes :\n/pris N : le trade #N est pris\n/ignore N : le trade #N est ignoré\n"
        "/r N X : ton résultat réel du trade #N, en R (ex. /r 12 -0.8)\n/bilan : bilan des 30 derniers jours")


def _set_taken(tr, tid, taken):
    t = T.find_trade(tr, tid)
    if t is None:
        return None
    t["taken"] = taken
    return t


def handle_update(u, g, tr, now):
    cq, msg = u.get("callback_query"), u.get("message")
    chat = str((cq or {}).get("message", {}).get("chat", {}).get("id") if cq else (msg or {}).get("chat", {}).get("id"))
    if chat != str(TG_CHAT):
        return  # on n'écoute que ton chat
    if cq:
        action, _, num = (cq.get("data") or "").partition(":")
        if action in ("take", "skip") and num.isdigit():
            t = _set_taken(tr, int(num), action == "take")
            label = "✅ Pris · noté" if action == "take" else "❌ Ignoré · noté"
            tg_api("answerCallbackQuery", {"callback_query_id": cq["id"],
                                           "text": label if t else "Trade introuvable (trop ancien ?)"})
            if t:
                tg_api("editMessageReplyMarkup", {
                    "chat_id": TG_CHAT, "message_id": cq["message"]["message_id"],
                    "reply_markup": {"inline_keyboard": [[{"text": label, "callback_data": "noop"}]]}})
        else:
            tg_api("answerCallbackQuery", {"callback_query_id": cq["id"]})
        return
    parts = (msg.get("text") or "").strip().split()
    if not parts or not parts[0].startswith("/"):
        return
    cmd, args = parts[0].split("@")[0].lower(), parts[1:]
    if cmd in ("/pris", "/ignore") and args and args[0].lstrip("#").isdigit():
        t = _set_taken(tr, int(args[0].lstrip("#")), cmd == "/pris")
        notify(f"Trade #{args[0].lstrip('#')} noté « {'pris' if cmd == '/pris' else 'ignoré'} »." if t
               else "Trade introuvable (trop ancien ?).")
    elif cmd == "/r" and len(args) == 2 and args[0].lstrip("#").isdigit():
        try:
            val = float(args[1].replace(",", "."))
        except ValueError:
            notify("Format : /r N X, par exemple /r 12 -0.8")
            return
        t = _set_taken(tr, int(args[0].lstrip("#")), True)
        if t:
            t["own_r"] = val
            notify(f"Trade #{args[0].lstrip('#')} : ton résultat réel {val:+.2f} R est enregistré.")
        else:
            notify("Trade introuvable (trop ancien ?).")
    elif cmd == "/bilan":
        notify(T.stats_text(tr, now, days=30))
    else:
        notify(HELP)


def poll_telegram(g, tr, now):
    """Lit les boutons et commandes envoyés depuis le dernier scan (réponse sous 5 minutes)."""
    if not (TG_TOKEN and TG_CHAT):
        return
    r = tg_api("getUpdates", {"offset": g.get("tg_offset", 0), "timeout": 0,
                              "allowed_updates": ["callback_query", "message"]})
    if not r or not r.get("ok"):
        return
    for u in r.get("result", []):
        g["tg_offset"] = u["update_id"] + 1
        try:
            handle_update(u, g, tr, now)
        except Exception as ex:
            print(f"Message Telegram ignoré : {str(ex)[:100]}")


def check_symbols(api):
    """Journalise les symboles absents chez Deriv (diagnostic uniquement)."""
    try:
        msg = api.request({"active_symbols": "brief"})
        names = {x.get("symbol") or x.get("underlying_symbol")
                 for x in (msg or {}).get("active_symbols", [])}
        if names:
            missing = [s for s in SYMBOLS if s not in names]
            print(f"Symboles Deriv : {len(names)} dispo, absents de ma liste : {missing or 'aucun'}")
    except Exception as e:
        print(f"check_symbols ignoré: {e}")


def vol_regime_ok(mtf):
    """Régime de volatilité : ATR rapide / ATR lent du MTF entre VOL_MIN et VOL_MAX.
    Sans assez d'historique (moins de VOL_SLOW + 1 bougies clôturées), le filtre ne s'applique pas."""
    cs = mtf[:-1]
    if len(cs) < VOL_SLOW + 1:
        return True
    slow = atr(cs, VOL_SLOW)
    return slow <= 0 or VOL_MIN <= atr(cs, VOL_FAST) / slow <= VOL_MAX


def find_liquidity(sw, provide):
    """Liquidités prises par la bougie de sweep : plus haut/bas de la veille (PDH/PDL) et equal highs/lows
    du MTF. Retourne aussi la liquidité opposée (PDL pour une vente, PDH pour un achat)."""
    d, ext, closed, i, a = sw["dir"], sw["sl"], sw["closed"], sw["i"], sw.get("atr", 0)
    swept, opp = [], None
    d1 = provide("D1")
    if d1 and len(d1) >= 3:
        prev = d1[-2]
        if d == "SELL" and ext > prev["high"]:
            swept.append("PDH")
        if d == "BUY" and ext < prev["low"]:
            swept.append("PDL")
        opp = prev["low"] if d == "SELL" else prev["high"]
    pre, kind = closed[:i], "high" if d == "SELL" else "low"
    if len(pre) >= 8 and a > 0:
        lv = [pre[x][kind] for x in swings(pre, kind, len(pre) - 1)][-6:]
        for x in range(len(lv)):
            hit = next((y for y in range(x + 1, len(lv)) if abs(lv[x] - lv[y]) <= EQ_TOL_ATR * a), None)
            if hit is not None and ((ext > max(lv[x], lv[hit])) if d == "SELL" else (ext < min(lv[x], lv[hit]))):
                swept.append("EQH" if d == "SELL" else "EQL")
                break
    return {"swept": swept, "opp": opp}


def scan_symbol(sym, provide, now, sent, funnel, news=(), align=False):
    """Analyse un actif sur toutes les cascades. provide(tf, force=False) renvoie les bougies
    (la dernière est en formation) ou None. Retourne les alertes candidates, D1 en premier.
    align=True (backtest) : une cascade n'est évaluée que quand sa bougie LTF vient de clôturer."""
    out = []
    for htf_n, mtf_n, ltf_n in CASCADES:
        if align and now % GRAN[ltf_n]:
            continue
        funnel["cascades"] += 1
        htf, mtf = provide(htf_n), provide(mtf_n)
        if not htf or not mtf:
            continue
        funnel["data"] += 1
        if not vol_regime_ok(mtf):
            continue  # volatilité en compression extrême ou en pic : on ne trade pas ce régime
        funnel["regime"] += 1
        sw = find_sweep(htf, mtf)
        if not sw:
            continue
        funnel["sweep"] += 1
        a_htf, size = atr(htf[:-2]), sw["range"]["high"] - sw["range"]["low"]
        if a_htf > 0 and not (RANGE_MIN_ATR * a_htf <= size <= RANGE_MAX_ATR * a_htf):
            continue  # range trop petit (inside bar) ou trop grand (bougie d'actualité)
        funnel["range"] += 1
        sw["gran"], sw["atr"] = GRAN[mtf_n], atr(sw["closed"])
        poi = find_poi(sw)
        if not poi:
            continue
        funnel["poi"] += 1
        ltf = provide(ltf_n, True)
        if not ltf or now - ltf[-1]["epoch"] > 3 * GRAN[ltf_n]:
            continue  # données absentes ou marché fermé
        bos = confirm_ltf(ltf, poi, sw["dir"], sym)
        if bos is None:
            continue
        funnel["mss"] += 1
        price = ltf[-1]["close"]
        trade = build_trade(sym, sw, bos, price)
        if trade is None:
            continue
        funnel["trade"] += 1
        key = (f"{sym}|{htf_n}>{mtf_n}>{ltf_n}|{sw['dir']}|"
               f"{sw['range']['epoch']}|{sw['closed'][sw['i']]['epoch']}")
        if key in sent:
            continue
        why = G.blackout(sym, now, news)
        if why:
            funnel["blackout"] += 1
            sent[key] = int(now)
            print(f"Alerte bloquée {display_name(sym)} {sw['dir']} : {why}")
            continue
        liq = find_liquidity(sw, provide)
        if REQUIRE_LIQUIDITY and not liq["swept"]:
            funnel["liquidity"] += 1
            sent[key] = int(now)
            continue
        dec = decimals(ltf)
        session = killzone(sym, bos["epoch"])
        rg = sw["range"]
        lines = [{"y": trade["entry"], "label": "Entrée", "kind": "entry"}, {"y": trade["sl"], "label": "SL", "kind": "sl"},
                 {"y": trade["tp2"], "label": "TP2", "kind": "tp"},
                 {"y": trade["tp3"], "label": "TP3", "kind": "tp"}, {"y": sw["sl"], "label": "Sweep", "kind": "sweep"},
                 {"y": rg["high"], "label": "Range H", "kind": "range"}, {"y": rg["low"], "label": "Range B", "kind": "range"}]
        if (trade["tp1"] > trade["entry"]) == (sw["dir"] == "BUY"):   # TP1 encore devant l'entrée
            lines.append({"y": trade["tp1"], "label": "TP1", "kind": "tp"})
        zones = [{"lo": poi["fvg"][0], "hi": poi["fvg"][1], "label": "FVG", "kind": "fvg"}]
        if poi["ob"]:
            zones.append({"lo": poi["ob"][0], "hi": poi["ob"][1], "label": "OB", "kind": "ob"})
        if trade["zone"]:
            zones.append({"lo": trade["zone"][0], "hi": trade["zone"][1], "label": trade["how"], "kind": "entry"})
        chart = {"title": f"{display_name(sym)} · {trade['otype']} · {ltf_n} · CRT", "dec": dec,
                 "candles": charts.candles_slice(ltf), "lines": lines, "zones": zones}
        out.append({
            "bot": "CRT", "key": key, "cascade": f"{htf_n}>{mtf_n}>{ltf_n}", "sym": sym,
            "name": display_name(sym), "dir": sw["dir"], "ltf": ltf_n, "gran": GRAN[ltf_n],
            "dec": dec, "price": price, "otype": trade["otype"], "entry": trade["entry"],
            "sl": trade["sl"], "tp1": trade["tp1"], "tp2": trade["tp2"], "tp3": trade["tp3"],
            "rr": trade["rr"], "session": session, "age": None, "inval": trade["inval"],
            "liq": "+".join(liq["swept"]) or "aucune", "chart": chart,
            "ttl": PENDING_TTL_FAST if ltf_n in ("M5", "M15") else PENDING_TTL_SLOW,
            "message": crt_message(sym, ltf_n, session, price, sw, poi, trade, dec, liq)})
    return out


def finish_run(bot, g, tr, funnel, now):
    """Santé du bot, entonnoir cumulé et bilan hebdomadaire (le bilan part avec le CRT)."""
    G.health_end(g, bot, funnel["data"] > 0, now, notify)
    G.funnel_add(g, bot, funnel)
    if not g["last_report"]:
        g["last_report"] = int(now)
    elif bot == "CRT" and G.report_due(g, now):
        notify(G.build_report(g, tr, now))
        G.report_sent(g, now)


def print_stats():
    tr, g = T.load_trades(), G.load_guard()
    print(T.stats_text(tr, time.time(), days=30))
    for bot, counts in sorted(g["funnel"].items()):
        print(f"Entonnoir {bot} : {G.funnel_text(counts)}")


def main():
    if "--stats" in sys.argv:
        print_stats()
        return
    now = time.time()
    cache, state = load(CACHE_FILE), load(STATE_FILE)
    tr, g = T.load_trades(), G.load_guard()
    news = G.load_news() + G.calendar_events(g, now)
    api, funnel = Deriv(), Counter()
    scanned = 0
    if not (TG_TOKEN and TG_CHAT):
        print("ATTENTION : secret Telegram manquant ou vide (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID)")
    try:
        G.health_start(g, "CRT", now, notify)
        G.fomc_check(g, now, notify)
        poll_telegram(g, tr, now)
        check_symbols(api)
        track_trades(api, tr, "CRT", now)
        for sym in SYMBOLS:
            scanned += 1
            provide = lambda tf, force=False, s=sym: tf_candles(api, cache, s, tf, force)
            for a in scan_symbol(sym, provide, now, state, funnel, news):
                emit_alert(a, tr, g, state, funnel, now, a["message"])
        finish_run("CRT", g, tr, funnel, now)
        if os.getenv("NOTIFY_OK") == "1":
            notify(f"Bot CRT OK : {scanned} symboles scannés, {funnel['alert']} alerte(s).\n"
                   f"Entonnoir : {G.funnel_text(funnel)}\n{T.stats_text(tr, now)}")
        print(f"Scan terminé : {scanned} symboles, {funnel['alert']} alerte(s). "
              f"Entonnoir : {G.funnel_text(funnel)}")
    finally:
        api.close()
        cut = time.time() - 7 * 86400
        state = {k: v for k, v in state.items() if v > cut}
        save(CACHE_FILE, cache)
        save(STATE_FILE, state)
        T.save_trades(tr)
        G.save_guard(g)


if __name__ == "__main__":
    main()
