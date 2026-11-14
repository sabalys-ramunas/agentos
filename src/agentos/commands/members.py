"""`agentos members`: people who sign in to the panel (invite, list, roles, remove)."""

from __future__ import annotations

from typing import Annotated

import typer

from agentos.commands.base import daemon_request, fail, initialized_config
from agentos.core.config import (
    NodeConfig,
)

members_app = typer.Typer(no_args_is_help=True, help="People who sign in to the panel: invite, list, roles, remove.")


MEMBER_ROLES = ("admin", "approver", "operator", "viewer", "auditor")
ROLE_HELP = ("A role (admin, approver, operator, viewer, auditor), or role@workspace. Repeat it for several; a "
             "bare role applies to every --workspace.")


RoleOpt = Annotated[list[str], typer.Option("--role", "-r", help=ROLE_HELP)]
WorkspaceOpt = Annotated[list[str] | None, typer.Option("--workspace", "-w", help=(
    "A workspace the bare --role values apply to (repeat it; '*' is every workspace)."))]
TtlOpt = Annotated[str | None, typer.Option(help="How long the access lasts: 12h, 90d, 4w (default: until removed).")]
WatchOpt = Annotated[bool, typer.Option("--watch", help="Viewers and approvers may watch the live view too.")]
MaxUsdOpt = Annotated[float | None, typer.Option("--max-usd", min=0, help="Approvers may approve payments up to this.")]


def member_roles(roles: list[str], workspaces: list[str] | None, *, watch: bool, max_usd: float | None) -> list[dict]:
    """--role approver --workspace biz-c (or --role approver@biz-c) as the API's role list."""
    out: list[dict] = []
    for item in roles:
        role, _, scope = item.partition("@")
        role = role.strip().lower()
        if role not in MEMBER_ROLES:
            raise fail(f"{role or item!r} is not a role; use one of {', '.join(MEMBER_ROLES)}")
        scopes = [scope.strip()] if scope else list(workspaces or [])
        if not scopes:
            raise fail(f"say where: --workspace biz-c, or --role {role}@biz-c")
        for ws in scopes:
            options: dict = {}
            if watch and role in ("viewer", "approver"):
                options["watch"] = True
            if max_usd is not None and role == "approver":
                options["max_usd"] = max_usd
            out.append({"scope": ws, "role": role, "options": options})
    return out


def role_words(roles: list[dict]) -> str:
    if not roles:
        return "no roles"
    words = []
    for r in roles:
        where = "every workspace" if r.get("scope") == "*" else r.get("scope")
        extra = []
        opts = r.get("options") or {}
        if opts.get("watch"):
            extra.append("watches")
        if opts.get("max_usd") is not None:
            extra.append(f"pay up to ${opts['max_usd']:g}")
        if r.get("expires_at"):
            extra.append(f"until {str(r['expires_at'])[:10]}")
        words.append(f"{r.get('role')} in {where}" + (f" ({', '.join(extra)})" if extra else ""))
    return ", ".join(words)


def find_member(cfg: NodeConfig, who: str) -> dict:
    for m in daemon_request(cfg, "GET", "/api/members", missing=MEMBERS_MISSING):
        if who in (m.get("handle"), m.get("id")):
            return m
    raise fail(f"no member {who}; `agentos members list` shows who is on this node")


MEMBERS_MISSING = "this agentosd has no members yet; update it and restart the service"


def show_qr(link: str, qr: bool | None) -> None:
    import sys

    from agentos import qr as qrcode

    if qr is False or (qr is None and not sys.stdout.isatty()):
        return
    try:
        typer.echo(qrcode.render(link), color=True)  # black on white even when asked for off a terminal
    except qrcode.QrTooLong:
        typer.echo("(the link is too long for a QR code here; send the link itself)")
    typer.echo("")


@members_app.command("list")
def members_list(as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False) -> None:
    """Who signs in to this node, with their roles, and the invites not used yet."""
    import json

    cfg = initialized_config()
    people = daemon_request(cfg, "GET", "/api/members", missing=MEMBERS_MISSING)
    invites = daemon_request(cfg, "GET", "/api/invites", missing=MEMBERS_MISSING)
    if as_json:
        typer.echo(json.dumps({"members": people, "invites": invites}, indent=2))
        return
    width = max([len(m.get("handle", "")) for m in people] + [6])
    for m in people:
        state = "" if m.get("state") == "active" else f"  [{m.get('state')}]"
        devices = m.get("devices", 0)
        typer.echo(f"{m.get('handle', '?'):<{width}}  {role_words(m.get('roles') or [])}  "
                   f"({devices} passkey{'' if devices == 1 else 's'}){state}")
    if invites:
        typer.echo("\nInvites not used yet:")
        for i in invites:
            typer.echo(f"  {i.get('handle'):<{width}}  {role_words(i.get('roles') or [])}  "
                       f"(link expires {str(i.get('expires_at', ''))[:16].replace('T', ' ')} UTC)")


@members_app.command("invite")
def members_invite(
    handle: Annotated[str, typer.Argument(help="Their short name in Activity: va, jonas, client-ostra.")],
    role: RoleOpt,
    workspace: WorkspaceOpt = None,
    ttl: TtlOpt = None,
    name: Annotated[str | None, typer.Option(help="Their name, shown next to their handle.")] = None,
    email: Annotated[str | None, typer.Option(help="Kept as a note; the node sends no email.")] = None,
    watch: WatchOpt = False,
    max_usd: MaxUsdOpt = None,
    qr: Annotated[bool | None, typer.Option("--qr/--no-qr", help="Print a QR code (default: on a terminal).")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the invite as JSON (the link included).")] = False,
) -> None:
    """Invite a person: prints a link that works once, for 24 hours, and a QR code for their phone."""
    import json

    cfg = initialized_config()
    body = {"handle": handle, "roles": member_roles(role, workspace, watch=watch, max_usd=max_usd),
            "display_name": name, "email": email, "ttl": ttl}
    invite = daemon_request(cfg, "POST", "/api/invites", body, missing=MEMBERS_MISSING, person=True)
    if as_json:
        typer.echo(json.dumps(invite, indent=2))
        return
    lasting = f", for {ttl}" if ttl else ""
    typer.echo(f"Invite for {invite.get('handle')}: {role_words(invite.get('roles') or [])}{lasting}.")
    typer.echo("Send them this link. It works once, for 24 hours, and is not shown again:\n")
    typer.echo(f"  {invite.get('link')}\n")
    show_qr(str(invite.get("link")), qr)
    typer.echo("They open it on their phone, make a passkey, and see only what these roles allow.")
    typer.echo("Changed your mind? Revoke it in the panel (Settings, Members).")


@members_app.command("role")
def members_role(
    who: Annotated[str, typer.Argument(help="Their handle (or member id).")],
    role: Annotated[list[str] | None, typer.Option("--role", "-r", help=ROLE_HELP)] = None,
    workspace: WorkspaceOpt = None,
    none: Annotated[bool, typer.Option("--none", help="Take every role away in the --workspace values.")] = False,
    ttl: TtlOpt = None,
    watch: WatchOpt = False,
    max_usd: MaxUsdOpt = None,
) -> None:
    """Change someone's roles in the workspaces named; their other workspaces stay as they are.

    The node re-mints their member grants; the change applies at their next click."""
    cfg = initialized_config()
    if none == bool(role):
        raise fail("give the new roles with --role, or --none with --workspace to take them away")
    roles = [] if none else member_roles(role or [], workspace, watch=watch, max_usd=max_usd)
    scopes = list(dict.fromkeys([*(workspace or []), *(r["scope"] for r in roles)]))
    if not scopes:
        raise fail("say where: --workspace biz-c")
    m = find_member(cfg, who)
    body: dict = {"roles": roles, "scopes": scopes}
    if ttl:
        body["ttl"] = ttl
    updated = daemon_request(cfg, "PATCH", f"/api/members/{m['id']}", body, missing=MEMBERS_MISSING, person=True)
    typer.echo(f"{updated.get('handle')}: {role_words(updated.get('roles') or [])}. It applies at their next click.")


@members_app.command("remove")
def members_remove(
    who: Annotated[str, typer.Argument(help="Their handle (or member id).")],
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask.")] = False,
) -> None:
    """Remove a person: sessions end, passkeys and member grants go, their agents' grants are revoked."""
    cfg = initialized_config()
    m = find_member(cfg, who)
    if m.get("owner"):
        raise fail("the owner cannot be removed")
    if not yes:
        typer.confirm(f"Remove {m.get('handle')} ({role_words(m.get('roles') or [])})? They lose access at once, "
                      "on every device, and the agents they connected stop working", abort=True)
    r = daemon_request(cfg, "DELETE", f"/api/members/{m['id']}", missing=MEMBERS_MISSING)
    agents = len(r.get("agent_grants") or [])
    sessions = r.get("sessions_ended", 0)
    typer.echo(f"Removed {m.get('handle')}: {sessions} session{'' if sessions == 1 else 's'} ended, "
               f"{len(r.get('grants') or [])} member grant(s) and {agents} agent grant(s) revoked, "
               "their push topic retired.")
