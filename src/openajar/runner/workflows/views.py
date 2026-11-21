"""What people read about a workflow (a routine): its public card, the list and the day, its runs,
and one run laid out step by step with what each step did.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time, timedelta
from fnmatch import fnmatchcase
from typing import Any

from openajar.core.errors import NotFound
from openajar.core.models import Effect, RunKind, RunStatus
from openajar.runner.records import DEFAULT_TIMEZONE, StepStatus
from openajar.runner.workflows.check import (
    Operation,
    check,
    counted_steps,
    operations_in,
)
from openajar.runner.workflows.engine import shown_value
from openajar.runner.workflows.mirror import mirror
from openajar.runner.workflows.models import (
    BranchStep,
    LoopStep,
    WorkflowState,
    strongest,
)

log = logging.getLogger("openajar.runner")


class ViewsMixin:
    """The read views of WorkflowService (openajar.runner.workflows.service)."""

    def _ask_first_view(self, flow_id: str) -> dict[str, Any]:
        switches = self._switches()
        fn = getattr(switches, "flow_view", None)
        return fn(flow_id) if callable(fn) else {"ask_first": False, "ask_first_by": None}

    @staticmethod
    def _sites(ops: list[tuple[Any, Operation]]) -> list[str]:
        out: list[str] = []
        for _, op in ops:
            site = (op.on or op.lane).split("/", 1)[-1]
            if site not in out:
                out.append(site)
        return out

    def _next_at(self, state: WorkflowState) -> str | None:
        scheduler = self.runner.scheduler
        if not state.enabled or not state.schedule_id or scheduler is None:
            return None
        s = scheduler.store.get_schedule(state.schedule_id)
        if s is not None and s.every:
            return s.state.get("next_check_at")
        return scheduler.next_slot(s).isoformat() if s is not None and s.cron else None

    def public(self, flow_id: str) -> dict[str, Any]:
        """A workflow as a `Flow` row (docs/WORKFLOWS.md, "What a saved workflow shows")."""
        state = self._state(flow_id)
        wv = self.get_version(flow_id)
        d = wv.definition
        ops = operations_in(d, self.operation)
        _, missing = check(d, self)
        effects = [op.effect for _, op in ops]
        watched = self.trigger_operation(d)
        data = d.canonical()
        trigger = dict(data["trigger"])
        if d.trigger.kind == "webhook":
            token = self._hook_token(state)
            trigger["hook"] = {"token": token, "secret_hint": state.secret_hint,
                               "last_received_at": state.last_received_at.isoformat() if state.last_received_at else None}
        always = [{"step": s.id, "because": op.effect.value} for s, op in ops if op.effect in (Effect.PAY, Effect.IRREVERSIBLE)]
        always += [{"step": s["id"], "because": "ask_step"} for s in self._flat(data["steps"]) if s["kind"] == "ask"]
        limits = [self.sends.limit_view(s.id, s.send.list if s.send else None, op.on or op.lane) for s, op in ops if op.sends]
        limits += [{**self.sends.limit_view(s.id, None, op.on or op.lane), "unsubscribe": False, "counted": True}
                   for s, op in counted_steps(d, self)]
        recent = self.runner.store.list_runs(flow_id=flow_id, limit=20)
        return {
            "id": flow_id, "schedule_id": state.schedule_id, "name": d.name, "workspace": d.workspace,
            "lane": f"{d.workspace or 'draft'}/*",
            "version": wv.version, "revision": wv.revision, "live_version": state.live,
            "needs_review": bool(state.enabled and state.live and state.live != state.head),
            "enabled": state.enabled, "next_at": self._next_at(state), "effect": strongest(effects).value,
            "trigger": trigger, "steps": data["steps"], "sites": self._sites([*ops, *([(None, watched)] if watched else [])]),
            "missing": [{"target": m.target, "words": m.words} for m in missing],
            "always_asks_steps": always, "send_limits": limits, "params": {},
            "steps_supported": True, **self._ask_first_view(flow_id),
            **self._lately(recent), **self._watch(state),
        }

    def _watch(self, state: WorkflowState) -> dict[str, Any]:
        """How a site_change trigger stands, for the version that runs, not one an agent saved over it."""
        wv = self.get_version(state.id, state.live or None)
        return {"watch": self.site_changes.view(state, wv)} if wv.definition.trigger.kind == "site_change" else {}

    def _hook_token(self, state: WorkflowState) -> str | None:
        scheduler = self.runner.scheduler
        s = scheduler.store.get_schedule(state.schedule_id) if scheduler is not None and state.schedule_id else None
        return s.webhook.token if s is not None and s.webhook else None

    @staticmethod
    def _flat(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for s in steps:
            out.append(s)
            for arm in ("then", "otherwise", "body"):
                out += ViewsMixin._flat(s.get(arm) or [])
        return out

    def _lately(self, runs: list[Any]) -> dict[str, Any]:
        if not runs:
            return {"last_run": None, "recent_runs": [], "latest": None, "stuck": None, "waiting": None}
        newest = runs[0]
        at = (newest.started_at or newest.created_at).isoformat()
        view = mirror(self.run_view(newest))  # a row reaches the controller, and any agent, so no run's content is on it
        out: dict[str, Any] = {
            "last_run": {"id": newest.id, "status": view["status"], "started_at": at, "cost": newest.cost},
            "recent_runs": [{"id": r.id, "status": str(r.status), "at": (r.started_at or r.created_at).isoformat()}
                            for r in runs], "latest": None, "stuck": None, "waiting": None}
        done = next((r for r in runs if r.terminal), None)
        if done is not None:
            out["latest"] = {"at": (done.finished_at or done.created_at).isoformat(), "status": str(done.status),
                             "result": mirror(self.run_view(done))["result"]}
        if newest.status in (RunStatus.FAILED, RunStatus.PARTIAL):
            failed = next((s for s in view["steps"] if s["status"] in ("failed", "partial")), None)
            if failed is not None:
                out["stuck"] = {"step": failed["step"], "label": failed["label"], "words": failed.get("note") or "It stopped here.",
                                "run": newest.id}
        waiting = next((s for s in view["steps"] if s["status"] == "waiting" and s.get("approval_id")), None)
        if waiting is not None:
            out["waiting"] = {"run": newest.id, "words": waiting["label"], "approval_id": waiting["approval_id"]}
        return out

    def list(self, *, allowed: Any = None) -> list[dict[str, Any]]:
        out = []
        for state in self.store.states():
            try:
                row = self.public(state.id)
            except Exception:
                log.exception("workflow %s could not be read", state.id)
                continue
            if allowed is None or allowed(row["workspace"] or ""):
                out.append(row)
        return out

    def day(self, *, allowed: Any = None, now: datetime | None = None) -> dict[str, Any]:
        """`RoutineDay`: what ran over the last day, what ran since this node's midnight, and the
        cron slots due in the next day. Counted from the runs and the cron, never guessed."""
        from openajar.runner.schedules import _zone, cron_slots

        now = now or self.clock.now()
        midnight = datetime.combine(now.astimezone(_zone(self._timezone() or DEFAULT_TIMEZONE)).date(), time(0),
                                    tzinfo=_zone(self._timezone() or DEFAULT_TIMEZONE)).astimezone(UTC)
        rows = {row["id"]: row for row in self.list(allowed=allowed)}

        def site(row: dict[str, Any]) -> str:
            return row["sites"][-1] if row["sites"] else ""

        marks: list[dict[str, Any]] = []
        ran = checked = 0
        for run in self.runner.store.list_runs(kind=RunKind.WORKFLOW, limit=2000):  # newest first
            started = run.started_at or run.created_at
            if started < now - timedelta(days=1):
                break
            row = rows.get(run.flow_id or "")
            if row is None:
                continue
            marks.append({"flow": row["id"], "at": started.isoformat(), "site": site(row), "status": str(run.status)})
            if started >= midnight:
                ran += 1
                checked += run.status == RunStatus.SUCCEEDED_VERIFIED
        for row in rows.values():
            trigger = row["trigger"]
            if not row["enabled"] or trigger.get("kind") != "schedule" or not trigger.get("cron"):
                continue
            zone = _zone(trigger.get("timezone") or DEFAULT_TIMEZONE)
            for at in cron_slots(trigger["cron"], zone, now, now + timedelta(days=1)):
                marks.append({"flow": row["id"], "at": at.isoformat(), "site": site(row), "status": None})
        marks.sort(key=lambda mark: mark["at"])
        return {"marks": marks, "ran_today": ran, "checked_today": checked}

    def runs(self, flow_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        self._state(flow_id)
        return [self.run_view(r) for r in self.runner.store.list_runs(flow_id=flow_id, limit=limit)]

    def get_run_view(self, flow_id: str, run_id: str) -> dict[str, Any]:
        run = self.runner.get(run_id)
        if run.kind != RunKind.WORKFLOW or run.flow_id != flow_id:
            raise NotFound(f"{run_id} is not a run of {flow_id}")
        return self.run_view(run)

    def _layout(self, steps: list[Any], prefix: str, pattern: str, taken: bool, out: list[tuple[Any, str, str, bool]],
                records: dict[str, Any], states: dict[str, Any]) -> None:
        """Each step once, in order, with the path pattern its records have (a loop's `*` is its item)."""
        for step in steps:
            path, pat = f"{prefix}{step.id}", f"{pattern}{step.id}"
            out.append((step, path, pat, taken))
            if isinstance(step, BranchStep):
                took = (states.get(path) or {}).get("out", {}).get("took")
                for arm, items in (("then", step.then), ("otherwise", step.otherwise)):
                    self._layout(items, f"{path}/{arm}/", f"{pat}/{arm}/", taken and took == arm, out, records, states)
            elif isinstance(step, LoopStep):
                self._layout(step.body, f"{path}/*/", f"{pat}/*/", taken, out, records, states)

    def _step_view(self, step: Any, pat: str, taken: bool, run: Any, records: dict[str, Any], states: dict[str, Any]) -> dict[str, Any]:
        found = [r for path, r in records.items() if fnmatchcase(path, pat) and path.count("/") == pat.count("/")]
        found.sort(key=lambda r: r.idx)
        views = [self._record_status(r, run, states.get(r.path) or {}) for r in found]
        last = found[-1] if found else None
        result = (last.result or {}) if last is not None else {}
        state = (states.get(last.path) or {}) if last is not None else {}
        status, approval, note = _shown_status(views, taken, run)
        view: dict[str, Any] = {
            "step": step.id, "label": result.get("label") or step.label, "kind": step.kind, "status": status,
            "short": result.get("short"), "started_at": last.started_at.isoformat() if last and last.started_at else None,
            "finished_at": last.finished_at.isoformat() if last and last.finished_at else None,
            "input": result.get("input") or state.get("input"), "output": result.get("output"),
            "note": note or result.get("note"), "attempts": max((r.attempts for r in found), default=0),
            "approval_id": approval, "run_id": result.get("run_id") or state.get("child")}
        _add_marks(view, status, result, state, (last.error or {}) if last is not None else {})
        if len(found) > 1 or "/*/" in pat:
            view["items"] = self._items(pat, found, views, records)
        return view

    @staticmethod
    def _row_of(path: str, pat: str) -> tuple[int, ...]:
        """Which row of each loop around it a path is, from where the pattern has a `*`."""
        return tuple(int(p) for p, m in zip(path.split("/"), pat.split("/"), strict=False) if m == "*")

    def _items(self, pat: str, found: list[Any], views: list[tuple[str, str | None, str | None]], records: dict[str, Any]) -> list[dict[str, Any]]:
        """One item for each row, numbered as the rows are. A row that stopped at a stop_if_replied
        has no record of this step, and says why, so a gap in the record is never a mystery."""
        def label(row: tuple[int, ...]) -> str:
            return ".".join(str(i + 1) for i in row)

        items: dict[tuple[int, ...], dict[str, Any]] = {}
        for r, (status, _, note) in zip(found, views, strict=True):
            row = self._row_of(r.path, pat)
            items[row] = {"label": label(row), "status": status, **({"note": note} if status == "stopped" and note else {})}
        parent = pat.rpartition("/")[0]
        for path, r in records.items():
            home = path.rpartition("/")[0]
            if (r.result or {}).get("stopped") and fnmatchcase(home, parent) and home.count("/") == parent.count("/"):
                row = self._row_of(path, pat)
                items.setdefault(row, {"label": label(row), "status": "skipped", "note": (r.result or {}).get("short")})
        return [items[k] for k in sorted(items)]

    def _record_status(self, r: Any, run: Any, state: dict[str, Any]) -> tuple[str, str | None, str | None]:
        result = r.result or {}
        if r.status == StepStatus.DONE and result.get("stopped"):
            return "stopped", None, result.get("short")
        if r.status == StepStatus.DONE:
            verified = result.get("verified") is True
            return (RunStatus.SUCCEEDED_VERIFIED.value if verified else RunStatus.SUCCEEDED_UNVERIFIED.value), None, None
        if r.status == StepStatus.FAILED:
            words = (r.error or {}).get("message")
            return ("partial" if result.get("acted") else "failed"), None, words
        if r.status == StepStatus.STARTED:
            if run.terminal:
                return "failed", None, (run.error or {}).get("message")
            if r.type in ("loop", "branch"):
                return "running", None, None
            child = self.runner.store.get_run(state["child"]) if state.get("child") else None
            approval = (child.approval_id if child is not None and child.status == RunStatus.WAITING_APPROVAL else None) \
                or (state.get("approval_id") if not state.get("authorized") else None)
            if approval or r.type == "wait" or (run.error or {}).get("control") == "workflow":
                return "waiting", approval, (run.error or {}).get("message")
            return "running", None, None
        return "skipped", None, None

    def run_view(self, run: Any) -> dict[str, Any]:
        """`RoutineRun`: every step in order, what it had and gave, and an honest status."""
        store = self.runner.store
        wv = self.store.version(run.flow_id or "", run.flow_version or "")
        records = {r.path: r for r in store.steps(run.id)}
        states = store.step_states(run.id)
        params = run.params or {}
        layout: list[tuple[Any, str, str, bool]] = []
        steps: list[dict[str, Any]] = []
        if wv is not None:
            self._layout(wv.definition.steps, "", "", True, layout, records, states)
            steps = [self._step_view(step, pat, taken, run, records, states) for step, _, pat, taken in layout]
        started = (run.started_at or run.created_at).isoformat()
        trigger = self._trigger_view(wv, params, started)
        status = str(run.status)
        if not run.terminal and any(s["status"] == "waiting" and s.get("approval_id") for s in steps):
            status = RunStatus.WAITING_APPROVAL.value
        elif run.status == RunStatus.QUEUED and run.started_at is not None:
            status = RunStatus.RUNNING.value
        result = (run.output or {}).get("result") if run.terminal else None
        quoted = False
        if run.terminal and result is None:
            result = (run.error or {}).get("message")
            quoted = not (run.error or {}).get("own_words")
        return {"id": run.id, "flow": run.flow_id, "version": run.flow_version, "revision": wv.revision if wv else None,
                "status": status, "cause": params.get("cause") or "It started", "result": result,
                **({"result_outside": True} if quoted else {}), "by": params.get("by") or "trigger", "started_at": started,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                "output": (run.output or {}).get("output"), "steps": [trigger, *steps], "cost": run.cost}

    def _trigger_view(self, wv: Any, params: dict[str, Any], started: str) -> dict[str, Any]:
        """The run's first row: what started it, and what came in with it."""
        trigger = {"step": "trigger", "label": (wv.definition.trigger.label if wv else None) or params.get("cause") or "It started",
                   "kind": "trigger", "status": RunStatus.SUCCEEDED_UNVERIFIED.value, "started_at": started, "finished_at": started,
                   "input": None, "output": shown_value(params.get("trigger") or {}, self.flows.vault_scrub), "attempts": 1,
                   "note": None, "short": None, "approval_id": None, "run_id": None}
        if params.get("untrusted"):
            trigger["untrusted"] = params["untrusted"]
            trigger["arrived"] = (params.get("origin") or {}).get("kind")
        return trigger

    def result_of(self, run: Any, records: list[Any]) -> dict[str, Any]:
        """What came of it, in a few words, from its last action."""
        done = [r for r in records if r.type == "action" and r.status == StepStatus.DONE]
        last = done[-1].result if done else None
        stopped = [r for r in records if r.type == "stop_if_replied" and "/" not in r.path and (r.result or {}).get("stopped")]
        words = (stopped[-1].result if stopped else last or {}).get("short") or "Finished"
        sent = (last or {}).get("output", {}).get("sent") if last else None
        output = {"kind": "email", "title": last.get("label") or "Sent", "more": words} if sent else None
        return {"result": words, "output": output}


def _shown_status(views: list[tuple[str, str | None, str | None]], taken: bool,
                  run: Any) -> tuple[str, str | None, str | None]:
    """A step's status, its approval card and its note: its last record's, or why it has none."""
    status, approval, note = views[-1] if views else ("skipped", None, None)
    if not views and taken and not run.terminal:  # a run under way has not reached it yet: not skipped
        status, note = "queued", "Not reached yet: the run is still going."
    elif not views:
        note = "This path was not taken." if not taken else "It did not get this far."
    return status, approval, note


def _add_marks(view: dict[str, Any], status: str, result: dict[str, Any], state: dict[str, Any],
               error: dict[str, Any]) -> None:
    """What a step's view says about where its words came from, and why it waits."""
    outside = result.get("untrusted") or state.get("input_outside")
    if outside:
        view["untrusted"] = outside
    if status in ("failed", "partial") and not error.get("own_words"):
        view["note_outside"] = True  # a failure's words may quote what it read, unless the walk wrote them itself
    if state.get("why"):
        view["why"] = state["why"]
