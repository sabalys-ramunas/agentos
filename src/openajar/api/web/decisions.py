"""What asks a person, and what a person decided (docs/API.md "Needs you").

Needs you (approvals, batch, review, the passkey check that proves a heavy yes), questions, standing
approvals, Ask me first, the confirmation setting and its methods, grants and the vault's names.

Every decision is the node's own route, run here with the same cookie and CSRF header (`write`), so who
may decide, the floors on a yes and the self-approval refusal all stay where they are. This module only
reshapes what the node answers into the app's shapes (web/src/api/types.ts). Anything the node does not
keep (an undo window, say) is answered as the node really is: refused, or null, never made up.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute

from openajar.api.app import Api
from openajar.api.errors import ApiError, error_body
from openajar.api.web import cardwords, telling
from openajar.api.web.base import call, org_route, read, unwrap, write
from openajar.core.errors import InvalidRequest

# A passkey check that proves a heavy yes is for one approval, and the node records which one.
PROOF_OPTIONS = "/api/session/verify/options"
PROOF = "/api/session/verify"


class Ctx:
    """What a decision read needs once per request: the run each approved action started, and which
    failed outcomes nobody has cleared yet (the node's own failed list)."""

    def __init__(self, runs: dict[str, dict[str, Any]], failed: set[str], workspaces: dict[str, str],
                 people: dict[str, str] | None = None, member_ids: dict[str, str] | None = None):
        self.runs = runs
        self.failed = failed
        self.workspaces = workspaces
        self.people = people or {}  # member id to their name in words, for who asked and who decided
        self.member_ids = member_ids or {}  # a member's handle to their member id, as the node writes a yes


def member_names(api: Api) -> tuple[dict[str, str], dict[str, str]]:
    """Each member's name in words, by member id (who asked) and by handle (who decided: the node records a
    member's yes or no under their handle, for Activity), and each handle's member id. The name is their display
    name, else their handle. Read from the node's own member store, because /api/members is an admin's list and a
    member may not read it. The owner is not in it: the app says "The owner" itself. A removed member stays named,
    as the decision they made still is; an active member wins a handle that a removed one once had."""
    names: dict[str, str] = {}
    ids: dict[str, str] = {}
    for m in sorted(api.members.store.members(include_removed=True), key=lambda m: m.state == "removed"):
        if m.owner:
            continue
        name = m.display_name or m.handle
        names.setdefault(m.id, name)
        names.setdefault(m.handle, name)
        ids.setdefault(m.handle, m.id)
    return names, ids


async def context(request: Request, *, approvals: bool = True) -> Ctx:
    runs: dict[str, dict[str, Any]] = {}
    failed: set[str] = set()
    if approvals:
        for run in await read(request, "/api/runs", query={"limit": 200}):  # newest first
            if run.get("approval_id"):
                runs.setdefault(run["approval_id"], run)
        failed = {item["id"] for item in await read(request, "/api/approvals", query={"outcome": "needs_you", "limit": 200})}
    names = {row["id"]: row.get("name") or row["id"] for row in await read(request, "/api/workspaces")}
    people, member_ids = member_names(request.app.state.api)
    return Ctx(runs, failed, names, people, member_ids)


def is_question(item: dict[str, Any]) -> bool:
    return item.get("kind") == "question"


IMAGE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def preview_image(item: dict[str, Any]) -> str | None:
    """The picture a review shows, as the app's URL under the org. The node's own preview picture when it
    has one (`preview.png`), else the first image a card attaches (`files/{name}`). Nothing when it has
    neither: the card then says what it says, and no broken picture is drawn."""
    approval_id = item["id"]
    preview = item.get("preview") or {}
    if isinstance(preview, dict) and preview.get("image"):
        return f"/api/orgs/local/approvals/{approval_id}/preview.png"
    card = item.get("card")
    for image in (card.get("images") or []) if isinstance(card, dict) else []:
        name = image.get("name") if isinstance(image, dict) else None
        if isinstance(name, str) and IMAGE_NAME.match(name):
            return f"/api/orgs/local/approvals/{approval_id}/files/{name}"
    return None


def approval_out(item: dict[str, Any], ctx: Ctx) -> dict[str, Any]:
    """A node approval as the app reads it (types.ts `Approval`). A pending one needs a person; an approved
    one needs one while its run failed and nobody has said "Got it"."""
    approved = item.get("state") == "approved"
    outcome = telling.outcome_for(ctx.runs.get(item["id"])) if approved else None
    requester = item.get("requested_by") or {}
    detail = item.get("decided_detail") or {}
    return {
        "id": item["id"],
        "kind": "action",
        "state": item.get("state"),
        "effect": item.get("effect"),
        "lane": item.get("lane") or "",
        "summary": item.get("summary") or "",
        "content_hash": item.get("content_hash") or "",
        # in words, from the node's card: never the raw record it binds (cardwords)
        "preview": {
            **cardwords.preview_of(item, telling.site_of(item.get("lane"))[1]),
            **({"image": image} if (image := preview_image(item)) else {}),
        },
        # `name` is a member's name in words (None for the owner or an agent): the app never shows a member's id
        "requested_by": {"kind": requester.get("kind"), "id": requester.get("id"), "thread": requester.get("thread"),
                         "name": ctx.people.get(requester.get("id") or "") if requester.get("kind") == "human" else None},
        "created_at": item.get("created_at"),
        "expires_at": item.get("expires_at"),
        "tool": item.get("tool"),
        "flow_target": item.get("flow_target"),
        "reason": item.get("reason"),
        "content_digest": item.get("content_digest"),
        "decided_by": item.get("decided_by"),
        # who decided, as an id the app can match with the signed-in person: a member's id, "owner", or the node's word
        "decided_by_id": ctx.member_ids.get(item["decided_by"], item["decided_by"]) if item.get("decided_by") else None,
        "decided_by_name": ctx.people.get(item.get("decided_by") or ""),
        "decided_at": item.get("decided_at"),
        "requires": item.get("requires"),
        "methods": item.get("methods"),
        "decided_method": item.get("decided_method"),
        "decided_level": item.get("decided_level"),
        "decided_where": detail.get("client") if isinstance(detail, dict) else None,
        "workspace": item.get("workspace"),
        "workspace_name": ctx.workspaces.get(item.get("workspace") or ""),
        "outcome": outcome,
        "needs_you": item.get("state") == "pending" or item["id"] in ctx.failed,
        "undo_until": None,  # the node keeps no undo window: nothing is ever offered back
    }


def _no_such(kind: str) -> ApiError:
    return ApiError("not_found", f"No {kind} here.")


async def _approval(request: Request, approval_id: str) -> dict[str, Any]:
    """One approval the node answers, and never a question: those are asked on /questions."""
    item = await read(request, f"/api/approvals/{approval_id}")
    if is_question(item):
        raise _no_such("approval")
    return item


class Handlers:
    """The handlers of the routes below, sharing one Api (and what they keep between calls)."""

    def __init__(self, api: Api) -> None:
        self.api = api

    async def approvals(self, request: Request) -> list[dict[str, Any]]:
        params = {"state": request.query_params.get("state") or "pending",
                  "limit": request.query_params.get("limit") or 50}
        if request.query_params.get("outcome"):
            params = {"outcome": request.query_params["outcome"], "limit": params["limit"]}
        items = [item for item in await read(request, "/api/approvals", query=params) if not is_question(item)]
        ctx = await context(request)
        return [approval_out(item, ctx) for item in items]

    async def approval(self, request: Request) -> dict[str, Any]:
        item = await _approval(request, request.path_params["id"])
        return approval_out(item, await context(request))

    async def batch(self, request: Request) -> list[dict[str, Any]]:
        body = await _body(request)
        ids = list(body.get("ids") or [])
        reply = await write(request, "POST", "/api/approvals/batch",
                            {"ids": ids, "decision": body.get("decision"), "content_hashes": body.get("content_hashes") or {}})
        ctx = await context(request)
        out: list[dict[str, Any]] = []
        for result in reply.get("results") or []:
            if result.get("ok") and isinstance(result.get("approval"), dict):
                out.append({**approval_out(result["approval"], ctx), "ok": True})
            else:
                out.append({"id": result.get("id"), "ok": False, "error": result.get("error") or {
                    "code": "error", "message": "No answer came back for this item, so treat it as not decided."}})
        return out

    async def clear(self, request: Request) -> dict[str, Any]:
        approval_id = request.path_params["id"]
        await _approval(request, approval_id)
        item = await write(request, "POST", f"/api/approvals/{approval_id}/clear")
        return approval_out(item, await context(request))  # the node's failed list no longer has it

    async def undo(self, request: Request) -> JSONResponse:
        """The node keeps no undo window. A decision is final there, so the answer says so, and says what to do
        instead. (The catalog's fix for `unavailable` is about setting a feature up, so this answer carries its own.)"""
        await _approval(request, request.path_params["id"])
        return JSONResponse(error_body("unavailable", "This computer cannot take a decision back. The decision stands.",
                                       fix="If it was the wrong answer, tell the person who asked, and they can ask again."),
                            status_code=503)

    async def review(self, request: Request) -> dict[str, Any]:
        """Where a Review link lands. With the link's signature (`e`, `s`) the node checks it; a signed-in
        person without one reads the approval they may see, as `GET /approvals/{id}` gives it. Decides nothing."""
        approval_id = request.path_params["id"]
        item = await _approval(request, approval_id)
        if request.query_params.get("e") or request.query_params.get("s"):
            query = {"e": request.query_params.get("e"), "s": request.query_params.get("s")}
            item = await read(request, f"/api/approvals/{approval_id}/review", query=query)
        return approval_out(item, await context(request))

    async def proof_options(self, request: Request) -> dict[str, Any]:
        """The passkey challenge for one approval: the browser signs it, and `proof` takes the answer."""
        approval_id = request.path_params["id"]
        await _approval(request, approval_id)
        return unwrap(await call(request, "POST", PROOF_OPTIONS, body={"approval_id": approval_id}))["publicKey"]

    async def proof(self, request: Request) -> dict[str, Any]:
        approval_id = request.path_params["id"]
        await _approval(request, approval_id)
        credential = await _body(request)
        checked = await write(request, "POST", PROOF, {"credential": credential})
        if checked.get("for_approval") != approval_id:
            raise InvalidRequest("This passkey check was not made for this approval. Check again.")
        return {"ok": True, "level": "present"}

    async def standing_list(self, request: Request) -> list[dict[str, Any]]:
        everything = request.query_params.get("all") == "true"
        items = await read(request, "/api/approvals/standing", query={"all": "true" if everything else None})
        return [standing_out(item) for item in items]

    async def standing_create(self, request: Request) -> dict[str, Any]:
        body = await _body(request)
        scope = _standing_scope(body)
        ttl = _seconds_until(body.get("until"))
        reply = await write(request, "POST", "/api/approvals/standing", {
            **scope,
            "effect": body.get("effect") or "commit",
            "ttl_seconds": ttl,
            "count": body.get("count"),
            "per_day": body.get("per_day"),
            "targets": body.get("targets") or [],
            "from_approvals": [ref["id"] for ref in body.get("from_approvals") or []],
            "note": body.get("note"),
        })
        return standing_out(reply)

    async def standing_delete(self, request: Request) -> dict[str, Any]:
        await write(request, "DELETE", f"/api/approvals/standing/{request.path_params['id']}")
        return {"ok": True}

    async def standing_support(self, request: Request) -> dict[str, Any]:
        workspace = request.query_params.get("workspace") or ""
        rows = await read(request, "/api/workspaces")
        row = next((item for item in rows if item.get("id") == workspace), None)
        accounts = len(row.get("lanes") or []) if row else 0
        if accounts == 0:
            return {"state": "no_lanes", "applied_by": [], "not_applied_by": [],
                    "message": "This computer has no accounts in this workspace yet, so nothing is covered."}
        return {"state": "applied", "applied_by": ["this computer"], "not_applied_by": [],
                "message": "This computer checks it before each action that would go out, until it runs out or ends."}

    async def confirmation(self, request: Request) -> dict[str, Any]:
        return await _confirmation_view(request, await read(request, "/api/confirmation"))

    async def set_confirmation(self, request: Request) -> dict[str, Any]:
        body = await _body(request)
        try:
            if "setting" in body:
                patch = _setting_patch(body["setting"])
                # the app shows chat only: a method it does not show (telegram) stays as the node holds it
                held = (await read(request, "/api/confirmation")).get("methods_off") or []
                patch["methods_off"] += [m for m in held if m != "chat" and m not in patch["methods_off"]]
            else:
                held = await read(request, "/api/confirmation")
                patch = {"workspaces": _workspaces_with(held, str(body["business"]), body.get("overlay") or {})}
        except (KeyError, TypeError, AttributeError):
            raise InvalidRequest("The setting names every row: commit, pay, irreversible and after_untrusted.") from None
        return await _confirmation_view(request, await write(request, "PUT", "/api/confirmation", patch))

    async def confirmation_methods(self, request: Request) -> list[dict[str, Any]]:
        decided = await _decided(request)
        last: dict[str, str] = {}
        for item in sorted(decided, key=lambda i: str(i.get("decided_at") or ""), reverse=True):
            if item.get("decided_method"):
                last.setdefault(item["decided_method"], item.get("decided_at"))
        clients = await read(request, "/api/confirmation")
        out = []
        for method in await read(request, "/api/confirmation/methods"):
            ready = method["status"] == "ready" and bool(method.get("on"))
            # a method reaches this person when it is ready and the person has it: this computer's own
            # ways always, and a chat app only once one has answered here
            mine = method["id"] in ("panel", "cli", "passkey") or (method["id"] == "chat" and bool(clients.get("chat_clients")))
            out.append({"id": method["id"], "level": method["level"], "status": method["status"],
                        "yours": bool(ready and mine), "detail": _method_detail(method["id"], clients),
                        "last_used_at": last.get(method["id"])})
        return out

    async def grants(self, request: Request) -> list[dict[str, Any]]:
        return [grant_out(item) for item in await read(request, "/api/grants")]

    async def create_grant(self, request: Request) -> dict[str, Any]:
        body = await _body(request)
        if body.get("budget"):
            raise InvalidRequest("A budget is set on the computer, not here yet. Leave it out.")
        issued = await write(request, "POST", "/api/grants", {
            "subject": body.get("subject"), "workspaces": body.get("workspaces") or [],
            "lanes": body.get("lanes") or None, "ceiling": body.get("ceiling") or "read",
            "specials": body.get("specials") or [], "budget": None, "ttl": body.get("ttl") or "7d"})
        return {"id": issued["id"], "token": issued["token"]}


def routes(api: Api) -> list[BaseRoute]:
    h = Handlers(api)

    def decider(decision: str) -> Any:
        async def decide(request: Request) -> dict[str, Any]:
            approval_id = request.path_params["id"]
            await _approval(request, approval_id)  # not a question, and visible to this caller
            body = await _body(request)
            item = await write(request, "POST", f"/api/approvals/{approval_id}/{decision}",
                               {k: body[k] for k in ("content_hash", "note", "edit") if body.get(k) is not None})
            return approval_out(item, await context(request))

        return decide

    # ---- questions: the node's own list and answer routes already say what the app reads ----

    # ---- the confirmation setting ----

    # ---- grants ----

    return [
        org_route(api, "/approvals", h.approvals, ["GET"]),
        org_route(api, "/approvals/batch", h.batch, ["POST"]),
        org_route(api, "/approvals/{id}", h.approval, ["GET"]),
        org_route(api, "/approvals/{id}/approve", decider("approve"), ["POST"]),
        org_route(api, "/approvals/{id}/deny", decider("deny"), ["POST"]),
        org_route(api, "/approvals/{id}/clear", h.clear, ["POST"]),
        org_route(api, "/approvals/{id}/undo", h.undo, ["POST"]),
        org_route(api, "/approvals/{id}/review", h.review, ["GET"]),
        org_route(api, "/approvals/{id}/proof/options", h.proof_options, ["POST"]),
        org_route(api, "/approvals/{id}/proof", h.proof, ["POST"]),
        org_route(api, "/standing-approvals", h.standing_list, ["GET"]),
        org_route(api, "/standing-approvals", h.standing_create, ["POST"]),
        org_route(api, "/standing-approvals/support", h.standing_support, ["GET"]),
        org_route(api, "/standing-approvals/{id}", h.standing_delete, ["DELETE"]),
        org_route(api, "/confirmation", h.confirmation, ["GET"]),
        org_route(api, "/confirmation", h.set_confirmation, ["PUT"]),
        org_route(api, "/confirmation/methods", h.confirmation_methods, ["GET"]),
        org_route(api, "/grants", h.grants, ["GET"]),
        org_route(api, "/grants", h.create_grant, ["POST"]),
    ]


async def _body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        raise InvalidRequest("The request body must be JSON.") from None
    if not isinstance(body, dict):
        raise InvalidRequest("The request body must be an object.")
    return body


def _seconds_until(stamp: Any) -> int:
    """The seconds from now until an instant the app sends, as the node takes a standing approval's
    window. A past or too-short window is the node's refusal to give, not this route's."""
    if not isinstance(stamp, str):
        raise InvalidRequest("until is the day the approval stops, as a time.")
    try:
        until = datetime.fromisoformat(stamp)
    except ValueError:
        raise InvalidRequest("until is the day the approval stops, as a time.") from None
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    return int((until - datetime.now(UTC)).total_seconds())


def _standing_scope(body: dict[str, Any]) -> dict[str, Any]:
    lanes = body.get("lanes")
    if lanes:
        return {"lanes": list(lanes)}
    if body.get("workspace"):
        return {"workspace": body["workspace"]}
    raise InvalidRequest("Name the accounts it covers: some of them, or every one in the workspace.")


ENFORCED = {"state": "applied", "applied_by": ["this computer"], "not_applied_by": [],
            "message": "This computer checks it before each action that would go out, until it runs out or ends."}


def standing_out(item: dict[str, Any]) -> dict[str, Any]:
    """A standing approval as the app reads it (types.ts `StandingApproval`)."""
    workspace = next(iter(item.get("workspaces") or []), None) or (item.get("lane") or "").split("/", 1)[0]
    state = {"withdrawn": "revoked"}.get(item.get("state") or "active", item.get("state") or "active")
    return {
        "id": item["id"],
        "workspace": workspace,
        "effect": item.get("effect") or "commit",
        "lanes": item.get("lanes_only"),
        "count": item.get("count"),
        "used": item.get("used") or 0,
        "remaining": item.get("remaining"),
        "per_day": item.get("per_day"),
        "until": item.get("expires_at"),
        "targets": item.get("targets") or [],
        "contents": item.get("contents") or 0,
        "covers_publishing": bool(item.get("covers_publishing")),
        "note": item.get("note"),
        "state": state,
        "created_at": item.get("created_at"),
        "revoked_at": item.get("revoked_at"),
        "enforcement": ENFORCED,
    }


def grant_out(item: dict[str, Any]) -> dict[str, Any]:
    """A grant as the app reads it (types.ts `Grant`). No token is ever in a grant."""
    specials = item.get("specials") or []
    budget = item.get("budget")
    words = None
    if isinstance(budget, dict):
        parts = [f"{budget[key]} {name}" for key, name in (("usd", "USD"), ("tokens", "tokens")) if budget.get(key) is not None]
        words = ", ".join(parts) or None
    return {
        "id": item["id"],
        "subject": item.get("subject") or "",
        "workspaces": item.get("workspaces") or [],
        "lanes": item.get("lanes"),
        "ceiling": item.get("ceiling") or "read",
        "specials": list(specials) if isinstance(specials, list) else list(specials.keys()),
        "budget": words,
        "expires_at": item.get("expires_at"),
        "revoked": bool(item.get("revoked")),
        "created_at": item.get("issued_at") or "",
    }


def _setting(config: dict[str, Any]) -> dict[str, Any]:
    """The node's rows as the app's setting. `chat` is on unless the node has turned that method off."""
    return {
        "commit": {"when": (config.get("commit") or {}).get("when", "auto"), "level": (config.get("commit") or {}).get("level")},
        "pay": {"when": "ask", "level": (config.get("pay") or {}).get("level")},
        "irreversible": {"when": "ask", "level": (config.get("irreversible") or {}).get("level")},
        "after_untrusted": {"level": (config.get("after_untrusted") or {}).get("level")},
        "chat": "chat" not in (config.get("methods_off") or []),
    }


def _overlay(rows: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if rows.get("commit") is not None:
        out["commit"] = {"when": rows["commit"].get("when") or "auto", "level": rows["commit"].get("level")}
    for name in ("pay", "irreversible"):
        if rows.get(name) is not None:
            out[name] = {"when": "ask", "level": rows[name].get("level")}
    if rows.get("after_untrusted") is not None:
        out["after_untrusted"] = {"level": rows["after_untrusted"].get("level")}
    return out


async def _decided(request: Request) -> list[dict[str, Any]]:
    """Recent yes and no answers, newest first in the node's list order. Questions are not decisions."""
    approved = await read(request, "/api/approvals", query={"state": "approved", "limit": 200})
    denied = await read(request, "/api/approvals", query={"state": "denied", "limit": 200})
    return [item for item in [*approved, *denied] if not is_question(item)]


async def _confirmation_view(request: Request, config: dict[str, Any]) -> dict[str, Any]:
    ctx = await context(request, approvals=False)  # only the workspace names are needed here
    decided = await _decided(request)
    return {
        "setting": _setting(config),
        "system": _setting(config.get("defaults") or {}),
        "overlays": {name: _overlay(rows) for name, rows in (config.get("workspaces") or {}).items()},
        "day": _yesterday(decided, ctx, request.app.state.api.timezone),
        "clients": _clients(config.get("chat_clients") or [], decided),
    }


def _yesterday(decided: list[dict[str, Any]], ctx: Ctx, timezone: str) -> dict[str, Any]:
    """What a person was asked yesterday, in the node's own time: the stops that a person answered.
    The ticks are left empty: the node does not keep a record of each look that ran on its own."""
    zone = ZoneInfo(timezone)
    day = datetime.now(zone).date() - timedelta(days=1)
    stops = []
    for item in decided:
        stamp = item.get("decided_at")
        method = item.get("decided_method")
        if not stamp or not method:
            continue  # nobody answered: a bearer token or a push, which is not a person's yes
        when = datetime.fromisoformat(str(stamp)).astimezone(zone)
        if when.date() != day:
            continue
        lane = item.get("lane") or ""
        requester = item.get("requested_by") or {}
        stops.append({
            "id": item["id"],
            "minute": when.hour * 60 + when.minute,
            "row": _row_of(item),
            "business": ctx.workspaces.get(item.get("workspace") or "", item.get("workspace") or ""),
            "site": telling.site_of(lane)[1],
            "title": item.get("summary") or "",
            "who": telling.agent_name(requester.get("id")),
            "said": "yes" if item.get("state") in ("approved", "consumed") else "no",
            "method": method,
            "where": (item.get("decided_detail") or {}).get("client"),
        })
    stops.sort(key=lambda stop: stop["minute"])
    return {"label": "Yesterday", "date": day.isoformat(), "ticks": [], "stops": stops}


def _row_of(item: dict[str, Any]) -> str:
    if item.get("effect") == "pay":
        return "pay"
    if item.get("effect") in ("irreversible", "install_base", "install_overlay", "service", "secret", "exec"):
        return "irreversible"
    if item.get("follows"):
        return "after_untrusted"
    return "marked"


def _clients(chat: list[dict[str, Any]], decided: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The chat apps that answered here, with the agent that asked most recently through each."""
    asked: dict[str, str] = {}
    for item in sorted(decided, key=lambda i: str(i.get("decided_at") or ""), reverse=True):
        client = (item.get("decided_detail") or {}).get("client")
        if item.get("decided_method") == "chat" and client:
            asked.setdefault(client, (item.get("requested_by") or {}).get("id") or "")
    return [{"name": row["client"], "agent": telling.agent_name(asked.get(row["client"])) if asked.get(row["client"]) else "",
             "answers": row["decisions"], "last_at": row["last"]} for row in chat if row.get("client")]


def _method_detail(method_id: str, clients: dict[str, Any]) -> str | None:
    if method_id == "chat":
        names = [row["client"] for row in clients.get("chat_clients") or [] if row.get("client")]
        return ", ".join(names) or None
    if method_id in ("panel", "cli", "passkey"):
        return "This computer"
    return None


def _setting_patch(setting: dict[str, Any]) -> dict[str, Any]:
    """The app's whole setting, as the node's `change` takes it. Chat is a method that can be turned off."""
    return {
        "commit": {"when": setting["commit"]["when"], "level": setting["commit"]["level"]},
        "pay": {"level": setting["pay"]["level"]},
        "irreversible": {"level": setting["irreversible"]["level"]},
        "after_untrusted": {"level": setting["after_untrusted"]["level"]},
        "methods_off": [] if setting.get("chat", True) else ["chat"],
    }


def _workspaces_with(held: dict[str, Any], business: str, overlay: dict[str, Any]) -> dict[str, Any]:
    """Every workspace the node holds, with this one's rows as the app set them. The node replaces the
    whole map on a change, so the other workspaces go back as they are, and this one keeps the ways to
    confirm turned off in it (the app does not show them)."""
    own = held.get("own_workspaces")
    if own is None:  # a node from before own_workspaces: what is in force is the closest it says
        own = held.get("workspaces") or {}
    out = {str(name): {k: v for k, v in (rows or {}).items() if v is not None} for name, rows in own.items()}
    off = (out.get(business) or {}).get("methods_off") or []
    out[business] = {**_overlay_patch(overlay), **({"methods_off": list(off)} if off else {})}
    return out


def _overlay_patch(overlay: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, value in overlay.items():
        if name == "after_untrusted":
            out[name] = {"level": value["level"]}
        elif name == "commit":
            out[name] = {"when": value["when"], "level": value["level"]}
        else:
            out[name] = {"level": value["level"]}
    return out
