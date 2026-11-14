"""The freeze service (runtime service "freeze"): the break-glass off switch.

    build(runtime)                          -> FreezeService, registered as "freeze"
    freeze(scope, target, reason=, by=)     -> what it stopped, revoked, cancelled and paused
    unfreeze(scope, target, by=)            the caller has proved it is the owner (see below)
    status()                                every frozen scope, and the latest journal entries
    gate / check_call / guard               where agents are stopped (see "Where agents stop")

One freeze, in one step, for a lane, a workspace, this node, or (from the controller) the mesh:

1. The flag is set in memory and written to its own file (agentos.freeze.store) before anything
   is awaited, so from the moment freeze() is called no agent action starts in the scope.
2. Agent work in flight is interrupted at its next await: calls inside LaneManager.using() on the
   scope's lanes, and MCP calls on them (or every MCP call, for a node or mesh freeze). The
   interrupted call ends with `frozen`. freeze() waits up to `stop_wait_s` for them and reports
   how long the last one took (`stopped.ms`).
3. Grants are revoked by how far they reach (PolicyService.revoke_reaching), with what was narrowed
   from them and the standing approvals over the scope. A node or mesh freeze revokes every agent
   grant. A lane or workspace freeze revokes only the grants whose whole reach is inside it; a wider
   grant (a workspace grant during a lane freeze, `*`) is kept for the lanes outside, and the gate
   below refuses every call it makes inside the scope until the unfreeze. A person's grant is kept.
4. Runs on the scope are cancelled; schedules, triggers (event routes) and brain tasks that touch
   it are paused or cancelled. Nothing comes back by itself: unfreezing restores no grant and
   resumes nothing; the owner grants and resumes again.
5. Browsers and profiles are left alone: a warm lane stays warm (it is never reaped or evicted
   while frozen), nothing is closed or deleted, no cookie is cleared, and a human can still take
   control in the viewer and look.
6. The freeze is journaled (freeze/journal.jsonl), traced (kind "freeze": who, why, what was
   revoked), and pushed to the owner.

Freezing again is harmless: the record stays, and the sweep (step 2 to 4) runs again, which also
catches a grant issued since. Every step is wrapped: a wedged browser, a full disk or a policy
database that cannot write never undoes step 1, and the report lists what failed.

Where agents stop: `gate` is a LaneManager gate (every non-human caller of open, close and using);
`guard` wraps every MCP call in the policy wrapper (the owner's agent token included);
`check_call` runs inside PolicyService.authorize_call for every non-person call that names a lane,
and refuses everything during a node or mesh freeze. A call that names a workspace (its `workspace`
argument, a memory call) is refused during a freeze of that workspace. These checks do not look at
the grant: every agent grant, wide or narrow, is stopped inside a frozen scope. lane_freeze, lane_control and whoami stay
callable, so an agent can still freeze more and see why it is stopped.

Unfreeze is owner only and never restores a grant. The CLI on the node (`agentos unfreeze`), a
signed-in owner panel session (with a fresh passkey check when the node has members), and the
controller for its own mesh freeze may call it. No MCP tool unfreezes, and no bearer token does.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import logging
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from agentos.core.config import home_from_env
from agentos.core.errors import InvalidRequest, LaneNotFound, NotFound
from agentos.core.models import RunStatus, parse_lane_id
from agentos.freeze.errors import Frozen
from agentos.freeze.store import MAX_REASON, SCOPES, FreezeRecord, FreezeStore, parse_key, scope_key

log = logging.getLogger("agentos.freeze")

SERVICE_NAME = "freeze"
POLL_S = 1.0                 # the freeze directory is read again this often (a CLI freeze while the API is down)
STOP_WAIT_S = 3.0            # freeze() waits this long for interrupted calls to stop
LINK_TTL_S = 1800            # a push's Freeze link works for 30 minutes, once
EXEMPT_TOOLS = frozenset({"lane_freeze", "lane_control", "whoami"})
ACTIVE_RUNS = (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_APPROVAL)
OPEN_BRAIN_TASKS = frozenset({"queued", "starting", "running", "waiting_approval", "interrupted"})


@dataclass(frozen=True)
class _Call:
    tool: str
    lane_id: str | None
    workspace: str | None
    chain: frozenset[str]


def clean_reason(reason: str | None) -> str:
    text = " ".join(str(reason or "").split())
    return text[:MAX_REASON]


class FreezeService:
    def __init__(self, runtime: Any, *, home: Path | None = None, clock: Any = time.time,
                 poll_s: float = POLL_S, stop_wait_s: float = STOP_WAIT_S):
        cfg = runtime.get("config") if runtime is not None else None
        self.runtime = runtime
        self.home = Path(home) if home is not None else (cfg.home if cfg is not None else home_from_env())
        self.node_name = str(getattr(cfg, "name", None) or "node")
        self.clock = clock
        self.poll_s = poll_s
        self.stop_wait_s = stop_wait_s
        self.store = FreezeStore(self.home)
        self._records: dict[str, FreezeRecord] = {}
        self._memory_only: set[str] = set()   # frozen, but the disk refused the marker
        self._calls: dict[asyncio.Task, _Call] = {}
        self._task: asyncio.Task | None = None
        self._sweeps: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()
        self._link_key: bytes | None = None
        self._used_links: set[str] | None = None
        self.problem: str | None = None
        self.reload()
        self.install()

    # ================================================================ wiring
    def install(self) -> None:
        """Close the lane gate and add the Freeze button to pushes. Called at build time, before
        any service starts, so nothing on this node runs ahead of a freeze that is on disk."""
        lanes = self._svc("lanes")
        add = getattr(lanes, "add_gate", None)
        if callable(add):
            add(self.gate, keep_warm=self.keeps_warm)
        notifier = self._svc("notifier")
        if notifier is not None and hasattr(notifier, "freeze_link"):
            notifier.freeze_link = self.push_link

    async def start(self) -> None:
        self.install()
        pending = [r for r in self._records.values() if r.applied_at is None]
        for rec in pending:  # written by the CLI while the daemon was down: run the sweep now
            await self._sweep_and_record(rec, by=rec.by, reason=rec.reason)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll(), name="agentos-freeze-poll")

    async def stop(self) -> None:
        for task in [self._task, *self._sweeps]:
            if task is not None:
                task.cancel()
        for task in [self._task, *self._sweeps]:
            if task is not None:
                with contextlib.suppress(BaseException):
                    await task
        self._task = None

    def _svc(self, name: str) -> Any:
        return self.runtime.get(name) if self.runtime is not None else None

    def _close_live(self) -> None:
        """Live streams (the panel's event stream, viewer and terminal) recheck now and close where the
        freeze covers them (agentos.core.live)."""
        poke = getattr(self._svc("live_sockets"), "poke", None)
        if callable(poke):
            poke()

    # ================================================================ state
    def reload(self) -> tuple[list[FreezeRecord], list[FreezeRecord]]:
        """Read the freeze directory again. Returns (new, gone): scopes frozen or unfrozen on disk by
        someone else (the CLI). A scope frozen in memory only stays frozen."""
        found, problem = self.store.load()
        if problem and problem != self.problem:
            log.error("freeze: %s; the whole node is frozen until it is fixed", problem)
        self.problem = problem
        before = self._records
        merged: dict[str, FreezeRecord] = {}
        for key, rec in found.items():
            old = before.get(key)
            # a scope we already hold keeps its record (and the sweep's results); the disk adds new ones
            merged[key] = old if old is not None and (old.readable or not rec.readable) else rec
        for key in self._memory_only:
            if key in before and key not in merged:
                merged[key] = before[key]
        self._records = merged
        new = [r for k, r in merged.items() if k not in before]
        gone = [r for k, r in before.items() if k not in merged]
        return new, gone

    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(self.poll_s)
            try:
                new, gone = self.reload()
                if new:
                    self._close_live()
                for rec in new:
                    log.warning("freeze: %s %s was frozen on disk (%s); stopping agents there", rec.scope,
                                rec.target, rec.reason or "no reason")
                    if rec.applied_at is None:
                        self._spawn(self._sweep_and_record(rec, by=rec.by, reason=rec.reason))
                for rec in gone:
                    log.warning("freeze: %s %s was unfrozen on disk", rec.scope, rec.target)
                    self._trace("unfreeze", rec, by="owner (node cli)", summary=f"unfroze {rec.scope} {rec.target}")
            except Exception:
                log.exception("freeze: reading the freeze directory failed")

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._sweeps.add(task)
        task.add_done_callback(self._sweeps.discard)

    def records(self) -> list[FreezeRecord]:
        return sorted(self._records.values(), key=lambda r: (SCOPES.index(r.scope), r.target))

    def frozen_for(self, lane_id: str | None, workspace: str | None = None) -> FreezeRecord | None:
        """The freeze that covers this lane or this workspace (or, with neither, a node or mesh
        freeze), or None."""
        for key in ("mesh", "node"):
            rec = self._records.get(key)
            if rec is not None:
                return rec
        for ws in (workspace, lane_id.split("/", 1)[0] if isinstance(lane_id, str) and lane_id else None):
            rec = self._records.get(f"workspace.{ws}") if isinstance(ws, str) and ws else None
            if rec is not None:
                return rec
        if not lane_id or not isinstance(lane_id, str):
            return None
        try:
            return self._records.get(scope_key("lane", lane_id))
        except ValueError:
            return None

    def is_frozen(self, lane_id: str | None = None) -> bool:
        return self.frozen_for(lane_id) is not None

    def keeps_warm(self, lane_id: str) -> bool:
        return self.frozen_for(lane_id) is not None

    def message(self, lane_id: str | None, rec: FreezeRecord) -> str:
        where = "the whole mesh" if rec.scope == "mesh" else (
            "this node" if rec.scope == "node" else f"{rec.scope} {rec.target}")
        what = lane_id or where
        why = f": {rec.reason}" if rec.reason else ""
        if rec.scope in ("node", "mesh"):
            return (f"{what} is frozen ({where}{why}). Every agent is stopped and every agent grant was revoked. "
                    "Do not retry; tell your user. Only the owner can unfreeze it, and agents then need a new grant.")
        return (f"{what} is frozen ({where}{why}). Every agent is stopped there, whatever its grant, until the "
                "owner unfreezes it; grants that reached only that were revoked. Do not retry and do not try to "
                "reach it another way; tell your user.")

    # ================================================================ where agents stop
    def gate(self, lane_id: str, principal_kind: str = "agent") -> None:
        """LaneManager gate: refuse a non-human caller on a frozen lane."""
        rec = self.frozen_for(lane_id)
        if rec is not None:
            raise Frozen(self.message(lane_id, rec))

    def check_call(self, lane_id: str | None = None, *, tool: str | None = None,
                   workspace: str | None = None) -> None:
        """Refuse an agent's call on a frozen lane or workspace, or any call during a node or mesh
        freeze. Whatever the caller's grant: a wide grant kept by a lane freeze is refused here too."""
        if tool in EXEMPT_TOOLS:
            return
        rec = self.frozen_for(lane_id, workspace)
        if rec is not None:
            raise Frozen(self.message(lane_id or (workspace if rec.scope == "workspace" else None), rec))

    @contextmanager
    def guard(self, *, tool: str, lane_id: str | None, chain: Any = (),
              workspace: str | None = None) -> Iterator[None]:
        """Around one MCP call: refuse it on a frozen scope, and track it so a freeze can interrupt it
        in flight (it then fails with `frozen`)."""
        if tool in EXEMPT_TOOLS:
            yield
            return
        workspace = workspace if isinstance(workspace, str) and workspace else None
        self.check_call(lane_id, tool=tool, workspace=workspace)
        task = asyncio.current_task()
        if task is None:
            yield
            return
        self._calls[task] = _Call(tool, lane_id, workspace, frozenset(chain or ()))
        try:
            yield
        except asyncio.CancelledError:
            reason = self._take(task)
            if reason is None:
                raise
            rec = self.frozen_for(lane_id, workspace)
            raise Frozen(self.message(lane_id, rec) if rec else f"{tool} was stopped: {reason}") from None
        finally:
            self._calls.pop(task, None)

    def _interrupt(self, task: asyncio.Task, reason: str) -> bool:
        lanes = self._svc("lanes")
        fn = getattr(lanes, "interrupt_task", None)
        if callable(fn):
            return bool(fn(task, reason))
        from agentos.node import interrupts  # a runtime without the lane manager (tests, the controller)

        return interrupts.interrupt(task, reason)

    def _take(self, task: asyncio.Task) -> str | None:
        lanes = self._svc("lanes")
        fn = getattr(lanes, "take_interrupt", None)
        if callable(fn):
            return fn(task)
        from agentos.node import interrupts

        return interrupts.take(task)

    # ================================================================ freeze
    def _normalize(self, scope: str, target: str | None) -> tuple[str, str]:
        scope = str(scope or "").strip().lower()
        if scope not in SCOPES:
            raise InvalidRequest(f"scope is one of {', '.join(SCOPES)}")
        target = (target or "").strip()
        if scope == "mesh":
            return scope, "mesh"
        if scope == "node":
            if target and target not in (self.node_name, "node", "this"):
                raise InvalidRequest(f"this node is {self.node_name!r}; freeze another node on that node or "
                                     "through the controller")
            return scope, self.node_name
        if scope == "workspace":
            try:
                parse_lane_id(f"{target}/x")
            except ValueError:
                raise InvalidRequest("a workspace id is lowercase letters, digits and dashes") from None
            return scope, target
        try:
            parse_lane_id(target)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from None
        return scope, target

    def _key(self, scope: str, target: str) -> str:
        return "node" if scope == "node" else scope_key(scope, target)

    async def freeze(self, scope: str, target: str | None = None, *, reason: str, by: str, via: str = "api",
                     source: str = "local", sweep: bool = True) -> dict[str, Any]:
        """Freeze a scope (see the module docstring). The gate is closed before the first await."""
        t0 = time.monotonic()
        scope, target = self._normalize(scope, target)
        text = clean_reason(reason) or "no reason given"
        if scope == "lane":
            self._require_lane(target)
        rec, already = self.mark(scope, target, reason=text, by=by, via=via, source=source)
        report: dict[str, Any] = {"freeze_id": rec.id, "scope": scope, "target": target, "reason": rec.reason,
                                  "by": rec.by, "frozen_at": rec.public()["at"], "already_frozen": already,
                                  "durable": rec.durable, "details_saved": rec.detail_saved}
        if not rec.durable:
            report["warning"] = ("the disk refused the freeze record: it holds until the daemon restarts. Free "
                                 "some disk space and freeze again.")
        if sweep:
            report.update(await self._sweep_and_record(rec, by=by, reason=text, t0=t0))
        report["took_ms"] = round((time.monotonic() - t0) * 1000, 1)
        report["next"] = (("Every agent is stopped, and every agent grant was revoked: agents must be granted "
                           "again after an unfreeze." if scope in ("node", "mesh") else
                           "Every agent is stopped there, whatever its grant. Grants that reached only that were "
                           "revoked and must be given again after an unfreeze; wider grants keep working elsewhere.")
                          + " A human can still take control in the viewer and look; nothing was closed or deleted.")
        return report

    def mark(self, scope: str, target: str, *, reason: str, by: str, via: str = "api", source: str = "local",
             freeze_id: str | None = None) -> tuple[FreezeRecord, bool]:
        """Set the flag, synchronously (memory first, then its file): from the moment this returns
        the gate refuses every agent in the scope. Returns (record, already_frozen)."""
        key = self._key(scope, target)
        rec = self._records.get(key)
        if rec is not None and rec.readable:
            return rec, True
        rec = FreezeRecord(scope, target, clean_reason(reason) or "no reason given", clean_reason(by)[:80] or "unknown",
                           self.clock(), via=via, source=source)
        if freeze_id:
            rec.id = freeze_id[:40]
        self._records[key] = rec  # from here on the gate refuses every agent in the scope
        self._close_live()
        rec.durable, rec.detail_saved = self.store.put(rec)
        if rec.durable:
            self._memory_only.discard(key)
        else:
            self._memory_only.add(key)
        return rec, False

    def apply_mesh(self, body: dict[str, Any]) -> str:
        """The controller's signed mesh freeze state (live, or in the welcome before anything else runs on
        this node). {frozen: true, reason, by, freeze_id} freezes this node as part of the mesh; {frozen:
        false} lifts a mesh freeze the controller set (never a freeze made on this node)."""
        if body.get("frozen") is True:
            by = f"controller:{clean_reason(str(body.get('by') or 'owner'))[:60]}"
            freeze_id = str(body.get("freeze_id") or "")[:40]
            rec, already = self.mark("mesh", "mesh", reason=str(body.get("reason") or "mesh freeze"), by=by,
                                     via="controller", source="mesh", freeze_id=freeze_id)
            renewed = already and bool(freeze_id) and rec.id != freeze_id and rec.source == "mesh"
            if renewed:
                rec.id = freeze_id  # the controller froze the mesh again: this is its freeze now
                if rec.durable and rec.detail_saved:
                    self.store.update(rec)
            # like a local re-freeze, every mesh freeze runs the sweep again: it catches grants issued since.
            # The owner is pushed once per freeze, not at every reconnect's welcome.
            self._spawn(self._sweep_and_record(rec, by=by, reason=rec.reason,
                                               notify=not already or renewed or rec.applied_at is None))
            return "frozen"
        if body.get("frozen") is False:
            rec = self._records.get("mesh")
            if rec is None or rec.source != "mesh":
                return "not_frozen"
            try:
                self.store.remove("mesh")
            except OSError:
                log.error("freeze: the controller lifted the mesh freeze, but its file cannot be deleted")
                return "kept"
            self._records.pop("mesh", None)
            self._memory_only.discard("mesh")
            self.store.journal({"event": "unfreeze", "freeze_id": rec.id, "scope": "mesh", "target": "mesh",
                                "by": f"controller:{clean_reason(str(body.get('by') or 'owner'))[:60]}",
                                "via": "controller"})
            self._trace("unfreeze", rec, by="controller", summary="the controller lifted its mesh freeze")
            return "unfrozen"
        raise InvalidRequest("mesh_freeze needs frozen: true or false")

    def _require_lane(self, lane_id: str) -> None:
        lanes = self._svc("lanes")
        if lanes is None:
            return
        try:
            lanes.get(lane_id)
        except (LaneNotFound, NotFound):
            raise
        except Exception:  # noqa: BLE001  (a broken lane store: freeze anyway, toward frozen)
            log.warning("freeze: could not look up %s; freezing it anyway", lane_id)

    def lanes_in(self, scope: str, target: str) -> list[str]:
        lanes = self._svc("lanes")
        try:
            ids = [i.spec.id for i in lanes.list()] if lanes is not None else []
        except Exception:  # noqa: BLE001
            ids = []
        if scope == "lane":
            return [target]
        if scope == "workspace":
            return [i for i in ids if i.split("/", 1)[0] == target]
        return ids

    def _in_scope(self, rec: FreezeRecord, lane_id: str | None, workspace: str | None = None) -> bool:
        if rec.scope in ("node", "mesh"):
            return True
        if rec.scope == "workspace" and workspace == rec.target:
            return True
        if not lane_id:
            return False
        if rec.scope == "workspace":
            return lane_id.split("/", 1)[0] == rec.target
        return lane_id == rec.target

    async def _sweep_and_record(self, rec: FreezeRecord, *, by: str, reason: str,
                                t0: float | None = None, notify: bool = True) -> dict[str, Any]:
        async with self._lock:
            out = await self._sweep(rec, by=by, reason=reason, t0=t0 if t0 is not None else time.monotonic())
        rec.applied_at = self.clock()
        rec.revoked = sorted({*rec.revoked, *(g["id"] for g in out["revoked"]["grants"])})[:1000]
        if rec.durable and rec.detail_saved and self._records.get(rec.key) is rec:
            self.store.update(rec)  # never writes back a marker the CLI deleted meanwhile
        entry = {"event": "freeze", "freeze_id": rec.id, "scope": rec.scope, "target": rec.target,
                 "reason": rec.reason, "by": by, "via": rec.via, "source": rec.source, "durable": rec.durable,
                 "revoked": out["revoked"], "stopped": out["stopped"], "runs_cancelled": out["runs_cancelled"],
                 "schedules_paused": out["schedules_paused"], "triggers_paused": out["triggers_paused"],
                 "brain_tasks_cancelled": out["brain_tasks_cancelled"], "errors": out["errors"]}
        self.store.journal(entry)
        n = len(out["revoked"]["grants"]) + len(out["revoked"]["cascades_to"])
        self._trace("freeze", rec, by=by, summary=(f"froze {rec.scope} {rec.target}: {rec.reason}; stopped "
                                                   f"{out['stopped']['calls']} calls, revoked {n} grants"),
                    detail=entry)
        if notify:
            self._notify(rec, by=by)
        return out

    async def _sweep(self, rec: FreezeRecord, *, by: str, reason: str, t0: float) -> dict[str, Any]:
        errors: list[str] = []
        why = f"frozen by {by}: {reason}"[:200]
        lanes_in = self.lanes_in(rec.scope, rec.target)
        # 1. interrupt what runs in the scope now
        interrupted = self._interrupt_scope(rec, lanes_in, why, errors)
        # 2. revoke every grant that reaches it (and its children, its standing approvals)
        # on policy.db's worker: every revocation is committed before the sweep goes on, and a
        # CLI holding the lock never stalls the event loop meanwhile (the gate is already shut)
        db = getattr(self._svc("policy"), "db", None)
        if callable(getattr(db, "run", None)):
            revoked = await db.run(self._revoke, rec, lanes_in, by=by, reason=why, errors=errors)
        else:
            revoked = self._revoke(rec, lanes_in, by=by, reason=why, errors=errors)
        gone = {g["id"] for g in revoked["grants"]} | set(revoked["cascades_to"])
        for task, call in list(self._calls.items()):  # calls under a grant that just died
            if call.chain & gone and task not in interrupted and self._interrupt(task, why):
                interrupted[task] = None
        # 3. runs, schedules, triggers, brain tasks
        runs = await self._cancel_runs(rec, lanes_in, by, errors)
        schedules = self._pause_schedules(rec, errors)
        triggers = self._pause_triggers(rec, why, errors)
        tasks = await self._cancel_brain_tasks(rec, errors)
        stopped = await self._wait_stopped(interrupted, t0)
        warm = self._warm(lanes_in)
        return {"lanes": lanes_in, "lanes_warm": warm, "stopped": stopped, "revoked": revoked,
                "runs_cancelled": runs, "schedules_paused": schedules, "triggers_paused": triggers,
                "brain_tasks_cancelled": tasks, "errors": errors}

    def _interrupt_scope(self, rec: FreezeRecord, lanes_in: list[str], why: str,
                         errors: list[str]) -> dict[asyncio.Task, float | None]:
        out: dict[asyncio.Task, float | None] = {}
        lanes = self._svc("lanes")
        stop = getattr(lanes, "interrupt_agents", None)
        if callable(stop):
            for lane_id in lanes_in:
                try:
                    for task in stop(lane_id, why):
                        out[task] = None
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"could not interrupt the work on {lane_id} ({type(exc).__name__})")
        for task, call in list(self._calls.items()):
            if task in out or task.done():
                continue
            if self._in_scope(rec, call.lane_id, call.workspace) and self._interrupt(task, why):
                out[task] = None
        return out

    async def _wait_stopped(self, tasks: dict[asyncio.Task, float | None], t0: float) -> dict[str, Any]:
        if not tasks:
            return {"calls": 0, "ms": 0.0, "still_running": 0}
        done_at: dict[asyncio.Task, float] = {}
        for task in tasks:
            if task.done():
                done_at[task] = time.monotonic()
            else:
                task.add_done_callback(lambda t: done_at.setdefault(t, time.monotonic()))
        pending = [t for t in tasks if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=self.stop_wait_s)
        still = [t for t in tasks if not t.done()]
        last = max(done_at.values()) if done_at else time.monotonic()
        out = {"calls": len(tasks), "ms": round((last - t0) * 1000, 1), "still_running": len(still)}
        if still:
            out["note"] = (f"{len(still)} call(s) did not stop within {self.stop_wait_s:g}s (a wedged browser?). "
                           "They can no longer act: the gate is closed and their grants are revoked.")
        return out

    def _revoke(self, rec: FreezeRecord, lanes_in: list[str], *, by: str, reason: str,
                errors: list[str]) -> dict[str, Any]:
        empty: dict[str, Any] = {"grants": [], "cascades_to": [], "standing": []}
        policy = self._svc("policy")
        sweep = getattr(policy, "revoke_reaching", None)
        if not callable(sweep):
            return empty
        try:
            if rec.scope in ("node", "mesh"):
                out = sweep(everything=True, by=by, reason=reason)
            elif rec.scope == "workspace":
                out = sweep(workspaces=[rec.target], by=by, reason=reason)
            else:
                out = sweep(lanes=[rec.target], by=by, reason=reason)
        except Exception as exc:
            log.exception("freeze: the revocation sweep failed")
            errors.append(f"grants could not be revoked ({type(exc).__name__}); the freeze holds anyway")
            return empty
        errors.extend(out.pop("errors", []) or [])
        return {**empty, **out}

    async def _cancel_runs(self, rec: FreezeRecord, lanes_in: list[str], by: str, errors: list[str]) -> list[str]:
        runner = self._svc("runner")
        if runner is None or not callable(getattr(runner, "list", None)):
            return []
        try:
            if rec.scope == "lane":
                runs = runner.list(status=list(ACTIVE_RUNS), lane=rec.target, limit=500)
            elif rec.scope == "workspace":
                runs = runner.list(status=list(ACTIVE_RUNS), workspace=rec.target, limit=500)
                runs += [r for r in runner.list(status=list(ACTIVE_RUNS), limit=500)
                         if r.lane and r.lane.split("/", 1)[0] == rec.target and r not in runs]
            else:
                runs = runner.list(status=list(ACTIVE_RUNS), limit=1000)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"runs could not be listed ({type(exc).__name__})")
            return []
        out = []
        for run in runs:
            try:
                await runner.cancel(run.id, by=f"freeze ({by})")
                out.append(run.id)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"run {run.id} could not be cancelled ({type(exc).__name__})")
        return out

    def _touches(self, rec: FreezeRecord, lanes: list[str | None], workspace: str | None) -> bool:
        """Does something on these lanes (or, with none named, in this workspace) touch the scope?"""
        if rec.scope in ("node", "mesh"):
            return True
        named = [x for x in lanes if x]
        if any(self._in_scope(rec, x) or _pattern_hits(rec, x) for x in named):
            return True
        ws = rec.target if rec.scope == "workspace" else rec.target.split("/", 1)[0]
        if not named and workspace == ws:
            return True  # it does not say which lane: assume it may be this one
        return rec.scope == "workspace" and workspace == ws

    def _pause_schedules(self, rec: FreezeRecord, errors: list[str]) -> list[str]:
        scheduler = getattr(self._svc("runner"), "scheduler", None)
        if scheduler is None:
            return []
        try:
            schedules = scheduler.list()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"schedules could not be listed ({type(exc).__name__})")
            return []
        out = []
        for s in schedules:
            if not getattr(s, "enabled", False):
                continue
            ws = (s.lane or s.surface or "").split("/", 1)[0] or (s.flow_id or "").split(".", 1)[0] or None
            if not self._touches(rec, [s.lane, s.surface], ws):
                continue
            try:
                scheduler.set_enabled(s.id, False)
                out.append(s.id)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"schedule {s.id} could not be paused ({type(exc).__name__})")
        return out

    def _pause_triggers(self, rec: FreezeRecord, why: str, errors: list[str]) -> list[str]:
        queue = self._svc("event_queue")
        dispatcher = getattr(queue, "dispatcher", None)
        if queue is None or dispatcher is None:
            return []
        try:
            routes = queue.list_routes()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"triggers could not be listed ({type(exc).__name__})")
            return []
        out = []
        now = _now()
        for r in routes:
            if r.state.paused:
                continue
            t = r.target
            lanes = [getattr(t, "lane", None), getattr(t, "on", None), r.match.lane, *r.grant.lanes]
            if not self._touches(rec, lanes, r.match.workspace):
                continue
            try:
                dispatcher.pause(r, f"frozen: {why}"[:200], now)
                out.append(r.id)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"trigger {r.id} could not be paused ({type(exc).__name__})")
        return out

    async def _cancel_brain_tasks(self, rec: FreezeRecord, errors: list[str]) -> list[str]:
        brains = self._svc("brains")
        store = getattr(brains, "store", None)
        if brains is None or store is None or not callable(getattr(store, "list_tasks", None)):
            return []
        try:
            ws = None if rec.scope in ("node", "mesh") else rec.target.split("/", 1)[0]
            tasks = self._open_brain_tasks(store, ws)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"brain tasks could not be listed ({type(exc).__name__})")
            return []
        out = []
        for t in tasks:
            status = str(getattr(t.status, "value", t.status))
            if status not in OPEN_BRAIN_TASKS:
                continue
            if rec.scope == "lane" and t.lanes and rec.target not in t.lanes:
                continue  # a task bound to other lanes of the workspace; its grant is checked anyway
            try:
                await brains.cancel(t.id, None)
                out.append(t.id)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"brain task {t.id} could not be cancelled ({type(exc).__name__})")
        return out

    @staticmethod
    def _open_brain_tasks(store: Any, ws: str | None) -> list[Any]:
        """Every open brain task (of a workspace), however many finished ones there are. The store lists
        newest first with a limit, so a page that comes back full is read again, larger."""
        from agentos.brains.models import TaskStatus

        statuses = [TaskStatus(s) for s in sorted(OPEN_BRAIN_TASKS) if s in TaskStatus._value2member_map_]
        limit = 500
        while True:
            tasks = store.list_tasks(statuses=statuses, workspace=ws, limit=limit)
            if len(tasks) < limit:
                return tasks
            limit *= 4

    def _warm(self, lanes_in: list[str]) -> list[str]:
        lanes = self._svc("lanes")
        out = []
        for lane_id in lanes_in:
            with contextlib.suppress(Exception):
                state = str(getattr(lanes.get(lane_id).state, "value", ""))
                if state in ("warm", "active", "warming"):
                    out.append(lane_id)
        return out

    # ================================================================ unfreeze
    async def unfreeze(self, scope: str, target: str | None = None, *, by: str, via: str = "cli",
                       source: str | None = None) -> dict[str, Any]:
        """Lift one freeze. The caller has already proved it is the owner (see the module docstring);
        `source="mesh"` lifts only the controller's own mesh freeze. Restores no grant and resumes
        nothing that was paused."""
        scope, target = self._normalize(scope, target)
        key = self._key(scope, target)
        rec = self._records.get(key)
        if scope == "node" and self.problem:
            # the freeze directory itself freezes the node: deleting a marker cannot lift that, and the
            # next poll would freeze (and sweep) the node again
            return {"scope": scope, "target": target, "unfrozen": False, "was_frozen": True,
                    "freeze_id": rec.id if rec is not None else None,
                    "still_frozen_by": {"scope": "node", "target": target, "reason": self.problem},
                    "next": (f"Nothing was unfrozen: {self.problem}. Fix or remove that file in "
                             f"{self.store.dir}, then unfreeze again.")}
        if rec is None:
            return {"scope": scope, "target": target, "unfrozen": False, "was_frozen": False,
                    "still_frozen_by": self._cover(scope, target)}
        if source == "mesh" and rec.source != "mesh":
            raise InvalidRequest("the controller can only lift its own mesh freeze")
        try:
            self.store.remove(key)
        except OSError as exc:
            raise InvalidRequest(f"the freeze could not be lifted: its file cannot be deleted "
                                 f"({exc.strerror or type(exc).__name__}). It stays frozen.") from None
        self._records.pop(key, None)
        self._memory_only.discard(key)
        self.store.journal({"event": "unfreeze", "freeze_id": rec.id, "scope": scope, "target": target, "by": by,
                            "via": via})
        self._trace("unfreeze", rec, by=by, summary=f"unfroze {scope} {target} (frozen by {rec.by}: {rec.reason})")
        return {"scope": scope, "target": target, "unfrozen": True, "was_frozen": True, "freeze_id": rec.id,
                "still_frozen_by": self._cover(scope, target),
                "next": ("Nothing was restored: grants revoked by the freeze stay revoked, and paused schedules, "
                         "triggers and cancelled runs stay that way. Grant agents again and resume what you want "
                         "back.")}

    def _cover(self, scope: str, target: str) -> dict[str, Any] | None:
        """A wider freeze that still covers this scope after an unfreeze."""
        probe = target if scope == "lane" else (f"{target}/x" if scope == "workspace" else None)
        rec = self.frozen_for(probe) if scope in ("lane", "workspace") else (
            self._records.get("mesh") or self._records.get("node"))
        if rec is None:
            return None
        return {"scope": rec.scope, "target": rec.target, "reason": rec.reason}

    # ================================================================ status
    def status(self, *, journal: int = 20) -> dict[str, Any]:
        lanes = self._svc("lanes")
        frozen_lanes: list[dict[str, Any]] = []
        with contextlib.suppress(Exception):
            for info in lanes.list() if lanes is not None else []:
                rec = self.frozen_for(info.spec.id)
                if rec is not None:
                    frozen_lanes.append({"lane": info.spec.id, "state": str(getattr(info.state, "value", "")),
                                         "by": f"{rec.scope} {rec.target}"})
        return {"node": self.node_name, "frozen": [r.public() for r in self.records()], "lanes": frozen_lanes,
                "node_frozen": self.frozen_for(None) is not None, "problem": self.problem,
                "journal": self.store.read_journal(journal)}

    # ================================================================ push links
    def _key_bytes(self) -> bytes:
        if self._link_key is None:
            self._link_key = self.store.link_key()
        return self._link_key

    def _sign(self, scope: str, target: str, exp: int, nonce: str) -> str:
        msg = f"freeze|{scope}|{target}|{exp}|{nonce}".encode()
        digest = hmac.new(self._key_bytes(), msg, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    def link_params(self, scope: str, target: str, *, ttl_s: int = LINK_TTL_S) -> dict[str, str]:
        exp = int(self.clock() + ttl_s)
        nonce = secrets.token_urlsafe(9)
        return {"scope": scope, "t": target, "e": str(exp), "n": nonce, "s": self._sign(scope, target, exp, nonce)}

    def push_link(self, event: dict[str, Any], base: str) -> str | None:
        """The one-tap Freeze link for a push: the event's lane, else this node."""
        lane = event.get("lane") if isinstance(event, dict) else None
        scope, target = ("lane", lane) if isinstance(lane, str) and _lane_ok(lane) else ("node", self.node_name)
        p = self.link_params(scope, target)
        query = "&".join(f"{k}={quote(v, safe='')}" for k, v in p.items())
        return f"{base.rstrip('/')}/api/freeze/link?{query}"

    async def redeem_link(self, params: dict[str, str]) -> dict[str, Any]:
        """A push's Freeze button: signed, single use, short lived. It only ever freezes."""
        scope, target = str(params.get("scope") or ""), str(params.get("t") or "")
        nonce, sig = str(params.get("n") or ""), str(params.get("s") or "")
        try:
            exp = int(str(params.get("e") or ""))
        except ValueError:
            raise InvalidRequest("this link is not valid") from None
        if scope not in ("lane", "node") or not nonce or len(nonce) > 40:
            raise InvalidRequest("this link is not valid")
        if not sig.isascii() or not hmac.compare_digest(self._sign(scope, target, exp, nonce), sig):
            raise InvalidRequest("this link is not valid")
        if exp < self.clock():
            raise InvalidRequest("this link has expired; freeze from the panel or with agentos freeze on the node")
        if self._used_links is None:
            self._used_links = self.store.used_links()
        if nonce in self._used_links:
            raise InvalidRequest("this link was already used")
        self._used_links.add(nonce)
        self.store.use_link(nonce)
        return await self.freeze(scope, target, reason="frozen from a push", by="owner (push)", via="push_link")

    # ================================================================ trace and push
    def _trace(self, kind: str, rec: FreezeRecord, *, by: str, summary: str, detail: dict | None = None) -> None:
        traces = self._svc("traces")
        write = getattr(traces, "write", None)
        if not callable(write):
            return
        try:
            from agentos.policy.traces import TraceEvent  # the trace's own model (as other packages write it)

            lane = rec.target if rec.scope == "lane" else None
            ws = rec.target if rec.scope == "workspace" else (rec.target.split("/", 1)[0] if lane else None)
            write(TraceEvent(kind=kind, tool=f"{kind}.{rec.scope}", principal=by[:80], principal_kind="freeze",
                             lane=lane, workspace=ws, status="ok", summary=summary[:500], args=detail))
        except Exception:
            log.warning("freeze: could not write the %s trace", kind, exc_info=True)

    def _notify(self, rec: FreezeRecord, *, by: str) -> None:
        notifier = self._svc("notifier")
        send = getattr(notifier, "send", None)
        if not callable(send):
            return
        where = "every node" if rec.scope == "mesh" else ("this node" if rec.scope == "node" else rec.target)
        with contextlib.suppress(Exception):
            send({"type": "freeze", "title": f"Frozen: {where}", "node_text": True, "urgent": True,
                  "lane": rec.target if rec.scope == "lane" else None, "key": f"freeze:{rec.key}",
                  "body": (f"{by} froze {where}: {rec.reason}. Agents are stopped there. Open the panel to look "
                           "or unfreeze.")})


def _lane_ok(lane: str) -> bool:
    try:
        parse_lane_id(lane)
        return True
    except ValueError:
        return False


def _pattern_hits(rec: FreezeRecord, pattern: str) -> bool:
    """A lane pattern (biz-*/x, ws/*) that could name a lane in the scope."""
    if "*" not in pattern and "?" not in pattern:
        return False
    from fnmatch import fnmatchcase

    if rec.scope == "workspace":
        return fnmatchcase(rec.target, pattern.split("/", 1)[0])
    return fnmatchcase(rec.target, pattern)


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime.now(UTC)


def build(runtime: Any) -> FreezeService:
    svc = FreezeService(runtime)
    if runtime is not None:
        runtime.register(SERVICE_NAME, svc)
    return svc


__all__ = ["SERVICE_NAME", "FreezeService", "build", "parse_key"]
