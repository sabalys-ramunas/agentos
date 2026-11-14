"""One prompt, one reply: a bounded task on the brain assigned to `review`, with no tools at all.

Like the browser worker's BrainText: `Requirements(tools=False, no_tools=True)` gives the brain no
tools, no MCP and no child grant, so the evidence (which could still carry a stranger's words in a
label) can only ever shape text a person reads. A reply is read whole or not at all: the
transcript keeps a preview, and when the reply is longer than what the brains service keeps of
it, the run asks again for fewer changes.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any

from agentos.core.errors import AgentosError
from agentos.core.models import Principal

PRINCIPAL = Principal(kind="flow", id="text-review", owner="owner", grant_id="owner")
CUT_RE = re.compile(r"\.\.\. \[(\d+) more chars\]$")  # how a transcript ends text it did not keep whole
TASK_END = frozenset({"succeeded_unverified", "failed", "cancelled", "interrupted", "waiting_approval"})
POLL_S = 1.0


class CallFailed(RuntimeError):
    """No usable reply. `cut_off`: there was one, longer than can be read back."""

    def __init__(self, message: str, *, cut_off: bool = False, usd: float = 0.0, tokens: int = 0):
        super().__init__(message)
        self.message, self.cut_off, self.usd, self.tokens = message, cut_off, usd, tokens


@dataclass(frozen=True)
class Reply:
    text: str
    usd: float = 0.0
    tokens: int = 0
    task: str | None = None
    notional_usd: float = 0.0  # what a subscription's answer would have cost at API prices
    model: str | None = None


class BrainCall:
    """`drive`: tick the brains service while waiting. A CLI process has no daemon loop doing it."""

    def __init__(self, brains: Any, *, brain: str, workspace: str, timeout_s: float, drive: bool = False,
                 poll_s: float = POLL_S):
        self.brains, self.brain, self.workspace = brains, brain, workspace
        self.timeout_s, self.drive, self.poll_s = timeout_s, drive, poll_s

    async def __call__(self, prompt: str, *, max_usd: float) -> Reply:
        from agentos.brains.models import Budget, Requirements

        started = time.monotonic()
        try:
            task = await self.brains.submit(PRINCIPAL, prompt=prompt, workspace=self.workspace, brain=self.brain,
                                            budget=Budget(max_usd=max_usd, max_turns=2),
                                            requires=Requirements(tools=False, no_tools=True))
            status = await self._finished(task["id"], started)
            events = self.brains.tail(task["id"], PRINCIPAL, limit=20)["events"]
        except AgentosError as e:
            raise CallFailed(f"{e.code}: {e.message}") from None
        usd, tokens = float(status.get("cost_usd") or 0), int(status.get("tokens") or 0)
        result = next((e for e in reversed(events) if e.get("kind") == "result"), {})
        if status.get("status") != "succeeded_unverified" or result.get("is_error"):
            raise CallFailed(f"{self.brain} did not finish the task ({status.get('status')}: "
                             f"{status.get('status_reason') or 'no reason given'})", usd=usd, tokens=tokens)
        text = self._whole(task["id"], str(result.get("text") or ""), status)
        if text is None:
            raise CallFailed(f"{self.brain} replied at more length than can be read back whole", cut_off=True,
                             usd=usd, tokens=tokens)
        if not text.strip():
            raise CallFailed(f"{self.brain} gave no reply", usd=usd, tokens=tokens)
        return Reply(text, usd=usd, tokens=tokens, task=task["id"], model=status.get("brain"),
                     notional_usd=float(status.get("notional_usd") or 0))

    def _whole(self, task_id: str, shown: str, status: dict[str, Any]) -> str | None:
        """The reply in full, or None when only part of it was kept. The transcript's preview says
        how long the reply was; the task's own copy is longer, and is used when it is all there."""
        cut = CUT_RE.search(shown)
        if not cut:
            return shown
        length = len(shown) - len(cut.group(0)) + int(cut.group(1))
        kept = str(status.get("result_preview") or "")
        return kept if len(kept) >= length else None

    async def _finished(self, task_id: str, started: float) -> dict[str, Any]:
        while True:
            if self.drive and callable(getattr(self.brains, "tick", None)):
                await self.brains.tick()
            status = self.brains.status(task_id, PRINCIPAL)
            if status.get("status") in TASK_END:
                return status
            if time.monotonic() - started > self.timeout_s:
                await self.brains.cancel(task_id, PRINCIPAL)
                raise CallFailed(f"{self.brain} took over {self.timeout_s:.0f} s to answer")
            await asyncio.sleep(self.poll_s)
