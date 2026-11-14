"""What the viewer and the control API say about a lane: the control hub that wakes event
streams on a change, and the lane, control and action dicts they send.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from typing import Any

from agentos.core.models import ControlState, LaneControl, LaneInfo
from agentos.node.viewer_tokens import iso

log = logging.getLogger("agentos.viewer")


RECENT_ACTIONS = 40


# ---- control hub ------------------------------------------------------------------------

class ControlHub:
    """One control listener on the lane manager, fanned out to every open relay and event
    stream, with a per-lane cache so the relay never reads the store per input event."""

    def __init__(self, lanes: Any):
        self.lanes = lanes
        self._cache: dict[str, LaneControl] = {}
        self._watchers: dict[str, list[Callable[[LaneControl], None]]] = {}
        self._notice_watchers: dict[str, list[Callable[[dict], None]]] = {}
        lanes.add_control_listener(self._changed)
        add_notices = getattr(lanes, "add_notice_listener", None)  # a manager without an egress guard has none
        if callable(add_notices):
            add_notices(self._noticed)

    def _noticed(self, lane_id: str, notice: dict) -> None:
        for watcher in list(self._notice_watchers.get(lane_id, ())):
            try:
                watcher(notice)
            except Exception:
                log.exception("notice watcher failed")

    def watch_notices(self, lane_id: str, callback: Callable[[dict], None]) -> Callable[[], None]:
        return _watch(self._notice_watchers, lane_id, callback)

    def _changed(self, lane_id: str, old: LaneControl, new: LaneControl) -> None:
        self._cache[lane_id] = new
        for watcher in list(self._watchers.get(lane_id, ())):
            try:
                watcher(new)
            except Exception:
                log.exception("control watcher failed")

    def control(self, lane_id: str) -> LaneControl:
        control = self._cache.get(lane_id)
        if control is None:
            control = self._cache[lane_id] = self.lanes.get(lane_id).control
        return control

    def state(self, lane_id: str) -> ControlState:
        """Fail closed: a lane that cannot be read counts as paused."""
        try:
            return self.control(lane_id).state
        except Exception:  # noqa: BLE001
            return ControlState.PAUSED

    def watch(self, lane_id: str, callback: Callable[[LaneControl], None]) -> Callable[[], None]:
        return _watch(self._watchers, lane_id, callback)


def _watch(table: dict[str, list[Callable]], lane_id: str, callback: Callable) -> Callable[[], None]:
    table.setdefault(lane_id, []).append(callback)

    def unwatch() -> None:
        watchers = table.get(lane_id, [])
        if callback in watchers:
            watchers.remove(callback)
        if not watchers:
            table.pop(lane_id, None)

    return unwatch


def control_dict(control: LaneControl) -> dict[str, Any]:
    data = control.model_dump(mode="json")
    for key in ("since", "last_human_input"):
        data[key] = iso(getattr(control, key))
    return data


def lane_dict(info: LaneInfo) -> dict[str, Any]:
    """The Lane shape from docs/API.md, as far as the lane manager knows it."""
    spec = info.spec
    out: dict[str, Any] = {
        "id": spec.id,
        "workspace": spec.workspace,
        "name": spec.name,
        "kind": spec.kind.value,
        "engine": spec.engine,
        "state": info.state.value,
        "proxy": {"label": spec.proxy_label} if spec.proxy_label else None,
        "exit_ip": info.exit_ip,
        "last_used": iso(info.last_used),
        "quarantine_reason": info.quarantine_reason,
        "live": info.viewer is not None,
        "control": control_dict(info.control),
    }
    signal = getattr(info, "signal", None)
    if signal is not None:
        out["signal"] = getattr(signal, "value", signal)
    return out


def action_dict(event: Any) -> dict[str, Any] | None:
    """A trace event (agentos.policy TraceEvent or a dict) as the page shows it."""
    if hasattr(event, "public"):
        event = event.public()
    elif hasattr(event, "model_dump"):
        event = event.model_dump(mode="json")
    if not isinstance(event, dict):
        return None
    at = event.get("at")
    if isinstance(at, (int, float)):
        at = iso(at)
    return {
        "id": event.get("id"),
        "at": at,
        "tool": event.get("tool") or event.get("kind"),
        "status": event.get("status"),
        "summary": event.get("summary") or event.get("error"),
        "principal": event.get("principal"),
        "principal_kind": event.get("principal_kind"),
        "effect": event.get("effect"),
    }


async def recent_actions(traces: Any, lane_id: str, limit: int = RECENT_ACTIONS) -> list[dict] | None:
    """Newest first, or None when this node has no traces provider."""
    if traces is None:
        return None
    query = getattr(traces, "query", None) or getattr(traces, "recent", None)
    if query is None:
        return None
    try:
        result = query(lane=lane_id, limit=limit)
        if inspect.isawaitable(result):
            result = await result
    except Exception:
        log.warning("could not read recent actions for %s", lane_id, exc_info=True)
        return []
    events = result[0] if isinstance(result, tuple) else result
    return [a for a in (action_dict(e) for e in events or ()) if a is not None]
