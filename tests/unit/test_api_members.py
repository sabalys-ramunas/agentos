"""Members in the panel, on the real policy: invites,
passkey sign-in with a software authenticator, every panel route against every role, step-up for
decisions, removal and role changes at the next request, and tokens kept out of member routes."""

from __future__ import annotations

import asyncio
import re
import threading
import time
from typing import Self

import pytest
from soft_authenticator import SoftAuthenticator
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_node_panel import TOKEN, WS, Node, bearer, concrete
from test_node_panel import api_routes as every_route

from agentos.api import realtime
from agentos.api.members import CAPABILITIES, MEMBER_ROLES
from agentos.api.realtime import WS_NOT_WARM
from agentos.core import live
from agentos.core.errors import ApprovalPending
from agentos.core.models import Effect
from agentos.core.ratelimit import RateLimiter
from agentos.policy.freeze_store import FreezeRecord, scope_key
from agentos.policy.members import MEMBER_ROLES as POLICY_MEMBER_ROLES

ORIGIN = "http://localhost:7400"


@pytest.fixture
def node(tmp_path):
    n = Node(tmp_path)
    n.api_app.state.api.limiter = RateLimiter(per_key=10_000, global_max=10_000)  # many people, one test address
    yield n
    n.close()


class Person:
    """One browser: its own cookies, its CSRF header and its passkeys."""

    def __init__(self, client: TestClient, csrf: str, auth: SoftAuthenticator, member: dict):
        self.client, self.auth, self.member = client, auth, member
        self.h = {"X-Agentos-CSRF": csrf}

    def get(self, url, **kw):
        return self.client.get(url, headers=self.h, **kw)

    def post(self, url, json=None, **kw):
        return self.client.post(url, json=json if json is not None else {}, headers=self.h, **kw)

    def req(self, method, url, json=None):
        return self.client.request(method, url, json=json if json is not None else {}, headers=self.h)

    def step_up(self, approval_id: str | None = None):
        opts = self.post("/api/session/verify/options", {"approval_id": approval_id} if approval_id else {})
        assert opts.status_code == 200, opts.text
        r = self.post("/api/session/verify", {"credential": self.auth.get(opts.json()["publicKey"])})
        assert r.status_code == 200, r.text
        return r.json()


class Browser(TestClient):
    """A browser of its own (cookies). It never runs the app's lifespan (the MCP session manager
    starts once per app), so any number of them talk to one node."""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        return None


def browser(node: Node) -> Browser:
    return Browser(node.app, base_url=ORIGIN)


def owner(node: Node) -> Person:
    client = browser(node)
    return Person(client, node.sign_in(client)["X-Agentos-CSRF"], SoftAuthenticator(ORIGIN), {"id": "owner"})


def invite(by: Person, handle: str, roles: list[dict], **extra):
    r = by.post("/api/invites", {"handle": handle, "roles": roles, **extra})
    return r


def accept(node: Node, code: str, auth: SoftAuthenticator | None = None, **extra) -> Person:
    auth = auth or SoftAuthenticator(ORIGIN)
    client = browser(node)
    opts = client.post("/api/invites/accept/options", json={"code": code})
    assert opts.status_code == 200, opts.text
    r = client.post("/api/invites/accept", json={"code": code, "credential": auth.create(opts.json()["publicKey"]),
                                                  **extra})
    assert r.status_code == 200, r.text
    return Person(client, r.json()["csrf"], auth, r.json()["member"])


def join(node: Node, boss: Person, handle: str, roles: list[dict], **extra) -> Person:
    r = invite(boss, handle, roles, **extra)
    assert r.status_code == 200, r.text
    return accept(node, r.json()["code"])


def role(role_name: str, scope: str = "biz-a", **options) -> dict:
    return {"scope": scope, "role": role_name, "options": options}


async def _pending(node: Node, lane: str, effect=Effect.COMMIT, *, owner_id=None, facts=None, content="post it"):
    g, _ = node.policy.issue(subject={"id": f"agent-{lane.replace('/', '-')}"}, workspaces=[lane.split("/")[0]],
                             ceiling="pay", owner=owner_id)
    ctx = node.policy.context(g, tool="lane_post")
    with pytest.raises(ApprovalPending) as e:
        await ctx.authorize(effect, lane_id=lane, content=content, summary="Do it", facts=facts)
    return e.value.approval_id


def pending(node: Node, lane: str = "biz-a/instagram", effect=Effect.COMMIT, **kw) -> str:
    return asyncio.run(_pending(node, lane, effect, **kw))


# ------------------------------------------------------------------ invites and passkeys

def test_role_names_match_the_policy():
    assert set(MEMBER_ROLES) == set(POLICY_MEMBER_ROLES) == set(CAPABILITIES)


def test_invite_accept_sign_out_and_sign_in_with_the_passkey(node):
    boss = owner(node)
    r = invite(boss, "va", [role("approver")], display_name="Vee")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["link"] == f"{ORIGIN}/invite#code={body['code']}" and body["state"] == "pending"
    assert [i["handle"] for i in boss.get("/api/invites").json()] == ["va"]
    assert "code" not in boss.get("/api/invites").json()[0]  # the code is shown once
    opts = boss.client.post("/api/invites/accept/options", json={"code": body["code"]}).json()
    assert opts["invite"]["roles"][0]["role"] == "approver" and opts["publicKey"]["rp"]["id"] == "localhost"
    va = accept(node, body["code"], timezone="Europe/Vilnius", device_label="phone")
    me = va.get("/api/me").json()
    assert me["member"]["handle"] == "va" and me["member"]["display_name"] == "Vee"
    assert me["member"]["timezone"] == "Europe/Vilnius" and me["principal"]["owner"] is False
    assert me["roles"] == [{"scope": "biz-a", "role": "approver", "options": {}, "expires_at": None}]
    assert set(me["can"]["biz-a"]) == set(CAPABILITIES["approver"])
    assert me["session"]["via"] == "passkey" and me["session"]["step_up_needed"] is False
    member_id = va.member["id"]
    assert member_id.startswith("m_") and len(node.policy.member_grants(member_id)) == 1
    # the invite is used once
    again = va.client.post("/api/invites/accept/options", json={"code": body["code"]})
    assert again.status_code == 401 and "used, or expired" in again.json()["error"]["message"]
    assert boss.get("/api/invites").json() == []
    # sign out, then in again with the passkey (no name typed: a discoverable credential)
    assert va.req("DELETE", "/api/session").status_code == 200
    assert va.get("/api/me").status_code == 401
    opts = va.client.post("/api/session/passkey/options", json={}).json()["publicKey"]
    assert opts["allowCredentials"] == [] and opts["userVerification"] == "required"
    r = va.client.post("/api/session/passkey", json={"credential": va.auth.get(opts)})
    assert r.status_code == 200 and r.json()["member"]["handle"] == "va", r.text
    va.h = {"X-Agentos-CSRF": r.json()["csrf"]}
    assert va.get("/api/me").json()["member"]["id"] == member_id
    # a replayed answer, or a passkey nobody registered, does not sign in
    replay = va.client.post("/api/session/passkey", json={"credential": va.auth.get(opts)})
    assert replay.status_code == 401
    stranger = SoftAuthenticator(ORIGIN)
    stranger.create({"rp": {"id": "localhost"}, "challenge": "eA"})
    fresh = va.client.post("/api/session/passkey/options", json={}).json()["publicKey"]
    r = va.client.post("/api/session/passkey", json={"credential": stranger.get(fresh)})
    assert r.status_code == 401 and "not registered" in r.json()["error"]["message"]
    traced = node.policy.traces.query(kind="panel", limit=200)[0]
    assert any(e.tool == "panel.invite.create" and "approver@biz-a" in (e.summary or "") for e in traced)
    assert any(e.tool == "panel.invite.accept" for e in traced)


def test_expired_reused_revoked_and_bad_invites(node):
    boss = owner(node)
    api = node.api_app.state.api
    api.settings.invite_ttl = -1  # made already expired
    expired = invite(boss, "late", [role("viewer")]).json()["code"]
    api.settings.invite_ttl = 24 * 3600
    with browser(node) as c:
        assert c.post("/api/invites/accept/options", json={"code": expired}).status_code == 401
    revoked = invite(boss, "gone", [role("viewer")]).json()
    assert boss.req("DELETE", f"/api/invites/{revoked['id']}").status_code == 200
    with browser(node) as c:
        assert c.post("/api/invites/accept/options", json={"code": revoked["code"]}).status_code == 401
    # two tabs race for one invite: the second registration is refused
    code = invite(boss, "twice", [role("viewer")]).json()["code"]
    with browser(node) as c1, browser(node) as c2:
        a1, a2 = SoftAuthenticator(ORIGIN), SoftAuthenticator(ORIGIN)
        o1 = c1.post("/api/invites/accept/options", json={"code": code}).json()["publicKey"]
        o2 = c2.post("/api/invites/accept/options", json={"code": code}).json()["publicKey"]
        assert c1.post("/api/invites/accept", json={"code": code, "credential": a1.create(o1)}).status_code == 200
        assert c2.post("/api/invites/accept", json={"code": code, "credential": a2.create(o2)}).status_code == 401
    # a passkey from another site, or one without user verification, is not accepted
    code = invite(boss, "phish", [role("viewer")]).json()["code"]
    with browser(node) as c:
        o = c.post("/api/invites/accept/options", json={"code": code}).json()["publicKey"]
        r = c.post("/api/invites/accept", json={"code": code, "credential": SoftAuthenticator("https://evil.example")
                                                .create(o)})
        assert r.status_code == 401 and "another site" in r.json()["error"]["message"]
        o = c.post("/api/invites/accept/options", json={"code": code}).json()["publicKey"]
        r = c.post("/api/invites/accept", json={"code": code, "credential": SoftAuthenticator(ORIGIN, uv=False)
                                                .create(o)})
        assert r.status_code == 401 and "verify the person" in r.json()["error"]["message"]
    # bad roles, scopes, options and handles
    for roles, why in (([role("owner")], 400), ([role("viewer", scope="Biz A")], 400),
                       ([role("approver", max_usd="lots")], 400), ([role("viewer", colour=1)], 400)):
        r = invite(boss, "bad", roles)
        assert r.status_code == why, (roles, r.text)
    assert invite(boss, "twice", [role("viewer")]).status_code == 409  # a member has it
    assert invite(boss, "Bad Handle", [role("viewer")]).status_code == 400
    # a request from another site is refused before anything happens
    with browser(node) as c:
        r = c.post("/api/invites/accept/options", json={"code": code}, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"


def test_an_admin_invites_only_inside_their_workspace(node):
    boss = owner(node)
    admin = join(node, boss, "ada", [role("admin")])
    ok = invite(admin, "va", [role("approver", max_usd=50)])
    assert ok.status_code == 200, ok.text
    for roles in ([role("approver", scope="biz-b")], [role("admin", scope="biz-b")], [role("viewer", scope="*")],
                  [role("viewer"), role("viewer", scope="personal")]):
        r = invite(admin, "nope", roles)
        assert r.status_code == 403 and r.json()["error"]["code"] == "denied", (roles, r.text)
    assert invite(admin, "co", [role("admin")]).status_code == 200  # at or below their own role
    # members they do not administer are invisible to them
    other = join(node, boss, "bo", [role("viewer", scope="biz-b")])
    listed = {m["handle"] for m in admin.get("/api/members").json()}
    assert listed == {"ada"} and admin.get(f"/api/members/{other.member['id']}").status_code == 404
    assert admin.req("DELETE", f"/api/members/{other.member['id']}").status_code == 404
    assert {m["handle"] for m in boss.get("/api/members").json()} == {"owner", "ada", "bo"}
    # an admin of every workspace may invite for any workspace and for "*"
    wide = join(node, boss, "wide", [role("admin", scope="*")])
    assert invite(wide, "anyone", [role("viewer", scope="biz-b"), role("viewer", scope="*")]).status_code == 200


# ------------------------------------------------------------------ every route, every role

# (method, path) -> the roles at biz-a that may call it. Everything else is the owner's (403 for a member).
EVERY_MEMBER = frozenset(MEMBER_ROLES)
SEES = frozenset({"viewer", "approver", "operator", "admin"})
MEMBER_ROUTES: dict[tuple[str, str], frozenset[str]] = {
    ("GET", "/api/status"): EVERY_MEMBER, ("GET", "/api/templates"): EVERY_MEMBER, ("GET", "/api/brains"): EVERY_MEMBER,
    ("GET", "/api/me"): EVERY_MEMBER, ("GET", "/api/me/devices"): EVERY_MEMBER,
    ("PATCH", "/api/me/details"): EVERY_MEMBER,
    # the web contract (src/agentos/api/web): a person's own sign-in, devices and details are theirs to see
    ("GET", "/api/session"): EVERY_MEMBER, ("PATCH", "/api/me"): EVERY_MEMBER,
    ("GET", "/api/auth/passkeys"): EVERY_MEMBER, ("DELETE", "/api/auth/passkeys/{id}"): EVERY_MEMBER,
    ("POST", "/api/auth/passkey/register/options"): EVERY_MEMBER,
    ("POST", "/api/auth/passkey/register/verify"): EVERY_MEMBER,
    ("POST", "/api/auth/step-up/passkey/options"): EVERY_MEMBER,
    ("POST", "/api/auth/step-up/passkey/verify"): EVERY_MEMBER,
    # a refusal for everyone, since a computer sends no email and has one organisation
    ("POST", "/api/auth/magic-link"): EVERY_MEMBER, ("POST", "/api/auth/magic-link/verify"): EVERY_MEMBER,
    ("POST", "/api/auth/step-up/magic-link"): EVERY_MEMBER, ("POST", "/api/auth/step-up/verify"): EVERY_MEMBER,
    ("GET", "/api/auth/google/start"): EVERY_MEMBER, ("POST", "/api/orgs"): EVERY_MEMBER,
    ("GET", "/api/sessions"): EVERY_MEMBER, ("DELETE", "/api/sessions/{id}"): EVERY_MEMBER,
    ("GET", "/api/orgs/{org}"): EVERY_MEMBER, ("GET", "/api/orgs/{org}/getting-started"): EVERY_MEMBER,
    ("GET", "/api/orgs/{org}/workspaces/{id}"): EVERY_MEMBER,  # one of the businesses they see, with its accounts
    ("GET", "/api/orgs/{org}/audit/days"): SEES, ("GET", "/api/orgs/{org}/audit/lanes/{ws}/{name}"): SEES,  # the node's replay, read in place
    ("GET", "/api/orgs/{org}/members"): frozenset({"admin"}),
    ("PATCH", "/api/orgs/{org}/members/{id}"): frozenset({"admin"}),
    ("GET", "/api/orgs/{org}/invites"): frozenset({"admin"}), ("POST", "/api/orgs/{org}/invites"): frozenset({"admin"}),
    ("GET", "/api/orgs/{org}/settings/notifications"): EVERY_MEMBER,
    ("PUT", "/api/orgs/{org}/settings/notifications"): EVERY_MEMBER,
    ("GET", "/api/orgs/{org}/push/config"): EVERY_MEMBER, ("GET", "/api/orgs/{org}/push/devices"): EVERY_MEMBER,
    ("POST", "/api/orgs/{org}/push/devices"): EVERY_MEMBER,
    ("DELETE", "/api/orgs/{org}/push/devices/{id}"): EVERY_MEMBER, ("POST", "/api/orgs/{org}/push/test"): EVERY_MEMBER,
    ("POST", "/api/invites/{token}/accept"): EVERY_MEMBER, ("POST", "/api/invites/{token}/join"): EVERY_MEMBER,
    ("DELETE", "/api/me/devices/{device}"): EVERY_MEMBER, ("GET", "/api/me/notify"): EVERY_MEMBER,
    ("PUT", "/api/me/notify"): EVERY_MEMBER, ("POST", "/api/me/passkeys/options"): EVERY_MEMBER,
    ("POST", "/api/me/passkeys"): EVERY_MEMBER, ("POST", "/api/session/verify/options"): EVERY_MEMBER,
    ("POST", "/api/session/verify"): EVERY_MEMBER,
    ("GET", "/api/lanes"): EVERY_MEMBER, ("GET", "/api/workspaces"): EVERY_MEMBER,  # filtered to their workspaces
    ("GET", "/api/approvals"): EVERY_MEMBER, ("GET", "/api/approvals/{id}"): EVERY_MEMBER,  # filtered (404)
    ("GET", "/api/approvals/{id}/preview.png"): EVERY_MEMBER, ("GET", "/api/questions"): EVERY_MEMBER,
    ("GET", "/api/approvals/{id}/files/{param}"): EVERY_MEMBER,
    ("GET", "/api/approvals/{id}/messages"): EVERY_MEMBER,  # a send card's messages, filtered (404)
    ("GET", "/api/activity"): EVERY_MEMBER, ("GET", "/api/runs"): EVERY_MEMBER, ("GET", "/api/runs/{id}"): EVERY_MEMBER,
    ("GET", "/api/flows"): EVERY_MEMBER, ("POST", "/api/flows/{id}/run"): EVERY_MEMBER,  # the flow's lane decides
    ("GET", "/api/ask-first"): EVERY_MEMBER, ("POST", "/api/ask-first"): EVERY_MEMBER,  # on: anyone; off: admins
    ("GET", "/api/confirmation"): EVERY_MEMBER, ("GET", "/api/confirmation/methods"): EVERY_MEMBER,  # a person, reading
    ("GET", "/api/schedules"): EVERY_MEMBER, ("GET", "/api/schedules/{id}"): EVERY_MEMBER,
    ("GET", "/api/schedules/{id}/fires"): EVERY_MEMBER,
    ("GET", "/api/lanes/{ws}/{name}"): SEES,
    ("GET", "/api/lanes/{ws}/{name}/metrics"): SEES,  # a lane's run bars: read like the lane itself
    ("GET", "/api/lanes/{ws}/{name}/sites"): SEES,  # read like the lane. Changing the list is the owner's
    ("GET", "/api/audit/replay"): SEES, ("GET", "/api/audit/runs/{id}/screenshot"): SEES,  # their lanes only
    ("GET", "/api/audit/captures/{id}"): SEES,  # a call's kept screenshot: their lanes only, like a run's
    ("GET", "/api/lanes/{ws}/{name}/check"): SEES, ("GET", "/api/lanes/checks"): EVERY_MEMBER,  # filtered
    ("GET", "/api/lanes/{ws}/{name}/keepalive"): SEES,
    ("GET", "/api/lanes/{ws}/{name}/health"): SEES,  # read like the check
    ("GET", "/api/lanes/{ws}/{name}/worker"): SEES, ("PUT", "/api/lanes/{ws}/{name}/worker"): SEES,  # the body decides
    ("PUT", "/api/lanes/{ws}/{name}/worker/ask-first"): SEES,  # on: anyone who sees the lane; off: admins
    ("POST", "/api/lanes/{ws}/{name}/open"): frozenset({"operator", "admin"}),
    ("POST", "/api/lanes/{ws}/{name}/close"): frozenset({"operator", "admin"}),
    ("GET", "/api/lanes/{ws}/{name}/screenshot"): frozenset({"operator", "admin"}),  # control: the live view
    # an assisted login: the members whose grant may take control of the lane; saving to the vault: admins
    **{(m, f"/api/lanes/{{ws}}/{{name}}/login{tail}"): frozenset({"operator", "admin"}) for m, tail in (
        ("GET", ""), ("POST", ""), ("POST", "/step"), ("POST", "/refresh"), ("POST", "/take"), ("POST", "/resume"),
        ("POST", "/cancel"), ("GET", "/screenshot"), ("POST", "/form"))},
    ("POST", "/api/lanes/{ws}/{name}/login/save"): frozenset({"admin"}),
    ("POST", "/api/lanes/{ws}/{name}/login/vault"): frozenset({"admin"}),  # the saved login: a vault role
    ("POST", "/api/approvals/{id}/approve"): frozenset({"approver", "admin"}),
    # a no is a decision too: the same right as a yes, in that approval's workspace
    ("POST", "/api/approvals/{id}/deny"): frozenset({"approver", "admin"}),
    ("POST", "/api/approvals/batch"): frozenset({"approver", "admin"}),
    # "Got it" on a failed outcome removes it for everyone, so it takes the same right as a no: decide, in that workspace
    ("POST", "/api/approvals/{id}/clear"): frozenset({"approver", "admin"}),
    ("GET", "/api/approvals/{id}/review"): EVERY_MEMBER,  # a Review link opened in the panel: visibility checked there
    ("POST", "/api/questions/{id}/answer"): frozenset({"approver", "operator", "admin"}),
    ("GET", "/api/approvals/standing"): frozenset({"admin"}), ("POST", "/api/approvals/standing"): frozenset({"admin"}),
    ("DELETE", "/api/approvals/standing/{id}"): frozenset({"admin"}),
    ("PATCH", "/api/schedules/{id}"): frozenset({"admin"}), ("DELETE", "/api/schedules/{id}"): frozenset({"admin"}),
    ("GET", "/api/vault"): frozenset({"admin"}), ("POST", "/api/vault"): frozenset({"admin"}),
    ("DELETE", "/api/vault/{id}"): frozenset({"admin"}),
    # the vault's state, its lock and a saved login's form: the owner alone, typed on the computer (routes_forms)
    ("GET", "/api/vault/status"): frozenset(), ("POST", "/api/vault/seal"): frozenset(),
    ("POST", "/api/vault/unseal"): frozenset(), ("POST", "/api/forms"): frozenset(),
    ("GET", "/api/forms/trips/{id}"): frozenset(),
    # browsers, connections and moving an account between browsers: the owner alone (routes_devices)
    ("GET", "/api/browser-devices"): frozenset(), ("POST", "/api/browser-devices"): frozenset(),
    ("GET", "/api/browser-devices/{name}"): frozenset(), ("DELETE", "/api/browser-devices/{name}"): frozenset(),
    ("GET", "/api/lanes/{ws}/{name}/device-move"): frozenset(), ("POST", "/api/lanes/{ws}/{name}/device"): frozenset(),
    ("GET", "/api/proxies"): frozenset(), ("POST", "/api/proxies"): frozenset(),
    # a workspace's export and import: the owner alone, sealed with a passphrase typed on the computer
    ("POST", "/api/workspaces/{id}/export"): frozenset(), ("POST", "/api/workspaces/import"): frozenset(),
    ("GET", "/api/grants"): frozenset({"operator", "admin"}), ("POST", "/api/grants"): frozenset({"operator", "admin"}),
    ("POST", "/api/grants/{id}/revoke"): frozenset({"operator", "admin"}),
    ("GET", "/api/freeze"): EVERY_MEMBER, ("POST", "/api/freeze"): frozenset({"admin"}),  # admins freeze, never unfreeze
    ("GET", "/api/meters"): EVERY_MEMBER,  # filtered: their lanes and their own agents
    **{("GET", f"/api/workflows{tail}"): EVERY_MEMBER for tail in (  # filtered to their workspaces
        "", "/catalog", "/day", "/lists", "/{id}", "/{id}/runs", "/{id}/runs/{run}")},
    **{(m, f"/api/workflows{tail}"): frozenset({"operator", "admin"}) for m, tail in (
        ("POST", ""), ("PUT", "/{id}"), ("POST", "/{id}/disable"), ("POST", "/{id}/run"),
        ("POST", "/{id}/runs/{run}/retry"), ("POST", "/suppressions"))},
    **{(m, f"/api/workflows{tail}"): frozenset({"admin"}) for m, tail in (  # lets commits run, or changes who hears
        ("POST", "/{id}/enable"), ("DELETE", "/{id}"), ("POST", "/lists"), ("DELETE", "/suppressions"),
        ("PUT", "/limits/{account}"))},
    ("GET", "/api/lanes/{ws}/{name}/meter"): SEES, ("GET", "/api/lanes/{ws}/{name}/queue"): SEES,
    ("GET", "/api/lanes/{ws}/{name}/pace"): SEES,
    ("GET", "/api/lanes/{ws}/{name}/taint"): SEES,  # which grants: the owner's only
    **{(m, p): frozenset({"admin"}) for m, p in (
        ("GET", "/api/invites"), ("POST", "/api/invites"), ("DELETE", "/api/invites/{id}"), ("GET", "/api/members"),
        ("GET", "/api/members/{id}"), ("PATCH", "/api/members/{id}"), ("DELETE", "/api/members/{id}"),
        ("DELETE", "/api/members/{id}/devices/{device}"))},
}
# The web contract's own reads. Each is built on a node route above, so each is filtered the same way.
MEMBER_ROUTES.update({
    ("GET", "/api/orgs/{org}/home"): EVERY_MEMBER,  # what a person may see of the node's own reads; spend is the owner's (test_home)
    ("GET", "/api/orgs/{org}/topology/night"): frozenset(),  # the owner's map of every computer and account, as /api/topology
    ("GET", "/api/orgs/{org}/metrics/activity"): EVERY_MEMBER,
    ("GET", "/api/orgs/{org}/retention"): EVERY_MEMBER,  # how long things are kept, nothing that was kept
    ("GET", "/api/orgs/{org}/audit"): EVERY_MEMBER, ("GET", "/api/orgs/{org}/history"): EVERY_MEMBER,
    ("GET", "/api/orgs/{org}/routines/catalog"): EVERY_MEMBER, ("GET", "/api/orgs/{org}/routines/day"): EVERY_MEMBER,
    ("GET", "/api/orgs/{org}/standing-approvals"): frozenset({"admin"}),
    ("GET", "/api/orgs/{org}/standing-approvals/support"): EVERY_MEMBER,
    ("POST", "/api/orgs/{org}/flows/{id}/run"): frozenset({"admin", "operator"}),
    # surfaces and connectors: who an account acts as is the owner's to read and to change (a token
    # reads and runs, never adds or removes a surface); a member sees neither
    **{(m, p): frozenset() for m, p in (
        ("GET", "/api/orgs/{org}/surfaces"), ("POST", "/api/orgs/{org}/surfaces"),
        ("GET", "/api/orgs/{org}/surfaces/{ws}/{name}"), ("DELETE", "/api/orgs/{org}/surfaces/{ws}/{name}"),
        ("GET", "/api/orgs/{org}/connectors"), ("POST", "/api/orgs/{org}/connectors/{operation}/run"))},
})
# An account's files (routes_files): reading needs `see`, changing them needs `operate` (operator, admin).
# The web contract passes these through to the node (or checks the same role first), so each has its row too.
MEMBER_ROUTES.update({
    ("GET", "/api/lanes/{ws}/{name}/files"): SEES, ("GET", "/api/lanes/{ws}/{name}/files/{id}/download"): SEES,
    ("POST", "/api/lanes/{ws}/{name}/files"): frozenset({"operator", "admin"}),
    ("DELETE", "/api/lanes/{ws}/{name}/files/{id}"): frozenset({"operator", "admin"}),
    ("GET", "/api/orgs/{org}/lanes/{ws}/{name}/files"): SEES,
    ("GET", "/api/orgs/{org}/lanes/{ws}/{name}/files/{id}/download"): SEES,
    ("POST", "/api/orgs/{org}/lanes/{ws}/{name}/files"): frozenset({"operator", "admin"}),
    ("DELETE", "/api/orgs/{org}/lanes/{ws}/{name}/files/{id}"): frozenset({"operator", "admin"}),
})
# Memory (routes_memory) is the owner's alone, in person: a token, a member and a grant are all refused, so
# nobody is listed as allowed. Its node routes are owner-only and are checked as such above.
MEMBER_ROUTES.update({(m, p): frozenset() for m, p in (
    ("GET", "/api/orgs/{org}/memory/workspaces"), ("GET", "/api/orgs/{org}/memory"),
    ("GET", "/api/orgs/{org}/memory/export"), ("GET", "/api/orgs/{org}/memory/{id}"),
    ("POST", "/api/orgs/{org}/memory/{id}/confirm"), ("POST", "/api/orgs/{org}/memory/{id}/review"),
    ("POST", "/api/orgs/{org}/memory/{id}/forget"))})
# Runs, Checks, Brand and VMs (agentos.api.web jobs, studio, brand, computers): web routes with no node route under
# them, so each says who may call it. Runs are read like a lane's activity: `see`, and only the runs in lanes of
# their workspaces (filtered, 403 for one outside); stopping one is operating the lane. A studio check belongs to
# the workspace of the profile it was checked with, and is read with `see` there; a check with no profile, and its
# images, is the owner's. A brand is a profile: the owner's alone, in person, Needs you's proposals included. A VM
# is a computer the owner rents, so it is the owner's as /api/nodes is.
MEMBER_ROUTES.update({
    ("GET", "/api/orgs/{org}/jobs"): SEES, ("GET", "/api/orgs/{org}/jobs/{id}"): SEES,
    ("POST", "/api/orgs/{org}/jobs/{id}/cancel"): frozenset({"operator", "admin"}),
    **{("GET", f"/api/orgs/{{org}}/studio{tail}"): SEES for tail in (
        "/runs", "/runs/{id}", "/runs/{id}/findings/{fid}/crop", "/captures/{id}/screens/{screen}", "/baseline",
        "/diff", "/profiles/{ws}/{id}/components")},
    **{(m, f"/api/orgs/{{org}}/brand{tail}"): frozenset() for m, tail in (
        ("GET", ""), ("GET", "/proposals"), ("GET", "/{ws}/{id}"), ("POST", "/{ws}/{id}/approve"),
        ("POST", "/{ws}/{id}/reject"))},
    ("GET", "/api/orgs/{org}/vms"): frozenset(),
})
# Environments (agentos.api.web.environments): an agent's cloud computer is read like a run, with `see`, and only
# where the role reaches workspace env (filtered; one outside is not found); closing or extending one is operating.
MEMBER_ROUTES.update({
    ("GET", "/api/orgs/{org}/environments"): SEES, ("GET", "/api/orgs/{org}/environments/{id}"): SEES,
    ("POST", "/api/orgs/{org}/environments/{id}/close"): frozenset({"operator", "admin"}),
    ("POST", "/api/orgs/{org}/environments/{id}/extend"): frozenset({"operator", "admin"}),
})
NOT_IN_MATRIX = {
    ("POST", "/api/orgs/{org}/lanes/{ws}/{name}/control"),  # needs the daemon's lane manager: tests/unit/web
    ("POST", "/api/session"), ("POST", "/api/invites/accept/options"), ("POST", "/api/invites/accept"),
    ("POST", "/api/session/passkey/options"), ("POST", "/api/session/passkey"),  # public (tested above)
    ("GET", "/api/invites/{token}"), ("POST", "/api/invites/{token}/passkey/options"),  # a computer's invite link
    ("POST", "/api/invites/{token}/passkey"),
    ("GET", "/api/auth/providers"), ("POST", "/api/auth/passkey/login/options"),  # the sign-in page, before a session
    ("POST", "/api/auth/passkey/login/verify"),
    ("POST", "/api/approvals/{id}/action"),  # the signed phone link: its signature is the credential
    ("GET", "/api/audit/shared/{token}"), ("GET", "/api/audit/shared/{token}/runs/{id}/screenshot"),  # share links
    ("POST", "/api/freeze/link"),  # a push's Freeze button: the same
    ("GET", "/api/mail/form/{token}"), ("POST", "/api/mail/form/{token}"),  # a job's link: its token is the credential
    ("GET", "/api/brains/form/{token}"), ("POST", "/api/brains/form/{token}"),  # the same
    ("GET", "/api/forms/{token}"), ("POST", "/api/forms/{token}"),  # the node's forms for a secret: the same
    ("GET", "/api/backups/form/{token}"), ("POST", "/api/backups/form/{token}"),  # a backup's passphrase form, the same
    ("DELETE", "/api/session"),  # signs out: tested on its own
    ("GET", "/api/events"),  # a stream: filtered per member (test below)
    ("WS", "/api/lanes/{ws}/{name}/vnc"), ("WS", "/api/lanes/{ws}/{name}/terminal"),  # tested below
}
def api_routes(node) -> list[tuple[str, str]]:
    return every_route(node, contract=True)


ORG = "/api/orgs/{org}"


def twin(route: tuple[str, str]) -> tuple[str, str]:
    """A route of the web contract has the rights of the node route it reshapes."""
    method, path = route
    return (method, "/api" + path[len(ORG):]) if path.startswith(ORG + "/") else route


PATH_PARAMS = {"device": "x1", "param": "image", "batch": "lb_1", "list": "list.biz-a.x"}


def _url(path: str) -> str:
    for k, v in PATH_PARAMS.items():
        path = path.replace("{" + k + "}", v)
    return concrete(path)


def test_every_route_against_every_role(node):
    boss = owner(node)
    people = {r: join(node, boss, r, [role(r)]) for r in MEMBER_ROLES}
    routes = api_routes(node)
    # a route of the web contract is read as the node route it reshapes, unless it is listed itself
    owner_only = [rt for rt in routes if rt not in MEMBER_ROUTES and twin(rt) not in MEMBER_ROUTES
                  and rt not in NOT_IN_MATRIX and twin(rt) not in NOT_IN_MATRIX]
    derived = {rt: MEMBER_ROUTES[twin(rt)] for rt in routes if rt not in MEMBER_ROUTES and twin(rt) in MEMBER_ROUTES}
    assert set(MEMBER_ROUTES) <= set(routes), set(MEMBER_ROUTES) - set(routes)
    assert len(owner_only) > 30 and ("GET", "/api/nodes") in owner_only and ("POST", "/api/lanes") in owner_only
    assert ("POST", "/api/lanes/{workspace}/{name}/sites") in owner_only  # a lane's site list: never a member's
    wrong: list[str] = []
    for (method, path), allowed in [*MEMBER_ROUTES.items(), *derived.items(), *((rt, frozenset()) for rt in owner_only)]:
        for name, person in people.items():
            r = person.req(method, _url(path))
            if name in allowed:
                if r.status_code in (401, 403):
                    wrong.append(f"refused {name}: {method} {path}")
            elif path.startswith(ORG):  # it may answer 400 or 404 first: what it wraps does the refusing
                if r.status_code < 400 or r.status_code in (500, 502):
                    wrong.append(f"let in {name}: {method} {path} ({r.status_code})")
            elif r.status_code != 403 or r.json()["error"]["code"] != "denied":
                wrong.append(f"let in {name}: {method} {path} ({r.status_code})")
            assert "—" not in r.text
    assert wrong == []
    # every refusal is in Activity, with the member's handle
    denied = node.policy.traces.query(kind="panel", status="denied", limit=500)[0]
    assert {e.args.get("member") for e in denied if e.args} >= set(MEMBER_ROLES)


def test_member_cookie_writes_need_csrf_and_bearer_agents_stay_out(node):
    boss = owner(node)
    va = join(node, boss, "va", [role("admin")])
    for method, path in api_routes(node):
        if method in ("GET", "WS") or (method, path) in NOT_IN_MATRIX:
            continue
        r = va.client.request(method, _url(path), json={})
        assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed", (method, path, r.text)
        r = va.client.request(method, _url(path), json={}, headers={**va.h, "Origin": "https://evil.example"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed", (method, path)
    # an agent's token, even an admin grant, is not a person: no member routes
    agent = node.grant()
    admin_agent = node.grant(subject={"id": "boss-bot"}, admin=True)
    with browser(node) as c:
        for tok in (agent, admin_agent):
            for method, path in (("GET", "/api/members"), ("POST", "/api/invites"), ("GET", "/api/invites"),
                                 ("GET", "/api/me/notify"), ("POST", "/api/session/verify/options"),
                                 ("GET", "/api/me/devices"), ("DELETE", f"/api/members/{va.member['id']}")):
                r = c.request(method, path, json={"handle": "x", "roles": [role("viewer")]}, headers=bearer(tok))
                assert r.status_code == 403, (tok[:6], method, path, r.text)
        # the owner's own token (the CLI) may manage members
        assert {m["handle"] for m in c.get("/api/members", headers=bearer(TOKEN)).json()} == {"owner", "va"}
        assert c.get("/api/me", headers=bearer(agent)).json()["member"] is None
    # and a member grant is never a bearer token
    grant = node.policy.member_grants(va.member["id"])[0]
    assert grant.human and node.policy.authenticate_panel(grant.id) is None


# ------------------------------------------------------------------ what a member sees

def test_a_member_sees_only_their_workspace(node):
    boss = owner(node)
    va = join(node, boss, "va", [role("approver")])
    mine, theirs = pending(node, "biz-a/instagram"), pending(node, "biz-b/facebook")
    assert [lane["id"] for lane in va.get("/api/lanes").json()] == ["biz-a/instagram"]
    assert [w["id"] for w in va.get("/api/workspaces").json()] == ["biz-a"]
    assert [a["id"] for a in va.get("/api/approvals").json()] == [mine]
    assert va.get(f"/api/approvals/{mine}").status_code == 200
    assert va.get(f"/api/approvals/{theirs}").status_code == 404
    for path in ("/api/lanes/personal/reddit", "/api/lanes/biz-b/facebook", "/api/activity?lane=biz-b/facebook",
                 "/api/activity?workspace=biz-b"):
        assert va.get(path).status_code == 403, path
    status = va.get("/api/status").json()
    assert status["principal"]["owner"] is False and status["lanes"]["total"] == 1 and status["services"] == []
    assert status["needs_you_detail"]["approvals"] == 1
    # Activity: every principal's events in biz-a, none from elsewhere, members named by handle
    va.post(f"/api/approvals/{mine}/deny", {"note": "not today"})
    events = va.get("/api/activity").json()
    assert events and all(e.get("workspace") in ("biz-a", None) and e.get("lane", "biz-a/") .startswith("biz-a/")
                          for e in events if e.get("lane"))
    assert any(e.get("handle") == "va" and e["tool"] == "panel.approval.deny" for e in events)
    assert not any((e.get("lane") or "").startswith("biz-b/") for e in events)
    # the owner's Activity names the member too
    assert any(e.get("handle") == "va" for e in boss.get("/api/activity").json())
    # an auditor (evidence only, phase 3) sees nothing yet
    aud = join(node, boss, "aud", [role("auditor")])
    assert aud.get("/api/lanes").json() == [] and aud.get("/api/approvals").json() == []
    assert aud.get("/api/activity").json() == []


def test_events_are_filtered_by_workspace(node):
    boss = owner(node)
    va = join(node, boss, "va", [role("viewer")])
    api = node.api_app.state.api

    async def access_of(person):
        class Conn:
            def __init__(self):
                self.headers = {}
                self.cookies = {"agentos_session": person.client.cookies.get("agentos_session")}
        return (await api.authenticate(Conn())).access

    access = asyncio.run(access_of(va))
    assert access.filter_event("lane", {"id": "biz-a/instagram"}) == {"id": "biz-a/instagram"}
    assert access.filter_event("lane", {"id": "biz-b/facebook"}) is None
    assert access.filter_event("run", {"lane": "biz-a/instagram", "id": "r1"}) is not None
    assert access.filter_event("approval", {"id": "x", "summary": "secret"}) == {}
    assert access.filter_event("schedule", {"lane": "biz-a/instagram"}) is None  # schedules are an admin's
    assert access.filter_event("brain", {"name": "x"}) is None and access.filter_event("member", {}) is None


def test_the_live_view_follows_control(node):
    boss = owner(node)
    plain = join(node, boss, "plain", [role("approver")])
    watcher = join(node, boss, "watch", [role("viewer", watch=True)])
    for person, can in ((plain, False), (watcher, True)):
        r = person.get("/api/lanes/biz-a/instagram/screenshot")
        assert (r.status_code != 403) is can, (person.member["handle"], r.text)
        assert _ws_close(person, "/api/lanes/biz-a/instagram/vnc") == (WS_NOT_WARM if can else 4403)
        assert _ws_close(person, "/api/lanes/biz-b/facebook/vnc") == 4403
    # a terminal is exec: no member role carries it
    assert _ws_close(watcher, "/api/lanes/biz-a/instagram/terminal") in (4403, 4404)


def _ws_close(person: Person, path: str) -> int:
    with pytest.raises(WebSocketDisconnect) as refused, person.client.websocket_connect(WS + path) as ws:
        ws.receive_bytes()
    return refused.value.code


# ------------------------------------------------------------------ deciding, with step-up

def test_an_approver_decides_with_a_recent_passkey_check(node):
    boss = owner(node)
    va = join(node, boss, "va", [role("approver")])
    api = node.api_app.state.api
    ap = pending(node)
    r = va.post(f"/api/approvals/{ap}/approve", {"note": "ship it"})
    assert r.status_code == 200 and r.json()["state"] == "approved", r.text  # just signed in with the passkey
    stored = node.policy.approvals.get(ap)
    assert stored.decided_by == "va" and stored.decided_via == "passkey"
    other = pending(node, "biz-b/facebook")
    assert va.post(f"/api/approvals/{other}/approve").status_code == 404  # not theirs to see
    # the window passes: approving needs a fresh check, denying never does
    api.settings.step_up_window = 0
    ap2, ap3 = pending(node, content="second"), pending(node, content="third")
    r = va.post(f"/api/approvals/{ap2}/approve")
    assert r.status_code == 403 and r.json()["error"]["code"] == "step_up_required", r.text
    assert va.get("/api/me").json()["session"]["step_up_needed"] is True
    assert va.post(f"/api/approvals/{ap3}/deny").json()["state"] == "denied"
    api.settings.step_up_window = 900
    va.step_up()
    assert va.post(f"/api/approvals/{ap2}/approve").json()["state"] == "approved"
    # a step-up started in another session is refused
    opts = va.post("/api/session/verify/options").json()["publicKey"]
    with browser(node) as other_tab:
        other_tab.cookies.set("agentos_session", "nope")
        assert other_tab.post("/api/session/verify", json={"credential": va.auth.get(opts)}).status_code == 401
    # the owner decides as today, without a passkey check
    assert boss.post(f"/api/approvals/{pending(node, content='owner')}/approve").json()["state"] == "approved"


def test_an_approver_edits_a_caption_inline_and_a_viewer_only_sees_the_card(node):
    boss = owner(node)
    va = join(node, boss, "va", [role("approver")])
    viewer = join(node, boss, "eve", [role("viewer")])
    content = {"flow": "biz-a.post", "version": "v1", "lane": "biz-a/instagram", "params": {"caption": "Helo"}}
    ap = pending(node, content=content)
    card = viewer.get(f"/api/approvals/{ap}").json()["card"]
    assert card["type"] == "post" and card["text"]["value"] == "Helo" and card["editable"] == ["caption"]
    r = viewer.post(f"/api/approvals/{ap}/approve", {"edit": {"caption": "Hello"}})
    assert r.status_code == 403 and r.json()["error"]["code"] == "denied"  # editing is approving
    r = va.post(f"/api/approvals/{ap}/approve", {"edit": {"caption": "Hello"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "approved" and body["edit"] == {"params": ["caption"], "by": "va", "at": body["edit"]["at"]}
    assert body["card"]["text"]["value"] == "Hello" and "approved with an edit by va" in body["summary"]
    traced = node.policy.traces.query(kind="panel", limit=20)[0]
    assert any("with an edit by va (caption)" in (e.summary or "") for e in traced)
    # a card's image route is scope-checked like the approval itself
    other = pending(node, "biz-b/facebook", content={**content, "lane": "biz-b/facebook"})
    assert va.get(f"/api/approvals/{other}/files/image").status_code == 404
    assert va.get(f"/api/approvals/{ap}/files/caption").status_code == 404


def test_pay_needs_a_fresh_check_for_that_approval(node):
    boss = owner(node)
    va = join(node, boss, "va", [role("approver", max_usd=50)])
    small = pending(node, effect=Effect.PAY, facts={"amount_usd": 20.0}, content="small")
    other = pending(node, effect=Effect.PAY, facts={"amount_usd": 30.0}, content="other")
    r = va.post(f"/api/approvals/{small}/approve")
    assert r.status_code == 403 and r.json()["error"]["code"] == "proof_too_weak"  # fresh session or not
    assert r.json()["error"]["requires"] == "present"
    va.step_up(other)  # a check made for another approval does not count
    assert va.post(f"/api/approvals/{small}/approve").status_code == 403
    va.step_up(small)
    assert va.post(f"/api/approvals/{small}/approve").json()["state"] == "approved"
    # used once: the next pay approval needs its own
    assert va.post(f"/api/approvals/{other}/approve").status_code == 403
    big = pending(node, effect=Effect.PAY, facts={"amount_usd": 80.0}, content="big")
    va.step_up(big)
    r = va.post(f"/api/approvals/{big}/approve")
    assert r.status_code == 403 and "not_approver" in r.json()["error"]["message"]  # over their limit, on the node
    batch = va.post("/api/approvals/batch", {"ids": [other], "decision": "approve"}).json()
    assert batch["results"][0]["error"]["code"] == "proof_too_weak"


def test_a_viewer_never_decides_either_way_and_four_eyes_holds(node):
    boss = owner(node)
    viewer = join(node, boss, "vi", [role("viewer")])
    ap = pending(node)
    assert viewer.post(f"/api/approvals/{ap}/approve").status_code == 403
    r = viewer.post(f"/api/approvals/{ap}/deny")
    assert r.status_code == 403 and r.json()["error"]["code"] == "denied", r.text
    for decision in ("approve", "deny"):
        r = viewer.post("/api/approvals/batch", {"ids": [ap], "decision": decision})
        assert r.status_code == 403, r.text
    assert node.policy.approvals.get(ap).state == "pending"
    # an approver of another workspace may not say no to this one either
    elsewhere = join(node, boss, "el", [role("approver", "biz-b"), role("viewer", "biz-a")])
    r = elsewhere.post(f"/api/approvals/{ap}/deny")
    assert r.status_code == 403 and r.json()["error"]["code"] == "denied", r.text
    r = elsewhere.post("/api/approvals/batch", {"ids": [ap], "decision": "deny"})
    assert r.json()["results"][0]["ok"] is False and node.policy.approvals.get(ap).state == "pending"
    # an approver of this workspace says no, and it is decided
    approver_no = join(node, boss, "an", [role("approver")])
    assert approver_no.post(f"/api/approvals/{ap}/deny").json()["state"] == "denied"
    # a question: an approver answers it, a viewer cannot
    approver = join(node, boss, "ap", [role("approver")])
    from agentos.policy.tools import human_ask

    g, _ = node.policy.issue(subject={"id": "asker"}, workspaces=["biz-a"], ceiling="read")
    ctx = node.policy.context(g, tool="human_ask", lane_id="biz-a/instagram")
    q = asyncio.run(human_ask(ctx, question="Which photo?", options=["a", "b"]))["approval_id"]
    assert node.policy.approvals.get(q).workspace == "biz-a"
    assert viewer.post(f"/api/questions/{q}/answer", {"answer": "a"}).status_code == 403
    r = approver.post(f"/api/questions/{q}/answer", {"answer": "a"})
    assert r.status_code == 200 and r.json()["answered_by"] == "ap", r.text


def test_got_it_on_a_failed_outcome_needs_the_right_to_decide_in_its_workspace(node):
    # "Got it" takes the failed outcome away for everyone, so it is a decision's right, not a viewer's
    boss = owner(node)
    ap = pending(node)
    viewer = join(node, boss, "gv", [role("viewer")])
    r = viewer.post(f"/api/approvals/{ap}/clear")
    assert r.status_code == 403 and r.json()["error"]["code"] == "denied", r.text
    elsewhere = join(node, boss, "ge", [role("approver", "biz-b"), role("viewer", "biz-a")])
    assert elsewhere.post(f"/api/approvals/{ap}/clear").status_code == 403
    approver = join(node, boss, "ga", [role("approver")])
    r = approver.post(f"/api/approvals/{ap}/clear")  # it may decide here: the pending one has no outcome yet
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid", r.text
    assert node.policy.approvals.get(ap).state == "pending"


async def _asked_by_member(node: Node, member_id: str) -> str:
    """An approval a member's own action asked for (their id is who asked), waiting for a yes."""
    g = node.policy.issue_member_grant(member_id, "biz-a", "operator")
    ctx = node.policy.member_context(g, tool="lane_post", lane_id="biz-a/instagram")
    ctx.ask_person = "Vee started it"
    with pytest.raises(ApprovalPending) as e:
        await ctx.authorize(Effect.COMMIT, lane_id="biz-a/instagram", content="post it", summary="Do it")
    return e.value.approval_id


def test_the_app_names_who_asked_and_who_decided_and_never_shows_a_members_id(node):
    # /api/members is an admin's list, so the names come from the contract's own read of the member store
    boss = owner(node)
    vee = join(node, boss, "vee", [role("viewer")], display_name="Vee Marsh")
    ada = join(node, boss, "ada", [role("approver")], display_name="Ada Lind")
    ap = asyncio.run(_asked_by_member(node, vee.member["id"]))
    assert ada.post(f"/api/approvals/{ap}/approve", {}).status_code == 200
    for who in (boss, ada, vee):
        shown = who.get(f"/api/orgs/local/approvals/{ap}").json()
        assert shown["requested_by"]["name"] == "Vee Marsh", shown["requested_by"]
        assert shown["decided_by_name"] == "Ada Lind", shown["decided_by"]  # the node writes a member's yes under their handle
        assert shown["decided_by_id"] == ada.member["id"]  # so the app can tell its own yes from another's
    shown = boss.get(f"/api/orgs/local/approvals/{ap}").json()
    assert shown["requested_by"]["id"] == vee.member["id"]  # the id stays for Technical details, never as words
    assert "Vee Marsh" == shown["requested_by"]["name"]


MEMBER_WORDS = re.compile(r"`|\bagentos\b|\btokens?\b|\bgrants?\b|node\.toml", re.IGNORECASE)


def test_a_members_refusal_says_what_happened_without_a_command_token_or_grant(node):
    # The catalog's fix for `denied` names `agentos grants`; a member is never told that, and the owner still is
    boss = owner(node)
    ap = pending(node)
    viewer = join(node, boss, "wd", [role("viewer")])
    for method, path in (("POST", f"/api/approvals/{ap}/deny"), ("POST", f"/api/approvals/{ap}/clear"),
                         ("POST", "/api/unfreeze"), ("POST", "/api/freeze")):
        r = viewer.req(method, path, {"scope": "node", "reason": "x"} if path == "/api/freeze" else {})
        assert r.status_code == 403, (path, r.text)
        error = r.json()["error"]
        assert not MEMBER_WORDS.search(r.text), (path, r.text)
        assert "fix" not in error or not MEMBER_WORDS.search(error["fix"]), (path, error)
    r = viewer.req("POST", "/api/unfreeze", {"scope": "node"})
    assert r.json()["error"]["message"] == "Only the owner of this computer can unfreeze."
    assert node.policy.approvals.get(ap).state == "pending"


# ------------------------------------------------------------------ operators: routed like an agent

def test_an_operator_acts_in_their_workspace_and_connects_their_agent(node):
    boss = owner(node)
    op = join(node, boss, "op", [role("operator")])
    assert op.post("/api/lanes/biz-a/instagram/open").json()["state"] == "warm"
    assert op.post("/api/lanes/biz-b/facebook/open").status_code == 403
    viewer = join(node, boss, "vi", [role("viewer")])
    assert viewer.post("/api/lanes/biz-a/instagram/close").status_code == 403
    events = node.policy.traces.query(kind="panel", lane="biz-a/instagram")[0]
    assert any(e.tool == "panel.lane.open" and e.status == "ok" and e.principal == op.member["id"] for e in events)
    # their own agent: in their workspace, up to commit, owned by them
    r = op.post("/api/grants", {"subject": "op-claude", "workspaces": ["biz-a"], "ceiling": "commit"})
    assert r.status_code == 200 and r.json()["token"], r.text
    gid = r.json()["id"]
    assert node.policy.grant_record(gid)["subject"]["owner"] == op.member["id"]
    for bad in ({"workspaces": ["biz-b"], "ceiling": "commit"}, {"workspaces": ["biz-a"], "ceiling": "pay"},
                {"workspaces": ["biz-a"], "ceiling": "read", "specials": ["exec"]},
                {"workspaces": ["biz-a"], "lanes": ["*"]}, {"workspaces": ["biz-a"], "lanes": ["biz-b/x"]}):
        assert op.post("/api/grants", {"subject": "x", **bad}).status_code == 403, bad  # a lane elsewhere widens it
    assert [g["id"] for g in op.get("/api/grants").json()] == [gid]
    other = boss.post("/api/grants", {"subject": "boss-bot", "workspaces": ["biz-a"]}).json()["id"]
    assert op.post(f"/api/grants/{other}/revoke").status_code == 404
    # removing the operator revokes their agent too
    assert boss.req("DELETE", f"/api/members/{op.member['id']}").json()["agent_grants"] == [gid]
    assert node.policy.grant_record(gid)["state"] == "revoked"


# ------------------------------------------------------------------ changes at the next request

def test_removing_a_member_ends_their_sessions_at_once(node):
    boss = owner(node)
    va = join(node, boss, "va", [role("approver")])
    assert va.get("/api/lanes").status_code == 200
    r = boss.req("DELETE", f"/api/members/{va.member['id']}")
    assert r.status_code == 200 and r.json()["sessions_ended"] == 1 and r.json()["grants"]
    assert va.get("/api/lanes").status_code == 401
    assert node.policy.member_grants(va.member["id"]) == []
    # the passkey no longer signs in, and the handle is free for a new invite
    opts = va.client.post("/api/session/passkey/options", json={}).json()["publicKey"]
    assert va.client.post("/api/session/passkey", json={"credential": va.auth.get(opts)}).status_code == 401
    assert invite(boss, "va", [role("viewer")]).status_code == 200
    assert {m["handle"] for m in boss.get("/api/members").json()} == {"owner"}


def test_a_role_change_takes_effect_at_the_next_request(node):
    boss = owner(node)
    va = join(node, boss, "va", [role("approver")])
    old = node.policy.member_grants(va.member["id"])[0].id
    ap = pending(node)
    r = boss.req("PATCH", f"/api/members/{va.member['id']}", {"roles": [role("viewer")]})
    assert r.status_code == 200 and [x["role"] for x in r.json()["roles"]] == ["viewer"], r.text
    assert va.post(f"/api/approvals/{ap}/approve").status_code == 403
    assert node.policy.grant_record(old)["state"] == "revoked"
    assert va.get("/api/me").json()["roles"][0]["role"] == "viewer"
    # a new scope, then none at all: the member signs in but has nothing to see
    boss.req("PATCH", f"/api/members/{va.member['id']}", {"roles": [role("viewer", scope="biz-b")]})
    assert [lane["id"] for lane in va.get("/api/lanes").json()] == ["biz-a/instagram", "biz-b/facebook"]
    boss.req("PATCH", f"/api/members/{va.member['id']}", {"roles": [], "scopes": ["biz-a", "biz-b"]})
    assert va.get("/api/lanes").status_code == 401
    # an admin cannot change roles outside their workspace
    ada = join(node, boss, "ada", [role("admin")])
    bo = join(node, boss, "bo", [role("viewer")])
    r = ada.req("PATCH", f"/api/members/{bo.member['id']}", {"roles": [role("viewer", scope="biz-b")]})
    assert r.status_code == 403
    r = ada.req("PATCH", f"/api/members/{bo.member['id']}", {"roles": [role("approver")]})
    assert r.status_code == 200 and r.json()["roles"][0]["role"] == "approver"


def test_a_membership_end_is_the_grant_end(node):
    boss = owner(node)
    va = join(node, boss, "va", [role("approver")], ttl="90d")
    g = node.policy.member_grants(va.member["id"])[0]
    assert abs(g.expires_at - (time.time() + 90 * 86400)) < 120
    assert va.get("/api/me").json()["roles"][0]["expires_at"] is not None


# ------------------------------------------------------------------ the owner, devices and notifications

def test_the_owner_keeps_the_code_sign_in_and_may_add_a_passkey(node):
    boss = owner(node)
    opts = boss.post("/api/me/passkeys/options").json()["publicKey"]
    r = boss.post("/api/me/passkeys", {"credential": boss.auth.create(opts), "device_label": "laptop"})
    assert r.status_code == 200 and r.json()["label"] == "laptop", r.text
    with browser(node) as c:
        o = c.post("/api/session/passkey/options", json={}).json()["publicKey"]
        s = c.post("/api/session/passkey", json={"credential": boss.auth.get(o)})
        assert s.status_code == 200 and s.json()["member"]["owner"] is True
        status = c.get("/api/status").json()
        assert status["principal"] == {"kind": "human", "id": "owner", "owner": True}
        assert c.get("/api/grants").status_code == 200
    devices = boss.get("/api/me/devices").json()
    assert len(devices["passkeys"]) == 1 and any(s["current"] for s in devices["sessions"])


def test_devices_list_and_remove(node):
    boss = owner(node)
    va = join(node, boss, "va", [role("viewer")])
    devices = va.get("/api/me/devices").json()
    assert len(devices["passkeys"]) == 1 and len(devices["sessions"]) == 1 and devices["sessions"][0]["current"]
    # a second phone
    opts = va.post("/api/me/passkeys/options").json()["publicKey"]
    assert opts["excludeCredentials"]  # the browser will not make a second passkey on the same device
    second = SoftAuthenticator(ORIGIN)
    assert va.post("/api/me/passkeys", {"credential": second.create(opts)}).status_code == 200
    seen = boss.get(f"/api/members/{va.member['id']}").json()
    assert len(seen["passkeys"]) == 2
    # the owner removes the first passkey: the session made with it ends
    first = devices["passkeys"][0]["id"]
    r = boss.req("DELETE", f"/api/members/{va.member['id']}/devices/{first}")
    assert r.status_code == 200 and r.json()["sessions_ended"] == 1
    assert va.get("/api/me").status_code == 401


def test_my_notifications_are_stored(node):
    boss = owner(node)
    va = join(node, boss, "va", [role("approver")])
    got = va.get("/api/me/notify")
    assert got.status_code == 200, got.text
    r = va.req("PUT", "/api/me/notify", {"channel": "ntfy", "timezone": "Europe/Vilnius",
                                         "quiet_hours": ["21:00", "08:00"]})
    assert r.status_code == 200, r.text
    back = va.get("/api/me/notify").json()
    assert back["channel"] == "ntfy" and back["timezone"] == "Europe/Vilnius"
    assert va.req("PUT", "/api/me/notify", {"timezone": "Mars/Base"}).status_code == 400
    assert boss.get("/api/me/notify").status_code == 200


def test_passkey_sign_in_is_rate_limited(tmp_path):
    n = Node(tmp_path)
    try:
        c = browser(n)
        codes = [c.post("/api/session/passkey/options", json={}).status_code for _ in range(12)]
        assert codes[:10] == [200] * 10 and codes[10:] == [429, 429]
    finally:
        n.close()


def test_an_admin_keeps_their_workspace_vault_and_irreversible_waits_for_an_approval(node):
    boss = owner(node)
    ada = join(node, boss, "ada", [role("admin")])

    def entry(person, name, lanes):
        return person.post("/api/vault", {"name": name, "kind": "password", "domain": "instagram.com",
                                          "lanes": lanes, "username": "me", "secret": "a-long-test-secret-42"})

    mine = entry(ada, "ig", ["biz-a/instagram"])
    assert mine.status_code == 200 and "a-long-test-secret" not in mine.text, mine.text
    assert entry(ada, "fb", ["biz-b/facebook"]).status_code == 403
    assert entry(ada, "none", []).status_code == 403
    theirs = entry(boss, "fb", ["biz-b/facebook"]).json()["id"]
    assert [e["name"] for e in ada.get("/api/vault").json()] == ["ig"]
    assert ada.req("DELETE", f"/api/vault/{theirs}").status_code == 404
    # deleting is irreversible: even an admin who may approve it gets an approval, then a fresh passkey check
    r = ada.req("DELETE", f"/api/vault/{mine.json()['id']}")
    assert r.status_code == 202 and r.json()["error"]["code"] == "approval_pending", r.text
    ap = r.json()["error"]["approval_id"]
    assert ada.post(f"/api/approvals/{ap}/approve").json()["error"]["code"] == "proof_too_weak"
    ada.step_up(ap)
    assert ada.post(f"/api/approvals/{ap}/approve").json()["state"] == "approved"
    assert ada.req("DELETE", f"/api/vault/{mine.json()['id']}").status_code == 200
    assert ada.get("/api/vault").json() == []


def test_a_removed_members_event_stream_ends(node):
    boss = owner(node)
    va = join(node, boss, "va", [role("viewer")])
    api = node.api_app.state.api

    class Conn:
        def __init__(self):
            self.headers = {}
            self.cookies = {"agentos_session": va.client.cookies.get("agentos_session")}

    auth = asyncio.run(api.authenticate(Conn()))
    assert api.still_signed_in(auth)
    boss.req("DELETE", f"/api/members/{va.member['id']}")
    assert not api.still_signed_in(auth)


# ------------------------------------------------------------------ live streams: the heartbeat

WRITES = frozenset({"INSERT", "UPDATE", "DELETE", "REPLACE"})


def later(fn, delay: float) -> threading.Timer:
    timer = threading.Timer(delay, fn)
    timer.start()
    return timer


def _counting_sync(api, monkeypatch) -> list[int]:
    calls: list[int] = []
    real = api.members.sync

    def counted(*args, **kw):
        calls.append(1)
        return real(*args, **kw)

    monkeypatch.setattr(api.members, "sync", counted)
    return calls


def _socket_of(api, person: Person):
    """A live socket's view of its caller, as the panel opens it: the auth it was opened under."""

    class Conn:
        def __init__(self):
            self.headers = {}
            self.cookies = {"agentos_session": person.client.cookies.get("agentos_session")}

    conn = Conn()
    return conn, asyncio.run(api.authenticate(conn))


def test_an_unchanged_heartbeat_neither_syncs_nor_writes(node, monkeypatch):
    boss = owner(node)
    va = join(node, boss, "va", [role("viewer")])
    api = node.api_app.state.api
    live.of(api.runtime).interval = 0.05  # a heartbeat every 50 ms for the window below
    syncs = _counting_sync(api, monkeypatch)
    writes: list[str] = []
    recording = {"on": False}

    def trace(sql: str) -> None:
        if recording["on"] and sql.split(None, 1)[0].upper() in WRITES:
            writes.append(sql)

    for db in (api.store._db, node.policy.db.conn):
        db.set_trace_callback(trace)
    seen: dict[str, int] = {}

    def window_open():
        seen["base"] = len(syncs)
        recording["on"] = True

    def window_close():
        recording["on"] = False
        seen["syncs"] = len(syncs) - seen["base"]
        seen["writes"] = len(writes)
        boss.req("DELETE", f"/api/members/{va.member['id']}")  # ends the stream, after the measurement

    with va.client.stream("GET", "/api/events") as resp:
        assert resp.status_code == 200
        later(window_open, 0.1)
        later(window_close, 0.8)
        for _ in resp.iter_lines():
            pass
    for db in (api.store._db, node.policy.db.conn):
        db.set_trace_callback(None)
    assert seen["syncs"] == 0, "an unchanged heartbeat called members.sync"
    assert seen["writes"] == 0, writes


def test_a_heartbeat_syncs_once_on_a_change_and_closes_a_removed_member(node, monkeypatch):
    boss = owner(node)
    va = join(node, boss, "va", [role("viewer")])
    api = node.api_app.state.api
    syncs = _counting_sync(api, monkeypatch)
    conn, auth = _socket_of(api, va)  # the open itself syncs once
    syncs.clear()
    check = realtime._recheck(api, conn, "events", None, auth)
    assert asyncio.run(check()) is None and not syncs

    boss.req("PATCH", f"/api/members/{va.member['id']}", {"roles": [role("approver")]})
    syncs.clear()  # the PATCH itself syncs; the heartbeat's own sync is what is counted here
    assert asyncio.run(check()) is None and len(syncs) == 1  # the role changed: picked up once
    assert asyncio.run(check()) is None and len(syncs) == 1  # and then quiet again

    boss.req("DELETE", f"/api/members/{va.member['id']}")  # no bus event is read here: the check alone
    verdict = asyncio.run(check())
    assert verdict is not None and verdict[0] == live.CLOSE_REVOKED


def test_once_a_minute_the_heartbeat_syncs_even_when_nothing_changed(node, monkeypatch):
    boss = owner(node)
    va = join(node, boss, "va", [role("viewer")])
    api = node.api_app.state.api
    syncs = _counting_sync(api, monkeypatch)
    conn, auth = _socket_of(api, va)
    syncs.clear()
    monkeypatch.setattr(realtime, "HEARTBEAT_SYNC_S", 0)  # a minute has passed
    check = realtime._recheck(api, conn, "events", None, auth)
    assert asyncio.run(check()) is None and len(syncs) == 1  # the grant near its end is re-minted then


# ------------------------------------------------------------------ live streams: a freeze
# ------------------------------------------------------------------ live streams: a freeze

def _hold(api, freeze, scope: str, target: str) -> None:
    """Hold a scope as a freeze does, in memory, and tell the live sockets now: the gate and the streams see it
    at once. The sweep (revocations, cancelled runs) is not run here, so the database stays with the request thread."""
    freeze._records[scope_key(scope, target)] = FreezeRecord(scope=scope, target=target, reason="stop",
                                                              by="owner", at=time.time())
    live.of(api.runtime).poke()


def _freeze(node: Node, tmp_path):
    from agentos.freeze.service import FreezeService

    svc = FreezeService(node.runtime, home=tmp_path / "freeze")
    node.runtime.register("freeze", svc)
    return svc


def test_a_freeze_ends_a_members_event_stream_when_nothing_else_is_open_to_them(node, tmp_path):
    boss = owner(node)
    va = join(node, boss, "va", [role("viewer")])  # biz-a only
    api = node.api_app.state.api
    freeze = _freeze(node, tmp_path)
    holds = later(lambda: _hold(api, freeze, "workspace", "biz-a"), 0.3)
    backstop = later(lambda: boss.req("DELETE", f"/api/members/{va.member['id']}"), 3.0)  # a stream the freeze misses fails here
    try:
        with va.client.stream("GET", "/api/events") as resp:
            assert resp.status_code == 200
            t0 = time.monotonic()
            for _ in resp.iter_lines():
                pass
            ended_after = time.monotonic() - t0
    finally:
        holds.cancel()
        backstop.cancel()
    assert ended_after < 2.5, "the freeze did not end the member's event stream"


def test_a_freeze_keeps_a_members_stream_but_not_the_frozen_workspaces_events(node, tmp_path):
    boss = owner(node)
    va = join(node, boss, "va", [role("viewer", scope="biz-a"), role("viewer", scope="biz-b")])
    api = node.api_app.state.api
    freeze = _freeze(node, tmp_path)

    def freeze_and_publish():
        _hold(api, freeze, "workspace", "biz-a")
        api.publish("lane", {"id": "biz-a/instagram", "marker": "frozen-workspace"})
        api.publish("lane", {"id": "biz-b/facebook", "marker": "open-workspace"})

    holds = later(freeze_and_publish, 0.5)
    backstop = later(lambda: boss.req("DELETE", f"/api/members/{va.member['id']}"), 5.0)
    lines: list[str] = []
    try:
        with va.client.stream("GET", "/api/events") as resp:
            for line in resp.iter_lines():
                lines.append(line)
                if "open-workspace" in line:
                    break
    finally:
        holds.cancel()
        backstop.cancel()
    assert any("open-workspace" in line for line in lines), "the stream closed on the freeze"
    assert not any("frozen-workspace" in line for line in lines), "a frozen workspace's event was sent"


# ------------------------------------------------------------------ runs and checks, by workspace

class _Jobs:
    """The env manager's job reads, for runs in two workspaces (agentos.api.web.jobs reads only these)."""

    def __init__(self, infos):
        self.infos = {i.id: i for i in infos}

    def job_list(self, lane_id=None):
        return [i for i in self.infos.values() if lane_id is None or i.lane == lane_id]

    def job(self, job_id):
        from types import SimpleNamespace

        from agentos.core.errors import NotFound
        if job_id not in self.infos:
            raise NotFound(f"no job {job_id}")
        return SimpleNamespace(info=self.infos[job_id], kept=lambda stream: 0)

    async def job_read(self, job_id, *, stdout_offset=0, stderr_offset=0, wait_s=0, max_bytes=65536):
        return self.infos[job_id], b"", b""

    async def job_cancel(self, job_id):
        return self.infos[job_id]

    def has(self, lane_id):
        return False


def test_a_member_sees_only_their_workspaces_runs_and_checks(node, tmp_path):
    import io

    from PIL import Image

    from agentos.env.models import JobInfo
    from agentos.studio.contracts import Capture, CheckRun, Screen, Shot, Target
    from agentos.studio.runs import RunRecord, Runs, new_run_id
    from agentos.studio.store import iso_now, new_capture_id, store_for
    from agentos.studio.tools import store_home

    boss = owner(node)
    viewer = join(node, boss, "vi", [role("viewer")])
    operator = join(node, boss, "op", [role("operator")])
    mine = JobInfo(id="job-aaaaaaaaaa", lane="biz-a/term", cmd="pytest", timeout_s=60, state="exited", exit_code=0)
    theirs = JobInfo(id="job-bbbbbbbbbb", lane="biz-b/term", cmd="pytest", timeout_s=60)
    node.runtime.register("envs", _Jobs([mine, theirs]))
    jobs = "/api/orgs/local/jobs"
    assert [j["id"] for j in viewer.get(jobs).json()["jobs"]] == [mine.id]
    assert {j["id"] for j in boss.get(jobs).json()["jobs"]} == {mine.id, theirs.id}
    assert viewer.get(f"{jobs}/{mine.id}").status_code == 200
    assert viewer.get(f"{jobs}/{theirs.id}").status_code == 403
    assert viewer.post(f"{jobs}/{mine.id}/cancel").status_code == 403  # a viewer reads; stopping is operating
    assert operator.post(f"{jobs}/{theirs.id}/cancel").status_code == 403  # not in their workspace

    state_dir = tmp_path / "studio-state" / "studio"
    state_dir.mkdir(parents=True)

    class Studio:
        pass

    svc = Studio()
    svc.state_dir = state_dir
    node.runtime.register("studio", svc)
    store, runs = store_for(store_home(state_dir)), Runs(state_dir)
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), (255, 255, 255)).save(buf, format="PNG")
    screen = Screen(width=400)
    kept = {}
    for name, profile in (("a", "biz-a/shop"), ("b", "biz-b/shop"), ("none", None)):
        cap = store.save(Capture(id=new_capture_id(), target=Target(url=f"https://{name}.example/"),
                                 shots=[Shot(screen=screen, image=store.put_blob(buf.getvalue(), "image/png"))],
                                 created_at=iso_now()))
        rid = new_run_id()
        runs.save(RunRecord(id=rid, kind="system" if profile else "check", url=f"https://{name}.example/",
                            profile=profile, capture=cap.id, created_at=iso_now(),
                            run=CheckRun(id=rid, findings=[], coverage=[])))
        kept[name] = (rid, cap.id)
    studio = "/api/orgs/local/studio"
    assert [r["id"] for r in viewer.get(f"{studio}/runs").json()["runs"]] == [kept["a"][0]]
    assert len(boss.get(f"{studio}/runs").json()["runs"]) == 3
    assert viewer.get(f"{studio}/runs/{kept['a'][0]}").status_code == 200
    for name in ("b", "none"):  # another workspace's check, and one no workspace owns: the owner's
        rid, cid = kept[name]
        assert viewer.get(f"{studio}/runs/{rid}").status_code == 403, name
        assert viewer.get(f"{studio}/captures/{cid}/screens/{screen.key}").status_code == 403, name
        assert viewer.get(f"{studio}/diff", params={"before": kept["a"][1], "after": cid}).status_code == 403, name
    assert viewer.get(f"{studio}/captures/{kept['a'][1]}/screens/{screen.key}").status_code == 200
    assert viewer.get(f"{studio}/profiles/biz-b/shop/components").status_code == 403
    assert viewer.get(f"{studio}/profiles/biz-a/shop/components").json()["run"] == kept["a"][0]
    assert viewer.get(f"{studio}/baseline", params={"url": "https://none.example/"}).status_code == 403
    assert viewer.get(f"{studio}/baseline", params={"url": "https://b.example/", "profile": "biz-b/shop"}).status_code == 403
    assert boss.get("/api/orgs/local/vms").status_code == 200 and viewer.get("/api/orgs/local/vms").status_code == 403
    assert viewer.get("/api/orgs/local/brand/proposals").status_code == 403
