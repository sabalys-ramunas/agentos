"""Every way a decision reaches an approval, and the level the node gives it (docs/CONFIRMATIONS.md).

Each row goes through the real route. The level comes from how the decision arrived: no caller
names it. The controller's rows are in test_channel_confirmations.py, chat's in test_confirm_chat.py.
"""

from __future__ import annotations

import asyncio

import pytest
from test_api_members import ORIGIN, join, owner, pending, role
from test_confirm_api import browser
from test_node_panel import TOKEN, Node, bearer
from typer.testing import CliRunner

from agentos.api.auth import PRESENCE_HEADER
from agentos.core.errors import ApprovalPending
from agentos.core.models import Effect, Principal
from agentos.core.ratelimit import RateLimiter
from agentos.policy.cli import approvals_app, confirmation_app
from agentos.policy.confirm import ConfirmLevel


@pytest.fixture
def node(tmp_path):
    n = Node(tmp_path)
    n.api_app.state.api.limiter = RateLimiter(per_key=10_000, global_max=10_000)
    yield n
    n.close()


def as_token(n, ap, token=TOKEN):
    return n.client().post(f"/api/approvals/{ap}/approve", headers=bearer(token), json={})


def recorded(n, ap):
    a = n.policy.approvals.get(ap)
    return (a.state, a.decided_method, a.decided_level, a.decided_via)


def with_passkey(person):
    """The owner adds a passkey, as they do in the panel after a code sign-in."""
    from soft_authenticator import SoftAuthenticator

    person.auth = SoftAuthenticator(ORIGIN)
    opts = person.post("/api/me/passkeys/options").json()["publicKey"]
    r = person.post("/api/me/passkeys", {"credential": person.auth.create(opts), "label": "laptop"})
    assert r.status_code == 200, r.text
    return person


def question(n, lane="biz-a"):
    g, _ = n.policy.issue(subject={"id": f"asker-{lane}"}, workspaces=[lane], ceiling="read")
    ap = n.policy.approvals.create_question(question="Which one?", options=None, requester=g.principal(),
                                            grant_id=g.id, grant_path=g.path, workspace=lane)
    return ap.id


# ---- the owner in the panel ----

def test_owner_session_is_signed_in(node):
    ap = pending(node)
    assert owner(node).post(f"/api/approvals/{ap}/approve").status_code == 200
    assert recorded(node, ap) == ("approved", "panel", "signed_in", "session")


def test_owner_session_is_below_what_a_payment_requires(node):
    ap = pending(node, effect=Effect.PAY)
    r = owner(node).post(f"/api/approvals/{ap}/approve")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "proof_too_weak" and r.json()["error"]["requires"] == "present"
    assert r.json()["error"]["approval_id"] == ap  # the check the panel asks for is for this one
    assert node.policy.approvals.get(ap).state == "pending"


def test_owner_fresh_passkey_check_for_this_approval_is_present_and_used_once(node):
    boss = with_passkey(owner(node))
    one, two = pending(node, effect=Effect.PAY, content="one"), pending(node, effect=Effect.PAY, content="two")
    boss.step_up(one)
    assert boss.post(f"/api/approvals/{two}/approve").json()["error"]["code"] == "proof_too_weak"
    assert boss.post(f"/api/approvals/{one}/approve").status_code == 200
    assert recorded(node, one) == ("approved", "passkey", "present", "session")
    assert boss.post(f"/api/approvals/{two}/approve").json()["error"]["code"] == "proof_too_weak"


def test_a_fresh_check_is_not_spent_on_an_approval_nothing_in_the_panel_could_reach(node):
    boss = with_passkey(owner(node))
    ap = pending(node, effect=Effect.PAY)
    node.policy.db.execute("UPDATE approvals SET requires = 'device_key' WHERE id = ?", (ap,))
    boss.step_up(ap)
    r = boss.post(f"/api/approvals/{ap}/approve")
    assert r.json()["error"]["code"] == "proof_too_weak" and r.json()["error"]["requires"] == "device_key"
    node.policy.db.execute("UPDATE approvals SET requires = 'present' WHERE id = ?", (ap,))
    assert boss.post(f"/api/approvals/{ap}/approve").status_code == 200


# ---- a member ----

def test_member_with_a_recent_check_is_signed_in(node):
    va = join(node, owner(node), "va", [role("approver")])
    ap = pending(node)
    assert va.post(f"/api/approvals/{ap}/approve").status_code == 200
    assert recorded(node, ap) == ("approved", "panel", "signed_in", "passkey")


def test_member_without_a_recent_check_is_not_helped_by_a_commit_that_chat_could_answer(node):
    """No weakening: `commit` requires only chat, and a member still needs the 15-minute check."""
    va = join(node, owner(node), "va", [role("approver")])
    ap = pending(node)
    assert node.policy.approvals.get(ap).requires == "chat"
    node.api_app.state.api.settings.step_up_window = 0
    r = va.post(f"/api/approvals/{ap}/approve")
    assert r.status_code == 403 and r.json()["error"]["code"] == "step_up_required", r.text


def test_member_pay_is_present_with_the_same_fresh_window_used_once(node):
    va = join(node, owner(node), "va", [role("approver", effects=["commit", "pay"], max_usd=50)])
    ap = pending(node, effect=Effect.PAY, facts={"amount_usd": 10})
    assert va.post(f"/api/approvals/{ap}/approve").json()["error"]["code"] == "proof_too_weak"
    va.step_up(ap)
    assert va.post(f"/api/approvals/{ap}/approve").status_code == 200
    assert recorded(node, ap) == ("approved", "passkey", "present", "passkey")


def test_member_fresh_check_goes_stale_after_two_minutes(node):
    va = join(node, owner(node), "va", [role("approver", effects=["commit", "pay"], max_usd=50)])
    ap = pending(node, effect=Effect.PAY, facts={"amount_usd": 10})
    va.step_up(ap)
    node.api_app.state.api.settings.step_up_fresh = 0
    assert va.post(f"/api/approvals/{ap}/approve").json()["error"]["code"] == "proof_too_weak"


def test_a_lowered_row_lets_the_member_window_decide_a_payment(node):
    node.policy.confirmation.change({"pay": {"level": "signed_in"}}, by="owner", role="owner")
    va = join(node, owner(node), "va", [role("approver", effects=["commit", "pay"], max_usd=50)])
    ap = pending(node, effect=Effect.PAY, facts={"amount_usd": 10})
    assert va.post(f"/api/approvals/{ap}/approve").status_code == 200
    assert recorded(node, ap) == ("approved", "panel", "signed_in", "passkey")


def test_a_raised_commit_row_needs_a_fresh_check_from_the_member(node):
    node.policy.confirmation.change({"commit": {"level": "present"}}, by="owner", role="owner")
    va = join(node, owner(node), "va", [role("approver")])
    ap = pending(node)
    r = va.post(f"/api/approvals/{ap}/approve")
    assert r.json()["error"]["code"] == "proof_too_weak" and r.json()["error"]["requires"] == "present"
    va.step_up(ap)
    assert va.post(f"/api/approvals/{ap}/approve").status_code == 200
    assert recorded(node, ap)[:3] == ("approved", "passkey", "present")


# ---- the node's CLI ----

@pytest.fixture
def cli(node, monkeypatch):
    monkeypatch.setenv("AGENTOS_HOME", str(node.config.home))
    return lambda *a: CliRunner().invoke(approvals_app, list(a))


def test_cli_is_signed_in(node, cli):
    ap = pending(node)
    assert cli("approve", ap).exit_code == 0
    assert recorded(node, ap) == ("approved", "cli", "signed_in", None)
    assert node.policy.approvals.get(ap).decided_by == "owner:cli"  # what the runner reads as a person


def test_cli_is_below_what_a_payment_requires_and_says_how_to_go_on(node, cli):
    ap = pending(node, effect=Effect.PAY)
    res = cli("approve", ap)
    assert res.exit_code == 1 and "proof_too_weak" in res.output and "agentos confirmation set" in res.output
    assert node.policy.approvals.get(ap).state == "pending"


def test_cli_batch_reports_each_approval_and_goes_on(node, cli):
    weak, ok = pending(node, effect=Effect.PAY), pending(node, content="fine")
    res = cli("batch", weak, ok)
    assert res.exit_code == 0 and "proof_too_weak" in res.output
    assert node.policy.approvals.get(weak).state == "pending"
    assert recorded(node, ok)[:3] == ("approved", "cli", "signed_in")


def test_cli_denies_a_payment(node, cli):
    ap = pending(node, effect=Effect.PAY)
    assert cli("deny", ap).exit_code == 0
    assert recorded(node, ap)[0] == "denied"


# ---- a bearer token: it proves no person, so it approves and answers nothing; it can still deny ----

def test_node_token_approves_nothing_and_its_deny_records_no_level(node):
    ap = pending(node)
    r = as_token(node, ap)
    assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"
    assert node.policy.approvals.get(ap).state == "pending"
    assert node.client().post(f"/api/approvals/{ap}/deny", headers=bearer(TOKEN), json={}).status_code == 200
    assert recorded(node, ap)[:3] == ("denied", None, None)


def test_admin_grant_approves_nothing_and_its_deny_records_no_level(node):
    ap = pending(node)
    admin = node.grant(subject={"id": "admin-bot"}, admin=True)
    r = as_token(node, ap, admin)
    assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"
    assert node.policy.approvals.get(ap).state == "pending"
    assert node.client().post(f"/api/approvals/{ap}/deny", headers=bearer(admin), json={}).status_code == 200
    assert recorded(node, ap)[:3] == ("denied", None, None)


def test_a_token_never_reaches_what_a_payment_requires(node):
    ap = pending(node, effect=Effect.PAY)
    r = as_token(node, ap)
    assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"
    assert node.policy.approvals.get(ap).state == "pending"


def test_a_token_is_no_person_and_its_own_family_cannot_deny_either(node):
    g, token = node.policy.issue(subject={"id": "own-admin"}, workspaces=["biz-a"], ceiling="pay", admin=True)
    ctx = node.policy.context(g, tool="lane_post")
    with pytest.raises(ApprovalPending) as e:
        asyncio.run(ctx.authorize(Effect.COMMIT, lane_id="biz-a/instagram", content="mine", summary="Mine"))
    ap = e.value.approval_id
    r = as_token(node, ap, token)
    assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"
    r = node.client().post(f"/api/approvals/{ap}/deny", headers=bearer(token), json={})
    assert r.status_code == 403 and "self_approval" in r.json()["error"]["message"]
    assert node.policy.approvals.get(ap).state == "pending"


# ---- what a caller sends never counts ----

@pytest.mark.parametrize("body", [{"level": "device_key"}, {"proof": {"method": "passkey", "level": "present"}},
                                  {"method": "passkey", "decided_level": "present", "requires": "chat"}])
def test_a_body_cannot_say_how_strong_the_yes_is(node, body):
    ap = pending(node, effect=Effect.PAY)
    assert owner(node).post(f"/api/approvals/{ap}/approve", body).json()["error"]["code"] == "proof_too_weak"
    r = node.client().post(f"/api/approvals/{ap}/approve", headers=bearer(TOKEN), json=body)
    assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"  # a token, whatever it sends
    assert node.policy.approvals.get(ap).state == "pending"


# ---- the push link ----

def test_push_link_denies_with_no_level_and_never_approves(node):
    ap = pending(node, effect=Effect.PAY)
    store = node.policy.approvals
    exp = int(store.clock() + 600)
    client = node.client()
    approve = client.post(f"/api/approvals/{ap}/action", params={"d": "approve", "e": exp,
                                                                 "s": store.sign_action(ap, "approve", exp)})
    assert approve.status_code == 403 and "approve_in_panel" in approve.text
    r = client.post(f"/api/approvals/{ap}/action", params={"d": "deny", "e": exp, "s": store.sign_action(ap, "deny", exp)})
    assert r.status_code == 200
    assert recorded(node, ap) == ("denied", None, None, "push_link")


# ---- denying never needs a level ----

def test_denying_a_payment_needs_no_level_anywhere(node):
    boss = owner(node)
    one, two = pending(node, effect=Effect.PAY, content="one"), pending(node, effect=Effect.PAY, content="two")
    assert boss.post(f"/api/approvals/{one}/deny").status_code == 200
    assert node.client().post(f"/api/approvals/{two}/deny", headers=bearer(TOKEN), json={}).status_code == 200
    assert recorded(node, one)[:3] == ("denied", "panel", "signed_in")
    assert recorded(node, two)[:3] == ("denied", None, None)


def test_a_member_with_no_recent_check_still_denies(node):
    va = join(node, owner(node), "va", [role("approver")])
    ap = pending(node)
    node.api_app.state.api.settings.step_up_window = 0
    assert va.post(f"/api/approvals/{ap}/deny").status_code == 200
    assert recorded(node, ap)[:3] == ("denied", "panel", None)


# ---- batch ----

def test_panel_batch_gives_each_approval_its_own_level(node):
    boss = owner(node)
    weak, ok = pending(node, effect=Effect.PAY), pending(node, content="fine")
    body = boss.post("/api/approvals/batch", {"ids": [weak, ok], "decision": "approve"}).json()
    by_id = {r["id"]: r for r in body["results"]}
    assert by_id[weak]["ok"] is False and by_id[weak]["error"]["code"] == "proof_too_weak"
    assert by_id[weak]["error"]["requires"] == "present"
    assert by_id[ok]["ok"] is True
    assert recorded(node, ok)[:3] == ("approved", "panel", "signed_in")


# ---- questions ----

def test_a_question_requires_signed_in_and_never_offers_chat(node):
    ap = node.policy.approvals.get(question(node))
    assert ap.requires == "signed_in" and "chat" not in ap.methods


def test_owner_answers_a_question_signed_in(node):
    q = question(node)
    assert owner(node).post(f"/api/questions/{q}/answer", {"answer": "this one"}).status_code == 200
    assert recorded(node, q)[:3] == ("approved", "panel", "signed_in")


def test_member_answers_with_a_recent_check_only(node):
    va = join(node, owner(node), "va", [role("approver")])
    q = question(node)
    assert va.post(f"/api/questions/{q}/answer", {"answer": "this one"}).status_code == 200
    assert recorded(node, q)[:3] == ("approved", "panel", "signed_in")
    node.api_app.state.api.settings.step_up_window = 0
    late = question(node)
    assert va.post(f"/api/questions/{late}/answer", {"answer": "x"}).json()["error"]["code"] == "step_up_required"


def test_a_token_cannot_answer_a_question(node):
    q = question(node)
    r = node.client().post(f"/api/questions/{q}/answer", headers=bearer(TOKEN), json={"answer": "this one"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"
    assert node.policy.approvals.get(q).state == "pending"


def test_cli_answers_a_question(node, cli):
    q = question(node)
    assert cli("approve", q, "--answer", "this one").exit_code == 0
    assert recorded(node, q)[:3] == ("approved", "cli", "signed_in")


# ---- what an approval keeps ----

def test_requires_and_methods_are_in_the_panels_answer(node):
    boss = owner(node)
    ap = pending(node, effect=Effect.PAY)
    item = boss.get(f"/api/approvals/{ap}").json()
    assert item["requires"] == "present" and item["methods"] == []
    with_passkey(boss)
    later = pending(node, effect=Effect.PAY, content="x")
    assert boss.get(f"/api/approvals/{later}").json()["methods"] == ["passkey"]


def test_a_setting_changed_later_cannot_lower_a_pending_approval(node):
    ap = pending(node, effect=Effect.PAY)
    node.policy.confirmation.change({"pay": {"level": "signed_in"}}, by="owner", role="owner")
    assert owner(node).post(f"/api/approvals/{ap}/approve").json()["error"]["requires"] == "present"
    assert node.policy.approvals.get(ap).requires == "present"


def test_a_setting_raised_later_does_not_move_a_pending_approval_either(node):
    ap = pending(node)
    node.policy.confirmation.change({"commit": {"level": "present"}}, by="owner", role="owner")
    assert owner(node).post(f"/api/approvals/{ap}/approve").status_code == 200


def test_the_retried_call_consumes_the_approval_as_before(node):
    g, _ = node.policy.issue(subject={"id": "retry"}, workspaces=["biz-a"], ceiling="commit")
    ctx = node.policy.context(g, tool="lane_post")

    async def call():
        return await ctx.authorize(Effect.COMMIT, lane_id="biz-a/instagram", content="again", summary="Again")

    with pytest.raises(ApprovalPending) as e:
        asyncio.run(call())
    assert owner(node).post(f"/api/approvals/{e.value.approval_id}/approve").status_code == 200
    assert asyncio.run(call()).route == "approved"


# ---- what requires reads ----

async def _ask(node, *, effect=Effect.COMMIT, labels=(), content="post it", facts=None, domain=None):
    g, _ = node.policy.issue(subject={"id": "reader"}, workspaces=["biz-a"], ceiling="pay")
    ctx = node.policy.context(g, tool="lane_post")
    for label in labels:
        ctx.taint(label, source="a page")
    with pytest.raises(ApprovalPending) as e:
        await ctx.authorize(effect, lane_id="biz-a/instagram", content=content, summary="Do it", facts=facts,
                            domain=domain)
    return node.policy.approvals.get(e.value.approval_id)


def test_a_commit_after_untrusted_text_never_takes_chat(node):
    ap = asyncio.run(_ask(node, labels=["untrusted"]))
    assert ap.follows and ap.requires == "signed_in" and "chat" not in ap.methods


def test_emptying_the_taint_downgrade_does_not_open_chat(tmp_path):
    (tmp_path / "policy.toml").write_text("[taint]\ndowngrade = []\n")
    from test_policy_support import Clock, make_service

    svc, _ = make_service(tmp_path, clock=Clock())
    svc.ask_first.set("lane", "biz-a/x", True, by="owner", person=True)
    g, _ = svc.issue(subject={"id": "reader"}, workspaces=["biz-a"], ceiling="commit")
    ctx = svc.context(g, tool="lane_post")
    ctx.taint("untrusted", source="a page")
    with pytest.raises(ApprovalPending) as e:
        asyncio.run(ctx.authorize(Effect.COMMIT, lane_id="biz-a/x", content="x", summary="Do it"))
    ap = svc.approvals.get(e.value.approval_id)
    assert ap.follows is None and ap.requires == "signed_in" and "chat" not in ap.methods
    svc.close()


def test_a_call_the_trifecta_stopped_requires_what_after_untrusted_says(node):
    node.policy.confirmation.change({"after_untrusted": {"level": "present"}}, by="owner", role="owner")
    ap = asyncio.run(_ask(node, effect=Effect.DRAFT, labels=["untrusted", "private"], domain="evil.example"))
    assert ap.requires == "present"


def test_a_clean_commit_can_be_answered_in_chat_and_the_agent_is_told(node):
    ap = asyncio.run(_ask(node))
    assert ap.requires == "chat" and "chat" in ap.methods
    assert ap.status()["can_answer_in_chat"] is True and "approval_confirm" in ap.status()["offer"]


def test_chat_turned_off_is_not_offered(node):
    node.policy.confirmation.change({"methods_off": ["chat"]}, by="owner", role="owner")
    ap = asyncio.run(_ask(node))
    assert ap.requires == "chat" and "chat" not in ap.methods and ap.status()["can_answer_in_chat"] is False


def test_a_long_or_withheld_preview_is_not_offered_in_chat(node):
    long = asyncio.run(_ask(node, content="x" * 3000))
    assert long.preview_cut() and "chat" not in long.methods
    withheld = asyncio.run(_ask(node, content={"password": "hunter2-hunter2"}))
    assert withheld.preview_cut() and "chat" not in withheld.methods


def test_an_agents_trigger_proposal_is_never_answered_in_chat(node):
    ap = node.policy.approvals.create_action(
        effect="commit", tool="trigger_propose", lane="biz-a/instagram", domain=None, summary="Create trigger",
        content_hash="h", preview="{}", requester=node.policy.owner_grant().principal(), grant_id=None,
        grant_path=None, reason=None, floor=ConfirmLevel.SIGNED_IN)
    assert ap.requires == "signed_in" and "chat" not in ap.methods


# ---- the migration ----

def test_an_old_database_gains_the_columns_and_a_waiting_approval_never_gets_weaker(tmp_path):
    from test_policy_support import Clock, make_service

    svc, _ = make_service(tmp_path, clock=Clock())
    g, _ = svc.issue(subject={"id": "old"}, workspaces=["biz-a"], ceiling="pay")
    ctx = svc.context(g, tool="lane_post")
    svc.ask_first.set("lane", "biz-a/x", True, by="owner", person=True)

    async def ask(effect, content):
        with pytest.raises(ApprovalPending) as e:
            await ctx.authorize(effect, lane_id="biz-a/x", content=content, summary="Do it")
        return e.value.approval_id

    commit, pay = asyncio.run(ask(Effect.COMMIT, "a")), asyncio.run(ask(Effect.PAY, "b"))
    decided = asyncio.run(ask(Effect.COMMIT, "c"))
    svc.approvals.decide(decided, "deny", by="owner")
    for column in ("requires", "methods", "decided_method", "decided_level", "decided_detail"):
        svc.db.execute(f"ALTER TABLE approvals DROP COLUMN {column}")
    svc.close()

    svc, _ = make_service(tmp_path, clock=Clock())
    old_commit, old_pay = svc.approvals.get(commit), svc.approvals.get(pay)
    assert old_commit.requires == "signed_in" and old_commit.methods == []  # never chat
    assert old_pay.requires == "present"
    assert svc.approvals.get(decided).requires is None
    assert svc.approvals.decide(commit, "approve", by="owner:cli").decided_method == "cli"
    svc.close()


# ---- the way out: a person applies a change to what already waits ----

async def pending_pay(n, content="pay this"):
    g, _token = n.policy.issue(subject={"id": "payer"}, workspaces=["biz-a"], ceiling="pay")
    ctx = n.policy.context(g, tool="lane_post")
    with pytest.raises(ApprovalPending) as e:
        await ctx.authorize(Effect.PAY, lane_id="biz-a/instagram", content=content, summary="Pay a thing")
    return e.value.approval_id, g


def lowered(n):
    n.policy.confirmation.change({"pay": {"level": "signed_in"}}, by="owner", role="owner")
    return n.policy.approvals


async def test_a_lower_setting_alone_leaves_what_waits_as_it_was(node):
    ap, _g = await pending_pay(node)
    assert lowered(node).get(ap).requires == "present"


async def test_applied_to_what_waits_a_lower_setting_frees_it(node):
    ap, g = await pending_pay(node)
    store = lowered(node)
    changed = store.apply_setting_to_waiting()
    assert [(c["id"], c["was"], c["now"]) for c in changed] == [(ap, "present", "signed_in")]
    assert store.get(ap).requires == "signed_in"
    assert store.find_pending(store.get(ap).content_hash, g.id).requires == "signed_in"  # what a retry is handed
    assert store.decide(ap, "approve", by="owner:cli").state == "approved"


async def test_it_never_goes_below_a_floor(node):
    """A floor is kept with the approval: a pay never takes chat, and neither does what followed untrusted
    text or what its creator held up, whatever the setting says now."""
    store = node.policy.approvals
    who = Principal(id="agent-1")

    def made(digit, **kw):
        return store.create_action(effect="commit", tool="post", lane="biz-a/instagram", domain=None, summary="A post",
                                   content_hash="sha256:" + digit * 64, preview="the post", requester=who, grant_id=None,
                                   grant_path=None, reason=None, content_digest="sha256:" + digit * 64, **kw).id

    node.policy.confirmation.change({"commit": {"level": "present"}}, by="owner", role="owner")
    untrusted, held, follows, plain = (made("1", untrusted=True), made("2", floor=ConfirmLevel.SIGNED_IN),
                                       made("3", follows=[{"kind": "web_page", "host": "x.example"}]), made("4"))
    node.policy.confirmation.change({"commit": {"level": "chat"}}, by="owner", role="owner")
    store.apply_setting_to_waiting()
    assert [store.get(a).requires for a in (untrusted, held, follows)] == ["signed_in"] * 3
    assert store.get(plain).requires == "chat"
    assert "chat" not in store.get(untrusted).methods and "chat" in store.get(plain).methods


async def test_what_follows_untrusted_text_keeps_the_row_the_owner_raised(node):
    store = node.policy.approvals
    node.policy.confirmation.change({"after_untrusted": {"level": "present"}}, by="owner", role="owner")
    ap = store.create_action(effect="commit", tool="post", lane="biz-a/instagram", domain=None, summary="A post",
                             content_hash="sha256:" + "5" * 64, preview="p", requester=Principal(id="agent-1"), grant_id=None,
                             grant_path=None, reason=None, content_digest="sha256:" + "5" * 64, untrusted=True)
    store.apply_setting_to_waiting()
    assert store.get(ap.id).requires == "present"


async def test_an_approval_made_before_levels_never_takes_chat(node):
    ap, _g = await pending_pay(node)
    store = lowered(node)
    node.policy.db.execute("UPDATE approvals SET effect = 'commit', requires_basis = NULL WHERE id = ?", (ap,))
    store.apply_setting_to_waiting()
    assert store.get(ap).requires == "signed_in"


async def test_a_decided_approval_is_left_alone(node):
    ap, _g = await pending_pay(node)
    store = node.policy.approvals
    store.decide(ap, "deny", by="owner:cli")
    lowered(node)
    assert store.apply_setting_to_waiting() == []
    assert store.get(ap).requires == "present"


async def test_a_controller_never_applies_anything_to_what_waits(node):
    """Its layer only raises, and an update of it leaves what waits as it was."""
    ap, _g = await pending_pay(node)
    store = lowered(node)
    node.policy.confirmation.apply_controller({"pay": {"level": "chat"}, "apply_to_waiting": True}, by="controller:org:x")
    assert store.get(ap).requires == "present"


async def test_the_setting_applied_is_the_one_in_force_a_controller_raise_included(node):
    ap, _g = await pending_pay(node)
    store = lowered(node)
    node.policy.confirmation.apply_controller({"pay": {"level": "present"}}, by="controller:org:x")
    assert store.apply_setting_to_waiting() == []
    assert store.get(ap).requires == "present"


async def test_the_panel_applies_it_and_says_so_in_activity(node):
    ap, _g = await pending_pay(node)
    boss = owner(node)
    r = boss.req("PUT", "/api/confirmation", {"pay": {"level": "signed_in"}, "apply_to_waiting": True})
    assert r.status_code == 200, r.text
    assert r.json()["applied_to_waiting"] == [{"id": ap, "was": "present", "now": "signed_in"}]
    assert "apply_to_waiting" not in node.policy.confirmation.own().model_dump()
    assert node.policy.approvals.get(ap).requires == "signed_in"
    events, _ = node.policy.traces.query(limit=20)
    assert any("waiting" in (e.summary or "") and "1 " in (e.summary or "") for e in events)


async def test_without_the_flag_the_panel_leaves_what_waits(node):
    ap, _g = await pending_pay(node)
    assert owner(node).req("PUT", "/api/confirmation", {"pay": {"level": "signed_in"}}).status_code == 200
    assert node.policy.approvals.get(ap).requires == "present"


async def test_the_flag_is_a_yes_or_a_no_and_a_change_is_still_needed(node):
    boss = owner(node)
    assert boss.req("PUT", "/api/confirmation", {"pay": {"level": "signed_in"}, "apply_to_waiting": "yes"}).status_code == 400
    assert boss.req("PUT", "/api/confirmation", {"apply_to_waiting": True}).status_code == 400


async def test_a_refused_change_applies_nothing(node):
    ap, _g = await pending_pay(node)
    node.policy.confirmation.change({"pay": {"level": "signed_in"}}, by="owner", role="owner")
    r = owner(node).req("PUT", "/api/confirmation", {"pay": {"level": "chat"}, "apply_to_waiting": True})
    assert r.status_code == 403
    assert node.policy.approvals.get(ap).requires == "present"


def test_a_bearer_token_never_reaches_it(node):
    with browser(node) as c:
        r = c.put("/api/confirmation", headers=bearer(TOKEN), json={"pay": {"level": "signed_in"}, "apply_to_waiting": True})
    assert r.status_code == 403


async def test_the_cli_applies_it_and_says_so_in_activity(node, monkeypatch):
    ap, _g = await pending_pay(node)
    monkeypatch.setenv("AGENTOS_HOME", str(node.config.home))
    res = CliRunner().invoke(confirmation_app, ["set", "pay.level", "signed_in", "--apply-to-waiting"])
    assert res.exit_code == 0, res.output
    assert "1 waiting" in res.output
    assert node.policy.approvals.get(ap).requires == "signed_in"
    events, _ = node.policy.traces.query(limit=20)
    assert any("waiting" in (e.summary or "") for e in events)


async def test_the_cli_says_nothing_of_what_waits_without_the_flag(node, monkeypatch):
    ap, _g = await pending_pay(node)
    monkeypatch.setenv("AGENTOS_HOME", str(node.config.home))
    assert CliRunner().invoke(confirmation_app, ["set", "pay.level", "signed_in"]).exit_code == 0
    assert node.policy.approvals.get(ap).requires == "present"


# ---- the node's one-time code: a person for that one request (docs/THREAT-MODEL.md) ----

@pytest.mark.parametrize("decision", ["approve", "deny"])
def test_the_node_token_with_a_one_time_code_decides_as_the_nodes_cli(node, decision):
    ap = pending(node)
    node.store.add_code(f"cli-{decision}", ttl_seconds=60)
    r = node.client().post(f"/api/approvals/{ap}/{decision}", json={},
                           headers={**bearer(TOKEN), PRESENCE_HEADER: f"cli-{decision}"})
    assert r.status_code == 200, r.text
    assert (r.json()["decided_level"], r.json()["decided_method"], r.json()["decided_via"]) == \
        ("signed_in", "cli", "cli"), r.text
    state = {"approve": "approved", "deny": "denied"}[decision]
    assert recorded(node, ap) == (state, "cli", "signed_in", "cli")


def test_the_node_token_with_a_one_time_code_is_traced_as_a_person(node):
    ap = pending(node)
    node.store.add_code("cli-traced", ttl_seconds=60)
    r = node.client().post(f"/api/approvals/{ap}/approve", json={},
                           headers={**bearer(TOKEN), PRESENCE_HEADER: "cli-traced"})
    assert r.status_code == 200, r.text
    decided = [e for e in node.policy.traces.query(kind="panel", tool="panel.approval.approve", limit=20)[0]
               if (e.args or {}).get("approval") == ap]
    assert decided, "no panel trace for the approval"
    assert decided[0].args["presence"] == "person"
