"""One home for the sets of page signals that stop work or need a person, and for the held codes.

Each engine used to write its own copy of these sets, with different members. They are gathered
here **with the members each module had**, so the differences sit side by side. Changing a set's
members changes what an engine does on a challenged account: that is an owner-reviewed change of
its own, never part of a move. `tests/unit/test_signals_pin.py` pins every set to its old literal.

The members are `Signal` values (a StrEnum), so `"captcha" in STOP` and `Signal.CAPTCHA in STOP`
both hold, and each member compares, hashes and serializes as its plain string.
"""

from __future__ import annotations

from agentos.core.models import Signal

# The account check's status for a lane signed into another account (agentos.node.sites.WRONG_ACCOUNT).
# Not a page signal, but holds and engage treat it like one.
WRONG_ACCOUNT = "wrong_account"

# ---- page signals ---------------------------------------------------------------------------------

# Every signal that says the page is not usable: stop the step and do not go on.
# Users: runner.proof.BAD_SIGNALS (and runner.schedules through it), stealth.lanecheck.BAD_SIGNALS,
# node.interaction.STOP_SIGNALS.
# The widest set: it is the only page-signal set that has all five of captcha, checkpoint,
# rate_limited, blocked and logged_out.
STOP: frozenset[Signal] = frozenset({Signal.CAPTCHA, Signal.CHECKPOINT, Signal.RATE_LIMITED, Signal.BLOCKED,
                                     Signal.LOGGED_OUT})

# Signals only a person can clear: the lane waits for a human to hand it back.
# Users: keepalive.planner.BAD_SIGNALS, api.lanes_view.NEEDS_HUMAN_SIGNALS, and node.holds through
# HOLD_FOR_HUMAN below.
# Differs from STOP: no rate_limited, because a rate limit is a timed rest (node.holds gives it a
# backoff), not a person's job.
NEEDS_HUMAN: frozenset[Signal] = frozenset({Signal.CAPTCHA, Signal.CHECKPOINT, Signal.BLOCKED, Signal.LOGGED_OUT})

# The signals node.holds turns into a needs_human hold, as plain strings: NEEDS_HUMAN plus a wrong account.
# Users: node.holds.HUMAN_SIGNALS.
HOLD_FOR_HUMAN: frozenset[str] = frozenset({
    Signal.CAPTCHA.value, Signal.CHECKPOINT.value, Signal.BLOCKED.value, Signal.LOGGED_OUT.value, WRONG_ACCOUNT,
})

# The page put a challenge in front of an agent working on it: give up the page.
# Users: pilot.loop.CHALLENGES, keepalive.visit.CHALLENGES.
# Differs from STOP: no logged_out. Both users check logged_out on its own: a keepalive visit stops
# as signed_out first, and the pilot holds only when the run was not handed a login.
CHALLENGE: frozenset[Signal] = frozenset({Signal.CAPTCHA, Signal.CHECKPOINT, Signal.BLOCKED, Signal.RATE_LIMITED})

# The signals that put a lane on the panel's "needs attention" summary.
# Users: api.lanes_view.ATTENTION_SIGNALS.
# Differs from STOP: no blocked. Differs from NEEDS_HUMAN: has rate_limited, no blocked.
ATTENTION: frozenset[Signal] = frozenset({Signal.CAPTCHA, Signal.CHECKPOINT, Signal.RATE_LIMITED, Signal.LOGGED_OUT})

# The signals the TUI shows as a lane's need (tui.state.lane_need and the demo's count, tui.demo._needs_you).
# Users: tui.state, tui.demo.
# Same members as NEEDS_HUMAN (checkpoint was missing until 2026-11-07, so a lane where the site asked
# the account holder a question never showed in the TUI's Needs you). tui.state.LANE_NEEDS has words for each.
TUI_NEED: frozenset[str] = frozenset({"captcha", "checkpoint", "logged_out", "blocked"})

# A vault login hands these to a person (outcome "human"). Users: vault.login.login_flow.
# Differs from STOP: no captcha, because the login checks captcha first and reports it as its own
# outcome ("captcha"). Taken together, the login stops on every STOP signal.
LOGIN_HUMAN: frozenset[Signal] = frozenset({Signal.CHECKPOINT, Signal.BLOCKED, Signal.RATE_LIMITED, Signal.LOGGED_OUT})

# A vault form step is stuck on these: take control, or try later. Users: vault.steps.read_step.
# Captcha is its own step kind there; checkpoint and logged_out are read from the form after this.
FORM_STUCK: frozenset[Signal] = frozenset({Signal.BLOCKED, Signal.RATE_LIMITED})

# The signals that never set a hold or count as a challenge: the page loaded, or nothing could be read.
# Users: node.lanes.report_signal, dev.lanes.report_signal, runner.siteapis.service.discover.
# Every other Signal is in STOP. runner.siteapis.cli.format_candidates also skips None, so it keeps its own.
QUIET: frozenset[Signal] = frozenset({Signal.OK, Signal.UNKNOWN})

# Signals that hold an engage surface and end its window: STOP plus a wrong account.
# Users: engage.ports.SIGNALS (and engage.adapters.signal_of through it).
ENGAGE_STOP: frozenset[str] = frozenset({"captcha", "checkpoint", "logged_out", "blocked", "rate_limited",
                                         WRONG_ACCOUNT})

# ---- signals and run error codes ---------------------------------------------------------------

# The run error code the runner's executor gives a step that met a page signal.
# Users: runner.executor.SIGNAL_CODES. Only the NEEDS_HUMAN signals have one: a rate limit ends a run
# as plain "rate_limited".
RUN_SIGNAL_CODES: dict[Signal, str] = {Signal.CAPTCHA: "signal_captcha", Signal.CHECKPOINT: "signal_checkpoint",
                                       Signal.LOGGED_OUT: "signal_logged_out", Signal.BLOCKED: "signal_blocked"}

# The run error codes above, as a set. Users: runner.siteapis.problems.SIGNALS (answers needs_human).
RUN_SIGNAL_CODE_SET: frozenset[str] = frozenset(RUN_SIGNAL_CODES.values())

# The engage executor's signal for a runner error code: the reverse of RUN_SIGNAL_CODES, plus the two
# codes engage reads as signals although the runner does not spell them signal_*.
# Users: engage.adapters.SIGNAL_CODES.
ENGAGE_SIGNAL_OF_CODE: dict[str, str] = {
    **{code: sig.value for sig, code in RUN_SIGNAL_CODES.items()},
    "rate_limited": "rate_limited",
    WRONG_ACCOUNT: WRONG_ACCOUNT,
}

# ---- held codes ---------------------------------------------------------------------------------
# Two different things used to share the name HELD_CODES.

# Run error codes after which engage holds the operation rather than failing it: the lane is pacing
# itself, or the connector is broken. Users: engage.adapters.HELD_CODES.
ENGAGE_HELD_CODES: frozenset[str] = frozenset({"pace_limited", "connector_broken"})

# Run error codes that put a connector's health in "held" (as a signal_* code does): the run met the
# wrong account or the wrong surface. Users: runner.connectors.health, runner.siteapis.problems.
CONNECTOR_HOLD_CODES: frozenset[str] = frozenset({WRONG_ACCOUNT, "wrong_surface"})
