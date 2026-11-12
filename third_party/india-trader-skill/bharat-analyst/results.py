#!/usr/bin/env python3
"""Quarterly results reader for bharat-analyst. Standard library only.

Reads a company's quarterly results from what it filed with NSE (SEBI's
integrated filing, since 2025) and lays out what changed, quarter by quarter:
revenue, operating profit (EBITDA), profit before tax, net profit, EPS, the
margins, and growth against the quarter before (QoQ) and the same quarter a
year earlier (YoY).

    python3 results.py RELIANCE
    python3 results.py RELIANCE TCS --quarters 6
    python3 results.py RELIANCE --standalone
    python3 results.py RELIANCE --json

Sources:
  https://www.nseindia.com/api/integrated-filing-results?index=equities&symbol=<SYMBOL>
      the list of filings, each with its XBRL link (NSE blocks some scripted
      requests: the tool says so and stops for that symbol)
  https://nsearchives.nseindia.com/corporate/xbrl/<file>.xml
      one XBRL file per quarter, Ind-AS tags (in-capmkt:)

How a quarter is read:
  - The XBRL context that covers about three months (80 to 100 days) and
    ends on the quarter's date is the quarter; a year-to-date context is
    ignored. Consolidated by default (--standalone for the parent alone).
  - Revenue       RevenueFromOperations
    EBITDA        ProfitBeforeExceptionalItemsAndTax + FinanceCosts
                  + DepreciationDepletionAndAmortisationExpense - OtherIncome
    PBT           ProfitBeforeTax
    Net profit    ProfitOrLossAttributableToOwnersOfParent (consolidated),
                  else ProfitLossForPeriod
    EPS           BasicEarningsLossPerShareFromContinuingOperations, else the
                  continuing-and-discontinued one
  - A tag the company did not file shows as n/a, never as 0.
  - Rupee amounts print in crore (1 crore = 1,00,00,000).

Offline: --offline DIR reads DIR/<SYMBOL>/integrated-filing-results.json and
the XBRL files named as in its links. The tests use it.

Numbers only: this tool never says whether a stock is a buy.
"""

import argparse
import datetime as _dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import india

LIST_URL = "https://www.nseindia.com/api/integrated-filing-results?index=equities&symbol=%s"
CRORE = 1e7
TAGS = {
    "revenue": ("RevenueFromOperations",),
    "other_income": ("OtherIncome",),
    "finance_costs": ("FinanceCosts",),
    "depreciation": ("DepreciationDepletionAndAmortisationExpense",),
    "pbt_before_exceptional": ("ProfitBeforeExceptionalItemsAndTax",),
    "pbt": ("ProfitBeforeTax",),
    "pat": ("ProfitLossForPeriod",),
    "pat_owners": ("ProfitOrLossAttributableToOwnersOfParent",),
    "eps": ("BasicEarningsLossPerShareFromContinuingOperations",
            "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations"),
}


class ResultsError(Exception):
    pass


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _date(s):
    return _dt.datetime.strptime(s.strip()[:10], "%Y-%m-%d").date()


def parse_xbrl(data):
    """One NSE results XBRL file -> the quarter's numbers (rupees; EPS in rupees a share)."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ResultsError("the XBRL file is not valid XML: %s" % e)
    contexts = {}
    for el in root.iter():
        if _local(el.tag) != "context":
            continue
        dims = any(_local(c.tag) == "explicitMember" for c in el.iter())
        dates = {_local(c.tag): (c.text or "") for c in el.iter() if _local(c.tag) in ("startDate", "endDate")}
        if not dims and "startDate" in dates and "endDate" in dates:
            contexts[el.get("id")] = (_date(dates["startDate"]), _date(dates["endDate"]))
    facts = {}
    for el in root.iter():
        ref = el.get("contextRef")
        if ref in contexts and el.text and el.text.strip():
            facts.setdefault(ref, {})[_local(el.tag)] = el.text.strip()
    quarter = [(cid, se) for cid, se in contexts.items() if 80 <= (se[1] - se[0]).days <= 100]
    if not quarter:
        raise ResultsError("no three-month period in this filing")
    quarter.sort(key=lambda x: x[1][1], reverse=True)
    cid, (start, end) = quarter[0]
    f = facts.get(cid, {})
    out = {"start": start.isoformat(), "end": end.isoformat(),
           "basis": f.get("NatureOfReportStandaloneConsolidated") or ""}
    for key, names in TAGS.items():
        out[key] = None
        for n in names:
            try:
                out[key] = float(f[n])
                break
            except (KeyError, ValueError):
                continue
    parts = (out["pbt_before_exceptional"], out["finance_costs"], out["depreciation"], out["other_income"])
    out["ebitda"] = (parts[0] + parts[1] + parts[2] - parts[3]) if None not in parts else None
    out["net_profit"] = out["pat_owners"] if out["pat_owners"] is not None else out["pat"]
    return out


def pct(a, b):
    if a is None or b is None or b == 0:
        return None
    return round(100.0 * (a / b - 1), 1)


def margin(part, whole):
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, 1)


def analyse(quarters):
    """Quarters (any order) -> newest first, each with margins, QoQ and YoY growth."""
    qs = sorted(quarters, key=lambda q: q["end"], reverse=True)
    by_end = {q["end"]: q for q in qs}
    for i, q in enumerate(qs):
        prev = qs[i + 1] if i + 1 < len(qs) else None
        end = _date(q["end"])
        year_ago = next((by_end[e] for e in by_end if abs((end - _date(e)).days - 365) <= 10), None)
        q["ebitda_margin"] = margin(q["ebitda"], q["revenue"])
        q["net_margin"] = margin(q["net_profit"], q["revenue"])
        for k in ("revenue", "ebitda", "net_profit", "eps"):
            q["%s_qoq" % k] = pct(q[k], prev[k]) if prev else None
            q["%s_yoy" % k] = pct(q[k], year_ago[k]) if year_ago else None
    return qs


class Fetcher(object):
    def __init__(self, offline=None):
        self.offline = offline

    def listing(self, sym):
        if self.offline:
            return self._file(os.path.join(self.offline, sym, "integrated-filing-results.json"))
        try:
            return india.nse_get(LIST_URL % urllib.parse.quote(sym))
        except india.NSEError as e:
            raise ResultsError("NSE did not answer for %s: %s" % (sym, e))

    def xbrl(self, sym, url):
        name = url.rsplit("/", 1)[-1]
        if self.offline:
            return self._file(os.path.join(self.offline, sym, name))
        u = urllib.parse.urlparse(url)
        host = u.hostname or ""
        if u.scheme != "https" or not (host == "nseindia.com" or host.endswith(".nseindia.com")):
            raise ResultsError("the filing link %r is not an NSE https link; not opened" % url)
        req = urllib.request.Request(url, headers={"User-Agent": india.TOOL_UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except (urllib.error.URLError, OSError) as e:
            raise ResultsError("could not read %s: %s" % (name, getattr(e, "reason", e)))

    def _file(self, path):
        if not os.path.exists(path):
            raise ResultsError("offline: no file %s" % path)
        with open(path, "rb") as fh:
            return fh.read()


def filings(listing, basis="Consolidated"):
    """The integrated-filing list -> [(quarter end, xbrl url)] for financial results on that basis."""
    try:
        doc = json.loads(listing.decode("utf-8") if isinstance(listing, bytes) else listing)
    except ValueError:
        raise ResultsError("NSE's filing list is not JSON")
    rows = doc.get("data", doc) if isinstance(doc, dict) else doc
    out = {}
    for r in rows or []:
        if "Financials" not in str(r.get("type") or "") or r.get("consolidated") != basis or not r.get("xbrl"):
            continue
        try:
            end = _dt.datetime.strptime(r["qe_Date"], "%d-%b-%Y").date().isoformat()
        except (KeyError, ValueError):
            continue
        if r.get("type_Sub") == "Original" or end not in out:
            out[end] = r["xbrl"]
    return sorted(out.items(), reverse=True)


def results(sym, fetcher, quarters=5, basis="Consolidated"):
    sym = sym.strip().upper()
    found = filings(fetcher.listing(sym), basis)
    if not found:
        raise ResultsError("no %s financial results for %s in NSE's integrated filings" % (basis.lower(), sym))
    qs, notes = [], []
    for end, url in found[:quarters + 4]:          # four more so the oldest shown quarter has its YoY
        try:
            qs.append(parse_xbrl(fetcher.xbrl(sym, url)))
        except ResultsError as e:
            notes.append("%s: %s" % (end, e))
    return analyse(qs)[:quarters], notes


def _cr(x):
    return "n/a" if x is None else "{:,.0f}".format(x / CRORE)


def _g(x):
    return "n/a" if x is None else "%+.1f%%" % x


def _m(x):
    return "n/a" if x is None else "%.1f%%" % x


def render(sym, qs, notes, basis):
    lines = ["%s, %s quarterly results from NSE filings (Rs crore; EPS in Rs)" % (sym, basis.lower()), ""]
    lines.append("%-12s %12s %8s %8s %12s %7s %12s %7s %8s %8s" % (
        "quarter end", "revenue", "QoQ", "YoY", "EBITDA", "margin", "net profit", "margin", "EPS", "EPS YoY"))
    for q in qs:
        lines.append("%-12s %12s %8s %8s %12s %7s %12s %7s %8s %8s" % (
            q["end"], _cr(q["revenue"]), _g(q["revenue_qoq"]), _g(q["revenue_yoy"]), _cr(q["ebitda"]),
            _m(q["ebitda_margin"]), _cr(q["net_profit"]), _m(q["net_margin"]),
            "n/a" if q["eps"] is None else "%.2f" % q["eps"], _g(q["eps_yoy"])))
    for n in notes:
        lines.append("note: " + n)
    lines.append("")
    lines.append("EBITDA = profit before exceptional items and tax + finance costs + depreciation - other income. "
                 "Numbers as filed; not a recommendation.")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Quarterly results from NSE filings (bharat-analyst).")
    ap.add_argument("symbols", nargs="+")
    ap.add_argument("--quarters", type=int, default=5)
    ap.add_argument("--standalone", action="store_true", help="the parent company alone, not consolidated")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    basis = "Standalone" if a.standalone else "Consolidated"
    fetcher = Fetcher(a.offline)
    out, failed = {}, False
    for sym in a.symbols:
        try:
            qs, notes = results(sym, fetcher, a.quarters, basis)
        except ResultsError as e:
            print("results.py: %s: %s" % (sym.upper(), e), file=sys.stderr)
            failed = True
            continue
        out[sym.upper()] = {"basis": basis, "quarters": qs, "notes": notes}
        if not a.json:
            print(render(sym.upper(), qs, notes, basis))
            print()
    if a.json:
        print(json.dumps(out, indent=2))
    return 1 if failed and not out else 0


if __name__ == "__main__":
    sys.exit(main())
