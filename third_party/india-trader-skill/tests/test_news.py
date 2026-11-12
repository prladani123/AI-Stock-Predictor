import datetime
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

news = load("bharat-news", "news")
DIR = os.path.join(FIX, "bharat-news")
NOW = "2026-11-04T03:00:00+00:00"   # 8:30 AM IST, Wed Nov 4 2026, when the fixtures were fetched


def raw(name):
    with open(os.path.join(DIR, name), "rb") as fh:
        return fh.read()


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = news.main(args)
    return code, out.getvalue(), err.getvalue()


class Feeds(unittest.TestCase):
    def test_nse_announcements(self):
        items = news.parse_nse(raw("nse-RELIANCE.json"), "RELIANCE")
        self.assertEqual(len(items), 19)
        board = [i for i in items if i["form"] == "Outcome of Board Meeting"][0]
        self.assertTrue(board["important"])
        self.assertEqual(board["title"], "Reliance Industries Limited: Outcome of Board Meeting")
        # 30-Oct-2026 19:40:11 IST is 14:10:11 UTC
        self.assertEqual(board["time"].isoformat(), "2026-10-30T14:10:11+00:00")
        self.assertTrue(board["link"].startswith("https://nsearchives.nseindia.com/corporate/"))
        self.assertTrue(board["id"].startswith("nse:"))
        clip = [i for i in items if i["form"] == "Copy of Newspaper Publication"][0]
        self.assertFalse(clip["important"])

    def test_nse_json_errors(self):
        with self.assertRaises(news.NewsError):
            news.parse_nse(b"<html>blocked</html>", "RELIANCE")
        self.assertEqual(news.parse_nse(b'{"data": []}', "RELIANCE"), [])

    def test_yahoo_rss(self):
        items = news.parse_rss(raw("yahoo-TCS.xml"), "TCS")
        self.assertEqual(len(items), 16)
        self.assertEqual(items[0]["time"], datetime.datetime(2026, 10, 30, 21, 15, tzinfo=datetime.timezone.utc))
        self.assertEqual(news.parse_rss(raw("yahoo-RELIANCE.xml"), "RELIANCE"), [])   # Yahoo has none

    def test_times_and_symbols(self):
        self.assertEqual(news.to_local(datetime.datetime(2026, 10, 30, 14, 10, 11)).strftime("%H:%M"), "19:40")
        self.assertEqual(news.yahoo_symbol("M&M"), "M%26M.NS")
        self.assertEqual(news.yahoo_symbol("NIFTY"), "^NSEI")
        self.assertIsNone(news.parse_time("soon"))


class Tagging(unittest.TestCase):
    def test_results_and_board_meeting_tags(self):
        items = news.tag(news.parse_nse(raw("nse-RELIANCE.json"), "RELIANCE"), {"RELIANCE": None})
        board = [i for i in items if i["form"] == "Outcome of Board Meeting"][0]
        self.assertEqual(board["tags"][:2], ["results", "board meeting"])

    def test_india_tags(self):
        items = [{"id": "x", "source": "Yahoo Finance", "kind": "headline", "ticker": "ITC",
                  "title": "ITC fixes record date for interim dividend; block deal in HUL; RBI holds repo rate",
                  "summary": "", "link": "", "time": None}]
        tags = news.tag(items, {"ITC": None})[0]["tags"]
        for t in ("dividend/split", "block deal", "regulatory", "macro"):
            self.assertIn(t, tags)

    def test_mentions(self):
        items = [{"id": "y", "source": "Yahoo Finance", "kind": "headline", "ticker": "TCS",
                  "title": "Tata Consultancy (NSE: TCS) and Infosys (NSE:INFY) win deals", "summary": "",
                  "link": "", "time": None}]
        self.assertEqual(news.tag(items, {"TCS": None, "INFY": None})[0]["touches"], ["INFY", "TCS"])

    def test_dedupe_merges_the_same_announcement(self):
        items = news.dedupe(news.tag(news.parse_nse(raw("nse-RELIANCE.json"), "RELIANCE"), {"RELIANCE": None}))
        self.assertEqual(sum(1 for i in items if i["form"] == "Outcome of Board Meeting"), 1)  # filed twice


class Check(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state", "news.json")
        none = os.path.join(self.tmp.name, "none.json")
        self.base = ["--offline", DIR, "--state", self.state, "--now", NOW,
                     "--portfolio", none, "--watchlist", none, "--plan", none]

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_items_once_newest_first(self):
        code, out, err = run(["--tickers", "RELIANCE", "TCS", "--hours", "168", "--json"] + self.base)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        times = [i["time"] for i in data["items"]]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertIn("no Yahoo headlines for RELIANCE (Yahoo covers NSE stocks patchily)", data["notes"])
        code, out, _ = run(["--tickers", "RELIANCE", "TCS", "--hours", "168"] + self.base)
        self.assertEqual(code, 1)
        self.assertIn("Nothing new.", out)

    def test_board_meeting_is_flagged(self):
        code, out, err = run(["--tickers", "RELIANCE", "--hours", "168", "--no-moves"] + self.base)
        self.assertEqual(code, 0, err)
        self.assertIn("! 2026-10-30 19:40 IST  RELIANCE    NSE           Reliance Industries Limited: "
                      "Outcome of Board Meeting", out)
        self.assertIn("Not financial advice", out)
        self.assertNotIn(chr(0x2014), out)

    def test_the_example_morning(self):
        # RELIANCE, Monday Oct 19 2026, checked at 9:30 AM IST: the weekend's filings
        code, out, err = run(["--tickers", "RELIANCE", "--hours", "48", "--no-moves",
                              "--now", "2026-10-19T09:30:00+05:30"] + self.base[:4])
        self.assertEqual(code, 0, err)
        self.assertIn("2026-10-17 19:32 IST  RELIANCE    NSE           Reliance Industries Limited: Updates", out)
        self.assertNotIn("2026-10-21", out)          # filed after the pinned clock
        self.assertIn("Checked 2026-10-19 09:30 IST", out)

    def test_an_unreadable_state_file_starts_fresh(self):
        os.makedirs(os.path.dirname(self.state))
        with open(self.state, "w") as fh:
            fh.write('{"seen": {"abc": "2026-11-0')            # cut short
        code, out, err = run(["--tickers", "RELIANCE", "--hours", "168", "--no-moves"] + self.base)
        self.assertEqual(code, 0, err)
        self.assertIn("starting fresh", out)
        with open(self.state) as fh:
            self.assertTrue(json.load(fh)["seen"])               # written again, whole

    def test_a_state_file_of_the_wrong_shape_starts_fresh(self):
        # valid JSON, but not the {"seen": {fingerprint: time}} the tool writes
        os.makedirs(os.path.dirname(self.state))
        for bad in ('["x"]', '"x"', '{"seen": ["abc"]}', '{"seen": {"abc": 5}}'):
            with open(self.state, "w") as fh:
                fh.write(bad)
            code, out, err = run(["--tickers", "RELIANCE", "--hours", "168", "--no-moves"] + self.base)
            self.assertEqual(code, 0, (bad, err))
            self.assertIn("starting fresh", out, bad)
            with open(self.state) as fh:
                self.assertTrue(json.load(fh)["seen"], bad)

    def test_a_now_without_a_zone_is_ist(self):
        # as in every other bharat-* tool: 9:30 with no offset is 9:30 IST, 04:00 UTC
        code, out, err = run(["--tickers", "RELIANCE", "--hours", "48", "--no-moves", "--json",
                              "--now", "2026-10-19 09:30"] + self.base[:4])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["checked"], "2026-10-19T04:00:00+00:00")

    def test_nse_blocked_keeps_the_headlines(self):
        class Blocked(news.Fetcher):
            def get(self, url, name, nse=False):
                if nse:
                    raise news.NewsError("got an HTML page, not JSON (NSE blocked the request)")
                return news.Fetcher.get(self, url, name)
        watch = {"TCS": None}
        errors = []
        items = news.collect(Blocked(offline=DIR), watch, {}, errors=errors)
        self.assertEqual(len(items), 16)              # the Yahoo headlines still come through
        self.assertEqual(errors, ["NSE announcements for TCS: got an HTML page, not JSON (NSE blocked the "
                                  "request) (headlines only)"])

    def test_tickers_from_watchlist_and_plan(self):
        wl = os.path.join(self.tmp.name, "watchlist.json")
        with open(wl, "w") as fh:
            json.dump({"created_by": "bharat-scanner", "hits": [{"symbol": "TCS", "setups": ["orb"]}]}, fh)
        plan = os.path.join(self.tmp.name, "plan.json")
        with open(plan, "w") as fh:
            json.dump({"created_by": "bharat-exit", "symbol": "RELIANCE"}, fh)
        self.assertEqual(sorted(news.watch_from_files("/nonexistent.json", wl, plan)), ["RELIANCE", "TCS"])

    def test_no_tickers_is_an_error(self):
        code, _, err = run(self.base)
        self.assertEqual(code, 2)
        self.assertIn("no tickers", err)

    def test_moves(self):
        m = news.parse_chart(raw("chart-RELIANCE.json"), "RELIANCE")
        self.assertEqual((m["price"], m["prev_close"], m["pct"]), (1307.9, 1300.0, 0.61))
        self.assertEqual(news.to_local(m["time"]).strftime("%H:%M"), "15:29")
        self.assertIn("MOVING", news.render_moves([dict(m, pct=3.5)])[0])
        self.assertNotIn("MOVING", news.render_moves([m])[0])
        with self.assertRaises(news.NewsError):
            news.parse_chart(b'{"chart": {"result": null}}', "ZZZZ")

    def test_state_forgets_after_30_days(self):
        state = {"seen": {"old": "2026-09-01T00:00:00+00:00"}}
        now = news.parse_time(NOW)
        news.remember([], state, now)
        self.assertNotIn("old", state["seen"])
        self.assertEqual(state["last_check"], now.isoformat())


if __name__ == "__main__":
    unittest.main()
