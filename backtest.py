"""Backtest des stratégies CRT et Impulsion : rejoue l'historique Deriv comme le bot le verrait en direct.

À chaque clôture de bougie du timeframe de pas (M15 pour le CRT, M5 pour l'Impulsion), le script
reconstruit ce que le bot aurait vu (bougies clôturées + une bougie en formation neutre), appelle
l'analyse de la stratégie (bot.analyze ou impulse.analyze), applique les mêmes verrous anti-doublon,
puis suit chaque alerte avec tracker.py sur les bougies suivantes du timeframe de pas (plus fines que
le timeframe de confirmation, donc moins de cas SL/TP dans la même bougie qu'en direct).

Usage : python backtest.py [--strategy crt|impulse] [--days 30] [--symbols R_75,frxEURUSD] [--out fichier.csv]
        [--trend-mode structure|deplacement|swings|all]   (all : mêmes données rejouées avec les trois lectures de tendance)
        [--crt-mode loose|ote|mid|ote_confirm|all]        (CRT : mode historique ou CRT strict avec l'entrée choisie ;
                                                           all : compare les quatre sur le même historique)
Limites : 5000 bougies par requête Deriv, soit 45 jours maximum en M15 (CRT) et 16 jours en M5 (Impulsion).
Pas de spread, de commission ni de glissement. Gain compté sur la cible de gain entière : TP2 pour le CRT,
TP1 pour l'Impulsion (-1 pour un SL). Alignement 17h NY ignoré. Les ordres limites de l'Impulsion
expirent après 24 h, ceux du CRT après 7 jours.
"""
import argparse, csv, os, sys, time
from bisect import bisect_right
from datetime import datetime, timezone

import bot
import impulse
import tracker

CRT_CONF = sorted({tf for l in bot.REFERENCE_CONFIRMATION_MAP.values() for tf in l})


CRT_VARIANT = "loose"   # variante CRT rejouée : loose, ote, mid ou ote_confirm


def crt_setups(sym, series, t):
    htf = {tf: series[tf].view(t) for tf in ("D1", "H4")}
    if not htf["D1"] and not htf["H4"]:
        return []
    ltf = {tf: v for tf in CRT_CONF if (v := series[tf].view(t))}
    return bot.select_best(bot.analyze_variant(sym, htf, ltf, now=t, variant=CRT_VARIANT))


def impulse_setups(sym, series, t):
    get = lambda tf, force=False: series[tf].view(t)
    return impulse.analyze(sym, get, now=t)


STRATEGIES = {
    "crt": {"label": "CRT", "symbols": bot.SYMBOLS, "tfs": ("D1", "H4", "H1", "M15"), "step": "M15",
            "max_days": 45, "analyze": crt_setups, "keys": lambda s: bot.setup_keys(s), "win": "TP2"},
    "impulse": {"label": "Impulsion", "symbols": impulse.SYMBOLS, "tfs": ("D1", "H4", "H1", "M15", "M5"),
                "step": "M5", "max_days": 16, "analyze": impulse_setups, "keys": lambda s: (s["key"],),
                "win": "TP1"},
}


def fetch_history(api, sym, days, tfs):
    """Historique d'un symbole pour les timeframes demandés, ou None si indisponible."""
    out = {}
    for tf in tfs:
        n = 400 if tf == "D1" else days * 86400 // bot.GRAN[tf] + bot.COUNT + 20
        cs = api.candles(sym, bot.GRAN[tf], count=min(n, 5000))
        if not cs:
            return None
        out[tf] = cs
    return out


class Series:
    """Série de bougies avec vue 'à l'instant t'."""
    def __init__(self, cs, gran):
        self.cs, self.gran = cs, gran
        self.ends = [c["epoch"] + gran for c in cs]

    def view(self, t):
        """Bougies clôturées à t (au plus COUNT-1) + bougie en formation neutre ; None si marché fermé."""
        k = bisect_right(self.ends, t)
        if k < 2:
            return None
        closed = self.cs[max(0, k - (bot.COUNT - 1)):k]
        last = closed[-1]
        if t - last["epoch"] > 4 * self.gran:   # marché fermé : mêmes données figées qu'en direct
            return None
        p = last["close"]
        forming = {"epoch": (t // self.gran) * self.gran, "open": p, "high": p, "low": p, "close": p}
        return closed + [forming]


def replay(strat, sym, hist, start_epoch):
    """Rejoue un symbole. Retourne la liste des trades suivis (résultat inclus)."""
    series = {tf: Series(hist[tf], bot.GRAN[tf]) for tf in hist}
    step_gran = bot.GRAN[strat["step"]]
    step_cs = hist[strat["step"]]
    last_epoch = step_cs[-1]["epoch"]
    seen, trades = set(), []
    for c in step_cs:
        t = c["epoch"] + step_gran
        if t <= start_epoch:
            continue
        for s in strat["analyze"](sym, series, t):
            keys = strat["keys"](s)
            if any(k in seen for k in keys):
                continue
            seen.update(keys)
            trade = tracker.new_trade(s, t)
            result = tracker.resolve_trade(trade, [x for x in step_cs if x["epoch"] >= t])
            if result is None:
                if not trade["filled"] and last_epoch - t > trade["max_age"]:
                    result = "EXPIRE"
                else:
                    result = "OUVERT" if trade["filled"] else "EN_ATTENTE"
            trade["result"] = result
            trades.append(trade)
    return trades


COSTS = True   # --no-costs pour les désactiver


def cost_r(tr):
    """Coût aller-retour (spread + glissement estimés, bot.SPREAD_PCT / bot.SLIPPAGE_PCT) exprimé en R."""
    risk = abs(tr["entry"] - tr["stop_loss"])
    if not COSTS or risk <= 0:
        return 0.0
    return tr["entry"] * (bot.spread_pct(tr["symbol"]) + bot.SLIPPAGE_PCT) / 100 / risk


def r_of(tr):
    return -1.0 if tr["result"] == "SL" else tr["risk_reward"]


def r_net(tr):
    return r_of(tr) - cost_r(tr)


def r_managed_net(tr):
    return tracker.managed_r(tr) - cost_r(tr)


def group_line(name, trs, win):
    res = [x for x in trs if x["result"] in ("TP", "SL")]
    if not res:
        return f"{name:<22} {len(trs):>4} setups, aucun résolu"
    wins = sum(1 for x in res if x["result"] == "TP")
    n = len(res)
    line = (f"{name:<22} {len(trs):>4} setups | {n:>4} résolus | réussite {100*wins/n:5.1f}% "
            f"| R brut {sum(r_of(x) for x in res) / n:+.2f} | R net {sum(r_net(x) for x in res) / n:+.2f}")
    if win == "TP2":
        line += (f" | TP1 {100 * sum(1 for x in res if x['tp1_hit']) / n:5.1f}%"
                 f" | R géré net {sum(r_managed_net(x) for x in res) / n:+.2f}")
    return line


def split_lines(strat, trades, days, oos):
    """Stabilité dans le temps : même stratégie, première puis dernière partie de la période. Les paramètres
    n'ont pas été ajustés sur la première partie : si les deux résultats divergent fortement, le résultat global est fragile."""
    cut = time.time() - days * 86400 * oos
    a = [x for x in trades if x["alert_epoch"] < cut]
    b = [x for x in trades if x["alert_epoch"] >= cut]
    return ["", f"Stabilité dans le temps (derniers {oos:.0%} de la période isolés)",
            "  " + group_line("Première partie", a, strat["win"]), "  " + group_line("Dernière partie", b, strat["win"])]


def fill_stats(trades):
    """(remplis, décidés) : un ordre est décidé quand il est rempli ou expiré ; les ordres encore en attente sont exclus."""
    decided = [x for x in trades if x["result"] != "EN_ATTENTE"]
    return sum(1 for x in decided if x["filled"]), len(decided)


def summary(strat, trades, days, oos=0.0, label=None):
    win = strat["win"]
    if not trades:
        return "Aucun setup détecté sur la période."
    L = [f"BACKTEST {(label or strat['label']).upper()} : {days} jours, {len(trades)} setups (gagnant = {win} atteint avant le SL)", ""]
    L.append(group_line("Global", trades, win))
    if oos:
        L += split_lines(strat, trades, days, oos)
    counts = {k: sum(1 for x in trades if x["result"] == k) for k in ("EXPIRE", "OUVERT", "EN_ATTENTE")}
    L.append(f"Non résolus : {counts['EXPIRE']} expirés (jamais remplis), {counts['OUVERT']} ouverts, "
             f"{counts['EN_ATTENTE']} en attente")
    filled, decided = fill_stats(trades)
    if decided:
        L.append(f"Taux de remplissage : {100 * filled / decided:.0f} % ({filled} ordres remplis sur {decided} décidés)")
    sections = [
        ("Classe d'actif", lambda x: tracker.asset_class(x["symbol"])),
        ("Référence", lambda x: x["ref_tf"]),
        ("Ordre", lambda x: "au marché" if x["order_type"] in tracker.MARKET_ORDERS else "en attente"),
        ("Sens", lambda x: x["direction"]),
    ]
    if any(x["counter_trend"] for x in trades):   # sans objet pour l'Impulsion (toujours dans la tendance)
        sections.insert(2, ("Tendance", lambda x: "contre-tendance" if x["counter_trend"] else "dans la tendance"))
    if any(x.get("zone") for x in trades):    # Impulsion
        sections.append(("Zone de retracement", lambda x: x.get("zone") or "n/d"))
    if any(x.get("ote") is not None for x in trades):    # CRT
        sections.append(("Entrée dans l'OTE (61,8-79 %)", lambda x: "oui" if x.get("ote") else "non"))
    if any(x.get("trend_age") for x in trades):
        sections.append(("Âge de la tendance", lambda x: x.get("trend_age") or "n/d"))
    age_order = ["naissante", "établie", "mûre", "n/d"]
    for title, key in sections:
        L += ["", title]
        groups = {key(x) for x in trades}
        for g in (sorted(groups, key=age_order.index) if title.startswith("Âge") else sorted(groups)):
            L.append("  " + group_line(g, [x for x in trades if key(x) == g], win))
    L += ["", "Meilleurs et pires symboles (résolus, 5 trades minimum)"]
    by_sym = {}
    for x in trades:
        if x["result"] in ("TP", "SL"):
            by_sym.setdefault(x["symbol"], []).append(x)
    ranked = sorted(((sum(r_of(x) for x in v) / len(v), k, v) for k, v in by_sym.items() if len(v) >= 5),
                    reverse=True)
    for r, k, v in (ranked[:5] + ranked[-5:] if len(ranked) > 10 else ranked):
        L.append(f"  {k:<12} {len(v):>3} trades, R moyen {r:+.2f}")
    L += ["", "Résultats théoriques : R net = R brut moins un coût estimé (spread + glissement, en R) ; SL prioritaire si SL et",
          "cible touchés dans la même bougie ; R géré = moitié à TP1, SL au point d'entrée, reste vers TP2 (CRT). Un backtest",
          "ne garantit rien pour la suite."]
    return "\n".join(L)


def write_csv(path, trades):
    iso = lambda e: datetime.fromtimestamp(e, timezone.utc).strftime("%Y-%m-%d %H:%M") if e else ""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["symbole", "reference", "confirmation", "sens", "ordre", "alerte_utc", "entree", "sl",
                    "tp1", "tp2", "ratio_cible", "cible_de_gain", "mode_crt", "tendance", "age_tendance", "zone", "ote", "mode_tendance", "contre_tendance", "resultat", "tp1_atteint",
                    "resolu_utc", "R"])
        for x in trades:
            w.writerow([x["symbol"], x["ref_tf"], x["confirmation_tf"], x["direction"], x["order_type"],
                        iso(x["alert_epoch"]), x["entry"], x["stop_loss"], x["tp1"], x["tp2"], x["risk_reward"],
                        x["win_tp"].upper(), x.get("crt_mode", ""), x.get("trend") or "", x.get("trend_age") or "", x.get("zone") or "",
                        "" if x.get("ote") is None else int(bool(x["ote"])), x.get("trend_mode", ""), int(x["counter_trend"]), x["result"], int(x["tp1_hit"]),
                        iso(x.get("resolved_epoch")),
                        round(r_of(x), 2) if x["result"] in ("TP", "SL") else ""])


def run_modes(api, strat, symbols, days, modes, log=print):
    """Rejoue chaque symbole une fois par mode de tendance, sur le même historique téléchargé une seule fois.
    Retourne {mode: trades}."""
    start = int(time.time()) - days * 86400
    original = bot.TREND_MODE
    out = {m: [] for m in modes}
    try:
        for n, sym in enumerate(symbols, 1):
            hist = fetch_history(api, sym, days, strat["tfs"])
            if not hist:
                log(f"[{n}/{len(symbols)}] {sym} : historique indisponible, ignoré")
                continue
            counts = []
            for m in modes:
                bot.TREND_MODE = m
                got = replay(strat, sym, hist, start)
                for t in got:
                    t["trend_mode"] = m
                out[m] += got
                counts.append(f"{m} {len(got)}")
            log(f"[{n}/{len(symbols)}] {sym} : " + " | ".join(counts) + " setup(s)")
    finally:
        bot.TREND_MODE = original
    return out


def run(api, strat, symbols, days, log=print):
    """Un seul mode : celui de bot.TREND_MODE. Retourne la liste des trades."""
    return run_modes(api, strat, symbols, days, [bot.TREND_MODE], log)[bot.TREND_MODE]


def compare(strat, by_mode):
    """Tableau comparatif des modes de tendance, sur le même historique."""
    L = ["COMPARAISON DES LECTURES DE TENDANCE (même historique)", ""]
    L.append(f"{'mode':<12} {'setups':>6} {'résolus':>8} {'réussite':>9} {'R net moy':>8} {'somme R net':>8}   contre-tendance")
    for m, trs in by_mode.items():
        res = [x for x in trs if x["result"] in ("TP", "SL")]
        wins = sum(1 for x in res if x["result"] == "TP")
        ct = [x for x in res if x["counter_trend"]]
        ct_txt = f"{len(ct)} trades, R moyen {sum(r_of(x) for x in ct) / len(ct):+.2f}" if ct else "aucun"
        if res:
            L.append(f"{m:<12} {len(trs):>6} {len(res):>8} {100 * wins / len(res):>8.1f}% "
                     f"{sum(r_net(x) for x in res) / len(res):>+8.2f} {sum(r_net(x) for x in res):>+8.1f}   {ct_txt}")
        else:
            L.append(f"{m:<12} {len(trs):>6} {0:>8} {'-':>9} {'-':>8} {'-':>8}   {ct_txt}")
    if strat["label"] == "CRT":
        L += ["", "Pour le CRT, la tendance n'est qu'une étiquette : les setups sont les mêmes dans les deux modes,",
              "seule la colonne contre-tendance change. Pour l'Impulsion, la tendance est un filtre : le nombre",
              "de setups change."]
    return "\n".join(L)


def run_variants(api, strat, symbols, days, variants, log=print):
    """Rejoue chaque symbole une fois par variante CRT (loose, ote, mid, ote_confirm), sur le même historique téléchargé
    une seule fois. Retourne {variante: trades}."""
    global CRT_VARIANT
    start = int(time.time()) - days * 86400
    original = CRT_VARIANT
    out = {v: [] for v in variants}
    try:
        for n, sym in enumerate(symbols, 1):
            hist = fetch_history(api, sym, days, strat["tfs"])
            if not hist:
                log(f"[{n}/{len(symbols)}] {sym} : historique indisponible, ignoré")
                continue
            counts = []
            for v in variants:
                CRT_VARIANT = v
                got = replay(strat, sym, hist, start)
                out[v] += got
                counts.append(f"{v} {len(got)}")
            log(f"[{n}/{len(symbols)}] {sym} : " + " | ".join(counts) + " setup(s)")
    finally:
        CRT_VARIANT = original
    return out


def compare_variants(by_variant):
    """Tableau comparatif des variantes CRT : remplissage, réussite et R net, par trade et par alerte."""
    L = ["COMPARAISON DES VARIANTES CRT (même historique, spread et glissement estimés déduits)", "",
         f"{'variante':<12} {'alertes':>7} {'remplis':>8} {'résolus':>8} {'réussite':>9} {'R net/trade':>11} "
         f"{'R géré net':>10} {'R net/alerte':>12}"]
    for v, trs in by_variant.items():
        res = [x for x in trs if x["result"] in ("TP", "SL")]
        filled, decided = fill_stats(trs)
        fill = f"{100 * filled / decided:.0f} %" if decided else "-"
        if res:
            wins = sum(1 for x in res if x["result"] == "TP")
            L.append(f"{v:<12} {len(trs):>7} {fill:>8} {len(res):>8} {100 * wins / len(res):>8.1f}% "
                     f"{sum(r_net(x) for x in res) / len(res):>+11.2f} "
                     f"{sum(r_managed_net(x) for x in res) / len(res):>+10.2f} "
                     f"{sum(r_net(x) for x in res) / len(trs):>+12.2f}")
        else:
            L.append(f"{v:<12} {len(trs):>7} {fill:>8} {0:>8} {'-':>9} {'-':>11} {'-':>10} {'-':>12}")
    L += ["", "R net/trade : gain moyen d'un trade résolu. R net/alerte : somme des R nets divisée par TOUTES les alertes",
          "(un ordre jamais rempli compte pour 0) : c'est la mesure qui permet de comparer des variantes dont le taux de",
          "remplissage diffère. R géré net : moitié à TP1, SL au point d'entrée, reste vers TP2. Peu de trades = peu de",
          "signification : compare aussi les deux moitiés de la période (--oos) avant de conclure."]
    return "\n".join(L)


def main():
    p = argparse.ArgumentParser(description="Backtest des stratégies CRT et Impulsion sur l'historique Deriv")
    p.add_argument("--strategy", choices=sorted(STRATEGIES), default="crt", help="crt (défaut) ou impulse")
    p.add_argument("--days", type=int, default=30, help="durée rejouée (45 jours maximum pour le CRT, 16 pour l'Impulsion)")
    p.add_argument("--symbols", default="", help="liste séparée par des virgules (défaut : tous les symboles de la stratégie)")
    p.add_argument("--out", default="", help="fichier CSV des trades (défaut : backtest_<stratégie>.csv)")
    p.add_argument("--trend-mode", choices=list(bot.TREND_MODES) + ["all"], default=bot.TREND_MODE,
                   help="lecture de tendance : structure (précoce), deplacement (structure + cassure impulsive), "
                        "swings (3 swings + SMA 50) ou all (comparaison des trois)")
    p.add_argument("--crt-mode", choices=list(bot.STRICT_ENTRIES) + ["loose", "all"], default=bot.current_variant(),
                   help="CRT : loose (mode historique), ote / mid / ote_confirm (CRT strict, C2 clôturée) "
                        "ou all (compare les quatre sur le même historique)")
    p.add_argument("--no-costs", action="store_true", help="ignore le spread et le glissement estimés (R brut = R net)")
    p.add_argument("--oos", type=float, default=0.3,
                   help="part finale de la période isolée pour juger la stabilité (0 pour désactiver, défaut 0,3)")
    a = p.parse_args()
    global COSTS
    COSTS = not a.no_costs
    strat = STRATEGIES[a.strategy]
    days = max(1, min(a.days, strat["max_days"]))
    if days != a.days:
        print(f"Durée ramenée à {days} jours (5000 bougies par requête Deriv).")
    symbols = [s.strip() for s in a.symbols.split(",") if s.strip()] or strat["symbols"]
    unknown = [s for s in symbols if s not in strat["symbols"]]
    if unknown:
        sys.exit(f"Symboles inconnus pour la stratégie {a.strategy} : {unknown}")
    if bot.NY_ALIGNED and a.strategy == "crt":
        print("NY_ALIGNED ignoré : le backtest utilise les bougies natives de Deriv.")
    out = a.out or f"backtest_{a.strategy}.csv"
    global CRT_VARIANT
    if a.strategy == "crt" and a.crt_mode == "all":
        variants = ["loose"] + list(bot.STRICT_ENTRIES)
        api = bot.Deriv()
        try:
            by_variant = run_variants(api, strat, symbols, days, variants)
        finally:
            api.close()
        texts = [f"[{bot.variant_label(v)}]\n{summary(strat, by_variant[v], days, a.oos, label=bot.variant_label(v))}"
                 for v in variants]
        texts.append(compare_variants(by_variant))
        text = "\n\n".join(texts)
        print("\n" + text)
        write_csv(out, [x for v in variants for x in by_variant[v]])
    else:
        if a.strategy == "crt":
            CRT_VARIANT = a.crt_mode
        modes = list(bot.TREND_MODES) if a.trend_mode == "all" else [a.trend_mode]
        api = bot.Deriv()
        try:
            by_mode = run_modes(api, strat, symbols, days, modes)
        finally:
            api.close()
        texts = []
        label = bot.variant_label(CRT_VARIANT) if a.strategy == "crt" else None
        for m in modes:
            t = summary(strat, by_mode[m], days, a.oos, label=label)
            texts.append(f"[tendance : {m}]\n{t}")
        if len(modes) > 1:
            texts.append(compare(strat, by_mode))
        text = "\n\n".join(texts)
        print("\n" + text)
        write_csv(out, [x for m in modes for x in by_mode[m]])
    print(f"\nTrades détaillés : {out}")
    gh = os.getenv("GITHUB_STEP_SUMMARY")
    if gh:
        with open(gh, "a", encoding="utf-8") as f:
            f.write("```\n" + text + "\n```\n")


if __name__ == "__main__":
    main()
