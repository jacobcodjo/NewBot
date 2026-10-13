"""Étude de prédictibilité : la clôture d'une bougie D1 ou W1 prédit-elle la direction de la suivante ?

Pour chaque règle, chaque timeframe (D1, W1) et chaque classe d'actif (Forex/Or, Crypto, Synthétiques),
le script mesure sur l'historique Deriv à quelle fréquence le signal d'une bougie donne la bonne direction
de la bougie suivante (ouverture -> clôture), et si cet écart par rapport au hasard est réel.

Contrôles contre les faux positifs :
- Dérive neutralisée : le taux attendu au hasard (p0) tient compte de la part de bougies haussières de
  l'échantillon. Un marché qui monte en moyenne ne rend pas un signal « acheteur » prédictif.
- Correction du nombre de tests (Holm) : on teste des dizaines de combinaisons, certaines paraissent
  significatives par pur hasard.
- Validation hors échantillon : la règle est choisie sur les 70 % de bougies les plus anciennes de chaque
  symbole, puis doit être confirmée, dans le même sens, sur les 30 % les plus récentes.
Une règle n'est « retenue » que si elle passe les trois contrôles. Même retenue, un écart de 1 à 2 points
ne couvre souvent pas les coûts (spread, glissement) : l'écart est donné en points de pourcentage.

Usage : python bias_study.py [--classes forex,crypto,synthetiques] [--symbols R_75,cryBTCUSD]
                             [--max-bars 1500] [--no-trend] [--out bias_study.csv]
Limites : 5000 bougies D1 par requête Deriv ; les symboles d'une même classe sont corrélés (les paires
forex partagent le dollar), donc l'échantillon groupé compte moins d'observations indépendantes que son
effectif : le test est optimiste, la validation hors échantillon et le nombre de symboles concordants
servent de garde-fou. Les Volatility Index sont un tirage aléatoire sans mémoire : aucun signal n'y est attendu.
"""
import argparse, csv, math, os, sys, time

import bot
import outlook
import tracker

SPLIT = 0.70            # part des bougies les plus anciennes (découverte) ; le reste sert à la validation
WARMUP = 60             # bougies ignorées au début (indicateurs et tendance)
MIN_DISCOVERY = 200     # signaux minimum en découverte
MIN_VALIDATION = 100    # signaux minimum en validation
ALPHA = 0.05
CLASS_KEYS = {"forex": "Forex/Or", "crypto": "Crypto", "synthetiques": "Synthétiques"}
TREND_RULES = tuple(f"tendance_{m}" for m in bot.TREND_MODES)


# --------------------------------------------------------------------------
# Signaux : +1 (biais haussier), -1 (biais baissier), 0 (aucun), lus à la clôture de la bougie i
# --------------------------------------------------------------------------
def _body(c):
    return abs(c["close"] - c["open"])


def sig_cassure(cs, i):
    """Clôture au-delà du plus haut / plus bas de la bougie précédente (continuation)."""
    if i < 1:
        return 0
    if cs[i]["close"] > cs[i - 1]["high"]:
        return 1
    return -1 if cs[i]["close"] < cs[i - 1]["low"] else 0


def sig_position(cs, i):
    """Clôture dans le tiers supérieur (+1) ou inférieur (-1) de l'amplitude de la bougie."""
    c = cs[i]
    rng = c["high"] - c["low"]
    if rng <= 0:
        return 0
    pos = (c["close"] - c["low"]) / rng
    return 1 if pos >= 2 / 3 else (-1 if pos <= 1 / 3 else 0)


def sig_direction(cs, i):
    """Direction de la bougie (haussière +1, baissière -1)."""
    c = cs[i]
    return 1 if c["close"] > c["open"] else (-1 if c["close"] < c["open"] else 0)


def sig_impulsion(cs, i):
    """Bougie impulsive : corps d'au moins 1,5 fois le corps moyen des 10 précédentes, dans sa direction."""
    if i < 10:
        return 0
    avg = sum(_body(c) for c in cs[i - 10:i]) / 10
    return sig_direction(cs, i) if avg > 0 and _body(cs[i]) >= 1.5 * avg else 0


def sig_tendance(mode):
    def f(cs, i, gran):
        window = cs[max(0, i - bot.COUNT + 2):i + 1]
        p = window[-1]["close"]   # bougie en formation neutre : trend_info ignore la dernière bougie
        forming = {"epoch": window[-1]["epoch"] + gran, "open": p, "high": p, "low": p, "close": p}
        t = bot.trend_info(window + [forming], mode)["trend"]
        return 1 if t == "bullish" else (-1 if t == "bearish" else 0)
    return f


SIMPLE_RULES = {
    "cassure_extreme_precedent": sig_cassure,
    "cloture_tiers_haut_bas": sig_position,
    "direction_bougie": sig_direction,
    "bougie_impulsive": sig_impulsion,
}


def atr(cs, i, n=14):
    trs = [max(cs[k]["high"] - cs[k]["low"], abs(cs[k]["high"] - cs[k - 1]["close"]),
               abs(cs[k]["low"] - cs[k - 1]["close"])) for k in range(max(1, i - n + 1), i + 1)]
    return sum(trs) / len(trs) if trs else 0.0


# --------------------------------------------------------------------------
# Collecte : une ligne par (symbole, bougie i, règle) avec le résultat de la bougie i+1
# --------------------------------------------------------------------------
def collect(sym, cs, gran, rules, split=SPLIT):
    """Retourne (base, rows). base : {half: [n_up, n_total]} sur toutes les bougies suivantes ;
    rows : liste de (rule, half, signal, up, ret_atr)."""
    n_bars = len(cs) - 1
    cut = WARMUP + int((n_bars - WARMUP) * split)
    base = {"decouverte": [0, 0], "validation": [0, 0]}
    rows = []
    trend_fns = {r: sig_tendance(r.split("_", 1)[1]) for r in rules if r in TREND_RULES}
    for i in range(WARMUP, n_bars):
        nxt = cs[i + 1]
        if nxt["close"] == nxt["open"]:
            continue                      # bougie suivante sans direction : écartée
        up = nxt["close"] > nxt["open"]
        half = "decouverte" if i < cut else "validation"
        base[half][1] += 1
        base[half][0] += up
        a = atr(cs, i)
        ret = (nxt["close"] - nxt["open"]) / a if a > 0 else 0.0
        for r in rules:
            s = SIMPLE_RULES[r](cs, i) if r in SIMPLE_RULES else trend_fns[r](cs, i, gran)
            if s:
                rows.append((r, half, s, up, ret * s))
    return base, rows


# --------------------------------------------------------------------------
# Statistiques
# --------------------------------------------------------------------------
def z_test(hits, n_long, n_short, q_up):
    """z et p (bilatéral) du nombre de bonnes directions, face au hasard compte tenu de q_up
    (part de bougies haussières) et du mélange de signaux acheteurs / vendeurs."""
    n = n_long + n_short
    if n == 0 or not 0 < q_up < 1:
        return 0.0, 1.0, 0.5
    p0 = (n_long * q_up + n_short * (1 - q_up)) / n
    z = (hits - n * p0) / math.sqrt(n * q_up * (1 - q_up))
    return z, math.erfc(abs(z) / math.sqrt(2)), p0


def holm(pvals):
    """Correction de Holm (contrôle du risque d'au moins un faux positif parmi tous les tests)."""
    order = sorted(range(len(pvals)), key=lambda k: pvals[k])
    adj, running, m = [1.0] * len(pvals), 0.0, len(pvals)
    for rank, k in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvals[k]))
        adj[k] = running
    return adj


def aggregate(rule_rows, base_half):
    """Résumé d'un lot de lignes (une règle) sur une moitié d'historique."""
    n_long = sum(1 for r in rule_rows if r[2] > 0)
    n_short = len(rule_rows) - n_long
    hits = sum(1 for r in rule_rows if (r[2] > 0) == r[3])
    q_up = base_half[0] / base_half[1] if base_half[1] else 0.5
    z, p, p0 = z_test(hits, n_long, n_short, q_up)
    n = n_long + n_short
    return {"n": n, "hit": hits / n if n else 0.0, "p0": p0, "z": z, "p": p,
            "edge_pp": 100 * (hits / n - p0) if n else 0.0,
            "ret_atr": sum(r[4] for r in rule_rows) / n if n else 0.0}


def analyze(data, rules):
    """data : {(classe, tf): {"base": {...}, "rows": [(symbole, règle, half, s, up, ret)], "symbols": [...]}}.
    Retourne la liste des tests (un par classe x tf x règle), corrigés et validés."""
    tests = []
    for (cls, tf), d in data.items():
        for rule in rules:
            res = {}
            for half in ("decouverte", "validation"):
                rr = [r[1:] for r in d["rows"] if r[1] == rule and r[2] == half]   # (règle, moitié, signal, haussière, rendement)
                res[half] = aggregate(rr, d["base"][half])
            # cohérence entre symboles (découverte) : combien ont un taux au-dessus de leur hasard
            per_sym = {}
            for r in d["rows"]:
                if r[1] == rule and r[2] == "decouverte":
                    per_sym.setdefault(r[0], []).append(r)
            agree = tot = 0
            for sym, rr in per_sym.items():
                if len(rr) >= 30:
                    q = d["sym_base"][sym]["decouverte"]
                    qu = q[0] / q[1] if q[1] else 0.5
                    nl = sum(1 for x in rr if x[3] > 0)
                    hits = sum(1 for x in rr if (x[3] > 0) == x[4])
                    p0 = (nl * qu + (len(rr) - nl) * (1 - qu)) / len(rr)
                    tot += 1
                    agree += (hits / len(rr) > p0) == (res["decouverte"]["edge_pp"] > 0)
            tests.append({"classe": cls, "tf": tf, "regle": rule, "disc": res["decouverte"],
                          "val": res["validation"], "sym_agree": agree, "sym_total": tot})
    adj = holm([t["disc"]["p"] for t in tests]) if tests else []
    for t, a in zip(tests, adj):
        t["p_holm"] = a
        d, v = t["disc"], t["val"]
        same_sign = d["edge_pp"] * v["edge_pp"] > 0
        t["retenue"] = bool(a < ALPHA and d["n"] >= MIN_DISCOVERY and v["n"] >= MIN_VALIDATION
                            and same_sign and v["p"] < ALPHA)
    return tests


# --------------------------------------------------------------------------
# Présentation
# --------------------------------------------------------------------------
LABEL_RULE = {
    "cassure_extreme_precedent": "clôture au-delà du plus haut / bas précédent",
    "cloture_tiers_haut_bas": "clôture dans le tiers haut / bas de la bougie",
    "direction_bougie": "direction de la bougie",
    "bougie_impulsive": "bougie impulsive (corps >= 1,5x)",
    "tendance_structure": "tendance (structure)",
    "tendance_deplacement": "tendance (déplacement)",
    "tendance_swings": "tendance (3 swings + SMA 50)",
}


def line(t):
    d, v = t["disc"], t["val"]
    sens = "continuation" if d["edge_pp"] > 0 else "inversion"
    sym = f", {t['sym_agree']}/{t['sym_total']} symboles dans le même sens" if t["sym_total"] else ""
    return (f"{t['classe']} {t['tf']} | {LABEL_RULE.get(t['regle'], t['regle'])} ({sens}) : "
            f"découverte {d['hit'] * 100:.1f}% vs {d['p0'] * 100:.1f}% attendu ({d['edge_pp']:+.1f} pt, n={d['n']}, "
            f"p corrigé {t['p_holm']:.3f}), validation {v['hit'] * 100:.1f}% vs {v['p0'] * 100:.1f}% "
            f"({v['edge_pp']:+.1f} pt, n={v['n']}, p {v['p']:.3f}){sym}")


def summary(tests, info):
    L = [f"ÉTUDE DE PRÉDICTIBILITÉ : {info}", "",
         f"{len(tests)} combinaisons testées (classe x timeframe x règle). Critères pour retenir une règle : "
         f"p corrigé (Holm) < {ALPHA} en découverte, même sens et p < {ALPHA} en validation, au moins "
         f"{MIN_DISCOVERY} / {MIN_VALIDATION} signaux.", ""]
    kept = [t for t in tests if t["retenue"]]
    if kept:
        L.append(f"RÈGLES RETENUES ({len(kept)}) :")
        L += ["  " + line(t) for t in sorted(kept, key=lambda t: -abs(t["disc"]["edge_pp"]))]
    else:
        L.append("AUCUNE RÈGLE RETENUE : aucun signal ne bat le hasard de façon fiable une fois corrigé "
                 "du nombre de tests et confirmé hors échantillon.")
    others = sorted((t for t in tests if not t["retenue"] and t["disc"]["n"] >= MIN_DISCOVERY),
                    key=lambda t: t["disc"]["p"])[:8]
    if others:
        L += ["", "Les plus proches du seuil, non retenues :"]
        L += ["  " + line(t) for t in others]
    n_nom = sum(1 for t in tests if t["disc"]["p"] < ALPHA)
    L += ["", f"Pour repère : {n_nom} tests sur {len(tests)} ont un p brut < {ALPHA} en découverte, alors que "
              f"le hasard seul en donnerait environ {len(tests) * ALPHA:.0f}.",
          "", "Lecture : écart en points de pourcentage au-dessus du taux attendu au hasard. 1 à 2 points ne couvrent "
          "généralement pas le spread et le glissement. Un biais journalier ne dit rien de l'amplitude du mouvement.",
          "Les Volatility Index sont un tirage aléatoire sans mémoire : tout signal « retenu » y serait suspect."]
    return "\n".join(L)


def write_csv(path, tests):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["classe", "timeframe", "regle", "sens", "retenue", "p_corrige", "decouverte_n", "decouverte_taux",
                    "decouverte_attendu", "decouverte_ecart_pt", "decouverte_p", "decouverte_ret_atr",
                    "validation_n", "validation_taux", "validation_attendu", "validation_ecart_pt", "validation_p",
                    "validation_ret_atr", "symboles_concordants", "symboles_total"])
        for t in tests:
            d, v = t["disc"], t["val"]
            w.writerow([t["classe"], t["tf"], t["regle"], "continuation" if d["edge_pp"] > 0 else "inversion",
                        int(t["retenue"]), round(t["p_holm"], 4), d["n"], round(d["hit"], 4), round(d["p0"], 4),
                        round(d["edge_pp"], 2), round(d["p"], 4), round(d["ret_atr"], 3),
                        v["n"], round(v["hit"], 4), round(v["p0"], 4), round(v["edge_pp"], 2), round(v["p"], 4),
                        round(v["ret_atr"], 3), t["sym_agree"], t["sym_total"]])


# --------------------------------------------------------------------------
# Données et exécution
# --------------------------------------------------------------------------
def closed_series(cs, gran, now):
    return [c for c in cs if c["epoch"] + gran <= now]


def series_for(sym, d1, tf, now):
    """Bougies clôturées du timeframe : D1 tel quel, W1 agrégé (semaine ISO, la semaine en cours écartée)."""
    d1c = closed_series(d1, 86400, now)
    if tf == "D1":
        return d1c, 86400
    wk = outlook.weekly_candles(d1c)
    return wk[:-1], 7 * 86400


def run(api, symbols, rules, max_bars, log=print, now=None):
    now = now or int(time.time())
    data = {}
    for n, sym in enumerate(symbols, 1):
        d1 = api.candles(sym, 86400, count=5000, end=now)
        if not d1 or len(d1) < 200:
            log(f"[{n}/{len(symbols)}] {sym} : historique D1 insuffisant, ignoré")
            continue
        cls = tracker.asset_class(sym)
        for tf in ("D1", "W1"):
            cs, gran = series_for(sym, d1, tf, now)
            cs = cs[-(max_bars if tf == "D1" else max(40, max_bars // 5)):]
            if len(cs) < WARMUP + 100:
                continue
            base, rows = collect(sym, cs, gran, rules)
            slot = data.setdefault((cls, tf), {"base": {"decouverte": [0, 0], "validation": [0, 0]},
                                               "rows": [], "sym_base": {}, "symbols": []})
            for half in base:
                slot["base"][half][0] += base[half][0]
                slot["base"][half][1] += base[half][1]
            slot["sym_base"][sym] = base
            slot["rows"] += [(sym,) + r for r in rows]
            slot["symbols"].append(sym)
        log(f"[{n}/{len(symbols)}] {sym} ({cls}) : {len(d1)} bougies D1")
    return data


def main():
    p = argparse.ArgumentParser(description="Étude de prédictibilité de la bougie D1 / W1 suivante")
    p.add_argument("--classes", default="forex,crypto,synthetiques",
                   help="classes d'actifs (forex, crypto, synthetiques), séparées par des virgules")
    p.add_argument("--symbols", default="", help="liste de symboles (défaut : tous ceux des classes choisies)")
    p.add_argument("--max-bars", type=int, default=1500, help="bougies D1 les plus récentes utilisées (défaut 1500)")
    p.add_argument("--no-trend", action="store_true", help="ignore les règles de tendance (plus rapide)")
    p.add_argument("--out", default="bias_study.csv", help="fichier CSV des résultats")
    a = p.parse_args()
    wanted = []
    for c in (x.strip() for x in a.classes.split(",") if x.strip()):
        if c not in CLASS_KEYS:
            sys.exit(f"Classe inconnue : {c} (choix : {', '.join(CLASS_KEYS)})")
        wanted.append(CLASS_KEYS[c])
    symbols = [s.strip() for s in a.symbols.split(",") if s.strip()] or bot.SYMBOLS
    symbols = [s for s in symbols if tracker.asset_class(s) in wanted]
    unknown = [s for s in symbols if s not in bot.SYMBOLS]
    if unknown:
        sys.exit(f"Symboles inconnus : {unknown}")
    rules = list(SIMPLE_RULES) + ([] if a.no_trend else list(TREND_RULES))
    api = bot.Deriv()
    try:
        data = run(api, symbols, rules, a.max_bars)
    finally:
        api.close()
    tests = analyze(data, rules)
    n_sym = sum(len(d["symbols"]) for (c, tf), d in data.items() if tf == "D1")
    text = summary(tests, f"{n_sym} symboles, jusqu'à {a.max_bars} bougies D1 chacun, découverte "
                          f"{int(SPLIT * 100)} % / validation {100 - int(SPLIT * 100)} %")
    print("\n" + text)
    write_csv(a.out, tests)
    print(f"\nRésultats détaillés : {a.out}")
    gh = os.getenv("GITHUB_STEP_SUMMARY")
    if gh:
        with open(gh, "a", encoding="utf-8") as f:
            f.write("```\n" + text + "\n```\n")


if __name__ == "__main__":
    main()
