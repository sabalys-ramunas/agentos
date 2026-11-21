"""The live stream of an org: `GET /api/orgs/{org}/events`, server-sent events (web/src/api/live.ts).

The node streams its own events (docs/API.md, "Live updates"). This route reads them as the node
writes them and reshapes each payload to the type `live.ts` applies, so the node decides who hears
what, and the frames, ids, heartbeats and resync markers pass through as the node wrote them.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import BaseRoute

from openajar.api.app import Api
from openajar.api.web.accounts import lane_for_app
from openajar.api.web.base import Opened, open_stream, org_route

Reshape = Callable[[Any], Any]


def control_for_app(payload: dict[str, Any]) -> dict[str, Any]:
    """`{ lane, control }`, the shape of the `control` event: the lane's control, read as the app's."""
    return {"lane": payload["lane"], "control": dict(payload.get("control") or {})}


TRACE_STATUS = {
    "ok": "succeeded_unverified",  # the call went through, and nothing checked its result
    "unverifiable": "succeeded_unverified",
    "started": "running",
    "planned": "queued",
    "denied": "failed",
    "error": "failed",
}


def trace_for_app(payload: dict[str, Any]) -> dict[str, Any]:
    """A trace event's status is a call's outcome; the app reads it as a run's (`RunStatus`)."""
    return {**payload, "status": TRACE_STATUS.get(payload.get("status"), "failed")}


def run_for_app(payload: dict[str, Any]) -> dict[str, Any]:
    """A run as the app's `Run`: a run with no lane says so with an empty string, and one not yet
    started is shown from when it was asked, since the app's `started_at` is always a time."""
    return {**payload, "lane": payload.get("lane") or "",
            "started_at": payload.get("started_at") or payload.get("created_at")}


def approval_for_app(payload: dict[str, Any]) -> dict[str, Any] | None:
    """An approval as the app's `Approval`. A question is an approval the node also sends as a
    `question`, which the app reads in its own shape, and a question has no effect or content to
    approve, so it is not sent as an approval at all."""
    return None if payload.get("kind") == "question" else payload


RESHAPE: dict[str, Reshape] = {
    "lane": lane_for_app,
    "control": control_for_app,
    "approval": approval_for_app,
    "trace": trace_for_app,
    "run": run_for_app,
}
"""Event name to the function that makes its payload the app's type. Any other name passes as it is,
and a function that returns None drops the frame."""


def reshape_frame(frame: bytes) -> bytes | None:
    """One SSE frame, with its `data:` payload reshaped when the app reads that event. Comments
    (`: ping`), ids, `retry:` and frames the app does not read are returned as they came. None: the
    frame is not for the app."""
    text = frame.decode("utf-8")
    kind: str | None = None
    data: list[str] = []
    for line in text.split("\n"):
        if line.startswith("event: "):
            kind = line[len("event: "):]
        elif line.startswith("data: "):
            data.append(line[len("data: "):])
    if kind not in RESHAPE or not data:
        return frame
    try:
        payload = RESHAPE[kind](json.loads("\n".join(data)))
    except (ValueError, KeyError, TypeError):  # a frame the node wrote unreadably: say nothing about it
        return frame
    if payload is None:
        return None
    kept = [line for line in text.split("\n") if line and not line.startswith("data: ")]
    lines = kept + [f"data: {json.dumps(payload, separators=(',', ':'), default=str)}", "", ""]
    return "\n".join(lines).encode("utf-8")


async def _frames(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Split the node's bytes at each blank line, so a frame cut across two writes is read whole."""
    pending = b""
    async for chunk in chunks:
        pending += chunk
        while True:
            end = pending.find(b"\n\n")
            if end < 0:
                break
            frame, pending = pending[:end + 2], pending[end + 2:]
            out = reshape_frame(frame)
            if out is not None:
                yield out
    if pending:
        yield pending


def routes(api: Api) -> list[BaseRoute]:
    async def events(request: Request) -> Response:
        opened: Opened = await open_stream(request, "/api/events",
                                           query={"last_event_id": request.query_params.get("last_event_id")})
        if opened.status >= 300:  # refused, or no stream here: the node's answer, as it was
            body = b"".join([chunk async for chunk in opened.chunks])
            kind = next((v.decode("latin-1") for k, v in opened.headers if k.lower() == b"content-type"), None)
            return Response(body, status_code=opened.status, media_type=kind)
        return StreamingResponse(_frames(opened.chunks), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return [org_route(api, "/events", events, ["GET"])]
