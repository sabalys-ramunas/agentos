"""`agentos site-api openapi` and `agentos site-api keys`: the owner's side of /v1/sites, at the node.

Root on the node is the owner in person, as for `agentos grants issue`: the key and its grant are made
here, straight in their databases, and agentosd sees them on the next call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from agentos.core.config import home_from_env
from agentos.core.errors import AgentosError
from agentos.runner.siteapis import cli
from agentos.runner.siteapis import openapi as oas

keys_app = typer.Typer(no_args_is_help=True, help="Keys for /v1/sites. A key is shown once, when it is made.")

ApiNames = Annotated[list[str], typer.Option("--api", help="a site API this key may call (repeat for more)")]
Endpoints = Annotated[list[str] | None, typer.Option("--endpoint", help="only this endpoint (repeat for more)")]
Surfaces = Annotated[list[str] | None, typer.Option("--surface", help="only this account or page (repeat "
                                                                      "for more)")]


def _runner() -> Any:
    from agentos.runner.cli import _service as runner_service

    return runner_service()


def _keys() -> Any:
    from agentos.policy.cli import _service as policy_service
    from agentos.runner.siteapis.http import catalog_of
    from agentos.runner.siteapis.http_store import HttpStore
    from agentos.runner.siteapis.keys import SiteKeys

    runner = _runner()
    return SiteKeys(HttpStore(home_from_env()), policy_service(), lambda: catalog_of(runner.flows, runner))


def openapi_cmd(api: cli.ApiArg,
                server: Annotated[str | None, typer.Option("--server", help="the URL callers reach the node on")] = None,
                out: Annotated[Path | None, typer.Option("--out", help="write it to this file")] = None) -> None:
    """Print the OpenAPI 3.1 document of a site API, with every endpoint and surface."""
    from agentos.runner.connectors.tools import _surfaces_for
    from agentos.runner.siteapis.http import local_flows

    try:
        runner = _runner()
        eps = local_flows(runner.flows).get(api)
        if not eps:
            raise oas.InvalidRequest(f"no active site API {api} on this node")
        meta = next(iter(eps.values())).definition.connector or {}
        surfaces = sorted({s.id for fv in eps.values() for s in _surfaces_for(runner, fv, lambda _s: True)})
        doc = oas.document(api=api, title=api, version=str(meta.get("version")), endpoints=eps, surfaces=surfaces,
                           server=server)
    except AgentosError as e:
        raise cli._fail(e) from None
    text = json.dumps(doc, indent=2)
    if out is not None:
        out.write_text(text + "\n")
        typer.echo(f"wrote {out}")
    else:
        typer.echo(text)


@keys_app.command("add")
def add(label: Annotated[str, typer.Argument(help="who or what uses it, like 'billing sync'")], apis: ApiNames,
        endpoints: Endpoints = None, surfaces: Surfaces = None,
        ceiling: Annotated[str, typer.Option("--ceiling", help="read, draft or commit")] = "read",
        rate: Annotated[int, typer.Option("--rate", help="calls per minute")] = 60,
        expires: Annotated[str | None, typer.Option("--expires", help="an ISO 8601 time with a timezone")] = None) -> None:
    """Make a key. Its text is printed here and nowhere else, ever."""
    try:
        key, secret = _keys().add(label=label, apis=apis, endpoints=endpoints or None, surfaces=surfaces or None,
                                  ceiling=ceiling, rate_per_min=rate, expires_at=expires, by=cli.OWNER)
    except AgentosError as e:
        raise cli._fail(e) from None
    typer.echo(f"key {key.id} ({key.prefix}), ceiling {key.ceiling}, {key.rate_per_min} calls a minute")
    typer.echo("This is the only time the key is shown:")
    typer.echo(secret)


@keys_app.command("list")
def list_keys(as_json: cli.JsonOpt = False) -> None:
    """Every key, by label and prefix."""
    try:
        svc = _keys()
        rows = [svc.public(k) for k in svc.all()]
    except AgentosError as e:
        raise cli._fail(e) from None
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        typer.echo("no keys yet (agentos site-api keys add)")
    for r in rows:
        typer.echo(f"{r['id']}  {r['prefix']}  {r['state']}  {r['label']}  {', '.join(r['apis'])}  {r['ceiling']}")


@keys_app.command("revoke")
def revoke(key_id: Annotated[str, typer.Argument(help="the key's id, from keys list")]) -> None:
    """Revoke a key and its grant. Its next call answers 401."""
    try:
        key = _keys().revoke(key_id, by=cli.OWNER)
    except AgentosError as e:
        raise cli._fail(e) from None
    typer.echo(f"revoked {key.id} ({key.prefix})")


cli.COMMANDS.append(("openapi", openapi_cmd))
cli.GROUPS.append(("keys", keys_app))
