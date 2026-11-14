"""The error envelope: `{ "error": { "code": "...", "message": "...", "fix": "..." } }` with a fitting
status. `fix` is the error catalog's suggestion for a person (agentos.core.catalog, docs/ERRORS.md). The
shape and the status map are agentos.core.http_errors, shared with agentos.mcp.http and the controller.

Codes come from `agentos.core.errors`; the few the HTTP layer adds on its own
(unauthorized, csrf_failed, step_up_required, rate_limited, lane_cold, conflict, internal, misdirected) are listed in
API_CODES. Messages are written for a person and never carry a secret or a raw
exception string from a service (those can include anything).
"""

from __future__ import annotations

import logging
import traceback
from pathlib import Path

from pydantic import ValidationError
from starlette.responses import JSONResponse

from agentos.core.errors import AgentosError, ApprovalPending
from agentos.core.http_errors import STATUS_BY_CLASS, STATUS_BY_CODE, error_body, status_for  # noqa: F401  (re-exported)

log = logging.getLogger("agentos.api")

API_CODES = ("unauthorized", "csrf_failed", "step_up_required", "presence_required", "rate_limited", "lane_cold",
             "conflict", "internal", "misdirected", "method_not_allowed")

class ApiError(AgentosError):
    """An error raised by the HTTP layer itself, with an explicit code: one of its own (`codes`,
    for the error catalog) or a shared one (denied, unavailable, conflict)."""

    codes = tuple(c for c in API_CODES if c != "conflict")

    def __init__(self, code: str, message: str, *, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.code = code
        self.headers = headers or {}


def error_extra(exc: AgentosError) -> dict[str, object]:
    """What an error's answer carries beyond its code: the approval that waits, or the level a
    refused approve needs, so the panel can step up."""
    if isinstance(exc, ApprovalPending):
        return {"approval_id": exc.approval_id}
    requires = getattr(exc, "requires", None)
    return {"requires": requires, "approval_id": getattr(exc, "approval_id", None)} if requires else {}


def error_response(exc: BaseException, *, member: bool = False) -> JSONResponse:
    """The answer for a failed request. `member`: the caller is a member, so the envelope carries only the
    fix a member may read (catalog.member_fix): no command, token or grant."""
    if isinstance(exc, ValidationError):
        return JSONResponse(error_body("invalid", validation_message(exc), member=member), status_code=400)
    if isinstance(exc, AgentosError):
        code = exc.code
        status = status_for(exc)
        extra = error_extra(exc)
        headers = getattr(exc, "headers", None) or None
        return JSONResponse(error_body(code, exc.message, member=member, **extra), status_code=status, headers=headers)
    # Unknown failures: log the type and where it was raised, never the text (it may quote what the caller
    # sent, which no scrubber knows), and answer generically. No exc_info: a traceback ends with the text,
    # and its source lines can hold literals.
    log.error("unhandled %s in API handler, raised at %s", type(exc).__name__, _frames(exc))
    return JSONResponse(error_body("internal", "Something went wrong on the node."), status_code=500)


def _frames(exc: BaseException, limit: int = 8) -> str:
    """The innermost frames of where `exc` was raised: file, line and function only."""
    frames = traceback.extract_tb(exc.__traceback__)[-limit:]
    return " < ".join(f"{Path(f.filename).name}:{f.lineno} in {f.name}" for f in reversed(frames)) or "an unknown place"


def validation_message(exc: ValidationError) -> str:
    """Field paths and reasons only. Pydantic's default text echoes input values, which can be secrets."""
    parts = []
    for err in exc.errors(include_input=False, include_url=False, include_context=False):
        loc = ".".join(str(p) for p in err.get("loc", ())) or "body"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "Invalid request. " + "; ".join(parts[:5])
