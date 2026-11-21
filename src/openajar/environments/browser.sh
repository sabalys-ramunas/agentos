#!/bin/sh
# The environment's browser on its own machine (environments.toml: browser = "vm"): a virtual screen
# (Xvfb), its live view (x11vnc, on a unix socket only), and Camoufox in a venv of its own, pinned to
# the versions the node runs (browser-requirements.txt, with hashes, and the browser build that
# camoufox release names). Nothing here starts anything: openajar.environments.vmbrowser starts the
# screen and the browser over the node's SSH connection, bound to the machine's loopback only.
#
# The one copy of this list: the docker provider's image runs it at build time
# (deploy/environments/Dockerfile), `openajar vm image build dev` runs it into the Hetzner snapshot,
# and a machine without it gets it when its first browser opens (slower: a few minutes). A machine
# that has run it has /opt/agentos-browser/ready, so a second run does nothing.
set -eu
DIR=/opt/agentos-browser
READY="$DIR/ready"
REQ="${AGENTOS_BROWSER_REQUIREMENTS:-/usr/local/lib/agentos-env/browser-requirements.txt}"
if [ -f "$READY" ]; then
  exit 0
fi
export DEBIAN_FRONTEND=noninteractive
# A fresh VM's unattended-upgrades holds the dpkg lock for its first minutes: wait for it (up to 10
# minutes) instead of failing on "Could not get lock". It stays on, so long-lived machines get updates.
# A config file, not a flag, so every apt-get here waits, also the one playwright install-deps runs.
mkdir -p /etc/apt/apt.conf.d
echo 'DPkg::Lock::Timeout "600";' > /etc/apt/apt.conf.d/90agentos-lock-timeout
if ! command -v uv >/dev/null 2>&1; then
  apt-get update -q
  apt-get install -y -q --no-install-recommends ca-certificates curl
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi
export UV_CACHE_DIR="$DIR/.uv-cache" XDG_CACHE_HOME="$DIR/cache"
mkdir -p "$DIR"
uv venv -q --python 3.12 "$DIR/venv"
uv pip install -q --python "$DIR/venv/bin/python" --require-hashes -r "$REQ"
apt-get update -q
# What Firefox needs to run, as the pinned Playwright lists it (the node installs the same), then the
# screen, its live view (and its clipboard, xclip), the recorder of video proofs (ffmpeg, openajar.environments.
# recording) and the fonts the node's browsers have.
"$DIR/venv/bin/python" -m playwright install-deps firefox
apt-get install -y -q --no-install-recommends xvfb x11vnc xclip ffmpeg fonts-liberation fonts-noto-color-emoji
# The browser build camoufox 0.5.9 is paired with (its browser-pin.json), and the fingerprint model.
"$DIR/venv/bin/python" -m camoufox fetch
rm -rf "$UV_CACHE_DIR"
apt-get clean
rm -rf /var/lib/apt/lists/*
chmod -R a+rX "$DIR"
"$DIR/venv/bin/python" -c 'import importlib.metadata as m; print("camoufox", m.version("camoufox"), "playwright", m.version("playwright"))' > "$READY"
date -u +%Y-%m-%dT%H:%M:%SZ >> "$READY"
