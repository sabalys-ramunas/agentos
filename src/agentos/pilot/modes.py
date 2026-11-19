"""What each pilot mode may do, as data (design 5.2). The grant's pins and ceiling hold it to this.

Only `goal` exists so far: `browser_task`. `repair`, `resolve` and `login_assist` are other briefs.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentos.core.models import Effect


@dataclass(frozen=True)
class Mode:
    name: str
    tools: frozenset[str]
    ceiling: Effect


# `commit` is the ceiling and never a free pass. Below it the egress guard would refuse a publish
# outright, so it could never become the approval the design asks for. At it, the commit gate holds
# the publish, and whether a person approves it is the node's policy (ROUTING.md, "After untrusted
# content"). No effect rule says `pay` or `irreversible`, so the ceiling alone keeps neither out of
# reach. What does: a lane the gate does not read is refused (PilotService.check_lane), and on a lane
# it reads, a request no rule names always asks a person (agentos.pilot.grant).
GOAL = Mode(name="goal", ceiling=Effect.COMMIT, tools=frozenset({
    "browser_claim", "browser_release", "browser_snapshot", "browser_act", "browser_scroll", "browser_wait",
    "browser_upload", "browser_files", "vault_fill"}))
