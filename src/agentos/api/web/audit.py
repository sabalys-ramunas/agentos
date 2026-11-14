"""Proof's reads of the action log, shaped for the app (web/src/features/activity/proof).

The node answers a day's accounts and one account's day on one route, told apart by whether it is
given a lane. The app reads them as two things, so each has its own path here: `days` for the day's
accounts, and `lanes/{ws}/{name}` for one account's day. Both are the node's `/api/audit/replay`, read
in place, so the node's own scope checks (who sees which lane) stay where they are.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.routing import BaseRoute

from agentos.api.app import Api
from agentos.api.web.base import org_route, read


def _day_query(request: Request) -> dict[str, Any]:
    return {"day": request.query_params.get("day"), "tz": request.query_params.get("tz")}


def routes(api: Api) -> list[BaseRoute]:
    async def days(request: Request) -> Any:
        return await read(request, "/api/audit/replay", query=_day_query(request))

    async def lane_day(request: Request) -> Any:
        lane = f"{request.path_params['ws']}/{request.path_params['name']}"
        return await read(request, "/api/audit/replay", query={"lane": lane, **_day_query(request)})

    return [
        org_route(api, "/audit/days", days, ["GET"]),
        org_route(api, "/audit/lanes/{ws}/{name}", lane_day, ["GET"]),
    ]
