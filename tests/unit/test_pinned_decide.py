"""Pins a decision on an approval (openajar.policy.approvals.ApprovalStore.decide): who may decide,
its arguments, expiry, content the person saw, questions and their answers, and what the decided
approval records. Random ids are replaced by labels; the clock is fixed."""

from __future__ import annotations

import json

import pytest
from pinned import check, outcome, plain
from test_policy_support import Clock, make_service

from openajar.core.errors import ApprovalPending
from openajar.core.models import Effect, Principal
from openajar.policy.confirm import ConfirmLevel, Proof


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    svc, _ = make_service(tmp_path, clock=clock)
    yield svc, clock
    svc.close()


async def pending(svc, lane="biz-c/insta", effect=Effect.COMMIT, content="hello", n=[0]):  # noqa: B006
    n[0] += 1
    if svc.ask_first.get("lane", lane) is None:  # "Ask me first" on, so a commit waits for a person
        svc.ask_first.set("lane", lane, True, by="owner", person=True)
    _, tok = svc.issue(subject={"id": "claude-code"}, workspaces=[lane.split("/")[0]], ceiling=Effect.PAY)
    g = svc.resolve(tok)[1]
    ctx = svc.context(g, tool="lane_post")
    with pytest.raises(ApprovalPending) as e:
        await ctx.authorize(effect, lane_id=lane, content=f"{content} {n[0]}", summary="Do it")
    return e.value.approval_id, g


def question(svc, options=None):
    p = Principal(kind="agent", id="agent-q", owner="owner")
    return svc.approvals.create_question(question="Which one?", options=options, requester=p, grant_id=None,
                                         grant_path=None, workspace="biz-c").id


async def test_decide_is_pinned(env):
    svc, clock = env
    va = svc.issue_member_grant("m_va", "biz-c", "approver", handle="va")
    other = svc.issue_member_grant("m_ot", "biz-a", "approver", handle="ot")
    ids: dict[str, str] = {}

    def label(value: str, name: str) -> str:
        ids[value] = name
        return value

    async def ap(**kw) -> str:
        aid, g = await pending(svc, **kw)
        label(aid, f"<approval {len(ids)}>")
        label(g.id, f"<grant {len(ids)}>")
        return aid

    cases: dict = {}

    def decide(name: str, aid: str, decision: str = "approve", **kw) -> None:
        cases[name] = outcome(lambda: plain(svc.approvals.decide(aid, decision, **kw)))

    decide("owner approves", await ap())
    decide("owner denies with a note", await ap(), "deny", note="  not now  ")
    decide("a note of spaces", await ap(), "deny", note="   ")
    decide("a long note", await ap(), "deny", note="n" * 1500)
    decide("not a decision", await ap(), "maybe")
    decide("deny with an edit", await ap(), "deny", edit={"text": "x"})
    decide("approve with an edit on a plain card", await ap(), edit={"text": "x"})
    decide("via is not a word", await ap(), via="Bad Via!")
    decide("owner via passkey", await ap(), via="passkey")
    decide("owner by a name", await ap(), by="r" * 200)
    aid, g = await pending(svc)
    label(aid, "<approval self>"), label(g.id, "<grant self>")
    decide("refuse the requester's own family", aid, refuse_family=g.root_id)
    decide("refuse family owner", await ap(), refuse_family="owner")
    aid, g = await pending(svc)
    label(aid, "<approval agent>"), label(g.id, "<grant agent>")
    decide("an agent's grant decides", aid, decider=g)
    decide("a member approves", await ap(), decider=va, via="passkey")
    decide("a member approves in person", await ap(), decider=va, in_person=True)
    decide("a member denies", await ap(), "deny", decider=va, via="passkey")
    decide("a member of another workspace", await ap(), decider=other, via="passkey")
    decide("a proof of no person", await ap(), proof=Proof(None, None, clock()))
    decide("a chat proof", await ap(), proof=Proof("chat", ConfirmLevel.CHAT, clock()))
    decide("a device key proof", await ap(), proof=Proof("passkey", ConfirmLevel.DEVICE_KEY, clock()))
    aid = await ap()
    decide("content the person saw, matching", aid, content=(svc.approvals.get(aid).content_hash,
                                                              svc.approvals.get(aid).content_digest))
    decide("content the person saw, other", await ap(), content=("sha256:x", None))
    aid = await ap()
    svc.approvals.decide(aid, "approve")
    decide("already approved", aid)
    aid = await ap()
    clock.advance(10 * 86400)
    decide("expired", aid)
    decide("no such approval", "ap_missing")
    q = label(question(svc), "<question 1>")
    decide("a question with no answer", q)
    q = label(question(svc, ["red", "blue"]), "<question 2>")
    decide("a question with no answer, options", q)
    q = label(question(svc, ["red", "blue"]), "<question 3>")
    decide("an answer not in the options", q, answer="green")
    q = label(question(svc, ["red", "blue"]), "<question 4>")
    decide("an answer in the options", q, answer=" red ")
    q = label(question(svc), "<question 5>")
    decide("an answer from the note", q, note="the note")
    q = label(question(svc), "<question 6>")
    decide("a long answer", q, answer="a" * 2500)
    q = label(question(svc), "<question 7>")
    decide("a question denied", q, "deny")
    for g in (va, other):
        label(g.id, f"<member {g.subject.id}>")
    text = json.dumps(cases)
    for value, name in sorted(ids.items(), key=lambda kv: -len(kv[0])):
        text = text.replace(value, name)
    check("decide", json.loads(text))
