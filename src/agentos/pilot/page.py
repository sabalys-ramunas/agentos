"""Reading a page for the pilot: the snapshot's outline as elements, and what may be offered.

Promoted from the measured jev executor (tests/lanebench/executors/jev.py, which scored runs stay
pinned to). Code narrows what a model picks from: no link off the lane's sites, no disabled control,
nothing behind an open dialog, no toggle this run already flipped.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from urllib.parse import urlsplit

from agentos.node.egress import in_sites
from agentos.node.snapshot import parse_line

WHERE_CHARS = 60              # a post's name holds its whole body, which a stranger wrote
TEXT_ROLES = frozenset({"textbox", "searchbox", "combobox", "spinbutton"})
DIALOG_ROLES = frozenset({"dialog", "alertdialog"})
CONTEXT_ROLES = DIALOG_ROLES | {"article", "form", "region", "navigation", "banner", "list", "complementary", "group",
                                "tabpanel", "search"}
READ_ROLES = frozenset({"listitem", "paragraph", "article"})
# where an item says who wrote it: its own name, a link, a heading, an image. Words in a paragraph or a
# bare text line are what it says, so "@mira" there is a mention, never its author
WHO_ROLES = frozenset({"link", "heading", "img", "image"})
ITEM_ROLES = frozenset({"article", "listitem", "row", "comment", "blockquote", "treeitem"})  # a post, a comment, a result
# a link that holds more than its own name (a quoted post shown as a card) is an item of its own, so its
# text is not taken for the text of the post around it
CARD_ROLES = frozenset({"link", "heading", "paragraph", "article", "blockquote", "comment", "listitem", "time"})
# a named block inside an item that holds a link and a paragraph (group "Quoted post", group "Comment by
# Beata") is an item of its own too: someone else's words with their own author
SOFT_ROLES = frozenset({"group", "region", "figure", "generic", "note", "section"})


@dataclass(frozen=True)
class Element:
    ref: str
    role: str
    label: str
    name: str = ""
    where: str = ""
    states: tuple[str, ...] = ()
    in_dialog: bool = False
    href: str | None = None
    box: int | None = None    # the innermost item (a post, a comment) it sits in: an index into Page.boxes

    @property
    def key(self) -> tuple[str, str, str]:
        """The same control across snapshots: refs change, and so does the count in "Like 12"."""
        return self.role, re.sub(r"\d+", "", self.name).strip(), self.where


@dataclass
class Box:
    """One item of many alike (a post, a comment, a search result) and what is written in it itself, so
    an action can be checked against what the request named. A comment inside a post is a box of its
    own: its words are not the post's. `parent`: the item around it; `level`: how many items it sits in
    (a wrapper with nothing of its own, such as a list item that only holds the post, does not count)."""
    role: str
    level: int
    texts: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    hrefs: list[str] = field(default_factory=list)
    parent: int | None = None
    names: list[str] = field(default_factory=list)   # the texts that may say who wrote it (WHO_ROLES)
    title: str = ""                                  # the item's own name, as in article "Comment by Beata"
    said: list[str] = field(default_factory=list)    # each paragraph or text line it says, whole (text and links as one)
    # every link it holds: (its name, where it goes, inside a paragraph, after the item's first paragraph)
    links: list[tuple[str, str, bool, bool]] = field(default_factory=list)
    lead: int | None = None   # how many of `names` come before its first words: an author comes first

    @property
    def hollow(self) -> bool:
        return not (self.texts or self.lines or self.hrefs)

    @property
    def text(self) -> str:
        return "\n".join(self.texts)


@dataclass
class Page:
    url: str
    signal: str
    outline: str
    elements: list[Element]
    lines: list[str]
    boxes: list[Box] = field(default_factory=list)

    @property
    def body(self) -> str:
        """The outline without its header, whose ref count changes with nothing to see."""
        return "\n".join(raw for raw in self.outline.splitlines() if raw.lstrip().startswith("- "))

    def by_ref(self, ref: str) -> Element:
        return next(e for e in self.elements if e.ref == ref)

    def by_key(self, key: tuple[str, str, str]) -> Element | None:
        return next((e for e in self.elements if e.key == key), None)

    def ancestors(self, index: int) -> list[int]:
        """The items around boxes[index], innermost first."""
        out: list[int] = []
        parent = self.boxes[index].parent
        while parent is not None:
            out.append(parent)
            parent = self.boxes[parent].parent
        return out

    def within(self, index: int, outer: int) -> bool:
        """Does boxes[index] sit inside boxes[outer]?"""
        return outer in self.ancestors(index)

    def context(self, index: int) -> str:
        """An item's own text and that of the items around it, never that of the items inside it."""
        return "\n".join(self.boxes[i].text for i in (index, *self.ancestors(index)))


def _is_card(rows: list[str], at: int) -> bool:
    """Does the link at rows[at] hold more than its own name: a heading, a paragraph, another link?"""
    depth = parse_line(rows[at]).depth
    for raw in rows[at + 1:]:
        if not raw.strip():
            continue
        line = parse_line(raw)
        if line.depth <= depth:
            return False
        if line.role in CARD_ROLES or raw.lstrip().startswith("- text:"):
            return True
    return False


def _is_block(rows: list[str], at: int) -> bool:
    """Does the block at rows[at] hold both a link and words (a paragraph or a text line)?"""
    depth = parse_line(rows[at]).depth
    link = para = False
    for raw in rows[at + 1:]:
        if not raw.strip():
            continue
        line = parse_line(raw)
        if line.depth <= depth:
            break
        link = link or line.role == "link"
        para = para or line.role == "paragraph" or raw.lstrip().startswith("- text:")
    return link and para


def _settle(boxes: list[Box], opened: list[int | None], elements: list[Element]) -> tuple[list[Box], list[Element]]:
    """Fold into the item around it each box with nothing of its own to tell it apart (a wrapper, a
    list item holding only buttons), then give every box its parent and level."""
    def folds(i: int) -> bool:
        box = boxes[i]
        return box.hollow or (opened[i] is not None and not (box.lines or box.hrefs or box.title))

    def home(i: int | None) -> int | None:
        while i is not None and folds(i):
            i = opened[i]
        return i

    for i, box in enumerate(boxes):
        to = home(i)
        if to is not None and to != i:
            boxes[to].texts.extend(box.texts)
            boxes[to].names.extend(box.names)
            boxes[to].said.extend(box.said)
            boxes[to].links.extend(box.links)
    kept = [i for i in range(len(boxes)) if not folds(i)]
    index = {old: new for new, old in enumerate(kept)}
    out = [boxes[i] for i in kept]
    for i, box in zip(kept, out, strict=True):
        parent = home(opened[i])
        box.parent = index[parent] if parent is not None else None
    for box in out:
        level, parent = 0, box.parent
        while parent is not None:
            level, parent = level + 1, out[parent].parent
        box.level = level

    def where(i: int | None) -> int | None:
        to = home(i)
        return index[to] if to is not None else None

    return out, [replace(e, box=where(e.box)) if e.box is not None else e for e in elements]


def parse_page(text: str) -> Page:
    """A browser_snapshot result: its header, every actionable element with where it sits on the
    page (the article or form around it is what tells two Like buttons apart), and the text lines."""
    url = re.search(r"https?://\S+", text)
    signal = re.search(r"^signal: (\w+)", text, re.MULTILINE)
    elements: list[Element] = []
    lines: list[str] = []
    stack: list[tuple[int, str, bool]] = []
    boxes: list[Box] = []
    opened: list[int | None] = []       # the item open around each box when it began
    items: list[tuple[int, int]] = []   # (depth, index into boxes) of the items open around this line
    para: list[int] = []                # depths of the paragraphs open around this line
    whole: list[tuple[int, int]] = []   # (box, index into its said) of the outermost paragraph open
    link: tuple[str, bool] = ("", False)  # the name of the last link, and whether it sat in a paragraph
    rows = text.splitlines()
    for at, raw in enumerate(rows):
        line = parse_line(raw)
        stripped = raw.lstrip()
        if stripped.startswith("- /url:"):
            href = stripped.split(":", 1)[1].strip()
            if elements and elements[-1].role == "link" and elements[-1].href is None:
                elements[-1] = replace(elements[-1], href=href)
            if items:
                box = boxes[items[-1][1]]
                box.hrefs.append(href)
                box.links.append((link[0], href, link[1], box.lead is not None))
            continue
        if line.role is None and not stripped.startswith("- text:"):
            continue
        while stack and stack[-1][0] >= line.depth:
            stack.pop()
        while items and items[-1][0] >= line.depth:
            items.pop()
        while para and para[-1] >= line.depth:
            para.pop()
        if not para:
            whole.clear()
        if line.role == "link":
            link = (line.name or "", bool(para))
        opens = (line.role in ITEM_ROLES or (line.role == "link" and _is_card(rows, at))
                 or (line.role in SOFT_ROLES and bool(line.name) and bool(items) and _is_block(rows, at)))
        if opens:
            boxes.append(Box(line.role, len(items), title=line.name or ""))
            opened.append(items[-1][1] if items else None)
            items.append((line.depth, len(boxes) - 1))
        where = " > ".join(s for _, s, _ in stack[-2:])
        tail = raw[line.attrs_span[1]:].strip(": ") if line.attrs_span else ""
        said = [t for t in (line.name, tail) if t]
        if line.role is None:
            said = [raw.split(":", 1)[-1].strip(' "')]
        # words with no role of their own: a text line, or a nameless block that holds only words
        # (`- generic: Thanks`). They are what the item says, never who wrote it
        plain = line.role is None or (line.role in SOFT_ROLES | {"cell", "gridcell"} and not opens and not line.name
                                      and bool(tail))
        if items:  # an item's words are its own: a comment's are not the post's around it
            boxes[items[-1][1]].texts.extend(said)
            if not para and (opens or line.role in WHO_ROLES):  # a text line is words, never an author slot
                boxes[items[-1][1]].names.extend(said)
        # what an item says, each piece whole: a paragraph split into text and links is read as one line,
        # so "so pretty" in "Not" + "so pretty" is never a whole line of its own
        if items and whole:
            b, at_said = whole[-1]
            boxes[b].said[at_said] = " ".join(t for t in (boxes[b].said[at_said], *said) if t)
        elif items and (line.role == "paragraph" or (plain and any(t.strip(' "') for t in said))):
            # a paragraph, or a bare text line (the shape of a body in spans and divs, and the dev site's), is
            # what the item says: rows after it are not its author. A heading may be a name: not said
            box = boxes[items[-1][1]]
            if box.lead is None:
                box.lead = len(box.names)
            box.said.append(" ".join(t.strip(' "') for t in said if t.strip(' "')))
            if line.role == "paragraph":
                whole.append((items[-1][1], len(box.said) - 1))
        if line.role == "paragraph":
            para.append(line.depth)
        if line.role in READ_ROLES or stripped.startswith("- text:"):
            body = line.name or tail or raw.split(":", 1)[-1]
            if body.strip(' "'):
                lines.append(body.strip(' "'))
                if items:
                    boxes[items[-1][1]].lines.append(body.strip(' "'))
        if line.role in CONTEXT_ROLES:
            named = f'{line.role} "{line.name[:WHERE_CHARS]}"' if line.name else line.role
            stack.append((line.depth, named, line.role in DIALOG_ROLES))
        if line.role == "heading" and line.name and stack and '"' not in stack[-1][1]:
            depth, role, dialog = stack[-1]  # a box with no name of its own goes by its heading
            stack[-1] = (depth, f'{role} "{line.name[:WHERE_CHARS]}"', dialog)
        if line.ref and line.actionable and line.role:
            states = tuple(a for a in line.attrs if a in ("pressed", "checked", "disabled", "expanded", "selected"))
            label = f'{line.role} "{line.name or ""}"'
            if states:
                label += " (" + ", ".join(states) + ")"
            if tail:
                label += f" {tail}"
            if where:
                label += f" in {where}"
            elements.append(Element(line.ref, line.role, label, name=line.name or "", where=where, states=states,
                                    in_dialog=any(d for _, _, d in stack), box=items[-1][1] if items else None))
    boxes, elements = _settle(boxes, opened, elements)
    return Page(url=url.group(0) if url else "", signal=signal.group(1) if signal else "unknown", outline=text,
                elements=elements, lines=lines, boxes=boxes)


def lean(outline: str) -> str:
    """The outline a model reads: a pointer shape adds only tokens."""
    return outline.replace(" [cursor=pointer]", "")


def on_site(element: Element, page_url: str, sites: Sequence[str]) -> bool:
    """Does this link stay on the lane's sites? A relative link does; anything else is read from
    its own host, so a lookalike never reaches the question."""
    if not element.href:
        return True
    href = element.href.strip().replace("\\", "/")  # a browser reads a backslash in a web URL as a slash
    try:
        target = urlsplit(href)
        if target.scheme and target.scheme not in ("http", "https"):
            return False
        host = target.hostname
        if host is None and target.scheme:  # `https:/evil.example`: a browser finds the host all the same
            host = urlsplit("//" + href.split(":", 1)[1].lstrip("/")).hostname
    except ValueError:  # a page wrote it, and an address nobody can read must not end the run
        return False
    return host is None or host == urlsplit(page_url).hostname or in_sites(list(sites), host)


def offered(page: Page, sites: Sequence[str], flipped: set[tuple[str, str, str]],
            filling: str | None = None) -> list[Element]:
    """What a model may pick from. An open dialog blocks the page behind it, so while there is one
    only its own controls are on offer (with no usable control in it, the page's are). Words just
    entered belong to their own form: a control that looks the same elsewhere on the page sends
    something else, so it is not on offer until something has been pressed."""
    usable = [e for e in page.elements
              if "disabled" not in e.states and e.key not in flipped and on_site(e, page.url, sites)]
    usable = [e for e in usable if e.in_dialog] or usable
    if filling:
        own = {(e.role, e.name) for e in usable if e.where == filling}
        usable = [e for e in usable if e.where == filling or (e.role, e.name) not in own]
    return usable
