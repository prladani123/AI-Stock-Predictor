#!/usr/bin/env python3
"""Exit Planner for bharat-exit: sets the stop and the target BEFORE you
enter, so you know when to get out. Standard library only. Long side only.
It never places an order.

    python3 exit.py plan                                    # from trader/entry.json: or-mid stop, 2R target
    python3 exit.py plan --stop or-mid --target 1R --write  # the example: stop 1215.35, target 1240.25
    python3 exit.py plan --symbol RELIANCE --entry 1227.80 --or-high 1223.40 --or-low 1207.30
    python3 exit.py plan --stop atr:1.5 --offline RELIANCE.json # ATR needs the session's bars
    python3 exit.py plan --symbol TCS --entry 2489.60 --setup level --stop pct:1
    python3 exit.py show                                    # the current trader/plan.json

Prices are on NSE's tick, Rs 0.05.

Stops (a long's stop rounds DOWN to the tick, away from the entry):
  or-mid     the opening-range midpoint (the default for an orb entry)
  or-low     the opening-range low
  price:X    your price
  pct:X      X percent under the entry
  atr:K      K times the 14-bar ATR under the entry, on the session's bars up
             to the entry bar (--offline FILE or Yahoo). With fewer than 14
             bars by then it averages all of them and says so.
  A level setup has no opening-range default: give a stop.

Targets (an R target rounds UP to the tick; your price rounds to the nearest tick):
  2R (default), 1R, 1.5R ...   entry + R x risk, where risk = entry - stop
  price:X                      your price

Refused, with the reason: a stop at or above the entry, a target at or below
it, a risk under 0.05% of the price (too tight to survive the spread), an
entry.json that has not triggered (unless you give --entry), a short (P1
plans longs only).

flat_by (default 15:15 IST, or "flat_by" in trader/config.json): out by then,
win or lose, before the broker's 15:20 MIS auto square-off.
"min_reward_risk" in the config (default 1.0) only warns here; bharat-risk
enforces it.

A stop is a plan, not a guarantee: a gap or a fast market can fill past it.
Exit codes: plan 0 = a plan, 1 = refused or an error. show 0 = a plan exists.
"""

import argparse
import datetime
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

import india

UA = india.TOOL_UA
BACKOFF = 5.0
HOSTS = ("query2", "query1")
INTERVALS = {"1m": 60, "5m": 300}
DEFAULTS = {"flat_by": "15:15", "min_reward_risk": 1.0}
ATR_BARS = 14
MIN_RISK_PCT = Decimal("0.05")     # risk under 0.05% of the price is inside the spread's noise
CENT = Decimal("0.01")
TICK = Decimal("0.05")   # NSE equity tick
EPOCH = datetime.datetime(1970, 1, 1)
NOTE = "A stop is a plan, not a guarantee: a gap or a fast market can fill past it."

OPEN = india.OPEN
CLOSE = india.CLOSE


class PlanError(Exception):
    pass


# ---------------------------------------------------------------- time

def to_local(utc):
    """Naive UTC -> naive IST."""
    return india.to_ist(utc.replace(tzinfo=datetime.timezone.utc)).replace(tzinfo=None)


def local_to_utc(local):
    """Naive IST -> naive UTC."""
    return india.ist_to_utc(local).replace(tzinfo=None)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def utc_of(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts))


def parse_now(text):
    """--now -> naive UTC. A time without an offset is IST."""
    if not text:
        return utc_now()
    t = text.strip()
    if t.upper().endswith(" IST"):
        t = t[:-4].strip()
    if re.match(r"^\d{1,2}:\d{2}$", t):
        t = "%s %s" % (to_local(utc_now()).date().isoformat(), t.zfill(5))
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise PlanError('--now wants an IST time like "2026-10-19 09:50" (or ...Z for UTC)')
    if dt.tzinfo is None:
        return local_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, what):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise PlanError("%s wants an IST time like 15:15, not %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def parse_clock(text, what):
    """10:15 AM, 10:15, 3:05 PM -> time."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\s*$", str(text))
    if not m:
        raise PlanError("%s wants a time like 10:15 AM, not %r" % (what, text))
    h, mi = int(m.group(1)), int(m.group(2))
    if m.group(3):
        if not 1 <= h <= 12:
            raise PlanError("%s wants a time like 10:15 AM, not %r" % (what, text))
        h = h % 12 + (12 if m.group(3).upper() == "PM" else 0)
    if h > 23 or mi > 59:
        raise PlanError("%s wants a time like 10:15 AM, not %r" % (what, text))
    return datetime.time(h, mi)


def hm(t):
    return "%02d:%02d" % (t.hour, t.minute)


def ampm(t):
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


def session(d):
    """(open_time, close_time) in IST, or None if NSE is closed all day."""
    s = india.session(d)
    return (s[0].time(), s[1].time()) if s else None


# ---------------------------------------------------------------- money math (Decimal, to the tick)

def D(x, what="a number"):
    try:
        if isinstance(x, float):
            return Decimal(repr(x))
        t = str(x).strip()
        for sign in ("Rs.", "Rs", "\u20b9"):
            if t.startswith(sign):
                t = t[len(sign):].strip()
        return Decimal(t.replace(",", ""))
    except (InvalidOperation, ValueError):
        raise PlanError("%s is not a number: %r" % (what, x))


def cents(x):
    """Round half up to the cent."""
    return D(x).quantize(CENT, rounding=ROUND_HALF_UP)


def to_tick(x, how):
    return (D(x) / TICK).quantize(Decimal("1"), rounding=how) * TICK


def tick_down(x):
    """A long's stop: down to the tick, away from the entry."""
    return to_tick(x, ROUND_FLOOR).quantize(CENT)


def tick_up(x):
    """An R target: up to the tick."""
    return to_tick(x, ROUND_CEILING).quantize(CENT)


def tick_near(x):
    return to_tick(x, ROUND_HALF_UP).quantize(CENT)


def money(x):
    return "Rs %s" % format(D(x).quantize(CENT), ",f")


def num(x):
    """1, 1.5, 2.25: a Decimal without trailing zeros."""
    s = format(D(x).normalize(), "f")
    return s


def ratio(x):
    """reward:risk for people: 1.0, 1.5, 1.37."""
    s = "%.2f" % float(x)
    return s[:-1] if s.endswith("0") else s


WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


def parse_stop(text):
    t = str(text).strip().lower()
    if t in ("or-mid", "or_mid", "mid", "or-midpoint"):
        return "or_mid", None
    if t in ("or-low", "or_low", "low"):
        return "or_low", None
    m = re.match(r"^(price|pct|atr):\s*(?:rs\.?\s*)?([0-9][0-9,]*\.?[0-9]*)$", t)
    if not m:
        raise PlanError("--stop is or-mid, or-low, price:X, pct:X or atr:K (got %r)" % text)
    kind, val = m.group(1), D(m.group(2), "--stop")
    if val <= 0:
        raise PlanError("--stop %s needs a number above 0" % kind)
    if kind == "pct" and val >= 100:
        raise PlanError("--stop pct:X needs X under 100")
    return kind, val


def parse_target(text):
    t = str(text).strip()
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*[Rr]$", t)
    if m:
        r = D(m.group(1), "--target")
        if r <= 0:
            raise PlanError("--target needs an R multiple above 0, like 1R or 2R")
        return "r", r
    m = re.match(r"^price:\s*(?:rs\.?\s*)?([0-9][0-9,]*\.?[0-9]*)$", t.lower())
    if m:
        return "price", D(m.group(1), "--target")
    raise PlanError("--target is 2R, 1R, 1.5R ... or price:X (got %r)" % text)


def r_label(r):
    n = int(r) if r == r.to_integral_value() else None
    return "%sR (%s times the risk)" % (num(r), WORDS.get(n, num(r)))


def compute(entry, stop_rule, stop_param, target_rule, target_param, or_high=None, or_low=None,
            atr=None, atr_bars=None):
    """The stop, the target and the risk, on the Rs 0.05 tick. Raises PlanError with the reason."""
    e = cents(entry)
    if e <= 0:
        raise PlanError("the entry price must be above Rs 0")
    detail = ""
    if stop_rule in ("or_mid", "or_low"):
        if or_high is None or or_low is None:
            raise PlanError("an %s stop needs the opening range (--or-high and --or-low)" % stop_rule.replace("_", "-"))
        oh, ol = cents(or_high), cents(or_low)
        if oh <= ol:
            raise PlanError("the opening-range high %s must be above its low %s" % (money(oh), money(ol)))
        if stop_rule == "or_mid":
            raw = (oh + ol) / 2
            label = "opening-range midpoint"
            detail = "(%s + %s) / 2 = Rs %s" % (money(oh), money(ol), num(raw))
        else:
            raw, label = ol, "opening-range low"
    elif stop_rule == "price":
        raw, label = stop_param, "your price"
    elif stop_rule == "pct":
        raw = e * (1 - stop_param / 100)
        label = "%s%% under the entry" % num(stop_param)
        detail = "%s x (1 - %s%%) = Rs %s" % (money(e), num(stop_param), num(raw))
    elif stop_rule == "atr":
        if atr is None:
            raise PlanError("an atr stop needs the session's bars")
        a = D(round(atr, 6))
        raw = e - stop_param * a
        label = "%s x ATR(%d) under the entry" % (num(stop_param), atr_bars)
        detail = "ATR Rs %s over the %d bars to the entry%s" % (
            num(a.quantize(Decimal("0.0001"))), atr_bars, " (fewer than 14 so far)" if atr_bars < ATR_BARS else "")
    else:
        raise PlanError("unknown stop rule %r" % stop_rule)
    stop = tick_down(raw)
    if stop != D(raw):
        detail = (detail + ", rounded down to the tick" if detail else "Rs %s rounded down to the tick" % num(raw))
    if stop >= e:
        raise PlanError("the stop %s (%s) is at or above the entry %s: a long's stop goes under the entry"
                        % (money(stop), label, money(e)))
    if stop <= 0:
        raise PlanError("the stop works out to %s: pick a stop above Rs 0" % money(stop))
    risk = e - stop
    floor_risk = e * MIN_RISK_PCT / 100
    if risk < floor_risk:
        raise PlanError("the stop is %s under the entry, less than 0.05%% of the price (%s on %s): too tight to "
                        "survive the spread, normal wiggle would stop you out. Widen it."
                        % (money(risk), money(floor_risk.quantize(CENT, rounding=ROUND_HALF_UP)), money(e)))
    if target_rule == "r":
        target = tick_up(e + target_param * risk)
        t_label = r_label(target_param)
    else:
        target = tick_near(target_param)
        t_label = None
    if target <= e:
        raise PlanError("the target %s is at or below the entry %s: a long's target goes above it"
                        % (money(target), money(e)))
    rr = ((target - e) / risk).quantize(CENT, rounding=ROUND_HALF_UP)
    if t_label is None:
        t_label = "your price (%sR)" % ratio(rr)
    return {"entry": e, "stop": stop, "stop_label": label, "stop_detail": detail, "target": target,
            "target_r": target_param if target_rule == "r" else rr, "target_label": t_label,
            "risk": risk, "reward_risk": rr}


# ---------------------------------------------------------------- bars for ATR (same reader as bharat-entry)

def _open_url(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _sleep(seconds):
    time.sleep(seconds)


def yahoo_get(path):
    """query2 first, query1 on 429 or 5xx; one retry after a pause on 429."""
    last = "no answer"
    for host in HOSTS:
        url = "https://%s.finance.yahoo.com%s" % (host, path)
        for attempt in (0, 1):
            try:
                return _open_url(url)
            except urllib.error.HTTPError as e:
                code, body = e.code, None
                try:
                    if code == 404:     # Yahoo says why in the body: an unknown ticker
                        body = e.read().decode("utf-8", "replace")
                except Exception:
                    body = None
                finally:
                    e.close()
                if code == 404:
                    if body:
                        return body
                    raise PlanError("Yahoo has no chart at %s (HTTP 404)" % url.split("?")[0])
                last = "HTTP %d" % code
                if code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if code == 429 or code >= 500:
                    break
                raise PlanError("Yahoo answered HTTP %d for %s" % (code, url.split("?")[0]))
            except urllib.error.URLError as e:
                last = "could not connect: %s" % (e.reason,)
                break
            except OSError as e:
                last = "network error: %s" % (e,)
                break
    raise PlanError("Yahoo did not answer (%s on query2 and query1): wait a minute and try again" % last)


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def load_chart(sym, interval, day, offline=None):
    ysym = india.yahoo_symbol(sym)
    if offline:
        path = offline
        if os.path.isdir(offline):
            path = os.path.join(offline, ysym + ".json")
            for alt in (path + ".gz", os.path.join(offline, sym.upper() + ".json"),
                        os.path.join(offline, sym.upper() + ".json.gz")):
                if not os.path.exists(path):
                    path = alt
        if not os.path.exists(path):
            raise PlanError("no offline file %s" % path)
        try:
            text = read_text(path)
        except (OSError, ValueError) as e:
            raise PlanError("could not read %s: %s" % (path, e))
    else:
        back = (to_local(utc_now()).date() - day).days
        rng = "1d" if back <= 0 else "5d" if back <= 6 else "1mo" if back <= 28 else "60d"
        if interval == "1m" and back > 6:
            raise PlanError("Yahoo keeps 1-minute bars for about a week")
        text = yahoo_get("/v8/finance/chart/%s?range=%s&interval=%s" % (
            urllib.parse.quote(ysym, safe="^."), rng, interval))
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise PlanError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise PlanError("Yahoo has no data for %s: %s" % (sym, err.get("description") or "unknown ticker"))
    r = res[0] or {}
    meta = r.get("meta") or {}
    if str(meta.get("symbol") or ysym).upper() != ysym:
        raise PlanError("the chart file holds %s, not %s" % (meta.get("symbol"), sym))
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        h, lo = at("high", i), at("low", i)
        bars.append({"t": int(t), "h": float(c if h is None else h), "l": float(c if lo is None else lo),
                     "c": float(c)})
    return {"bars": bars, "data_time": meta.get("regularMarketTime")}


def session_bars(chart, day, interval, upto_slot=None, now_utc=None):
    """Closed, on-grid bars of one NSE session, oldest first (the live tick is dropped)."""
    step = INTERVALS[interval]
    s = session(day)
    if not s:
        return []
    open_dt = datetime.datetime.combine(day, s[0])
    close_dt = datetime.datetime.combine(day, s[1])
    if now_utc is None and chart.get("data_time"):
        now_utc = utc_of(chart["data_time"])
    local = to_local(now_utc) if now_utc else None
    out = {}
    for b in chart["bars"]:
        if b["t"] % step:
            continue
        et = to_local(utc_of(b["t"]))
        if et.date() != day or not (open_dt <= et < close_dt):
            continue
        if local and et + datetime.timedelta(seconds=step) > local:
            continue
        slot = int((et - open_dt).total_seconds()) // step
        if upto_slot is not None and slot > upto_slot:
            continue
        out[slot] = b
    return [out[k] for k in sorted(out)]


def atr_of(bars, n=ATR_BARS):
    """Average true range of the last n bars (all of them if fewer). The first
    bar of the session uses its own high - low (no overnight gap)."""
    trs, prev = [], None
    for b in bars:
        tr = b["h"] - b["l"]
        if prev is not None:
            tr = max(tr, abs(b["h"] - prev), abs(b["l"] - prev))
        trs.append(tr)
        prev = b["c"]
    use = trs[-n:]
    return sum(use) / len(use), len(use)


# ---------------------------------------------------------------- plan

write_json = india.write_json   # whole or not at all: a write cut short leaves the old file


def read_json(path, what):
    if not path or not os.path.exists(path):
        raise PlanError("no %s at %s" % (what, path))
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except ValueError as e:
        raise PlanError("%s is not valid JSON: %s" % (path, e))


def read_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        doc = read_json(path, "config")
        for k in DEFAULTS:
            if doc.get(k) is not None:
                cfg[k] = doc[k]
    return cfg


def resolve_flat_by(day, explicit, cfg, entry_time):
    """(HH:MM, note). The flat-by has to be before the session's close."""
    t = parse_hhmm(explicit or cfg.get("flat_by") or DEFAULTS["flat_by"], "--flat-by" if explicit else "flat_by")
    note = ""
    s = session(day) if day else None
    if s:
        close_dt = datetime.datetime.combine(day, s[1])
        at = datetime.datetime.combine(day, t)
        if at >= close_dt:
            raise PlanError("%s %s is at or after the %s close on %s" % (
                "--flat-by" if explicit else "flat_by", hm(t), ampm(s[1]), day))
        if t > india.SQUARE_OFF:
            note = "after the broker's %s MIS auto square-off: it may close you first" % hm(india.SQUARE_OFF)
    if entry_time and entry_time >= t:
        raise PlanError("the entry at %s is at or after your flat-by %s: there is no time left to hold it"
                        % (ampm(entry_time), hm(t)))
    return hm(t), note


def gather(a):
    """The entry facts, from trader/entry.json or the flags."""
    if a.symbol and a.from_path:
        raise PlanError("use --from or --symbol, not both")
    if a.side in ("sell", "short"):
        raise PlanError("P1 plans longs only: no stops above a short entry. Plan a buy instead.")
    if a.symbol:
        if a.entry is None:
            raise PlanError("--symbol needs --entry PRICE")
        if (a.or_high is None) != (a.or_low is None):
            raise PlanError("give both --or-high and --or-low, or neither")
        info = {"symbol": a.symbol.upper(), "entry": a.entry, "setup": a.setup or ("orb" if a.or_high is not None else "level"),
                "date": a.date, "entry_time": a.time, "entry_bar": a.entry_bar, "interval": a.interval or "5m",
                "or_high": a.or_high, "or_low": a.or_low, "or_minutes": a.or_minutes, "source": "flags"}
    else:
        path = a.from_path or "trader/entry.json"
        doc = read_json(path, "entry (run /bharat-entry first, or give --symbol and --entry)")
        if str(doc.get("side") or "buy").lower() not in ("buy", "long"):
            raise PlanError("%s is a %s: P1 plans longs only" % (path, doc.get("side")))
        if doc.get("status") != "triggered" and a.entry is None:
            raise PlanError("%s says %s is %s, not triggered: plan once it triggers, or give --entry PRICE to plan ahead"
                            % (path, doc.get("symbol"), doc.get("status") or "unknown"))
        orr = doc.get("opening_range") or {}
        info = {"symbol": str(doc.get("symbol") or "").upper(), "entry": a.entry if a.entry is not None else doc.get("price"),
                "setup": a.setup or doc.get("setup") or "orb", "date": a.date or doc.get("date"),
                "entry_time": a.time or (doc.get("time") if a.entry is None else None),
                "entry_bar": a.entry_bar if a.entry_bar is not None else (doc.get("bar_index") if a.entry is None else None),
                "interval": a.interval or doc.get("interval") or "5m",
                "or_high": a.or_high if a.or_high is not None else orr.get("high"),
                "or_low": a.or_low if a.or_low is not None else orr.get("low"),
                "or_minutes": orr.get("minutes") or a.or_minutes, "source": path,
                "instrument": doc.get("instrument") or "eq", "future": doc.get("future"),
                "option": doc.get("option"), "index_side": doc.get("index_side") or "up"}
        if info["instrument"] == "fut" and not info["future"]:
            raise PlanError("%s is a futures entry with no future block: re-run bharat-entry --instrument fut" % path)
        if info["instrument"] == "fut":
            if doc.get("status") != "triggered":
                raise PlanError("%s is a futures entry that has not triggered: plan it once the index triggers" % path)
            info["entry"] = doc.get("price")  # the levels come from the index; --entry is the futures fill
            if a.entry is not None:
                info["future"] = dict(info["future"], price=a.entry, source="user", estimated=False)
        if info["instrument"] == "opt":
            if not info["option"]:
                raise PlanError("%s is an options entry with no option block: re-run bharat-entry --instrument opt"
                                % path)
            if doc.get("status") != "triggered":
                raise PlanError("%s is an options entry that has not triggered: plan it once the index triggers" % path)
            info["entry"] = doc.get("price")  # the index; --entry is the premium you paid
            info["entry_time"] = a.time or doc.get("time")
            info["entry_bar"] = a.entry_bar if a.entry_bar is not None else doc.get("bar_index")
            if a.entry is not None:
                info["option"] = dict(info["option"], premium=a.entry, premium_source="user", iv=None,
                                      premium_index=a.premium_index)
        if a.premium_index is not None and (info["instrument"] != "opt" or a.entry is None):
            raise PlanError("--premium-index is the index when you read the option's --entry premium (options only)")
        if not info["symbol"] or info["entry"] is None:
            raise PlanError("%s has no symbol or price" % path)
    if info["interval"] not in INTERVALS:
        raise PlanError("interval is 5m or 1m")
    return info


def futures_levels(c, fut):
    """The index plan moved to the futures contract: the stop and target keep their distance
    from the entry in points, times the basis (so a futures price you gave keeps the same risk)."""
    ratio = D(repr(fut["basis_ratio"]))
    entry_f = cents(fut["price"]) if fut.get("price") is not None else tick_near(c["entry"] * ratio)
    stop = tick_down(entry_f - (c["entry"] - c["stop"]) * ratio)
    target = tick_up(entry_f + (c["target"] - c["entry"]) * ratio)
    risk = entry_f - stop
    return {"instrument": "fut", "contract": fut["contract"], "expiry": fut["expiry"], "lot": int(fut["lot"]),
            "basis_ratio": fut["basis_ratio"], "basis_date": fut["basis_date"],
            "basis_estimated": bool(fut.get("estimated")), "fut_price_source": fut.get("source") or "bhavcopy",
            "index": {"entry": float(c["entry"]), "stop": float(c["stop"]), "target": float(c["target"])},
            "entry": float(entry_f), "stop": float(stop), "target": float(target),
            "risk_per_share": float(risk), "risk_per_unit": float(risk), "risk_per_lot": float(risk * int(fut["lot"])),
            "reward_risk": float(((target - entry_f) / risk).quantize(CENT, rounding=ROUND_HALF_UP))}


def mirror(c, entry):
    """A put's index levels from the long rule run on the index mirrored around the entry: the stop goes
    above the index, the target below (2 x entry - x)."""
    e2 = 2 * cents(entry)
    out = dict(c, entry=cents(entry), stop=e2 - c["stop"], target=e2 - c["target"])
    out["stop_label"] = c["stop_label"].replace("opening-range low", "opening-range high")
    return out


def option_levels(c, opt, at_ist, flat_ist):
    """The index stop and target as premiums: Black-76 on the forward (index x ratio) at the entry time, with
    the entry's implied volatility held constant. Stop ticks down (at least one tick), target ticks up.
    Refuses a target that hitting the index target at the flat-by would not pay: the time decay eats the move."""
    right, k, ratio, rate = opt["right"], float(opt["strike"]), float(opt["forward_ratio"]), float(opt["rate"])
    expiry = datetime.date.fromisoformat(opt["expiry"])
    years = india.years_to_expiry(at_ist, expiry)
    if years <= 0:
        raise PlanError("%s has expired at the entry time" % opt["contract"])
    premium = float(opt["premium"])
    iv = opt.get("iv")
    try:
        if iv is None and opt.get("premium_index") is not None:   # read later: solve where and when it was read
            iv = india.option_iv(right, premium, float(opt["premium_index"]), ratio, k,
                                 india.years_to_expiry(opt["premium_at"], expiry), rate)
        elif iv is None:
            iv = india.option_iv(right, premium, float(c["entry"]), ratio, k, years, rate)
    except ValueError as e:
        raise PlanError("%s at Rs %.2f: %s" % (opt["contract"], premium, e))
    notes = []

    def at(index, yrs):
        return india.option_price(right, float(index), ratio, k, yrs, iv, rate)
    stop = india.tick_round(at(c["stop"], years), mode="down")
    if stop < 0.05:
        stop = 0.05
        notes.append("The model prices the premium at the index stop under one tick: the stop is clamped at Rs 0.05, "
                     "so a stop-out can lose the whole premium.")
    target = india.tick_round(at(c["target"], years), mode="up")
    late = at(c["target"], india.years_to_expiry(flat_ist, expiry))
    if late <= premium:
        raise PlanError("%s cannot reach a profit by the flat-by: even at the index target %s at %s the model prices "
                        "it Rs %.2f, not above the Rs %.2f premium (time decay). Pick a nearer strike or a later expiry."
                        % (opt["contract"], num(D(c["target"])), flat_ist.strftime("%H:%M"), late, premium))
    if stop >= premium:
        raise PlanError("the premium stop %.2f is not under the premium %.2f" % (stop, premium))
    risk = round(premium - stop, 2)
    lot = int(opt["lot"])
    return {"instrument": "opt", "contract": opt["contract"], "expiry": opt["expiry"], "strike": k, "right": right,
            "lot": lot, "iv": round(iv, 6), "forward_ratio": ratio, "rate": rate,
            "premium_source": opt.get("premium_source") or "estimated", "oi": opt.get("oi"),
            "volume": opt.get("volume"), "illiquid": bool(opt.get("illiquid")), "bhav_date": opt.get("bhav_date"),
            "index_entry": float(c["entry"]), "index_stop": float(c["stop"]), "index_target": float(c["target"]),
            "entry": premium, "stop": stop, "target": target, "risk_per_share": risk, "risk_per_unit": risk,
            "risk_per_lot": round(risk * lot, 2), "reward_risk": round((target - premium) / risk, 2)}, notes


def build_plan(a, now_utc):
    info = gather(a)
    cfg = read_config(a.config)
    stop_text = a.stop
    if not stop_text:
        if info["setup"] != "orb":
            raise PlanError("a %s setup has no opening-range stop to default to: give --stop price:X, pct:X or atr:K"
                            % info["setup"])
        stop_text = "or-mid"
    stop_rule, stop_param = parse_stop(stop_text)
    target_rule, target_param = parse_target(a.target or "2R")
    day = None
    if info["date"]:
        try:
            day = datetime.date.fromisoformat(str(info["date"]))
        except ValueError:
            raise PlanError("date wants YYYY-MM-DD, not %r" % info["date"])
    else:
        day = to_local(now_utc).date()
    entry_time = parse_clock(info["entry_time"], "the entry time") if info["entry_time"] else None
    atr = atr_n = None
    if stop_rule == "atr":
        chart = load_chart(info["symbol"], info["interval"], day, a.offline)
        bars = session_bars(chart, day, info["interval"], info["entry_bar"], parse_now(a.now) if a.now else None)
        if len(bars) < 3:
            raise PlanError("an atr stop needs at least 3 closed %s bars of %s on %s by the entry; found %d"
                            % (info["interval"], info["symbol"], day, len(bars)))
        atr, atr_n = atr_of(bars)
    down = info.get("instrument") == "opt" and info.get("index_side") == "down"
    if down:  # a bought put: run the long rule on the index mirrored around the entry
        e2 = 2 * float(info["entry"])
        if stop_rule == "price":
            stop_param = D(repr(e2)) - stop_param
        if target_rule == "price":
            target_param = D(repr(e2)) - target_param
        c = compute(info["entry"], stop_rule, stop_param, target_rule, target_param,
                    e2 - info["or_low"] if info["or_low"] is not None else None,
                    e2 - info["or_high"] if info["or_high"] is not None else None, atr, atr_n)
        c = mirror(c, info["entry"])
    else:
        c = compute(info["entry"], stop_rule, stop_param, target_rule, target_param, info["or_high"], info["or_low"],
                    atr, atr_n)
    flat_by, flat_note = resolve_flat_by(day, a.flat_by, cfg, entry_time)
    orr = None
    if info["or_high"] is not None and info["or_low"] is not None:
        orr = {"high": float(cents(info["or_high"])), "low": float(cents(info["or_low"])),
               "minutes": int(info["or_minutes"] or 15)}
    plan = {"created_by": "bharat-exit", "symbol": info["symbol"], "side": "buy", "date": day.isoformat(),
            "setup": info["setup"], "entry": float(c["entry"]),
            "entry_time": ampm(entry_time) if entry_time else None,
            "entry_bar": info["entry_bar"], "interval": info["interval"], "opening_range": orr,
            "stop": float(c["stop"]), "stop_rule": stop_rule, "stop_label": c["stop_label"],
            "target": float(c["target"]), "target_r": float(c["target_r"]), "target_label": c["target_label"],
            "risk_per_share": float(c["risk"]), "reward_risk": float(c["reward_risk"]), "flat_by": flat_by,
            "stop_detail": c["stop_detail"], "created_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "note": NOTE}
    if info.get("instrument") == "fut":
        plan.update(futures_levels(c, info["future"]))
    opt_notes = []
    if info.get("instrument") == "opt":
        if not entry_time:
            raise PlanError("an options plan needs the entry time (the option's time to expiry starts there)")
        at_ist = datetime.datetime.combine(day, entry_time, india.IST)
        flat_ist = datetime.datetime.combine(day, parse_hhmm(flat_by, "flat_by"), india.IST)
        o = dict(info["option"], premium_at=to_local(now_utc).replace(tzinfo=india.IST))
        lv, opt_notes = option_levels(c, o, at_ist, flat_ist)
        plan.update(lv, index_side=info.get("index_side") or "up", target_label=plan["target_label"] + " on the index")
    if atr is not None:
        plan["atr"] = round(atr, 4)
        plan["atr_bars"] = atr_n
    warnings = list(opt_notes)
    if flat_note:
        warnings.append("Flat-by %s: %s." % (flat_by, flat_note))
    mrr = cfg.get("min_reward_risk")
    try:
        if mrr is not None and c["reward_risk"] < D(mrr):
            warnings.append("Reward:risk %s is under your min_reward_risk %s in the config: bharat-risk checks that."
                            % (ratio(c["reward_risk"]), num(D(mrr))))
    except PlanError:
        pass
    today = to_local(now_utc).date()
    if day != today:
        warnings.append("This plan is for %s, not today (%s)." % (day.isoformat(), today.isoformat()))
    return plan, warnings


def describe(plan):
    day = datetime.date.fromisoformat(plan["date"])
    setup = {"orb": "opening-range breakout", "level": "close above a level"}.get(plan["setup"], plan["setup"])
    e = D(plan["entry"])
    risk = D(plan["risk_per_share"])
    pct = (risk / e * 100).quantize(CENT, rounding=ROUND_HALF_UP)
    when = ""
    if plan.get("entry_time"):
        when = " at %s" % plan["entry_time"]
        if plan.get("entry_bar") is not None:
            when += " (bar %d)" % plan["entry_bar"]
    out = ["Exit plan: %s long, %s, %s on %s bars" % (
        plan["symbol"], day.strftime("%a %Y-%m-%d"), setup, "5-minute" if plan["interval"] == "5m" else "1-minute")]
    out.append("  Entry         %s%s" % (money(plan["entry"]), when))
    out.append("  Stop          %s  %s%s" % (money(plan["stop"]), plan["stop_label"],
                                             (": " + plan["stop_detail"]) if plan.get("stop_detail") else ""))
    out.append("  Target        %s  %s" % (money(plan["target"]), plan["target_label"]))
    if plan.get("instrument") == "opt":
        out[0] = out[0].replace(" long,", " option buy,")
        out.insert(1, "  Contract      buy %s (%s %g, expires %s, lot %d). The signal and the levels come from the index: "
                      "entry %s, stop %s, target %s; the premiums are Black-76 on the forward (index x %.6f) with "
                      "implied volatility %.2f%% held constant%s" % (
                          plan["contract"], "call" if plan["right"] == "CE" else "put", plan["strike"], plan["expiry"],
                          plan["lot"], num(D(plan["index_entry"])), num(D(plan["index_stop"])),
                          num(D(plan["index_target"])), plan["forward_ratio"], 100 * plan["iv"],
                          "" if plan["premium_source"] == "user" else ", from an ESTIMATED premium"))
        out.append("  Risk a unit   %s of premium (%s%% of it); Risk a lot %s" % (
            money(plan["risk_per_unit"]), pct, money(plan["risk_per_lot"])))
        if plan["premium_source"] != "user":
            out.append("  The premium is estimated from the last bhavcopy: read the live premium on Kite and give it "
                       "with --entry; bharat-risk blocks an entry on the estimate.")
    elif plan.get("instrument") == "fut":
        ix = plan["index"]
        out.insert(1, "  Contract      %s, expires %s, lot %d; the signal and the levels come from the index "
                      "(entry %s, stop %s, target %s) x basis %.6f from %s%s" % (
                          plan["contract"], plan["expiry"], plan["lot"], num(D(ix["entry"])), num(D(ix["stop"])),
                          num(D(ix["target"])), plan["basis_ratio"], plan["basis_date"],
                          ", ESTIMATED (old basis)" if plan["basis_estimated"] else ""))
        out.append("  Risk a unit   %s (%s%% of the entry); Risk a lot %s" % (
            money(plan["risk_per_unit"]), pct, money(plan["risk_per_lot"])))
        if plan["fut_price_source"] != "user":
            out.append("  The futures entry is an estimate from the basis: give the live Kite price with --entry "
                       "(the stop and target keep their points), or size a limit order at it; bharat-risk "
                       "blocks a market entry on the estimate.")
    else:
        out.append("  Risk a share  %s (%s%% of the entry)" % (money(plan["risk_per_share"]), pct))
    out.append("  Reward:risk   %s" % ratio(plan["reward_risk"]))
    out.append("  Flat by       %s IST, win or lose: no overnight hold" % plan["flat_by"])
    out.append("Set before the entry. If price hits the stop, you are out; never move it further away.")
    out.append(NOTE)
    return out


def cmd_plan(a):
    now = parse_now(a.now)
    plan, warnings = build_plan(a, now)
    if a.write:
        write_json(a.write, plan)
    if a.json:
        print(json.dumps(dict(plan, warnings=warnings), indent=2))
        return 0
    for line in describe(plan):
        print(line)
    for w in warnings:
        print("Note: " + w)
    if a.write:
        print("Wrote %s." % a.write)
    print("Next: /bharat-timemachine replays it, then /bharat-budget sizes it to your max loss. Nothing was placed.")
    return 0


def cmd_show(a):
    if not os.path.exists(a.plan):
        print("No plan at %s yet. Make one: exit.py plan --write" % a.plan)
        return 1
    plan = read_json(a.plan, "plan")
    for k in ("symbol", "date", "entry", "stop", "target", "risk_per_share", "reward_risk", "flat_by"):
        if plan.get(k) is None:
            raise PlanError("%s is missing %r: make a new plan with exit.py plan --write" % (a.plan, k))
    if a.json:
        print(json.dumps(plan, indent=2))
        return 0
    for line in describe(dict({"setup": "orb", "interval": "5m", "stop_label": "", "target_label": ""}, **plan)):
        print(line)
    today = to_local(parse_now(a.now)).date().isoformat()
    if plan["date"] != today:
        print("Note: this plan is for %s, not today (%s)." % (plan["date"], today))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Exit Planner (bharat-exit): the stop and target before the entry.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("plan", help="set the stop and target")
    p.add_argument("--from", dest="from_path", metavar="PATH", help="entry.json to plan from (default trader/entry.json)")
    p.add_argument("--symbol")
    p.add_argument("--entry", type=float, help="entry price (or a plan-ahead price with --from)")
    p.add_argument("--premium-index", type=float, help="options: the index when you read the --entry premium "
                                                       "(the implied volatility is solved there, at --now)")
    p.add_argument("--or-high", type=float)
    p.add_argument("--or-low", type=float)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--setup", choices=["orb", "level"])
    p.add_argument("--date", help="session date YYYY-MM-DD (default: the entry's, or today)")
    p.add_argument("--time", help="entry time, e.g. 10:15 AM")
    p.add_argument("--entry-bar", type=int)
    p.add_argument("--interval", choices=["5m", "1m"])
    p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="P1: buy only")
    p.add_argument("--stop", help="or-mid (orb default), or-low, price:X, pct:X, atr:K")
    p.add_argument("--target", help="2R (default), 1R, 1.5R, price:X")
    p.add_argument("--flat-by", help="HH:MM IST (default: config flat_by, 15:15)")
    p.add_argument("--config", default="trader/config.json")
    p.add_argument("--offline", metavar="PATH", help="saved Yahoo chart JSON for an atr stop")
    p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-10-19 09:50"')
    p.add_argument("--write", nargs="?", const="trader/plan.json", metavar="PATH",
                   help="save the plan (default trader/plan.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show", help="print the current plan")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"plan": cmd_plan, "show": cmd_show}[a.cmd](a)
    except PlanError as e:
        print("exit.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
