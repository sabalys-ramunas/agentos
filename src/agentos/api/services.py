"""What the HTTP API needs from each runtime service.

The API looks services up by their well-known runtime name (agentos.core.runtime) and
answers 503 `unavailable` when one is missing. Every method may be sync or async; the
API awaits whatever comes back. Results may be dicts or pydantic models; the shapes are
the ones in docs/API.md. Methods marked optional are looked up with getattr and the
API degrades gracefully without them.

These are deliberately small so the lead can adapt each package with a thin wrapper: the
real services are adapted in agentos.api.bridge. List and get methods may also accept
`viewer=<grant id>` (approvals, traces): a grant's caller then sees only its own grant family.
A service that does not accept it gives a scoped caller nothing (agentos.api.app.scoped_list).
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

from agentos.core.models import LaneInfo, LaneSpec, Principal, ProxyConfig, ViewerInfo


class LaneService(Protocol):
    """runtime "lanes": agentos.node.interfaces.LaneManager, used as is.

    Optional extra: `hints(lane_id) -> {"signal": str, "logged_in_hint": str, "preset": str}`
    (the last deterministic signal and login state, which LaneInfo does not carry yet).
    Optional extra: `screenshot(lane_id) -> bytes`; otherwise the API takes one through
    `using()` plus the "interaction" service, and only for a lane that is already warm.
    """

    def list(self) -> list[LaneInfo]: ...
    def get(self, lane_id: str) -> LaneInfo: ...
    async def create(self, spec: LaneSpec) -> LaneInfo: ...
    async def open(self, lane_id: str) -> LaneInfo: ...
    async def close(self, lane_id: str) -> LaneInfo: ...
    def view(self, lane_id: str) -> ViewerInfo | None: ...
    def using(self, lane_id: str) -> AbstractAsyncContextManager[Any]: ...


class WorkspaceService(Protocol):
    """runtime "workspaces" (optional). Without it the API keeps names and colors in panel.db."""

    def list(self) -> list[dict]: ...  # [{"id", "name", "color"}]
    def create(self, *, id: str, name: str, color: str | None) -> dict: ...


class ApprovalService(Protocol):
    """runtime "approvals" (agentos.policy). Approval shape as in docs/API.md.

    Optional: `preview_image(approval_id) -> bytes | None` (PNG),
    `list_standing() -> list[dict]`, `create_standing(*, lane, effect, ttl_seconds, principal, note)
    -> dict`, `revoke_standing(standing_id) -> dict | None`. A standing approval is "commit on this
    lane inside this window without asking again" (ARCHITECTURE.md section 5).
    """

    def list(self, *, state: str | None = None, limit: int = 50) -> list[dict]: ...
    def get(self, approval_id: str) -> dict: ...  # raises NotFound
    def decide(self, approval_id: str, *, decision: str, principal: Principal,
               note: str | None = None) -> dict: ...  # decision: "approve" | "deny"
    # Optional for questions (a question is an approval of kind "question"; the bridge's adapter has
    # `kind=` and `answer=`): list(kind="question") and decide(..., answer=...) answer a human_ask.


class TraceService(Protocol):
    """runtime "traces" (agentos.policy). Events are redacted at write.

    Optional: `cost_today() -> {"tokens": int, "usd": float}`.
    """

    def query(self, *, lane: str | None = None, workspace: str | None = None, kind: str | None = None,
              before: str | None = None, limit: int = 100) -> list[dict]: ...


class RunnerService(Protocol):
    """runtime "runner" (agentos.runner). Runs carry honest RunStatus values and proof."""

    def list_runs(self, *, status: str | None = None, limit: int = 50, flow: str | None = None) -> list[dict]: ...  # flow: one routine's runs
    def get_run(self, run_id: str) -> dict: ...  # with "steps"; raises NotFound
    def list_schedules(self) -> list[dict]: ...  # each with grant_id, enabled, next_fire_at, lane, last_run
    def get_schedule(self, schedule_id: str) -> dict: ...  # raises NotFound
    def create_schedule(self, *, flow: str, cron: str | None, watch: str | None, params: dict,
                        deadman_minutes: int | None, principal: Principal) -> dict: ...
    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> dict: ...  # keeps the grant_id
    def delete_schedule(self, schedule_id: str) -> None: ...


class FlowService(Protocol):
    """runtime "flows" (agentos.runner). `params` in a flow is a JSON schema object."""

    def list(self) -> list[dict]: ...
    def run(self, flow_id: str, *, params: dict, principal: Principal) -> dict: ...  # returns a run


class BrainService(Protocol):
    """runtime "brains" (agentos.brains).

    Optional: `status() -> {"name", "state", "offline_ready"}` for the top-bar chip; without it the
    API uses the first brain in router order.
    """

    def list(self) -> list[dict]: ...
    def set_order(self, order: list[str]) -> Any: ...
    def list_tasks(self, *, limit: int = 50) -> list[dict]: ...
    def get_task(self, task_id: str) -> dict: ...


class VaultService(Protocol):
    """runtime "vault" (agentos.vault). list() returns names only; the API whitelists fields anyway."""

    def list(self) -> list[dict]: ...
    def create(self, *, name: str, kind: str, domain: str, lanes: list[str], username: str | None,
               secret: str, principal: Principal, totp: str | None = None) -> dict: ...
    def delete(self, entry_id: str) -> None: ...


class PolicyService(Protocol):
    """runtime "policy" (agentos.policy): panel auth and grants.

    `authenticate_panel(token)` returns the owner's Principal for the node token, a grant's
    Principal for a grant token, else None. With `context_for_token(token, tool=)` and
    `owner_context(tool=)` (agentos.policy) the API scopes every request to that grant
    (agentos.api.access); a policy without them is trusted as the owner (tests, fakes).
    """

    def authenticate_panel(self, token: str) -> Principal | None: ...
    def list_grants(self) -> list[dict]: ...
    def issue_grant(self, *, subject: str, workspaces: list[str], lanes: list[str] | None, ceiling: str,
                    specials: list[str], budget: dict | None, ttl: str,
                    principal: Principal) -> dict: ...  # {"id", "token", ...}
    def revoke_grant(self, grant_id: str) -> dict | None: ...


class ProxyService(Protocol):
    """runtime "proxies" (optional). Without it the API reads and writes secrets/proxies.json
    through agentos.core.config, as the M1 CLI does."""

    def list(self) -> list[ProxyConfig]: ...
    def add(self, proxy: ProxyConfig) -> ProxyConfig: ...


class NodeService(Protocol):
    """runtime "nodes" (optional, the controller in M5). Without it the API reports this node."""

    def list(self) -> list[dict]: ...


class NotifierService(Protocol):
    """runtime "notifier" (agentos.policy). Settings must hold public fields only (a webhook
    secret is write-only); the API strips sensitive keys before answering."""

    def get_settings(self) -> dict: ...
    def put_settings(self, settings: dict) -> dict: ...
    def test(self) -> dict: ...  # {"ok": bool, "detail": str}


class TerminalSession(Protocol):
    async def read(self) -> str | bytes | None: ...  # None when the shell has exited
    async def write(self, data: str) -> None: ...
    async def resize(self, cols: int, rows: int) -> None: ...
    async def close(self) -> None: ...


class EnvService(Protocol):
    """runtime "envs" (agentos.env): terminal lanes."""

    async def open_terminal(self, lane_id: str, *, principal: Principal, cols: int = 80,
                            rows: int = 24) -> TerminalSession: ...
