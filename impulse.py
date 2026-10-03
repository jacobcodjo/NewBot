"""Stratégie Impulsion + retracement Fibonacci (zone 50 %-79 %) - Volatility Index Deriv.
Cascade identique au CRT : (D1,H4,H1) (H4,H1,M15) (H1,M15,M5)
  HTF : tendance (swings) | MTF : impulsion + Fibonacci | LTF : sweep -> MSS + FVG -> OB (zone 50 %-79 %)
Le bot n'exécute aucun ordre : il envoie des alertes Telegram (ordre limite ou entrée immédiate).
Les calculs sont écrits pour un achat ; pour une vente, les bougies sont inversées (prix * -1).
"""
import os, time
from bot import (Deriv, get_candles, load, save, notify, body_avg, swings,
                 display_name, fmt, code, decimals, GRAN, CASCADES, CACHE_FILE)

SYMBOLS = ["R_10", "R_25", "R_50", "R_75", "R_100",
           "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V"]
STATE_FILE = "impulse_state.json"

ZONE = (0.5, 0.79)          # zone de retracement acceptée : 50 % -> 79 %
GOLDEN_ZONE = (0.5, 0.618)  # étiquette "Golden Zone"
OTE = (0.618, 0.79)         # étiquette "OTE"
EXT = (1.272, 1.618)        # extensions pour TP2 / TP3
IMPULSE_ATR = 2.0           # jambe d'impulsion >= 2 ATR
IMPULSE_DISP = 1.5          # une bougie de la jambe >= 1,5 x corps moyen
DISP = 1.2                  # déplacement de la bougie de cassure LTF
ATR_N = 14
SL_BUFFER_ATR = 0.15        # tampon du stop-loss (x ATR LTF)
MIN_RR = 3.0                # ratio gain/risque minimum (1 pour 3)
FRESH = 2                   # la cassure LTF doit dater des 2 dernières bougies clôturées
MAX_PROGRESS = 0.5          # ordre limite rejeté si le prix a déjà fait plus de 50 % du chemin entrée -> TP1
LATE_CANDLES = 2            # entrée tardive : le prix a quitté l'OB depuis 2 bougies au plus
LATE_DIST = 0.5             # ... et se trouve à moins d'une demi-hauteur d'OB de son bord
LATE_PROGRESS = 0.25        # ... et a fait moins de 25 % du chemin bord de l'OB -> TP1
PENDING_TTL = 24 * 3600     # un ordre limite en attente expire après 24 h


def flip(cs):
    return [{"epoch": c["epoch"], "open": -c["open"], "high": -c["low"],
             "low": -c["high"], "close": -c["close"]} for c in cs]


def atr(cs, n=ATR_N):
    cs = cs[-(n + 1):]
    trs = [max(c["high"] - c["low"], abs(c["high"] - p["close"]), abs(c["low"] - p["close"]))
           for p, c in zip(cs, cs[1:])]
    return sum(trs) / len(trs) if trs else 0


def body(c):
    return abs(c["close"] - c["open"])


# --------------------------------------------------------------------------
# 1) Tendance HTF : sommets et creux de plus en plus hauts
# --------------------------------------------------------------------------
def trend_up(htf):
    h = htf[:-1]
    if len(h) < 12:
        return False
    sh, sl = swings(h, "high", len(h) - 1), swings(h, "low", len(h) - 1)
    if len(sh) < 2 or len(sl) < 2:
        return False
    return h[sh[-1]]["high"] > h[sh[-2]]["high"] and h[sl[-1]]["low"] > h[sl[-2]]["low"]


# --------------------------------------------------------------------------
# 2) Impulsion MTF : cassure de structure avec déplacement (jambe >= 2 ATR)
# --------------------------------------------------------------------------
def find_impulse(mtf):
    m = mtf[:-1]
    if len(m) < 30:
        return None
    lows, highs = swings(m, "low", len(m) - 1), swings(m, "high", len(m) - 1)
    for o in reversed(lows):
        rest = m[o + 1:]
        if len(rest) < 2 or min(x["low"] for x in rest) < m[o]["low"]:
            continue  # l'origine a été reprise par le prix
        e = o + 1 + max(range(len(rest)), key=lambda i: rest[i]["high"])
        before = [s for s in highs if s < o]
        if not before:
            continue
        prior = m[before[-1]]["high"]
        if not any(m[x]["close"] > prior for x in range(o + 1, e + 1)):
            continue  # pas de cassure de structure
        lo, hi = m[o]["low"], m[e]["high"]
        leg = hi - lo
        if leg <= 0 or leg < IMPULSE_ATR * atr(m[:e + 1]):
            continue
        avg = body_avg(m[max(0, o - 10):o + 1])
        if avg and max(body(x) for x in m[o + 1:e + 1]) < IMPULSE_DISP * avg:
            continue  # pas de déplacement
        if any(x["close"] < hi - ZONE[1] * leg for x in m[e + 1:]):
            return None  # retracement trop profond (au-delà de 0,79) : setup annulé
        return {"lo": lo, "hi": hi, "leg": leg,
                "o_epoch": m[o]["epoch"], "ext_epoch": m[e]["epoch"]}
    return None


# --------------------------------------------------------------------------
# 3) Entrée LTF : zone 50-79 % touchée -> sweep -> MSS + FVG -> OB dans la zone
# --------------------------------------------------------------------------
def find_entry(ltf, imp, mtf_gran):
    lo, hi, leg = imp["lo"], imp["hi"], imp["leg"]
    zone = (hi - ZONE[1] * leg, hi - ZONE[0] * leg)
    closed = ltf[:-1]
    start = imp["ext_epoch"] + mtf_gran
    first = next((i for i, c in enumerate(closed) if c["epoch"] >= start), None)
    if first is None:
        return None
    m = closed[first:]
    a = atr(closed)
    if len(m) < 8 or a <= 0:
        return None
    touch = next((i for i, c in enumerate(m) if c["low"] <= zone[1] and c["high"] >= zone[0]), None)
    if touch is None or any(c["close"] < lo for c in m):
        return None  # zone jamais touchée, ou origine cassée

    found = None
    for k in range(touch, len(m)):
        # sweep : la mèche prend un creux LTF (formé pendant le retracement) et referme au-dessus
        levels = [m[s]["low"] for s in swings(m[:k], "low", k - 1)[-3:]] if k >= 5 else []
        swept = [lv for lv in levels if m[k]["low"] < lv < m[k]["close"]]
        if not swept or any(c["close"] < m[k]["low"] for c in m[k + 1:]):
            continue
        for j in range(max(k + 1, len(m) - FRESH), len(m)):
            sh = swings(m[:j], "high", j - 1)
            if not sh:
                continue
            level = m[sh[-1]]["high"]
            if not (m[j]["close"] > level and m[j - 1]["close"] <= level):
                continue  # pas de cassure de structure
            avg = body_avg(m[max(0, j - 8):j])
            if avg and body(m[j]) < DISP * avg:
                continue  # pas de déplacement
            i = next((ii for ii in (j, j + 1)
                      if 2 <= ii < len(m) and ii - 1 >= k
                      and m[ii]["low"] > m[ii - 2]["high"]
                      and m[ii - 1]["close"] > m[ii - 1]["open"]), None)
            if i is None:
                continue  # pas de FVG
            ob_i = next((x for x in range(i - 2, k - 1, -1) if m[x]["close"] < m[x]["open"]), None)
            if ob_i is None:
                continue
            ob = (m[ob_i]["low"], m[ob_i]["high"])
            if not (ob[0] <= zone[1] and ob[1] >= zone[0]):
                continue  # l'OB doit chevaucher la zone 50 %-79 %
            found = {"k": k, "i": i, "ob": ob, "fvg": (m[i - 2]["high"], m[i]["low"]),
                     "sweep": max(swept), "sweep_low": m[k]["low"]}
    if not found:
        return None

    ob, fvg, price = found["ob"], found["fvg"], ltf[-1]["close"]
    sl = found["sweep_low"] - SL_BUFFER_ATR * a
    tp1 = hi
    if price <= sl or price >= tp1:
        return None
    after = m[found["i"] + 1:] + [ltf[-1]]   # bougies depuis la fin du FVG, bougie en cours comprise

    def rr_at(e):
        return (tp1 - e) / (e - sl) if e > sl else 0

    mode = entry = how = None
    # 1) ordre limite : milieu du FVG (prioritaire), sinon extrémité de l'OB, dans la zone et pas encore atteint
    for name, e in (("FVG 50 %", (fvg[0] + fvg[1]) / 2), ("extrémité OB", ob[1])):
        if not (zone[0] <= e <= zone[1]) or any(c["low"] <= e for c in after):
            continue
        if (price - e) > MAX_PROGRESS * (tp1 - e) or rr_at(e) < MIN_RR:
            continue
        mode, entry, how = "LIMITE", e, name
        break
    # 2) ordre instantané : le prix est dans l'OB
    if mode is None and ob[0] <= price <= ob[1] and rr_at(price) >= MIN_RR:
        mode, entry, how = "MARCHE", price, "OB"
    # 3) entrée tardive : le prix vient de quitter l'OB
    if mode is None and price > ob[1]:
        touches = [n for n, c in enumerate(after) if c["low"] <= ob[1]]
        if touches:
            since = len(after) - 1 - touches[-1]
            if (since <= LATE_CANDLES
                    and price - ob[1] <= LATE_DIST * (ob[1] - ob[0])
                    and (price - ob[1]) < LATE_PROGRESS * (tp1 - ob[1])
                    and rr_at(price) >= MIN_RR):
                mode, entry, how = "MARCHE", price, "tardive"
    if mode is None:
        return None
    return {"mode": mode, "entry": entry, "how": how, "sl": sl, "tp1": tp1,
            "tp2": lo + EXT[0] * leg, "tp3": lo + EXT[1] * leg,
            "rr": rr_at(entry), "ob": ob, "fvg": fvg, "zone": zone,
            "label": "Golden Zone" if (hi - entry) / leg < OTE[0] else "OTE",
            "sweep": found["sweep"], "price": price}


def unflip(r):
    out = dict(r)
    for k in ("entry", "sl", "tp1", "tp2", "tp3", "sweep", "price"):
        out[k] = -r[k]
    for k in ("ob", "fvg", "zone"):
        out[k] = (-r[k][1], -r[k][0])
    return out


# --------------------------------------------------------------------------
# Ordres limites en attente : message d'annulation
# --------------------------------------------------------------------------
def check_pending(api, cache, pending):
    for key, p in list(pending.items()):
        if time.time() - p["t"] > PENDING_TTL:
            del pending[key]
            continue
        c = get_candles(api, cache, p["sym"], p["gran"], force=True)
        if not c:
            continue
        cs = [x for x in c if x["epoch"] + p["gran"] > p["t"]]
        buy = p["dir"] == "BUY"
        if any((x["low"] <= p["entry"]) if buy else (x["high"] >= p["entry"]) for x in cs):
            del pending[key]  # ordre exécuté : plus rien à surveiller
            continue
        tp_hit = any((x["high"] >= p["tp1"]) if buy else (x["low"] <= p["tp1"]) for x in cs)
        sl_hit = any((x["close"] < p["sl"]) if buy else (x["close"] > p["sl"]) for x in cs)
        if tp_hit or sl_hit:
            why = "TP1 atteint sans exécution" if tp_hit else "stop-loss franchi avant exécution"
            notify(f"⚠️ ANNULER · {p['dir']} LIMIT · {p['name']} · {p['ltf']}\n"
                   f"{why}\nEntrée prévue  {code(p['entry'], p.get('dec'))}", html=True)
            del pending[key]


def build_message(name, ltf_n, d, r, dec=None):
    c = lambda v: code(v, dec)
    limit = r["mode"] == "LIMITE"
    label = f"{d} LIMIT" if limit else d
    lines = [f"{'🟢' if d == 'BUY' else '🔴'} {label} · {name} · {ltf_n}"]
    if r["how"] == "tardive":
        lines.append("⏱ Entrée tardive : le prix vient de quitter l'OB")
    lines += ["", "────────────────", f"Prix actuel  {c(r['price'])}", "────────────────", "",
              f"Entrée  {c(r['entry'])}" + (f"  ({r['how']})" if limit else ""),
              f"SL  {c(r['sl'])}",
              f"TP1  {c(r['tp1'])}   (1:{r['rr']:.1f})",
              f"TP2  {c(r['tp2'])}",
              f"TP3  {c(r['tp3'])}", "",
              f"✅ {r['label']} · OB {c(r['ob'][0])} - {c(r['ob'][1])}",
              f"✅ Sweep  {c(r['sweep'])}"]
    return "\n".join(lines)


def main():
    cache, st = load(CACHE_FILE), load(STATE_FILE)
    sent, pending = st.setdefault("sent", {}), st.setdefault("pending", {})
    api, alerts = Deriv(), 0
    try:
        check_pending(api, cache, pending)
        for sym in SYMBOLS:
            for htf_n, mtf_n, ltf_n in CASCADES:
                htf = get_candles(api, cache, sym, GRAN[htf_n])
                mtf = get_candles(api, cache, sym, GRAN[mtf_n])
                if not htf or not mtf:
                    continue
                ltf = None
                for d in ("BUY", "SELL"):
                    H, M = (htf, mtf) if d == "BUY" else (flip(htf), flip(mtf))
                    if not trend_up(H):
                        continue
                    imp = find_impulse(M)
                    if not imp:
                        continue
                    if ltf is None:
                        ltf = get_candles(api, cache, sym, GRAN[ltf_n], force=True) or []
                    if not ltf or time.time() - ltf[-1]["epoch"] > 3 * GRAN[ltf_n]:
                        continue
                    r = find_entry(ltf if d == "BUY" else flip(ltf), imp, GRAN[mtf_n])
                    if not r:
                        continue
                    if d == "SELL":
                        r = unflip(r)
                    key = f"{sym}|{htf_n}>{mtf_n}>{ltf_n}|{d}|{imp['o_epoch']}|{imp['ext_epoch']}"
                    if key in sent:
                        continue  # une seule alerte par impulsion
                    sent[key] = int(time.time())
                    alerts += 1
                    name = display_name(sym)
                    dec = decimals(ltf)
                    notify(build_message(name, ltf_n, d, r, dec), html=True)
                    if r["mode"] == "LIMITE":
                        pending[key] = {"sym": sym, "name": name, "dir": d, "entry": r["entry"],
                                        "sl": r["sl"], "tp1": r["tp1"], "ltf": ltf_n, "dec": dec,
                                        "gran": GRAN[ltf_n], "t": int(time.time())}
        print(f"Impulsion : scan terminé, {alerts} alerte(s), {len(pending)} ordre(s) en attente.")
        if os.getenv("NOTIFY_OK") == "1":
            notify(f"Impulsion OK : {len(SYMBOLS)} indices scannés, {alerts} alerte(s).")
    finally:
        api.close()
        cut = time.time() - 7 * 86400
        st["sent"] = {k: v for k, v in sent.items() if v > cut}
        save(CACHE_FILE, cache)
        save(STATE_FILE, st)


if __name__ == "__main__":
    main()
