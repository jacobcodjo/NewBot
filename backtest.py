"""Backtest du CRT et de l'Impulsion : rejoue la stratégie sur de l'historique M5.

Le rejeu appelle les mêmes fonctions de scan que le bot en direct (scan_symbol), les mêmes
garde-fous (guard.risk_check, fenêtres sans alerte) et la même gestion des trades (tracker.advance).
Toutes les unités de temps sont reconstruites depuis le M5, sans regarder le futur : à chaque pas,
seules les bougies clôturées sont visibles et la bougie en formation est partielle.

Exemples :
  python backtest.py --sym R_75 --source deriv --days 60 --spread-pct 0.01
  python backtest.py --sym frxEURUSD --strategy crt --source csv --file eurusd_m5.csv
  python backtest.py --sym R_75 --source sim --days 90 --seed 3     (marche aléatoire : contrôle de bon sens)
Un CSV contient les colonnes epoch,open,high,low,close (bougies M5, epoch en secondes, ordre croissant).
Limites : spread constant, pas de glissement, un fichier = un actif, news.json non appliqué.
"""
import argparse, csv, math, random, sys, time
from collections import Counter
from datetime import datetime, timedelta

import bot
import guard as G
import impulse
import tracker as T

M5 = 300


# --------------------------------------------------------------------------
# Sources de données
# --------------------------------------------------------------------------
def load_csv(path):
    out = []
    with open(path, newline="") as f:
        for row in csv.reader(f):
            try:
                e, o, h, l, c = (float(x) for x in row[:5])
            except ValueError:
                continue  # en-tête
            out.append({"epoch": int(e), "open": o, "high": h, "low": l, "close": c})
    out.sort(key=lambda x: x["epoch"])
    return out


def load_deriv(sym, days):
    """Télécharge ~days jours de M5 en remontant par blocs de 5000 bougies."""
    api, out, end = bot.Deriv(), {}, "latest"
    target = int(days * 288)
    try:
        while len(out) < target:
            cs = api.candles(sym, M5, count=5000, end=end)
            if not cs:
                break
            new = {c["epoch"]: c for c in cs}
            if not set(new) - set(out):
                break
            out.update(new)
            end = min(new) - 1
            print(f"  {len(out)} bougies M5 téléchargées…")
    finally:
        api.close()
    return [out[k] for k in sorted(out)][-target:]


def gen_sim(days, seed, vol=0.0004, sub=10, start=1_700_000_000):
    """Marche aléatoire à volatilité constante (aucune structure de marché à exploiter)."""
    r, p, out = random.Random(seed), 1000.0, []
    start -= start % M5
    for i in range(int(days * 288)):
        o, hi, lo = p, p, p
        for _ in range(sub):
            p *= math.exp(r.gauss(0, vol / math.sqrt(sub)))
            hi, lo = max(hi, p), min(lo, p)
        out.append({"epoch": start + i * M5, "open": o, "high": hi, "low": lo, "close": p})
    return out


# --------------------------------------------------------------------------
# Rejeu : bougies de n'importe quel timeframe à l'instant t, sans look-ahead
# --------------------------------------------------------------------------
class Replay:
    def __init__(self, m5, sym):
        self.m5, self.sym, self.n, self._b = m5, sym, len(m5), {}

    def _start(self, tf, epoch):
        g = bot.GRAN[tf]
        if bot.NY_ALIGNED and not bot.is_247(self.sym) and tf in ("D1", "H4"):
            hours = 24 if tf == "D1" else 4
            dt = datetime.fromtimestamp(epoch, bot.NY)
            h = (dt.hour - 17) % 24
            return int((dt.replace(minute=0, second=0, microsecond=0)
                        - timedelta(hours=h % hours)).timestamp())
        return epoch - epoch % g

    def _build(self, tf):
        buckets, bid, last = [], [], None
        for i, c in enumerate(self.m5):
            st = self._start(tf, c["epoch"])
            if st != last:
                buckets.append({"epoch": st, "open": c["open"], "high": c["high"],
                                "low": c["low"], "close": c["close"], "i0": i})
                last = st
            else:
                b = buckets[-1]
                b["high"], b["low"], b["close"] = max(b["high"], c["high"]), min(b["low"], c["low"]), c["close"]
            bid.append(len(buckets) - 1)
        self._b[tf] = (buckets, bid)

    def provide(self, tf, t, size=199):
        """Bougies du timeframe `tf` quand la bougie M5 d'index t vient de clôturer :
        `size` bougies clôturées + la bougie en formation (partielle ou vide)."""
        if tf not in self._b:
            self._build(tf)
        buckets, bid = self._b[tf]
        nxt = bid[t + 1] if t + 1 < self.n else bid[t]
        closed = buckets[max(0, nxt - size):nxt]
        if nxt == bid[t]:
            part = self.m5[buckets[nxt]["i0"]:t + 1]
            forming = {"epoch": buckets[nxt]["epoch"], "open": part[0]["open"],
                       "high": max(x["high"] for x in part), "low": min(x["low"] for x in part),
                       "close": part[-1]["close"]}
        else:
            px = self.m5[t]["close"]
            forming = {"epoch": buckets[nxt]["epoch"], "open": px, "high": px, "low": px, "close": px}
        return closed + [forming]


# --------------------------------------------------------------------------
# Exécution du backtest
# --------------------------------------------------------------------------
def run(m5, sym, strategy, spread_pct=0.0, warmup_days=15, progress=True):
    rp, funnel, sent = Replay(m5, sym), Counter(), {}
    tr = {"active": {}, "closed": []}
    warm = min(int(warmup_days * 288), len(m5) // 3)
    stats = Counter()
    scan = bot.scan_symbol if strategy == "crt" else impulse.scan_symbol
    t_start = time.time()
    for t in range(warm, len(m5) - 1):
        now = m5[t]["epoch"] + M5
        # 1) fait avancer les trades avec la bougie M5 qui vient de clôturer
        for key, tk in list(tr["active"].items()):
            tk["gran"] = M5
            for e in T.advance(tk, [m5[t]], now):
                stats[e] += 1
            if tk["status"] not in T.ACTIVE:
                T.archive(tr, key)
        # 2) cherche de nouveaux setups
        provide = lambda tf, force=False, _t=t: rp.provide(tf, _t)
        if strategy == "crt":
            alerts = scan(sym, provide, now, sent, funnel, (), align=True)
        else:
            alerts = scan(sym, provide, now, sent, funnel, align=True)
        for a in alerts:
            sent[a["key"]] = now
            ok, why = G.risk_check(tr, a["sym"], a["dir"], now)
            if not ok:
                funnel["dedupe" if why == "dedupe" else "risk"] += 1
                continue
            funnel["alert"] += 1
            tk = T.new_trade(a["bot"], a["key"], a["sym"], a["name"], a["dir"], a["otype"], a["entry"],
                             a["sl"], a["tp1"], a["tp2"], a["tp3"], a["ltf"], M5, a["dec"],
                             a["cascade"], now, a["ttl"],
                             extra={"age": a.get("age"), "session": a.get("session"),
                                    "cost_r": (spread_pct / 100 * a["entry"]) / abs(a["entry"] - a["sl"])})
            tr["active"][a["key"]] = tk
        if progress and (t - warm) % 5000 == 0:
            print(f"  pas {t - warm}/{len(m5) - warm - 1} · {funnel['alert']} alertes · "
                  f"{time.time() - t_start:.0f}s", flush=True)
    for tk in tr["closed"]:                       # coût du spread, en R
        tk["r"] = round(tk["r"] - tk.get("cost_r", 0.0), 3)
    return tr, funnel, stats


def breakdown(ts, key):
    rows = []
    for val in sorted({str(t.get(key)) for t in ts if t.get(key) is not None}):
        s = T.summarize([t for t in ts if str(t.get(key)) == val])
        if s:
            rows.append(T.fmt_summary(f"  {key} {val}", s))
    return rows


def report(tr, funnel, stats, sym, strategy, days, spread_pct):
    """Construit le rapport (liste de lignes) ; main() l'affiche et l'envoie si --telegram."""
    ts = tr["closed"]
    L = [f"BACKTEST {strategy.upper()} · {sym} · {days:g} jours · spread {spread_pct:g} %",
         "Entonnoir : " + G.funnel_text(funnel),
         f"Ordres : {stats['filled']} exécutés, {stats['cancel_tp'] + stats['cancel_sl']} annulés, "
         f"{stats['expired']} expirés ; {len(T.active_list(tr))} trade(s) encore ouvert(s)/en attente"]
    s = T.summarize(ts)
    if not s:
        L.append("Aucun trade clôturé : pas assez de signaux sur cette période.")
        return L
    L.append(T.fmt_summary("TOTAL", s))
    for key in ("cascade", "dir", "age", "session", "result"):
        L += breakdown(ts, key)
    L += ["", f"Lecture : sous {s['n']} trades, les chiffres sont très bruités "
              f"(±{100 * math.sqrt(0.25 / s['n']):.0f} points sur le taux de gain est normal).",
          "Un avantage réel se voit sur plusieurs centaines de trades, sur plusieurs actifs et périodes."]
    return L


def main():
    ap = argparse.ArgumentParser(description="Backtest CRT / Impulsion sur historique M5")
    ap.add_argument("--sym", default="R_75")
    ap.add_argument("--strategy", choices=["auto", "crt", "impulse"], default="auto")
    ap.add_argument("--source", choices=["deriv", "csv", "sim"], default="deriv")
    ap.add_argument("--file", help="CSV M5 (source csv)")
    ap.add_argument("--days", type=float, default=60)
    ap.add_argument("--spread-pct", type=float, default=0.0, help="spread aller-retour en %% du prix")
    ap.add_argument("--warmup-days", type=float, default=15)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--vol", type=float, default=0.0004, help="volatilité M5 de la marche aléatoire (sim)")
    ap.add_argument("--telegram", action="store_true", help="envoie aussi le rapport sur Telegram")
    a = ap.parse_args()
    strategy = a.strategy if a.strategy != "auto" else ("impulse" if bot.is_synthetic(a.sym) else "crt")
    if a.source == "csv":
        if not a.file:
            sys.exit("--file requis avec --source csv")
        m5 = load_csv(a.file)
    elif a.source == "sim":
        m5 = gen_sim(a.days + a.warmup_days, a.seed, a.vol)
    else:
        print(f"Téléchargement de {a.days:g} jours de M5 pour {a.sym}…")
        m5 = load_deriv(a.sym, a.days + a.warmup_days)
    if len(m5) < 2000:
        sys.exit(f"Historique insuffisant ({len(m5)} bougies M5).")
    print(f"{len(m5)} bougies M5 ({len(m5) / 288:.0f} jours) · stratégie {strategy}")
    tr, funnel, stats = run(m5, a.sym, strategy, a.spread_pct, a.warmup_days)
    lines = report(tr, funnel, stats, a.sym, strategy, len(m5) / 288, a.spread_pct)
    print("\n" + "=" * 60 + "\n" + "\n".join(lines))
    if a.telegram:
        bot.notify("\n".join(lines)[:4000])


if __name__ == "__main__":
    main()
