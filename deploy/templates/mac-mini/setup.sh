#!/bin/bash
# Makes this Mac mini an agentos controller: Homebrew's uv, the Python environment in this checkout,
# the controller settings in AGENTOS_HOME (default ~/.agentos), and a LaunchAgent that keeps
# `agentos controller serve` running. Then it checks that the controller answers. Safe to run again.
#
#   deploy/templates/mac-mini/setup.sh           install or update, then check
#   deploy/templates/mac-mini/setup.sh --check   only check
#
# Run it in a logged-in session on the Mac: launchctl needs one. README.md says what runs here.
set -euo pipefail

LABEL=dev.agentos.controller
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"
home_dir="${AGENTOS_HOME:-$HOME/.agentos}"
logs="$HOME/Library/Logs/agentos"
plist="$HOME/Library/LaunchAgents/$LABEL.plist"
agentos="$repo/.venv/bin/agentos"
check_only=0

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
say() { printf '\n==> %s\n' "$*"; }

case "${1:-}" in
  "") ;;
  --check) check_only=1 ;;
  -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "usage: setup.sh [--check]" >&2; exit 2 ;;
esac
[ "$(uname -s)" = Darwin ] || die "this template is for macOS; Linux nodes use deploy/templates/hetzner or home-box"
grep -qs '^name = "agentos"' "$repo/pyproject.toml" || die "$repo is not an agentos checkout"

if [ "$check_only" = 0 ]; then
  say "Homebrew packages (Brewfile)"
  if command -v brew > /dev/null 2>&1; then
    brew bundle --file "$here/Brewfile"
  else
    echo "Homebrew is not installed; install uv yourself (https://docs.astral.sh/uv/)." >&2
  fi
  command -v uv > /dev/null 2>&1 || die "uv is not on PATH"

  say "Python environment in $repo (no browser stack: browser lanes are not on macOS yet)"
  (cd "$repo" && uv sync --frozen --no-dev)

  say "Controller settings in $home_dir"
  mkdir -p "$home_dir" "$logs" "$HOME/Library/LaunchAgents"
  chmod 700 "$home_dir"
  AGENTOS_HOME="$home_dir" "$agentos" controller init > /dev/null

  say "LaunchAgent $plist"
  "$repo/.venv/bin/python" "$here/render_plist.py" "$here/$LABEL.plist" "$plist" "$agentos" "$home_dir" "$logs"
  plutil -lint "$plist" > /dev/null
  launchctl bootout "gui/$(id -u)/$LABEL" > /dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$plist" \
    || die "launchctl could not load $plist (run this in a logged-in session on the Mac)"
fi

say "Self-check"
failed=0
pass() { printf '  PASS  %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; failed=1; }

if launchctl print "gui/$(id -u)/$LABEL" > /dev/null 2>&1; then
  pass "LaunchAgent $LABEL is loaded"
else
  fail "LaunchAgent $LABEL is not loaded (run setup.sh without --check)"
fi

# The token is read into a variable and sent through curl's config on stdin, never on a command line.
settings="$home_dir/controller/controller.toml"
port="$(sed -n 's/^port = \([0-9][0-9]*\)$/\1/p' "$settings" 2> /dev/null | head -n 1 || true)"
token="$(sed -n 's/^agent_token = "\(.*\)"$/\1/p' "$settings" 2> /dev/null | head -n 1 || true)"
if [ -z "$port" ] || [ -z "$token" ]; then
  fail "no controller settings in $settings (run setup.sh without --check)"
else
  url="http://localhost:$port/mcp"
  init='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"agentos-setup","version":"check"}}}'
  code=000
  for _ in $(seq 1 15); do
    code="$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
      --data "$init" "$url" || true)"
    [ "$code" != 000 ] && break
    sleep 1
  done
  if [ "$code" = 401 ]; then
    pass "$url answers 401 without the token"
  elif [ "$code" = 000 ]; then
    fail "nothing listens on port $port; the log is $logs/controller.log"
  else
    fail "$url answered HTTP $code without the token (expected 401)"
  fi
  authed="$(printf 'header = "Authorization: Bearer %s"\n' "$token" \
    | curl -s -o /dev/null -w '%{http_code}' -K - -X POST -H 'Content-Type: application/json' \
      -H 'Accept: application/json, text/event-stream' --data "$init" "$url" || true)"
  if [[ "$authed" =~ ^2[0-9][0-9]$ ]]; then
    pass "the agent token opens the MCP endpoint (HTTP $authed)"
  else
    fail "the agent token got HTTP $authed on initialize"
  fi
fi

if [ "$failed" = 1 ]; then
  say "The self-check failed. The controller log is $logs/controller.log"
  exit 1
fi
say "The controller is up"
status="$(fdesetup status 2> /dev/null || true)"
case "$status" in
  *"FileVault is On"*) echo "note: FileVault is on, so after a restart the controller starts only when someone logs in." ;;
esac
echo "Power settings: run deploy/templates/mac-mini/power.sh once (it asks for your password)."
echo "Connect Claude Code on this Mac: AGENTOS_HOME=$home_dir $agentos controller agent-token"
echo "(it prints the token: keep it private; set AGENTOS_HOME in your shell profile to skip the prefix)"
