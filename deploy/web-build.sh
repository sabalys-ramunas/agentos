#!/usr/bin/env bash
# The web app's build (web/dist), rebuilt when its sources changed since the last good build.
#
# Sourced by deploy/install-node.sh and scripts/sync-node.sh; nothing runs when it is sourced.
#
#   web_is_stale DIR          true (status 0) when DIR/dist is missing or was built from other sources
#   web_rebuild DIR CMD...    run CMD in DIR to build dist. The old dist stays when the build fails.
#
# The stamp is a fingerprint of DIR's sources and build inputs (every file but dist, node_modules and the
# test and screenshot folders), written into dist after a good build. A checkout copied with its own old
# dist is then rebuilt, and an upgrade never keeps serving a build of older sources.

# A fingerprint of DIR's files, the same for the same content wherever the folder is copied.
web_fingerprint() {
  (cd "$1" && find . \( -path ./node_modules -o -path ./dist -o -path ./dist.previous -o -path ./test-results \
      -o -path ./screenshots -o -path ./coverage -o -name '.dev-node-*' \) -prune -o -type f -print0 \
    | LC_ALL=C sort -z | xargs -0 cksum | cksum)
}

web_is_stale() {
  local dir="$1"
  [ -f "$dir/dist/index.html" ] || return 0
  [ "$(cat "$dir/dist/.openajar-build" 2>/dev/null)" != "$(web_fingerprint "$dir")" ]
}

web_rebuild() {
  local dir="$1"
  shift
  local old="$dir/dist.previous" fingerprint
  # Taken before anything moves: a source edited while the build runs is seen as changed next time.
  fingerprint="$(web_fingerprint "$dir")"
  rm -rf "$old"
  if [ -d "$dir/dist" ]; then mv "$dir/dist" "$old"; fi
  if (cd "$dir" && "$@") && [ -f "$dir/dist/index.html" ]; then
    printf '%s\n' "$fingerprint" > "$dir/dist/.openajar-build"
    rm -rf "$old"
    return 0
  fi
  rm -rf "$dir/dist"
  if [ -d "$old" ]; then mv "$old" "$dir/dist"; fi
  return 1
}
