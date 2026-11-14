"""The freeze in the panel (agentos.freeze, docs/API.md "Freeze").

  GET  /api/freeze          what is frozen on this node (any signed-in caller)
  POST /api/freeze          {scope, target, reason}: the owner (an admin grant, the node token, a
                            signed-in owner session), or a member who is an admin of that
                            workspace (lane and workspace scope only)
  POST /api/unfreeze        {scope, target}: the owner in person, with a fresh check. In the panel: a
                            passkey check inside the step-up window when the owner has a passkey, else a
                            sign-in with a one-time code in the last few minutes, with or without members.
                            The node's CLI unfreezes with the node token and a one-time code
                            (Presence.PERSON) while the node has no members; a token alone or a grant
                            never does (403 `presence_required`).
  POST /api/freeze/link     a push's one-tap Freeze button: the signed, single-use link is the only
                            credential, and it can only freeze
"""

from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.routing import Route

from agentos.api.access import Presence
from agentos.api.app import Api, access_of, endpoint, read_body
from agentos.api.errors import ApiError
from agentos.api.members import OWNER_ID
from agentos.core.errors import InvalidRequest

UNFREEZE_IN_PERSON = ("Only the owner unfreezes, signed in to the panel (or with `agentos unfreeze` on the node). "
                      "A token cannot unfreeze.")
# A member is told in a person's words: no command, no token. The owner's own refusals keep the above.
UNFREEZE_BY_MEMBER = "Only the owner of this computer can unfreeze."
# An owner with no passkey unfreezes straight after a sign-in with a one-time code (the panel's own sign-in).
CODE_SIGN_IN_WINDOW_S = 5 * 60


class FreezeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["lane", "workspace", "node"]
    target: str | None = Field(default=None, max_length=80)
    reason: str = Field(min_length=1, max_length=300)


class UnfreezeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["lane", "workspace", "node", "mesh"]
    target: str | None = Field(default=None, max_length=80)


def _workspace(scope: str, target: str | None) -> str | None:
    if scope == "workspace":
        return target
    if scope == "lane" and target and "/" in target:
        return target.split("/", 1)[0]
    return None


def _covers(freeze: dict, lane: str) -> bool:
    if freeze["scope"] == "workspace":
        return lane.split("/", 1)[0] == freeze["target"]
    return freeze["scope"] != "lane" or freeze["target"] == lane


UNFREEZE_PASSKEY = "Confirm with your passkey first: unfreezing lets agents act again, so it needs a fresh check."
UNFREEZE_SIGN_IN = ("Unfreezing lets agents act again, so it needs a sign-in from the last "
                    f"{CODE_SIGN_IN_WINDOW_S // 60} minutes. Sign in again with a new code, then unfreeze.")


def _has_members(api: Api) -> bool:
    try:
        return any(not m.owner and m.state == "active" for m in api.members.store.members())
    except Exception:  # noqa: BLE001  (cannot tell: ask for the passkey, toward safe)
        return True


def _owner_has_passkey(api: Api) -> bool:
    try:
        return bool(api.members.store.credentials(OWNER_ID))
    except Exception:  # noqa: BLE001  (cannot tell: ask for the passkey, toward safe)
        return True


def routes(api: Api) -> list[Route]:
    def freeze_svc():
        return api.svc("freeze")

    async def freeze_status(request: Request) -> dict:
        svc = api.optional("freeze")
        if svc is None:  # a node without the freeze service: nothing is frozen, and the button stays hidden
            return {"available": False, "frozen": [], "lanes": [], "node_frozen": False}
        out = {"available": True, **svc.status()}
        access = access_of(request)
        if not access.owner:  # a member or a grant sees the freezes over what it can see
            out["frozen"] = [f for f in out["frozen"] if f["scope"] in ("node", "mesh") or any(
                access.lane_allowed(lane["lane"]) for lane in out["lanes"] if _covers(f, lane["lane"]))]
            out["lanes"] = [lane for lane in out["lanes"] if access.lane_allowed(lane["lane"])]
            out["journal"] = []
        return out

    async def freeze(request: Request) -> dict:
        access = access_of(request)
        auth = request.state.auth
        roles = getattr(access, "roles", {}) if access.member is not None else {}
        if not access.owner and not any("admin" in r for r in roles.values()):
            access.trace("freeze", status="denied", summary="freeze needs the owner or an admin of that workspace")
            raise ApiError("denied", "Freezing needs the owner, or an admin of that workspace (lanes and the "
                                     "workspace itself).")
        body = await read_body(request, FreezeIn)
        if not access.owner:
            ws = _workspace(body.scope, body.target)
            admin = ws is not None and ("admin" in roles.get(ws, set()) or "admin" in roles.get("*", set()))
            if not admin:
                access.trace("freeze", status="denied", lane=body.target if body.scope == "lane" else None,
                             summary="freeze needs the owner or an admin of that workspace")
                raise ApiError("denied", "Freezing needs the owner, or an admin of that workspace (lanes and the "
                                         "workspace itself).")
        who = "owner" if access.owner else f"member:{access.member.id}" if access.member else "panel"
        if auth.via == "bearer" and access.owner:
            who = "owner (token)"
        report = await freeze_svc().freeze(body.scope, body.target, reason=body.reason, by=who, via="panel")
        access.trace("freeze", lane=body.target if body.scope == "lane" else None,
                     summary=f"froze {report['scope']} {report['target']}: {body.reason}")
        api.publish("freeze", {"state": "frozen", "scope": report["scope"], "target": report["target"]})
        return report

    async def unfreeze(request: Request) -> dict:
        access = access_of(request)
        auth = request.state.auth
        # the owner in person: their panel session, or the node's CLI (the node token with a one-time code
        # from panel.db, Presence.PERSON). Never a member, a grant, the node token alone or a device credential.
        access.require_presence(Presence.PERSON, "unfreeze")
        if not access.owner or access.member is not None:
            access.trace("unfreeze", status="denied", summary="unfreeze needs the owner in person")
            raise ApiError("denied", UNFREEZE_BY_MEMBER if access.member is not None else UNFREEZE_IN_PERSON)
        body = await read_body(request, UnfreezeIn)
        session, now = auth.session, time.time()
        if session is None:
            # The node's CLI: the node token with a one-time code (Presence.PERSON, checked above). A code
            # proves the node's operator, not a passkey, so with members it is refused.
            if _has_members(api):
                access.trace("unfreeze", status="denied", summary="unfreeze needs a fresh passkey check")
                raise ApiError("step_up_required", "This node has members, so unfreezing needs a fresh passkey "
                                                   "check: unfreeze in the panel, or run `agentos unfreeze` on "
                                                   "the node itself.")
        elif _owner_has_passkey(api):
            verified = session.last_verified_at
            if verified is None or now - verified > api.settings.step_up_window:
                access.trace("unfreeze", status="denied", summary="unfreeze needs a fresh passkey check")
                raise ApiError("step_up_required", UNFREEZE_PASSKEY)
        elif session.via != "code" or now - (session.created_at or 0) > CODE_SIGN_IN_WINDOW_S:
            access.trace("unfreeze", status="denied", summary="unfreeze needs a sign-in from the last few minutes")
            raise ApiError("denied", UNFREEZE_SIGN_IN)
        result = await freeze_svc().unfreeze(body.scope, body.target, by="owner",
                                             via="panel" if session is not None else "cli")
        if result.get("unfrozen"):
            access.trace("unfreeze", lane=body.target if body.scope == "lane" else None,
                         summary=f"unfroze {result['scope']} {result['target']}")
            api.publish("freeze", {"state": "unfrozen", "scope": result["scope"], "target": result["target"]})
        return result

    async def freeze_link(request: Request) -> dict:
        svc = api.optional("freeze")
        if svc is None:
            raise ApiError("unauthorized", "This freeze link is not valid.")
        try:
            report = await svc.redeem_link(dict(request.query_params))
        except InvalidRequest as exc:
            raise ApiError("unauthorized", f"This freeze link is not valid: {exc.message}") from None
        api.publish("freeze", {"state": "frozen", "scope": report["scope"], "target": report["target"]})
        return {"frozen": True, "scope": report["scope"], "target": report["target"],
                "freeze_id": report["freeze_id"], "already_frozen": report["already_frozen"]}

    return [
        Route("/api/freeze", endpoint(api, freeze_status), methods=["GET"]),
        Route("/api/freeze", endpoint(api, freeze, action="freeze"), methods=["POST"]),
        Route("/api/unfreeze", endpoint(api, unfreeze, action="unfreeze", presence=Presence.PERSON), methods=["POST"]),
        Route("/api/freeze/link", endpoint(api, freeze_link, public=True, action="freeze.link"), methods=["POST"]),
    ]
