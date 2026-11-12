#!/usr/bin/env python3
"""Position sizing for bharat-budget. Standard library only, no network.

Sizes ONE intraday NSE trade from trader/plan.json (written by bharat-exit)
to your budget and your max loss per trade, trading costs included, then
writes one bracket order (entry, stop-loss, take-profit) to
trader/orders.json. It never places anything: bharat-risk checks the order
and bharat-broker asks you yes or no.

    python3 size.py                                  # trader/plan.json + trader/config.json
    python3 size.py --write                          # write trader/orders.json
    python3 size.py --limit                          # a limit order at the plan's entry
    python3 size.py --limit 1228.00 --write          # a limit order at 1228.00
    python3 size.py --now "2026-10-19 09:51" --json  # pin the clock (IST)

How many shares. Whole shares only.

    shares = the smallest of
      max loss       the most shares where shares x risk a share + round-trip
                     costs at the stop <= max_loss_per_trade_inr
      position cap   budget x max_position_pct / entry
      max order      max_order_inr / entry
      cash           (budget minus the cash buffer) x mis_leverage / entry
                     (and no more than the cash in trader/portfolio.json, if you have one)

Costs are Zerodha's intraday equity charges (brokerage, STT, NSE transaction
and investor-protection charges, SEBI fee, GST, stamp duty), from india.py.
mis_leverage (default 1, at most 5) lets the cash limit use the broker's
intraday margin; it never raises the max loss or the position cap.

The entry it sizes at: a limit order's limit price; for a market order the
plan's entry, or the price in trader/prices.json if that is fresh (under 15
minutes old) and higher, because a price that ran up since the plan means a
bigger loss at the same stop. Zero shares (the stop is too far away for your
max loss) is a plain refusal: no order is written. So is a reward:risk under
min_reward_risk.
"""

import argparse
import datetime
import json
import math
import os
import sys

import india

DEFAULTS = {
    "budget": None,
    "cash_buffer_pct": 10,
    "max_position_pct": 75,
    "max_order_inr": 200000,
    "min_order_inr": 500,
    "max_loss_per_trade_inr": 1000,
    "max_daily_loss_inr": 3000,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "min_reward_risk": 1.0,
    "max_cost_share": 0.25,
    "require_stop": True,
    "no_entries_before": "09:30",
    "no_entries_after": "14:45",
    "flat_by": "15:15",
    "mis_leverage": 1,
    "broker": "zerodha",
    "paper": True,
    "blocked": [],
    "allow_fno": False,
    "allow_margin": False,
    "allow_short": False,
    "margin_per_lot_inr": {},
    "max_lots": 1,
    "max_premium_inr": None,
    "min_option_oi": 50000,
    "expiry_day_cutoff": "13:00",
    "risk_free_rate": 0.06,
    "stop_limit_ticks": 10,
}
FUT_INDEX = ("NIFTY", "BANKNIFTY")
MAX_LEVERAGE = 5      # SEBI's minimum intraday margin is 20 % of the trade's value
EPS = 1e-9
FRESH_MINUTES = 15
SEGMENT = "eq_intraday"
SETUP_WORDS = {"orb": "opening-range breakout", "flag": "flag", "bounce": "oversold bounce", "level": "level"}


class SizeError(Exception):
    pass


# ---------------------------------------------------------------- time (IST)

def to_local(utc):
    """Naive UTC -> naive IST."""
    return india.to_ist(utc.replace(tzinfo=datetime.timezone.utc)).replace(tzinfo=None)


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as IST."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise SizeError("--now wants a time like \"2026-10-19 09:51\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return india.ist_to_utc(dt).replace(tzinfo=None)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_utc(text):
    """"2026-10-19T04:21:13Z" -> naive UTC, or None."""
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def utc_iso(now_utc=None):
    now_utc = now_utc or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return now_utc.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- inputs

def read_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SizeError("no %s at %s" % (what, path))
    except json.JSONDecodeError as e:
        raise SizeError("%s is not valid JSON: %s" % (path, e))


def load_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        cfg.update(read_json(path, "config"))
    return cfg


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def price_time(doc, sym):
    """When this symbol's price is from: detail[sym].time, else .fetched, else the file's as_of."""
    det = ((doc or {}).get("detail") or {}).get(sym) or {}
    for key in ("time", "fetched"):
        t = parse_utc(det.get(key))
        if t:
            return t
    return parse_utc((doc or {}).get("as_of"))


def fresh_price(doc, sym, now_utc, minutes=FRESH_MINUTES):
    """(price, fetched_utc, fresh) for sym from a prices.json doc, or (None, None, False)."""
    if not isinstance(doc, dict):
        return None, None, False
    p = _f((doc.get("prices") or {}).get(sym))
    if p is None or p <= 0:
        return None, None, False
    t = price_time(doc, sym)
    if t is None:
        return p, None, False
    age = (now_utc - t).total_seconds()
    return p, t, -60 <= age <= minutes * 60


# ---------------------------------------------------------------- money helpers

def round_paisa(x):
    """Round to the paisa (half up)."""
    return math.floor(float(x) * 100 + 0.5 + EPS) / 100.0


def shares_for(inr, per_share):
    """Whole shares that fit in inr at per_share, in exact integer paise."""
    u, p = int(round(float(inr) * 100)), int(round(float(per_share) * 100))
    if p <= 0 or u <= 0:
        return 0
    return u // p


def costs_at(qty, buy, sell, segment=SEGMENT):
    """Round-trip charges for qty units bought at buy and sold at sell (Rs)."""
    return india.round_trip(segment, qty, buy, sell)["total"] if qty > 0 else 0.0


def shares_after_costs(max_loss, risk, entry, stop):
    """The most whole shares whose loss at the stop, costs included, fits max_loss."""
    q = shares_for(max_loss, risk)
    while q > 0 and q * risk + costs_at(q, entry, stop) > max_loss + EPS:
        q -= 1
    return q


def _price(x):
    return "%.2f" % round_paisa(x)


def _num(x):
    x = round(float(x), 2)
    if abs(x - round(x)) < 0.005:
        return str(int(round(x)))
    return "%.2f" % x


def money(x):
    return india.inr(x)


# ---------------------------------------------------------------- sizing

def size_trade(plan, cfg, budget=None, limit=None, price=None, cash=None, today=None):
    """Pure function: one plan + your rules -> the sizing (a dict). qty may be 0.

    limit  the limit price for a limit order (None = market)
    price  a FRESH latest price; a market order is sized at the higher of it and the plan's entry
    cash   the cash in trader/portfolio.json, if there is one
    today  IST date (YYYY-MM-DD); a plan for another day is refused"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    if not isinstance(plan, dict):
        raise SizeError("the plan is not a JSON object")
    sym = str(plan.get("symbol") or "").strip().upper()
    if not sym:
        raise SizeError("the plan has no symbol (run bharat-exit)")
    side = str(plan.get("side") or "buy").strip().lower()
    if side != "buy":
        raise SizeError("this pack sizes long (buy) intraday trades only; the plan says %r" % side)
    if today and plan.get("date") and str(plan["date"]) != today:
        raise SizeError("the plan is for %s and today is %s (IST): make a new plan with bharat-exit"
                        % (plan["date"], today))
    entry_plan, stop, target = _f(plan.get("entry")), _f(plan.get("stop")), _f(plan.get("target"))
    for name, v in (("entry", entry_plan), ("stop", stop), ("target", target)):
        if v is None or v <= 0:
            raise SizeError("the plan needs a positive %s (run bharat-exit); a bracket order needs entry, "
                            "stop and target" % name)
    blocked = {str(b).upper() for b in (c.get("blocked") or [])}
    if sym in blocked:
        raise SizeError("%s is on your blocked list" % sym)
    if plan.get("instrument") == "fut":
        return size_futures(plan, c, sym, budget, limit, cash, entry_plan, stop, target)
    if plan.get("instrument") == "opt":
        return size_options(plan, c, sym, budget, limit, cash, entry_plan, stop, target)

    budget = float(budget if budget is not None else (c.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader/config.json (in rupees)")
    buf_pct = float(c.get("cash_buffer_pct") or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    lev = _f(c.get("mis_leverage"))
    if lev is None or not 1 <= lev <= MAX_LEVERAGE:
        raise SizeError("mis_leverage must be between 1 and %d (SEBI's intraday margin is at least 20 %%), not %r"
                        % (MAX_LEVERAGE, c.get("mis_leverage")))

    if limit is not None:
        ref, ref_from = india.tick_round(limit), "limit"
    elif price is not None and round_paisa(price) > round_paisa(entry_plan):
        ref, ref_from = round_paisa(price), "price"
    else:
        ref, ref_from = round_paisa(entry_plan), "plan"
    stop, target = round_paisa(stop), round_paisa(target)
    if stop >= ref:
        raise SizeError("the stop %s is not below the entry %s; a long trade needs its stop underneath"
                        % (_price(stop), _price(ref)))
    if target <= ref:
        raise SizeError("the target %s is not above the entry %s: the move has already happened. "
                        "Skip it or make a new plan with bharat-exit" % (_price(target), _price(ref)))
    risk_p = int(round((ref - stop) * 100))
    reward_p = int(round((target - ref) * 100))
    risk = risk_p / 100.0

    cash_buffer = round(budget * buf_pct / 100.0, 2)
    available = budget - cash_buffer
    cash_note = "(budget %s - %s buffer)" % (money(budget), money(cash_buffer))
    if cash is not None and cash - cash_buffer < available - 0.005:
        available = cash - cash_buffer
        cash_note = "(cash %s - %s buffer)" % (money(cash), money(cash_buffer))
    available = max(round(available, 2), 0.0)
    buying = round(available * lev, 2)
    if lev != 1:
        cash_note += " x %sx MIS" % _num(lev)

    limits = []
    max_loss = _f(c.get("max_loss_per_trade_inr"))
    if max_loss:
        q = shares_after_costs(max_loss, risk, ref, stop)
        limits.append({"name": "max loss", "inr": max_loss, "per": risk, "shares": q,
                       "how": "%s incl. costs: %d x %s + %s costs = %s" % (
                           money(max_loss), q, _price(risk), money(costs_at(q, ref, stop)),
                           money(q * risk + costs_at(q, ref, stop)))})
    cap_pct = _f(c.get("max_position_pct"))
    if cap_pct:
        cap = round(budget * cap_pct / 100.0, 2)
        limits.append({"name": "position cap", "inr": cap, "per": ref, "shares": shares_for(cap, ref),
                       "how": "%s%% of %s = %s / %s" % (_num(cap_pct), money(budget), money(cap), _price(ref))})
    max_order = _f(c.get("max_order_inr"))
    if max_order:
        limits.append({"name": "max order", "inr": max_order, "per": ref, "shares": shares_for(max_order, ref),
                       "how": "%s / %s" % (money(max_order), _price(ref))})
    limits.append({"name": "cash", "inr": buying, "per": ref, "shares": shares_for(buying, ref),
                   "how": "%s %s / %s" % (money(buying), cash_note, _price(ref))})

    qty = max(0, min(l["shares"] for l in limits))
    bound = [l["name"] for l in limits if l["shares"] == qty]
    for l in limits:
        l["binds"] = l["name"] == bound[0]
    value = round(qty * ref, 2)
    costs_stop = round(costs_at(qty, ref, stop), 2)
    costs_target = round(costs_at(qty, ref, target), 2)
    loss = round(qty * risk_p / 100.0 + costs_stop, 2)
    gain = round(qty * reward_p / 100.0 - costs_target, 2)
    refusal = None
    if qty == 0:
        if bound[0] == "max loss":
            refusal = ("one share risks %s (entry %s minus stop %s) plus %s costs, more than your %s max loss per "
                       "trade. No order. Skip this trade, or plan a closer stop with bharat-exit."
                       % (money(risk), _price(ref), _price(stop), money(costs_at(1, ref, stop)), money(max_loss)))
        else:
            refusal = ("one share of %s costs %s, more than your %s limit (%s). No order."
                       % (sym, money(ref), bound[0], limits[[l["name"] for l in limits].index(bound[0])]["how"]))
    elif _f(c.get("min_reward_risk")) and reward_p < _f(c["min_reward_risk"]) * risk_p - EPS:
        refusal = ("reward:risk %.2f (target %s, entry %s, stop %s) is under your minimum %s, so bharat-risk "
                   "would block it. No order. Skip it, or make a new plan with bharat-exit."
                   % (reward_p / float(risk_p), _price(target), _price(ref), _price(stop), _num(c["min_reward_risk"])))
    elif value < float(c.get("min_order_inr") or 0) - 0.005:
        refusal = ("%d share%s of %s is %s, under your %s minimum order. No order."
                   % (qty, "" if qty == 1 else "s", sym, money(value), money(c["min_order_inr"])))
    return {
        "symbol": sym, "side": "buy", "setup": plan.get("setup", ""), "date": plan.get("date"),
        "entry_time": plan.get("entry_time", ""), "entry_plan": round_paisa(entry_plan),
        "entry": ref, "entry_from": ref_from, "stop": stop, "target": target,
        "risk_per_share": risk, "reward_risk": round(reward_p / float(risk_p), 2),
        "stop_label": plan.get("stop_label", ""), "target_label": plan.get("target_label", ""),
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer, "cash_available": available,
        "mis_leverage": lev, "max_loss_per_trade_inr": max_loss, "limits": limits, "qty": qty,
        "bound_by": bound[0], "value": value, "costs_at_stop": costs_stop, "costs_at_target": costs_target,
        "max_loss": loss, "gain_at_target": gain, "flat_by": plan.get("flat_by") or c.get("flat_by"),
        "order_type": "limit" if limit is not None else "market", "refusal": refusal,
    }


def size_futures(plan, c, sym, budget, limit, cash, entry_plan, stop, target):
    """Index futures: whole lots. Limits: max loss (lots x lot x risk + futures costs at the stop),
    margin ((cash - buffer) / margin_per_lot_inr[symbol], never guessed), max_lots, and
    max_fut_notional_inr if you set one. No position cap: a futures lot is worth far more than its margin."""
    if not c.get("allow_fno"):
        raise SizeError("futures are off: set \"allow_fno\": true in trader/config.json to size %s futures "
                        "(F&O can lose more than the margin)" % sym)
    if sym not in FUT_INDEX:
        raise SizeError("P2 sizes NIFTY and BANKNIFTY index futures only, not %s" % sym)
    lot = int(_f(plan.get("lot")) or 0)
    if lot <= 0 or not plan.get("contract"):
        raise SizeError("the futures plan needs its contract and lot (run bharat-entry --instrument fut, then bharat-exit)")
    budget = float(budget if budget is not None else (c.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader/config.json (in rupees)")
    buf_pct = float(c.get("cash_buffer_pct") or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    ref, ref_from = (india.tick_round(limit), "limit") if limit is not None else (round_paisa(entry_plan), "plan")
    stop, target = round_paisa(stop), round_paisa(target)
    if stop >= ref:
        raise SizeError("the stop %s is not below the entry %s" % (_price(stop), _price(ref)))
    if target <= ref:
        raise SizeError("the target %s is not above the entry %s: make a new plan with bharat-exit"
                        % (_price(target), _price(ref)))
    risk_p, reward_p = int(round((ref - stop) * 100)), int(round((target - ref) * 100))
    risk = risk_p / 100.0
    cash_buffer = round(budget * buf_pct / 100.0, 2)
    available = budget - cash_buffer
    cash_note = "(budget %s - %s buffer)" % (money(budget), money(cash_buffer))
    if cash is not None and cash - cash_buffer < available - 0.005:
        available = cash - cash_buffer
        cash_note = "(cash %s - %s buffer)" % (money(cash), money(cash_buffer))
    available = max(round(available, 2), 0.0)

    limits, refusal = [], None
    max_loss = _f(c.get("max_loss_per_trade_inr"))
    if max_loss:
        n = 0
        while (n + 1) * lot * risk + costs_at((n + 1) * lot, ref, stop, "fut") <= max_loss + EPS:
            n += 1
        limits.append({"name": "max loss", "lots": n, "how": "%s incl. costs: %d x %d x %s + %s costs = %s" % (
            money(max_loss), n, lot, _price(risk), money(costs_at(n * lot, ref, stop, "fut")),
            money(n * lot * risk + costs_at(n * lot, ref, stop, "fut")))})
    margins = c.get("margin_per_lot_inr") or {}
    m = _f(margins.get(sym)) if isinstance(margins, dict) else None
    if not m or m <= 0:
        limits.append({"name": "margin", "lots": 0, "how": "no figure for %s" % sym})
        refusal = ("no margin figure for %s: set \"margin_per_lot_inr\": {\"%s\": RUPEES} in trader/config.json "
                   "from Zerodha's margin calculator (zerodha.com/margin-calculator/SPAN). It is never guessed. "
                   "No order." % (sym, sym))
    else:
        limits.append({"name": "margin", "lots": int(available // m + EPS), "how": "%s %s / %s a lot" % (
            money(available), cash_note, money(m))})
    max_lots = 1 if _f(c.get("max_lots")) is None else int(_f(c["max_lots"]))
    limits.append({"name": "max lots", "lots": max_lots, "how": "max_lots in the config"})
    cap = _f(c.get("max_fut_notional_inr"))
    if cap:
        limits.append({"name": "max order", "lots": shares_for(cap, ref * lot),
                       "how": "%s / (%d x %s)" % (money(cap), lot, _price(ref))})
    for l in limits:
        l["shares"] = l["lots"] * lot
    lots = max(0, min(l["lots"] for l in limits))
    bound = [l["name"] for l in limits if l["lots"] == lots]
    for l in limits:
        l["binds"] = l["name"] == bound[0]
    qty = lots * lot
    costs_stop = round(costs_at(qty, ref, stop, "fut"), 2)
    costs_target = round(costs_at(qty, ref, target, "fut"), 2)
    loss = round(qty * risk_p / 100.0 + costs_stop, 2)
    gain = round(qty * reward_p / 100.0 - costs_target, 2)
    if refusal is None and lots == 0:
        if bound[0] == "max loss":
            refusal = ("one lot (%d) risks %s plus %s costs = %s, more than your %s max loss per trade. No order."
                       % (lot, money(lot * risk), money(costs_at(lot, ref, stop, "fut")),
                          money(lot * risk + costs_at(lot, ref, stop, "fut")), money(max_loss)))
        elif bound[0] == "margin":
            refusal = ("one lot needs %s margin, more than the %s you have after the buffer. No order."
                       % (money(m), money(available)))
        else:
            refusal = "your %s limit allows 0 lots. No order." % bound[0]
    elif refusal is None and _f(c.get("min_reward_risk")) and reward_p < _f(c["min_reward_risk"]) * risk_p - EPS:
        refusal = ("reward:risk %.2f is under your minimum %s, so bharat-risk would block it. No order."
                   % (reward_p / float(risk_p), _num(c["min_reward_risk"])))
    return {
        "symbol": sym, "side": "buy", "setup": plan.get("setup", ""), "date": plan.get("date"),
        "entry_time": plan.get("entry_time", ""), "entry_plan": round_paisa(entry_plan),
        "entry": ref, "entry_from": ref_from, "stop": stop, "target": target,
        "risk_per_share": risk, "reward_risk": round(reward_p / float(risk_p), 2),
        "stop_label": plan.get("stop_label", ""), "target_label": plan.get("target_label", ""),
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer, "cash_available": available,
        "mis_leverage": 1, "max_loss_per_trade_inr": max_loss, "limits": limits, "qty": qty, "lots": lots,
        "bound_by": bound[0], "value": round(qty * ref, 2), "costs_at_stop": costs_stop,
        "costs_at_target": costs_target, "max_loss": loss, "gain_at_target": gain,
        "flat_by": plan.get("flat_by") or c.get("flat_by"), "order_type": "limit" if limit is not None else "market",
        "refusal": refusal, "instrument": "fut", "contract": plan["contract"], "lot": lot,
        "expiry": plan.get("expiry"), "margin_per_lot": m, "margin": round(lots * m, 2) if m else None,
        "basis_estimated": bool(plan.get("basis_estimated")), "basis_date": plan.get("basis_date"),
        "fut_price_source": plan.get("fut_price_source") or "bhavcopy",
    }


def size_options(plan, c, sym, budget, limit, cash, entry_plan, stop, target):
    """Index options, bought: whole lots, the premium paid in full from cash (no margin, no leverage).
    Limits: max loss (lots x lot x (premium - premium stop) + option costs at the stop), cash after the buffer,
    max_premium_inr if you set one, max_lots. Always a limit order at the premium (or --limit)."""
    if not c.get("allow_fno"):
        raise SizeError("options are off: set \"allow_fno\": true in trader/config.json to size %s options "
                        "(a bought option can lose all of its premium in hours)" % sym)
    if sym not in FUT_INDEX:
        raise SizeError("P3 sizes NIFTY and BANKNIFTY index options only, not %s" % sym)
    lot = int(_f(plan.get("lot")) or 0)
    if lot <= 0 or not plan.get("contract"):
        raise SizeError("the options plan needs its contract and lot (run bharat-entry --instrument opt, then bharat-exit)")
    budget = float(budget if budget is not None else (c.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: pass --budget or set \"budget\" in trader/config.json (in rupees)")
    buf_pct = float(c.get("cash_buffer_pct") or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")
    ref, ref_from = (india.tick_round(limit), "limit") if limit is not None else (round_paisa(entry_plan), "plan")
    stop, target = round_paisa(stop), round_paisa(target)
    if not stop < ref < target:
        raise SizeError("the premiums need stop < entry < target (stop %s, entry %s, target %s): make a new plan "
                        "with bharat-exit" % (_price(stop), _price(ref), _price(target)))
    risk_p, reward_p = int(round((ref - stop) * 100)), int(round((target - ref) * 100))
    risk = risk_p / 100.0
    cash_buffer = round(budget * buf_pct / 100.0, 2)
    available, cash_note = budget - cash_buffer, "(budget %s - %s buffer)" % (money(budget), money(cash_buffer))
    if cash is not None and cash - cash_buffer < available - 0.005:
        available, cash_note = cash - cash_buffer, "(cash %s - %s buffer)" % (money(cash), money(cash_buffer))
    available = max(round(available, 2), 0.0)
    per_lot = lot * ref
    limits, refusal = [], None
    max_loss = _f(c.get("max_loss_per_trade_inr"))
    if max_loss:
        n = 0
        while (n + 1) * lot * risk + costs_at((n + 1) * lot, ref, stop, "opt") <= max_loss + EPS:
            n += 1
        limits.append({"name": "max loss", "lots": n, "how": "%s incl. costs: %d x %d x %s + %s costs = %s" % (
            money(max_loss), n, lot, _price(risk), money(costs_at(n * lot, ref, stop, "opt")),
            money(n * lot * risk + costs_at(n * lot, ref, stop, "opt")))})
    limits.append({"name": "cash", "lots": int(available // per_lot + EPS), "how": "%s %s / (%d x %s)" % (
        money(available), cash_note, lot, _price(ref))})
    cap = _f(c.get("max_premium_inr"))
    if cap:
        limits.append({"name": "max premium", "lots": int(cap // per_lot + EPS),
                       "how": "%s max_premium_inr / (%d x %s)" % (money(cap), lot, _price(ref))})
    max_lots = 1 if _f(c.get("max_lots")) is None else int(_f(c["max_lots"]))
    limits.append({"name": "max lots", "lots": max_lots, "how": "max_lots in the config"})
    for l in limits:
        l["shares"] = l["lots"] * lot
    lots = max(0, min(l["lots"] for l in limits))
    bound = [l["name"] for l in limits if l["lots"] == lots]
    for l in limits:
        l["binds"] = l["name"] == bound[0]
    qty = lots * lot
    costs_stop = round(costs_at(qty, ref, stop, "opt"), 2)
    costs_target = round(costs_at(qty, ref, target, "opt"), 2)
    loss = round(qty * risk_p / 100.0 + costs_stop, 2)
    gain = round(qty * reward_p / 100.0 - costs_target, 2)
    if lots == 0:
        refusal = {
            "max loss": "one lot (%d) risks %s plus %s costs, more than your %s max loss per trade. No order." % (
                lot, money(lot * risk), money(costs_at(lot, ref, stop, "opt")), money(max_loss or 0)),
            "cash": "one lot costs %s of premium, more than the %s you have after the buffer. No order." % (
                money(per_lot), money(available)),
            "max premium": "one lot costs %s of premium, over your %s max_premium_inr. No order." % (
                money(per_lot), money(cap or 0)),
        }.get(bound[0], "your %s limit allows 0 lots. No order." % bound[0])
    elif _f(c.get("min_reward_risk")) and reward_p < _f(c["min_reward_risk"]) * risk_p - EPS:
        refusal = ("reward:risk %.2f is under your minimum %s, so bharat-risk would block it. No order."
                   % (reward_p / float(risk_p), _num(c["min_reward_risk"])))
    keep = ("contract", "expiry", "strike", "right", "iv", "forward_ratio", "rate", "oi", "volume", "illiquid",
            "index_entry", "index_stop", "index_target", "index_side", "bhav_date")
    out = {
        "symbol": sym, "side": "buy", "setup": plan.get("setup", ""), "date": plan.get("date"),
        "entry_time": plan.get("entry_time", ""), "entry_plan": round_paisa(entry_plan),
        "entry": ref, "entry_from": ref_from, "stop": stop, "target": target,
        "risk_per_share": risk, "reward_risk": round(reward_p / float(risk_p), 2),
        "stop_label": plan.get("stop_label", ""), "target_label": plan.get("target_label", ""),
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer, "cash_available": available,
        "mis_leverage": 1, "max_loss_per_trade_inr": max_loss, "limits": limits, "qty": qty, "lots": lots,
        "bound_by": bound[0], "value": round(qty * ref, 2), "costs_at_stop": costs_stop,
        "costs_at_target": costs_target, "max_loss": loss, "gain_at_target": gain,
        "flat_by": plan.get("flat_by") or c.get("flat_by"), "order_type": "limit", "refusal": refusal,
        "instrument": "opt", "lot": lot, "premium_source": plan.get("premium_source") or "estimated",
        "stop_limit_ticks": int(_f(c.get("stop_limit_ticks")) or 10),
    }
    out.update({k: plan.get(k) for k in keep})
    return out


def bracket_order(s):
    """The one proposed order: an NSE intraday (MIS) buy with its stop-loss and take-profit."""
    if s["refusal"] or s["qty"] <= 0:
        return None
    o = {"symbol": s["symbol"], "exchange": "NSE", "product": "MIS", "side": "buy", "qty": str(int(s["qty"])),
         "type": s["order_type"], "time_in_force": "day"}
    if s.get("instrument") == "fut":
        o.update(symbol=s["contract"], exchange="NFO", instrument="fut", underlying=s["symbol"], lots=s["lots"],
                 lot=s["lot"], expiry=s["expiry"], margin=s["margin"], basis_estimated=s["basis_estimated"],
                 basis_date=s["basis_date"], fut_price_source=s["fut_price_source"])
    if s.get("instrument") == "opt":
        o.update(symbol=s["contract"], exchange="NFO", instrument="opt", underlying=s["symbol"], lots=s["lots"],
                 lot=s["lot"], expiry=s["expiry"], strike=s["strike"], right=s["right"],
                 premium_source=s["premium_source"], oi=s["oi"], illiquid=s["illiquid"], iv=s["iv"])
    if s["order_type"] == "limit":
        o["limit_price"] = _price(s["entry"])
    if s.get("instrument") == "opt":
        # NSE takes no SL-M on options: the stop is SL, its limit stop_limit_ticks under the trigger (at least a tick)
        o["stop_loss_limit_price"] = _price(max(round_paisa(s["stop"] - 0.05 * s["stop_limit_ticks"]), 0.05))
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "intraday trade")
    why = ("%s on %s: %d %s, bound by your %s (%d x %s + %s costs = %s at the stop)"
           % (setup, s["symbol"], s["qty"], "units" if s.get("instrument") in ("fut", "opt") else "shares",
              {"max loss": "%s max loss per trade" % money(s["max_loss_per_trade_inr"] or 0),
               "position cap": "position cap", "max order": "max order", "margin": "margin",
               "max lots": "max lots", "cash": "cash after the buffer",
               "max premium": "max premium"}[s["bound_by"]],
              s["qty"], _price(s["risk_per_share"]), money(s["costs_at_stop"]), money(s["max_loss"])))
    o.update({
        "order_class": "bracket",
        "take_profit_limit_price": _price(s["target"]),
        "stop_loss_stop_price": _price(s["stop"]),
        "why": why,
        "plan": {"entry": s["entry"], "stop": s["stop"], "target": s["target"],
                 "risk_per_share": s["risk_per_share"], "max_loss": s["max_loss"],
                 "costs_at_stop": s["costs_at_stop"], "costs_at_target": s["costs_at_target"],
                 "setup": s["setup"], "entry_time": s["entry_time"], "flat_by": s["flat_by"]},
    })
    return o


def fut_report(s, order, notes=()):
    lines = ["%s futures buy (%s, expires %s, lot %d), %s, entry %s%s" % (
        s["symbol"], s["contract"], s["expiry"], s["lot"], SETUP_WORDS.get(s["setup"], s["setup"] or "setup"),
        _price(s["entry_plan"]), (" at " + s["entry_time"]) if s["entry_time"] else "")]
    lines.append("Stop %s. Target %s. Risk %s a unit, %s a lot. Reward:risk %.2f." % (
        _price(s["stop"]), _price(s["target"]), _price(s["risk_per_share"]),
        money(s["risk_per_share"] * s["lot"]), s["reward_risk"]))
    if s["fut_price_source"] != "user" and s["order_type"] == "market":
        lines.append("The entry is the basis estimate, not a live price: bharat-risk blocks it as a market order. "
                     "Use --limit (a limit at the plan's entry), or give the live price (bharat-exit --entry).")
    if s["basis_estimated"]:
        lines.append("The futures price is ESTIMATED from an old basis (%s): bharat-risk blocks a market entry "
                     "on it. Give the live price (bharat-exit --entry) first." % s["basis_date"])
    lines += list(notes) + ["", "Limits (whole lots; the smallest one wins):"]
    for l in s["limits"]:
        lines.append("  %-10s %-66s %3d lot%s%s" % (l["name"], l["how"], l["lots"], "" if l["lots"] == 1 else "s",
                                                    "  <- binds" if l["binds"] else ""))
    lines.append("")
    if order is None:
        lines += ["No order: " + s["refusal"], "Education and tooling, not financial advice."]
        return "\n".join(lines)
    lines.append("Size: %d lot%s of %s = %d units, %s notional, %s margin blocked (MIS = NRML for F&O)." % (
        s["lots"], "" if s["lots"] == 1 else "s", s["contract"], s["qty"], money(s["value"]), money(s["margin"])))
    lines.append("Costs, round trip (Zerodha futures): %s at the stop, %s at the target." % (
        money(s["costs_at_stop"]), money(s["costs_at_target"])))
    lines.append("Max loss at the stop: %d x %s + %s costs = %s. At the target, after costs: %s." % (
        s["qty"], _price(s["risk_per_share"]), money(s["costs_at_stop"]), money(s["max_loss"]),
        money(s["gain_at_target"])))
    lines.append("Futures are leveraged: a gap can lose more than the margin, and more than the max loss.")
    lines.append("")
    lines.append("Proposed bracket order (nothing has been placed):")
    lines.append("  buy %s %s on NFO, MIS, %s, day, take-profit %s, stop-loss %s" % (
        order["qty"], order["symbol"], "limit %s" % order["limit_price"] if order["type"] == "limit" else "market",
        order["take_profit_limit_price"], order["stop_loss_stop_price"]))
    lines += ["", "Next: bharat-risk checks it, then bharat-broker asks you yes or no.",
              "Education and tooling, not financial advice."]
    return "\n".join(lines)


def opt_report(s, order, notes=()):
    lines = ["%s option buy: %s (%s %g, expires %s, lot %d), %s on the index, premium %s%s" % (
        s["symbol"], s["contract"], "call" if s["right"] == "CE" else "put", s["strike"], s["expiry"], s["lot"],
        SETUP_WORDS.get(s["setup"], s["setup"] or "setup"), _price(s["entry"]),
        (" at " + s["entry_time"]) if s["entry_time"] else "")]
    lines.append("Premium stop %s. Premium target %s. Risk %s a unit, %s a lot. Reward:risk %.2f." % (
        _price(s["stop"]), _price(s["target"]), _price(s["risk_per_share"]), money(s["risk_per_share"] * s["lot"]),
        s["reward_risk"]))
    if s["premium_source"] != "user":
        lines.append("The premium is ESTIMATED from the last bhavcopy: bharat-risk blocks it. Read the live premium "
                     "on Kite and re-plan with it (bharat-exit --entry PREMIUM).")
    lines += list(notes) + ["", "Limits (whole lots; the smallest one wins):"]
    for l in s["limits"]:
        lines.append("  %-11s %-66s %3d lot%s%s" % (l["name"], l["how"], l["lots"], "" if l["lots"] == 1 else "s",
                                                    "  <- binds" if l["binds"] else ""))
    lines.append("")
    if order is None:
        lines += ["No order: " + s["refusal"], "Education and tooling, not financial advice."]
        return "\n".join(lines)
    lines.append("Size: %d lot%s of %s = %d units, %s of premium, paid in full (no margin)." % (
        s["lots"], "" if s["lots"] == 1 else "s", s["contract"], s["qty"], money(s["value"])))
    lines.append("Costs, round trip (Zerodha options): %s at the stop, %s at the target." % (
        money(s["costs_at_stop"]), money(s["costs_at_target"])))
    lines.append("Max loss at the stop: %d x %s + %s costs = %s. At the target, after costs: %s." % (
        s["qty"], _price(s["risk_per_share"]), money(s["costs_at_stop"]), money(s["max_loss"]),
        money(s["gain_at_target"])))
    lines.append("A bought option can lose all of its premium (%s) in hours, more so on expiry day." % money(s["value"]))
    lines += ["", "Proposed order (nothing has been placed):",
              "  buy %s %s on NFO, MIS, limit %s, day; then a stop-limit at %s; target %s (an alert, not an order)" % (
                  order["qty"], order["symbol"], order["limit_price"], order["stop_loss_stop_price"],
                  order["take_profit_limit_price"]),
              "", "Next: bharat-risk checks it, then bharat-broker asks you yes or no.",
              "Education and tooling, not financial advice."]
    return "\n".join(lines)


def plan_report(s, order, notes=()):
    if s.get("instrument") == "fut":
        return fut_report(s, order, notes)
    if s.get("instrument") == "opt":
        return opt_report(s, order, notes)
    lines = []
    setup = SETUP_WORDS.get(s["setup"], s["setup"] or "setup")
    lines.append("%s buy, %s, entry %s%s" % (s["symbol"], setup, _price(s["entry_plan"]),
                                            (" at " + s["entry_time"]) if s["entry_time"] else ""))
    lines.append("Stop %s%s. Target %s%s." % (_price(s["stop"]), (": " + s["stop_label"]) if s["stop_label"] else "",
                                             _price(s["target"]),
                                             (": " + s["target_label"]) if s["target_label"] else ""))
    for n in notes:
        lines.append(n)
    src = {"plan": "the plan's entry", "price": "the latest price, above the plan's entry",
           "limit": "your limit price"}[s["entry_from"]]
    lines.append("Sized at %s (%s). Risk %s a share (entry %s minus stop %s). Reward:risk %.2f."
                 % (_price(s["entry"]), src, _price(s["risk_per_share"]), _price(s["entry"]), _price(s["stop"]),
                    s["reward_risk"]))
    lines.append("")
    lines.append("Limits (whole shares; the smallest one wins):")
    for l in s["limits"]:
        lines.append("  %-13s %-62s %4d shares%s" % (l["name"], l["how"], l["shares"],
                                                      "  <- binds" if l["binds"] else ""))
    lines.append("")
    if order is None:
        lines.append("No order: " + s["refusal"])
        lines.append("Education and tooling, not financial advice.")
        return "\n".join(lines)
    lines.append("Size: %d shares of %s = %s at %s (MIS, intraday)." % (
        s["qty"], s["symbol"], money(s["value"]), _price(s["entry"])))
    lines.append("Costs, round trip (Zerodha): %s if it exits at the stop, %s at the target." % (
        money(s["costs_at_stop"]), money(s["costs_at_target"])))
    lines.append("Max loss at the stop: %d x %s + %s costs = %s. At the target, after costs: %s." % (
        s["qty"], _price(s["risk_per_share"]), money(s["costs_at_stop"]), money(s["max_loss"]),
        money(s["gain_at_target"])))
    lines.append("A stop is an order, not a promise: in a fast market or a band freeze it can fill below %s."
                 % _price(s["stop"]))
    lines.append("")
    lines.append("Proposed bracket order (nothing has been placed):")
    how = "limit %s" % order["limit_price"] if order["type"] == "limit" else "market"
    lines.append("  buy %s %s on NSE, MIS, %s, day, take-profit %s, stop-loss %s"
                 % (order["qty"], order["symbol"], how, order["take_profit_limit_price"],
                    order["stop_loss_stop_price"]))
    if s["flat_by"]:
        lines.append("Flat by %s IST: anything still open then gets closed (bharat-alerts rings, "
                     "bharat-broker flatten), before the broker's 15:20 square-off." % s["flat_by"])
    lines.append("")
    lines.append("Next: bharat-risk checks it, then bharat-broker asks you yes or no.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


def write_orders(path, orders, cfg, source="", now_utc=None):
    doc = {
        "created_by": "bharat-budget",
        "created_at": utc_iso(now_utc),
        "status": "proposed",
        "paper": bool(cfg.get("paper", True)),
        "note": ("Proposals only. Nothing is placed until bharat-risk passes each order "
                 "and you say yes to it in the chat."),
        "source": source,
        "orders": orders,
    }
    india.write_json(path, doc)


# ---------------------------------------------------------------- main

def run_plan(a, cfg):
    now = parse_now(a.now)
    today = to_local(now).date().isoformat()
    plan = read_json(a.plan, "plan (run bharat-exit first)")
    limit = None
    if a.limit is not None:
        if a.limit == "plan":
            limit = _f((plan or {}).get("entry"))
        else:
            limit = _f(a.limit)
            if limit is None or limit <= 0:
                raise SizeError("--limit wants a price, like --limit 1228.00")
    sym = str((plan or {}).get("symbol") or "").upper()
    notes, price = [], None
    ppath = a.prices or ("trader/prices.json" if os.path.exists("trader/prices.json") else None)
    # ponytail: prices.json holds the index (NIFTY), not the contract: a futures plan is sized at its own entry
    if ppath and limit is None and sym and (plan or {}).get("instrument") not in ("fut", "opt"):
        pdoc = read_json(ppath, "prices")
        p, t, fresh = fresh_price(pdoc, sym, now)
        if p is not None and fresh:
            price = p
            if round_paisa(p) > round_paisa(_f(plan.get("entry")) or 0):
                notes.append("%s last %s (as of %s IST) is above the plan's entry %s, so the risk "
                             "is bigger: sized at %s." % (sym, _price(p), to_local(t).strftime("%H:%M"),
                                                          _price(plan.get("entry")), _price(p)))
        elif p is not None:
            notes.append("%s in %s is %s: not used (older than %d minutes). Sized at the plan's entry."
                         % (sym, ppath, ("from " + to_local(t).strftime("%Y-%m-%d %H:%M") + " IST")
                            if t else "undated", FRESH_MINUTES))
    cash = None
    if a.portfolio and os.path.exists(a.portfolio):
        pf = read_json(a.portfolio, "portfolio")
        cash = _f(pf.get("cash")) if isinstance(pf, dict) else None
    s = size_trade(plan, cfg, a.budget, limit, price, cash, today)
    order = bracket_order(s)
    if order and a.write:
        src = "trader/plan.json (%s %s %s)" % (s["symbol"], s["setup"] or "plan", s["date"] or today)
        write_orders(a.write, [order], cfg, src, now)
    if a.json:
        print(json.dumps({"sizing": s, "orders": [order] if order else [], "notes": notes}, indent=2))
    else:
        print(plan_report(s, order, notes))
        if order and a.write:
            print("Wrote 1 proposed bracket order to %s" % a.write)
    return 0 if order else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Size an intraday trade to your budget and max loss (bharat-budget).")
    ap.add_argument("--plan", default="trader/plan.json", help="the trade plan (default trader/plan.json)")
    ap.add_argument("--config", default="trader/config.json", help="rules (default trader/config.json)")
    ap.add_argument("--budget", type=float, help="override the budget in the config (Rs)")
    ap.add_argument("--limit", nargs="?", const="plan", metavar="PRICE",
                    help="a limit order instead of market, at the plan's entry or at PRICE")
    ap.add_argument("--prices", help="prices JSON (default trader/prices.json if it exists)")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; its cash caps the size if it exists")
    ap.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-19 09:51\"")
    ap.add_argument("--write", nargs="?", const="trader/orders.json", metavar="PATH",
                    help="write the proposed order (default trader/orders.json)")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the report")
    a = ap.parse_args(argv)
    try:
        return run_plan(a, load_config(a.config))
    except SizeError as e:
        print("size.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
