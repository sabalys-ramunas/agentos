"""A lane's login hint when its manager does not say (finding 26 of the 2026-11-10 full test): the readiness a
person set wins, else the lane check's own session check, else unknown."""

from __future__ import annotations

from types import SimpleNamespace

from openajar.api.routes_core import signed_in_hint


class _Runtime:
    def __init__(self, **services):
        self.services = services

    def get(self, name):
        return self.services.get(name)


def _api(row=None, session_level=None):
    store = SimpleNamespace(lane_row=lambda lane_id: row)
    checks = [SimpleNamespace(id="session", level=session_level)] if session_level else []
    checker = SimpleNamespace(last={"biz/ig": SimpleNamespace(checks=checks)}) if session_level else None
    runtime = _Runtime(workspaces=SimpleNamespace(store=store), lane_check=checker)
    return SimpleNamespace(runtime=runtime, optional=runtime.get)


def test_the_lane_check_says_signed_in_when_nobody_set_readiness() -> None:
    never_set = {"readiness": "needs_login", "updated_by": None}
    assert signed_in_hint(_api(never_set, "ok"), "biz/ig") == "ok"
    assert signed_in_hint(_api(None, "fail"), "biz/ig") == "needs_login"


def test_a_readiness_a_person_set_wins_over_the_check() -> None:
    assert signed_in_hint(_api({"readiness": "needs_login", "updated_by": "owner"}, "ok"), "biz/ig") == "needs_login"
    assert signed_in_hint(_api({"readiness": "ready", "updated_by": "owner"}, "fail"), "biz/ig") == "ok"


def test_nothing_set_and_nothing_checked_stays_unknown() -> None:
    assert signed_in_hint(_api(None, None), "biz/ig") == "unknown"
    assert signed_in_hint(_api({"readiness": "needs_login", "updated_by": None}, "unknown"), "biz/ig") == "unknown"


# ---- the act's read-back on the record, so History can count a checked act (finding 23) ----

def test_an_acts_read_back_is_traced_as_a_flag_only() -> None:
    from openajar.core.models import ActionKind, ActionResult
    from openajar.node.tools import trace_read_back

    rows: list[tuple[str, dict]] = []
    ctx = SimpleNamespace(trace=lambda kind, **fields: rows.append((kind, fields)))
    trace_read_back(ctx, "biz/ig", ActionResult(ok=True, action=ActionKind.CLICK, url="https://x", changed=True,
                                                detail="aria-pressed false -> true"))
    trace_read_back(ctx, "biz/ig", ActionResult(ok=True, action=ActionKind.CLICK, url="https://x"))  # nothing read back
    assert rows == [("act_read_back", {"lane": "biz/ig", "status": "ok", "changed": True, "action": "click"})]
