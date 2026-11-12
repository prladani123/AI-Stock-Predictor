import json
import os
import re
import sys
import unittest

from _load import ROOT

SKILLS = ["bharat-scanner", "bharat-news", "bharat-entry", "bharat-exit", "bharat-timemachine",
          "bharat-risk", "bharat-budget", "bharat-broker", "bharat-alerts", "bharat-analyst",
          "bharat-insider"]
ABOUT = ("11 Claude skills for intraday trading on NSE: scan the market, track announcements, time "
         "every entry and exit, and replay every outcome before you risk a rupee.")
# skills on the way to an order carry the safety block
ORDER_PATH = ["bharat-scanner", "bharat-entry", "bharat-exit", "bharat-timemachine", "bharat-budget",
              "bharat-insider", "bharat-risk", "bharat-broker", "bharat-alerts"]


def frontmatter(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return (m.group(1) if m else ""), text


def ignored_names():
    """What .gitignore keeps local, as root-level names (docs/ -> docs, .claude/settings.local.json -> .claude,
    .DS_Store), plus .git. Glob lines like *.pyc are not names."""
    with open(os.path.join(ROOT, ".gitignore")) as fh:
        lines = [x.strip() for x in fh if x.strip() and not x.startswith("#")]
    return {x.strip("/").split("/")[0] for x in lines if not any(c in x for c in "*?[")} | {".git"}


def every_text_file():
    skip = ignored_names()
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in skip]
        for f in files:
            if f.endswith((".md", ".py", ".json", ".csv", ".txt", ".xml")) or f == "LICENSE":
                yield os.path.join(base, f)


class Repo(unittest.TestCase):
    def test_eleven_skill_folders_at_the_root(self):
        found = sorted(d for d in os.listdir(ROOT)
                       if os.path.isfile(os.path.join(ROOT, d, "SKILL.md")))
        self.assertEqual(found, sorted(SKILLS))

    def test_root_matches_the_reel(self):
        root = set(d for d in os.listdir(ROOT) if d not in ignored_names() | {".gitignore"})
        required = set([".claude-plugin", "LICENSE", "README.md", "tests"] + SKILLS)
        self.assertLessEqual(required, root)
        self.assertLessEqual(root - required, {"tools", "india"})

    def test_local_only_files_stay_out_of_git(self):
        self.assertLessEqual({"docs", "trader", ".superpowers", ".DS_Store", ".claude"}, ignored_names())
        self.assertNotIn("tools", ignored_names())
        with open(os.path.join(ROOT, ".gitignore")) as fh:
            text = fh.read()
        for line in ("*.pyc", ".claude/settings.local.json"):
            self.assertIn(line, text.splitlines())

    def test_india_copies_identical(self):
        with open(os.path.join(ROOT, "india", "india.py"), "rb") as fh:
            master = fh.read()
        for s in SKILLS:
            path = os.path.join(ROOT, s, "india.py")
            self.assertTrue(os.path.isfile(path), "%s has no india.py: run tools/sync_india.py" % s)
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), master, "%s/india.py differs: run tools/sync_india.py" % s)

    def test_frontmatter_name_and_description(self):
        for s in SKILLS:
            fm, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            self.assertIn("name: %s\n" % s, fm + "\n", s)
            self.assertRegex(fm, r"description: ", s)
            self.assertGreater(len(fm), 200, "%s description is too thin" % s)
            self.assertGreater(len(text.splitlines()), 60, s)

    def test_order_path_skills_carry_the_safety_rule(self):
        for s in ORDER_PATH:
            _, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            low = " ".join(text.lower().split())
            self.assertIn("not financial advice", low, s)
            self.assertIn("yes", low, s)

    def test_plugin_manifest_points_at_the_root_skills(self):
        with open(os.path.join(ROOT, ".claude-plugin", "plugin.json")) as fh:
            plugin = json.load(fh)
        self.assertEqual(plugin["name"], "india-trader-skill")
        self.assertEqual(plugin["skills"], "./")
        self.assertEqual(plugin["description"], ABOUT)
        with open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")) as fh:
            market = json.load(fh)
        self.assertEqual(market["name"], "india-trader-skill")
        self.assertEqual(market["plugins"][0]["name"], "india-trader-skill")

    def test_readme_carries_the_pack(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = " ".join(fh.read().split())
        self.assertTrue(readme.startswith("# The India Trader skill"))
        for line in (ABOUT, "github.com/Jakeschincariol/day-trader-skill", "## Fine print", "SEBI",
                     "https://github.com/nawed1111/india-trader-skill install skill",
                     "/plugin marketplace add nawed1111/india-trader-skill",
                     "/plugin install india-trader-skill@india-trader-skill",
                     "not from a SEBI-registered adviser", "speculative business income", "static IP"):
            self.assertIn(line, readme)
        for sk in SKILLS:
            self.assertIn("/" + sk, readme, sk)

    def test_readme_example_numbers(self):
        """Every number of the worked example, pinned by the skills' own tests, is in the README."""
        import example_values as EX
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = fh.read()

        def forms(v):
            if isinstance(v, str):
                return {v, v.lstrip("0")}
            out = set()
            if isinstance(v, float) and v < 1:                  # TM odds are fractions; PCT_OVER is a percent
                out |= {"%.1f %%" % (100 * v), "%.1f%%" % (100 * v)}
            out |= {"%g" % v, "%.2f" % v, "{:,.2f}".format(v), "{:,}".format(v) if isinstance(v, int) else "%.2f" % v}
            out.add(india_inr(v))
            return out

        def india_inr(v):
            sys.path.insert(0, os.path.join(ROOT, "india"))
            import india
            return india.inr(v)[3:]

        for name in dir(EX):
            if name.isupper():
                v = getattr(EX, name)
                self.assertTrue(any(f in readme for f in forms(v)), "%s = %r is not in the README" % (name, v))

    def test_readme_options_words(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = fh.read()
        for words in ("buying only", "implied volatility", "held constant", "STT on its intrinsic value",
                      "max_option_cost_share", "1,800 units", "at a limit"):
            self.assertIn(words, readme)

    def test_no_em_dashes_and_no_personal_email(self):
        for path in every_text_file():
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            rel = os.path.relpath(path, ROOT)
            if not rel.startswith(os.path.join("tests", "fixtures")):  # fixtures are real data, kept as published
                self.assertNotIn("\u2014", text, rel)
            self.assertNotIn("jakeschincariol" + "@" + "gmail.com", text, rel)  # built, so this file passes too

    def test_every_tool_runs_on_python_3_8(self):
        # no 3.9+ only modules or syntax in the tools (the README promises 3.8)
        for s in SKILLS:
            folder = os.path.join(ROOT, s)
            for f in os.listdir(folder):
                if not f.endswith(".py"):
                    continue
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    src = fh.read()
                for bad in ("import zoneinfo", "from zoneinfo", ".removeprefix(", ".removesuffix(",
                            "match ", "list[", "dict[", "tuple["):
                    if bad == "match ":
                        self.assertNotRegex(src, r"(?m)^\s+match \w+.*:$", "%s/%s" % (s, f))
                    elif bad.endswith("["):
                        self.assertNotRegex(src, r"(?<![\w.])%s" % re.escape(bad), "%s/%s" % (s, f))
                    else:
                        self.assertNotIn(bad, src, "%s/%s" % (s, f))

    def test_day_trading_limits_ship_in_the_example_config(self):
        with open(os.path.join(ROOT, "bharat-budget", "config.example.json")) as fh:
            cfg = json.load(fh)
        for k in ("max_loss_per_trade_inr", "max_daily_loss_inr", "max_trades_per_day",
                  "min_reward_risk", "require_stop", "no_entries_after", "flat_by", "max_cost_share",
                  "mis_leverage"):
            self.assertIn(k, cfg, k)
        self.assertIs(cfg["require_stop"], True)
        self.assertIs(cfg.get("allow_short", False), False)
        self.assertIs(cfg.get("allow_margin", False), False)

    def test_paper_trading_is_the_default(self):
        with open(os.path.join(ROOT, "bharat-budget", "config.example.json")) as fh:
            cfg = json.load(fh)
        self.assertIs(cfg["paper"], True)
        self.assertIs(cfg.get("allow_fno", False), False)
        self.assertEqual(cfg["mis_leverage"], 1)


if __name__ == "__main__":
    unittest.main()
