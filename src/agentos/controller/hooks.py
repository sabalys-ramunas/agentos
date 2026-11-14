"""How much a public route takes: the gate in front of `/hooks` and `/u`.

These routes need no sign-in, so anyone can reach them. A request is counted against its source
first, before anything is read or looked up, so a scan of made-up tokens is limited too. Then against
its token and its node, so a sender on many addresses is limited as well. In-flight relays are capped
per node, so a flood is refused at once rather than queued. The limiter's keys are bounded (a
refused source stores nothing, and the node and token keys are only ever live nodes and
well-formed tokens), so a flood cannot grow memory either.

The source is the peer address: behind a reverse proxy every sender is that proxy, and the token and
node limits are what hold. X-Forwarded-For is not read, because a sender can write it.
"""

from __future__ import annotations

import re

from agentos.api.auth import RateLimiter

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
BODY_TIMEOUT_S = 10.0  # a sender that dribbles its body holds a slot for no longer than this
WINDOW_S = 60.0


class HookGate:
    def __init__(self, *, per_source: int = 60, per_token: int = 300, per_node: int = 1200, in_flight: int = 16,
                 everyone: int = 20_000, window: float = WINDOW_S):
        self.sources = RateLimiter(per_key=per_source, global_max=everyone, window=window)
        self.tokens = RateLimiter(per_key=per_token, global_max=everyone, window=window)
        self.nodes = RateLimiter(per_key=per_node, global_max=everyone, window=window)
        self.in_flight = in_flight
        self._busy: dict[str, int] = {}

    def source(self, source: str) -> float | None:
        """Seconds to wait, or None when this source may go on."""
        return self.sources.check(source)

    def target(self, node_id: str, token: str) -> float | None:
        return self.tokens.check(f"{node_id}:{token}") or self.nodes.check(node_id)

    def enter(self, node_id: str) -> bool:
        """A place among the relays to this node, or False at once: nothing waits in line here."""
        if self._busy.get(node_id, 0) >= self.in_flight:
            return False
        self._busy[node_id] = self._busy.get(node_id, 0) + 1
        return True

    def leave(self, node_id: str) -> None:
        left = self._busy.get(node_id, 1) - 1
        if left > 0:
            self._busy[node_id] = left
        else:
            self._busy.pop(node_id, None)
