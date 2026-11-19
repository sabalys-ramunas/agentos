"""Nodes, brains, the vault, grants, proxies, notifications, ask-first and confirmation settings (docs/API.md)."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from starlette.requests import Request

from agentos.api.access import Presence
from agentos.api.app import (
    Api,
    _takes,
    access_of,
    principal_of,
    query_int,
    read_body,
    read_json_object,
)
from agentos.api.errors import ApiError
from agentos.api.jsonable import jsonable, pick, strip_sensitive
from agentos.api.route_ids import path_id
from agentos.api.routes_core import OUT_OF_SCOPE, node_list
from agentos.core.aio import maybe_await
from agentos.core.errors import (
    AgentosError,
    ConfirmRequired,
    InvalidRequest,
    NotFound,
)
from agentos.core.models import PENDING_SPECIALS, Effect, ProxyConfig, parse_lane_id

VAULT_FIELDS = ("id", "name", "kind", "domain", "lanes", "created_at", "last_used")


LADDER = tuple(e.value for e in Effect if e.rank is not None)


# memory: a special with no effect of its own (agentos.policy.memory_rules); the panel grants it as
# read and write over the grant's workspaces, without private or owner-only entries; memory_read is
# the same without write (agentos.api.bridge.PANEL_SPECIAL_ALIASES)
# environments: vm, exec and secret on lanes env/* only (agentos.api.bridge.PANEL_SPECIAL_BUNDLES)
SPECIALS = (*(e.value for e in Effect if e.rank is None and e not in PENDING_SPECIALS), "memory", "memory_read",
            "environments")


MEMBER_SPECIALS = ("memory_read",)  # what a member's own agent may carry: reading its workspaces' memory


TTL_RE = re.compile(r"^[1-9][0-9]{0,4}[mhdw]$")


class GrantRoute(BaseModel):
    """How the computer that holds a grant reaches this node (agentos.node.viewroutes)."""
    model_config = ConfigDict(extra="forbid")
    ssh_host: str = Field(min_length=1, max_length=255)
    local_port: int = Field(ge=1, le=65535)


def issuing_grant(access: Any) -> Any:
    """The grant a new grant must fit inside: the caller's own, unless the caller is the owner (the
    owner token, or the owner's session, which carries the owner's grant). None for the owner."""
    grant = getattr(getattr(access, "ctx", None), "grant", None)
    if grant is None or getattr(grant, "owner_token", False):
        return None
    return grant


class AskFirstChange(BaseModel):
    scope: Literal["flow", "lane"]
    id: str = Field(min_length=1, max_length=256)
    ask_first: bool


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


class SettingsHandlers:
    """The node settings handlers, sharing one Api."""

    def __init__(self, api: Api) -> None:
        self.api = api

    # ---- nodes and brains ----
    async def nodes(self, request: Request) -> list[dict]:
        return await node_list(self.api)

    async def brains(self, request: Request) -> list[dict]:
        return jsonable(await maybe_await(self.api.svc("brains").list()))

    async def brains_order(self, request: Request) -> list[dict]:
        body = await read_body(request, BrainOrder)
        svc = self.api.svc("brains")
        await maybe_await(svc.set_order(body.order))
        access_of(request).trace("brains.order", summary="Brain order: " + ", ".join(body.order))
        return jsonable(await maybe_await(svc.list()))

    async def brain_tasks(self, request: Request) -> list[dict]:
        return jsonable(await maybe_await(self.api.svc("brains").list_tasks(limit=query_int(request, "limit", 50))))

    async def brain_task(self, request: Request) -> dict:
        return jsonable(await maybe_await(self.api.svc("brains").get_task(path_id(request))))

    # ---- vault (names only) ----
    def _vault_mine(self, request: Request, lanes: list[str] | None) -> bool:
        """The owner: every entry. A member with `vault` (an admin): entries bound only to lanes of
        their workspaces (an entry for no lane, or one shared with another workspace, is the owner's)."""
        access = access_of(request)
        if access.owner:
            return True
        return bool(lanes) and all(isinstance(lane, str) and "/" in lane and access.can("vault", lane.split("/")[0])
                                   for lane in lanes)

    async def vault(self, request: Request) -> list[dict]:
        items = jsonable(await maybe_await(self.api.svc("vault").list()))
        return [pick(i, VAULT_FIELDS) for i in items if self._vault_mine(request, i.get("lanes"))]

    async def vault_create(self, request: Request) -> dict:
        body = await read_body(request, VaultCreate)
        if not self._vault_mine(request, body.lanes):
            access_of(request).trace("vault.create", status="denied", summary="lanes outside your workspaces")
            raise ApiError("denied", "Bind the entry to lanes of the workspaces you administer.")
        created = jsonable(await maybe_await(self.api.svc("vault").create(
            name=body.name, kind=body.kind, domain=body.domain, lanes=body.lanes, username=body.username,
            secret=body.secret.get_secret_value(), principal=principal_of(request),
            **({"totp": body.totp.get_secret_value()} if body.totp is not None else {}))))
        access_of(request).trace("vault.create", summary=f"Add vault entry {body.name} ({body.kind}) for "
                                                         f"{body.domain}")
        return pick(created, VAULT_FIELDS)

    async def vault_delete(self, request: Request) -> dict:
        entry_id = path_id(request)
        access = access_of(request)
        lane = None
        if access.member is not None:
            entry = next((i for i in jsonable(await maybe_await(self.api.svc("vault").list())) if i.get("id") == entry_id),
                         None)
            if entry is None or not self._vault_mine(request, entry.get("lanes")):
                raise NotFound(f"No vault entry {entry_id}.")
            lane = entry["lanes"][0]
        await access.authorize(Effect.IRREVERSIBLE, action="vault.delete", lane_id=lane,
                               summary=f"Delete vault entry {entry_id}", content={"vault_delete": entry_id})
        await maybe_await(self.api.svc("vault").delete(entry_id))
        return {"ok": True}

    # ---- grants ----
    async def grants(self, request: Request) -> list[dict]:
        policy = self.api.svc("policy")
        if not hasattr(policy, "list_grants"):
            raise ApiError("unavailable", "Grants are not supported by this node yet.")
        items = strip_sensitive(jsonable(await maybe_await(policy.list_grants())))
        access = access_of(request)
        if access.member is not None:  # a member's own agents (sub.owner is the member)
            items = [g for g in items if g.get("subject_owner") == access.member.id and g.get("subject_kind") != "human"]
        return items

    async def grant_create(self, request: Request) -> dict:
        body = await read_body(request, GrantCreate)
        policy = self.api.svc("policy")
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

    async def grant_revoke(self, request: Request) -> dict:
        policy = self.api.svc("policy")
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

    async def grant_route(self, request: Request) -> dict:
        """`agentos connect` records the SSH host and local port its tunnel uses for this grant, so
        browser_view gives that computer a link that works there (QA 2026-11-10 finding 9)."""
        from agentos.node import viewroutes

        body = await read_body(request, GrantRoute)
        gid = path_id(request)
        if self.api.config is None:
            raise ApiError("unavailable", "This node keeps no connection routes.")
        try:
            route = viewroutes.save(self.api.config.home, gid, body.ssh_host, body.local_port)
        except ValueError as e:
            raise ApiError("invalid", str(e)) from None
        access_of(request).trace("grant.route", summary=f"Grant {gid} reaches this node as {body.ssh_host}, "
                                                         f"port {body.local_port} there")
        return {"id": gid, **route}

    # ---- proxies (public fields only) ----
    async def proxies(self, request: Request) -> list[dict]:
        from agentos.api.routes_core import proxies_by_label
        items = await proxies_by_label(self.api)
        return [p.public() if hasattr(p, "public") else {k: p.get(k) for k in ("label", "host", "port")}
                for p in items.values()]

    async def proxy_create(self, request: Request) -> dict:
        body = await read_body(request, ProxyCreate)
        proxy = ProxyConfig(label=body.label, host=body.host, port=body.port, username=body.username,
                            password=body.password.get_secret_value() if body.password else None)
        from agentos.api.routes_core import proxies_by_label
        old = (await proxies_by_label(self.api)).get(proxy.label)
        was = tuple((old.get(k) if isinstance(old, dict) else getattr(old, k, None))
                    for k in ("host", "port", "username", "password")) if old is not None else None
        if was is not None and was != (proxy.host, proxy.port, proxy.username, proxy.password):
            # the label is the exit of every account on it: a new host, port or login is a person's
            # call (docs/THREAT-MODEL.md). A brand-new label changes no account, so it stays open
            access_of(request).require_presence(Presence.PERSON, "proxy.add")
        if was is not None and not body.confirm and was[:3] != (proxy.host, proxy.port, proxy.username):
            from agentos.core.layout import proxy_users
            users = proxy_users(self.api.config.home, proxy.label) if self.api.config is not None else []
            who = f" ({'; '.join(users)})" if users else ""
            raise ConfirmRequired(
                f"Proxy {proxy.label} already points at {was[0]}:{was[1]}. Pointing it elsewhere is a new IP "
                f"for everything that uses it{who}: logged-in accounts may be challenged. Nothing changed.",
                {"label": proxy.label, "users": users})
        svc = self.api.optional("proxies")
        if svc is not None:
            await maybe_await(svc.add(proxy))
        elif self.api.config is not None:
            from agentos.core.config import load_proxies, save_proxies
            current = load_proxies(self.api.config)
            current[proxy.label] = proxy
            save_proxies(self.api.config, current)
        else:
            raise ApiError("unavailable", "Proxies are not configured on this node.")
        access_of(request).trace("proxy.add", summary=f"Proxy {proxy.label} at {proxy.host}:{proxy.port}")
        return proxy.public()

    # ---- notifications ----
    async def notifications(self, request: Request) -> dict:
        return strip_sensitive(jsonable(await maybe_await(self.api.svc("notifier").get_settings())))

    async def notifications_put(self, request: Request) -> dict:
        data = await read_json_object(request)
        result = strip_sensitive(jsonable(await maybe_await(self.api.svc("notifier").put_settings(data))))
        access_of(request).trace("notifications.update", summary="Notification settings changed")
        return result

    async def notifications_test(self, request: Request) -> dict:
        access_of(request).trace("notifications.test", summary="Test notification")
        return strip_sensitive(jsonable(await maybe_await(self.api.svc("notifier").test())))

    # ---- Ask me first (agentos.policy.ask_first; docs/PROTOCOL.md "Ask me first") ----
    def _switch_lane(self, scope: str, ident: str) -> str | None:
        """The lane a switch is about: the lane itself, or a flow's lane (None for a lane pattern
        or a flow that is not saved)."""
        if scope == "lane":
            return ident
        flows = self.api.runtime.get("flows")
        try:
            fv = flows.get(ident) if flows is not None else None
        except AgentosError:
            return None
        return None if fv is None or fv.definition.lane_is_pattern else fv.definition.lane

    async def ask_first_list(self, request: Request) -> list[dict]:
        access = access_of(request)
        items = [x.public() for x in self.api.svc("ask_first").list()]
        if access.owner:
            return items
        out = []
        for item in items:
            lane = self._switch_lane(item["scope"], item["id"])
            if lane is not None and access.lane_allowed(lane):
                out.append(item)
        return out

    async def ask_first_set(self, request: Request) -> dict:
        """On: any signed-in caller whose grant reaches the lane. Off lets commits run without a person,
        so it needs a person (Presence.PERSON): the owner, or a member whose role may set standing
        approvals in that workspace. A bearer token (an agent's, or the node token an agent may hold)
        and the device credential only turn it on."""
        body = await read_body(request, AskFirstChange)
        access = access_of(request)
        store = self.api.svc("ask_first")
        from agentos.policy.ask_first import check_id

        scope, ident = check_id(body.scope, body.id)
        lane = self._switch_lane(scope, ident)
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
    async def confirmation_get(self, request: Request) -> dict:
        return {**self.api.svc("confirmation").public(), "chat_clients": self.api.svc("approvals").store.chat_clients()}

    async def confirmation_methods(self, request: Request) -> list[dict]:
        return self.api.svc("confirmation").methods_public()

    async def confirmation_set(self, request: Request) -> dict:
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
        store = self.api.svc("confirmation")
        try:
            store.change(patch, by=who, role=what)
        except AgentosError as exc:
            access.trace("confirmation.set", status="denied", summary=exc.message)
            raise
        access.trace("confirmation.set", summary=f"changed {', '.join(sorted(patch))}")
        applied = {"applied_to_waiting": self.api.runtime.get("policy").apply_setting_to_waiting(by=who)} if to_waiting else {}
        return {**store.public(), "chat_clients": self.api.svc("approvals").store.chat_clients(), **applied}
