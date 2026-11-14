#!/bin/bash
# Keeps this Mac mini on and reachable as a controller: no system sleep (the disks stay awake), the
# display may sleep, the Mac wakes for network access, and it starts again after a power cut.
# Settings that this Mac does not accept are reported and skipped. It asks for an administrator
# password. Run it once; the settings in use are printed at the end.
#
#   deploy/templates/mac-mini/power.sh
set -euo pipefail

[ "$(uname -s)" = Darwin ] || { echo "error: power.sh is for macOS" >&2; exit 1; }

apply() {
  if ! sudo pmset -a "$@"; then
    echo "warning: this Mac did not accept: pmset -a $*" >&2
  fi
}
apply sleep 0
apply disksleep 0
apply displaysleep 10
apply womp 1
apply autorestart 1

echo
pmset -g
