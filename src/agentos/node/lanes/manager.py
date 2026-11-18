"""LaneManagerImpl: the lane manager, made of the mixins of this package (agentos.node.lanes has
the whole design), plus what is the manager's own: its start and shutdown, the reads (list, get,
the lane a page belongs to), the lane events, the reaper loop, and build_lane_manager, the
production wiring. The resource probes are read through agentos.node.lanes.resources when a
manager is made, so a test patches them there.
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from agentos.core.config import NodeConfig, load_proxies
from agentos.core.errors import ServiceUnavailable
from agentos.core.models import ControlState, LaneControl, LaneInfo, LaneState, Signal, ViewerInfo, utcnow
from agentos.node.claims import DEFAULT_MAX_TOTAL_S, SYSTEM, ClaimsFile, holder_text
from agentos.node.devices import DevicesMixin
from agentos.node.handback import HandbackTracker
from agentos.node.interfaces import DisplayManager, Engine
from agentos.node.lanes import resources as _resources
from agentos.node.lanes.access import AccessMixin
from agentos.node.lanes.base import (
    _HOLDS_SLOT,
    ControlListener,
    Gate,
    HoldPolicy,
    NodeIpProbe,
    NoticeListener,
    ProxiesLoader,
    ResourcesLow,
    _Lane,
    _private_dir,
)
from agentos.node.lanes.control import ControlMixin, _is_hold, default_hold_policy
from agentos.node.lanes.frames import FramesMixin
from agentos.node.lanes.launch import LaunchMixin
from agentos.node.lanes.quarantine import SELF_CHECK_URL, QuarantineMixin, direct_exit_ip_probe
from agentos.node.lanes.resources import (
    DEFAULT_DISK_FLOOR_BYTES,
    DEFAULT_MEMORY_FLOOR_MB,
    ResourcesMixin,
    ScopeLimiter,
)
from agentos.node.lanes.specs import SpecsMixin
from agentos.node.lanes.watch import SignalWatch
from agentos.node.memwatch import MemoryWatch, resolve_ceiling
from agentos.node.store import LaneRecord, LaneStore, QuarantineRecord

log = logging.getLogger("agentos.lanes")

SHUTDOWN_USE_WAIT = 5.0            # shutdown lets a tool call in flight finish its step for this long
_DEFAULT: Any = object()


class LaneManagerImpl(ControlMixin, AccessMixin, QuarantineMixin, LaunchMixin, ResourcesMixin, SpecsMixin,
                      DevicesMixin, FramesMixin):
    # probe_exit_ip() judges the exit itself (IP-literal proxies, pinned gateway exits, never the
    # node's IP) and quarantines on a mismatch, so health checks take its verdict.
    judges_exit_ip = True

    def __init__(
        self,
        config: NodeConfig,
        store: LaneStore,
        engine: Engine,
        displays: DisplayManager,
        proxies_loader: ProxiesLoader | None = None,
        *,
        self_check_url: str = SELF_CHECK_URL,
        self_check_timeout: float = 30.0,
        reaper_interval: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = utcnow,
        hold_policy: HoldPolicy | None = _DEFAULT,
        node_ip_probe: NodeIpProbe | None = None,
        memory_floor_mb: int | None = None,
        disk_floor_bytes: int | None = None,
        memory_probe: Callable[[], int | None] | None = None,
        disk_probe: Callable[[Path], int | None] | None = None,
        lane_memory_max_mb: int | None = None,
        limiter: ScopeLimiter | None = None,
        trash_dir: Path | None = None,
        queue_timeout: float | None = None,
        claim_max_total_s: float | None = None,
    ):
        self.config = config
        self.store = store
        self.engine = engine
        self.displays = displays
        self.proxies_loader = proxies_loader or (lambda: load_proxies(config))
        self.self_check_url = self_check_url
        self.self_check_timeout = self_check_timeout
        self.reaper_interval = reaper_interval or max(
            1.0, min(30.0, config.idle_seconds / 4, max(config.control_idle_seconds, 4) / 4))
        self.clock = clock
        self.now = now  # wall clock for LaneControl times (they survive a restart)
        self.hold_policy = default_hold_policy() if hold_policy is _DEFAULT else hold_policy
        self.node_ip_probe = node_ip_probe
        self.memory_floor_mb = _setting(memory_floor_mb, config, "memory_floor_mb", DEFAULT_MEMORY_FLOOR_MB)
        self.disk_floor_bytes = _setting(disk_floor_bytes, config, "disk_floor_bytes", DEFAULT_DISK_FLOOR_BYTES)
        # the host's, looked up when the manager is made
        self.memory_probe = memory_probe or _resources.available_memory_mb
        self.disk_probe = disk_probe or _resources.disk_free_bytes
        # The per-lane memory ceiling (agentos.node.memwatch): set in node.toml, 0 for none, or by default
        # LANE_MEMORY_DEFAULT_MB. A set cap asks systemd for a scope by itself; the default one rides only on
        # the default scopes (under a capped service a scope would take the browser out of that cap, and
        # lane_scopes = false stays an off switch). The watchdog (a step of the reaper loop) uses it either way.
        self.lane_memory_ceiling = resolve_ceiling(_setting(lane_memory_max_mb, config, "lane_memory_max_mb", None))
        ceiling, source = self.lane_memory_ceiling
        self.memory_watch = MemoryWatch(self, ceiling_mb=ceiling)
        cpu_pct = getattr(config, "lane_cpu_max_pct", None)
        default_scopes = bool(getattr(config, "lane_scopes", True) and _resources.default_scopes_fit())
        max_mb = ceiling if source == "set" or (source == "default" and default_scopes) else None
        scoped = bool(source == "set" or cpu_pct or default_scopes)
        self.limiter = limiter or (ScopeLimiter(max_mb, cpu_max_pct=cpu_pct)
                                   if scoped and ScopeLimiter.supported() else None)
        self.trash_dir = trash_dir or config.home / "trash"
        self.queue_timeout = float(_setting(queue_timeout, config, "queue_timeout", 60.0))
        self.claim_max_total_s = float(_setting(claim_max_total_s, config, "claim_max_seconds", DEFAULT_MAX_TOTAL_S))
        self._claims_file = ClaimsFile(config.home / "run" / "claims.json")
        self._lanes: dict[str, _Lane] = {}
        self._cond = asyncio.Condition()
        self._reaper: asyncio.Task | None = None
        self._closed = False
        self._controls: dict[str, LaneControl] = {}
        self._control_cond = asyncio.Condition()
        self._listeners: list[ControlListener] = []
        self._notice_listeners: list[NoticeListener] = []
        self._commit_listeners: list[Callable[[str], Any]] = []
        self._effect_listeners: list[Callable[[str, str, str | None, str], Any]] = []
        # callback(lane_id, session) once each browser is up and its guard bound (agentos.node.expose)
        self._session_listeners: list[Callable[[str, Any], Any]] = []
        self._gates: list[Gate] = []
        self._keep_warm: list[Callable[[str], bool]] = []
        self._agent_tasks: dict[str, set[asyncio.Task]] = {}  # non-human work inside using(), per lane
        self._background: set[asyncio.Task] = set()  # slow async listeners left running
        self._input_persisted: dict[str, float] = {}  # monotonic time of the last write
        self._input_dirty: set[str] = set()
        self._denied_at: dict[str, float] = {}
        self._signals: dict[str, tuple[Signal, float]] = {}  # last signal written, and when (monotonic)
        # quarantines whose write is still on the store's worker (waiting for a lock): refused already
        self._quarantining: dict[str, QuarantineRecord] = {}
        # quarantines whose write failed: refused in memory until a human clears them (_clear_quarantine)
        self._unsaved_quarantines: dict[str, QuarantineRecord] = {}
        self._page_lanes: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()
        self._reservations: dict[str, int] = {}  # memory other work reserved (brains), MB
        self._node_ip: str | None = None
        self._node_ip_at: float | None = None
        self._disk_warned = False
        # what changed while agents did not have a lane (agentos.node.handback): captured when they
        # lose control, compared when they get it back
        self.handback = HandbackTracker(self)
        self._listeners.append(self.handback.on_control)

    # ---- lifecycle of the manager ---------------------------------------------

    async def start(self) -> None:
        self._closed = False
        _private_dir(self.config.home / "lanes")
        self.store.list()  # fail early on a broken database
        sweep = getattr(self.engine, "sweep_orphans", None)
        if sweep:
            await sweep(self.config.home / "lanes")
        sweep_displays = getattr(self.displays, "sweep", None)
        if sweep_displays:
            await sweep_displays()
        for rec in self.store.list():  # control and quarantine survive a restart; lanes do not
            if rec.control is not None:
                self._controls[rec.spec.id] = rec.control
        self.repair_device_pins()  # each device lane's pin copy matches its device (AUD-02)
        self._load_claim_notes()  # claims do not: their holders hear on their next call
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_loop(), name="agentos-lane-reaper")

    async def shutdown(self) -> None:
        """Close every lane. A tool call in flight gets SHUTDOWN_USE_WAIT to finish its step.
        Afterwards nothing opens a browser again (open and using raise). Each lane that was open
        gets a `closed_by_shutdown` event naming who was on it, and the log says so, so a lane
        another session held never disappears without a trace (`agentos node restart` asks first)."""
        self._closed = True  # claims stay in the claims file: their holders hear of the restart
        for row in self.holders():
            who = "; ".join(holder_text(h) for h in row["holders"]) or "nobody"
            log.warning("%s: closed because agentosd is stopping (%s); it opens again from its profile on its "
                        "next call", row["id"], who)
            self._event(row["id"], "closed_by_shutdown", {"state": row["state"], "holders": row["holders"]})
        if self._reaper:
            self._reaper.cancel()
            try:
                await self._reaper  # a reap in progress finishes closing its browser first
            except asyncio.CancelledError:
                pass
            self._reaper = None
        await self.handback.close()
        await asyncio.gather(*(self._shutdown_lane(lid, lane) for lid, lane in list(self._lanes.items())),
                             return_exceptions=True)
        self._flush_human_input()
        release_all = getattr(self.displays, "release_all", None)
        if release_all:
            await release_all()

    async def _shutdown_lane(self, lane_id: str, lane: _Lane) -> None:
        if lane.state == LaneState.COLD and lane.session is None and lane.viewer is None:
            return
        used = await self._enter_system(lane_id, lane, SHUTDOWN_USE_WAIT)
        if used is None:
            log.warning("%s: a tool call is still using it at shutdown; closing it anyway", lane_id)
        try:
            try:
                await asyncio.wait_for(lane.op.acquire(), 30.0)
            except TimeoutError:
                log.warning("%s: still busy at shutdown; closing it anyway", lane_id)
                await self._teardown(lane_id, lane)
                return
            try:
                await self._teardown(lane_id, lane)
            finally:
                lane.op.release()
        finally:
            if used is not None:
                self._leave(lane_id, lane, used)

    def holders(self) -> list[dict[str, Any]]:
        """Every lane a restart would close, with who is on it: a claim (or a flow or connector
        run's claim), a person who took control or paused it, an agent driving it through the commit
        gate, calls in flight, callers waiting, live views open. A lane with no holder is idle and
        only loses its page (its cookies are kept). For `agentos node restart` and the ops socket."""
        return [{"id": lane_id, "state": lane.state.value, "holders": self._lane_holders(lane_id, lane)}
                for lane_id, lane in sorted(self._lanes.items())
                if lane.state in _HOLDS_SLOT or lane.session is not None]

    def _lane_holders(self, lane_id: str, lane: _Lane) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []

        def add(kind: str, who: str, **extra: Any) -> None:
            item = {"kind": kind, "who": who[:80], **extra}
            if not any(f["kind"] == kind and f["who"] == item["who"] for f in found):
                found.append(item)

        claim = lane.access.claim
        if claim is not None:
            add("run" if claim.implicit else "claim", claim.holder.label, purpose=claim.purpose,
                held_s=round(self.clock() - claim.started))
        control = self._controls.get(lane_id)
        if control is not None and control.state != ControlState.AGENT:
            add("takeover" if control.state == ControlState.HUMAN else "paused", control.by or "a person")
        guard = getattr(lane.session, "guard", None)
        driver = getattr(guard, "driver", None)
        if driver is not None and callable(getattr(guard, "driving", None)) and guard.driving():
            add("drive", f"{driver.kind} {driver.id}")
        for w in lane.access.inside():
            if w.mode != SYSTEM:
                add("call", w.holder.label)
        waiting = sum(1 for w in lane.access.queue if w.mode != SYSTEM and not w.done)
        if waiting:
            add("waiting", f"{waiting} caller{'s' if waiting != 1 else ''}")
        views = max(lane.viewers, lane.watchers)
        if views:
            add("view", f"{views} live view{'s' if views != 1 else ''}")
        return found

    def _ensure_running(self) -> None:
        if self._closed:
            raise ServiceUnavailable("the lane manager has shut down (agentosd is stopping); no lane opens "
                                     "until it starts again")

    def events(self, lane_id: str | None = None, *, limit: int = 100) -> list[Any]:
        """Recent lane events, newest first (holds, quarantines, proxy changes, deletes)."""
        return self.store.events(lane_id, limit=limit)

    def _event(self, lane_id: str, kind: str, detail: dict[str, Any]) -> None:
        try:
            # never waits for a lock another process holds; queued in order when it would
            self.store.soon(self.store.add_event, lane_id, kind, detail, when=self.now())
        except Exception:
            log.warning("%s: could not record a %s event", lane_id, kind, exc_info=True)

    # ---- reads ------------------------------------------------------------------

    def _info(self, rec: LaneRecord) -> LaneInfo:
        lane_id = rec.spec.id
        control = self._controls.get(lane_id) or rec.control or LaneControl()
        held = _is_hold(control)
        common: dict[str, Any] = {
            "spec": rec.spec, "last_used": rec.last_used, "control": control,
            "signal": rec.signal.signal if rec.signal else None,
            "signal_at": rec.signal.at if rec.signal else None,
            "hold": control.reason if held else None,
            "hold_until": control.hold_until if held else None,
        }
        lane = self._lanes.get(lane_id)
        if lane is not None:
            claim = lane.access.claim
            common["claim"] = claim.public(self.clock(), self.claim_max_total_s) if claim is not None else None
            common["queue_length"] = len(lane.access.queue)
        exit_ip = (lane.exit_ip if lane else None) or rec.exit_ip
        quarantine = rec.quarantine or self._unsaved_quarantines.get(lane_id)
        if quarantine is not None:
            return LaneInfo(**common, state=LaneState.QUARANTINED, exit_ip=exit_ip,
                            quarantine_reason=quarantine.reason)
        if lane is None:
            return LaneInfo(**common, exit_ip=exit_ip)
        return LaneInfo(
            **common,
            state=lane.state,
            viewer=lane.viewer if lane.state in (LaneState.WARM, LaneState.ACTIVE) else None,
            exit_ip=exit_ip,
            quarantine_reason=lane.quarantine_reason if lane.state == LaneState.QUARANTINED else None,
        )

    def list(self) -> list[LaneInfo]:
        return [self._info(rec) for rec in self.store.list()]

    def get(self, lane_id: str) -> LaneInfo:
        return self._info(self.store.require(lane_id))

    def view(self, lane_id: str) -> ViewerInfo | None:
        return self.get(lane_id).viewer

    async def read_signed_out(self, lane_id: str, url: str, text: str, *, timeout_ms: int = 20_000) -> dict[str, Any]:
        """Whether `text` shows at `url` for a signed-out visitor with this lane's fingerprint and
        proxy (AUD-22; agentos.node.signed_out). The lane must be warm: its browser lends the
        launch options. The throwaway browser gets a display of its own, so nothing shows on the
        lane's screen; it counts against the memory floor like a lane."""
        spec = self.store.require(lane_id).spec
        lane = self._lanes.get(lane_id)
        session = lane.session if lane is not None and lane.state in (LaneState.WARM, LaneState.ACTIVE) else None
        read = getattr(session, "read_signed_out", None)
        if read is None:
            raise ServiceUnavailable(f"{lane_id} is not open in an engine that can read a page signed out")
        short = self._memory_short()
        if short is not None:
            raise ResourcesLow(f"only {max(short[0], 0)} MB of memory is free (agentos keeps {short[1]} MB free), "
                               "so the signed-out browser was not started")
        key = f"{lane_id}+signed-out"  # a display slot of its own, never the lane's
        viewer = await self._acquire_display(key, self._window_size(spec))
        try:
            return await read(url, text, display=viewer.display, timeout_ms=timeout_ms, lane_id=lane_id,
                              sites=tuple(spec.sites), strict=spec.egress_strict)
        finally:
            await asyncio.shield(self.displays.release(key))

    def _lane(self, lane_id: str) -> _Lane:
        lane = self._lanes.get(lane_id)
        if lane is None:
            lane = self._lanes[lane_id] = _Lane()
        return lane

    def lane_for_page(self, page: Any) -> str | None:
        """The lane a page belongs to (any page of a lane's browser), or None."""
        for key in (getattr(page, "context", None), page):
            if key is None:
                continue
            try:
                lane_id = self._page_lanes.get(key)
            except TypeError:
                continue
            if lane_id is not None:
                return lane_id
        return None

    def _register_page(self, lane_id: str, page: Any) -> None:
        for key in (getattr(page, "context", None), page):
            if key is None:
                continue
            try:
                self._page_lanes[key] = lane_id
            except TypeError:
                continue

    def watch(self, interaction: Any) -> SignalWatch:
        """`interaction`, reporting every signal it reads to this manager (see SignalWatch). The
        hand-back note reads the page through the unwatched one (it reports nothing)."""
        watched = interaction if isinstance(interaction, SignalWatch) else SignalWatch(interaction, self)
        self.handback.interaction = watched.inner
        return watched

    # ---- idle reaper --------------------------------------------------------------

    async def reap_idle(self) -> list[str]:
        """Close warm lanes idle for longer than idle_seconds. Returns the closed lane ids."""
        closed = []
        for lane_id, lane in list(self._lanes.items()):
            if not (lane.idle() and self.clock() - lane.last_active >= self.config.idle_seconds):
                continue
            if self._held_by_human(lane_id) or self._kept_warm(lane_id) or lane.claimed():
                continue
            async with lane.op:
                if lane.state == LaneState.WARM and lane.users == 0 and not self._held_by_human(lane_id) and \
                        not lane.claimed() and self.clock() - lane.last_active >= self.config.idle_seconds:
                    log.info("%s: idle for %ds; closing", lane_id, self.config.idle_seconds)
                    await self._teardown(lane_id, lane)
                    closed.append(lane_id)
        if closed:
            await self._notify()
        return closed

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(self.reaper_interval)
            for step, what in ((self.reap_holds, "hold expiry"), (self.reap_idle_control, "idle hand-back"),
                               (self.reap_claims, "claim expiry")):
                try:
                    await step()
                except Exception:
                    log.exception("%s failed", what)
            try:
                self._flush_human_input()
            except Exception:
                log.exception("could not save human input times")
            try:
                self._watch_disk()
            except Exception:
                log.exception("disk check failed")
            try:
                await self.memory_watch.step(self.clock())  # every WATCH_S at most (agentos.node.memwatch)
            except Exception:
                log.exception("memory watchdog round failed")
            try:
                await self.reap_idle()
            except Exception:
                log.exception("idle reaper failed")


def _setting(value: Any, config: NodeConfig, name: str, default: Any) -> Any:
    """An explicit argument, else node.toml (once NodeConfig has the field), else the default."""
    if value is not None:
        return value
    from_config = getattr(config, name, None)
    return from_config if from_config is not None else default


def build_lane_manager(config: NodeConfig, **kwargs: Any) -> LaneManagerImpl:
    """The production wiring: SQLite store, Camoufox engine (with the lanes' Web Bot Auth
    identities, which sign nothing until the owner lists a host), Xvfb/x11vnc displays, and the
    node's own IP read directly for the self-check. Constructing it starts nothing; the CLI
    can use it for create, import, delete and set-proxy. `displays` replaces the display manager
    (the stealth check's sweeps only its own home)."""
    from agentos.node.camoufox.engine import CamoufoxEngine
    from agentos.node.display import DisplayManagerImpl
    from agentos.webbotauth.route import IdentityProvider

    kwargs.setdefault("node_ip_probe", direct_exit_ip_probe(kwargs.get("self_check_url", SELF_CHECK_URL)))
    store = LaneStore(config.db_path)
    engine = CamoufoxEngine(identity=IdentityProvider(config, events=store.add_event),
                            identity_os=list(getattr(config, "lane_os", None) or []) or None)
    displays = kwargs.pop("displays", None) or DisplayManagerImpl(config)
    return LaneManagerImpl(config, store, engine, displays, **kwargs)
