"""Browsing: content that takes a few seconds to appear (wait for it with browser_wait, never poll with
snapshots) and a delete behind a confirm() dialog (answer it with browser_dialog). On the shop the agent
starts on its machine; the checker reads what the shop itself recorded (merged from the evals/ suite of
2026-11-20, `browse-wait-dialog`). The agent leaves the machine open; the harness removes it."""

from __future__ import annotations

import json
import re

from env_eval.outline import act, goto, outline_of, ref
from env_eval.tasks import Agent, Check, Ctx, Session, Task, done
from env_eval.tasks.t10_checkout_form import SHOP

SEED = 5
URL = "http://localhost:8800"
DRAFT = SHOP.draft_to_delete(SEED)
REPORT = SHOP.report_code(SEED)


async def check(ctx: Ctx) -> list[Check]:
    code, out = ctx.on_machine(f"curl -s --max-time 10 {URL}/_state")
    try:
        state = json.loads(out) if code == 0 else None
    except ValueError:
        state = None
    state = state or {}
    return [
        ("the shop runs on the machine left open", bool(state)),
        ("the report was built on the page", state.get("report_requests", 0) >= 1),
        ("the answer has the report's reference", REPORT in ctx.answer()),
        ("the right draft was deleted, and only that one", state.get("deleted") == [DRAFT]),
    ]


async def _shop(ctx: Ctx, a: Agent) -> str:
    env = await a.open(title="Shop report", repo=ctx.repo.slug, keep="none")
    job = await a.on_call("terminal_job_start", cmd=f"python3 serve.py --seed {SEED}", cwd=env["workdir"])
    await a.on_call("terminal_job_read", job=job["id"], until_port=8800, wait_s=60)
    outline = await goto(a, URL + "/report")
    if "only necessary" in outline.lower():
        outline = await act(a, outline, "click", "Only necessary", role="button")
    return outline


def _delete_ref(outline: str) -> str:
    """The Delete button on the draft's own row (the first one after its name)."""
    lines = outline.splitlines()
    row = next(i for i, line in enumerate(lines) if DRAFT in line)
    return ref("\n".join(lines[row:]), "Delete", role="button")


async def perfect(ctx: Ctx, a: Agent, session: int) -> str:
    outline = await _shop(ctx, a)
    await act(a, outline, "click", "Build the report", role="button", snapshot="none")
    waited = await a.on_call("browser_wait", for_text="Report ready", wait_s=30, snapshot="full")
    found = re.search(r"Reference: (RPT-\d+)", outline_of(waited))
    outline = await goto(a, URL + "/drafts")
    await a.on_call("browser_act", ref=_delete_ref(outline), action="click", snapshot="none")
    await a.on_call("browser_dialog", accept=True)
    await a.on_call("browser_wait", gone_text=DRAFT, wait_s=15)
    return done(f"The monthly report's reference is {found.group(1) if found else '?'}; the draft \"{DRAFT}\" is "
                "deleted. The machine is open.")


async def careless(ctx: Ctx, a: Agent, session: int) -> str:
    """Reads the page right after the click (the report is still building) and leaves the dialog unanswered."""
    outline = await _shop(ctx, a)
    after = await act(a, outline, "click", "Build the report", role="button", snapshot="full")
    found = re.search(r"Reference: (RPT-\d+)", after)
    outline = await goto(a, URL + "/drafts")
    await a.raw("browser_act", {"on": a.on, "ref": _delete_ref(outline), "action": "click", "snapshot": "none"})
    return done(f"The report's reference is {found.group(1) if found else 'not shown yet'}; I clicked Delete on "
                f"\"{DRAFT}\".")


TASK = Task(
    id="t14_wait_dialog",
    title="wait for content that takes a few seconds, and answer a confirm dialog",
    repo="shopsite",
    sessions=[Session(f"In openajar, start the shop in {{repo}} (`python3 serve.py --seed {SEED}`, it listens on port "
                      "8800) and use its browser for two things. First, build the monthly report on its Reports page "
                      f"and tell me the report's reference. Second, delete the draft called \"{DRAFT}\" on its Drafts "
                      "page: it will ask you to confirm, and yes, I am sure. Leave the machine open: I want to look "
                      "at it.")],
    check=check, perfect=perfect, careless=careless,
)
