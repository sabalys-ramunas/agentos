"""`openajar environments secret ...`: API keys the app in an environment uses, with the value kept on
the node (openajar.environments.appsecrets). The owner runs these at the node; no tool sets one.

    openajar environments secret add STRIPE_SECRET_KEY --host api.stripe.com [--repo github.com/me/shop]
    openajar environments secret list
    openajar environments secret remove STRIPE_SECRET_KEY [--repo github.com/me/shop]

The value is read from a prompt (or stdin with --stdin), never from the command line.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import sys
from typing import Annotated, Any

import typer

from openajar.core import catalog
from openajar.core.errors import InvalidRequest, OpenajarError

secret_app = typer.Typer(no_args_is_help=True, help=(
    "API keys the app in an environment uses: the machine holds a placeholder, and the node puts the value "
    "in for the bound host only."))


def _fail(e: OpenajarError) -> typer.Exit:
    typer.echo(catalog.cli_error(e.code, e.message), err=True)
    return typer.Exit(1)


def _vault() -> Any:
    from openajar.commands.base import cli_runtime

    vault = cli_runtime().get("vault")
    if vault is None:
        raise _fail(InvalidRequest("this node has no vault yet; run `openajar vault init` first"))
    return vault


def _run(coro: Any) -> Any:
    try:
        return asyncio.run(coro)
    except OpenajarError as e:
        raise _fail(e) from None


def _check(var: str, hosts: list[str], repo: str | None) -> tuple[str, list[str], str | None]:
    from openajar.environments.network import check_host
    from openajar.vault.models import ENV_VAR_RE

    if not ENV_VAR_RE.match(var):
        raise _fail(InvalidRequest(f"{var!r} is not a variable name: capitals, digits and _, like STRIPE_SECRET_KEY"))
    out = []
    for h in hosts:
        try:
            host = check_host(h)
        except ValueError as e:
            raise _fail(InvalidRequest(str(e))) from None
        if host.startswith((".", "*")) or ":" in host:
            raise _fail(InvalidRequest(f"--host is one host name, like api.stripe.com (not {h!r})"))
        out.append(host)
    if repo:
        from openajar.git.wire import parse_remote

        try:
            repo = parse_remote(repo).slug
        except OpenajarError as e:
            raise _fail(e) from None
    return var, out, repo


@secret_app.command("add")
def add(var: Annotated[str, typer.Argument(metavar="NAME", help="The variable the app reads, like STRIPE_SECRET_KEY.")],
        host: Annotated[list[str], typer.Option("--host", help="The host it goes to, like api.stripe.com (repeat for "
                                                "more hosts of the same domain).")],
        repo: Annotated[str | None, typer.Option("--repo", help="Only environments of this repository, like "
                                                 "github.com/me/shop (default: every environment).")] = None,
        stdin: Annotated[bool, typer.Option("--stdin", help="Read the value from stdin instead of a prompt.")] = False,
        replace: Annotated[bool, typer.Option("--replace", help="Replace a secret of this name.")] = False) -> None:
    """Bind an API key to environments: the machine gets a placeholder in NAME, and requests to the host get
    the value, put in by the node. Storing it is the permission: nothing asks when it is used."""
    from openajar.environments import appsecrets

    var, hosts, repo = _check(var, host, repo)
    vault = _vault()
    raw = sys.stdin.readline() if stdin else getpass.getpass(f"Value of {var} (not shown): ")
    value = raw.strip()
    if not value:
        raise _fail(InvalidRequest("the value is empty; nothing was stored"))

    async def go() -> Any:
        unsealed = getattr(vault, "_unsealed", None)
        if callable(unsealed):
            await unsealed()
        return await appsecrets.add(vault, var, value, hosts, repo=repo, replace=replace)

    _run(go())
    scope = f"environments of {repo}" if repo else "every environment"
    typer.echo(f"Bound {var} to {', '.join(hosts)} for {scope}. New environments get a placeholder in {var}; "
               "the node puts the value in for those hosts only. Open ones get it when they are opened again.")
    typer.echo(f"Hosts a secret is bound to are on every environment's network list: limited and none reach "
               f"{', '.join(hosts)} too.")


@secret_app.command("list")
def list_cmd(json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """The secrets bound to environments: names, hosts and repositories, never a value."""
    from openajar.environments import appsecrets

    rows = [{"name": b.var, "hosts": list(b.hosts), "repo": b.repo}
            for b in _run(appsecrets.all_bound(_vault()))]
    if json_out:
        typer.echo(json.dumps({"secrets": rows}, indent=2))
        return
    if not rows:
        typer.echo("No secret is bound to environments. Bind one: openajar environments secret add NAME --host HOST")
        return
    for r in rows:
        typer.echo(f"{r['name']:<28} {', '.join(r['hosts']):<36} {r['repo'] or 'every environment'}")


@secret_app.command("remove")
def remove(var: Annotated[str, typer.Argument(metavar="NAME")],
           repo: Annotated[str | None, typer.Option("--repo", help="The one bound for this repository.")] = None
           ) -> None:
    """Remove a secret from the vault and from environments (new ones no longer get it)."""
    from openajar.environments import appsecrets

    var, _hosts, repo = _check(var, [], repo)
    _run(appsecrets.remove(_vault(), var, repo=repo))
    typer.echo(f"Removed {var}{' for ' + repo if repo else ''}. Machines still open keep its placeholder, and "
               "within a minute the node no longer puts a value in for it.")
