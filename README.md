# Crt-Bot : alertes CRT et Impulsion (Deriv + Telegram)

Deux scripts Python qui analysent les bougies de l'API Deriv et envoient des alertes Telegram. Ils n'exécutent aucun ordre : tu places toi-même l'ordre sur ta plateforme.

| Fichier | Rôle |
|---|---|
| `bot.py` | Stratégie CRT (Candle Range Trading) sur 22 actifs |
| `impulse.py` | Stratégie Impulsion + retracement Fibonacci sur 10 Volatility Index |
| `crt.yml` | Workflow GitHub Actions, à placer dans `.github/workflows/crt.yml` |
| `requirements.txt` | Dépendances (`websocket-client`, `requests`) |

## Mise en place

1. Crée un dépôt GitHub. Mets `bot.py`, `impulse.py` et `requirements.txt` à la racine. Pour le workflow, crée le fichier `.github/workflows/crt.yml` (« Add file → Create new file », avec ce chemin comme nom) et colle le contenu de `crt.yml`.
2. Crée un bot Telegram avec @BotFather (`/newbot`) et garde le token.
3. Envoie un message à ton bot, puis ouvre `https://api.telegram.org/bot<TOKEN>/getUpdates` pour lire ton `chat id` (`"chat":{"id":...}`).
4. Dans le dépôt : Settings → Secrets and variables → Actions → New repository secret. Crée `TELEGRAM_TOKEN` et `TELEGRAM_CHAT_ID` (noms exacts, dans « Repository secrets »).
5. Onglet Actions : active les workflows si GitHub le demande.
6. Test : Actions → CRT Bot → Run workflow, avec « test » coché. Chaque bot envoie un message de contrôle (« Bot CRT OK… » et « Impulsion OK… »). Sans « test », aucun message de contrôle n'est envoyé.

### Déclenchement (cron externe)

Le workflow n'a plus de déclencheur `schedule` : il est lancé uniquement par un cron externe (par exemple cron-job.org), ou à la main depuis l'onglet Actions.

- Requête POST toutes les 5 minutes vers `https://api.github.com/repos/<COMPTE>/<DEPOT>/actions/workflows/crt.yml/dispatches`
- Corps : `{"ref":"main"}` (remplace `main` par le nom de ta branche par défaut si besoin)
- En-têtes : `Authorization: Bearer <TOKEN>`, `Accept: application/vnd.github+json` et `X-GitHub-Api-Version: 2022-11-28`
- Le token est un jeton « fine-grained » limité à ce dépôt, avec la permission « Actions : lecture et écriture ». Il a une date d'expiration : note-la pour le renouveler.
- Réponse attendue : `204` (aucun contenu). `401` ou `403` : token ou permission incorrects. `404` : mauvais compte, dépôt ou nom de fichier. `422` : branche inexistante ou workflow sans `workflow_dispatch`.
- Les exécutions sont mises en file d'attente (une seule à la fois). Si un scan dure plus de 5 minutes, le suivant attend.

Attention : 288 exécutions par jour dépassent le quota gratuit des dépôts privés GitHub (à vérifier sur ton compte). Un dépôt public n'a pas cette limite. Sinon, héberge le bot ailleurs avec un cron.

## Source de données Deriv

- Le bot se connecte d'abord à `wss://api.derivws.com/trading/v1/options/ws/public` (sans authentification), avec l'ancien endpoint en secours.
- Le client limite le débit (0,6 s entre deux requêtes), réessaie avec un délai croissant et se reconnecte en cas de coupure ou de limite atteinte.
- Un cache évite de retélécharger les bougies tant que la dernière n'est pas clôturée.
- Au démarrage, le log affiche les symboles absents chez Deriv (« Symboles Deriv… »). Les symboles indisponibles sont ignorés.
- Variables optionnelles : `DERIV_WS_URL` (forcer un endpoint) et `DERIV_APP_ID` (ancien endpoint). Pour les utiliser, ajoute-les dans les blocs `env` du workflow.

## Stratégie CRT (`bot.py`)

**Actifs :** EURUSD, GBPUSD, USDJPY, USDCHF, AUDUSD, USDCAD, NZDUSD, l'or (XAUUSD), BTC, ETH, LTC, XRP et les 10 Volatility Index.

**Cascade (Référence → Manipulation et POI → Confirmation) :** D1→H4→H1, H4→H1→M15, H1→M15→M5. Chaque cascade génère ses propres alertes.

**Règles**
1. Le range est la dernière bougie clôturée du timeframe de référence.
2. Sweep (MTF) : une bougie dépasse le range puis referme dedans. Le setup est annulé si le prix clôture ensuite au-delà de l'extrême du sweep.
3. POI (MTF) : un FVG formé après le sweep, avec une bougie centrale d'au moins 1,5 fois le corps moyen, situé dans la bonne moitié du range (premium pour une vente, discount pour un achat). L'OB éventuel doit être dans la même moitié et ne peut pas être la bougie de sweep.
4. Confirmation (LTF) : le prix touche le POI, puis une clôture casse le dernier swing (fractale de 2 bougies de chaque côté), avec un corps d'au moins 1,2 fois le corps moyen. La cassure doit dater des 2 dernières bougies.
5. Killzones (heure de New York) : Asie 20h-00h, Londres 02h-05h, New York 07h-10h, London Close 10h-12h. Les cryptos et les indices synthétiques ne sont pas filtrés.
6. Pour le forex et l'or, D1 et H4 sont recalculés depuis le H1 avec une ouverture à 17h New York. Les autres actifs gardent les bougies natives de Deriv.
7. SL au-delà de l'extrême du sweep, TP à l'extrême opposé du range.

## Stratégie Impulsion (`impulse.py`)

**Actifs :** Volatility 10, 25, 50, 75, 100 et leurs variantes 1s. **Cascade :** identique au CRT (HTF → MTF → LTF).

**Règles (écrites pour un achat, la vente est le miroir)**
1. Tendance (HTF) : sommets et creux de plus en plus hauts, selon les swings. Sinon, pas de signal.
2. Impulsion (MTF) : cassure de structure avec déplacement, sur une jambe d'au moins 2 ATR. Le Fibonacci va de l'origine (creux) à l'extrême (plus haut, mèche comprise). Le setup est annulé si une bougie MTF clôture au-delà de 79 % ou si l'origine est reprise.
3. Zone acceptée : de 50 % à 79 % de la jambe, mesurés à partir de l'extrême.
4. Entrée (LTF) : le prix touche la zone, balaie un creux LTF formé pendant le retracement (mèche sous le creux, clôture au-dessus), puis casse la structure avec déplacement en laissant un FVG. L'OB est la dernière bougie baissière avant ce déplacement. Il doit chevaucher la zone.
5. Étiquette : « Golden Zone » si le milieu de l'OB est entre 50 % et 61,8 %, « OTE » entre 61,8 % et 79 %.
6. SL au-delà de l'extrême du sweep, plus 0,15 ATR. TP1 à l'extrême de l'impulsion, avec un ratio minimum de 1 pour 3. TP2 et TP3 aux extensions 1,272 et 1,618.

**Type d'ordre selon la position du prix au moment du scan**
- Prix au-dessus de l'OB : ordre limite sur le milieu de l'OB (entrée différente du prix actuel).
- Prix dans l'OB : entrée immédiate (entrée égale au prix actuel), ratio recalculé.
- Prix déjà sorti de l'OB, SL franchi ou TP1 atteint : aucune alerte.

**Ordres limites en attente :** un message « ANNULER » est envoyé si TP1 est atteint sans exécution ou si le stop est franchi avant exécution. L'ordre en attente expire après 24 h. Une seule alerte est envoyée par impulsion.

## Format des notifications

```
🟢 BUY · Volatility 75 · M5

────────────────
Prix actuel  31912.4
────────────────

Entrée  31842.35
SL  31801.2
TP1  32105.8   (1:6.4)
TP2  32243.17
TP3  32408.44

✅ Golden Zone · OB 31828.1 - 31856.6
✅ Sweep  31819.75
```

Les prix sont envoyés en police à espacement fixe (balise `<code>`), copiables d'un toucher dans Telegram. Les alertes CRT suivent le même style, avec la session sous l'en-tête et ✅ sur le sweep, le FVG et l'OB.

## Réglages principaux

| Où | Constante | Effet |
|---|---|---|
| `bot.py` | `SYMBOLS` | Liste des actifs du CRT |
| `bot.py` | `CASCADES` | Triplets de timeframes (partagés avec `impulse.py`) |
| `bot.py` | `KILLZONES_NY` | Noms et horaires des sessions (heure de New York) |
| `bot.py` | `KILLZONE_24_7` | `True` pour filtrer aussi cryptos et indices synthétiques |
| `bot.py` | `NY_ALIGNED` | Alignement 17h New York pour le forex et l'or |
| `bot.py` | `MIN_INTERVAL`, `MAX_RETRIES` | Débit et nombre d'essais du client Deriv |
| `impulse.py` | `SYMBOLS` | Liste des indices de la stratégie Impulsion |
| `impulse.py` | `ZONE` | Zone de retracement acceptée (0,5 à 0,79) |
| `impulse.py` | `IMPULSE_ATR` | Taille minimale de la jambe (2 ATR) |
| `impulse.py` | `MIN_RR` | Ratio gain/risque minimum (3 = 1 pour 3) |
| `impulse.py` | `SL_BUFFER_ATR` | Tampon du stop-loss |
| `impulse.py` | `FRESH` | Ancienneté maximale de la cassure LTF (en bougies) |
| `impulse.py` | `PENDING_TTL` | Durée de vie d'un ordre limite en attente |

## Dépannage

- **Erreurs « Handshake status 520 » :** l'endpoint Deriv refuse la connexion. Le bot alterne entre le nouvel et l'ancien endpoint. Si cela persiste, envoie les premières lignes du log.
- **Aucun message Telegram :** vérifie que `TELEGRAM_TOKEN` n'est pas vide dans les logs (une valeur de secret remplie s'affiche `***`). Le log affiche « ATTENTION… message NON envoyé » si un secret manque.
- **Aucune alerte pendant des heures :** c'est normal. Toutes les conditions doivent être réunies, surtout avec le ratio de 1 pour 3.
- **Symbole absent :** lis la ligne « Symboles Deriv… » au début du log.
- **Plus aucune exécution :** vérifie le tableau de bord du cron externe (réponse `204` attendue) et la date d'expiration du token GitHub.

## Limites et avertissements

- Le code a été testé sur des bougies fictives et avec un faux serveur. Il n'a pas été validé sur plusieurs jours de données réelles, et aucun backtest n'a été fait. Les réglages (2 ATR, déplacements, tailles de zone) peuvent demander des ajustements.
- Les Volatility Index sont des marchés simulés par un algorithme, sans vrais participants. Les concepts ICT comme le sweep de liquidité reposent sur le comportement de vrais marchés : les signaux sur ces indices sont à prendre avec prudence.
- Les alertes arrivent jusqu'à 5 minutes après la bougie (plus le délai de démarrage de GitHub Actions). Sur les indices 1s, le prix peut bouger entre l'alerte et ton ordre.
- Ce projet n'est pas un conseil financier. Les résultats passés ou simulés ne garantissent rien. Teste d'abord sur un compte démo.
