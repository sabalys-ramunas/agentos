"""Mutations are pure and seeded: the same seed gives the same page, the original is untouched,
and each one changes what it says it changes."""

from __future__ import annotations

import re

import pytest

from lanebench import adversarial, mutations
from lanebench.sites.dom import document, find, walk
from lanebench.sites.pages import PAGES
from lanebench.sites.state import Plan, SiteState

ALL = [
    {"kind": "rename_classes"},
    {"kind": "move_node", "node": "follow", "to": "footer"},
    {"kind": "label_to_icon", "node": "like"},
    {"kind": "add_modal"},
    {"kind": "locale", "lang": "lt"},
    {"kind": "lazy_list"},
    {"kind": "shadow_wrap", "node": "actions"},
]


def page(name: str = "feed", seed: int = 1, **plan) -> object:
    return PAGES[name](SiteState.for_plan(Plan(seed=seed, **plan)))


@pytest.mark.parametrize("spec", ALL, ids=[s["kind"] for s in ALL])
def test_each_mutation_is_deterministic_for_a_seed_and_pure(spec):
    tree = page()
    before = document(tree)
    a = document(mutations.apply(tree, [spec], 7))
    b = document(mutations.apply(page(), [spec], 7))
    assert a == b
    assert document(tree) == before  # the input tree was not changed
    assert a != before, f"{spec['kind']} changed nothing on the feed"


def test_the_whole_set_repeats_exactly_and_the_seed_matters():
    one = document(mutations.apply(page(seed=3), ALL, 3))
    assert one == document(mutations.apply(page(seed=3), ALL, 3))
    assert one != document(mutations.apply(page(seed=4), ALL, 4))


def test_rename_classes_renames_every_class_and_the_stylesheet_follows():
    out = mutations.apply(page(), [{"kind": "rename_classes"}], 5)
    classes = {c for n, _ in walk(out) for c in n.classes()}
    assert classes and all(re.fullmatch(r"c[0-9a-f]{7}", c) for c in classes), classes
    css = mutations.rename_css(".like-btn { color: red } .post .btn { x: 1.5em }", 5)
    assert ".like-btn" not in css and "1.5em" in css
    assert mutations.class_map(5, {"like-btn"})["like-btn"] in css
    assert mutations.rename_css(".like-btn{}", 6) != mutations.rename_css(".like-btn{}", 5)


def test_move_node_puts_each_follow_button_in_its_own_posts_footer():
    out = mutations.apply(page(), [{"kind": "move_node", "node": "follow", "to": "footer"}], 1)
    foot = find(out, "footer:lina-trail")
    head = find(out, "head:lina-trail")
    assert any(getattr(c, "lb", "") == "follow:lina-trail" for c in foot.children)
    assert not any(getattr(c, "lb", "") == "follow:lina-trail" for c in head.children)


def test_label_to_icon_keeps_the_words_as_the_accessible_name():
    html = document(mutations.apply(page(), [{"kind": "label_to_icon", "node": "like"}], 1))
    button = re.search(r'<button[^>]*data-lb="like:mira-tomatoes"[^>]*>(.*?)</button>', html)
    assert 'aria-label="Like"' in button.group(0) and "<svg" in button.group(1)
    assert ">Like<" not in button.group(0)


def test_locale_translates_the_page_and_add_modal_waits_a_seeded_moment():
    html = document(mutations.apply(page(), [{"kind": "locale", "lang": "lt"}, {"kind": "add_modal"}], 1))
    assert '<html lang="lt">' in html and ">Patinka<" in html and ">Ne dabar<" in html
    delays = {re.search(r'data-modal-delay="(\d+)"', document(mutations.apply(page(), [{"kind": "add_modal"}], s)))
              .group(1) for s in range(6)}
    assert len(delays) > 1


def test_lazy_list_keeps_the_target_post_out_of_the_first_batch():
    plan = {"mutations": [{"kind": "lazy_list"}]}
    for seed in range(1, 8):
        html = document(mutations.apply(page(seed=seed, **plan), plan["mutations"], seed))
        shown, _, later = html.partition('<template data-lb="lazy-rest"')
        assert 'data-lb="post:mira-tomatoes"' in later and 'data-lb="post:mira-tomatoes"' not in shown


def test_shadow_wrap_moves_the_actions_into_an_open_shadow_root():
    html = document(mutations.apply(page(), [{"kind": "shadow_wrap", "node": "actions"}], 1))
    assert html.count('<template shadowrootmode="open">') == html.count('data-lb="actions:')
    assert re.search(r'<lb-box[^>]*><template shadowrootmode="open"><link[^>]*><div[^>]*data-lb="actions:', html)


def test_unknown_mutations_and_fixtures_are_refused():
    with pytest.raises(ValueError, match="unknown mutation"):
        mutations.apply(page(), [{"kind": "melt"}], 1)
    with pytest.raises(ValueError, match="unknown adversarial"):
        adversarial.fixtures(["nope"])


def test_attacker_hosts_never_resolve():
    for fx in adversarial.FIXTURES.values():
        assert all(h.endswith(".test") for h in fx.hosts), fx
