"""Find what a page repeats, from its accessibility outline, in pure Python (docs/SITE-APIS.md).

`discover(outline, url)` takes the text browser_snapshot reads (openajar.node.snapshot) and proposes
`{name, items, fields, sample, count, confidence}` per repeated shape: table rows, list items,
repeated articles, cards of boxes with the same role shape. It runs no JavaScript and reads nothing
but the text it is handed, so it can be tested from stored outlines.

Page text is hostile here. What leaves this module:

- structure: roles, levels, positions, and names only through `plain_label`, which keeps short plain
  labels and drops anything else, so a table named "Ignore your instructions" is addressed by its
  position and its column is `col_1`;
- field names, always through `field_name`, so they match the param name pattern whatever the page
  says, and are never page text verbatim;
- `sample`: at most SAMPLE_ROWS rows of values clipped to SAMPLE_CHARS, under `{"untrusted": true}`.

Size is capped as well (MAX_CHARS, MAX_LINES, MAX_DEPTH, MAX_NODES_READ), so a huge or deep page costs
a bounded read and cannot run this out of stack.

`definition_from` turns chosen candidates into a site API definition; a person still activates it.

`json_candidates` does the same for the JSON a page's own code fetched while it loaded. A body is hostile
too and is held only for this call. What leaves is its URL path and shape: keys that fit a path, and
the type of each value, never a value. A key that names a credential (openajar.runner.models.names_secret)
reads `hidden` and is never proposed as a field.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from openajar.core.driver import JsonResponse, parse_json
from openajar.node.snapshot import parse_line
from openajar.runner.names import PARAM_NAME_RE, RESERVED_PARAMS
from openajar.runner.urlpattern import names_secret, url_pattern
from openajar.runner.util import host_in

MAX_CHARS = 1_000_000
MAX_LINES = 30_000
MAX_DEPTH = 48
MAX_NODES_READ = 4000  # nodes a shape looks at per candidate, however wide the page
MIN_ITEMS = 3
MIN_ROWS = 2
MAX_CANDIDATES = 8
MAX_FIELDS = 12
MAX_ITEMS = 100
SAMPLE_ROWS = 3
SAMPLE_CHARS = 80
MAX_JSON = 6  # responses listed per page
MAX_JSON_KEYS = 30  # per object
MAX_JSON_DEPTH = 4
ID_MASK = "{id}"  # what a URL path shows in place of a part that looks like an id or a token
MIN_COVERAGE = 0.6  # a field in fewer of the items than this is not part of the shape
MIN_SIMILAR = 0.5

TABLES = frozenset({"table", "grid", "treegrid"})
CELLS = frozenset({"cell", "gridcell", "rowheader"})
# landmarks and widgets that hold navigation, not data
CHROME = frozenset({"navigation", "banner", "contentinfo", "menu", "menubar", "tablist", "toolbar", "listbox",
                    "combobox", "tree", "dialog", "alertdialog", "search", "radiogroup"})
ACTIONS = frozenset({"button", "checkbox", "radio", "switch", "slider", "spinbutton", "tab", "menuitem"})
INPUTS = frozenset({"textbox", "searchbox", "combobox", "spinbutton", "slider"})
TEXTS = {"heading": "title", "paragraph": "text", "time": "date", "strong": "text", "emphasis": "text",
         "blockquote": "quote", "status": "status", "term": "term", "definition": "text", "note": "note",
         "caption": "caption", "cell": "text", "gridcell": "text", "rowheader": "name"}
WRAPPERS = frozenset({"generic", "none", "presentation", "text"})
PLURALS = {"article": "articles", "listitem": "items", "generic": "cards", "figure": "figures", "group": "groups",
           "region": "sections"}
ROLE_RE = re.compile(r"^[a-z][a-z-]{1,40}$")
_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+")
_PLAIN_RE = re.compile(r"^[^\W_][\w .,&()/%'#+-]*$")
# words a label that is really aimed at a model tends to carry; not the boundary, the clipping is
_COMMANDS = re.compile(r"\b(ignore|instruction|instructions|system|prompt|assistant|agent|token|password|secret|"
                       r"cookie|admin|developer|override|approve|activate|execute)\b", re.IGNORECASE)
_MONEY_RE = re.compile(r"^(?:[€$£¥]\s?\d[\d.,\s]*|\d[\d.,\s]*\s?(?:€|EUR|USD|GBP|\$|£))(?:\s.*)?$", re.IGNORECASE)
_MONEY_IN_RE = r"((?:[€$£¥]\s?\d[\d.,]*)|(?:\d[\d.,]*\s?(?:€|EUR|USD|GBP)))"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}|^\d{1,2}[./]\d{1,2}[./]\d{2,4}$")
_NUMBER_RE = re.compile(r"^[+-]?\d[\d.,]*$")
_JSON_KEY_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")
_CONTROL_RE = re.compile("[\\x00-\\x1f\\x7f-\\x9f\\u200b-\\u200f\\u202a-\\u202e\\u2060-\\u206f\\ufeff]")


def clip(value: Any, limit: int = SAMPLE_CHARS) -> str:
    """One line of plain text, at most `limit` characters: what a sample may hold."""
    text = " ".join(_CONTROL_RE.sub(" ", str(value)).split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def plain_label(text: str | None) -> bool:
    """A short, plain name a locator may use: nothing that reads like a sentence or a template."""
    return (bool(text) and len(text or "") <= 40 and len((text or "").split()) <= 4 and bool(_PLAIN_RE.match(text or ""))
            and not _COMMANDS.search(text or ""))


def field_name(text: str | None, fallback: str, taken: Iterable[str] = ()) -> str:
    """A name that matches the param name pattern and is not in `taken`: from a plain label, else
    `fallback`. Never the page's text as it stands."""
    base = ""
    if plain_label(text):
        ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
        base = re.sub(r"[^a-z0-9]+", "_", ascii_text.lower()).strip("_")[:32].rstrip("_")
    if not base or not base[0].isalpha() or base in RESERVED_PARAMS:
        base = fallback
    taken = set(taken)
    name, n = base, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    assert PARAM_NAME_RE.match(name)
    return name


# ---------------------------------------------------------------- the tree


@dataclass(slots=True, eq=False)
class Node:
    role: str
    name: str = ""
    text: str = ""
    url: str | None = None
    level: int | None = None
    parent: Node | None = field(default=None, repr=False)
    children: list[Node] = field(default_factory=list, repr=False)

    @property
    def label(self) -> str:
        return self.name or self.text

    def walk(self) -> Iterator[Node]:
        """Every node under this one, depth first (MAX_DEPTH bounds the stack)."""
        for c in self.children:
            yield c
            yield from c.walk()

    def inside(self, roles: frozenset[str]) -> bool:
        cur = self.parent
        while cur is not None:
            if cur.role in roles:
                return True
            cur = cur.parent
        return False


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        try:
            out = json.loads(value)
            return out if isinstance(out, str) else value
        except ValueError:
            return value[1:-1]
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def parse_outline(text: str) -> tuple[Node, bool]:
    """(the root, was anything cut). Lines deeper than MAX_DEPTH or past the caps are left out."""
    cut = len(text) > MAX_CHARS
    lines = text[:MAX_CHARS].splitlines()
    cut = cut or len(lines) > MAX_LINES
    root = Node("root")
    stack: list[Node] = [root]
    for raw in lines[:MAX_LINES]:
        if not raw.strip():
            continue
        line = parse_line(raw)
        if line.depth >= MAX_DEPTH:
            cut = True
            continue
        del stack[line.depth + 1:]
        parent = stack[-1]
        if line.role is None:
            body = raw.strip().removeprefix("- ")
            if body.startswith("/url:"):
                parent.url = _unquote(body[5:])
            elif body.startswith("text:"):
                parent.children.append(Node("text", text=_unquote(body[5:]), parent=parent))
            continue
        if not ROLE_RE.match(line.role):
            continue
        tail = raw[line.attrs_span[1]:] if line.attrs_span else ""
        tail = tail.strip().strip("'").strip()
        value = _unquote(tail[1:]) if tail.startswith(":") and line.role not in INPUTS else ""
        level = line.attr("level")
        node = Node(line.role, name=line.name or "", text=value, parent=parent,
                    level=int(level) if level and level.isdigit() else None)
        parent.children.append(node)
        stack.append(node)
    return root, cut


def _text_of(node: Node) -> str:
    """The visible text of a node: its name, its value, else its children's."""
    if node.label:
        return node.label
    parts: list[str] = []
    for n in node.walk():
        if len(parts) >= 20:
            break
        if n.label and not n.children:
            parts.append(n.label)
    return " ".join(parts)


def _roles(node: Node) -> Counter[str]:
    """What a node holds of its own, wrappers looked through: its shape."""
    return Counter(n.role for n in _own_nodes(node) if n.role == "text" or n.role not in WRAPPERS)


def _similar(a: Counter[str], b: Counter[str]) -> float:
    keys = set(a) | set(b)
    most = sum(max(a[k], b[k]) for k in keys)
    return sum(min(a[k], b[k]) for k in keys) / most if most else 0.0


def _matches(base: Node, role: str, *, level: int | None = None, name: str | None = None) -> list[Node]:
    return [n for n in base.walk() if n.role == role and (level is None or n.level == level)
            and (name is None or n.name == name)]


# ---------------------------------------------------------------- addressing


def _anchor(node: Node, root: Node) -> dict[str, Any] | None:
    """A locator for one landmark, by its plain name when it has one, else by position."""
    cur: Node | None = node
    while cur is not None and cur.role in WRAPPERS:
        cur = cur.parent
    if cur is None or cur is root or cur.role == "root":
        return None
    if plain_label(cur.name) and len(_matches(root, cur.role, name=cur.name)) == 1:
        return {"role": cur.role, "name": cur.name}
    same = _matches(root, cur.role)
    return {"role": cur.role} if len(same) == 1 else {"role": cur.role, "nth": same.index(cur)}


def _outermost(found: list[Node]) -> list[Node]:
    seen = set(map(id, found))
    out = []
    for n in found:
        cur, nested = n.parent, False
        while cur is not None and not nested:
            nested = id(cur) in seen
            cur = cur.parent
        if not nested:
            out.append(n)
    return out


def _card_anchor(items: list[Node], root: Node) -> dict[str, Any] | None:
    """Boxes no role can name: one element each of them holds exactly once, and nothing else on the
    page does, stands for the box (`pick: card`)."""
    def each_once(loc: dict[str, Any]) -> bool:
        kw = {k: loc[k] for k in ("level", "name") if k in loc}
        found = _matches(root, loc["role"], **kw)
        return len(found) == len(items) and all(len(_matches(i, loc["role"], **kw)) == 1 for i in items)

    first = items[0]
    options: list[dict[str, Any]] = []
    for n in first.walk():
        if n.role == "heading" and n.level:
            options.append({"role": "heading", "level": n.level})
    for n in first.walk():
        if n.role in ("button", "link") and plain_label(n.name):
            options.append({"role": n.role, "name": n.name})
    for role in ("heading", "img", "link", "button"):
        options.append({"role": role})
    return next((o for o in options if each_once(o)), None)


# ---------------------------------------------------------------- fields


def _href(node: Node, page: str, domain: str) -> str | None:
    if not node.url:
        return None
    try:
        parts = urlsplit(urljoin(page, node.url))
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname or not host_in(parts.hostname, domain):
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))  # a query can hold a token


def _value_name(values: list[str]) -> str | None:
    """A name from what kind of values these are, never from what they say."""
    present = [v for v in values if v]
    if not present:
        return None
    for name, rx in (("price", _MONEY_RE), ("date", _DATE_RE), ("number", _NUMBER_RE)):
        if all(rx.match(v) for v in present):
            return name
    return None


def _max_chars(values: list[str]) -> int:
    longest = max((len(v) for v in values), default=0)
    return 200 if longest <= 100 else 2000


def _text_field(locator: dict[str, Any] | None, values: list[str]) -> dict[str, Any]:
    return {**({"locator": locator} if locator else {}), "read": "text", "max_chars": _max_chars(values), "untrusted": True}


def _link_field(locator: dict[str, Any], domain: str) -> dict[str, Any]:
    return {"locator": locator, "attr": "href", "format": "uri", "host": domain}


@dataclass(slots=True)
class _Column:
    name: str
    spec: dict[str, Any]
    values: list[str | None]


def _own_nodes(item: Node) -> list[Node]:
    """What an item holds, not counting a nested repeat (the replies under a post) or its controls."""
    out: list[Node] = []

    def visit(n: Node) -> None:
        for c in n.children:
            if len(out) >= MAX_NODES_READ or c.role == item.role or c.role in ACTIONS or c.role in INPUTS:
                continue
            if c.role in ("region", "list", "group", "feed") and c.name:
                continue
            out.append(c)
            if c.role != "link":
                visit(c)

    visit(item)
    return out


def _keyed(item: Node) -> dict[tuple[str, int], Node]:
    seen: Counter[str] = Counter()
    keyed: dict[tuple[str, int], Node] = {}
    for n in _own_nodes(item):
        keyed[(n.role, seen[n.role])] = n
        seen[n.role] += 1
    return keyed


class _Columns:
    """The columns found so far, and the field names they took."""

    def __init__(self) -> None:
        self.found: list[_Column] = []
        self.taken: set[str] = set()

    def add(self, base: str, spec: dict[str, Any], values: list[Any]) -> None:
        name = field_name(None, base, self.taken)
        self.found.append(_Column(name, spec, values))
        self.taken.add(name)

    def full(self) -> bool:
        return len(self.found) >= MAX_FIELDS


def _item_columns(items: list[Node], page: str, domain: str) -> list[_Column]:
    keyed = [_keyed(i) for i in items]
    counts: Counter[tuple[str, int]] = Counter(k for row in keyed for k in row)
    per_role: dict[str, int] = {}
    for role, n in counts:
        per_role[role] = max(per_role.get(role, 0), n + 1)
    columns = _Columns()
    texts: dict[int, list[str]] = {}
    order = list(dict.fromkeys(k for row in keyed for k in row))
    for key in order:
        role, n = key
        if counts[key] < MIN_COVERAGE * len(items) or columns.full():
            continue
        rows = [row.get(key) for row in keyed]
        locator: dict[str, Any] = {"role": role} if per_role[role] == 1 else {"role": role, "nth": n}
        loose = _key_columns(columns, role, locator, rows, page, domain)
        if loose is not None:
            texts[n] = loose
    if texts and not any(c.spec.get("read") == "text" and "locator" not in c.spec and "capture" not in c.spec
                         for c in columns.found):
        words = [" ".join(_text_of(i).split()) for i in items]
        name = field_name(None, "text", columns.taken)
        columns.found.append(_Column(name, _text_field(None, words), list(words)))
    return columns.found


def _key_columns(columns: _Columns, role: str, locator: dict[str, Any], rows: list[Node | None], page: str,
                 domain: str) -> list[str] | None:
    """The columns of one element (by role and place) across the items. Loose text that is not a
    price is not a column of its own: it is returned, to be read as the item's whole text."""
    if role == "link":
        _link_columns(columns, locator, rows, page, domain)
    elif role == "img":
        words = [r.name if r else "" for r in rows]
        if any(words):
            columns.add("image", _text_field(locator, words), list(words))
    elif role == "text":  # loose text has no element of its own: read it out of the item
        words = [r.text if r else "" for r in rows]
        if _value_name(words) != "price":
            return words
        found = [re.search(_MONEY_IN_RE, w) for w in words]
        columns.add("price", {"read": "text", "capture": _MONEY_IN_RE, "max_chars": 80, "untrusted": True},
                    [m.group(1) if m else "" for m in found])
    elif role in TEXTS:
        words = [_text_of(r) if r else "" for r in rows]
        if any(words):
            columns.add(_value_name(words) or TEXTS[role], _text_field(locator, [w for w in words if w]),
                        list(words))
    return None


def _link_columns(columns: _Columns, locator: dict[str, Any], rows: list[Node | None], page: str,
                  domain: str) -> None:
    """A link in each item: its words (the title, unless a column already holds them) and its address."""
    hrefs = [_href(r, page, domain) if r else None for r in rows]
    words = [_text_of(r) if r else "" for r in rows]
    found = columns.found
    headed = any(c.name.startswith("title") and c.values == words for c in found)
    if any(words) and not headed and not any(c.values == words for c in found):
        base = "title" if not any(c.name.startswith("title") for c in found) else "link"
        columns.add(base, _text_field(locator, [w for w in words if w]), list(words))
    if any(hrefs):
        columns.add("url", _link_field(locator, domain), list(hrefs))


def _table_columns(rows: list[Node], heads: list[Node], page: str, domain: str) -> list[_Column]:
    width = max(len([c for c in r.children if c.role in CELLS]) for r in rows)
    columns: list[_Column] = []
    taken: set[str] = set()
    cells = [[c for c in r.children if c.role in CELLS] for r in rows]
    seen: Counter[str] = Counter()
    for i in range(min(width, MAX_FIELDS)):
        role = cells[0][i].role if i < len(cells[0]) else "cell"
        nth = seen[role]
        seen[role] += 1
        col = [row[i] if i < len(row) else None for row in cells]
        words = [_text_of(c) if c else "" for c in col]
        label = heads[i].name if i < len(heads) else None
        locator = {"role": role, "nth": nth}
        fallback = _value_name(words) or f"col_{i + 1}"
        name = field_name(label, fallback, taken)
        taken.add(name)
        columns.append(_Column(name, _text_field(locator, [w for w in words if w]), list(words)))
        links = [next((n for n in c.walk() if n.role == "link"), None) if c else None for c in col]
        hrefs = [_href(n, page, domain) if n else None for n in links]
        if any(hrefs):
            url_name = field_name(None, "url", taken)
            taken.add(url_name)
            columns.append(_Column(url_name, _link_field({"role": "link", "within": locator}, domain), list(hrefs)))
    return columns


def _sample(columns: list[_Column], count: int) -> dict[str, Any]:
    rows = [{c.name: clip(c.values[i]) if c.values[i] else None for c in columns} for i in range(min(SAMPLE_ROWS, count))]
    return {"untrusted": True, "rows": rows}


def _candidate(name: str, kind: str, items: dict[str, Any], columns: list[_Column], count: int, confidence: float,
               pick: str | None = None) -> dict[str, Any]:
    out = {"name": name, "kind": kind, "items": items, "fields": {c.name: c.spec for c in columns},
           "sample": _sample(columns, count), "count": count, "confidence": round(max(0.05, min(confidence, 0.99)), 2)}
    return {**out, "pick": pick} if pick else out


# ---------------------------------------------------------------- what repeats


def _table(node: Node, root: Node, page: str, domain: str, n: int) -> dict[str, Any] | None:
    rows = [r for r in node.walk() if r.role == "row" and not _in_nested_table(r, node)]
    heads = next(([c for c in r.children if c.role == "columnheader"] for r in rows
                  if any(c.role == "columnheader" for c in r.children)), [])
    data = [r for r in rows if any(c.role in CELLS for c in r.children)]
    if len(data) < MIN_ROWS:
        return None
    columns = _table_columns(data, heads, page, domain)
    if not columns:
        return None
    anchor = _anchor(node, root)
    groups = [g for g in node.children if g.role == "rowgroup"]
    body = next((g for g in groups if any(r in data for r in g.children)), None)
    within = anchor
    if body is not None and len(groups) > 1:
        within = {"role": "rowgroup", "nth": groups.index(body), **({"within": anchor} if anchor else {})}
    items = {"role": "row", **({"within": within} if within else {})}
    name = field_name(node.name, f"table_{n}" if n else "rows")
    return _candidate(name, "table", items, columns, len(data), 0.95 if heads else 0.8)


def _in_nested_table(row: Node, table: Node) -> bool:
    cur = row.parent
    while cur is not None and cur is not table:
        if cur.role in TABLES:
            return True
        cur = cur.parent
    return False


def _members(kids: list[Node]) -> list[Node]:
    """The largest set of siblings of one shape, or nothing when they share none."""
    shapes = [_roles(k) for k in kids]
    best: list[Node] = []
    for seed in shapes[:12]:
        same = [k for k, s in zip(kids, shapes, strict=True) if _similar(seed, s) >= MIN_SIMILAR]
        if len(same) > len(best):
            best = same
    return best


def _repeat(parent: Node, role: str, kids: list[Node], root: Node, page: str, domain: str,
            n: int) -> tuple[dict[str, Any], list[Node]] | None:
    members = _members(kids)
    if len(members) < MIN_ITEMS:
        return None
    columns = _item_columns(members, page, domain)
    if not columns:
        return None
    links_only = all(c.name.startswith(("title", "url", "link")) for c in columns)
    if role == "generic":
        loc = _card_anchor(members, root)
        if loc is None:
            return None
        within, pick, kind, base = None, "card", "cards", 0.75
        items = loc
    else:
        within = _anchor(parent, root)
        items = {"role": role, **({"within": within} if within else {})}
        scope = _scope(parent, root) if within else root
        found = _matches(scope, role)
        pick = None
        if len(found) != len(_outermost(found)):
            pick = "outermost"
            found = _outermost(found)
        if len(found) > len(members) * 1.5:
            return None  # the locator would read far more than this shape
        kind, base = ("list", 0.85) if role == "listitem" else ("items", 0.85)
    confidence = base * (len(members) / max(len(kids), 1)) ** 0.5
    if len(columns) < 2:
        confidence *= 0.7
    if links_only:
        confidence *= 0.8
    if parent.inside(frozenset({"complementary"})) or parent.role == "complementary":
        confidence *= 0.7
    label = parent.name if parent.role not in WRAPPERS else ""
    plural = PLURALS.get(role, "items")
    name = field_name(label, f"{plural}_{n + 1}" if n else plural)
    return _candidate(name, kind, items, columns, len(members), confidence, pick), members


def _scope(parent: Node, root: Node) -> Node:
    cur: Node | None = parent
    while cur is not None and cur.role in WRAPPERS:
        cur = cur.parent
    return cur or root


def _scan(root: Node, page: str, domain: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    tables = 0
    groups = 0

    def visit(node: Node) -> None:
        nonlocal tables, groups
        if node.role in CHROME:
            return
        if node.role in TABLES:
            cand = _table(node, root, page, domain, tables)
            tables += 1
            if cand:
                found.append(cand)
            return
        taken: set[int] = set()
        by_role: dict[str, list[Node]] = {}
        for c in node.children:
            if c.role not in ("text", "paragraph", "heading", "link", "button", "img") and c.role not in CHROME:
                by_role.setdefault(c.role, []).append(c)
        for role, kids in by_role.items():
            if len(kids) < MIN_ITEMS:
                continue
            got = _repeat(node, role, kids, root, page, domain, groups)
            if got:
                groups += 1
                found.append(got[0])
                taken |= set(map(id, got[1]))
        for c in node.children:
            if id(c) not in taken:
                visit(c)

    visit(root)
    return found


def _unique(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out = []
    for c in candidates:
        c["name"] = field_name(None, c["name"], seen)
        seen.add(c["name"])
        out.append(c)
    return out


NOTHING_FOUND = (
    "Nothing with a role repeats on this page (no list, table or named items), and no plain boxes could be told "
    "apart by an element each holds once. If the page shows cards of plain boxes, write the extract step yourself: pick: card with "
    "a locator for something each card holds once (a link such as role link, or a heading level), then try it "
    "with site_api_try. A page that needs input first (a search) can be recorded instead.")


def discover(outline: str, url: str) -> dict[str, Any]:
    """Candidates from one page's outline, best first, and what could not be read."""
    root, cut = parse_outline(outline or "")
    parts = urlsplit(url)
    domain = (parts.hostname or "").lower()
    found = sorted(_scan(root, url, domain), key=lambda c: (-c["confidence"], -c["count"]))
    out: dict[str, Any] = {"url": urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")),
                           "candidates": _unique(found[:MAX_CANDIDATES]), "truncated": cut}
    if not found:
        # QA 2026-11-10, tester B 8: a page of plain boxes (nested `generic`) does repeat; say what to try
        out["note"] = NOTHING_FOUND
    return out


# ---------------------------------------------------------------- the page's own JSON


def _looks_like_id(text: str) -> bool:
    """Is this part of a URL path, or a JSON key, an id or a token and not a word: a number, hex, a uuid,
    base64, or anything a plain word never holds? A guess that errs toward hiding, since a URL can hold a
    token and a map's keys are values."""
    if re.search(r"[^A-Za-z0-9._-]", text) or text.isdigit() or sum(c.isdigit() for c in text) >= 3:
        return True
    if re.fullmatch(r"[0-9a-fA-F-]{8,}", text):
        return True
    words = _WORD_RE.findall(text)
    return len(text) >= 12 and (len(text) / max(len(words), 1) < 3 or max(map(len, words), default=0) > 24)


def _plain_parts(path: str) -> list[str]:
    return [part for part in path.split("/") if part and not _looks_like_id(part)]


def _masked(path: str) -> str:
    return "/".join(ID_MASK if part and _looks_like_id(part) else part for part in path.split("/"))


def _json_keys(value: dict[str, Any]) -> list[tuple[str, Any]]:
    """The keys a path could name, at most MAX_JSON_KEYS: a key is the site's text like any other."""
    return [(k, v) for k, v in value.items() if _JSON_KEY_RE.fullmatch(k)][:MAX_JSON_KEYS]


def _is_map(value: dict[str, Any]) -> bool:
    """Keyed by ids and not by fields, so its keys are values: none is plain, or one looks like an id."""
    plain = [k for k in value if _JSON_KEY_RE.fullmatch(k)]
    return bool(value) and (not plain or any(_looks_like_id(k) for k in plain))


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    return {int: "integer", float: "number", str: "string", list: "list", dict: "object"}.get(type(value), "null")


def _shape(value: Any, depth: int = 0) -> Any:
    if depth < MAX_JSON_DEPTH and isinstance(value, dict):
        if _is_map(value):
            return f"map of {len(value)}"
        return {k: "hidden" if names_secret(k) else _shape(v, depth + 1) for k, v in _json_keys(value)}
    if depth < MAX_JSON_DEPTH and isinstance(value, list):
        return [_shape(value[0], depth + 1)] if value else []
    return _kind(value)


def _records(value: Any, path: str = ".", depth: int = 0) -> tuple[str, list[Any]] | None:
    """The first list of objects, nearest the top: (its path, the list)."""
    if isinstance(value, list):
        return (path, value) if value and isinstance(value[0], dict) else None
    if not isinstance(value, dict) or depth >= MAX_JSON_DEPTH or _is_map(value):
        return None
    keys = [(k, v) for k, v in _json_keys(value) if not names_secret(k)]
    under = [(k if path == "." else f"{path}.{k}", v) for k, v in keys]
    return next((got for p, v in under if isinstance(v, list) and (got := _records(v, p, depth + 1))), None) \
        or next((got for p, v in under if isinstance(v, dict) and (got := _records(v, p, depth + 1))), None)


def _json_field(path: str, value: Any) -> dict[str, Any] | None:
    kind = _kind(value)
    if kind == "string":
        return {"path": path, "type": "string", "max_chars": 200, "untrusted": True}
    return {"path": path, "type": kind} if kind in ("boolean", "integer", "number") else None


def _json_fields(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """A field per plain value of the first record, and of the objects right under it."""
    if _is_map(item):
        return {}
    found = [(k, k, v) for k, v in _json_keys(item) if not isinstance(v, dict)]
    found += [(f"{k}.{k2}", f"{k} {k2}", v2) for k, v in _json_keys(item)
              if isinstance(v, dict) and not names_secret(k) and not _is_map(v) for k2, v2 in _json_keys(v)]
    out: dict[str, dict[str, Any]] = {}
    for path, label, value in found:
        field_spec = None if names_secret(path.rsplit(".", 1)[-1]) else _json_field(path, value)
        if field_spec is not None and len(out) < MAX_FIELDS:
            out[field_name(label, f"col_{len(out) + 1}", out)] = field_spec
    return out


def _url_for(url: str) -> str | None:
    """The read_json `url` of a response: its host and path, with what looks like an id as `[^/]+`. None if
    it cannot say that."""
    parts = urlsplit(url)

    def literal(text: str) -> str:
        return re.sub(r"([.?+*()\[\]{}|$^])", r"\\\1", text)

    path = "/".join("[^/]+" if part and _looks_like_id(part) else literal(part) for part in parts.path.split("/"))
    text = f"{literal(parts.netloc)}{path}"
    try:
        url_pattern(f"^https://{text}")
    except ValueError:
        return None
    return f"^https://{text}"


def json_candidates(seen: Iterable[JsonResponse]) -> list[dict[str, Any]]:
    """What a page's own code fetched, as candidates for a read_json step, its bodies left behind."""
    out: list[dict[str, Any]] = []
    taken: set[str] = set()
    for response in list(seen)[:MAX_JSON]:
        try:
            body = parse_json(response.body or b"") if 200 <= response.status < 300 and not response.problem else None
        except ValueError:
            continue
        if body is None or not isinstance(body, dict | list):
            continue
        path = urlsplit(response.url).path
        name = field_name(next(iter(reversed(_plain_parts(path))), ""), "json", taken)
        taken.add(name)
        cand: dict[str, Any] = {"name": name, "kind": "json", "path": clip(_masked(path), 120), "shape": _shape(body),
                                "untrusted": True}
        found = _records(body)
        url = _url_for(response.url)
        fields = _json_fields(found[1][0]) if found else {}
        if found and url and fields:
            cand |= {"url": url, "items": found[0], "count": len(found[1]), "fields": fields}
        out.append(cand)
    return out


# ---------------------------------------------------------------- into a definition


def extract_step(candidate: dict[str, Any], *, max_items: int = MAX_ITEMS) -> dict[str, Any]:
    step = {"type": "extract", "save_as": candidate["name"], "max_items": max_items, "items": candidate["items"],
            "fields": candidate["fields"], "drop_empty": True}
    return {**step, "pick": candidate["pick"]} if candidate.get("pick") else step


def read_json_step(candidate: dict[str, Any], *, max_items: int = MAX_ITEMS) -> dict[str, Any]:
    """A read_json step for a candidate of json_candidates that has fields. It goes right after the
    navigate of the page it was seen on."""
    return {"type": "read_json", "save_as": candidate["name"], "url": candidate["url"], "items": candidate["items"],
            "max_items": max_items, "fields": candidate["fields"]}


def endpoint_from(candidate: dict[str, Any], url: str) -> dict[str, Any]:
    """A read endpoint for one candidate. The URL is the page's, without its fragment."""
    parts = urlsplit(url)
    name = candidate["name"]
    return {"title": name.replace("_", " ").capitalize(), "effect": "read",
            "steps": [{"type": "navigate", "url": urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))},
                      extract_step(candidate)],
            "outputs": {name: name}, "expect": {"min_items": {name: 1}}}


def definition_from(api: str, url: str, candidates: list[dict[str, Any]], *, surfaces: list[str] | None = None,
                    title: str | None = None) -> dict[str, Any]:
    """A site API definition, one read endpoint per candidate, on the page's own host."""
    domain = (urlsplit(url).hostname or "").lower()
    return {"api": api, **({"title": title} if title else {}), "domain": domain, "version": "1.0.0",
            "surfaces": list(surfaces or []), "endpoints": {c["name"]: endpoint_from(c, url) for c in candidates}}
