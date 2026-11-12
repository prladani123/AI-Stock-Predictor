"""bharat-entry tests. Fixtures in tests/fixtures/bharat-entry/:

RELIANCE-2026-10-19-5m.json  RELIANCE's Mon Oct 19 2026 five-minute bars (Yahoo chart JSON v8),
                             cut like a range=1d answer read at 10:49:45 IST: it ends with the
                             10:45 bar and a live tick at 10:49:45. previousClose is Fri Oct 16's.
DEMO-1m-synthetic.json       SYNTHETIC one-minute bars (not market data) from 9:15 IST: a wick
                             above the range high, a close AT the high, a null bar, a live tick.
"""

import copy
import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT
import example_values as EX

entry = load("bharat-entry", "entry")

EDIR = os.path.join(FIX, "bharat-entry")
REL = os.path.join(EDIR, "RELIANCE-2026-10-19-5m.json")
DEMO = os.path.join(EDIR, "DEMO-1m-synthetic.json")
OPEN_TS = 1792381500       # 2026-10-19 09:15 IST
DT = datetime.datetime


def ist(text):
    return entry.parse_now(text)


def doc(path=REL):
    with open(path) as fh:
        return json.load(fh)


def chart(d=None, sym="RELIANCE"):
    c = entry.parse_chart(json.dumps(d if d is not None else doc()), sym)
    c["symbol"] = sym
    return c


def at(text, d=None, **kw):
    return entry.evaluate(chart(d), ist(text), **kw)


def shift_to(d, day, open_time=datetime.time(9, 15)):
    """The same bars moved to another date and open time."""
    d = copy.deepcopy(d)
    r = d["chart"]["result"][0]
    new_open = int((DT.combine(day, open_time) - DT(1970, 1, 1)).total_seconds()) - 19800
    delta = new_open - OPEN_TS
    r["timestamp"] = [t + delta for t in r["timestamp"]]
    r["meta"]["regularMarketTime"] += delta
    return d


def no_breakout(d=None):
    """Every bar after the range stays under the 1223.40 high."""
    d = copy.deepcopy(d if d is not None else doc())
    q = d["chart"]["result"][0]["indicators"]["quote"][0]
    for i in range(3, len(q["close"])):
        q["close"][i] = min(q["close"][i], 1223.00)
        q["high"][i] = min(q["high"][i], 1223.10)
        q["open"][i] = min(q["open"][i], 1223.00)
    return d


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = entry.main(argv)
    return code, out.getvalue(), err.getvalue()


class TheExample(unittest.TestCase):
    def test_triggers_at_950_on_the_945_bar(self):
        r = at("2026-10-19 09:50")
        self.assertEqual(r["status"], "triggered")
        self.assertEqual((r["opening_range"]["high"], r["opening_range"]["low"]), (EX.OR_HIGH, EX.OR_LOW))
        self.assertEqual(r["opening_range"]["bars"], 3)
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_start"], t["bar_index"], t["price"], t["pct_over"]),
                         (EX.TRIGGER_AT, EX.TRIGGER_BAR, 6, EX.TRIGGER_CLOSE, EX.PCT_OVER))
        self.assertEqual(t["close"], 1227.800048828125)
        self.assertEqual(r["level"], 1223.4)
        self.assertEqual(r["prev_close"], 1211.2)

    def test_at_947_the_945_bar_is_still_forming(self):
        r = at("2026-10-19 09:47")
        self.assertEqual(r["status"], "waiting")
        self.assertIsNone(r["trigger"])
        self.assertEqual((r["last"]["bar_start"], r["last"]["close"]), ("09:40", 1221.8))
        self.assertEqual(r["forming"]["bar_start"], "09:45")
        self.assertIn("Rs 1.60 under the range high Rs 1223.40", r["reason"])
        self.assertEqual(at("2026-10-19 09:49:59")["status"], "waiting")

    def test_before_the_open_and_while_the_range_forms(self):
        r = at("2026-10-19 09:05")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "pre-open"))
        self.assertIn("NSE opens at 9:15", r["reason"])
        r = at("2026-10-19 09:29")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "forming"))
        self.assertEqual(r["opening_range"]["bars"], 2)
        self.assertFalse(r["opening_range"]["complete"])
        r = at("2026-10-19 09:31")
        self.assertEqual((r["status"], r["phase"]), ("waiting", "open"))
        self.assertEqual(r["opening_range"]["bars"], 3)

    def test_the_live_tick_and_the_in_progress_bar_are_dropped(self):
        c = chart()
        self.assertEqual(c["bars"][-1]["t"], OPEN_TS + 94 * 60 + 45)     # 10:49:45 is in the file
        r = entry.evaluate(c, entry.utc_of(c["data_time"]))
        self.assertEqual(r["now"], "2026-10-19 10:49:45")
        self.assertEqual(r["_bars"][-1]["slot"], 17)                      # the 10:40 bar
        self.assertEqual(r["forming"]["bar_start"], "10:45")
        self.assertEqual(r["bars_closed"], 18)
        r = at("2026-10-19 10:50")
        self.assertEqual(r["_bars"][-1]["slot"], 17)
        self.assertIn("Yahoo's last trade is 10:49:45", r["lag_note"])
        r = at("2026-10-19 10:51")
        self.assertEqual((r["_bars"][-1]["slot"], r["bars_closed"]), (18, 19))
        self.assertTrue(all(b["t"] % 300 == 0 for b in r["_bars"]))

    def test_after_the_close_it_stays_triggered_and_says_it_is_late(self):
        r = at("2026-10-19 15:40")
        self.assertEqual((r["status"], r["phase"]), ("triggered", "after-close"))
        self.assertEqual(r["trigger"]["time"], "9:50 AM")
        text = "\n".join(entry.describe(r))
        self.assertIn("minutes ago", text)
        self.assertNotIn("This is the moment", text)


class Rules(unittest.TestCase):
    def test_range_volume_skips_the_zero_volume_915_bar(self):
        r = at("2026-10-19 09:50", min_volume_x=1)
        self.assertEqual(r["opening_range"]["avg_volume"], 294168)        # (230,154 + 358,182) / 2
        self.assertEqual(r["trigger"]["volume_x"], 1.52)                  # 448,544 / 294,168
        r = at("2026-10-19 09:52", min_volume_x=2)
        self.assertEqual(r["status"], "waiting")
        self.assertIn("1.52x the range's volume, under your 2.00x", r["reason"])

    def test_level_setup(self):
        r = at("2026-10-19 10:30", setup="level", level=1225)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"], r["trigger"]["price"]),
                         ("9:50 AM", 6, 1227.8))
        # a level under the open: the first eligible close is the 9:25 bar's, at 9:30 (no_entries_before)
        r = at("2026-10-19 10:30", setup="level", level=1100)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("9:30 AM", 2))
        with self.assertRaises(entry.EntryError):
            at("2026-10-19 10:30", setup="level")

    def test_no_breakout_waits_then_expires_at_the_cutoff(self):
        d = no_breakout()
        self.assertEqual(at("2026-10-19 10:49", d)["status"], "waiting")
        self.assertEqual(at("2026-10-19 14:44", d)["status"], "waiting")
        r = at("2026-10-19 14:45", d)
        self.assertEqual(r["status"], "expired")
        self.assertIn("by 14:45", r["reason"])
        self.assertEqual(at("2026-10-19 15:35", d)["phase"], "after-close")

    def test_config_cutoff(self):
        r = at("2026-10-19 09:50", cfg={"no_entries_before": "09:30", "no_entries_after": "09:45"})
        self.assertEqual(r["status"], "expired")
        r = at("2026-10-19 10:30", cfg={"no_entries_before": "09:55", "no_entries_after": "14:45"})
        self.assertEqual(r["trigger"]["time"], "9:55 AM")   # the 9:50 bar, first above to close at or after 9:55

    def test_band_warning(self):
        r = at("2026-10-19 09:50", band=1.5)    # prev close 1211.20: a 1.5 % band tops out at 1229.35
        self.assertEqual(r["near_band"], {"band": 1.5, "upper": 1229.35, "prev_close": 1211.2, "price": 1227.8})
        self.assertIn("within 1% of its 1.5% band limit Rs 1229.35", "\n".join(entry.describe(r)))
        self.assertIsNone(at("2026-10-19 09:50", band=20)["near_band"])
        self.assertIsNone(at("2026-10-19 09:50")["near_band"])

    def test_bad_inputs(self):
        with self.assertRaises(entry.EntryError):
            at("2026-10-19 10:15", or_minutes=7)
        with self.assertRaises(entry.EntryError):
            at("2026-10-19 10:15", interval="15m")
        with self.assertRaises(entry.EntryError):
            entry.parse_now("tomorrow")
        with self.assertRaises(entry.EntryError):
            entry.parse_hhmm("25:00", "x")
        self.assertEqual(entry.parse_now("2026-10-19 09:50"), DT(2026, 10, 19, 4, 20))
        self.assertEqual(entry.parse_now("2026-10-19T04:20:00Z"), DT(2026, 10, 19, 4, 20))
        self.assertEqual(entry.parse_now("2026-10-19 09:50 IST"), DT(2026, 10, 19, 4, 20))

    def test_or_minutes_30(self):
        r = at("2026-10-19 10:49", or_minutes=30)
        self.assertEqual((r["opening_range"]["bars"], r["opening_range"]["high"]), (6, 1223.4))
        self.assertEqual(r["trigger"]["time"], "9:50 AM")


class OneMinute(unittest.TestCase):
    def c(self):
        c = entry.parse_chart(json.dumps(doc(DEMO)), "DEMO")
        c["symbol"] = "DEMO"
        return c

    def test_wick_and_close_at_the_high_do_not_trigger(self):
        c = self.c()
        r = entry.evaluate(c, entry.utc_of(c["data_time"]), interval="1m")
        self.assertEqual(r["now"], "2026-10-19 09:51:30")
        self.assertEqual(r["opening_range"], {"high": 100.5, "low": 99.0, "minutes": 15, "bars": 15, "of": 15,
                                              "complete": True, "avg_volume": 10667})
        t = r["trigger"]
        self.assertEqual((t["time"], t["bar_start"], t["bar_index"], t["price"]), ("9:44 AM", "09:43", 28, 100.62))
        r = entry.evaluate(c, ist("2026-10-19 09:43:59"), interval="1m")
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(r["forming"]["bar_start"], "09:43")
        self.assertEqual(r["last"]["close"], 100.5)             # the 9:42 bar closed AT the high

    def test_null_bar_tick_and_volume(self):
        c = self.c()
        r = entry.evaluate(c, entry.utc_of(c["data_time"]), interval="1m", min_volume_x=2)
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("9:47 AM", 31))
        slots = [b["slot"] for b in r["_bars"]]
        self.assertNotIn(19, slots)                               # 9:34 came as nulls
        self.assertEqual(slots[-1], 35)                           # 9:51 still forming, the tick dropped
        self.assertEqual(r["forming"]["bar_start"], "09:51")

    def test_the_file_and_the_interval_must_agree(self):
        code, _, err = run(["check", "DEMO", "--offline", DEMO])
        self.assertEqual(code, 1)
        self.assertIn("1m bars, not 5m", err)


class Calendar(unittest.TestCase):
    def test_weekend_holiday_and_empty_data(self):
        r = at("2026-10-17 10:30")
        self.assertEqual((r["status"], r["phase"]), ("no-data", "closed"))
        self.assertEqual(r["next_open"], "2026-10-19 09:15")
        r = at("2026-10-20 10:30")
        self.assertIn("NSE holiday: Dussehra", r["reason"])
        self.assertEqual(r["next_open"], "2026-10-21 09:15")
        empty = copy.deepcopy(doc())
        res = empty["chart"]["result"][0]
        del res["timestamp"]
        res["indicators"] = {"quote": [{}]}
        r = at("2026-10-19 10:15", empty)
        self.assertEqual((r["status"], r["phase"]), ("no-data", "open"))
        r = at("2026-10-21 10:15")                                # Yahoo sent Monday's bars only
        self.assertEqual(r["status"], "no-data")
        r = at("2028-03-01 10:30")
        self.assertFalse(r["calendar_known"])
        self.assertIn("holiday list unknown for 2028", r["reason"])

    def test_no_known_last_session_is_a_plain_error(self):
        # Feb 2027: the 2027 holiday list is not in india.py yet, so no trading day is known in the last 30 days
        code, out, err = run(["check", "NIFTY", "--instrument", "fut", "--offline", NSEI, "--now", "2027-02-15 10:00"])
        self.assertEqual(code, 1, out + err)
        self.assertIn("holiday list for 2027", err)
        self.assertIn("--bhavcopy FILE", err)

    def test_muhurat_session(self):
        d = shift_to(doc(), datetime.date(2026, 11, 8), datetime.time(14, 20))
        cfg = {"no_entries_before": "14:35", "no_entries_after": "15:15"}
        r = entry.evaluate(chart(d), ist("2026-11-08 14:55"), cfg=cfg)
        self.assertEqual((r["opens"], r["closes"]), ("14:20", "15:30"))
        self.assertEqual((r["trigger"]["time"], r["trigger"]["bar_index"]), ("2:55 PM", 6))
        # with the default 14:45 cutoff, Muhurat's 14:55 trigger is too late
        self.assertEqual(entry.evaluate(chart(d), ist("2026-11-08 14:55"))["status"], "expired")

    def test_yahoo_errors(self):
        bad = json.dumps({"chart": {"result": None, "error": {"code": "Not Found",
                                                              "description": "No data found, symbol may be delisted"}}})
        with self.assertRaises(entry.EntryError) as cm:
            entry.parse_chart(bad, "ZZZZ")
        self.assertIn("No data found", str(cm.exception))
        with self.assertRaises(entry.EntryError):
            entry.parse_chart("<html>", "RELIANCE")


class Network(unittest.TestCase):
    def setUp(self):
        self.calls, self.sleeps = [], []
        self._open, self._sleep = entry._open_url, entry._sleep
        entry._sleep = lambda s: self.sleeps.append(s)
        with open(REL) as fh:
            self.body = fh.read()

    def tearDown(self):
        entry._open_url, entry._sleep = self._open, self._sleep

    def answers(self, *codes):
        seq = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url)
            code = seq.pop(0)
            if code == 200:
                return self.body
            raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b'{"chart":{"result":null,'
                                                                           b'"error":{"description":"No data found"}}}'))
        entry._open_url = fake

    def test_url_and_429_retry_once_after_a_pause(self):
        self.answers(429, 200)
        c = entry.get_chart("RELIANCE", "5m", datetime.date(2026, 10, 19), today=datetime.date(2026, 10, 19))
        self.assertEqual(c["symbol"], "RELIANCE")
        self.assertEqual(len(c["bars"]), 20)
        self.assertEqual(self.calls, ["https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d"
                                      "&interval=5m"] * 2)
        self.assertEqual(self.sleeps, [entry.BACKOFF])

    def test_5xx_falls_back_without_waiting(self):
        self.answers(503, 200)
        entry.get_chart("RELIANCE", "5m", datetime.date(2026, 10, 19), today=datetime.date(2026, 10, 19))
        self.assertEqual(self.sleeps, [])
        self.assertIn("query1", self.calls[1])

    def test_still_busy_is_one_plain_error(self):
        self.answers(429, 429, 429, 429)
        code, out, err = run(["check", "RELIANCE", "--now", "2026-10-19 09:50"])
        self.assertEqual(code, 1)
        self.assertIn("HTTP 429", err)
        self.assertNotIn("Traceback", err)

    def test_unknown_symbol_404(self):
        self.answers(404)
        with self.assertRaises(entry.EntryError) as cm:
            entry.get_chart("ZZZZ", "5m", datetime.date(2026, 10, 19), today=datetime.date(2026, 10, 19))
        self.assertIn("No data found", str(cm.exception))

    def test_ranges_and_symbols(self):
        today = datetime.date(2026, 10, 21)
        self.assertEqual(entry.chart_range(today, today, "5m"), "1d")
        self.assertEqual(entry.chart_range(datetime.date(2026, 10, 19), today, "5m"), "5d")
        self.assertIn("/chart/M%26M.NS?", entry.chart_path("m&m", "1d", "5m"))
        self.assertIn("/chart/^NSEI?", entry.chart_path("NIFTY", "1d", "5m"))


def live_provider(d, lag_until=None, calls=None):
    """Bars the way Yahoo serves them at `now`: closed bars final, the in-progress bar
    showing a spike to 1224.00 (above the high), plus a live tick."""
    base = chart(d)

    def provider(now):
        if calls is not None:
            calls.append(now)
        nts = entry.ts_of(now)
        data_ts = nts - 30 if (lag_until is not None and now < lag_until) else nts
        bars = []
        for b in base["bars"]:
            if b["t"] % 300 or b["t"] > data_ts:
                continue
            b = dict(b)
            if b["t"] + 300 > data_ts:
                b["c"], b["h"] = 1224.00, max(b["h"], 1224.00)
            bars.append(b)
        if data_ts % 300:
            bars.append({"t": data_ts, "o": 1224.0, "h": 1224.0, "l": 1224.0, "c": 1224.0, "v": 0})
        return {"symbol": "RELIANCE", "meta": {}, "bars": bars, "prev_close": 1211.2, "data_time": data_ts}
    return provider


class Watch(unittest.TestCase):
    OPTS = {"setup": "orb", "or_minutes": 15, "interval": "5m", "level": None, "min_volume_x": None, "cfg": None}

    def loop(self, provider, start, **kw):
        clock = entry.SimClock(ist(start))
        lines, fired = [], []
        code, res = entry.watch_loop(provider, clock.now, clock.sleep, lines.append, dict(self.OPTS),
                                     on_trigger=lambda r, now: fired.append((r, now)), **kw)
        return code, res, lines, fired, clock

    def test_fires_on_the_first_closed_bar_not_the_spike_in_progress(self):
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(doc(), calls=calls), "2026-10-19 09:42", every=60)
        self.assertEqual(code, 0)
        self.assertEqual(len(fired), 1)
        self.assertEqual((fired[0][0]["trigger"]["time"], fired[0][0]["trigger"]["price"]), ("9:50 AM", 1227.8))
        self.assertEqual([entry.to_local(c).strftime("%H:%M:%S") for c in calls], ["09:42:00", "09:45:02", "09:50:02"])
        text = "\n".join(lines)
        self.assertIn(" 9:40 bar  closed Rs 1221.80  Rs 1.60 under Rs 1223.40", text)
        self.assertIn(" 9:45 bar  closed Rs 1227.80  TRIGGERED", text)
        self.assertEqual(text.count("TRIGGERED"), 1)

    def test_waits_for_yahoo_to_catch_up(self):
        calls = []
        code, res, lines, fired, clock = self.loop(live_provider(doc(), lag_until=ist("2026-10-19 09:50:30"),
                                                                 calls=calls), "2026-10-19 09:47", every=30)
        self.assertEqual(code, 0)
        self.assertEqual([entry.to_local(c).strftime("%H:%M:%S") for c in calls], ["09:47:00", "09:50:02", "09:50:32"])
        self.assertEqual(fired[0][0]["trigger"]["time"], "9:50 AM")

    def test_from_before_the_open_prints_the_range_bar_by_bar(self):
        code, res, lines, fired, clock = self.loop(live_provider(doc()), "2026-10-19 09:00", every=30)
        self.assertEqual(code, 0)
        text = "\n".join(lines)
        self.assertIn("not open yet", lines[0])
        self.assertIn(" 9:25 bar  closed Rs 1208.40  opening range set: high Rs 1223.40, low Rs 1207.30", text)
        self.assertEqual(sum(1 for x in lines if " bar  closed " in x), 7)    # 9:15 ... 9:45, one line each

    def test_a_computer_that_dozes_off_catches_up_when_it_wakes(self):
        class Dozing(entry.SimClock):
            naps = 0

            def sleep(self, seconds):
                entry.SimClock.sleep(self, seconds)
                self.naps += 1
                if self.naps == 3:                      # the laptop sleeps for 17 minutes mid-wait
                    self.t += datetime.timedelta(minutes=17)
        clock = Dozing(ist("2026-10-19 09:31"))
        calls, lines = [], []
        code, res = entry.watch_loop(live_provider(doc(), calls=calls), clock.now, clock.sleep, lines.append,
                                     dict(self.OPTS), every=30)
        self.assertEqual(code, 0)
        times = [entry.to_local(c).strftime("%H:%M:%S") for c in calls]
        self.assertEqual(times[:2], ["09:31:00", "09:49:30"])   # straight after waking, not a long sleep later
        text = "\n".join(lines)
        for start in (" 9:30", " 9:35", " 9:40"):
            self.assertIn(start + " bar  closed", text)          # the bars it slept through, one line each
        self.assertEqual(res["trigger"]["time"], "9:50 AM")

    def test_expires_and_until(self):
        code, res, lines, fired, clock = self.loop(live_provider(no_breakout()), "2026-10-19 14:00", every=300)
        self.assertEqual((code, res["status"], fired), (1, "expired", []))
        self.assertEqual(entry.to_local(clock.now()).strftime("%H:%M"), "14:45")
        code, res, lines, fired, clock = self.loop(live_provider(no_breakout()), "2026-10-19 10:30", every=300,
                                                   until=DT(2026, 10, 19, 11, 0))
        self.assertEqual(code, 1)
        self.assertIn("--until 11:00", lines[-1])

    def test_closed_day_and_repeated_errors(self):
        code, _, lines, _, _ = self.loop(live_provider(doc()), "2026-10-17 10:00")
        self.assertEqual(code, 1)
        self.assertIn("weekend", lines[0])

        def broken(now):
            raise entry.EntryError("Yahoo did not answer")
        code, _, lines, _, _ = self.loop(broken, "2026-10-19 10:00", every=30)
        self.assertEqual(code, 1)
        self.assertIn("Stopping after 5 failed reads", lines[-1])


P2 = os.path.join(FIX, "p2")
NSEI = os.path.join(P2, "yahoo-NSEI-5m.json")
BHAV19 = os.path.join(P2, "fo-bhavcopy-20261019.csv.zip")
BHAV16 = os.path.join(P2, "fo-bhavcopy-20261016.csv.zip")


class Futures(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def fut(self, *extra):
        return run(["check", "NIFTY", "--instrument", "fut", "--offline", NSEI, "--now", "2026-10-21 09:35",
                    "--write"] + list(extra))

    def test_the_example_prices_the_future_from_the_last_session(self):
        code, text, _ = self.fut("--bhavcopy", BHAV19)
        self.assertEqual(code, 0, text)
        self.assertIn("TRIGGERED 9:35 AM at Rs %.2f" % EX.FUT_TRIGGER_CLOSE, text)
        self.assertIn(EX.FUT_CONTRACT, text)
        with open(os.path.join("trader", "entry.json")) as fh:
            e = json.load(fh)
        self.assertEqual(e["instrument"], "fut")
        self.assertEqual(e["price"], EX.FUT_TRIGGER_CLOSE)  # the signal stays on the index
        f = e["future"]
        self.assertEqual((f["contract"], f["expiry"], f["lot"], f["basis_date"], f["price"]),
                         (EX.FUT_CONTRACT, EX.FUT_EXPIRY, EX.FUT_LOT, EX.FUT_BASIS_DATE, EX.FUT_PRICE))
        self.assertAlmostEqual(f["basis_ratio"], EX.FUT_BASIS, places=9)
        self.assertFalse(f["estimated"])
        self.assertEqual(f["source"], "bhavcopy")

    def test_a_given_futures_price_wins(self):
        code, text, _ = self.fut("--bhavcopy", BHAV19, "--fut-price", "23050.10")
        self.assertEqual(code, 0, text)
        with open(os.path.join("trader", "entry.json")) as fh:
            f = json.load(fh)["future"]
        self.assertEqual((f["price"], f["source"], f["estimated"]), (23050.10, "user", False))

    def test_a_bhavcopy_two_sessions_old_is_estimated(self):
        code, text, _ = self.fut("--bhavcopy", BHAV16)
        self.assertEqual(code, 0, text)
        self.assertIn("estimated from 2026-10-16", text)
        with open(os.path.join("trader", "entry.json")) as fh:
            f = json.load(fh)["future"]
        self.assertTrue(f["estimated"])
        self.assertEqual(f["basis_date"], "2026-10-16")

    def test_on_expiry_day_it_is_the_next_contract(self):
        with open(os.path.join(FIX, "p0", "fo-bhavcopy-20261103.csv.zip"), "rb") as fh:
            rows = entry.india.parse_bhavcopy(fh.read())
        f = entry.future_leg(rows, "NIFTY", datetime.date(2026, 11, 23), 23800.0)
        self.assertEqual(f["contract"], "NIFTY26DECFUT")
        self.assertTrue(f["estimated"])  # a 3 Nov basis on 23 Nov
        self.assertNotIn("rolls over", " ".join(f["warnings"]))

    def test_the_day_before_expiry_warns_it_rolls_over(self):
        with open(os.path.join(FIX, "p0", "fo-bhavcopy-20261103.csv.zip"), "rb") as fh:
            rows = entry.india.parse_bhavcopy(fh.read())
        f = entry.future_leg(rows, "NIFTY", datetime.date(2026, 11, 20), 23800.0)
        self.assertEqual(f["contract"], "NIFTY26NOVFUT")
        self.assertIn("rolls over", " ".join(f["warnings"]))
        self.assertIn("NIFTY26DECFUT", " ".join(f["warnings"]))

    def test_watch_gets_the_bhavcopy_before_the_first_bar(self):
        code, _, err = run(["watch", "NIFTY", "--instrument", "fut", "--offline", NSEI, "--now", "2026-10-21 09:00",
                            "--until", "09:20", "--bhavcopy", os.path.join(self.d, "missing.zip")])
        self.assertEqual(code, 1)
        self.assertIn("cannot read the bhavcopy", err)   # said at the start, not lost at the trigger

    def test_fut_needs_an_index(self):
        code, _, err = run(["check", "RELIANCE", "--instrument", "fut", "--offline", REL,
                            "--now", "2026-10-19 09:50", "--bhavcopy", BHAV19])
        self.assertEqual(code, 1)
        self.assertIn("NIFTY or BANKNIFTY", err)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_check_write_is_the_contract_shape(self):
        code, text, _ = run(["check", "RELIANCE", "--offline", REL, "--now", "2026-10-19 09:50", "--write"])
        self.assertEqual(code, 0)
        self.assertIn("TRIGGERED 9:50 AM at Rs 1227.80", text)
        self.assertIn("/bharat-exit", text)
        with open(os.path.join("trader", "entry.json")) as fh:
            e = json.load(fh)
        want = {"created_by": "bharat-entry", "symbol": "RELIANCE", "setup": "orb", "side": "buy",
                "date": "2026-10-19", "status": "triggered", "time": "9:50 AM", "bar_start": "09:45",
                "bar_index": 6, "price": 1227.8, "level": 1223.4, "interval": "5m",
                "opening_range": {"high": 1223.4, "low": 1207.3, "minutes": 15}, "source": "yahoo",
                "checked_at": "2026-10-19T04:20:00Z", "prev_close": 1211.2, "band": None, "near_band": None}
        for k, v in want.items():
            self.assertEqual(e[k], v, k)

    def test_check_waiting_json_and_exit_codes(self):
        code, text, _ = run(["check", "RELIANCE", "--offline", REL, "--now", "2026-10-19 09:47", "--json"])
        self.assertEqual(code, 1)
        r = json.loads(text)
        self.assertEqual(r["status"], "waiting")
        self.assertNotIn("_bars", r)

    def test_offline_gz_folder_and_mismatch(self):
        os.mkdir("charts")
        with open(REL, "rb") as src, gzip.open(os.path.join("charts", "RELIANCE.json.gz"), "wb") as dst:
            dst.write(src.read())
        code, text, _ = run(["check", "reliance", "--offline", "charts", "--now", "2026-10-19 09:50"])
        self.assertEqual(code, 0)
        code, _, err = run(["check", "TCS", "--offline", REL])
        self.assertEqual(code, 1)
        self.assertIn("holds RELIANCE.NS, not TCS", err)

    def test_longs_only_and_flag_errors(self):
        code, _, err = run(["check", "RELIANCE", "--offline", REL, "--side", "short"])
        self.assertIn("P1 plans longs only", err)
        code, _, err = run(["check", "RELIANCE", "--offline", REL, "--level", "300"])
        self.assertIn("--level is for --setup level", err)
        code, _, err = run(["check", "RELIANCE", "--offline", REL, "--setup", "level"])
        self.assertIn("needs --level", err)
        code, _, err = run(["check", "RELIANCE", "--offline", REL, "--band", "0"])
        self.assertIn("--band", err)

    def test_config_is_read(self):
        os.mkdir("trader")
        with open(os.path.join("trader", "config.json"), "w") as fh:
            json.dump({"no_entries_after": "09:45"}, fh)
        code, text, _ = run(["check", "RELIANCE", "--offline", REL, "--now", "2026-10-19 09:50"])
        self.assertEqual(code, 1)
        self.assertIn("EXPIRED", text)
        with open(os.path.join("trader", "config.json"), "w") as fh:
            fh.write("{nope")
        code, _, err = run(["check", "RELIANCE", "--offline", REL])
        self.assertIn("not valid JSON", err)

    def test_watch_replays_the_file_and_writes_the_entry(self):
        code, text, _ = run(["watch", "RELIANCE", "--offline", REL, "--now", "2026-10-19 09:31", "--every", "30"])
        self.assertEqual(code, 0)
        self.assertIn(" 9:45 bar  closed Rs 1227.80  TRIGGERED", text)
        with open(os.path.join("trader", "entry.json")) as fh:
            self.assertEqual(json.load(fh)["bar_index"], 6)
        code, text, _ = run(["watch", "RELIANCE", "--offline", REL, "--now", "2026-10-19 09:31", "--until", "09:40",
                             "--out", "x.json"])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists("x.json"))

    def test_watchlist_takes_the_band_from_the_scanner(self):
        os.mkdir("charts")
        shutil.copy(REL, os.path.join("charts", "RELIANCE.json"))
        shutil.copy(DEMO, os.path.join("charts", "DEMO.json"))
        os.mkdir("trader")
        with open(os.path.join("trader", "watchlist.json"), "w") as fh:
            json.dump({"created_by": "bharat-scanner", "hits": [
                {"symbol": "RELIANCE", "setups": ["orb"], "band": None},
                {"symbol": "ZZZZ", "setups": ["orb"]}, {"symbol": "DEMO", "setups": ["orb"]}]}, fh)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-10-19 09:50"])
        self.assertEqual(code, 0)
        rows = text.splitlines()
        self.assertTrue(rows[1].startswith("RELIANCE    triggered  9:50 AM   Rs 1227.80  Rs 1223.40"), rows[1])
        self.assertIn("ZZZZ        error: no offline file", rows[2])
        self.assertIn("DEMO        error: DEMO has 1m bars", rows[3])
        self.assertIn("1 of 3 triggered", text)
        with open(os.path.join("trader", "watchlist.json"), "w") as fh:
            json.dump({"hits": [{"symbol": "RELIANCE", "band": 1.5}]}, fh)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-10-19 09:50", "--json"])
        self.assertEqual(json.loads(text)[0]["result"]["near_band"]["upper"], 1229.35)
        code, _, err = run(["watchlist", "--watchlist", "missing.json"])
        self.assertIn("run /bharat-scanner first", err)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "bharat-entry", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        with open(os.path.join(ROOT, "bharat-risk", "SKILL.md"), encoding="utf-8") as fh:
            risk = fh.read()
        self.assertTrue(text.startswith("---\nname: bharat-entry\n"))
        self.assertGreater(len(text.splitlines()), 60)
        self.assertEqual(text[text.index("## Safety"):], risk[risk.index("## Safety"):])
        low = " ".join(text.lower().split())
        for must in ("bar by bar", "can lag", "most retail intraday traders lose money", "/bharat-exit",
                     "longs only", "in the background", "/loop", "nothing was placed", "9:50 am", "rs 1227.80",
                     "bar 6", "9:15 bar", "price band"):
            self.assertIn(must, low)
        for never in (chr(0x2014), "tick " + "by tick", "real " + "time", "pro" + "fit", "new york", "nyse"):
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()


class FuturesDeferred(unittest.TestCase):
    def test_fut_price_needs_instrument_fut(self):
        code, _, err = run(["check", "RELIANCE", "--offline", REL, "--now", "2026-10-19 09:50",
                            "--fut-price", "23050"])
        self.assertEqual(code, 1)
        self.assertIn("--instrument fut", err)

    def test_year_end_without_next_year_holidays(self):
        # no 2027 holiday list: 30 Dec is not "the day before" the 25 Jan expiry
        rows = [dict(kind="fut", symbol="NIFTY", contract="NIFTY27JANFUT", expiry=datetime.date(2027, 1, 25),
                     strike=None, right=None, close=24000.0, settle=24000.0, underlying=23900.0, oi=1, lot=65,
                     date=datetime.date(2026, 12, 29))]
        for day in (datetime.date(2026, 12, 30), datetime.date(2026, 12, 31)):
            f = entry.future_leg(rows, "NIFTY", day, 23900.0)
            self.assertEqual(f["contract"], "NIFTY27JANFUT", day)
            self.assertNotIn("rolls over", " ".join(f["warnings"]), day)

    def test_no_known_last_session_flags_the_bhavcopy_as_maybe_old(self):
        # from Feb 2027 (no 2027 holiday list) there is no known last session: a --bhavcopy FILE may be any age
        with open(BHAV19, "rb") as fh:
            rows = entry.india.parse_bhavcopy(fh.read())
        day, at = datetime.date(2026, 10, 21), datetime.datetime(2026, 10, 21, 9, 35, tzinfo=entry.india.IST)
        saved, entry.india.prev_trading_day = entry.india.prev_trading_day, lambda d: None
        try:
            f = entry.future_leg(rows, "NIFTY", day, 23000.0)
            o = entry.option_leg(rows, "NIFTY", day, 23000.0, at, "CE")
        finally:
            entry.india.prev_trading_day = saved
        self.assertTrue(f["estimated"])
        self.assertTrue([w for w in f["warnings"] if "last session" in w and "unknown" in w], f["warnings"])
        self.assertTrue([w for w in o["warnings"] if "last session" in w and "unknown" in w], o["warnings"])



class Options(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def opt(self, *extra, now="2026-10-21 09:35", bhav=BHAV19):
        return run(["check", "NIFTY", "--instrument", "opt", "--offline", NSEI, "--now", now, "--bhavcopy", bhav,
                    "--write"] + list(extra))

    def entry(self):
        with open(os.path.join("trader", "entry.json")) as fh:
            return json.load(fh)

    def test_the_example_atm_call_and_its_estimated_premium(self):
        code, text, err = self.opt("--right", "CE")
        self.assertEqual(code, 0, text + err)
        e = self.entry()
        self.assertEqual((e["instrument"], e["price"]), ("opt", EX.FUT_TRIGGER_CLOSE))  # the signal is the index
        o = e["option"]
        self.assertEqual((o["contract"], o["strike"], o["right"], o["expiry"], o["lot"], o["oi"]),
                         (EX.OPT_CONTRACT, EX.OPT_STRIKE, "CE", EX.OPT_EXPIRY, EX.OPT_LOT, EX.OPT_OI))
        self.assertEqual((o["premium"], o["premium_source"], o["bhav_date"]), (EX.OPT_PREMIUM, "estimated", "2026-10-19"))
        self.assertAlmostEqual(o["iv"], EX.OPT_IV, places=4)
        self.assertIn(EX.OPT_CONTRACT, text)
        self.assertIn("estimated", text)
        self.assertIn("implied volatility", text)

    def test_a_given_premium_sets_the_iv(self):
        code, text, err = self.opt("--right", "CE", "--premium", "170")
        self.assertEqual(code, 0, text + err)
        o = self.entry()["option"]
        self.assertEqual((o["premium"], o["premium_source"]), (170.0, "user"))
        self.assertGreater(o["iv"], EX.OPT_IV)

    def test_steps_otm(self):
        self.opt("--right", "CE", "--steps-otm", "2")
        self.assertEqual(self.entry()["option"]["strike"], 23200.0)

    def test_a_put_triggers_on_the_first_close_under_the_range_low(self):
        code, text, err = self.opt("--right", "PE", now="2026-10-19 12:05", bhav=BHAV16)
        self.assertEqual(code, 0, text + err)
        e = self.entry()
        low = e["opening_range"]["low"]
        self.assertLess(e["price"], low)
        self.assertEqual(e["index_side"], "down")
        self.assertIn("below the range low", text)
        # no earlier closed bar after the range was under the low
        ch = entry.get_chart("NIFTY", "5m", datetime.date(2026, 10, 19), NSEI, datetime.date(2026, 10, 19))
        res = entry.evaluate(ch, entry.parse_now("2026-10-19 12:05"), below=True)
        first = [b for b in res["_bars"] if b["slot"] >= 3 and b["c"] < low][0]
        self.assertEqual(res["trigger"]["bar_index"], first["slot"])
        self.assertEqual(e["option"]["right"], "PE")

    def test_watch_lines_for_a_put_say_below(self):
        # NIFTY Oct 19: range low 22970.85; the 12:00 bar closed under it at 22954.25
        ch = entry.get_chart("NIFTY", "5m", datetime.date(2026, 10, 19), NSEI, datetime.date(2026, 10, 19))
        res = entry.evaluate(ch, entry.parse_now("2026-10-19 12:05"), below=True)
        bar = {b["et"].strftime("%H:%M"): b for b in res["_bars"]}
        self.assertEqual(entry.bar_line(bar["09:30"], res),
                         " 9:30 bar  closed Rs 23096.90  Rs 126.05 over Rs 22970.85")     # not yet: still above
        self.assertEqual(entry.bar_line(bar["12:00"], res),
                         "12:00 bar  closed Rs 22954.25  TRIGGERED: closed below Rs 22970.85 (-0.07%). Get in now "
                         "if you take it: 12:05 PM, Rs 22954.25")

    def test_a_put_on_a_level_says_close_below_in_the_header(self):
        ch = entry.get_chart("NIFTY", "5m", datetime.date(2026, 10, 19), NSEI, datetime.date(2026, 10, 19))
        res = entry.evaluate(ch, entry.parse_now("2026-10-19 12:05"), setup="level", level=22970.85, below=True)
        head = entry.describe(res)[0]
        self.assertIn("close below Rs 22970.85", head)
        self.assertNotIn("above", head)

    def test_an_illiquid_strike_warns(self):
        with open("cfg.json", "w") as fh:
            json.dump({"min_option_oi": 10 ** 9}, fh)
        code, text, _ = self.opt("--right", "CE", "--config", "cfg.json")
        self.assertIn("open interest", text)
        self.assertTrue(self.entry()["option"]["illiquid"])

    def test_the_option_flags_are_checked(self):
        code, _, err = self.opt()
        self.assertEqual(code, 1)
        self.assertIn("--right CE or PE", err)
        code, _, err = run(["check", "NIFTY", "--offline", NSEI, "--now", "2026-10-21 09:35", "--right", "CE"])
        self.assertIn("--instrument opt", err)
        code, _, err = run(["check", "RELIANCE", "--instrument", "opt", "--right", "CE", "--offline", REL,
                            "--now", "2026-10-19 09:50", "--bhavcopy", BHAV19])
        self.assertIn("NIFTY or BANKNIFTY", err)


class ReviewFixesP3(unittest.TestCase):
    setUp = Options.setUp
    tearDown = Options.tearDown
    entry = Options.entry

    def test_a_given_premium_solves_its_iv_at_the_index_it_was_read_at(self):
        code, text, err = Options.opt(self, "--right", "CE", "--premium", "198.87", "--premium-index", "23180",
                                      now="2026-10-21 10:00")
        self.assertEqual(code, 0, text + err)
        self.assertAlmostEqual(self.entry()["option"]["iv"], EX.OPT_IV, delta=0.002)

