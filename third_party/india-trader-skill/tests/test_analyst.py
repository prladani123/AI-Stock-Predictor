import io
import json
import os
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX, ROOT

res = load("bharat-analyst", "results")
DIR = os.path.join(FIX, "bharat-analyst")      # RELIANCE's real integrated-filing list and 7 XBRL files, 4 Nov 2026
Q2 = os.path.join(DIR, "RELIANCE", "INTEGRATED_FILING_INDAS_1750537_30102026093254_WEB.xml")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = res.main(argv)
    return code, out.getvalue(), err.getvalue()


class Xbrl(unittest.TestCase):
    def test_the_quarter_not_the_year_to_date(self):
        with open(Q2, "rb") as fh:
            q = res.parse_xbrl(fh.read())
        self.assertEqual((q["start"], q["end"], q["basis"]), ("2026-07-01", "2026-09-30", "Consolidated"))
        self.assertEqual(q["revenue"], 3328610000000.0)          # not the 6,447,110,000,000 half year
        self.assertEqual((q["pbt"], q["pat"], q["pat_owners"], q["eps"]),
                         (386130000000.0, 295500000000.0, 255480000000.0, 18.88))
        # EBITDA by hand: 38,613 + 8,481 + 15,315 - 5,793 = 56,616 crore
        self.assertEqual(round(q["ebitda"] / res.CRORE), 56616)
        self.assertEqual(q["net_profit"], q["pat_owners"])

    def test_a_missing_tag_is_none_not_zero(self):
        with open(Q2, "rb") as fh:
            text = fh.read().decode("utf-8")
        cut = text.replace("in-capmkt:OtherIncome", "in-capmkt:SomethingElse")
        q = res.parse_xbrl(cut.encode("utf-8"))
        self.assertIsNone(q["other_income"])
        self.assertIsNone(q["ebitda"])
        self.assertEqual(res.margin(None, 5), None)

    def test_bad_files(self):
        with self.assertRaises(res.ResultsError):
            res.parse_xbrl(b"<html>blocked</html")
        with self.assertRaises(res.ResultsError):
            res.parse_xbrl(b"<xbrl></xbrl>")


class Quarters(unittest.TestCase):
    def setUp(self):
        self.qs, self.notes = res.results("RELIANCE", res.Fetcher(DIR), quarters=5)

    def test_five_quarters_newest_first(self):
        self.assertEqual([q["end"] for q in self.qs],
                         ["2026-09-30", "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"])
        self.assertEqual(self.notes, [])

    def test_growth_and_margins(self):
        q = self.qs[0]
        self.assertEqual((q["revenue_qoq"], q["revenue_yoy"], q["eps_yoy"]), (6.7, 28.6, 40.7))
        self.assertEqual((q["ebitda_margin"], q["net_margin"]), (17.0, 7.7))
        self.assertIsNone(self.qs[3]["revenue_yoy"])             # Dec 2024 is not in the integrated filings
        self.assertIsNone(self.qs[-1]["revenue_yoy"])

    def test_standalone_is_another_basis(self):
        found = res.filings(open(os.path.join(DIR, "RELIANCE", "integrated-filing-results.json"), "rb").read(),
                            "Standalone")
        self.assertEqual(found[0][0], "2026-09-30")
        self.assertTrue(found[0][1].endswith("INTEGRATED_FILING_INDAS_1750535_30102026093155_WEB.xml"))


class Cli(unittest.TestCase):
    def test_the_table(self):
        code, text, err = run(["RELIANCE", "--offline", DIR])
        self.assertEqual(code, 0, err)
        self.assertIn("RELIANCE, consolidated quarterly results from NSE filings (Rs crore; EPS in Rs)", text)
        self.assertIn("2026-09-30        332,861    +6.7%   +28.6%       56,616   17.0%       25,548    7.7%    "
                      "18.88   +40.7%", text)
        self.assertIn("not a recommendation", text)
        for never in ("buy", "sell", "undervalued", "target price"):
            self.assertNotIn(never, text.lower().replace("not a recommendation", ""))

    def test_json_and_a_missing_symbol(self):
        code, text, err = run(["RELIANCE", "TCS", "--offline", DIR, "--json", "--quarters", "2"])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(text)["RELIANCE"]["quarters"]), 2)
        self.assertIn("TCS: offline: no file", err)
        code, _, err = run(["TCS", "--offline", DIR])
        self.assertEqual(code, 1)


class SkillDoc(unittest.TestCase):
    def test_skill_md(self):
        with open(os.path.join(ROOT, "bharat-analyst", "SKILL.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---\nname: bharat-analyst\n"))
        low = text.lower()
        for must in ("results.py", "integrated filing", "ebitda", "crore", "n/a"):
            self.assertIn(must, low)
        for never in ("sec ", "edgar", "8-k", "10-q", "facts.py"):
            self.assertNotIn(never, low)


if __name__ == "__main__":
    unittest.main()


class ReviewFixes(unittest.TestCase):
    def test_only_nse_https_links_are_opened(self):
        f = res.Fetcher()
        for bad in ("file:///etc/passwd", "http://nsearchives.nseindia.com/x.xml", "https://evil.example/x.xml",
                    "https://evilnseindia.com/x.xml"):
            with self.assertRaises(res.ResultsError) as cm:
                f.xbrl("RELIANCE", bad)
            self.assertIn("not an NSE https link", str(cm.exception))
