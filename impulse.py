"""Stratégie Impulsion + retracement Fibonacci (zone 50 %-79 %) - Volatility Index Deriv.
Cascade identique au CRT : (D1,H4,H1) (H4,H1,M15) (H1,M15,M5)
  HTF : tendance (2 swings, structure intacte, âge classé) | MTF : impulsion + Fibonacci
  LTF : sweep -> MSS + FVG -> OB (zone 50 %-79 %)
Le bot n'exécute aucun ordre : il envoie des alertes Telegram (ordre limite ou entrée immédiate),
puis suit chaque trade (tracker.py) avec les garde-fous de guard.py.
Les calculs sont écrits pour un achat ; pour une vente, les bougies sont inversées (prix * -1).
"""
import os, sys, time
from collections import Counter

import charts
import guard as G
import tracker as T
from bot import (Deriv, get_candles, load, save, notify, body_avg, swings, atr, body, display_name, code,
                 decimals, ratio_txt, mgmt_line, track_trades, emit_alert, finish_run, print_stats,
                 vol_regime_ok, GRAN, CASCADES, CACHE_FILE)

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
SL_BUFFER_ATR = 0.15        # tampon du stop-loss (x ATR LTF)
MIN_RR = 3.0                # ratio gain/risque minimum (1 pour 3)
FRESH = 2                   # la cassure LTF doit dater des 2 dernières bougies clôturées
MAX_PROGRESS = 0.5          # ordre limite rejeté si le prix a déjà fait plus de 50 % du chemin entrée -> TP1
LATE_CANDLES = 2            # entrée tardive : le prix a quitté l'OB depuis 2 bougies au plus
LATE_DIST = 0.5             # ... et se trouve à moins d'une demi-hauteur d'OB de son bord
LATE_PROGRESS = 0.25        # ... et a fait moins de 25 % du chemin bord de l'OB -> TP1
PENDING_TTL = 24 * 3600     # un ordre limite en attente expire après 24 h
SENT_TTL = 14 * 86400       # une impulsion déjà alertée n'est pas réalertée avant 14 jours
IMPULSE_MAX_AGE = 48        # l'extrême de l'impulsion doit dater de 48 bougies MTF clôturées au plus
FVG_DISP = 1.5              # bougie centrale du FVG >= 1,5 x corps moyen (comme le CRT)
FVG_MIN_ATR = 0.1           # taille minimale du FVG (x ATR LTF)
RR_ON = "tp2"               # cible sur laquelle porte le ratio minimum : "tp1" ou "tp2" (comme le CRT)
BOS_BY_CLOSE = True         # tendance : le dernier sommet doit être dépassé par une clôture (pas une simple mèche)
AGE_ESTABLISHED = 2         # jambes consécutives (sommet + creux plus hauts) : 1 = naissante
AGE_MATURE = 4              # >= 4 jambes = tendance mûre (proche de sa fin)
TREND_AGES_OK = ("naissante", "établie")   # âges acceptés ; ajoute "mûre" pour tout accepter


def flip(cs):
    return [{"epoch": c["epoch"], "open": -c["open"], "high": -c["low"],
             "low": -c["high"], "close": -c["close"]} for c in cs]


# --------------------------------------------------------------------------
# 1) Tendance HTF : sommets et creux plus hauts, structure intacte, âge classé
# --------------------------------------------------------------------------
def _run(vals):
    """Nombre de hausses successives à la fin de la liste (ex : 1,2,3 -> 2)."""
    n = 0
    for a, b in zip(vals[-2::-1], vals[:0:-1]):
        if b > a:
            n += 1
        else:
            break
    return n


def trend_info(htf):
    """Tendance haussière (écrite pour un achat) ou None.
    - dernier sommet et dernier creux plus hauts que les précédents (2 swings suffisent) ;
    - structure intacte : aucune clôture sous le dernier creux depuis sa formation ;
    - dernier sommet dépassé par une clôture (BOS), pas seulement par une mèche ;
    - âge = nombre de jambes consécutives (sommet + creux plus hauts) :
      1 -> naissante, 2-3 -> établie, 4 et plus -> mûre."""
    h = htf[:-1]
    if len(h) < 12:
        return None
    sh, sl = swings(h, "high", len(h) - 1), swings(h, "low", len(h) - 1)
    if len(sh) < 2 or len(sl) < 2:
        return None
    highs, lows = [h[i]["high"] for i in sh], [h[i]["low"] for i in sl]
    if not (highs[-1] > highs[-2] and lows[-1] > lows[-2]):
        return None
    if any(c["close"] < lows[-1] for c in h[sl[-1] + 1:]):
        return None  # structure cassée : le dernier creux a été clôturé en dessous
    if BOS_BY_CLOSE and not any(c["close"] > highs[-2] for c in h[sh[-2] + 1:]):
        return None  # sommet dépassé seulement par une mèche
    legs = max(1, min(_run(highs), _run(lows)))
    age = "naissante" if legs < AGE_ESTABLISHED else "établie" if legs < AGE_MATURE else "mûre"
    return {"age": age, "legs": legs}


def trend_up(htf):
    return trend_info(htf) is not None


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
        if len(m) - 1 - e > IMPULSE_MAX_AGE:
            return None  # impulsion trop ancienne (les origines plus anciennes ont un extrême plus ancien encore)
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
    if any(c["close"] < zone[0] for c in m):
        return None  # une clôture LTF au-delà de 79 % : retracement trop profond, setup annulé

    cands = []
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
            favg = body_avg(m[max(0, j - 8):j])
            i = next((ii for ii in (j, j + 1)
                      if 2 <= ii < len(m) and ii - 1 >= k
                      and m[ii]["low"] > m[ii - 2]["high"]
                      and m[ii]["low"] - m[ii - 2]["high"] >= FVG_MIN_ATR * a
                      and m[ii - 1]["close"] > m[ii - 1]["open"]
                      and (not favg or body(m[ii - 1]) >= FVG_DISP * favg)), None)
            if i is None:
                continue  # pas de FVG
            ob_i = next((x for x in range(i - 2, k - 1, -1) if m[x]["close"] < m[x]["open"]), None)
            if ob_i is None:
                continue
            ob = (m[ob_i]["low"], m[ob_i]["high"])
            if not (ob[0] <= zone[1] and ob[1] >= zone[0]):
                continue  # l'OB doit chevaucher la zone 50 %-79 %
            cands.append({"k": k, "i": i, "ob": ob, "fvg": (m[i - 2]["high"], m[i]["low"]),
                          "sweep": max(swept), "sweep_low": m[k]["low"]})
    if not cands:
        return None
    # plusieurs combinaisons sweep/cassure possibles : on garde celle qui offre le meilleur ratio
    best = [r for r in (_evaluate(c, m, ltf, zone, lo, hi, leg, a) for c in cands) if r]
    return max(best, key=lambda r: r["rr"]) if best else None


def _evaluate(found, m, ltf, zone, lo, hi, leg, a):
    """Stop, cibles et type d'ordre d'un candidat. Retourne None si le trade est refusé."""
    ob, fvg, price = found["ob"], found["fvg"], ltf[-1]["close"]
    sl = found["sweep_low"] - SL_BUFFER_ATR * a
    tp1 = hi
    tp2 = lo + EXT[0] * leg
    if price <= sl or price >= tp1:
        return None
    after = m[found["i"] + 1:] + [ltf[-1]]   # bougies depuis la fin du FVG, bougie en cours comprise

    def rr_at(e):  # ratio gain/risque sur la cible choisie par RR_ON (TP2 par défaut, comme le CRT)
        tp = tp2 if RR_ON == "tp2" else tp1
        return (tp - e) / (e - sl) if e > sl else 0

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
            "tp2": tp2, "tp3": lo + EXT[1] * leg,
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


def build_message(name, ltf_n, d, r, dec=None, trend=None, htf_n=None, sym=None):
    c = lambda v: code(v, dec)
    limit = r["mode"] == "LIMITE"
    label = f"{d} LIMIT" if limit else d
    lines = [f"{'🟢' if d == 'BUY' else '🔴'} {label} · {name} · {ltf_n} · Impulsion"]
    if r["how"] == "tardive":
        lines.append("⏱ Entrée tardive : le prix vient de quitter l'OB")
    lines += ["", "────────────────", f"Prix actuel  {c(r['price'])}", "────────────────", "",
              f"Entrée  {c(r['entry'])}" + (f"  ({r['how']})" if limit else ""),
              f"SL  {c(r['sl'])}",
              f"TP1  {c(r['tp1'])}{ratio_txt(d, r['entry'], r['sl'], r['tp1'])}",
              f"TP2  {c(r['tp2'])}{ratio_txt(d, r['entry'], r['sl'], r['tp2'])}",
              f"TP3  {c(r['tp3'])}{ratio_txt(d, r['entry'], r['sl'], r['tp3'])}", "",
              f"✅ {r['label']} · OB {c(r['ob'][0])} - {c(r['ob'][1])}",
              f"✅ Sweep  {c(r['sweep'])}"]
    if trend:
        lines.append(f"✅ Tendance {trend['age']} · {htf_n}")
    if T.STRUCT_EXIT and r.get("inval") is not None:
        lines.append(f"Invalidation  {c(r['inval'])}  (clôture au-delà = sortie)")
    extra = [x for x in (G.lot_line(sym, r["entry"], r["sl"]) if sym else None, mgmt_line()) if x]
    if extra:
        lines += [""] + extra
    return "\n".join(lines)


def scan_symbol(sym, provide, now, sent, funnel, align=False):
    """Analyse un indice sur toutes les cascades. provide(tf, force=False) renvoie les bougies
    (la dernière est en formation) ou None. align=True (backtest) : une cascade n'est évaluée que
    quand sa bougie LTF vient de clôturer."""
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
            continue  # volatilité en compression extrême ou en pic
        funnel["regime"] += 1
        ltf = None
        for d in ("BUY", "SELL"):
            H, M = (htf, mtf) if d == "BUY" else (flip(htf), flip(mtf))
            trend = trend_info(H)
            if not trend or trend["age"] not in TREND_AGES_OK:
                continue
            funnel["trend"] += 1
            imp = find_impulse(M)
            if not imp:
                continue
            funnel["impulse"] += 1
            if ltf is None:
                ltf = provide(ltf_n, True) or []
            if not ltf or now - ltf[-1]["epoch"] > 3 * GRAN[ltf_n]:
                continue
            r = find_entry(ltf if d == "BUY" else flip(ltf), imp, GRAN[mtf_n])
            if not r:
                continue
            funnel["trade"] += 1
            if d == "SELL":
                r = unflip(r)
            key = f"{sym}|{htf_n}>{mtf_n}>{ltf_n}|{d}|{imp['o_epoch']}|{imp['ext_epoch']}"
            if key in sent:
                continue  # une seule alerte par impulsion
            name, dec = display_name(sym), decimals(ltf)
            buy = d == "BUY"
            inval = r["ob"][0] if buy else r["ob"][1]          # bord de l'OB côté stop
            r["inval"] = inval
            origin, extreme = (imp["lo"], imp["hi"]) if buy else (-imp["lo"], -imp["hi"])
            lines = [{"y": r["entry"], "label": "Entrée", "kind": "entry"}, {"y": r["sl"], "label": "SL", "kind": "sl"},
                     {"y": r["tp1"], "label": "TP1", "kind": "tp"}, {"y": r["tp2"], "label": "TP2", "kind": "tp"},
                     {"y": r["tp3"], "label": "TP3", "kind": "tp"}, {"y": r["sweep"], "label": "Sweep", "kind": "sweep"},
                     {"y": origin, "label": "Origine", "kind": "range"}, {"y": extreme, "label": "Extrême", "kind": "range"}]
            zones = [{"lo": r["zone"][0], "hi": r["zone"][1], "label": "Zone 50-79 %", "kind": "fib"},
                     {"lo": r["fvg"][0], "hi": r["fvg"][1], "label": "FVG", "kind": "fvg"},
                     {"lo": r["ob"][0], "hi": r["ob"][1], "label": "OB", "kind": "ob"}]
            otype = f"{d} LIMIT" if r["mode"] == "LIMITE" else d
            chart = {"title": f"{name} · {otype} · {ltf_n} · Impulsion", "dec": dec,
                     "candles": charts.candles_slice(ltf), "lines": lines, "zones": zones}
            out.append({
                "bot": "Impulsion", "key": key, "cascade": f"{htf_n}>{mtf_n}>{ltf_n}", "sym": sym,
                "name": name, "dir": d, "ltf": ltf_n, "gran": GRAN[ltf_n], "dec": dec,
                "price": r["price"], "otype": otype, "inval": inval, "liq": None, "chart": chart,
                "entry": r["entry"], "sl": r["sl"], "tp1": r["tp1"], "tp2": r["tp2"],
                "tp3": r["tp3"], "rr": r["rr"], "age": trend["age"], "ttl": PENDING_TTL,
                "message": build_message(name, ltf_n, d, r, dec, trend, htf_n, sym)})
    return out


def main():
    if "--stats" in sys.argv:
        print_stats()
        return
    now = time.time()
    cache, st = load(CACHE_FILE), load(STATE_FILE)
    sent = st.setdefault("sent", {})
    tr, g = T.load_trades(), G.load_guard()
    api, funnel = Deriv(), Counter()
    try:
        G.health_start(g, "Impulsion", now, notify)
        track_trades(api, tr, "Impulsion", now)
        for sym in SYMBOLS:
            provide = lambda tf, force=False, s=sym: get_candles(api, cache, s, GRAN[tf], force=force)
            for a in scan_symbol(sym, provide, now, sent, funnel):
                emit_alert(a, tr, g, sent, funnel, now, a["message"])
        finish_run("Impulsion", g, tr, funnel, now)
        pend = sum(1 for t in T.active_list(tr) if t["bot"] == "Impulsion" and t["status"] == "pending")
        print(f"Impulsion : scan terminé, {funnel['alert']} alerte(s), {pend} ordre(s) en attente. "
              f"Entonnoir : {G.funnel_text(funnel)}")
        if os.getenv("NOTIFY_OK") == "1":
            notify(f"Impulsion OK : {len(SYMBOLS)} indices scannés, {funnel['alert']} alerte(s).\n"
                   f"Entonnoir : {G.funnel_text(funnel)}")
    finally:
        api.close()
        cut = time.time() - SENT_TTL
        st["sent"] = {k: v for k, v in sent.items() if v > cut}
        save(CACHE_FILE, cache)
        save(STATE_FILE, st)
        T.save_trades(tr)
        G.save_guard(g)


if __name__ == "__main__":
    main()
