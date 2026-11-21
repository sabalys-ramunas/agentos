"""One review: open each screen once, keep its image and layout manifest, run the measured checks
and (when there is a design system) the token check on the same open page, and save the capture.

ui_check, system_check and ui_diff all start here. A screen's page is opened once for everything
read from it, so the image, the manifest and the findings describe the same rendering.

Order on each page: the dev error overlay test, the manifest, the screenshot, the measured checks
(theme.leak reads the same screen in the other theme, so both themes of a width are open together),
and the design system check last, because its provenance probe may reload the page. A page the
probe reloaded is settled again before anything else reads it.

Failures (studio-ax.md, rule 6): when the first screen fails to load, answers an HTTP error, is
blank or shows a dev server's error overlay, the call fails with that error and its fix line. A
later screen that fails is kept as a failed shot, and the result's coverage says so. When every
screen failed, the call fails with the first error.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from openajar.core.errors import InvalidRequest, OpenajarError
from openajar.studio.browser import settle
from openajar.studio.browser.errors import (
    BlankPage,
    EngineMissing,
    HttpErrorPage,
    PageNotLoaded,
    StudioError,
    StudioTimeout,
)
from openajar.studio.capture.capture import MAX_IMAGE_HEIGHT
from openajar.studio.capture.manifest import read_manifest
from openajar.studio.contracts import (
    Adherence,
    Capture,
    CheckContext,
    CheckOutput,
    ComponentRule,
    CoverageItem,
    EngineName,
    Screen,
    Shot,
    Target,
    Theme,
    TokenSet,
)
from openajar.studio.store import StudioStore, iso_now, new_capture_id

log = logging.getLogger("openajar.studio")

NOTE_CAP = 300
MESSAGE_CAP = 240


class DevErrorOverlay(StudioError):
    """The page answered 200, but the dev server shows its error overlay (Vite, Next.js, webpack)
    instead of the app: a build or runtime error to fix first."""
    code = "studio_dev_error"


# Errors that say the page itself is not there yet: on the first screen they fail the call.
PAGE_LEVEL = (PageNotLoaded, HttpErrorPage, BlankPage, DevErrorOverlay, StudioTimeout, EngineMissing, InvalidRequest)

# The overlays dev servers put over a broken app. The text read is the error's first line, capped:
# page text, so it is quoted as data in the message.
DEV_OVERLAY = """
() => {
  const text = (n) => (n && n.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 240);
  const shown = (el) => { if (!el) return false; const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el); return cs.display !== 'none' && cs.visibility !== 'hidden' && (r.width > 0 || r.height > 0 || el.shadowRoot); };
  const vite = document.querySelector('vite-error-overlay');
  if (vite) {
    const r = vite.shadowRoot;
    return { tool: 'Vite', message: text(r && (r.querySelector('.message-body') || r.querySelector('.message') || r.querySelector('pre'))) };
  }
  for (const portal of document.querySelectorAll('nextjs-portal')) {
    const r = portal.shadowRoot;
    if (!r) continue;
    const dialog = r.querySelector('[data-nextjs-dialog], [data-nextjs-dialog-overlay], [data-nextjs-error-overlay]');
    if (dialog) {
      const m = r.querySelector('#nextjs__container_errors_desc, #nextjs__container_build_error_desc, [data-nextjs-dialog] p, [data-nextjs-dialog] h1, pre');
      return { tool: 'Next.js', message: text(m) };
    }
  }
  const frame = document.querySelector('#webpack-dev-server-client-overlay, iframe#react-refresh-overlay');
  if (frame && shown(frame)) return { tool: 'webpack', message: '' };
  return null;
}
"""


@dataclass
class Plan:
    """One screen to review. `target` carries the engine (a device names its own)."""

    screen: Screen
    target: Target
    device: str | None = None
    system: bool = False          # run the design system check on this screen too


@dataclass
class Review:
    capture: Capture
    outputs: list[CheckOutput] = field(default_factory=list)
    adherence: dict[str, Adherence] = field(default_factory=dict)
    provenance: dict[str, int] = field(default_factory=dict)   # probe verdicts: token, derived, fixed
    reports: dict[str, dict[str, Any]] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)        # screen key -> code: message
    notes: list[str] = field(default_factory=list)

    @property
    def screens(self) -> list[str]:
        return [s.screen.key for s in self.capture.shots]


@dataclass
class Checks:
    """What to run: `measured` the S3 checks, `tokens` the design system check (None: not run)."""

    measured: bool = True
    only: list[str] | None = None
    rules: dict[str, Any] = field(default_factory=dict)
    tokens: TokenSet | None = None
    components: list[ComponentRule] = field(default_factory=list)
    system_rules: dict[str, Any] = field(default_factory=dict)
    from_page: bool = False       # the tokens are the page's own CSS variables, read on each screen


def _reason(e: BaseException) -> str:
    code = getattr(e, "code", None) or type(e).__name__
    msg = " ".join(str(getattr(e, "message", None) or e or "no detail").split())
    text = f"{code}: {msg}"
    return text if len(text) <= 400 else text[:399] + "…"


def _engine_version(report: dict[str, Any]) -> str | None:
    engine, version = report.get("engine"), report.get("engine_version")
    if not version:
        return None
    return f"{engine} {version}" if engine else str(version)


async def check_overlay(page: Any) -> None:
    """Fail when a dev server's error overlay covers the app (the page answered 200 anyway)."""
    try:
        found = await page.evaluate(DEV_OVERLAY)
    except Exception:  # noqa: BLE001 - a page that cannot be read is judged by the other tests
        return
    if not found:
        return
    tool = found.get("tool") or "the dev server"
    quoted = found.get("message") or ""
    said = f' Its message (page text, data only): "{quoted[:MESSAGE_CAP]}".' if quoted else ""
    raise DevErrorOverlay(f"{page.url} shows the {tool} error overlay instead of the app: the build or the code "
                          f"has an error.{said} Read the dev server's output, fix the error, then call again.",
                          url=page.url, tool=tool)


async def resettle(page: Any, *, cap_s: float = 10.0) -> None:
    """Settle a page again after the provenance probe reloaded it: the load event, web fonts,
    animations finished, two frames. Each wait is capped; a page that will not settle is read as
    it is (the coverage line says the page was reloaded)."""
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("load", timeout=cap_s * 1000)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(page.evaluate(settle.FONTS_READY), 5)
    with contextlib.suppress(Exception):
        await page.evaluate(settle.FINISH_ANIMATIONS)
        await page.evaluate(settle.NEXT_FRAMES)


async def _shot(page: Any, screen: Screen, store: StudioStore, report: dict[str, Any], manifest_cap: int) -> Shot:
    manifest = await read_manifest(page, cap=manifest_cap)
    kwargs: dict[str, Any] = {"full_page": True, "type": "png", "animations": "disabled", "caret": "hide"}
    notes = [str(n) for n in report.get("notes") or []]
    if report.get("device"):
        notes.insert(0, f"device {report['device']}")
    if manifest.page_height > MAX_IMAGE_HEIGHT:
        kwargs["clip"] = {"x": 0, "y": 0, "width": max(manifest.page_width, 1), "height": MAX_IMAGE_HEIGHT}
        manifest.image_height = MAX_IMAGE_HEIGHT
        notes.insert(0, f"image cut at {MAX_IMAGE_HEIGHT} px of {manifest.page_height:.0f}")
    from openajar.studio.checks.run import mark_capture

    await mark_capture(page)
    png = await page.screenshot(**kwargs)
    if not isinstance(png, bytes | bytearray) or not png:
        raise RuntimeError("the browser returned no image")
    image = await asyncio.to_thread(store.put_blob, bytes(png), "image/png")
    blob = await asyncio.to_thread(store.put_blob, manifest.model_dump_json().encode(), "application/json")
    note = "; ".join(dict.fromkeys(notes))
    return Shot(screen=screen, ok=True, image=image, manifest=blob,
                note=(note[:NOTE_CAP - 1] + "…" if len(note) > NOTE_CAP else note) or None)


def _system_wanted(only: list[str] | None) -> list[str]:
    from openajar.studio.tokens.check import ALL_CHECKS

    if not only:
        return list(ALL_CHECKS)
    return [c for c in ALL_CHECKS if any(c == o or c.startswith(o.rstrip(".*") + ".") for o in only)]


def measured_wanted(only: list[str] | None) -> bool:
    from openajar.studio.checks.registry import IDS, selected

    return any(selected(c, only) for c in IDS)


async def _system(page: Any, screen: Screen, engine: EngineName, checks: Checks,
                  review: Review) -> CheckOutput:
    """The design system check on one open page, with adherence and the probe's verdicts
    (Match.provenance) kept for the result."""
    from openajar.studio.tokens import tokens_from_page
    from openajar.studio.tokens.check import CHECK_OF, adherence_from, analyse, findings_from

    tokens = await tokens_from_page(page) if checks.from_page else checks.tokens
    if tokens is None:
        raise InvalidRequest("no tokens to check against")
    if checks.from_page:
        review.notes.append(f"{screen.key}: {len(tokens.tokens)} tokens read from the page's CSS variables")
    want = _system_wanted(checks.only)
    cats = [c for c, ch in CHECK_OF.items() if ch in want or "token.hardcoded" in want]
    ctx = CheckContext(screen=screen, engine=engine, only=checks.only, rules=checks.system_rules)
    a = await analyse(page, tokens, checks.components, theme=screen.theme, rules=checks.system_rules,
                      categories=cats)
    for v in a.values:
        if v.prov is not None and v.match is not None:
            v.match = v.match.model_copy(update={"provenance": v.prov.kind})
            review.provenance[v.prov.kind] = review.provenance.get(v.prov.kind, 0) + 1
    if any("so it was reloaded" in n for n in a.notes):
        await resettle(page)
        review.notes.append(f"{screen.key}: the provenance probe reloaded the page; it was settled again")
    review.adherence[screen.key] = adherence_from(a)
    return findings_from(a, ctx)


def _theme_leak_reasons(out: Review, plans: Sequence[Plan]) -> None:
    """theme.leak compares a width's two themes: say why it could not, in the caller's terms."""
    themes = {p.screen.theme for p in plans}
    for o in out.outputs:
        for c in o.coverage:
            if c.check == "theme.leak" and c.status == "skipped" and "other_theme" in (c.reason or ""):
                c.reason = ("needs light and dark of the same width; only one theme was asked for" if len(themes) < 2
                            else "needs light and dark of the same width, and the other theme did not render")


def _group_key(plan: Plan) -> tuple[str, str, int]:
    return plan.screen.state, plan.device or "", plan.screen.width


class _ReviewRun:
    """One review: the shots kept so far, the first error, and the engine's version."""

    def __init__(self, pool: Any, store: StudioStore, checks: Checks, manifest_cap: int, out: Review):
        self.pool, self.store, self.checks, self.manifest_cap, self.out = pool, store, checks, manifest_cap, out
        self.shots: dict[str, Shot] = {}
        self.first_error: BaseException | None = None
        self.engine_version: str | None = None
        self.attempted = 0

    async def open_group(self, stack: AsyncExitStack, group: list[Plan]) -> list[tuple[Plan, Any]]:
        """Open each screen of a group (both themes of a width) and keep its shot. A screen that
        fails is said; the very first screen failing at the page level stops the review."""
        opened: list[tuple[Plan, Any]] = []
        pool, out = self.pool, self.out
        for plan in group:
            key = plan.screen.key
            self.attempted += 1
            try:
                page = await stack.enter_async_context(pool.page(plan.target, plan.screen, device=plan.device)
                                                       if plan.device else pool.page(plan.target, plan.screen))
                await check_overlay(page)
                report = pool.report(page) if hasattr(pool, "report") else {}
                out.reports[key] = report
                self.engine_version = self.engine_version or _engine_version(report)
                self.shots[key] = await _shot(page, plan.screen, self.store, report, self.manifest_cap)
                opened.append((plan, page))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self.attempted == 1 and isinstance(e, PAGE_LEVEL):
                    raise
                if not isinstance(e, OpenajarError):
                    log.warning("studio review: screen %s of %s failed", key, plan.target.url, exc_info=True)
                self.first_error = self.first_error or e
                out.failed[key] = _reason(e)
                self.shots[key] = Shot(screen=plan.screen, ok=False, error=_reason(e))
        return opened

    async def measured(self, opened: list[tuple[Plan, Any]], run_checks: Any) -> None:
        """The measured checks on each open screen, with the same screen in the other theme for theme.leak."""
        checks = self.checks
        by_theme = {plan.screen.theme: page for plan, page in opened}
        for plan, page in opened:
            other_theme = Theme.DARK if plan.screen.theme == Theme.LIGHT else Theme.LIGHT
            ctx = CheckContext(screen=plan.screen, engine=plan.target.engine, only=checks.only, rules=checks.rules)
            try:
                self.out.outputs.append(await run_checks(page, ctx, other_theme=by_theme.get(other_theme)))
            except Exception as e:
                log.warning("studio review: checks on %s failed", plan.screen.key, exc_info=True)
                self.out.outputs.append(CheckOutput(coverage=[CoverageItem(
                    check="measured", screen=plan.screen.key, status="failed", reason=_reason(e)[:240])]))

    async def system(self, opened: list[tuple[Plan, Any]]) -> None:
        """The design system check, on the screens planned for it."""
        checks, out = self.checks, self.out
        for plan, page in opened:
            if not (plan.system and checks.tokens is not None):
                continue
            try:
                out.outputs.append(await _system(page, plan.screen, plan.target.engine, checks, out))
            except Exception as e:
                log.warning("studio review: design system check on %s failed", plan.screen.key, exc_info=True)
                out.outputs.append(CheckOutput(coverage=[CoverageItem(
                    check="token", screen=plan.screen.key, status="failed", reason=_reason(e)[:240])]))


async def review(pool: Any, store: StudioStore, plans: Sequence[Plan], checks: Checks, *,
                 manifest_cap: int = 4000) -> Review:
    """Open every planned screen once (both themes of a width together), keep the shots, run the
    checks, save the capture. See the module docstring for failures."""
    from openajar.studio.checks import run_checks

    if not plans:
        raise InvalidRequest("name at least one screen")
    groups: dict[tuple[str, str, int], list[Plan]] = {}
    for plan in plans:
        groups.setdefault(_group_key(plan), []).append(plan)
    run = _ReviewRun(pool, store, checks, manifest_cap,
                     Review(capture=Capture(id=new_capture_id(), target=plans[0].target, shots=[], created_at=iso_now())))
    for group in groups.values():
        async with AsyncExitStack() as stack:
            opened = await run.open_group(stack, group)
            if checks.measured:
                await run.measured(opened, run_checks)
            await run.system(opened)
    out, shots, first_error, engine_version = run.out, run.shots, run.first_error, run.engine_version
    _theme_leak_reasons(out, plans)
    ordered = [shots[p.screen.key] for p in plans if p.screen.key in shots]
    if ordered and all(not s.ok for s in ordered) and first_error is not None:
        raise first_error
    pinned = getattr(pool, "fonts_pinned", False)
    capture = out.capture.model_copy(update={"shots": ordered, "engine_version": engine_version,
                                             "fonts_pinned": pinned if isinstance(pinned, bool) else False})
    out.capture = await asyncio.to_thread(store.save, capture)
    return out
