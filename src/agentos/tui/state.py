"""Everything the dashboard knows about the node, and the views derived from it.

Pure Python: the sync layer writes into a DashState, the screens read from it.
`apply_event` folds one SSE event in and returns the topics that changed, so the
screens redraw only what moved.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any

from agentos.core.signals import ATTENTION, TUI_NEED
from agentos.tui import fmt
from agentos.tui.models import (
    Approval,
    Brain,
    BrainTask,
    Control,
    Environment,
    Flow,
    Lane,
    Node,
    Question,
    Run,
    Schedule,
    Status,
    TraceEvent,
    Workspace,
    parse_list,
    parse_one,
)

RESOURCES = ("status", "workspaces", "nodes", "approvals", "questions", "activity", "runs",
             "schedules", "flows", "brains", "tasks", "environments")

ACTIVITY_CAP = 1000
RUNS_CAP = 300

# what a lane needs from a human, in the order the queue shows them
LANE_NEEDS = {
    "captcha": "captcha to solve",
    "checkpoint": "the site asks you to confirm it is you",
    "logged_out": "logged out, log in once",
    "needs_login": "needs a first login",
    "quarantined": "quarantined",
    "blocked": "blocked by the site",
}


@dataclass
class Resource:
    status: str = "loading"  # loading | ok | unavailable | denied | error
    message: str = ""
    loaded_at: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class ConnState:
    online: bool | None = None   # None until the first answer
    reason: str = ""
    auth_failed: bool = False
    last_ok: datetime | None = None
    retry_at: datetime | None = None
    live: str = "connecting"     # connecting | live | polling | reconnecting


@dataclass
class NeedItem:
    key: str              # approval:<id> | question:<id> | control:<lane> | lane:<lane>
    kind: str             # approval | question | control | lane
    group: str            # group key: control, question, effect:<effect>, lane
    title: str
    lane: str | None = None
    ref: Any = None
    at: datetime | None = None
    detail: str = ""


@dataclass
class LaneIssue:
    word: str
    health: str  # attention | blocked


class DashState:
    def __init__(self) -> None:
        self.status: Status | None = None
        self.workspaces: list[Workspace] = []
        self.nodes: list[Node] = []
        self.approvals: dict[str, Approval] = {}
        self.questions: dict[str, Question] = {}
        self.activity: list[TraceEvent] = []
        self._activity_ids: set[str] = set()
        self.runs: dict[str, Run] = {}
        self.schedules: dict[str, Schedule] = {}
        self.flows: dict[str, Flow] = {}
        self.brains: list[Brain] = []
        self.tasks: dict[str, BrainTask] = {}
        self.environments: list[Environment] = []
        self.res: dict[str, Resource] = {name: Resource() for name in RESOURCES}
        self.conn = ConnState()
        self.last_event_id: str | None = None
        self.event_count = 0
        self.extra_needs: int = 0

    # ---- setters from full loads ----
    def set(self, name: str, data: Any) -> set[str]:
        setter = getattr(self, f"_set_{name}")
        setter(data)
        return {name}

    def _set_status(self, data: Any) -> None:
        self.status = parse_one(Status, data) or Status()

    def _set_workspaces(self, data: Any) -> None:
        self.workspaces = parse_list(Workspace, data)

    def _set_nodes(self, data: Any) -> None:
        self.nodes = parse_list(Node, data)

    def _set_approvals(self, data: Any) -> None:
        # questions arrive here too (kind "question") and are listed under questions instead
        self.approvals = {a.id: a for a in parse_list(Approval, data)
                          if a.state == "pending" and getattr(a, "kind", None) != "question"}

    def _set_questions(self, data: Any) -> None:
        self.questions = {q.id: q for q in parse_list(Question, data) if q.state in ("open", "pending")}

    def _set_activity(self, data: Any) -> None:
        events = parse_list(TraceEvent, data)
        events.sort(key=lambda e: e.at.timestamp() if e.at else 0.0)
        # keep live events that arrived after the snapshot was cut
        newest = events[-1].at if events and events[-1].at else None
        later = [e for e in self.activity if newest and e.at and e.at > newest
                 and e.id not in {x.id for x in events}]
        self.activity = (events + later)[-ACTIVITY_CAP:]
        self._activity_ids = {e.id for e in self.activity}

    def _set_runs(self, data: Any) -> None:
        runs = {r.id: r for r in parse_list(Run, data)}
        for rid, run in runs.items():  # the list leaves out steps; keep the ones we fetched
            old = self.runs.get(rid)
            if old is not None and not run.steps and old.steps:
                run.steps = old.steps
        self.runs = runs

    def _set_schedules(self, data: Any) -> None:
        self.schedules = {s.id: s for s in parse_list(Schedule, data)}

    def _set_flows(self, data: Any) -> None:
        self.flows = {f.id: f for f in parse_list(Flow, data)}

    def _set_brains(self, data: Any) -> None:
        self.brains = parse_list(Brain, data)

    def _set_tasks(self, data: Any) -> None:
        tasks = {t.id: t for t in parse_list(BrainTask, data)}
        for tid, task in tasks.items():  # the list leaves out the tail; keep the one we fetched
            old = self.tasks.get(tid)
            if old is not None and task.tail is None and old.tail is not None:
                task.tail = old.tail
        self.tasks = tasks

    def _set_environments(self, data: Any) -> None:
        envs = parse_list(Environment, data)
        envs.sort(key=lambda e: (not e.open, -(e.created_at.timestamp() if e.created_at else 0.0)))
        self.environments = envs

    # ---- environments: one name each, its title ----
    def environment_of(self, lane_id: str | None) -> Environment | None:
        """The environment a lane belongs to (env/<id>, its terminal, or env/<id>-browser), if known."""
        if not lane_id:
            return None
        for env in self.environments:
            if lane_id in env.lanes.values() or lane_id in (f"env/{env.id}", f"env/{env.id}-browser"):
                return env
        return None

    def place(self, lane_id: str | None) -> str:
        """How a lane is named on screen: an environment's lanes by its title, every other by its id."""
        env = self.environment_of(lane_id)
        if env is None:
            return lane_id or ""
        return f"{env.title} (browser)" if str(lane_id).endswith("-browser") else env.title

    def account_workspaces(self) -> list[Workspace]:
        """Workspaces as accounts: an environment's lanes are its own computer, shown under Environments, so
        they are left out, and the env workspace with them when nothing else is in it."""
        out = []
        for ws in self.workspaces:
            kept = [lane for lane in ws.lanes if self.environment_of(lane.id) is None]
            if ws.lanes and not kept:
                continue
            if len(kept) != len(ws.lanes):
                ws = ws.model_copy(update={"lanes": kept})
            out.append(ws)
        return out

    # ---- lookups ----
    def lanes(self) -> Iterable[Lane]:
        for ws in self.workspaces:
            yield from ws.lanes

    def lane(self, lane_id: str) -> Lane | None:
        for lane in self.lanes():
            if lane.id == lane_id:
                return lane
        return None

    def workspace(self, ws_id: str) -> Workspace | None:
        return next((w for w in self.workspaces if w.id == ws_id), None)

    def node(self, node_id: str | None) -> Node | None:
        return next((n for n in self.nodes if n.id == node_id), None) if node_id else None

    def put_lane(self, lane: Lane) -> None:
        ws = self.workspace(lane.workspace)
        if ws is None:
            ws = Workspace(id=lane.workspace, name=lane.workspace)
            self.workspaces.append(ws)
        for i, old in enumerate(ws.lanes):
            if old.id == lane.id:
                if lane.viewer is None and old.viewer is not None and lane.state in ("warm", "active"):
                    lane.viewer = old.viewer
                ws.lanes[i] = lane
                return
        ws.lanes.append(lane)

    def add_trace(self, event: TraceEvent) -> bool:
        if event.id in self._activity_ids:
            return False
        self.activity.append(event)
        self._activity_ids.add(event.id)
        if len(self.activity) > ACTIVITY_CAP:
            dropped = self.activity[: len(self.activity) - ACTIVITY_CAP]
            self.activity = self.activity[-ACTIVITY_CAP:]
            self._activity_ids.difference_update(e.id for e in dropped)
        return True

    @property
    def tz(self) -> tzinfo:
        name = self.status.timezone if self.status else None
        if not name:
            node = self.nodes[0] if self.nodes else None
            name = node.timezone if node else None
        return fmt.zone(name)

    @property
    def tz_name(self) -> str:
        tz = self.tz
        return getattr(tz, "key", None) or str(tz)

    # ---- live updates ----
    def apply_event(self, event: str, data: Any, event_id: str | None = None) -> set[str]:
        """Fold one SSE event into the state. Returns the topics that changed."""
        if event_id:
            self.last_event_id = event_id
        self.event_count += 1
        if not isinstance(data, dict):
            return set()
        handler = getattr(self, f"_on_{event.replace('-', '_')}", None)
        if handler is None:
            return set()
        return handler(data) or set()

    def _on_lane(self, data: dict) -> set[str]:
        lane = parse_one(Lane, data)
        if lane is None:
            return set()
        self.put_lane(lane)
        return {"workspaces"}

    def _on_control(self, data: dict) -> set[str]:
        lane = self.lane(str(data.get("lane") or ""))
        control = parse_one(Control, data.get("control"))
        if lane is None or control is None:
            return set()
        lane.control = control
        return {"workspaces"}

    def _on_approval(self, data: dict) -> set[str]:
        approval = parse_one(Approval, data)
        if approval is None:
            return set()
        if approval.state == "pending":
            self.approvals[approval.id] = approval
        else:
            self.approvals.pop(approval.id, None)
        return {"approvals"}

    def _on_question(self, data: dict) -> set[str]:
        question = parse_one(Question, data)
        if question is None:
            return set()
        if question.state in ("open", "pending"):
            self.questions[question.id] = question
        else:
            self.questions.pop(question.id, None)
        return {"questions"}

    def _on_run(self, data: dict) -> set[str]:
        run = parse_one(Run, data)
        if run is None:
            return set()
        old = self.runs.get(run.id)
        if old is not None and not run.steps and old.steps:
            run.steps = old.steps
        self.runs[run.id] = run
        if len(self.runs) > RUNS_CAP:
            for rid in [r.id for r in self.sorted_runs()[RUNS_CAP:]]:
                self.runs.pop(rid, None)
        return {"runs"}

    def _on_trace(self, data: dict) -> set[str]:
        event = parse_one(TraceEvent, data)
        return {"activity"} if event is not None and self.add_trace(event) else set()

    def _on_node(self, data: dict) -> set[str]:
        node = parse_one(Node, data)
        if node is None:
            return set()
        for i, old in enumerate(self.nodes):
            if old.id == node.id:
                self.nodes[i] = node
                break
        else:
            self.nodes.append(node)
        return {"nodes"}

    def _on_schedule(self, data: dict) -> set[str]:
        schedule = parse_one(Schedule, data)
        if schedule is None:
            return set()
        if data.get("deleted"):
            self.schedules.pop(schedule.id, None)
        else:
            self.schedules[schedule.id] = schedule
        return {"schedules"}

    def _on_brain(self, data: dict) -> set[str]:
        brain = parse_one(Brain, data)
        if brain is None:
            return set()
        for i, old in enumerate(self.brains):
            if old.name == brain.name:
                self.brains[i] = brain
                break
        else:
            self.brains.append(brain)
        return {"brains"}

    def _on_task(self, data: dict) -> set[str]:
        task = parse_one(BrainTask, data)
        if task is None:
            return set()
        self.tasks[task.id] = task
        return {"tasks"}

    _on_brain_task = _on_task

    def _on_status(self, data: dict) -> set[str]:
        self._set_status(data)
        return {"status"}

    # ---- derived: runs, schedules ----
    def sorted_runs(self) -> list[Run]:
        """Newest first; queued runs with no start time lead."""
        return sorted(self.runs.values(), reverse=True,
                      key=lambda r: r.started_at.timestamp() if r.started_at else float("inf"))

    def sorted_schedules(self) -> list[Schedule]:
        return sorted(self.schedules.values(),
                      key=lambda s: (not s.enabled, s.next_fire_at is None,
                                     s.next_fire_at.timestamp() if s.next_fire_at else 0, s.flow))

    def flow_lane(self, flow_id: str) -> str | None:
        flow = self.flows.get(flow_id)
        if flow and flow.lane:
            return flow.lane
        parts = flow_id.split(".")
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
        return None

    def schedule_lane(self, schedule: Schedule) -> str | None:
        return schedule.lane or self.flow_lane(schedule.flow)

    def scheduled_today(self, ws_id: str, now: datetime | None = None) -> list[Schedule]:
        tz = self.tz
        out = []
        for s in self.schedules.values():
            lane = self.schedule_lane(s) or ""
            upcoming = s.next_fire_at is not None and s.next_fire_at >= (now or fmt.now_utc())
            if s.enabled and upcoming and lane.split("/", 1)[0] == ws_id and fmt.is_today(s.next_fire_at, tz, now):
                out.append(s)
        return out

    # ---- derived: lane and workspace health ----
    def lane_issues(self, lane: Lane) -> list[LaneIssue]:
        issues: list[LaneIssue] = []
        if lane.state == "quarantined":
            issues.append(LaneIssue("quarantined", "blocked"))
        if lane.signal == "blocked":
            issues.append(LaneIssue("blocked", "blocked"))
        elif lane.signal in ATTENTION:
            issues.append(LaneIssue(fmt.SIGNAL_WORD[lane.signal], "attention"))
        if lane.logged_in_hint == "needs_login" and lane.signal != "logged_out":
            issues.append(LaneIssue("needs login", "attention"))
        if lane.control.takeback_requested:
            issues.append(LaneIssue("wants control back", "attention"))
        node = self.node(lane.node)
        if node is not None and node.state == "down":
            issues.append(LaneIssue(f"{node.id} down", "attention"))
        return issues

    def workspace_health(self, ws: Workspace) -> str:
        if not ws.lanes:
            return ws.health or "idle"
        worst = "ok"
        for lane in ws.lanes:
            for issue in self.lane_issues(lane):
                if issue.health == "blocked":
                    return "blocked"
                worst = "attention"
        if worst == "ok" and all(lane.state == "cold" for lane in ws.lanes):
            return "idle"
        return worst

    def workspace_line(self, ws: Workspace, now: datetime | None = None) -> str:
        """One calm health line: '6 lanes, all ok, 2 posts scheduled today'."""
        if not ws.lanes:
            return ws.summary or "no lanes yet"
        parts = [fmt.plural(len(ws.lanes), "lane")]
        counts: dict[str, int] = {}
        for lane in ws.lanes:
            for issue in self.lane_issues(lane):
                counts[issue.word] = counts.get(issue.word, 0) + 1
        humans = sum(1 for lane in ws.lanes if lane.control.state == "human")
        paused = sum(1 for lane in ws.lanes if lane.control.state == "paused")
        if counts:
            parts.extend(f"{n} {word}" if not word.endswith(" down") else word for word, n in counts.items())
        elif all(lane.state == "cold" for lane in ws.lanes):
            parts.append("sleeping")
        else:
            parts.append("all ok")
        if humans:
            parts.append(f"{humans} with you")
        if paused:
            parts.append(f"{paused} paused")
        today = self.scheduled_today(ws.id, now)
        if today:
            noun = "post" if all(s.flow.endswith(".post") for s in today) else "run"
            parts.append(f"{fmt.plural(len(today), noun)} scheduled today")
        return ", ".join(parts)

    def should_expand(self, ws: Workspace) -> bool:
        return self.workspace_health(ws) in ("attention", "blocked") or any(
            lane.control.state != "agent" for lane in ws.lanes)

    # ---- derived: the Needs you queue ----
    def needs_you(self) -> list[NeedItem]:
        items: list[NeedItem] = []
        for lane in self.lanes():
            c = lane.control
            if c.takeback_requested:
                reason = c.takeback_reason or "no reason given"
                items.append(NeedItem(key=f"control:{lane.id}", kind="control", group="control",
                                      title=f"{lane.id}: agent wants control back", lane=lane.id,
                                      ref=lane, at=c.since, detail=reason))
        for q in sorted(self.questions.values(), key=_by_time):
            items.append(NeedItem(key=f"question:{q.id}", kind="question", group="question",
                                  title=fmt.one_line(q.question), lane=q.lane, ref=q, at=q.created_at))
        order = {e: i for i, e in enumerate(fmt.EFFECT_ORDER)}
        for a in sorted(self.approvals.values(),
                        key=lambda a: (order.get(fmt.norm(a.effect), 99), _by_time(a))):
            items.append(NeedItem(key=f"approval:{a.id}", kind="approval", group=f"effect:{fmt.norm(a.effect)}",
                                  title=fmt.one_line(a.summary) or a.id, lane=a.lane, ref=a, at=a.created_at))
        for lane in self.lanes():
            need = self.lane_need(lane)
            if need:
                items.append(NeedItem(key=f"lane:{lane.id}", kind="lane", group="lane",
                                      title=f"{lane.id}: {need}", lane=lane.id, ref=lane, at=lane.last_used,
                                      detail=lane.quarantine_reason or ""))
        return items

    def lane_need(self, lane: Lane) -> str | None:
        if lane.state == "quarantined":
            return LANE_NEEDS["quarantined"]
        if lane.signal in TUI_NEED:
            return LANE_NEEDS[lane.signal]
        if lane.logged_in_hint == "needs_login":
            return LANE_NEEDS["needs_login"]
        return None

    def needs_you_count(self) -> int:
        count = len(self.needs_you())
        if not self.res["approvals"].ok and self.status is not None:
            # the queue is not readable here; trust the node's own count
            return max(count, self.status.needs_you)
        return count

    def needs_you_summary(self) -> str:
        items = self.needs_you()
        if not items:
            return "nothing waiting"
        counts: dict[str, int] = {}
        for item in items:
            if item.kind == "approval":
                effect = fmt.norm(item.ref.effect)
                word = "post approval" if effect == "commit" else f"{effect.replace('_', ' ')} approval"
            elif item.kind == "question":
                word = "question"
            elif item.kind == "control":
                word = "wants control back"
            else:
                word = self.lane_need(item.ref) or "lane"
                word = {"captcha to solve": "captcha", "logged out, log in once": "logged out",
                        "needs a first login": "needs login"}.get(word, word)
            counts[word] = counts.get(word, 0) + 1
        out = []
        for word, n in counts.items():
            if word.endswith("approval") or word == "question":
                out.append(fmt.plural(n, word))
            else:
                out.append(f"{n} {word}")
        return ", ".join(out)

    # ---- derived: activity ----
    def filtered_activity(self, *, workspace: str | None = None, lane: str | None = None,
                          status: str | None = None, effect: str | None = None) -> list[TraceEvent]:
        out = []
        for e in self.activity:
            if workspace and e.workspace != workspace:
                continue
            if lane and e.lane != lane:
                continue
            if effect and fmt.norm(e.effect) != fmt.norm(effect):
                continue
            if status and fmt.status_info(e.status, e.effect)[0] != status:
                continue
            out.append(e)
        return out

    def cost_today(self) -> tuple[float | None, int | None]:
        cost = self.status.cost_today if self.status else None
        return (cost.usd if cost else None, cost.tokens if cost else None)


def _by_time(obj: Any) -> float:
    at = getattr(obj, "created_at", None)
    return at.timestamp() if at else 0.0


def merge_lane_detail(state: DashState, data: Any) -> Lane | None:
    """Fold a GET /api/lanes/{ws}/{name} answer (which carries `viewer`) into the state."""
    lane = parse_one(Lane, data)
    if lane is not None:
        state.put_lane(lane)
    return lane


@dataclass
class ActivityFilter:
    workspace: str | None = None
    lane: str | None = None
    status: str | None = None
    effect: str | None = None
    extra: dict = field(default_factory=dict)

    def active(self) -> bool:
        return any((self.workspace, self.lane, self.status, self.effect))

    def describe(self) -> str:
        parts = []
        if self.workspace:
            parts.append(f"workspace {self.workspace}")
        if self.lane:
            parts.append(f"lane {self.lane}")
        if self.status:
            parts.append(f"status {self.status}")
        if self.effect:
            parts.append(f"effect {self.effect}")
        return ", ".join(parts) if parts else "everything"
