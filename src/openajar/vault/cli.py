"""`openajar vault ...`: create, unseal, fill and inspect the sealed vault.

Secrets are read with getpass (or as JSON on stdin with --stdin), never from argv, and
nothing here ever prints a username, secret, seed or code. Commands that change entries
open vault.db directly (SQLite locking keeps them safe next to a running daemon);
unseal, seal and status also talk to the daemon over its control socket.
"""

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

from openajar.core import catalog
from openajar.core.config import home_from_env
from openajar.core.errors import OpenajarError
from openajar.vault import control, crypto
from openajar.vault.domains import display_host
from openajar.vault.errors import (
    EntryInvalid,
    VaultAlreadyInitialized,
    VaultError,
    VaultNotInitialized,
    WrongPassphrase,
)
from openajar.vault.importers import parse_bindings, parse_file, shred
from openajar.vault.models import EntryKind, NewEntry, validate_new_entry
from openajar.vault.secret import Secret
from openajar.vault.service import keyfile_path, load_keyfile_master, pending_keyfile_path
from openajar.vault.store import VaultStore, WrongKey

app = typer.Typer(no_args_is_help=True,
                  help="Sealed vault: logins, TOTP seeds and API keys bound to domains and lanes.")

MIN_PASSPHRASE = 12
KDF_PARAMS: dict | None = None   # None = crypto.DEFAULT_KDF; tests lower it


def _getpass(prompt: str) -> str:
    return getpass.getpass(prompt)


def _home() -> Path:
    return home_from_env()


def _store() -> VaultStore:
    return VaultStore(_home() / "vault.db")


def _socket() -> Path:
    return _home() / "run" / "vault.sock"


def _fail(message: str, code: str = "error") -> NoReturn:
    typer.echo(f"error: {message} ({code})", err=True)
    raise typer.Exit(1)


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except OpenajarError as e:
        typer.echo(catalog.cli_error(e.code, e.message), err=True)
        raise typer.Exit(1) from None


def _master(store: VaultStore) -> bytes:
    header = store.header()
    if header is None:
        raise VaultNotInitialized(f"no vault at {store.path}; run: openajar vault init")
    if header.mode == "keyfile":
        return load_keyfile_master(store, keyfile_path(_home()))
    key = crypto.derive_master(_getpass("Vault passphrase: "), header.kdf)
    try:
        store.check_master(key)
    except WrongKey:
        raise WrongPassphrase("wrong passphrase") from None
    return key


def _new_passphrase() -> str:
    first = _getpass("New vault passphrase: ")
    if len(first) < MIN_PASSPHRASE:
        _fail(f"the passphrase must be at least {MIN_PASSPHRASE} characters", "invalid")
    if _getpass("Repeat the passphrase: ") != first:
        _fail("the passphrases do not match", "invalid")
    return first


def _ask_secret(label: str, *, confirm: bool = False, optional: bool = False) -> Secret | None:
    value = _getpass(f"{label}{' (Enter for none)' if optional else ''}: ")
    if not value:
        if optional:
            return None
        _fail(f"{label.lower()} is required", "invalid")
    if confirm and _getpass(f"Repeat {label.lower()}: ") != value:
        _fail(f"the two {label.lower()} entries do not match", "invalid")
    return Secret(value)


def _daemon(payload: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return control.request(_socket(), payload)
    except (OSError, ValueError):
        return None


def _domain_label(domain: str | None) -> str:
    if not domain:
        return "-"
    shown = display_host(domain)
    return domain if shown == domain else f"{domain} ({shown})"


# ---- init / unseal / seal / status --------------------------------------------------

@app.command()
def init(passphrase: bool = typer.Option(
        False, "--passphrase",
        help="Seal with a passphrase, asked for after every restart, instead of a key file.")) -> None:
    """Create the vault (vault.db plus a key file, or a passphrase)."""
    store = _store()
    with _errors():
        if store.header() is not None:
            raise VaultAlreadyInitialized(f"a vault already exists at {store.path}")
        if passphrase:
            secret = _new_passphrase()
            kdf = crypto.new_kdf(KDF_PARAMS)
            store.initialize(mode="passphrase", master=crypto.derive_master(secret, kdf), kdf=kdf)
            typer.echo(f"Vault created at {store.path} (passphrase). It starts sealed after every restart: "
                       "run `openajar vault unseal`.")
            return
        keyfile = keyfile_path(_home())
        if keyfile.exists():
            raise VaultError(f"a key file already exists at {keyfile}; move it away first")
        pending = pending_keyfile_path(keyfile)
        master = os.urandom(crypto.KEY_LEN)
        crypto.write_keyfile(pending, master)
        store.initialize(mode="keyfile", master=master, kdf=None)
        os.replace(pending, keyfile)
        crypto.fsync_dir(keyfile.parent)
        typer.echo(f"Vault created at {store.path} with key file {keyfile} (chmod 600). "
                   "Back the key file up somewhere safe: without it the vault cannot be opened.")


@app.command()
def unseal() -> None:
    """Unseal the running daemon's vault (asks for the passphrase)."""
    store = _store()
    with _errors():
        header = store.header()
        if header is None:
            raise VaultNotInitialized(f"no vault at {store.path}; run: openajar vault init")
        payload: dict[str, Any] = {"op": "unseal"}
        if header.mode == "passphrase":
            payload["passphrase"] = _getpass("Vault passphrase: ")
        resp = _daemon(payload)
        if resp is None:
            if header.mode == "passphrase":
                key = crypto.derive_master(payload["passphrase"], header.kdf)
                try:
                    store.check_master(key)
                except WrongKey:
                    raise WrongPassphrase("wrong passphrase") from None
                _fail("the passphrase is correct, but no openajar daemon is running to unseal; "
                      "run this again once the daemon is up", "unavailable")
            load_keyfile_master(store, keyfile_path(_home()))
            _fail("no openajar daemon is running; a key-file vault unseals itself when the daemon starts",
                  "unavailable")
        if not resp.get("ok"):
            err = resp.get("error", {})
            _fail(err.get("message", "unseal failed"), err.get("code", "error"))
        typer.echo(f"Vault unsealed ({resp['status'].get('entries', 0)} entries).")


@app.command()
def seal() -> None:
    """Seal the running daemon's vault: the key is dropped from memory."""
    resp = _daemon({"op": "seal"})
    if resp is None:
        typer.echo("No openajar daemon is running; nothing to seal.")
        return
    if not resp.get("ok"):
        err = resp.get("error", {})
        _fail(err.get("message", "seal failed"), err.get("code", "error"))
    typer.echo("Vault sealed.")


@app.command()
def status(as_json: bool = typer.Option(False, "--json", help="Print JSON.")) -> None:
    """Show whether the vault exists, its mode, entry count, and the daemon's state."""
    store = _store()
    out: dict[str, Any] = {"path": str(store.path)}
    with _errors():
        header = store.header()
        out["initialized"] = header is not None
        if header is not None:
            out.update(mode=header.mode, key_version=header.key_version, entries=store.count())
            if header.mode == "keyfile":
                try:
                    crypto.read_keyfile(keyfile_path(_home()))
                    out["key_file"] = "ok"
                except VaultError as e:
                    out["key_file"] = f"{e.code}: {e.message}"
    resp = _daemon({"op": "status"})
    out["daemon"] = "not running" if resp is None else resp.get("status", {}).get("state", "unknown")
    if as_json:
        typer.echo(json.dumps(out, indent=2))
        return
    for key, value in out.items():
        typer.echo(f"{key.replace('_', ' ')}: {value}")


# ---- entries --------------------------------------------------------------------

@app.command()
def add(
    name: str = typer.Argument(..., help="A name for the entry, e.g. 'biz-a instagram'."),
    kind: Annotated[EntryKind, typer.Option("--kind", help="password, totp, cookie, api_key or note.")] = (
        EntryKind.PASSWORD),
    domain: str = typer.Option(None, "--domain", help="The site, e.g. instagram.com."),
    host: Annotated[list[str] | None, typer.Option(
        "--host", help="Allowed host (repeatable); narrows the domain.")] = None,
    lane: Annotated[list[str] | None, typer.Option("--lane", help="Bound lane or workspace/* (repeatable).")] = None,
    username: bool = typer.Option(True, "--username/--no-username", help="Ask for a username."),
    totp: bool = typer.Option(False, "--totp", help="Also ask for a TOTP seed or otpauth:// URI."),
    header: str = typer.Option(None, "--header", help="api_key: header name (default Authorization)."),
    prefix: str = typer.Option(None, "--prefix", help="api_key: value prefix (default 'Bearer ')."),
    stdin: bool = typer.Option(False, "--stdin", help='Read {"username", "secret", "totp"} as JSON from stdin.'),
    replace: bool = typer.Option(False, "--replace", help="Replace an entry with the same name."),
) -> None:
    """Add an entry. Secrets are prompted for (hidden) or read from stdin, never from argv."""
    store = _store()
    with _errors():
        master = _master(store)
        user = secret = seed = None
        if stdin:
            try:
                data = json.loads(sys.stdin.read() or "{}")
            except json.JSONDecodeError:
                raise EntryInvalid("stdin must be a JSON object") from None
            if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
                raise EntryInvalid('stdin must be a JSON object of strings: {"username", "secret", "totp"}')
            user, secret, seed = (Secret(data[k]) if data.get(k) else None for k in ("username", "secret", "totp"))
        elif kind is EntryKind.TOTP:
            seed = _ask_secret("TOTP seed or otpauth:// URI")
        else:
            if kind is EntryKind.PASSWORD and username:
                user = _ask_secret("Username", optional=True)
            secret = _ask_secret({EntryKind.PASSWORD: "Password", EntryKind.API_KEY: "API key",
                                  EntryKind.COOKIE: "Cookie header value",
                                  EntryKind.NOTE: "Note"}[kind], confirm=kind is EntryKind.PASSWORD)
            if totp and kind is EntryKind.PASSWORD:
                seed = _ask_secret("TOTP seed or otpauth:// URI")
        config: dict[str, Any] = {}
        if header is not None:
            config["header"] = header
        if prefix is not None:
            config["prefix"] = prefix
        entry = validate_new_entry(NewEntry(name=name, kind=kind, domain=domain, hosts=host or [], lanes=lane or [],
                                            username=user, secret=secret, totp=seed, config=config))
        meta = store.add(master, entry, replace=replace)
    typer.echo(f"Added {meta.name!r} ({meta.kind.value}, {_domain_label(meta.domain)}), "
               f"lanes: {', '.join(meta.lanes) or 'none (bind one before use)'}.")


@app.command("list")
def list_(lane: str = typer.Option(None, "--lane", help="Only entries bound to this lane."),
          as_json: bool = typer.Option(False, "--json", help="Print JSON.")) -> None:
    """List entries: names, kinds, domains and lanes only."""
    from openajar.vault.policy import lane_bound

    with _errors():
        entries = _store().list()
    if lane:
        entries = [e for e in entries if lane_bound(e.lanes, lane)]
    if as_json:
        typer.echo(json.dumps([e.public() for e in entries], indent=2))
        return
    if not entries:
        typer.echo("No vault entries.")
        return
    width = max(len(e.name) for e in entries)
    for e in entries:
        used = e.last_used.strftime("%Y-%m-%d %H:%M") if e.last_used else "never"
        typer.echo(f"{e.name:<{width}}  {e.kind.value:<8}  {_domain_label(e.domain):<24}  "
                   f"{','.join(e.lanes) or '(unbound)':<24}  used {used}")


@app.command()
def show(name: str) -> None:
    """Show an entry's metadata (never its username, secret or seed)."""
    with _errors():
        meta = _store().get(name)
    detail = meta.detail()
    detail["domain"] = _domain_label(meta.domain)
    for key, value in detail.items():
        if isinstance(value, list | dict):
            value = json.dumps(value) if value else "-"
        typer.echo(f"{key.replace('_', ' ')}: {value if value is not None else '-'}")


@app.command()
def bind(name: str, lanes: Annotated[list[str], typer.Argument(help="Lane ids or workspace/* wildcards.")]) -> None:
    """Bind an entry to lanes (it resolves only on bound lanes)."""
    from openajar.vault.models import validate_lanes

    store = _store()
    with _errors():
        add_ = validate_lanes(lanes)
        current = store.get(name)
        meta = store.update(_master(store), current.id, lanes=sorted({*current.lanes, *add_}))
    typer.echo(f"{meta.name!r} lanes: {', '.join(meta.lanes) or 'none'}.")


@app.command()
def unbind(name: str, lanes: Annotated[list[str], typer.Argument(help="Lane ids or wildcards to remove.")]) -> None:
    """Remove lane bindings from an entry."""
    store = _store()
    with _errors():
        current = store.get(name)
        meta = store.update(_master(store), current.id, lanes=[p for p in current.lanes if p not in set(lanes)])
    typer.echo(f"{meta.name!r} lanes: {', '.join(meta.lanes) or 'none'}.")


@app.command()
def remove(name: str, yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask.")) -> None:
    """Delete an entry for good."""
    store = _store()
    with _errors():
        meta = store.get(name)
        if not yes and not typer.confirm(f"Delete vault entry {meta.name!r} ({_domain_label(meta.domain)})?"):
            raise typer.Exit(1)
        store.remove(meta.id)
    typer.echo(f"Removed {meta.name!r}.")


@app.command()
def catcher(
    name: str = typer.Argument(..., help="The entry whose login sends codes by email."),
    account: str = typer.Option(None, "--account", help="Vault entry with the mailbox login."),
    host: str = typer.Option(None, "--host", help="IMAP host, e.g. imap.gmail.com."),
    port: int = typer.Option(993, "--port"),
    sender: str = typer.Option(None, "--sender", help="Expected sender address or @domain."),
    pattern: str = typer.Option(None, "--pattern", help="Regex for the code (first group wins)."),
    mailbox: str = typer.Option("INBOX", "--mailbox"),
    window: int = typer.Option(None, "--window", help="Only mail sent this many seconds back (default 600); "
                                                          "with --relay, how long the code card stays open (300)."),
    timeout: int = typer.Option(None, "--timeout", help="How long login waits for the code (default 120; "
                                                          "300 with --relay)."),
    relay: bool = typer.Option(False, "--relay", help="Use the code card instead: the owner gets a push and types "
                                                      "the code the site sent into the panel."),
    off: bool = typer.Option(False, "--off", help="Remove the code catcher."),
) -> None:
    """Configure the code source of an entry: the email code catcher, or the code card (--relay).
    Codes are typed by the node, never shown."""
    store = _store()
    with _errors():
        meta = store.get(name)
        config = dict(meta.config)
        if off:
            config.pop("catcher", None)
        elif relay:
            config["catcher"] = {"kind": "relay", "window_seconds": window or 300, "timeout_seconds": timeout or 300}
        else:
            window, timeout = window or 600, timeout or 120
            if not (account and host and sender):
                raise EntryInvalid("--account, --host and --sender are required")
            cfg: dict[str, Any] = {"account": account, "host": host, "port": port, "sender": sender,
                                   "mailbox": mailbox, "window_seconds": window, "timeout_seconds": timeout}
            if pattern:
                cfg["pattern"] = pattern
            config["catcher"] = cfg
        meta = store.update(_master(store), meta.id, config=config)
    if relay and not off:
        typer.echo(f"{meta.name!r} code source: the code card (the owner types the code in the panel).")
        return
    state = "off" if off else f"on ({meta.config['catcher']['host']}, from {meta.config['catcher']['sender']})"
    typer.echo(f"{meta.name!r} code catcher: {state}.")


@app.command("rotate-key")
def rotate_key(master: bool = typer.Option(
        False, "--master", help="Also replace the master key (a new key file, or a new passphrase).")) -> None:
    """Re-seal every entry under a fresh data key; with --master, replace the master key too."""
    store = _store()
    with _errors():
        old = _master(store)
        header = store.header()
        assert header is not None
        version = store.rotate_data_key(old)
        typer.echo(f"Data key rotated (version {version}).")
        if not master:
            return
        if header.mode == "passphrase":
            secret = _new_passphrase()
            kdf = crypto.new_kdf(KDF_PARAMS)
            store.rewrap(old, crypto.derive_master(secret, kdf), mode="passphrase", kdf=kdf)
            typer.echo("Passphrase changed. A running daemon must be unsealed again with the new passphrase.")
            return
        keyfile = keyfile_path(_home())
        pending = pending_keyfile_path(keyfile)
        new_key = os.urandom(crypto.KEY_LEN)
        crypto.write_keyfile(pending, new_key)
        store.rewrap(old, new_key, mode="keyfile", kdf=None)
        os.replace(pending, keyfile)
        crypto.fsync_dir(keyfile.parent)
        typer.echo(f"New key file written to {keyfile}. Replace any backup of the old one.")


# ---- import ---------------------------------------------------------------------

@app.command("import")
def import_(
    file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="The export file.")],
    fmt: str = typer.Option("auto", "--format", help="auto, apple, bitwarden, 1password or csv."),
    bind_rules: Annotated[list[str] | None, typer.Option(
        "--bind", help="domain=lane, e.g. instagram.com=biz-a/instagram.")] = None,
    dry_run: bool = typer.Option(False, "--dry-run", help="Only show what would be imported."),
    only_bound: bool = typer.Option(False, "--only-bound", help="Skip entries no --bind rule covers."),
    on_conflict: str = typer.Option("skip", "--on-conflict", help="skip, rename or replace same-named entries."),
    shred_source: bool = typer.Option(False, "--shred", help="Overwrite and delete the export file afterwards."),
) -> None:
    """Import an Apple Passwords, Bitwarden (unencrypted JSON), 1Password or generic CSV export."""
    store = _store()
    with _errors():
        if on_conflict not in ("skip", "rename", "replace"):
            raise EntryInvalid("--on-conflict must be skip, rename or replace")
        detected, plan = parse_file(file, fmt)
        bindings = parse_bindings(bind_rules or [])
        existing = {e.name for e in store.list()} if store.header() is not None else set()
        summary = plan.summary(bindings, only_bound=only_bound, existing=existing)
        _print_summary(detected, summary, dry_run=dry_run)
        if dry_run:
            return
        master = _master(store)
        entries, _ = plan.entries(bindings, only_bound=only_bound)
        result = store.add_many(master, entries, on_conflict=on_conflict)
    typer.echo(f"Imported {result['added']} entries (existing skipped {result['skipped']}, "
               f"renamed {result['renamed']}, replaced {result['replaced']}).")
    if shred_source:
        try:
            shred(file)
        except OSError as e:
            _fail(f"imported, but could not shred {file}: {e.strerror}", "error")
        typer.echo(f"Shredded {file} (best effort: SSDs, snapshots and backups may still hold copies).")
    else:
        typer.echo(f"{file} still holds every password in plain text. Delete it, or re-run with --shred.")


def _print_summary(fmt: str, s: dict[str, Any], *, dry_run: bool) -> None:
    verb = "Would import" if dry_run else "Importing"
    kinds = ", ".join(f"{k} {n}" for k, n in sorted(s["kinds"].items())) or "nothing"
    typer.echo(f"Format: {fmt}")
    typer.echo(f"{verb} {s['importable']} entries: {kinds} ({s['with_totp']} with TOTP).")
    typer.echo(f"Bound: {s['bound']}, unbound: {s['unbound']} (unbound entries resolve nowhere until bound).")
    if s["conflicts"]:
        typer.echo(f"Same name as an existing entry: {s['conflicts']}.")
    if s["skipped"]:
        typer.echo("Skipped: " + ", ".join(f"{r} {n}" for r, n in sorted(s["skipped"].items())) + ".")
    if s["notes_not_imported"]:
        typer.echo(f"Notes on {s['notes_not_imported']} logins were not imported.")
    if s["domains"]:
        typer.echo("Domains:")
        width = max(len(_domain_label(d)) for d in s["domains"])
        for d, info in s["domains"].items():
            lanes = ", ".join(info["lanes"]) if info["lanes"] else "(unbound)"
            typer.echo(f"  {_domain_label(d):<{width}}  {info['count']:>4}  {lanes}")
