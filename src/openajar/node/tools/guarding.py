"""A browser tool's call under the browser's egress guard: what it blocked, and what a publish
during the call is charged to (openajar.node.egress).
"""

from __future__ import annotations

import functools
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from openajar.core.errors import EgressRefused, OpenajarError
from openajar.core.tools import ToolContext
from openajar.node import commitgate, egress

log = logging.getLogger(__name__)


def _trace_blocks(ctx: ToolContext, on: str, blocks: list[egress.Block]) -> None:
    trace = getattr(ctx, "trace", None)
    if not callable(trace):
        return
    for b in blocks:
        try:
            trace("egress_blocked", lane=on, status="denied", summary=f"{b.rule}: {b.host}", args=b.public())
        except Exception:
            log.debug("could not trace an egress block", exc_info=True)


class ActMeter:
    """egress.CommitMeter for one tool call: a publish its act set off is one commit on the
    caller's grant and the browser (PolicyContext.charge_commit, openajar.policy.meters), traced as
    `commit_observed`. A context without meters (a bare ToolContext) charges nothing."""

    def __init__(self, ctx: ToolContext, on: str):
        self.ctx = ctx
        self.on = on
        self.code: str | None = None  # the last refusal's code: limit_reached or lane_paced

    def charge(self, rule: egress.EffectRule, *, sent: bool) -> str | None:
        why = self.ctx.charge_commit(self.on, sent=sent)  # type: ignore[attr-defined]
        self.code = (getattr(self.ctx, "commit_code", None) or "limit_reached") if why else None
        trace = getattr(self.ctx, "trace", None)
        if callable(trace):
            # the guard lets a publish go when charge() raises (LaneGuard._charge), so a trace that
            # fails must never take the decision with it: a refusal stays a refusal
            try:
                trace("commit_observed", lane=self.on, effect="commit", status="denied" if why else "ok",
                      summary=f"{rule.id}: {why}" if why else rule.id, effect_rule=rule.id, sent=sent)
            except Exception:  # the trace only; the charge above is done
                log.warning("could not trace a commit on %s (%s)", self.on, rule.id, exc_info=True)
        return why

    def capped(self) -> bool:
        return bool(self.ctx.commits_capped(self.on))  # type: ignore[attr-defined]

    @classmethod
    def of(cls, ctx: ToolContext, on: str) -> ActMeter | None:
        metered = callable(getattr(ctx, "charge_commit", None)) and callable(getattr(ctx, "commits_capped", None))
        return cls(ctx, on) if metered else None


@asynccontextmanager
async def guarded(ctx: ToolContext, on: str, page: Any, *, route: bool = True,
                  gate: commitgate.ActGate | None = None) -> AsyncIterator[list[dict[str, Any]]]:
    """Run an agent's action under the browser's egress guard. Yields the list the result reports
    as `egress_blocked`. A failed action that the guard stopped becomes egress_refused, naming
    the host; every block of the call goes to the action log. `route`: a strict browser's route is
    on for the action (waits pass False: they act on nothing). A publish the action sets off is
    charged as a commit (ActMeter). `gate`: the commit gate (AUD-03): every publish the action
    sets off waits for its approval; when the gate held one, a failed action fails with what the
    gate decided (approval_pending, a refusal), never as an egress refusal."""
    guard = egress.guard_for(page)
    report: list[dict[str, Any]] = []
    if guard is None:
        yield report
        return
    if gate is None and route:  # any act starts (or carries on) the agent's drive under the commit gate
        gate = commitgate.ActGate(ctx, on, call={"tool": "guarded"}, submit_like=False)
    if gate is not None:
        gate.uploads = guard.uploads
        gate.typed = guard.carries_body  # an unknown request asks a person only when it carries typed text
        gate.egress_trace = guard.trace  # an identity the gate does not know goes to the lane's gate log
        context = getattr(guard, "_context", None)
        if context is not None and callable(getattr(context, "cookies", None)):
            from openajar.node.sites import account_identity

            gate.identity = functools.partial(account_identity, context)  # the live session's account, for the card
    async with guard.acting(route=route, meter=ActMeter.of(ctx, on), gate=gate) as mark:
        if gate is not None:
            gate.active = True  # until the act ends: a request held between acts asks nobody
        try:
            yield report
            if gate is not None and gate.submit_like:
                await guard.settle(gate)  # a publish that lags the press (a captcha first) is still gated
        except OpenajarError as e:
            blocks = guard.blocks_since(mark)
            _trace_blocks(ctx, on, blocks)
            if gate is not None and gate.outcome is not None:
                raise gate.outcome from e
            # the page's own requests the gate held without asking did not make the act fail: its own error does
            refused = [b for b in blocks if b.rule != "unrecognised"]
            if refused and not isinstance(e, EgressRefused):
                raise EgressRefused(egress.refusal_message(refused[0])) from e
            raise
        finally:
            if gate is not None:
                gate.active = False
    blocks = guard.blocks_since(mark)
    _trace_blocks(ctx, on, blocks)
    report.extend(b.public() for b in blocks)


def main_frame_block(report: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The first block of the call that stopped the tab's own document, if any."""
    return next((b for b in report if b["kind"] in ("navigation", "redirect", "committed")), None)


EGRESS_NOTE = ("egress_blocked: requests the browser's guard refused while you acted. Rule unrecognised (often the "
               "page's own telemetry) was held without asking anyone, and your act still ran.")


def with_blocks(out: dict[str, Any], report: list[dict[str, Any]]) -> dict[str, Any]:
    """The result with the call's blocks, and once what they mean (the browser tools' descriptions leave it out:
    only an account browser's guard blocks)."""
    if report:
        out["egress_blocked"] = report
        out.setdefault("egress_note", EGRESS_NOTE)
    return out
