""""Ask me first" switches a controller sets (docs/PROTOCOL.md v1 clarifications, `ask_first`).

A person in the controller's panel turns a routine's or an account's "Ask me first" on or off. The
controller sends the change signed (`ask_first { ts, nonce, changes, sig }`); the node checks the
body as it checks `standing` (a fresh `ts`, a nonce never seen), applies each change to its own
switches (agentos.policy.ask_first) as a person's, and answers `ask_first_result { applied, refused }`.

Turning a switch on is never refused for a valid id. Turning one off lets commits run without a
person, so it needs a controller ceiling that reaches commit (`controller_ceiling` otherwise): a
controller that may not approve a commit may not let one run unasked either.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from agentos.core.errors import AgentosError, InvalidRequest, PolicyDenied
from agentos.core.models import Effect
from agentos.node.channel import wire
from agentos.node.channel.standing import NONCE_KEEP_S, NONCE_RE
from agentos.policy.ask_first import MAX_BY, check_id
from agentos.policy.ceiling import ControllerCeiling

KIND = "ask_first"
MAX_CHANGES = 500
REASONS = ("invalid", "controller_ceiling")
NONCES = "ask_first_nonces"


class AskFirstSync:
    """The node's half. `store` is the policy's AskFirst; `get_meta`/`set_meta` the channel store's."""

    def __init__(self, store: Any, *, ceiling: ControllerCeiling, get_meta: Callable[[str, Any], Any],
                 set_meta: Callable[[str, Any], None]):
        self.store = store
        self.ceiling = ceiling
        self.get_meta, self.set_meta = get_meta, set_meta

    def check_body(self, body: wire.Json) -> list:
        """After the signature: `ts` (required), a nonce not seen before, and a `changes` list."""
        if wire.parse_time(body.get("ts")) is None:
            raise InvalidRequest("ask_first needs ts (ISO 8601 UTC)")
        nonce = body.get("nonce")
        if not isinstance(nonce, str) or not NONCE_RE.match(nonce):
            raise InvalidRequest("ask_first needs a nonce (8 to 64 letters, digits, _ -)")
        changes = body.get("changes")
        if not isinstance(changes, list) or len(changes) > MAX_CHANGES:
            raise InvalidRequest(f"ask_first needs changes, a list of at most {MAX_CHANGES}")
        now = time.time()
        seen = {k: v for k, v in (self.get_meta(NONCES, {}) or {}).items() if v > now - NONCE_KEEP_S}
        if nonce in seen:
            raise PolicyDenied("this ask_first message was already applied (its nonce was seen before)")
        seen[nonce] = now  # kept only for a body that is applied, so a refused one does not use it up
        self.set_meta(NONCES, seen)
        return changes

    def apply(self, changes: list) -> dict:
        """Each change in order (the last for the same switch wins). The `ask_first_result` body."""
        out: dict[str, list] = {"applied": [], "refused": []}
        final: dict[tuple[str, str], dict] = {}
        for raw in changes:
            scope = raw.get("scope") if isinstance(raw, dict) else None
            ident = raw.get("id") if isinstance(raw, dict) else None
            label = {"scope": str(scope)[:8] if scope is not None else None,
                     "id": ident[:256] if isinstance(ident, str) else None}
            try:
                if not isinstance(raw, dict):
                    raise InvalidRequest("a change is an object")
                scope, ident = check_id(scope, ident)
                on = raw.get("ask_first")
                if not isinstance(on, bool):
                    raise InvalidRequest("ask_first is true or false")
                if not on and not self.ceiling.allows_effect(Effect.COMMIT):
                    out["refused"].append({**label, "code": "controller_ceiling",
                                           "message": "this node's owner does not let a controller approve a "
                                                      "commit, so it cannot let one run without asking either"})
                    continue
                by = raw.get("by")
                who = f"controller:{by[:MAX_BY]}" if isinstance(by, str) and by else "controller"
                setting = self.store.set(scope, ident, on, by=who, person=True)
            except AgentosError as exc:
                out["refused"].append({**label, "code": "invalid", "message": exc.message[:300]})
                continue
            final[(setting.scope, setting.id)] = setting.public()
        out["applied"] = list(final.values())
        return out
