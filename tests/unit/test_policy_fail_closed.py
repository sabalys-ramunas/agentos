"""Fail-closed audit of the authorization core (agentos.policy, 2026-11-07): each test injects the
failure a path used to read as "allow" and asserts the refusal, next to a control that shows the
ordinary path still works."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from test_policy_support import Clock, make_service

from agentos.core.errors import ApprovalPending, AuditUnwritable
from agentos.core.models import Effect
from agentos.policy import tokens
from agentos.policy.approvals import ApprovalPolicy, FourEyesRule
from agentos.policy.errors import Denied, TokenInvalid
from agentos.policy.grants import Grant, blocks_from_token
from agentos.policy.routing import PolicyConfig, load_policy_config
from agentos.policy.traces import TraceEvent

T0 = 1_700_000_000.0


@pytest.fixture
def svc(tmp_path):
    s, _ = make_service(tmp_path, clock=Clock(T0))
    yield s
    s.close()


def agent_ctx(svc):
    g, tok = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    p, g = svc.resolve(tok)
    return svc.context(g, principal=p, tool="browser_snapshot", token=tok)


# ---- PolicyContext.trace: a taint label that cannot be recorded ------------------------------

def test_a_read_whose_taint_cannot_be_recorded_is_refused(svc, monkeypatch):
    """A `web_content` trace labels the family untrusted, and the downgrade after untrusted text
    reads that label. A ledger write that fails used to be swallowed with the trace's own errors,
    so the call went on with a clean family and its next commit ran on its own."""
    ctx = agent_ctx(svc)

    def broken(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(svc.taint, "add", broken)
    with pytest.raises(AuditUnwritable):
        ctx.trace("web_content", host="evil.example", summary="read a page")
    with pytest.raises(AuditUnwritable):
        ctx.trace("custom", taint=["untrusted"])


def test_a_trace_row_that_cannot_be_written_is_still_only_logged(svc, monkeypatch):
    """The trace row itself stays a side effect (audit_health refuses acts while nothing is
    recorded), and a recorded label still lands before the row is tried."""
    ctx = agent_ctx(svc)

    def broken(*a, **k):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(svc.traces, "write", broken)
    ctx.trace("custom", summary="no label")  # does not raise
    ctx.trace("web_content", host="evil.example")  # the label is recorded, the row is not
    assert "untrusted" in svc.taint.get(ctx.taint_key)


# ---- _check_registered: an id another call registered first ----------------------------------

def _offline_child(tok: str, parent_id: str, ceiling: str) -> str:
    return tokens.append(tok, {"v": 2, "id": "gr_" + "r" * 16, "parent": parent_id, "iat": int(T0),
                               "ceiling": ceiling})


def test_an_offline_child_that_loses_the_registration_race_must_match_the_winner(svc, monkeypatch):
    """Two offline children with one block id, registered at once: the loser's insert fails, and it
    used to pass because a row with its id existed, without the check that the row holds its block."""
    g, tok = svc.issue(subject={"id": "x"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    winner = _offline_child(tok, g.id, "read")
    loser = _offline_child(tok, g.id, "commit")
    register = svc._register

    def race(sub, *, source, by):
        if sub.blocks[-1].id == "gr_" + "r" * 16 and svc.db.one("SELECT 1 FROM grants WHERE id = ?",
                                                                (sub.blocks[-1].id,)) is None:
            register(Grant(blocks_from_token(tokens.inspect(winner))), source=source, by=by)  # the other call
        register(sub, source=source, by=by)  # raises: the id is taken

    monkeypatch.setattr(svc, "_register", race)
    with pytest.raises(TokenInvalid):
        svc.resolve(loser)
    _, child = svc.resolve(winner)  # the winner's own block is what the registry holds
    assert child.ceiling == Effect.READ


def test_an_offline_child_registered_by_another_call_with_the_same_block_resolves(svc, monkeypatch):
    g, tok = svc.issue(subject={"id": "x"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    child_tok = _offline_child(tok, g.id, "read")
    register = svc._register

    def race(sub, *, source, by):
        register(sub, source=source, by=by)  # the other call registers this very block
        raise sqlite3.IntegrityError("UNIQUE constraint failed: grants.id")

    monkeypatch.setattr(svc, "_register", race)
    _, child = svc.resolve(child_tok)
    assert child.ceiling == Effect.READ


# ---- is_owner_token: a token that could not be checked ---------------------------------------

def test_a_token_that_cannot_be_checked_is_held_back_from_a_brain(svc, monkeypatch):
    """An admin grant is owner-equivalent and never handed to a brain. A database error while it is
    resolved used to read as "not the owner's", so the brain got it."""
    _, admin = svc.issue(subject={"id": "ops"}, workspaces=["biz-a"], admin=True)
    _, plain = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"])
    assert svc.is_owner_token(admin) is True and svc.is_owner_token(plain) is False

    def broken(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(svc, "resolve", broken)
    assert svc.is_owner_token(admin) is True
    assert svc.is_owner_token(plain) is True  # it cannot be told apart now: held back too

    def refused(*a, **k):
        raise Denied("revoked", "this grant was revoked")

    monkeypatch.setattr(svc, "resolve", refused)
    assert svc.is_owner_token(plain) is False  # a refused token acts as no one


# ---- TraceStore: a write that fails with something other than a SQLite error ------------------

class _UnlistablePath(type(Path())):
    """A spill path whose directory cannot be searched (chmod 000 on logs/, a failing disk)."""

    def exists(self, *a, **k):
        raise PermissionError(13, "Permission denied", str(self))


def _refuses_commit(svc) -> bool:
    g, _ = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    try:
        svc.precheck(g, Effect.COMMIT, lane_id="biz-a/x")
    except AuditUnwritable:
        return True
    return False


@pytest.mark.parametrize("failure", ["insert", "spill_path"])
def test_a_trace_write_that_fails_outside_sqlite_starts_the_fail_closed_clock(tmp_path, monkeypatch, failure):
    """Only sqlite3.Error used to reach _write_failed: any other error (the spill path's directory
    cannot be searched, a chain error) left the clock unset, so commits ran on while nothing was
    recorded, past audit_fail_closed_s."""
    clock = Clock(T0)
    svc, _ = make_service(tmp_path, clock=clock)
    try:
        traces = svc.traces
        if failure == "insert":
            def broken(*a, **k):
                raise RuntimeError("the chain state cannot be read")

            monkeypatch.setattr(traces, "_insert", broken)
        else:
            monkeypatch.setattr(traces, "spill_path", _UnlistablePath(traces.spill_path))

        def no_spill():
            raise OSError(30, "Read-only file system")

        monkeypatch.setattr(traces, "_spill", no_spill)
        traces.write(TraceEvent(kind="tool_start", tool="t", status="started"))  # never raises
        assert traces.unwritten_since == T0
        assert not _refuses_commit(svc)  # a blip: under the threshold
        clock.advance(svc.cfg.audit_fail_closed_s)
        assert _refuses_commit(svc)
    finally:
        monkeypatch.undo()
        svc.close()


# ---- four-eyes: an action that names no lane -------------------------------------------------

async def test_four_eyes_for_every_workspace_holds_an_action_with_no_lane(tmp_path):
    """A rule for "*" used to need a workspace to match, so a pay that named no lane was decided by
    the person behind the agent that asked for it."""
    cfg = PolicyConfig(approvals=ApprovalPolicy(four_eyes=[FourEyesRule(effects=["pay"])]))
    svc, _ = make_service(tmp_path, clock=Clock(T0), config=cfg)
    try:
        _, tok = svc.issue(subject={"id": "claude-code"}, workspaces=["biz-a"], ceiling=Effect.PAY)
        ctx = svc.context(svc.resolve(tok)[1], tool="t")
        with pytest.raises(ApprovalPending) as e:
            await ctx.authorize(Effect.PAY, content="pay it", summary="Pay", facts={"amount_usd": 5.0})
        ap = svc.approvals.get(e.value.approval_id)
        assert ap.workspace is None
        with pytest.raises(Denied, match="^four_eyes:"):
            svc.approvals.decide(ap.id, "approve", by="owner (cli)")  # the owner's own agent
        # a narrower rule still needs its workspace
        assert not FourEyesRule(workspace="biz-*", effects=["pay"]).matches(None, "pay")
        assert FourEyesRule(effects=["pay"]).matches(None, "pay")
    finally:
        svc.close()


# ---- routing rules: a misspelled key ----------------------------------------------------------

def test_a_routing_rule_with_a_misspelled_condition_is_refused_not_unconditional(tmp_path):
    (tmp_path / "policy.toml").write_text(
        '[routing]\nrules = [{effect = "exec", whn = {isolated = true}, then = "auto"}]\n')
    with pytest.raises(ValueError, match="whn"):
        load_policy_config(tmp_path)
    (tmp_path / "policy.toml").write_text(
        '[routing]\nrules = [{effect = "exec", when = {isolated = true}, then = "auto"}]\n')
    assert load_policy_config(tmp_path).routing.rules[0].when == {"isolated": True}
