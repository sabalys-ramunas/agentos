"""Members, invites, passkey sign-in, step-up, devices and my notifications (docs/API.md, Members).

Public (no session yet; Origin checked, rate limited like the code sign-in):
  POST /api/invites/accept/options   {code} -> {invite, publicKey}      the invite, and passkey options
  POST /api/invites/accept           {code, credential, ...} -> session  registers the passkey, once
  POST /api/session/passkey/options  {} -> {publicKey}
  POST /api/session/passkey          {credential} -> session             sign in with a passkey
Signed in:
  GET  /api/me                                   who I am, my roles and what they let me do
  PATCH /api/me/details                          my name (the web contract reshapes /api/me)
  POST /api/session/verify/options, /verify      step-up: a fresh passkey check (for one approval)
  POST /api/me/passkeys/options, /api/me/passkeys add a passkey (the owner too)
  GET  /api/me/devices, DELETE /api/me/devices/{id}
  GET, PUT /api/me/notify
Owner, or a member with `members` (an admin) for the workspaces they administer:
  GET, POST /api/invites; DELETE /api/invites/{id}
  GET /api/members; GET, PATCH, DELETE /api/members/{id}
  DELETE /api/members/{id}/devices/{device} (GET /api/members/{id} lists them)
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from openajar.api import passkeys as pk
from openajar.api.access import Access, Presence
from openajar.api.app import Api, access_of, endpoint, read_body, read_json_object
from openajar.api.auth import MEMBER_IDLE_SECONDS, MEMBER_SESSION_SECONDS, SESSION_COOKIE, Session
from openajar.api.errors import ApiError
from openajar.api.jsonable import jsonable, strip_sensitive
from openajar.api.members import (
    CAPABILITIES,
    MEMBER_ROLES,
    OWNER_ID,
    Member,
    Role,
    b64url,
    capabilities,
    check_roles_with_policy,
    group_by_scope,
    new_member_id,
    validate_role,
)
from openajar.core import clock
from openajar.core.aio import maybe_await
from openajar.core.errors import InvalidRequest, NotFound, OpenajarError

log = logging.getLogger("openajar.api")

TTL_RE = re.compile(r"^([1-9][0-9]{0,4})([hdw])$")
TTL_UNITS = {"h": 3600, "d": 86400, "w": 7 * 86400}
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,1400}$")
ALL_CAPS = sorted(set().union(*CAPABILITIES.values()))


def _ttl(value: str | None) -> int | None:
    if value is None:
        return None
    m = TTL_RE.match(value)
    if not m:
        raise InvalidRequest("ttl looks like 12h, 90d or 4w")
    return int(m.group(1)) * TTL_UNITS[m.group(2)]


class RoleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: str = Field(min_length=1, max_length=32)
    role: Literal["admin", "approver", "operator", "viewer", "auditor"]
    options: dict[str, Any] = Field(default_factory=dict)


class InviteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    handle: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,39}$")
    display_name: str | None = Field(default=None, max_length=80)
    email: str | None = Field(default=None, max_length=200, pattern=r"^[^@\s]+@[^@\s]+$")
    roles: list[RoleIn] = Field(min_length=1, max_length=50)
    ttl: str | None = Field(default=None, max_length=8)   # how long the membership lasts; None: until removed


class CodeIn(BaseModel):
    code: str = Field(min_length=8, max_length=200)


class InviteAccept(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=8, max_length=200)
    credential: dict[str, Any]
    display_name: str | None = Field(default=None, max_length=80)
    timezone: str | None = Field(default=None, max_length=64)
    device_label: str | None = Field(default=None, max_length=80)

    @field_validator("timezone")
    @classmethod
    def _zone(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("timezone is an IANA name like Europe/Vilnius") from None
        return v


class PasskeyIn(BaseModel):
    credential: dict[str, Any]
    device_label: str | None = Field(default=None, max_length=80)


class VerifyOptions(BaseModel):
    approval_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class RolesUpdate(BaseModel):
    """Replace the member's roles in `scopes` (default: the scopes named in `roles`) with `roles`."""
    model_config = ConfigDict(extra="forbid")
    roles: list[RoleIn] = Field(default_factory=list, max_length=50)
    scopes: list[str] | None = Field(default=None, max_length=50)
    ttl: str | None = Field(default=None, max_length=8)


class MeUpdate(BaseModel):
    """My details the node keeps: the name a person goes by here."""
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=80)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("name cannot be empty")
        return v


class NotifyIn(BaseModel):
    """My notifications, kept in panel.db while the notifier does not keep them itself."""
    model_config = ConfigDict(extra="forbid")
    channel: Literal["ntfy", "webhook", "none"] = "none"
    topic: str | None = Field(default=None, min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    timezone: str = "UTC"
    quiet_hours: list[str] | None = Field(default=None, min_length=2, max_length=2)
    digest_at: list[str] = Field(default_factory=lambda: ["08:00", "13:00", "18:00"], max_length=12)
    batch_window_s: int = Field(default=600, ge=0, le=3600)
    urgent_breaks_quiet: bool = False

    @field_validator("quiet_hours", "digest_at")
    @classmethod
    def _times(cls, v: list[str] | None) -> list[str] | None:
        for t in v or []:
            if not re.match(HHMM, t):
                raise ValueError("times look like 21:00")
        return v

    @field_validator("timezone")
    @classmethod
    def _zone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("timezone is an IANA name like Europe/Vilnius") from None
        return v


def public_base(api: Api) -> str | None:
    if api.settings.public_base_url:
        return api.settings.public_base_url.rstrip("/")
    notifier = api.runtime.get("notifier")
    url = getattr(getattr(notifier, "config", None), "public_base_url", None)
    return url.rstrip("/") if isinstance(url, str) and url else None


def relying_party(api: Api) -> pk.RelyingParty:
    try:
        return pk.RelyingParty.for_urls(public_base(api), port=getattr(api.config, "mcp_port", None),
                                        extra_origins=api.settings.allowed_origins)
    except ValueError:
        raise ApiError("unavailable", "public_base_url is not a valid URL; passkeys need it.") from None


def panel_base(api: Api) -> str:
    base = public_base(api)
    if base:
        return base
    port = getattr(api.config, "mcp_port", None)
    return f"http://localhost:{port}" if port else "http://localhost"


class Handlers:
    """The handlers of the routes below, sharing one Api (and what they keep between calls)."""

    def __init__(self, api: Api) -> None:
        self.api = api
        self.members = api.members
        self.store = self.members.store

    # ---- helpers ----
    def guard(self, request: Request) -> None:
        """Public passkey routes: a browser on this site only, and the sign-in rate limit."""
        if not self.api.origin_ok(request):
            raise ApiError("csrf_failed", "This request came from another site.")
        client = request.client.host if request.client else "unknown"
        wait = self.api.limiter.check(client)
        if wait is not None:
            raise ApiError("rate_limited", "Too many sign-in attempts. Wait a few minutes and try again.",
                           headers={"Retry-After": str(int(wait))})

    def need_members(self) -> None:
        if not self.members.enabled:
            raise ApiError("unavailable", "Members need the policy service on this node.")

    def ceremony(self, credential: dict, purpose: str):
        try:
            challenge = pk.client_challenge(credential)
        except pk.PasskeyError as exc:
            raise ApiError("unauthorized", exc.message) from None
        found = self.store.finish(challenge, purpose)
        if found is None:
            raise ApiError("unauthorized", "This passkey request expired or was already used. Start again.")
        return found

    def session_response(self, request: Request, member: Member, *, credential_id: str, label: str | None) -> Response:
        if member.owner:
            ttl, idle = self.api.settings.session_ttl, None
        else:
            ttl, idle = MEMBER_SESSION_SECONDS, MEMBER_IDLE_SECONDS
        sid, session = self.api.store.create_session(member.id, ttl, member_id=member.id, via="passkey",
                                                credential_id=credential_id, device_label=label, idle_seconds=idle)
        resp = JSONResponse({"csrf": session.csrf, "expires_at": jsonable(clock.iso(session.expires_at)),
                             "member": member.public()})
        resp.set_cookie(SESSION_COOKIE, sid, max_age=ttl, path="/", httponly=True, samesite="strict",
                        secure=self.api.secure_cookie(request))
        return resp

    def me_member(self, request: Request) -> Member:
        """The person behind this request: a member, or the owner (a panel session, or the node's own
        token for the CLI). Any other token is refused: a grant is never a person."""
        auth = request.state.auth
        if auth.access.member is not None:
            return auth.access.member
        if auth.via == "cookie" or (auth.access.owner and auth.principal.id == OWNER_ID):
            return self.store.require(OWNER_ID)
        auth.access.trace("member.route", status="denied", summary="a token is not a person")
        raise ApiError("denied", "This is for a person signed in to the panel, not a token.")

    def me_session(self, request: Request) -> tuple[Member, Session]:
        member = self.me_member(request)
        session = request.state.auth.session
        if session is None:
            raise ApiError("denied", "This needs a signed-in panel session.")
        return member, session

    def administers(self, access: Access, scope: str) -> bool:
        """May this caller give or take roles in `scope`? The owner anywhere; an admin in their
        workspace (`*` only for an admin of every workspace)."""
        if access.owner:
            return True
        return access.member is not None and "members" in capabilities(access.roles.get(scope, set()) |  # type: ignore[attr-defined]
                                                                       access.roles.get("*", set()))  # type: ignore[attr-defined]

    def may_manage(self, access: Access, target: Member) -> bool:
        """An admin manages a member whose every role is in a scope they administer."""
        if access.owner:
            return not target.owner
        roles = self.store.roles(target.id, include_expired=True)
        return not target.owner and bool(roles) and all(self.administers(access, r.scope) for r in roles)

    def target(self, request: Request) -> Member:
        mid = request.path_params["id"]
        m = self.store.member(mid) if isinstance(mid, str) and len(mid) < 120 else None
        access = access_of(request)
        if m is None or (not access.owner and not self.may_manage(access, m)):
            raise NotFound(f"No member {mid}.")
        return m

    def member_json(self, m: Member, access: Access | None = None) -> dict:
        roles = self.store.roles(m.id, include_expired=True)
        if access is not None and not access.owner:
            roles = [r for r in roles if self.administers(access, r.scope)]
        out = m.public()
        out["roles"] = [{"scope": "*", "role": "owner", "options": {}, "expires_at": None}] if m.owner else \
            [r.public() for r in roles]
        out["devices"] = len(self.store.credentials(m.id))
        return out

    def roles_in(self, items: list[RoleIn], access: Access, *, exp: float | None) -> list[Role]:
        roles = []
        for item in items:
            r = validate_role(item.scope, item.role, item.options)
            if not self.administers(access, r.scope):
                access.trace("member.roles", status="denied", summary=f"cannot give {r.role} in {r.scope}")
                raise ApiError("denied", f"You cannot give roles in {r.scope}: only the owner, or an admin of "
                                         "that workspace, can.")
            roles.append(Role(scope=r.scope, role=r.role, options=r.options, expires_at=exp))
        try:
            check_roles_with_policy(roles)
        except InvalidRequest as exc:
            raise InvalidRequest(f"Role options: {exc.message}") from None
        return roles

    # ---- invites ----
    async def invites(self, request: Request) -> list[dict]:
        access = access_of(request)
        return [i.public() for i in self.store.invites()
                if access.owner or all(self.administers(access, r.scope) for r in i.roles)]

    async def invite_create(self, request: Request) -> dict:
        self.need_members()
        body = await read_body(request, InviteCreate)
        access = access_of(request)
        member_ttl = _ttl(body.ttl)
        roles = self.roles_in(body.roles, access, exp=None)
        if self.store.handle_taken(body.handle) or body.handle == OWNER_ID:
            raise ApiError("conflict", f"The handle {body.handle} is taken.")
        invite, code = self.store.add_invite(handle=body.handle, display_name=body.display_name, email=body.email,
                                        roles=roles, member_ttl_s=member_ttl, ttl_s=self.api.settings.invite_ttl,
                                        by=self.me_member(request).handle)
        access.trace("invite.create", summary=f"Invite {body.handle}: " + ", ".join(
            f"{r.role}@{r.scope}" for r in roles) + (f" for {body.ttl}" if body.ttl else ""))
        self.api.publish("member", {"invite": invite.id})
        # the code and the link are in this one response and nowhere else (only a hash is kept)
        return {**invite.public(), "code": code, "link": f"{panel_base(self.api)}/invite#code={code}"}

    async def invite_delete(self, request: Request) -> dict:
        invite = self.store.invite(request.path_params["id"]) if len(request.path_params["id"]) < 40 else None
        access = access_of(request)
        if invite is None or not (access.owner or all(self.administers(access, r.scope) for r in invite.roles)):
            raise NotFound("No such invite.")
        if not self.store.revoke_invite(invite.id):
            raise ApiError("conflict", f"This invite is already {invite.state}.")
        access.trace("invite.revoke", summary=f"Revoke the invite for {invite.handle}")
        return {"ok": True}

    async def accept_options(self, request: Request) -> dict:
        self.guard(request)
        self.need_members()
        body = await read_body(request, CodeIn)
        invite = self.store.invite_by_code(body.code.strip())
        if invite is None:
            raise ApiError("unauthorized", "This invite link is invalid, used, or expired. Ask for a new one.")
        member_id = new_member_id()
        challenge = pk.new_challenge()
        self.store.begin(challenge, "register", invite_id=invite.id, member_id=member_id)
        options = pk.registration_options(relying_party(self.api), user_id=member_id.encode(), user_name=invite.handle,
                                          display_name=invite.display_name or invite.handle, challenge=challenge)
        shown = {k: v for k, v in invite.public().items()
                 if k in ("handle", "display_name", "roles", "expires_at", "created_by")}
        return {"invite": shown, "publicKey": options}

    async def accept(self, request: Request) -> Response:
        self.guard(request)
        self.need_members()
        body = await read_body(request, InviteAccept)
        invite = self.store.invite_by_code(body.code.strip())
        if invite is None:
            raise ApiError("unauthorized", "This invite link is invalid, used, or expired. Ask for a new one.")
        found = self.ceremony(body.credential, "register")
        if found.invite_id != invite.id or not found.member_id:
            raise ApiError("unauthorized", "This passkey was made for another invite. Start again.")
        try:
            cred = pk.verify_registration(relying_party(self.api), body.credential, challenge=found.challenge)
        except pk.PasskeyError as exc:
            raise ApiError("unauthorized", f"The passkey was not accepted: {exc.message}.") from None
        if not self.store.use_invite(invite.id, found.member_id):  # single use, even when two tabs race
            raise ApiError("unauthorized", "This invite link is invalid, used, or expired. Ask for a new one.")
        member = self.store.create_member(member_id=found.member_id, handle=invite.handle,
                                     display_name=body.display_name or invite.display_name, email=invite.email,
                                     timezone=body.timezone or "UTC", by=invite.created_by, roles=invite.roles,
                                     member_ttl_s=invite.member_ttl_s)
        saved = self.store.add_credential(member_id=member.id, cred_id=cred.id, public_key=cred.public_key,
                                     sign_count=cred.sign_count, label=body.device_label, aaguid=cred.aaguid)
        try:
            self.members.sync(member.id, by=invite.created_by, force=True)
        except OpenajarError:
            self.members.remove(member.id, by="owner")
            raise ApiError("conflict", "The roles in this invite can no longer be given. Ask for a new invite.") \
                from None
        _trace_owner(self.api, "invite.accept", f"{member.handle} accepted the invite {invite.id} and added a passkey")
        self.api.publish("member", {"id": member.id})
        return self.session_response(request, member, credential_id=saved.id, label=body.device_label)

    # ---- passkey sign-in ----
    async def passkey_options(self, request: Request) -> dict:
        self.guard(request)
        challenge = pk.new_challenge()
        self.store.begin(challenge, "sign_in")
        return {"publicKey": pk.authentication_options(relying_party(self.api), challenge=challenge)}

    async def passkey_sign_in(self, request: Request) -> Response:
        self.guard(request)
        body = await read_body(request, PasskeyIn)
        found = self.ceremony(body.credential, "sign_in")
        cred, member = self._known_credential(body.credential)
        self._check_assertion(body.credential, found.challenge, cred)
        if not member.owner:
            self.members.sync(member.id, by="owner")
            if not self.members.grants(member.id):
                raise ApiError("unauthorized", "Your membership on this node has ended. Ask the owner to invite you "
                                               "again.")
        return self.session_response(request, member, credential_id=cred.id, label=body.device_label)

    def _known_credential(self, credential: dict):
        try:
            cid = b64url(pk.credential_id(credential))
        except pk.PasskeyError as exc:
            raise ApiError("unauthorized", exc.message) from None
        cred = self.store.credential(cid)
        member = self.store.member(cred.member_id) if cred else None
        if cred is None or member is None or member.state != "active":
            raise ApiError("unauthorized", "This passkey is not registered on this node.")
        return cred, member

    def _check_assertion(self, credential: dict, challenge: bytes, cred: Any) -> None:
        try:
            count = pk.verify_assertion(relying_party(self.api), credential, challenge=challenge,
                                        public_key=cred.public_key, sign_count=cred.sign_count)
        except pk.PasskeyError as exc:
            raise ApiError("unauthorized", f"The passkey was not accepted: {exc.message}.") from None
        self.store.used_credential(cred.id, count)

    # ---- step-up ----
    async def verify_options(self, request: Request) -> dict:
        body = await read_body(request, VerifyOptions)
        member, session = self.me_session(request)
        creds = self.store.credentials(member.id)
        if not creds:
            raise ApiError("conflict", "Add a passkey first: there is none to check.")
        challenge = pk.new_challenge()
        self.store.begin(challenge, "step_up", member_id=member.id, session_hash=session.id_hash,
                    approval_id=body.approval_id)
        return {"publicKey": pk.authentication_options(relying_party(self.api), challenge=challenge,
                                                       allow=[pk.unb64url(c.id) for c in creds])}

    async def verify(self, request: Request) -> dict:
        body = await read_body(request, PasskeyIn)
        member, session = self.me_session(request)
        found = self.ceremony(body.credential, "step_up")
        if found.member_id != member.id or found.session_hash != session.id_hash:
            raise ApiError("unauthorized", "This passkey check was started in another session. Start again.")
        cred, owner_of = self._known_credential(body.credential)
        if owner_of.id != member.id:
            raise ApiError("unauthorized", "This passkey belongs to someone else.")
        self._check_assertion(body.credential, found.challenge, cred)
        await self.api.store.run(self.api.store.verified, session, approval_id=found.approval_id)
        access_of(request).trace("session.verify", summary="Passkey check" + (
            f" for approval {found.approval_id}" if found.approval_id else ""), approval=found.approval_id)
        return {"verified_at": clock.iso(time.time()), "for_approval": found.approval_id,
                "window_s": self.api.settings.step_up_window, "fresh_s": self.api.settings.step_up_fresh}

    # ---- me ----
    async def me(self, request: Request) -> dict:
        auth = request.state.auth
        access = auth.access
        session = auth.session
        out: dict[str, Any] = {"principal": {"kind": auth.principal.kind, "id": auth.principal.id,
                                             "owner": access.owner}, "member": None, "roles": [], "can": {},
                               "session": None}
        if access.member is not None:
            m = access.member
            out["member"] = m.public()
            out["roles"] = [r.public() for r in self.store.roles(m.id) if r.scope in access.roles]  # type: ignore[attr-defined]
            out["can"] = {scope: sorted(capabilities(roles) | ({"watch"} if self._watch(access, scope) else set()))
                          for scope, roles in access.roles.items()}  # type: ignore[attr-defined]
        elif access.owner:
            out["member"] = self.store.require(OWNER_ID).public() if auth.via == "cookie" or \
                auth.principal.id == OWNER_ID else None
            out["roles"] = [{"scope": "*", "role": "owner", "options": {}, "expires_at": None}]
            out["can"] = {"*": [*ALL_CAPS, "owner", "watch"]}
        if session is not None:
            recent = session.last_verified_at is not None and \
                time.time() - session.last_verified_at <= self.api.settings.step_up_window
            out["session"] = {**session.public(), "step_up_window_s": self.api.settings.step_up_window,
                              "verified_recently": recent, "step_up_needed": access.member is not None and not recent}
            out["passkeys"] = len(self.store.credentials(session.member_id or OWNER_ID))
        return out

    def _watch(self, access: Any, scope: str) -> bool:
        g = access.grants.get(scope)
        try:
            return g is not None and g.caps("control") is not None
        except Exception:  # noqa: BLE001
            return False

    # ---- my passkeys and devices ----
    def require_recent_check(self, member: Member, session: Session) -> None:
        """Adding a passkey to an account that has one needs a passkey check within the step-up
        window, so someone who finds an open session cannot enrol their own authenticator
        (QA-teams-2). The first passkey (the owner after a code sign-in) needs none."""
        if not self.store.credentials(member.id):
            return
        verified = session.last_verified_at
        if verified is None or time.time() - verified > self.api.settings.step_up_window:
            raise ApiError("step_up_required", "Confirm with one of your passkeys first, then add the new one.")

    async def my_passkey_options(self, request: Request) -> dict:
        member, session = self.me_session(request)
        self.require_recent_check(member, session)
        challenge = pk.new_challenge()
        self.store.begin(challenge, "add_passkey", member_id=member.id, session_hash=session.id_hash)
        return {"publicKey": pk.registration_options(
            relying_party(self.api), user_id=member.id.encode(), user_name=member.handle,
            display_name=member.display_name or member.handle, challenge=challenge,
            exclude=[pk.unb64url(c.id) for c in self.store.credentials(member.id)])}

    async def my_passkey_add(self, request: Request) -> dict:
        body = await read_body(request, PasskeyIn)
        member, session = self.me_session(request)
        self.require_recent_check(member, session)
        found = self.ceremony(body.credential, "add_passkey")
        if found.member_id != member.id or found.session_hash != session.id_hash:
            raise ApiError("unauthorized", "This passkey request was started in another session. Start again.")
        try:
            cred = pk.verify_registration(relying_party(self.api), body.credential, challenge=found.challenge)
        except pk.PasskeyError as exc:
            raise ApiError("unauthorized", f"The passkey was not accepted: {exc.message}.") from None
        saved = self.store.add_credential(member_id=member.id, cred_id=cred.id, public_key=cred.public_key,
                                     sign_count=cred.sign_count, label=body.device_label, aaguid=cred.aaguid)
        await self.api.store.run(self.api.store.verified, session)
        access_of(request).trace("passkey.add", summary=f"{member.handle} added a passkey")
        return saved.public()

    def devices_of(self, member: Member, current: Session | None = None) -> dict:
        sessions = self.api.store.sessions_of(member.id)
        if member.owner:
            sessions = [s for s in sessions if s.member_id == OWNER_ID]
        return {"passkeys": [c.public() for c in self.store.credentials(member.id)],
                "sessions": [{**s.public(), "current": current is not None and s.id_hash == current.id_hash}
                             for s in sessions]}

    def drop_device(self, request: Request, member: Member, device: str) -> dict:
        if not ID_RE.match(device):
            raise NotFound("No such device.")
        if self.store.delete_credential(member.id, device):
            ended = self.api.store.delete_sessions(member_id=member.id, credential_id=device)
            access_of(request).trace("device.remove", summary=f"Remove a passkey of {member.handle}")
            return {"ok": True, "sessions_ended": ended}
        ended = self.api.store.delete_sessions(member_id=member.id, session_prefix=device) if len(device) == 16 else 0
        if not ended:
            raise NotFound("No such device.")
        access_of(request).trace("device.remove", summary=f"End a session of {member.handle}")
        return {"ok": True, "sessions_ended": ended}

    async def my_update(self, request: Request) -> dict:
        member = self.me_member(request)
        body = await read_body(request, MeUpdate)
        if body.name is not None:
            self.store.set_display_name(member.id, body.name)
            access_of(request).trace("me.update", summary=f"{member.handle} changed their name")
        return self.store.require(member.id).public()

    async def my_devices(self, request: Request) -> dict:
        return self.devices_of(self.me_member(request), request.state.auth.session)

    async def my_device_delete(self, request: Request) -> dict:
        return self.drop_device(request, self.me_member(request), request.path_params["device"])

    # ---- my notifications ----
    async def my_notify(self, request: Request) -> dict:
        member = self.me_member(request)
        notifier = self.api.runtime.get("notifier")
        if callable(getattr(notifier, "member_settings", None)):
            return strip_sensitive(jsonable(await maybe_await(notifier.member_settings(member.id))))
        stored = self.store.notify_prefs(member.id)
        return stored or NotifyIn(timezone=member.timezone, quiet_hours=["21:00", "08:00"],
                                  urgent_breaks_quiet=member.owner).model_dump()

    async def my_notify_put(self, request: Request) -> dict:
        member = self.me_member(request)
        data = await read_json_object(request)
        notifier = self.api.runtime.get("notifier")
        if callable(getattr(notifier, "update_member", None)):
            try:
                result = await maybe_await(notifier.update_member(member.id, data))
            except ValueError as exc:
                raise InvalidRequest(str(exc).splitlines()[0][:300]) from None
        else:
            prefs = NotifyIn.model_validate(data).model_dump()
            self.store.set_notify_prefs(member.id, prefs)
            result = prefs
        access_of(request).trace("notify.update", summary=f"{member.handle} changed their notifications")
        return strip_sensitive(jsonable(result))

    # ---- members ----
    async def members_list(self, request: Request) -> list[dict]:
        access = access_of(request)
        return [self.member_json(m, access) for m in self.store.members()
                if access.owner or (not m.owner and self.may_manage(access, m)) or m.id == getattr(access.member, "id", "")]

    async def member_get(self, request: Request) -> dict:
        m = self.target(request)
        return {**self.member_json(m, access_of(request)), **self.devices_of(m)}

    async def member_update(self, request: Request) -> dict:
        self.need_members()
        m = self.target(request)
        if m.owner or m.state == "removed":
            raise InvalidRequest("Only an active member's roles can change.")
        body = await read_body(request, RolesUpdate)
        access = access_of(request)
        ttl = _ttl(body.ttl)
        exp = time.time() + ttl if ttl else None
        new = self.roles_in(body.roles, access, exp=exp)
        if body.scopes is None and not new:  # nothing to change would answer 200 and change nothing (QA-teams-1)
            raise InvalidRequest("An empty role list needs `scopes`: the workspaces whose roles to clear. "
                                 "To remove the member, delete them instead.")
        scopes = set(body.scopes) if body.scopes is not None else {r.scope for r in new}
        for scope in scopes:
            validate_role(scope, MEMBER_ROLES[0], {})  # a valid scope
            if not self.administers(access, scope):
                raise ApiError("denied", f"You cannot change roles in {scope}.")
        if not ({r.scope for r in new} <= scopes):
            raise InvalidRequest("Every role must be in one of the scopes being changed.")
        if exp is None:  # a changed scope keeps the end its roles had, unless a new ttl is given
            old_end = {r.scope: r.expires_at for r in self.store.roles(m.id, include_expired=True)}
            new = [Role(scope=r.scope, role=r.role, options=r.options, expires_at=old_end.get(r.scope)) for r in new]
        by = self.me_member(request).handle
        grouped = group_by_scope(new)
        for scope in scopes:
            self.store.set_roles(m.id, scope, grouped.get(scope, []), by=by)
        self.members.sync(m.id, scopes=scopes, by=by, force=True)
        access.trace("member.roles", summary=f"Roles of {m.handle}: " + (", ".join(
            f"{r.role}@{r.scope}" for r in new) or "none") + f" in {', '.join(sorted(scopes))}")
        self.api.publish("member", {"id": m.id})
        return self.member_json(self.store.require(m.id), access)

    async def member_delete(self, request: Request) -> dict:
        self.need_members()
        m = self.target(request)
        access = access_of(request)
        result = self.members.remove(m.id, by=self.me_member(request).handle)
        ended = await self.api.store.run(self.api.store.delete_sessions, member_id=m.id)
        access.trace("member.remove", summary=f"Remove member {m.handle}: {len(result['grants'])} grants and "
                                              f"{len(result['agent_grants'])} agent grants revoked")
        self.api.publish("member", {"id": m.id, "removed": True})
        return {**result, "sessions_ended": ended}

    async def member_device_delete(self, request: Request) -> dict:
        return self.drop_device(request, self.target(request), request.path_params["device"])


def routes(api: Api) -> list[Route]:
    h = Handlers(api)

    def e(fn):
        return endpoint(api, fn)

    def public(fn):
        return endpoint(api, fn, public=True)

    def admin(fn, presence: Presence | None = None):
        """The owner or an admin member, in person (or the owner's token); never another token. With
        `presence`, at least that: who may act on this node is a person's decision (Presence.PERSON),
        so a token or the device credential, which an agent may hold, never invites or gives a role."""
        async def person_only(request: Request) -> Any:
            h.me_member(request)
            return await fn(request)

        person_only.__name__ = fn.__name__
        return endpoint(api, person_only, cap="members", presence=presence)

    def person(fn):
        """Signed in, and a person (Presence.PERSON): adding a passkey makes a credential that signs in
        as a person, so the node token or the device credential may never add one."""
        return endpoint(api, fn, presence=Presence.PERSON)

    PERSON = Presence.PERSON
    return [
        Route("/api/invites/accept/options", public(h.accept_options), methods=["POST"]),
        Route("/api/invites/accept", public(h.accept), methods=["POST"]),
        Route("/api/session/passkey/options", public(h.passkey_options), methods=["POST"]),
        Route("/api/session/passkey", public(h.passkey_sign_in), methods=["POST"]),
        Route("/api/session/verify/options", e(h.verify_options), methods=["POST"]),
        Route("/api/session/verify", e(h.verify), methods=["POST"]),
        Route("/api/me", e(h.me), methods=["GET"]),
        Route("/api/me/details", e(h.my_update), methods=["PATCH"]),
        Route("/api/me/passkeys/options", person(h.my_passkey_options), methods=["POST"]),
        Route("/api/me/passkeys", person(h.my_passkey_add), methods=["POST"]),
        Route("/api/me/devices", e(h.my_devices), methods=["GET"]),
        Route("/api/me/devices/{device}", e(h.my_device_delete), methods=["DELETE"]),
        Route("/api/me/notify", e(h.my_notify), methods=["GET"]),
        Route("/api/me/notify", e(h.my_notify_put), methods=["PUT"]),
        Route("/api/invites", admin(h.invites), methods=["GET"]),
        Route("/api/invites", admin(h.invite_create, PERSON), methods=["POST"]),
        Route("/api/invites/{id}", admin(h.invite_delete), methods=["DELETE"]),
        Route("/api/members", admin(h.members_list), methods=["GET"]),
        Route("/api/members/{id}", admin(h.member_get), methods=["GET"]),
        Route("/api/members/{id}", admin(h.member_update, PERSON), methods=["PATCH"]),
        Route("/api/members/{id}", admin(h.member_delete), methods=["DELETE"]),
        Route("/api/members/{id}/devices/{device}", admin(h.member_device_delete), methods=["DELETE"]),
    ]


def _trace_owner(api: Api, action: str, summary: str) -> None:
    """A trace for something no session did yet (an invite accepted): under the owner's context."""
    policy = api.runtime.get("policy")
    make = getattr(policy, "owner_context", None)
    if not callable(make):
        return
    try:
        ctx = make(tool=f"panel.{action}")
        ctx.trace("panel", status="ok", summary=summary, via="passkey")
    except Exception:  # noqa: BLE001  (tracing never fails a request)
        log.warning("panel trace failed for %s", action)
