import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX
import example_values as EX

size = load("bharat-budget", "size")

CFG = os.path.join(ROOT, "bharat-budget", "config.example.json")
PLAN = os.path.join(FIX, "bharat-budget", "plan-reliance.json")
NOW = "2026-10-19 09:51"
TODAY = "2026-10-19"
CONFIG_KEYS = {
    "budget": 200000, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_inr": 200000, "min_order_inr": 500,
    "max_loss_per_trade_inr": 1000, "max_daily_loss_inr": 3000, "max_trades_per_day": 3, "max_orders_per_day": 10,
    "min_reward_risk": 1.0, "max_cost_share": 0.25, "max_option_cost_share": 0.10, "require_stop": True,
    "no_entries_before": "09:30", "no_entries_after": "14:45", "flat_by": "15:15", "mis_leverage": 1,
    "broker": "zerodha", "paper": True, "blocked": [], "allow_fno": False, "allow_margin": False,
    "allow_short": False, "margin_per_lot_inr": {}, "max_lots": 1,
    "max_premium_inr": None, "min_option_oi": 50000, "expiry_day_cutoff": "13:00", "risk_free_rate": 0.06,
    "stop_limit_ticks": 10,
}


def cfg(**over):
    with open(CFG) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def the_plan(**over):
    with open(PLAN) as fh:
        p = json.load(fh)
    p.update(over)
    return p


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = size.main(argv)
    return code, out.getvalue(), err.getvalue()


class TheExample(unittest.TestCase):
    def setUp(self):
        self.s = size.size_trade(the_plan(), cfg(), today=TODAY)

    def test_config_example_is_the_contract(self):
        self.assertEqual(cfg(), CONFIG_KEYS)

    def test_73_shares_bound_by_the_max_loss_with_costs(self):
        s = self.s
        self.assertEqual((s["qty"], s["bound_by"], s["refusal"]), (73, "max loss", None))
        # by hand: 73 x 12.45 = 908.85; costs at the stop: brokerage 40, STT 22, NSE 5.48, GST 8.22,
        # SEBI 0.18, stamp 3 = 78.88; 908.85 + 78.88 = 987.73. 74 shares would be 921.30 + 78.96 = 1,000.26
        self.assertEqual((s["costs_at_stop"], s["max_loss"]), (78.88, 987.73))
        self.assertEqual((s["costs_at_target"], s["gain_at_target"]), (79.94, 828.91))
        self.assertEqual(s["value"], 89629.4)
        self.assertGreater(74 * 12.45 + size.costs_at(74, 1227.8, 1215.35), 1000)
        self.assertEqual([(l["name"], l["shares"]) for l in s["limits"]],
                         [("max loss", 73), ("position cap", 122), ("max order", 162), ("cash", 146)])
        self.assertEqual((s["qty"], s["costs_at_stop"], s["max_loss"]), (EX.QTY, EX.COSTS_AT_STOP, EX.MAX_LOSS))

    def test_the_bracket_order(self):
        o = size.bracket_order(self.s)
        self.assertEqual({k: o[k] for k in ("symbol", "exchange", "product", "side", "qty", "type", "time_in_force",
                                            "order_class", "take_profit_limit_price", "stop_loss_stop_price")},
                         {"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "side": "buy", "qty": "73",
                          "type": "market", "time_in_force": "day", "order_class": "bracket",
                          "take_profit_limit_price": "1240.25", "stop_loss_stop_price": "1215.35"})
        self.assertEqual(o["plan"]["costs_at_stop"], 78.88)
        self.assertIn("Rs 1,000 max loss per trade", o["why"])

    def test_a_limit_order_rounds_to_the_tick(self):
        s = size.size_trade(the_plan(), cfg(), limit=1227.82, today=TODAY)
        self.assertEqual(s["entry"], 1227.8)
        o = size.bracket_order(s)
        self.assertEqual((o["type"], o["limit_price"]), ("limit", "1227.80"))
        # a limit above the entry shrinks the reward under the risk: refused, not resized
        self.assertIn("reward:risk 0.97", size.size_trade(the_plan(), cfg(), limit=1228.0, today=TODAY)["refusal"])

    def test_cli_writes_one_bracket_order(self):
        d = tempfile.mkdtemp()
        out = os.path.join(d, "orders.json")
        code, text, err = run(["--plan", PLAN, "--config", CFG, "--now", NOW, "--portfolio", "/nonexistent",
                               "--write", out])
        self.assertEqual(code, 0, err)
        for line in ("max loss      Rs 1,000 incl. costs: 73 x 12.45 + Rs 78.88 costs = Rs 987.73",
                     "position cap  75% of Rs 2,00,000 = Rs 1,50,000 / 1227.80",
                     "Size: 73 shares of RELIANCE = Rs 89,629.40 at 1227.80 (MIS, intraday).",
                     "Costs, round trip (Zerodha): Rs 78.88 if it exits at the stop, Rs 79.94 at the target.",
                     "Max loss at the stop: 73 x 12.45 + Rs 78.88 costs = Rs 987.73.",
                     "buy 73 RELIANCE on NSE, MIS, market, day, take-profit 1240.25, stop-loss 1215.35",
                     "Flat by 15:15 IST", "not financial advice"):
            self.assertIn(line, text)
        with open(out) as fh:
            doc = json.load(fh)
        self.assertEqual((doc["created_by"], doc["status"], doc["paper"], len(doc["orders"])),
                         ("bharat-budget", "proposed", True, 1))
        self.assertEqual(doc["created_at"], "2026-10-19T04:21:00Z")


FUT_PLAN = {"created_by": "bharat-exit", "symbol": "NIFTY", "side": "buy", "date": EX.FUT_DATE, "setup": "orb",
            "entry_time": EX.FUT_TRIGGER_AT, "instrument": "fut", "contract": EX.FUT_CONTRACT, "lot": EX.FUT_LOT,
            "expiry": EX.FUT_EXPIRY, "basis_ratio": EX.FUT_BASIS, "basis_date": EX.FUT_BASIS_DATE,
            "basis_estimated": False, "entry": EX.FUT_PRICE, "stop": EX.FUT_STOP, "target": EX.FUT_TARGET,
            "risk_per_share": EX.FUT_RISK_PER_UNIT, "risk_per_unit": EX.FUT_RISK_PER_UNIT, "flat_by": "15:15"}


def fut_cfg(**over):
    c = cfg(max_loss_per_trade_inr=EX.FUT_MAX_LOSS_LIMIT, margin_per_lot_inr={"NIFTY": EX.FUT_MARGIN_PER_LOT},
            allow_fno=True)
    c.update(over)
    return c


class Futures(unittest.TestCase):
    def test_the_example_sizes_to_the_lot(self):
        s = size.size_trade(dict(FUT_PLAN), fut_cfg(), today=EX.FUT_DATE)
        self.assertIsNone(s["refusal"])
        self.assertEqual((s["lots"], s["qty"]), (EX.FUT_LOTS, EX.FUT_LOTS * EX.FUT_LOT))
        self.assertEqual((s["costs_at_stop"], s["max_loss"]), (EX.FUT_COSTS_AT_STOP, EX.FUT_MAX_LOSS))
        names = {l["name"]: l["lots"] for l in s["limits"]}
        self.assertEqual(names["max loss"], 1)      # 2 lots: 2 x 5,307.25 + costs > 7,000
        self.assertEqual(names["margin"], 1)        # (2,00,000 - 20,000) / 1,75,953
        self.assertEqual(names["max lots"], 1)
        o = size.bracket_order(s)
        self.assertEqual((o["exchange"], o["product"], o["symbol"], o["qty"], o["instrument"], o["lots"], o["lot"]),
                         ("NFO", "MIS", EX.FUT_CONTRACT, "65", "fut", 1, 65))
        self.assertEqual((o["stop_loss_stop_price"], o["take_profit_limit_price"]), ("23040.30", "23203.60"))
        self.assertEqual(o["underlying"], "NIFTY")
        self.assertEqual(o["fut_price_source"], "bhavcopy")
        text = size.plan_report(s, o)
        self.assertIn("1 lot of NIFTY26OCTFUT", text)
        self.assertIn("margin", text)

    def test_fno_off_refuses(self):
        with self.assertRaises(size.SizeError) as cm:
            size.size_trade(dict(FUT_PLAN), fut_cfg(allow_fno=False), today=EX.FUT_DATE)
        self.assertIn("allow_fno", str(cm.exception))

    def test_no_margin_figure_refuses_and_names_the_key(self):
        s = size.size_trade(dict(FUT_PLAN), fut_cfg(margin_per_lot_inr={}), today=EX.FUT_DATE)
        self.assertEqual(s["qty"], 0)
        self.assertIn("margin_per_lot_inr", s["refusal"])
        self.assertIsNone(size.bracket_order(s))

    def test_a_margin_above_the_cash_is_zero_lots(self):
        s = size.size_trade(dict(FUT_PLAN), fut_cfg(margin_per_lot_inr={"NIFTY": 250000}), today=EX.FUT_DATE)
        self.assertEqual((s["lots"], s["bound_by"]), (0, "margin"))
        self.assertIn("margin", s["refusal"])

    def test_max_loss_is_whole_lots(self):
        s = size.size_trade(dict(FUT_PLAN), fut_cfg(max_loss_per_trade_inr=6000), today=EX.FUT_DATE)
        self.assertEqual((s["lots"], s["bound_by"]), (0, "max loss"))
        s = size.size_trade(dict(FUT_PLAN), fut_cfg(max_loss_per_trade_inr=20000, max_lots=3, budget=1000000,
                                                    margin_per_lot_inr={"NIFTY": 175953}), today=EX.FUT_DATE)
        self.assertEqual((s["lots"], s["qty"]), (3, 195))

    def test_costs_use_futures_rounding(self):
        with open(os.path.join(FIX, "p2", "zerodha-calc-fut.json")) as fh:
            row = json.load(fh)["rows"][0]           # 65 NIFTY 23,500 -> 23,600
        p = dict(FUT_PLAN, entry=23500.0, stop=23400.0, target=23600.0)
        s = size.size_trade(p, fut_cfg(max_loss_per_trade_inr=20000), today=EX.FUT_DATE)
        self.assertEqual(s["lots"], 1)
        self.assertEqual(s["costs_at_target"], row["total"])


class Rules(unittest.TestCase):
    def test_zero_shares_is_a_plain_refusal(self):
        s = size.size_trade(the_plan(), cfg(max_loss_per_trade_inr=12), today=TODAY)
        self.assertEqual(s["qty"], 0)
        self.assertIn("more than your Rs 12 max loss per trade", s["refusal"])
        self.assertEqual(size.size_trade(the_plan(), cfg(max_loss_per_trade_inr=30), today=TODAY)["qty"], 2)
        self.assertIsNone(size.bracket_order(s))
        code, text, _ = run(["--plan", PLAN, "--config", CFG, "--now", NOW, "--budget", "200000",
                             "--portfolio", "/nonexistent", "--write", os.path.join(tempfile.mkdtemp(), "o.json")])
        self.assertEqual(code, 0)

    def test_each_limit_can_bind(self):
        self.assertEqual(size.size_trade(the_plan(), cfg(budget=100000), today=TODAY)["bound_by"], "position cap")
        self.assertEqual(size.size_trade(the_plan(), cfg(max_order_inr=50000), today=TODAY)["bound_by"], "max order")
        s = size.size_trade(the_plan(), cfg(max_position_pct=100, cash_buffer_pct=60), today=TODAY)
        self.assertEqual((s["bound_by"], s["qty"]), ("cash", 65))   # 80,000 / 1227.80

    def test_mis_leverage_widens_only_the_cash_limit(self):
        s = size.size_trade(the_plan(), cfg(budget=40000, max_position_pct=500, mis_leverage=5), today=TODAY)
        self.assertEqual([(l["name"], l["shares"]) for l in s["limits"]][3], ("cash", 146))   # 36,000 x 5
        self.assertEqual((s["qty"], s["bound_by"]), (73, "max loss"))
        for bad in (9, 0.5, "lots"):
            with self.assertRaises(size.SizeError) as cm:
                size.size_trade(the_plan(), cfg(mis_leverage=bad), today=TODAY)
            self.assertIn("between 1 and 5", str(cm.exception))

    def test_a_fresh_higher_price_means_a_bigger_risk(self):
        s = size.size_trade(the_plan(), cfg(), price=1230.0, today=TODAY)
        self.assertEqual((s["entry"], s["entry_from"], s["risk_per_share"]), (1230.0, "price", 14.65))
        self.assertLess(s["qty"], 73)
        self.assertEqual(size.size_trade(the_plan(), cfg(), price=1225.0, today=TODAY)["entry"], 1227.8)

    def test_prices_file_is_used_only_when_fresh(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "prices.json")
        with open(p, "w") as fh:
            json.dump({"prices": {"RELIANCE": 1230.0}, "as_of": "2026-10-19T04:20:00Z"}, fh)
        code, text, _ = run(["--plan", PLAN, "--config", CFG, "--now", NOW, "--prices", p,
                             "--portfolio", "/nonexistent"])
        self.assertIn("is above the plan's entry 1227.80", text)
        code, text, _ = run(["--plan", PLAN, "--config", CFG, "--now", "2026-10-19 10:30", "--prices", p,
                             "--portfolio", "/nonexistent"])
        self.assertIn("not used (older than 15 minutes)", text)

    def test_the_portfolio_cash_caps_the_size(self):
        s = size.size_trade(the_plan(), cfg(), cash=50000, today=TODAY)
        self.assertEqual((s["bound_by"], s["qty"]), ("cash", 24))      # (50,000 - 20,000) / 1227.80

    def test_bad_plans(self):
        cases = [(dict(side="sell"), "long (buy)"), (dict(date="2026-10-21"), "today is 2026-10-19"),
                 (dict(stop=None), "positive stop"), (dict(stop=1230.0), "not below the entry"),
                 (dict(target=1220.0), "not above the entry"), (dict(symbol=""), "no symbol")]
        for over, words in cases:
            with self.assertRaises(size.SizeError, msg=over) as cm:
                size.size_trade(the_plan(**over), cfg(), today=TODAY)
            self.assertIn(words, str(cm.exception))
        with self.assertRaises(size.SizeError):
            size.size_trade(the_plan(), cfg(blocked=["reliance"]), today=TODAY)
        with self.assertRaises(size.SizeError):
            size.size_trade(the_plan(), cfg(budget=None), today=TODAY)

    def test_reward_risk_and_minimum_order(self):
        s = size.size_trade(the_plan(target=1235.0), cfg(), today=TODAY)
        self.assertIn("reward:risk 0.58", s["refusal"])
        s = size.size_trade(the_plan(), cfg(min_order_inr=100000), today=TODAY)
        self.assertIn("under your Rs 1,00,000 minimum order", s["refusal"])

    def test_missing_plan(self):
        code, _, err = run(["--plan", "/nonexistent.json", "--config", CFG])
        self.assertEqual(code, 1)
        self.assertIn("run bharat-exit first", err)


if __name__ == "__main__":
    unittest.main()


class FuturesDeferred(unittest.TestCase):
    def test_max_lots_zero_means_no_lots(self):
        s = size.size_trade(dict(FUT_PLAN), fut_cfg(max_lots=0), today=EX.FUT_DATE)
        self.assertEqual((s["lots"], s["bound_by"]), (0, "max lots"))
        self.assertIsNone(size.bracket_order(s))

    def test_the_index_price_in_prices_json_is_not_the_futures_price(self):
        d = tempfile.mkdtemp()
        paths = {}
        for name, doc in (("plan", FUT_PLAN), ("config", fut_cfg()),
                          ("prices", {"prices": {"NIFTY": 23900.0}, "as_of": "2026-10-21T04:05:00Z"})):
            paths[name] = os.path.join(d, name + ".json")
            with open(paths[name], "w") as fh:
                json.dump(doc, fh)
        code, text, _ = run(["--plan", paths["plan"], "--config", paths["config"], "--now", "2026-10-21 09:36",
                             "--prices", paths["prices"], "--portfolio", os.path.join(d, "none.json")])
        self.assertEqual(code, 0, text)
        self.assertNotIn("sized at", text)


OPT_PLAN = {"created_by": "bharat-exit", "symbol": "NIFTY", "side": "buy", "date": EX.OPT_DATE, "setup": "orb",
            "entry_time": EX.FUT_TRIGGER_AT, "instrument": "opt", "contract": EX.OPT_CONTRACT, "lot": EX.OPT_LOT,
            "expiry": EX.OPT_EXPIRY, "strike": EX.OPT_STRIKE, "right": "CE", "iv": EX.OPT_IV,
            "forward_ratio": 1.000362, "rate": 0.06, "premium_source": "estimated", "oi": EX.OPT_OI, "volume": 1000,
            "illiquid": False, "index_entry": EX.FUT_TRIGGER_CLOSE, "index_stop": EX.FUT_INDEX_STOP,
            "index_target": EX.OPT_INDEX_TARGET, "index_side": "up", "entry": EX.OPT_PREMIUM, "stop": EX.OPT_STOP,
            "target": EX.OPT_TARGET, "risk_per_share": EX.OPT_RISK_PER_UNIT, "risk_per_unit": EX.OPT_RISK_PER_UNIT,
            "flat_by": "15:15"}


def opt_cfg(**over):
    c = cfg(max_loss_per_trade_inr=EX.FUT_MAX_LOSS_LIMIT, allow_fno=True)
    c.update(over)
    return c


class Options(unittest.TestCase):
    def test_the_example_sizes_by_premium(self):
        s = size.size_trade(dict(OPT_PLAN), opt_cfg(), today=EX.OPT_DATE)
        self.assertIsNone(s["refusal"])
        self.assertEqual((s["lots"], s["qty"], s["value"]), (EX.OPT_LOTS, EX.OPT_LOTS * EX.OPT_LOT, EX.OPT_PREMIUM_PAID))
        self.assertEqual((s["costs_at_stop"], s["max_loss"]), (EX.OPT_COSTS_AT_STOP, EX.OPT_MAX_LOSS))
        names = {l["name"]: l["lots"] for l in s["limits"]}
        self.assertEqual(names["max loss"], 2)        # 2 x 2,574 + costs <= 7,000; 3 lots is over
        self.assertEqual(names["max lots"], 1)
        self.assertEqual(names["cash"], 16)           # 1,80,000 / (65 x 162.95)
        o = size.bracket_order(s)
        self.assertEqual((o["exchange"], o["product"], o["symbol"], o["qty"], o["instrument"], o["type"],
                          o["limit_price"]), ("NFO", "MIS", EX.OPT_CONTRACT, "65", "opt", "limit", "162.95"))
        self.assertEqual((o["stop_loss_stop_price"], o["take_profit_limit_price"]), ("123.35", "209.55"))
        self.assertEqual((o["right"], o["strike"], o["expiry"], o["premium_source"]),
                         ("CE", EX.OPT_STRIKE, EX.OPT_EXPIRY, "estimated"))
        text = size.plan_report(s, o)
        self.assertIn("1 lot of %s" % EX.OPT_CONTRACT, text)
        self.assertIn("paid in full", text)

    def test_it_is_always_a_limit_order(self):
        s = size.size_trade(dict(OPT_PLAN), opt_cfg(), today=EX.OPT_DATE, limit=None)
        self.assertEqual(size.bracket_order(s)["type"], "limit")

    def test_max_premium_caps_the_lots(self):
        s = size.size_trade(dict(OPT_PLAN), opt_cfg(max_premium_inr=5000), today=EX.OPT_DATE)
        self.assertEqual((s["lots"], s["bound_by"]), (0, "max premium"))
        self.assertIn("max_premium_inr", s["refusal"])

    def test_the_premium_is_paid_from_cash(self):
        s = size.size_trade(dict(OPT_PLAN), opt_cfg(budget=10000), today=EX.OPT_DATE)
        self.assertEqual((s["lots"], s["bound_by"]), (0, "cash"))

    def test_fno_off_refuses(self):
        with self.assertRaises(size.SizeError) as cm:
            size.size_trade(dict(OPT_PLAN), opt_cfg(allow_fno=False), today=EX.OPT_DATE)
        self.assertIn("allow_fno", str(cm.exception))
