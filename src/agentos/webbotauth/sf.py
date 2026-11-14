"""The part of RFC 8941 (Structured Field Values for HTTP) that Web Bot Auth headers use.

`Signature-Input`, `Signature` and the dictionary form of `Signature-Agent` are Dictionaries; the
legacy `Signature-Agent` is a bare String Item. Covered components are an Inner List of Strings
with Parameters. This parser and serializer handle those shapes (strings, tokens, integers,
decimals, byte sequences, booleans, inner lists, parameters) and refuse anything else, so a
malformed header is an error rather than a guess. Ported from the Phase 0 spike, where the
serializer was checked byte for byte against RFC 9421 and the protocol draft's test vectors.
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any

_KEY = re.compile(r"[a-z*][a-z0-9_\-.*]*")
_NUMBER = re.compile(r"-?\d{1,15}(\.\d{1,3})?")
_TOKEN = re.compile(r"[A-Za-z*][A-Za-z0-9:/!#$%&'*+\-.^_`|~]*")

Params = dict[str, Any]
Member = tuple[Any, Params]  # (bare item or inner list, parameters)


class Token(str):
    """An sf-token (serialized without quotes), as opposed to an sf-string."""


class SFError(ValueError):
    """A structured header that does not parse, or a value that cannot be serialized."""


class _Parser:
    def __init__(self, text: str):
        self.s, self.i = text, 0

    def peek(self) -> str:
        return self.s[self.i] if self.i < len(self.s) else ""

    def ows(self) -> None:
        while self.peek() in (" ", "\t") and self.peek():
            self.i += 1

    def sp(self) -> None:
        while self.peek() == " ":
            self.i += 1

    def key(self) -> str:
        m = _KEY.match(self.s, self.i)
        if not m:
            raise SFError(f"expected a key at offset {self.i}")
        self.i = m.end()
        return m.group()

    def bare(self) -> Any:
        c = self.peek()
        if c == '"':
            return self._string()
        if c == ":":
            end = self.s.find(":", self.i + 1)
            if end < 0:
                raise SFError("unterminated byte sequence")
            raw = self.s[self.i + 1:end]
            self.i = end + 1
            try:
                return base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError):
                raise SFError("a byte sequence is not valid base64") from None
        if c == "?":
            if self.s[self.i + 1:self.i + 2] not in ("0", "1"):
                raise SFError(f"bad boolean at offset {self.i}")
            self.i += 2
            return self.s[self.i - 1] == "1"
        m = _NUMBER.match(self.s, self.i)
        if m and (c == "-" or c.isdigit()):
            self.i = m.end()
            return float(m.group()) if m.group(1) else int(m.group())
        m = _TOKEN.match(self.s, self.i)
        if m:
            self.i = m.end()
            return Token(m.group())
        raise SFError(f"bad item at offset {self.i}")

    def _string(self) -> str:
        self.i += 1
        out = []
        while True:
            c = self.peek()
            if not c:
                raise SFError("unterminated string")
            self.i += 1
            if c == "\\":
                nxt = self.peek()
                if nxt not in ('"', "\\"):
                    raise SFError("bad escape in a string")
                out.append(nxt)
                self.i += 1
            elif c == '"':
                return "".join(out)
            elif not (0x20 <= ord(c) <= 0x7E):
                raise SFError("a string holds a character outside printable ASCII")
            else:
                out.append(c)

    def params(self) -> Params:
        out: Params = {}
        while self.peek() == ";":
            self.i += 1
            self.sp()
            k = self.key()
            v: Any = True
            if self.peek() == "=":
                self.i += 1
                v = self.bare()
            out[k] = v
        return out

    def item_or_inner(self) -> Member:
        if self.peek() == "(":
            self.i += 1
            items: list[Member] = []
            while True:
                self.sp()
                if self.peek() == ")":
                    self.i += 1
                    break
                v = self.bare()
                items.append((v, self.params()))
                if self.peek() not in (" ", ")"):
                    raise SFError("bad inner list")
            return items, self.params()
        v = self.bare()
        return v, self.params()

    def dictionary(self) -> dict[str, Member]:
        out: dict[str, Member] = {}
        while True:
            self.sp()
            k = self.key()
            if self.peek() == "=":
                self.i += 1
                out[k] = self.item_or_inner()
            else:
                out[k] = (True, self.params())
            self.ows()
            if not self.peek():
                return out
            if self.peek() != ",":
                raise SFError(f"expected a comma at offset {self.i}")
            self.i += 1
            self.ows()
            if not self.peek():
                raise SFError("a trailing comma")

    def done(self) -> None:
        self.sp()
        if self.peek():
            raise SFError(f"trailing data at offset {self.i}")


def parse_dictionary(text: str) -> dict[str, Member]:
    p = _Parser(text.strip())
    if not p.s:
        raise SFError("an empty dictionary")
    d = p.dictionary()
    p.done()
    return d


def parse_item(text: str) -> Member:
    p = _Parser(text.strip())
    v = p.item_or_inner()
    p.done()
    return v


def ser_bare(v: Any) -> str:
    if isinstance(v, bool):
        return "?1" if v else "?0"
    if isinstance(v, int):
        if abs(v) > 999_999_999_999_999:
            raise SFError("an integer is out of range")
        return str(v)
    if isinstance(v, Token):
        if not _TOKEN.fullmatch(v):
            raise SFError(f"not a token: {v!r}")
        return str(v)
    if isinstance(v, bytes):
        return ":" + base64.b64encode(v).decode() + ":"
    if isinstance(v, str):
        if any(not (0x20 <= ord(c) <= 0x7E) for c in v):
            raise SFError("a string holds a character outside printable ASCII")
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    raise SFError(f"cannot serialize {type(v).__name__}")


def ser_params(p: Params) -> str:
    out = []
    for k, v in p.items():
        if not _KEY.fullmatch(k):
            raise SFError(f"not a key: {k!r}")
        out.append(f";{k}" if v is True else f";{k}={ser_bare(v)}")
    return "".join(out)


def ser_inner(items: list[Member], params: Params) -> str:
    return "(" + " ".join(ser_bare(v) + ser_params(p) for v, p in items) + ")" + ser_params(params)
