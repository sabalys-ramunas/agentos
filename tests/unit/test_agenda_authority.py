"""Whose authority an agenda entry's run carries (openajar.agenda.service, "Whose authority a run
carries"): the drafter's grant; when it merely expired and a person approved the entry, a grant for
that one run derived from the approver's authority (PolicyService.approved_run_grant). Explicit
stops win: a revoked drafter's grant cancels its entries, a freeze fails them."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_agenda import FLOW, LANE, PARAMS, Env, ny

from openajar.agenda import tools as agenda_tools
from openajar.agenda.models import EntryState
from openajar.core.errors import InvalidRequest, PolicyDenied
from openajar.core.models import Effect
from openajar.runner.fakes import FakeClock

AT = ny(17, 11)          # 12 days out: the drafting agent's week-long grant has expired by then
MEMBER = "m_ana"


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path, FakeClock(ny(5, 9)))
    yield e
    e.agenda.close()
    e.policy.close()


async def _planned(env: Env, *, at=AT, ctx=None) -> dict:
    ctx = ctx or env.agent()
    out = await agenda_tools.agenda_draft(ctx, title="Launch post", action={"flow": FLOW, "params": PARAMS},
                                          when={"at": at.isoformat()}, propose=True)
    assert out["state"] == "proposed"
    return out


async def _approved(env: Env, *, member: bool = False) -> dict:
    out = await _planned(env)
    if member:
        env.policy.issue_member_grant(MEMBER, "biz-a", "approver")
        env.policy.decide(out["approval"]["id"], "approve", member_id=MEMBER, by="ana")
    else:
        env.policy.approvals.decide(out["approval"]["id"], "approve", by="owner")
    await env.tick()
    e = env.agenda.get(out["id"])
    assert e.state == EntryState.SCHEDULED
    return out


async def _fire(env: Env):
    env.clock.set(AT)
    await env.tick()
    return env.agenda.get


async def test_an_expired_drafter_grant_runs_once_under_the_owners_approval(env):
    out = await _approved(env)
    e = env.agenda.get(out["id"])
    assert e.approval["approver"] == {"kind": "owner", "id": "owner", "grant_id": "owner"}
    assert env.policy.grant_standing(e.grant_id)["state"] == "live"
    env.clock.set(AT)
    assert env.policy.grant_standing(e.grant_id)["state"] == "expired"
    await env.tick()
    e = env.agenda.get(out["id"])
    assert e.state == EntryState.DONE and len(env.posts()) == 1, e.reason
    run = env.h.runner.get(e.run_id)
    assert run.grant_id == e.authority["grant_id"] != e.grant_id and run.authorized
    assert run.principal["id"] == "claude-code"  # the run still names the agent that drafted it
    assert e.authority["approver"]["id"] == "owner" and "expired" in e.authority["why"]
    assert env.policy.grant_standing(run.grant_id)["state"] == "revoked"  # spent: its one run is over
    await env.tick()
    env.clock.set(AT + timedelta(minutes=10))
    await env.tick()
    assert len(env.posts()) == 1 and len(env.h.runner.list()) == 1


async def test_the_audit_row_names_the_drafting_agent_and_the_approver(env):
    out = await _approved(env, member=True)
    await _fire(env)
    e = env.agenda.get(out["id"])
    assert e.state == EntryState.DONE, e.reason
    rows, _ = env.policy.traces.query(kind="agenda_run")
    assert len(rows) == 1
    row = rows[0]
    assert row.principal == "claude-code" and row.principal_kind == "agent"
    assert row.grant_id == e.authority["grant_id"] and row.run_id == e.run_id
    assert row.args["approved_by"]["id"] == MEMBER and row.args["drafted_by"]["id"] == "claude-code"
    assert row.args["drafted_under"] == e.grant_id and MEMBER in row.summary and "claude-code" in row.summary
    assert env.policy.grant_record(e.authority["grant_id"])["subject"]["owner"] == MEMBER


async def test_a_removed_approver_means_no_run(env):
    out = await _approved(env, member=True)
    env.policy.revoke_member(MEMBER, reason="left the company")
    await _fire(env)
    e = env.agenda.get(out["id"])
    assert e.state == EntryState.FAILED and env.posts() == [] and env.h.runner.list() == []
    assert "drafted under expired" in e.reason and MEMBER in e.reason and "revoked" in e.reason
    assert e.authority is None


async def test_an_approver_whose_role_was_narrowed_means_no_run(env):
    out = await _approved(env, member=True)
    env.policy.issue_member_grant(MEMBER, "biz-a", "viewer")  # the role change revokes the approver grant
    await _fire(env)
    e = env.agenda.get(out["id"])
    assert e.state == EntryState.FAILED and env.posts() == [] and MEMBER in e.reason


async def test_a_revoked_drafter_grant_cancels_its_entries_and_never_runs(env):
    ctx = env.agent()
    waiting = await _planned(env, ctx=ctx)
    approved = await _planned(env, ctx=ctx, at=AT + timedelta(hours=2))
    env.policy.approvals.decide(approved["approval"]["id"], "approve", by="owner")
    await env.tick()
    assert env.agenda.get(approved["id"]).state == EntryState.SCHEDULED
    env.policy.revoke(ctx.grant.id, reason="the agent misbehaved")
    await env.tick()
    for out in (waiting, approved):
        e = env.agenda.get(out["id"])
        assert e.state == EntryState.CANCELLED and "was revoked" in e.reason and "the agent misbehaved" in e.reason
    assert env.policy.approvals.get(waiting["approval"]["id"]).state == "expired"  # withdrawn from Needs you
    env.clock.set(AT + timedelta(hours=2))
    await env.tick()
    assert env.posts() == [] and env.h.runner.list() == []


async def test_revoked_wins_over_expired(env):
    ctx = env.agent()
    env.policy.revoke(ctx.grant.id)
    env.clock.set(ny(14, 9))
    assert env.policy.grant_standing(ctx.grant.id)["state"] == "revoked"


class Frozen:
    def __init__(self, lane: str):
        self.lane = lane

    def frozen_for(self, lane_id):
        return SimpleNamespace(scope="lane", target=self.lane, reason="incident 42") if lane_id == self.lane else None

    def check_call(self, lane_id=None, *, tool=None):
        if lane_id == self.lane:
            raise PolicyDenied("frozen")


async def test_a_frozen_lane_runs_nothing_whoever_approved_it(env):
    out = await _approved(env)
    owners = await env.agenda.approve(env.agenda.draft(title="Owner's post", action={"flow": FLOW, "params": PARAMS},
                                                       when={"at": AT.isoformat()}).id)
    env.h.runtime.register("freeze", Frozen(LANE))
    await _fire(env)
    for entry_id in (out["id"], owners.id):
        e = env.agenda.get(entry_id)
        assert e.state == EntryState.FAILED and "frozen" in e.reason and "incident 42" in e.reason
    assert env.posts() == [] and env.h.runner.list() == []


async def test_an_entry_no_person_approved_still_needs_the_drafters_grant(env):
    env.policy.ask_first.set("flow", FLOW, False, by="owner", person=True)
    out = await agenda_tools.agenda_draft(env.agent(), title="Post", action={"flow": FLOW, "params": PARAMS},
                                          when={"at": AT.isoformat()}, propose=True)
    assert out["state"] == "scheduled" and out["approval"]["via"] == "auto"
    await _fire(env)
    e = env.agenda.get(out["id"])
    assert e.state == EntryState.FAILED and "expired" in e.reason and e.authority is None
    assert env.posts() == []


async def test_a_token_that_approves_is_no_approver(env):
    out = await _planned(env)
    other = env.agent()
    env.policy.approvals.decide(out["approval"]["id"], "approve", by="admin-bot", refuse_family=other.grant.root_id)
    await env.tick()
    e = env.agenda.get(out["id"])
    assert e.state == EntryState.SCHEDULED and "approver" not in e.approval
    await _fire(env)
    assert env.agenda.get(out["id"]).state == EntryState.FAILED and env.posts() == []


async def test_the_one_run_grant_carries_that_run_only(env):
    approver = {"kind": "owner", "id": "owner", "grant_id": "owner"}
    key = "agenda:ag_000000000001:abc"
    g = env.policy.approved_run_grant(approver=approver, entry="ag_000000000001", flow_id=FLOW,
                                      version=env.h.runner.flows.get(FLOW).version, lane=LANE, target=LANE,
                                      params=PARAMS, key=key, effect="commit", not_before=None,
                                      not_after=(env.clock.now() + timedelta(hours=1)).timestamp(),
                                      drafted_under=None)
    assert g.depth == 0 and g.ceiling == Effect.COMMIT and g.subject.id == "agenda:ag_000000000001"
    runner = env.h.runner
    with pytest.raises((PolicyDenied, InvalidRequest)):  # another lane
        runner.submit(flow_id=FLOW, params=PARAMS, lane="biz-a/other", idempotency_key=key, grant_id=g.id,
                      authorized=True)
    with pytest.raises(PolicyDenied, match="inputs"):  # other inputs
        runner.submit(flow_id=FLOW, params={**PARAMS, "caption": "something else"}, lane=LANE,
                      idempotency_key=key, grant_id=g.id, authorized=True)
    with pytest.raises(PolicyDenied, match="one approved run"):  # a second run under another key
        runner.submit(flow_id=FLOW, params=PARAMS, lane=LANE, idempotency_key="another", grant_id=g.id,
                      authorized=True)
    run, created = runner.submit(flow_id=FLOW, params=PARAMS, lane=LANE, idempotency_key=key, grant_id=g.id,
                                 authorized=True)
    again, created_again = runner.submit(flow_id=FLOW, params=PARAMS, lane=LANE, idempotency_key=key,
                                         grant_id=g.id, authorized=True)
    assert created and not created_again and again.id == run.id  # the same key finds the same run
    assert g.pins() and not g.pins_allow("flow_run", "sha256:" + "0" * 64)  # its pins open no MCP tool


async def test_a_member_cannot_stand_behind_more_than_their_role_covers(env):
    env.policy.issue_member_grant(MEMBER, "biz-a", "approver", {"lanes": ["biz-a/other"]})
    g = env.policy.member_grants(MEMBER)[0]
    approver = {"kind": "member", "id": MEMBER, "grant_id": g.id}
    assert "may no longer approve" in env.policy.approver_problem(approver, "commit", LANE, "biz-a")
    assert env.policy.approver_problem(approver, "commit", "biz-a/other", "biz-a") is None
    assert env.policy.approver_problem(approver, "pay", "biz-a/other", "biz-a") is not None  # no max_usd


async def test_an_unknown_drafter_grant_mints_no_run_grant(env):
    """Fail closed: without the drafter's stored chain its sites and egress list are unknown, so no
    one-run grant is minted (it would reach sites the agent never could)."""
    approver = {"kind": "owner", "id": "owner", "grant_id": "owner"}
    with pytest.raises(PolicyDenied, match="not known on this node"):
        env.policy.approved_run_grant(approver=approver, entry="ag_000000000002", flow_id=FLOW,
                                      version=env.h.runner.flows.get(FLOW).version, lane=LANE, target=LANE,
                                      params=PARAMS, key="agenda:ag_000000000002:abc", effect="commit",
                                      not_before=None, not_after=(env.clock.now() + timedelta(hours=1)).timestamp(),
                                      drafted_under="gr_doesnotexist")
