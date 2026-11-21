#!/usr/bin/env bash
# Install Node.js 22 and the official brain CLIs (Claude Code, Codex) for openajar brains.
#
# Idempotent and self-contained: everything goes under $PREFIX (default /opt/agentos/node),
# so a system Node.js that other software relies on is never replaced. openajar finds the
# CLIs through the brains setting `bin_dirs` (default includes /opt/agentos/node/bin).
#
#   install_cli.sh                       install what is missing
#   install_cli.sh --upgrade             reinstall the CLIs at the requested versions
#   install_cli.sh --with-user           also create the unprivileged brain user (agentos-brain)
#   install_cli.sh --only claude         just one CLI (claude | codex)
#
# Versions: CLAUDE_CODE_VERSION / CODEX_VERSION pin npm versions (default: latest).
# The Node.js tarball is verified against the official SHASUMS256.txt before use.
#
# Subscriptions are bring-your-own for the owner's own use on the owner's node. This script
# installs software only; it never signs in and never touches credentials.
set -euo pipefail

PREFIX="${OPENAJAR_NODE_PREFIX:-/opt/openajar/node}"
NODE_MAJOR="${OPENAJAR_NODE_MAJOR:-22}"
BRAIN_USER="${OPENAJAR_BRAIN_USER:-agentos-brain}"
NODE_DIST="${OPENAJAR_NODE_DIST:-https://nodejs.org/dist}"
UPGRADE=0
WITH_USER=0
ONLY=""

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --upgrade) UPGRADE=1; shift ;;
    --with-user) WITH_USER=1; shift ;;
    --only) ONLY="$2"; shift 2 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

log() { printf '[install_cli] %s\n' "$*"; }
die() { printf '[install_cli] error: %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
  Linux) OS=linux ;;
  Darwin) OS=darwin ;;
  *) die "unsupported OS $(uname -s)" ;;
esac
case "$(uname -m)" in
  x86_64|amd64) ARCH=x64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) die "unsupported CPU $(uname -m)" ;;
esac

node_major() { "$1" -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0; }

install_node() {
  if [ -x "$PREFIX/bin/node" ] && [ "$(node_major "$PREFIX/bin/node")" -ge "$NODE_MAJOR" ]; then
    log "node $("$PREFIX/bin/node" --version) already in $PREFIX"
    return
  fi
  command -v curl >/dev/null || die "curl is required"
  command -v tar >/dev/null || die "tar is required"
  local tmp; tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN
  local base="$NODE_DIST/latest-v${NODE_MAJOR}.x"
  log "fetching $base/SHASUMS256.txt"
  curl -fsSL "$base/SHASUMS256.txt" -o "$tmp/SHASUMS256.txt"
  local file
  file="$(grep -oE "node-v${NODE_MAJOR}\.[0-9]+\.[0-9]+-${OS}-${ARCH}\.tar\.gz" "$tmp/SHASUMS256.txt" | head -1)"
  [ -n "$file" ] || die "no node v${NODE_MAJOR} tarball for ${OS}-${ARCH}"
  log "downloading $file"
  curl -fsSL "$base/$file" -o "$tmp/$file"
  local want got
  want="$(grep " ${file}\$" "$tmp/SHASUMS256.txt" | awk '{print $1}')"
  if command -v sha256sum >/dev/null; then got="$(sha256sum "$tmp/$file" | awk '{print $1}')"
  else got="$(shasum -a 256 "$tmp/$file" | awk '{print $1}')"; fi
  [ "$want" = "$got" ] || die "checksum mismatch for $file"
  log "checksum ok"
  mkdir -p "$(dirname "$PREFIX")"
  rm -rf "$PREFIX.new"
  mkdir -p "$PREFIX.new"
  tar -xzf "$tmp/$file" -C "$PREFIX.new" --strip-components=1
  # keep globally installed CLIs if a previous prefix exists
  if [ -d "$PREFIX/lib/node_modules" ]; then
    for pkg in "$PREFIX"/lib/node_modules/@anthropic-ai "$PREFIX"/lib/node_modules/@openai; do
      [ -d "$pkg" ] && cp -a "$pkg" "$PREFIX.new/lib/node_modules/" || true
    done
  fi
  rm -rf "$PREFIX.old"
  [ -d "$PREFIX" ] && mv "$PREFIX" "$PREFIX.old"
  mv "$PREFIX.new" "$PREFIX"
  rm -rf "$PREFIX.old"
  log "node $("$PREFIX/bin/node" --version) installed in $PREFIX"
}

cli_version() {
  # a throwaway HOME so a version check never writes ~/.claude or ~/.codex for this user
  local h; h="$(mktemp -d)"
  HOME="$h" CLAUDE_CONFIG_DIR="$h/.claude" CODEX_HOME="$h/.codex" DISABLE_AUTOUPDATER=1 \
    "$PREFIX/bin/$1" --version 2>/dev/null | head -1
  rm -rf "$h"
}

install_pkg() {
  local bin="$1" pkg="$2"
  if [ -x "$PREFIX/bin/$bin" ] && [ "$UPGRADE" -eq 0 ]; then
    log "$bin already installed ($(cli_version "$bin"))"
    return
  fi
  log "npm install -g $pkg"
  "$PREFIX/bin/npm" install -g --prefix "$PREFIX" --no-fund --no-audit "$pkg" >/dev/null
  log "$bin $(cli_version "$bin")"
}

create_user() {
  [ "$OS" = linux ] || { log "skipping brain user on $OS"; return; }
  [ "$(id -u)" -eq 0 ] || die "--with-user needs root"
  if id "$BRAIN_USER" >/dev/null 2>&1; then
    log "user $BRAIN_USER exists"
  else
    useradd --system --create-home --home-dir "/var/lib/$BRAIN_USER" --shell /usr/sbin/nologin "$BRAIN_USER"
    chmod 700 "/var/lib/$BRAIN_USER"
    log "created system user $BRAIN_USER (brain processes run as this user, not root)"
  fi
}

export PATH="$PREFIX/bin:$PATH"
install_node
if [ -z "$ONLY" ] || [ "$ONLY" = claude ]; then
  install_pkg claude "@anthropic-ai/claude-code${CLAUDE_CODE_VERSION:+@$CLAUDE_CODE_VERSION}"
fi
if [ -z "$ONLY" ] || [ "$ONLY" = codex ]; then
  install_pkg codex "@openai/codex${CODEX_VERSION:+@$CODEX_VERSION}"
fi
[ "$WITH_USER" -eq 1 ] && create_user
log "done. bin dir: $PREFIX/bin"
log "next: sign in on the node (claude setup-token, codex login), then: openajar brains add ..."
