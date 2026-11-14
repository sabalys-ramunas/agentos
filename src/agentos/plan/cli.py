"""`agentos plan ...`: one view across lanes and engagement programs, and a day-plan draft.

Both commands only read. They work on the node's files directly, so they need no running daemon,
and they run as whoever can read those files (the owner, on the node).

    agentos plan status [--workspace biz-a] [--json]
    agentos plan draft  [--workspace biz-a] [--json]
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated

import typer

from agentos.core.config import home_from_env
from agentos.plan.draft import SECTIONS, DayDraft, draft_day
from agentos.plan.state import LaneRow, PlanState, collect, load

app = typer.Typer(no_args_is_help=True, help="Planning: what every lane and program needs today. Read-only.")

NONE = "–"
HEADERS = ["LANE", "READINESS", "LAST SIGNAL", "PROGRAM PHASE", "PENDING"]
WorkspaceOpt = Annotated[str | None, typer.Option("--workspace", "-w", help="Only this workspace.")]
JsonOpt = Annotated[bool, typer.Option("--json", help="Print JSON instead of text.")]


def _age(at: datetime, now: datetime) -> str:
    seconds = max(int((now - at).total_seconds()), 0)
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 2 * 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def _signal(row: LaneRow, now: datetime) -> str:
    if row.last_signal is None or row.signal_at is None:
        return NONE
    return f"{row.last_signal}, {_age(row.signal_at, now)}" + (" (stale)" if row.signal_stale else "")


def _pending(row: LaneRow) -> str:
    p = row.pending
    parts = [(p.approvals, "approval"), (p.runs, "run"), (len(p.broken), "broken")]
    return ", ".join(f"{n} {word}" if word == "broken" or n == 1 else f"{n} {word}s" for n, word in parts if n) or NONE


def status_rows(state: PlanState) -> list[list[str]]:
    rows = []
    for row in state.lanes:
        phases = "; ".join(p.phase for p in row.programs) or NONE
        rows.append([row.lane, row.readiness.replace("_", " "), _signal(row, state.at), phases, _pending(row)])
    return rows


def render_status(state: PlanState) -> str:
    rows = status_rows(state)
    if not rows:
        return "No lanes or programs on this node yet."
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(HEADERS)]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in [HEADERS, *rows]]
    for workspace, n in sorted(state.unattached_approvals.items()):
        lines.append(f"{workspace}: {n} waiting with no lane (questions)")
    lines += [f"could not read {name}" for name in state.unavailable]
    return "\n".join(lines)


def render_draft(draft: DayDraft) -> str:
    engine = ("Engagement is a dry run on this node." if draft.dry_run
              else "Engagement sessions are on: approved programs run at their planned times, whatever this says.")
    lines = [f"Day plan draft · {draft.day}", f"A draft: nothing here is queued or sent. {engine}"]
    for section, title in SECTIONS.items():
        items = [i for i in draft.items if i.section == section]
        if items:
            lines += ["", title]
            lines += [f"  {i.lane}: {i.text}" if i.lane else f"  {i.text}" for i in items]
    if not draft.items:
        lines += ["", "Nothing needs you today."]
    lines += ["", "Content"]
    if draft.content:
        lines += [f"  {c.surface} ({c.kind}, draft): {c.text}" for c in draft.content]
        lines.append("  Each goes to you for approval on the commit gate before anything is posted.")
    else:
        lines.append("  None drafted: a brain task will write this batch later.")
    lines += [f"could not read {name}" for name in draft.unavailable]
    return "\n".join(lines)


@app.command("status")
def status(workspace: WorkspaceOpt = None, as_json: JsonOpt = False) -> None:
    """A table of every lane: readiness, last signal, program phase, pending items."""
    state = collect(load(home_from_env()), workspace=workspace)
    typer.echo(json.dumps(state.model_dump(mode="json"), indent=2) if as_json else render_status(state))


@app.command("draft")
def draft(workspace: WorkspaceOpt = None, as_json: JsonOpt = False) -> None:
    """A day plan from the current state. A draft: it queues nothing and sends nothing."""
    plan = draft_day(collect(load(home_from_env()), workspace=workspace))
    typer.echo(json.dumps(plan.model_dump(mode="json"), indent=2) if as_json else render_draft(plan))
