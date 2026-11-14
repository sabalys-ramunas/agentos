"""Retention and the export, as the app reads them (web/src/api/types.ts, `Retention`).

A node keeps its action log for `trace_retention_days` (policy.toml, 90 by default). The policy's
daily sweep removes what is older (`TraceStore.purge`), so the window shown here is the one the sweep
applies, and nothing on this screen sets it. A computer has no plan, so `plan` is left out.

Runs are not swept: a run's record stays on the node until someone removes it, and the screen says
so rather than naming a window that nothing enforces.

The export is the action log, oldest first, as CSV or NDJSON. Only the owner may take it, because it
holds every workspace.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import BaseRoute

from agentos.api.app import Api, access_of
from agentos.api.errors import ApiError
from agentos.api.web.base import ORG, org_route
from agentos.policy.util import iso

EXPORT_PAGE = 500
FORMATS = ("ndjson", "csv")
COLUMNS = ("id", "at", "kind", "tool", "lane", "workspace", "principal", "effect", "status", "summary", "run_id")
# A spreadsheet runs a cell that starts with one of these, and summaries come from lanes.
FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")
MEDIA = {"csv": "text/csv; charset=utf-8", "ndjson": "application/x-ndjson"}


def cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(FORMULA_STARTS) else text


def when(value: str | None, name: str) -> float | None:
    """A date and time from the query string, as a timestamp. A date with no zone is UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ApiError("invalid", f"{name} should be a date and time, such as 2026-11-01T00:00:00Z.") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def may_export(access: Any) -> bool:
    """The owner, in person. The export is every workspace's history in one file, so a token (an
    agent's, or the owner's own) never takes it, the same as the other things only a person does."""
    return bool(access.owner and access.human)


def routes(api: Api) -> list[BaseRoute]:
    def log() -> Any:
        store = api.runtime.get("traces")
        if store is None:
            raise ApiError("unavailable", "This computer keeps no action log.")
        return store

    async def retention(request: Request) -> dict[str, Any]:
        store = log()
        oldest = await asyncio.to_thread(store.oldest_at)
        return {
            "retention_days": int(store.retention_days),
            "kept_days": int(store.retention_days),
            "hold_until": None,
            "oldest_at": iso(oldest),
            "can_export": may_export(access_of(request)),
        }

    async def export(request: Request) -> StreamingResponse:
        access = access_of(request)
        if not may_export(access):
            access.trace("activity.export", status="denied", summary="the export needs the owner in person")
            raise ApiError("denied", "Only the owner, signed in to the panel, can export the history.")
        fmt = request.query_params.get("format", "ndjson")
        if fmt not in FORMATS:
            raise ApiError("invalid", "Export the history as csv or ndjson.")
        since = when(request.query_params.get("start"), "start")
        until = when(request.query_params.get("end"), "end")
        workspace = request.query_params.get("workspace") or None
        store = log()
        access.trace("activity.export", summary=f"exported the action log as {fmt}")

        async def body() -> AsyncIterator[bytes]:
            if fmt == "csv":
                yield (",".join(COLUMNS) + "\n").encode()
            after = 0
            while True:
                page = await asyncio.to_thread(store.export_page, after=after, since=since, until=until,
                                               workspace=workspace, limit=EXPORT_PAGE)
                if not page:
                    return
                after = page[-1][0]
                rows = []
                for _, event in page:
                    shown = event.public()
                    rows.append({column: shown.get(column) for column in COLUMNS})
                if fmt == "csv":
                    buffer = io.StringIO()
                    writer = csv.writer(buffer)
                    for row in rows:
                        writer.writerow([cell(row[column]) for column in COLUMNS])
                    yield buffer.getvalue().encode()
                else:
                    yield "".join(json.dumps(row) + "\n" for row in rows).encode()

        stamp = datetime.now(UTC).strftime("%Y-%m-%d")
        disposition = f'attachment; filename="agentos-activity-{ORG}-{stamp}.{fmt}"'
        return StreamingResponse(body(), media_type=MEDIA[fmt], headers={"Content-Disposition": disposition})

    return [
        org_route(api, "/retention", retention, ["GET"]),
        org_route(api, "/activity/export", export, ["GET"], owner=True),
    ]
