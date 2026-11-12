import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

ticket = load("bharat-broker", "ticket")
check = load("bharat-risk", "check")
india = load("bharat-broker", "india")

CFG_PATH = os.path.join(ROOT, "bharat-budget", "config.example.json")
ORDERS_PATH = os.path.join(FIX, "bharat-risk", "orders-reliance.json")
PLAN_PATH = os.path.join(FIX, "bharat-budget", "plan-reliance.json")
NOW = "2026-10-19 09:51"
TODAY = "2026-10-19"
REL_CALL = {"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "side": "buy", "qty": "73", "type": "market",
            "time_in_force": "day", "order_class": "bracket", "take_profit_limit_price": "1240.25",
            "stop_loss_stop_price": "1215.35"}
REL_PLAIN = ("BUY 73 RELIANCE, market, good for today / stop-loss 1215.35 / take-profit 1240.25 / max loss about "
             "Rs 987.73 with costs if the stop fills at 1215.35 (it can fill worse in a fast market or a band freeze)")
KITE_ENTRY = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "product": "MIS",
              "validity": "DAY", "transaction_type": "BUY", "quantity": 73, "order_type": "MARKET",
              "market_protection": -1}
KITE_STOP = {"variety": "regular", "exchange": "NSE", "tradingsymbol": "RELIANCE", "product": "MIS",
             "validity": "DAY", "transaction_type": "SELL", "quantity": 73, "order_type": "SL-M",
             "trigger_price": 1215.35, "market_protection": -1}


def rel(**over):
    with open(ORDERS_PATH) as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ticket.main(argv)
    return code, out.getvalue(), err.getvalue()


class ExplicitYes(unittest.TestCase):
    def yes(self, reply, order=None, batch=("RELIANCE", "TCS")):
        return ticket.parse_yes(reply, order or rel(), batch)[0]

    def test_plain_yes_counts(self):
        for r in ("yes", "Yes.", "YES", "y", "yes place it", "Yes, buy it", "yes RELIANCE", "yes 73 RELIANCE",
                  "yes 1215.35", "yes max loss 987.73", "yes target 1240.25"):
            self.assertTrue(self.yes(r), r)

    def test_anything_else_does_not(self):
        for r in ("", "sure", "ok", "okay", "go", "do it", "yeah", "haan", "theek hai", "send it",
                  "yes to all", "yes, all of them", "yes both", "yes TCS", "yes 74", "yes but stop at 1210",
                  "yes, stop 1215.35", "yes?", "no", "not yes", "yes, wait", "yes if it is under 1230"):
            self.assertFalse(self.yes(r), r)

    def test_every_number_in_the_yes_must_be_this_orders(self):
        # 73 is the qty, but 1300 is not the limit, stop, target or max loss
        for r in ("yes 73 at 1300", "yes 73 shares, and 5000 more", "yes 73 at 1227.85"):
            self.assertFalse(self.yes(r, order=rel(type="limit", limit_price="1227.80")), r)
        self.assertTrue(self.yes("yes 73 at 1227.80", order=rel(type="limit", limit_price="1227.80")))

    def test_a_yes_naming_a_stock_outside_the_batch(self):
        for r in ("yes TCS", "yes tcs", "yes buy INFY", "yes, go for HDFCBANK"):
            ok, why = ticket.parse_yes(r, rel(), ["RELIANCE"])
            self.assertFalse(ok, r)
        self.assertIn("tcs", ticket.parse_yes("yes tcs", rel(), ["RELIANCE"])[1].lower())

    def test_a_yes_for_the_other_side(self):
        self.assertFalse(self.yes("yes sell it"))                                # this order is a buy
        self.assertTrue(self.yes("yes sell 73", order=rel(side="sell")))

    def test_a_yes_naming_its_own_fno_contract(self):
        opt = {"symbol": "NIFTY26NOV24000CE", "exchange": "NFO", "side": "buy", "qty": "65", "type": "limit",
               "limit_price": "120.50", "time_in_force": "day"}
        for r in ("yes NIFTY26NOV24000CE", "yes 65 NIFTY26NOV24000CE at 120.50", "yes buy nifty26nov24000ce"):
            self.assertTrue(ticket.parse_yes(r, opt, ["NIFTY26NOV24000CE"])[0], r)
        self.assertFalse(ticket.parse_yes("yes NIFTY26NOV24100CE", opt, ["NIFTY26NOV24000CE"])[0])

    def test_a_count_in_words_is_a_number_too(self):
        # "one share" is 1, not this order's 73; "this one" points at the order
        for r in ("yes one share", "yes, one lot", "yes buy one"):
            self.assertFalse(self.yes(r), r)
        for r in ("yes this one", "yes, that one, place it"):
            self.assertTrue(self.yes(r), r)
        self.assertTrue(self.yes("yes one share", order=rel(qty="1")))
        self.assertTrue(self.yes("yes 360ONE", order=rel(symbol="360ONE"), batch=("360ONE",)))   # its own name

    def test_a_yes_for_another_order_type(self):
        lim = rel(type="limit", limit_price="1227.80")
        for r, o in (("yes at market", lim), ("yes, market order", lim), ("yes at limit", rel()),
                     ("yes limit price", rel())):
            ok, why = ticket.parse_yes(r, o, ["RELIANCE"])
            self.assertFalse(ok, (r, o["type"]))
            self.assertIn(o["type"], why)
        self.assertTrue(self.yes("yes at market", order=rel()))
        self.assertTrue(self.yes("yes limit 1227.80", order=lim))

    def test_every_number_the_ticket_shows_can_be_repeated(self):
        stop = rel(type="stop", stop_price="1250")
        stop.pop("order_class", None)
        for k in ("take_profit_limit_price", "stop_loss_stop_price"):
            stop.pop(k, None)
        self.assertIn("1,250", ticket.plain(stop))
        self.assertTrue(self.yes("yes 73 at 1250", order=stop))
        fut = {"symbol": "NIFTY26NOVFUT", "exchange": "NFO", "side": "buy", "qty": "150", "lot": 75, "lots": 2,
               "type": "limit", "limit_price": "24000.00", "time_in_force": "day"}
        self.assertTrue(ticket.parse_yes("yes 2 lots at 24000", fut, ["NIFTY26NOVFUT"])[0])
        for r in ("yes 3 lots", "yes 75"):          # a lot's size is not this order's amount: 75 of 150 units
            self.assertFalse(ticket.parse_yes(r, fut, ["NIFTY26NOVFUT"])[0], r)


class TicketShape(unittest.TestCase):
    def test_the_order_fields_leave_out_why_and_plan(self):
        self.assertEqual(ticket.tool_input(rel()), REL_CALL)
        self.assertEqual(ticket.fingerprint(rel()), ticket.fingerprint(REL_CALL))
        self.assertEqual(ticket.fingerprint(rel()), check.fingerprint(rel()))

    def test_kite_calls_are_the_entry_then_one_stop(self):
        calls = ticket.kite_calls(rel())
        self.assertEqual([(c["when"], c["tool"]) for c in calls],
                         [("now", "place_order"), ("after the entry fills", "place_order")])
        self.assertEqual((calls[0]["input"], calls[1]["input"]), (KITE_ENTRY, KITE_STOP))
        # the target is never sent as a second exit order: two exits could both fill and leave you short
        self.assertNotIn(1240.25, [c["input"].get("price") for c in calls])
        lim = ticket.kite_calls(rel(type="limit", limit_price="1227.80"))[0]["input"]
        self.assertEqual((lim["order_type"], lim["price"]), ("LIMIT", 1227.8))
        self.assertNotIn("market_protection", lim)  # a limit order has its own price protection
        flat = {"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "side": "sell", "qty": "73",
                "type": "market", "time_in_force": "day"}
        self.assertEqual(len(ticket.kite_calls(flat)), 1)

    def test_plain_english_and_max_loss_with_costs(self):
        self.assertEqual(ticket.plain(rel()), REL_PLAIN)
        self.assertEqual(ticket.plain({"symbol": "TCS", "side": "sell", "qty": "1", "type": "limit",
                                       "limit_price": "2500", "time_in_force": "day"}),
                         "SELL 1 share of TCS, limit order at Rs 2,500, good for today")
        lim = rel(type="limit", limit_price="1227.00")
        cost = india.round_trip("eq_intraday", 73, 1227.0, 1215.35)["total"]
        self.assertEqual(ticket.max_loss(lim), (round(73 * 11.65 + cost, 2), 1215.35))
        no_plan = dict(REL_CALL)
        self.assertEqual(ticket.max_loss(no_plan), (None, 1215.35))


class KiteKeyFile(unittest.TestCase):
    def example(self):
        with open(os.path.join(ROOT, "bharat-broker", "kite-mcp-server.env.example")) as fh:
            lines = fh.read().splitlines()
        note = " ".join(x.lstrip("# ") for x in lines if x.startswith("#"))
        return note, dict(x.split("=", 1) for x in lines if x and not x.startswith("#"))

    def test_example_is_one_shared_self_hosted_kite_server_with_placeholder_keys(self):
        note, env = self.example()
        for k in ("KITE_API_KEY", "KITE_API_SECRET"):
            self.assertIn("NEVER_IN_CHAT", env[k])
        self.assertEqual(set(env["EXCLUDED_TOOLS"].split(",")),   # GTT orders are orders too
                         {"place_order", "modify_order", "cancel_order", "place_gtt_order", "modify_gtt_order",
                          "delete_gtt_order"})
        for words in ("self-hosted", "static IP", "Needs approval", "APP_MODE=http",
                      "http://127.0.0.1:8080/callback", "never stdio"):
            self.assertIn(words, note)

    def test_keys_go_in_the_key_file_never_in_the_example(self):
        # the example is tracked by git: a key typed into it ships with the next commit
        note, env = self.example()
        for k in ("KITE_API_KEY", "KITE_API_SECRET"):
            self.assertIn("NOT_HERE", env[k],
                          "a real key in the tracked example? put the placeholder back and regenerate the key")
        for words in ("Do not fill in this file", "~/.config/kite-mcp-server/env"):
            self.assertIn(words, note)
        with open(os.path.join(ROOT, ".gitignore")) as fh:
            ignored = fh.read().splitlines()
        for name in ("claude_desktop_config.json", ".env", ".mcp.json", "kite-mcp-server.env"):
            self.assertIn(name, ignored)


import example_values as EX  # noqa: E402
FUT = {"symbol": EX.FUT_CONTRACT, "exchange": "NFO", "product": "MIS", "side": "buy", "qty": "65", "type": "market",
       "time_in_force": "day", "order_class": "bracket", "take_profit_limit_price": "23203.60",
       "stop_loss_stop_price": "23040.30", "instrument": "fut", "underlying": "NIFTY", "lots": 1, "lot": 65,
       "expiry": EX.FUT_EXPIRY}


class Futures(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.pf = os.path.join(self.d, "portfolio.json")
        self.orders = os.path.join(self.d, "orders.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_the_two_kite_calls_on_nfo(self):
        calls = ticket.kite_calls(FUT)
        base = {"variety": "regular", "exchange": "NFO", "tradingsymbol": EX.FUT_CONTRACT, "product": "MIS",
                "validity": "DAY", "quantity": 65, "market_protection": -1}
        self.assertEqual(calls[0]["input"], dict(base, transaction_type="BUY", order_type="MARKET"))
        self.assertEqual(calls[1]["input"], dict(base, transaction_type="SELL", order_type="SL-M",
                                                 trigger_price=23040.3))

    def test_paper_round_trip_pays_futures_charges(self):
        pf = ticket.apply_fill(self.pf, {"budget": 200000}, EX.FUT_CONTRACT, "buy", 65, EX.FUT_PRICE, EX.FUT_DATE)
        self.assertEqual(pf["cash"], round(200000 - india.leg_costs("fut", "buy", 65, EX.FUT_PRICE)["total"], 2))
        pf = ticket.apply_fill(self.pf, {"budget": 200000}, EX.FUT_CONTRACT, "sell", 65, EX.FUT_TARGET, EX.FUT_DATE)
        legs = (india.leg_costs("fut", "buy", 65, EX.FUT_PRICE)["total"]
                + india.leg_costs("fut", "sell", 65, EX.FUT_TARGET)["total"])
        self.assertEqual(pf["cash"], round(200000 + 65 * (EX.FUT_TARGET - EX.FUT_PRICE) - legs, 2))
        self.assertEqual(pf["positions"], [])

    def test_flatten_a_futures_position(self):
        ticket.apply_fill(self.pf, {"budget": 200000}, EX.FUT_CONTRACT, "buy", 65, EX.FUT_PRICE, EX.FUT_DATE)
        plan = os.path.join(self.d, "plan.json")
        with open(plan, "w") as fh:
            json.dump({"symbol": "NIFTY", "contract": EX.FUT_CONTRACT, "lot": 65, "instrument": "fut",
                       "flat_by": "15:15"}, fh)
        code, text, err = run(["flatten", "--symbol", EX.FUT_CONTRACT, "--orders", self.orders, "--portfolio",
                               self.pf, "--plan", plan, "--now", "2026-10-21 15:15"])
        self.assertEqual(code, 0, err)
        with open(self.orders) as fh:
            o = json.load(fh)["orders"][0]
        self.assertEqual((o["exchange"], o["qty"], o["instrument"], o["lot"]), ("NFO", "65", "fut", 65))
        r = check.check([o], {"budget": 200000, "allow_fno": True}, now=check.parse_now("2026-10-21 15:15"),
                        portfolio={"cash": 50000, "positions": [{"ticker": EX.FUT_CONTRACT, "shares": 65}]})
        self.assertEqual(r["results"][0]["verdict"], "PASS", r["results"][0])

    def flatten(self, *extra):
        return run(["flatten", "--orders", self.orders, "--portfolio", self.pf, "--plan", os.path.join(self.d, "no"),
                    "--config", os.path.join(self.d, "no"), "--now", "2026-11-04 15:15"] + list(extra))

    def risk_on_the_exit(self, sym, qty):
        with open(self.orders) as fh:
            o = json.load(fh)["orders"][0]
        r = check.check([o], {"budget": 200000, "allow_fno": True}, now=check.parse_now("2026-11-04 15:15"),
                        portfolio={"cash": 50000, "positions": [{"ticker": sym, "shares": qty}]})
        return o, r["results"][0]

    def test_flatten_a_futures_name_with_digits(self):
        # bharat-risk reads NIFTYNXT50 and 360ONE futures (digits in the name): the flatten ticket must go to NFO
        for sym, qty in (("NIFTYNXT5026NOVFUT", "25"), ("360ONE26NOVFUT", "500")):
            code, _, err = self.flatten("--symbol", sym, "--qty", qty)
            self.assertEqual(code, 0, err)
            o, res = self.risk_on_the_exit(sym, float(qty))
            self.assertEqual((o["exchange"], o.get("instrument")), ("NFO", "fut"), sym)
            self.assertEqual((res["verdict"], res["kind"]), ("PASS", "exit"), res)

    def test_paper_fill_of_a_futures_name_with_digits_pays_only_the_charges(self):
        pf = ticket.apply_fill(self.pf, {"budget": 200000}, "360ONE26NOVFUT", "buy", 500, 1200.0, "2026-11-04")
        self.assertEqual(pf["cash"], round(200000 - india.leg_costs("fut", "buy", 500, 1200.0)["total"], 2))

    def test_flatten_an_option_name_with_digits_goes_to_nfo_at_a_limit(self):
        code, _, err = self.flatten("--symbol", "360ONE26NOV1200CE", "--qty", "500")
        self.assertEqual(code, 1)
        self.assertIn("--limit PRICE", err)
        code, _, err = self.flatten("--symbol", "360ONE26NOV1200CE", "--qty", "500", "--limit", "12.30")
        self.assertEqual(code, 0, err)
        o, res = self.risk_on_the_exit("360ONE26NOV1200CE", 500.0)
        self.assertEqual((o["exchange"], o.get("instrument"), o["type"], o["limit_price"]),
                         ("NFO", "opt", "limit", "12.30"))
        self.assertEqual((res["verdict"], res["kind"]), ("PASS", "exit"), res)


class Flow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.orders = os.path.join(self.d, "orders.json")
        shutil.copy(ORDERS_PATH, self.orders)
        self.cfg = os.path.join(self.d, "config.json")
        shutil.copy(CFG_PATH, self.cfg)
        self.risk = os.path.join(self.d, "state", "risk.json")
        self.state = os.path.join(self.d, "state", "broker.json")
        self.pf = os.path.join(self.d, "portfolio.json")
        self.none = os.path.join(self.d, "none")

    def tearDown(self):
        shutil.rmtree(self.d)

    def args(self, *extra, **kw):
        return list(extra) + ["--orders", self.orders, "--config", self.cfg, "--risk", self.risk,
                              "--state", self.state, "--now", kw.get("now", NOW)]

    def run_risk(self, now=NOW):
        with redirect_stdout(io.StringIO()):
            return check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", self.pf,
                               "--prices", self.none, "--state", self.state, "--entry", self.none,
                               "--now", now, "--out", self.risk])

    def state_doc(self):
        with open(self.state) as fh:
            return json.load(fh)

    def set_cfg(self, **over):
        with open(self.cfg) as fh:
            c = json.load(fh)
        c.update(over)
        with open(self.cfg, "w") as fh:
            json.dump(c, fh)

    def test_nothing_is_ready_before_the_risk_check(self):
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("NOT READY", text)
        self.assertNotIn("Reply \"yes\"", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertIn("not ready", err)

    def test_the_paper_flow(self):
        self.assertEqual(self.run_risk(), 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 0)
        self.assertIn("ORDER 1 of 1, PAPER ledger (no broker)", text)
        for part in REL_PLAIN.split(" / "):
            self.assertIn("  " + part + "\n", text)
        self.assertIn("costs (Zerodha, round trip): Rs 78.88 at the stop, Rs 79.94 at the target", text)
        self.assertIn("READY: bharat-risk PASS", text)
        self.assertNotIn("place_order", text)

        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "sure"))
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.state))
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 0)
        self.assertIn("Paper: no broker call", text)
        self.assertIn("--filled-qty 73 --filled-price P", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertIn("APPROVED", err)

        code, text, _ = run(self.args("result", "--index", "1", "--status", "placed", "--filled-qty", "73",
                                      "--filled-price", "1228.10", "--portfolio", self.pf, now="2026-10-19 09:55"))
        self.assertEqual(code, 0)
        self.assertIn("charges Rs 29.95). Cash now Rs 1,10,318.75.", text)
        st = self.state_doc()
        self.assertEqual([r["status"] for r in st["orders"]], ["approved", "placed", "filled"])
        appr = st["orders"][0]
        self.assertEqual((appr["reply"], appr["kind"], appr["max_loss"], appr["date"], appr["broker"]),
                         ("yes", "entry", 987.73, TODAY, "paper ledger"))
        self.assertEqual(appr["at"], "2026-10-19T09:51:00+05:30")
        with open(self.pf) as fh:
            pf = json.load(fh)
        # 2,00,000 - 73 x 1228.10 - 29.95 charges on the buy
        self.assertEqual((pf["cash"], pf["positions"][0]["shares"], pf["charges"]), (110318.75, 73, {TODAY: 29.95}))
        code, text, _ = run(["log", "--state", self.state, "--now", NOW])
        self.assertIn("max loss Rs 987.73", text)
        res = check.check([rel()], {"budget": 200000}, state=st, now=check.parse_now("2026-10-19 10:30"))
        self.assertIn("already placed today", " ".join(res["results"][0]["reasons"]))

    def test_flatten_by_15_15(self):
        self.run_risk()
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        run(self.args("result", "--index", "1", "--status", "placed", "--filled-qty", "73",
                      "--filled-price", "1228.10", "--portfolio", self.pf))
        late = "2026-10-19 15:15"
        code, text, _ = run(["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf,
                             "--plan", PLAN_PATH, "--config", self.cfg, "--now", late])
        self.assertEqual(code, 0)
        self.assertIn("SELL 73 shares of RELIANCE, market order, good for today (flat by 15:15 IST)", text)
        self.assertIn("cancel it FIRST", text)
        with open(self.orders) as fh:
            self.assertEqual(ticket.tool_input(json.load(fh)["orders"][0]),
                             {"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "side": "sell", "qty": "73",
                              "type": "market", "time_in_force": "day"})
        self.assertEqual(self.run_risk(now=late), 0)        # after the 14:45 entry cutoff: an exit still passes
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes sell 73", now=late))[0], 0)
        code, _, _ = run(self.args("result", "--index", "1", "--status", "placed", "--filled-qty", "73",
                                   "--filled-price", "1225.00", "--portfolio", self.pf, now="2026-10-19 15:16"))
        with open(self.pf) as fh:
            pf = json.load(fh)
        sell_charges = india.leg_costs("eq_intraday", "sell", 73, 1225.0)["total"]
        self.assertEqual(sell_charges, 48.95)
        self.assertEqual((pf["cash"], pf["positions"]), (round(110318.75 + 73 * 1225.0 - 48.95, 2), []))
        code, _, err = run(["flatten", "--symbol", "RELIANCE", "--orders", self.orders, "--portfolio", self.pf,
                            "--now", late])
        self.assertIn("nothing to flatten", err)

    def test_live_needs_the_static_ip(self):
        # live refuses --now, so the real clock is pinned here instead
        self.set_cfg(paper=False)
        old_t, old_c = ticket.parse_now, check.parse_now
        ticket.parse_now = lambda text: old_t(text or NOW)
        check.parse_now = lambda text: old_c(text or NOW)
        self.addCleanup(setattr, ticket, "parse_now", old_t)
        self.addCleanup(setattr, check, "parse_now", old_c)
        self.args = lambda *extra, **kw: list(extra) + ["--orders", self.orders, "--config", self.cfg,
                                                        "--risk", self.risk, "--state", self.state]
        with redirect_stdout(io.StringIO()):
            check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", self.pf,
                        "--prices", self.none, "--state", self.state, "--entry", self.none, "--out", self.risk])
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 1)
        self.assertIn("ORDER 1 of 1, LIVE ZERODHA ACCOUNT, REAL MONEY", text)
        self.assertIn("NOT READY: live API orders must come from a static IP registered in Kite", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.set_cfg(paper=False, static_ip_registered=True)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 0)
        self.assertIn('"order_type": "SL-M"', text)
        self.assertIn("Allow once", text)
        code, text, _ = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 0)
        self.assertIn("Now call the Kite MCP place_order with exactly:", text)
        self.assertIn('"trigger_price": 1215.35', text)
        self.assertIn("Zerodha only", ticket.live_problem({"paper": False, "broker": "upstox",
                                                          "static_ip_registered": True}))
        self.assertIsNone(ticket.live_problem({"paper": True}))

    def test_an_edited_order_is_not_ready(self):
        self.run_risk()
        with open(self.orders) as fh:
            doc = json.load(fh)
        doc["orders"][0]["qty"] = "74"
        with open(self.orders, "w") as fh:
            json.dump(doc, fh)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("was not in the last risk check", text)

    def test_yesterdays_check_does_not_count(self):
        self.run_risk(now="2026-10-16 09:51")
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("not today", text)

    def test_blocked_orders_are_never_ready(self):
        self.assertEqual(self.run_risk(now="2026-10-19 09:20"), 1)
        code, text, _ = run(self.args("show", "--index", "1", now="2026-10-19 09:20"))
        self.assertIn("BLOCKED by bharat-risk: window: no new entries before 09:30", text)
        self.assertEqual(run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-10-19 09:20"))[0], 1)

    def test_result_needs_an_approval_and_decline_is_logged(self):
        self.run_risk()
        code, _, err = run(self.args("result", "--index", "1", "--status", "placed"))
        self.assertIn("no yes recorded", err)
        run(self.args("decline", "--index", "1", "--reply", "no thanks"))
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertIn("DONE: declined", text)

    def test_fill_sell_relieves_the_oldest_lot_and_pays_charges(self):
        pf = {"cash": 0, "positions": [{"ticker": "ITC", "shares": 300, "lots": [
            {"id": "ITC-1", "date": "2026-10-16", "shares": 200, "cost": 54000.0},
            {"id": "ITC-2", "date": "2026-10-19", "shares": 100, "cost": 27500.0}]}]}
        with open(self.pf, "w") as fh:
            json.dump(pf, fh)
        code, text, _ = run(["fill", "--symbol", "ITC", "--side", "sell", "--qty", "250", "--price", "275",
                             "--date", TODAY, "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 0)
        with open(self.pf) as fh:
            pf = json.load(fh)
        charges = india.leg_costs("eq_intraday", "sell", 250, 275.0)["total"]
        self.assertEqual(pf["cash"], round(250 * 275.0 - charges, 2))
        self.assertEqual([(l["id"], l["shares"], l["cost"]) for l in pf["positions"][0]["lots"]],
                         [("ITC-2", 50, 13750.0)])
        code, _, err = run(["fill", "--symbol", "ITC", "--side", "sell", "--qty", "500", "--price", "275",
                            "--portfolio", self.pf, "--config", self.cfg, "--state", self.state])
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()


class ReviewFixes(unittest.TestCase):
    def setUp(self):
        Flow.setUp(self)

    def tearDown(self):
        Flow.tearDown(self)

    args = Flow.args
    run_risk = Flow.run_risk
    state_doc = Flow.state_doc
    set_cfg = Flow.set_cfg

    def test_the_log_keeps_the_loss_risk_checked(self):
        prices = os.path.join(self.d, "prices.json")
        with open(prices, "w") as fh:
            json.dump({"as_of": "2026-10-19T04:20:30Z", "prices": {"RELIANCE": 1229.0}}, fh)
        self.set_cfg(min_reward_risk=0.5, max_loss_per_trade_inr=1500)
        with redirect_stdout(io.StringIO()):
            check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", self.pf, "--prices", prices,
                        "--state", self.state, "--entry", self.none, "--now", NOW, "--out", self.risk])
        with open(self.risk) as fh:
            checked = json.load(fh)["results"][0]["max_loss"]
        self.assertGreater(checked, 987.73)                     # measured from the fresher, higher price
        run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(self.state_doc()["orders"][0]["max_loss"], checked)

    def test_a_stale_pass_is_not_a_yes(self):
        self.run_risk()
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-10-19 10:20"))
        self.assertEqual(code, 1)
        self.assertIn("the risk check is 29 minutes old", err)
        self.run_risk(now="2026-10-19 14:44")
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes", now="2026-10-19 14:50"))
        self.assertEqual(code, 1)
        self.assertIn("no new entries after 14:45 IST", err)

    def test_live_refuses_a_pinned_clock(self):
        self.set_cfg(paper=False, static_ip_registered=True)
        code, _, err = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("--now and --today are for tests and paper only", err)

    def test_a_yes_naming_another_symbol(self):
        o = rel()
        for r in ("yes HDFCBANK", "yes M&M", "yes BAJAJ-AUTO"):
            self.assertFalse(ticket.parse_yes(r, o, ["RELIANCE", "HDFCBANK", "M&M", "BAJAJ-AUTO"])[0], r)
        self.assertTrue(ticket.parse_yes("yes RELIANCE", o, ["RELIANCE", "HDFCBANK"])[0])

    def test_the_sl_m_step_says_what_to_do(self):
        self.set_cfg(paper=False, static_ip_registered=True)
        old = ticket.parse_now
        ticket.parse_now = lambda text: old(text or NOW)
        old_c = check.parse_now
        check.parse_now = lambda text: old_c(text or NOW)
        try:
            with redirect_stdout(io.StringIO()):
                check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", self.pf, "--prices",
                            self.none, "--state", self.state, "--entry", self.none, "--out", self.risk])
            code, text, err = run(["approve", "--index", "1", "--reply", "yes", "--orders", self.orders, "--config",
                                   self.cfg, "--risk", self.risk, "--state", self.state])
        finally:
            ticket.parse_now, check.parse_now = old, old_c
        self.assertEqual(code, 0, err)
        self.assertIn("quantity = the FILLED quantity", text)
        self.assertIn("If the stop is rejected or denied, the position has no stop: flatten it now", text)


def _opt_order(**over):
    size = load("bharat-budget", "size")
    import test_budget as TB
    o = size.bracket_order(size.size_trade(dict(TB.OPT_PLAN, premium_source="user"), TB.opt_cfg(),
                                           today=EX.OPT_DATE))
    o.update(over)
    return o


class Options(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.pf = os.path.join(self.d, "portfolio.json")
        self.orders = os.path.join(self.d, "orders.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_a_limit_buy_then_a_stop_limit(self):
        o = _opt_order()
        self.assertEqual(o["stop_loss_limit_price"], "122.85")        # 10 ticks under the 123.35 trigger
        calls = ticket.kite_calls(o)
        base = {"variety": "regular", "exchange": "NFO", "tradingsymbol": EX.OPT_CONTRACT, "product": "MIS",
                "validity": "DAY", "quantity": 65}
        self.assertEqual(calls[0]["input"], dict(base, transaction_type="BUY", order_type="LIMIT", price=162.95))
        self.assertEqual(calls[1]["input"], dict(base, transaction_type="SELL", order_type="SL", trigger_price=123.35,
                                                 price=122.85))

    def test_the_ticket_says_a_stop_limit_may_not_fill(self):
        self.assertIn("may not fill in a fast move", "\n".join(ticket.plain_parts(_opt_order())))

    def test_the_stop_limit_floor_is_one_tick(self):
        size = load("bharat-budget", "size")
        import test_budget as TB
        p = dict(TB.OPT_PLAN, premium_source="user", entry=3.0, stop=0.30, target=6.0)
        o = size.bracket_order(size.size_trade(p, TB.opt_cfg(min_reward_risk=None), today=EX.OPT_DATE))
        self.assertEqual(o["stop_loss_limit_price"], "0.05")

    def test_paper_round_trip_pays_the_premium_and_option_charges(self):
        pf = ticket.apply_fill(self.pf, {"budget": 200000}, EX.OPT_CONTRACT, "buy", 65, EX.OPT_PREMIUM, EX.OPT_DATE)
        buy = india.leg_costs("opt", "buy", 65, EX.OPT_PREMIUM)["total"]
        self.assertEqual(pf["cash"], round(200000 - 65 * EX.OPT_PREMIUM - buy, 2))
        pf = ticket.apply_fill(self.pf, {"budget": 200000}, EX.OPT_CONTRACT, "sell", 65, EX.OPT_TARGET, EX.OPT_DATE)
        sell = india.leg_costs("opt", "sell", 65, EX.OPT_TARGET)["total"]
        self.assertEqual(pf["cash"], round(200000 + 65 * (EX.OPT_TARGET - EX.OPT_PREMIUM) - buy - sell, 2))

    def test_the_max_loss_uses_option_costs(self):
        self.assertEqual(ticket.max_loss(_opt_order())[0], EX.OPT_MAX_LOSS)

    def test_flatten_rounds_the_limit_down_to_the_tick(self):
        ticket.apply_fill(self.pf, {"budget": 200000}, EX.OPT_CONTRACT, "buy", 65, EX.OPT_PREMIUM, EX.OPT_DATE)
        args = ["flatten", "--symbol", EX.OPT_CONTRACT, "--orders", self.orders, "--portfolio", self.pf,
                "--plan", os.path.join(self.d, "none.json"), "--now", "2026-10-21 15:15"]
        for given, sent, note in (("150.03", "150.00", True), ("150.00", "150.00", False)):
            code, out, err = run(args + ["--limit", given])
            self.assertEqual(code, 0, err)
            with open(self.orders) as fh:
                self.assertEqual(json.load(fh)["orders"][0]["limit_price"], sent)
            self.assertEqual("off NSE's 0.05 tick" in out, note, out)
            if note:
                self.assertIn("150.03", out)

    def test_flatten_refuses_a_limit_under_one_tick(self):
        ticket.apply_fill(self.pf, {"budget": 200000}, EX.OPT_CONTRACT, "buy", 65, EX.OPT_PREMIUM, EX.OPT_DATE)
        args = ["flatten", "--symbol", EX.OPT_CONTRACT, "--orders", self.orders, "--portfolio", self.pf,
                "--plan", os.path.join(self.d, "none.json"), "--now", "2026-10-21 15:15"]
        code, out, err = run(args + ["--limit", "0.04"])
        self.assertEqual(code, 1, out)
        self.assertIn("0.04", err)
        self.assertIn("0.05", err)
        self.assertFalse(os.path.exists(self.orders))
        code, out, err = run(args + ["--limit", "0.05"])
        self.assertEqual(code, 0, err)
        with open(self.orders) as fh:
            self.assertEqual(json.load(fh)["orders"][0]["limit_price"], "0.05")
        self.assertNotIn("off NSE's 0.05 tick", out)

    def test_flatten_refuses_a_limit_that_is_not_a_number(self):
        ticket.apply_fill(self.pf, {"budget": 200000}, EX.OPT_CONTRACT, "buy", 65, EX.OPT_PREMIUM, EX.OPT_DATE)
        args = ["flatten", "--symbol", EX.OPT_CONTRACT, "--orders", self.orders, "--portfolio", self.pf,
                "--plan", os.path.join(self.d, "none.json"), "--now", "2026-10-21 15:15"]
        for bad in ("nan", "inf", "abc"):
            code, out, err = run(args + ["--limit", bad])
            self.assertEqual(code, 1, (bad, out, err))
            self.assertIn("--limit", err, bad)
            self.assertFalse(os.path.exists(self.orders), bad)

    def test_flatten_an_option_needs_a_limit(self):
        ticket.apply_fill(self.pf, {"budget": 200000}, EX.OPT_CONTRACT, "buy", 65, EX.OPT_PREMIUM, EX.OPT_DATE)
        args = ["flatten", "--symbol", EX.OPT_CONTRACT, "--orders", self.orders, "--portfolio", self.pf,
                "--plan", os.path.join(self.d, "none.json"), "--now", "2026-10-21 15:15"]
        code, _, err = run(args)
        self.assertEqual(code, 1)
        self.assertIn("--limit", err)
        code, _, err = run(args + ["--limit", "150"])
        self.assertEqual(code, 0, err)
        with open(self.orders) as fh:
            o = json.load(fh)["orders"][0]
        self.assertEqual((o["exchange"], o["type"], o["limit_price"], o["instrument"]), ("NFO", "limit", "150.00", "opt"))
