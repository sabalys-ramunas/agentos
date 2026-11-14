"""Workflows on the node's API (docs/WORKFLOWS.md, "Node API, tools and files").

Who is a person is decided here, as for turning Ask me first off: the owner or a member signed in
to the panel. A bearer token (an agent's, or the owner token an agent may hold) is not one, so it can
save and run but not turn a workflow on, change a list, or raise a limit.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agentos.api.access import Presence
from agentos.api.app import Api, access_of, endpoint, principal_of
from agentos.api.errors import ApiError
from agentos.core import catalog
from agentos.core.errors import ApprovalPending, InvalidRequest, NotFound
from agentos.core.models import Effect
from agentos.runner.run_rules import OWNER_GRANT
from agentos.runner.workflows.check import operations_in
from agentos.runner.workflows.mirror import mirror
from agentos.runner.workflows.service import Actor, WorkflowInvalid, for_an_agent


async def _json(request: Request) -> dict[str, Any]:
    raw = await request.body()
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        raise InvalidRequest("the request body must be JSON") from None
    if not isinstance(data, dict):
        raise InvalidRequest("the request body must be a JSON object")
    return data


def _actor(request: Request) -> Actor:
    access, p = access_of(request), principal_of(request)
    by = p.id if access.member is not None else ("owner" if access.human else f"agent:{p.id}")
    grant = p.grant_id or (OWNER_GRANT if access.owner else access.grant_id)
    return Actor(p.model_dump(exclude_none=True), grant, bool(access.human), by)


def _invalid(e: WorkflowInvalid, request: Request) -> Response:
    """A routine that broke a rule, with where, for the person who wrote it. A member gets the words only."""
    body = catalog.error_body(e.code, e.message, member=access_of(request).member is not None)
    body["error"]["problems"] = e.problems
    return JSONResponse(body, status_code=400)


class Handlers:
    """The handlers of the routes below, sharing one Api (and what they keep between calls)."""

    def __init__(self, api: Api) -> None:
        self.api = api

    def svc(self) -> Any:
        return self.api.svc("workflows")

    def in_scope(self, request: Request, workspace: str | None) -> bool:
        return bool(workspace) and access_of(request).workspace_allowed(workspace)

    def reachable(self, request: Request, workspace: Any) -> None:
        """A named business the caller cannot reach is refused; naming none is the body's problem."""
        if workspace and not self.in_scope(request, workspace) and not access_of(request).owner:
            raise ApiError("denied", "That business is outside what you can reach.")

    def row(self, request: Request) -> dict[str, Any]:
        out = self.svc().public(request.path_params["id"])
        if not self.in_scope(request, out["workspace"]) and not access_of(request).owner:
            raise NotFound(f"workflow {out['id']!r} is not saved on this node")
        return out

    def shown_run(self, request: Request, view: dict[str, Any]) -> dict[str, Any]:
        """All of a run for a person. A token may be an agent's, and a route has no family to label
        for what a stranger wrote, so it reads what leaves the node. The tools read all of it, labelled."""
        return view if access_of(request).human else mirror(view)

    def shown_row(self, request: Request, row: dict[str, Any]) -> dict[str, Any]:
        return row if access_of(request).human else for_an_agent(row)

    def person_may_run_it(self, request: Request, workspace: str | None) -> None:
        """Turning one on lets it run on its own, so a member needs the role that may let commits run."""
        access = access_of(request)
        if access.member is not None:
            access.require("standing", workspace or "", action="workflow.enable", lane=None)

    def may_go_live(self, request: Request, workspace: str | None) -> bool:
        """A save by a person goes live on a routine that is on only where they may turn it on: the
        owner, or a member with standing who may also decide there. Otherwise it waits for someone who may."""
        access = access_of(request)
        return access.member is None or (access.can("standing", workspace or "") and access.can("decide", workspace or ""))

    async def workflows_list(self, request: Request) -> list[dict]:
        access = access_of(request)
        return [self.shown_row(request, r) for r in self.svc().list(allowed=None if access.owner else lambda ws: self.in_scope(request, ws))]

    async def workflows_catalog(self, request: Request) -> dict:
        access = access_of(request)
        return self.svc().catalog(workspace=request.query_params.get("workspace"), allowed=access.lane_allowed, person=bool(access.human))

    async def workflows_day(self, request: Request) -> dict:
        access = access_of(request)
        return self.svc().day(allowed=None if access.owner else lambda ws: self.in_scope(request, ws))

    async def workflow_get(self, request: Request) -> dict:
        return self.shown_row(request, self.row(request))

    async def workflow_create(self, request: Request) -> Any:
        body = await _json(request)
        self.reachable(request, body.get("workspace"))
        try:
            return self.shown_row(request, self.svc().save(body, _actor(request)))
        except WorkflowInvalid as e:
            return _invalid(e, request)

    async def workflow_update(self, request: Request) -> Any:
        current = self.row(request)
        body = await _json(request)
        live = self.may_go_live(request, current["workspace"]) and (body.get("workspace") in (None, current["workspace"])
                                                              or self.may_go_live(request, body.get("workspace")))
        try:
            return self.shown_row(request, self.svc().save(body, _actor(request), flow_id=request.path_params["id"], live=live))
        except WorkflowInvalid as e:
            return _invalid(e, request)

    async def workflow_delete(self, request: Request) -> dict:
        self.row(request)
        return {"deleted": self.svc().delete(request.path_params["id"], _actor(request))}

    async def workflow_enable(self, request: Request) -> Any:
        current = self.row(request)
        self.person_may_run_it(request, current["workspace"])
        try:
            return self.svc().enable(request.path_params["id"], _actor(request), version=(await _json(request)).get("version"))
        except WorkflowInvalid as e:
            return _invalid(e, request)

    async def workflow_disable(self, request: Request) -> dict:
        self.row(request)
        return self.shown_row(request, self.svc().disable(request.path_params["id"], _actor(request)))

    def acting_lane(self, flow_id: str) -> str | None:
        """The lane of the first step that publishes, commits or more, or None when it only reads."""
        wv = self.svc().get_version(flow_id)
        for _, op in operations_in(wv.definition, self.svc().operation):
            if (op.effect.rank or 0) >= (Effect.COMMIT.rank or 0):
                return op.lane
        return None

    async def member_asks_first(self, request: Request, current: dict[str, Any], values: Any) -> str | None:
        """A member who may not decide approvals in its workspace starts a routine that publishes or
        commits: it waits for a yes from someone who may (the approval's id), as an agent's post does.
        The owner, and a member who may decide there, start it directly (None)."""
        access = access_of(request)
        if access.member is None or access.can("decide", current["workspace"]):
            return None
        lane = self.acting_lane(current["id"])
        if lane is None:
            return None
        who = access.member.display_name or access.member.handle
        why = f"{who} started it, and their role does not let them say yes to it"
        content = {"workflow": current["id"], "name": current["name"], "values": values}
        try:
            await access.authorize(Effect.COMMIT, action="workflow.run", lane_id=lane, summary=f"Run {current['name']}",
                                   content=content, ask_person=why)
        except ApprovalPending as exc:
            return exc.approval_id
        return None  # the policy let it through without asking: nothing to wait for

    async def workflow_run(self, request: Request) -> Any:
        current = self.row(request)
        body = await _json(request)
        values = body.get("values")
        if values is not None and not isinstance(values, dict):
            raise InvalidRequest("values is a JSON object of name: value")
        try:
            self.svc().check_runnable(current["id"])  # an unfinished routine makes no approval
            approval_id = await self.member_asks_first(request, current, values)
            return self.shown_run(request, self.svc().run(request.path_params["id"], _actor(request), values=values,
                                                key=body.get("key"), approval_id=approval_id))
        except WorkflowInvalid as e:
            return _invalid(e, request)

    async def workflow_runs(self, request: Request) -> list[dict]:
        self.row(request)
        return [self.shown_run(request, view) for view in self.svc().runs(request.path_params["id"], limit=50)]

    async def workflow_run_get(self, request: Request) -> dict:
        self.row(request)
        return self.shown_run(request, self.svc().get_run_view(request.path_params["id"], request.path_params["run"]))

    async def workflow_retry(self, request: Request) -> dict:
        current = self.row(request)
        body = await _json(request)
        access = access_of(request)
        if access.member is not None and not access.can("decide", current["workspace"]) and self.acting_lane(current["id"]):
            access.trace("workflow.retry", status="denied", summary="retrying a routine that posts needs someone who decides")
            raise ApiError("denied", "This routine posts or sends, so someone who may say yes here retries it. "
                                     "You can run it again, and it will ask first.")
        return self.shown_run(request, self.svc().retry(request.path_params["id"], request.path_params["run"], _actor(request), force=body.get("force") is True))

    async def lists_get(self, request: Request) -> list[dict]:
        access = access_of(request)
        rows = [self.shown_row(request, r) for r in self.svc().sends.lists()]
        return rows if access.owner else [r for r in rows if self.in_scope(request, r["workspace"])]

    async def lists_save(self, request: Request) -> dict:
        body, actor = await _json(request), _actor(request)
        self.reachable(request, body.get("workspace"))
        return self.svc().sends.save_list(body.get("id"), workspace=str(body.get("workspace") or ""), name=str(body.get("name") or ""),
                                     people=body.get("people"), person=actor.person, by=actor.by)

    async def suppressions_add(self, request: Request) -> dict:
        body, actor = await _json(request), _actor(request)
        self.reachable(request, body.get("workspace"))
        new = self.svc().sends.suppress(str(body.get("workspace") or ""), body.get("email"), by=actor.by, why=body.get("why"))
        return {"suppressed": True, **({"new": new} if actor.person else {})}

    async def suppressions_remove(self, request: Request) -> dict:
        body, actor = await _json(request), _actor(request)
        self.reachable(request, body.get("workspace"))
        return {"removed": self.svc().sends.unsuppress(str(body.get("workspace") or ""), body.get("email"), person=actor.person)}

    async def send_limit_set(self, request: Request) -> dict:
        body, actor = await _json(request), _actor(request)
        return {"daily_limit": self.svc().sends.set_limit(request.path_params["account"].replace("~", "/"), body.get("daily_limit"),
                                                     person=actor.person, by=actor.by)}

    async def list_people(self, request: Request) -> dict:
        return self.svc().sends.people(request.path_params["list"], person=self.in_person(request))

    def in_person(self, request: Request) -> bool:
        """The owner in person (Presence.PERSON), as `answered_in_person` reads a card. Addresses are in
        these answers, so a token never is one, nor the device credential, nor a member's session."""
        access = access_of(request)
        return access.presence >= Presence.PERSON and access.owner and access.member is None

    async def leads_review(self, request: Request) -> dict:
        return self.svc().leads.review(in_person=self.in_person(request))

    async def leads_batch(self, request: Request) -> dict:
        return self.svc().leads.batch(request.path_params["batch"], in_person=self.in_person(request))

    async def leads_decide(self, request: Request) -> dict:
        body = await _json(request)
        return self.svc().leads.decide(request.path_params["batch"], body.get("take"), in_person=self.in_person(request), by="human:owner")

    async def leads_seen(self, request: Request) -> dict:
        return {"noted": self.svc().leads.acknowledge(in_person=self.in_person(request), by="human:owner")}

    async def leads_export(self, request: Request) -> dict:
        return self.svc().leads.export(request.path_params["list"], in_person=self.in_person(request))

    async def leads_erase(self, request: Request) -> dict:
        body = await _json(request)
        return self.svc().leads.erase(body.get("workspace"), body.get("email"), in_person=self.in_person(request))


def routes(api: Api) -> list[Route]:
    h = Handlers(api)

    def e(fn: Any) -> Any:
        return endpoint(api, fn)

    def operate(fn: Any) -> Any:
        return endpoint(api, fn, cap="operate")

    def admin(fn: Any) -> Any:  # what lets commits run, or who gets messages: as standing approvals are
        return endpoint(api, fn, cap="standing")

    def owner(fn: Any) -> Any:  # a lead's address is the owner's to read: `in_person` then refuses a token too
        return endpoint(api, fn, owner=True)

    base = "/api/workflows"
    return [
        Route(base, e(h.workflows_list), methods=["GET"]),
        Route(base, operate(h.workflow_create), methods=["POST"]),
        Route(f"{base}/catalog", e(h.workflows_catalog), methods=["GET"]),
        Route(f"{base}/day", e(h.workflows_day), methods=["GET"]),
        Route(f"{base}/lists", e(h.lists_get), methods=["GET"]),
        Route(f"{base}/lists", admin(h.lists_save), methods=["POST"]),
        Route(f"{base}/lists/{{list}}/leads", owner(h.leads_export), methods=["GET"]),
        Route(f"{base}/lists/{{list}}/people", owner(h.list_people), methods=["GET"]),
        Route(f"{base}/leads", owner(h.leads_review), methods=["GET"]),
        Route(f"{base}/leads", owner(h.leads_erase), methods=["DELETE"]),
        Route(f"{base}/leads/note", owner(h.leads_seen), methods=["POST"]),
        Route(f"{base}/leads/{{batch}}", owner(h.leads_batch), methods=["GET"]),
        Route(f"{base}/leads/{{batch}}", owner(h.leads_decide), methods=["POST"]),
        Route(f"{base}/suppressions", operate(h.suppressions_add), methods=["POST"]),
        Route(f"{base}/suppressions", admin(h.suppressions_remove), methods=["DELETE"]),
        Route(f"{base}/limits/{{account}}", admin(h.send_limit_set), methods=["PUT"]),
        Route(f"{base}/{{id}}", e(h.workflow_get), methods=["GET"]),
        Route(f"{base}/{{id}}", operate(h.workflow_update), methods=["PUT"]),
        Route(f"{base}/{{id}}", admin(h.workflow_delete), methods=["DELETE"]),
        Route(f"{base}/{{id}}/enable", admin(h.workflow_enable), methods=["POST"]),
        Route(f"{base}/{{id}}/disable", operate(h.workflow_disable), methods=["POST"]),
        Route(f"{base}/{{id}}/run", operate(h.workflow_run), methods=["POST"]),
        Route(f"{base}/{{id}}/runs", e(h.workflow_runs), methods=["GET"]),
        Route(f"{base}/{{id}}/runs/{{run}}", e(h.workflow_run_get), methods=["GET"]),
        Route(f"{base}/{{id}}/runs/{{run}}/retry", operate(h.workflow_retry), methods=["POST"]),
    ]
