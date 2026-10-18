# Crt-Bot : alertes CRT et Impulsion (Deriv + Telegram)

Des scripts Python qui analysent les bougies de l'API Deriv et envoient des alertes Telegram. Ils n'exécutent aucun ordre : tu places toi-même l'ordre sur ta plateforme. Depuis cette version, ils suivent aussi chaque alerte (exécution, TP1, TP2, stop) et savent rejouer les stratégies sur de l'historique.

| Fichier | Rôle |
|---|---|
| `bot.py` | Stratégie CRT (Candle Range Trading) sur 12 actifs : 7 paires forex, or, 4 cryptos |
| `impulse.py` | Stratégie Impulsion + retracement Fibonacci sur 10 Volatility Index |
| `tracker.py` | Suivi des trades : ordre en attente, position ouverte, résultat en R, statistiques |
| `guard.py` | Garde-fous : news et horaires, doublons, exposition, perte journalière, lot, santé du bot, bilan |
| `backtest.py` | Rejoue CRT et Impulsion sur de l'historique M5 |
| `crt.yml` | Workflow du scan, à placer dans `.github/workflows/crt.yml` |
| `backtest.yml` | Workflow du backtest, à placer dans `.github/workflows/backtest.yml` |
| `news.example.json` | Modèle de calendrier de news (à copier en `news.json`) |
| `requirements.txt` | Dépendances (`websocket-client`, `requests`) |

## Mise en place

1. Crée un dépôt GitHub. Mets à la racine : `bot.py`, `impulse.py`, `tracker.py`, `guard.py`, `backtest.py` et `requirements.txt`. Pour les workflows, crée `.github/workflows/crt.yml` et `.github/workflows/backtest.yml` (« Add file → Create new file », avec ce chemin comme nom) et colle le contenu de `crt.yml` et de `backtest.yml`.
2. Crée un bot Telegram avec @BotFather (`/newbot`) et garde le token.
3. Envoie un message à ton bot, puis ouvre `https://api.telegram.org/bot<TOKEN>/getUpdates` pour lire ton `chat id` (`"chat":{"id":...}`).
4. Dans le dépôt : Settings → Secrets and variables → Actions → New repository secret. Crée `TELEGRAM_TOKEN` et `TELEGRAM_CHAT_ID` (noms exacts, dans « Repository secrets »).
5. Onglet Actions : active les workflows si GitHub le demande.
6. Test : Actions → CRT Bot → Run workflow, avec « test » coché. Chaque bot envoie un message de contrôle avec l'entonnoir de filtres (le CRT ajoute les statistiques). Sans « test », aucun message de contrôle n'est envoyé.

Variables facultatives (Settings → Secrets and variables → Actions → onglet **Variables**) :
- `ACCOUNT_BALANCE` : solde de ton compte. Avec elle, chaque alerte affiche un lot estimé.
- `RISK_PCT` : risque par trade en % du solde (1 par défaut).

### Déclenchement (cron externe)

Le workflow n'a pas de déclencheur `schedule` : il est lancé par un cron externe (par exemple cron-job.org), ou à la main depuis l'onglet Actions.

- Requête POST toutes les 5 minutes vers `https://api.github.com/repos/<COMPTE>/<DEPOT>/actions/workflows/crt.yml/dispatches`
- Corps : `{"ref":"main"}` (remplace `main` par le nom de ta branche par défaut si besoin)
- En-têtes : `Authorization: Bearer <TOKEN>`, `Accept: application/vnd.github+json` et `X-GitHub-Api-Version: 2022-11-28`
- Le token est un jeton « fine-grained » limité à ce dépôt, avec la permission « Actions : lecture et écriture ». Il a une date d'expiration : note-la pour le renouveler.
- Réponse attendue : `204`. `401` ou `403` : token ou permission incorrects. `404` : mauvais compte, dépôt ou nom de fichier. `422` : branche inexistante ou workflow sans `workflow_dispatch`.
- Les exécutions sont mises en file d'attente (une seule à la fois).

Attention : 288 exécutions par jour dépassent le quota gratuit des dépôts privés GitHub (à vérifier sur ton compte). Un dépôt public n'a pas cette limite.

## Source de données Deriv

- Le bot se connecte d'abord à `wss://api.derivws.com/trading/v1/options/ws/public` (sans authentification), avec l'ancien endpoint en secours.
- Le client limite le débit (0,6 s entre deux requêtes), réessaie avec un délai croissant et se reconnecte en cas de coupure.
- Un cache évite de retélécharger les bougies tant que la dernière n'est pas clôturée.
- Au démarrage, le log affiche les symboles absents chez Deriv. Les symboles indisponibles sont ignorés.
- Variables d'environnement optionnelles : `DERIV_WS_URL`, `DERIV_APP_ID`, et `CRT_SYNTHETIC=1` pour que le CRT scanne aussi les Volatility Index (réservés à l'Impulsion par défaut). Pour les utiliser, ajoute-les dans les blocs `env` du workflow.

## Stratégie CRT (`bot.py`)

**Actifs :** EURUSD, GBPUSD, USDJPY, USDCHF, AUDUSD, USDCAD, NZDUSD, l'or (XAUUSD), BTC, ETH, LTC, XRP.

**Cascade (Référence → Manipulation et POI → Confirmation) :** D1→H4→H1, H4→H1→M15, H1→M15→M5. Chaque cascade génère ses propres alertes.

**Règles**
1. Le range est la dernière bougie clôturée du timeframe de référence. Il doit mesurer entre 0,5 et 3 ATR du timeframe de référence (`RANGE_MIN_ATR`, `RANGE_MAX_ATR`) : cela écarte les inside bars et les bougies d'actualité géantes.
2. Sweep (MTF) : une bougie dépasse le range puis referme dedans. Une bougie qui dépasse les deux côtés est ignorée (sens ambigu). Si le prix clôture ensuite au-delà de l'extrême du sweep, ce sweep est invalidé et le bot cherche un sweep ultérieur.
3. POI (MTF) : un FVG formé après le sweep, avec une bougie centrale d'au moins 1,5 fois le corps moyen, situé dans la bonne moitié du range (premium pour une vente, discount pour un achat). L'OB éventuel doit être dans la même moitié et ne peut pas être la bougie de sweep.
4. Confirmation (LTF) : le prix touche le POI, puis une clôture casse le dernier swing (fractale de 2 bougies de chaque côté), avec un corps d'au moins 1,2 fois le corps moyen. La cassure doit dater des 2 dernières bougies.
5. Killzones (heure de New York) : Asie 20h-00h, Londres 02h-05h, New York 07h-10h, London Close 10h-12h. Les cryptos ne sont pas filtrées.
6. Pour le forex et l'or, D1 et H4 sont recalculés depuis le H1 avec une ouverture à 17h New York.

**Entrées et sorties**
- **Entrée :** au niveau de la cassure de structure LTF, par ordre de priorité : Order Block (dernière bougie opposée avant le mouvement, à 8 bougies LTF au plus de la cassure), sinon Breaker Block, sinon milieu du FVG. Si aucun des trois n'existe, le setup est ignoré. Sur les indices synthétiques (si `CRT_SYNTHETIC=1`), l'entrée est immédiate à la clôture de la cassure.
- **Stop-loss :** au-delà de l'extrême du sweep, avec une marge d'au moins 0,05 % du prix ou 0,1 ATR MTF si c'est plus grand (`STOP_LOSS_BUFFER_PCT`, `SL_BUFFER_ATR`). La marge s'adapte ainsi à la volatilité de chaque actif.
- **Cibles :** TP1 à mi-range, TP2 au bord opposé du range, TP3 au bord opposé plus 50 % de la taille du range (`TP_EXTENSION_PCT`).
- **Ratio :** calculé sur TP2, minimum 1 pour 3 (`MIN_RISK_REWARD`).
- **Type d'ordre :** `BUY`/`SELL` si le prix est déjà sur la zone (écart sous `ORDER_TYPE_TOLERANCE_PCT`), sinon `BUY LIMIT`/`SELL LIMIT` ou `BUY STOP`/`SELL STOP`.
- **OTE :** si l'entrée tombe dans le retracement 61,8-79 % du mouvement après le sweep, la ligne « ✅ OTE » est ajoutée. Avec `REQUIRE_FIB_OTE = True`, c'est un filtre obligatoire.

## Stratégie Impulsion (`impulse.py`)

**Actifs :** Volatility 10, 25, 50, 75, 100 et leurs variantes 1s. **Cascade :** identique au CRT.

**Règles (écrites pour un achat, la vente est le miroir)**
1. Tendance (HTF) : dernier sommet et dernier creux plus hauts que les précédents (2 swings suffisent), structure intacte (aucune clôture sous le dernier creux) et dernier sommet dépassé par une clôture (`BOS_BY_CLOSE`). L'âge est classé d'après le nombre de jambes consécutives : 1 = naissante, 2-3 = établie, 4 et plus = mûre (`AGE_ESTABLISHED`, `AGE_MATURE`). Seules les tendances naissantes et établies donnent un signal (`TREND_AGES_OK`).
2. Impulsion (MTF) : cassure de structure avec déplacement, sur une jambe d'au moins 2 ATR. Le Fibonacci va de l'origine (creux) à l'extrême (plus haut, mèche comprise). Le setup est annulé si une bougie MTF ou LTF clôture au-delà de 79 % ou si l'origine est reprise. L'extrême doit dater de 48 bougies MTF au plus (`IMPULSE_MAX_AGE`).
3. Zone acceptée : de 50 % à 79 % de la jambe, mesurés à partir de l'extrême.
4. Entrée (LTF) : le prix touche la zone, balaie un creux LTF formé pendant le retracement, puis casse la structure avec déplacement en laissant un FVG (bougie centrale d'au moins 1,5 fois le corps moyen, taille d'au moins 0,1 ATR). L'OB est la dernière bougie baissière avant ce déplacement et doit chevaucher la zone. Si plusieurs combinaisons sweep/cassure existent, le bot garde celle qui offre le meilleur ratio.
5. Étiquette : « Golden Zone » si l'entrée est entre 50 % et 61,8 %, « OTE » entre 61,8 % et 79 %.
6. SL au-delà de l'extrême du sweep, plus 0,15 ATR. TP1 à l'extrême de l'impulsion, TP2 et TP3 aux extensions 1,272 et 1,618. Le ratio minimum de 1 pour 3 est calculé sur TP2 (`RR_ON = "tp1"` pour le calculer sur TP1).

**Niveaux d'entrée :** le milieu du FVG (prioritaire) ou l'extrémité de l'OB, dans la zone 50-79 %.

**Types d'ordres (selon la position du prix au moment du scan)**
1. `BUY LIMIT` / `SELL LIMIT` : le niveau n'a pas encore été atteint depuis la fin du FVG, le prix a fait au plus 50 % du chemin vers TP1 (`MAX_PROGRESS`) et le ratio est d'au moins 1 pour 3.
2. `BUY` / `SELL` instantané : le prix est dans l'OB et les niveaux limites ne sont plus disponibles.
3. `BUY` / `SELL` avec « ⏱ Entrée tardive » : le prix vient de quitter l'OB (`LATE_CANDLES`, `LATE_DIST`, `LATE_PROGRESS`).
4. Aucune alerte : le stop est franchi, TP1 est atteint, le ratio est trop faible ou le prix est déjà trop loin.

Une seule alerte est envoyée par impulsion (mémorisée 14 jours, `SENT_TTL`).

## Suivi des trades (`tracker.py`)

Chaque alerte est enregistrée dans `trades.json`. À chaque scan, le bot compare les bougies récentes à tes niveaux et envoie un message à chaque étape :

- **▶️ Ordre touché** : le prix a atteint l'entrée d'un ordre limite ou stop.
- **🎯 TP1 atteint** : le suivi clôture 50 % de la position et remonte le stop à l'entrée (`PARTIAL_AT_TP1`, `BE_AT_TP1`).
- **✅ TP2 atteint**, **❌ Stop touché** ou **➖ Stop à l'entrée touché** : le résultat est donné en R (multiple du risque).
- **⚠️ ANNULER** : TP1 atteint sans exécution, stop franchi avant exécution, ou ordre expiré (24 h ; 48 h pour le CRT quand la LTF est le H1).

Le suivi suppose que tu as placé l'ordre exactement aux niveaux de l'alerte. Si le stop et une cible sont touchés dans la même bougie, le stop est compté en premier (hypothèse prudente). TP3 reste indicatif.

Un bilan hebdomadaire est envoyé (résultats 7 et 30 jours, par stratégie et par cascade, trades actifs, entonnoir de filtres). En local ou dans les logs : `python bot.py --stats`.

## Garde-fous (`guard.py`)

- **Un seul trade actif par actif et par sens.** Les alertes en double entre cascades (D1>H4>H1 et H4>H1>M15 sur le même mouvement) sont bloquées : la cascade la plus haute passe en premier.
- **Exposition corrélée :** au plus 2 trades de même sens sur le dollar (forex et or comptent ensemble, un achat EURUSD équivaut à une vente USD) et 2 sur les cryptos (`GROUP_LIMIT`). Au plus 8 trades actifs en tout (`MAX_OPEN_TRADES`).
- **Perte journalière :** après −3 R sur la journée de trading (qui démarre à 17h New York), plus aucune alerte jusqu'au lendemain (`MAX_DAILY_LOSS_R`). Un message prévient une fois.
- **Fenêtres sans alerte (forex et or) :** rollover 16h55-17h15 New York, vendredi après 15h30, ouverture du dimanche 17h-18h, NFP (premier vendredi du mois, 8h-9h30). Les cryptos ne sont concernées que par `news.json`. Les indices synthétiques ne sont jamais bloqués.
- **Calendrier de news :** copie `news.example.json` en `news.json` à la racine du dépôt, puis liste tes événements (FOMC, CPI, BCE…) avec leurs heures en UTC. `currencies` est facultatif (sans lui, tous les actifs réels sont concernés). Il n'y a pas de calendrier automatique : c'est à toi de le tenir à jour.
- **Lot estimé :** si `ACCOUNT_BALANCE` est défini, chaque alerte affiche un lot. Les contrats sont supposés à 100 000 unités (forex), 100 oz (or) et 1 (cryptos). Pour un indice synthétique, renseigne la valeur de ta plateforme dans `LOT_UNITS_<SYMBOLE>` (par exemple `LOT_UNITS_R_75`), sinon aucun lot n'est calculé. C'est une estimation à vérifier sur ta plateforme avant d'ordonner.
- **Surveillance :** un message prévient si le bot reprend après plus de 30 minutes d'arrêt, ou si Deriv ne renvoie aucune donnée pendant 3 scans de suite (et quand c'est rétabli). Si GitHub ne lance plus rien du tout, le bot ne peut pas le dire : active les notifications d'échec de ton cron externe.
- **Entonnoir :** chaque scan compte combien de cascades passent chaque filtre (tendance, sweep, range, POI, cassure, trade valide, bloquées, alertes). Ces compteurs s'affichent dans les logs et le bilan. Un même setup peut être compté à plusieurs scans successifs.

## Backtest (`backtest.py`)

Le backtest rejoue la stratégie sur de l'historique M5, avec les mêmes fonctions que le bot en direct, les mêmes garde-fous et la même gestion des trades. Les autres timeframes sont reconstruits depuis le M5 sans regarder le futur.

**Depuis GitHub :** Actions → Backtest → Run workflow, avec l'actif (`R_75`, `frxEURUSD`…), le nombre de jours, la stratégie et le spread. Le rapport arrive sur Telegram et dans les logs.

**En local :**
```
python backtest.py --sym R_75 --source deriv --days 60 --spread-pct 0.01
python backtest.py --sym frxEURUSD --source csv --file eurusd_m5.csv
python backtest.py --sym R_75 --source sim --days 90      # marche aléatoire, contrôle de bon sens
```
Le rapport donne le nombre de trades, le taux de gain, le total en R, le facteur de profit (PF), le drawdown maximal, et le détail par cascade, par sens, par âge de tendance (Impulsion), par session (CRT) et par type de sortie.

Pour juger une stratégie : plusieurs centaines de trades, plusieurs actifs, plusieurs périodes, et un spread réaliste. En dessous d'une cinquantaine de trades, les résultats sont du bruit. Le backtest suppose un spread constant, aucun glissement, et ne tient pas compte de `news.json`.

## Format des notifications

```
🟢 BUY LIMIT · Volatility 75 · M5 · Impulsion

────────────────
Prix actuel  31912.4
────────────────

Entrée  31842.35  (FVG 50 %)
SL  31801.2
TP1  32105.8   (1:6.4)
TP2  32243.17   (1:9.7)
TP3  32408.44   (1:13.8)

✅ Golden Zone · OB 31828.1 - 31856.6
✅ Sweep  31819.75
✅ Tendance établie · H1

Lot  0.12  (risque 1 % de 1000)
Gestion : 50 % à TP1, puis SL à l'entrée, le reste vise TP2
```

Chaque TP affiche son ratio gain/risque depuis l'entrée prévue. Les prix sont arrondis au nombre de décimales de l'actif et envoyés en police à espacement fixe, copiables d'un toucher dans Telegram. La ligne « Lot » n'apparaît que si `ACCOUNT_BALANCE` est défini. Les alertes CRT suivent le même style : type d'ordre dans l'en-tête, session sous l'en-tête, ligne Entrée avec son origine (Order Block, Breaker Block, FVG 50 % ou clôture de la cassure), SL, TP1 à TP3, puis ✅ sur le sweep, le FVG, l'OB et l'OTE.

## Réglages principaux

| Où | Constante | Effet |
|---|---|---|
| `bot.py` | `SYMBOLS` | Actifs du CRT (`CRT_SYNTHETIC=1` ajoute les indices) |
| `bot.py` | `CASCADES` | Triplets de timeframes (partagés avec `impulse.py`) |
| `bot.py` | `KILLZONES_NY`, `KILLZONE_24_7` | Sessions du CRT, et filtre éventuel sur les cryptos |
| `bot.py` | `NY_ALIGNED` | Alignement 17h New York pour le forex et l'or |
| `bot.py` | `STOP_LOSS_BUFFER_PCT`, `SL_BUFFER_ATR` | Marge du SL (le plus grand des deux) |
| `bot.py` | `RANGE_MIN_ATR`, `RANGE_MAX_ATR` | Taille acceptée du range de référence |
| `bot.py` | `TP_EXTENSION_PCT` | Prolongement de TP3 |
| `bot.py` | `MIN_RISK_REWARD` | Ratio minimum du CRT, sur TP2 |
| `bot.py` | `PENDING_TTL_FAST`, `PENDING_TTL_SLOW` | Validité d'un ordre en attente |
| `bot.py` | `ORDER_TYPE_TOLERANCE_PCT`, `OB_LOOKBACK`, `REQUIRE_FIB_OTE` | Type d'ordre, distance de l'OB, OTE obligatoire |
| `impulse.py` | `SYMBOLS` | Indices de l'Impulsion |
| `impulse.py` | `ZONE`, `IMPULSE_ATR`, `IMPULSE_MAX_AGE` | Zone de retracement, taille et ancienneté de l'impulsion |
| `impulse.py` | `TREND_AGES_OK`, `AGE_ESTABLISHED`, `AGE_MATURE`, `BOS_BY_CLOSE` | Filtre d'âge de tendance |
| `impulse.py` | `FVG_DISP`, `FVG_MIN_ATR` | Déplacement et taille minimaux du FVG |
| `impulse.py` | `RR_ON`, `MIN_RR`, `SL_BUFFER_ATR` | Ratio minimum et marge du stop |
| `impulse.py` | `MAX_PROGRESS`, `LATE_CANDLES`, `LATE_DIST`, `LATE_PROGRESS` | Ordres limites et entrées tardives |
| `impulse.py` | `PENDING_TTL`, `SENT_TTL`, `FRESH` | Validité d'un ordre, mémoire des alertes, fraîcheur de la cassure |
| `tracker.py` | `PARTIAL_AT_TP1`, `BE_AT_TP1` | Gestion du trade après TP1 |
| `guard.py` | `MAX_OPEN_TRADES`, `GROUP_LIMIT`, `MAX_DAILY_LOSS_R` | Limites de risque |
| `guard.py` | `ROLLOVER`, `FRIDAY_CLOSE_FROM`, `SUNDAY_OPEN`, `NFP` | Fenêtres sans alerte |
| `guard.py` | `GAP_ALERT_MIN`, `FAILS_ALERT`, `REPORT_EVERY` | Surveillance et bilan |

## Dépannage

- **Erreurs « Handshake status 520 » :** l'endpoint Deriv refuse la connexion. Le bot alterne entre le nouvel et l'ancien endpoint.
- **Aucun message Telegram :** vérifie que `TELEGRAM_TOKEN` n'est pas vide dans les logs. Le log affiche « ATTENTION… message NON envoyé » si un secret manque.
- **Aucune alerte pendant des heures :** c'est normal. Lis la ligne « Entonnoir » dans les logs pour voir où les setups sont filtrés.
- **Alerte absente alors que le setup semble valide :** vérifie le log (« Alerte bloquée… ») : un trade actif sur le même actif, une limite de risque ou la pause journalière peut l'avoir bloquée.
- **Plus aucune exécution :** vérifie le tableau de bord du cron externe (réponse `204`) et la date d'expiration du token GitHub.
- **Repartir de zéro :** supprime le cache GitHub Actions (onglet Actions → Caches) pour effacer `trades.json`, `guard.json` et les états.

## Limites et avertissements

- Le code a été testé sur des bougies simulées (marche aléatoire), avec un faux serveur Deriv pour le scan en direct. Il n'a pas été validé sur de l'historique réel : lance le backtest sur plusieurs actifs avant de t'y fier.
- Sur une marche aléatoire, le backtest ne montre aucun avantage statistique, ce qui est normal : les concepts de sweep et de structure supposent de vrais participants. Les Volatility Index sont générés par un algorithme, donc leurs signaux sont à prendre avec prudence jusqu'à ce qu'un backtest sur l'historique réel dise le contraire.
- Pas repris : tag de contre-tendance, pools de liquidité (equal highs/lows), calendrier de news automatique, spread en direct, suivi de TP3.
- Les alertes arrivent jusqu'à 5 minutes après la bougie (plus le délai de démarrage de GitHub Actions). Sur les indices 1s, le prix peut bouger entre l'alerte et ton ordre.
- Ce projet n'est pas un conseil financier. Les résultats passés ou simulés ne garantissent rien. Teste d'abord sur un compte démo.
