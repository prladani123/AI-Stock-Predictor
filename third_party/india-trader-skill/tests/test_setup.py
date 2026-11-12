"""tools/setup-trader.sh, run for real against stand-in git, go, launchctl, systemctl, claude and uname
(no network, no build, nothing installed: the real service manager and Claude Code are never called)."""
import json
import os
import plistlib
import shutil
import stat
import subprocess
import tempfile
import unittest

from _load import ROOT

SCRIPT = os.path.join(ROOT, "tools", "setup-trader.sh")
URL = "http://127.0.0.1:8080/mcp"

FAKE_GIT = """#!/bin/sh
# clone URL: make the folder a real clone would; anything else (pull) does nothing
if [ "$1" = clone ]; then
  dest=$(basename "$2")
  mkdir -p "$dest"
  case "$2" in *india-trader-skill*)
    mkdir -p "$dest/bharat-budget" "$dest/bharat-risk"
    echo '{"paper": true}' > "$dest/bharat-budget/config.example.json";;
  esac
fi
exit 0
"""
FAKE_GO = """#!/bin/sh
# go build -o NAME: leave an empty NAME behind
touch "$3"
"""
# stand-ins that only write down how they were called
LOGGER = """#!/bin/sh
echo "$(basename "$0") $*" >> "$CALLS"
"""


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class SetupScript(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.bin = os.path.join(self.d, "bin")
        os.mkdir(self.bin)
        for name, body in (("git", FAKE_GIT), ("go", FAKE_GO), ("launchctl", LOGGER),
                           ("systemctl", LOGGER), ("claude", LOGGER)):
            self.fake(name, body)
        self.home = os.path.join(self.d, "home")
        self.ssd = os.path.join(self.d, "MySSD")
        os.mkdir(self.ssd)
        self.target = os.path.join(self.ssd, "trader")
        self.exe = os.path.join(self.target, "kite-mcp-server", "kite-mcp-server")
        self.cfg = os.path.join(self.home, "Claude", "claude_desktop_config.json")
        self.keys = os.path.join(self.home, ".config", "kite-mcp-server", "env")
        self.calls = os.path.join(self.d, "calls")

    def tearDown(self):
        shutil.rmtree(self.d)

    def fake(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, 0o755)

    def run_script(self, *args, system="Darwin"):
        self.fake("uname", "#!/bin/sh\necho %s\n" % system)
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ.get("PATH", ""), HOME=self.home,
                   CLAUDE_DESKTOP_CONFIG=self.cfg, INDIA_TRADER_SKIP_TESTS="1", CALLS=self.calls)
        return subprocess.run(["bash", SCRIPT] + list(args), env=env, capture_output=True, text=True)

    def called(self):
        with open(self.calls) as fh:
            return fh.read().splitlines()

    def key_file(self):
        with open(self.keys) as fh:
            return fh.read().splitlines()

    def ask_rules(self):
        with open(os.path.join(self.target, ".claude", "settings.json")) as fh:
            return json.load(fh)["permissions"]["ask"]

    def test_sets_up_the_folder_the_skills_and_one_shared_kite_server(self):
        r = self.run_script(self.target)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.stat(self.exe).st_mode & stat.S_IXUSR)
        self.assertTrue(os.path.isdir(os.path.join(self.target, ".claude", "skills", "bharat-risk")))
        with open(os.path.join(self.target, "trader", "config.json")) as fh:
            self.assertIs(json.load(fh)["paper"], True)
        # placeholder keys and the order tools off, in a file only you can read
        self.assertEqual(stat.S_IMODE(os.stat(self.keys).st_mode), 0o600)
        lines = self.key_file()
        for line in ("KITE_API_KEY=PASTE_KEY_LATER", "KITE_API_SECRET=PASTE_SECRET_LATER"):
            self.assertIn(line, lines)
        # every order tool that asks in Claude is also off at the server, GTT orders included
        excluded = set(next(x for x in lines if x.startswith("EXCLUDED_TOOLS=")).split("=", 1)[1].split(","))
        self.assertLessEqual({"place_order", "modify_order", "cancel_order", "place_gtt_order"}, excluded)
        self.assertEqual({t.replace("mcp__kite__", "") for t in self.ask_rules()} - excluded, set())
        # one copy, HTTP mode on loopback, started at login and restarted if it stops
        plist_path = os.path.join(self.home, "Library", "LaunchAgents", "local.kite-mcp-server.plist")
        with open(plist_path, "rb") as fh:
            plist = plistlib.load(fh)
        self.assertEqual(plist["EnvironmentVariables"],
                         {"APP_MODE": "http", "APP_HOST": "127.0.0.1", "APP_PORT": "8080"})
        self.assertEqual(plist["ProgramArguments"][-2:], [self.keys, self.exe])
        self.assertIs(plist["KeepAlive"], True)
        calls = self.called()
        self.assertIn("launchctl bootstrap gui/%d %s" % (os.getuid(), plist_path), calls)
        # Claude Code connects to it; order tools always ask
        self.assertIn("claude mcp add -s local --transport http kite " + URL, calls)
        self.assertIn("mcp__kite__place_order", self.ask_rules())
        self.assertIn("mcp__kite__cancel_order", self.ask_rules())
        self.assertIn("127.0.0.1:8080/callback", r.stdout)
        self.assertFalse(os.path.exists(self.cfg))  # no Claude Desktop config made

    def test_moves_old_desktop_keys_over_takes_kite_out_and_keeps_the_rest(self):
        os.makedirs(os.path.dirname(self.cfg))
        # the old manual setup had no EXCLUDED_TOOLS: that is not a choice to go live
        old_kite = {"command": "/old/kite-mcp-server", "env": {
            "APP_MODE": "stdio", "KITE_API_KEY": "typed-key", "KITE_API_SECRET": "typed-secret"}}
        with open(self.cfg, "w") as fh:
            json.dump({"mcpServers": {"kite": old_kite, "other": {"command": "x"}}, "theme": "dark"}, fh)
        os.chmod(self.cfg, 0o644)
        older = self.cfg + ".backup-20261101-090000"          # left by an earlier run, keys and all
        shutil.copy(self.cfg, older)
        os.chmod(older, 0o644)
        os.makedirs(os.path.join(self.target, ".claude"))
        with open(os.path.join(self.target, ".claude", "settings.json"), "w") as fh:
            json.dump({"model": "opus", "permissions": {"allow": ["Bash(ls)"]}}, fh)
        r = self.run_script(self.target)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        lines = self.key_file()
        self.assertIn("KITE_API_KEY=typed-key", lines)
        self.assertIn("KITE_API_SECRET=typed-secret", lines)
        # moved keys come over with the order tools off, and the script says how to go live again
        self.assertTrue([x for x in lines if x.startswith("EXCLUDED_TOOLS=place_order")], lines)
        self.assertIn("delete the EXCLUDED_TOOLS line", r.stdout)
        with open(self.cfg) as fh:
            doc = json.load(fh)
        self.assertNotIn("kite", doc["mcpServers"])
        self.assertEqual(doc["mcpServers"]["other"], {"command": "x"})
        self.assertEqual(doc["theme"], "dark")
        backups = [os.path.join(os.path.dirname(self.cfg), f) for f in os.listdir(os.path.dirname(self.cfg))
                   if ".backup-" in f]
        self.assertEqual(len(backups), 2)
        for b in backups:                                       # they hold the old keys: only you can read them
            self.assertEqual(stat.S_IMODE(os.stat(b).st_mode), 0o600, b)
        self.assertIn("holds your old keys", r.stdout)
        with open(os.path.join(self.target, ".claude", "settings.json")) as fh:
            settings = json.load(fh)
        self.assertEqual((settings["model"], settings["permissions"]["allow"]), ("opus", ["Bash(ls)"]))
        # a second run keeps the key file you edited and adds no duplicate rules. An older line without
        # the GTT tools gains them: a line there means the order tools are meant to be off, and GTTs are orders
        keep = [x for x in lines if not x.startswith("EXCLUDED_TOOLS")]
        with open(self.keys, "w") as fh:
            fh.write("\n".join(keep + ["EXCLUDED_TOOLS=place_order,modify_order,cancel_order"]) + "\n")
        r = self.run_script(self.target)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.key_file(), keep + ["EXCLUDED_TOOLS=place_order,modify_order,cancel_order,"
                                                  "place_gtt_order,modify_gtt_order,delete_gtt_order"])
        self.assertEqual(stat.S_IMODE(os.stat(self.keys).st_mode), 0o600)
        self.assertIn("GTT", r.stdout)
        self.assertEqual(self.ask_rules().count("mcp__kite__place_order"), 1)
        # spaces after the commas are fine (the server trims them): nothing to add, the file is not touched
        spaced = "EXCLUDED_TOOLS=place_order, modify_order, cancel_order, place_gtt_order, modify_gtt_order, " \
                 "delete_gtt_order"
        with open(self.keys, "w") as fh:
            fh.write("\n".join(keep + [spaced]) + "\n")
        r = self.run_script(self.target)
        self.assertEqual(self.key_file(), keep + [spaced])
        self.assertNotIn("EXCLUDED_TOOLS", r.stdout)
        # no line at all is a choice to go live: kept as it is
        with open(self.keys, "w") as fh:
            fh.write("\n".join(keep) + "\n")
        self.run_script(self.target)
        self.assertEqual(self.key_file(), keep)

    def test_linux_runs_it_as_a_systemd_user_service(self):
        r = self.run_script(self.target, system="Linux")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(os.path.join(self.home, ".config", "systemd", "user", "kite-mcp-server.service")) as fh:
            unit = fh.read().splitlines()
        for line in ("Environment=APP_MODE=http", "Environment=APP_HOST=127.0.0.1",
                     "EnvironmentFile=" + self.keys, 'ExecStart="%s"' % self.exe, "Restart=always"):
            self.assertIn(line, unit)
        self.assertIn("systemctl --user restart kite-mcp-server", self.called())
        self.assertIn("systemctl --user restart kite-mcp-server", r.stdout)

    def test_an_unplugged_drive_stops_before_making_anything(self):
        gone = os.path.join(self.d, "NotPlugged", "trader")
        r = self.run_script(gone)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ls /Volumes", r.stdout + r.stderr)
        self.assertFalse(os.path.exists(os.path.dirname(gone)))
        self.assertFalse(os.path.exists(self.keys))
        self.assertFalse(os.path.exists(self.calls))

    def test_no_folder_given_prints_usage(self):
        r = self.run_script()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Usage", r.stdout + r.stderr)

    def test_the_readme_and_broker_skill_point_at_it(self):
        for path in ("README.md", os.path.join("bharat-broker", "SKILL.md")):
            with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
                text = fh.read()
            for words in ("tools/setup-trader.sh", "127.0.0.1:8080/callback", "~/.config/kite-mcp-server/env"):
                self.assertTrue(words in text, "%s: %s" % (path, words))
            self.assertFalse("add-from-claude-desktop" in text, path)  # the old stdio copy cannot log in


if __name__ == "__main__":
    unittest.main()
