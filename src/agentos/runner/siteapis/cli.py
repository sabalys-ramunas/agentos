"""`agentos site-api discover|new|try|activate|pause|list|show|remove`: the owner's commands.

    agentos site-api discover harbor/supplier https://portal.northsupply.example/orders
    agentos site-api new harbor-supplier --url https://portal.northsupply.example/orders --surface harbor/supplier
    agentos site-api new --file harbor.json
    agentos site-api try harbor-supplier open_orders -p status=late
    agentos site-api activate harbor-supplier

Whoever has a shell on the node is the owner, as for `agentos connector`. `discover`, `new --url` and
`try` need the lane's browser, so agentosd does them over the ops socket; the others work on this
node's runner database.

Another module adds commands by appending to COMMANDS or GROUPS when it is imported, and naming
itself in EXTENSIONS:

    from agentos.runner.siteapis import cli
    cli.COMMANDS.append(("openapi", openapi_cmd))
    cli.GROUPS.append(("keys", keys_app))
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from agentos.core import catalog
from agentos.core.config import home_from_env
from agentos.core.errors import AgentosError
from agentos.core.layout import ops_socket
from agentos.core.system import Host

app = typer.Typer(no_args_is_help=True, help="Site APIs: a site you can use, as a typed API of your own.")

COMMANDS: list[tuple[str, Callable[..., Any]]] = []
GROUPS: list[tuple[str, typer.Typer]] = []
EXTENSIONS = ["agentos.runner.siteapis.http_cli"]

OWNER = {"kind": "human", "id": "owner", "owner": "owner"}  # root on the node acts as the owner
LANE_TIMEOUT_S = 120.0
ApiArg = Annotated[str, typer.Argument(help="the API's key, like harbor-supplier")]
JsonOpt = Annotated[bool, typer.Option("--json", help="print JSON")]


def _service() -> Any:
    from agentos.runner.cli import _service as runner_service

    return runner_service().site_apis


def _fail(e: AgentosError) -> typer.Exit:
    typer.echo(catalog.cli_error(e.code, e.message), err=True)
    for issue in getattr(e, "issues", [])[8:]:
        if issue.level == "error":
            typer.echo(f"  {issue}", err=True)
    return typer.Exit(1)


def _daemon(payload: dict[str, Any]) -> dict[str, Any]:
    resp = Host().unix_request(ops_socket(home_from_env()), payload, timeout=LANE_TIMEOUT_S)
    if resp is None:
        typer.echo("error: agentosd is not running here; it owns the lanes' browsers", err=True)
        raise typer.Exit(1)
    if not resp.get("ok"):
        error = resp.get("error") or {}
        typer.echo(catalog.cli_error(str(error.get("code") or "invalid"), str(error.get("message") or "it failed")), err=True)
        raise typer.Exit(1)
    return resp["result"]


def _params(pairs: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep:
            typer.echo(f"error: --param is key=value, not {pair!r}", err=True)
            raise typer.Exit(2)
        try:
            out[key] = json.loads(value)
        except ValueError:
            out[key] = value
    return out


def format_candidates(found: dict[str, Any]) -> list[str]:
    if found.get("signal") not in (None, "ok", "unknown"):
        return [f"{found.get('on')}: stopped on {found['signal']}. {found.get('note') or ''}".strip()]
    lines = []
    for c in found.get("candidates") or []:
        lines.append(f"{c['name']}: {c['count']} {c['kind']}, confidence {c['confidence']}")
        lines.append(f"  fields: {', '.join(c['fields'])}")
        rows = (c.get("sample") or {}).get("rows") or []
        if rows:
            lines.append(f"  sample (page text): {json.dumps(rows[0], ensure_ascii=False)}")
    return lines or [str(found.get("note") or "Nothing found.")]


def format_row(row: dict[str, Any]) -> list[str]:
    lines = [f"{row['api']} {row['version']}: {row['state']} on {row['domain']} ({row['site']}, as the {row['acts_as']})"]
    lines.append(f"  surfaces: {', '.join(row['surfaces']) or 'none'}")
    if row.get("edited"):
        lines.append("  edited: the live version is older; activate to replace it")
    for name, ep in row["endpoints"].items():
        lines.append(f"  {name}: {ep['effect']}, tool {ep['tool']}, cached {ep['cache_s']} s")
    lines += [f"  problem: {p}" for p in row.get("problems") or []]
    return lines


def _echo(lines: list[str]) -> None:
    for line in lines:
        typer.echo(line)


@app.command()
def discover(on: Annotated[str, typer.Argument(help="the lane, or the account or page")],
             url: Annotated[str | None, typer.Argument(help="the page; default: the page open in the lane")] = None,
             as_json: JsonOpt = False) -> None:
    """Find what a page repeats."""
    found = _daemon({"op": "site_api_discover", "on": on, "url": url})
    typer.echo(json.dumps(found, indent=2, ensure_ascii=False)) if as_json else _echo(format_candidates(found))


@app.command()
def new(api: Annotated[str | None, typer.Argument(help="the key, with --url")] = None,
        file: Annotated[Path | None, typer.Option("--file", exists=True, help="a definition, in JSON")] = None,
        url: Annotated[str | None, typer.Option("--url", help="discover this page and take its best candidate")] = None,
        recording: Annotated[Path | None, typer.Option("--recording", exists=True, help="a draft from flow_record_stop, in JSON")] = None,
        endpoint: Annotated[str, typer.Option("--endpoint", help="the name of the recording's endpoint")] = "recorded",
        vouch: Annotated[bool, typer.Option("--vouch", help="the recorded input steps publish nothing")] = False,
        surface: Annotated[list[str] | None, typer.Option("--surface", help="where it runs, like harbor/supplier")] = None,
        pick: Annotated[str | None, typer.Option("--pick", help="the candidate's name; default: the best")] = None) -> None:
    """Save a draft from a file, from what a page repeats, or from a recording."""
    from agentos.runner.siteapis.discover import definition_from

    if sum(x is not None for x in (file, url, recording)) != 1:
        typer.echo("error: pass --file, or a key and --url or --recording", err=True)
        raise typer.Exit(2)
    try:
        if recording is not None:
            draft = json.loads(recording.read_text())
            _echo(format_row(_service().from_recording(draft.get("draft", draft), api=api or "", endpoint=endpoint, by=OWNER,
                                                       surfaces=surface, vouch=vouch)))
            return
        if file is not None:
            definition = json.loads(file.read_text())
        else:
            found = _daemon({"op": "site_api_discover", "on": (surface or [""])[0], "url": url})
            chosen = [c for c in found.get("candidates") or [] if pick in (None, c["name"])][:1]
            if not chosen:
                typer.echo(f"error: {found.get('note') or 'no such candidate on that page'}", err=True)
                raise typer.Exit(1)
            definition = definition_from(api or "", found.get("url") or url or "", chosen, surfaces=surface)
        _echo(format_row(_service().save(definition, by=OWNER, bump=True)))
    except ValueError as e:
        typer.echo(f"error: {file or recording} is not valid JSON: {e}", err=True)
        raise typer.Exit(1) from None
    except AgentosError as e:
        raise _fail(e) from None


@app.command("try")
def try_cmd(api: ApiArg, endpoint: Annotated[str, typer.Argument(help="the endpoint")],
            param: Annotated[list[str] | None, typer.Option("--param", "-p", help="key=value (JSON values ok)")] = None,
            on: Annotated[str | None,
                          typer.Option("--on", help="the account or page, or the lane")] = None) -> None:
    """Run one endpoint once on the lane. It never runs a commit step."""
    out = _daemon({"op": "site_api_try", "api": api, "endpoint": endpoint, "params": _params(param), "on": on})
    typer.echo(json.dumps(out, indent=2, ensure_ascii=False))


@app.command()
def activate(api: ApiArg, vouch: Annotated[bool, typer.Option("--vouch", help="you read an agent's input steps and vouch for them")] = False) -> None:
    """Make the draft the live API, or wake a paused one."""
    try:
        _echo(format_row(_service().activate(api, by=OWNER, vouch=vouch)))
    except AgentosError as e:
        raise _fail(e) from None


def _drop_cache(api: str) -> None:
    """The CLI's runtime has no site_http, so the service cannot do this here: go to the store. No
    store yet means nothing was saved, and the pause or removal has happened either way."""
    from agentos.runner.siteapis.http_store import DB, HttpStore

    home = home_from_env()
    if not (home / DB).exists():
        return
    try:
        HttpStore(home).cache_delete(api=api)
    except AgentosError as e:
        typer.echo(f"Its saved answers could not be deleted: {e.message}", err=True)


@app.command()
def pause(api: ApiArg) -> None:
    """Stop it answering, until you activate it again."""
    try:
        row = _service().pause(api, by=OWNER)
        _drop_cache(api)
        _echo(format_row(row))
    except AgentosError as e:
        raise _fail(e) from None


@app.command("list")
def list_cmd(as_json: JsonOpt = False) -> None:
    """Every site API and draft on this node."""
    rows = _service().apis()
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    for row in rows:
        _echo(format_row(row))
    if not rows:
        typer.echo("No site APIs yet. agentos site-api discover shows what a page offers.")


@app.command()
def show(api: ApiArg) -> None:
    """One API, with its definition."""
    try:
        typer.echo(json.dumps(_service().get(api), indent=2, ensure_ascii=False))
    except AgentosError as e:
        raise _fail(e) from None


@app.command()
def remove(api: ApiArg) -> None:
    """Remove it. Its runs stay in history. Refused while a schedule or workflow uses it."""
    try:
        gone = _service().remove(api, by=OWNER)
        _drop_cache(api)
        typer.echo(json.dumps(gone))
    except AgentosError as e:
        raise _fail(e) from None


def _extend() -> None:
    for module in EXTENSIONS:
        try:
            importlib.import_module(module)
        except ModuleNotFoundError as e:  # that half is not on this node, nothing else is wrong
            if not (e.name and module.startswith(e.name)):
                raise
    done = {c.name or getattr(c.callback, "__name__", "") for c in app.registered_commands} | {g.name for g in app.registered_groups}
    for name, fn in COMMANDS:
        if name not in done:
            app.command(name)(fn)
    for name, group in GROUPS:
        if name not in done:
            app.add_typer(group, name=name)


_extend()
