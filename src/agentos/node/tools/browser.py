"""The browser tools that read or act on a page: browser_navigate, browser_snapshot, browser_act,
browser_scroll and browser_screenshot.
"""

from __future__ import annotations

import functools
import logging
from typing import Any

from agentos.core.errors import EgressRefused, InvalidRequest
from agentos.core.models import ActionKind, Effect
from agentos.core.tools import ToolContext, ToolImage
from agentos.node import commitgate, deadlines, egress, human
from agentos.node.claims import READ
from agentos.node.tools.form_check import check_form_unchanged, form_before_submit
from agentos.node.tools.guarding import guarded, main_frame_block, with_blocks
from agentos.node.tools.inputs import (
    MAX_TEXT_CHARS,
    NEEDS_TEXT,
    lane_id,
    normalize_ref,
    normalize_url,
    url_host,
)
from agentos.node.tools.outline import (
    SnapshotMode,
    active_page,
    outline_after,
    page_result,
    render_snapshot,
    snapshot_mode,
    with_outline,
)
from agentos.node.tools.session import (
    _acts_alone,
    _link_click_recorder,
    _recording,
    _takes,
    driven_session,
    label_page_text,
    lane_asks_first,
)

log = logging.getLogger(__name__)


# The runtime service an agent's commit on a lane is handed to, after the act: `async (ctx, on, page)`, which
# never raises. Every node runtime registers agentos.audit.captures.after_commit (the page after it, opt-in
# per lane, for the replay).
AFTER_COMMIT = "after_commit"
SUMMARY_TEXT_CHARS = 80


def act_summary(on: str, ref: str, action: ActionKind, text: str | None) -> str:
    summary = f"{action.value} {ref} on {on}"
    if text:
        quoted = text if len(text) <= SUMMARY_TEXT_CHARS else text[:SUMMARY_TEXT_CHARS] + "..."
        summary += f' with "{quoted}"'
    return summary


async def browser_navigate(ctx: ToolContext, *, on: str, url: str,
                           snapshot: SnapshotMode = "none", main_only: bool = False) -> dict[str, Any] | str:
    """Open a URL in the lane's active tab (http or https; a bare domain gets https). Returns the
    final url, title, HTTP status and signal (with signal_detail and account when there is something
    to say). If the signal is not ok, stop and follow the instructions for it. With snapshot set,
    the result is text: this result as one JSON line, then the page outline (no browser_snapshot
    call needed). Effect: read (when you may act here without a person and the page links to exactly
    this address, it clicks that link instead). Errors: invalid (not a web URL, or an address on the node or its network),
    egress_refused (outside the lane's site list or your grant's egress list, redirects included:
    tell the human, do not retry), dialog_pending (answer it with browser_dialog_answer first),
    engine_error (retry once). Do not reopen a page to see it anew: snapshot it.

    Args:
        url: The page to open, for example https://www.reddit.com/
        snapshot: "none" (default), "full" (the outline once the page has loaded) or "delta" (what
            changed since your last snapshot of this tab).
        main_only: With a full outline, fold the page header, menus, side columns and footer to one
            line each with a count, to read a large page's main content for fewer tokens.
    """
    target = normalize_url(url)
    on = lane_id(on)
    mode = snapshot_mode(snapshot)
    # a navigation carries its whole URL (the query too) to the host, so under taint it is a sink
    # like a post: outside the grant's egress allowlist it waits for the owner (AUD-09)
    sink = {"facts": {"sink": True}} if _takes(ctx.authorize, "facts") else {}
    await ctx.authorize(Effect.READ, lane_id=on, domain=url_host(target), summary=f"open {target} in {on}",
                        content={"url": target}, **sink)
    interaction = ctx.runtime.interaction
    async with driven_session(ctx, on) as page:
        async with guarded(ctx, on, page) as report:
            guard = egress.guard_for(page)
            if guard is not None:
                guard.check_url(target)
            # a click runs the page's handlers, which a read must not do: only a caller whose
            # browser_act would run on its own may have the link followed
            follow: dict[str, Any] = {}
            if _acts_alone(ctx, Effect.DRAFT, on):
                follow = {"follow_link": True, "on_click": _link_click_recorder(ctx, on, target)}
            result = await interaction.navigate(page, target, **follow)
        refused = main_frame_block(report)
        if refused is not None:  # a redirect hop the guard stopped: the page stayed where it was
            raise EgressRefused(egress.refusal_message(refused))
        outline = await outline_after(ctx, on, page, mode, settle=True, main_only=main_only) if mode != "none" else None
    label_page_text(ctx, "browser_navigate", result.url or target)
    if _recording(ctx.runtime, on):
        from agentos.node.flow_recorder import record_navigate

        record_navigate(ctx.runtime, on, result.url or target)
    out = with_blocks(page_result(on, result), report)
    return with_outline(out, outline) if outline is not None else out


async def browser_snapshot(ctx: ToolContext, *, on: str, main_only: bool = False) -> str:
    """Read the lane's active tab as an accessibility outline: roles, names and text, with a ref
    like [ref=e12] on everything you can act on (pass it to browser_act); unnamed icon buttons get a
    (hint: ...) read from their attributes. The header shows the url, signal (and account) and ref
    count. Costs about as many tokens as the page has text, at most about 10,000; a screenshot costs
    far more and has no refs. To see what an action changed, pass snapshot="delta" to browser_act
    instead of calling this again. Never changes the page, so it runs while another caller claims
    the lane. A page frozen by a dialog shows the dialog instead. Images keep their ref too, so
    hover reaches one that shows something on hover. Effect: read. Errors: engine_error (retry once).

    Args:
        main_only: Fold the page header, menus, side columns and footer to one line each with a
            count, to read a large page's main content for fewer tokens. Off by default.
    """
    on = lane_id(on)
    await ctx.authorize(Effect.READ, lane_id=on, summary=f"read the page outline in {on}")
    interaction = ctx.runtime.interaction
    async with driven_session(ctx, on, mode=READ) as page:
        snap = await interaction.snapshot(page)
        label_page_text(ctx, "browser_snapshot", getattr(snap, "url", None) or getattr(page, "url", None))
        return render_snapshot(ctx, on, page, snap, "full", labeled=False, main_only=main_only)


async def browser_act(
    ctx: ToolContext,
    *,
    on: str,
    ref: str,
    action: ActionKind,
    text: str | None = None,
    snapshot: SnapshotMode = "none",
) -> dict[str, Any] | str:
    """Act on an element by its ref from your latest snapshot, with human input (pauses, real
    clicks, typing at human rhythm). Actions: click, hover, scroll (wheel it into
    view), type, fill (clear, then type), select (an option by its label), press (a key such as
    Enter). type, fill, select and press need `text`. Send acts one at a time: calls sent together
    run in no set order. Returns ok, url, signal and, when it can be
    read back, whether the target's own state changed (`changed`, with `detail`); a label changing
    nearby is not proof. With snapshot set, the result is text: this result as one JSON line, then
    the outline. Effect: draft.

    A click or Enter that publishes (a post, a comment, a form that needs approval) counts as a
    commit against your grant's and the lane's limits and posting pace (limit_reached, lane_paced).
    Where Ask me first is on, it is held and nothing is sent: status approval_pending with an
    approval_id. Tell the user, wait with approval_wait, then call again with the same arguments: it
    posts only if it sends exactly what was approved. A result can list `egress_blocked`: requests
    the lane's guard refused while you acted; rule `unrecognised` (often the page's own telemetry)
    was held without asking anyone, and your act still ran.

    Errors, each with its next step: ref_not_found, egress_refused (do not retry or look for another
    way there), form_changed, publish_changed, publish_unbound, denied, dialog_pending, invalid,
    engine_error. Nothing was sent when a publish fails.

    Args:
        ref: A ref from your latest snapshot, for example e12.
        action: Required: what to do with the element.
        text: Text to type or fill, the option label to select, or the key to press.
        snapshot: "none" (default), "delta" (only the outline lines that appeared, went away or
            changed since your last snapshot of this tab, with their refs: the cheapest way to see
            what the action did, also after a click that opens another page) or "full".
    """
    target = normalize_ref(ref)
    if action in NEEDS_TEXT and not text:
        raise InvalidRequest(f"action {action.value} needs `text`")
    if text is not None and len(text) > MAX_TEXT_CHARS:
        raise InvalidRequest(f"text is {len(text)} characters; the limit is {MAX_TEXT_CHARS} (typing is "
                             "human-paced, so longer text would hold the lane for too long)")
    on = lane_id(on)
    mode = snapshot_mode(snapshot)
    # DRAFT: what a click does is not known before it. A request it sets off that matches a
    # commit rule is held by the commit gate (agentos.node.commitgate) until a person approved
    # exactly what it publishes, and is authorized (and metered) as a commit then.
    content: dict[str, Any] = {"ref": target, "action": action.value, "text": text}
    form, ungated = await form_before_submit(ctx, on, target, action, text)
    if form is not None:  # an approval of this submit shows (and binds) the fields it sends
        content["form"] = form
    # Ask me first on a site agentos has no publish rules for (finding 10 of 2026-11-10): the commit
    # gate never sees a form post there, so a click that submits a form by POST to such a host is
    # authorized as a commit on a lane that asks first, and waits for a person (the form card)
    effect, summary = Effect.DRAFT, act_summary(on, target, action, text)
    if ungated is not None and lane_asks_first(ctx.runtime, on):
        effect, summary = Effect.COMMIT, f"Submit a form to {ungated} through {on} (Ask me first is on)"
        if action is not ActionKind.CLICK:  # a key or typed text sends it: say which, and what is typed
            summary += f": {act_summary(on, target, action, text)}"
    await ctx.authorize(effect, lane_id=on, summary=summary, content=content)
    interaction = ctx.runtime.interaction
    gate = commitgate.ActGate(ctx, on, call={"ref": target, "action": action.value, "text": text},
                              submit_like=egress._press_mode(action.value, text) is not None)
    async with driven_session(ctx, on, extra_s=deadlines.typing_s(text) if action in NEEDS_TEXT else 0.0) as page:
        async with guarded(ctx, on, page, gate=gate) as report:
            guard = egress.guard_for(page)
            if guard is not None:  # the link or form this would follow, read before acting
                await guard.check_action(page, target, action, text)
                if action in (ActionKind.TYPE, ActionKind.FILL):
                    guard.note_typed(text)  # a request that carries it may be the agent's publish
                elif action is ActionKind.PRESS:
                    guard.note_key(text)  # a printable key is typed text too, one character at a time
            recheck = None
            if form is not None:  # the approval bound this form: what the click sends must still be it,
                await check_form_unchanged(page, target, action, form, text)  # now, and again just before the press
                recheck = functools.partial(check_form_unchanged, page, target, action, form, text)
            with human.before_press(recheck):
                if _recording(ctx.runtime, on):  # a flow recording is on: also capture a structural locator
                    from agentos.node.flow_recorder import record_act

                    result = await record_act(ctx.runtime, on, page, target, action, text,
                                              lambda: interaction.act(page, target, action, text))
                else:
                    result = await interaction.act(page, target, action, text)
        gate.raise_outcome()  # a publish waits for a person (approval_pending) or was refused; it says what was sent
        outline = await outline_after(ctx, on, page, mode) if mode != "none" else None
        if getattr(ctx, "effect", None) is Effect.COMMIT:  # opt-in per lane: the page after it, for the replay
            after_commit = ctx.runtime.get(AFTER_COMMIT)
            if after_commit is not None:
                await after_commit(ctx, on, page)
    label_page_text(ctx, "browser_act", getattr(result, "url", None) or getattr(page, "url", None))
    trace_read_back(ctx, on, result)
    out = with_blocks(page_result(on, result), report)
    return with_outline(out, outline) if outline is not None else out


def trace_read_back(ctx: ToolContext, on: str, result: Any) -> None:
    """The act's read-back on the record (`act_read_back`, whether the target's own state changed), so
    History can say an act was checked. Only the flag: never the page's text. A trace that fails is only logged."""
    changed = getattr(result, "changed", None)
    trace = getattr(ctx, "trace", None)
    if changed is None or not callable(trace):
        return
    try:
        trace("act_read_back", lane=on, status="ok", changed=bool(changed),
              action=str(getattr(getattr(result, "action", None), "value", "") or ""))
    except Exception:  # the record only; the act is done
        log.debug("could not trace the read-back of an act", exc_info=True)


async def browser_scroll(ctx: ToolContext, *, on: str, snapshot: SnapshotMode = "none") -> dict[str, Any] | str:
    """Move further down the page with the real wheel: a few notches, at the lane's own pace, as a
    person keeps reading. Use it when what you need is not on the page yet, or a list loads as you
    go. browser_act's scroll only wheels one element into view; this moves the page itself. Returns
    url and signal; with snapshot set, the result is text: this result as one JSON line, then the
    outline ("delta" shows what the wheel brought in). If the signal is not ok, stop and follow the
    instructions for it. A list can load new content, and the lane's guard judges what that sends.
    Effect: read. It acts on the lane, so it takes its turn with other acting calls. Errors:
    dialog_pending (answer it with browser_dialog_answer), engine_error (retry once).

    Args:
        snapshot: "none" (default), "delta" (only the outline lines that changed) or "full".
    """
    on = lane_id(on)
    mode = snapshot_mode(snapshot)
    await ctx.authorize(Effect.READ, lane_id=on, summary=f"scroll down the page in {on}")
    interaction = ctx.runtime.interaction
    async with driven_session(ctx, on) as page:
        target = active_page(page)
        _no_open_dialog(target, "scroll")
        gate = commitgate.ActGate(ctx, on, call={"tool": "browser_scroll"}, submit_like=False)
        async with guarded(ctx, on, page, gate=gate) as report:
            await human.wheel_down(target)
        gate.raise_outcome()  # a publish a page sent during the wheel waits for a person: say so, or its card is orphaned
        signal = await interaction.signal(target)
        outline = await outline_after(ctx, on, page, mode) if mode != "none" else None
    url = getattr(target, "url", None)
    label_page_text(ctx, "browser_scroll", url)
    out = with_blocks({"on": on, "ok": True, "url": url, "signal": getattr(signal, "value", signal)}, report)
    return with_outline(out, outline) if outline is not None else out


def _no_open_dialog(page: Any, doing: str) -> None:
    """A page frozen by a dialog takes no wheel: say so, as every other acting tool does."""
    from agentos.node.interaction.dialogs import DialogPending, dialog_guard, dialog_notice

    guard = dialog_guard(page)
    pending = guard.pending_on(page) if guard is not None else None
    if pending is not None:
        raise DialogPending(f"cannot {doing}: {dialog_notice(pending)}")


async def browser_screenshot(ctx: ToolContext, *, on: str) -> ToolImage:
    """A PNG of what the lane's active tab shows right now. Use it only when the snapshot outline is
    not enough (layout, images, a canvas, a puzzle you must describe to the human): it costs far
    more context than a snapshot, often more than a thousand tokens, and has no refs to act on.
    A read: it runs alongside other reads and while another caller claims the lane. Effect: read.
    Errors: dialog_pending (answer it with browser_dialog_answer), engine_error (retry once)."""
    on = lane_id(on)
    await ctx.authorize(Effect.READ, lane_id=on, summary=f"screenshot {on}")
    interaction = ctx.runtime.interaction
    async with driven_session(ctx, on, mode=READ) as page:
        png = await interaction.screenshot(page)
    label_page_text(ctx, "browser_screenshot", getattr(page, "url", None))
    return ToolImage(data=png, mime_type="image/png")
