"""This computer, as the app's Computers pages read it (`/api/nodes/{id}/...`, the app's
`/api/orgs/local/nodes/{id}/...` without the org).

A node is one computer, so the list has one row, this node, and every route here checks that `{id}`
is it. Each answer is what the node reads about itself, the same facts it sends its controller
(`agentos.node.channel.reports`). A value the node cannot read is null, and a list it does not keep
is empty. Nothing is invented: a sample that was never taken is not a point on a graph.

Joining another computer, and removing this one, are refused: a node runs alone, and there is no
controller here to join it to.
"""

from __future__ import annotations

import asyncio
import json
import platform
import queue
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route

from agentos import __version__
from agentos.api.app import Api, access_of, endpoint
from agentos.api.errors import ApiError
from agentos.api.routes_core import all_lanes, local_node
from agentos.node.channel import reports

MIB = 1024 * 1024
APP_EFFECTS = frozenset(
    {
        "read",
        "draft",
        "commit",
        "pay",
        "irreversible",
        "secret",
        "exec",
        "install_overlay",
        "install_base",
        "service",
        "brain",
        "agent",
        "vm",
    }
)
NO_JOIN = (
    "This computer runs on its own. Other computers join a cloud account, not this one, "
    "so there is no join code to make here."
)
NO_REMOVE = "This is the computer you are using, so it cannot be removed from itself."

# The window a metrics answer covers, and the spacing its points would have.
METRIC_BUCKETS = {"1h": 60, "24h": 600, "7d": 3600}


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _utc(stamp: datetime) -> str:
    return stamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


def next_fires(cron: str | None, zone: str | None, count: int = 3) -> list[str]:
    """The next slots of a cron schedule, in UTC. A slot is the planned time: a fire can come a few
    seconds later, when a schedule has jitter, and a paced schedule can move a slot."""
    if not cron:
        return []
    try:
        from zoneinfo import ZoneInfo

        from croniter import croniter

        at = datetime.now(ZoneInfo(zone) if zone else UTC)
        it = croniter(cron, at)
        return [_utc(it.get_next(datetime)) for _ in range(count)]
    except (ValueError, KeyError, ImportError):
        return []


def schedules_of(api: Api) -> list[dict[str, Any]] | None:
    """This computer's schedules as `NodeSchedule`, or None when it runs no scheduler at all."""
    runner = api.runtime.get("runner")
    scheduler = getattr(runner, "scheduler", None)
    if scheduler is None:
        return None
    store = getattr(runner, "store", None)
    out = []
    for s in scheduler.list():
        row = reports._schedule(scheduler, s)
        runs = store.list_runs(schedule_id=s.id, limit=10) if store is not None else []
        recent = [
            {"at": _utc(run.created_at), "status": getattr(run.status, "value", run.status), "run_id": run.id}
            for run in runs
        ]
        out.append(
            {
                "id": row["id"],
                "flow": row["flow"],
                "cron": row["cron"],
                "watch": row["watch"],
                "params": row["params"],
                "deadman_minutes": row["deadman_minutes"],
                "enabled": row["enabled"],
                "next_run": row["next_run"],
                "next_fire_at": row["next_run"],
                "lane": row["lane"],
                "last_run": {"id": recent[0]["run_id"], "status": recent[0]["status"]} if recent else None,
                "recent_fires": recent,
                "next_fires": next_fires(row["cron"], row["timezone"]),
            }
        )
    return out


def tools_of(api: Api) -> list[dict[str, Any]]:
    """The tools this node offers its agents, as `ToolDef`: the same list the MCP server serves."""
    from agentos.mcp.registry import collect
    from agentos.node.daemon import tool_providers

    tools, _ = collect(api.runtime, tool_providers())
    # The screens name these effects; an effect the screens have no word for is not reported.
    return [
        {
            "name": tool.name,
            "description": " ".join(tool.description.split()) or None,
            "effect": tool.effect.value if tool.effect.value in APP_EFFECTS else None,
        }
        for tool in tools
    ]


def log_query(request: Request) -> dict[str, Any]:
    """The app's filters as `Query` params. The stream follows: a log view stays open."""
    params: dict[str, Any] = {"follow": True}
    for key in ("source", "level", "since"):
        if request.query_params.get(key):
            params[key] = request.query_params[key]
    return params


def mem_used_mb(total_mb: int | None) -> int | None:
    """Memory in use: the total less what the system could still hand out. MemAvailable on Linux; on
    macOS the free, inactive and speculative pages. None when the host does not say."""
    try:
        with open("/proc/meminfo") as handle:
            info = {line.split(":")[0]: int(line.split()[1]) for line in handle if line.split()[1:]}
        return (info["MemTotal"] - info["MemAvailable"]) // 1024
    except (OSError, KeyError, ValueError, IndexError):
        pass
    if platform.system() != "Darwin" or total_mb is None:
        return None
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    page = re.search(r"page size of (\d+) bytes", out)
    counts = {
        m.group(1): int(m.group(2)) for m in re.finditer(r"^Pages ([\w ]+?):\s+(\d+)\.", out, re.MULTILINE)
    }
    if page is None or not {"free", "inactive"} <= counts.keys():
        return None
    available = counts["free"] + counts["inactive"] + counts.get("speculative", 0)
    return max(0, total_mb - available * int(page.group(1)) // MIB)


def uptime_s() -> int | None:
    """Seconds since the machine booted: /proc/uptime on Linux, the boot time on macOS."""
    try:
        with open("/proc/uptime") as handle:
            return int(float(handle.read().split()[0]))
    except (OSError, ValueError, IndexError):
        pass
    if platform.system() != "Darwin":
        return None
    try:
        out = subprocess.run(
            ["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"sec = (\d+)", out)
    return int(time.time() - int(match.group(1))) if match else None


def system_facts(api: Api, started: float) -> dict[str, Any]:
    """`NodeSystem`: the machine, from the facts the node already reports, plus what macOS needs."""
    facts = reports.system(api.config, started=started)
    return {
        "os": facts["os"] or "",
        "arch": facts["arch"] or "",
        "kernel": facts["kernel"],
        "cpu_model": facts["cpu_model"],
        "cores": facts["cores"],
        "mem_total_mb": facts["mem_total_mb"],
        "disk_total_mb": facts["disk_total_mb"],
        "gpu": None,
        "timezone": facts["timezone"],
        "uptime_s": facts["uptime_s"] if facts["uptime_s"] is not None else uptime_s(),
        "version": __version__,
        # This node was never enrolled with a controller, and its key is not read here.
        "enrolled_at": None,
        "enrolled_by": None,
        "key_fingerprint": None,
        "protocol": facts["protocol"],
        "reported_at": _now(),
    }


async def fleet_node(api: Api, started: float) -> dict[str, Any]:
    """This node as a row of the Computers list, `FleetNode`. `degraded` means a service it runs
    has failed or keeps restarting; a node that answers at all is `up`."""
    node = await local_node(api)
    cpu = node.get("cpu")
    if cpu is not None:  # the load average per core, capped at 100 as the node's own heartbeat caps it
        node["cpu"] = min(100.0, cpu)
    services = reports.services(api.runtime, started=started)
    degraded = any(item["state"] in ("failed", "restarting") for item in services)
    mem = node.get("mem")
    total = mem["total_mb"] if mem else None
    if total is None:
        total = (await asyncio.to_thread(system_facts, api, started))["mem_total_mb"]
    used = mem["used_mb"] if mem else None
    if used is None:
        used = await asyncio.to_thread(mem_used_mb, total)
    return {
        **node,
        "state": "degraded" if degraded else "up",
        "mem": {"used_mb": used, "total_mb": total} if used is not None and total is not None else None,
        "version": __version__,
        "degraded": degraded,
    }


def routes(api: Api) -> list:
    started = time.time()  # the API is built when agentosd starts, so this is the daemon's start

    def this(request: Request) -> None:
        if request.path_params["id"] != api.node_name:
            raise ApiError("not_found", "This computer has no such computer.")

    # ---- join codes: a node has nobody to join ----
    async def join_tokens(request: Request) -> list:
        del request
        return []

    async def join_token_create(request: Request) -> dict:
        del request
        raise ApiError("conflict", NO_JOIN)

    async def join_token_revoke(request: Request) -> None:
        del request
        raise ApiError("not_found", "There is no join code here to revoke.")

    # ---- this computer ----
    async def node(request: Request) -> dict:
        this(request)
        base = await fleet_node(api, started)
        system = await asyncio.to_thread(system_facts, api, started)
        mem = base["mem"] or {}
        disk = None
        try:
            disk = shutil.disk_usage(api.config.home).free // MIB
        except (OSError, AttributeError):
            pass
        return {
            **base,
            "os": system["os"],
            "arch": system["arch"],
            "load": {
                "cpu_pct": base["cpu"],
                "mem_used_mb": mem.get("used_mb"),
                "mem_total_mb": mem.get("total_mb"),
                "disk_free_mb": disk,
            },
            # The node's own lane rows (the panel's shape). web/computers.py shapes them to `Lane`.
            "lanes": await all_lanes(api, access_of(request)),
        }

    async def remove(request: Request) -> None:
        this(request)
        raise ApiError("conflict", NO_REMOVE)

    async def system(request: Request) -> dict:
        this(request)
        return await asyncio.to_thread(system_facts, api, started)

    async def programs(request: Request) -> list:
        this(request)
        rows = await asyncio.to_thread(reports.programs, api.runtime)
        # What the node does not record: a pin and an install date for node and lane programs.
        return [{"pinned_hash": None, "installed_at": None, **row} for row in rows]

    async def services(request: Request) -> list:
        this(request)
        rows = reports.services(api.runtime, started=started)
        # The supervisor keeps no port list, and does not sample a service's CPU or memory: those are
        # left out or null, never guessed.
        return [
            {"cpu_pct": None, "mem_mb": None, **{k: v for k, v in row.items() if k != "ports"}}
            for row in rows
        ]

    async def schedules(request: Request) -> list | None:
        this(request)
        return await asyncio.to_thread(schedules_of, api)

    async def tools(request: Request) -> list:
        this(request)
        return await asyncio.to_thread(tools_of, api)

    async def logs(request: Request) -> StreamingResponse:
        """A live tail of the node's log, as `event: log` frames. Lines are shaped and redacted as the
        node sends them to its controller. It ends when the reader does, or after the stream's limit."""
        this(request)
        from agentos.node.channel import logstream

        query = logstream.Query(log_query(request))
        needle = (request.query_params.get("q") or "").lower()
        scrub = logstream.Scrubber(api.runtime, api.config)
        reader = logstream.Reader(Path(api.config.home), query)
        deadline = time.monotonic() + api.settings.sse_max_seconds

        async def frames():
            reader.start()
            quiet_since = time.monotonic()
            try:
                while time.monotonic() < deadline and not await request.is_disconnected():
                    try:
                        item = await asyncio.to_thread(reader.lines.get, True, 1.0)
                    except queue.Empty:
                        if time.monotonic() - quiet_since >= api.settings.sse_heartbeat:
                            quiet_since = time.monotonic()
                            yield ": keep-alive\n\n"  # a comment, so a proxy keeps the stream open
                        continue
                    quiet_since = time.monotonic()
                    if item is None:
                        return
                    if item is logstream.LIVE:
                        continue
                    line = logstream.shape(item, scrub)
                    if needle and needle not in str(line.get("message", "")).lower():
                        continue
                    yield f"event: log\ndata: {json.dumps(line, ensure_ascii=False)}\n\n"
            finally:
                reader.stop()

        return StreamingResponse(
            frames(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def health(request: Request) -> list:
        this(request)
        rows = await asyncio.to_thread(reports.doctor, api.config)
        return [
            {"id": row["id"], "name": row["name"], "status": row["status"], "detail": row["detail"],
             "fix": row.get("fix"), "at": row["at"]}
            for row in rows
        ]

    async def connections(request: Request) -> None:
        # A node has no controller to be connected to, so it keeps no connection history to show.
        this(request)

    async def metrics(request: Request) -> dict:
        this(request)
        window = request.query_params.get("range", "24h")
        if window not in METRIC_BUCKETS:
            raise ApiError("invalid", "The range is 1h, 24h or 7d.")
        # No sampler keeps a history yet, so there are no points to draw. That is said, not faked.
        return {"range": window, "bucket_seconds": METRIC_BUCKETS[window], "points": []}

    def route(path: str, fn: Any, methods: list[str]) -> Route:
        return Route(path, endpoint(api, fn, owner=True), methods=methods)

    return [
        # Before `/api/nodes/{id}`, which would otherwise take `join-tokens` for a computer's name.
        route("/api/nodes/join-tokens", join_tokens, ["GET"]),
        route("/api/nodes/join-tokens", join_token_create, ["POST"]),
        route("/api/nodes/join-tokens/{id}", join_token_revoke, ["DELETE"]),
        route("/api/nodes/{id}", node, ["GET"]),
        route("/api/nodes/{id}", remove, ["DELETE"]),
        route("/api/nodes/{id}/system", system, ["GET"]),
        route("/api/nodes/{id}/programs", programs, ["GET"]),
        route("/api/nodes/{id}/services", services, ["GET"]),
        route("/api/nodes/{id}/schedules", schedules, ["GET"]),
        route("/api/nodes/{id}/tools", tools, ["GET"]),
        route("/api/nodes/{id}/logs/stream", logs, ["GET"]),
        route("/api/nodes/{id}/health", health, ["GET"]),
        route("/api/nodes/{id}/connections", connections, ["GET"]),
        route("/api/nodes/{id}/metrics", metrics, ["GET"]),
    ]
