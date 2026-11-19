"""In-memory lane managers, for the unit tests and for `agentos dev up`.

FakeLaneManager is the dict-backed LaneManager the tests have always used. It covers the contract in
agentos.node.interfaces except the human control actions (take over, hand back, stop), which
ControlFakeLaneManager adds: tools pick the signed-link viewer path when a manager has take_control,
and tests written for the plain path rely on it missing. LegacyLaneManager is the manager from before
the control contract (`using(lane_id)` only). Holds and quarantines block non-human callers exactly as
the real gate does; `hold_policy(signal, *, now)` -> None | (reason, until) turns report_signal into holds.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import shutil
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from agentos.core.config import NodeConfig
from agentos.core.errors import (
    AgentPaused,
    ClaimLost,
    ConfirmRequired,
    HumanInControl,
    InvalidRequest,
    LaneBusy,
    LaneExists,
    LaneNotFound,
    LaneQuarantined,
    NotFound,
)
from agentos.core.models import (
    ActionKind,
    ControlState,
    DeviceSpec,
    LaneControl,
    LaneInfo,
    LaneSpec,
    LaneState,
    Signal,
    ViewerInfo,
    utcnow,
)
from agentos.core.pauses import paused_words
from agentos.core.signals import QUIET
from agentos.dev.display import VncServer
from agentos.dev.image import Raster
from agentos.node.claims import (
    ACT,
    CLAIM,
    READ,
    RUN,
    TICK_S,
    Holder,
    LaneAccess,
    Waiter,
    clamp_ttl,
    clean_purpose,
)
from agentos.node.holds import hold_for

log = logging.getLogger(__name__)
CONTROL_POLL_S = 0.2  # browser_wait_handback looks at the lane this often


class _FormlessLocator:
    async def evaluate(self, js: str, arg: Any = None) -> None:
        return None


class FakePage:
    """A lane's tab. A tab with a `driver` (the fake browser of agentos.dev.web) reads its url and
    title from it; `refs`, `refs_root` and `frame` are the interaction's bookkeeping on the tab."""

    def __init__(self, lane_id: str, driver: Any = None):
        self.lane_id = lane_id
        self.driver = driver
        self._url = "about:blank"
        self._title = ""
        self.refs: dict[str, Any] = {}
        self.refs_root: Any = None
        self.frame: tuple[int, Any] | None = None

    @property
    def url(self) -> str:
        return self._url if self.driver is None else self.driver.url

    @url.setter
    def url(self, value: str) -> None:
        if self.driver is None:
            self._url = value
        else:
            self.driver.url = value

    @property
    def title(self) -> str:
        return self._title if self.driver is None else self.driver.root.name

    async def reload(self, **_: Any) -> None:
        """Load the page again, as a browser does: what was typed is gone (the guard reloads an
        agent's page before a flow takes the lane)."""
        if self.driver is not None:
            self.driver.values = {}
            await self.driver.navigate(self.driver.url)

    def locator(self, selector: str) -> _FormlessLocator:
        """What browser_act reads before a click (agentos.node.fields.read_form_diff): the fake
        browser has no forms, so every click and Enter submits none."""
        return _FormlessLocator()

    @title.setter
    def title(self, value: str) -> None:
        self._title = value


class FakeLaneManager:
    """LaneManager over a dict. `fail_with` makes the next call raise, to test error mapping."""

    def __init__(self, config: NodeConfig | None = None):
        self.config = config or NodeConfig()
        self.lanes: dict[str, LaneInfo] = {}
        self.pages: dict[str, FakePage] = {}
        self.started = False
        self.shut_down = False
        self.fail_with: BaseException | None = None
        self.calls: list[tuple[str, str]] = []
        self.principals: list[str] = []  # principal_kind of every using() call
        self.listeners: list = []
        self.hold_policy = None
        self.exit_ips: dict[str, str] = {}  # what probe_exit_ip returns for a warm lane
        self.events_log: list[dict] = []
        self.reservations: dict[str, int] = {}
        self.deleted: list[str] = []
        self.open_views: dict[str, list[int]] = {}  # lane -> [viewers, watchers, ever opened]
        self.device_specs: dict[str, DeviceSpec] = {}  # devices (AUD-02), in memory
        self.launched: set[str] = set()  # lanes that have warmed once: their site has seen a device
        # proxies by label, as the node's lane manager has them: none here, but an agent environment
        # adds its tunnel (agentos.environments), which an environment's dev tab reads pages through
        self.proxies_loader: Callable[[], dict[str, Any]] = dict

    def _maybe_fail(self) -> None:
        if self.fail_with is not None:
            exc, self.fail_with = self.fail_with, None
            raise exc

    def _require(self, lane_id: str) -> LaneInfo:
        if lane_id not in self.lanes:
            raise LaneNotFound(f"no lane {lane_id!r}; see browsers_list")
        return self.lanes[lane_id]

    def _warm(self, info: LaneInfo) -> None:
        if info.state in (LaneState.WARM, LaneState.ACTIVE):
            return
        slot = sorted(self.lanes).index(info.spec.id)
        self.launched.add(info.spec.id)
        info.state = LaneState.WARM
        info.quarantine_reason = None
        info.viewer = ViewerInfo(
            display=f":{self.config.display_base + slot}",
            vnc_port=self.config.vnc_base + slot,
            viewer_port=self.config.viewer_base + slot,
        )

    async def start(self) -> None:
        self.started = True

    async def shutdown(self) -> None:
        for info in self.lanes.values():
            info.state, info.viewer = LaneState.COLD, None
        # A dev tab's context sits in the node's page registries (egress guard, devtools capture),
        # whose values hold the context again, so the entries never go by themselves: each manager
        # kept its tabs, itself and the runtime behind its listeners (every store's SQLite files)
        # for the rest of the process. The node does the same when a lane's browser closes.
        from agentos.node.camoufox.pages import forget_context

        for page in self.pages.values():
            context = getattr(page, "context", None)
            if context is not None:
                forget_context(context)
        self.shut_down = True

    async def create(self, spec: LaneSpec) -> LaneInfo:
        self._maybe_fail()
        if spec.id in self.lanes:
            raise LaneExists(f"lane {spec.id!r} already exists")
        with contextlib.suppress(OSError):  # as a real lane has: files live here. A test may have no home at all
            self.config.lane_dir(spec.id).mkdir(parents=True, exist_ok=True)
        if spec.device:
            device = self._device(spec.device)
            if spec.proxy_label and spec.proxy_label != device.proxy_label:
                raise InvalidRequest(f"device {device.name} leaves through proxy {device.proxy_label or 'none'}; a "
                                     "lane on it uses the device's proxy")
            spec = spec.model_copy(update={"proxy_label": device.proxy_label})
        self.lanes[spec.id] = LaneInfo(spec=spec)
        return self.lanes[spec.id]

    async def import_profile(self, spec: LaneSpec, profile_src: Path, fingerprint_src: Path | None) -> LaneInfo:
        info = await self.create(spec)
        shutil.copytree(profile_src, self.config.profile_dir(spec.id))
        if fingerprint_src is not None:
            shutil.copy2(fingerprint_src, self.config.fingerprint_path(spec.id))
        return info

    async def set_sites(self, lane_id: str, sites: list[str], *, strict: bool | None = None,
                        by: str = "owner") -> LaneInfo:
        info = self._require(lane_id)
        update: dict[str, Any] = {"sites": sites}
        if strict is not None:
            update["egress_strict"] = strict
        try:
            info.spec = LaneSpec.model_validate({**info.spec.model_dump(), **update})
        except ValueError as e:
            raise InvalidRequest(str(e).splitlines()[-1]) from None
        return info

    def list(self) -> list[LaneInfo]:
        return [self.lanes[k] for k in sorted(self.lanes)]

    def get(self, lane_id: str) -> LaneInfo:
        self._maybe_fail()
        return self._require(lane_id)

    async def open(self, lane_id: str, *, principal_kind: str = "agent") -> LaneInfo:
        self.calls.append(("open", lane_id))
        self._maybe_fail()
        info = self._require(lane_id)
        self._gate(info, principal_kind)
        self._warm(info)
        return info

    async def close(self, lane_id: str, *, principal_kind: str = "agent") -> LaneInfo:
        self.calls.append(("close", lane_id))
        self._maybe_fail()
        info = self._require(lane_id)
        self._check_control(info, principal_kind)
        if info.state != LaneState.QUARANTINED:
            info.state = LaneState.COLD
        info.viewer = None
        return info

    def _check_control(self, info: LaneInfo, principal_kind: str) -> None:
        if principal_kind == "human":
            return
        lane_id = info.spec.id
        if info.control.state is ControlState.HUMAN:
            raise HumanInControl(f"a human has taken control of {lane_id}")
        if info.control.state is ControlState.PAUSED:
            raise AgentPaused(paused_words(lane_id, info.control))

    def _gate(self, info: LaneInfo, principal_kind: str) -> None:
        if info.state is LaneState.QUARANTINED:
            raise LaneQuarantined(f"lane {info.spec.id} is quarantined ({info.quarantine_reason})")
        self._check_control(info, principal_kind)

    def _set_control(self, lane_id: str, new: LaneControl) -> LaneInfo:
        info = self._require(lane_id)
        old, info.control = info.control, new
        held = bool(new.reason) and new.state is not ControlState.AGENT
        info.hold, info.hold_until = (new.reason, new.hold_until) if held else (None, None)
        if new != old:
            for callback in list(self.listeners):
                callback(lane_id, old, new)
        return info

    def _event(self, lane_id: str, kind: str, **detail) -> None:
        self.events_log.append({"lane_id": lane_id, "kind": kind, "detail": detail, "at": utcnow()})

    # ---- control listeners and reads ----

    def add_control_listener(self, callback) -> None:
        self.listeners.append(callback)

    def remove_control_listener(self, callback) -> None:
        if callback in self.listeners:
            self.listeners.remove(callback)

    def control(self, lane_id: str) -> LaneControl:
        return self._require(lane_id).control

    def events(self, lane_id: str | None = None, *, limit: int = 100) -> list[dict]:
        found = [e for e in reversed(self.events_log) if lane_id is None or e["lane_id"] == lane_id]
        return found[:limit]

    # ---- holds, signals, quarantine ----

    async def hold(self, lane_id: str, *, reason: str, until=None, by: str = "node") -> LaneInfo:
        self.calls.append(("hold", lane_id))
        info = self._require(lane_id)
        if not (reason or "").strip():
            raise InvalidRequest("a hold needs a reason")
        old = info.control
        if old.reason and old.state is not ControlState.AGENT and old.hold_until is None:
            return info  # a hold only a human clears is never weakened
        state = ControlState.HUMAN if old.state is ControlState.HUMAN else ControlState.PAUSED
        self._event(lane_id, "held", reason=reason, until=until, by=by)
        return self._set_control(lane_id, old.model_copy(update={
            "state": state, "by": old.by if state is ControlState.HUMAN else by, "since": old.since or utcnow(),
            "reason": reason, "hold_until": until}))

    async def report_signal(self, lane_id: str, signal, detail: str | None = None) -> LaneInfo:
        self.calls.append(("report_signal", lane_id))
        info = self._require(lane_id)
        info.signal, info.signal_at = Signal(getattr(signal, "value", signal)), utcnow()
        if self.hold_policy is None or info.signal in QUIET:
            return info
        result = self.hold_policy(info.signal, now=info.signal_at)
        if not result:
            return info
        reason, until = (result, None) if isinstance(result, str) else result
        return await self.hold(lane_id, reason=reason, until=until)

    def watch(self, interaction):
        """No page-to-lane mapping here: the interaction is returned as it is."""
        return interaction

    def _views(self, lane_id: str, i: int, step: int) -> None:
        counts = self.open_views.setdefault(lane_id, [0, 0, 0])
        counts[i] = max(0, counts[i] + step)
        if step > 0:
            counts[2] += 1

    def viewer_attached(self, lane_id: str) -> None:
        self.calls.append(("viewer_attached", lane_id))
        self._views(lane_id, 0, 1)

    def viewer_detached(self, lane_id: str) -> None:
        self.calls.append(("viewer_detached", lane_id))
        self._views(lane_id, 0, -1)

    def view_opened(self, lane_id: str) -> None:
        self.calls.append(("view_opened", lane_id))
        self._views(lane_id, 1, 1)

    def view_closed(self, lane_id: str) -> None:
        self.calls.append(("view_closed", lane_id))
        self._views(lane_id, 1, -1)

    def views(self, lane_id: str) -> tuple[int, int]:
        viewers, watchers, ever = self.open_views.get(lane_id, [0, 0, 0])
        return max(viewers, watchers), ever

    def driven(self, lane_id: str) -> bool:
        """An agent's commit-gate drive on the lane's tab (LaneManager.driven)."""
        from agentos.node.egress import guard_for

        page = self.pages.get(lane_id)
        guard = guard_for(page) if page is not None else None
        return guard is not None and guard.driving()

    async def quarantine(self, lane_id: str, reason: str, *, by: str = "node", **_: object) -> LaneInfo:
        self.calls.append(("quarantine", lane_id))
        info = self._require(lane_id)
        await self.hold(lane_id, reason=f"quarantined: {reason}", by=by)
        info.state, info.viewer, info.quarantine_reason = LaneState.QUARANTINED, None, reason
        self._event(lane_id, "quarantined", reason=reason, by=by)
        return info

    async def clear_quarantine(self, lane_id: str, *, by: str) -> LaneInfo:
        info = self._require(lane_id)
        if info.state is LaneState.QUARANTINED:
            info.state, info.quarantine_reason = LaneState.COLD, None
            self._event(lane_id, "quarantine_cleared", by=by)
        return info

    async def probe_exit_ip(self, lane_id: str) -> str | None:
        info = self._require(lane_id)
        return self.exit_ips.get(lane_id) if info.state in (LaneState.WARM, LaneState.ACTIVE) else None

    # ---- lifecycle ----

    async def delete(self, lane_id: str, *, by: str = "owner") -> Path | None:
        info = self._require(lane_id)
        if info.state in (LaneState.WARMING, LaneState.WARM, LaneState.ACTIVE):
            raise InvalidRequest(f"lane {lane_id} is {info.state.value}; close it first")
        del self.lanes[lane_id]
        self.deleted.append(lane_id)
        self._event(lane_id, "deleted", by=by)
        return None

    async def set_proxy(self, lane_id: str, label: str | None, *, confirm: bool = False,
                        by: str = "owner") -> LaneInfo:
        info = self._require(lane_id)
        if info.state in (LaneState.WARMING, LaneState.WARM, LaneState.ACTIVE):
            raise InvalidRequest(f"lane {lane_id} is {info.state.value}; close it first")
        self._event(lane_id, "proxy_changed", **{"from": info.spec.proxy_label, "to": label, "by": by})
        info.spec = info.spec.model_copy(update={"proxy_label": label})
        return info

    # ---- devices (AUD-02; agentos.node.devices has the real rules) ----

    def _device(self, name: str) -> DeviceSpec:
        if name not in self.device_specs:
            raise NotFound(f"no device named {name}")
        return self.device_specs[name]

    def device_info(self, name: str) -> dict:
        device = self._device(name)
        on = [i for i in self.lanes.values() if i.spec.device == name]
        pinned = any(i.spec.id in self.launched for i in on) or device.from_lane is not None
        return {"name": name, "proxy": device.proxy_label, "created_at": device.created_at.isoformat(),
                "from_lane": device.from_lane, "note": device.note, "pinned": pinned,
                "pin": {"os": "linux", "screen": "1920x1080", "window": "1920x1050", "gpu": None,
                        "timezone": "Europe/Vilnius", "locale": "en-US", "firefox": 158, "pinned_at": None,
                        "pinned_exit_ip": None, "pinned_country": "LT"} if pinned else None,
                "lanes": [{"id": i.spec.id, "state": i.state.value, "proxy": i.spec.proxy_label, "pin_copy": pinned}
                          for i in on],
                "workspaces": sorted({i.spec.workspace for i in on})}

    def devices(self) -> list[dict]:
        return [self.device_info(n) for n in sorted(self.device_specs)]

    async def create_device(self, name: str, *, proxy_label: str | None = None, from_lane: str | None = None,
                            note: str | None = None, confirm: bool = False, by: str = "owner") -> dict:
        if name in self.device_specs:
            raise InvalidRequest(f"device {name} already exists")
        if from_lane is not None:
            info = self._require(from_lane)
            if from_lane not in self.launched:
                raise InvalidRequest(f"{from_lane} has no pinned fingerprint yet")
            proxy_label = info.spec.proxy_label
        self.device_specs[name] = DeviceSpec(name=name, proxy_label=proxy_label, from_lane=from_lane, note=note)
        if from_lane is not None:
            info.spec = info.spec.model_copy(update={"device": name})
        return self.device_info(name)

    async def remove_device(self, name: str, *, by: str = "owner") -> None:
        self._device(name)
        if any(i.spec.device == name for i in self.lanes.values()):
            raise InvalidRequest(f"device {name} is used by lanes; move or delete them first")
        del self.device_specs[name]

    def plan_device_move(self, lane_id: str, device: str | None) -> dict:
        info = self._require(lane_id)
        target = self._device(device) if device else None
        proxy_to = target.proxy_label if target else info.spec.proxy_label
        plan = {"lane": lane_id, "from": info.spec.device, "to": device, "changes": [], "warnings": [],
                "confirm": False, "proxy": {"from": info.spec.proxy_label, "to": proxy_to}}
        if info.spec.device == device:
            plan["changes"].append(f"{lane_id} is already on {device or 'its own device'}; nothing changes")
        elif lane_id in self.launched:
            plan["warnings"].append(f"{lane_id} will launch with another fingerprint. To its site this is a new "
                                    "computer: expect a re-login, a security check or a checkpoint")
            plan["confirm"] = True
        else:
            plan["changes"].append(f"{lane_id} has never launched, so its site has not seen it")
        return plan

    async def set_device(self, lane_id: str, device: str | None, *, confirm: bool = False,
                         by: str = "owner") -> dict:
        plan = self.plan_device_move(lane_id, device)
        if plan["from"] == plan["to"]:
            return plan
        if plan["confirm"] and not confirm:
            raise ConfirmRequired(" ".join(plan["warnings"]) + ". Pass --yes to go ahead.", details=plan)
        info = self._require(lane_id)
        if info.state in (LaneState.WARMING, LaneState.WARM, LaneState.ACTIVE):
            raise InvalidRequest(f"lane {lane_id} is {info.state.value}; close it first")
        info.spec = info.spec.model_copy(update={"device": device, "proxy_label": plan["proxy"]["to"]})
        self._event(lane_id, "device_changed", **{"from": plan["from"], "to": device, "by": by})
        return {**plan, "done": True}

    # ---- resources ----

    def reserve_memory(self, key: str, mb: int) -> None:
        self.reservations[key] = mb

    def release_memory(self, key: str) -> None:
        self.reservations.pop(key, None)

    def memory_status(self) -> dict:
        return {"available_mb": None, "reserved_mb": sum(self.reservations.values()), "floor_mb": None,
                "disk_free_bytes": None, "disk_floor_bytes": None}

    def view(self, lane_id: str) -> ViewerInfo | None:
        return self._require(lane_id).viewer

    @asynccontextmanager
    async def _using(self, lane_id: str, principal_kind: str) -> AsyncIterator[FakePage]:
        self.calls.append(("using", lane_id))
        self.principals.append(principal_kind)
        self._maybe_fail()
        info = self._require(lane_id)
        self._gate(info, principal_kind)
        self._warm(info)
        info.state = LaneState.ACTIVE
        try:
            yield self._page_for(lane_id)
        finally:
            info.state = LaneState.WARM
            info.last_used = utcnow()

    def _page_for(self, lane_id: str) -> FakePage:
        if lane_id not in self.pages:
            self.pages[lane_id] = self.make_page(lane_id)
        return self.pages[lane_id]

    def make_page(self, lane_id: str) -> FakePage:
        return FakePage(lane_id)

    def using(self, lane_id: str, *, principal_kind: str = "agent"):
        return self._using(lane_id, principal_kind)

    # ---- claims: one holder per lane, no queue (the real queue is agentos.node.claims) ----

    async def claim(self, lane_id: str, holder: Any, *, purpose: str | None = None, ttl_s: float | None = None,
                    wait_s: float | None = None, principal_kind: str = "agent") -> dict:
        info = self._require(lane_id)
        self._check_control(info, principal_kind)
        claims = self.__dict__.setdefault("claims", {})
        held = claims.get(lane_id)
        if held is not None and held["key"] != holder.key:
            raise LaneBusy(f"lane {lane_id} is busy: {held['holder']} has claimed it")
        claims[lane_id] = {"key": holder.key, "holder": holder.label, "purpose": purpose or "", "ttl_s": ttl_s}
        return {"on": lane_id, "lease": f"lease_{lane_id}", "renewed": held is not None, "holder": holder.label,
                "purpose": purpose or "", "ttl_s": ttl_s, "queue_length": 0, "state": info.state.value}

    async def release(self, lane_id: str, holder: Any) -> dict:
        self._require(lane_id)
        claims = self.__dict__.setdefault("claims", {})
        held = claims.get(lane_id)
        released = held is not None and held["key"] == holder.key
        if released:
            del claims[lane_id]
        return {"on": lane_id, "released": released, **self.queue_info(lane_id, viewer=holder)}

    def queue_info(self, lane_id: str, *, viewer: Any = None) -> dict:
        self._require(lane_id)
        held = self.__dict__.get("claims", {}).get(lane_id)
        claim = None if held is None else {"holder": held["holder"], "purpose": held["purpose"],
                                           "yours": viewer is not None and held["key"] == viewer.key}
        return {"claim": claim, "queue_length": 0, "queue": [], "inside": [], "call_estimate_s": None}


class ControlFakeLaneManager(FakeLaneManager):
    """FakeLaneManager plus the human control actions: take over, hand back, stop, requests."""

    async def take_control(self, lane_id: str, *, by: str) -> LaneInfo:
        old = self._require(lane_id).control
        return self._set_control(lane_id, old.model_copy(update={
            "state": ControlState.HUMAN, "by": by, "since": utcnow(), "takeback_requested": False,
            "takeback_reason": None, "takeback_by": None}))

    async def hand_back(self, lane_id: str, *, by: str, note: str | None = None) -> LaneInfo:
        await self.clear_quarantine(lane_id, by=by)
        old = self._require(lane_id).control
        return self._set_control(lane_id, LaneControl(state=ControlState.AGENT, by=by, since=utcnow(),
                                                      last_human_input=old.last_human_input,
                                                      handback_note=(note or "").strip()[:300] or None))

    async def stop_agent(self, lane_id: str, *, by: str) -> LaneInfo:
        old = self._require(lane_id).control
        return self._set_control(lane_id, old.model_copy(update={
            "state": ControlState.PAUSED, "by": by, "since": utcnow(), "hold_until": None}))

    async def request_control(self, lane_id: str, *, by: str, reason: str) -> LaneInfo:
        old = self._require(lane_id).control
        if old.state is ControlState.AGENT:
            return self.lanes[lane_id]
        return self._set_control(lane_id, old.model_copy(update={
            "takeback_requested": True, "takeback_reason": reason, "takeback_by": by}))

    async def deny_request(self, lane_id: str, *, by: str) -> LaneInfo:
        old = self._require(lane_id).control
        return self._set_control(lane_id, old.model_copy(update={
            "takeback_requested": False, "takeback_reason": None, "takeback_by": None}))

    def note_human_input(self, lane_id: str) -> None:
        info = self.lanes.get(lane_id)
        if info is not None:
            info.control = info.control.model_copy(update={"last_human_input": utcnow()})

    async def wait_for_agent_control(self, lane_id: str, timeout: float) -> LaneInfo:
        return self._require(lane_id)


class LegacyLaneManager(FakeLaneManager):
    """A LaneManager from before the control contract: `using(lane_id)` only."""

    def using(self, lane_id: str):  # type: ignore[override]
        return self._using(lane_id, "legacy")


def _hold_policy(signal: Any, *, now: datetime) -> tuple[str, datetime | None] | None:
    """agentos.node.holds.hold_for in the shape FakeLaneManager.hold_policy takes."""
    hold = hold_for(signal, now=now)
    return None if hold is None else (hold.reason, hold.until)


class SignalReporter:
    """An Interaction that reports every signal its page reads see to the lane manager (navigate,
    act, signal, snapshot, wait, upload, and a screenshot's page), as the production watch does: a
    captcha holds the lane whoever read it. Everything else passes through."""

    def __init__(self, inner: Any, lanes: FakeLaneManager):
        self.inner = inner
        self._lanes = lanes

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def navigate(self, page: FakePage, url: str, **kwargs: Any) -> Any:
        result = await self.inner.navigate(page, url, **kwargs)
        await self._report(page, result.signal)
        return result

    async def act(self, page: FakePage, ref: str, action: ActionKind, text: str | None = None) -> Any:
        result = await self.inner.act(page, ref, action, text)
        await self._report(page, result.signal)
        return result

    async def signal(self, page: FakePage) -> Signal:
        value = await self.inner.signal(page)
        await self._report(page, value)
        return value

    async def snapshot(self, page: FakePage, **kwargs: Any) -> Any:
        result = await self.inner.snapshot(page, **kwargs)
        await self._report(page, getattr(result, "signal", None))
        return result

    async def wait(self, page: FakePage, *args: Any, **kwargs: Any) -> Any:
        result = await self.inner.wait(page, *args, **kwargs)
        await self._report(page, getattr(result, "signal", None))
        return result

    async def upload(self, page: FakePage, *args: Any, **kwargs: Any) -> Any:
        result = await self.inner.upload(page, *args, **kwargs)
        await self._report(page, getattr(result, "signal", None))
        return result

    async def screenshot(self, page: FakePage) -> bytes:
        png = await self.inner.screenshot(page)
        await self._report(page, await self.inner.signal(page))
        return png

    async def _report(self, page: FakePage, signal: Signal | None) -> None:
        if signal is None:
            return
        try:
            await self._lanes.report_signal(page.lane_id, signal, detail=page.url)
        except LaneNotFound:
            return


class DevLaneManager(ControlFakeLaneManager):
    """The laptop node's lane manager: the full LaneManager contract over memory. Each lane has a tab
    (`make_page`) and a display: a VNC server that shows the tab (`frame` gives its picture), started
    when the lane is created and closed when it goes. Bad signals hold the lane as production does."""

    def __init__(self, config: NodeConfig | None = None, *,
                 make_page: Callable[[str], FakePage] | None = None,
                 frame: Callable[[FakePage], tuple[int, Raster]] | None = None):
        super().__init__(config)
        self.hold_policy = _hold_policy
        self.servers: dict[str, VncServer] = {}
        self._make_tab = make_page
        self._frame = frame
        self._effect_listeners: list[Callable[[str, str, str | None, str], Any]] = []
        self._commit_listeners: list[Callable[[str], Any]] = []
        self._lane_access: dict[str, LaneAccess] = {}
        self.queue_timeout = float(self.config.queue_timeout)
        self.claim_max_total_s = float(self.config.claim_max_seconds)

    # ---- what the lanes' guards hear, as agentos.node.lanes (LaneManagerImpl) tells it ----

    def add_effect_listener(self, callback: Callable[[str, str, str | None, str], Any]) -> None:
        """callback(lane_id, kind, rule_id, actor) on each request an effect rule names that the fake
        site answered, whoever sent it: the action ledger counts it."""
        self._effect_listeners.append(callback)

    def add_commit_listener(self, callback: Callable[[str], Any]) -> None:
        """callback(lane_id) on each publish a person makes by hand while in control of the lane."""
        self._commit_listeners.append(callback)

    def _tell(self, listeners: list[Callable[..., Any]], lane_id: str, *args: Any) -> None:
        for callback in list(listeners):
            try:
                callback(lane_id, *args)
            except Exception:
                log.exception("%s: a lane listener failed", lane_id)

    def make_page(self, lane_id: str) -> FakePage:
        page = self._make_tab(lane_id) if self._make_tab is not None else super().make_page(lane_id)
        info = self.lanes.get(lane_id)
        label = info.spec.proxy_label if info is not None else None
        proxy = self.proxies_loader().get(label) if label else None
        if proxy is not None and getattr(proxy, "scheme", "http") == "socks5" and page.driver is not None:
            from agentos.dev.proxied import attach  # an environment's browser: pages come through its tunnel

            attach(page.driver, proxy.host, proxy.port)
        from agentos.node.egress import guard_for

        guard = guard_for(page)
        if guard is not None:  # a tab with a network (agentos.dev.net): its guard knows who has control
            guard.human_in_control = lambda: self._require(lane_id).control.state is ControlState.HUMAN
            guard.on_effect = lambda rule, actor: self._tell(self._effect_listeners, lane_id, rule.kind, rule.id,
                                                             actor)
            guard.on_human_commit = lambda: self._tell(self._commit_listeners, lane_id)

            def control_left(lid: str, old: LaneControl, new: LaneControl) -> None:
                if lid == lane_id and new.state is not ControlState.AGENT:  # as the node's manager does
                    guard.end_drive(f"control is {new.state.value}")

            self.listeners.append(control_left)
        return page

    # ---- shared lanes: the node's own queue and claims (agentos.node.claims), over memory ----

    def _access(self, lane_id: str) -> LaneAccess:
        return self._lane_access.setdefault(lane_id, LaneAccess())

    def _dispatch(self, lane_id: str) -> None:
        self._access(lane_id).dispatch(time.monotonic(), new_lease=lambda: "lease_" + secrets.token_hex(8),
                                       wall=utcnow)

    def _expire_claim(self, lane_id: str) -> None:
        """End a claim whose lease ran out or whose holder's grant ended, never while it is inside a call."""
        access = self._access(lane_id)
        claim = access.claim
        if claim is None or claim.implicit or access.holder_inside():
            return
        why = None
        if claim.holder.check is not None:
            try:
                why = claim.holder.check()
            except Exception:  # a broken check never ends a claim by itself
                log.warning("%s: the claim holder check failed", lane_id, exc_info=True)
        if why:
            self._end_claim(lane_id, "grant", why)
        elif time.monotonic() >= claim.expires:
            if claim.capped:
                self._end_claim(lane_id, "max_total", f"others were waiting and it was held for "
                                                      f"{self.claim_max_total_s:g} s, the most a claim may be renewed")
            else:
                self._end_claim(lane_id, "expired", f"no call from you for {claim.ttl_s:g} s, its ttl")

    def _end_claim(self, lane_id: str, reason: str, message: str, *, note: bool = True) -> None:
        access = self._access(lane_id)
        if access.claim is None or access.claim.implicit:
            return
        claim = access.end(reason, message, time.monotonic(), note=note)
        if claim is not None:
            self._event(lane_id, "claim_released" if not note else "claim_ended",
                        holder=claim.holder.label, reason=reason, detail=message)
            self._dispatch(lane_id)

    def _tell_lost(self, lane_id: str, holder: Holder) -> None:
        self._expire_claim(lane_id)
        note = self._access(lane_id).take_note(holder, time.monotonic())
        if note is not None:
            raise ClaimLost(f"your claim on {lane_id} ended ({note.message}), so this call did nothing. Someone "
                            "else may have changed the page: take a new snapshot, and claim the lane again with "
                            "browser_claim if the job still needs it to itself.")

    async def _enter(self, lane_id: str, holder: Holder, mode: str, principal_kind: str, *,
                     timeout: float | None = None, purpose: str | None = None, ttl_s: float = 0.0) -> Waiter:
        loop = asyncio.get_running_loop()
        access = self._access(lane_id)
        w = Waiter(holder=holder, mode=mode, since=time.monotonic(), granted=loop.create_future(),
                   purpose=clean_purpose(purpose), ttl_s=ttl_s)
        access.enqueue(w)
        started = loop.time()
        deadline = started + (self.queue_timeout if timeout is None else max(0.0, timeout))
        try:
            self._dispatch(lane_id)
            while not w.done:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise self._busy(lane_id, w, deadline - started)
                await asyncio.wait([w.granted], timeout=min(remaining, TICK_S))
                if w.done:
                    break
                self._expire_claim(lane_id)
                self._check_control(self._require(lane_id), principal_kind)  # a take-over while we wait
        except BaseException:
            access.leave(w, time.monotonic())
            self._dispatch(lane_id)
            raise
        access.renew(holder, time.monotonic(), self.claim_max_total_s)
        return w

    def _busy(self, lane_id: str, w: Waiter, waited: float) -> LaneBusy:
        access = self._access(lane_id)
        position, estimate = access.position(w), access.estimate(w, time.monotonic())
        claim = access.claim
        if claim is not None and access.kept_out(w):
            text = f"{claim.holder.label} has claimed it since {claim.since:%H:%M:%S} UTC"
            if claim.purpose:
                text += f" ({claim.purpose})"
        else:
            text = "other callers are ahead of you"
        e = LaneBusy(f"lane {lane_id} is busy: {text}. You were number {position} in its queue, about {estimate:g} s "
                     f"from your turn, after {waited:.0f} s of waiting. Call again later; browser_queue(on=\"{lane_id}\") "
                     "shows the queue.")
        e.details = {"lane": lane_id, "position": position, "estimate_s": estimate, "waited_s": round(waited, 1),
                     "queue_length": len(access.queue)}
        return e

    @staticmethod
    def _caller(holder: Holder | None, principal_kind: str) -> Holder:
        if principal_kind == "human":
            if holder is None:
                return Holder.anonymous("human")
            return Holder(key=holder.key, label=holder.label, kind="human", identity=holder.identity)
        return holder if holder is not None else Holder.anonymous(principal_kind)

    def using(self, lane_id: str, *, principal_kind: str = "agent", holder: Holder | None = None,
              mode: str = ACT, purpose: str | None = None):
        """As the node's LaneManager.using: reads share the lane, acts take turns, another caller's
        claim keeps acts waiting (then busy), and a claim that ended without its holder fails the
        holder's next call once with claim_lost."""
        return self._shared_using(lane_id, principal_kind, holder, mode, purpose)

    @asynccontextmanager
    async def _shared_using(self, lane_id: str, principal_kind: str, holder: Holder | None, mode: str,
                            purpose: str | None) -> AsyncIterator[FakePage]:
        if mode not in (READ, ACT, RUN):
            raise InvalidRequest(f"unknown lane use {mode!r}")
        self._gate(self._require(lane_id), principal_kind)
        holder = self._caller(holder, principal_kind)
        self._tell_lost(lane_id, holder)
        waiter = await self._enter(lane_id, holder, mode, principal_kind, purpose=purpose)
        try:
            async with self._using(lane_id, principal_kind) as page:
                yield page
        finally:
            access = self._access(lane_id)
            access.renew(holder, time.monotonic(), self.claim_max_total_s)  # every call renews, at its end too
            access.leave(waiter, time.monotonic())
            self._dispatch(lane_id)

    async def claim(self, lane_id: str, holder: Any, *, purpose: str | None = None, ttl_s: float | None = None,
                    wait_s: float | None = None, principal_kind: str = "agent") -> dict:
        info = self._require(lane_id)
        self._gate(info, principal_kind)
        if principal_kind == "human":
            raise InvalidRequest("a person does not claim a lane: taking control of it ends every claim")
        access = self._access(lane_id)
        self._expire_claim(lane_id)
        earlier = access.take_note(holder, time.monotonic())
        ttl, text = clamp_ttl(ttl_s), clean_purpose(purpose)
        renewed = access.holds(holder)
        if renewed:
            claim = access.claim
            assert claim is not None
            claim.ttl_s, claim.purpose = ttl, text or claim.purpose
        else:
            waiter = await self._enter(lane_id, holder, CLAIM, principal_kind,
                                       timeout=self.queue_timeout if wait_s is None else wait_s, purpose=text,
                                       ttl_s=ttl)
            access.leave(waiter, time.monotonic())  # a claim takes no place on the lane, only the lease
            self._dispatch(lane_id)
            if not access.holds(holder):
                raise LaneBusy(f"lane {lane_id}: the claim ended as soon as it was granted; call again")
            self._event(lane_id, "claimed", holder=holder.label, purpose=text, ttl_s=ttl)
            self._warm(info)  # a claim on a cold lane warms it, once
        access.renew(holder, time.monotonic(), self.claim_max_total_s)
        claim = access.claim
        assert claim is not None
        out = {"on": lane_id, "lease": claim.lease, "renewed": renewed,
               **claim.public(time.monotonic(), self.claim_max_total_s), "queue_length": len(access.queue),
               "state": info.state.value}
        if earlier is not None:
            out["ended_earlier"] = {"reason": earlier.reason, "detail": earlier.message}
        return out

    async def release(self, lane_id: str, holder: Any) -> dict:
        self._require(lane_id)
        access = self._access(lane_id)
        self._expire_claim(lane_id)
        note = access.take_note(holder, time.monotonic())
        released = access.holds(holder) and access.claim is not None and not access.claim.implicit
        if released:
            self._end_claim(lane_id, "released", "released by its holder", note=False)
        out: dict[str, Any] = {"on": lane_id, "released": released}
        if note is not None and not released:
            out["ended_earlier"] = {"reason": note.reason, "detail": note.message}
        elif not released:
            out["note"] = "you held no claim on this lane"
        out.update(self.queue_info(lane_id, viewer=holder))
        return out

    def claimed_by(self, lane_id: str, key: str) -> bool:
        if not key or lane_id not in self.lanes:
            return False
        self._expire_claim(lane_id)
        claim = self._access(lane_id).claim
        return claim is not None and not claim.implicit and claim.holder.key == key  # the key: it carries a nonce

    def queue_info(self, lane_id: str, *, viewer: Any = None) -> dict:
        self._require(lane_id)
        self._expire_claim(lane_id)
        return self._access(lane_id).snapshot(time.monotonic(), self.claim_max_total_s, viewer=viewer)

    def _set_control(self, lane_id: str, new: LaneControl) -> LaneInfo:
        info = super()._set_control(lane_id, new)
        if new.state is not ControlState.AGENT:  # a take-over, a pause or a hold overrides any claim
            self._end_claim(lane_id, "human" if new.state is ControlState.HUMAN else "paused",
                            "a human took control of the lane" if new.state is ControlState.HUMAN
                            else f"agents were held off the lane ({new.reason or 'stopped by a human'})")
        return info

    async def wait_for_agent_control(self, lane_id: str, timeout: float) -> LaneInfo:
        """Block until agents may act on the lane again (a hand back), or `timeout` passes."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout)
        while True:
            info = self._require(lane_id)
            if info.control.state is ControlState.AGENT or loop.time() >= deadline:
                return info
            await asyncio.sleep(min(CONTROL_POLL_S, max(0.0, deadline - loop.time())))

    def watch(self, interaction: Any) -> SignalReporter:
        watched = interaction if isinstance(interaction, SignalReporter) else SignalReporter(interaction, self)
        self._reader = watched.inner  # the hand-back reads the page through it (it reports nothing)
        return watched

    async def hand_back(self, lane_id: str, *, by: str, note: str | None = None) -> LaneInfo:
        """As production: a page that still shows a challenge holds the lane again at once."""
        reader = getattr(self, "_reader", None)
        page = self.pages.get(lane_id)
        hold = None
        if reader is not None and page is not None and self._require(lane_id).state is not LaneState.COLD:
            signal = Signal(getattr(value := await reader.signal(page), "value", value))  # read first
            hold = self.hold_policy(signal, now=utcnow()) if signal not in QUIET else None
        if not hold or hold[1] is not None:
            return await super().hand_back(lane_id, by=by, note=note)
        await self.clear_quarantine(lane_id, by=by)
        info = self._require(lane_id)
        self._event(lane_id, "held", reason=hold[0], until=None, by="node", after="hand_back")
        info.signal, info.signal_at = signal, utcnow()
        return self._set_control(lane_id, info.control.model_copy(update={
            "state": ControlState.PAUSED, "by": "node", "since": utcnow(), "reason": hold[0],
            "hold_until": None, "handback_note": None, "takeback_requested": False,
            "takeback_reason": None, "takeback_by": None}))

    def hints(self, lane_id: str) -> dict:
        """What the panel reads besides LaneInfo (agentos.api.services): the last signal a tool saw."""
        info = self._require(lane_id)
        return {"signal": info.signal.value if info.signal else "unknown", "logged_in_hint": "unknown"}

    async def create(self, spec: LaneSpec) -> LaneInfo:
        info = await super().create(spec)
        try:  # browser_file_put keeps the lane's inbox in it, as on a node
            self.config.lane_dir(spec.id).mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:  # a manager without a home of its own (a test's default config): no files here
            log.debug("%s: no lane folder under %s", spec.id, self.config.home)
        if self._frame is not None:
            lane_id = spec.id
            server = VncServer(lambda: self._frame(self._page_for(lane_id)), name=lane_id)
            await server.start()
            self.servers[lane_id] = server
        return info

    def _warm(self, info: LaneInfo) -> None:
        super()._warm(info)
        server = self.servers.get(info.spec.id)
        if server is not None and server.path is not None and info.viewer is not None:
            info.viewer = info.viewer.model_copy(update={"vnc_socket": server.path, "vnc_port": None})

    async def delete(self, lane_id: str, *, by: str = "owner") -> Path | None:
        trash = await super().delete(lane_id, by=by)
        shutil.rmtree(self.config.lane_dir(lane_id), ignore_errors=True)  # a lane made again starts empty
        self._lane_access.pop(lane_id, None)  # and with no claim of the lane that went
        server = self.servers.pop(lane_id, None)
        if server is not None:
            await server.close()
        return trash

    async def shutdown(self) -> None:
        await super().shutdown()
        for server in self.servers.values():
            await server.close()
        self.servers.clear()
