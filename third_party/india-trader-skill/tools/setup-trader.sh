#!/bin/bash
# Set up the India Trader skills and Zerodha's Kite MCP server in one folder.
# An external SSD works: the folder can live anywhere. macOS or Linux.
#
#   bash setup-trader.sh /Volumes/MySSD/trader
#
# It makes the folder, clones and builds zerodha/kite-mcp-server, installs the
# 11 skills for that folder, writes a paper-trading config, and runs ONE shared
# Kite server in the background (HTTP mode on 127.0.0.1:8080, started at login)
# with placeholder keys and the order tools switched off. Claude Code connects
# to it, and the order tools always ask before they run. Safe to run again:
# nothing is cloned twice and your keys are kept (also the ones you typed into
# Claude Desktop's config for the old setup, which this moves over).
#
# Why one shared server: the server's stdio mode cannot log in (it rejects the
# fixed "stdio" session ID), and every stdio copy fights over port 8080, where
# Zerodha sends you back after you sign in.
#
# Your Kite key and secret go in ~/.config/kite-mcp-server/env afterwards,
# typed by you. Never in this repo, never in a chat.
set -euo pipefail

usage() { echo "Usage: bash setup-trader.sh /path/to/folder   (for example /Volumes/MySSD/trader)"; }
[ $# -eq 1 ] || { usage; exit 2; }
W="${1%/}"
PARENT="$(dirname "$W")"
# an unplugged SSD: mkdir -p would quietly make /Volumes/<name> on the internal disk
[ -d "$PARENT" ] || { echo "No folder at $PARENT. If it is an SSD, plug it in and check its name with: ls /Volumes"; exit 1; }

command -v git >/dev/null     || { echo "git is missing. macOS: xcode-select --install"; exit 1; }
command -v go >/dev/null      || { echo "Go is missing (1.21 or newer). macOS: brew install go, or go.dev/dl"; exit 1; }
command -v python3 >/dev/null || { echo "python3 is missing. macOS: xcode-select --install"; exit 1; }

LABEL="local.kite-mcp-server"
case "$(uname -s)" in
  Darwin)
    OS=mac
    DEFAULT_CFG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
    SERVICE="$HOME/Library/LaunchAgents/$LABEL.plist"
    RESTART="launchctl kickstart -k gui/\$(id -u)/$LABEL" ;;
  Linux)
    OS=linux
    DEFAULT_CFG="$HOME/.config/Claude/claude_desktop_config.json"
    SERVICE="$HOME/.config/systemd/user/kite-mcp-server.service"
    RESTART="systemctl --user restart kite-mcp-server" ;;
  *) echo "Not macOS or Linux: follow the manual steps in the README (Live trading setup)"; exit 1 ;;
esac
CFG="${CLAUDE_DESKTOP_CONFIG:-$DEFAULT_CFG}"
KITE_ENV="$HOME/.config/kite-mcp-server/env"
KITE_URL="http://127.0.0.1:8080/mcp"

echo "== 1. Folder: $W"
mkdir -p "$W/.claude/skills" "$W/trader"
cd "$W"

echo "== 2. Zerodha's Kite MCP server: clone and build"
[ -d kite-mcp-server ] || git clone https://github.com/zerodha/kite-mcp-server
(cd kite-mcp-server && go build -o kite-mcp-server)
chmod +x kite-mcp-server/kite-mcp-server   # exFAT drives can drop the run permission

echo "== 3. The India Trader skills, installed for this folder"
if [ -d india-trader-skill ]; then
  git -C india-trader-skill pull -q --ff-only || echo "   (could not update india-trader-skill; using the copy you have)"
else
  git clone https://github.com/nawed1111/india-trader-skill
fi
cp -R india-trader-skill/bharat-* .claude/skills/
[ -f trader/config.json ] || cp india-trader-skill/bharat-budget/config.example.json trader/config.json

if [ -z "${INDIA_TRADER_SKIP_TESTS:-}" ]; then
  echo "== 4. Skill tests (offline, about 30 seconds)"
  (cd india-trader-skill && python3 -m unittest discover -s tests 2>&1 | tail -1) \
    || echo "   Tests did not pass: the lines above say which"
fi

echo "== 5. The Kite server: one shared copy in the background"
if [ -f "$CFG" ] && grep -q '"kite"' "$CFG"; then
  BACKUP="$CFG.backup-$(date +%Y%m%d-%H%M%S)"
  (umask 077 && cp "$CFG" "$BACKUP")
  echo "   backup: $BACKUP (it holds your old keys: only you can read it; delete it once this setup works)"
fi
for b in "$CFG".backup-*; do   # backups from earlier runs hold the keys too
  if [ -f "$b" ]; then chmod 600 "$b"; fi
done
OS="$OS" CFG="$CFG" W="$W" KITE_ENV="$KITE_ENV" SERVICE="$SERVICE" LABEL="$LABEL" RESTART="$RESTART" \
KITE_BIN="$W/kite-mcp-server/kite-mcp-server" python3 - <<'PY'
import json, os, plistlib

E = os.environ
HOME = os.path.expanduser("~")
# every tool that places, changes or cancels an order, a GTT included: off at the server, and asking in Claude
ORDER_TOOL_NAMES = ("place_order", "modify_order", "cancel_order", "place_gtt_order", "modify_gtt_order",
                    "delete_gtt_order")
ORDER_TOOLS = ",".join(ORDER_TOOL_NAMES)
NEEDS_APPROVAL = ["mcp__kite__" + t for t in ORDER_TOOL_NAMES]


def missing_order_tools(excluded):
    """The order tools an EXCLUDED_TOOLS value leaves on. The server trims the spaces around each name."""
    have = {t.strip() for t in (excluded or "").split(",")}
    return [t for t in ORDER_TOOL_NAMES if t not in have]


def with_order_tools(excluded):
    """An EXCLUDED_TOOLS value plus any order tool it leaves out."""
    kept = (excluded or "").strip().rstrip(",")
    return ",".join(([kept] if kept else []) + missing_order_tools(excluded))


def write_key_file(lines):
    os.makedirs(os.path.dirname(E["KITE_ENV"]), mode=0o700, exist_ok=True)
    fd = os.open(E["KITE_ENV"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(E["KITE_ENV"], 0o600)


def load_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def save_json(path, doc):
    with open(path, "w") as fh:
        json.dump(doc, fh, indent=2)


def read_env(path):
    out = {}
    with open(path) as fh:
        for line in fh:
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#"):
                out[k] = v
    return out


def real(env):
    key = (env or {}).get("KITE_API_KEY", "")
    return bool(key) and not key.startswith("PASTE_") and bool(env.get("KITE_API_SECRET"))


def kite_env(doc):
    return ((doc.get("mcpServers") or {}).get("kite") or {}).get("env") or {}


# Keys typed for the old stdio setup: Claude Desktop's config, Claude Code's copy of it, an earlier service file
desktop = load_json(E["CFG"])
old = [kite_env(desktop), kite_env((load_json(os.path.join(HOME, ".claude.json")).get("projects") or {}).get(E["W"]) or {})]
if E["OS"] == "mac" and os.path.exists(E["SERVICE"]):
    with open(E["SERVICE"], "rb") as fh:
        old.insert(0, plistlib.load(fh).get("EnvironmentVariables") or {})

# The key file: only you can read it. Kept once your keys are in, except that an EXCLUDED_TOOLS line
# gains any order tool it leaves on (a line there means the order tools are meant to be off).
have = read_env(E["KITE_ENV"]) if os.path.exists(E["KITE_ENV"]) else None
moved = next((e for e in old if real(e)), None)
if have is None or (not real(have) and moved):
    keys = moved or {"KITE_API_KEY": "PASTE_KEY_LATER", "KITE_API_SECRET": "PASTE_SECRET_LATER"}
    lines = ["# Your Kite Connect key and secret, for the shared Kite MCP server. Only you can read this file.",
             "# Never put them in a repo or a chat. After any change, restart the server:",
             "#   " + E["RESTART"],
             "# Delete the EXCLUDED_TOOLS line only when you go live (see /bharat-broker).",
             "KITE_API_KEY=" + keys["KITE_API_KEY"], "KITE_API_SECRET=" + keys["KITE_API_SECRET"],
             # always, moved keys too: an old config without the line never chose to go live
             "EXCLUDED_TOOLS=" + with_order_tools(keys.get("EXCLUDED_TOOLS"))]
    write_key_file(lines)
    print("   key file: " + E["KITE_ENV"] + (" (your keys, moved from the old setup)" if moved else " (placeholder keys)"))
    if moved:
        print("   the order tools are OFF. If you were trading live, delete the EXCLUDED_TOOLS line in that file,")
        print("   then restart the server: " + E["RESTART"])
else:
    print("   key file kept: " + E["KITE_ENV"])
    add = missing_order_tools(have["EXCLUDED_TOOLS"]) if have.get("EXCLUDED_TOOLS", "").strip() else []
    if add:   # an older line, from before the GTT tools were in it: GTT orders are orders too
        with open(E["KITE_ENV"]) as fh:
            text = fh.read().splitlines()
        write_key_file([("EXCLUDED_TOOLS=" + with_order_tools(l.strip().partition("=")[2])
                         if l.strip().partition("=")[0] == "EXCLUDED_TOOLS" else l) for l in text])
        print("   its EXCLUDED_TOOLS line now switches off every order tool, GTT orders included (added "
              + ", ".join(add) + ")")

# The service: started at login, restarted if it stops (it exits while another copy still holds port 8080)
exe = E["KITE_BIN"]
app = {"APP_MODE": "http", "APP_HOST": "127.0.0.1", "APP_PORT": "8080"}  # loopback only
os.makedirs(os.path.dirname(E["SERVICE"]), exist_ok=True)
if E["OS"] == "mac":
    log = os.path.join(HOME, "Library", "Logs", "kite-mcp-server.log")
    with open(E["SERVICE"], "wb") as fh:
        plistlib.dump({
            "Label": E["LABEL"],
            "ProgramArguments": ["/bin/sh", "-c", 'set -a; . "$0"; exec "$1"', E["KITE_ENV"], exe],
            "WorkingDirectory": os.path.dirname(exe),
            "EnvironmentVariables": app,
            "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
            "StandardOutPath": log, "StandardErrorPath": log,
        }, fh)
else:
    with open(E["SERVICE"], "w") as fh:
        fh.write("[Unit]\nDescription=Kite MCP server, shared by every Claude session\n\n[Service]\n"
                 + "".join("Environment=%s=%s\n" % kv for kv in app.items())
                 + 'EnvironmentFile=%s\nExecStart="%s"\nRestart=always\nRestartSec=10\n\n' % (E["KITE_ENV"], exe)
                 + "[Install]\nWantedBy=default.target\n")
print("   service: " + E["SERVICE"])

# The old stdio copy in Claude Desktop's config cannot log in and holds port 8080: take it out (backed up above)
if "kite" in (desktop.get("mcpServers") or {}):
    del desktop["mcpServers"]["kite"]
    save_json(E["CFG"], desktop)
    print("   removed the old stdio kite from Claude Desktop's config (backup next to it)")

# Order tools ask every time ("Needs approval"); ask rules win over allow rules
settings_path = os.path.join(E["W"], ".claude", "settings.json")
settings = load_json(settings_path)
ask = settings.setdefault("permissions", {}).setdefault("ask", [])
ask += [t for t in NEEDS_APPROVAL if t not in ask]
save_json(settings_path, settings)
print("   order tools set to ask before every call: " + settings_path)
PY
case "$OS" in
  mac)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    # right after a bootout, launchd can refuse once while it lets go
    launchctl bootstrap "gui/$(id -u)" "$SERVICE" 2>/dev/null || { sleep 2; launchctl bootstrap "gui/$(id -u)" "$SERVICE"; } ;;
  linux)
    systemctl --user daemon-reload
    systemctl --user enable kite-mcp-server >/dev/null 2>&1
    systemctl --user restart kite-mcp-server ;;
esac

echo "== 6. Claude Code: connect to the shared server"
if command -v claude >/dev/null; then
  claude mcp remove kite -s local >/dev/null 2>&1 || true   # an old stdio copy, keys and all
  claude mcp add -s local --transport http kite "$KITE_URL" >/dev/null
  echo "   kite: $KITE_URL, for Claude Code in $W"
else
  echo "   Claude Code is not installed yet. Once it is, run this in $W:"
  echo "      claude mcp add -s local --transport http kite $KITE_URL"
fi

echo
echo "Done. Next:"
echo " 1. Your Kite Connect app at developers.kite.trade: set its Redirect URL to http://127.0.0.1:8080/callback"
echo " 2. Type its key and secret in $KITE_ENV, then restart the server:"
echo "      $RESTART"
echo " 3. Quit Claude completely and reopen it, so no old Kite copy holds port 8080"
echo " 4. Use the skills: cd \"$W\" && claude, then /bharat-scanner. Ask for your Kite profile to log in."
