#!/usr/bin/env python3
"""Pre-trade risk check for bharat-risk. Standard library only, no network.

Reads the proposed orders (trader/orders.json) and checks every one against
your rules (trader/config.json), your account (trader/portfolio.json),
prices (trader/prices.json) and what was placed today (trader/state/broker.json).
Each order gets PASS or BLOCK with the reasons. Orders are checked in file
order as if each PASS had filled, so buys that are fine one by one but break
a limit together are caught.

    python3 check.py                                 # the trader/ files, writes trader/state/risk.json
    python3 check.py --now "2026-10-19 09:51"        # pin the clock (IST)
    python3 check.py --orders trader/orders.json --json
    python3 check.py --no-write                      # check without recording the verdicts

Entries and exits. An ENTRY opens or adds to a position (a buy, or a sell
that would go short). An EXIT only reduces what you hold. Getting out must
always be possible: an exit is checked for format, asset and short sales
only; every other rule turns into a warning for it. An F&O order must be on
NFO, and an option is closed at a limit, never market: both are format
rules, and a limit sell still gets you out.

What blocks an order (every order):

  format        missing symbol or side, no whole-share qty (NSE orders are in
                shares; notional orders do not exist), a limit order without a
                limit price, unknown type; a bracket that misses a leg
  asset         F&O contracts (NIFTY26NOVFUT, ...CE / PE) unless
                "allow_fno": true (P1 trades cash stocks only); anything that
                is not an NSE symbol
  short         selling more than you hold, unless "allow_short": true

What blocks an entry (on top):

  product       anything but MIS (intraday): P1 plans intraday trades only
  blocked       a symbol on your "blocked" list
  size          under min_order_inr, over max_order_inr
  position      the stock would be over max_position_pct of the account
  cash          the buy would dip into your cash buffer, or need more than
                mis_leverage x your cash (broker margin)
  duplicate     the same symbol and side twice in this batch, or already
                placed today (trader/state/broker.json)
  daily limit   max_orders_per_day orders placed today
  stop          no stop while "require_stop": true (a bracket stop_loss_stop_price
                or the order's plan stop); a stop on the wrong side of the entry;
                a market or limit order whose fresh price is already past the
                stop (it would fill past it, and the stop order be rejected)
  loss          qty x (entry - stop) + round-trip costs at the stop over
                max_loss_per_trade_inr. The entry is the limit price for a
                limit order, else the HIGHER of the plan's entry and a fresh
                price (under 15 minutes old), the LOWER for a short sell, so a
                stale plan cannot understate the risk (the size checks value a
                short at the higher price)
  reward:risk   (target - entry) / (entry - stop) under min_reward_risk
  costs         round-trip costs at the target over max_cost_share (0.25) of
                the reward at the target: on a small trade the charges eat
                most of what it can make
  daily loss    the max losses at the stops of today's entries plus this one
                over max_daily_loss_inr (win or lose, each counts in full)
  trades        max_trades_per_day entries already placed today
  window        before no_entries_before or after no_entries_after (IST), or
                NSE is closed (weekend, NSE holiday, outside the session)
  band          the entry at or over the stock's upper price band (worked out
                from the previous close and the band in trader/entry.json):
                a stock frozen at its band can leave you unable to exit.
                Within 1 % of the band is a warning

Exit code 0 when every order passes, 1 when any order is blocked.
The verdicts go to trader/state/risk.json with a fingerprint of each order,
so bharat-broker only shows an order as ready if THIS exact order passed today.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

import india

DEFAULTS = {
    "budget": None, "cash_buffer_pct": 10, "max_position_pct": 75, "max_order_inr": 200000,
    "min_order_inr": 500, "max_loss_per_trade_inr": 1000, "max_daily_loss_inr": 3000, "max_trades_per_day": 3,
    "max_orders_per_day": 10, "min_reward_risk": 1.0, "max_cost_share": 0.25, "require_stop": True,
    "no_entries_before": "09:30", "no_entries_after": "14:45", "flat_by": "15:15", "mis_leverage": 1,
    "paper": True, "blocked": [], "allow_fno": False, "allow_margin": False, "allow_short": False,
    "margin_per_lot_inr": {}, "max_lots": 1, "max_premium_inr": None, "min_option_oi": 50000,
    "expiry_day_cutoff": "13:00", "max_option_cost_share": 0.10,
}
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
FUT_NAME = re.compile(r"^([A-Z0-9&\-]+?)(\d{2})([A-Z]{3})FUT$")   # digits too: 360ONE26NOVFUT, NIFTYNXT5026NOVFUT
ORDER_FIELDS = ("symbol", "exchange", "product", "side", "qty", "notional", "type", "time_in_force", "limit_price",
                 "stop_price", "trail_price", "trail_percent", "extended_hours", "client_order_id",
                 "order_class", "take_profit_limit_price", "stop_loss_stop_price",
                 "stop_loss_limit_price", "advanced_instructions")
PRICE_FIELDS = ("qty", "notional", "limit_price", "stop_price", "take_profit_limit_price",
                "stop_loss_stop_price", "stop_loss_limit_price")
TYPES = {"market", "limit", "stop", "stop_limit", "trailing_stop"}
TIFS = {"day", "gtc", "opg", "cls", "ioc", "fok"}
FNO = re.compile(r"^[A-Z&\-]+\d{2}([A-Z]{3}FUT|[A-Z0-9]{3}\d+(CE|PE))$")
SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&\-]{0,19}$")
NEAR_BAND = 0.01
HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
CENT = 0.005
# futures fields that change the verdict: editing one after a PASS must change the fingerprint
FUT_FIELDS = ("instrument", "underlying", "lot", "lots", "expiry", "basis_estimated", "basis_date",
              "fut_price_source", "right", "strike", "premium_source", "oi", "illiquid", "iv")
FRESH_MINUTES = 15
REF_WORDS = {"plan": "the plan's entry", "price": "the latest price", "limit": "the limit price",
             "stop price": "the stop price", "stale price": "an old price"}

class RiskError(Exception):
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
        raise RiskError("--now wants a time like \"2026-10-19 09:51\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return india.ist_to_utc(dt).replace(tzinfo=None)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_utc(text):
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def hhmm(value, key):
    """"09:30" -> time(9, 30); None or "" -> None (that bound is off)."""
    if value in (None, "", False):
        return None
    m = HHMM.match(str(value).strip())
    if not m:
        raise RiskError("%s must be an IST time like \"09:30\", got %r" % (key, value))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def window_check(now_ist, c):
    """Reasons an entry cannot be placed now (IST). Empty when the window is off."""
    before = hhmm(c.get("no_entries_before"), "no_entries_before")
    after = hhmm(c.get("no_entries_after"), "no_entries_after")
    d, t = now_ist.date(), now_ist.time().replace(second=0, microsecond=0)
    ok, why = india.trading_day(d)
    stamp = now_ist.strftime("%a %Y-%m-%d %H:%M")
    if not ok:
        return ["window: NSE is closed today (%s, %s IST)" % (why, stamp)], []
    o, close = [x.time() for x in india.session(d)]
    if t < o or now_ist.time() >= close:
        return ["window: NSE is closed (%s IST; the session is %s to %s)"
                % (stamp, o.strftime("%H:%M"), close.strftime("%H:%M"))], []
    if before is not None and t < before:
        return ["window: no new entries before %s IST (it is %s)" % (before.strftime("%H:%M"),
                                                                     t.strftime("%H:%M"))], []
    if after is not None and t > after:
        return ["window: no new entries after %s IST (it is %s)" % (after.strftime("%H:%M"),
                                                                    t.strftime("%H:%M"))], []
    return [], []


# ---------------------------------------------------------------- files and helpers

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise RiskError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise RiskError("%s is not valid JSON: %s" % (path, e))


def fingerprint(order):
    """Same function in bharat-broker/ticket.py: the exact order that was checked."""
    core = {k: order[k] for k in ORDER_FIELDS + FUT_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def money(x):
    return india.inr(x)


def costs(qty, buy, sell, segment="eq_intraday"):
    """Round-trip Zerodha charges for qty shares or units (Rs)."""
    return india.round_trip(segment, qty, buy, sell)["total"] if qty and qty > 0 else 0.0


def next_contract(name):
    """NIFTY26NOVFUT -> NIFTY26DECFUT (the next monthly future); None if it is not a futures name."""
    m = FUT_NAME.match(name or "")
    if not m or m.group(3) not in MONTHS:
        return None
    i, yy = MONTHS.index(m.group(3)) + 1, int(m.group(2))
    return "%s%02d%sFUT" % (m.group(1), yy + i // 12, MONTHS[i % 12])


def _cent(x):
    """A price to the cent, half up (366.475 -> 366.48)."""
    return int(float(x) * 100 + 0.5 + 1e-9) / 100.0


def _p(x):
    return "%.2f" % float(x)


def account(cfg, portfolio, prices):
    """Starting account: cash and position values. No portfolio = a fresh
    account funded with the budget."""
    warnings = []
    if portfolio is None:
        budget = _f(cfg.get("budget"))
        if not budget:
            raise RiskError("no trader/portfolio.json and no budget in the config: "
                            "nothing to check the orders against")
        return {"cash": budget, "shares": {}, "values": {}}, warnings
    cash = _f(portfolio.get("cash"))
    if cash is None:
        raise RiskError("portfolio.json needs a \"cash\" number")
    shares, values = {}, {}
    for p in portfolio.get("positions", []):
        t = str(p.get("ticker") or p.get("symbol") or "").upper()
        if not t:
            continue
        n = _f(p.get("shares", p.get("qty")))
        if n is None:
            n = sum(_f(l.get("shares")) or 0 for l in p.get("lots", []))
        shares[t] = shares.get(t, 0) + n
        if prices.get(t):
            values[t] = shares[t] * prices[t]
        else:
            basis = sum(_f(l.get("cost")) or 0 for l in p.get("lots", []))
            values[t] = basis
            warnings.append("no price for %s: valued at its cost basis %s" % (t, money(basis)))
    return {"cash": cash, "shares": shares, "values": values}, warnings


def rec_day(rec):
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


def rec_is_entry(rec):
    """bharat-broker logs the kind bharat-risk gave; older records: a buy is an entry."""
    k = rec.get("kind")
    if k:
        return k == "entry"
    return str(rec.get("side", "")).lower() == "buy"


def placed_today(state, today):
    """Orders recorded as placed today by bharat-broker."""
    out = []
    for rec in (state or {}).get("orders", []):
        if rec.get("status") == "placed" and rec_day(rec) == today:
            out.append(rec)
    return out


# ---------------------------------------------------------------- the check

def check(orders, cfg, portfolio=None, prices=None, state=None, today=None, now=None, price_times=None,
          bands=None):
    """Pure function. Returns {"results": [...], "pass": n, "block": n, "warnings": [...]}.

    now          naive UTC (default: the real clock); its IST date is "today"
    price_times  {SYM: naive UTC time of that price}; None = every price counts as fresh
    bands        {SYM: {"band": pct, "prev_close": price}} for the price-band check"""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    prices = {str(k).upper(): float(v) for k, v in (prices or {}).items() if _f(v)}
    now_utc = now or parse_now(None)
    now_ist = to_local(now_utc)
    today = today or now_ist.date().isoformat()
    bands = {str(k).upper(): v for k, v in (bands or {}).items()}
    lev = _f(c.get("mis_leverage")) or 1.0
    acct, top_warn = account(c, portfolio, prices)
    blocked = {str(b).upper() for b in c.get("blocked") or []}
    done_today = placed_today(state, today)
    count_today = len(done_today)
    entries_today = [r for r in done_today if rec_is_entry(r)]
    loss_today = 0.0
    for r in entries_today:
        ml = _f(r.get("max_loss"))
        if ml is None:
            top_warn.append("an entry placed today (%s %s) has no max loss in the log; not counted in "
                            "the daily loss" % (r.get("side"), r.get("symbol")))
        else:
            loss_today += ml
    placed_keys = {(str(r.get("symbol", "")).upper(), r.get("side")) for r in done_today}
    win_reasons, win_warns = window_check(now_ist, c)
    seen = set()
    results = []
    batch_entries, batch_loss = 0, 0.0
    batch_margin = {}                    # futures margin blocked by buys earlier in this batch
    if not c.get("paper", True):
        top_warn.append("LIVE account (\"paper\": false): these orders use real money")
    top_warn.extend(win_warns)

    def fresh(sym):
        if sym not in prices:
            return None
        if price_times is None:
            return prices[sym]
        t = price_times.get(sym)
        if t is None:
            return None
        age = (now_utc - t).total_seconds()
        return prices[sym] if -60 <= age <= FRESH_MINUTES * 60 else None

    for i, o in enumerate(orders, 1):
        reasons, warns, info = [], [], []
        later = []                       # entry-only reasons, turned into warnings for an exit
        sym = str(o.get("symbol", "")).strip().upper()
        side = str(o.get("side", "")).strip().lower()
        typ = str(o.get("type", "market")).strip().lower()
        tif = str(o.get("time_in_force", "day")).strip().lower()
        qty, notional = o.get("qty"), o.get("notional")
        plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}
        oc = str(o.get("order_class") or "").strip().lower()
        legs = o.get("take_profit_limit_price") is not None or o.get("stop_loss_stop_price") is not None
        bracket = oc == "bracket" or (not oc and legs)
        value = None
        fno = o.get("instrument") in ("fut", "opt") or str(o.get("exchange") or "").upper() == "NFO"
        name = india.parse_option_symbol(sym)
        opt = o.get("instrument") == "opt" or name is not None
        fno = fno or opt or bool(FUT_NAME.match(sym))
        fut = fno and not opt
        fut_name = FUT_NAME.match(sym)
        known = name is not None or bool(fut_name and fut_name.group(3) in india.MONTHS)
        # F&O-shaped but unreadable (a bad month code, a weekly dated 31 Nov), or on NFO with nothing to classify it.
        # Not "instrument: opt" alone: real contracts like 360ONE26NOV1200CE have digits the parser cannot split.
        # "instrument: fut" too: every real futures name reads (FUT_NAME takes digits), so one that does not is a typo
        unknown_nfo = not known and (bool(FNO.match(sym)) or o.get("instrument") == "fut"
                                     or (fno and not o.get("instrument")))
        seg = "opt" if opt else "fut" if fut else "eq_intraday"
        lot = int(_f(o.get("lot")) or 0) if fno else 0
        lots = None
        lot_missing = None

        # format
        if not sym:
            reasons.append("format: no symbol")
        if side not in ("buy", "sell"):
            reasons.append("format: side must be buy or sell, got %r" % o.get("side"))
        if notional is not None:
            reasons.append("format: NSE orders are in whole shares; a notional (rupee amount) order does not exist")
        elif qty is None:
            reasons.append("format: no qty")
        elif _f(qty) is not None and abs(_f(qty) - round(_f(qty))) > 1e-9:
            reasons.append("format: NSE orders take whole shares only, got qty %r" % qty)
        elif fno:
            if lot <= 0:   # an entry only: getting out never waits on a lot size
                lot_missing = "format: an F&O entry needs its lot size (bharat-budget writes it)"
            elif int(round(_f(qty) or 0)) % lot:
                reasons.append("format: %s trades in whole lots, a lot of %d units; qty %s is not" % (sym, lot, qty))
            else:
                lots = int(round(_f(qty))) // lot
        for k in PRICE_FIELDS:
            if o.get(k) is not None:
                v = _f(o.get(k))
                if v is None or v <= 0:
                    reasons.append("format: %s must be a positive number, got %r" % (k, o.get(k)))
                elif not isinstance(o.get(k), str):
                    warns.append("send %s as a string (\"%s\"), the way place_stock_order types it"
                                 % (k, o.get(k)))
        if typ not in TYPES:
            reasons.append("format: unknown order type %r" % typ)
        if tif not in TIFS:
            reasons.append("format: unknown time_in_force %r" % tif)
        if typ in ("limit", "stop_limit") and o.get("limit_price") is None:
            reasons.append("format: a %s order needs a limit_price" % typ)
        if typ in ("stop", "stop_limit") and o.get("stop_price") is None:
            reasons.append("format: a %s order needs a stop_price" % typ)
        if o.get("extended_hours") and not (typ == "limit" and tif in ("day", "gtc")):
            reasons.append("format: extended hours needs a limit order with time_in_force day or gtc")
        if o.get("stop_loss_limit_price") is not None and o.get("stop_loss_stop_price") is None:
            reasons.append("format: stop_loss_limit_price needs a stop_loss_stop_price")
        if bracket:
            if tif not in ("day", "gtc"):
                reasons.append("format: a bracket order needs time_in_force day or gtc, got %r" % tif)
            if o.get("take_profit_limit_price") is None or o.get("stop_loss_stop_price") is None:
                reasons.append("format: a bracket order needs both take_profit_limit_price and "
                               "stop_loss_stop_price")
        elif oc or o.get("advanced_instructions"):
            warns.append("advanced order (%s): check every leg before you say yes"
                         % (oc or "advanced_instructions"))

        exch = str(o.get("exchange") or "").strip().upper()
        if (fut or opt) and exch != "NFO":
            reasons.append("format: %s is an F&O contract and goes on NFO, not %s" % (sym, exch or "no exchange"))
        if opt and typ == "market":   # format, so an exit blocks too: a limit sell still gets you out
            reasons.append("format: an option is bought and sold at a limit, never market (a market order can fill "
                           "far from its value); to close it: ticket.py flatten --symbol %s --limit PRICE" % sym)

        # asset
        if unknown_nfo:   # fail closed: an F&O name this pack cannot read skips no checks, exits included
            reasons.append("asset: %s is not an NFO contract name this pack can read (a future like NIFTY26OCTFUT, a "
                           "monthly option like NIFTY26OCT23100CE, a weekly like NIFTY26O2023100CE): check the "
                           "month code and the date" % sym)
        if sym and (fno or FNO.match(sym.replace(" ", ""))):
            if not c.get("allow_fno"):
                reasons.append("asset: %s is an F&O contract; P1 trades cash stocks only "
                               "(\"allow_fno\": false)" % sym)
            else:
                warns.append("F&O order: futures and options can lose more than the margin")
        elif sym and not SYMBOL.match(sym):
            reasons.append("asset: %s does not look like an NSE symbol" % sym)

        # the reference price: what one share costs to get in
        plan_entry = _f(plan.get("entry"))
        live = fresh(sym)
        ref, ref_from = None, None
        size_ref = None   # what the order is worth: the higher price, a short's too (its loss uses the lower)
        if typ in ("limit", "stop_limit"):
            ref, ref_from = _f(o.get("limit_price")), "limit"
        elif typ == "stop":
            ref, ref_from = _f(o.get("stop_price")), "stop price"
        if ref is None:
            cands = [(p, w) for p, w in ((plan_entry, "plan"), (live, "price")) if p]
            if cands:
                # the price further from the stop: higher for a buy, lower for a sell (a short's stop is above it)
                ref, ref_from = (min if side == "sell" else max)(cands, key=lambda x: x[0])
                ref = _cent(ref)
                size_ref = _cent(max(p for p, _ in cands))
                if live and ref_from == "price" and plan_entry and abs(live - plan_entry) > CENT:
                    info.append("%s last %s is %s the plan's entry %s: the risk is measured from %s"
                                % (sym, _p(ref), "below" if side == "sell" else "above", _p(plan_entry), _p(ref)))
            elif sym in prices:
                ref, ref_from = prices[sym], "stale price"
        if plan_entry and live is None and sym in prices and price_times is not None and typ == "market":
            warns.append("the %s price in trader/prices.json is older than %d minutes: not used"
                         % (sym, FRESH_MINUTES))
        q = _f(qty)
        if notional is not None and _f(notional):
            value = _f(notional)
        elif q:
            if ref:
                value = q * (size_ref or ref)

        # entry or exit
        held = acct["shares"].get(sym, 0.0)
        held_value = acct["values"].get(sym, 0.0)
        exiting = False
        if side == "sell" and held > 1e-9:
            if q is not None:
                exiting = q <= held + 1e-9
            elif value is not None:
                exiting = value <= held_value + CENT
        elif side == "buy" and held < -1e-9 and q is not None:
            exiting = q <= -held + 1e-9
        kind = "exit" if exiting else "entry"
        if fno and q:   # in reasons, so an exit blocks too: the exchange would reject it anyway
            und = name["symbol"] if name else (FUT_NAME.match(sym).group(1) if FUT_NAME.match(sym)
                                               else str(o.get("underlying") or "").upper())
            cap_q = india.FREEZE_QTY.get(und)
            if cap_q and q > cap_q:
                per = (cap_q // lot) * lot if lot else cap_q   # a ticket is whole lots
                reasons.append("freeze: %s units is over NSE's %s-unit limit for one %s order; send it as %d tickets "
                               "of at most %s" % (_num(q), format(cap_q, ","), und, -(-int(q) // per),
                                                  format(per, ",")))
        product = str(o.get("product") or "MIS").strip().upper()
        if lot_missing:
            later.append(lot_missing)
        if kind == "entry" and product != "MIS":
            later.append("product: %s is not intraday; P1 plans intraday (MIS) trades only" % product)
        if kind == "entry" and value is None and q:
            later.append("price: no price for %s to size a %s-share order; add it to "
                         "trader/prices.json (bharat-alerts: alerts.py quote %s --write)" % (sym, qty, sym))

        # blocked list and size
        if sym in blocked:
            later.append("blocked: %s is on your blocked list" % sym)
        if value is not None and fut:
            cap_n = _f(c.get("max_fut_notional_inr"))
            if cap_n and value > cap_n + CENT:
                later.append("size: %s notional is over your %s max_fut_notional_inr" % (money(value), money(cap_n)))
        elif value is not None and opt:
            cap_p = _f(c.get("max_premium_inr"))
            if cap_p and value > cap_p + CENT:
                later.append("size: %s of premium is over your %s max_premium_inr" % (money(value), money(cap_p)))
        elif value is not None:
            if value < float(c.get("min_order_inr") or 0) - CENT:
                later.append("size: %s is under your %s minimum order"
                             % (money(value), money(c["min_order_inr"])))
            if c.get("max_order_inr") and value > float(c["max_order_inr"]) + CENT:
                later.append("size: %s is over your %s max order"
                             % (money(value), money(c["max_order_inr"])))

        # account effects, simulated in order
        cash = acct["cash"]
        total = cash + sum(acct["values"].values())
        buffer_inr = total * float(c.get("cash_buffer_pct") or 0) / 100.0
        need_margin = None
        if opt and kind == "entry":
            if side == "sell":
                reasons.append("short: option selling is not part of this pack (a written option's loss has no cap); "
                               "it only buys calls and puts. If you hold this option, record the fill first")
            if typ not in ("limit", "market"):   # market is a format block above
                later.append("entry: an option entry is a limit order at the premium; a market order on an option "
                             "can fill far from its fair value")
            if o.get("stop_loss_stop_price") is not None and o.get("stop_loss_limit_price") is None:
                later.append("stop: an option's stop goes as SL (NSE takes no SL-M on options), so the order needs "
                             "stop_loss_limit_price (bharat-budget writes it); without it the stop would be rejected "
                             "after the entry fills")
            if o.get("premium_source") != "user":
                later.append("premium: the premium is estimated from the last bhavcopy, and options move too fast for "
                             "an end-of-day price. Read the live premium on Kite and re-plan (bharat-exit --entry)")
            min_oi = int(_f(c.get("min_option_oi")) or 0)
            oi = _f(o.get("oi"))
            if oi is None or oi < min_oi:
                later.append("liquidity: %s open interest is %s units, under your %s min_option_oi" % (
                    sym, "unknown" if oi is None else format(int(oi), ","), format(min_oi, ",")))
            if o.get("illiquid"):
                later.append("liquidity: %s is marked illiquid (no trades in the last bhavcopy, or low open interest)"
                             % sym)
            exp = str(o.get("expiry") or "")
            if name and name["expiry"]:   # a weekly name carries its date: it wins over the order's
                named = name["expiry"].isoformat()
                if exp and exp != named:
                    later.append("expiry: the order says %s but %s expires on %s (the date is in its name)"
                                 % (exp, sym, named))
                exp = named
            elif name and exp and exp[:7] != name["month"]:
                # ponytail: a monthly name gives the month only; checking it is that month's last expiry needs the bhavcopy
                later.append("expiry: the order says %s but %s is a %s contract" % (exp, sym, name["month"]))
            if not exp:
                later.append("expiry: the option entry has no expiry date (bharat-budget writes it)")
            elif exp < today:
                later.append("expiry: %s expired on %s" % (sym, exp))
            elif exp == today:
                cut = hhmm(c.get("expiry_day_cutoff") or "13:00", "expiry_day_cutoff")
                if (now_ist.hour, now_ist.minute) >= (cut.hour, cut.minute):
                    later.append("expiry: %s expires today and it is past your %s expiry_day_cutoff: time decay "
                                 "and gamma are at their worst in the last hours" % (sym, cut.strftime("%H:%M")))
                else:
                    warns.append("%s expires today: its premium decays fast and swings hard; no new entry after %s"
                                 % (sym, cut.strftime("%H:%M")))
        if fno and kind == "entry" and lots:
            max_lots = 1 if _f(c.get("max_lots")) is None else int(_f(c["max_lots"]))
            held_lots = int(max(held, 0) // lot) if lot else 0
            if lots + held_lots > max_lots:
                later.append("lots: %d lots%s is over your max_lots %d" % (
                    lots, (" plus %d held" % held_lots) if held_lots else "", max_lots))
        if fut and kind == "entry" and lots:
            und = str(o.get("underlying") or "").upper() or (FUT_NAME.match(sym).group(1) if FUT_NAME.match(sym) else sym)
            mtab = c.get("margin_per_lot_inr") or {}
            m = _f(mtab.get(und)) if isinstance(mtab, dict) else None
            if not m or m <= 0:
                later.append("margin: no figure for %s; set \"margin_per_lot_inr\": {\"%s\": RUPEES} from Zerodha's "
                             "margin calculator. It is never guessed" % (und, und))
            else:
                need_margin = lots * m
                info.append("margin %s for %d lot%s (%s a lot, MIS = NRML for F&O)" % (
                    money(need_margin), lots, "" if lots == 1 else "s", money(m)))
                if cash - need_margin < buffer_inr - CENT:
                    later.append("margin: %d lot%s need %s, which leaves %s cash, under your %s%% buffer of %s" % (
                        lots, "" if lots == 1 else "s", money(need_margin), money(max(cash - need_margin, 0)),
                        _pct(c["cash_buffer_pct"]), money(buffer_inr)))
            if typ == "market" and o.get("fut_price_source") != "user":
                later.append("entry: the futures entry is an estimate from the bhavcopy basis (it can move 75 points "
                             "in a session), so a market order could fill far from the plan and break the max loss. "
                             "Send a limit order (bharat-budget size.py --limit) or give the live price "
                             "(bharat-exit --entry PRICE)")
            exp = str(o.get("expiry") or "")
            if not exp:
                later.append("expiry: the futures entry has no expiry date (bharat-budget writes it), so the "
                             "expiry-day check cannot run")
            elif exp < today:
                later.append("expiry: %s expired on %s; trade a live contract%s" % (
                    sym, exp, (" (%s?)" % next_contract(sym)) if next_contract(sym) else ""))
            elif exp == today:
                nxt = next_contract(sym)
                later.append("expiry: %s expires today; no new entry in an expiring contract%s" % (
                    sym, (": trade %s" % nxt) if nxt else ""))
            if o.get("basis_estimated") and typ == "market":
                later.append("basis: the futures price is estimated from an old bhavcopy (%s); a market entry on it "
                             "is blocked. Read the live price on Kite and re-plan (bharat-exit --entry PRICE)"
                             % (o.get("basis_date") or "an earlier day"))
        if value is not None and side == "buy" and kind == "entry" and not fut:
            need = value / (1 if opt else lev)  # MIS margin; an option's premium is paid in full
            after_cash = cash - need
            if after_cash < -CENT and not c.get("allow_margin"):
                later.append("cash: needs %s%s but only %s cash" % (
                    money(need), (" (%s at %sx MIS)" % (money(value), _num(lev))) if lev != 1 else "", money(cash)))
            elif after_cash < buffer_inr - CENT:
                later.append("cash: leaves %s cash, under your %s%% buffer of %s"
                             % (money(max(after_cash, 0)), _pct(c["cash_buffer_pct"]), money(buffer_inr)))
            pos_after = held_value + value
            limit = total * float(c.get("max_position_pct") or 100) / 100.0
            if total > 0 and pos_after > limit + CENT:
                later.append("position: %s would be %s, %.1f%% of the %s account, over your %s%% cap"
                             % (sym, money(pos_after), 100.0 * pos_after / total, money(total),
                                _pct(c["max_position_pct"])))
        if side == "sell" and sym and kind == "entry":
            if q is not None:
                over = q > held + 1e-9
            elif value is not None:
                over = value > held_value + CENT
            else:
                over = held <= 0
            if over and not c.get("allow_short") and not opt:
                reasons.append("short: selling more %s than you hold (%s shares); short selling is off. "
                               "If you bought it, record the fill first (ticket.py fill)" % (sym, _num(held)))

        # day-trade checks: stop, loss, reward:risk, costs, daily loss, trades, window, band
        max_loss = risk_ps = rr = stop = target = None
        if kind == "entry" and side in ("buy", "sell"):
            long = side == "buy"
            leg_stop, plan_stop = _f(o.get("stop_loss_stop_price")), _f(plan.get("stop"))
            stop = leg_stop if leg_stop else plan_stop
            target = _f(o.get("take_profit_limit_price")) or _f(plan.get("target"))
            if leg_stop and plan_stop and abs(leg_stop - plan_stop) > CENT:
                warns.append("the bracket stop %s differs from the plan's stop %s: the bracket's is the one "
                             "the broker uses" % (_p(leg_stop), _p(plan_stop)))
            if not c.get("paper", True) and not leg_stop:
                later.append("stop: a live entry needs its stop at the broker (a bracket stop_loss_stop_price); "
                             "a stop in the plan only does not protect the position")
            if stop and not leg_stop:
                warns.append("the stop %s is in the plan only, not at the broker: set a stop alert "
                             "(bharat-alerts: alerts.py add --from-plan)" % _p(stop))
            # a market order, or a limit the price is already through, fills at the fresh price, whatever the plan says
            if stop and live and typ in ("market", "limit") and (
                    (long and live <= stop + CENT) or (not long and live >= stop - CENT)):
                later.append("stop: %s last %s is already %s the stop %s: a %s now fills past it, and the broker "
                             "rejects a stop on the wrong side of the price" % (
                                 sym, _p(live), "below" if long else "above", _p(stop), side))
            if not stop:
                if c.get("require_stop", True):
                    later.append("stop: no stop. Every entry needs one (\"require_stop\": true): a bracket "
                                 "stop_loss_stop_price, from bharat-exit and bharat-budget")
                else:
                    warns.append("no stop: the max loss is not capped, so it is not counted in the daily loss")
            elif ref:
                if (long and stop >= ref - CENT) or (not long and stop <= ref + CENT):
                    later.append("stop: the stop %s must be %s the entry %s for a %s"
                                 % (_p(stop), "below" if long else "above", _p(ref), side))
                else:
                    risk_c = int(round(abs(ref - stop) * 100))
                    risk_ps = risk_c / 100.0
                    shares = q if q is not None else ((value or 0) / ref)
                    cost_stop = costs(shares, ref, stop, seg) if long else costs(shares, stop, ref, seg)
                    max_loss = round(shares * risk_ps + cost_stop, 2)
                    cap = _f(c.get("max_loss_per_trade_inr"))
                    if cap and max_loss > cap + CENT:
                        later.append("loss: %s shares x %s risk a share + %s costs = %s at the stop, over your %s "
                                     "max loss per trade" % (_num(shares), _p(risk_ps), money(cost_stop),
                                                             money(max_loss), money(cap)))
                    min_rr = _f(c.get("min_reward_risk"))
                    if target:
                        if (long and target <= ref + CENT) or (not long and target >= ref - CENT):
                            later.append("target: the target %s must be %s the entry %s"
                                         % (_p(target), "above" if long else "below", _p(ref)))
                        else:
                            rr = round(int(round(abs(target - ref) * 100)) / float(risk_c), 2)
                            if min_rr and rr < min_rr - 1e-9:
                                later.append("reward:risk: %.2f (target %s, entry %s, stop %s) is under your "
                                             "minimum %s" % (rr, _p(target), _p(ref), _p(stop), _num(min_rr)))
                            reward = shares * abs(target - ref)
                            cost_t = costs(shares, ref, target, seg) if long else costs(shares, target, ref, seg)
                            share = _f(c.get("max_cost_share"))
                            info.append("costs %s at the stop, %s at the target (%.0f%% of the %s reward)"
                                        % (money(cost_stop), money(cost_t), 100.0 * cost_t / reward if reward else 0,
                                           money(reward)))
                            if share and reward > 0 and cost_t > share * reward + CENT:
                                later.append("costs: %s round-trip costs are %.0f%% of the %s reward at the target, "
                                             "over your %.0f%% limit (max_cost_share): the charges would eat most "
                                             "of what this trade can make" % (money(cost_t), 100.0 * cost_t / reward,
                                                                               money(reward), 100.0 * share))
                    elif min_rr:
                        later.append("reward:risk: no target to measure it against (min_reward_risk %s)"
                                     % _num(min_rr))
            opt_share = _f(c.get("max_option_cost_share"))
            if opt and opt_share and ref:   # against the premium paid: needs no stop and no target
                n = q if q is not None else ((value or 0) / ref)
                paid = n * ref
                cost_in = costs(n, ref, ref, seg) if paid > 0 else 0
                if paid > 0 and cost_in > opt_share * paid + CENT:
                    later.append("costs: %s round-trip costs are %.0f%% of the %s premium paid, over your "
                                 "%.0f%% max_option_cost_share: the flat Rs 20 an order eats a cheap option; "
                                 "buy more lots or a dearer strike" % (money(cost_in), 100.0 * cost_in / paid,
                                                                       money(paid), 100.0 * opt_share))
            daily = _f(c.get("max_daily_loss_inr"))
            if daily and max_loss is not None:
                used = loss_today + batch_loss
                if used + max_loss > daily + CENT:
                    later.append("daily loss: %s already at risk today plus %s for this one = %s, over your %s "
                                 "daily loss limit" % (money(used), money(max_loss), money(used + max_loss),
                                                       money(daily)))
            cap_t = int(c.get("max_trades_per_day") or 0)
            if cap_t and len(entries_today) + batch_entries >= cap_t:
                later.append("trades: %d entries placed today plus %d passed earlier in this batch, your limit "
                             "is %d a day" % (len(entries_today), batch_entries, cap_t))
            later.extend(win_reasons)
            b = bands.get(sym) or {}
            if long and not fno and not (_f(b.get("band")) and _f(b.get("prev_close"))):
                warns.append("band: no price band known for %s, so the band check is off (bharat-entry --band, or "
                             "the band the scanner wrote)" % sym)
            if long and ref and _f(b.get("band")) and _f(b.get("prev_close")):
                upper = india.tick_round(_f(b["prev_close"]) * (1 + _f(b["band"]) / 100.0), mode="down")
                if ref >= upper - CENT:
                    later.append("band: the entry %s is at the %s%% upper band %s (previous close %s): the stock "
                                 "can freeze there and leave you unable to exit" % (
                                     _p(ref), _num(b["band"]), _p(upper), _p(b["prev_close"])))
                elif ref >= upper * (1 - NEAR_BAND):
                    warns.append("band: the entry %s is within 1%% of the %s%% upper band %s" % (
                        _p(ref), _num(b["band"]), _p(upper)))
            if stop and ref and risk_ps:
                info.append("entry %s (%s), stop %s, target %s: risk %s a share"
                            % (_p(ref), REF_WORDS.get(ref_from, ref_from), _p(stop),
                               _p(target) if target else "none", _p(risk_ps)))
                info.append("max loss %s%s, reward:risk %s%s"
                            % (money(max_loss), (" incl. costs (limit %s)" % money(c["max_loss_per_trade_inr"]))
                               if _f(c.get("max_loss_per_trade_inr")) else " incl. costs",
                               ("%.2f" % rr) if rr is not None else "n/a",
                               (" (min %s)" % _num(c["min_reward_risk"])) if _f(c.get("min_reward_risk")) else ""))
            if daily or cap_t:
                info.append("today before this: %d of %s trades, %s of %s daily loss"
                            % (len(entries_today) + batch_entries, cap_t or "unlimited",
                               money(loss_today + batch_loss), money(daily) if daily else "no limit on"))

        # duplicates and the daily order limit
        key = (sym, side)
        if key in seen:
            later.append("duplicate: a second %s %s in this batch" % (side, sym))
        if key in placed_keys:
            later.append("duplicate: a %s %s was already placed today" % (side, sym))
        passed_so_far = sum(1 for r in results if r["verdict"] == "PASS")
        cap_n = int(c.get("max_orders_per_day") or 0)
        if cap_n and count_today + passed_so_far >= cap_n:
            later.append("daily limit: %d placed today plus %d passed earlier in this batch, "
                         "your limit is %d a day" % (count_today, passed_so_far, cap_n))
        seen.add(key)

        if kind == "entry":
            reasons.extend(later)
        else:
            warns.extend("exit, not blocked: " + x for x in later)

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS":
            if kind == "entry" and side in ("buy", "sell"):
                batch_entries += 1
                batch_loss += max_loss or 0.0
            if fut:
                # a lot holds margin, not its notional: the margin stays account value while the lot is open
                if side == "buy" and need_margin is not None:
                    acct["cash"] -= need_margin
                    acct["values"][sym] = acct["values"].get(sym, 0.0) + need_margin
                    batch_margin[sym] = batch_margin.get(sym, 0.0) + need_margin
                elif side == "sell" and kind == "exit" and q and held > 0 and batch_margin.get(sym):
                    freed = batch_margin[sym] * min(q / held, 1.0)
                    batch_margin[sym] -= freed
                    acct["cash"] += freed
                    acct["values"][sym] -= freed
                if q is not None:
                    acct["shares"][sym] = held + (q if side == "buy" else -q)
            elif side == "buy":
                if value is not None:
                    acct["cash"] -= value
                    acct["values"][sym] = acct["values"].get(sym, 0.0) + value
                if q is not None:
                    acct["shares"][sym] = held + q
                elif ref and value is not None:
                    acct["shares"][sym] = held + value / ref
            elif side == "sell":
                if value is not None:
                    acct["cash"] += value
                    acct["values"][sym] = max(acct["values"].get(sym, 0.0) - value, 0.0)
                if q is not None:
                    acct["shares"][sym] = held - q
                elif ref and value is not None:
                    acct["shares"][sym] = held - value / ref
        results.append({
            "index": i, "symbol": sym, "side": side, "kind": kind,
            "amount": money(_f(notional) or 0) if notional is not None else ("%s shares" % qty),
            "value_inr": round(value, 2) if value is not None else None,
            "entry_ref": round(ref, 2) if ref else None, "entry_ref_from": ref_from,
            "stop": stop, "target": target, "risk_per_share": risk_ps, "max_loss": max_loss,
            "reward_risk": rr, "fingerprint": fingerprint(o), "verdict": verdict,
            "reasons": reasons, "warnings": warns, "info": info,
        })

    return {
        "date": today,
        "now_ist": now_ist.strftime("%Y-%m-%d %H:%M"),
        "paper": bool(c.get("paper", True)),
        "results": results,
        "pass": sum(1 for r in results if r["verdict"] == "PASS"),
        "block": sum(1 for r in results if r["verdict"] == "BLOCK"),
        "warnings": top_warn,
        "cash_after": round(acct["cash"], 2),
        "today": {"orders_placed": count_today, "entries_placed": len(entries_today),
                  "loss_at_risk": round(loss_today, 2)},
    }


def _pct(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.2f" % x)


def _num(x):
    x = float(x)
    return str(int(x)) if x == int(x) else ("%.4f" % x).rstrip("0")


def report(res):
    lines = []
    for w in res["warnings"]:
        lines.append("WARNING: " + w)
    if res["warnings"]:
        lines.append("")
    for r in res["results"]:
        lines.append("%-5s #%d %s %s %s, %s" % (r["verdict"], r["index"], r["side"], r["symbol"], r["amount"],
                                                r["kind"]))
        for x in r.get("info", []):
            lines.append("          " + x)
        for x in r["reasons"]:
            lines.append("        - " + x)
        for x in r["warnings"]:
            lines.append("        ~ " + x)
    lines.append("")
    lines.append("%d passed, %d blocked at %s IST. Cash after the passed orders: %s (%s account)."
                 % (res["pass"], res["block"], res["now_ist"], money(res["cash_after"]),
                    "paper" if res["paper"] else "LIVE"))
    if res["block"]:
        lines.append("Blocked orders do not go to the broker. Fix the order or your rules, then check again.")
    else:
        lines.append("Every order passed. bharat-broker still asks you yes or no for each one.")
    lines.append("Education and tooling, not financial advice.")
    return "\n".join(lines)


def load_prices(pdoc):
    """prices.json -> ({SYM: price}, {SYM: price time as naive UTC or None}).

    The price time is the quote's own time (detail.time), else when it was fetched
    (detail.fetched), else the file's as_of."""
    if not isinstance(pdoc, dict):
        return {}, {}
    prices = pdoc.get("prices", pdoc)
    if not isinstance(prices, dict):
        return {}, {}
    out, times = {}, {}
    as_of = parse_utc(pdoc.get("as_of"))
    detail = pdoc.get("detail") or {}
    for s, p in prices.items():
        if _f(p) is None:
            continue
        s = str(s).upper()
        out[s] = float(p)
        d = detail.get(s) or {}
        times[s] = parse_utc(d.get("time")) or parse_utc(d.get("fetched")) or as_of
    return out, times


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check proposed orders against your limits (bharat-risk).")
    ap.add_argument("--orders", default="trader/orders.json")
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--portfolio", default="trader/portfolio.json",
                    help="your account; if missing, a fresh account funded with the budget")
    ap.add_argument("--prices", default="trader/prices.json")
    ap.add_argument("--state", default="trader/state/broker.json", help="what bharat-broker placed today")
    ap.add_argument("--entry", default="trader/entry.json",
                    help="bharat-entry's result: its previous close and price band feed the band check")
    ap.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-19 09:51\"")
    ap.add_argument("--today", help="YYYY-MM-DD (default: today in IST)")
    ap.add_argument("--out", default="trader/state/risk.json", help="where the verdicts are recorded")
    ap.add_argument("--no-write", action="store_true", help="do not record the verdicts")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        now = parse_now(a.now)
        cfg_peek = read_json(a.config, "config", required=False) or {}
        if not cfg_peek.get("paper", True) and (a.now or a.today):
            raise RiskError("--now and --today are for tests and paper only: a live check uses the real clock")
        if a.today and not re.match(r"^\d{4}-\d{2}-\d{2}$", a.today):
            raise RiskError("--today wants a date like 2026-10-19")
        doc = read_json(a.orders, "orders file (run bharat-budget first)")
        orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
        if not isinstance(orders, list) or not orders:
            raise RiskError("no orders in %s" % a.orders)
        cfg = read_json(a.config, "config", required=False) or {}
        portfolio = read_json(a.portfolio, "portfolio", required=False)
        pdoc = read_json(a.prices, "prices", required=False) or {}
        prices, times = load_prices(pdoc)
        state = read_json(a.state, "broker state", required=False)
        ent = read_json(a.entry, "entry", required=False) or {}
        today = a.today or to_local(now).date().isoformat()
        bands, old_entry = {}, None
        if ent.get("date") and str(ent["date"]) != today:
            old_entry = "%s is from %s, not today: its band is not used" % (a.entry, ent["date"])
        elif ent.get("symbol") and ent.get("prev_close") and ent.get("band"):
            bands[str(ent["symbol"]).upper()] = {"band": ent["band"], "prev_close": ent["prev_close"]}
        res = check(orders, cfg, portfolio, prices, state, a.today, now, times, bands)
        if old_entry:
            res["warnings"].append(old_entry)
        if pdoc.get("as_of"):
            res["prices_as_of"] = pdoc["as_of"]
        if not a.no_write:
            d = os.path.dirname(a.out)
            if d:
                os.makedirs(d, exist_ok=True)
            rec = dict(res)
            rec["checked_at"] = datetime.datetime.now(datetime.timezone.utc).replace(
                microsecond=0, tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ")
            rec["orders_file"] = a.orders
            india.write_json(a.out, rec)
    except RiskError as e:
        print("check.py: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, indent=2) if a.json else report(res))
    return 1 if res["block"] else 0


if __name__ == "__main__":
    sys.exit(main())
