"""The freeze's surfaces: the MCP wrapper's guard, the lane_freeze tool, the push button, the panel
routes and the CLI (agentos.freeze)."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
import test_node_panel
from freeze_support import close, make_node, started
from starlette.testclient import TestClient
from test_api_members import ORIGIN, accept, browser, invite, owner, role
from test_node_panel import TOKEN, Node, bearer
from typer.testing import CliRunner

from agentos.api.auth import PRESENCE_HEADER, SESSION_COOKIE
from agentos.core.models import Effect
from agentos.core.tools import ToolSpec
from agentos.freeze import cli as freeze_cli
from agentos.freeze.store import FreezeRecord, FreezeStore
from agentos.freeze.tools import tool_specs
from agentos.policy import ToolCallError, bind_request_token, wrap_tool

EM_DASH = "—"


@pytest.fixture
async def node(tmp_path):
    n = await started(make_node(tmp_path))
    yield n
    await close(n)


def tools(n):
    async def look(ctx, *, on: str) -> dict:
        """Look at a lane."""
        await ctx.authorize(Effect.READ, lane_id=on)
        async with n.lanes.using(on, principal_kind=ctx.principal.kind):
            await asyncio.sleep(float(getattr(n, "work_s", 0)))
        return {"looked": on}

    async def think(ctx, *, on: str) -> dict:
        """A long call that never enters the lane."""
        await ctx.authorize(Effect.READ, lane_id=on)
        await asyncio.sleep(30)
        return {}

    specs = {s.name: s for s in [ToolSpec("look", look), ToolSpec("think", think), *tool_specs(n.runtime)]}
    return {name: wrap_tool(spec, n.runtime) for name, spec in specs.items()}


async def call(fn, token, **kw):
    with bind_request_token(token):
        return await fn(**kw)


# ---- MCP -----------------------------------------------------------------------------------------


async def test_mcp_calls_on_a_frozen_lane_fail_with_frozen(node):
    fns = tools(node)
    _, tok = node.agent(workspaces=["biz-a", "biz-b"])
    assert await call(fns["look"], tok, on="biz-a/one") == {"looked": "biz-a/one"}
    await node.freeze.freeze("lane", "biz-a/one", reason="stop", by="owner")
    with pytest.raises(ToolCallError) as e:
        await call(fns["look"], owner_token(node), on="biz-a/one")
    assert e.value.code == "frozen" and EM_DASH not in str(e.value)


def owner_token(n):  # the node's own token: in M1, Claude Code drives lanes with it
    return n.config.mcp_token


async def test_mcp_calls_in_flight_are_interrupted_the_owner_token_too(node):
    fns = tools(node)
    node.work_s = 30
    _, tok = node.agent(workspaces=["biz-a"])
    calls = [asyncio.create_task(call(fns["look"], owner_token(node), on="biz-a/one")),  # inside using()
             asyncio.create_task(call(fns["think"], tok, on="biz-a/two"))]                 # outside it
    await asyncio.sleep(0.1)
    report = await node.freeze.freeze("workspace", "biz-a", reason="stop", by="owner")
    assert report["stopped"]["calls"] == 2 and report["stopped"]["still_running"] == 0
    for c in calls:
        with pytest.raises(ToolCallError) as e:
            await c
        assert e.value.code == "frozen"


async def test_an_agent_freezes_its_own_lane_and_loses_its_grant(node):
    fns = tools(node)
    g, tok = node.agent(lanes=["biz-a/one"])
    with pytest.raises(ToolCallError) as e:
        await call(fns["lane_freeze"], tok, on="biz-a/one", reason="the page asked for my password",
                   scope="workspace")
    assert e.value.code == "denied"  # its grant does not cover the whole workspace
    out = await call(fns["lane_freeze"], tok, on="biz-a/one", reason="the page asked for my password")
    assert out["frozen"] and out["target"] == "biz-a/one" and g.id in out["revoked_grants"]
    assert "only the owner can unfreeze" in out["next"]
    with pytest.raises(ToolCallError) as e:
        await call(fns["look"], tok, on="biz-a/one")
    assert e.value.code == "frozen" and "freeze of lane biz-a/one" in str(e.value)  # revoked by the freeze
    rec = node.freeze.records()[0]
    assert rec.by == "agent:agent-1" and rec.via == "mcp"


async def test_a_wide_grant_may_freeze_its_workspace_or_the_node_and_no_tool_unfreezes(node):
    fns = tools(node)
    assert set(fns) == {"look", "think", "lane_freeze"}  # there is no unfreeze tool
    _, ws_tok = node.agent(workspaces=["biz-a"])
    out = await call(fns["lane_freeze"], ws_tok, on="biz-a/two", reason="r", scope="workspace")
    assert out["scope"] == "workspace" and out["target"] == "biz-a"
    _, all_tok = node.agent(workspaces=["*"], sub="wide", during_freeze=True)
    out = await call(fns["lane_freeze"], all_tok, on="biz-b/three", reason="r", scope="node")
    assert out["scope"] == "node"
    _, late = node.agent(workspaces=["*"], sub="late", during_freeze=True)
    out = await call(fns["lane_freeze"], late, on="biz-b/three", reason="again", scope="node")
    assert out["already_frozen"]  # still callable on a frozen node


# ---- the push button -----------------------------------------------------------------------------


async def test_every_owner_push_carries_a_one_tap_freeze_that_works_once(node):
    notifier = node.policy.notifier
    notifier.update_config({"public_base_url": "https://agentos.example.com", "ntfy": {"topic": "generate"}})
    one = notifier.ntfy_payload([{"type": "lane.held", "lane": "biz-a/one", "title": "t", "body": "b"}])
    assert [a["label"] for a in one["actions"]][-1] == "Freeze"
    url = httpx.URL(one["actions"][-1]["url"])
    assert url.path == "/api/freeze/link" and url.params["scope"] == "lane" and url.params["t"] == "biz-a/one"
    many = notifier.ntfy_payload([{"type": "notice", "title": "a"}, {"type": "notice", "title": "b"}])
    assert httpx.URL(many["actions"][-1]["url"]).params["scope"] == "node"
    member = notifier._freeze_action({"lane": "biz-a/one"}, "m_alice")
    assert member == []  # only the owner's pushes

    params = dict(url.params)
    tampered = {**params, "t": "biz-a/two"}
    with pytest.raises(Exception, match="not valid"):
        await node.freeze.redeem_link(tampered)
    out = await node.freeze.redeem_link(params)
    assert out["scope"] == "lane" and node.freeze.is_frozen("biz-a/one")
    with pytest.raises(Exception, match="already used"):
        await node.freeze.redeem_link(params)
    stale = node.freeze.link_params("lane", "biz-a/two", ttl_s=-1)
    with pytest.raises(Exception, match="expired"):
        await node.freeze.redeem_link(stale)
    rec = node.freeze.records()[0]
    assert rec.via == "push_link" and rec.by == "owner (push)"


async def test_no_freeze_button_without_a_public_url(node):
    node.policy.notifier.update_config({"ntfy": {"topic": "generate"}})
    payload = node.policy.notifier.ntfy_payload([{"type": "notice", "title": "a", "lane": "biz-a/one"}])
    assert [a["label"] for a in payload["actions"]] == ["Open"]


# ---- the panel -----------------------------------------------------------------------------------


@pytest.fixture
def panel(tmp_path, monkeypatch):
    monkeypatch.setattr(test_node_panel, "SERVICES", [*test_node_panel.SERVICES, "agentos.freeze.service"])
    n = Node(tmp_path)
    n.api_app.state.api.limiter.per_key = n.api_app.state.api.limiter.global_max = 10_000
    yield n
    n.close()


def test_the_owner_freezes_with_a_token_but_only_unfreezes_in_person(panel):
    with TestClient(panel.app, base_url=ORIGIN) as client:
        r = client.post("/api/freeze", json={"scope": "lane", "target": "biz-a/instagram", "reason": "stop"},
                        headers=bearer(TOKEN))
        assert r.status_code == 200, r.text
        assert r.json()["scope"] == "lane" and r.json()["by"] == "owner (token)"
        status = client.get("/api/freeze", headers=bearer(TOKEN)).json()
        assert [f["target"] for f in status["frozen"]] == ["biz-a/instagram"]
        r = client.post("/api/unfreeze", json={"scope": "lane", "target": "biz-a/instagram"}, headers=bearer(TOKEN))
        assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"
        agent = panel.grant(workspaces=["biz-b"])  # one that reaches the frozen lane is not made now
        r = client.post("/api/freeze", json={"scope": "workspace", "target": "biz-a", "reason": "x"},
                        headers=bearer(agent))
        assert r.status_code == 403  # an agent's token is not the owner
        boss = owner(panel)
        r = boss.post("/api/unfreeze", {"scope": "lane", "target": "biz-a/instagram"})
        assert r.status_code == 200 and r.json()["unfrozen"], r.text
        assert EM_DASH not in r.text


def test_with_members_an_admin_freezes_and_only_the_owner_unfreezes_on_a_fresh_check(panel):
    boss = owner(panel)
    r = invite(boss, "ada", [role("admin", "biz-a")])
    assert r.status_code == 200, r.text
    ada = accept(panel, r.json()["code"])
    r = ada.post("/api/freeze", {"scope": "lane", "target": "biz-a/instagram", "reason": "looks wrong"})
    assert r.status_code == 200, r.text
    r = ada.post("/api/freeze", {"scope": "lane", "target": "biz-b/facebook", "reason": "not mine"})
    assert r.status_code == 403
    r = ada.post("/api/freeze", {"scope": "node", "reason": "everything"})
    assert r.status_code == 403
    r = ada.post("/api/unfreeze", {"scope": "lane", "target": "biz-a/instagram"})
    assert r.status_code == 403  # an admin freezes, never unfreezes
    # an owner with no passkey unfreezes straight after a sign-in with a code, members or not
    r = boss.post("/api/unfreeze", {"scope": "lane", "target": "biz-a/instagram"})
    assert r.status_code == 200 and r.json()["unfrozen"], r.text


def test_the_push_link_route_needs_nothing_but_the_link(panel):
    svc = panel.runtime.get("freeze")
    params = svc.link_params("lane", "biz-a/instagram")
    with browser(panel) as client:
        r = client.post("/api/freeze/link", params={**params, "s": "forged"})
        assert r.status_code == 401
        r = client.post("/api/freeze/link", params=params)
        assert r.status_code == 200 and r.json()["frozen"], r.text
        assert svc.is_frozen("biz-a/instagram")


# ---- the CLI -------------------------------------------------------------------------------------


def test_cli_freezes_through_the_files_when_the_daemon_is_down_and_unfreezes_on_disk(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "node.toml").write_text('name = "box-1"\nmcp_token = "t0k3n-for-cli-tests"\nmcp_port = 1\n')
    monkeypatch.setenv("AGENTOS_HOME", str(home))
    runner = CliRunner()
    r = runner.invoke(freeze_cli.app, ["lane", "biz-a/one", "--reason", "from the shell"])
    assert r.exit_code == 0, r.output
    assert "did not answer" in r.output and "is frozen" in r.output and EM_DASH not in r.output
    records, _ = FreezeStore(home).load()
    rec = records["lane.biz-a.one"]
    assert rec.reason == "from the shell" and rec.via == "cli" and rec.applied_at is None
    r = runner.invoke(freeze_cli.app, ["status"])
    assert "biz-a/one" in r.output
    r = runner.invoke(freeze_cli.app, ["node", "--reason", "all"])
    assert r.exit_code == 0 and "this node is frozen" in r.output
    r = runner.invoke(freeze_cli.unfreeze_app, ["lane", "biz-a/one"])
    assert r.exit_code == 0 and "Nothing was restored" in r.output and "Still frozen by node" in r.output
    assert "lane.biz-a.one" not in FreezeStore(home).load()[0]
    r = runner.invoke(freeze_cli.unfreeze_app, ["node"])
    assert r.exit_code == 0 and FreezeStore(home).load()[0] == {}
    r = runner.invoke(freeze_cli.app, ["lane", "biz-a/one"])
    assert r.exit_code != 0  # a reason is required


def test_cli_uses_the_daemon_when_it_answers(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "node.toml").write_text('name = "box-1"\nmcp_token = "t0k3n-for-cli-tests"\nmcp_port = 1\n')
    monkeypatch.setenv("AGENTOS_HOME", str(home))
    seen = {}

    def fake_request(method, url, json=None, timeout=None, headers=None):
        seen.update(method=method, url=url, body=json)
        return httpx.Response(200, json={"scope": "lane", "target": "biz-a/one", "freeze_id": "fz_1",
                                         "already_frozen": False, "stopped": {"calls": 2, "ms": 41.0},
                                         "revoked": {"grants": [{"id": "g1", "subject": "agent:a"}],
                                                     "cascades_to": ["g2"], "standing": []},
                                         "runs_cancelled": ["r1"], "brain_tasks_cancelled": [],
                                         "schedules_paused": [], "triggers_paused": [], "lanes_warm": ["biz-a/one"],
                                         "errors": []})

    monkeypatch.setattr(httpx, "request", fake_request)
    r = CliRunner().invoke(freeze_cli.app, ["lane", "biz-a/one", "-r", "x"])
    assert r.exit_code == 0, r.output
    assert seen["method"] == "POST" and seen["url"].endswith("/api/freeze") and seen["body"]["target"] == "biz-a/one"
    assert "stopped 2 call(s) in flight in 41 ms" in r.output and "revoked 2 grant(s)" in r.output
    assert "kept warm for inspection: biz-a/one" in r.output
    assert not FreezeStore(home).load()[0]  # the daemon wrote it, not the CLI


def test_store_records_round_trip(tmp_path):
    store = FreezeStore(tmp_path)
    rec = FreezeRecord("workspace", "biz-a", "why", "owner", time.time(), revoked=["g1"])
    assert store.put(rec) == (True, True)
    back = store.load()[0]["workspace.biz-a"]
    assert (back.reason, back.by, back.revoked, back.id) == ("why", "owner", ["g1"], rec.id)


# ---- unfreeze with the node's one-time code (docs/THREAT-MODEL.md, agentos.core.presence) ----

UNFREEZE_LANE = {"scope": "lane", "target": "biz-a/instagram"}


def _freeze_lane(client) -> None:
    r = client.post("/api/freeze", json={**UNFREEZE_LANE, "reason": "stop"}, headers=bearer(TOKEN))
    assert r.status_code == 200, r.text


def _frozen(client) -> list[str]:
    return [f["target"] for f in client.get("/api/freeze", headers=bearer(TOKEN)).json()["frozen"]]


def test_the_node_token_with_a_one_time_code_unfreezes_the_lane(panel):
    """`agentos unfreeze` on the node: the node token plus a code it wrote into panel.db is a person for
    that one request, so it unfreezes."""
    panel.store.add_code("cli-unfreeze-1", ttl_seconds=60)
    with TestClient(panel.app, base_url=ORIGIN) as client:
        _freeze_lane(client)
        r = client.post("/api/unfreeze", json=UNFREEZE_LANE,
                        headers={**bearer(TOKEN), PRESENCE_HEADER: "cli-unfreeze-1"})
        assert r.status_code == 200 and r.json()["unfrozen"], r.text
        assert "biz-a/instagram" not in _frozen(client)
        assert EM_DASH not in r.text


def test_a_one_time_code_unfreezes_once(panel):
    panel.store.add_code("cli-unfreeze-2", ttl_seconds=60)
    headers = {**bearer(TOKEN), PRESENCE_HEADER: "cli-unfreeze-2"}
    with TestClient(panel.app, base_url=ORIGIN) as client:
        _freeze_lane(client)
        assert client.post("/api/unfreeze", json=UNFREEZE_LANE, headers=headers).status_code == 200
        _freeze_lane(client)
        r = client.post("/api/unfreeze", json=UNFREEZE_LANE, headers=headers)
        assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required", r.text
        assert "biz-a/instagram" in _frozen(client)


def test_unfreezing_without_a_person_is_refused(panel):
    """The node token alone, an agent's grant and a code that was never issued are no person."""
    with TestClient(panel.app, base_url=ORIGIN) as client:
        _freeze_lane(client)  # a freeze revokes the grants that reach the lane, and none that reach it is made now
        agent = panel.grant(workspaces=["biz-b"])
        for headers in (bearer(TOKEN), bearer(agent), {**bearer(TOKEN), PRESENCE_HEADER: "never-issued"}):
            r = client.post("/api/unfreeze", json=UNFREEZE_LANE, headers=headers)
            assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required", r.text
            assert EM_DASH not in r.text
        assert "biz-a/instagram" in _frozen(client)


def test_with_members_the_cli_code_does_not_skip_the_passkey_check(panel):
    """A one-time code proves the node's operator, not a passkey: with members, unfreezing over HTTP needs
    the panel's fresh passkey check, or `agentos unfreeze` on the node itself."""
    boss = owner(panel)
    r = invite(boss, "ada", [role("admin", "biz-a")])
    assert r.status_code == 200, r.text
    accept(panel, r.json()["code"])
    panel.store.add_code("cli-unfreeze-members", ttl_seconds=60)
    with TestClient(panel.app, base_url=ORIGIN) as client:
        _freeze_lane(client)
        r = client.post("/api/unfreeze", json=UNFREEZE_LANE,
                        headers={**bearer(TOKEN), PRESENCE_HEADER: "cli-unfreeze-members"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "step_up_required", r.text
        assert "agentos unfreeze" in r.json()["error"]["message"]
        assert "biz-a/instagram" in _frozen(client)


def _with_a_passkey(panel) -> None:
    """The owner has a passkey on this node: a stored credential is what makes it so (its key is never used here)."""
    panel.api_app.state.api.members.store.add_credential(member_id="owner", cred_id=b"owner-passkey", public_key=b"not-a-key",
                                                         sign_count=0, label="Laptop", aaguid=bytes(16))


@pytest.mark.parametrize("with_members", [False, True])
def test_an_owner_with_a_passkey_unfreezes_only_after_the_passkey_check(panel, with_members):
    _with_a_passkey(panel)
    boss = owner(panel)
    if with_members:
        assert invite(boss, "ada", [role("admin", "biz-a")]).status_code == 200
    assert boss.post("/api/freeze", {"scope": "lane", "target": "biz-a/instagram", "reason": "wait"}).status_code == 200
    r = boss.post("/api/unfreeze", {"scope": "lane", "target": "biz-a/instagram"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "step_up_required", r.text
    assert "passkey" in r.json()["error"]["message"]
    session = panel.store.get_session(boss.client.cookies[SESSION_COOKIE])
    panel.store.verified(session)  # what POST /api/session/verify records after the passkey check
    r = boss.post("/api/unfreeze", {"scope": "lane", "target": "biz-a/instagram"})
    assert r.status_code == 200 and r.json()["unfrozen"], r.text


def test_an_owner_with_no_passkey_unfreezes_only_after_a_code_sign_in_in_the_last_minutes(panel):
    boss = owner(panel)
    assert boss.post("/api/freeze", {"scope": "lane", "target": "biz-a/instagram", "reason": "wait"}).status_code == 200
    session = panel.store.get_session(boss.client.cookies[SESSION_COOKIE])
    store = panel.store  # the session store requests read
    with store._lock:  # the sign-in with the code was an hour ago
        store._db.execute("UPDATE sessions SET created_at = ? WHERE id_hash = ?", (time.time() - 3600, session.id_hash))
        store._db.commit()  # the app reads sessions on a connection of its own
    r = boss.post("/api/unfreeze", {"scope": "lane", "target": "biz-a/instagram"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "denied", r.text
    assert "last 5 minutes" in r.json()["error"]["message"] and "code" in r.json()["error"]["message"]
    with store._lock:
        store._db.execute("UPDATE sessions SET created_at = ? WHERE id_hash = ?", (time.time(), session.id_hash))
        store._db.commit()
    r = boss.post("/api/unfreeze", {"scope": "lane", "target": "biz-a/instagram"})
    assert r.status_code == 200 and r.json()["unfrozen"], r.text
