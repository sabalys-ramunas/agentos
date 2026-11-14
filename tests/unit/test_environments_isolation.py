"""Whose an environment is (agentos.environments): only the grant family that opened it acts on its
lanes, reads it or closes it, and the owner and that member in the web app; ids cannot be guessed;
the VM's one gateway token reaches its own repository only, through its own tunnel, until it
closes; one chat holds an environment at a time, and a chat that lost it or reconnected is told
what is open. The reviewer's probes of 2026-11-13 (findings 1, 2, 3, 5, 6, 7, 10 and 14), as tests."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_environments import TITLE, node, opened  # noqa: F401  (node is a fixture)

from agentos.api.bridge import panel_specials
from agentos.client.ops import DEFAULT_SPECIALS
from agentos.core.errors import LaneNotFound
from agentos.core.models import Effect, utcnow
from agentos.environments import manager as envmod
from agentos.environments.manager import EnvironmentManager, new_id
from agentos.environments.models import ID_RE, Environment
from agentos.git.gateway import Refused
from agentos.git.service import GitService
from agentos.policy.wrap import ToolCallError

HOST = "example.test"


def default_grant(node, subject: str):  # noqa: F811
    """The grant `agentos connect` gives an agent by default."""
    return node["policy"].issue(subject={"id": subject}, workspaces=["*"], ceiling=Effect.COMMIT,
                                specials=panel_specials(DEFAULT_SPECIALS))


# ------------------------------------------------------------------ 1, 2: another agent's environment


async def test_another_agent_cannot_run_on_drive_or_list_an_environment(node):  # noqa: F811
    a = await opened(node)
    terminal, browser = a["lanes"]["terminal"], a["lanes"]["browser"]
    g2, tok2 = default_grant(node, "stranger")
    before = list(node["machine"].ran)
    for tool, kw in (("lane_exec", {"cmd": "cat ~/.bash_history"}), ("lane_job_start", {"cmd": "sleep 1"}),
                     ("shell_open", {})):
        with pytest.raises(ToolCallError, match="lane_not_found"):
            await node["call"](tool, on=terminal, session="chat-x", tok=tok2, **kw)
    assert node["machine"].ran == before  # nothing ran on A's machine
    # the browser lane: every check the browser tools go through refuses it, as a lane that does not exist
    ctx = node["policy"].context(g2, tool="browser_navigate")
    ctx.mcp = True
    with pytest.raises(LaneNotFound, match="no lane named"):
        ctx.precheck(Effect.READ, lane_id=browser)
    with pytest.raises(LaneNotFound):
        await ctx.authorize(Effect.READ, lane_id=browser)
    assert not ctx.can_access_lane(browser) and not ctx.can_access_lane(terminal)
    assert ctx.can_access_lane("env/plain")  # a lane no environment made: the usual rules


async def test_the_family_reaches_it_and_the_owner_only_outside_mcp(node):  # noqa: F811
    a = await opened(node)
    terminal = a["lanes"]["terminal"]
    policy = node["policy"]
    # a grant narrowed from the opener's (a brain or a routine of the same agent) is the same family
    _child, child_tok = policy.attenuate(node["token"], ttl_s=600)
    out = await node["call"]("lane_exec", on=terminal, cmd="echo child", session="chat-c", tok=child_tok)
    assert out["exit_code"] == 0
    # the node's token over MCP is an agent's call like any other
    from test_policy_support import OWNER

    with pytest.raises(ToolCallError, match="lane_not_found"):
        await node["call"]("lane_exec", on=terminal, cmd="echo owner", session="chat-o", tok=OWNER)
    # the owner outside MCP (the web app, the CLI): reaches every one
    owner_ctx = policy.context(policy.owner_grant(), tool="panel.environment.read")
    assert owner_ctx.can_access_lane(terminal)
    owner_ctx.precheck(Effect.READ, lane_id=terminal)


async def test_the_default_grant_opens_its_own_and_works_there(node):  # noqa: F811
    # finding 2: the grant agentos connect gives by default opens one and runs commands on it, asking nobody
    _g, tok = default_grant(node, "claude-code")
    out = await node["call"]("environment_open", title="Mine", session="chat-d", tok=tok)
    assert out["state"] == "ready", out
    ran = await node["call"]("lane_exec", cmd="npm test", session="chat-d", tok=tok)
    assert ran["exit_code"] == 0 and ran.get("status") != "approval_pending"
    with pytest.raises(ToolCallError):
        await node["call"]("lane_exec", on="personal/code", cmd="ls", session="chat-d", tok=tok)


def test_ids_are_long_and_readable():
    ids = {new_id() for _ in range(200)}
    assert len(ids) == 200
    for eid in ids:
        assert ID_RE.match(eid) and len(eid) == 1 + envmod.ID_LENGTH
        assert not set(eid[1:]) & set("0o1li")
    assert len(envmod._ID_CHARS) ** envmod.ID_LENGTH > 10 ** 14
    Environment(id="e7k2", title="t", grant_id="g", binding="b", created_at=utcnow(), expires_at=utcnow())  # older


async def test_a_session_id_of_another_agent_finds_nothing(node):  # noqa: F811
    a = await opened(node)  # chat-1
    _g2, tok2 = default_grant(node, "stranger")
    with pytest.raises(ToolCallError, match="no_environment") as err:
        await node["call"]("lane_exec", cmd="id", session="chat-1", tok=tok2)
    assert a["id"] not in str(err.value)
    seen = await node["call"]("environment", session="chat-1", tok=tok2)
    assert seen["environment"] is None and seen["open"] == []


# ------------------------------------------------------------------ 3, 5, 14: the VM's one token


@pytest.fixture
async def git(node, monkeypatch):  # noqa: F811
    gitsvc = GitService(node["rt"], node["mgr"].home, allow_file=True, host_port=0)
    node["rt"].register("git", gitsvc)
    gitsvc.remotes.allow("env", f"{HOST}/me/shop", url="/nonexistent/shop.git")
    gitsvc.remotes.allow("env", f"{HOST}/me/secret", url="/nonexistent/secret.git")
    fed: list[tuple[str, str | None]] = []
    plain = node["machine"].exec

    async def exec_(lane_id, cmd, *, timeout_s=None, stdin=None, grant_id=None):
        fed.append((cmd, stdin))
        return await plain(lane_id, cmd, timeout_s=timeout_s, stdin=stdin, grant_id=grant_id)

    monkeypatch.setattr(node["envs"], "exec", exec_)
    yield SimpleNamespace(svc=gitsvc, fed=fed)
    await gitsvc.stop()
    for lane in list(gitsvc._vm_listeners):
        await gitsvc.vm_down(lane)


def _vm_token(fed) -> str:
    [config] = [stdin for cmd, stdin in fed if stdin and "agtg_" in stdin]
    return next(w for w in config.split() if w.startswith("agtg_"))


async def test_the_vm_token_reaches_its_own_repository_only_and_never_a_command_line(node, git):  # noqa: F811
    a = await opened(node, repo=f"{HOST}/me/shop")
    assert a["state"] == "ready", a
    lane = a["lanes"]["terminal"]
    token = _vm_token(git.fed)
    # it went in through stdin; no command line on the node carries it (ps shows those)
    assert all(token not in cmd for cmd, _ in git.fed) and all(token not in cmd for cmd, _ in node["machine"].ran)
    config = next(stdin for _cmd, stdin in git.fed if stdin and token in stdin)
    assert f"{HOST}/me/shop" in config and "me/secret" not in config
    # a command on the VM gets no token in its environment: a VM lane's git config lives on the VM
    rt = SimpleNamespace(lane=SimpleNamespace(id=lane, workspace="env", remote=object()))
    assert await git.svc.lane_env(rt, node["grant"].id) == {}
    gw = git.svc.gateway
    caller = gw.caller(token, listener_lane=lane)
    assert caller.repos == (f"{HOST}/me/shop",) and caller.lane == lane
    # refused on the node's own listener and on another lane's
    with pytest.raises(Refused, match="another lane"):
        gw.caller(token, listener_lane=None)
    with pytest.raises(Refused, match="another lane"):
        gw.caller(token, listener_lane="env/eother")
    # another declared repository: refused
    from agentos.git.gateway import Request
    from agentos.git.wire import Remote

    req = Request("GET", "/x", {"x-agentos-git": token}, b"")
    with pytest.raises(Refused, match="reaches only example.test/me/shop"):
        gw._resolve(req, Remote(HOST, "me/secret"), lane, push=False)
    assert gw._resolve(req, Remote(HOST, "me/shop"), lane, push=True)[0] == caller


async def test_no_declared_repository_means_no_token_at_all(node, git):  # noqa: F811
    out = await opened(node)  # an empty /work
    assert out["state"] == "ready"
    assert not any(stdin and "agtg_" in stdin for _cmd, stdin in git.fed)
    assert git.svc.gateway._tokens == {}
    other = await opened(node, title="Public", session="chat-p", repo="example.org/someone/public")
    assert other["state"] == "ready" and git.svc.gateway._tokens == {}


async def test_the_token_dies_with_its_environment(node, git):  # noqa: F811
    a = await opened(node, repo=f"{HOST}/me/shop")
    lane = a["lanes"]["terminal"]
    token = _vm_token(git.fed)
    port = git.svc._vm_listeners[lane][1]
    await node["call"]("environment_close", discard=True)
    with pytest.raises(Refused, match="no longer valid"):
        git.svc.gateway.caller(token, listener_lane=lane)
    assert lane not in git.svc._vm_listeners
    with pytest.raises(OSError):  # its listener is closed
        _r, w = await asyncio.open_connection("127.0.0.1", port)
        w.close()


async def test_a_token_of_a_closed_environment_is_refused_even_before_teardown(node, git):  # noqa: F811
    a = await opened(node, repo=f"{HOST}/me/shop")
    lane = a["lanes"]["terminal"]
    token = _vm_token(git.fed)
    node["mgr"].store.update(a["id"], lambda e: setattr(e, "state", "closed"))  # as the CLI and a crash leave it
    with pytest.raises(Refused, match="environment closed"):
        git.svc.gateway.caller(token, listener_lane=lane)


# ------------------------------------------------------------------ 6, 7: one chat at a time


async def test_another_chat_does_not_take_an_environment_in_use(node):  # noqa: F811
    a = await opened(node)
    mgr = node["mgr"]
    await node["call"]("lane_exec", cmd="echo working")
    with pytest.raises(ToolCallError, match="environment_in_use") as err:
        await node["call"]("environment_open", title=TITLE, session="chat-2")
    assert "last call" in str(err.value) and "UTC" in str(err.value)
    assert mgr.get(a["id"]).binding == "session:chat-1"
    ok = await node["call"]("lane_exec", cmd="echo still mine")  # chat-1 keeps working
    assert ok["exit_code"] == 0


async def test_a_quiet_environment_moves_and_the_chat_that_lost_it_is_told(node):  # noqa: F811
    a = await opened(node)
    mgr = node["mgr"]
    mgr._clock = lambda: utcnow() + timedelta(minutes=11)
    other = await node["call"]("environment_open", title=TITLE, session="chat-2")
    assert other["id"] == a["id"] and mgr.get(a["id"]).binding == "session:chat-2"
    with pytest.raises(ToolCallError, match="no_environment") as err:
        await node["call"]("lane_exec", cmd="echo hi")  # chat-1
    assert "another chat of this agent took environment" in str(err.value) and a["id"] in str(err.value)


async def test_a_reconnected_chat_hears_its_open_environments(node):  # noqa: F811
    a = await opened(node)
    with pytest.raises(ToolCallError, match="no_environment") as err:
        await node["call"]("lane_exec", cmd="echo hi", session="chat-1-reconnected")
    msg = str(err.value)
    assert a["id"] in msg and TITLE in msg and "takes yours back" in msg
    seen = await node["call"]("environment", session="chat-1-reconnected")
    assert [o["id"] for o in seen["open"]] == [a["id"]] and "last_call_at" in seen["open"][0]


async def test_concurrent_opens_in_one_chat_rent_one_machine(node):  # noqa: F811
    call = node["call"]
    res = await asyncio.gather(call("environment_open", title="One", wait_s=5),
                               call("environment_open", title="Two", wait_s=5),
                               call("environment_open", title="One", wait_s=5))
    assert len({r["id"] for r in res}) == 1, res
    assert len(node["mgr"].store.open()) == 1
    assert len(node["vms"].store.recent()) == 1  # one machine rented, ever


# ------------------------------------------------------------------ 14: the CLI's close


async def test_a_close_from_the_cli_survives_agentosd_recording_a_call(node):  # noqa: F811
    a = await opened(node)
    mgr = node["mgr"]
    stale = mgr.get(a["id"])
    mgr.store.update(a["id"], lambda e: setattr(e, "state", "closing"))  # the CLI, in its own process
    mgr.record_call(stale.terminal_lane, "lane_exec", ok=True, summary="late")  # agentosd, from before
    assert mgr.get(a["id"]).state == "closing"


# ------------------------------------------------------------------ 10: summaries are scrubbed


async def test_call_summaries_are_scrubbed_as_traces_are(node):  # noqa: F811
    a = await opened(node)
    secret = "github_pat_" + "A1b2C3d4" * 6
    await node["call"]("lane_exec", cmd=f"git push https://x:{secret}@example.test/me/shop")
    env = node["mgr"].get(a["id"])
    assert secret not in env.calls[-1].summary and "redacted" in env.calls[-1].summary
    detail = await node["mgr"].detail(env)
    assert secret not in str(detail["calls"]) and secret not in str(detail["commands"])


def test_reaches_is_the_family_and_people_outside_mcp():
    now = utcnow()
    env = Environment(id="eabcdefghjk", title="t", grant_id="gr_child", grant_root="gr_root", binding="b",
                      member="m_ann", created_at=now, expires_at=now)

    def grant(root, *, owner=False, admin=False, sub=None):
        return SimpleNamespace(root_id=root, chain_ids=[root], owner_token=owner, admin=admin,
                               subject=sub or SimpleNamespace(kind="agent", id="x", owner=None))

    reaches = EnvironmentManager.reaches
    assert reaches(env, grant("gr_root"), mcp=True)
    assert not reaches(env, grant("gr_other"), mcp=True) and not reaches(env, grant("gr_other"), mcp=False)
    assert not reaches(env, grant("owner", owner=True), mcp=True) and reaches(env, grant("owner", owner=True), mcp=False)
    ann = SimpleNamespace(kind="human", id="m_ann", owner="m_ann")
    bob = SimpleNamespace(kind="human", id="m_bob", owner="m_bob")
    assert reaches(env, grant("gr_m1", sub=ann), mcp=False) and not reaches(env, grant("gr_m2", sub=bob), mcp=False)
    assert not reaches(env, grant("gr_m1", sub=ann), mcp=True)


# ------------------------------------------------------------------ 10: the web routes


@pytest.fixture
def panel(tmp_path):
    from test_api_members import Node

    from agentos.api.auth import RateLimiter

    n = Node(tmp_path)
    n.api_app.state.api.limiter = RateLimiter(per_key=10_000, global_max=10_000)
    mgr = EnvironmentManager(n.config.home, runtime=n.runtime)
    n.runtime.register("environments", mgr)
    yield n, mgr
    mgr.store.close()
    n.close()


async def test_an_environment_records_who_opened_it(node):  # noqa: F811
    g, tok = node["policy"].issue(subject={"id": "anns-agent"}, workspaces=["*"], ceiling=Effect.COMMIT,
                                  specials=panel_specials(DEFAULT_SPECIALS), owner="m_ann")
    out = await node["call"]("environment_open", title="Hers", session="chat-ann", tok=tok)
    env = node["mgr"].get(out["id"])
    assert (env.grant_root, env.subject, env.member) == (g.root_id, "anns-agent", "m_ann")
    mine = await opened(node, session="chat-own")
    assert node["mgr"].get(mine["id"]).member is None  # the owner's own agent


def _record(mgr, eid: str, *, member: str | None, root: str) -> Environment:
    now = utcnow()
    env = Environment(id=eid, title=f"Work {eid}", grant_id=root, grant_root=root, binding=f"session:{eid}",
                      member=member, state="ready", created_at=now, ready_at=now,
                      expires_at=now + timedelta(hours=1))
    mgr.store.put(env)
    return env


def test_a_member_sees_and_closes_only_their_own_agents_environments(panel):
    from test_api_members import join, owner, role

    api_node, mgr = panel
    boss = owner(api_node)
    ann = join(api_node, boss, "ann", [role("operator", scope="env")])
    bob = join(api_node, boss, "bob", [role("operator", scope="env")])
    hers = _record(mgr, "eannsagent2", member=ann.member["id"], root="gr_ann_agent")
    his = _record(mgr, "ebobsagent2", member=bob.member["id"], root="gr_bob_agent")
    owners = _record(mgr, "eownersagnt", member=None, root="gr_owner_agent")
    base = "/api/orgs/local/environments"
    seen = ann.get(base)
    assert seen.status_code == 200, seen.text
    assert [e["id"] for e in seen.json()["environments"]] == [hers.id]
    assert {e["id"] for e in boss.get(base).json()["environments"]} == {hers.id, his.id, owners.id}
    assert ann.get(f"{base}/{hers.id}").status_code == 200
    for other in (his, owners):
        assert ann.get(f"{base}/{other.id}").status_code == 404  # not found: an id tells nothing
        assert ann.post(f"{base}/{other.id}/close", {"discard": True}).status_code == 404
        assert mgr.get(other.id).open
    assert ann.post(f"{base}/{his.id}/extend", {"minutes": 30}).status_code == 404
