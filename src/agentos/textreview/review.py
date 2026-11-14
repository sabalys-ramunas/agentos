"""One review run: evidence, then at most three answers from a tool-less brain, then a bundle.

1. Evidence from the action log over the window, and any lanebench reports in the inbox.
2. Too little of it (settings `min_calls`): the run is recorded as skipped, and no model is asked.
3. The prompt holds the evidence and the current text. A reply too long to read back is asked for
   again with fewer changes; a proposal the safety lock refuses is asked for again once, with the
   problems (harness2's validate and retry). After that the run is rejected.
4. The bundle is written, and the owner is told.

It never changes the running node's text, opens a pull request or applies anything. Before each
answer it checks the plugin is still on, so a person's `disable` stops a run before it spends more.
"""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentos.core import clock
from agentos.core.errors import AgentosError
from agentos.textreview import evidence as ev
from agentos.textreview import prompt as prompts
from agentos.textreview import validate
from agentos.textreview.brain import CallFailed, Reply
from agentos.textreview.bundle import Runs, changes_md
from agentos.textreview.current import CurrentText
from agentos.textreview.settings import Settings, inbox_dir

log = logging.getLogger("agentos.textreview")

DAY = 86400.0
MIN_SPEND = 0.005  # below this much budget left, another answer cannot be asked for

Call = Callable[..., Awaitable[Reply]]  # (prompt, *, max_usd) -> Reply, raising CallFailed


@dataclass
class Inputs:
    home: Path
    settings: Settings
    traces: Any                                   # the action log: `query(...)`, or None when there is none
    current: CurrentText
    call: Call | None                             # None: no brain could be had, and `no_brain` says why
    brain: str | None
    clock: Callable[[], float]
    still_on: Callable[[], None] = lambda: None   # raises when a person turned the plugin off
    label: Callable[[str, str], tuple[str, str]] | None = None
    notify: Callable[..., Any] | None = None      # (title, body, data) -> None
    trigger: str = "schedule"
    no_brain: str = ""
    codes: Any = None                             # evidence.Codes; the catalog when left out


def gather(inp: Inputs, since: float, until: float) -> tuple[ev.Evidence, ev.Codes]:
    from agentos.core import catalog

    codes = inp.codes or ev.Codes(catalog.entries())
    s = inp.settings
    rows = ev.read_rows(inp.traces, since, until) if inp.traces is not None else []
    sessions, left_out, lanes = ev.sessions_of(rows, enums=inp.current.enums, codes=codes, label=inp.label,
                                               skip_subjects=s.skip_subjects, skip_workspaces=s.skip_workspaces)
    probes, words, used = ev.read_reports(inbox_dir(inp.home, s), enums=inp.current.enums, codes=codes)
    return ev.Evidence(since=since, until=until, sessions=sessions, probes=probes, left_out=left_out, lanes=lanes,
                       probe_words=words, reports=used), codes


async def run(inp: Inputs) -> dict[str, Any]:
    """One run, start to bundle. Returns its meta. Only a broken disk raises."""
    runs = Runs(inp.home)
    with runs.lock():
        return await _run(inp, runs)


async def _run(inp: Inputs, runs: Runs) -> dict[str, Any]:
    s = inp.settings
    started = inp.clock()
    run_id = runs.new_id(started)
    since = started - s.window_days * DAY
    found, codes = gather(inp, since, started)
    evidence_md = ev.render(found, codes, max_sessions=s.max_sessions_shown, max_calls=s.max_calls_shown)
    meta: dict[str, Any] = {
        "id": run_id, "trigger": inp.trigger, "started_at": started, "window": {"from": clock.iso(since, timespec="seconds"), "to": clock.iso(started, timespec="seconds")},
        "counts": {"calls": found.calls, "sessions": len(found.sessions), "left_out": found.left_out,
                   "probe_tasks": len(found.probes), "probe_failed": sum(not p.passed for p in found.probes),
                   "reports": [p.name for p in found.reports]},
        "brain": inp.brain, "cost_usd": 0.0, "notional_usd": 0.0, "attempts": [], "changes": 0, "reason": None,
    }
    proposal: dict[str, Any] | None = None
    problems: list[str] = []
    last_reply = ""
    if found.calls < s.min_calls:
        meta.update(status="skipped", reason=f"{found.calls} calls in the window, and a review needs at least "
                                             f"{s.min_calls} (settings min_calls)")
    elif inp.call is None:
        meta.update(status="failed", reason=inp.no_brain or "no brain is assigned to the review job")
    else:
        proposal, problems, last_reply = await _ask(inp, meta, evidence_md, found)
    files = {"evidence.md": evidence_md}
    if meta["status"] in ("ready", "rejected"):
        files["validation.json"] = json.dumps({"ok": meta["status"] == "ready", "problems": problems,
                                               "attempts": meta["attempts"]}, indent=2) + "\n"
        files["proposal.json"] = json.dumps(proposal if proposal is not None else {"unreadable_reply": last_reply},
                                            indent=2) + "\n"
    if meta["status"] == "ready" and proposal is not None:
        meta["changes"] = len(proposal.get("changes") or [])
        files.update({f"variant/{name}": text for name, text in inp.current.merged(proposal).files().items()})
    meta["ended_at"] = inp.clock()
    folder = runs.root / run_id
    files["CHANGES.md"] = changes_md(meta, proposal, problems if meta["status"] == "rejected" else [], folder)
    files["meta.json"] = json.dumps(meta, indent=2) + "\n"
    runs.write(run_id, files)
    if meta["status"] in ("ready", "rejected"):
        runs.keep_reports(run_id, found.reports)
    await _tell(inp, meta, folder)
    log.info("text review %s: %s", run_id, meta["status"])
    return meta


async def _ask(inp: Inputs, meta: dict[str, Any], evidence_md: str, found: ev.Evidence,
               ) -> tuple[dict[str, Any] | None, list[str], str]:
    """Ask, read, check: at most one retry for length and one for the lock. Sets meta's status."""
    s = inp.settings
    assert inp.call is not None
    max_changes, shorter, problems, last_reply = s.max_changes, False, [], ""
    retried_length = retried_lock = False
    proposal: dict[str, Any] | None = None
    forbidden = found.lanes | found.probe_words
    while True:
        try:
            inp.still_on()
        except AgentosError as e:
            meta.update(status="failed", stopped=True, reason=f"the plugin was turned off during the run ({e.code})")
            return proposal, problems, last_reply
        left = s.max_usd - meta["cost_usd"]
        if left < MIN_SPEND:
            meta.update(status="failed", reason=f"the run's cost cap of ${s.max_usd:.2f} is spent (settings max_usd)")
            return proposal, problems, last_reply
        text = prompts.build(evidence_md, inp.current, max_changes=max_changes, problems=problems or None,
                             last_reply=last_reply or None, shorter=shorter)
        try:
            reply = await inp.call(text, max_usd=left)
        except CallFailed as e:
            meta["cost_usd"] = round(meta["cost_usd"] + e.usd, 6)
            meta["attempts"].append({"outcome": "cut_off" if e.cut_off else "failed", "reason": e.message,
                                     "cost_usd": e.usd, "max_changes": max_changes})
            if e.cut_off and not retried_length and max_changes > 1:
                retried_length, shorter, max_changes = True, True, max(1, max_changes // 2)
                continue
            meta.update(status="failed", reason=e.message)
            return proposal, problems, last_reply
        meta["cost_usd"] = round(meta["cost_usd"] + reply.usd, 6)
        meta["notional_usd"] = round(meta["notional_usd"] + reply.notional_usd, 6)
        last_reply = reply.text
        proposal, problems = prompts.parse(reply.text)
        if proposal is not None:
            problems = validate.check(proposal, inp.current, forbidden=forbidden)
        meta["attempts"].append({"outcome": "refused" if problems else "accepted", "task": reply.task,
                                 "model": reply.model, "cost_usd": reply.usd, "max_changes": max_changes,
                                 "problems": problems})
        if not problems:
            meta.update(status="ready")
            return proposal, [], last_reply
        if retried_lock:
            meta.update(status="rejected", reason=f"{len(problems)} problems remained after one retry")
            return proposal, problems, last_reply
        retried_lock, shorter = True, False


async def _tell(inp: Inputs, meta: dict[str, Any], folder: Path) -> None:
    """The owner hears of a run that needs them: ready, rejected or failed. A skipped week does not."""
    if inp.notify is None or meta["status"] == "skipped" or meta.get("stopped"):
        return
    body = {
        "ready": f"{meta['changes']} changes to the agent text are proposed, for you to review. Nothing changed on "
                 f"the node. Read them with `agentos text-review show {meta['id']}`.",
        "rejected": f"This week's proposal failed its safety checks, so there is nothing to probe. "
                    f"`agentos text-review show {meta['id']}` says why.",
        "failed": f"The weekly agent text review could not run: {meta.get('reason')}.",
    }[meta["status"]]
    try:
        out = inp.notify(f"Agent text review: {meta['status']}", body, {"key": f"text_review:{meta['id']}"})
        if inspect.isawaitable(out):
            await out
    except Exception:  # the bundle is written; a notice that fails must not undo it
        log.exception("the text review notice for %s was not sent", meta["id"])
