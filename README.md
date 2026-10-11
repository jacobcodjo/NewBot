# Crt-Bot : alertes CRT et Impulsion (Deriv + Telegram)

Deux scripts Python qui analysent les bougies de l'API Deriv et envoient des alertes Telegram. Ils n'exécutent aucun ordre : tu places toi-même l'ordre sur ta plateforme.

| Fichier | Rôle |
|---|---|
| `bot.py` | Stratégie CRT (Candle Range Trading) sur 47 actifs, plus le client Deriv, les notifications et les fonctions partagées avec `impulse.py` |
| `impulse.py` | Stratégie Impulsion + retracement Fibonacci sur 10 Volatility Index |
| `outlook.py` | Récapitulatif hebdomadaire du biais D1/W1 (repris de pa-bot) |
| `tracker.py` | Suivi automatique des trades du CRT (`trade_stats.json`) et de l'Impulsion (`trade_stats_impulse.json`) |
| `backtest.py` | Backtest du CRT et de l'Impulsion sur l'historique Deriv |
| `crt.yml` | Workflow GitHub Actions du bot, à placer dans `.github/workflows/crt.yml` |
| `backtest.yml` | Workflow GitHub Actions du backtest, à placer dans `.github/workflows/backtest.yml` |
| `requirements.txt` | Dépendances (`websocket-client`, `requests`) |

## Mise en place

1. Crée un dépôt GitHub. Mets `bot.py`, `impulse.py`, `outlook.py`, `tracker.py`, `backtest.py` et `requirements.txt` à la racine. Pour les workflows, crée les fichiers `.github/workflows/crt.yml` et `.github/workflows/backtest.yml` (« Add file → Create new file », avec ce chemin comme nom) et colle le contenu de `crt.yml` et de `backtest.yml`.
2. Crée un bot Telegram avec @BotFather (`/newbot`) et garde le token.
3. Envoie un message à ton bot, puis ouvre `https://api.telegram.org/bot<TOKEN>/getUpdates` pour lire ton `chat id` (`"chat":{"id":...}`).
4. Dans le dépôt : Settings → Secrets and variables → Actions → New repository secret. Crée `TELEGRAM_TOKEN` et `TELEGRAM_CHAT_ID` (noms exacts, dans « Repository secrets »).
5. Onglet Actions : active les workflows si GitHub le demande.
6. Test : Actions → CRT Bot → Run workflow, avec « test » coché. Chaque bot envoie un message de contrôle (« Bot CRT OK… » et « Impulsion OK… »). Le message du CRT contient aussi le résumé du suivi des trades. Sans « test », aucun message de contrôle n'est envoyé.

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

Stratégie et liste d'actifs reprises de l'ancien Crt-Bot. Le client Deriv, le cache, les notifications, les entrées/sorties et les alertes du second bot restent ceux de ce dépôt.

**Actifs (47) :** forex majeurs (EURUSD, GBPUSD, USDJPY, USDCHF, AUDUSD, USDCAD) et mineurs (paires croisées sans USD : EUR, GBP, AUD, CAD, CHF, NZD), l'or (XAUUSD), BTC, ETH, LTC, XRP, les Volatility Index classiques (10, 25, 50, 75, 100) et toutes les variantes 1 seconde (10, 15, 25, 30, 50, 75, 90, 100, 150, 200, 250, 300), et le Step Index. La liste est `SYMBOLS` dans `bot.py`.

**Principe**
1. Range de référence : haut/bas de la dernière période **W1** (semaine ISO complète, dérivée des bougies D1), **D1** ou **H4** clôturée.
2. Sweep de liquidité : une bougie du timeframe de confirmation dépasse le range puis referme dedans. Chaque paire référence/confirmation génère ses propres alertes.

   | Référence | Confirmation |
   |---|---|
   | W1 | H4 |
   | D1 | H1 |
   | H4 | M15 |

   Cette répartition (`REFERENCE_CONFIRMATION_MAP`) évite de confirmer un large range sur un timeframe trop fin. Pour désactiver une référence (le W1 par exemple), supprime sa ligne.
3. Confirmation avancée, calculée sur les bougies clôturées uniquement (la bougie en formation est ignorée, pour éviter les signaux qui disparaissent) : cassure de structure (clôture au-delà du dernier swing, fenêtre `STRUCTURE_SWING_WINDOW`, 1 pour M15 et 2 ailleurs) **et** Fair Value Gap et/ou Order Block. Sans FVG ni OB, le setup est ignoré. Un setup est aussi ignoré s'il est périmé : depuis la cassure, le prix a déjà touché le SL ou atteint TP1.
4. Une seule alerte par range de référence et par timeframe de confirmation : le premier setup confirmé verrouille le range jusqu'à la bougie de référence suivante. Un second contrôle sur le contenu (symbole, sens, entrée, SL, TP2) évite deux messages identiques. Les verrous sont purgés après 30 jours (`STATE_MAX_AGE_DAYS`).

**Filtres et indications**
- **Tendance de fond (D1) :** un setup à contre-tendance n'est jamais bloqué : la ligne « ⚠️ Contre-tendance (D1) » est ajoutée. La lecture de tendance est commune au CRT, à l'Impulsion et au récapitulatif hebdomadaire (voir « Lecture de la tendance » plus bas).
- **Killzones (heure de New York) :** Londres 3h-6h et New York 8h30-11h30, uniquement pour le forex et l'or. Les cryptos et les indices synthétiques (24/7) ne sont pas filtrés. Par défaut, c'est une indication : la ligne « Session » affiche le nom de la killzone du sweep, ou « hors killzone ». `REQUIRE_KILLZONE_FOR_REAL_MARKETS = True` ignore les sweeps hors killzone, et `KILLZONE_24_7 = True` applique aussi le filtre aux cryptos et indices synthétiques.
- **Filtre de gap de weekend (forex et or) :** une bougie qui suit un écart de temps anormal n'est jamais comptée comme un sweep (`WEEKEND_GAP_MULTIPLIER`).
- **Pools de liquidité (equal highs / equal lows) :** si le sweep a balayé un pool détecté, la ligne « 💧 Pool de liquidité balayé » est ajoutée. Si le stop-loss tombe lui-même sur un pool du même côté (creux égaux sous un achat, sommets égaux au-dessus d'une vente), il est écarté d'une marge supplémentaire (`STOP_LOSS_POOL_BUFFER_PCT`). Réglages : `LIQUIDITY_POOL_SWING_WINDOW` et `LIQUIDITY_POOL_TOLERANCE_PCT`.
- **D1 et H4 :** bougies natives de Deriv. `NY_ALIGNED = True` recalcule D1 et H4 depuis le H1 avec une ouverture à 17h New York (forex et or).

**Entrées et sorties**
- **Entrée, marchés réels (forex, or, cryptos) :** au niveau de la cassure de structure, par ordre de priorité : Order Block (dernière bougie opposée avant le mouvement, à 8 bougies au plus de la cassure), sinon Breaker Block, sinon milieu du FVG. Pour l'OB et le Breaker, le niveau est le bord que le prix atteint en premier (le haut pour un achat, le bas pour une vente). Si aucun des trois n'existe, le setup est ignoré.
- **Entrée, indices synthétiques :** immédiate, à la clôture de la bougie de cassure de structure. Ces actifs enchaînent souvent des mouvements directs sans revenir sur la zone, où un ordre en attente resterait non rempli.
- **Stop-loss :** au-delà de l'extrême de la bougie de sweep, avec une marge de 0,05 % du prix (`STOP_LOSS_BUFFER_PCT`).
- **Cibles (tous les actifs) :** TP1 à mi-range (50 %), TP2 au bord opposé du range de référence, TP3 au bord opposé plus 50 % de la taille du range (`TP_EXTENSION_PCT`).
- **Ratio :** calculé sur TP2. Une alerte n'est envoyée que si le ratio est d'au moins 1 pour 3 (`MIN_RISK_REWARD`).
- **Type d'ordre :** déterminé en comparant l'entrée au prix actuel (relevé au moment de l'envoi) : `BUY`/`SELL` si le prix est déjà sur la zone (écart sous `ORDER_TYPE_TOLERANCE_PCT`), sinon `BUY LIMIT`/`SELL LIMIT` ou `BUY STOP`/`SELL STOP`.
- **OTE :** si l'entrée tombe dans le retracement 61,8-79 % du mouvement impulsif après le sweep, la ligne « ✅ OTE » est ajoutée. Avec `REQUIRE_FIB_OTE = True`, c'est un filtre obligatoire.

Le CRT ne dépend plus des cascades à trois niveaux (`CASCADES`), de l'alignement 17h New York par défaut, des filtres premium/discount et de déplacement ni des killzones Asie et London Close : ils appartenaient à la version précédente du CRT. `CASCADES` reste dans `bot.py` pour l'Impulsion.

## Suivi automatique des trades (`tracker.py`)

Chaque alerte est enregistrée dans un fichier de statistiques par stratégie : `trade_stats.json` pour le CRT, `trade_stats_impulse.json` pour l'Impulsion (conservés par le cache du workflow, comme `state.json`).
- Un ordre au marché (`BUY`/`SELL`) est considéré rempli à l'envoi. Un ordre `LIMIT`/`STOP` doit d'abord être rempli : le prix atteint l'entrée. Un ordre jamais rempli après son délai est archivé « EXPIRE » (7 jours pour le CRT, `PENDING_MAX_AGE_DAYS` ; 24 h pour l'Impulsion, comme `PENDING_TTL`), sans compter comme gagnant ni perdant, et alimente le taux de remplissage.
- Une fois rempli, le premier niveau touché décide : **SL** (perdant) ou **cible de gain** (gagnant). La cible est TP2 pour le CRT et TP1 pour l'Impulsion : c'est le niveau sur lequel porte le ratio minimum de 1 pour 3 de chaque stratégie, et le R d'un gain est ce ratio. Pour le CRT, TP1 est noté à part. Si SL et cible sont touchés dans la même bougie, le SL est retenu. Sur la bougie de remplissage, seul le SL est pris en compte.
- Seules les bougies qui s'ouvrent après l'alerte comptent : aucun résultat n'utilise des prix antérieurs à l'envoi.
- Les bougies utilisées sont celles du timeframe de confirmation. Chaque trade mémorise la dernière bougie évaluée : il n'a donc pas besoin que l'historique récupéré (200 bougies) remonte jusqu'à son ouverture. Il est archivé « NON_RESOLU » si le bot reste arrêté plus longtemps que cet historique, ou si le trade reste ouvert plus de 30 jours (`TRADE_MAX_OPEN_DAYS`). Ces cas sont comptés à part.
- Le résumé (réussite globale, R moyen, TP1 atteint pour le CRT, contre-tendance ou non pour le CRT, classe d'actif, référence, taux de remplissage) s'affiche dans les logs à chaque passage et dans le message de test de chaque stratégie. L'historique de plus de 180 jours est purgé (`TRADE_HISTORY_MAX_AGE_DAYS`).

## Backtest (`backtest.py`)

Le script rejoue l'historique Deriv comme le bot l'aurait vu : à chaque clôture de bougie du timeframe de pas (M15 pour le CRT, M5 pour l'Impulsion), il reconstruit les bougies visibles, applique la stratégie, les mêmes verrous anti-doublon et le même suivi. Chaque alerte est ensuite suivie sur les bougies suivantes du timeframe de pas (plus fines que le suivi en direct, donc moins de cas SL et cible dans la même bougie).

- **Sur GitHub :** Actions → CRT Backtest → Run workflow, avec la stratégie (`crt` ou `impulse`), la durée en jours et, si besoin, des symboles séparés par des virgules (par exemple `R_75,frxEURUSD` pour le CRT, `R_75,1HZ100V` pour l'Impulsion). Le résumé s'affiche dans la page de l'exécution et le fichier `backtest_crt.csv` ou `backtest_impulse.csv` (avec les colonnes `tendance` et `mode_tendance`) est téléchargeable dans les artefacts. Le backtest complet des 47 actifs du CRT prend plusieurs minutes.
- **Lecture de tendance :** `--trend-mode structure` (défaut), `swings`, ou `both`, qui rejoue le même historique avec les deux règles et affiche un tableau comparatif (setups, réussite, R moyen, contre-tendance). Pour le CRT, la tendance n'est qu'une étiquette : seuls les résultats « contre-tendance » changent. Pour l'Impulsion, c'est un filtre : le nombre de setups change. Sur GitHub, c'est le champ « trend_mode » du workflow.
- **En local :** `python backtest.py --strategy impulse --days 14 --symbols R_75,1HZ100V --trend-mode both`
- **Résultat :** réussite et R moyen (SL = -1, gain = ratio de la cible : TP2 pour le CRT, TP1 pour l'Impulsion), plus TP1 atteint pour le CRT, globalement puis par classe d'actif, référence, tendance (CRT), type d'ordre et sens, plus les meilleurs et pires symboles (5 trades minimum).
- **Limites :** 5000 bougies par requête Deriv, soit 45 jours au maximum pour le CRT (M15) et 16 jours pour l'Impulsion (M5) : une durée plus longue est ramenée à ce maximum. Pas de spread, de commission ni de glissement. Les bougies D1 et H4 sont celles de Deriv (`NY_ALIGNED` est ignoré). Les marchés fermés suivent la même règle qu'en direct. Un backtest sur quelques semaines n'est pas statistiquement concluant et ne garantit rien pour la suite.

## Stratégie Impulsion (`impulse.py`)

**Actifs :** Volatility 10, 25, 50, 75, 100 et leurs variantes 1s. **Cascade (HTF → MTF → LTF) :** D1→H4→H1, H4→H1→M15, H1→M15→M5 (`CASCADES` dans `bot.py`, utilisée uniquement par l'Impulsion).

**Règles (écrites pour un achat, la vente est le miroir)**
1. Tendance (HTF) : haussière selon `bot.detect_trend` (voir « Lecture de la tendance » : structure de marché précoce par défaut, ou 3 swings + SMA 50 avec `TREND_MODE = swings`). Sinon, pas de signal. `STRICT_TREND = False` rétablit l'ancienne règle, encore plus souple : les 2 derniers sommets et creux plus hauts, sans cassure ni SMA.
2. Impulsion (MTF) : cassure de structure avec déplacement, sur une jambe d'au moins 2 ATR. Le Fibonacci va de l'origine (creux) à l'extrême (plus haut, mèche comprise). Le setup est annulé si une bougie MTF clôture au-delà de 79 % ou si l'origine est reprise.
3. Zone acceptée : de 50 % à 79 % de la jambe, mesurés à partir de l'extrême.
4. Entrée (LTF) : le prix touche la zone, balaie un creux LTF formé pendant le retracement (mèche sous le creux, clôture au-dessus), puis casse la structure avec déplacement en laissant un FVG. L'OB est la dernière bougie baissière avant ce déplacement. Il doit chevaucher la zone.
5. Étiquette : « Golden Zone » si le niveau d'entrée est entre 50 % et 61,8 %, « OTE » entre 61,8 % et 79 %.
6. SL au-delà de l'extrême du sweep, plus 0,15 ATR. TP1 à l'extrême de l'impulsion, avec un ratio minimum de 1 pour 3. TP2 et TP3 aux extensions 1,272 et 1,618.

**Niveaux d'entrée possibles :** le milieu du FVG (prioritaire) ou l'extrémité de l'OB (le bord que le prix atteint en premier : le haut de l'OB pour un achat, le bas pour une vente). Le niveau doit être dans la zone 50-79 %.

**Types d'ordres (selon la position du prix au moment du scan)**
1. `BUY LIMIT` / `SELL LIMIT` : le milieu du FVG est choisi s'il n'a pas encore été atteint par le prix depuis la fin du FVG, si le prix a fait au plus 50 % du chemin entre ce niveau et TP1 (`MAX_PROGRESS`), et si le ratio à ce niveau est d'au moins 1 pour 3. Sinon, le même test est fait avec l'extrémité de l'OB. Le niveau choisi est indiqué entre parenthèses sur la ligne « Entrée ».
2. `BUY` / `SELL` (ordre instantané, sans LIMIT) : le prix est dans l'OB et les niveaux limites ne sont plus disponibles. Entrée : prix actuel, ratio recalculé, minimum 1 pour 3.
3. `BUY` / `SELL` avec la mention « ⏱ Entrée tardive » : le prix vient de quitter l'OB. Il faut qu'il ait touché l'OB depuis 2 bougies au plus (`LATE_CANDLES`), qu'il soit à moins d'une demi-hauteur d'OB du bord de l'OB (`LATE_DIST`), qu'il ait fait moins de 25 % du chemin entre ce bord et TP1 (`LATE_PROGRESS`), et que le ratio recalculé au prix actuel soit d'au moins 1 pour 3.
4. Aucune alerte : le stop est franchi, TP1 est atteint, le ratio est inférieur à 1 pour 3, ou le prix est déjà trop loin après avoir quitté l'OB.

**Ordres limites en attente :** un message « ANNULER » est envoyé si TP1 est atteint sans exécution ou si le stop est franchi avant exécution. L'ordre en attente expire après 24 h. Une seule alerte est envoyée par impulsion.

**Suivi et backtest :** chaque alerte est suivie dans `trade_stats_impulse.json` (voir plus haut). Le gain est compté sur TP1, la cible du ratio de 1 pour 3. Le message de test affiche le résumé, et `python backtest.py --strategy impulse` rejoue la stratégie sur l'historique Deriv.

## Lecture de la tendance (`bot.detect_trend`)

Une seule fonction lit la tendance de fond, pour le tag contre-tendance du CRT (D1), le filtre de tendance de l'Impulsion (timeframe de référence) et le récapitulatif hebdomadaire (D1 et W1). Elle renvoie `bullish`, `bearish`, `range` (ou rien si l'historique est trop court). Deux règles, choisies par `TREND_MODE` dans `bot.py` :

- **`structure` (défaut, précoce).** Pivots confirmés 3 bougies après leur formation (`TREND_STRUCTURE_WINDOW`). La tendance est haussière si les deux derniers creux montent, si les deux derniers sommets montent **ou** si le dernier cours clôture au-dessus du dernier sommet (cassure de structure), et si le cours reste au-dessus du dernier creux. Baissière : le miroir. Elle demande 2 sommets et 2 creux au lieu de 3 et 3, sans SMA, et elle s'arrête dès que le dernier creux (ou sommet) est cassé.
- **`swings` (ancienne règle, tardive).** 3 sommets et 3 creux consécutifs dans le même sens, fenêtre de 4 bougies, prix du bon côté de la SMA 50.

Deux pivots consécutifs au même prix comptent pour un seul swing dans les deux règles.

**Ce que ça change, mesuré sur des données fictives :** sur un retournement construit à la main, le mode `structure` signale la nouvelle tendance 20 bougies après le creux, contre 44 pour `swings`. En fin de tendance, il ne sort pas plus tôt (14 bougies après le sommet, contre 12). Le prix à payer est le bruit : sur un marché aléatoire sans dérive, `structure` annonce une tendance 62 % du temps et change de sens environ 43 fois par série de 1400 bougies, contre 23 % et environ 9 fois pour `swings`. Il filtre donc beaucoup moins : c'est surtout vrai pour les Volatility Index, qui sont des marchés simulés. Compare les deux règles sur l'historique réel avec `python backtest.py --trend-mode both` avant de te fier à l'une ou à l'autre.

**Changer de règle sans modifier le code :** crée une variable de dépôt `TREND_MODE` (Settings → Secrets and variables → Actions → Variables) avec la valeur `swings` ou `structure`. Sans variable, c'est `structure`.

## Récapitulatif hebdomadaire (`outlook.py`)

Repris de pa-bot. Une fois par week-end, le bot envoie le biais directionnel de fond (D1 et W1) de chaque actif. Ce n'est pas une stratégie : c'est la même lecture de tendance que le tag contre-tendance du CRT (`bot.detect_trend`, voir « Lecture de la tendance »), sans niveaux d'entrée ni de sortie. C'est un repère pour préparer la semaine.

```
📅 Outlook de la semaine — 09:00 (UTC+1)

Forex
EURUSD : 🟢 Haussier (D1) / 🟢 Haussier (W1)
GBPUSD : 🔴 Baissier (D1) / ⚪ Range (W1)

Or
XAUUSD : 🟢 Haussier (D1) / 🟢 Haussier (W1)

Crypto
BTCUSD : 🟢 Haussier (D1) / ⚪ Range (W1)

Indices synthétiques
Volatility 75 : ⚪ Range (D1) / ⚪ Range (W1)
```

- **Envoi :** dès la fermeture du forex (vendredi ~22h UTC), une seule fois par semaine ISO grâce au marqueur `weekly_outlook_state.json` (conservé par le cache du workflow). Si Telegram échoue, le marqueur n'est pas écrit et l'envoi est retenté au passage suivant.
- **Mise à jour du dimanche :** les marchés 24/7 (cryptos et indices synthétiques) continuent de trader, donc leur lecture du vendredi vieillit. Un second message, limité à ces actifs, part le dimanche à partir de 20h UTC, avant la réouverture du forex (~22h UTC).
- **Aucun coût hors week-end :** le script tourne à chaque passage du workflow mais se termine immédiatement tant qu'aucun envoi n'est dû, sans appel à Deriv.
- **Données :** 500 bougies D1 par actif (le W1 demande plus d'historique que les 200 bougies du scan), lues une fois par week-end avec une date de fin explicite pour fonctionner marché fermé. Les messages sont coupés entre deux lignes au-delà de 3500 caractères.
- **Lecture W1 :** la semaine en cours est comptée comme terminée, puisque le message part au week-end. La dernière bougie d'un actif forex ou or est considérée clôturée tant que le marché est fermé.
- **Test :** lancer le workflow avec « test » envoie le récapitulatif tout de suite, sans toucher au marqueur : le vrai envoi du week-end n'est pas bloqué.
- **Réglages (`outlook.py`) :** `OUTLOOK_INCLUDE_SYNTHETIC` (`False` retire les indices synthétiques), `REFRESH_HOUR_UTC`, `D1_COUNT`, `DISPLAY_TZ` (`Africa/Lagos` = UTC+1 fixe ; mets `Africa/Abidjan` pour UTC+0). Les horaires de fermeture du forex (`FOREX_CLOSE_UTC`, `FOREX_OPEN_UTC`) sont ceux de pa-bot, sans gestion du changement d'heure.
- **Écart avec pa-bot :** son outlook retirait les bougies D1 forex qui suivent un weekend, donc tous les lundis, ce qui faussait les sommets, les creux et le W1. Cette étape n'est pas reprise ici.

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
```

Chaque TP affiche son ratio gain/risque, calculé depuis l'entrée prévue (le filtre de l'Impulsion porte sur TP1). Les prix sont arrondis au nombre de décimales de l'actif et envoyés en police à espacement fixe (balise `<code>`), copiables d'un toucher dans Telegram. L'en-tête d'un ordre instantané ne contient que `BUY` ou `SELL`. Il se termine par le nom de la stratégie (`CRT` ou `Impulsion`), y compris sur le message d'annulation. Les alertes CRT suivent le même style : le type d'ordre dans l'en-tête (`BUY LIMIT`, `SELL STOP`, ou `BUY`/`SELL` au marché) avec le timeframe de confirmation, la session sous l'en-tête (killzone, « hors killzone » ou « 24/7 »), la ligne Entrée avec son origine entre parenthèses (Order Block, Breaker Block, FVG 50 % ou clôture de la cassure), SL, TP1 à TP3 avec le ratio de chaque cible (le filtre de 1 pour 3 porte sur TP2), la ligne « Range » précédée de la référence (W1, D1 ou H4), puis ✅ sur le sweep, le FVG, l'OB et l'OTE le cas échéant, et enfin « 💧 Pool de liquidité balayé » et « ⚠️ Contre-tendance (D1) » lorsqu'ils s'appliquent.

## Réglages principaux

| Où | Constante | Effet |
|---|---|---|
| `bot.py` | `SYMBOLS` | Liste des actifs du CRT (47) |
| `bot.py` | `REFERENCE_CONFIRMATION_MAP` | Référence (W1, D1, H4) et timeframe de confirmation associé |
| `bot.py` | `STRUCTURE_SWING_WINDOW`, `DEFAULT_STRUCTURE_SWING_WINDOW` | Fenêtre des swings de la cassure de structure, par timeframe de confirmation |
| `bot.py` | `TREND_SWING_WINDOW`, `TREND_SWING_COUNT`, `TREND_SMA_PERIOD` | Mode `swings` : fenêtre, nombre de swings et SMA de la tendance |
| `bot.py` | `TREND_MODE` | Lecture de tendance : `structure` (précoce, défaut) ou `swings` (3 swings + SMA 50) ; peut être imposé par la variable de dépôt `TREND_MODE` |
| `bot.py` | `TREND_STRUCTURE_WINDOW` | Mode structure : bougies de confirmation d'un pivot |
| `bot.py` | `KILLZONES_NY` | Noms et horaires des sessions (heure de New York) |
| `bot.py` | `REQUIRE_KILLZONE_FOR_REAL_MARKETS` | `True` ignore les sweeps hors killzone (forex et or) |
| `bot.py` | `KILLZONE_24_7` | `True` pour filtrer aussi cryptos et indices synthétiques |
| `bot.py` | `NY_ALIGNED` | Alignement 17h New York pour le forex et l'or (désactivé par défaut) |
| `bot.py` | `WEEKEND_GAP_PREFIXES`, `WEEKEND_GAP_MULTIPLIER` | Filtre de gap de weekend |
| `bot.py` | `LIQUIDITY_POOL_SWING_WINDOW`, `LIQUIDITY_POOL_TOLERANCE_PCT`, `STOP_LOSS_POOL_BUFFER_PCT` | Pools de liquidité et marge de SL associée |
| `bot.py` | `STATE_MAX_AGE_DAYS` | Durée de conservation des verrous anti-doublon |
| `outlook.py` | `OUTLOOK_INCLUDE_SYNTHETIC`, `REFRESH_HOUR_UTC`, `D1_COUNT`, `DISPLAY_TZ` | Récapitulatif hebdomadaire : actifs inclus, heure de la mise à jour du dimanche, historique lu, fuseau d'affichage |
| `tracker.py` | `PENDING_MAX_AGE_DAYS`, `TRADE_MAX_OPEN_DAYS`, `TRADE_HISTORY_MAX_AGE_DAYS` | Expiration des ordres CRT jamais remplis, trade ouvert trop longtemps, durée de l'historique |
| `bot.py` | `CASCADES` | Triplets de timeframes (utilisés par `impulse.py`, plus par le CRT) |
| `bot.py` | `STOP_LOSS_BUFFER_PCT` | Marge du SL au-delà de l'extrême du sweep (% du prix) |
| `bot.py` | `TP_EXTENSION_PCT` | Prolongement de TP3 au-delà du bord opposé du range (% de la taille du range) |
| `bot.py` | `MIN_RISK_REWARD` | Ratio minimum du CRT, calculé sur TP2 |
| `bot.py` | `ORDER_TYPE_TOLERANCE_PCT` | Écart sous lequel le prix est « déjà sur la zone » (ordre au marché) |
| `bot.py` | `OB_LOOKBACK` | Distance maximale (en bougies de confirmation) entre l'OB et la cassure |
| `bot.py` | `REQUIRE_FIB_OTE` | Rend l'OTE obligatoire pour une alerte CRT |
| `bot.py` | `MIN_INTERVAL`, `MAX_RETRIES` | Débit et nombre d'essais du client Deriv |
| `impulse.py` | `SYMBOLS` | Liste des indices de la stratégie Impulsion |
| `impulse.py` | `STRICT_TREND` | `True` : 3 swings + SMA 50 pour la tendance ; `False` : ancienne règle à 2 swings |
| `impulse.py` | `ZONE` | Zone de retracement acceptée (0,5 à 0,79) |
| `impulse.py` | `IMPULSE_ATR` | Taille minimale de la jambe (2 ATR) |
| `impulse.py` | `MIN_RR` | Ratio gain/risque minimum (3 = 1 pour 3) |
| `impulse.py` | `SL_BUFFER_ATR` | Tampon du stop-loss |
| `impulse.py` | `MAX_PROGRESS` | Part maximale du chemin entrée → TP1 déjà parcourue pour un ordre limite (0,5 = 50 %) |
| `impulse.py` | `LATE_CANDLES`, `LATE_DIST`, `LATE_PROGRESS` | Tolérance de l'entrée tardive (bougies, distance en hauteur d'OB, progression vers TP1) |
| `impulse.py` | `FRESH` | Ancienneté maximale de la cassure LTF (en bougies) |
| `impulse.py` | `PENDING_TTL` | Durée de vie d'un ordre limite en attente |
| `impulse.py` | `STATS_FILE` | Fichier de suivi des trades de l'Impulsion |

## Dépannage

- **Erreurs « Handshake status 520 » :** l'endpoint Deriv refuse la connexion. Le bot alterne entre le nouvel et l'ancien endpoint. Si cela persiste, envoie les premières lignes du log.
- **Aucun message Telegram :** vérifie que `TELEGRAM_TOKEN` n'est pas vide dans les logs (une valeur de secret remplie s'affiche `***`). Le log affiche « ATTENTION… message NON envoyé » si un secret manque.
- **Pas de récapitulatif le week-end :** lance le workflow avec « test » pour vérifier l'envoi. Vérifie que `weekly_outlook_state.json` figure dans les chemins du cache de `crt.yml` et que l'étape `python outlook.py` y est présente.
- **Suivi vide :** le résumé reste « aucun trade résolu » tant qu'aucun SL ni TP2 n'a été touché. Vérifie que `trade_stats.json` et `trade_stats_impulse.json` figurent dans les chemins du cache de `crt.yml`.
- **Aucune alerte pendant des heures :** c'est normal. Toutes les conditions doivent être réunies, surtout avec le ratio de 1 pour 3.
- **Plusieurs alertes au premier passage :** le CRT analyse tous les sweeps depuis l'ouverture du range de référence (jusqu'à une semaine pour le W1). Les setups déjà valides à ce moment-là sont envoyés une fois, puis verrouillés.
- **Symbole absent :** lis la ligne « Symboles Deriv… » au début du log.
- **Plus aucune exécution :** vérifie le tableau de bord du cron externe (réponse `204` attendue) et la date d'expiration du token GitHub.

## Limites et avertissements

- Le code, y compris la stratégie reprise de l'ancien bot, a été testé sur des bougies fictives et avec un faux serveur. Il n'a pas été validé sur plusieurs jours de données réelles, et aucun backtest n'a été lancé sur des données réelles. Les réglages (2 ATR, déplacements, tailles de zone) peuvent demander des ajustements.
- Les Volatility Index sont des marchés simulés par un algorithme, sans vrais participants. Les concepts ICT comme le sweep de liquidité reposent sur le comportement de vrais marchés : les signaux sur ces indices sont à prendre avec prudence.
- Les alertes arrivent jusqu'à 5 minutes après la bougie (plus le délai de démarrage de GitHub Actions). Sur les indices 1s, le prix peut bouger entre l'alerte et ton ordre.
- Ce projet n'est pas un conseil financier. Les résultats passés ou simulés ne garantissent rien. Teste d'abord sur un compte démo.
