"""The agenda's limits and the small parsers it shares: when an entry is due, the principal, and the
title and notes an entry may carry.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from openajar.agenda.models import (
    MAX_NOTES,
    MAX_TITLE,
    EntryState,
    When,
)
from openajar.core import clock
from openajar.core.errors import (
    Conflict,
    InvalidRequest,
)
from openajar.core.models import Principal

SERVICE_NAME = "agenda"
OWNER_GRANT = "owner"
TOOL = "agenda_propose"          # what the approval records as the asking tool
AT_GRACE = timedelta(minutes=30)  # an exact time still runs this long after, for a busy or restarted node
MAX_AHEAD = timedelta(days=366)
COMMIT_EFFECTS = frozenset({"commit", "pay", "irreversible"})  # the effects the lane's pace applies to
CONNECTOR_RETRY = timedelta(minutes=15)  # a broken connector or its own pace: look again this often
RUN_FINISHED = {"done": EntryState.DONE, "failed": EntryState.FAILED, "cancelled": EntryState.CANCELLED}
PERSON_VIAS = frozenset({"person", "standing", "approved", "owner"})  # a run that goes as approved
# services that plan timed work on lanes on their own; the timeline shows what they plan, read only
MANAGED = {"keepalive": "keepalive"}
APPROVAL_MAX_DAYS = 14   # node.toml agenda_approval_max_days: the longest a request for an entry waits
DEFAULT_REQUEST_S = 86400  # an ordinary request's time, when the approvals service does not say


class AgendaStateError(Conflict):
    """The entry is in a state that does not allow this (approve a cancelled entry, edit a running one)."""
    code = "agenda_state"


def _utc(value: Any, name: str) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        when = value
    else:
        try:
            when = datetime.fromisoformat(str(value).strip())
        except ValueError:
            raise InvalidRequest(f"{name} is an ISO 8601 time like 2026-11-08T09:00:00+02:00") from None
    if when.tzinfo is None:
        raise InvalidRequest(f"{name} needs a timezone, like 2026-11-08T09:00:00+02:00")
    return when.astimezone(UTC)


def parse_when(data: Any, now: datetime) -> When:
    """`{"at": t}`, `{"not_before": t, "not_after": t}` (either may be left out), or `{}` / None
    for as soon as approved (`{"not_after": t}` gives that a deadline). Times need a timezone."""
    if data is None:
        data = {}
    if isinstance(data, str):
        data = {"at": data}
    if not isinstance(data, dict):
        raise InvalidRequest("when is an object: {at}, {not_before, not_after}, or {} for as soon as approved")
    unknown = set(data) - {"at", "not_before", "not_after", "kind"}
    if unknown:
        raise InvalidRequest(f"when takes at, not_before and not_after, not {', '.join(sorted(unknown))}")
    at = _utc(data.get("at"), "at")
    nb, na = _utc(data.get("not_before"), "not_before"), _utc(data.get("not_after"), "not_after")
    if at is not None:
        if nb is not None:
            raise InvalidRequest("give either at (an exact time) or not_before (a window), not both")
        w = When(kind="at", not_before=at, not_after=na or at + AT_GRACE)
    elif nb is not None:
        w = When(kind="window", not_before=nb, not_after=na)
    else:
        w = When(kind="asap", not_before=None, not_after=na)
    if w.not_before and w.not_after and w.not_after <= w.not_before:
        raise InvalidRequest("not_after must be later than the start")
    if w.not_before and w.not_before > now + MAX_AHEAD:
        raise InvalidRequest("an agenda entry is planned at most a year ahead")
    return w


def principal_dict(p: Principal | dict | None) -> dict[str, Any]:
    if isinstance(p, Principal):
        return p.model_dump()
    return dict(p or {})


def counts(items: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        out[it.get("state") or "?"] = out.get(it.get("state") or "?", 0) + 1
    return out


def grant_of(ctx: Any) -> str | None:
    gid = getattr(getattr(ctx, "grant", None), "id", None)
    return gid or getattr(getattr(ctx, "principal", None), "grant_id", None)


def iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


def ts_of(t: Any) -> str | None:
    return clock.iso(t) if isinstance(t, int | float) else None


def clean_title(title: Any) -> str:
    t = " ".join(str(title or "").split())
    if not t:
        raise InvalidRequest("an agenda entry needs a title")
    if len(t) > MAX_TITLE:
        raise InvalidRequest(f"a title is at most {MAX_TITLE} characters")
    return t


def clean_notes(notes: Any) -> str | None:
    if notes is None:
        return None
    n = str(notes).strip()
    if len(n) > MAX_NOTES:
        raise InvalidRequest(f"notes are at most {MAX_NOTES} characters")
    return n or None
