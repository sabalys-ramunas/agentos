"""Code cards: the one-time codes a site asks for, as the app's Needs you reads them.

The node's own route (`openajar.api.routes_approvals`) lists the requests it holds. The app is shown only
the open ones, whatever the node returns, so a card that is closed is never offered for an answer.
Answering and declining pass through unchanged: a code is taken once, and this module never sees one
(`POST /api/orgs/{org}/codes/{id}` is the node's own route).
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request

from openajar.api.app import Api
from openajar.api.web.base import org_route, read


def routes(api: Api) -> list:
    async def codes(request: Request) -> list[dict[str, Any]]:
        return [item for item in await read(request, "/api/codes") if item.get("state") == "open"]

    return [org_route(api, "/codes", codes, ["GET"])]
