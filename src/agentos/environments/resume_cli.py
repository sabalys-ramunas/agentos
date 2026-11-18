"""`agentos environments resume <title or save id>`: the owner resumes a saved environment without a
chat (agentos.environments.waking), from the node's shell.

Its command joins `agentos environments` (agentos.environments.cli's app, with prepared_cli's commands;
agentos.commands.features mounts this module). It asks agentosd (POST /api/environments/resume, the
owner only), which rents the machine within the node's limits and brings it up as environment_open
does; the answer has the live view's link. No chat holds it, and an agent's environment_open with its
title takes it at once.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer

from agentos.core.errors import InvalidRequest
from agentos.environments import prepared_cli as _prepared  # noqa: F401  (its commands join the same app)
from agentos.environments.cli import _daemon_base, _daemon_error, _fail, _home, _http_client, app


def _post(path: str, body: dict) -> dict:
    """POST to agentosd's own API with the node's token, as the owner."""
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
            r = http.post(f"{base}{path}", json=body, headers={"Authorization": f"Bearer {cfg.mcp_token}"})
    except httpx.HTTPError as e:
        raise _fail(InvalidRequest(f"agentosd is not reachable at {base} ({type(e).__name__}); start agentosd and "
                                   "run this again")) from None
    if r.status_code != 200:
        raise _fail(InvalidRequest(_daemon_error(r)))
    return r.json()


def resumed_words(out: dict[str, Any]) -> str:
    """What the CLI says about a resume."""
    resumed = out.get("resumed") or {}
    how = {"paused": "the paused machine wakes", "snapshot": "from its disk snapshot",
           "files": "its files on a fresh machine"}.get(str(resumed.get("from")), "")
    if out.get("opened") == "already_open":
        head = f"\"{out['title']}\" is open already: {out['id']} ({out['state']})."
    else:
        head = (f"Resuming \"{out['title']}\" from save {resumed.get('save_id')}" + (f" ({how})" if how else "")
                + f": environment {out['id']}, {out['state']}.")
    links = out.get("links") or {}
    lines = [head]
    if links.get("browser"):
        lines.append(f"Live view: {links['browser']}" + (f"  (first: {links['tunnel']})" if links.get("tunnel") else ""))
    lines.append(f"No chat holds it: an agent's environment_open(title=\"{out['title']}\") takes it. Unused and "
                 "unwatched, it is saved again after idle_minutes.")
    return "\n".join(lines)


@app.command()
def resume(ref: Annotated[str, typer.Argument(metavar="TITLE_OR_SAVE_ID",
                                              help="The saved environment's title, or a save id (`saves` lists them).")],
           json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Resume a saved environment now, held by no chat, and give its live view.

    The same resume as an agent's environment_open with its title (a snapshot, or the paused machine on
    Morph), within the node's limits. The next agent that opens its title takes it at once."""
    out = _post("/api/environments/resume", {"ref": ref})
    if json_out:
        typer.echo(json.dumps(out, indent=2))
        return
    typer.echo(resumed_words(out))
