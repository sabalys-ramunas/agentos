"""The controller end: enrollment, challenge auth, replay, outbox acks, untrusted input,
per-org isolation, the owner's sign-in, and routing across nodes."""

import asyncio
import contextlib
import json

import httpx2
import pytest
from channel_support import (
    AGENT_TOKEN,
    OWNER_TOKEN,
    agent,
    join_node,
    owner,
    start_controller,
    start_node,
    wait_for,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from websockets.asyncio.client import connect

from agentos.controller import routing
from agentos.controller.routing import NodeView, RouteError, ToolDef
from agentos.core import signing
from agentos.core.config import NodeConfig
from agentos.core.models import Effect
from agentos.node.channel import enroll as enrolling
from agentos.node.channel import wire
from agentos.node.channel.identity import NodeKey


@pytest.fixture
async def ctl(tmp_path):
    running = await start_controller(tmp_path / "controller")
    yield running
    await running.close()


async def recv(ws, timeout=5.0) -> wire.Envelope:
    async with asyncio.timeout(timeout):
        while True:
            frame = await ws.recv()
            if isinstance(frame, str):
                return wire.decode(frame)


@contextlib.asynccontextmanager
async def dial(ctl):
    async with connect(ctl.node_url, max_size=wire.HARD_FRAME_BYTES) as ws:
        challenge = await recv(ws)
        assert challenge.type == "challenge" and isinstance(challenge.body["nonce"], str)
        yield ws, challenge.body["nonce"]


async def enroll(ctl, token, name="raw-node", key=None, **extra):
    """An enroll with the node key's proof over this socket's challenge, unless `extra` sets `proof`."""
    key = key or Ed25519PrivateKey.generate()
    body = {"join_token": token, "node_pubkey": signing.public_b64(key), "name": name, "version": "0.1.0",
            "os": "linux", "arch": "x86_64", "capabilities": ["browser", "teleport"], "max_warm_lanes": 3, **extra}
    async with dial(ctl) as (ws, nonce):
        if "proof" not in extra:
            body["proof"] = signing.b64e(key.sign(wire.enroll_message(nonce, body["node_pubkey"])))
        await ws.send(wire.Envelope("enroll", body).encode())
        return await recv(ws), key


@contextlib.asynccontextmanager
async def session(ctl, node_id, key):
    async with dial(ctl) as (ws, nonce):
        sig = signing.b64e(key.sign(wire.hello_message(nonce, node_id)))
        await ws.send(wire.Envelope("hello", {"node_id": node_id, "nonce": nonce, "sig": sig}).encode())
        welcome = await recv(ws)
        assert welcome.type == "welcome", welcome.body
        yield ws, welcome


async def enrolled(ctl, name="raw-node"):
    _, token = ctl.controller.create_join_token(ctl.org)
    reply, key = await enroll(ctl, token, name)
    assert reply.type == "enrolled", reply.body
    return reply.body, key


async def test_enrollment_records_the_node_and_consumes_the_token(ctl):
    _, token = ctl.controller.create_join_token(ctl.org, capabilities=["browser", "terminal"])
    reply, key = await enroll(ctl, token, "box-1")
    assert reply.type == "enrolled"
    body = reply.body
    assert body["org_id"] == ctl.org and body["node_id"].startswith("node_")
    assert body["controller_pubkey"] == ctl.controller.keys.controller_pubkey
    assert len(signing.b64d(body["grant_pubkey"])) == 32 and isinstance(body["config"], dict)
    node = ctl.controller.store.node(body["node_id"])
    assert node["pubkey"] == signing.public_b64(key) and node["capabilities"] == ["browser"]  # limited by the token
    again, _ = await enroll(ctl, token, "box-2")
    assert again.type == "error" and again.body["code"] == "already_used"


@pytest.mark.parametrize(("change", "code"), [
    ({"join_token": "ajt_not_a_token"}, "denied"),
    ({"node_pubkey": "short"}, "invalid"),
    ({"name": "Not A Name!"}, "invalid"),
])
async def test_enrollment_refusals(ctl, change, code):
    _, token = ctl.controller.create_join_token(ctl.org)
    reply, _ = await enroll(ctl, change.pop("join_token", token), **change)
    assert reply.type == "error" and reply.body["code"] == code
    if code != "denied":  # a refused enrollment leaves the token usable
        assert (await enroll(ctl, token, "ok-node"))[0].type == "enrolled"


async def enroll_with_proof(ctl, token, *, key, signer=None, nonce_for=None, pubkey=None):
    """An enroll whose proof is signed by `signer` (the node key by default) over the challenge of
    this socket (or `nonce_for`), naming `pubkey` (the key's own by default)."""
    pubkey = pubkey or signing.public_b64(key)
    async with dial(ctl) as (ws, nonce):
        proof = signing.b64e((signer or key).sign(wire.enroll_message(nonce_for or nonce, pubkey)))
        body = {"join_token": token, "node_pubkey": pubkey, "name": "proved", "proof": proof}
        await ws.send(wire.Envelope("enroll", body).encode())
        return await recv(ws)


async def test_an_enroll_proof_must_match_the_key_and_this_sockets_challenge(ctl):
    _, token = ctl.controller.create_join_token(ctl.org)
    key = Ed25519PrivateKey.generate()
    for bad in ({"signer": Ed25519PrivateKey.generate()},       # someone else's key
                {"nonce_for": "another-sockets-nonce-0123"},    # another challenge
                {"pubkey": signing.public_b64(Ed25519PrivateKey.generate())}):  # a key the sender does not hold
        reply = await enroll_with_proof(ctl, token, key=key, **bad)
        assert reply.type == "error" and reply.body["code"] == "invalid", bad
    garbage, _ = await enroll(ctl, token, proof="not-a-signature")
    assert garbage.body["code"] == "invalid"
    assert ctl.controller.store.join_tokens(ctl.org)[0]["used_at"] is None  # never spent
    good = await enroll_with_proof(ctl, token, key=key)
    assert good.type == "enrolled", good.body
    assert ctl.controller.store.node(good.body["node_id"])["pubkey"] == signing.public_b64(key)


async def test_an_enroll_without_proof_is_refused_and_the_token_kept(ctl):
    _, token = ctl.controller.create_join_token(ctl.org)
    for missing in ({"proof": None}, {"proof": ""}):
        reply, _ = await enroll(ctl, token, "older-node", **missing)
        assert reply.type == "error" and reply.body["code"] == "invalid", missing
    assert ctl.controller.store.join_tokens(ctl.org)[0]["used_at"] is None  # never spent
    assert (await enroll(ctl, token, "older-node"))[0].type == "enrolled"


async def test_expired_tokens_and_name_clashes(ctl):
    _row, token = ctl.controller.store.create_join_token(ctl.org, ttl=-1)
    assert (await enroll(ctl, token))[0].body["code"] == "expired"
    await enrolled(ctl, "twin")
    _, token2 = ctl.controller.create_join_token(ctl.org)
    clash, _ = await enroll(ctl, token2, "twin")
    assert clash.body["code"] == "conflict"


async def test_hello_needs_this_sockets_nonce_and_the_enrolled_key(ctl):
    body, key = await enrolled(ctl)
    node_id = body["node_id"]
    async with session(ctl, node_id, key) as (_, welcome):
        assert welcome.body["node_id"] == node_id and "revoked_grants" in welcome.body
    async with dial(ctl) as (ws, nonce):  # signed by another key
        sig = signing.b64e(Ed25519PrivateKey.generate().sign(wire.hello_message(nonce, node_id)))
        await ws.send(wire.Envelope("hello", {"node_id": node_id, "nonce": nonce, "sig": sig}).encode())
        assert (await recv(ws)).body["code"] == "denied"
    captured = {}
    async with dial(ctl) as (ws, nonce):  # a recorded hello, replayed on a new socket
        sig = signing.b64e(key.sign(wire.hello_message(nonce, node_id)))
        captured = wire.Envelope("hello", {"node_id": node_id, "nonce": nonce, "sig": sig}).encode()
        await ws.send(captured)
        assert (await recv(ws)).type == "welcome"
    async with dial(ctl) as (ws, _):
        await ws.send(captured)
        assert (await recv(ws)).body["code"] == "denied"
    assert not ctl.controller.store.use_nonce(json.loads(captured)["body"]["nonce"], node_id)  # single use
    async with dial(ctl) as (ws, nonce):
        await ws.send(wire.Envelope("hello", {"node_id": "node_unknown", "nonce": nonce, "sig": "x"}).encode())
        assert (await recv(ws)).body["code"] == "denied"


async def test_before_authentication_other_messages_get_errors(ctl):
    async with dial(ctl) as (ws, _):
        await ws.send(wire.Envelope("heartbeat", {}).encode())
        assert (await recv(ws)).body["code"] == "invalid"
        await ws.send("{broken")
        assert (await recv(ws)).body["code"] == "invalid"


async def test_outbox_messages_are_acked_cumulatively_and_deduped_on_seq(ctl):
    body, key = await enrolled(ctl)
    store = ctl.controller.store
    async with session(ctl, body["node_id"], key) as (ws, _):
        trace = {"id": "trc_1", "tool": "browser_snapshot", "status": "ok", "summary": "<b>hi</b>",
                 "secret_field": "never stored", "cost": {"tokens": "lots"}}
        for _ in range(2):  # a redelivery keeps its seq
            await ws.send(wire.Envelope("traces", {"events": [trace]}, seq=5).encode())
            ack = await recv(ws)
            assert ack.type == "ack" and ack.body == {"upto_seq": 5}
        stored = store.traces(ctl.org)
        assert len(stored) == 1 and "secret_field" not in stored[0] and stored[0]["cost"] == {"tokens": 0, "usd": 0.0}
        await ws.send(wire.Envelope("event", {"kind": "lane", "payload": {"id": "personal/x", "state": "warm",
                                                                         "exit_ip": "203.0.113.9"}}, seq=3).encode())
        assert (await recv(ws)).body == {"upto_seq": 5}  # older than what was applied: acked, ignored
        await ws.send(wire.Envelope("event", {"kind": "lane", "payload": {"id": "personal/x", "state": "warm",
                                                                         "exit_ip": "203.0.113.9"}}, seq=6).encode())
        assert (await recv(ws)).body == {"upto_seq": 6}
        assert store.lanes(ctl.org)[0]["exit_ip"] is None
        await ws.send(wire.Envelope("approval_request", {"approval": {"id": "bad id!"}}, seq=7).encode())
        assert (await recv(ws)).body == {"upto_seq": 7}  # dropped, still acknowledged
        assert store.approvals(ctl.org) == []
        assert store.last_seq(body["node_id"]) == 7


async def test_unknown_types_and_oversized_frames_never_drop_the_connection(ctl):
    body, key = await enrolled(ctl)
    async with session(ctl, body["node_id"], key) as (ws, _):
        sent = wire.Envelope("teleport", {})
        await ws.send(sent.encode())
        reply = await recv(ws)
        assert reply.body["code"] == "unsupported" and reply.reply_to == sent.id
        await ws.send(json.dumps({"v": 1, "type": "heartbeat", "id": "m_big",
                                  "body": {"pad": "x" * (wire.MAX_JSON_BYTES + 1)}}))
        assert (await recv(ws)).body["code"] == "invalid"
        await ws.send(wire.Envelope("heartbeat", {"lanes": "nope", "load": [1], "degraded": "yes"}).encode())
        await ws.send(wire.Envelope("heartbeat", {"lanes": [{"id": "a/b", "state": "warm"}, {"id": "../x"}],
                                                  "load": {"cpu_pct": 3}}).encode())
        await wait_for(lambda: ctl.controller.hub.sessions[body["node_id"]].lane_ids == {"a/b"})


async def test_revocations_are_signed_pushes_and_arrive_in_welcome(ctl):
    body, key = await enrolled(ctl)
    grant, _ = ctl.controller.grants.create(ctl.org, subject="helper")
    child, _ = ctl.controller.grants.create(ctl.org, subject="child", parent_id=grant.id)
    async with session(ctl, body["node_id"], key) as (ws, _):
        async with owner(ctl) as api:
            assert (await api.delete(f"/api/grants/{grant.id}")).status_code == 200
        push = await recv(ws)
        assert push.type == "revoke"
        ids = set(push.body["revocation_ids"])
        assert {grant.id, grant.rid, child.id, child.rid} <= ids  # the cascade
        assert signing.verify(ctl.controller.keys.controller_pubkey, push.body["sig"],
                           signing.controller_message("revoke", body["node_id"], push.body))
    async with session(ctl, body["node_id"], key) as (_, welcome):
        assert {grant.rid, child.rid} <= set(welcome.body["revoked_grants"])


async def test_each_org_sees_only_its_own_nodes_tools_and_approvals(tmp_path, ctl):
    other = ctl.controller.store.create_org("other", "Other Org")
    mine = await start_node(tmp_path / "mine", "mine", ctl, ["personal/reddit"])
    theirs = await start_node(tmp_path / "theirs", "theirs", ctl, ["secret/vault"], org_id=other["id"])
    try:
        await wait_for(lambda: all(s.tools for s in ctl.controller.hub.sessions.values())
                       and len(ctl.controller.hub.sessions) == 2)
        ctl.controller.store.put_approval(other["id"], theirs.config.node_id, {"id": "ap_theirs0000", "state": "pending",
                                                                              "summary": "theirs"})
        async with owner(ctl) as api:
            names = [n["name"] for n in (await api.get("/api/nodes")).json()]
            assert names == ["mine"]
            assert (await api.get("/api/approvals")).json() == []
            refused = await api.post(f"/api/approvals/{theirs.config.node_id}/ap_theirs0000",
                                     json={"decision": "approved"})
            assert refused.status_code == 404
            assert (await api.delete(f"/api/nodes/{theirs.config.node_id}")).status_code == 404
        async with agent(ctl) as client:
            nodes = (await client.call_tool("nodes_list", {})).structured_content["nodes"]
            assert [n["name"] for n in nodes] == ["mine"]
            snapshot = next(t for t in (await client.list_tools()).tools if t.name == "browser_snapshot")
            assert snapshot.input_schema["properties"]["node"]["enum"] == ["mine"]
            stolen = await client.call_tool("browser_snapshot", {"on": "secret/vault"})
            assert stolen.is_error
            assert not theirs.lanes.calls
            explicit = await client.call_tool("browser_snapshot", {"on": "secret/vault", "node": "theirs"})
            assert explicit.is_error and "not online" in explicit.content[0].text
        grant, token = ctl.controller.grants.create(other["id"], subject="their-agent")
        async with agent(ctl, token) as client:
            nodes = (await client.call_tool("nodes_list", {})).structured_content["nodes"]
            assert [n["name"] for n in nodes] == ["theirs"]
            snap = await client.call_tool("browser_snapshot", {"on": "secret/vault"})
            assert not snap.is_error, snap.content[0].text
        # a grant signed by the other org's key does not verify on this org's node (token v2 names
        # its issuer, which this node does not trust)
        foreign = ctl.controller.grants.mint(grant, deadline=10**10)
        with pytest.raises(Exception) as refused_grant:
            mine.runtime.policy.resolve(foreign)
        assert getattr(refused_grant.value, "reason", "") == "unknown_issuer"
        # this org's grant, minted for another node of the org, is refused by its audience
        own, _ = ctl.controller.grants.create(ctl.org, subject="helper")
        elsewhere = ctl.controller.grants.mint(own, deadline=10**10, node_id="node_elsewhere")
        with pytest.raises(Exception) as wrong_node:
            mine.runtime.policy.resolve(elsewhere)
        assert getattr(wrong_node.value, "reason", "") == "audience"
        here = ctl.controller.grants.mint(own, deadline=10**10, node_id=mine.config.node_id)
        principal, held = mine.runtime.policy.resolve(here)
        assert principal.id == "helper" and held.version == 2 and held.blocks[0].iss == f"org:{ctl.org}"
    finally:
        await mine.close()
        await theirs.close()


async def test_owner_sign_in_with_a_one_time_code_and_csrf(ctl):
    url, _ = ctl.controller.login_link()
    code = url.split("#code=")[1]
    async with httpx2.AsyncClient(base_url=ctl.url) as web:
        assert (await web.get("/api/nodes")).status_code == 401
        assert (await web.post("/api/session", json={"code": "wrong"})).status_code == 401
        signed_in = await web.post("/api/session", json={"code": code})
        assert signed_in.status_code == 200
        csrf = signed_in.json()["csrf"]
        cookie = signed_in.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        assert (await web.post("/api/session", json={"code": code})).status_code == 401  # one time
        assert (await web.get("/api/nodes")).status_code == 200
        assert (await web.post("/api/join-tokens", json={})).status_code == 403  # no CSRF header
        made = await web.post("/api/join-tokens", json={"name": "box-9"}, headers={"X-Agentos-CSRF": csrf})
        assert made.status_code == 201 and made.json()["join_command"].startswith("agentos node join --self-hosted ws://")
        cross = await web.post("/api/join-tokens", json={},
                               headers={"X-Agentos-CSRF": csrf, "Origin": "https://evil.example"})
        assert cross.status_code == 403
        page = await web.get("/")
        assert page.status_code == 200 and "script-src 'self'" in page.headers["content-security-policy"]
        assert "—" not in page.text
    async with httpx2.AsyncClient(base_url=ctl.url, headers={"Authorization": f"Bearer {OWNER_TOKEN}x"}) as bad:
        assert (await bad.get("/api/status")).status_code == 401
    async with httpx2.AsyncClient(base_url=ctl.url) as spray:
        codes = [(await spray.post("/api/session", json={"code": f"guess-{i}"})).status_code for i in range(12)]
        assert codes[-1] == 429


async def test_the_host_header_is_checked(ctl):
    async with httpx2.AsyncClient() as http:
        response = await http.get(f"http://127.0.0.1:{ctl.port}/api/status", headers={"Host": "evil.example"})
        assert response.status_code == 421


# ---- routing (pure) ----

def tool(name, lane_arg="on", effect=Effect.READ):
    props = {lane_arg: {"type": "string"}} if lane_arg else {}
    return ToolDef(name, f"{name} does a thing", {"type": "object", "properties": props}, effect, lane_arg)


def nodes():
    tools = {t.name: t for t in (tool("browser_snapshot"), tool("browsers_list", None), tool("approval_status", None))}
    return [NodeView("node_a", "a", tools, {"personal/reddit", "shared/blog"}),
            NodeView("node_b", "b", dict(tools), {"work/x", "shared/blog"})]


def test_plain_lane_ids_route_to_the_only_node_that_has_them():
    target, args = routing.route("browser_snapshot", {"on": "work/x"}, nodes())
    assert target.name == "b" and args == {"on": "work/x"}


def test_a_shared_lane_id_needs_the_qualified_form_or_node():
    with pytest.raises(RouteError) as raised:
        routing.route("browser_snapshot", {"on": "shared/blog"}, nodes())
    assert "a/shared/blog" in raised.value.message and "b/shared/blog" in raised.value.message
    target, args = routing.route("browser_snapshot", {"on": "a/shared/blog"}, nodes())
    assert target.name == "a" and args == {"on": "shared/blog"}
    target, args = routing.route("browser_snapshot", {"on": "shared/blog", "node": "b"}, nodes())
    assert target.name == "b" and "node" not in args
    with pytest.raises(RouteError):
        routing.route("browser_snapshot", {"on": "a/shared/blog", "node": "b"}, nodes())


def test_tools_without_a_lane_route_by_node_or_approval():
    with pytest.raises(RouteError):
        routing.route("browsers_list", {}, nodes())
    assert routing.route("browsers_list", {"node": "a"}, nodes())[0].name == "a"
    target, _ = routing.route("approval_status", {"approval_id": "ap_1"}, nodes(),
                              approval_node=lambda aid: "node_b" if aid == "ap_1" else None)
    assert target.name == "b"
    with pytest.raises(RouteError):
        routing.route("browsers_list", {"node": "ghost"}, nodes())
    with pytest.raises(RouteError):
        routing.route("unknown_tool", {}, nodes())
    single = [nodes()[0]]
    assert routing.route("browsers_list", {}, single)[0].name == "a"


def test_node_tool_definitions_are_untrusted():
    parsed = routing.parse_tools([
        {"name": "good_tool", "input_schema": {"type": "object", "properties": {"on": {}}}, "lane_arg": "on"},
        {"name": "Bad Name", "input_schema": {"type": "object"}},
        {"name": "nodes_list", "input_schema": {"type": "object"}},
        {"name": "sneaky", "input_schema": {"type": "object", "properties": {"node": {}}}},
        {"name": "wrong_effect", "input_schema": {"type": "object"}, "effect": "superuser"},
        {"name": "lane_lie", "input_schema": {"type": "object", "properties": {}}, "lane_arg": "missing"},
        "not a dict",
    ])
    assert set(parsed) == {"good_tool", "lane_lie"} and parsed["lane_lie"].lane_arg is None
    assert routing.parse_tools("nope") == {}
    schema = routing.public_schema(parsed["good_tool"], ["a", "b"])
    assert schema["properties"]["node"]["enum"] == ["a", "b"]
    assert "node" not in parsed["good_tool"].input_schema["properties"]  # the node's copy is untouched


class RecordingController:
    """Records the node's `enroll` and refuses it; sends a challenge first unless told not to."""

    def __init__(self, challenge: bool = True):
        self.challenge = challenge
        self.nonce = "recorded-nonce-0123456789"
        self.enrolls: list[dict] = []

    async def __aenter__(self):
        from websockets.asyncio.server import serve

        self.server = await serve(self._handle, "127.0.0.1", 0)
        self.url = f"ws://localhost:{self.server.sockets[0].getsockname()[1]}/v1/node/connect"
        return self

    async def __aexit__(self, *exc):
        self.server.close()

    async def _handle(self, ws):
        if self.challenge:
            await ws.send(wire.Envelope("challenge", {"nonce": self.nonce}).encode())
        message = wire.decode(await ws.recv())
        self.enrolls.append(message.body)
        await ws.send(wire.error_envelope("denied", "recorded only", message.id).encode())


async def test_the_node_proves_its_key_on_the_sockets_challenge(tmp_path, monkeypatch):
    config = NodeConfig(home=tmp_path, name="proving")
    async with RecordingController() as fake:
        with pytest.raises(enrolling.EnrollmentRefused):
            await enrolling.enroll(config, fake.url, "ajt_x")
    body = fake.enrolls[0]
    public = NodeKey.load_or_create(config.secrets_dir).public
    assert body["node_pubkey"] == public
    assert signing.verify(public, body["proof"], f"agentos-node-enroll:v1:{fake.nonce}:{public}".encode())
    monkeypatch.setattr(enrolling, "CHALLENGE_WAIT", 0.2)
    async with RecordingController(challenge=False) as quiet:  # no challenge: an enroll without proof
        with pytest.raises(enrolling.EnrollmentRefused):
            await enrolling.enroll(config, quiet.url, "ajt_x")
    assert "proof" not in quiet.enrolls[0] and quiet.enrolls[0]["join_token"] == "ajt_x"


async def test_a_real_node_enrolls_with_proof_end_to_end(ctl, tmp_path):
    config = await join_node(tmp_path / "proved-node", "proved-node", ctl)
    assert config.enrolled
    assert ctl.controller.store.node(config.node_id)["pubkey"] == NodeKey.load_or_create(config.secrets_dir).public


@contextlib.asynccontextmanager
async def agent_at(ctl, query: str):
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {AGENT_TOKEN}"}, timeout=60) as http,
        Client(streamable_http_client(f"{ctl.url}/mcp{query}", http_client=http), mode="legacy") as client,
    ):
        yield client


async def test_the_combined_list_starts_with_core_and_loads_the_rest(tmp_path, ctl):
    node = await start_node(tmp_path / "mine", "mine", ctl, ["personal/reddit"])
    try:
        await wait_for(lambda: all(s.tools for s in ctl.controller.hub.sessions.values())
                       and len(ctl.controller.hub.sessions) == 1)
        everything = set(ctl.controller.hub.sessions[next(iter(ctl.controller.hub.sessions))].tools)
        assert "browser_files" in everything and "grant_info" in everything
        async with agent(ctl) as client:
            listed = {t.name: t for t in (await client.list_tools()).tools}
            assert {"nodes_list", "tools_more", "tools_call", "browser_snapshot", "browser_open", "browser_file_put"} <= set(listed)
            assert "browser_files" not in listed and "grant_info" not in listed
            assert len(listed) < len(everything)
            # the controller lists lean schemas, as a node does: no pydantic titles
            assert '"title":' not in json.dumps(listed["browser_snapshot"].input_schema)
            assert listed["browser_snapshot"].input_schema["properties"]["node"]["enum"] == ["mine"]
            overview = (await client.call_tool("tools_more", {})).structured_content
            assert {"browser", "accounts", "admin"} <= {row["toolset"] for row in overview["toolsets"]}
            loaded = (await client.call_tool("tools_more", {"toolset": "files"})).structured_content  # the old name
            assert loaded["toolset"] == "browser" and "in your list now" in loaded["next"]
            assert any(t["name"] == "browser_files" for t in loaded["tools"])
            assert "browser_files" in {t.name for t in (await client.list_tools()).tools}
            again = (await client.call_tool("tools_more", {"toolset": "browser_files"})).structured_content
            assert again["next"] == "These tools are already in your list."
            bad = await client.call_tool("tools_more", {"toolset": "nope"})
            assert bad.is_error and "no toolset or tool of yours" in bad.content[0].text
            # tools_call routes like a direct call, also for the controller's own tool
            nodes = await client.call_tool("tools_call", {"name": "nodes_list"})
            assert [n["name"] for n in nodes.structured_content["nodes"]] == ["mine"]
            mine = await client.call_tool("tools_call", {"name": "grant_info", "arguments": {}})
            assert not mine.is_error, mine.content[0].text
            meta = await client.call_tool("tools_call", {"name": "tools_more"})
            assert meta.is_error and "directly" in meta.content[0].text
            missing = await client.call_tool("tools_call", {"name": "no_such_tool"})
            assert missing.is_error and "not_found" in missing.content[0].text
        async with agent_at(ctl, "?toolsets=full") as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert names == everything | {"nodes_list"}
        async with agent_at(ctl, "?toolsets=admin") as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert "grant_info" in names and "browser_files" not in names and "tools_more" in names
    finally:
        await node.close()
