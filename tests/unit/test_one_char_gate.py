"""Changing one character of an approved action never runs it: the binding, the policy gate and the co-sign."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from test_policy_support import PRESENT, Clock, make_service
from testkit.brains import add as add_brain
from testkit.brains import make_service as make_brain_service

from agentos.brains.models import Budget
from agentos.core.errors import ApprovalPending
from agentos.core.models import Effect, Principal
from agentos.policy.approval_binding import ActionBinding, action_binding_problem
from agentos.policy.approvals import ask_for_action

GRANT = "gr_test"
GRANT_PATH = "/gr_test/"
PRINCIPAL = Principal(kind="agent", id="claude-code", grant_id=GRANT)


@pytest.fixture
def policy(tmp_path):
    """A policy service on a fresh home, with its runtime, as the policy tests build one."""
    svc, rt = make_service(tmp_path, clock=Clock())
    yield svc, rt
    svc.close()


def hello(**over) -> ActionBinding:
    """A commit that says "hello" on biz-a/insta: a call-form binding, as the policy gate makes."""
    base = {"effect": "commit", "tool": "t", "lane": "biz-a/insta", "summary": "Say hello",
            "content": {"text": "hello"}}
    return ActionBinding(**{**base, **over})


def keyed(**over) -> ActionBinding:
    """A queued like on biz/x: a keyed binding, so its key and params are part of what the card covers."""
    base = {"effect": "commit", "tool": "connector.x.like", "lane": "biz/x", "params": {"post": "p1"},
            "content": {"operation": "x.like", "post": "p1"}, "key": "k1", "summary": "Like a post"}
    return ActionBinding(**{**base, **over})


def ask_first_ctx(svc, lane: str):
    """An agent's context for commits on `lane`, with Ask me first on for it (a person sees each commit)."""
    svc.ask_first.set("lane", lane, True, by="owner", person=True)
    _g, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.IRREVERSIBLE)
    p, g = svc.resolve(tok)
    return svc.context(g, principal=p, tool="t", token=tok)


async def commit_waits(ctx, **call) -> str:
    """The approval id that a commit call waits on: the call must raise ApprovalPending."""
    with pytest.raises(ApprovalPending) as e:
        await ctx.authorize(Effect.COMMIT, **call)
    return e.value.approval_id


# ------------------------------------------------------------------ ActionBinding and action_binding_problem


def test_a_card_covers_the_identical_action(policy):
    """A card made by ask_for_action for {"text": "hello"} covers the same binding again."""
    svc, _ = policy
    ap = ask_for_action(svc.approvals, hello(), PRINCIPAL, "test", grant_id=GRANT, grant_path=GRANT_PATH)
    assert ap.binding == "call"
    assert action_binding_problem(ap, hello()) is None


def test_one_character_changed_in_content_is_a_different_action(policy):
    """The same binding with {"text": "hellp"} is refused by the card made for "hello"."""
    svc, _ = policy
    ap = ask_for_action(svc.approvals, hello(), PRINCIPAL, "test", grant_id=GRANT, grant_path=GRANT_PATH)
    assert action_binding_problem(ap, hello(content={"text": "hellp"})) == "is for different content"


def test_a_card_for_one_lane_covers_no_other(policy):
    """The same content on biz-a/instb (one character off) is refused by the card made for biz-a/insta."""
    svc, _ = policy
    ap = ask_for_action(svc.approvals, hello(), PRINCIPAL, "test", grant_id=GRANT, grant_path=GRANT_PATH)
    assert action_binding_problem(ap, hello(lane="biz-a/instb")) == "is for biz-a/insta, not biz-a/instb"


@pytest.mark.parametrize("changed", [
    keyed(params={"post": "p2"}),
    keyed(key="k2"),
    keyed(summary="Like a past"),
], ids=["params", "key", "summary"])
def test_a_keyed_card_refuses_one_character_changed_in_params_key_or_summary(policy, changed):
    """A keyed card covers only its own params, key and summary, even when the content is identical."""
    svc, _ = policy
    ap = ask_for_action(svc.approvals, keyed(), PRINCIPAL, "test", grant_id=GRANT, grant_path=GRANT_PATH)
    assert ap.binding == "keyed"
    assert action_binding_problem(ap, keyed()) is None
    assert changed.content == keyed().content
    assert action_binding_problem(ap, changed) == "is for a different action"


def test_a_card_from_before_action_binding_is_covered_only_by_its_legacy_hash(policy):
    """A binding-less card with a bare legacy hash: covered by that hash, refused for a hash or content one character away."""
    svc, _ = policy
    bind = keyed()
    legacy = hashlib.sha256(b"a card made before ActionBinding").hexdigest()
    ap = svc.approvals.create_action(
        effect="commit", tool="connector.x.like", lane="biz/x", domain=None, summary="Like a post",
        content_hash=legacy, content_digest=bind.content_digest, preview=None, requester=PRINCIPAL,
        grant_id=GRANT, grant_path=GRANT_PATH, reason=None, content=bind.content)
    assert ap.binding is None and ap.content_hash == legacy
    assert action_binding_problem(ap, bind, legacy=(legacy,)) is None
    one_off = legacy[:-1] + ("1" if legacy[-1] != "1" else "2")
    assert action_binding_problem(ap, bind, legacy=(one_off,)) == "is for a different action"
    other = keyed(content={"operation": "x.like", "post": "p2"})
    assert action_binding_problem(ap, other, legacy=(legacy,)) == "is for different content"


def test_an_action_card_that_binds_no_content_covers_nothing(policy):
    """An action approval with a binding form but no content_digest is refused: it binds no content."""
    svc, _ = policy
    ap = svc.approvals.create_action(
        effect="commit", tool="connector.x.like", lane="biz/x", domain=None, summary="Like a post",
        content_hash=keyed().content_hash, preview=None, requester=PRINCIPAL, grant_id=GRANT,
        grant_path=GRANT_PATH, reason=None, binding="keyed")
    assert ap.content_digest is None
    assert action_binding_problem(ap, keyed()) == "binds no content"


def test_a_question_covers_no_action(policy):
    """A question approval is refused as an action: it is not an approval of an action."""
    svc, _ = policy
    q = svc.approvals.create_question(question="Ship it?", options=None, requester=PRINCIPAL,
                                      grant_id=GRANT, grant_path=GRANT_PATH)
    assert q.kind == "question"
    assert action_binding_problem(q, hello()) == "is not an approval of an action"


def test_an_action_binding_rejects_an_unknown_extra_field():
    """ActionBinding's extra holds only the stated cost and the routing facts; any other key raises ValueError."""
    with pytest.raises(ValueError, match="extra holds only"):
        ActionBinding(effect="commit", tool="t", lane=None, content={"text": "x"}, extra={"other": 1})


# ------------------------------------------------------------------ the policy gate


async def test_the_identical_commit_is_released_once_it_is_approved(policy):
    """The identical commit after the owner approves it: route approved, and the approval is used up."""
    svc, _ = policy
    ctx = ask_first_ctx(svc, "biz-a/insta")
    call = {"lane_id": "biz-a/insta", "summary": "Say hello", "content": {"text": "hello"}}
    ap_id = await commit_waits(ctx, **call)
    svc.approvals.decide(ap_id, "approve", by="owner:test")
    d = await ctx.authorize(Effect.COMMIT, **call)
    assert (d.route, d.via) == ("approved", ap_id)
    assert svc.approvals.get(ap_id).state == "consumed"


async def test_one_character_changed_in_content_waits_for_its_own_approval(policy):
    """Content {"text": "hellp"} after "hello" was approved: a new approval, and the first one stays approved."""
    svc, _ = policy
    ctx = ask_first_ctx(svc, "biz-a/insta")
    call = {"lane_id": "biz-a/insta", "summary": "Say hello", "content": {"text": "hello"}}
    ap_id = await commit_waits(ctx, **call)
    svc.approvals.decide(ap_id, "approve", by="owner:test")
    changed = await commit_waits(ctx, **{**call, "content": {"text": "hellp"}})
    assert changed != ap_id
    assert svc.approvals.get(ap_id).state == "approved"
    assert svc.approvals.get(changed).state == "pending"
    d = await ctx.authorize(Effect.COMMIT, **call)  # the approved content still goes through
    assert (d.route, d.via) == ("approved", ap_id)


async def test_one_character_changed_in_summary_waits_for_its_own_approval(policy):
    """The same content with the summary changed by one character: a new approval, and the first stays approved."""
    svc, _ = policy
    ctx = ask_first_ctx(svc, "biz-a/insta")
    call = {"lane_id": "biz-a/insta", "summary": "Say hello", "content": {"text": "hello"}}
    ap_id = await commit_waits(ctx, **call)
    svc.approvals.decide(ap_id, "approve", by="owner:test")
    changed = await commit_waits(ctx, **{**call, "summary": "Say hellp"})
    assert changed != ap_id
    assert svc.approvals.get(ap_id).state == "approved"


# ------------------------------------------------------------------ the brain co-sign


class RecordingCtx:
    """The ctx a brain's co-sign asks through: records each authorize, then asks the real policy context."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.calls: list[dict] = []

    async def authorize(self, effect, *, summary=None, content=None, **kw):
        self.calls.append({"effect": effect, "summary": summary, "content": content})
        return await self.ctx.authorize(effect, summary=summary, content=content, **kw)


def pay_ctx(svc):
    """An agent's context that may ask for a payment (it always waits for a person)."""
    _g, tok = svc.issue(subject={"id": "brain-caller"}, workspaces=["biz-a"], ceiling=Effect.IRREVERSIBLE)
    p, g = svc.resolve(tok)
    return svc.context(g, principal=p, tool="brains", token=tok)


async def metered_brains(tmp_path):
    """A brain service with the metered brain the co-sign names: a card names a registered brain only
    (c306a126, the yes covers that brain as it is now)."""
    brains = make_brain_service(tmp_path / "brains")
    await add_brain(brains, "anthropic-api", "metered", "anthropic")
    return brains


async def cosign(brains, ctx, grant, prompt: str) -> None:
    """The co-sign a metered brain needs before a task with `prompt` runs, at up to $1."""
    decision = SimpleNamespace(approval={"kind": "metered", "brains": ["anthropic-api"]}, reason="metered brain")
    await brains._cosign(decision, ctx, workspace="biz-a", budget=Budget(max_usd=1.0), grant=grant,
                         what="a brain task", content={"prompt": prompt})


async def test_cosign_prompts_one_character_apart_bind_different_content(tmp_path, policy):
    """Two co-sign asks whose prompt differs by one character each wait on their own card, bound to their own content."""
    svc, _ = policy
    brains = await metered_brains(tmp_path)
    rec = RecordingCtx(pay_ctx(svc))
    grant = SimpleNamespace(allow_metered=False, remaining_usd=None)
    with pytest.raises(ApprovalPending) as first:
        await cosign(brains, rec, grant, "Send the invoice")
    with pytest.raises(ApprovalPending) as second:
        await cosign(brains, rec, grant, "Send the invoise")
    assert first.value.approval_id != second.value.approval_id
    c1, c2 = rec.calls[0]["content"], rec.calls[1]["content"]
    assert (c1["prompt"], c2["prompt"]) == ("Send the invoice", "Send the invoise")
    cards = [svc.approvals.get(first.value.approval_id), svc.approvals.get(second.value.approval_id)]
    for card, content in zip(cards, (c1, c2), strict=True):
        assert card.content_digest == ActionBinding(effect="pay", tool="brains", lane=None,
                                                    content=content).content_digest
    assert cards[0].content_digest != cards[1].content_digest
    assert grant.allow_metered is False


async def test_approving_one_cosign_prompt_does_not_release_the_next(tmp_path, policy):
    """Approving the co-sign for one prompt releases that prompt only: the prompt one character away waits again."""
    svc, _ = policy
    brains = await metered_brains(tmp_path)
    rec = RecordingCtx(pay_ctx(svc))
    grant = SimpleNamespace(allow_metered=False, remaining_usd=None)
    with pytest.raises(ApprovalPending) as first:
        await cosign(brains, rec, grant, "Send the invoice")
    svc.approvals.decide(first.value.approval_id, "approve", by="owner:test", proof=PRESENT)
    with pytest.raises(ApprovalPending) as changed:
        await cosign(brains, rec, grant, "Send the invoise")
    assert changed.value.approval_id != first.value.approval_id
    assert grant.allow_metered is False
    await cosign(brains, rec, grant, "Send the invoice")  # the approved prompt is released
    assert grant.allow_metered is True
