"""Was the element the run acted on the one the request named?

A toggle that flipped proves the click landed; it does not prove it landed on the right post. A model
picks the target, so a Like on the post next door flips `aria-pressed` just as well. An action counts
toward done_verified only when the item it sits in (a post, a comment, a result) is anchored to
something the request itself named:

- a target the caller passed (`Target`: author, permalink, one whole line of text, or the sha256 of one);
- else what the goal names in a form a page cannot blur: an @handle, a link, words in quotes;
- else a word of the goal found in that item and in no other item like it on the page.

Every anchor the request names must match (the caller's target and the goal's alike), so "@mira's
post about tomatoes" on mira's other post does not pass. When another item holds those anchors too
(a post that mentions @mira, every post on mira's own profile), a goal word only the acted item holds
must tell them apart as well. Items nest: a comment in a post, a reply in a comment, a quoted post in
a post. Each holds only its own words, and the request is matched at the most specific level: the item
acted on must hold an anchor itself (the items around it may hold the rest), and a goal word that only
an item inside it holds means the request named that inner item. Two controls like each other in one
item, or in no item at all, tell nothing apart. What this returns is node-written: it quotes the
request, never the page.

A quote verifies only as the whole of one line the item says (a paragraph, a text line, a heading or its
own name), after normalising case, spacing and the punctuation at its ends. Part of a line may sit in
words that turn its meaning ("So pretty" in "Not so pretty"), so a quote any item in the chain holds only
as part of a line leaves the action unverified.

An author named by a display name alone never verifies: two people can share one. It still narrows
which items fit, but the request must also name something that pins one item: an @handle or u/name the
item shows as its author, a profile link, a permalink, or a whole line it says. A handle on a page that
shows only names (no handle, no profile link) cannot be checked, so it counts as a name.

A reshare whose quoted post is plain rows inside it (a paragraph, a bare link), not an item, mixes the
quote's author, words and links with its own. When the item shows signs of that (two authors, a line
quoting someone, links to two posts, a reshare word in its name with no item inside), nothing it holds
verifies it either way. Only its own page does: the request names the page's address, and it is the
first post there. Who wrote an item is read only from its author slot: its own name, a name link, a
heading or a profile link before its first words, and not about someone else ("Replying to @beata",
"Liked by @ieva"). Its words (a paragraph, a bare text line) are never an author slot: "@beata thanks"
there is a mention, and an item with no author slot has no known author. A person named elsewhere outside
its words, or a line of its words that only names a person ("Beata @beata"), blurs it the same way. A link is the item's own permalink only when it is named by a time, by the
item's own name or by nothing: a link named by words may lead to the quoted post.

Every target field, the author the goal names for the item acted on ("@beata's comment"), and a quote
before the goal's second item word are the acted item's own: an item around it holding them is not
enough. A control in no item never verifies what sits in an item.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from agentos.pilot.page import Box, Element, Page

HANDLE_RE = re.compile(r"(?<![\w@])@(\w[\w.-]{0,63})")
REDDIT_RE = re.compile(r"(?<![\w/@])u/(\w[\w-]{0,63})")   # Reddit writes a handle as u/name
# signs an item holds someone else's post as plain rows: a reshare word, a line that quotes a person
# ("@beata: ...", "Beata wrote:")
RESHARE_RE = re.compile(r"\b(?:re-?(?:share[sd]?|posts?|posted|tweets?|tweeted)|quoted|quote (?:post|tweet)"
                        r"|shared (?:a|this|the|their|his|her) post)\b", re.IGNORECASE)
QUOTE_LINE_RE = re.compile(r"(?:^|\s)(?:rt\s+)?(?:@[\w.-]+|u/[\w-]+)\s*:(?:\s|$)|\b(?:wrote|said|posted|shared)\s*:",
                           re.IGNORECASE)
# a line that only names a person ("Beata @beata", "@beata", "u/beata"): in an item's words, the byline of
# a post it quotes, since an item's own author shows in its author slot, not in what it says
BYLINE_RE = re.compile(r"^(?:[^\W\d_][\w'.-]*\s+){0,3}\(?(?:@(\w[\w.-]*)|u/(\w[\w-]*))\)?$")
# a row about someone other than the author: "Replying to @beata", "Liked by @ieva and 3 others", "via @ieva"
RELATION_RE = re.compile(r"\b(?:replying|replied|reply|repl(?:y|ies) to|in reply|liked|likes|loved|via|with|and \d+|"
                         r"others?|follows?|followed|following|mentioned|tagged|commented|reacted|suggested|"
                         r"re-?shared|re-?posted|retweeted|shared|quoted)\b", re.IGNORECASE)
# a link's own name when it is the item's permalink: a time ("3h", "2 hours ago", "Nov 7")
TIME_RE = re.compile(r"^(?:\d+\s*(?:s|m|h|d|w|y|mo|sec|secs|min|mins|hr|hrs|hour|hours|day|days|week|weeks|month|"
                     r"months|year|years)\.?(?:\s+ago)?|just now|now|yesterday|today|\d{4}-\d\d-\d\d.*|"
                     r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:,?\s+\d{4})?|"
                     r"\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?(?:\s+\d{4})?)$",
                     re.IGNORECASE)
# where a site keeps a person's own page: /u/ieva, /user/ieva, /in/ieva, /@ieva. The last part is their id
PROFILE_DIRS = frozenset(["u", "user", "users", "profile", "profiles", "people", "in", "member", "members",
                          "author", "authors", "channel", "c"])
URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")
QUOTED_RE = re.compile(r"\"([^\"]{4,200})\"|“([^”]{4,200})”|«([^»]{4,200})»")
WORD_RE = re.compile(r"\w{3,}")
STOP = frozenset(["the", "and", "for", "with", "that", "this", "from", "into", "onto", "about", "post", "posts", "like", "likes", "comment", "comments", "reply", "follow", "unfollow", "share", "repost", "page", "profile", "feed", "his", "her", "their", "its", "them", "they", "you", "your", "our", "one", "first", "last", "latest", "newest", "top", "new", "please", "make", "sure", "just", "only", "also", "then", "there", "here", "what", "which", "who", "whom", "whose", "when", "where", "button", "click", "press", "open", "tap", "give", "leave", "write", "send", "say", "says", "said", "account", "user", "people", "person", "thing", "item", "items"])


REPLY_WORDS = frozenset(["reply", "replies", "answer", "answers", "response", "responses"])
COMMENT_WORDS = frozenset(["comment", "comments"])
POST_WORDS = frozenset(["post", "posts", "photo", "photos", "picture", "pic", "video", "tweet", "status", "story",
                        "reel", "article", "thread", "pin", "submission", "update", "share", "repost", "reshare"])


_ITEM = "|".join(sorted(REPLY_WORDS | COMMENT_WORDS | POST_WORDS, key=len, reverse=True))
# "Beata's comment", "Jonas's lake photo", "the comment by Beata": the name of whoever wrote that item
POSSESSIVE_RE = re.compile(rf"(?<![\w@.-])([^\W\d_][\w.-]*?)['’]s?\s+(?:[\w-]+\s+){{0,2}}?(?:{_ITEM})\b", re.IGNORECASE)
BY_RE = re.compile(rf"\b(?:{_ITEM})\s+(?:by|from)\s+[\"“«']?(?:@)?([^\W\d_][\w.-]*)", re.IGNORECASE)
NOT_NAMES = frozenset(["today", "yesterday", "tonight", "everyone", "everybody", "someone", "somebody", "nobody",
                       "it", "one", "other", "another", "friend", "friends", "mom", "dad"])


def _kind(goal: str) -> str | None:
    """What the goal acts on, by its first word for an item: "reply", "comment", "post" or None.
    "Like @beata's comment on Jonas's photo" acts on a comment."""
    for word in re.findall(r"\w+", goal.casefold()):
        if word in REPLY_WORDS:
            return "reply"
        if word in COMMENT_WORDS:
            return "comment"
        if word in POST_WORDS:
            return "post"
    return None


@dataclass(frozen=True)
class Target:
    """What a caller names as the item to act on. Any field set must match the acted item."""
    author: str | None = None        # an @handle, a profile link, or a display name (which alone never verifies)
    permalink: str | None = None     # the item's own link
    text: str | None = None          # one whole line the item says (a paragraph), as written
    text_sha256: str | None = None   # sha256 (hex) of one line of the item, whitespace collapsed

    @property
    def empty(self) -> bool:
        return not (self.author or self.permalink or self.text or self.text_sha256)

    @classmethod
    def parse(cls, given: dict[str, Any] | None) -> Target | None:
        if not given:
            return None
        keys = {"author", "permalink", "text", "text_sha256"}
        if not isinstance(given, dict) or set(given) - keys or not all(isinstance(v, str) for v in given.values()):
            raise ValueError("target takes author, permalink, text and text_sha256, each a string")
        target = cls(**{k: v.strip() or None for k, v in given.items()})
        if target.text_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", target.text_sha256):
            raise ValueError("target.text_sha256 is 64 lowercase hex characters")
        return None if target.empty else target

    def public(self) -> dict[str, str]:
        return {k: v for k, v in (("author", self.author), ("permalink", self.permalink), ("text", self.text),
                                  ("text_sha256", self.text_sha256)) if v}


@dataclass(frozen=True)
class Anchor:
    kind: str     # author, permalink, text or text_sha256
    value: str
    source: str   # target (the caller passed it) or goal (the goal names it)
    own: bool = False  # it names the acted item itself, not one around it: every target field; "@beata's comment"


def _norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split()).casefold()


def _whole(text: str) -> str:
    """A line as said, for comparing whole lines: case, spacing and the punctuation at its ends aside."""
    return re.sub(r"^[\W_]+|[\W_]+$", "", _norm(text))


def says_whole(quote: str, box: Box) -> bool:
    """Is the quote the whole of one paragraph the item says? A quote that is only part of one may sit in
    words that turn its meaning ("So pretty" in "Not so pretty"); a heading, a name or a bare text line
    may be who wrote it, not what it says."""
    want = _whole(quote)
    return bool(want) and any(_whole(line) == want for line in box.said)


def line_sha256(line: str) -> str:
    return hashlib.sha256(" ".join(line.split()).encode()).hexdigest()


def anchors_of(goal: str, target: Target | None, entered: tuple[str, ...] = ()) -> list[Anchor]:
    """What the request names. Quoted words that are a value to enter are what to write, not where."""
    goal = unicodedata.normalize("NFC", goal)
    out: list[Anchor] = []
    if target is not None:
        out += [Anchor(kind, value, "target", own=True) for kind, value in target.public().items()]
    for url in URL_RE.findall(goal):
        out.append(Anchor("permalink", url.rstrip(".,;:!?"), "goal"))
    bare = URL_RE.sub(" ", goal)
    for handle in HANDLE_RE.findall(bare):
        out.append(Anchor("author", "@" + handle.rstrip(".-"), "goal"))
    for handle in REDDIT_RE.findall(bare):
        out.append(Anchor("author", "u/" + handle.rstrip("-"), "goal"))
    typed = {_norm(t) for t in entered if t}
    items = [m.start() for m in re.finditer(rf"\b(?:{_ITEM})\b", bare, re.IGNORECASE)]
    for found in QUOTED_RE.finditer(bare):
        quoted = next(g for g in found.groups() if g)
        if _norm(quoted) not in typed:
            # a quote before the goal's second item word is about the item acted on: 'Beata's comment "So
            # pretty" on Jonas's photo'; after it, about the item around: 'the comment on the post "Lake"'
            mine = len(items) < 2 or found.start() < items[1]
            out.append(Anchor("text", quoted, "goal", own=mine))
    unquoted = QUOTED_RE.sub(" ", bare)
    named = {_norm(_handle(a.value) or a.value) for a in out if a.kind == "author"}
    for name in [*POSSESSIVE_RE.findall(unquoted), *BY_RE.findall(unquoted)]:
        name = name.rstrip(".-")
        if len(name) > 1 and _norm(name) not in named and name.casefold() not in STOP | NOT_NAMES:
            named.add(_norm(name))
            out.append(Anchor("author", name, "goal"))
    mine = _author_of_first(unquoted)
    return [Anchor(a.kind, a.value, a.source, own=True)
            if a.kind == "author" and mine and _norm(_handle(a.value) or a.value) == mine else a for a in out]


def _author_of_first(goal: str) -> str | None:
    """Whose is the item the goal acts on: "@beata" in "Like @beata's comment on Jonas's photo", "beata"
    in "Like the comment by Beata". The author named for an item around it is not."""
    first = re.search(rf"\b(?:{_ITEM})\b", goal, re.IGNORECASE)
    if first is None:
        return None
    before = re.search(r"(?<![\w.-])(?:@|u/)?([^\W\d_][\w.-]*?)['’]s?\s+(?:[\w-]+\s+){0,2}?$", goal[:first.start()])
    after = re.match(r"\s+(?:by|from)\s+(?:@|u/)?([^\W\d_][\w.-]*)", goal[first.end():], re.IGNORECASE)
    found = before or after
    return _norm(found.group(1).rstrip(".-")) if found else None


def _path(url: str) -> tuple[str | None, str, str]:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None, "", ""
    query = "&".join(sorted(q for q in parts.query.split("&") if q))  # /item?id=111 is not /item?id=222
    return (parts.hostname or None), parts.path.rstrip("/") or "/", query


def _same_link(a: str, b: str) -> bool:
    """Is `b` the link `a` names? A fragment in `a` (/p/1#c3, one comment of the post) must be in `b`."""
    (host_a, path_a, query_a), (host_b, path_b, query_b) = _path(a), _path(b)
    if path_a in ("", "/") or path_a != path_b or query_a != query_b:
        return False
    fragment = urlsplit(a.strip()).fragment if "#" in a else ""
    if fragment and fragment != (urlsplit(b.strip()).fragment if "#" in b else ""):
        return False
    return host_a is None or host_b is None or host_a.removeprefix("www.") == host_b.removeprefix("www.")


def _handle(value: str) -> str | None:
    """The handle in "@ieva" or "u/ieva", or None for a display name or a link."""
    value = value.strip()
    if value.startswith("@"):
        return value[1:] or None
    if value[:2].casefold() == "u/" and "/" not in value[2:]:
        return value[2:] or None
    return None


def _is_link(value: str) -> bool:
    return value.startswith(("/", "http://", "https://")) or ("/" in value and _handle(value) is None)


def _slug(href: str) -> str | None:
    """The person a profile link names: /u/ieva and https://site/@ieva name ieva; /p/55 names no one."""
    parts = [p for p in _path(href)[1].split("/") if p]
    if len(parts) == 1 and parts[0].startswith("@") and len(parts[0]) > 1:
        return _norm(parts[0][1:])
    if len(parts) == 2 and parts[0].casefold() in PROFILE_DIRS:
        return _norm(parts[1])
    return None


def _ids_in(rows: list[str], links: list[str]) -> set[str]:
    who = _norm("\n".join(rows))
    ids = {h.rstrip(".-") for h in re.findall(r"(?<![\w@])@(\w[\w.-]*)", who)}
    ids |= {h.rstrip("-") for h in REDDIT_RE.findall(who)}
    return ids | {s for s in map(_slug, links) if s}


def _author_rows(box: Box) -> tuple[list[str], list[str]]:
    """The rows and links that may say who wrote the item: outside its paragraphs, before its first one,
    and not about someone else ("Replying to @beata", "Liked by @ieva")."""
    lead = box.names if box.lead is None else box.names[:box.lead]
    rows = [r for r in lead if not RELATION_RE.search(r)]
    links = [h for name, h, in_para, after in box.links if not in_para and not after and not RELATION_RE.search(name)]
    return rows, links


def ids_of(box: Box) -> set[str]:
    """Who the item shows as its author in a form a site keeps to one person: an @handle or u/name, or a
    profile link, where an author shows (see _author_rows). A display name is not one: two people can share it."""
    return _ids_in(*_author_rows(box))


def _others(box: Box) -> set[str]:
    """People the item names outside its paragraphs who are not its author: after its words, or in a row
    about someone else."""
    rows = [r for r in box.names if r not in _author_rows(box)[0]]
    links = [h for name, h, in_para, after in box.links if not in_para and (after or RELATION_RE.search(name))]
    return _ids_in(rows, links) - ids_of(box)


def _bylines(box: Box) -> set[str]:
    """People a line of the item's words names and nothing else ("Beata @beata"): see BYLINE_RE."""
    def bare(line: str) -> str:  # "Ieva · @ieva · 2h" is "Ieva @ieva": separators and a time aside
        parts = [p for p in re.split(r"\s*[·•|]\s*", _norm(line)) if p and not TIME_RE.match(p.strip(" .,"))]
        return " ".join(parts).strip(" .,:;!?")

    found = (BYLINE_RE.match(bare(line)) for line in (*box.said, *box.lines) if line != box.title)
    return {(m.group(1) or m.group(2)).rstrip(".-") for m in found if m}


def _own_hrefs(box: Box) -> list[str]:
    """The links that may be the item's own permalink: one named by a time ("3h"), by the item's own
    name, or by nothing. A link named by words ("Slow mornings are the best") may lead to another post."""
    return [h for name, h, _, _ in box.links
            if not name.strip() or TIME_RE.match(name.strip()) or _norm(name) == _norm(box.title)]


def _author(anchor: Anchor, box: Box) -> str:
    """How the item matches an author: "sure" (a handle or profile link it shows as its author), "name"
    (only a name it shows, which two people can share) or "" (not at all)."""
    if _is_link(anchor.value):
        return "sure" if any(_same_link(anchor.value, h) for h in _author_rows(box)[1]) else ""
    handle, ids = _handle(anchor.value), ids_of(box)
    if handle is not None and ids:  # a handle the request names, among those shown: "@mira" is not "@mira.bakes"
        return "sure" if _norm(handle).rstrip(".-") in ids else ""
    who = _norm("\n".join(_author_rows(box)[0]))  # an item that shows no author row names no one
    name = re.escape(_norm(handle or anchor.value))
    return "name" if re.search(rf"(?<![\w.-]){name}(?![\w]|[.-]\w)", who) else ""


def _post_link(href: str, page: Page) -> str | None:
    """The path of a link to a post on this site (/p/55, /status/9, /r/x/comments/7), or None."""
    host, path, query = _path(href)
    own = urlsplit(page.url).hostname
    if _slug(href) is not None or not re.search(r"\d", path + query):
        return None
    if host is not None and own is not None and host.removeprefix("www.") != own.removeprefix("www."):
        return None
    return path + ("?" + query if query else "")


def blur(index: int, page: Page) -> str | None:
    """Does the item hold another post as plain rows of its own (a reshare whose quote is a paragraph or a
    bare link, not an item)? Then its words, author and links mix with the quote's. What it shows, in
    node words, or None."""
    box = page.boxes[index]
    if len(ids_of(box)) > 1:
        return "more than one author"
    if _others(box) or _bylines(box) - ids_of(box):
        return "another person named apart from its author"
    if any(QUOTE_LINE_RE.search(_norm(line)) for line in (*box.said, *box.names)):
        return "a line that quotes someone"
    posts = {p for p in (_post_link(h, page) for h in box.hrefs) if p}
    if len({p for p in posts if not any(p.startswith(q + "/") for q in posts)}) > 1:  # /p/70/share is /p/70
        return "links to more than one post"
    quoted = any(b.parent == index and b.role not in ("comment", "treeitem")
                 and not re.match(r"(?:comment|reply)\b", b.title, re.IGNORECASE) for b in page.boxes)
    rows = [r for r in (box.title, *box.names, *box.said) if len(re.findall(r"[^\W\d_]+", r)) > 1]
    if not quoted and any(RESHARE_RE.search(r) for r in rows):  # "Repost" alone is a button's word
        return "a reshare"
    return None


def _blurred(what: str) -> dict[str, Any]:
    return {"anchored": False, "by": f"the item acted on holds {what}, as if it quotes a post or a person with no "
                                     "structure of its own, so what the request names may be the quoted post and not "
                                     "this one, or the other way round: pass target with the post's permalink and act "
                                     "on it on its own page"}


def _sure(anchor: Anchor, box: Box, index: int, page: Page) -> bool:
    """Does the item hold it in a form that names one item: not a display name, not part of a line?"""
    if anchor.kind == "author":
        return _author(anchor, box) == "sure"
    return _matches(anchor, box, index, page, strict=True)


def _matches(anchor: Anchor, box: Box, index: int, page: Page, strict: bool = False) -> bool:
    """Does the item hold it? `strict`: a quote only as the whole of a line it says."""
    if anchor.kind == "author":  # who wrote it, not who it mentions
        return _author(anchor, box) != ""
    if anchor.kind == "text":
        return says_whole(anchor.value, box) if strict else _norm(anchor.value) in _norm(box.text)
    if anchor.kind == "text_sha256":
        return any(line_sha256(line) == anchor.value for line in (*box.said, *box.lines))  # not a link's name
    if any(_same_link(anchor.value, href) for href in _own_hrefs(box)):
        return True
    # on the item's own page the item seldom links to itself: the first top-level item like it is it
    # (check() then still wants a goal word when other items stand beside it)
    first = next((i for i, b in enumerate(page.boxes) if b.level == 0 and b.role == box.role), None)
    return _same_link(anchor.value, page.url) and box.level == 0 and index == first


def _bare(name: str) -> str:
    return re.sub(r"\d+", "", name).strip().casefold()


def _alike(element: Element, page: Page, box: int | None = None) -> bool:
    """Another control like this one on the page. Given `box`, one in that same item that does the
    same (the same first word: "Like" and "Like comment"), as when a comment is not an item of its own."""
    name = _bare(element.name)
    first = name.split()[:1]
    return any(e.ref != element.ref and e.role == element.role
               and (_bare(e.name) == name if box is None else (e.box == box and _bare(e.name).split()[:1] == first))
               for e in page.elements)


def _on_page(anchor: Anchor, page: Page) -> bool:
    """Is it anywhere on the page? For a control in no item, which nothing else checks."""
    if anchor.kind == "permalink":
        return any(_same_link(anchor.value, h) for h in _page_box(page).hrefs)
    return _matches(anchor, _page_box(page), -1, page)


def _page_box(page: Page) -> Box:
    """The whole page read as one item, for a control that sits in none."""
    # no row on it is known to say who wrote it, and only the page's own address is its permalink
    hrefs = re.findall(r"^\s*- /url:\s*(\S+)", page.outline, re.MULTILINE) + [page.url]
    return Box("page", 0, texts=[page.outline], lines=list(page.lines), hrefs=hrefs,
               links=[("", page.url, False, False)], said=[line for b in page.boxes for line in b.said])


def _unsure(anchors: list[Anchor], index: int, box: Box, page: Page) -> dict[str, Any] | None:
    """With nothing named in a form that pins the item acted on itself, an author named by a name alone is
    not enough. A handle that pins the post around it does not pin a comment in it that only a name names."""
    if any(_sure(a, box, index, page) for a in anchors):
        return None
    who = next((a for a in anchors if a.kind == "author"), None)
    if who is None:
        return None
    if _handle(who.value) is None:
        return {"anchored": False, "by": f"the {who.source} names the author {who.value!r} by a display name only, "
                                         "and two people can share one: pass their @handle or profile link, or the "
                                         "item's permalink, to make it verifiable"}
    return {"anchored": False, "by": f"the item shows no @handle or profile link to check the author {who.value!r} "
                                     f"the {who.source} names, only a name two people can share: pass the item's "
                                     "permalink or its whole text to make it verifiable"}


def _part(anchors: list[Anchor], boxes: list[Box]) -> Anchor | None:
    """A quote one of these items holds only as part of a longer line. Then it may be that item the quote
    was meant for, in words that turn its meaning, even when an item around it says the quote whole."""
    return next((a for a in anchors if a.kind == "text"
                 and any(_norm(a.value) in _norm(b.text) and not says_whole(a.value, b) for b in boxes)), None)


def _partial(anchor: Anchor) -> dict[str, Any]:
    return {"anchored": False, "by": f"the {anchor.source}'s quote {anchor.value!r} is only part of what the item "
                                     "says, and the words around it may change its meaning: quote its whole text, "
                                     "or pass target with its permalink or text_sha256"}


def _held(anchor: Anchor, index: int, page: Page, strict: bool = False) -> bool:
    """The item itself holds it, not an item inside it."""
    return _matches(anchor, page.boxes[index], index, page, strict)


def _around(anchor: Anchor, index: int, page: Page, strict: bool = False) -> bool:
    """The item or an item around it holds it: "@beata's comment on @jonas's photo" is in jonas's post."""
    return any(_held(anchor, i, page, strict) for i in (index, *page.ancestors(index)))


def _fits(anchors: list[Anchor], index: int, page: Page, strict: bool = False) -> bool:
    return (all(_around(a, index, page, strict) for a in anchors)
            and any(_held(a, index, page, strict) for a in anchors))


def _words(text: str) -> set[str]:
    return {w.casefold() for w in WORD_RE.findall(text)}


def _goal_words(goal: str) -> set[str]:
    return _words(URL_RE.sub(" ", goal)) - STOP


def _inside(goal: str, index: int, page: Page) -> str | None:
    """A word of the goal that only an item inside the acted one holds: then the goal names that item."""
    mine = _words(page.context(index))
    inner: set[str] = set()
    for i, _ in enumerate(page.boxes):
        if page.within(i, index):
            inner |= _words(page.boxes[i].text)
    found = sorted((_goal_words(goal) & inner) - mine)
    return found[0] if found else None


def check(goal: str, target: Target | None, page: Page, element: Element | None,
          entered: tuple[str, ...] = ()) -> dict[str, Any] | None:
    """{"anchored": bool, "by": why} for an action on `element`, or None when nothing needs telling apart.

    Items nest (a reply in a comment in a post, a quoted post in a post), and each holds only its own
    words. What the request names is matched at the most specific level: the item acted on must hold
    at least one anchor itself (the items around it may hold the rest), and a word of the goal that
    only an item inside it holds means the goal named that inner item, not this one."""
    if element is None:
        return None
    if element.box is None:
        return _check_outside_items(goal, target, page, element, entered)
    index = element.box
    if _alike(element, page, index):
        return {"anchored": False, "by": "the item acted on holds another control like this one, so nothing "
                                         "tells which of the two the request meant"}
    nesting = _wrong_level(_kind(goal), page, element, index)
    if nesting:
        return nesting
    anchors = anchors_of(goal, target, entered)
    blurred = _blurred_around(anchors, index, page)
    if blurred:
        return blurred
    others = [i for i in range(len(page.boxes)) if i != index]
    deeper = _inside(goal, index, page)
    if anchors:
        return _check_anchors(goal, anchors, page, element, index, others, deeper)
    if deeper:
        return _named_deeper(deeper)
    word = _unique_word(goal, index, others, page)
    if word:
        return {"anchored": True, "by": f"only the item acted on holds the goal's word {word!r}"}
    return {"anchored": False, "by": "nothing the goal names tells the item acted on apart from the others "
                                     "on the page: pass target to name it"}


def _check_outside_items(goal: str, target: Target | None, page: Page, element: Element,
                         entered: tuple[str, ...]) -> dict[str, Any] | None:
    """check() for a control that sits in no item: what is named must be on the page and in no item."""
    named = anchors_of(goal, target, entered)
    absent = [a for a in named if not _on_page(a, page)]
    if absent:
        return {"anchored": False, "by": f"the page does not hold the {absent[0].kind} {absent[0].value!r} "
                                         f"the {absent[0].source} names"}
    inner = next((a for a in named if not (a.kind == "permalink" and _same_link(a.value, page.url))
                  and any(_held(a, i, page) or (a.kind == "permalink" and any(_same_link(a.value, h)
                                                                             for h in page.boxes[i].hrefs))
                          for i in range(len(page.boxes)))), None)
    if inner:  # what is named sits in an item, and the control acted on sits in none
        return {"anchored": False, "by": f"the {inner.kind} {inner.value!r} the {inner.source} names is in an "
                                         "item the control acted on does not sit in"}
    part = _part(named, [_page_box(page)])
    if part:
        return _partial(part)
    unsure = _unsure(named, -1, _page_box(page), page)
    if unsure:
        return unsure
    if not _alike(element, page):
        return None
    return {"anchored": False, "by": "the page holds another control like this one and no post around "
                                     "either, so nothing tells them apart"}


def _wrong_level(kind: str | None, page: Page, element: Element, index: int) -> dict[str, Any] | None:
    """The goal names a post, a comment or a reply, and the item acted on may sit at another level."""
    box = page.boxes[index]
    if kind in ("comment", "reply") and box.level == 0 and box.role not in ("comment", "treeitem"):
        return {"anchored": False, "by": f"the goal names a {kind}, and the item acted on sits in no other item, "
                                         f"so it may be the post itself: pass target to name it"}
    nested_in = f"{box.title} {element.where}"
    if kind == "post" and box.level > 0 and (box.role in ("comment", "treeitem")
                                             or re.search(r"\b(?:comment|repl)", nested_in, re.IGNORECASE)):
        return {"anchored": False, "by": "the goal names a post, and the item acted on is a comment or a reply "
                                         "inside one: pass target to name it"}
    parent = page.boxes[box.parent] if box.parent is not None else None
    if kind == "reply" and parent is not None and parent.level == 0 and parent.role not in ("comment", "treeitem") \
            and not re.match(r"(?:comment|repl)", parent.title, re.IGNORECASE):
        return {"anchored": False, "by": "the goal names a reply, and the item acted on sits right in a post, so it "
                                         "may be a comment, not a reply: pass target to name it"}
    if kind == "reply" and any(page.within(i, index) for i in range(len(page.boxes))):
        return {"anchored": False, "by": "the goal names a reply, and the item acted on has replies under it, "
                                         "so it may be the comment they answer: pass target to name it"}
    return None


def _blurred_around(anchors: list[Anchor], index: int, page: Page) -> dict[str, Any] | None:
    """The item acted on, or the only items around it that hold an anchor, are blurred (a reshare)."""
    box = page.boxes[index]
    # a reshare whose quote is plain rows: only the post's own page, where it is the first post, tells
    first = next((i for i, b in enumerate(page.boxes) if b.level == 0 and b.role == box.role), None)
    own = index == first and _post_link(page.url, page) is not None and any(
        a.kind == "permalink" and _same_link(a.value, page.url) for a in anchors)
    what = blur(index, page)
    if what and not own:
        return _blurred(what)
    for a in anchors:  # an anchor only a blurred item around it holds may be the quote's
        holders = [i for i in page.ancestors(index) if _held(a, i, page)]
        if holders and not _held(a, index, page) and all(blur(i, page) for i in holders):
            return _blurred(blur(holders[0], page) or "")
    return None


def _named_deeper(deeper: str) -> dict[str, Any]:
    return {"anchored": False, "by": f"the goal's word {deeper!r} is in an item inside the one acted on, "
                                     "so the request names that item"}


def _check_anchors(goal: str, anchors: list[Anchor], page: Page, element: Element, index: int,
                   others: list[int], deeper: str | None) -> dict[str, Any]:
    """check() when the request names anchors: the item must hold them, then no rival may."""
    box = page.boxes[index]
    named = ", ".join(f"{a.kind} {a.value!r}" for a in anchors)
    not_held = _anchors_not_held(anchors, page, index, named, deeper)
    if not_held:
        return not_held
    # an item matched only because the page is the link's own page could be any item on it (a
    # feed's address names no post): then every item beside it is a rival
    by_page = any(a.kind == "permalink" and not any(_same_link(a.value, h) for h in _own_hrefs(box))
                  for a in anchors)
    if by_page and any(e.box is None and e.role == element.role and _bare(e.name) == _bare(element.name)
                       for e in page.elements):
        return {"anchored": False, "by": "the page's own address names the item, but a control like this one "
                                         "sits outside every item, so the post itself may be that one"}
    # a rival holds what is named as the acted item does: a quote as a whole line
    rivals = [i for i in others if (by_page and page.boxes[i].role == box.role) or _fits(anchors, i, page, True)]
    if not rivals:
        return {"anchored": True, "by": f"the item acted on holds the {named} the request names"}
    names = frozenset(w for a in anchors if a.kind == "author" and _handle(a.value) is None for w in _words(a.value))
    word = _unique_word(goal, index, rivals, page, names)
    if word:
        return {"anchored": True, "by": f"the item acted on holds the {named} the request names, and only it "
                                        f"holds the goal's word {word!r}"}
    return {"anchored": False, "by": f"other items on the page hold the {named} too, and nothing in the "
                                     "goal tells the one acted on apart: pass target to name it"}


def _anchors_not_held(anchors: list[Anchor], page: Page, index: int, named: str,
                      deeper: str | None) -> dict[str, Any] | None:
    """Why the item acted on does not hold what the request names, or None when it does."""
    missed = [a for a in anchors if not _around(a, index, page)]
    if missed:
        return {"anchored": False, "by": f"the item acted on does not hold the {missed[0].kind} "
                                         f"{missed[0].value!r} the {missed[0].source} names"}
    if not any(_held(a, index, page) for a in anchors):
        return {"anchored": False, "by": f"only an item around the one acted on holds the {named} the "
                                         "request names, not the item itself"}
    loose = next((a for a in anchors if a.own and not _held(a, index, page)), None)
    if loose:  # every target field, and the author the goal names for the item itself, are the item's own
        return {"anchored": False, "by": f"the {loose.source} names the {loose.kind} {loose.value!r} for the item "
                                         "acted on itself, and only an item around it holds that"}
    if deeper:
        return _named_deeper(deeper)
    part = _part(anchors, [page.boxes[i] for i in (index, *page.ancestors(index))])
    if part:
        return _partial(part)
    return _unsure(anchors, index, page.boxes[index], page) or None


def _unique_word(goal: str, index: int, others: list[int], page: Page,
                 skip: frozenset[str] = frozenset()) -> str | None:
    """A word of the goal the item (or an item around it) holds and no other item does. An item inside
    the acted one is read without the acted one's words, which it only sits in."""
    mine = _words(page.context(index))
    seen: set[str] = set()
    for other in others:
        if page.within(other, index):
            seen |= _words("\n".join(page.boxes[i].text for i in (other, *page.ancestors(other))
                                     if i == other or page.within(i, index)))
        else:
            seen |= _words(page.context(other))
    unique = sorted((_goal_words(goal) & mine) - seen - skip)
    return unique[0] if unique else None
