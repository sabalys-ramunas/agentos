"""Locator parity: the same pages and locators, the same expected results, for the reference
FakeDriver (here, on the Mac) and PlaywrightFlowDriver on a real page (tests/node, which loads
this module by path and renders each page as HTML).

A page is a tree of `N` nodes. Each renders to a fakes.El and to a `<div role=... aria-label=...>`
(or a custom element, `tag`, whose content may sit in an open shadow root, `shadow`), so role,
accessible name, own text, attributes and CSS classes are the same on both sides.
Cases marked `fake_diverges` would be where runner.fakes.own_text does not follow its own
docstring. There are none since own_text adopted the `fixed_own_text` logic below (text inside a
same-role element nested through a wrapper of another role is excluded too, as the real driver does).
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import Any

import pytest

from openajar.core.driver import AmbiguousLocator, ElementNotFound
from openajar.core.locator import Locator
from openajar.runner import fakes
from openajar.runner.fakes import El, FakeDriver, FakeSite
from openajar.runner.models import ExtractField

# ---------------------------------------------------------------- pages


@dataclass
class N:
    role: str
    name: str = ""
    text: str = ""
    kids: list[N] = field(default_factory=list)
    key: str = ""
    css: str = ""  # one class name; the CSS fallback in a case is ".<css>"
    attrs: dict[str, str] = field(default_factory=dict)
    tag: str = ""  # a custom element ("x-comment") instead of a div
    shadow: bool = False  # text and children inside an open shadow root (transparent to the fake)


def n(role: str, name: str = "", text: str = "", *kids: N, key: str = "", css: str = "", tag: str = "",
      shadow: bool = False, **attrs: str) -> N:
    return N(role, name, text, list(kids), key, css, {k.replace("_", "-"): v for k, v in attrs.items()}, tag, shadow)


def _keyed(nodes: list[N], prefix: str = "n") -> list[N]:
    for i, node in enumerate(nodes):
        node.key = node.key or f"{prefix}{i}"
        _keyed(node.kids, node.key + ".")
    return nodes


def to_el(node: N) -> El:
    return El(node.role, node.name, text=node.text, attrs={**node.attrs, "data-pk": node.key},
              children=[to_el(k) for k in node.kids], key=node.key,
              css=(f".{node.css}",) if node.css else ())


# Fixture-only: move each <template data-shadow> into an open shadow root on its parent.
SHADOW_JS = """<script>(function attach(root) {
  for (const t of root.querySelectorAll('template[data-shadow]')) {
    const host = t.parentNode; const sr = host.attachShadow({mode: 'open'});
    sr.append(t.content.cloneNode(true)); t.remove(); attach(sr);
  }
})(document);</script>"""


def to_html(nodes: list[N]) -> str:
    def one(node: N) -> str:
        tag = node.tag or "div"
        a = f' role="{node.role}"'
        if node.name:
            a += f' aria-label="{html.escape(node.name)}"'
        if node.css:
            a += f' class="{node.css}"'
        for k, v in {**node.attrs, "data-pk": node.key}.items():
            a += f' {k}="{html.escape(v)}"'
        if node.tag:
            a += ' style="display:block"'  # custom elements are inline by default; divs are blocks
        inner = f"{html.escape(node.text)}{''.join(one(k) for k in node.kids)}"
        if node.shadow:
            inner = f"<template data-shadow>{inner}</template>"
        return f"<{tag}{a}>{inner}</{tag}>"

    body = "".join(one(node) for node in nodes)
    script = SHADOW_JS if "data-shadow" in body else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>parity</title></head>'
            f"<body>{body}{script}</body></html>")


def like(key: str, *, pressed: str = "false", css: str = "") -> N:
    return n("button", "Like", "Like", key=key, css=css, aria_pressed=pressed)


# The runner's social_site post: comments nested directly in the post.
SOCIAL = _keyed([
    n("main", "", "",
      n("article", "Post by Ieva", "Lovely day at the lake",
        like("like-post", css="like-post"),
        n("article", "Comment by Beata", "So pretty", like("like-c1", pressed="true"), key="c1"),
        n("article", "Comment by Ieva", "dekui", like("like-c2"), key="c2"),
        key="post"),
      key="main"),
])

# A feed with list wrappers, nested replies, a duplicate anchor name and DOM-order traps.
WRAPPED = _keyed([
    n("main", "", "",
      n("article", "Post 1", "Hello world",
        like("like-p1"),
        n("list", "Comments", "",
          n("listitem", "", "",
            n("article", "Comment by Ana", "Secret comment",
              like("like-p1c1"),
              n("list", "Replies", "",
                n("listitem", "", "",
                  n("article", "Reply by Bo", "Deep reply", like("like-p1c1r1"), key="p1c1r1"),
                  key="p1c1li"),
                key="p1c1replies"),
              key="p1c1"),
            key="p1li"),
          key="p1comments"),
        key="p1"),
      n("article", "Post 2", "Second",
        n("article", "Reply by Bo", "Other", like("like-p2c1"), key="p2c1"),
        like("like-p2"),
        key="p2"),
      n("article", "Empty post", "No likes here",
        n("article", "Comment by Cy", "x", like("like-p3c1"), key="p3c1"),
        n("button", "Share", "Share", key="share-p3", css="share"),
        key="p3"),
      n("status", "Posts", "3 posts", key="status"),
      n("textbox", "Caption", "", key="caption", value="draft text"),
      key="main"),
])

# Machine attributes on custom elements, a post and a comment rendered in shadow roots, a reply
# nested in the comment, links by href, and a button named per account language.
CUSTOM = _keyed([
    n("main", "", "",
      n("article", "Post by Ana", "Our first post",
        n("button", "Like", "Like", key="like-p1", data_action="like"),
        n("link", "Open post", "Open", key="open-p1", href="https://social.example/p/1"),
        n("article", "Comment by Bo", "",
          n("generic", "", "", key="time-a", css="stamp", datetime="2026-11-04T08:12:00Z"),
          n("paragraph", "", "First comment", key="body-a"),
          n("link", "Permalink", "Permalink", key="perm-a", href="/p/1/c/t1_a"),
          n("button", "Like", "Like", key="like-a", data_action="like"),
          n("article", "Reply by Cy", "",
            n("paragraph", "", "A reply", key="body-b"),
            n("link", "Permalink", "Permalink", key="perm-b", href="https://evil.example/x"),
            key="c-b", tag="x-comment", thingid="t1_b", author="cy"),
          key="c-a", tag="x-comment", shadow=True, thingid="t1_a", author="bo"),
        key="p1", tag="x-post", shadow=True, thingid="t3_p1"),
      n("article", "Post by Dee", "Second post",
        n("link", "Open post", "Open", key="open-p2", href="https://social.example/p/2"),
        n("button", "Patinka", "Patinka", key="like-p2", data_action="like"),
        key="p2", tag="x-post", thingid="t3_p2"),
      n("button", "Add photo", "Add photo", key="file-btn", type="file"),
      n("button", "Add link", "Add link", key="link-btn", type="url"),
      key="main"),
])

PAGES = {"social": SOCIAL, "wrapped": WRAPPED, "custom": CUSTOM}


def L(**kw: Any) -> Locator:  # reads like the JSON a flow holds
    return Locator.model_validate(kw)


POST1 = {"role": "article", "name": "Post 1"}
EMPTY = {"role": "article", "name": "Empty post"}

# (id, page, locator, expected subset of observe(), fake_diverges)
CASES: list[tuple[str, str, Locator, dict[str, Any], bool]] = [
    ("page-level likes are ambiguous", "social", L(role="button", name="Like"),
     {"find": "ambiguous", "count": 3, "found": False, "ambiguous": True}, False),
    ("post anchor keeps only the post's own like", "social",
     L(role="button", name="Like", within={"role": "article", "name": "Post by Ieva"}),
     {"find": "ok", "count": 1, "key": "like-post", "attrs": {"aria-pressed": "false"}}, False),
    ("own false reaches into comments", "social",
     L(role="button", name="Like", within={"role": "article", "name": "Post by Ieva"}, own=False),
     {"find": "ambiguous", "count": 3, "ambiguous": True}, False),
    ("comment anchor", "social",
     L(role="button", name="Like", within={"role": "article", "name": "Comment by Beata"}),
     {"find": "ok", "count": 1, "key": "like-c1", "attrs": {"aria-pressed": "true"}}, False),
    ("articles in main count comments too", "social", L(role="article", within={"role": "main"}),
     {"find": "ambiguous", "count": 3, "ambiguous": True}, False),
    ("post text excludes its comments", "social", L(role="article", name="Post by Ieva"),
     {"find": "ok", "key": "post", "text": "Lovely day at the lake Like"}, False),
    ("nth picks among page matches", "social", L(role="button", name="Like", nth=2),
     {"find": "ok", "count": 3, "key": "like-c2"}, False),
    ("nth out of range", "social", L(role="button", name="Like", nth=5),
     {"find": "not_found", "count": 3, "found": False, "ambiguous": False}, False),
    ("contains", "social", L(role="button", name="ik", match="contains"),
     {"count": 3}, False),
    ("contains is case sensitive", "social", L(role="button", name="like", match="contains"),
     {"find": "not_found", "count": 0}, False),
    ("regex", "social", L(role="article", name="^Comment by", match="regex"),
     {"find": "ambiguous", "count": 2}, False),
    ("exact normalizes whitespace", "social", L(role="article", name="  Post   by Ieva "),
     {"find": "ok", "key": "post"}, False),
    ("exact is whole string", "social", L(role="article", name="Post by"),
     {"find": "not_found", "count": 0}, False),
    ("css fallback when no role match", "social", L(role="button", name="Nope", css=".like-post"),
     {"find": "ok", "count": 1, "key": "like-post"}, False),
    ("missing anchor", "social", L(role="button", within={"role": "article", "name": "Nobody"}),
     {"find": "not_found", "count": 0, "found": False, "ambiguous": False}, False),
    ("ambiguous anchor", "social", L(role="button", name="Like", within={"role": "article"}),
     {"find": "ambiguous", "count": 0, "found": False, "ambiguous": True}, False),
    ("absent role", "social", L(role="dialog"), {"find": "not_found", "count": 0}, False),

    ("own excludes comments nested through wrappers", "wrapped", L(role="button", name="Like", within=POST1),
     {"find": "ok", "count": 1, "key": "like-p1"}, False),
    ("own first like in DOM order is a comment's", "wrapped",
     L(role="button", name="Like", within={"role": "article", "name": "Post 2"}, nth=0),
     {"find": "ok", "count": 1, "key": "like-p2"}, False),
    ("own false includes the comment first", "wrapped",
     L(role="button", name="Like", within={"role": "article", "name": "Post 2"}, own=False, nth=0),
     {"find": "ok", "count": 2, "key": "like-p2c1"}, False),
    ("duplicate anchor name is ambiguous", "wrapped",
     L(role="button", name="Like", within={"role": "article", "name": "Reply by Bo"}),
     {"find": "ambiguous", "count": 0, "ambiguous": True}, False),
    ("nested anchor with own drops a reply inside a comment", "wrapped",
     L(role="button", name="Like", within={"role": "article", "name": "Reply by Bo", "within": POST1}),
     {"find": "not_found", "count": 0}, False),
    ("nested anchor without own reaches the reply", "wrapped",
     L(role="button", name="Like", within={"role": "article", "name": "Reply by Bo", "within": POST1, "own": False}),
     {"find": "ok", "count": 1, "key": "like-p1c1r1"}, False),
    ("css fallback is skipped when role matches exist (even if own drops them)", "wrapped",
     L(role="button", name="Like", within=EMPTY, css=".share"),
     {"find": "not_found", "count": 0}, False),
    ("css only", "wrapped", L(css=".share"), {"find": "ok", "count": 1, "key": "share-p3"}, False),
    ("css only within an anchor", "wrapped", L(css=".share", within=EMPTY),
     {"find": "ok", "count": 1, "key": "share-p3"}, False),
    ("status text", "wrapped", L(role="status", name="Posts"), {"find": "ok", "text": "3 posts"}, False),
    ("value attribute", "wrapped", L(role="textbox", name="Caption"), {"find": "ok", "value": "draft text"}, False),
    ("regex alternation", "wrapped", L(role="article", name="^(Comment|Reply) by", match="regex"),
     {"count": 4}, False),
    ("all articles in main", "wrapped", L(role="article", within={"role": "main"}), {"count": 7}, False),
    ("second post by regex and nth", "wrapped", L(role="article", name="^Post", match="regex", nth=1),
     {"find": "ok", "key": "p2"}, False),
    ("contains counts every like", "wrapped", L(role="button", name="ik", match="contains"), {"count": 6}, False),
    ("button text", "wrapped", L(role="button", name="Like", within=POST1), {"text": "Like"}, False),
    ("post text excludes comments nested through a list", "wrapped", L(**POST1),
     {"find": "ok", "key": "p1", "text": "Hello world Like"}, False),
    ("list text excludes the nested list", "wrapped", L(role="list", name="Comments"),
     {"find": "ok", "key": "p1comments", "text": "Secret comment Like"}, False),

    ("attr equals narrows a role", "custom", L(role="button", attr={"name": "type", "equals": "file"}),
     {"find": "ok", "count": 1, "key": "file-btn", "attrs": {"type": "file"}}, False),
    ("attr prefix counts both post links", "custom",
     L(role="link", attr={"name": "href", "prefix": "https://social.example/p/"}),
     {"find": "ambiguous", "count": 2, "ambiguous": True}, False),
    ("attr regex picks one link", "custom", L(role="link", attr={"name": "href", "regex": "/p/2$"}),
     {"find": "ok", "count": 1, "key": "open-p2"}, False),
    ("attr alone finds a custom element", "custom", L(attr={"name": "thingid", "equals": "t1_b"}),
     {"find": "ok", "count": 1, "key": "c-b", "attrs": {"author": "cy"}}, False),
    ("attr alone counts every element carrying it, shadow roots included", "custom",
     L(attr={"name": "thingid", "prefix": "t"}), {"find": "ambiguous", "count": 4, "ambiguous": True}, False),
    ("a missing attribute never matches", "custom", L(role="button", attr={"name": "data-missing", "prefix": ""}),
     {"find": "not_found", "count": 0}, False),
    ("inside a shadow root by role within an attr anchor", "custom",
     L(role="button", name="Like", within={"role": "article", "attr": {"name": "thingid", "equals": "t1_a"}}),
     {"find": "ok", "count": 1, "key": "like-a"}, False),
    ("a shadow post keeps only its own like", "custom",
     L(role="button", name="Like", within={"role": "article", "attr": {"name": "thingid", "equals": "t3_p1"}}),
     {"find": "ok", "count": 1, "key": "like-p1"}, False),
    ("own text of a shadow host excludes its nested comment", "custom",
     L(role="article", attr={"name": "thingid", "equals": "t3_p1"}),
     {"find": "ok", "key": "p1", "text": "Our first post Like Open"}, False),
    ("attr applies to the css fallback", "custom",
     L(role="button", name="Nope", css=".stamp", attr={"name": "datetime", "prefix": "2026-11-04"}),
     {"find": "ok", "count": 1, "key": "time-a"}, False),
    ("names render to the account language", "custom",
     L(role="button", names={"en": "Like", "lt": "Patinka"}).render({"account_language": "lt"}),
     {"find": "ok", "count": 1, "key": "like-p2"}, False),
    ("attr regex on a data attribute under an anchor", "custom",
     L(role="button", attr={"name": "data-action", "regex": "^like$"}, within={"role": "main"}),
     {"find": "ambiguous", "count": 3}, False),
    ("unrendered names never match anything", "custom", L(role="button", names={"en": "Like"}),
     {"find": "not_found", "count": 0}, False),
]

COMMENTS = {"role": "article", "attr": {"name": "thingid", "prefix": "t1_"}, "within": {"role": "main"}}

# (id, page, items, fields, max_items, expected rows, expected total)
EXTRACT_CASES: list[tuple[str, str, Locator, dict[str, Any], int, list[dict[str, Any]], int]] = [
    ("comments with own fields under each item", "custom", L(**COMMENTS), {
        "id": {"attr": "thingid"},
        "body": {"locator": {"role": "paragraph"}, "read": "own_text"},
        "link": {"locator": {"role": "link"}, "attr": "href"},
        "when": {"locator": {"attr": {"name": "datetime", "regex": "."}}, "attr": "datetime"},
    }, 10, [
        {"id": "t1_a", "body": "First comment", "link": "/p/1/c/t1_a", "when": "2026-11-04T08:12:00Z"},
        {"id": "t1_b", "body": "A reply", "link": "https://evil.example/x", "when": None},
    ], 2),
    ("max_items caps the records, not the total", "custom", L(attr={"name": "thingid", "prefix": "t"}),
     {"id": {"attr": "thingid"}}, 2, [{"id": "t3_p1"}, {"id": "t1_a"}], 4),
    ("own_text skips the nested reply, text keeps it", "custom",
     L(role="article", attr={"name": "thingid", "equals": "t1_a"}),
     {"own": {"read": "own_text"}, "all": {"read": "text"}}, 5,
     [{"own": "First comment Permalink Like", "all": "First comment Permalink Like A reply Permalink"}], 1),
    ("a field that matches several elements is None", "custom",
     L(role="article", attr={"name": "thingid", "equals": "t3_p1"}),
     {"links": {"locator": {"role": "link", "own": False}, "attr": "href"},
      "open": {"locator": {"role": "link", "name": "Open post"}, "attr": "href"}}, 5,
     [{"links": None, "open": "https://social.example/p/1"}], 1),
    ("posts under main, attributes from the item itself", "custom",
     L(role="article", attr={"name": "thingid", "prefix": "t3_"}, within={"role": "main"}),
     {"id": {"attr": "thingid"}, "missing": {"attr": "author"}, "text": {"read": "own_text"}}, 5,
     [{"id": "t3_p1", "missing": None, "text": "Our first post Like Open"},
      {"id": "t3_p2", "missing": None, "text": "Second post Open Patinka"}], 2),
    ("no items under the anchor is an empty list", "social",
     L(role="article", attr={"name": "thingid", "prefix": "t"}, within={"role": "main"}),
     {"id": {"attr": "thingid"}}, 5, [], 0),
]

EXTRACT_IDS = [c[0] for c in EXTRACT_CASES]

CASE_IDS = [c[0] for c in CASES]

# ---------------------------------------------------------------- observing a driver


async def observe(driver: Any, loc: Locator) -> dict[str, Any]:
    """What a flow can learn about a locator: find's outcome, then read_state."""
    out: dict[str, Any] = {}
    try:
        await driver.find(loc)
        out["find"] = "ok"
    except AmbiguousLocator:
        out["find"] = "ambiguous"
    except ElementNotFound:
        out["find"] = "not_found"
    st = await driver.read_state(loc)
    out.update(count=st.count, found=st.found, ambiguous=st.ambiguous)
    if st.found:
        out.update(key=st.attributes.get("data-pk"), text=st.text, value=st.value, attrs=dict(st.attributes))
    return out


async def observe_extract(driver: Any, items: Locator, fields: dict[str, Any], max_items: int) -> dict[str, Any]:
    parsed = {k: ExtractField.model_validate(v) for k, v in fields.items()}
    rows, total = await driver.extract_items(items, parsed, max_items)
    return {"rows": rows, "total": total}


def mismatches(observed: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    bad = []
    for k, want in expected.items():
        got = observed.get(k)
        if k == "attrs":
            got = {a: (got or {}).get(a) for a in want}
        if got != want:
            bad.append(f"{k}: expected {want!r}, got {got!r}")
    return bad


def fake_driver(page: str) -> FakeDriver:
    site = FakeSite()
    nodes = PAGES[page]
    site.route("https://parity.example/", lambda s, d: El("document", "parity", children=[to_el(x) for x in nodes]))
    return FakeDriver(site)


def fixed_own_text(el: El, role: str | None = None) -> str:
    """fakes.own_text as fixed: exclude text inside nested same-role elements at any depth."""
    role = el.role if role is None else role
    parts = [el.text] if el.text else []
    for c in el.children:
        if c.role != role:
            t = fixed_own_text(c, role)
            if t:
                parts.append(t)
    return " ".join(" ".join(parts).split())


# ---------------------------------------------------------------- the fake side


@pytest.mark.parametrize(("case_id", "page", "loc", "expected", "diverges"), CASES, ids=CASE_IDS)
async def test_fake_matches_the_expected_semantics(case_id, page, loc, expected, diverges):
    driver = fake_driver(page)
    await driver.navigate("https://parity.example/")
    bad = mismatches(await observe(driver, loc), expected)
    if diverges:
        assert bad, "the fake now follows the docstring: drop fake_diverges for this case"
        pytest.xfail("fakes.own_text keeps text of same-role elements nested through a wrapper; "
                     "see fixed_own_text for the proposed fix")
    assert not bad, bad


@pytest.mark.parametrize(("case_id", "page", "loc", "expected", "diverges"), CASES, ids=CASE_IDS)
async def test_fake_with_the_proposed_own_text_fix_passes_every_case(monkeypatch, case_id, page, loc, expected,
                                                                    diverges):
    monkeypatch.setattr(fakes, "own_text", fixed_own_text)
    driver = fake_driver(page)
    await driver.navigate("https://parity.example/")
    assert not mismatches(await observe(driver, loc), expected)


@pytest.mark.parametrize(("case_id", "page", "items", "fields", "max_items", "rows", "total"), EXTRACT_CASES,
                         ids=EXTRACT_IDS)
async def test_fake_extracts_the_expected_records(case_id, page, items, fields, max_items, rows, total):
    driver = fake_driver(page)
    await driver.navigate("https://parity.example/")
    assert await observe_extract(driver, items, fields, max_items) == {"rows": rows, "total": total}


def test_custom_pages_render_shadow_roots_and_custom_elements():
    page = to_html(CUSTOM)
    assert '<x-post role="article" aria-label="Post by Ana" thingid="t3_p1" data-pk="p1" style="display:block">' \
           "<template data-shadow>Our first post" in page
    assert "attachShadow" in page and "attachShadow" not in to_html(SOCIAL)


def test_pages_render_to_html_with_roles_names_and_keys():
    page = to_html(SOCIAL)
    assert '<div role="article" aria-label="Post by Ieva" data-pk="post">Lovely day at the lake' in page
    assert 'class="like-post"' in page and 'aria-pressed="false"' in page
    keys = [k for k in page.split('data-pk="')[1:]]
    assert len(keys) == len(set(keys))


def test_every_case_locator_is_a_valid_flow_locator():
    for case_id, page, loc, _, _ in CASES:
        assert page in PAGES, case_id
        assert Locator.model_validate(loc.model_dump()) == loc
