"""MCP tools for events: agents read what their grant reaches and may only propose triggers."""

from fnmatch import fnmatchcase

import pytest
from events_support import World, no_quiet

from openajar.core.errors import PolicyDenied
from openajar.core.models import Effect, Principal
from openajar.events.tools import events_list, tool_specs, trigger_propose, triggers_list


class Ctx:
    def __init__(self, runtime, lanes):
        self.runtime, self.lanes = runtime, lanes
        self.principal = Principal(kind="agent", id="claude-code", grant_id="gr_agent0001")
        self.authorized, self.traces = [], []

    def can_access_lane(self, lane_id):
        return any(fnmatchcase(lane_id, p) for p in self.lanes)

    async def authorize(self, effect, **kw):
        self.authorized.append((effect, kw))

    def trace(self, kind, **fields):
        self.traces.append((kind, fields))


def test_tool_effects():
    specs = {s.name: s for s in tool_specs(None)}
    assert specs["events_list"].effect == Effect.READ and specs["triggers_list"].effect == Effect.READ
    assert specs["trigger_propose"].effect == Effect.DRAFT
    assert all(s.hidden_without == "event_queue" for s in specs.values())
    assert "trigger_create" not in specs  # agents never write triggers


async def test_events_list_shows_what_the_grant_reaches_without_content(tmp_path):
    w = World(tmp_path, settings=no_quiet())
    w.route()
    w.emit(1, content={"text": "secret customer message"})
    w.emit(2, workspace="biz-b", lane="biz-b/facebook")
    w.events.emit({"source": "cron", "kind": "cron.fired", "workspace": "biz-a", "dedupe_key": "c"})
    narrow = await events_list(Ctx(w.runtime, ["biz-a/instagram"]), limit=10)
    assert [e["lane"] for e in narrow["events"]] == ["biz-a/instagram"]
    item = narrow["events"][0]
    assert item["content"] == {"chars": 23, "origin": None, "truncated": False}
    assert "secret customer message" not in str(narrow)
    assert item["deliveries"][0]["state"] == "pending"
    whole = await events_list(Ctx(w.runtime, ["biz-a/*"]), limit=10)
    assert {e["source"] for e in whole["events"]} == {"watch", "cron"}


async def test_triggers_list_and_propose(tmp_path):
    w = World(tmp_path, settings=no_quiet())
    w.route(name="IG")
    ctx = Ctx(w.runtime, ["biz-a/*"])
    listed = await triggers_list(ctx)
    assert [t["name"] for t in listed["triggers"]] == ["IG"] and listed["triggers"][0]["waiting"] == 0
    assert (await triggers_list(Ctx(w.runtime, ["biz-b/*"])))["triggers"] == []
    proposal = {"name": "FB comments", "match": {"source": "watch", "workspace": "biz-a"},
                "target": {"kind": "agent", "lane": "biz-a/claude"}, "instruction": "Draft replies.",
                "grant": {"lanes": ["biz-a/facebook"]}}
    out = await trigger_propose(ctx, trigger=proposal)
    assert out["approval_id"] and out["state"] == "pending"
    assert [r.name for r in w.events.list_routes()] == ["IG"]  # nothing created yet
    with pytest.raises(PolicyDenied, match="grant reaches"):
        await trigger_propose(Ctx(w.runtime, ["biz-a/instagram"]), trigger=proposal)


async def test_trigger_propose_refuses_a_match_lane_the_grant_does_not_reach(tmp_path):
    w = World(tmp_path, settings=no_quiet())
    # a lane in another workspace fails Match validation before this check, so the lane stays in biz-a
    ctx = Ctx(w.runtime, ["biz-a/instagram"])
    proposal = {"name": "FB comments", "match": {"source": "watch", "workspace": "biz-a", "lane": "biz-a/facebook"},
                "target": {"kind": "brain"}, "instruction": "Draft replies.", "grant": {"lanes": ["biz-a/instagram"]}}
    with pytest.raises(PolicyDenied, match="biz-a/facebook"):
        await trigger_propose(ctx, trigger=proposal)
    assert ctx.authorized == [] and w.approvals.created == []
