"""`agentos git ...`: deploy keys and tokens in the vault, and the remotes lanes may use.

Run on the node, as the owner. A deploy key is made here and only its public half is printed: paste
it into the repository's deploy keys. A token is read with getpass (or from stdin with --stdin),
never from argv. Nothing here prints a private key or a token, and nothing exports one.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import sys
from typing import Annotated, Any, NoReturn

import typer

from agentos.core import catalog
from agentos.core.config import home_from_env
from agentos.core.errors import AgentosError, InvalidRequest
from agentos.git import keys as keymod
from agentos.git.remotes import Remotes
from agentos.git.wire import normalize_host

app = typer.Typer(no_args_is_help=True, help=(
    "Git credentials for lanes and brains: deploy keys and tokens kept in the vault, and the remotes "
    "each workspace or lane may fetch from and push to. See docs/GIT.md."))
key_app = typer.Typer(no_args_is_help=True, help="Deploy keys and tokens in the vault (never exported).")
remote_app = typer.Typer(no_args_is_help=True, help="The remotes a workspace or a lane may use.")
app.add_typer(key_app, name="key")
app.add_typer(remote_app, name="remote")


def _fail(message: str, code: str = "invalid") -> NoReturn:
    typer.echo(catalog.cli_error(code, message), err=True)
    raise typer.Exit(1)


def _vault() -> Any:
    from agentos.cli import cli_runtime

    vault = cli_runtime().get("vault")
    if vault is None:
        _fail("this node has no vault yet; run `agentos vault init` first", "unavailable")
    return vault


def _remotes() -> Remotes:
    return Remotes(home_from_env() / "git" / "remotes.json")


def _run(coro: Any) -> Any:
    try:
        return asyncio.run(coro)
    except AgentosError as e:
        _fail(e.message, e.code)


async def _credentials(vault: Any) -> list[keymod.Credential]:
    return [c for c in (keymod.from_meta(m) for m in await vault.list_secrets()) if c is not None]


def _deploy_key_hint(host: str, repos: list[str]) -> str:
    if host == "github.com" and not repos[0].endswith("/*"):
        return f"https://github.com/{repos[0]}/settings/keys/new"
    if host == "gitlab.com" and not repos[0].endswith("/*"):
        return f"https://gitlab.com/{repos[0]}/-/settings/repository (Deploy keys)"
    return f"the repository's deploy keys on {host}"


@key_app.command("add")
def key_add(
    host: Annotated[str, typer.Option("--host", help="The git host, for example github.com.")],
    repo: Annotated[list[str], typer.Option("--repo", help="owner/name (repeat for more), or owner/* for all.")],
    token: Annotated[bool, typer.Option("--token", help="Store a token (fine-grained PAT) instead of making an SSH key.")] = False,
    name: Annotated[str | None, typer.Option("--name", help="A short name for the credential.")] = None,
    username: Annotated[str | None, typer.Option("--username", help="The user a token is sent as (default x-access-token).")] = None,
    stdin: Annotated[bool, typer.Option("--stdin", help="Read the token from stdin instead of a prompt.")] = False,
    port: Annotated[int | None, typer.Option("--port", help="The host's SSH port, when it is not 22.")] = None,
    host_key: Annotated[str | None, typer.Option("--host-key", help="The host's SSH key (`ssh-ed25519 AAAA...`), "
                                                 "instead of reading it from the host.")] = None,
    replace: Annotated[bool, typer.Option("--replace", help="Replace a credential of the same name.")] = False,
) -> None:
    """Make a deploy key on the node (default) or store a token, bound to a host and repositories.

    The private key is generated here, stored sealed in the vault and never printed or exported;
    only the public key is shown, to add as a deploy key (with write access to push)."""
    out = make_credential(host, repo, token=token, name=name, username=username, stdin=stdin, port=port,
                          host_key=host_key, replace=replace)
    echo_credential(out, pushers="lanes")
    typer.echo(f"Then declare where it may be used: agentos git remote allow <workspace or lane> "
               f"{out['host']}/{out['repos'][0]}")


def make_credential(host: str, repo: list[str], *, token: bool = False, name: str | None = None,
                    username: str | None = None, stdin: bool = False, port: int | None = None,
                    host_key: str | None = None, replace: bool = False) -> dict[str, Any]:
    """`agentos git key add` without its output (agentos environments repo add makes one too): a
    deploy key made here, or a token read from a prompt or stdin, sealed in the vault. Returns what
    may be shown: name, type, host, repos, and a deploy key's public half and fingerprint."""
    try:
        h = normalize_host(host)
        repos = keymod.check_repos(repo)
        kind = keymod.TOKEN_KIND if token else keymod.SSH_KIND
        cname = keymod.check_name(name) if name else keymod.default_name(h, repos, kind)
    except InvalidRequest as e:
        _fail(e.message)
    ref = keymod.REF_PREFIX + cname
    vault = _vault()

    async def go() -> dict[str, Any]:
        existing = {c.ref for c in await _credentials(vault)}
        if ref in existing and not replace:
            raise InvalidRequest(f"a credential named {cname} exists; pick another --name, or pass --replace")
        if token:
            raw = sys.stdin.readline() if stdin else getpass.getpass(f"Token for {h} ({', '.join(repos)}): ")
            secret = keymod.check_token(raw)
            scope = keymod.token_scope(h, repos, username)
            await vault.put_secret(ref, secret.reveal(), kind=kind, label=f"git token for {h}", scope=scope)
            return {"name": cname, "type": "token", "host": h, "repos": repos}
        if h not in keymod.KNOWN_HOSTS or port not in (None, 22):
            _keep_host_key(h, port, host_key)
        private, public, fp = keymod.generate_ed25519(f"agentos-{cname}")
        scope = keymod.ssh_scope(h, repos, public)
        await vault.put_secret(ref, private.reveal(), kind=kind, label=f"git deploy key for {h}", scope=scope)
        return {"name": cname, "type": "ssh", "host": h, "repos": repos, "public_key": public, "fingerprint": fp}

    return _run(go())


def vault_credentials() -> list[keymod.Credential]:
    """The git credentials in the vault (never a secret), for other commands of the node's CLI."""
    return _run(_credentials(_vault()))


def remove_credential(cred: keymod.Credential) -> None:
    _run(_vault().delete_secret(cred.ref))


def deploy_key_hint(host: str, repos: list[str]) -> str:
    return _deploy_key_hint(host, repos)


def echo_credential(out: dict[str, Any], *, pushers: str) -> None:
    """What make_credential made: a token's name, or the deploy key to paste and where."""
    h, repos = out["host"], out["repos"]
    if out["type"] == "token":
        typer.echo(f"Stored token {out['name']} for {h} ({', '.join(repos)}) in the vault.")
        return
    typer.echo(f"Made deploy key {out['name']} ({out['fingerprint']}) for {h} ({', '.join(repos)}).")
    typer.echo(f"Add this public key as a deploy key, with write access if {pushers} should push:")
    typer.echo(f"  {_deploy_key_hint(h, repos)}")
    typer.echo("")
    typer.echo(out["public_key"])
    typer.echo("")
    typer.echo("The private key stays sealed in the vault; nothing can export it.")


def _keep_host_key(host: str, port: int | None, given: str | None) -> None:
    pat = keymod.host_patterns(host, port)[0]
    if given:
        parts = given.split()
        if parts and not parts[0].startswith(("ssh-", "ecdsa-")):
            parts = parts[1:]  # a known_hosts line: the host first
        if len(parts) < 2:
            _fail("--host-key is a key line: ssh-ed25519 AAAA...")
        try:
            keymod.fingerprint(f"{parts[0]} {parts[1]}")
        except InvalidRequest:
            _fail("--host-key is a key line: ssh-ed25519 AAAA...")
        lines = [f"{pat} {parts[0]} {parts[1]}"]
    else:
        lines = keymod.scan_host_keys(host, port)
        if not lines:
            _fail(f"could not read the SSH host key of {host}; pass it with --host-key (from the host's docs)",
                  "unavailable")
    for line in lines:
        typer.echo(f"Host key of {pat}: {line.split()[1]} {keymod.fingerprint(' '.join(line.split()[1:3]))}")
    typer.echo("Compare these fingerprints with the ones your git host publishes before you push.")
    keymod.add_known_hosts(home_from_env(), lines)


@key_app.command("list")
def key_list(as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False) -> None:
    """The git credentials in the vault: names, hosts, repositories, public keys. Never a secret."""
    creds = _run(_credentials(_vault()))
    if as_json:
        typer.echo(json.dumps([c.public() for c in creds], indent=1))
        return
    if not creds:
        typer.echo("No git credentials. Make one: agentos git key add --host github.com --repo owner/name")
        return
    for c in creds:
        used = f"used {c.use_count} times, last {c.last_used_at}" if c.use_count else "never used"
        typer.echo(f"{c.name}: {c.type} for {c.host} ({', '.join(c.repos)}), {used}")
        if c.public_key:
            typer.echo(f"  {c.fingerprint}  {c.public_key}")


@key_app.command("show")
def key_show(name: Annotated[str, typer.Argument(help="The credential's name.")]) -> None:
    """Print a deploy key's public key again (to paste into the host)."""
    creds = {c.name: c for c in _run(_credentials(_vault()))}
    c = creds.get(name)
    if c is None:
        _fail(f"no git credential named {name}; `agentos git key list` shows them", "not_found")
    if not c.public_key:
        _fail(f"{name} is a token; it has no public key")
    typer.echo(c.public_key)


@key_app.command("remove")
def key_remove(name: Annotated[str, typer.Argument(help="The credential's name.")]) -> None:
    """Delete a credential from the vault. Also remove the deploy key (or revoke the token) at the host."""
    vault = _vault()
    creds = {c.name: c for c in _run(_credentials(vault))}
    c = creds.get(name)
    if c is None:
        _fail(f"no git credential named {name}; `agentos git key list` shows them", "not_found")
    _run(vault.delete_secret(c.ref))
    typer.echo(f"Removed {name} from the vault. Remove it at {c.host} too (the deploy key or the token).")


@remote_app.command("allow")
def remote_allow(
    scope: Annotated[str, typer.Argument(help="A workspace (personal) or a lane (personal/code).")],
    remote: Annotated[str, typer.Argument(help="host/owner/name, host/owner/*, or a remote URL.")],
    read_only: Annotated[bool, typer.Option("--read-only", help="Fetch only, no pushes.")] = False,
    protect: Annotated[list[str] | None, typer.Option("--protect", help="Another protected branch or glob "
                                                      "(a push there always asks).")] = None,
    allow_default_branch: Annotated[bool, typer.Option("--allow-default-branch", help="A push to the default "
                                                       "branch is a plain commit, not one that always asks.")] = False,
    url: Annotated[str | None, typer.Option("--url", help="The real remote, when it is not the host's usual "
                                            "address (ssh://git@host:2222/owner/name.git).")] = None,
) -> None:
    """Let a workspace or a lane fetch from (and push to) a remote through the node."""
    try:
        rule = _remotes().allow(scope, remote, push=not read_only, protect=protect or [],
                                allow_default_branch=allow_default_branch, url=url)
    except AgentosError as e:
        _fail(e.message, e.code)
    typer.echo(f"Declared {rule.describe()}.")
    if not rule.on_lane and rule.push:
        typer.echo("A push from a lane is a commit on that lane (its Ask me first decides); a brain task's push "
                   "with no lane always asks. Declare it on a lane to let a brain given that lane push on its own.")


@remote_app.command("list")
def remote_list(
    scope: Annotated[str | None, typer.Argument(help="A workspace or a lane (all when left out).")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """The declared remotes."""
    try:
        rules = _remotes().list(scope)
    except AgentosError as e:
        _fail(e.message, e.code)
    if as_json:
        typer.echo(json.dumps([r.model_dump() for r in rules], indent=1))
        return
    if not rules:
        typer.echo("No remotes declared. Declare one: agentos git remote allow <workspace> github.com/owner/name")
        return
    for r in rules:
        typer.echo(r.describe())


@remote_app.command("deny")
def remote_deny(
    scope: Annotated[str, typer.Argument(help="The workspace or lane it was declared for.")],
    remote: Annotated[str, typer.Argument(help="host/owner/name, host/owner/* or a remote URL.")],
) -> None:
    """Remove a declared remote: lanes and brains can no longer fetch from or push to it."""
    try:
        rule = _remotes().deny(scope, remote)
    except AgentosError as e:
        _fail(e.message, e.code)
    typer.echo(f"Removed {rule.slug} for {rule.scope}.")
