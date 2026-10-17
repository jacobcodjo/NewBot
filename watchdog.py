"""Surveille le workflow du bot (crt.yml) avec l'API GitHub et prévient sur Telegram si le bot ne tourne plus ou échoue.

Il s'exécute dans son propre workflow planifié (watchdog.yml), donc il continue de fonctionner même quand le déclencheur
externe du bot s'arrête. Variables : GITHUB_TOKEN et GITHUB_REPOSITORY (fournis par GitHub Actions), TELEGRAM_TOKEN et
TELEGRAM_CHAT_ID (secrets du dépôt), et en option WATCHDOG_WORKFLOW (défaut crt.yml), WATCHDOG_MAX_AGE_MIN (défaut 45 :
silence toléré) et WATCHDOG_WINDOW_MIN (défaut 65 : fenêtre pour signaler les échecs, un peu plus que la période du watchdog
pour qu'un échec soit signalé une fois)."""
import os
import sys
import time
from datetime import datetime

import requests


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def evaluate(runs, now, max_age_min=45, window_min=65):
    """Messages d'alerte d'après la liste des derniers runs (dicts de l'API GitHub) ; liste vide si tout va bien."""
    if not runs:
        return ["Aucun run du workflow du bot trouvé : le déclencheur externe est peut-être arrêté ou le workflow désactivé."]
    runs = sorted(runs, key=lambda r: parse_ts(r["created_at"]), reverse=True)
    msgs = []
    age = (now - parse_ts(runs[0]["created_at"])) / 60
    if age > max_age_min:
        msgs.append(f"Le bot n'a pas tourné depuis {age:.0f} min (seuil {max_age_min} min) : vérifie le déclencheur externe "
                    "(cron-job.org ou équivalent) et que le workflow n'est pas désactivé.")
    failed = [r for r in runs if r.get("conclusion") in ("failure", "timed_out", "startup_failure")
              and now - parse_ts(r["created_at"]) <= window_min * 60]
    if failed:
        msgs.append(f"{len(failed)} exécution(s) du bot en échec sur la dernière heure. Dernière : {failed[0].get('html_url', '')}")
    return msgs


def notify(text):
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        print("ATTENTION : secret Telegram manquant, alerte non envoyée :", text)
        return
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat, "text": text}, timeout=15)
    r.raise_for_status()


def main():
    repo, token = os.getenv("GITHUB_REPOSITORY"), os.getenv("GITHUB_TOKEN")
    if not (repo and token):
        sys.exit("GITHUB_REPOSITORY et GITHUB_TOKEN sont requis (fournis automatiquement dans GitHub Actions).")
    workflow = os.getenv("WATCHDOG_WORKFLOW", "crt.yml")
    r = requests.get(f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/runs", params={"per_page": 20},
                     headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, timeout=20)
    r.raise_for_status()
    msgs = evaluate(r.json().get("workflow_runs", []), time.time(),
                    int(os.getenv("WATCHDOG_MAX_AGE_MIN", "45")), int(os.getenv("WATCHDOG_WINDOW_MIN", "65")))
    for m in msgs:
        print("ALERTE :", m)
        notify(f"🐕 Watchdog CRT : {m}")
    if not msgs:
        print("Watchdog : le bot tourne normalement.")


if __name__ == "__main__":
    main()
