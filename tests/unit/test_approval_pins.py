"""Characterization pins: the exact content_hash and content_digest the approval card makers create today."""

from __future__ import annotations

import hashlib
import json

import pytest
from events_support import World, no_quiet
from test_policy_support import Clock, make_service

import agentos.events.service as events_service
from agentos.core.errors import ApprovalPending
from agentos.core.models import Effect, Principal
from agentos.policy.approval_binding import content_digest
from agentos.policy.util import content_hash


@pytest.fixture
def policy(tmp_path):
    """A policy service on a fresh home, with its runtime, as the policy tests build one."""
    svc, rt = make_service(tmp_path, clock=Clock())
    yield svc, rt
    svc.close()


async def pending_commit(svc, *, budget=None, **call):
    """The approval card a fresh grant's commit creates (a person has Ask me first on for the lane)."""
    svc.ask_first.set("lane", call["lane_id"], True, by="owner", person=True)
    kw = {"budget": budget} if budget is not None else {}
    _g, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.IRREVERSIBLE, **kw)
    p, g = svc.resolve(tok)
    ctx = svc.context(g, principal=p, tool="t", token=tok)
    with pytest.raises(ApprovalPending) as e:
        await ctx.authorize(Effect.COMMIT, **call)
    return svc.approvals.get(e.value.approval_id)


async def test_policy_commit_card_pins_hash_and_digest(policy):
    """A commit with plain content: the card's content_hash (bound with tool, lane, summary) and content_digest."""
    svc, _ = policy
    ap = await pending_commit(svc, lane_id="biz-a/insta", summary="Say hello", content={"text": "hello"})
    assert (ap.tool, ap.lane, ap.effect, ap.summary) == ("t", "biz-a/insta", "commit", "Say hello")
    assert ap.content_hash == "sha256:90c70f67cf9029525ad89f18dc43822d8e5362d1be9f39e6212423829c35d450"
    assert ap.content_digest == "sha256:cbbbdcd27692344de5dbab3abcaba413fb0f45307267de7081401576df1cb176"


async def test_policy_commit_with_a_stated_cost_pins_the_cost_in_the_hash(policy):
    """The same commit with a stated $0.90 cost: the hash moves (cost usd_micro 900000), the digest does not."""
    svc, _ = policy
    ap = await pending_commit(svc, budget={"usd": 1.0}, lane_id="biz-a/insta", summary="Say hello",
                              content={"text": "hello"}, cost={"usd": 0.9})
    assert (ap.tool, ap.lane, ap.effect) == ("t", "biz-a/insta", "commit")
    assert ap.content_hash == "sha256:30d725abb18e8da1311ed765fd9486157e3f4b306fde46feb3cfcb9f07d71b0e"
    assert ap.content_digest == "sha256:cbbbdcd27692344de5dbab3abcaba413fb0f45307267de7081401576df1cb176"


def test_events_trigger_proposal_card_pins_hash_of_its_preview(tmp_path, monkeypatch):
    """An agent's trigger proposal. P1 moved it onto a keyed ActionBinding on purpose: the preview is unchanged,
    the card now binds the route's digest too, and the bare sha256 of the preview cards made before it hold is
    what the proposal check still accepts for them."""
    w = World(tmp_path, settings=no_quiet())
    monkeypatch.setattr(events_service, "new_id", lambda prefix: "rt_0123456789ab")
    agent = Principal(kind="agent", id="claude-code", grant_id="gr_agent0001")
    out = w.events.propose_route({"name": "IG comments", "match": {"source": "watch", "workspace": "biz-a"},
                                  "target": {"kind": "agent", "lane": "biz-a/claude"}, "instruction": "Draft replies.",
                                  "grant": {"lanes": ["biz-a/instagram"]}},
                                 principal=agent, grant_id="gr_agent0001", grant_path="/gr_agent0001/")
    # the fake approvals store keeps the create_action kwargs; the preview lives there, not on the Approval
    card = w.approvals.created[0]
    assert out["trigger_id"] == "rt_0123456789ab"
    assert (card["tool"], card["lane"], card["effect"]) == ("trigger_propose", "biz-a/claude", "commit")
    assert card["content_digest"] == content_digest(doc_route := json.loads(card["preview"]))
    assert card["binding"] == "keyed"
    assert hashlib.sha256(card["preview"].encode()).hexdigest() == \
        "8d654c7205c30d38e6a6d3245550a550c5e8338e43c78165b9b11cb87137739c"
    [(approval_id, doc)] = w.store.pending_proposals()
    assert approval_id == out["approval_id"]
    assert doc_route == doc["route"]
    assert card["content_hash"] == "sha256:5608a81d813c7fbc2961f9b8484060d65646d4f9d700a9c69e345f80dd6d7c1d"


def test_content_hash_of_a_plain_dict_is_pinned():
    """content_hash of {"a": 1}: sha256 over its canonical JSON, with the sha256: prefix."""
    assert content_hash({"a": 1}) == "sha256:015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862"


def test_content_digest_of_plain_content_is_pinned():
    """content_digest of {"text": "hello"}: the same canonical-JSON hash as content_hash."""
    assert content_digest({"text": "hello"}) == "sha256:cbbbdcd27692344de5dbab3abcaba413fb0f45307267de7081401576df1cb176"
