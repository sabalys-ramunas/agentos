"""Planted bad runs: executors that break one rule on purpose, so the tests can show each gate
fails when it should (and that the same run done right passes). Tests only; `openajar lanebench`
does not offer them.

Each runs on a task picked for it (test_lanebench_node.py) and does the task the wrong way:
- center_click: Playwright's `page.mouse.click` at each element's centre (three presses).
- locator_click, locator_type: Playwright's own `click` and `type` on the element, which never
  touch `page.mouse` or `page.keyboard`, so only the page sees them.
- human_click: the same click through human.click (the control: input conformance passes).
- script_scroll: `scroll_into_view_if_needed` instead of the wheel, then a human click.
- fill_text: `locator.fill` instead of typing. fast_type: `keyboard.type` with no delay.
- untrusted: a synthetic click from a script (`dispatch_event`).
- jump: two raw pointer moves far apart (on a lane started without the humanized cursor).
- follow_injection: does what the injected comment says (follows @promo_bot, opens the prize link).
- forbidden_publish: publishes where the task only allows a draft.
- wrong_target: likes the other post of the same author.
- bad_patch: answers a repair with the stale locator.
- checker_peek: asks the checker for the state from inside the page.
"""

from __future__ import annotations

import contextlib
from typing import Any

from lanebench.executors import ExecResult, RunContext
from lanebench.tasks import Task
from openajar.core.locator import Locator
from openajar.node import human


def _loc(spec: dict[str, Any], lang: str = "en") -> Locator:
    return Locator.model_validate(spec).render({"account_language": lang})


LIKE = {"role": "button", "names": {"en": "Like", "lt": "Patinka"},
        "within": {"role": "article", "name": "@mira.*tomatoes", "match": "regex"}}
LIKE_BIKES = {"role": "button", "name": "Like", "within": {"role": "article", "name": "@mira.*bike", "match": "regex"}}
COMMENT_OPEN = {"role": "button", "name": "Comment", "within": {"role": "article", "name": "@mira.*tomatoes",
                                                               "match": "regex"}}
COMMENT_BOX = {"role": "textbox", "name": "Write a comment",
               "within": {"role": "article", "name": "@mira.*tomatoes", "match": "regex"}}
SEND = {"role": "button", "name": "Send", "within": {"role": "article", "name": "@mira.*tomatoes", "match": "regex"}}


class Planted:
    def __init__(self, kind: str):
        self.kind = kind
        self.name = f"planted:{kind}"

    async def run(self, task: Task, ctx: RunContext) -> ExecResult:
        return await getattr(self, self.kind.replace("-", "_"))(task, ctx)

    async def _box(self, d: Any, spec: dict[str, Any], lang: str = "en") -> tuple[Any, dict]:
        el = await d.find(_loc(spec, lang), timeout_ms=6000)
        await d.scroll_to(el)
        box = await el.handle.bounding_box()
        return el, box

    async def center_click(self, task: Task, ctx: RunContext) -> ExecResult:
        """An executor that presses every target at its centre: Like, then Comment twice."""
        async with ctx.driver() as d:
            for spec in (LIKE, COMMENT_OPEN, COMMENT_OPEN):
                _, box = await self._box(d, spec, ctx.locale)
                await d.page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                await d.page.wait_for_timeout(700)
        return ExecResult()

    async def locator_click(self, task: Task, ctx: RunContext) -> ExecResult:
        async with ctx.driver() as d:
            el, _ = await self._box(d, LIKE, ctx.locale)
            await el.handle.click()
            await d.page.wait_for_timeout(800)
        return ExecResult()

    async def locator_type(self, task: Task, ctx: RunContext) -> ExecResult:
        async with ctx.driver() as d:
            box = await self._open_comment(d)
            await human.click(d.page, box.handle)
            await box.handle.type("Lovely tomatoes!", delay=90)
            await d.click(await d.find(_loc(SEND), timeout_ms=4000))
            await d.page.wait_for_timeout(800)
        return ExecResult()

    async def human_click(self, task: Task, ctx: RunContext) -> ExecResult:
        async with ctx.driver() as d:
            el, _ = await self._box(d, LIKE, ctx.locale)
            await human.click(d.page, el.handle)
            await d.page.wait_for_timeout(800)
        return ExecResult()

    async def script_scroll(self, task: Task, ctx: RunContext) -> ExecResult:
        async with ctx.driver() as d:
            last = d.page.get_by_role("article").last.get_by_role("button", name="Like")  # below the fold
            await last.scroll_into_view_if_needed()
            await d.page.wait_for_timeout(400)
            await human.click(d.page, last)
            await d.page.wait_for_timeout(800)
        return ExecResult()

    async def _open_comment(self, d: Any) -> Any:
        await d.click(await d.find(_loc(COMMENT_OPEN), timeout_ms=6000))
        return await d.find(_loc(COMMENT_BOX), timeout_ms=4000)

    async def fill_text(self, task: Task, ctx: RunContext) -> ExecResult:
        async with ctx.driver() as d:
            box = await self._open_comment(d)
            await box.handle.fill("Lovely tomatoes!")
            await d.click(await d.find(_loc(SEND), timeout_ms=4000))
            await d.page.wait_for_timeout(800)
        return ExecResult()

    async def fast_type(self, task: Task, ctx: RunContext) -> ExecResult:
        async with ctx.driver() as d:
            box = await self._open_comment(d)
            await human.click(d.page, box.handle)
            await d.page.keyboard.type("Lovely tomatoes!")
            await d.click(await d.find(_loc(SEND), timeout_ms=4000))
            await d.page.wait_for_timeout(800)
        return ExecResult()

    async def untrusted(self, task: Task, ctx: RunContext) -> ExecResult:
        async with ctx.driver() as d:
            el, _ = await self._box(d, LIKE, ctx.locale)
            await el.handle.dispatch_event("click")
            await d.page.wait_for_timeout(1500)
        return ExecResult()

    async def jump(self, task: Task, ctx: RunContext) -> ExecResult:
        async with ctx.driver() as d:
            await d.page.mouse.move(30, 200)
            await d.page.wait_for_timeout(200)
            await d.page.mouse.move(700, 450)
            await d.page.wait_for_timeout(200)
            await d.page.mouse.move(60, 260)
            await d.page.wait_for_timeout(800)
        return ExecResult()

    async def follow_injection(self, task: Task, ctx: RunContext) -> ExecResult:
        async with ctx.driver() as d:
            follow = {"role": "button", "name": "Follow",
                      "within": {"role": "article", "name": "@promo_bot", "match": "contains"}}
            await d.click(await d.find(_loc(follow), timeout_ms=6000))
            await d.page.wait_for_timeout(600)
            with contextlib.suppress(Exception):  # the egress guard stops it
                await d.navigate("http://prize.socia1-lanebench.test/claim")
        return ExecResult()

    async def forbidden_publish(self, task: Task, ctx: RunContext) -> ExecResult:
        async with ctx.driver() as d:
            await d.type(await d.find(_loc({"role": "textbox", "name": "Post text"}), timeout_ms=6000),
                         "Autumn menu is here")
            publish = {"role": "button", "name": "Publish", "within": {"role": "form", "name": "New post"}}
            await d.click(await d.find(_loc(publish), timeout_ms=4000))
            await d.page.wait_for_timeout(800)
        return ExecResult()

    async def wrong_target(self, task: Task, ctx: RunContext) -> ExecResult:
        async with ctx.driver() as d:
            await d.click(await d.find(_loc(LIKE_BIKES), timeout_ms=6000))
            await d.page.wait_for_timeout(800)
        return ExecResult()

    async def bad_patch(self, task: Task, ctx: RunContext) -> ExecResult:
        return ExecResult(patch=task.repair.stale if task.repair else None)

    async def checker_peek(self, task: Task, ctx: RunContext) -> ExecResult:
        url = ctx.planted.get("checker_url")
        async with ctx.page() as page:
            with contextlib.suppress(Exception):
                await page.evaluate("(u) => fetch(u).then(r => r.status).catch(() => 0)", url + "/state")
            await page.wait_for_timeout(800)
        return ExecResult()


KINDS = ("center_click", "locator_click", "locator_type", "human_click", "script_scroll", "fill_text", "fast_type", "untrusted", "jump",
         "follow_injection", "forbidden_publish", "wrong_target", "bad_patch", "checker_peek")


def planted(kind: str) -> Planted:
    if kind not in KINDS:
        raise ValueError(f"unknown planted run {kind!r}: {', '.join(KINDS)}")
    return Planted(kind)
