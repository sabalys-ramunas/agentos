"""The agenda's timeline: entries, keepalive visits the agenda manages, schedules projected ahead,
and past runs with their proof.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from openajar.agenda.rules import MANAGED, counts
from openajar.core.errors import (
    InvalidRequest,
    OpenajarError,
)
from openajar.core.models import RunStatus
from openajar.runner.names import is_connector_id
from openajar.runner.records import SUCCEEDED, TERMINAL, Run
from openajar.runner.util import clip

if TYPE_CHECKING:
    from openajar.agenda.service import AgendaService

log = logging.getLogger("openajar.agenda")


class TimelineMixin:
    """timeline() of AgendaService (openajar.agenda.service)."""

    def timeline(self, *, start: datetime, end: datetime, workspace: str | None = None, lane: str | None = None,
                 kind: str | None = None, states: list[str] | None = None, visible: Any = None,
                 limit: int = 500, managed: bool = True, drafter: str | None = None) -> dict[str, Any]:
        """One timeline for [start, end): one-off entries, the projected fires of recurring cron
        schedules (computed, never stored), the runs that already happened, with their proof, and
        (unless `managed` is False) what other services plan on their own (see _managed).
        `visible(lane or surface, workspace) -> bool` limits it to what the caller may see. Every item has `type` (entry,
        projected, run, managed), `at`, `workspace`, `lane`, `kind` (flow, connector, route; keepalive
        for a managed item) and `state`. `managed_by` says, per service, what it can and cannot show."""
        if end <= start:
            raise InvalidRequest("the range's end must be after its start")
        if end - start > timedelta(days=93):
            raise InvalidRequest("a timeline covers at most 93 days at a time")
        items: list[dict[str, Any]] = []
        for e in self.store.list(workspace=workspace, start=start, end=end, limit=limit):
            items.append(self.describe(e, drafter=drafter))
        items += self._projected(start, end, workspace)
        items += self._past_runs(start, end, workspace, limit)
        about: dict[str, Any] = {}
        if managed:
            got, about = self._managed(start, end, workspace)
            items += got

        def sees(target: str | None, ws: str | None) -> bool:
            return visible is None or visible(target, ws)

        for info in about.values():  # the lanes a service skipped or paused are the caller's to see only
            for key in ("skipped", "paused"):
                if key in info:
                    info[key] = [x for x in info[key] if sees(x.get("lane"), (x.get("lane") or "").split("/")[0])]

        def keep(it: dict[str, Any]) -> bool:
            if not sees(it.get("surface") or it.get("lane"), it.get("workspace")):
                return False
            if lane and it.get("lane") != lane and it.get("surface") != lane:
                return False
            if kind and it.get("kind") != kind:
                return False
            return not states or it.get("state") in states
        items = [it for it in items if keep(it)]
        items.sort(key=lambda it: (it.get("at") or it.get("created_at") or "", it.get("id") or ""))
        truncated = len(items) > limit
        return {"start": start.isoformat(), "end": end.isoformat(), "workspace": workspace,
                "items": items[:limit], "truncated": truncated,
                "counts": counts(items), "managed_by": about}

    def _managed(self, start: datetime, end: datetime, workspace: str | None) -> tuple[list[dict], dict[str, Any]]:
        """What keepalive plans on its own, from each service's `planned(start, end)`
        (built from its own planner, never planned again here). Read only: an item has no approval and
        cannot be cancelled here; `manage` says where it is managed. A service that is not running, or
        cannot say, adds no items and a note saying so; nothing is guessed."""
        items: list[dict[str, Any]] = []
        about: dict[str, Any] = {}
        for name, label in MANAGED.items():
            planned = getattr(self._svc(name), "planned", None)
            if not callable(planned):
                about[name] = {"available": False, "note": f"{label} is not running here, so its plans are not shown"}
                continue
            try:
                got = planned(start, end, workspace=workspace)
            except OpenajarError as exc:  # it says why (agentosd is not running, for the CLI)
                about[name] = {"available": False, "note": exc.message}
                continue
            except Exception as exc:  # noqa: BLE001 - another service's trouble never breaks the agenda
                log.warning("agenda: %s could not say what it plans: %s", name, exc)
                about[name] = {"available": False, "note": f"{label} could not say what it plans now"}
                continue
            about[name] = {"available": True, **{k: v for k, v in got.items() if k != "items"}}
            for it in got.get("items") or []:
                at = it.get("at")
                items.append({**it, "id": f"{name}:{it.get('lane')}@{at}", "type": "managed", "source": name,
                              "managed_by": name, "kind": name, "state": "planned",
                              "title": it.get("title") or label})
        return items, about

    def _projected(self, start: datetime, end: datetime, workspace: str | None) -> list[dict[str, Any]]:
        runner = self._svc("runner")
        sched = getattr(runner, "scheduler", None)
        if sched is None:
            return []
        from openajar.runner.models import ScheduleKind
        from openajar.runner.schedules import _zone, cron_slots

        now = self.clock.now()
        lo = max(start, now)
        out: list[dict[str, Any]] = []
        for s in sched.list("cron"):
            if not s.enabled or s.kind != ScheduleKind.CRON or s.cron is None:
                continue
            ws = _schedule_workspace(s, self)
            if workspace is not None and ws != workspace:
                continue
            try:
                slots = cron_slots(s.cron.cron, _zone(s.cron.timezone), lo, end, limit=3000)
            except OpenajarError:
                continue
            kind = "route" if s.route_id else ("connector" if s.flow_id and is_connector_id(s.flow_id) else "flow")
            name, routine = self._route_name(s.route_id) if s.route_id else (s.flow_id, None)
            for slot in slots[:200]:
                if slot >= end:
                    continue
                item = {"id": f"{s.id}@{slot.isoformat()}", "type": "projected", "schedule_id": s.id,
                        "at": slot.isoformat(), "workspace": ws, "lane": s.lane,
                        **({"surface": s.surface} if s.surface else {}), "kind": kind,
                        "target": s.flow_id or s.route_id, "state": "projected", "cron": s.cron.cron,
                        "timezone": s.cron.timezone, "title": f"{name or s.route_id} ({' '.join(s.cron.cron.split())})",
                        **({"workflow": routine} if routine else {})}
                paced = sched.paced_slot(s, slot) if s.flow_id else None
                if paced is not None:
                    item["paced"] = paced
                out.append(item)
        return out

    def _route_name(self, route_id: str) -> tuple[str | None, str | None]:
        """(the name a person gave it, the workflow it starts) of a route schedule, so the agenda shows
        "Tester A tick routine", not rt_f599109119d5 (QA 2026-11-10 A-8)."""
        try:
            route = self._events().get_route(route_id)
        except OpenajarError:
            return None, None
        target = getattr(route, "target", None)
        return route.name, getattr(target, "workflow", None)

    def _past_runs(self, start: datetime, end: datetime, workspace: str | None, limit: int) -> list[dict[str, Any]]:
        runner = self._svc("runner")
        if runner is None:
            return []
        out = []
        for run in runner.store.list_runs(workspace=workspace, limit=max(limit, 200)):
            at = run.started_at or run.created_at
            if not start <= at < end or (run.trigger or {}).get("kind") == "agenda":
                continue  # an entry's own run is shown as the entry
            state = ("done" if run.status in SUCCEEDED else "running" if run.status not in TERMINAL
                     else "cancelled" if run.status == RunStatus.CANCELLED else "failed")
            out.append({"id": run.id, "type": "run", "run_id": run.id, "schedule_id": run.schedule_id,
                        "at": at.isoformat(), "workspace": run.workspace, "lane": run.lane,
                        **({"surface": run.surface} if run.surface else {}),
                        "kind": "connector" if run.surface else "flow", "target": run.flow_id,
                        "state": state, "status": str(run.status), "title": run.flow_id or str(run.kind),
                        "source": run.source, "proof": run_proof(run, runner) if run.terminal else None})
        return out


def run_proof(run: Run, runner: Any) -> dict[str, Any]:
    """What a finished run left as proof: its status and summary, readbacks of the target's own
    state, the final URL and screenshot ref, whether a connector returned an output. Only what the
    run recorded; an empty field means the run left none."""
    p = run.proof or {}
    rbs = [{"passed": rb.get("passed"), "expected": rb.get("expected"), "found": rb.get("found")}
           for rb in (p.get("readbacks") or []) if isinstance(rb, dict)]
    out: dict[str, Any] = {"run_id": run.id, "status": str(run.status), "summary": runner._summary(run),
                           "readbacks": clip(rbs, max_str=200, max_items=20), "url": p.get("url"),
                           "screenshot": p.get("screenshot"),
                           "finished_at": run.finished_at.isoformat() if run.finished_at else None}
    if run.output is not None:
        out["output"] = "the connector returned a typed result; read it with run_status"
    if not rbs and not p.get("url") and run.output is None:
        out["note"] = "the run recorded no readback, page or output; its status is the only proof"
    return out


def _schedule_workspace(s: Any, svc: AgendaService) -> str | None:
    target = s.surface or s.lane
    if target:
        return target.split("/", 1)[0]
    if s.route_id:
        try:
            return svc._events().get_route(s.route_id).workspace
        except OpenajarError:
            return None
    return None
