"""A terminal lane that shares the node's filesystem or processes reaches the node, whatever its other
dials say (review of the 2026-11-10 fix wave, finding 1). It binds `/` (or sees every process), so it
reads the vault, node.toml and every profile: it needs `--can-read-node-secrets`, says so in its
isolation line, sends `runs_on_node` on every agent path, and every agent command in it waits for a
person. These started as the reviewer's proofs of the attack; they now assert it is stopped."""

import pytest
from test_policy_support import Clock, make_service

from agentos.core.errors import ApprovalPending, InvalidRequest
from agentos.core.models import Effect, Principal
from agentos.core.runtime import Runtime
from agentos.env import exposure, tool_specs
from agentos.env.host import FakeHost
from agentos.env.manager import EnvManager
from agentos.env.models import EnvLane
from agentos.env.paths import EnvSettings
from agentos.env.presets import resolve
from agentos.env.sandbox import DirectLauncher
from agentos.env.tools import _facts, lane_exec
from agentos.policy.grants import LaneCap, Specials


@pytest.fixture
async def env(tmp_path):
    svc, rt = make_service(tmp_path, clock=Clock())
    mgr = EnvManager(tmp_path / "env", runtime=rt, host=FakeHost(), launcher=DirectLauncher(),
                     settings=EnvSettings(proxy_port=0))
    rt.register("envs", mgr)
    yield svc, mgr
    await mgr.shutdown()
    svc.close()


def agent_ctx(svc):
    _g, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.IRREVERSIBLE,
                        specials=Specials(exec=LaneCap()))
    p, g = svc.resolve(tok)
    return lambda: svc.context(g, principal=p, tool="terminal_exec", token=tok)


async def run_twice(svc, ctx, lane):
    """Approve one command, then try a different one: does it run on its own?"""
    with pytest.raises(ApprovalPending) as e:
        await lane_exec(ctx(), on=lane, cmd="echo first")
    svc.approvals.decide(e.value.approval_id, "approve")
    await lane_exec(ctx(), on=lane, cmd="echo first")
    try:
        out = await lane_exec(ctx(), on=lane, cmd="id; echo second-different-command")
        return "ran", out
    except ApprovalPending:
        return "asked", None


@pytest.mark.parametrize(("preset", "overrides"), [
    ("workbench", {"network": "off"}),           # the reviewer's first attack
    ("workbench", {"network": "isolated"}),
    ("sandbox", {"filesystem": "shared"}),       # the reviewer's second attack
    ("sandbox", {"processes": "shared"}),        # a shared process table reaches the node too
])
async def test_a_lane_sharing_the_node_needs_the_flag_and_asks_every_time(env, preset, overrides):
    svc, mgr = env
    with pytest.raises(InvalidRequest, match="can-read-node-secrets") as e:
        mgr.create("biz-a/fs", preset=preset, overrides=overrides)
    assert "Nothing was created" in e.value.message and "—" not in e.value.message
    assert not mgr.has("biz-a/fs")
    info = mgr.create("biz-a/fs", preset=preset, overrides=overrides, node_secrets_ok=True)
    lane = mgr.get("biz-a/fs")
    assert lane.isolated and exposure.reaches_node(lane) and not lane.contained
    facts = _facts(lane)
    assert facts["isolated"] is False and "node token" in facts["runs_on_node"]
    assert "shares the node's" in info.isolation and "waits for a person" in info.isolation
    assert "—" not in info.isolation + facts["runs_on_node"]
    what, _ = await run_twice(svc, agent_ctx(svc), "biz-a/fs")
    assert what == "asked"


async def test_shared_network_alone_stays_a_sandbox(env):
    """Decided in exposure.reaches_node: the node's loopback services all ask for a secret the lane
    cannot read, so a shared network alone does not reach the node's files."""
    _svc, mgr = env
    mgr.create("biz-a/net", preset="sandbox", overrides={"network": "shared"})
    lane = mgr.get("biz-a/net")
    assert not exposure.reaches_node(lane) and _facts(lane) == {"isolated": True}


async def test_a_lane_made_before_the_rule_is_treated_the_same(env, tmp_path):
    """No migration: a stored lane that shares the node's filesystem (made before this fix, so with no
    flag) asks every time, and doctor warns about it."""
    svc, mgr = env
    profile, limits, policy = resolve("sandbox", overrides={"filesystem": "shared"})
    mgr.store.add_lane(EnvLane(id="biz-a/old", profile=profile, limits=limits, policy=policy))
    what, _ = await run_twice(svc, agent_ctx(svc), "biz-a/old")
    assert what == "asked"
    rows = {r["id"]: r for r in exposure.doctor_rows(tmp_path / "env")}
    assert "biz-a/old" in rows["env.workbench"]["detail"]


async def test_control_real_workbench_asks_every_time(env):
    svc, mgr = env
    mgr.create("biz-a/wb", preset="workbench", node_secrets_ok=True)
    what, _ = await run_twice(svc, agent_ctx(svc), "biz-a/wb")
    assert what == "asked"


async def test_control_a_private_sandbox_asks_once(env):
    _svc, mgr = env
    mgr.create("biz-a/sb", preset="sandbox")
    lane = mgr.get("biz-a/sb")
    assert lane.contained and _facts(lane) == {"isolated": True}


class RecordingCtx:
    def __init__(self, runtime):
        self.runtime = runtime
        self.principal = Principal(id="test-agent")
        self.calls: list[dict] = []

    async def authorize(self, effect, *, lane_id=None, domain=None, summary=None, content=None, facts=None):
        self.calls.append({"effect": effect, "facts": facts})
        raise ApprovalPending("stop here", approval_id="appr_x")

    def trace(self, kind, **fields):
        pass


@pytest.mark.parametrize(("tool", "args"), [
    ("terminal_exec", {"cmd": "id"}),
    ("terminal_job_start", {"cmd": "id"}),
    ("shell_open", {}),
    ("terminal_install", {"packages": ["jq"]}),
    ("terminal_upload", {"chunks": {"9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08": "dGVzdA=="}}),
])
async def test_every_agent_path_sends_runs_on_node(tmp_path, tool, args):
    mgr = EnvManager(tmp_path / "home", host=FakeHost(), launcher=DirectLauncher(), settings=EnvSettings(proxy_port=0))
    mgr.create("ws/fs", preset="sandbox", overrides={"filesystem": "shared"}, node_secrets_ok=True)
    mgr.create("ws/svc", kind="service", preset="service", overrides={"filesystem": "shared"},
               service={"command": ["/bin/sh", "-c", "sleep 30"]}, node_secrets_ok=True)
    rt = Runtime(envs=mgr)
    specs = {s.name: s for s in tool_specs(rt)}
    try:
        for name, lane, kw in ((tool, "ws/fs", args), ("service_start", "ws/svc", {})):
            ctx = RecordingCtx(rt)
            with pytest.raises(ApprovalPending):
                await specs[name].handler(ctx, on=lane, **kw)
            assert "runs_on_node" in ctx.calls[0]["facts"], name
    finally:
        await mgr.shutdown()


def test_the_panel_terminal_sends_runs_on_node(tmp_path):
    from agentos.api.bridge import EnvsAdapter
    mgr = EnvManager(tmp_path / "home", host=FakeHost(), launcher=DirectLauncher(), settings=EnvSettings(proxy_port=0))
    mgr.create("ws/fs", preset="sandbox", overrides={"filesystem": "shared"}, node_secrets_ok=True)
    facts = EnvsAdapter(mgr).terminal_facts("ws/fs")
    assert facts["isolated"] is False and "runs_on_node" in facts
