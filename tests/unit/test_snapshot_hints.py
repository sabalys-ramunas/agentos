"""Hints for unnamed controls: formatting, source order, limits, and the reads that feed them.

The page-side read (`_FACTS_JS`) runs in a browser and is covered by tests/node/test_snapshot_hints_node.py.
Here the facts are plain dicts, the same shape that read returns.
"""

import asyncio
import time

import pytest

from agentos.node.snapshot import (
    HINT_LIMIT,
    build_outline,
    collect_facts,
    hint_from_facts,
    parse_line,
    render_line,
    take_snapshot,
)

# ---- source order ---------------------------------------------------------------------------

@pytest.mark.parametrize("facts, expected", [
    ({"shadow_label": "upvote", "title": "Share", "svg_title": "Save", "icons": {"data-icon": "reply"},
      "flags": ["downvote"], "classes": ["open-menu"], "hidden": "Hide post"}, "upvote"),
    ({"title": "Share", "svg_title": "Save", "icons": {"data-icon": "reply"},
      "flags": ["downvote"], "classes": ["open-menu"], "hidden": "Hide post"}, "share"),
    ({"svg_title": "Save post", "icons": {"data-icon": "reply"},
      "flags": ["downvote"], "classes": ["open-menu"], "hidden": "Hide post"}, "save post"),
    ({"icons": {"data-icon": "reply"}, "classes": ["open-menu"], "hidden": "Hide post"}, "reply"),
    ({"flags": ["downvote"], "classes": ["open-menu"], "hidden": "Hide post"}, "downvote"),
    ({"classes": ["open-menu"], "hidden": "Hide post"}, "open menu"),
    ({"hidden": "Hide post"}, "hide post"),
])
def test_the_first_source_with_a_value_gives_the_hint(facts, expected):
    assert hint_from_facts(facts) == expected


def test_icon_attributes_are_read_in_their_order():
    assert hint_from_facts({"icons": {"data-testid": "post vote", "data-action": "share"}}) == "post vote / share"
    assert hint_from_facts({"icons": {"name": "reply", "data-icon": "upvote"}}) == "upvote / reply"


def test_a_boolean_attribute_names_a_web_component_control():
    assert hint_from_facts({"flags": ["upvote"]}) == "upvote"
    assert hint_from_facts({"flags": ["downvote"]}) == "downvote"


# ---- de-duplication ---------------------------------------------------------------------------

@pytest.mark.parametrize("facts, expected", [
    ({"title": "Upvote", "icons": {"data-icon": "upvote"}}, "upvote"),
    ({"icons": {"icon-name": "upvote-outline"}, "flags": ["upvote"]}, "upvote"),
    ({"icons": {"data-testid": "post-upvote-button"}, "flags": ["upvote"]}, "upvote"),
    ({"icons": {"icon-name": "reply", "data-icon": "reply"}}, "reply"),
])
def test_a_repeated_word_is_not_said_twice(facts, expected):
    assert hint_from_facts(facts) == expected


# ---- normalization ----------------------------------------------------------------------------

@pytest.mark.parametrize("title, expected", [
    ("upVote", "up vote"),
    ("post_upvote.button", "post upvote button"),
    ('[ref=e1] "Share" (now)!', "ref e1 share now"),
    ("Pažymėti", "pažymėti"),
    ("bookmark", "bookmark"),
])
def test_labels_become_lowercase_words(title, expected):
    assert hint_from_facts({"title": title}) == expected


def test_a_hint_never_carries_brackets_quotes_or_parentheses():
    hint = hint_from_facts({"title": '][ "x" ( ) [ref=f1e2] ' + "'"})
    assert hint is not None
    assert not any(c in hint for c in '[]()"\'')


@pytest.mark.parametrize("facts", [
    {"title": "12345"},             # numbers are not a label
    {"title": "   "},
    {"title": "x" * 25},            # a long word is an id or a token
    {"title": "k3j8f2m9x0q7w1e5"},  # so is a letters-and-digits mix
    {"classes": ["css-1x2y3z", "btn-primary"]},  # hashes and utilities name no action
    {"classes": ["icon-button", "btn", "primary"]},
    {"classes": ["post-actions"]},  # a container, not an action
    {},
    None,
])
def test_no_label_means_no_hint(facts):
    assert hint_from_facts(facts) is None


def test_secret_looking_words_are_dropped_from_a_label():
    assert hint_from_facts({"title": "share k3j8f2m9x0q7w1e5"}) == "share"


def test_a_trailing_style_word_is_dropped_but_a_lone_one_is_kept():
    assert hint_from_facts({"icons": {"icon-name": "bookmark-filled"}}) == "bookmark"
    assert hint_from_facts({"icons": {"icon-name": "outline"}}) == "outline"


def test_class_names_only_count_when_they_name_an_action():
    assert hint_from_facts({"classes": ["open-menu"]}) == "open menu"
    assert hint_from_facts({"classes": ["vote-up"]}) == "vote up"
    assert hint_from_facts({"classes": ["css-1x2y3z", "menu"]}) == "menu"


# ---- length -----------------------------------------------------------------------------------

def test_a_long_hint_is_cut_at_a_word_boundary():
    hint = hint_from_facts({"title": "Share this post with everyone you know on every platform today"})
    assert hint == "share this post with everyone you know"
    assert len(hint) <= 40


def test_a_hint_keeps_only_the_whole_words_that_fit():
    assert hint_from_facts({"title": "a" * 24 + " " + "b" * 24}) == "a" * 24


def test_a_very_long_label_stays_within_forty_characters():
    hint = hint_from_facts({"title": " ".join(["word"] * 50)})
    assert hint is not None and len(hint) <= 40
    assert hint.endswith("word") and not hint.endswith(" ")


# ---- passwords --------------------------------------------------------------------------------

def test_a_password_gets_no_hint_even_with_labels():
    facts = {"input_type": "password", "title": "Password", "icons": {"data-icon": "reveal"},
             "flags": ["secret"], "hidden": "Show password"}
    assert hint_from_facts(facts) is None


def test_other_inputs_are_hinted_normally():
    assert hint_from_facts({"input_type": "text", "title": "Name"}) == "name"


# ---- rendering --------------------------------------------------------------------------------

def test_the_hint_goes_after_the_attributes_and_before_any_value():
    line = parse_line('- button [ref=f6e268] [cursor=pointer] [box=1,2,3,4]')
    assert render_line(line, "upvote") == "- button [ref=f6e268] [cursor=pointer] (hint: upvote)"
    text = parse_line('- button [ref=e1]: Hide post')
    assert render_line(text, "hide post") == "- button [ref=e1] (hint: hide post): Hide post"


def test_a_text_box_value_can_be_left_out():
    line = parse_line('- textbox "Pin" [ref=e29]: hunter2')
    assert render_line(line, value=False) == '- textbox "Pin" [ref=e29]'
    assert render_line(line) == '- textbox "Pin" [ref=e29]: hunter2'


REDDIT = """\
- group "Actions available for this post" [ref=e8] [box=239,126,760,43]:
  - button [ref=e9] [cursor=pointer] [box=239,130,28,35]
  - text: 6.2K
  - button [ref=e10] [cursor=pointer] [box=312,130,28,35]
  - button "Share" [ref=e11] [cursor=pointer] [box=350,130,28,35]
"""


def test_the_vote_buttons_are_told_apart_and_named_controls_are_left_alone():
    facts = {"e9": {"input_type": "", "flags": ["upvote"]}, "e10": {"flags": ["downvote"]},
             "e11": {"title": "Ignored, the button has a name"}}
    text, refs, truncated = build_outline(REDDIT, facts=facts)
    assert text.splitlines() == [
        '- group "Actions available for this post":',
        '  - button [ref=e9] [cursor=pointer] (hint: upvote)',
        '  - text: 6.2K',
        '  - button [ref=e10] [cursor=pointer] (hint: downvote)',
        '  - button "Share" [ref=e11] [cursor=pointer]',
    ]
    assert refs == 3 and not truncated


TEXTBOXES = """\
- textbox "Pin" [ref=e29]: hunter2
- textbox "Name" [ref=e30]: Ada
- textbox [ref=e31]: hunter3
"""


def test_text_box_values_show_only_when_known_not_to_be_a_password():
    text, _, _ = build_outline(TEXTBOXES)
    assert text == '- textbox "Pin" [ref=e29]\n- textbox "Name" [ref=e30]\n- textbox [ref=e31]'

    facts = {"e29": {"input_type": "password"}, "e30": {"input_type": "text"}}
    text, _, _ = build_outline(TEXTBOXES, facts=facts)
    assert text == '- textbox "Pin" [ref=e29]\n- textbox "Name" [ref=e30]: Ada\n- textbox [ref=e31]'
    assert "hunter" not in text


def test_hints_count_toward_the_size_limit():
    lines = ["- generic [ref=e1] [box=0,0,800,20000]:"]
    lines += [f'  - button [ref=e{100 + i}] [cursor=pointer] [box=0,{i * 100},80,30]' for i in range(200)]
    raw = "\n".join(lines)
    facts = {f"e{100 + i}": {"title": "Long label that keeps going for a while"} for i in range(200)}

    plain, _, truncated = build_outline(raw, viewport_height=900, max_chars=3000)
    hinted, _, hinted_truncated = build_outline(raw, viewport_height=900, max_chars=3000, facts=facts)

    assert truncated and hinted_truncated
    assert len(hinted) <= 3000 + 200
    assert hinted.count("(hint: long label that keeps going for a while)") > 0
    assert hinted.count("\n") <= plain.count("\n")  # a longer line leaves room for fewer lines


# ---- the reads: fake page, no browser ---------------------------------------------------------

class FakeLocator:
    def __init__(self, page: "FakePage", selector: str):
        self.page = page
        self.ref = selector.removeprefix("aria-ref=")

    async def evaluate(self, expression, arg=None, timeout=None):
        self.page.read_refs.append(self.ref)
        outcome = self.page.outcomes.get(self.ref)
        if outcome is None:
            raise RuntimeError("no element for this ref")
        if isinstance(outcome, float):
            await asyncio.sleep(outcome)
            return {}
        return outcome


class FakePage:
    url = "http://127.0.0.1:6181/hints.html"

    def __init__(self, raw: str, outcomes: dict):
        self.raw = raw
        self.outcomes = outcomes
        self.read_refs: list[str] = []

    async def aria_snapshot(self, **kwargs):
        return self.raw

    async def evaluate(self, expression, arg=None):
        return 900  # window.innerHeight

    async def title(self):
        return "Fixture hints"

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)


def _buttons(count: int) -> str:
    return "\n".join(f"- button [ref=e{100 + i}] [cursor=pointer]" for i in range(count))


async def test_a_snapshot_reads_each_unnamed_control_once_and_stops_at_the_cap():
    raw = _buttons(HINT_LIMIT + 40)
    outcomes = {f"e{100 + i}": {"icons": {"data-icon": f"item {i}"}} for i in range(HINT_LIMIT + 40)}
    page = FakePage(raw, outcomes)

    snap = await take_snapshot(page)

    assert len(page.read_refs) == HINT_LIMIT
    assert len(set(page.read_refs)) == HINT_LIMIT
    assert snap.text.count("(hint: item ") == HINT_LIMIT
    assert "- button [ref=e100] [cursor=pointer] (hint: item 0)" in snap.text.splitlines()


async def test_a_snapshot_reads_nothing_when_nothing_needs_a_hint():
    raw = '- button "Submit" [ref=e1] [cursor=pointer]\n- heading "Feed" [level=1] [ref=e2]'
    page = FakePage(raw, {})
    snap = await take_snapshot(page)
    assert page.read_refs == []
    assert "(hint:" not in snap.text


async def test_a_text_box_value_appears_only_after_its_read_says_it_is_not_a_password():
    raw = TEXTBOXES
    page = FakePage(raw, {"e29": {"input_type": "password"}, "e30": {"input_type": "text"}})

    snap = await take_snapshot(page)

    assert '- textbox "Name" [ref=e30]: Ada' in snap.text
    assert "hunter" not in snap.text  # e31 could not be read, so its value stays hidden


async def test_a_failed_read_costs_only_its_hint():
    raw = "- button [ref=e1] [cursor=pointer]\n- button [ref=e2] [cursor=pointer]"
    page = FakePage(raw, {"e2": {"flags": ["upvote"]}})  # e1 has no element: its read raises

    snap = await take_snapshot(page)

    assert snap.text.splitlines() == ["- button [ref=e1] [cursor=pointer]",
                                      "- button [ref=e2] [cursor=pointer] (hint: upvote)"]


async def test_reads_past_the_budget_are_cancelled_not_awaited():
    page = FakePage("", {"e1": {"flags": ["upvote"]}, "e2": 5.0})  # e2 would take five seconds

    started = time.monotonic()
    facts = await collect_facts(page, ["e1", "e2"], budget_ms=100)
    elapsed = time.monotonic() - started

    assert facts == {"e1": {"flags": ["upvote"]}}
    assert elapsed < 1.0
    await asyncio.sleep(0)  # let the cancelled read finish its cancellation


def test_design_system_and_state_flags_are_not_hints():
    """Live Reddit: vote buttons carry `upvote rpl actioned leading`; only the action is a hint."""
    assert hint_from_facts({"flags": ["upvote", "rpl", "actioned", "leading"]}) == "upvote"
    assert hint_from_facts({"flags": ["rpl", "actioned"]}) is None
    assert hint_from_facts({"icons": {"icon-name": "chat"}, "flags": ["rpl"]}) == "chat"
