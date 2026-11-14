"""How long a request for an agenda entry waits in Needs you (agentos.agenda.service.request_expiry):
until the entry's start plus its grace, never sooner than an ordinary request, never later than
agenda_approval_max_days after proposing. Other approvals keep the node's approval time."""

from __future__ import annotations

from datetime import timedelta

import pytest
from test_agenda import FLOW, LANE, PARAMS, Env, _proposed, ny

from agentos.agenda import tools as agenda_tools
from agentos.agenda.models import EntryState
from agentos.core.config import NodeConfig
from agentos.core.errors import ApprovalPending
from agentos.core.models import Effect
from agentos.runner.fakes import FakeClock

DAY = timedelta(days=1)


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path, FakeClock(ny(5, 9)))
    yield e
    e.agenda.close()
    e.policy.close()


def _long_agent(env: Env):
    """An agent whose grant lives until the entry fires (a grant lasts a week by default)."""
    _, tok = env.policy.issue(subject={"id": "claude-code"}, workspaces=["biz-a"], ceiling=Effect.IRREVERSIBLE,
                              ttl_s=60 * 86400)
    p, g = env.policy.resolve(tok)
    return env.policy.context(g, principal=p, tool="agenda_draft", token=tok)


def _expires(env: Env, entry: dict) -> float:
    return env.policy.approvals.get(entry["approval"]["id"]).expires_at


async def test_a_request_for_an_entry_two_weeks_out_is_still_open_after_three_days_and_approvable(env):
    at = ny(17, 11)  # 12 days ahead, inside the 14 day cap
    out = await _proposed(env, when={"at": at.isoformat()}, ctx=_long_agent(env))
    assert _expires(env, out) == (at + timedelta(minutes=30)).timestamp()  # its start plus the 30 minute grace
    assert out["approval"]["expires_at"] == (at + timedelta(minutes=30)).isoformat()
    assert out["approval"]["capped"] is False and "stays open until" in out["next"]
    env.clock.set(ny(8, 9))
    await env.tick()
    assert env.policy.approvals.get(out["approval"]["id"]).state == "pending"
    assert env.agenda.get(out["id"]).state == EntryState.PROPOSED
    env.policy.approvals.decide(out["approval"]["id"], "approve", by="owner")
    await env.tick()
    e = env.agenda.get(out["id"])
    assert e.state == EntryState.SCHEDULED and e.approval["via"] == "person"
    env.clock.set(at)
    await env.tick()
    e = env.agenda.get(out["id"])
    assert e.state == EntryState.DONE and env.h.runner.get(e.run_id).authorized  # authorized again when it fired


async def test_a_window_waits_until_its_not_before(env):
    nb = ny(12, 8)
    out = await _proposed(env, when={"not_before": nb.isoformat(), "not_after": ny(14, 20).isoformat()})
    assert _expires(env, out) == (nb + timedelta(minutes=30)).timestamp()


async def test_a_near_entry_waits_at_least_as_long_as_an_ordinary_request(env):
    out = await _proposed(env)  # at 11:00 today, two hours ahead
    ap = env.policy.approvals.get(out["approval"]["id"])
    assert ap.expires_at - ap.created_at == env.policy.approvals.approval_ttl_s == 86400


async def test_an_entry_later_than_the_cap_gets_the_cap_and_goes_back_to_draft_when_it_passes(env):
    at = ny(5, 11) + timedelta(days=40)
    agent = _long_agent(env)
    out = await _proposed(env, when={"at": at.isoformat()}, ctx=agent)
    assert _expires(env, out) == (ny(5, 9) + 14 * DAY).timestamp()
    assert out["approval"]["capped"] is True
    env.clock.set(ny(5, 9) + 14 * DAY - timedelta(minutes=1))
    await env.tick()
    assert env.agenda.get(out["id"]).state == EntryState.PROPOSED
    env.clock.set(ny(5, 9) + 14 * DAY)
    await env.tick()
    e = env.agenda.get(out["id"])
    assert e.state == EntryState.DRAFT and e.approval["state"] == "expired"
    assert "within the 14 days" in e.reason and "agenda_approval_max_days" in e.reason
    assert "closer to its date" in e.reason
    again = await agenda_tools.agenda_propose(agent, id=out["id"])
    assert again["state"] == "proposed" and again["approval"]["id"] != out["approval"]["id"]
    assert _expires(env, again) == (ny(5, 9) + 28 * DAY).timestamp()  # 14 days from proposing again


async def test_as_soon_as_approved_keeps_the_cap(env):
    out = await _proposed(env, when={})
    assert _expires(env, out) == (ny(5, 9) + 14 * DAY).timestamp() and out["approval"]["capped"] is True


async def test_the_cap_is_a_node_setting(env):
    env.h.runtime.register("config", NodeConfig(home=env.tmp, agenda_approval_max_days=3))
    out = await _proposed(env, when={"at": ny(17, 11).isoformat()})
    assert _expires(env, out) == (ny(5, 9) + 3 * DAY).timestamp()
    with pytest.raises(ValueError, match="agenda_approval_max_days"):
        NodeConfig(agenda_approval_max_days=0)
    with pytest.raises(ValueError, match="agenda_approval_max_days"):
        NodeConfig(agenda_approval_max_days=91)


async def test_an_edit_still_withdraws_a_long_request(env):
    out = await _proposed(env, when={"at": ny(17, 11).isoformat()})
    edited = await env.agenda.update(out["id"], action={"params": {"caption": "changed"}})
    assert env.policy.approvals.get(out["approval"]["id"]).state == "expired"
    assert edited.state == EntryState.PROPOSED and edited.approval["id"] != out["approval"]["id"]


async def test_other_approvals_keep_the_default_time(env):
    ctx = env.agent(tool="flow_run")
    content = env.h.runner.flows.approval_content(env.h.runner.flows.get(FLOW), LANE, PARAMS)
    with pytest.raises(ApprovalPending) as pending:
        await ctx.authorize(Effect.COMMIT, lane_id=LANE, summary="Post on social", content=content)
    ap = env.policy.approvals.get(pending.value.approval_id)
    assert ap.expires_at - ap.created_at == 86400


async def test_an_explicit_expiry_is_bounded(env):
    from agentos.policy.approvals import MAX_REQUEST_S

    ctx = env.agent(tool="flow_run")
    content = env.h.runner.flows.approval_content(env.h.runner.flows.get(FLOW), LANE, PARAMS)
    now = env.clock.now().timestamp()
    with pytest.raises(ApprovalPending) as pending:
        await ctx.authorize(Effect.COMMIT, lane_id=LANE, summary="Post far ahead", content=content,
                            approval_expires_at=now + 400 * 86400)
    assert env.policy.approvals.get(pending.value.approval_id).expires_at == now + MAX_REQUEST_S
    with pytest.raises(ApprovalPending) as pending:  # a time already past is ignored: the default applies
        await ctx.authorize(Effect.COMMIT, lane_id=LANE, summary="Post in the past", content=content,
                            approval_expires_at=now - 60)
    assert env.policy.approvals.get(pending.value.approval_id).expires_at == now + 86400
