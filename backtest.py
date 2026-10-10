"""Backtest des stratégies CRT et Impulsion : rejoue l'historique Deriv comme le bot le verrait en direct.

À chaque clôture de bougie du timeframe de pas (M15 pour le CRT, M5 pour l'Impulsion), le script
reconstruit ce que le bot aurait vu (bougies clôturées + une bougie en formation neutre), appelle
l'analyse de la stratégie (bot.analyze ou impulse.analyze), applique les mêmes verrous anti-doublon,
puis suit chaque alerte avec tracker.py sur les bougies suivantes du timeframe de pas (plus fines que
le timeframe de confirmation, donc moins de cas SL/TP dans la même bougie qu'en direct).

Usage : python backtest.py [--strategy crt|impulse] [--days 30] [--symbols R_75,frxEURUSD] [--out fichier.csv]
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


def crt_setups(sym, series, t):
    htf = {tf: series[tf].view(t) for tf in ("D1", "H4")}
    if not htf["D1"] and not htf["H4"]:
        return []
    ltf = {tf: v for tf in CRT_CONF if (v := series[tf].view(t))}
    return bot.analyze(sym, htf, ltf)


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


def r_of(tr):
    return -1.0 if tr["result"] == "SL" else tr["risk_reward"]


def group_line(name, trs, win):
    res = [x for x in trs if x["result"] in ("TP", "SL")]
    if not res:
        return f"{name:<22} {len(trs):>4} setups, aucun résolu"
    wins = sum(1 for x in res if x["result"] == "TP")
    line = (f"{name:<22} {len(trs):>4} setups | {len(res):>4} résolus | réussite {100*wins/len(res):5.1f}% "
            f"| R moyen {sum(r_of(x) for x in res) / len(res):+.2f}")
    if win == "TP2":
        line += f" | TP1 {100 * sum(1 for x in res if x['tp1_hit']) / len(res):5.1f}%"
    return line


def summary(strat, trades, days):
    win = strat["win"]
    if not trades:
        return "Aucun setup détecté sur la période."
    L = [f"BACKTEST {strat['label'].upper()} : {days} jours, {len(trades)} setups (gagnant = {win} atteint avant le SL)", ""]
    L.append(group_line("Global", trades, win))
    counts = {k: sum(1 for x in trades if x["result"] == k) for k in ("EXPIRE", "OUVERT", "EN_ATTENTE")}
    L.append(f"Non résolus : {counts['EXPIRE']} expirés (jamais remplis), {counts['OUVERT']} ouverts, "
             f"{counts['EN_ATTENTE']} en attente")
    sections = [
        ("Classe d'actif", lambda x: tracker.asset_class(x["symbol"])),
        ("Référence", lambda x: x["ref_tf"]),
        ("Ordre", lambda x: "au marché" if x["order_type"] in tracker.MARKET_ORDERS else "en attente"),
        ("Sens", lambda x: x["direction"]),
    ]
    if any(x["counter_trend"] for x in trades):   # sans objet pour l'Impulsion (toujours dans la tendance)
        sections.insert(2, ("Tendance", lambda x: "contre-tendance" if x["counter_trend"] else "dans la tendance"))
    for title, key in sections:
        L += ["", title]
        for g in sorted({key(x) for x in trades}):
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
    L += ["", "Résultats théoriques : sans spread ni glissement, SL prioritaire si SL et cible touchés dans la",
          "même bougie. Un backtest ne garantit rien pour la suite."]
    return "\n".join(L)


def write_csv(path, trades):
    iso = lambda e: datetime.fromtimestamp(e, timezone.utc).strftime("%Y-%m-%d %H:%M") if e else ""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["symbole", "reference", "confirmation", "sens", "ordre", "alerte_utc", "entree", "sl",
                    "tp1", "tp2", "ratio_cible", "cible_de_gain", "contre_tendance", "resultat", "tp1_atteint",
                    "resolu_utc", "R"])
        for x in trades:
            w.writerow([x["symbol"], x["ref_tf"], x["confirmation_tf"], x["direction"], x["order_type"],
                        iso(x["alert_epoch"]), x["entry"], x["stop_loss"], x["tp1"], x["tp2"], x["risk_reward"],
                        x["win_tp"].upper(), int(x["counter_trend"]), x["result"], int(x["tp1_hit"]),
                        iso(x.get("resolved_epoch")),
                        round(r_of(x), 2) if x["result"] in ("TP", "SL") else ""])


def run(api, strat, symbols, days, log=print):
    start = int(time.time()) - days * 86400
    trades = []
    for n, sym in enumerate(symbols, 1):
        hist = fetch_history(api, sym, days, strat["tfs"])
        if not hist:
            log(f"[{n}/{len(symbols)}] {sym} : historique indisponible, ignoré")
            continue
        got = replay(strat, sym, hist, start)
        trades += got
        log(f"[{n}/{len(symbols)}] {sym} : {len(got)} setup(s)")
    return trades


def main():
    p = argparse.ArgumentParser(description="Backtest des stratégies CRT et Impulsion sur l'historique Deriv")
    p.add_argument("--strategy", choices=sorted(STRATEGIES), default="crt", help="crt (défaut) ou impulse")
    p.add_argument("--days", type=int, default=30, help="durée rejouée (45 jours maximum pour le CRT, 16 pour l'Impulsion)")
    p.add_argument("--symbols", default="", help="liste séparée par des virgules (défaut : tous les symboles de la stratégie)")
    p.add_argument("--out", default="", help="fichier CSV des trades (défaut : backtest_<stratégie>.csv)")
    a = p.parse_args()
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
    api = bot.Deriv()
    try:
        trades = run(api, strat, symbols, days)
    finally:
        api.close()
    text = summary(strat, trades, days)
    print("\n" + text)
    write_csv(out, trades)
    print(f"\nTrades détaillés : {out}")
    gh = os.getenv("GITHUB_STEP_SUMMARY")
    if gh:
        with open(gh, "a", encoding="utf-8") as f:
            f.write("```\n" + text + "\n```\n")


if __name__ == "__main__":
    main()
