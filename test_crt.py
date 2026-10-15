"""Tests de la logique CRT sur des données synthétiques (aucun accès réseau).

Lancement depuis la racine du dépôt : python -m unittest discover -s tests -v
Chaque test construit un scénario minimal (range D1 de 100 à 110, sweep au-dessus puis retournement baissier en H1)
et vérifie qu'un garde-fou précis accepte ou rejette le setup."""
import calendar, os, sys, tempfile, types, unittest

for _m in ("websocket", "requests"):   # le client Deriv n'est pas utilisé ici
    try:
        __import__(_m)
    except ImportError:
        sys.modules[_m] = types.ModuleType(_m)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot, tracker  # noqa: E402

H = 3600
E0 = calendar.timegm((2026, 10, 6, 0, 0, 0))   # mardi 6 octobre 2026, 00:00 UTC (début de C2 en D1)
NOW = E0 + 12 * H
SYM = "cryBTCUSD"


def C(e, o, h, l, c):
    return {"epoch": e, "open": o, "high": h, "low": l, "close": c}


def make_d1(fvg=False):
    cs, p = [], 100.0
    for k in range(40, 1, -1):
        cs.append(C(E0 - k * 86400, p, p + 3, p - 3, p + 0.5))
        p += 0.3
    if fvg:   # FVG baissier non comblé [109.5, 115] créé 8 à 6 jours avant (les clôtures suivantes restent sous 115)
        cs[-8] = C(cs[-8]["epoch"], 116, 117, 115, 115.5)
        cs[-7] = C(cs[-7]["epoch"], 115, 115.2, 109.8, 110)
        cs[-6] = C(cs[-6]["epoch"], 110, 109.5, 108, 108.5)
    cs.append(C(E0 - 86400, 104, 110, 100, 107))   # C1 : range 100-110
    cs.append(C(E0, 107, 110.4, 105, 108))          # C2 en formation
    return cs


def make_h1(sweep_high=110.4, extra=(), pre=()):
    cs = [C(E0 - k * H, 108.5, 109, 108, 108.5) for k in range(60, 0, -1)]
    seq = [
        (108.5, 109.2, 108.0, 108.8), (108.8, 109.4, 108.4, 108.6), (108.6, 109.0, 107.8, 108.4),
        (108.4, 109.1, 108.3, 108.9), (108.9, 109.3, 108.6, 109.1), (109.1, 109.5, 108.9, 109.2),
        (109.2, sweep_high, 109.0, 109.8),     # 6 : sweep (mèche au-dessus de 110, clôture dans le range)
        (109.7, 109.8, 105.5, 106.0),          # 7 : déplacement baissier, casse 107.8
        (106.0, 106.6, 105.9, 106.4), (106.4, 107.0, 106.1, 106.5),
    ]
    for i, row in enumerate(seq):
        cs.append(C(E0 + i * H, *row))
    for k, row in enumerate(extra):
        cs.append(C(E0 + (10 + k) * H, *row))
    for k, row in enumerate(pre):   # bougies insérées avant le sweep (indices négatifs : veille)
        cs.insert(len(cs) - len(seq) - len(extra), C(E0 - (len(pre) - k) * H, *row))
    last = cs[-1]["epoch"] + H
    cs.append(C(last, 106.5, 106.5, 106.5, 106.5))   # bougie en formation
    return cs


def run(ltf=None, d1=None, now=NOW, **flags):
    saved = {k: getattr(bot, k) for k in flags}
    for k, v in flags.items():
        setattr(bot, k, v)
    try:
        return bot.analyze(SYM, {"D1": d1 or make_d1()}, {"H1": ltf or make_h1()}, now=now)
    finally:
        for k, v in saved.items():
            setattr(bot, k, v)


class Setups(unittest.TestCase):
    def test_scenario_de_base(self):
        r = run()
        self.assertEqual(len(r), 1)
        s = r[0]
        self.assertEqual((s["dir"], s["ref_n"], s["conf_n"]), ("SELL", "D1", "H1"))
        self.assertEqual(s["trade"]["how"], "Order Block")
        self.assertGreaterEqual(s["trade"]["rr"], 3)
        self.assertLess(s["trade"]["entry"], s["sw"]["sl"] + 1)
        self.assertTrue(s["trade"]["tp1"] < s["trade"]["entry"] < s["sw"]["sl"] < s["trade"]["sl"] + 1)

    def test_cle_contient_le_sens(self):
        k1, _ = bot.setup_keys(run()[0])
        self.assertTrue(k1.endswith("_SELL"))

    def test_prix_actuel_hors_range(self):
        ltf = make_h1()
        ltf[-1] = C(ltf[-1]["epoch"], 111, 111, 111, 111)
        self.assertEqual(run(ltf=ltf), [])

    def test_cloture_hors_range_apres_le_sweep(self):
        ltf = make_h1(extra=[(106.5, 110.5, 106.4, 110.2)])
        self.assertEqual(run(ltf=ltf), [])
        self.assertEqual(len(run(ltf=ltf, REQUIRE_C2_CLOSE_INSIDE=False)), 1)

    def test_cassure_peu_impulsive(self):
        ltf = make_h1()
        i7 = [i for i, c in enumerate(ltf) if c["epoch"] == E0 + 7 * H][0]
        ltf[i7] = C(E0 + 7 * H, 107.85, 108.0, 107.6, 107.7)
        self.assertEqual(run(ltf=ltf), [])
        self.assertEqual(len(run(ltf=ltf, MSS_REQUIRE_DISPLACEMENT=False)), 1)

    def test_sweep_trop_profond(self):
        self.assertEqual(run(ltf=make_h1(sweep_high=116.0)), [])

    def test_sweep_trop_ancien(self):
        self.assertEqual(run(SWEEP_MAX_AGE={"H1": 1}), [])

    def test_deux_cotes_balayes(self):
        ltf = make_h1()
        i2 = [i for i, c in enumerate(ltf) if c["epoch"] == E0 + 2 * H][0]
        ltf[i2] = C(E0 + 2 * H, 108.6, 109.0, 99.5, 108.4)   # balaie aussi le bas du range
        sweeps = bot.detect_sweeps(ltf[:-1], {"high": 110, "low": 100, "epoch": E0 - 86400}, H, False)
        self.assertEqual({x["dir"] for x in sweeps}, {"BUY", "SELL"})   # la détection voit bien les deux côtés
        self.assertEqual(run(ltf=ltf), [])

    def test_range_hors_normes(self):
        self.assertEqual(run(RANGE_MIN_ATR=50.0), [])
        self.assertEqual(run(RANGE_MAX_ATR=0.1), [])

    def test_entree_deja_retestee(self):
        retest = [(106.0, 109.2, 106.0, 108.5), (108.5, 108.6, 107.0, 107.5)]
        self.assertEqual(run(ltf=make_h1(extra=retest)), [])
        self.assertEqual(len(run(ltf=make_h1(extra=retest), SKIP_MITIGATED=False)), 1)
        # un retest limité à la dernière bougie clôturée reste un ordre légitime
        self.assertEqual(len(run(ltf=make_h1(extra=[(106.0, 109.2, 106.0, 108.5)]))), 1)

    def test_expiration_fin_de_c3(self):
        r = run()[0]
        self.assertEqual(r["expires"], E0 + 2 * 86400)
        self.assertEqual(r["max_age"], 2 * 86400 - 12 * H)
        self.assertEqual(run(now=E0 + 3 * 86400), [])

    def test_poi_htf(self):
        d1 = make_d1(fvg=True)
        self.assertIsNotNone(bot.htf_poi("D1", {"D1": d1}, "SELL", 110.4, 6.0))
        self.assertIsNone(bot.htf_poi("D1", {"D1": d1}, "SELL", 125.0, 6.0))
        self.assertEqual(run(REQUIRE_HTF_POI=True), [])   # le D1 de base n'a pas de FVG

    def test_premium_discount(self):
        m = make_h1()[:-1]
        rng = {"high": 120, "low": 100, "epoch": 0}   # milieu : 110
        orig = bot.entry_zone
        try:
            def build(entry, ext):
                bot.entry_zone = lambda *a, **k: ("Order Block", entry, (entry, entry + 1))
                sw = {"dir": "SELL", "i": 66, "sl": ext, "range": rng, "closed": m, "fvg": None}
                return bot.build_trade(SYM, sw, {"j": 67, "m": m}, entry - 1, None)
            saved = bot.REQUIRE_PREMIUM_DISCOUNT
            bot.REQUIRE_PREMIUM_DISCOUNT = True
            self.assertIsNone(build(108.0, 108.6))        # vente sous le milieu (discount) : refusée
            self.assertIsNotNone(build(115.0, 116.6))     # vente au-dessus du milieu (premium) : acceptée
            bot.REQUIRE_PREMIUM_DISCOUNT = False
            self.assertIsNotNone(build(108.0, 108.6))     # sans le filtre, la même vente passe
        finally:
            bot.entry_zone, bot.REQUIRE_PREMIUM_DISCOUNT = orig, saved


def make_d1_strict(c2=(107, 111, 105, 108)):
    """C1 = range 100-110, C2 = bougie clôturée qui balaie le haut (111) et clôture à 108, C3 = bougie en formation."""
    cs, p = [], 100.0
    for k in range(40, 2, -1):
        cs.append(C(E0 - k * 86400, p, p + 3, p - 3, p + 0.5))
        p += 0.3
    cs.append(C(E0 - 2 * 86400, 104, 110, 100, 107))
    cs.append(C(E0 - 86400, *c2))
    cs.append(C(E0, 108, 108.4, 107.6, 108))
    return cs


def calm_h1(extra=(), base=108.0):
    """H1 calme autour de `base` pendant C3 : l'OTE de C2 (entrée 109,23) n'est jamais touchée."""
    row = (base, base + 0.4, base - 0.4, base)
    cs = [C(E0 - k * H, *row) for k in range(60, 0, -1)]
    cs += [C(E0 + i * H, *row) for i in range(8)]
    cs += [C(E0 + (8 + k) * H, *r) for k, r in enumerate(extra)]
    cs.append(C(cs[-1]["epoch"] + H, base, base, base, base))
    return cs


def run_strict(entry, d1=None, ltf=None, now=E0 + 10 * H, **flags):
    saved = {k: getattr(bot, k) for k in flags}
    for k, v in flags.items():
        setattr(bot, k, v)
    try:
        return bot.analyze_strict(SYM, {"D1": d1 or make_d1_strict()}, {"H1": ltf or calm_h1()}, now=now, entry=entry)
    finally:
        for k, v in saved.items():
            setattr(bot, k, v)


class CrtStrict(unittest.TestCase):
    def test_ote_de_c2(self):
        r = run_strict("ote")
        self.assertEqual(len(r), 1)
        s, t = r[0], r[0]["trade"]
        self.assertEqual((s["dir"], s["mode"], s["ref_n"]), ("SELL", "ote", "D1"))
        self.assertAlmostEqual(t["entry"], 105 + 0.705 * 6, places=6)     # OTE du retracement de C2 (105 -> 111)
        self.assertEqual(t["otype"], "SELL LIMIT")
        self.assertTrue(t["ote"])
        self.assertGreater(t["sl"], 111)                                  # SL au-delà de l'extrême de C2
        self.assertEqual((t["tp1"], t["tp2"]), (105.0, 100))              # milieu et bord opposé de C1
        self.assertGreaterEqual(t["rr"], 3)
        self.assertEqual(s["expires"], E0 + 86400)                        # fin de C3
        self.assertTrue(bot.setup_keys(s)[0].endswith("_SELL_ote"))

    def test_cinquante_pour_cent_refuse_si_ratio_insuffisant(self):
        ltf = calm_h1(base=106.0)
        self.assertEqual(run_strict("mid", ltf=ltf), [])            # entrée à 108 : R:R de 2,4 < 3
        r = run_strict("mid", ltf=ltf, MIN_RISK_REWARD=2.0)
        self.assertEqual(len(r), 1)
        self.assertAlmostEqual(r[0]["trade"]["entry"], 108.0)
        self.assertFalse(r[0]["trade"]["ote"])

    def test_c2_non_cloturee_dans_le_range(self):
        self.assertEqual(run_strict("ote", d1=make_d1_strict(c2=(107, 111, 105, 110.6))), [])   # clôture au-dessus de C1 : cassure
        self.assertEqual(run_strict("ote", d1=make_d1_strict(c2=(107, 109.5, 105, 108))), [])   # aucun sweep

    def test_c2_balaie_les_deux_cotes(self):
        self.assertEqual(run_strict("ote", d1=make_d1_strict(c2=(107, 111, 99, 108))), [])

    def test_sweep_de_c2_trop_profond(self):
        self.assertEqual(run_strict("ote", d1=make_d1_strict(c2=(107, 116.5, 105, 108))), [])

    def test_entree_hors_du_range_de_c1(self):
        # C2 entièrement au-dessus du milieu : l'OTE tomberait au-dessus du haut de C1 (110)
        self.assertEqual(run_strict("ote", d1=make_d1_strict(c2=(109.5, 112, 109.2, 109.8))), [])

    def test_c3_a_depasse_lextreme_de_c2(self):
        self.assertEqual(run_strict("ote", ltf=calm_h1(extra=[(108, 111.5, 107.9, 108)])), [])

    def test_ordre_deja_rempli_pendant_c3(self):
        touched = calm_h1(extra=[(108, 109.5, 107.9, 108), (108, 108.4, 107.6, 108)])
        self.assertEqual(run_strict("ote", ltf=touched), [])
        self.assertEqual(len(run_strict("ote", ltf=touched, SKIP_MITIGATED=False)), 1)

    def test_tp1_deja_atteint_pendant_c3(self):
        self.assertEqual(run_strict("ote", ltf=calm_h1(extra=[(108, 108.4, 104.8, 105), (105, 106, 104, 106)])), [])

    def test_expiration_fin_de_c3(self):
        self.assertEqual(run_strict("ote", now=E0 + 86400 + 60), [])

    def test_ote_avec_confirmation(self):
        r = run_strict("ote_confirm", ltf=make_h1())
        self.assertEqual(len(r), 1)
        s = r[0]
        self.assertEqual((s["dir"], s["mode"]), ("SELL", "ote_confirm"))
        zone = s["ote_zone"]
        self.assertAlmostEqual(zone[0], 105 + 0.62 * 6, places=6)
        self.assertAlmostEqual(zone[1], 105 + 0.79 * 6, places=6)
        self.assertTrue(s["fvg"] or s["ob"])
        # tant que l'OTE de C2 n'est pas touchée, aucun setup
        self.assertEqual(run_strict("ote_confirm", ltf=calm_h1()), [])

    def test_variantes_et_cles_distinctes(self):
        d1, ltf = make_d1_strict(), calm_h1()
        k_ote = bot.setup_keys(run_strict("ote", d1, ltf)[0])[0]
        k_conf = bot.setup_keys(run_strict("ote_confirm", d1, make_h1())[0])[0]
        self.assertNotEqual(k_ote, k_conf)
        loose = run()[0]
        self.assertIsNone(bot.legacy_key(run_strict("ote", d1, ltf)[0], k_ote))
        self.assertEqual(bot.legacy_key(loose, bot.setup_keys(loose)[0]), bot.setup_keys(loose)[0].rsplit("_", 1)[0])

    def test_dispatcher(self):
        saved = bot.CRT_MODE, bot.STRICT_ENTRY
        try:
            bot.CRT_MODE, bot.STRICT_ENTRY = "strict", "ote"
            self.assertEqual(bot.current_variant(), "ote")
            self.assertEqual(len(bot.analyze_variant(SYM, {"D1": make_d1_strict()}, {"H1": calm_h1()}, now=E0 + 10 * H)), 1)
            bot.CRT_MODE = "loose"
            self.assertEqual(bot.current_variant(), "loose")
        finally:
            bot.CRT_MODE, bot.STRICT_ENTRY = saved

    def test_message_strict(self):
        s = run_strict("ote")[0]
        txt = bot.crt_message(s, s["price"], bot.decimals(s["ltf"]))
        self.assertIn("CRT strict · OTE de C2", txt)
        self.assertIn("C2 clôturée dans le range de C1", txt)
        self.assertIn("OTE de C2", txt)

    def test_semaine_w1(self):
        # semaines synthétiques : C1 = semaine du 14/09, C2 = semaine du 21/09 (sweep du haut), C3 = semaine du 28/09
        days = []
        start = calendar.timegm((2026, 8, 3, 0, 0, 0))   # lundi
        for k in range(0, 60):
            days.append(C(start + k * 86400, 100, 103, 97, 100))
        lun = {c["epoch"]: i for i, c in enumerate(days)}
        c1_start = calendar.timegm((2026, 9, 14, 0, 0, 0))
        for k in range(7):
            days[lun[c1_start + k * 86400]] = C(c1_start + k * 86400, 100, 108 if k == 2 else 104, 96 if k == 4 else 99, 100)
        c2_start = c1_start + 7 * 86400
        for k in range(7):
            days[lun[c2_start + k * 86400]] = C(c2_start + k * 86400, 100, 110 if k == 3 else 104, 99, 101)
        end = c2_start + 14 * 86400
        days = [c for c in days if c["epoch"] < end]
        sc = bot.strict_candles("W1", {"D1": days})
        self.assertIsNotNone(sc)
        c1, c2, c3_start, c3 = sc
        self.assertEqual((c1["high"], c1["low"]), (108, 96))
        self.assertEqual((c2["high"], c2["low"]), (110, 99))
        self.assertEqual(c3_start, c2["epoch"] + 86400)


class Filtres(unittest.TestCase):
    def test_news_nfp_et_crypto(self):
        nfp = bot.nfp_events(E0)[1][0]   # 2 octobre 2026, 8h30 New York
        self.assertEqual(calendar.timegm((2026, 10, 2, 12, 30, 0)), nfp)
        self.assertTrue(bot.news_blackout("frxEURUSD", nfp + 600))
        self.assertTrue(bot.news_blackout("cryBTCUSD", nfp + 600))
        self.assertFalse(bot.news_blackout("frxEURUSD", nfp + 2 * H))
        self.assertFalse(bot.news_blackout("frxEURJPY", nfp + 600))   # pas de devise USD
        self.assertEqual(bot.news_upcoming("frxEURUSD", nfp - 1800, 60), nfp)
        self.assertIsNone(bot.news_upcoming("frxEURUSD", nfp + 60, 60))

    def test_rollover(self):
        ts = calendar.timegm((2026, 10, 6, 21, 0, 0))   # 17h00 New York (EDT)
        self.assertTrue(bot.rollover_blackout("frxEURUSD", ts))
        self.assertFalse(bot.rollover_blackout("cryBTCUSD", ts))
        self.assertFalse(bot.rollover_blackout("frxEURUSD", ts - 5 * H))

    def test_risk_gate(self):
        loss = {"result": "SL", "resolved_epoch": NOW, "win_tp": "tp2", "tp1_hit": False, "risk_reward": 3.0, "rr_tp1": 1.5}
        self.assertIsNone(bot.risk_gate({"pending": {}, "history": []}, NOW))
        self.assertEqual(bot.risk_gate({"pending": {str(i): {} for i in range(6)}, "history": []}, NOW)[0], "cap")
        self.assertEqual(bot.risk_gate({"pending": {}, "history": [dict(loss) for _ in range(3)]}, NOW)[0], "day")
        old = [dict(loss, resolved_epoch=NOW - 86400) for _ in range(6)]   # lundi : pas de perte « du jour » mais -6 R sur la semaine
        self.assertEqual(bot.risk_gate({"pending": {}, "history": old}, NOW)[0], "week")

    def test_exposition_et_selection(self):
        self.assertEqual(bot.exposure_group("frxEURUSD", "BUY"), ("USD", -1))
        self.assertEqual(bot.exposure_group("frxUSDJPY", "BUY"), ("USD", 1))
        self.assertEqual(bot.exposure_group("cryBTCUSD", "SELL"), ("CRYPTO", -1))
        s1 = run()[0]
        s2 = dict(s1, ref_n="H4", trade=dict(s1["trade"], rr=9))
        self.assertEqual([x["ref_n"] for x in bot.select_best([s2, s1])], ["D1"])

    def test_taille_de_position(self):
        bot.ACCOUNT_BALANCE = 10000
        try:
            self.assertIn("0.20 lot", bot.size_line("frxEURUSD", 1.0850, 1.0900))
            self.assertIn("0.30 lot", bot.size_line("frxUSDJPY", 150.0, 150.5))
        finally:
            bot.ACCOUNT_BALANCE = 0.0

    def test_semaine_ny(self):
        dimanche_17h_ny = calendar.timegm((2026, 10, 4, 21, 0, 0))
        self.assertNotEqual(bot.week_key(dimanche_17h_ny, False), bot.week_key(dimanche_17h_ny, True))


class Suivi(unittest.TestCase):
    @staticmethod
    def trade():
        st = {"sym": SYM, "dir": "BUY", "ref_n": "D1", "conf_n": "H1", "counter": False,
              "trade": {"otype": "BUY", "entry": 100.0, "sl": 98.0, "tp1": 103.0, "tp2": 106.0, "rr": 3.0, "ote": False}}
        return tracker.new_trade(st, 0)

    @staticmethod
    def candles(*rows):
        return [C(i * H, *r) for i, r in enumerate(rows, start=1)]

    def resolve(self, *rows):
        t = self.trade()
        t["result"] = tracker.resolve_trade(t, self.candles(*rows))
        return t

    def test_tp2_direct(self):
        t = self.resolve((100, 101, 99.5, 100.5), (100.5, 103.5, 100.2, 103), (103, 106.5, 102, 106))
        self.assertEqual((t["result"], t["be_hit"]), ("TP", False))
        self.assertAlmostEqual(tracker.managed_r(t), 2.25)

    def test_tp1_retour_be_puis_tp2(self):
        t = self.resolve((100, 101, 99.5, 100.5), (100.5, 103.5, 100.2, 103), (103, 104, 99.9, 100.2), (100.2, 106.5, 100, 106))
        self.assertEqual((t["result"], t["be_hit"]), ("TP", True))
        self.assertAlmostEqual(tracker.managed_r(t), 0.75)

    def test_tp1_puis_sl(self):
        t = self.resolve((100, 101, 99.5, 100.5), (100.5, 103.5, 100.2, 103), (103, 104, 97.5, 98))
        self.assertAlmostEqual(tracker.managed_r(t), 0.75)

    def test_trade_ancien_sans_be_hit(self):
        t = self.trade()
        del t["be_hit"], t["rr_tp1"]   # trade enregistré avant l'ajout du break-even
        t["result"] = tracker.resolve_trade(t, self.candles((100, 101, 99.5, 100.5), (100.5, 103.5, 100.2, 103), (103, 106.5, 102, 106)))
        self.assertEqual(t["result"], "TP")
        self.assertAlmostEqual(t["rr_tp1"], 1.5)
        self.assertAlmostEqual(tracker.managed_r(t), 2.25)

    def test_sl_direct(self):
        self.assertAlmostEqual(tracker.managed_r(self.resolve((100, 101, 97.5, 98))), -1.0)


class BacktestVariantes(unittest.TestCase):
    def test_comparaison_des_variantes(self):
        import backtest
        def trade(mode, result, filled=True):
            st = {"sym": SYM, "dir": "SELL", "ref_n": "D1", "conf_n": "H1", "counter": False, "mode": mode,
                  "trade": {"otype": "SELL LIMIT", "entry": 100.0, "sl": 102.0, "tp1": 97.0, "tp2": 94.0, "rr": 3.0, "ote": True}}
            t = tracker.new_trade(st, E0)
            t["result"], t["filled"], t["tp1_hit"] = result, filled, result == "TP"
            return t
        by = {"loose": [trade("loose", "TP"), trade("loose", "SL")],
              "ote": [trade("ote", "TP"), trade("ote", "EXPIRE", filled=False), trade("ote", "EN_ATTENTE", filled=False)]}
        self.assertEqual(backtest.fill_stats(by["ote"]), (1, 2))      # l'ordre en attente n'est pas décidé
        txt = backtest.compare_variants(by)
        self.assertIn("COMPARAISON DES VARIANTES CRT", txt)
        self.assertIn("loose", txt)
        self.assertIn("50 %", txt)                                   # remplissage de la variante ote : 1 rempli sur 2 décidés
        self.assertIn("R net/alerte", txt)
        self.assertIn("Taux de remplissage", backtest.summary(backtest.STRATEGIES["crt"], by["ote"], 30))


class Main(unittest.TestCase):
    def test_main_avec_api_simulee(self):
        import random, time
        random.seed(3)

        class Fake:
            def candles(self, sym, gran, count=200, end=None):
                last = int(time.time()); last -= last % gran
                p, out = 100.0 + random.random() * 10, []
                for k in range(count - 1, -1, -1):
                    c = p + random.gauss(0, 0.6)
                    out.append({"epoch": last - k * gran, "open": p, "high": max(p, c) + abs(random.gauss(0, .3)),
                                "low": min(p, c) - abs(random.gauss(0, .3)), "close": c})
                    p = c
                return out

            def request(self, *a, **k):
                return {}

            def close(self):
                pass

        saved = (bot.Deriv, bot.notify, os.getcwd())
        bot.Deriv, bot.notify = Fake, lambda *a, **k: True
        try:
            with tempfile.TemporaryDirectory() as d:
                os.chdir(d)
                for ny in (False, True):
                    bot.NY_ALIGNED = ny
                    bot.main()
        finally:
            bot.Deriv, bot.notify = saved[0], saved[1]
            bot.NY_ALIGNED = False
            os.chdir(saved[2])


if __name__ == "__main__":
    unittest.main()
