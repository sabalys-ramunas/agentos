"""The day plan in the panel (agentos.plan): one view across accounts and programs, and a draft of the day.

  GET /api/plan/status?workspace=    every account and program on this computer, as `agentos plan status`
  GET /api/plan/draft?workspace=     the day's draft, as `agentos plan draft`: what is blocked, what waits
                                     in the queue, what is due, and what to look at

Both only read the node's files and queue nothing, so nothing here can send, post or change an account. A
member sees the workspaces where their role has `see`; the owner sees all. A grant is refused, and so is a
bearer token: this is a person's view of the day, read in the panel.
"""

from __future__ import annotations

import asyncio
from typing import Any

from starlette.requests import Request
from starlette.routing import Route

from agentos.api.app import Api, access_of, at_the_panel, endpoint
from agentos.api.errors import ApiError
from agentos.core.errors import ServiceUnavailable
from agentos.plan.state import PlanState

PERSON_ONLY = "Only a person signed in to the panel can read the day plan. A token cannot, the owner token included."


def routes(api: Api) -> list[Route]:
    from agentos.plan.draft import draft_day
    from agentos.plan.state import collect

    def config() -> Any:
        cfg = api.runtime.get("config")
        if cfg is None:
            raise ServiceUnavailable("this computer has no configuration to plan from")
        return cfg

    def visible(request: Request, state: PlanState) -> PlanState:
        """The part of the plan this caller may read: a member's workspaces with `see`, or all of it."""
        access = access_of(request)
        spaces = access.workspaces_with("see") if access.member is not None else None  # type: ignore[attr-defined]
        if spaces is None:
            return state
        state.lanes = [row for row in state.lanes if set(row.workspaces) & spaces]
        state.unattached_approvals = {ws: n for ws, n in state.unattached_approvals.items() if ws in spaces}
        return state

    async def build(request: Request) -> PlanState:
        if not at_the_panel(request):
            access_of(request).trace("plan.status", status="denied", summary="the day plan is for a person")
            raise ApiError("denied", PERSON_ONLY)
        cfg = config()
        ws = request.query_params.get("workspace") or None
        state = await asyncio.to_thread(collect, cfg, workspace=ws)
        return visible(request, state)

    async def status(request: Request) -> dict[str, Any]:
        state = await build(request)
        return state.model_dump(mode="json")

    async def draft(request: Request) -> dict[str, Any]:
        state = await build(request)
        return draft_day(state).model_dump(mode="json")

    return [
        Route("/api/plan/status", endpoint(api, status, cap="see", action="plan.status"), methods=["GET"]),
        Route("/api/plan/draft", endpoint(api, draft, cap="see", action="plan.draft"), methods=["GET"]),
    ]
