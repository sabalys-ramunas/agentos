"""MCP tools of the stealth package: `browser_check`, the per-lane safety checklist (openajar.stealth.lanecheck).

Effect read. It never warms a browser, opens a page or contacts an account's site, so an agent can call
it before it starts on a browser. A grant sees only the browsers it covers.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from openajar.core.errors import InvalidRequest
from openajar.core.models import Effect, parse_lane_id
from openajar.core.tools import ToolContext, ToolSpec

NEXT = {
    "ok": "Safe to use. Go ahead.",
    "warn": ("Usable, but tell the human about the warnings (each has a fix) when you report back. Do not "
             "try to fix them yourself."),
    "fail": ("Stop. Do not use this browser and do not retry or call this check in a loop: tell the human which "
             "check failed and its fix, then wait for them."),
}

OnArg = Annotated[str | None, Field(
    description='Browser id as "workspace/name", for example "biz-a/instagram". Leave it out to check every '
                "browser you may use.")]


def _result(report: Any) -> dict[str, Any]:
    out = report.to_dict()
    out["next"] = NEXT.get(out["level"], NEXT["warn"])
    return out


def tool_specs(runtime: Any) -> list[ToolSpec]:
    async def lane_check(ctx: ToolContext, *, on: OnArg = None) -> dict[str, Any]:
        """Is this browser safe to use right now? A checklist: proxy alive, exit IP and its location against
        the browser's pinned timezone and locale, WebRTC and DNS leaks, profile and fingerprint pin, still
        signed in (from the session cookies already in the profile), the expected account, and any hold or
        quarantine with who can clear it. Each check is ok, warn, fail or unknown with one sentence and a
        fix. It never warms the browser, opens a page or contacts the account's site, so call it before you
        start work on a browser. On a fail, stop: do not use the browser, do not retry, and tell the human which
        check failed and its fix. "unknown" alone does not make a browser unsafe. Effect: read. Errors:
        lane_not_found (see browsers_list)."""
        checker = ctx.runtime.require("lane_check")
        if on is not None:
            try:
                parse_lane_id(on)
            except ValueError as exc:
                raise InvalidRequest(str(exc)) from None
            await ctx.authorize(Effect.READ, lane_id=on, summary=f"check browser {on}")
            return _result(await checker.check(on))
        await ctx.authorize(Effect.READ, summary="check every browser")
        allowed = getattr(ctx, "can_access_lane", None)  # a grant sees only the lanes it covers
        ids = [i for i in checker.lane_ids() if allowed is None or allowed(i)]
        reports = [_result(r) for r in await checker.check_all(ids)]
        failing = [r["lane"] for r in reports if r["level"] == "fail"]
        return {"lanes": reports, "failing": failing,
                "next": NEXT["fail"] if failing else NEXT["ok"] if all(r["level"] == "ok" for r in reports)
                else NEXT["warn"]}

    return [ToolSpec("browser_check", lane_check, Effect.READ, lane_arg="on", hidden_without="lane_check")]
