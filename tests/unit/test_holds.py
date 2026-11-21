"""Lane holds after a bad signal (openajar.node.holds), pure functions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from openajar.core.models import Signal
from openajar.node import holds
from openajar.node.holds import RATE_LIMIT_BACKOFF, Hold, hold_for, hold_for_report
from openajar.node.sites import AccountCheck, SignalReport

NOW = datetime(2026, 11, 4, 3, 0, tzinfo=UTC)


@pytest.mark.parametrize("signal", [Signal.CAPTCHA, Signal.CHECKPOINT, Signal.BLOCKED, Signal.LOGGED_OUT,
                                    "wrong_account", "checkpoint"])
def test_needs_human_signals_hold_until_a_human_hands_back(signal):
    hold = hold_for(signal, now=NOW, detail="instagram challenge URL")
    value = getattr(signal, "value", signal)
    assert hold == Hold(value, f"needs_human:{value}", NOW, None, "instagram challenge URL")
    assert hold.needs_human


def test_rate_limited_holds_itself_for_the_backoff_and_never_sooner():
    hold = hold_for(Signal.RATE_LIMITED, now=NOW)
    assert hold.reason == "backoff:rate_limited" and hold.until == NOW + timedelta(hours=6)
    assert RATE_LIMIT_BACKOFF == timedelta(hours=6) and not hold.needs_human
    assert hold_for(Signal.RATE_LIMITED, now=NOW, backoff=timedelta(minutes=5)).until == NOW + RATE_LIMIT_BACKOFF
    assert hold_for(Signal.RATE_LIMITED, now=NOW, backoff=timedelta(hours=24)).until == NOW + timedelta(hours=24)


@pytest.mark.parametrize("signal", [Signal.OK, Signal.UNKNOWN, "ok", "something_else"])
def test_good_or_unknown_signals_hold_nothing(signal):
    assert hold_for(signal, now=NOW) is None


def _wrong() -> AccountCheck:
    return AccountCheck("instagram", "555", "cookie ds_user_id", "wrong_account", "444")


def test_hold_for_report_puts_a_human_signal_first_then_a_wrong_account_then_a_backoff():
    assert hold_for_report(None, now=NOW) is None
    assert hold_for_report(SignalReport(Signal.OK), now=NOW) is None
    wrong = SignalReport(Signal.OK, "wrong account: ...", account=_wrong())
    assert hold_for_report(wrong, now=NOW).reason == "needs_human:wrong_account"
    limited_wrong = SignalReport(Signal.RATE_LIMITED, "HTTP 429", account=_wrong())
    assert hold_for_report(limited_wrong, now=NOW).reason == "needs_human:wrong_account"
    checkpoint_wrong = SignalReport(Signal.CHECKPOINT, "instagram challenge URL", account=_wrong())
    got = hold_for_report(checkpoint_wrong, now=NOW)
    assert got.reason == "needs_human:checkpoint" and got.detail == "instagram challenge URL"
    assert hold_for_report(SignalReport(Signal.RATE_LIMITED, "HTTP 429"), now=NOW).until == NOW + RATE_LIMIT_BACKOFF
    matched = AccountCheck("instagram", "555", "cookie ds_user_id", "match", "555")
    assert hold_for_report(SignalReport(Signal.OK, account=matched), now=NOW) is None


def test_one_rule_for_holds_lives_in_the_lane_manager():
    """A hold is a control with a reason, written by LaneManager.hold() with by=NODE_BY ("node")
    for the node's own holds, quarantines included. This module only says which hold a signal calls
    for; it has no second `by` value and no second predicate or merge to drift from the manager's."""
    for name in ("HOLD_BY", "is_hold", "apply_hold", "release_due"):
        assert not hasattr(holds, name), name
