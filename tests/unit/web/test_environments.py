"""Environments as the app reads them (agentos.api.web.environments): the list, one in detail, close
(with the 409 for unsaved work) and extend; who may (see to read, operate to close or extend, in
workspace env); an environment outside the caller's scope is not found. The environments are records
of the node's real environments service; what their machine would answer (ports, git) is stubbed, the
way test_jobs stubs nothing but runs commands on the fake host."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from starlette.testclient import TestClient

from agentos.core.models import utcnow
from agentos.environments.models import Call, Environment, Machine

from .test_engage import add_operator, as_member, as_token

ENVS = "/api/orgs/local/environments"
CLEAN = [{"dir": "/work/shop", "branch": "main", "ahead": 0, "dirty": 0, "dirty_files": [], "unpushed": []}]
DIRTY = [{"dir": "/work/shop", "branch": "sale", "ahead": 1, "dirty": 1, "dirty_files": ["index.html"],
          "unpushed": ["abc1234 Thirty percent off"]}]


@pytest.fixture
def envs(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Any:
    mgr = client.app.state.api.runtime.get("environments")
    now = utcnow()
    for eid, title, state in (("e7k2", "Black Friday page", "ready"), ("eold", "Last week", "closed")):
        env = Environment(id=eid, title=title, state=state, grant_id="gr_agent", binding=f"session:{eid}",
                          agent="claude-code", client="claude-code 2.1", repo="github.com/me/shop", branch="main",
                          workdir="/work/shop", created_at=now - timedelta(minutes=30), ready_at=now - timedelta(minutes=29),
                          last_call_at=now - timedelta(seconds=30), expires_at=now + timedelta(minutes=90),
                          machine=Machine(provider="hetzner", server_type="cpx22", cpu=2, memory_gb=4.0,
                                          location="nbg1"),
                          price_hourly_eur=0.0095,
                          calls=[Call(at=now - timedelta(seconds=30), tool="lane_exec", summary="npm test", ok=True)])
        if state == "closed":
            env.closed_at, env.closed_reason, env.cost_eur = now - timedelta(days=6), "closed by the agent", 0.019
        mgr.store.put(env)
    state = {"repos": CLEAN}

    async def repos(env):
        return state["repos"]

    async def ports(env):
        return [{"port": 3000, "process": "node", "link": None}]

    async def teardown(env, *, reason, final="closed"):
        env.state, env.closed_reason, env.closed_at, env.cost_eur = final, reason, utcnow(), 0.0095
        mgr.store.put(env)
        return env

    monkeypatch.setattr(mgr, "git_repos", repos)
    monkeypatch.setattr(mgr, "ports", ports)
    monkeypatch.setattr(mgr, "_teardown", teardown)
    monkeypatch.setattr(mgr, "cost_now", lambda env: env.cost_eur if env.cost_eur is not None else 0.0048)
    mgr.state = state
    return mgr


def test_the_list_shows_open_ones_and_all_on_request(client: TestClient, envs: Any) -> None:
    body = client.get(ENVS).json()
    assert [e["id"] for e in body["environments"]] == ["e7k2"]
    [e] = body["environments"]
    assert e["state"] == "working" and e["title"] == "Black Friday page" and e["repo"] == "github.com/me/shop"
    assert e["machine"] == {"provider": "hetzner", "server_type": "cpx22", "cpu": 2, "memory_gb": 4.0,
                            "location": "nbg1", "size": None}  # a record from before sizes had names
    assert e["price_hourly_eur"] == 0.0095 and e["cost_eur"] == 0.0048
    assert e["agent"] == {"name": "claude-code", "client": "claude-code 2.1"}
    assert e["lanes"] == {"terminal": "env/e7k2", "browser": "env/e7k2-browser"}
    assert {k for k in ("created_at", "last_call_at", "closed_at", "expires_at")} <= set(e)
    every = client.get(ENVS, params={"state": "all"}).json()["environments"]
    assert {x["id"] for x in every} == {"e7k2", "eold"}
    assert client.get(ENVS, params={"state": "sometimes"}).status_code == 400


def test_one_environment_in_detail(client: TestClient, envs: Any) -> None:
    body = client.get(f"{ENVS}/e7k2").json()
    assert body["ports"] == [{"port": 3000, "process": "node", "link": None}]
    assert body["git"] == {"branch": "main", "ahead": 0, "dirty": 0, "pushed": []}
    assert body["calls"][0] == {"at": body["calls"][0]["at"], "tool": "lane_exec", "summary": "npm test", "ok": True}
    assert body["commands"][0]["cmd"] == "npm test" and body["unsaved"] is None
    assert body["viewer"] is None  # the browser lane is not warm
    assert client.get(f"{ENVS}/enope").status_code == 404
    assert client.get(f"{ENVS}/..%2F..").status_code == 404


def test_a_closed_environment_still_lists_the_jobs_it_started(client: TestClient, envs: Any) -> None:
    # a job's record goes with its lane when the machine is deleted: the call that started it stays
    env = envs.get("eold")
    env.calls.append(Call(at=utcnow() - timedelta(days=6), tool="lane_job_start", summary="npm run dev", ok=True))
    envs.store.put(env)
    commands = client.get(f"{ENVS}/eold").json()["commands"]
    assert [(c["cmd"], c["state"]) for c in commands] == [("npm test", "exited"), ("npm run dev", "ended")]
    assert commands[1]["id"].startswith("start-")  # not a job id: the app links no run page for it


def test_close_answers_409_with_the_unsaved_work_then_closes_with_discard(client: TestClient, envs: Any) -> None:
    envs.state["repos"] = DIRTY
    refused = client.post(f"{ENVS}/e7k2/close", json={})
    assert refused.status_code == 409
    err = refused.json()["error"]
    assert err["code"] == "unsaved_work" and "push every branch" in err["message"].lower()
    assert err["unsaved"] == {"dirty_files": ["index.html"], "unpushed": ["abc1234 Thirty percent off"], "stashes": []}
    assert envs.get("e7k2").open
    done = client.post(f"{ENVS}/e7k2/close", json={"discard": True})
    assert done.status_code == 200 and done.json()["state"] == "closed"
    assert envs.get("e7k2").closed_reason == "closed from the app (discarded)"
    assert client.post(f"{ENVS}/e7k2/close", json={"discard": "yes"}).status_code == 400


def test_extend_gives_more_time_within_the_limit(client: TestClient, envs: Any) -> None:
    before = envs.get("e7k2").expires_at
    out = client.post(f"{ENVS}/e7k2/extend", json={"minutes": 30})
    assert out.status_code == 200, out.text
    assert envs.get("e7k2").expires_at - before == timedelta(minutes=30)
    assert client.post(f"{ENVS}/e7k2/extend", json={"minutes": 0}).status_code == 400
    assert client.post(f"{ENVS}/eold/extend", json={"minutes": 30}).status_code == 400  # closed


def test_a_member_without_env_sees_none_and_cannot_close(client: TestClient, envs: Any) -> None:
    add_operator(client, scopes=("biz-a",))
    member = as_member(client)
    assert member.get(ENVS).json()["environments"] == []
    assert member.get(f"{ENVS}/e7k2").status_code == 404
    assert member.post(f"{ENVS}/e7k2/close", json={"discard": True}).status_code in (403, 404)
    assert envs.get("e7k2").open


def test_a_member_with_env_reads_and_closes_only_their_own_agents(client: TestClient, envs: Any) -> None:
    from .test_engage import MEMBER

    add_operator(client, scopes=("env",))
    member = as_member(client)
    # the owner's agent opened it: not the member's (2026-11-13: a member sees their own agents' only)
    assert member.get(ENVS).json()["environments"] == []
    assert member.get(f"{ENVS}/e7k2").status_code == 404
    assert member.post(f"{ENVS}/e7k2/close", json={"discard": True}).status_code == 404
    envs.store.update("e7k2", lambda e: setattr(e, "member", MEMBER))  # the member's own agent opened it
    assert [e["id"] for e in member.get(ENVS).json()["environments"]] == ["e7k2"]
    assert member.post(f"{ENVS}/e7k2/close", json={}).status_code == 200


def test_an_agents_grant_outside_env_reads_none(client: TestClient, envs: Any) -> None:
    from agentos.core.models import Effect

    assert [e["id"] for e in as_token(client).get(ENVS).json()["environments"]] == ["e7k2"]  # the owner's token
    policy = client.app.state.api.runtime.get("policy")
    _grant, token = policy.issue(subject={"id": "agent-b"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    agent = TestClient(client.app, base_url="http://localhost")
    agent.headers["authorization"] = f"Bearer {token}"
    listed = agent.get(ENVS)
    assert listed.status_code == 403 or listed.json()["environments"] == []
    assert agent.get(f"{ENVS}/e7k2").status_code in (403, 404)
    assert agent.post(f"{ENVS}/e7k2/close", json={"discard": True}).status_code in (403, 404)
    assert envs.get("e7k2").open


def test_a_computer_without_environments_says_so(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentos.api.web import environments

    monkeypatch.setattr(environments, "manager", lambda api: None)
    body = client.get(ENVS).json()
    assert body["environments"] == [] and body["available"] is False
    assert client.get(f"{ENVS}/e7k2").status_code == 404


def test_an_environments_lanes_are_not_accounts(client: TestClient, envs: Any) -> None:
    # env/<id> and env/<id>-browser are the agent's own computer, on Environments; Accounts and Home
    # leave them out, and workspace env with them when nothing else is in it
    from agentos.api.web.environments import without_environments

    api = client.app.state.api
    lanes = [{"id": "env/e7k2"}, {"id": "env/e7k2-browser"}]
    shop = {"id": "shop", "lanes": [{"id": "shop/x"}]}
    assert without_environments(api, [{"id": "env", "lanes": lanes}, shop]) == [shop]
    mixed = without_environments(api, [{"id": "env", "lanes": [*lanes, {"id": "env/mine"}]}])
    assert mixed == [{"id": "env", "lanes": [{"id": "env/mine"}]}]
    # a lane named like one of an environment that does not exist stays where it is
    assert without_environments(api, [{"id": "env", "lanes": [{"id": "env/zzzz-browser"}]}])[0]["lanes"]
