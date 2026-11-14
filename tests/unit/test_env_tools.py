import inspect

import pytest

from agentos.core.errors import PolicyDenied
from agentos.core.models import Effect, Principal
from agentos.core.runtime import Runtime
from agentos.core.tools import TOOL_NAME_RE
from agentos.env import tool_specs
from agentos.env.host import FakeHost
from agentos.env.manager import EnvManager
from agentos.env.paths import EnvSettings
from agentos.env.sandbox import DirectLauncher


class Ctx:
    def __init__(self, runtime, deny: set[Effect] | None = None):
        self.runtime = runtime
        self.principal = Principal(id="test-agent")
        self.calls: list[dict] = []
        self.traces: list[tuple] = []
        self.deny = deny or set()

    async def authorize(self, effect, *, lane_id=None, domain=None, summary=None, content=None, facts=None):
        self.calls.append({"effect": effect, "lane_id": lane_id, "summary": summary, "content": content,
                           "facts": facts})
        if effect in self.deny:
            raise PolicyDenied(f"{effect} denied")

    def trace(self, kind, **fields):
        self.traces.append((kind, fields))


@pytest.fixture
async def rt(tmp_path):
    mgr = EnvManager(tmp_path / "home", host=FakeHost(), launcher=DirectLauncher(),
                     settings=EnvSettings(proxy_port=0))
    mgr.create("ws/bench", preset="workbench", node_secrets_ok=True)
    mgr.create("ws/svc", kind="service", preset="service", service={"command": ["/bin/sh", "-c", "sleep 30"]})
    runtime = Runtime(envs=mgr)
    yield runtime
    await mgr.shutdown()


def specs(runtime):
    return {s.name: s for s in tool_specs(runtime)}


def test_specs_are_well_formed(rt):
    for s in tool_specs(rt):
        assert TOOL_NAME_RE.match(s.name) and s.description and "\u2014" not in s.description
        params = list(inspect.signature(s.handler).parameters.values())
        assert params[0].name == "ctx" and all(p.kind == p.KEYWORD_ONLY for p in params[1:])
        assert s.hidden_without == "envs"


async def test_exec_authorizes_with_the_exact_command(rt):
    ctx = Ctx(rt)
    out = await specs(rt)["lane_exec"].handler(ctx, on="ws/bench", cmd="echo $((40+2))")
    assert out["stdout"] == "42\n"
    call = ctx.calls[0]
    assert call["effect"] == Effect.EXEC and call["lane_id"] == "ws/bench"
    assert "echo $((40+2))" in call["summary"] and "no isolation" in call["summary"]
    assert call["content"]["cmd"] == "echo $((40+2))"
    assert call["facts"]["isolated"] is False  # a workbench lane: a person every time, never standing
    assert "node token" in call["facts"]["runs_on_node"] and "ws/bench" in call["facts"]["runs_on_node"]


async def test_exec_tells_the_policy_an_isolated_lane_is_one(rt):
    rt.envs.create("ws/sb", preset="sandbox")
    ctx = Ctx(rt)
    await specs(rt)["lane_exec"].handler(ctx, on="ws/sb", cmd="true")
    await specs(rt)["env_create"].handler(ctx, on="ws/sb2")
    assert [c["facts"] for c in ctx.calls] == [{"isolated": True}, {"isolated": True}]


async def test_denied_exec_runs_nothing(rt, tmp_path):
    ctx = Ctx(rt, deny={Effect.EXEC})
    marker = tmp_path / "ran"
    with pytest.raises(PolicyDenied):
        await specs(rt)["lane_exec"].handler(ctx, on="ws/bench", cmd=f"touch {marker}")
    assert not marker.exists()


async def test_install_effect_depends_on_shared_filesystem(rt):
    ctx = Ctx(rt, deny={Effect.INSTALL_BASE, Effect.INSTALL_OVERLAY})
    with pytest.raises(PolicyDenied):
        await specs(rt)["lane_install"].handler(ctx, on="ws/bench", packages=["jq"], source="apt")
    assert ctx.calls[0]["effect"] == Effect.INSTALL_BASE and "onto the node itself" in ctx.calls[0]["summary"]
    rt.envs.create("ws/sb")
    with pytest.raises(PolicyDenied):
        await specs(rt)["lane_install"].handler(ctx, on="ws/sb", packages=["requests==2.32.3"], source="pip")
    call = ctx.calls[1]
    assert call["effect"] == Effect.INSTALL_OVERLAY and "requests==2.32.3" in call["summary"]
    assert "pypi.org" in call["summary"] and call["content"] == {"source": "pip", "packages": ["requests==2.32.3"]}


async def test_shell_tools_roundtrip(rt):
    ctx = Ctx(rt)
    sp = specs(rt)
    opened = await sp["shell_open"].handler(ctx, on="ws/bench")
    sid = opened["session"]
    await sp["shell_write"].handler(ctx, session=sid, text="echo tool-$((2*21))\n")
    text = ""
    for _ in range(20):
        r = await sp["shell_read"].handler(ctx, session=sid, wait_ms=500)
        text += r["output"]
        if "tool-42" in text:
            break
    assert "tool-42" in text
    assert [c["effect"] for c in ctx.calls[:3]] == [Effect.EXEC, Effect.EXEC, Effect.READ]
    assert "echo tool-$((2*21))" in ctx.calls[1]["summary"]
    assert (await sp["shell_close"].handler(ctx, session=sid))["closed"]


async def test_service_tools_use_the_service_effect(rt):
    ctx = Ctx(rt)
    sp = specs(rt)
    st = await sp["service_start"].handler(ctx, on="ws/svc", ttl_s=60)
    assert st["desired"] == "running"
    assert ctx.calls[0]["effect"] == Effect.SERVICE and "sleep 30" in ctx.calls[0]["summary"]
    assert "stops after 60s" in ctx.calls[0]["summary"]
    assert (await sp["service_status"].handler(ctx, on="ws/svc"))["lane"] == "ws/svc"
    assert "log" in await sp["service_logs"].handler(ctx, on="ws/svc")
    st = await sp["service_stop"].handler(ctx, on="ws/svc")
    assert st["state"] == "stopped"


async def test_lane_listing_create_remove_and_images(rt):
    ctx = Ctx(rt)
    sp = specs(rt)
    lanes = await sp["env_lanes_list"].handler(ctx)
    assert {lane["id"] for lane in lanes} == {"ws/bench", "ws/svc"}
    made = await sp["env_create"].handler(ctx, on="ws/tmp", network="isolated", allow_hosts=["example.com"])
    assert made["egress_allowlist"] == ["example.com"] and made["preset"] == "sandbox"
    with pytest.raises(PolicyDenied):
        await sp["env_create"].handler(ctx, on="ws/wb", preset="workbench")
    await sp["env_remove"].handler(ctx, on="ws/tmp")
    assert ctx.calls[-1]["effect"] == Effect.IRREVERSIBLE
    imgs = await sp["images_list"].handler(ctx)
    assert {t["name"] for t in imgs["templates"]} >= {"dev-box", "render", "brain", "browser"}
