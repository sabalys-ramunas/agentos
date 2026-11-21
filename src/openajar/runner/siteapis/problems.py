"""What /v1/sites answers when it does not answer: application/problem+json (docs/SITE-APIS.md, Answers).

Every error is `{type, title, status, code, detail}`, plus `retry_at` or `run` when they apply. The
detail is one of the fixed sentences below, or one built only from names the caller already sent:
never an exception's text, because that can carry a lane, a param's value or text from a page.

`from_exception` maps by exception type and `from_run` by a run's stored error code, the code the
executor itself wrote. Neither reads a message.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from starlette.responses import Response

from openajar.core.errors import (
    AgentPaused,
    Frozen,
    HumanInControl,
    InvalidRequest,
    LaneBusy,
    LaneNotFound,
    LaneQuarantined,
    NotFound,
    PolicyDenied,
    ServiceUnavailable,
)
from openajar.core.signals import CONNECTOR_HOLD_CODES, RUN_SIGNAL_CODE_SET
from openajar.policy.errors import Denied
from openajar.policy.meters import LimitReached
from openajar.runner.connectors.health import BROKEN_CODES
from openajar.runner.connectors.surfaces import ConnectorBroken, PaceLimited
from openajar.runner.records import Run
from openajar.runner.siteapis.service import ApiPaused

CONTENT_TYPE = "application/problem+json"
TYPE_BASE = "urn:agentos:site-api:"
LANE_BUSY_RETRY_S = 30
DEFAULT_RETRY_S = 60

PROBLEMS: dict[str, tuple[int, str, str]] = {
    "invalid_params": (400, "Invalid parameters", "The parameters do not fit this endpoint's input schema."),
    "unauthorized": (401, "Unauthorized", "Send a live key as Authorization: Bearer aos_..."),
    "denied": (403, "Denied", "This key's scope does not cover that."),
    "ceiling": (403, "Above the key's ceiling", "This endpoint's effect is above this key's ceiling."),
    "not_found": (404, "Not found", "There is no such API or endpoint for this key."),
    "method_not_allowed": (405, "Method not allowed", "This route does not take that method."),
    "paused": (409, "Paused", "A person paused this API."),
    "too_large": (413, "Body too large", "The request body is over the limit."),
    "empty_result": (422, "Empty result", "The read found fewer rows than this endpoint expects, so the site "
                     + "probably changed. Nothing was cached."),
    "rate_limited_key": (429, "Key limit reached", "This key is over its rate limit."),
    "pace_limited": (429, "Pace limit reached", "The account's pace allows no more runs of this endpoint for now."),
    "internal": (500, "Internal error", "The node could not answer this call."),
    "run_failed": (500, "Run failed", "The run ended without a result. A person can see why in Activity."),
    "output_contract_violation": (502, "Output contract violation", "The site changed shape, so the value was not "
                                  + "passed on."),
    "needs_human": (503, "Needs a person", "The lane needs a person before anything runs on it. Do not retry."),
    "rate_limited": (503, "Rate limited by the site", "The site is limiting this account, so the lane is resting."),
    "connector_broken": (503, "Endpoint broken", "This endpoint is broken on this surface until a repaired version "
                         + "is installed."),
    "frozen": (503, "Frozen", "A person froze this scope. Do not retry."),
    "lane_busy": (503, "Lane busy", "The lane is in use by a person."),
    "node_offline": (503, "Node offline", "The node cannot run this right now."),
}
# what a stored grant's end looks like to a call: the key is gone, so it answers as a bad key
GONE = frozenset({"unauthenticated", "invalid_token", "revoked", "expired", "not_yet_valid", "unregistered",
                  "unknown_grant", "retired_key"})
RUN_GONE = frozenset({"grant_revoked", "grant_expired", "grant_unknown", "grant_not_yet_valid"})
SIGNALS = RUN_SIGNAL_CODE_SET


class Problem(Exception):
    def __init__(self, code: str, detail: str | None = None, *, retry_at: datetime | str | None = None,
                 retry_after: int | None = None, run: str | None = None, headers: dict[str, str] | None = None):
        self.status, self.title, default = PROBLEMS[code]
        self.code, self.detail = code, detail or default
        self.retry_at = retry_at.isoformat() if isinstance(retry_at, datetime) else retry_at
        self.retry_after = retry_after
        self.run = run
        self.headers = dict(headers or {})
        super().__init__(code)

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": TYPE_BASE + self.code, "title": self.title, "status": self.status,
                               "code": self.code, "detail": self.detail}
        return out | ({"retry_at": self.retry_at} if self.retry_at else {}) | ({"run": self.run} if self.run else {})

    def response(self, headers: dict[str, str] | None = None) -> Response:
        out = {"Cache-Control": "no-store", **self.headers, **(headers or {})}
        if self.status == 401:
            out["WWW-Authenticate"] = 'Bearer realm="site-apis"'
        if self.retry_after is not None:
            out["Retry-After"] = str(self.retry_after)
        elif self.status == 429 and "Retry-After" not in out:
            out["Retry-After"] = str(DEFAULT_RETRY_S)
        if self.run:
            out["Agentos-Run"] = self.run
        return Response(json.dumps(self.body()), status_code=self.status, media_type=CONTENT_TYPE, headers=out)


def seconds_until(when: datetime | None, now: datetime) -> int:
    return max(1, int((when - now).total_seconds()) + 1) if when is not None else DEFAULT_RETRY_S


def from_exception(e: BaseException, now: datetime) -> Problem:
    if isinstance(e, Problem):
        return e
    if isinstance(e, PaceLimited):
        return Problem("pace_limited", retry_at=e.retry_at, retry_after=seconds_until(e.retry_at, now))
    if isinstance(e, ApiPaused):  # a ConnectorBroken to the runner, so its runs wait: first here
        return Problem("paused")
    if isinstance(e, ConnectorBroken):
        return Problem("connector_broken")
    if isinstance(e, Frozen):
        return Problem("frozen")
    if isinstance(e, LimitReached):
        ends = e.hit.get("resets_at")  # a day's meter: the same answer as the account's pace
        return Problem("pace_limited", retry_at=ends if isinstance(ends, str) else None,
                       retry_after=_retry_from(ends, now))
    if isinstance(e, Denied):
        return Problem("ceiling" if e.reason == "ceiling" else "unauthorized" if e.reason in GONE else "denied")
    if isinstance(e, PolicyDenied):
        return Problem("denied")
    if isinstance(e, HumanInControl | LaneBusy):
        return Problem("lane_busy", retry_after=LANE_BUSY_RETRY_S)
    if isinstance(e, AgentPaused | LaneQuarantined):
        return Problem("needs_human")
    if isinstance(e, InvalidRequest):
        return Problem("invalid_params")
    if isinstance(e, NotFound | LaneNotFound):
        return Problem("not_found")
    if isinstance(e, ServiceUnavailable):
        return Problem("node_offline")
    return Problem("internal")


def _retry_from(ends: Any, now: datetime) -> int:
    try:
        return seconds_until(datetime.fromisoformat(str(ends)), now)
    except ValueError:
        return DEFAULT_RETRY_S


def from_run(run: Run, *, frozen: bool = False, rest_until: datetime | None = None, now: datetime) -> Problem:
    """The answer for a run that ended without a result. A run the freeze cancelled ends as `cancelled`,
    which only `frozen` tells apart from a person's cancel."""
    code = (run.error or {}).get("code")
    if frozen:
        return Problem("frozen", run=run.id)
    if code in SIGNALS or code in CONNECTOR_HOLD_CODES:
        return Problem("needs_human", run=run.id)
    if code == "rate_limited":
        return Problem("rate_limited", retry_at=rest_until, retry_after=seconds_until(rest_until, now), run=run.id)
    if code == "output_contract_violation":
        return Problem("output_contract_violation", run=run.id)
    if code in BROKEN_CODES:
        return Problem("connector_broken", run=run.id)
    if code in RUN_GONE:
        return Problem("unauthorized", run=run.id)
    if code == "denied":
        return Problem("denied", run=run.id)
    if code == "lane_unavailable":
        return Problem("lane_busy", retry_after=LANE_BUSY_RETRY_S, run=run.id)
    if code == "unavailable":
        return Problem("node_offline", run=run.id)
    return Problem("run_failed", run=run.id)
