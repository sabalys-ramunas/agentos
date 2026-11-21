"""Checkpoints inside an environment: a cheap undo for a risky step ("checkpoint, try the big refactor,
roll back if the tests explode"). A mixin of EnvironmentManager.

environment_save saves the whole machine (a provider snapshot: minutes on Hetzner). A checkpoint is
git's, on the machine itself, the same on every provider: for each repository under /work (the ones
the save finds), the work tree as git would commit it (tracked and new files git does not ignore),
written with a temporary index and kept as refs/agentos/checkpoints/<n> with HEAD as its parent
(openajar.environments.checkpoint_scripts). Seconds, no provider call, nothing billed; the agent's
index, stash and branches are never touched, and `git diff refs/agentos/checkpoints/<n>` compares it.

A rollback first checkpoints the work as it is (so it can be undone), then puts each repository back:
the branch it was on to the checkpoint's HEAD, the index to that HEAD, the work tree to the
checkpoint's files, new files git does not ignore removed. Ignored files (node_modules, .env, build
output) are never touched. It is refused, changing nothing, while a repository is mid merge or rebase.

About KEEP numbers stay per environment (the oldest refs go). They never leave the machine: the git
gateway refuses a push of refs/agentos/* (openajar.git.gateway), the save's bundle carries only
branches and stashes, so a snapshot or a paused save keeps them and a files-only resume does not.
environment() lists them from the same command that reads git (probes.GIT_STATE); the owner's API adds
what changed since each one, and one checkpoint's files and patch against now (capped).
"""

from __future__ import annotations

import asyncio
import re
import shlex
import time
from typing import Any

from openajar.core import clock
from openajar.core.errors import OpenajarError
from openajar.environments import checkpoint_scripts as cs
from openajar.environments.errors import (
    CheckpointFailed,
    CheckpointNotFound,
    EnvironmentNotReady,
    RollbackRefused,
)
from openajar.environments.models import Environment

KEEP = 20
TIMEOUT_S = 120.0
NOTE_MAX = 200
DIFF_MAX_BYTES = 200_000     # the patch of one checkpoint against now, all repositories together
DIFF_MAX_FILES = 500         # files listed per repository
SUMMARY_FRESH_S = 15.0       # the owner's page polls: what changed since each checkpoint is read at most this often
SUBJECT_RE = re.compile(r"^\[agentos checkpoint (\d+), (\d+) files?(?:, ([^\]]*))?\] ?(.*)$")
IGNORED = ("Files git ignores (node_modules, .env, build output) are not in a checkpoint, and a rollback "
           "leaves them as they are: after a dependency change, run the install again (like pnpm install)")
STAYS = ("Checkpoints stay on this machine: a push of refs/agentos/* is refused, a snapshot or paused save keeps "
         "them, a files-only resume does not")
BUSY_FIX = {"merge": "git merge --abort", "rebase": "git rebase --abort", "cherry-pick": "git cherry-pick --abort",
            "revert": "git revert --abort"}


def compare_command(n: int) -> str:
    return f"git diff refs/agentos/checkpoints/{n}"


def _lines(text: str) -> list[tuple[str, str]]:
    out = []
    for line in (text or "").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.isidentifier():
            out.append((key, value))
    return out


def _one_line(note: str | None) -> str:
    return " ".join(str(note or "").split())[:NOTE_MAX]


def listed(repos: list[dict] | None) -> list[dict]:
    """The checkpoints GIT_STATE read (each repository's `checkpoint=` lines), newest first:
    {id, note, at, auto (the rollback it was made before, or None), repos: [{dir, head, files}]}."""
    by_id: dict[int, dict] = {}
    for repo in repos or []:
        for value in repo.get("checkpoints") or []:
            ref, parent, at, subject = cs.fields(value, 4)
            m = SUBJECT_RE.match(subject)
            if not ref.isdigit() or m is None:
                continue
            row = by_id.setdefault(int(ref), {"id": int(ref), "note": m.group(4) or None, "at": at or None,
                                              "auto": m.group(3) or None, "repos": []})
            row["repos"].append({"dir": repo["dir"], "head": parent[:12] or None, "files": int(m.group(2))})
    return [by_id[n] for n in sorted(by_id, reverse=True)]


def _repo_row(fields: list[str]) -> dict:
    d, _commit, head, branch, files = fields
    return {"dir": d, "head": head[:12] or None, "branch": branch or None,
            "files_changed": int(files) if files.isdigit() else 0}


def _rolled_row(fields: list[str]) -> dict:
    d, head, branch, was_head, was_branch, branch_was, restored, removed, undone = fields
    row: dict = {"dir": d, "branch": branch or None, "head": head[:12] or None,
                 "files_restored": int(restored or 0), "files_removed": int(removed or 0),
                 "was": {"branch": was_branch or None, "head": was_head[:12] or None}}
    if undone and undone != "0":
        row["commits_undone"] = int(undone)
    if branch and branch_was and branch_was != head and branch_was != was_head:
        row["branch_was_at"] = branch_was[:12]  # another branch moved back: its old commit, to keep it
    return row


class Checkpoints:
    """For EnvironmentManager (envs, _machine_root, store, clock and record_call)."""

    def _checkpoint_lock(self, env_id: str) -> asyncio.Lock:
        locks = self.__dict__.setdefault("_checkpoint_locks", {})
        return locks.setdefault(env_id, asyncio.Lock())

    async def _checkpoint_script(self, env: Environment, script: str, *args: str, probe: bool = False) -> Any:
        """Run one of checkpoint_scripts on the machine. `probe`: the owner's page reading it, which is
        no use of the machine (Activity.probe_exec)."""
        if not env.open or env.state != "ready" or not env.vm_id:
            raise EnvironmentNotReady(f"environment {env.id} is {env.state}; checkpoints need it ready "
                                      "(environment(on=...) waits for that)")
        cmd = shlex.join(["bash", "-s", "--", self._machine_root(env), "work", str(KEEP), *args])
        run = self.probe_exec if probe else self.envs().exec
        try:
            return await run(env.terminal_lane, cmd, stdin=script, timeout_s=TIMEOUT_S)
        except OpenajarError as e:
            raise CheckpointFailed(f"environment {env.id}'s machine did not answer ({e.message}), so nothing "
                                   "changed. Call environment(on=...) and try again") from None

    @staticmethod
    def _failed(res: Any, what: str) -> CheckpointFailed:
        found = dict(_lines(res.stdout))
        why = found.get("error") or (res.stderr or res.stdout or "").strip()[-300:] or f"exit {res.exit_code}"
        fix = (" Checkpoints are git's: git init a repository in /work, or use environment_save for the whole "
               "machine" if "no git repository" in why else "")
        return CheckpointFailed(f"{what} failed on the machine: {why}. Nothing changed.{fix}")

    async def checkpoint(self, env: Environment, note: str | None = None) -> dict:
        """Checkpoint every repository under /work now (environment_checkpoint)."""
        note = _one_line(note)
        async with self._checkpoint_lock(env.id):
            res = await self._checkpoint_script(env, cs.CHECKPOINT, note)
        lines = _lines(res.stdout)
        found = dict(lines)
        if res.exit_code != 0 or "ok" not in found or not found.get("id", "").isdigit():
            raise self._failed(res, "the checkpoint")
        n = int(found["id"])
        repos = [_repo_row(cs.fields(v, 5)) for k, v in lines if k == "made"]
        return {"checkpoint": n, "note": note or None, "at": clock.iso_or_none(self._clock(), z=True),
                "repos": repos, "compare": compare_command(n),
                "rollback": f"environment_rollback(checkpoint={n})",
                "ignored": IGNORED, "stays": STAYS}

    async def rollback(self, env: Environment, checkpoint: int) -> dict:
        """Put every repository back to `checkpoint`, after an automatic checkpoint of now (environment_rollback)."""
        if isinstance(checkpoint, bool) or not isinstance(checkpoint, int) or checkpoint < 1:
            from openajar.core.errors import InvalidRequest

            raise InvalidRequest("checkpoint is a checkpoint's number, like 3 (environment() lists them)")
        async with self._checkpoint_lock(env.id):
            res = await self._checkpoint_script(env, cs.ROLLBACK, str(checkpoint))
        lines = _lines(res.stdout)
        found = dict(lines)
        if res.exit_code == 6:
            raise CheckpointNotFound(f"no repository under /work has checkpoint {checkpoint}, so nothing changed. "
                                     "environment(on=...) lists the checkpoints there are (about 20 are kept, and a "
                                     "files-only resume keeps none)")
        if res.exit_code == 7:
            busy = [cs.fields(v, 2) for k, v in lines if k == "busy"]
            what = "; ".join(f"{d} is in the middle of a {w} (finish it, or {BUSY_FIX.get(w, 'abort it')} there)"
                             for d, w in busy)
            raise RollbackRefused(f"{what}. Nothing changed: call environment_rollback again after that")
        if res.exit_code != 0 or "ok" not in found or not found.get("undo", "").isdigit():
            raise self._failed(res, "the rollback")
        undo = int(found["undo"])
        rolled = [_rolled_row(cs.fields(v, 9)) for k, v in lines if k == "rolled"]
        failed = [dict(zip(("dir", "why"), cs.fields(v, 2), strict=True)) for k, v in lines if k == "failed"]
        out: dict = {"checkpoint": checkpoint, "repos": rolled,
                     "untouched": [v for k, v in lines if k == "untouched"],
                     "undo": {"checkpoint": undo, "rollback": f"environment_rollback(checkpoint={undo})"},
                     "ignored": IGNORED}
        if failed:
            out["failed"] = failed
        return out

    async def checkpoint_summaries(self, env: Environment, rows: list[dict]) -> list[dict]:
        """`rows` (listed()) with `diff` in each repository: what changed since that checkpoint, against the
        work tree now ({files_added, files_changed, files_removed, lines_added, lines_removed}). Read at
        most every SUMMARY_FRESH_S per environment; without an answer the rows come back as they are."""
        if not rows:
            return rows
        cache = self.__dict__.setdefault("_checkpoint_sums", {})
        key = (env.id, tuple(r["id"] for r in rows))
        got = cache.get(env.id)
        if got is None or got[0] != key or time.monotonic() - got[1] > SUMMARY_FRESH_S:
            try:
                res = await self._checkpoint_script(env, cs.SUMMARY, probe=True)
            except OpenajarError:
                return rows
            sums: dict[tuple[str, int], dict] = {}
            for k, v in _lines(res.stdout):
                if k != "sum":
                    continue
                d, n, files, lines = cs.fields(v, 4)
                a, m, r = (files.split() + ["0", "0", "0"])[:3]
                la, lr = (lines.split() + ["0", "0"])[:2]
                if n.isdigit():
                    sums[(d, int(n))] = {"files_added": int(a), "files_changed": int(m), "files_removed": int(r),
                                         "lines_added": int(la), "lines_removed": int(lr)}
            got = (key, time.monotonic(), sums)
            cache[env.id] = got
        sums = got[2]
        return [{**r, "repos": [{**p, "diff": sums.get((p["dir"], r["id"]))} for p in r["repos"]]} for r in rows]

    async def checkpoint_diff(self, env: Environment, checkpoint: int) -> dict:
        """One checkpoint against the work tree now, for the owner's diff view: per repository its files
        (path, status, lines added and removed) and the patch, at most DIFF_MAX_BYTES in all."""
        res = await self._checkpoint_script(env, cs.DIFF, str(checkpoint), str(DIFF_MAX_BYTES), str(DIFF_MAX_FILES),
                                            probe=True)
        if res.exit_code == 6:
            raise CheckpointNotFound(f"environment {env.id} has no checkpoint {checkpoint}")
        if res.exit_code != 0:
            raise self._failed(res, "reading the checkpoint")
        return {"checkpoint": checkpoint, "repos": parse_diff(res.stdout), "max_bytes": DIFF_MAX_BYTES,
                "max_files": DIFF_MAX_FILES}


def parse_diff(text: str) -> list[dict]:
    """DIFF's sections: [{dir, files: [{path, status, lines_added, lines_removed}], files_total, patch,
    patch_bytes, truncated}]."""
    repos: list[dict] = []
    cur: dict | None = None
    part = ""
    status: list[tuple[str, str]] = []
    numstat: dict[str, tuple[str, str]] = {}
    patch: list[str] = []
    for line in text.splitlines(keepends=True):
        bare = line.rstrip("\n")
        if bare.startswith("::repo "):
            cur, part, status, numstat, patch = {"dir": bare[7:], "files_total": 0}, "", [], {}, []
            continue
        if cur is None:
            continue
        if bare.startswith("::files "):
            cur["files_total"] = int(bare[8:] or 0) if bare[8:].isdigit() else 0
        elif bare in ("::status", "::numstat"):
            part = bare[2:]
        elif bare.startswith("::patch "):
            part = "patch"
            cur["patch_bytes"] = int(bare[8:]) if bare[8:].isdigit() else 0
        elif bare == "::end":
            text_patch = "".join(patch)
            text_patch = text_patch[:-1] if text_patch.endswith("\n\n") else text_patch
            cur["files"] = [{"path": p, "status": s, "lines_added": _num(numstat.get(p, ("", ""))[0]),
                             "lines_removed": _num(numstat.get(p, ("", ""))[1])} for s, p in status]
            cur["patch"] = text_patch
            cur["truncated"] = cur.get("patch_bytes", 0) > len(text_patch.encode()) or cur["files_total"] > len(status)
            repos.append(cur)
            cur = None
        elif part == "patch":
            patch.append(line)
        elif part == "status" and "\t" in bare:
            s, p = bare.split("\t", 1)
            status.append((s[:1], p))
        elif part == "numstat" and bare.count("\t") >= 2:
            a, r, p = bare.split("\t", 2)
            numstat[p] = (a, r)
    return repos


def _num(v: str) -> int | None:
    """A numstat count, or None for a binary file ("-")."""
    return int(v) if v.isdigit() else None
