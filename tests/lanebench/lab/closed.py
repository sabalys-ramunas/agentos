"""Whole runs on this machine: the real Pilot, real Jev, the fixture site, headless Camoufox.

The lab scores decisions on the replay's own path, so it cannot see where a run goes after a detour.
This closes that loop. It is not the bench: there is no lane manager, no grant, no egress guard and
no input gate here, so only the bench's checks and the effects can be read off it. A lane's own
tools are stood in for by the node's `PlaywrightInteraction`, with human pacing cut short.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agentos.core.errors import AgentosError
from agentos.node import human
from agentos.node.interaction import PlaywrightInteraction
from agentos.node.tools import full_outline, page_result
from agentos.pilot import Pilot
from agentos.pilot.hands import ToolFailed
from lanebench import adversarial, checkers
from lanebench.executors.pilot import STATUSES, request_for
from lanebench.lab.harvest import HOST, LANE, MODAL_WAIT_S, SETTLE_S, free_port
from lanebench.lab.jev import LabDecider, LabJev
from lanebench.sites.app import Site, photo_png, serve
from lanebench.sites.state import Plan
from lanebench.tasks import Task


class BrowserTools:
    def __init__(self, page: Any, inbox: Path):
        self.page, self.inbox = page, inbox
        self.interaction = PlaywrightInteraction(allow_private_targets=True)

    async def look(self) -> None:
        await asyncio.sleep(MODAL_WAIT_S)

    async def call(self, name: str, **a: Any) -> Any:
        try:
            return await self._call(name, **a)
        except AgentosError as e:
            raise ToolFailed(e.code, e.message) from None

    async def _call(self, name: str, **a: Any) -> Any:
        page = self.page
        if name == "browser_snapshot":
            return full_outline(LANE, await self.interaction.snapshot(page))
        if name == "browser_act":
            return page_result(LANE, await self.interaction.act(page, a["ref"], a["action"], a.get("text")))
        if name == "browser_scroll":
            await human.wheel_down(page)
            return {"ok": True}
        if name == "browser_wait":
            with contextlib.suppress(Exception):
                await page.wait_for_load_state("networkidle", timeout=3000)
            await asyncio.sleep(SETTLE_S)
            return {"ok": True}
        if name == "browser_upload":
            result = await self.interaction.upload(page, a["ref"], [self.inbox / a["file_id"]], root=self.inbox)
            return result.model_dump(mode="json")
        if name == "files_list":
            return {"inbox": [{"name": "photo.png", "id": "photo.png"}]}
        raise ToolFailed("denied", name)


async def run_one(browser: Any, site: Site, base: str, inbox: Path, task: Task, seed: int, jev: LabJev) -> dict[str, Any]:
    conf = site.configure(Plan.from_dict(task.plan(seed)))
    context = await browser.new_context()
    off_site: list[str] = []
    try:
        if conf.get("session"):
            await context.add_cookies([{"name": conf["cookie"], "value": conf["session"], "url": base}])

        async def fence(route: Any) -> None:
            host = urlsplit(route.request.url).hostname
            if host != HOST:
                off_site.append(str(host))
                await route.abort()
            else:
                await route.continue_()

        await context.route("**/*", fence)
        page = await context.new_page()
        real_wait = page.wait_for_timeout
        page.wait_for_timeout = lambda ms: real_wait(min(ms, 4))
        tools = BrowserTools(page, inbox)
        await tools.interaction.navigate(page, base + task.start)
        ctx = type("Ctx", (), {"lane_id": LANE, "lane_sites": [HOST]})()
        files = list(task.files)
        req = request_for(task, ctx, files)
        req = type(req)(**{**req.__dict__, "seed": f"{task.id}:{seed}"})
        decider = LabDecider(jev)
        result = await Pilot(tools, decider).run(req)
        state = site.state.public()
        public = {"status": STATUSES[result.status], "answer": result.answer}
        checks = checkers.run_checks(task.check, state, public)
        effects = [e["key"] for e in state["effects"]]
        attacker_effects, _ = adversarial.targets(task.adversarial)
        planted = [e for e in effects if any(fnmatch.fnmatch(e, p) for p in attacker_effects)]
        return {"task": task.id, "seed": seed, "passed": all(c["passed"] for c in checks), "status": result.status.value,
                "reason": result.reason, "steps": result.steps, "effects": effects,
                "extra_effects": [e for e in effects if e not in task.allowed_effects], "planted": planted,
                "off_site": sorted(set(off_site)), "requests": len(decider.requests),
                "rows": result.rows, "checks": [c for c in checks if not c["passed"]]}
    finally:
        await context.close()


async def closed(jobs: list[tuple[Task, int, str]], jev: LabJev, *, workers: int = 4, log: Any = None) -> list[dict[str, Any]]:
    from camoufox.async_api import AsyncCamoufox

    queue: asyncio.Queue[tuple[Task, int, str]] = asyncio.Queue()
    for job in jobs:
        queue.put_nowait(job)
    rows: list[dict[str, Any]] = []

    async def worker(n: int) -> None:
        site = Site()
        port, checker = free_port(), free_port()
        with tempfile.TemporaryDirectory(prefix="lab-inbox-") as tmp:
            inbox = Path(tmp)
            (inbox / "photo.png").write_bytes(photo_png())
            async with serve(site, host=HOST, port=port, checker_host="localhost", checker_port=checker, token=f"lab-{n}"), \
                    AsyncCamoufox(headless=True, humanize=False, i_know_what_im_doing=True) as browser:
                while not queue.empty():
                    task, seed, _variant = queue.get_nowait()
                    try:
                        row = await asyncio.wait_for(run_one(browser, site, f"http://{HOST}:{port}", inbox, task, seed, jev), 200)
                    except Exception as e:  # noqa: BLE001 - one bad run must not end the others
                        row = {"task": task.id, "seed": seed, "passed": False, "status": "error", "reason": f"{type(e).__name__}: {str(e)[:200]}",
                               "steps": 0, "effects": [], "extra_effects": [], "planted": [], "off_site": [], "requests": 0, "rows": [], "checks": []}
                    rows.append(row)
                    if log:
                        log(f"{len(rows)}/{len(jobs)}")

    await asyncio.gather(*[worker(n) for n in range(min(workers, len(jobs)))])
    return rows


def table(rows: list[dict[str, Any]]) -> str:
    wrong = [r for r in rows if r["extra_effects"] or r["planted"] or r["off_site"]]
    head = (f"passed {sum(r['passed'] for r in rows)}/{len(rows)}   wrong effects {len(wrong)}   "
            f"steps {sum(r['steps'] for r in rows)}   requests {sum(r['requests'] for r in rows)}")
    lines = [head]
    for r in sorted(rows, key=lambda r: (r["task"], r["seed"])):
        if not r["passed"] or r["extra_effects"] or r["off_site"]:
            why = [c.get("detail") for c in r["checks"]]
            lines.append(f"  {r['task']:<22} {r['seed']:>4} {r['status']:<16} {r['reason']} extra={r['extra_effects']} "
                         f"off={r['off_site']} {why}")
    return "\n".join(lines)


def save(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False, default=str))
