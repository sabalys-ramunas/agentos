"""The bash scripts behind checkpoints (agentos.environments.checkpoints), run on an environment's
machine over its SSH with their arguments after `bash -s --`. As in agentos.environments.scripts, every
path is relative to the machine's root (the first argument), so the same script runs on a VM ("/") and
in tests (a folder), and the repositories are the ones the save finds: every `.git` under /work, four
folders down, past node_modules and .venv.

A checkpoint is git's own: for each repository, the work tree as `git add -A` sees it (tracked and new
files git does not ignore) is written with a temporary index (a copy of the real one, so only changed
files are read), committed with HEAD as its parent, and kept as refs/agentos/checkpoints/<n>. The
agent's index, branches and stashes are never touched, and the subject line says what it is:
`[agentos checkpoint 3, 4 files] before the refactor`, with `branch <name>` in the body (the branch
it was on, which a rollback checks out again). Only the newest KEEP numbers stay.

Each script prints `key=value` lines (fields after the key are tab-separated), and `error=...` with a
non-zero exit when it cannot go on.
"""

from __future__ import annotations

# The shared part: the arguments, the repositories, and the functions both scripts use.
#   now_tree: the work tree as a tree object (temporary index), into $TREE
#   make_one N: checkpoint N of repository $D, with $NOTE and $EXTRA in its subject
PRELUDE = r"""
set -u
ROOT=$1; W=$2; KEEP=$3; shift 3
cd "$ROOT" || { echo "error=the machine has no folder $ROOT"; exit 3; }
export GIT_AUTHOR_NAME="agentos checkpoint" GIT_AUTHOR_EMAIL="checkpoint@agentos.invalid"
export GIT_COMMITTER_NAME="agentos checkpoint" GIT_COMMITTER_EMAIL="checkpoint@agentos.invalid"
T=$(mktemp -d "${TMPDIR:-/tmp}/agentos-checkpoint.XXXXXX") || { echo "error=could not make a temporary folder (is the disk full?)"; exit 3; }
trap 'rm -rf "$T"' EXIT
find "$W" -maxdepth 4 \( -name node_modules -o -name .venv \) -prune -o -name .git -print -prune 2>/dev/null \
  | sort > "$T/repos"
D=
gx() { git -c safe.directory='*' -C "$D" "$@"; }
count() { tr -cd '\0' | wc -c | tr -d ' '; }
next_id() {
  m=0
  while IFS= read -r g <&3; do
    D=${g%/.git}
    for r in $(gx for-each-ref --format='%(refname:lstrip=3)' refs/agentos/checkpoints 2>/dev/null); do
      case "$r" in ''|*[!0-9]*) ;; *) [ "$r" -gt "$m" ] && m=$r ;; esac
    done
  done 3< "$T/repos"
  echo $((m + 1))
}
now_tree() {
  TREE=
  GD=$(gx rev-parse --absolute-git-dir 2>"$T/err") || return 1
  rm -f "$T/index"
  if [ -f "$GD/index" ]; then cp -p "$GD/index" "$T/index" || return 1; fi
  GIT_INDEX_FILE="$T/index" gx add -A -- . >/dev/null 2>"$T/err" || return 1
  TREE=$(GIT_INDEX_FILE="$T/index" gx write-tree 2>"$T/err") || return 1
}
make_one() {
  N=$1
  H=$(gx rev-parse -q --verify HEAD 2>/dev/null) || H=
  B=$(gx symbolic-ref -q --short HEAD 2>/dev/null) || B=
  now_tree || { echo "error=git could not read /$D: $(tail -n 1 "$T/err")"; return 1; }
  if [ -n "$H" ]; then F=$(gx diff-tree -r -z --no-commit-id --name-only "$H" "$TREE" | count)
  else F=$(gx ls-tree -r -z --name-only "$TREE" | count); fi
  [ "$F" = 1 ] && FW=file || FW=files
  C=$(printf '[agentos checkpoint %s, %s %s%s] %s\n\nbranch %s\n' "$N" "$F" "$FW" "$EXTRA" "$NOTE" "$B" \
      | gx commit-tree "$TREE" ${H:+-p "$H"} 2>"$T/err") || { echo "error=git could not commit /$D: $(tail -n 1 "$T/err")"; return 1; }
  gx update-ref "refs/agentos/checkpoints/$N" "$C" || { echo "error=git could not keep the checkpoint of /$D"; return 1; }
  printf 'made=/%s\t%s\t%s\t%s\t%s\n' "$D" "$C" "$H" "$B" "$F"
  for r in $(gx for-each-ref --format='%(refname:lstrip=3)' refs/agentos/checkpoints 2>/dev/null); do
    case "$r" in ''|*[!0-9]*) continue ;; esac
    if [ "$r" -le $((N - KEEP)) ]; then gx update-ref -d "refs/agentos/checkpoints/$r" && echo "pruned=$r"; fi
  done
}
make_all() {
  N=$1; done_list=
  while IFS= read -r g <&3; do
    D=${g%/.git}
    if ! make_one "$N"; then
      for d in $done_list; do D=$d; gx update-ref -d "refs/agentos/checkpoints/$N" 2>/dev/null; done
      return 1
    fi
    done_list="$done_list $D"
  done 3< "$T/repos"
}
"""

# Args after the prelude's: the note (one line). Makes checkpoint <next> of every repository.
CHECKPOINT = PRELUDE + r"""
NOTE=${1:-}; EXTRA=
[ -s "$T/repos" ] || { echo "error=no git repository under /$W"; exit 5; }
N=$(next_id)
echo "id=$N"
make_all "$N" || exit 4
echo "ok=1"
"""

# Args after the prelude's: the checkpoint to go back to. First every repository that has it is
# checked (nothing changes when one is mid merge or rebase, or when none has it), then the work as it
# is now becomes checkpoint <next> (`undo=`), then each repository goes back: its branch (the one the
# checkpoint was made on) to the checkpoint's HEAD, the index to that HEAD, the work tree to the
# checkpoint's files. Files made since that git does not ignore are removed; ignored ones stay.
ROLLBACK = PRELUDE + r"""
TO=$1
[ -s "$T/repos" ] || { echo "error=no git repository under /$W"; exit 5; }
have=0; busy=0
while IFS= read -r g <&3; do
  D=${g%/.git}
  gx rev-parse -q --verify "refs/agentos/checkpoints/$TO^{commit}" >/dev/null 2>&1 || continue
  have=$((have + 1))
  GD=$(gx rev-parse --absolute-git-dir 2>/dev/null) || continue
  what=
  if [ -f "$GD/MERGE_HEAD" ]; then what=merge
  elif [ -d "$GD/rebase-merge" ] || [ -d "$GD/rebase-apply" ]; then what=rebase
  elif [ -f "$GD/CHERRY_PICK_HEAD" ]; then what=cherry-pick
  elif [ -f "$GD/REVERT_HEAD" ]; then what=revert; fi
  if [ -n "$what" ]; then printf 'busy=/%s\t%s\n' "$D" "$what"; busy=1; fi
done 3< "$T/repos"
[ "$have" -gt 0 ] || { echo "missing=$TO"; exit 6; }
[ "$busy" = 0 ] || exit 7
NOTE="automatic, before the rollback to checkpoint $TO"; EXTRA=", before rollback to $TO"
U=$(next_id)
echo "undo=$U"
make_all "$U" || exit 4
while IFS= read -r g <&3; do
  D=${g%/.git}
  C=$(gx rev-parse -q --verify "refs/agentos/checkpoints/$TO^{commit}" 2>/dev/null) || { printf 'untouched=/%s\n' "$D"; continue; }
  A=$(gx rev-parse -q --verify "refs/agentos/checkpoints/$U^{commit}")
  P=$(gx rev-parse -q --verify "$C^" 2>/dev/null) || P=
  B0=$(gx log -1 --format=%b "$C" | sed -n 's/^branch //p' | head -n 1)
  WH=$(gx rev-parse -q --verify HEAD 2>/dev/null) || WH=
  WB=$(gx symbolic-ref -q --short HEAD 2>/dev/null) || WB=
  BW=; [ -n "$B0" ] && { BW=$(gx rev-parse -q --verify "refs/heads/$B0" 2>/dev/null) || BW=; }
  restored=$(gx diff-tree -r -z --no-commit-id --name-only --diff-filter=MDT "$C" "$A" | count)
  removed=$(gx diff-tree -r -z --no-commit-id --name-only --diff-filter=A "$C" "$A" | count)
  undone=0
  if [ -n "$P" ]; then
    [ -n "$WH" ] && undone=$(gx rev-list --count "$P..$WH" 2>/dev/null || echo 0)
    if [ -n "$B0" ]; then gx checkout -q -f -B "$B0" "$P" 2>"$T/err"; else gx checkout -q -f --detach "$P" 2>"$T/err"; fi \
      || { printf 'failed=/%s\t%s\n' "$D" "$(tail -n 1 "$T/err")"; continue; }
    gx diff-tree -r -z --no-commit-id --name-only --diff-filter=D "$P" "$C" \
      | while IFS= read -r -d '' f; do rm -f -- "$D/$f"; done
  fi
  gx diff-tree -r -z --no-commit-id --name-only --diff-filter=A "$C" "$A" \
    | while IFS= read -r -d '' f; do rm -f -- "$D/$f"; done
  rm -f "$T/index.r"
  GIT_INDEX_FILE="$T/index.r" gx read-tree "$C" && GIT_INDEX_FILE="$T/index.r" gx checkout-index -a -f 2>"$T/err" \
    || { printf 'failed=/%s\t%s\n' "$D" "$(tail -n 1 "$T/err")"; continue; }
  printf 'rolled=/%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$D" "$P" "$B0" "$WH" "$WB" "$BW" "$restored" "$removed" "$undone"
done 3< "$T/repos"
echo "ok=1"
"""

# Args after the prelude's: none. For each repository, each checkpoint against the work tree now (a
# tree made with a temporary index): files added, changed and removed since, and lines added and
# removed (git diff-tree --name-status and --numstat; a binary file counts no lines).
SUMMARY = PRELUDE + r"""
while IFS= read -r g <&3; do
  D=${g%/.git}
  refs=$(gx for-each-ref --format='%(refname:lstrip=3)' refs/agentos/checkpoints 2>/dev/null | head -n 40)
  [ -n "$refs" ] || continue
  now_tree || { printf 'error_repo=/%s\n' "$D"; continue; }
  for r in $refs; do
    s=$(gx diff-tree -r --no-commit-id --name-status "refs/agentos/checkpoints/$r" "$TREE" \
        | awk '{c=substr($1,1,1); if (c=="A") a++; else if (c=="D") d++; else m++} END {print a+0, m+0, d+0}')
    l=$(gx diff-tree -r --no-commit-id --numstat "refs/agentos/checkpoints/$r" "$TREE" \
        | awk '{if ($1 != "-") a+=$1; if ($2 != "-") d+=$2} END {print a+0, d+0}')
    printf 'sum=/%s\t%s\t%s\t%s\n' "$D" "$r" "$s" "$l"
  done
done 3< "$T/repos"
echo "ok=1"
"""

# Args after the prelude's: the checkpoint, the most bytes of patch in all, the most files listed per
# repository. Per repository that has it: `::repo <dir>`, then `::status` (git's --name-status lines),
# `::numstat` (its --numstat lines) and `::patch <bytes in all>` (the patch, cut at the cap), `::end`.
DIFF = PRELUDE + r"""
TO=$1; MAXB=$2; MAXF=$3; left=$MAXB; have=0
while IFS= read -r g <&3; do
  D=${g%/.git}
  gx rev-parse -q --verify "refs/agentos/checkpoints/$TO^{commit}" >/dev/null 2>&1 || continue
  have=1
  now_tree || { printf 'error_repo=/%s\n' "$D"; continue; }
  R="refs/agentos/checkpoints/$TO"
  echo "::repo /$D"
  echo "::files $(gx diff-tree -r -z --no-commit-id --name-only "$R" "$TREE" | count)"
  echo "::status"; gx diff-tree -r --no-commit-id --name-status "$R" "$TREE" | head -n "$MAXF"
  echo "::numstat"; gx diff-tree -r --no-commit-id --numstat "$R" "$TREE" | head -n "$MAXF"
  gx diff-tree -r -p --no-commit-id "$R" "$TREE" > "$T/patch"
  echo "::patch $(wc -c < "$T/patch" | tr -d ' ')"
  [ "$left" -gt 0 ] && head -c "$left" "$T/patch"
  n=$(wc -c < "$T/patch" | tr -d ' '); left=$((left - n)); [ "$left" -lt 0 ] && left=0
  echo; echo "::end"
done 3< "$T/repos"
[ "$have" = 1 ] || { echo "missing=$TO"; exit 6; }
echo "ok=1"
"""


def fields(value: str, n: int) -> list[str]:
    """A tab-separated value as exactly `n` fields ("" for the missing ones)."""
    parts = value.split("\t")
    return (parts + [""] * n)[:n]
