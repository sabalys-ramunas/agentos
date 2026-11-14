"""A trigger an agent proposed runs only what its card showed: the flow version the card named, and
nothing above the ceiling the card showed (docs/WORKFLOWS.md, docs/CONFIRMATIONS.md)."""

from __future__ import annotations

import pytest
from events_support import OWNER, World, check_flow, no_quiet

from agentos.core.errors import InvalidRequest
from agentos.core.models import Principal
from agentos.events.models import DeliveryState as S

AGENT = Principal(kind="agent", id="claude-code", grant_id="gr_agent0001")


def typing_flow(fid: str = "biz-a.social.reply") -> dict:
    """A flow whose step types: it can publish, whatever it declares."""
    flow = check_flow(fid)
    flow["effect"] = "read"
    flow["steps"].insert(1, {"type": "type", "locator": {"role": "textbox", "name": "Reply"}, "text": "hi"})
    return flow


def propose(w: World, target: dict, **grant):
    return w.events.propose_route({"name": "f", "match": {"source": "watch", "workspace": "biz-a"}, "target": target,
                                   **({"grant": grant} if grant else {})},
                                  principal=AGENT, grant_id="gr_agent0001", grant_path="/gr_agent0001/")


async def approve(w: World, out: dict):
    w.approvals.items[out["approval_id"]].state = "approved"
    await w.tick()
    return w.events.get_route(out["trigger_id"])


# ---- the version the card showed ----

async def test_a_proposed_flow_trigger_pins_the_version_its_card_shows(tmp_path):
    w = World(tmp_path, settings=no_quiet())
    v1 = w.flow()
    out = propose(w, {"kind": "flow", "flow": v1.id})
    assert f'"version": "{v1.version}"' in w.approvals.created[0]["preview"]
    route = await approve(w, out)
    assert route.target.version == v1.version
    newer = check_flow()
    newer["steps"][0]["url"] += "newer"
    v2 = w.flow(newer)
    assert v2.version != v1.version
    w.emit(1)
    await w.tick()
    runs = w.h.store.list_runs()
    assert len(runs) == 1 and runs[0].flow_version == v1.version


async def test_a_trigger_approved_without_a_version_does_not_run_the_newest(tmp_path):
    """Upgrade: a route created from a proposal before cards named a version runs nothing."""
    w = World(tmp_path, settings=no_quiet())
    fv = w.flow()
    r = w.events.add_route({"name": "f", "match": {"source": "watch", "workspace": "biz-a"},
                            "target": {"kind": "flow", "flow": fv.id}}, principal=OWNER)
    old = r.model_copy(update={"proposed_by": AGENT.model_dump()})
    w.store.put_route(old)
    w.emit(1)
    await w.tick()
    assert w.h.store.list_runs() == []
    d = w.deliveries(r.id)[0]
    assert d.state == S.FAILED and "version" in (d.reason or "")


async def test_a_waiting_card_without_a_version_creates_nothing(tmp_path):
    """Upgrade: a proposal still pending from before, whose card named no version, is not created."""
    w = World(tmp_path, settings=no_quiet())
    fv = w.flow()
    out = propose(w, {"kind": "flow", "flow": fv.id})
    aid = out["approval_id"]
    (doc,) = [d for a, d in w.store.pending_proposals() if a == aid]
    doc["route"]["target"]["version"] = None  # as a card made before this release holds it
    w.store.put_proposal(aid, "pending", w.clock.now(), doc)
    from agentos.events.service import _proposal_binding
    binding = _proposal_binding(doc["route"], doc["id"], "biz-a/social", doc["summary"])
    w.approvals.items[aid].content_hash, w.approvals.items[aid].content_digest = binding.content_hash, binding.content_digest
    w.approvals.items[aid].state = "approved"
    await w.tick()
    assert w.events.list_routes() == []


# ---- the ceiling the card showed ----

async def test_a_proposal_whose_flow_is_above_its_ceiling_is_refused(tmp_path):
    w = World(tmp_path, settings=no_quiet())
    fv = w.flow(typing_flow())
    with pytest.raises(InvalidRequest, match="ceiling"):
        propose(w, {"kind": "flow", "flow": fv.id})  # the ceiling is draft by default; the flow types
    assert w.approvals.created == []
    out = propose(w, {"kind": "flow", "flow": fv.id}, ceiling="commit")
    assert "ceiling commit" in out["summary"]


async def test_a_proposed_trigger_never_runs_a_flow_above_its_ceiling(tmp_path):
    w = World(tmp_path, settings=no_quiet())
    fv = w.flow(typing_flow())
    r = w.events.add_route({"name": "f", "match": {"source": "watch", "workspace": "biz-a"},
                            "target": {"kind": "flow", "flow": fv.id, "version": fv.version}}, principal=OWNER)
    w.store.put_route(r.model_copy(update={"proposed_by": AGENT.model_dump()}))  # its ceiling is draft
    w.emit(1)
    await w.tick()
    assert w.h.store.list_runs() == []
    d = w.deliveries(r.id)[0]
    assert d.state == S.FAILED and "ceiling" in (d.reason or "")


# ---- a workflow target ----

def test_a_proposed_trigger_for_a_workflow_is_refused_with_what_to_do_instead(tmp_path):
    """A workflow carries its own trigger, and workflow_propose puts it on a card with the version a yes
    turns on: a route proposed beside it would wake whatever version is live, so none is made."""
    w = World(tmp_path, settings=no_quiet())
    with pytest.raises(InvalidRequest, match="workflow_propose") as e:
        propose(w, {"kind": "workflow", "workflow": "biz-a.news"})
    assert "KeyError" not in str(e.value) and "trigger" in e.value.message
    assert w.approvals.created == [] and w.events.list_routes() == []
