"""browser_wait: wait for text, a ref's state, a quiet network or a URL, without polling with
snapshots.
"""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, Field

from agentos.core.errors import InvalidRequest, ServiceUnavailable
from agentos.core.models import ControlState, Effect
from agentos.core.pauses import paused_words
from agentos.core.tools import ToolContext, in_person
from agentos.node.claims import READ
from agentos.node.tools.guarding import guarded, with_blocks
from agentos.node.tools.inputs import MAX_URL_CHARS, lane_id, normalize_ref
from agentos.node.tools.outline import SnapshotMode, outline_after, page_result, snapshot_mode, with_outline
from agentos.node.tools.session import driven_session, label_page_text

WAIT_MAX_S = 60.0          # browser_wait never holds a lane longer than this
WAIT_DEFAULT_S = 10.0
WAIT_TEXTS = 10            # texts per for_text or gone_text
WAIT_TEXT_CHARS = 200
QUIET_MS_RANGE = (100, 10_000)


# ---- waits ----

RefStateName = Literal["visible", "hidden", "enabled", "disabled", "checked", "unchecked", "pressed", "unpressed",
                       "expanded", "collapsed", "selected", "focused", "changed"]


class RefState(BaseModel):
    ref: str = Field(description="A ref from your latest snapshot.")
    state: RefStateName = Field(description=(
        "hidden is also met when it leaves the page; changed is any change in its own state."))


WAIT_NEXT = {
    "timeout": ("Not met yet: `conditions` shows what each one saw last. Call browser_wait again to keep "
                "waiting, or take a snapshot to look."),
    "dialog": "A dialog opened (see `dialog`): answer it with browser_dialog_answer, then wait again if needed.",
    "signal": "The page signal is no longer ok: stop and follow the instructions for it.",
    "tab_closed": "The tab closed: browser_tabs lists what is open; take a snapshot of the tab the lane acts in now.",
    "human_in_control": "A human took the lane: do not act on it; wait with lane_wait_control.",
}
WAIT_REFS = ("The page was re-read like a snapshot: refs of elements that changed are replaced. Take a "
             "snapshot (or wait with snapshot=\"delta\") before acting on them.")


def wait_texts(value: str | list[str] | None, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    items = [value] if isinstance(value, str) else list(value)
    texts = tuple(t.strip() for t in items if isinstance(t, str) and t.strip())
    if not texts:
        raise InvalidRequest(f"{name} is empty; give a text or a list of texts")
    if len(texts) > WAIT_TEXTS:
        raise InvalidRequest(f"{name} has {len(texts)} texts; at most {WAIT_TEXTS}")
    if any(len(t) > WAIT_TEXT_CHARS for t in texts):
        raise InvalidRequest(f"each text in {name} is at most {WAIT_TEXT_CHARS} characters")
    return texts


def wait_timeout(timeout_s: float) -> tuple[float, str | None]:
    try:
        timeout = float(timeout_s)
    except (TypeError, ValueError):
        raise InvalidRequest("timeout_s must be a number of seconds") from None
    if math.isnan(timeout) or timeout < 0:
        raise InvalidRequest("timeout_s must be 0 or more seconds")
    if timeout > WAIT_MAX_S:
        return WAIT_MAX_S, f"timeout_s is capped at {WAIT_MAX_S:g} s; waited at most that long"
    return timeout, None


def control_stop(ctx: ToolContext, on: str) -> Any:
    """`should_stop` for a wait: a reason once a human takes the lane or stops agents on it."""
    lanes = ctx.runtime.lanes
    if in_person(ctx):
        return None

    def should_stop() -> str | None:
        try:
            state = lanes.get(on).control.state
        except Exception:  # noqa: BLE001 - a manager without control state never stops a wait
            return None
        if state is ControlState.HUMAN:
            return "human_in_control"
        if state is ControlState.PAUSED:
            return "agent_paused"
        return None

    return should_stop


def _control(ctx: ToolContext, on: str) -> Any:
    try:
        return ctx.runtime.lanes.get(on).control
    except Exception:  # noqa: BLE001 - the lane went away: the words of a person's stop
        return None


async def browser_wait(
    ctx: ToolContext,
    *,
    on: str,
    for_text: str | list[str] | None = None,
    gone_text: str | list[str] | None = None,
    exact: bool = False,
    for_ref_state: RefState | None = None,
    network_quiet_ms: int | None = None,
    url_matches: str | None = None,
    timeout_s: float = WAIT_DEFAULT_S,
    snapshot: SnapshotMode = "none",
) -> dict[str, Any] | str:
    """Wait for something on the page instead of polling with snapshots: a text appears (for_text)
    or goes away (gone_text), an element reaches a state (for_ref_state), the network goes quiet
    (network_quiet_ms) or the URL matches (url_matches). Give at least one; all must hold at once.
    Returns as soon as they do, or after timeout_s (at most 60 s) with met false and what each
    condition saw last: call again to keep waiting. It stops early, with `stopped` saying why, on a
    dialog, a challenge signal, a human taking the lane or the tab closing. Text matches the outline
    (part of a name or text, any case or spacing), in the account's language. Text waits re-read the page, so changed
    elements get new refs. It runs while another caller claims the lane. Effect: read. Errors:
    invalid, ref_not_found, dialog_pending.

    Args:
        for_text: A text to wait for, or a list where any one appearing is enough.
        gone_text: A text that must go away, or a list that must all go away.
        exact: Match whole names or texts only, so "Post" does not match "Posts".
        for_ref_state: An element from your latest snapshot and its state, for example
            {"ref": "e12", "state": "enabled"}.
        network_quiet_ms: No request started or ended for this long, 100 to 10000. Requests open
            over 20 s do not count: for a long upload, wait for the site's own text.
        url_matches: Part of the URL, or a glob over the whole URL when it has a *.
        timeout_s: Seconds to wait, 0 to 60. 0 checks once.
        snapshot: "none" (default), "delta" or "full": the outline when the wait ends.
    """
    on = lane_id(on)
    mode = snapshot_mode(snapshot)
    texts, gone = wait_texts(for_text, "for_text"), wait_texts(gone_text, "gone_text")
    ref = state = None
    if for_ref_state is not None:
        if isinstance(for_ref_state, dict):
            for_ref_state = RefState.model_validate(for_ref_state)
        ref, state = normalize_ref(for_ref_state.ref), for_ref_state.state
    if network_quiet_ms is not None and not QUIET_MS_RANGE[0] <= network_quiet_ms <= QUIET_MS_RANGE[1]:
        raise InvalidRequest(f"network_quiet_ms must be {QUIET_MS_RANGE[0]} to {QUIET_MS_RANGE[1]}")
    if url_matches is not None:
        url_matches = url_matches.strip()
        if not url_matches or len(url_matches) > MAX_URL_CHARS:
            raise InvalidRequest("url_matches must be part of a URL, or a glob with *")
    timeout, note = wait_timeout(timeout_s)
    from agentos.node.interaction.waits import WaitFor

    spec = WaitFor(for_text=texts, gone_text=gone, exact=exact, ref=ref, ref_state=state,
                   network_quiet_ms=network_quiet_ms, url_matches=url_matches, timeout_s=timeout)
    if spec.empty:
        raise InvalidRequest("nothing to wait for: give for_text, gone_text, for_ref_state, network_quiet_ms "
                             "or url_matches")
    wanted = ", ".join(k for k, v in (("for_text", texts), ("gone_text", gone), ("for_ref_state", ref),
                                      ("network_quiet_ms", network_quiet_ms), ("url_matches", url_matches)) if v)
    await ctx.authorize(Effect.READ, lane_id=on, summary=f"wait in {on} for {wanted} (up to {timeout:g} s)")
    interaction = ctx.runtime.interaction
    wait = getattr(interaction, "wait", None)
    if wait is None:
        raise ServiceUnavailable("this node's browser interaction cannot wait")
    async with driven_session(ctx, on, mode=READ, extra_s=timeout) as page:
        async with guarded(ctx, on, page, route=False) as report:
            result = await wait(page, spec, should_stop=control_stop(ctx, on))
        outline = None
        if mode != "none" and result.stopped in ("met", "timeout", "signal"):
            outline = await outline_after(ctx, on, page, mode)
    out = with_blocks(page_result(on, result), report)
    if out.get("dialog") is None:
        out.pop("dialog", None)
    if note:
        out["note"] = note
    if result.stopped in WAIT_NEXT:
        out["next"] = WAIT_NEXT[result.stopped]
    elif result.stopped == "agent_paused":  # who paused it and why (agentos.core.pauses)
        out["next"] = paused_words(on, _control(ctx, on))
    if result.reread and outline is None:
        out["refs"] = WAIT_REFS
    label_page_text(ctx, "browser_wait", getattr(result, "url", None))  # page text: untrusted (AUD-31)
    return with_outline(out, outline) if outline is not None else out
