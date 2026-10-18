# Crt-Bot : alertes CRT et Impulsion (Deriv + Telegram)

Des scripts Python qui analysent les bougies de l'API Deriv et envoient des alertes Telegram avec un graphique du setup. Ils n'exécutent aucun ordre : tu places toi-même l'ordre sur ta plateforme. Ils suivent chaque alerte (exécution, TP1 à TP3, stop, sorties), te laissent indiquer les trades que tu as pris, et savent rejouer les stratégies sur de l'historique avec des outils de validation.

| Fichier | Rôle |
|---|---|
| `bot.py` | Stratégie CRT (Candle Range Trading) sur 12 actifs : 7 paires forex, or, 4 cryptos |
| `impulse.py` | Stratégie Impulsion + retracement Fibonacci sur 10 Volatility Index |
| `tracker.py` | Suivi des trades : ordre en attente, position ouverte, gestion après l'entrée, résultat en R, statistiques |
| `charts.py` | Graphique du setup joint à chaque alerte |
| `guard.py` | Garde-fous : news et horaires, doublons, exposition, perte journalière, lot, santé du bot, bilan |
| `backtest.py` | Rejoue CRT et Impulsion sur de l'historique M5 |
| `crt.yml` | Workflow du scan, à placer dans `.github/workflows/crt.yml` |
| `backtest.yml` | Workflow du backtest, à placer dans `.github/workflows/backtest.yml` |
| `news.example.json` | Modèle de calendrier de news (à copier en `news.json`) |
| `requirements.txt` | Dépendances (`websocket-client`, `requests`, `matplotlib`) |

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
- Variables d'environnement optionnelles : `DERIV_WS_URL`, `DERIV_APP_ID`, `CALENDAR_URL` (autre source de calendrier au même format) et `CRT_SYNTHETIC=1` pour que le CRT scanne aussi les Volatility Index (réservés à l'Impulsion par défaut). Pour les utiliser, ajoute-les dans les blocs `env` du workflow.

## Stratégie CRT (`bot.py`)

**Actifs :** EURUSD, GBPUSD, USDJPY, USDCHF, AUDUSD, USDCAD, NZDUSD, l'or (XAUUSD), BTC, ETH, LTC, XRP.

**Cascade (Référence → Manipulation et POI → Confirmation) :** D1→H4→H1, H4→H1→M15, H1→M15→M5. Chaque cascade génère ses propres alertes.

**Règles**
1. Le range est la dernière bougie clôturée du timeframe de référence. Il doit mesurer entre 0,5 et 3 ATR du timeframe de référence (`RANGE_MIN_ATR`, `RANGE_MAX_ATR`) : cela écarte les inside bars et les bougies d'actualité géantes.
2. Régime de volatilité : l'ATR 14 du MTF doit valoir entre 0,6 et 2 fois son ATR 100 (`VOL_MIN`, `VOL_MAX`). Cela écarte la compression extrême et les pics d'actualité. Sans assez d'historique (H4 reconstruit trop court), le filtre ne s'applique pas. Il est sans effet sur les Volatility Index, dont la volatilité est constante.
3. Sweep (MTF) : une bougie dépasse le range puis referme dedans. Une bougie qui dépasse les deux côtés est ignorée (sens ambigu). Si le prix clôture ensuite au-delà de l'extrême du sweep, ce sweep est invalidé et le bot cherche un sweep ultérieur.
4. POI (MTF) : un FVG formé après le sweep, avec une bougie centrale d'au moins 1,5 fois le corps moyen, situé dans la bonne moitié du range (premium pour une vente, discount pour un achat). L'OB éventuel doit être dans la même moitié et ne peut pas être la bougie de sweep.
5. Confirmation (LTF) : le prix touche le POI, puis une clôture casse le dernier swing (fractale de 2 bougies de chaque côté), avec un corps d'au moins 1,2 fois le corps moyen. La cassure doit dater des 2 dernières bougies.
6. Killzones (heure de New York) : Asie 20h-00h, Londres 02h-05h, New York 07h-10h, London Close 10h-12h. Les cryptos ne sont pas filtrées.
7. Pour le forex et l'or, D1 et H4 sont recalculés depuis le H1 avec une ouverture à 17h New York.

**Entrées et sorties**
- **Entrée :** au niveau de la cassure de structure LTF, par ordre de priorité : Order Block (dernière bougie opposée avant le mouvement, à 8 bougies LTF au plus de la cassure), sinon Breaker Block, sinon milieu du FVG. Si aucun des trois n'existe, le setup est ignoré. Sur les indices synthétiques (si `CRT_SYNTHETIC=1`), l'entrée est immédiate à la clôture de la cassure.
- **Stop-loss :** au-delà de l'extrême du sweep, avec une marge d'au moins 0,05 % du prix ou 0,1 ATR MTF si c'est plus grand (`STOP_LOSS_BUFFER_PCT`, `SL_BUFFER_ATR`). La marge s'adapte ainsi à la volatilité de chaque actif.
- **Cibles :** TP1 à mi-range, TP2 au bord opposé du range, TP3 au bord opposé plus 50 % de la taille du range (`TP_EXTENSION_PCT`).
- **Ratio :** calculé sur TP2, minimum 1 pour 3 (`MIN_RISK_REWARD`).
- **Liquidité prise :** l'alerte indique si le sweep a pris le plus haut/bas de la veille (PDH/PDL) ou des equal highs/lows (deux swings à moins de 0,15 ATR), et affiche la liquidité opposée. Par défaut, c'est une information : le backtest la détaille dans sa colonne « liq ». Avec `REQUIRE_LIQUIDITY = True`, elle devient obligatoire. Dans la cascade D1, le PDH/PDL est par construction le range lui-même.
- **Invalidation :** le bord de la zone d'entrée (OB, Breaker ou FVG) côté stop. Voir « Gestion des trades ».
- **Type d'ordre :** `BUY`/`SELL` si le prix est déjà sur la zone (écart sous `ORDER_TYPE_TOLERANCE_PCT`), sinon `BUY LIMIT`/`SELL LIMIT` ou `BUY STOP`/`SELL STOP`.
- **OTE :** si l'entrée tombe dans le retracement 61,8-79 % du mouvement après le sweep, la ligne « ✅ OTE » est ajoutée. Avec `REQUIRE_FIB_OTE = True`, c'est un filtre obligatoire.

## Stratégie Impulsion (`impulse.py`)

**Actifs :** Volatility 10, 25, 50, 75, 100 et leurs variantes 1s. **Cascade :** identique au CRT.

**Règles (écrites pour un achat, la vente est le miroir)**
1. Tendance (HTF) : dernier sommet et dernier creux plus hauts que les précédents (2 swings suffisent), structure intacte (aucune clôture sous le dernier creux) et dernier sommet dépassé par une clôture (`BOS_BY_CLOSE`). L'âge est classé d'après le nombre de jambes consécutives : 1 = naissante, 2-3 = établie, 4 et plus = mûre (`AGE_ESTABLISHED`, `AGE_MATURE`). Seules les tendances naissantes et établies donnent un signal (`TREND_AGES_OK`).
2. Régime de volatilité : même filtre que le CRT, sur le MTF.
3. Impulsion (MTF) : cassure de structure avec déplacement, sur une jambe d'au moins 2 ATR. Le Fibonacci va de l'origine (creux) à l'extrême (plus haut, mèche comprise). Le setup est annulé si une bougie MTF ou LTF clôture au-delà de 79 % ou si l'origine est reprise. L'extrême doit dater de 48 bougies MTF au plus (`IMPULSE_MAX_AGE`).
4. Zone acceptée : de 50 % à 79 % de la jambe, mesurés à partir de l'extrême.
5. Entrée (LTF) : le prix touche la zone, balaie un creux LTF formé pendant le retracement, puis casse la structure avec déplacement en laissant un FVG (bougie centrale d'au moins 1,5 fois le corps moyen, taille d'au moins 0,1 ATR). L'OB est la dernière bougie baissière avant ce déplacement et doit chevaucher la zone. Si plusieurs combinaisons sweep/cassure existent, le bot garde celle qui offre le meilleur ratio.
6. Étiquette : « Golden Zone » si l'entrée est entre 50 % et 61,8 %, « OTE » entre 61,8 % et 79 %.
7. SL au-delà de l'extrême du sweep, plus 0,15 ATR. TP1 à l'extrême de l'impulsion, TP2 et TP3 aux extensions 1,272 et 1,618. Le ratio minimum de 1 pour 3 est calculé sur TP2 (`RR_ON = "tp1"` pour le calculer sur TP1).

**Niveaux d'entrée :** le milieu du FVG (prioritaire) ou l'extrémité de l'OB, dans la zone 50-79 %.

**Types d'ordres (selon la position du prix au moment du scan)**
1. `BUY LIMIT` / `SELL LIMIT` : le niveau n'a pas encore été atteint depuis la fin du FVG, le prix a fait au plus 50 % du chemin vers TP1 (`MAX_PROGRESS`) et le ratio est d'au moins 1 pour 3.
2. `BUY` / `SELL` instantané : le prix est dans l'OB et les niveaux limites ne sont plus disponibles.
3. `BUY` / `SELL` avec « ⏱ Entrée tardive » : le prix vient de quitter l'OB (`LATE_CANDLES`, `LATE_DIST`, `LATE_PROGRESS`).
4. Aucune alerte : le stop est franchi, TP1 est atteint, le ratio est trop faible ou le prix est déjà trop loin.

Une seule alerte est envoyée par impulsion (mémorisée 14 jours, `SENT_TTL`).

## Boutons « Pris / Ignoré » et commandes

Chaque alerte porte une référence (`Réf. #12`) et deux boutons. Appuie sur **✅ Pris** ou **❌ Ignoré** : le bot lit ta réponse au scan suivant (moins de 5 minutes) et remplace les boutons par la confirmation. Commandes à envoyer au bot :

- `/pris 12` ou `/ignore 12` : note le trade n°12 (si tu préfères le texte aux boutons).
- `/r 12 -0.8` : enregistre ton résultat réel du trade n°12, en R. Il sert à comparer avec le résultat simulé.
- `/bilan` : bilan des 30 derniers jours. Il sépare les trades pris, ignorés et non renseignés, et compare tes résultats réels au simulé quand tu les as saisis.
- `/aide` (ou tout autre texte commençant par `/`) : rappel des commandes.

Le bot ignore tout message qui ne vient pas de ton chat. Il lit les messages avec `getUpdates` : si un autre programme lit les messages du même bot Telegram, utilise un bot dédié pour celui-ci, sinon les messages se répartissent entre les deux.

## Graphique dans l'alerte

Chaque alerte est envoyée avec une image (`charts.py`) : les 70 dernières bougies LTF, l'entrée, le stop, les cibles, le sweep, le range (CRT) ou l'origine et l'extrême de l'impulsion, et les zones FVG, OB et d'entrée ou Fibonacci. Si l'image ne peut pas être produite, l'alerte part en texte seul. `CHARTS=0` désactive les images.

## Suivi et gestion des trades (`tracker.py`)

Chaque alerte est enregistrée dans `trades.json`. À chaque scan, le bot compare les bougies récentes à tes niveaux et envoie un message à chaque étape :

- **▶️ Ordre touché** : le prix a atteint l'entrée d'un ordre limite ou stop.
- **🎯 TP1 atteint** : clôture de 50 % de la position, stop remonté à l'entrée.
- **🎯 TP2 atteint** : clôture de 25 %, stop remonté à TP1, le reste vise TP3. **🏁 TP3 atteint** clôture le trade.
- **❌ Stop touché**, **➖ Stop à l'entrée touché** (après TP1) ou **🔒 Stop remonté touché** (après TP2), avec le résultat en R.
- **🔻 Structure cassée** : avant TP1, une bougie LTF a clôturé au-delà du bord de la zone d'entrée côté stop (`STRUCT_EXIT`). Le message te dit de fermer la position.
- **⏱ Sortie au temps** : TP1 n'a pas été atteint après 150 bougies LTF (`TIME_STOP_BARS`).
- **⚠️ ANNULER** : TP1 atteint sans exécution, stop franchi avant exécution, ou ordre expiré (24 h ; 48 h pour le CRT quand la LTF est le H1).

Réglages dans `tracker.py` : `PARTIAL_AT_TP1`, `PARTIAL_AT_TP2` (0 = tout clôturer à TP2), `BE_AT_TP1`, `LOCK_AT_TP2`, `STRUCT_EXIT`, `TIME_STOP_BARS` (0 = désactivé). Ces règles de gestion ne sont pas encore validées sur de l'historique réel : compare-les avec la grille du backtest (par exemple `tracker.STRUCT_EXIT=true,false;tracker.PARTIAL_AT_TP2=0,0.25`) avant de t'y fier.

Le suivi suppose que tu as placé l'ordre exactement aux niveaux de l'alerte et que tu exécutes les sorties annoncées. Si le stop et une cible sont touchés dans la même bougie, le stop est compté en premier (hypothèse prudente).

Un bilan hebdomadaire est envoyé (résultats 7 et 30 jours, par stratégie, cascade et statut pris/ignoré, trades actifs, entonnoir de filtres). En local ou dans les logs : `python bot.py --stats`.

## Garde-fous (`guard.py`)

- **Un seul trade actif par actif et par sens.** Les alertes en double entre cascades (D1>H4>H1 et H4>H1>M15 sur le même mouvement) sont bloquées : la cascade la plus haute passe en premier.
- **Exposition corrélée :** au plus 2 trades de même sens sur le dollar (forex et or comptent ensemble, un achat EURUSD équivaut à une vente USD) et 2 sur les cryptos (`GROUP_LIMIT`). Au plus 8 trades actifs en tout (`MAX_OPEN_TRADES`).
- **Perte journalière :** après −3 R sur la journée de trading (qui démarre à 17h New York), plus aucune alerte jusqu'au lendemain (`MAX_DAILY_LOSS_R`). Un message prévient une fois.
- **Fenêtres sans alerte (forex et or) :** rollover 16h55-17h15 New York, vendredi après 15h30, ouverture du dimanche 17h-18h, NFP (premier vendredi du mois, 8h-9h30). Les cryptos ne sont concernées que par `news.json`. Les indices synthétiques ne sont jamais bloqués.
- **Calendrier de news automatique :** le bot télécharge toutes les 6 heures le calendrier gratuit Forex Factory (flux Fair Economy, cette semaine et la suivante). Il retient les événements à fort impact des 8 grandes devises (USD, EUR, GBP, JPY, CHF, AUD, CAD, NZD) : CPI, emploi, décisions de taux BCE, BoE, BoJ, etc. Aucune alerte n'est envoyée de 15 minutes avant à 45 minutes après l'événement, et jusqu'à 90 minutes après une décision de taux ou une conférence de presse. Un événement en USD bloque aussi l'or et les cryptos ; un événement en EUR bloque les paires contenant l'euro, et ainsi de suite.
- **Décisions de la Fed (FOMC) :** les 8 dates de 2026 sont intégrées au code (fenêtre de 13h45 à 16h00 New York). Les dates 2027 seront à ajouter dans `FOMC_2026` en fin d'année.
- **Calendrier manuel :** copie `news.example.json` en `news.json` à la racine du dépôt pour ajouter tes propres événements, avec leurs heures en UTC. `currencies` est facultatif.
- Si le flux est indisponible, le bot réessaie 15 minutes plus tard et garde les dernières données ; les fenêtres fixes et le FOMC restent actives. Le flux est une source non officielle : vérifie dans les logs la ligne « Calendrier mis à jour : N événement(s) » après la première exécution.
- **Lot estimé :** si `ACCOUNT_BALANCE` est défini, chaque alerte affiche un lot. Les contrats sont supposés à 100 000 unités (forex), 100 oz (or) et 1 (cryptos). Pour un indice synthétique, renseigne la valeur de ta plateforme dans `LOT_UNITS_<SYMBOLE>` (par exemple `LOT_UNITS_R_75`), sinon aucun lot n'est calculé. C'est une estimation à vérifier sur ta plateforme avant d'ordonner.
- **Surveillance :** un message prévient si le bot reprend après plus de 30 minutes d'arrêt, ou si Deriv ne renvoie aucune donnée pendant 3 scans de suite (et quand c'est rétabli). Si GitHub ne lance plus rien du tout, le bot ne peut pas le dire : active les notifications d'échec de ton cron externe.
- **Entonnoir :** chaque scan compte combien de cascades passent chaque filtre (tendance, sweep, range, POI, cassure, trade valide, bloquées, alertes). Ces compteurs s'affichent dans les logs et le bilan. Un même setup peut être compté à plusieurs scans successifs.

## Validation : backtest (`backtest.py`)

Le backtest rejoue la stratégie sur de l'historique M5, avec les mêmes fonctions que le bot en direct, les mêmes garde-fous et la même gestion des trades. Les autres timeframes sont reconstruits depuis le M5 sans regarder le futur. Il sert à répondre à une seule question : les résultats tiennent-ils hors de la période où l'on a réglé les paramètres ?

**Depuis GitHub :** Actions → Backtest → Run workflow. Champs : actifs (un ou plusieurs, séparés par des virgules), jours d'historique, stratégie, spread, séparation en deux périodes (0.6 par défaut) et grille de réglages facultative (réglages séparés par des points-virgules). Le rapport arrive sur Telegram et dans les logs. Compte environ une minute par actif pour 100 jours, et autant par combinaison de la grille.

**En local :**
```
python backtest.py --sym R_75 --source deriv --days 90 --spread-pct 0.01
python backtest.py --sym R_50,R_75,R_100 --days 90 --split 0.6
python backtest.py --sym R_75 --days 90 --split 0.6 --set impulse.IMPULSE_ATR=1.5,2,2.5
python backtest.py --sym frxEURUSD --source csv --file eurusd_m5.csv
python backtest.py --sym R_75 --source sim --days 90      # marche aléatoire, contrôle de bon sens
```

**Ce que contient le rapport**
- Nombre de trades, taux de gain, total en R, facteur de profit (PF), drawdown maximal, avec le coût du spread.
- **Intervalle de confiance à 95 %** de l'espérance par trade (rééchantillonnage) et un verdict prudent : « positive », « négative » ou « indistinguable de zéro ». Sous 30 trades, le verdict refuse de conclure.
- **Plusieurs actifs :** un total cumulé et le détail par actif.
- **Deux périodes (`--split`) :** les résultats de chaque période et un avertissement si leurs signes s'opposent.
- **Détail** par cascade, sens, âge de tendance (Impulsion), session et liquidité prise (CRT), et type de sortie.
- **Grille (`--set module.NOM=v1,v2`) :** une ligne par combinaison, avec le résultat de chaque période. Retiens un réglage seulement si les deux périodes sont positives et si ses voisins donnent des résultats proches ; un réglage brillant sur une période et mauvais sur l'autre est du sur-ajustement. Maximum 30 combinaisons.

**Méthode conseillée :** (1) un rapport simple sur plusieurs actifs et 90 jours ou plus, avec le spread de ton courtier ; (2) la même chose avec `--split 0.6` ; (3) une grille sur les réglages qui t'intéressent, jugée sur la période 2 ; (4) n'activer en réel que ce qui tient partout. Le backtest suppose un spread constant, aucun glissement, et ne tient pas compte des news (calendrier ni `news.json`).

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

Invalidation  31828.1  (clôture au-delà = sortie)

Lot  0.12  (risque 1 % de 1000)
Gestion : 50 % à TP1 (SL à l'entrée), 25 % à TP2 (SL à TP1), 25 % vers TP3. Sortie si structure cassée ou TP1 non atteint en 150 bougies

Réf. #12
```

L'alerte arrive avec le graphique du setup et les boutons Pris / Ignoré. Chaque TP affiche son ratio gain/risque depuis l'entrée prévue. Les prix sont arrondis au nombre de décimales de l'actif et envoyés en police à espacement fixe, copiables d'un toucher dans Telegram. La ligne « Lot » n'apparaît que si `ACCOUNT_BALANCE` est défini. Les alertes CRT suivent le même style : type d'ordre dans l'en-tête, session sous l'en-tête, ligne Entrée avec son origine (Order Block, Breaker Block, FVG 50 % ou clôture de la cassure), SL, TP1 à TP3, puis ✅ sur le sweep, le FVG, l'OB et l'OTE.

## Réglages principaux

| Où | Constante | Effet |
|---|---|---|
| `bot.py` | `SYMBOLS` | Actifs du CRT (`CRT_SYNTHETIC=1` ajoute les indices) |
| `bot.py` | `CASCADES` | Triplets de timeframes (partagés avec `impulse.py`) |
| `bot.py` | `KILLZONES_NY`, `KILLZONE_24_7` | Sessions du CRT, et filtre éventuel sur les cryptos |
| `bot.py` | `NY_ALIGNED` | Alignement 17h New York pour le forex et l'or |
| `bot.py` | `STOP_LOSS_BUFFER_PCT`, `SL_BUFFER_ATR` | Marge du SL (le plus grand des deux) |
| `bot.py` | `RANGE_MIN_ATR`, `RANGE_MAX_ATR` | Taille acceptée du range de référence |
| `bot.py` | `VOL_FAST`, `VOL_SLOW`, `VOL_MIN`, `VOL_MAX` | Filtre de régime de volatilité (aussi pour l'Impulsion) |
| `bot.py` | `EQ_TOL_ATR`, `REQUIRE_LIQUIDITY` | Equal highs/lows et liquidité obligatoire |
| `bot.py` | `COUNT_H1_NY` | Historique H1 pour reconstruire D1/H4 (forex et or) |
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
| `tracker.py` | `PARTIAL_AT_TP1`, `PARTIAL_AT_TP2`, `BE_AT_TP1`, `LOCK_AT_TP2` | Clôtures partielles et stops remontés |
| `tracker.py` | `STRUCT_EXIT`, `TIME_STOP_BARS` | Sortie sur structure cassée et sortie au temps |
| `guard.py` | `MAX_OPEN_TRADES`, `GROUP_LIMIT`, `MAX_DAILY_LOSS_R` | Limites de risque |
| `guard.py` | `ROLLOVER`, `FRIDAY_CLOSE_FROM`, `SUNDAY_OPEN`, `NFP` | Fenêtres sans alerte fixes |
| `guard.py` | `NEWS_BEFORE_MIN`, `NEWS_AFTER_MIN`, `BIG_AFTER_MIN`, `FOMC_2026` | Calendrier automatique et décisions de la Fed |
| `guard.py` | `GAP_ALERT_MIN`, `FAILS_ALERT`, `REPORT_EVERY` | Surveillance et bilan |

## Dépannage

- **Erreurs « Handshake status 520 » :** l'endpoint Deriv refuse la connexion. Le bot alterne entre le nouvel et l'ancien endpoint.
- **Aucun message Telegram :** vérifie que `TELEGRAM_TOKEN` n'est pas vide dans les logs. Le log affiche « ATTENTION… message NON envoyé » si un secret manque.
- **Aucune alerte pendant des heures :** c'est normal. Lis la ligne « Entonnoir » dans les logs pour voir où les setups sont filtrés.
- **Alerte absente alors que le setup semble valide :** vérifie le log (« Alerte bloquée… ») : un trade actif sur le même actif, une limite de risque ou la pause journalière peut l'avoir bloquée.
- **Plus aucune exécution :** vérifie le tableau de bord du cron externe (réponse `204`) et la date d'expiration du token GitHub.
- **Les boutons ne réagissent pas :** le bot lit tes réponses à chaque scan (moins de 5 minutes). Si rien ne se passe, vérifie qu'aucun autre programme ne lit les messages du même bot (voir « Boutons »).
- **Pas d'image dans l'alerte :** vérifie que `matplotlib` est bien installé (étape `pip install -r requirements.txt`) ; le log affiche « Graphique ignoré » avec la cause.
- **Repartir de zéro :** supprime le cache GitHub Actions (onglet Actions → Caches) pour effacer `trades.json`, `guard.json` et les états.

## Limites et avertissements

- Le code a été testé sur des bougies simulées (marche aléatoire), avec un faux serveur Deriv pour le scan en direct. Il n'a pas été validé sur de l'historique réel : lance le backtest sur plusieurs actifs avant de t'y fier.
- Sur une marche aléatoire, le backtest ne montre aucun avantage statistique, ce qui est normal : les concepts de sweep et de structure supposent de vrais participants. Les Volatility Index sont générés par un algorithme, donc leurs signaux sont à prendre avec prudence jusqu'à ce qu'un backtest sur l'historique réel dise le contraire.
- Pas repris : tag de contre-tendance, spread en direct, ouverture de la journée ou de la semaine comme référence, corrélation entre l'or et les paires en euro.
- Les règles de gestion (clôtures partielles, sortie sur structure cassée, sortie au temps), le filtre de volatilité et la liquidité prise sont des réglages raisonnables mais non validés : le backtest sur l'historique réel doit les confirmer.
- Les alertes arrivent jusqu'à 5 minutes après la bougie (plus le délai de démarrage de GitHub Actions). Sur les indices 1s, le prix peut bouger entre l'alerte et ton ordre.
- Ce projet n'est pas un conseil financier. Les résultats passés ou simulés ne garantissent rien. Teste d'abord sur un compte démo.
