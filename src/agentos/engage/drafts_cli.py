"""`agentos engage drafts` and `agentos engage provider ...`: the drafts of an account and the
draft provider, for the owner on the node (agentos.engage.drafts).

    agentos engage drafts biz-a/reddit [--status used]
    agentos engage provider test [--surface biz-a/reddit] [--kind comment]
    agentos engage provider token [--stdin]

`provider test` sends one request and prints how many candidates came back and how long it took,
never the token and never the candidates' text. `provider token` stores the bearer token in the
vault (it is read from the terminal or stdin, never from the command line).
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Annotated, Any

import typer

from agentos.core import catalog
from agentos.core.errors import AgentosError

provider_app = typer.Typer(no_args_is_help=True, help="The draft provider: where comments and posts get their words.")


def _fail(message: str, code: str = "invalid_request") -> None:
    typer.secho(catalog.cli_error(code, message), err=True, fg=typer.colors.RED)
    raise typer.Exit(1)


def _one_line(text: str, width: int = 70) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[:width - 3] + "..."


def drafts(surface: Annotated[str, typer.Argument(help="The account, e.g. biz-a/reddit.")],
           status: Annotated[str | None, typer.Option("--status", help="Only drafts in this status.")] = None,
           as_json: Annotated[bool, typer.Option("--json", help="Print JSON instead of text.")] = False) -> None:
    """The drafts of an account: what the provider wrote, what a session used, and the alternatives."""
    from agentos.engage.cli import _service

    with _service() as svc:
        found = svc.drafts.list(surface, status=status)
    if as_json:
        typer.echo(json.dumps(found, indent=2, default=str))
        return
    if not found:
        typer.echo(f"{surface}: no drafts.")
        return
    batches: dict[str, list[dict[str, Any]]] = {}
    for d in found:
        batches.setdefault(d["batch"], []).append(d)
    for rows in batches.values():
        first = rows[0]
        typer.echo(f"{first['created_at']}  {first['kind']}  from {first['provider']}")
        for d in rows:
            run = f"  run {d['run_id']}" if d["run_id"] else ""
            approval = f"  approval {d['approval_id']}" if d["approval_id"] else ""
            typer.echo(f"  {d['id']}  {d['status']}{run}{approval}")
            typer.echo(f"      {_one_line(d['text'])}")


@provider_app.command("test")
def provider_test(surface: Annotated[str | None, typer.Option("--surface", help="Ask as this account's approved "
                                                                                "program would.")] = None,
                  kind: Annotated[str, typer.Option("--kind", help="comment or post.")] = "comment",
                  as_json: Annotated[bool, typer.Option("--json", help="Print JSON instead of text.")] = False) -> None:
    """Send the draft provider one request: how many candidates came back, how fast (never the text)."""
    from agentos.cli import cli_runtime
    from agentos.engage.drafts import (
        DraftRequest,
        HttpDraftProvider,
        ProviderUnavailable,
        provider_from_config,
        request_for,
    )

    runtime = cli_runtime()
    config = runtime.get("config")
    try:
        provider = provider_from_config(config, vault=runtime.get("vault"))
    except AgentosError as e:
        _fail(e.message, e.code)
    if provider is None:
        _fail("no draft provider is configured: set engage_draft_provider in node.toml (passthrough or http)",
              "unavailable")
    if surface is not None:
        from agentos.engage.cli import _service

        with _service() as svc:
            pv = svc.store.active(surface)
        if pv is None:
            _fail(f"{surface} has no approved program")
        request = request_for(pv.definition, kind)
    else:
        request = DraftRequest("test/test", kind, ("test",), ("en",), 140)
    t0 = time.monotonic()
    try:
        got = asyncio.run(provider.candidates(request))
        error = None
    except ProviderUnavailable as e:
        got, error = [], e
    except Exception as e:  # noqa: BLE001  (a provider bug is reported, never a traceback with its answer)
        got, error = [], ProviderUnavailable("error", f"the draft provider failed ({type(e).__name__})")
    ms = int((time.monotonic() - t0) * 1000)
    http = isinstance(provider, HttpDraftProvider)
    token = provider.token_ref if http else None
    out = {"provider": provider.name, "ok": error is None, "candidates": len(got), "asked": request.n,
           "max_length": request.max_chars, "ms": ms, **({"token": token} if http else {}),
           **({"reason": error.reason, "message": str(error)} if error else {})}
    if as_json:
        typer.echo(json.dumps(out, indent=2))
    else:
        if error is None:
            typer.echo(f"{provider.name}: {len(got)} of {request.n} candidates in {ms} ms.")
        else:
            typer.echo(f"{provider.name}: unavailable after {ms} ms ({error.reason}): {error}")
        if http and token is None and (error is None or error.reason not in ("no_token", "no_vault", "vault")):
            typer.echo("No token is configured, so the request carried none. Store one with: "
                       "agentos engage provider token")
        elif token is not None:
            typer.echo(f"The request carried the token from the vault ({token}).")
    if error is not None:
        raise typer.Exit(1)


@provider_app.command("token")
def provider_token(stdin: Annotated[bool, typer.Option("--stdin", help="Read the token from stdin.")] = False) -> None:
    """Store the draft provider's bearer token in the vault (under engage_draft_secret_ref)."""
    from agentos.cli import cli_runtime
    from agentos.engage.drafts import DEFAULT_SECRET_REF

    runtime = cli_runtime()
    vault = runtime.get("vault")
    if vault is None:
        _fail("this node has no vault; create one with agentos vault init", "unavailable")
    ref = runtime.get("config").engage_draft_secret_ref or DEFAULT_SECRET_REF
    token = sys.stdin.read().strip() if stdin else typer.prompt("Draft provider token", hide_input=True).strip()
    if not token or any(c in token for c in "\r\n\x00"):
        _fail("the token must be one line of text")
    try:
        asyncio.run(vault.put_secret(ref, token, kind="api_key", label="engage draft provider"))
    except AgentosError as e:
        _fail(e.message, e.code)
    typer.echo(f"Stored the draft provider's token in the vault as {ref}. Requests carry it from now on.")


def register(app: typer.Typer) -> None:
    app.command("drafts")(drafts)
    app.add_typer(provider_app, name="provider")
