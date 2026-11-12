import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

al = load("bharat-alerts", "alerts")

QDIR = os.path.join(FIX, "bharat-alerts")     # real Yahoo 1d charts, fetched 4 Nov 2026 (Tue Nov 3's close)
PLAN = os.path.join(FIX, "bharat-budget", "plan-reliance.json")
EXAMPLE = os.path.join(ROOT, "bharat-alerts", "alerts.example.json")
DT = datetime.datetime


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = al.main(argv)
    return code, out.getvalue(), err.getvalue()


def status(now):
    return al.market_status(al.parse_now(now))


class Calendar(unittest.TestCase):
    def test_parse_now(self):
        self.assertEqual(al.parse_now("2026-10-19 15:15"), DT(2026, 10, 19, 9, 45))
        self.assertEqual(al.parse_now("2026-10-19T09:45:00Z"), DT(2026, 10, 19, 9, 45))
        with self.assertRaises(al.AlertError):
            al.parse_now("teatime")

    def test_market_status(self):
        st = status("2026-10-19 09:51")
        self.assertEqual((st["open"], st["reason"], st["opens"], st["closes"]), (True, "regular session", "09:15", "15:30"))
        st = status("2026-10-19 08:00")
        self.assertEqual((st["open"], st["reason"], st["next_open"]), (False, "before the open", "2026-10-19 09:15"))
        st = status("2026-10-19 16:00")
        self.assertEqual((st["reason"], st["next_open"]), ("after the close", "2026-10-21 09:15"))   # Oct 20 Dussehra
        st = status("2026-10-20 10:00")
        self.assertEqual(st["reason"], "NSE holiday: Dussehra")
        st = status("2026-11-08 14:30")
        self.assertEqual((st["open"], st["reason"], st["opens"]), (True, "Muhurat session", "14:20"))
        self.assertFalse(status("2028-01-03 10:00")["calendar_known"])


class Quotes(unittest.TestCase):
    def test_yahoo_fixtures(self):
        q, errors = al.get_quotes(["RELIANCE", "TCS", "INFY"], offline=QDIR)
        self.assertEqual(errors, [])
        self.assertEqual((q["RELIANCE"]["price"], q["RELIANCE"]["prev_close"], q["RELIANCE"]["currency"]),
                         (1307.9, 1300.0, "INR"))
        self.assertEqual(q["RELIANCE"]["time"], "2026-11-03T09:59:39Z")   # 15:29:39 IST

    def test_urls_and_user_agent(self):
        self.assertEqual(al.yahoo_url("reliance"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=1d")
        self.assertIn("/chart/M%26M.NS?", al.yahoo_url("M&M"))
        self.assertIn("/chart/^NSEI?", al.yahoo_url("NIFTY"))
        self.assertEqual(al.UA, "Mozilla/5.0 (compatible; india-trader-skill/0.1)")

    def test_offline_errors(self):
        q, errors = al.get_quotes(["ZZZZ"], offline=QDIR)
        self.assertEqual(q, {})
        self.assertIn("no offline quote file", errors[0])


class FakeNet(object):
    def __init__(self, codes, body):
        self.codes, self.body, self.urls, self.sleeps = list(codes), body, [], []

    def urlopen(self, req, timeout=20):
        self.urls.append(req.full_url)
        code = self.codes.pop(0)
        if code != 200:
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(b"{}"))
        return io.BytesIO(self.body.encode("utf-8"))


class Network(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(QDIR, "yahoo-RELIANCE.json")) as fh:
            self.body = fh.read()
        self._o, self._s = al._urlopen, al._sleep

    def tearDown(self):
        al._urlopen, al._sleep = self._o, self._s

    def net(self, codes):
        n = FakeNet(codes, self.body)
        al._urlopen, al._sleep = n.urlopen, n.sleeps.append
        return n

    def test_one_retry_then_query1(self):
        n = self.net([429, 429, 200])
        self.assertEqual(al.get_quotes(["RELIANCE"])[0]["RELIANCE"]["price"], 1307.9)
        self.assertEqual([u.split("/")[2] for u in n.urls],
                         ["query2.finance.yahoo.com", "query2.finance.yahoo.com", "query1.finance.yahoo.com"])
        self.assertEqual(n.sleeps, [al.BACKOFF])


class Rules(unittest.TestCase):
    def ev(self, rules, price, now, state=None, prev=None):
        q = {"RELIANCE": {"price": price, "prev_close": prev or 1211.2}}
        return al.evaluate(rules, q, status(now), state or {}, al.parse_now(now))

    def test_the_plan_gives_a_stop_a_target_and_flat_by(self):
        with open(PLAN) as fh:
            rules = al.rules_from_plan(json.load(fh))
        self.assertEqual([(r["id"], r["type"], r.get("price"), r.get("at")) for r in rules],
                         [("reliance-plan-stop", "stop", 1215.35, None), ("reliance-plan-target", "target", 1240.25, None),
                          ("reliance-flat-by", "time", None, "15:15")])
        self.assertTrue(all(r["date"] == "2026-10-19" for r in rules))
        rings, st = self.ev(rules, 1241.0, "2026-10-19 11:30")
        self.assertEqual([r["id"] for r in rings], ["reliance-plan-target"])
        self.assertEqual(rings[0]["message"], "RELIANCE is Rs 1241.00: at or above your target Rs 1240.25 (plan target: "
                                              "1R (one times the risk)).")
        rings, st = self.ev(rules, 1239.0, "2026-10-19 11:35", st)
        self.assertEqual(rings, [])                       # rang once
        rings, st = self.ev(rules, 1239.0, "2026-10-19 15:15", st)
        self.assertEqual([r["id"] for r in rings], ["reliance-flat-by"])
        self.assertIn("Flat by 15:15: it is 15:15 IST (set for 15:15), the market closes at 15:30.", rings[0]["message"])
        self.assertIn("before the broker's 15:20 square-off", rings[0]["message"])
        self.assertEqual(self.ev(rules, 1200.0, "2026-10-21 11:00")[0], [])   # another day: the plan's rules sleep

    def test_stop_and_rearm(self):
        rule = [{"id": "s", "symbol": "RELIANCE", "type": "stop", "price": 1215.35, "rearm": True}]
        rings, st = self.ev(rule, 1215.0, "2026-10-19 10:30")
        self.assertEqual(len(rings), 1)
        rings, st = self.ev(rule, 1218.0, "2026-10-19 10:35", st)
        self.assertNotIn("s", st["rung"])
        self.assertEqual(len(self.ev(rule, 1214.0, "2026-10-19 10:40", st)[0]), 1)

    def test_pct_move_from_previous_close(self):
        rule = [{"id": "m", "symbol": "RELIANCE", "type": "pct_move", "pct": 3}]
        self.assertEqual(self.ev(rule, 1240.0, "2026-10-19 10:30")[0], [])            # +2.38 %
        rings, _ = self.ev(rule, 1250.0, "2026-10-19 10:30")
        self.assertEqual(rings[0]["message"], "RELIANCE moved +3.20% to Rs 1250.00 (from Rs 1211.20).")

    def test_opening_bell_and_muhurat(self):
        bell = [{"id": "b", "type": "opening_bell"}]
        rings, st = self.ev(bell, 1, "2026-10-19 09:16")
        self.assertEqual(rings[0]["message"], "Opening bell: NSE is open (2026-10-19 09:16 IST).")
        self.assertEqual(self.ev(bell, 1, "2026-10-19 10:00", st)[0], [])
        rings, _ = self.ev(bell, 1, "2026-11-08 14:21")
        self.assertIn("the Muhurat session, 14:20 to 15:30", rings[0]["message"])
        self.assertEqual(self.ev(bell, 1, "2026-10-20 10:00")[0], [])                 # Dussehra

    def test_the_example_rules_are_valid(self):
        with open(EXAMPLE) as fh:
            for r in json.load(fh)["rules"]:
                al.validate(r)

    def test_bad_rules(self):
        for bad in ({"type": "nope"}, {"type": "stop", "symbol": "X"}, {"type": "time", "at": "25:00"},
                    {"type": "above", "price": 1}):
            with self.assertRaises(al.AlertError):
                al.validate(bad)


NOV = {"id": "nifty26novfut-expiry", "type": "expiry", "symbol": "NIFTY26NOVFUT", "expiry": "2026-11-23"}


class Expiry(unittest.TestCase):
    def ev(self, rules, now, state=None):
        return al.evaluate(rules, {}, status(now), state or {}, al.parse_now(now))

    def test_rings_the_day_before_and_on_expiry_day(self):
        rings, st = self.ev([NOV], "2026-11-20 09:20")       # Friday: the next trading day is the 23rd
        self.assertEqual([r["id"] for r in rings], ["nifty26novfut-expiry"])
        self.assertIn("expires on the next trading day", rings[0]["message"])
        self.assertEqual(self.ev([NOV], "2026-11-20 11:00", st)[0], [])   # once a day
        rings, _ = self.ev([NOV], "2026-11-23 09:15", st)
        self.assertIn("NIFTY26NOVFUT expires today", rings[0]["message"])
        self.assertEqual(self.ev([NOV], "2026-11-24 09:30")[0], [])        # holiday, and past the expiry
        self.assertEqual(self.ev([NOV], "2026-11-19 09:30")[0], [])
        self.assertEqual(self.ev([NOV], "2026-11-20 09:10")[0], [])        # before 09:15

    def test_from_plan_adds_it_for_a_futures_plan(self):
        plan = {"symbol": "NIFTY", "date": "2026-10-21", "instrument": "fut", "contract": "NIFTY26OCTFUT",
                "expiry": "2026-10-27", "entry": 23121.95, "stop": 23040.30, "target": 23203.60,
                "index": {"entry": 23115.65, "stop": 23034.05, "target": 23197.25}, "flat_by": "15:15"}
        rules = {r["id"]: r for r in al.rules_from_plan(plan)}
        e = rules["nifty26octfut-expiry"]
        self.assertEqual((e["type"], e["symbol"], e["expiry"], e.get("date")), ("expiry", "NIFTY26OCTFUT",
                                                                                 "2026-10-27", None))
        # the quotes are the index's, so the price alerts sit on the index levels
        self.assertEqual((rules["nifty-plan-stop"]["price"], rules["nifty-plan-target"]["price"]),
                         (23034.05, 23197.25))
        self.assertIn("NIFTY26OCTFUT 23040.30", rules["nifty-plan-stop"]["note"])

    def test_bad_expiry_rule(self):
        with self.assertRaises(al.AlertError):
            al.validate({"id": "x", "type": "expiry", "symbol": "NIFTY26NOVFUT", "expiry": "23 Nov"})


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.rules = os.path.join(self.d, "alerts.json")
        self.state = os.path.join(self.d, "state.json")
        self.prices = os.path.join(self.d, "prices.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_from_plan_then_check_with_real_quotes(self):
        code, text, _ = run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state,
                             "--config", os.path.join(self.d, "none.json")])
        self.assertEqual(code, 0)
        self.assertIn("Added reliance-flat-by", text)
        rules = json.load(open(self.rules))["rules"]
        for r in rules:
            r["date"] = "2026-11-03"                  # replay the rules on the fixture's day
        json.dump({"rules": rules}, open(self.rules, "w"))
        code, text, _ = run(["check", "--rules", self.rules, "--state", self.state, "--prices-out", self.prices,
                             "--offline", QDIR, "--now", "2026-11-03 15:16"])
        self.assertEqual(code, 0)
        self.assertIn("NSE OPEN (2026-11-03 15:16 IST, regular session).", text)
        self.assertIn("ALERT RELIANCE is Rs 1307.90: at or above your target Rs 1240.25", text)
        self.assertIn("ALERT Flat by 15:15: it is 15:16 IST", text)
        with open(self.prices) as fh:
            self.assertEqual(json.load(fh)["prices"]["RELIANCE"], 1307.9)
        code, text, _ = run(["check", "--rules", self.rules, "--state", self.state, "--prices-out", self.prices,
                             "--offline", QDIR, "--now", "2026-11-03 15:20"])
        self.assertEqual(code, 1)
        self.assertIn("Nothing hit", text)

    def test_quote_holdings_and_write(self):
        code, text, _ = run(["quote", "RELIANCE", "tcs", "--offline", QDIR, "--write", self.prices])
        self.assertEqual(code, 0)
        self.assertIn("RELIANCE   Rs 1307.90 (+0.61% vs prev close Rs 1300.00)", text)
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(doc["prices"]), ["RELIANCE", "TCS"])
        code, text, _ = run(["quote", "--holdings", "--plan", PLAN, "--portfolio", os.path.join(self.d, "none"),
                             "--offline", QDIR])
        self.assertIn("RELIANCE", text)

    def test_clock_exit_codes(self):
        self.assertEqual(run(["clock", "--now", "2026-10-19 10:00"])[0], 0)
        code, text, _ = run(["clock", "--now", "2026-10-20 10:00"])
        self.assertEqual(code, 1)
        self.assertIn("NSE closed, NSE holiday: Dussehra", text)
        self.assertIn("Next open: 2026-10-21 09:15 IST.", text)

    def test_check_without_rules(self):
        code, _, err = run(["check", "--rules", self.rules])
        self.assertEqual(code, 1)
        self.assertIn("no rules", err)


if __name__ == "__main__":
    unittest.main()


OPT_PLAN = {"symbol": "NIFTY", "date": "2026-10-21", "instrument": "opt", "contract": "NIFTY26OCT23100CE",
            "expiry": "2026-10-27", "right": "CE", "index_side": "up", "entry": 162.95, "stop": 123.35,
            "target": 209.55, "index_entry": 23115.65, "index_stop": 23034.05, "index_target": 23197.25,
            "flat_by": "15:15"}


class Options(unittest.TestCase):
    def ev(self, rules, quotes, now, state=None):
        return al.evaluate(rules, quotes, status(now), state or {}, al.parse_now(now))

    def test_from_plan_watches_the_index_levels_and_says_so(self):
        rules = {r["id"]: r for r in al.rules_from_plan(OPT_PLAN)}
        s, t = rules["nifty-plan-stop"], rules["nifty-plan-target"]
        self.assertEqual((s["price"], t["price"]), (23034.05, 23197.25))
        for r in (s, t):
            self.assertIn("NIFTY26OCT23100CE", r["note"])
            self.assertIn("index level from the model", r["note"])
        self.assertIn("premium 123.35", s["note"])
        rings, _ = self.ev([s], {"NIFTY": {"price": 23030.0}}, "2026-10-21 10:30")
        self.assertIn("index level from the model", rings[0]["message"])
        self.assertEqual(rules["nifty26oct23100ce-expiry-close"]["at"], "15:00")

    def test_a_put_stop_rings_when_the_index_rises(self):
        plan = dict(OPT_PLAN, contract="NIFTY26OCT23000PE", right="PE", index_side="down", index_entry=22954.25,
                    index_stop=23073.00, index_target=22835.50)
        rules = {r["id"]: r for r in al.rules_from_plan(plan)}
        rings, _ = self.ev([rules["nifty-plan-stop"]], {"NIFTY": {"price": 23080.0}}, "2026-10-21 12:30")
        self.assertEqual(len(rings), 1)
        self.assertIn("at or above your stop", rings[0]["message"])
        rings, _ = self.ev([rules["nifty-plan-stop"]], {"NIFTY": {"price": 22900.0}}, "2026-10-21 12:30")
        self.assertEqual(rings, [])
        rings, _ = self.ev([rules["nifty-plan-target"]], {"NIFTY": {"price": 22830.0}}, "2026-10-21 12:30")
        self.assertIn("at or below your target", rings[0]["message"])

    def test_the_09_15_alert_says_next_expiry_not_next_month(self):
        r = {"id": "w", "symbol": "NIFTY26O1923100CE", "type": "expiry", "expiry": "2026-10-19", "at": "09:15"}
        for now in ("2026-10-16 09:20", "2026-10-19 09:20"):
            rings, _ = self.ev([r], {}, now)
            self.assertEqual(len(rings), 1, now)
            self.assertNotIn("month", rings[0]["message"])
            self.assertIn("next expiry", rings[0]["message"])

    def test_the_15_00_expiry_alert_rings_on_expiry_day_only(self):
        r = {x["id"]: x for x in al.rules_from_plan(OPT_PLAN)}["nifty26oct23100ce-expiry-close"]
        rings, _ = self.ev([r], {}, "2026-10-27 15:00")
        self.assertEqual(len(rings), 1)
        self.assertIn("STT on its intrinsic value", rings[0]["message"])
        self.assertEqual(self.ev([r], {}, "2026-10-27 14:59")[0], [])
        self.assertEqual(self.ev([r], {}, "2026-10-26 15:00")[0], [])   # the day before: the 09:15 rule says it
