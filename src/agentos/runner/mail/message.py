"""What a stranger sent, made safe to read (docs/EMAIL.md, "Reading").

Every field here is untrusted. Parsing never raises on a bad message: a poll must get past mail that
is broken on purpose, so what cannot be read is a stub marked `unreadable`, with whatever its headers
said, and it never stops a mailbox. Headers come out decoded and on one line, so an encoded word
cannot smuggle a line break into a value used later. HTML becomes text here, and nothing in it is
ever fetched.
"""

from __future__ import annotations

import contextlib
import email
import email.parser
import email.policy
import logging
import re
import sys
import time
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from email.message import EmailMessage, Message
from html.parser import HTMLParser
from typing import Any

log = logging.getLogger("agentos.runner")

MAX_BODY = 200_000
MAX_HTML = 1_000_000      # HTML past this is not read: it is what a size cap is for
MAX_FIELD = 300
MAX_ID = 998              # a header line's own limit, RFC 5322
MAX_ATTACHMENTS = 50
MAX_PARTS = 500
MAX_CONTAINERS = 200      # parts that hold parts, so no nesting gets near the parser's recursion
PARSE_SECONDS = 4.0       # the header parser is quadratic: a long header on purpose would run for hours
CLIP = 200
MESSAGE_ID_RE = re.compile(r"<[^<>\s]{1,900}>")
CONTAINER_RE = re.compile(rb"^content-type:\s*(?:multipart|message)/", re.IGNORECASE | re.MULTILINE)

# Marks that change how text reads without being seen: direction overrides and zero-width characters.
INVISIBLE = dict.fromkeys((0x200B, 0x200E, 0x200F, 0x061C, *range(0x202A, 0x202F), *range(0x2066, 0x206A), 0xFEFF))
SKIPPED_TAGS = frozenset({"script", "style", "head", "title", "template", "noscript", "svg", "object", "iframe"})
BLOCK_TAGS = frozenset({"p", "div", "br", "tr", "table", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6",
                        "blockquote", "hr", "section", "article", "header", "footer", "pre"})
VOID_TAGS = frozenset({"br", "hr", "img", "meta", "link", "input"})


def _encodable(text: str) -> str:
    """Bytes that were no charset come through a parser as lone surrogates, which nothing can store."""
    return text.encode("utf-8", "replace").decode("utf-8")


def one_line(text: Any, limit: int = MAX_FIELD) -> str:
    """A value that stays on one line, whatever it decoded to."""
    out = "".join(" " if unicodedata.category(c) in ("Cc", "Zl", "Zp") else c for c in _encodable(str(text or "")).translate(INVISIBLE))
    return " ".join(out.split())[:limit]


def clean_text(text: str, limit: int = MAX_BODY) -> str:
    """A body as lines: the same marks gone, line ends plain, no other control characters."""
    text = _encodable(text).translate(INVISIBLE).replace("\r\n", "\n").replace("\r", "\n")
    return "".join(c for c in text if c in "\n\t" or unicodedata.category(c) != "Cc")[:limit]


class _Reducer(HTMLParser):
    """HTML to text, in one pass over the tags. Nothing is resolved or fetched: an image is its alt
    text, and a link is its words with the address after them, as text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skipping: list[str] = []
        self.href: str | None = None
        self.link_at = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.skipping:
            if tag == self.skipping[-1] and tag not in VOID_TAGS:
                self.skipping.append(tag)
            return
        if tag in SKIPPED_TAGS:
            self.skipping.append(tag)
            return
        if tag in BLOCK_TAGS:
            self.out.append("\n")
        if tag == "li":
            self.out.append("- ")
        fields = dict(attrs)
        if tag == "img" and fields.get("alt"):
            self.out.append(str(fields["alt"]))
        if tag == "a" and (fields.get("href") or "").lower().startswith(("http://", "https://", "mailto:")):
            self.href, self.link_at = str(fields["href"]), len(self.out)

    def handle_endtag(self, tag: str) -> None:
        if self.skipping:
            if tag == self.skipping[-1]:
                self.skipping.pop()
            return
        if tag == "a" and self.href is not None:
            words = "".join(self.out[self.link_at:]).strip()
            if words and words not in self.href:
                self.out.append(f" ({self.href})")
            elif not words:
                self.out.append(self.href)
            self.href = None
        if tag in BLOCK_TAGS:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            self.out.append(data)


def html_to_text(html: str) -> str:
    parser = _Reducer()
    try:
        parser.feed(html[:MAX_HTML])
        parser.close()
    except Exception:  # what was read so far is still the message
        log.debug("a message's HTML could not be read to its end", exc_info=True)
    lines = [" ".join(line.split()) for line in "".join(parser.out).split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _part_text(part: Message) -> str:
    try:
        return str(part.get_content())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - an unknown charset, or bytes that are no charset at all
        raw = part.get_payload(decode=True)
        return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else ""


def _body(msg: EmailMessage) -> str:
    for kind in ("plain", "html"):
        try:
            part = msg.get_body(preferencelist=(kind,))
        except Exception:  # noqa: BLE001
            part = None
        if part is None:
            continue
        text = _part_text(part)
        text = html_to_text(text) if kind == "html" else text
        if text.strip():
            return clean_text(text)
    return ""


def attachment_name(name: Any) -> str:
    """What a sender called a file. Only ever shown: a path in it means nothing here."""
    return one_line(re.split(r"[/\\]", str(name or ""))[-1], 200) or "unnamed"


def _attachments(msg: EmailMessage) -> tuple[dict[str, str], ...]:
    found: list[dict[str, str]] = []
    try:
        for count, part in enumerate(msg.walk()):
            if count >= MAX_PARTS or len(found) >= MAX_ATTACHMENTS:
                break
            if part.is_multipart():
                continue
            name = part.get_filename()
            if name or part.get_content_disposition() == "attachment":
                found.append({"name": attachment_name(name), "type": one_line(part.get_content_type(), 100)})
    except Exception:
        log.debug("a message's parts could not all be read", exc_info=True)
    return tuple(found)


def header(msg: Message, name: str, limit: int = MAX_FIELD) -> str:
    try:
        return one_line(msg.get(name), limit)
    except Exception:  # noqa: BLE001 - a header the parser cannot make sense of is an empty one
        return ""


def _auto_reply(msg: Message) -> bool:
    value = header(msg, "Auto-Submitted").split(";")[0].strip().lower()
    return bool(value) and value != "no"


@dataclass(frozen=True)
class Parsed:
    sender: str
    to: str
    subject: str
    date: str
    body: str
    message_id: str
    in_reply_to: str
    references: str
    auto_reply: bool
    attachments: tuple[dict[str, str], ...]
    unreadable: bool = False


def _fields(msg: Message, body: str, attachments: tuple[dict[str, str], ...], unreadable: bool) -> Parsed:
    return Parsed(sender=header(msg, "From"), to=header(msg, "To"), subject=header(msg, "Subject"),
                  date=header(msg, "Date"), body=body, message_id=header(msg, "Message-ID", MAX_ID),
                  in_reply_to=header(msg, "In-Reply-To", MAX_ID), references=header(msg, "References", MAX_ID),
                  auto_reply=_auto_reply(msg), attachments=attachments, unreadable=unreadable)


class OutOfTime(BaseException):
    """Not an `Exception`, so the guards inside a parse cannot swallow it and carry on."""


# Where the limit stops a parse: the parsers' own code. Python also runs code on this thread that it
# never lets an exception out of (a weakref callback or a __del__ when the garbage collector runs in the
# middle of a parse): OutOfTime raised there is printed and dropped, and the profile hook is gone with
# it, so the parse would run on for hours. Those are left alone; the parser's next call stops it.
PARSER_MODULES = ("email", "html", __name__)


@contextlib.contextmanager
def time_limit(seconds: float) -> Iterator[None]:
    """Stops pure Python on this thread at the next call into a parser after `seconds`. A thread cannot be
    killed and a socket deadline does not reach it, and the parsers are all Python, so this is what ends them."""
    end, before = time.monotonic() + seconds, sys.getprofile()

    def check(frame: Any, event: str, _arg: Any) -> None:
        if event == "call" and time.monotonic() > end and \
                str(frame.f_globals.get("__name__", "")).startswith(PARSER_MODULES):
            raise OutOfTime

    sys.setprofile(check)
    try:
        yield
    finally:
        sys.setprofile(before)


def _stub(raw: bytes) -> Parsed:
    """What cannot be read as a message: its headers if they can be read, and nothing else."""
    try:
        with time_limit(PARSE_SECONDS / 4):
            return _fields(email.parser.BytesParser(policy=email.policy.default).parsebytes(raw, headersonly=True), "", (), True)
    except (Exception, OutOfTime):  # noqa: BLE001
        return Parsed("", "", "", "", "", "", "", "", False, (), True)


def parse(raw: bytes) -> Parsed:
    """Never raises, whatever `raw` is: too much nesting, too long a parse, or a parser's own failure
    gives a stub."""
    try:
        if len(CONTAINER_RE.findall(raw)) > MAX_CONTAINERS:
            return _stub(raw)
        with time_limit(PARSE_SECONDS):
            msg: EmailMessage = email.message_from_bytes(raw, policy=email.policy.default)  # type: ignore[assignment]
            return _fields(msg, _body(msg), _attachments(msg), False)
    except (Exception, OutOfTime):  # RecursionError, OverflowError, and whatever a stranger finds next
        log.debug("a message could not be read", exc_info=True)
        return _stub(raw)


def message_ids(text: str) -> list[str]:
    """The ids in a header that came from outside, each one whole: what goes in a reply's headers."""
    return MESSAGE_ID_RE.findall(text or "")


def clip(text: str, limit: int = CLIP) -> str:
    return one_line(text, limit)
