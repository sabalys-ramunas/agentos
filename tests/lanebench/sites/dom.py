"""A tiny element tree for the fixture pages, so mutations can work on structure instead of on
HTML text. Pages are built from `h(tag, attrs, *children)`; a child is an element, a plain string,
a `T` (a localized string, resolved when the page is rendered) or `Raw` HTML (an icon, a script).
Attribute values may be `T` too (aria-label, title, placeholder).

`data-lb` keys name the parts mutations and the harness need ("like:mira-tomatoes", "feed",
"composer"). They are the grader's handles: a repair patch that uses one is invalid
(gates.patch_valid), because a real site has no such attribute.
"""

from __future__ import annotations

import copy
import html
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

VOID = frozenset({"area", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"})


@dataclass(frozen=True)
class T:
    """A string from the locale catalog (sites.strings), with optional format arguments."""
    key: str
    args: tuple[tuple[str, str], ...] = ()

    def text(self, lang: str) -> str:
        from lanebench.sites.strings import text

        return text(lang, self.key, **dict(self.args))


class Raw(str):
    """HTML that is written as it is (trusted: our own icons and scripts)."""


@dataclass
class El:
    tag: str
    attrs: dict[str, object] = field(default_factory=dict)
    children: list[object] = field(default_factory=list)

    @property
    def lb(self) -> str:
        return str(self.attrs.get("data-lb", ""))

    def classes(self) -> list[str]:
        return str(self.attrs.get("class", "")).split()


def h(tag: str, attrs: dict[str, object] | None = None, *children: object) -> El:
    return El(tag, dict(attrs or {}), [c for c in children if c is not None])


def clone(node: El) -> El:
    return copy.deepcopy(node)


def walk(node: El, parent: El | None = None) -> Iterator[tuple[El, El | None]]:
    """Every element under `node` (itself first), with its parent, in document order."""
    yield node, parent
    for child in list(node.children):
        if isinstance(child, El):
            yield from walk(child, node)


def find_all(root: El, pred: Callable[[El], bool]) -> list[tuple[El, El | None]]:
    return [(n, p) for n, p in walk(root) if pred(n)]


def find(root: El, lb: str) -> El | None:
    for n, _ in walk(root):
        if n.lb == lb:
            return n
    return None


def lang_of(root: El) -> str:
    return str(root.attrs.get("lang", "en"))


def _attr_text(value: object, lang: str) -> str:
    return value.text(lang) if isinstance(value, T) else str(value)


def render(node: object, lang: str) -> str:
    if isinstance(node, Raw):
        return str(node)
    if isinstance(node, T):
        return html.escape(node.text(lang), quote=False)
    if isinstance(node, str):
        return html.escape(node, quote=False)
    assert isinstance(node, El), node
    parts = [node.tag]
    for name, value in node.attrs.items():
        if value is None or value is False:
            continue
        if value is True:
            parts.append(name)
        else:
            parts.append(f'{name}="{html.escape(_attr_text(value, lang), quote=True)}"')
    opening = "<" + " ".join(parts) + ">"
    if node.tag in VOID:
        return opening
    inner = "".join(render(c, lang) for c in node.children)
    return f"{opening}{inner}</{node.tag}>"


def document(root: El) -> str:
    return "<!doctype html>\n" + render(root, lang_of(root))
