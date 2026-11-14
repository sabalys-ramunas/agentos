# Sourced (not run) by the scripts that reach a dev node: sync-node.sh and node-test.sh.
#
# They need two settings, from the environment or from .agentos-node.env at the top of the
# checkout (gitignored, so it never reaches a commit):
#
#   AGENTOS_NODE      the node's SSH host, as in ~/.ssh/config
#   AGENTOS_DEV_ROOT  a directory on the node that holds the dev checkouts; every sync goes
#                     into a directory under it, and nowhere else
#
# One line sets both, host and root joined by a colon as scp writes it:
#
#   AGENTOS_NODE=mynode:/srv/agentos-dev
#
# The environment wins over the file. NODE_HOST, the older name, still works for the host.

_agentos_env_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_agentos_env_node="${AGENTOS_NODE:-${NODE_HOST:-}}"
_agentos_env_dev="${AGENTOS_DEV_ROOT:-}"
if [ -f "$_agentos_env_root/.agentos-node.env" ]; then
  # shellcheck disable=SC1091
  . "$_agentos_env_root/.agentos-node.env"
  [ -n "$_agentos_env_node" ] || _agentos_env_node="${AGENTOS_NODE:-${NODE_HOST:-}}"
  [ -n "$_agentos_env_dev" ] || _agentos_env_dev="${AGENTOS_DEV_ROOT:-}"
fi
case "$_agentos_env_node" in
  *:/*)
    [ -n "$_agentos_env_dev" ] || _agentos_env_dev="${_agentos_env_node#*:}"
    _agentos_env_node="${_agentos_env_node%%:*}"
    ;;
esac
if [ -z "$_agentos_env_node" ] || [ -z "$_agentos_env_dev" ]; then
  {
    echo "error: this script needs your dev node: its SSH host and a directory on it for dev checkouts."
    echo "Put one line in $_agentos_env_root/.agentos-node.env (gitignored), for example:"
    echo ""
    echo "  AGENTOS_NODE=mynode:/srv/agentos-dev"
    echo ""
    echo "or export AGENTOS_NODE and AGENTOS_DEV_ROOT. See CONTRIBUTING.md (\"Testing on a node\")."
  } >&2
  exit 2
fi
_agentos_env_dev="${_agentos_env_dev%/}"
case "$_agentos_env_dev" in
  /?*) ;;
  *) echo "error: AGENTOS_DEV_ROOT must be an absolute path on the node (got '$_agentos_env_dev')" >&2; exit 2 ;;
esac
NODE_HOST="$_agentos_env_node"
AGENTOS_DEV_ROOT="$_agentos_env_dev"
unset _agentos_env_root _agentos_env_node _agentos_env_dev
