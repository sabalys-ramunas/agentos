"""The proxy reputation rules. A proxy label is an account's IP identity, so these rules refuse any
new lane that would link two accounts by IP. They are pure functions over facts about lanes, and
the service supplies the facts (every lane the node has, plus the lanes a plan has accepted so far).

    (a) one proxy label used by lanes in two different workspaces links two businesses
    (b) two lanes with the same platform on one proxy: two accounts of one platform share an IP

A lane with no proxy is not refused. It exits on the node's own IP, so the planner warns instead.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class LaneFact:
    """A lane as the rules see it. `platform` comes from the workspace store, or the lane name."""
    lane: str
    workspace: str
    platform: str
    proxy: str | None


def proxy_conflicts(candidate: LaneFact, others: Iterable[LaneFact],
                    shown: Callable[[str], bool] | None = None) -> list[str]:
    """Why `candidate` must not be created next to `others`. Empty when it may be. `shown(lane)`: a
    lane the reader may not see is not named (nor its workspace), only that one is there."""
    if candidate.proxy is None:
        return []
    reasons: list[str] = []
    for other in sorted(others, key=lambda fact: fact.lane):
        if other.lane == candidate.lane or other.proxy != candidate.proxy:
            continue
        named = shown is None or shown(other.lane)
        if other.workspace != candidate.workspace:
            where = f"{other.lane} in workspace {other.workspace}" if named else "a lane in another workspace"
            reasons.append(f"proxy {candidate.proxy} is already used by {where}: two businesses must not share an IP")
        if other.platform == candidate.platform:
            who = other.lane if named else "another lane"
            reasons.append(f"{who} already uses {candidate.platform} on proxy {candidate.proxy}: "
                           "one account per platform per IP")
    return reasons
