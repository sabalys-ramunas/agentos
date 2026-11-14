"""Fail closed when the action log cannot be written (agentos.policy.audit_health).

A call above READ is refused with audit_unwritable once nothing has been recorded for
audit_fail_closed_s; reads keep working; the health monitor warns before that; a working
spill file keeps the trail, so the rule does not apply.
"""

from __future__ import annotations

import types

import pytest
from test_policy_support import Clock, make_service

from agentos.core.config import NodeConfig
from agentos.core.errors import ApprovalPending, AuditUnwritable
from agentos.core.models import Effect
from agentos.ops.health import HealthMonitor
from agentos.policy import audit_health

THRESHOLD = 120
NOW = 1_800_000_000.0


def fake(unwritten_since=None, db_failing_since=None, pending=0, error=None):
    return types.SimpleNamespace(unwritten_since=unwritten_since, db_failing_since=db_failing_since,
                                 pending_count=pending, last_write_error=error)


# ---------------------------------------------------------------- 1. the rule itself

def test_read_never_raises_even_far_past_the_threshold():
    audit_health.check(fake(unwritten_since=NOW), Effect.READ, NOW + 3600, THRESHOLD)


def test_commit_is_refused_at_the_threshold_and_later():
    for elapsed in (THRESHOLD, THRESHOLD + 1, THRESHOLD + 900):
        with pytest.raises(AuditUnwritable) as e:
            audit_health.check(fake(unwritten_since=NOW), Effect.COMMIT, NOW + elapsed, THRESHOLD)
        assert e.value.code == "audit_unwritable"


def test_commit_is_allowed_one_second_before_the_threshold():
    audit_health.check(fake(unwritten_since=NOW), Effect.COMMIT, NOW + THRESHOLD - 1, THRESHOLD)


def test_a_working_spill_file_keeps_the_trail_so_the_rule_does_not_apply():
    audit_health.check(fake(db_failing_since=NOW - 900), Effect.COMMIT, NOW, THRESHOLD)


@pytest.mark.parametrize("effect", [Effect.DRAFT, Effect.PAY, Effect.IRREVERSIBLE, Effect.EXEC, Effect.SECRET])
def test_every_effect_above_read_is_refused_the_specials_included(effect):
    with pytest.raises(AuditUnwritable) as e:
        audit_health.check(fake(unwritten_since=NOW), effect, NOW + THRESHOLD, THRESHOLD)
    assert e.value.code == "audit_unwritable"


# ---------------------------------------------------------------- 2. what the health monitor says

def test_state_and_finding_are_quiet_while_both_clocks_are_clear():
    assert audit_health.state(fake(), NOW, THRESHOLD)["level"] == "ok"
    assert audit_health.finding(fake(), NOW, THRESHOLD) is None


def test_a_failing_database_with_a_spill_file_warns_and_names_the_spill_file():
    traces = fake(db_failing_since=NOW - 10)
    assert audit_health.state(traces, NOW, THRESHOLD)["level"] == "warn"
    level, title, _detail = audit_health.finding(traces, NOW, THRESHOLD)
    assert level == "warn"
    assert "spill file" in title


def test_unwritten_rows_warn_before_the_rule_bites():
    traces = fake(unwritten_since=NOW - 10, pending=3)
    assert audit_health.state(traces, NOW, THRESHOLD)["level"] == "warn"
    level, _title, detail = audit_health.finding(traces, NOW, THRESHOLD)
    assert level == "warn"
    assert "3 actions wait in memory" in detail


def test_critical_at_the_threshold():
    level, _title, _detail = audit_health.finding(fake(unwritten_since=NOW - THRESHOLD), NOW, THRESHOLD)
    assert level == "critical"


def test_no_em_dash_in_any_finding_text():
    texts = []
    for traces in (fake(db_failing_since=NOW - 10),
                   fake(unwritten_since=NOW - 10, pending=3, error="disk full"),
                   fake(unwritten_since=NOW - THRESHOLD, error="database is locked")):
        texts.extend(audit_health.finding(traces, NOW, THRESHOLD)[1:])
    assert texts and all("\u2014" not in text for text in texts)


# ---------------------------------------------------------------- 3. through the real policy

@pytest.fixture
def env(tmp_path):
    clock = Clock()
    policy, _rt = make_service(tmp_path, clock=clock)  # the trace store reads the same clock
    yield policy, clock
    policy.close()


def agent_ctx(policy):
    g, tok = policy.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.IRREVERSIBLE)
    p, g = policy.resolve(tok)
    return policy.context(g, principal=p, tool="t", token=tok)


def stall(policy, clock, seconds=200):
    """The database and its spill file both refuse rows, since `seconds` ago."""
    policy.traces.unwritten_since = clock.t - seconds


async def test_a_stalled_log_refuses_commit(env):
    policy, clock = env
    ctx = agent_ctx(policy)
    stall(policy, clock)
    with pytest.raises(AuditUnwritable) as e:
        await ctx.authorize(Effect.COMMIT, lane_id="biz-a/x", content="post")
    assert e.value.code == "audit_unwritable"


async def test_a_stalled_log_still_lets_read_through(env):
    policy, clock = env
    ctx = agent_ctx(policy)
    stall(policy, clock)
    assert (await ctx.authorize(Effect.READ, lane_id="biz-a/x")).route == "auto"


async def test_the_rule_lifts_when_the_log_records_again(env):
    policy, clock = env
    ctx = agent_ctx(policy)
    stall(policy, clock)
    with pytest.raises(AuditUnwritable):
        await ctx.authorize(Effect.COMMIT, lane_id="biz-a/x", content="post")
    policy.traces.unwritten_since = None  # the database takes rows again
    try:
        await ctx.authorize(Effect.COMMIT, lane_id="biz-a/x", content="post")
    except ApprovalPending:
        pass  # routed to a person: it is no longer refused for the log


def test_precheck_refuses_once_the_clock_passes_the_threshold(env):
    policy, clock = env
    ctx = agent_ctx(policy)
    policy.traces.unwritten_since = clock.t
    policy.precheck(ctx.grant, Effect.COMMIT)
    clock.advance(THRESHOLD)
    with pytest.raises(AuditUnwritable):
        policy.precheck(ctx.grant, Effect.COMMIT)


# ---------------------------------------------------------------- 4. the health monitor

def test_the_health_monitor_is_ok_then_critical(env, tmp_path):
    policy, clock = env
    monitor = HealthMonitor(NodeConfig(home=tmp_path))
    monitor.policy = policy
    [quiet] = monitor.check_audit()
    assert (quiet.key, quiet.level) == ("audit.write", "ok")
    stall(policy, clock)
    [finding] = monitor.check_audit()
    assert (finding.key, finding.level) == ("audit.write", "critical")
    assert finding.data["threshold_s"] == THRESHOLD
