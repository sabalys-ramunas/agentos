"""Nested items: a comment in a post, a reply in a comment, a quoted post in a post. Each holds only its
own words, so a goal that names the inner item is never verified by an action on the outer one."""

from __future__ import annotations

from typing import Any

import pytest
from testkit.pilot import FakeTools, ScriptedDecider, step

from agentos.pilot import Pilot, Request, Status
from agentos.pilot.anchor import Target, check
from agentos.pilot.page import parse_page

# the dev site's /p/1 (runner/fakes.py), the QA report's repro
DEV_POST = """signal: ok
http://127.0.0.1:8000/p/1 · Post · 3 refs
- main:
  - article "Post by Ieva":
    - paragraph: Lovely day at the lake
    - button "Like" [ref=e1] [cursor=pointer]
    - article "Comment by Beata":
      - paragraph: So pretty
      - button "Like" [ref=e2] [cursor=pointer]
    - article "Comment by Ieva":
      - paragraph: dekui
      - button "Like" [ref=e3] [cursor=pointer]
"""

# a feed of posts in list items, each with comments, and replies under a comment
FEED = """signal: ok
http://127.0.0.1:8000/feed · Feed · 7 refs
- main:
  - list "Feed":
    - listitem:
      - article "Jonas":
        - link "@jonas" [ref=e20] [cursor=pointer]:
          - /url: /u/jonas
        - paragraph: Photo from the mountains
        - button "Like" [ref=e1] [cursor=pointer]
        - list "Comments":
          - listitem:
            - link "@beata" [ref=e21] [cursor=pointer]:
              - /url: /u/beata
            - paragraph: What a view
            - button "Like" [ref=e2] [cursor=pointer]
            - list "Replies":
              - listitem:
                - link "@tomas" [ref=e22] [cursor=pointer]:
                  - /url: /u/tomas
                - paragraph: Agreed, stunning
                - button "Like" [ref=e3] [cursor=pointer]
          - listitem:
            - link "@ieva" [ref=e23] [cursor=pointer]:
              - /url: /u/ieva
            - paragraph: Where is this
            - button "Like" [ref=e4] [cursor=pointer]
    - listitem:
      - article "Mira":
        - link "@mira" [ref=e24] [cursor=pointer]:
          - /url: /u/mira
        - paragraph: Basil from the garden
        - button "Like" [ref=e5] [cursor=pointer]
        - list "Comments":
          - listitem:
            - link "@beata" [ref=e25] [cursor=pointer]:
              - /url: /u/beata
            - paragraph: Looks fresh
            - button "Like" [ref=e6] [cursor=pointer]
"""

# a thread's own page: the post, then a comment tree
THREAD = """signal: ok
http://127.0.0.1:8000/r/garden/comments/7 · Thread · 4 refs
- main:
  - article "Tomatoes split after rain":
    - link "u/ona" [ref=e20] [cursor=pointer]:
      - /url: /user/ona
    - paragraph: Tomatoes split after rain, why?
    - button "Upvote" [ref=e1] [cursor=pointer]
  - comment "u/lukas":
    - link "u/lukas" [ref=e21] [cursor=pointer]:
      - /url: /user/lukas
    - paragraph: Water them evenly
    - button "Upvote" [ref=e2] [cursor=pointer]
    - comment "u/ona":
      - link "u/ona" [ref=e22] [cursor=pointer]:
        - /url: /user/ona
      - paragraph: Thanks, will try mulch
      - button "Upvote" [ref=e3] [cursor=pointer]
  - comment "u/rasa":
    - link "u/rasa" [ref=e23] [cursor=pointer]:
      - /url: /user/rasa
    - paragraph: Calcium helps too
    - button "Upvote" [ref=e4] [cursor=pointer]
"""

# a reshare: Ieva's post quotes Beata's, as a nested article, a blockquote and a link card
QUOTE_ARTICLE = """signal: ok
http://127.0.0.1:8000/feed · Feed · 3 refs
- main:
  - article "Ieva reshared":
    - link "@ieva" [ref=e20] [cursor=pointer]:
      - /url: /u/ieva
    - paragraph: So true
    - button "Like" [ref=e1] [cursor=pointer]
    - article "Beata":
      - link "@beata" [ref=e21] [cursor=pointer]:
        - /url: /u/beata
      - paragraph: Slow mornings are the best
      - button "Like" [ref=e2] [cursor=pointer]
  - article "Tomas":
    - link "@tomas" [ref=e22] [cursor=pointer]:
      - /url: /u/tomas
    - paragraph: Coffee first
    - button "Like" [ref=e3] [cursor=pointer]
"""
QUOTE_BLOCK = """signal: ok
http://127.0.0.1:8000/feed · Feed · 2 refs
- main:
  - article "Ieva reshared":
    - link "@ieva" [ref=e20] [cursor=pointer]:
      - /url: /u/ieva
    - paragraph: So true
    - blockquote:
      - text: "@beata Slow mornings are the best"
    - button "Like" [ref=e1] [cursor=pointer]
  - article "Tomas":
    - paragraph: Coffee first
    - button "Like" [ref=e3] [cursor=pointer]
"""
QUOTE_CARD = """signal: ok
http://127.0.0.1:8000/feed · Feed · 3 refs
- main:
  - article "Ieva reshared":
    - link "@ieva" [ref=e20] [cursor=pointer]:
      - /url: /u/ieva
    - paragraph: So true
    - link "Beata @beata Slow mornings are the best" [ref=e21] [cursor=pointer]:
      - /url: /p/55
      - text: "@beata"
      - paragraph: Slow mornings are the best
    - button "Like" [ref=e1] [cursor=pointer]
  - article "Tomas":
    - paragraph: Coffee first
    - button "Like" [ref=e3] [cursor=pointer]
"""
# comments that are not items of their own: two Likes in one post tell nothing apart
FLAT_IN_POST = """signal: ok
http://127.0.0.1:8000/p/1 · Post · 2 refs
- main:
  - article "Post by Ieva":
    - paragraph: Lovely day at the lake
    - button "Like" [ref=e1] [cursor=pointer]
    - group "Comment by Beata":
      - paragraph: So pretty
      - button "Like" [ref=e2] [cursor=pointer]
"""


def anchored(page_text: str, ref: str, goal: str, target: Target | None = None) -> bool:
    page = parse_page(page_text)
    result = check(goal, target, page, page.by_ref(ref))
    assert result is not None
    return result["anchored"]


CASES = [
    # (page, goal, target, right ref, the parent's ref, a sibling's ref)
    # the dev site shows names only, no handle: a whole line the comment says pins it
    (DEV_POST, "Like @beata's comment", Target(text="So pretty"), "e2", "e1", "e3"),
    (DEV_POST, "Like the comment by Beata", Target(text="So pretty"), "e2", "e1", "e3"),
    (DEV_POST, "Like it", Target(author="Beata", text="So pretty"), "e2", "e1", "e3"),
    (FEED, "Like @beata's comment on Jonas's photo", None, "e2", "e1", "e4"),
    (FEED, "Like @beata's comment on @jonas's photo", None, "e2", "e1", "e4"),
    (FEED, "Like @tomas's reply", None, "e3", "e2", "e4"),
    (FEED, "Like the reply by @tomas", None, "e3", "e2", "e4"),
    (THREAD, "Upvote u/lukas's comment", Target(author="u/lukas"), "e2", "e1", "e4"),
    (THREAD, "Upvote u/ona's reply about mulch", None, "e3", "e2", "e4"),
    (QUOTE_ARTICLE, "Like @beata's post", None, "e2", "e1", "e3"),
]


@pytest.mark.parametrize(("page", "goal", "target", "right", "parent", "sibling"), CASES)
def test_the_named_inner_item_is_the_only_one_verified(page: str, goal: str, target: Target | None,
                                                       right: str, parent: str, sibling: str) -> None:
    assert anchored(page, parent, goal, target) is False
    assert anchored(page, sibling, goal, target) is False
    assert anchored(page, right, goal, target) is True


def test_a_comment_by_beata_on_another_post_is_a_rival() -> None:
    # beata comments on both posts: only the post named around it tells them apart
    assert anchored(FEED, "e2", "Like @beata's comment", None) is False
    assert anchored(FEED, "e6", "Like @beata's comment on @mira's post", None) is True
    assert anchored(FEED, "e2", "Like @beata's comment on @mira's post", None) is False


def test_the_outer_post_is_still_verified_when_it_is_the_one_named() -> None:
    assert anchored(DEV_POST, "e1", "Like Ieva's lake post", Target(text="Lovely day at the lake")) is True
    assert anchored(FEED, "e1", "Like @jonas's post", None) is True
    assert anchored(FEED, "e2", "Like @jonas's post", None) is False   # a comment in it is not it
    assert anchored(THREAD, "e1", "Upvote the post about tomatoes", None) is True
    assert anchored(QUOTE_ARTICLE, "e1", "Like @ieva's post", None) is True


@pytest.mark.parametrize("page", [QUOTE_BLOCK, QUOTE_CARD])
def test_a_quoted_post_is_not_the_post_quoting_it(page: str) -> None:
    assert anchored(page, "e1", "Like @beata's post", None) is False
    assert anchored(page, "e1", "Like the post about slow mornings", None) is False
    assert anchored(page, "e1", "Like @ieva's post", None) is True


@pytest.mark.parametrize("page", [FLAT_IN_POST, FLAT_IN_POST.replace('"Like" [ref=e2]', '"Like comment" [ref=e2]')
                                  .replace('group "Comment by Beata"', "generic").replace(
                                      "paragraph: So pretty", 'link "Beata" [ref=e9]:\n        - /url: /u/beata')])
def test_two_alike_controls_in_one_item_tell_nothing_apart(page: str) -> None:
    assert anchored(page, "e1", "Like @beata's comment", None) is False
    assert anchored(page, "e2", "Like @beata's comment", None) is False


async def nap(_: float) -> None:
    return None


async def run(ref: str, goal: str, **kw: Any):
    after = DEV_POST.replace(f'button "Like" [ref={ref}]', f'button "Like" [pressed] [ref={ref}]')
    tools = FakeTools(DEV_POST, after, results={"browser_act": {"ok": True, "changed": True, "detail": "pressed"}})
    decider = ScriptedDecider(step("CLICK", click_target=ref), step("DONE", done=0.95))
    req = Request(lane="w/l", goal=goal, sites=("127.0.0.1",), seed="t", **kw)
    return await Pilot(tools, decider, sleep=nap).run(req)


@pytest.mark.parametrize(("goal", "target"), [("Like @beata's comment", None), ("Like the comment by Beata", None),
                                              ("Like it", Target(author="Beata"))])
async def test_the_qa_repro_a_like_on_the_post_is_unverified(goal: str, target: Target | None) -> None:
    wrong = await run("e1", goal, target=target)
    named = await run("e2", goal, target=target)   # the dev site shows a name only: two Beatas look the same
    sure = Target(author=target.author if target else None, text="So pretty")
    right = await run("e2", goal, target=sure)
    assert wrong.status is Status.DONE_UNVERIFIED and wrong.reason == "target_not_in_goal"
    assert named.status is Status.DONE_UNVERIFIED and "@handle or profile link" in (named.message or "")
    assert right.status is Status.DONE_VERIFIED


# ---- what the adversarial sweep tried ----------------------------------------------------------

REPLY_MENTIONS = FEED.replace("paragraph: Agreed, stunning", "paragraph: \"@beata is this the lake by the mill?\"")
GROUP_QUOTE = """signal: ok
http://127.0.0.1:8000/feed · Feed · 2 refs
- main:
  - article "Ieva reshared":
    - link "@ieva" [ref=e20] [cursor=pointer]:
      - /url: /u/ieva
    - paragraph: So true
    - group "Quoted post":
      - link "@beata" [ref=e21] [cursor=pointer]:
        - /url: /u/beata
      - paragraph: Slow mornings are the best
      - link "3h" [ref=e22] [cursor=pointer]:
        - /url: /p/55
    - button "Like" [ref=e1] [cursor=pointer]
  - article "Tomas":
    - paragraph: Coffee first
    - button "Like" [ref=e3] [cursor=pointer]
"""
GROUP_POST = """signal: ok
http://127.0.0.1:8000/p/1 · Post · 2 refs
- main:
  - group "Post by Ieva":
    - paragraph: Lovely day at the lake
    - button "Like" [ref=e1] [cursor=pointer]
  - article "Comment by Beata":
    - paragraph: So pretty
    - button "Like" [ref=e2] [cursor=pointer]
"""
DOTTED = """signal: ok
http://127.0.0.1:8000/feed · Feed · 1 refs
- main:
  - article "Mira Bakes":
    - link "@mira.bakes" [ref=e12] [cursor=pointer]:
      - /url: /u/mira.bakes
    - paragraph: Tomatoes are in
    - button "Like" [ref=e2] [cursor=pointer]
"""
ACTIONS_BAR = FEED.replace('        - button "Like" [ref=e1] [cursor=pointer]\n',
                           '        - list "Actions":\n          - listitem:\n'
                           '            - button "Like" [ref=e1] [cursor=pointer]\n')


@pytest.mark.parametrize(("page", "ref", "goal"), [
    (FEED, "e1", "Like the comment on Jonas's photo"),           # the item word says comment
    (DEV_POST, "e1", "Like the comment under Ieva's lake post"),
    (FEED, "e2", "Like the reply to @beata"),                    # the comment the reply answers
    (FEED, "e2", "Like the reply to Beata"),
    (REPLY_MENTIONS, "e3", "Like @beata's comment about the lake"),  # a reply that mentions her
    (REPLY_MENTIONS, "e3", "Like Beata's comment about the lake"),
    (GROUP_QUOTE, "e1", "Like @beata's post"),                    # a quote that is a named block
    (GROUP_QUOTE, "e1", "Like the post by Beata"),
    (GROUP_QUOTE, "e1", "Like the post at http://127.0.0.1:8000/p/55"),
    (GROUP_POST, "e2", "Like http://127.0.0.1:8000/p/1"),         # the post is no item, its comment is
    (DOTTED, "e2", "Like @mira's post"),                          # @mira.bakes is not @mira
    (DEV_POST.replace("Post by Ieva", "Post"), "e1", "Like the comment at http://127.0.0.1:8000/p/1#c3"),
])
def test_wrong_items_the_sweep_found_are_unverified(page: str, ref: str, goal: str) -> None:
    assert anchored(page, ref, goal) is False


def test_a_mention_is_not_authorship_and_a_lone_control_must_hold_what_is_named() -> None:
    lone = """signal: ok
http://127.0.0.1:8000/feed · Feed · 1 refs
- main:
  - group "Post by Mira":
    - paragraph: Basil
    - button "Like" [ref=e5] [cursor=pointer]
"""
    assert anchored(lone, "e5", "Like @ieva's post") is False
    assert anchored(lone, "e5", "Like it", Target(author="Ieva")) is False
    mention = TWO_POSTS_MENTION
    assert anchored(mention, "e1", "Like @mira's post") is False


TWO_POSTS_MENTION = """signal: ok
http://127.0.0.1:8000/feed · Feed · 1 refs
- main:
  - article "tomas":
    - link "@tomas" [ref=e10] [cursor=pointer]:
      - /url: /u/tomas
    - paragraph: Replying to @mira great shot
    - button "Like 2" [ref=e1] [cursor=pointer]
"""


def test_a_like_in_an_actions_bar_still_belongs_to_its_post() -> None:
    assert anchored(ACTIONS_BAR, "e1", "Like @jonas's post") is True
    assert anchored(ACTIONS_BAR, "e5", "Like @jonas's post") is False
