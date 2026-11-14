"""Scope gaps in the tools: a grant sees a workflow, a list, a flow or a workspace only where it reaches all
of it (or the lane it is bound to), and a refused authorization saves nothing. The grants are real Grant
objects, so the grant path is what runs. Only the ToolContext around them is a small fake."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from test_node_panel import Node, bearer
from testkit.workspaces import Harness as WorkspaceHarness
from workflow_support import LIST, OWNER, action, make_node, read_flow, workflow

from agentos.core.errors import InvalidRequest, NotFound, PolicyDenied
from agentos.core.models import Effect
from agentos.core.runtime import Runtime
from agentos.policy.grants import Grant, GrantBlock, Subject
from agentos.runner import tools as runner_tools
from agentos.runner.fakes import BASE, Harness
from agentos.runner.workflows import tools as wf_tools
from agentos.workspaces import tools as ws_tools
from agentos.workspaces.rules import LaneFact, proxy_conflicts

OWNER_P = {"kind": "human", "id": "owner", "owner": "owner"}
READ = "biz-a.social.read"
ADDRESS = "ana@example.com"
LEAD = {"email": "new@acme.example", "source": "https://www.linkedin.com/in/someone"}


def grant_for(*lanes: str) -> Grant:
    """A real Grant (an authority block, unsigned here) whose lane scope is these patterns."""
    block = GrantBlock(v=2, id="gr_scopegap1", iat=0, iss="node:test", aud=["node:test"],
                       kid="ed25519:" + "0" * 32, sub=Subject(kind="agent", id="agent-1"),
                       lanes=list(lanes), ceiling=Effect.COMMIT, exp=4_102_444_800)
    return Grant([block])


class GrantCtx:
    """A ToolContext for a call made under `grant`. Scope answers come from the grant, precheck refuses a
    lane outside it, and authorize records each call and refuses when `deny` is set."""

    def __init__(self, runtime: Any, grant: Grant, *, deny: str | None = None):
        self.runtime = runtime
        self.grant = grant
        self.principal = grant.principal()
        self.deny = deny
        self.authorized: list[dict[str, Any]] = []
        self.traces: list[dict[str, Any]] = []

    def can_access_lane(self, lane_id: str) -> bool:
        return self.grant.lane_allowed(lane_id)

    def precheck(self, effect: Any, *, lane_id: str | None = None) -> None:
        if lane_id is not None and not self.grant.lane_allowed(lane_id):
            raise PolicyDenied(f"your grant does not cover lane {lane_id}")

    async def authorize(self, effect: Any, *, lane_id: str | None = None, domain: str | None = None,
                        summary: str | None = None, content: Any = None, **_: Any) -> None:
        self.authorized.append({"effect": effect, "lane_id": lane_id, "summary": summary, "content": content})
        if self.deny is not None:
            raise PolicyDenied(self.deny)

    def trace(self, kind: str, **fields: Any) -> None:
        self.traces.append({"kind": kind, **fields})


def read_def(flow_id: str, lane: str) -> dict[str, Any]:
    """A flow that only reads, bound to `lane`."""
    return {"id": flow_id, "lane": lane, "steps": [{"type": "navigate", "url": f"{BASE}/"}]}


@pytest.fixture
def node(tmp_path):
    n = make_node(tmp_path)
    n.wf.sends.timezone = lambda: "UTC"
    n.h.clock.set(datetime(2026, 11, 4, 10, 0, tzinfo=UTC))
    return n


@pytest.fixture
def ws(tmp_path):
    harness = WorkspaceHarness(tmp_path / "home")
    yield harness
    harness.close()


@pytest.fixture
def api_node(tmp_path):
    n = Node(tmp_path)
    yield n
    n.close()


# ---- core helpers ----

def test_core_helpers_answer_from_the_grant_lane_patterns():
    """A grant for biz-a/* covers biz-a and nothing else; one lane is not its workspace."""
    from agentos.core.tools import covers_workspace, pattern_reaches, pattern_within, require_lane_patterns

    ctx = GrantCtx(Runtime(), grant_for("biz-a/*"))
    assert covers_workspace(ctx, "biz-a") is True
    assert covers_workspace(ctx, "biz-b") is False
    assert covers_workspace(ctx, "biz-a/*") is False
    assert covers_workspace(GrantCtx(Runtime(), grant_for("biz-a/0")), "biz-a") is False
    assert pattern_within(ctx, "biz-a/insta*") is True
    assert pattern_within(ctx, "biz-a/*") is True
    assert pattern_within(ctx, "*") is False
    assert pattern_within(ctx, "biz-b/*") is False
    assert pattern_reaches(ctx, "*/instagram") is True
    assert pattern_reaches(ctx, "biz-b/*") is False
    with pytest.raises(PolicyDenied):
        require_lane_patterns(ctx, ["*"], Effect.DRAFT)
    require_lane_patterns(ctx, ["biz-a/insta*"], Effect.DRAFT)


# ---- tools that ask before they act ----

async def test_a_refused_authorization_saves_no_suppression_batch_or_proposal(node):
    """send_suppress, leads_propose and workflow_propose ask ctx.authorize first, so a refusal saves nothing."""
    node.h.save(read_flow(), by=OWNER_P)
    wid = node.wf.save(workflow(action("look", READ), trigger={"kind": "webhook"}), OWNER)["id"]
    grant, _ = node.svc.issue(subject={"id": "agent-1"}, lanes=["biz-a/*"], ceiling=Effect.COMMIT)
    refused = GrantCtx(node.h.runtime, grant, deny="the owner has not allowed this")
    with pytest.raises(PolicyDenied):
        await wf_tools.send_suppress(refused, workspace="biz-a", email=ADDRESS)
    with pytest.raises(PolicyDenied):
        await wf_tools.leads_propose(refused, list=LIST, leads=[LEAD])
    with pytest.raises(PolicyDenied):
        await wf_tools.workflow_propose(refused, id=wid)
    assert node.wf.store.is_suppressed("biz-a", ADDRESS) is False
    assert node.wf.store.lead_batches() == []
    assert node.wf.store.state(wid).proposal is None

    allowed = GrantCtx(node.h.runtime, grant)
    await wf_tools.send_suppress(allowed, workspace="biz-a", email=ADDRESS)
    assert node.wf.store.is_suppressed("biz-a", ADDRESS) is True
    assert (await wf_tools.leads_propose(allowed, list=LIST, leads=[LEAD]))["received"] == 1
    assert (await wf_tools.workflow_propose(allowed, id=wid))["status"] == "pending"
    assert node.wf.store.state(wid).proposal is not None


async def test_workflow_save_keeps_a_workflow_outside_the_grant_as_it_was(node):
    """A save over a workflow in a workspace the grant does not cover is refused, and so is one placed there."""
    node.wf.save({"id": "biz-b.routine.job", "name": "Job", "workspace": "biz-b",
                  "trigger": {"kind": "manual"}, "steps": []}, OWNER)
    grant, _ = node.svc.issue(subject={"id": "agent-1"}, lanes=["biz-a/*"], ceiling=Effect.COMMIT)
    ctx = GrantCtx(node.h.runtime, grant)
    head = node.wf.get_version("biz-b.routine.job")
    with pytest.raises(PolicyDenied):
        await wf_tools.workflow_save(ctx, definition={"id": "biz-b.routine.job", "name": "Job", "workspace": "biz-a",
                                                      "trigger": {"kind": "manual"}, "steps": []})
    now = node.wf.get_version("biz-b.routine.job")
    assert now.version == head.version and now.definition.workspace == "biz-b"
    with pytest.raises(PolicyDenied):
        await wf_tools.workflow_save(ctx, definition={"id": "biz-b.routine.fresh", "name": "Fresh",
                                                      "workspace": "biz-b", "trigger": {"kind": "manual"},
                                                      "steps": []})
    assert node.wf.store.state("biz-b.routine.fresh") is None


# ---- flows ----

async def test_flows_list_leaves_out_flows_bound_outside_the_grant(tmp_path):
    """A flow bound to biz-b, or to the pattern biz-b/*, is hidden; a pattern that can match biz-a shows."""
    h = Harness(tmp_path)
    for flow_id, lane in [("biz-a.social.x", "biz-a/x"), ("biz-b.social.x", "biz-b/x"),
                          ("biz-b.pattern.all", "biz-b/*"), ("any.pattern.x", "*/x")]:
        h.save(read_def(flow_id, lane))
    out = await runner_tools.flows_list(GrantCtx(h.runtime, grant_for("biz-a/*")))
    assert {f["id"] for f in out["flows"]} == {"biz-a.social.x", "any.pattern.x"}
    assert out["total"] == 2
    assert out["namespaces"] == [{"prefix": "any", "flows": 1}, {"prefix": "biz-a", "flows": 1}]


async def test_flow_save_refuses_lanes_and_replacements_outside_the_grant(tmp_path):
    """A pattern outside the grant is refused, one inside it saves, and a new version of a flow bound
    outside the grant is refused while the stored head keeps its lane."""
    h = Harness(tmp_path)
    ctx = GrantCtx(h.runtime, grant_for("biz-a/*"))
    for lane in ("biz-b/*", "*/*"):
        with pytest.raises(PolicyDenied):
            await runner_tools.flow_save(ctx, definition=read_def("ops.social.any", lane))
    with pytest.raises((InvalidRequest, PolicyDenied)):  # the flow model refuses a bare "*" as a lane
        await runner_tools.flow_save(ctx, definition=read_def("ops.social.bare", "*"))
    assert h.flows.store.get_flow("ops.social.any") is None

    saved = await runner_tools.flow_save(ctx, definition=read_def("ops.social.insta", "biz-a/insta*"))
    assert saved["created"] and saved["lane"] == "biz-a/insta*"

    h.save(read_def("ops.social.post", "biz-b/x"))
    with pytest.raises(PolicyDenied):
        await runner_tools.flow_save(ctx, definition=read_def("ops.social.post", "biz-a/x"))
    assert h.flows.get("ops.social.post").definition.lane == "biz-b/x"


async def test_flow_run_says_not_found_for_a_flow_bound_outside_the_grant(tmp_path):
    """The refusal is the same not_found a missing flow gets, and it does not name the lane."""
    h = Harness(tmp_path)
    h.save(read_def("ops.social.post", "biz-b/x"))
    ctx = GrantCtx(h.runtime, grant_for("biz-a/*"))
    with pytest.raises(NotFound) as refused:
        await runner_tools.flow_run(ctx, name="ops.social.post")
    assert "biz-b" not in str(refused.value)
    assert ctx.authorized == []


# ---- runs of workflows ----

class _Runner:
    """The runner's run lookups, for a run that belongs to a workflow (no lane, no surface)."""

    def __init__(self) -> None:
        self.cancelled: list[str] = []

    def get(self, run_id: str) -> Any:
        return SimpleNamespace(id=run_id, target=None, flow_id="w1", output=None, trigger=None)

    def describe(self, run: Any, steps: bool = True) -> dict[str, Any]:
        return {"id": run.id}

    async def cancel(self, run_id: str, by: Any = None) -> Any:
        self.cancelled.append(run_id)
        return SimpleNamespace(id=run_id)


def _workflows(workspace: str) -> Any:
    """The workflows service, with one workflow whose workspace is `workspace`."""
    return SimpleNamespace(get_version=lambda flow_id, version=None: SimpleNamespace(
        definition=SimpleNamespace(workspace=workspace)))


async def test_run_status_and_cancel_hide_a_workflow_run_outside_the_grant():
    """A run with no lane belongs to its workflow's workspace: outside the grant it is not found."""
    runtime = Runtime(runner=_Runner(), workflows=_workflows("biz-b"))
    ctx = GrantCtx(runtime, grant_for("biz-a/*"))
    with pytest.raises(NotFound):
        await runner_tools.run_status(ctx, id="run_w1")
    with pytest.raises(NotFound):
        await runner_tools.run_cancel(ctx, id="run_w1")
    assert ctx.authorized == [] and runtime.require("runner").cancelled == []

    mine = GrantCtx(Runtime(runner=_Runner(), workflows=_workflows("biz-a")), grant_for("biz-a/*"))
    assert await runner_tools.run_status(mine, id="run_w1") == {"id": "run_w1"}


# ---- workspaces ----

async def test_a_workspace_outside_the_grant_looks_absent_and_cannot_be_planned(ws):
    """workspace_list of biz-b answers what a missing workspace answers, without its notes; a plan into
    biz-b is refused before anything is planned."""
    ws.service.create_workspace("biz-b", notes="biz-b private note")
    ws.service.create_workspace("biz-a")
    ctx = GrantCtx(ws.runtime, grant_for("biz-a/*"))
    with pytest.raises(NotFound) as outside:
        await ws_tools.workspace_list(ctx, workspace="biz-b")
    with pytest.raises(NotFound) as missing:
        await ws_tools.workspace_list(ctx, workspace="biz-zz")
    assert "private note" not in str(outside.value)
    assert str(outside.value).replace("biz-b", "X") == str(missing.value).replace("biz-zz", "X")
    with pytest.raises(PolicyDenied):
        await ws_tools.workspace_plan(ctx, template="social-6", workspaces=["biz-b"])
    assert ws.lanes.created == []


def test_a_proxy_conflict_names_no_lane_the_reader_may_not_see():
    """With shown() False, the reasons say another workspace and another lane; with it True they name biz-b."""
    candidate = LaneFact("biz-a/instagram", "biz-a", "instagram", "lt1")
    other = LaneFact("biz-b/instagram", "biz-b", "instagram", "lt1")
    hidden = " ".join(proxy_conflicts(candidate, [other], shown=lambda lane: False))
    assert "biz-b" not in hidden and "a lane in another workspace" in hidden
    named = " ".join(proxy_conflicts(candidate, [other], shown=lambda lane: True))
    assert "biz-b/instagram in workspace biz-b" in named


async def test_a_proxy_conflict_through_workspace_plan_names_no_lane_outside_the_grant(ws):
    """A plan inside the grant that clashes with a lane outside it reports the clash without naming it."""
    ws.lanes.add("biz-b/instagram", proxy="lt1")
    ws.service.create_workspace("biz-a")
    ctx = GrantCtx(ws.runtime, grant_for("biz-a/*"))
    plan = await ws_tools.workspace_plan(ctx, template="social-6", workspaces=["biz-a"],
                                         proxy_per_workspace={"biz-a": "lt1"})
    assert plan["ok"] is False
    text = json.dumps(plan)
    assert "biz-b" not in text and "a lane in another workspace" in text


# ---- the API ----

def test_get_api_flows_hides_a_pattern_flow_outside_the_grant(api_node):
    """A grant for biz-a lists the pattern flows it could match, not the pattern biz-b/*."""
    flows = api_node.runtime.flows
    flows.save(read_def("biz-b.any.look", "biz-b/*"))
    flows.save(read_def("biz-a.any.look", "biz-a/instagram"))
    flows.save(read_def("any.pattern.look", "*/instagram"))
    token = api_node.grant(subject={"id": "reader"}, workspaces=["biz-a"])
    with api_node.client() as client:
        listed = client.get("/api/flows", headers=bearer(token)).json()
    assert {f["id"] for f in listed} == {"biz-a.any.look", "any.pattern.look"}


# ---- what the review found next: flow tools, idempotency keys, site API keys ----

def test_a_flows_typed_tool_is_listed_only_to_a_grant_that_sees_the_flow(tmp_path):
    """flow_<id> carries the flow's title and params: a grant outside its lane is not shown it."""
    h = Harness(tmp_path)
    h.save({**read_def("biz-b.social.read", "biz-b/x"), "title": "Read biz-b's inbox"})
    h.save(read_def("any.pattern.read", "*/x"))
    specs = {s.name: s for s in runner_tools.flow_tool_specs(h.runtime)}
    grant = grant_for("biz-a/*")
    seen = {name for name, s in specs.items() if s.view is not None and s.view(grant.lane_allowed) is not None}
    assert seen == {"flow_any_pattern_read"}
    assert specs["flow_biz_b_social_read"].view(grant_for("biz-b/*").lane_allowed) is not None


def test_a_used_idempotency_key_does_not_name_the_run_that_holds_it(tmp_path):
    h = Harness(tmp_path)
    h.save(read_def("biz-b.social.read", "biz-b/x"))
    h.submit("biz-b.social.read", lane="biz-b/x", idempotency_key="k-shared")
    with pytest.raises(InvalidRequest) as refused:
        h.runner.existing_run("k-shared", flow_id="biz-a.social.read", lane="biz-a/x")
    assert "biz-b" not in str(refused.value) and "run_" not in str(refused.value)


async def test_a_site_api_key_outside_the_grant_is_neither_replaced_nor_read(sa):
    """A draft whose surfaces the grant does not reach keeps its definition, and its key answers not_found."""
    from test_siteapi_service import API, LANE, doc

    from agentos.runner.fakes import FakeCtx
    from agentos.runner.siteapis import tools as site_tools

    sa.svc.save(doc(), by={"kind": "agent", "id": "agent-b", "owner": "owner"})

    class Elsewhere(FakeCtx):
        def can_access_lane(self, lane_id):
            return lane_id != LANE

    ctx = Elsewhere(sa.runtime)
    with pytest.raises(PolicyDenied, match="taken"):
        await site_tools.site_api_propose(ctx, draft={**doc(), "surfaces": ["biz-a/x"]})
    assert sa.svc.drafts.get(API).definition["surfaces"] == [LANE] and ctx.authorized == []
    with pytest.raises(NotFound):
        await site_tools.site_api_propose(ctx, draft=API)


@pytest.fixture
def sa(tmp_path):
    from test_siteapi_service import LANE, Approvals, FakeLanes, FakeNotifier, portal

    from agentos.runner.siteapis.service import SERVICE_NAME

    harness = Harness(tmp_path, site=portal(), services={"lanes": FakeLanes([LANE]), "notifier": FakeNotifier(),
                                                         "approvals": Approvals()})
    harness.svc = harness.runtime.get(SERVICE_NAME)
    return harness
