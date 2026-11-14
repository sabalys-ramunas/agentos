"""Adapters from the real runtime services to what the panel API expects (agentos.api.services).

The feature packages register their services under the well-known names the API looks up
(approvals, traces, runner, flows, brains, vault, policy, notifier, envs), with their own
method shapes. `Bridge.adapt(name, service)` wraps a real one in a thin adapter with the
Protocol's methods and the shapes in docs/API.md; anything that already speaks the Protocol
(the fakes in agentos.api.fakes) passes through untouched. Services are recognized by the
methods they have, so this module imports no feature package.

`Bridge.wire_events(bus)` forwards the services' own change feeds into the panel's event bus:
approvals and traces from the policy bus, run status from the runner, lane control changes
from the lane manager. Lane state changes have no feed, so `pump_lanes` polls for them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from agentos.api.actors import actor, decided_by
from agentos.api.jsonable import jsonable, maybe_await
from agentos.core.errors import AgentosError, InvalidRequest, NotFound, PolicyDenied
from agentos.core.models import LaneInfo, LaneKind, LaneSpec, LaneState, Principal

log = logging.getLogger("agentos.api")

TTL_RE = re.compile(r"^([1-9][0-9]{0,4})([mhdw])$")
TTL_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
APPROVAL_STATES = {"consumed": "approved"}  # the panel's four states; a used approval was approved


def ttl_seconds(ttl: str) -> int:
    m = TTL_RE.match(ttl or "")
    if not m:
        raise InvalidRequest("ttl looks like 30m, 12h, 7d or 4w")
    return int(m.group(1)) * TTL_UNITS[m.group(2)]


def _by(principal: Principal | None) -> str:
    return f"{principal.kind}:{principal.id}" if principal is not None else "panel"


def _via(principal: Principal | None, refuse_family: str | None) -> str:
    """How a panel caller with no member grant proved it. A bearer token always names the family it
    may not decide for; the node token (principal kind "node") is the owner's authority, and an agent
    may hold it, so this is what says no person was there."""
    if refuse_family is None:
        return "session"
    return "owner_token" if principal is not None and principal.kind in ("node", "human") else "token"


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


# ---------------------------------------------------------------- approvals

def approval_json(public: dict) -> dict:
    """An approval as docs/API.md shows it (requested_by without grant ids). A card's images get
    the route the panel fetches them from."""
    out = dict(public)
    out["state"] = APPROVAL_STATES.get(out.get("state"), out.get("state"))
    if out.get("decided_by") is not None or out.get("decided_via") is not None:  # one wording (api.actors)
        out["decided_who"], out["decided_how"] = actor(out.get("decided_by"), out.get("decided_via"))
        out["decided_by"] = decided_by(out.get("decided_by"))
    rb = out.get("requested_by") or {}
    out["requested_by"] = {k: rb.get(k) for k in ("kind", "id", "thread")}
    card = out.get("card")
    if isinstance(card, dict) and card.get("images"):
        card = {**card, "images": [{**img, "url": f"/api/approvals/{out.get('id')}/files/{img.get('name')}"}
                                   for img in card["images"] if isinstance(img, dict)]}
        out["card"] = card
    return out


def standing_json(public: dict, *, state: str | None = None) -> dict:
    """A standing approval as the panel shows it. `lanes` and `lane` are the old read (every account it
    names, workspace patterns included); `workspaces`, `lanes_only` (null: every account in the
    workspace), `count`, `used`, `per_day` and `contents` are what a person set and what it has done."""
    lanes = list(public.get("lanes") or []) + [f"{w}/*" for w in public.get("workspaces") or []]
    effects = public.get("effects") or []
    return {"id": public.get("id"), "lane": lanes[0] if lanes else None, "lanes": lanes,
            "lanes_only": public.get("lanes") or None, "workspaces": public.get("workspaces") or [],
            "effect": effects[0] if effects else None, "effects": effects, "expires_at": public.get("expires_at"),
            "starts_at": public.get("starts_at"), "created_at": public.get("created_at"),
            "revoked_at": public.get("revoked_at"), "state": state,
            "remaining": public.get("remaining"), "count": public.get("max_uses"), "used": public.get("uses"),
            "per_day": public.get("per_day"), "contents": len(public.get("contents") or []),
            "covers_publishing": bool(public.get("covers_publishing")),
            "note": public.get("note"), "created_by": public.get("created_by"),
            "targets": public.get("targets") or [], "shape": bool(public.get("shape")),
            "issuer": public.get("issuer")}


QUESTION_STATES = {"open": "pending", "answered": "approved", "declined": "denied", "expired": "expired"}
_QUESTION_NAMES = {store_state: name for name, store_state in QUESTION_STATES.items()}


def question_json(approval: dict) -> dict:
    """A question as the panel and the dashboard show it. A question is an approval of kind
    "question" underneath: `open` until someone answers it, then `answered`, `declined` or `expired`."""
    asked_by = approval.get("requested_by") or {}
    return {"id": approval.get("id"), "state": _QUESTION_NAMES.get(approval.get("state"), approval.get("state")),
            "lane": None, "question": approval.get("summary") or "", "options": approval.get("options") or [],
            "answer": approval.get("answer"), "asked_by": {k: asked_by.get(k) for k in ("kind", "id", "thread")},
            "created_at": approval.get("created_at"), "expires_at": approval.get("expires_at"),
            "answered_at": approval.get("decided_at"), "answered_by": approval.get("decided_by")}


class ApprovalsAdapter:
    """agentos.policy.approvals.ApprovalStore as the panel's ApprovalService."""

    def __init__(self, store: Any):
        self.store = store

    def _visible(self, ap: Any, viewer: str | None, viewer_grants: list[Any] | None = None) -> bool:
        if viewer_grants is not None:
            return any(self.store.visible_to(ap, g) for g in viewer_grants)
        return viewer is None or (ap.grant_path is not None and f"/{viewer}/" in ap.grant_path)

    def list(self, *, state: str | None = None, limit: int = 50, viewer: str | None = None,
             kind: str | None = None, viewer_grants: list[Any] | None = None) -> list[dict]:
        """`viewer` (a grant id) keeps that grant family's approvals; `viewer_grants` (a member's
        grants) the approvals on the lanes any of them covers (ApprovalStore.visible_to)."""
        if viewer_grants is not None:
            seen: dict[str, Any] = {}
            for g in viewer_grants:
                for st in (["approved", "consumed"] if state == "approved" else [state]):
                    for ap in self.store.list(state=st, limit=limit, kind=kind, viewer_grant=g):
                        seen.setdefault(ap.id, ap)
            items = sorted(seen.values(), key=lambda a: a.created_at, reverse=True)[:limit]
        elif state == "approved":
            items = self.store.list(state="approved", limit=limit, viewer=viewer, kind=kind) + \
                self.store.list(state="consumed", limit=limit, viewer=viewer, kind=kind)
            items.sort(key=lambda a: a.created_at, reverse=True)
            items = items[:limit]
        else:
            items = self.store.list(state=state, limit=limit, viewer=viewer, kind=kind)
        return [approval_json(a.public()) for a in items]

    def get(self, approval_id: str, *, viewer: str | None = None, viewer_grants: list[Any] | None = None) -> dict:
        ap = self.store.get(approval_id)
        if not self._visible(ap, viewer, viewer_grants):
            raise NotFound(f"No approval {approval_id}.")
        return approval_json(ap.public())

    def decide(self, approval_id: str, *, decision: str, principal: Principal, note: str | None = None,
               refuse_family: str | None = None, answer: str | None = None, decider: Any = None,
               by: str | None = None, via: str | None = None, edit: dict | None = None,
               proof: Any = None, seen: str | None = None) -> dict:
        """`refuse_family`: the root grant id of a family whose requests this caller may not decide
        (a token never approves what its grant, or one narrowed from it, asked for). `answer` is a
        question's reply (ApprovalStore.decide, the same call the policy's /answer route makes).
        `decider`: a member's grant, deciding in person; `by` their handle for Activity and `via`
        how they signed in (the store checks the grant: not_approver, four_eyes, revoked). `proof`:
        how strong this decision is, from agentos.api.app.decision_proof, never from the request."""
        if refuse_family is not None:
            ap = self.store.get(approval_id)
            if ap.grant_path and f"/{refuse_family}/" in ap.grant_path:
                raise PolicyDenied("self_approval: a token cannot decide what its own grant asked for; "
                                   "decide it in a signed-in panel session")
        extra: dict[str, Any] = {"edit": edit} if edit is not None else {}
        if proof is not None:
            extra["proof"] = proof
        if seen is not None:  # a cut send card: the token its last page of messages gave
            extra["seen"] = seen
        if decider is not None:
            return approval_json(self.store.decide(approval_id, decision, by=by, note=note, answer=answer,
                                                   decider=decider, via=via, in_person=True, **extra).public())
        return approval_json(self.store.decide(approval_id, decision, by=_by(principal), note=note, answer=answer,
                                               via=via or _via(principal, refuse_family), **extra).public())

    def messages(self, approval_id: str, page: int = 0, cursor: str | None = None) -> dict:
        """One page of a send card's messages (ApprovalStore.send_messages). The caller has checked
        the approval is visible."""
        return self.store.send_messages(approval_id, page, cursor)

    def file(self, approval_id: str, param: str) -> tuple[bytes, str]:
        """(bytes, mime) of an image a card shows: the inbox file the approval binds for `param`,
        read only while its bytes still match the digest the approval binds (connectors) or the
        file the flow names (flows). The caller has checked the approval is visible."""
        ap = self.store.get(approval_id)
        files = getattr(self.store, "card_files", None)
        if not callable(files):
            raise NotFound("This node cannot read approval images.")
        return files(ap, param)

    def decide_by_link(self, approval_id: str, decision: str, exp: str, sig: str) -> dict:
        ap = self.store.decide_by_link(approval_id, decision, exp, sig)
        return {"id": ap.id, "state": APPROVAL_STATES.get(ap.state, ap.state)}

    def clear_outcome(self, approval_id: str, *, principal: Principal | None = None) -> dict:
        """"Got it" on an approved action whose run did not work: it leaves the failed list."""
        ap = self.store.clear_outcome(approval_id, by=_by(principal))
        return approval_json(ap.public())

    def cleared_outcomes(self) -> dict[str, dict]:
        return self.store.cleared_outcomes()

    def review(self, approval_id: str, exp: str, sig: str) -> dict:
        """A push's Review link opened in the panel: the approval it names, and nothing decided."""
        return approval_json(self.store.check_review_link(approval_id, exp, sig).public())

    def list_standing(self, *, all_states: bool = False) -> list[dict]:
        """The standing approvals that still do something, or every one (`all_states`): used up,
        expired, revoked and withdrawn ones too."""
        now = self.store.clock()
        found = self.store.list_standing(active_only=not all_states)
        return [standing_json(s.public(), state=s.state(now)) for s in found]

    def create_standing(self, *, effect: str, ttl_seconds: int, principal: Principal, lane: str | None = None,
                        lanes: list[str] | None = None, workspace: str | None = None,
                        note: str | None = None, from_approvals: list[str] | None = None,
                        shape: bool = False, count: int | None = None, per_day: int | None = None,
                        targets: list[str] | None = None, decider: list[Any] | None = None) -> dict:
        """One account (`lane`), some accounts of one workspace (`lanes`), or every account in it
        (`workspace` alone). `count` is how many times it may cover, `per_day` how many a day.
        `shape` ("always allow this shape"): the flow versions or connector operations of
        `from_approvals` on the lane, whatever they post, until it expires; never their contents.
        `from_approvals` must be approved already, and for the accounts it covers; `decider` (a member's
        grants) must be able to approve each one (ApprovalStore.binding_from)."""
        until = self.store.clock() + int(ttl_seconds)
        scope_lanes = [lane] if lane else (list(lanes) if lanes else None)
        scope_workspaces = None if scope_lanes else ([workspace] if workspace else None)
        named: list[str] = list(targets or [])
        contents: list[str] = []
        if from_approvals and callable(getattr(self.store, "binding_from", None)):
            bound, contents, bound_lanes = self.store.binding_from(list(from_approvals), lane=lane, deciders=decider)
            named = sorted({*named, *bound})
            if scope_lanes is not None and not set(bound_lanes) <= set(scope_lanes):
                # "the same again" never moves to another account
                raise InvalidRequest(f"the approvals it comes from are for {', '.join(bound_lanes)}, not these accounts")
            if shape and scope_lanes is not None and bound_lanes != scope_lanes:
                raise InvalidRequest(f"always allow covers the lane the approval was for ({', '.join(bound_lanes)})")
        if shape:
            if not from_approvals:
                raise InvalidRequest("always allow this shape needs the approval it comes from (from_approvals)")
            contents = []
        problem = getattr(self.store, "binding_problem", lambda *_: None)([effect], named)
        if problem:
            raise InvalidRequest(problem)
        extra = {"shape": True} if shape else {}
        sa = self.store.add_standing(effects=[effect], workspaces=scope_workspaces, lanes=scope_lanes, until=until,
                                     count=count, per_day=per_day, by=_by(principal), note=note,
                                     targets=named or None, contents=contents or None, **extra)
        return standing_json(sa.public(), state=sa.state(self.store.clock()))

    def revoke_standing(self, standing_id: str, *, principal: Principal | None = None) -> dict:
        sa = self.store.revoke_standing(standing_id, by=_by(principal))
        return standing_json(sa.public(), state=sa.state(self.store.clock()))


# ---------------------------------------------------------------- traces

class TracesAdapter:
    """agentos.policy.traces.TraceStore as the panel's TraceService."""

    def __init__(self, store: Any):
        self.store = store

    def query(self, *, lane: str | None = None, workspace: str | None = None, kind: str | None = None,
              before: str | None = None, limit: int = 100, viewer: str | None = None,
              viewer_workspaces: list[str] | None = None) -> list[dict]:
        """`viewer` keeps one grant family's events; `viewer_workspaces` (a member's) every event
        in those workspaces, whoever acted."""
        if viewer_workspaces is not None:
            wanted = [workspace] if workspace else viewer_workspaces
            events = []
            for ws in wanted:
                if ws in viewer_workspaces:
                    events += self.store.query(lane=lane, workspace=ws, kind=kind, before=before, limit=limit)[0]
            events.sort(key=lambda e: (e.at or 0, e.id), reverse=True)
            return [e.public() for e in events[:limit]]
        events, _ = self.store.query(viewer_path=viewer, lane=lane, workspace=workspace, kind=kind,
                                     before=before, limit=limit)
        return [e.public() for e in events]

    def cost_today(self) -> dict:
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        cost = self.store.cost_since(start)
        return {"tokens": int(cost.tokens), "usd": round(float(cost.usd), 4)}


# ---------------------------------------------------------------- runner and flows

def run_json(described: dict) -> dict:
    """A run as docs/API.md shows it: honest status, cost, proof and steps."""
    d = dict(described)
    cost = d.get("cost") or {}
    d["cost"] = {"tokens": int(cost.get("tokens", 0) or 0), "usd": float(cost.get("usd", 0.0) or 0.0)}
    proof = d.get("proof") or None
    if proof:
        shot = proof.get("screenshot")
        d["proof"] = {"url": proof.get("url"), "id": proof.get("id"),
                      # a file path on the node is not something a browser can load
                      "screenshot": shot if isinstance(shot, str) and shot.startswith("/api/") else None,
                      "readbacks": proof.get("readbacks")}
    if "steps" in d:
        d["steps"] = [{"at": s.get("finished_at") or s.get("started_at"),
                       "summary": " ".join(str(p) for p in (s.get("type"), s.get("path")) if p),
                       "status": s.get("status"), **{k: s[k] for k in ("readback", "error") if k in s}}
                      for s in d["steps"]]
    d["detail"] = d.get("summary")
    d.pop("params", None)  # flow parameters may carry anything the caller typed
    return d


def schedule_json(described: dict, *, next_fire: str | None = None, last_run: dict | None = None) -> dict:
    """A schedule as the panel and the dashboard show it. `next_fire_at` is in the node's timezone;
    `grant_id` is the grant the schedule's runs go under (a grant sees its own schedules only)."""
    d = dict(described)
    cron, watch = d.get("cron"), d.get("watch")
    return {"id": d.get("id"), "flow": d.get("flow_id"), "kind": d.get("kind"), "lane": d.get("lane"),
            "cron": cron.get("cron") if isinstance(cron, dict) else cron,
            "watch": json.dumps(watch, sort_keys=True) if isinstance(watch, dict) else watch,
            "params": d.get("params") or {}, "deadman_minutes": d.get("deadman_minutes"),
            "enabled": d.get("enabled"), "next_run": d.get("next_run"), "next_fire_at": next_fire,
            "last_run": last_run, "grant_id": d.get("grant_id"),
            "webhook": d.get("webhook") if isinstance(d.get("webhook"), dict) else None}


class RunnerAdapter:
    """agentos.runner.RunnerService (and its scheduler) as the panel's RunnerService."""

    def __init__(self, runner: Any, *, zone: ZoneInfo | None = None):
        self.runner = runner
        self.zone = zone or UTC

    def list_runs(self, *, status: str | None = None, limit: int = 50, flow: str | None = None) -> list[dict]:
        from agentos.core.models import RunStatus

        runs = self.runner.list(status=[RunStatus(status)] if status else None, limit=limit, flow_id=flow)
        return [run_json(self.runner.describe(r)) for r in runs]

    def get_run(self, run_id: str) -> dict:
        return run_json(self.runner.describe(self.runner.get(run_id), steps=True))

    def _scheduler(self) -> Any:
        sch = getattr(self.runner, "scheduler", None)
        if sch is None:
            from agentos.core.errors import ServiceUnavailable
            raise ServiceUnavailable("Schedules are not configured on this node.")
        return sch

    def _schedule(self, s: Any) -> dict:
        next_fire = None
        if s.enabled and s.cron is not None:  # a cron that is on fires at its next slot
            next_fire = self._scheduler().next_slot(s).astimezone(self.zone).isoformat()
        return schedule_json(self._scheduler().describe(s), next_fire=next_fire, last_run=self._last_run(s))

    def _last_run(self, s: Any) -> dict | None:
        run_id = s.state.get("last_run_id")
        if not run_id:
            return None
        try:
            run = self.runner.get(run_id)
        except AgentosError:  # pruned or never stored: the schedule simply has no last run to show
            return None
        return run_json(self.runner.describe(run))

    def list_schedules(self) -> list[dict]:
        return [self._schedule(s) for s in self._scheduler().list()]

    def get_schedule(self, schedule_id: str) -> dict:
        return self._schedule(self._scheduler().get(schedule_id))

    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> dict:
        return self._schedule(self._scheduler().set_enabled(schedule_id, enabled))

    def create_schedule(self, *, flow: str, cron: str | None, watch: str | None, params: dict,
                        deadman_minutes: int | None, principal: Principal) -> dict:
        watch_cfg = None
        if watch:
            try:
                watch_cfg = json.loads(watch)
            except ValueError:
                raise InvalidRequest('watch is a JSON object like {"url": ..., "locator": ...}') from None
            if not isinstance(watch_cfg, dict):
                raise InvalidRequest('watch is a JSON object like {"url": ..., "locator": ...}')
        sch = self._scheduler()
        s, _ = sch.add(flow=flow, cron=cron or None, watch=watch_cfg, params=params,
                            deadman_minutes=deadman_minutes, principal=principal)
        return self._schedule(s)

    def schedule_runs(self, schedule_id: str, *, limit: int = 20) -> list[dict]:
        """The runs a schedule started, newest first: its fires that ran (none for one that never fired)."""
        return [run_json(self.runner.describe(run)) for run in self.runner.list(schedule_id=schedule_id, limit=limit)]

    def delete_schedule(self, schedule_id: str) -> None:
        if not self._scheduler().remove(schedule_id):
            raise NotFound(f"No schedule {schedule_id}.")


def _ask_first_view(flows: Any, flow_id: str) -> dict:
    """`{ ask_first, ask_first_by }` when the flow service knows them (agentos.policy.ask_first)."""
    view = getattr(flows, "ask_first_view", None)
    return view(flow_id) if callable(view) else {}


class FlowsAdapter:
    """agentos.runner.FlowService (plus the runner, to submit) as the panel's FlowService."""

    def __init__(self, flows: Any, runner: Any | None):
        self.flows = flows
        self.runner = runner

    def _last_run(self, flow_id: str) -> dict | None:
        if self.runner is None:
            return None
        runs = self.runner.list(flow_id=flow_id, limit=1)
        return run_json(self.runner.describe(runs[0])) if runs else None

    def list(self) -> list[dict]:
        out = []
        for fv in self.flows.list():
            d = fv.definition
            out.append({"id": d.id, "lane": d.lane, "version": fv.version, "title": d.title,
                        "description": d.description or d.title or "", "params": d.params,
                        "effect": str(fv.effect), "last_run": self._last_run(d.id),
                        **_ask_first_view(self.flows, d.id)})
        return out

    def plan(self, flow_id: str, *, params: dict, on: str | None = None) -> dict:
        """What running this flow would do, checked but not queued: lane, effect, params and the
        node-written summary a human would approve. For a connector operation `lane` is the
        surface `on` (what grants and approvals check; the run binds to its lane) and the content
        is the operation's approval content (agentos.runner.connectors.surfaces)."""
        fv = self.flows.get(flow_id)
        if fv.definition.connector and self.runner is not None and getattr(self.runner, "surfaces", None):
            target = self.runner.connector_target(fv, on, params)
            summary, content = self.runner.surfaces.approval(fv, target)
            return {"flow": fv.id, "version": fv.version, "lane": target.surface.id, "acting_lane": target.lane, "effect": str(fv.effect),
                    "params": target.caller_params, "summary": summary, "content": content}
        lane = self.flows.resolve_lane(fv, on)
        params = self.flows.validate_params(fv, params)
        # the effect its steps really have when it is above the declared one (FlowVersion.effect), as
        # the runner and the tool list read it: a flow declared read that types is a commit
        return {"flow": fv.id, "version": fv.version, "lane": lane, "effect": str(fv.effect),
                "params": params, "summary": self.flows.run_summary(fv, lane, params),
                "content": self.flows.approval_content(fv, lane, params)}

    def run(self, flow_id: str, *, params: dict, principal: Principal, on: str | None = None,
            authorized: bool = False, approval_id: str | None = None, pace_id: int | None = None,
            version: str | None = None, auto: bool = False) -> dict:
        """`version`: the one `plan` checked, so a version saved in between never runs under its yes.
        `auto`: policy let it through on its own, so the runner reads Ask me first again before it runs."""
        if self.runner is None:
            from agentos.core.errors import ServiceUnavailable
            raise ServiceUnavailable("The runner is not configured on this node.")
        run, _ = self.runner.submit(flow_id=flow_id, version=version, params=params, lane=on, principal=principal,
                                    source="panel", authorized=authorized, approval_id=approval_id, pace_id=pace_id,
                                    auto=auto)
        return run_json(self.runner.describe(run))


# ---------------------------------------------------------------- brains

BRAIN_FIELDS = ("name", "kind", "provider", "runner", "model", "state", "state_reason", "seats", "window_used_pct",
                "order", "enabled", "locality")
TASK_FIELDS = ("id", "workspace", "status", "status_reason", "brain", "attempt", "tokens", "turns", "cost_usd",
               "prompt_preview", "created_at", "ended_at", "resumable")


def task_json(task: dict) -> dict:
    """A brain task with the names the dashboard reads too: `started_at` and `finished_at`, the prompt
    preview as `summary`, and `lane` (the lanes a task uses are not in its list entry, so none)."""
    return {**task, "started_at": task.get("created_at"), "finished_at": task.get("ended_at"),
            "summary": task.get("prompt_preview") or "", "lane": task.get("lane")}


class BrainsAdapter:
    """agentos.brains.BrainService as the panel's BrainService (no credential, no prompt body)."""

    def __init__(self, brains: Any):
        self.brains = brains

    async def list(self) -> list[dict]:
        return [{k: b.get(k) for k in BRAIN_FIELDS} for b in await self.brains.list_brains()]

    async def status(self) -> dict:
        chip = await self.brains.chip()
        return {k: chip.get(k) for k in ("name", "kind", "state", "offline_ready", "local_ready")}

    def set_order(self, order: list[str]) -> list[str]:
        return self.brains.set_order(order)

    def list_tasks(self, *, limit: int = 50) -> list[dict]:
        return [task_json({k: t.get(k) for k in TASK_FIELDS}) for t in self.brains.list_tasks(limit=limit)]

    def get_task(self, task_id: str) -> dict:
        st = self.brains.status(task_id)
        tail = self.brains.tail(task_id, limit=50)
        out = task_json({k: st.get(k) for k in TASK_FIELDS})
        out["attempts"] = st.get("attempts")
        out["transcript_tail"] = [str(ev.get("text") or "") for ev in tail.get("events", []) if ev.get("text")]
        out["tail"] = out["transcript_tail"]  # the dashboard's name for the same lines
        return out


# ---------------------------------------------------------------- vault, policy, notifier

class VaultAdapter:
    """agentos.vault.VaultService as the panel's VaultService: names, domains and lanes only."""

    def __init__(self, vault: Any):
        self.vault = vault

    async def list(self) -> list[dict]:
        return [e.public() for e in await self.vault.list_entries()]

    async def create(self, *, name: str, kind: str, domain: str, lanes: list[str], username: str | None,
                     secret: str, principal: Principal, totp: str | None = None) -> dict:
        meta = await self.vault.add_entry(name=name, kind=kind, domain=domain, lanes=lanes, username=username,
                                          secret=secret, totp=totp)
        return meta.public()

    async def delete(self, entry_id: str) -> None:
        await self.vault.remove_entry(entry_id)

    # code cards (agentos.vault.relay): request metadata out, the code in once, never back
    def codes(self) -> list[dict]:
        return [r.public() for r in self.vault.relay.list()]

    def submit_code(self, request_id: str, *, code: Any, by: str, family: str | None) -> dict:
        return self.vault.relay.submit(request_id, code, by=by, family=family).public()

    def decline_code(self, request_id: str, *, by: str, family: str | None) -> dict:
        return self.vault.relay.decline(request_id, by=by, family=family).public()


# What a special the panel grants by name carries; every other one is granted unnarrowed ({}).
PANEL_SPECIAL_CAPS: dict[str, dict] = {"memory": {"write": True}}   # memory notes, no private or owner-only
# Names the API takes for a special a grant field carries narrowed: memory_read is the memory special
# without write (memory_context, search and get), what `agentos connect` gives an agent by default.
PANEL_SPECIAL_ALIASES: dict[str, tuple[str, dict]] = {"memory_read": ("memory", {})}
# Names that stand for several specials at once. environments: rent this chat's cloud computer
# (agentos.environments) and run commands on it, and only there: lanes env/*, never an account lane.
ENVIRONMENT_LANES = ["env/*"]
PANEL_SPECIAL_BUNDLES: dict[str, dict[str, dict]] = {
    "environments": {"vm": {"lanes": ENVIRONMENT_LANES}, "exec": {"lanes": ENVIRONMENT_LANES}},
}


def panel_specials(names: list[str]) -> dict[str, dict]:
    """The Specials block for the names the API took; memory wins over memory_read when both are asked,
    and a special asked for by its own name (unnarrowed) wins over the narrower one a bundle carries."""
    out: dict[str, dict] = {}
    exact: set[str] = set()
    for name in names:
        if name in PANEL_SPECIAL_BUNDLES:
            for field, caps in PANEL_SPECIAL_BUNDLES[name].items():
                if field not in exact:
                    out[field] = {k: list(v) if isinstance(v, list) else v for k, v in caps.items()}
            continue
        field, caps = PANEL_SPECIAL_ALIASES.get(name, (name, PANEL_SPECIAL_CAPS.get(name, {})))
        if field not in out or name == field:
            out[field] = dict(caps)
            if name == field:
                exact.add(field)
    return out


def grant_json(record: dict) -> dict:
    """A grant as docs/API.md shows it, from agentos.policy's record (no token, ever)."""
    scope = record.get("scope") or {}
    blocks = scope.get("blocks") or [{}]
    first = blocks[0]
    subject = record.get("subject") or {}
    return {"id": record.get("id"), "subject": subject.get("id"), "subject_kind": subject.get("kind"),
            "subject_owner": subject.get("owner"),
            "workspaces": first.get("workspaces") or [], "lanes": first.get("lanes"),
            "ceiling": scope.get("ceiling"), "specials": scope.get("specials") or [], "admin": scope.get("admin"),
            "budget": first.get("budget"), "remaining": record.get("budget"), "state": record.get("state"),
            "revoked": bool(record.get("revoked")), "issued_at": record.get("issued_at"),
            "expires_at": record.get("expires_at"), "parent_id": record.get("parent_id"),
            "depth": record.get("depth"), "issued_by": record.get("issued_by")}


class PolicyAdapter:
    """agentos.policy.PolicyService as the panel's PolicyService."""

    def __init__(self, policy: Any):
        self.policy = policy

    def authenticate_panel(self, token: str) -> Principal | None:
        return self.policy.authenticate_panel(token)

    def list_grants(self) -> list[dict]:
        explain = getattr(self.policy, "explain_grant", None)  # plain sentences (agentos grants explain)
        return [{**grant_json(r), **({"explain": explain(r)} if callable(explain) else {})}
                for r in self.policy.list_grants(include_inactive=True)]

    def issue_grant(self, *, subject: str, workspaces: list[str], lanes: list[str] | None, ceiling: str,
                    specials: list[str], budget: dict | None, ttl: str, principal: Principal,
                    owner: str | None = None, within: Any = None) -> dict:
        """`owner`: the member the agent belongs to (its sub.owner), when a member connects their own.
        `within`: the asking caller's grant (an admin grant): the new one is never wider than it."""
        g, token = self.policy.issue(subject={"kind": "agent", "id": subject}, workspaces=workspaces, lanes=lanes,
                                     ceiling=ceiling, specials=panel_specials(specials), budget=budget,
                                     ttl_s=ttl_seconds(ttl), by=_by(principal), owner=owner, within=within)
        return {**grant_json(self.policy.grant_record(g.id)), "token": token}

    def device_ceiling(self) -> Any:
        """What a grant the owner's device credential issues must fit inside (policy.device_grants)."""
        return self.policy.device_ceiling()

    def revoke_grant(self, grant_id: str, *, principal: Principal | None = None) -> dict:
        return self.policy.revoke(grant_id, by=_by(principal))


class NotifierAdapter:
    """agentos.policy.notifier.Notifier as the panel's NotifierService (no token, no secret)."""

    def __init__(self, notifier: Any):
        self.notifier = notifier

    @staticmethod
    def _shown(public: dict) -> dict:
        # "has_token" and "has_secret" would be dropped by the API's sensitive-key filter
        out = dict(public)
        for section, flag, name in (("ntfy", "has_token", "token_saved"), ("webhook", "has_secret", "secret_saved")):
            if isinstance(out.get(section), dict):
                out[section] = {(name if k == flag else k): v for k, v in out[section].items()}
        return out

    def get_settings(self) -> dict:
        return self._shown(self.notifier.config.public())

    def put_settings(self, settings: dict) -> dict:
        return self._shown(self.notifier.update_config(settings).public())

    async def test(self) -> dict:
        results = await self.notifier.test()
        if not results:
            return {"ok": False, "detail": "No notification channel is enabled."}
        ok = all(v == "delivered" for v in results.values())
        return {"ok": ok, "detail": "; ".join(f"{ch}: {st}" for ch, st in sorted(results.items())),
                "results": results}


# ---------------------------------------------------------------- env (terminal and service lanes)

class TerminalAdapter:
    """An agentos.env PTY session as the panel's TerminalSession; closing it ends the shell."""

    def __init__(self, envs: Any, session: Any):
        self.envs = envs
        self.session = session
        self.feed = session.subscribe(replay=True)

    async def read(self) -> bytes | None:
        return await self.feed.get()

    async def write(self, data: str) -> None:
        if self.session.running:
            await self.session.write(data.encode())

    async def resize(self, cols: int, rows: int) -> None:
        self.session.resize(cols, rows)

    async def close(self) -> None:
        self.session.unsubscribe(self.feed)
        await self.envs.shell_close(self.session.id)


class EnvsAdapter:
    """agentos.env.EnvManager as the panel's EnvService, plus its lanes in the lane list."""

    def __init__(self, envs: Any):
        self.envs = envs

    def has(self, lane_id: str) -> bool:
        try:
            return bool(self.envs.has(lane_id))
        except Exception:  # noqa: BLE001
            return False

    def lane_infos(self) -> list[tuple[LaneInfo, dict]]:
        """Env lanes as core LaneInfo plus hints (preset), for /api/lanes and /api/workspaces."""
        out = []
        for info in self.envs.list():
            lane = info.lane
            spec = LaneSpec(id=lane.id, kind=LaneKind(getattr(lane.kind, "value", lane.kind)), engine="env")
            state = LaneState(getattr(info.state, "value", info.state))
            out.append((LaneInfo(spec=spec, state=state, last_used=info.last_used,
                                 quarantine_reason=info.quarantine_reason),
                        {"preset": getattr(lane.preset, "value", lane.preset), "signal": "unknown",
                         "logged_in_hint": "unknown", "isolation": info.isolation}))
        return out

    def lane_info(self, lane_id: str) -> tuple[LaneInfo, dict]:
        for info, hints in self.lane_infos():
            if info.spec.id == lane_id:
                return info, hints
        from agentos.core.errors import LaneNotFound
        raise LaneNotFound(f"No lane {lane_id}.")

    async def open(self, lane_id: str) -> None:
        await self.envs.ensure_started()
        await self.envs.warm(lane_id)

    async def close(self, lane_id: str) -> None:
        await self.envs.cool(lane_id)

    def terminal_facts(self, lane_id: str) -> dict:
        lane = self.envs.get(lane_id)
        facts = {"preset": getattr(lane.preset, "value", lane.preset), "isolated": bool(getattr(lane, "isolated", False))}
        if hasattr(lane, "contained"):  # a lane that reaches the node: a person, every time (agentos.env.exposure)
            from agentos.env import exposure
            facts.update(exposure.facts(lane))
        return facts

    async def open_terminal(self, lane_id: str, *, principal: Principal, cols: int = 80, rows: int = 24):
        await self.envs.ensure_started()
        session = await self.envs.shell_open(lane_id, cols=cols, rows=rows, term="xterm-256color")
        return TerminalAdapter(self.envs, session)


# ---------------------------------------------------------------- recognizing real services


class WorkspacesAdapter:
    """agentos.workspaces in the panel's shapes. Template apply goes through the workspaces
    service, so its proxy rules (one proxy per business, no two accounts of a platform on one
    IP) hold for the panel exactly as for the CLI and the tools."""

    def __init__(self, svc: Any):
        self.svc = svc

    def list(self, *, viewer: str | None = None) -> list[dict]:
        return [{"id": w.id, "name": w.name, "color": w.color} for w in self.svc.list_workspaces()]

    def create(self, *, id: str, name: str, color: str | None) -> dict:
        w = self.svc.create_workspace(id, name=name, color=color)
        return {"id": w.id, "name": w.name, "color": w.color}

    def templates(self) -> list[dict]:
        found, _problems = self.svc.templates()
        return [{"id": t.id, "name": t.title, "description": t.description,
                 "lanes": [{"name": s.name, "platform": s.platform, "kind": "browser", "preset": "account"}
                           for s in t.surfaces]}
                for t in found]

    async def apply(self, *, template: str, workspaces: list[str], proxy_per_workspace: dict[str, str],
                    by: str) -> dict:
        result = await self.svc.apply(template, workspaces, by=by, proxy_per_workspace=proxy_per_workspace)
        return result.model_dump(mode="json")

ADAPTERS: dict[str, tuple[tuple[str, ...], Callable[..., Any]]] = {
    # name: (methods that mark the real service, adapter factory)
    "approvals": (("consume_approved", "add_standing"), lambda svc, b: ApprovalsAdapter(svc)),
    "traces": (("write", "cost_since"), lambda svc, b: TracesAdapter(svc)),
    "runner": (("describe", "submit"), lambda svc, b: RunnerAdapter(svc, zone=b.zone)),
    "flows": (("validate_params", "resolve_lane"), lambda svc, b: FlowsAdapter(svc, b.runtime.get("runner"))),
    "brains": (("list_brains", "chip"), lambda svc, b: BrainsAdapter(svc)),
    "vault": (("list_entries", "add_entry"), lambda svc, b: VaultAdapter(svc)),
    "policy": (("grant_record", "issue"), lambda svc, b: PolicyAdapter(svc)),
    "notifier": (("update_config", "enabled_channels"), lambda svc, b: NotifierAdapter(svc)),
    "workspaces": (("list_workspaces", "plan"), lambda svc, b: WorkspacesAdapter(svc)),
    "envs": (("shell_open", "warm"), lambda svc, b: EnvsAdapter(svc)),
}


class Bridge:
    def __init__(self, runtime: Any, *, timezone: str = "UTC"):
        self.runtime = runtime
        self.zone = ZoneInfo(timezone)  # the node's timezone: schedule times are shown in it
        self._cache: dict[str, tuple[int, Any]] = {}
        self._sent: dict[tuple[str, str], tuple[str, dict]] = {}  # (kind, id) -> (last sent JSON, object)

    def adapt(self, name: str, service: Any) -> Any:
        entry = ADAPTERS.get(name)
        if entry is None or not all(callable(getattr(service, m, None)) for m in entry[0]):
            return service
        cached = self._cache.get(name)
        if cached is not None and cached[0] == id(service):
            return cached[1]
        adapter = entry[1](service, self)
        self._cache[name] = (id(service), adapter)
        return adapter

    # ---- live updates ----
    def wire_events(self, bus: Any, lane_json: Callable[[Any], Any]) -> None:
        """Forward the services' change feeds into `bus` (once per bus)."""
        wired: set[str] = getattr(bus, "_agentos_wired", set())
        policy_bus = getattr(self.runtime.get("traces"), "subscribe", None)
        if callable(policy_bus) and "policy" not in wired:
            def on_policy(kind: str, payload: dict) -> None:
                if kind == "approval" and isinstance(payload, dict):
                    approval = approval_json(payload) if "requested_by" in payload else payload
                    bus.publish("approval", approval)  # the Needs you queue refetches
                    if approval.get("kind") == "question":
                        bus.publish("question", question_json(approval))
                elif kind == "trace":
                    bus.publish("trace", payload)
                elif kind == "meter":  # a warning, a cutoff or new limits: the meters view refetches
                    bus.publish("meter", payload)
            policy_bus(on_policy)
            wired.add("policy")
        runner = self.runtime.get("runner")
        if callable(getattr(runner, "subscribe", None)) and callable(getattr(runner, "describe", None)) \
                and "runner" not in wired:
            runner.subscribe(lambda kind, payload: bus.publish("run", run_json(payload)) if kind == "run" else None)
            wired.add("runner")
        lanes = self.runtime.get("lanes")
        if callable(getattr(lanes, "add_control_listener", None)) and "control" not in wired:
            def on_control(lane_id: str, old: Any, new: Any) -> None:
                try:
                    bus.publish("lane", lane_json(lanes.get(lane_id)))
                except AgentosError:
                    pass
            try:
                lanes.add_control_listener(on_control)
                wired.add("control")
            except Exception:  # noqa: BLE001
                log.warning("lane control changes will not reach the panel live")
        bus._agentos_wired = wired

    async def pump_lanes(self, bus: Any, lane_json: Callable[[Any], Any], *, interval: float = 2.0) -> None:
        """Publish a `lane` event whenever a lane's state, control or quarantine changes (the lane
        manager has no change feed). Runs until cancelled."""
        seen: dict[str, tuple] = {}
        first = True
        while True:
            try:
                infos = list(await maybe_await(self.runtime.lanes.list())) if self.runtime.has("lanes") else []
                current = {}
                for info in infos:
                    key = (info.state.value, jsonable(info.control), info.quarantine_reason, info.exit_ip)
                    current[info.spec.id] = key
                    if not first and seen.get(info.spec.id) != key:
                        bus.publish("lane", lane_json(info))
                seen = current
                first = False
            except asyncio.CancelledError:
                raise
            except Exception:  # the pump must outlive one bad read
                log.debug("lane pump read failed", exc_info=True)
            await asyncio.sleep(interval)

    # ---- sent-once feeds (the schedules and brains have no change feed of their own) ----
    def publish_changed(self, bus: Any, kind: str, key: str, obj: dict, *, announce: bool = True) -> bool:
        """Publish `obj` as a `kind` event unless it is what was last sent for this key. A route that
        changes a schedule sends it at once; the poll that follows finds it unchanged and does not
        send it again. `announce=False` only records (the first poll, which must not replay the node)."""
        text = json.dumps(obj, sort_keys=True, default=str)
        slot = (kind, key)
        if slot in self._sent and self._sent[slot][0] == text:
            return False
        self._sent[slot] = (text, obj)
        if announce:
            bus.publish(kind, obj)
        return announce

    def retire(self, bus: Any, kind: str, key: str, obj: dict | None = None, *, announce: bool = True) -> bool:
        """The object is gone: send it once more with `deleted: true` (its last known shape, so a
        grant's filter still sees whose it was)."""
        last = self._sent.pop((kind, key), None)
        last_obj = obj if obj is not None else (last[1] if last else None)
        if last_obj is None or not announce:
            return False
        bus.publish(kind, {**last_obj, "deleted": True})
        return True

    def _known(self, kind: str) -> set[str]:
        return {key for (k, key) in self._sent if k == kind}

    async def pump_brains(self, bus: Any, *, interval: float = 5.0, brains_every: float = 30.0) -> None:
        """Publish `task` and `brain` events for what the brains service changes on its own (a task
        starts, ends, a brain cools down or needs a login). The service has no change feed. Tasks are
        read every `interval`; brains are probed (a CLI check each), so only every `brains_every`.
        Runs until cancelled."""
        primed = False
        last_brains = float("-inf")
        loop = asyncio.get_running_loop()
        while True:
            try:
                svc = self.runtime.get("brains")
                if svc is not None:
                    adapter = self.adapt("brains", svc)
                    for task in await maybe_await(adapter.list_tasks(limit=50)):
                        self.publish_changed(bus, "task", task["id"], task, announce=primed)
                    if loop.time() - last_brains >= brains_every:
                        last_brains = loop.time()
                        for brain in await maybe_await(adapter.list()):
                            self.publish_changed(bus, "brain", brain["name"], brain, announce=primed)
                    primed = True
            except asyncio.CancelledError:
                raise
            except Exception:  # the feed must outlive one bad read
                log.debug("brains feed read failed", exc_info=True)
            await asyncio.sleep(interval)

    async def pump_schedules(self, bus: Any, *, interval: float = 5.0) -> None:
        """Publish `schedule` events for what the scheduler changes on its own: a fire (last_run), a
        cron's next slot, a dead-man pause. Runs until cancelled."""
        primed = False
        while True:
            try:
                svc = self.runtime.get("runner")
                if svc is not None:
                    current = await maybe_await(self.adapt("runner", svc).list_schedules())
                    ids = set()
                    for item in current:
                        ids.add(item["id"])
                        self.publish_changed(bus, "schedule", item["id"], item, announce=primed)
                    for gone in self._known("schedule") - ids:
                        self.retire(bus, "schedule", gone, announce=primed)
                    primed = True
            except asyncio.CancelledError:
                raise
            except Exception:  # the feed must outlive one bad read (no scheduler on this node, a busy db)
                log.debug("schedule feed read failed", exc_info=True)
            await asyncio.sleep(interval)
