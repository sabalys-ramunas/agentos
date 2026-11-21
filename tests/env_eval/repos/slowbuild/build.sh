#!/bin/sh
# The release build: slow on purpose (longer than a command waits by default). Prints its build id at the end.
set -eu
n="${1:-7}"
for step in compile bundle compress sign; do
  echo "step: $step"
  sleep "${SLOWBUILD_STEP_S:-20}"
done
mkdir -p dist
id=$(printf 'release-%s' "$n" | sha256sum | cut -c1-12)
echo "$id" > dist/build-id.txt
echo "BUILD_ID=$id"
