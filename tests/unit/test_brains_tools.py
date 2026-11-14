"""MCP tools: authorize Effect.BRAIN before spending, never return secrets."""

import json

import pytest
from testkit.brains import OAUTH, FakeCtx, add, make_service, wait_status

from agentos.brains import tool_specs
from agentos.core.errors import NotFound, PolicyDenied
from agentos.core.models import Effect, Principal
from agentos.core.tools import TOOL_NAME_RE

P = Principal(id="claude-code", thread="T-9")


@pytest.fixture
def svc(tmp_path):
    return make_service(tmp_path)


def tools(svc):
    return {s.name: s for s in tool_specs(svc.runtime)}


class ScopedCtx(FakeCtx):
    """A grant that reaches only `lanes`: its precheck refuses every other lane, as the policy's does."""

    def __init__(self, runtime, principal=None, *, lanes=()):
        super().__init__(runtime, principal)
        self.lanes = set(lanes)

    def precheck(self, effect, *, lane_id=None):
        if lane_id not in self.lanes:
            raise PolicyDenied(f"your grant does not cover lane {lane_id}")


def test_specs_are_valid(svc):
    specs = tools(svc)
    assert set(specs) == {"brain_run", "brain_status", "brain_tail", "brain_cancel", "brain_resume",
                          "brain_tasks", "brains_list", "brain_use"}
    for s in specs.values():
        assert TOOL_NAME_RE.match(s.name) and s.description and s.hidden_without == "brains"
        assert s.lane_arg is None
    assert specs["brain_run"].effect == Effect.BRAIN and specs["brain_status"].effect == Effect.READ


async def test_brain_run_authorizes_brain_first_with_a_node_summary(svc):
    await add(svc, "claude-max", "subscription", "anthropic")
    ctx = FakeCtx(svc.runtime, P)
    out = await tools(svc)["brain_run"].handler(ctx, prompt="Do the thing FAKE:ok", workspace="personal",
                                                budget={"max_turns": 9})
    first = ctx.authorized[0]
    assert first["effect"] == Effect.BRAIN
    assert "workspace personal" in first["summary"] and "9 turns" in first["summary"]
    assert "Do the thing" not in first["summary"]  # the summary is node-authored, not the prompt
    assert len(first["content"]["prompt_sha256"]) == 64
    st = await wait_status(svc, out["id"])
    assert st["status"] == "succeeded_unverified"


async def test_denied_brain_spends_nothing(svc):
    await add(svc, "claude-max", "subscription", "anthropic")
    ctx = FakeCtx(svc.runtime, P, deny={Effect.BRAIN})
    with pytest.raises(PolicyDenied):
        await tools(svc)["brain_run"].handler(ctx, prompt="FAKE:ok", workspace="personal")
    assert svc.list_tasks() == []


async def test_brain_run_refuses_a_lane_outside_the_grant_before_it_asks_or_charges(svc):
    await add(svc, "claude-max", "subscription", "anthropic")
    ctx = ScopedCtx(svc.runtime, P, lanes={"biz-a/x"})
    with pytest.raises(PolicyDenied, match="biz-b/x"):
        await tools(svc)["brain_run"].handler(ctx, prompt="FAKE:ok", workspace="biz-b", lanes=["biz-b/x"])
    assert ctx.authorized == [] and svc.list_tasks() == []


async def test_status_tail_list_and_use(svc):
    await add(svc, "claude-max", "subscription", "anthropic")
    await add(svc, "ollama", "local", "ollama")
    t = tools(svc)
    ctx = FakeCtx(svc.runtime, P)
    out = await t["brain_run"].handler(ctx, prompt="FAKE:ok", workspace="personal")
    await wait_status(svc, out["id"])
    st = await t["brain_status"].handler(ctx, id=out["id"])
    tail = await t["brain_tail"].handler(ctx, id=out["id"], since=None, limit=5)
    listed = await t["brains_list"].handler(ctx)
    tasks = await t["brain_tasks"].handler(ctx)
    blob = json.dumps([st, tail, listed, tasks])
    assert OAUTH not in blob and "raw" not in json.dumps(tail["events"])
    assert {b["name"] for b in listed} == {"claude-max", "ollama"}
    assert tasks[0]["id"] == out["id"]
    used = await t["brain_use"].handler(ctx, name="ollama", workspace="personal")
    assert used == {"workspace": "personal", "default_brain": "ollama"}
    assert ctx.authorized[-1]["effect"] == Effect.BRAIN
    out2 = await t["brain_run"].handler(ctx, prompt="FAKE:ok", workspace="personal")
    assert out2["route"]["chain"][0] == "ollama"
    await wait_status(svc, out2["id"])
    node = await t["brain_use"].handler(ctx, name="claude-max")
    assert node == {"workspace": None, "default_brain": "claude-max"}


async def test_cancel_and_resume_tools(svc):
    await add(svc, "claude-max", "subscription", "anthropic")
    t = tools(svc)
    ctx = FakeCtx(svc.runtime, P)
    out = await t["brain_run"].handler(ctx, prompt="FAKE:slow:20", workspace="personal")
    await wait_status(svc, out["id"], until=("running",))
    await t["brain_cancel"].handler(ctx, id=out["id"])
    st = await wait_status(svc, out["id"], until=("cancelled",))
    assert st["resumable"]
    (svc.task_dir(out["id"]) / "work" / "fake-recover").write_text("")
    r = await t["brain_resume"].handler(ctx, id=out["id"])
    assert r["status"] in ("starting", "running")
    assert (await wait_status(svc, out["id"]))["status"] == "succeeded_unverified"


async def test_another_agent_of_the_owner_cannot_see_or_steer_a_task_it_does_not_reach(svc):
    """Every agent grant of the owner shares the owner: that alone must not open another agent's task."""
    await add(svc, "claude-max", "subscription", "anthropic")
    t = tools(svc)
    mine = FakeCtx(svc.runtime, P)
    out = await t["brain_run"].handler(mine, prompt="FAKE:slow:20", workspace="personal")
    await wait_status(svc, out["id"], until=("running",))
    other = FakeCtx(svc.runtime, Principal(id="other-agent"))
    for name, args in (("brain_status", {}), ("brain_tail", {"since": None, "limit": 5}), ("brain_cancel", {}),
                       ("brain_resume", {})):
        with pytest.raises(NotFound):
            await t[name].handler(other, id=out["id"], **args)
    assert [a["effect"] for a in other.authorized] == [Effect.READ, Effect.READ]  # status and tail; no BRAIN asked
    assert await t["brain_tasks"].handler(other) == []
    assert [x["id"] for x in await t["brain_tasks"].handler(mine)] == [out["id"]]
    sub = FakeCtx(svc.runtime, Principal(id="claude-code/worker"))  # a sub-agent sees its own only, not its parent's
    assert await t["brain_tasks"].handler(sub) == []
    reach = ScopedCtx(svc.runtime, Principal(id="lane-agent"), lanes={"personal/x"})
    reach.can_access_lane = lambda lane: lane in reach.lanes
    assert await t["brain_tasks"].handler(reach) == []  # a task with no lanes is its starter's
    await t["brain_cancel"].handler(mine, id=out["id"])
    await wait_status(svc, out["id"], until=("cancelled",))


async def test_reaching_a_tasks_lanes_shows_it_but_does_not_steer_it(svc, monkeypatch):
    """A resumed task runs on its starter's child grant: only its starter (or an admin grant) resumes it."""
    await add(svc, "claude-max", "subscription", "anthropic")
    t = tools(svc)
    mine = FakeCtx(svc.runtime, P)
    out = await t["brain_run"].handler(mine, prompt="FAKE:slow:20", workspace="personal")
    await wait_status(svc, out["id"], until=("running",))
    status, listed = svc.status, svc.list_tasks  # as if it ran on personal/x (lanes need a policy to mint for)
    monkeypatch.setattr(svc, "status", lambda *a, **k: {**status(*a, **k), "lanes": ["personal/x"]})
    monkeypatch.setattr(svc, "list_tasks", lambda *a, **k: [{**x, "lanes": ["personal/x"]} for x in listed(*a, **k)])
    peer = ScopedCtx(svc.runtime, Principal(id="peer"), lanes={"personal/x"})
    peer.can_access_lane = lambda lane: lane in peer.lanes
    assert (await t["brain_status"].handler(peer, id=out["id"]))["id"] == out["id"]
    assert [x["id"] for x in await t["brain_tasks"].handler(peer)] == [out["id"]]
    for name in ("brain_cancel", "brain_resume"):
        with pytest.raises(NotFound):
            await t[name].handler(peer, id=out["id"])
    assert all(a["effect"] == Effect.READ for a in peer.authorized)
    monkeypatch.undo()
    await t["brain_cancel"].handler(mine, id=out["id"])
    await wait_status(svc, out["id"], until=("cancelled",))


async def test_bad_arguments_are_clear(svc):
    from agentos.core.errors import InvalidRequest
    ctx = FakeCtx(svc.runtime, P)
    with pytest.raises(InvalidRequest, match="budget"):
        await tools(svc)["brain_run"].handler(ctx, prompt="x", workspace="personal", budget={"max_turns": "lots"})
    with pytest.raises(InvalidRequest, match="workspace"):
        await tools(svc)["brain_run"].handler(ctx, prompt="x", workspace="Bad Name")


def test_require_lanes_refuses_a_lane_the_grant_cannot_access():
    from agentos.core.tools import require_lanes

    class Closed:  # a context with no precheck: require_lanes falls back to can_access_lane
        def can_access_lane(self, lane_id):
            return False

    with pytest.raises(PolicyDenied, match="biz-a/x"):
        require_lanes(Closed(), ["biz-a/x"], Effect.BRAIN)


def test_require_lanes_refuses_a_lane_that_is_not_a_string():
    from agentos.core.errors import InvalidRequest
    from agentos.core.tools import require_lanes

    class Open:
        def can_access_lane(self, lane_id):
            return True

    with pytest.raises(InvalidRequest):
        require_lanes(Open(), ["biz-a/x", 7], Effect.BRAIN)


def test_require_lanes_passes_when_the_grant_reaches_every_lane():
    from agentos.core.tools import require_lanes

    class Open:
        def can_access_lane(self, lane_id):
            return True

    require_lanes(Open(), ["biz-a/x", "biz-a/y"], Effect.BRAIN)
    require_lanes(Open(), None, Effect.BRAIN)


async def test_brains_list_with_no_brain_says_what_the_owner_does_next(tmp_path):
    """QA 2026-11-10, tester B 5: a bare [] told the agent nothing."""
    empty = make_service(tmp_path / "empty")
    out = await tools(empty)["brains_list"].handler(FakeCtx(empty.runtime, P))
    assert out["brains"] == []
    assert "agentos brains add" in out["next"] and "--no-credential" in out["next"] and "Tell the user" in out["next"]
