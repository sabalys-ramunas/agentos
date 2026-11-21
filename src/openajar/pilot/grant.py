"""The child grant a pilot run acts under (design 5.3), attenuated from the caller's by the policy.

One lane, the mode's tools (as pins, so the policy refuses anything else), the mode's ceiling, a
budget, no delegation, and a short life. The policy intersects all of it with the parent, so a child
can never exceed it, and revoking the parent revokes the child. `secret` is asked for only when the
run was handed a vault value, and only a parent that holds it can pass it on.

A request no rule reads could be a payment, and money always needs a person (docs/PRINCIPLES.md), so
such a publish on a child grant asks whatever `[taint] downgrade` says (`PolicyService.must_ask`).

The life is the wall-time budget plus the approval window. An approval binds to the grant that asked
for it, so a child that died while a person was deciding could never send what they approved. The
run revokes it the moment it ends, so the extra time exists only while a person is being waited on.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from openajar.core.errors import ServiceUnavailable
from openajar.core.models import Principal, parse_lane_id
from openajar.pilot.modes import Mode


@dataclass(frozen=True)
class ChildGrant:
    token: str
    grant_id: str


def pins_for(mode: Mode, specs: Mapping[str, Any]) -> dict[str, str]:
    """The mode's tools as they are now: a tool that changes under a run stops working for it."""
    from openajar.policy.manifest import manifest_hash

    return {name: manifest_hash(specs[name]) for name in sorted(mode.tools) if name in specs}


def mint(policy: Any, principal: Principal, *, run_id: str, lane: str, mode: Mode, specs: Mapping[str, Any],
         ttl_s: float, max_usd: float, specials: Iterable[str] = ()) -> ChildGrant:
    if policy is None:
        raise ServiceUnavailable("the pilot needs the policy service: every action runs under a child grant")
    workspace, _ = parse_lane_id(lane)
    out = policy.mint_child_token(principal, purpose=f"pilot run {run_id}", workspace=workspace, lanes=[lane],
                                  ceiling=mode.ceiling, specials=list(specials), ttl_seconds=int(ttl_s),
                                  budget={"usd": max_usd}, pins=pins_for(mode, specs))
    policy.must_ask(out["grant_id"], "unknown")
    return ChildGrant(token=out["token"], grant_id=out["grant_id"])
