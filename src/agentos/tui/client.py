"""Async client for the panel API (docs/API.md) and its Server-Sent Events stream.

Every call raises one of four things on failure, so callers can degrade calmly:
  NodeUnreachable  the node did not answer (down, tunnel closed, timeout)
  AuthFailed       401: the token was refused
  Unavailable      503, or a 404 for a whole endpoint: not configured on this node
  ApiError         anything else the node refused, with its error code and message
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from agentos.core.models import parse_lane_id


class NodeUnreachable(Exception):
    def __init__(self, message: str, *, sent: bool = False):
        super().__init__(message)
        self.message = message
        self.sent = sent  # the request may have reached the node before the failure


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class AuthFailed(ApiError):
    pass


class Unavailable(ApiError):
    pass


@dataclass
class SseEvent:
    event: str
    data: Any
    id: str | None = None


class SseParser:
    """Incremental text/event-stream parser (the WHATWG rules we need: event, data, id, retry,
    comments, multi-line data). Feed lines without their newline; a blank line dispatches."""

    def __init__(self) -> None:
        self.last_id: str | None = None
        self.retry_ms: int | None = None
        self._event = ""
        self._data: list[str] = []

    def feed(self, line: str) -> SseEvent | None:
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return None  # comment, used as a heartbeat
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "event":
            self._event = value
        elif name == "data":
            self._data.append(value)
        elif name == "id":
            if "\0" not in value:
                self.last_id = value
        elif name == "retry" and value.isdigit():
            self.retry_ms = int(value)
        return None

    def _dispatch(self) -> SseEvent | None:
        event, data = self._event or "message", self._data
        self._event, self._data = "", []
        if not data:
            return None
        raw = "\n".join(data)
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = raw
        return SseEvent(event=event, data=payload, id=self.last_id)


def _seg(value: str) -> str:
    return quote(value, safe="")


def lane_path(lane_id: str) -> str:
    workspace, name = parse_lane_id(lane_id)
    return f"/api/lanes/{_seg(workspace)}/{_seg(name)}"


def _error_from(response: httpx.Response) -> ApiError:
    code, message = "error", ""
    try:
        body = response.json()
        err = body.get("error", {}) if isinstance(body, dict) else {}
        code = str(err.get("code") or code)
        message = str(err.get("message") or "")
    except ValueError:
        message = response.text[:200].strip()
    if not message:
        message = response.reason_phrase or f"HTTP {response.status_code}"
    status = response.status_code
    if status == 401:
        return AuthFailed(status, code if code != "error" else "unauthorized", message)
    if status == 503:
        return Unavailable(status, code if code != "error" else "unavailable", message)
    return ApiError(status, code, message)


def _unreachable(exc: httpx.HTTPError) -> NodeUnreachable:
    sent = not isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout)
    kind = type(exc).__name__
    detail = str(exc).strip()
    reason = {
        "ConnectError": "connection refused",
        "ConnectTimeout": "connection timed out",
        "ReadTimeout": "no answer in time",
        "WriteTimeout": "no answer in time",
        "PoolTimeout": "no answer in time",
        "RemoteProtocolError": "connection dropped",
        "ReadError": "connection dropped",
    }.get(kind, detail or kind)
    if kind == "ConnectError" and detail and "refused" not in detail.lower():
        reason = detail
    return NodeUnreachable(reason, sent=sent)


def _operator_presence(home: Any) -> dict[str, str] | None:
    """A one-time code in this node's panel.db, beside the node token (agentos.cli.operator_presence)."""
    import sqlite3

    from agentos.api.auth import PRESENCE_HEADER, PanelStore, new_login_code

    code = new_login_code()
    try:
        PanelStore.for_home(home).add_code(code, ttl_seconds=60)
    except (OSError, sqlite3.Error):
        return None
    return {PRESENCE_HEADER: code}


class ApiClient:
    """One client per dashboard. Paths are relative to the node's base URL."""

    def __init__(self, url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 10.0, sse_idle_timeout: float = 90.0, session: str = "",
                 home: Any = None):
        """`session`: the owner's device credential (`agentos connect`), sent as the panel's session
        cookie; writes then carry its CSRF value, read once from /api/status. `home`: on the node, with
        the node token from its node.toml, every write carries a one-time code from that home's panel.db,
        so approving and answering are a person's, as for the node's CLI (docs/THREAT-MODEL.md)."""
        self.base_url = url.rstrip("/")
        headers = {"Accept": "application/json", "User-Agent": "agentos-dash"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif session:
            headers["Cookie"] = f"agentos_session={session}"
        self._session = bool(session) and not token
        self._home = home if token else None
        self._csrf: str | None = None
        self._http = httpx.AsyncClient(base_url=self.base_url, headers=headers, transport=transport,
                                       timeout=timeout, follow_redirects=False)
        self._sse_timeout = httpx.Timeout(timeout, read=sse_idle_timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    def absolute(self, path: str) -> str:
        if "://" in path:
            return path
        return self.base_url + ("" if path.startswith("/") else "/") + path

    # ---- plumbing ----
    async def request(self, method: str, path: str, *, params: dict | None = None,
                      body: Any = None, missing_is_unavailable: bool = False) -> httpx.Response:
        clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        headers = None
        if self._session and method not in ("GET", "HEAD"):
            if self._csrf is None:
                self._csrf = (await self.status()).get("csrf") or ""
            headers = {"X-Agentos-CSRF": self._csrf}
        elif self._home is not None and method not in ("GET", "HEAD"):
            headers = _operator_presence(self._home)
        try:
            response = await self._http.request(method, path, params=clean or None,
                                                json=body if body is not None else None, headers=headers)
        except httpx.HTTPError as exc:
            raise _unreachable(exc) from None
        if response.status_code >= 400:
            err = _error_from(response)
            if missing_is_unavailable and response.status_code in (404, 405) \
                    and err.code in ("error", "not_found"):
                raise Unavailable(response.status_code, "unavailable", "not available on this node")
            raise err
        return response

    async def get(self, path: str, params: dict | None = None, *, collection: bool = True) -> Any:
        response = await self.request("GET", path, params=params, missing_is_unavailable=collection)
        return response.json()

    async def post(self, path: str, body: Any = None) -> Any:
        response = await self.request("POST", path, body={} if body is None else body)
        return _json_or_none(response)

    # ---- status, workspaces, lanes ----
    async def status(self) -> dict:
        return await self.get("/api/status")

    async def workspaces(self) -> list:
        return await self.get("/api/workspaces")

    async def lane(self, lane_id: str) -> dict:
        return await self.get(lane_path(lane_id), collection=False)

    async def lane_open(self, lane_id: str) -> Any:
        return await self.post(lane_path(lane_id) + "/open")

    async def lane_close(self, lane_id: str) -> Any:
        return await self.post(lane_path(lane_id) + "/close")

    async def lane_control(self, lane_id: str, action: str) -> Any:
        return await self.post(lane_path(lane_id) + "/control", {"action": action})

    async def view_link(self, lane_id: str, mode: str = "view", ttl_s: int | None = None) -> dict:
        body: dict[str, Any] = {"mode": mode}
        if ttl_s:
            body["ttl_s"] = ttl_s
        return await self.post(lane_path(lane_id) + "/view-link", body)

    async def screenshot(self, lane_id: str) -> bytes:
        response = await self.request("GET", lane_path(lane_id) + "/screenshot")
        return response.content

    async def fetch_bytes(self, path: str) -> bytes:
        response = await self.request("GET", path)
        return response.content

    # ---- nodes ----
    async def nodes(self) -> list:
        return await self.get("/api/nodes")

    # ---- Needs you ----
    async def approvals(self, state: str = "pending", limit: int = 100) -> list:
        return await self.get("/api/approvals", {"state": state, "limit": limit})

    async def approve(self, approval_id: str, note: str | None = None) -> Any:
        return await self.post(f"/api/approvals/{_seg(approval_id)}/approve", {"note": note} if note else {})

    async def deny(self, approval_id: str, note: str | None = None) -> Any:
        return await self.post(f"/api/approvals/{_seg(approval_id)}/deny", {"note": note} if note else {})

    async def batch_decide(self, ids: list[str], decision: str) -> Any:
        return await self.post("/api/approvals/batch", {"ids": list(ids), "decision": decision})

    async def questions(self, state: str = "open") -> list:
        return await self.get("/api/questions", {"state": state})

    async def answer(self, question_id: str, answer: str) -> Any:
        return await self.post(f"/api/questions/{_seg(question_id)}/answer", {"answer": answer})

    # ---- activity and runs ----
    async def activity(self, *, lane: str | None = None, workspace: str | None = None,
                       kind: str | None = None, before: str | None = None, limit: int = 300) -> list:
        return await self.get("/api/activity", {"lane": lane, "workspace": workspace, "kind": kind,
                                                "before": before, "limit": limit})

    async def runs(self, status: str | None = None, limit: int = 100) -> list:
        return await self.get("/api/runs", {"status": status, "limit": limit})

    async def run(self, run_id: str) -> dict:
        return await self.get(f"/api/runs/{_seg(run_id)}", collection=False)

    # ---- flows and schedules ----
    async def flows(self) -> list:
        return await self.get("/api/flows")

    async def run_flow(self, flow_id: str, params: dict | None = None) -> Any:
        return await self.post(f"/api/flows/{_seg(flow_id)}/run", {"params": params or {}})

    async def schedules(self) -> list:
        return await self.get("/api/schedules")

    async def add_schedule(self, flow: str, cron: str, params: dict | None = None) -> Any:
        return await self.post("/api/schedules", {"flow": flow, "cron": cron, "params": params or {}})

    async def delete_schedule(self, schedule_id: str) -> Any:
        response = await self.request("DELETE", f"/api/schedules/{_seg(schedule_id)}")
        return _json_or_none(response)

    async def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> Any:
        response = await self.request("PATCH", f"/api/schedules/{_seg(schedule_id)}",
                                      body={"enabled": enabled})
        return _json_or_none(response)

    # ---- brains ----
    async def brains(self) -> list:
        return await self.get("/api/brains")

    async def brain_tasks(self) -> list:
        return await self.get("/api/brains/tasks")

    async def brain_task(self, task_id: str) -> dict:
        return await self.get(f"/api/brains/tasks/{_seg(task_id)}", collection=False)

    # ---- live updates ----
    async def events(self, last_event_id: str | None = None) -> AsyncIterator[SseEvent]:
        """Yield SseEvent("open") once connected, then every event until the stream ends.
        Reconnecting is the caller's job (agentos.tui.sync)."""
        headers = {"Accept": "text/event-stream", "Cache-Control": "no-cache"}
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        parser = SseParser()
        try:
            async with self._http.stream("GET", "/api/events", headers=headers,
                                         timeout=self._sse_timeout) as response:
                if response.status_code >= 400:
                    await response.aread()
                    err = _error_from(response)
                    if response.status_code in (404, 405):
                        raise Unavailable(response.status_code, "unavailable",
                                          "live updates are not available on this node")
                    raise err
                yield SseEvent(event="open", data=None)
                async for line in response.aiter_lines():
                    event = parser.feed(line.rstrip("\r"))
                    if event is not None:
                        yield event
        except httpx.HTTPError as exc:
            raise _unreachable(exc) from None


def _json_or_none(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return None
