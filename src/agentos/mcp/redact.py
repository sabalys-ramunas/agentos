"""Last line of defense: mask known secrets in anything a tool returns or an error says."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import Any

from agentos.core.config import NodeConfig, load_proxies

log = logging.getLogger(__name__)

# user:pass@ in a URL, also with an empty user (`http://:pw@host`), and a lone user part that is a
# token (`https://tok@host`). The password part runs to the last "@" before the path, so a password
# that itself contains "@" is masked whole. A query or fragment ends the search (an @ there is no login).
URL_CREDENTIALS_RE = re.compile(r"(\w+://)(?:[^\s/?#@:]*:[^\s/?#]*|[^\s/?#@:\[]+)@")


class Redactor:
    """Masks proxy credentials (and user:pass@ in any URL) in tool output.

    Credentials are never put into tool output on purpose; this catches the accidental
    case (an engine error that quotes a proxy URL, a page that echoes it back). Without a
    config only the URL credentials are masked. `scrub` runs first on every string: on the
    node, the vault's values (agentos.vault.scrub.runtime_scrub), so page text a tool returns
    (an outline, a delta, a read value, an error quoting the page) never carries one.
    """

    def __init__(self, config: NodeConfig | None, *, cache_seconds: float = 0,
                 scrub: Callable[[str], str] | None = None):
        self._config = config
        self._scrub = scrub
        self._cache_seconds = cache_seconds  # the log scrubber reads the proxies file at most this often
        self._cached: tuple[float, list[str]] | None = None
        self._last_read: list[str] = []  # the last list read whole: what an unreadable file falls back to

    def _secrets(self) -> list[str]:
        if self._config is None:
            return []
        if self._cache_seconds and self._cached and time.monotonic() - self._cached[0] < self._cache_seconds:
            return self._cached[1]
        found = self._read_secrets()
        if self._cache_seconds:
            self._cached = (time.monotonic(), found)
        return found

    def _read_secrets(self) -> list[str]:
        assert self._config is not None
        try:
            proxies = load_proxies(self._config)
        except Exception:  # an unreadable proxies file must not break every tool call
            # nor unmask what it held: a file caught mid-edit, or damaged, still masks the logins
            # last read from it, which the running lanes may still use (fail closed)
            log.warning("could not read proxies for redaction; masking the logins last read", exc_info=True)
            return self._last_read
        found: set[str] = set()
        for p in proxies.values():
            if p.password and len(p.password) >= 3:
                found.add(p.password)
            if p.username and len(p.username) >= 6:
                found.add(p.username)
        self._last_read = sorted(found, key=len, reverse=True)
        return self._last_read

    def text(self, value: str, secrets: list[str] | None = None) -> str:
        secrets = self._secrets() if secrets is None else secrets
        if self._scrub is not None:
            value = self._scrub(value)
        for secret in secrets:  # known secrets first, so the URL pattern sees only masks
            value = value.replace(secret, "***")
        return URL_CREDENTIALS_RE.sub(r"\1***@", value)

    def value(self, value: Any) -> Any:
        return self._walk(value, self._secrets())

    def _walk(self, value: Any, secrets: list[str]) -> Any:
        if isinstance(value, str):
            return self.text(value, secrets)
        if isinstance(value, dict):
            return {k: self._walk(v, secrets) for k, v in value.items()}
        if isinstance(value, list):
            return [self._walk(v, secrets) for v in value]
        return value
