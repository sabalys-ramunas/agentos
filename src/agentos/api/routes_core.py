"""Session, status, workspaces, templates and lanes."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agentos import __version__
from agentos.api.access import Access
from agentos.api.app import Api, access_of, endpoint, read_body, scoped_list
from agentos.api.auth import SESSION_COOKIE, tokens_equal
from agentos.api.errors import ApiError
from agentos.api.jsonable import jsonable
from agentos.api.lane_metrics import RANGES, lane_metrics
from agentos.api.lanes_view import (
    TEMPLATES,
    default_color,
    lane_json,
    needs_human,
    title_from_id,
    workspace_health,
)
from agentos.core import clock
from agentos.core.aio import maybe_await
from agentos.core.config import proxies_by_label as config_proxies_by_label
from agentos.core.errors import AgentosError, InvalidRequest, LaneExists, LaneNotFound
from agentos.core.models import Effect, LaneKind, LaneSpec, Preset, parse_lane_id
from agentos.core.presence import Presence
from agentos.core.system import MiB, read_meminfo

OUT_OF_SCOPE = "This lane is outside your grant."

WORKSPACE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


class SessionCreate(BaseModel):
    code: str = Field(min_length=8, max_length=200)


class DeviceCreate(BaseModel):
    label: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._@:-]*$")
    ttl_s: int | None = Field(default=None, ge=3600)


class WorkspaceCreate(BaseModel):
    id: str = Field(pattern=WORKSPACE_RE.pattern)
    name: str = Field(min_length=1, max_length=60)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")


class TemplateApply(BaseModel):
    template: str
    workspaces: list[str] = Field(min_length=1, max_length=50)
    proxy_per_workspace: dict[str, str] = Field(default_factory=dict)

    @field_validator("workspaces")
    @classmethod
    def _ws(cls, v: list[str]) -> list[str]:
        for ws in v:
            if not WORKSPACE_RE.match(ws):
                raise ValueError(f"workspace ids are lowercase letters, digits and dashes: {ws!r}")
        return v


class LaneCreate(BaseModel):
    id: str
    kind: LaneKind = LaneKind.BROWSER
    preset: Preset | None = None
    proxy_label: str | None = Field(default=None, max_length=64)
    engine: str | None = Field(default=None, max_length=32)
    device: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,40}$")  # AUD-02

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        parse_lane_id(v)
        return v

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: LaneKind) -> LaneKind:
        if v == LaneKind.AGENT:
            raise ValueError("agent lanes are created from the agents view, not as a plain lane")
        return v


# ---- helpers shared with other route modules ----

def lane_id_from(request: Request) -> str:
    ws, name = request.path_params["ws"], request.path_params["name"]
    if not WORKSPACE_RE.match(ws) or not NAME_RE.match(name):
        raise LaneNotFound(f"No lane {ws}/{name}.")
    return f"{ws}/{name}"


def scoped_lane_id(request: Request, action: str) -> str:
    """The lane in the path, refused (403, before any lookup, so nothing leaks) when it is
    outside the caller's grant."""
    lane_id = lane_id_from(request)
    access = access_of(request)
    if not access.lane_allowed(lane_id):
        access.trace(action, status="denied", lane=lane_id, summary="out of scope")
        raise ApiError("denied", OUT_OF_SCOPE)
    return lane_id


def env_lanes(api: Api) -> Any | None:
    """The env adapter when terminal and service lanes are on this node (agentos.api.bridge)."""
    envs = api.optional("envs")
    return envs if envs is not None and hasattr(envs, "lane_infos") else None


def is_env_lane(api: Api, lane_id: str) -> bool:
    envs = env_lanes(api)
    return envs is not None and envs.has(lane_id)


async def proxies_by_label(api: Api) -> dict[str, Any]:
    return await config_proxies_by_label(api.optional("proxies"), api.config)


async def shared_exit_needs_a_person(api: Api, request: Request, labels: list[str | None], action: str,
                                     lane: str | None = None) -> None:
    """A new browser behind a proxy label another lane already leaves through is a new computer in that
    account's office (another IP user, or another profile on its device): a person's call
    (docs/THREAT-MODEL.md). A label nothing uses stays open to the owner's token."""
    wanted = {label for label in labels if label}
    if not wanted:
        return
    infos = await maybe_await(api.svc("lanes").list())
    if any(getattr(i.spec, "proxy_label", None) in wanted for i in infos):
        access_of(request).require_presence(Presence.PERSON, action, lane=lane)


async def lane_hints(lanes: Any, lane_id: str) -> dict | None:
    fn = getattr(lanes, "hints", None)
    if fn is None:
        return None
    try:
        return await maybe_await(fn(lane_id))
    except AgentosError:
        return None


def frozen_of(api: Api, lane_id: str) -> bool | None:
    """Whether a freeze covers the lane, or None when this node has no freeze service."""
    freeze = api.runtime.get("freeze")
    check = getattr(freeze, "is_frozen", None)
    if not callable(check):
        return None
    try:
        return bool(check(lane_id))
    except Exception:  # noqa: BLE001 - an unreadable freeze state counts as frozen (agentos.freeze)
        return True


def login_requests(api: Api) -> dict[str, dict]:
    """Agents' open requests for a login (lane_login_request), by lane: {lane, why, by, at}. Empty when no
    agent has asked since the node started (the login service is made on first use)."""
    svc = api.runtime.get("login_assist") if callable(getattr(api.runtime, "get", None)) else None
    listing = getattr(svc, "requests", None)
    if not callable(listing):
        return {}
    try:
        return dict(listing())
    except Exception:  # noqa: BLE001 - a request list that cannot be read asks nobody for anything
        return {}


def needs_hand(lane: dict) -> bool:
    """An account only a person can help, as Needs you shows it (web lib/lanes.ts needsHand): a challenge,
    a quarantine or a login that needs an owner's sign-in, an agent asking for the lane back or for a
    login, another account signed in."""
    control = lane.get("control") or {}
    account = lane.get("account") or {}
    return (needs_human(lane) or bool(lane.get("login_request")) or bool(control.get("takeback_requested"))
            or account.get("status") == "wrong_account")


async def all_lanes(api: Api, access: Access | None = None) -> list[dict]:
    """Browser lanes and env lanes, limited to `access` when given."""
    allowed = access.lane_allowed if access is not None else (lambda _lane: True)
    lanes = api.svc("lanes")
    proxies = await proxies_by_label(api)
    asked = login_requests(api)
    out = []
    for info in await maybe_await(lanes.list()):
        lane_id = info.spec.id if hasattr(info, "spec") else info["spec"]["id"]
        if allowed(lane_id):
            out.append(_with_request(lane_json(info, hints=await lane_hints(lanes, lane_id), proxies=proxies,
                                               node=api.node_name, frozen=frozen_of(api, lane_id)), asked, api))
    envs = env_lanes(api)
    if envs is not None:
        for info, hints in envs.lane_infos():
            if allowed(info.spec.id):
                out.append(_with_request(lane_json(info, hints=hints, proxies=proxies, node=api.node_name,
                                                   frozen=frozen_of(api, info.spec.id)), asked))
    return out


def _with_request(out: dict, asked: dict[str, dict], api: Api | None = None) -> dict:
    """The agent's open login request on the lane, or null (additive: the web app shows it as a hand)."""
    out["login_request"] = asked.get(out["id"])
    if api is not None and out.get("logged_in_hint") in (None, "unknown"):
        out["logged_in_hint"] = signed_in_hint(api, out["id"])
    return out


# A readiness a person set, as the lane's login hint (agentos.workspaces.models.Readiness).
READINESS_HINT = {"ready": "ok", "needs_login": "needs_login"}


def signed_in_hint(api: Api, lane_id: str) -> str:
    """Whether the lane is signed in, when its manager does not say (finding 26 of the 2026-11-10 test): the
    readiness a person set wins; else the lane check's own session check (its last run, cookies only: nothing
    is probed here); else unknown. Never a guess: a lane nobody set and nobody checked stays unknown."""
    get = getattr(api.runtime, "get", None)
    workspaces = get("workspaces") if callable(get) else None  # the service itself: its adapter keeps no store
    store = getattr(workspaces, "store", None)
    try:
        row = store.lane_row(lane_id) if store is not None and callable(getattr(store, "lane_row", None)) else None
    except Exception:  # noqa: BLE001 - an unreadable record says nothing about the login
        row = None
    if row and row.get("updated_by") and row.get("readiness") in READINESS_HINT:
        return READINESS_HINT[row["readiness"]]
    checker = api.optional("lane_check")
    report = (getattr(checker, "last", None) or {}).get(lane_id) if checker is not None else None
    session = next((c for c in getattr(report, "checks", None) or [] if getattr(c, "id", None) == "session"), None)
    level = getattr(session, "level", None)
    return {"ok": "ok", "fail": "needs_login"}.get(str(level), "unknown")


async def one_lane(api: Api, lane_id: str, info: Any = None) -> dict:
    if info is None and is_env_lane(api, lane_id):
        info, hints = env_lanes(api).lane_info(lane_id)
        return _with_request(lane_json(info, hints=hints, proxies=await proxies_by_label(api), node=api.node_name,
                                       frozen=frozen_of(api, lane_id)), login_requests(api))
    lanes = api.svc("lanes")
    if info is None:
        info = await maybe_await(lanes.get(lane_id))
    return _with_request(lane_json(info, hints=await lane_hints(lanes, lane_id), proxies=await proxies_by_label(api),
                                   node=api.node_name, frozen=frozen_of(api, lane_id)), login_requests(api), api)


def stopped_runs(api: Api) -> list[dict]:
    """Runs that stopped halfway and wait for a person to say whether the step went through (partial runs
    with a decision needed). Their approval may also be an outcome: Needs you counts each run once."""
    store = getattr(api.runtime.get("runner"), "store", None)
    if not callable(getattr(store, "list_runs", None)):
        return []
    from agentos.core.models import RunStatus

    try:
        runs = store.list_runs(status=[RunStatus.PARTIAL], limit=200)
        return jsonable([run for run in runs if getattr(run, "needs_decision", False)])
    except Exception:  # noqa: BLE001
        return []


async def pending_approvals(api: Api, access: Access | None = None) -> list[dict]:
    svc = api.optional("approvals")
    if svc is None:
        return []
    try:
        return jsonable(await scoped_list(svc.list, access, state="pending", limit=500))
    except AgentosError:
        return []


async def workspace_meta(api: Api) -> list[dict]:
    svc = api.optional("workspaces")
    if svc is not None:
        return jsonable(await maybe_await(svc.list()))
    return api.store.list_workspaces()


async def ensure_workspace(api: Api, ws: str) -> None:
    if any(w["id"] == ws for w in await workspace_meta(api)):
        return
    svc = api.optional("workspaces")
    if svc is not None:
        await maybe_await(svc.create(id=ws, name=title_from_id(ws), color=default_color(ws)))
    else:
        try:
            await api.store.run(api.store.create_workspace, id=ws, name=title_from_id(ws), color=default_color(ws))
        except ApiError as e:  # another request made it while this one waited for the store
            if e.code != "conflict":
                raise


class Handlers:
    """The handlers of the routes below, sharing one Api (and what they keep between calls)."""

    def __init__(self, api: Api) -> None:
        self.api = api

    # ---- session ----
    async def session_create(self, request: Request) -> Response:
        # another site's page cannot sign a browser in with a code it holds (the controller checks too)
        if not self.api.origin_ok(request):
            raise ApiError("csrf_failed", "This sign-in came from another site, so it was refused.")
        client = request.client.host if request.client else "unknown"
        wait = self.api.limiter.check(client)
        if wait is not None:
            raise ApiError("rate_limited", "Too many sign-in attempts. Wait a few minutes and try again.",
                           headers={"Retry-After": str(int(wait))})
        body = await read_body(request, SessionCreate)
        if not await self.api.store.run(self.api.store.use_code, body.code.strip()):
            raise ApiError("unauthorized", "This sign-in link is invalid, used, or expired. "
                                           "Run `agentos panel login` for a new one.")
        sid, session = await self.api.store.run(self.api.store.create_session, "owner", self.api.settings.session_ttl, member_id="owner")
        resp = JSONResponse({"csrf": session.csrf, "expires_at": jsonable(clock.iso(session.expires_at))})
        resp.set_cookie(SESSION_COOKIE, sid, max_age=self.api.settings.session_ttl, path="/", httponly=True,
                        samesite="strict", secure=self.api.secure_cookie(request))
        return resp

    async def session_device(self, request: Request) -> Response:
        """The owner's device credential for the CLI tools on their computer (agentos dash, approvals,
        open): an owner panel session with a device label and a long life, returned in the body
        instead of a cookie. Only the node's own token may ask for one (`agentos connect` reads it
        over SSH for this one call), never a grant, an admin grant or a session. The credential is a
        session id, not a token: /mcp and the bearer path refuse it, so it never makes a tool call. It is
        Presence.DEVICE, below a person, because it is made by the node token and kept in a Keychain an
        agent may read: it reads, denies and controls lanes, and never approves (docs/THREAT-MODEL.md)."""
        auth = request.state.auth
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header[:7].lower() == "bearer " else None
        if auth.via != "bearer" or not tokens_equal(token, self.api.owner_token):
            auth.access.trace("session.device", status="denied", summary="not the node token")
            raise ApiError("denied", "A device credential is made with the node's own token, which "
                                     "`agentos connect` reads over SSH. Run it on your computer.")
        body = await read_body(request, DeviceCreate)
        ttl = min(body.ttl_s or self.api.settings.device_ttl, self.api.settings.device_max_ttl)
        sid, session = await self.api.store.run(self.api.store.create_session, "owner", ttl, member_id="owner",
                                           via="device", device_label=body.label)
        auth.access.trace("session.device", summary=f"Device credential for {body.label}")
        return JSONResponse({"session": sid, "csrf": session.csrf, "id": session.id_hash[:16],
                             "label": session.device_label, "expires_at": jsonable(clock.iso(session.expires_at))})

    async def session_delete(self, request: Request) -> Response:
        auth = request.state.auth
        if auth.sid:
            await self.api.store.run(self.api.store.delete_session, auth.sid)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
        return resp

    # ---- status ----
    async def status(self, request: Request) -> dict:
        auth = request.state.auth
        access = auth.access
        pending = await pending_approvals(self.api, access)  # actions and questions alike
        questions = sum(1 for item in pending if item.get("kind") == "question")
        lanes_needing: list[dict] = []  # what the web app shows as a hand (needs_hand)
        lanes_human: list[dict] = []  # the older count (needs_human), which `needs_you` keeps
        lane_counts = {"total": 0, "warm": 0, "ready": 0}
        if self.api.optional("lanes") is not None:
            try:
                lanes = await all_lanes(self.api, access)
            except AgentosError:
                lanes = []
            lanes_needing = [lane for lane in lanes if needs_hand(lane)]
            lanes_human = [lane for lane in lanes if needs_human(lane)]
            lane_counts = {
                "total": len(lanes),
                "warm": sum(1 for lane in lanes if lane["state"] in ("warm", "active")),
                "ready": sum(1 for lane in lanes if lane["logged_in_hint"] == "ok"),
            }
        codes = 0  # open code cards (agentos.vault.relay): only the owner and approvers answer them
        vault = self.api.optional("vault") if access.owner else None
        if vault is not None and hasattr(vault, "codes"):
            codes = len(await maybe_await(vault.codes()))
        # The rest of what Needs you shows, so this count is the one every screen says (web
        # features/needs-you/count.ts): failed outcomes, and the owner's stopped runs. A run that
        # stopped halfway is one thing, shown once, so it leaves the outcomes it is also counted in.
        from agentos.api.routes_approvals import failed_outcomes_for

        try:
            outcome_rows = await failed_outcomes_for(self.api, access, 500)
        except AgentosError:
            outcome_rows = []
        stopped = stopped_runs(self.api) if access.owner else []
        stopped_approvals = {run.get("approval_id") for run in stopped if run.get("approval_id")}
        outcomes = sum(1 for row in outcome_rows if row.get("id") not in stopped_approvals)
        runs = len(stopped)
        cost = {"tokens": 0, "usd": 0.0}
        traces = self.api.optional("traces")
        if access.owner and traces is not None and hasattr(traces, "cost_today"):  # node-wide spend: owner only
            cost = jsonable(await maybe_await(traces.cost_today()))
        return {
            "node": self.api.node_name,
            "version": __version__,
            "csrf": auth.session.csrf if auth.session else None,
            "principal": {"kind": auth.principal.kind, "id": auth.principal.id, "owner": access.owner},
            # `needs_you` and the detail's `lanes` keep their older meaning for any client that reads them.
            # `needs_you_all` is the web app's count (features/needs-you/count.ts), with
            # `hands`, `outcomes`, `runs` and `logins` beside it.
            "needs_you": len(pending) + len(lanes_human) + codes,
            "needs_you_all": len(pending) + len(lanes_needing) + codes + outcomes + runs,
            "needs_you_detail": {"approvals": len(pending) - questions, "questions": questions, "codes": codes,
                                 "lanes": [lane["id"] for lane in lanes_human],
                                 "hands": [lane["id"] for lane in lanes_needing],
                                 "outcomes": outcomes, "runs": runs,
                                 "logins": [lane["id"] for lane in lanes_needing if lane.get("login_request")]},
            "cost_today": cost,
            "brain": await brain_chip(self.api),
            "nodes": [{"id": n["id"], "state": n.get("state", "up"), "degraded": n.get("state") == "degraded"}
                      for n in await node_list(self.api)],
            "lanes": lane_counts,
            "services": self.api.runtime.names() if access.owner else [],
            "mcp_url": self.api.mcp_url,
            "timezone": self.api.timezone,
        }

    # ---- workspaces ----
    async def workspaces(self, request: Request) -> list[dict]:
        access = access_of(request)
        lanes = await all_lanes(self.api, access)
        pending = await pending_approvals(self.api, access)
        meta = await workspace_meta(self.api)
        if not access.owner:  # a grant sees the workspaces its lanes are in, a member the ones they hold roles in
            visible = {lane["workspace"] for lane in lanes}
            if access.member is not None:
                spaces = access.workspaces_with("see")  # type: ignore[attr-defined]
                visible |= {w["id"] for w in meta} if spaces is None else spaces
            meta = [w for w in meta if w["id"] in visible]
        ids = [w["id"] for w in meta]
        for lane in lanes:
            if lane["workspace"] not in ids:
                ids.append(lane["workspace"])
        by_id = {w["id"]: w for w in meta}
        out = []
        for ws in ids:
            ws_lanes = sorted((lane for lane in lanes if lane["workspace"] == ws), key=lambda x: x["name"])
            ws_pending = sum(1 for a in pending if str(a.get("lane") or "").split("/", 1)[0] == ws)
            health, summary = workspace_health(ws_lanes, ws_pending)
            m = by_id.get(ws, {})
            out.append({
                "id": ws,
                "name": m.get("name") or title_from_id(ws),
                "color": m.get("color") or default_color(ws),
                "health": health,
                "summary": summary,
                "pending_approvals": ws_pending,
                "lanes": ws_lanes,
            })
        return out

    async def workspace_create(self, request: Request) -> dict:
        body = await read_body(request, WorkspaceCreate)
        svc = self.api.optional("workspaces")
        color = body.color or default_color(body.id)
        if svc is not None:
            created = jsonable(await maybe_await(svc.create(id=body.id, name=body.name, color=color)))
        else:
            created = await self.api.store.run(self.api.store.create_workspace, id=body.id, name=body.name, color=color)
        ws = {**created, "health": "idle", "summary": "No lanes yet", "pending_approvals": 0, "lanes": []}
        access_of(request).trace("workspace.create", summary=f"Create workspace {body.id}")
        self.api.publish("workspace", ws)
        return ws

    # ---- templates ----
    async def templates(self, request: Request) -> list[dict]:
        svc = self.api.optional("workspaces")
        if svc is not None and callable(getattr(svc, "templates", None)):
            return jsonable(await maybe_await(svc.templates()))
        return list(TEMPLATES.values())

    async def template_apply(self, request: Request) -> dict:
        body = await read_body(request, TemplateApply)
        svc = self.api.optional("workspaces")
        if svc is not None and callable(getattr(svc, "apply", None)):
            # The workspaces service plans first and refuses (409 conflict) a plan that would link
            # two businesses or two accounts of one platform by IP; nothing is created then.
            for ws in body.workspaces:
                await ensure_workspace(self.api, ws)
            access = access_of(request)
            real = getattr(svc, "svc", svc)  # the panel's adapter wraps the workspaces service
            if callable(getattr(real, "plan", None)):
                plan = await maybe_await(real.plan(body.template, body.workspaces,
                                                   proxy_per_workspace=body.proxy_per_workspace))
                await shared_exit_needs_a_person(self.api, request, [s.proxy for s in plan.creates], "template.apply")
            result = await svc.apply(template=body.template, workspaces=body.workspaces,
                                     proxy_per_workspace=body.proxy_per_workspace,
                                     by=request.state.auth.principal.id)
            lanes = self.api.svc("lanes")
            created = []
            for lane_id in result.get("created", []):
                lane = await one_lane(self.api, lane_id, await maybe_await(lanes.get(lane_id)))
                created.append(lane)
                self.api.publish("lane", lane)
            access.trace("template.apply", summary=f"Apply template {body.template} to "
                                                   f"{', '.join(body.workspaces)}: {len(created)} lanes")
            return {**result, "created": created, "skipped": result.get("existing", [])}
        tpl = TEMPLATES.get(body.template)
        if tpl is None:
            raise InvalidRequest(f"Unknown template {body.template!r}.")
        lanes = self.api.svc("lanes")
        proxies = await proxies_by_label(self.api)
        for ws, label in body.proxy_per_workspace.items():
            if label not in proxies:
                raise InvalidRequest(f"Unknown proxy {label!r} for {ws}.")
        await shared_exit_needs_a_person(self.api, request, list(body.proxy_per_workspace.values()), "template.apply")
        created, skipped = [], []
        for ws in body.workspaces:
            await ensure_workspace(self.api, ws)
            for item in tpl["lanes"]:
                spec = LaneSpec(id=f"{ws}/{item['name']}", kind=LaneKind(item["kind"]),
                                proxy_label=body.proxy_per_workspace.get(ws))
                try:
                    info = await maybe_await(lanes.create(spec))
                except LaneExists:
                    skipped.append(spec.id)
                    continue
                lane = await one_lane(self.api, spec.id, info)
                created.append(lane)
                self.api.publish("lane", lane)
        access_of(request).trace("template.apply", summary=f"Apply template {body.template} to "
                                                         f"{', '.join(body.workspaces)}: {len(created)} lanes")
        return {"created": created, "skipped": skipped}

    # ---- lanes ----
    async def lanes_list(self, request: Request) -> list[dict]:
        return await all_lanes(self.api, access_of(request))

    async def lane_get(self, request: Request) -> dict:
        return await one_lane(self.api, scoped_lane_id(request, "lane.get"))

    async def lane_create(self, request: Request) -> dict:
        body = await read_body(request, LaneCreate)
        if body.proxy_label and body.proxy_label not in await proxies_by_label(self.api):
            raise InvalidRequest(f"Unknown proxy {body.proxy_label!r}.")
        label = body.proxy_label
        if body.device and callable(getattr(self.api.svc("lanes"), "device_info", None)):
            try:  # a lane on a device leaves through the device's proxy
                label = (await maybe_await(self.api.svc("lanes").device_info(body.device))).get("proxy") or label
            except AgentosError:
                pass  # no such device: the lane manager says so
        await shared_exit_needs_a_person(self.api, request, [label], "lane.create", lane=body.id)
        spec = LaneSpec(id=body.id, kind=body.kind, proxy_label=body.proxy_label, device=body.device,
                        **({"engine": body.engine} if body.engine else {}))
        await ensure_workspace(self.api, spec.workspace)
        info = await maybe_await(self.api.svc("lanes").create(spec))
        lane = await one_lane(self.api, spec.id, info)
        access_of(request).trace("lane.create", lane=spec.id, summary=f"Create {spec.kind.value} lane {spec.id}")
        self.api.publish("lane", lane)
        return lane

    async def _open_or_close(self, request: Request, verb: str) -> dict:
        lane_id = scoped_lane_id(request, f"lane.{verb}")
        access_of(request).require("operate", lane_id.split("/", 1)[0], action=f"lane.{verb}", lane=lane_id)
        await access_of(request).authorize(Effect.READ, action=f"lane.{verb}", lane_id=lane_id,
                                           summary=f"{verb.capitalize()} lane {lane_id}")
        if is_env_lane(self.api, lane_id):
            envs = env_lanes(self.api)
            await (envs.open(lane_id) if verb == "open" else envs.close(lane_id))
            lane = await one_lane(self.api, lane_id)
        else:
            lanes = self.api.svc("lanes")
            info = await maybe_await(lanes.open(lane_id) if verb == "open" else lanes.close(lane_id))
            lane = await one_lane(self.api, lane_id, info)
        self.api.publish("lane", lane)
        return lane

    async def lane_open(self, request: Request) -> dict:
        return await self._open_or_close(request, "open")

    async def lane_close(self, request: Request) -> dict:
        return await self._open_or_close(request, "close")

    async def lane_screenshot(self, request: Request) -> Response:
        lane_id = scoped_lane_id(request, "lane.screenshot")
        access = access_of(request)
        if access.member is not None and access.watch_terms(lane_id) is None:  # type: ignore[attr-defined]
            # a screenshot shows what the account shows (DMs too): members watch only when their role says so
            access.trace("lane.screenshot", status="denied", lane=lane_id, summary="no live view for this role")
            raise ApiError("denied", "Your role does not include the live view of this lane.")
        if is_env_lane(self.api, lane_id):
            raise ApiError("lane_cold", "Terminal and service lanes have no screen to capture.")
        await access_of(request).authorize(Effect.READ, action="lane.screenshot", lane_id=lane_id,
                                           summary=f"Screenshot of lane {lane_id}")
        lanes = self.api.svc("lanes")
        info = await maybe_await(lanes.get(lane_id))
        if info.state.value not in ("warm", "active"):
            raise ApiError("lane_cold", "The lane is not open. Open it first to take a screenshot.")
        if hasattr(lanes, "screenshot"):
            png = await maybe_await(lanes.screenshot(lane_id))
        else:
            from agentos.node.claims import READ
            from agentos.node.tools import lane_session

            interaction = self.api.svc("interaction")
            async with lane_session(lanes, lane_id, "agent", mode=READ) as page:  # a read: passes a claim
                png = await maybe_await(interaction.screenshot(page))
        return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})

    async def lane_metrics_route(self, request: Request) -> dict:
        """The lane's runs per bar over a range (agentos.api.lane_metrics), counted from its real runs."""
        lane_id = scoped_lane_id(request, "lane.metrics")
        range_name = request.query_params.get("range") or "7d"
        if range_name not in RANGES:
            raise InvalidRequest(f"range is one of {', '.join(RANGES)}")
        runner = self.api.runtime.get("runner")
        if not callable(getattr(runner, "lane_run_times", None)):  # no runner here: no bars to draw
            return {"range": range_name, "bucket_seconds": int(RANGES[range_name][1].total_seconds()), "buckets": []}
        now = datetime.now(UTC)
        runs = runner.lane_run_times(lane_id, now - RANGES[range_name][0])
        return lane_metrics(runs, range_name=range_name, now=now)

    async def lane_check(self, request: Request) -> dict:
        """The lane's checklist (agentos.stealth.lanecheck), run now. Reads only: it never warms the lane."""
        lane_id = scoped_lane_id(request, "lane.check")
        await access_of(request).authorize(Effect.READ, action="lane.check", lane_id=lane_id,
                                           summary=f"Check lane {lane_id}")
        report = await self.api.svc("lane_check").check(lane_id)
        return report.to_dict()

    async def lane_health(self, request: Request) -> dict:
        """The lane's health (agentos.stealth.health): one grade and plain findings from the lane check's
        last run (run now when there is none, or with ?fresh=1), its pin findings and its holds in the
        last ?days=30. Reads only, as the check does, and is authorised as the check is."""
        from agentos.stealth import health

        lane_id = scoped_lane_id(request, "lane.health")
        days = _days_param(request, health.DEFAULT_DAYS, health.MAX_DAYS)
        await access_of(request).authorize(Effect.READ, action="lane.health", lane_id=lane_id,
                                           summary=f"Health of lane {lane_id}")
        fresh = request.query_params.get("fresh") in ("1", "true")
        found = await health.gather(self.api.svc("lane_check"), lane_id, days=days, fresh=fresh)
        out = found.to_dict()
        return out if access_of(request).owner else health.for_member(out)

    async def lane_keepalive(self, request: Request) -> dict:
        """The lane's keepalive plan (agentos.keepalive): the next visit, why not now, the last visits."""
        lane_id = scoped_lane_id(request, "lane.keepalive")
        await access_of(request).authorize(Effect.READ, action="lane.keepalive", lane_id=lane_id,
                                           summary=f"Keepalive plan of lane {lane_id}")
        keepalive = self.api.optional("keepalive")
        if keepalive is None:
            return {"lane": lane_id, "available": False}
        return {"available": True, **keepalive.status(lane_id)}

    async def lane_sites(self, request: Request) -> dict:
        """The domains this lane's account may reach. A read for anyone who can see the lane; the owner
        changes the list with POST /api/lanes/{ws}/{name}/sites (agentos.node.viewer_app)."""
        lane_id = scoped_lane_id(request, "lane.sites")
        await access_of(request).authorize(Effect.READ, action="lane.sites", lane_id=lane_id,
                                           summary=f"Site list of lane {lane_id}")
        info = await maybe_await(self.api.svc("lanes").get(lane_id))
        return {"lane": lane_id, "sites": list(info.spec.sites), "strict": info.spec.egress_strict}

    async def lanes_checks(self, request: Request) -> dict:
        """The worst level per lane the caller may see: from the last run (the health monitor runs every
        lane every few minutes), or run now with ?fresh=1."""
        access = access_of(request)
        checker = self.api.optional("lane_check")
        if checker is None:  # the health service is not running: every lane is unknown
            lanes = self.api.optional("lanes")
            ids = [info.spec.id for info in await maybe_await(lanes.list())] if lanes is not None else []
            return {"lanes": [{"lane": i, "level": "unknown", "safe": None, "state": None, "at": None,
                               "failing": [], "warning": [], "health": None} for i in ids if access.lane_allowed(i)],
                    "note": "The lane check is not running on this node."}
        ids = [i for i in await maybe_await(checker.lane_ids()) if access.lane_allowed(i)]
        if request.query_params.get("fresh") in ("1", "true"):
            await access.authorize(Effect.READ, action="lane.check", summary=f"Check {len(ids)} lanes")
            reports = {r.lane: r for r in await checker.check_all(ids)}
        else:
            reports = {i: checker.last[i] for i in ids if i in getattr(checker, "last", {})}
        out = [reports[i].brief() if i in reports else
               {"lane": i, "level": "unknown", "safe": None, "state": None, "at": None, "failing": [], "warning": []}
               for i in ids]
        from agentos.stealth import health

        try:  # the grade is extra: a list never fails because it could not be worked out
            grades = {h.lane: h.brief() for h in await health.grade_reports(checker, list(reports.values()))}
        except Exception as e:  # noqa: BLE001
            health.log.warning("lane health grades not worked out: %s", type(e).__name__)
            grades = {}
        for entry in out:
            entry["health"] = grades.get(entry["lane"])
        return {"lanes": out}


def routes(api: Api) -> list[Route]:
    h = Handlers(api)

    return [
        Route("/api/session", endpoint(api, h.session_create, public=True), methods=["POST"]),
        Route("/api/session", endpoint(api, h.session_delete), methods=["DELETE"]),
        Route("/api/session/device", endpoint(api, h.session_device), methods=["POST"]),
        Route("/api/status", endpoint(api, h.status), methods=["GET"]),
        Route("/api/workspaces", endpoint(api, h.workspaces), methods=["GET"]),
        Route("/api/workspaces", endpoint(api, h.workspace_create, owner=True), methods=["POST"]),
        Route("/api/templates", endpoint(api, h.templates), methods=["GET"]),
        Route("/api/templates/apply", endpoint(api, h.template_apply, owner=True), methods=["POST"]),
        Route("/api/lanes", endpoint(api, h.lanes_list), methods=["GET"]),
        Route("/api/lanes", endpoint(api, h.lane_create, owner=True), methods=["POST"]),
        Route("/api/lanes/{ws}/{name}", endpoint(api, h.lane_get), methods=["GET"]),
        Route("/api/lanes/{ws}/{name}/open", endpoint(api, h.lane_open), methods=["POST"]),
        Route("/api/lanes/{ws}/{name}/close", endpoint(api, h.lane_close), methods=["POST"]),
        Route("/api/lanes/{ws}/{name}/screenshot", endpoint(api, h.lane_screenshot), methods=["GET"]),
        Route("/api/lanes/checks", endpoint(api, h.lanes_checks), methods=["GET"]),
        Route("/api/lanes/{ws}/{name}/check", endpoint(api, h.lane_check), methods=["GET"]),
        Route("/api/lanes/{ws}/{name}/health", endpoint(api, h.lane_health), methods=["GET"]),
        Route("/api/lanes/{ws}/{name}/keepalive", endpoint(api, h.lane_keepalive), methods=["GET"]),
        Route("/api/lanes/{ws}/{name}/metrics", endpoint(api, h.lane_metrics_route), methods=["GET"]),
        Route("/api/lanes/{ws}/{name}/sites", endpoint(api, h.lane_sites), methods=["GET"]),
    ]


def _days_param(request: Request, default: int, most: int) -> int:
    raw = request.query_params.get("days")
    if raw is None or raw == "":
        return default
    if not raw.isdigit() or not 1 <= int(raw) <= most:
        raise InvalidRequest(f"days is a whole number from 1 to {most}, not {raw[:12]!r}")
    return int(raw)


async def brain_chip(api: Api) -> dict | None:
    brains = api.optional("brains")
    if brains is None:
        return None
    try:
        if hasattr(brains, "status"):
            # No AI model configured reads as no brain at all, which the app's `brain: null` means.
            status = jsonable(await maybe_await(brains.status()))
            return status if status and status.get("name") else None
        items = jsonable(await maybe_await(brains.list()))
    except AgentosError:
        return None
    if not items:
        return None
    first = next((b for b in items if b.get("state") == "ready"), items[0])
    return {"name": first.get("name"), "state": first.get("state"),
            "offline_ready": any(b.get("kind") in ("subscription", "local") and b.get("state") == "ready"
                                 for b in items)}


async def node_list(api: Api) -> list[dict]:
    svc = api.optional("nodes")
    if svc is not None:
        from agentos.api.jsonable import strip_sensitive
        return strip_sensitive(jsonable(await maybe_await(svc.list())))
    return [await local_node(api)]


async def local_node(api: Api) -> dict:
    """This node, when no controller is around (single-box mode)."""
    import os

    from agentos.core.models import utcnow

    warm = 0
    caps = []
    if api.optional("lanes") is not None:
        caps.append("browser")
        try:
            warm = sum(1 for i in await maybe_await(api.svc("lanes").list())
                       if i.state.value in ("warm", "active", "warming"))
        except AgentosError:
            pass
    if api.optional("envs") is not None:
        caps += ["terminal", "service"]
    if api.optional("brains") is not None:
        caps.append("brain")
    cpu = None
    try:
        cpu = round(100 * os.getloadavg()[0] / (os.cpu_count() or 1), 1)
    except (OSError, AttributeError):
        pass
    return {
        "id": api.node_name,
        "state": "up",
        "capabilities": caps,
        "cpu": cpu,
        "mem": _meminfo(),
        "warm_lanes": warm,
        "max_warm_lanes": getattr(api.config, "max_warm_lanes", None),
        "last_seen": jsonable(utcnow()),
        "timezone": api.timezone,
    }


def _meminfo() -> dict | None:
    info = read_meminfo()
    if not info or "MemTotal" not in info:
        return None
    total, avail = info["MemTotal"] // MiB, info.get("MemAvailable", info.get("MemFree", 0)) // MiB
    return {"used_mb": total - avail, "total_mb": total}
