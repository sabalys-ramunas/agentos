"""Redaction at write time, for traces, approval previews, notifications and errors.

Two layers:
- structured: a value under a key that names a secret (password, token, api_key, cookie,
  authorization, otp, ...) is replaced whatever it contains;
- textual: known secret shapes inside strings are replaced (Authorization and Cookie
  values, bearer tokens, agentos grant tokens, JWTs, provider keys, URL passwords,
  otpauth URIs, PEM private keys, credit-card-like numbers that pass Luhn, and long
  high-entropy tokens), one-time codes after their label (`2FA code: 482913`), the word after
  "password" when it reads as the password (`wrong password hunter2`), and session cookies by name
  (`connect.sid=`, `xs=`, `PHPSESSID=`).

`mask_addresses` is separate: a node masks IP addresses in what it reports, a local trace keeps them.

It errs toward over-redaction: a trace that hides a harmless id is fine, a trace that
leaks a secret is not.
"""

from __future__ import annotations

import ipaddress
import math
import re
from collections import Counter
from collections.abc import Iterable
from itertools import pairwise
from typing import Any

from pydantic import BaseModel

REDACTED = "[redacted]"
MAX_TEXT = 4000

_SECRET_WORDS = {
    "password", "passwd", "pwd", "passphrase", "secret", "secrets", "token", "apikey",
    "authorization", "auth", "cookie", "cookies", "otp", "totp", "hotp", "mfa", "2fa", "pin",
    "cvv", "cvc", "credential", "credentials", "privatekey", "sessionid", "session", "bearer",
    "signature", "sig", "seed", "mnemonic", "csrf", "xsrf",
}
_SECRET_PAIRS = {
    ("api", "key"), ("private", "key"), ("client", "secret"), ("access", "token"),
    ("refresh", "token"), ("id", "token"), ("card", "number"), ("set", "cookie"),
    ("verification", "code"), ("security", "code"), ("recovery", "code"), ("backup", "code"),
    ("one", "time"), ("secret", "key"), ("access", "key"),
}
# Under these words a number is a count or a flag, not a secret (max_tokens is not a token).
_NUMERIC_SAFE = {"token", "session", "auth", "signature", "sig"}

_WORD_SPLIT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")


def _words(key: str) -> list[str]:
    return [w.lower() for w in _WORD_SPLIT.split(key) if w]


def secret_key_kind(key: str) -> str | None:
    """The matched secret word for a field name, or None."""
    words = _words(key)
    if not words:
        return None
    joined = "".join(words)
    if joined in _SECRET_WORDS:
        return joined
    for w in words:
        if w in _SECRET_WORDS:
            return w
    for a, b in pairwise(words):
        if (a, b) in _SECRET_PAIRS:
            return f"{a}_{b}"
    return None


def is_secret_key(key: str) -> bool:
    return secret_key_kind(key) is not None


_TEXT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.DOTALL),
     "[redacted:private_key]"),
    (re.compile(r"\botpauth(?:-migration)?://[^\s\"'<>]+", re.IGNORECASE), "[redacted:otpauth]"),
    (re.compile(r"\bagt[12]_[A-Za-z0-9_-]+"), "[redacted:grant]"),
    (re.compile(r"\bagtg_[A-Za-z0-9_-]{20,}"), "[redacted:git]"),  # a git gateway token (agentos.git)
    (re.compile(r"\baos_[A-Za-z0-9_-]{43}"), "[redacted:site-key]"),
    (re.compile(r"(?i)\b((?:proxy-)?authorization|set-cookie|cookie)(\"?'?\s*[:=]\s*)"
                r"(\"[^\"]*\"|'[^']*'|[^\r\n]+)"), r"\1\2[redacted]"),
    (re.compile(r"(?i)\b(bearer|basic)(\s+)[A-Za-z0-9._~+/=-]{6,}"), r"\1\2[redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}"), "[redacted:jwt]"),
    (re.compile(r"\b(?:sk|rk)[-_](?:live|test|proj|ant|svcacct)?[-_]?[A-Za-z0-9_-]{16,}"), "[redacted:key]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"
                r"|\bglpat-[A-Za-z0-9_-]{20,}|\bxox[abposr]-[A-Za-z0-9-]{10,}|\bAKIA[0-9A-Z]{16}\b"
                r"|\bAIza[0-9A-Za-z_-]{35}"), "[redacted:key]"),
    # user:pass@ in a URL (the user may be empty; the password runs to the last @ before the path, so one
    # that holds an @ is masked whole), then a URL whose user part alone is the token
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@?#]*:)([^/\s?#]+)(@)"), r"\1[redacted]\3"),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@?#\[]+)(@)"), r"\1[redacted]\3"),
]

# A one-time code after its label: `2FA code: 482913`, `otp 482913`, `one-time password 482913`. The label
# names the code, so "login 1200 ms" or "status code 503" is left alone; a bare `code=` needs 6 to 8 digits.
_OTP_RE = re.compile(
    r"(?i)\b((?:2fa|mfa|otp|totp|hotp)(?:[ _-]?(?:code|passcode|password|pin|token))?"
    r"|(?:one[ -]?time|verification|security|login|sign[ -]?in|auth(?:entication)?|confirmation|sms|access)"
    r"[ _-]?(?:code|passcode|password|pin))"
    r"(\s*(?:(?:is|was)\s+)?[:=#]?\s*)(\d{4,10})(?![\d.])")
_BARE_CODE_RE = re.compile(r"(?i)(?<![\w.-])(code\s*[:=]\s*)(\d{6,8})(?![\d.])")
# The word after "password" when the sentence says it is one ("wrong password hunter2", "password is x"),
# or when it has a digit or a symbol in it. Prose after the word ("password field", "wrong password for
# x") is left alone.
_PASSWORD_WORD_RE = re.compile(
    r"(?i)\b(?:(wrong|incorrect|invalid|bad|rejected|old|new|temporary|temp)\s+)?(pass(?:word|wd|phrase|code))"
    r"(\s+(?:is|was)\s+|\s+)([^\s,;]+)")
_PROSE_WORDS = """
    for on at in into from to of and or not no is was are be been the a an with by via as than then
    field fields form input prompt page box entry manager reset change changed required expired entered
    typed filled login attempt attempts failed fails error policy mismatch again too must should does did
    has had have will may can cannot needed missing empty saved set updated hint strength rules length
    incorrect wrong invalid correct accepted rejected ok valid weak strong shown hidden visible stored
    autofill autofilled used unchanged sent mail email link
"""
_PROSE = frozenset(_PROSE_WORDS.split())


def _redact_password_words(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        qualifier, joint, token = m.group(1), m.group(3), m.group(4)
        word = token.rstrip(".!?)'\"]")
        if not word or word.startswith(("[redacted", "(", "[", "<", "{")) or word.lower() in _PROSE:
            return m.group(0)
        said = bool(qualifier) or joint.strip() != ""
        if not said and word.isalpha():
            return m.group(0)
        return m.group(0)[: m.start(4) - m.start()] + REDACTED + token[len(word):]

    return _PASSWORD_WORD_RE.sub(repl, text)


# Session cookies whose names hold no secret word: Express, PHP, Java, Meta, Google.
_SESSION_COOKIES = frozenset({"sid", "xs", "phpsessid", "jsessionid", "asp.net_sessionid", "sapisid", "apisid",
                              "hsid", "ssid", "sessid"})


def _session_cookie(key: str) -> bool:
    lower = key.lower()
    return lower in _SESSION_COOKIES or lower.endswith((".sid", "_sid", "-sid", "sessid", "sessionid"))


# `key: value` and `key=value` in text: a header line, an env assignment, a query parameter, a dict's
# repr. The key is a secret when its words hold a secret word (`x-api-key`, `DB_PASSWORD`, `sessionid`),
# the same rule as for a structured key, so any prefix or suffix counts.
_KEY_RE = re.compile(r"(?<![A-Za-z0-9_.-])([A-Za-z][A-Za-z0-9_.-]{0,80})(\"?'?\s*[=:]\s*)(\"|')?")
_VALUE_END = {None: re.compile(r"[\s\"',;&})\]<>]"), '"': re.compile(r'"'), "'": re.compile(r"'")}

def _redact_key_values(text: str) -> str:
    """Mask the value after every secret key in `text` (see _KEY_RE). A number under a counting word
    (`max_tokens=500`, `session: 3`) is left alone, as in redact()."""
    out: list[str] = []
    pos = 0
    for m in _KEY_RE.finditer(text):
        if m.start() < pos:
            continue
        kind = secret_key_kind(m.group(1)) or ("session" if _session_cookie(m.group(1)) else None)
        if kind is None:
            continue
        start = m.end()
        end_match = _VALUE_END[m.group(3)].search(text, start)
        end = end_match.start() if end_match else len(text)
        value = text[start:end]
        if not value or value == REDACTED or value.startswith("[redacted"):
            continue
        if kind in _NUMERIC_SAFE and value.isdecimal():
            continue
        out.append(text[pos:start])
        out.append(REDACTED)
        pos = end
    if not out:
        return text
    out.append(text[pos:])
    return "".join(out)


_CARD_RE = re.compile(r"(?<![\d.])\d(?:[ -]?\d){12,18}(?![\d.])")
_CANDIDATE_RE = re.compile(r"[A-Za-z0-9+_=~-]{24,}")


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _entropy(s: str) -> float:
    counts = Counter(s)
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def _looks_random(s: str) -> bool:
    core = s.strip("=-_~+")
    if len(core) < 24:
        return False
    has_upper = any(c.isupper() for c in core)
    has_lower = any(c.islower() for c in core)
    digits = sum(c.isdigit() for c in core)
    if re.fullmatch(r"[0-9a-fA-F]+", core):
        return len(core) >= 32 and digits >= 4 and digits < len(core) and _entropy(core) >= 3.0
    if has_upper and has_lower and digits >= 2:
        return _entropy(core) >= 3.5
    if has_lower and not has_upper and digits >= 4 and len(core) >= 32:
        return _entropy(core) >= 3.8
    return False


def _redact_cards(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return "[redacted:card]"
        return m.group(0)

    return _CARD_RE.sub(repl, text)


def _redact_entropy(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        start = m.start()
        prefix = text[max(0, start - 7):start].lower()
        if prefix.endswith(("sha256:", "sha1:", "sha512:")):
            return m.group(0)
        return "[redacted:token]" if _looks_random(m.group(0)) else m.group(0)

    return _CANDIDATE_RE.sub(repl, text)


def redact_text(text: str, *, extra: Iterable[str] = (), max_len: int = MAX_TEXT) -> str:
    """Redact secret shapes inside a string; `extra` are exact values to scrub (e.g. the bearer)."""
    for value in extra:
        if value and len(value) >= 6 and value in text:
            text = text.replace(value, REDACTED)
    for pattern, repl in _TEXT_PATTERNS:
        text = pattern.sub(repl, text)
    text = _OTP_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _BARE_CODE_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    text = _redact_password_words(text)
    text = _redact_key_values(text)
    text = _redact_cards(text)
    text = _redact_entropy(text)
    if max_len and len(text) > max_len:
        text = f"{text[:max_len]}...[truncated {len(text) - max_len} chars]"
    return text


# ---- addresses ----

ADDRESS = "[address]"
_IPV4_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\w|\.\d)")
# Any run of hex groups and colons: checked with ipaddress, so a time (10:12:34), a MAC or `std::vector`
# is not taken for an address, and a compressed one (2001:db8::1, ::1) is.
_IPV6_RE = re.compile(r"(?<![\w:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?:%[\w.-]+)?(?![\w:])")


def _ipv4(m: re.Match[str]) -> str:
    return ADDRESS if all(int(part) <= 255 for part in m.group(0).split(".")) else m.group(0)


def _ipv6(m: re.Match[str]) -> str:
    candidate = m.group(0).split("%", 1)[0]
    if not any(c not in ":" for c in candidate):
        return m.group(0)  # a bare "::"
    try:
        ipaddress.IPv6Address(candidate)
    except ValueError:
        return m.group(0)
    return ADDRESS


def mask_addresses(text: str) -> str:
    """IPv4 and IPv6 addresses out of free text: a controller never holds a lane's exit IP. Version
    strings (1.2.3) and times (10:12:34) are not addresses. Mask before any cut, so a cut cannot keep
    half an address."""
    return _IPV6_RE.sub(_ipv6, _IPV4_RE.sub(_ipv4, text))


def redact(value: Any, *, extra: Iterable[str] = (), max_len: int = MAX_TEXT, _depth: int = 0) -> Any:
    """Return a JSON-safe, redacted copy of value."""
    extra = tuple(extra)
    if _depth > 12:
        return "[nested too deep]"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return redact_text(value, extra=extra, max_len=max_len)
    if isinstance(value, bytes | bytearray | memoryview):
        return f"<{len(bytes(value))} bytes>"
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            kind = secret_key_kind(key)
            if kind and v is not None and not (isinstance(v, bool)):
                if isinstance(v, int | float) and kind in _NUMERIC_SAFE:
                    out[key] = v
                else:
                    out[key] = REDACTED
            else:
                out[key] = redact(v, extra=extra, max_len=max_len, _depth=_depth + 1)
        return out
    if isinstance(value, list | tuple | set | frozenset):
        return [redact(v, extra=extra, max_len=max_len, _depth=_depth + 1) for v in value]
    if hasattr(value, "data") and hasattr(value, "mime_type"):  # ToolImage
        return f"<image {getattr(value, 'mime_type', '')}>"
    return redact_text(repr(value), extra=extra, max_len=max_len)
