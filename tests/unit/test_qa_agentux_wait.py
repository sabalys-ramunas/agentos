"""QA for browser_wait's bound on a page that stops answering.

A wait holds the lane until it returns, so its own deadline must hold even when the page never answers
the script that reads the ref's state. Bugs are strict xfails pinned to their reason; remove them once
fixed. The fake page is the wait suite's (test_interaction_wait.py), with one handle that never answers.
"""

from __future__ import annotations

import asyncio

from test_interaction_wait import FakeHandle, WaitPage

from openajar.node.interaction import PlaywrightInteraction, WaitFor


class SilentHandle(FakeHandle):
    """An element whose page never returns from a script (a busy loop, a hung renderer)."""

    async def evaluate(self, js: str, arg=None):
        await asyncio.Event().wait()


async def test_a_ref_wait_on_a_page_that_never_answers_returns_by_its_timeout():
    page = WaitPage()
    page.handles["e7"] = SilentHandle()
    spec = WaitFor(ref="e7", ref_state="enabled", timeout_s=0.2)
    result = await asyncio.wait_for(PlaywrightInteraction().wait(page, spec), 3)
    assert result.stopped == "timeout" and result.met is False
