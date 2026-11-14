"""env_create checks what it can refuse before it asks a person: a lane that exists, and a sandbox
this machine cannot run. Otherwise the approval is spent on a call that cannot succeed."""

from __future__ import annotations

import sys

import pytest
from test_policy_support import Clock, make_service

from agentos.core.errors import AgentosError, ApprovalPending
from agentos.core.models import Effect
from agentos.env.host import FakeHost
from agentos.env.manager import EnvManager
from agentos.env.paths import EnvSettings
from agentos.env.sandbox import DirectLauncher
from agentos.env.tools import env_create
from agentos.policy.grants import LaneCap, Specials


@pytest.fixture
def policy(tmp_path):
    svc, rt = make_service(tmp_path, clock=Clock())
    yield svc, rt
    svc.close()


def agent_ctx(svc):
    """An agent's ToolContext for env_create on biz-a lanes, built the way an MCP call builds one."""
    _g, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.IRREVERSIBLE,
                        specials=Specials(exec=LaneCap()))
    p, g = svc.resolve(tok)
    return svc.context(g, principal=p, tool="env_create", token=tok)


async def test_env_create_refuses_an_existing_lane_before_it_asks(policy, tmp_path):
    svc, rt = policy
    mgr = EnvManager(tmp_path / "env", runtime=rt, host=FakeHost(), launcher=DirectLauncher(),
                     settings=EnvSettings(proxy_port=0))
    rt.register("envs", mgr)
    mgr.create("biz-a/box")
    try:
        with pytest.raises(AgentosError) as e:
            await env_create(agent_ctx(svc), on="biz-a/box")
        assert not isinstance(e.value, ApprovalPending), "env_create asked a person about a lane that exists"
        assert e.value.code == "lane_exists"
        assert svc.approvals.list(state="pending") == []
    finally:
        await mgr.shutdown()


async def test_env_create_refuses_a_sandbox_this_machine_cannot_run_before_it_asks(policy, tmp_path, monkeypatch):
    svc, rt = policy
    monkeypatch.setattr(sys, "platform", "darwin")  # a Mac: default_mechanisms gives a NullHost, no bubblewrap
    mgr = EnvManager(tmp_path / "env", runtime=rt, settings=EnvSettings(proxy_port=0))
    rt.register("envs", mgr)
    try:
        with pytest.raises(AgentosError) as e:
            await env_create(agent_ctx(svc), on="biz-a/box", preset="sandbox")
        assert not isinstance(e.value, ApprovalPending), "env_create asked a person for a sandbox this machine cannot run"
        assert e.value.code == "no_sandbox"
        assert "workbench" in e.value.message and "node token" in e.value.message  # the real choice, in words
        assert svc.approvals.list(state="pending") == []
        assert not mgr.has("biz-a/box")
    finally:
        await mgr.shutdown()


async def test_the_check_never_names_a_lane_outside_the_grant(policy, tmp_path):
    svc, rt = policy
    mgr = EnvManager(tmp_path / "env", runtime=rt, host=FakeHost(), launcher=DirectLauncher(),
                     settings=EnvSettings(proxy_port=0))
    rt.register("envs", mgr)
    mgr.create("biz-b/box")
    try:
        with pytest.raises(AgentosError) as e:
            await env_create(agent_ctx(svc), on="biz-b/box")
        assert e.value.code != "lane_exists", e.value
    finally:
        await mgr.shutdown()


def hardened(mgr):
    """The node lost root after the lane was made (migrate-service-user): no sandbox any more."""
    from agentos.env.host import NullHost
    from agentos.env.manager import NOT_ROOT
    mgr.host = NullHost(NOT_ROOT.format(user="agentos"))


async def test_lane_exec_on_a_sandbox_this_node_cannot_run_fails_before_it_asks(policy, tmp_path):
    """QA 2026-11-10, tester B 11: the owner was asked to approve a command that could never run."""
    from agentos.env.tools import lane_exec, service_start
    svc, rt = policy
    mgr = EnvManager(tmp_path / "env", runtime=rt, host=FakeHost(), launcher=DirectLauncher(),
                     settings=EnvSettings(proxy_port=0))
    rt.register("envs", mgr)
    mgr.create("biz-a/box")
    mgr.create("biz-a/svc", kind="service", preset="service", service={"command": ["/bin/sh", "-c", "sleep 30"]})
    hardened(mgr)
    try:
        with pytest.raises(AgentosError) as e:
            await lane_exec(agent_ctx(svc), on="biz-a/box", cmd="id")
        assert e.value.code == "no_sandbox" and "Nothing ran" in e.value.message
        assert "runs it as agentos" in e.value.message and "workbench" in e.value.message
        with pytest.raises(AgentosError) as e:
            await service_start(agent_ctx(svc), on="biz-a/svc")
        assert e.value.code == "no_sandbox"
        assert svc.approvals.list(state="pending") == []
        status = mgr.service_status("biz-a/svc")  # finding 31: the reason, not detail: null
        assert status.state == "stopped" and "cannot start on this node" in status.detail
        assert "runs it as agentos" in status.detail
    finally:
        await mgr.shutdown()


def test_doctor_warns_while_a_lane_with_no_sandbox_exists(tmp_path):
    from agentos.env import exposure
    home = tmp_path / "env"
    assert exposure.doctor_rows(home) == []
    mgr = EnvManager(home, host=FakeHost(), launcher=DirectLauncher(), settings=EnvSettings(proxy_port=0))
    mgr.create("biz-a/box")
    assert [r["id"] for r in exposure.doctor_rows(home)] in ([], ["env.isolation"])
    mgr.create("biz-a/wb", preset="workbench", node_secrets_ok=True)
    rows = {r["id"]: r for r in exposure.doctor_rows(home)}
    wb = rows["env.workbench"]
    assert wb["status"] == "warn" and "biz-a/wb" in wb["detail"] and "node token" in wb["detail"]
    assert "agentos env remove biz-a/wb" in wb["fix"]
    assert "—" not in wb["detail"] + wb["fix"]


def test_a_workbench_lane_is_never_made_without_the_owners_yes(tmp_path):
    from agentos.core.errors import InvalidRequest
    mgr = EnvManager(tmp_path / "env", host=FakeHost(), launcher=DirectLauncher(), settings=EnvSettings(proxy_port=0))
    with pytest.raises(InvalidRequest, match="can-read-node-secrets"):
        mgr.create("biz-a/wb", preset="workbench")
    with pytest.raises(InvalidRequest, match="can-read-node-secrets"):  # the same lane by dials
        mgr.create("biz-a/wb", preset="sandbox", overrides={"network": "shared", "filesystem": "shared",
                                                          "processes": "shared"})
    assert not mgr.has("biz-a/wb")
    info = mgr.create("biz-a/wb", preset="workbench", node_secrets_ok=True)
    assert "as root" not in info.isolation or info.isolation.startswith("env0 (workbench): runs directly on the node as root")
    assert "node token" in info.isolation and "waits for a person" in info.isolation
