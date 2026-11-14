"""`agentos node templates`: the node templates in deploy/templates and what each one needs.

A template is a directory under deploy/templates with a template.toml manifest:

    title = "Hetzner Cloud server"
    summary = "..."
    file = "cloud-init.yaml"                 # the main file, relative to the directory
    create = "deploy/templates/hetzner/create.sh ..."   # optional: how to make the node

    [[variables]]                            # one per value to fill in
    name = "ADMIN_SSH_KEY"
    description = "that user's public key, on one line"
    example = "ssh-ed25519 AAAA... you@laptop"
    required = true                          # default true
    default = ""                             # optional

A directory without a manifest is not a template (deploy/templates/common holds shared scripts).
The templates ship with the checkout, not in the wheel, so an install without deploy/ lists none.
"""

from __future__ import annotations

import textwrap
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "deploy" / "templates"
MANIFEST = "template.toml"


class ManifestError(ValueError):
    """A template.toml that cannot be read, or one that names a file which is not there."""


@dataclass(frozen=True)
class Variable:
    name: str
    description: str
    example: str = ""
    required: bool = True
    default: str = ""


@dataclass(frozen=True)
class Template:
    name: str
    title: str
    summary: str
    path: Path
    file: str
    create: str
    variables: tuple[Variable, ...]

    @property
    def main_file(self) -> Path:
        return self.path / self.file


def _variable(raw: dict, where: str) -> Variable:
    for key in ("name", "description"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise ManifestError(f"{where}: every [[variables]] entry needs a {key}")
    return Variable(name=raw["name"], description=raw["description"], example=str(raw.get("example", "")),
                    required=bool(raw.get("required", True)), default=str(raw.get("default", "")))


def load_template(directory: Path) -> Template:
    manifest = directory / MANIFEST
    where = str(manifest)
    try:
        raw = tomllib.loads(manifest.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError(f"{where}: {exc}") from None
    for key in ("title", "file"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise ManifestError(f"{where}: missing {key}")
    if not (directory / raw["file"]).is_file():
        raise ManifestError(f"{where}: {raw['file']} is not in {directory}")
    return Template(
        name=directory.name,
        title=raw["title"],
        summary=str(raw.get("summary", "")),
        path=directory,
        file=raw["file"],
        create=str(raw.get("create", "")),
        variables=tuple(_variable(v, where) for v in raw.get("variables", [])),
    )


def load_templates(root: Path = TEMPLATES_DIR) -> dict[str, Template]:
    """Every template under root, by directory name, in name order."""
    if not root.is_dir():
        return {}
    directories = [d for d in sorted(root.iterdir()) if (d / MANIFEST).is_file()]
    return {d.name: load_template(d) for d in directories}


def _fail(message: str, code: int) -> typer.Exit:
    typer.secho(f"error: {message}", err=True, fg=typer.colors.RED)
    return typer.Exit(code)


def _show(template: Template) -> None:
    typer.echo(f"{template.title} ({template.name})")
    typer.echo(f"  path: {template.path}")
    typer.echo(f"  file: {template.main_file}")
    if template.create:
        typer.echo(f"  create: {template.create}")
    typer.echo("")
    typer.echo(textwrap.fill(template.summary, width=100))
    if not template.variables:
        return
    width = max(len(v.name) for v in template.variables)
    typer.echo("")
    typer.echo("Variables (fill them in the file; required ones have no default):")
    for v in template.variables:
        status = "required" if v.required else "optional"
        default = f" (default: {v.default})" if v.default else ""
        typer.echo(f"  {v.name.ljust(width)}  {status:<8}  {v.description}{default}")
        if v.example and v.example != v.default:
            typer.echo(f"  {'':<{width}}  {'':<8}  e.g. {v.example}")


def templates_command(
    name: Annotated[str | None, typer.Argument(help="A template, for example hetzner. Leave out to list them.")]
    = None,
) -> None:
    """List the node templates, or show one: where its files are and the variables to fill in."""
    if not TEMPLATES_DIR.is_dir():
        raise _fail("the node templates are not in this install; they come with the checkout (deploy/templates)", 1)
    try:
        templates = load_templates(TEMPLATES_DIR)
    except ManifestError as exc:
        raise _fail(str(exc), 1) from None
    if name is None:
        width = max((len(t) for t in templates), default=0)
        for template in templates.values():
            typer.echo(f"{template.name.ljust(width)}  {template.title}")
        typer.echo("")
        typer.echo("Show one with: agentos node templates NAME")
        return
    if name not in templates:
        known = ", ".join(templates) or "none"
        raise _fail(f"no template {name!r} (known: {known})", 2)
    _show(templates[name])
