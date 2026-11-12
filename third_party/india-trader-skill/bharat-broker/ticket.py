#!/usr/bin/env python3
"""Order tickets and the decision log for bharat-broker. Standard library only.

This tool never talks to a broker and never places anything. It does its
work around the one moment that matters, your yes:

  show      renders each proposed order as a plain-English ticket plus the
            exact Kite place_order calls Claude would make (live), or the
            paper-ledger step (paper, the default). A bracket shows its
            stop-loss, take-profit, the costs and the max loss at the stop.
            An order is READY only if bharat-risk passed THIS exact order
            today (matched by fingerprint) and nobody has decided on it yet.
  approve   records your explicit yes for ONE order, with your reply word for
            word. It refuses "yes to all", "sure", "ok", "haan", a yes that
            names a different stock, side, order type or amount ("one share"
            counts as 1), a yes with words it cannot check, and any order that
            is not READY.
  decline   records a no.
  result    records what happened: placed (with the broker's order id, or
            the paper fill), denied, or failed.
  fill      records a fill in trader/portfolio.json (cash, shares and the
            Zerodha charges for that order).
  flatten   builds a sell ticket that closes a symbol's open MIS position by
            flat_by (15:15 IST), before the broker's 15:20 square-off. It
            still goes through bharat-risk and your yes.
  log       prints today's decisions.

Paper ("paper": true, the default): there is no broker call. India's brokers
have no paper accounts, so the paper ledger is trader/portfolio.json: after
the yes, record the fill at the next bar's open (plus slippage, if you like)
with `result --status placed --filled-qty Q --filled-price P`.

Live ("paper": false, "broker": "zerodha"): Claude calls the Kite MCP
server's place_order tool (a self-hosted zerodha/kite-mcp-server: the hosted
mcp.kite.trade has no order tools). SEBI's rule since 1 Apr 2026: every API
order must come from a static IP registered in Kite, so live tickets refuse
until "static_ip_registered": true is in the config. Zerodha has no bracket
orders: the entry goes first, then ONE SL-M stop once the entry fills. The
target is an alert (bharat-alerts), and taking it is a new sell ticket, so
two exit orders can never both fill and leave you short.

    python3 ticket.py show                    # every order, READY or not
    python3 ticket.py show --index 1 --json   # one ticket as JSON
    python3 ticket.py approve --index 1 --reply "yes"
    python3 ticket.py result --index 1 --status placed --filled-qty 73 --filled-price 1227.80
    python3 ticket.py fill --symbol RELIANCE --side sell --qty 73 --price 1240.25
    python3 ticket.py flatten --symbol RELIANCE --qty 73
    python3 ticket.py log

Everything is written to trader/state/broker.json (append only). Dates are
IST dates; --now "2026-10-19 09:51" pins the clock.
"""

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import sys

import india

ORDER_FIELDS = ("symbol", "exchange", "product", "side", "qty", "notional", "type", "time_in_force", "limit_price",
                 "stop_price", "trail_price", "trail_percent", "extended_hours", "client_order_id",
                 "order_class", "take_profit_limit_price", "stop_loss_stop_price",
                 "stop_loss_limit_price", "advanced_instructions")
STRING_FIELDS = ("qty", "notional", "limit_price", "stop_price", "trail_price", "trail_percent",
                 "take_profit_limit_price", "stop_loss_stop_price", "stop_loss_limit_price")
TIF_WORDS = {"day": "good for today", "gtc": "good until canceled", "opg": "at the open",
             "cls": "at the close", "ioc": "immediate or cancel", "fok": "fill or kill"}
YES_WORDS = ("yes", "y")
# the only other words a yes may carry: anything else (a ticker, "go for it") is asked again
PLAIN_WORDS = {"please", "place", "it", "this", "that", "the", "order", "one", "go", "ahead", "now", "confirm",
               "confirmed", "share", "shares", "unit", "units", "lot", "lots", "at", "rs", "of", "for", "market",
               "max", "loss", "take", "profit", "target", "limit", "price", "qty", "quantity"}
SIDE_WORDS = ("buy", "sell")
TYPE_WORD = {"market": "market", "limit": "limit", "stop_limit": "limit"}   # what a yes may call each order type
POINTERS = ("this", "that")   # "this one" points at the order; any other "one" is the number 1
SEGMENT = "eq_intraday"
# digits too, as in bharat-risk/check.py: 360ONE26NOVFUT, NIFTYNXT5026NOVFUT, 360ONE26NOV1200CE
FUT_NAME = re.compile(r"^[A-Z0-9&\-]+?\d{2}[A-Z]{3}FUT$")
OPT_NAME = re.compile(r"^[A-Z0-9&\-]+?\d{2}[A-Z0-9]{3}\d+(CE|PE)$")
FUT_FIELDS = ("instrument", "underlying", "lot", "lots", "expiry", "basis_estimated", "basis_date",
              "fut_price_source", "right", "strike", "premium_source", "oi", "illiquid", "iv")   # same as bharat-risk/check.py: in the fingerprint, not sent to Kite
MARKET_PROTECTION = -1  # Kite's automatic protection; MARKET and SL-M orders without it are rejected (since 1 Apr 2026)


def segment_of(symbol):
    s = str(symbol or "").upper()
    return "fut" if FUT_NAME.match(s) else "opt" if OPT_NAME.match(s) else SEGMENT
KITE_TYPES = {"market": "MARKET", "limit": "LIMIT", "stop": "SL-M", "stop_limit": "SL"}
BATCH_WORDS = {"all", "every", "everything", "rest", "remaining", "them", "both", "batch", "each"}
DOUBT_WORDS = {"no", "not", "dont", "don't", "wait", "cancel", "stop", "but", "maybe", "later",
               "hold", "unless", "if"}


class TicketError(Exception):
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
        raise TicketError("--now wants a time like \"2026-10-19 09:51\" (IST) or ...Z (UTC)")
    if dt.tzinfo is None:
        return india.ist_to_utc(dt).replace(tzinfo=None)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def ist_iso(now_utc):
    """naive UTC -> "2026-10-19T09:51:00+05:30"."""
    return "%s+05:30" % to_local(now_utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- files

def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise TicketError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise TicketError("%s is not valid JSON: %s" % (path, e))


write_json = india.write_json   # whole or not at all: a write cut short leaves the old file


def rec_day(rec):
    """The trading day a decision belongs to: its "date" (the day it was made under), else the day of "at"."""
    return str(rec.get("date") or str(rec.get("at", ""))[:10])


# ---------------------------------------------------------------- the order

def fingerprint(order):
    """Same function as bharat-risk/check.py: identifies the exact order."""
    core = {k: order[k] for k in ORDER_FIELDS + FUT_FIELDS if k in order and order[k] is not None}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _str_num(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        f = float(v)
        return str(int(f)) if f == int(f) else repr(f)
    return str(v)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def tool_input(order):
    """The order's own fields (no notes): what bharat-risk fingerprinted."""
    out = {}
    for k in ORDER_FIELDS:
        if k in order and order[k] is not None:
            out[k] = _str_num(order[k]) if k in STRING_FIELDS else order[k]
    return out


def kite_calls(order):
    """The Kite MCP place_order calls for this order, in order. A bracket is the
    entry, then ONE SL-M stop after the entry fills; the target is not sent
    (Zerodha has no bracket or OCO for MIS here: two exit orders could both fill)."""
    sym = str(order.get("symbol", "")).upper()
    side = str(order.get("side", "")).upper()
    typ = str(order.get("type", "market")).lower()
    base = {"variety": "regular", "exchange": order.get("exchange") or "NSE", "tradingsymbol": sym,
            "product": order.get("product") or "MIS", "validity": "DAY"}
    qty = int(round(_f(order.get("qty")) or 0))
    entry = dict(base, transaction_type=side, quantity=qty, order_type=KITE_TYPES.get(typ, typ.upper()))
    if typ in ("limit", "stop_limit") and order.get("limit_price") is not None:
        entry["price"] = float(order["limit_price"])
    if typ in ("stop", "stop_limit") and order.get("stop_price") is not None:
        entry["trigger_price"] = float(order["stop_price"])
    if entry["order_type"] in ("MARKET", "SL-M"):
        entry["market_protection"] = MARKET_PROTECTION
    calls = [{"when": "now", "tool": "place_order", "input": entry}]
    if is_bracket(order) and order.get("stop_loss_stop_price") is not None:
        stop = dict(base, transaction_type="SELL" if side == "BUY" else "BUY", quantity=qty,
                    trigger_price=float(order["stop_loss_stop_price"]))
        if order.get("stop_loss_limit_price") is not None:   # SL: options take no SL-M
            stop.update(order_type="SL", price=float(order["stop_loss_limit_price"]))
        else:
            stop.update(order_type="SL-M", market_protection=MARKET_PROTECTION)
        calls.append({"when": "after the entry fills", "tool": "place_order", "input": stop})
    return calls


def _money(v):
    return india.inr(v)


def _p(v):
    return "%.2f" % float(v)


def is_bracket(order):
    oc = str(order.get("order_class") or "").lower()
    legs = order.get("take_profit_limit_price") is not None or order.get("stop_loss_stop_price") is not None
    return oc == "bracket" or (not oc and legs)


def max_loss(order):
    """(rupees, stop) lost if the stop fills exactly at the stop, costs included, or (None, stop)."""
    stop = _f(order.get("stop_loss_stop_price")) or _f((order.get("plan") or {}).get("stop"))
    q = _f(order.get("qty"))
    if not stop or not q:
        return None, stop
    plan = order.get("plan") or {}
    if str(order.get("type", "market")) == "limit" and _f(order.get("limit_price")):
        entry = _f(order["limit_price"])
    elif _f(plan.get("max_loss")) is not None:
        return round(_f(plan["max_loss"]), 2), stop
    else:
        entry = _f(plan.get("entry"))
    if not entry:
        return None, stop
    risk = abs(entry - stop)
    cost = india.round_trip(segment_of(order.get("symbol")), q, entry, stop)["total"]
    return round(q * int(round(risk * 100)) / 100.0 + cost, 2), stop


def plain_parts(order):
    """The ticket in plain words, one part per line."""
    side = str(order.get("side", "")).upper()
    sym = order.get("symbol", "?")
    typ = str(order.get("type", "market"))
    tif = TIF_WORDS.get(str(order.get("time_in_force", "day")), order.get("time_in_force"))
    if is_bracket(order):
        how = "limit %s" % _p(order["limit_price"]) if typ == "limit" and order.get("limit_price") else typ
        parts = ["%s %s %s, %s, %s" % (side, _str_num(order.get("qty")), sym, how, tif)]
        if order.get("stop_loss_stop_price") is not None:
            leg = "stop-loss %s" % _p(order["stop_loss_stop_price"])
            if order.get("stop_loss_limit_price") is not None:
                leg += (" (a stop-limit, limit %s: it may not fill in a fast move, when the price jumps past the "
                        "limit)" % _p(order["stop_loss_limit_price"]))
            parts.append(leg)
        if order.get("take_profit_limit_price") is not None:
            parts.append("take-profit %s" % _p(order["take_profit_limit_price"]))
        loss, stop = max_loss(order)
        if loss is not None:
            parts.append("max loss about %s with costs if the stop fills at %s (it can fill worse in a fast "
                         "market or a band freeze)" % (_money(loss), _p(stop)))
        return parts
    q = _str_num(order.get("qty"))
    what = "%s share%s of %s" % (q, "" if q == "1" else "s", sym)
    if typ == "limit":
        how = "limit order at %s" % _money(order.get("limit_price"))
    elif typ == "stop":
        how = "stop order at %s" % _money(order.get("stop_price"))
    elif typ == "stop_limit":
        how = "stop %s, limit %s" % (_money(order.get("stop_price")), _money(order.get("limit_price")))
    else:
        how = "%s order" % typ.replace("_", " ")
    return ["%s %s, %s, %s" % (side, what, how, tif)]


def plain(order):
    """BUY 73 RELIANCE, market, good for today / stop-loss 1215.35 / take-profit 1240.25 / max loss ..."""
    return " / ".join(plain_parts(order))


def amount_numbers(order):
    nums = []
    # every amount the ticket can show, and the count of lots (not a lot's size: "75" is half of a 150-unit order)
    for k in ("qty", "lots", "limit_price", "stop_price", "stop_loss_stop_price", "take_profit_limit_price",
              "stop_loss_limit_price"):
        if order.get(k) is not None:
            v = _f(order[k])
            if v is not None:
                nums.append(v)
    loss, _ = max_loss(order)
    if loss is not None:
        nums.append(loss)
    return nums


def parse_yes(reply, order, batch_symbols=()):
    """Is this reply an explicit yes for THIS one order? Returns (ok, why)."""
    raw = (reply or "").strip()
    if not raw:
        return False, "the reply is empty"
    if "?" in raw:
        return False, "the reply is a question, not a yes"
    words = re.findall(r"[a-z']+", raw.lower())
    if not words or words[0] not in YES_WORDS:
        return False, "only a reply that starts with \"yes\" counts (not ok, sure, go, do it)"
    if BATCH_WORDS & set(words):
        return False, "a yes covers one order at a time, not a batch"
    if DOUBT_WORDS & set(words[1:]):
        return False, "the reply has a condition or a doubt in it; ask again"
    sym = str(order.get("symbol", "")).upper()
    tokens = set(re.findall(r"[A-Z0-9][A-Z0-9&\-]{0,19}", raw.upper()))
    others = {s.upper() for s in batch_symbols} - {sym}
    named = tokens & others
    if named:
        return False, "the reply names %s, but this order is for %s" % (", ".join(sorted(named)), sym)
    side = str(order.get("side", "")).lower()
    wrong = [w for w in words[1:] if w in SIDE_WORDS and w != side]
    if wrong:
        return False, "the reply says %s, but this order is a %s" % (wrong[0], side)
    typ = str(order.get("type", "market")).lower()
    other = [w for w in words[1:] if w in ("market", "limit") and w != TYPE_WORD.get(typ)]
    if other:
        return False, "the reply says %s, but this order is a %s order" % (other[0], typ.replace("_", " "))
    own = set(re.findall(r"[a-z']+", sym.lower()))
    unknown = [w for w in words[1:] if w not in PLAIN_WORDS and w not in YES_WORDS and w != side and w not in own]
    if unknown:
        return False, "the reply has words this check cannot confirm (%s); ask for a plain yes" % ", ".join(unknown)
    # the order's own name may carry digits (NIFTY26NOV24000CE): they are not amounts
    rest = re.sub(re.escape(sym), " ", raw, flags=re.I) if sym else raw
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", rest)]
    left = re.findall(r"[a-z']+", rest.lower())
    nums += [1.0 for i, w in enumerate(left) if w == "one" and (i == 0 or left[i - 1] not in POINTERS)]
    want = amount_numbers(order)
    off = [n for n in nums if not any(abs(n - w) < 0.005 for w in want)]
    if off:
        return False, "the reply says %s, but this order is %s" % (", ".join(_str_num(n) for n in off), plain(order))
    return True, "explicit yes for %s" % plain(order)


# ---------------------------------------------------------------- state

def load_state(path):
    return read_json(path, "broker state", required=False) or {"orders": []}


def latest(state, fp, today):
    """The latest decision recorded today for this fingerprint, or None."""
    found = None
    for rec in state.get("orders", []):
        if rec.get("fingerprint") == fp and rec_day(rec) == today:
            found = rec
    return found


def risk_verdict(risk, fp, today):
    if not risk:
        return None, "bharat-risk has not checked these orders (run check.py)"
    if risk.get("date") != today:
        return None, "the risk check is from %s, not today; run check.py again" % risk.get("date")
    for r in risk.get("results", []):
        if r.get("fingerprint") == fp:
            return r, None
    return None, "this exact order was not in the last risk check (it changed or is new); run check.py again"


def status_of(order, risk, state, today):
    """(ready, label, risk_result)"""
    fp = fingerprint(order)
    rv, why = risk_verdict(risk, fp, today)
    if rv is None:
        return False, "NOT READY: " + why, None
    if rv.get("verdict") != "PASS":
        return False, "BLOCKED by bharat-risk: " + "; ".join(rv.get("reasons", [])), rv
    rec = latest(state, fp, today)
    if rec:
        st = rec.get("status")
        if st == "approved":
            return False, "APPROVED by you at %s, waiting for the place_order call or the paper fill" % rec.get("at"), rv
        return False, "DONE: %s at %s" % (st, rec.get("at")), rv
    return True, "READY: bharat-risk PASS, waiting for your yes", rv


def kind_of(order, rv):
    """entry or exit, as bharat-risk saw it (older checks: a buy is an entry)."""
    if rv and rv.get("kind"):
        return rv["kind"]
    return "entry" if str(order.get("side", "")).lower() == "buy" else "exit"


# ---------------------------------------------------------------- rendering

def live_problem(cfg):
    """Why live tickets cannot go out, or None (paper needs nothing)."""
    if cfg.get("paper", True):
        return None
    if str(cfg.get("broker", "zerodha")).lower() != "zerodha":
        return "this pack sends live orders to Zerodha only (\"broker\": \"zerodha\")"
    if cfg.get("static_ip_registered") is not True:
        return ("live API orders must come from a static IP registered in Kite (SEBI rule since 1 Apr 2026). "
                "Register it in Kite, then set \"static_ip_registered\": true in trader/config.json, or stay on "
                "paper")
    return None


def render(order, idx, total, cfg, risk, state, today):
    ready, label, rv = status_of(order, risk, state, today)
    paper = cfg.get("paper", True)
    acct = "PAPER ledger (no broker)" if paper else "LIVE ZERODHA ACCOUNT, REAL MONEY"
    lines = ["ORDER %d of %d, %s" % (idx, total, acct)]
    for part in plain_parts(order):
        lines.append("  " + part)
    plan = order.get("plan") or {}
    if plan.get("costs_at_stop") is not None:
        lines.append("  costs (Zerodha, round trip): %s at the stop, %s at the target"
                     % (_money(plan["costs_at_stop"]), _money(plan.get("costs_at_target") or 0)))
    if order.get("why"):
        lines.append("  why: " + order["why"])
    problem = live_problem(cfg)
    if problem and ready:
        ready, label = False, "NOT READY: " + problem
    lines.append("  " + label)
    for w in (rv or {}).get("warnings", []):
        lines.append("  note: " + w)
    if paper:
        lines.append("  Paper: nothing goes to a broker. After your yes, the fill is recorded in")
        lines.append("  trader/portfolio.json at the next bar's open, with Zerodha's charges.")
    else:
        lines.append("  Claude will call the Kite MCP server's place_order with exactly:")
        for c in kite_calls(order):
            lines.append("    %s:" % c["when"])
            for l in json.dumps(c["input"], indent=2).splitlines():
                lines.append("      " + l)
        if is_bracket(order):
            lines.append("  Zerodha has no bracket order: the stop goes in after the entry fills. The")
            lines.append("  target %s is an alert (bharat-alerts); taking it is a new sell ticket, after"
                         % _p(order.get("take_profit_limit_price")))
            lines.append("  cancelling the stop, so two exits can never both fill.")
    if ready:
        lines.append("  Reply \"yes\" to place this one order, or \"no\" to skip it.")
        if not paper:
            lines.append("  Claude's own card then asks again: Allow once places it, Deny stops it.")
    return "\n".join(lines), ready


def ticket_json(order, idx, cfg, risk, state, today):
    ready, label, rv = status_of(order, risk, state, today)
    problem = live_problem(cfg)
    if problem and ready:
        ready, label = False, "NOT READY: " + problem
    loss, stop = max_loss(order)
    paper = bool(cfg.get("paper", True))
    return {"index": idx, "ready": ready, "status": label, "plain": plain(order),
            "fingerprint": fingerprint(order), "paper": paper,
            "broker": "paper ledger" if paper else "zerodha", "tool": None if paper else "place_order",
            "calls": [] if paper else kite_calls(order), "why": order.get("why", ""),
            "bracket": is_bracket(order), "max_loss": loss, "stop": stop}


# ---------------------------------------------------------------- fills -> portfolio

def apply_fill(portfolio_path, cfg, symbol, side, qty, price, date):
    """Record a filled order in trader/portfolio.json: cash and shares, with a
    cost-basis lot per buy (oldest lots are relieved first on a sell), and
    Zerodha's intraday charges for this one order taken out of the cash."""
    pf = read_json(portfolio_path, "portfolio", required=False)
    if pf is None:
        budget = cfg.get("budget")
        if not budget:
            raise TicketError("no %s yet and no budget in the config to start it from" % portfolio_path)
        pf = {"cash": float(budget), "positions": []}
    symbol = symbol.upper()
    qty, price = float(qty), float(price)
    if qty <= 0 or price <= 0:
        raise TicketError("a fill needs a positive qty and price")
    cost = round(qty * price, 2)
    fut = segment_of(symbol) == "fut"
    charges = india.leg_costs(segment_of(symbol), side, qty, price)["total"]
    pos = None
    for p in pf.setdefault("positions", []):
        if str(p.get("ticker", "")).upper() == symbol:
            pos = p
    if side == "buy":
        if pos is None:
            pos = {"ticker": symbol, "shares": 0.0, "lots": []}
            pf["positions"].append(pos)
        lots = pos.setdefault("lots", [])
        lots.append({"id": "%s-%d" % (symbol, len(lots) + 1), "date": date, "shares": qty, "cost": cost})
        pos["shares"] = round(float(pos.get("shares", 0)) + qty, 6)
        # a future blocks margin, it is not bought outright: only the charges leave the cash now
        pf["cash"] = round(float(pf.get("cash", 0)) - (0 if fut else cost) - charges, 2)
    else:
        if pos is None or float(pos.get("shares", 0)) + 1e-9 < qty:
            raise TicketError("cannot record selling %s %s: the portfolio holds %s"
                              % (_str_num(qty), symbol, pos.get("shares", 0) if pos else 0))
        left, relieved = qty, 0.0
        for lot in sorted(pos.get("lots", []), key=lambda l: l.get("date", "")):
            if left <= 1e-9:
                break
            take = min(left, float(lot["shares"]))
            per = float(lot["cost"]) / float(lot["shares"]) if float(lot["shares"]) else 0
            relieved += per * take
            lot["cost"] = round(float(lot["cost"]) - per * take, 2)
            lot["shares"] = round(float(lot["shares"]) - take, 6)
            left -= take
        pos["lots"] = [l for l in pos.get("lots", []) if float(l["shares"]) > 1e-9]
        pos["shares"] = round(float(pos["shares"]) - qty, 6)
        if pos["shares"] <= 1e-9:
            pf["positions"].remove(pos)
        pf["cash"] = round(float(pf.get("cash", 0)) + (cost - relieved if fut else cost) - charges, 2)
    fees = pf.setdefault("charges", {})
    fees[date] = round(float(fees.get(date, 0)) + charges, 2)
    write_json(portfolio_path, pf)
    pf["_charges"] = charges
    return pf


def log_fill(state_path, now, today, symbol, side, qty, price):
    """A fill in the log: what happened, for the user and for bharat-risk's record of the day."""
    state = load_state(state_path)
    state.setdefault("orders", []).append({
        "at": ist_iso(now), "date": today, "status": "filled", "symbol": symbol.upper(), "side": side,
        "qty": _str_num(float(qty)), "price": float(price),
        "plain": "%s %s %s at %s" % (side.upper(), _str_num(float(qty)), symbol.upper(), _p(price))})
    write_json(state_path, state)


# ---------------------------------------------------------------- commands

def _clock(a):
    now = parse_now(getattr(a, "now", None))
    today = getattr(a, "today", None) or to_local(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", today):
        raise TicketError("--today wants a date like 2026-10-19")
    return now, today


def no_pinned_clock_live(a, cfg):
    if not cfg.get("paper", True) and (getattr(a, "now", None) or getattr(a, "today", None)):
        raise TicketError("--now and --today are for tests and paper only: a live ticket uses the real clock")


STALE_MINUTES = 15


def entry_now_problem(cfg, risk, now):
    """Why an entry cannot be approved right now, or None: the PASS must be fresh
    (under 15 minutes old) and now must be inside NSE's session and the entry window."""
    local = to_local(now)
    try:
        checked = datetime.datetime.strptime(str((risk or {}).get("now_ist")), "%Y-%m-%d %H:%M")
    except ValueError:
        checked = None
    if checked is not None:
        age = (local - checked).total_seconds() / 60.0
        if age > STALE_MINUTES:
            return "the risk check is %d minutes old; prices and the window move: run check.py again" % age
    s = india.session(local.date())
    if not s or not (s[0].replace(tzinfo=None) <= local < s[1].replace(tzinfo=None)):
        return "NSE is not open now (%s IST)" % local.strftime("%Y-%m-%d %H:%M")
    for key, default, word in (("no_entries_before", "09:30", "before"), ("no_entries_after", "14:45", "after")):
        v = cfg.get(key, default)
        if not v:
            continue
        h, m = [int(x) for x in str(v).split(":")]
        t = datetime.time(h, m)
        hm_now = local.time().replace(second=0, microsecond=0)
        if (word == "before" and hm_now < t) or (word == "after" and hm_now > t):
            return "no new entries %s %s IST (it is %s)" % (word, v, local.strftime("%H:%M"))
    return None


def _load(a):
    doc = read_json(a.orders, "orders file (run bharat-budget, or ticket.py flatten, first)")
    orders = doc.get("orders", doc) if isinstance(doc, dict) else doc
    if not isinstance(orders, list) or not orders:
        raise TicketError("no orders in %s" % a.orders)
    cfg = read_json(a.config, "config", required=False) or {}
    no_pinned_clock_live(a, cfg)
    risk = read_json(a.risk, "risk check", required=False)
    state = load_state(a.state)
    now, today = _clock(a)
    return orders, cfg, risk, state, today, now


def _pick(orders, index):
    if index is None:
        raise TicketError("say which order: --index N")
    if not 1 <= index <= len(orders):
        raise TicketError("there is no order %d (there are %d)" % (index, len(orders)))
    return orders[index - 1]


def cmd_show(a):
    orders, cfg, risk, state, today, now = _load(a)
    picks = [(a.index, _pick(orders, a.index))] if a.index else list(enumerate(orders, 1))
    if a.json:
        out = [ticket_json(o, i, cfg, risk, state, today) for i, o in picks]
        print(json.dumps(out[0] if a.index else out, indent=2))
        return 0 if any(t["ready"] for t in out) else 1
    blocks, any_ready = [], False
    for i, o in picks:
        text, ready = render(o, i, len(orders), cfg, risk, state, today)
        blocks.append(text)
        any_ready = any_ready or ready
    print("\n\n".join(blocks))
    print("\nOne order at a time: a yes covers one ticket, never the batch. Education and tooling, "
          "not financial advice.")
    return 0 if any_ready else 1


def cmd_approve(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    ready, label, rv = status_of(order, risk, state, today)
    if not ready:
        raise TicketError("order %d is not ready: %s" % (a.index, label))
    problem = live_problem(cfg)
    if problem:
        raise TicketError("order %d cannot go live: %s" % (a.index, problem))
    if kind_of(order, rv) == "entry":
        late = entry_now_problem(cfg, risk, now)
        if late:
            raise TicketError("order %d is not ready: %s" % (a.index, late))
    ok, why = parse_yes(a.reply, order, [o.get("symbol", "") for o in orders])
    if not ok:
        print("Not recorded: %s. Ask the user again with the ticket for order %d." % (why, a.index))
        return 1
    loss, stop = max_loss(order)
    if rv and rv.get("max_loss") is not None:
        loss = rv["max_loss"]          # what bharat-risk checked, so the daily limit counts the same number
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": str(order.get("symbol", "")).upper(), "side": order.get("side"),
           "qty": order.get("qty"), "kind": kind_of(order, rv), "max_loss": loss, "stop": stop,
           "plain": plain(order), "status": "approved", "reply": a.reply,
           "paper": bool(cfg.get("paper", True)), "broker": "paper ledger" if cfg.get("paper", True) else "zerodha"}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded your yes for order %d: %s." % (a.index, plain(order)))
    if cfg.get("paper", True):
        print("Paper: no broker call. Record the fill at the next bar's open:")
        print("  ticket.py result --index %d --status placed --filled-qty %s --filled-price P"
              % (a.index, _str_num(order.get("qty"))))
    else:
        calls = kite_calls(order)
        print("Now call the Kite MCP place_order with exactly:")
        print(json.dumps(calls[0]["input"], indent=2))
        if len(calls) > 1:
            print("After it fills, the stop (one more place_order, same yes):")
            print(json.dumps(calls[1]["input"], indent=2))
            print("Its quantity = the FILLED quantity (check get_orders): a part-filled entry gets a smaller stop,")
            print("or the stop would sell more than you hold and leave you short.")
            print("If the stop is rejected or denied, the position has no stop: flatten it now (ticket.py flatten,")
            print("bharat-risk, the user's yes) and tell the user.")
        print("Claude's card shows Allow once / Deny for each call. Then: ticket.py result --index %d --status "
              "placed|denied|failed --broker-id ID" % a.index)
    return 0


def cmd_decline(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    rec = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fingerprint(order),
           "symbol": str(order.get("symbol", "")).upper(), "side": order.get("side"),
           "plain": plain(order), "status": "declined", "reply": a.reply or "no"}
    state.setdefault("orders", []).append(rec)
    write_json(a.state, state)
    print("Recorded: you said no to order %d (%s). Nothing was sent." % (a.index, plain(order)))
    return 0


def cmd_result(a):
    orders, cfg, risk, state, today, now = _load(a)
    order = _pick(orders, a.index)
    fp = fingerprint(order)
    rec = latest(state, fp, today)
    if not rec or rec.get("status") != "approved":
        raise TicketError("order %d has no yes recorded today; a result can only follow an approval" % a.index)
    new = {"at": ist_iso(now), "date": today, "index": a.index, "fingerprint": fp, "symbol": rec["symbol"],
           "side": rec["side"], "plain": rec["plain"], "status": a.status}
    for k in ("qty", "kind", "max_loss", "stop"):
        if rec.get(k) is not None:
            new[k] = rec[k]
    if a.broker_id:
        new["broker_order_id"] = a.broker_id
    if a.note:
        new["note"] = a.note
    state["orders"].append(new)
    write_json(a.state, state)
    print("Recorded order %d as %s." % (a.index, a.status))
    if a.status == "placed" and a.filled_qty and a.filled_price:
        pf = apply_fill(a.portfolio, cfg, rec["symbol"], rec["side"], a.filled_qty, a.filled_price, today)
        log_fill(a.state, now, today, rec["symbol"], rec["side"], a.filled_qty, a.filled_price)
        print("Added the fill to %s (charges %s). Cash now %s." % (a.portfolio, _money(pf["_charges"]),
                                                                   _money(pf["cash"])))
    elif a.status == "placed":
        print("When it fills, record it: ticket.py fill --symbol %s --side %s --qty Q --price P"
              % (rec["symbol"], rec["side"]))
        if rec.get("kind") == "entry" and is_bracket(order):
            print("When the stop or the target exit fills later, record that sell the same way.")
    return 0


def cmd_fill(a):
    cfg = read_json(a.config, "config", required=False) or {}
    now = parse_now(a.now)
    date = a.date or to_local(now).date().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise TicketError("--date wants a date like 2026-10-19")
    pf = apply_fill(a.portfolio, cfg, a.symbol, a.side, a.qty, a.price, date)
    log_fill(a.state, now, date, a.symbol, a.side, a.qty, a.price)
    print("Recorded %s %s %s at %s on %s in %s (charges %s). Cash now %s." % (
        a.side, a.qty, a.symbol.upper(), _money(a.price), date, a.portfolio, _money(pf["_charges"]),
        _money(pf["cash"])))
    return 0


def cmd_flatten(a):
    cfg = read_json(a.config, "config", required=False) or {}
    no_pinned_clock_live(a, cfg)
    sym = a.symbol.strip().upper()
    now, today = _clock(a)
    source = "the broker, get_positions"
    if a.qty is not None:
        qty = _f(a.qty)
        if qty is None or qty <= 0:
            raise TicketError("--qty wants the number of shares you hold, like --qty 8")
    else:
        pf = read_json(a.portfolio, "portfolio", required=False) or {}
        qty = sum(_f(p.get("shares", p.get("qty"))) or 0 for p in pf.get("positions", [])
                  if str(p.get("ticker") or p.get("symbol") or "").upper() == sym)
        source = a.portfolio
        if qty <= 1e-9:
            raise TicketError("nothing to flatten: no %s shares in %s. Check the broker (get_positions) "
                              "and pass --qty if you do hold some" % (sym, a.portfolio))
    plan = read_json(a.plan, "plan", required=False) or {}
    mine = sym in (str(plan.get("symbol", "")).upper(), str(plan.get("contract", "")).upper())
    flat_by = (plan.get("flat_by") if mine else None) or cfg.get("flat_by") or "15:15"
    seg = segment_of(sym)
    fut = seg == "fut"
    if seg == "opt" and a.limit is None:
        raise TicketError("an option is flattened at a limit, never market (a market sell can fill far under its "
                          "value): read the bid on Kite and pass --limit PRICE")
    if a.limit is not None and not (_f(a.limit) is not None and math.isfinite(_f(a.limit)) and _f(a.limit) > 0):
        raise TicketError("--limit wants a price above 0, got %r" % a.limit)
    order = {"symbol": sym, "exchange": "NSE" if seg == SEGMENT else "NFO", "product": "MIS", "side": "sell",
             "qty": _str_num(qty), "type": "limit" if a.limit is not None else "market", "time_in_force": "day",
             "why": "flat by %s IST: closes the %s intraday trade before the broker's 15:20 square-off "
                    "(qty from %s)" % (flat_by, sym, source)}
    lim = None
    if a.limit is not None:   # NSE rejects an off-tick price; down, so the sell that gets you out fills
        lim = india.tick_round(_f(a.limit), mode="down")
        if lim < 0.05 - 1e-9:
            raise TicketError("--limit %s is under NSE's 0.05 tick and would round down to 0.00: the lowest limit "
                              "is 0.05" % a.limit)
        order["limit_price"] = _p(lim)
    if seg != SEGMENT:
        order["instrument"] = seg
        if mine and plan.get("lot"):
            order["lot"] = int(plan["lot"])
    old = read_json(a.orders, "orders", required=False)
    replaced = len(old.get("orders", [])) if isinstance(old, dict) else 0
    doc = {"created_by": "bharat-broker", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "proposed",
           "paper": bool(cfg.get("paper", True)),
           "note": "Flatten ticket. Nothing is placed until bharat-risk passes it and you say yes in the chat.",
           "source": "ticket.py flatten %s on %s" % (sym, today), "orders": [order]}
    write_json(a.orders, doc)
    if lim is not None and abs(lim - _f(a.limit)) > 1e-9:
        print("--limit %s is off NSE's 0.05 tick: the sell goes at %s." % (a.limit, _p(lim)))
    print("Flatten %s: %s (flat by %s IST)." % (sym, plain(order), flat_by))
    print("Wrote it to %s%s." % (a.orders, (" (it replaced %d earlier proposal%s)"
                                             % (replaced, "" if replaced == 1 else "s")) if replaced else ""))
    if a.qty is None:
        print("The qty comes from %s. Check it against the broker's positions before you say yes." % source)
    print("Next:")
    print("  1. bharat-risk: check.py. A sell that only closes what you hold is never blocked by the daily")
    print("     limits or the entry window.")
    print("  2. ticket.py show, then the user's yes for this one order.")
    print("  3. Live: if the stop (SL-M, or SL for an option) is still open, cancel it FIRST (get_orders, then cancel_order, Needs")
    print("     approval), or the stop and this sell could both fill and leave you short. Then place_order")
    print("     with the ticket. Paper: record the fill with ticket.py fill.")
    return 0


def cmd_log(a):
    state = load_state(a.state)
    now, today = _clock(a)
    rows = [r for r in state.get("orders", []) if a.all or rec_day(r) == today]
    if not rows:
        print("No decisions recorded%s." % ("" if a.all else " today"))
        return 1
    for r in rows:
        extra = (" id " + r["broker_order_id"]) if r.get("broker_order_id") else ""
        reply = (' reply "%s"' % r["reply"]) if r.get("reply") and r["status"] in ("approved", "declined") else ""
        loss = (" max loss %s" % _money(r["max_loss"])) if r.get("max_loss") is not None and r["status"] == "placed" else ""
        idx = ("#%s" % r["index"]) if r.get("index") is not None else "  "
        print("%s  %s %-9s %s%s%s%s" % (r.get("at"), idx, r.get("status"), r.get("plain"), reply, loss, extra))
    placed = sum(1 for r in rows if r.get("status") == "placed")
    print("%d placed%s." % (placed, "" if a.all else " today"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Order tickets and your yes (bharat-broker). Never places orders.")
    sub = ap.add_subparsers(dest="cmd")

    def clock(p):
        p.add_argument("--now", help="pretend it is this IST time, e.g. \"2026-10-19 09:51\"")
        p.add_argument("--today", help="YYYY-MM-DD (default: today in IST)")

    def common(p):
        p.add_argument("--orders", default="trader/orders.json")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--risk", default="trader/state/risk.json")
        p.add_argument("--state", default="trader/state/broker.json")
        clock(p)

    p = sub.add_parser("show", help="render the tickets")
    common(p)
    p.add_argument("--index", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("approve", help="record the user's explicit yes for one order")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply", required=True, help="the user's reply, word for word")
    p = sub.add_parser("decline", help="record a no")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--reply")
    p = sub.add_parser("result", help="record what happened: placed (or the paper fill), denied, failed")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--status", required=True, choices=["placed", "denied", "failed"])
    p.add_argument("--broker-id")
    p.add_argument("--note")
    p.add_argument("--filled-qty")
    p.add_argument("--filled-price")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p = sub.add_parser("fill", help="record a fill in trader/portfolio.json")
    p.add_argument("--symbol", required=True)
    p.add_argument("--side", required=True, choices=["buy", "sell"])
    p.add_argument("--qty", required=True)
    p.add_argument("--price", required=True)
    p.add_argument("--date", help="YYYY-MM-DD (default: today in IST)")
    p.add_argument("--now")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--state", default="trader/state/broker.json")
    p = sub.add_parser("flatten", help="a sell ticket that closes a symbol's position by flat_by")
    p.add_argument("--symbol", required=True)
    p.add_argument("--qty", help="shares you hold, from the broker's positions (default: trader/portfolio.json)")
    p.add_argument("--orders", default="trader/orders.json", help="where the ticket is written")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--limit", help="sell at this limit (required for an option)")
    clock(p)
    p = sub.add_parser("log", help="today's decisions")
    p.add_argument("--state", default="trader/state/broker.json")
    clock(p)
    p.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"show": cmd_show, "approve": cmd_approve, "decline": cmd_decline, "result": cmd_result,
                "fill": cmd_fill, "flatten": cmd_flatten, "log": cmd_log}[a.cmd](a)
    except TicketError as e:
        print("ticket.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
