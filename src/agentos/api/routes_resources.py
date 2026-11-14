"""Approvals, activity, runs, flows, schedules, nodes, brains, vault, grants, proxies, notifications."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from agentos.api.access import Presence
from agentos.api.actors import trace_actor
from agentos.api.app import (
    MAX_BODY,
    Api,
    _takes,
    access_of,
    decision_proof,
    endpoint,
    principal_of,
    query_int,
    query_str,
    read_body,
    read_json_object,
    scoped_get,
    scoped_list,
)
from agentos.api.bridge import QUESTION_STATES, question_json
from agentos.api.errors import ApiError, error_body, error_extra
from agentos.api.jsonable import jsonable, maybe_await, pick, strip_sensitive
from agentos.api.routes_core import OUT_OF_SCOPE, node_list
from agentos.core.errors import (
    AgentosError,
    ApprovalPending,
    ConfirmRequired,
    InvalidRequest,
    NotFound,
    PolicyDenied,
)
from agentos.core.models import PENDING_SPECIALS, Effect, ProxyConfig, RunStatus, parse_lane_id
from agentos.core.tools import pattern_reaches
from agentos.vault.secret import Secret

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
APPROVAL_STATES = ("pending", "approved", "denied", "expired")
VAULT_FIELDS = ("id", "name", "kind", "domain", "lanes", "created_at", "last_used")
LADDER = tuple(e.value for e in Effect if e.rank is not None)
# memory: a special with no effect of its own (agentos.policy.memory_rules); the panel grants it as
# read and write over the grant's workspaces, without private or owner-only entries; memory_read is
# the same without write (agentos.api.bridge.PANEL_SPECIAL_ALIASES)
# environments: vm and exec on lanes env/* only (agentos.api.bridge.PANEL_SPECIAL_BUNDLES)
SPECIALS = (*(e.value for e in Effect if e.rank is None and e not in PENDING_SPECIALS), "memory", "memory_read",
            "environments")
MEMBER_SPECIALS = ("memory_read",)  # what a member's own agent may carry: reading its workspaces' memory
TTL_RE = re.compile(r"^[1-9][0-9]{0,4}[mhdw]$")


def withheld_run(run: dict) -> dict:
    """A run a stranger's text fed, for a token: a route has no family to label, so it leaves that out,
    as agentos.runner.workflows.mirror does for a routine's own run."""
    if not run.get("from_outside"):
        return run
    held = {"params": {"withheld": True}, "steps": [], "summary": None, "proof": None, "output": None, "error": None}
    return {**run, **{k: v for k, v in held.items() if k in run}}


def path_id(request: Request, key: str = "id") -> str:
    value = request.path_params[key]
    if not ID_RE.match(value):
        raise NotFound(f"No item {value!r}.")
    return value


class Decision(BaseModel):
    note: str | None = Field(default=None, max_length=500)
    content_hash: str | None = Field(default=None, max_length=200)  # what the human saw
    # approve with an edit: {param: new text} for the card's editable params (a caption)
    edit: dict[str, str] | None = Field(default=None, max_length=10)
    # a send card whose preview is cut: the token the last page of its messages gave
    seen: str | None = Field(default=None, max_length=200)


class GrantRoute(BaseModel):
    """How the computer that holds a grant reaches this node (agentos.ops.viewroutes)."""
    model_config = ConfigDict(extra="forbid")
    ssh_host: str = Field(min_length=1, max_length=255)
    local_port: int = Field(ge=1, le=65535)


class CodeAnswer(BaseModel):
    """A code card's answer. The code is write-only: it goes to the typist and is never echoed."""
    model_config = ConfigDict(extra="forbid")
    code: SecretStr = Field(min_length=1, max_length=64)


def issuing_grant(access: Any) -> Any:
    """The grant a new grant must fit inside: the caller's own, unless the caller is the owner (the
    owner token, or the owner's session, which carries the owner's grant). None for the owner."""
    grant = getattr(getattr(access, "ctx", None), "grant", None)
    if grant is None or getattr(grant, "owner_token", False):
        return None
    return grant


def grant_family(access: Any) -> str | None:
    """The root grant id of the grant a token acts under: the family every grant narrowed from it
    belongs to. A narrowed grant's own id (`access.grant_id`) is only its leaf, so a check on it
    misses what the family's parent asked for."""
    grant = getattr(getattr(access, "ctx", None), "grant", None)
    return getattr(grant, "root_id", None) or access.grant_id


def code_family(request: Request) -> str | None:
    """The grant family a caller may not answer codes for: its own root grant when it acts under a
    token. The owner signed in to the panel answers every code card (as for approvals)."""
    if request.state.auth.via != "bearer":
        return None
    return grant_family(access_of(request))


class StandingCreate(BaseModel):
    # the accounts it covers: one lane, several lanes of one workspace, or every lane of one workspace
    lane: str | None = None
    lanes: list[str] | None = Field(default=None, min_length=1, max_length=100)
    workspace: str | None = Field(default=None, max_length=64)
    effect: Literal["commit"] = "commit"  # drafts never wait for a human, so there is nothing to pre-approve
    ttl_seconds: int = Field(ge=600, le=30 * 24 * 3600)
    note: str | None = Field(default=None, max_length=500)
    # "approve this, and the same again": the approvals the owner saw; a commit standing approval
    # covers only their tool or flow version and their content (AUD-10)
    from_approvals: list[str] = Field(default_factory=list, max_length=50)
    # "always allow this shape": the approval's flow version or connector operation on this lane,
    # whatever it posts, until it expires (the panel offers a day or a week)
    shape: bool = False
    count: int | None = Field(default=None, ge=1, le=1000)  # how many times it may cover
    per_day: int | None = Field(default=None, ge=1, le=1000)  # and how many of those a day
    targets: list[str] = Field(default_factory=list, max_length=20)  # flow:<id>@<version> or tool:<name>

    @model_validator(mode="after")
    def _scope(self) -> StandingCreate:
        given = [name for name in ("lane", "lanes", "workspace") if getattr(self, name)]
        if len(given) != 1:
            raise ValueError("give exactly one of lane, lanes or workspace")
        lanes = [self.lane] if self.lane else list(self.lanes or [])
        for lane in lanes:
            parse_lane_id(lane)
        if len({lane.split("/", 1)[0] for lane in lanes}) > 1:
            raise ValueError("the lanes must all be in one workspace")
        if self.workspace is not None and not ID_RE.match(self.workspace):
            raise ValueError("workspace is not a valid id")
        return self

    def workspaces(self) -> list[str]:
        if self.workspace:
            return [self.workspace]
        return sorted({lane.split("/", 1)[0] for lane in ([self.lane] if self.lane else self.lanes or [])})


class QuestionAnswer(BaseModel):
    answer: str = Field(min_length=1, max_length=2000)


class Batch(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)
    decision: Literal["approve", "deny"]
    note: str | None = Field(default=None, max_length=500)
    content_hashes: dict[str, str] = Field(default_factory=dict)


class AskFirstChange(BaseModel):
    scope: Literal["flow", "lane"]
    id: str = Field(min_length=1, max_length=256)
    ask_first: bool


class FlowRun(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)
    on: str | None = Field(default=None, max_length=200)  # the lane, for flows bound to a lane pattern


class ScheduleCreate(BaseModel):
    flow: str = Field(min_length=1, max_length=128)
    cron: str | None = Field(default=None, max_length=100)
    watch: str | None = Field(default=None, max_length=2000)
    params: dict[str, Any] = Field(default_factory=dict)
    deadman_minutes: int | None = Field(default=None, ge=1, le=7 * 24 * 60)

    @model_validator(mode="after")
    def _one_trigger(self) -> ScheduleCreate:
        if bool(self.cron) == bool(self.watch):
            raise ValueError("give exactly one of cron or watch")
        return self


class ScheduleUpdate(BaseModel):
    enabled: bool


class BrainOrder(BaseModel):
    order: list[str] = Field(min_length=1, max_length=20)


class VaultCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["password", "totp", "cookie", "api_key"]
    domain: str = Field(min_length=1, max_length=253)
    lanes: list[str] = Field(default_factory=list, max_length=100)
    username: str | None = Field(default=None, max_length=200)
    secret: SecretStr
    totp: SecretStr | None = None   # a password entry's 2FA seed (or otpauth:// URI): the login tool types its codes

    @field_validator("lanes")
    @classmethod
    def _lanes(cls, v: list[str]) -> list[str]:
        for lane in v:
            parse_lane_id(lane)
        return v


class GrantCreate(BaseModel):
    subject: str = Field(min_length=1, max_length=100)
    workspaces: list[str] = Field(min_length=1, max_length=50)
    lanes: list[str] | None = None
    ceiling: str = "read"
    specials: list[str] = Field(default_factory=list)
    budget: dict[str, Any] | None = None
    ttl: str = "7d"

    @field_validator("ceiling")
    @classmethod
    def _ceiling(cls, v: str) -> str:
        if v not in LADDER:
            raise ValueError(f"ceiling must be one of {', '.join(LADDER)}")
        return v

    @field_validator("specials")
    @classmethod
    def _specials(cls, v: list[str]) -> list[str]:
        bad = [s for s in v if s not in SPECIALS]
        if bad:
            raise ValueError(f"unknown special capability: {', '.join(bad)}")
        return v

    @field_validator("ttl")
    @classmethod
    def _ttl(cls, v: str) -> str:
        if not TTL_RE.match(v):
            raise ValueError("ttl looks like 30m, 12h, 7d or 4w")
        return v


class ProxyCreate(BaseModel):
    label: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    username: str | None = Field(default=None, max_length=200)
    password: SecretStr | None = None
    confirm: bool = False  # point an existing label at another host, port or user (a new IP for its users)


def proof_kwargs(svc: Any, proof: Any) -> dict[str, Any]:
    return {"proof": proof} if proof is not None and _takes(svc.decide, "proof") else {}


def _decided(access: Any, proof: Any) -> dict[str, Any]:
    """What a decision's trace records of who decided: the request's presence level, and the method and
    level of its proof (None for a token, which proves no person)."""
    level = getattr(proof, "level", None)
    return {"presence": access.presence.word, "method": getattr(proof, "method", None),
            "level": getattr(level, "word", None)}


def decider_kwargs(request: Request, svc: Any, approval: dict) -> dict[str, Any]:
    """How the approvals service is told who decides: a member's grant for the approval's workspace
    (checked by the node: not_approver, four_eyes, revoked), their handle and how they signed in; a
    token's own grant family, which it never decides for (self_approval); nothing for the owner."""
    auth = request.state.auth
    access = auth.access
    if access.member is not None:
        if not _takes(svc.decide, "decider"):
            raise ApiError("unavailable", "Members cannot decide approvals on this node yet.")
        lane = approval.get("lane")
        ws = approval.get("workspace") or (lane.split("/", 1)[0] if isinstance(lane, str) and "/" in lane else None)
        grants = [g for g in access.grants_with("approvals") if g.covers(lane, ws)] or \
            access.grants_with("approvals")
        policy = request.app.state.api.runtime.get("policy")
        decision_ok = getattr(getattr(policy, "approvals", None), "can_decide", None)
        if callable(decision_ok) and len(grants) > 1:  # the grant whose approve covers it, when one does
            try:
                ap = policy.approvals.get(approval["id"])
                grants = sorted(grants, key=lambda g: not decision_ok(ap, g))
            except AgentosError:
                pass
        if not grants:
            raise ApiError("denied", "Your role does not see this approval.")
        return {"decider": grants[0], "by": access.member.handle, "via": auth.session.via if auth.session else "passkey"}
    if auth.via == "bearer" and auth.session is None and access.presence >= Presence.PERSON \
            and _takes(svc.decide, "via"):
        return {"via": "cli"}  # the node's CLI, with its one-time code: a person, as on the node
    if auth.via == "bearer" and access.grant_id and _takes(svc.decide, "refuse_family"):
        return {"refuse_family": grant_family(access)}
    return {}


def with_handles(api: Api, events: list[dict]) -> list[dict]:
    """Activity names a member by their handle (`handle`), not only their member id."""
    handles = api.members.store.handles()
    for e in events:
        pid = e.get("principal")
        if isinstance(e, dict) and e.get("principal_kind") == "human" and pid in handles:
            e["handle"] = handles[pid]
        if isinstance(e, dict):  # who and how, one wording (agentos.api.actors)
            e["who"], e["how"] = trace_actor(e)
    return events


async def failed_outcomes_for(api: Api, access: Any, limit: int) -> list[dict]:
    """Approved actions whose run failed or stopped short, and nobody has said "Got it" yet. The run
    says what happened; the approval is what a person decided, so only approvals a caller may see.
    Needs you counts these too (routes_core status), so the list and the count share this one rule."""
    svc = api.svc("approvals")
    cleared = svc.cleared_outcomes() if callable(getattr(svc, "cleared_outcomes", None)) else {}
    runs = []
    for status in ("failed", "partial"):
        runs += jsonable(await maybe_await(api.svc("runner").list_runs(status=status, limit=200)))
    out: list[dict] = []
    for run in sorted(runs, key=lambda r: str(r.get("finished_at") or ""), reverse=True):
        approval_id = run.get("approval_id")
        if not approval_id or approval_id in cleared or len(out) >= limit or not ID_RE.match(approval_id):
            continue
        try:
            approval = jsonable(await scoped_get(svc.get, access, approval_id))
        except (NotFound, ApiError):
            continue  # not visible to this caller, or gone
        if approval.get("state") in ("approved", "consumed") and approval.get("kind") != "question":
            out.append(approval)
    return out


def routes(api: Api) -> list[Route]:
    # ---- approvals ----
    async def approvals(request: Request) -> list[dict]:
        state = query_str(request, "state")
        if state and state not in APPROVAL_STATES:
            raise InvalidRequest(f"state must be one of {', '.join(APPROVAL_STATES)}.")
        limit = query_int(request, "limit", 50)
        outcome = query_str(request, "outcome")
        if outcome is not None:
            if outcome != "needs_you":
                raise InvalidRequest("outcome must be needs_you.")
            return await failed_outcomes(request, limit)
        return jsonable(await scoped_list(api.svc("approvals").list, access_of(request), state=state,
                                          limit=limit))

    async def failed_outcomes(request: Request, limit: int) -> list[dict]:
        return await failed_outcomes_for(api, access_of(request), limit)

    async def approval_get(request: Request) -> dict:
        return jsonable(await scoped_get(api.svc("approvals").get, access_of(request), path_id(request)))

    async def _decide(request: Request, approval_id: str, decision: str, note: str | None,
                      seen_hash: str | None, edit: dict[str, str] | None = None, seen: str | None = None) -> dict:
        svc = api.svc("approvals")
        access = access_of(request)
        action = f"approval.{decision}"
        if access.member is not None:  # a member: only what they see (404 otherwise)
            current = jsonable(await scoped_get(svc.get, access, approval_id))
            # deciding either way is the same right: a no stops what someone else may have wanted
            lane = current.get("lane")
            ws = current.get("workspace") or (lane.split("/", 1)[0] if isinstance(lane, str) and "/" in lane else None)
            access.require("decide", ws, action=action, lane=lane if isinstance(lane, str) else None)
        else:
            current = jsonable(await maybe_await(svc.get(approval_id)))
        if seen_hash is not None:
            from agentos.api.auth import tokens_equal
            if not tokens_equal(str(current.get("content_hash") or ""), seen_hash):
                raise ApiError("conflict", "The content changed since you looked at it. Review it again.")
        proof = decision_proof(api, request, current, decision)  # a fresh check is spent here, once
        extra = decider_kwargs(request, svc, current) | proof_kwargs(svc, proof)
        if edit is not None:
            if not _takes(svc.decide, "edit"):
                raise ApiError("unavailable", "This node cannot approve with an edit yet.")
            extra["edit"] = edit
        if seen is not None and _takes(svc.decide, "seen"):
            extra["seen"] = seen
        try:
            result = jsonable(await maybe_await(svc.decide(approval_id, decision=decision,
                                                           principal=principal_of(request), note=note, **extra)))
        except AgentosError as exc:
            access.trace(action, status="denied", lane=current.get("lane"), effect=current.get("effect"),
                         summary=exc.message, approval=approval_id, **_decided(access, proof))
            raise
        edited = (result.get("edit") or {}) if edit is not None else {}
        how = f" with an edit by {edited.get('by')} ({', '.join(edited.get('params') or [])})" if edited else ""
        access.trace(action, lane=result.get("lane"), effect=result.get("effect"),
                     summary=f"{decision}{how} {approval_id}: {str(result.get('summary') or '')[:200]}",
                     approval=approval_id, **_decided(access, proof))
        api.publish("approval", result)
        return result

    async def approve(request: Request) -> dict:
        body = await read_body(request, Decision)
        return await _decide(request, path_id(request), "approve", body.note, body.content_hash, body.edit, body.seen)

    async def approval_messages(request: Request) -> dict:
        """A send card's messages, a page at a time, in order (ApprovalStore.send_messages)."""
        await scoped_get(api.svc("approvals").get, access_of(request), path_id(request))  # 404 unless visible
        get = getattr(api.svc("approvals"), "messages", None)
        if not callable(get):
            raise ApiError("unavailable", "This node cannot show a send card's messages yet.")
        return jsonable(await maybe_await(get(path_id(request), query_int(request, "page", 0, lo=0, hi=100_000),
                                              query_str(request, "cursor"))))

    async def deny(request: Request) -> dict:
        body = await read_body(request, Decision)
        return await _decide(request, path_id(request), "deny", body.note, body.content_hash)

    async def batch(request: Request) -> dict:
        access = access_of(request)
        raw = await request.body()
        try:
            peek = json.loads(raw) if len(raw) <= MAX_BODY else None
        except ValueError:  # read_body below answers a body that is not JSON
            peek = None
        if isinstance(peek, dict) and peek.get("decision") == "approve":
            # presence first, as on the single approve route: a grant learns a person decides it (QA-2 #9)
            access.require_presence(Presence.PERSON, "batch approve")
        # the same role the single routes ask for, either way (QA-teams-3), before the body is read
        access.require_role("decide", action="batch")
        body = await read_body(request, Batch)
        results = []
        for approval_id in dict.fromkeys(body.ids):  # unique, in order
            try:
                if not ID_RE.match(approval_id):
                    raise NotFound(f"No approval {approval_id!r}.")
                approval = await _decide(request, approval_id, body.decision, body.note,
                                         body.content_hashes.get(approval_id))
                results.append({"id": approval_id, "ok": True, "approval": approval})
            except AgentosError as exc:
                results.append({"id": approval_id, "ok": False,
                                **error_body(exc.code, exc.message, member=access.member is not None, **error_extra(exc))})
        return {"results": results, "ok": all(r["ok"] for r in results)}

    async def approval_review(request: Request) -> dict:
        """A Review link from a push opens here. It names the approval and decides nothing: the owner
        approves in the signed-in panel. The link's own signature is what makes it valid."""
        svc, access = api.svc("approvals"), access_of(request)
        approval_id = path_id(request)
        if getattr(svc, "review", None) is None:
            raise ApiError("unavailable", "Review links are not supported by this node yet.")
        if access.member is not None:
            await scoped_get(svc.get, access, approval_id)  # visible to this caller, else 404
        q = request.query_params
        if not (q.get("e") and q.get("s")):
            raise ApiError("unauthorized", "This approval link is not valid.")
        try:
            return jsonable(await maybe_await(svc.review(approval_id, q["e"], q["s"])))
        except InvalidRequest:
            raise ApiError("unauthorized", "This approval link is not valid.") from None

    async def approval_clear(request: Request) -> dict:
        """"Got it" on an approved action that did not work: it leaves the failed list for everyone. Nothing
        is decided again, and the run keeps the outcome it had. So it takes the right to decide in that
        approval's workspace, the same as a no."""
        svc, access = api.svc("approvals"), access_of(request)
        approval_id = path_id(request)
        if getattr(svc, "clear_outcome", None) is None:
            raise ApiError("unavailable", "Clearing an outcome is not supported by this node yet.")
        if access.member is not None:  # visible to this caller, else 404; and a right to decide there
            current = jsonable(await scoped_get(svc.get, access, approval_id))
            lane = current.get("lane")
            ws = current.get("workspace") or (lane.split("/", 1)[0] if isinstance(lane, str) and "/" in lane else None)
            access.require("decide", ws, action="approval.clear", lane=lane if isinstance(lane, str) else None)
        result = jsonable(await maybe_await(svc.clear_outcome(approval_id, principal=principal_of(request))))
        access.trace("approval.clear", lane=result.get("lane"), summary=f"Got it on {approval_id}",
                     approval=approval_id)
        api.publish("approval", result)
        return result

    async def preview_png(request: Request) -> Response:
        svc = api.svc("approvals")
        await scoped_get(svc.get, access_of(request), path_id(request))  # visible to this caller
        fn = getattr(svc, "preview_image", None)
        png = await maybe_await(fn(path_id(request))) if fn else None
        if not png:
            raise NotFound("This approval has no image preview.")
        return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})

    async def approval_file(request: Request) -> Response:
        """An image an approval card shows (a photo the post attaches), for whoever sees the card."""
        svc = api.svc("approvals")
        approval_id = path_id(request)
        await scoped_get(svc.get, access_of(request), approval_id)  # visible to this caller, else 404
        param = request.path_params["param"]
        fn = getattr(svc, "file", None)
        if fn is None or not re.match(r"^[a-z][a-z0-9_]{0,63}$", param):
            raise NotFound("This approval has no such image.")
        data, mime = await maybe_await(fn(approval_id, param))
        return Response(data, media_type=mime, headers={"Cache-Control": "private, max-age=300",
                                                        "X-Content-Type-Options": "nosniff",
                                                        "Content-Security-Policy": "default-src 'none'"})

    def _standing_mine(request: Request, item: dict) -> bool:
        access = access_of(request)
        lanes = item.get("lanes") or ([item["lane"]] if item.get("lane") else [])
        return access.owner or (bool(lanes) and all(isinstance(lane, str) and "/" in lane and access.can(
            "standing", lane.split("/")[0]) for lane in lanes))

    async def standing_list(request: Request) -> list[dict]:
        svc = api.svc("approvals")
        fn = getattr(svc, "list_standing", None)
        everything = query_str(request, "all") == "true"
        if fn is not None and everything and not _takes(fn, "all_states"):
            raise ApiError("unavailable", "This node cannot list finished standing approvals yet.")
        items = jsonable(await maybe_await(fn(all_states=True) if everything else fn())) if fn else []
        return [i for i in items if _standing_mine(request, i)]

    async def standing_create(request: Request) -> dict:
        """A person present (endpoint: PERSON, and a member's passkey check). Made from approvals, each
        must be one this person could have approved themselves (decision_problem: their role's approve,
        four-eyes), already approved, and for this very lane (ApprovalsAdapter.create_standing)."""
        body = await read_body(request, StandingCreate)
        access = access_of(request)
        workspaces = body.workspaces()
        for workspace in workspaces:
            access.require("standing", workspace, action="standing.create", lane=body.lane)
        fn = getattr(api.svc("approvals"), "create_standing", None)
        if fn is None:
            raise ApiError("unavailable", "Standing approvals are not supported by this node yet.")
        kwargs: dict[str, Any] = {"from_approvals": body.from_approvals} if body.from_approvals else {}
        if body.from_approvals and access.member is not None:
            if not _takes(fn, "decider"):
                raise ApiError("unavailable", "Members cannot make standing approvals from approvals on this node yet.")
            deciders = [g for g in access.grants_with("decide")  # type: ignore[attr-defined]
                        if all(g.covers(body.lane, ws) for ws in workspaces)]
            if not deciders:
                access.trace("standing.create", status="denied", lane=body.lane, summary="role: no approve here")
                raise ApiError("denied", "Your role does not approve on this lane.")
            kwargs["decider"] = deciders
        if body.shape:
            if not _takes(fn, "shape"):
                raise ApiError("unavailable", "This node cannot allow a shape yet.")
            kwargs["shape"] = True
        for name, value in (("count", body.count), ("per_day", body.per_day), ("targets", body.targets or None),
                            ("lanes", body.lanes), ("workspace", body.workspace)):
            if value is None:
                continue
            if not _takes(fn, name):
                raise ApiError("unavailable", f"This node cannot limit a standing approval by {name} yet.")
            kwargs[name] = value
        result = jsonable(await maybe_await(fn(effect=body.effect, ttl_seconds=body.ttl_seconds,
                                               principal=principal_of(request), lane=body.lane, note=body.note,
                                               **kwargs)))
        scope = body.lane or ", ".join(body.lanes or []) or f"every account in {body.workspace}"
        what = f"Always allow {', '.join(result.get('targets') or [])} on" if body.shape else f"Standing {body.effect} on"
        access_of(request).trace("standing.create", lane=body.lane, effect=body.effect,
                                 summary=f"{what} {scope} for {body.ttl_seconds} s")
        api.publish("approval", {"standing": result})
        return result

    async def standing_revoke(request: Request) -> dict:
        fn = getattr(api.svc("approvals"), "revoke_standing", None)
        if fn is None:
            raise ApiError("unavailable", "Standing approvals are not supported by this node yet.")
        sid = path_id(request)
        if access_of(request).member is not None:
            mine = [i for i in jsonable(await maybe_await(api.svc("approvals").list_standing()))
                    if i.get("id") == sid and _standing_mine(request, i)]
            if not mine:
                raise NotFound(f"No standing approval {sid}.")
        result = await maybe_await(fn(sid, principal=principal_of(request)) if _takes(fn, "principal") else fn(sid))
        access_of(request).trace("standing.revoke", summary=f"Revoke standing approval {sid}")
        return jsonable(result) if result else {"ok": True}

    async def approval_link(request: Request) -> dict:
        """The phone's Approve and Deny buttons: a signed, single-approval, expiring link is the
        only credential (the signature, never a grant). Anything else is 401."""
        q = request.query_params
        fn = getattr(api.svc("approvals"), "decide_by_link", None)
        if fn is None or not (q.get("d") and q.get("e") and q.get("s")):
            raise ApiError("unauthorized", "This approval link is not valid.")
        try:
            result = jsonable(await maybe_await(fn(path_id(request), q["d"], q["e"], q["s"])))
        except InvalidRequest:
            raise ApiError("unauthorized", "This approval link is not valid.") from None
        api.publish("approval", result)
        return result

    # ---- questions (agent questions from human_ask; a question is an approval of kind "question") ----
    async def questions(request: Request) -> list[dict]:
        state = query_str(request, "state") or "open"
        if state not in QUESTION_STATES:
            raise InvalidRequest(f"state must be one of {', '.join(QUESTION_STATES)}.")
        items = await scoped_list(api.svc("approvals").list, access_of(request), state=QUESTION_STATES[state],
                                  kind="question", limit=query_int(request, "limit", 50))
        return [question_json(i) for i in jsonable(items)]

    async def question_answer(request: Request) -> dict:
        body = await read_body(request, QuestionAnswer)
        question_id = path_id(request)
        svc = api.svc("approvals")
        access = access_of(request)
        current = jsonable(await (scoped_get(svc.get, access, question_id) if access.member is not None
                                  else maybe_await(svc.get(question_id))))
        if current.get("kind") != "question":
            raise NotFound(f"No question {question_id}.")
        proof = decision_proof(api, request, current, "approve")
        extra = decider_kwargs(request, svc, current) | proof_kwargs(svc, proof)  # as for approvals: a token never answers its own asks
        try:
            result = jsonable(await maybe_await(svc.decide(question_id, decision="approve",
                                                           principal=principal_of(request), answer=body.answer,
                                                           **extra)))
        except AgentosError as exc:
            access.trace("question.answer", status="denied", summary=exc.message, approval=question_id,
                         **_decided(access, proof))
            raise
        access.trace("question.answer", summary=f"Answer question {question_id}", approval=question_id,
                     **_decided(access, proof))
        question = question_json(result)
        api.publish("question", question)
        return question

    # ---- code cards (agentos.vault.relay): a one-time code typed by the owner, for one login step ----
    async def codes(request: Request) -> list[dict]:
        fn = getattr(api.svc("vault"), "codes", None)
        return [] if fn is None else jsonable(await maybe_await(fn()))

    async def _code(request: Request, action: str, call: str, **kwargs: Any) -> dict:
        fn = getattr(api.svc("vault"), call, None)
        if fn is None:
            raise ApiError("unavailable", "Code cards are not supported by this node yet.")
        request_id = path_id(request)
        principal = principal_of(request)
        access = access_of(request)
        try:
            result = jsonable(await maybe_await(fn(request_id, by=f"{principal.kind}:{principal.id}",
                                                   family=code_family(request), **kwargs)))
        except AgentosError as exc:  # a refusal is a denial; a closed or unknown request is only an error
            access.trace(action, status="denied" if isinstance(exc, PolicyDenied) else "error", summary=exc.message,
                         code_request=request_id)
            raise
        access.trace(action, lane=result.get("lane"), code_request=request_id,
                     summary=f"{action} {request_id} for {result.get('site')} on {result.get('lane')}")
        return result

    async def code_answer(request: Request) -> dict:
        body = await read_body(request, CodeAnswer)
        return await _code(request, "code.enter", "submit_code", code=Secret(body.code.get_secret_value()))

    async def code_decline(request: Request) -> dict:
        return await _code(request, "code.decline", "decline_code")

    # ---- activity and runs ----
    async def activity(request: Request) -> list[dict]:
        access = access_of(request)
        lane = query_str(request, "lane")
        workspace = query_str(request, "workspace")
        if lane and not access.lane_allowed(lane):
            raise ApiError("denied", OUT_OF_SCOPE)
        query = api.svc("traces").query
        kw = {"lane": lane, "workspace": workspace, "kind": query_str(request, "kind"),
              "before": query_str(request, "before"), "limit": query_int(request, "limit", 100)}
        if access.member is not None:  # every principal's events in the member's workspaces
            spaces = access.workspaces_with("see")  # type: ignore[attr-defined]
            if spaces is not None and workspace is not None and workspace not in spaces:
                raise ApiError("denied", OUT_OF_SCOPE)
            if spaces is None:
                events = await maybe_await(query(**kw))
            elif not spaces or not _takes(query, "viewer_workspaces"):
                events = []
            else:
                events = await maybe_await(query(viewer_workspaces=sorted(spaces), **kw))
        else:
            events = await scoped_list(query, access.viewer, **kw)
        return with_handles(api, jsonable(events))

    def _run_visible(request: Request, run: dict) -> bool:
        access = access_of(request)
        lane = run.get("lane")
        return access.owner or (isinstance(lane, str) and access.lane_allowed(lane))

    async def runs(request: Request) -> list[dict]:
        status = query_str(request, "status")
        if status and status not in {s.value for s in RunStatus}:
            raise InvalidRequest("Unknown run status.")
        flow = query_str(request, "flow")  # a recorded routine's own runs, newest first (additive)
        extra = {"flow": flow} if flow and _takes(api.svc("runner").list_runs, "flow") else {}
        items = jsonable(await maybe_await(api.svc("runner").list_runs(status=status,
                                                                       limit=query_int(request, "limit", 50), **extra)))
        return [r for r in items if _run_visible(request, r)]

    async def run_get(request: Request) -> dict:
        run = jsonable(await maybe_await(api.svc("runner").get_run(path_id(request))))
        if not _run_visible(request, run):
            raise NotFound(f"No run {path_id(request)}.")
        return run if access_of(request).human else withheld_run(run)

    # ---- flows and schedules ----
    async def flows(request: Request) -> list[dict]:
        access = access_of(request)
        items = jsonable(await maybe_await(api.svc("flows").list()))
        if access.owner:
            return items
        # a flow bound to one lane shows when that lane is in scope; a pattern when it can match a lane in
        # the grant (a member's session: its patterns are checked at run time)
        ctx = getattr(access, "ctx", None)

        def seen(lane: str) -> bool:
            if any(c in lane for c in "*?["):
                return pattern_reaches(ctx, lane) if ctx is not None else True
            return access.lane_allowed(lane)

        return [f for f in items if isinstance(f.get("lane"), str) and seen(f["lane"])]

    async def flow_run(request: Request) -> dict:
        body = await read_body(request, FlowRun)
        svc = api.svc("flows")
        flow_id, access, principal = path_id(request), access_of(request), principal_of(request)
        plan = getattr(svc, "plan", None)
        if plan is None:  # a flow service that cannot say what it would do: it authorizes on its own
            run = jsonable(await maybe_await(svc.run(flow_id, params=body.params, principal=principal)))
        else:
            p = await maybe_await(plan(flow_id, params=body.params, on=body.on))
            if not access.lane_allowed(p["lane"]):
                access.trace("flow.run", status="denied", lane=p["lane"], summary="out of scope")
                raise ApiError("denied", OUT_OF_SCOPE)
            if access.member is not None:
                access.require("operate", p["lane"].split("/", 1)[0], action="flow.run", lane=p["lane"])
                principal = access.principal_for(principal, p["lane"])
            approval_id = decision = None
            try:
                decision = await access.authorize(p["effect"], action="flow.run", lane_id=p["lane"],
                                                  summary=p["summary"], content=p["content"])
            except ApprovalPending as exc:
                approval_id = exc.approval_id  # the run waits for the owner, then starts
            authorized = approval_id is None and access.grant_id is not None  # without policy the runner decides
            ids = getattr(decision, "pace_ids", None) or []  # the pace row its authorization wrote, kept on the run
            row = {"pace_id": ids[0]} if authorized and ids else {}
            if p.get("version"):  # the version it checked runs, not one saved since
                row["version"] = p["version"]
            if authorized and getattr(decision, "route", None) == "auto":
                row["auto"] = True  # let through on its own: the runner reads Ask me first again before it runs
            run = jsonable(await maybe_await(svc.run(flow_id, params=p["params"], principal=principal, on=p["lane"],
                                                     authorized=authorized, approval_id=approval_id, **row)))
        api.publish("run", run)
        return run

    # A grant sees its own schedules (the grant_id they were created under); the owner sees all; a
    # member with `standing` (an admin) the schedules on their workspaces' lanes.
    def _schedule_visible(request: Request, schedule: dict) -> bool:
        return access_of(request).schedule_visible(schedule)

    async def _own_schedule(request: Request, sid: str, action: str) -> dict:
        """The schedule a member may change (404 for one they do not see, before anything changes)."""
        schedule = jsonable(await maybe_await(api.svc("runner").get_schedule(sid)))
        if not _schedule_visible(request, schedule):
            access_of(request).trace(action, status="denied", summary=f"schedule {sid} is not in your workspaces")
            raise NotFound(f"No schedule {sid}.")
        return schedule

    async def schedules(request: Request) -> list[dict]:
        items = jsonable(await maybe_await(api.svc("runner").list_schedules()))
        return [s for s in items if _schedule_visible(request, s)]

    async def schedule_get(request: Request) -> dict:
        sid = path_id(request)
        schedule = jsonable(await maybe_await(api.svc("runner").get_schedule(sid)))
        if not _schedule_visible(request, schedule):
            raise NotFound(f"No schedule {sid}.")  # as for an item that does not exist: nothing leaks
        return schedule

    def _owner_schedule_needs_a_person(request: Request, grant_id: str | None, action: str) -> None:
        """A schedule under the owner's own grant runs as the owner and never ends, so making one or
        turning one on is a person's word: the node token and the device credential carry that grant,
        and an agent may hold them. An admin grant's schedule is bound to that grant instead."""
        if (grant_id or "owner") == "owner":
            access_of(request).require_presence(Presence.PERSON, action)

    async def schedule_fires(request: Request) -> list[dict]:
        """The runs a schedule started, oldest first in what the node reports: `ScheduleFire`s."""
        sid = path_id(request)
        await _own_schedule(request, sid, "schedule.fires")
        runs = await maybe_await(api.svc("runner").schedule_runs(sid, limit=query_int(request, "limit", 20, hi=100)))
        return [{"at": run.get("started_at") or run.get("created_at"), "status": run["status"], "run_id": run["id"]}
                for run in runs]

    async def schedule_create(request: Request) -> dict:
        body = await read_body(request, ScheduleCreate)
        _owner_schedule_needs_a_person(request, principal_of(request).grant_id, "schedule.create")
        created = jsonable(await maybe_await(api.svc("runner").create_schedule(
            flow=body.flow, cron=body.cron, watch=body.watch, params=body.params,
            deadman_minutes=body.deadman_minutes, principal=principal_of(request))))
        access_of(request).trace("schedule.create", summary=f"Schedule flow {body.flow}")
        api.bridge.publish_changed(api.bus, "schedule", created["id"], created)
        return created

    async def schedule_update(request: Request) -> dict:
        body = await read_body(request, ScheduleUpdate)
        sid = path_id(request)
        before = await _own_schedule(request, sid, "schedule.update")
        if body.enabled:  # a token may stop a schedule, never start one, whoever's grant it runs under
            _owner_schedule_needs_a_person(request, before.get("grant_id"), "schedule.update")
            access_of(request).require_presence(Presence.PERSON, "schedule.update")
        updated = jsonable(await maybe_await(api.svc("runner").set_schedule_enabled(sid, body.enabled)))
        access_of(request).trace("schedule.update", summary=f"{'Enable' if body.enabled else 'Disable'} schedule {sid}")
        api.bridge.publish_changed(api.bus, "schedule", sid, updated)
        return updated

    async def schedule_delete(request: Request) -> dict:
        sid = path_id(request)
        svc = api.svc("runner")
        before = await _own_schedule(request, sid, "schedule.delete")  # 404 before anything changes
        await maybe_await(svc.delete_schedule(sid))
        access_of(request).trace("schedule.delete", summary=f"Delete schedule {sid}")
        api.bridge.retire(api.bus, "schedule", sid, before)
        return {"ok": True}

    # ---- nodes and brains ----
    async def nodes(request: Request) -> list[dict]:
        return await node_list(api)

    async def brains(request: Request) -> list[dict]:
        return jsonable(await maybe_await(api.svc("brains").list()))

    async def brains_order(request: Request) -> list[dict]:
        body = await read_body(request, BrainOrder)
        svc = api.svc("brains")
        await maybe_await(svc.set_order(body.order))
        access_of(request).trace("brains.order", summary="Brain order: " + ", ".join(body.order))
        return jsonable(await maybe_await(svc.list()))

    async def brain_tasks(request: Request) -> list[dict]:
        return jsonable(await maybe_await(api.svc("brains").list_tasks(limit=query_int(request, "limit", 50))))

    async def brain_task(request: Request) -> dict:
        return jsonable(await maybe_await(api.svc("brains").get_task(path_id(request))))

    # ---- vault (names only) ----
    def _vault_mine(request: Request, lanes: list[str] | None) -> bool:
        """The owner: every entry. A member with `vault` (an admin): entries bound only to lanes of
        their workspaces (an entry for no lane, or one shared with another workspace, is the owner's)."""
        access = access_of(request)
        if access.owner:
            return True
        return bool(lanes) and all(isinstance(lane, str) and "/" in lane and access.can("vault", lane.split("/")[0])
                                   for lane in lanes)

    async def vault(request: Request) -> list[dict]:
        items = jsonable(await maybe_await(api.svc("vault").list()))
        return [pick(i, VAULT_FIELDS) for i in items if _vault_mine(request, i.get("lanes"))]

    async def vault_create(request: Request) -> dict:
        body = await read_body(request, VaultCreate)
        if not _vault_mine(request, body.lanes):
            access_of(request).trace("vault.create", status="denied", summary="lanes outside your workspaces")
            raise ApiError("denied", "Bind the entry to lanes of the workspaces you administer.")
        created = jsonable(await maybe_await(api.svc("vault").create(
            name=body.name, kind=body.kind, domain=body.domain, lanes=body.lanes, username=body.username,
            secret=body.secret.get_secret_value(), principal=principal_of(request),
            **({"totp": body.totp.get_secret_value()} if body.totp is not None else {}))))
        access_of(request).trace("vault.create", summary=f"Add vault entry {body.name} ({body.kind}) for "
                                                         f"{body.domain}")
        return pick(created, VAULT_FIELDS)

    async def vault_delete(request: Request) -> dict:
        entry_id = path_id(request)
        access = access_of(request)
        lane = None
        if access.member is not None:
            entry = next((i for i in jsonable(await maybe_await(api.svc("vault").list())) if i.get("id") == entry_id),
                         None)
            if entry is None or not _vault_mine(request, entry.get("lanes")):
                raise NotFound(f"No vault entry {entry_id}.")
            lane = entry["lanes"][0]
        await access.authorize(Effect.IRREVERSIBLE, action="vault.delete", lane_id=lane,
                               summary=f"Delete vault entry {entry_id}", content={"vault_delete": entry_id})
        await maybe_await(api.svc("vault").delete(entry_id))
        return {"ok": True}

    # ---- grants ----
    async def grants(request: Request) -> list[dict]:
        policy = api.svc("policy")
        if not hasattr(policy, "list_grants"):
            raise ApiError("unavailable", "Grants are not supported by this node yet.")
        items = strip_sensitive(jsonable(await maybe_await(policy.list_grants())))
        access = access_of(request)
        if access.member is not None:  # a member's own agents (sub.owner is the member)
            items = [g for g in items if g.get("subject_owner") == access.member.id and g.get("subject_kind") != "human"]
        return items

    async def grant_create(request: Request) -> dict:
        body = await read_body(request, GrantCreate)
        policy = api.svc("policy")
        if not hasattr(policy, "issue_grant"):
            raise ApiError("unavailable", "Grants are not supported by this node yet.")
        access = access_of(request)
        extra: dict[str, Any] = {}
        if access.member is not None:  # connect their own agent: their workspaces, up to commit, memory_read only
            for ws in body.workspaces:
                access.require("connect", ws, action="grant.issue")
            outside = [lane for lane in body.lanes or [] if lane.split("/", 1)[0] not in body.workspaces]
            if outside:  # a lane adds to the workspaces' lanes, so one elsewhere ("*", another workspace) widens it
                access.trace("grant.issue", status="denied", summary=f"lanes outside the workspaces: {', '.join(outside)}")
                raise ApiError("denied", "Every lane must be in one of the grant's workspaces.")
            wider = [s for s in body.specials if s not in MEMBER_SPECIALS]
            if wider or LADDER.index(body.ceiling) > LADDER.index(Effect.COMMIT.value):
                access.trace("grant.issue", status="denied", summary="members connect agents up to commit")
                raise ApiError("denied", "Your own agent's grant goes up to commit, with no special capabilities "
                                         "but memory_read.")
            if not _takes(policy.issue_grant, "owner"):
                raise ApiError("unavailable", "Members cannot connect agents on this node yet.")
            extra["owner"] = access.member.id
        elif access.presence == Presence.DEVICE:  # the device credential: within the owner's ceiling for it
            ceiling = getattr(policy, "device_ceiling", None)
            if not callable(ceiling) or not _takes(policy.issue_grant, "within"):
                access.trace("grant.issue", status="denied", summary="this node cannot bound a device's grant")
                raise ApiError("unavailable", "Only the owner issues grants on this node.")
            extra["within"] = ceiling()
        elif (issuer := issuing_grant(access)) is not None:  # an admin grant: never wider than itself
            if not _takes(policy.issue_grant, "within"):
                access.trace("grant.issue", status="denied", summary="this node cannot bound a grant's grant")
                raise ApiError("unavailable", "Only the owner issues grants on this node.")
            extra["within"] = issuer
        issued = jsonable(await maybe_await(policy.issue_grant(
            subject=body.subject, workspaces=body.workspaces, lanes=body.lanes, ceiling=body.ceiling,
            specials=body.specials, budget=body.budget, ttl=body.ttl, principal=principal_of(request), **extra)))
        token = issued.get("token")
        if not token:
            raise ApiError("internal", "The grant was issued without a token.")
        access_of(request).trace("grant.issue", summary=(
            f"Grant {issued.get('id')} for {body.subject}: {', '.join(body.workspaces)}, up to {body.ceiling}"
            + (f", specials {', '.join(body.specials)}" if body.specials else "") + f", {body.ttl}"))
        # The token is in this one response and nowhere else.
        return {"id": issued.get("id"), "token": token, "grant": strip_sensitive(issued)}

    async def grant_revoke(request: Request) -> dict:
        policy = api.svc("policy")
        gid = path_id(request)
        access = access_of(request)
        if access.member is not None:
            own = [g for g in jsonable(await maybe_await(policy.list_grants())) if g.get("id") == gid]
            if not own or own[0].get("subject_owner") != access.member.id or own[0].get("subject_kind") == "human":
                raise NotFound(f"No grant {gid}.")
        fn = policy.revoke_grant
        result = await maybe_await(fn(gid, principal=principal_of(request)) if _takes(fn, "principal") else fn(gid))
        access_of(request).trace("grant.revoke", summary=f"Revoke grant {gid}")
        return strip_sensitive(jsonable(result)) if result else {"ok": True}

    async def grant_route(request: Request) -> dict:
        """`agentos connect` records the SSH host and local port its tunnel uses for this grant, so
        lane_view gives that computer a link that works there (QA 2026-11-10 finding 9)."""
        from agentos.ops import viewroutes

        body = await read_body(request, GrantRoute)
        gid = path_id(request)
        if api.config is None:
            raise ApiError("unavailable", "This node keeps no connection routes.")
        try:
            route = viewroutes.save(api.config.home, gid, body.ssh_host, body.local_port)
        except ValueError as e:
            raise ApiError("invalid", str(e)) from None
        access_of(request).trace("grant.route", summary=f"Grant {gid} reaches this node as {body.ssh_host}, "
                                                         f"port {body.local_port} there")
        return {"id": gid, **route}

    # ---- proxies (public fields only) ----
    async def proxies(request: Request) -> list[dict]:
        from agentos.api.routes_core import proxies_by_label
        items = await proxies_by_label(api)
        return [p.public() if hasattr(p, "public") else {k: p.get(k) for k in ("label", "host", "port")}
                for p in items.values()]

    async def proxy_create(request: Request) -> dict:
        body = await read_body(request, ProxyCreate)
        proxy = ProxyConfig(label=body.label, host=body.host, port=body.port, username=body.username,
                            password=body.password.get_secret_value() if body.password else None)
        from agentos.api.routes_core import proxies_by_label
        old = (await proxies_by_label(api)).get(proxy.label)
        was = tuple((old.get(k) if isinstance(old, dict) else getattr(old, k, None))
                    for k in ("host", "port", "username", "password")) if old is not None else None
        if was is not None and was != (proxy.host, proxy.port, proxy.username, proxy.password):
            # the label is the exit of every account on it: a new host, port or login is a person's
            # call (docs/THREAT-MODEL.md). A brand-new label changes no account, so it stays open
            access_of(request).require_presence(Presence.PERSON, "proxy.add")
        if was is not None and not body.confirm and was[:3] != (proxy.host, proxy.port, proxy.username):
            from agentos.ops.layout import proxy_users
            users = proxy_users(api.config.home, proxy.label) if api.config is not None else []
            who = f" ({'; '.join(users)})" if users else ""
            raise ConfirmRequired(
                f"Proxy {proxy.label} already points at {was[0]}:{was[1]}. Pointing it elsewhere is a new IP "
                f"for everything that uses it{who}: logged-in accounts may be challenged. Nothing changed.",
                {"label": proxy.label, "users": users})
        svc = api.optional("proxies")
        if svc is not None:
            await maybe_await(svc.add(proxy))
        elif api.config is not None:
            from agentos.core.config import load_proxies, save_proxies
            current = load_proxies(api.config)
            current[proxy.label] = proxy
            save_proxies(api.config, current)
        else:
            raise ApiError("unavailable", "Proxies are not configured on this node.")
        access_of(request).trace("proxy.add", summary=f"Proxy {proxy.label} at {proxy.host}:{proxy.port}")
        return proxy.public()

    # ---- notifications ----
    async def notifications(request: Request) -> dict:
        return strip_sensitive(jsonable(await maybe_await(api.svc("notifier").get_settings())))

    async def notifications_put(request: Request) -> dict:
        data = await read_json_object(request)
        result = strip_sensitive(jsonable(await maybe_await(api.svc("notifier").put_settings(data))))
        access_of(request).trace("notifications.update", summary="Notification settings changed")
        return result

    async def notifications_test(request: Request) -> dict:
        access_of(request).trace("notifications.test", summary="Test notification")
        return strip_sensitive(jsonable(await maybe_await(api.svc("notifier").test())))

    # ---- Ask me first (agentos.policy.ask_first; docs/PROTOCOL.md "Ask me first") ----
    def _switch_lane(scope: str, ident: str) -> str | None:
        """The lane a switch is about: the lane itself, or a flow's lane (None for a lane pattern
        or a flow that is not saved)."""
        if scope == "lane":
            return ident
        flows = api.runtime.get("flows")
        try:
            fv = flows.get(ident) if flows is not None else None
        except AgentosError:
            return None
        return None if fv is None or fv.definition.lane_is_pattern else fv.definition.lane

    async def ask_first_list(request: Request) -> list[dict]:
        access = access_of(request)
        items = [x.public() for x in api.svc("ask_first").list()]
        if access.owner:
            return items
        out = []
        for item in items:
            lane = _switch_lane(item["scope"], item["id"])
            if lane is not None and access.lane_allowed(lane):
                out.append(item)
        return out

    async def ask_first_set(request: Request) -> dict:
        """On: any signed-in caller whose grant reaches the lane. Off lets commits run without a person,
        so it needs a person (Presence.PERSON): the owner, or a member whose role may set standing
        approvals in that workspace. A bearer token (an agent's, or the node token an agent may hold)
        and the device credential only turn it on."""
        body = await read_body(request, AskFirstChange)
        access = access_of(request)
        store = api.svc("ask_first")
        from agentos.policy.ask_first import check_id

        scope, ident = check_id(body.scope, body.id)
        lane = _switch_lane(scope, ident)
        if lane is not None and not access.lane_allowed(lane):
            access.trace("ask_first.set", status="denied", lane=lane, summary="out of scope")
            raise ApiError("denied", OUT_OF_SCOPE)
        if not body.ask_first:
            access.require_presence(Presence.PERSON, "ask_first.set", lane=lane)
            if access.member is not None:
                if lane is None:
                    raise ApiError("denied", "This routine runs on several accounts; ask the owner.")
                access.require("standing", lane.split("/", 1)[0], action="ask_first.set", lane=lane)
        principal = principal_of(request)
        by = principal.id if access.member is not None else ("owner" if access.human else f"agent:{principal.id}")
        setting = store.set(scope, ident, body.ask_first, by=by, person=access.human)
        what = "asks first" if setting.ask_first else "runs on its own"
        access.trace("ask_first.set", lane=lane, summary=f"{scope} {ident} {what}")
        return setting.public()

    # ---- the confirmation setting (agentos.policy.confirmation; docs/CONFIRMATIONS.md) ----
    # every route here needs a person (Presence.PERSON at the endpoint), as for turning Ask me first off:
    # a bearer token never, the node token included, because an agent may hold it
    async def confirmation_get(request: Request) -> dict:
        return {**api.svc("confirmation").public(), "chat_clients": api.svc("approvals").store.chat_clients()}

    async def confirmation_methods(request: Request) -> list[dict]:
        return api.svc("confirmation").methods_public()

    async def confirmation_set(request: Request) -> dict:
        """The owner changes every row; an admin of every workspace changes `commit` and may turn chat off,
        with a recent passkey check as for any member's decision. The floors hold for both. With
        `apply_to_waiting`, the setting then in force also reaches what already waits."""
        access = access_of(request)
        patch = await read_json_object(request)
        to_waiting = patch.pop("apply_to_waiting", False)
        if not isinstance(to_waiting, bool):
            raise InvalidRequest("apply_to_waiting is true or false.")
        if access.owner:
            who, what = "owner", "owner"
        elif access.member is not None and "*" in access.scopes_with("standing"):
            if not access.verified_recently:
                raise ApiError("step_up_required", "Confirm with your passkey first.")
            who, what = access.member.id, "admin"
        else:
            access.trace("confirmation.set", status="denied", summary="this needs the owner or an admin")
            raise ApiError("denied", "Only the owner, or an admin of every workspace, changes this.")
        store = api.svc("confirmation")
        try:
            store.change(patch, by=who, role=what)
        except AgentosError as exc:
            access.trace("confirmation.set", status="denied", summary=exc.message)
            raise
        access.trace("confirmation.set", summary=f"changed {', '.join(sorted(patch))}")
        applied = {"applied_to_waiting": api.runtime.get("policy").apply_setting_to_waiting(by=who)} if to_waiting else {}
        return {**store.public(), "chat_clients": api.svc("approvals").store.chat_clients(), **applied}

    def e(fn):
        return endpoint(api, fn)

    def owner(fn):
        return endpoint(api, fn, owner=True)

    def role(cap: str, presence: Presence | None = None, step_up: bool = False):
        """The owner, or a member whose role has `cap` somewhere (the handler checks the workspace).
        `presence`: the least the request must be (a person, for a decision; docs/THREAT-MODEL.md)."""
        return lambda fn: endpoint(api, fn, cap=cap, presence=presence, step_up=step_up)

    def person(fn):
        """Any signed-in caller limited to its grant, as `e`, but only a person (Presence.PERSON)."""
        return endpoint(api, fn, presence=Presence.PERSON)

    PERSON = Presence.PERSON

    # Scope (see docs/API.md, "Who may call what"): e = any signed-in caller, limited to its grant
    # inside the handler; owner = the owner or an admin grant only; role(cap) = the owner, an admin
    # grant, or a member whose role has `cap` (agentos.api.members.CAPABILITIES) where it applies.
    return [
        Route("/api/approvals", e(approvals), methods=["GET"]),
        Route("/api/approvals/batch", e(batch), methods=["POST"]),  # presence, then the decide role, in the handler
        Route("/api/approvals/standing", role("standing")(standing_list), methods=["GET"]),
        Route("/api/approvals/standing", role("standing", PERSON, step_up=True)(standing_create), methods=["POST"]),
        Route("/api/approvals/standing/{id}", role("standing")(standing_revoke), methods=["DELETE"]),
        Route("/api/approvals/{id}", e(approval_get), methods=["GET"]),
        Route("/api/approvals/{id}/approve", role("decide", PERSON)(approve), methods=["POST"]),
        Route("/api/approvals/{id}/deny", role("decide")(deny), methods=["POST"]),
        Route("/api/approvals/{id}/clear", role("decide")(approval_clear), methods=["POST"]),
        Route("/api/approvals/{id}/review", e(approval_review), methods=["GET"]),
        Route("/api/approvals/{id}/action", endpoint(api, approval_link, public=True), methods=["POST"]),
        Route("/api/approvals/{id}/preview.png", e(preview_png), methods=["GET"]),
        Route("/api/approvals/{id}/files/{param}", e(approval_file), methods=["GET"]),
        Route("/api/approvals/{id}/messages", e(approval_messages), methods=["GET"]),
        Route("/api/codes", owner(codes), methods=["GET"]),
        Route("/api/codes/{id}", endpoint(api, code_answer, owner=True, presence=PERSON), methods=["POST"]),
        Route("/api/codes/{id}/decline", owner(code_decline), methods=["POST"]),
        Route("/api/questions", e(questions), methods=["GET"]),
        Route("/api/questions/{id}/answer", role("answer", PERSON)(question_answer), methods=["POST"]),
        Route("/api/activity", e(activity), methods=["GET"]),
        Route("/api/runs", e(runs), methods=["GET"]),
        Route("/api/runs/{id}", e(run_get), methods=["GET"]),
        Route("/api/flows", e(flows), methods=["GET"]),
        Route("/api/ask-first", e(ask_first_list), methods=["GET"]),
        Route("/api/ask-first", e(ask_first_set), methods=["POST"]),
        Route("/api/confirmation", person(confirmation_get), methods=["GET"]),
        Route("/api/confirmation", person(confirmation_set), methods=["PUT"]),
        Route("/api/confirmation/methods", person(confirmation_methods), methods=["GET"]),
        Route("/api/flows/{id}/run", e(flow_run), methods=["POST"]),
        Route("/api/schedules", e(schedules), methods=["GET"]),
        Route("/api/schedules", owner(schedule_create), methods=["POST"]),
        Route("/api/schedules/{id}", e(schedule_get), methods=["GET"]),
        Route("/api/schedules/{id}/fires", e(schedule_fires), methods=["GET"]),
        Route("/api/schedules/{id}", role("standing")(schedule_update), methods=["PATCH"]),
        Route("/api/schedules/{id}", role("standing")(schedule_delete), methods=["DELETE"]),
        Route("/api/nodes", owner(nodes), methods=["GET"]),
        Route("/api/brains", e(brains), methods=["GET"]),
        Route("/api/brains/order", owner(brains_order), methods=["POST"]),
        Route("/api/brains/tasks", owner(brain_tasks), methods=["GET"]),
        Route("/api/brains/tasks/{id}", owner(brain_task), methods=["GET"]),
        Route("/api/vault", role("vault")(vault), methods=["GET"]),
        Route("/api/vault", role("vault")(vault_create), methods=["POST"]),
        Route("/api/vault/{id}", role("vault")(vault_delete), methods=["DELETE"]),
        Route("/api/grants", role("connect")(grants), methods=["GET"]),
        Route("/api/grants", role("connect")(grant_create), methods=["POST"]),
        Route("/api/grants/{id}/revoke", role("connect")(grant_revoke), methods=["POST"]),
        Route("/api/grants/{id}/route", owner(grant_route), methods=["POST"]),
        Route("/api/proxies", owner(proxies), methods=["GET"]),
        Route("/api/proxies", owner(proxy_create), methods=["POST"]),
        Route("/api/settings/notifications", owner(notifications), methods=["GET"]),
        Route("/api/settings/notifications", owner(notifications_put), methods=["PUT"]),
        Route("/api/settings/notifications/test", owner(notifications_test), methods=["POST"]),
    ]
