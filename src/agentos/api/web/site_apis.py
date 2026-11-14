"""Site APIs and their keys on a node, as the app reads them (web/src/api/types.ts: `SiteApi`, `SiteApiDetail`,
`SiteUsageHour`, `SiteKey`, `SiteDiscovery`, `SiteTryResult`).

A Site API is a person's own definition of one job on a site they use (docs/SITE-APIS.md). The node already has
the owner's routes for it, and each action the app offers goes to the node route that really does it. This module
only reshapes what those routes answer. Nothing is invented: a field the node does not know is left out, or is
null where the type allows it. A key's secret is returned once, on create, and is never written down here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from statistics import median
from typing import Any
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute

from agentos.api.app import Api
from agentos.api.web.base import org_route, read, write

WEEK_HOURS = 168  # the window of the app's totals and sparkline: a week of hours
SPARK_HOURS = 6  # one bar of the sparkline is six hours, so a week is 28 bars
SUCCEEDED = ("succeeded_verified", "succeeded_unverified")
ACTIONS = ("read", "pause", "resume", "activate", "decline", "repair", "accept-empty", "keep-updated")
KEY_FIELDS = ("label", "apis", "endpoints", "ceiling", "rate_per_min", "expires_at")
SITE_CHECK = ("signal_captcha", "signal_checkpoint")
WORSE = {"broken": 4, "held": 3, "degraded": 2, "healthy": 1, "unknown": 0}


def _z(value: str | None) -> str | None:
    if not value:
        return None
    return value.replace("+00:00", "Z") if value.endswith("+00:00") else value


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _label(key: str) -> str:
    return key.replace("_", " ").strip().capitalize() or key


def stage_of(state: str) -> str:
    """The app's three stages. Anything not live yet is a proposal waiting for a person."""
    return state if state in ("active", "paused") else "proposed"


def health_of(row: dict[str, Any]) -> str:
    """One line for the whole API, from what its runs said on the node, the worst surface first."""
    if row.get("state") == "paused":
        return "paused"
    worst = max(row.get("health", {}).values(), key=lambda h: WORSE.get(h.get("state"), 0), default=None)
    if worst is None:
        return "unread"
    state, code = worst.get("state"), worst.get("code")
    if state == "broken":
        return "changed"
    if state == "held":
        return "site_check" if code in SITE_CHECK else "sign_in"
    if state == "degraded":
        return "changed" if code else "empty"
    return "good" if state == "healthy" else "unread"


def problem_of(row: dict[str, Any], health: str) -> dict[str, Any] | None:
    """Why it is not answering, in facts the node recorded. Nothing is guessed about the site."""
    if health == "paused":
        return {"since": row.get("updated_at") or _now()}
    if health in ("good", "unread"):
        return None
    worst = max(row.get("health", {}).values(), key=lambda h: WORSE.get(h.get("state"), 0), default={})
    since = worst.get("since")
    return {"since": _z(since) or row.get("updated_at") or _now()}


def points_of(points: list[dict[str, Any]]) -> list[dict[str, int]]:
    """The hours of a usage chart, reduced to what the app counts: calls served cached, fresh, and as errors."""
    return [{"calls": p["calls"], "cached": p["cached"], "fresh": p["fresh"], "errors": sum((p.get("errors") or {}).values())}
            for p in points]


def totals_of(points: list[dict[str, int]]) -> dict[str, Any]:
    calls = sum(p["calls"] for p in points)
    cached = sum(p["cached"] for p in points)
    return {"calls": calls, "cached": cached, "fresh": sum(p["fresh"] for p in points),
            "errors": sum(p["errors"] for p in points), "cached_pct": round(cached * 100 / calls) if calls else None}


def spark_of(points: list[dict[str, int]]) -> list[dict[str, int]]:
    bars = []
    for start in range(0, len(points), SPARK_HOURS):
        chunk = points[start:start + SPARK_HOURS]
        bars.append({"cached": sum(p["cached"] for p in chunk), "fresh": sum(p["fresh"] for p in chunk),
                     "errors": sum(p["errors"] for p in chunk)})
    return bars


def p50_of(points: list[dict[str, Any]]) -> float | None:
    """The median of the hourly medians, for the hours that had calls. None when none had."""
    values = [p["p50_ms"] for p in points if p.get("calls") and p.get("p50_ms") is not None]
    return median(values) if values else None


def field_of(key: str, kind: str) -> dict[str, str]:
    return {"key": key, "label": _label(key), "kind": kind}


def site_api(row: dict[str, Any], points: list[dict[str, int]]) -> dict[str, Any]:
    """A row of the list: the job, where it runs, its state and the week's calls."""
    endpoints = list(row.get("endpoints", {}).values())
    first = endpoints[0] if endpoints else {}
    lane = (row.get("surfaces") or [""])[0]
    health = health_of(row)
    totals = totals_of(points)
    read_at = max((h["last_success_at"] for h in row.get("health", {}).values() if h.get("last_success_at")), default=None)
    proposal = row.get("proposal")
    out: dict[str, Any] = {
        "key": row["api"],
        "title": row.get("title") or row["api"],
        "site": {"name": row["domain"], "domain": row["domain"]},
        "words": first.get("description") or first.get("title") or row.get("title") or row["api"],
        "workspace": lane.split("/", 1)[0] if "/" in lane else "",
        "lane": lane,
        "account": lane,
        "stage": stage_of(row["state"]),
        "health": health,
        "problem": problem_of(row, health),
        "proposal": None,
        "sample": {"kind": "rows", "columns": [_label(f["key"]) for f in first.get("fields", [])], "rows": []},
        "spark": spark_of(points),
        "calls_7d": totals["calls"],
        "cached_pct": totals["cached_pct"],
        "read_at": _z(read_at),
        "version": row["version"],
    }
    if proposal is not None:
        out["proposal"] = {"by": proposal.get("by") or "an agent", "at": _z(proposal.get("at")) or _now(),
                           "steps": proposal.get("steps") or []}
    return out


def detail_of(row: dict[str, Any], raw: dict[str, list[dict[str, Any]]], computer: str) -> dict[str, Any]:
    """One API as its page reads it: the list's row, each endpoint's own week, and the pace the account is under."""
    by_endpoint = {name: points_of(points) for name, points in raw.items()}
    base = site_api(row, _sum_hours(by_endpoint))
    endpoints = []
    for name, ep in row.get("endpoints", {}).items():
        totals = totals_of(by_endpoint.get(name, []))
        endpoints.append({
            "key": name,
            "title": ep["title"],
            "description": ep.get("description") or "",
            "effect": ep["effect"],
            "input": ep.get("input", []),
            "fields": [field_of(f["key"], f["kind"]) for f in ep.get("fields", [])],
            "rows": [],
            "total": None,
            "page": None,
            "cache_s": ep["cache_s"],
            "pace_per_hour": ep["pace_per_hour"],
            "expect_min": ep.get("expect_min"),
            "calls_7d": totals["calls"],
            "cached_pct": totals["cached_pct"],
            "p50_ms": p50_of(raw.get(name, [])),
        })
    hours = _sum_hours(by_endpoint)
    last_hour = [p["calls"] for p in hours[-12:]]
    return {**base, "endpoints": endpoints,
            "pace": {"per_hour": endpoints[0]["pace_per_hour"] if endpoints else 0, "used": last_hour[-1] if last_hour else 0,
                     "others": [], "hours": last_hour},
            "computer": computer, "keep_updated": "off"}


def _sum_hours(by_endpoint: dict[str, list[dict[str, int]]]) -> list[dict[str, int]]:
    """Every endpoint's hours added up, hour by hour. The lists line up: they are the same hours."""
    if not by_endpoint:
        return []
    length = min(len(points) for points in by_endpoint.values())
    summed = []
    for index in range(length):
        rows = [points[index] for points in by_endpoint.values()]
        summed.append({"calls": sum(r["calls"] for r in rows), "cached": sum(r["cached"] for r in rows),
                       "fresh": sum(r["fresh"] for r in rows), "errors": sum(r["errors"] for r in rows)})
    return summed


def discovery_of(found: dict[str, Any], lane: str, api_key: str, now: str) -> dict[str, Any] | None:
    """What the page on the lane offered, as Teach it shows it. None when the node read no repeating shape."""
    candidates = found.get("candidates") or []
    if not candidates or found.get("signal") not in (None, "ok", "unknown"):
        return None
    url = found.get("url") or ""
    host = urlsplit(url).hostname or ""
    top = candidates[0]
    values = top["sample"]["rows"]
    columns = [{"label": name, "field": name, "values": [str(row.get(name) or "") for row in values]} for name in top["fields"]]
    return {
        "lane": lane,
        "api": api_key,
        "site": {"name": host, "domain": host},
        "page": {"url": url, "wordmark": host, "nav": [], "heading": "", "count_words": f"{top['count']} found",
                 "layout": "table" if top.get("kind") == "table" else "cards", "columns": columns, "side": [],
                 "seen_at": now},
        "regions": [{"id": f"r{index}", "words": f"{c['name']}, {c['count']} found",
                     "sure": "sure" if c["confidence"] >= 0.8 else "likely" if c["confidence"] >= 0.5 else "unsure",
                     "kind": "main" if index == 0 else "side"} for index, c in enumerate(candidates)],
        "rows": [{name: str(value or "") for name, value in row.items()} for row in values],
        "total": top["count"],
        "suggestions": [],
    }


def try_answer(api: str, endpoint: str | None, answer: dict[str, Any]) -> dict[str, Any]:
    """A try, as the app reads it: the status and time a caller would have had, and its body. A run that ended
    without a result says what a caller would have been told, from the node's own run."""
    run, ms = answer.get("run"), answer.get("ms") or 0
    if answer.get("problem"):
        problem = answer["problem"]
        return {"status": problem["status"], "ms": ms, "headers": {"Content-Type": "application/problem+json"}, "body": problem}
    if answer.get("status") in SUCCEEDED:
        return {"status": 200, "ms": ms, "headers": {"Agentos-Run": run or ""}, "body": {
            "data": answer.get("data"), "status": answer["status"],
            "untrusted_fields": answer.get("untrusted_fields") or [], "private_fields": answer.get("private_fields") or [],
            "truncated": bool(answer.get("truncated")), "stale": False, "cache": {"hit": False, "age_s": 0},
            "source": {"api": api, "endpoint": endpoint, "run": run}}}
    return {"status": 202, "ms": ms, "headers": {"Agentos-Run": run or ""},
            "body": {"code": "pending", "status": answer.get("status"), "run": run}}


def key_out(key: dict[str, Any]) -> dict[str, Any]:
    return {field: key.get(field) for field in ("id", "label", "prefix", "apis", "endpoints", "ceiling", "rate_per_min",
                                                "expires_at", "last_used_at", "created_at")}


def routes(api: Api) -> list[BaseRoute]:
    computer = api.node_name

    async def points_for(request: Request, name: str, endpoint: str | None = None) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"hours": WEEK_HOURS, "endpoint": endpoint}
        chart = await read(request, f"/api/site-apis/{name}/usage", query=query)
        return chart["points"]

    async def week_of(request: Request, name: str) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
        row = await read(request, f"/api/site-apis/{name}")
        raw = {endpoint: await points_for(request, name, endpoint) for endpoint in row.get("endpoints", {})}
        return row, raw

    async def detail(request: Request, name: str) -> dict[str, Any]:
        row, raw = await week_of(request, name)
        return detail_of(row, raw, computer)

    async def list_apis(request: Request) -> list[dict[str, Any]]:
        rows = (await read(request, "/api/site-apis"))["apis"]
        out = []
        for row in rows:
            out.append(site_api(row, points_of(await points_for(request, row["api"]))))
        return sorted(out, key=lambda item: _rank(item["health"]))

    async def one(request: Request) -> dict[str, Any]:
        return await detail(request, request.path_params["api"])

    async def usage(request: Request) -> list[dict[str, Any]]:
        _, raw = await week_of(request, request.path_params["api"])
        per = {endpoint: points_of(points) for endpoint, points in raw.items()}
        hours = []
        for index in range(min((len(points) for points in raw.values()), default=0)):
            at = next(iter(raw.values()))[index]["hour"]
            hours.append({"at": at, "calls": {endpoint: {"cached": points[index]["cached"], "fresh": points[index]["fresh"],
                                                         "errors": points[index]["errors"]}
                                             for endpoint, points in per.items()}})
        return hours

    async def discover(request: Request) -> Any:
        lane = request.query_params.get("lane") or ""
        if not lane:
            return JSONResponse(None)
        found = await read(request, "/api/site-apis/discover", query={"on": lane})
        host = urlsplit(found.get("url") or "").hostname or ""
        existing = next((row["api"] for row in (await read(request, "/api/site-apis"))["apis"]
                         if row["domain"] == host and lane in (row.get("surfaces") or [])), "")
        return JSONResponse(discovery_of(found, lane, existing, _now()))

    async def try_it(request: Request) -> dict[str, Any]:
        name = request.path_params["api"]
        body = await request.json()
        answer = await write(request, "POST", f"/api/site-apis/{name}/try",
                             {"endpoint": body.get("endpoint"), "params": body.get("params") or {}})
        return try_answer(name, body.get("endpoint"), answer)

    def action(kind: str) -> Any:
        async def handler(request: Request) -> Any:
            name = request.path_params["api"]
            payload = {"vouch": False} if kind == "activate" else None
            answer = await write(request, "POST", f"/api/site-apis/{name}/{kind}", payload)
            if kind == "decline" and answer.get("removed"):
                return Response(status_code=204)
            return JSONResponse(await detail(request, name))

        handler.__name__ = f"site_api_{kind.replace('-', '_')}"
        return handler

    async def keys(request: Request) -> list[dict[str, Any]]:
        rows = (await read(request, "/api/site-keys"))["keys"]
        return [key_out(row) for row in rows if not row.get("revoked_at")]

    async def make_key(request: Request) -> Response:
        body = await request.json()
        fields = {field: body[field] for field in KEY_FIELDS if field in body}
        answer = await write(request, "POST", "/api/site-keys", fields)
        return JSONResponse({**key_out(answer["key"]), "secret": answer["secret"]}, status_code=201)

    async def revoke_key(request: Request) -> Response:
        await write(request, "DELETE", f"/api/site-keys/{request.path_params['id']}")
        return Response(status_code=204)

    out: list[BaseRoute] = [
        org_route(api, "/site-apis", list_apis, ["GET"], owner=True),
        org_route(api, "/site-apis/discover", discover, ["GET"], owner=True),
        org_route(api, "/site-apis/{api}", one, ["GET"], owner=True),
        org_route(api, "/site-apis/{api}/usage", usage, ["GET"], owner=True),
        org_route(api, "/site-apis/{api}/try", try_it, ["POST"], owner=True),
        *[org_route(api, f"/site-apis/{{api}}/{kind}", action(kind), ["POST"], owner=True) for kind in ACTIONS],
        org_route(api, "/site-keys", keys, ["GET"], owner=True),
        org_route(api, "/site-keys", make_key, ["POST"], owner=True),
        org_route(api, "/site-keys/{id}", revoke_key, ["DELETE"], owner=True),
    ]
    return out


def _rank(health: str) -> int:
    order = ["changed", "sign_in", "site_check", "empty", "resting", "unread", "paused", "good"]
    return order.index(health) if health in order else len(order)
