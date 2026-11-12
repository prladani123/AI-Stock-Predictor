import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

check = load("bharat-risk", "check")
ticket = load("bharat-broker", "ticket")

CFG_PATH = os.path.join(ROOT, "bharat-budget", "config.example.json")
ORDERS_PATH = os.path.join(FIX, "bharat-risk", "orders-reliance.json")
NOW = "2026-10-19 09:51"
TODAY = "2026-10-19"


def at(text):
    return check.parse_now(text)


def cfg(**over):
    with open(CFG_PATH) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def rel(**over):
    with open(ORDERS_PATH) as fh:
        o = json.load(fh)["orders"][0]
    o.update(over)
    return o


def bracket(sym, qty, entry, stop, target, **over):
    o = {"symbol": sym, "exchange": "NSE", "product": "MIS", "side": "buy", "qty": str(qty), "type": "market",
         "time_in_force": "day", "order_class": "bracket", "take_profit_limit_price": "%.2f" % target,
         "stop_loss_stop_price": "%.2f" % stop, "plan": {"entry": entry, "stop": stop, "target": target}}
    o.update(over)
    return o


def one(orders, c=None, now=NOW, **kw):
    return check.check(orders, c or cfg(), now=at(now), **kw)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = check.main(argv)
    return code, out.getvalue(), err.getvalue()


class TheExample(unittest.TestCase):
    def test_the_reliance_bracket_passes_every_check(self):
        res = one([rel()])
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["kind"], r["reasons"]), ("PASS", "entry", []))
        self.assertEqual((r["entry_ref"], r["stop"], r["target"], r["risk_per_share"], r["max_loss"], r["reward_risk"]),
                         (1227.8, 1215.35, 1240.25, 12.45, 987.73, 1.0))
        self.assertIn("costs Rs 78.88 at the stop, Rs 79.94 at the target (9% of the Rs 908.85 reward)", r["info"])
        self.assertEqual(res["cash_after"], 110370.6)
        self.assertEqual(res["now_ist"], "2026-10-19 09:51")

    def test_74_shares_is_blocked_by_the_max_loss_with_costs(self):
        r = one([rel(qty="74")])["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertEqual(len(r["reasons"]), 1)
        self.assertIn("loss: 74 shares x 12.45 risk a share + Rs 78.96 costs = Rs 1,000.26 at the stop, over your "
                      "Rs 1,000 max loss per trade", r["reasons"][0])

    def test_fingerprint_is_shared_with_the_broker(self):
        o = rel()
        self.assertEqual(check.fingerprint(o), ticket.fingerprint(o))
        self.assertNotEqual(check.fingerprint(o), check.fingerprint(rel(product="CNC")))
        self.assertEqual(check.fingerprint(o), check.fingerprint(rel(why="changed note")))   # notes do not count

    def test_cli(self):
        d = tempfile.mkdtemp()
        out = os.path.join(d, "risk.json")
        code, text, err = run(["--orders", ORDERS_PATH, "--config", CFG_PATH, "--now", NOW, "--portfolio",
                               "/nonexistent", "--prices", "/nonexistent", "--state", "/nonexistent",
                               "--entry", "/nonexistent", "--out", out])
        self.assertEqual(code, 0, err)
        self.assertIn("PASS  #1 buy RELIANCE 73 shares, entry", text)
        self.assertIn("max loss Rs 987.73 incl. costs (limit Rs 1,000), reward:risk 1.00", text)
        self.assertIn("1 passed, 0 blocked at 2026-10-19 09:51 IST", text)
        with open(out) as fh:
            self.assertEqual(json.load(fh)["results"][0]["fingerprint"], check.fingerprint(rel()))


class DayTradeChecks(unittest.TestCase):
    def test_a_stop_is_required(self):
        o = rel()
        for k in ("stop_loss_stop_price", "take_profit_limit_price", "order_class"):
            del o[k]
        o["plan"] = {"entry": 1227.8}
        r = one([o])["results"][0]
        self.assertTrue(any(x.startswith("stop: no stop") for x in r["reasons"]))

    def test_a_plan_stop_counts_but_warns(self):
        o = rel()
        for k in ("stop_loss_stop_price", "take_profit_limit_price", "order_class"):
            del o[k]
        r = one([o])["results"][0]
        self.assertEqual(r["verdict"], "PASS")
        self.assertTrue(any("in the plan only" in w for w in r["warnings"]))

    def test_stop_and_target_on_the_right_side(self):
        r = one([bracket("RELIANCE", 10, 1227.8, 1230.0, 1240.0)])["results"][0]
        self.assertTrue(any("must be below the entry" in x for x in r["reasons"]))
        r = one([bracket("RELIANCE", 10, 1227.8, 1215.35, 1220.0)])["results"][0]
        self.assertTrue(any("must be above the entry" in x for x in r["reasons"]))

    def test_reward_risk(self):
        r = one([bracket("RELIANCE", 10, 1227.8, 1215.35, 1235.0)])["results"][0]
        self.assertTrue(any(x.startswith("reward:risk: 0.58") for x in r["reasons"]))

    def test_costs_that_eat_the_reward_are_blocked(self):
        # 40 shares of a Rs 500 stock for a Rs 1 target: Rs 40 of reward, Rs 21.67 of charges (stamp 0.60 rounds to 1)
        r = one([bracket("ITC", 40, 500.0, 499.0, 501.0)])["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertEqual([x for x in r["reasons"] if x.startswith("costs:")],
                         ["costs: Rs 21.67 round-trip costs are 54% of the Rs 40 reward at the target, over your "
                          "25% limit (max_cost_share): the charges would eat most of what this trade can make"])
        self.assertEqual(one([bracket("ITC", 40, 500.0, 499.0, 501.0)], cfg(max_cost_share=0.6))["block"], 0)

    def test_a_fresh_higher_price_is_the_entry(self):
        times = {"RELIANCE": at(NOW) - datetime.timedelta(minutes=1)}
        r = one([rel()], prices={"RELIANCE": 1229.0}, price_times=times)["results"][0]
        self.assertEqual((r["entry_ref"], r["entry_ref_from"]), (1229.0, "price"))
        self.assertTrue(any(x.startswith("reward:risk") for x in r["reasons"]))

    def test_a_short_is_measured_from_the_lower_price(self):
        # a short's stop is above it: a market sell at a lower fresh price is the bigger loss
        times = {"ABC": at(NOW) - datetime.timedelta(minutes=1)}
        short = bracket("ABC", 150, 100.0, 105.0, 90.0, side="sell")
        r = one([short], cfg(allow_short=True), prices={"ABC": 98.0}, price_times=times)["results"][0]
        self.assertEqual((r["entry_ref"], r["entry_ref_from"], r["risk_per_share"]), (98.0, "price", 7.0))
        self.assertEqual(r["verdict"], "BLOCK")                     # 150 x 7 = 1,050 + costs, over the 1,000 cap
        self.assertTrue(any(x.startswith("loss:") for x in r["reasons"]), r["reasons"])
        r = one([short], cfg(allow_short=True), prices={"ABC": 102.0}, price_times=times)["results"][0]
        self.assertEqual((r["entry_ref"], r["entry_ref_from"], r["risk_per_share"]), (100.0, "plan", 5.0))

    def test_a_fresh_price_already_past_the_stop_blocks_the_entry(self):
        # the order fills at the price, on the wrong side of its stop: the stop order would be rejected
        times = {"ABC": at(NOW) - datetime.timedelta(minutes=1)}
        for o, c, price in (
                (bracket("ABC", 50, 100.0, 105.0, 90.0, side="sell"), cfg(allow_short=True), 106.0),
                (bracket("ABC", 50, 100.0, 95.0, 110.0), cfg(), 94.0),
                (bracket("ABC", 50, 100.0, 95.0, 110.0, type="limit", limit_price="100.00"), cfg(), 94.0)):
            r = one([o], c, prices={"ABC": price}, price_times=times)["results"][0]
            self.assertEqual(r["verdict"], "BLOCK", (o["side"], o["type"], r))
            self.assertTrue([x for x in r["reasons"] if x.startswith("stop:") and check._p(price) in x], r["reasons"])

    def test_a_shorts_size_is_measured_at_the_higher_price(self):
        # the loss is measured from the lower price, but the order is worth the higher one
        times = {"ABC": at(NOW) - datetime.timedelta(minutes=1)}
        short = bracket("ABC", 50, 100.0, 130.0, 90.0, side="sell")
        c = cfg(allow_short=True, max_order_inr=5500, max_loss_per_trade_inr=100000, min_reward_risk=0)
        r = one([short], c, prices={"ABC": 120.0}, price_times=times)["results"][0]
        self.assertEqual(r["entry_ref"], 100.0)
        self.assertIn("size: Rs 6,000 is over your Rs 5,500 max order", r["reasons"])

    def test_a_stale_price_is_not_used(self):
        times = {"RELIANCE": at(NOW) - datetime.timedelta(minutes=40)}
        r = one([rel()], prices={"RELIANCE": 1229.0}, price_times=times)["results"][0]
        self.assertEqual(r["entry_ref"], 1227.8)
        self.assertTrue(any("older than 15 minutes" in w for w in r["warnings"]))

    def test_a_limit_order_uses_its_limit_price(self):
        r = one([rel(type="limit", limit_price="1227.00")])["results"][0]
        self.assertEqual((r["entry_ref"], r["entry_ref_from"]), (1227.0, "limit"))

    def test_daily_loss_and_trades_per_day(self):
        state = {"orders": [{"status": "placed", "date": TODAY, "symbol": s, "side": "buy", "kind": "entry",
                             "max_loss": 1100} for s in ("TCS", "INFY")]}
        r = one([rel()], state=state)["results"][0]
        self.assertIn("daily loss: Rs 2,200 already at risk today plus Rs 987.73 for this one = Rs 3,187.73, over "
                      "your Rs 3,000 daily loss limit", r["reasons"])
        state["orders"][1]["max_loss"] = 900        # 2,000 + 987.73 fits under 3,000
        self.assertEqual(one([rel()], state=state)["results"][0]["verdict"], "PASS")
        state["orders"].append({"status": "placed", "date": TODAY, "symbol": "SBIN", "side": "buy", "kind": "entry",
                                "max_loss": 10})
        r = one([rel()], cfg(max_daily_loss_inr=10000), state=state)["results"][0]
        self.assertTrue(any(x.startswith("trades: 3 entries placed today") for x in r["reasons"]))

    def test_the_batch_counts_toward_the_daily_limits(self):
        res = one([bracket("TCS", 20, 2489.6, 2470.0, 2510.0), bracket("INFY", 40, 1148.1, 1130.0, 1170.0),
                   rel()], cfg(max_daily_loss_inr=1500))
        self.assertEqual([r["verdict"] for r in res["results"]], ["PASS", "PASS", "BLOCK"])
        self.assertTrue(any(x.startswith("daily loss") for x in res["results"][2]["reasons"]))

    def test_entry_window_ist(self):
        def why(now):
            return [x for x in one([rel()], now=now)["results"][0]["reasons"] if x.startswith("window")]
        self.assertEqual(why("2026-10-19 09:51"), [])
        self.assertEqual(why("2026-10-19 09:20"), ["window: no new entries before 09:30 IST (it is 09:20)"])
        self.assertEqual(why("2026-10-19 14:50"), ["window: no new entries after 14:45 IST (it is 14:50)"])
        self.assertIn("NSE holiday: Dussehra", why("2026-10-20 10:00")[0])
        self.assertIn("weekend", why("2026-10-17 10:00")[0])
        self.assertIn("holiday list unknown for 2028", why("2028-03-01 10:00")[0])
        self.assertIn("NSE is closed", why("2026-10-19 08:30")[0])
        # Muhurat: 14:20 to 15:30, so only 14:20 to 14:45 with the default window
        self.assertEqual(why("2026-11-08 14:30"), [])
        self.assertIn("NSE is closed", why("2026-11-08 11:00")[0])

    def test_price_band(self):
        r = one([rel()], bands={"RELIANCE": {"band": 1.3, "prev_close": 1211.2}})["results"][0]
        self.assertEqual([x for x in r["reasons"] if x.startswith("band")],
                         ["band: the entry 1227.80 is at the 1.3% upper band 1226.90 (previous close 1211.20): the "
                          "stock can freeze there and leave you unable to exit"])
        r = one([rel()], bands={"RELIANCE": {"band": 1.5, "prev_close": 1211.2}})["results"][0]
        self.assertEqual(r["verdict"], "PASS")
        self.assertIn("band: the entry 1227.80 is within 1% of the 1.5% upper band 1229.35", r["warnings"])
        self.assertEqual(one([rel()], bands={"RELIANCE": {"band": 20, "prev_close": 1211.2}})["results"][0]["warnings"], [])

    def test_product_and_assets(self):
        r = one([rel(product="CNC")])["results"][0]
        self.assertIn("product: CNC is not intraday; P1 plans intraday (MIS) trades only", r["reasons"])
        for sym in ("NIFTY26NOVFUT", "NIFTY26N1024500CE", "BANKNIFTY26NOV52000PE"):
            r = one([rel(symbol=sym)])["results"][0]
            self.assertTrue(any(x.startswith("asset: %s is an F&O contract" % sym) for x in r["reasons"]), sym)
        self.assertTrue(any(x.startswith("asset") for x in one([rel(symbol="BTC/INR")])["results"][0]["reasons"]))
        self.assertFalse(any(x.startswith("asset") for x in one([rel(symbol="M&M")])["results"][0]["reasons"]))

    def test_format(self):
        r = one([rel(qty=None, notional="10000")])["results"][0]
        self.assertTrue(any("notional (rupee amount) order does not exist" in x for x in r["reasons"]))
        r = one([rel(qty="7.5")])["results"][0]
        self.assertTrue(any("whole shares only" in x for x in r["reasons"]))
        r = one([rel(type="limit")])["results"][0]
        self.assertIn("format: a limit order needs a limit_price", r["reasons"])
        o = rel()
        del o["take_profit_limit_price"]
        self.assertTrue(any("needs both" in x for x in one([o])["results"][0]["reasons"]))

    def test_mis_leverage_and_cash(self):
        small = cfg(budget=40000, max_position_pct=500)
        r = one([rel()], small)["results"][0]
        self.assertTrue(any(x.startswith("cash: needs Rs 89,629.40 but only Rs 40,000") for x in r["reasons"]))
        r = one([rel()], dict(small, mis_leverage=5))["results"][0]
        self.assertFalse(any(x.startswith("cash: needs") for x in r["reasons"]), r["reasons"])

    def test_blocked_list_and_size_limits(self):
        self.assertIn("blocked: RELIANCE is on your blocked list",
                      one([rel()], cfg(blocked=["RELIANCE"]))["results"][0]["reasons"])
        self.assertTrue(any(x.startswith("size: Rs 89,629.40 is over your Rs 50,000 max order")
                            for x in one([rel()], cfg(max_order_inr=50000))["results"][0]["reasons"]))

    def test_live_account_warns(self):
        self.assertIn('LIVE account ("paper": false): these orders use real money', one([rel()], cfg(paper=False))["warnings"])


import example_values as EX  # noqa: E402
size = load("bharat-budget", "size")


def fut_cfg(**over):
    c = cfg(max_loss_per_trade_inr=EX.FUT_MAX_LOSS_LIMIT, margin_per_lot_inr={"NIFTY": EX.FUT_MARGIN_PER_LOT},
            allow_fno=True, max_daily_loss_inr=14000)
    c.update(over)
    return c


def fut_order(**over):
    plan = {"symbol": "NIFTY", "side": "buy", "date": EX.FUT_DATE, "setup": "orb", "entry_time": EX.FUT_TRIGGER_AT,
            "instrument": "fut", "contract": EX.FUT_CONTRACT, "lot": EX.FUT_LOT, "expiry": EX.FUT_EXPIRY,
            "basis_date": EX.FUT_BASIS_DATE, "basis_estimated": False, "entry": EX.FUT_PRICE,
            "stop": EX.FUT_STOP, "target": EX.FUT_TARGET, "flat_by": "15:15"}
    limit = over.pop("_limit", EX.FUT_PRICE)
    o = size.bracket_order(size.size_trade(plan, fut_cfg(), limit=limit, today=EX.FUT_DATE))
    o.update(over)
    return o


FUT_NOW = "2026-10-21 09:36"


class Futures(unittest.TestCase):
    def test_an_unreadable_name_with_instrument_fut_blocks(self):
        for sym in ("NIFTY26NOV", "NIFTYFUT", "NIFTY26NOVFU", "NIFTY26NOV23100XE"):
            r = one([fut_order(symbol=sym)], fut_cfg(), now=FUT_NOW)["results"][0]
            self.assertEqual(r["verdict"], "BLOCK", sym)
            self.assertTrue([x for x in r["reasons"] if x.startswith("asset:") and sym in x], (sym, r["reasons"]))

    def test_a_real_future_with_digits_in_its_underlying_is_readable(self):
        for sym in ("NIFTYNXT5026NOVFUT", "360ONE26NOVFUT"):
            r = one([fut_order(symbol=sym)], fut_cfg(), now=FUT_NOW)["results"][0]
            self.assertFalse([x for x in r["reasons"] if x.startswith("asset:")], (sym, r["reasons"]))
            pf = {"cash": 200000, "positions": [{"ticker": sym, "shares": 65, "lots": []}]}
            sell = {"symbol": sym, "exchange": "NFO", "product": "MIS", "side": "sell", "qty": "65", "type": "limit",
                    "limit_price": "150.00", "time_in_force": "day", "instrument": "fut", "lot": 65}
            r = one([sell], fut_cfg(), now=FUT_NOW, portfolio=pf)["results"][0]
            self.assertEqual((r["kind"], r["verdict"]), ("exit", "PASS"), (sym, r))

    def test_a_futures_name_with_a_bad_month_blocks(self):
        r = one([fut_order(symbol="NIFTY26XYZFUT")], fut_cfg(), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue([x for x in r["reasons"] if x.startswith("asset:") and "NIFTY26XYZFUT" in x], r["reasons"])

    def test_the_example_passes_with_costs_and_margin(self):
        r = one([fut_order()], fut_cfg(), now=FUT_NOW)["results"][0]
        self.assertEqual((r["verdict"], r["reasons"]), ("PASS", []), r)
        self.assertEqual(r["max_loss"], EX.FUT_MAX_LOSS)
        info = " ".join(r["info"])
        self.assertIn("costs Rs 894.53 at the stop", info)
        self.assertIn("margin Rs 1,75,953 for 1 lot", info)

    def test_a_future_off_nfo_blocks(self):
        r = one([fut_order(exchange="NSE")], fut_cfg(), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("goes on NFO", " ".join(r["reasons"]))

    def test_a_future_over_the_freeze_limit_blocks(self):
        o = fut_order(symbol="BANKNIFTY26OCTFUT", underlying="BANKNIFTY", lot=30, qty="930")
        r = one([o], fut_cfg(max_lots=40), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("900-unit limit for one BANKNIFTY order", " ".join(r["reasons"]))

    def test_a_futures_name_alone_is_f_and_o(self):
        pf = {"cash": 200000, "positions": [{"ticker": EX.FUT_CONTRACT, "shares": 65, "lots": []}]}
        sell = {"symbol": EX.FUT_CONTRACT, "side": "sell", "qty": "65", "type": "market"}
        r = one([sell], fut_cfg(), now=FUT_NOW, portfolio=pf)["results"][0]
        self.assertEqual((r["kind"], r["verdict"]), ("exit", "BLOCK"), r)
        self.assertIn("goes on NFO", " ".join(r["reasons"]))

    def test_freeze_tickets_are_whole_lots(self):
        r = one([opt_order(qty="3575")], fut_cfg(max_lots=60), now=FUT_NOW)["results"][0]
        self.assertIn("3 tickets of at most 1,755", " ".join(r["reasons"]), r)

    def test_fno_off_blocks(self):
        r = one([fut_order()], fut_cfg(allow_fno=False), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("allow_fno", " ".join(r["reasons"]))

    def test_qty_must_be_whole_lots(self):
        r = one([fut_order(qty="64")], fut_cfg(), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("lot of 65", " ".join(r["reasons"]))

    def test_a_new_entry_on_expiry_day_blocks_and_names_the_next(self):
        o = fut_order(symbol="NIFTY26NOVFUT", expiry="2026-11-23")
        r = one([o], fut_cfg(), now="2026-11-23 09:36")["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("trade NIFTY26DECFUT", " ".join(r["reasons"]))

    def test_a_market_entry_on_the_basis_estimate_blocks(self):
        # the basis moved 75 points in one session: an estimate is not an entry price
        r = one([fut_order(_limit=None)], fut_cfg(), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("limit", " ".join(r["reasons"]))
        r = one([fut_order(_limit=None, fut_price_source="user")], fut_cfg(), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "PASS", r)

    def test_max_lots_counts_what_you_hold(self):
        pf = {"cash": 200000, "positions": [{"ticker": EX.FUT_CONTRACT, "shares": 65, "lots": []}]}
        r = one([fut_order()], fut_cfg(budget=1000000), now=FUT_NOW, portfolio=pf)["results"][0]
        self.assertIn("max_lots", " ".join(r["reasons"]))

    def test_an_estimated_basis_blocks_a_market_entry(self):
        r = one([fut_order(basis_estimated=True, _limit=None)], fut_cfg(), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("estimated", " ".join(r["reasons"]))

    def test_margin_and_max_lots(self):
        r = one([fut_order()], fut_cfg(margin_per_lot_inr={}), now=FUT_NOW)["results"][0]
        self.assertIn("margin_per_lot_inr", " ".join(r["reasons"]))
        r = one([fut_order()], fut_cfg(margin_per_lot_inr={"NIFTY": 300000}), now=FUT_NOW)["results"][0]
        self.assertIn("margin:", " ".join(r["reasons"]))
        r = one([fut_order(qty="130", lots=2)], fut_cfg(max_loss_per_trade_inr=20000), now=FUT_NOW)["results"][0]
        self.assertIn("max_lots", " ".join(r["reasons"]))


class Exits(unittest.TestCase):
    PF = {"cash": 110370.6, "positions": [{"ticker": "RELIANCE", "shares": 73}]}

    def sell(self, qty=73):
        return {"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "side": "sell", "qty": str(qty),
                "type": "market", "time_in_force": "day"}

    def test_getting_out_is_never_blocked_by_the_day_limits(self):
        # 15:15, after the entry window, on a blocked symbol: an exit still passes
        res = one([self.sell()], cfg(blocked=["RELIANCE"]), now="2026-10-19 15:15", portfolio=self.PF,
                  prices={"RELIANCE": 1225.0})
        r = res["results"][0]
        self.assertEqual((r["verdict"], r["kind"]), ("PASS", "exit"))
        self.assertIn("exit, not blocked: blocked: RELIANCE is on your blocked list", r["warnings"])

    def test_selling_more_than_you_hold_is_a_short(self):
        r = one([self.sell(80)], portfolio=self.PF, prices={"RELIANCE": 1225.0})["results"][0]
        self.assertTrue(any(x.startswith("short: selling more RELIANCE than you hold") for x in r["reasons"]))


class Cli(unittest.TestCase):
    def test_entry_json_feeds_the_band_check(self):
        d = tempfile.mkdtemp()
        e = os.path.join(d, "entry.json")
        with open(e, "w") as fh:
            json.dump({"symbol": "RELIANCE", "status": "triggered", "prev_close": 1211.2, "band": 1.3}, fh)
        code, text, _ = run(["--orders", ORDERS_PATH, "--config", CFG_PATH, "--now", NOW, "--portfolio",
                             "/nonexistent", "--prices", "/nonexistent", "--state", "/nonexistent",
                             "--entry", e, "--no-write"])
        self.assertEqual(code, 1)
        self.assertIn("band: the entry 1227.80 is at the 1.3% upper band 1226.90", text)

    def test_errors_are_one_line(self):
        for argv in (["--orders", "/nonexistent.json"], ["--orders", ORDERS_PATH, "--now", "soon"],
                     ["--orders", ORDERS_PATH, "--today", "19/10"]):
            code, _, err = run(argv + ["--no-write"])
            self.assertEqual(code, 1, argv)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()


class ReviewFixes(unittest.TestCase):
    def test_window_off_still_blocks_a_closed_market(self):
        c = cfg(no_entries_before=None, no_entries_after=None)
        for now in ("2026-10-20 10:00", "2028-03-01 10:00", "2026-10-19 08:00"):
            r = one([rel()], c, now=now)["results"][0]
            self.assertTrue(any(x.startswith("window: NSE is closed") for x in r["reasons"]), now)
        self.assertEqual(one([rel()], c, now="2026-10-19 15:00")["results"][0]["verdict"], "PASS")

    def test_live_entries_need_the_stop_at_the_broker(self):
        o = rel()
        for k in ("stop_loss_stop_price", "take_profit_limit_price", "order_class"):
            del o[k]
        self.assertEqual(one([o])["results"][0]["verdict"], "PASS")          # paper: the plan stop is enough
        r = one([o], cfg(paper=False))["results"][0]
        self.assertTrue(any(x.startswith("stop: a live entry needs its stop at the broker") for x in r["reasons"]))
        self.assertEqual(one([rel()], cfg(paper=False))["results"][0]["verdict"], "PASS")

    def test_an_unknown_band_is_said(self):
        r = one([rel()])["results"][0]
        self.assertTrue(any(w.startswith("band: no price band known for RELIANCE, so the band check is off")
                            for w in r["warnings"]), r["warnings"])

    def test_an_old_entry_json_is_ignored(self):
        d = tempfile.mkdtemp()
        e = os.path.join(d, "entry.json")
        with open(e, "w") as fh:
            json.dump({"symbol": "RELIANCE", "date": "2026-10-16", "prev_close": 1211.2, "band": 1.3}, fh)
        code, text, _ = run(["--orders", ORDERS_PATH, "--config", CFG_PATH, "--now", NOW, "--portfolio",
                             "/nonexistent", "--prices", "/nonexistent", "--state", "/nonexistent",
                             "--entry", e, "--no-write"])
        self.assertEqual(code, 0)
        self.assertIn("is from 2026-10-16, not today: its band is not used", text)

    def test_live_refuses_a_pinned_clock(self):
        d = tempfile.mkdtemp()
        live = os.path.join(d, "live.json")
        with open(live, "w") as fh:
            json.dump(cfg(paper=False), fh)
        code, _, err = run(["--orders", ORDERS_PATH, "--config", live, "--now", NOW, "--no-write"])
        self.assertEqual(code, 1)
        self.assertIn("--now and --today are for tests and paper only", err)


class FuturesDeferred(unittest.TestCase):
    def test_the_fno_gate_reads_instrument_and_exchange(self):
        # a contract name that does not look like one is still F&O on NFO
        r = one([fut_order(symbol="NIFTYOCT")], fut_cfg(allow_fno=False), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("allow_fno", " ".join(r["reasons"]))

    def test_an_expired_contract_blocks(self):
        r = one([fut_order(expiry="2026-10-20")], fut_cfg(), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("expired", " ".join(r["reasons"]))

    def test_an_entry_with_no_expiry_blocks(self):
        o = fut_order()
        del o["expiry"]
        r = one([o], fut_cfg(), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("expiry", " ".join(r["reasons"]))

    def test_futures_fields_are_in_the_fingerprint(self):
        o = fut_order()
        for k, v in (("lot", 75), ("expiry", "2026-11-23"), ("basis_estimated", True), ("instrument", "eq"),
                     ("underlying", "BANKNIFTY"), ("fut_price_source", "user"), ("lots", 2)):
            self.assertNotEqual(check.fingerprint(o), check.fingerprint(dict(o, **{k: v})), k)
            self.assertEqual(check.fingerprint(dict(o, **{k: v})), ticket.fingerprint(dict(o, **{k: v})), k)

    def test_max_lots_zero_blocks(self):
        r = one([fut_order()], fut_cfg(max_lots=0), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("max_lots 0", " ".join(r["reasons"]))

    def test_a_futures_buy_earlier_in_the_batch_is_held(self):
        buy = fut_order()
        sell = {"symbol": EX.FUT_CONTRACT, "exchange": "NFO", "product": "MIS", "side": "sell", "qty": "65",
                "type": "market", "time_in_force": "day", "instrument": "fut", "lot": EX.FUT_LOT}
        res = one([buy, sell], fut_cfg(), now=FUT_NOW)
        self.assertEqual([r["verdict"] for r in res["results"]], ["PASS", "PASS"], res)
        self.assertEqual(res["results"][1]["kind"], "exit")
        self.assertEqual(res["cash_after"], 200000)   # closing the lot frees its margin


def opt_order(**over):
    tb = load("bharat-budget", "size")
    import test_budget as TB
    plan = dict(TB.OPT_PLAN, premium_source=over.pop("_source", "user"))
    o = tb.bracket_order(tb.size_trade(plan, fut_cfg(), today=EX.OPT_DATE))
    o.update(over)
    return o


class Options(unittest.TestCase):
    def test_the_example_passes_with_option_costs(self):
        r = one([opt_order()], fut_cfg(), now=FUT_NOW)["results"][0]
        self.assertEqual((r["verdict"], r["reasons"]), ("PASS", []), r)
        self.assertEqual(r["max_loss"], EX.OPT_MAX_LOSS)
        self.assertIn("costs Rs %s at the stop" % "{:,.2f}".format(EX.OPT_COSTS_AT_STOP), " ".join(r["info"]))

    def test_an_estimated_premium_blocks(self):
        r = one([opt_order(_source="estimated")], fut_cfg(), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("estimated", " ".join(r["reasons"]))

    def test_an_opening_sell_blocks_whatever_the_config(self):
        o = opt_order(side="sell")
        r = one([o], fut_cfg(allow_short=True), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("option selling is not part of this pack", " ".join(r["reasons"]))

    def test_selling_a_bought_option_is_an_exit(self):
        pf = {"cash": 200000, "positions": [{"ticker": EX.OPT_CONTRACT, "shares": 65, "lots": []}]}
        sell = {"symbol": EX.OPT_CONTRACT, "exchange": "NFO", "product": "MIS", "side": "sell", "qty": "65",
                "type": "limit", "limit_price": "150.00", "time_in_force": "day", "instrument": "opt", "lot": 65}
        r = one([sell], fut_cfg(), now=FUT_NOW, portfolio=pf)["results"][0]
        self.assertEqual((r["kind"], r["verdict"]), ("exit", "PASS"), r)

    def asset_reason(self, r, sym):
        return [x for x in r["reasons"] if x.startswith("asset:") and sym in x]

    def test_a_bad_month_code_blocks(self):
        sym = "NIFTY26XYZ23100CE"
        r = one([opt_order(symbol=sym)], fut_cfg(), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue(self.asset_reason(r, sym), r["reasons"])
        o = opt_order(symbol=sym)
        o.pop("instrument", None)
        o.pop("exchange", None)
        r = one([o], fut_cfg(), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue(self.asset_reason(r, sym), r["reasons"])

    def test_an_impossible_weekly_date_blocks_not_crashes(self):
        sym = "NIFTY26N3123100CE"   # 31 Nov
        r = one([opt_order(symbol=sym, expiry=None)], fut_cfg(), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue(self.asset_reason(r, sym), r["reasons"])

    def test_a_real_contract_with_digits_in_its_underlying_is_not_unreadable(self):
        # 360ONE and NIFTYNXT50 are real NSE contracts the name parser cannot split; they must not block as unreadable
        for sym in ("360ONE26NOV1200CE", "NIFTYNXT5026NOV60000CE"):
            r = one([opt_order(symbol=sym)], fut_cfg(), now=FUT_NOW)["results"][0]
            self.assertFalse(self.asset_reason(r, sym), r["reasons"])
            pf = {"cash": 200000, "positions": [{"ticker": sym, "shares": 65, "lots": []}]}
            sell = {"symbol": sym, "exchange": "NFO", "product": "MIS", "side": "sell", "qty": "65", "type": "limit",
                    "limit_price": "150.00", "time_in_force": "day", "instrument": "opt", "lot": 65}
            r = one([sell], fut_cfg(), now=FUT_NOW, portfolio=pf)["results"][0]
            self.assertEqual((r["kind"], r["verdict"]), ("exit", "PASS"), r)

    def test_a_bad_name_blocks_an_exit_too(self):
        sym = "NIFTY26XYZ23100CE"
        pf = {"cash": 200000, "positions": [{"ticker": sym, "shares": 65, "lots": []}]}
        sell = {"symbol": sym, "exchange": "NFO", "product": "MIS", "side": "sell", "qty": "65",
                "type": "limit", "limit_price": "150.00", "time_in_force": "day", "instrument": "opt", "lot": 65}
        r = one([sell], fut_cfg(), now=FUT_NOW, portfolio=pf)["results"][0]
        self.assertEqual((r["kind"], r["verdict"]), ("exit", "BLOCK"), r)
        self.assertTrue(self.asset_reason(r, sym), r["reasons"])

    def exit_sell(self, **over):
        pf = {"cash": 200000, "positions": [{"ticker": EX.OPT_CONTRACT, "shares": 65, "lots": []}]}
        sell = {"symbol": EX.OPT_CONTRACT, "exchange": "NFO", "product": "MIS", "side": "sell", "qty": "65",
                "type": "limit", "limit_price": "150.00", "time_in_force": "day", "instrument": "opt", "lot": 65}
        sell.update(over)
        return one([sell], fut_cfg(), now=FUT_NOW, portfolio=pf)["results"][0]

    def test_closing_an_option_at_market_blocks(self):
        r = self.exit_sell(type="market", limit_price=None)
        self.assertEqual((r["kind"], r["verdict"]), ("exit", "BLOCK"), r)
        self.assertIn("flatten --symbol", " ".join(r["reasons"]))
        self.assertIn("--limit", " ".join(r["reasons"]))

    def test_an_option_off_nfo_blocks(self):
        r = one([opt_order(exchange="NSE")], fut_cfg(), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("NFO", " ".join(r["reasons"]))
        o = opt_order()
        del o["exchange"]
        r = one([o], fut_cfg(), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("no exchange", " ".join(r["reasons"]))
        r = self.exit_sell(exchange="NSE")
        self.assertEqual((r["kind"], r["verdict"]), ("exit", "BLOCK"), r)

    def test_a_weekly_name_sets_the_expiry(self):
        r = one([opt_order(symbol="NIFTY26O2023100CE", expiry="2026-10-27")], fut_cfg(), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        why = " ".join(r["reasons"])
        self.assertIn("2026-10-20", why)
        self.assertIn("2026-10-27", why)
        r = one([opt_order(symbol="NIFTY26O2723100CE", expiry="2026-10-27")], fut_cfg(), now=FUT_NOW)["results"][0]
        self.assertNotIn("expiry:", " ".join(r["reasons"]), r)

    def test_a_weekly_expiring_today_from_its_name(self):
        o = opt_order(symbol="NIFTY26O2123100CE", expiry=None)
        r = one([o], fut_cfg(), now="2026-10-21 13:05")["results"][0]
        why = " ".join(r["reasons"])
        self.assertIn("expiry_day_cutoff", why)
        self.assertNotIn("no expiry date", why)

    def test_a_monthly_name_checks_the_month(self):
        r = one([opt_order(expiry="2026-11-24")], fut_cfg(), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("a 2026-10 contract", " ".join(r["reasons"]))

    def test_over_the_freeze_limit_blocks(self):
        r = one([opt_order(qty="1820")], fut_cfg(max_lots=30), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        why = " ".join(r["reasons"])
        self.assertIn("1,800", why)
        self.assertIn("2 tickets", why)
        r = one([opt_order(qty="1755")], fut_cfg(max_lots=30), now=FUT_NOW)["results"][0]
        self.assertNotIn("freeze:", " ".join(r["reasons"]))

    def test_a_market_order_blocks(self):
        o = opt_order(type="market")
        del o["limit_price"]
        r = one([o], fut_cfg(), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("limit", " ".join(r["reasons"]))

    def test_expiry_day_after_the_cutoff_blocks(self):
        o = opt_order(expiry=EX.OPT_DATE)
        r = one([o], fut_cfg(), now="2026-10-21 13:05")["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("expiry_day_cutoff", " ".join(r["reasons"]))
        r = one([o], fut_cfg(), now="2026-10-21 12:00")["results"][0]
        self.assertEqual(r["verdict"], "PASS", r)
        self.assertIn("expires today", " ".join(r["warnings"]))

    def test_an_illiquid_strike_blocks(self):
        r = one([opt_order(oi=20000)], fut_cfg(), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("min_option_oi", " ".join(r["reasons"]))
        r = one([opt_order(illiquid=True)], fut_cfg(), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")

    def test_costs_block_a_cheap_option(self):
        # Rs 3 premium, 1 lot: Rs 40 of brokerage alone on a Rs 195 position
        o = opt_order(limit_price="3.00", stop_loss_stop_price="2.00", take_profit_limit_price="4.00",
                      plan={"entry": 3.0, "stop": 2.0, "target": 4.0})
        r = one([o], fut_cfg(), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("costs:", " ".join(r["reasons"]))

    @staticmethod
    def no_stop(o):
        for k in ("stop_loss_stop_price", "stop_loss_limit_price", "take_profit_limit_price", "order_class"):
            o.pop(k, None)
        return o

    def cheap_no_stop(self, **over):
        return self.no_stop(opt_order(limit_price="3.00", plan={"entry": 3.0, "target": 6.0}, **over))

    def test_a_cheap_option_without_a_stop_still_blocks_on_costs(self):
        r = one([self.cheap_no_stop()], fut_cfg(require_stop=False), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK", r)
        why = " ".join(r["reasons"])
        self.assertIn("max_option_cost_share", why)
        self.assertIn("Rs 195", why)

    def test_enough_cheap_lots_pass_without_a_stop(self):
        r = one([self.cheap_no_stop(qty="325")], fut_cfg(require_stop=False, max_lots=5), now=FUT_NOW)["results"][0]
        self.assertNotIn("max_option_cost_share", " ".join(r["reasons"]))

    def test_no_stop_and_no_qty_does_not_crash(self):
        o = self.cheap_no_stop()
        del o["qty"]
        r = one([o], fut_cfg(require_stop=False), now=FUT_NOW)["results"][0]
        why = " ".join(r["reasons"])
        self.assertIn("no qty", why)
        self.assertNotIn("max_option_cost_share", why)

    def test_the_cost_check_runs_once_with_a_stop(self):
        o = opt_order(limit_price="3.00", stop_loss_stop_price="2.00", take_profit_limit_price="6.00",
                      plan={"entry": 3.0, "stop": 2.0, "target": 6.0})
        r = one([o], fut_cfg(), now=FUT_NOW)["results"][0]
        self.assertEqual(sum("max_option_cost_share" in x for x in r["reasons"]), 1, r["reasons"])

    def test_a_cheap_option_blocks_whatever_the_target(self):
        o = opt_order(limit_price="3.00", stop_loss_stop_price="2.00", take_profit_limit_price="6.00",
                      plan={"entry": 3.0, "stop": 2.0, "target": 6.0})
        r = one([o], fut_cfg(), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        why = " ".join(r["reasons"])
        self.assertIn("max_option_cost_share", why)
        self.assertIn("Rs 195", why)

    def test_enough_cheap_lots_pass_the_premium_cost_check(self):
        o = opt_order(qty="325", limit_price="3.00", stop_loss_stop_price="2.00", take_profit_limit_price="6.00",
                      plan={"entry": 3.0, "stop": 2.0, "target": 6.0})
        r = one([o], fut_cfg(max_lots=5), now=FUT_NOW)["results"][0]
        self.assertNotIn("max_option_cost_share", " ".join(r["reasons"]), r)

    def test_option_fields_are_in_the_fingerprint(self):
        o = opt_order()
        for k, v in (("right", "PE"), ("strike", 23200.0), ("premium_source", "estimated"), ("oi", 1)):
            self.assertNotEqual(check.fingerprint(o), check.fingerprint(dict(o, **{k: v})), k)
            self.assertEqual(check.fingerprint(dict(o, **{k: v})), ticket.fingerprint(dict(o, **{k: v})), k)


class ReviewFixesP3(unittest.TestCase):
    def test_an_option_stop_without_its_limit_blocks(self):
        o = opt_order()
        del o["stop_loss_limit_price"]          # the ticket would send SL-M, which NSE rejects on options
        r = one([o], fut_cfg(), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("stop_loss_limit_price", " ".join(r["reasons"]))

    def test_an_unknown_nfo_name_fails_closed(self):
        o = opt_order(symbol="NIFTY 26OCT 23100 CE")
        del o["instrument"]
        r = one([o], fut_cfg(), now=FUT_NOW)["results"][0]
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("NFO", " ".join(r["reasons"]))
