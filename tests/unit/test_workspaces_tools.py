"""Workspace MCP tools: names and effects, grant filtering, the approval flow of apply, and readiness."""

from __future__ import annotations

import inspect
import typing

import pytest
from pydantic import TypeAdapter

from agentos.core.errors import (
    ApprovalPending,
    InvalidRequest,
    LaneNotFound,
    PolicyDenied,
    ServiceUnavailable,
)
from agentos.core.models import Effect
from agentos.core.runtime import Runtime
from agentos.core.tools import TOOL_NAME_RE
from agentos.workspaces.errors import WorkspaceConflict
from agentos.workspaces.fakes import FakeCtx, Harness
from agentos.workspaces.tools import tool_specs

SOCIAL = ["instagram", "facebook", "reddit", "medium", "blog", "linkedin"]


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path / "home")
    yield harness
    harness.close()


def spec(h_or_runtime, name):
    runtime = h_or_runtime.runtime if isinstance(h_or_runtime, Harness) else h_or_runtime
    return next(s for s in tool_specs(runtime) if s.name == name)


class PendingCtx(FakeCtx):
    """Like FakeCtx, but the owner has not approved yet."""

    async def authorize(self, effect, **kwargs):
        await super().authorize(effect, **kwargs)
        raise ApprovalPending("waiting for the owner to approve: test", approval_id="ap_7f3")


class DeniedCtx(FakeCtx):
    async def authorize(self, effect, **kwargs):
        raise PolicyDenied("the grant does not allow commit")


# ---- the specs ------------------------------------------------------------------

def test_the_five_tools_have_the_right_effects_and_names(h):
    specs = {s.name: s for s in tool_specs(h.runtime)}
    assert set(specs) == {"workspace_list", "workspace_queue", "workspace_plan", "workspace_apply",
                          "lane_set_readiness"}
    assert specs["workspace_list"].effect == Effect.READ and specs["workspace_queue"].effect == Effect.READ
    assert specs["workspace_plan"].effect == Effect.READ
    assert specs["workspace_apply"].effect == Effect.COMMIT
    assert specs["lane_set_readiness"].effect == Effect.DRAFT and specs["lane_set_readiness"].lane_arg == "on"
    assert all(TOOL_NAME_RE.match(name) for name in specs)
    assert all(s.lane_arg is None for name, s in specs.items() if name != "lane_set_readiness")
    assert all(s.hidden_without == "workspaces" and s.tags == ("workspaces",) for s in specs.values())


def test_descriptions_are_written_for_agents_and_have_no_em_dashes(h):
    for s in tool_specs(h.runtime):
        assert s.description and len(s.description) > 80, s.name
        assert "\u2014" not in s.description, s.name
    assert "approves first" in spec(h, "workspace_apply").description
    assert "Do not mark a lane ready on a guess" in spec(h, "lane_set_readiness").description


def test_every_parameter_has_a_schema_the_mcp_layer_can_derive(h):
    for s in tool_specs(h.runtime):
        hints = typing.get_type_hints(s.handler, include_extras=True)
        sig = inspect.signature(s.handler)
        assert next(iter(sig.parameters)) == "ctx"
        for name, param in sig.parameters.items():
            if name == "ctx":
                continue
            assert param.kind is inspect.Parameter.KEYWORD_ONLY, (s.name, name)
            TypeAdapter(hints[name]).json_schema()


def test_every_tool_names_the_service_it_needs():
    bare = Runtime()
    specs = tool_specs(bare)
    assert specs and all(s.hidden_without == "workspaces" for s in specs)


async def test_calling_a_tool_without_the_service_is_a_clear_error():
    ctx = FakeCtx(Runtime())
    with pytest.raises(ServiceUnavailable, match="workspaces is not configured on this node"):
        await spec(Runtime(), "workspace_list").handler(ctx)


# ---- reading ---------------------------------------------------------------------

async def test_workspace_list_and_queue_leave_out_lanes_outside_the_grant(h):
    h.lanes.add("acme/instagram", proxy="lt1")
    h.lanes.add("beta/instagram", proxy="lt2")
    h.service.create_workspace("acme")
    h.service.create_workspace("beta")
    ctx = FakeCtx(h.runtime, allowed=lambda lane_id: lane_id.startswith("acme/"))
    listed = await spec(h, "workspace_list").handler(ctx)
    assert [ws["id"] for ws in listed["workspaces"]] == ["acme"]
    assert [lane["id"] for lane in listed["workspaces"][0]["lanes"]] == ["acme/instagram"]
    queue = await spec(h, "workspace_queue").handler(ctx)
    assert [lane["id"] for lane in queue["lanes"]] == ["acme/instagram"] and queue["total"] == 1


async def test_workspace_plan_reads_only_and_checks_the_grant_per_lane(h):
    h.service.create_workspace("acme")
    h.service.create_workspace("beta")
    ctx = FakeCtx(h.runtime, allowed=lambda lane_id: lane_id.startswith("acme/"))
    plan = await spec(h, "workspace_plan").handler(ctx, template="social-6", workspaces=["acme"])
    assert plan["ok"] and plan["counts"]["create"] == 6
    assert ctx.authorized == [] and h.lanes.created == []
    with pytest.raises(PolicyDenied, match="this grant does not cover: beta/blog, beta/facebook"):
        await spec(h, "workspace_plan").handler(ctx, template="social-6", workspaces=["beta"])


# ---- apply -------------------------------------------------------------------------

async def test_workspace_apply_asks_for_commit_with_a_node_written_summary_then_creates(h):
    h.service.create_workspace("acme")
    ctx = FakeCtx(h.runtime)
    result = await spec(h, "workspace_apply").handler(ctx, template="social-6", workspaces=["acme"],
                                                      proxy_per_workspace={"acme": "lt1"})
    assert result["status"] == "complete" and result["created"] == [f"acme/{n}" for n in SOCIAL]
    auth = ctx.authorized[0]
    assert auth["effect"] == Effect.COMMIT and auth["lane_id"] is None
    assert auth["summary"].startswith("Create 6 lanes from template social-6: acme/instagram (proxy lt1)")
    assert auth["content"]["template"] == "social-6" and auth["content"]["override_proxy_rules"] is False
    assert ctx.traces[0]["kind"] == "workspace_apply"
    assert h.lanes.created == [f"acme/{n}" for n in SOCIAL]


async def test_a_conflict_is_refused_before_anyone_is_asked_to_approve(h):
    h.lanes.add("beta/instagram", proxy="lt1")
    h.service.create_workspace("acme")
    ctx = FakeCtx(h.runtime)
    with pytest.raises(WorkspaceConflict, match="6 conflicts, nothing was created: acme/instagram"):
        await spec(h, "workspace_apply").handler(ctx, template="social-6", workspaces=["acme"],
                                                 proxy_per_workspace={"acme": "lt1"})
    assert ctx.authorized == [] and h.lanes.created == []


async def test_apply_with_nothing_to_create_needs_no_approval(h):
    h.service.create_workspace("acme")
    await h.service.apply("social-6", ["acme"], by="owner", proxy_per_workspace={"acme": "lt1"})
    ctx = FakeCtx(h.runtime)
    again = await spec(h, "workspace_apply").handler(ctx, template="social-6", workspaces=["acme"],
                                                     proxy_per_workspace={"acme": "lt1"})
    assert again["status"] == "complete" and again["created"] == [] and len(again["existing"]) == 6
    assert ctx.authorized == []


async def test_a_pending_approval_propagates_and_creates_nothing(h):
    h.service.create_workspace("acme")
    with pytest.raises(ApprovalPending) as info:
        await spec(h, "workspace_apply").handler(PendingCtx(h.runtime), template="social-6",
                                                 workspaces=["acme"], proxy_per_workspace={"acme": "lt1"})
    assert info.value.approval_id == "ap_7f3"
    assert h.lanes.created == []


async def test_a_denied_commit_creates_nothing(h):
    h.service.create_workspace("acme")
    with pytest.raises(PolicyDenied):
        await spec(h, "workspace_apply").handler(DeniedCtx(h.runtime), template="social-6",
                                                 workspaces=["acme"], proxy_per_workspace={"acme": "lt1"})
    assert h.lanes.created == []


async def test_apply_outside_the_grant_is_denied_before_the_approval(h):
    h.service.create_workspace("beta")
    ctx = FakeCtx(h.runtime, allowed=lambda lane_id: lane_id.startswith("acme/"))
    with pytest.raises(PolicyDenied, match="this grant does not cover: beta/blog"):
        await spec(h, "workspace_apply").handler(ctx, template="social-6", workspaces=["beta"],
                                                 proxy_per_workspace={"beta": "lt1"})
    assert ctx.authorized == []


async def test_an_override_says_so_in_the_approval_and_is_recorded(h):
    h.service.create_workspace("acme")
    h.service.create_workspace("beta")
    await h.service.apply("social-6", ["acme"], by="owner", proxy_per_workspace={"acme": "lt1"})
    ctx = FakeCtx(h.runtime)
    await spec(h, "workspace_apply").handler(ctx, template="social-6", workspaces=["beta"],
                                             proxy_per_workspace={"beta": "lt1"},
                                             override_proxy_rules=True, reason="same owner, two brands")
    assert "OVERRIDES the proxy rules for 6 lanes" in ctx.authorized[0]["summary"]
    assert "same owner, two brands" not in ctx.authorized[0]["summary"]  # the reason is hashed, not shown
    assert h.service.overrides()[0].reason == "same owner, two brands"


# ---- readiness ------------------------------------------------------------------------

async def test_lane_set_readiness_is_a_draft_on_the_lane(h):
    h.lanes.add("acme/instagram")
    ctx = FakeCtx(h.runtime)
    out = await spec(h, "lane_set_readiness").handler(ctx, on="acme/instagram", state="ready")
    assert out["readiness"] == "ready" and out["updated_by"] == "claude-code"
    auth = ctx.authorized[0]
    assert auth["effect"] == Effect.DRAFT and auth["lane_id"] == "acme/instagram"
    assert auth["summary"] == "Set acme/instagram to ready"


async def test_lane_set_readiness_checks_the_lane_id_and_the_lane(h):
    ctx = FakeCtx(h.runtime)
    with pytest.raises(InvalidRequest, match="lane id must look like"):
        await spec(h, "lane_set_readiness").handler(ctx, on="Acme", state="ready")
    assert ctx.authorized == []
    with pytest.raises(LaneNotFound):
        await spec(h, "lane_set_readiness").handler(ctx, on="acme/ghost", state="ready")


async def test_needs_attention_through_the_tool_needs_a_note(h):
    h.lanes.add("acme/reddit")
    with pytest.raises(InvalidRequest, match="needs_attention needs a note"):
        await spec(h, "lane_set_readiness").handler(FakeCtx(h.runtime), on="acme/reddit", state="needs_attention")
    out = await spec(h, "lane_set_readiness").handler(FakeCtx(h.runtime), on="acme/reddit",
                                                      state="needs_attention", note="a phone code was asked")
    assert out["readiness"] == "needs_attention" and out["note"] == "a phone code was asked"
