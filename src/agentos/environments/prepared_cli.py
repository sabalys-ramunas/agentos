"""`agentos environments prepare`, `prepared` and `prepared forget`: the repositories' prepared machines
(agentos.environments.prepbuild), and the warm pool's machines, from the node's shell.

Its commands join `agentos environments` (agentos.environments.cli's app, which this module adds to and
agentos.commands.features mounts from here).

`prepare` asks agentosd to build one now (the build needs its git gateway) and follows the build's
steps in the store until it is ready or failed; Ctrl-C stops only the following. `prepared` lists
them with what they cost a month, and the warm pool; `prepared forget` deletes a repository's.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Annotated

import typer

from agentos.core.errors import AgentosError, InvalidRequest
from agentos.environments.cli import _daemon_base, _daemon_error, _fail, _home, _http_client, _managers, app

POLL_S = 2.0

prepared_app = typer.Typer(invoke_without_command=True, help=(
    "The repositories' prepared machines (an image with their setup run), and the warm pool."))
app.add_typer(prepared_app, name="prepared")


def _post_prepare(repo: str, ref: str | None, force: bool) -> dict:
    from agentos.core.config import config_path, load_config

    home = _home()
    if not config_path(home).exists():
        raise _fail(InvalidRequest(f"no node.toml in {home}; run `agentos node init` first (or set AGENTOS_HOME)"))
    cfg = load_config(home)
    if not cfg.mcp_token:
        raise _fail(InvalidRequest("this node has no token for its own API, so the CLI cannot reach agentosd"))
    import httpx

    base = _daemon_base(cfg)
    try:
        with _http_client() as http:
            r = http.post(f"{base}/api/environments/prepare", json={"repo": repo, "ref": ref, "force": force},
                          headers={"Authorization": f"Bearer {cfg.mcp_token}"})
    except httpx.HTTPError as e:
        raise _fail(InvalidRequest(f"agentosd is not reachable at {base} ({type(e).__name__}); start agentosd and "
                                   "run this again")) from None
    if r.status_code != 200:
        raise _fail(InvalidRequest(_daemon_error(r)))
    return r.json()


@app.command()
def prepare(repo: Annotated[str, typer.Argument(help="The repository, like github.com/me/shop.")],
            ref: Annotated[str | None, typer.Option("--ref", help="A branch, tag or commit (default: the default "
                                                    "branch).")] = None,
            force: Annotated[bool, typer.Option("--force", help="Build it again when it is ready already.")] = False,
            json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Build the repository's prepared machine now, and show its steps.

    The repository says how in .agentos/environment.toml (setup, refresh, start, port). The build runs
    on a machine of its own, within the node's limits, and agentosd keeps going if you stop this."""
    from agentos.environments.prepared import PreparedStore

    out = _post_prepare(repo, ref, force)
    store, key = PreparedStore(_home()), out["key"]
    if not out.get("started"):
        typer.echo(f"{out['repo']}: {out.get('why') or 'not started'}; following it.")
    began, said = time.monotonic(), ""
    while True:
        rec = store.get(key)
        if rec is None:
            raise _fail(InvalidRequest("the build's record is gone (it was forgotten meanwhile)"))
        if rec.state != "building":
            break
        if rec.step and rec.step != said and not json_out:
            typer.echo(f"  {int(time.monotonic() - began):>4} s  {rec.step}")
            said = rec.step
        time.sleep(POLL_S)
    if json_out:
        typer.echo(json.dumps(rec.model_dump(mode="json"), indent=2))
        if rec.state != "ready":
            raise typer.Exit(1)
        return
    if rec.state == "ready":
        typer.echo(f"Ready: {rec.repo} at {(rec.commit or '')[:12]}, image {rec.image}. Setup took {rec.setup_s or 0:.0f} s, "
                   f"the whole build {rec.build_s or 0:.0f} s. The next open of it starts from this.")
        return
    typer.echo(f"Failed: {rec.error or 'no reason given'}", err=True)
    raise typer.Exit(1)


@prepared_app.callback()
def prepared(ctx: typer.Context, json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List the prepared machines, what each costs a month, and the warm pool."""
    if ctx.invoked_subcommand is not None:
        return
    mgr, _vms = _managers()
    rows = mgr.prepared_rows()
    pool = mgr.pool_view()
    if json_out:
        typer.echo(json.dumps({"prepared": rows, "eur_month": mgr.prepared_eur_month(), "pool": pool}, indent=2))
        return
    if not rows:
        typer.echo("No prepared machines. A repository with .agentos/environment.toml gets one after its first "
                   "open, or now with `agentos environments prepare <repo>`.")
    for r in rows:
        size = f"{r['gb']:.1f} GB" if r.get("gb") else "-"
        when = f"built {r['age']} ago" if r.get("age") else (r.get("step") or "")
        line = (f"{r['repo']}  {r['state']}  {r['commit'] or '-'}  {when}  used {r['uses']}x  {size}  "
                f"EUR {r['cost_eur_month']:.2f} a month")
        typer.echo(line + (f"\n    {r['error'][-300:]}" if r.get("error") else ""))
    if rows:
        typer.echo(f"Together: EUR {mgr.prepared_eur_month():.2f} a month (snapshot_eur_per_gb_month in "
                   "environments.toml). A newer build replaces a repository's older one; one unused for "
                   f"{mgr.settings.prepared_days} days is deleted.")
    typer.echo(pool_words(pool))


def pool_words(pool: dict) -> str:
    """The warm pool in one line or a few."""
    if not pool["target"] and not pool["warm"]:
        return "Warm pool: off (`agentos environments allow --warm 1` keeps one machine booted for the next open)."
    head = (f"Warm pool: {pool['warm']} of {pool['target']} warm, each kept {pool['minutes']} min unless an open "
            "takes it.")
    rows = [f"  {m['vm']}  {m['state']}  since {m['since']}  {m['server_type']}  {m['image']}  "
            f"EUR {m['cost_eur'] or 0:.2f} so far" for m in pool["machines"]]
    return "\n".join([head, *rows])


@prepared_app.command("forget")
def forget_prepared(repo: Annotated[str, typer.Argument(help="The repository, like github.com/me/shop.")]) -> None:
    """Delete a repository's prepared machines (their images at the provider too)."""
    from agentos.git.wire import parse_remote

    mgr, _vms = _managers()
    try:
        slug = parse_remote(repo).slug
        gone = asyncio.run(mgr.forget_prepared(slug))
    except AgentosError as e:
        raise _fail(e) from None
    typer.echo(f"Forgot {len(gone)} prepared machine{'s' if len(gone) != 1 else ''} of {slug}.")
