"""CLI: `openajar grants ...`, `openajar approvals ...`, `openajar policy ...`.

`app` is the policy group (routing, notify, traces, key) and also carries `grants` and
`approvals` as subgroups; `apps` lets the root CLI mount all three at the top level.
Tokens are printed once and read back from a file or stdin, never from argv.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from openajar.core import catalog
from openajar.core.config import home_from_env, load_config
from openajar.core.errors import InvalidRequest, OpenajarError
from openajar.policy.ask_first import open_store
from openajar.policy.confirmation import UnreachableLevel
from openajar.policy.grants import BrainCap, EmptyCap, InstallCap, LaneCap, ServiceCap, Specials, VmCap
from openajar.policy.memory_rules import MemoryCap
from openajar.policy.service import PolicyService
from openajar.policy.util import parse_duration, parse_until

app = typer.Typer(no_args_is_help=True, help="Grants, approvals, the action log and notifications.")
grants_app = typer.Typer(no_args_is_help=True, help="Issue, narrow, inspect and revoke grants.")
approvals_app = typer.Typer(no_args_is_help=True, help="The Needs-you queue.")
confirmation_app = typer.Typer(no_args_is_help=True, help="How strong a yes must be, and where it can be given.")
standing_app = typer.Typer(no_args_is_help=True, help="Standing approvals (pre-approved budgets).")
notify_app = typer.Typer(no_args_is_help=True, help="Phone pushes and webhooks.")
telegram_app = typer.Typer(no_args_is_help=True, help="Approve from your phone, in Telegram.")
app.add_typer(grants_app, name="grants")
app.add_typer(approvals_app, name="approvals")
app.add_typer(notify_app, name="notify")
notify_app.add_typer(telegram_app, name="telegram")
approvals_app.add_typer(standing_app, name="standing")
apps = {"policy": app, "grants": grants_app, "approvals": approvals_app}

BY = "owner:cli"


def _service() -> PolicyService:
    home = home_from_env()
    cfg = load_config(home) if (home / "node.toml").exists() else None
    try:
        return PolicyService(home, node_config=cfg)
    except UnreachableLevel as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from None


def _fail(e: OpenajarError) -> None:
    typer.echo(catalog.cli_error(e.code, e.message), err=True)
    raise typer.Exit(1)


def _echo_json(obj: object) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


def _read_token(path: str) -> str:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text()
    return raw.strip()


def _specials(names: list[str], *, exec_lanes: list[str], secret_lanes: list[str], sources: list[str],
              service_ttl: str | None, brain_provider: str | None, brain_tier: str | None,
              brain_credential_owner: str | None, brain_quota_tokens: int | None,
              memory: dict | None = None, vm_profiles: list[str] | None = None,
              vm_lanes: list[str] | None = None, vm_repos: list[str] | None = None) -> Specials | None:
    if not names:
        return None
    caps: dict = {}
    for n in names:
        if n == "secret":
            caps["secret"] = LaneCap(lanes=secret_lanes or None)
        elif n == "exec":
            caps["exec"] = LaneCap(lanes=exec_lanes or None)
        elif n == "install_overlay":
            caps["install_overlay"] = InstallCap(sources=sources or None)
        elif n == "install_base":
            caps["install_base"] = EmptyCap()
        elif n == "service":
            caps["service"] = ServiceCap(max_ttl_s=parse_duration(service_ttl) if service_ttl else None)
        elif n == "brain":
            caps["brain"] = BrainCap(provider=brain_provider, tier=brain_tier,
                                     credential_owner=brain_credential_owner, quota_tokens=brain_quota_tokens)
        elif n == "vm":
            caps["vm"] = VmCap(lanes=vm_lanes or None, profiles=vm_profiles or None, repos=vm_repos or None)
        elif n == "memory":
            try:
                caps["memory"] = MemoryCap(**(memory or {}))
            except ValueError as e:
                raise typer.BadParameter(f"--special memory: {e}") from None
        else:
            raise typer.BadParameter(f"unknown special {n!r}")
    return Specials(**caps)


@grants_app.command("issue")
def grants_issue(
    subject: Annotated[str, typer.Option(help="who holds it, e.g. claude-code")],
    kind: Annotated[str, typer.Option(help="agent, human, flow, brain or panel")] = "agent",
    thread: Annotated[str | None, typer.Option(help="pin to one agent thread")] = None,
    workspace: Annotated[list[str] | None, typer.Option(help="workspace (repeatable)")] = None,
    lane: Annotated[list[str] | None, typer.Option(help="lane pattern like biz-a/insta* (repeatable)")] = None,
    domain: Annotated[list[str] | None, typer.Option(help="domain like instagram.com or *.example.com")] = None,
    ceiling: Annotated[str, typer.Option(help="read, draft, commit, pay or irreversible")] = "read",
    special: Annotated[list[str] | None, typer.Option(help="secret, exec, install_overlay, install_base, "
                                                          "service, brain, memory, vm (repeatable)")] = None,
    exec_lane: Annotated[list[str] | None, typer.Option(help="limit exec to these lane patterns")] = None,
    secret_lane: Annotated[list[str] | None, typer.Option(help="limit secret to these lane patterns")] = None,
    source: Annotated[list[str] | None, typer.Option(help="allowlisted pinned install source")] = None,
    service_ttl: Annotated[str | None, typer.Option(help="max service TTL, e.g. 12h")] = None,
    brain_provider: Annotated[str | None, typer.Option()] = None,
    brain_tier: Annotated[str | None, typer.Option(help="small, medium or large (haiku, sonnet, opus)")] = None,
    brain_credential_owner: Annotated[str | None, typer.Option()] = None,
    brain_quota_tokens: Annotated[int | None, typer.Option(help="tokens per day")] = None,
    memory_workspace: Annotated[list[str] | None, typer.Option(
        help="limit memory to these workspaces (repeatable; default: the grant's workspaces)")] = None,
    memory_write: Annotated[bool, typer.Option(help="memory may be written, not only read")] = False,
    memory_type: Annotated[list[str] | None, typer.Option(
        help="entry types memory may write: fact, document, decision, preference, contact (repeatable)")] = None,
    memory_private: Annotated[bool, typer.Option(help="memory may read private entries (contacts)")] = False,
    memory_audience_owner: Annotated[bool, typer.Option(
        help="memory may read owner-only entries (admin grants only)")] = False,
    vm_lane: Annotated[list[str] | None, typer.Option(help="limit vm to these lane patterns, e.g. dev/*")] = None,
    vm_profile: Annotated[list[str] | None, typer.Option(help="limit vm to these profiles (vms.toml; custom: "
                                                         "a size of the agent's own)")] = None,
    vm_repo: Annotated[list[str] | None, typer.Option(help="limit vm_clone to these repos, e.g. github.com/acme/*")
                       ] = None,
    budget_usd: Annotated[float | None, typer.Option()] = None,
    budget_tokens: Annotated[int | None, typer.Option()] = None,
    ttl: Annotated[str, typer.Option(help="lifetime, e.g. 12h, 7d, 2w")] = "7d",
    depth: Annotated[int | None, typer.Option(help="how many levels it may hand down")] = None,
    fanout: Annotated[int | None, typer.Option(help="how many children it may hand out")] = None,
    egress: Annotated[list[str] | None, typer.Option(help="egress allowlist domain (repeatable)")] = None,
    admin: Annotated[bool, typer.Option(help="may decide approvals and manage grants (panel)")] = False,
    note: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Issue a grant. The token is printed once; store it in your agent's MCP config."""
    svc = _service()
    try:
        budget = {"usd": budget_usd, "tokens": budget_tokens} if (budget_usd is not None or budget_tokens is not None) \
            else None
        g, token = svc.issue(
            subject={"id": subject, "kind": kind, "thread": thread}, workspaces=workspace or None,
            lanes=lane or None, domains=domain or None, ceiling=ceiling,
            specials=_specials(special or [], exec_lanes=exec_lane or [], secret_lanes=secret_lane or [],
                               sources=source or [], service_ttl=service_ttl, brain_provider=brain_provider,
                               brain_tier=brain_tier, brain_credential_owner=brain_credential_owner,
                               brain_quota_tokens=brain_quota_tokens,
                               memory={"workspaces": memory_workspace or None, "write": memory_write,
                                       "types": memory_type or None, "private": memory_private,
                                       "audience_owner": memory_audience_owner},
                               vm_profiles=vm_profile or None, vm_lanes=vm_lane or None,
                               vm_repos=vm_repo or None),
            budget=budget, ttl_s=parse_duration(ttl), depth=depth, fanout=fanout, egress=egress or None,
            admin=admin, note=note, by=BY)
    except OpenajarError as e:
        _fail(e)
    typer.echo(f"grant {g.id} for {subject}, expires {g.projection()['expires_at']}")
    typer.echo("token (shown once, not stored):", err=True)
    typer.echo(token)


@grants_app.command("attenuate")
def grants_attenuate(
    token_file: Annotated[str, typer.Option("--from", help="file holding the parent token, or - for stdin")],
    subject: Annotated[str | None, typer.Option()] = None,
    workspace: Annotated[list[str] | None, typer.Option()] = None,
    lane: Annotated[list[str] | None, typer.Option()] = None,
    domain: Annotated[list[str] | None, typer.Option()] = None,
    ceiling: Annotated[str | None, typer.Option()] = None,
    drop_specials: Annotated[bool, typer.Option(help="drop every special capability")] = False,
    budget_usd: Annotated[float | None, typer.Option()] = None,
    budget_tokens: Annotated[int | None, typer.Option()] = None,
    ttl: Annotated[str | None, typer.Option()] = None,
    depth: Annotated[int | None, typer.Option()] = None,
    note: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Derive a narrower child grant from a token. Widening is refused."""
    svc = _service()
    try:
        budget = {"usd": budget_usd, "tokens": budget_tokens} if (budget_usd is not None or budget_tokens is not None) \
            else None
        g, child = svc.attenuate(
            _read_token(token_file), subject={"id": subject} if subject else None, workspaces=workspace or None,
            lanes=lane or None, domains=domain or None, ceiling=ceiling,
            specials=Specials() if drop_specials else None, budget=budget,
            ttl_s=parse_duration(ttl) if ttl else None, depth=depth, note=note, by=BY)
    except OpenajarError as e:
        _fail(e)
    typer.echo(f"grant {g.id} (child of {g.blocks[-2].id if len(g.blocks) > 1 else 'owner'})")
    typer.echo("token (shown once, not stored):", err=True)
    typer.echo(child)


@grants_app.command("list")
def grants_list(all_: Annotated[bool, typer.Option("--all", help="include revoked and expired")] = False,
                as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List issued grants (hashes only; tokens are never stored)."""
    rows = _service().list_grants(include_inactive=all_)
    if as_json:
        _echo_json(rows)
        return
    if not rows:
        typer.echo("no grants")
    for r in rows:
        scope = r["scope"] or {}
        typer.echo(f"{r['id']}  {r['state']:<8} {r['subject']['id']:<20} ceiling={scope.get('ceiling', '?'):<12} "
                   f"level={r['depth']} expires={r['expires_at']}")


@grants_app.command("show")
def grants_show(grant_id: str) -> None:
    """Show one grant's readable projection."""
    try:
        _echo_json(_service().grant_record(grant_id))
    except OpenajarError as e:
        _fail(e)


@grants_app.command("explain")
def grants_explain(grant_id: str) -> None:
    """Say in plain language what a grant lets its holder do, and what waits for you."""
    from openajar.policy.errors import Denied
    from openajar.policy.explain import explain

    svc = _service()
    try:
        record = svc.grant_record(grant_id)
        if record["state"] != "active":
            typer.echo(f"{grant_id} is {record['state']}, so it allows nothing now.")
            return
        grant = svc.grant_by_id(grant_id)
    except Denied as e:
        typer.echo(f"{grant_id} allows nothing now: {e.message}")
        return
    except OpenajarError as e:
        _fail(e)
    for line in explain(grant, routing=svc.routing_now(), remaining=svc.remaining(grant)):
        typer.echo(line)


@grants_app.command("taint")
def grants_taint(grant_id: str, clear: Annotated[bool, typer.Option(
        "--clear", help="Start the grant's family clean: you are the owner in person.")] = False) -> None:
    """Show whether a grant's session read untrusted content and from where (kind and host), or clear it."""
    svc = _service()
    try:
        if clear:
            res = svc.clear_taint(grant_id, by=BY)
            typer.echo(f"cleared {res['family']}: {res['origins_cleared']} untrusted origins forgotten")
            return
        _echo_json(svc.taint_state(svc.grant_by_id(grant_id)))
    except OpenajarError as e:
        _fail(e)


@grants_app.command("meters")
def grants_meters(grant_id: Annotated[str | None, typer.Argument(help="a grant id; without it, --lane")] = None,
                  lane: Annotated[str | None, typer.Option("--lane", help="a lane id, like biz-a/instagram")] = None,
                  limit: Annotated[list[str] | None, typer.Option(
                      "--limit", help="set the lane's limits: usd_per_day=2, tokens_per_day=50000, "
                                      "commits_per_hour=2, commits_per_day=6 (repeat it)")] = None,
                  unset: Annotated[bool, typer.Option("--unset", help="remove the lane's own limits "
                                                                      "(policy.toml applies again)")] = False) -> None:
    """Today's use against the daily and hourly limits of a grant or a lane, or set a lane's limits."""
    svc = _service()
    try:
        if lane is not None:
            if limit or unset:
                pairs = dict(item.split("=", 1) for item in (limit or []) if "=" in item)
                if len(pairs) != len(limit or []):
                    raise InvalidRequest("--limit takes name=value, like commits_per_day=6")
                _echo_json(svc.set_lane_limits(lane, None if unset else pairs, by=BY))
            else:
                _echo_json(svc.lane_meter(lane))
            return
        if grant_id is None:
            raise InvalidRequest("name a grant id, or --lane")
        _echo_json(svc.grant_meter(svc.grant_by_id(grant_id)))
    except ValidationError as e:
        _fail(InvalidRequest(str(e.errors()[0].get("msg"))))
    except OpenajarError as e:
        _fail(e)


@grants_app.command("revoke")
def grants_revoke(grant_id: str, reason: Annotated[str | None, typer.Option()] = None) -> None:
    """Revoke a grant and, with it, every grant narrowed from it."""
    try:
        res = _service().revoke(grant_id, by=BY, reason=reason)
    except OpenajarError as e:
        _fail(e)
    below = res["cascades_to"]
    typer.echo(f"revoked {grant_id}" + (f" and {len(below)} narrowed from it" if below else ""))


@grants_app.command("rotate-key")
def grants_rotate_key(overlap: Annotated[str, typer.Option(
        help="how long grants signed with the old key keep working (30m, 12h, 7d)")] = "7d",
        yes: Annotated[bool, typer.Option("--yes", help="do not ask for confirmation")] = False) -> None:
    """Rotate the node's grant signing key. New grants use the new key id; grants signed with the
    old one keep working for the overlap, then are refused. Version 1 grants stop at once."""
    svc = _service()
    try:
        seconds = parse_duration(overlap)
    except OpenajarError as e:
        _fail(e)
    if not yes:
        typer.confirm(f"Rotate the grant key? Grants signed with {svc.kid} keep working for {overlap}.",
                      abort=True)
    try:
        out = svc.rotate_key(overlap_s=seconds, by=BY)
    except ValueError as e:
        _fail(InvalidRequest(str(e)))
    typer.echo(f"active key {out['kid']}")
    typer.echo(f"previous key {out['previous']} accepted until {out['retire_at']}")
    if out["v1_grants_ended"]:
        typer.echo(f"{out['v1_grants_ended']} version 1 grants stop working now; issue new grants for them")


@grants_app.command("keys")
def grants_keys(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """The node's grant keys: the active one and rotated ones still inside their overlap."""
    keys = _service().signing_keys()
    if as_json:
        _echo_json(keys)
        return
    for k in keys:
        until = f" until {k['retire_at']}" if k["state"] == "retiring" else ""
        typer.echo(f"{k['kid']}  {k['state']}{until}")


@grants_app.command("retire-key")
def grants_retire_key(kid: str, at: Annotated[str | None, typer.Option(
        help="when (an ISO date or a duration from now); now by default")] = None) -> None:
    """End a rotated key's overlap: grants signed with it are refused from then on."""
    svc = _service()
    try:
        out = svc.retire_key(kid, at=parse_until(at, time.time()) if at else None)
    except OpenajarError as e:
        _fail(e)
    typer.echo(f"{out['kid']} retired at {out['retire_at']}")


NodeOpt = Annotated[str | None, typer.Option(
    "--node", help="On your computer: decide on this connection's node (`openajar nodes`), as the owner.",
    show_default=False)]


def _remote(node: str | None) -> tuple[object, object] | None:
    """(client, connection) when this runs on the owner's computer: `--node`, or neither
    OPENAJAR_HOME nor a node.toml here and a connection made by `openajar connect`. The owner's
    device credential decides there, so what their agents asked can be approved (an agent's grant
    never decides its own asks)."""
    if node is None and (os.environ.get("OPENAJAR_HOME") or (home_from_env() / "node.toml").exists()):
        return None
    try:
        from openajar.client.errors import ClientError
        from openajar.client.ops import Client
    except ModuleNotFoundError:
        return None
    c = Client(out=lambda _line: None)
    if node is None and not c.connections():
        return None
    try:
        return c, c.pick(node)
    except ClientError as e:
        typer.echo(f"error: {e.message}" + (f"\n  {e.hint}" if e.hint else ""), err=True)
        raise typer.Exit(1) from None


def _remote_call(fn, *args, **kw):
    from openajar.client.errors import ClientError

    try:
        return fn(*args, **kw)
    except ClientError as e:
        typer.echo(f"error: {e.message}" + (f"\n  {e.hint}" if e.hint else ""), err=True)
        raise typer.Exit(1) from None


@approvals_app.command("list")
def approvals_list(state: Annotated[str | None, typer.Option(help="pending, approved, denied, expired, consumed")] = "pending",
                   limit: int = 50, as_json: Annotated[bool, typer.Option("--json")] = False,
                   node: NodeOpt = None) -> None:
    """List approvals and questions."""
    remote = _remote(node)
    if remote is not None:
        c, conn = remote
        rows = _remote_call(c.approvals, conn, state=state or None, limit=limit)
        if as_json:
            _echo_json(rows)
            return
        if not rows:
            typer.echo("nothing needs you")
        for a in rows:
            what = f"{a.get('effect')} on {a.get('lane')}" if a.get("kind", "action") == "action" else "question"
            typer.echo(f"{a.get('id')}  {a.get('state')!s:<9} {what:<32} {str(a.get('summary') or '')[:80]}")
        return
    try:
        items = _service().approvals.list(state=state or None, limit=limit)
    except OpenajarError as e:
        _fail(e)
    if as_json:
        _echo_json([a.public() for a in items])
        return
    if not items:
        typer.echo("nothing needs you")
    for a in items:
        what = f"{a.effect} on {a.lane}" if a.kind == "action" else "question"
        typer.echo(f"{a.id}  {a.state:<9} {what:<32} {a.summary[:80]}")


@approvals_app.command("approve")
def approvals_approve(approval_id: str, note: Annotated[str | None, typer.Option()] = None,
                      answer: Annotated[str | None, typer.Option(help="the answer, for a question")] = None,
                      node: NodeOpt = None) -> None:
    """Approve an action (or answer a question)."""
    remote = _remote(node)
    if remote is not None:
        c, conn = remote
        ap = _remote_call(c.decide, conn, approval_id, "approve", note=note, answer=answer)
        typer.echo(f"{ap.get('id', approval_id)} {ap.get('state', 'approved')}")
        return
    try:
        store = _service().approvals
        asked = store.get(approval_id)
        if asked.kind == "question" and asked.state == "pending" and not (answer or note or "").strip():
            # QA 2026-11-10, tester B 9: say how to answer, and with what
            options = " or ".join(f'--answer "{o}"' for o in asked.options) if asked.options else '--answer "..."'
            typer.echo(f"error: invalid: {approval_id} is a question, so approving it means answering it. Run it "
                       f"again with {options}:\n  openajar approvals approve {approval_id} {options.split(' or ')[0]}",
                       err=True)
            raise typer.Exit(1)
        seen = _read_all_messages(store, approval_id)
        ap = store.decide(approval_id, "approve", by=BY, note=note, answer=answer, seen=seen)
    except OpenajarError as e:
        _fail(e)
    if ap.kind == "question":
        typer.echo(f"{ap.id} answered: {ap.answer}. Whatever waits on it acts on the answer within a few seconds.")
        return
    typer.echo(f"{ap.id} {ap.state}")


def _read_all_messages(store: Any, approval_id: str) -> str | None:
    """A send card whose preview is cut: print every message it sends, and ask before approving.
    Returns the token the last page gives (None for any other card)."""
    if not store.get(approval_id).needs_whole_view():
        return None
    page, cursor, seen = 0, None, None
    while True:
        got = store.send_messages(approval_id, page, cursor)
        for m in got["messages"]:
            typer.echo(f"--- to {m.get('to')}{' (part of it is withheld)' if m.get('withheld') else ''}")
            for key, value in sorted((m.get("inputs") or {}).items()):
                typer.echo(f"{key}: {value}")
        seen = got["seen"]
        if got["next"] is None:
            break
        page, cursor = got["next"]["page"], got["next"]["cursor"]
    if not typer.confirm(f"Send these {got['count']} messages?", default=False):
        raise typer.Exit(1)
    return seen


@approvals_app.command("deny")
def approvals_deny(approval_id: str, note: Annotated[str | None, typer.Option()] = None,
                   node: NodeOpt = None) -> None:
    """Deny an action or decline a question."""
    remote = _remote(node)
    if remote is not None:
        c, conn = remote
        ap = _remote_call(c.decide, conn, approval_id, "deny", note=note)
        typer.echo(f"{ap.get('id', approval_id)} {ap.get('state', 'denied')}")
        return
    try:
        ap = _service().approvals.decide(approval_id, "deny", by=BY, note=note)
    except OpenajarError as e:
        _fail(e)
    typer.echo(f"{ap.id} {ap.state}")


@approvals_app.command("batch")
def approvals_batch(ids: list[str], deny: Annotated[bool, typer.Option("--deny", help="deny instead of approve")] = False,
                    note: Annotated[str | None, typer.Option()] = None) -> None:
    """Approve (or --deny) several approvals at once."""
    res = _service().approvals.batch(ids, "deny" if deny else "approve", by=BY, note=note)
    for i, outcome in res.items():
        typer.echo(f"{i} {outcome}")


@confirmation_app.command("show")
def confirmation_show(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show the setting, the ways to confirm, and the chat clients decisions came through."""
    svc = _service()
    now = svc.confirmation.public()
    methods = svc.confirmation.methods_public()
    clients = svc.approvals.chat_clients()
    if as_json:
        _echo_json({**now, "methods": methods, "chat_clients": clients})
        return
    typer.echo(f"commit           when {now['commit']['when']}, level {now['commit']['level']}")
    for row in ("pay", "irreversible"):
        typer.echo(f"{row:<16} always asks, level {now[row]['level']}")
    typer.echo(f"after_untrusted  level {now['after_untrusted']['level']}")
    typer.echo(f"controller       believed up to {now['controller_max_level']}")
    if now["controller"] is not None:
        typer.echo(f"                 asks for more: what is above is the stricter of this node and {now['controller']['set_by']}")
    typer.echo("")
    for m in methods:
        state = m["status"] if m["status"] == "planned" else ("on" if m["on"] else "off")
        typer.echo(f"{m['id']:<10} {m['level']:<11} {state:<8} {m['label']}")
    if clients:
        typer.echo("")
        typer.echo("chat clients decisions came through (each by its own word):")
        for c in clients:
            typer.echo(f"  {c['client']}: {c['decisions']}, last {c['last']}")


@confirmation_app.command("set")
def confirmation_set(
    key: Annotated[str, typer.Argument(help="commit.when, commit.level, pay.level, irreversible.level, "
                                            "after_untrusted.level, controller_max_level, chat, telegram or "
                                            "publish_in_chat")],
    value: Annotated[str, typer.Argument(help="a level; ask or auto for commit.when; on or off for chat, telegram "
                                              "and publish_in_chat")],
    apply_to_waiting: Annotated[bool, typer.Option(
        "--apply-to-waiting", help="also apply the setting to approvals already waiting, never below a floor")] = False,
) -> None:
    """Change the setting. It counts for new approvals: one that waits keeps what it requires, unless you
    say --apply-to-waiting."""
    svc = _service()
    try:
        if key == "publish_in_chat":
            if value not in ("on", "off"):
                raise InvalidRequest("publish_in_chat is on or off")
            patch: dict = {"publish_in_chat": value == "on"}
        elif key in ("chat", "telegram"):
            if value not in ("on", "off"):
                raise InvalidRequest(f"{key} is on or off")
            off = [m for m in svc.confirmation.own().methods_off if m != key]
            patch = {"methods_off": off + ([key] if value == "off" else [])}
        elif key == "controller_max_level":
            patch = {key: value}
        else:
            row, _, field = key.partition(".")
            if not field:
                raise InvalidRequest("name a field: commit.level, pay.level, ...")
            patch = {row: {field: value}}
        svc.confirmation.change(patch, by=BY, role="owner")
    except OpenajarError as e:
        _fail(e)
    typer.echo(f"{key} is {value}")
    if apply_to_waiting:
        moved = svc.apply_setting_to_waiting(by=BY)
        typer.echo(f"applied to {len(moved)} waiting approval{'' if len(moved) == 1 else 's'}")
    if svc.confirmation.own() != svc.confirmation.current():
        typer.echo("a controller asks for more than this node does, and the stricter of the two holds: "
                   "see openajar confirmation show")


@standing_app.command("add")
def standing_add(
    effect: Annotated[list[str], typer.Option(help="effect it covers, e.g. commit (repeatable)")],
    until: Annotated[str, typer.Option(help="end: 2026-11-07, 2026-11-07T18:00, or 3d")],
    workspace: Annotated[list[str] | None, typer.Option()] = None,
    lane: Annotated[list[str] | None, typer.Option(help="lane pattern, e.g. biz-a/insta*")] = None,
    count: Annotated[int | None, typer.Option(help="total uses")] = None,
    per_day: Annotated[int | None, typer.Option(help="uses per UTC day")] = None,
    grant: Annotated[str | None, typer.Option(help="only for this grant and those narrowed from it")] = None,
    note: Annotated[str | None, typer.Option()] = None,
    target: Annotated[list[str] | None, typer.Option(
        help="what it covers: flow:<id>@<version> or tool:<name> (repeatable; needed for commit)")] = None,
    content: Annotated[list[str] | None, typer.Option(
        help="a content digest the owner approved, sha256:... (repeatable)")] = None,
    from_approval: Annotated[list[str] | None, typer.Option(
        help="cover the same tool or flow, content and lane as this approval (repeatable)")] = None,
) -> None:
    """Pre-approve an effect, e.g. 20 posts to biz-a lanes until Friday. For commit, name the flow
    version or tool and the contents you approved (--from-approval takes them from approvals you
    saw); a flow version with no parameters is a template and needs no contents."""
    svc = _service()
    try:
        targets, contents = list(target or []), list(content or [])
        if from_approval:
            t, c, seen = svc.approvals.binding_from(from_approval)
            targets, contents = targets + t, contents + c
            lane = lane or (None if workspace else seen)
        problem = svc.approvals.binding_problem(effect, targets)
        if problem:
            raise InvalidRequest(problem)
        sa = svc.approvals.add_standing(effects=effect, workspaces=workspace, lanes=lane,
                                        until=parse_until(until, time.time()), count=count, per_day=per_day,
                                        grant_id=grant, by=BY, note=note, targets=targets, contents=contents)
    except OpenajarError as e:
        _fail(e)
    typer.echo(f"standing approval {sa.id}: {', '.join(sa.effects)} on "
               f"{', '.join([*sa.workspaces, *sa.lanes])} until {sa.public()['expires_at']}"
               + (f", {sa.max_uses} uses" if sa.max_uses else ""))


@standing_app.command("list")
def standing_list(all_: Annotated[bool, typer.Option("--all")] = False) -> None:
    """List standing approvals."""
    items = _service().approvals.list_standing(active_only=not all_)
    if not items:
        typer.echo("no standing approvals")
    for s in items:
        p = s.public()
        left = "unlimited" if p["remaining"] is None else f"{p['remaining']} left"
        source = f"from {s.issuer}" if s.issuer else "this node"
        mark = "  (revoked)" if s.revoked_at else ("  (withdrawn by its controller)" if s.withdrawn_at else "")
        typer.echo(f"{s.id}  {','.join(s.effects):<10} {','.join([*s.workspaces, *s.lanes]):<24} {left:<12} "
                   f"until {p['expires_at']}  {source}{mark}")


@standing_app.command("revoke")
def standing_revoke(standing_id: str) -> None:
    """End a standing approval now, also one a controller sent (it is never brought back)."""
    try:
        _service().approvals.revoke_standing(standing_id, by=BY)
    except OpenajarError as e:
        _fail(e)
    typer.echo(f"revoked {standing_id}")


@app.command("routing")
def routing(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show the routing table: which effects run on their own and which wait for you."""
    from openajar.policy.routing import load_policy_config

    home = home_from_env()
    cfg = load_policy_config(home)
    table = cfg.routing
    if (home / "policy.db").exists():  # a person may have changed commit.when since the file was written
        table = table.model_copy(update={"ask_first_default": open_store(home).default})
    if as_json:
        _echo_json({**table.model_dump(), "taint": cfg.taint.model_dump()})
    else:
        typer.echo(table.render())
        typer.echo("after untrusted content, waits for a person: " + (", ".join(cfg.taint.downgrade) or "nothing"))


@app.command("traces")
def traces(lane: Annotated[str | None, typer.Option()] = None, tool: Annotated[str | None, typer.Option()] = None,
           status: Annotated[str | None, typer.Option()] = None, limit: int = 30,
           as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show recent entries of the action log."""
    events, _ = _service().traces.query(lane=lane, tool=tool, status=status, limit=limit)
    if as_json:
        _echo_json([e.public() for e in events])
        return
    for e in events:
        p = e.public()
        typer.echo(f"{p['at']}  {e.status:<8} {e.kind:<11} {e.tool or '-':<20} {e.lane or '-':<20} "
                   f"{e.principal or '-'}  {e.summary or ''}"[:200])


@app.command("key")
def key() -> None:
    """Print the policy public key (for offline verification)."""
    path = home_from_env() / "secrets" / "policy.pub"
    _service()
    typer.echo(path.read_text().strip())


@notify_app.command("show")
def notify_show(member: Annotated[str | None, typer.Option(help="one member's settings, topic included")] = None
                ) -> None:
    """Show notification settings (tokens and secrets hidden)."""
    svc = _service()
    _echo_json(svc.notifier.member_settings(member) if member else svc.notifier.config.public())


@notify_app.command("ntfy")
def notify_ntfy(topic: Annotated[str | None, typer.Argument(help="leave out to generate a random topic")] = None,
                server: Annotated[str, typer.Option(help="best: your own ntfy on the tailnet")] = "https://ntfy.sh",
                token_file: Annotated[str | None, typer.Option("--token-from", help="file or - for stdin")] = None,
                disable: Annotated[bool, typer.Option("--disable")] = False) -> None:
    """Send pushes to an ntfy topic. Anyone who knows a topic on a public server can read it, so
    the topic must be long and random; leave it out and the node generates one."""
    svc = _service()
    patch: dict = {"ntfy": {"topic": topic or "generate", "server": server, "enabled": not disable}}
    if token_file:
        patch["ntfy"]["token"] = _read_token(token_file)
    try:
        cfg = svc.notifier.update_config(patch)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from None
    typer.echo(f"ntfy configured: subscribe to the topic {cfg.ntfy.topic} on {cfg.ntfy.server} in the ntfy app")


@notify_app.command("webhook")
def notify_webhook(url: str, secret_file: Annotated[str, typer.Option("--secret-from", help="file or - for stdin")],
                   disable: Annotated[bool, typer.Option("--disable")] = False) -> None:
    """POST signed JSON (X-Agentos-Signature: sha256=HMAC) to a URL."""
    svc = _service()
    try:
        svc.notifier.update_config({"webhook": {"url": url, "secret": _read_token(secret_file),
                                                "enabled": not disable}})
    except ValueError as e:
        raise typer.BadParameter(str(e)) from None
    typer.echo("webhook configured")


@notify_app.command("urls")
def notify_urls(public_base_url: Annotated[str | None, typer.Option(help="enables Review and Deny buttons")] = None,
                panel_url: Annotated[str | None, typer.Option()] = None) -> None:
    """Set the panel link and the public base URL used in pushes."""
    patch: dict = {}
    if public_base_url is not None:
        patch["public_base_url"] = public_base_url or None
    if panel_url is not None:
        patch["panel_url"] = panel_url
    _service().notifier.update_config(patch)
    typer.echo("saved")


@notify_app.command("hours")
def notify_hours(member: Annotated[str, typer.Option(help="a member id, or owner")] = "owner",
                 quiet: Annotated[str | None, typer.Option(help="21:00-08:00, or off")] = None,
                 digest: Annotated[str | None, typer.Option(help="08:00,13:00: one card at these times, or off")] = None,
                 timezone: Annotated[str | None, typer.Option(help="an IANA zone, for example Europe/Vilnius")] = None,
                 batch: Annotated[str | None, typer.Option(help="collapse pushes within this window: 10m, or 0")] = None,
                 urgent_breaks_quiet: Annotated[bool | None, typer.Option(
                     "--urgent-breaks-quiet/--urgent-waits", help="may an urgent push wake this person")] = None,
                 defaults: Annotated[bool, typer.Option("--defaults", help="change the defaults for new members")] = False
                 ) -> None:
    """Quiet hours, digest times and batching for the owner, a member or the member defaults.
    Inside quiet hours nothing is pushed unless it is urgent (the run would go stale before the
    morning) and allowed; what waited goes out as one card when quiet hours end."""
    patch: dict = {}
    if quiet is not None:
        patch["quiet_hours"] = None if quiet == "off" else [t.strip() for t in quiet.split("-")]
    if digest is not None:
        patch["digest_at"] = [] if digest == "off" else [t.strip() for t in digest.split(",") if t.strip()]
    if timezone is not None:
        patch["timezone"] = timezone
    if batch is not None:
        try:
            patch["batch_window_s"] = parse_duration(int(batch) if batch.isdigit() else batch)
        except OpenajarError as e:
            raise typer.BadParameter(e.message) from None
    if urgent_breaks_quiet is not None:
        patch["urgent_breaks_quiet"] = urgent_breaks_quiet
    svc = _service()
    try:
        if defaults:
            out = svc.notifier.update_config({"member_defaults": patch}).member_defaults.model_dump()
        else:
            out = svc.notifier.update_member(member, patch)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from None
    _echo_json({k: v for k, v in out.items() if k != "topic"})


@notify_app.command("member")
def notify_member(member: str,
                  channel: Annotated[str | None, typer.Option(help="ntfy, webhook or none")] = None,
                  server: Annotated[str | None, typer.Option(help="their ntfy server (default: the node's)")] = None,
                  webhook_url: Annotated[str | None, typer.Option()] = None,
                  secret_file: Annotated[str | None, typer.Option("--secret-from", help="file or - for stdin")] = None,
                  show_summary: Annotated[bool | None, typer.Option(
                      "--show-summary/--no-summary", help="put the approval summary in their pushes")] = None
                  ) -> None:
    """Give a member their own push channel (new members get a generated ntfy topic and the
    default hours). Prints the topic to subscribe to on their phone."""
    patch: dict = {}
    if channel is not None:
        patch["channel"] = channel
    if server is not None:
        patch["server"] = server or None
    if webhook_url is not None or secret_file is not None:
        patch["webhook"] = {k: v for k, v in (("url", webhook_url),
                                               ("secret", _read_token(secret_file) if secret_file else None)) if v}
    if show_summary is not None:
        patch["show_summary"] = show_summary
    try:
        out = _service().notifier.update_member(member, patch)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from None
    if out["channel"] == "ntfy":
        typer.echo(f"{member}: subscribe to the topic {out['topic']} in the ntfy app")
    else:
        typer.echo(f"{member}: channel {out['channel']}")


@notify_app.command("rotate")
def notify_rotate(member: Annotated[str, typer.Argument(help="a member id, or owner")] = "owner") -> None:
    """Give a member (or the owner) a new random topic; nothing is pushed to the old one again."""
    try:
        topic = _service().notifier.rotate_topic(member)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from None
    typer.echo(f"{member}: subscribe to the new topic {topic}; the old one is retired")


@notify_app.command("remove")
def notify_remove(member: str) -> None:
    """Stop all pushes to a member: their topic is retired and what waited for them is dropped."""
    try:
        dropped = _service().notifier.remove_member(member)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from None
    typer.echo(f"{member}: no more pushes ({dropped} waiting dropped)")


@notify_app.command("test")
def notify_test(member: Annotated[str, typer.Option(help="a member id, or owner")] = "owner") -> None:
    """Send a test notification on every enabled channel now."""
    res = asyncio.run(_service().notifier.test(member))
    if not res:
        typer.echo("no notification channels are enabled")
        raise typer.Exit(1)
    for ch, outcome in res.items():
        typer.echo(f"{ch}: {outcome}")
    if any(not v.startswith("delivered") for v in res.values()):
        raise typer.Exit(1)


@telegram_app.command("set-token")
def telegram_set_token(token_file: Annotated[str | None, typer.Option(
        "--token-from", help="file or - for stdin; without it the token is asked for, hidden")] = None) -> None:
    """Give the node its own Telegram bot (make one with @BotFather). The token is read from a prompt, a
    file or stdin, never from the command line. A new token starts unpaired."""
    from openajar.policy.telegram import TelegramError

    token = _read_token(token_file) if token_file else typer.prompt("Bot token from BotFather", hide_input=True)
    svc = _service()
    try:
        bot = asyncio.run(svc.telegram.set_token(token))
    except (ValueError, TelegramError) as e:
        typer.echo(f"The token was not saved: {e}", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"Saved the token of @{bot}. Next: openajar policy notify telegram pair")
    said = svc.telegram.status().get("last_error")
    if said:  # the webhook could not be removed: reading the bot will fail until it is
        typer.echo(f"warning: {said}", err=True)


@telegram_app.command("pair")
def telegram_pair() -> None:
    """Print a link that pairs your Telegram account with this node: it works once, for 10 minutes."""
    svc = _service()
    try:
        code = svc.telegram.start_pairing()
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from None
    typer.echo("Open this link on your phone within 10 minutes and tap Start:")
    typer.echo(f"  {svc.telegram.pair_link(code)}")
    typer.echo("The node must be running: it reads your answer from Telegram. Then check with: "
               "openajar policy notify telegram status")


@telegram_app.command("status")
def telegram_status(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """The bot, the paired account, and the last error reading Telegram. Never the token."""
    from openajar.policy.telegram import describe

    status = _service().telegram.status()
    typer.echo(json.dumps(status, indent=2) if as_json else describe(status))


@telegram_app.command("remove")
def telegram_remove() -> None:
    """Forget the bot and the paired account: nothing more goes to Telegram, and no tap is taken."""
    _service().telegram.remove()
    typer.echo("Telegram is removed from this node. If you no longer use the bot, revoke its token with "
               "@BotFather (/revoke).")
