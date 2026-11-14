"""Snapshot deltas, the per-tab outline memory, and the text matching waits use (no browser)."""

from __future__ import annotations

import gc

from agentos.node.snapshot import (
    DELTA_GONE_RUN,
    OutlineMemory,
    find_text,
    outline_delta,
    outline_texts,
)

FEED = """\
- banner:
  - heading "Feed" [level=1]
- main:
  - article "Post by Ada Lovelace":
    - paragraph: The engine weaves algebraic patterns.
    - button "Like 12" [ref=e12] [cursor=pointer]
    - button "Comments" [ref=e13] [cursor=pointer]
  - article "Post by Charles Babbage":
    - button "Like 5" [ref=e28] [cursor=pointer]
  - button "Load more" [ref=e33] [cursor=pointer]"""


def test_nothing_changed_is_an_empty_delta():
    delta = outline_delta(FEED, FEED)
    assert delta is not None and delta.empty and delta.text == "" and delta.summary() == "nothing changed"


def test_a_changed_button_shows_its_new_ref_and_what_it_was_under_its_post():
    liked = FEED.replace('button "Like 12" [ref=e12]', 'button "Like 13" [pressed] [ref=e40]')
    delta = outline_delta(FEED, liked)
    assert (delta.changed, delta.added, delta.removed) == (1, 0, 0)
    assert delta.text.splitlines() == [
        'in article "Post by Ada Lovelace":',
        '~ button "Like 13" [pressed] [ref=e40] [cursor=pointer] (was: button "Like 12" [ref=e12] [cursor=pointer])',
    ]


def test_new_and_gone_lines_keep_their_nesting_and_context():
    after = FEED.replace('  - button "Load more" [ref=e33] [cursor=pointer]',
                         '  - dialog "Share":\n    - textbox "Message" [ref=e50]\n    - button "Send" [ref=e51]')
    after = after.replace('    - paragraph: The engine weaves algebraic patterns.\n',
                          '    - paragraph: The engine weaves algebraic patterns.\n    - paragraph: Shared\n')
    delta = outline_delta(FEED, after, max_share=1)
    assert delta.summary() == "0 changed, 4 new, 1 gone"
    assert delta.text.splitlines() == [
        'in article "Post by Ada Lovelace":',
        "+ paragraph: Shared",
        "in main:",
        '+ dialog "Share":',
        '+   textbox "Message" [ref=e50]',
        '+   button "Send" [ref=e51]',
        '- button "Load more" [ref=e33] [cursor=pointer]',
    ]


def test_a_rerendered_element_with_a_new_ref_is_a_change():
    rerendered = FEED.replace("[ref=e13]", "[ref=e90]")
    delta = outline_delta(FEED, rerendered)
    assert delta.changed == 1 and "[ref=e90]" in delta.text and "(was: " in delta.text


def test_a_hint_that_came_or_went_is_not_a_change():
    hinted = FEED.replace('button "Like 5" [ref=e28] [cursor=pointer]',
                          'button "Like 5" [ref=e28] [cursor=pointer] (hint: upvote)')
    assert outline_delta(FEED, hinted).empty


def test_focus_moving_is_not_a_change():
    focused = FEED.replace("- main:", "- main [active]:")
    assert outline_delta(focused, FEED).empty
    clicked = FEED.replace('button "Like 12" [ref=e12]', 'button "Like 13" [active] [pressed] [ref=e40]')
    delta = outline_delta(focused, clicked)
    assert delta.changed == 1 and "[active]" in delta.text  # shown on the line that changed anyway


def test_text_changes_pair_with_the_line_they_replace():
    progress = FEED.replace("The engine weaves algebraic patterns.", "Uploading 45%")
    done = FEED.replace("The engine weaves algebraic patterns.", "Uploading 80%")
    delta = outline_delta(progress, done)
    assert delta.changed == 1 and delta.text.endswith("~ paragraph: Uploading 80% (was: paragraph: Uploading 45%)")


def test_too_many_changes_fall_back_to_the_full_outline():
    other = "\n".join(f'- link "Result {i}" [ref=e{100 + i}]' for i in range(12))
    assert outline_delta(FEED, other) is None  # the delta would be longer than most of the page


def _posts(start: int, stop: int) -> str:
    return "\n".join(f'  - article "Post {i}":\n    - paragraph: {"word " * 40}{i}\n'
                     f'    - button "Like {i}" [ref=e{i}] [cursor=pointer]' for i in range(start, stop))


def test_a_scroll_down_a_long_page_is_a_delta_of_what_came_into_view():
    """Measured on a real feed: the outline shows a window of the page, a scroll moves it, and
    the lines that left it were more of the delta than the posts that came in."""
    before = "- main:\n" + _posts(0, 100)
    after = "- main:\n" + _posts(30, 130)
    delta = outline_delta(before, after)
    assert delta is not None and delta.added == 90 and delta.removed == 90
    assert "- ... 90 lines left the outline" in delta.text
    assert 'button "Like 129" [ref=e129]' in delta.text
    assert 'button "Like 3"' not in delta.text


def test_a_short_run_of_gone_lines_is_still_shown():
    lines = FEED.splitlines()
    gone = "\n".join(f'  - link "Tag {i}" [ref=e{200 + i}]' for i in range(DELTA_GONE_RUN))
    delta = outline_delta("\n".join([*lines, gone]), FEED, max_share=10)
    assert delta.removed == DELTA_GONE_RUN and 'link "Tag 0"' in delta.text and "left the outline" not in delta.text
    more = gone + f'\n  - link "Tag last" [ref=e{200 + DELTA_GONE_RUN}]'
    delta = outline_delta("\n".join([*lines, more]), FEED, max_share=10)
    assert delta.removed == DELTA_GONE_RUN + 1 and 'link "Tag 0"' not in delta.text
    assert f"- ... {DELTA_GONE_RUN + 1} lines left the outline" in delta.text


def test_long_lines_are_cut_in_a_delta():
    long = FEED.replace("The engine weaves algebraic patterns.", "word " * 300)
    delta = outline_delta(FEED, long, max_share=1)
    assert all(len(line) < 900 for line in delta.text.splitlines())


def test_a_delta_from_nothing_is_everything_new():
    assert outline_delta("", FEED) is None  # all new: the full outline is the answer
    small = '- button "OK" [ref=e1]'
    assert outline_delta("", small, max_share=10).added == 1


# ---- memory -------------------------------------------------------------------------------------

class Page:
    pass


def test_memory_is_per_tab_and_per_agent():
    memory = OutlineMemory()
    a, b = Page(), Page()
    memory.remember(a, "agent-1", "https://x.example/1", "one")
    memory.remember(b, "agent-1", "https://x.example/2", "two")
    memory.remember(a, "agent-2", "https://x.example/1", "three")
    assert memory.get(a, "agent-1").text == "one" and memory.get(b, "agent-1").text == "two"
    assert memory.get(a, "agent-2").text == "three" and memory.get(b, "agent-2") is None
    memory.remember(a, "agent-1", "https://x.example/1b", "four")
    assert memory.get(a, "agent-1").url == "https://x.example/1b"
    memory.forget(a)
    assert memory.get(a, "agent-1") is None


def test_memory_keeps_a_few_agents_per_tab_and_forgets_closed_tabs():
    memory = OutlineMemory(per_page=2)
    page = Page()
    for agent in ("a", "b", "c"):
        memory.remember(page, agent, "u", agent)
    assert memory.get(page, "a") is None and memory.get(page, "c").text == "c"
    memory.get(page, "b")
    del page
    gc.collect()
    assert len(memory._seen) == 0


def test_memory_ignores_pages_it_cannot_reference_weakly():
    memory = OutlineMemory()
    memory.remember(3, "a", "u", "t")  # ints cannot be weakly referenced
    assert memory.get(3, "a") is None


# ---- text in the tree ---------------------------------------------------------------------------

RAW = """\
- main [ref=e1] [box=0,0,800,600]:
  - heading "Create post" [level=2] [ref=e2] [box=1,2,3,4]
  - paragraph [ref=e3] [box=1,2,3,4]: Uploading 45%
  - textbox "Password" [ref=e4] [box=1,2,3,4]: hunter2
  - combobox "Audience" [ref=e5] [box=1,2,3,4]: Friends only
  - text: Your post   was  shared
  - link "Help" [ref=e6] [cursor=pointer] [box=1,2,3,4]:
    - /url: /help-secret-path
  - button "Post" [ref=e7] [cursor=pointer] [box=1,2,3,4]"""


def test_names_and_texts_are_matched_case_and_spacing_folded():
    texts = outline_texts(RAW)
    assert find_text(texts, "uploading") == "paragraph: Uploading 45%"
    assert find_text(texts, "YOUR POST WAS SHARED") == "text: Your post   was  shared"
    assert find_text(texts, "create") == 'heading "Create post" [level=2]'
    assert find_text(texts, "help") == 'link "Help" [ref=e6] [cursor=pointer]'  # no trailing colon
    assert find_text(texts, "Post", exact=True) == 'button "Post" [ref=e7] [cursor=pointer]'
    assert find_text(texts, "Posts", exact=True) is None


def test_field_values_and_link_targets_are_never_matched_or_shown():
    texts = outline_texts(RAW)
    assert find_text(texts, "hunter2") is None
    assert find_text(texts, "Friends only") is None
    assert find_text(texts, "help-secret-path") is None
    assert find_text(texts, "Password") == 'textbox "Password" [ref=e4]'
    assert all("hunter2" not in shown and "box=" not in shown for _, shown in texts)
