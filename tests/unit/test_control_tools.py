"""The agent's control tools (lane_control, lane_request_control, lane_wait_control) and the
take-over link for lane_view, driven through ToolSpec handlers with a fake ToolContext."""

from __future__ import annotations

import asyncio
import inspect
import typing

import pytest
from pydantic import ValidationError, create_model
from test_lanes import make

from agentos.core.errors import HumanInControl, InvalidRequest, PolicyDenied, ServiceUnavailable
from agentos.core.models import Effect, Principal
from agentos.core.runtime import Runtime
from agentos.node import control_tools
from agentos.node.control_tools import take_over_link, tool_specs
from agentos.node.tools import lane_view
from agentos.node.viewer_app import ViewTokens
from agentos.policy.ceiling import ControllerCeiling

LANE = "personal/reddit"
EM_DASH = "\u2014"


class FakeCtx:
    def __init__(self, runtime, principal_id: str = "claude-code", deny: bool = False):
        self.runtime = runtime
        self.principal = Principal(id=principal_id)
        self.authorized: list[tuple[Effect, str | None]] = []
        self.deny = deny

    async def authorize(self, effect, *, lane_id=None, **kw):
        self.authorized.append((effect, lane_id))
        if self.deny:
            raise PolicyDenied("not in your grant")

    def trace(self, kind, **fields):
        pass


@pytest.fixture
async def env(tmp_path):
    mgr, *_ = make(tmp_path)
    from agentos.core.models import LaneSpec

    await mgr.create(LaneSpec(id=LANE))
    runtime = Runtime(lanes=mgr, config=mgr.config)
    specs = {s.name: s for s in tool_specs(runtime)}
    yield mgr, runtime, specs
    await mgr.shutdown()


def call(specs, name, ctx, **kw):
    return specs[name].handler(ctx, **kw)


def test_specs_are_read_only_and_well_described(tmp_path):
    specs = tool_specs(Runtime())
    assert [s.name for s in specs] == ["lane_control", "lane_request_control", "lane_wait_control",
                                       "lane_login_request"]
    for spec in specs:
        assert spec.effect == Effect.READ and spec.lane_arg == "on" and "control" in spec.tags
        assert len(spec.description) > 80 and EM_DASH not in spec.description
        params = list(inspect.signature(spec.handler).parameters.values())
        assert params[0].name == "ctx"
        assert all(p.kind == p.KEYWORD_ONLY for p in params[1:])


def test_handler_signatures_make_a_valid_input_schema():
    """What the MCP wrapper will do: build a model from the keyword-only parameters."""
    for spec in tool_specs(Runtime()):
        hints = typing.get_type_hints(spec.handler, include_extras=True)
        params = inspect.signature(spec.handler).parameters
        fields = {name: (hints[name], ... if p.default is p.empty else p.default)
                  for name, p in params.items() if name != "ctx"}
        model = create_model(f"{spec.name}_args", **fields)
        schema = model.model_json_schema()
        assert "on" in schema["properties"] and "description" in schema["properties"]["on"]
        if spec.name == "lane_wait_control":
            assert schema["properties"]["timeout_s"]["maximum"] == 300
            with pytest.raises(ValidationError):
                model(on=LANE, timeout_s=301)
        if spec.name == "lane_request_control":
            assert schema["required"] == ["on", "reason"]


async def test_lane_control_reports_who_may_act(env):
    mgr, runtime, specs = env
    ctx = FakeCtx(runtime)
    out = await call(specs, "lane_control", ctx, on=LANE)
    assert out["on"] == LANE and out["state"] == "agent" and out["agent_may_act"] is True
    assert ctx.authorized == [(Effect.READ, LANE)]
    await mgr.take_control(LANE, by="owner")
    out = await call(specs, "lane_control", ctx, on=LANE)
    assert out["state"] == "human" and out["by"] == "owner" and out["agent_may_act"] is False
    assert out["since"].endswith("Z") and "lane_wait_control" in out["next"]
    await mgr.stop_agent(LANE, by="owner")
    out = await call(specs, "lane_control", ctx, on=LANE)
    assert out["state"] == "paused" and "do not ask again" in out["next"]


async def test_request_control_records_the_agent_and_its_reason(env):
    mgr, runtime, specs = env
    ctx = FakeCtx(runtime, principal_id="claude-code:T-91")
    out = await call(specs, "lane_request_control", ctx, on=LANE, reason="ready")
    assert out["state"] == "agent" and out["takeback_requested"] is False  # nothing to ask for
    await mgr.take_control(LANE, by="owner")
    out = await call(specs, "lane_request_control", ctx, on=LANE, reason="The login is done.")
    assert out["takeback_requested"] and out["takeback_by"] == "claude-code:T-91"
    assert out["takeback_reason"] == "The login is done." and "not answered" in out["next"]
    await mgr.deny_request(LANE, by="owner")
    with pytest.raises(HumanInControl, match="just refused"):
        await call(specs, "lane_request_control", ctx, on=LANE, reason="please")
    with pytest.raises(InvalidRequest):
        await call(specs, "lane_request_control", ctx, on="Not A Lane", reason="x")


async def test_login_request_only_notifies_the_owner_and_never_starts_a_login(env):
    mgr, runtime, specs = env
    pushes: list[dict] = []
    published: list[tuple[str, dict]] = []

    class Notifier:
        def send(self, event):
            pushes.append(event)

    class Bus:
        def publish(self, kind, data):
            published.append((kind, data))

    runtime.register("notifier", Notifier())
    runtime.register("events", Bus())
    ctx = FakeCtx(runtime)
    out = await call(specs, "lane_login_request", ctx, on=LANE, why="Reddit signed this lane out — I need it.")
    assert out["asked"] is True and "lane_wait_control" in out["next"] and EM_DASH not in out["next"]
    assert ctx.authorized == [(Effect.READ, LANE)]
    assert pushes[0]["type"] == "login.requested" and pushes[0]["lane"] == LANE
    assert "Reddit signed this lane out, I need it." in pushes[0]["body"] and EM_DASH not in pushes[0]["body"]
    assert published == [("login", {"lane": LANE, "state": "requested", "version": 0, "step": None})]
    assert mgr.get(LANE).control.state == "agent"  # nothing was taken or started
    again = await call(specs, "lane_login_request", ctx, on=LANE, why="Please log in.")
    assert again["asked"] is False and again["already_asked"] and len(pushes) == 1
    assert runtime.get("login_assist").get(LANE)["requested"]["why"] == "Reddit signed this lane out, I need it."
    with pytest.raises(InvalidRequest):
        await call(specs, "lane_login_request", ctx, on=LANE, why="   ")


async def test_policy_runs_before_anything_happens(env):
    mgr, runtime, specs = env
    await mgr.take_control(LANE, by="owner")
    ctx = FakeCtx(runtime, deny=True)
    with pytest.raises(PolicyDenied):
        await call(specs, "lane_request_control", ctx, on=LANE, reason="please")
    assert not mgr.get(LANE).control.takeback_requested
    for name in ("lane_control", "lane_wait_control"):
        with pytest.raises(PolicyDenied):
            await call(specs, name, ctx, on=LANE)


async def test_wait_control_returns_on_hand_back(env):
    mgr, runtime, specs = env
    ctx = FakeCtx(runtime)
    out = await call(specs, "lane_wait_control", ctx, on=LANE, timeout_s=5)
    assert out["agent_may_act"] and not out["timed_out"] and out["waited_s"] < 1
    await mgr.take_control(LANE, by="owner")

    async def hand_back_soon():
        await asyncio.sleep(0.05)
        await mgr.hand_back(LANE, by="owner")

    asyncio.create_task(hand_back_soon())
    out = await call(specs, "lane_wait_control", ctx, on=LANE, timeout_s=5)
    assert out["agent_may_act"] and out["state"] == "agent" and 0 < out["waited_s"] < 1


async def test_wait_control_times_out_and_clamps(env, monkeypatch):
    mgr, runtime, specs = env
    ctx = FakeCtx(runtime)
    await mgr.stop_agent(LANE, by="owner")
    out = await call(specs, "lane_wait_control", ctx, on=LANE, timeout_s=0.05)
    assert out["timed_out"] and out["state"] == "paused"
    asked: list[float] = []
    original = mgr.wait_for_agent_control

    async def spy(lane_id, timeout):
        asked.append(timeout)
        return await original(lane_id, 0)

    monkeypatch.setattr(mgr, "wait_for_agent_control", spy)
    for given in (10_000, -3, 0, "12"):
        await call(specs, "lane_wait_control", ctx, on=LANE, timeout_s=given)
    assert asked == [300.0, 0.0, 0.0, 12.0]
    for bad in (float("nan"), "soon", None):
        with pytest.raises(InvalidRequest):
            await call(specs, "lane_wait_control", ctx, on=LANE, timeout_s=bad)


async def test_tools_need_the_lanes_service():
    specs = {s.name: s for s in tool_specs(Runtime())}
    with pytest.raises(ServiceUnavailable):
        await call(specs, "lane_control", FakeCtx(Runtime()), on=LANE)


def test_take_over_link(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTOS_SSH_HOST", raising=False)
    mgr, *_ = make(tmp_path, mcp_port=7441, ssh_host="mynode")
    runtime = Runtime(lanes=mgr, config=mgr.config)
    link = take_over_link(runtime, LANE)
    assert link["mode"] == "control" and link["url"].startswith("http://localhost:7441/view/personal/reddit?t=")
    assert link["tunnel"] == "ssh -N -L 7441:localhost:7441 mynode"
    token = link["url"].split("?t=", 1)[1]
    assert ViewTokens.for_config(mgr.config).verify(token, LANE).can_control
    assert EM_DASH not in link["take_over"]
    monkeypatch.setenv("AGENTOS_SSH_HOST", "tunnelhost")
    assert take_over_link(runtime, LANE)["tunnel"].endswith(" tunnelhost")
    assert take_over_link(runtime, LANE, ssh_host="x", port=9000, base="/node")["url"].startswith(
        "http://localhost:9000/node/view/personal/reddit?t=")
    with pytest.raises(InvalidRequest):
        take_over_link(runtime, "nope")


def test_lane_view_links_follow_the_route_connect_recorded_for_the_grant(tmp_path, monkeypatch):
    """QA 2026-11-10 finding 9: on a second node the link named this node's port and hostname."""
    from types import SimpleNamespace

    from agentos.node.tools import _caller_route
    from agentos.ops import viewroutes

    monkeypatch.delenv("AGENTOS_SSH_HOST", raising=False)
    mgr, *_ = make(tmp_path, mcp_port=7400, ssh_host="ubuntu-8gb-fsn1-2")
    runtime = Runtime(lanes=mgr, config=mgr.config)
    ctx = FakeCtx(runtime)
    ctx.grant = SimpleNamespace(chain_ids=["gr_root", "gr_child"])
    assert _caller_route(ctx) is None  # nothing recorded: today's words
    viewroutes.save(mgr.config.home, "gr_root", "vm2", 7401)
    route = _caller_route(ctx)
    assert route == {"ssh_host": "vm2", "local_port": 7401}
    link = take_over_link(runtime, LANE, **route)
    assert link["url"].startswith("http://localhost:7401/view/personal/reddit?t=")
    assert link["tunnel"] == "ssh -N -L 7401:localhost:7400 vm2"
    viewroutes.save(mgr.config.home, "gr_child", "vm2-alias", 7409)  # the nearest grant wins
    assert _caller_route(ctx) == {"ssh_host": "vm2-alias", "local_port": 7409}
    with pytest.raises(ValueError):
        viewroutes.save(mgr.config.home, "gr_x", "-oProxyCommand=x", 7401)


def test_take_over_link_uses_the_registered_signer(tmp_path):
    mgr, *_ = make(tmp_path)
    signer = ViewTokens(tmp_path / "other.key", default_ttl=60)
    runtime = Runtime(lanes=mgr, config=mgr.config, viewer_links=signer)
    token = take_over_link(runtime, LANE, ssh_host="x")["url"].split("?t=", 1)[1]
    assert signer.verify(token, LANE).can_control


class _ViewLanes:
    """A warm lane with take over, for lane_view."""

    def __init__(self, mgr):
        self.mgr = mgr

    def get(self, lane_id):
        return self.mgr.get(lane_id)

    def view(self, lane_id):
        return object()

    async def take_control(self, lane_id, **kw):
        raise AssertionError("lane_view never takes a lane")


class _Grant:
    def __init__(self, limit):
        self.limit = limit


@pytest.mark.parametrize(("limit", "control"), [
    (None, True),  # the node's own grant
    (ControllerCeiling(), False),  # the hosted default: watch only
    (ControllerCeiling.full(), True),
])
async def test_lane_view_gives_a_controller_a_control_link_only_within_its_ceiling(env, limit, control):
    mgr, *_ = env
    runtime = Runtime(lanes=_ViewLanes(mgr), config=mgr.config)
    ctx = FakeCtx(runtime)
    ctx.grant = _Grant(limit)
    out = await lane_view(ctx, on=LANE)
    token = out["viewer"]["url"].split("?t=", 1)[1]
    assert ViewTokens.for_config(mgr.config).verify(token, LANE).can_control is control
    assert out["viewer"]["mode"] == ("control" if control else "view")
    assert EM_DASH not in out["viewer"]["take_over"]


def test_messages_have_no_em_dash():
    texts = list(control_tools.NEXT.values()) + [control_tools.NEXT_REQUESTED]
    assert not any(EM_DASH in t for t in texts)
