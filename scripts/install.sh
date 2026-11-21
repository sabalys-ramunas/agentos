#!/bin/sh
# Install the openajar CLI on your computer (macOS or Linux), as you, without sudo:
#
#   curl -fsSL https://raw.githubusercontent.com/sabalys-ramunas/openajar/main/scripts/install.sh | sh
#
# It installs uv if you do not have it (into ~/.local/bin), then the newest openajar release as a uv
# tool from the git repository with its `local` extra (Playwright, which drives each environment's browser on its
# VM; no browser is downloaded to this computer), puts both on your PATH, and starts `openajar setup` when a terminal is attached
# (otherwise it prints that one next step). Safe to run again: it updates openajar in place.
#
# The web app (http://127.0.0.1:7410/) is optional: setup offers it (default yes) and fetches the
# release's prebuilt copy. When setup does not start, this script runs `openajar web update` instead,
# which installs or updates it to match, unless you said no before or set OPENAJAR_NO_WEB=1.
#
# Environment (each also read under its old name, AGENTOS_..., when the new one is not set): (legacy-name)
#   OPENAJAR_GIT_URL   the repository to install from (default: OPENAJAR_DEFAULT_GIT_URL below)
#   OPENAJAR_GIT_REF   a branch or tag (default: the newest release tag, vX.Y.Z; main when there is none)
#   OPENAJAR_NO_SETUP  set to 1 to only install, never start openajar setup
#   OPENAJAR_NO_WEB    set to 1 to leave the web app out when setup does not start
set -eu

# The public repository. OPENAJAR_GIT_URL installs from another one (a fork, a mirror, a private copy).
OPENAJAR_DEFAULT_GIT_URL="https://github.com/sabalys-ramunas/openajar.git"

url="${OPENAJAR_GIT_URL:-${AGENTOS_GIT_URL:-$OPENAJAR_DEFAULT_GIT_URL}}"  # legacy-name: AGENTOS_GIT_URL
ref="${OPENAJAR_GIT_REF:-${AGENTOS_GIT_REF:-}}"  # legacy-name: AGENTOS_GIT_REF
no_setup="${OPENAJAR_NO_SETUP:-${AGENTOS_NO_SETUP:-0}}"  # legacy-name: AGENTOS_NO_SETUP
bin="$HOME/.local/bin"

say() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" != 0 ] || say "Running as root: openajar goes into root's home. On a server, that is fine."
command -v git >/dev/null 2>&1 || die "git is not installed (macOS: xcode-select --install; Linux: your package manager)"

given_path="$PATH"
export PATH="$bin:$HOME/.cargo/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv (the Python tool manager) into $bin"
  command -v curl >/dev/null 2>&1 || die "curl is not installed"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
  command -v uv >/dev/null 2>&1 || die "uv did not install; see https://docs.astral.sh/uv/"
fi

# The newest release (the highest vX.Y.Z tag), unless a ref was given; main when there is no release.
if [ -z "$ref" ]; then
  ref=$(git ls-remote --tags --refs "$url" 'v*' 2>/dev/null | sed 's#.*refs/tags/##' \
    | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sed 's/^v//' | sort -t. -k1,1n -k2,2n -k3,3n | tail -n 1) || true
  if [ -n "$ref" ]; then ref="v$ref"; else ref=main; fi
fi

# Releases up to 0.4.0 are the package and command `agentos`; every later one is `openajar`, which also (legacy-name)
# brings the old command as an alias. A branch made before the rename is tried under the old name too.
pkg=openajar
case "$ref" in v0.[0-4].*) pkg=agentos ;; esac  # legacy-name: the package's name up to 0.4.0
cmd=$pkg

# legacy-name: an agentos tool installed before the rename goes first; the new one brings that command
# too. Its local service stops before its program goes, so it never restarts into a missing one, and
# `openajar local start` below puts the new service on the same home and port.
# legacy-name: ~/.agentos and ~/.config/agentos stay where they are, and openajar uses them as they are.
old_service=
if [ "$pkg" = openajar ] && uv tool list 2>/dev/null | grep -q '^agentos '; then  # legacy-name
  if [ -f "$HOME/Library/LaunchAgents/dev.agentos.local.plist" ]; then  # legacy-name
    old_service=1
    launchctl bootout "gui/$(id -u)/dev.agentos.local" 2>/dev/null || true  # legacy-name
  elif [ -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/agentos-local.service" ]; then  # legacy-name
    old_service=1
    systemctl --user stop agentos-local.service 2>/dev/null || true  # legacy-name
  fi
  say "Replacing the agentos tool with openajar (your files stay where they are)"  # legacy-name
  uv tool uninstall agentos >/dev/null  # legacy-name
fi

say "Installing $pkg from $url ($ref)"
if ! uv tool install --force --python 3.12 "$pkg[local] @ git+$url@$ref"; then
  [ "$pkg" = openajar ] || exit 1
  say "No openajar package at $ref: installing it under its name before the rename"
  pkg=agentos cmd=agentos  # legacy-name
  uv tool install --force --python 3.12 "$pkg[local] @ git+$url@$ref"
fi

# Puts ~/.local/bin on PATH in your shell's startup file, when it is not there yet.
uv tool update-shell >/dev/null 2>&1 || true
command -v "$cmd" >/dev/null 2>&1 || die "$cmd is installed but not on PATH; add $bin to PATH"
say "$cmd $("$cmd" --version 2>/dev/null || echo installed) is at $(command -v "$cmd")"

if [ -n "$old_service" ]; then
  say "Starting the local service again, as openajar (same home, same port)"
  "$cmd" local start || say "Could not start it; run: $cmd local start"
fi

if [ "$no_setup" != 1 ] && [ -t 1 ] && (: </dev/tty) 2>/dev/null; then
  say "Starting $cmd setup"
  exec "$cmd" setup </dev/tty
fi
# No setup to ask, so the web app follows openajar here (setup's own web step does it otherwise).
"$cmd" web update || say "The web app was not updated; the node works without it ($cmd web install)."
echo
echo "Next step: $cmd setup"
case ":$given_path:" in
  *":$bin:"*) ;;
  *) echo "(open a new terminal first, so $bin is on your PATH)" ;;
esac
