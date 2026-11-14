"""`agentos mail connect|list|test|remove`.

Whoever runs the CLI on the node is a person. The password is asked for hidden, here, and goes from
this process to the two servers and the vault: it is never an argument, and it never passes the
daemon. The daemon reads what is saved here from disk, as for `agentos vault add`.
"""

from __future__ import annotations

import asyncio
import getpass
from typing import Any

import typer

from agentos.core.config import home_from_env
from agentos.core.errors import AgentosError
from agentos.runner.mail import connect as connecting
from agentos.runner.mail.discovery import SECURITY, Found, Server
from agentos.runner.mail.form import SOURCES
from agentos.runner.mail.net import RealNet
from agentos.runner.mail.session import MailFailed, explain
from agentos.runner.workflows.cli import _svc
from agentos.runner.workflows.mail import SendRefused, SignInFailed
from agentos.vault.secret import Secret

app = typer.Typer(no_args_is_help=True, help="Mailboxes on this node: connect one in a minute, mail in and out.")
NET = RealNet


def _ask(prompt: str) -> str:
    return getpass.getpass(prompt)


def _fail(message: str) -> typer.Exit:
    typer.echo(message, err=True)
    return typer.Exit(1)


async def _vault() -> Any:
    from agentos.vault.errors import VaultSealed
    from agentos.vault.service import VaultService

    vault = VaultService(home_from_env())
    try:
        await vault.ensure_unsealed()
    except VaultSealed:
        await vault.unseal(_ask("Vault passphrase: "))
    return vault


def _line(server: Server) -> str:
    return f"{server.host}:{server.port} ({'TLS' if server.security == 'tls' else 'required STARTTLS'})"


def _show(found: Found) -> None:
    typer.echo(f"Reading: {_line(found.imap)}")
    typer.echo(f"Sending: {_line(found.smtp)}")
    typer.echo(f"Found by: {SOURCES.get(found.source, 'what you typed')}")
    for note in found.notes:
        typer.echo(note)
    if found.words:
        typer.echo(f"\n{found.words}")


def _typed(host: str | None, port: int | None, security: str | None) -> dict[str, Any] | None:
    if host is None and port is None and security is None:
        return None
    return {"host": host, "port": port, "security": security}


async def _connect(address: str, mailbox: str, imap: dict[str, Any] | None, smtp: dict[str, Any] | None) -> None:
    found = None if imap and imap.get("host") and smtp and smtp.get("host") else \
        await connecting.discover_quietly(address, NET())
    if found is None and not (imap and smtp):
        raise _fail("No mail servers were found. Type them in with --imap-host, --imap-port, --smtp-host and --smtp-port.")
    found = connecting.apply_typed(found, address, imap=imap, smtp=smtp)
    _show(found)
    confirmed = False
    if found.needs_yes:
        typer.echo("\nOnly DNS or a guess named these servers, and a false record could point anywhere.")
        confirmed = typer.confirm("Are these your mail servers?", default=False)
        if not confirmed:
            raise _fail("Nothing was sent, and nothing was saved. Type the right ones in with --imap-host and --smtp-host.")
    svc = _svc()
    svc.mail.vault = await _vault()
    word = "App password" if found.needs_app_password else "Password"
    secret = Secret(_ask(f"{word} for {found.address}: "))
    box = await connecting.connect(svc.mail, found, secret, mailbox_id=mailbox, confirmed=confirmed, in_person=True)
    typer.echo(f"{box.address} is connected as {box.id}. Both sign-ins worked. Nothing was read, and nothing was sent.")


@app.command("connect")
def connect(address: str,
            mailbox: str = typer.Option("personal/mail", "--id", help="The id it gets, like a lane id: harbor/mail."),
            imap_host: str = typer.Option(None, "--imap-host", help="Use this server, as typed."),
            imap_port: int = typer.Option(None, "--imap-port"),
            imap_security: str = typer.Option(None, "--imap-security", help=f"One of {', '.join(SECURITY)}."),
            smtp_host: str = typer.Option(None, "--smtp-host", help="Use this server, as typed."),
            smtp_port: int = typer.Option(None, "--smtp-port"),
            smtp_security: str = typer.Option(None, "--smtp-security", help=f"One of {', '.join(SECURITY)}.")) -> None:
    """Find a mailbox's servers, show them, ask for the password (hidden), and sign in to both."""
    imap, smtp = _typed(imap_host, imap_port, imap_security), _typed(smtp_host, smtp_port, smtp_security)
    try:
        asyncio.run(_connect(address, mailbox, imap, smtp))
    except AgentosError as e:
        raise _fail(e.message) from None


@app.command("list")
def list_() -> None:
    """List the mailboxes and how each is doing."""
    svc = _svc()
    boxes = svc.mail.list()
    if not boxes:
        typer.echo("No mailboxes yet. Add one with `agentos vault mail connect ADDRESS`.")
        return
    for box in boxes:
        health = svc.mail.health(box.id)
        state = "not checked yet" if health is None else "ok" if health["ok"] else health["words"]
        typer.echo(f"{box.id}  {box.address}  {box.imap_host}, {box.smtp_host}  {state}")


async def _test(mailbox: str) -> None:
    svc = _svc()
    box = svc.mail.require(mailbox)
    svc.mail.vault = await _vault()
    try:
        await svc.mail.test(box)
    except SignInFailed as e:
        words = explain(e.stage, box.imap_host if e.stage == "imap" else box.smtp_host, e.kind)
        svc.mail.note_health(box.id, False, words)
        raise _fail(words) from None
    except (MailFailed, SendRefused) as e:
        raise _fail(str(e)) from None
    svc.mail.note_health(box.id, True)
    typer.echo(f"{box.id}: both sign-ins work. Nothing was read, and nothing was sent.")


@app.command("test")
def test(mailbox: str) -> None:
    """Sign in to both servers again, reading nothing and sending nothing."""
    try:
        asyncio.run(_test(mailbox))
    except AgentosError as e:
        raise _fail(e.message) from None


async def _remove(mailbox: str) -> None:
    svc = _svc()
    svc.mail.require(mailbox)
    svc.mail.vault = await _vault()
    await svc.mail.remove(mailbox, person=True)


@app.command("remove")
def remove(mailbox: str, yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask.")) -> None:
    """Remove a mailbox and its password from this node."""
    if not yes and not typer.confirm(f"Remove {mailbox} and its password?", default=False):
        raise typer.Exit(1)
    try:
        asyncio.run(_remove(mailbox))
    except AgentosError as e:
        raise _fail(e.message) from None
    typer.echo(f"{mailbox} is removed, and its password with it.")
