"""How the pilot reaches a lane: the node's own tools, under its child grant, and nothing else.

There is no input layer here. A click is browser_act and a scroll is browser_scroll, each through
the policy wrapper an MCP call takes, so the grant, the effect routing, the commit gate, the egress
guard, the lane's claim and the action log all apply unchanged, and every press, key and wheel
notch comes from agentos.node.human. Nothing here touches the flow driver, whose `guard.flow` would
lift the agent grant.

The one thing no tool does is look at a fresh page. `look` is human.dwell at the flow driver's
DWELL_MS, as flows do, so a 200 ms decision never becomes a 200 ms glance. It moves nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Protocol


class ToolFailed(RuntimeError):
    """A tool call that failed, as its stable code and message."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class Tools(Protocol):
    async def call(self, name: str, **arguments: Any) -> Any:
        """Run one tool. Raises ToolFailed; an approval comes back as a result, as over MCP."""

    async def look(self) -> None:
        """A person's look at a page that just loaded."""


class NodeTools:
    """The tools of one pilot run: each wrapped by the policy, bound to the run's child token and
    its own session, so the lane's claim knows this run as one caller. Only `allowed` exist here,
    and the child grant's pins refuse the rest on the policy's side as well.

    An MCP call's result is masked on its way out (agentos.policy.redactor). A call from inside the node
    skips that layer, so results pass through the same masking here before any model reads them."""

    def __init__(self, runtime: Any, *, lane: str, token: str, grant_id: str, run_id: str, allowed: Iterable[str],
                 specs: dict[str, Any], wrap: Callable[..., Any], bind: Callable[..., Any],
                 redact: Callable[[Any], Any]):
        self.runtime, self.lane, self.token, self.grant_id, self.run_id = runtime, lane, token, grant_id, run_id
        self.session = f"pilot:{run_id}"
        self.bind, self.redact = bind, redact
        self.calls = {name: wrap(specs[name], runtime) for name in allowed if name in specs}

    async def call(self, name: str, **arguments: Any) -> Any:
        fn = self.calls.get(name)
        if fn is None:
            raise ToolFailed("denied", f"{name} is not one of this run's tools")
        with self.bind(self.token, session=self.session, run_id=self.run_id):
            try:
                return self.redact(await fn(**arguments))
            except Exception as e:
                code = getattr(e, "code", None)
                if not isinstance(code, str):
                    raise
                raise ToolFailed(code, self.redact(str(getattr(e, "message", "") or e))) from None

    async def look(self) -> None:
        from agentos.node import human
        from agentos.node.claims import READ
        from agentos.node.flow_driver import DWELL_MS
        from agentos.node.tools import driven_session
        from agentos.policy.context import CallMeta

        policy = self.runtime.require("policy")
        ctx = policy.context_for_grant(self.grant_id, tool="pilot_look", lane_id=self.lane,
                                       meta=CallMeta(session=self.session, run_id=self.run_id))
        try:
            async with driven_session(ctx, self.lane, mode=READ) as page:
                await human.dwell(page, *DWELL_MS)
        except Exception as e:
            code = getattr(e, "code", None)
            if not isinstance(code, str):
                raise
            raise ToolFailed(code, self.redact(str(getattr(e, "message", "") or e))) from None
