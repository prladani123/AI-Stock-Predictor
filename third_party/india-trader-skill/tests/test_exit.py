"""bharat-exit tests. Bars for the ATR stop reuse tests/fixtures/bharat-entry/
(RELIANCE's real Oct 19 2026 five-minute bars, see test_entry.py)."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal

from _load import load, FIX, ROOT
import example_values as EX

exitp = load("bharat-exit", "exit")
entry = load("bharat-entry", "entry")

REL = os.path.join(FIX, "bharat-entry", "RELIANCE-2026-10-19-5m.json")

# trader/plan.json for the worked example, exactly as the contract writes it
PLAN = {"created_by": "bharat-exit", "symbol": "RELIANCE", "side": "buy", "date": "2026-10-19", "setup": "orb",
        "entry": 1227.8, "entry_time": "9:50 AM", "entry_bar": 6, "interval": "5m",
        "opening_range": {"high": 1223.4, "low": 1207.3, "minutes": 15},
        "stop": 1215.35, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
        "target": 1240.25, "target_r": 1.0, "target_label": "1R (one times the risk)",
        "risk_per_share": 12.45, "reward_risk": 1.0, "flat_by": "15:15"}
ENTRY = {"created_by": "bharat-entry", "symbol": "RELIANCE", "setup": "orb", "side": "buy", "date": "2026-10-19",
         "status": "triggered", "time": "9:50 AM", "bar_start": "09:45", "bar_index": 6, "price": 1227.8,
         "level": 1223.4, "interval": "5m", "opening_range": {"high": 1223.4, "low": 1207.3, "minutes": 15},
         "source": "yahoo", "checked_at": "2026-10-19T04:20:00Z"}


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


def c(entry_px, stop, target="2R", oh=1223.40, ol=1207.30, atr=None, n=None):
    sr, sp = exitp.parse_stop(stop)
    tr, tp = exitp.parse_target(target)
    return exitp.compute(entry_px, sr, sp, tr, tp, oh, ol, atr, n)


class Math(unittest.TestCase):
    def test_the_example_or_mid_1r(self):
        p = c(1227.80, "or-mid", "1R")
        self.assertEqual(p["stop"], Decimal("1215.35"))          # (1223.40 + 1207.30) / 2, on the tick already
        self.assertEqual(p["risk"], Decimal("12.45"))
        self.assertEqual(p["target"], Decimal("1240.25"))
        self.assertEqual(p["reward_risk"], Decimal("1.00"))
        self.assertEqual(p["target_label"], "1R (one times the risk)")
        self.assertNotIn("rounded down", p["stop_detail"])
        self.assertEqual((float(p["stop"]), float(p["target"])), (EX.STOP, EX.TARGET))

    def test_the_default_2r(self):
        p = c(1227.80, "or-mid")
        self.assertEqual(p["target"], Decimal("1252.70"))
        self.assertEqual(p["target_label"], "2R (two times the risk)")

    def test_raw_yahoo_floats_give_the_same_prices(self):
        p = c(1227.800048828125, "or-mid", "1R", 1223.4000244140625, 1207.300048828125)
        self.assertEqual((p["entry"], p["stop"], p["target"]),
                         (Decimal("1227.80"), Decimal("1215.35"), Decimal("1240.25")))

    def test_other_stops_round_to_the_tick(self):
        p = c(1227.80, "or-low")
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("1207.30"), Decimal("20.50"), Decimal("1268.80")))
        p = c(1227.80, "price:1215.33")
        self.assertEqual(p["stop"], Decimal("1215.30"))              # down to the tick, away from the entry
        self.assertIn("rounded down to the tick", p["stop_detail"])
        p = c(1227.80, "pct:1")                                      # 1215.522 -> 1215.50
        self.assertEqual((p["stop"], p["risk"], p["target"]), (Decimal("1215.50"), Decimal("12.30"), Decimal("1252.40")))
        p = c(1227.80, "atr:1.5", atr=7.4572, n=7)
        self.assertEqual(p["stop"], Decimal("1216.60"))              # 1227.80 - 11.1858 = 1216.6142
        self.assertEqual(p["stop_label"], "1.5 x ATR(7) under the entry")

    def test_targets_round_up_to_the_tick_and_price_targets(self):
        p = c(100, "price:99.05", "1.5R", None, None)
        self.assertEqual(p["target"], Decimal("101.45"))             # 100 + 1.425 -> up to 101.45
        self.assertEqual(p["target_label"], "1.5R (1.5 times the risk)")
        p = c(1227.80, "or-mid", "price:1245.02")
        self.assertEqual(p["target"], Decimal("1245.00"))            # your price, nearest tick
        self.assertEqual(p["reward_risk"], Decimal("1.38"))
        self.assertEqual(p["target_label"], "your price (1.38R)")
        p = c(1227.80, "price:Rs 1215.35", "1R")
        self.assertEqual(p["stop"], Decimal("1215.35"))

    def test_refusals_say_why(self):
        cases = [("price:1227.80", "2R", "at or above the entry"),
                 ("price:1230", "2R", "at or above the entry"),
                 ("or-mid", "price:1227", "at or below the entry"),
                 ("price:1227.35", "2R", "too tight to survive the spread"),
                 ("pct:0.04", "2R", "too tight to survive the spread")]
        for stop, target, why in cases:
            with self.assertRaises(exitp.PlanError) as cm:
                c(1227.80, stop, target)
            self.assertIn(why, str(cm.exception), stop)
        # 0.05% of 1227.80 is 0.6139: a Rs 0.65 risk is allowed, Rs 0.60 is not
        self.assertEqual(c(1227.80, "price:1227.15")["risk"], Decimal("0.65"))
        with self.assertRaises(exitp.PlanError):
            c(1227.80, "price:1227.20")
        with self.assertRaises(exitp.PlanError):
            c(1215.00, "or-mid")                                     # the midpoint is above this entry
        for bad in ("mid-ish", "pct:100", "price:0", "atr:-1"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_stop(bad)
        for bad in ("0R", "R", "lots", "price:x"):
            with self.assertRaises(exitp.PlanError):
                exitp.parse_target(bad)
        with self.assertRaises(exitp.PlanError):
            c(1227.80, "or-mid", oh=None, ol=None)
        with self.assertRaises(exitp.PlanError):
            c(1227.80, "or-mid", oh=1207.30, ol=1223.40)

    def test_atr_on_the_session_bars(self):
        ch = exitp.load_chart("RELIANCE", "5m", None, REL)
        day = exitp.datetime.date(2026, 10, 19)
        bars = exitp.session_bars(ch, day, "5m", upto_slot=6)
        self.assertEqual(len(bars), 7)                              # 9:15 ... the 9:45 entry bar
        trs, prev = [], None
        for b in bars:
            r = b["h"] - b["l"]
            if prev is not None:
                r = max(r, abs(b["h"] - prev), abs(b["l"] - prev))
            trs.append(r)
            prev = b["c"]
        self.assertAlmostEqual(trs[0], 1223.40 - 1211.90, places=3)
        atr, n = exitp.atr_of(bars)
        self.assertEqual(n, 7)
        self.assertAlmostEqual(atr, sum(trs) / 7, places=9)
        self.assertAlmostEqual(atr, 7.4571, places=3)                # 52.2 / 7 by hand
        all_bars = exitp.session_bars(ch, day, "5m")
        self.assertEqual(len(all_bars), 18)                          # to 10:40; the 10:45 bar and the tick never count
        self.assertEqual(exitp.atr_of(all_bars)[1], 14)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("trader")

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def write(self, path, doc):
        with open(path, "w") as fh:
            json.dump(doc, fh)

    def plan(self, *args):
        return run(exitp, ["plan", "--now", "2026-10-19 09:51"] + list(args))

    def read_plan(self):
        with open(os.path.join("trader", "plan.json")) as fh:
            return json.load(fh)

    def test_entry_to_plan_end_to_end(self):
        code, _, _ = run(entry, ["check", "RELIANCE", "--offline", REL, "--now", "2026-10-19 09:50", "--write"])
        self.assertEqual(code, 0)
        code, text, err = self.plan("--stop", "or-mid", "--target", "1R", "--write")
        self.assertEqual(code, 0, err)
        p = self.read_plan()
        for k, v in PLAN.items():
            self.assertEqual(p[k], v, k)
        self.assertIn("Stop          Rs 1,215.35  opening-range midpoint", text)
        self.assertIn("Target        Rs 1,240.25  1R (one times the risk)", text)
        self.assertIn("Risk a share  Rs 12.45 (1.01% of the entry)", text)
        self.assertIn("Flat by       15:15 IST", text)
        self.assertIn("not a guarantee", text)
        self.assertIn("/bharat-timemachine", text)
        self.assertNotIn("pro" + "fit", text.lower())
        code, text, _ = self.plan("--write")                       # the defaults: or-mid, 2R
        self.assertEqual((self.read_plan()["target"], self.read_plan()["target_r"]), (1252.7, 2.0))
        code, text, _ = run(exitp, ["show", "--now", "2026-10-19 09:55"])
        self.assertEqual(code, 0)
        self.assertIn("Rs 1,252.70", text)
        self.assertNotIn("not today", text)
        code, text, _ = run(exitp, ["show", "--now", "2026-10-21 09:55"])
        self.assertIn("not today", text)

    def test_manual_flags_and_json(self):
        code, text, _ = self.plan("--symbol", "reliance", "--entry", "1227.80", "--or-high", "1223.40", "--or-low",
                                  "1207.30", "--time", "9:50", "--entry-bar", "6", "--date", "2026-10-19",
                                  "--target", "1R", "--json")
        self.assertEqual(code, 0)
        p = json.loads(text)
        for k, v in PLAN.items():
            self.assertEqual(p[k], v, k)
        self.assertEqual(p["warnings"], [])

    def test_atr_stop_reads_bars(self):
        self.write(os.path.join("trader", "entry.json"), ENTRY)
        code, text, err = self.plan("--stop", "atr:1.5", "--offline", REL, "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["stop"], p["risk_per_share"], p["target"]), (1216.6, 11.2, 1250.2))
        self.assertEqual((p["stop_rule"], p["atr_bars"]), ("atr", 7))
        code, _, err = self.plan("--stop", "atr:1.5", "--offline", REL, "--entry-bar", "1")
        self.assertEqual(code, 1)
        self.assertIn("at least 3 closed", err)

    def test_refusals_from_the_cli(self):
        self.write(os.path.join("trader", "entry.json"), dict(ENTRY, status="waiting", time=None, price=None,
                                                              bar_index=None, bar_start=None))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("RELIANCE is waiting, not triggered", err)
        code, text, _ = self.plan("--entry", "1223.45", "--json")    # plan ahead at the level
        self.assertEqual(code, 0)
        p = json.loads(text)
        self.assertEqual((p["entry"], p["stop"], p["entry_time"]), (1223.45, 1215.35, None))

        self.write(os.path.join("trader", "entry.json"), dict(ENTRY, setup="level", level=1220.0))
        code, _, err = self.plan()
        self.assertIn("level setup has no opening-range stop", err)
        self.assertEqual(self.plan("--stop", "pct:1")[0], 0)

        code, _, err = self.plan("--side", "sell")
        self.assertIn("P1 plans longs only", err)
        self.write(os.path.join("trader", "entry.json"), dict(ENTRY, side="sell"))
        code, _, err = self.plan()
        self.assertIn("longs only", err)
        os.remove(os.path.join("trader", "entry.json"))
        code, _, err = self.plan()
        self.assertIn("no entry", err)
        code, _, err = self.plan("--symbol", "RELIANCE")
        self.assertIn("needs --entry", err)
        code, _, err = self.plan("--symbol", "RELIANCE", "--entry", "1227.80", "--or-high", "1223.40")
        self.assertIn("both --or-high and --or-low", err)
        self.assertNotIn("Traceback", err)

    def test_flat_by_config_square_off_and_late_entries(self):
        self.write(os.path.join("trader", "entry.json"), ENTRY)
        self.write(os.path.join("trader", "config.json"), {"flat_by": "15:10", "min_reward_risk": 1.5})
        code, text, _ = self.plan("--target", "1R", "--json")
        p = json.loads(text)
        self.assertEqual(p["flat_by"], "15:10")
        self.assertIn("under your min_reward_risk 1.5", p["warnings"][0])
        os.remove(os.path.join("trader", "config.json"))

        code, text, _ = self.plan("--flat-by", "15:25")
        self.assertEqual(code, 0)
        self.assertIn("after the broker's 15:20 MIS auto square-off", text)
        code, _, err = self.plan("--flat-by", "15:30")
        self.assertEqual(code, 1)
        self.assertIn("at or after the 3:30 PM close", err)
        self.write(os.path.join("trader", "entry.json"), dict(ENTRY, time="3:16 PM"))
        code, _, err = self.plan()
        self.assertEqual(code, 1)
        self.assertIn("no time left to hold it", err)

    def test_show_without_a_plan(self):
        code, text, _ = run(exitp, ["show"])
        self.assertEqual(code, 1)
        self.assertIn("No plan", text)


P2 = os.path.join(FIX, "p2")


class Futures(unittest.TestCase):
    setUp = Cli.setUp
    tearDown = Cli.tearDown
    read_plan = Cli.read_plan

    def entry_fut(self, *extra):
        code, text, err = run(entry, ["check", "NIFTY", "--instrument", "fut", "--now", "2026-10-21 09:35",
                                      "--offline", os.path.join(P2, "yahoo-NSEI-5m.json"),
                                      "--bhavcopy", os.path.join(P2, "fo-bhavcopy-20261019.csv.zip"), "--write"]
                              + list(extra))
        self.assertEqual(code, 0, err)

    def test_the_example_in_futures_points(self):
        self.entry_fut()
        code, text, err = run(exitp, ["plan", "--now", "2026-10-21 09:36", "--stop", "or-mid", "--target", "1R",
                                      "--write"])
        self.assertEqual(code, 0, err)
        p = self.read_plan()
        self.assertEqual(p["instrument"], "fut")
        self.assertEqual((p["entry"], p["stop"], p["target"]), (EX.FUT_PRICE, EX.FUT_STOP, EX.FUT_TARGET))
        self.assertEqual((p["contract"], p["lot"], p["expiry"]), (EX.FUT_CONTRACT, EX.FUT_LOT, EX.FUT_EXPIRY))
        self.assertEqual(p["index"], {"entry": EX.FUT_TRIGGER_CLOSE, "stop": EX.FUT_INDEX_STOP,
                                      "target": 23197.25})
        self.assertEqual(p["risk_per_unit"], EX.FUT_RISK_PER_UNIT)
        self.assertEqual(p["risk_per_unit"], p["risk_per_share"])
        self.assertAlmostEqual(p["risk_per_unit"] * p["lot"], EX.FUT_RISK_PER_LOT, places=2)
        self.assertEqual(p["risk_per_lot"], EX.FUT_RISK_PER_LOT)
        self.assertEqual((p["basis_date"], p["basis_estimated"], p["fut_price_source"]),
                         (EX.FUT_BASIS_DATE, False, "bhavcopy"))
        self.assertIn(EX.FUT_CONTRACT, text)
        self.assertIn("Risk a lot", text)

    def test_a_given_futures_price_keeps_the_points(self):
        self.entry_fut("--fut-price", "23050.00")
        code, _, err = run(exitp, ["plan", "--now", "2026-10-21 09:36", "--stop", "or-mid", "--target", "1R",
                                   "--write"])
        self.assertEqual(code, 0, err)
        p = self.read_plan()
        self.assertEqual((p["entry"], p["stop"], p["target"]), (23050.00, 22968.35, 23131.65))
        self.assertEqual(p["fut_price_source"], "user")

    def test_entry_flag_is_the_futures_fill(self):
        self.entry_fut()
        code, _, err = run(exitp, ["plan", "--now", "2026-10-21 09:36", "--stop", "or-mid", "--target", "1R",
                                   "--entry", "23050", "--write"])
        self.assertEqual(code, 0, err)
        p = self.read_plan()
        self.assertEqual((p["entry"], p["stop"], p["target"]), (23050.00, 22968.35, 23131.65))
        self.assertEqual((p["index"]["entry"], p["fut_price_source"]), (EX.FUT_TRIGGER_CLOSE, "user"))

    def test_a_level_plan_still_needs_a_named_stop(self):
        self.entry_fut()
        with open(os.path.join("trader", "entry.json")) as fh:
            e = json.load(fh)
        e["setup"] = "level"
        with open(os.path.join("trader", "entry.json"), "w") as fh:
            json.dump(e, fh)
        code, _, err = run(exitp, ["plan", "--now", "2026-10-21 09:36"])
        self.assertEqual(code, 1)
        self.assertIn("give --stop", err)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "bharat-exit", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "bharat-risk", "SKILL.md"), encoding="utf-8") as fh:
            risk = fh.read()
        self.assertTrue(text.startswith("---\nname: bharat-exit\n"))
        self.assertGreater(len(text.splitlines()), 60)
        self.assertEqual(text[text.index("## Safety"):], risk[risk.index("## Safety"):])
        low = " ".join(text.lower().split())
        for must in ("before the entry", "a stop is a plan, not a guarantee", "most retail intraday traders lose money",
                     "/bharat-timemachine", "/bharat-budget", "longs only", "rs 1,215.35", "rs 1,240.25",
                     "rs 12.45", "1,252.70", "too tight to survive the spread", "15:15", "rs 0.05"):
            self.assertIn(must, low)
        for never in (chr(0x2014), "tick " + "by tick", "real " + "time", "pro" + "fit", "new york", "nyse"):
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()


def _opt_premium(right, index, o, at, mode):
    import datetime as _dt
    years = exitp.india.years_to_expiry(at, _dt.date.fromisoformat(o["expiry"]))
    p = exitp.india.option_price(right, index, o["forward_ratio"], o["strike"], years, o["iv"], o["rate"])
    return exitp.india.tick_round(p, mode=mode)


class Options(unittest.TestCase):
    setUp = Cli.setUp
    tearDown = Cli.tearDown
    read_plan = Cli.read_plan

    def entry_opt(self, right="CE", now="2026-10-21 09:35", bhav="fo-bhavcopy-20261019.csv.zip", *extra):
        code, text, err = run(entry, ["check", "NIFTY", "--instrument", "opt", "--right", right, "--now", now,
                                      "--offline", os.path.join(P2, "yahoo-NSEI-5m.json"),
                                      "--bhavcopy", os.path.join(P2, bhav), "--write"] + list(extra))
        self.assertEqual(code, 0, text + err)
        with open(os.path.join("trader", "entry.json")) as fh:
            return json.load(fh)

    def plan(self, *extra, now="2026-10-21 09:36"):
        code, text, err = run(exitp, ["plan", "--now", now, "--stop", "or-mid", "--target", "1R", "--write"]
                              + list(extra))
        self.assertEqual(code, 0, text + err)
        return self.read_plan(), text

    def test_the_example_premiums_at_the_index_levels(self):
        import datetime as _dt
        e = self.entry_opt()
        p, text = self.plan()
        at = _dt.datetime(2026, 10, 21, 9, 35, tzinfo=exitp.india.IST)
        self.assertEqual(p["instrument"], "opt")
        self.assertEqual((p["index_entry"], p["index_stop"], p["index_target"]),
                         (EX.FUT_TRIGGER_CLOSE, EX.FUT_INDEX_STOP, 23197.25))
        self.assertEqual(p["entry"], EX.OPT_PREMIUM)
        self.assertEqual(p["stop"], _opt_premium("CE", EX.FUT_INDEX_STOP, e["option"], at, "down"))
        self.assertEqual(p["target"], _opt_premium("CE", 23197.25, e["option"], at, "up"))
        self.assertEqual((p["stop"], p["target"], p["risk_per_unit"], p["risk_per_lot"]),
                         (EX.OPT_STOP, EX.OPT_TARGET, EX.OPT_RISK_PER_UNIT, EX.OPT_RISK_PER_LOT))
        self.assertIn("on the index", p["target_label"])
        self.assertAlmostEqual(p["risk_per_unit"], p["entry"] - p["stop"], places=2)
        self.assertAlmostEqual(p["risk_per_lot"], p["risk_per_unit"] * EX.OPT_LOT, places=2)
        self.assertEqual((p["contract"], p["strike"], p["right"], p["lot"], p["expiry"]),
                         (EX.OPT_CONTRACT, EX.OPT_STRIKE, "CE", EX.OPT_LOT, EX.OPT_EXPIRY))
        self.assertAlmostEqual(p["iv"], EX.OPT_IV, places=4)
        self.assertEqual(p["premium_source"], "estimated")
        self.assertIn("index", text)
        self.assertIn("held constant", text)

    def test_a_put_stops_above_the_index_and_targets_below(self):
        self.entry_opt("PE", "2026-10-19 12:05", "fo-bhavcopy-20261016.csv.zip")
        p, _ = self.plan(now="2026-10-19 12:06")
        self.assertEqual(p["index_side"], "down")
        self.assertEqual(p["index_stop"], 23073.00)        # (23175.10 + 22970.85) / 2, rounded up for a put
        self.assertGreater(p["index_stop"], p["index_entry"])
        self.assertLess(p["index_target"], p["index_entry"])
        self.assertLess(p["stop"], p["entry"])
        self.assertGreater(p["target"], p["entry"])

    def test_the_entry_flag_is_the_premium_fill(self):
        self.entry_opt()
        p, _ = self.plan("--entry", "170")
        self.assertEqual((p["entry"], p["premium_source"]), (170.0, "user"))
        self.assertGreater(p["iv"], EX.OPT_IV)            # a dearer fill is a higher implied volatility

    def test_a_cheap_far_strike_clamps_the_stop_at_one_tick(self):
        import datetime as _dt
        at = _dt.datetime(2026, 10, 21, 9, 35, tzinfo=exitp.india.IST)
        o = {"contract": "NIFTY26O2223600CE", "expiry": "2026-10-22", "strike": 23600.0, "right": "CE", "lot": 65,
             "iv": 0.12, "forward_ratio": 1.0, "rate": 0.06, "premium_source": "estimated"}
        o["premium"] = 0.30                                # a fill above one tick
        raw = exitp.india.option_price("CE", 23034.05, 1.0, 23600, exitp.india.years_to_expiry(at, _dt.date(2026, 10, 22)), 0.12)
        self.assertLess(raw, 0.05)                         # the case: the model prices the stop under a tick
        c = {"entry": exitp.D("23115.65"), "stop": exitp.D("23034.05"), "target": exitp.D("23278.85")}
        lv, notes = exitp.option_levels(c, o, at, _dt.datetime(2026, 10, 21, 15, 15, tzinfo=exitp.india.IST))
        self.assertEqual(lv["stop"], 0.05)
        self.assertIn("one tick", " ".join(notes))

    def test_a_target_theta_eats_by_flat_by_is_refused(self):
        import datetime as _dt
        at = _dt.datetime(2026, 10, 21, 14, 0, tzinfo=exitp.india.IST)
        o = {"contract": "NIFTY26O2124000CE", "expiry": "2026-10-21", "strike": 24000.0, "right": "CE", "lot": 65,
             "iv": 0.12, "forward_ratio": 1.0, "rate": 0.06, "premium": 0.40, "premium_source": "user"}
        c = {"entry": exitp.D("23115.65"), "stop": exitp.D("23034.05"), "target": exitp.D("23197.25")}
        with self.assertRaises(exitp.PlanError) as cm:
            exitp.option_levels(c, o, at, _dt.datetime(2026, 10, 21, 15, 15, tzinfo=exitp.india.IST))
        self.assertIn("cannot reach", str(cm.exception))


class ReviewFixesP3(unittest.TestCase):
    setUp = Cli.setUp
    tearDown = Cli.tearDown
    read_plan = Cli.read_plan

    def test_a_premium_read_later_solves_its_iv_at_the_index_then(self):
        # 10:00, the index at 23180 and the call at 198.87 (the 12.5 % model's price there): the IV must stay
        # about 12.5 %, so the premium stop still sits at the index stop
        Options.entry_opt(self)
        code, text, err = run(exitp, ["plan", "--now", "2026-10-21 10:00", "--stop", "or-mid", "--target", "1R",
                                      "--entry", "198.87", "--premium-index", "23180", "--write"])
        self.assertEqual(code, 0, text + err)
        p = self.read_plan()
        self.assertAlmostEqual(p["iv"], EX.OPT_IV, delta=0.002)
        self.assertEqual(p["index_stop"], EX.FUT_INDEX_STOP)
        self.assertLess(abs(p["stop"] - EX.OPT_STOP), 2.0)
