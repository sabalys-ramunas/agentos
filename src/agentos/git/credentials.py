"""Storing a git credential in the vault: a token a person gave, or a deploy key made here.

`agentos git key add`, `agentos environments repo add` and the panel's repository card
(agentos.environments.connect) all store one this way, so a credential is the same thing however
it arrived. A token is a `Secret` from the moment it is read, and only its name is ever returned.
"""

from __future__ import annotations

from typing import Any

from agentos.core.errors import InvalidRequest
from agentos.git import keys as keymod
from agentos.git.wire import normalize_host
from agentos.vault.secret import Secret


async def git_credentials(vault: Any) -> list[keymod.Credential]:
    """The git credentials in the vault: names, hosts, repositories, never a value."""
    return [c for c in (keymod.from_meta(m) for m in await vault.list_secrets()) if c is not None]


def deploy_key_hint(host: str, repos: list[str]) -> str:
    """Where a repository's admin adds a deploy key."""
    if host == "github.com" and not repos[0].endswith("/*"):
        return f"https://github.com/{repos[0]}/settings/keys/new"
    if host == "gitlab.com" and not repos[0].endswith("/*"):
        return f"https://gitlab.com/{repos[0]}/-/settings/repository (Deploy keys)"
    return f"the repository's deploy keys on {host}"


def _name(host: str, repos: list[str], kind: str, name: str | None) -> tuple[str, list[str], str]:
    h = normalize_host(host)
    rs = keymod.check_repos(repos)
    return h, rs, keymod.check_name(name) if name else keymod.default_name(h, rs, kind)


async def _free(vault: Any, ref: str, cname: str, replace: bool) -> None:
    if not replace and ref in {c.ref for c in await git_credentials(vault)}:
        raise InvalidRequest(f"a credential named {cname} exists; pick another --name, or pass --replace")


async def put_token(vault: Any, host: str, repos: list[str], token: Secret, *, name: str | None = None,
                    username: str | None = None, replace: bool = False) -> dict[str, Any]:
    """Seal a token in the vault, bound to `host` and `repos`. Returns its name, never the token."""
    h, rs, cname = _name(host, repos, keymod.TOKEN_KIND, name)
    ref = keymod.REF_PREFIX + cname
    await _free(vault, ref, cname, replace)
    secret = keymod.check_token(token.reveal())
    scope = keymod.token_scope(h, rs, username)
    await vault.put_secret(ref, secret, kind=keymod.TOKEN_KIND, label=f"git token for {h}", scope=scope)
    return {"name": cname, "type": "token", "host": h, "repos": rs}


async def put_deploy_key(vault: Any, host: str, repos: list[str], *, name: str | None = None,
                         replace: bool = False) -> dict[str, Any]:
    """Make an Ed25519 deploy key here and seal its private half in the vault. Returns the public half
    and its fingerprint, to add at the host. The host's SSH key must already be known (a pinned host,
    or one `agentos git key add` recorded)."""
    h, rs, cname = _name(host, repos, keymod.SSH_KIND, name)
    ref = keymod.REF_PREFIX + cname
    await _free(vault, ref, cname, replace)
    private, public, fp = keymod.generate_ed25519(f"agentos-{cname}")
    scope = keymod.ssh_scope(h, rs, public)
    await vault.put_secret(ref, private, kind=keymod.SSH_KIND, label=f"git deploy key for {h}", scope=scope)
    return {"name": cname, "type": "ssh", "host": h, "repos": rs, "public_key": public, "fingerprint": fp}
