"""Proof of done: readbacks of the target's own state.

A readback is resolved (templates filled, baselines looked up) into a ResolvedCheck that
is stored with the run, so a later re-check can run it again without the original params.
Each result is `passed: True` (checked, holds), `False` (checked, does not hold) or `None`
(could not be checked: the page showed a captcha, the read failed, the lane was busy).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from agentos.core.models import Signal, utcnow
from agentos.core.signals import STOP
from agentos.runner.driver import ElementState, FlowDriver
from agentos.runner.models import Condition, Locator
from agentos.runner.util import Clock, clip, norm_lines

BAD_SIGNALS = STOP


class ResolvedCheck(BaseModel):
    path: str
    locator: Locator | None = None
    condition: Condition
    url: str | None = None
    timeout_ms: int = 8000

    def describe(self) -> str:
        c = self.condition
        target = self.locator.describe() if self.locator else "the page"
        return f"{target}: {_expected(c)}"


class ReadbackResult(BaseModel):
    path: str
    check: ResolvedCheck
    passed: bool | None
    expected: str
    found: Any = None
    note: str | None = None
    checked_at: datetime = Field(default_factory=utcnow)


def _norm(value: Any) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _expected(c: Condition) -> str:
    k = c.kind
    if k == "exists":
        return "exactly one element is present"
    if k == "absent":
        return "no element is present"
    if k == "count":
        if c.changed_by is not None:
            return f"count changed by {c.changed_by:+d} from {c.baseline_count}"
        for op in ("equals", "at_least", "at_most"):
            v = getattr(c, op)
            if v is not None:
                return f"count {op.replace('_', ' ')} {v}"
    subject = f"attribute {c.name}" if k == "attribute" else k
    if c.changed_from is not None:
        was = repr(c.baseline_value) if c.baseline_value is not None else "nothing"
        part = f" (compared by {c.identity!r})" if c.identity else ""
        return f"{subject} differs from the one saved as {c.changed_from}{part} (it was {was})"
    if c.lines:
        subject += " (line breaks kept)"
    for op in ("equals", "not_equals", "contains", "matches"):
        v = getattr(c, op)
        if v is not None:
            return f"{subject} {op.replace('_', ' ')} {v!r}"
    return k


def _identity(value: str, pattern: str | None) -> str | None:
    """The part of a value that names the thing: group 1 of `pattern`, else the whole value
    whitespace normalized (no pattern). None when the pattern does not match."""
    if pattern is None:
        return _norm(value) or None
    m = re.search(pattern, value)
    return (m.group(1) or None) if m else None


def _changed(actual: str, c: Condition) -> bool:
    """A changed_from check: present, and not what the element saved before held. A missing
    baseline never passes, unless the condition allows an empty list and nothing was there."""
    now = _identity(actual, c.identity)
    if now is None:
        return False
    if c.baseline_value is None or not _norm(c.baseline_value):
        return c.empty_before_ok and c.baseline_value is None and c.baseline_count == 0
    before = _identity(c.baseline_value, c.identity) or _norm(c.baseline_value)
    return now != before


def _compare(actual: str | None, c: Condition, lines: bool = False) -> bool:
    if actual is None:
        return c.not_equals is not None
    if c.changed_from is not None:
        return _changed(actual, c)
    norm = norm_lines if lines else _norm
    a = norm(actual)
    if c.equals is not None:
        return a == norm(str(c.equals))
    if c.not_equals is not None:
        return a != norm(c.not_equals)
    if c.contains is not None:
        return norm(c.contains) in a
    if c.matches is not None:
        return re.search(c.matches, actual) is not None
    return False


def _found_desc(st: ElementState) -> str:
    if st.ambiguous:
        return f"{st.count} matches"
    return "1 match" if st.found else "no match"


def evaluate(c: Condition, st: ElementState) -> tuple[bool, str, Any]:
    """(passed, expected, found) for a condition against the target's own state."""
    expected = _expected(c)
    k = c.kind
    if k == "url":
        return _compare(st.url, c), expected, clip(st.url, max_str=300)
    if k == "exists":
        return (st.found and not st.ambiguous), expected, _found_desc(st)
    if k == "absent":
        return st.count == 0, expected, f"{st.count} matches"
    if k == "count":
        n = st.count
        if c.changed_by is not None:
            delta = n - (c.baseline_count or 0)
            ok = delta >= c.changed_by if c.changed_by >= 0 else delta <= c.changed_by
            return ok, expected, f"count {n} (changed by {delta:+d})"
        if c.equals is not None:
            return n == int(c.equals), expected, f"count {n}"
        if c.at_least is not None:
            return n >= int(c.at_least), expected, f"count {n}"
        return n <= int(c.at_most or 0), expected, f"count {n}"
    if st.ambiguous:
        return False, expected, (f"{st.count} matches; a readback must identify one element "
                                 "(anchor it with within, or set nth)")
    if not st.found:
        return False, expected, "no element at the locator"
    if k == "attribute":
        actual = st.attributes.get(c.name or "")
    elif k == "text":
        actual = st.lines if c.lines else st.text      # no lines read: nothing to compare (fails)
    else:
        actual = st.value
    return _compare(actual, c, lines=c.lines), expected, clip(actual, max_str=300)


async def run_check(driver: FlowDriver, check: ResolvedCheck, clock: Clock, *,
                    poll_s: float = 0.5) -> ReadbackResult:
    """Read the target until the condition holds or the timeout passes."""

    def result(passed: bool | None, expected: str, found: Any = None, note: str | None = None):
        return ReadbackResult(path=check.path, check=check, passed=passed, expected=expected,
                              found=found, note=note, checked_at=clock.now())

    expected = _expected(check.condition)
    if check.url:
        try:
            nav = await driver.navigate(check.url)
        except Exception as e:  # noqa: BLE001 - any failure here means "could not check"
            return result(None, expected, note=f"could not load {check.url}: {type(e).__name__}")
        if nav.signal in BAD_SIGNALS:
            return result(None, expected, note=f"the page shows {nav.signal}, so the state cannot be read")
    deadline = clock.monotonic() + check.timeout_ms / 1000
    while True:
        try:
            sig = await driver.signal()
        except Exception:  # noqa: BLE001
            sig = Signal.UNKNOWN
        if sig in BAD_SIGNALS:
            return result(None, expected, note=f"the page shows {sig}, so the state cannot be read")
        note = None
        try:
            if check.locator is None:
                state = ElementState(url=await driver.current_url(), found=True, count=1)
            else:
                state = await driver.read_state(check.locator)
        except Exception as e:  # noqa: BLE001
            state = None
            note = f"read failed: {type(e).__name__}: {clip(str(e), max_str=200)}"
        if state is not None:
            passed, expected, found = evaluate(check.condition, state)
            if passed or clock.monotonic() >= deadline:
                return result(passed, expected, found)
        elif clock.monotonic() >= deadline:
            return result(None, expected, note=note)
        await clock.sleep(poll_s)
