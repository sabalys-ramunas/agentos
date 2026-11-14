"""The code card end to end on the real node services (policy, vault, event bus, notifier, panel
API): an agent's login reaches the code step, the owner types the code into the panel, the node
types it into the site. Afterwards the code is in no SQLite file, trace, outbox row, approval,
live event, log line or API response."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3

import httpx
import pytest
from fakes import FakeInteraction, FakeLaneManager
from test_vault_support import FakeTypist, Page, fld, login_form, make_vault, private_home

from agentos.api import ApiSettings, build_api
from agentos.api.auth import PanelStore
from agentos.core.config import NodeConfig
from agentos.core.models import LaneSpec
from agentos.node.daemon import build_services, node_runtime
from agentos.vault.login import login_flow

TOKEN = "node-token-for-the-code-card-tests"
SERVICES = ["agentos.api.events", "agentos.policy.service", "agentos.vault.service"]
LANE = "biz-a/instagram"
IG = "https://www.instagram.com"
CODE = "Z7Q4K9"           # letters too, so no timestamp or counter can match it by chance
TYPED = "z7q4 k9".upper()  # what the owner types: with a space, normalized by the node


class Node:
    def __init__(self, tmp_path):
        self.home = private_home(tmp_path)
        make_vault(self.home)
        self.config = NodeConfig(home=self.home, mcp_token=TOKEN, mcp_port=7400)
        self.lanes = FakeLaneManager(self.config)
        asyncio.run(self.lanes.create(LaneSpec(id=LANE)))
        self.runtime = node_runtime(self.config, self.lanes, FakeInteraction())
        build_services(self.runtime, SERVICES)
        self.store = PanelStore.for_home(self.home)
        self.app = build_api(self.runtime, ApiSettings(lane_pump_seconds=0, sse_max_seconds=0.3, sse_heartbeat=0.1,
                                                       panel_dir=tmp_path / "none"))
        self.policy = self.runtime.policy
        self.vault = self.runtime.vault

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://localhost:7400")

    async def sign_in(self, client: httpx.AsyncClient) -> dict:
        self.store.add_code("one-time-code-for-tests")
        r = await client.post("/api/session", json={"code": "one-time-code-for-tests"})
        assert r.status_code == 200, r.text
        return {"X-Agentos-CSRF": r.json()["csrf"], "Origin": "http://localhost:7400"}


@pytest.fixture
def node(tmp_path):
    n = Node(tmp_path)
    yield n
    n.policy.close()


def site() -> FakeTypist:
    return FakeTypist({
        "login": Page(IG + "/accounts/login/", login_form(IG), on_enter="code"),
        "code": Page(IG + "/accounts/login/code", [fld("otp", IG, autocomplete="one-time-code", form="o")],
                     on_enter="home"),
        "home": Page(IG + "/", []),
    }, "login")


async def add_relay_entry(node: Node) -> None:
    await node.vault.add_entry(name="ig", kind="password", domain="instagram.com", lanes=[LANE],
                               username="me@example.org", secret="Tr0ub4dor&3-correct-horse",
                               config={"catcher": {"kind": "relay"}})


async def agent_login(node: Node, token: str, typist: FakeTypist) -> dict:
    ctx = node.policy.context_for_token(token, tool="login", lane_id=LANE)
    return await login_flow(ctx, node.vault, typist, lane_id=LANE)


async def open_cards(client: httpx.AsyncClient, headers: dict, n: int = 1) -> list[dict]:
    for _ in range(400):
        r = await client.get("/api/codes", headers=headers)
        if r.status_code == 200 and len(r.json()) >= n:
            return r.json()
        await asyncio.sleep(0.005)
    raise AssertionError("no code card opened")


def sqlite_dump(node: Node) -> str:
    out = []
    for path in sorted(node.home.rglob("*")):
        if path.is_file():
            out.append(path.read_bytes().decode("latin-1"))  # raw pages, WAL and journals included
        if path.suffix == ".db":
            conn = sqlite3.connect(path)
            try:
                out.extend(conn.iterdump())
            finally:
                conn.close()
    return "\n".join(out)


async def test_the_code_goes_from_the_panel_to_the_site_and_nowhere_else(node, caplog):
    caplog.set_level(logging.DEBUG)
    await add_relay_entry(node)
    _, token = node.policy.issue(subject={"id": "agent"}, workspaces=["biz-a"], ceiling="commit",
                                 specials={"secret": {}})
    responses: list[str] = []
    async with node.client() as client:
        headers = await node.sign_in(client)
        typist = site()
        login = asyncio.create_task(agent_login(node, token, typist))
        [card] = await open_cards(client, headers)
        assert card["lane"] == LANE and card["site"] == "instagram.com" and card["state"] == "open"
        assert card["id"].startswith("cr_") and card["requested_by"]["id"] == "agent"

        status = await client.get("/api/status", headers=headers)
        assert status.json()["needs_you_detail"]["codes"] == 1
        responses.append(status.text)

        r = await client.post(f"/api/codes/{card['id']}", json={"code": TYPED}, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "used" and "code" not in r.json()
        responses.append(r.text)

        result = await login
        assert result == {"ok": True, "step": "done", "needs": None, "signal": "ok"}
        assert typist.typed_text("otp") == [CODE]

        again = await client.post(f"/api/codes/{card['id']}", json={"code": TYPED}, headers=headers)
        assert again.status_code == 404 and again.json()["error"]["code"] == "vault_code_request_closed"
        responses.append(again.text)
        events = await client.get("/api/events?last_event_id=0", headers=headers)
        responses.append((await client.get("/api/codes", headers=headers)).text)
        responses.append((await client.get("/api/activity?limit=500", headers=headers)).text)
        responses.append((await client.get("/api/approvals?state=approved", headers=headers)).text)

    assert "event: code" in events.text and card["id"] in events.text
    await node.policy.notifier.flush()
    outbox = node.policy.notifier.outbox(include_delivered=True)
    pushes = [row["event"] for row in outbox if row["event"].get("type") == "code.requested"]
    assert pushes and pushes[0]["code_request_id"] == card["id"] and pushes[0]["site"] == "instagram.com"

    node.policy.traces.flush()
    traces, _ = node.policy.traces.query(limit=500)
    by_kind = {t.kind: t for t in traces}
    assert {"vault.code_requested", "vault.code_used"} <= set(by_kind)
    used = by_kind["vault.code_used"].args
    assert used["source"] == "manual" and used["request"] == card["id"] and used["by"] == "human:owner"
    blob = "\n".join([json.dumps(result), *responses, events.text, json.dumps(outbox, default=str),
                      json.dumps([t.model_dump() for t in traces], default=str), caplog.text, sqlite_dump(node)])
    assert card["id"] in sqlite_dump(node)  # the scan sees the request's traces on disk
    for needle in (CODE, TYPED, CODE.lower()):
        assert needle not in blob, f"{needle!r} leaked"


async def test_a_grant_cannot_answer_its_own_code_card(node):
    await add_relay_entry(node)
    _, token = node.policy.issue(subject={"id": "agent"}, workspaces=["biz-a"], ceiling="commit",
                                 specials={"secret": {}})
    async with node.client() as client:
        headers = await node.sign_in(client)
        login = asyncio.create_task(agent_login(node, token, site()))
        [card] = await open_cards(client, headers)
        own = {"Authorization": f"Bearer {token}"}
        r = await client.post(f"/api/codes/{card['id']}", json={"code": CODE}, headers=own)
        assert r.status_code == 403  # not an approver, and its own family anyway
        _, admin_token = node.policy.issue(subject={"id": "va-dana", "kind": "panel"}, workspaces=["*"],
                                                 ceiling="commit", admin=True)
        assert (await client.get("/api/codes", headers={"Authorization": f"Bearer {admin_token}"})).json()
        # a code card is answered by a person (agentos.core.presence): an approver's token is not one
        refused = await client.post(f"/api/codes/{card['id']}", json={"code": CODE},
                                    headers={"Authorization": f"Bearer {admin_token}"})
        assert refused.status_code == 403 and refused.json()["error"]["code"] == "presence_required"
        bad = await client.post(f"/api/codes/{card['id']}", json={"code": "12"}, headers=headers)
        assert bad.status_code == 400 and "12" not in bad.json()["error"]["message"].replace("4 to 12", "")
        ok = await client.post(f"/api/codes/{card['id']}", json={"code": CODE}, headers=headers)
        assert ok.status_code == 200, ok.text  # the owner, signed in, answers it
        assert (await login)["ok"]


async def test_an_approver_never_answers_a_card_its_own_family_opened(node):
    await add_relay_entry(node)
    _, token = node.policy.issue(subject={"id": "va-dana", "kind": "panel"}, workspaces=["biz-a"],
                                 ceiling="commit", specials={"secret": {}}, admin=True)
    async with node.client() as client:
        headers = await node.sign_in(client)
        login = asyncio.create_task(agent_login(node, token, site()))
        [card] = await open_cards(client, headers)
        own = await client.post(f"/api/codes/{card['id']}", json={"code": CODE},
                                headers={"Authorization": f"Bearer {token}"})
        assert own.status_code == 403 and own.json()["error"]["code"] == "presence_required"  # a token is no person
        assert "—" not in own.text
        narrowed = node.policy.attenuate(token, admin=True, ttl_s=600)[1]  # the same family, one grant narrower
        child = await client.post(f"/api/codes/{card['id']}", json={"code": CODE},
                                  headers={"Authorization": f"Bearer {narrowed}"})
        assert child.status_code == 403 and child.json()["error"]["code"] == "presence_required"
        assert (await client.post(f"/api/codes/{card['id']}", json={"code": CODE}, headers=headers)).status_code \
            == 200  # the owner, signed in, answers it
        assert (await login)["ok"]


async def test_the_owner_dismisses_a_card(node):
    await add_relay_entry(node)
    _, token = node.policy.issue(subject={"id": "agent"}, workspaces=["biz-a"], ceiling="commit",
                                 specials={"secret": {}})
    async with node.client() as client:
        headers = await node.sign_in(client)
        typist = site()
        login = asyncio.create_task(agent_login(node, token, typist))
        [card] = await open_cards(client, headers)
        r = await client.post(f"/api/codes/{card['id']}/decline", json={}, headers=headers)
        assert r.status_code == 200 and r.json()["state"] == "declined"
        assert (await login)["needs"] == "human" and typist.typed_text("otp") == []
        assert (await client.get("/api/codes", headers=headers)).json() == []


async def test_code_routes_need_the_owner_and_a_well_formed_body(node):
    async with node.client() as client:
        assert (await client.get("/api/codes")).status_code == 401
        _, token = node.policy.issue(subject={"id": "worker"}, workspaces=["biz-a"], ceiling="read")
        worker = {"Authorization": f"Bearer {token}"}
        assert (await client.get("/api/codes", headers=worker)).status_code == 403
        headers = await node.sign_in(client)
        assert (await client.get("/api/codes", headers=headers)).json() == []
        missing = await client.post("/api/codes/cr_NOPE", json={"code": CODE}, headers=headers)
        assert missing.status_code == 404 and CODE not in missing.text
        extra = await client.post("/api/codes/cr_NOPE", json={"code": CODE, "note": "x"}, headers=headers)
        assert extra.status_code == 400 and CODE not in extra.text
        wrong_type = await client.post("/api/codes/cr_NOPE", json={"code": 730519}, headers=headers)
        assert wrong_type.status_code == 400 and "730519" not in wrong_type.text
        no_csrf = await client.post("/api/codes/cr_NOPE", json={"code": CODE})
        assert no_csrf.status_code == 403
