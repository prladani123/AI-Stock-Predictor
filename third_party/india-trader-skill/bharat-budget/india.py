"""India rules shared by every bharat-* skill: the NSE clock and calendar,
symbols, Zerodha trading costs, tick rounding, price bands, a polite
NSE fetcher, and writing a file whole or not at all.

This file is copied into every skill folder so each skill installs on its
own. Edit india/india.py at the repo root, then run tools/sync_india.py;
tests/test_repo.py fails if a copy differs.

Standard library only, Python 3.8+.
"""

import csv
import gzip
import http.cookiejar
import io
import json
import math
import os
import re
import stat
import tempfile
import zipfile
import time as _time
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

# ------------------------------------------------------------------ files

def write_atomic(path, fill):
    """Write a file whole or not at all: fill(fh) writes a temp file of its own next to it, which is then
    swapped in. A write cut short (a crash, Ctrl-C, a full disk) leaves the old file and no temp file, and
    two runs writing the same file at once never share one. The file keeps its mode."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:   # a new file: the usual mode for this umask, not the temp file's private 0600
        mask = os.umask(0)
        os.umask(mask)
        mode = 0o666 & ~mask
    fd, tmp = tempfile.mkstemp(dir=d or ".", prefix="." + os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fill(fh)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_state(path, lost="what you have seen may show again"):
    """(state, note) for a tool's {"seen": {fingerprint: time}} state file: ({}, None) when there is none, and
    a fresh {} with a note when it cannot be read or is not that shape, instead of stopping every run."""
    if not path or not os.path.isfile(path):
        return {}, None
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
        seen = state.get("seen", {}) if isinstance(state, dict) else None
        if not isinstance(seen, dict) or not all(isinstance(v, str) for v in seen.values()):
            raise ValueError('not the {"seen": {...}} this tool writes')
    except ValueError as e:
        return {}, "the state file %s could not be read (%s): starting fresh, so %s" % (path, e, lost)
    return state, None


def write_json(path, doc, indent=2):
    """write_atomic for a JSON document, with a newline at the end."""
    def fill(fh):
        json.dump(doc, fh, indent=indent)
        fh.write("\n")
    write_atomic(path, fill)


# ------------------------------------------------------------------ clock

IST = timezone(timedelta(hours=5, minutes=30))  # no DST in India
OPEN = time(9, 15)
CLOSE = time(15, 30)
PRE_OPEN = time(9, 0)
SQUARE_OFF = time(15, 20)  # Zerodha's MIS auto square-off
FLAT_BY = time(15, 15)  # our default: out before the broker does it for you


def to_ist(utc):
    """An aware datetime (any zone) to IST."""
    return utc.astimezone(IST)


def ist_to_utc(local):
    """An IST datetime to UTC. A naive datetime is read as IST."""
    if local.tzinfo is None:
        local = local.replace(tzinfo=IST)
    return local.astimezone(timezone.utc)


# --------------------------------------------------------------- calendar

# NSE cash-market trading holidays, from nseindia.com/api/holiday-master
# (checked 2026-11-04). Add each year when NSE publishes it (usually December).
HOLIDAYS = {
    date(2026, 1, 15): "Municipal Corporation Election - Maharashtra",
    date(2026, 1, 26): "Republic Day",
    date(2026, 2, 15): "Mahashivratri",
    date(2026, 3, 3): "Holi",
    date(2026, 3, 21): "Id-Ul-Fitr (Ramadan Eid)",
    date(2026, 3, 26): "Shri Ram Navami",
    date(2026, 3, 31): "Shri Mahavir Jayanti",
    date(2026, 4, 3): "Good Friday",
    date(2026, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti",
    date(2026, 5, 1): "Maharashtra Day",
    date(2026, 5, 28): "Bakri Id",
    date(2026, 6, 26): "Muharram",
    date(2026, 8, 15): "Independence Day",
    date(2026, 9, 14): "Ganesh Chaturthi",
    date(2026, 10, 2): "Mahatma Gandhi Jayanti",
    date(2026, 10, 20): "Dussehra",
    date(2026, 11, 8): "Diwali Laxmi Pujan",
    date(2026, 11, 10): "Diwali-Balipratipada",
    date(2026, 11, 24): "Prakash Gurpurb Sri Guru Nanak Dev",
    date(2026, 12, 25): "Christmas",
}
HOLIDAY_YEARS = {2026}

# Muhurat trading: a short evening session on Diwali, announced by NSE
# circular each October (2026: CMTR76941). Normal-market times only.
MUHURAT = {
    date(2026, 11, 8): (time(14, 20), time(15, 30)),
}


def trading_day(d):
    """(is_trading_day, reason). reason is "" on a normal day."""
    if d in MUHURAT:
        return True, "Muhurat session"
    if d.weekday() >= 5:
        return False, "weekend"
    if d.year not in HOLIDAY_YEARS:
        # ponytail: unknown years read as closed, so nothing trades on a guess
        return False, "holiday list unknown for %d" % d.year
    if d in HOLIDAYS:
        return False, "NSE holiday: " + HOLIDAYS[d]
    return True, ""


def session(d):
    """(open, close) as IST datetimes, or None when NSE is closed."""
    ok, _ = trading_day(d)
    if not ok:
        return None
    o, c = MUHURAT.get(d, (OPEN, CLOSE))
    return datetime.combine(d, o, IST), datetime.combine(d, c, IST)


def prev_trading_day(d):
    """The last trading day strictly before day d, or None within 30 days."""
    for k in range(1, 31):
        p = d - timedelta(days=k)
        if trading_day(p)[0]:
            return p
    return None


def next_open(d):
    """The first session open strictly after day d (IST datetime), or None
    when no known day follows within 30 days."""
    for k in range(1, 31):
        s = session(d + timedelta(days=k))
        if s:
            return s[0]
    return None


# ---------------------------------------------------------------- symbols

INDEX_YAHOO = {"NIFTY": "^NSEI", "NIFTY50": "^NSEI", "BANKNIFTY": "^NSEBANK", "NIFTYBANK": "^NSEBANK"}


def yahoo_symbol(sym):
    """NSE symbol to Yahoo: RELIANCE -> RELIANCE.NS, NIFTY -> ^NSEI.
    Callers URL-encode the result (M&M.NS -> M%26M.NS)."""
    s = sym.strip().upper()
    if s.startswith("^") or s.endswith(".NS") or s.endswith(".BO"):
        return s
    return INDEX_YAHOO.get(s.replace(" ", ""), s + ".NS")


# ------------------------------------------------------------------ costs

# Zerodha, NSE, from zerodha.com/charges and the brokerage calculator's own
# formulas. Fractions, not percent. "ipft" is NSE's investor protection fund
# charge, which Zerodha adds to the exchange charge.
RATES = {
    "checked": "2026-11-04",
    "eq_intraday": {"brokerage_pct": 0.0003, "brokerage_cap": 20.0, "stt_sell_pct": 0.00025,
                    "exchange_pct": 0.0000297, "ipft_pct": 0.000001, "sebi_pct": 0.000001,
                    "stamp_buy_pct": 0.00003, "gst_pct": 0.18},
    "fut": {"brokerage_pct": 0.0003, "brokerage_cap": 20.0, "stt_sell_pct": 0.0005,
            "exchange_pct": 0.0000173, "ipft_pct": 0.000001, "sebi_pct": 0.000001,
            "stamp_buy_pct": 0.00002, "gst_pct": 0.18},
    "opt": {"brokerage_pct": None, "brokerage_cap": 20.0, "stt_sell_pct": 0.0015,
            "exchange_pct": 0.0003503, "ipft_pct": 0.000005, "sebi_pct": 0.000001,
            "stamp_buy_pct": 0.00003, "gst_pct": 0.18},
}
COST_KEYS = ("brokerage", "stt", "exchange", "sebi", "stamp", "gst", "total")


def _r2(x):
    """Round half up to paise, on the float's exact value (as JS toFixed(2))."""
    return float(Decimal(x).quantize(Decimal("0.01"), ROUND_HALF_UP))


def _r0(x):
    """Round half up to the rupee (as JS Math.round)."""
    return float(Decimal(_r2(x)).quantize(Decimal("1"), ROUND_HALF_UP))


def _rates(segment, rates):
    table = rates or RATES
    if segment not in table or segment == "checked":
        raise ValueError("unknown segment %r: use eq_intraday, fut or opt" % segment)
    return table[segment]


def _brokerage(r, value):
    if not value:
        return 0.0
    if r["brokerage_pct"] is None:
        return r["brokerage_cap"]
    raw = value * r["brokerage_pct"]
    return r["brokerage_cap"] if raw > r["brokerage_cap"] else _r2(raw)


def _costs(r, buy_value, sell_value):
    turnover = _r2(buy_value + sell_value)
    brokerage = _r2(_brokerage(r, buy_value) + _brokerage(r, sell_value))
    stt = _r0(sell_value * r["stt_sell_pct"])
    sebi = _r2(turnover * r["sebi_pct"])
    exchange = _r2(_r2(turnover * r["exchange_pct"]) + _r2(turnover * r["ipft_pct"]))
    gst = _r2(r["gst_pct"] * (brokerage + exchange + sebi))
    stamp = _r0(buy_value * r["stamp_buy_pct"])
    total = _r2(brokerage + stt + exchange + gst + sebi + stamp)
    return {"brokerage": brokerage, "stt": stt, "exchange": exchange, "sebi": sebi,
            "stamp": stamp, "gst": gst, "total": total}


def leg_costs(segment, side, qty, price, rates=None):
    """Charges on one executed order, in rupees. side is "buy" or "sell"."""
    if side not in ("buy", "sell"):
        raise ValueError("side must be buy or sell, not %r" % side)
    value = qty * price
    r = _rates(segment, rates)
    return _costs(r, value if side == "buy" else 0.0, value if side == "sell" else 0.0)


def round_trip(segment, qty, buy, sell, rates=None):
    """Charges on a buy and a sell of qty, in rupees, rounded the way
    Zerodha's calculator rounds a round trip (on the combined turnover)."""
    return _costs(_rates(segment, rates), qty * buy, qty * sell)


def inr(x, paise=None):
    """Rupees the Indian way: Rs 2,00,000 and Rs 1,227.80. paise=None shows
    them only when there are any."""
    x = float(x)
    sign = "-" if x < 0 else ""
    x = abs(x)
    if paise is None:
        paise = abs(x - round(x)) >= 0.005
    whole, frac = ("%.2f" % x).split(".") if paise else (str(int(round(x))), "")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups) + "," + tail
    return "%sRs %s%s" % (sign, whole, ("." + frac) if paise else "")


# ----------------------------------------------------------------- prices

def tick_round(price, tick=0.05, mode="nearest"):
    """Round a price to the exchange tick. mode: nearest, down or up."""
    how = {"nearest": ROUND_HALF_UP, "down": ROUND_FLOOR, "up": ROUND_CEILING}[mode]
    t = Decimal(str(tick))
    q = (Decimal(str(price)) / t).quantize(Decimal("1"), how)
    return float(q * t)


def band_ok(price, prev_close, band_pct):
    """True when price is inside the daily price band. band_pct None = no band."""
    if band_pct is None:
        return True
    lo = tick_round(prev_close * (1 - band_pct / 100.0), mode="up")
    hi = tick_round(prev_close * (1 + band_pct / 100.0), mode="down")
    return lo <= price <= hi


# ------------------------------------------------------------------ F&O contracts

MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
BHAV_KIND = {"IDF": "fut", "STF": "fut", "IDO": "opt", "STO": "opt"}
# NSE's freeze quantity: the most units one F&O order may carry (NSE/FAOP/77189,
# from 2 Nov 2026). NSE revises it each quarter.
FREEZE_QTY = {"NIFTY": 1800, "BANKNIFTY": 900}


def _num(x):
    return float(x) if x not in ("", None) else None


def parse_bhavcopy(data):
    """NSE F&O UDiFF bhavcopy (the .zip or the CSV inside it) -> futures and options rows."""
    if data[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(data))
        data = z.read(z.namelist()[0])
    out = []
    for r in csv.DictReader(io.StringIO(data.decode("utf-8-sig"))):
        kind = BHAV_KIND.get(r.get("FinInstrmTp", ""))
        if not kind:
            continue
        out.append({"kind": kind, "symbol": r["TckrSymb"], "contract": r["FinInstrmNm"],
                    "expiry": date.fromisoformat(r["XpryDt"]), "strike": _num(r["StrkPric"]),
                    "right": r["OptnTp"] or None, "close": float(r["ClsPric"]), "settle": float(r["SttlmPric"]),
                    "underlying": _num(r["UndrlygPric"]), "oi": int(float(r["OpnIntrst"] or 0)),
                    "lot": int(r["NewBrdLotQty"]), "date": date.fromisoformat(r["TradDt"]),
                    "volume": int(float(r.get("TtlTradgVol") or 0))})
    return out


def futures(rows, symbol):
    """That symbol's futures, nearest expiry first."""
    return sorted((r for r in rows if r["kind"] == "fut" and r["symbol"] == symbol), key=lambda r: r["expiry"])


def _trading_days_between(a, b):
    """Trading days after a, up to and including b. A weekday in a year with no
    holiday list yet counts as a trading day (a holiday can only lower the count)."""
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        n += trading_day(d)[0] or (d.year not in HOLIDAY_YEARS and d.weekday() < 5)
    return n


def front_future(rows, symbol, today, roll_days=0):
    """The nearest future that does not expire today, nor within roll_days trading days."""
    for f in futures(rows, symbol):
        if f["expiry"] > today and _trading_days_between(today, f["expiry"]) > roll_days:
            return f
    return None


def basis_ratio(fut_row):
    return fut_row["close"] / fut_row["underlying"]


def is_expiry_day(rows, symbol, d):
    return any(f["expiry"] == d for f in futures(rows, symbol))


# ---------------------------------------------------------------- options

def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(right, spot, strike, years, vol, rate=0.06):
    """Black-Scholes premium, no dividend. right CE or PE; years 0 = intrinsic value."""
    if years <= 0 or vol <= 0:
        return max(spot - strike, 0.0) if right == "CE" else max(strike - spot, 0.0)
    sd = vol * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + vol * vol / 2.0) * years) / sd
    d2 = d1 - sd
    disc = strike * math.exp(-rate * years)
    if right == "CE":
        return spot * norm_cdf(d1) - disc * norm_cdf(d2)
    return disc * norm_cdf(-d2) - spot * norm_cdf(-d1)


def implied_vol(right, premium, spot, strike, years, rate=0.06):
    """The volatility (0.01 to 3.00) that prices premium, by bisection to 1e-6 on the price."""
    disc = strike * math.exp(-rate * max(years, 0))
    intrinsic = max(spot - disc, 0.0) if right == "CE" else max(disc - spot, 0.0)
    top = spot if right == "CE" else disc
    if years <= 0:
        raise ValueError("no implied volatility: the option has expired")
    if premium < intrinsic:
        raise ValueError("no implied volatility: premium %.2f is under the intrinsic value %.2f" % (premium, intrinsic))
    if premium >= top:
        raise ValueError("no implied volatility: premium %.2f is not under the %s %.2f"
                         % (premium, "index" if right == "CE" else "discounted strike", top))
    lo, hi = 0.01, 3.00
    if not bs_price(right, spot, strike, years, lo, rate) <= premium <= bs_price(right, spot, strike, years, hi, rate):
        raise ValueError("no implied volatility: premium %.2f is outside what 1%% to 300%% volatility gives" % premium)
    for _ in range(200):
        mid = (lo + hi) / 2.0
        p = bs_price(right, spot, strike, years, mid, rate)
        if abs(p - premium) < 1e-6:
            return mid
        lo, hi = (mid, hi) if p < premium else (lo, mid)
    return (lo + hi) / 2.0


def option_price(right, index, ratio, strike, years, vol, rate=0.06):
    """Black-76: the option on the forward index x ratio (bs_price with spot = F e^(-rT))."""
    return bs_price(right, index * ratio * math.exp(-rate * max(years, 0)), strike, years, vol, rate)


def option_iv(right, premium, index, ratio, strike, years, rate=0.06):
    return implied_vol(right, premium, index * ratio * math.exp(-rate * max(years, 0)), strike, years, rate)


def years_to_expiry(now_ist, expiry):
    """Calendar minutes to 15:30 IST on expiry / (365 x 1440); 0 after it."""
    end = datetime.combine(expiry, CLOSE, IST)
    return max((end - now_ist).total_seconds() / 60.0, 0.0) / (365 * 1440.0)


WEEK_CODE = "123456789OND"
OPT_NAME = re.compile(r"^([A-Z&\-]+?)(\d{2})(?:([1-9OND])(\d{2})|([A-Z]{3}))(\d+(?:\.\d+)?)(CE|PE)$")


def parse_option_symbol(text):
    """{symbol, expiry (None for a monthly name), month YYYY-MM, strike, right, weekly}, or None
    (None also for a name NSE could not issue, like a weekly dated 31 Nov)."""
    m = OPT_NAME.match(str(text or "").upper())
    if not m or (m.group(5) and m.group(5) not in MONTHS):
        return None
    yy = 2000 + int(m.group(2))
    if m.group(3):
        month = WEEK_CODE.index(m.group(3)) + 1
        try:
            exp = date(yy, month, int(m.group(4)))
        except ValueError:
            return None
    else:
        month, exp = MONTHS.index(m.group(5)) + 1, None
    return {"symbol": m.group(1), "expiry": exp, "month": "%d-%02d" % (yy, month), "strike": float(m.group(6)),
            "right": m.group(7), "weekly": bool(m.group(3))}


def _options(rows, symbol, expiry=None):
    return [r for r in rows if r["kind"] == "opt" and r["symbol"] == symbol and (expiry is None or r["expiry"] == expiry)]


def option_expiries(rows, symbol):
    return sorted({r["expiry"] for r in _options(rows, symbol)})


def strikes(rows, symbol, expiry):
    return sorted({r["strike"] for r in _options(rows, symbol, expiry)})


def atm_strike(strike_list, spot):
    return min(strike_list, key=lambda k: (abs(k - spot), k))


def pick_strike(strike_list, spot, right, steps_otm=0):
    """ATM, or steps_otm strikes out of the money (higher for a call, lower for a put)."""
    i = strike_list.index(atm_strike(strike_list, spot)) + (steps_otm if right == "CE" else -steps_otm)
    if not 0 <= i < len(strike_list):
        raise ValueError("no strike %d steps out of the money from %.2f" % (steps_otm, spot))
    return strike_list[i]


def option_row(rows, symbol, expiry, strike, right):
    for r in _options(rows, symbol, expiry):
        if r["strike"] == float(strike) and r["right"] == right:
            return r
    return None


def forward_ratio(rows, symbol, expiry, rate=0.06):
    """Forward / index for one expiry, from put-call parity at the strike nearest the index:
    F = K + e^(rT) (C - P), with the bhavcopy closes and T = calendar days to expiry / 365."""
    opts = _options(rows, symbol, expiry)
    if not opts or not opts[0]["underlying"]:
        raise ValueError("no %s options expiring %s in the bhavcopy" % (symbol, expiry))
    und, day = opts[0]["underlying"], opts[0]["date"]
    for k in sorted(strikes(rows, symbol, expiry), key=lambda k: abs(k - und)):
        c, p = option_row(rows, symbol, expiry, k, "CE"), option_row(rows, symbol, expiry, k, "PE")
        if c and p and c["close"] > 0 and p["close"] > 0:
            years = (expiry - day).days / 365.0
            return (k + math.exp(rate * years) * (c["close"] - p["close"])) / und
    raise ValueError("no %s call and put pair expiring %s to read the forward from" % (symbol, expiry))


# ------------------------------------------------------------------ fetch

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
TOOL_UA = "Mozilla/5.0 (compatible; india-trader-skill/0.1)"  # Yahoo 429s browser agents


class NSEError(Exception):
    def __init__(self, url, reason):
        Exception.__init__(self, "NSE fetch failed for %s: %s" % (url, reason))
        self.url = url
        self.reason = reason


_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))
_warmed = []
SLEEP = _time.sleep


def nse_get(url, timeout=20, opener=None, warm=True, want_json=True):
    """GET an NSE URL. Warms cookies on the home page once (best effort: it
    can 403 itself), sends a browser User-Agent, tries 3 times, 1 and then 2
    seconds apart, on 401/403/429/5xx or no answer. Raises NSEError when it
    gives up (at once, with no wait after the last try), or on an HTML page
    where JSON was wanted."""
    op = opener or _opener
    if warm and not _warmed:
        _warmed.append(True)
        try:
            op.open(urllib.request.Request("https://www.nseindia.com/", headers={"User-Agent": BROWSER_UA}),
                    timeout=timeout).read()
        except Exception:
            pass
    headers = {"User-Agent": BROWSER_UA, "Referer": "https://www.nseindia.com/",
               "Accept": "application/json" if want_json else "*/*",
               "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip"}
    last = "no answer"
    for attempt in range(3):
        if attempt:
            SLEEP(2 ** (attempt - 1))   # before a retry only: after the last try there is nothing to wait for
        try:
            with op.open(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
                body = r.read()
                if (getattr(r, "headers", None) or {}).get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
        except urllib.error.HTTPError as e:
            last = "HTTP %d" % e.code
            if e.code in (401, 403, 429) or e.code >= 500:
                continue
            raise NSEError(url, last)
        except (urllib.error.URLError, OSError) as e:
            last = str(getattr(e, "reason", e))
            continue
        if want_json and body.lstrip()[:1] == b"<":
            raise NSEError(url, "got an HTML page, not JSON (NSE blocked the request)")
        return body
    raise NSEError(url, last)
