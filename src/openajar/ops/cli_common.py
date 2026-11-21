"""What the ops commands share: a clean exit, OPENAJAR_HOME and the node's name, JSON output, and
reading a passphrase (never from the command line, where `ps` would show it)."""

from __future__ import annotations

import getpass
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from openajar.core import age, catalog, migrate
from openajar.core.config import config_path, home_from_env, load_config
from openajar.core.errors import OpenajarError
from openajar.node import lanefmt
from openajar.ops import backup

PASSPHRASE_ENV = "OPENAJAR_BACKUP_PASSPHRASE_FILE"


def _fail(message: str, code: int = 1) -> NoReturn:
    typer.secho(f"error: {message}", err=True, fg=typer.colors.RED)
    raise typer.Exit(code)


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except OpenajarError as e:
        typer.secho(catalog.cli_error(e.code, e.message), err=True, fg=typer.colors.RED)
        raise typer.Exit(1) from None
    except (migrate.MigrationError, lanefmt.LaneFormatError, age.AgeError) as e:
        _fail(str(e))


def _home() -> Path:
    return home_from_env()


def _node_name(home: Path) -> str:
    if not config_path(home).exists():
        _fail(f"no node.toml in {home}; run `openajar node init` first (or set OPENAJAR_HOME)")
    return load_config(home).name


def _emit(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str))


def _mib(n: int) -> str:
    return f"{n / 1024**2:.1f} MiB"


def read_secret_file(path: Path, what: str = "passphrase file") -> str:
    """A secret from a file only its owner can read (chmod 600), without its final newline."""
    try:
        st = os.stat(path)
    except OSError:
        _fail(f"cannot read the {what} {path}")
    if st.st_mode & 0o077:
        _fail(f"the {what} {path} is readable by others (run: chmod 600 {path})")
    text = path.read_text().rstrip("\r\n")
    if not text:
        _fail(f"the {what} {path} is empty")
    return text


def read_passphrase(file: Path | None, *, confirm: bool, prompt: str = "Backup passphrase: ",
                    env: str = PASSPHRASE_ENV) -> str:
    path = file or (Path(os.environ[env]) if os.environ.get(env) else None)
    if path is not None:
        return read_secret_file(path)
    if not sys.stdin.isatty():
        _fail(f"no terminal to ask for the passphrase on; pass --passphrase-file or set {env}")
    first = getpass.getpass(prompt)
    if confirm:
        if len(first) < backup.MIN_PASSPHRASE:
            _fail(f"the passphrase must be at least {backup.MIN_PASSPHRASE} characters")
        if getpass.getpass("Repeat the passphrase: ") != first:
            _fail("the passphrases do not match")
    return first


JsonOpt = Annotated[bool, typer.Option("--json", help="Print JSON.")]
