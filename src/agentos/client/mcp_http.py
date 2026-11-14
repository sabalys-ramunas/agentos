"""A small MCP client over streamable HTTP: enough to check a tunnel and read whoami and lanes.

Standard library only, so connect and status start fast and can say exactly what is
wrong: nothing listens locally (the tunnel is down), the tunnel took the connection but
the node did not answer (agentosd is not running), or the node refused the token.
"""

from __future__ import annotations

import contextlib
import http.client
import json
from enum import StrEnum
from typing import Any, Self

from agentos import __version__

PROTOCOL_VERSION = "2025-06-18"
MCP_PATH = "/mcp"


class Failure(StrEnum):
    UNREACHABLE = "unreachable"    # nothing listens on the local port
    NO_ANSWER = "no_answer"        # the tunnel accepted, but no HTTP answer came from the node
    UNAUTHORIZED = "unauthorized"  # 401: wrong or rotated token
    BAD_RESPONSE = "bad_response"


class McpError(Exception):
    def __init__(self, failure: Failure, detail: str):
        super().__init__(detail)
        self.failure = failure
        self.detail = detail


class ToolError(Exception):
    """The tool ran and returned an error result (the text starts with its code)."""


def _error_text(resp: Any) -> str:
    """`code: message` from the shared error body ({"error": {"code", "message", "fix"}}), or ''."""
    try:
        body = json.loads(resp.read(65536) or b"{}")
    except (ValueError, OSError):
        return ""
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict) and isinstance(err.get("code"), str):
        text = f"{err['code']}: {err.get('message') or ''}".strip()
        return f"{text} (fix: {err['fix']})" if isinstance(err.get("fix"), str) and err["fix"] else text
    return ""


class McpSession:
    def __init__(self, port: int, token: str, *, timeout: float = 10.0, host: str = "localhost"):
        self.host = host
        self.port = port
        self._token = token
        self.timeout = timeout
        self.session_id: str | None = None
        self.protocol: str | None = None
        self.server: dict[str, Any] = {}
        self._next_id = 0

    def __enter__(self) -> Self:
        self.initialize()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self._token}",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        return headers

    def _open(self) -> http.client.HTTPConnection:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            conn.connect()
        except OSError as exc:
            conn.close()
            raise McpError(Failure.UNREACHABLE,
                           f"nothing answers on localhost:{self.port} ({exc.strerror or exc})") from None
        return conn

    def _send(self, method: str, body: bytes | None) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        conn = self._open()
        try:
            conn.request(method, MCP_PATH, body=body, headers=self._headers())
            return conn, conn.getresponse()
        except TimeoutError:
            conn.close()
            raise McpError(Failure.NO_ANSWER, f"no answer through localhost:{self.port} in {self.timeout:.0f}s") \
                from None
        except (http.client.HTTPException, ConnectionError, OSError):
            conn.close()
            raise McpError(Failure.NO_ANSWER,
                           f"localhost:{self.port} accepted the connection but closed it without an answer") from None

    def _rpc(self, method: str, params: dict[str, Any] | None = None, *, notify: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notify:
            self._next_id += 1
            payload["id"] = self._next_id
        conn, resp = self._send("POST", json.dumps(payload).encode())
        try:
            if resp.status == 401:
                raise McpError(Failure.UNAUTHORIZED, "the node refused the token (401)")
            if resp.status >= 400:
                why = _error_text(resp)  # the node's own error body says what happened (frozen, denied ...)
                if resp.status in (403, 421):
                    raise McpError(Failure.BAD_RESPONSE, why or f"the node refused the request ({resp.status} {resp.reason})")
                raise McpError(Failure.BAD_RESPONSE, why or f"HTTP {resp.status} {resp.reason} from {MCP_PATH}")
            if session := resp.getheader("mcp-session-id"):
                self.session_id = session
            if notify:
                resp.read()
                return {}
            message = self._read_message(resp, payload["id"])
        except TimeoutError:
            raise McpError(Failure.NO_ANSWER, f"the answer from localhost:{self.port} stopped midway") from None
        finally:
            conn.close()
        if "error" in message:
            error = message["error"] or {}
            raise McpError(Failure.BAD_RESPONSE, f"{method} failed: {error.get('message', error)}")
        return message.get("result") or {}

    def _read_message(self, resp: http.client.HTTPResponse, want_id: int) -> dict[str, Any]:
        kind = (resp.getheader("content-type") or "").split(";")[0].strip().lower()
        if kind == "application/json":
            return self._match(json.loads(resp.read() or b"{}"), want_id)
        if kind != "text/event-stream":
            raise McpError(Failure.BAD_RESPONSE, f"unexpected content type {kind or 'none'} from {MCP_PATH}")
        data: list[str] = []
        while True:
            raw = resp.readline()
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not raw or not line:
                if data:
                    with contextlib.suppress(ValueError):
                        found = self._match(json.loads("\n".join(data)), want_id)
                        if found:
                            return found
                    data = []
                if not raw:
                    break
                continue
            if line.startswith("data:"):
                data.append(line[5:].removeprefix(" "))
        raise McpError(Failure.BAD_RESPONSE, "the event stream ended without an answer")

    @staticmethod
    def _match(message: Any, want_id: int) -> dict[str, Any]:
        items = message if isinstance(message, list) else [message]
        for item in items:
            if isinstance(item, dict) and item.get("id") == want_id:
                return item
        return {}

    def initialize(self) -> dict[str, Any]:
        result = self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "agentos-connect", "version": __version__},
        })
        self.protocol = result.get("protocolVersion") or PROTOCOL_VERSION
        self.server = result.get("serverInfo") or {}
        self._rpc("notifications/initialized", notify=True)
        return result

    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> Any:
        result = self._rpc("tools/call", {"name": tool, "arguments": arguments or {}})
        texts = [c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text"]
        if result.get("isError"):
            raise ToolError("\n".join(texts) or f"{tool} failed")
        structured = result.get("structuredContent")
        if isinstance(structured, dict) and set(structured) != {"result"}:
            return structured
        if isinstance(structured, dict):
            return structured["result"]
        joined = "\n".join(texts)
        try:
            return json.loads(joined)
        except ValueError:
            return joined

    def close(self) -> None:
        if not self.session_id:
            return
        with contextlib.suppress(McpError):
            conn, resp = self._send("DELETE", None)
            with contextlib.suppress(OSError, http.client.HTTPException):
                resp.read()
            conn.close()
        self.session_id = None


PROBE_TIMEOUT = 5.0  # how long `agentos connect` waits for one probe of a node


def probe(port: int, token: str, *, timeout: float = 5.0) -> dict[str, Any]:
    """An MCP initialize with the token; returns the server's info or raises McpError."""
    with McpSession(port, token, timeout=timeout) as session:
        return session.server
