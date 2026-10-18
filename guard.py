"""Garde-fous communs au CRT et à l'Impulsion : fenêtres sans alerte (news, rollover, week-end),
un seul trade actif par actif et par sens, exposition corrélée, plafond de pertes journalier,
calcul du lot, surveillance du bot, entonnoir de filtres et bilan hebdomadaire.
Autonome : aucun import de bot.py (les fonctions d'envoi sont passées en argument)."""
import json, math, os
from datetime import datetime
from zoneinfo import ZoneInfo

import tracker as T

NY = ZoneInfo("America/New_York")
GUARD_FILE = "guard.json"
NEWS_FILE = "news.json"

# --- Limites de risque ---
MAX_OPEN_TRADES = 8        # ordres en attente + positions ouvertes (CRT + Impulsion)
GROUP_LIMIT = 2            # exposition nette maximale par groupe corrélé (USD, cryptos), même sens
MAX_DAILY_LOSS_R = 3.0     # plus de nouvelles alertes si la perte du jour atteint 3 R (jour = 17h NY)
# --- Fenêtres sans alerte (forex et or) ---
ROLLOVER = (16 * 60 + 55, 17 * 60 + 15)     # minutes NY : élargissement des spreads
FRIDAY_CLOSE_FROM = 15 * 60 + 30            # vendredi : trop proche de la fermeture hebdomadaire
SUNDAY_OPEN = (17 * 60, 18 * 60)            # dimanche : ouverture de semaine (gap)
NFP = (8 * 60, 9 * 60 + 30)                 # premier vendredi du mois, publication à 8h30 NY
# --- Surveillance ---
GAP_ALERT_MIN = 30         # alerte si le bot reprend après plus de 30 min sans scan
FAILS_ALERT = 3            # alerte après 3 scans de suite sans aucune donnée Deriv
REPORT_EVERY = 7 * 86400   # bilan envoyé une fois par semaine

FUNNEL_ORDER = ["cascades", "data", "trend", "impulse", "sweep", "range", "poi", "mss", "trade",
                "blackout", "dedupe", "risk", "alert"]
FUNNEL_LABEL = {"cascades": "cascades", "data": "données", "trend": "tendance ok", "impulse": "impulsion",
                "range": "range ok", "sweep": "sweep",
                "poi": "POI", "mss": "MSS", "trade": "trade valide", "blackout": "bloquées (news/horaire)",
                "dedupe": "doublons", "risk": "bloquées (risque)", "alert": "alertes"}


def load_guard(path=GUARD_FILE):
    try:
        with open(path) as f:
            g = json.load(f)
    except Exception:
        g = {}
    g.setdefault("health", {})
    g.setdefault("funnel", {})
    g.setdefault("last_report", 0)
    g.setdefault("paused_since", 0)
    return g


def save_guard(g, path=GUARD_FILE):
    with open(path, "w") as f:
        json.dump(g, f)


# --------------------------------------------------------------------------
# Fenêtres sans alerte : rollover, week-end, NFP, calendrier perso (news.json)
# --------------------------------------------------------------------------
def load_news(path=NEWS_FILE):
    """news.json : [{"start": "2026-10-28T18:00:00Z", "end": "2026-10-28T19:30:00Z",
    "note": "FOMC", "currencies": ["USD"]}] ; 'currencies' est facultatif (sinon tous les actifs réels)."""
    try:
        with open(path) as f:
            out = []
            for n in json.load(f):
                p = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
                out.append({"start": p(n["start"]), "end": p(n["end"]),
                            "note": n.get("note", "news"),
                            "cur": [c.upper() for c in n.get("currencies", [])]})
            return out
    except Exception:
        return []


def blackout(sym, now, news=()):
    """Motif de blocage si l'alerte tombe dans une fenêtre à éviter, sinon None.
    Les indices synthétiques ne sont jamais bloqués (pas de news ni de week-end)."""
    if sym.startswith(("R_", "1HZ", "stp")):
        return None
    for n in news:
        if n["start"] <= now <= n["end"] and (not n["cur"] or any(c in sym.upper() for c in n["cur"])):
            return f"news : {n['note']}"
    if not sym.startswith("frx"):
        return None
    dt = datetime.fromtimestamp(now, NY)
    mins, wd = dt.hour * 60 + dt.minute, dt.weekday()
    if ROLLOVER[0] <= mins < ROLLOVER[1]:
        return "rollover 17h NY"
    if wd == 4 and mins >= FRIDAY_CLOSE_FROM:
        return "fermeture hebdomadaire proche"
    if wd == 6 and SUNDAY_OPEN[0] <= mins < SUNDAY_OPEN[1]:
        return "ouverture de semaine"
    if wd == 4 and dt.day <= 7 and NFP[0] <= mins < NFP[1]:
        return "NFP"
    return None


# --------------------------------------------------------------------------
# Doublons, exposition corrélée, plafond de pertes
# --------------------------------------------------------------------------
def usd_sign(sym, d):
    """+1 si le trade équivaut à être long USD, -1 s'il équivaut à être short USD, 0 sinon."""
    s = sym.upper()
    if not (s.startswith("FRX") and len(s) >= 9):
        return 0
    base, quote = s[3:6], s[6:9]
    buy = 1 if d == "BUY" else -1
    if quote == "USD":
        return -buy
    if base == "USD":
        return buy
    return 0


def exposure_groups(sym, d):
    """[(groupe, signe)] : USD pour forex/or, 'crypto' pour les cryptos, rien pour les synthétiques."""
    if sym.startswith("cry"):
        return [("crypto", 1 if d == "BUY" else -1)]
    u = usd_sign(sym, d)
    return [("USD", u)] if u else []


def risk_check(tr, sym, d, now):
    """Retourne (ok, motif). Contrôle : trade actif sur le même actif et sens, nombre de trades
    ouverts, exposition par groupe corrélé, plafond de perte journalier."""
    act = T.active_list(tr)
    if any(t["sym"] == sym and t["dir"] == d for t in act):
        return False, "dedupe"
    if len(act) >= MAX_OPEN_TRADES:
        return False, f"{MAX_OPEN_TRADES} trades déjà actifs"
    for grp, sign in exposure_groups(sym, d):
        net = sum(s for t in act for g2, s in exposure_groups(t["sym"], t["dir"]) if g2 == grp)
        if sign and net * sign >= GROUP_LIMIT:
            return False, f"exposition {grp} (même sens) au maximum"
    if T.realized_r(tr, T.day_start(now)) <= -MAX_DAILY_LOSS_R:
        return False, f"perte journalière de {MAX_DAILY_LOSS_R:g} R atteinte"
    return True, ""


# --------------------------------------------------------------------------
# Taille de position (facultatif : variable d'environnement ACCOUNT_BALANCE)
# --------------------------------------------------------------------------
def contract_units(sym):
    """Unités par lot. Pour un indice synthétique, renseigne LOT_UNITS_<SYMBOLE> (ex. LOT_UNITS_R_75=1)
    avec la valeur de ta plateforme ; sinon aucun lot n'est calculé."""
    env = os.getenv(f"LOT_UNITS_{sym.upper()}")
    if env:
        try:
            return float(env)
        except ValueError:
            return None
    if sym == "frxXAUUSD":
        return 100.0
    if sym.startswith("frx"):
        return 100000.0
    if sym.startswith("cry"):
        return 1.0
    return None


def lot_size(sym, entry, sl, balance=None, risk_pct=None):
    """Lot pour risquer risk_pct % du solde, ou None si inconnu. Le résultat est arrondi vers le bas
    au 0,01 ; il reste une estimation à vérifier sur ta plateforme."""
    try:
        balance = float(balance or os.getenv("ACCOUNT_BALANCE") or 0)
        risk_pct = float(risk_pct or os.getenv("RISK_PCT") or 1)
    except ValueError:
        return None
    units = contract_units(sym)
    dist = abs(entry - sl)
    if balance <= 0 or not units or dist <= 0:
        return None
    loss_per_lot = dist * units
    if sym.startswith("frx") and sym[6:9] != "USD":
        loss_per_lot /= entry          # perte exprimée en devise de cotation -> USD
    lot = math.floor(balance * risk_pct / 100 / loss_per_lot * 100 + 1e-9) / 100
    return lot


def lot_line(sym, entry, sl):
    bal = os.getenv("ACCOUNT_BALANCE")
    if not bal:
        return None
    lot = lot_size(sym, entry, sl)
    pct = os.getenv("RISK_PCT") or "1"
    if lot is None:
        return f"Lot  à calculer (contrat inconnu) · risque {pct} %"
    return f"Lot  {lot:.2f}  (risque {pct} % de {bal})" if lot >= 0.01 else "Lot  < 0,01 : stop trop large pour ce risque"


# --------------------------------------------------------------------------
# Surveillance du bot
# --------------------------------------------------------------------------
def health_start(g, bot, now, notify):
    h = g["health"].setdefault(bot, {"last_ok": 0, "fails": 0, "alerted": False})
    if h["last_ok"] and now - h["last_ok"] > GAP_ALERT_MIN * 60:
        notify(f"⚠️ {bot} : scan interrompu pendant {(now - h['last_ok']) / 3600:.1f} h, il reprend. "
               f"Les setups apparus pendant la coupure ont pu être manqués.")
    return h


def health_end(g, bot, got_data, now, notify):
    """got_data : au moins un symbole a renvoyé des bougies pendant ce scan."""
    h = g["health"].setdefault(bot, {"last_ok": 0, "fails": 0, "alerted": False})
    if got_data:
        if h["alerted"]:
            notify(f"✅ {bot} : les données Deriv sont de nouveau accessibles.")
        h.update(last_ok=int(now), fails=0, alerted=False)
    else:
        h["fails"] += 1
        if h["fails"] >= FAILS_ALERT and not h["alerted"]:
            notify(f"⚠️ {bot} : aucune donnée Deriv sur {h['fails']} scans de suite. "
                   f"Vérifie l'API Deriv ou le workflow.")
            h["alerted"] = True


# --------------------------------------------------------------------------
# Entonnoir de filtres et bilan
# --------------------------------------------------------------------------
def funnel_add(g, bot, counts):
    acc = g["funnel"].setdefault(bot, {})
    for k, v in counts.items():
        acc[k] = acc.get(k, 0) + v


def funnel_text(counts):
    parts = [f"{FUNNEL_LABEL[k]} {counts[k]}" for k in FUNNEL_ORDER if counts.get(k)]
    return " → ".join(parts) if parts else "aucune donnée"


def report_due(g, now):
    return now - g["last_report"] >= REPORT_EVERY


def build_report(g, tr, now):
    lines = ["📊 Bilan hebdomadaire", T.stats_text(tr, now, days=7), ""]
    lines.append(T.stats_text(tr, now, days=30))
    act = T.active_list(tr)
    lines += ["", f"Trades actifs : {len(act)}"]
    for bot, counts in sorted(g["funnel"].items()):
        lines.append(f"Entonnoir {bot} : {funnel_text(counts)}")
    return "\n".join(lines)


def report_sent(g, now):
    g["last_report"] = int(now)
    g["funnel"] = {}
