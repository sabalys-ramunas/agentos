"""One "is a person present" decision (openajar.core.presence, docs/THREAT-MODEL.md).

Every credential an agent can hold (a grant, an admin grant, the node token, the owner's device
credential that `openajar connect` keeps in the Mac's login Keychain) stops below Presence.PERSON, so
none of them approves, answers, makes a standing approval, turns Ask me first or a confirmation
setting off, widens a lane's sites, enrolls a passkey, invites a member or unfreezes. A person signed
in to the panel, and the node's own CLI (a one-time code it writes into panel.db), still do.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient
from test_api_members import owner, pending
from test_node_panel import TOKEN, Node, _ask, bearer

from openajar.api.access import Access, PolicyAccess
from openajar.api.auth import PRESENCE_HEADER, SESSION_COOKIE
from openajar.api.errors import ApiError
from openajar.core import globs
from openajar.core.presence import Presence
from openajar.core.ratelimit import RateLimiter
from openajar.core.tools import gate_kind, in_person
from openajar.policy.confirm import ConfirmLevel, arrival
from openajar.policy.confirmation import ConfirmationConfig

LANE = "biz-a/instagram"
EM_DASH = "—"


@pytest.fixture
def node(tmp_path):
    n = Node(tmp_path)
    n.api_app.state.api.limiter = RateLimiter(per_key=10_000, global_max=10_000)
    yield n
    n.close()


class Device:
    """The owner's device credential, as `openajar dash` and `openajar approvals` send it: the session
    cookie and the CSRF header, no Origin."""

    def __init__(self, node: Node):
        with node.client() as c:
            r = c.post("/api/session/device", json={"label": "macbook"}, headers=bearer(TOKEN))
        assert r.status_code == 200, r.text
        self.client = TestClient(node.app, base_url="http://localhost:7400")
        self.client.cookies.set(SESSION_COOKIE, r.json()["session"])
        self.h = {"X-Agentos-CSRF": r.json()["csrf"]}

    def req(self, method: str, url: str, json: dict | None = None):
        return self.client.request(method, url, json=json if json is not None else {}, headers=self.h)


class Token:
    def __init__(self, node: Node, token: str):
        self.client = TestClient(node.app, base_url="http://localhost:7400")
        self.h = bearer(token)

    def req(self, method: str, url: str, json: dict | None = None):
        return self.client.request(method, url, json=json if json is not None else {}, headers=self.h)


def refused(r) -> None:
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "presence_required", r.text
    assert EM_DASH not in r.text


def person_routes(node: Node, approval: str, question: str) -> list[tuple[str, str, dict]]:
    """What only a person may do, with a body that would succeed for the signed-in owner."""
    return [
        ("POST", f"/api/approvals/{approval}/approve", {}),
        ("POST", "/api/approvals/batch", {"ids": [approval], "decision": "approve"}),
        ("POST", f"/api/questions/{question}/answer", {"answer": "yes"}),
        ("POST", "/api/approvals/standing", {"lane": LANE, "ttl_seconds": 3600, "from_approvals": [approval]}),
        ("POST", "/api/ask-first", {"scope": "lane", "id": LANE, "ask_first": False}),
        ("GET", "/api/confirmation", {}),
        ("PUT", "/api/confirmation", {"publish_in_chat": True}),
        ("GET", "/api/confirmation/methods", {}),
        ("POST", "/api/unfreeze", {"scope": "lane", "target": LANE}),
        ("POST", f"/api/lanes/{LANE}/sites", {"op": "add", "sites": ["evil.example"]}),
        ("POST", "/api/me/passkeys/options", {}),
        ("POST", "/api/invites", {"handle": "mallory", "roles": [{"role": "admin", "scope": "*"}]}),
        ("PUT", f"/api/lanes/{LANE}/meter", {}),
        ("PUT", f"/api/lanes/{LANE}/pace", {"pace": None}),
        ("POST", "/api/codes/x1", {"code": "123456"}),
        # an assisted login hands a held lane back once signed in (the control route: test_viewer_person)
        ("POST", f"/api/lanes/{LANE}/login", {}),
        ("POST", f"/api/lanes/{LANE}/login/cancel", {"done": True}),
    ]


def assert_nothing_changed(node: Node, approval: str) -> None:
    assert node.policy.approvals.get(approval).state == "pending"
    assert node.lanes.get(LANE).spec.sites == []
    assert node.policy.ask_first.is_on("lane", LANE)
    assert node.api_app.state.api.members.store.invites() == []
    assert node.runtime.get("confirmation").current().publish_in_chat is False


def _setup(node: Node) -> tuple[str, str]:
    node.policy.ask_first.set("lane", LANE, True, by="owner", person=True)
    question = asyncio.run(_ask(node, TOKEN, "Shall I post it?"))
    return pending(node, LANE), question


def test_the_device_credential_never_approves_or_turns_a_protection_off(node):
    """Bypass 1: the node token mints a device session, and the device session was an owner session
    in person. An agent with the node token, or with a shell on the Mac (the login Keychain), held it."""
    approval, question = _setup(node)
    device = Device(node)
    for method, url, body in person_routes(node, approval, question):
        refused(device.req(method, url, body))
    assert_nothing_changed(node, approval)
    # what the owner's computer does keep: reading and denying
    assert device.req("GET", "/api/approvals").status_code == 200
    r = device.req("POST", f"/api/approvals/{approval}/deny", {"note": "no"})
    assert r.status_code == 200 and r.json()["state"] == "denied", r.text


@pytest.mark.parametrize("who", ["node_token", "admin_grant"])
def test_no_bearer_token_is_a_person(node, who):
    """Bypass 4 and the admin grant: the node token read as a person on the viewer routes."""
    approval, question = _setup(node)
    token = TOKEN if who == "node_token" else node.grant(subject={"id": "boss-agent"}, admin=True)
    caller = Token(node, token)
    for method, url, body in person_routes(node, approval, question):
        r = caller.req(method, url, body)
        assert r.status_code == 403 and EM_DASH not in r.text, (url, r.text)
        assert r.json()["error"]["code"] in ("presence_required", "denied"), (url, r.text)
    assert_nothing_changed(node, approval)


def test_the_node_token_has_its_own_principal_kind(node):
    with node.client() as c:
        me = c.get("/api/status", headers=bearer(TOKEN)).json()["principal"]
        assert me == {"kind": "node", "id": "owner", "owner": True}
        h = node.sign_in(c)
        assert c.get("/api/status", headers=h).json()["principal"]["kind"] == "human"


def test_the_owner_signed_in_still_does_all_of_it(node):
    approval, question = _setup(node)
    boss = owner(node)
    assert boss.post(f"/api/approvals/{approval}/approve").json()["state"] == "approved"
    assert boss.post(f"/api/questions/{question}/answer", {"answer": "yes"}).status_code == 200
    assert boss.post("/api/ask-first", {"scope": "lane", "id": LANE, "ask_first": False}).status_code == 200
    assert boss.get("/api/confirmation").status_code == 200
    r = boss.post(f"/api/lanes/{LANE}/sites", {"op": "add", "sites": ["instagram.com"]})
    assert r.status_code == 200 and node.lanes.get(LANE).spec.sites == ["instagram.com"], r.text
    r = boss.post("/api/approvals/standing", {"lane": LANE, "ttl_seconds": 3600, "from_approvals": [approval]})
    assert r.status_code == 200, r.text


def test_the_nodes_cli_is_a_person_with_a_one_time_code_from_panel_db(node):
    """`openajar lane sites` on the node: the node token plus a code it wrote into panel.db. The code
    works once, and only beside the node token."""
    url = f"/api/lanes/{LANE}/sites"
    with node.client() as c:
        node.store.add_code("cli-code-1", ttl_seconds=60)
        r = c.post(url, json={"op": "add", "sites": ["instagram.com"]},
                   headers={**bearer(TOKEN), PRESENCE_HEADER: "cli-code-1"})
        assert r.status_code == 200, r.text
        again = c.post(url, json={"op": "add", "sites": ["evil.example"]},
                       headers={**bearer(TOKEN), PRESENCE_HEADER: "cli-code-1"})
        refused(again)
        node.store.add_code("cli-code-2", ttl_seconds=60)
        agent = node.grant(subject={"id": "boss-agent"}, admin=True)
        r = c.post(url, json={"op": "add", "sites": ["evil.example"]},
                   headers={**bearer(agent), PRESENCE_HEADER: "cli-code-2"})
        assert r.status_code == 403
    assert node.lanes.get(LANE).spec.sites == ["instagram.com"]


# ---------------------------------------------------------------- standing approvals (bypass 2)

def test_a_standing_approval_is_made_only_from_approved_approvals_on_its_own_lane(node):
    """Bypass 2: a pending approval turned into a standing approval and the retry went out."""
    node.policy.ask_first.set("lane", LANE, True, by="owner", person=True)
    node.policy.ask_first.set("lane", "biz-b/facebook", True, by="owner", person=True)
    approval = pending(node, LANE)
    other = pending(node, "biz-b/facebook")
    boss = owner(node)
    body = {"lane": LANE, "ttl_seconds": 3600, "from_approvals": [approval]}
    r = boss.post("/api/approvals/standing", body)
    assert r.status_code == 400 and "approve it first" in r.json()["error"]["message"], r.text
    assert boss.post(f"/api/approvals/{other}/approve").status_code == 200
    r = boss.post("/api/approvals/standing", {**body, "from_approvals": [other]})
    assert r.status_code == 400 and "not biz-a/instagram" in r.json()["error"]["message"], r.text
    assert node.policy.approvals.list_standing() == []


def test_a_member_never_makes_a_standing_approval_from_what_four_eyes_keeps_from_them(node):
    """A member standing in for their own agent's approval: decision_problem holds for each one."""
    from openajar.policy.approval_models import ApprovalPolicy, FourEyesRule
    from openajar.policy.approvals import ApprovalStore
    from openajar.policy.errors import Denied

    store: ApprovalStore = node.policy.approvals
    node.policy.ask_first.set("lane", LANE, True, by="owner", person=True)
    approval = pending(node, LANE, owner_id="m_alice")
    store.decide(approval, "approve", by="owner")
    member_grant = node.policy.issue_member_grant("m_alice", "biz-a", "approver")
    before = store.policy
    store.policy = ApprovalPolicy(four_eyes=[FourEyesRule(workspace="biz-*", effects=["commit"])])
    with pytest.raises(Denied) as e:
        store.binding_from([approval], lane=LANE, deciders=[member_grant])
    assert e.value.reason == "four_eyes"
    store.policy = before
    assert store.binding_from([approval], lane=LANE, deciders=[member_grant])[2] == [LANE]


def test_the_standing_route_test_api_routes_used_now_refuses_a_token(node):
    refused(Token(node, TOKEN).req("POST", "/api/approvals/standing", {"lane": LANE, "ttl_seconds": 3600}))


# ---------------------------------------------------------------- chat after untrusted text (bypass 3)

def test_chat_does_not_carry_a_publish_after_untrusted_text_by_default():
    """An agent with a shell can open its own MCP session and answer its own prompt, so the publish
    exception is the owner's explicit choice, off unless they turn it on."""
    cfg = ConfirmationConfig()
    assert cfg.publish_in_chat is False and cfg.publish_takes_chat() is False
    assert cfg.requires("commit", untrusted=True, publish=True) > ConfirmLevel.CHAT


def test_the_device_credential_and_tokens_prove_no_person():
    assert arrival(via="device").level is None
    assert arrival(via="owner_token").level is None
    assert arrival(via="cli").level == ConfirmLevel.SIGNED_IN


def test_presence_is_ordered():
    assert Presence.AGENT < Presence.NODE_TOKEN < Presence.DEVICE < Presence.PERSON < Presence.VERIFIED


def in_person_ctx(**attrs) -> SimpleNamespace:
    """A tool context as core.tools reads it: the owner's grant is a human principal, not in person by flag."""
    return SimpleNamespace(principal=SimpleNamespace(kind="human"), in_person=False, **attrs)


def test_the_node_token_context_is_gated_as_an_agent_not_as_the_owner_in_person():
    """The node token on /mcp carries the owner's principal (kind human). Its context says how sure the
    node is, so below PERSON it is not in person: a lane takeover, a pause or a freeze holds it."""
    node_token = in_person_ctx(presence=Presence.NODE_TOKEN)
    assert in_person(node_token) is False and gate_kind(node_token) == "member"
    for level in (Presence.AGENT, Presence.DEVICE):
        assert in_person(in_person_ctx(presence=level)) is False
    signed_in = in_person_ctx(presence=Presence.PERSON)
    assert in_person(signed_in) is True and gate_kind(signed_in) == "human"


def test_a_context_with_no_presence_keeps_the_old_answer():
    """Internal owner contexts (no MCP call, no panel request) carry no presence: a human principal on
    this node is in person, as before."""
    assert in_person(in_person_ctx()) is True and gate_kind(in_person_ctx()) == "human"


def test_access_defaults_to_an_agent_so_an_unvouched_request_is_never_a_person():
    access = Access()
    assert access.presence == Presence.AGENT and access.human is False
    with pytest.raises(ApiError) as e:
        access.require_presence(Presence.PERSON, "x")
    assert e.value.code == "presence_required"


def test_a_device_credential_context_is_never_in_person():
    """PolicyAccess sets the context's level from the request. Below PERSON (the device credential) the
    context is not in person, so the gates treat its calls as an agent's."""
    ctx = SimpleNamespace(in_person=True, presence=None)
    access = PolicyAccess(ctx, presence=Presence.DEVICE)
    assert ctx.in_person is False and ctx.presence == Presence.DEVICE
    assert access.presence == Presence.DEVICE and access.human is False
    assert in_person(ctx) is False


# ---------------------------------------------------------------- low: lane globs and the Secure cookie

def test_a_workspace_glob_covers_one_level_of_lane_only():
    assert globs.lane_match("ws/*", "ws/a")
    assert not globs.lane_match("ws/*", "ws/a/b")
    assert not globs.lane_match("*/*", "ws/a/b")


def _request(client: str, proto: str | None, scheme: str = "http") -> Request:
    headers = [(b"host", b"node.example")] + ([(b"x-forwarded-proto", proto.encode())] if proto else [])
    return Request({"type": "http", "scheme": scheme, "path": "/", "headers": headers, "client": (client, 1234),
                    "server": ("node.example", 80), "query_string": b""})


def test_x_forwarded_proto_counts_only_from_a_local_proxy(node):
    api = node.api_app.state.api
    assert api.secure_cookie(_request("127.0.0.1", "https")) is True
    assert api.secure_cookie(_request("203.0.113.9", "https")) is False
    assert api.secure_cookie(_request("203.0.113.9", None, scheme="https")) is True
    assert api.secure_cookie(_request("127.0.0.1", None)) is False


def test_the_owner_computer_learns_how_to_approve():
    """`openajar approvals approve` with the device credential: the node's refusal becomes a hint
    that names the panel through the tunnel."""
    from openajar.client.errors import ClientError
    from openajar.client.ops import Client
    from openajar.client.owner import PanelError

    class Conn:
        name, ssh_host, local_port, credential, token_store = "mynode", "mynode", 7400, "agent_grant", "file"

    c = Client.__new__(Client)
    c.owner_device = lambda conn: "device-session"  # type: ignore[method-assign]

    def refuse(device, *a, **kw):
        raise PanelError(403, "presence_required", "This needs you in person.")

    with pytest.raises(ClientError) as e:
        c._owner_call(Conn(), refuse)
    assert "http://localhost:7400/needs-you" in (e.value.hint or "") and "deny still works" in e.value.message
    assert EM_DASH not in e.value.message + (e.value.hint or "")



def test_the_bare_node_token_api_gives_an_owner_link_and_sites_only_to_the_nodes_cli(tmp_path):
    """No panel: the node token alone is the caller. Its view link is not an owner link, and it does not
    change a site list; with a one-time code from this node's panel.db (the CLI) it does, once."""
    from fakes import FakeLaneManager
    from starlette.applications import Starlette
    from starlette.routing import Mount

    from openajar.api.auth import PanelStore
    from openajar.core.config import NodeConfig
    from openajar.core.models import LaneSpec
    from openajar.node.viewer_routes import control_api_routes
    from openajar.node.viewer_tokens import ViewTokens

    config = NodeConfig(home=tmp_path / "home", mcp_token=TOKEN)
    lanes = FakeLaneManager(config)
    asyncio.run(lanes.create(LaneSpec(id=LANE)))
    app = Starlette(routes=[Mount("/api", routes=control_api_routes(lanes, ViewTokens.for_config(config)))])
    panel = PanelStore.for_home(config.home)
    with TestClient(app) as c:
        link = c.post(f"/api/lanes/{LANE}/view-link", json={"mode": "control"})
        assert link.status_code == 200 and link.json()["owner"] is False, link.text
        refused(c.post(f"/api/lanes/{LANE}/sites", json={"op": "add", "sites": ["evil.example"]}))
        panel.add_code("cli-1", ttl_seconds=60)
        link = c.post(f"/api/lanes/{LANE}/view-link", json={"mode": "control"}, headers={PRESENCE_HEADER: "cli-1"})
        assert link.json()["owner"] is True
        panel.add_code("cli-2", ttl_seconds=60)
        r = c.post(f"/api/lanes/{LANE}/sites", json={"op": "add", "sites": ["instagram.com"]},
                   headers={PRESENCE_HEADER: "cli-2"})
        assert r.status_code == 200, r.text
        refused(c.post(f"/api/lanes/{LANE}/sites", json={"op": "add", "sites": ["evil.example"]},
                       headers={PRESENCE_HEADER: "cli-2"}))
    assert lanes.get(LANE).spec.sites == ["instagram.com"]


def test_the_device_credential_is_routed_as_the_node_token_never_as_the_owner_in_person(node):
    """Reviewed: the device session used to get the owner's in-person context, which skips pacing,
    taint and the trifecta."""
    device = Device(node)
    sid = device.client.cookies.get(SESSION_COOKIE)
    session = node.store.get_session(sid)
    api = node.api_app.state.api
    assert api.session_presence(session) == Presence.DEVICE
    access = api._token_access(node.policy, TOKEN, Presence.DEVICE)
    assert access.human is False and getattr(access.ctx, "in_person", False) is False
    r = device.req("GET", "/api/status")
    assert r.json()["principal"]["kind"] == "human"  # the owner's computer, read as the owner, not in person


def test_rotating_the_node_token_ends_the_device_credentials_it_made(node):
    device = Device(node)
    assert device.req("GET", "/api/status").status_code == 200
    assert node.store.delete_device_sessions() == 1
    assert device.req("GET", "/api/status").status_code == 401


def test_the_dash_on_the_node_decides_as_a_person(node):
    """`openajar dash` on the node, with the node token from node.toml: each write carries a fresh code."""
    import httpx

    from openajar.tui.client import ApiClient

    approval, _ = _setup(node)
    seen: list[dict] = []
    app_client = node.client()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        r = app_client.request(request.method, request.url.path, content=request.content,
                               headers={k: v for k, v in request.headers.items() if k != "host"})
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": "application/json"})

    async def go() -> None:
        c = ApiClient("http://localhost:7400", TOKEN, transport=httpx.MockTransport(handler), home=node.config.home)
        try:
            await c.approve(approval)
        finally:
            await c.aclose()

    asyncio.run(go())
    assert PRESENCE_HEADER in seen[-1]
    assert node.policy.approvals.get(approval).state == "approved"


def test_an_agent_grant_trying_to_decide_learns_a_person_decides_in_the_panel(node):
    """QA-2 #9: an agent's own grant on approve, batch approve, answer or a standing approval got
    `denied` "use the owner token or an admin grant", which points an agent at a stronger credential.
    It gets `presence_required`: a person decides it in the panel."""
    approval, question = _setup(node)
    agent = Token(node, node.grant(subject={"id": "claude"}, ceiling="commit"))
    for method, url, body in person_routes(node, approval, question)[:4]:
        r = agent.req(method, url, body)
        refused(r)
        assert "owner token" not in r.text and "admin grant" not in r.text, (url, r.text)
        assert "panel" in r.json()["error"]["fix"], (url, r.text)
    assert_nothing_changed(node, approval)
    # a grant still gets the role answer where no person is needed: denying in a batch
    r = agent.req("POST", "/api/approvals/batch", {"ids": [approval], "decision": "deny"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "denied", r.text
