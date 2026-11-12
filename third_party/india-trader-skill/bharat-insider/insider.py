#!/usr/bin/env python3
"""Insider trades, bulk and block deals, and FII/DII flows for bharat-insider.
Standard library only.

Three public NSE feeds that say who is buying and selling:

  insider   SEBI insider-trading (PIT) disclosures filed with NSE: promoters,
            directors, employees and their relatives buying, selling,
            pledging or gifting shares of their own company.
            https://www.nseindia.com/api/corporates-pit?index=equities&symbol=<SYMBOL>
  deals     the latest day's bulk deals (a client trading 0.5 % or more of a
            company's shares in a day) and block deals (large trades in the
            block window). https://www.nseindia.com/api/snapshot-capital-market-largedeal
            One day only: history builds up in the state file across runs.
  flows     the latest day's FII/FPI and DII buying and selling in the cash
            market, Rs crore. https://www.nseindia.com/api/fiidiiTradeReact

    python3 insider.py insider --symbol RELIANCE         # its recent disclosures
    python3 insider.py deals                             # today's bulk and block deals
    python3 insider.py deals --symbol RELIANCE --min-crore 10
    python3 insider.py flows                             # FII/DII net, latest day
    python3 insider.py new                               # all three, only what is new since the last run
    python3 insider.py new --symbols RELIANCE TCS

`new` watches the symbols in --symbols, else trader/watchlist.json, the plan
and the portfolio, and remembers what it has shown in trader/state/insider.json
(90 days). NSE blocks some scripted requests: a feed that fails is reported
and the other feeds still print. Offline: --offline DIR reads pit-<SYMBOL>.json,
large-deals.json and fiidii.json from DIR.

These are public filings, not inside information, and not a reason to trade
on their own.
"""

import argparse
import datetime as _dt
import hashlib
import json
import os
import sys
import urllib.parse

import india

PIT_URL = "https://www.nseindia.com/api/corporates-pit?index=equities&symbol=%s"
DEALS_URL = "https://www.nseindia.com/api/snapshot-capital-market-largedeal"
FLOWS_URL = "https://www.nseindia.com/api/fiidiiTradeReact"
KEEP_DAYS = 90
CRORE = 1e7
PROMOTER = ("Promoter", "Promoter Group", "Director", "KMP", "Key Managerial Personnel")


class InsiderError(Exception):
    pass


def _f(x):
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


class Fetcher(object):
    def __init__(self, offline=None):
        self.offline = offline

    def get(self, url, name):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise InsiderError("offline: no file %s" % path)
            with open(path, "rb") as fh:
                return fh.read()
        try:
            return india.nse_get(url)
        except india.NSEError as e:
            raise InsiderError(str(e))          # names the URL


def parse_now(text):
    """--now -> aware UTC. A time without an offset is IST, as in every bharat-* tool."""
    try:
        d = _dt.datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        raise InsiderError('--now wants a time like "2026-11-04 08:30" (IST) or ...Z (UTC), not %r' % text)
    return india.ist_to_utc(d)


def _json(data, what):
    try:
        return json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
    except ValueError:
        raise InsiderError("%s is not JSON" % what)


# ---------------------------------------------------------------- parsing

def parse_pit(data):
    """PIT disclosures -> rows, newest filing first."""
    doc = _json(data, "the insider disclosures")
    out = []
    for r in (doc.get("data") if isinstance(doc, dict) else doc) or []:
        cat = (r.get("personCategory") or "").strip()
        out.append({
            "id": "pit:%s:%s" % (r.get("did"), r.get("pid")), "feed": "insider",
            "symbol": str(r.get("symbol") or "").upper(), "company": r.get("company") or "",
            "filed": r.get("date") or "", "from": r.get("acqfromDt") or "", "to": r.get("acqtoDt") or "",
            "person": " ".join((r.get("acqName") or "").split()), "category": cat,
            "promoter": any(p.lower() in cat.lower() for p in PROMOTER),
            "type": r.get("tdpTransactionType") or "", "mode": r.get("acqMode") or "",
            "qty": _f(r.get("secAcq")), "value": _f(r.get("secVal")),
            "before": _f(r.get("befAcqSharesNo")), "after": _f(r.get("afterAcqSharesNo")),
            "link": r.get("xbrl") or ""})
    out.sort(key=lambda x: _when(x["filed"]), reverse=True)
    return out


def _when(text):
    for fmt in ("%d-%b-%Y %H:%M", "%d-%b-%Y"):
        try:
            return _dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return _dt.datetime(1970, 1, 1)


def parse_deals(data):
    """The large-deal snapshot -> bulk and block deal rows."""
    doc = _json(data, "the bulk / block deal snapshot")
    out = []
    for key, kind in (("BULK_DEALS_DATA", "bulk"), ("BLOCK_DEALS_DATA", "block")):
        for r in doc.get(key) or []:
            qty, price = _f(r.get("qty")), _f(r.get("watp"))
            out.append({"id": "deal:%s:%s:%s:%s:%s:%s" % (kind, r.get("date"), r.get("symbol"), r.get("clientName"),
                                                          r.get("buySell"), r.get("qty")),
                        "feed": kind, "date": r.get("date") or "", "symbol": str(r.get("symbol") or "").upper(),
                        "company": r.get("name") or "", "client": " ".join((r.get("clientName") or "").split()),
                        "side": str(r.get("buySell") or "").upper(), "qty": qty, "price": price,
                        "value": qty * price if qty is not None and price is not None else None})
    return out


def parse_flows(data):
    """FII/DII -> rows in Rs crore."""
    doc = _json(data, "the FII/DII figures")
    out = []
    for r in doc if isinstance(doc, list) else []:
        out.append({"id": "flow:%s:%s" % (r.get("date"), r.get("category")), "feed": "flows",
                    "date": r.get("date") or "", "category": r.get("category") or "",
                    "buy": _f(r.get("buyValue")), "sell": _f(r.get("sellValue")), "net": _f(r.get("netValue"))})
    return out


# ---------------------------------------------------------------- words

def cr(x):
    return "n/a" if x is None else "Rs {:,.2f} cr".format(x / CRORE)


def _qty(x):
    return "n/a" if x is None else india.inr(round(x), paise=False)[3:]       # 50,00,000 shares


def pit_line(r):
    mark = "!" if r["promoter"] and r["mode"] in ("Market Purchase", "Market Sale") else " "
    return "%s %s  %-10s %-28s %-24s %-5s %s shares, %s (%s, traded %s)" % (
        mark, r["filed"][:11], r["symbol"], r["person"][:28], r["category"][:24], r["type"], _qty(r["qty"]),
        india.inr(r["value"]) if r["value"] is not None else "n/a", r["mode"], r["to"] or r["from"])


def deal_line(r):
    return "  %s  %-5s %-10s %-4s %s shares at Rs %.2f = %s  %s" % (
        r["date"], r["feed"], r["symbol"], r["side"], _qty(r["qty"]), r["price"] or 0, cr(r["value"]), r["client"])


def flow_line(r):
    return "  %s  %-8s bought Rs {:,.2f} cr, sold Rs {:,.2f} cr, net %s Rs {:,.2f} cr".format(
        r["buy"] or 0, r["sell"] or 0, abs(r["net"] or 0)) % (r["date"], r["category"],
                                                            "+" if (r["net"] or 0) >= 0 else "-")


# ---------------------------------------------------------------- state

def load_json(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def load_state(path):
    """(state, note). A state file that cannot be read starts fresh, with a note, instead of stopping every run."""
    return india.read_state(path, "rows you have seen may show again")


save_state = india.write_json   # whole or not at all: a write cut short leaves the old file


def fingerprint(row):
    return hashlib.sha1(row["id"].encode("utf-8")).hexdigest()[:16]


def remember(rows, state, now):
    seen = state.setdefault("seen", {})
    stamp = now.isoformat()
    for r in rows:
        seen.setdefault(fingerprint(r), stamp)
    cutoff = (now - _dt.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_check"] = stamp


def watch_from_files(watchlist="trader/watchlist.json", plan="trader/plan.json", portfolio="trader/portfolio.json"):
    out = []
    for h in (load_json(watchlist) or {}).get("hits", []):
        if h.get("symbol"):
            out.append(str(h["symbol"]).upper())
    p = load_json(plan) or {}
    if p.get("symbol"):
        out.append(str(p["symbol"]).upper())
    for pos in (load_json(portfolio) or {}).get("positions", []):
        s = pos.get("ticker") or pos.get("symbol")
        if s:
            out.append(str(s).upper())
    return sorted(set(out))


# ---------------------------------------------------------------- commands

def collect(fetcher, symbols, errors):
    """(insider rows for the symbols, deal rows, flow rows). A feed that fails goes to errors."""
    pit = []
    for s in symbols:
        try:
            pit += parse_pit(fetcher.get(PIT_URL % urllib.parse.quote(s), "pit-%s.json" % s))
        except InsiderError as e:
            errors.append("insider disclosures for %s: %s" % (s, e))
    deals = flows = []
    try:
        deals = parse_deals(fetcher.get(DEALS_URL, "large-deals.json"))
    except InsiderError as e:
        errors.append("bulk / block deals: %s" % e)
    try:
        flows = parse_flows(fetcher.get(FLOWS_URL, "fiidii.json"))
    except InsiderError as e:
        errors.append("FII/DII: %s" % e)
    return pit, deals, flows


def main(argv=None):
    ap = argparse.ArgumentParser(description="Insider trades, bulk / block deals and FII/DII flows (bharat-insider).")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("insider", help="SEBI insider-trading disclosures for one symbol")
    p.add_argument("--symbol", required=True)
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("deals", help="the latest day's bulk and block deals")
    p.add_argument("--symbol")
    p.add_argument("--min-crore", type=float, default=0.0, help="only deals worth at least this, Rs crore")
    p = sub.add_parser("flows", help="the latest day's FII/DII cash-market figures")
    p = sub.add_parser("new", help="everything new since the last run, for your symbols")
    p.add_argument("--symbols", nargs="*")
    p.add_argument("--watchlist", default="trader/watchlist.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--state", default="trader/state/insider.json")
    p.add_argument("--now", help='pretend it is this time ("2026-11-04 08:30" IST, or ISO with an offset)')
    p.add_argument("--dry-run", action="store_true", help="do not update the state file")
    for sp in sub.choices.values():
        sp.add_argument("--offline", metavar="DIR")
        sp.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    fetcher = Fetcher(a.offline)
    try:
        if a.cmd == "insider":
            sym = a.symbol.strip().upper()
            rows = parse_pit(fetcher.get(PIT_URL % urllib.parse.quote(sym), "pit-%s.json" % sym))[:a.limit]
            if a.json:
                print(json.dumps(rows, indent=2))
            else:
                print("%s: %d insider disclosures (SEBI PIT, filed with NSE), newest first. ! = a promoter-group "
                      "market buy or sale." % (sym, len(rows)))
                for r in rows:
                    print(pit_line(r))
            return 0 if rows else 1
        if a.cmd == "deals":
            rows = [r for r in parse_deals(fetcher.get(DEALS_URL, "large-deals.json"))
                    if (not a.symbol or r["symbol"] == a.symbol.upper())
                    and (r["value"] or 0) >= a.min_crore * CRORE]
            if a.json:
                print(json.dumps(rows, indent=2))
            else:
                print("%d bulk / block deal(s)%s%s, latest day:" % (
                    len(rows), (" in " + a.symbol.upper()) if a.symbol else "",
                    (" worth Rs %g cr or more" % a.min_crore) if a.min_crore else ""))
                for r in sorted(rows, key=lambda r: -(r["value"] or 0)):
                    print(deal_line(r))
            return 0 if rows else 1
        if a.cmd == "flows":
            rows = parse_flows(fetcher.get(FLOWS_URL, "fiidii.json"))
            if a.json:
                print(json.dumps(rows, indent=2))
            else:
                print("FII/DII, cash market:")
                for r in rows:
                    print(flow_line(r))
            return 0 if rows else 1
    except InsiderError as e:
        print("insider.py: %s" % e, file=sys.stderr)
        return 1

    # new
    symbols = [s.upper() for s in a.symbols] if a.symbols else watch_from_files(a.watchlist, a.plan, a.portfolio)
    if not symbols:
        print("insider.py: no symbols. Pass --symbols, or run /bharat-scanner first.", file=sys.stderr)
        return 2
    try:
        now = parse_now(a.now) if a.now else _dt.datetime.now(_dt.timezone.utc)
    except InsiderError as e:
        print("insider.py: %s" % e, file=sys.stderr)
        return 1
    state, note = load_state(a.state)
    seen = state.get("seen", {})
    errors = [note] if note else []
    pit, deals, flows = collect(fetcher, symbols, errors)
    mine = [d for d in deals if d["symbol"] in symbols]
    fresh = [r for r in pit + mine + flows if fingerprint(r) not in seen]
    if a.json:
        print(json.dumps({"symbols": symbols, "new": fresh, "notes": errors}, indent=2))
    else:
        print("New since the last check, for %s:" % ", ".join(symbols))
        for r in fresh:
            print(pit_line(r) if r["feed"] == "insider" else deal_line(r) if r["feed"] in ("bulk", "block")
                  else flow_line(r))
        if not fresh:
            print("  Nothing new.")
        for e in errors:
            print("note: %s" % e)
        print("Public filings, not inside information. Not financial advice.")
    if not a.dry_run:
        remember(pit + mine + flows, state, now)
        save_state(a.state, state)
    return 0 if fresh else 1


if __name__ == "__main__":
    sys.exit(main())
