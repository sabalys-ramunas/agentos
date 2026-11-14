"""While you slept: `GET /api/orgs/local/topology/night`, the shape `TopologyNight` (web/src/api/types.ts).

The night before a morning, 11 pm to 7 am on the computer's clock: how many actions ran and on how many accounts,
how many failed, which posts went out, which accounts stopped and need a person, and each hour's count.

The hosted cloud reads its own copy of the action log. A node reads its
own action log and its own stories, as the caller. A node keeps no record of its own outages, so it reports none:
an outage the node cannot know about is an empty list, never a guess.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from starlette.requests import Request
from starlette.routing import BaseRoute

from agentos.api.app import Api
from agentos.api.web import history, telling
from agentos.api.web.base import org_route, read
from agentos.api.web.home import account_state, face_of
from agentos.core.errors import InvalidRequest

NIGHT_STARTS, NIGHT_ENDS = 23, 7
MAX_NIGHT_STOPS = 5
MAX_PAGES = 40  # of the action log, 500 rows each: a night is far less than this
NOT_A_CALL = frozenset({"tool_start", "run", "approval", "authorize"})
POSTS = frozenset({history.POST, history.PUBLISH, history.REPLY})


def _clock(hour: int) -> str:
    return f"{hour % 12 or 12} {'am' if hour < 12 else 'pm'}"


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _at(stamp: str) -> datetime:
    return datetime.fromisoformat(str(stamp))


def night_window(now: datetime, zone: ZoneInfo, morning: date | None) -> tuple[datetime, datetime]:
    """The night that ended at 7 that morning, or the one still under way if it is not 7 yet."""
    if morning is None:
        morning = now.astimezone(zone).date()  # before 7 this is the night under way, after it the one just over
    end = datetime.combine(morning, time(NIGHT_ENDS), tzinfo=zone)
    start = datetime.combine(morning - timedelta(days=1), time(NIGHT_STARTS), tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)


async def _actions(request: Request, start: datetime, until: datetime) -> list[dict[str, Any]]:
    """The action log rows of the night that are on an account, newest first. Reads back to `start`."""
    rows: list[dict[str, Any]] = []
    before = None
    for _ in range(MAX_PAGES):
        page = await read(request, "/api/activity", query={"before": before, "limit": 500})
        for row in page:
            at = _at(row["at"])
            if start <= at < until and row.get("lane") and row["kind"] not in NOT_A_CALL:
                rows.append(row)
        if len(page) < 500 or not page or _at(page[-1]["at"]) < start:
            return rows
        before = page[-1]["id"]
    raise InvalidRequest("There are too many actions to read back for this night.")


def routes(api: Api) -> list[BaseRoute]:
    async def topology_night(request: Request) -> dict[str, Any]:
        zone = ZoneInfo(api.timezone)
        now = datetime.now(UTC)
        try:
            morning = date.fromisoformat(request.query_params["morning"]) if request.query_params.get("morning") else None
        except ValueError:
            raise InvalidRequest("morning is a day, like 2026-11-03.") from None
        if morning is not None and morning > now.astimezone(zone).date() + timedelta(days=1):
            raise InvalidRequest("That night has not happened yet.")
        start, end = night_window(now, zone, morning)
        until = min(end, now)

        def local(moment: datetime) -> str:
            return moment.astimezone(zone).strftime("%H:%M")

        rows = await _actions(request, start, until)
        hours: dict[datetime, dict[str, Any]] = {}
        first_failure: dict[str, datetime] = {}
        for row in rows:
            at = _at(row["at"])
            hour = at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
            slot = hours.setdefault(hour, {"at": _iso(hour), "actions": 0, "failed": 0})
            slot["actions"] += 1
            if row.get("status") == "error":
                slot["failed"] += 1
                first_failure.setdefault(row["lane"], at)
        accounts_touched = len({row["lane"] for row in rows})

        stories, _, _ = await history.stories_between(request, api, start, until, None)
        went_out = sum(
            story["action"] in POSTS and story["verdict"] in (history.CHECKED, history.NOT_CHECKED) for story in stories
        )

        workspaces = await read(request, "/api/workspaces")
        lanes = {lane["id"]: lane for workspace in workspaces for lane in workspace.get("lanes") or []}
        names = {workspace["id"]: workspace.get("name") or workspace["id"] for workspace in workspaces}
        stopped: list[dict[str, Any]] = []
        for lane_id, at in sorted(first_failure.items(), key=lambda item: item[1]):
            lane = lanes.get(lane_id)
            face = face_of(account_state(lane)) if lane is not None else "all_good"
            if face not in ("waiting", "stuck"):
                continue
            place = telling.place_of(lane_id, names)
            stopped.append({
                "lane": lane_id, "place": place, "at": _iso(at), "face": face,
                "sentence": f"{place} stopped at {local(at)} and needs you.",
            })
        stopped = stopped[:MAX_NIGHT_STOPS]

        actions = len(rows)
        lead = (
            f"While you slept, {_clock(NIGHT_STARTS)} to {_clock(NIGHT_ENDS)}: "
            f"{actions} {'action' if actions == 1 else 'actions'} on {accounts_touched} "
            f"{'account' if accounts_touched == 1 else 'accounts'}."
        )
        parts = [lead]
        if went_out:
            parts.append(f"{went_out} {'post' if went_out == 1 else 'posts'} went out.")
        parts += [item["sentence"] for item in stopped]
        return {
            "timezone": api.timezone,
            "from": _iso(start),
            "to": _iso(end),
            "label": f"{_clock(NIGHT_STARTS)} to {_clock(NIGHT_ENDS)}",
            "over": now >= end,
            "actions": actions,
            "accounts": accounts_touched,
            "failed": sum(slot["failed"] for slot in hours.values()),
            "posts": went_out,
            "outages": [],
            "stopped": stopped,
            "hours": [hours[key] for key in sorted(hours)],
            "sentence": " ".join(parts),
        }

    # Owner only, as the node's own map is (`/api/topology`): it names every computer and account in the org.
    return [org_route(api, "/topology/night", topology_night, ["GET"], owner=True)]
