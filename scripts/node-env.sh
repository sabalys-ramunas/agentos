# Sourced (not run) by the scripts that reach a dev node: sync-node.sh and node-test.sh.
#
# They need two settings, from the environment or from .agentos-node.env at the top of the
# checkout (gitignored, so it never reaches a commit):
#
#   OPENAJAR_NODE      the node's SSH host, as in ~/.ssh/config
#   OPENAJAR_DEV_ROOT  a directory on the node that holds the dev checkouts; every sync goes
#                     into a directory under it, and nowhere else
#
# One line sets both, host and root joined by a colon as scp writes it:
#
#   OPENAJAR_NODE=mynode:/srv/agentos-dev
#
# The environment wins over the file. NODE_HOST, the older name, still works for the host.

# legacy-name: a variable set under its old name (AGENTOS_NODE, ...) counts when the OPENAJAR_ one is not set
_openajar_env_legacy() {
  local old new
  for old in $(compgen -v AGENTOS_); do  # legacy-name
    new="OPENAJAR_${old#AGENTOS_}"  # legacy-name
    [ -n "${!new+x}" ] || export "$new=${!old}"
  done
}
_openajar_env_legacy
_openajar_env_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_openajar_env_node="${OPENAJAR_NODE:-${NODE_HOST:-}}"
_openajar_env_dev="${OPENAJAR_DEV_ROOT:-}"
if [ -f "$_openajar_env_root/.agentos-node.env" ]; then
  # shellcheck disable=SC1091
  . "$_openajar_env_root/.agentos-node.env"
  _openajar_env_legacy
  [ -n "$_openajar_env_node" ] || _openajar_env_node="${OPENAJAR_NODE:-${NODE_HOST:-}}"
  [ -n "$_openajar_env_dev" ] || _openajar_env_dev="${OPENAJAR_DEV_ROOT:-}"
fi
case "$_openajar_env_node" in
  *:/*)
    [ -n "$_openajar_env_dev" ] || _openajar_env_dev="${_openajar_env_node#*:}"
    _openajar_env_node="${_openajar_env_node%%:*}"
    ;;
esac
if [ -z "$_openajar_env_node" ] || [ -z "$_openajar_env_dev" ]; then
  {
    echo "error: this script needs your dev node: its SSH host and a directory on it for dev checkouts."
    echo "Put one line in $_openajar_env_root/.agentos-node.env (gitignored), for example:"
    echo ""
    echo "  OPENAJAR_NODE=mynode:/srv/agentos-dev"
    echo ""
    echo "or export OPENAJAR_NODE and OPENAJAR_DEV_ROOT. See CONTRIBUTING.md (\"Testing on a node\")."
  } >&2
  exit 2
fi
_openajar_env_dev="${_openajar_env_dev%/}"
case "$_openajar_env_dev" in
  /?*) ;;
  *) echo "error: OPENAJAR_DEV_ROOT must be an absolute path on the node (got '$_openajar_env_dev')" >&2; exit 2 ;;
esac
NODE_HOST="$_openajar_env_node"
OPENAJAR_DEV_ROOT="$_openajar_env_dev"
unset _openajar_env_root _openajar_env_node _openajar_env_dev
