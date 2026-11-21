"""Billing, as the app reads it. A node has no plan and nothing to pay for, so there is no billing
portal to open. The answer says so, and the app never offers the button (web/src/features/settings).

The status the app reads has no `plan` or `usage` here: the fields are optional in
web/src/api/types.ts, and the billing screen says that this computer has no plan.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.routing import BaseRoute

from openajar.api.app import Api
from openajar.api.errors import ApiError
from openajar.api.web.base import org_route

NO_BILLING = "This computer has no billing. It has no plan and nothing to pay, so there is no portal to open."


def routes(api: Api) -> list[BaseRoute]:
    async def portal(request: Request) -> dict[str, Any]:
        raise ApiError("not_found", NO_BILLING)

    return [org_route(api, "/billing/portal", portal, ["POST"], owner=True)]
