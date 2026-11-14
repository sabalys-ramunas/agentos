"""BrainService: registry, router, seats and the detached task runner, as runtime service
"brains". Built by `build(runtime)`.

Lifecycle of a task:

  brain_run -> route (privacy floor, capabilities, cost rank, budget gate)
     -> queued -> seat acquired (atomic) -> starting -> shim spawned (detached) -> running
     -> attempt ends:  succeeded         -> succeeded_unverified
                       rate limit / 429  -> brain cools down, task routes onward (or waits)
                       expired auth      -> brain needs_auth + owner notified, task routes onward
                       crash/hang/5xx    -> next brain in the chain (max attempts)
                       limits / cancel   -> failed / cancelled
                       node restarted    -> interrupted (resumable with transcript context)

The daemon is the only process that schedules queued tasks; the CLI can inspect, cancel and
resume. Tasks survive the MCP caller disconnecting and the daemon restarting, because the
shim owns the CLI process, not the daemon.

Who the brain runs as (agentos.brains.launch): agentosd as root drops to the brain user itself;
agentosd as its own unprivileged user asks the agentos-runas helper (agentos.runas), which also
gives each shim a scope of its own, and reaches the brain's directories by running small commands
as the brain (BrainFiles). Either way no brain starts until the AUD-08 confinement check
(agentos.brains.confine) has shown, from the brain's own identity, that it cannot reach the
node's secrets.

A decision brain is the other kind: `decide` answers typed questions with one in-process HTTP call
(agentos.brains.decisions), with no task, no shim and no transcript, under the same privacy floor.
The router keeps the two apart: a task never reaches a decision brain, a decision never reaches
anything else.

ToS boundary: subscriptions are bring-your-own, for the owner's own use on the owner's node.
One subscription is never pooled across tenants or people; a hosted edition must give each
tenant its own (see docs/ARCHITECTURE.md section 4).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import secrets as pysecrets
import signal
import socket
import subprocess
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from agentos.brains import bundle as bundle_mod
from agentos.brains import cliprobe, confine, decisions, launch
from agentos.brains.attempts import ACTIVE, AttemptsMixin, read_json
from agentos.brains.credentials import DEFAULT_CHILD_REF, CredentialStore, brain_ref, child_ref
from agentos.brains.deciding import DecidingMixin
from agentos.brains.identity import IdentityMixin
from agentos.brains.models import (
    KIND_RANK,
    WORKSPACE_RE,
    BrainKind,
    BrainSpec,
    BrainState,
    BrainView,
    Budget,
    ContextBundle,
    GrantView,
    Locality,
    Outcome,
    Privacy,
    Requirements,
    RouteDecision,
    Runner,
    TaskRecord,
    TaskStatus,
    brain_facts,
    brain_identity,
)
from agentos.brains.ports import Ports
from agentos.brains.router import ANY_BRAIN, route
from agentos.brains.scheduling import SchedulingMixin
from agentos.brains.seats import SeatManager
from agentos.brains.settings import BrainsSettings, load_settings
from agentos.brains.store import Store
from agentos.brains.transcript import last_seq, read_events, redact_patterns
from agentos.core.errors import InvalidRequest, NotFound, PolicyDenied
from agentos.core.models import Effect, Principal, parse_lane_id

log = logging.getLogger("agentos.brains")

PROMPT_MAX = 200_000


def new_task_id() -> str:
    return "bt_" + pysecrets.token_hex(6)


def _dt(ts: float | None) -> datetime | None:
    return datetime.fromtimestamp(ts, UTC) if ts else None



class BrainService(IdentityMixin, SchedulingMixin, AttemptsMixin, DecidingMixin):
    def __init__(self, runtime: Any, *, home: Path, settings: BrainsSettings | None = None,
                 clock=time.time, online_check=None, local_check=None, allow_file_git: bool = False,
                 decide_transport: httpx.AsyncBaseTransport | None = None):
        self.runtime = runtime
        self.home = home
        self.dir = home / "brains"
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self.tasks_dir = self.dir / "tasks"
        self.tasks_dir.mkdir(exist_ok=True)
        self.settings = settings or load_settings(home)
        self.clock = clock
        self.store = Store(self.dir / "brains.db")
        self.seats = SeatManager(self.store, self.settings, clock)
        self.creds = CredentialStore(home, runtime.get("vault") if runtime is not None else None)
        self.ports = Ports(runtime, self.dir)
        self.allow_file_git = allow_file_git
        self._online_check = online_check
        self._local_check = local_check
        self._online_cache: tuple[float, bool] = (0.0, True)
        self._local_cache: dict[str, tuple[float, bool, str | None]] = {}
        self._cred_cache: dict[str, tuple[float, bool]] = {}
        self._procs: dict[tuple[str, int], subprocess.Popen] = {}
        self._sched_lock = asyncio.Lock()
        self._monitor: asyncio.Task | None = None
        self._bg: set[asyncio.Task] = set()
        self._preparing: set[str] = set()
        self._confinement: confine.Confinement | None = None
        self._identity: tuple[float, launch.BrainIdentity] | None = None
        self.wire = decisions.Wire(self.settings.decide_timeout_seconds, decide_transport)
        self._holds = decisions.Holds()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        moved = await self.creds.migrate_dev_entries()
        if moved:
            log.info("moved %d brain credential(s) from the dev fallback file into the vault", moved)
        await self.tick()
        self._monitor = asyncio.create_task(self._monitor_loop(), name="brains-monitor")

    async def shutdown(self) -> None:
        """Stop monitoring. Running tasks keep going (they are detached) and are picked up
        again by the next start."""
        if self._monitor:
            self._monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._monitor
            self._monitor = None
        for t in list(self._bg):
            t.cancel()
        await self.wire.aclose()

    async def _monitor_loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("brains monitor tick failed")
            await asyncio.sleep(self.settings.poll_seconds)

    async def tick(self) -> None:
        for key, proc in list(self._procs.items()):
            if proc.poll() is not None:
                self._procs.pop(key, None)
        for rec in self.store.list_tasks(statuses=ACTIVE, limit=500):
            await self._check_running(rec)
        queued = self.store.list_tasks(statuses=[TaskStatus.QUEUED], limit=200)
        if queued:
            views = await self.brain_views()
            for rec in sorted(queued, key=lambda r: r.created_at):
                if rec.prepared:
                    await self.schedule(rec.id, views=views)
                elif rec.id not in self._preparing:
                    # preparation was cut short (daemon restart): do it again
                    self._spawn_bg(self._prepare_then_schedule(rec.id))

    # ------------------------------------------------------------------ paths
    def task_dir(self, task_id: str) -> Path:
        return self.tasks_dir / task_id

    def attempt_dir(self, task_id: str, n: int) -> Path:
        return self.task_dir(task_id) / "attempts" / f"{n:02d}"

    def attempt_dirs(self, task_id: str) -> list[Path]:
        base = self.task_dir(task_id) / "attempts"
        return sorted(p for p in base.iterdir() if p.is_dir()) if base.exists() else []

    # ------------------------------------------------------------------ registry
    def get_spec(self, name: str) -> BrainSpec:
        spec = self.store.get_brain(name)
        if spec is None:
            raise NotFound(f"no brain named {name}")
        return spec

    def _task_brain(self, name: str) -> BrainSpec:
        """A brain a task may name. Saying no here beats a task that queues until its time runs out."""
        spec = self.get_spec(name)
        if spec.caps.decisions:
            raise InvalidRequest(f"{name} answers typed questions and cannot run a task")
        return spec

    async def add_brain(self, spec: BrainSpec, credential: str | None = None) -> dict[str, Any]:
        if spec.credential and credential:
            await self.set_credential(spec.name, credential, spec=spec)
        await self.store.run(self.store.put_brain, spec)
        self.ports.trace("brain.registry", action="add", brain=spec.name, brain_kind=spec.kind.value)
        return await self.brain_info(spec)

    async def set_credential(self, name: str, value: str, *, spec: BrainSpec | None = None) -> None:
        spec = spec or self.get_spec(name)
        value = value.strip()
        if spec.credential == "codex_auth":
            try:
                parsed = json.loads(value)
            except ValueError as exc:
                raise InvalidRequest("a Codex login must be the contents of ~/.codex/auth.json") from exc
            if not isinstance(parsed, dict) or not (parsed.get("tokens") or parsed.get("OPENAI_API_KEY")):
                raise InvalidRequest("that auth.json has no tokens; run `codex login` first")
        elif spec.credential == "oauth_token" and not value.startswith("sk-ant-oat"):
            raise InvalidRequest("that does not look like a token from `claude setup-token` (sk-ant-oat...)")
        await self.creds.put(brain_ref(name), value, kind=spec.credential or "secret",
                             label=f"brain {name} ({spec.provider} {spec.kind.value})",
                             scope={"node": True, "brain": name, "provider": spec.provider})
        self._cred_cache.pop(name, None)
        # a new sign-in may be another subscription: what a co-sign named of this brain no longer holds
        await self.store.run(self.store.set_kv, f"signin:{name}", pysecrets.token_hex(8))
        await self.store.run(self.seats.clear_auth, name)
        self.ports.trace("brain.credential", action="set", brain=name, backend=self.creds.backend)

    async def remove_brain(self, name: str) -> None:
        self.get_spec(name)
        if self.store.active_count(name):
            raise InvalidRequest(f"{name} has running tasks; cancel them first")
        await self.creds.delete(brain_ref(name))
        await self.store.run(self.store.remove_brain, name)
        self.ports.trace("brain.registry", action="remove", brain=name)

    def set_order(self, names: list[str]) -> list[str]:
        specs = {s.name: s for s in self.store.list_brains()}
        unknown = [n for n in names if n not in specs]
        if unknown:
            raise NotFound(f"no brain named {', '.join(unknown)}")
        rest = [n for n in sorted(specs, key=lambda n: (specs[n].order, n)) if n not in names]
        final = [*names, *rest]
        for i, n in enumerate(final):
            spec = specs[n]
            spec.order = (i + 1) * 10
            self.store.put_brain(spec)
        self.ports.trace("brain.registry", action="order", order=final)
        return final

    def use(self, name: str, workspace: str) -> dict[str, Any]:
        self._task_brain(name)
        self._check_workspace(workspace)
        self.store.set_workspace(workspace, default_brain=name)
        self.ports.trace("brain.registry", action="use", brain=name, workspace=workspace)
        return {"workspace": workspace, "default_brain": name}

    def use_node_default(self, name: str) -> dict[str, Any]:
        self._task_brain(name)
        self.store.set_kv("default_brain", name)
        self.ports.trace("brain.registry", action="use", brain=name, workspace=None)
        return {"workspace": None, "default_brain": name}

    def set_privacy(self, workspace: str, privacy: Privacy) -> dict[str, Any]:
        self._check_workspace(workspace)
        self.store.set_workspace(workspace, privacy=privacy)
        self.ports.trace("brain.registry", action="privacy", workspace=workspace, privacy=privacy.value)
        return {"workspace": workspace, "privacy": privacy.value}

    def privacy_for(self, workspace: str) -> Privacy:
        p = self.store.workspace(workspace).get("privacy")
        return Privacy(p) if p else self.settings.default_privacy

    @staticmethod
    def _check_workspace(ws: str) -> None:
        if not WORKSPACE_RE.match(ws or ""):
            raise InvalidRequest("workspace must be lowercase letters, digits and dashes")

    # ------------------------------------------------------------------ health
    def probe_runner(self, runner: Runner) -> cliprobe.CliInfo:
        explicit = {Runner.CLAUDE: self.settings.claude_bin, Runner.CODEX: self.settings.codex_bin,
                    Runner.CHAT: self.settings.chat_python}.get(runner)
        return cliprobe.probe(runner.value, explicit=explicit, bin_dirs=self.settings.bin_dirs)

    def probe(self, spec: BrainSpec) -> cliprobe.CliInfo:
        return self.probe_runner(spec.runner)

    async def online(self) -> bool:
        if self._online_check is not None:
            out = self._online_check()
            return bool(await out) if asyncio.iscoroutine(out) else bool(out)
        ts, ok = self._online_cache
        if self.clock() - ts < self.settings.online_check_ttl:
            return ok

        def check() -> bool:
            try:
                with socket.create_connection((self.settings.online_check_host, 443), timeout=3):
                    return True
            except OSError:
                return False

        ok = await asyncio.to_thread(check)
        self._online_cache = (self.clock(), ok)
        return ok

    async def _local_ok(self, spec: BrainSpec) -> tuple[bool, str | None]:
        if self._local_check is not None:
            out = self._local_check(spec)
            return out if isinstance(out, tuple) else (bool(out), None)
        cached = self._local_cache.get(spec.name)
        if cached and self.clock() - cached[0] < self.settings.online_check_ttl:
            return cached[1], cached[2]
        base = (spec.base_url or "http://127.0.0.1:11434/v1").rstrip("/")
        url = base + "/models"

        def check() -> tuple[bool, str | None]:
            try:
                with urllib.request.urlopen(url, timeout=3) as resp:
                    return resp.status < 500, None
            except Exception as exc:  # noqa: BLE001
                return False, f"model server at {base} is not answering ({type(exc).__name__})"

        ok, why = await asyncio.to_thread(check)
        self._local_cache[spec.name] = (self.clock(), ok, why)
        return ok, why

    async def _has_credential(self, spec: BrainSpec) -> bool:
        cached = self._cred_cache.get(spec.name)
        if cached and self.clock() - cached[0] < 10:
            return cached[1]
        ok = await self.creds.has(brain_ref(spec.name))
        self._cred_cache[spec.name] = (self.clock(), ok)
        return ok

    async def effective_state(self, spec: BrainSpec) -> tuple[BrainState, str | None]:
        if not spec.enabled:
            return BrainState.DOWN, "disabled"
        state, reason, _ = self.seats.override(spec.name)
        if state is not None:
            return state, reason
        info = await asyncio.to_thread(self.probe, spec)
        if not info.ok:
            return BrainState.DOWN, info.reason()
        if spec.credential and not await self._has_credential(spec):
            return BrainState.NEEDS_AUTH, f"no credential stored; run: agentos brains auth {spec.name}"
        if spec.kind == BrainKind.LOCAL:
            ok, why = await self._local_ok(spec)
            if not ok:
                return BrainState.DOWN, why
        return BrainState.READY, None

    async def _view(self, spec: BrainSpec) -> tuple[BrainSpec, BrainView]:
        state, reason = await self.effective_state(spec)
        return spec, BrainView(
            name=spec.name, kind=spec.kind, locality=spec.locality, state=state, state_reason=reason,
            caps=spec.caps, order=spec.order, seats_total=spec.seats,
            seats_in_use=self.seats.in_use(spec.name), window_saturated=self.seats.window_saturated(spec),
            enabled=spec.enabled, identity=brain_identity(spec, self._signin(spec.name)))

    def _signin(self, name: str) -> str | None:
        return self.store.get_kv(f"signin:{name}")

    def _cosign_problem(self, grant: GrantView, taint: Any, spec: BrainSpec) -> str | None:
        """Why a co-sign the grant holds does not cover `spec` as it is right now, or None. Asked again
        right where its credential is read, so a sign-in between the route and the run is caught."""
        identity = brain_identity(spec, self._signin(spec.name))
        if (spec.locality == Locality.CLOUD and "private" in {str(t).lower() for t in taint}
                and identity not in (grant.private_cloud_brains or [])):
            return f"{spec.name} changed since the co-sign to send private data (another sign-in or setting)"
        if spec.kind == BrainKind.METERED and grant.metered_brains is not None and identity not in grant.metered_brains:
            return f"{spec.name} changed since the co-sign to spend (another sign-in or setting)"
        return None

    async def brain_views(self) -> list[tuple[BrainSpec, BrainView]]:
        return [await self._view(spec) for spec in self.store.list_brains()]

    async def brain_info(self, spec: BrainSpec) -> dict[str, Any]:
        state, reason = await self.effective_state(spec)
        info = self.probe(spec)
        return {
            "name": spec.name, "kind": spec.kind.value, "provider": spec.provider, "runner": spec.runner.value,
            "model": spec.model, "state": state.value, "state_reason": reason,
            "seats": {"in_use": self.seats.in_use(spec.name), "total": spec.seats},
            "window_used_pct": self.seats.window_pct(spec), "order": spec.order, "enabled": spec.enabled,
            "caps": spec.caps.model_dump(), "locality": spec.locality.value,
            "credential": {"kind": spec.credential, "backend": self.creds.backend} if spec.credential else None,
            "cli": {"version": info.version, "verified": cliprobe.PINNED[spec.runner.value]["verified"],
                    "ok": info.ok, "warnings": info.warnings},
        }

    async def list_brains(self) -> list[dict[str, Any]]:
        return [await self.brain_info(s) for s in self.store.list_brains()]

    async def chip(self) -> dict[str, Any]:
        """The panel's Brain chip: the brain the router would try first, and whether work can
        keep going on the node with the laptop closed (offline_ready) or the internet down
        (local_ready). A decision brain is neither: it cannot run a task."""
        views = [sv for sv in await self.brain_views() if not sv[0].caps.decisions]
        if not views:
            return {"name": None, "state": None, "offline_ready": False, "local_ready": False}
        ranked = sorted(views, key=lambda sv: (sv[1].state != BrainState.READY, KIND_RANK[sv[1].kind],
                                               sv[1].order, sv[1].name))
        top = ranked[0][1]
        ready = [v for _, v in views if v.state == BrainState.READY]
        return {"name": top.name, "kind": top.kind.value, "state": top.state.value,
                "offline_ready": bool(ready),
                "local_ready": any(v.kind == BrainKind.LOCAL for v in ready),
                "spend_today": self.spend_today()}

    # ------------------------------------------------------------------ submit
    def effective_budget(self, b: Budget | None) -> Budget:
        b = b or Budget()
        s = self.settings
        return Budget(
            max_usd=b.max_usd,
            max_tokens=min(b.max_tokens or s.default_max_tokens, s.cap_max_tokens),
            max_turns=min(b.max_turns or s.default_max_turns, s.cap_max_turns),
            max_minutes=min(b.max_minutes or s.default_max_minutes, s.cap_max_minutes),
            max_queue_minutes=b.max_queue_minutes or s.default_max_queue_minutes)

    async def _grant_view(self, principal: Principal) -> GrantView:
        gv = await self.ports.grant_view(principal)
        if gv is not None:
            return gv
        return GrantView(allow_metered=self.settings.metered_without_policy)

    async def route_for(self, rec: TaskRecord, *, views=None) -> RouteDecision:
        views = views if views is not None else await self.brain_views()
        exclude = set()
        for a in self.store.attempts(rec.id):
            if a["n"] > rec.resume_from_attempt and a.get("outcome") in (
                    Outcome.CRASHED.value, Outcome.STALLED.value, Outcome.SERVER_ERROR.value,
                    Outcome.SPAWN_FAILED.value):
                exclude.add(a["brain"])
        preferred = self.store.workspace(rec.workspace).get("default_brain") or self.store.get_kv("default_brain")
        return route(rec.requires, privacy=rec.privacy, taint=rec.taint, grant=rec.grant,
                     brains=[v for _, v in views], budget=rec.budget, online=await self.online(),
                     pinned=rec.pinned_brain, preferred=preferred, exclude=exclude)

    async def submit(self, principal: Principal, *, prompt: str, workspace: str, lanes: list[str] | None = None,
                     brain: str | None = None, budget: Budget | None = None,
                     context_bundle: ContextBundle | None = None, requires: Requirements | None = None,
                     ctx: Any = None, child_token: str | None = None, wait_prepare: bool = False,
                     turn_grant: dict[str, Any] | None = None) -> dict[str, Any]:
        """Start a brain task. The caller has already authorized Effect.BRAIN.

        `turn_grant` ({"token", "grant_id"}) is a grant the caller already minted for this one
        task (agentos.events: narrowed by its trigger and seeded with the event's taint). It is
        used instead of minting a child grant here, and revoked when the task ends, like one."""
        lanes = self._check_submit(prompt, workspace, lanes, brain, requires, context_bundle)
        bundle = context_bundle
        if self.confinement_required():
            await asyncio.to_thread(self.check_confinement)
        task_id = new_task_id()
        privacy = self.privacy_for(workspace)
        taint = set(await self.ports.taint(principal)) | {t.lower() for t in (bundle.taint if bundle else [])}
        grant = await self._grant_view(principal)
        rec = TaskRecord(
            id=task_id, workspace=workspace, principal_id=principal.id, owner=principal.owner,
            thread=principal.thread, grant_path=getattr(getattr(ctx, "grant", None), "path", None), pinned_brain=brain, privacy=privacy, taint=sorted(taint), lanes=lanes,
            budget=self.effective_budget(budget), requires=requires or Requirements(), grant=grant,
            prompt_preview=redact_patterns(prompt.strip())[:160], queued_since=datetime.now(UTC))

        decision = await self.route_for(rec)
        self._trace_decision(rec, decision, ctx=ctx, stage="submit")
        if decision.action == "needs_approval":
            bundle = await self._pin_commit(bundle)
            decision = await self._escalate(rec, decision, ctx, content=_cosign_content(prompt, bundle))
        token, grant_id = await self._child_grant(principal, rec, turn_grant, child_token)
        self._write_task_files(task_id, prompt, bundle)
        if token:
            await self.creds.put(child_ref(task_id), token, kind="grant_token", label=f"child grant for {task_id}",
                                 scope={"task": task_id})
        rec.child_grant_id = grant_id
        rec.spend_grant_id = grant_id or getattr(principal, "grant_id", None)
        needs_slow_prep = bool(bundle and (bundle.git or bundle.archive_b64))
        rec.status_reason = "preparing the workspace" if needs_slow_prep else decision.reason
        await self.store.run(self.store.insert_task, rec)
        self.ports.trace("brain.task", ctx=ctx, action="created", task=task_id, workspace=workspace,
                         lanes=lanes, privacy=privacy.value, taint=sorted(taint),
                         prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest())

        if needs_slow_prep and not wait_prepare:
            self._spawn_bg(self._prepare_then_schedule(task_id))
        else:
            await self._prepare_then_schedule(task_id)
        out = self.status(task_id)
        out["route"] = {"action": decision.action, "reason": decision.reason,
                        "chain": [c.name for c in decision.candidates]}
        return out

    def _check_submit(self, prompt: str, workspace: str, lanes: list[str] | None, brain: str | None,
                      requires: Requirements | None, bundle: ContextBundle | None) -> list[str]:
        """What `submit` refuses before anything is stored. Returns the lanes, each once."""
        self._check_workspace(workspace)
        if not prompt or not prompt.strip():
            raise InvalidRequest("prompt is empty")
        if len(prompt) > PROMPT_MAX:
            raise InvalidRequest(f"prompt is over {PROMPT_MAX} characters; put the detail in the context bundle")
        lanes = list(dict.fromkeys(lanes or []))
        for lane in lanes:
            try:
                ws, _ = parse_lane_id(lane)
            except ValueError as exc:
                raise InvalidRequest(str(exc)) from exc
            if ws != workspace:
                raise InvalidRequest(f"lane {lane} is not in workspace {workspace}")
        if brain:
            self._task_brain(brain)
        if requires is not None and requires.decisions:
            raise InvalidRequest("a task cannot ask for a decision brain: it would have nothing to run on")
        if bundle is not None:
            bundle_mod.validate(bundle, allow_file_git=self.allow_file_git)
        return lanes

    async def _pin_commit(self, bundle: ContextBundle | None) -> ContextBundle | None:
        """A co-sign binds the commit the branch is at now, never a name that can move after the yes."""
        if bundle is None or bundle.git is None or bundle.git.commit is not None:
            return bundle
        commit = await asyncio.to_thread(bundle_mod.resolve_commit, bundle.git, self.dir, allow_file=self.allow_file_git)
        return bundle.model_copy(update={"git": bundle.git.model_copy(update={"commit": commit})})

    async def _child_grant(self, principal: Principal, rec: TaskRecord, turn_grant: dict[str, Any] | None,
                           child_token: str | None) -> tuple[str | None, str | None]:
        """A CHILD grant for the brain's callback into agentos (token, grant id), never the owner token."""
        lanes = rec.lanes
        token, grant_id = None, None
        if turn_grant is not None:
            token, grant_id = turn_grant.get("token"), turn_grant.get("grant_id")
            if not token or not lanes:
                raise InvalidRequest("a turn grant needs its token and the lanes it covers")
            if await self.ports.is_owner_token(token):
                raise PolicyDenied("refusing to give a brain the owner's own token; it needs a child grant")
        elif lanes:
            minted = await self.ports.mint_child(
                principal, task_id=rec.id, workspace=rec.workspace, lanes=lanes,
                ttl_seconds=int((rec.budget.max_minutes or 60) * 60 * 2 + 3600),
                budget={"tokens": rec.budget.max_tokens, "usd": rec.budget.max_usd})
            if minted:
                token, grant_id = minted
            else:
                token = child_token or await self.creds.get(DEFAULT_CHILD_REF, purpose="brain child grant")
            if not token:
                raise InvalidRequest(
                    "lanes need a child grant token for the brain, and this node has no policy service to "
                    "mint one. Provision one with: agentos brains child-token --stdin")
            if await self.ports.is_owner_token(token):
                raise PolicyDenied("refusing to give a brain the owner's own token; it needs a child grant")
        elif child_token and await self.ports.is_owner_token(child_token):
            raise PolicyDenied("refusing to give a brain the owner's own token; it needs a child grant")
        return token, grant_id

    def _write_task_files(self, task_id: str, prompt: str, bundle: ContextBundle | None) -> None:
        """The task's own directory, with the prompt and the context bundle, only for agentosd."""
        tdir = self.task_dir(task_id)
        tdir.mkdir(parents=True, exist_ok=False)
        os.chmod(tdir, 0o700)
        fd = os.open(tdir / "prompt.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(prompt)
        if bundle is not None:
            fd = os.open(tdir / "bundle.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(bundle.model_dump_json())

    async def _escalate(self, rec: TaskRecord, decision: RouteDecision, ctx: Any, *,
                        content: dict[str, Any] | None = None) -> RouteDecision:
        """A human co-sign could unlock metered spend or private data on a cloud brain. `content`: what
        it binds (_cosign_content), the whole prompt and the bundle that go, not the task's preview."""
        views = await self.brain_views()

        async def trial(grant: GrantView) -> RouteDecision:
            return await self.route_for(rec.model_copy(update={"grant": grant}), views=views)

        await self._cosign(decision, ctx, workspace=rec.workspace, budget=rec.budget, grant=rec.grant,
                           what="a brain task", content=content or {"prompt": rec.prompt_preview},
                           trial=trial, specs={sp.name: sp for sp, _ in views})
        decision = await self.route_for(rec)
        self._trace_decision(rec, decision, ctx=ctx, stage="after co-sign")
        return decision

    async def _cosign(self, decision: RouteDecision, ctx: Any, *, workspace: str, budget: Budget, grant: GrantView,
                      what: str, content: dict[str, Any], trial: Any = None,
                      specs: dict[str, BrainSpec] | None = None) -> None:
        """Ask a person for what the router said only a person can allow, and write the answer into
        `grant`. Raises when nobody can be asked or the answer is not yes yet. The card names one
        brain, the one the router would pick with the yes (`trial`), with its provider, model and
        sign-in; the yes covers only that brain as it is now, so a retry elsewhere asks again."""
        appr = decision.approval or {}
        names = appr.get("brains") or []
        if appr.get("kind") == "metered" and budget.max_usd is None:
            raise InvalidRequest("only metered brains can take this right now. Pass budget.max_usd to allow "
                                 "spending real money, or wait for a subscription or local brain.")
        if ctx is None or not callable(getattr(ctx, "authorize", None)):
            raise PolicyDenied(decision.reason)
        chosen = await self._cosign_brain(appr, names, grant, trial)
        if chosen is not None:
            names = [chosen]
        spec = (specs or {}).get(chosen) if chosen else None
        if spec is None and chosen is not None:
            try:
                spec = self.get_spec(chosen)
            except NotFound:
                spec = None
        if spec is None:
            raise PolicyDenied("the brain a co-sign would name is not registered on this node")
        signin = self._signin(spec.name)
        identity = brain_identity(spec, signin)
        content = {**content, "brain": {**brain_facts(spec, signin), "identity": identity}}
        who = f"{spec.name} ({spec.provider} {spec.runner.value}{f', model {spec.model}' if spec.model else ''})"
        if appr.get("kind") == "metered":
            await ctx.authorize(
                Effect.PAY,
                summary=(f"Spend up to ${budget.max_usd:.2f} on metered model use "
                         f"on {who} for {what} in workspace {workspace}"),
                content={"workspace": workspace, "brains": names, "max_usd": budget.max_usd, **content})
            grant.allow_metered = True
            grant.metered_brains = [identity]
            if grant.remaining_usd is not None and budget.max_usd > grant.remaining_usd:
                grant.remaining_usd = budget.max_usd
        else:
            await ctx.authorize(
                Effect.IRREVERSIBLE,
                summary=(f"Send private data from workspace {workspace} to cloud brain "
                         f"{who} (it cannot be unsent)"),
                content={"workspace": workspace, "brains": names, **content})
            grant.allow_private_cloud = True
            grant.private_cloud_brains = [identity]

    async def _cosign_brain(self, appr: dict[str, Any], names: list[str], grant: GrantView, trial: Any) -> str | None:
        """The brain a yes would run on: the first the router ranks among those a co-sign could
        allow, with the yes given. Without a trial (or when it ranks none), the first named."""
        if trial is not None and names:
            update = ({"allow_metered": True, "metered_brains": [ANY_BRAIN], "remaining_usd": None}
                      if appr.get("kind") == "metered"
                      else {"allow_private_cloud": True, "private_cloud_brains": [ANY_BRAIN]})
            ranked = await trial(grant.model_copy(update=update))
            for cand in ranked.candidates:
                if cand.name in names:
                    return cand.name
        return names[0] if names else None

    def _trace_decision(self, rec: TaskRecord, d: RouteDecision, *, ctx: Any = None, stage: str = "") -> None:
        self.ports.trace("brain.route", ctx=ctx, task=rec.id, stage=stage, action=d.action, reason=d.reason,
                         chain=[c.name for c in d.candidates],
                         rejected={r.name: r.reason for r in d.rejected})

    def _spawn_bg(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    # ------------------------------------------------------------------ control
    def _get(self, task_id: str) -> TaskRecord:
        rec = self.store.get_task(task_id) if task_id else None
        if rec is None:
            raise NotFound(f"no brain task {task_id}")
        return rec

    def check_access(self, rec: TaskRecord, principal: Principal | None) -> None:
        """Tasks are visible to principals of the same owner (one owner per node in self-host)."""
        if principal is not None and principal.owner != rec.owner:
            raise NotFound(f"no brain task {rec.id}")

    async def cancel(self, task_id: str, principal: Principal | None = None, *, wait: bool = False) -> dict[str, Any]:
        rec = self._get(task_id)
        self.check_access(rec, principal)
        if rec.status in (TaskStatus.QUEUED, TaskStatus.INTERRUPTED, TaskStatus.WAITING_APPROVAL):
            prev = rec.status
            rec.status = TaskStatus.CANCELLED
            rec.status_reason = "cancelled before it ran" if prev == TaskStatus.QUEUED else "cancelled"
            rec.ended_at = datetime.now(UTC)
            if await self.store.run(self.store.save_task, rec, expect=[TaskStatus.QUEUED, TaskStatus.INTERRUPTED,
                                                 TaskStatus.WAITING_APPROVAL]):
                await self._cleanup_task(rec)
            self.ports.trace("brain.task", action="cancelled", task=rec.id)
            return self.status(task_id)
        if rec.status in ACTIVE:
            attempt = self.store.attempt(rec.id, rec.attempt)
            if attempt:
                adir = Path(attempt["dir"])
                with contextlib.suppress(OSError):
                    (adir / "cancel").touch()
                status = read_json(adir / "status.json")
                # signal only a shim that is up (its handlers installed); a starting one
                # sees the cancel file before it launches the brain
                if status and self._shim_alive(rec, attempt, status):
                    pid = attempt.get("shim_pid") or status.get("pid")
                    with contextlib.suppress(OSError, TypeError):
                        os.kill(int(pid), signal.SIGTERM)
            self.ports.trace("brain.task", action="cancel_requested", task=rec.id)
            if wait:
                deadline = time.time() + self.settings.cancel_grace_seconds + 20
                while time.time() < deadline:
                    await self._check_running(self._get(task_id))
                    if self._get(task_id).status not in ACTIVE:
                        break
                    await asyncio.sleep(0.3)
            out = self.status(task_id)
            if out["status"] in ("starting", "running"):
                out["status_reason"] = "cancelling (graceful stop, then kill)"
            return out
        return self.status(task_id)

    async def resume(self, task_id: str, principal: Principal | None = None, *, brain: str | None = None) -> dict:
        rec = self._get(task_id)
        self.check_access(rec, principal)
        if rec.status not in (TaskStatus.INTERRUPTED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            raise InvalidRequest(f"task {task_id} is {rec.status.value}; only interrupted, failed or cancelled "
                                 "tasks can be resumed")
        if brain:
            self._task_brain(brain)
            rec.pinned_brain = brain
        prev = rec.status
        rec.status = TaskStatus.QUEUED
        rec.status_reason = "resuming with transcript context"
        rec.resume_from_attempt = rec.attempt
        rec.queued_since = datetime.now(UTC)
        rec.ended_at = None
        if rec.lanes and not await self.creds.has(child_ref(rec.id)):
            raise InvalidRequest("the task's child grant was already revoked; start a new task instead")
        if not await self.store.run(self.store.save_task, rec, expect=[prev]):
            raise InvalidRequest(f"task {task_id} changed state; try again")
        self.ports.trace("brain.task", action="resume", task=rec.id, brain=brain)
        await self._prepare_then_schedule(task_id)
        return self.status(task_id)

    # ------------------------------------------------------------------ views
    def status(self, task_id: str, principal: Principal | None = None) -> dict[str, Any]:
        rec = self._get(task_id)
        self.check_access(rec, principal)
        out = rec.public()
        attempts = []
        for a in self.store.attempts(rec.id):
            attempts.append({"n": a["n"], "brain": a["brain"], "runner": a["runner"],
                             "started_at": _dt(a.get("started_at")), "ended_at": _dt(a.get("ended_at")),
                             "outcome": a.get("outcome"), "reason": a.get("reason"), "tokens": a.get("tokens"),
                             "turns": a.get("turns"), "cost_usd": a.get("cost_usd")})
            if a.get("outcome") is None:
                st = read_json(Path(a["dir"]) / "status.json") or {}
                acct = st.get("accounting") or {}
                attempts[-1].update({"live": True, "tokens": acct.get("tokens"), "turns": acct.get("turns"),
                                     "heartbeat_at": _dt(st.get("heartbeat_at"))})
        out["attempts"] = attempts
        out["transcript_last_seq"] = last_seq(self.attempt_dirs(rec.id))
        out["work_dir"] = str(self.task_dir(rec.id) / "work")
        out["resumable"] = rec.status in (TaskStatus.INTERRUPTED, TaskStatus.FAILED, TaskStatus.CANCELLED)
        return json.loads(json.dumps(out, default=str))

    def tail(self, task_id: str, principal: Principal | None = None, *, since: int | None = None,
             limit: int = 100) -> dict[str, Any]:
        rec = self._get(task_id)
        self.check_access(rec, principal)
        limit = max(1, min(int(limit), 500))
        data = read_events(self.attempt_dirs(task_id), since=since, limit=limit)
        for ev in data["events"]:
            ev.pop("raw", None)
        return {"id": task_id, "status": rec.status.value, "status_reason": rec.status_reason, **data}

    def list_tasks(self, principal: Principal | None = None, *, status: str | None = None,
                   workspace: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        statuses = [TaskStatus(status)] if status else None
        out = []
        for rec in self.store.list_tasks(statuses=statuses, workspace=workspace, limit=limit):
            if principal is not None and principal.owner != rec.owner:
                continue
            out.append(json.loads(json.dumps({
                "id": rec.id, "workspace": rec.workspace, "status": rec.status.value,
                "status_reason": rec.status_reason, "brain": rec.brain, "attempt": rec.attempt,
                "tokens": rec.tokens, "turns": rec.turns, "cost_usd": rec.cost_usd,
                "prompt_preview": rec.prompt_preview, "created_at": rec.created_at,
                "ended_at": rec.ended_at, "lanes": list(rec.lanes), "principal_id": rec.principal_id,
                "grant_path": rec.grant_path},
                default=str)))
        return out

    def spend_today(self) -> dict[str, Any]:
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        tokens, usd = self.store.spend_since(start)
        return {"tokens": tokens, "usd": round(usd, 4)}


SERVICE_NAME = "brains"  # the daemon registers the built service under this name


def build(runtime: Any) -> BrainService:
    """Composition-root hook: `runtime.register("brains", build(runtime))`, then
    `await runtime.brains.start()` once the event loop runs."""
    cfg = runtime.get("config") if runtime is not None else None
    home = Path(getattr(cfg, "home", None) or os.environ.get("AGENTOS_HOME", "/var/lib/agentos"))
    return BrainService(runtime, home=home)


def _cosign_content(prompt: str, bundle: ContextBundle | None) -> dict[str, Any]:
    """What a co-sign for a brain task binds and shows: the whole prompt (redacted, as the card shows
    it, and its digest, so another prompt is another card), and the context bundle that goes with it:
    its summary, the files it names, the git ref, and the archive's size and digest."""
    out: dict[str, Any] = {"prompt": redact_patterns(prompt.strip()),
                           "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()}
    if bundle is not None:
        files = [f if isinstance(f, str) else getattr(f, "path", None) or getattr(f, "name", None) or "(inline file)"
                 for f in bundle.files]
        archive = None
        if bundle.archive_b64:
            archive = {"chars": len(bundle.archive_b64),
                       "sha256": hashlib.sha256(bundle.archive_b64.encode()).hexdigest()}
        out["bundle"] = {"summary": bundle.summary, "files": files,
                         "git": bundle.git.model_dump(mode="json") if bundle.git is not None else None,
                         "archive": archive,
                         "sha256": hashlib.sha256(bundle.model_dump_json().encode()).hexdigest()}
    return out
