"""The browser tools that read or act on a page: browser_navigate, browser_snapshot, browser_act,
browser_scroll and browser_screenshot.
"""

from __future__ import annotations

import functools
import logging
from typing import Any

from openajar.core.errors import EgressRefused, InvalidRequest
from openajar.core.models import ActionKind, Effect
from openajar.core.tools import ToolContext, ToolImage
from openajar.node import commitgate, deadlines, egress, human
from openajar.node.claims import READ
from openajar.node.tools.form_check import check_form_unchanged, form_before_submit
from openajar.node.tools.guarding import guarded, main_frame_block, with_blocks
from openajar.node.tools.inputs import (
    MAX_TEXT_CHARS,
    NEEDS_TEXT,
    lane_id,
    normalize_ref,
    normalize_url,
    url_host,
)
from openajar.node.tools.outline import (
    SnapshotMode,
    active_page,
    outline_after,
    page_result,
    render_snapshot,
    snapshot_mode,
    with_outline,
)
from openajar.node.tools.session import (
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
# never raises. Every node runtime registers openajar.audit.captures.after_commit (the page after it, opt-in
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
    """Open a URL in the browser's active tab (a bare domain gets https). If the result's signal is
    not ok, stop and follow its instructions. With snapshot set, the result is this as one JSON line,
    then the page outline. A site outside the browser's site list or your grant's egress list
    (redirects too) is egress_refused: tell the human, do not retry. To see a page anew, snapshot it;
    do not reopen it.

    Args:
        url: The page to open, like https://example.com/
        snapshot: "full" (the outline once the page has loaded) or "delta" (what changed since your
            last snapshot of this tab).
        main_only: With a full outline, fold the header, menus, side columns and footer to a line
            each, to read a large page's main content.
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
        from openajar.node.flow_recorder import record_navigate

        record_navigate(ctx.runtime, on, result.url or target)
    out = with_blocks(page_result(on, result), report)
    return with_outline(out, outline) if outline is not None else out


async def browser_snapshot(ctx: ToolContext, *, on: str, main_only: bool = False) -> str:
    """Read the browser's active tab as an accessibility outline: roles, names and text, with a ref
    like [ref=e12] on everything you can act on (pass it to browser_act; images too, for hover), and a
    (hint: ...) on unnamed icon buttons. About as many tokens as the page has text, at most about
    10,000; a screenshot costs far more and has no refs. To see what an action changed, pass
    snapshot="delta" to browser_act instead. A dialog that freezes the page shows instead.

    Args:
        main_only: Fold the header, menus, side columns and footer to a line each, to read a large
            page's main content.
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
    """Act on an element by its ref from your latest snapshot, with human input. Actions: click,
    hover, scroll (wheel it into view), type, fill (clear, then type), select (an option by its label),
    press (a key such as Enter); type, fill, select and press need `text`. Send acts one at a time:
    calls sent together run in no set order. `changed` (when it can be read back) says whether the
    target's own state changed; a label changing nearby is not proof. With snapshot set, the result is
    this as one JSON line, then the outline. A click that publishes can return approval_pending: wait
    with approval_wait, then call again with the same arguments. Errors such as ref_not_found and
    dialog_pending say what to do next; nothing was sent when a publish fails.

    Args:
        ref: From your latest snapshot, like e12.
        action: What to do with the element.
        text: What to type or fill, the option to select, or the key to press.
        snapshot: "delta" (only the outline lines that changed since your last snapshot of this tab:
            the cheapest way to see what it did, also after a click opens another page) or "full".
    """
    target = normalize_ref(ref)
    if action in NEEDS_TEXT and not text:
        raise InvalidRequest(f"action {action.value} needs `text`")
    if text is not None and len(text) > MAX_TEXT_CHARS:
        raise InvalidRequest(f"text is {len(text)} characters; the limit is {MAX_TEXT_CHARS} (typing is "
                             "human-paced, so longer text would hold the browser for too long)")
    on = lane_id(on)
    mode = snapshot_mode(snapshot)
    # DRAFT: what a click does is not known before it. A request it sets off that matches a
    # commit rule is held by the commit gate (openajar.node.commitgate) until a person approved
    # exactly what it publishes, and is authorized (and metered) as a commit then.
    content: dict[str, Any] = {"ref": target, "action": action.value, "text": text}
    form, ungated = await form_before_submit(ctx, on, target, action, text)
    if form is not None:  # an approval of this submit shows (and binds) the fields it sends
        content["form"] = form
    # Ask me first on a site openajar has no publish rules for (finding 10 of 2026-11-10): the commit
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
                    from openajar.node.flow_recorder import record_act

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
    """Move further down the page with the real wheel: a few notches, at the browser's own pace, as a
    person keeps reading. Use it when what you need is not on the page yet, or a list loads as you
    go. browser_act's scroll only wheels one element into view; this moves the page itself. Returns
    url and signal; with snapshot set, the result is text: this result as one JSON line, then the
    outline ("delta" shows what the wheel brought in). If the signal is not ok, stop and follow the
    instructions for it. A list can load new content, and the browser's guard judges what that sends.
    Effect: read. It acts on the browser, so it takes its turn with other acting calls. Errors:
    dialog_pending (answer it with browser_dialog), engine_error (retry once).

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
    from openajar.node.interaction.dialogs import DialogPending, dialog_guard, dialog_notice

    guard = dialog_guard(page)
    pending = guard.pending_on(page) if guard is not None else None
    if pending is not None:
        raise DialogPending(f"cannot {doing}: {dialog_notice(pending)}")


async def browser_screenshot(ctx: ToolContext, *, on: str, full_page: bool = False, width: int | None = None,
                             proof: str | None = None) -> ToolImage | list[Any]:
    """A PNG of what the browser's active tab shows now. Only when the outline is not enough (layout,
    images, a canvas, a puzzle to describe to the human): it costs far more context than a snapshot
    and has no refs.

    Args:
        full_page: The whole page, not only the window.
        width: Lay the page out this wide (390: a phone). An environment's browser only.
        proof: A title to keep it as proof for the human ("cart after the fix"), with a link, also after
            the environment closes (browser_record keeps a video). An environment's browser only.
    """
    from openajar.node.tools import shots

    on = lane_id(on)
    shot_width, title = shots.check_width(width), shots.check_title(proof)
    env = shots.environment_of(ctx, on) if shot_width is not None or title is not None else None
    if (shot_width is not None or title is not None) and env is None:
        raise InvalidRequest("width and proof are for an environment's browser: call browser_screenshot without "
                             "`on` in a chat with an environment")
    await ctx.authorize(Effect.READ, lane_id=on, summary=f"screenshot {on}" + (f" as proof: {title}" if title else ""))
    interaction = ctx.runtime.interaction
    # another width lays the page out again, which a caller acting there would see: it takes its turn
    async with driven_session(ctx, on, **({} if shot_width is not None else {"mode": READ})) as page:
        if shot_width is not None:
            png = await shots.shot_at_width(interaction, page, shot_width, full_page=full_page)
        else:
            png = await (interaction.screenshot(page, full_page=True) if full_page else interaction.screenshot(page))
    url = getattr(page, "url", None)
    label_page_text(ctx, "browser_screenshot", url)
    image = ToolImage(data=png, mime_type="image/png")
    if title is None:
        return image
    kept = shots.keep(ctx, env, png, title=title, page_url=url, on=on)
    return [image, {"proof": kept, "next": "Give the human proof.url (it works for a few minutes; environment() "
                                           "gives a fresh one), or save it with curl -o and show the file."}]
