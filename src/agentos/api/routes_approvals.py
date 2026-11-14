"""Approvals, standing approvals, questions and code requests: what waits for a person (docs/API.md)."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from starlette.requests import Request
from starlette.responses import Response

from agentos.api.access import Presence
from agentos.api.app import (
    MAX_BODY,
    Api,
    _takes,
    access_of,
    decision_proof,
    principal_of,
    query_int,
    query_str,
    read_body,
    scoped_get,
    scoped_list,
)
from agentos.api.bridge import QUESTION_STATES, question_json
from agentos.api.errors import ApiError, error_body, error_extra
from agentos.api.jsonable import jsonable
from agentos.api.route_ids import ID_RE, grant_family, path_id
from agentos.core.aio import maybe_await
from agentos.core.errors import (
    AgentosError,
    InvalidRequest,
    NotFound,
    PolicyDenied,
)
from agentos.core.models import parse_lane_id
from agentos.vault.secret import Secret

APPROVAL_STATES = ("pending", "approved", "denied", "expired")


class Decision(BaseModel):
    note: str | None = Field(default=None, max_length=500)
    content_hash: str | None = Field(default=None, max_length=200)  # what the human saw
    # approve with an edit: {param: new text} for the card's editable params (a caption)
    edit: dict[str, str] | None = Field(default=None, max_length=10)
    # a send card whose preview is cut: the token the last page of its messages gave
    seen: str | None = Field(default=None, max_length=200)


class CodeAnswer(BaseModel):
    """A code card's answer. The code is write-only: it goes to the typist and is never echoed."""
    model_config = ConfigDict(extra="forbid")
    code: SecretStr = Field(min_length=1, max_length=64)


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


class ApprovalHandlers:
    """The approval, question and code handlers, sharing one Api."""

    def __init__(self, api: Api) -> None:
        self.api = api

    # ---- approvals ----
    async def approvals(self, request: Request) -> list[dict]:
        state = query_str(request, "state")
        if state and state not in APPROVAL_STATES:
            raise InvalidRequest(f"state must be one of {', '.join(APPROVAL_STATES)}.")
        limit = query_int(request, "limit", 50)
        outcome = query_str(request, "outcome")
        if outcome is not None:
            if outcome != "needs_you":
                raise InvalidRequest("outcome must be needs_you.")
            return await self.failed_outcomes(request, limit)
        return jsonable(await scoped_list(self.api.svc("approvals").list, access_of(request), state=state,
                                          limit=limit))

    async def failed_outcomes(self, request: Request, limit: int) -> list[dict]:
        return await failed_outcomes_for(self.api, access_of(request), limit)

    async def approval_get(self, request: Request) -> dict:
        return jsonable(await scoped_get(self.api.svc("approvals").get, access_of(request), path_id(request)))

    async def _decide(self, request: Request, approval_id: str, decision: str, note: str | None,
                      seen_hash: str | None, edit: dict[str, str] | None = None, seen: str | None = None) -> dict:
        svc = self.api.svc("approvals")
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
        proof = decision_proof(self.api, request, current, decision)  # a fresh check is spent here, once
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
        self.api.publish("approval", result)
        return result

    async def approve(self, request: Request) -> dict:
        body = await read_body(request, Decision)
        return await self._decide(request, path_id(request), "approve", body.note, body.content_hash, body.edit, body.seen)

    async def approval_messages(self, request: Request) -> dict:
        """A send card's messages, a page at a time, in order (ApprovalStore.send_messages)."""
        await scoped_get(self.api.svc("approvals").get, access_of(request), path_id(request))  # 404 unless visible
        get = getattr(self.api.svc("approvals"), "messages", None)
        if not callable(get):
            raise ApiError("unavailable", "This node cannot show a send card's messages yet.")
        return jsonable(await maybe_await(get(path_id(request), query_int(request, "page", 0, lo=0, hi=100_000),
                                              query_str(request, "cursor"))))

    async def deny(self, request: Request) -> dict:
        body = await read_body(request, Decision)
        return await self._decide(request, path_id(request), "deny", body.note, body.content_hash)

    async def batch(self, request: Request) -> dict:
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
                approval = await self._decide(request, approval_id, body.decision, body.note,
                                         body.content_hashes.get(approval_id))
                results.append({"id": approval_id, "ok": True, "approval": approval})
            except AgentosError as exc:
                results.append({"id": approval_id, "ok": False,
                                **error_body(exc.code, exc.message, member=access.member is not None, **error_extra(exc))})
        return {"results": results, "ok": all(r["ok"] for r in results)}

    async def approval_review(self, request: Request) -> dict:
        """A Review link from a push opens here. It names the approval and decides nothing: the owner
        approves in the signed-in panel. The link's own signature is what makes it valid."""
        svc, access = self.api.svc("approvals"), access_of(request)
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

    async def approval_clear(self, request: Request) -> dict:
        """"Got it" on an approved action that did not work: it leaves the failed list for everyone. Nothing
        is decided again, and the run keeps the outcome it had. So it takes the right to decide in that
        approval's workspace, the same as a no."""
        svc, access = self.api.svc("approvals"), access_of(request)
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
        self.api.publish("approval", result)
        return result

    async def preview_png(self, request: Request) -> Response:
        svc = self.api.svc("approvals")
        await scoped_get(svc.get, access_of(request), path_id(request))  # visible to this caller
        fn = getattr(svc, "preview_image", None)
        png = await maybe_await(fn(path_id(request))) if fn else None
        if not png:
            raise NotFound("This approval has no image preview.")
        return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})

    async def approval_file(self, request: Request) -> Response:
        """An image an approval card shows (a photo the post attaches), for whoever sees the card."""
        svc = self.api.svc("approvals")
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

    def _standing_mine(self, request: Request, item: dict) -> bool:
        access = access_of(request)
        lanes = item.get("lanes") or ([item["lane"]] if item.get("lane") else [])
        return access.owner or (bool(lanes) and all(isinstance(lane, str) and "/" in lane and access.can(
            "standing", lane.split("/")[0]) for lane in lanes))

    async def standing_list(self, request: Request) -> list[dict]:
        svc = self.api.svc("approvals")
        fn = getattr(svc, "list_standing", None)
        everything = query_str(request, "all") == "true"
        if fn is not None and everything and not _takes(fn, "all_states"):
            raise ApiError("unavailable", "This node cannot list finished standing approvals yet.")
        items = jsonable(await maybe_await(fn(all_states=True) if everything else fn())) if fn else []
        return [i for i in items if self._standing_mine(request, i)]

    async def standing_create(self, request: Request) -> dict:
        """A person present (endpoint: PERSON, and a member's passkey check). Made from approvals, each
        must be one this person could have approved themselves (decision_problem: their role's approve,
        four-eyes), already approved, and for this very lane (ApprovalsAdapter.create_standing)."""
        body = await read_body(request, StandingCreate)
        access = access_of(request)
        workspaces = body.workspaces()
        for workspace in workspaces:
            access.require("standing", workspace, action="standing.create", lane=body.lane)
        fn = getattr(self.api.svc("approvals"), "create_standing", None)
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
        self.api.publish("approval", {"standing": result})
        return result

    async def standing_revoke(self, request: Request) -> dict:
        fn = getattr(self.api.svc("approvals"), "revoke_standing", None)
        if fn is None:
            raise ApiError("unavailable", "Standing approvals are not supported by this node yet.")
        sid = path_id(request)
        if access_of(request).member is not None:
            mine = [i for i in jsonable(await maybe_await(self.api.svc("approvals").list_standing()))
                    if i.get("id") == sid and self._standing_mine(request, i)]
            if not mine:
                raise NotFound(f"No standing approval {sid}.")
        result = await maybe_await(fn(sid, principal=principal_of(request)) if _takes(fn, "principal") else fn(sid))
        access_of(request).trace("standing.revoke", summary=f"Revoke standing approval {sid}")
        return jsonable(result) if result else {"ok": True}

    async def approval_link(self, request: Request) -> dict:
        """The phone's Approve and Deny buttons: a signed, single-approval, expiring link is the
        only credential (the signature, never a grant). Anything else is 401."""
        q = request.query_params
        fn = getattr(self.api.svc("approvals"), "decide_by_link", None)
        if fn is None or not (q.get("d") and q.get("e") and q.get("s")):
            raise ApiError("unauthorized", "This approval link is not valid.")
        try:
            result = jsonable(await maybe_await(fn(path_id(request), q["d"], q["e"], q["s"])))
        except InvalidRequest:
            raise ApiError("unauthorized", "This approval link is not valid.") from None
        self.api.publish("approval", result)
        return result

    # ---- questions (agent questions from human_ask; a question is an approval of kind "question") ----
    async def questions(self, request: Request) -> list[dict]:
        state = query_str(request, "state") or "open"
        if state not in QUESTION_STATES:
            raise InvalidRequest(f"state must be one of {', '.join(QUESTION_STATES)}.")
        items = await scoped_list(self.api.svc("approvals").list, access_of(request), state=QUESTION_STATES[state],
                                  kind="question", limit=query_int(request, "limit", 50))
        return [question_json(i) for i in jsonable(items)]

    async def question_answer(self, request: Request) -> dict:
        body = await read_body(request, QuestionAnswer)
        question_id = path_id(request)
        svc = self.api.svc("approvals")
        access = access_of(request)
        current = jsonable(await (scoped_get(svc.get, access, question_id) if access.member is not None
                                  else maybe_await(svc.get(question_id))))
        if current.get("kind") != "question":
            raise NotFound(f"No question {question_id}.")
        proof = decision_proof(self.api, request, current, "approve")
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
        self.api.publish("question", question)
        return question

    # ---- code cards (agentos.vault.relay): a one-time code typed by the owner, for one login step ----
    async def codes(self, request: Request) -> list[dict]:
        fn = getattr(self.api.svc("vault"), "codes", None)
        return [] if fn is None else jsonable(await maybe_await(fn()))

    async def _code(self, request: Request, action: str, call: str, **kwargs: Any) -> dict:
        fn = getattr(self.api.svc("vault"), call, None)
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

    async def code_answer(self, request: Request) -> dict:
        body = await read_body(request, CodeAnswer)
        return await self._code(request, "code.enter", "submit_code", code=Secret(body.code.get_secret_value()))

    async def code_decline(self, request: Request) -> dict:
        return await self._code(request, "code.decline", "decline_code")
