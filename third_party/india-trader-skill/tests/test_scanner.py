import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX
import example_values as EX

scan = load("bharat-scanner", "scan")

SDIR = os.path.join(FIX, "bharat-scanner")
WALL = os.path.join(SDIR, "wall5m-2026-10-19.json.gz")        # the NIFTY 50's real 5-min charts, Mon Oct 19 2026
DAILY = os.path.join(SDIR, "daily-spark-2026-11-04.json")     # a real 20-symbol spark answer, fetched 4 Nov 2026
NSE = os.path.join(SDIR, "nse")                               # sec_list.csv, fo_secban.csv, ind_nifty50list.csv
P0 = os.path.join(FIX, "p0")                                  # EQUITY_L.csv
DT = datetime.datetime
D = datetime.date


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scan.main(argv)
    return code, out.getvalue(), err.getvalue()


def ts(ist_text):
    """IST wall-clock text -> unix seconds."""
    utc = scan.local_to_utc(DT.strptime(ist_text, "%Y-%m-%d %H:%M"))
    return int((utc - DT(1970, 1, 1)).total_seconds())


def bars_series(day, closes, highs=None, start="09:15"):
    t0 = DT.strptime(day + " " + start, "%Y-%m-%d %H:%M")
    t = [ts((t0 + datetime.timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M")) for i in range(len(closes))]
    return {"t": t, "c": list(closes),
            "h": list(highs or [c + 0.1 if c is not None else None for c in closes]),
            "l": [c - 0.1 if c is not None else None for c in closes], "o": None,
            "v": [1000] * len(closes), "meta": {}}


class Universe(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(P0, "EQUITY_L.csv"), encoding="utf-8") as fh:
            self.kept, self.counts = scan.parse_equity_l(fh.read())

    def test_eq_series_only(self):
        self.assertEqual(self.counts, {"rows": 2627, "EQ": 2392, "BE": 202, "BZ": 33})
        self.assertEqual(len(self.kept), 2392)
        names = dict(self.kept)
        self.assertEqual(names["RELIANCE"], "Reliance Industries Limited")
        self.assertIn("M&M", names)
        self.assertIn("BAJAJ-AUTO", names)

    def test_not_equity_l(self):
        with self.assertRaises(scan.ScanError):
            scan.parse_equity_l("<html>maintenance</html>")

    def test_bands_and_ban(self):
        with open(os.path.join(NSE, "sec_list.csv"), encoding="utf-8") as fh:
            bands = scan.parse_bands(fh.read())
        self.assertIsNone(bands["RELIANCE"])          # F&O stocks have no fixed band
        self.assertEqual(bands["A2ZINFRA"], 5.0)
        self.assertEqual(sum(1 for v in bands.values() if v == 5.0), 139)   # EQ series only (BE has the 2 % bands)
        with open(os.path.join(NSE, "fo_secban.csv"), encoding="utf-8") as fh:
            self.assertEqual(scan.parse_ban(fh.read()), {"KAYNES", "SAIL"})


class Time(unittest.TestCase):
    def test_ist_clock(self):
        self.assertEqual(scan.ts_to_local(ts("2026-10-19 09:15")), DT(2026, 10, 19, 9, 15))
        self.assertEqual(scan.parse_now("2026-10-19 10:15"), DT(2026, 10, 19, 4, 45))
        self.assertEqual(scan.parse_now("2026-10-19T04:45:00Z"), DT(2026, 10, 19, 4, 45))
        self.assertEqual(scan.clock_label(DT(2026, 10, 19, 10, 15)), "10:15 AM")
        self.assertEqual(scan.session(D(2026, 10, 19)), (datetime.time(9, 15), datetime.time(15, 30)))
        self.assertIsNone(scan.session(D(2026, 10, 20)))           # Dussehra
        with self.assertRaises(scan.ScanError):
            scan.parse_now("tomorrow")
        with self.assertRaises(scan.ScanError):
            scan.parse_hhmm("25:00")


class Spark(unittest.TestCase):
    def test_a_real_spark_answer(self):
        with open(DAILY, encoding="utf-8") as fh:
            text = fh.read()
        wanted = {"M&M.NS": "M&M", "ZZZZQX.NS": "ZZZZQX"}
        data = scan.parse_spark(text, wanted)
        self.assertEqual(len(data), 19)               # 20 asked; Yahoo drops the made-up ZZZZQX
        self.assertNotIn("ZZZZQX", data)
        self.assertIn("M&M", data)
        self.assertIn("BAJAJ-AUTO", data)             # .NS stripped back to ours
        self.assertIsNone(data["RELIANCE"]["h"])      # spark is closes only

    def test_the_empty_last_close_comes_from_the_meta(self):
        data, _ = scan.load_offline(DAILY)
        rel = data["RELIANCE"]
        self.assertIsNone(rel["c"][-1])               # Yahoo had not filled Tue Nov 3 yet
        closes = scan.daily_closes(rel, scan.parse_now("2026-11-04 08:30"))
        self.assertEqual(closes[-2:], [(D(2026, 11, 2), 1300.0), (D(2026, 11, 3), 1307.9)])
        # during Tuesday's session the meta is a live price, not a close
        closes = scan.daily_closes(rel, scan.parse_now("2026-11-03 12:00"))
        self.assertEqual(closes[-1][0], D(2026, 11, 2))

    def test_urls(self):
        self.assertEqual(scan.spark_path(["M&M", "reliance"], "3mo", "1d"),
                         "/v7/finance/spark?symbols=M%26M.NS,RELIANCE.NS&range=3mo&interval=1d")
        self.assertEqual(scan.chart_path("NIFTY", "1d", "5m"), "/v8/finance/chart/^NSEI?range=1d&interval=5m")
        self.assertEqual(scan.from_yahoo("M%26M.NS"), "M&M")
        self.assertEqual(scan.from_yahoo("^NSEBANK"), "BANKNIFTY")


class Network(unittest.TestCase):
    def setUp(self):
        self.calls, self.sleeps = [], []
        self._get, self._sleep = scan.http_get, scan.SLEEP
        scan.SLEEP = self.sleeps.append
        with open(DAILY, encoding="utf-8") as fh:
            self.body = fh.read()

    def tearDown(self):
        scan.http_get, scan.SLEEP = self._get, self._sleep

    def answers(self, codes):
        codes = list(codes)

        def fake(url, timeout=20):
            self.calls.append(url.split("/v")[0])
            code = codes.pop(0) if codes else 200
            if code != 200:
                raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(b""))
            return self.body
        scan.http_get = fake

    def test_429_falls_back_then_waits_once(self):
        self.answers([429, 429, 200])
        self.assertEqual(scan.yahoo_get("/v7/finance/spark?symbols=TCS.NS&range=3mo&interval=1d"), self.body)
        self.assertEqual(self.calls, ["https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com",
                                      "https://query2.finance.yahoo.com"])
        self.assertEqual(self.sleeps, [scan.BACKOFF])

    def test_429_for_good_is_reported_as_partial(self):
        self.answers([200] + [429] * 10)
        syms = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN", "M&M", "BAJAJ-AUTO", "ITC", "LT",
                "TITAN", "MARUTI", "SUNPHARMA", "TATASTEEL", "WIPRO", "ADANIENT", "ONGC", "HCLTECH", "AXISBANK",
                "ZZZZQX"] + ["X%02d" % i for i in range(20)]
        data, failed = scan.fetch_spark(syms, "3mo", "1d", 0.3)
        self.assertEqual(len(data), 19)
        self.assertEqual(failed["ZZZZQX"], "no data from Yahoo")
        self.assertIn("HTTP 429", failed["X00"])
        self.assertEqual(self.sleeps, [0.3, scan.BACKOFF])

    def test_a_404_is_final(self):
        self.answers([404])
        with self.assertRaises(scan.ScanError):
            scan.yahoo_get("/v8/finance/chart/NOPE.NS?range=1d&interval=5m")
        self.assertEqual(len(self.calls), 1)


class Daily(unittest.TestCase):
    # Through Tue Nov 3 2026. Hand check: RELIANCE 1307.90 > 1300.00, the best close of the 20 before.
    EXPECT = {"RELIANCE": ["breakout_20d"], "HDFCBANK": ["reclaim_ma20"], "ICICIBANK": ["breakout_20d"],
              "TCS": ["bull_flag"], "ITC": ["breakout_20d"], "LT": ["breakout_20d"],
              "BAJAJ-AUTO": ["breakout_20d"], "MARUTI": ["breakout_20d"], "HCLTECH": ["breakout_20d", "bull_flag"]}

    def setUp(self):
        self.data, _ = scan.load_offline(DAILY)
        self.syms = sorted(self.data)

    def test_setups_before_the_open(self):
        hits, counts, last = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-11-04 08:30"), 0, 0)
        self.assertEqual({h["symbol"]: h["setups"] for h in hits}, self.EXPECT)
        self.assertEqual(last, D(2026, 11, 3))
        self.assertEqual(hits[0]["symbol"], "RELIANCE")   # most rupees traded first
        self.assertEqual((hits[0]["price"], hits[0]["close_date"]), (1307.9, "2026-11-03"))

    def test_turnover_filter(self):
        # last day's turnover: WIPRO Rs 198 x 8.43M = Rs 166.9 crore, the smallest here
        hits, counts, _ = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-11-04 08:30"), 50,
                                          170 * scan.CRORE)
        self.assertEqual(counts["illiquid"], 1)
        hits, counts, _ = scan.scan_daily(self.data, self.syms, scan.parse_now("2026-11-04 08:30"), 1000, 0)
        self.assertEqual(sorted(h["symbol"] for h in hits),
                         ["BAJAJ-AUTO", "HCLTECH", "ICICIBANK", "LT", "MARUTI", "RELIANCE", "TCS"])

    def test_rsi(self):
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61,
                  46.28, 46.28]
        self.assertAlmostEqual(scan.rsi(closes), 70.46, places=1)


class Orb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data, cls.errors = scan.load_offline(WALL)
        cls.syms = sorted(cls.data)

    def scan_at(self, now, min_price=0, syms=None, **kw):
        day, cutoff = scan.orb_window(scan.parse_now(now), None, 15, datetime.time(14, 45))
        return scan.scan_orb(self.data, syms or self.syms, day, cutoff, 15, min_price, 0, **kw)

    def test_the_example_scan_at_1015(self):
        hits, counts, failed = self.scan_at("2026-10-19 10:15")
        self.assertEqual(len(self.syms), 50)
        self.assertEqual(counts["scanned"], 50)
        self.assertEqual([h["symbol"] for h in hits],
                         ["SBIN", "ULTRACEMCO", "HCLTECH", "MARUTI", "TCS", "RELIANCE", "TECHM", "TITAN"])
        rel = [h for h in hits if h["symbol"] == "RELIANCE"][0]
        self.assertEqual(rel, {"symbol": "RELIANCE", "setups": ["orb"], "price": 1227.8, "at": "9:50 AM",
                               "bar_start": "09:45", "bar_index": 6, "or_high": 1223.4, "or_low": 1207.3,
                               "pct_over": 0.36, "fno_ban": False})
        self.assertEqual((len(hits), rel["or_high"], rel["or_low"], rel["bar_start"], rel["at"], rel["price"],
                          rel["pct_over"]),
                         (EX.SCAN_HITS, EX.OR_HIGH, EX.OR_LOW, EX.TRIGGER_BAR, EX.TRIGGER_AT, EX.TRIGGER_CLOSE,
                          EX.PCT_OVER))

    def test_in_progress_bar(self):
        self.assertEqual(self.scan_at("2026-10-19 09:49", syms=["RELIANCE"])[0], [])
        self.assertEqual(self.scan_at("2026-10-19 09:50", syms=["RELIANCE"])[0][0]["at"], "9:50 AM")

    def test_the_closing_print_and_live_tick_are_dropped(self):
        s = self.data["RELIANCE"]
        bars = scan.session_bars(s, D(2026, 10, 19), DT(2026, 10, 19, 15, 30))
        self.assertEqual(len(bars), 74)                   # 9:15 to 15:25; Yahoo has no close for 15:20
        self.assertEqual(bars[-1]["start"], DT(2026, 10, 19, 15, 25))
        self.assertNotIn(DT(2026, 10, 19, 15, 20), [b["start"] for b in bars])

    def test_bands_and_ban_flag_hits(self):
        hits, _, _ = self.scan_at("2026-10-19 10:15", syms=["RELIANCE", "SBIN"], bands={"SBIN": 20.0},
                                  banned={"SBIN"})
        sbin = [h for h in hits if h["symbol"] == "SBIN"][0]
        self.assertEqual((sbin["band"], sbin["fno_ban"]), (20.0, True))   # flagged, not dropped: cash is allowed
        self.assertIsNone([h for h in hits if h["symbol"] == "RELIANCE"][0]["band"])

    def test_confirm_uses_the_v8_highs(self):
        spark_like = {k: dict(v, h=None, l=None) for k, v in self.data.items()}
        asked = []

        def confirm(sym):
            asked.append(sym)
            return self.data[sym]
        hits, counts, _ = scan.scan_orb(spark_like, self.syms, D(2026, 10, 19), DT(2026, 10, 19, 10, 15), 15,
                                        0, 0, confirm)
        self.assertEqual(len(hits), 8)
        self.assertEqual(counts["candidates"], len(asked))
        self.assertLess(len(asked), 50)

    def test_a_missing_bar_makes_a_chart_unusable(self):
        s = bars_series("2026-10-21", [10, 10.2, 10.1, None, 10.5, 10.6, 10.7, 10.8, 10.9])
        bars = scan.session_bars(s, D(2026, 10, 21), DT(2026, 10, 21, 10, 0))
        self.assertFalse(scan.usable(bars, D(2026, 10, 21), DT(2026, 10, 21, 10, 0), 3))

    def test_window_messages(self):
        until = datetime.time(14, 45)

        def msg(now, at=None, until=until):
            with self.assertRaises(scan.ScanError) as cm:
                scan.orb_window(scan.parse_now(now), at, 15, until)
            return str(cm.exception)
        self.assertIn("weekend", msg("2026-10-17 10:15"))
        self.assertIn("Dussehra", msg("2026-10-20 10:15"))
        self.assertIn("holiday list unknown for 2028", msg("2028-03-01 10:15"))
        self.assertIn("Run orb again after 9:35 AM", msg("2026-10-19 09:30"))
        self.assertIn("expired", msg("2026-10-19 15:00"))
        self.assertIn("closed at 3:30 PM", msg("2026-10-19 16:00", until=datetime.time(16, 0)))
        self.assertIn("later than now", msg("2026-10-19 10:00", at=datetime.time(10, 15)))
        self.assertEqual(scan.orb_window(scan.parse_now("2026-10-19 16:30"), datetime.time(10, 15), 15, until),
                         (D(2026, 10, 19), DT(2026, 10, 19, 10, 15)))


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.t = os.path.join(self.d, "trader")
        self.wl = os.path.join(self.t, "watchlist.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_universe_daily_orb_end_to_end(self):
        uni = os.path.join(self.t, "universe.txt")
        code, text, _ = run(["universe", "--offline", P0, "--out", uni])
        self.assertEqual(code, 0)
        self.assertIn("Kept 2,392 EQ-series stocks", text)
        with open(uni, encoding="utf-8") as fh:
            self.assertIn("RELIANCE|Reliance Industries Limited", fh.read().splitlines())

        code, text, _ = run(["daily", "--offline", DAILY, "--now", "2026-11-04 08:30", "--out", self.wl])
        self.assertEqual(code, 0)
        self.assertIn("(Rs 50+ and Rs 5 crore+ traded)", text)
        self.assertEqual(self.read(self.wl)["as_of"], "2026-11-03 close")

        code, text, _ = run(["orb", "--offline", WALL, "--now", "2026-10-19 10:15", "--nse-files", NSE,
                             "--out", self.wl])
        self.assertEqual(code, 0)
        self.assertIn("15-minute range (9:15 to 9:30 AM)", text)
        self.assertIn("8 of 50 charts closed above their opening-range high by 10:15 AM", text)
        self.assertIn("9:50 AM  RELIANCE     Rs 1,227.80  range 1,207.30 to 1,223.40  +0.36% over", text)
        doc = self.read(self.wl)
        self.assertEqual({k: doc[k] for k in ("created_by", "created_at", "mode", "as_of", "universe", "scanned")},
                         {"created_by": "bharat-scanner", "created_at": "2026-10-19T04:45:00Z", "mode": "orb",
                          "as_of": "2026-10-19 10:15 IST", "universe": 50, "scanned": 50})
        rel = [h for h in doc["hits"] if h["symbol"] == "RELIANCE"][0]
        self.assertEqual((rel["band"], rel["fno_ban"]), (None, False))

    def test_plain_messages_and_exit_codes(self):
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-10-17 10:15", "--out", self.wl])
        self.assertEqual(code, 1)
        self.assertIn("No session on Sat 2026-10-17 (weekend)", err)
        self.assertNotIn("Traceback", err)
        code, _, err = run(["orb", "--offline", WALL, "--now", "2026-10-19 09:31", "--out", self.wl])
        self.assertIn("after 9:35 AM", err)
        code, _, err = run(["orb", "--offline", WALL])
        self.assertIn("--at HH:MM", err)
        code, _, err = run(["daily", "--universe", os.path.join(self.d, "nope.txt")])
        self.assertIn("scan.py universe", err)

    def test_symbol_lists(self):
        txt = os.path.join(self.d, "list.txt")
        with open(txt, "w") as fh:
            fh.write("# my list\nRELIANCE|Reliance Industries Limited\nm&m\n")
        self.assertEqual(scan.symbols_from_file(txt), ["RELIANCE", "M&M"])
        code, text, _ = run(["daily", "--offline", DAILY, "--watchlist", txt, "--now", "2026-11-04 08:30",
                             "--json", "--no-write"])
        self.assertEqual(json.loads(text)["scanned"], 2)



class IndexFutures(unittest.TestCase):
    def test_orb_on_the_indices_lists_the_futures_example(self):
        d, cwd = tempfile.mkdtemp(), os.getcwd()
        try:
            os.chdir(d)                       # the scan writes trader/watchlist.json here
            os.mkdir("charts")
            shutil.copy(os.path.join(FIX, "p2", "yahoo-NSEI-5m.json"), os.path.join("charts", "NIFTY.json"))
            shutil.copy(os.path.join(FIX, "p2", "yahoo-NSEBANK-5m.json"), os.path.join("charts", "BANKNIFTY.json"))
            code, text, err = run(["orb", "--symbols", "NIFTY,BANKNIFTY", "--offline", "charts",
                                   "--now", "2026-10-21 10:15", "--min-price", "0", "--json"])
        finally:
            os.chdir(cwd)
            shutil.rmtree(d)
        hits = {h["symbol"]: h for h in json.loads(text)["hits"]}
        n = hits["NIFTY"]
        self.assertEqual((n["price"], n["at"], n["or_high"], n["or_low"]),
                         (EX.FUT_TRIGGER_CLOSE, EX.FUT_TRIGGER_AT, EX.FUT_OR_HIGH, EX.FUT_OR_LOW))

if __name__ == "__main__":
    unittest.main()
