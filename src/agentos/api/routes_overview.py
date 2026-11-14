"""This computer's overview for Home: the map (`/api/topology`, `/api/topology/events`), as the
app's pages read it. Its activity chart is `agentos.api.web.history`, built on what a caller may see.

Everything is read from the node's own trace log, runs, approvals and lanes. A call is a `tool_start`
and a `tool_end` in the trace log, and the end carries its cost and its outcome. A trace status the
app's run statuses do not name is mapped by what it says, and nothing is counted that the node
did not record.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from starlette.requests import Request
from starlette.routing import Route

from agentos.api.app import Api, endpoint
from agentos.api.errors import ApiError
from agentos.api.jsonable import jsonable, maybe_await
from agentos.api.lanes_view import workspace_health
from agentos.api.routes_core import all_lanes

WINDOWS = {"1h": 3600, "24h": 86400}
# The span of each range and the width of its buckets.
RANGES = {"24h": (86400, 3600), "7d": (7 * 86400, 6 * 3600), "30d": (30 * 86400, 86400)}
RECENT_S = 600
# A call's trace status as the app's run status. `ok` is a call that returned: nothing checked the outcome.
CALL_STATUS = {
    "started": "running",
    "ok": "succeeded_unverified",
    "pending": "waiting_approval",
    "denied": "failed",
    "error": "failed",
}
RUN_OUTCOMES = ("succeeded_verified", "succeeded_unverified", "partial", "failed")
PRINCIPAL_KIND = {"human": "human", "flow": "flow"}  # any other principal acts as an agent
# Runs are read from the runner's store, which has no time index for this; this many newest are counted.
RUN_SCAN = 5000


def _at(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, UTC).isoformat().replace("+00:00", "Z")


def _parse(text: str, name: str) -> float:
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError as e:
        raise ApiError("invalid", f"{name} is not a time.") from e


def _lane_state(lane: dict[str, Any]) -> str:
    if lane.get("quarantine_reason") or lane.get("state") == "quarantined":
        return "blocked"
    if lane.get("signal") not in ("ok", "unknown", None) or lane.get("logged_in_hint") == "needs_login":
        return "attention"
    return "ok" if lane.get("state") in ("warming", "warm", "active") else "idle"


def _calls(store: Any, since: float | None, until: float | None, limit: int) -> list[Any]:
    rows, _ = store.query(kind="tool_end", since=since, until=until, limit=min(limit, 500))
    return [row for row in rows if row.effect]


def _call_row(row: Any) -> dict[str, Any]:
    return {
        "at": _at(row.at),
        "lane": row.lane or "",
        "principal": row.principal or "",
        "tool": row.tool or "",
        "effect": row.effect,
        "status": CALL_STATUS.get(row.status, "failed"),
    }


async def _workspaces(api: Api, lanes: list[dict[str, Any]]) -> dict[str, str]:
    """Workspace id to name: the ones the lanes name, with the names the workspace service gives."""
    names = {lane["workspace"]: lane["workspace"] for lane in lanes}
    service = api.runtime.get("workspaces")
    if service is not None:
        for item in await maybe_await(service.list_workspaces()):
            row = jsonable(item)
            if row.get("id") in names:
                names[row["id"]] = row.get("name") or row["id"]
    return names


async def topology(api: Api, window: str) -> dict[str, Any]:
    span = WINDOWS[window]
    now = time.time()
    store = api.runtime.get("traces")
    lanes = await all_lanes(api, None)
    names = await _workspaces(api, lanes)

    per_lane: dict[str, dict[str, Any]] = {}
    active: list[dict[str, Any]] = []
    recent: list[dict[str, Any]] = []
    if store is not None:
        for row in store.db.all(
            "SELECT lane, COUNT(*) AS calls, SUM(CASE WHEN status <> 'ok' THEN 1 ELSE 0 END) AS failed, "
            "MAX(at) AS last_at FROM traces WHERE kind = 'tool_end' AND at >= ? AND lane IS NOT NULL "
            "GROUP BY lane",
            (now - span,),
        ):
            per_lane[row["lane"]] = {
                "calls": row["calls"],
                "failed": row["failed"] or 0,
                "last_at": _at(row["last_at"]) if row["last_at"] else None,
            }
        for row in store.db.all(
            "SELECT s.lane, s.principal, s.tool, s.at, s.run_id FROM traces s WHERE s.kind = 'tool_start' "
            "AND s.at >= ? AND NOT EXISTS (SELECT 1 FROM traces e WHERE e.kind = 'tool_end' "
            "AND e.parent_id = s.id) ORDER BY s.at DESC LIMIT 50",
            (now - RECENT_S,),
        ):
            active.append(
                {
                    "lane": row["lane"] or "",
                    "principal": row["principal"] or "",
                    "tool": row["tool"] or "",
                    "since": _at(row["at"]),
                    "run_id": row["run_id"],
                }
            )
        for row in _calls(store, now - RECENT_S, None, 50):
            kind = PRINCIPAL_KIND.get(row.principal_kind or "", "agent")
            recent.append(
                {
                    "at": _at(row.at),
                    "lane": row.lane or "",
                    "principal": row.principal or "",
                    "kind": kind,
                    "tool": row.tool or "",
                    "status": CALL_STATUS.get(row.status, "failed"),
                }
            )

    node_id = f"node:{api.node_name}"
    entities: list[dict[str, Any]] = [
        {
            "id": node_id,
            "type": "node",
            "label": api.node_name,
            "state": "ok",
            "ref": None,
            "stats": {"lanes": len(lanes), "workspaces": len(names)},
        }
    ]
    links: list[dict[str, Any]] = []
    for ws_id, ws_name in names.items():
        ws_lanes = [lane for lane in lanes if lane["workspace"] == ws_id]
        health, _ = workspace_health(ws_lanes)
        entities.append(
            {
                "id": f"workspace:{ws_id}",
                "type": "workspace",
                "label": ws_name,
                "state": health,
                "ref": None,
                "stats": {"lanes": len(ws_lanes)},
            }
        )
    for lane in lanes:
        lane_id = f"lane:{lane['id']}"
        entities.append(
            {
                "id": lane_id,
                "type": "lane",
                "label": lane["name"],
                "state": _lane_state(lane),
                "ref": None,
                "stats": {"state": lane["state"]},
            }
        )
        counts = per_lane.get(lane["id"], {"calls": 0, "failed": 0, "last_at": None})
        links.append(
            {
                "from": lane_id,
                "to": f"workspace:{lane['workspace']}",
                "kind": "belongs_to",
                "calls": 0,
                "failed": 0,
                "last_at": None,
            }
        )
        links.append(
            {
                "from": lane_id,
                "to": node_id,
                "kind": "runs_on",
                "calls": counts["calls"],
                "failed": counts["failed"],
                "last_at": counts["last_at"],
            }
        )

    return {
        "generated_at": _at(now),
        "window_seconds": span,
        "entities": entities,
        "links": links,
        "active": active,
        "recent": recent,
    }


async def topology_events(api: Api, frm: str, to: str, limit: int) -> list[dict[str, Any]]:
    store = api.runtime.get("traces")
    if store is None:
        return []
    rows = _calls(store, _parse(frm, "from"), _parse(to, "to"), limit)
    return [_call_row(row) for row in rows]



def routes(api: Api) -> list:
    def route(path: str, fn: Any, methods: list[str]) -> Route:
        return Route(path, endpoint(api, fn, owner=True), methods=methods)

    async def topo(request: Request) -> dict:
        window = request.query_params.get("window", "1h")
        if window not in WINDOWS:
            raise ApiError("invalid", "The window is 1h or 24h.")
        return await topology(api, window)

    async def topo_events(request: Request) -> list:
        # Without a range, the last hour: the map's own default.
        now = datetime.now(UTC)
        frm = request.query_params.get("from") or _at(now.timestamp() - 3600)
        to = request.query_params.get("to") or _at(now.timestamp())
        try:
            limit = int(request.query_params.get("limit", "500"))
        except ValueError as e:
            raise ApiError("invalid", "The limit is a number.") from e
        return await topology_events(api, frm, to, limit)

    return [
        route("/api/topology", topo, ["GET"]),
        route("/api/topology/events", topo_events, ["GET"]),
    ]
