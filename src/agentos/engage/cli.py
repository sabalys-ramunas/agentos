"""`agentos engage ...`: engagement programs for the owner, on the node.

Whoever runs this has a shell on the node, so it is the owner in person: it may approve programs
and change caps (up to the hard ceiling). It works on the node's files directly, so it needs no
running daemon. Agents use the MCP tools instead (agentos.engage.tools), which only draft and read.

    agentos engage program draft biz-a/instagram program.yaml
    agentos engage program show [biz-a-ig-coffee | biz-a/instagram]
    agentos engage program approve biz-a-ig-coffee [--version 2]
    agentos engage plan biz-a/instagram --dry-run [--days 7]
    agentos engage cap biz-a/instagram like 20
    agentos engage status [biz-a/instagram]
    agentos engage caps [instagram]
    agentos engage run biz-a/instagram --dry-run [--day 2026-11-05] [--session 1]
    agentos engage run biz-a/instagram --now [--session 1]
    agentos engage holds biz-a/instagram [--all]
    agentos engage hold clear biz-a/instagram [--kind like] [--id 3]
    agentos engage items biz-a/reddit [--status queued]
    agentos engage item withdraw it_0123456789abcdef
    agentos engage drafts biz-a/reddit [--status used]
    agentos engage provider test [--surface biz-a/reddit]
    agentos engage provider token [--stdin]
    agentos engage digest [biz-a/instagram] [--day 2026-11-04] [--json]
    agentos engage profile reset biz-a/linkedin [--field headline]
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from agentos.core import catalog
from agentos.core.config import home_from_env, load_config
from agentos.core.errors import AgentosError
from agentos.core.runtime import Runtime
from agentos.engage import digest_cli, drafts_cli
from agentos.engage.caps import ACTS, Kind
from agentos.engage.models import DayPlan
from agentos.engage.service import EngageService, to_yaml

app = typer.Typer(no_args_is_help=True, help="Engagement programs: posting, likes, comments and follows as a system.")
program_app = typer.Typer(no_args_is_help=True, help="Draft, show and approve programs.")
app.add_typer(program_app, name="program")
hold_app = typer.Typer(no_args_is_help=True, help="Holds (pauses on an account's engagement): clear them "
                                                  "once you have checked the account.")
app.add_typer(hold_app, name="hold")
item_app = typer.Typer(no_args_is_help=True, help="Items: approved content waiting for a session.")
app.add_typer(item_app, name="item")
drafts_cli.register(app)          # agentos engage drafts, agentos engage provider (brief B3)
digest_cli.register(app)          # agentos engage digest (brief B5)
profile_app = typer.Typer(no_args_is_help=True, help="The profile a program keeps: forget what the engine knows.")
app.add_typer(profile_app, name="profile")

OWNER = {"kind": "human", "id": "owner", "owner": "owner"}
JsonOpt = Annotated[bool, typer.Option("--json", help="Print JSON instead of text.")]
KIND_NAMES = {Kind.STORY_VIEW: "story views", Kind.LIKE: "likes", Kind.COMMENT: "comments", Kind.FOLLOW: "follows",
              Kind.JOIN: "joins", Kind.POST: "posts", Kind.PROFILE_EDIT: "profile edits"}


@contextmanager
def _service() -> Iterator[EngageService]:
    home = home_from_env()
    cfg = load_config(home) if (home / "node.toml").exists() else None
    try:
        svc = EngageService(Runtime(config=cfg) if cfg is not None else Runtime(), home=home)
    except AgentosError as exc:
        typer.secho(catalog.cli_error(exc.code, exc.message), err=True, fg=typer.colors.RED)
        raise typer.Exit(1) from None
    try:
        yield svc
    except AgentosError as exc:
        typer.secho(catalog.cli_error(exc.code, exc.message), err=True, fg=typer.colors.RED)
        raise typer.Exit(1) from None
    finally:
        svc.store.close()


def _print_json(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str))


def _counts(actions: dict[Kind, int]) -> str:
    parts = [f"{n} {KIND_NAMES[k] if n != 1 else KIND_NAMES[k].rstrip('s')}" for k, n in actions.items() if n]
    return ", ".join(parts) if parts else "only browsing"


# ---------------------------------------------------------------- programs

@program_app.command("draft")
def program_draft(surface: Annotated[str, typer.Argument(help="The account, e.g. biz-a/instagram.")],
                  file: Annotated[str, typer.Argument(help="The program as YAML or JSON (a file, or - for stdin).")],
                  as_json: JsonOpt = False) -> None:
    """Validate a program and save it as a draft version. Nothing runs until you approve it."""
    text = sys.stdin.read() if file == "-" else Path(file).read_text()
    with _service() as svc:
        out = svc.draft(surface, text, by=OWNER)
    if as_json:
        _print_json(out)
        return
    typer.echo(f"{out['program']} version {out['version']} ({out['hash']}), {out['state']}"
               + ("" if out["created"] else ", unchanged from a saved version"))
    typer.echo(out["summary"])
    for w in out["warnings"]:
        typer.secho(f"note: {w}", fg=typer.colors.YELLOW)
    typer.echo(f"Approve it with: agentos engage program approve {out['program']} --version {out['version']}")


@program_app.command("show")
def program_show(ref: Annotated[str | None, typer.Argument(help="A program name, or an account or page (a "
                                                                "surface). Leave out to list all.")]
                 = None, as_json: JsonOpt = False) -> None:
    """List programs, or show one program's versions and its YAML."""
    with _service() as svc:
        if ref is None or "/" in ref:
            found = svc.store.programs(ref)
            if as_json:
                _print_json([{"program": v.id, "surface": v.definition.surface, "version": v.version,
                              "hash": v.hash, "state": v.state.value} for v in found])
                return
            if not found:
                typer.echo("No programs yet. Draft one with: agentos engage program draft <surface> <file>")
            for v in found:
                active = svc.store.active(v.definition.surface)
                running = f", running version {active.version}" if active and active.id == v.id else ""
                typer.echo(f"{v.definition.surface:28} {v.id:28} version {v.version} {v.state.value}{running}")
            return
        versions = svc.versions(ref)
    if as_json:
        _print_json([{"version": v.version, "hash": v.hash, "state": v.state.value,
                      "created_by": v.created_by, "approved_by": v.approved_by,
                      "starts_on": v.starts_on, "program": v.definition.canonical()} for v in versions])
        return
    for v in versions:
        who = (v.created_by or {}).get("kind", "?")
        typer.echo(f"version {v.version}  {v.hash}  {v.state.value}  drafted by {who}"
                   + (f", approved, day 1 was {v.starts_on}" if v.starts_on else ""))
    typer.echo("")
    typer.echo(to_yaml(versions[-1].definition).rstrip())


@program_app.command("approve")
def program_approve(program: Annotated[str, typer.Argument(help="The program name.")],
                    version: Annotated[int | None, typer.Option("--version", help="Which version (the latest by "
                                                                                    "default).")] = None) -> None:
    """Approve a program version: it becomes the program of the surface (an account or page it runs on). Owner
    only, on the node."""
    with _service() as svc:
        fv = svc.approve(program, version, by=OWNER)
    typer.echo(f"{fv.id} version {fv.version} approved for {fv.definition.surface}; day 1 is {fv.starts_on}. "
               "This node plans it as a dry run: nothing is clicked.")


# ---------------------------------------------------------------- plans, caps, status

def _plan_lines(plan: DayPlan) -> list[str]:
    head = f"{plan.day.strftime('%a %Y-%m-%d')}  "
    if plan.phase == "warm_up":
        head += f"warm-up day {plan.phase_day}" + (f" ({plan.stage.replace('_', ' ')})" if plan.stage else "")
    else:
        head += f"steady day {plan.phase_day}, ramp week {plan.ramp_weeks}"
    lines = [head]
    if plan.quiet:
        lines.append("  quiet day: only replies to comments on own posts")
        return lines
    for s in plan.sessions:
        acts = sum(n for k, n in s.actions.items() if k in ACTS)
        lines.append(f"  {s.start.strftime('%H:%M:%S')}  {s.minutes:4.1f} min  {_counts(s.actions):48} "
                     f"sees {s.seen}" + (f", acts on {acts}" if acts else ""))
    caps = ", ".join(f"{k.value} {n}" for k, n in plan.caps.items() if n)
    lines.append(f"  total: {_counts(plan.totals())}" + (f"   (caps: {caps})" if caps else ""))
    lines.extend(f"  note: {n}" for n in plan.notes)
    return lines


@app.command("plan")
def plan(surface: Annotated[str, typer.Argument(help="The account, e.g. biz-a/instagram.")],
         dry_run: Annotated[bool, typer.Option("--dry-run", help="Draw and print the plan without acting "
                                                                  "(the only mode in this release).")] = False,
         days: Annotated[int, typer.Option("--days", min=1, max=60, help="How many days.")] = 7,
         start: Annotated[str | None, typer.Option("--start", help="First day, YYYY-MM-DD (today by default).")]
         = None, as_json: JsonOpt = False) -> None:
    """Print each day's planned sessions and action counts for a surface's program (a surface is an account or
    page)."""
    if not dry_run:
        typer.secho("error: plans run only as a dry run in this release; pass --dry-run", err=True,
                    fg=typer.colors.RED)
        raise typer.Exit(2)
    first = date.fromisoformat(start) if start else None
    with _service() as svc:
        plans = svc.plan(surface, days=days, start=first)
        pv = svc.program_for(surface)
    if as_json:
        _print_json([p.model_dump(mode="json") for p in plans])
        return
    assert pv is not None
    state = "approved" if pv.starts_on else "draft"
    typer.echo(f"{surface}: {pv.id} version {pv.version} ({state}), times in {plans[0].timezone}. Dry run: "
               "nothing is clicked.")
    for p in plans:
        typer.echo("")
        for line in _plan_lines(p):
            typer.echo(line)


@app.command("cap")
def cap(surface: Annotated[str, typer.Argument(help="The account, e.g. biz-a/instagram.")],
        kind: Annotated[str, typer.Argument(help="like, comment, follow, join, story_view, post or profile_edit.")],
        value: Annotated[int | None, typer.Argument(help="Daily cap, up to the hard limit.")] = None,
        clear: Annotated[bool, typer.Option("--clear", help="Go back to the ramp.")] = False) -> None:
    """Set a daily cap for one kind (owner only, never above the hard limit)."""
    if value is None and not clear:
        typer.secho("error: give a value, or --clear", err=True, fg=typer.colors.RED)
        raise typer.Exit(2)
    with _service() as svc:
        out = svc.set_cap(surface, kind, None if clear else value, by=OWNER)
    typer.echo(f"{surface} {out['kind']}: " + (f"{out['cap']} a day" if out["cap"] is not None else "back to the ramp")
               + f" (hard ceiling {out['hard_ceiling']})")


@app.command("status")
def status(surface: Annotated[str | None, typer.Argument(help="One account, or leave out for all.")] = None,
           as_json: JsonOpt = False) -> None:
    """Where each account's engagement stands today."""
    with _service() as svc:
        out = svc.status(surface=surface)
    if as_json:
        _print_json(out)
        return
    typer.echo(out["note"])
    for s in out["surfaces"]:
        t = s["today"]
        p = s["program"]
        typer.echo("")
        typer.echo(f"{s['surface']}  {p['id']} v{p['version']} {p['state']}  {s['risk']}  {s['phase']}")
        planned = ", ".join(f"{k} {n}" for k, n in t["planned"].items()) or "only browsing"
        typer.echo("  today: quiet day" if t["quiet"] else f"  today: {t['sessions']} sessions, {planned}"
                   + (f"; next in the {t['next_session']}" if t["next_session"] else ""))
        if s.get("timezone_fallback"):
            typer.echo(f"  time zone: UTC, because {s['timezone_fallback']}")
        for n in s["notices"]:
            typer.echo(f"  notice: {n['text']}")


@app.command("caps")
def caps(site: Annotated[str | None, typer.Argument(help="One site, e.g. instagram.")] = None,
         as_json: JsonOpt = False) -> None:
    """The caps tables: per day, the careful start (it grows each clean week, up to the careful ceiling, the
    most allowed), the normal ceiling (also the hard limit: no account goes above it), and the hourly
    limit."""
    with _service() as svc:
        tables = svc.caps_tables([site] if site else None)
    if as_json:
        _print_json(tables)
        return
    for name, rows in tables.items():
        typer.echo(name)
        for r in rows:
            week = r.get("week")
            extra = (f"  a week: {week['careful_start']}, {week['careful_ceiling']}, {week['normal_ceiling']}"
                     if week else "")
            typer.echo(f"  {r['kind']:11} careful {r['careful_start']:>3} to {r['careful_ceiling']:>3}   "
                       f"normal {r['normal_start']:>3} to {r['normal_ceiling']:>3}   hourly {r['hourly']:>3}{extra}")


# ---------------------------------------------------------------- sessions and holds

def _executor(svc: EngageService):
    """An Executor for reading and previews on the node's files: its ports are never called here
    (windows run only inside agentosd, from the engage service's loop)."""
    from agentos.engage.adapters import NoContent, NodeLanes, NoTargets, RunnerOperations, node_registry
    from agentos.engage.executor import Executor

    return Executor(svc.store, registry=node_registry(None, svc.home),
                    operations=RunnerOperations(None), targets=NoTargets(), content=NoContent(), lanes=NodeLanes(None),
                    clock=svc.clock, plan_for=svc.day_plan, tz_for=svc.timezone)


@app.command("run")
def run(surface: Annotated[str, typer.Argument(help="The account, e.g. biz-a/instagram.")],
        dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what the day's sessions would do, acting on "
                                                                 "nothing.")] = False,
        now: Annotated[bool, typer.Option("--now", help="Move the next session (or --session) to now; the node "
                                                        "runs it within a minute while engage_sessions is on.")]
        = False,
        day: Annotated[str | None, typer.Option("--day", help="YYYY-MM-DD (today by default).")] = None,
        session: Annotated[int | None, typer.Option("--session", min=1, help="One session, counting from 1.")] = None,
        as_json: JsonOpt = False) -> None:
    """Preview a day's sessions, or start one now. Owner only, on the node."""
    if dry_run == now:
        typer.secho("error: pass --dry-run or --now", err=True, fg=typer.colors.RED)
        raise typer.Exit(2)
    with _service() as svc:
        pv = svc.store.active(surface)
        if pv is None:
            typer.secho(f"error: {surface} has no approved program, so nothing runs there", err=True,
                        fg=typer.colors.RED)
            raise typer.Exit(1)
        ex = _executor(svc)
        the_day = date.fromisoformat(day) if day else svc.today(pv.definition)
        windows = ex.windows(surface, the_day)
        if session is not None:
            windows = [w for w in windows if w.idx == session - 1]
            if not windows:
                typer.secho(f"error: {surface} has no session {session} on {the_day}", err=True, fg=typer.colors.RED)
                raise typer.Exit(1)
        if dry_run:
            out = [ex.preview(surface, the_day, w.idx) for w in windows]
            if as_json:
                _print_json(out)
                return
            typer.echo(f"{surface} on {the_day}: {len(out)} sessions. Dry run: nothing is clicked.")
            for p in out:
                start = datetime.fromisoformat(p["start"]).astimezone(svc.timezone(pv.definition))
                typer.echo(f"  session {p['idx'] + 1} at {start:%H:%M}  {p['status']}  "
                           + ("would run" if p["would_run"] else "would not run"))
                for o in p["ops"]:
                    problem = {"op_unverified": "no canary verified it yet (agentos connector verify)"}.get(
                        o["problem"] or "", (o["problem"] or "").replace("_", " "))
                    why = f"  (cannot run: {problem})" if o["problem"] else ""
                    typer.echo(f"    {o['kind']:10} {o['flow'] or 'no connector'}{why}")
                for h in p["holds"]:
                    typer.echo(f"    held: {h['reason']} since {h['since']}")
            return
        scheduled = [w for w in windows if (ex.store.session(surface, w.day, w.idx) or {}).get("status",
                                                                                               "scheduled") == "scheduled"]
        if not scheduled:
            typer.secho(f"error: no session of {surface} is still to run on {the_day}", err=True, fg=typer.colors.RED)
            raise typer.Exit(1)
        w = scheduled[0] if session is not None else min(scheduled, key=lambda x: x.start)
        ex.store.put_session(surface, w.day, w.idx, planned_start=ex.now(), minutes=w.minutes, origin=w.origin)
        on = svc.sessions_enabled()
    typer.echo(f"{surface}: session {w.idx + 1} of {w.day} moved to now."
               + (" The node runs it within a minute." if on else
                  " Engagement sessions are off on this node (engage_sessions in node.toml), so it does not run."))


@app.command("holds")
def holds(surface: Annotated[str, typer.Argument(help="The account, e.g. biz-a/instagram.")],
          show_all: Annotated[bool, typer.Option("--all", help="Include cleared holds.")] = False,
          as_json: JsonOpt = False) -> None:
    """The holds on an account: each pause on its engagement (likes, comments, follows) with what stopped it,
    since when, and until when."""
    with _service() as svc:
        found = svc.store.holds(surface, include_cleared=show_all)
        now = svc.now()
    if as_json:
        _print_json(found)
        return
    if not found:
        typer.echo(f"{surface}: no holds.")
        return
    for h in found:
        what = f"{h['kind']} only" if h["kind"] else "everything"
        until = f"until {h['until']}" if h["until"] else "until you clear it"
        state = f"cleared {h['cleared_at']}" if h["cleared_at"] else (
            "ended" if h["until"] and h["until"] <= now.isoformat(timespec="seconds") else "in force")
        typer.echo(f"  #{h['id']}  {h['reason']}  {what}, since {h['since']}, {until}  ({state})"
                   + (f"  {h['detail']}" if h["detail"] else ""))


@hold_app.command("clear")
def hold_clear(surface: Annotated[str, typer.Argument(help="The account, e.g. biz-a/instagram.")],
               kind: Annotated[str | None, typer.Option("--kind", help="Only the holds of this kind.")] = None,
               hold_id: Annotated[int | None, typer.Option("--id", help="Only this hold.")] = None) -> None:
    """Clear an account's holds (pauses on its engagement) once you have checked it. Owner only, on the node.
    A lane the site challenged is handed back separately, in the panel or with agentos lane."""
    with _service() as svc:
        n = _executor(svc).clear_hold(surface, by=OWNER, kind=kind, hold_id=hold_id)
    typer.echo(f"{surface}: {n} hold{'s' if n != 1 else ''} cleared." if n else f"{surface}: no hold to clear.")


@app.command("items")
def items(surface: Annotated[str, typer.Argument(help="The account, e.g. biz-a/reddit.")],
          status: Annotated[str | None, typer.Option("--status", help="Only items in this status.")] = None,
          as_json: JsonOpt = False) -> None:
    """The items of an account: approved content waiting for a session, and what came of it."""
    with _service() as svc:
        found = svc.items.list(surface, status=status)
    if as_json:
        _print_json(found)
        return
    if not found:
        typer.echo(f"{surface}: no items.")
        return
    for i in found:
        text = " ".join(i["text"].split())
        text = text if len(text) <= 60 else text[:57] + "..."
        where = f" to {i['targets'][0]}" if i["targets"] else ""
        what = {"queued": f"queued until {i['expires_at']}", "used": f"used ({i['used_run_id'] or 'no run'})",
                "reserved": "in a session now",
                "awaiting": f"waiting for your approval ({i['used_run_id'] or 'no run'})",
                "failed": f"failed and may have posted: check run {i['used_run_id'] or 'unknown'}, then withdraw "
                          "or add it again"}.get(i["status"], i["status"])
        asked = f", asked by {i['requested_by']['id']}" if i.get("requested_by") else ""
        typer.echo(f"  {i['id']}  {i['kind']}{where}  {what}{asked}")
        typer.echo(f"      {text}")


@item_app.command("withdraw")
def item_withdraw(item_id: Annotated[str, typer.Argument(help="The item's id (it_...).")]) -> None:
    """Take a queued item back, so no session uses it. Owner, on the node."""
    with _service() as svc:
        out = svc.items.withdraw(item_id, by=OWNER)
    typer.echo(f"{out['id']}: withdrawn. No session will use it.")


@profile_app.command("reset")
def profile_reset(surface: Annotated[str, typer.Argument(help="The account, e.g. biz-a/linkedin.")],
                  field: Annotated[list[str] | None, typer.Option("--field", help="name, headline, about, photo or "
                                                                          "cover (repeat it; every field if left out).")]
                  = None) -> None:
    """Forget what the engine knows about profile fields: the value it last read back, an edit
    still waiting or not proved, one you denied. Each field is due again at the next profile edit.
    Owner only, on the node."""
    with _service() as svc:
        out = svc.reset_profile(surface, field or None, by=OWNER)
    typer.echo(f"{surface}: reset {', '.join(out['reset'])}. Each is edited again if the program asks for a value, "
               "after your approval.")
