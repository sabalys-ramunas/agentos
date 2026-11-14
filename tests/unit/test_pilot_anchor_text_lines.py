"""Items whose words are plain text lines (`- text: "..."`), not paragraphs: the shape the dev site and
many real sites (bodies in spans and divs) write.

Who wrote an item comes only from its author slot: its own name, a name link, a heading, or a profile
link in its header. A text line is what the item says, so "@beata" in one is a mention, never the
author; an item that shows no author slot has no known author, and an author the request names cannot
verify on it."""

from __future__ import annotations

import pytest

from agentos.pilot.anchor import Target, check
from agentos.pilot.page import parse_page


def result(page_text: str, ref: str, goal: str, target: Target | None = None) -> dict:
    page = parse_page(page_text)
    out = check(goal, target, page, page.by_ref(ref))
    assert out is not None
    return out


# ---- a mention in a text line is not the author ---------------------------------------------------

MENTION_POST = """signal: ok
http://127.0.0.1:8000/p/1 · Post · 2 refs
- main
  - article "Basil is up"
    - text: "@beata thanks for the seeds"
    - button "Like" [ref=e1] pressed=false
"""

MENTION_COMMENT = """signal: ok
http://127.0.0.1:8000/p/1 · Post · 3 refs
- main
  - article "Post by Ieva"
    - text: "Lovely day at the lake"
    - button "Like" [ref=e1] pressed=false
    - article "Comment by Mallory"
      - text: "@beata great photo"
      - button "Like" [ref=e2] pressed=false
"""

MENTION_RESHARE = """signal: ok
http://127.0.0.1:8000/feed · Feed · 2 refs
- main
  - article "Ieva reshared"
    - text: "So true"
    - text: "@beata Slow mornings are the best"
    - button "Like" [ref=e1] pressed=false
  - article "Tomas"
    - text: "Coffee first"
    - button "Like" [ref=e3] pressed=false
"""

# a text line before any other words, and one that is a handle alone: still what the item says
MENTION_FIRST = """signal: ok
http://127.0.0.1:8000/feed · Feed · 2 refs
- main
  - article "Post"
    - text: "@beata"
    - text: "Look what came in the mail"
    - button "Like" [ref=e1] pressed=false
  - article "Post"
    - text: "Coffee first"
    - button "Like" [ref=e2] pressed=false
"""

# the author slot is there (a profile link in the header): the mention in the text is still not it
MENTION_UNDER_AUTHOR = """signal: ok
http://127.0.0.1:8000/feed · Feed · 2 refs
- main
  - article "Post"
    - link "@ieva" [ref=e20]:
      - /url: /u/ieva
    - text: "@beata thanks for the seeds"
    - button "Like" [ref=e1] pressed=false
  - article "Post"
    - link "@tomas" [ref=e21]:
      - /url: /u/tomas
    - text: "Coffee first"
    - button "Like" [ref=e2] pressed=false
"""


@pytest.mark.parametrize(("page", "ref", "goal"), [
    (MENTION_POST, "e1", "Like @beata's post"),
    (MENTION_POST, "e1", "Like the post by @beata"),
    (MENTION_COMMENT, "e2", "Like @beata's comment"),
    (MENTION_COMMENT, "e2", "Like the comment by @beata"),
    (MENTION_RESHARE, "e1", "Like @beata's post"),
    (MENTION_FIRST, "e1", "Like @beata's post"),
    (MENTION_UNDER_AUTHOR, "e1", "Like @beata's post"),
])
def test_a_mention_in_a_text_line_is_not_the_author(page: str, ref: str, goal: str) -> None:
    assert not result(page, ref, goal)["anchored"]


@pytest.mark.parametrize(("page", "ref"), [(MENTION_POST, "e1"), (MENTION_COMMENT, "e2"), (MENTION_FIRST, "e1")])
def test_a_caller_author_is_not_a_mention_either(page: str, ref: str) -> None:
    assert not result(page, ref, "Like it", Target(author="@beata"))["anchored"]


def test_a_text_line_never_counts_as_an_author_row() -> None:
    page = parse_page(MENTION_POST)
    assert all("@beata" not in name for box in page.boxes for name in box.names)


def test_the_author_slot_still_verifies_when_the_words_are_text_lines() -> None:
    assert result(MENTION_UNDER_AUTHOR, "e1", "Like @ieva's post")["anchored"]
    assert not result(MENTION_UNDER_AUTHOR, "e2", "Like @ieva's post")["anchored"]


# a line that only names someone else ("Beata @beata") is the byline of a post the item quotes as rows
BYLINE_IN_WORDS = """signal: ok
http://127.0.0.1:8000/feed · Feed · 2 refs
- main
  - article "Ieva"
    - link "@ieva" [ref=e20]:
      - /url: /u/ieva
    - text: "So true"
    - text: "Beata @beata"
    - text: "Slow mornings are the best"
    - button "Like" [ref=e1] pressed=false
  - article "Tomas"
    - link "@tomas" [ref=e22]:
      - /url: /u/tomas
    - text: "Coffee first"
    - button "Like" [ref=e3] pressed=false
"""


@pytest.mark.parametrize("goal", ['Like @ieva\'s post "Slow mornings are the best"', "Like @beata's post",
                                  'Like the post "Slow mornings are the best"'])
def test_a_line_that_only_names_someone_else_blurs_the_item(goal: str) -> None:
    out = result(BYLINE_IN_WORDS, "e1", goal)
    assert not out["anchored"]
    assert "another person named" in out["by"]


# ---- words as text lines are the item's own words -------------------------------------------------
# The dev site writes an item's words as a text line under it, never a paragraph (agentos.dev.web.outline).

DEV_POST = """signal: ok
https://social.example/p/1 · Post · 3 refs
- main
  - article "Post by Ieva"
    - text: "Lovely day at the lake"
    - button "Like" [ref=e1] pressed=false
    - article "Comment by Beata"
      - text: "So pretty"
      - button "Like" [ref=e2] pressed=false
    - article "Comment by Ieva"
      - text: "dekui"
      - button "Like" [ref=e3] pressed=false
"""

# a body in spans: a text line, then a mention as a link inside the words, under the author's link
SPAN_FEED = """signal: ok
http://127.0.0.1:8000/feed · Feed · 2 refs
- main
  - article "Post"
    - link "@ieva" [ref=e20]:
      - /url: /u/ieva
    - text: "Thanks for the seeds"
    - link "@beata" [ref=e21]:
      - /url: /u/beata
    - button "Like" [ref=e1] pressed=false
  - article "Post"
    - link "@tomas" [ref=e22]:
      - /url: /u/tomas
    - text: "Coffee first"
    - button "Like" [ref=e2] pressed=false
"""

NOT_SO_PRETTY_LINES = """signal: ok
http://127.0.0.1:8000/p/1 · Post · 3 refs
- main
  - article "Post by Jonas"
    - text: "Lovely day at the lake"
    - button "Like" [ref=e1] pressed=false
    - article "Comment by Beata"
      - text: "Not so pretty"
      - button "Like" [ref=e2] pressed=false
    - article "Comment by Ieva"
      - text: "So pretty"
      - button "Like" [ref=e3] pressed=false
"""


@pytest.mark.parametrize(("goal", "target"), [
    ("Like it", Target(author="Beata", text="So pretty")),
    ('Like Beata\'s comment "So pretty"', None),
    ('Like the comment "So pretty"', None),
])
def test_a_text_line_verifies_the_item_that_says_it(goal: str, target: Target | None) -> None:
    assert result(DEV_POST, "e2", goal, target)["anchored"]
    assert not result(DEV_POST, "e1", goal, target)["anchored"]
    assert not result(DEV_POST, "e3", goal, target)["anchored"]


def test_a_text_line_is_said_whole_and_never_an_author_slot() -> None:
    page = parse_page(DEV_POST)
    comment = page.boxes[page.by_ref("e2").box]
    assert comment.said == ["So pretty"]
    assert comment.names == ["Comment by Beata"]


def test_a_quote_in_a_longer_text_line_is_unverified() -> None:
    assert not result(NOT_SO_PRETTY_LINES, "e2", 'Like the comment "So pretty"')["anchored"]
    assert not result(NOT_SO_PRETTY_LINES, "e2", "Like it", Target(text="So pretty"))["anchored"]
    assert result(NOT_SO_PRETTY_LINES, "e3", 'Like the comment "So pretty"')["anchored"]


def test_a_link_after_the_words_is_a_mention_not_the_author() -> None:
    assert not result(SPAN_FEED, "e1", "Like @beata's post")["anchored"]
    # a person named outside the words, not as the author, may be a quoted post's: unverified, never a guess
    out = result(SPAN_FEED, "e1", "Like @ieva's post")
    assert not out["anchored"] and "another person named" in out["by"]
    assert result(SPAN_FEED, "e2", "Like @tomas's post")["anchored"]


def test_a_dev_site_post_page_verifies_the_right_comment() -> None:
    """The QA steps, on the outline the dev site itself renders for /p/1."""
    import asyncio

    from agentos.dev.web import DevDriver, dev_site, outline

    async def render() -> str:
        driver = DevDriver(dev_site())
        await driver.navigate("https://social.example/p/1")
        return outline(driver)[0]

    page_text = "signal: ok\nhttps://social.example/p/1 · Post · 3 refs\n" + asyncio.run(render())
    target = Target(author="Beata", text="So pretty")
    assert [result(page_text, ref, "Like it", target)["anchored"] for ref in ("e1", "e2", "e3")] == [False, True, False]


# ---- shapes the attacks found -------------------------------------------------------------------
FEED = "signal: ok\nhttp://127.0.0.1:8000/feed · Feed · 3 refs\n- main\n"
RIVAL = """  - article "Post"
    - link "@tomas" [ref=e21]:
      - /url: /u/tomas
    - text: "Coffee first"
    - button "Like" [ref=e2] pressed=false
"""
POST = """signal: ok
http://127.0.0.1:8000/p/1 · Post · 3 refs
- main
  - article "Post by Ieva"
    - link "@ieva" [ref=e20]:
      - /url: /u/ieva
    - text: "Lovely day at the lake"
    - button "Like" [ref=e3] pressed=false
"""


def mention_after(words: str) -> str:
    """A post with no author slot: its words, then a mention link."""
    return FEED + f"""  - article "Post"
    {words}
    - link "@beata" [ref=e20]:
      - /url: /u/beata
    - button "Like" [ref=e1] pressed=false
""" + RIVAL


@pytest.mark.parametrize("words", [
    '- generic: Thanks for the seeds',         # words in a nameless block
    '- text: "\U0001F33F\U0001F33F\U0001F33F"',  # words that are emoji
    '- text: "..."',
    '- generic: Liked by',
])
def test_a_link_after_any_words_is_not_the_author(words: str) -> None:
    assert not result(mention_after(words), "e1", "Like @beata's post")["anchored"]


def test_a_reply_line_in_a_nameless_block_does_not_make_the_reply_target_the_author() -> None:
    page = POST + """    - article "Comment by Mallory"
      - generic: Replying to
      - link "u/beata" [ref=e21]:
        - /url: /u/beata
      - text: "Great photo"
      - button "Like" [ref=e1] pressed=false
"""
    assert not result(page, "e1", "Like u/beata's comment")["anchored"]


# a quoted post as a named group of a link and text lines is an item of its own, as it is with a paragraph
GROUP_QUOTE_LINES = FEED + """  - article "Post by Jonas"
    - link "@jonas" [ref=e20]:
      - /url: /u/jonas
    - text: "Look at this"
    - group "Quoted post"
      - link "Beata" [ref=e21]:
        - /url: /p/9
      - text: "So pretty"
      - button "Like" [ref=e1] pressed=false
""" + RIVAL


def test_a_quoted_group_of_text_lines_is_an_item_of_its_own() -> None:
    page = parse_page(GROUP_QUOTE_LINES)
    assert page.boxes[page.by_ref("e1").box].title == "Quoted post"
    assert not result(GROUP_QUOTE_LINES, "e1", 'Like @jonas\'s post "So pretty"')["anchored"]


@pytest.mark.parametrize("byline", ["Ieva @ieva · 2h", "Ieva (@ieva)", "Ieva · @ieva", "@ieva · 3 hours ago"])
def test_a_byline_with_a_time_or_separators_still_blurs(byline: str) -> None:
    page = POST + f"""    - article "Comment by Beata"
      - link "@beata" [ref=e21]:
        - /url: /u/beata
      - text: "{byline}"
      - text: "So pretty"
      - button "Like" [ref=e1] pressed=false
"""
    out = result(page, "e1", 'Like @beata\'s comment "So pretty"')
    assert not out["anchored"] and "another person named" in out["by"]
