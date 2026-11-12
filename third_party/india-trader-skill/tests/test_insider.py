import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

ins = load("bharat-insider", "insider")
DIR = os.path.join(FIX, "bharat-insider")       # real NSE answers, 4 Nov 2026: PIT for RELIANCE, large deals, FII/DII
NOW = "2026-11-04T03:00:00+00:00"


def raw(name):
    with open(os.path.join(DIR, name), "rb") as fh:
        return fh.read()


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ins.main(argv)
    return code, out.getvalue(), err.getvalue()


class Feeds(unittest.TestCase):
    def test_insider_disclosures(self):
        rows = ins.parse_pit(raw("pit-RELIANCE.json"))
        self.assertEqual(len(rows), 20)
        first = rows[0]
        self.assertEqual((first["person"], first["category"], first["type"], first["mode"], first["qty"],
                          first["value"], first["filed"]),
                         ("BALANADU NARAYAN", "Other", "Sell", "Off Market", 2320.0, 3294168.0, "18-Feb-2026 19:06"))
        self.assertFalse(first["promoter"])
        self.assertTrue(any(r["promoter"] for r in rows))                 # Promoter Group filings are flagged
        filed = [ins._when(r["filed"]) for r in rows]
        self.assertEqual(filed, sorted(filed, reverse=True))

    def test_bulk_and_block_deals(self):
        rows = ins.parse_deals(raw("large-deals.json"))
        self.assertEqual((sum(1 for r in rows if r["feed"] == "bulk"), sum(1 for r in rows if r["feed"] == "block")),
                         (147, 2))
        g = [r for r in rows if r["feed"] == "block" and r["side"] == "BUY"][0]
        self.assertEqual((g["symbol"], g["qty"], g["price"], g["value"]), ("GALLANTT", 5000000.0, 501.0, 2505000000.0))
        self.assertEqual(ins.cr(g["value"]), "Rs 250.50 cr")

    def test_fii_dii(self):
        rows = ins.parse_flows(raw("fiidii.json"))
        self.assertEqual([(r["category"], r["net"]) for r in rows], [("DII", -3678.85), ("FII/FPI", 6836.81)])
        self.assertEqual(ins.flow_line(rows[1]),
                         "  03-Nov-2026  FII/FPI  bought Rs 20,970.51 cr, sold Rs 14,133.70 cr, net + Rs 6,836.81 cr")

    def test_not_json(self):
        with self.assertRaises(ins.InsiderError):
            ins.parse_pit(b"<html>blocked</html>")


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state", "insider.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_insider_deals_flows(self):
        code, text, _ = run(["insider", "--symbol", "reliance", "--offline", DIR, "--limit", "1"])
        self.assertEqual(code, 0)
        self.assertIn("BALANADU NARAYAN", text)
        self.assertIn("2,320 shares, Rs 32,94,168 (Off Market, traded 13-Feb-2026)", text)
        code, text, _ = run(["deals", "--symbol", "GALLANTT", "--offline", DIR])
        self.assertIn("block GALLANTT   BUY  50,00,000 shares at Rs 501.00 = Rs 250.50 cr", text)
        code, text, _ = run(["deals", "--symbol", "RELIANCE", "--offline", DIR])
        self.assertEqual(code, 1)                                         # no RELIANCE deal that day
        code, text, _ = run(["flows", "--offline", DIR])
        self.assertIn("DII      bought Rs 16,723.91 cr", text)

    def test_new_shows_each_row_once(self):
        args = ["new", "--symbols", "RELIANCE", "GALLANTT", "--offline", DIR, "--state", self.state, "--now", NOW]
        code, out, _ = run(args + ["--json"])
        self.assertEqual(code, 0)
        doc = json.loads(out)
        feeds = [r["feed"] for r in doc["new"]]
        self.assertEqual((feeds.count("insider"), feeds.count("bulk"), feeds.count("block"), feeds.count("flows")),
                         (20, 2, 2, 2))
        code, out, _ = run(args)
        self.assertEqual(code, 1)
        self.assertIn("Nothing new.", out)

    def test_an_unreadable_state_file_starts_fresh(self):
        os.makedirs(os.path.dirname(self.state), exist_ok=True)
        with open(self.state, "w") as fh:
            fh.write('{"seen": {')                               # cut short
        code, out, err = run(["new", "--symbols", "RELIANCE", "--offline", DIR, "--state", self.state, "--now", NOW])
        self.assertEqual(code, 0, err)
        self.assertIn("starting fresh", out)
        with open(self.state) as fh:
            self.assertTrue(json.load(fh)["seen"])

    def test_a_state_file_of_the_wrong_shape_starts_fresh(self):
        # valid JSON, but not the {"seen": {fingerprint: time}} the tool writes
        os.makedirs(os.path.dirname(self.state), exist_ok=True)
        for bad in ('["x"]', '"x"', '{"seen": ["abc"]}', '{"seen": {"abc": 5}}'):
            with open(self.state, "w") as fh:
                fh.write(bad)
            code, out, err = run(["new", "--symbols", "RELIANCE", "--offline", DIR, "--state", self.state,
                                  "--now", NOW])
            self.assertEqual(code, 0, (bad, err))
            self.assertIn("starting fresh", out, bad)
            with open(self.state) as fh:
                self.assertTrue(json.load(fh)["seen"], bad)

    def test_a_now_without_a_zone_is_ist(self):
        run(["new", "--symbols", "RELIANCE", "--offline", DIR, "--state", self.state, "--now", "2026-11-04 08:30"])
        with open(self.state) as fh:
            self.assertEqual(json.load(fh)["last_check"], "2026-11-04T03:00:00+00:00")   # 8:30 IST

    def test_one_feed_failing_keeps_the_others(self):
        class Blocked(ins.Fetcher):
            def get(self, url, name):
                if "corporates-pit" in url:
                    raise ins.InsiderError("got an HTML page, not JSON (NSE blocked the request)")
                return ins.Fetcher.get(self, url, name)
        errors = []
        pit, deals, flows = ins.collect(Blocked(DIR), ["RELIANCE"], errors)
        self.assertEqual((len(pit), len(deals), len(flows)), (0, 149, 2))
        self.assertEqual(errors, ["insider disclosures for RELIANCE: got an HTML page, not JSON (NSE blocked the "
                                  "request)"])

    def test_no_symbols(self):
        none = os.path.join(self.tmp.name, "none.json")
        code, _, err = run(["new", "--offline", DIR, "--watchlist", none, "--plan", none, "--portfolio", none])
        self.assertEqual(code, 2)
        self.assertIn("no symbols", err)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "bharat-insider", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: bharat-insider\n"))
        low = text.lower()
        for must in ("insider.py", "sebi", "bulk", "block", "fii", "dii", "not inside information"):
            self.assertIn(must, low)
        for never in ("pelosi", "congress", "ptr.py", "house clerk"):
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()


class ReviewFixes(unittest.TestCase):
    def test_a_failed_fetch_names_the_url(self):
        def blocked(url, **kw):
            raise ins.india.NSEError(url, "HTTP 403")
        old = ins.india.nse_get
        ins.india.nse_get = blocked
        try:
            with self.assertRaises(ins.InsiderError) as cm:
                ins.Fetcher().get(ins.FLOWS_URL, "fiidii.json")
        finally:
            ins.india.nse_get = old
        self.assertIn("fiidiiTradeReact", str(cm.exception))
