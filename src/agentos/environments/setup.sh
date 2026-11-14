#!/bin/sh
# What an environment's machine has before an agent works on it: git, curl, build tools, Node 22,
# Python with uv, and the /work folder. The one copy of this list: cloud-init runs it on a fresh
# Ubuntu VM (agentos.environments.machine), and the docker provider's image runs it at build time
# (deploy/environments/Dockerfile). A machine that has run it has /var/lib/agentos-env/ready, so a
# second run (a `dev` snapshot made from such a machine) does nothing.
set -eu
READY=/var/lib/agentos-env/ready
if [ -f "$READY" ]; then
  exit 0
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends ca-certificates curl git build-essential pkg-config \
  python3 python3-venv iproute2 procps jq unzip rsync less
if ! command -v node >/dev/null 2>&1 || ! node --version | grep -q '^v22\.'; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y -q nodejs
fi
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi
apt-get clean
rm -rf /var/lib/apt/lists/*
mkdir -p /work
mkdir -p "$(dirname "$READY")"
date -u +%Y-%m-%dT%H:%M:%SZ > "$READY"
