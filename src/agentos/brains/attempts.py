"""One attempt of a brain task, for BrainService: write its prompt, spawn the detached shim, watch
its heartbeat while it runs, and decide what its end means (done, next brain, cool down, re-auth,
failed). The shim owns the CLI process, so an attempt outlives the daemon.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import pwd
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import agentos
from agentos.brains import adapters, confine, launch
from agentos.brains.credentials import brain_ref, child_ref
from agentos.brains.models import (
    BrainKind,
    BrainSpec,
    Outcome,
    Runner,
    TaskRecord,
    TaskStatus,
)
from agentos.brains.transcript import last_seq, read_events, redact_patterns
from agentos.core import fs

log = logging.getLogger("agentos.brains")
MAX_AUTH_FILE = 1_000_000  # a codex auth.json is a few KB
ACTIVE = (TaskStatus.STARTING, TaskStatus.RUNNING)


# the shim's own environment: nothing of agentosd's, and the package it runs from
_SHIM_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
             "PYTHONPATH": str(Path(agentos.__file__).resolve().parent.parent),
             "PYTHONDONTWRITEBYTECODE": "1"}


def _write_private(path: Path, text: str) -> None:
    """Write a file only its owner can read (mode 0600 from the start, never wider)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _spawn_shim(argv: list[str], adir: Path, env: dict[str, str], secrets: dict[str, Any]) -> subprocess.Popen:
    """Start the detached shim and hand it the secrets on stdin (never argv, never disk)."""
    with open(adir / "shim.log", "ab") as logf:
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=logf, stderr=logf, cwd=adir, env=env,
                                start_new_session=True, close_fds=True)
    assert proc.stdin is not None
    try:
        proc.stdin.write(json.dumps(secrets).encode())
        proc.stdin.close()
    except BrokenPipeError:
        pass
    return proc


def _spawn_shim_helper(launcher: launch.Launcher, argv: list[str], adir: Path, env: dict[str, str],
                       secrets: dict[str, Any], unit: str | None) -> launch.Proc:
    """The same, through agentos-runas: the shim runs as agentosd's own user, detached, in a scope
    of its own. The helper's answer is all that comes back; the pid is watched like any shim's."""
    r, w = os.pipe()
    logfd = os.open(adir / "shim.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        proc = launcher.spawn(argv, env=env, cwd=str(adir), stdin=r, stdout=logfd, stderr=logfd, scope=unit)
    finally:
        os.close(r)
        os.close(logfd)
    try:
        with os.fdopen(w, "wb") as fh:
            fh.write(json.dumps(secrets).encode())
    except BrokenPipeError:
        pass
    proc.close()
    return proc


class _AttemptError(Exception):
    def __init__(self, outcome: Outcome, reason: str):
        super().__init__(reason)
        self.outcome = outcome
        self.reason = reason


SPAWN_GRACE = 30.0     # a starting attempt may take this long to write its first status


HEARTBEAT_STALE = 30.0


def _pid_cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        pass
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True,
                             timeout=5, check=False)
        return out.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


class AttemptsMixin:
    """BrainService's attempt methods: start, watch and finish an attempt."""

    async def _fail(self, rec: TaskRecord, reason: str) -> None:
        rec.status = TaskStatus.FAILED
        rec.status_reason = reason
        rec.ended_at = datetime.now(UTC)
        if await self.store.run(self.store.save_task, rec, expect=[TaskStatus.QUEUED, *ACTIVE]):
            self.ports.trace("brain.task", action="failed", task=rec.id, reason=reason)
            await self._cleanup_task(rec)

    def _digest(self, task_id: str, limit_chars: int = 6000) -> str:
        data = read_events(self.attempt_dirs(task_id), since=None, limit=400)
        lines = []
        for ev in data["events"]:
            if ev.get("kind") in ("assistant", "tool_use", "result") and ev.get("text"):
                lines.append(f"[{ev['kind']}] {ev['text']}")
        text = "\n".join(lines)
        return text[-limit_chars:]

    def _attempt_prompt(self, rec: TaskRecord, n: int, spec: BrainSpec, resume_session: str | None,
                        last_reason: str | None) -> str:
        original = (self.task_dir(rec.id) / "prompt.md").read_text()
        if resume_session:
            return ("Continue the task from where you stopped"
                    + (f" (you were interrupted: {last_reason})" if last_reason else "")
                    + ". Check the working directory for the state of your work first, then finish the task.")
        parts = [original]
        if rec.handoff:
            parts.append(rec.handoff)
        if n > 1:
            digest = self._digest(rec.id)
            if digest:
                parts.append("## Progress so far (from an earlier attempt on another brain)\n"
                             "The working directory already holds that attempt's changes. Continue from there; "
                             "do not redo finished steps.\n\n" + digest)
        return "\n\n".join(parts)

    async def _start_attempt(self, rec: TaskRecord, spec: BrainSpec, n: int, *, redact: bool = False) -> None:
        """Start attempt `n` of a task on `spec`: check the CLI and the sign-in, write the brain's
        files, the prompt and launch.json, then spawn the detached shim. Any failure finishes the
        attempt with its outcome, so the task routes onward."""
        adir = self.attempt_dir(rec.id, n)
        try:
            adir.mkdir(parents=True, exist_ok=True)
            os.chmod(adir.parent, 0o700)
            os.chmod(adir, 0o700)
            info = await asyncio.to_thread(self.probe, spec)
            if not info.ok or not info.path:
                raise _AttemptError(Outcome.SPAWN_FAILED, info.reason() or f"{spec.runner} CLI not available")
            credential = await self._attempt_credential(rec, spec)
            token = await self.creds.get(child_ref(rec.id), purpose="brain child grant") if rec.lanes else None
            if token and await self.ports.is_owner_token(token):
                raise _AttemptError(Outcome.SPAWN_FAILED, "refusing to give a brain the owner's own token")
            work, home = await asyncio.to_thread(self._brain_dirs, rec.id)
            resume_session = rec.session_id if rec.session_id and rec.session_brain == spec.name else None
            prompt = self._attempt_prompt_text(rec, n, spec, resume_session, redact=redact)
            try:
                ident = self.brain_identity()  # the probe passed for this identity; the attempt uses it
            except confine.BrainUnconfined as exc:
                raise _AttemptError(Outcome.SPAWN_FAILED, f"{exc.code}: {exc.message}") from None
            user = pwd.getpwnam(ident.name) if ident.name else None
            plan = adapters.plan(
                spec, self.settings, bin_path=info.path, task_id=rec.id, work=work, home=home,
                user=user.pw_name if user else None, credential=credential,
                mcp_url=self.mcp_url() if token else None, child_token=token,
                max_turns=rec.budget.max_turns or self.settings.default_max_turns,
                max_usd=self._remaining_usd(rec, spec), resume_session=resume_session,
                no_tools=rec.requires.no_tools)
            await self._write_plan_files(rec, plan, home)
            prompt_file = adir / "prompt.txt"
            _write_private(prompt_file, prompt)
            helper = ident.helper is not None
            unit = self._attempt_unit(rec, n, ident)
            launch = self._launch_spec(rec, spec, n, plan, work=work, prompt_file=prompt_file, user=user,
                                       ident=ident, unit=unit)
            _write_private(adir / "launch.json", json.dumps(launch, indent=2))
            argv = self._shim_argv(adir, unit if not helper else None)
            self._lane_memory("reserve", rec.id, n)  # a run counts against the lanes' RAM floor (AUD-43)
            secrets = {"env": plan.secret_env, "redact": [v for v in (credential, token) if v]}
            if helper:  # its own scope, so it outlives an agentosd restart (no limits: they are the CLI's)
                proc = await asyncio.to_thread(_spawn_shim_helper, self._launcher("caller"), argv, adir, dict(_SHIM_ENV),
                                               secrets, unit)
            else:
                proc = _spawn_shim(argv, adir, dict(_SHIM_ENV), secrets)
                self._procs[(rec.id, n)] = proc
            del plan, secrets
            await self._mark_running(rec, spec, n, proc.pid, unit=unit, resume=bool(resume_session),
                                     run_as=user.pw_name if user else None, helper=helper)
        except _AttemptError as exc:
            await self._finish_attempt(rec, spec, n, {"outcome": exc.outcome.value, "reason": exc.reason})
        except Exception as exc:
            log.exception("could not start attempt %s/%s", rec.id, n)
            await self._finish_attempt(rec, spec, n, {"outcome": Outcome.SPAWN_FAILED.value,
                                                      "reason": f"could not start: {type(exc).__name__}"})

    async def _attempt_credential(self, rec: TaskRecord, spec: BrainSpec) -> str | None:
        """The brain's stored credential, when it takes one, and the sign-in this attempt runs on (a
        refreshed login is stored back only while that sign-in still holds)."""
        if why := self._cosign_problem(rec.grant, rec.taint, spec):
            raise _AttemptError(Outcome.SPAWN_FAILED, why)
        credential = None
        signin = self._signin(spec.name)
        if spec.credential:
            credential = await self.creds.get(brain_ref(spec.name), purpose=f"brain task {rec.id}")
            if not credential:
                raise _AttemptError(Outcome.AUTH_FAILED, f"no credential stored; run: agentos brains auth {spec.name}")
            if self._signin(spec.name) != signin:  # signed in again while it was read
                raise _AttemptError(Outcome.SPAWN_FAILED, f"{spec.name} was signed in again just now")
        await self.store.run(self.store.set_kv, f"attempt_signin:{rec.id}", signin)
        return credential

    def _attempt_prompt_text(self, rec: TaskRecord, n: int, spec: BrainSpec, resume_session: str | None, *,
                             redact: bool) -> str:
        prev = self.store.attempt(rec.id, n - 1) if n > 1 else None
        prompt = self._attempt_prompt(rec, n, spec, resume_session, prev.get("reason") if prev else None)
        return redact_patterns(prompt) if redact else prompt

    async def _write_plan_files(self, rec: TaskRecord, plan: Any, home: Path) -> None:
        """The files the adapter's plan puts in the brain's home, and its git identity."""
        pairs = await self._git_pairs(rec)
        if pairs:  # a file, not the environment: Claude Code may scrub its Bash tool's environment
            from agentos.git.service import gitconfig_text

            plan.files[str(home / ".gitconfig")] = (gitconfig_text(pairs), 0o600)
        for path, (content, mode) in plan.files.items():
            await asyncio.to_thread(self._write_brain_file, Path(path), content, mode)

    def _attempt_unit(self, rec: TaskRecord, n: int, ident: Any) -> str | None:
        """The systemd scope this attempt runs in, or None when it runs in a plain session."""
        helper = ident.helper is not None
        if helper and self.settings.isolation == "scope" and not ident.scopes:
            raise _AttemptError(Outcome.SPAWN_FAILED, "isolation=scope, and the agentos-runas helper has no systemd")
        scoped = self.settings.isolation != "none" and ident.scopes if helper else self._use_scope()
        return f"agentos-brain-{rec.id}-{n:02d}" if scoped else None

    def _launch_spec(self, rec: TaskRecord, spec: BrainSpec, n: int, plan: Any, *, work: Path, prompt_file: Path,
                     user: Any, ident: Any, unit: str | None) -> dict[str, Any]:
        """launch.json: everything the shim needs to run the CLI, but no secret (those go on stdin)."""
        helper = ident.helper is not None
        metered = spec.kind == BrainKind.METERED
        limits = {
            "max_turns": rec.budget.max_turns, "max_seconds": (rec.budget.max_minutes or 60) * 60,
            "max_tokens": max((rec.budget.max_tokens or 0) - rec.tokens, 1),
            "max_usd": self._remaining_usd(rec, spec),
            "max_output_bytes": self.settings.max_output_bytes, "stall_seconds": self.settings.stall_seconds,
            "grace_seconds": self.settings.cancel_grace_seconds, "max_line_bytes": self.settings.max_line_bytes,
            "price_in_per_mtok": spec.price_in_per_mtok if metered else None,
            "price_out_per_mtok": spec.price_out_per_mtok if metered else None,
        }
        return {
            "task_id": rec.id, "attempt": n, "brain": spec.name, "runner": spec.runner.value,
            "argv": plan.argv, "cwd": str(work), "env": plan.env, "stdin_file": str(prompt_file),
            "run_as": {"uid": user.pw_uid, "gid": user.pw_gid} if user and not helper else None,
            "launcher": {"kind": "helper", "socket": str(ident.helper), "brain_uid": ident.uid,
                         "scope": f"{unit}-cli" if unit else None,
                         "limits": self._scope_limits() if unit else None} if helper else None,
            "limits": limits, "reports_cost": plan.reports_cost,
            "unit": f"{unit}.scope" if unit and not helper else None,
            "rlimits": {"core": 0, "nofile": 8192, "fsize": 8 * 1024 ** 3},
            "transcript": {"seq_start": max(last_seq(self.attempt_dirs(rec.id)), 0) + 1,
                           "segment_bytes": self.settings.transcript_segment_bytes,
                           "max_segments": self.settings.transcript_segments},
        }

    def _shim_argv(self, adir: Path, unit: str | None) -> list[str]:
        """The shim's command line, inside a systemd scope of its own when `unit` is given."""
        argv = [sys.executable, "-m", "agentos.brains.shim", str(adir)]
        if unit:
            argv = ["systemd-run", "--scope", "--quiet", "--collect", f"--unit={unit}",
                    "-p", f"MemoryMax={self.settings.memory_mb}M", "-p", f"TasksMax={self.settings.tasks_max}",
                    "-p", f"CPUQuota={self.settings.cpu_quota_pct}%", "--", *argv]
        return argv

    async def _mark_running(self, rec: TaskRecord, spec: BrainSpec, n: int, pid: int, *, unit: str | None,
                            resume: bool, run_as: str | None, helper: bool) -> None:
        await self.store.run(self.store.update_attempt, rec.id, n, shim_pid=pid, unit=unit)
        rec.status = TaskStatus.RUNNING
        rec.status_reason = f"running on {spec.name} (attempt {n})"
        rec.started_at = rec.started_at or datetime.now(UTC)
        await self.store.run(self.store.save_task, rec, expect=[TaskStatus.STARTING])
        self.ports.trace("brain.attempt", action="start", task=rec.id, attempt=n, brain=spec.name,
                         brain_kind=spec.kind.value, runner=spec.runner.value, resume=resume,
                         isolation="scope" if unit else "session", run_as=run_as,
                         launcher="helper" if helper else "direct")

    def _remaining_usd(self, rec: TaskRecord, spec: BrainSpec) -> float | None:
        if spec.kind != BrainKind.METERED or rec.budget.max_usd is None:
            return None
        return max(round(rec.budget.max_usd - rec.cost_usd, 4), 0.0)

    # ------------------------------------------------------------------ monitoring
    def _shim_alive(self, rec: TaskRecord, attempt: dict[str, Any], status: dict[str, Any] | None) -> bool:
        proc = self._procs.get((rec.id, attempt["n"]))
        if proc is not None:
            return proc.poll() is None
        pid = attempt.get("shim_pid") or (status or {}).get("pid")
        if not pid:
            return False
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        cmd = _pid_cmdline(int(pid))
        return "agentos.brains.shim" in cmd and attempt["dir"] in cmd

    async def _check_running(self, rec: TaskRecord) -> None:
        attempt = self.store.attempt(rec.id, rec.attempt)
        spec = self.store.get_brain(rec.brain) if rec.brain else None
        if attempt is None or spec is None:
            rec.status = TaskStatus.INTERRUPTED
            rec.status_reason = "lost track of the running attempt"
            await self.store.run(self.store.save_task, rec, expect=ACTIVE)
            return
        adir = Path(attempt["dir"])
        status = read_json(adir / "status.json")
        if status and status.get("state") == "exited":
            await self._finish_attempt(rec, spec, attempt["n"], status)
            return
        if self._shim_alive(rec, attempt, status):
            acct = (status or {}).get("accounting") or {}
            await self.store.run(self.seats.note_utilization, spec.name, acct.get("window_utilization"))
            return
        status = read_json(adir / "status.json")  # it may have finished between the two reads
        if status and status.get("state") == "exited":
            await self._finish_attempt(rec, spec, attempt["n"], status)
            return
        started = attempt.get("started_at") or 0
        if rec.status == TaskStatus.STARTING and self.clock() - started < SPAWN_GRACE:
            return
        await self._finish_attempt(rec, spec, attempt["n"], {
            **(status or {}), "outcome": Outcome.INTERRUPTED.value,
            "reason": "the node restarted or the task supervisor died; resumable with transcript context"})

    def _lane_memory(self, action: str, task_id: str, n: int) -> None:
        """Reserve (or release) this attempt's memory with the lane manager, so a new browser lane
        does not warm into the memory a brain run is using."""
        lanes = self.runtime.get("lanes") if self.runtime is not None else None
        try:
            if action == "reserve" and callable(getattr(lanes, "reserve_memory", None)):
                lanes.reserve_memory(f"brain:{task_id}:{n}", int(self.settings.memory_mb))
            elif action == "release" and callable(getattr(lanes, "release_memory", None)):
                lanes.release_memory(f"brain:{task_id}:{n}")
        except Exception:  # noqa: BLE001  (bookkeeping must never stop a run)
            log.warning("could not %s lane memory for brain task %s", action, task_id)

    async def _finish_attempt(self, rec: TaskRecord, spec: BrainSpec, n: int, status: dict[str, Any]) -> None:
        """An attempt ended with `status` (the shim's status.json, or one made up for a failed
        start): account for it once, then requeue the task or end it."""
        self._lane_memory("release", rec.id, n)
        try:
            outcome = Outcome(status.get("outcome") or Outcome.CRASHED.value)
        except ValueError:
            outcome = Outcome.CRASHED
        reason = str(status.get("reason") or outcome.value)[:600]
        claimed = await self._account_attempt(rec, spec, n, status, outcome, reason)
        if claimed is None:
            return  # another process already finalized this attempt
        rec = claimed
        terminal = await self._after_outcome(rec, spec, n, status, outcome, reason)
        if terminal is None:
            rec.status = TaskStatus.QUEUED
            rec.queued_since = datetime.now(UTC)
            await self.store.run(self.store.save_task, rec, expect=[*ACTIVE, TaskStatus.QUEUED])
            # inside schedule() the enclosing loop retries; elsewhere route onward right away
            if rec.prepared and not self._sched_lock.locked():
                await self.schedule(rec.id)
            return
        await self._end_task(rec, spec, terminal)

    async def _account_attempt(self, rec: TaskRecord, spec: BrainSpec, n: int, status: dict[str, Any],
                               outcome: Outcome, reason: str) -> TaskRecord | None:
        """Claim the attempt's end and add its tokens, turns and cost to the seat, the grant and the
        task. None when another process claimed it first."""
        acct = status.get("accounting") or {}
        tokens = int(acct.get("tokens") or 0)
        turns = int(acct.get("turns") or 0)
        cli_cost = acct.get("cost_usd")
        est = status.get("cost_estimate_usd")
        real_cost = float(est if est is not None else (cli_cost or 0)) if spec.kind == BrainKind.METERED else 0.0
        notional = float(cli_cost or 0) if spec.kind != BrainKind.METERED else 0.0
        if not await self.store.run(self.store.claim_attempt, rec.id, n, ended_at=self.clock(), outcome=outcome.value,
                                    reason=reason, tokens=tokens, cost_usd=real_cost, turns=turns):
            return None
        rec = self.store.get_task(rec.id) or rec
        await self.store.run(self.seats.record_usage, spec.name, rec.id, tokens, real_cost)
        await self.ports.charge(rec.spend_grant_id, tokens=tokens, usd=real_cost)  # the grant's day meters
        await self.store.run(self.seats.note_utilization, spec.name, acct.get("window_utilization"))
        rec.tokens += tokens
        rec.turns += turns
        rec.cost_usd = round(rec.cost_usd + real_cost, 6)
        rec.notional_usd = round(rec.notional_usd + notional, 6)
        if acct.get("session_id"):
            rec.session_id, rec.session_brain = acct["session_id"], spec.name
        if status.get("result_text"):
            rec.result_preview = str(status["result_text"])[:4000]
        await self._after_attempt_files(rec, spec)
        self.ports.trace("brain.attempt", action="end", task=rec.id, attempt=n, brain=spec.name,
                         outcome=outcome.value, reason=reason, tokens=tokens, turns=turns,
                         cost_usd=real_cost, notional_usd=notional)
        return rec

    async def _after_outcome(self, rec: TaskRecord, spec: BrainSpec, n: int, status: dict[str, Any],
                             outcome: Outcome, reason: str) -> TaskStatus | None:
        """What the outcome means for the brain and the task: the status the task ends in, or None
        when it routes onward to the next brain. Sets the task's status reason."""
        if outcome == Outcome.SUCCEEDED:
            await self.store.run(self.seats.mark_success, spec.name)
            rec.status_reason = f"finished on {spec.name}; the result is not verified"
            return TaskStatus.SUCCEEDED_UNVERIFIED
        if outcome == Outcome.CANCELLED:
            rec.status_reason = "cancelled"
            return TaskStatus.CANCELLED
        if outcome == Outcome.INTERRUPTED:
            rec.status_reason = reason
            return TaskStatus.INTERRUPTED
        if outcome == Outcome.RATE_LIMITED:
            until = await self.store.run(self.seats.mark_rate_limited, spec, resets_at=status.get("resets_at"),
                                         detail=reason)
            self.ports.trace("brain.seat", action="cooling_down", brain=spec.name, until=until, reason=reason[:200])
            rec.status_reason = f"{spec.name} hit a rate limit; routing onward"
            return None
        if outcome == Outcome.AUTH_FAILED:
            await self.store.run(self.seats.mark_needs_auth, spec.name, reason)
            self._cred_cache.pop(spec.name, None)
            await self._notify_reauth(spec, reason)
            rec.status_reason = f"{spec.name} needs you to sign in again; routing onward"
            return None
        if outcome.retry_elsewhere:
            rec.status_reason = f"{spec.name} attempt {n} {outcome.value}: {reason[:200]}; trying the next brain"
            return None
        rec.status_reason = f"{outcome.value}: {reason}"
        return TaskStatus.FAILED

    async def _end_task(self, rec: TaskRecord, spec: BrainSpec, terminal: TaskStatus) -> None:
        rec.status = terminal
        rec.ended_at = datetime.now(UTC)
        await self.store.run(self.store.save_task, rec, expect=[*ACTIVE, TaskStatus.QUEUED])
        if terminal == TaskStatus.INTERRUPTED:
            await self.ports.notify(
                "A brain task was interrupted",
                f"Task {rec.id} in {rec.workspace} stopped when the node restarted. Resume it with "
                f"brain_resume or: agentos brains tasks resume {rec.id}",
                kind="brain.interrupted", data={"task": rec.id})
            return
        await self._cleanup_task(rec)
        if terminal == TaskStatus.SUCCEEDED_UNVERIFIED and self.settings.notify_on_finish:
            await self.ports.notify("A brain task finished",
                                    f"Task {rec.id} in {rec.workspace} finished on {spec.name} (not verified).",
                                    kind="brain.finished", data={"task": rec.id})

    async def _notify_reauth(self, spec: BrainSpec, reason: str) -> None:
        how = {
            "oauth_token": "On the node run `claude setup-token`, then `agentos brains auth "
                           f"{spec.name} --stdin` and paste the token.",
            "codex_auth": f"On the node run `codex login`, then `agentos brains auth {spec.name} "
                          "--auth-file ~/.codex/auth.json`.",
            "api_key": f"Run `agentos brains auth {spec.name} --stdin` with a working API key.",
        }.get(spec.credential or "", "Check the brain's credential.")
        await self.ports.notify(f"Brain {spec.name} needs you to sign in again",
                                f"{spec.name} rejected its credential ({reason[:160]}). {how}",
                                kind="brain.needs_auth", urgent=True, data={"brain": spec.name})

    async def _after_attempt_files(self, rec: TaskRecord, spec: BrainSpec) -> None:
        """Codex refreshes its ChatGPT login in auth.json: store the refreshed copy back in
        the vault, then remove it from the task home either way."""
        if spec.runner != Runner.CODEX or spec.kind != BrainKind.SUBSCRIPTION:
            return
        auth = self.task_dir(rec.id) / "home" / ".codex" / "auth.json"
        # The brain wrote this file and may have made it (or .codex, or home) a link to a host file
        # such as the vault key or ~/.ssh: read it as the brain user (helper), or without following
        # a link and only as a regular file with one link (a hard link to a host file has two).
        if self._via_helper():
            home_link = auth.parent.parent
            if not home_link.is_symlink():
                return
            auth = Path(os.readlink(home_link)) / ".codex" / "auth.json"

            def read() -> bytes | None:
                return self._brain_files().read(auth)

            def remove() -> None:
                self._brain_files().remove(auth)
        else:
            anchor, rel = self._beneath(auth)

            def read() -> bytes | None:
                return fs.read_beneath(anchor, rel, limit=MAX_AUTH_FILE)

            def remove() -> None:
                fs.unlink_beneath(anchor, rel)
        try:
            raw = await asyncio.to_thread(read)
        except (OSError, launch.BrainFilesError, confine.BrainUnconfined) as exc:
            log.warning("not reading the codex login back from task %s: %s", rec.id, exc)
            raw = None
        if raw is not None:
            try:
                fresh = raw.decode()
                stored = await self.creds.get(brain_ref(spec.name), purpose="refresh check")
                # a sign-in since the attempt started is the owner's newer login: never overwrite it
                same_signin = self.store.get_kv(f"attempt_signin:{rec.id}", "?") == self._signin(spec.name)
                if same_signin and fresh.strip() and fresh != stored and json.loads(fresh).get("tokens"):
                    await self.creds.put(brain_ref(spec.name), fresh, kind="codex_auth",
                                         label=f"brain {spec.name} (openai subscription)",
                                         scope={"node": True, "brain": spec.name, "provider": spec.provider})
                    self.ports.trace("brain.credential", action="refreshed", brain=spec.name)
            except (OSError, ValueError):
                pass
        with contextlib.suppress(OSError, launch.BrainFilesError, confine.BrainUnconfined):
            await asyncio.to_thread(remove)

    async def _cleanup_task(self, rec: TaskRecord) -> None:
        git = self.runtime.get("git") if self.runtime is not None else None
        if git is not None:
            git.task_end(rec.id)  # its git gateway token ends with it
        if rec.lanes:
            await self.creds.delete(child_ref(rec.id))
        await self.ports.revoke(rec.child_grant_id)
        mcp_file = self.task_dir(rec.id) / "home" / "agentos-mcp.json"
        try:
            if self._via_helper():
                if mcp_file.parent.is_symlink():  # readlink only: the target is the brain's, closed to us
                    target = Path(os.readlink(mcp_file.parent)) / mcp_file.name
                    await asyncio.to_thread(self._brain_files().remove, target)
            else:
                anchor, rel = self._beneath(mcp_file)
                fs.unlink_beneath(anchor, rel)  # a home swapped for a link is refused, never followed
        except (OSError, launch.BrainFilesError, confine.BrainUnconfined) as exc:
            log.warning("could not remove %s: %s", mcp_file, exc)
