#!/usr/bin/env bash
# Copy this checkout to a directory on the node.
#
#   scripts/sync-node.sh "$AGENTOS_DEV_ROOT/mcp"
#   scripts/sync-node.sh --web "$AGENTOS_DEV_ROOT/mcp"
#   scripts/sync-node.sh --watch "$AGENTOS_DEV_ROOT/mcp" --restart 'systemctl restart agentosd-dev'
#
# AGENTOS_NODE names the node and AGENTOS_DEV_ROOT its dev directory (scripts/node-env.sh reads
# them). Only paths under AGENTOS_DEV_ROOT are allowed unless SYNC_ANY_DIR=1, so a typo can
# never overwrite a production install.
# The remote .venv and the dev node data in .agentos-home survive the sync.
#
# --web rebuilds web/dist here when the web sources changed since its last good build, before
# the copy (deploy/web-build.sh). A failed build keeps the last good one and still syncs.
# Without it a stale web/dist is copied as it is, and the sync says so.
#
# --watch keeps syncing on save (it checks for changed files every SYNC_POLL_S seconds,
# default 1, and reuses one ssh connection). After a sync it runs `uv sync` on the node
# when pyproject.toml or uv.lock changed, and the --restart command (in the remote dir)
# only when Python code or dependencies changed. With --web, a change under web/
# rebuilds web/dist first. A deleted file is synced with the next change. Ctrl-C stops watching.
set -euo pipefail

usage() { sed -n '2,/^set -euo/{/^set -euo/!p}' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

WATCH=0
WEB=0
RESTART=""
REMOTE_DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --watch) WATCH=1; shift ;;
    --web) WEB=1; shift ;;
    --restart) [ $# -ge 2 ] || { echo "error: --restart needs a command" >&2; exit 2; }; RESTART="$2"; shift 2 ;;
    --restart=*) RESTART="${1#--restart=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "error: unknown option $1 (see --help)" >&2; exit 2 ;;
    *) [ -z "$REMOTE_DIR" ] || { echo "error: one remote dir only" >&2; exit 2; }; REMOTE_DIR="$1"; shift ;;
  esac
done
[ -n "$REMOTE_DIR" ] || { echo "usage: scripts/sync-node.sh [--web] [--watch [--restart CMD]] <remote-dir>" >&2; exit 2; }
if [ "$WATCH" = 0 ] && [ -n "$RESTART" ]; then
  echo "error: --restart goes with --watch" >&2; exit 2
fi
# shellcheck source=scripts/node-env.sh
. "$(dirname "${BASH_SOURCE[0]}")/node-env.sh"
REMOTE_DIR="${REMOTE_DIR%/}"
# Lexical safety first: no "..", "." or empty segments, so a path cannot look like it is under
# AGENTOS_DEV_ROOT while resolving somewhere else (for example the PoC at /root/agentos).
case "$REMOTE_DIR/" in
  */../*|*/./*|*//*) echo "error: refusing $REMOTE_DIR (no '..', '.' or empty path segments)" >&2; exit 2 ;;
esac
[[ "$REMOTE_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]] || { echo "error: refusing $REMOTE_DIR (unexpected characters)" >&2; exit 2; }

case "$REMOTE_DIR" in
  "$AGENTOS_DEV_ROOT"/?*) ;;
  *)
    if [ "${SYNC_ANY_DIR:-0}" != 1 ]; then
      echo "error: refusing $REMOTE_DIR; use a dir under $AGENTOS_DEV_ROOT/ (or SYNC_ANY_DIR=1)" >&2
      exit 2
    fi
    ;;
esac
case "$REMOTE_DIR" in
  /root/agentos|/root/agentos/*) echo "error: never sync into the PoC at /root/agentos" >&2; exit 2 ;;
esac
# A restart command runs as root on the node: it must not touch the production service, its
# data, or the PoC. Dev units have their own names (agentosd-dev, agentosd-qa, ...).
if [ -n "$RESTART" ]; then
  if [[ "$RESTART" =~ (^|[^A-Za-z0-9_.-])agentosd(\.service)?([^A-Za-z0-9_-]|$) ]] \
     || [[ "$RESTART" == */var/lib/agentos* ]] || [[ "$RESTART" =~ /root/agentos(/|$|[^A-Za-z0-9_-]) ]]; then
    echo "error: refusing a restart command that touches the production agentosd, its data or the PoC" >&2
    exit 2
  fi
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$SRC_DIR/deploy/web-build.sh"  # web_is_stale and web_rebuild
SSH_CMD=(ssh)
RSYNC_E="ssh"
if [ "$WATCH" = 1 ]; then
  # one connection for every round, so a save syncs in well under a second (a short socket
  # path: macOS allows 104 characters)
  SSH_CMD=(ssh -o ControlMaster=auto -o "ControlPath=/tmp/agentos-sync-%C" -o ControlPersist=120)
  RSYNC_E="ssh -o ControlMaster=auto -o ControlPath=/tmp/agentos-sync-%C -o ControlPersist=120"
fi

# --web: rebuild web/dist when the web sources changed since its last good build. Needs the web's
# node_modules (pnpm install is not run here) and npx. A failed build keeps the last good build.
ensure_web_build() {
  local dir="$SRC_DIR/web"
  [ -f "$dir/package.json" ] || return 0
  web_is_stale "$dir" || return 0
  if [ ! -d "$dir/node_modules" ]; then
    echo "  web/ has no node_modules here, so it is not rebuilt: run pnpm install in web/ first" >&2
    return 0
  fi
  if ! command -v npx >/dev/null 2>&1; then
    echo "  npx is missing here, so web/ is not rebuilt" >&2
    return 0
  fi
  echo "web changed: building web/ (the last good build stays if this fails)"
  web_rebuild "$dir" npx --yes pnpm@10.33.0 build \
    || echo "the web build failed; synced the rest with the last good build" >&2
  return 0
}

sync_once() {
  rsync -az --delete --no-owner --no-group "$@" -e "$RSYNC_E" \
    --rsync-path="mkdir -p '$REMOTE_DIR' && rsync" \
    --exclude /.venv/ --exclude .git --exclude __pycache__ --exclude '*.pyc' \
    --exclude .pytest_cache --exclude .ruff_cache --exclude .DS_Store \
    --exclude /poc/shots/ --exclude /poc/profiles/ --exclude /poc/fingerprints/ --exclude /poc/secrets.json \
    --exclude /.agentos-home/ \
    --exclude /web/node_modules/ --exclude /web/test-results/ --exclude /web/screenshots/ --exclude '/web/.dev-node-*' \
    --exclude /panel/ \
    --exclude /redteam/site/ --exclude /redteam/detect/.venv/ \
    "$SRC_DIR/" "$NODE_HOST:$REMOTE_DIR/"
}

if [ "$WATCH" = 0 ]; then
  if [ "$WEB" = 1 ]; then
    ensure_web_build
  elif [ -f "$SRC_DIR/web/package.json" ] && web_is_stale "$SRC_DIR/web"; then
    echo "note: web/ changed since its last build, so this sync sends the old build. --web rebuilds it first" >&2
  fi
  sync_once
  echo "synced $SRC_DIR to $NODE_HOST:$REMOTE_DIR"
  exit 0
fi

# uv lives in ~/.local/bin, which a non-interactive ssh shell does not put on PATH
remote() { "${SSH_CMD[@]}" "$NODE_HOST" "export PATH=\"\$HOME/.local/bin:\$PATH\"; cd '$REMOTE_DIR' && $1"; }

round() {
  local changed
  changed="$(sync_once --out-format='%n')" || { echo "sync failed; retrying on the next change" >&2; return 0; }
  local n
  n="$(printf '%s' "$changed" | grep -c . || true)"
  echo "$(date +%H:%M:%S) synced $n change(s) to $NODE_HOST:$REMOTE_DIR"
  local deps=0 code=0
  grep -qE '^(deleting )?(pyproject\.toml|uv\.lock)$' <<<"$changed" && deps=1
  grep -qE '^(deleting )?src/.*\.(py|html|js|css|json|toml)$' <<<"$changed" && code=1
  if [ "$deps" = 1 ]; then
    echo "  dependencies changed: uv sync on the node"
    remote "uv sync --frozen --extra node" || echo "  uv sync failed on the node" >&2
  fi
  if [ -n "$RESTART" ] && { [ "$code" = 1 ] || [ "$deps" = 1 ]; }; then
    echo "  restarting: $RESTART"
    remote "$RESTART" || echo "  the restart command failed" >&2
  fi
}

stamp="$(mktemp "${TMPDIR:-/tmp}/agentos-sync.XXXXXX")"
trap 'rm -f "$stamp"' EXIT
trap 'echo; echo "stopped watching"; exit 0' INT TERM
if [ "$WEB" = 1 ]; then ensure_web_build; fi
round
rounds=1
watch_done() { [ -n "${SYNC_WATCH_ROUNDS:-}" ] && [ "$rounds" -ge "$SYNC_WATCH_ROUNDS" ]; }
watch_done && exit 0
echo "watching $SRC_DIR (Ctrl-C to stop)"
while :; do
  sleep "${SYNC_POLL_S:-1}"
  newer="$(find "$SRC_DIR" \( -name .git -o -name .venv -o -name node_modules -o -name __pycache__ \
      -o -name .pytest_cache -o -name .ruff_cache -o -name .agentos-home -o -path "$SRC_DIR/panel" \
      -o -path "$SRC_DIR/web/dist" -o -path "$SRC_DIR/web/dist.previous" \
      \) -prune \
      -o -newer "$stamp" -print 2>/dev/null | head -200)"
  [ -n "$newer" ] || continue
  touch "$stamp"   # before syncing: an edit made during this round is caught by the next one
  if [ "$WEB" = 1 ] && grep -q "^$SRC_DIR/web/" <<<"$newer"; then
    ensure_web_build
  fi
  round
  rounds=$((rounds + 1))
  watch_done && break
done
