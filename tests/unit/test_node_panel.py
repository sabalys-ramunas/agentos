"""The panel mounted on the node, on the real policy, vault, runner, brains and env services:
every route needs credentials, owner-only routes refuse grants, lane routes stay inside a
grant, the terminal is exec through policy, and the services' changes reach the event bus."""

from __future__ import annotations

import asyncio
import contextlib
import re
import threading
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from fakes import FakeInteraction, FakeLaneManager
from starlette.requests import Request
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_vault_support import make_vault, private_home

from agentos.api import ApiSettings, build_api
from agentos.api.access import PolicyAccess
from agentos.api.auth import PRESENCE_HEADER, SESSION_COOKIE, PanelStore
from agentos.core.config import NodeConfig
from agentos.core.models import Effect, LaneSpec
from agentos.core.presence import Presence
from agentos.node.daemon import build_node_app, build_services, node_runtime
from agentos.policy.tools import approval_status

TOKEN = "node-token-for-the-panel-tests"
SERVICES = ["agentos.api.events", "agentos.policy.service", "agentos.vault.service", "agentos.workspaces.service",
            "agentos.runner.service",
            "agentos.brains.service", "agentos.env.service", "agentos.memory.service",
            "agentos.agenda.service"]
LANES = ["personal/reddit", "biz-a/instagram", "biz-b/facebook"]
PARAMS = {"org": "local", "node": "x1", "job": "decide", "uid": "x1", "ws": "biz-a", "workspace": "biz-a", "name": "instagram", "id": "x1", "action": "enable", "path": "x",
          "device": "x1", "token": "x1", "operation": "instagram_post", "param": "image", "run": "x1", "account": "x1",
          "api": "x1", "list": "x1", "batch": "x1", "program": "biz-b-ig-bakery", "item": "it_x1", "kind": "like", "fid": "f_0000000000", "screen": "default@1280-light",
          "save_id": "sabcdefghjk", "frame": "fabcdefghjk", "checkpoint": "3"}

# (method, path) that only the owner (or an admin grant) may call; the docs/API.md table
OWNER_ONLY = [
    ("POST", "/api/workspaces"), ("POST", "/api/templates/apply"), ("POST", "/api/lanes"),
    ("POST", "/api/approvals/batch"), ("GET", "/api/approvals/standing"), ("POST", "/api/approvals/standing"),
    ("DELETE", "/api/approvals/standing/x1"), ("POST", "/api/approvals/x1/approve"),
    ("GET", "/api/codes"), ("POST", "/api/codes/x1"), ("POST", "/api/codes/x1/decline"),
    # a repository card (agentos.environments.api): the token page's link or a key, from the owner in person
    ("POST", "/api/repo-connect/x1/form"), ("POST", "/api/repo-connect/x1/deploy-key"),
    ("POST", "/api/repo-connect/x1/check"), ("POST", "/api/repo-connect/x1/decline"),
    ("POST", "/api/approvals/x1/deny"), ("POST", "/api/questions/x1/answer"), ("POST", "/api/schedules"),
    ("DELETE", "/api/schedules/x1"), ("GET", "/api/nodes"), ("POST", "/api/brains/order"),
    ("GET", "/api/brains/tasks"), ("GET", "/api/brains/tasks/x1"), ("GET", "/api/vault"), ("POST", "/api/vault"),
    ("DELETE", "/api/vault/x1"), ("GET", "/api/grants"), ("POST", "/api/grants"), ("POST", "/api/grants/x1/revoke"),
    ("GET", "/api/proxies"), ("POST", "/api/proxies"), ("GET", "/api/settings/notifications"),
    ("PUT", "/api/settings/notifications"), ("POST", "/api/settings/notifications/test"),
    # the feature packages' own routes (agentos.api.mounts)
    ("GET", "/api/vault/status"), ("POST", "/api/vault/unseal"), ("POST", "/api/vault/seal"),
    ("POST", "/api/runs/x1/cancel"), ("POST", "/api/runs/x1/decide"), ("GET", "/api/flows/x1"),
    ("POST", "/api/flows"), ("PATCH", "/api/schedules/x1"), ("POST", "/api/schedules/x1/enable"),
    ("POST", "/api/brains/tasks/x1/cancel"), ("POST", "/api/brains/tasks/x1/resume"),
    ("POST", "/api/workspaces/biz-a/export"), ("POST", "/api/workspaces/import"),
    ("GET", "/api/agenda"), ("POST", "/api/agenda"), ("GET", "/api/agenda/x1"), ("PATCH", "/api/agenda/x1"),
    ("POST", "/api/agenda/x1/enable"),  # the agenda (agentos.agenda.api)
    ("POST", "/api/lanes/biz-a/instagram/sites"),  # the lane's site list (agentos.node.viewer_app)
    # an assisted login (agentos.api.routes_login): the owner in person, never a grant
    ("GET", "/api/lanes/biz-a/instagram/login"), ("POST", "/api/lanes/biz-a/instagram/login"),
    ("POST", "/api/lanes/biz-a/instagram/login/step"), ("POST", "/api/lanes/biz-a/instagram/login/refresh"),
    ("POST", "/api/lanes/biz-a/instagram/login/take"), ("POST", "/api/lanes/biz-a/instagram/login/resume"),
    ("POST", "/api/lanes/biz-a/instagram/login/cancel"), ("POST", "/api/lanes/biz-a/instagram/login/save"),
    ("GET", "/api/lanes/biz-a/instagram/login/screenshot"),
    ("PUT", "/api/lanes/biz-a/instagram/meter"),  # a lane's limits (agentos.api.routes_meters)
    # browser devices (agentos.api.routes_devices): they span lanes and name proxies
    ("GET", "/api/browser-devices"), ("POST", "/api/browser-devices"), ("GET", "/api/browser-devices/x1"),
    ("DELETE", "/api/browser-devices/x1"), ("GET", "/api/lanes/biz-a/instagram/device-move"),
    ("POST", "/api/lanes/biz-a/instagram/device"),
]
# the owner-only routes that also need a person in the panel: a bearer token is no person, the node token
# included, so the owner's token gets presence_required there (docs/THREAT-MODEL.md)
NEEDS_A_PERSON = {("POST", "/api/approvals/standing"), ("POST", "/api/approvals/x1/approve"),
                  ("POST", "/api/questions/x1/answer"), ("POST", "/api/repo-connect/x1/form"),
                  ("POST", "/api/repo-connect/x1/deploy-key"), ("POST", "/api/repo-connect/x1/check"),
                  ("POST", "/api/repo-connect/x1/decline")}
LOGIN_ROUTES = [rt for rt in OWNER_ONLY if "/login" in rt[1]]
WS = "ws://localhost:7400"  # websocket_connect ignores base_url; the node refuses any other Host
# the login exchange and the passkey ceremonies (agentos.api.routes_members); the static panel is the catch-all
PUBLIC = {("GET", "/api/mail/form/{token}"), ("POST", "/api/mail/form/{token}"),  # the link is the credential
          ("GET", "/api/brains/form/{token}"), ("POST", "/api/brains/form/{token}"),  # so is this one
          ("GET", "/api/forms/{token}"), ("POST", "/api/forms/{token}"),  # and the node's forms for a secret
          ("GET", "/api/backups/form/{token}"), ("POST", "/api/backups/form/{token}"),  # and this one
          ("GET", "/api/auth/providers"), ("POST", "/api/auth/passkey/login/options"),  # how to sign in, and doing it
          ("POST", "/api/auth/passkey/login/verify"),
          ("POST", "/api/session"), ("POST", "/api/invites/accept/options"), ("POST", "/api/invites/accept"),
          ("POST", "/api/session/passkey/options"), ("POST", "/api/session/passkey"),
          # the sign-in page's own calls, made before there is a session (web/src/api/client.ts `auth`)
          ("GET", "/api/auth/providers"), ("POST", "/api/auth/passkey/login/options"),
          ("POST", "/api/auth/passkey/login/verify"),
          # a computer's invite: the link's code is the credential, as the node's own invite routes above
          ("GET", "/api/invites/{token}"), ("POST", "/api/invites/{token}/passkey/options"),
          ("POST", "/api/invites/{token}/passkey"),
          # a read-only share link: its signature is the credential (agentos.audit.service)
          ("GET", "/api/audit/shared/{token}"), ("GET", "/api/audit/shared/{token}/runs/{id}/screenshot"),
          ("POST", "/api/freeze/link")}  # a push's Freeze button: the signed link is the credential


class Node:
    def __init__(self, tmp_path):
        home = private_home(tmp_path)
        make_vault(home)  # a key-file vault, as `agentos vault init` makes
        self.config = NodeConfig(home=home, mcp_token=TOKEN, mcp_port=7400)
        self.lanes = FakeLaneManager(self.config)
        for lane in LANES:
            asyncio.run(self.lanes.create(LaneSpec(id=lane)))
        self.runtime = node_runtime(self.config, self.lanes, FakeInteraction())
        build_services(self.runtime, SERVICES)
        self.store = PanelStore.for_home(home)
        self.api_app = build_api(self.runtime, ApiSettings(lane_pump_seconds=0.05, panel_dir=tmp_path / "none"))
        self.app = build_node_app(self.config, self.runtime, api_app=self.api_app, panel=True)
        self.policy = self.runtime.policy

    def client(self) -> TestClient:
        return TestClient(self.app, base_url="http://localhost:7400")

    def grant(self, **kw) -> str:
        kw.setdefault("subject", {"id": "worker"})
        kw.setdefault("workspaces", ["biz-a"])
        kw.setdefault("ceiling", "read")
        _, token = self.policy.issue(**kw)
        return token

    def sign_in(self, client: TestClient) -> dict:
        self.store.add_code("one-time-code-for-tests")
        r = client.post("/api/session", json={"code": "one-time-code-for-tests"})
        assert r.status_code == 200, r.text
        return {"X-Agentos-CSRF": r.json()["csrf"]}

    def close(self):
        for svc in ("policy",):
            self.runtime.get(svc).close()


@pytest.fixture
def node(tmp_path):
    n = Node(tmp_path)
    yield n
    n.close()


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def concrete(path: str) -> str:
    return re.sub(r"\{(\w+)(?::\w+)?\}", lambda m: PARAMS[m.group(1)], path)


def api_routes(node: Node, *, contract: bool = False) -> list[tuple[str, str]]:
    """The node's routes. `contract`: with the web app's as well (tests/unit/test_api_members.py)."""
    out = []
    for route in node.api_app.routes:
        if isinstance(route, WebSocketRoute):
            out.append(("WS", route.path))
        elif isinstance(route, Route) and route.path != "/{path:path}" and not route.path.startswith("/v1/sites") \
                and (contract or not route.path.startswith("/api/orgs/")):
            out += [(m, route.path) for m in sorted(route.methods - {"HEAD"})]  # /v1/sites: tests/unit/test_siteapi_*
    return out


# ------------------------------------------------------------------ 401 everywhere

def test_every_route_needs_credentials(node):
    routes = api_routes(node)
    assert len(routes) > 60 and ("GET", "/api/vault/status") in routes and ("WS", "/api/lanes/{ws}/{name}/terminal") \
        in routes and ("POST", "/api/brains/tasks/{id}/resume") in routes
    assert {("GET", "/api/questions"), ("POST", "/api/questions/{id}/answer"), ("PATCH", "/api/schedules/{id}"),
            ("GET", "/api/schedules/{id}")} <= set(routes)
    with node.client() as client:
        for method, path in routes:
            url = concrete(path)
            if (method, path) in PUBLIC:
                continue
            if method == "WS":
                with pytest.raises(WebSocketDisconnect) as refused:
                    client.websocket_connect(WS + url).__enter__()
                assert refused.value.code == 4401, url
                continue
            r = client.request(method, url, json={})
            assert r.status_code == 401, (method, url, r.status_code, r.text)
            assert r.json()["error"]["code"] == "unauthorized" and "—" not in r.text
            forged = client.request(method, url, json={}, headers=bearer("agt1_forged"))
            assert forged.status_code == 401, (method, url)
        # MCP and its auth metadata: bearer only
        assert client.post("/mcp", json={}).status_code == 401
        assert client.get("/.well-known/oauth-authorization-server").status_code == 401
        # the static panel (not built here) and the login exchange are the only public routes
        assert client.get("/").status_code == 200 and "agentos" in client.get("/needs-you").text
        assert client.post("/api/session", json={"code": "wrong-code-123"}).status_code == 401
        assert client.post("/api/invites/accept/options", json={"code": "wrong-code-123"}).status_code == 401
        assert client.post("/api/invites/accept", json={"code": "wrong-code-123", "credential": {}}).status_code == 401
        assert client.post("/api/session/passkey", json={"credential": {"rawId": "eA"}}).status_code == 401


def test_cookie_writes_need_the_csrf_header(node):
    with node.client() as client:
        node.sign_in(client)
        for method, path in api_routes(node):
            if method in ("GET", "WS") or (method, path) in PUBLIC or path.endswith("/action"):
                continue
            r = client.request(method, concrete(path), json={})
            assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed", (method, path, r.text)


# ------------------------------------------------------------------ owner-only and lane scope

def test_owner_only_routes_refuse_a_grant_and_admit_the_owner(node):
    grant = node.grant()
    with node.client() as client:
        for method, path in OWNER_ONLY:
            r = client.request(method, path, json={}, headers=bearer(grant))
            owner = client.request(method, path, json={}, headers=bearer(TOKEN))
            # the owner's token passes the owner check; a route that needs a person in the panel refuses it
            # with presence_required, never with the role's "denied"
            refused = owner.json()["error"]["code"] if owner.status_code == 403 else None
            # such a route tells a grant the same (a person decides it), never "use a stronger credential"
            # (QA-2 #9); every other owner-only route says denied
            want = ({"presence_required"} if (method, path) in NEEDS_A_PERSON
                    else {"presence_required", "denied"} if refused == "presence_required" else {"denied"})
            assert r.status_code == 403 and r.json()["error"]["code"] in want, (method, path, r.text)
            assert owner.status_code != 401 and refused in (None, "presence_required"), (method, path, owner.text)
            if (method, path) in NEEDS_A_PERSON:
                assert refused == "presence_required", (method, path, owner.text)
    denied = [e for e in node.policy.traces.query(kind="panel", status="denied", limit=500)[0]
              if e.principal == "worker"]  # the owner's presence refusals are traced too, under the owner
    assert len(denied) >= len(OWNER_ONLY)


def test_only_the_owner_in_person_changes_a_lanes_site_list(node):
    """An admin grant is the owner on most routes, but it is an agent's credential: it never widens
    a lane's site list. Neither does the node token: the owner in person does."""
    admin = node.grant(admin=True)
    url = "/api/lanes/biz-a/instagram/sites"
    with node.client() as client:
        r = client.post(url, json={"op": "add", "sites": ["evil.example"]}, headers=bearer(admin))
        assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required" and "\u2014" not in r.text
        assert node.lanes.get("biz-a/instagram").spec.sites == []
        r = client.post(url, json={"op": "add", "sites": ["evil.example"]}, headers=bearer(TOKEN))
        assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"
        assert node.lanes.get("biz-a/instagram").spec.sites == []
        h = node.sign_in(client)
        r = client.post(url, json={"op": "add", "sites": ["instagram.com", "frame:www.google.com"]}, headers=h)
        assert r.status_code == 200 and r.json()["sites"] == ["instagram.com", "frame:www.google.com"]
        r = client.post(url, json={"op": "remove", "sites": ["instagram.com"]}, headers=h)
        assert r.status_code == 200 and node.lanes.get("biz-a/instagram").spec.sites == ["frame:www.google.com"]
    denied = node.policy.traces.query(kind="panel", status="denied", limit=50)[0]
    assert any(e.tool == "panel.lane.sites" for e in denied)


def _json_post(body: bytes) -> Request:
    """A POST with a body, as the node's own request object sees it (Starlette reads the body once and caches it)."""
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {"type": "http", "method": "POST", "path": "/api/lanes/biz-a/instagram/control", "query_string": b"",
             "headers": [(b"content-type", b"application/json")]}
    return Request(scope, receive)


class _Trace:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def trace(self, action, **fields):
        self.calls.append((action, fields))


def test_a_lane_write_is_traced_on_its_account_with_its_control_action(monkeypatch):
    """A write on a lane's route is traced with the lane, so the account's History finds it, and a lane control
    carries its action. The summary stays what the panel reads."""
    from types import SimpleNamespace

    from agentos.api import mounts

    recorder = _Trace()
    request = _json_post(b'{"action": "hand_back"}')
    request.scope["path_params"] = {"workspace": "biz-a", "name": "social"}
    request.scope["state"] = {"auth": SimpleNamespace(access=recorder)}
    asyncio.run(mounts._trace_write(request, "lane.control"))
    assert recorder.calls == [("lane.control", {"lane": "biz-a/social", "summary": "lane.control biz-a social",
                                                "control": "hand_back"})]
    # A write with no account on its route (a computer's own) names no lane.
    other = _Trace()
    bare = _json_post(b"{}")
    bare.scope["path_params"] = {"id": "x1"}
    bare.scope["state"] = {"auth": SimpleNamespace(access=other)}
    asyncio.run(mounts._trace_write(bare, "devices.remove"))
    assert other.calls == [("devices.remove", {"summary": "devices.remove x1"})]


def test_a_lane_control_trace_says_which_action_the_person_took(monkeypatch):
    """The feed names the action of a lane control (take over, hand back), not only the account. The action rides
    on the trace as one new field; the summary the panel reads stays exactly what it was."""
    from agentos.api import mounts

    assert asyncio.run(mounts._control_fields(_json_post(b'{"action": "take"}'), "lane.control")) == {"control": "take"}
    assert asyncio.run(mounts._control_fields(_json_post(b'{"action": "hand_back", "done": true}'), "lane.control")) == {"control": "hand_back"}
    # Nothing else is traced as a control, and a body that names no known action adds nothing.
    assert asyncio.run(mounts._control_fields(_json_post(b'{"action": "rm -rf"}'), "lane.control")) == {}
    assert asyncio.run(mounts._control_fields(_json_post(b"not json"), "lane.control")) == {}
    assert asyncio.run(mounts._control_fields(_json_post(b'{"action": "take"}'), "lane.sites")) == {}
    # The endpoint still reads the same body after the trace has.
    request = _json_post(b'{"action": "stop_agent"}')
    assert asyncio.run(mounts._control_fields(request, "lane.control")) == {"control": "stop_agent"}
    assert asyncio.run(request.json()) == {"action": "stop_agent"}


def test_only_the_owner_in_person_runs_an_assisted_login(node):
    """An admin grant is an agent's credential: it never sees or drives a login form."""
    admin = node.grant(admin=True)
    with node.client() as client:
        for method, path in LOGIN_ROUTES:
            r = client.request(method, path, json={}, headers=bearer(admin))
            assert r.status_code == 403 and r.json()["error"]["code"] == "denied" and "—" not in r.text, path
            assert "lane_login_request" in r.json()["error"]["message"]
        # the node token alone is not a person (an agent may hold it): a login hands a held lane back
        r = client.get("/api/lanes/biz-a/instagram/login", headers=bearer(TOKEN))
        assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required", r.text

        def person() -> dict:  # the node's CLI: a one-time code it writes into panel.db beside the token
            code = f"cli-{time.time_ns()}-abcdefghijklmnop"
            PanelStore.for_home(node.config.home).add_code(code, 60)
            return {**bearer(TOKEN), PRESENCE_HEADER: code}

        assert client.get("/api/lanes/biz-a/instagram/login", headers=person()).json() == {
            "lane": "biz-a/instagram", "state": "none"}
        r = client.post("/api/lanes/biz-a/instagram/login/step", json={"step": "s1", "values": {"password": "x"}},
                        headers=person())
        assert r.status_code == 404 and r.json()["error"]["code"] == "login_closed"
    denied = node.policy.traces.query(kind="panel", status="denied", limit=50)[0]
    assert sum(e.tool.startswith("panel.lane.login") for e in denied) >= len(LOGIN_ROUTES)


def test_an_admin_grant_is_an_owner(node):
    admin = node.grant(admin=True)
    with node.client() as client:
        assert client.get("/api/grants", headers=bearer(admin)).status_code == 200
        assert client.get("/api/status", headers=bearer(admin)).json()["principal"]["owner"] is True


def test_the_grants_list_explains_each_grant(node):
    reader = node.policy.issue(subject={"id": "reader"}, workspaces=["biz-a"], ceiling="read")[0]
    gone = node.policy.issue(subject={"id": "gone"}, workspaces=["biz-a"], ceiling="draft")[0]
    node.policy.revoke(gone.id)
    with node.client() as client:
        listed = {g["id"]: g for g in client.get("/api/grants", headers=bearer(TOKEN)).json()}
    lines = listed[reader.id]["explain"]
    assert lines == node.policy.explain_grant(reader.id)  # the same sentences as `agentos grants explain`
    assert any("only look at pages" in line for line in lines)
    assert listed[gone.id]["explain"] == ["It is revoked, so it allows nothing now."]


def test_a_grant_sees_only_its_lanes(node):
    grant = node.grant()
    with node.client() as client:
        h = bearer(grant)
        assert [lane["id"] for lane in client.get("/api/lanes", headers=h).json()] == ["biz-a/instagram"]
        assert [w["id"] for w in client.get("/api/workspaces", headers=h).json()] == ["biz-a"]
        assert client.get("/api/lanes/biz-a/instagram", headers=h).status_code == 200
        for path in ("/api/lanes/personal/reddit", "/api/lanes/biz-b/facebook/screenshot",
                     "/api/lanes/personal/nonexistent", "/api/activity?lane=personal/reddit"):
            r = client.get(path, headers=h)
            assert r.status_code == 403 and "outside your grant" in r.json()["error"]["message"], path
        assert client.post("/api/lanes/personal/reddit/open", headers=h).status_code == 403
        opened = client.post("/api/lanes/biz-a/instagram/open", headers=h)
        assert opened.status_code == 200 and opened.json()["state"] == "warm"
        status = client.get("/api/status", headers=h).json()
        assert status["principal"] == {"kind": "agent", "id": "worker", "owner": False}
        assert status["lanes"]["total"] == 1 and status["services"] == [] and status["cost_today"]["usd"] == 0
        # the owner sees everything
        assert len(client.get("/api/lanes", headers=bearer(TOKEN)).json()) == 3
    events, _ = node.policy.traces.query(kind="panel", lane="biz-a/instagram")
    assert any(e.tool == "panel.lane.open" and e.status == "ok" and e.principal == "worker" for e in events)


def test_lane_checks_are_scoped_to_the_grant(node):
    from test_ops_support import FakeHost

    from agentos.stealth.lanecheck import LaneChecker

    node.runtime.register("lane_check", LaneChecker(node.config, lanes=node.lanes, host=FakeHost(), network=False,
                                                    write_cache=False))
    grant = node.grant()
    with node.client() as client:
        h = bearer(grant)
        one = client.get("/api/lanes/biz-a/instagram/check", headers=h)
        assert one.status_code == 200 and one.json()["lane"] == "biz-a/instagram", one.text
        r = client.get("/api/lanes/personal/reddit/check", headers=h)
        assert r.status_code == 403 and "outside your grant" in r.json()["error"]["message"]
        listed = client.get("/api/lanes/checks?fresh=1", headers=h).json()["lanes"]
        assert [lane["lane"] for lane in listed] == ["biz-a/instagram"]
        assert len(client.get("/api/lanes/checks", headers=bearer(TOKEN)).json()["lanes"]) == 3


async def _ask(node: Node, token: str, question: str) -> str:
    from agentos.policy.tools import human_ask

    ctx = node.policy.context_for_token(token, tool="human_ask")
    return (await human_ask(ctx, question=question))["approval_id"]


def test_approvals_and_activity_stay_in_the_grant_family(node):
    mine, other = node.grant(subject={"id": "mine"}), node.grant(subject={"id": "other"})
    a = asyncio.run(_ask(node, mine, "Post the draft?"))
    b = asyncio.run(_ask(node, other, "Delete the old post?"))
    with node.client() as client:
        assert [x["id"] for x in client.get("/api/approvals", headers=bearer(mine)).json()] == [a]
        assert client.get(f"/api/approvals/{b}", headers=bearer(mine)).status_code == 404
        assert client.get(f"/api/approvals/{a}", headers=bearer(mine)).json()["requested_by"] == {
            "kind": "agent", "id": "mine", "thread": None}
        assert {x["id"] for x in client.get("/api/approvals", headers=bearer(TOKEN)).json()} == {a, b}
        assert {e["principal"] for e in client.get("/api/activity", headers=bearer(mine)).json()} == {"mine"}
        assert {"mine", "other"} <= {e["principal"] for e in client.get("/api/activity", headers=bearer(TOKEN)).json()}


def test_a_token_never_approves_what_its_own_grant_asked(node):
    admin = node.grant(subject={"id": "admin-agent"}, admin=True)
    own = asyncio.run(_ask(node, admin, "May I?"))
    owners = asyncio.run(_ask(node, TOKEN, "Owner token agent asks"))
    with node.client() as client:
        # a bearer token is no person: neither its own asks nor the owner token's are decided on it
        r = client.post(f"/api/approvals/{own}/approve", json={"note": "yes"}, headers=bearer(admin))
        assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"
        r = client.post(f"/api/approvals/{owners}/approve", json={"note": "yes"}, headers=bearer(TOKEN))
        assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"
        csrf = node.sign_in(client)  # the human in the panel can
        r = client.post(f"/api/approvals/{own}/approve", json={"note": "yes"}, headers=csrf)
        assert r.status_code == 200 and r.json()["state"] == "approved"
    assert node.policy.approvals.get(owners).state == "pending"
    traced = node.policy.traces.query(kind="panel")[0]
    assert any(e.tool == "panel.approval.approve" and e.status == "ok" and e.principal == "owner" for e in traced)
    assert any(e.status == "denied" and e.principal == "admin-agent" for e in traced)


def test_a_narrowed_child_of_an_admin_never_decides_its_familys_asks(node):
    admin = node.grant(subject={"id": "admin-agent"}, admin=True)
    child = node.policy.attenuate(admin, admin=True, ttl_s=600)[1]  # narrowed, and still admin
    sibling = node.policy.attenuate(admin, admin=True, ttl_s=600)[1]
    parents_ask = asyncio.run(_ask(node, admin, "May I?"))
    siblings_ask = asyncio.run(_ask(node, sibling, "May I, too?"))
    with node.client() as client:
        for ap in (parents_ask, siblings_ask):  # the family's asks, from the admin and from a sibling
            r = client.post(f"/api/approvals/{ap}/approve", json={"note": "yes"}, headers=bearer(child))
            assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"
            r = client.post(f"/api/approvals/{ap}/deny", json={"note": "no"}, headers=bearer(child))
            assert r.status_code == 403 and "self_approval" in r.json()["error"]["message"]
        # a batch deny needs no person, so the family rule answers each item
        r = client.post("/api/approvals/batch", json={"ids": [parents_ask, siblings_ask], "decision": "deny"},
                        headers=bearer(child))
        assert r.status_code == 200
        assert all(not x["ok"] and "self_approval" in x["error"]["message"] for x in r.json()["results"])
        assert node.policy.approvals.get(parents_ask).state == "pending"
        assert node.policy.approvals.get(siblings_ask).state == "pending"
        # a person in the panel decides the family's ask: the owner is not in that family
        r = client.post(f"/api/approvals/{parents_ask}/approve", json={"note": "yes"}, headers=node.sign_in(client))
        assert r.status_code == 200 and r.json()["state"] == "approved"


def _partial_run(node: Node, token: str, run_id: str) -> str:
    """A run its grant started that stopped part way and asks for retry, skip or fail."""
    from agentos.core.models import RunStatus
    from agentos.runner.models import Run

    grant = node.policy.context_for_token(token, tool="flow_run").grant
    runner = node.runtime.require("runner")
    runner.store.insert_run(Run(id=run_id, lane="biz-a/instagram", flow_id="biz-a.instagram.post",
                                status=RunStatus.PARTIAL, grant_id=grant.id,
                                decision={"needed": {"path": "0", "reason": "the click may have posted"}}))
    return run_id


def test_a_narrowed_child_of_an_admin_never_decides_its_familys_partial_runs(node):
    """/api/runs/{id}/decide is the run's approval: retry re-posts, skip marks it done. A decision is a
    person's, so a token (any family's) gets presence_required, and the owner signed in decides it."""
    admin = node.grant(subject={"id": "admin-agent"}, admin=True)
    child = node.policy.attenuate(admin, admin=True, ttl_s=600)[1]
    parents = _partial_run(node, admin, "run_parents")
    childs = _partial_run(node, child, "run_childs")
    runner = node.runtime.require("runner")
    with node.client() as client:
        for run_id, token in ((parents, child), (childs, child), (parents, admin)):
            r = client.post(f"/api/runs/{run_id}/decide", json={"decision": "skip"}, headers=bearer(token))
            assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required", r.text
        assert runner.get(parents).status == "partial" and runner.get(childs).status == "partial"
        csrf = node.sign_in(client)
        r = client.post(f"/api/runs/{parents}/decide", json={"decision": "fail"}, headers=csrf)
        assert r.status_code == 200 and r.json()["status"] == "failed", r.text
        r = client.post(f"/api/runs/{childs}/decide", json={"decision": "fail"}, headers=csrf)
        assert r.status_code == 200 and r.json()["status"] == "failed", r.text


def test_a_partial_run_whose_grant_is_revoked_is_decided_only_in_the_panel(node):
    """A run whose grant was revoked is decided in the panel by the owner, and never by a token (fail closed)."""
    admin = node.grant(subject={"id": "admin-agent"}, admin=True)
    child_grant, child = node.policy.attenuate(admin, admin=True, ttl_s=600)
    run_id = _partial_run(node, child, "run_revoked")
    node.policy.revoke(child_grant.id)
    other = node.grant(subject={"id": "other-admin"}, admin=True)
    with node.client() as client:
        r = client.post(f"/api/runs/{run_id}/decide", json={"decision": "fail"}, headers=bearer(other))
        assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required", r.text
        r = client.post(f"/api/runs/{run_id}/decide", json={"decision": "fail"}, headers=node.sign_in(client))
        assert r.status_code == 200 and r.json()["status"] == "failed", r.text


def test_a_narrowed_child_of_an_admin_never_answers_its_familys_questions(node):
    admin = node.grant(subject={"id": "admin-agent"}, admin=True)
    child = node.policy.attenuate(admin, admin=True, ttl_s=600)[1]
    question = asyncio.run(_ask(node, admin, "Which one?"))
    with node.client() as client:
        r = client.post(f"/api/questions/{question}/answer", json={"answer": "yes"}, headers=bearer(child))
        assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"
    assert node.policy.approvals.get(question).state == "pending"


def test_questions_stay_in_the_grant_family_and_only_the_owner_answers(node):
    mine, other = node.grant(subject={"id": "mine"}), node.grant(subject={"id": "other"})
    a = asyncio.run(_ask(node, mine, "Post the draft?"))
    b = asyncio.run(_ask(node, other, "Delete the old post?"))
    with node.client() as client:
        assert [q["id"] for q in client.get("/api/questions", headers=bearer(mine)).json()] == [a]
        assert {q["id"] for q in client.get("/api/questions", headers=bearer(TOKEN)).json()} == {a, b}
        assert client.get("/api/questions", headers=bearer(mine)).json()[0]["asked_by"] == {
            "kind": "agent", "id": "mine", "thread": None}
        r = client.post(f"/api/questions/{a}/answer", json={"answer": "yes"}, headers=bearer(mine))
        assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"
        assert node.policy.approvals.get(a).state == "pending"
    denied = node.policy.traces.query(kind="panel", status="denied", limit=500)[0]
    assert any(e.tool == "panel.question.answer" and e.principal == "mine" for e in denied)


def test_the_owner_answers_a_question_through_the_policy(node):
    agent = node.grant(subject={"id": "asker"})
    qid = asyncio.run(_ask(node, agent, "Use the blue photo?"))
    with node.client() as client:
        csrf = node.sign_in(client)
        assert client.post(f"/api/questions/{qid}/answer", json={"answer": "yes"}).status_code == 403
        r = client.post(f"/api/questions/{qid}/answer", json={"answer": "yes"}, headers=csrf)
        assert r.status_code == 200 and r.json()["state"] == "answered" and r.json()["answer"] == "yes"
        assert r.json()["asked_by"] == {"kind": "agent", "id": "asker", "thread": None}
        assert client.post(f"/api/questions/{qid}/answer", json={"answer": "no"}, headers=csrf).status_code == 409
    status = asyncio.run(approval_status(node.policy.context_for_token(agent, tool="approval_status"),
                                         approval_id=qid))
    assert status["state"] == "approved" and status["answer"] == "yes"  # the agent reads the reply it asked for
    traced = node.policy.traces.query(kind="panel", limit=50)[0]
    assert any(e.tool == "panel.question.answer" and e.status == "ok" and e.principal == "owner" for e in traced)


def test_a_grant_sees_its_own_schedules_and_the_owner_changes_them(node):
    node.runtime.flows.save({"id": "biz-a.instagram.look", "lane": "biz-a/instagram",
                             "steps": [{"type": "navigate", "url": "https://www.instagram.com/"}]})
    sch = node.runtime.runner.scheduler
    mine_token = node.grant(subject={"id": "mine"}, ceiling="read")
    mine = node.policy.context_for_token(mine_token, tool="panel").grant.id
    own, _ = sch.add(flow="biz-a.instagram.look", cron="0 9 * * *",
                     principal={"kind": "agent", "id": "mine", "grant_id": mine})
    owners, _ = sch.add(flow="biz-a.instagram.look", cron="30 8 * * *", principal={})
    with node.client() as client:
        assert {s["id"] for s in client.get("/api/schedules", headers=bearer(mine_token)).json()} == {own.id}
        assert client.get(f"/api/schedules/{own.id}", headers=bearer(mine_token)).json()["grant_id"] == mine
        assert client.get(f"/api/schedules/{owners.id}", headers=bearer(mine_token)).status_code == 404
        assert client.patch(f"/api/schedules/{own.id}", json={"enabled": False},
                            headers=bearer(mine_token)).status_code == 403
        assert {s["id"] for s in client.get("/api/schedules", headers=bearer(TOKEN)).json()} == {own.id, owners.id}
        csrf = node.sign_in(client)
        paused = client.patch(f"/api/schedules/{own.id}", json={"enabled": False}, headers=csrf).json()
        assert paused["enabled"] is False and paused["next_fire_at"] is None and paused["cron"] == "0 9 * * *"
        assert paused["grant_id"] == mine  # toggling keeps the grant the schedule runs under
        resumed = client.patch(f"/api/schedules/{own.id}", json={"enabled": True}, headers=csrf).json()
        assert resumed["enabled"] is True and resumed["grant_id"] == mine
        zone = client.get("/api/status", headers=bearer(TOKEN)).json()["timezone"]
        fire = datetime.fromisoformat(resumed["next_fire_at"])
        assert fire > datetime.now(UTC) and fire.utcoffset() == fire.astimezone(ZoneInfo(zone)).utcoffset()


def test_schedule_changes_reach_the_event_bus_once(node):
    bus = node.runtime.events
    node.runtime.flows.save({"id": "biz-a.instagram.look", "lane": "biz-a/instagram",
                             "steps": [{"type": "navigate", "url": "https://www.instagram.com/"}]})
    with node.client() as client:
        csrf = node.sign_in(client)
        sid = client.post("/api/schedules", json={"flow": "biz-a.instagram.look", "cron": "0 9 * * *"},
                          headers=csrf).json()["id"]
        _wait_for(bus, "schedule", lambda d: d.get("id") == sid and d["enabled"] is True)
        client.patch(f"/api/schedules/{sid}", json={"enabled": False}, headers=csrf)
        _wait_for(bus, "schedule", lambda d: d.get("id") == sid and d["enabled"] is False)
        client.delete(f"/api/schedules/{sid}", headers=csrf)
        gone = _wait_for(bus, "schedule", lambda d: d.get("id") == sid and d.get("deleted") is True)
    assert gone["grant_id"] == "owner"
    sent = [e.data for e in bus.since(0) if e.kind == "schedule" and e.data.get("id") == sid]
    assert [e.get("enabled") for e in sent] == [True, False, False]  # the poll did not send them again


def test_the_schedulers_own_changes_reach_the_bus_through_the_poll(node):
    """A fire or a dead-man pause happens inside the scheduler, not in a route: the poll sends it."""
    bus = node.runtime.events
    node.runtime.flows.save({"id": "biz-a.instagram.look", "lane": "biz-a/instagram",
                             "steps": [{"type": "navigate", "url": "https://www.instagram.com/"}]})
    sch = node.runtime.runner.scheduler
    s, _ = sch.add(flow="biz-a.instagram.look", cron="0 9 * * *", principal={})
    bridge = node.api_app.state.api.bridge

    async def scenario() -> None:
        task = asyncio.create_task(bridge.pump_schedules(bus, interval=0.02))
        await asyncio.sleep(0.1)  # the first pass records what exists and announces nothing
        sch.set_enabled(s.id, False)
        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    sent = [e.data for e in bus.since(0) if e.kind == "schedule" and e.data.get("id") == s.id]
    assert [e["enabled"] for e in sent] == [False]


def test_a_schedules_last_run_is_its_latest_run(node):
    node.runtime.flows.save({"id": "biz-a.instagram.look", "lane": "biz-a/instagram",
                             "steps": [{"type": "navigate", "url": "https://www.instagram.com/"}]})
    sch = node.runtime.runner.scheduler
    s, _ = sch.add(flow="biz-a.instagram.look", cron="0 9 * * *", principal={})
    run, _ = node.runtime.runner.submit(flow_id="biz-a.instagram.look", principal={})
    s.state["last_run_id"] = run.id
    sch.store.put_schedule(s)
    with node.client() as client:
        listed = next(x for x in client.get("/api/schedules", headers=bearer(TOKEN)).json() if x["id"] == s.id)
        assert listed["last_run"]["id"] == run.id and listed["last_run"]["status"] == "queued"


def test_flow_runs_go_through_policy(node):
    flows = node.runtime.flows
    flows.save({"id": "biz-a.instagram.check", "lane": "biz-a/instagram", "effect": "commit",
                "steps": [{"type": "navigate", "url": "https://www.instagram.com/"}]})
    reader, poster = node.grant(subject={"id": "reader"}), node.grant(subject={"id": "poster"}, ceiling="commit")
    with node.client() as client:
        r = client.post("/api/flows/biz-a.instagram.check/run", json={}, headers=bearer(reader))
        assert r.status_code == 403 and "ceiling" in r.json()["error"]["message"]
        r = client.post("/api/flows/biz-a.instagram.check/run", json={}, headers=bearer(poster))
        assert r.status_code == 200 and r.json()["status"] == "waiting_approval" and r.json()["approval_id"]
        csrf = node.sign_in(client)  # the owner clicking Run is the co-sign
        r = client.post("/api/flows/biz-a.instagram.check/run", json={}, headers=csrf)
        assert r.status_code == 200 and r.json()["status"] == "queued"
        assert [f["id"] for f in client.get("/api/flows", headers=bearer(reader)).json()] == ["biz-a.instagram.check"]
        other = node.grant(subject={"id": "elsewhere"}, workspaces=["biz-b"])
        assert client.get("/api/flows", headers=bearer(other)).json() == []
        assert client.get("/api/runs", headers=bearer(other)).json() == []
        assert len(client.get("/api/runs", headers=bearer(TOKEN)).json()) == 2
    assert node.runtime.runner.get(r.json()["id"]).authorized


# ------------------------------------------------------------------ terminal and live view

class FakeShell:
    id = "sh_test"
    running = True

    def __init__(self):
        self.q: asyncio.Queue | None = None
        self.written: list[bytes] = []

    def subscribe(self, *, replay=True):
        self.q = asyncio.Queue()
        self.q.put_nowait(b"$ ")
        return self.q

    def unsubscribe(self, q):
        pass

    async def write(self, data: bytes) -> int:
        self.written.append(data)
        await self.q.put(data.upper())
        return len(data)

    def resize(self, cols, rows):
        pass


@pytest.fixture
def shell(node, monkeypatch):
    envs = node.runtime.envs
    envs.create("biz-a/shell", kind="terminal", preset="workbench", node_secrets_ok=True)
    fake = FakeShell()
    closed: list[str] = []

    async def shell_open(lane_id, **kw):
        return fake

    async def shell_close(sid):
        closed.append(sid)

    async def started():
        return None

    monkeypatch.setattr(envs, "shell_open", shell_open)
    monkeypatch.setattr(envs, "shell_close", shell_close)
    monkeypatch.setattr(envs, "ensure_started", started)
    return fake, closed


def _refused(client, url, headers) -> WebSocketDisconnect:
    with client.websocket_connect(WS + url, headers=headers) as ws, pytest.raises(WebSocketDisconnect) as closed:
        ws.receive_text()
    return closed.value


def test_terminal_is_exec_through_policy(node, shell):
    _fake, closed = shell
    url = "/api/lanes/biz-a/shell/terminal"
    no_exec = node.grant(subject={"id": "no-exec"})
    other_lane = node.grant(subject={"id": "elsewhere"}, workspaces=["biz-b"], specials={"exec": {}})
    dev = node.grant(subject={"id": "dev"}, specials={"exec": {}})
    with node.client() as client:
        assert client.get("/api/lanes", headers=bearer(TOKEN)).json()[-1]["terminal"]["ws_path"] == url
        assert _refused(client, url, bearer(no_exec)).code == 4403
        assert _refused(client, url, bearer(other_lane)).code == 4403
        pending = _refused(client, url, bearer(dev))  # a shared (workbench) lane: exec is co-signed
        assert pending.code == 4202
        approval = re.search(r"ap_[a-z0-9]+", pending.reason).group(0)
        csrf = node.sign_in(client)
        assert client.post(f"/api/approvals/{approval}/approve", json={}, headers=csrf).status_code == 200
        with client.websocket_connect(WS + url, headers=bearer(dev)) as ws:  # approved: the same call goes through
            assert ws.receive_text() == "$ "
            ws.send_text("ls\n")
            assert ws.receive_text() == "LS\n"
        sid = client.cookies.get(SESSION_COOKIE)
        with client.websocket_connect(WS + url, headers={"Cookie": f"{SESSION_COOKIE}={sid}"}) as ws:
            assert ws.receive_text() == "$ "  # the owner in person is the co-signer
    assert closed == ["sh_test", "sh_test"]
    traced = node.policy.traces.query(lane="biz-a/shell", limit=100)[0]
    assert {(e.tool, e.status) for e in traced} >= {("panel.lane.terminal", "ok"), ("panel.lane.terminal", "denied")}
    assert any(e.kind == "authorize" and e.status == "pending" for e in traced)


def test_a_terminal_refuses_a_cross_origin_handshake_even_with_a_valid_token(node, shell):
    """The panel's origin check runs before the credential is read: a page on another site cannot open a
    shell, with the owner's signed-in cookie or with a token that is good here. The socket closes 4401."""
    url = "/api/lanes/biz-a/shell/terminal"
    with node.client() as client:
        node.sign_in(client)
        sid = client.cookies.get(SESSION_COOKIE)
        same_site = {"Cookie": f"{SESSION_COOKIE}={sid}", "origin": "http://localhost:7400"}
        with client.websocket_connect(WS + url, headers=same_site) as ws:
            assert ws.receive_text() == "$ "  # the panel's own origin: the owner's shell opens
        for cross_site in ({"Cookie": f"{SESSION_COOKIE}={sid}", "origin": "https://evil.example"},
                           {**bearer(TOKEN), "origin": "https://evil.example"}):
            with pytest.raises(WebSocketDisconnect) as refused:  # refused before the socket is accepted
                client.websocket_connect(WS + url, headers=cross_site).__enter__()
            assert refused.value.code == 4401


def test_a_lane_with_shells_off_refuses_the_terminal_and_says_why(node, monkeypatch):
    """A service lane's preset turns shells off. The socket closes with the reason, so the panel can show it."""
    from agentos.env.host import FakeHost
    envs = node.runtime.envs
    monkeypatch.setattr(envs, "host", FakeHost())  # a node that can run a service lane's sandbox
    envs.create("biz-a/svc", kind="service", preset="service", service={"command": ["true"]})

    async def started():
        return None

    monkeypatch.setattr(envs, "ensure_started", started)  # no sandbox is started for a refused shell
    with node.client() as client:
        node.sign_in(client)  # the owner in person co-signs the exec, so the refusal is about shells alone
        cookie = {"Cookie": f"{SESSION_COOKIE}={client.cookies.get(SESSION_COOKIE)}"}
        refused = _refused(client, "/api/lanes/biz-a/svc/terminal", cookie)
    assert refused.code == 4503 and "interactive shells are off in service lanes" in refused.reason


def test_the_live_view_relay_is_the_owners(node):
    asyncio.run(node.lanes.open("biz-a/instagram"))
    with node.client() as client:
        assert _refused(client, "/api/lanes/biz-a/instagram/vnc", bearer(node.grant())).code == 4403


# ------------------------------------------------------------------ live updates

def _wait_for(bus, kind: str, pred, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for ev in bus.since(0) or []:
            if ev.kind == kind and pred(ev.data):
                return ev.data
        time.sleep(0.02)
    raise AssertionError(f"no {kind} event")


def test_approvals_runs_and_lanes_reach_the_event_bus(node):
    bus = node.runtime.events
    assert node.runtime.get("events") is node.api_app.state.api.bus  # one bus, registered first
    with node.client():
        ap = asyncio.run(_ask(node, node.grant(), "Ready?"))
        created = _wait_for(bus, "approval", lambda d: d.get("id") == ap)
        assert created["state"] == "pending" and "grant_id" not in created["requested_by"]
        node.policy.approvals.decide(ap, "approve", by="owner", answer="yes")
        _wait_for(bus, "approval", lambda d: d.get("id") == ap and d.get("state") == "approved")
        node.runtime.flows.save({"id": "biz-a.instagram.look", "lane": "biz-a/instagram",
                                 "steps": [{"type": "navigate", "url": "https://www.instagram.com/"}]})
        run, _ = node.runtime.runner.submit(flow_id="biz-a.instagram.look",
                                          principal={"kind": "human", "id": "owner", "grant_id": "owner"})
        assert _wait_for(bus, "run", lambda d: d.get("id") == run.id)["status"] == "queued"
        asyncio.run(node.lanes.open("personal/reddit"))  # a change the lane manager makes on its own
        assert _wait_for(bus, "lane", lambda d: d.get("id") == "personal/reddit")["state"] == "warm"
        _wait_for(bus, "trace", lambda d: d.get("tool") == "human_ask")


def test_questions_reach_the_event_bus(node):
    bus = node.runtime.events
    with node.client():
        qid = asyncio.run(_ask(node, node.grant(), "Ready?"))
        created = _wait_for(bus, "question", lambda d: d.get("id") == qid)
        assert created["state"] == "open" and created["question"] == "Ready?" and "grant_id" not in str(created)
        node.policy.approvals.decide(qid, "approve", by="human:owner", answer="yes")
        assert _wait_for(bus, "question", lambda d: d.get("id") == qid and d.get("state") == "answered")["answer"] \
            == "yes"


def test_a_grant_hears_only_its_own_lanes(node):
    ctx = node.policy.context_for_token(node.grant(), tool="panel")
    access = PolicyAccess(ctx, presence=Presence.AGENT)  # a grant is an agent's credential
    assert access.filter_event("lane", {"id": "biz-a/instagram"}) == {"id": "biz-a/instagram"}
    assert access.filter_event("lane", {"id": "personal/reddit"}) is None
    assert access.filter_event("run", {"lane": "biz-b/facebook"}) is None
    assert access.filter_event("approval", {"id": "ap_1", "summary": "secret plans"}) == {}
    assert access.filter_event("trace", {"summary": "x"}) == {} and access.filter_event("node", {}) is None
    owner = PolicyAccess(node.policy.owner_context(), presence=Presence.PERSON)
    assert owner.filter_event("approval", {"id": "ap_1"}) == {"id": "ap_1"}


def test_a_grant_stream_still_gets_its_heartbeat(node, tmp_path):
    """Lane events the grant may not see are not traffic: the comment still comes on schedule."""
    api = build_api(node.runtime, ApiSettings(sse_heartbeat=0.1, sse_max_seconds=1.5, lane_pump_seconds=0,
                                              panel_dir=tmp_path / "none"))
    stop = threading.Event()

    def noise():
        while not stop.is_set():
            node.runtime.events.publish("lane", {"id": "personal/reddit", "state": "warm"})
            time.sleep(0.005)

    worker = threading.Thread(target=noise, daemon=True)
    worker.start()
    try:
        with TestClient(api, base_url="http://localhost:7400").stream(
                "GET", "/api/events", headers=bearer(node.grant())) as resp:
            lines = list(resp.iter_lines())
    finally:
        stop.set()
        worker.join()
    assert lines.count(": ping") >= 5 and "event: lane" not in lines


def test_the_panel_lifespan_runs_its_feeds_and_stops_them(node, tmp_path):
    node.runtime.flows.save({"id": "biz-a.instagram.look", "lane": "biz-a/instagram",
                             "steps": [{"type": "navigate", "url": "https://www.instagram.com/"}]})
    sch = node.runtime.runner.scheduler
    s, _ = sch.add(flow="biz-a.instagram.look", cron="0 9 * * *", principal={})
    api = build_api(node.runtime, ApiSettings(lane_pump_seconds=0.02, schedule_pump_seconds=0.02,
                                              brain_pump_seconds=0, panel_dir=tmp_path / "none"))
    with TestClient(api):  # the schedule feed runs while the app does
        time.sleep(0.05)  # its first pass records the schedule
        sch.set_enabled(s.id, False)
        _wait_for(node.runtime.events, "schedule", lambda d: d.get("id") == s.id and d["enabled"] is False)


def test_events_service_is_well_known(node):
    from agentos.core import runtime as runtime_module

    assert "events" in runtime_module.__doc__ and node.runtime.get("feature_services")[0][0] == "agentos.api.events"
    assert Effect.EXEC.value == "exec"


def test_every_screen_loads_for_the_owner_on_real_services(node):
    """What the panel's screens fetch, against the real services, with the owner's session."""
    node.runtime.flows.save({"id": "biz-a.instagram.look", "lane": "biz-a/instagram",
                             "steps": [{"type": "navigate", "url": "https://www.instagram.com/"}]})
    with node.client() as client:
        csrf = node.sign_in(client)
        for path, method, _ in [(p, m, 0) for m, p in api_routes(node) if m == "GET" and "{" not in p]:
            if path == "/api/events":
                continue  # a stream; its filtering is tested above
            r = client.request(method, path)
            assert r.status_code == 200, (path, r.text)
            assert "—" not in r.text and TOKEN not in r.text

        # vault: write-only secret, names back, delete
        secret = "correct-horse-battery-staple-42"
        made = client.post("/api/vault", json={"name": "ig", "kind": "password", "domain": "instagram.com",
                                               "lanes": ["biz-a/instagram"], "username": "me", "secret": secret},
                           headers=csrf)
        assert made.status_code == 200 and secret not in made.text and set(made.json()) >= {"id", "name", "kind"}
        assert [e["name"] for e in client.get("/api/vault").json()] == ["ig"]
        assert client.delete(f"/api/vault/{made.json()['id']}", headers=csrf).status_code == 200
        # standing approvals
        # a standing approval must name what the owner saw (AUD-10)
        unbound = client.post("/api/approvals/standing", json={"lane": "biz-a/instagram", "effect": "commit",
                                                              "ttl_seconds": 3600}, headers=csrf)
        assert unbound.status_code == 400 and "must name what it covers" in unbound.text
        assert client.get("/api/approvals/standing").json() == []
        # schedules
        sc = client.post("/api/schedules", json={"flow": "biz-a.instagram.look", "cron": "0 9 * * *"}, headers=csrf)
        assert sc.status_code == 200, sc.text
        assert sc.json()["cron"] == "0 9 * * *" and sc.json()["next_run"]
        assert client.delete(f"/api/schedules/{sc.json()['id']}", headers=csrf).status_code == 200
        # grants: issue, list, revoke
        g = client.post("/api/grants", json={"subject": "w", "workspaces": ["biz-a"], "ceiling": "draft",
                                             "specials": ["secret"], "ttl": "2h"}, headers=csrf).json()
        assert g["grant"]["ceiling"] == "draft" and g["grant"]["specials"] == ["secret"] and "token" not in g["grant"]
        assert client.post(f"/api/grants/{g['id']}/revoke", headers=csrf).json()["revoked"] is True
        assert next(x for x in client.get("/api/grants").json() if x["id"] == g["id"])["revoked"] is True
        # notifications: public fields only, test reports per channel
        put = client.put("/api/settings/notifications", json={"ntfy": {"topic": "agentos-test", "token": "tk_secret_1",
                                                                     "enabled": False}}, headers=csrf)  # no network
        assert put.status_code == 200 and "tk_secret_1" not in put.text and put.json()["ntfy"]["token_saved"] is True
        tested = client.post("/api/settings/notifications/test", headers=csrf).json()
        assert tested["ok"] is True and tested["detail"] == "log: delivered"
        # vault seal state (a mounted vault route)
        assert client.post("/api/vault/seal", headers=csrf).json()["state"] == "sealed"
        assert client.post("/api/vault/unseal", json={}, headers=csrf).json()["state"] == "unsealed"


async def test_panel_template_apply_keeps_the_workspace_proxy_rules(tmp_path):
    """The panel's template apply goes through agentos.workspaces: one proxy for two businesses
    is refused with 409 and nothing is created; the panel lists the workspace templates."""
    from fakes import FakeInteraction, FakeLaneManager
    from starlette.testclient import TestClient

    from agentos.api.app import ApiSettings, build_api
    from agentos.core.config import NodeConfig, save_proxies
    from agentos.core.models import ProxyConfig
    from agentos.node.daemon import build_services, node_runtime

    cfg = NodeConfig(home=tmp_path, mcp_token="tok-panel-ws")
    cfg.secrets_dir.mkdir(parents=True)
    save_proxies(cfg, {"lt1": ProxyConfig(label="lt1", host="198.51.100.7", port=8080)})
    manager = FakeLaneManager(cfg)
    runtime = node_runtime(cfg, manager, FakeInteraction())
    build_services(runtime, ["agentos.workspaces.service"])
    app = build_api(runtime, ApiSettings(owner_token="tok-panel-ws", home=tmp_path))
    auth = {"Authorization": "Bearer tok-panel-ws"}
    with TestClient(app, base_url="http://localhost") as http:
        names = {t["id"] for t in http.get("/api/templates", headers=auth).json()}
        assert {"social-6", "personal"} <= names
        first = http.post("/api/templates/apply", headers=auth,
                          json={"template": "social-6", "workspaces": ["acme"], "proxy_per_workspace": {"acme": "lt1"}})
        assert first.status_code == 200, first.text
        assert len(first.json()["created"]) == 6
        second = http.post("/api/templates/apply", headers=auth,
                           json={"template": "social-6", "workspaces": ["beta"], "proxy_per_workspace": {"beta": "lt1"}})
        assert second.status_code == 409, second.text
        assert not [lane for lane in manager.lanes if lane.startswith("beta/")]
        listed = http.get("/api/workspaces", headers=auth)
        assert listed.status_code == 200 and "acme" in {w["id"] for w in listed.json()}


def test_lane_json_shows_the_lane_managers_signal_and_hold():
    from datetime import UTC, datetime, timedelta

    from agentos.api.lanes_view import lane_json
    from agentos.core.models import LaneInfo, LaneSpec, Signal

    until = datetime.now(UTC) + timedelta(hours=6)
    info = LaneInfo(spec=LaneSpec(id="biz-a/instagram"), signal=Signal.RATE_LIMITED,
                    hold="backoff:rate_limited", hold_until=until)
    out = lane_json(info, hints=None, proxies={}, node="node-1")
    assert out["signal"] == "rate_limited" and out["hold"] == "backoff:rate_limited" and out["hold_until"]
    assert lane_json(LaneInfo(spec=LaneSpec(id="a/b")), hints=None, proxies={}, node="n")["signal"] == "unknown"


def test_a_flow_declared_read_that_types_is_run_as_the_commit_it_is(node):
    """The declared effect is not trusted on its own: a step that can publish makes the run a commit,
    so a read grant cannot run it and Ask me first holds it for a person."""
    flows = node.runtime.flows
    flows.save({"id": "biz-a.instagram.sneak", "lane": "biz-a/instagram", "effect": "read",
                "steps": [{"type": "navigate", "url": "https://www.instagram.com/"},
                          {"type": "type", "locator": {"role": "textbox", "name": "Caption"}, "text": "hello"}]})
    reader, poster = node.grant(subject={"id": "reader"}), node.grant(subject={"id": "poster"}, ceiling="commit")
    node.policy.ask_first.set("flow", "biz-a.instagram.sneak", True, by="owner", person=True)
    with node.client() as client:
        r = client.post("/api/flows/biz-a.instagram.sneak/run", json={}, headers=bearer(reader))
        assert r.status_code == 403 and "ceiling" in r.json()["error"]["message"]
        r = client.post("/api/flows/biz-a.instagram.sneak/run", json={}, headers=bearer(poster))
        assert r.status_code == 200 and r.json()["status"] == "waiting_approval" and r.json()["approval_id"]
        listed = next(f for f in client.get("/api/flows", headers=bearer(poster)).json() if f["id"] == "biz-a.instagram.sneak")
        assert listed["effect"] == "commit"


def test_a_flow_run_let_through_on_its_own_asks_if_ask_me_first_is_turned_on_before_it_runs(node):
    flows = node.runtime.flows
    flows.save({"id": "biz-a.instagram.post", "lane": "biz-a/instagram", "effect": "commit",
                "steps": [{"type": "navigate", "url": "https://www.instagram.com/"}]})
    poster = node.grant(subject={"id": "poster"}, ceiling="commit")
    node.policy.ask_first.set("flow", "biz-a.instagram.post", False, by="owner", person=True)
    node.policy.ask_first.set("lane", "biz-a/instagram", False, by="owner", person=True)
    with node.client() as client:
        r = client.post("/api/flows/biz-a.instagram.post/run", json={}, headers=bearer(poster))
        assert r.status_code == 200 and r.json()["status"] == "queued", r.text
    run = node.runtime.runner.get(r.json()["id"])
    assert run.authorized and run.auto  # so the runner reads the switch again right before it runs


def test_the_nodes_cli_removes_a_terminal_lane_through_agentosd(node, monkeypatch):
    """QA 2026-11-10, finding 30: while agentosd runs, `agentos env remove` and `agentos env doctor` go
    through it. Removing needs a person: the node token alone is not one (an agent may hold it)."""
    from agentos.env.host import FakeHost
    envs = node.runtime.envs
    monkeypatch.setattr(envs, "host", FakeHost())
    envs.create("biz-a/box")
    name = getattr(node.config, "name", None) or "node-1"
    path = f"/api/nodes/{name}/environments/biz-a/box"

    def person() -> dict:
        code = f"cli-{time.time_ns()}-abcdefghijklmnop"
        PanelStore.for_home(node.config.home).add_code(code, 60)
        return {**bearer(TOKEN), PRESENCE_HEADER: code}

    with node.client() as client:
        r = client.delete(path, headers=bearer(TOKEN))
        assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required", r.text
        assert envs.has("biz-a/box")
        r = client.delete(path, headers=bearer(node.grant(ceiling="irreversible")))
        assert r.status_code == 403 and envs.has("biz-a/box")
        r = client.delete(path, headers=person())
        assert r.status_code == 200 and r.json() == {"removed": "biz-a/box"}, r.text
        assert not envs.has("biz-a/box")
        r = client.delete(path, headers=person())
        assert r.json()["error"]["code"] == "lane_not_found"
        report = client.get(f"/api/nodes/{name}/environments/doctor", headers=bearer(TOKEN)).json()
        assert "isolation" in report and "host" in report
