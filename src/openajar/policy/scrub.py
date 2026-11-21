"""Known vault values out of text: the matcher behind VaultService.scrub, and the lazy hook the
node's sinks call (tool results, traces, run proof, logs).

What is caught, for each value (a password, an API key, a cookie, a machine secret, a TOTP seed,
a recent one-time code, and a username of at least MIN_USERNAME characters):

- the value as typed, in any letter case (a page that upper-cases it, an email typed in capitals);
- the value percent-encoded the three ways a page or a browser writes it into a URL: Python's
  quote, quote_plus (a form's query string) and JavaScript's encodeURIComponent, with either
  case of hex digits.

What is not caught, on purpose or because it cannot be done cheaply:

- a value split by something in between: across two outline lines, two elements, an ellipsis
  or a line wrap in a log ("hunter" and "2pass" on two lines are two harmless words);
- a value transformed further: base64, HTML entities, a hash, part of the value (a page that
  shows the last four characters), or a value shorter than MIN_SECRET characters;
- text whose lower case changes its length (a few Unicode letters such as the Turkish dotted I):
  the cheap check below misses a value inside it.

What is left alone so an agent can still work:

- a snapshot's ref tokens (`[ref=e12]`, `[ref=f1e12]`, `aria-ref=e12`): an agent acts by them, and a
  ref is numbered by the outline, never by a page;
- a short value (under BOUNDED_BELOW characters) or an all-digit value inside a longer word or
  number: it is caught only between boundaries (not next to a letter or a digit), so a password
  "test" leaves "latest" alone and a code 1234 leaves "e1234" and "912345" alone. The same value
  standing on its own ("1234 Main St") is still hidden: a scrub cannot tell a code from a number.
  A longer value is caught anywhere, inside other text too.

Cost: the values are decrypted once and kept, until the vault file changes, the master key
changes or the vault seals (VaultService keys the cache). A scrub is then one `in` per form
over the lower-cased text, and a small regex only when one of them is there.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import quote, quote_plus

from openajar.core import logs

log = logging.getLogger(__name__)

REDACTED = "[redacted]"
MIN_SECRET = 4     # shorter values are left alone: they would hide common words
MIN_USERNAME = 6   # a username is often a word or a name: only longer ones (emails, phones, handles)
BOUNDED_BELOW = 8  # shorter values, and all-digit ones, are caught only between word boundaries
_JS_SAFE = "!'()*-._~"  # what encodeURIComponent leaves alone
# a snapshot's ref tokens (openajar.node.snapshot.REF_RE), kept whole: matched first, put back as they are
_REF_TOKEN = r"\[ref=(?:f\d+)?e\d+\]|aria-ref=(?:f\d+)?e\d+(?![0-9A-Za-z])"
_NOT_ALNUM_BEFORE = r"(?<![^\W_])"  # the previous character, if any, is not a letter or a digit
_NOT_ALNUM_AFTER = r"(?![^\W_])"


def forms(value: str) -> set[str]:
    """The value and the ways it appears inside a URL."""
    out = {value, quote(value, safe=""), quote_plus(value), quote(value, safe=_JS_SAFE)}
    return {f for f in out if f}


class Matcher:
    """Replaces known values in text with [redacted], whatever their letter case."""

    def __init__(self, values: Iterable[str]):
        found: set[str] = set()
        for v in values:
            found |= {f.lower() for f in forms(v)}
        self._forms = sorted(found, key=len, reverse=True)  # the longer one first: a value inside another

    def __bool__(self) -> bool:
        return bool(self._forms)

    def __len__(self) -> int:
        return len(self._forms)

    def sub(self, text: str) -> str:
        if not text or not self._forms:
            return text
        low = text.lower()
        hits = [f for f in self._forms if f in low]
        if not hits:
            return text
        alts = [_REF_TOKEN] + [_bounded(f) for f in hits]
        return re.compile("|".join(f"({a})" for a in alts), re.IGNORECASE).sub(_keep_refs, text)


def _bounded(form: str) -> str:
    """The regex for one form: anywhere when it is long and not all digits, else between boundaries."""
    if len(form) >= BOUNDED_BELOW and not form.isdigit():
        return re.escape(form)
    return _NOT_ALNUM_BEFORE + re.escape(form) + _NOT_ALNUM_AFTER


def _keep_refs(m: re.Match[str]) -> str:
    return m.group(0) if m.group(1) is not None else REDACTED


def walk(value: Any, fn: Callable[[str], str]) -> Any:
    """`fn` on every string in a JSON-like value (dict keys stay as they are)."""
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, dict):
        return {k: walk(v, fn) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [walk(v, fn) for v in value]
    return value


def runtime_scrub(runtime: Any, *, lane_codes: bool = False) -> Callable[[str], str]:
    """The vault's scrub over every lane's values, looked up on each call, so a sink built before
    the vault was registered still uses it. Page text is scrubbed of other lanes' values too: a
    secret's lane binding says where it may be typed, not who may read it, and the model never
    sees a secret (docs/PRINCIPLES.md 3); a reused password shows up on a second lane's pages.

    `lane_codes` (page text a tool returns): the recent one-time codes are those of the lane the
    call runs on (the log context's lane), so a 6-digit code typed on one lane does not hide every
    6-digit number on the others for CODE_MEMORY_SECONDS. A code of one lane read on another (a
    webmail lane that shows the code email) then reaches the agent, after the code was used. Every
    other sink (traces, logs, run proof) scrubs every lane's codes.

    Fails closed: when the vault's scrub raises, the text is withheld, as the log filter does."""
    def scrub(text: str) -> str:
        if not text or runtime is None:
            return text
        vault = runtime.get("vault")
        fn = getattr(vault, "scrub", None)
        if not callable(fn):
            return text
        try:
            lane = logs.current().get("lane") if lane_codes else None
            return fn(text, codes_lane=lane) if lane else fn(text)
        except Exception:  # noqa: BLE001 - never let unscrubbed text through
            log.error("vault scrub failed; text withheld")
            return "[withheld: the vault scrub failed]"
    return scrub
