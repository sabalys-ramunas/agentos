"""URL patterns and JSON paths for read_json, and the key names that look like secrets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from itertools import pairwise

MAX_URL_PATTERN = 200  # as a code catcher's pattern (agentos.vault.catcher)
MAX_WATCHED_URL = 2048  # a longer URL is never tested: a page chooses how long its URLs are
JSON_ROOT = "."  # read_json `items`: the body itself is the list
_JSON_PART = r"[A-Za-z0-9_-]{1,60}"
JSON_PATH_RE = re.compile(rf"{_JSON_PART}(?:\.{_JSON_PART}){{0,7}}")
# what may repeat in a URL pattern. Each is bounded by the character after it (_url_tokens), so it ends
# in one place only and compiles possessive: no pattern can make the matcher go back over a URL
URL_RUNS = ("[^/]+", "[^&]+", "[0-9]+", r"\d+", "[a-z0-9_-]+", "[A-Za-z0-9_-]+")
_URL_PLAIN = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/-_~%=&:,;@!'")
_URL_ESCAPED = frozenset(".?+*()[]{}|$^")
_SECRET_WORDS = frozenset({"auth", "bearer", "cookie", "cookies", "credential", "credentials", "csrf", "jwt", "otp",
                           "passphrase", "passwd", "password", "passwords", "pwd", "secret", "secrets", "session",
                           "sessions", "sid", "token", "tokens", "xsrf"})
_SECRET_GLUED = ("accesskey", "apikey", "authorization", "cookie", "credential", "passwd", "password", "phpsessid",
                 "privatekey", "secret", "sessionid", "sessionkey", "token")
_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+")


def names_secret(key: str) -> bool:
    """Does this JSON key name a credential, in any of the ways sites write one (`accessToken`,
    `session_id`, `X-Auth`, `apikey`)? A safety net for a mistake, not a boundary: a key can be called
    anything, so what keeps a secret out is that only declared fields leave, and a person activates."""
    glued = re.sub(r"[-_]", "", key).lower()
    return any(w.lower() in _SECRET_WORDS for w in _WORD_RE.findall(key)) or any(s in glued for s in _SECRET_GLUED)


def _url_tokens(pattern: str) -> tuple[list[tuple[bool, str]], bool]:
    """([(is a run, text)], ends in `$`), or a ValueError that says what to write instead."""
    tokens: list[tuple[bool, str]] = []
    at = 1  # past the ^
    while at < len(pattern):
        run = next((r for r in URL_RUNS if pattern.startswith(r, at)), None)
        ch = pattern[at]
        if run is not None:
            tokens.append((True, run))
            at += len(run)
        elif ch == "$" and at == len(pattern) - 1:
            return tokens, True
        elif ch == "\\" and pattern[at + 1:at + 2] in _URL_ESCAPED:
            tokens.append((False, pattern[at + 1]))
            at += 2
        elif ch in _URL_PLAIN:
            tokens.append((False, ch))
            at += 1
        else:
            raise ValueError(f"a read_json url cannot hold {ch!r} there: it is literal URL text, with a dot or a ? "
                             f"written \\. and \\?, and only {', '.join(URL_RUNS)} may repeat. No groups, alternatives "
                             "or params")
    return tokens, False


@dataclass(frozen=True, slots=True)
class UrlPattern:
    """A read_json `url`, parsed. `origin` is its literal `https://host/`, which lint checks as it
    checks any URL. A match never takes longer than one pass over the URL."""
    origin: str
    regex: re.Pattern[str]

    def matches(self, url: str) -> bool:
        return len(url) <= MAX_WATCHED_URL and self.regex.match(url) is not None


@lru_cache(maxsize=256)
def url_pattern(pattern: str) -> UrlPattern:
    """A read_json `url`: `^https://`, a literal host, a path. It matches from the start of a URL and
    to the end only with `$`. Text an owner or an agent wrote is run against every response a page
    makes, on the node's event loop, so it is held to a form that cannot backtrack, not to `re`."""
    if len(pattern) > MAX_URL_PATTERN:
        raise ValueError(f"a read_json url is at most {MAX_URL_PATTERN} characters")
    if not pattern.startswith("^https://"):
        raise ValueError("a read_json url starts ^https:// and a literal host, so it can only match that site")
    tokens, ended = _url_tokens(pattern)
    text = "".join("\0" if run else t for run, t in tokens)
    authority, slash, _ = text.removeprefix("https://").partition("/")
    if not slash or not authority or "\0" in authority:
        raise ValueError("a read_json url names a literal host and then a path: ^https://host\\.example/api")
    for (run, t), (next_run, following) in pairwise(tokens):
        if run and (next_run or re.fullmatch(t, following)):
            raise ValueError(f"{t} must be followed by a character it cannot match, so it has one place to end")
    source = "".join(t + "+" if run else re.escape(t) for run, t in tokens)
    return UrlPattern(origin=f"https://{authority}/", regex=re.compile(source + (r"\Z" if ended else "")))
