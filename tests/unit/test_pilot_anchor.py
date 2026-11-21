"""done_verified needs the item acted on to be the one the request named, not just a toggle that flipped."""

from __future__ import annotations

from typing import Any

import pytest
from testkit.pilot import FakeTools, ScriptedDecider, step

from openajar.pilot import Pilot, Request, Status, Value
from openajar.pilot.anchor import Target, anchors_of, check, line_sha256
from openajar.pilot.page import parse_page

TWO_POSTS = """signal: ok
http://127.0.0.1:8000/feed · Feed · 4 refs
- main:
  - article "Sunset over the bay":
    - link "@mira" [ref=e10] [cursor=pointer]:
      - /url: /u/mira
    - paragraph: Sunset over the bay tonight
    - link "3h" [ref=e11] [cursor=pointer]:
      - /url: /p/101
    - button "Like 3" [ref=e1] [cursor=pointer]
  - article "Sunset over the hills":
    - link "@miro" [ref=e12] [cursor=pointer]:
      - /url: /u/miro
    - paragraph: Sunset over the hills tonight
    - link "2h" [ref=e13] [cursor=pointer]:
      - /url: /p/102
    - button "Like 1" [ref=e2] [cursor=pointer]
"""
LIKED_FIRST = TWO_POSTS.replace('button "Like 3" [ref=e1]', 'button "Like 4" [pressed] [ref=e1]')
LIKED_SECOND = TWO_POSTS.replace('button "Like 1" [ref=e2]', 'button "Like 2" [pressed] [ref=e2]')
NO_POSTS = """signal: ok
http://127.0.0.1:8000/feed · Feed · 2 refs
- main:
  - button "Like 3" [ref=e1] [cursor=pointer]
  - button "Like 1" [ref=e2] [cursor=pointer]
"""
OWN_PAGE = """signal: ok
http://127.0.0.1:8000/p/101 · Post · 2 refs
- main:
  - article "Sunset over the bay":
    - paragraph: Sunset over the bay tonight
    - button "Like 3" [ref=e1] [cursor=pointer]
  - article "More from others":
    - paragraph: Unrelated
    - button "Like 9" [ref=e2] [cursor=pointer]
"""
CHANGED = {"browser_act": {"ok": True, "changed": True, "detail": "pressed"}}


async def nap(_: float) -> None:
    return None


async def like(ref: str, after: str, goal: str, page: str = TWO_POSTS, **kw: Any):
    tools = FakeTools(page, after, results=CHANGED)
    decider = ScriptedDecider(step("CLICK", click_target=ref), step("DONE", done=0.95))
    req = Request(lane="w/l", goal=goal, sites=("127.0.0.1",), seed="t", **kw)
    return await Pilot(tools, decider, sleep=nap).run(req)


async def test_a_like_on_the_neighbouring_post_is_not_verified() -> None:
    result = await like("e2", LIKED_SECOND, "Like the post by @mira")
    assert result.status is Status.DONE_UNVERIFIED and result.reason == "target_not_in_goal"
    assert result.readbacks[-1]["confirmed"] is True and result.readbacks[-1]["anchored"] is False
    assert "mira" in (result.message or "")  # it says what the goal named


async def test_the_named_post_is_verified() -> None:
    result = await like("e1", LIKED_FIRST, "Like the post by @mira")
    assert result.status is Status.DONE_VERIFIED and result.readbacks[-1]["anchored"] is True


@pytest.mark.parametrize("target", [Target(author="@mira"), Target(permalink="http://127.0.0.1:8000/p/101"),
                                    Target(text="Sunset over the bay tonight"), Target(text_sha256=line_sha256("Sunset over the bay tonight"))])
async def test_a_caller_target_anchors_each_way(target: Target) -> None:
    right = await like("e1", LIKED_FIRST, "Like this post", target=target)
    wrong = await like("e2", LIKED_SECOND, "Like this post", target=target)
    assert right.status is Status.DONE_VERIFIED
    assert wrong.status is Status.DONE_UNVERIFIED and wrong.reason == "target_not_in_goal"


async def test_every_field_of_a_target_must_match() -> None:
    result = await like("e1", LIKED_FIRST, "Like it", target=Target(author="mira", text="over the hills"))
    assert result.status is Status.DONE_UNVERIFIED and result.reason == "target_not_in_goal"


async def test_a_goal_that_names_nothing_apart_is_not_verified() -> None:
    result = await like("e1", LIKED_FIRST, "Like the sunset post")  # both posts are sunsets
    assert result.status is Status.DONE_UNVERIFIED and result.reason == "target_not_in_goal"


async def test_a_goal_word_found_only_in_the_acted_post_anchors_it() -> None:
    assert (await like("e1", LIKED_FIRST, "Like the sunset over the bay")).status is Status.DONE_VERIFIED
    assert (await like("e2", LIKED_SECOND, "Like the sunset over the bay")).status is Status.DONE_UNVERIFIED


async def test_two_alike_controls_in_no_post_are_not_verified() -> None:
    after = NO_POSTS.replace('button "Like 3" [ref=e1]', 'button "Like 4" [pressed] [ref=e1]')
    result = await like("e1", after, "Like the post by @mira", page=NO_POSTS)
    assert result.status is Status.DONE_UNVERIFIED and result.reason == "target_not_in_goal"


async def test_on_the_posts_own_page_its_first_post_is_it() -> None:
    link = "http://127.0.0.1:8000/p/101"
    liked = OWN_PAGE.replace('"Like 3" [ref=e1]', '"Like 4" [pressed] [ref=e1]')
    right = await like("e1", liked, f"Like the bay post {link}", page=OWN_PAGE)
    wrong = await like("e2", OWN_PAGE.replace('"Like 9" [ref=e2]', '"Like 10" [pressed] [ref=e2]'),
                       f"Like the bay post {link}", page=OWN_PAGE)
    bare = await like("e1", liked, f"Like {link}", page=OWN_PAGE)   # a feed's address looks the same
    assert right.status is Status.DONE_VERIFIED and wrong.status is Status.DONE_UNVERIFIED
    assert bare.status is Status.DONE_UNVERIFIED


MENTIONS = """signal: ok
http://127.0.0.1:8000/feed · Feed · 2 refs
- main:
  - article "Basil is up":
    - paragraph: Thanks @mira for the seeds
    - button "Like 1" [ref=e1] [cursor=pointer]
  - article "Tomatoes are in":
    - link "@mira" [ref=e10] [cursor=pointer]:
      - /url: /u/mira
    - button "Like 2" [ref=e2] [cursor=pointer]
"""


async def test_a_post_that_only_mentions_the_author_is_not_theirs() -> None:
    wrong = await like("e1", MENTIONS.replace('"Like 1" [ref=e1]', '"Like 2" [pressed] [ref=e1]'),
                       "Like @mira's tomato post", page=MENTIONS)
    right = await like("e2", MENTIONS.replace('"Like 2" [ref=e2]', '"Like 3" [pressed] [ref=e2]'),
                       "Like @mira's tomatoes post", page=MENTIONS)
    assert wrong.status is Status.DONE_UNVERIFIED and wrong.reason == "target_not_in_goal"
    assert right.status is Status.DONE_VERIFIED


def test_a_permalink_names_its_query_and_a_target_keeps_the_goals_anchors() -> None:
    page = parse_page(TWO_POSTS.replace("/p/101", "/item?id=222"))
    first = page.by_ref("e1")
    assert check("Like http://127.0.0.1:8000/item?id=111", None, page, first)["anchored"] is False
    assert check("Like @miro's post", Target(text="over the bay"), parse_page(TWO_POSTS),
                 parse_page(TWO_POSTS).by_ref("e1"))["anchored"] is False


async def test_an_earlier_toggle_on_the_wrong_post_spoils_the_run() -> None:
    both = LIKED_SECOND.replace('button "Like 3" [ref=e1]', 'button "Like 4" [pressed] [ref=e1]')
    tools = FakeTools(TWO_POSTS, LIKED_SECOND, both, results=CHANGED)
    decider = ScriptedDecider(step("CLICK", click_target="e2"), step("CLICK", click_target="e1"),
                              step("DONE", done=0.95))
    req = Request(lane="w/l", goal="Like the post by @mira", sites=("127.0.0.1",), seed="t")
    result = await Pilot(tools, decider, sleep=nap).run(req)
    assert result.status is Status.DONE_UNVERIFIED and result.reason == "target_not_in_goal"


def test_quoted_words_to_enter_are_not_an_anchor() -> None:
    found = anchors_of('Reply "Nice work" to @mira on "Sunset over the bay"', None, ("Nice work",))
    assert [(a.kind, a.value) for a in found] == [("author", "@mira"), ("text", "Sunset over the bay")]


def test_an_element_in_no_post_and_alone_needs_no_anchor() -> None:
    page = parse_page(TWO_POSTS.replace("  - article", "  - region", 1))
    assert check("Open settings", None, page, None) is None


def test_target_parse_refuses_what_it_does_not_know() -> None:
    with pytest.raises(ValueError):
        Target.parse({"id": "1"})
    with pytest.raises(ValueError):
        Target.parse({"text_sha256": "abc"})
    assert Target.parse({"author": " "}) is None


# ---- a signed-out page ------------------------------------------------------------------------

VAULT = Value(key="v1", label="the password (hidden, the node fills it)", vault="site-login", secret=True)


async def test_a_vault_value_alone_does_not_sign_a_lane_back_in() -> None:
    out = TWO_POSTS.replace("signal: ok", "signal: logged_out")
    decider = ScriptedDecider(step("DONE", done=0.95))
    req = Request(lane="w/l", goal="Like it", sites=("127.0.0.1",), values=(VAULT,))
    result = await Pilot(FakeTools(out), decider, sleep=nap).run(req)
    assert result.status is Status.HELD and result.reason == "signal_logged_out" and decider.requests == []
    login = Request(lane="w/l", goal="Sign in", sites=("127.0.0.1",), values=(VAULT,), login=True)
    await Pilot(FakeTools(out), decider, sleep=nap).run(login)
    assert len(decider.requests) == 1


async def test_words_typed_into_another_post_spoil_the_run() -> None:
    page = TWO_POSTS.replace('    - button "Like 1" [ref=e2] [cursor=pointer]\n',
                             '    - button "Like 1" [ref=e2] [cursor=pointer]\n    - textbox "Comment" [ref=e3]\n')
    liked = page.replace('button "Like 3" [ref=e1]', 'button "Like 4" [pressed] [ref=e1]')
    typed = page.replace('textbox "Comment" [ref=e3]', 'textbox "Comment" [ref=e3]: Nice')
    tools = FakeTools(page, typed, liked, results=CHANGED)
    value = Value(key="v1", label="the comment 'Nice'", text="Nice")
    decider = ScriptedDecider(step("TYPE", type_value="v1", field_for_v1="e3"), step("CLICK", click_target="e1"),
                              step("DONE", done=0.95))
    req = Request(lane="w/l", goal="Like the post by @mira", sites=("127.0.0.1",), seed="t", values=(value,))
    result = await Pilot(tools, decider, sleep=nap).run(req)
    assert result.status is Status.DONE_UNVERIFIED and result.reason == "target_not_in_goal"


async def test_a_signed_out_lane_after_an_approval_holds_the_run() -> None:
    from openajar.pilot import State

    pending = {"status": "approval_pending", "approval_id": "ap_1", "summary": "Like"}
    out = TWO_POSTS.replace("signal: ok", "signal: logged_out")
    tools = FakeTools(TWO_POSTS, out, results={"browser_act": [pending, {"ok": True, "changed": True}]})
    run = Pilot(tools, ScriptedDecider(step("CLICK", click_target="e1"), step("DONE", done=0.95)), sleep=nap)
    req = Request(lane="w/l", goal="Like the post by @mira", sites=("127.0.0.1",), seed="t")
    state = State()
    assert (await run.run(req, state)).status is Status.WAITING_APPROVAL
    second = await run.run(req, state)
    assert second.status is Status.HELD and len(tools.named("browser_act")) == 1


async def test_an_approved_click_that_now_matches_two_controls_sends_nothing() -> None:
    from openajar.pilot import State

    items = """signal: ok
http://127.0.0.1:8000/feed · Feed · 2 refs
- main:
  - list "Posts":
    - listitem:
      - paragraph: Tomatoes by @mira
      - button "Like 3" [ref=e1] [cursor=pointer]
"""
    later = items + """    - listitem:
      - paragraph: Basil by @tomas
      - button "Like 9" [ref=e2] [cursor=pointer]
"""
    pending = {"status": "approval_pending", "approval_id": "ap_1", "summary": "Like"}
    tools = FakeTools(items, later, results={"browser_act": [pending, {"ok": True, "changed": True}]})
    run = Pilot(tools, ScriptedDecider(step("CLICK", click_target="e1"), step("DONE", done=0.95)), sleep=nap)
    req = Request(lane="w/l", goal="Like the post by @mira", sites=("127.0.0.1",), seed="t")
    state = State()
    assert (await run.run(req, state)).status is Status.WAITING_APPROVAL
    second = await run.run(req, state)
    assert second.reason == "approved_target_ambiguous" and len(tools.named("browser_act")) == 1
