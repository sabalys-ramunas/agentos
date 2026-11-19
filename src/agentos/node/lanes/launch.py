"""Opening and closing a lane's browser, for the lane manager (agentos.node.lanes, "Lifecycle" there).

open() warms a lane: a warm slot (closing the least recently used idle lane when the cap or the
memory floor needs it), a display, the browser launched with the lane's (or its device's) pin and
proxy, the self-check, and the per-lane scope. Closing always runs to the end, even when cancelled,
and bumps the lane's epoch so an open that lost to a teardown closes what it started. The egress
guard of each session is bound here to what only the manager knows (_bind_guard).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from functools import partial
from typing import Any

from agentos.core.aio import run_to_end as _run_to_end
from agentos.core.errors import AgentosError, EngineError, InvalidRequest, LaneBusy
from agentos.core.logs import log_context
from agentos.core.models import ControlState, LaneInfo, LaneSpec, LaneState, ProxyConfig, ViewerInfo
from agentos.node import gateshapes, human, lanefmt, learned_rules
from agentos.node.browser_output import browser_error
from agentos.node.claims import ACT, Holder
from agentos.node.interfaces import BrowserSession
from agentos.node.lanes.base import _HOLDS_SLOT, NODE_BY, LaneFormatMismatch, _Lane
from agentos.node.store import LaneRecord

log = logging.getLogger("agentos.lanes")

class LaunchMixin:
    """Open and close of the lane manager (agentos.node.lanes.LaneManagerImpl)."""

    def _bind_guard(self, lane_id: str, guard: Any) -> None:
        """Give a session's egress guard what only the manager knows: whether a human has control,
        the lane's events, the stored site list, and the hold that follows a refused publish."""

        def settings() -> tuple[list[str], bool]:
            spec = self.store.require(lane_id).spec
            return list(spec.sites), spec.egress_strict

        async def violation(block: Any) -> None:
            await self.hold(lane_id, reason=f"egress:{block.rule} {block.host}", by=NODE_BY)

        def notice(detail: dict[str, Any]) -> None:
            for callback in list(self._notice_listeners):
                try:
                    callback(lane_id, detail)
                except Exception:
                    log.exception("%s: a block notice listener failed", lane_id)

        guard.human_in_control = lambda: self._control(lane_id).state == ControlState.HUMAN
        guard.events = self._event
        guard.settings = settings
        guard.owner_shapes = gateshapes.source(self.config.home)  # the owner's promoted gate shapes
        guard.learned_rules = learned_rules.source(self.config.home)  # the node's learned profile rules
        guard.on_violation = violation
        guard.on_notice = notice
        guard.on_human_commit = lambda: self._human_commit(lane_id)
        guard.on_effect = lambda rule, actor: self._effect(lane_id, rule.kind, rule.id, actor)

    # ---- open / close -----------------------------------------------------------

    async def open(self, lane_id: str, *, principal_kind: str = "agent") -> LaneInfo:
        rec = self.store.require(lane_id)
        self._ensure_running()
        self._refuse_quarantined(rec)
        await self._expire_hold(lane_id)
        self._check_control(lane_id, principal_kind)
        return await self._open(rec, principal_kind)

    async def _open(self, rec: LaneRecord, principal_kind: str = "agent") -> LaneInfo:
        lane_id = rec.spec.id
        lane = self._lane(lane_id)
        with log_context(lane=lane_id):
            async with lane.op:
                await self._open_locked(rec.spec, lane, principal_kind)
        return self.get(lane_id)

    def _recheck(self, lane_id: str, principal_kind: str) -> None:
        """The gate again, under lane.op: a quarantine or take-over may have landed while the
        caller waited for the lane."""
        rec = self.store.get(lane_id)
        if rec is not None:
            self._refuse_quarantined(rec)
        self._check_control(lane_id, principal_kind)

    async def _open_locked(self, spec: LaneSpec, lane: _Lane, principal_kind: str = "agent") -> None:
        self._recheck(spec.id, principal_kind)
        if lane.state in (LaneState.WARM, LaneState.ACTIVE):
            healthy = getattr(self.displays, "healthy", None)
            display_ok = healthy(spec.id) if callable(healthy) else True
            if getattr(lane.session, "alive", True) and display_ok:
                return
            log.warning("%s: the %s exited on its own; restarting it", spec.id,
                        "display" if display_ok is False else "browser")
            await self._teardown(spec.id, lane)
        self._ensure_running()
        self._check_format(spec.id)
        proxy = self._proxy_for(spec)
        self._check_disk(spec.id)
        await self._reserve(spec.id, lane)  # -> WARMING, or LaneBusy / ResourcesLow
        epoch = lane.epoch

        def stale() -> bool:  # shutdown tore the lane down while it was starting
            return lane.epoch != epoch

        try:
            size = self._window_size(spec)
            first_launch = size is None
            lane.viewer = await self._acquire_display(spec.id, size)
            if stale():
                return await self._abandon(spec.id, lane, None)
            session = await self._launch(spec, lane, proxy)
            if stale():
                return await self._abandon(spec.id, lane, session)
            lane.session = session
            human.allow_idle(session, partial(self._agents_hold, spec.id))
            if first_launch:
                # The screen is known once the fingerprint is pinned: make the display that size.
                fitted = self._window_size(spec)
                if fitted is not None and fitted != (lane.viewer.width, lane.viewer.height):
                    log.info("%s: sizing the display to the pinned screen (%dx%d)", spec.id, *fitted)
                    await _run_to_end(self._release_now(spec.id, lane))
                    if stale():
                        return
                    lane.viewer = await self._acquire_display(spec.id, fitted)
                    if stale():
                        return await self._abandon(spec.id, lane, None)
                    session = await self._launch(spec, lane, proxy)
                    if stale():
                        return await self._abandon(spec.id, lane, session)
                    lane.session = session
                    human.allow_idle(session, partial(self._agents_hold, spec.id))
            if self.config.self_check:
                verdict = await self._self_check(spec.id, lane, proxy)
                if stale():
                    return
                if verdict:
                    reason, record = verdict
                    log.warning("%s: quarantined: %s", spec.id, reason)
                    if record is not None:  # persisted first: a cancelled close cannot lose it
                        await self._quarantine_record(spec.id, record)
                    await self._teardown(spec.id, lane, LaneState.QUARANTINED)
                    lane.quarantine_reason = reason
                    return
            await self._limit_memory(spec.id, lane.session)
            if stale():
                return
            rec = self.store.get(spec.id)
            if rec is not None:
                self._refuse_quarantined(rec)  # quarantined while it started: the except below closes it
            lane.state = LaneState.WARM
            lane.quarantine_reason = None
            lane.last_active = self.clock()
            self.store.soon(self.store.touch, spec.id)
            log.info("%s: warm on %s", spec.id, lane.viewer.display if lane.viewer else "?")
        except BaseException as e:
            await self._teardown(spec.id, lane)  # runs to the end even when cancelled
            if isinstance(e, (AgentosError, asyncio.CancelledError)):
                raise
            raise EngineError(f"{spec.id}: could not open the lane: {browser_error(e, spec.id)}") from e
        finally:
            await self._notify()

    def _check_format(self, lane_id: str) -> None:
        why = lanefmt.check_openable(self.config.lane_dir(lane_id))
        if why:
            raise LaneFormatMismatch(f"lane {lane_id} was not opened: {why}")

    async def _hold_format_lock(self, lane_id: str, lane: _Lane) -> None:
        """A shared lock on the lane's format lock file for as long as a browser runs on it, so
        `agentos migrate run` leaves the lane alone. Waits a little for a migration in progress."""
        if lane.format_lock is not None and lane.format_lock.held:
            return
        self._prepare_lane_dir(lane_id)
        lock = lanefmt.LaneLock(self.config.lane_dir(lane_id))
        if not await asyncio.to_thread(lock.acquire, exclusive=False, wait=10.0):
            raise LaneFormatMismatch(f"lane {lane_id} was not opened: `agentos migrate run` is migrating "
                                     "its directory right now. Try again when it has finished.")
        lane.format_lock = lock
        self._check_format(lane_id)  # what the migration left, now that it cannot change

    async def _launch(self, spec: LaneSpec, lane: _Lane, proxy: ProxyConfig | None) -> BrowserSession:
        assert lane.viewer is not None
        await self._hold_format_lock(spec.id, lane)
        if spec.device:
            # One launch at a time per device: the first makes the device's pin once, and a pin
            # completion or Firefox version move is written once. Each lane still gets its own
            # browser, profile, display and scope.
            async with self._device_lock(spec.device):
                self.ensure_device_pin(spec)
                session = await self.engine.launch(
                    spec,
                    profile_dir=self.config.profile_dir(spec.id),
                    fingerprint_path=self.pin_path_for(spec),
                    display=lane.viewer.display,
                    xauthority=lane.viewer.xauthority,
                    proxy=proxy,
                )
                try:
                    self.sync_device_pins(spec.device)
                except OSError as e:
                    log.warning("%s: could not copy device %s's pin: %s", spec.id, spec.device, type(e).__name__)
        else:
            session = await self.engine.launch(
                spec,
                profile_dir=self.config.profile_dir(spec.id),
                fingerprint_path=self.config.fingerprint_path(spec.id),
                display=lane.viewer.display,
                xauthority=lane.viewer.xauthority,
                proxy=proxy,
            )
        # What the launch did to the pin (pinned, completed, the exit moved) goes in the lane's events.
        for kind, detail in getattr(session, "pin_notes", None) or ():
            self._event(spec.id, kind, detail)
        guard = getattr(session, "guard", None)
        if guard is not None:
            self._bind_guard(spec.id, guard)
        await self._session_started(spec.id, session)
        return session

    # ---- session listeners --------------------------------------------------------

    def add_session_listener(self, callback: Callable[[str, Any], Any]) -> None:
        """callback(lane_id, session) -> None | Awaitable, once each browser of a lane is up and its
        egress guard bound, before anything uses it (agentos.node.expose routes a dev server there)."""
        self._session_listeners.append(callback)

    async def _session_started(self, lane_id: str, session: Any) -> None:
        """Tell the session listeners. One that fails is logged, never the lane's failure: what it adds
        (an exposed dev server) is the owner's convenience, and the lane works without it."""
        for callback in list(self._session_listeners):
            try:
                out = callback(lane_id, session)
                if inspect.isawaitable(out):
                    await out
            except Exception:
                log.exception("%s: a session listener failed", lane_id)

    def live_session(self, lane_id: str) -> Any:
        """The browser session of a lane that has one (warm, or starting once its browser is up), or
        None (cold, closing or quarantined)."""
        lane = self._lanes.get(lane_id)
        if lane is None or lane.state not in (LaneState.WARMING, LaneState.WARM, LaneState.ACTIVE):
            return None
        return lane.session

    async def _abandon(self, lane_id: str, lane: _Lane, session: BrowserSession | None) -> None:
        """An open that lost to shutdown: close what it started, leave the lane cold."""
        log.info("%s: closed while it was starting; stopping what it started", lane_id)

        async def stop() -> None:
            if session is not None:
                try:
                    await session.close()
                except Exception as e:  # noqa: BLE001
                    log.warning("%s: browser close failed: %s", lane_id, browser_error(e, lane_id))
            try:
                await self.displays.release(lane_id)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: display release failed: %s", lane_id, browser_error(e, lane_id))

        await _run_to_end(stop())
        if lane.session is session:
            lane.session = None
        lane.viewer = None
        if lane.session is None and lane.format_lock is not None:
            lane.format_lock.release()
            lane.format_lock = None

    def _window_size(self, spec: LaneSpec) -> tuple[int, int] | None:
        """The size the lane's display is made: the engine's display_size (the pinned screen), else
        its window_size."""
        fn = getattr(self.engine, "display_size", None) or getattr(self.engine, "window_size", None)
        if not callable(fn):
            return None
        try:
            self.ensure_device_pin(spec)
            return fn(self.pin_path_for(spec))
        except Exception:  # noqa: BLE001 - the display falls back to its default size
            return None

    async def _acquire_display(self, lane_id: str, size: tuple[int, int] | None) -> ViewerInfo:
        return await self.displays.acquire(lane_id, size=size)

    async def _limit_memory(self, lane_id: str, session: BrowserSession | None) -> None:
        if self.limiter is None or session is None:
            return
        pids = list(getattr(session, "pids", None) or [])
        pid = getattr(session, "pid", None)
        if pid and pid not in pids:
            pids.append(pid)
        await self.limiter.apply(lane_id, pids)

    def _proxy_for(self, spec: LaneSpec) -> ProxyConfig | None:
        label = spec.proxy_label
        if spec.device:  # the device's proxy is the one its lanes leave through (AUD-02)
            label = self._require_device(spec.device).proxy_label
            if label != spec.proxy_label:
                raise InvalidRequest(f"{spec.id}: its proxy {spec.proxy_label or 'none'} is not its device "
                                     f"{spec.device}'s proxy {label or 'none'}; move it again with: agentos lane "
                                     f"set-device {spec.id} {spec.device}")
        if not label:
            return None
        proxy = self.proxies_loader().get(label)
        if proxy is None:
            raise InvalidRequest(f"{spec.id}: proxy {label!r} is not configured on this node")
        return proxy

    async def _reserve(self, lane_id: str, lane: _Lane) -> None:
        """Claim a warm slot for `lane`, closing the least recently used idle lane when the
        cap is reached or memory is below the floor. Waits up to busy_timeout when every warm
        lane is in use; then LaneBusy, or ResourcesLow when memory is what is missing."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.busy_timeout
        while True:
            async with self._cond:
                self._ensure_running()
                in_use = sum(1 for other in self._lanes.values() if other.state in _HOLDS_SLOT)
                short = self._memory_short()
                if in_use < self.config.max_warm_lanes and short is None:
                    lane.state = LaneState.WARMING
                    return
                victim = self._pick_victim(memory_short=short is not None)
                if victim is None:
                    if short is not None and in_use == 0:
                        raise self._memory_error(lane_id, short)
                    remaining = deadline - loop.time()
                    try:
                        if remaining <= 0:
                            raise TimeoutError
                        # short of memory: look again every second (other work may free some)
                        await asyncio.wait_for(self._cond.wait(), min(remaining, 1.0) if short else remaining)
                    except TimeoutError:
                        if loop.time() < deadline:
                            continue
                        if short is not None:
                            raise self._memory_error(lane_id, short) from None
                        raise LaneBusy(
                            f"all {self.config.max_warm_lanes} warm lanes are busy; {lane_id} could not "
                            f"open within {self.config.busy_timeout:g}s. Try again shortly.") from None
                    continue
                victim_id, victim_lane = victim
                victim_lane.evicting = True
            try:
                async with victim_lane.op:
                    if victim_lane.state == LaneState.WARM and victim_lane.users == 0 and \
                            (short is not None or not victim_lane.claimed()):
                        why = "memory is low" if short is not None else "the warm cap is reached"
                        log.info("closing least recently used lane %s to make room for %s (%s)", victim_id,
                                 lane_id, why)
                        await self._teardown(victim_id, victim_lane)
                        if victim_lane.claimed():  # only a memory floor nothing else could meet closes it
                            self._end_claim(victim_id, victim_lane, "closed",
                                            "the node was short of memory, so the lane was closed and its page is gone")
            finally:
                victim_lane.evicting = False

    def _pick_victim(self, *, memory_short: bool = False) -> tuple[str, _Lane] | None:
        """The least recently used idle lane agents hold (a paused one only after those). Never
        a lane a human holds, one with human input in the last idle_seconds, or one in use. A
        claimed lane only when memory is short and no other lane can go (its holder is told)."""
        now = self.clock()
        idle = []
        for lid, lane in self._lanes.items():
            if not lane.idle() or self._held_by_human(lid) or self._kept_warm(lid):
                continue
            if lane.human_at is not None and now - lane.human_at < self.config.idle_seconds:
                continue
            if lane.claimed() and not memory_short:
                continue
            control = self._controls.get(lid)
            paused = control is not None and control.state == ControlState.PAUSED
            idle.append((lane.claimed(), paused, lane.last_active, lid, lane))
        if not idle:
            return None
        *_, lid, lane = min(idle, key=lambda t: (t[0], t[1], t[2], t[3]))
        return lid, lane

    async def close(self, lane_id: str, *, principal_kind: str = "agent", holder: Holder | None = None) -> LaneInfo:
        """Send the lane to cold. An act: it waits for the calls in flight, and while another caller
        claims the lane it waits in the queue like any other act (then busy). When the wait ends with
        the lane still held by a stuck call (past its deadline, or on a page that no longer answers a
        probe: AccessMixin.stuck_calls), the call is stopped and the browser closed anyway;
        take_close_note() then says so."""
        self.store.require(lane_id)
        self._check_control(lane_id, principal_kind)
        lane = self._lanes.get(lane_id)
        if lane is not None:
            lane.close_note = None
            holder = self._caller(holder, principal_kind)
            self._tell(lane_id, lane, holder)
            try:
                waiter = await self._enter(lane_id, lane, holder, ACT, principal_kind)
            except LaneBusy:
                claim = lane.access.claim
                if claim is not None and not claim.implicit and not lane.access.holder_inside():
                    raise  # a claim with nobody stuck inside: its holder carries on later
                stuck = await self.stuck_calls(lane_id)
                if not stuck:
                    raise
                self._check_control(lane_id, principal_kind)
                lane.close_note = {"stuck_calls": [{"tool": c.tool, "ran_s": round(self._ran_s(c), 1)} for c in stuck],
                                   "detail": "a call was stuck on the lane, so it was stopped and the browser was "
                                             "closed; the next call opens it again"}
                await self.close_stuck(lane_id, stuck, "browser_close found the lane held by a stuck call")
                lane.quarantine_reason = None
                await self._notify()
                return self.get(lane_id)
            try:
                async with lane.op:
                    self._check_control(lane_id, principal_kind)  # a take-over while we waited wins
                    if lane.state != LaneState.COLD or lane.session is not None or lane.viewer is not None:
                        await self._teardown(lane_id, lane)
                    lane.quarantine_reason = None
            finally:
                self._leave(lane_id, lane, waiter)
            await self._notify()
        return self.get(lane_id)

    def take_close_note(self, lane_id: str) -> dict[str, Any] | None:
        """What the last close() of this lane had to do about a stuck call, once (browser_close reports it)."""
        lane = self._lanes.get(lane_id)
        if lane is None:
            return None
        note, lane.close_note = lane.close_note, None
        return note

    async def _teardown(self, lane_id: str, lane: _Lane, final: LaneState = LaneState.COLD) -> None:
        """Close the browser and free the display, to the end even when cancelled."""
        lane.epoch += 1
        try:
            with log_context(lane=lane_id):  # an eviction runs inside another lane's open
                await _run_to_end(self._release_now(lane_id, lane))
        finally:
            lane.state = final
            if final == LaneState.COLD:
                log.info("%s: cold", lane_id)

    @staticmethod
    def _end_drive(lane: _Lane | None, why: str) -> None:
        """Control left the agent: the egress guard stops gating and drops the commit gate's route
        (LaneGuard.end_drive), so the browser caches again."""
        guard = getattr(getattr(lane, "session", None), "guard", None)
        end = getattr(guard, "end_drive", None)
        if callable(end):
            try:
                end(why)
            except Exception:
                log.warning("could not end the agent's drive on the egress guard", exc_info=True)

    async def _release_now(self, lane_id: str, lane: _Lane) -> None:
        self._end_drive(lane, "the lane closes")
        session, lane.session = lane.session, None
        if session is not None:
            try:
                await session.close()
            except Exception as e:  # noqa: BLE001
                log.warning("%s: browser close failed: %s", lane_id, browser_error(e, lane_id))
        if lane.viewer is not None or self.displays.get(lane_id) is not None:
            try:
                await self.displays.release(lane_id)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: display release failed: %s", lane_id, browser_error(e, lane_id))
        lane.viewer = None
        format_lock, lane.format_lock = lane.format_lock, None
        if format_lock is not None:
            format_lock.release()

    async def _notify(self) -> None:
        async with self._cond:
            self._cond.notify_all()
