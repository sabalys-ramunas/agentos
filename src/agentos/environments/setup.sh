#!/bin/sh
# What an environment's machine has before an agent works on it: git, curl, build tools, Node 22
# with npm and pnpm (through corepack), Python with uv, and the /work folder. The one copy of this list: cloud-init runs it on a fresh
# Ubuntu VM (agentos.environments.machine), and the docker provider's image runs it at build time
# (deploy/environments/Dockerfile). A machine that has run it has /var/lib/agentos-env/ready, so a
# second run (a `dev` snapshot made from such a machine) does nothing.
set -eu
READY=/var/lib/agentos-env/ready
if [ -f "$READY" ]; then
  exit 0
fi
export DEBIAN_FRONTEND=noninteractive
# A fresh VM's unattended-upgrades holds the dpkg lock for its first minutes: wait for it (up to 10
# minutes) instead of failing on "Could not get lock". It stays on, so long-lived machines get updates.
# A config file, not a flag, so every apt-get here waits, also the one playwright install-deps runs.
mkdir -p /etc/apt/apt.conf.d
echo 'DPkg::Lock::Timeout "600";' > /etc/apt/apt.conf.d/90agentos-lock-timeout
apt-get update -q
apt-get install -y -q --no-install-recommends ca-certificates curl git build-essential pkg-config \
  python3 python3-venv iproute2 procps jq unzip rsync less
if ! command -v node >/dev/null 2>&1 || ! node --version | grep -q '^v22\.'; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y -q nodejs
fi
# pnpm through corepack (it comes with Node): enabled, with a pnpm already downloaded, so `pnpm install`
# works at once. A project's packageManager field still picks its own version; never a prompt for it.
grep -q '^COREPACK_ENABLE_DOWNLOAD_PROMPT=' /etc/environment 2>/dev/null || \
  echo 'COREPACK_ENABLE_DOWNLOAD_PROMPT=0' >> /etc/environment
if ! command -v pnpm >/dev/null 2>&1; then
  corepack enable
  COREPACK_ENABLE_DOWNLOAD_PROMPT=0 corepack install -g pnpm@latest || true
fi
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi
# The package lists stay, so the agent's first `apt-get install` works (the .deb cache goes). A machine
# from a snapshot fetches them again in the background (agentos.environments.machine.apt_ready).
apt-get clean
mkdir -p /work
mkdir -p "$(dirname "$READY")"
date -u +%Y-%m-%dT%H:%M:%SZ > "$READY"
