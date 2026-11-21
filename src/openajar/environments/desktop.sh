#!/bin/sh
# The environment's desktop on its own machine (environments.toml: browser = "vm", desktop = true): a
# window manager (openbox), a terminal (lxterminal) and a file manager (pcmanfm), the dock's runtime
# (python3 with python3-xlib and python3-pil), the wallpaper's renderer (rsvg-convert) and the fonts
# (Inter for the UI, JetBrains Mono for the terminals; both SIL Open Font License 1.1), and TigerVNC's Xvnc
# (GPL-2.0), the screen and its VNC server in one process, which streams a desktop smoother and cheaper than
# Xvfb with x11vnc (measured: twice the updates a second for the same CPU). All from the Ubuntu archive.
# Nothing here starts anything: the screen's control script (vmbrowser_ctl.py) starts the session with the
# screen, from the files the node sends it each time (openajar/environments/desktop).
#
# Beside browser.sh, run by the same three paths: the docker image (deploy/environments/Dockerfile), the
# Hetzner `dev` snapshot, and a machine without it when its screen first starts. The ready marker holds
# VERSION on its first line: a machine with an older one installs again, so a later change of the
# desktop's packages reaches machines made before it. openajar.environments.vmbrowser.DESKTOP_VERSION is
# the same number.
set -eu
VERSION=1
DIR=/opt/agentos-desktop
READY="$DIR/ready"
if [ -f "$READY" ] && [ "$(head -n 1 "$READY")" = "$VERSION" ]; then
  exit 0
fi
export DEBIAN_FRONTEND=noninteractive
mkdir -p /etc/apt/apt.conf.d
echo 'DPkg::Lock::Timeout "600";' > /etc/apt/apt.conf.d/90agentos-lock-timeout
apt-get update -q
apt-get install -y -q --no-install-recommends openbox lxterminal pcmanfm librsvg2-bin python3 python3-xlib \
  python3-pil fonts-inter-variable fonts-jetbrains-mono adwaita-icon-theme tigervnc-standalone-server
apt-get clean
# an image build drops the package lists to stay small; on a running machine (AGENTOS_DESKTOP_LIVE, the node
# installing it late) the agent may be using apt right now, and without the lists its install would fail
if [ -z "${AGENTOS_DESKTOP_LIVE:-}" ]; then
  rm -rf /var/lib/apt/lists/*
fi
mkdir -p "$DIR"
echo "$VERSION" > "$READY"
date -u +%Y-%m-%dT%H:%M:%SZ >> "$READY"
