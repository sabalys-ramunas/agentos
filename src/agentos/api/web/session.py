"""Who is signed in, and how they get there.

The cloud signs people in by email, Google or passkey. A node signs its owner in with a one-time
code (`agentos panel login`) and everyone with a passkey, so `GET /api/auth/providers` says `code`
and the sign-in page offers that. The code itself still goes to the node's `POST /api/session`.
"""

from __future__ import annotations

import time
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute

from agentos.api.app import Api
from agentos.api.auth import SESSION_COOKIE
from agentos.api.errors import ApiError
from agentos.api.web.base import ORG, Refused, Reply, call, plain_route, read, unwrap
from agentos.core import clock

# The app's roles, highest first, and the node's role each one stands for.
ROLE_OF = {"admin": "admin", "operator": "member", "approver": "approver", "viewer": "viewer", "auditor": "auditor"}
ORDER = ("admin", "member", "approver", "viewer", "auditor")


def iso(stamp: float | None) -> str | None:
    return clock.iso_or_none(stamp, z=True)


def role_of(me: dict[str, Any]) -> str:
    """One role for the app. A node's roles are per workspace, and the app shows the highest."""
    if (me.get("principal") or {}).get("owner"):
        return "owner"
    held = {ROLE_OF.get(item.get("role"), "viewer") for item in me.get("roles") or []}
    return next((role for role in ORDER if role in held), "viewer")


def org_name(api: Api) -> str:
    """The name the app shows for this node: the one it was given, else the node's own name."""
    return api.store.setting("org.name") or api.node_name


def public_user(api: Api, me: dict[str, Any]) -> dict[str, Any]:
    """The person behind `GET /api/me`, as the app's `Session.user`."""
    member = me.get("member") or {}
    return {
        "id": member.get("id") or "owner",
        "email": member.get("email") or "",
        "name": member.get("display_name") or member.get("handle") or "Owner",
        "email_verified": True,
        "has_passkey": bool(member.get("id") and api.members.store.credentials(member["id"])),
        "avatar_color": None,
    }


def public_session(api: Api, me: dict[str, Any], status: dict[str, Any], session: Any) -> dict[str, Any]:
    verified = getattr(session, "last_verified_at", None)
    until = verified + api.settings.step_up_window if verified else None
    return {
        "user": public_user(api, me),
        "csrf": session.csrf,
        "step_up_until": iso(until) if until and until > time.time() else None,
        "expires_at": iso(session.expires_at),
        "orgs": [{
            "id": ORG,
            "slug": ORG,
            "name": org_name(api),
            "role": role_of(me),
            "plan": "self_hosted",
            "needs_you": status.get("needs_you_all", status.get("needs_you", 0)),
        }],
    }


async def signed_in(api: Api, request: Request, reply: Reply) -> Response:
    """A node route just made a session (its answer sets the cookie). Read that session as the app
    sees it, and pass the cookie on."""
    cookie = (reply.header("set-cookie") or "").split(";", 1)[0]
    sid = cookie.split("=", 1)[1] if cookie.startswith(f"{SESSION_COOKIE}=") else None
    live = api.store.get_session(sid) if sid else None
    if live is None:
        raise ApiError("unauthorized", "That sign-in did not finish. Try again.")
    me = unwrap(await call(request, "GET", "/api/me", cookie=cookie))
    status = unwrap(await call(request, "GET", "/api/status", cookie=cookie))
    out = JSONResponse(public_session(api, me, status, live))
    out.headers["set-cookie"] = reply.header("set-cookie") or ""
    return out


def routes(api: Api) -> list[BaseRoute]:
    async def session(request: Request) -> dict[str, Any]:
        auth = request.state.auth
        if auth.session is None:
            raise ApiError("unauthorized", "Sign in first. Run `agentos panel login` on this computer.")
        return public_session(api, await read(request, "/api/me"), await read(request, "/api/status"), auth.session)

    async def providers(request: Request) -> dict[str, Any]:
        del request
        return {"magic_link": False, "google": False, "passkey": True, "code": True}

    async def passkey_options(request: Request) -> dict[str, Any]:
        return unwrap(await call(request, "POST", "/api/session/passkey/options"))["publicKey"]

    async def passkey_verify(request: Request) -> Response:
        credential = await request.json()
        reply = await call(request, "POST", "/api/session/passkey", body={"credential": credential})
        if reply.status >= 300:
            raise Refused(reply)
        return await signed_in(api, request, reply)

    return [
        plain_route(api, "/api/session", session, ["GET"]),
        plain_route(api, "/api/auth/providers", providers, ["GET"], public=True),
        plain_route(api, "/api/auth/passkey/login/options", passkey_options, ["POST"], public=True),
        plain_route(api, "/api/auth/passkey/login/verify", passkey_verify, ["POST"], public=True),
    ]
