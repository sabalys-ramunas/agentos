"""Done today" has one rule, shared by Home's summary and History's day lines (api/web/history.py counts_as_done):
agents' and routines' work that went through. A person's own action, a failure, a part that went out and a
look at a site do not count as things done."""

from __future__ import annotations

from openajar.api.web.history import CHECKED, FAILED, NOT_CHECKED, PARTIAL, READ, counts_as_done

POST = "post"


def story(verdict: str, *, action: str = POST, kind: str = "agent") -> dict:
    return {"verdict": verdict, "action": action, "who": {"kind": kind, "id": "claude-code", "name": "Claude Code"}}


def test_an_agents_post_that_went_through_is_done() -> None:
    assert counts_as_done(story(CHECKED))
    assert counts_as_done(story(NOT_CHECKED))


def test_a_routine_counts_like_an_agent() -> None:
    assert counts_as_done(story(CHECKED, kind="flow"))


def test_a_persons_own_action_is_not_done_for_them() -> None:
    assert not counts_as_done(story(CHECKED, kind="human"))


def test_failures_and_parts_that_went_out_are_problems_not_done() -> None:
    assert not counts_as_done(story(FAILED))
    assert not counts_as_done(story(PARTIAL))


def test_a_look_at_a_site_is_a_check_not_a_thing_done() -> None:
    assert not counts_as_done(story(CHECKED, action=READ))
