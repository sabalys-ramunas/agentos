"""Fail closed when the action log cannot be written.

`TraceStore.write` never raises: a locked or full database keeps rows in memory, then in the
spill file, and writes them later. That is right for a blip. It is wrong for a node that has
recorded nothing for minutes while agents keep posting. So:

- the store keeps two clocks: since when the database refuses rows (`db_failing_since`), and
  since when rows sit only in memory because the spill file failed too (`unwritten_since`);
- a call above READ is refused with `audit_unwritable` once rows have been unwritten for
  longer than `PolicyConfig.audit_fail_closed_s` (120 s by default, so the 60 s health round
  warns first). READ calls keep working, so an agent can still look and the owner can see why;
- the health monitor warns as soon as either clock starts (`finding`), well before the rule
  bites, and the rule lifts by itself with the first row that is written.

A transient lock never trips it: the database is retried on every write and every policy tick
(5 s), and a spill file that works counts as written.
"""

from __future__ import annotations

from typing import Any

from openajar.core.errors import AuditUnwritable
from openajar.core.models import Effect

DEFAULT_FAIL_CLOSED_S = 120  # two health rounds: the warning always comes first
WARN_AFTER_S = 5.0  # a lock shorter than this is a blip, not worth a warning


def state(traces: Any, now: float, threshold_s: float = DEFAULT_FAIL_CLOSED_S) -> dict:
    """The write path in plain terms: level ok, warn or critical, and how long it has failed."""
    db_since = getattr(traces, "db_failing_since", None)
    lost_since = getattr(traces, "unwritten_since", None)
    out = {"level": "ok", "db_failing_s": None, "unwritten_s": None, "threshold_s": threshold_s,
           "pending": int(getattr(traces, "pending_count", 0) or 0),
           "error": getattr(traces, "last_write_error", None)}
    if db_since is not None:
        out["db_failing_s"] = max(0.0, now - db_since)
    if lost_since is not None:
        out["unwritten_s"] = max(0.0, now - lost_since)
    if out["unwritten_s"] is not None and out["unwritten_s"] >= threshold_s:
        out["level"] = "critical"
    elif (out["unwritten_s"] or 0) >= WARN_AFTER_S or (out["db_failing_s"] or 0) >= WARN_AFTER_S:
        out["level"] = "warn"
    return out


def check(traces: Any, effect: Effect, now: float, threshold_s: float = DEFAULT_FAIL_CLOSED_S) -> None:
    """Refuse `effect` when it is anything but READ (the specials included: an exec or a login
    changes things too) and the log has recorded nothing for too long."""
    if effect == Effect.READ:
        return
    lost_since = getattr(traces, "unwritten_since", None)
    if lost_since is None or now - lost_since < threshold_s:
        return
    raise AuditUnwritable(f"the action log has not been able to record anything for {now - lost_since:.0f} s, "
                          "so nothing that changes anything runs until it can; reading still works")


def finding(traces: Any, now: float, threshold_s: float = DEFAULT_FAIL_CLOSED_S) -> tuple[str, str, str] | None:
    """(level, title, detail) for the health monitor, or None while all is well."""
    s = state(traces, now, threshold_s)
    if s["level"] == "ok":
        return None
    why = f" ({s['error']})" if s.get("error") else ""
    if s["level"] == "critical":
        return ("critical", "The action log cannot be written",
                (f"nothing has been recorded for {s['unwritten_s']:.0f} s{why}, so the node refuses every call that "
                "changes something; reads still work. Free disk space under the node's home or end whatever "
                "holds traces.db."))
    if s["unwritten_s"] is not None:
        left = max(0.0, threshold_s - s["unwritten_s"])
        return ("warn", "The action log is failing",
                (f"{s['pending']} actions wait in memory{why}: neither the log nor its spill file takes them. "
                f"In {left:.0f} s the node starts refusing calls that change something. Free disk space under "
                "the node's home."))
    return ("warn", "The action log is writing to its spill file",
            (f"the log database has refused rows for {s['db_failing_s']:.0f} s{why}; they are safe in the spill "
            "file and go in once the database takes them. Check what holds traces.db."))
