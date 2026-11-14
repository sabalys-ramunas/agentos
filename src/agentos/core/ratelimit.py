"""A sliding-window rate limiter: per key, plus a global cap (panel sign-in, site API keys, webhook
relays over the controller channel)."""

from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    """Sliding window per key plus a global cap, for login attempts."""

    def __init__(self, *, per_key: int = 10, global_max: int = 60, window: float = 300.0):
        self.per_key, self.global_max, self.window = per_key, global_max, window
        self._hits: dict[str, deque[float]] = {}
        self._all: deque[float] = deque()
        self._lock = threading.Lock()

    def _trim(self, q: deque[float], now: float) -> None:
        while q and q[0] <= now - self.window:
            q.popleft()

    def check(self, key: str) -> float | None:
        """Record an attempt. Returns None when allowed, else seconds until the next one is."""
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            self._trim(q, now)
            self._trim(self._all, now)
            if len(q) >= self.per_key:
                return max(1.0, q[0] + self.window - now)
            if len(self._all) >= self.global_max:
                return max(1.0, self._all[0] + self.window - now)
            q.append(now)
            self._all.append(now)
            if len(self._hits) > 10_000:  # bound memory under a spray of addresses
                for k in [k for k, v in self._hits.items() if not v]:
                    del self._hits[k]
            return None
