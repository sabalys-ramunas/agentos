"""The recorder's locator: while a flow is recorded, each act also gets a structural locator
(role and name, anchored on stable ancestors, CSS only as a fallback) that resolves to exactly
the element the agent acted on, checked with the flow driver itself.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from openajar.core.errors import InvalidRequest, RefNotFound
from openajar.core.text import first_line

# The runner's driver contract comes through flow_driver, the one node module that implements it
# (scripts/layers.toml).
from openajar.node.flow_driver import (
    ANCHOR_ROLES,
    EVAL_TIMEOUT_MS,
    MAX_ANCHOR_DEPTH,
    MAX_ATTRS,
    NO_NEST_ROLES,
    SNAPSHOT_TIMEOUT_MS,
    AmbiguousLocator,
    ElementNotFound,
    Locator,
    PlaywrightFlowDriver,
)
from openajar.node.flow_match import _css_string, name_variants, ref_role_and_name
from openajar.node.flow_scripts import (
    _ANCESTORS_JS,
    _IS_CONNECTED_JS,
    _STABLE_ATTRS_JS,
    PATH_JS,
    ROLE_JS,
    STATE_JS,
)
from openajar.node.human import quietly

log = logging.getLogger("openajar.flow_driver")
Page = Any     # playwright.async_api.Page
PwLocator = Any  # playwright.async_api.Locator
Handle = Any   # playwright.async_api.ElementHandle


# ---------------------------------------------------------------- the recorder's locator


class _Builder:
    """Builds and verifies structural locators on one page (uses the driver's resolver)."""

    def __init__(self, page: Page):
        self.driver = PlaywrightFlowDriver(page, lane_id="rec/rec", dwell_ms=(0, 0))

    async def candidates(self, loc: Locator) -> list[str] | None:
        try:
            return [m.path for m in await self.driver._resolve_all(loc)]
        except (ElementNotFound, AmbiguousLocator):
            return None

    async def verify(self, loc: Locator, path: str) -> bool:
        try:
            return (await self.driver._resolve_one(loc)).path == path
        except (ElementNotFound, AmbiguousLocator):
            return False

    async def best_in(self, anchor: Locator | None, path: str, role: str,
                      name: str | None) -> tuple[Locator | None, bool]:
        """The most durable name variant that leads back to `path` in this scope, and whether it
        needs no nth. A unique match wins; otherwise the variant with the fewest equal matches."""
        best: Locator | None = None
        best_count = 0
        for value, match in name_variants(name):
            loc = Locator(role=role, name=value, match=match, within=anchor)
            found = await self.candidates(loc)
            if not found or path not in found:
                continue
            if len(found) > 1:
                loc = loc.model_copy(update={"nth": found.index(path)})
            if not await self.verify(loc, path):
                continue
            if len(found) == 1:
                return loc, True
            if best is None or len(found) < best_count:
                best, best_count = loc, len(found)
        return best, False

    async def build(self, path: str, role: str, name: str | None, ancestors: list[dict[str, Any]],
                    depth: int = 0) -> Locator | None:
        """A verified locator for the element at `path`. The target is tried within its nearest
        named anchor first (a post's Like, never a comment's), then unnamed ones (a landmark, a
        list item); an anchor itself is tried on the whole page first and nested in its own
        anchors only when it repeats. nth only as a last resort."""
        chains = [ancestors[i:] for i in range(len(ancestors))] if depth < MAX_ANCHOR_DEPTH else []
        chains.sort(key=lambda c: not c[0]["name"])  # named anchors first, each group nearest first
        scopes = [[], *chains] if depth > 0 else [*chains, []]
        fallback: Locator | None = None
        for chain in scopes:
            anchor: Locator | None = None
            if chain:
                anc = chain[0]
                anchor = await self.build(anc["path"], anc["role"], anc["name"] or None, chain[1:], depth + 1)
                if anchor is None:
                    continue
            loc, unique = await self.best_in(anchor, path, role, name)
            if loc is not None and unique:
                return loc
            if loc is not None and fallback is None:
                fallback = loc
        return fallback

    async def css_fallback(self, target: PwLocator, path: str) -> Locator | None:
        """For an element without a usable role: a stable test id, id or form field name."""
        info = await target.evaluate(_STABLE_ATTRS_JS, timeout=EVAL_TIMEOUT_MS)
        options = []
        if info.get("testid"):
            options.append(f'[data-testid="{_css_string(info["testid"])}"]')
        if info.get("id") and re.fullmatch(r"[A-Za-z][\w-]*", info["id"]) and not re.search(r"\d{3,}", info["id"]):
            options.append(f"#{info['id']}")
        if info.get("name") and info.get("tag") in ("input", "select", "textarea", "button"):
            options.append(f'{info["tag"]}[name="{_css_string(info["name"])}"]')
        for css in options:
            loc = Locator(css=css)
            if await self.verify(loc, path):
                return loc
        return None


async def structural_locator_for(page: Page, ref: str, *, snapshot: str | None = None) -> Locator:
    """A durable locator for the element a snapshot ref points at (role, accessible name,
    nth among equal matches, the nearest anchor such as a named article or a landmark),
    verified by resolving it back to the same element. `snapshot` (the outline the ref came
    from) is optional: it saves a read of the element's own role and name."""
    value = (ref or "").strip().strip("[]").strip().removeprefix("ref=")
    target = page.locator(f"aria-ref={value}")
    if await target.count() != 1:
        raise RefNotFound(f"ref {ref} is not on the page (stale after a navigation or a newer snapshot). "
                          "Take a new snapshot.")
    path = await target.evaluate(PATH_JS, timeout=EVAL_TIMEOUT_MS)
    role, name = ref_role_and_name(snapshot or "", value)
    if role is None:
        # Any aria snapshot replaces the page's ref map, and only an AI snapshot keeps the refs of
        # unchanged elements, so this is the one way to read Playwright's role and name without
        # breaking the caller's ref (an element's own aria_snapshot() would).
        role, name = ref_role_and_name(await page.aria_snapshot(mode="ai", timeout=SNAPSHOT_TIMEOUT_MS), value)
        if await target.count() != 1 or await target.evaluate(PATH_JS, timeout=EVAL_TIMEOUT_MS) != path:
            raise RefNotFound(f"ref {ref} changed while its locator was being built. Take a new snapshot.")
    guessed = str(await target.evaluate(ROLE_JS, timeout=EVAL_TIMEOUT_MS) or "")
    ancestors = await target.evaluate(_ANCESTORS_JS, list(ANCHOR_ROLES), timeout=EVAL_TIMEOUT_MS)
    builder = _Builder(page)
    for r in dict.fromkeys(x for x in (role, guessed) if x and x not in NO_NEST_ROLES and x != "text"):
        loc = await builder.build(path, r, name if r == role else None, ancestors)
        if loc is not None:
            return loc
    loc = await builder.css_fallback(target, path)
    if loc is not None:
        return loc
    raise InvalidRequest(f"could not build a durable locator for ref {ref}: it has no role and name that "
                         "lead back to it. Record a step on a nearby control instead.")


# ---------------------------------------------------------------- the recorder hook

_RECORDED = {"click": "click", "type": "type", "fill": "fill", "select": "select", "press": "press",
             "scroll": "scroll"}


def _recorder(runtime: Any) -> Any:
    flows = runtime.get("flows") if runtime is not None else None
    return getattr(flows, "recorder", None)


async def _handle_state(handle: Handle | None) -> dict[str, Any] | None:
    if handle is None or not await quietly(handle.evaluate(_IS_CONNECTED_JS), False):
        return None
    data = await quietly(handle.evaluate(STATE_JS, {"exclude": [], "maxText": 2000, "maxAttrs": MAX_ATTRS}))
    if not data:
        return None
    return {"attributes": data["attrs"], "value": None if data.get("sensitive") else data["value"],
            "text": data["text"][:300], "sensitive": bool(data.get("sensitive"))}


async def record_act(runtime: Any, lane_id: str, page: Page, ref: str, action: str, text: str | None,
                     perform: Callable[[], Awaitable[Any]]) -> Any:
    """Run one browser act; while a recording is active on the lane, also push an action event
    with the target's structural locator (built before acting, while the ref resolves) and its
    own state before and after. Never changes what the act does or returns."""
    recorder = _recorder(runtime)
    kind = str(getattr(action, "value", action))
    if recorder is None or kind not in _RECORDED or not recorder.active(lane_id):
        return await perform()
    locator: Locator | None = None
    handle = None
    before = None
    try:
        locator = await structural_locator_for(page, ref)
        handle = await page.locator(f"aria-ref={ref}").element_handle(timeout=EVAL_TIMEOUT_MS)
        before = await _handle_state(handle)
    except Exception as e:  # noqa: BLE001 - the act still runs; the recorder drops the step
        log.info("recorder: no structural locator for %s: %s", ref, first_line(e, 300))
    result = await perform()
    after = await _handle_state(handle)
    event: dict[str, Any] = {
        "action": _RECORDED[kind], "url": getattr(result, "url", page.url), "ok": bool(getattr(result, "ok", True)),
        "locator": locator.model_dump(exclude_none=True, exclude_defaults=True) if locator else None,
        "sensitive": bool((before or {}).get("sensitive")),
        "state_before": before, "state_after": after,
    }
    if kind in ("type", "fill"):
        event["text"] = text or ""
    elif kind == "select":
        event["option"] = text or ""
    elif kind == "press":
        event["key"] = text or ""
    try:
        recorder.observe(lane_id, event)
    except Exception:
        log.exception("recorder: could not store the event")
    if handle is not None:
        await quietly(handle.dispose())
    return result


def record_navigate(runtime: Any, lane_id: str, url: str) -> None:
    """Push a navigate event while a recording is active on the lane."""
    recorder = _recorder(runtime)
    if recorder is not None and recorder.active(lane_id):
        recorder.observe(lane_id, {"action": "navigate", "url": url, "ok": True})
