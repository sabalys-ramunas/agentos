"""Short commands that ask an environment's VM about itself (its listening ports, its repositories
and their unsaved work), what they print read back, and how long such a check may take."""

from __future__ import annotations

import re

CHECK_TIMEOUT_S = 30.0  # one short command on the VM (a probe, mkdir, the setup state)

# One command over SSH that says, for every repository under /work (four folders down, past
# node_modules and .venv), its branch, the commits on any local branch (or a detached HEAD) that no
# remote has, its stashes, and the files git status lists, untracked ones included. That is the
# work close and the reaper never delete without discard. A repository git cannot read says
# `error=`, which counts as unsaved. safe.directory: one made by another user still answers.
GIT_STATE = r"""
find /work -maxdepth 4 \( -name node_modules -o -name .venv \) -prune -o -name .git -print -prune 2>/dev/null | sort |
while IFS= read -r gitdir; do
  d=$(dirname "$gitdir")
  g() { git -c safe.directory='*' -C "$d" "$@"; }
  echo "repo=$d"
  if ! g status --porcelain=v1 >/dev/null 2>&1; then echo "error=git cannot read it"; continue; fi
  echo "branch=$(g rev-parse --abbrev-ref HEAD 2>/dev/null)"
  echo "ahead=$(g rev-list --count HEAD --branches --not --remotes 2>/dev/null \
    || g rev-list --count --branches --not --remotes 2>/dev/null || echo 0)"
  echo "dirty_count=$(g status --porcelain=v1 --untracked-files=normal | wc -l | tr -d ' ')"
  g status --porcelain=v1 --untracked-files=normal | head -n 50 | sed 's/^/dirty=/'
  { g log --format='unpushed=%h %s' HEAD --branches --not --remotes 2>/dev/null \
    || g log --format='unpushed=%h %s' --branches --not --remotes 2>/dev/null; } | head -n 50
  echo "stash_count=$(g stash list 2>/dev/null | wc -l | tr -d ' ')"
  g stash list --format='stash=%gd %s' 2>/dev/null | head -n 20
done
"""


def parse_ports(text: str) -> list[dict]:
    """`ss -ltnpH` lines -> [{port, process}], each port once, without the node's own (ssh, the
    gateway's reverse tunnel)."""
    out: dict[int, dict] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0] != "LISTEN":
            continue
        local = parts[3]
        try:
            port = int(local.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            continue
        m = re.search(r'users:\(\("([^"]+)"', line)
        proc = m.group(1) if m else None
        if proc in ("sshd", "sshd-session") or port in (22, 3129):
            continue
        out.setdefault(port, {"port": port, "process": proc, "link": None})
    return [out[p] for p in sorted(out)]


def parse_git_state(text: str) -> list[dict]:
    repos: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key == "repo":
            cur = {"dir": value, "branch": None, "ahead": 0, "dirty": 0, "dirty_files": [], "unpushed": [],
                   "stash_count": 0, "stashes": [], "error": None}
            repos.append(cur)
        elif cur is None:
            continue
        elif key == "branch":
            cur["branch"] = value or None
        elif key in ("ahead", "dirty_count", "stash_count"):
            cur["dirty" if key == "dirty_count" else key] = int(value) if value.isdigit() else 0
        elif key == "dirty":
            cur["dirty_files"].append(value[3:] if len(value) > 3 else value)
        elif key == "unpushed":
            cur["unpushed"].append(value)
        elif key == "stash":
            cur["stashes"].append(value)
        elif key == "error":
            cur["error"] = value or "git cannot read it"
    return repos


def main_repo(repos: list[dict] | None, workdir: str) -> dict | None:
    """The repository an environment was opened with (its workdir), else the first one."""
    if not repos:
        return None
    return next((r for r in repos if r["dir"] == workdir), repos[0])
