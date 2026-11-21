"""One changed character between an approval and what it covers, for trigger proposals (events) and
workflow sends (workflows): the changed copy is refused, and the unchanged one goes through."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest
from events_support import World, no_quiet
from workflow_support import LIST, MAIL, OWNER, PEOPLE, action, make_node, workflow

from openajar.core.models import Principal

AGENT_GRANT = "gr_agent0001"
SEND = f"mail.send@{MAIL}"


def one_char_apart(before: str, after: str) -> bool:
    return len(before) == len(after) and sum(a != b for a, b in zip(before, after, strict=True)) == 1


def hex_char_changed(text: str, at: int = -1) -> str:
    """The text with one hex digit changed, so a route id stays well formed (rt_ and 12 hex digits)."""
    i = at % len(text)
    return text[:i] + ("0" if text[i] != "0" else "1") + text[i + 1 :]


def propose(w: World) -> dict:
    agent = Principal(kind="agent", id="claude-code", grant_id=AGENT_GRANT)
    return w.events.propose_route(
        {
            "name": "IG comments",
            "match": {"source": "watch", "workspace": "biz-a"},
            "target": {"kind": "agent", "lane": "biz-a/claude"},
            "instruction": "Draft replies.",
            "grant": {"lanes": ["biz-a/instagram"]},
        },
        principal=agent,
        grant_id=AGENT_GRANT,
        grant_path=f"/{AGENT_GRANT}/",
    )


def pending_doc(w: World, aid: str) -> dict:
    return next(doc for a, doc in w.store.pending_proposals() if a == aid)


def write_doc(w: World, aid: str, doc: dict) -> None:
    w.store.put_proposal(aid, "pending", w.clock.now(), doc)


def state_of(w: World, aid: str) -> str:
    return w.store._rows("SELECT state FROM proposals WHERE approval_id = ?", (aid,))[0]["state"]


def make_legacy(w: World, aid: str, route: dict) -> None:
    """The fake approval becomes a card made before ActionBinding: no binding, bare sha256 of the route."""
    ap = w.approvals.items[aid]
    ap.binding, ap.content_digest = None, None
    ap.content_hash = hashlib.sha256(json.dumps(route, indent=2, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------- events: trigger proposals


async def test_a_proposal_approved_as_written_creates_its_route(tmp_path):
    """Control: a proposal approved exactly as it was proposed creates its route."""
    w = World(tmp_path, settings=no_quiet())
    out = propose(w)
    w.approvals.items[out["approval_id"]].state = "approved"
    assert w.events.check_proposals() == 1
    assert [r.id for r in w.events.list_routes()] == [out["trigger_id"]]
    assert state_of(w, out["approval_id"]) == "created"


async def test_a_route_changed_by_one_character_after_approval_creates_nothing(tmp_path):
    """One character of the stored route's instruction changed after approval: no route, state mismatch."""
    w = World(tmp_path, settings=no_quiet())
    out = propose(w)
    aid = out["approval_id"]
    doc = pending_doc(w, aid)
    changed_text = doc["route"]["instruction"].replace("replies.", "replies!")
    assert one_char_apart(doc["route"]["instruction"], changed_text)
    write_doc(w, aid, {**doc, "route": {**doc["route"], "instruction": changed_text}})
    w.approvals.items[aid].state = "approved"
    assert w.events.check_proposals() == 0
    assert w.events.list_routes() == [] and state_of(w, aid) == "mismatch"


async def test_a_route_id_changed_by_one_character_after_approval_creates_nothing(tmp_path):
    """One character of the stored proposal id changed after approval: no route, state mismatch."""
    w = World(tmp_path, settings=no_quiet())
    out = propose(w)
    aid = out["approval_id"]
    doc = pending_doc(w, aid)
    changed_id = hex_char_changed(doc["id"])
    assert one_char_apart(doc["id"], changed_id)
    write_doc(w, aid, {**doc, "id": changed_id})
    w.approvals.items[aid].state = "approved"
    assert w.events.check_proposals() == 0
    assert w.events.list_routes() == [] and state_of(w, aid) == "mismatch"


async def test_a_summary_changed_by_one_character_after_approval_creates_nothing(tmp_path):
    """One character of the stored summary (the card's words) changed after approval: no route, state mismatch."""
    w = World(tmp_path, settings=no_quiet())
    out = propose(w)
    aid = out["approval_id"]
    doc = pending_doc(w, aid)
    changed_summary = doc["summary"].replace('"IG comments"', '"Ig comments"', 1)
    assert one_char_apart(doc["summary"], changed_summary)
    write_doc(w, aid, {**doc, "summary": changed_summary})
    w.approvals.items[aid].state = "approved"
    assert w.events.check_proposals() == 0
    assert w.events.list_routes() == [] and state_of(w, aid) == "mismatch"


async def test_a_legacy_card_still_creates_the_route_it_bound_as_written(tmp_path):
    """A card made before ActionBinding (bare hash of the route) creates its route when nothing changed."""
    w = World(tmp_path, settings=no_quiet())
    out = propose(w)
    aid = out["approval_id"]
    make_legacy(w, aid, pending_doc(w, aid)["route"])
    w.approvals.items[aid].state = "approved"
    assert w.events.check_proposals() == 1
    assert [r.id for r in w.events.list_routes()] == [out["trigger_id"]]


async def test_a_legacy_card_with_a_route_changed_by_one_character_creates_nothing(tmp_path):
    """A legacy card whose stored route changed by one character after approval: no route, state mismatch."""
    w = World(tmp_path, settings=no_quiet())
    out = propose(w)
    aid = out["approval_id"]
    doc = pending_doc(w, aid)
    make_legacy(w, aid, doc["route"])
    changed_text = doc["route"]["instruction"].replace("replies.", "replies!")
    assert one_char_apart(doc["route"]["instruction"], changed_text)
    write_doc(w, aid, {**doc, "route": {**doc["route"], "instruction": changed_text}})
    w.approvals.items[aid].state = "approved"
    assert w.events.check_proposals() == 0
    assert w.events.list_routes() == [] and state_of(w, aid) == "mismatch"


# ---------------------------------------------------------------- workflows: sends released by approval


@pytest.fixture
def node(tmp_path):
    n = make_node(tmp_path)
    n.wf.sends.timezone = lambda: "UTC"
    n.h.clock.set(datetime(2026, 11, 4, 10, 0, tzinfo=UTC))
    return n


def note_send(node) -> str:
    """A webhook workflow that mails the whole list the text it was started with, so the text asks first."""
    step = action("send", SEND, inputs={"subject": "Hi", "body": "{{trigger.note}}"}, send={"list": LIST})
    return node.wf.save(workflow(step, trigger={"kind": "webhook"}), OWNER)["id"]


async def test_an_approved_send_with_nothing_changed_goes_out_once_to_the_list(node):
    """Control: the send a person approved, with nothing changed, mails each person on the list once."""
    wid = note_send(node)
    view = await node.run(wid, node.agent(), values={"note": "from outside"})
    assert view["status"] == "waiting_approval" and node.mail.sent == []
    node.svc.decide(node.step(view, "send")["approval_id"], "approve")
    await node.settle()
    assert sorted(node.mail.to) == PEOPLE and len(node.mail.sent) == 3
    assert node.view(wid, view["id"])["status"] == "succeeded_unverified"


async def test_an_approval_does_not_cover_a_send_whose_text_changed_by_one_character(node):
    """The text changed by one character after approval and before the run resumed: nothing is sent, and the run asks again."""
    wid = note_send(node)
    view = await node.run(wid, node.agent(), values={"note": "from outside"})
    first = node.step(view, "send")["approval_id"]
    node.svc.decide(first, "approve")
    assert node.view(wid, view["id"])["status"] == "waiting_approval"
    run = node.runner.store.get_run(view["id"])
    changed_note = "from outsidE"
    assert one_char_apart(run.params["trigger"]["note"], changed_note)
    run.params["trigger"]["note"] = changed_note
    node.runner.store.save_run(run)
    await node.settle()
    after = node.view(wid, view["id"])
    again = node.step(after, "send")["approval_id"]
    assert node.mail.sent == []
    assert after["status"] == "waiting_approval" and again not in (None, first)
