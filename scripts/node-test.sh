#!/usr/bin/env bash
# Run the node tests (pytest -m node) on the node, in this agent's own directory.
#
#   scripts/node-test.sh mcp
#   scripts/node-test.sh lanes tests/node/test_lanes.py -k warm -x
#   scripts/node-test.sh bench --python scripts/bench_node.py --runs 5
#
# Syncs this checkout to $OPENAJAR_DEV_ROOT/<name> on $OPENAJAR_NODE (see scripts/node-env.sh), runs `uv sync`, then `uv run pytest -m node`
# with any extra pytest arguments (or, after --python, `uv run python` with the arguments that follow). The agent's display and port ranges (docs/DEVELOPING.md, "On the node") are
# exported as OPENAJAR_TEST_* so tests never collide with another agent or the PoC.
set -euo pipefail

NAME="${1:?usage: scripts/node-test.sh <name> [pytest args]}"
shift
[[ "$NAME" =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "error: name must be lowercase letters, digits, dashes" >&2; exit 2; }

# Ranges (docs/DEVELOPING.md, "On the node"). Any other name uses OPENAJAR_TEST_SLOT (default 14).
case "$NAME" in
  lanes) SLOT=11 ;;
  interaction) SLOT=12 ;;
  mcp) SLOT=13 ;;
  *) SLOT="${OPENAJAR_TEST_SLOT:-14}" ;;
esac
# shellcheck source=scripts/node-env.sh
. "$(dirname "${BASH_SOURCE[0]}")/node-env.sh"
REMOTE_DIR="$OPENAJAR_DEV_ROOT/$NAME"

# Slots 11 to 59. Slot S owns displays :S0 to :S9, viewer ports 6S0 to 6S9 (6000 + 10S),
# VNC ports 5000 + 10S and MCP ports 7400 + 10(S - 10), ten of each. The check below refuses
# any slot whose ranges touch the PoC (:99, 5900, 6080) or the production node (5910 to 5919,
# 6100 to 6109, 7400).
[[ "$SLOT" =~ ^[1-5][0-9]$ ]] && [ "$SLOT" -ge 11 ] \
  || { echo "error: OPENAJAR_TEST_SLOT must be 11 to 59 (got $SLOT)" >&2; exit 2; }
D=$((SLOT * 10)); V=$((6000 + SLOT * 10)); C=$((5000 + SLOT * 10)); M=$((7400 + (SLOT - 10) * 10))
for port in 5900 6080 7400 $(seq 5910 5919) $(seq 6100 6109); do
  for base in $V $C $M; do
    if [ "$port" -ge "$base" ] && [ "$port" -le $((base + 9)) ]; then
      echo "error: slot $SLOT would use reserved port $port" >&2; exit 2
    fi
  done
done
"$(dirname "${BASH_SOURCE[0]}")/sync-node.sh" "$REMOTE_DIR"

env_vars=(
  "OPENAJAR_TEST_NAME=$NAME"
  "OPENAJAR_TEST_DISPLAY_BASE=$D"
  "OPENAJAR_TEST_VIEWER_BASE=$V"
  "OPENAJAR_TEST_VNC_BASE=$C"
  "OPENAJAR_TEST_MCP_PORT_BASE=$M"
)
# Settings of the soak runs and benchmarks (docs/BENCH.md) go along, for example OPENAJAR_SOAK_SECONDS.
while IFS='=' read -r var value; do
  [[ "$value" =~ ^[A-Za-z0-9._/:-]*$ ]] || { echo "error: $var has characters node-test.sh will not pass" >&2; exit 2; }
  env_vars+=("$var=$value")
done < <(env | grep -E '^OPENAJAR_(SOAK|BENCH)_[A-Z_]+=' || true)
RUN="pytest -m node"
if [ "${1:-}" = "--python" ]; then RUN="python"; shift; fi
pytest_args=""
[ "$#" -gt 0 ] && pytest_args="$(printf '%q ' "$@")"

# shellcheck disable=SC2029  # expanded locally on purpose
ssh "$NODE_HOST" "set -e
  export PATH=\"\$HOME/.local/bin:\$PATH\"
  cd '$REMOTE_DIR'
  uv sync --frozen --extra node --quiet
  env ${env_vars[*]} uv run --no-sync $RUN $pytest_args"
