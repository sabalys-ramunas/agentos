"""People and sign-in on a node: passkeys, sessions, my details, the organisation this node is, its
members and invites, notifications, phone push, and Getting started.

The app's calls are in `web/src/api/client.ts`. A node has no email, so every route that would send
one answers a clear refusal, and the app offers a passkey or a code from the computer instead
(`GET /api/auth/providers` says so). A node is one organisation with one owner. Its roles are per
workspace, so the app shows each person one role, the highest they hold (`session.role_of`).

Roles the app gives map onto the node's: admin stays admin, member is the node's operator, and
approver, viewer and auditor stay themselves. The owner is not given: this computer has one owner.
A person's roles differ by business, so each one also comes with `roles_by_workspace`, and a change
names the business it is for (`workspace`); a business not named keeps the role it has.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import BaseRoute

from openajar.api.app import Api
from openajar.api.errors import ApiError
from openajar.api.members import OWNER_ID
from openajar.api.routes_core import workspace_meta
from openajar.api.web.base import ORG, Refused, call, org_route, plain_route, read, write
from openajar.api.web.session import ORDER, ROLE_OF, iso, org_name, public_user, signed_in

NODE_ROLE = {"admin": "admin", "member": "operator", "approver": "approver", "viewer": "viewer", "auditor": "auditor"}
"""The app's role, as the node writes it. `ROLE_OF` reads it back."""

GOAL_TITLES = {
    "post": "Post for a business",
    "email": "Handle email",
    "invoices": "Collect invoices and receipts",
    "research": "Research and keep a list",
    "forms": "Fill in forms on websites",
    "other": "Something else",
}

# Getting started: key, title, what to do, minutes (as web/src/features/onboarding/setup.ts says them).
SETUP_ITEMS = (
    ("goal", "Choose a first job", "Tell openajar what your AI should do first.", 1),
    ("computer", "Add a computer", "Your AI works on a computer of its own.", 5),
    ("account", "Sign in to an account", "Sign in once to the site your AI will use.", 2),
    ("ai", "Connect your AI", "Connect Claude or ChatGPT so it can use the computer.", 3),
    ("approval", "Decide a first approval", "Say yes or no to the first thing it asks.", 1),
    ("phone", "Add your phone", "Hear about what needs you, wherever you are.", 2),
    ("invite", "Invite someone to approve", "Someone else can say yes too.", 1),
)
# Phone push is not set up on a computer. The push settings and Getting started both read this one fact.
PHONE_PUSH_HERE = False

INVITE_STATE = {"pending": "pending", "used": "accepted", "revoked": "revoked", "expired": "expired"}
VIA_LABEL = {
    "code": "Signed in with a code",
    "passkey": "Signed in with a passkey",
    "device": "Signed in with openajar connect",
}
DEFAULT_QUIET = ("21:00", "08:00")

NOT_BY_EMAIL = (
    "This computer cannot send email, so there is no link to send. Sign in with a passkey, "
    "or with a code from this computer."
)
NOT_BY_EMAIL_STEP_UP = (
    "This computer cannot send email, so there is no link to send. Confirm with your passkey instead."
)
INVITE_NEEDS_PASSKEY = (
    "An invite on this computer is accepted with a passkey. Open the invite link to add one."
)
ONE_ORG = "This computer is one organisation, and it is already here."
ORG_STAYS = "This computer is the organisation, so it cannot be deleted from the app. Remove openajar from this computer to end it."
EMAIL_OFF = "This computer cannot send email, so an email digest or an urgent email cannot be turned on."
PUSH_OFF = "Phone push is not set up on this computer."
NO_PUSH_DEVICES = "Phone push is not set up on this computer, so there is no device to add."
NO_PUSH_TEST = "Phone push is not set up on this computer, so there is nothing to test."


# ---- shapes -------------------------------------------------------------------------------------


def app_role(roles: list[dict[str, Any]], owner: bool = False) -> str:
    """One role for a person: the owner's, else the highest of the node roles they hold."""
    if owner:
        return "owner"
    held = {ROLE_OF.get(item.get("role"), "viewer") for item in roles}
    return next((role for role in ORDER if role in held), "viewer")


def scopes_of(roles: list[dict[str, Any]]) -> list[str] | None:
    """The businesses a person can see: None when a role covers every workspace (`*`)."""
    scopes = list(dict.fromkeys(item["scope"] for item in roles))
    return None if "*" in scopes else scopes


def words(items: list[str]) -> str:
    """ "A", "A and B", "A, B and C": for a sentence the app shows."""
    if len(items) <= 1:
        return items[0] if items else ""
    return f"{', '.join(items[:-1])} and {items[-1]}"


def app_member(item: dict[str, Any]) -> dict[str, Any]:
    owner = bool(item.get("owner"))
    roles = item.get("roles") or []
    return {
        "id": item["id"],
        "user": {
            "id": item["id"],
            "email": item.get("email") or "",
            "name": item.get("display_name") or item.get("handle") or "Member",
        },
        "role": app_role(roles, owner),
        "workspaces": None if owner else scopes_of(roles),
        # The role in each business (`*`: every one). A person's own role can differ by business.
        "roles_by_workspace": [] if owner else [
            {"workspace": item_role["scope"], "role": ROLE_OF.get(item_role.get("role"), "viewer")} for item_role in roles
        ],
        "joined_at": item["created_at"],
        # The node does not keep when a person was last here, and says so with null.
        "last_active_at": None,
    }


def app_invite(item: dict[str, Any]) -> dict[str, Any]:
    roles = item.get("roles") or []
    return {
        "id": item["id"],
        "email": item.get("email") or "",
        "role": app_role(roles),
        "workspaces": scopes_of(roles),
        "invited_by": item.get("created_by") or "",
        "created_at": item["created_at"],
        "expires_at": item["expires_at"],
        "state": INVITE_STATE.get(item.get("state"), "expired"),
    }


def app_passkey(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"],
        "name": item.get("label") or "Passkey",
        "created_at": item["created_at"],
        "last_used_at": item.get("last_used_at"),
    }


def app_device(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"],
        "label": item.get("device_label") or VIA_LABEL.get(item.get("via"), "Signed in"),
        # The node does not keep the address a session came from, so it says so with null.
        "ip": None,
        "location": None,
        "last_active_at": item["last_seen_at"],
        "current": bool(item.get("current")),
    }


def app_notify(node: dict[str, Any]) -> dict[str, Any]:
    channels = node.get("channels") or []
    quiet = node.get("quiet_hours")
    start, end = quiet if quiet else DEFAULT_QUIET
    zone = node.get("timezone") or "UTC"
    return {
        "timezone": zone,
        "timezone_source": "person",
        "push": "ntfy" in channels or node.get("channel") == "ntfy",
        # A node sends no email. These are off, and a change to them is refused.
        "email_digest": "off",
        "email_urgent": False,
        "quiet_hours": {
            "enabled": quiet is not None,
            "from": start,
            "to": end,
            "timezone": zone,
            "urgent_breaks_through": bool(node.get("urgent_breaks_quiet")),
        },
        "batch_minutes": round((node.get("batch_window_s") or 0) / 60),
    }


def org_json(api: Api) -> dict[str, Any]:
    owner = api.members.store.require(OWNER_ID)
    return {
        "id": ORG,
        "slug": ORG,
        "name": org_name(api),
        "plan": "self_hosted",
        "created_at": iso(owner.created_at),
        "timezone": None,
        "goal": api.store.setting("org.goal"),
        "goal_note": api.store.setting("org.goal_note"),
    }


# ---- helpers ------------------------------------------------------------------------------------


async def body_of(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except ValueError:
        raise ApiError("invalid", "The request body must be JSON.") from None
    if not isinstance(data, dict):
        raise ApiError("invalid", "The request body must be a JSON object.")
    return data


async def credential_of(request: Request) -> dict[str, Any]:
    """A passkey's answer as the body: an empty one is a missing answer, not a refused one."""
    data = await body_of(request)
    if not data:
        raise ApiError("invalid", "The passkey answer is missing. Try again.")
    return data


async def optional(request: Request, path: str, query: dict[str, Any] | None = None) -> Any:
    """A read that may be refused for this person (a viewer cannot see everything). None when refused."""
    try:
        return await read(request, path, query=query)
    except Refused:
        return None


def scopes_for(workspaces: Any) -> list[str]:
    """The node scopes for the app's `workspaces`: every workspace (`*`) for null, else the ids given."""
    if workspaces is None:
        return ["*"]
    if not (isinstance(workspaces, list) and all(isinstance(item, str) for item in workspaces)):
        raise ApiError("invalid", "Businesses are a list of business ids.")
    return list(dict.fromkeys(workspaces))


VIA_WORDS = {"passkey": "Signed in with a passkey", "code": "Signed in with a code", "device": "Signed in from a computer"}


def app_devices(item: dict[str, Any]) -> dict[str, Any]:
    """A member's signed-in devices and passkeys (the node sends them beside the member), as the app reads
    them. A device with no name says how its person signed in, never an id."""
    return {
        "passkeys": [
            {"id": key["id"], "name": key.get("label") or "A passkey", "created_at": key.get("created_at"),
             "last_used_at": key.get("last_used_at")}
            for key in item.get("passkeys") or []
        ],
        "sessions": [
            {"id": row["id"], "label": row.get("device_label") or VIA_WORDS.get(row.get("via"), "A browser"),
             "via": row.get("via"), "created_at": row.get("created_at"),
             "last_seen_at": row.get("last_seen_at") or row.get("created_at"), "expires_at": row.get("expires_at"),
             "current": bool(row.get("current"))}
            for row in item.get("sessions") or []
        ],
    }


def node_roles(role: Any, workspaces: Any) -> list[dict[str, str]]:
    """The node's roles for a person in the app's `role` and `workspaces`."""
    if role == "owner":
        raise ApiError("invalid", "The owner role is not given here. This computer has one owner.")
    if role not in NODE_ROLE:
        raise ApiError("invalid", "A person is an admin, a member, an approver, a viewer or an auditor.")
    scopes = scopes_for(workspaces)
    if not scopes:
        raise ApiError("invalid", "Pick at least one business, or all of them.")
    return [{"scope": scope, "role": NODE_ROLE[role]} for scope in scopes]


def path_for(prefix: str, value: str) -> str:
    return f"{prefix}/{quote(value, safe='')}"


def no_content() -> Response:
    return Response(status_code=204)


class Handlers:
    """The handlers of the routes below, sharing one Api (and what they keep between calls)."""

    def __init__(self, api: Api) -> None:
        self.api = api
        self.store = api.members.store

    # ---- sign-in by email: none on a node ----
    async def passkeys(self, request: Request) -> list[dict[str, Any]]:
        return [app_passkey(item) for item in (await read(request, "/api/me/devices"))["passkeys"]]

    async def passkey_register_options(self, request: Request) -> dict[str, Any]:
        return (await write(request, "POST", "/api/me/passkeys/options"))["publicKey"]

    async def passkey_register_verify(self, request: Request) -> dict[str, Any]:
        saved = await write(request, "POST", "/api/me/passkeys", {"credential": await credential_of(request)})
        return app_passkey(saved)

    async def passkey_delete(self, request: Request) -> Response:
        ident = request.path_params["id"]
        if ident not in {item["id"] for item in (await read(request, "/api/me/devices"))["passkeys"]}:
            raise ApiError("not_found", "No such passkey.")
        session = (await read(request, "/api/me")).get("session") or {}
        if not session.get("verified_recently"):  # as adding one asks: a passkey is how this person is known
            raise ApiError("step_up_required", "Confirm it is you to remove a passkey.")
        await write(request, "DELETE", path_for("/api/me/devices", ident))
        return no_content()

    async def step_up_options(self, request: Request) -> dict[str, Any]:
        return (await write(request, "POST", "/api/session/verify/options", {}))["publicKey"]

    async def step_up_verify(self, request: Request) -> Response:
        await write(request, "POST", "/api/session/verify", {"credential": await credential_of(request)})
        return no_content()

    # ---- sessions and my details ----
    async def sessions(self, request: Request) -> list[dict[str, Any]]:
        return [app_device(item) for item in (await read(request, "/api/me/devices"))["sessions"]]

    async def session_delete(self, request: Request) -> Response:
        ident = request.path_params["id"]
        if ident not in {item["id"] for item in (await read(request, "/api/me/devices"))["sessions"]}:
            raise ApiError("not_found", "No such session.")
        await write(request, "DELETE", path_for("/api/me/devices", ident))
        return no_content()

    async def me_update(self, request: Request) -> dict[str, Any]:
        data = await body_of(request)
        if data.get("avatar_color") is not None:
            raise ApiError("invalid", "This computer does not keep an avatar colour.")
        if "name" in data:
            await write(request, "PATCH", "/api/me/details", {"name": data["name"]})
        return public_user(self.api, await read(request, "/api/me"))

    # ---- the organisation ----
    async def org_get(self, request: Request) -> dict[str, Any]:
        del request
        return org_json(self.api)

    async def org_update(self, request: Request) -> dict[str, Any]:
        data = await body_of(request)
        if "slug" in data and data["slug"] not in (None, ORG):
            raise ApiError("invalid", "The address of this computer cannot change.")
        if "name" in data:
            name = (data["name"] or "").strip() if isinstance(data["name"], str) else ""
            if not name or len(name) > 80:
                raise ApiError("invalid", "A name is 1 to 80 characters.")
            self.api.store.set_setting("org.name", name)
        if "goal" in data:
            goal = data["goal"]
            if goal is not None and not (isinstance(goal, str) and goal in GOAL_TITLES):
                raise ApiError("invalid", "Pick one of the first jobs.")
            self.api.store.set_setting("org.goal", goal)
        if "goal_note" in data:
            note = data["goal_note"]
            if note is not None and (not isinstance(note, str) or len(note) > 200):
                raise ApiError("invalid", "A note is up to 200 characters.")
            self.api.store.set_setting("org.goal_note", (note or "").strip() or None)
        return org_json(self.api)

    async def google_unavailable(self, request: Request) -> RedirectResponse:
        """A link to Google opens this, so the person lands back on sign-in with the reason."""
        del request
        return RedirectResponse("/sign-in?error=google_unavailable", status_code=303)

    async def getting_started(self, request: Request) -> dict[str, Any]:
        goal = self.api.store.setting("org.goal")
        status = await read(request, "/api/status")
        approved = await optional(request, "/api/approvals", {"state": "approved", "limit": 1})
        denied = await optional(request, "/api/approvals", {"state": "denied", "limit": 1})
        members = await optional(request, "/api/members") or []
        invites = await optional(request, "/api/invites") or []
        notify = await optional(request, "/api/me/notify")
        done = {
            "goal": goal in GOAL_TITLES,
            "computer": True,
            # The person is signed in to reach this, so their account is set up here.
            "account": True,
            # A connected AI brain stands in for a first call, which the node cannot see.
            "ai": (status.get("brain") or {}).get("state") == "ready",
            "approval": bool(approved or denied),
            "phone": bool(notify and app_notify(notify)["push"]),
            "invite": bool(invites) or len(members) > 1,
        }
        items = [
            {
                "key": key,
                "title": title,
                "done": done.get(key, False),
                "hint": "This computer is connected." if key == "computer" and done[key] else hint,
                "minutes": minutes,
            }
            for key, title, hint, minutes in SETUP_ITEMS
            # A phone is reached by push, and this computer has none set up, so no step asks for one.
            if key != "phone" or PHONE_PUSH_HERE
        ]
        count = sum(1 for item in items if item["done"])
        return {
            "goal": {"key": goal, "title": GOAL_TITLES[goal]} if goal in GOAL_TITLES else None,
            "items": items,
            "done": count,
            "total": len(items),
            "complete": count == len(items),
        }

    # ---- members ----
    async def members(self, request: Request) -> list[dict[str, Any]]:
        return [app_member(item) for item in await read(request, "/api/members")]

    async def member_get(self, request: Request) -> dict[str, Any]:
        """One member, with the devices they are signed in on (the owner's view, from Settings, Members)."""
        item = await read(request, path_for("/api/members", request.path_params["id"]))
        return {**app_member(item), "devices": app_devices(item)}

    async def member_update(self, request: Request) -> dict[str, Any]:
        ident = request.path_params["id"]
        data = await body_of(request)
        current = await read(request, path_for("/api/members", ident))
        if current.get("owner"):
            raise ApiError("invalid", "Only an active member's roles can change.")
        role = data.get("role")
        if role is not None and role not in NODE_ROLE:
            node_roles(role, None)  # refuses with the reason
        have = {item["scope"]: item["role"] for item in current.get("roles") or []}
        workspace = data.get("workspace")
        if workspace is not None:
            # One business's role: the others keep theirs, so a person who differs by business stays that way.
            if not isinstance(workspace, str) or not workspace or role is None:
                raise ApiError("invalid", "Say which role the person has, and in which business.")
            targets = list(dict.fromkeys([*have, workspace]))
            roles = [{"scope": scope, "role": NODE_ROLE[role] if scope == workspace else have[scope]} for scope in targets]
            scopes = targets
        else:
            if "workspaces" in data:
                targets = scopes_for(data["workspaces"])
            else:
                targets = list(have)
            if role is not None and not targets:
                raise ApiError("invalid", "Pick at least one business, or all of them.")
            # A business a person is newly given starts as a viewer, never at the highest role they hold elsewhere.
            fallback = NODE_ROLE["viewer"]
            new_role = NODE_ROLE[role] if role is not None else None
            roles = [{"scope": scope, "role": new_role or have.get(scope, fallback)} for scope in targets]
            # Every scope the person held is listed too, so one they lose is cleared, not kept.
            scopes = list(dict.fromkeys([*have, *targets]))
        updated = await write(
            request, "PATCH", path_for("/api/members", ident), {"roles": roles, "scopes": scopes}
        )
        return app_member(updated)

    async def invites_list(self, request: Request) -> list[dict[str, Any]]:
        return [app_invite(item) for item in await read(request, "/api/invites")]

    async def invite_create(self, request: Request) -> dict[str, Any]:
        # The node decides first: only a person who may invite gets as far as checking the form.
        await read(request, "/api/invites")
        data = await body_of(request)
        email = data.get("email")
        if not isinstance(email, str) or not email.strip():
            raise ApiError("invalid", "Enter the address the invite goes to.")
        roles = node_roles(data.get("role"), data.get("workspaces"))
        handle = _free_handle(email.strip(), self.store.handle_taken)
        created = await write(
            request, "POST", "/api/invites", {"handle": handle, "email": email.strip(), "roles": roles}
        )
        # The code is shown once, here, so the app can offer the link now and never again.
        return {**app_invite(created), "token": created["code"]}

    # ---- invites, as the person who was invited ----
    async def invite_preview(self, request: Request) -> dict[str, Any]:
        token = request.path_params["token"]
        found = (await write(request, "POST", "/api/invites/accept/options", {"code": token}))["invite"]
        roles = found.get("roles") or []
        scopes = scopes_of(roles)
        names = {item["id"]: item["name"] for item in await workspace_meta(self.api)}
        workspaces = None if scopes is None else [names.get(scope, scope) for scope in scopes]
        return {
            "org_name": org_name(self.api),
            "role": app_role(roles),
            "inviter": found.get("created_by") or "",
            "business": org_name(self.api) if workspaces is None else words(workspaces),
            "workspaces": workspaces,
            "expires_at": found["expires_at"],
            # Here the invite is accepted with a passkey, so the page offers one.
            "join_by": "passkey",
        }

    async def invite_passkey_options(self, request: Request) -> dict[str, Any]:
        token = request.path_params["token"]
        return (await write(request, "POST", "/api/invites/accept/options", {"code": token}))["publicKey"]

    async def invite_passkey_join(self, request: Request) -> Response:
        token = request.path_params["token"]
        data = await body_of(request)
        if not data.get("credential"):
            raise ApiError("invalid", "The passkey answer is missing. Try again.")
        body: dict[str, Any] = {"code": token, "credential": data["credential"]}
        if isinstance(data.get("timezone"), str):
            body["timezone"] = data["timezone"]
        reply = await call(request, "POST", "/api/invites/accept", body=body)
        if reply.status >= 300:
            raise Refused(reply)
        return await signed_in(self.api, request, reply)

    # ---- notifications: my own, on this computer ----
    async def notify_get(self, request: Request) -> dict[str, Any]:
        return app_notify(await read(request, "/api/me/notify"))

    async def notify_put(self, request: Request) -> dict[str, Any]:
        data = await body_of(request)
        current = await read(request, "/api/me/notify")
        now = app_notify(current)
        if data.get("email_digest", "off") != "off" or data.get("email_urgent"):
            raise ApiError("invalid", EMAIL_OFF)
        if bool(data.get("push")) != now["push"]:
            raise ApiError("invalid", PUSH_OFF)
        quiet = data.get("quiet_hours") or {}
        minutes = data.get("batch_minutes", 0)
        if not isinstance(minutes, int) or isinstance(minutes, bool):
            raise ApiError("invalid", "Batch minutes is a whole number.")
        body: dict[str, Any] = {
            "timezone": data.get("timezone") or now["timezone"],
            "quiet_hours": [quiet.get("from"), quiet.get("to")] if quiet.get("enabled") else None,
            "batch_window_s": minutes * 60,
            "urgent_breaks_quiet": bool(quiet.get("urgent_breaks_through")),
        }
        if "channel" in current:
            # A member keeps the channel they have; the timing alone is saved.
            body["channel"] = current["channel"]
        return app_notify(await write(request, "PUT", "/api/me/notify", body))

    async def notify_test(self, request: Request) -> Response:
        result = await write(request, "POST", "/api/settings/notifications/test")
        if not result.get("ok"):
            raise ApiError("conflict", result.get("detail") or "No notification channel is enabled.")
        return no_content()

    async def push_config(self, request: Request) -> dict[str, Any]:
        del request
        return {"available": PHONE_PUSH_HERE, "reason": None if PHONE_PUSH_HERE else "not_configured", "public_key": None}

    async def push_devices(self, request: Request) -> list[dict[str, Any]]:
        del request
        return []


def routes(api: Api) -> list[BaseRoute]:
    h = Handlers(api)

    def refusal(message: str, code: str = "unavailable") -> Any:
        async def refuse(request: Request) -> None:
            del request
            raise ApiError(code, message)

        return refuse

    return [
        plain_route(api, "/api/auth/passkeys", h.passkeys, ["GET"]),
        plain_route(api, "/api/auth/passkey/register/options", h.passkey_register_options, ["POST"]),
        plain_route(api, "/api/auth/passkey/register/verify", h.passkey_register_verify, ["POST"]),
        plain_route(api, "/api/auth/passkeys/{id}", h.passkey_delete, ["DELETE"]),
        plain_route(api, "/api/auth/step-up/passkey/options", h.step_up_options, ["POST"]),
        plain_route(api, "/api/auth/step-up/passkey/verify", h.step_up_verify, ["POST"]),
        plain_route(api, "/api/auth/magic-link", refusal(NOT_BY_EMAIL), ["POST"]),
        plain_route(api, "/api/auth/magic-link/verify", refusal(NOT_BY_EMAIL), ["POST"]),
        plain_route(api, "/api/auth/step-up/magic-link", refusal(NOT_BY_EMAIL_STEP_UP), ["POST"]),
        plain_route(api, "/api/auth/step-up/verify", refusal(NOT_BY_EMAIL_STEP_UP), ["POST"]),
        plain_route(api, "/api/auth/google/start", h.google_unavailable, ["GET"]),
        plain_route(api, "/api/sessions", h.sessions, ["GET"]),
        plain_route(api, "/api/sessions/{id}", h.session_delete, ["DELETE"]),
        plain_route(api, "/api/me", h.me_update, ["PATCH"]),
        plain_route(api, "/api/orgs", refusal(ONE_ORG, "conflict"), ["POST"]),
        org_route(api, "", h.org_get, ["GET"]),
        org_route(api, "", h.org_update, ["PATCH"], owner=True),
        org_route(api, "", refusal(ORG_STAYS, "denied"), ["DELETE"], owner=True),
        org_route(api, "/getting-started", h.getting_started, ["GET"]),
        org_route(api, "/members", h.members, ["GET"]),
        org_route(api, "/members/{id}", h.member_get, ["GET"]),
        org_route(api, "/members/{id}", h.member_update, ["PATCH"]),
        org_route(api, "/invites", h.invites_list, ["GET"]),
        org_route(api, "/invites", h.invite_create, ["POST"]),
        org_route(api, "/settings/notifications", h.notify_get, ["GET"]),
        org_route(api, "/settings/notifications", h.notify_put, ["PUT"]),
        org_route(api, "/settings/notifications/test", h.notify_test, ["POST"]),
        org_route(api, "/push/config", h.push_config, ["GET"]),
        org_route(api, "/push/devices", h.push_devices, ["GET"]),
        org_route(api, "/push/devices", refusal(NO_PUSH_DEVICES), ["POST"]),
        org_route(api, "/push/devices/{id}", refusal(NO_PUSH_DEVICES), ["DELETE"]),
        org_route(api, "/push/test", refusal(NO_PUSH_TEST), ["POST"]),
        plain_route(api, "/api/invites/{token}", h.invite_preview, ["GET"], public=True),
        plain_route(api, "/api/invites/{token}/accept", refusal(INVITE_NEEDS_PASSKEY), ["POST"]),
        plain_route(api, "/api/invites/{token}/join", refusal(INVITE_NEEDS_PASSKEY), ["POST"]),
        plain_route(
            api, "/api/invites/{token}/passkey/options", h.invite_passkey_options, ["POST"], public=True
        ),
        plain_route(api, "/api/invites/{token}/passkey", h.invite_passkey_join, ["POST"], public=True),
    ]


def _free_handle(email: str, taken: Any) -> str:
    """A member handle from an address: its name part, lower case, and a number when it is taken."""
    local = email.split("@", 1)[0].lower()
    base = re.sub(r"[^a-z0-9._-]+", "", local).lstrip("._-")[:36] or "member"
    candidate, number = base, 2
    while taken(candidate) or candidate == OWNER_ID:
        candidate = f"{base}-{number}"
        number += 1
    return candidate
