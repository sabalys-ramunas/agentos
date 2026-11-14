"""QA for the agent tools' outline memory (agentos.node.tools.render_snapshot over snapshot.OUTLINES).

A delta is "what changed since your last snapshot of this tab" (docs/TOOLS.md), so it has to be
measured against the snapshot this same agent session was shown. Bugs are strict xfails that
fail today for the reason in their reason string; they must be removed once fixed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentos.core.models import Principal
from agentos.node.tools import render_snapshot

URL = "https://site.example/feed"
# A page long enough that a one-line change is a shorter delta than the full outline (short pages
# fall back to the full outline, which would hide the delta paths under test).
BODY = [f"  - paragraph: Post number {i} in the feed, unchanged" for i in range(40)]
FIRST = "\n".join(["- main:", '  - button "Like 12" [ref=e12]', *BODY, '  - button "Share" [ref=e13]'])
SECOND = "\n".join(["- main:", '  - button "Like 13" [ref=e12]', *BODY, '  - button "Share" [ref=e13]'])


class Tab:
    """A page object. OUTLINES keys its memory weakly by page, so every test gets its own tab."""


def snap(text: str, *, frozen: bool = False) -> SimpleNamespace:
    return SimpleNamespace(url=URL, title="Feed", text=text, notices=[], frozen=frozen,
                           ref_count=text.count("[ref="), truncated=False, signal=None, signal_detail=None,
                           account=None)


def caller(thread: str, grant: str, subject: str = "owner-agent") -> SimpleNamespace:
    """A tool context as the node builds it for a controller-routed call (the principal comes from
    the verified token: subject, session thread and grant)."""
    return SimpleNamespace(principal=Principal(id=subject, thread=thread, grant_id=grant))


def test_an_agent_gets_a_delta_against_its_own_last_snapshot_of_the_tab():
    tab, me = Tab(), caller("t1", "g1")
    render_snapshot(me, "personal/reddit", tab, snap(FIRST), "full")
    out = render_snapshot(me, "personal/reddit", tab, snap(SECOND), "delta")
    assert 'Like 13' in out and '(was: button "Like 12"' in out
    assert "no earlier snapshot" not in out and "snapshot: full" not in out


def test_an_agent_with_no_earlier_snapshot_of_the_tab_gets_the_full_outline():
    out = render_snapshot(caller("t1", "g1"), "personal/reddit", Tab(), snap(SECOND), "delta")
    assert "no earlier snapshot of this tab" in out and "Like 13" in out


def test_a_frozen_tab_is_shown_in_full_and_its_outline_is_not_remembered():
    tab, me = Tab(), caller("t1", "g1")
    render_snapshot(me, "personal/reddit", tab, snap(FIRST), "full")
    frozen = render_snapshot(me, "personal/reddit", tab, snap(SECOND, frozen=True), "delta")
    assert "a dialog froze the page" in frozen
    later = render_snapshot(me, "personal/reddit", tab, snap(SECOND), "delta")
    assert '(was: button "Like 12"' in later  # still measured against the last real snapshot


@pytest.mark.parametrize("thread,grant", [("t2", "g1"), ("t1", "g2")], ids=["another-thread", "another-grant"])
def test_another_session_of_the_same_subject_gets_the_full_outline_not_a_delta(thread, grant):
    tab = Tab()
    render_snapshot(caller("t1", "g1"), "personal/reddit", tab, snap(FIRST), "full")
    out = render_snapshot(caller(thread, grant), "personal/reddit", tab, snap(SECOND), "delta")
    assert "no earlier snapshot of this tab" in out
