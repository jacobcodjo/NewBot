"""Suivi automatique des trades (CRT et Impulsion), repris de l'ancien crt-bot et adapté.

Chaque alerte envoyée est enregistrée dans un fichier de statistiques (trade_stats.json pour le CRT,
trade_stats_impulse.json pour l'Impulsion). Un ordre en attente (LIMIT/STOP) doit d'abord être rempli
(le prix atteint l'entrée) ; un ordre au marché est rempli dès l'alerte. Une fois rempli, le premier
niveau touché décide du résultat : SL (perdant) ou cible de gain (gagnant). La cible de gain est TP2
pour le CRT (le ratio de 1 pour 3 est calculé sur TP2) et TP1 pour l'Impulsion (idem sur TP1) : champ
`win` du setup. Si SL et cible sont touchés dans la même bougie, le SL est retenu ; sur la bougie de
remplissage, seul le SL compte. Seules les bougies qui s'ouvrent après l'alerte comptent.
Chaque trade mémorise la dernière bougie évaluée (`resume_epoch`) : il n'a donc pas besoin que
l'historique récupéré remonte jusqu'à son ouverture. Ce module ne dépend pas de bot.py
(usage partagé avec backtest.py).
"""
import json, time

STATS_FILE = "trade_stats.json"
PENDING_MAX_AGE_DAYS = 7           # ordre en attente jamais rempli après 7 jours -> EXPIRE (par défaut)
TRADE_MAX_OPEN_DAYS = 30           # trade rempli toujours ouvert après 30 jours -> NON_RESOLU
TRADE_HISTORY_MAX_AGE_DAYS = 180   # historique plus ancien purgé
MARKET_ORDERS = ("BUY", "SELL")


def load_stats(path=STATS_FILE):
    try:
        with open(path) as f:
            stats = json.load(f)
    except Exception:
        stats = {}
    stats.setdefault("pending", {})
    stats.setdefault("history", [])
    return stats


def save_stats(stats, path=STATS_FILE):
    with open(path, "w") as f:
        json.dump(stats, f, indent=1)


def new_trade(setup, alert_epoch):
    """Trade à suivre, construit depuis un setup (clés : sym, dir, ref_n, conf_n, counter, trade{...}).
    Champs optionnels du setup : win ('tp1' ou 'tp2', défaut tp2), max_age (secondes avant EXPIRE)."""
    t = setup["trade"]
    market = t["otype"] in MARKET_ORDERS
    return {
        "symbol": setup["sym"], "direction": setup["dir"],
        "ref_tf": setup["ref_n"], "confirmation_tf": setup["conf_n"],
        "order_type": t["otype"], "entry": t["entry"], "stop_loss": t["sl"],
        "tp1": t["tp1"], "tp2": t["tp2"], "risk_reward": round(t["rr"], 2),
        "win_tp": setup.get("win", "tp2"),
        "max_age": setup.get("max_age", PENDING_MAX_AGE_DAYS * 86400),
        "counter_trend": bool(setup.get("counter")),
        "trend": setup.get("trend"), "trend_age": setup.get("trend_age"),
        "zone": setup.get("zone"), "ote": t.get("ote"),
        "alert_epoch": alert_epoch,
        "filled": market, "fill_epoch": alert_epoch if market else None,
        "tp1_hit": False,
    }


def add_pending(stats, key, setup, alert_epoch):
    stats["pending"][key] = new_trade(setup, alert_epoch)


def _start(trade, floor):
    return max(floor, trade.get("resume_epoch", 0))


def _fill_epoch(trade, candles):
    """Epoch de la première bougie qui atteint l'entrée d'un ordre en attente, sinon None."""
    entry, kind = trade["entry"], trade["order_type"]
    start = _start(trade, trade["alert_epoch"])
    limit = trade["alert_epoch"] + trade.get("max_age", PENDING_MAX_AGE_DAYS * 86400)
    for c in candles:
        if c["epoch"] < start:
            continue
        if c["epoch"] > limit:
            break
        if kind in ("BUY LIMIT", "SELL STOP"):
            hit = c["low"] <= entry
        elif kind in ("SELL LIMIT", "BUY STOP"):
            hit = c["high"] >= entry
        else:
            hit = True
        if hit:
            return c["epoch"]
    return None


def resolve_trade(trade, candles):
    """Fait avancer un trade avec les bougies disponibles. Retourne 'TP', 'SL' ou None."""
    if not candles:
        return None
    if not trade["filled"]:
        fe = _fill_epoch(trade, candles)
        if fe is None:
            trade["resume_epoch"] = candles[-1]["epoch"]
            return None
        trade["filled"], trade["fill_epoch"], trade["fill_candle"] = True, fe, True
    buy = trade["direction"] == "BUY"
    win_level = trade[trade.get("win_tp", "tp2")]
    start = _start(trade, trade["fill_epoch"])
    for c in candles:
        if c["epoch"] < start:
            continue
        on_fill = trade.get("fill_candle") and c["epoch"] == trade["fill_epoch"]
        sl = c["low"] <= trade["stop_loss"] if buy else c["high"] >= trade["stop_loss"]
        if sl:
            trade["resolved_epoch"] = c["epoch"]
            return "SL"
        if on_fill:
            continue  # bougie de remplissage : l'ordre des touches est inconnu, seul le SL est retenu
        win = c["high"] >= win_level if buy else c["low"] <= win_level
        tp1 = c["high"] >= trade["tp1"] if buy else c["low"] <= trade["tp1"]
        if tp1 or win:
            trade["tp1_hit"] = True
        if win:
            trade["resolved_epoch"] = c["epoch"]
            return "TP"
    trade["resume_epoch"] = candles[-1]["epoch"]
    return None


def realized_r(h):
    """Gain en R : -1 pour un SL, ratio de la cible de gain pour un TP."""
    return -1.0 if h["result"] == "SL" else h["risk_reward"]


def _archive(stats, trade, result, when):
    stats["history"].append({
        "symbol": trade["symbol"], "direction": trade["direction"], "ref_tf": trade["ref_tf"],
        "order_type": trade["order_type"], "counter_trend": trade["counter_trend"],
        "risk_reward": trade["risk_reward"], "tp1_hit": trade["tp1_hit"],
        "win_tp": trade.get("win_tp", "tp2"),
        "zone": trade.get("zone"), "ote": trade.get("ote"),
        "result": result, "alert_epoch": trade["alert_epoch"], "resolved_epoch": when,
    })


def resolve_pending(stats, candles_lookup):
    """candles_lookup(symbol, confirmation_tf) -> bougies ou None. Retourne les clés archivées."""
    done = []
    for key, trade in stats["pending"].items():
        candles = candles_lookup(trade["symbol"], trade["confirmation_tf"])
        if not candles:
            continue
        seen = trade.get("resume_epoch")
        if seen and candles[0]["epoch"] > seen:   # le bot est resté arrêté plus longtemps que l'historique
            _archive(stats, trade, "NON_RESOLU", time.time())
            done.append(key)
            continue
        result = resolve_trade(trade, candles)
        if result:
            _archive(stats, trade, result, trade["resolved_epoch"])
            done.append(key)
    for key in done:
        del stats["pending"][key]
    return done


def expire_stale_pending(stats):
    """EXPIRE : ordre jamais rempli après son délai. NON_RESOLU : trade rempli, ouvert depuis trop longtemps."""
    now, n = time.time(), 0
    for key, t in list(stats["pending"].items()):
        age = now - t["alert_epoch"]
        if not t["filled"] and age > t.get("max_age", PENDING_MAX_AGE_DAYS * 86400):
            _archive(stats, stats["pending"].pop(key), "EXPIRE", now)
            n += 1
        elif t["filled"] and age > TRADE_MAX_OPEN_DAYS * 86400:
            _archive(stats, stats["pending"].pop(key), "NON_RESOLU", now)
            n += 1
    return n


def prune_history(stats):
    cutoff = time.time() - TRADE_HISTORY_MAX_AGE_DAYS * 86400
    before = len(stats["history"])
    stats["history"] = [h for h in stats["history"] if h.get("resolved_epoch", time.time()) >= cutoff]
    return before - len(stats["history"])


def asset_class(symbol):
    if symbol.startswith("frx"):
        return "Forex/Or"
    if symbol.startswith("cry"):
        return "Crypto"
    return "Synthétiques"


def _rate(trades):
    if not trades:
        return None
    wins = sum(1 for t in trades if t["result"] == "TP")
    return round(100 * wins / len(trades), 1), len(trades)


def summarize(stats, name="CRT", win="tp2"):
    """Résumé texte (logs et message de test) : réussite, R moyen, contre-tendance, classe d'actif,
    référence, taux de remplissage. Les EXPIRE et NON_RESOLU sont comptés à part. `win` : cible de
    gain de la stratégie (tp2 pour le CRT, tp1 pour l'Impulsion) ; TP1 n'est affiché à part que pour tp2."""
    history = stats.get("history", [])
    resolved = [h for h in history if h["result"] in ("TP", "SL")]
    expired = [h for h in history if h["result"] == "EXPIRE"]
    unresolved = [h for h in history if h["result"] == "NON_RESOLU"]
    n_pending = len(stats.get("pending", {}))
    head = f"Suivi {name} : "
    if not resolved and not expired:
        return head + f"aucun trade résolu pour le moment ({n_pending} en cours)."
    parts = []
    if resolved:
        wr = _rate(resolved)
        r_avg = sum(realized_r(h) for h in resolved) / len(resolved)
        g = f"Global : {wr[0]}% de réussite sur {wr[1]} trades, R moyen {r_avg:+.2f}"
        if win == "tp2":
            g += f", TP1 atteint {100 * sum(1 for h in resolved if h.get('tp1_hit')) / len(resolved):.0f}%"
        parts.append(g)
        ct = [h for h in resolved if h["counter_trend"]]
        if ct:  # sans setup à contre-tendance (Impulsion), la comparaison n'a pas de sens
            for label, sel in (("Contre-tendance", ct), ("Dans la tendance", [h for h in resolved if not h["counter_trend"]])):
                r = _rate(sel)
                if r:
                    parts.append(f"{label} : {r[0]}% sur {r[1]}")
        for cls in ("Forex/Or", "Crypto", "Synthétiques"):
            r = _rate([h for h in resolved if asset_class(h["symbol"]) == cls])
            if r:
                parts.append(f"{cls} : {r[0]}% sur {r[1]}")
        for zone in sorted({h.get("zone") for h in resolved if h.get("zone")}):   # Impulsion : Golden Zone / OTE
            r = _rate([h for h in resolved if h.get("zone") == zone])
            parts.append(f"Zone {zone} : {r[0]}% sur {r[1]}")
        for label, val in (("dans l'OTE", True), ("hors OTE", False)):                # CRT : entrée dans l'OTE ou non
            r = _rate([h for h in resolved if h.get("ote") is val])
            if r and any(h.get("ote") is not None and h["ote"] is not val for h in resolved):
                parts.append(f"Entrée {label} : {r[0]}% sur {r[1]}")
        for ref in sorted({h.get("ref_tf") for h in resolved if h.get("ref_tf")}):
            r = _rate([h for h in resolved if h.get("ref_tf") == ref])
            parts.append(f"Référence {ref} : {r[0]}% sur {r[1]}")
    if expired:
        total = len(resolved) + len(expired)
        parts.append(f"Remplissage : {round(100 * len(resolved) / total, 1)}% "
                     f"({len(resolved)} résolus / {len(expired)} expirés)")
    if unresolved:
        parts.append(f"{len(unresolved)} non résolu(s)")
    parts.append(f"{n_pending} en cours")
    return head + " | ".join(parts)
