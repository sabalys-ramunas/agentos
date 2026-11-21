"""The workspaces service: workspace records, template plans, apply and the login queue.

`build(runtime)` opens `$OPENAJAR_HOME/workspaces.db` and registers the service as "workspaces". Lanes
are read from the runtime's "lanes" service (the node's LaneManager) and created only through
LaneManager.create, so every new lane still passes the node's own checks (engine, proxy label, id).

Login state is never detected here. It is set explicitly, by the owner or by an agent after the owner
says so. Nothing in this package visits a site.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from openajar.core.config import NodeConfig, home_from_env, load_proxies
from openajar.core.errors import InvalidRequest, LaneExists, NotFound, OpenajarError, ServiceUnavailable
from openajar.core.models import LaneInfo, LaneSpec, LaneState, parse_lane_id, utcnow
from openajar.core.runtime import Runtime
from openajar.workspaces.errors import WorkspaceConflict
from openajar.workspaces.models import (
    COLOR_RE,
    NAME_MAX,
    NOTE_MAX,
    PLAN_MAX_LANES,
    READINESS_NOTE_MAX,
    ApplyResult,
    LaneView,
    OverrideRecord,
    Plan,
    PlanStep,
    QueueView,
    Readiness,
    Template,
    TemplateProblem,
    WorkspaceCount,
    WorkspaceView,
    check_template_id,
    check_workspace_id,
)
from openajar.workspaces.planner import existing_problems, make_plan, refusal
from openajar.workspaces.rules import LaneFact, proxy_conflicts
from openajar.workspaces.store import WorkspaceStore
from openajar.workspaces.templates import load_templates

log = logging.getLogger("openajar.workspaces")

DB_NAME = "workspaces.db"
SERVICE_NAME = "workspaces"
WORKSPACE_FIELDS = ("name", "color", "default_proxy", "notes")


def derived_name(workspace_id: str) -> str:
    """The name of a workspace that only exists as a lane-id prefix: 'biz-a' becomes 'Biz A'."""
    return " ".join(part.capitalize() for part in workspace_id.split("-"))


def _summary(ready: int, total: int) -> str:
    return "no lanes yet" if total == 0 else f"{ready} of {total} ready"


def _health(lanes: list[LaneView]) -> str:
    if not lanes:
        return "idle"
    if any(lane.lane_state == LaneState.QUARANTINED.value for lane in lanes):
        return "blocked"
    if any(lane.readiness == Readiness.NEEDS_ATTENTION for lane in lanes):
        return "attention"
    if all(lane.readiness == Readiness.READY for lane in lanes):
        return "ok"
    return "idle"  # waiting for the owner to log in


def _platform_of(lane_id: str, row: dict[str, Any] | None) -> str:
    """The platform a lane was made for, or its own name when no workspace record says otherwise."""
    return row["platform"] if row else parse_lane_id(lane_id)[1]


def _failure(lane: str, code: str, message: str) -> dict[str, str]:
    return {"lane": lane, "code": code, "message": message}


def _text(value: object, *, what: str, limit: int, required: bool = False, multiline: bool = False) -> str | None:
    """Trimmed text of at most `limit` characters. Names are one line; notes may have line breaks."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidRequest(f"{what} must be text")
    text = value.strip()
    if required and not text:
        raise InvalidRequest(f"{what} cannot be empty")
    if len(text) > limit:
        raise InvalidRequest(f"{what} must be at most {limit} characters")
    allowed = {"\n", "\t"} if multiline else set()
    if any(not ch.isprintable() and ch not in allowed for ch in text):
        raise InvalidRequest(f"{what} cannot contain control characters")
    return text


def _readiness(state: object) -> Readiness:
    try:
        return Readiness(state)
    except ValueError:
        options = ", ".join(r.value for r in Readiness)
        raise InvalidRequest(f"state must be one of: {options}") from None


def _proxy_map(value: object, what: str) -> dict[str, str | None]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InvalidRequest(f"{what} must map names to proxy labels")
    out: dict[str, str | None] = {}
    for key, label in value.items():
        if not isinstance(key, str) or not (label is None or isinstance(label, str)):
            raise InvalidRequest(f"{what} must map names to proxy labels (or null for no proxy)")
        out[key] = label
    return out


def _unknown_label(label: object, labels: set[str]) -> InvalidRequest:
    """Never the labels that exist: a grant for one workspace is not told another's proxies. The
    owner lists them on the node (`openajar proxy list`) or in the panel."""
    if not labels:
        return InvalidRequest(f"proxy {label!r} is not configured: no proxies are set up on this node yet "
                              "(add one with: openajar proxy add)")
    return InvalidRequest(f"proxy {label!r} is not configured on this node. The owner sees the labels with: "
                          "openajar proxy list")


class WorkspaceService:
    """Workspaces, templates and the login queue, on top of the node's lane manager."""

    def __init__(self, store: WorkspaceStore, runtime: Runtime, *, labels: Callable[[], set[str]],
                 templates_dir: Path | None, clock: Callable[[], datetime] = utcnow):
        self.store = store
        self.runtime = runtime
        self.templates_dir = templates_dir
        self._labels = labels
        self._clock = clock
        self._apply_lock = asyncio.Lock()

    # ---- plumbing --------------------------------------------------------------

    def _lanes(self) -> Any:
        return self.runtime.require("lanes")

    def _now(self) -> str:
        return self._clock().isoformat()

    def _lane_infos(self) -> dict[str, LaneInfo]:
        return {info.spec.id: info for info in self._lanes().list()}

    def _known_labels(self) -> set[str]:
        return set(self._labels())

    def _facts(self, infos: dict[str, LaneInfo], rows: dict[str, dict[str, Any]]) -> dict[str, LaneFact]:
        return {lane: LaneFact(lane, info.spec.workspace, _platform_of(lane, rows.get(lane)),
                               info.spec.proxy_label) for lane, info in infos.items()}

    def _lane_view(self, info: LaneInfo, row: dict[str, Any] | None) -> LaneView:
        spec, row = info.spec, row or {}
        return LaneView(
            id=spec.id, workspace=spec.workspace, name=spec.name, surface=row.get("surface"),
            platform=row.get("platform") or spec.name, proxy=spec.proxy_label, lane_state=info.state.value,
            readiness=Readiness(row.get("readiness") or Readiness.NEEDS_LOGIN), note=row.get("note"),
            login_url=row.get("login_url"), home_url=row.get("home_url"), updated_at=row.get("updated_at"),
            updated_by=row.get("updated_by"))

    def _workspace_view(self, workspace_id: str, record: dict[str, Any] | None,
                        lanes: list[LaneView]) -> WorkspaceView:
        ready = sum(1 for lane in lanes if lane.readiness == Readiness.READY)
        return WorkspaceView(
            id=workspace_id, name=record["name"] if record else derived_name(workspace_id),
            color=record["color"] if record else None, default_proxy=record["default_proxy"] if record else None,
            notes=record["notes"] if record else "", derived=record is None, health=_health(lanes),
            summary=_summary(ready, len(lanes)), ready=ready, total=len(lanes),
            created_at=record["created_at"] if record else None,
            updated_at=record["updated_at"] if record else None,
            lanes=sorted(lanes, key=lambda lane: lane.name))

    def _known(self, workspace_id: str) -> bool:
        if self.store.get_workspace(workspace_id) is not None:
            return True
        return any(info.spec.workspace == workspace_id for info in self._lane_infos().values())

    # ---- workspaces ----------------------------------------------------------------

    def list_workspaces(self, *, allowed: Callable[[str], bool] | None = None) -> list[WorkspaceView]:
        """Every workspace, including ones that only exist as a lane-id prefix (derived names).
        With `allowed`, lanes outside it are left out, and a workspace with none left is left out too."""
        records = {r["id"]: r for r in self.store.list_workspaces()}
        rows = self.store.lane_rows()
        by_workspace: dict[str, list[LaneView]] = {}
        for info in self._lane_infos().values():
            if allowed is not None and not allowed(info.spec.id):
                continue
            by_workspace.setdefault(info.spec.workspace, []).append(self._lane_view(info, rows.get(info.spec.id)))
        ids = set(by_workspace) if allowed is not None else set(by_workspace) | set(records)
        return [self._workspace_view(ws, records.get(ws), by_workspace.get(ws, [])) for ws in sorted(ids)]

    def get_workspace(self, workspace_id: str, *, allowed: Callable[[str], bool] | None = None) -> WorkspaceView:
        check_workspace_id(workspace_id)
        record = self.store.get_workspace(workspace_id)
        infos = [info for info in self._lane_infos().values() if info.spec.workspace == workspace_id]
        if record is None and not infos:
            raise self._missing(workspace_id, allowed)
        rows = self.store.lane_rows()
        lanes = [self._lane_view(info, rows.get(info.spec.id)) for info in infos
                 if allowed is None or allowed(info.spec.id)]
        return self._workspace_view(workspace_id, record, lanes)

    def _missing(self, workspace_id: str, allowed: Callable[[str], bool] | None) -> NotFound:
        """not_found for a workspace that is not there, naming a close one the caller may see first."""
        from openajar.core.text import close_match

        found = close_match(workspace_id, [ws.id for ws in self.list_workspaces(allowed=allowed)])
        if found:
            return NotFound(f"no workspace {workspace_id!r}. Did you mean {found!r}? (see the workspace list)")
        return NotFound(f"no workspace {workspace_id!r}; create it with: openajar workspace create {workspace_id}")

    def _check_field(self, key: str, value: object) -> Any:
        if key == "name":
            if value is None:
                raise InvalidRequest("name cannot be cleared; give the workspace a name")
            return _text(value, what="name", limit=NAME_MAX, required=True)
        if key == "color":
            if value is None:
                return None
            if not isinstance(value, str) or not COLOR_RE.fullmatch(value):
                raise InvalidRequest("color must look like #rrggbb, for example #4f7cff")
            return value.lower()
        if key == "default_proxy":
            if value is None:
                return None
            if not isinstance(value, str) or value not in self._known_labels():
                raise _unknown_label(value, self._known_labels())
            return value
        return _text(value, what="notes", limit=NOTE_MAX, multiline=True) or ""

    def create_workspace(self, workspace_id: str, *, name: str | None = None, color: str | None = None,
                         default_proxy: str | None = None, notes: str | None = None) -> WorkspaceView:
        check_workspace_id(workspace_id)
        if self.store.get_workspace(workspace_id) is not None:
            raise WorkspaceConflict(f"workspace {workspace_id!r} already exists; change it with: "
                           f"openajar workspace update {workspace_id}")
        fields = {"name": derived_name(workspace_id) if name is None else self._check_field("name", name),
                  "color": self._check_field("color", color),
                  "default_proxy": self._check_field("default_proxy", default_proxy),
                  "notes": self._check_field("notes", notes) or ""}
        now = self._now()
        self.store.insert_workspace({"id": workspace_id, **fields, "created_at": now, "updated_at": now})
        return self.get_workspace(workspace_id)

    def update_workspace(self, workspace_id: str, changes: dict[str, Any]) -> WorkspaceView:
        """Change any of name, color, default_proxy, notes. A null color or proxy clears it. The default
        proxy only shapes future plans: existing lanes are never moved by it."""
        check_workspace_id(workspace_id)
        unknown = set(changes) - set(WORKSPACE_FIELDS)
        if unknown:
            raise InvalidRequest(f"a workspace can change: {', '.join(WORKSPACE_FIELDS)} (not {', '.join(sorted(unknown))})")
        if not self._known(workspace_id):
            raise NotFound(f"no workspace {workspace_id!r}; create it with: openajar workspace create {workspace_id}")
        fields = {key: self._check_field(key, value) for key, value in changes.items()}
        now = self._now()
        if self.store.get_workspace(workspace_id) is None:
            # A workspace that only existed as a lane-id prefix becomes a record when it is first changed.
            self.store.insert_workspace({"id": workspace_id, "name": derived_name(workspace_id), "color": None,
                                         "default_proxy": None, "notes": "", "created_at": now, "updated_at": now})
        if fields:
            self.store.update_workspace(workspace_id, fields, now)
        return self.get_workspace(workspace_id)

    def _memory_file(self, workspace_id: str) -> Path | None:
        """The workspace's memory file (openajar.memory), when it has one."""
        cfg = self.runtime.get("config") if self.runtime is not None else None
        home = getattr(cfg, "home", None)
        path = Path(home) / "memory" / f"{workspace_id}.db" if home else None
        return path if path is not None and path.exists() else None

    def delete_workspace(self, workspace_id: str, *, with_memory: bool = False) -> None:
        """Refused while the workspace still has lanes: a lane belongs to exactly one workspace.
        Refused too while it has memory, unless `with_memory`, which deletes the memory file."""
        check_workspace_id(workspace_id)
        lanes = [info.spec.id for info in self._lane_infos().values() if info.spec.workspace == workspace_id]
        if lanes:
            raise WorkspaceConflict(f"workspace {workspace_id!r} still has {len(lanes)} "
                           f"{'lane' if len(lanes) == 1 else 'lanes'}; a workspace can only be deleted "
                           "once it has none")
        if self.store.get_workspace(workspace_id) is None:
            raise NotFound(f"no workspace {workspace_id!r}")
        memory = self.runtime.get("memory") if self.runtime is not None else None
        has = memory.has_memory(workspace_id) if memory is not None else self._memory_file(workspace_id) is not None
        if has and not with_memory:
            raise WorkspaceConflict(f"workspace {workspace_id!r} has memory. Export it first (openajar memory export "
                                    f"{workspace_id} --md DIR), then delete with --with-memory, which erases it")
        self.store.delete_workspace(workspace_id)
        if has:
            if memory is not None:
                memory.drop_workspace(workspace_id)
            elif (path := self._memory_file(workspace_id)) is not None:
                for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
                    p.unlink(missing_ok=True)

    # ---- templates ------------------------------------------------------------------

    def templates(self) -> tuple[list[Template], list[TemplateProblem]]:
        """Built-in templates first, then user templates. Bad user files come back as problems."""
        templates, problems = load_templates(self.templates_dir)
        return sorted(templates.values(), key=lambda t: (not t.builtin, t.id)), problems

    def get_template(self, template_id: str) -> Template:
        check_template_id(template_id)
        templates, problems = load_templates(self.templates_dir)
        if template_id in templates:
            return templates[template_id]
        broken = [p.message for p in problems if p.template == template_id]
        if broken:
            raise InvalidRequest(f"template {template_id!r} cannot be used: {'; '.join(broken)}")
        known = sorted(templates)
        names = ", ".join(known[:20]) + (f" and {len(known) - 20} more" if len(known) > 20 else "")
        raise NotFound(f"no template {template_id!r}. The templates on this node are: {names or 'none'}. "
                       "`openajar workspace templates` lists them with the lanes each one makes")

    # ---- readiness and the login queue ----------------------------------------------

    def set_readiness(self, lane_id: str, state: object, *, note: object = None, by: str) -> LaneView:
        """Record that the owner has logged the lane in (ready), or that it still needs it
        (needs_login), or that something is wrong (needs_attention, with a note saying what)."""
        try:
            workspace, _ = parse_lane_id(lane_id)
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from None
        readiness = _readiness(state)
        info = self._lanes().get(lane_id)  # LaneNotFound for a lane the node does not have
        text = _text(note, what="note", limit=READINESS_NOTE_MAX, multiline=True)
        if readiness is Readiness.NEEDS_ATTENTION and not text:
            raise InvalidRequest("needs_attention needs a note saying what is wrong, for example: "
                                 "the site asks for a phone code")
        row = self.store.lane_row(lane_id)
        self.store.set_readiness(lane_id, workspace=workspace, platform=_platform_of(lane_id, row),
                                 readiness=readiness.value, note=text or None, by=by, now=self._now())
        return self._lane_view(info, self.store.lane_row(lane_id))

    def _environment_lane(self, lane_id: str) -> bool:
        """An environment's own lanes (env/<id>, env/<id>-browser) are the agent's computer, not an account to
        sign in: they stay out of the login queue and its counts (2026-11-15 test, F5)."""
        workspace, _, name = lane_id.partition("/")
        envs = self.runtime.get("environments") if workspace == "env" and self.runtime is not None else None
        try:
            return bool(envs is not None and envs.store.has(name.removesuffix("-browser")))
        except (AttributeError, ValueError):
            return False

    def _signed_in(self, view: LaneView) -> LaneView:
        """A lane nobody marked whose last session check passed reads as ready, as Accounts reads it."""
        if view.readiness != Readiness.NEEDS_LOGIN or view.updated_by:
            return view
        checker = self.runtime.get("lane_check") if self.runtime is not None else None
        report = (getattr(checker, "last", None) or {}).get(view.id) if checker is not None else None
        session = next((c for c in getattr(report, "checks", None) or [] if getattr(c, "id", None) == "session"), None)
        if str(getattr(session, "level", "")) != "ok":
            return view
        return view.model_copy(update={"readiness": Readiness.READY})

    def queue(self, workspace: str | None = None, *, allowed: Callable[[str], bool] | None = None) -> QueueView:
        """Lanes that are not ready yet, needs_attention first, each with its login URL. The counts
        cover every lane in scope, ready ones included, so the panel can say "18 of 29 ready"."""
        if workspace is not None:
            check_workspace_id(workspace)
            if not self._known(workspace):
                raise NotFound(f"no workspace {workspace!r}")
        rows = self.store.lane_rows()
        views: list[LaneView] = []
        for info in self._lane_infos().values():
            if workspace is not None and info.spec.workspace != workspace:
                continue
            if allowed is not None and not allowed(info.spec.id):
                continue
            if self._environment_lane(info.spec.id):
                continue
            views.append(self._signed_in(self._lane_view(info, rows.get(info.spec.id))))
        per_workspace = []
        for ws in sorted({view.workspace for view in views}):
            mine = [view for view in views if view.workspace == ws]
            ready = sum(1 for view in mine if view.readiness == Readiness.READY)
            name = (self.store.get_workspace(ws) or {}).get("name") or derived_name(ws)
            per_workspace.append(WorkspaceCount(id=ws, name=name, summary=_summary(ready, len(mine)),
                                                ready=ready, total=len(mine)))
        ready = sum(1 for view in views if view.readiness == Readiness.READY)
        pending = sorted((view for view in views if view.readiness != Readiness.READY),
                         key=lambda v: (v.readiness != Readiness.NEEDS_ATTENTION, v.workspace, v.name))
        return QueueView(
            workspace=workspace, summary=_summary(ready, len(views)), ready=ready, total=len(views),
            needs_login=sum(1 for v in views if v.readiness == Readiness.NEEDS_LOGIN),
            needs_attention=sum(1 for v in views if v.readiness == Readiness.NEEDS_ATTENTION),
            workspaces=per_workspace, lanes=pending)

    # ---- plan and apply --------------------------------------------------------------

    def plan(self, template_id: str, workspaces: list[str], *, proxy_per_workspace: dict[str, str | None] | None = None,
             proxy_per_lane: dict[str, str | None] | None = None, override_proxy_rules: bool = False,
             reason: str | None = None, shown: Callable[[str], bool] | None = None) -> Plan:
        """What applying the template would do. Creates nothing. Proxy assignment: a per-lane label
        wins, then a per-workspace label, then the workspace's default proxy, then no proxy. `shown`:
        the lanes the caller may see; a conflict with any other is told without naming it."""
        template = self.get_template(template_id)
        if not isinstance(workspaces, list) or not workspaces:
            raise InvalidRequest("name at least one workspace to apply the template to")
        ids = [check_workspace_id(ws) for ws in workspaces]
        if len(set(ids)) != len(ids):
            dupes = sorted({ws for ws in ids if ids.count(ws) > 1})
            raise InvalidRequest(f"workspace listed more than once: {', '.join(dupes)}")
        for ws in ids:
            if not self._known(ws):
                raise NotFound(f"no workspace {ws!r}; create it first with: openajar workspace create {ws}")
        if len(ids) * len(template.surfaces) > PLAN_MAX_LANES:
            raise InvalidRequest(f"a plan may create at most {PLAN_MAX_LANES} lanes; this one would create "
                                 f"{len(ids) * len(template.surfaces)}")
        per_ws = _proxy_map(proxy_per_workspace, "proxy_per_workspace")
        per_lane = _proxy_map(proxy_per_lane, "proxy_per_lane")
        for ws in per_ws:
            if ws not in ids:
                raise InvalidRequest(f"proxy_per_workspace names {ws!r}, which is not in this plan")
        planned = {f"{ws}/{surface.name}" for ws in ids for surface in template.surfaces}
        for lane in per_lane:
            if lane not in planned:
                raise InvalidRequest(f"proxy_per_lane names {lane!r}, which this plan does not create")
        labels = self._known_labels()
        for label in [*per_ws.values(), *per_lane.values()]:
            if label is not None and label not in labels:
                raise _unknown_label(label, labels)
        clean_reason = (reason or "").strip() or None
        if override_proxy_rules and clean_reason is None:
            raise InvalidRequest("overriding the proxy rules needs a reason: say why these accounts may share an IP")
        proxies: dict[str, str | None] = {}
        for ws in ids:
            record = self.store.get_workspace(ws)
            default = record["default_proxy"] if record else None
            for surface in template.surfaces:
                lane = f"{ws}/{surface.name}"
                if lane in per_lane:
                    proxies[lane] = per_lane[lane]
                elif ws in per_ws:
                    proxies[lane] = per_ws[ws]
                else:
                    proxies[lane] = default
        existing = self._facts(self._lane_infos(), self.store.lane_rows())
        return make_plan(template, ids, proxies=proxies, existing=existing,
                         override=override_proxy_rules, reason=clean_reason, shown=shown)

    async def apply(self, template_id: str, workspaces: list[str], *, by: str, **options: Any) -> ApplyResult:
        """Plan, refuse on any conflict, then create. Used by the API. The CLI and the tools show the
        plan (or the approval) first and then call `execute`."""
        plan = self.plan(template_id, workspaces, **options)
        if plan.conflicts:
            raise WorkspaceConflict(refusal(plan))
        return await self.execute(plan, by=by)

    async def execute(self, plan: Plan, *, by: str) -> ApplyResult:
        """Create the plan's lanes, one at a time, re-checking each step against the live state first.
        Stops at the first failure and reports what was created, so a second run finishes the rest."""
        async with self._apply_lock:
            return await self._execute(plan, by=by)

    async def _execute(self, plan: Plan, *, by: str) -> ApplyResult:
        created: list[str] = []
        existing: list[str] = []
        overridden: list[dict[str, Any]] = []
        warnings: list[str] = []
        failed: dict[str, str] | None = None
        for step in plan.steps:
            if step.action == "conflict":
                failed = _failure(step.lane, WorkspaceConflict.code, "; ".join(step.reasons))
                break
            if step.lane in self._lane_infos():
                problems = self._problems_now(step)
                if problems:
                    failed = _failure(step.lane, WorkspaceConflict.code, "; ".join(problems))
                    break
                await self.store.run(self._adopt, step, plan)
                existing.append(step.lane)
                continue
            facts = self._facts(self._lane_infos(), self.store.lane_rows())
            candidate = LaneFact(step.lane, step.workspace, step.platform, step.proxy)
            reasons = proxy_conflicts(candidate, facts.values())
            unexpected = [reason for reason in reasons if reason not in step.overridden]
            if unexpected:
                failed = _failure(step.lane, WorkspaceConflict.code, "; ".join(unexpected))
                break
            try:
                await self._lanes().create(LaneSpec(id=step.lane, engine=step.engine, proxy_label=step.proxy,
                                                    sites=step.sites or []))
            except LaneExists:
                # Created by someone else since the plan was made. Accept it only if it is what the plan wants.
                problems = self._problems_now(step)
                if problems:
                    failed = _failure(step.lane, WorkspaceConflict.code, "; ".join(problems))
                    break
                await self.store.run(self._adopt, step, plan)
                existing.append(step.lane)
                continue
            except OpenajarError as exc:
                failed = _failure(step.lane, exc.code, exc.message)
                break
            except Exception:  # report and stop; the lanes made so far stay and are listed
                log.exception("creating lane %s failed", step.lane)
                failed = _failure(step.lane, "error", "unexpected error creating the lane; see the node log")
                break
            try:
                await self.store.run(self._record, step, plan)
                if reasons:
                    await self.store.run(self.store.add_override, at=self._now(), by=by, lane=step.lane,
                                         proxy=step.proxy, rules=reasons, reason=plan.reason or "")
            except OpenajarError as exc:
                created.append(step.lane)  # the lane is real even though its record is not
                failed = _failure(step.lane, exc.code, f"created, but not recorded ({exc.message}); "
                                                       "run apply again to finish")
                break
            created.append(step.lane)
            warnings.extend(step.warnings)
            if reasons:
                overridden.append({"lane": step.lane, "rules": reasons})
        done = set(created) | set(existing)
        return ApplyResult(status="complete" if failed is None else "partial", created=created,
                           existing=existing, overridden=overridden, failed=failed,
                           remaining=[s.lane for s in plan.steps if s.lane not in done], warnings=warnings)

    def _problems_now(self, step: PlanStep) -> list[str]:
        info = self._lane_infos().get(step.lane)
        if info is None:
            return ["the lane is no longer there"]
        current = self._facts({step.lane: info}, self.store.lane_rows())[step.lane]
        return existing_problems(proxy=step.proxy, platform=step.platform, current=current)

    def _adopt(self, step: PlanStep, plan: Plan) -> None:
        self.store.adopt_lane({"id": step.lane, "workspace": step.workspace, "surface": step.surface,
                               "platform": step.platform, "template": plan.template,
                               "home_url": step.home_url, "login_url": step.login_url, "created_at": self._now()})

    def _record(self, step: PlanStep, plan: Plan) -> None:
        self.store.record_lane({"id": step.lane, "workspace": step.workspace, "surface": step.surface,
                                "platform": step.platform, "template": plan.template,
                                "home_url": step.home_url, "login_url": step.login_url,
                                "created_at": self._now()})

    def overrides(self, limit: int = 50) -> list[OverrideRecord]:
        return [OverrideRecord(**row) for row in self.store.list_overrides(limit)]


def _proxy_labels(home: Path) -> Callable[[], set[str]]:
    """Labels only: the proxy file also holds credentials, which this package never reads out."""
    def labels() -> set[str]:
        try:
            return set(load_proxies(NodeConfig(home=home)))
        except ValueError as exc:
            raise ServiceUnavailable(str(exc)) from None
    return labels


def build(runtime: Runtime, *, home: Path | None = None, labels: Callable[[], set[str]] | None = None,
          clock: Callable[[], datetime] = utcnow, register: bool = True) -> WorkspaceService:
    """Open the workspace store under `home` (default: the runtime's config, then OPENAJAR_HOME)."""
    cfg = runtime.get("config") if runtime is not None else None
    home = Path(home) if home is not None else (cfg.home if cfg is not None else home_from_env())
    if labels is None:
        labels = _proxy_labels(home)
    store = WorkspaceStore(home / DB_NAME)
    service = WorkspaceService(store, runtime, labels=labels, templates_dir=home / "templates", clock=clock)
    if register and runtime is not None:
        runtime.register(SERVICE_NAME, service)
    return service
