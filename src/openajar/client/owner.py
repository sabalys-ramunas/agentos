"""The node's panel API through the tunnel: the calls `openajar connect` makes once with the node
token, and what the owner does from this computer with their device credential.

With the node token (read over SSH, kept in memory for this one connect, never stored):
- `POST /api/grants`: the connection's agent grant (subject `<client>@<computer>`, every lane,
  the owner's ceiling, no specials unless asked). Its token goes to the agent's store item;
- `POST /api/session/device`: the owner's device credential, an owner panel session with this
  computer's name as its label. It goes to the owner's store item.

With the device credential (sent as the session cookie, with the CSRF header on writes, exactly
like a signed-in panel): revoking the agent grant, listing and denying approvals, view links,
waking a lane. It is a session id, so /mcp and the bearer path refuse it: it never calls a tool.
It sits in the login Keychain, where an agent with a shell here can read it, so the node gives it
Presence.DEVICE, below a person: approving and answering need the panel (docs/THREAT-MODEL.md).

Standard library only, like mcp_http, so the commands start fast.
"""

from __future__ import annotations

import http.client
import json
from typing import Any
from urllib.parse import quote, urlencode

from openajar.client.errors import ClientError

SESSION_COOKIE = "agentos_session"  # openajar.api.auth.SESSION_COOKIE
CSRF_HEADER = "X-Agentos-CSRF"
TIMEOUT = 20.0


class PanelError(ClientError):
    """The node answered the panel API with an error (`status`, `code`), or did not answer (status 0)."""

    def __init__(self, status: int, code: str, message: str, hint: str | None = None):
        super().__init__(message, hint)
        self.status = status
        self.code = code

    @property
    def missing(self) -> bool:
        """The node does not have this route or service (an older node, or one without policy)."""
        return self.status in (404, 405, 501, 503) and self.code in ("not_found", "unavailable", "error", "http")


def lane_path(lane_id: str) -> str:
    workspace, _, name = lane_id.partition("/")
    if not workspace or not name or "/" in name:
        raise ClientError(f"{lane_id!r} is not a lane id", "lane ids look like workspace/name, for example biz-a/x")
    return f"/api/lanes/{quote(workspace, safe='')}/{quote(name, safe='')}"


class NodePanel:
    """One node's panel API at localhost:<port> (this computer's end of the tunnel)."""

    def __init__(self, port: int, *, host: str = "localhost", timeout: float = TIMEOUT):
        self.port = port
        self.host = host
        self.timeout = timeout
        self._csrf: dict[str, str] = {}

    def call(self, method: str, path: str, body: Any = None, *, bearer: str | None = None,
             device: str | None = None, params: dict[str, Any] | None = None) -> Any:
        headers = {"Accept": "application/json", "User-Agent": "openajar-connect"}
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        if device:
            headers["Cookie"] = f"{SESSION_COOKIE}={device}"
            if method not in ("GET", "HEAD"):
                headers[CSRF_HEADER] = self.csrf(device)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        query = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        url = path + (f"?{urlencode(query)}" if query else "")
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            conn.request(method, url, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        except (OSError, http.client.HTTPException) as exc:
            raise PanelError(0, "unreachable", f"the node did not answer on localhost:{self.port} "
                                               f"({getattr(exc, 'strerror', None) or type(exc).__name__})",
                             "is the tunnel up? See `openajar status`") from None
        finally:
            conn.close()
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None
        if resp.status >= 400:
            err = payload.get("error") if isinstance(payload, dict) else None
            code = str(err.get("code")) if isinstance(err, dict) and err.get("code") else "http"
            message = str(err.get("message")) if isinstance(err, dict) and err.get("message") else \
                f"HTTP {resp.status} {resp.reason} from {path}"
            raise PanelError(resp.status, code, message)
        return payload

    def csrf(self, device: str) -> str:
        """A session's CSRF value, read once from /api/status (the panel does the same)."""
        if device not in self._csrf:
            status = self.call("GET", "/api/status", device=device)
            value = status.get("csrf") if isinstance(status, dict) else None
            if not value:
                raise PanelError(401, "unauthorized", "the node did not accept the device credential")
            self._csrf[device] = value
        return self._csrf[device]

    # ---- with the node token, once, during connect ----

    def issue_agent_grant(self, owner_token: str, *, subject: str, ceiling: str, ttl: str,
                          specials: list[str]) -> dict:
        return self.call("POST", "/api/grants", {"subject": subject, "workspaces": ["*"], "ceiling": ceiling,
                                                 "specials": list(specials), "ttl": ttl}, bearer=owner_token)

    def record_route(self, owner_token: str, grant_id: str, *, ssh_host: str, local_port: int) -> Any:
        """Tell the node how this computer reaches it for this grant (browser_view's links)."""
        return self.call("POST", f"/api/grants/{quote(grant_id, safe='')}/route",
                         {"ssh_host": ssh_host, "local_port": local_port}, bearer=owner_token)

    def mint_device(self, owner_token: str, *, label: str) -> dict:
        return self.call("POST", "/api/session/device", {"label": label}, bearer=owner_token)

    # ---- with the owner's device credential ----

    def whoami(self, device: str) -> dict:
        return self.call("GET", "/api/status", device=device)

    def revoke_grant(self, device: str, grant_id: str) -> Any:
        return self.call("POST", f"/api/grants/{quote(grant_id, safe='')}/revoke", {}, device=device)

    def end_device(self, device: str) -> None:
        self.call("DELETE", "/api/session", device=device)
        self._csrf.pop(device, None)

    def approvals(self, device: str, *, state: str | None = "pending", limit: int = 50) -> list[dict]:
        return self.call("GET", "/api/approvals", device=device, params={"state": state, "limit": limit}) or []

    def decide(self, device: str, approval_id: str, decision: str, *, note: str | None = None) -> dict:
        body = {"note": note} if note else {}
        return self.call("POST", f"/api/approvals/{quote(approval_id, safe='')}/{decision}", body, device=device)

    def answer(self, device: str, question_id: str, answer: str) -> dict:
        return self.call("POST", f"/api/questions/{quote(question_id, safe='')}/answer", {"answer": answer},
                         device=device)

    def lane(self, device: str, lane_id: str) -> dict:
        return self.call("GET", lane_path(lane_id), device=device)

    def lane_open(self, device: str, lane_id: str) -> dict:
        return self.call("POST", lane_path(lane_id) + "/open", {}, device=device)

    def view_link(self, device: str, lane_id: str, mode: str = "control") -> dict:
        return self.call("POST", lane_path(lane_id) + "/view-link", {"mode": mode}, device=device)

    # ---- exposes and forwards (openajar.node.expose; the device credential or a person, never a grant) ----

    def exposes(self, device: str) -> list[dict]:
        out = self.call("GET", "/api/exposes", device=device)
        return list(out.get("links") or []) if isinstance(out, dict) else []

    def expose_create(self, device: str, body: dict[str, Any]) -> dict:
        return self.call("POST", "/api/exposes", body, device=device)

    def expose_ready(self, device: str, link_id: str) -> dict:
        return self.call("POST", f"/api/exposes/{quote(link_id, safe='')}/ready", {}, device=device)

    def expose_heartbeat(self, device: str, link_id: str) -> dict:
        return self.call("POST", f"/api/exposes/{quote(link_id, safe='')}/heartbeat", {}, device=device)

    def expose_end(self, device: str, link_id: str) -> dict:
        return self.call("DELETE", f"/api/exposes/{quote(link_id, safe='')}", device=device)
