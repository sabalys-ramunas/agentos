"""What an agent reads about a paused lane (ControlState.PAUSED): who paused it, why, and what clears
it, in one place. The refusal of every tool (agent_paused), browser_control's `next`, browser_wait's
`next`, a flow's and a run's wait all word it here.

A pause is one of two kinds, and an agent that mixes them up tells its human something false:

- a person stopped agents (control.reason is empty, control.by is their principal id);
- agentos holds the lane itself (control.reason is the hold: `needs_human:captcha on x.com`,
  `backoff:rate_limited`, `quarantined:<why>`; control.by is "node", or "idle" when a person who took
  a held lane walked away and the hold came back). agentos.node.holds says which signal makes which hold.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# `by` of a pause agentos set itself (agentos.node.lanes.base: NODE_BY, IDLE_HANDBACK_BY, and "unknown")
NODE_BYS = frozenset({"node", "idle", "unknown", ""})

# needs_human:<signal>: what happened, and what the person does about it in the live view
NEEDS = {
    "captcha": ("a robot check (CAPTCHA) appeared", "solves it"),
    "checkpoint": ("the site asked the account holder to confirm it is them", "answers it"),
    "logged_out": ("the site signed this browser out", "signs in again"),
    "blocked": ("the site blocked this browser", "deals with what the site says"),
    "wrong_account": ("the browser is signed in to another account than the one it is for",
                      "signs in to the right account"),
}
BACKOFF = {"rate_limited": "the site limited how fast this account may act"}


def _parts(control: Any) -> tuple[str, str, str]:
    """(kind, what, where) of a hold reason: "needs_human", "captcha", "x.com"."""
    reason = str(getattr(control, "reason", None) or "").strip()
    head, _, where = reason.partition(" on ")
    kind, _, what = head.partition(":")
    return kind.strip(), what.strip(), where.strip()


def _by_agentos(control: Any) -> bool:
    return str(getattr(control, "by", None) or "").strip() in NODE_BYS


def _until(control: Any) -> str | None:
    until = getattr(control, "hold_until", None)
    return f"{until:%Y-%m-%d %H:%M} UTC" if isinstance(until, datetime) else None


def pause_why(lane_id: str, control: Any) -> str:
    """Who paused the lane, why, and what clears it, in plain words, with the hold's code in brackets
    (browser_control's `reason`, which the skills name)."""
    reason = str(getattr(control, "reason", None) or "").strip()
    if not reason:
        return f"A person stopped agents on {lane_id}. Nothing runs there until they hand it back."
    kind, what, where = _parts(control)
    on, until = (f" on {where}" if where else ""), _until(control)
    who = f"agentos paused {lane_id} itself (no person stopped you)" if _by_agentos(control) \
        else f"A person paused {lane_id}"
    code = reason.split(" on ", 1)[0]
    if kind == "needs_human" and what in NEEDS and not until:
        happened, does = NEEDS[what]
        return (f"{who}: {happened}{on} ({code}). Nothing runs there until the human {does} in the live view "
                "and hands the browser back.")
    if kind == "quarantined":
        return (f"{who}: it is quarantined ({what or 'it failed its self-check'}). Nothing runs there until a "
                "person checks it and hands it back.")
    limited = BACKOFF.get(what if kind == "backoff" else kind)
    why = f": {limited}{on} ({code})" if limited else f" ({reason})"
    if until:
        return f"{who}{why}. Nothing runs there until {until}; then it ends by itself."
    return f"{who}{why}. Nothing runs there until a person hands it back."


def paused_words(lane_id: str, control: Any) -> str:
    """The refusal and the `next` an agent reads on a paused lane: pause_why, then what the agent does."""
    why = pause_why(lane_id, control)
    if _until(control):
        return f"{why} Do not retry sooner, and do not ask again in a loop: wait with browser_wait_handback, or tell your user."
    if getattr(control, "reason", None) and _by_agentos(control):
        return f"{why} Tell your user that. Do not retry, and do not ask again in a loop: wait with browser_wait_handback."
    return f"{why} Do not retry, and do not ask again in a loop: wait with browser_wait_handback, or tell your user."
