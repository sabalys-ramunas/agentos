#!/bin/sh
# Install the agentos CLI on your computer (macOS or Linux), as you, without sudo:
#
#   curl -fsSL https://raw.githubusercontent.com/sabalys-ramunas/agentos/main/scripts/install.sh | sh
#
# It installs uv if you do not have it (into ~/.local/bin), then agentos as a uv tool from the
# git repository, puts both on your PATH, and starts `agentos setup` when a terminal is attached
# (otherwise it prints that one next step). Safe to run again: it updates agentos in place.
#
# Environment:
#   AGENTOS_GIT_URL   the repository to install from (default: AGENTOS_DEFAULT_GIT_URL below)
#   AGENTOS_GIT_REF   a branch or tag (default main)
#   AGENTOS_NO_SETUP  set to 1 to only install, never start agentos setup
set -eu

# The public repository. AGENTOS_GIT_URL installs from another one (a fork, a mirror, a private copy).
AGENTOS_DEFAULT_GIT_URL="https://github.com/sabalys-ramunas/agentos.git"

url="${AGENTOS_GIT_URL:-$AGENTOS_DEFAULT_GIT_URL}"
ref="${AGENTOS_GIT_REF:-main}"
bin="$HOME/.local/bin"

say() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" != 0 ] || say "Running as root: agentos goes into root's home. On a server, that is fine."
command -v git >/dev/null 2>&1 || die "git is not installed (macOS: xcode-select --install; Linux: your package manager)"

given_path="$PATH"
export PATH="$bin:$HOME/.cargo/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv (the Python tool manager) into $bin"
  command -v curl >/dev/null 2>&1 || die "curl is not installed"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
  command -v uv >/dev/null 2>&1 || die "uv did not install; see https://docs.astral.sh/uv/"
fi

say "Installing agentos from $url ($ref)"
uv tool install --force --python 3.12 "agentos @ git+$url@$ref"

# Puts ~/.local/bin on PATH in your shell's startup file, when it is not there yet.
uv tool update-shell >/dev/null 2>&1 || true
command -v agentos >/dev/null 2>&1 || die "agentos is installed but not on PATH; add $bin to PATH"
say "agentos $(agentos --version 2>/dev/null || echo installed) is at $(command -v agentos)"

if [ "${AGENTOS_NO_SETUP:-0}" != 1 ] && [ -t 1 ] && (: </dev/tty) 2>/dev/null; then
  say "Starting agentos setup"
  exec agentos setup </dev/tty
fi
echo
echo "Next step: agentos setup"
case ":$given_path:" in
  *":$bin:"*) ;;
  *) echo "(open a new terminal first, so $bin is on your PATH)" ;;
esac
