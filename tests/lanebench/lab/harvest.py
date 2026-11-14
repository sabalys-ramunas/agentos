"""Walk each task's hand-written replay on the fixture site and keep every decision point.

A walk is one task on one seed. It records the outline before each step, what the step did (the
node's own act result), and the correct answer the step implies, so scoring needs no browser. The
browser is headless Camoufox and the snapshot is the node's own (`PlaywrightInteraction.snapshot`
under `tools.full_outline`), so the text is what `browser_snapshot` gives the pilot. Human pacing is
cut short: no gate reads this input, and the outline does not depend on how slowly it was typed.

A point's `gold` holds every right answer. Steps that commute (a form's fields, a file and a field)
are alternatives of one another. `lb` maps a ref to the element's grader handle (`data-lb`), which
is for grading only and never reaches a question.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import socket
import tempfile
from pathlib import Path
from typing import Any

from agentos.core.models import ActionKind
from agentos.node import human
from agentos.node.flow_driver import PATH_JS, PlaywrightFlowDriver
from agentos.node.interaction import PlaywrightInteraction
from agentos.node.tools import full_outline, page_result
from agentos.runner.driver import ElementNotFound
from lanebench.executors.jev import planner_values
from lanebench.executors.replay import ReplayExecutor, locator
from lanebench.sites.app import Site, photo_png, serve
from lanebench.sites.state import Plan
from lanebench.sites.strings import STRINGS
from lanebench.tasks import Task

LANE = "lab/walk"
DATA = Path(__file__).resolve().parent / "data"
HOST = "127.0.0.1"
BASE = "http://127.0.0.1:8600"  # every walk reads as served from here, so a page is the same text on any worker
SETTLE_S = 0.25
MODAL_WAIT_S = 1.7            # add_modal shows its dialog at most 1.5 s after the page loads
QUIET_STEPS = ("wait_for", "wait_gone", "expect")
ENTRY_STEPS = ("type", "fill", "upload")
OPTIONAL_CLICKS = {"Comment"}  # a click no next step needs: the comment box is on the page before it is pressed
FACTS_JS = "e => [(" + PATH_JS + ")(e), e.getAttribute('data-lb'), e.getAttribute('type')]"


def free_port() -> int:
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def twin(task: Task) -> Task:
    """The task's Lithuanian twin: the same steps on the Lithuanian site. It exists only here."""
    if task.locale == "lt":
        return task
    english = {v: k for k, v in STRINGS["en"].items()}

    def localised(spec: Any) -> Any:
        if isinstance(spec, list):
            return [localised(v) for v in spec]
        if not isinstance(spec, dict):
            return spec
        out = {k: localised(v) for k, v in spec.items()}
        key = english.get(out.get("name"))
        if key is not None and out.get("match", "exact") == "exact" and "names" not in out:
            out["names"] = {"en": out.pop("name"), "lt": STRINGS["lt"][key]}
        return out

    return task.model_copy(update={"mutation": [*task.mutation, {"kind": "locale", "lang": "lt"}],
                                   "replay": [localised(s) for s in task.replay]})


class Walker:
    """One headless browser and one fixture site, walking tasks one at a time."""

    def __init__(self, browser: Any, site: Site, base: str, inbox: Path):
        self.browser, self.site, self.base, self.inbox = browser, site, base, inbox
        self.interaction = PlaywrightInteraction(allow_private_targets=True)

    async def snapshot(self, page: Any) -> tuple[str, dict[str, str], dict[str, str], list[str]]:
        """The outline as browser_snapshot gives it, each ref's DOM path and grader handle, and
        which refs are password inputs."""
        snap = await self.interaction.snapshot(page)
        text = full_outline(LANE, snap)
        paths: dict[str, str] = {}
        lb: dict[str, str] = {}
        secret: list[str] = []
        for ref in sorted(set(re.findall(r"\[ref=(\w+)\]", text))):
            with contextlib.suppress(Exception):
                path, handle, kind = await page.locator(f"aria-ref={ref}").evaluate(FACTS_JS, timeout=1500)
                paths[path] = ref
                if handle:
                    lb[ref] = handle
                if kind == "password":
                    secret.append(ref)
        return text, paths, lb, secret

    async def resolve(self, d: Any, spec: dict[str, Any], lang: str, paths: dict[str, str], timeout_ms: int = 4000) -> str | None:
        with contextlib.suppress(ElementNotFound):
            el = await d.find(locator(spec["locator"], lang), timeout_ms=timeout_ms)
            return paths.get(el.path)
        return None

    @staticmethod
    async def settle(page: Any) -> None:
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=3000)
        await asyncio.sleep(SETTLE_S)

    async def walk(self, task: Task, seed: int, variant: str = "base") -> dict[str, Any]:
        lang = task.locale
        conf = self.site.configure(Plan.from_dict(task.plan(seed)))
        context = await self.browser.new_context()
        try:
            if conf.get("session"):
                await context.add_cookies([{"name": conf["cookie"], "value": conf["session"], "url": self.base}])
            page = await context.new_page()
            real_wait = page.wait_for_timeout
            page.wait_for_timeout = lambda ms: real_wait(min(ms, 4))  # human pacing, cut short
            await self.interaction.navigate(page, self.base + task.start)
            await self.settle(page)
            if any(m.get("kind") == "add_modal" for m in task.mutation):
                with contextlib.suppress(Exception):  # a busy machine shows it late
                    await page.get_by_role("dialog").wait_for(timeout=6000)
                await asyncio.sleep(SETTLE_S)
            d = PlaywrightFlowDriver(page, lane_id=LANE, files_dir=self.inbox, interaction=self.interaction, dwell_ms=(0, 0))
            return await self._steps(task, seed, variant, lang, page, d)
        finally:
            await context.close()

    async def _steps(self, task: Task, seed: int, variant: str, lang: str, page: Any, d: Any) -> dict[str, Any]:
        values = planner_values(task)
        by_text = {v.text: v for v in values}
        walk: dict[str, Any] = {
            "task": task.id, "seed": seed, "variant": variant, "goal": task.goal, "mode": task.mode, "tags": task.tags,
            "mutations": [m["kind"] for m in task.mutation], "adversarial": task.adversarial, "lang": lang,
            "values": [{"key": v.key, "label": v.label, "text": v.text, "secret": v.secret} for v in values],
            "files": list(task.files),
            "read": next((str(c["key"]) for c in task.check if c.get("kind") == "answer"), None),
            "points": [], "complete": True}
        ended = False

        async def point(gold: list[dict[str, Any]], kind: str, step_i: int | None, shot: tuple[str, dict, dict, list]) -> dict[str, Any]:
            text, _, lb, secret = shot
            row = {"i": len(walk["points"]), "step": step_i, "kind": kind, "outline": text, "lb": lb, "password_refs": secret,
                   "gold": gold, "did": None}
            walk["points"].append(row)
            return row

        def alternative(step: dict[str, Any], ref: str | None) -> dict[str, Any] | None:
            if ref is None:
                return None
            if step["do"] == "upload":
                return {"op": "UPLOAD", "ref": ref}
            if step["do"] in ("type", "fill"):
                return {"op": "TYPE", "ref": ref, "value": by_text[step["text"]].key}
            return {"op": "CLICK", "ref": ref}

        for i, step in enumerate(task.replay):
            do = step["do"]
            if do in QUIET_STEPS:
                await ReplayExecutor().step(d, do, step, lang, None, None)  # type: ignore[arg-type]
            elif do == "dwell":
                await asyncio.sleep(min(int(step.get("ms", 300)), 900) / 1000)
            elif do == "scroll_until":
                for _ in range(int(step.get("max_steps", 12))):
                    if await self.found(d, step, lang):
                        break
                    shot = await self.snapshot(page)
                    if await self.found(d, step, lang):  # a list can load between the two looks
                        break
                    row = await point([{"op": "SCROLL"}], "scroll", i, shot)
                    row["did"] = {"op": "SCROLL"}
                    await human.wheel_down(page)
                    await self.settle(page)
                else:
                    walk["complete"] = False
            elif do == "read":
                shot = await self.snapshot(page)
                want = self.site.state.public()["latest_comment"][next(c for c in task.check if c.get("kind") == "answer")["latest_comment"]]
                await point([{"op": "READ", "text": want}], "read", i, shot)
                ended = True
            elif do == "hold":
                await point([{"op": "HOLD"}], "hold", i, await self.snapshot(page))
                ended = True
            elif do in ("click", "type", "fill", "upload"):
                shot = await self.snapshot(page)
                ref = await self.resolve(d, step, lang, shot[1])
                if ref is None:
                    walk["complete"] = False
                    walk["broken_at"] = i
                    break
                gold = [alternative(step, ref)]
                following = await self.commuting(task, i, lang, d, shot[1], alternative)
                row = await point([*gold, *following], self.kind(step, walk), i, shot)
                row["did"] = await self.act(page, d, step, ref, by_text, lang)
                await self.settle(page)
            else:
                raise ValueError(f"{task.id}: no walk for step {do}")
        if not ended and walk["complete"]:
            await point([{"op": "DONE"}], "done", None, await self.snapshot(page))
        state = self.site.state.public()
        walk["effects"] = [e["key"] for e in state["effects"]]
        return walk

    @staticmethod
    def kind(step: dict[str, Any], walk: dict[str, Any]) -> str:
        last = next((p for p in reversed(walk["points"]) if p["did"]), None)
        after_value = bool(last) and last["did"]["op"] in ("TYPE", "UPLOAD")
        base = {"click": "click", "type": "type", "fill": "type", "upload": "upload"}[step["do"]]
        return f"{base}_after_value" if after_value else base

    async def found(self, d: Any, step: dict[str, Any], lang: str) -> bool:
        with contextlib.suppress(ElementNotFound):
            await d.find(locator(step["locator"], lang), timeout_ms=0)
            return True
        return False

    async def commuting(self, task: Task, i: int, lang: str, d: Any, paths: dict[str, str], alternative: Any) -> list[dict[str, Any]]:
        """The right answers besides step `i`: entries that may come in any order, and what follows
        a click nothing depends on."""
        out: list[dict[str, Any]] = []
        steps = [s for s in task.replay[i + 1:] if s["do"] not in QUIET_STEPS and s["do"] != "dwell"]
        if task.replay[i]["do"] in ENTRY_STEPS:
            for later in steps:
                if later["do"] not in ENTRY_STEPS:
                    break
                alt = alternative(later, await self.resolve(d, later, lang, paths, 0))
                if alt:
                    out.append(alt)
        elif task.replay[i]["locator"].get("name") in OPTIONAL_CLICKS and steps:
            alt = alternative(steps[0], await self.resolve(d, steps[0], lang, paths, 0))
            if alt:
                out.append(alt)
        return out

    async def act(self, page: Any, d: Any, step: dict[str, Any], ref: str, by_text: dict[str, Any], lang: str) -> dict[str, Any]:
        """Do the step through the node's own act, and keep its result for the pilot's state."""
        do = step["do"]
        if do == "upload":
            await d.upload(await d.find(locator(step["locator"], lang), timeout_ms=2000), step["file"])
            return {"op": "UPLOAD", "ref": ref, "result": {"ok": True}}
        if do == "click":
            result = await self.interaction.act(page, ref, ActionKind.CLICK)
            return {"op": "CLICK", "ref": ref, "result": page_result(LANE, result)}
        result = await self.interaction.act(page, ref, ActionKind.FILL, step["text"])
        return {"op": "TYPE", "ref": ref, "value": by_text[step["text"]].key, "result": page_result(LANE, result)}


def walk_path(variant: str, task_id: str, seed: int, root: Path = DATA) -> Path:
    return root / "walks" / variant / f"{task_id}--{seed}.json"


async def harvest(jobs: list[tuple[Task, int, str]], *, workers: int = 4, force: bool = False, root: Path = DATA,
                  log: Any = lambda *a: print(*a, flush=True)) -> list[Path]:
    """Walk every (task, seed, variant) not on disk yet, `workers` browsers at a time."""
    from camoufox.async_api import AsyncCamoufox

    todo = [j for j in jobs if force or not walk_path(j[2], j[0].id, j[1], root).exists()]
    queue: asyncio.Queue[tuple[Task, int, str]] = asyncio.Queue()
    for job in todo:
        queue.put_nowait(job)
    out: list[Path] = []
    done = 0

    async def worker(n: int) -> None:
        nonlocal done
        site = Site()
        port, checker = free_port(), free_port()
        with tempfile.TemporaryDirectory(prefix="lab-inbox-") as tmp:
            inbox = Path(tmp)
            (inbox / "photo.png").write_bytes(photo_png())
            async with serve(site, host=HOST, port=port, checker_host="localhost", checker_port=checker, token=f"lab-{n}"), \
                    AsyncCamoufox(headless=True, humanize=False, i_know_what_im_doing=True) as browser:
                walker = Walker(browser, site, f"http://{HOST}:{port}", inbox)
                while not queue.empty():
                    task, seed, variant = queue.get_nowait()
                    try:
                        walk = await asyncio.wait_for(walker.walk(task, seed, variant), 180)
                    except Exception as e:  # noqa: BLE001 - one bad walk must not end the harvest
                        log(f"FAILED {task.id} seed {seed} {variant}: {type(e).__name__}: {str(e)[:200]}")
                        continue
                    path = walk_path(variant, task.id, seed, root)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(walk, ensure_ascii=False).replace(walker.base, BASE))
                    out.append(path)
                    done += 1
                    log(f"{done}/{len(todo)} {variant} {task.id} seed {seed}: {len(walk['points'])} points"
                        + ("" if walk["complete"] else " INCOMPLETE"))

    await asyncio.gather(*[worker(n) for n in range(min(workers, max(1, len(todo))))])
    return out
