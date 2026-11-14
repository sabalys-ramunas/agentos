"""The control actions a person takes on a lane (take, hand_back, stop_agent, deny_request), and
changes to a lane's site list, shared by the viewer, the owner API, the CLI and the channel.
"""

from __future__ import annotations

from typing import Any

from agentos.core.errors import InvalidRequest
from agentos.core.models import ControlState, LaneInfo, LaneState, validate_sites

ACTIONS = ("take", "hand_back", "stop_agent", "deny_request")
HANDBACK_NOTE_CHARS = 300
SITES_OPS = ("add", "remove", "set")
PERSON_ACTIONS = ("hand_back",)    # always a person's: it clears holds, quarantines and a person's stop


def person_needed(info: LaneInfo, action: Any) -> bool:
    """Does this control action need a person (docs/THREAT-MODEL.md)? A hand-back always: it clears a
    hold, a quarantine and a person's stop. Taking over too while the lane is held, stopped or
    quarantined, since a take there is how a held lane's browser gets used. Stopping an agent and
    keeping control (deny_request) only ever hold agents off, so any control link may."""
    if action in PERSON_ACTIONS:
        return True
    if action != "take":
        return False
    control = info.control
    return (control.state == ControlState.PAUSED or bool(control.reason) or bool(info.quarantine_reason)
            or info.state == LaneState.QUARANTINED)


def handback_warning(action: Any, info: LaneInfo) -> str | None:
    """What a person reads when their hand-back did not give agents the lane: the page still shows
    a challenge (the lane manager read it at the hand-back and held the lane again)."""
    control = info.control
    if action != "hand_back" or control.state == ControlState.AGENT or not control.reason:
        return None
    what = control.reason.split(" on ", 1)[0].removeprefix("needs_human:").replace("_", " ")
    return (f"Agents were not given the lane: the page still shows {what}. Agents never act on that page, "
            "so the lane is held again. Deal with it in the live view (or open another page), then hand back.")


CHALLENGES = frozenset({"captcha", "checkpoint", "logged_out", "blocked"})  # what only a person clears


def challenge_held(info: Any) -> bool:
    """The lane is held for a person (a `needs_human:` hold) and its last page still shows the challenge
    (a robot check, a sign-in, a checkpoint): handing back does not make that go away."""
    signal = getattr(info.signal, "value", info.signal) if getattr(info, "signal", None) else None
    reason = getattr(info.control, "reason", None) or getattr(info, "hold", None) or ""
    return signal in CHALLENGES and str(reason).startswith("needs_human")


async def apply_action(lanes: Any, lane_id: str, action: Any, *, by: str, note: Any = None,
                       done: Any = None) -> tuple[LaneInfo, bool]:
    """Run one control action. Returns the lane and whether control changed. `note` (hand_back
    only) is what the human tells the agents, at most HANDBACK_NOTE_CHARS.

    `done` (hand_back only, optional, for the web app): whether the person says the challenge that
    holds the lane (a robot check, a sign-in) is done. False keeps a challenge hold: agents stay off and
    the lane waits for the check. True or left out hands back as always: the lane manager reads the page
    at the hand-back and holds the lane again if it still shows the challenge (handback_warning)."""
    if action not in ACTIONS:
        raise InvalidRequest(f"action must be one of {', '.join(ACTIONS)}")
    if note is not None and (action != "hand_back" or not isinstance(note, str) or len(note) > HANDBACK_NOTE_CHARS):
        raise InvalidRequest(f"note goes with hand_back only, as text of at most {HANDBACK_NOTE_CHARS} characters")
    if done is not None and (action != "hand_back" or not isinstance(done, bool)):
        raise InvalidRequest("done goes with hand_back only, as true or false")
    current = lanes.get(lane_id)
    before = current.control
    if action == "hand_back" and done is False and challenge_held(current):
        # The check is still there: the person lets go, and the lane stays held for it.
        info = await lanes.stop_agent(lane_id, by=by) if before.state == ControlState.HUMAN else current
        return info, info.control != before
    if action == "take":
        info = await lanes.take_control(lane_id, by=by)
    elif action == "hand_back":
        info = await lanes.hand_back(lane_id, by=by, note=note) if note else await lanes.hand_back(lane_id, by=by)
    elif action == "stop_agent":
        info = await lanes.stop_agent(lane_id, by=by)
    else:
        info = await lanes.deny_request(lane_id, by=by)
    return info, info.control != before


async def change_sites(lanes: Any, lane_id: str, body: Any, *, by: str) -> dict[str, Any]:
    """Change a lane's site list on this manager, so a warm lane's guard has it at once:
    `{"op": "add" | "remove" | "set", "sites": [...], "strict"?: bool}`. `add` appends what is not
    there yet, `remove` refuses entries that are not on the list, `set` replaces it (`[]`: no lane
    restriction). Returns {lane, sites, strict, changed, running}."""
    if not isinstance(body, dict):
        raise InvalidRequest('send JSON like {"op": "add", "sites": ["reddit.com"]}')
    op, sites, strict = body.get("op", "add"), body.get("sites", []), body.get("strict")
    if op not in SITES_OPS:
        raise InvalidRequest(f"op must be one of {', '.join(SITES_OPS)}")
    if not isinstance(sites, list) or not all(isinstance(x, str) for x in sites):
        raise InvalidRequest("sites must be a list of domain patterns")
    if strict is not None and not isinstance(strict, bool):
        raise InvalidRequest("strict must be true or false")
    try:
        wanted = validate_sites(sites) if sites else []
    except ValueError as e:
        raise InvalidRequest(str(e)) from None
    info = lanes.get(lane_id)
    current = list(info.spec.sites)
    if op == "add":
        new = current + [x for x in wanted if x not in current]
    elif op == "remove":
        missing = [x for x in wanted if x not in current]
        if missing:
            raise InvalidRequest(f"not on the list: {', '.join(missing)}")
        new = [x for x in current if x not in wanted]
    else:
        new = wanted
    before = (current, info.spec.egress_strict)
    info = await lanes.set_sites(lane_id, new, strict=strict, by=by)
    return {"lane": lane_id, "sites": list(info.spec.sites), "strict": info.spec.egress_strict,
            "changed": (list(info.spec.sites), info.spec.egress_strict) != before,
            "running": info.state in (LaneState.WARM, LaneState.ACTIVE)}
