"""P0 data probe (dev only, not shipped in any skill).

Fetches every source the spec names once and saves the raw response to
tests/fixtures/p0/<name>. Prints one line per source: OK <bytes> or FAIL <reason>.

    python3 tools/probe.py [--only name,name] [--day YYYYMMDD]
    python3 tools/probe.py --p2 [--days 20261016,20261019]   # P2 sources -> tests/fixtures/p2/
    python3 tools/probe.py --p3 --expiry 09-Nov-2026         # NSE option chain -> tests/fixtures/p3/
"""

import argparse
import gzip
import http.cookiejar
import os
import sys
import time
import urllib.request
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tests", "fixtures", "p0")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
_warm = {}


def get(url, referer=None, accept="*/*"):
    host = urllib.request.urlparse(url).netloc
    if host == "www.nseindia.com" and not _warm.get(host):
        # best effort: the home page sets cookies some API paths want, but it can 403 itself
        _warm[host] = True
        try:
            opener.open(urllib.request.Request("https://www.nseindia.com/", headers={"User-Agent": UA}), timeout=20).read()
        except Exception:
            pass
        time.sleep(1)
    # Yahoo 429s browser-looking agents without its cookies; a plain tool agent gets through
    ua = "Mozilla/5.0 (compatible; india-trader-skill/0.1)" if "yahoo.com" in host else UA
    h = {"User-Agent": ua, "Accept": accept, "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip"}
    if referer:
        h["Referer"] = referer
    with opener.open(urllib.request.Request(url, headers=h), timeout=30) as r:
        body = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        return body


def last_weekday(d):
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def sources(day):
    ymd = day.strftime("%Y%m%d")
    frm = (day - timedelta(days=30)).strftime("%d-%m-%Y")
    to = day.strftime("%d-%m-%Y")
    nse = "https://www.nseindia.com/api/"
    arch = "https://nsearchives.nseindia.com/content/"
    yq = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=60d&interval=5m"
    return [
        ("yahoo-RELIANCE.NS-5m.json", yq % "RELIANCE.NS", None, "*/*"),
        ("yahoo-NSEI-5m.json", yq % "%5ENSEI", None, "*/*"),
        ("yahoo-M&M.NS-5m.json", yq % "M%26M.NS", None, "*/*"),
        ("EQUITY_L.csv", arch + "equities/EQUITY_L.csv", None, "*/*"),
        ("sec_list.csv", arch + "equities/sec_list.csv", None, "*/*"),
        ("fo_secban.csv", arch + "fo/fo_secban.csv", None, "*/*"),
        ("fo_mktlots.csv", arch + "fo/fo_mktlots.csv", None, "*/*"),
        ("fo-bhavcopy-%s.csv.zip" % ymd, arch + "fo/BhavCopy_NSE_FO_0_0_0_%s_F_0000.csv.zip" % ymd, None, "*/*"),
        ("holidays.json", nse + "holiday-master?type=trading", "https://www.nseindia.com/", "application/json"),
        ("announcements-RELIANCE.json", nse + "corporate-announcements?index=equities&symbol=RELIANCE",
         "https://www.nseindia.com/", "application/json"),
        ("bse-announcements-500325.json",
         "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w?pageno=1&strCat=-1&strPrevDate=%s"
         "&strScrip=500325&strSearch=P&strToDate=%s&strType=C&subcategory=-1"
         % ((day - timedelta(days=30)).strftime("%Y%m%d"), ymd), "https://www.bseindia.com/", "application/json"),
        # the date-filtered form answers an empty list; the plain form gives the recent filings
        ("pit-RELIANCE.json", nse + "corporates-pit?index=equities&symbol=RELIANCE",
         "https://www.nseindia.com/", "application/json"),
        # historical/bulk-deals answers an HTML page to scripts; the snapshot carries bulk + block
        ("large-deals.json", nse + "snapshot-capital-market-largedeal", "https://www.nseindia.com/", "application/json"),
        ("fiidii.json", nse + "fiidiiTradeReact", "https://www.nseindia.com/", "application/json"),
        ("results-RELIANCE.json", nse + "corporates-financial-results?index=equities&symbol=RELIANCE&period=Quarterly",
         "https://www.nseindia.com/", "application/json"),
        # since 2025 results come through SEBI's integrated filing; the old results list stops at Dec 2024
        ("integrated-RELIANCE.json", nse + "integrated-filing-results?index=equities&symbol=RELIANCE",
         "https://www.nseindia.com/", "application/json"),
    ]


def p2_sources(days):
    arch = "https://nsearchives.nseindia.com/content/fo/"
    yq = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=60d&interval=5m"
    out = [("yahoo-NSEBANK-5m.json", yq % "%5ENSEBANK", None, "*/*"),
           ("yahoo-NSEI-5m.json", yq % "%5ENSEI", None, "*/*"),
           ("fo_mktlots.csv", arch + "fo_mktlots.csv", None, "*/*")]
    for ymd in days:
        out.append(("fo-bhavcopy-%s.csv.zip" % ymd, arch + "BhavCopy_NSE_FO_0_0_0_%s_F_0000.csv.zip" % ymd, None, "*/*"))
    return out


def p3_sources(expiry):
    chain = "https://www.nseindia.com/api/option-chain-v3?type=Indices&symbol=NIFTY&expiry=" + expiry
    return [("nse-option-chain-NIFTY.json", chain, "https://www.nseindia.com/option-chain", "application/json")]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--day", default="")
    ap.add_argument("--p2", action="store_true")
    ap.add_argument("--days", default="20261016,20261019")
    ap.add_argument("--p3", action="store_true")
    ap.add_argument("--expiry", default="09-Nov-2026", help="option chain expiry, DD-Mon-YYYY")
    a = ap.parse_args(argv)
    global OUT
    day = date(int(a.day[:4]), int(a.day[4:6]), int(a.day[6:])) if a.day else last_weekday(date.today() - timedelta(days=1))
    if a.p2 or a.p3:
        OUT = os.path.join(ROOT, "tests", "fixtures", "p3" if a.p3 else "p2")
    os.makedirs(OUT, exist_ok=True)
    only = set(x for x in a.only.split(",") if x)
    bad = 0
    srcs = p3_sources(a.expiry) if a.p3 else p2_sources(a.days.split(",")) if a.p2 else sources(day)
    for name, url, ref, acc in srcs:
        if only and not any(o in name for o in only):
            continue
        try:
            body = get(url, ref, acc)
            if acc == "application/json" and body.lstrip()[:1] == b"<":
                raise ValueError("HTML, not JSON")
            with open(os.path.join(OUT, name), "wb") as fh:
                fh.write(body)
            print("OK   %-34s %8d" % (name, len(body)))
        except Exception as e:  # report and keep going: this is a probe
            bad += 1
            print("FAIL %-34s %s" % (name, str(e)[:90]))
        time.sleep(1)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
