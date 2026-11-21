"""`openajar lane sites | gate-log | rules | pace`: which sites a lane may reach, what its gate
did, how its rules hold up, and how fast it acts."""

from __future__ import annotations

import contextlib
import json
import urllib.parse
from datetime import UTC, datetime
from typing import Annotated

import typer

from openajar.commands.base import (
    LaneIdArg,
    daemon_request,
    fail,
    initialized_config,
    lane_api_path,
    run_manager,
    valid_lane_id,
)
from openajar.commands.lanes import lane_app
from openajar.core.config import (
    NodeConfig,
)
from openajar.core.errors import InvalidRequest, OpenajarError

SITES_ACTIONS = ("show", "add", "remove", "clear", "suggest", "strict", "observe")


@lane_app.command("sites")
def lane_sites(
    lane_id: LaneIdArg,
    action: Annotated[str, typer.Argument(help="show, add, remove, clear, suggest, strict or observe.")] = "show",
    values: Annotated[list[str] | None, typer.Argument(
        help="Domain patterns (instagram.com, *.instagram.com, frame:www.google.com, accounts.google.@cc), a "
             "platform for suggest, on/off for strict, or on <minutes>/off for observe.",
        show_default=False)] = None,
) -> None:
    """The lane's site list: the only hosts its pages may load documents from (every navigation, redirect,
    frame and popup, checked on the node). Empty means no restriction on this lane. `frame:<host>` allows a
    host only inside a frame (a captcha), never as a page; `accounts.google.@cc` is that host on any of
    Google's country domains. `suggest` without a platform lists the suggested lists; with one, it adds
    that list (with the sign-in and captcha hosts every login can need). `strict on` also blocks refused
    requests before they leave while an agent acts, at the cost of the HTTP cache for that moment. While
    agentosd runs, a change reaches a running lane at once. `observe on <minutes>` (default 60, at most
    240) records every non-GET request of the lane by its shape (its host, method, path, and the names of
    its operation and fields, never a value), so you can check the lane's rules against a throwaway
    account's real traffic."""
    from openajar.node import egress

    if action not in SITES_ACTIONS:
        raise fail(f"unknown action {action!r}; use one of: {', '.join(SITES_ACTIONS)}")
    values = list(values or [])

    async def current(manager):
        return manager.get(lane_id)

    if action == "show":
        if values:
            raise fail("show takes no values")
        info = run_manager(current)
        show_sites(lane_id, info.spec.sites, info.spec.egress_strict, egress)
        return
    if action == "observe":
        if not values or values[0] not in ("on", "off") or len(values) > 2 or (values[0] == "off" and len(values) > 1):
            raise fail("observe takes on [minutes] or off")
        run_manager(current)  # the lane exists
        lane_dir = initialized_config().lane_dir(lane_id)
        lane_dir.mkdir(parents=True, exist_ok=True)
        try:
            minutes = int(values[1]) if len(values) > 1 else 60
            until = egress.write_observe(lane_dir / egress.OBSERVE_NAME, minutes if values[0] == "on" else None)
        except ValueError as e:
            raise fail("observe takes a whole number of minutes") from e
        except InvalidRequest as e:
            raise fail(e.message) from e
        if until is None:
            typer.echo(f"Lane {lane_id}: not observing requests.")
        else:
            at = datetime.fromtimestamp(until, UTC).isoformat(timespec="minutes")
            typer.echo(f"Lane {lane_id}: every non-GET request is traced by its shape until {at} "
                       f"(a running lane notices within two seconds).")
        return
    if action == "suggest" and not values:
        typer.echo("Suggested site lists (apply one with `openajar lane sites "
                   f"{lane_id} suggest <platform>`; nothing applies them on its own):")
        for platform, sites in egress.SUGGESTED_SITES.items():
            typer.echo(f"  {platform}: {', '.join(sites)}")
        return
    if action == "add":
        if not values:
            raise fail("add needs one or more domain patterns, for example instagram.com *.instagram.com")
        body: dict = {"op": "add", "sites": values}
    elif action == "remove":
        if not values:
            raise fail("remove needs the patterns to remove")
        body = {"op": "remove", "sites": values}
    elif action == "clear":
        body = {"op": "set", "sites": []}
    elif action == "suggest":
        if len(values) != 1 or values[0] not in egress.SUGGESTED_SITES:
            raise fail(f"suggest takes one platform: {', '.join(egress.SUGGESTED_SITES)}")
        body = {"op": "add", "sites": list(egress.SUGGESTED_SITES[values[0]])}
    else:  # strict
        if len(values) != 1 or values[0] not in ("on", "off"):
            raise fail("strict takes on or off")
        body = {"op": "add", "sites": [], "strict": values[0] == "on"}
    sites, strict, running = change_lane_sites(lane_id, body)
    show_sites(lane_id, sites, strict, egress, trace=False)
    if running:
        typer.echo("The running lane has the new list now.")


@lane_app.command("gate-log")
def lane_gate_log(
    lane_id: LaneIdArg,
    since: Annotated[str | None, typer.Option("--since", help="Only request shapes seen since then: 30m, "
                                                              "24h, 7d or an ISO date.")] = None,
    allow_read: Annotated[str | None, typer.Option("--allow-read", metavar="SHAPE",
                                                   help="Add a shape to the node's read list (it publishes "
                                                        "nothing).")] = None,
    as_commit: Annotated[str | None, typer.Option("--as-commit", metavar="NAME",
                                                  help="Make --shape a named commit rule, like "
                                                       "post.media.")] = None,
    shape: Annotated[str | None, typer.Option("--shape", help="The request shape that --as-commit makes a "
                                                              "rule.")] = None,
    remove: Annotated[str | None, typer.Option("--remove", metavar="SHAPE", help="Undo a promotion.")] = None,
    all_lanes: Annotated[bool, typer.Option("--all-lanes", help="Promote for every lane of the node, not this "
                                                                "lane only.")] = False,
    observed: Annotated[bool, typer.Option("--observed", help="Also list what observe mode saw that no "
                                                              "commit or write rule matched (add those with "
                                                              "--as-commit only).")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """What the commit gate (the check that holds a publish until a person approves exactly what it publishes)
    classified without a rule while agents drove this lane: each request shape (its host, method, path, and
    the names of its operation and fields, never a value), with how it was classified and how often.
    `--allow-read <shape>` adds a shape that publishes nothing to the node's read list; `--as-commit <name>
    --shape <shape>` makes it a named commit rule (the gate files it under that name). Either one applies
    to exactly that host, path and method, on this lane only unless `--all-lanes`. Stored on this node
    (OPENAJAR_HOME/gate-shapes.json), each change written to the action log; every lane's gate reads it
    within seconds. `--observed` adds what observe mode saw that no commit or write rule matched (a publish
    the gate takes for a read): name one with `--as-commit`. The owner only."""
    from openajar.node import gateshapes

    chosen = [x for x in (allow_read, as_commit, remove) if x]
    if len(chosen) > 1:
        raise fail("use one of --allow-read, --as-commit or --remove")
    if (as_commit is None) != (shape is None):
        raise fail("--as-commit NAME goes with --shape SHAPE")
    if all_lanes and not (allow_read or as_commit):
        raise fail("--all-lanes goes with --allow-read or --as-commit")
    body: dict | None = None
    if allow_read:
        body = {"allow_read": allow_read, "all_lanes": all_lanes}
    elif as_commit:
        body = {"as_commit": as_commit, "shape": shape, "all_lanes": all_lanes}
    elif remove:
        body = {"remove": remove}
    cfg = initialized_config()
    path = lane_api_path(lane_id, "gate-log")
    try:
        if body is None:
            params = {k: v for k, v in (("since", since), ("observed", "1" if observed else None)) if v}
            query = "?" + urllib.parse.urlencode(params) if params else ""  # a zone's + is not a space
            done = daemon_request(cfg, "GET", path + query, absent_ok=True)
            if not isinstance(done, dict):
                run_manager(lambda manager: _lane_exists(manager, lane_id))
                promoted = gateshapes.load(gateshapes.store_path(cfg.home))
                rows = gateshapes.trace_rows(cfg.lane_dir(lane_id))
                done = {"lane": lane_id, "promoted": promoted,
                        "shapes": gateshapes.log(rows, since=gateshapes.since_of(since), promoted=promoted,
                                                 observed=observed),
                        "identities": gateshapes.identities(rows, since=gateshapes.since_of(since))}
        else:
            done = daemon_request(cfg, "POST", path, body, absent_ok=True, person=True)
            if not isinstance(done, dict):
                done = _gate_shape_locally(cfg, lane_id, body)
    except OpenajarError as e:
        raise fail(e.message) from e
    if as_json:
        typer.echo(json.dumps(done, indent=2, default=str))
    elif body is None:
        show_gate_log(done, since)
    else:
        entry = done.get("promoted") or done.get("removed") or {}
        what = "the read allowlist" if entry.get("as") == "read" else f"commit rule {entry.get('rule')}"
        verb = "Removed" if "removed" in done else "Promoted"
        where = "" if verb == "Removed" else f" {gateshapes.scope(entry.get('lanes'))}"
        typer.echo(f"{verb} {entry.get('method')} {entry.get('host')}{entry.get('path')}"
                   f"{' ' + entry['op'] if entry.get('op') else ''} {'from' if verb == 'Removed' else 'to'} {what}"
                   f"{where}. Every lane's gate reads it within seconds; the action log has the change.")


@lane_app.command("rules")
def lane_rules(
    lane_id: Annotated[str | None, typer.Argument(help="One lane (workspace/name); every lane when left out.")] = None,
    stale_days: Annotated[float, typer.Option("--stale-days", help="Flag a verified rule quiet this many days while "
                                                                   "its site sends unknown writes.")] = 14,
    show_all: Annotated[bool, typer.Option("--all", help="List every unknown request shape, not only those "
                                                         "that look like writes.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """Rule health: when each commit rule (a publish) and write rule (a change that needs only draft
    permission) last matched on this node; the request shapes no rule covers that each site sent (gated,
    let through or observed); and what looks like drift: a verified rule that went quiet while its site
    sends unknown requests of the same kind, a burst of new unknown writes, a lane on a site with no rules
    (its publishes are not gated). Read from the lanes' request logs (names and shapes only, never a
    value). Fix a stale rule without a code change: `openajar lane gate-log <lane> --as-commit
    <kind>.<detail> --shape <id>`."""
    from openajar.node import rulehealth

    if lane_id is not None:
        valid_lane_id(lane_id)
    cfg = initialized_config()

    async def specs(manager):
        return [info.spec for info in manager.list()]

    done = rulehealth.for_node(cfg.home, run_manager(specs), lane=lane_id, stale_days=stale_days)
    if as_json:
        typer.echo(json.dumps(done, indent=2, default=str))
        return
    show_rule_health(done, lane_id, show_all=show_all)


def show_rule_health(done: dict, lane_id: str | None, *, show_all: bool = False) -> None:
    where = f"lane {lane_id}" if lane_id else "this node"
    since = done.get("since")
    typer.echo(f"Effect rules on {where}, from the egress traces" + (f" (since {since})." if since else
                                                                      " (no trace rows yet)."))
    flags = done.get("flags") or []
    if flags:
        typer.echo("Needs you:")
        for f in flags:
            typer.echo(f"  ! {f['message']}")
    else:
        typer.echo("No drift seen: no quiet verified rule with unknown writes on its site, and no lane on a site "
                   "without rules.")
    typer.echo("Commit and write rules (last matched, matches in the traces):")
    rules = sorted(done.get("rules") or [], key=lambda r: (r["site"], not (r.get("verified") or r.get("owner")),
                                                           r["id"]))
    width = max((len(r["id"]) for r in rules), default=10)
    for r in rules:
        state = (f"last {r['last']}  x{r['count']}  {', '.join(r['lanes'])}" if r.get("last")
                 else "no match in the traces")
        mark = "owner" if r.get("owner") else "verified" if r.get("verified") else "unverified"
        typer.echo(f"  {r['id'].ljust(width)}  {r['effect']:<6}  {mark:<10}  {state}")
    shapes = [e for e in done.get("unknown") or [] if show_all or e.get("write_like")]
    if shapes:
        typer.echo("Shapes no rule names" + ("" if show_all else " that look like writes (--all lists every one)")
                   + ":")
        for e in shapes[:40]:
            decided = ", ".join(f"{k} {v}" for k, v in e.get("decisions", {}).items())
            kind = f"  like a {e['kind']}" if e.get("kind") else ""
            promoted = "  [promoted]" if e.get("promoted") else ""
            typer.echo(f"  {e['id']}  {e['site']}  {e['method']} {e['host']}{e['path']}"
                       f"{'  op ' + e['op'] if e.get('op') else ''}  x{e['count']} ({decided}){kind}{promoted}")
            typer.echo(f"      seen {e.get('first')} to {e.get('last')} on {', '.join(e.get('lanes') or [])}")
        if len(shapes) > 40:
            typer.echo(f"  and {len(shapes) - 40} more (--json lists them all)")


@lane_app.command("pace")
def lane_pace(
    lane_id: Annotated[str | None, typer.Argument(help="workspace/name, for example biz-a/instagram.")] = None,
    preset: Annotated[bool, typer.Option("--preset", help="Apply the recommended pace for a new account: "
                                                          "08:00-22:00 on the lane's clock, 45 minutes "
                                                          "apart, 3 a day.")] = False,
    hours: Annotated[list[str] | None, typer.Option(
        "--hours", help="Waking hours on the lane's clock, like 08:00-22:00, 'mon-fri 09:00-18:00' or "
                        "22:00-02:00 (repeat it for several windows).")] = None,
    gap: Annotated[int | None, typer.Option("--gap", help="At least this many minutes between posts.")] = None,
    per_day: Annotated[int | None, typer.Option("--per-day", help="Posts a lane-local day.")] = None,
    timezone: Annotated[str | None, typer.Option(
        "--timezone", help="A time zone, like America/New_York (the IANA name): overrides the lane's fixed "
                           "zone, or with --workspace sets the default for its lanes that have none. 'auto' "
                           "removes a lane's; 'off' removes a workspace's.")] = None,
    workspace: Annotated[str | None, typer.Option(
        "--workspace", help="A workspace's default timezone: shows it, or sets it with --timezone.")] = None,
    off: Annotated[bool, typer.Option("--off", help="Remove the lane's pace: it is not paced.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """A lane's posting pace: its waking hours, read on the lane's own clock (its proxy's region from its
    fixed browser fingerprint, unless you set a timezone), the gap between posts and the posts a day. A
    person driving the lane by hand is never paced; runs and agents are, including runs you start. A
    publish outside those hours is refused (lane_paced), whatever is approved; a queued or scheduled run
    moves to the next allowed time. Without options, shows the pace, whose clock it reads and when the lane
    may post next. Off for every lane until you set one."""
    if workspace is not None:
        if lane_id or preset or hours or gap is not None or per_day is not None or off:
            raise fail("--workspace goes with --timezone only")
        return _workspace_zone(workspace, timezone, as_json=as_json)
    if not lane_id:
        raise fail("name a lane (workspace/name), or --workspace with --timezone")
    valid_lane_id(lane_id)
    changes = [preset, bool(hours), gap is not None, per_day is not None, timezone is not None, off]
    if preset and (hours or gap is not None or per_day is not None):
        raise fail("--preset sets hours, gap and posts a day; leave the others out, or set them without it")
    if off and any(changes[:5]):
        raise fail("--off removes the pace; leave the other options out")
    cfg = initialized_config()
    path = lane_api_path(lane_id, "pace")
    body: dict | None = None
    if any(changes):
        current = daemon_request(cfg, "GET", path, absent_ok=True)
        if not isinstance(current, dict):
            current = _pace_locally(cfg, lambda svc: svc.lane_pace(lane_id))
        pace = _changed_pace(current, preset=preset, hours=hours, gap=gap, per_day=per_day, timezone=timezone)
        body = {"pace": None if off else pace}
    try:
        if body is None:
            done = daemon_request(cfg, "GET", path, absent_ok=True)
            if not isinstance(done, dict):
                done = _pace_locally(cfg, lambda svc: svc.lane_pace(lane_id))
        else:
            done = daemon_request(cfg, "PUT", path, body, absent_ok=True, person=True)
            if not isinstance(done, dict):
                done = _pace_locally(cfg, lambda svc: svc.set_lane_pace(lane_id, body["pace"], by="owner:cli"))
    except OpenajarError as e:
        raise fail(e.message) from e
    if as_json:
        typer.echo(json.dumps(done, indent=2, default=str))
    else:
        show_pace(done)


def _workspace_zone(workspace: str, timezone: str | None, *, as_json: bool) -> None:
    """`lane pace --workspace`: show a workspace's default timezone, or set it with --timezone."""
    cfg = initialized_config()
    path = f"/api/pace/workspaces/{workspace}"
    if timezone is None:  # show it: never change a default the owner did not name
        done = daemon_request(cfg, "GET", path, absent_ok=True)
        if not isinstance(done, dict):
            done = _pace_locally(cfg, lambda svc: {"workspace": workspace,
                                                   "timezone": svc.pace.workspace_zone(workspace)})
    else:
        zone = None if timezone.strip().lower() in ("off", "auto", "") else timezone
        done = daemon_request(cfg, "PUT", path, {"timezone": zone}, absent_ok=True)
        if not isinstance(done, dict):
            done = _pace_locally(cfg, lambda svc: svc.set_workspace_zone(workspace, zone, by="owner:cli"))
    hint = "" if timezone is not None else (" Set it with --timezone America/New_York, or remove it with "
                                            "--timezone off.")
    typer.echo(json.dumps(done, indent=2) if as_json else
               f"Workspace {workspace}: lanes with no pin and no timezone of their own read "
               f"{done.get('timezone') or 'the node zone'}.{hint}")


def _changed_pace(current: dict, *, preset: bool, hours: list[str] | None, gap: int | None, per_day: int | None,
                  timezone: str | None) -> dict | None:
    """The lane's pace with the options given applied over what it has (None: no pace left)."""
    pace = dict(current.get("pace") or {})
    if preset:
        pace.update(current.get("preset") or {})
    if hours:
        pace["active_hours"] = hours
    if gap is not None:
        pace["min_gap_minutes"] = gap
    if per_day is not None:
        pace["commits_per_day"] = per_day
    if timezone is not None:
        pace["timezone"] = None if timezone in ("auto", "") else timezone
    return {k: v for k, v in pace.items() if v is not None} or None


def _pace_locally(cfg: NodeConfig, fn):
    """The pace with agentosd stopped: the policy service on its own database."""
    from pydantic import ValidationError

    from openajar.policy.service import PolicyService

    svc = PolicyService(cfg.home, node_config=cfg)
    try:
        return fn(svc)
    except ValidationError as e:
        raise fail(str(e.errors()[0].get("msg"))) from None
    except ValueError as e:
        raise fail(str(e)) from None
    except OpenajarError as e:
        raise fail(e.message) from e
    finally:
        svc.close()


def show_pace(p: dict) -> None:
    zone = p.get("zone") or {}
    pace = p.get("pace") or {}
    typer.echo(f"{p['lane']}: lane time is {zone.get('timezone')} ({zone.get('source_words')}), "
               f"now {str(p.get('local_now', ''))[11:16]}")
    if zone.get("note"):
        typer.echo(f"  note: {zone['note']}")
    if not p.get("paced"):
        typer.echo("  not paced: agents may publish at any hour (`openajar lane pace <lane> --preset` sets "
                   "08:00-22:00, 45 minutes apart, 3 a day)")
        return
    parts = []
    if pace.get("active_hours"):
        parts.append("waking hours " + ", ".join(pace["active_hours"]))
    if pace.get("min_gap_minutes"):
        parts.append(f"at least {pace['min_gap_minutes']} minutes apart")
    if pace.get("commits_per_day") is not None:
        parts.append(f"{pace['commits_per_day']} a day")
    typer.echo("  pace: " + "; ".join(parts))
    last = p.get("last_post_at")
    typer.echo(f"  posts today: {p.get('posts_today', 0)}" + (f" (last {last})" if last else ""))
    if p.get("may_post_now"):
        typer.echo("  may post now")
    else:
        typer.echo(f"  next allowed: {p.get('next_allowed_local') or 'none in the next week'} lane time"
                   + (f", {p['next_allowed_at']}" if p.get("next_allowed_at") else ""))


async def _lane_exists(manager, lane_id: str):
    return manager.get(lane_id)


def _gate_shape_locally(cfg: NodeConfig, lane_id: str, body: dict) -> dict:
    """A gate-log change with agentosd stopped: the file, and the audit row in the action log."""
    from openajar.node import gateshapes
    from openajar.policy.service import PolicyService

    run_manager(lambda manager: _lane_exists(manager, lane_id))
    by = "owner:cli"
    if "remove" in body:
        entry, removed = gateshapes.remove(cfg.home, body["remove"]), True
    elif "allow_read" in body:
        entry, removed = gateshapes.promote(cfg.home, cfg.lane_dir(lane_id), body["allow_read"], as_="read",
                                            lane=lane_id, by=by, all_lanes=bool(body.get("all_lanes"))), False
    else:
        entry, removed = gateshapes.promote(cfg.home, cfg.lane_dir(lane_id), body["shape"], as_="commit",
                                            name=body["as_commit"], lane=lane_id, by=by,
                                            all_lanes=bool(body.get("all_lanes"))), False
    policy = PolicyService(cfg.home, node_config=cfg)
    try:
        gateshapes.audit(policy.traces, entry, lane=lane_id, by=by, removed=removed)
        policy.traces.flush()
    finally:
        with contextlib.suppress(Exception):
            policy.traces.close()
    return {"lane": lane_id, "removed" if removed else "promoted": entry}


def show_gate_log(done: dict, since: str | None) -> None:
    from openajar.node import gateshapes

    lane_id, shapes = done.get("lane"), done.get("shapes") or []
    if not shapes:
        typer.echo(f"Lane {lane_id}: the commit gate classified no shape without a rule"
                   f"{' since ' + since if since else ''}.")
    else:
        typer.echo(f"Lane {lane_id}: what the commit gate classified without a rule"
                   f"{' since ' + since if since else ''} (one row a second at most per shape):")
    for e in shapes:
        decided = ", ".join(f"{k} {v}" for k, v in e.get("decisions", {}).items())
        promoted = (f"  [promoted: {e['promoted']} {gateshapes.scope(e.get('promoted_lanes'))}]"
                    if e.get("promoted") else "")
        typer.echo(f"  {e['id']}  {e['method']} {e['host']}{e['path']}{'  op ' + e['op'] if e.get('op') else ''}"
                   f"  x{e['count']} ({decided}){promoted}")
        for why, n in e.get("why", {}).items():
            typer.echo(f"      why: {why} ({n})")
        if e.get("doc_ids"):
            typer.echo(f"      doc_ids: {', '.join(e['doc_ids'])}")
        if e.get("observed_only"):
            typer.echo("      seen in observe mode only (the gate let it go as a read or never drove it): "
                       "--as-commit names it, a read promotion does not apply")
        if e.get("mixed"):
            typer.echo(f"      mixes {e['field_sets']} bodies on one path (field sets): it cannot be promoted as a read "
                       "without an operation")
        if e.get("fields"):
            typer.echo(f"      fields: {', '.join(e['fields'][:30])}{' ...' if len(e['fields']) > 30 else ''}")
        typer.echo(f"      seen {e.get('first')} to {e.get('last')}")
    promoted = done.get("promoted") or []
    if promoted:
        typer.echo("Promoted on this node:")
        for p in promoted:
            what = "read" if p.get("as") == "read" else p.get("rule")
            typer.echo(f"  {p['id']}  {p['method']} {p['host']}{p['path']}{'  op ' + p['op'] if p.get('op') else ''}"
                       f"  -> {what} {gateshapes.scope(p.get('lanes'))}  (by {p.get('by')}, {p.get('at')})")
    if shapes:
        typer.echo("Promote one: --allow-read <shape> (it publishes nothing) or --as-commit <name> --shape <shape>.")
    found = done.get("identities") or []
    if found:
        typer.echo("Publishes that acted as an identity openajar does not know (no surface names it):")
        for e in found:
            who = e.get("id") or "(unreadable)"
            where = f" in {e['field']}" if e.get("field") else ""
            flow = ", a flow refused" if e.get("flow") else ""
            typer.echo(f"  {e.get('site')} {who}{where}  x{e.get('count')} ({', '.join(e.get('rules') or [])}{flow})")
            if e.get("why"):
                typer.echo(f"      why: {e['why']}")
            typer.echo(f"      seen {e.get('first')} to {e.get('last')}")
        typer.echo("If one is a Page or company page you manage, add it as a surface (`openajar connector bind`, or POST /api/surfaces).")


def change_lane_sites(lane_id: str, body: dict) -> tuple[list[str], bool, bool]:
    """Through the running agentosd, so a warm lane's guard has the new list at once; when it is not
    running (or predates the route), in the store, which the next lane start reads. -> (sites,
    strict, applied to a running lane)."""
    from openajar.node.viewer_actions import change_sites

    cfg = initialized_config()
    done = daemon_request(cfg, "POST", lane_api_path(lane_id, "sites"), body, absent_ok=True, person=True)
    if isinstance(done, dict):
        return list(done.get("sites") or []), bool(done.get("strict")), bool(done.get("running"))
    result = run_manager(lambda manager: change_sites(manager, lane_id, body, by="owner"))
    return result["sites"], result["strict"], False


def show_sites(lane_id: str, sites: list[str], strict: bool, egress, *, trace: bool = True) -> None:
    from openajar.node import rulehealth

    if sites:
        typer.echo(f"Lane {lane_id} loads documents only from: {', '.join(sites)}")
        unruled = [e for e in sites if not e.startswith(egress.FRAME_PREFIX)
                   and not rulehealth.covered(e, egress.EFFECT_RULES)]
        if unruled:
            typer.echo(f"No effect rules for: {', '.join(unruled)}. Publishes there are not gated (no approval is "
                       "asked); their non-GET requests are traced by shape (openajar lane rules).")
    else:
        typer.echo(f"Lane {lane_id} has no site list: no lane restriction (grants still apply).")
    typer.echo(f"Strict egress: {'on' if strict else 'off'}.")
    until = egress.read_observe(initialized_config().lane_dir(lane_id) / egress.OBSERVE_NAME)
    if until is not None:
        typer.echo(f"Observing requests until {datetime.fromtimestamp(until, UTC).isoformat(timespec='minutes')}.")
    if not trace:
        return
    rows = egress.EgressTrace(initialized_config().lane_dir(lane_id) / egress.TRACE_NAME).read(limit=10)
    if rows:
        typer.echo("Recent egress records:")
        for row in rows:
            kind = row.get("kind")
            what = (row.get("rule") if kind == "blocked" else "observed commit" if kind == "observed_commit"
                    else f"observed {(row.get('shape') or {}).get('op') or 'request'}" if kind == "observed_request"
                    else kind)
            typer.echo(f"  {row.get('at', '?')}  {what}  {row.get('method', '')} {row.get('host', '?')}"
                       f"{row.get('path', '')}")
