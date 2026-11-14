"""A flood at the public routes: refused at once, with a clear status, and nothing piles up."""

from __future__ import annotations

import asyncio
import base64
import json
import time

import httpx
import pytest
from channel_support import start_controller, wait_for
from test_workflow_access import full_node
from workflow_support import OWNER, action, make_node, read_flow, workflow

from agentos.controller.hooks import HookGate
from agentos.node.channel.workflows import NodeHookGate, relay_hook
from agentos.runner.schedules import sign_webhook

OWNER_P = {"kind": "human", "id": "owner", "owner": "owner"}
TOKEN = "a" * 32


# ---- the controller's gate ----

def test_a_source_is_limited_on_its_own():
    gate = HookGate(per_source=2)
    assert gate.source("1.1.1.1") is None and gate.source("1.1.1.1") is None
    assert gate.source("1.1.1.1") > 0
    assert gate.source("2.2.2.2") is None  # one flood does not refuse everyone


def test_a_token_is_limited_however_many_sources_call_it():
    gate = HookGate(per_source=100, per_token=3, per_node=100)
    for i in range(3):
        assert gate.source(f"10.0.0.{i}") is None and gate.target("node", TOKEN) is None
    assert gate.source("10.0.0.9") is None and gate.target("node", TOKEN) > 0
    assert gate.target("node", "b" * 32) is None  # the sender next door is not affected


def test_a_node_is_limited_across_its_tokens():
    gate = HookGate(per_token=100, per_node=2)
    assert gate.target("node", "a" * 32) is None and gate.target("node", "b" * 32) is None
    assert gate.target("node", "c" * 32) > 0 and gate.target("other", "c" * 32) is None


def test_places_in_flight_are_refused_not_queued_and_come_back():
    gate = HookGate(in_flight=2)
    assert gate.enter("node") and gate.enter("node") and not gate.enter("node")
    assert gate.enter("other")
    gate.leave("node")
    assert gate.enter("node")


def test_a_spray_of_addresses_cannot_grow_memory_without_bound():
    gate = HookGate(everyone=50)
    for i in range(500):
        gate.source(f"10.{i // 250}.{i % 250}.1")
    assert sum(1 for q in gate.sources._hits.values() if q) == 50


# ---- over real HTTP ----

class Live:
    def __init__(self, ctl, node, wid, token, secret):
        self.ctl, self.node, self.wid, self.token, self.secret = ctl, node, wid, token, secret
        self.web = ctl.controller.web
        self.node_id = node.config.node_id
        self.url = f"/hooks/{self.node_id}/{token}"

    def signed(self, body: bytes) -> dict:
        ts = str(int(time.time()))
        return {"X-Agentos-Timestamp": ts, "X-Agentos-Signature": sign_webhook(self.secret, ts, body)}


@pytest.fixture
async def live(tmp_path):
    ctl = await start_controller(tmp_path / "controller")
    n = await full_node(tmp_path / "node", "node-a", ctl, ["biz-a/social"])
    await wait_for(lambda: ctl.controller.hub.sessions)
    wf = n.runtime.get("workflows")
    wid = wf.save(workflow({"id": "n", "kind": "notify", "message": "ran"}, trigger={"kind": "webhook"}), OWNER)["id"]
    secret = wf.enable(wid, OWNER)["secret"]
    yield Live(ctl, n, wid, wf.public(wid)["trigger"]["hook"]["token"], secret)
    await n.close()
    await ctl.close()


def client(live) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=live.ctl.url, timeout=60)  # some requests here are held open on purpose


async def test_a_flood_from_one_source_is_refused_with_a_clear_status(live):
    live.web.hooks = HookGate(per_source=3)
    async with client(live) as web:
        got = [await web.post(live.url, content=f"{i}".encode(), headers=live.signed(f"{i}".encode())) for i in range(8)]
    assert [r.status_code for r in got] == [202, 202, 202, 429, 429, 429, 429, 429]
    refused = got[-1]
    assert refused.json()["error"]["code"] == "rate_limited" and int(refused.headers["Retry-After"]) >= 1


async def test_made_up_tokens_count_against_the_source_and_are_never_relayed(live, monkeypatch):
    live.web.hooks = HookGate(per_source=3)
    relayed = []
    monkeypatch.setattr(live.ctl.controller.hub, "relay_hook", lambda *a: relayed.append(a))
    async with client(live) as web:
        got = [await web.post(f"/hooks/{live.node_id}/short{i}", content=b"x") for i in range(5)]
    assert [r.status_code for r in got] == [404, 404, 404, 429, 429] and relayed == []


async def test_a_refused_request_is_not_read(live):
    live.web.hooks = HookGate(per_source=1)

    async def body():
        yield b"x" * 1024

    async with client(live) as web:
        await web.post(live.url, content=b"{}", headers=live.signed(b"{}"))
        assert (await web.post(live.url, content=body())).status_code == 429


async def test_a_flood_cannot_queue_work_behind_a_slow_node(live, monkeypatch):
    live.web.hooks = HookGate(per_source=1000, per_token=1000, in_flight=2)
    gate, running, most = asyncio.Event(), 0, 0

    async def slow(node_id, token, body, headers):
        nonlocal running, most
        running += 1
        most = max(most, running)
        await gate.wait()
        running -= 1
        return 202, {}

    monkeypatch.setattr(live.ctl.controller.hub, "relay_hook", slow)
    async with client(live) as web:
        held = [asyncio.create_task(web.post(live.url, content=b"{}")) for _ in range(2)]
        await wait_for(lambda: running == 2)
        flood = await asyncio.gather(*[web.post(live.url, content=b"{}") for _ in range(20)])
        assert {r.status_code for r in flood} == {429} and most == 2  # at once, while the node is still busy
        gate.set()
        assert [(await t).status_code for t in held] == [202, 202]
        assert (await web.post(live.url, content=b"{}")).status_code == 202  # the places came back
    assert live.web.hooks._busy == {}


async def test_a_body_over_the_cap_is_refused_before_or_as_it_passes(live):
    big = b"x" * (512 * 1024 + 1)

    async def chunked():
        for _ in range(9):
            yield b"x" * (64 * 1024)

    async with client(live) as web:
        assert (await web.post(live.url, content=big)).status_code == 413
        assert (await web.post(live.url, content=chunked())).status_code == 413
    assert live.web.hooks._busy == {}


async def test_a_sender_that_dribbles_its_body_gives_its_place_back(live):
    live.web.hook_body_timeout = 0.1

    async def dribble():
        yield b"{"
        await asyncio.sleep(1)
        yield b"}"

    async with client(live) as web:
        assert (await web.post(live.url, content=dribble())).status_code == 408
    assert live.web.hooks._busy == {}


async def test_an_offline_node_says_when_to_come_back(live):
    async with client(live) as web:
        r = await web.post(f"/hooks/no-such-node/{TOKEN}", content=b"{}")
    assert r.status_code == 503 and int(r.headers["Retry-After"]) >= 1


async def test_a_node_that_does_not_answer_gives_the_place_back(live, monkeypatch):
    hub = live.ctl.controller.hub
    monkeypatch.setattr("agentos.controller.hub.HOOK_TIMEOUT", 0.05)
    live.node.channel._handlers = lambda real=live.node.channel._handlers: {
        **real(), "hook_relay": lambda conn, message: asyncio.sleep(0)}
    async with client(live) as web:
        r = await web.post(live.url, content=b"{}", headers=live.signed(b"{}"))
    assert r.status_code == 504 and live.web.hooks._busy == {} and not hub.requests


async def test_unsubscribe_posts_are_limited_too(live):
    live.web.hooks = HookGate(per_source=2)
    link = f"/u/{live.node_id}/{TOKEN}"
    async with client(live) as web:
        got = [(await web.post(link)).status_code for _ in range(4)]
        assert (await web.get(link)).status_code == 200  # the page itself does nothing
    assert got == [200, 200, 429, 429]


async def test_the_nodes_retry_after_reaches_the_sender(live):
    live.node.channel._hook_gate = NodeHookGate(per_token=1)
    async with client(live) as web:
        assert (await web.post(live.url, content=b"1", headers=live.signed(b"1"))).status_code == 202
        r = await web.post(live.url, content=b"2", headers=live.signed(b"2"))
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1 and "retry_after" not in r.json()


# ---- the node's own gate: it does not rest on the controller ----

@pytest.fixture
def node(tmp_path):
    n = make_node(tmp_path)
    n.h.save(read_flow(), by=OWNER_P)
    return n


def hooked(node):
    wid = node.wf.save(workflow(action("look", "biz-a.social.read"), trigger={"kind": "webhook"}), OWNER)["id"]
    secret = node.wf.enable(wid, OWNER)["secret"]
    return wid, node.wf.public(wid)["trigger"]["hook"]["token"], secret


def relayed(node, token, secret, body: bytes, *, good=True):
    ts = str(int(node.h.clock.now().timestamp()))
    return {"token": token, "body_b64": base64.b64encode(body).decode(),
            "headers": {"x-agentos-timestamp": ts,
                        "x-agentos-signature": sign_webhook(secret if good else "wrong", ts, body)}}


async def test_a_node_refuses_past_its_relay_limit_before_it_decodes_anything(node):
    _, token, secret = hooked(node)
    gate = NodeHookGate(relays=2)
    scheduler = node.runner.scheduler
    got = [await relay_hook(scheduler, relayed(node, token, secret, f"{i}".encode()), gate) for i in range(3)]
    assert [r["status"] for r in got] == [202, 202, 429] and got[2]["retry_after"] > 0
    assert (await relay_hook(scheduler, {"nonsense": True}, gate))["status"] == 429


async def test_a_stranger_cannot_use_up_a_senders_allowance(node):
    _, token, secret = hooked(node)
    gate = NodeHookGate(per_token=2)
    scheduler = node.runner.scheduler
    for i in range(20):
        assert (await relay_hook(scheduler, relayed(node, token, secret, f"{i}".encode(), good=False), gate))["status"] == 401
    got = [await relay_hook(scheduler, relayed(node, token, secret, f"s{i}".encode()), gate) for i in range(3)]
    assert [r["status"] for r in got] == [202, 202, 429]


async def test_a_signed_flood_cannot_pile_up_unbounded_work_on_a_node(node):
    wid, token, secret = hooked(node)
    gate = NodeHookGate(backlog=5, per_token=1000, waiting=node.wf.hook_backlog)
    scheduler = node.runner.scheduler
    got = [(await relay_hook(scheduler, relayed(node, token, secret, json.dumps({"i": i}).encode()), gate))["status"]
           for i in range(30)]
    assert got == [202] * 5 + [429] * 25  # nothing has run yet: five wait, and the rest are told to come back
    assert len(node.events.list_events(source="webhook", limit=100)) == 5
    for _ in range(4):
        await node.settle()
        node.h.clock.advance(60)
    assert node.wf.hook_backlog(node.runner.store.get_schedule(node.wf.store.state(wid).schedule_id)) == 0
    assert (await relay_hook(scheduler, relayed(node, token, secret, b"after"), gate))["status"] == 202


async def test_a_run_in_a_long_wait_is_not_a_backlog(node):
    wid = node.wf.save(workflow({"id": "w", "kind": "wait", "amount": 1, "unit": "days"}, trigger={"kind": "webhook"}), OWNER)["id"]
    secret = node.wf.enable(wid, OWNER)["secret"]
    token = node.wf.public(wid)["trigger"]["hook"]["token"]
    gate = NodeHookGate(backlog=2, waiting=node.wf.hook_backlog)
    for i in range(6):
        assert (await relay_hook(node.runner.scheduler, relayed(node, token, secret, f"{i}".encode()), gate))["status"] == 202
        await node.settle(2)
    assert len(node.wf.runs(wid)) == 6


