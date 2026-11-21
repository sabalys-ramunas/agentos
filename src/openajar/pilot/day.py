"""One day of a worker's runs: what the limits count and what a person reads."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

WAITING = "waiting_approval"


@dataclass(frozen=True)
class Day:
    things: int = 0
    steps: int = 0
    helped: int = 0
    asked: int = 0
    waiting: int = 0
    longest: int = 0
    usd: float = 0.0

    def public(self) -> dict[str, Any]:
        return {**asdict(self), "usd": round(self.usd, 6)}


def start_of_today(now: datetime | None = None) -> datetime:
    """Midnight on this computer, with its offset, so it compares with a run's UTC times."""
    moment = (now or datetime.now().astimezone()).astimezone()
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def count(runs: list[Any], live_steps: dict[str, int] | None = None, *, since: datetime | None = None) -> Day:
    """`runs`: this worker's runs. `live_steps`: how far the runs still going have got, because a
    run only writes its steps down when it ends."""
    since = since or start_of_today()
    live_steps = live_steps or {}
    things = steps = helped = asked = waiting = longest = 0
    usd = 0.0
    for run in runs:
        if run.created_at < since:
            continue
        output = run.output or {}
        taken = int(output.get("steps") or live_steps.get(run.id, 0))
        things += 1
        steps += taken
        longest = max(longest, taken)
        helped += len(output.get("escalations") or ())
        usd += float((output.get("spend") or {}).get("total_usd") or 0.0)
        if str(run.status) == WAITING:
            asked += 1
            waiting += 1
        elif output.get("pilot_status") in ("needs_help", "held"):
            asked += 1
    return Day(things=things, steps=steps, helped=helped, asked=asked, waiting=waiting, longest=longest, usd=usd)
