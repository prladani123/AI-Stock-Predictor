#!/usr/bin/env python3
"""News monitor for bharat-news. Standard library only.

Watches NSE corporate announcements and headlines for the stocks you watch
or hold and prints only what is new since the last check, newest first, with
how far each stock has moved today and a flag on anything that touches your
symbols.

    python3 news.py                                  # watchlist + plan + portfolio symbols
    python3 news.py --tickers RELIANCE TCS INFY
    python3 news.py --tickers RELIANCE --all --hours 72  # everything in the last 3 days, seen or not
    python3 news.py --no-nse                         # Yahoo headlines only
    python3 news.py --no-moves                       # skip the price line per symbol

Your symbols, when --tickers is not given: every hit in trader/watchlist.json
(bharat-scanner), the symbol in trader/plan.json (bharat-exit) and the
positions in trader/portfolio.json.

Sources, one request each per symbol:
  - NSE corporate announcements (what the company filed with the exchange:
    results, board meetings, dividends, deals, credit ratings):
    https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=<SYMBOL>
    NSE blocks some scripted requests. When it does, the tool says so and
    carries on with the headlines. BSE is not a fallback: it blocks scripts.
  - Yahoo Finance headline RSS (coverage of NSE stocks is patchy: some
    symbols have no items):
    https://feeds.finance.yahoo.com/rss/2.0/headline?s=<SYMBOL>.NS&region=US&lang=en-US
  - The move: Yahoo Finance's chart endpoint (unofficial, can lag), the last
    price against yesterday's close:
    https://query2.finance.yahoo.com/v8/finance/chart/<SYMBOL>.NS?range=1d&interval=5m

Times print in IST. State: trader/state/news.json remembers what you have
seen (30 days), so an item shows once. Offline: --offline DIR reads
nse-<SYMBOL>.json, yahoo-<SYMBOL>.xml and chart-<SYMBOL>.json from DIR. The
tests use it.

Headlines and announcements are leads, not facts: open the filing before
acting, and treat nothing here as advice.
"""

import argparse
import datetime as _dt
import email.utils
import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import india

YAHOO = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=%s&region=US&lang=en-US"
CHART = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=5m"
NSE_ANN = "https://www.nseindia.com/api/corporate-announcements?index=equities&symbol=%s"
BROWSER_UA = india.TOOL_UA
KEEP_DAYS = 30
MOVE_PCT = 3.0  # a stock this far from yesterday's close is flagged as moving

# NSE announcement categories that usually move a stock
IMPORTANT = re.compile(r"financial result|outcome of board|acquisition|amalgamation|merger|scheme of arrangement"
                       r"|resignation|insolvency|fraud|default|credit rating|buyback|buy back|delisting"
                       r"|change in (?:director|management|auditor)|restructuring", re.I)
KEYWORDS = [
    ("results", r"\bresults?\b|\bearnings\b|\bEPS\b|\bguidance\b|\bQ[1-4]\b|\bquarter\b"),
    ("board meeting", r"\bboard meeting\b|\boutcome of board\b|meeting of the board"),
    ("dividend/split", r"\bdividend\b|\bstock split\b|\bsplit\b|\bsub-division\b|\bbonus\b|\brecord date\b"),
    ("deal", r"\bacqui(?:re|res|red|sition)\b|\bmerger\b|\bamalgamation\b|\btakeover\b|\bdeal\b|\bstake\b"),
    ("block deal", r"\bblock deal\b|\bbulk deal\b"),
    ("capital", r"\bbuy ?back\b|\brights issue\b|\bQIP\b|\bpreferential\b|\bIPO\b|\binitial public offer\b|\bfund raising\b"),
    ("rating", r"\bcredit rating\b|\bupgrade[sd]?\b|\bdowngrade[sd]?\b|price target|\boutperform\b|\bunderperform\b"),
    ("people", r"\bCEO\b|\bCFO\b|\bMD\b|\bresign|\bsteps down\b|\bappoint"),
    ("legal", r"\blawsuit\b|\bprobe\b|\binvestigation\b|\bshow cause\b|\bpenalty\b|\bcourt\b|\bNCLT\b"),
    ("risk", r"\bdefault\b|\binsolvency\b|\bfraud\b|\bdelist|\bhack|\bbreach\b|\bfire\b|\bstrike\b"),
    ("regulatory", r"\bSEBI\b|\bRBI\b|\bCCI\b|\bUSFDA\b|\bFDA\b|\bapproval\b"),
    ("macro", r"\brepo rate\b|\bmonetary policy\b|\bCPI\b|\binflation\b|\bGDP\b|\bbudget\b|\btariffs?\b|\bFII\b"),
]


class NewsError(Exception):
    pass


# ---------------------------------------------------------------- time

def to_local(utc):
    """Aware or naive UTC -> naive IST wall-clock time."""
    if utc.tzinfo is None:
        utc = utc.replace(tzinfo=_dt.timezone.utc)
    return india.to_ist(utc).replace(tzinfo=None)


def parse_now(text):
    """--now -> aware UTC. A time without an offset is IST, as in every bharat-* tool."""
    try:
        d = _dt.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        raise NewsError('--now wants a time like "2026-10-19 09:30" (IST) or ...Z (UTC), not %r' % text)
    return india.ist_to_utc(d)


# ---------------------------------------------------------------- inputs

def load_json(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def load_state(path):
    """(state, note). A state file that cannot be read starts fresh, with a note, instead of stopping every run."""
    return india.read_state(path, "items you have seen may show again")


save_state = india.write_json   # whole or not at all: a write cut short leaves the old file


def watch_from_files(portfolio="trader/portfolio.json", watchlist="trader/watchlist.json",
                     plan="trader/plan.json"):
    """{ticker: name or None}: the scanner's hits, the planned trade and your holdings."""
    out = {}
    wl = load_json(watchlist) or {}
    for h in wl.get("hits", []):
        sym = h.get("symbol") or h.get("ticker")
        if sym:
            out[sym.upper()] = h.get("name")
    pl = load_json(plan) or {}
    if pl.get("symbol"):
        out.setdefault(pl["symbol"].upper(), pl.get("name"))
    pf = load_json(portfolio) or {}
    for p in pf.get("positions", []):
        sym = p.get("ticker") or p.get("symbol")
        if sym:
            out[sym.upper()] = p.get("name") or out.get(sym.upper())
    return out


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    def __init__(self, offline=None, pause=0.15):
        self.offline = offline
        self.pause = pause
        self._last = 0.0

    def get(self, url, name, nse=False):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise NewsError("offline: no file %s" % path)
            with open(path, "rb") as fh:
                return fh.read()
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        if nse:
            try:
                return india.nse_get(url)
            except india.NSEError as exc:
                raise NewsError(str(exc))       # names the URL
        headers = {"User-Agent": BROWSER_UA, "Accept-Encoding": "gzip"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            raise NewsError("HTTP %s for %s" % (exc.code, url))
        except urllib.error.URLError as exc:
            raise NewsError("could not reach %s: %s" % (url, exc.reason))


def yahoo_symbol(ticker):
    return urllib.parse.quote(india.yahoo_symbol(ticker), safe="^.")


# ---------------------------------------------------------------- parsing

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _text(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def parse_time(value):
    """RFC 822 (RSS) or ISO 8601 (Atom) -> aware UTC datetime, or None."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


def parse_rss(data, ticker):
    """Yahoo headline RSS -> items."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("RSS for %s is not valid XML: %s" % (ticker, exc))
    out = []
    for it in root.iter("item"):
        title = _text(it, "title")
        link = _text(it, "link")
        guid = _text(it, "guid") or link
        out.append({"id": "rss:" + guid, "source": "Yahoo Finance", "kind": "headline",
                    "ticker": ticker, "title": title, "summary": _text(it, "description"),
                    "link": link, "time": parse_time(_text(it, "pubDate"))})
    return out


def parse_nse(data, ticker):
    """NSE corporate-announcements JSON -> items. an_dt is IST."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
    except ValueError as exc:
        raise NewsError("NSE announcements for %s are not JSON: %s" % (ticker, exc))
    if isinstance(doc, dict):
        doc = doc.get("data") or []
    out = []
    for r in doc if isinstance(doc, list) else []:
        cat = (r.get("desc") or "").strip()
        text = " ".join((r.get("attchmntText") or "").split())
        try:
            when = india.ist_to_utc(_dt.datetime.strptime(r.get("an_dt") or "", "%d-%b-%Y %H:%M:%S"))
        except ValueError:
            when = None
        out.append({"id": "nse:%s" % (r.get("seq_id") or r.get("attchmntFile") or cat + text[:40]),
                    "source": "NSE", "kind": "filing", "form": cat, "ticker": ticker,
                    "title": "%s: %s" % (r.get("sm_name") or ticker, cat or "announcement"),
                    "summary": text[:400], "important": bool(IMPORTANT.search(cat)),
                    "link": r.get("attchmntFile") or "", "time": when})
    return out


def parse_chart(data, ticker):
    """Yahoo chart JSON -> {"ticker", "price", "prev_close", "pct", "time"} or raise."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        meta = doc["chart"]["result"][0]["meta"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise NewsError("chart for %s is not usable" % ticker)
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if not price or not prev:
        raise NewsError("chart for %s has no price" % ticker)
    when = meta.get("regularMarketTime")
    return {"ticker": ticker, "price": round(float(price), 2), "prev_close": round(float(prev), 2),
            "pct": round((float(price) / float(prev) - 1) * 100, 2),
            "time": _dt.datetime.fromtimestamp(when, _dt.timezone.utc) if when else None}


def collect_moves(fetcher, watch, errors=None):
    errors = [] if errors is None else errors
    out = []
    for t in sorted(watch):
        name = "chart-%s.json" % t
        try:
            try:
                data = fetcher.get(CHART % ("query2", yahoo_symbol(t)), name)
            except NewsError as exc:
                if fetcher.offline or not re.search(r"HTTP (429|5\d\d)", str(exc)):
                    raise
                data = fetcher.get(CHART % ("query1", yahoo_symbol(t)), name)
            out.append(parse_chart(data, t))
        except NewsError as exc:
            if not (fetcher.offline and "no file" in str(exc)):
                errors.append("price for %s: %s" % (t, exc))
    return out


def mention_patterns(watch):
    pats = {}
    for t, name in watch.items():
        alts = [r"\(%s\)" % re.escape(t), r"\b(?:NSE|BSE)\s*:\s*%s\b" % re.escape(t)]
        if name and len(name) >= 4:
            alts.append(r"\b%s\b" % re.escape(name).replace(r"\ ", r"[\s-]"))
        pats[t] = re.compile("|".join(alts))
    return pats


def tag(items, watch):
    """Which of your tickers each item touches, and why it matters."""
    pats = mention_patterns(watch)
    for it in items:
        hay = "%s %s" % (it["title"], it.get("summary") or "")
        touches = {it["ticker"]} if it["ticker"] in watch else set()
        for t, rx in pats.items():
            if rx.search(hay):
                touches.add(t)
        it["touches"] = sorted(touches)
        it["tags"] = [label for label, rx in KEYWORDS if re.search(rx, hay, re.I)]
        if it["kind"] == "filing" and re.search(r"financial result", it.get("form") or "", re.I) \
                and "results" not in it["tags"]:
            it["tags"].insert(0, "results")
    return items


def dedupe(items):
    """One item per story: same id, or same link, or same title the same day."""
    seen, out = {}, []
    for it in items:
        keys = [it["id"], it["link"],
                "%s|%s" % (re.sub(r"\W+", " ", it["title"].lower()).strip(),
                           it["time"].date() if it["time"] else "")]
        hit = next((seen[k] for k in keys if k and k in seen), None)
        if hit is not None:
            for t in it.get("touches", []):
                if t not in hit["touches"]:
                    hit["touches"].append(t)
            hit["touches"].sort()
            continue
        for k in keys:
            if k:
                seen[k] = it
        out.append(it)
    return out


def fingerprint(it):
    return hashlib.sha1(it["id"].encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- the check

def collect(fetcher, watch, state, nse=True, errors=None):
    errors = [] if errors is None else errors
    items = []
    for t in sorted(watch):
        try:
            got = parse_rss(fetcher.get(YAHOO % yahoo_symbol(t), "yahoo-%s.xml" % t), t)
            if not got:
                errors.append("no Yahoo headlines for %s (Yahoo covers NSE stocks patchily)" % t)
            items += got
        except NewsError as exc:
            errors.append("headlines for %s: %s" % (t, exc))
    if nse:
        for t in sorted(watch):
            if t in india.INDEX_YAHOO:
                continue
            try:
                items += parse_nse(fetcher.get(NSE_ANN % urllib.parse.quote(t), "nse-%s.json" % t, nse=True), t)
            except NewsError as exc:
                errors.append("NSE announcements for %s: %s (headlines only)" % (t, exc))
    return items


def select(items, state, now, hours=48, show_all=False):
    """Items within the window; only unseen ones unless show_all."""
    seen = state.setdefault("seen", {})
    cutoff = now - _dt.timedelta(hours=hours)
    ahead = now + _dt.timedelta(minutes=10)  # a pinned --now: nothing from after it
    out = []
    for it in items:
        if it["time"] is not None and (it["time"] < cutoff or it["time"] > ahead):
            continue
        fp = fingerprint(it)
        it["new"] = fp not in seen
        if it["new"] or show_all:
            out.append(it)
    out.sort(key=lambda i: (i["time"] or now), reverse=True)
    return out


def remember(items, state, now):
    seen = state.setdefault("seen", {})
    stamp = now.isoformat()
    for it in items:
        seen.setdefault(fingerprint(it), stamp)
    cutoff = (now - _dt.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_check"] = stamp


def render_moves(moves, move_pct=MOVE_PCT):
    out = []
    for m in moves:
        flag = "MOVING" if abs(m["pct"]) >= move_pct else ""
        when = (" at %s IST" % to_local(m["time"]).strftime("%H:%M")) if m.get("time") else ""
        out.append("%-10s %s %+.2f%% vs yesterday's close %s%s  %s" % (
            m["ticker"], "{:,.2f}".format(m["price"]), m["pct"], "{:,.2f}".format(m["prev_close"]),
            when, flag))
    return out


def render(items, errors, now, moves=None, move_pct=MOVE_PCT):
    out = render_moves(moves or [], move_pct)
    if out:
        out.append("")
    if not items:
        out.append("Nothing new.")
    for it in items:
        when = to_local(it["time"]).strftime("%Y-%m-%d %H:%M IST") if it["time"] else "time unknown"
        mark = "!" if it.get("important") else " "
        who = ",".join(it["touches"]) or it["ticker"]
        out.append("%s %s  %-11s %-13s %s" % (mark, when, who, it["source"], it["title"]))
        extra = []
        if it["tags"]:
            extra.append("tags: " + ", ".join(it["tags"]))
        extra.append(it["link"])
        out.append("      " + "  ".join(extra))
    for e in errors:
        out.append("note: %s" % e)
    out.append("Checked %s. Headlines and announcements are leads, not facts; open the filing before "
               "acting. Not financial advice." % to_local(now).strftime("%Y-%m-%d %H:%M IST"))
    return "\n".join(out) + "\n"


def _json_ready(items):
    out = []
    for it in items:
        d = dict(it)
        d["time"] = it["time"].isoformat() if it["time"] else None
        out.append(d)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tickers", nargs="*", help="default: watchlist + plan + portfolio")
    ap.add_argument("--portfolio", default="trader/portfolio.json")
    ap.add_argument("--watchlist", default="trader/watchlist.json")
    ap.add_argument("--plan", default="trader/plan.json")
    ap.add_argument("--state", default="trader/state/news.json")
    ap.add_argument("--hours", type=float, default=48, help="ignore items older than this")
    ap.add_argument("--all", action="store_true", help="show seen items too")
    ap.add_argument("--no-nse", action="store_true", help="Yahoo headlines only")
    ap.add_argument("--no-moves", action="store_true", help="skip the price line per ticker")
    ap.add_argument("--move-pct", type=float, default=MOVE_PCT, help="flag a move this big (percent)")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help='pretend it is this time ("2026-10-19 09:30" IST, or ISO with an offset)')
    ap.add_argument("--dry-run", action="store_true", help="do not update the state file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.tickers:
        watch = {t.upper(): None for t in args.tickers}
        known = watch_from_files(args.portfolio, args.watchlist, args.plan)
        watch.update({t: known[t] for t in watch if known.get(t)})
    else:
        watch = watch_from_files(args.portfolio, args.watchlist, args.plan)
    if not watch:
        print("news: no tickers. Pass --tickers, or run /bharat-scanner first (it writes "
              "trader/watchlist.json).", file=sys.stderr)
        return 2
    try:
        now = parse_now(args.now) if args.now else _dt.datetime.now(_dt.timezone.utc)
    except NewsError as exc:
        print("news: %s" % exc, file=sys.stderr)
        return 2
    state, note = load_state(args.state)
    errors = [note] if note else []
    fetcher = Fetcher(offline=args.offline)
    items = collect(fetcher, watch, state, nse=not args.no_nse, errors=errors)
    items = dedupe(tag(items, watch))
    shown = select(items, state, now, hours=args.hours, show_all=args.all)
    moves = [] if args.no_moves else collect_moves(fetcher, watch, errors)
    if args.json:
        mv = [dict(m, time=m["time"].isoformat() if m.get("time") else None,
                   moving=abs(m["pct"]) >= args.move_pct) for m in moves]
        print(json.dumps({"checked": now.isoformat(), "moves": mv, "items": _json_ready(shown),
                          "notes": errors}, indent=2))
    else:
        sys.stdout.write(render(shown, errors, now, moves, args.move_pct))
    if not args.dry_run:
        remember(items, state, now)
        save_state(args.state, state)
    return 0 if any(i["new"] for i in shown) else 1


if __name__ == "__main__":
    sys.exit(main())
