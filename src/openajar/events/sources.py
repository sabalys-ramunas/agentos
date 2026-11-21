"""Event sources that live in other packages, wired when the events service starts.

  run.finished      the runner's on_finished hook: every terminal run on a lane
  lane.held         the lane control listener: a hold (a checkpoint, a rate limit) starts
  lane.released     the same listener: the lane went back to agents; waiting deliveries resume
  approval.decided  policy approvals: approved, denied or expired. Brief B3's
                    `approvals.on_decision(callback)` when the node has it, else the panel's
                    event bus ("events", kind "approval")

Cron, page watches and webhooks emit from the runner's Scheduler (Schedule.route_id), and
owner messages from EventService.owner_message. Every source writes node facts only; the
only content here is none (these are all node-observed).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from openajar.core.models import parse_lane_id

log = logging.getLogger("openajar.events")

DECIDED = {"approved", "denied", "expired"}


def _state(c: Any) -> str | None:
    st = getattr(c, "state", None)
    return str(getattr(st, "value", st)) if st is not None else None


def _held(c: Any) -> bool:
    return _state(c) == "paused" and bool(getattr(c, "reason", None))


class Sources:
    def __init__(self, service: Any):
        self.service = service
        self._tasks: list[asyncio.Task] = []
        self._lane_listener = False
        self._run_hook = False

    def _svc(self, name: str) -> Any:
        return self.service._svc(name)

    def install(self) -> None:
        runner = self._svc("runner")
        if runner is not None and isinstance(getattr(runner, "on_finished", None), list):
            runner.on_finished.append(self.on_run_finished)
            self._run_hook = True
        lanes = self._svc("lanes")
        if callable(getattr(lanes, "add_control_listener", None)):
            lanes.add_control_listener(self.on_lane_control)
            self._lane_listener = True
        approvals = self._svc("approvals")
        if callable(getattr(approvals, "on_decision", None)):
            approvals.on_decision(self.on_approval)
        else:
            bus = self._svc("events")
            if callable(getattr(bus, "subscribe", None)):
                self._tasks.append(asyncio.create_task(self._watch_bus(bus), name="events-approvals"))

    async def uninstall(self) -> None:
        runner = self._svc("runner")
        if self._run_hook and runner is not None and self.on_run_finished in runner.on_finished:
            runner.on_finished.remove(self.on_run_finished)
        lanes = self._svc("lanes")
        if self._lane_listener and callable(getattr(lanes, "remove_control_listener", None)):
            lanes.remove_control_listener(self.on_lane_control)
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks = []

    # ------------------------------------------------------------ runs

    def on_run_finished(self, run: Any) -> None:
        """Runner hook: every terminal run on a lane is a run.finished event. A run a
        delivery started carries that delivery's cause one level deeper."""
        try:
            self.service.wake()  # a flow delivery may be done
            if not run.lane:
                return
            ws, _ = parse_lane_id(run.lane)
            trig = run.trigger or {}
            cause: dict[str, Any] = {"depth": 0}
            if trig.get("kind") == "event":
                events = trig.get("events") or []
                cause = {"event": events[0] if events else None, "route": trig.get("route"), "lane": run.lane,
                         "depth": int(trig.get("depth", 0)) + 1}
            facts = {"run_id": run.id, "status": str(getattr(run.status, "value", run.status)),
                     "flow": run.flow_id, "schedule_id": run.schedule_id, "source": run.source}
            self.service.emit({"source": "run", "kind": "run.finished", "workspace": ws, "lane": run.lane,
                               "dedupe_key": f"run:{run.id}:{facts['status']}",
                               "facts": {k: v for k, v in facts.items() if v is not None}, "cause": cause})
        except Exception:
            log.exception("run.finished could not be recorded for %s", getattr(run, "id", "?"))

    # ------------------------------------------------------------ lane holds

    def on_lane_control(self, lane_id: str, old: Any, new: Any) -> None:
        """Lane control listener: a hold starting is lane.held, the lane going back to agents
        after a hold or a human is lane.released (and wakes deliveries waiting on it)."""
        try:
            ws, _ = parse_lane_id(lane_id)
        except ValueError:
            return
        now = self.service.clock.now()
        since = getattr(new, "since", None) or now
        try:
            if _held(new) and not (_held(old) and getattr(old, "reason", None) == getattr(new, "reason", None)):
                facts = {"reason": str(new.reason)[:200], "by": str(getattr(new, "by", None) or "node")}
                until = getattr(new, "hold_until", None)
                if until is not None:
                    facts["until"] = until.isoformat()
                self.service.emit({"source": "lane", "kind": "lane.held", "workspace": ws, "lane": lane_id,
                                   "dedupe_key": f"lane:{lane_id}:held:{since.isoformat()}", "facts": facts})
            elif _state(new) == "agent" and _state(old) in ("paused", "human"):
                self.service.dispatcher.wake_lane(lane_id, now)
                facts = {"by": str(getattr(new, "by", None) or "owner"), "was": _state(old)}
                if getattr(old, "reason", None):
                    facts["reason"] = str(old.reason)[:200]
                self.service.emit({"source": "lane", "kind": "lane.released", "workspace": ws, "lane": lane_id,
                                   "dedupe_key": f"lane:{lane_id}:released:{since.isoformat()}", "facts": facts})
        except Exception:
            log.exception("lane event could not be recorded for %s", lane_id)

    # ------------------------------------------------------------ approvals

    def on_approval(self, ap: Any) -> None:
        """approval.decided for an approval that names a lane (that is how it has a workspace).
        Trigger proposals are picked up by EventService.check_proposals on the next tick."""
        data = ap.public() if hasattr(ap, "public") else dict(ap or {})
        state = data.get("state")
        self.service.wake()
        if state not in DECIDED or not data.get("id"):
            return
        lane = data.get("lane")
        try:
            ws, _ = parse_lane_id(lane) if lane else (None, None)
        except ValueError:
            return
        if ws is None:
            return
        facts = {"approval_id": data["id"], "state": state, "tool": data.get("tool"), "effect": data.get("effect"),
                 "decided_by": data.get("decided_by")}
        try:
            self.service.emit({"source": "approval", "kind": "approval.decided", "workspace": ws, "lane": lane,
                               "dedupe_key": f"approval:{data['id']}:{state}",
                               "facts": {k: v for k, v in facts.items() if v is not None}})
        except Exception:
            log.exception("approval.decided could not be recorded for %s", data.get("id"))

    async def _watch_bus(self, bus: Any) -> None:
        while True:
            try:
                async with bus.subscribe() as sub:
                    while True:
                        ev = await sub.get()
                        if getattr(ev, "kind", None) == "approval" and isinstance(ev.data, dict) and "id" in ev.data:
                            self.on_approval(ev.data)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("the approvals feed failed; reconnecting")
                await asyncio.sleep(5)
