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

from agentos.api.auth import SESSION_COOKIE
from agentos.api.members import Role
from agentos.core import clock
from agentos.core.models import utcnow
from agentos.environments.models import Call, Environment, Machine

TOKEN = "t" * 43
MEMBER = "m_ana0001"

ENVS = "/api/orgs/local/environments"
CLEAN = [{"dir": "/work/shop", "branch": "main", "ahead": 0, "dirty": 0, "dirty_files": [], "unpushed": []}]
DIRTY = [{"dir": "/work/shop", "branch": "sale", "ahead": 1, "dirty": 1, "dirty_files": ["index.html"],
          "unpushed": ["abc1234 Thirty percent off"]}]


def add_operator(client: TestClient, *, scopes: tuple[str, ...] = ("biz-a",), member: str = MEMBER) -> None:
    """A member with operator in each of `scopes`, as an accepted invite leaves them (test_people's add_member)."""
    api = client.app.state.api
    api.members.store.create_member(
        member_id=member, handle=member[2:5], display_name="Ana", email=f"{member}@example.com", timezone="UTC",
        by="owner", roles=[Role(scope=scope, role="operator", options={}, expires_at=None) for scope in scopes],
        member_ttl_s=None,
    )
    api.members.sync(member, by="owner", force=True)


def as_member(client: TestClient, member: str = MEMBER) -> TestClient:
    """A second client signed in as the member, with a session cookie and its CSRF header."""
    api = client.app.state.api
    sid, session = api.store.create_session(member, 3600, member_id=member, via="passkey")
    member = TestClient(client.app, base_url="http://localhost")
    member.cookies.set(SESSION_COOKIE, sid)
    member.headers["x-agentos-csrf"] = session.csrf
    return member


def as_token(client: TestClient) -> TestClient:
    """The same node, reached with the owner's bearer token: an agent's way in."""
    bearer = TestClient(client.app, base_url="http://localhost")
    bearer.headers["authorization"] = f"Bearer {TOKEN}"
    return bearer


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
                          calls=[Call(at=now - timedelta(seconds=30), tool="terminal_exec", summary="npm test", ok=True)])
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
                            "location": "nbg1", "size": None,  # a record from before sizes had names
                            "swap": None, "updates": None}  # and before swap and the update notes
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
    assert body["git"] == {"branch": "main", "ahead": 0, "dirty": 0, "pushed": [], "deleted": []}
    assert body["calls"][0] == {"at": body["calls"][0]["at"], "tool": "terminal_exec", "summary": "npm test", "ok": True}
    assert body["commands"][0]["cmd"] == "npm test" and body["unsaved"] is None
    assert body["viewer"] is None  # the browser lane is not warm
    assert client.get(f"{ENVS}/enope").status_code == 404
    assert client.get(f"{ENVS}/..%2F..").status_code == 404


def test_a_closed_environment_still_lists_the_jobs_it_started(client: TestClient, envs: Any) -> None:
    # a job's record goes with its lane when the machine is deleted: the call that started it stays
    env = envs.get("eold")
    env.calls.append(Call(at=utcnow() - timedelta(days=6), tool="terminal_job_start", summary="npm run dev", ok=True))
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


def test_the_detail_has_the_bring_up_the_replay_and_the_resume(client: TestClient, envs: Any) -> None:
    body = client.get(f"{ENVS}/e7k2").json()
    assert body["bringup"] is None  # a record from before the steps were kept
    assert body["resumed"] is None and body["resumed_by"] is None and body["owner_woken"] is False
    assert body["resumed_at"] is None
    assert body["replay"] == {"frames": 0, "since": None, "bytes": 0, "kept_until": None}  # open: kept while it is
    steps = [{"step": "VM create", "at_s": 0.0, "seconds": 21.3, "ok": True},
             {"step": "clone", "at_s": 40.1, "seconds": 8.0, "ok": True},
             {"step": "browser", "at_s": 39.9, "seconds": 3.2, "ok": True}]

    def resumed(e: Environment) -> None:
        e.bringup = {"total_s": 52.4, "ok": True, "steps": steps}
        e.resumed, e.resumed_by = {"save_id": "s1", "from": "snapshot", "state": "restoring"}, "owner"
        e.binding = "kept:e7k2"

    envs.store.update("e7k2", resumed)
    body = client.get(f"{ENVS}/e7k2").json()
    up = body["bringup"]
    assert up["total_s"] == 52.4 and up["ok"] is True and up["running"] == [] and up["restore_from"] == "snapshot"
    assert [s["step"] for s in up["steps"]] == ["VM create", "browser", "clone"]  # by when each began
    assert body["resumed"]["save_id"] == "s1" and body["resumed_by"] == "owner"
    assert body["resumed_at"] == body["created_at"]
    assert body["owner_woken"] is False  # a chat made calls on it already
    closed = envs.get("eold")
    kept = client.get(f"{ENVS}/eold").json()["replay"]["kept_until"]
    assert kept == clock.iso(closed.closed_at + timedelta(days=envs.settings.replay_days), z=True)
    envs.settings.replay = False
    assert client.get(f"{ENVS}/e7k2").json()["replay"] is None  # the node keeps none


def test_the_prepared_machines_and_pool_are_the_owners(client: TestClient, envs: Any,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    from agentos.core.models import Effect
    from agentos.environments.fleet import worst_eur_month

    async def price() -> float:
        return 0.0123

    monkeypatch.setattr(envs, "_pool_plan_price", price)
    envs.settings.warm = 2
    got = client.get(f"{ENVS}/prepared")
    assert got.status_code == 200, got.text
    body = got.json()
    assert set(body) == {"machines", "eur_month", "eur_per_gb_month", "pool", "month"}
    assert body["machines"] == [] and body["eur_month"] == 0.0
    pool = body["pool"]
    assert {"warm", "target", "minutes", "machines", "price_hourly_eur", "worst_eur_month", "month_eur"} <= set(pool)
    assert pool["price_hourly_eur"] == 0.0123 and pool["target"] == 2
    assert pool["worst_eur_month"] == worst_eur_month(2, pool["minutes"], 0.0123)
    month = body["month"]
    start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    mine = [e for e in envs.store.recent(10) if e.created_at >= start]  # both, but on the 1st just after midnight
    assert month["opens"] == len(mine) and month["from_prepared"] == 0 and month["from_warm"] == 0
    assert [r["from"] for r in month["recent"]] == ["cold"] * len(mine)
    assert all(r["seconds"] == 60.0 for r in month["recent"])  # created 30 min ago, ready 29 min ago
    assert as_token(client).get(f"{ENVS}/prepared").status_code == 200  # the owner's own token
    add_operator(client, scopes=("env",))
    assert as_member(client).get(f"{ENVS}/prepared").status_code == 403
    policy = client.app.state.api.runtime.get("policy")
    _grant, token = policy.issue(subject={"id": "agent-b"}, workspaces=["*"], ceiling=Effect.COMMIT,
                                 specials={"vm": {}})
    agent = TestClient(client.app, base_url="http://localhost")
    agent.headers["authorization"] = f"Bearer {token}"
    assert agent.get(f"{ENVS}/prepared").status_code == 403


def test_no_environments_service_is_404_for_the_prepared_machines(client: TestClient,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    from agentos.api.web import environments

    monkeypatch.setattr(environments, "manager", lambda api: None)
    assert client.get(f"{ENVS}/prepared").status_code == 404
