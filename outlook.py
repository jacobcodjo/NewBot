"""Récapitulatif hebdomadaire (repris de pa-bot) : biais directionnel D1 et W1 de chaque actif.

Envoyé une seule fois par week-end, dès la fermeture du forex (vendredi ~22h UTC), puis une mise à jour
des marchés 24/7 (cryptos, indices synthétiques) le dimanche à partir de 20h UTC, avant la réouverture
du forex. Ce n'est pas une stratégie : c'est la même lecture de tendance de fond que le tag
contre-tendance du CRT (bot.detect_trend : 3 sommets et 3 creux consécutifs + SMA 50), sans niveaux
d'entrée ni de sortie. Lancé par le workflow à chaque passage ; sans envoi dû, il se termine
immédiatement, sans appel à Deriv. Avec NOTIFY_OK=1 (test), il envoie le récapitulatif tout de suite,
sans toucher au marqueur.
"""
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import bot

MARKER_FILE = "weekly_outlook_state.json"
DISPLAY_TZ = "Africa/Lagos"          # fuseau d'affichage de l'heure (UTC+1 fixe, comme pa-bot)
D1_COUNT = 500                        # le W1 (SMA 50 semaines) demande bien plus que les 200 bougies du scan
OUTLOOK_INCLUDE_SYNTHETIC = True      # False : forex, or et cryptos seulement
REFRESH_HOUR_UTC = 20                 # mise à jour 24/7 du dimanche, à partir de cette heure
FOREX_CLOSE_UTC = (4, 22)             # (jour, heure) : vendredi 22h UTC
FOREX_OPEN_UTC = (6, 22)              # dimanche 22h UTC
MAX_MESSAGE = 3500                    # Telegram limite à 4096 caractères

LABEL = {"bullish": "🟢 Haussier", "bearish": "🔴 Baissier", "range": "⚪ Range", None: "⚪ Indéterminé"}


def is_forex_open(now):
    """Forex et or : fermés du vendredi ~22h UTC au dimanche ~22h UTC (crypto et synthétiques : 24/7)."""
    wd = now.weekday()
    if wd == 5:
        return False
    if wd == 6:
        return now.hour >= FOREX_OPEN_UTC[1]
    if wd == 4:
        return now.hour < FOREX_CLOSE_UTC[1]
    return True


def week_key(now):
    y, w, _ = now.isocalendar()
    return f"{y}-W{w:02d}"


def symbols_in_scope():
    return [s for s in bot.SYMBOLS if OUTLOOK_INCLUDE_SYNTHETIC or not bot.is_synthetic(s)]


def should_send(now, marker):
    """Vrai pendant la fermeture du week-end, si le récapitulatif de cette semaine ISO n'est pas encore parti."""
    return not is_forex_open(now) and marker.get("last_sent_week") != week_key(now)


def should_refresh(now, marker):
    """Vrai le dimanche à partir de 20h UTC, avant la réouverture du forex, une fois par semaine."""
    return (not is_forex_open(now) and now.weekday() == 6 and now.hour >= REFRESH_HOUR_UTC
            and marker.get("last_sent_refresh_week") != week_key(now))


def closed_d1(sym, cs, now):
    """Bougies D1 clôturées. Forex et or : la dernière bougie est définitive quand le marché est fermé."""
    now_e = int(now.timestamp())
    out = [c for c in cs if c["epoch"] + 86400 <= now_e]
    frozen = not bot.is_247(sym) and not is_forex_open(now)
    if frozen and cs and (not out or out[-1]["epoch"] != cs[-1]["epoch"]):
        out = list(cs)
    return out


def weekly_candles(cs):
    """Agrège des bougies D1 en bougies W1 (semaine ISO)."""
    weeks = {}
    for c in cs:
        weeks.setdefault(datetime.fromtimestamp(c["epoch"], tz=timezone.utc).isocalendar()[:2], []).append(c)
    return [{"epoch": g[-1]["epoch"], "open": g[0]["open"], "high": max(c["high"] for c in g),
             "low": min(c["low"] for c in g), "close": g[-1]["close"]}
            for _, g in sorted(weeks.items())]


def with_forming(cs, gran):
    """detect_trend ignore la dernière bougie (en formation) : on lui en ajoute une neutre pour
    ne perdre aucune bougie clôturée."""
    p = cs[-1]["close"]
    return cs + [{"epoch": cs[-1]["epoch"] + gran, "open": p, "high": p, "low": p, "close": p}]


def trends(sym, cs, now):
    """Biais D1 et W1. Au week-end, la semaine ISO en cours est considérée comme terminée."""
    closed = closed_d1(sym, cs, now)
    if len(closed) < 2:
        return {"d1": None, "w1": None}
    weekly = weekly_candles(closed)
    return {"d1": bot.detect_trend(with_forming(closed, 86400)),
            "w1": bot.detect_trend(with_forming(weekly, 7 * 86400))}


def group_of(sym):
    if sym == "frxXAUUSD":
        return "Or"
    if sym.startswith("cry"):
        return "Crypto"
    if bot.is_synthetic(sym):
        return "Indices synthétiques"
    return "Forex"


def build_lines(api, symbols, now):
    groups = {"Forex": [], "Or": [], "Crypto": [], "Indices synthétiques": []}
    end = int(now.timestamp())  # fin explicite : on lit l'historique même marché fermé
    for sym in symbols:
        cs = api.candles(sym, 86400, count=D1_COUNT, end=end)
        if not cs:
            print(f"[outlook/{sym}] pas de données D1, ignoré.")
            continue
        t = trends(sym, cs, now)
        groups[group_of(sym)].append(f"{bot.display_name(sym)} : {LABEL[t['d1']]} (D1) / {LABEL[t['w1']]} (W1)")
    return groups


def display_time(now):
    local = now.astimezone(ZoneInfo(DISPLAY_TZ))
    return f"{local.strftime('%H:%M')} (UTC{int(local.utcoffset().total_seconds() // 3600):+d})"


def split_message(header, groups):
    """Messages de 3500 caractères au plus : coupés entre deux lignes, jamais au milieu d'une."""
    lines = [header]
    for name, rows in groups.items():
        if rows:
            lines += [f"\n<b>{name}</b>"] + rows
    chunks, cur = [], ""
    for line in lines:
        if cur and len(cur) + len(line) + 1 > MAX_MESSAGE:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    return chunks + ([cur] if cur else [])


def main(now=None):
    now = now or datetime.now(timezone.utc)
    marker = bot.load(MARKER_FILE)
    test = os.getenv("NOTIFY_OK") == "1"
    if test or should_send(now, marker):
        kind, symbols = "weekly", symbols_in_scope()
        header = f"📅 <b>Outlook de la semaine</b> — {display_time(now)}"
    elif should_refresh(now, marker):
        kind = "refresh"
        symbols = [s for s in symbols_in_scope() if bot.is_247(s)]
        header = f"🔄 <b>Mise à jour crypto et indices</b> (dimanche) — {display_time(now)}"
    else:
        print("Outlook : rien à envoyer.")
        return
    api = bot.Deriv()
    try:
        groups = build_lines(api, symbols, now)
    finally:
        api.close()
    if not any(groups.values()):
        print("Outlook : aucune donnée disponible, envoi annulé.")
        return
    ok = all([bot.notify(m, html=True) for m in split_message(header, groups)])
    if not ok:
        print("Outlook : envoi Telegram échoué, nouvel essai au prochain passage.")
        return
    print("Outlook envoyé.")
    if not test:  # un test n'empêche pas l'envoi réel du week-end
        marker["last_sent_week" if kind == "weekly" else "last_sent_refresh_week"] = week_key(now)
        bot.save(MARKER_FILE, marker)


if __name__ == "__main__":
    main()
