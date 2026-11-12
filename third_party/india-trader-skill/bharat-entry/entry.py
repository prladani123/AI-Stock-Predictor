#!/usr/bin/env python3
"""Entry Timer for bharat-entry: watches a setup bar by bar and says the
moment to get in. Standard library only. Long side only. It never places an order.

    python3 entry.py check RELIANCE                          # opening-range breakout, 5-minute bars
    python3 entry.py check RELIANCE --now "2026-10-19 09:50" --offline RELIANCE.json
    python3 entry.py check RELIANCE --setup level --level 1250   # first close above a price
    python3 entry.py check RELIANCE --interval 1m --min-volume-x 1.5 --write
    python3 entry.py watch RELIANCE --every 30 --until 14:45  # one line per closed bar
    python3 entry.py watchlist                            # every hit in trader/watchlist.json

Setups (P1 plans longs only):
  orb    opening-range breakout. The range is the high and low of the first
         --or-minutes (default 15: the 9:15, 9:20 and 9:25 five-minute bars).
         It triggers on the first COMPLETED bar after the range that CLOSES
         above the range high. A wick above the high is not a trigger.
  level  the first completed bar that closes above --level (for example a
         20-day high from bharat-scanner).
  --min-volume-x X  the trigger bar also needs at least X times the average
         volume of the opening-range bars. Yahoo reports volume 0 for NSE's
         9:15 bar, so the average skips it.

A trigger's time is the trigger bar's CLOSE time (the 9:45 bar closes at
9:50 AM) and its price is that close, to the paisa. Only bars on the interval
grid count (Yahoo's live tick and the 15:30 closing print are dropped) and a
bar counts only once it has closed: now >= its start + the interval.

Price band: with --band PCT (or the band bharat-scanner wrote on the
watchlist hit), the result warns when the price is within 1 % of the band's
upper limit, worked out from the previous session's close. A stock frozen at
its band can leave you unable to exit.

Statuses: triggered; waiting (not open yet, range still forming, or no close
above the level yet); expired (no trigger by no_entries_after, default
14:45); no-data (NSE is closed today or Yahoo sent no bars).

trader/config.json (optional): "no_entries_before" (09:30) and
"no_entries_after" (14:45), IST.

watch wakes just after each bar closes (bars only change then), re-polls every
--every seconds (default 30) while Yahoo has not posted the new bar, prints one
line per closed bar and stops on a trigger (exit 0, writes trader/entry.json),
on expiry, at the close or at --until (exit 1). With --now or --offline it runs
on a simulated clock and replays the day without sleeping.

Data: Yahoo's free chart JSON (unofficial, can lag, bars not single trades)
  https://query2.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?range=1d&interval=5m
  falls back to query1 on HTTP 429 or 5xx; a 429 gets one retry after a pause.
  --offline PATH reads a saved chart JSON (.json or .json.gz) instead, or
  PATH/<SYMBOL>.json when PATH is a folder. Without --now, an offline file is
  read as of its own time (meta.regularMarketTime).

Exit codes: check 0 = triggered, 1 = anything else or an error. watch 0 on a
trigger, 1 otherwise. watchlist 0 when at least one symbol triggered.
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

import india

UA = india.TOOL_UA
PAUSE = 0.5      # seconds between requests to the free endpoint
BACKOFF = 5.0    # seconds before the one retry after an HTTP 429
GRACE = 2        # seconds after a bar closes before watch asks for it
SETTLE = 60      # seconds a closed bar may wait for Yahoo's data to catch up
HOSTS = ("query2", "query1")
INTERVALS = {"1m": 60, "5m": 300}
DEFAULTS = {"no_entries_before": "09:30", "no_entries_after": "14:45"}
OPT_DEFAULTS = {"min_option_oi": 50000, "risk_free_rate": 0.06}
NEAR_BAND = 0.01  # warn within 1 % of the band limit
EPOCH = datetime.datetime(1970, 1, 1)

OPEN = india.OPEN
CLOSE = india.CLOSE


FUT_INDEX = ("NIFTY", "BANKNIFTY")
BHAV_URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_%s_F_0000.csv.zip"


class EntryError(Exception):
    pass


# ---------------------------------------------------------------- index futures

def load_bhavcopy(path, day):
    """F&O bhavcopy rows from PATH, or downloaded for the last session before day."""
    if path:
        try:
            with open(path, "rb") as fh:
                return india.parse_bhavcopy(fh.read())
        except (OSError, ValueError, KeyError) as e:
            raise EntryError("cannot read the bhavcopy %s: %s" % (path, e))
    last = india.prev_trading_day(day)
    if last is None:
        raise EntryError("no known trading day in the 30 days before %s: NSE's holiday list for %d is not in india.py "
                         "yet. Save the last session's F&O bhavcopy from nseindia.com and pass --bhavcopy FILE"
                         % (day, day.year))
    try:
        return india.parse_bhavcopy(india.nse_get(BHAV_URL % last.strftime("%Y%m%d"), want_json=False, warm=False))
    except (india.NSEError, ValueError, KeyError) as e:
        raise EntryError("could not get the F&O bhavcopy for %s (%s): save it and pass --bhavcopy FILE" % (last, e))


def last_session_words(last, day):
    """Why a bhavcopy may be old: older than the last session, or no last session is known."""
    if last is not None:
        return "older than the last session (%s)" % last
    return ("and the last session before %s is unknown (NSE's %d holiday list is not in india.py yet), so it may "
            "be older" % (day, day.year))


def future_leg(rows, symbol, day, index_price, fut_price=None):
    """The futures contract for a signal on the index: contract, lot, basis and price.
    price = index price x (futures close / index close) from the bhavcopy, tick-rounded;
    a fut_price the user read off Kite wins."""
    f = india.front_future(rows, symbol, day)
    if not f:
        raise EntryError("no %s future after %s in the bhavcopy" % (symbol, day))
    ratio = india.basis_ratio(f)
    last = india.prev_trading_day(day)
    stale = last is None or f["date"] < last   # no known last session: the basis may be any age
    warnings = []
    if fut_price is not None:
        price, source = round(float(fut_price), 2), "user"
    else:
        price, source = (india.tick_round(index_price * ratio) if index_price else None), "bhavcopy"
        if stale:
            warnings.append("The basis is from %s, %s: the futures price is estimated from %s. Read the live "
                            "price off Kite and pass --fut-price." % (f["date"], last_session_words(last, day),
                                                                       f["date"]))
    if india._trading_days_between(day, f["expiry"]) == 1:
        nxt = india.front_future(rows, symbol, day, roll_days=1)
        warnings.append("%s expires on the next trading day (%s): it rolls over. A new entry should be in %s." % (
            f["contract"], f["expiry"], nxt["contract"] if nxt else "the next month"))
    return {"contract": f["contract"], "expiry": f["expiry"].isoformat(), "lot": f["lot"], "basis_ratio": ratio,
            "basis_date": f["date"].isoformat(), "price": price, "source": source,
            "estimated": bool(stale and source == "bhavcopy"), "warnings": warnings}


def attach_future(res, a, rows=None):
    """For --instrument fut, add res["future"] (the signal itself stays on the index)."""
    if a.instrument != "fut":
        return
    day = datetime.date.fromisoformat(res["date"])
    t = res.get("trigger")
    res["instrument"] = "fut"
    res["future"] = future_leg(rows if rows is not None else load_bhavcopy(a.bhavcopy, day), res["symbol"], day,
                               t["price"] if t else None, a.fut_price)


def future_lines(f):
    if not f:
        return []
    out = ["Futures: %s (expires %s, lot %d). %s" % (
        f["contract"], f["expiry"], f["lot"],
        ("Price Rs %.2f, given by you." % f["price"]) if f["source"] == "user" else
        ("Price about Rs %.2f = the index x %.6f, the basis from the %s bhavcopy; the basis moves during the "
         "day, so the fill will differ." % (f["price"], f["basis_ratio"], f["basis_date"])) if f["price"] else
        ("Basis %.6f from the %s bhavcopy." % (f["basis_ratio"], f["basis_date"])))]
    return out + ["WARNING: " + w for w in f["warnings"]]


# ---------------------------------------------------------------- index options

def option_leg(rows, symbol, day, index_price, at_ist, right, steps_otm=0, expiry=None, premium=None, cfg=None,
               premium_index=None, premium_at=None):
    """The option for a signal on the index: contract, IV and premium (Black-76 on the parity forward).
    The IV comes from a premium you give (Kite), else from the bhavcopy close: then the premium is estimated."""
    c = dict(OPT_DEFAULTS)
    c.update(cfg or {})
    rate = float(c["risk_free_rate"])
    exps = [e for e in india.option_expiries(rows, symbol) if e > day]
    if expiry:
        if expiry not in exps:
            raise EntryError("no %s options expiring %s in the bhavcopy (expiries: %s)" % (
                symbol, expiry, ", ".join(e.isoformat() for e in exps[:6])))
    elif not exps:
        raise EntryError("no %s option expiry after %s in the bhavcopy" % (symbol, day))
    expiry = expiry or exps[0]
    try:
        strike = india.pick_strike(india.strikes(rows, symbol, expiry), index_price, right, steps_otm)
        ratio = india.forward_ratio(rows, symbol, expiry, rate)
    except ValueError as e:
        raise EntryError(str(e))
    row = india.option_row(rows, symbol, expiry, strike, right)
    if not row:
        raise EntryError("no %s %s %g %s in the bhavcopy" % (symbol, expiry, strike, right))
    years = india.years_to_expiry(at_ist, expiry)
    try:
        if premium is not None:
            source = "user"
            if premium_index is not None:   # read later: solve at the index and the time it was read
                iv = india.option_iv(right, float(premium), premium_index, ratio, strike,
                                     india.years_to_expiry(premium_at, expiry), rate)
            else:
                iv = india.option_iv(right, float(premium), index_price, ratio, strike, years, rate)
            price = round(float(premium), 2)
        else:
            source = "estimated"
            closed = datetime.datetime.combine(row["date"], CLOSE, india.IST)
            iv = india.option_iv(right, row["close"], row["underlying"], ratio, strike,
                                 india.years_to_expiry(closed, expiry), rate)
            price = india.tick_round(india.option_price(right, index_price, ratio, strike, years, iv, rate))
    except ValueError as e:
        raise EntryError("%s: %s" % (row["contract"], e))
    warnings = []
    min_oi = int(c["min_option_oi"] or 0)
    illiquid = row["oi"] < min_oi or row["volume"] == 0
    if row["oi"] < min_oi:
        warnings.append("%s open interest is %s units, under your %s (min_option_oi): an illiquid strike, "
                        "bharat-risk blocks it" % (row["contract"], format(row["oi"], ","), format(min_oi, ",")))
    if row["volume"] == 0:
        warnings.append("%s had no trades in the %s bhavcopy: an illiquid strike" % (row["contract"], row["date"]))
    last = india.prev_trading_day(day)
    if last is None or row["date"] < last:
        warnings.append("The bhavcopy is from %s, %s." % (row["date"], last_session_words(last, day)))
    return {"contract": row["contract"], "expiry": expiry.isoformat(), "strike": strike, "right": right,
            "lot": row["lot"], "premium": price, "premium_source": source, "iv": round(iv, 6), "oi": row["oi"],
            "volume": row["volume"], "illiquid": illiquid, "years": years, "forward_ratio": ratio, "rate": rate,
            "bhav_date": row["date"].isoformat(), "bhav_close": row["close"], "warnings": warnings}


def attach_option(res, a, cfg, rows=None):
    """For --instrument opt, add res["option"] at the trigger (or the last close while waiting)."""
    if a.instrument != "opt":
        return
    res["instrument"] = "opt"
    t = res.get("trigger")
    price = t["price"] if t else (res.get("last") or {}).get("close")
    if price is None:
        return
    day = datetime.date.fromisoformat(res["date"])
    at = datetime.datetime.combine(day, parse_hhmm(t["close_time"], "trigger") if t else
                                   datetime.datetime.strptime(res["now"], "%Y-%m-%d %H:%M:%S").time(), india.IST)
    exp = datetime.date.fromisoformat(a.expiry) if a.expiry else None
    now = datetime.datetime.strptime(res["now"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=india.IST)
    res["option"] = option_leg(rows if rows is not None else load_bhavcopy(a.bhavcopy, day), res["symbol"], day,
                               price, at, a.right, a.steps_otm, exp, a.premium, cfg, a.premium_index, now)


def option_lines(o):
    if not o:
        return []
    out = ["Option: buy %s (%s %g, expires %s, lot %d). %s" % (
        o["contract"], "call" if o["right"] == "CE" else "put", o["strike"], o["expiry"], o["lot"],
        ("Premium Rs %.2f, given by you; implied volatility %.2f%% from it." % (o["premium"], 100 * o["iv"]))
        if o["premium_source"] == "user" else
        ("Premium about Rs %.2f, estimated: Black-76 on the forward (index x %.6f) with the implied volatility "
         "%.2f%% of the %s close Rs %.2f, held constant. Options move fast: read the live premium on Kite and pass "
         "--premium." % (o["premium"], o["forward_ratio"], 100 * o["iv"], o["bhav_date"], o["bhav_close"])))]
    return out + ["WARNING: " + w for w in o["warnings"]]


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
    """Unix seconds -> naive UTC, without the machine's time zone."""
    return EPOCH + datetime.timedelta(seconds=int(ts))


def ts_of(utc):
    return int((utc.replace(tzinfo=None) - EPOCH).total_seconds())


def iso(utc):
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_now(text):
    """--now -> naive UTC. "2026-10-19 10:15" (or ...T10:15, or just 10:15 for
    today) is IST; an offset or Z makes it absolute."""
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
        raise EntryError('--now wants an IST time like "2026-10-19 10:15" (or ...Z for UTC)')
    if dt.tzinfo is None:
        return local_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, what):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise EntryError("%s wants an IST time like 14:45, not %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def hm(t):
    """10:10, 09:45: the contract's wall-clock form."""
    return "%02d:%02d" % (t.hour, t.minute)


def short(t):
    """9:45, 10:10: for sentences."""
    return "%d:%02d" % (t.hour, t.minute)


def ampm(t):
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


trading_day = india.trading_day


def session(d):
    """(open_time, close_time) in IST, or None if NSE is closed all day."""
    s = india.session(d)
    return (s[0].time(), s[1].time()) if s else None


def next_open(d):
    nxt = india.next_open(d)
    return nxt.strftime("%Y-%m-%d %H:%M") if nxt else None


# ---------------------------------------------------------------- Yahoo

def yahoo_symbol(sym):
    """NSE symbol -> Yahoo, URL-encoded (M&M -> M%26M.NS)."""
    return urllib.parse.quote(india.yahoo_symbol(sym), safe="^.")


def chart_path(sym, rng, interval):
    return "/v8/finance/chart/%s?range=%s&interval=%s" % (yahoo_symbol(sym), rng, interval)


def chart_range(day, today, interval):
    """The smallest Yahoo range that still holds the session of `day`."""
    back = (today - day).days
    if back <= 0:
        return "1d"
    if back <= 6:
        return "5d"
    if interval == "1m":
        raise EntryError("Yahoo keeps 1-minute bars for about a week: use --interval 5m for %s" % day)
    if back <= 28:
        return "1mo"
    if back <= 58:
        return "60d"
    raise EntryError("Yahoo keeps 5-minute bars for about 60 days: %s is too far back" % day)


def _open_url(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _sleep(seconds):
    time.sleep(seconds)


def yahoo_get(path):
    """GET a Yahoo path: query2 first, query1 when query2 answers 429 or 5xx.
    An HTTP 429 gets one retry on the same host after BACKOFF seconds."""
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
                    raise EntryError("Yahoo has no chart at %s (HTTP 404)" % url.split("?")[0])
                last = "HTTP %d" % code
                if code == 429 and attempt == 0:
                    _sleep(BACKOFF)
                    continue
                if code == 429 or code >= 500:
                    break
                raise EntryError("Yahoo answered HTTP %d for %s" % (code, url.split("?")[0]))
            except urllib.error.URLError as e:
                last = "could not connect: %s" % (e.reason,)
                break
            except OSError as e:
                last = "network error: %s" % (e,)
                break
    raise EntryError("Yahoo did not answer (%s on query2 and query1): wait a minute and try again" % last)


def parse_chart(text, sym):
    """Yahoo chart JSON v8 -> {symbol, meta, bars, prev_close, data_time}.
    Bars with a null close are dropped here; off-grid ticks later."""
    try:
        data = json.loads(text)
        res = data["chart"]["result"]
    except (ValueError, KeyError, TypeError):
        raise EntryError("Yahoo returned something unexpected for %s" % sym)
    if not res:
        err = (data.get("chart") or {}).get("error") or {}
        raise EntryError("Yahoo has no data for %s: %s" % (sym, err.get("description") or "unknown ticker"))
    r = res[0] or {}
    meta = r.get("meta") or {}
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0] or {}

    def at(key, i):
        arr = q.get(key) or []
        return arr[i] if i < len(arr) else None

    bars = []
    for i, t in enumerate(r.get("timestamp") or []):
        c = at("close", i)
        if t is None or c is None:
            continue
        o, h, lo = at("open", i), at("high", i), at("low", i)
        bars.append({"t": int(t), "o": float(c if o is None else o), "h": float(c if h is None else h),
                     "l": float(c if lo is None else lo), "c": float(c), "v": int(at("volume", i) or 0)})
    return {"symbol": str(meta.get("symbol") or sym).upper(), "meta": meta, "bars": bars,
            "prev_close": meta.get("previousClose") or meta.get("chartPreviousClose"),
            "data_time": meta.get("regularMarketTime")}


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def offline_file(path, sym):
    if os.path.isdir(path):
        y = india.yahoo_symbol(sym)
        for name in (sym + ".json", sym + ".json.gz", y + ".json", y + ".json.gz", sym + "-5m.json"):
            p = os.path.join(path, name)
            if os.path.exists(p):
                return p
        raise EntryError("no offline file for %s in %s (want %s.json)" % (sym, path, sym))
    if not os.path.exists(path):
        raise EntryError("no offline file %s" % path)
    return path


def get_chart(sym, interval, day=None, offline=None, today=None):
    """Bars for one ticker, from Yahoo (range=1d for today) or a saved file."""
    sym = sym.upper()
    if offline:
        f = offline_file(offline, sym)
        try:
            text = read_text(f)
        except (OSError, ValueError) as e:
            raise EntryError("could not read %s: %s" % (f, e))
    else:
        today = today or to_local(utc_now()).date()
        text = yahoo_get(chart_path(sym, chart_range(day or today, today, interval), interval))
    ch = parse_chart(text, sym)
    if ch["symbol"] != india.yahoo_symbol(sym):
        raise EntryError("the chart file holds %s, not %s" % (ch["symbol"], sym))
    ch["symbol"] = sym
    gran = ch["meta"].get("dataGranularity")
    if gran and gran != interval:
        raise EntryError("%s has %s bars, not %s: use --interval %s" % (sym, gran, interval, gran))
    return ch


# ---------------------------------------------------------------- the rule

def read_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except ValueError as e:
            raise EntryError("%s is not valid JSON: %s" % (path, e))
        for k in DEFAULTS:
            if doc.get(k):
                cfg[k] = doc[k]
        for k in OPT_DEFAULTS:
            if doc.get(k) is not None:
                cfg[k] = doc[k]
    for k in DEFAULTS:
        parse_hhmm(cfg[k], k)
    return cfg


def r2(x):
    return None if x is None else round(float(x), 2) + 0.0   # + 0.0 turns -0.0 into 0.0


def prev_session_close(chart, day):
    """The last close before `day` in the chart's own bars; else Yahoo's meta
    (which is only right for a range=1d request)."""
    before = [b for b in chart["bars"] if to_local(utc_of(b["t"])).date() < day]
    return before[-1]["c"] if before else chart.get("prev_close")


def evaluate(chart, now_utc, setup="orb", or_minutes=15, interval="5m", level=None,
             min_volume_x=None, cfg=None, band=None, below=False):
    """Pure function: the setup's status at now_utc from one session's bars.
    below=True is the mirror for a bought put: a close BELOW the range low (or the level)."""
    cfg = cfg or dict(DEFAULTS)
    if interval not in INTERVALS:
        raise EntryError("--interval is 5m or 1m")
    step = INTERVALS[interval]
    if or_minutes <= 0 or (or_minutes * 60) % step:
        raise EntryError("--or-minutes must be a whole number of %s bars" % interval)
    if setup not in ("orb", "level"):
        raise EntryError("--setup is orb or level")
    if setup == "level" and level is None:
        raise EntryError("--setup level needs --level PRICE (for example a 20-day high)")
    n_or = or_minutes * 60 // step
    local = to_local(now_utc)
    day = local.date()
    res = {"symbol": chart["symbol"], "setup": setup, "side": "buy", "date": day.isoformat(),
           "interval": interval, "or_minutes": or_minutes, "now": local.strftime("%Y-%m-%d %H:%M:%S"), "status": "waiting",
           "phase": "", "reason": "", "opening_range": None, "level": None, "trigger": None,
           "last": None, "forming": None, "min_volume_x": min_volume_x,
           "prev_close": None, "calendar_known": day.year in india.HOLIDAY_YEARS, "band": band,
           "near_band": None, "index_side": "down" if below else "up", "_bars": []}
    dt = chart.get("data_time")
    res["data_time"] = to_local(utc_of(dt)).strftime("%Y-%m-%d %H:%M:%S") if dt else None
    ok, why = trading_day(day)
    if not ok:
        res.update(status="no-data", phase="closed", reason="NSE is closed today (%s)" % why,
                   next_open=next_open(day))
        return res
    o, c = session(day)
    open_dt = datetime.datetime.combine(day, o)
    close_dt = datetime.datetime.combine(day, c)
    res["prev_close"] = r2(prev_session_close(chart, day))
    before = datetime.datetime.combine(day, parse_hhmm(cfg["no_entries_before"], "no_entries_before"))
    cutoff = min(datetime.datetime.combine(day, parse_hhmm(cfg["no_entries_after"], "no_entries_after")), close_dt)
    or_end = open_dt + datetime.timedelta(minutes=or_minutes)
    res.update(opens=hm(o), closes=hm(c), no_entries_before=hm(before), no_entries_after=hm(cutoff))

    by_slot = {}
    for b in chart["bars"]:
        if b["t"] % step:
            continue                       # Yahoo's live tick (10:49:45) is not a bar
        et = to_local(utc_of(b["t"]))
        if et.date() != day or not (open_dt <= et < close_dt):
            continue
        slot = int((et - open_dt).total_seconds()) // step
        by_slot[slot] = dict(b, et=et, end=et + datetime.timedelta(seconds=step), slot=slot)
    bars, pending = [], None
    now_ts = ts_of(now_utc)
    top = max(by_slot) if by_slot else -1
    for slot in sorted(by_slot):
        b = by_slot[slot]
        end_ts = b["t"] + step
        if end_ts <= now_ts:
            # Closed by the clock. On live data it counts once Yahoo has caught up: its last
            # trade is at or after the close, a later bar exists, or a minute has passed.
            if dt is None or dt >= end_ts or slot < top or now_ts >= end_ts + SETTLE:
                bars.append(b)
            else:
                pending = b
                res["forming"] = {"bar_start": hm(b["et"]), "closes": ampm(b["end"]), "price": r2(b["c"])}
                res["lag_note"] = ("Yahoo's last trade is %s, before the %s bar's close: it counts once Yahoo "
                                   "catches up (a minute at most)" % (to_local(utc_of(dt)).strftime("%H:%M:%S"),
                                                                       short(b["et"])))
        elif b["et"] <= local:
            res["forming"] = {"bar_start": hm(b["et"]), "closes": ampm(b["end"]), "price": r2(b["c"])}
    res["_bars"] = bars
    res["bars_closed"] = len(bars)

    if local < open_dt:
        res.update(phase="pre-open", reason="NSE opens at %s%s" % (short(open_dt),
            " and the %d-minute range is set at %s" % (or_minutes, short(or_end)) if setup == "orb" else ""))
        return res

    or_bars = [b for b in bars if b["slot"] < n_or]
    # Yahoo reports volume 0 for NSE's 9:15 bar: leave it out of the average
    vol_bars = [b for b in or_bars if b["slot"] > 0 or b["v"] > 0] or or_bars
    avg_vol = (sum(b["v"] for b in vol_bars) / float(len(vol_bars))) if vol_bars else None
    if or_bars:
        res["opening_range"] = {"high": r2(max(b["h"] for b in or_bars)), "low": r2(min(b["l"] for b in or_bars)),
                                "minutes": or_minutes, "bars": len(or_bars), "of": n_or,
                                "complete": local >= or_end, "avg_volume": int(round(avg_vol))}
    if setup == "orb":
        if local < or_end:
            res.update(phase="forming", reason="%d of %d range bars closed; the range is set at %s" % (
                len(or_bars), n_or, short(or_end)))
            return res
        if not or_bars:
            res.update(status="no-data", phase="open", reason="Yahoo sent no bars for the opening range")
            return res
        lvl = min(b["l"] for b in or_bars) if below else max(b["h"] for b in or_bars)
    else:
        lvl = float(level)
    res["level"] = r2(lvl)

    def crossed(c):
        return c < lvl if below else c > lvl
    side_word = "below" if below else "above"
    edge = "the range low" if below else "the range high"
    first_slot = n_or if setup == "orb" else 0
    trig, thin = None, []
    for b in bars:
        if b["slot"] < first_slot or b["end"] < before or b["end"] > cutoff:
            continue
        if crossed(b["c"]):
            vx = (b["v"] / avg_vol) if avg_vol else None
            if min_volume_x and (vx is None or vx < min_volume_x):
                thin.append(b)
                continue
            trig = b
            break
    last = bars[-1] if bars else None
    mark = trig or last
    if band and res["prev_close"] and mark and not below:
        upper = india.tick_round(res["prev_close"] * (1 + band / 100.0), mode="down")
        if mark["c"] >= upper * (1 - NEAR_BAND):
            res["near_band"] = {"band": band, "upper": r2(upper), "prev_close": res["prev_close"],
                                "price": r2(mark["c"])}
    if last:
        res["last"] = {"bar_start": hm(last["et"]), "close": r2(last["c"]),
                       "vs_level_pct": r2(100.0 * (last["c"] / lvl - 1))}

    if trig:
        vx = (trig["v"] / avg_vol) if avg_vol else None
        res.update(status="triggered", phase="open" if local < close_dt else "after-close",
                   reason="the %s bar closed %s %s" % (short(trig["et"]), side_word, edge if setup == "orb" else "the level"))
        res["trigger"] = {"time": ampm(trig["end"]), "close_time": hm(trig["end"]), "bar_start": hm(trig["et"]),
                          "bar_index": trig["slot"], "price": r2(trig["c"]), "close": trig["c"],
                          "volume": trig["v"], "volume_x": r2(vx), "pct_over": r2(100.0 * (trig["c"] / lvl - 1)),
                          "et": hm(trig["end"])}
        res["minutes_since"] = int((local - trig["end"]).total_seconds() // 60)
        if dt and utc_of(dt) < local_to_utc(trig["end"]):
            res["data_note"] = ("Yahoo's last trade is %s, before this bar closed: its close can still move a cent or two"
                                % to_local(utc_of(dt)).strftime("%H:%M:%S"))
        return res
    if not bars:
        res.update(status="no-data", phase="open", reason="Yahoo sent no bars for %s yet" % day.isoformat())
        return res
    what = ("close %s %s Rs %.2f" % (side_word, edge, lvl)) if setup == "orb" else ("close %s Rs %.2f" % (side_word, lvl))
    if local >= cutoff and not (pending and pending["end"] <= cutoff):
        res.update(status="expired", phase="after-close" if local >= close_dt else "after-cutoff",
                   reason="no %s by %s, the last time for a new entry" % (what, short(cutoff)))
        return res
    res["phase"] = "open"
    if crossed(last["c"]) and last in thin:
        res["reason"] = "the %s bar closed %s at Rs %.2f but on %.2fx the range's volume, under your %.2fx" % (
            short(last["et"]), side_word, last["c"], last["v"] / avg_vol if avg_vol else 0, min_volume_x)
    elif crossed(last["c"]) and last["end"] < before:
        res["reason"] = "the %s bar closed %s at Rs %.2f, but no entries before %s (config)" % (
            short(last["et"]), side_word, last["c"], short(before))
    else:
        gap = lvl - last["c"]
        res["reason"] = "last close Rs %.2f (%s bar), Rs %.2f %s %s (%+.2f%%)" % (
            last["c"], short(last["et"]), abs(gap), "under" if gap >= 0 else "over",
            ("%s Rs %.2f" % (edge, lvl)) if setup == "orb" else "the level Rs %.2f" % lvl,
            100.0 * (last["c"] / lvl - 1))
    return res


def public(res):
    """The result without the internal bar list, ready for JSON."""
    return {k: v for k, v in res.items() if not k.startswith("_")}


def entry_doc(res, now_utc):
    """trader/entry.json, the contract shape (plus a plain-words reason)."""
    t = res.get("trigger") or {}
    orr = res.get("opening_range")
    return {"created_by": "bharat-entry", "symbol": res["symbol"], "setup": res["setup"], "side": "buy",
            "date": res["date"], "status": res["status"], "time": t.get("time"), "bar_start": t.get("bar_start"),
            "bar_index": t.get("bar_index"), "price": t.get("price"), "level": res.get("level"),
            "interval": res["interval"],
            "opening_range": {"high": orr["high"], "low": orr["low"], "minutes": orr["minutes"]} if orr else None,
            "source": "yahoo", "checked_at": iso(now_utc), "reason": res.get("reason"),
            "prev_close": res.get("prev_close"), "band": res.get("band"), "near_band": res.get("near_band"),
            "instrument": res.get("instrument", "eq"), "future": res.get("future"),
            "index_side": res.get("index_side", "up"), "option": res.get("option")}


write_json = india.write_json   # whole or not at all: a write cut short leaves the old file


# ---------------------------------------------------------------- words

LABEL = {"triggered": "TRIGGERED", "waiting": "WAITING", "expired": "EXPIRED", "no-data": "NO DATA"}
PHASE_LABEL = {"pre-open": "NOT OPEN YET", "forming": "RANGE FORMING", "closed": "MARKET CLOSED"}
SHORT = {"pre-open": "not open", "forming": "forming", "closed": "closed"}


def setup_name(res):
    if res["setup"] == "orb":
        return "opening-range breakout (%d-minute range)" % res["or_minutes"]
    side = "below" if res.get("index_side") == "down" else "above"   # a bought put waits for a close below it
    return "close %s Rs %.2f" % (side, res["level"]) if res["level"] is not None else "close %s a level" % side


def describe(res):
    """Plain lines for a person."""
    when = datetime.datetime.strptime(res["now"], "%Y-%m-%d %H:%M:%S")
    bars = "5-minute" if res["interval"] == "5m" else "1-minute"
    head = "%s  %s %s IST  %s on %s bars (Yahoo, free, can lag)" % (
        res["symbol"], when.strftime("%a %Y-%m-%d"), when.strftime("%H:%M"), setup_name(res), bars)
    if res.get("data_time") and res["data_time"] <= res["now"] and res["data_time"][:10] == res["date"]:
        head += ", data to %s" % res["data_time"][11:]
    out = [head]
    orr = res.get("opening_range")
    if orr:
        start = datetime.datetime.strptime(res["opens"], "%H:%M")
        end = start + datetime.timedelta(minutes=orr["minutes"])
        out.append("Opening range %s-%s: high Rs %.2f, low Rs %.2f (%d of %d bars%s)." % (
            short(start), short(end), orr["high"], orr["low"], orr["bars"], orr["of"],
            "" if orr["complete"] else ", still forming"))
    label = PHASE_LABEL.get(res["phase"]) or LABEL.get(res["status"], res["status"].upper())
    t = res.get("trigger")
    if t:
        down = res.get("index_side") == "down"
        out.append("TRIGGERED %s at Rs %.2f: the %s bar (bar %d) closed %s %s Rs %.2f (%+.2f%%%s)." % (
            t["time"], t["price"], short(datetime.datetime.strptime(t["bar_start"], "%H:%M")), t["bar_index"],
            "below" if down else "above",
            ("the range low" if down else "the range high") if res["setup"] == "orb" else "the level", res["level"],
            t["pct_over"],
            (", %.2fx the range's volume" % t["volume_x"]) if res.get("min_volume_x") and t.get("volume_x") else ""))
        step = INTERVALS[res["interval"]] // 60
        if res.get("minutes_since", 0) > step and res.get("last"):
            out.append("That was %d minutes ago; the last close is Rs %.2f (%s bar). A late entry is a different "
                       "trade: the price, the stop distance and the odds have moved." % (
                           res["minutes_since"], res["last"]["close"], res["last"]["bar_start"]))
        if res.get("data_note"):
            out.append(res["data_note"] + ".")
        if res.get("minutes_since", 0) > step:
            out.append("Nothing was placed.")
        else:
            out.append("This is the moment to get in, if you take the trade. Nothing was placed.")
        out.append("Next: set the stop and target BEFORE you buy: /bharat-exit "
                   "(exit.py plan --from trader/entry.json).")
    else:
        out.append("%s  %s." % (label, res["reason"]))
        f = res.get("forming")
        if res.get("lag_note") and res["status"] == "waiting":
            out.append(res["lag_note"] + ".")
        elif f and res["status"] == "waiting" and res["phase"] in ("open", "forming"):
            out.append("The %s bar is still forming (Rs %.2f so far, closes %s); it does not count until it closes." % (
                f["bar_start"].lstrip("0"), f["price"], f["closes"]))
        if res["status"] == "expired":
            out.append("No entry on this setup today. Nothing was placed.")
        if res.get("next_open"):
            out.append("Next open: %s IST." % res["next_open"])
    nb = res.get("near_band")
    if nb:
        out.append("WARNING: Rs %.2f is within 1%% of its %g%% band limit Rs %.2f (previous close Rs %.2f). "
                   "A stock frozen at its band can leave you unable to exit." % (
                       nb["price"], nb["band"], nb["upper"], nb["prev_close"]))
    if not res.get("calendar_known"):
        out.append("NSE holiday list covers %s only: check nseindia.com for this date." % " and ".join(
            map(str, sorted(india.HOLIDAY_YEARS))))
    return out


def bar_line(b, res):
    """One watch line for a bar that just closed."""
    start = short(b["et"])
    orr = res.get("opening_range") or {}
    lvl = res.get("level")
    t = res.get("trigger")
    n_or = orr.get("of") or 0
    down = res.get("index_side") == "down"   # a bought put: the trigger is a close BELOW the range low
    side_word = "below" if down else "above"
    if res["setup"] == "orb" and b["slot"] < n_or:
        seen = [x for x in res["_bars"] if x["slot"] <= b["slot"]]
        note = "range so far Rs %.2f to Rs %.2f" % (min(x["l"] for x in seen), max(x["h"] for x in seen))
        if b["slot"] == n_or - 1:
            note = "opening range set: high Rs %.2f, low Rs %.2f" % (orr["high"], orr["low"])
    elif t and t["bar_index"] == b["slot"]:
        note = "TRIGGERED: closed %s Rs %.2f (%+.2f%%). Get in now if you take it: %s, Rs %.2f" % (
            side_word, lvl, t["pct_over"], t["time"], t["price"])
    elif lvl is None:
        note = ""
    elif (b["c"] < lvl) if down else (b["c"] > lvl):
        note = "%s Rs %.2f" % (side_word, lvl)
        if res["setup"] == "level" or b["slot"] >= n_or:
            avg = orr.get("avg_volume")
            if res.get("min_volume_x") and avg:
                note += ", but volume %.2fx is under %.2fx" % (b["v"] / float(avg), res["min_volume_x"])
            elif b["end"] < datetime.datetime.combine(b["et"].date(), parse_hhmm(res["no_entries_before"], "x")):
                note += ", but no entries before %s" % res["no_entries_before"]
    else:
        note = "Rs %.2f %s Rs %.2f" % (abs(lvl - b["c"]), "over" if down else "under", lvl)
    return "%5s bar  closed Rs %.2f  %s" % (start, b["c"], note)


# ---------------------------------------------------------------- watch

class SimClock(object):
    """A clock that moves only when it sleeps: replays a day without waiting."""

    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += datetime.timedelta(seconds=max(1, int(round(seconds))))


def watch_loop(provider, clock, sleep, emit, opts, every=30, until=None, max_errors=5, on_trigger=None):
    """Poll bar by bar until a trigger (0), expiry, the close or --until (1).
    provider(now_utc) -> chart, clock() -> naive UTC, sleep(seconds), emit(line).
    It sleeps in steps of at most `every` seconds and re-reads the clock each time,
    so a computer that dozed off catches up as soon as it wakes."""
    step = INTERVALS[opts.get("interval", "5m")]
    printed, errors, said_preopen, first = -1, 0, False, True
    next_fetch = None
    while True:
        now = clock()
        if next_fetch is not None and now < next_fetch:
            sleep(max(1, min(every, (next_fetch - now).total_seconds())))
            continue
        local = to_local(now)
        day = local.date()
        s = session(day)
        if not s:
            emit("%s: market closed today (%s). Nothing to watch." % (day.isoformat(), trading_day(day)[1]))
            return 1, None
        open_dt = datetime.datetime.combine(day, s[0])
        close_dt = datetime.datetime.combine(day, s[1])
        if local < open_dt + datetime.timedelta(seconds=step):
            if not said_preopen:
                emit("%s IST  %s: watching from the first bar's close at %s." % (
                    local.strftime("%H:%M"), "not open yet" if local < open_dt else "open, no bar closed yet",
                    short(open_dt + datetime.timedelta(seconds=step))))
                said_preopen = True
            next_fetch = local_to_utc(open_dt) + datetime.timedelta(seconds=step + GRACE)
            continue
        try:
            chart = provider(now)
            errors = 0
        except EntryError as e:
            errors += 1
            emit("%s IST  could not read bars: %s" % (local.strftime("%H:%M:%S"), e))
            if errors >= max_errors:
                emit("Stopping after %d failed reads in a row." % errors)
                return 1, None
            next_fetch = now + datetime.timedelta(seconds=every)
            continue
        res = evaluate(chart, now, **opts)
        bars = res["_bars"]
        lines = describe(res)
        fresh = first
        if first:
            emit(lines[0])
            if len(lines) > 1 and lines[1].startswith("Opening range"):
                emit(lines[1])
            if bars and not res.get("trigger"):
                emit(bar_line(bars[-1], res))
            printed = bars[-1]["slot"] if bars else -1
            first = False
        else:
            for b in bars:
                if b["slot"] > printed:
                    emit(bar_line(b, res))
                    printed = b["slot"]
        if res["status"] == "triggered":
            for line in lines[1:]:
                if line.startswith("Opening range") or (line.startswith("TRIGGERED") and not fresh):
                    continue
                emit(line)
            if on_trigger:
                on_trigger(res, now)
            return 0, res
        if res["status"] == "expired" or local >= close_dt:
            emit("EXPIRED  %s." % (res["reason"] or "the market closed without a trigger"))
            return 1, res
        if until and local >= until:
            emit("Stopped at --until %s without a trigger. %s." % (short(until), res["reason"]))
            return 1, res
        # sleep until just after the next bar closes; poll every `every` seconds if Yahoo is behind
        cur_slot = int((local - open_dt).total_seconds()) // step
        expected_last = cur_slot - 1
        behind = (not bars) or bars[-1]["slot"] < expected_last
        if behind:
            wait = every
        else:
            nxt = open_dt + datetime.timedelta(seconds=(cur_slot + 1) * step + GRACE)
            wait = (nxt - local).total_seconds()
        cutoff = datetime.datetime.combine(day, parse_hhmm(res.get("no_entries_after") or "14:45", "cutoff"))
        for stop_at in (until, cutoff, close_dt):
            if stop_at and stop_at > local:
                wait = min(wait, (stop_at - local).total_seconds())
        next_fetch = now + datetime.timedelta(seconds=max(1, wait))


# ---------------------------------------------------------------- commands

def _check_instrument(a, sym):
    if a.instrument == "fut" and sym not in FUT_INDEX:
        raise EntryError("--instrument fut is for NIFTY or BANKNIFTY index futures (P2), not %s" % sym)
    if a.instrument == "opt":
        if sym not in FUT_INDEX:
            raise EntryError("--instrument opt is for NIFTY or BANKNIFTY index options (P3), not %s" % sym)
        if a.right not in ("CE", "PE"):
            raise EntryError("--instrument opt needs --right CE or PE (a call or a put; this pack only buys them)")
        if a.premium is not None and a.premium <= 0:
            raise EntryError("--premium must be a price above 0")
        if a.premium_index is not None and a.premium is None:
            raise EntryError("--premium-index is the index when you read --premium: give both")
        if a.steps_otm < 0:
            raise EntryError("--steps-otm counts strikes out of the money: 0 or more")
        if a.expiry:
            try:
                datetime.date.fromisoformat(a.expiry)
            except ValueError:
                raise EntryError("--expiry wants a date like 2026-10-27")
    elif a.right or a.premium is not None or a.expiry or a.steps_otm or a.premium_index is not None:
        raise EntryError("--right, --premium, --expiry and --steps-otm are for options: add --instrument opt")
    if a.fut_price is not None and a.instrument != "fut":
        raise EntryError("--fut-price is the futures price: add --instrument fut")
    if a.fut_price is not None and a.fut_price <= 0:
        raise EntryError("--fut-price must be a price above 0")


def _opts(a, cfg):
    if a.side in ("sell", "short"):
        raise EntryError("P1 plans longs only: no short entries. Watch for a buy (--side buy) instead.")
    if a.setup == "orb" and a.level is not None:
        raise EntryError("--level is for --setup level (an orb's level is its range high)")
    if a.min_volume_x is not None and a.min_volume_x <= 0:
        raise EntryError("--min-volume-x must be above 0")
    if a.level is not None and a.level <= 0:
        raise EntryError("--level must be a price above 0")
    if a.band is not None and a.band <= 0:
        raise EntryError("--band is the stock's NSE price band in percent (2, 5, 10 or 20)")
    return {"setup": a.setup, "or_minutes": a.or_minutes, "interval": a.interval, "level": a.level,
            "min_volume_x": a.min_volume_x, "cfg": cfg, "band": a.band,
            "below": a.instrument == "opt" and a.right == "PE"}


def _load(a, sym, now):
    """(chart, now). An offline file without --now is read as of its own time."""
    real_today = to_local(utc_now()).date()
    if a.offline and now is None:
        ch = get_chart(sym, a.interval, offline=a.offline)
        return ch, (utc_of(ch["data_time"]) if ch.get("data_time") else utc_now())
    now = now or utc_now()
    return get_chart(sym, a.interval, to_local(now).date(), a.offline, real_today), now


def cmd_check(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    sym = a.symbol.upper()
    _check_instrument(a, sym)
    chart, now = _load(a, sym, parse_now(a.now) if a.now else None)
    res = evaluate(chart, now, **opts)
    attach_future(res, a)
    attach_option(res, a, cfg)
    if a.write:
        write_json(a.write, entry_doc(res, now))
    if a.json:
        print(json.dumps(public(res), indent=2))
    else:
        for line in describe(res) + future_lines(res.get("future")) + option_lines(res.get("option")):
            print(line)
        if a.write:
            print("Wrote %s (status %s)." % (a.write, res["status"]))
    return 0 if res["status"] == "triggered" else 1


def cmd_watch(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    sym = a.symbol.upper()
    _check_instrument(a, sym)
    if a.every <= 0:
        raise EntryError("--every must be above 0 seconds")
    start = parse_now(a.now) if a.now else None
    if a.offline or start is not None:
        if start is None:
            start = _load(a, sym, None)[1]
        clock = SimClock(start)
        now_fn, sleep_fn = clock.now, clock.sleep
    else:
        now_fn, sleep_fn = utc_now, _sleep
    real_today = to_local(utc_now()).date()

    def provider(now):
        return get_chart(sym, a.interval, to_local(now).date(), a.offline, real_today)

    until = None
    if a.until:
        t = parse_hhmm(a.until, "--until")
        until = datetime.datetime.combine(to_local(now_fn()).date(), t)

    rows = None
    if a.instrument in ("fut", "opt"):  # fetched now: a download failure at the trigger would lose the signal
        rows = load_bhavcopy(a.bhavcopy, to_local(now_fn()).date())

    def on_trigger(res, now):
        attach_future(res, a, rows)
        attach_option(res, a, cfg, rows)
        for line in future_lines(res.get("future")) + option_lines(res.get("option")):
            print(line, flush=True)
        write_json(a.out, entry_doc(res, now))
        print("Wrote %s." % a.out, flush=True)

    def emit(line):
        print(line, flush=True)

    code, _ = watch_loop(provider, now_fn, sleep_fn, emit, opts, every=a.every, until=until,
                         on_trigger=on_trigger)
    return code


def cmd_watchlist(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    if a.instrument != "eq":
        raise EntryError("watchlist is for stocks; check NIFTY or BANKNIFTY futures with: check NIFTY --instrument fut")
    if not os.path.exists(a.watchlist):
        raise EntryError("no watchlist at %s: run /bharat-scanner first (it writes the hits there)" % a.watchlist)
    try:
        with open(a.watchlist, encoding="utf-8") as fh:
            doc = json.load(fh)
    except ValueError as e:
        raise EntryError("%s is not valid JSON: %s" % (a.watchlist, e))
    hits = [h for h in (doc.get("hits") or []) if isinstance(h, dict) and h.get("symbol")]
    if not hits:
        raise EntryError("no hits in %s: the scanner found nothing to watch" % a.watchlist)
    if a.offline and not os.path.isdir(a.offline):
        raise EntryError("watchlist --offline wants a folder of <TICKER>.json files")
    now = parse_now(a.now) if a.now else None
    rows = []
    for i, h in enumerate(hits):
        sym = str(h["symbol"]).upper()
        o = dict(opts)
        if a.setup == "level" and o["level"] is None:
            o["level"] = h.get("level")
        if o["band"] is None and h.get("band"):
            o["band"] = float(h["band"])
        try:
            if a.setup == "level" and o["level"] is None:
                raise EntryError("needs a level: --level PRICE or a \"level\" in the watchlist")
            if i and not a.offline:
                _sleep(PAUSE)
            chart, when = _load(a, sym, now)
            rows.append((sym, evaluate(chart, when, **o), None))
        except EntryError as e:
            rows.append((sym, None, str(e)))
    if a.json:
        print(json.dumps([{"symbol": s, "result": public(r) if r else None, "error": e} for s, r, e in rows], indent=2))
    else:
        print("%-10s  %-9s  %-8s  %-10s  %-11s  %-11s  %s" % ("SYMBOL", "STATUS", "TIME", "PRICE", "LEVEL", "LAST", "VS LEVEL"))
        for s, r, e in rows:
            if e:
                print("%-10s  error: %s" % (s, e))
                continue
            t = r.get("trigger") or {}
            last = r.get("last") or {}
            status = SHORT.get(r["phase"], r["status"]) if r["status"] != "triggered" else "triggered"
            print("%-10s  %-9s  %-8s  %-10s  %-11s  %-11s  %s" % (
                s, status[:9], t.get("time") or "-", ("Rs %.2f" % t["price"]) if t else "-",
                ("Rs %.2f" % r["level"]) if r.get("level") is not None else "-",
                ("Rs %.2f" % last["close"]) if last else "-",
                ("%+.2f%%" % last["vs_level_pct"]) if last.get("vs_level_pct") is not None else "-"))
        n = sum(1 for _, r, _ in rows if r and r["status"] == "triggered")
        print("%d of %d triggered. Pick one, then: entry.py check <SYMBOL> --write, and /bharat-exit." % (n, len(rows)))
        print("Free Yahoo bars, unofficial and can lag. Nothing was placed.")
    return 0 if any(r and r["status"] == "triggered" for _, r, _ in rows) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Entry Timer (bharat-entry): watches a setup bar by bar.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--setup", choices=["orb", "level"], default="orb")
        p.add_argument("--or-minutes", type=int, default=15, help="opening range length (default 15)")
        p.add_argument("--interval", choices=["5m", "1m"], default="5m")
        p.add_argument("--level", type=float, help="the price to close above (--setup level)")
        p.add_argument("--min-volume-x", type=float, help="trigger bar volume vs the range bars' average")
        p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="P1: buy only")
        p.add_argument("--band", type=float, help="the stock's NSE price band in percent (warns near its limit)")
        p.add_argument("--now", help='pretend it is this IST time, e.g. "2026-10-19 10:15"')
        p.add_argument("--offline", metavar="PATH", help="saved Yahoo chart JSON (.json/.json.gz) or a folder")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--instrument", choices=["eq", "fut", "opt"], default="eq",
                       help="fut: NIFTY/BANKNIFTY index futures; the signal is read on the index")
        p.add_argument("--bhavcopy", metavar="FILE", help="F&O bhavcopy (default: download the last session's)")
        p.add_argument("--fut-price", type=float, help="the futures price you see on Kite (beats the basis estimate)")
        p.add_argument("--right", type=str.upper, choices=["CE", "PE"], help="--instrument opt: buy a call or a put")
        p.add_argument("--steps-otm", type=int, default=0, help="strikes out of the money from ATM (default 0)")
        p.add_argument("--expiry", help="option expiry YYYY-MM-DD (default: the nearest that is not today)")
        p.add_argument("--premium", type=float, help="the option premium you see on Kite (sets the implied volatility)")
        p.add_argument("--premium-index", type=float, help="the index when you read --premium (default: the trigger's)")

    p = sub.add_parser("check", help="the setup's status right now")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--write", nargs="?", const="trader/entry.json", metavar="PATH",
                   help="save the result (default trader/entry.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("watch", help="poll bar by bar until it triggers or expires")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--every", type=float, default=30, help="seconds between polls while Yahoo is behind")
    p.add_argument("--until", help="stop at this IST time (HH:MM)")
    p.add_argument("--out", default="trader/entry.json", help="where a trigger is written")
    p = sub.add_parser("watchlist", help="check every hit in trader/watchlist.json")
    common(p)
    p.add_argument("--watchlist", default="trader/watchlist.json")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"check": cmd_check, "watch": cmd_watch, "watchlist": cmd_watchlist}[a.cmd](a)
    except EntryError as e:
        print("entry.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("entry.py: stopped.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
