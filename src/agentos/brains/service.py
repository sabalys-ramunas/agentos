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
import pwd
import random
import secrets as pysecrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

import agentos
from agentos import runas
from agentos.brains import adapters, cliprobe, confine, decisions, launch
from agentos.brains import bundle as bundle_mod
from agentos.brains.credentials import DEFAULT_CHILD_REF, CredentialStore, brain_ref, child_ref
from agentos.brains.models import (
    KIND_RANK,
    WORKSPACE_RE,
    BrainKind,
    BrainSpec,
    BrainState,
    BrainView,
    Budget,
    Candidate,
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
from agentos.brains.seats import SeatManager
from agentos.brains.settings import BrainsSettings, load_settings
from agentos.brains.store import Store
from agentos.brains.transcript import last_seq, read_events, redact_patterns
from agentos.core import fs
from agentos.core.errors import InvalidRequest, NotFound, PolicyDenied
from agentos.core.models import Effect, Principal, parse_lane_id

log = logging.getLogger("agentos.brains")
MAX_AUTH_FILE = 1_000_000  # a codex auth.json is a few KB

ACTIVE = (TaskStatus.STARTING, TaskStatus.RUNNING)
PROMPT_MAX = 200_000
PICK_RUNNERS = (Runner.CLAUDE, Runner.CHAT)
PICK_TIMEOUT_S = 90.0
PICK_MAX_TURNS = 2
PICK_POLL_S = 0.2
SPAWN_GRACE = 30.0     # a starting attempt may take this long to write its first status
HEARTBEAT_STALE = 30.0
IDENTITY_TTL = 5.0     # brain_identity() asks the helper again after this long


def new_task_id() -> str:
    return "bt_" + pysecrets.token_hex(6)


def _dt(ts: float | None) -> datetime | None:
    return datetime.fromtimestamp(ts, UTC) if ts else None


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


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


class BrainService:
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

    def _helper_socket(self) -> Path | None:
        """The agentos-runas socket, when agentosd is not root and the helper is up."""
        if os.geteuid() == 0 or not self.settings.runas_socket:
            return None
        path = Path(self.settings.runas_socket)
        try:
            return path if path.is_socket() else None
        except OSError:
            return None

    def brain_identity(self) -> launch.BrainIdentity:
        """Who brains run as: one answer for the confinement probe, the attempt, and BrainFiles.

            root agentosd          drops to run_as itself (an account that must exist)
            non-root agentosd      asks agentos-runas, which must serve exactly run_as (whoami), a
                                   user other than agentosd's own
            neither                agentosd's own user: only where the confinement check is not
                                   required (dev on macOS, or confinement = "off") and no helper is
                                   up; anywhere else it is refused

        Raises BrainUnconfined when run_as names no account, the helper is down or serves another
        user, or the brain user would be agentosd's own. Resolved at most every few seconds."""
        now = time.monotonic()
        if self._identity is not None and now - self._identity[0] < IDENTITY_TTL:
            return self._identity[1]
        ident = self._resolve_identity()
        self._identity = (now, ident)
        return ident

    def _resolve_identity(self) -> launch.BrainIdentity:
        name = self.settings.run_as
        me = os.geteuid()
        sock = self._helper_socket()  # None when root
        if me != 0 and sock is None and not self.confinement_required():
            return launch.BrainIdentity(None, me, os.getegid(), "")  # dev: brains as agentosd's own user
        if not name:
            if me != 0:
                raise confine.BrainUnconfined("brains have no user of their own (brains settings run_as is empty) "
                                              "and agentosd is not root: they would run as agentosd's own user")
            return launch.BrainIdentity(None, me, os.getegid(), "")  # root itself: the probe refuses it
        try:
            pw = pwd.getpwnam(name)
        except KeyError:
            raise confine.BrainUnconfined(f"the brain user {name} (brains settings run_as) does not exist; brains "
                                          "would run as agentosd's own user") from None
        if pw.pw_uid == me or pw.pw_uid == 0:
            raise confine.BrainUnconfined(f"the brain user {name} is uid {pw.pw_uid}, which is agentosd's own or "
                                          "root; brains need a user of their own")
        if me == 0:
            return launch.BrainIdentity(name, pw.pw_uid, pw.pw_gid, pw.pw_dir)
        if sock is None:
            raise confine.BrainUnconfined(f"agentosd is not root and the agentos-runas helper is not up at "
                                          f"{self.settings.runas_socket or '(no runas_socket)'}, so brains cannot "
                                          f"start as {name}")
        try:
            info = runas.whoami(sock)
        except runas.RunasError as exc:
            raise confine.BrainUnconfined(f"the agentos-runas helper did not say who brains run as: {exc.message}"
                                          ) from None
        b = info.get("brain") or {}
        if b.get("name") != name or b.get("uid") != pw.pw_uid or b.get("gid") != pw.pw_gid:
            raise confine.BrainUnconfined(
                f"the agentos-runas helper starts brains as {b.get('name')} (uid {b.get('uid')}), not {name} "
                f"(uid {pw.pw_uid}); make its --brain-user and brains settings run_as name the same user")
        return launch.BrainIdentity(name, pw.pw_uid, pw.pw_gid, pw.pw_dir, helper=sock,
                                    groups=tuple(int(g) for g in b.get("groups") or ()),
                                    scopes=bool(info.get("scopes")))

    def _run_as(self) -> pwd.struct_passwd | None:
        """The brain user's account when brains run as one (from brain_identity), else None."""
        ident = self.brain_identity()
        return pwd.getpwnam(ident.name) if ident.name else None

    def _launcher(self, identity: str = "brain") -> launch.Launcher:
        """How programs start as the brain (or, `caller`, the detached shim as agentosd itself)."""
        ident = self.brain_identity()
        if ident.helper is not None:
            caller = identity == "caller"
            return launch.HelperLauncher(ident.helper, identity, detach=caller,
                                         expect_uid=os.geteuid() if caller else ident.uid)
        if identity == "brain" and ident.name:
            return launch.DirectLauncher({"uid": ident.uid, "gid": ident.gid})
        return launch.DirectLauncher(None)

    def _via_helper(self) -> bool:
        return self.brain_identity().helper is not None

    def _brain_files(self) -> launch.BrainFiles:
        return launch.BrainFiles(self._launcher())

    def _brain_dirs(self, task_id: str) -> tuple[Path, Path]:
        """(work, home) for the brain process. With an unprivileged brain user they live in
        that user's home (so AGENTOS_HOME stays closed to it) and are linked from the task dir.
        A non-root agentosd cannot chown, so the brain user makes them itself (BrainFiles)."""
        tdir = self.task_dir(task_id)
        user = self._run_as()
        if user is None:
            work, home = tdir / "work", tdir / "home"
            for d in (work, home, home / "tmp"):
                d.mkdir(parents=True, exist_ok=True)
            os.chmod(home, 0o700)
            return work, home
        base = Path(user.pw_dir) / "tasks" / task_id
        work, home = base / "work", base / "home"
        if self._via_helper():
            self._brain_files().mkdirs(Path(user.pw_dir) / "tasks", base, work, home, home / "tmp")
        else:
            # The brain owns everything below its home and may have swapped any of these for a link
            # (between attempts, or from another task): resolve each without following one, then
            # chown and chmod the directory that was opened, never a path.
            own = (user.pw_uid, user.pw_gid)
            for d in (base.parent, base, work, home, home / "tmp"):
                fd = fs.open_dir(user.pw_dir, d.relative_to(user.pw_dir), make=True, mode=0o700, owner=own)
                try:
                    os.fchown(fd, *own)
                    os.fchmod(fd, 0o700)
                finally:
                    os.close(fd)
        for name, target in (("work", work), ("home", home)):
            link = tdir / name
            if not os.path.lexists(link):  # never follow it: agentosd may not stat the brain's side
                link.symlink_to(target)
        return work, home

    def _beneath(self, path: Path) -> tuple[Path, PurePosixPath]:
        """(anchor, rel) for a path in a task's work or home dir, for the `fs.*_beneath` calls: the
        anchor is a directory the brain cannot replace (the brain user's home itself, or
        agentosd's own tasks dir), and every name below it is resolved without following a
        link. A path through the task dir's own link (tasks/<id>/home) maps to where it points."""
        user = self._run_as()
        anchor = Path(user.pw_dir) if user is not None else self.tasks_dir
        try:
            return anchor, PurePosixPath(path.relative_to(anchor))
        except ValueError:
            rel = PurePosixPath(path.relative_to(self.tasks_dir))  # <id>/work/...: the daemon's link
            return anchor, (PurePosixPath("tasks") / rel if user is not None else rel)

    def _write_brain_file(self, path: Path, content: str, mode: int) -> None:
        if self._via_helper():
            self._brain_files().write(path, content, mode)
            return
        user = self._run_as()
        own = (user.pw_uid, user.pw_gid) if user is not None and os.geteuid() == 0 else None
        anchor, rel = self._beneath(path)
        # a link at the name is replaced; a linked directory on the way refuses the attempt
        fs.write_beneath(anchor, rel, content, mode=mode, owner=own, dir_mode=0o700)

    # ------------------------------------------------------------------ confinement (AUD-08)
    def confinement_required(self) -> bool:
        mode = self.settings.confinement
        if mode == "off":
            return False
        return mode == "require" or sys.platform.startswith("linux")

    def confinement(self) -> confine.Confinement:
        if self._confinement is None:
            cfg = self.runtime.get("config") if self.runtime is not None else None
            self._confinement = confine.Confinement(
                self.home, launcher=self._launcher, identity=self.brain_identity, python=confine.probe_python(),
                config=cfg, cache_dir=self.dir)
        return self._confinement

    def check_confinement(self, *, force: bool = False) -> confine.Result:
        """Run (or reuse) the AUD-08 check. Raises BrainUnconfined when it fails."""
        result = self.confinement().check(force=force)
        if not result.cached:
            self.ports.trace("brain.confinement", ok=result.ok, reason=result.reason, launcher=result.launcher,
                             uid=result.uid, checked=result.checked)
        if not result.ok:
            raise confine.BrainUnconfined(result.reason)
        return result

    def _scope_limits(self) -> dict[str, int]:
        return {"memory_mb": int(self.settings.memory_mb), "tasks_max": int(self.settings.tasks_max),
                "cpu_quota_pct": int(self.settings.cpu_quota_pct)}

    def _use_scope(self) -> bool:
        mode = self.settings.isolation
        if mode == "none":
            return False
        available = (shutil.which("systemd-run") is not None and os.geteuid() == 0
                     and Path("/run/systemd/system").exists())
        if mode == "scope" and not available:
            raise InvalidRequest("isolation=scope needs systemd and root on the node")
        return available

    def mcp_url(self) -> str:
        if self.settings.mcp_url:
            return self.settings.mcp_url
        cfg = self.runtime.get("config") if self.runtime is not None else None
        return f"http://127.0.0.1:{getattr(cfg, 'mcp_port', 7400)}/mcp"

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
        bundle = context_bundle
        if bundle is not None:
            bundle_mod.validate(bundle, allow_file_git=self.allow_file_git)
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
            if bundle is not None and bundle.git is not None and bundle.git.commit is None:
                # a co-sign binds the commit the branch is at now, never a name that can move after the yes
                commit = await asyncio.to_thread(bundle_mod.resolve_commit, bundle.git, self.dir,
                                                 allow_file=self.allow_file_git)
                bundle = bundle.model_copy(update={"git": bundle.git.model_copy(update={"commit": commit})})
            decision = await self._escalate(rec, decision, ctx, content=_cosign_content(prompt, bundle))

        # a CHILD grant for the brain's callback into agentos, never the owner token
        token, grant_id = None, None
        if turn_grant is not None:
            token, grant_id = turn_grant.get("token"), turn_grant.get("grant_id")
            if not token or not lanes:
                raise InvalidRequest("a turn grant needs its token and the lanes it covers")
            if await self.ports.is_owner_token(token):
                raise PolicyDenied("refusing to give a brain the owner's own token; it needs a child grant")
        elif lanes:
            minted = await self.ports.mint_child(
                principal, task_id=task_id, workspace=workspace, lanes=lanes,
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

    async def _prepare_then_schedule(self, task_id: str) -> None:
        if task_id in self._preparing:
            return
        self._preparing.add(task_id)
        try:
            await self._prepare(task_id)
        finally:
            self._preparing.discard(task_id)
        await self.schedule(task_id)

    async def _prepare(self, task_id: str) -> None:
        rec = self.store.get_task(task_id)
        if rec is None or rec.status != TaskStatus.QUEUED:
            return
        if not rec.prepared:
            raw = _read_json(self.task_dir(task_id) / "bundle.json")
            bundle = ContextBundle(**raw) if raw else None
            try:
                work, _ = await asyncio.to_thread(self._brain_dirs, task_id)
                git_env = None
                if bundle is not None and bundle.git is not None:
                    from agentos.git.upstream import config_env

                    pairs = await self._git_pairs(rec)
                    git_env = config_env(pairs) if pairs else None
                handoff = await asyncio.to_thread(self._materialize, task_id, bundle, work,
                                                  redact=rec.privacy == Privacy.REDACTED_CLOUD, git_env=git_env)
            except Exception as exc:  # noqa: BLE001
                rec.status = TaskStatus.FAILED
                rec.status_reason = f"could not prepare the workspace: {getattr(exc, 'message', None) or exc}"[:400]
                rec.ended_at = datetime.now(UTC)
                await self.store.run(self.store.save_task, rec, expect=[TaskStatus.QUEUED])
                await self._cleanup_task(rec)
                return
            rec.handoff = handoff
            rec.prepared = True
            rec.status_reason = "queued"
            await self.store.run(self.store.save_task, rec, expect=[TaskStatus.QUEUED])

    async def _git_pairs(self, rec: TaskRecord) -> list[tuple[str, str]]:
        """Git config that sends the task's git to the node's git gateway (agentos.git) as the task's
        grant, for the remotes declared for its workspace and lanes; [] when there are none. The
        brain gets it in its private ~/.gitconfig, and the bundle's clone uses it too."""
        git = self.runtime.get("git") if self.runtime is not None else None
        if git is None:
            return []
        try:
            return await git.task_pairs(rec.id, grant_id=rec.spend_grant_id, workspace=rec.workspace,
                                        lanes=list(rec.lanes),
                                        ttl_s=(rec.budget.max_minutes or 60) * 60 * 2 + 3600)
        except Exception:  # the task still runs; its git reaches no declared remote
            log.exception("could not set up git for brain task %s", rec.id)
            return []

    def _materialize(self, task_id: str, bundle: ContextBundle | None, work: Path, *, redact: bool,
                     git_env: dict[str, str] | None = None) -> str | None:
        """Write the bundle into the brain's working directory. With a brain user it is written
        into a staging directory of agentosd's own first (no brain can touch it while the clone
        and the archive land), then copied over: as the brain user through the helper, or by
        agentosd without following a link (`fs.copy_tree_beneath`, links copied as links)."""
        user = self._run_as()
        if user is None:  # the work dir is agentosd's own
            return bundle_mod.materialize(bundle, work, redact=redact, allow_file_git=self.allow_file_git,
                                          git_env=git_env)
        stage = self.task_dir(task_id) / "stage"
        shutil.rmtree(stage, ignore_errors=True)
        try:
            handoff = bundle_mod.materialize(bundle, stage, redact=redact, allow_file_git=self.allow_file_git,
                                             git_env=git_env)
            if stage.is_dir():
                if self._via_helper():
                    self._brain_files().unpack(stage, work)
                else:
                    anchor, rel = self._beneath(work)
                    fs.copy_tree_beneath(stage, anchor, rel, owner=(user.pw_uid, user.pw_gid))
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        return handoff

    # ------------------------------------------------------------------ scheduling
    async def schedule(self, task_id: str, *, views=None) -> None:
        async with self._sched_lock:
            for _ in range(self.settings.max_attempts + 1):
                if not await self._schedule_once(task_id, views):
                    return
                views = None  # an attempt ended at once (no credential, CLI drift): states changed

    async def _schedule_once(self, task_id: str, views=None) -> bool:
        """One routing pass. True when an attempt started and immediately went back to the queue."""
        rec = self.store.get_task(task_id)
        if rec is None or rec.status != TaskStatus.QUEUED or not rec.prepared:
            return False
        now = self.clock()
        queued_since = rec.queued_since.timestamp() if rec.queued_since else now
        if rec.attempt - rec.resume_from_attempt >= self.settings.max_attempts:
            await self._fail(rec, f"gave up after {self.settings.max_attempts} attempts")
            return False
        if rec.tokens >= (rec.budget.max_tokens or 1 << 62):
            await self._fail(rec, f"token budget of {rec.budget.max_tokens} used up")
            return False
        if rec.budget.max_usd is not None and rec.cost_usd >= rec.budget.max_usd > 0:
            await self._fail(rec, f"dollar budget of ${rec.budget.max_usd:.2f} used up")
            return False
        if self.confinement_required():
            try:
                await asyncio.to_thread(self.check_confinement)
            except confine.BrainUnconfined as exc:
                await self._fail(rec, f"{exc.code}: {exc.message}")
                return False
        views = views if views is not None else await self.brain_views()
        decision = await self.route_for(rec, views=views)
        if decision.action == "run":
            specs = {s.name: s for s, _ in views}
            for cand in decision.candidates:
                if cand.wait:
                    break
                spec = specs[cand.name]
                n = rec.attempt + 1
                adir = self.attempt_dir(rec.id, n)
                if await self.store.run(self.seats.try_acquire, spec, rec, n, adir):
                    self._trace_decision(rec, decision, stage=f"attempt {n}")
                    await self._start_attempt(rec, spec, n, redact=cand.redact)
                    after = self.store.get_task(task_id)
                    return bool(after and after.status == TaskStatus.QUEUED)
            decision.reason = "seats were taken a moment ago; waiting"
        # still queued
        max_q = (rec.budget.max_queue_minutes or self.settings.default_max_queue_minutes) * 60
        if now - queued_since > max_q:
            await self._fail(rec, f"waited {int(max_q // 60)} min in the queue: {decision.reason}")
            return False
        if rec.status_reason != decision.reason:
            rec.status_reason = decision.reason
            await self.store.run(self.store.save_task, rec, expect=[TaskStatus.QUEUED])
            self._trace_decision(rec, decision, stage="queued")
        if decision.action == "needs_approval" and "approval" not in rec.notified:
            rec.notified.append("approval")
            await self.store.run(self.store.save_task, rec, expect=[TaskStatus.QUEUED])
            await self.ports.notify(
                "A brain task is waiting for you",
                f"Task {rec.id} in {rec.workspace} is queued: {decision.reason}.",
                kind="brain.queued", data={"task": rec.id})
        return False

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
        adir = self.attempt_dir(rec.id, n)
        try:
            adir.mkdir(parents=True, exist_ok=True)
            os.chmod(adir.parent, 0o700)
            os.chmod(adir, 0o700)
            info = await asyncio.to_thread(self.probe, spec)
            if not info.ok or not info.path:
                raise _AttemptError(Outcome.SPAWN_FAILED, info.reason() or f"{spec.runner} CLI not available")
            credential = None
            if why := self._cosign_problem(rec.grant, rec.taint, spec):
                raise _AttemptError(Outcome.SPAWN_FAILED, why)
            signin = self._signin(spec.name)
            if spec.credential:
                credential = await self.creds.get(brain_ref(spec.name), purpose=f"brain task {rec.id}")
                if not credential:
                    raise _AttemptError(Outcome.AUTH_FAILED,
                                        f"no credential stored; run: agentos brains auth {spec.name}")
                if self._signin(spec.name) != signin:  # signed in again while it was read
                    raise _AttemptError(Outcome.SPAWN_FAILED, f"{spec.name} was signed in again just now")
            # the sign-in this attempt runs on: a refreshed login is stored back only while it still holds
            await self.store.run(self.store.set_kv, f"attempt_signin:{rec.id}", signin)
            token = await self.creds.get(child_ref(rec.id), purpose="brain child grant") if rec.lanes else None
            if token and await self.ports.is_owner_token(token):
                raise _AttemptError(Outcome.SPAWN_FAILED, "refusing to give a brain the owner's own token")
            work, home = await asyncio.to_thread(self._brain_dirs, rec.id)
            prev = self.store.attempt(rec.id, n - 1) if n > 1 else None
            resume_session = None
            if rec.session_id and rec.session_brain == spec.name:
                resume_session = rec.session_id
            prompt = self._attempt_prompt(rec, n, spec, resume_session, prev.get("reason") if prev else None)
            if redact:
                prompt = redact_patterns(prompt)
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
            pairs = await self._git_pairs(rec)
            if pairs:  # a file, not the environment: Claude Code may scrub its Bash tool's environment
                from agentos.git.service import gitconfig_text

                plan.files[str(home / ".gitconfig")] = (gitconfig_text(pairs), 0o600)
            for path, (content, mode) in plan.files.items():
                await asyncio.to_thread(self._write_brain_file, Path(path), content, mode)
            prompt_file = adir / "prompt.txt"
            fd = os.open(prompt_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(prompt)
            helper = ident.helper is not None
            if helper and self.settings.isolation == "scope" and not ident.scopes:
                raise _AttemptError(Outcome.SPAWN_FAILED, "isolation=scope, and the agentos-runas helper has no systemd")
            scoped = self.settings.isolation != "none" and ident.scopes if helper else self._use_scope()
            unit = f"agentos-brain-{rec.id}-{n:02d}" if scoped else None
            seq_start = max(last_seq(self.attempt_dirs(rec.id)), 0) + 1
            limits = {
                "max_turns": rec.budget.max_turns, "max_seconds": (rec.budget.max_minutes or 60) * 60,
                "max_tokens": max((rec.budget.max_tokens or 0) - rec.tokens, 1),
                "max_usd": self._remaining_usd(rec, spec),
                "max_output_bytes": self.settings.max_output_bytes, "stall_seconds": self.settings.stall_seconds,
                "grace_seconds": self.settings.cancel_grace_seconds, "max_line_bytes": self.settings.max_line_bytes,
                "price_in_per_mtok": spec.price_in_per_mtok if spec.kind == BrainKind.METERED else None,
                "price_out_per_mtok": spec.price_out_per_mtok if spec.kind == BrainKind.METERED else None,
            }
            launch = {
                "task_id": rec.id, "attempt": n, "brain": spec.name, "runner": spec.runner.value,
                "argv": plan.argv, "cwd": str(work), "env": plan.env, "stdin_file": str(prompt_file),
                "run_as": {"uid": user.pw_uid, "gid": user.pw_gid} if user and not helper else None,
                "launcher": {"kind": "helper", "socket": str(ident.helper), "brain_uid": ident.uid,
                             "scope": f"{unit}-cli" if unit else None,
                             "limits": self._scope_limits() if unit else None} if helper else None,
                "limits": limits, "reports_cost": plan.reports_cost,
                "unit": f"{unit}.scope" if unit and not helper else None,
                "rlimits": {"core": 0, "nofile": 8192, "fsize": 8 * 1024 ** 3},
                "transcript": {"seq_start": seq_start, "segment_bytes": self.settings.transcript_segment_bytes,
                               "max_segments": self.settings.transcript_segments},
            }
            fd = os.open(adir / "launch.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(launch, fh, indent=2)
            argv = [sys.executable, "-m", "agentos.brains.shim", str(adir)]
            if unit and not helper:
                argv = ["systemd-run", "--scope", "--quiet", "--collect", f"--unit={unit}",
                        "-p", f"MemoryMax={self.settings.memory_mb}M", "-p", f"TasksMax={self.settings.tasks_max}",
                        "-p", f"CPUQuota={self.settings.cpu_quota_pct}%", "--", *argv]
            shim_env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
                        "PYTHONPATH": str(Path(agentos.__file__).resolve().parent.parent),
                        "PYTHONDONTWRITEBYTECODE": "1"}
            redact_values = [v for v in (credential, token) if v]
            self._lane_memory("reserve", rec.id, n)  # a run counts against the lanes' RAM floor (AUD-43)
            secrets = {"env": plan.secret_env, "redact": redact_values}
            if helper:  # its own scope, so it outlives an agentosd restart (no limits: they are the CLI's)
                proc = await asyncio.to_thread(_spawn_shim_helper, self._launcher("caller"), argv, adir, shim_env,
                                               secrets, unit)
            else:
                proc = _spawn_shim(argv, adir, shim_env, secrets)
                self._procs[(rec.id, n)] = proc
            del plan, secrets
            await self.store.run(self.store.update_attempt, rec.id, n, shim_pid=proc.pid, unit=unit)
            rec.status = TaskStatus.RUNNING
            rec.status_reason = f"running on {spec.name} (attempt {n})"
            rec.started_at = rec.started_at or datetime.now(UTC)
            await self.store.run(self.store.save_task, rec, expect=[TaskStatus.STARTING])
            self.ports.trace("brain.attempt", action="start", task=rec.id, attempt=n, brain=spec.name,
                             brain_kind=spec.kind.value, runner=spec.runner.value, resume=bool(resume_session),
                             isolation="scope" if unit else "session", run_as=user.pw_name if user else None,
                             launcher="helper" if helper else "direct")
        except _AttemptError as exc:
            await self._finish_attempt(rec, spec, n, {"outcome": exc.outcome.value, "reason": exc.reason})
        except Exception as exc:
            log.exception("could not start attempt %s/%s", rec.id, n)
            await self._finish_attempt(rec, spec, n, {"outcome": Outcome.SPAWN_FAILED.value,
                                                      "reason": f"could not start: {type(exc).__name__}"})

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
        status = _read_json(adir / "status.json")
        if status and status.get("state") == "exited":
            await self._finish_attempt(rec, spec, attempt["n"], status)
            return
        if self._shim_alive(rec, attempt, status):
            acct = (status or {}).get("accounting") or {}
            await self.store.run(self.seats.note_utilization, spec.name, acct.get("window_utilization"))
            return
        status = _read_json(adir / "status.json")  # it may have finished between the two reads
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
        self._lane_memory("release", rec.id, n)
        try:
            outcome = Outcome(status.get("outcome") or Outcome.CRASHED.value)
        except ValueError:
            outcome = Outcome.CRASHED
        reason = str(status.get("reason") or outcome.value)[:600]
        acct = status.get("accounting") or {}
        tokens = int(acct.get("tokens") or 0)
        turns = int(acct.get("turns") or 0)
        cli_cost = acct.get("cost_usd")
        est = status.get("cost_estimate_usd")
        real_cost = float(est if est is not None else (cli_cost or 0)) if spec.kind == BrainKind.METERED else 0.0
        notional = float(cli_cost or 0) if spec.kind != BrainKind.METERED else 0.0
        if not await self.store.run(self.store.claim_attempt, rec.id, n, ended_at=self.clock(), outcome=outcome.value, reason=reason,
                                        tokens=tokens, cost_usd=real_cost, turns=turns):
            return  # another process already finalized this attempt
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

        requeue = False
        terminal: TaskStatus | None = None
        if outcome == Outcome.SUCCEEDED:
            await self.store.run(self.seats.mark_success, spec.name)
            terminal, rec.status_reason = TaskStatus.SUCCEEDED_UNVERIFIED, \
                f"finished on {spec.name}; the result is not verified"
        elif outcome == Outcome.CANCELLED:
            terminal, rec.status_reason = TaskStatus.CANCELLED, "cancelled"
        elif outcome == Outcome.INTERRUPTED:
            terminal = TaskStatus.INTERRUPTED
            rec.status_reason = reason
        elif outcome == Outcome.RATE_LIMITED:
            until = await self.store.run(self.seats.mark_rate_limited, spec, resets_at=status.get("resets_at"), detail=reason)
            self.ports.trace("brain.seat", action="cooling_down", brain=spec.name, until=until, reason=reason[:200])
            requeue = True
            rec.status_reason = f"{spec.name} hit a rate limit; routing onward"
        elif outcome == Outcome.AUTH_FAILED:
            await self.store.run(self.seats.mark_needs_auth, spec.name, reason)
            self._cred_cache.pop(spec.name, None)
            await self._notify_reauth(spec, reason)
            requeue = True
            rec.status_reason = f"{spec.name} needs you to sign in again; routing onward"
        elif outcome.retry_elsewhere:
            requeue = True
            rec.status_reason = f"{spec.name} attempt {n} {outcome.value}: {reason[:200]}; trying the next brain"
        else:
            terminal = TaskStatus.FAILED
            rec.status_reason = f"{outcome.value}: {reason}"

        if requeue:
            rec.status = TaskStatus.QUEUED
            rec.queued_since = datetime.now(UTC)
            await self.store.run(self.store.save_task, rec, expect=[*ACTIVE, TaskStatus.QUEUED])
            # inside schedule() the enclosing loop retries; elsewhere route onward right away
            if rec.prepared and not self._sched_lock.locked():
                await self.schedule(rec.id)
            return
        assert terminal is not None
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

    # ------------------------------------------------------------------ decisions
    async def decide(self, principal: Principal, *, workspace: str, state: Any,
                     questions: dict[str, dict[str, Any]], must_not_send: Iterable[str],
                     budget: Budget | None = None, job: str | None = None, taint: Iterable[str] = (),
                     brain: str | None = None, assets: dict[str, Any] | None = None,
                     ctx: Any = None) -> dict[str, Any]:
        """Answer typed questions about `state` on a decision brain, in process, and return
        {"brain", "model", "answers", "usage": {"input_tokens", "output_tokens", "estimated"},
        "cost_usd", "latency_ms", "retries"}. Nothing is queued: it answers now or raises.

        must_not_send: strings that must never leave the node (a password, a code). A body holding
        one is refused before any call. There is no default: pass () only when there are none.
        budget: `max_usd` is required (metered spend) and `max_tokens` optional. Without `job` they
        limit this call. With `job`, an id of your choosing, they limit everything spent under that
        id, here and in earlier calls, even across a restart: that is how one budget covers the
        hundreds of calls of one task. The grant's own remainder applies either way.
        taint: labels of what is in `state`; "private" needs the usual co-sign, on top of the
        grant's own taint. brain: use this one decision brain. assets: images, by placeholder.
        ctx: a tool context, the only way to reach a person for a co-sign. Without one, what needs
        a co-sign is refused, so a hot path wants a grant that already allows metered spend.

        Raises DecisionUnavailable (nothing can answer now; see its `state` and `retry_after`),
        DecisionBlocked, DecisionRejected, DecisionOverBudget, or PolicyDenied."""
        labels = await self.ports.taint(principal) | {t.lower() for t in taint}
        return await self._decide(principal, await self._grant_view(principal), labels, workspace=workspace,
                                  state=state, questions=questions, must_not_send=must_not_send, budget=budget,
                                  job=job, brain=brain, assets=assets, ctx=ctx)

    def _pick_brain(self, name: str | None) -> BrainSpec:
        """The brain a pick runs on: the one asked for, else the first that writes in the owner's
        order, a subscription before a metered key. Codex cannot promise to run without tools."""
        if name:
            spec = self._task_brain(name)
            if spec.runner not in PICK_RUNNERS:
                raise InvalidRequest(f"{name} cannot answer a pick: it would run with tools")
            return spec
        ready = [s for s in self.store.list_brains() if s.enabled and s.runner in PICK_RUNNERS]
        ready.sort(key=lambda s: (s.kind != BrainKind.SUBSCRIPTION, s.order))
        if not ready:
            raise decisions.DecisionUnavailable("no model that writes is signed in: run `agentos brains signin claude`")
        return ready[0]

    async def pick(self, principal: Principal, *, workspace: str, question: str, options: dict[str, str],
                   state: Any, must_not_send: Iterable[str], brain: str | None = None, ctx: Any = None,
                   timeout_s: float = PICK_TIMEOUT_S, budget: Budget | None = None) -> dict[str, Any]:
        """One typed answer from a model that writes, for a worker that is unsure: the same options
        the worker had, so a rescue cannot invent an action, a URL or a selector. Returns {"brain",
        "choice" (None when no option is right), "confidence", "tokens", "cost_usd", "latency_ms",
        "task"}.

        It runs as a brain task, so a Claude subscription works here (its token only runs its own
        CLI) under the same confinement, seats, privacy floor and traces. It runs with no tools at
        all, because `state` is a stranger's text. The caller has already authorized Effect.BRAIN.
        A task takes seconds, not milliseconds: this is for the rare step, never every step.

        budget: `max_usd` and `max_tokens` cap this one pick, so pass what is left, not the whole
        allowance. A metered brain is refused without `max_usd`, as any task is; a subscription
        spends no money, so `cost_usd` is 0 there and `tokens` is what it used. Only those two
        fields are read: a pick's turns and minutes are its own.

        must_not_send has no default, as in `decide`. Raises DecisionBlocked, DecisionOverBudget
        (`max_usd` is 0 or less on a metered brain), DecisionRejected, DecisionUnavailable (nothing
        answered in time; the worker should stop for a person). Once
        a task ran, the last two carry its `tokens` and `cost_usd`, because a bad answer is paid for
        too. After a timeout that is what was known then, and a late attempt can add to it."""
        spec = self._pick_brain(brain)
        prompt = decisions.pick_prompt(question, options, state)
        if decisions.leak({"prompt": prompt}, prompt.encode(), list(must_not_send)) is not None:
            raise decisions.DecisionBlocked("a pick held something that must not leave the node, so it was not sent")
        started = time.monotonic()
        limit = budget or Budget()
        if spec.kind == BrainKind.METERED and limit.max_usd is not None and limit.max_usd <= 0:
            raise decisions.DecisionOverBudget(f"budget: {spec.name} is metered and no money is left for a pick")
        out = await self.submit(principal, prompt=prompt, workspace=workspace, brain=spec.name, ctx=ctx,
                                requires=Requirements(tools=False, no_tools=True),
                                budget=Budget(max_usd=limit.max_usd, max_tokens=limit.max_tokens,
                                              max_turns=PICK_MAX_TURNS, max_minutes=timeout_s / 60,
                                              max_queue_minutes=timeout_s / 60))
        task = out["id"]
        try:
            rec, said = await self._pick_reply(task, spec, principal, started, timeout_s)
            choice, confidence = decisions.read_pick(said, options)
        except (decisions.DecisionRejected, decisions.DecisionUnavailable) as exc:
            spent = self._get(task)
            exc.tokens, exc.cost_usd = spent.tokens, spent.cost_usd
            raise
        return {"brain": spec.name, "choice": choice, "confidence": confidence, "tokens": rec.tokens,
                "cost_usd": rec.cost_usd, "latency_ms": round((time.monotonic() - started) * 1000), "task": task}

    async def _pick_reply(self, task: str, spec: BrainSpec, principal: Principal, started: float,
                          timeout_s: float) -> tuple[TaskRecord, str]:
        """The finished task and what it said, or why it will not say anything."""
        while not (rec := self._get(task)).status.terminal:
            if rec.status == TaskStatus.INTERRUPTED or time.monotonic() - started > timeout_s:
                await self.cancel(task, principal)
                raise decisions.DecisionUnavailable(f"{spec.name} did not answer in {timeout_s:.0f} s")
            if rec.status == TaskStatus.QUEUED:  # a brain that cannot start will not start: say so now
                state, why = await self.effective_state(spec)
                if state != BrainState.READY:
                    await self.cancel(task, principal)
                    raise decisions.DecisionUnavailable(f"{spec.name} did not answer: {why or state.value}",
                                                        state=state.value)
            await asyncio.sleep(PICK_POLL_S)
        if rec.status != TaskStatus.SUCCEEDED_UNVERIFIED:
            raise decisions.DecisionUnavailable(f"{spec.name} did not answer: {rec.status_reason or rec.status.value}")
        events = self.tail(task, principal, limit=20)["events"]
        return rec, next((e.get("text") or "" for e in reversed(events) if e.get("kind") == "result"), "")

    async def probe_decision(self, principal: Principal, name: str) -> dict[str, Any]:
        """One harmless question for `agentos brains test --live`. The owner at the node's own shell
        is the co-sign for this fixed, cent-sized call; no tool reaches it. The privacy floor holds."""
        if not self.get_spec(name).caps.decisions:
            raise InvalidRequest(f"{name} is not a decision brain")
        question = {"type": "noul", "instructions": "Does this sentence mention the sky?"}
        return await self._decide(principal, GrantView(allow_metered=True), set(), workspace="agentos-test",
                                  state="The sky is blue today.", questions={"mentions_sky": question},
                                  must_not_send=(), budget=Budget(max_usd=0.01), brain=name)

    async def _decide(self, principal: Principal, grant: GrantView, labels: set[str], *, workspace: str, state: Any,
                      questions: dict[str, dict[str, Any]], must_not_send: Iterable[str],
                      budget: Budget | None = None, job: str | None = None, brain: str | None = None,
                      assets: dict[str, Any] | None = None, ctx: Any = None) -> dict[str, Any]:
        self._check_workspace(workspace)
        decisions.check_job(job)
        ask = decisions.Ask.build(state, questions, assets, must_not_send)
        budget = budget or Budget()
        if brain and not self.get_spec(brain).caps.decisions:
            raise InvalidRequest(f"{brain} runs tasks and cannot answer typed questions")
        tokens, usd = self._decision_spent(job)
        if budget.max_usd is not None and usd >= budget.max_usd > 0:
            raise decisions.DecisionOverBudget(f"budget: ${usd:.6f} of ${budget.max_usd:.6f} is spent")
        if budget.max_tokens is not None and tokens >= budget.max_tokens:
            raise decisions.DecisionOverBudget(f"budget: {tokens} of {budget.max_tokens} tokens are spent")
        # only decision brains are looked at, so an unrelated CLI's cold probe never holds up a decision
        views = [await self._view(s) for s in self.store.list_brains() if s.caps.decisions]
        if not views:
            raise decisions.DecisionUnavailable("no decision brain is registered on this node")
        privacy = self.privacy_for(workspace)
        req = Requirements(tools=False, decisions=True, min_context=ask.context_tokens)

        async def routed(with_grant: GrantView | None = None) -> RouteDecision:
            g = with_grant or grant
            return route(req, privacy=privacy, taint=labels, grant=g, brains=[v for _, v in views],
                         budget=self._decision_left(job, budget, g), online=await self.online(), pinned=brain)

        decision = await routed()
        if decision.action == "needs_approval":
            digest = hashlib.sha256(decisions.encode({"state": state, "questions": questions})).hexdigest()
            await self._cosign(decision, ctx, workspace=workspace, budget=budget, grant=grant,
                               what="typed decisions", content={"body_sha256": digest}, trial=routed,
                               specs={sp.name: sp for sp, _ in views})
            # read again after the wait for the yes: a brain signed in again meanwhile is another brain
            views = [await self._view(s) for s in self.store.list_brains() if s.caps.decisions]
            decision = await routed()
        if decision.action != "run":
            self.ports.trace("brain.decide", ctx=ctx, outcome="not_routed", workspace=workspace, job=job,
                             action=decision.action, reason=decision.reason)
            raise self._decision_refused(decision)
        specs = {s.name: s for s, _ in views}
        last: _NextBrain | None = None
        for cand in decision.candidates:
            try:
                return await self._decide_on(specs[cand.name], cand, ask, principal, grant, budget, job, workspace, ctx,
                                             labels=labels)
            except _NextBrain as exc:
                last = exc
                self.ports.trace("brain.decide", ctx=ctx, outcome="next_brain", brain=cand.name, workspace=workspace,
                                 job=job, reason=exc.reason)
        assert last is not None  # the router said run, so there was a candidate
        raise decisions.DecisionUnavailable(last.reason, state=last.state, retry_after=last.retry_after)

    def _decision_refused(self, decision: RouteDecision) -> decisions.DecisionUnavailable:
        """What the router said, with the brain state behind it when one state explains it all."""
        rejected = decision.rejected
        if rejected and all(r.reason.startswith("cooling down") for r in rejected):
            until = min(self.seats.override(r.name)[2] or 0.0 for r in rejected)
            return decisions.DecisionUnavailable(decision.reason, state=BrainState.COOLING_DOWN.value,
                                                 retry_after=max(until - self.clock(), 0.0))
        if rejected and all(r.reason.startswith("needs the owner") for r in rejected):
            return decisions.DecisionUnavailable(decision.reason, state=BrainState.NEEDS_AUTH.value)
        return decisions.DecisionUnavailable(decision.reason)

    def _decision_spent(self, job: str | None) -> tuple[int, float]:
        """Tokens and dollars spent under `job`, counting calls still in flight."""
        if not job:
            return 0, 0.0
        tokens, usd = self.store.usage_for_task(job)
        held_tokens, held_usd = self._holds.total(job)
        return tokens + held_tokens, usd + held_usd

    def _decision_left(self, job: str | None, budget: Budget, grant: GrantView) -> Budget:
        """What is left to spend. Never above the grant's remainder: the router treats a budget over
        it as a request for more, and the grant shrinks with every call of a job set to its size."""
        tokens, usd = self._decision_spent(job)
        max_usd = None if budget.max_usd is None else budget.max_usd - usd
        if max_usd is not None and grant.remaining_usd is not None:
            max_usd = min(max_usd, grant.remaining_usd)
        max_tokens = None if budget.max_tokens is None else budget.max_tokens - tokens
        if max_tokens is not None and grant.remaining_tokens is not None:
            max_tokens = min(max_tokens, grant.remaining_tokens)
        return Budget(max_usd=max_usd, max_tokens=max_tokens)

    async def _decide_on(self, spec: BrainSpec, cand: Candidate, ask: decisions.Ask, principal: Principal,
                         grant: GrantView, budget: Budget, job: str | None, workspace: str,
                         ctx: Any, labels: set[str] = frozenset()) -> dict[str, Any]:
        """One brain's try: everything that must hold before the body leaves, the call, the money."""
        signin = self._signin(spec.name)
        if why := self._cosign_problem(grant, labels, spec):
            raise _NextBrain(why)
        key = await self.creds.get(brain_ref(spec.name), purpose=f"typed decision in {workspace}")
        if self._signin(spec.name) != signin:
            raise _NextBrain(f"{spec.name} was signed in again just now")
        if not key:
            await self._decision_auth_failed(spec, "no credential stored")
            raise _NextBrain(f"{spec.name} has no credential; run: agentos brains auth {spec.name}",
                             state=BrainState.NEEDS_AUTH.value)
        payload = ask.payload(spec.model or "", redact=cand.redact)
        body = decisions.encode(payload)
        digest = hashlib.sha256(body).hexdigest()
        hit = decisions.leak(payload, body, (*ask.must_not_send, key))
        if hit is not None:
            what = f"must_not_send[{hit}]" if hit < len(ask.must_not_send) else f"the API key of {spec.name}"
            self.ports.trace("brain.decide", ctx=ctx, outcome="blocked", brain=spec.name, workspace=workspace, job=job,
                             body_sha256=digest, what=what)
            raise decisions.DecisionBlocked(f"the body holds {what}, so nothing was sent")
        guess = ask.budget_tokens
        guess_usd = decisions.cost_usd(spec, guess, 0)
        left = self._decision_left(job, budget, grant)
        if left.max_usd is not None and guess_usd > left.max_usd:
            raise decisions.DecisionOverBudget(f"budget: this call could cost ${guess_usd:.6f} and "
                                               f"${max(left.max_usd, 0.0):.6f} is left")
        if left.max_tokens is not None and guess > left.max_tokens:
            raise decisions.DecisionOverBudget(f"budget: this call could use {guess} tokens and "
                                               f"{max(left.max_tokens, 0)} are left")
        started = time.monotonic()
        with self._holds.hold(job, guess, guess_usd):
            reply, retries = await self._decide_post(spec, body, key, ask)
            data = await self._decide_read(spec, reply, (*ask.must_not_send, key))
            counted = decisions.parse_usage(data)
            input_tokens, output_tokens = counted or (guess, 0)  # the budget must still move
            usd = decisions.cost_usd(spec, input_tokens, output_tokens)
            await self.store.run(self.seats.record_usage, spec.name, job, input_tokens + output_tokens, usd)
            await self.ports.charge(getattr(principal, "grant_id", None), tokens=input_tokens + output_tokens, usd=usd)
        answers = decisions.parse_answers(data, payload["questions"])
        if isinstance(answers, str):
            raise _NextBrain(f"{spec.name} sent a reply that cannot be used: {answers}")
        def succeeded() -> None:  # a call that was in flight must not end a cooldown (checked on the worker)
            if self.seats.override(spec.name)[0] is None:
                self.seats.mark_success(spec.name)

        await self.store.run(succeeded)
        latency_ms = round((time.monotonic() - started) * 1000)
        self.ports.trace("brain.decide", ctx=ctx, outcome="answered", brain=spec.name, workspace=workspace, job=job,
                         questions=len(ask.questions), body_sha256=digest, redacted=cand.redact, retries=retries,
                         input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=usd, latency_ms=latency_ms)
        return {"brain": spec.name, "model": answers.model or spec.model, "answers": answers.answers,
                "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens, "estimated": counted is None},
                "cost_usd": usd, "latency_ms": latency_ms, "retries": retries}

    async def _decide_post(self, spec: BrainSpec, body: bytes, key: str,
                           ask: decisions.Ask) -> tuple[decisions.Reply, int]:
        """The call, again on the statuses worth another try. Hands back the last reply, and how many
        tries came after the first. A wait longer than a caller should sit through ends the tries."""
        url = (spec.base_url or "").rstrip("/") + "/systemone"
        s = self.settings
        for n in range(s.decide_retries + 1):
            try:
                reply = await self.wire.post(url, body, key, images=ask.uses_images)
            except (httpx.HTTPError, UnicodeError) as exc:  # a key that is no header value is a UnicodeError
                raise _NextBrain(f"{spec.name} did not answer ({type(exc).__name__})") from None
            if reply.status not in decisions.RETRY_STATUSES or n == s.decide_retries:
                return reply, n
            wait = decisions.retry_after(reply.headers.get("retry-after"), self.clock())
            if wait is None:
                wait = s.decide_backoff_seconds * 2 ** n * random.uniform(1.0, 1.25)  # parallel calls spread out
            if wait > s.decide_max_wait_seconds:
                return reply, n
            await asyncio.sleep(wait)
        raise AssertionError("the last try always returns")

    async def _decide_read(self, spec: BrainSpec, reply: decisions.Reply, secrets: Iterable[str]) -> Any:
        """The reply's JSON, or the failure as the brain state it means: 401 and 403 need the owner,
        429 cools the brain down, and what another brain might answer tries the next one."""
        status = reply.status
        if 200 <= status < 300:
            try:
                return json.loads(reply.raw)
            except ValueError:
                return None  # billed or not, it answered: the caller counts it and tries the next brain
        if status in (401, 403):
            await self._decision_auth_failed(spec, f"the endpoint answered HTTP {status}")
            raise _NextBrain(f"{spec.name} rejected its API key (HTTP {status})", state=BrainState.NEEDS_AUTH.value)
        if status == 429:
            now = self.clock()
            wait = decisions.retry_after(reply.headers.get("retry-after"), now)
            def limited() -> tuple[bool, float | None]:  # parallel calls count as one rate limit, not ten
                state, _, until = self.seats.override(spec.name)
                if state == BrainState.COOLING_DOWN:
                    return False, until
                return True, self.seats.mark_rate_limited(spec, resets_at=now + wait if wait else None,
                                                          detail="HTTP 429")

            first, until = await self.store.run(limited)
            if first:
                self.ports.trace("brain.seat", action="cooling_down", brain=spec.name, until=until, reason="HTTP 429")
            raise _NextBrain(f"{spec.name} hit a rate limit", state=BrainState.COOLING_DOWN.value,
                             retry_after=max((until or now) - now, 0.0))
        if status == 404 or status < 400 or status >= 500:  # a redirect lands here: it is never followed
            raise _NextBrain(f"{spec.name} answered HTTP {status}")
        why = decisions.error_text(reply.raw, secrets)
        raise decisions.DecisionRejected(f"{spec.name} refused the request (HTTP {status})" + (f": {why}" if why else ""))

    async def _decision_auth_failed(self, spec: BrainSpec, reason: str) -> None:
        def mark() -> bool:  # check and mark in one call on the worker: parallel calls tell the owner once
            was = self.seats.override(spec.name)[0]
            self.seats.mark_needs_auth(spec.name, reason)
            return was != BrainState.NEEDS_AUTH

        first = await self.store.run(mark)
        self._cred_cache.pop(spec.name, None)
        if first:
            await self._notify_reauth(spec, reason)

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
                status = _read_json(adir / "status.json")
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
                st = _read_json(Path(a["dir"]) / "status.json") or {}
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


class _NextBrain(Exception):
    """This decision brain cannot answer; the next in the chain may. The last one's reason is what the
    caller gets, as a DecisionUnavailable."""

    def __init__(self, reason: str, *, state: str | None = None, retry_after: float | None = None):
        super().__init__(reason)
        self.reason = reason
        self.state = state
        self.retry_after = retry_after


class _AttemptError(Exception):
    def __init__(self, outcome: Outcome, reason: str):
        super().__init__(reason)
        self.outcome = outcome
        self.reason = reason


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
