"""The learn window, reached from the runtime and a lane id (docs/design/learn-a-site.md section 4).

The profiles service (node code, never a tool argument) opens a window on a lane's guard while an
agent edits one profile field there; the window releases exactly one request (LaneGuard.open_learn
says which), and learn_result() then gives its shape, which the node turns into the connector's
commit request and a learned rule (agentos.node.learned_rules).

How a lane's guard is reached: the runtime's lane manager (`runtime.get("lanes")`, a
LaneManagerImpl) has `live_session(lane_id)`, the browser session of a warm lane; the camoufox
session carries its guard as `session.guard`, and egress.guard_for(session.context) finds it for any
other session. A cold lane has no guard, so no window (open the lane first).

The pinned account is read from the lane's spec (`lanes.get(lane_id).spec.account`) when the request
comes, so a lane re-pinned during the window is judged by its new pin. A lane a pilot run (lane_do)
is driving gets no window, and a window refuses while one is.
"""

from __future__ import annotations

import logging
from typing import Any

from agentos.core.errors import InvalidRequest, LaneNotFound
from agentos.node.egress.guard import LaneGuard, guard_for
from agentos.node.egress.learn_window import LEARN_TTL_S

log = logging.getLogger("agentos.node.egress")


def lane_guard(runtime: Any, lane_id: str) -> LaneGuard:
    """The egress guard of a warm lane. LaneNotFound for a lane the node does not have,
    InvalidRequest for one with no live browser."""
    lanes = runtime.get("lanes") if runtime is not None else None
    if lanes is None:
        raise InvalidRequest("this node has no lanes")
    lanes.get(lane_id)  # LaneNotFound for an unknown lane
    session = lanes.live_session(lane_id)
    guard = getattr(session, "guard", None) if session is not None else None
    if not isinstance(guard, LaneGuard) and session is not None:
        guard = guard_for(getattr(session, "context", None))
    if not isinstance(guard, LaneGuard):
        raise InvalidRequest(f"{lane_id} has no browser open, so it has no egress guard; open the lane first")
    return guard


def _pilot_on(runtime: Any, lane_id: str) -> str | None:
    """Why the lane may not learn now: a pilot run (lane_do) drives it. Read fresh each time."""
    pilot = runtime.get("pilot") if runtime is not None else None
    sessions = getattr(pilot, "sessions", None) if pilot is not None else None
    try:
        busy = any(getattr(getattr(s, "request", None), "lane", None) == lane_id
                   for s in list((sessions or {}).values()))
    except Exception:  # noqa: BLE001 - cannot tell: refuse
        return "whether a pilot run drives the lane cannot be read"
    return "a pilot run drives the lane" if busy else None


def _pinned(runtime: Any, lane_id: str) -> Any:
    try:
        return runtime.get("lanes").get(lane_id).spec.account
    except LaneNotFound:
        return None
    except Exception:  # an account pin that cannot be read is no pin: nothing is released
        log.warning("could not read the account pinned for %s; the learn window releases nothing", lane_id,
                    exc_info=True)
        return None


def open_learn(runtime: Any, lane_id: str, *, field: str, value: str, grant_id: str,
               path_prefixes: list[str] | None = None, ttl_s: int = LEARN_TTL_S, site: str | None = None) -> str:
    """Open the learn window on `lane_id`'s guard and return its id (LaneGuard.open_learn).
    `grant_id` (required): only acts of this grant's driver are released, and any other driver's act
    closes the window. `site`: the site the request must go to (default: the site of the lane's
    pinned account). `path_prefixes`: the site's reviewed edit pages for the pinned account
    (agentos.node.sites.profile_edit_paths(site, account_id)), all of them when None. InvalidRequest
    when the grant is missing, the lane pins no account, a pilot run drives it, it has no browser,
    the field has no reviewed edit endpoint, or a prefix is not a reviewed one."""
    if not isinstance(grant_id, str) or not grant_id:
        raise InvalidRequest("a learn window is bound to the grant of the agent that learns: pass grant_id")
    guard = lane_guard(runtime, lane_id)
    pinned = _pinned(runtime, lane_id)
    if pinned is None or not getattr(pinned, "user_id", None):
        raise InvalidRequest(f"{lane_id} pins no account, so who an edit would change cannot be checked; pin the "
                             "lane's account first")
    stop = _pilot_on(runtime, lane_id)
    if stop:
        raise InvalidRequest(f"{lane_id} cannot learn now: {stop}")
    from agentos.node.sites import profile_edit_paths, site_key

    on = site_key(site or pinned.site)
    if on != site_key(pinned.site):
        raise InvalidRequest(f"{lane_id} pins an account on {pinned.site}, not on {on}")
    reviewed = profile_edit_paths(on, pinned.user_id)
    prefixes = reviewed if path_prefixes is None else path_prefixes
    if isinstance(prefixes, str) or not prefixes or any(p not in reviewed for p in prefixes):
        raise InvalidRequest(f"a learn window on {on} opens only for its reviewed profile edit pages "
                             f"({', '.join(reviewed) or 'none'}): agentos.node.sites.profile_edit_paths")
    try:
        return guard.open_learn(field=field, value=value, path_prefixes=prefixes, ttl_s=ttl_s,
                                pinned=lambda: _pinned(runtime, lane_id), site=on,
                                grant_id=grant_id, excluded=lambda: _pilot_on(runtime, lane_id))
    except ValueError as e:
        raise InvalidRequest(str(e)) from None


def learn_result(runtime: Any, lane_id: str, window_id: str) -> dict[str, Any] | None:
    """What the window released, or None while nothing was: {host, path, method, op, url, page_url, at,
    act_seq, act_at, act_call} (LaneGuard.learn_result says what the act fields are)."""
    return lane_guard(runtime, lane_id).learn_result(window_id)


def close_learn(runtime: Any, lane_id: str, window_id: str) -> None:
    """Close the window: it releases nothing more. A lane whose browser is gone has nothing to close."""
    try:
        guard = lane_guard(runtime, lane_id)
    except (InvalidRequest, LaneNotFound):
        return
    guard.close_learn(window_id)
