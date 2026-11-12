import gzip
import hashlib
import io
import json
import os
import random
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX
import example_values as EX

tm = load("bharat-timemachine", "timemachine")

DIR = os.path.join(FIX, "bharat-timemachine")
REL = os.path.join(DIR, "RELIANCE-60d5m.json.gz")   # Yahoo v8 chart, range=60d interval=5m, fetched 2026-11-04
with gzip.open(REL, "rt", encoding="utf-8") as _fh:
    TEXT = _fh.read()
NOW = "2026-10-19 09:51"
NOW_UTC = tm.parse_now(NOW)

# the worked example's plan, as bharat-exit writes it
PLAN = {"created_by": "bharat-exit", "symbol": "RELIANCE", "side": "buy", "date": "2026-10-19", "setup": "orb",
        "entry": 1227.8, "entry_time": "9:50 AM", "entry_bar": 6, "interval": "5m",
        "opening_range": {"high": 1223.4, "low": 1207.3, "minutes": 15},
        "stop": 1215.35, "stop_rule": "or_mid", "stop_label": "opening-range midpoint",
        "target": 1240.25, "target_r": 1.0, "target_label": "1R (one times the risk)",
        "risk_per_share": 12.45, "reward_risk": 1.0, "flat_by": "15:15"}

# regression fingerprints of the seed-19 run (the algorithm itself is proven bit for bit against the original
# sim.py in the US pack; these pin that nothing here drifts)
BANDS = "d59cd3ebd63876a1"
PATHS = "d4780cbecd857084"
TRADES = "d4e0a717c3d92a5d"
HIST = {"lo": 1207.24, "hi": 1250.28,
        "bins": [110, 101, 183, 237, 336, 411, 485, 595, 714, 737, 729, 811, 732, 714, 646, 544, 474, 335, 316, 222,
                 155, 138, 66, 53]}


def sha(x):
    return hashlib.sha256(json.dumps(x).encode("utf-8")).hexdigest()[:16]


def plan(**kw):
    raw = dict(PLAN)
    raw.update(kw)
    return tm.normalize_plan(raw)


def machine(p=None, text=TEXT, now=NOW_UTC, **kw):
    return tm.time_machine(text, p or plan(), now, **kw)


def edited(fn):
    doc = json.loads(TEXT)
    fn(doc["chart"]["result"][0])
    return json.dumps(doc)


def keep_bars(result, keep):
    q = result["indicators"]["quote"][0]
    idx = [i for i, ts in enumerate(result["timestamp"]) if keep(ts)]
    result["timestamp"] = [result["timestamp"][i] for i in idx]
    for k in q:
        q[k] = [q[k][i] for i in idx]


def ist(ts):
    return tm.to_local(tm.utc_of(ts))


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tm.main(argv)
    return code, out.getvalue(), err.getvalue()


class TheExample(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = machine()

    def test_the_odds(self):
        m = self.doc["mc"]
        self.assertEqual((m["replays"], m["seed"], m["window"], m["until"]), (10000, 19, 2, "15:15"))
        self.assertEqual((m["pool_sessions"], m["bars_left"]), (48, 65))
        self.assertEqual((m["pool_from"], m["pool_to"]), ("2026-08-07", "2026-10-16"))
        self.assertEqual((m["p_target"], m["p_stop"], m["p_neither"], m["p_green"]), (0.1702, 0.1767, 0.6531, 0.493))
        self.assertEqual((m["median_close"], m["p5_close"], m["p95_close"]), (1227.66, 1213.03, 1243.22))
        self.assertEqual((m["avg_R"], m["r_at_target"]), (-0.008, 1.0))
        self.assertEqual((m["p_target"], m["p_stop"], m["p_neither"]), (EX.TM_TARGET, EX.TM_STOP, EX.TM_NEITHER))

    def test_fingerprints(self):
        m = self.doc["mc"]
        self.assertEqual(sha(m["bands"]), BANDS)
        self.assertEqual(len(m["sample_paths"]), 160)
        self.assertEqual(sha(m["sample_paths"]), PATHS)
        self.assertEqual(m["hist"], HIST)

    def test_the_history_replay(self):
        h = self.doc["history"]
        self.assertEqual((h["sessions"], h["triggered"]), (48, 22))
        self.assertEqual((h["target"], h["stop"], h["close"], h["green"]), (10, 10, 2, 10))
        self.assertEqual((h["sessions"], h["triggered"], h["target"], h["stop"], h["close"]),
                         (EX.TM_SESSIONS, EX.TM_FIRED, EX.TM_H_TARGET, EX.TM_H_STOP, EX.TM_H_NEITHER))
        self.assertEqual(h["avg_R"], -0.027)
        self.assertEqual(h["rule"], {"setup": "orb", "or_minutes": 15, "stop_rule": "or_mid", "target_r": 1.0})
        slim = [{"date": t["date"], "entry": t["entry"], "outcome": t["outcome"], "R": t["R"]} for t in h["trades"]]
        self.assertEqual(sha(slim), TRADES)
        # by hand: Oct 1, range 1177.90 to 1181.90, the 9:40 bar closed 1182.00; stop the midpoint 1179.90,
        # target 1R 1184.10; the 10:10 bar's low 1179.30 hit the stop before any high reached 1184.10
        oct1 = [t for t in h["trades"] if t["date"] == "2026-10-01"][0]
        self.assertEqual((oct1["time"], oct1["entry"], oct1["stop"], oct1["target"], oct1["outcome"]),
                         ("9:45 AM", 1182.0, 1179.9, 1184.1, "stop"))

    def test_the_verdict(self):
        self.assertEqual(self.doc["verdict"], "Close to a coin flip: target first 17.0 % vs stop first 17.7 %.")
        self.assertEqual(self.doc["verdict_notes"],
                         ["65.3 % of the replays hit neither by 3:15 PM; the price at 3:15 PM decides those."])

    def test_bands_start_at_the_entry(self):
        m = self.doc["mc"]
        self.assertEqual(m["bands"][0], [1227.8] * 5)
        self.assertEqual((m["band_times"][0], m["band_times"][-1], len(m["band_times"])), ("09:50", "15:15", 66))
        self.assertTrue(all(p[0] == 1227.8 and len(p) == 66 for p in m["sample_paths"]))

    def test_the_text_report(self):
        text = "\n".join(tm.report(self.doc))
        for line in ("RELIANCE buy, Monday 2026-10-19: entry 1227.80 at 9:50 AM (bar 6), stop 1215.35, target 1240.25",
                     "10,000 replays", "9:50 AM to 3:15 PM (65 five-minute bars)", "48 sessions",
                     "target first   17.0 %", "stop first     17.7 %", "neither        65.3 %   still open at 3:15 PM",
                     "fired 22 times: 10 target, 10 stop, 2 neither. Average R -0.027",
                     "Left out 1 session(s) with a missing bar before the replay's end: 2026-10-15",
                     "not a prediction", "R leaves out costs", "--seed 19"):
            self.assertIn(line, text)
        for word in ("profit", "P&L", "guarantee", "real time", "tick by tick", "ET", "New York"):
            self.assertNotIn(word, text)


P2 = os.path.join(FIX, "p2")
with open(os.path.join(P2, "yahoo-NSEI-5m.json"), encoding="utf-8") as _fh:
    NSEI = _fh.read()
R = EX.FUT_BASIS
FUT_PLAN = {"created_by": "bharat-exit", "symbol": "NIFTY", "side": "buy", "date": EX.FUT_DATE, "setup": "orb",
            "entry_time": EX.FUT_TRIGGER_AT, "entry_bar": 3, "interval": "5m",
            "opening_range": {"high": EX.FUT_OR_HIGH, "low": EX.FUT_OR_LOW, "minutes": 15},
            "stop_rule": "or_mid", "target_r": 1.0, "flat_by": "15:15", "instrument": "fut",
            "contract": EX.FUT_CONTRACT, "lot": EX.FUT_LOT, "expiry": EX.FUT_EXPIRY, "basis_ratio": R,
            "basis_date": EX.FUT_BASIS_DATE, "basis_estimated": False,
            "index": {"entry": EX.FUT_TRIGGER_CLOSE, "stop": EX.FUT_INDEX_STOP, "target": 23197.25},
            "entry": EX.FUT_PRICE, "stop": EX.FUT_STOP, "target": EX.FUT_TARGET,
            "risk_per_share": EX.FUT_RISK_PER_UNIT, "risk_per_unit": EX.FUT_RISK_PER_UNIT}
NOW_FUT = tm.parse_now("2026-10-21 09:36")


class Futures(unittest.TestCase):
    def test_the_odds_are_the_index_odds(self):
        fut = tm.time_machine(NSEI, tm.normalize_plan(FUT_PLAN), NOW_FUT)
        ix_raw = {k: v for k, v in FUT_PLAN.items() if k not in ("instrument", "index")}
        ix_raw.update(FUT_PLAN["index"])
        ix = tm.time_machine(NSEI, tm.normalize_plan(ix_raw), NOW_FUT)
        for k in ("p_target", "p_stop", "p_neither", "avg_R"):
            self.assertEqual(fut["mc"][k], ix["mc"][k], k)
        self.assertEqual(fut["fut"]["median_close"], round(ix["mc"]["median_close"] * R, 2))
        self.assertEqual(fut["plan"]["entry"], EX.FUT_TRIGGER_CLOSE)  # replayed on the index
        text = "\n".join(tm.report(fut))
        self.assertIn("futures price = index x %.6f from %s" % (R, EX.FUT_BASIS_DATE), text)
        self.assertIn(EX.FUT_CONTRACT, text)

    def test_without_index_levels_it_divides_by_the_basis(self):
        raw = {k: v for k, v in FUT_PLAN.items() if k != "index"}
        p = tm.normalize_plan(raw)
        self.assertAlmostEqual(p["stop"], EX.FUT_STOP / R, places=6)
        self.assertEqual(p["fut"]["stop"], EX.FUT_STOP)


class Seeds(unittest.TestCase):
    def test_same_seed_same_numbers(self):
        self.assertEqual(machine(replays=2000, seed=5)["mc"], machine(replays=2000, seed=5)["mc"])

    def test_another_seed_gives_other_numbers(self):
        m = machine(seed=7)["mc"]
        self.assertEqual((m["p_target"], m["p_stop"]), (0.1703, 0.1781))
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertNotEqual(sha(m["bands"]), BANDS)

    def test_the_draws_are_stable_across_pythons(self):
        r = random.Random(19)
        self.assertEqual([r.randint(-2, 2) for _ in range(8)], [-2, 2, -2, 2, -1, 1, 0, 2])
        self.assertEqual(r.choice(range(59)), 18)

    def test_window_and_replays(self):
        a = machine(replays=500, window=0)["mc"]
        self.assertEqual((a["replays"], a["window"]), (500, 0))
        self.assertNotEqual(a["bands"], machine(replays=500)["mc"]["bands"])
        with self.assertRaises(tm.TMError):
            machine(replays=0)
        with self.assertRaises(tm.TMError):
            machine(window=11)


class Bars(unittest.TestCase):
    def setUp(self):
        self.result = tm.chart_result(TEXT, "RELIANCE")

    def test_the_clock_and_missing_closes(self):
        meta, bars, dropped = tm.parse_bars(self.result, NOW_UTC)
        self.assertEqual(dropped["no_price"], 112)         # Yahoo's NSE data: no 15:20 close, often no 15:25
        last = bars[-1]
        self.assertEqual((last["date"], last["start"], last["slot"]), ("2026-10-19", "09:45", 6))
        entry = [b for b in bars if b["date"] == "2026-10-19" and b["slot"] == 6][0]
        self.assertEqual(entry["c"], 1227.800048828125)
        # with the real clock the data's own time (Nov 3, 15:29:39) marks the 15:25 print as not complete
        self.assertEqual(tm.parse_bars(self.result, tm.parse_now(None))[1][-1]["date"], "2026-11-03")

    def test_now_pins_the_clock(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(now=tm.parse_now("2026-10-19 09:47"))
        self.assertIn("not in the data", str(cm.exception))
        self.assertIn("the data stops at the 09:40 bar", str(cm.exception))

    def test_ist_slots(self):
        res = {"meta": {"symbol": "X.NS"}, "timestamp": [1792381500, 1792380600, 1792403100],   # 9:15, 9:00, 15:15
               "indicators": {"quote": [{"open": [1] * 3, "high": [1] * 3, "low": [1] * 3, "close": [1] * 3,
                                         "volume": [0] * 3}]}}
        meta, bars, dropped = tm.parse_bars(res, tm.parse_now("2026-10-20T00:00Z"))
        self.assertEqual([(b["start"], b["slot"]) for b in bars], [("09:15", 0), ("15:15", 72)])
        self.assertEqual(dropped["outside_hours"], 1)

    def test_a_session_with_a_gap_is_left_out(self):
        def hole(res):
            res["indicators"]["quote"][0]["close"][5] = None
        doc = machine(text=edited(hole), replays=200)
        self.assertEqual(doc["mc"]["pool_sessions"], 47)
        self.assertEqual(doc["data"]["short_sessions_left_out"][0], {"date": "2026-08-07", "bars": 71})

    def test_today_is_not_in_the_pool(self):
        doc = machine(replays=200)
        self.assertEqual(doc["mc"]["pool_to"], "2026-10-16")
        self.assertNotIn("2026-10-19", [t["date"] for t in doc["history"]["trades"]])

    def test_empty_and_broken_data(self):
        empty = json.dumps({"chart": {"result": [{"meta": {"symbol": "RELIANCE.NS"}, "timestamp": [],
                                                  "indicators": {"quote": [{}]}}], "error": None}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=empty)
        self.assertIn("no complete 5-minute bars", str(cm.exception))
        gone = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                               "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(tm.TMError) as cm:
            machine(text=gone)
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(tm.TMError):
            machine(text="<html>rate limited</html>")

    def test_fewer_than_20_sessions_is_refused(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(text=edited(lambda r: keep_bars(r, lambda ts: ist(ts).date().isoformat() >= "2026-09-18")))
        self.assertIn("only 19 session(s)", str(cm.exception))
        self.assertIn("at least 20", str(cm.exception))
        self.assertEqual(machine(text=edited(lambda r: keep_bars(r, lambda ts: ist(ts).date().isoformat()
                                                                 >= "2026-09-17")), replays=200)["mc"]["pool_sessions"], 20)

    def test_past_the_flat_by_says_why(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(until="15:25", replays=10)
        self.assertIn("Yahoo's NSE data has no 15:20 bar: use --until 15:15 or earlier", str(cm.exception))

    def test_the_data_must_be_the_plans_symbol(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(symbol="TCS"))
        self.assertIn("data is for RELIANCE.NS", str(cm.exception))


class Plans(unittest.TestCase):
    def test_an_entry_not_in_the_data_is_refused(self):
        with self.assertRaises(tm.TMError) as cm:
            machine(plan(date="2026-10-21"))
        self.assertIn("no bars for that day", str(cm.exception))

    def test_plan_checks(self):
        cases = [
            (dict(entry_time="9:52 AM", entry_bar=None), "5-minute grid"),
            (dict(entry_time="3:30 PM", entry_bar=None), "between 9:20 AM and 3:25 PM"),
            (dict(entry_bar=7), "disagrees with itself"),
            (dict(entry_time=None, entry_bar=None), "entry_time"),
            (dict(interval="1m"), "7 days of 1-min bars"),
            (dict(stop=1230.0), "stop < entry < target"),
            (dict(side="short"), "buy or sell"),
            (dict(symbol=""), "NSE symbol"),
            (dict(entry="abc"), "number for entry"),
            (dict(date="Oct 19"), "date like"),
            (dict(date="2026-11-08"), "Muhurat"),
            (dict(opening_range={"minutes": 7}), "multiple of 5"),
        ]
        for change, words in cases:
            with self.assertRaises(tm.TMError, msg=change) as cm:
                plan(**change)
            self.assertIn(words, str(cm.exception), change)
        self.assertEqual(plan(symbol="M&M")["symbol"], "M&M")

    def test_entry_bar_or_entry_time_alone(self):
        self.assertEqual(plan(entry_time=None)["entry_time"], "9:50 AM")
        self.assertEqual(plan(entry_bar=None)["entry_bar"], 6)
        self.assertEqual(plan(entry_time="9:50", entry_bar=None)["entry_bar"], 6)

    def test_until_cuts_the_replay(self):
        doc = machine(until="15:00", replays=500)
        m = doc["mc"]
        self.assertEqual((m["until"], m["bars_left"], m["band_times"][-1]), ("15:00", 62, "15:00"))
        self.assertIn("still open at 3:00 PM", "\n".join(tm.report(doc)))
        self.assertEqual(machine(until="3:00 PM", replays=500)["mc"], m)
        self.assertEqual(machine(plan(flat_by="15:00"), replays=500)["mc"]["until"], "15:00")   # the plan's flat-by
        for bad, words in (("9:50 AM", "nothing to replay"), ("15:35", "no later than 3:30 PM"),
                           ("15:02", "5-minute grid"), ("noon", "IST time")):
            with self.assertRaises(tm.TMError) as cm:
                machine(until=bad, replays=10)
            self.assertIn(words, str(cm.exception), bad)

    def test_an_entry_outside_its_bar_is_flagged(self):
        doc = machine(plan(entry=1235.0, stop=1230.0, target=1240.0), replays=200)
        self.assertIn("outside that bar's range 1220.60 to 1228.00", doc["warnings"][0])

    def test_stop_rounds_away_from_the_entry_to_the_tick(self):
        self.assertEqual(tm.tick_away(1215.37, True), 1215.35)
        self.assertEqual(tm.tick_away(1215.37, False), 1215.40)
        self.assertEqual(tm.tick_away(1207.300048828125, True), 1207.30)
        self.assertEqual(tm.tick_away(1207.2999999999, True), 1207.30)
        self.assertEqual(tm.tick_toward_target(1240.21, True), 1240.25)

    def test_the_stop_rule_is_worked_out_when_not_given(self):
        self.assertEqual(machine(plan(stop_rule=None), replays=100)["history"]["rule"]["stop_rule"], "or_mid")
        rule = machine(plan(stop_rule=None, stop=1207.3, target=1248.3), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["target_r"]), ("or_low", 1.0))
        rule = machine(plan(stop_rule="atr", stop=1216.6, target=1250.2), replays=100)["history"]["rule"]
        self.assertEqual((rule["stop_rule"], rule["stop_pct"], rule["target_r"]), ("pct", 0.9122, 2.0))

    def test_a_setup_other_than_orb_skips_the_history(self):
        doc = machine(plan(setup="flag"), replays=200)
        self.assertIsNone(doc["history"]["rule"])
        self.assertIn("History: skipped", "\n".join(tm.report(doc)))

    def test_a_sell_plan_mirrors_the_rules(self):
        doc = machine(plan(side="sell", stop=1240.25, target=1215.35, stop_rule="or_high"), replays=2000)
        m = doc["mc"]
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=6)
        self.assertEqual(doc["history"]["rule"]["stop_rule"], "or_high")
        for t in doc["history"]["trades"]:
            self.assertTrue(t["target"] < t["entry"] < t["stop"], t)

    def test_the_verdict_thresholds_at_1r(self):
        self.assertTrue(tm.verdict({"p_target": 0.30, "p_stop": 0.26}).startswith("Close to a coin flip"))
        self.assertTrue(tm.verdict({"p_target": 0.40, "p_stop": 0.30}).startswith("Leans toward the target"))
        self.assertTrue(tm.verdict({"p_target": 0.20, "p_stop": 0.30}).startswith("Leans toward the stop"))
        self.assertTrue(tm.verdict({"p_target": 0.60, "p_stop": 0.30}).startswith("Strongly toward the target"))
        self.assertEqual(tm.verdict({"p_target": 0.0, "p_stop": 0.0, "until": "15:15"}),
                         "No replay reached the target or the stop by 3:15 PM: the price at 3:15 PM decides every one.")

    def test_a_2r_target_is_judged_against_its_break_even(self):
        mc = {"p_target": 0.241, "p_stop": 0.505, "p_neither": 0.254, "r_at_target": 2.0, "avg_R": 0.095}
        self.assertEqual(tm.verdict(mc), "Close to break-even: at 2R the target has to come first in 33 % of the "
                                         "replays that hit one side; it did in 32 % (target first 24.1 % vs stop "
                                         "first 50.5 %).")
        lean = dict(mc, p_target=0.15, p_stop=0.55)
        self.assertEqual(tm.verdict_notes(lean), ["Average R is +0.095 all the same: the replays that hit neither "
                                                  "pull it the other way."])

    def test_2r_on_the_fixture(self):
        doc = machine(plan(target=1252.7))
        m = doc["mc"]
        self.assertEqual((m["r_at_target"], m["p_target"], m["p_stop"], m["p_neither"], m["avg_R"]),
                         (2.0, 0.0085, 0.1767, 0.8148, -0.005))
        self.assertTrue(doc["verdict"].startswith("Strongly toward the stop: at 2R"))
        self.assertEqual(doc["verdict_notes"],
                         ["81.5 % of the replays hit neither by 3:15 PM; the price at 3:15 PM decides those."])


class Network(unittest.TestCase):
    def setUp(self):
        self.calls, self.sleeps = [], []
        self._get, self._sleep = tm._get, tm._sleep
        tm._sleep = self.sleeps.append

    def tearDown(self):
        tm._get, tm._sleep = self._get, self._sleep

    def answer(self, codes):
        codes = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url.split(".finance")[0].split("//")[1])
            code = codes.pop(0)
            if code != 200:
                raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b""))
            return TEXT
        tm._get = fake

    def test_url_and_user_agent(self):
        self.assertEqual(tm.chart_url("reliance"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=60d&interval=5m")
        self.assertIn("/chart/M%26M.NS?", tm.chart_url("m&m", "query1"))
        self.assertIn("/chart/^NSEI?", tm.chart_url("NIFTY"))
        self.assertEqual(tm.UA, "Mozilla/5.0 (compatible; india-trader-skill/0.1)")

    def test_429_waits_retries_then_falls_back_to_query1(self):
        self.answer([429, 429, 200])
        self.assertEqual(tm.fetch_chart("RELIANCE"), TEXT)
        self.assertEqual(self.calls, ["query2", "query2", "query1"])
        self.assertEqual(self.sleeps, [tm.BACKOFF, tm.PAUSE])

    def test_rate_limited_everywhere_is_one_plain_line(self):
        self.answer([429, 429, 429, 429])
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_chart("RELIANCE")
        self.assertIn("rate limiting", str(cm.exception))

    def test_404(self):
        self.answer([404])
        with self.assertRaises(tm.TMError) as cm:
            tm.fetch_chart("ZZZZ")
        self.assertIn("check the symbol", str(cm.exception))


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.plan = os.path.join(self.d, "trader", "plan.json")
        os.makedirs(os.path.dirname(self.plan))
        with open(self.plan, "w") as fh:
            json.dump(PLAN, fh, indent=2)
        self.out = os.path.join(self.d, "trader", "timemachine.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_run_with_the_plan_end_to_end(self):
        code, text, err = run(["run", "--plan", self.plan, "--offline", REL, "--now", NOW, "--write", self.out])
        self.assertEqual(code, 0, err)
        self.assertIn("Close to a coin flip: target first 17.0 % vs stop first 17.7 %.", text)
        self.assertIn("/bharat-budget", text)
        with open(self.out) as fh:
            doc = json.load(fh)
        self.assertEqual((doc["created_by"], doc["source"]), ("bharat-timemachine", "offline"))
        self.assertEqual(sha(doc["mc"]["sample_paths"]), PATHS)
        self.assertEqual(doc["data"]["last_bar"], "2026-10-19 09:45 IST")

    def test_the_plan_as_flags_matches_the_file(self):
        code, text, err = run(["run", "--symbol", "reliance", "--date", "2026-10-19", "--entry", "1227.80",
                               "--stop", "1215.35", "--target", "1240.25", "--entry-time", "9:50 AM",
                               "--offline", REL, "--now", NOW, "--json"])
        self.assertEqual(code, 0, err)
        doc = json.loads(text)
        self.assertEqual((doc["mc"]["p_target"], doc["mc"]["until"]), (0.1702, "15:15"))
        self.assertEqual(doc["plan"]["stop_rule"], "or_mid")
        self.assertNotIn("sample_paths", doc["mc"])

    def test_offline_folder_and_chart(self):
        shutil.copy(REL, self.d)
        cwd = os.getcwd()
        os.chdir(self.d)
        try:
            code, text, err = run(["run", "--offline", ".", "--now", NOW, "--replays", "500", "--write"])
            self.assertEqual(code, 0, err)
            code, text, err = run(["chart", "--from"])
            self.assertEqual(code, 0, err)
            lines = text.splitlines()
            self.assertIn("replay paths from the 9:50 AM entry (seed 19)", lines[0])
            self.assertTrue(any(l.startswith("  1240.25 |") and l.endswith("  target") for l in lines))
            self.assertTrue(any(l.startswith("  1215.35 |") and l.endswith("  stop") for l in lines))
            self.assertIn("3:15", text)
        finally:
            os.chdir(cwd)

    def test_errors_are_one_plain_line(self):
        for argv in (["run", "--plan", os.path.join(self.d, "nope.json")],
                     ["run", "--plan", self.plan, "--offline", os.path.join(self.d, "missing.json.gz")],
                     ["run", "--plan", self.plan, "--offline", REL, "--until", "9:00"],
                     ["chart", "--from", os.path.join(self.d, "nope.json")]):
            code, text, err = run(argv)
            self.assertEqual(code, 1, argv)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertTrue(err.startswith("timemachine.py: "), err)


if __name__ == "__main__":
    unittest.main()


OPT_SEED19 = (0.3312, 0.4837, 0.1851, -0.097)   # target, stop, neither, avg R (premium R)
OPT_SEED19_MEDIAN = 149.99                       # the premium at 15:15


def _opt_plan():
    """The P3 example plan, made the real way: bharat-entry --instrument opt, then bharat-exit."""
    import shutil
    import tempfile
    from contextlib import redirect_stdout
    entry = load("bharat-entry", "entry")
    exitp = load("bharat-exit", "exit")
    d, cwd = tempfile.mkdtemp(), os.getcwd()
    os.chdir(d)
    try:
        with redirect_stdout(io.StringIO()):
            assert entry.main(["check", "NIFTY", "--instrument", "opt", "--right", "CE", "--now", "2026-10-21 09:35",
                               "--offline", os.path.join(P2, "yahoo-NSEI-5m.json"),
                               "--bhavcopy", os.path.join(P2, "fo-bhavcopy-20261019.csv.zip"), "--write"]) == 0
            assert exitp.main(["plan", "--now", "2026-10-21 09:36", "--stop", "or-mid", "--target", "1R",
                               "--write"]) == 0
        with open(os.path.join("trader", "plan.json")) as fh:
            return json.load(fh)
    finally:
        os.chdir(cwd)
        shutil.rmtree(d)


class Options(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = _opt_plan()
        cls.doc = tm.time_machine(NSEI, tm.normalize_plan(cls.raw), NOW_FUT)

    def test_it_replays_the_index_and_decides_on_the_premium(self):
        m, o = self.doc["mc"], self.doc["opt"]
        self.assertAlmostEqual(m["p_target"] + m["p_stop"] + m["p_neither"], 1.0, places=9)
        self.assertEqual(self.doc["plan"]["entry"], EX.FUT_TRIGGER_CLOSE)          # the index
        self.assertEqual((o["entry"], o["stop"], o["target"]), (EX.OPT_PREMIUM, EX.OPT_STOP, EX.OPT_TARGET))
        # the premium decides, so the odds are not the index plan's
        ix_raw = {k: v for k, v in self.raw.items() if k not in ("instrument",)}
        ix_raw.update(entry=self.raw["index_entry"], stop=self.raw["index_stop"], target=self.raw["index_target"])
        ix = tm.time_machine(NSEI, tm.normalize_plan(ix_raw), NOW_FUT)
        self.assertNotEqual((m["p_target"], m["p_stop"]), (ix["mc"]["p_target"], ix["mc"]["p_stop"]))
        self.assertLessEqual(o["p5_premium"], o["median_premium"])
        self.assertLessEqual(o["median_premium"], o["p95_premium"])

    def test_seed_19_numbers(self):
        m, o = self.doc["mc"], self.doc["opt"]
        self.assertEqual((m["p_target"], m["p_stop"], m["p_neither"], m["avg_R"]), OPT_SEED19)
        self.assertEqual(o["median_premium"], OPT_SEED19_MEDIAN)

    def test_a_higher_iv_stops_out_less(self):
        # the same premium levels, a dearer option at every index level: further from the stop
        raw = dict(self.raw, iv=self.raw["iv"] * 1.5)
        hi = tm.time_machine(NSEI, tm.normalize_plan(raw), NOW_FUT)
        self.assertLess(hi["mc"]["p_stop"], self.doc["mc"]["p_stop"])

    def test_the_report_names_the_iv_and_says_it_is_held_constant(self):
        text = "\n".join(tm.report(self.doc))
        self.assertIn(EX.OPT_CONTRACT, text)
        self.assertIn("implied volatility %.2f %%" % (100 * self.raw["iv"]), text)
        self.assertIn("held constant", text)
        self.assertIn("R = the 39.60 premium risk", text)

    def test_the_history_replay_prices_each_session_on_the_premium(self):
        h = self.doc["history"]
        self.assertGreater(h["triggered"], 0)
        t = h["trades"][0]
        self.assertTrue({"premium_entry", "premium_stop", "premium_target"} <= set(t), t)
        self.assertIn("plan's implied volatility", h["note"])
