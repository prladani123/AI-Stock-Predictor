import json
import os
import sys
import unittest
from datetime import date, datetime, time, timezone

from _load import FIX, ROOT

sys.path.insert(0, os.path.join(ROOT, "india"))
sys.modules.pop("india", None)
import india  # noqa: E402


class Calendar(unittest.TestCase):
    def test_holiday(self):
        ok, why = india.trading_day(date(2026, 1, 26))
        self.assertFalse(ok)
        self.assertTrue(why.startswith("NSE holiday"), why)

    def test_weekend(self):
        self.assertEqual(india.trading_day(date(2026, 11, 7)), (False, "weekend"))

    def test_unknown_year(self):
        self.assertEqual(india.trading_day(date(2028, 3, 1))[1], "holiday list unknown for 2028")
        self.assertFalse(india.trading_day(date(2028, 3, 1))[0])

    def test_normal_day(self):
        self.assertEqual(india.trading_day(date(2026, 10, 19)), (True, ""))
        o, c = india.session(date(2026, 10, 19))
        self.assertEqual((o.time(), c.time()), (time(9, 15), time(15, 30)))

    def test_muhurat(self):
        self.assertTrue(india.trading_day(date(2026, 11, 8))[0])
        o, c = india.session(date(2026, 11, 8))
        self.assertEqual((o.time(), c.time()), (time(14, 20), time(15, 30)))

    def test_closed_day_has_no_session(self):
        self.assertIsNone(india.session(date(2026, 10, 20)))  # Dussehra

    def test_next_open_skips_holiday(self):
        self.assertEqual(india.next_open(date(2026, 10, 19)), datetime(2026, 10, 21, 9, 15, tzinfo=india.IST))


class Clock(unittest.TestCase):
    def test_to_ist(self):
        self.assertEqual(india.to_ist(datetime(2026, 11, 4, 3, 45, tzinfo=timezone.utc)).time(), time(9, 15))

    def test_naive_is_ist(self):
        self.assertEqual(india.ist_to_utc(datetime(2026, 11, 4, 9, 15)),
                         datetime(2026, 11, 4, 3, 45, tzinfo=timezone.utc))


class Symbols(unittest.TestCase):
    def test_yahoo(self):
        self.assertEqual(india.yahoo_symbol("M&M"), "M&M.NS")
        self.assertEqual(india.yahoo_symbol("BAJAJ-AUTO"), "BAJAJ-AUTO.NS")
        self.assertEqual(india.yahoo_symbol("NIFTY"), "^NSEI")
        self.assertEqual(india.yahoo_symbol("banknifty"), "^NSEBANK")
        self.assertEqual(india.yahoo_symbol("TCS.BO"), "TCS.BO")
        self.assertEqual(india.yahoo_symbol("^NSEI"), "^NSEI")
        self.assertEqual(india.yahoo_symbol(" reliance "), "RELIANCE.NS")


class Costs(unittest.TestCase):
    def test_matches_zerodha_calculator(self):
        with open(os.path.join(FIX, "p0", "zerodha-calc.json")) as fh:
            rows = json.load(fh)["rows"]
        for r in rows:
            got = india.round_trip(r["segment"], r["qty"], r["buy"], r["sell"])
            for k in ("brokerage", "stt", "exchange", "sebi", "stamp", "gst", "total"):
                self.assertAlmostEqual(got[k], r[k], delta=0.01, msg="%s %s" % (k, r))

    def test_leg_costs_sell_pays_stt_buy_pays_stamp(self):
        buy = india.leg_costs("eq_intraday", "buy", 100, 1400)
        sell = india.leg_costs("eq_intraday", "sell", 100, 1410)
        self.assertEqual(buy["stt"], 0)
        self.assertEqual(sell["stamp"], 0)
        self.assertEqual(buy["stamp"], 4)
        self.assertEqual(sell["stt"], 35)
        self.assertEqual(buy["brokerage"], 20)

    def test_unknown_segment(self):
        with self.assertRaises(ValueError):
            india.round_trip("mtf", 1, 1, 1)

    def test_rates_are_dated(self):
        self.assertEqual(india.RATES["checked"], "2026-11-04")


class Rupees(unittest.TestCase):
    def test_lakh_grouping(self):
        self.assertEqual(india.inr(200000), "Rs 2,00,000")
        self.assertEqual(india.inr(89629.4), "Rs 89,629.40")
        self.assertEqual(india.inr(12345678.5), "Rs 1,23,45,678.50")
        self.assertEqual(india.inr(987.73), "Rs 987.73")
        self.assertEqual(india.inr(-1000), "-Rs 1,000")
        self.assertEqual(india.inr(1227.8, paise=True), "Rs 1,227.80")


class Prices(unittest.TestCase):
    def test_tick_round(self):
        self.assertEqual(india.tick_round(1402.33), 1402.35)
        self.assertEqual(india.tick_round(1402.33, mode="down"), 1402.30)
        self.assertEqual(india.tick_round(1402.31, mode="up"), 1402.35)
        self.assertEqual(india.tick_round(1215.35), 1215.35)

    def test_band(self):
        self.assertTrue(india.band_ok(110.0, 100.0, 10))
        self.assertFalse(india.band_ok(110.05, 100.0, 10))
        self.assertTrue(india.band_ok(89.95, 100.0, None))  # "No Band"


class Fetch(unittest.TestCase):
    def test_html_is_an_error(self):
        class Resp:
            headers = {}
            def read(self):
                return b"<!DOCTYPE html><html>blocked</html>"
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        class Opener:
            def open(self, req, timeout=None):
                return Resp()

        with self.assertRaises(india.NSEError) as cm:
            india.nse_get("https://www.nseindia.com/api/x", opener=Opener(), warm=False)
        self.assertIn("api/x", str(cm.exception))

    def test_a_blocked_fetch_waits_only_between_its_three_tries(self):
        import io
        import urllib.error
        tries, slept = [], []

        class Blocked:
            def open(self, req, timeout=None):
                tries.append(req.full_url)
                raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b""))

        saved, india.SLEEP = india.SLEEP, slept.append
        try:
            with self.assertRaises(india.NSEError) as cm:
                india.nse_get("https://www.nseindia.com/api/x", opener=Blocked(), warm=False)
        finally:
            india.SLEEP = saved
        self.assertIn("HTTP 403", str(cm.exception))
        self.assertEqual(len(tries), 3)
        self.assertEqual(slept, [1, 2])          # no wait after the last try: it gives up at once


P2 = os.path.join(FIX, "p2")


def _bhav(name, folder="p2"):
    with open(os.path.join(FIX, folder, name), "rb") as fh:
        return india.parse_bhavcopy(fh.read())


class Contracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = _bhav("fo-bhavcopy-20261103.csv.zip", "p0")

    def test_nifty_front_month(self):
        f = india.futures(self.rows, "NIFTY")[0]
        self.assertEqual(f["contract"], "NIFTY26NOVFUT")
        self.assertEqual(f["expiry"], date(2026, 11, 23))
        self.assertEqual((f["kind"], f["lot"], f["close"], f["underlying"]), ("fut", 65, 23780.50, 23810.85))
        self.assertEqual(f["date"], date(2026, 11, 3))
        self.assertEqual([x["expiry"] for x in india.futures(self.rows, "NIFTY")],
                         [date(2026, 11, 23), date(2026, 12, 29), date(2027, 1, 25)])

    def test_options_rows(self):
        o = [r for r in self.rows if r["contract"] == "NIFTY26N0924400PE"][0]
        self.assertEqual((o["kind"], o["strike"], o["right"]), ("opt", 24400.0, "PE"))

    def test_front_future_skips_expiry_day(self):
        self.assertEqual(india.front_future(self.rows, "NIFTY", date(2026, 11, 20))["contract"], "NIFTY26NOVFUT")
        self.assertEqual(india.front_future(self.rows, "NIFTY", date(2026, 11, 23))["contract"], "NIFTY26DECFUT")
        # roll 1 trading day early: 20 Nov is the day before the 23 Nov expiry
        self.assertEqual(india.front_future(self.rows, "NIFTY", date(2026, 11, 20), roll_days=1)["contract"],
                         "NIFTY26DECFUT")
        self.assertIsNone(india.front_future(self.rows, "NOSUCH", date(2026, 11, 20)))

    def test_expiry_day(self):
        self.assertTrue(india.is_expiry_day(self.rows, "NIFTY", date(2026, 11, 23)))
        self.assertFalse(india.is_expiry_day(self.rows, "NIFTY", date(2026, 11, 24)))  # holiday

    def test_prev_trading_day(self):
        self.assertEqual(india.prev_trading_day(date(2026, 10, 21)), date(2026, 10, 19))  # 20 Oct holiday
        self.assertEqual(india.prev_trading_day(date(2026, 10, 19)), date(2026, 10, 16))  # weekend

    def test_basis_ratio(self):
        f = india.futures(self.rows, "NIFTY")[0]
        self.assertEqual(india.basis_ratio(f), 23780.50 / 23810.85)

    def test_plain_csv_and_old_day(self):
        import io, zipfile
        with open(os.path.join(P2, "fo-bhavcopy-20261019.csv.zip"), "rb") as fh:
            z = zipfile.ZipFile(io.BytesIO(fh.read()))
            plain = z.read(z.namelist()[0])
        f = india.front_future(india.parse_bhavcopy(plain), "NIFTY", date(2026, 10, 21))
        self.assertEqual((f["contract"], f["close"], f["underlying"]), ("NIFTY26OCTFUT", 22895.20, 22888.95))

    def test_fut_costs_match_zerodha(self):
        with open(os.path.join(P2, "zerodha-calc-fut.json")) as fh:
            rows = json.load(fh)["rows"]
        for r in rows:
            c = india.round_trip("fut", r["qty"], r["buy"], r["sell"])
            for k in india.COST_KEYS:
                self.assertAlmostEqual(c[k], r[k], delta=0.01, msg="%s %s" % (k, r))


if __name__ == "__main__":
    unittest.main()


P2DIR = os.path.join(FIX, "p2")
P3DIR = os.path.join(FIX, "p3")


def _rows(day):
    with open(os.path.join(P2DIR, "fo-bhavcopy-%s.csv.zip" % day), "rb") as fh:
        return india.parse_bhavcopy(fh.read())


class OptionsMath(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r19, cls.r21 = _rows("20261019"), _rows("20261021")

    def test_textbook_call(self):
        self.assertEqual(round(india.bs_price("CE", 100, 100, 1, 0.2, 0.05), 4), 10.4506)

    def test_put_call_parity(self):
        import math
        for k in (90, 100, 115):
            c = india.bs_price("CE", 100, k, 0.3, 0.25, 0.06)
            p = india.bs_price("PE", 100, k, 0.3, 0.25, 0.06)
            self.assertAlmostEqual(c - p, 100 - k * math.exp(-0.06 * 0.3), places=6)

    def test_expired_option_is_its_intrinsic_value(self):
        self.assertEqual(india.bs_price("CE", 105, 100, 0, 0.2), 5.0)
        self.assertEqual(india.bs_price("PE", 105, 100, 0, 0.2), 0.0)

    def test_implied_vol_round_trips(self):
        p = india.bs_price("PE", 23000, 22900, 0.02, 0.137, 0.06)
        v = india.implied_vol("PE", p, 23000, 22900, 0.02, 0.06)
        self.assertAlmostEqual(india.bs_price("PE", 23000, 22900, 0.02, v, 0.06), p, delta=1e-6)
        self.assertAlmostEqual(v, 0.137, places=4)

    def test_no_implied_vol_below_intrinsic_or_above_the_spot(self):
        for args in (("CE", 4.0, 105, 100, 0.1), ("PE", 201.0, 105, 200, 0.1), ("CE", 106.0, 105, 100, 0.1)):
            with self.assertRaises(ValueError) as cm:
                india.implied_vol(*args)
            self.assertIn("no implied volatility", str(cm.exception))

    def test_years_to_expiry(self):
        ist = india.IST
        self.assertEqual(india.years_to_expiry(datetime(2026, 11, 3, 15, 30, tzinfo=ist), date(2026, 11, 9)), 6 / 365.0)
        self.assertEqual(india.years_to_expiry(datetime(2026, 11, 9, 15, 31, tzinfo=ist), date(2026, 11, 9)), 0.0)

    def test_our_iv_matches_nse(self):
        with open(os.path.join(P3DIR, "iv-check.json")) as fh:
            doc = json.load(fh)
        for r in doc["rows"]:
            v = india.implied_vol(r["right"], r["premium"], doc["spot"], r["strike"], 5 / 365.0, doc["rate"])
            self.assertLess(abs(v * 100 - r["nse_iv"]), 0.5, r)

    def test_option_symbols(self):
        self.assertEqual(india.parse_option_symbol("NIFTY26O1923100CE"),
                         {"symbol": "NIFTY", "expiry": date(2026, 10, 19), "month": "2026-10", "strike": 23100.0,
                          "right": "CE", "weekly": True})
        self.assertEqual(india.parse_option_symbol("BANKNIFTY26OCT55000PE")["expiry"], None)
        self.assertIsNone(india.parse_option_symbol("NIFTY26OCTFUT"))
        for s in ("NIFTY26N3123100CE", "NIFTY26O0023100CE", "NIFTY2623023100CE"):   # 31 Nov, day 00, 30 Feb
            self.assertIsNone(india.parse_option_symbol(s), s)
        n = 0
        for r in self.r19:
            if r["kind"] == "opt" and r["symbol"] in ("NIFTY", "BANKNIFTY"):
                p = india.parse_option_symbol(r["contract"])
                self.assertIsNotNone(p, r["contract"])
                self.assertEqual((p["symbol"], p["strike"], p["right"], p["month"]),
                                 (r["symbol"], r["strike"], r["right"], r["expiry"].strftime("%Y-%m")))
                self.assertIn(p["expiry"], (None, r["expiry"]))
                n += 1
        self.assertGreater(n, 1000)

    def test_freeze_limits(self):
        self.assertEqual(india.FREEZE_QTY["NIFTY"] // 65, 27)
        self.assertEqual(india.FREEZE_QTY["BANKNIFTY"], 900)

    def test_strikes(self):
        ks = india.strikes(self.r19, "NIFTY", date(2026, 10, 27))
        self.assertEqual(ks, sorted(ks))
        self.assertEqual(india.atm_strike(ks, 23115.65), 23100.0)
        self.assertEqual(india.pick_strike(ks, 23115.65, "CE", 2), 23200.0)
        self.assertEqual(india.pick_strike(ks, 23115.65, "PE", 2), 23000.0)
        self.assertEqual(india.pick_strike(ks, 23115.65, "CE", 0), 23100.0)
        self.assertEqual(india.option_expiries(self.r19, "NIFTY")[:3],
                         [date(2026, 10, 19), date(2026, 10, 27), date(2026, 11, 3)])

    def test_option_row(self):
        r = india.option_row(self.r19, "NIFTY", date(2026, 10, 27), 23100, "CE")
        self.assertEqual((r["contract"], r["close"], r["oi"], r["lot"]), ("NIFTY26OCT23100CE", 87.25, 4934930, 65))
        self.assertGreater(r["volume"], 0)
        self.assertIsNone(india.option_row(self.r19, "NIFTY", date(2026, 10, 27), 23101, "CE"))

    def test_parity_forward(self):
        ratio = india.forward_ratio(self.r19, "NIFTY", date(2026, 10, 27))
        self.assertAlmostEqual(ratio * 22888.95, 22897.25, delta=0.05)
        ratio = india.forward_ratio(self.r21, "NIFTY", date(2026, 10, 27))
        self.assertAlmostEqual(ratio * 22811.35, 22739.94, delta=0.05)

    def test_black76_gives_calls_and_puts_one_iv(self):
        # spot pricing splits them by 9 points on 21 Oct; on the forward they agree
        ratio = india.forward_ratio(self.r21, "NIFTY", date(2026, 10, 27))
        ivs = []
        for right in ("CE", "PE"):
            r = india.option_row(self.r21, "NIFTY", date(2026, 10, 27), 23100, right)
            ivs.append(india.option_iv(right, r["close"], r["underlying"], ratio, 23100, 6 / 365.0))
        self.assertLess(abs(ivs[0] - ivs[1]), 0.005, ivs)

    def test_the_example_premium(self):
        ratio = india.forward_ratio(self.r19, "NIFTY", date(2026, 10, 27))
        r = india.option_row(self.r19, "NIFTY", date(2026, 10, 27), 23100, "CE")
        v = india.option_iv("CE", r["close"], r["underlying"], ratio, 23100, 8 / 365.0)
        self.assertAlmostEqual(v, 0.1250, delta=0.0001)
        years = india.years_to_expiry(datetime(2026, 10, 21, 9, 35, tzinfo=india.IST), date(2026, 10, 27))
        p = india.option_price("CE", 23115.65, ratio, 23100, years, v)
        self.assertEqual(india.tick_round(p), 162.95)

    def test_opt_costs_match_zerodha(self):
        with open(os.path.join(P3DIR, "zerodha-calc-opt.json")) as fh:
            for r in json.load(fh)["rows"]:
                self.assertAlmostEqual(india.round_trip("opt", r["qty"], r["buy"], r["sell"])["total"], r["total"],
                                       delta=0.01)
