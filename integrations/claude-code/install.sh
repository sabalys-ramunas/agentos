#!/bin/sh
# Copy the agentos skills into Claude Code's skills folder (~/.claude/skills, or
# $CLAUDE_CONFIG_DIR/skills). `agentos connect` does the same when Claude Code is
# installed; this script is for a checkout without it. The skills live in the package
# (src/agentos/client/skills), so an installed agentos carries them too.
#
#   integrations/claude-code/install.sh             install or update
#   integrations/claude-code/install.sh --dry-run   show what it would copy
#   integrations/claude-code/install.sh --uninstall remove them again
set -eu

here=$(cd "$(dirname "$0")/../../src/agentos/client" && pwd)
dest="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
mode=install
case "${1:-}" in
  "") ;;
  --dry-run) mode=dry ;;
  --uninstall) mode=uninstall ;;
  *) echo "usage: install.sh [--dry-run | --uninstall]" >&2; exit 2 ;;
esac

for skill in agentos-environment agentos-offload agentos-lanes agentos-studio; do
  src="$here/skills/$skill/SKILL.md"
  target="$dest/$skill"
  case "$mode" in
    dry)
      echo "would copy $src to $target/SKILL.md"
      ;;
    uninstall)
      if [ -f "$target/SKILL.md" ]; then
        rm -f "$target/SKILL.md"
        rmdir "$target" 2>/dev/null || true
        echo "removed $target/SKILL.md"
      fi
      ;;
    install)
      mkdir -p "$target"
      cp "$src" "$target/SKILL.md"
      echo "installed $target/SKILL.md"
      ;;
  esac
done

if [ "$mode" = install ]; then
  echo "In Claude Code: /agentos-offload hands the session to a node; agentos-environment, agentos-lanes"
  echo "and agentos-studio load by themselves when a task needs a VM, uses lanes or checks a page."
  echo "Start a new session to pick them up."
fi
