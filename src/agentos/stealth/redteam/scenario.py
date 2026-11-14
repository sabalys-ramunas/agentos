"""The agent's visit to the detector site, driven the way an agent drives a lane: through the
production `PlaywrightInteraction` (browser_navigate, browser_snapshot, browser_act), so every
event the sensor sees comes from the same code path an agent's tool calls take.

The visit: arrive by URL, follow a link to the sign-up form, fill it (mixed case, Lithuanian
letters, an emoji, a select, a checkbox), submit, scroll the feed to a post far down and like it,
jump to the next page by URL (as an agent does), and follow a link home.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from typing import Any

NAME = "Jonas Petraitis"
EMAIL = "jonas.petraitis@example.com"
ABOUT = "Labas! Mėgstu ilgus žygius prie upės, kavą ir gerą muziką. Šeštadienį būsiu prie tilto 😀"
COUNTRY = "Lithuania"
LIKE_POST = 25

_LINE = re.compile(r'^\s*[-~]\s+(?P<role>[\w-]+)(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?(?P<rest>.*)$')
_REF = re.compile(r"\[ref=([a-z0-9]+)\]")


class StepFailed(Exception):
    pass


def find_ref(snapshot: str, role: str, name: str | re.Pattern[str]) -> str:
    """The ref of the first element with this role whose accessible name is `name` (or matches it)."""
    for line in snapshot.splitlines():
        m = _LINE.match(line)
        if not m or m["role"] != role:
            continue
        label = (m["name"] or "").replace('\\"', '"')
        hit = name.search(label) if isinstance(name, re.Pattern) else label == name
        ref = _REF.search(m["rest"])
        if hit and ref:
            return ref.group(1)
    raise StepFailed(f"no {role} named {name if isinstance(name, str) else name.pattern!r} on the page")


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""


async def visit(ix: Any, page: Any, base: str, run: str) -> list[Step]:
    """One visit. Returns what each step did; a failed step ends the visit (the sensor keeps what it
    saw until then)."""
    steps: list[Step] = []

    async def act(label: str, role: str, name: str | re.Pattern[str], kind: str, text: str | None = None) -> None:
        snap = await ix.snapshot(page)
        ref = find_ref(snap.text, role, name)
        result = await ix.act(page, ref, kind, text)
        # an action that did not take (a select left unchanged) is a result; the visit goes on
        steps.append(Step(label, bool(result.ok), result.detail))

    try:
        await ix.navigate(page, f"{base}/?run={run}")
        steps.append(Step("arrive by URL", True))
        await page.wait_for_timeout(2500)  # the landing page's active tests run for a moment
        await act("open the sign-up form", "link", "Create an account", "click")
        await act("name", "textbox", "Full name", "type", NAME)
        await act("email", "textbox", "Email", "type", EMAIL)
        await act("about", "textbox", "About you", "type", ABOUT)
        await act("country", "combobox", "Country", "select", COUNTRY)
        await act("rules", "checkbox", "I agree to the club rules", "click")
        await act("join", "button", "Join", "click")
        await act("scroll to a post", "button", f"Like post {LIKE_POST}", "scroll")
        await act("like it", "button", f"Like post {LIKE_POST}", "click")
        await ix.navigate(page, f"{base}/feed?page=2", follow_link=True)  # as an agent allowed to act
        steps.append(Step("next page by URL", True))
        await act("home", "link", "Home", "click")
    except StepFailed as e:
        steps.append(Step("failed", False, str(e)))
    except Exception as e:  # noqa: BLE001 - a broken step is a result, not a crash of the bench
        steps.append(Step("failed", False, f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}"))
    await flush(page)
    return steps


async def flush(page: Any) -> None:
    """Let the sensor send what it holds: a page's own timer posts every 1.5 s."""
    with contextlib.suppress(Exception):  # a closed page has nothing left to send
        await page.wait_for_timeout(2000)
