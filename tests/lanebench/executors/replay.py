"""The replay executor: each task's hand-written `[[replay]]` steps, run through the node's own
flow driver (agentos.node.flow_driver: structural locators, human input from human.py). It proves
the fixtures and checkers right before any model runs, and is the input conformance reference.

Steps (`do = ...`):
- navigate {url}: a path on the fixture site.
- click {locator, timeout_ms?}; click_if {locator, timeout_ms?}: click when it shows up in time.
- type {locator, text}; fill {locator, text}; press {key, locator?}; upload {locator, file}.
- wait_for {locator, timeout_ms?}; wait_gone {locator, timeout_ms?}: until it is no longer there
  (a banner closing); scroll_until {locator, max_steps?}: wheel down until it exists.
- expect {locator, attr, value, timeout_ms?}: the element's own state reads back as expected.
- read {locator, key}: its text becomes answer[key].
- hold {reason}: stop and hand the lane to a person. patch {locator}: the repair's answer.

Locators are runner Locator tables; `names = {en = "...", lt = "..."}` resolve by the task's
language, as a connector's do.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agentos.core.driver import DriverError, ElementNotFound
from agentos.core.locator import Locator
from agentos.node import human
from lanebench.executors import ExecResult, RunContext
from lanebench.tasks import Task

DEFAULT_TIMEOUT_MS = 6000


class StepFailed(Exception):
    pass


def locator(spec: dict[str, Any], lang: str) -> Locator:
    return Locator.model_validate(spec).render({"account_language": lang})


async def _find(d: Any, step: dict[str, Any], lang: str, default_ms: int = DEFAULT_TIMEOUT_MS) -> Any:
    return await d.find(locator(step["locator"], lang), timeout_ms=int(step.get("timeout_ms", default_ms)))


class ReplayExecutor:
    name = "replay"

    async def run(self, task: Task, ctx: RunContext) -> ExecResult:
        out = ExecResult()
        if not task.replay:
            return ExecResult(status="failed", error="this task has no replay steps")
        lang = ctx.locale
        async with ctx.driver() as d:
            for i, step in enumerate(task.replay):
                do = step.get("do")
                try:
                    stop = await self.step(d, do, step, lang, ctx, out)
                except (DriverError, ElementNotFound, StepFailed, human.TargetCovered, human.TargetNotVisible) as e:
                    msg = getattr(e, "message", None) or str(e)
                    return ExecResult(status="failed", answer=out.answer, patch=out.patch,
                                      error=f"step {i + 1} ({do}): {msg.splitlines()[0] if msg else type(e).__name__}")
                if stop:
                    break
        return out

    async def step(self, d: Any, do: str, step: dict[str, Any], lang: str, ctx: RunContext, out: ExecResult) -> bool:
        if do == "navigate":
            await d.navigate(ctx.url(step["url"]))
        elif do == "click":
            await d.click(await _find(d, step, lang))
        elif do == "click_if":
            try:
                el = await _find(d, step, lang, 2500)
            except ElementNotFound:
                return False
            await d.click(el)
        elif do == "type":
            await d.type(await _find(d, step, lang), step["text"])
        elif do == "fill":
            await d.fill(await _find(d, step, lang), step["text"])
        elif do == "press":
            el = await _find(d, step, lang) if "locator" in step else None
            await d.press(step["key"], el)
        elif do == "upload":
            await d.upload(await _find(d, step, lang), step["file"])
        elif do == "wait_for":
            await _find(d, step, lang)
        elif do == "wait_gone":
            await self.wait_gone(d, step, lang)
        elif do == "scroll_until":
            await self.scroll_until(d, step, lang)
        elif do == "expect":
            await self.expect(d, step, lang)
        elif do == "read":
            state = await d.read_state(locator(step["locator"], lang))
            if not state.found:
                raise StepFailed(f"nothing to read at {step['locator']}")
            out.answer[step["key"]] = " ".join((state.text or "").split())
        elif do == "dwell":
            await d.page.wait_for_timeout(int(step.get("ms", 800)))
        elif do == "hold":
            out.status, out.reason = "held", step.get("reason", "needs a person")
            return True
        elif do == "patch":
            out.patch = step["locator"]
        else:
            raise StepFailed(f"unknown step {do!r}")
        return False

    async def scroll_until(self, d: Any, step: dict[str, Any], lang: str) -> None:
        loc = locator(step["locator"], lang)
        for _ in range(int(step.get("max_steps", 12))):
            try:
                el = await d.find(loc, timeout_ms=0)
            except ElementNotFound:
                await human.wheel_down(d.page)
                continue
            await d.scroll_to(el)
            return
        raise StepFailed(f"{loc.describe()} did not show up after scrolling")

    async def wait_gone(self, d: Any, step: dict[str, Any], lang: str) -> None:
        loc = locator(step["locator"], lang)
        deadline = asyncio.get_running_loop().time() + int(step.get("timeout_ms", DEFAULT_TIMEOUT_MS)) / 1000
        while (await d.read_state(loc)).found:
            if asyncio.get_running_loop().time() > deadline:
                raise StepFailed(f"{loc.describe()} is still there")
            await asyncio.sleep(0.2)

    async def expect(self, d: Any, step: dict[str, Any], lang: str) -> None:
        loc = locator(step["locator"], lang)
        attr, value = step["attr"], str(step["value"])
        deadline = asyncio.get_running_loop().time() + int(step.get("timeout_ms", 4000)) / 1000
        got = None
        while True:
            state = await d.read_state(loc)
            got = state.attributes.get(attr) if state.found else None
            if got == value:
                return
            if asyncio.get_running_loop().time() > deadline:
                raise StepFailed(f"{loc.describe()}: {attr} is {got!r}, expected {value!r}")
            await asyncio.sleep(0.25)
