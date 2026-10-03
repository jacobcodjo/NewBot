"""Bot Candle Range Trading (CRT) - forex majeurs, or, 4 cryptos.
Actifs : forex majeurs, or, 4 cryptos, Volatility Index 10/25/50/75/100 (+1s)
Cascade ICT : Référence (HTF) -> Manipulation & POI (MTF) -> Confirmation (LTF)
Données : API websocket Deriv | Alertes : Telegram
"""
import json, os, random, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
import websocket  # websocket-client

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
    # Volatility Index (24/7) : 10, 25, 50, 75, 100 et leurs variantes 1 seconde
    "R_10", "R_25", "R_50", "R_75", "R_100",
    "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V",
]
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

    def candles(self, symbol, gran):
        msg = self.request({
            "ticks_history": symbol, "style": "candles", "granularity": gran,
            "count": COUNT, "end": "latest",
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
    return sym.startswith(("cry", "R_", "1HZ"))


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
    if NY_ALIGNED and not is_247(sym) and tf in ("D1", "H4"):
        h1 = get_candles(api, cache, sym, GRAN["H1"])
        return aggregate_ny(h1, 24 if tf == "D1" else 4) if h1 else None
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
        if c["high"] > rng["high"] and rng["low"] <= c["close"] < rng["high"]:
            d, ext, tp = "SELL", c["high"], rng["low"]
            invalid = any(x["close"] > ext for x in closed[i + 1:])
        elif c["low"] < rng["low"] and rng["low"] < c["close"] <= rng["high"]:
            d, ext, tp = "BUY", c["low"], rng["high"]
            invalid = any(x["close"] < ext for x in closed[i + 1:])
        else:
            continue
        if invalid:
            return None
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
    pendant une killzone. Retourne l'époque de la bougie de cassure ou None."""
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
        return m[j]["epoch"]
    return None


def fmt(x):
    return f"{x:.5f}".rstrip("0").rstrip(".")


def code(x):
    """Prix en police à espacement fixe (copiable d'un toucher dans Telegram)."""
    return f"<code>{fmt(x)}</code>"


def notify(text, html=False):
    if not (TG_TOKEN and TG_CHAT):
        print("ATTENTION : TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID vide -> message NON envoyé :")
        print(text); return
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
    except Exception as e:
        print(f"Telegram injoignable: {str(e)[:100]}")


def crt_message(sym, ltf_n, d, session, price, sw, poi):
    r = sw["range"]
    lines = [
        f"{'🟢' if d == 'BUY' else '🔴'} {d} · {display_name(sym)} · {ltf_n}",
        f"Session : {session}", "",
        "────────────────", f"Prix actuel  {code(price)}", "────────────────", "",
        f"SL  {code(sw['sl'])}", f"TP  {code(sw['tp'])}", "",
        f"Range  {code(r['low'])} - {code(r['high'])}",
        f"✅ Sweep  {code(sw['sl'])}",
        f"✅ FVG  {code(poi['fvg'][0])} - {code(poi['fvg'][1])}",
    ]
    if poi["ob"]:
        lines.append(f"✅ OB  {code(poi['ob'][0])} - {code(poi['ob'][1])}")
    return "\n".join(lines)


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


def main():
    cache, state = load(CACHE_FILE), load(STATE_FILE)
    api = Deriv()
    scanned = sent = 0
    if not (TG_TOKEN and TG_CHAT):
        print("ATTENTION : secret Telegram manquant ou vide (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID)")
    try:
        check_symbols(api)
        for sym in SYMBOLS:
            scanned += 1
            for htf_n, mtf_n, ltf_n in CASCADES:
                htf = tf_candles(api, cache, sym, htf_n)
                mtf = tf_candles(api, cache, sym, mtf_n)
                if not htf or not mtf:
                    continue
                sw = find_sweep(htf, mtf)
                if not sw:
                    continue
                sw["gran"] = GRAN[mtf_n]
                poi = find_poi(sw)
                if not poi:
                    continue
                ltf = tf_candles(api, cache, sym, ltf_n, force=True)
                if not ltf or time.time() - ltf[-1]["epoch"] > 3 * GRAN[ltf_n]:
                    continue  # données absentes ou marché fermé
                bos = confirm_ltf(ltf, poi, sw["dir"], sym)
                if bos is None:
                    continue
                key = (f"{sym}|{htf_n}>{mtf_n}>{ltf_n}|{sw['dir']}|"
                       f"{sw['range']['epoch']}|{sw['closed'][sw['i']]['epoch']}")
                if key in state:
                    continue
                state[key] = int(time.time())
                sent += 1
                r = sw["range"]
                notify(crt_message(sym, ltf_n, sw["dir"], killzone(sym, bos),
                                   ltf[-1]["close"], sw, poi), html=True)
        if os.getenv("NOTIFY_OK") == "1":
            notify(f"Bot CRT OK : {scanned} symboles scannés, {sent} alerte(s).")
        print(f"Scan terminé : {scanned} symboles, {sent} alerte(s).")
    finally:
        api.close()
        cut = time.time() - 7 * 86400
        state = {k: v for k, v in state.items() if v > cut}
        save(CACHE_FILE, cache)
        save(STATE_FILE, state)


if __name__ == "__main__":
    main()
