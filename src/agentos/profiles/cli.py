"""`agentos profile ...` (also `agentos workspace profile ...`): profiles for the owner, on the node.

Whoever runs this has a shell on the node, so it is the owner in person: a save is live at once,
and drafts agents saved are approved or rejected here. It works on the node's files directly.

    agentos profile list [acme]
    agentos profile show acme/acme [--version 2] [--json]
    agentos profile save profile.yaml
    agentos profile set acme/acme headline "Coffee gear, made in Vilnius"
    agentos profile edit acme/acme changes.yaml
    agentos profile photo acme/acme avatar me.jpg
    agentos profile attach acme/acme acme/instagram [--site instagram] [--override bio="..."]
    agentos profile detach acme/acme acme/instagram
    agentos profile approve acme/acme --version 3
    agentos profile reject acme/acme --version 3
    agentos profile status acme/acme
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml

from agentos.core import catalog
from agentos.core.config import home_from_env, load_config
from agentos.core.errors import AgentosError, InvalidRequest
from agentos.core.runtime import Runtime
from agentos.profiles.service import ProfilesService, split_ref

app = typer.Typer(no_args_is_help=True, help="Profiles: a person or business, shown on all its accounts.")
OWNER = {"kind": "human", "id": "owner", "owner": "owner"}
STATE_WORDS = {"in_sync": "in sync", "due": "due at the next session", "awaiting_approval": "waits for your approval",
               "denied": "you denied it", "failed": "failed, tried again later",
               "no_program": "needs an approved engagement program (risk off is enough)",
               "manual": "change by hand in the lane", "done_by_hand": "changed by hand"}


def _home() -> Path:
    """AGENTOS_HOME, else the node's home; on a laptop with no node, the profiles `agentos studio
    up` keeps (~/.agentos/studio/node), so the owner approves there without setting anything."""
    home = home_from_env()
    if "AGENTOS_HOME" in os.environ or (home / "node.toml").exists():
        return home
    studio = Path.home() / ".agentos" / "studio" / "node"
    if studio.is_dir():
        typer.echo(f"(the profiles studio keeps on this computer, in {studio})", err=True)
        return studio
    return home


@contextmanager
def _service() -> Iterator[ProfilesService]:
    home = _home()
    cfg = load_config(home) if (home / "node.toml").exists() else None
    svc = engage = None
    try:
        runtime = Runtime(config=cfg) if cfg is not None else Runtime()
        try:
            from agentos.engage.service import EngageService

            engage = EngageService(runtime, home=home)
            runtime.register("engage", engage)
        except AgentosError:
            engage = None           # the status then says no_program; everything else works
        from agentos.profiles.service import build

        svc = build(runtime, home=home)
        yield svc
    except AgentosError as exc:
        typer.secho(catalog.cli_error(exc.code, exc.message), err=True, fg=typer.colors.RED)
        raise typer.Exit(1) from None
    finally:
        if svc is not None:
            svc.store.close()
        if engage is not None:
            engage.store.close()


def _saved(out: dict[str, Any]) -> None:
    typer.echo(f"{out['profile']} version {out['version']}: {out['state']}" + ("" if out["created"] else " (no change)"))
    for line in out["summary"]:
        typer.echo(f"  {line}")
    typer.echo(out["next"])


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError as e:
        raise InvalidRequest(f"cannot read {path}: {e.strerror}") from None


@app.command("list")
def list_(workspace: Annotated[str | None, typer.Argument(help="Only this workspace.")] = None) -> None:
    """Every profile, its accounts, and drafts waiting for you."""
    with _service() as svc:
        rows = svc.list(workspace)
    if not rows:
        typer.echo("No profiles yet. Save one with: agentos profile save profile.yaml")
    for r in rows:
        wait = f", draft {r['draft_waiting']} waits for you" if r["draft_waiting"] else ""
        typer.echo(f"{r['profile']}  {r['display_name']} ({r['kind']}), version {r['version']} {r['state']}{wait}")
        typer.echo(f"  accounts: {', '.join(r['accounts']) or 'none'}")


@app.command("show")
def show(ref: Annotated[str, typer.Argument(help="workspace/profile, e.g. acme/acme.")],
         version: Annotated[int | None, typer.Option(help="One saved version.")] = None,
         as_json: Annotated[bool, typer.Option("--json", help="As JSON.")] = False) -> None:
    """The profile as YAML."""
    with _service() as svc:
        out = svc.get(ref, version=version)
    if as_json:
        typer.echo(json.dumps(out, indent=2, default=str))
        return
    typer.echo(f"# version {out['version']}, {out['state']}" +
               (f"; draft {out['draft_waiting']} waits for you" if out.get("draft_waiting") else ""))
    if out.get("reason"):
        typer.echo(f"# proposed because: {out['reason']}".replace("\n", "\n#   "))
    for line in out.get("diff") or []:
        typer.echo(f"# change: {line}")
    typer.echo(yaml.safe_dump(out["definition"], sort_keys=False, allow_unicode=True).rstrip())


@app.command("save")
def save(path: Annotated[Path, typer.Argument(help="The profile as YAML or JSON.")]) -> None:
    """Save a whole profile (new, or a full replacement). Live at once."""
    with _service() as svc:
        _saved(svc.save(_read(path), by=OWNER, as_owner=True))


@app.command("edit")
def edit(ref: Annotated[str, typer.Argument(help="workspace/profile.")],
         path: Annotated[Path, typer.Argument(help="YAML or JSON with only the fields to change.")]) -> None:
    """Change some fields: nested fields merge, lists replace, null removes."""
    with _service() as svc:
        _saved(svc.update(ref, _read(path), by=OWNER, as_owner=True))


@app.command("set")
def set_(ref: Annotated[str, typer.Argument(help="workspace/profile.")],
         field: Annotated[str, typer.Argument(help="A field such as headline, bio.short, phone or voice.tone.")],
         value: Annotated[str, typer.Argument(help="The new value ('' removes it).")]) -> None:
    """Set one field, for every attached account."""
    changes: dict[str, Any] = {}
    node = changes
    parts = field.split(".")
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value if value != "" else None
    with _service() as svc:
        _saved(svc.update(ref, changes, by=OWNER, as_owner=True))


@app.command("photo")
def photo(ref: Annotated[str, typer.Argument(help="workspace/profile.")],
          which: Annotated[str, typer.Argument(help="avatar or cover.")],
          path: Annotated[Path, typer.Argument(help="A JPEG, PNG, WebP or GIF file.")]) -> None:
    """Set the profile's avatar or cover photo from a file."""
    if which not in ("avatar", "cover"):
        raise typer.BadParameter("avatar or cover")
    ws, _pid = split_ref(ref)
    with _service() as svc:
        try:
            data = path.read_bytes()
        except OSError as e:
            raise InvalidRequest(f"cannot read {path}: {e.strerror}") from None
        kept = svc.put_photo(ws, path.name, data)
        _saved(svc.update(ref, {"photos": {which: kept["photo"]}}, by=OWNER, as_owner=True))


@app.command("attach")
def attach(ref: Annotated[str, typer.Argument(help="workspace/profile.")],
           surface: Annotated[str, typer.Argument(help="The account, e.g. acme/instagram.")],
           site: Annotated[str | None, typer.Option(help="The site, when the account's name does not say it.")] = None,
           override: Annotated[list[str] | None, typer.Option(help="field=value only this account shows "
                                                                  "(repeat it).")] = None) -> None:
    """Attach an account: it shows the profile from now on."""
    overrides = {}
    for item in override or []:
        key, sep, value = item.partition("=")
        if not sep:
            raise typer.BadParameter(f"--override {item!r} is field=value")
        overrides[key.strip()] = value
    with _service() as svc:
        _saved(svc.attach(ref, surface, by=OWNER, site=site, overrides=overrides, as_owner=True))


@app.command("detach")
def detach(ref: Annotated[str, typer.Argument(help="workspace/profile.")],
           surface: Annotated[str, typer.Argument(help="The account.")]) -> None:
    """Detach an account. It keeps what it shows."""
    with _service() as svc:
        _saved(svc.detach(ref, surface, by=OWNER, as_owner=True))


@app.command("approve")
def approve(ref: Annotated[str, typer.Argument(help="workspace/profile.")],
            version: Annotated[int, typer.Option(help="The draft to approve, as `agentos profile show --version N` "
                                                      "showed it.")],
            ) -> None:
    """Approve a draft an agent saved: it becomes the profile. Read it first with show --version."""
    with _service() as svc:
        _saved(svc.decide(ref, version, approve=True, by=OWNER, in_person=True))


@app.command("reject")
def reject(ref: Annotated[str, typer.Argument(help="workspace/profile.")],
           version: Annotated[int, typer.Option(help="The draft to reject.")],
           note: Annotated[str | None, typer.Option(help="Why, for the agent that saved it.")] = None) -> None:
    """Reject a draft. Nothing changes."""
    with _service() as svc:
        _saved(svc.decide(ref, version, approve=False, by=OWNER, in_person=True, note=note))


@app.command("status")
def status(ref: Annotated[str, typer.Argument(help="workspace/profile.")],
           as_json: Annotated[bool, typer.Option("--json", help="As JSON.")] = False) -> None:
    """Per account: each field and whether it shows the profile yet."""
    with _service() as svc:
        out = svc.status(ref)
    if as_json:
        typer.echo(json.dumps(out, indent=2, default=str))
        return
    typer.echo(f"{out['profile']} version {out['version']}")
    for a in out["accounts"]:
        typer.echo(f"\n{a['surface']} ({a['site']})")
        for f, v in a["fields"].items():
            typer.echo(f"  {f:<13} {STATE_WORDS.get(v['state'], v['state'])}")
        for f, why in a["skipped"].items():
            typer.echo(f"  {f:<13} left out: {why}")
        for p in a["problems"]:
            typer.secho(f"  problem: {p}", fg=typer.colors.YELLOW)
