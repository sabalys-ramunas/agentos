"""Serve-time changes to the fixture pages, the kind real sites make between two visits.

Each mutation is a pure function of a page tree and a seed: it copies the tree and returns the
changed copy, and the same seed always gives the same page (tests/lanebench/test_mutations.py).
A task lists its mutations as tables (`[[mutation]] kind = "move_node" node = "follow" to =
"footer"`); `apply` runs them in order. A mutation that finds nothing to change on a page leaves
it as it was, so one list serves every page of a task.

- rename_classes: every class name becomes a seeded hash (the stylesheet follows).
- move_node(node, to): each `node:<id>` element moves into `to:<id>` (the follow button into the
  post's footer); without an id, `node` moves into `to`.
- label_to_icon(node): the text of each `node:*` button becomes an icon; the words stay as its
  aria-label and title, the way sites ship icon buttons.
- add_modal(modal): a dialog that covers the page a seeded moment after it loads.
- locale(lang): the page in another language (Lithuanian).
- lazy_list(batch): the feed shows its first posts; the rest load as it is scrolled.
- shadow_wrap(node): each `node:*` element moves into an open shadow root.
"""

from __future__ import annotations

import hashlib
import random
import re
from collections.abc import Callable
from typing import Any

from lanebench.sites.dom import El, Raw, T, clone, find, h, walk

Mutation = Callable[..., El]


def _rng(seed: int, name: str) -> random.Random:
    return random.Random(int(hashlib.sha256(f"{seed}:{name}".encode()).hexdigest()[:12], 16))


def class_map(seed: int, names: set[str]) -> dict[str, str]:
    return {n: "c" + hashlib.sha256(f"{seed}:{n}".encode()).hexdigest()[:7] for n in sorted(names)}


def rename_classes(tree: El, seed: int) -> El:
    tree = clone(tree)
    names = {c for node, _ in walk(tree) for c in node.classes()}
    mapping = class_map(seed, names | CSS_CLASSES)
    for node, _ in walk(tree):
        if node.classes():
            node.attrs["class"] = " ".join(mapping.get(c, c) for c in node.classes())
    return tree


def rename_css(css: str, seed: int) -> str:
    """The stylesheet for a page whose classes were renamed with this seed."""
    mapping = class_map(seed, CSS_CLASSES)
    return re.sub(r"\.([a-zA-Z][\w-]*)", lambda m: "." + mapping.get(m.group(1), m.group(1)), css)


def _split(lb: str) -> tuple[str, str | None]:
    kind, _, ident = lb.partition(":")
    return kind, ident or None


def move_node(tree: El, seed: int, *, node: str, to: str) -> El:
    tree = clone(tree)
    moves: list[tuple[El, El, El]] = []
    for el, parent in walk(tree):
        kind, ident = _split(el.lb)
        if kind != node or parent is None:
            continue
        target = find(tree, f"{to}:{ident}" if ident else to)
        if target is not None and target is not el:
            moves.append((el, parent, target))
    for el, parent, target in moves:
        parent.children.remove(el)
        target.children.append(el)
    return tree


def label_to_icon(tree: El, seed: int, *, node: str) -> El:
    from lanebench.sites.pages import ICONS

    tree = clone(tree)
    icon = ICONS.get(node, Raw('<svg class="icon" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">'
                                '<circle cx="12" cy="12" r="8" fill="currentColor"/></svg>'))
    for el, _ in walk(tree):
        kind, _ident = _split(el.lb)
        if kind != node:
            continue
        label = next((c for c in el.children if isinstance(c, T)), None)
        if label is None:
            continue
        el.children = [icon if c is label else c for c in el.children]
        el.attrs["aria-label"] = label
        el.attrs["title"] = label
    return tree


MODALS = {
    "notifications": ("notif_title", "notif_body", "turn_on", "not_now"),
}


def add_modal(tree: El, seed: int, *, modal: str = "notifications") -> El:
    tree = clone(tree)
    title, body, yes, no = MODALS[modal]
    body_el = next(n for n, _ in walk(tree) if n.tag == "body")
    if find(tree, "nav") is None:  # only signed-in pages nag
        return tree
    delay = _rng(seed, "modal").randint(400, 1500)
    body_el.children.append(h(
        "div", {"class": "wall", "data-lb": "modal", "data-modal-delay": str(delay), "hidden": True},
        h("div", {"class": "modal", "role": "dialog", "aria-modal": "true", "aria-labelledby": "modal-h"},
          h("h2", {"id": "modal-h"}, T(title)),
          h("p", {}, T(body)),
          h("div", {"class": "row"},
            h("button", {"type": "button", "class": "btn btn-primary", "data-action": "notif", "data-value": "on",
                         "data-lb": "modal-yes"}, T(yes)),
            h("button", {"type": "button", "class": "btn", "data-action": "notif", "data-value": "off",
                         "data-lb": "modal-no"}, T(no))))))
    return tree


def locale(tree: El, seed: int, *, lang: str = "lt") -> El:
    tree = clone(tree)
    tree.attrs["lang"] = lang
    return tree


def lazy_list(tree: El, seed: int, *, batch: int | None = None) -> El:
    tree = clone(tree)
    feed = find(tree, "feed")
    if feed is None:
        return tree
    size = batch or _rng(seed, "lazy").randint(3, 5)
    shown, rest = feed.children[:size], feed.children[size:]
    if not rest:
        return tree
    feed.children = [*shown,
                     h("template", {"data-lb": "lazy-rest", "data-batch": str(size)}, *rest),
                     h("p", {"class": "sentinel", "data-lb": "lazy-sentinel"}, T("load_more"))]
    return tree


def shadow_wrap(tree: El, seed: int, *, node: str) -> El:
    tree = clone(tree)
    for el, parent in list(walk(tree)):
        kind, _ident = _split(el.lb)
        if kind != node or parent is None:
            continue
        host = h("lb-box", {"class": "box"},
                 h("template", {"shadowrootmode": "open"},
                   h("link", {"rel": "stylesheet", "href": "/static/site.css"}), el))
        parent.children[parent.children.index(el)] = host
    return tree


MUTATIONS: dict[str, Mutation] = {
    "rename_classes": rename_classes,
    "move_node": move_node,
    "label_to_icon": label_to_icon,
    "add_modal": add_modal,
    "locale": locale,
    "lazy_list": lazy_list,
    "shadow_wrap": shadow_wrap,
}

# Every class the stylesheet styles (sites/static/site.css), so a renamed page keeps its look.
CSS_CLASSES = {
    "page", "topnav", "brand", "nav-link", "content", "card", "login-card", "field", "check", "error", "btn",
    "btn-primary", "row", "wall", "modal", "post", "post-head", "name", "handle", "follow-btn", "post-body",
    "post-actions", "like-btn", "comment-btn", "count", "comments", "comment", "comment-form", "post-foot", "when",
    "post-img", "feed", "title", "composer", "preview", "status", "settings", "sentinel", "icon", "box", "promo",
    "hidden-text", "lookalike",
}


def validate(specs: list[dict[str, Any]]) -> None:
    for spec in specs:
        kind = spec.get("kind")
        if kind not in MUTATIONS:
            raise ValueError(f"unknown mutation {kind!r}; known: {', '.join(MUTATIONS)}")


def apply(tree: El, specs: list[dict[str, Any]], seed: int) -> El:
    """The page with every mutation of `specs` applied in order (pure: `tree` is unchanged)."""
    validate(specs)
    for spec in specs:
        params = {k: v for k, v in spec.items() if k != "kind"}
        tree = MUTATIONS[spec["kind"]](tree, seed, **params)
    return tree


def renames(specs: list[dict[str, Any]]) -> bool:
    return any(s.get("kind") == "rename_classes" for s in specs)


def lang(specs: list[dict[str, Any]]) -> str:
    return next((s.get("lang", "lt") for s in specs if s.get("kind") == "locale"), "en")
