"""Secrets the app in an environment uses, with the value never on the VM.

The owner binds a vault API key to environments at the node (`openajar environments secret add
STRIPE_SECRET_KEY --host api.stripe.com [--repo github.com/me/shop]`). It is an ordinary api_key
entry of the vault: its domain and hosts are the hosts it may go to, its lanes are every environment
(`env/*`), and its config names the variable (`env.var`) and, when given, the one repository it is
for (`env.repo`). Storing it is the permission: using it asks nobody. Only the owner sets one: no
tool takes a binding, so an agent never can.

On the machine the variable holds a placeholder (openajar.environments.networking); the node's swap
(openajar.environments.swap) puts the value in on the way to the bound host only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openajar.environments.models import WORKSPACE

ENTRY_PREFIX = "env "


@dataclass(frozen=True)
class Bound:
    var: str
    hosts: tuple[str, ...]
    ref: str                 # the vault entry's id
    repo: str | None = None


def entry_name(var: str, repo: str | None) -> str:
    return f"{ENTRY_PREFIX}{var}" + (f" for {repo}" if repo else "")


def _bound(meta: Any) -> Bound | None:
    env = (getattr(meta, "config", None) or {}).get("env")
    kind = getattr(getattr(meta, "kind", None), "value", getattr(meta, "kind", None))
    if not isinstance(env, dict) or kind != "api_key" or not env.get("var"):
        return None
    hosts = tuple(getattr(meta, "hosts", None) or ([meta.domain] if getattr(meta, "domain", None) else []))
    return Bound(var=str(env["var"]), hosts=hosts, ref=str(meta.id), repo=env.get("repo"))


async def all_bound(vault: Any) -> list[Bound]:
    """Every secret bound to environments (names and hosts only: no value is read)."""
    if vault is None:
        return []
    return [b for b in (_bound(m) for m in await vault.list_entries()) if b is not None]


async def bound_for(vault: Any, repo: str | None) -> list[Bound]:
    """The secrets an environment with `repo` gets: one per variable, the one for its repository first."""
    out: dict[str, Bound] = {}
    for b in await all_bound(vault):
        if b.repo is not None and b.repo != repo:
            continue
        have = out.get(b.var)
        if have is None or (have.repo is None and b.repo is not None):
            out[b.var] = b
    return sorted(out.values(), key=lambda b: b.var)


async def add(vault: Any, var: str, value: Any, hosts: list[str], *, repo: str | None = None,
              replace: bool = False) -> Any:
    """Store the value in the vault, bound to these hosts and to every environment (or `repo`'s)."""
    from openajar.vault.domains import host_of, registrable_domain

    if not hosts:
        from openajar.core.errors import InvalidRequest

        raise InvalidRequest("name the host it goes to with --host, like --host api.stripe.com")
    domain = registrable_domain(host_of(hosts[0])) or hosts[0]
    return await vault.add_entry(name=entry_name(var, repo), kind="api_key", domain=domain, hosts=list(hosts),
                                 lanes=[f"{WORKSPACE}/*"], secret=value, config={"env": {"var": var, "repo": repo}},
                                 replace=replace)


async def remove(vault: Any, var: str, *, repo: str | None = None) -> Any:
    for b in await all_bound(vault):
        if b.var == var and b.repo == repo:
            return await vault.remove_entry(b.ref)
    from openajar.core.errors import NotFound

    raise NotFound(f"no secret {var}{' for ' + repo if repo else ''} is bound to environments; "
                   "`openajar environments secret list` shows them")
