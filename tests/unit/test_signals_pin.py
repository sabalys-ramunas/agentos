"""Pins every module's signal set and held codes to the literal it had before agentos.core.signals.

Each old literal is written out here as it was in its module. A failure means a set's members
changed: that changes what an engine does on a challenged account, and needs the owner's yes.
"""

from __future__ import annotations

import importlib

import pytest

from agentos.core.models import Signal

S = Signal

# (module, name, the old literal)
PINS = [
    ("agentos.runner.proof", "BAD_SIGNALS",
     frozenset({S.CAPTCHA, S.CHECKPOINT, S.RATE_LIMITED, S.BLOCKED, S.LOGGED_OUT})),
    ("agentos.stealth.lanecheck", "BAD_SIGNALS",
     (S.CAPTCHA, S.CHECKPOINT, S.RATE_LIMITED, S.BLOCKED, S.LOGGED_OUT)),
    ("agentos.node.interaction", "STOP_SIGNALS",
     frozenset({S.CAPTCHA, S.CHECKPOINT, S.LOGGED_OUT, S.BLOCKED, S.RATE_LIMITED})),
    ("agentos.keepalive.planner", "BAD_SIGNALS",
     frozenset({S.CAPTCHA, S.CHECKPOINT, S.BLOCKED, S.LOGGED_OUT})),
    ("agentos.keepalive.visit", "CHALLENGES",
     frozenset({S.CAPTCHA, S.CHECKPOINT, S.BLOCKED, S.RATE_LIMITED})),
    ("agentos.node.holds", "HUMAN_SIGNALS",
     frozenset({S.CAPTCHA.value, S.CHECKPOINT.value, S.BLOCKED.value, S.LOGGED_OUT.value, "wrong_account"})),
    ("agentos.api.lanes_view", "NEEDS_HUMAN_SIGNALS", {"captcha", "checkpoint", "logged_out", "blocked"}),
    ("agentos.api.lanes_view", "ATTENTION_SIGNALS", {"captcha", "checkpoint", "rate_limited", "logged_out"}),
    ("agentos.pilot.loop", "CHALLENGES", frozenset({"captcha", "checkpoint", "blocked", "rate_limited"})),
    ("agentos.engage.ports", "SIGNALS",
     frozenset({"captcha", "checkpoint", "logged_out", "blocked", "rate_limited", "wrong_account"})),
    ("agentos.runner.siteapis.problems", "SIGNALS",
     frozenset({"signal_captcha", "signal_checkpoint", "signal_logged_out", "signal_blocked"})),
    ("agentos.engage.adapters", "HELD_CODES", frozenset({"pace_limited", "connector_broken"})),
    ("agentos.runner.connectors.health", "HELD_CODES", frozenset({"wrong_account", "wrong_surface"})),
]

# Sets that were written inline in a function: (where it was, the core.signals name, the old literal)
INLINE = [
    ("vault.login.login_flow", "LOGIN_HUMAN", (S.CHECKPOINT, S.BLOCKED, S.RATE_LIMITED, S.LOGGED_OUT)),
    ("vault.steps.read_step", "FORM_STUCK", (S.BLOCKED, S.RATE_LIMITED)),
    ("tui.state.lane_issues", "ATTENTION", ("captcha", "checkpoint", "rate_limited", "logged_out")),
    # checkpoint added on purpose (2026-11-07): a lane at a checkpoint needs the person most of all
    ("tui.state.lane_need", "TUI_NEED", ("captcha", "checkpoint", "logged_out", "blocked")),
    ("tui.demo._needs_you", "TUI_NEED", ("captcha", "checkpoint", "logged_out", "blocked")),
    ("node.lanes.report_signal", "QUIET", (S.OK, S.UNKNOWN)),
    ("dev.lanes.report_signal", "QUIET", (S.OK, S.UNKNOWN)),
    ("runner.siteapis.service.discover", "QUIET", (S.OK, S.UNKNOWN)),
]

MAPS = [
    ("agentos.runner.executor", "SIGNAL_CODES",
     {S.CAPTCHA: "signal_captcha", S.CHECKPOINT: "signal_checkpoint",
      S.LOGGED_OUT: "signal_logged_out", S.BLOCKED: "signal_blocked"}),
    ("agentos.engage.adapters", "SIGNAL_CODES",
     {"signal_captcha": "captcha", "signal_checkpoint": "checkpoint", "signal_logged_out": "logged_out",
      "signal_blocked": "blocked", "rate_limited": "rate_limited", "wrong_account": "wrong_account"}),
]


def _value(x):
    return getattr(x, "value", x)


@pytest.mark.parametrize(("module", "name", "old"), PINS, ids=[f"{m}.{n}" for m, n, _ in PINS])
def test_set_keeps_its_old_members(module, name, old):
    got = getattr(importlib.import_module(module), name)
    assert set(got) == set(old)
    # the members compare and hash as their plain strings, as the old literal's did
    assert {_value(x) for x in got} == {_value(x) for x in old}
    for member in old:
        assert member in got
        assert _value(member) in got
    for other in [*Signal, "wrong_account", "wrong_surface", "signal_captcha", None]:
        assert (other in got) == (other in old), other


@pytest.mark.parametrize(("where", "name", "old"), INLINE, ids=[w for w, _, _ in INLINE])
def test_inline_set_keeps_its_old_members(where, name, old):
    from agentos.core import signals

    got = getattr(signals, name)
    assert {_value(x) for x in got} == {_value(x) for x in old}
    for member in old:
        assert member in got
        assert _value(member) in got
    for other in [*Signal, "wrong_account", None]:
        assert (other in got) == (other in old), other


@pytest.mark.parametrize(("module", "name", "old"), MAPS, ids=[f"{m}.{n}" for m, n, _ in MAPS])
def test_map_keeps_its_old_entries(module, name, old):
    got = getattr(importlib.import_module(module), name)
    assert dict(got) == old
    assert {_value(k): _value(v) for k, v in got.items()} == {_value(k): _value(v) for k, v in old.items()}
    for key, value in old.items():
        assert got[_value(key)] == value


def test_engage_signal_values_are_plain_strings():
    """engage.adapters.signal_of returns these as OpResult.signal, stored and compared as text."""
    from agentos.engage.adapters import SIGNAL_CODES

    assert all(type(v) is str for v in SIGNAL_CODES.values())


def test_held_codes_have_their_new_names():
    from agentos.core import signals
    from agentos.engage import adapters
    from agentos.runner.connectors import health

    assert adapters.HELD_CODES is signals.ENGAGE_HELD_CODES
    assert health.HELD_CODES is signals.CONNECTOR_HOLD_CODES
    assert signals.ENGAGE_HELD_CODES.isdisjoint(signals.CONNECTOR_HOLD_CODES)


def test_every_module_set_comes_from_core_signals():
    """Each module's set is the one object in agentos.core.signals, not a copy."""
    from agentos.core import signals

    named = {id(v) for v in vars(signals).values() if isinstance(v, frozenset | dict)}
    for module, name, _ in PINS + MAPS:
        assert id(getattr(importlib.import_module(module), name)) in named, f"{module}.{name}"


def test_signal_sets_use_only_known_signals():
    from agentos.core import signals

    known = {s.value for s in Signal} | {signals.WRONG_ACCOUNT}
    for name in ("STOP", "NEEDS_HUMAN", "HOLD_FOR_HUMAN", "CHALLENGE", "ATTENTION", "ENGAGE_STOP", "TUI_NEED",
                 "LOGIN_HUMAN", "FORM_STUCK", "QUIET"):
        assert {_value(x) for x in getattr(signals, name)} <= known, name
    assert Signal.OK.value not in signals.STOP and Signal.UNKNOWN.value not in signals.STOP


def test_wrong_account_matches_the_account_check():
    from agentos.core.signals import WRONG_ACCOUNT
    from agentos.node.sites import WRONG_ACCOUNT as SITES_WRONG_ACCOUNT

    assert WRONG_ACCOUNT == SITES_WRONG_ACCOUNT
