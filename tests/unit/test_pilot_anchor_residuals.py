"""Three shapes where the wrong item could still verify: a quote with no structure of its own, a
quoted fragment of a longer text, and an author named by a display name alone."""

from __future__ import annotations

import pytest

from openajar.pilot.anchor import Target, check, line_sha256
from openajar.pilot.page import parse_page


def result(page_text: str, ref: str, goal: str, target: Target | None = None) -> dict:
    page = parse_page(page_text)
    out = check(goal, target, page, page.by_ref(ref))
    assert out is not None
    return out


# ---- a quoted fragment of a longer text ---------------------------------------------------------
# A quote verifies only when it is the whole of one line the item says (a paragraph, a text line, a
# heading or its own name), compared after normalising case, spacing and the punctuation at its ends.
# A quote that is only part of a line may sit in words that turn its meaning ("Not so pretty"), so it
# narrows the search but never verifies on its own.

NOT_SO_PRETTY = """signal: ok
http://127.0.0.1:8000/p/1 · Post · 4 refs
- main:
  - article "Post by Jonas":
    - link "@jonas" [ref=e20] [cursor=pointer]:
      - /url: /u/jonas
    - paragraph: Lovely day at the lake
    - button "Like" [ref=e1] [cursor=pointer]
    - article "Comment by Beata":
      - link "@beata" [ref=e21] [cursor=pointer]:
        - /url: /u/beata
      - paragraph: Not so pretty
      - button "Like" [ref=e2] [cursor=pointer]
"""

BOTH_PRETTY = """signal: ok
http://127.0.0.1:8000/p/1 · Post · 6 refs
- main:
  - article "Post by Jonas":
    - link "@jonas" [ref=e20] [cursor=pointer]:
      - /url: /u/jonas
    - paragraph: Lovely day at the lake
    - button "Like" [ref=e1] [cursor=pointer]
    - article "Comment by Beata":
      - link "@beata" [ref=e21] [cursor=pointer]:
        - /url: /u/beata
      - paragraph: Not so pretty
      - button "Like" [ref=e2] [cursor=pointer]
    - article "Comment by Ieva":
      - link "@ieva" [ref=e22] [cursor=pointer]:
        - /url: /u/ieva
      - paragraph: So pretty!
      - button "Like" [ref=e3] [cursor=pointer]
"""

LONGER_SENTENCE = """signal: ok
http://127.0.0.1:8000/p/1 · Post · 6 refs
- main:
  - article "Post by Jonas":
    - link "@jonas" [ref=e20] [cursor=pointer]:
      - /url: /u/jonas
    - paragraph: Lovely day at the lake
    - button "Like" [ref=e1] [cursor=pointer]
    - article "Comment by Beata":
      - link "@beata" [ref=e21] [cursor=pointer]:
        - /url: /u/beata
      - paragraph: So pretty, but the water looks cold
      - button "Like" [ref=e2] [cursor=pointer]
    - article "Comment by Ieva":
      - link "@ieva" [ref=e22] [cursor=pointer]:
        - /url: /u/ieva
      - paragraph: dekui
      - button "Like" [ref=e3] [cursor=pointer]
"""

TWO_SAME = """signal: ok
http://127.0.0.1:8000/p/1 · Post · 6 refs
- main:
  - article "Post by Jonas":
    - link "@jonas" [ref=e20] [cursor=pointer]:
      - /url: /u/jonas
    - paragraph: Lovely day at the lake
    - button "Like" [ref=e1] [cursor=pointer]
    - article "Comment by Beata":
      - link "@beata" [ref=e21] [cursor=pointer]:
        - /url: /u/beata
      - paragraph: So pretty
      - button "Like" [ref=e2] [cursor=pointer]
    - article "Comment by Ieva":
      - link "@ieva" [ref=e22] [cursor=pointer]:
        - /url: /u/ieva
      - paragraph: so   pretty
      - button "Like" [ref=e3] [cursor=pointer]
"""

MULTI_PARAGRAPH = """signal: ok
http://127.0.0.1:8000/p/1 · Post · 6 refs
- main:
  - article "Post by Jonas":
    - link "@jonas" [ref=e20] [cursor=pointer]:
      - /url: /u/jonas
    - paragraph: Lovely day at the lake
    - button "Like" [ref=e1] [cursor=pointer]
    - article "Comment by Beata":
      - link "@beata" [ref=e21] [cursor=pointer]:
        - /url: /u/beata
      - paragraph: So pretty
      - paragraph: Wish I was there
      - button "Like" [ref=e2] [cursor=pointer]
    - article "Comment by Ieva":
      - link "@ieva" [ref=e22] [cursor=pointer]:
        - /url: /u/ieva
      - paragraph: Not so pretty
      - button "Like" [ref=e3] [cursor=pointer]
"""

NEGATION_AROUND = """signal: ok
http://127.0.0.1:8000/p/1 · Post · 6 refs
- main:
  - article "Post by Jonas":
    - link "@jonas" [ref=e20] [cursor=pointer]:
      - /url: /u/jonas
    - paragraph: Lovely day at the lake
    - button "Like" [ref=e1] [cursor=pointer]
    - article "Comment by Beata":
      - link "@beata" [ref=e21] [cursor=pointer]:
        - /url: /u/beata
      - paragraph: I would never say so pretty
      - button "Like" [ref=e2] [cursor=pointer]
    - article "Comment by Ieva":
      - link "@ieva" [ref=e22] [cursor=pointer]:
        - /url: /u/ieva
      - paragraph: Thanks
      - button "Like" [ref=e3] [cursor=pointer]
"""


@pytest.mark.parametrize(("page", "ref"), [(NOT_SO_PRETTY, "e2"), (BOTH_PRETTY, "e2"), (LONGER_SENTENCE, "e2"),
                                           (NEGATION_AROUND, "e2"), (MULTI_PARAGRAPH, "e3")])
def test_a_quote_inside_longer_words_is_unverified(page: str, ref: str) -> None:
    out = result(page, ref, 'Like the comment "So pretty"')
    assert out["anchored"] is False
    assert "only part of" in out["by"] and "whole text" in out["by"]


def test_a_caller_text_that_is_a_fragment_is_unverified_too() -> None:
    out = result(NOT_SO_PRETTY, "e2", "Like it", Target(text="so pretty"))
    assert out["anchored"] is False and "only part of" in out["by"]


@pytest.mark.parametrize(("page", "ref"), [(BOTH_PRETTY, "e3"), (MULTI_PARAGRAPH, "e2")])
def test_the_whole_line_quoted_verifies(page: str, ref: str) -> None:
    out = result(page, ref, 'Like the comment "So pretty"')
    assert out["anchored"] is True, out


def test_the_whole_line_from_the_caller_verifies() -> None:
    assert result(BOTH_PRETTY, "e3", "Like it", Target(text="so pretty!"))["anchored"] is True
    assert result(LONGER_SENTENCE, "e2", "Like it",
                  Target(text="So pretty, but the water looks cold"))["anchored"] is True
    assert result(LONGER_SENTENCE, "e2", "Like it",
                  Target(text_sha256=line_sha256("So pretty, but the water looks cold")))["anchored"] is True


def test_the_same_whole_line_twice_tells_nothing_apart() -> None:
    for ref in ("e2", "e3"):
        out = result(TWO_SAME, ref, 'Like the comment "So pretty"')
        assert out["anchored"] is False and "pass target" in out["by"]


def test_a_paragraph_split_into_pieces_is_read_whole() -> None:
    page = NOT_SO_PRETTY.replace("      - paragraph: Not so pretty\n",
                                 '      - paragraph:\n        - text: "Not"\n        - text: "so pretty"\n')
    assert result(page, "e2", 'Like the comment "So pretty"')["anchored"] is False


def test_a_fragment_in_the_acted_item_is_unverified_even_when_the_post_says_it_whole() -> None:
    page = NOT_SO_PRETTY.replace("paragraph: Lovely day at the lake", "paragraph: So pretty")
    assert result(page, "e2", 'Like @beata\'s comment "So pretty"')["anchored"] is False
    assert result(page, "e1", 'Like @jonas\'s post "So pretty"')["anchored"] is True  # the post says it whole


# ---- an author named by a display name alone ----------------------------------------------------
# Two people can share a display name, so a name alone ("Ieva", "the post by Ieva") never verifies.
# What does: an @handle (or u/name) the item shows as its author, a profile link it shows, or another
# anchor that pins the item (a permalink, a whole line it says). A handle the page cannot check
# (the item shows only a name) counts as a name.

TWO_IEVAS = """signal: ok
http://127.0.0.1:8000/feed · Feed · 6 refs
- main:
  - article "Ieva Petraite":
    - link "Ieva" [ref=e10] [cursor=pointer]:
      - /url: /u/ieva.p
    - link "@ieva.p" [ref=e11] [cursor=pointer]:
      - /url: /u/ieva.p
    - paragraph: Lake at dawn
    - button "Like" [ref=e1] [cursor=pointer]
  - article "Ieva Kairyte":
    - link "Ieva" [ref=e12] [cursor=pointer]:
      - /url: /u/ieva_k
    - link "@ieva_k" [ref=e13] [cursor=pointer]:
      - /url: /u/ieva_k
    - paragraph: Basil seedlings
    - button "Like" [ref=e2] [cursor=pointer]
"""

# only the first article of TWO_IEVAS; the other Ieva is not on the page
ONE_IEVA = """signal: ok
http://127.0.0.1:8000/feed · Feed · 3 refs
- main:
  - article "Ieva Petraite":
    - link "Ieva" [ref=e10] [cursor=pointer]:
      - /url: /u/ieva.p
    - link "@ieva.p" [ref=e11] [cursor=pointer]:
      - /url: /u/ieva.p
    - paragraph: Lake at dawn
    - button "Like" [ref=e1] [cursor=pointer]
"""

# the name is a heading only: no link, no handle
NAME_ONLY = """signal: ok
http://127.0.0.1:8000/feed · Feed · 2 refs
- main:
  - article "Post by Ieva":
    - heading "Ieva"
    - paragraph: Lake at dawn
    - button "Like" [ref=e1] [cursor=pointer]
  - article "Post by Tomas":
    - heading "Tomas"
    - paragraph: Coffee first
    - button "Like" [ref=e2] [cursor=pointer]
"""

# the name is a profile link only: no @handle text anywhere on the page
# (article names are not given in the brief; the visible names are used)
PROFILE_LINK_ONLY = """signal: ok
http://127.0.0.1:8000/feed · Feed · 4 refs
- main:
  - article "Ieva":
    - link "Ieva" [ref=e10] [cursor=pointer]:
      - /url: /u/ieva.p
    - paragraph: Lake at dawn
    - button "Like" [ref=e1] [cursor=pointer]
  - article "Tomas":
    - link "Tomas" [ref=e12] [cursor=pointer]:
      - /url: /u/tomas
    - paragraph: Coffee first
    - button "Like" [ref=e2] [cursor=pointer]
"""

# two comments on a thread, authored by u/ieva and u/ieva_k
REDDIT_NAMES = """signal: ok
http://127.0.0.1:8000/r/garden/comments/7 · Thread · 4 refs
- main:
  - comment "u/ieva":
    - link "u/ieva" [ref=e10] [cursor=pointer]:
      - /url: /user/ieva
    - paragraph: Water them evenly
    - button "Upvote" [ref=e1] [cursor=pointer]
  - comment "u/ieva_k":
    - link "u/ieva_k" [ref=e12] [cursor=pointer]:
      - /url: /user/ieva_k
    - paragraph: Calcium helps
    - button "Upvote" [ref=e2] [cursor=pointer]
"""


@pytest.mark.parametrize(("page", "goal", "target"), [
    (TWO_IEVAS, "Like Ieva's post", None),
    (ONE_IEVA, "Like Ieva's post", None),                 # the Ieva meant may not be on the page at all
    (ONE_IEVA, "Like the post by Ieva", None),
    (ONE_IEVA, "Like it", Target(author="Ieva")),
    (ONE_IEVA, "Like Ieva's lake post", None),            # a word of the post does not make the name sure
    (PROFILE_LINK_ONLY, "Like it", Target(author="Ieva")),
])
def test_a_display_name_alone_is_unverified(page: str, goal: str, target: Target | None) -> None:
    out = result(page, "e1", goal, target)
    assert out["anchored"] is False
    assert "display name" in out["by"] and "@handle or profile link" in out["by"]


def test_a_handle_the_page_cannot_check_is_a_name() -> None:
    out = result(NAME_ONLY, "e1", "Like @ieva's post")
    assert out["anchored"] is False and "no @handle or profile link" in out["by"]


@pytest.mark.parametrize(("page", "ref", "wrong", "goal", "target"), [
    (TWO_IEVAS, "e1", "e2", "Like @ieva.p's post", None),
    (TWO_IEVAS, "e2", "e1", "Like it", Target(author="@ieva_k")),
    (TWO_IEVAS, "e2", "e1", "Like it", Target(author="Ieva", text="Basil seedlings")),
    (TWO_IEVAS, "e1", "e2", "Like it", Target(author="http://127.0.0.1:8000/u/ieva.p")),
    (PROFILE_LINK_ONLY, "e1", "e2", "Like it", Target(author="/u/ieva.p")),
    (PROFILE_LINK_ONLY, "e1", "e2", "Like @ieva.p's post", None),
    (NAME_ONLY, "e1", "e2", "Like it", Target(author="Ieva", text="Lake at dawn")),
    (REDDIT_NAMES, "e1", "e2", "Upvote u/ieva's comment", None),
    (REDDIT_NAMES, "e2", "e1", "Upvote it", Target(author="u/ieva_k")),
])
def test_a_handle_or_link_makes_the_name_sure(page: str, ref: str, wrong: str, goal: str,
                                              target: Target | None) -> None:
    assert result(page, ref, goal, target)["anchored"] is True
    assert result(page, wrong, goal, target)["anchored"] is False


def test_a_handle_is_not_another_handle_that_shares_the_name() -> None:
    assert result(TWO_IEVAS, "e1", "Like @ieva's post")["anchored"] is False
    assert result(TWO_IEVAS, "e2", "Like @ieva's post")["anchored"] is False
    assert result(REDDIT_NAMES, "e2", "Upvote u/ieva's comment")["anchored"] is False


# ---- a quote with no structure of its own -------------------------------------------------------
# A reshare whose quoted original is plain rows inside it (a paragraph, a bare link), not an item of
# its own. Its words, author and links mix with the resharer's, so nothing in it tells which of the two
# the request names. Signs of one: two authors shown, a line quoting someone ("@beata: ..."), a reshare
# word in its name with no item inside, links to two posts. Then only the post's own page (its
# permalink, where it is the first post) verifies it.

RESHARE_PARAGRAPH = """signal: ok
http://127.0.0.1:8000/feed · Feed · 2 refs
- main:
  - article "Ieva reshared":
    - link "@ieva" [ref=e20] [cursor=pointer]:
      - /url: /u/ieva
    - paragraph: So true
    - text: "Beata @beata"
    - paragraph: Slow mornings are the best
    - button "Like" [ref=e1] [cursor=pointer]
  - article "Tomas":
    - link "@tomas" [ref=e22] [cursor=pointer]:
      - /url: /u/tomas
    - paragraph: Coffee first
    - button "Like" [ref=e3] [cursor=pointer]
"""

RESHARE_AUTHOR_LINK = """signal: ok
http://127.0.0.1:8000/feed · Feed · 2 refs
- main:
  - article "Ieva":
    - link "@ieva" [ref=e20] [cursor=pointer]:
      - /url: /u/ieva
    - paragraph: So true
    - link "@beata" [ref=e21] [cursor=pointer]:
      - /url: /u/beata
    - paragraph: Slow mornings are the best
    - button "Like" [ref=e1] [cursor=pointer]
  - article "Tomas":
    - link "@tomas" [ref=e22] [cursor=pointer]:
      - /url: /u/tomas
    - paragraph: Coffee first
    - button "Like" [ref=e3] [cursor=pointer]
"""

RESHARE_BARE_LINK = """signal: ok
http://127.0.0.1:8000/feed · Feed · 2 refs
- main:
  - article "Ieva reposted":
    - link "@ieva" [ref=e20] [cursor=pointer]:
      - /url: /u/ieva
    - paragraph: So true
    - link "Slow mornings are the best" [ref=e21] [cursor=pointer]:
      - /url: /p/55
    - button "Like" [ref=e1] [cursor=pointer]
  - article "Tomas":
    - link "@tomas" [ref=e22] [cursor=pointer]:
      - /url: /u/tomas
    - paragraph: Coffee first
    - button "Like" [ref=e3] [cursor=pointer]
"""

RESHARE_TWO_PERMALINKS = """signal: ok
http://127.0.0.1:8000/feed · Feed · 2 refs
- main:
  - article "Ieva":
    - link "@ieva" [ref=e20] [cursor=pointer]:
      - /url: /u/ieva
    - link "3h" [ref=e23] [cursor=pointer]:
      - /url: /p/60
    - paragraph: So true
    - link "http://127.0.0.1:8000/p/55" [ref=e21] [cursor=pointer]:
      - /url: /p/55
    - button "Like" [ref=e1] [cursor=pointer]
  - article "Tomas":
    - link "@tomas" [ref=e22] [cursor=pointer]:
      - /url: /u/tomas
    - paragraph: Coffee first
    - button "Like" [ref=e3] [cursor=pointer]
"""

RESHARE_COLON_LINE = """signal: ok
http://127.0.0.1:8000/feed · Feed · 2 refs
- main:
  - article "Ieva":
    - link "@ieva" [ref=e20] [cursor=pointer]:
      - /url: /u/ieva
    - paragraph: So true
    - paragraph: "@beata: Slow mornings are the best"
    - button "Like" [ref=e1] [cursor=pointer]
  - article "Tomas":
    - link "@tomas" [ref=e22] [cursor=pointer]:
      - /url: /u/tomas
    - paragraph: Coffee first
    - button "Like" [ref=e3] [cursor=pointer]
"""

RESHARE_OWN_PAGE = """signal: ok
http://127.0.0.1:8000/p/60 · Post · 2 refs
- main:
  - article "Ieva reshared":
    - link "@ieva" [ref=e20] [cursor=pointer]:
      - /url: /u/ieva
    - paragraph: So true
    - text: "Beata @beata"
    - paragraph: Slow mornings are the best
    - button "Like" [ref=e1] [cursor=pointer]
  - article "More posts":
    - paragraph: Coffee first
    - button "Like" [ref=e3] [cursor=pointer]
"""

ORIGINAL_OWN_PAGE = """signal: ok
http://127.0.0.1:8000/p/55 · Post · 1 refs
- main:
  - article "Beata":
    - link "@beata" [ref=e21] [cursor=pointer]:
      - /url: /u/beata
    - paragraph: Slow mornings are the best
    - button "Like" [ref=e2] [cursor=pointer]
"""


MIXED = [RESHARE_PARAGRAPH, RESHARE_AUTHOR_LINK, RESHARE_BARE_LINK, RESHARE_TWO_PERMALINKS, RESHARE_COLON_LINE]


@pytest.mark.parametrize("page", MIXED)
@pytest.mark.parametrize("goal", [
    "Like @beata's post",                                    # the quoted original
    "Like the post by @beata",
    'Like the post "Slow mornings are the best"',
    "Like the post about slow mornings",
    "Like http://127.0.0.1:8000/p/55",
    "Like @ieva's post",                                     # the resharer: just as blurred
    'Like @ieva\'s post "So true"',
])
def test_a_quote_with_no_structure_is_unverified_either_way(page: str, goal: str) -> None:
    out = result(page, "e1", goal)
    assert out["anchored"] is False
    assert "no structure of its own" in out["by"] and "permalink" in out["by"]


@pytest.mark.parametrize("page", MIXED)
def test_the_blurred_item_says_why(page: str) -> None:
    out = result(page, "e1", "Like @ieva's post")
    assert out["by"].startswith("the item acted on holds ") and "may be the quoted post" in out["by"]


def test_on_its_own_page_the_reshare_verifies_by_its_permalink() -> None:
    link = "http://127.0.0.1:8000/p/60"
    assert result(RESHARE_OWN_PAGE, "e1", f"Like @ieva's post {link}")["anchored"] is True
    assert result(RESHARE_OWN_PAGE, "e1", "Like @ieva's post", Target(permalink=link))["anchored"] is True
    assert result(RESHARE_OWN_PAGE, "e3", f"Like @ieva's post {link}")["anchored"] is False
    assert result(RESHARE_OWN_PAGE, "e1", "Like @ieva's post")["anchored"] is False   # no permalink


def test_the_original_on_its_own_page_verifies() -> None:
    assert result(ORIGINAL_OWN_PAGE, "e2", "Like @beata's post")["anchored"] is True
    assert result(ORIGINAL_OWN_PAGE, "e2", 'Like the post "Slow mornings are the best"')["anchored"] is True


def test_a_plain_post_with_a_share_link_is_not_blurred() -> None:
    page = RESHARE_PARAGRAPH.replace('article "Tomas"', 'article "Tomas"').replace(
        '    - paragraph: Coffee first\n', '    - paragraph: Coffee first\n    - link "Share" [ref=e30]:\n'
        '      - /url: /p/70/share\n    - link "2h" [ref=e31]:\n      - /url: /p/70\n')
    assert result(page, "e3", "Like @tomas's post")["anchored"] is True


def test_a_comment_under_a_blurred_reshare_cannot_lean_on_it() -> None:
    page = RESHARE_AUTHOR_LINK.replace('    - button "Like" [ref=e1] [cursor=pointer]\n',
                                       '    - button "Like" [ref=e1] [cursor=pointer]\n'
                                       '    - article "Comment by Rasa":\n'
                                       '      - link "@rasa" [ref=e40] [cursor=pointer]:\n'
                                       '        - /url: /u/rasa\n'
                                       '      - paragraph: Lovely\n'
                                       '      - button "Like" [ref=e41] [cursor=pointer]\n')
    assert result(page, "e41", "Like @rasa's comment on @beata's post")["anchored"] is False
    assert result(page, "e41", "Like @rasa's comment")["anchored"] is True


# ---- what two attacks on these rules found: each was a wrong item verified ----------------------

SWEEP_1 = """signal: ok
http://127.0.0.1:8000/p/1 · Post · 4 refs
- main:
  - article "Post by Ieva":
    - link "@ieva" [ref=e20] [cursor=pointer]:
      - /url: /u/ieva
    - paragraph: So pretty
    - button "Like" [ref=e1] [cursor=pointer]
    - article "Comment by Beata":
      - link "@beata" [ref=e21] [cursor=pointer]:
        - /url: /u/beata
      - paragraph: dekui
      - button "Like" [ref=e2] [cursor=pointer]
"""
SWEEP_2 = """signal: ok
http://127.0.0.1:8000/p/1 · Post · 4 refs
- main:
  - article "Post by Ieva":
    - link "@ieva" [ref=e20] [cursor=pointer]:
      - /url: /u/ieva
    - paragraph: Lovely day at the lake
    - button "Like" [ref=e1] [cursor=pointer]
    - article "Comment by Mira":
      - link "@mira" [ref=e22] [cursor=pointer]:
        - /url: /u/mira
      - paragraph: So pretty
      - button "Like" [ref=e2] [cursor=pointer]
"""
SWEEP_3 = """signal: ok
http://127.0.0.1:8000/p/1 · Post · 4 refs
- main:
  - article "Post by Mira":
    - link "@mira" [ref=e20] [cursor=pointer]:
      - /url: /u/mira
    - paragraph: Basil from the garden
    - button "Like" [ref=e1] [cursor=pointer]
    - article "Comment by Ieva":
      - heading "Ieva"
      - paragraph: Looks fresh
      - button "Like" [ref=e2] [cursor=pointer]
"""
SWEEP_4 = """signal: ok
http://127.0.0.1:8000/feed · Feed · 2 refs
- main:
  - article "Post by Ieva":
    - heading "Ieva"
    - paragraph: Lake at dawn
    - button "Like" [ref=e1] [cursor=pointer]
  - article "Post by Tomas":
    - heading "Tomas"
    - paragraph: Coffee first
    - button "Like" [ref=e2] [cursor=pointer]
"""
SWEEP_5 = """signal: ok
http://127.0.0.1:8000/feed · Feed · 2 refs
- main:
  - article "Ieva Petraite":
    - paragraph: Lake at dawn
    - button "Like" [ref=e1] [cursor=pointer]
"""
SWEEP_6 = """signal: ok
http://127.0.0.1:8000/feed · Feed · 2 refs
- main:
  - article "Tom Kairys":
    - paragraph: Lake at dawn
    - button "Like" [ref=e1] [cursor=pointer]
"""
SWEEP_7 = """signal: ok
http://127.0.0.1:8000/feed · Feed · 2 refs
- main:
  - article "Tomas":
    - heading "Tomas"
    - link "Replying to @ieva" [ref=e20] [cursor=pointer]:
      - /url: /u/ieva
    - paragraph: Coffee first
    - button "Like" [ref=e1] [cursor=pointer]
  - article "Mira":
    - link "@mira" [ref=e22] [cursor=pointer]:
      - /url: /u/mira
    - paragraph: Basil
    - button "Like" [ref=e3] [cursor=pointer]
"""
SWEEP_8 = """signal: ok
http://127.0.0.1:8000/feed · Feed · 2 refs
- main:
  - list "Feed":
    - listitem:
      - paragraph: Thanks Ieva for the seeds
      - paragraph: Coffee first
      - button "Like" [ref=e1] [cursor=pointer]
    - listitem:
      - paragraph: Lovely day
      - button "Like" [ref=e2] [cursor=pointer]
"""
SWEEP_9 = """signal: ok
http://127.0.0.1:8000/feed · Feed · 2 refs
- main:
  - paragraph: Who to follow @ieva
  - button "Like" [ref=e1] [cursor=pointer]
"""
SWEEP_10 = """signal: ok
http://127.0.0.1:8000/p/1 · Post · 3 refs
- main:
  - button "Like" [ref=e1] [cursor=pointer]
  - article "Beata":
    - link "@beata" [ref=e21] [cursor=pointer]:
      - /url: /u/beata
    - paragraph: So pretty
    - button "Reply" [ref=e2] [cursor=pointer]
"""
SWEEP_11 = """signal: ok
http://127.0.0.1:8000/p/1 · Post · 3 refs
- main:
  - button "Like" [ref=e1] [cursor=pointer]
  - article "Beata":
    - link "@beata" [ref=e21]:
      - /url: /u/beata
    - paragraph: Read it here
    - link "source" [ref=e22]:
      - /url: /p/55
    - button "Reply" [ref=e2]
"""
SWEEP_12 = """signal: ok
http://127.0.0.1:8000/feed · Feed · 3 refs
- main:
  - article "Tomas":
    - link "@tomas" [ref=e22]:
      - /url: /u/tomas
    - paragraph: Coffee first
    - link "Context" [ref=e23]:
      - /url: /p/55
    - button "Like" [ref=e1] [cursor=pointer]
"""
SWEEP_13 = """signal: ok
http://127.0.0.1:8000/feed · Feed · 2 refs
- main:
  - article "Tomas":
    - heading "Tomas"
    - link "via @ieva" [ref=e20] [cursor=pointer]:
      - /url: https://other.example/@ieva
    - paragraph: Coffee first
    - button "Like" [ref=e1] [cursor=pointer]
"""
SWEEP_14 = """signal: ok
http://127.0.0.1:8000/feed · Feed · 3 refs
- main:
  - article "Post by Jonas":
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
"""
SWEEP_15 = """signal: ok
http://127.0.0.1:8000/p/1 · Post · 4 refs
- main:
  - article "Post by Ieva":
    - link "@ieva" [ref=e20] [cursor=pointer]:
      - /url: /u/ieva
    - paragraph: Lovely day at the lake
    - button "Like" [ref=e1] [cursor=pointer]
    - article "Comment by Mira":
      - link "@mira" [ref=e22] [cursor=pointer]:
        - /url: /u/mira
      - paragraph: Lovely day at the lake
      - button "Like" [ref=e2] [cursor=pointer]
"""
SWEEP_16 = """signal: ok
http://127.0.0.1:8000/feed · Feed · 2 refs
- main:
  - article "Tomas":
    - heading "Tomas"
    - paragraph: Coffee first
    - link "So pretty" [ref=e20] [cursor=pointer]:
      - /url: /p/88
    - button "Like" [ref=e1] [cursor=pointer]
"""
SWEEP_17 = """signal: ok
http://127.0.0.1:8000/feed · Feed · 2 refs
- main:
  - article "Ieva":
    - link "Ieva" [ref=e20] [cursor=pointer]:
      - /url: /ieva
    - paragraph: Lake at dawn
    - button "Like" [ref=e1] [cursor=pointer]
"""
SWEEP_18 = """signal: ok
http://127.0.0.1:8000/feed · Feed · 5 refs
- main:
  - article "Post":
    - heading "Ieva Petraite"
    - paragraph: So true
    - link "@beata" [ref=e21] [cursor=pointer]:
      - /url: /u/beata
    - paragraph: Slow mornings are the best
    - button "Like" [ref=e1] [cursor=pointer]
  - article "Tomas":
    - link "@tomas" [ref=e22] [cursor=pointer]:
      - /url: /u/tomas
    - paragraph: Coffee first
    - button "Like" [ref=e3] [cursor=pointer]
"""
SWEEP_19 = """signal: ok
http://127.0.0.1:8000/feed · Feed · 4 refs
- main:
  - article "Ieva":
    - link "@ieva" [ref=e20] [cursor=pointer]:
      - /url: /u/ieva
    - paragraph: So true
    - link "Slow mornings are the best" [ref=e21] [cursor=pointer]:
      - /url: /p/55
    - button "Like" [ref=e1] [cursor=pointer]
  - article "Tomas":
    - link "@tomas" [ref=e22] [cursor=pointer]:
      - /url: /u/tomas
    - paragraph: Coffee first
    - button "Like" [ref=e3] [cursor=pointer]
"""
SWEEP_20 = """signal: ok
http://127.0.0.1:8000/feed · Feed · 3 refs
- main:
  - article "Post":
    - heading "Beata Kairyte"
    - paragraph: Slow mornings are the best
    - link "Liked by @ieva and 3 others" [ref=e21] [cursor=pointer]:
      - /url: /u/ieva
    - button "Like" [ref=e1] [cursor=pointer]
"""
SWEEP_21 = """signal: ok
http://127.0.0.1:8000/feed · Feed · 3 refs
- main:
  - article "Ieva Petraite":
    - heading "Ieva Petraite"
    - link "Replying to @beata" [ref=e21] [cursor=pointer]:
      - /url: /u/beata
    - paragraph: Slow mornings are the best
    - button "Like" [ref=e1] [cursor=pointer]
"""
SWEEP_22 = """signal: ok
http://127.0.0.1:8000/feed · Feed · 3 refs
- main:
  - article "Post":
    - paragraph: Ieva reposted this
    - link "@beata" [ref=e21] [cursor=pointer]:
      - /url: /u/beata
    - paragraph: Slow mornings are the best
    - button "Like" [ref=e1] [cursor=pointer]
"""
SWEEP_23 = """signal: ok
http://127.0.0.1:8000/feed · Feed · 3 refs
- main:
  - article "Ieva reshares":
    - paragraph: So true
    - link "@beata" [ref=e21] [cursor=pointer]:
      - /url: /u/beata
    - paragraph: Slow mornings are the best
    - button "Like" [ref=e1] [cursor=pointer]
"""
SWEEP_24 = """signal: ok
http://127.0.0.1:8000/feed · Feed · 3 refs
- main:
  - article "Post":
    - heading "Ieva Petraite"
    - paragraph: So true
    - img "Beata Kairyte @beata: Slow mornings are the best"
    - button "Like" [ref=e1] [cursor=pointer]
"""
SWEEP_25 = """signal: ok
http://127.0.0.1:8000/feed · Feed · 4 refs
- main:
  - article "Ieva reposted":
    - link "@ieva" [ref=e20] [cursor=pointer]:
      - /url: /u/ieva
    - paragraph: So true
    - text: "Beata @beata"
    - paragraph: Slow mornings are the best
    - button "Like" [ref=e1] [cursor=pointer]
  - article "Tomas":
    - paragraph: Coffee first
    - button "Like" [ref=e3] [cursor=pointer]
"""


SWEPT = [
    (SWEEP_1, 'Like @beata\'s comment "So pretty"', 'e2', None),  # comment w/o quote, quote in post
    (SWEEP_1, 'Like it', 'e2', Target(author='@beata', text='So pretty')),  # target author+text, quote in post
    (SWEEP_2, 'Like @ieva\'s comment "So pretty"', 'e2', None),  # handle author via post, quote on comment
    (SWEEP_2, 'Like it', 'e2', Target(author='@ieva', text='So pretty')),  # target author via post, quote on comment
    (SWEEP_3, "Like Ieva's comment on @mira's post", 'e2', None),  # name-only comment, handle pins the post
    (SWEEP_3, "Like the comment on @mira's post", 'e2', Target(author='Ieva')),  # target name-only, goal names mira's post
    (SWEEP_4, 'Like the post by "Ieva"', 'e1', None),  # quoted display name as the author
    (SWEEP_4, 'Like the post "Ieva"', 'e1', None),  # quoted display name, no by
    (SWEEP_5, 'Like the post by "Ieva"', 'e1', None),  # short name in quotes, 3 letters
    (SWEEP_6, 'Like the post by "Tom"', 'e1', None),  # 3-letter name quoted, unique word
    (SWEEP_7, "Like @ieva's post", 'e1', None),  # 'Replying to @ieva' link, name-only author
    (SWEEP_8, 'Like Ieva\'s post "Coffee first"', 'e1', None),  # nameless item, mention in text, quote
    (SWEEP_8, "Like Ieva's post about coffee", 'e1', None),  # nameless item, mention in text, no quote
    (SWEEP_9, "Like @ieva's post", 'e1', None),  # Like in no item, @ieva mentioned elsewhere
    (SWEEP_10, 'Like the comment "So pretty"', 'e1', None),  # Like in no item, quote is a line of another item
    (SWEEP_11, 'Like http://127.0.0.1:8000/p/55', 'e1', None),  # Like in no item, permalink in another item
    (SWEEP_12, 'Like http://127.0.0.1:8000/p/55', 'e1', None),  # item merely links to /p/55 as context
    (SWEEP_12, 'Like it', 'e1', Target(author='Ieva', permalink='http://127.0.0.1:8000/p/55')),  # Target permalink matches a link in another item
    (SWEEP_13, "Like @ieva's post", 'e1', None),  # profile link on another host, outside paragraph
    (SWEEP_14, "Like @beata's post", 'e2', None),  # goal asks for Beata's POST, acted is her comment
    (SWEEP_14, "Like @beata's reply", 'e2', None),  # goal asks for Beata's reply, acted is top comment
    (SWEEP_14, 'Like the post by @beata', 'e2', None),  # goal asks for the post by Beata, comment article
    (SWEEP_15, 'Like it', 'e2', Target(author='@ieva', text='Lovely day at the lake')),  # Target author Ieva, quote copied in Mira's comment
    (SWEEP_16, 'Like it', 'e1', Target(text_sha256='150c0c5af7b9bfd344cf743338e484924e07251c4a99570dd8790f4200b6f87a')),  # text_sha256 equal to a link name in the item
    (SWEEP_17, "Like @ieva's post", 'e1', None),  # a one-part link /ieva is not known to be a profile
    (SWEEP_18, "Like @beata's post", 'e1', None),  # name-only resharer, @beata link row
    (SWEEP_18, 'Like the post "Slow mornings are the best"', 'e1', None),  # same, quoted line
    (SWEEP_19, 'Like http://127.0.0.1:8000/p/55', 'e1', None),  # bare link to quoted post
    (SWEEP_20, "Like @ieva's post", 'e1', None),  # 'Liked by @ieva' row on Beata's post
    (SWEEP_21, "Like @beata's post", 'e1', None),  # 'Replying to @beata' row on Ieva's reply
    (SWEEP_22, "Like @beata's post", 'e1', None),  # 'reposted this' paragraph
    (SWEEP_23, "Like @beata's post", 'e1', None),  # title 'Ieva reshares'
    (SWEEP_24, "Like @beata's post", 'e1', None),  # img alt quote with @beata
    (SWEEP_25, 'Like the post about slow mornings at http://127.0.0.1:8000/feed', 'e1', None),  # feed URL named in goal
    (SWEEP_25, "Like Beata's post about slow mornings", 'e1', Target(permalink='http://127.0.0.1:8000/feed')),  # feed URL as target
]


@pytest.mark.parametrize(("page", "goal", "ref", "target"), SWEPT)
def test_wrong_items_the_attacks_found_are_unverified(page: str, goal: str, ref: str, target: Target | None) -> None:
    page_ = parse_page(page)
    out = check(goal, target, page_, page_.by_ref(ref))
    assert out is not None and out["anchored"] is False, out


def test_what_the_attacks_closed_still_verifies_the_right_item() -> None:
    # a quote before the second item word is the acted item's own; after it, the item's around it
    assert result(BOTH_PRETTY, "e3", 'Like @ieva\'s comment "So pretty" on @jonas\'s post')["anchored"] is True
    assert result(BOTH_PRETTY, "e3", 'Like @ieva\'s comment on the post "Lovely day at the lake"')["anchored"] is True
    assert result(BOTH_PRETTY, "e2", 'Like @ieva\'s comment on the post "Lovely day at the lake"')["anchored"] is False
    # a timestamp link is the item's own permalink, a handle before its words is its author
    two = RESHARE_TWO_PERMALINKS.replace('    - link "http://127.0.0.1:8000/p/55" [ref=e21] [cursor=pointer]:\n'
                                         '      - /url: /p/55\n', "")
    assert result(two, "e1", "Like http://127.0.0.1:8000/p/60")["anchored"] is True
    assert result(two, "e3", "Like http://127.0.0.1:8000/p/60")["anchored"] is False
    # a reply in a comment is a reply
    reply = BOTH_PRETTY.replace('      - button "Like" [ref=e3] [cursor=pointer]\n',
                                '      - button "Like" [ref=e3] [cursor=pointer]\n'
                                '      - article "Reply by Rasa":\n'
                                '        - link "@rasa" [ref=e40] [cursor=pointer]:\n'
                                '          - /url: /u/rasa\n'
                                '        - paragraph: Agreed\n'
                                '        - button "Like" [ref=e41] [cursor=pointer]\n')
    assert result(reply, "e41", "Like @rasa's reply")["anchored"] is True
    assert result(reply, "e3", "Like @rasa's reply")["anchored"] is False
