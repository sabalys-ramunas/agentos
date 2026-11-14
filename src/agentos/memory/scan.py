"""What must never enter memory (4.14) and the suspicious heuristic (4.4).

Secrets: every text a writer sends (title, body, every string in the attributes, a forget
reason) goes through each scrubber. If any of them changes the text, the write is refused with
the kinds found, so memory never holds half-redacted text. Attribute keys named like secrets
are refused too.

Suspicious: a cheap, deterministic signal for untrusted entries that look like instructions
to agents. It is a hint for the owner and the context pack, never a security boundary.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Sequence
from itertools import pairwise
from typing import Any
from urllib.parse import urlsplit

from agentos.memory.errors import SecretInMemory

log = logging.getLogger("agentos.memory")

Scrubber = Callable[[str], str]

_MARKER_RE = re.compile(r"\[redacted(?::([a-z_]+))?\]")
_SECRET_KEY_WORDS = {"password", "passwd", "pwd", "passphrase", "otp", "totp", "cookie", "cookies", "token",
                     "secret", "secrets", "apikey", "credential", "credentials", "sessionid", "privatekey",
                     "mnemonic", "seed", "cvv", "cvc"}
_SECRET_KEY_PAIRS = {("api", "key"), ("private", "key"), ("client", "secret"), ("access", "token"),
                     ("refresh", "token"), ("set", "cookie"), ("session", "token"), ("auth", "token"),
                     ("verification", "code"), ("recovery", "code"), ("backup", "code"), ("one", "time")}
_WORD_SPLIT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")


def default_scrubbers() -> list[tuple[str, Scrubber]]:
    """The credential pattern scrubbers the design names: the policy redactor and the brains
    transcript patterns. Imported lazily, and skipped (with a log line) when the package is not
    part of this build. The vault's own `scrub` is a separate port (it knows the values)."""
    out: list[tuple[str, Scrubber]] = []
    try:
        from agentos.policy.redact import redact_text

        out.append(("credential_pattern", lambda text: redact_text(text, max_len=0)))
    except ImportError:  # pragma: no cover - policy is always built with the node
        log.warning("agentos.policy.redact is not available; memory checks fewer secret shapes")
    try:
        from agentos.brains.transcript import redact_patterns

        out.append(("credential_pattern", redact_patterns))
    except ImportError:  # pragma: no cover - brains is optional
        pass
    return out


def secret_key_kind(key: str) -> str | None:
    words = [w.lower() for w in _WORD_SPLIT.split(key) if w]
    if not words:
        return None
    joined = "".join(words)
    if joined in _SECRET_KEY_WORDS:
        return joined
    for w in words:
        if w in _SECRET_KEY_WORDS:
            return w
    for a, b in pairwise(words):
        if (a, b) in _SECRET_KEY_PAIRS:
            return f"{a}_{b}"
    return None


def strings_of(value: Any, _depth: int = 0) -> Iterable[str]:
    """Every string inside a JSON-like value, keys included."""
    if _depth > 10:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from strings_of(v, _depth + 1)
    elif isinstance(value, list | tuple):
        for v in value:
            yield from strings_of(v, _depth + 1)


def secret_keys(value: Any, _depth: int = 0) -> list[str]:
    """Attribute keys (at any depth) that name a secret."""
    found: list[str] = []
    if _depth > 10:
        return found
    if isinstance(value, dict):
        for k, v in value.items():
            kind = secret_key_kind(str(k))
            if kind:
                found.append(kind)
            found.extend(secret_keys(v, _depth + 1))
    elif isinstance(value, list | tuple):
        for v in value:
            found.extend(secret_keys(v, _depth + 1))
    return found


class SecretScanner:
    """Refuses text that any scrubber would change. `vault_scrub` replaces known vault values
    (all entries) and recent one-time codes; `patterns` replace credential shapes."""

    def __init__(self, vault_scrub: Scrubber | None, patterns: Sequence[tuple[str, Scrubber]]):
        self.vault_scrub = vault_scrub
        self.patterns = list(patterns)

    def kinds(self, text: str) -> list[str]:
        if not text:
            return []
        found: list[str] = []
        if self.vault_scrub is not None:
            try:
                if self.vault_scrub(text) != text:
                    found.append("vault_value")
            except Exception:
                log.exception("vault scrub failed; refusing the write to be safe")
                found.append("vault_unavailable")
        for kind, fn in self.patterns:
            out = fn(text)
            if out != text:
                named = {m.group(1) for m in _MARKER_RE.finditer(out) if m.group(1)} - set(_MARKER_RE.findall(text))
                found.extend(sorted(named) or [kind])
        return found

    def check(self, *texts: str, attrs: Any = None) -> None:
        """Raise SecretInMemory when anything looks like a secret."""
        kinds: list[str] = []
        for text in texts:
            kinds.extend(self.kinds(text))
        if attrs:
            kinds.extend(f"attribute named {k}" for k in secret_keys(attrs))
            for text in strings_of(attrs):
                kinds.extend(self.kinds(text))
        if kinds:
            raise SecretInMemory(kinds)

    def scrub_out(self, text: str | None) -> tuple[str | None, bool]:
        """On read: the vault's scrub again (it may have learned a value since the write).
        Returns the text to serve and whether anything was redacted."""
        if not text or self.vault_scrub is None:
            return text, False
        try:
            out = self.vault_scrub(text)
        except Exception:
            log.exception("vault scrub failed on read; withholding the text")
            return "[withheld: the vault could not check this text]", True
        return out, out != text


# ---- suspicious (4.4) ------------------------------------------------------------------

_ADDRESSES_AGENTS = re.compile(
    r"\b(agents?|assistants?|ai|llms?|models?|bots?|chatgpt|gpt-?\d*|claude|codex|copilot|gemini)\b[,:]?",
    re.IGNORECASE)
_OVERRIDE = re.compile(
    r"\b(ignore (all |any |the )?(previous|prior|above|earlier)|disregard|forget (all|everything|your)|"
    r"from now on|always|never tell|system prompt|system message|system:|new instructions?|"
    r"you must|you are now|do not tell|override)\b", re.IGNORECASE)
_URL_RE = re.compile(r"\b(?:https?://|www\.)[^\s<>\"')\]]+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
_B64_RE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
_HEX_RE = re.compile(r"\b[0-9a-fA-F]{48,}\b")
_PCT_RE = re.compile(r"(?:%[0-9a-fA-F]{2}){6,}")
_DATA_URI_RE = re.compile(r"\bdata:[a-z]+/[a-z0-9.+-]+;base64,", re.IGNORECASE)


def _host(url: str) -> str:
    if not url.lower().startswith(("http://", "https://")):
        url = "http://" + url
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def suspicious_reasons(text: str, *, allowed: Callable[[str], bool] | None = None) -> list[str]:
    """Why a text looks like an instruction aimed at agents (empty when it does not).
    `allowed(host)` says whether a host is on the workspace's egress allowlist."""
    reasons: list[str] = []
    if _ADDRESSES_AGENTS.search(text):
        reasons.append("addresses agents")
    if _OVERRIDE.search(text):
        reasons.append("override phrase")
    hosts = {_host(u) for u in _URL_RE.findall(text)} | {m.lower() for m in _EMAIL_RE.findall(text)}
    off_list = sorted(h for h in hosts if h and not (allowed and allowed(h)))
    if off_list:
        reasons.append("link or address off the allowlist")
    if _DATA_URI_RE.search(text) or _PCT_RE.search(text) or _HEX_RE.search(text) or any(
            _looks_encoded(m) for m in _B64_RE.findall(text)):
        reasons.append("encoded blob")
    return reasons


def _looks_encoded(s: str) -> bool:
    return any(c.isdigit() for c in s) and any(c.isupper() for c in s) and any(c.islower() for c in s)
