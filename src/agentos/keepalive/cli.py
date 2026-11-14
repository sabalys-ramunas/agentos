"""`agentos lane keepalive`: see and switch session keepalive, as the owner on the node.

    agentos lane keepalive biz-a/instagram           the next planned visit, the last ones, why not now
    agentos lane keepalive biz-a/instagram --now     one visit now, in the foreground (agentosd makes it)
    agentos lane keepalive biz-a/instagram --off     this lane opts out (--on opts it back in)
    agentos lane keepalive --node on                 the node switch in node.toml (off by default)

The plan and --now come from agentosd over the ops socket: it owns the lanes' browsers. The switches
are written to node.toml and to the lane's own directory (`keepalive-off`, mirrored in keepalive.db)
directly, and agentosd reads them again every round.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any

import typer

from agentos.core.config import config_path, home_from_env, load_config, save_config
from agentos.core.layout import ops_socket
from agentos.core.models import parse_lane_id
from agentos.core.system import Host

NOW_TIMEOUT_S = 900.0  # the wait for a slot plus the visit (agentos.keepalive.service.NOW_WAIT_S, Pace)


def _fail(message: str, code: int = 1) -> typer.Exit:
    typer.secho(f"error: {message}", err=True, fg=typer.colors.RED)
    return typer.Exit(code)


def _when(value: str | None, tz: str | None = None) -> str:
    if not value:
        return "none"
    at = datetime.fromisoformat(value)
    local = ""
    if tz and tz != "UTC":
        from zoneinfo import ZoneInfo

        local = f" ({at.astimezone(ZoneInfo(tz)):%a %H:%M} {tz})"
    return f"{at:%Y-%m-%d %H:%M} UTC{local}"


def format_status(plan: dict[str, Any]) -> list[str]:
    lane, tz = plan["lane"], plan.get("timezone")
    lines = [f"{lane}: keepalive {'on' if plan.get('enabled') else 'off'} on this node"
             + (", this lane opted out" if plan.get("opted_out") else "")]
    if plan.get("site"):
        lines.append(f"  site:        {plan['site']} ({plan.get('home')})")
    if plan.get("window_today"):
        lo, hi = plan["window_today"]
        lines.append(f"  awake:       {lo} to {hi} today, {tz} ({plan.get('timezone_source')})")
    if plan.get("activity_at"):
        lines.append(f"  last used:   {_when(plan['activity_at'], tz)}")
    if plan.get("session_expires_at"):
        lines.append(f"  session:     cookie expires {_when(plan['session_expires_at'], tz)}")
    blocked = plan.get("blocked")
    if blocked:
        lines.append(f"  next visit:  none now: {blocked['reason']}")
        if plan.get("next_at") and blocked["code"] in ("off", "opted_out"):
            lines.append(f"               would be {_when(plan['next_at'], tz)}")
    elif plan.get("next_at"):
        lines.append(f"  next visit:  {_when(plan['next_at'], tz)}")
    if plan.get("waiting"):
        lines.append(f"  waiting:     {plan['waiting']}")
    if plan.get("status"):
        lines.append(f"  marked:      {plan['status']}: {plan.get('status_reason')}")
    visits = plan.get("visits") or ([plan["last_visit"]] if plan.get("last_visit") else [])
    if not visits:
        lines.append("  last visit:  none yet")
    for i, v in enumerate(visits):
        label = "last visit:  " if i == 0 else "             "
        bits = [v["outcome"], f"{v.get('scrolls', 0)} scrolls"]
        if v.get("opened_item"):
            bits.append("opened one item")
        if v.get("session") and v["session"] != "unknown":
            bits.append(f"session {v['session']}")
        lines.append(f"  {label}{_when(v['at'], tz)}, {v['trigger']}: {', '.join(bits)}"
                     + (f" ({v['reason']})" if v.get("reason") else ""))
    return lines


def _offline(home: Any, lane: str) -> dict[str, Any]:
    from agentos.keepalive.store import KeepaliveStore, lane_opted_out

    cfg = load_config(home)
    store = KeepaliveStore(home)
    try:
        row = store.lane(lane)
        visits = [v.public() for v in store.visits(lane, 5)]
    finally:
        store.close()
    return {"lane": lane, "enabled": bool(cfg.keepalive), "opted_out": lane_opted_out(cfg.lane_dir(lane)),
            "status": row.status, "status_reason": row.reason, "visits": visits, "blocked": None,
            "note": "agentosd is not running, so there is no plan; this is what keepalive.db holds"}


def keepalive_cmd(
    lane: Annotated[str | None, typer.Argument(help="workspace/name; leave out with --node.")] = None,
    now: Annotated[bool, typer.Option("--now", help="Make one visit now and wait for it (agentosd makes it).")]
    = False,
    on: Annotated[bool | None, typer.Option("--on/--off", help="Opt this lane in or out.")] = None,
    node: Annotated[str | None, typer.Option("--node", help="Turn keepalive on or off on this node: on | off.")]
    = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """Session keepalive: a short read-only visit to an idle lane's site every few days, inside its waking
    hours, so its login does not go stale. Shows the next planned visit and the last ones. Off on a node
    until the owner turns it on (--node on), because it opens real accounts unattended."""
    home = home_from_env()
    if not config_path(home).exists():
        raise _fail(f"no node.toml in {home}; run `agentos node init` first (or set AGENTOS_HOME)")
    if node is not None:
        if node not in ("on", "off") or lane is not None or now or on is not None:
            raise _fail("--node takes on or off, and nothing else")
        cfg = load_config(home)
        save_config(cfg.model_copy(update={"keepalive": node == "on"}))
        typer.echo(f"keepalive is {node} on this node; agentosd reads it on its next round (within a minute)."
                   + (" Lanes opt out with `agentos lane keepalive <lane> --off`." if node == "on" else ""))
        return
    if lane is None:
        raise _fail("name a lane (agentos lane keepalive biz-a/instagram), or pass --node on|off")
    try:
        parse_lane_id(lane)
    except ValueError as e:
        raise _fail(str(e)) from None
    if on is not None:
        from agentos.keepalive.store import KeepaliveStore, set_lane_opt_out

        lane_dir = load_config(home).lane_dir(lane)
        if not lane_dir.is_dir():
            raise _fail(f"no lane {lane} on this node (agentos lane list)")
        set_lane_opt_out(lane_dir, not on)
        store = KeepaliveStore(home)
        try:
            store.set_opt_out(lane, not on)
        finally:
            store.close()
        typer.echo(f"{lane} {'takes part in' if on else 'opted out of'} keepalive.")
        if not now:
            return
    if now:
        typer.echo(f"Visiting {lane} now (read only, a few minutes)...", err=True)
    payload: dict[str, Any] = {"op": "keepalive", "lane": lane, **({"now": True} if now else {})}
    resp = Host().unix_request(ops_socket(home), payload, timeout=NOW_TIMEOUT_S if now else 30)
    if resp is None:
        if now:
            raise _fail("agentosd is not running, and it is what opens the lane; start it, then try again")
        plan = _offline(home, lane)
    elif not resp.get("ok"):
        err = resp.get("error") or {}
        raise _fail(f"{err.get('code')}: {err.get('message')}")
    else:
        plan = resp["keepalive"]
    if as_json:
        typer.echo(json.dumps({**plan, **({"visit": resp["visit"]} if resp and "visit" in resp else {})},
                              indent=2, default=str))
        return
    for line in format_status(plan):
        typer.echo(line)
    if plan.get("note"):
        typer.echo(f"({plan['note']})")
