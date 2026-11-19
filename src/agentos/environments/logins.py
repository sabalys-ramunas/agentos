"""Logins for agent environments: the owner binds a vault login to computers, and the agent signs in.

The owner binds at the node (`agentos environments login add github.com --user me --title "Shop fix"`,
or `--repo github.com/me/shop`, or `--all`). It is an ordinary password entry of the vault, bound to
every environment's lanes (`env/*`), whose config `envs` names the environments it covers (`scopes`:
a title, a repository, or all), the username the owner's list shows and the login page the node opens
(`user`, `url`), and whether this command made the entry (`made`: it goes with its last binding). An existing vault login
can be bound instead (`--entry`). The vault's own check (agentos.vault.policy.entry_bound) reads the
lane's environment from the node's records (`lane_facts`). A binding by title is the owner's word about
one computer: `title_scope` records, when the owner binds, the grant family whose computer has that title
(open or saved), and the binding covers that family's computer of that title only, across its saves and
resumes (the id changes; the title and the family do not). Another agent's computer with the same title
gets nothing, since a title is the agent's own choice. A binding by repository or `--all` names no family. Storing and binding it is the permission:
signing in asks nobody. Only the owner binds: no tool takes a binding, and the entry's metadata is
sealed with the vault key.

The agent signs in with `login` on its environment's browser (`env_login`): the node opens the login
page when the page is not on the bound login's site, then fills it with the vault's login flow
(agentos.vault.login), the model never seeing a value. `environment()` and `environment_open` list the
logins by name and site (a login is named after its site unless the owner names it). Not by username:
the vault scrubs every stored username from what an agent reads. The owner signs it in again from the node with
`agentos environments login <title> <site>` (`sign_in_now`), when the agent is not running.

Typing happens where the browser is: with browser = "vm" the value goes into the VM's browser over
the connection the node drives, so it passes through that browser's process, and the session cookies
stay in it and in its saves (encrypted, encrypt_saves). docs/THREAT-MODEL.md says what that means.
While a fill runs no replay frame is taken (`filling`).
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from agentos.core.errors import InvalidRequest, NotFound
from agentos.environments.models import WORKSPACE

log = logging.getLogger("agentos.environments")

ENV_LANES = f"{WORKSPACE}/*"
SETTLE_S = 5.0        # after opening the login page, before the fill reads it (at most)


@dataclass(frozen=True)
class BoundLogin:
    meta: Any                                 # the vault's EntryMeta (no value)
    user: str | None
    url: str | None
    scopes: tuple[dict, ...]
    made: bool

    @property
    def ref(self) -> str:
        return str(self.meta.id)

    @property
    def name(self) -> str:
        return str(self.meta.name)

    @property
    def site(self) -> str:
        return str(self.meta.domain or "")

    @property
    def login_url(self) -> str:
        return self.url or f"https://{self.site}/"

    def public(self) -> dict[str, Any]:
        """What the agent sees: the login's name and site, never a secret. Not its username: the vault
        scrubs every stored username from what an agent reads, so it would only read [redacted]."""
        return {"name": self.name, "site": self.site, "totp": bool(self.meta.has_totp)}

    def owner_view(self) -> dict[str, Any]:
        """What the owner's `login list` shows: the username too (not a secret to the owner)."""
        return {**self.public(), "user": self.user}


def scope_of(title: str | None, repo: str | None, all_: bool) -> dict[str, Any]:
    """The one scope a binding names: --title, --repo or --all."""
    given = [k for k, v in (("title", title), ("repo", repo), ("all", all_)) if v]
    if len(given) != 1:
        raise InvalidRequest("name the computers with one of --title \"<environment title>\", --repo <repository> "
                             "or --all")
    if all_:
        return {"all": True}
    if repo:
        from agentos.git.wire import parse_remote

        return {"repo": parse_remote(repo).slug}
    return {"title": " ".join(str(title).split())}


def title_families(mgr: Any, title: str) -> dict[str, list[Any]]:
    """The computers titled `title`, by grant family: its open environments and its saves."""
    from agentos.environments.picking import same_title

    out: dict[str, list[Any]] = {}
    for env in mgr.store.open():
        if same_title(env.title, title):
            out.setdefault(env.family, []).append(env)
    for m in mgr.saves().all():
        if same_title(m.title, title):
            out.setdefault(m.family, []).append(m)
    return out


def title_scope(mgr: Any, title: str) -> dict[str, Any]:
    """The scope of `--title`: the title and the one grant family whose computer has it now (an open
    environment or a save; `title` may also be an open environment's id). InvalidRequest, with the fix,
    when no computer has that title yet or several agents' computers do."""
    title = " ".join(str(title).split())
    with contextlib.suppress(ValueError, KeyError, NotFound):
        if mgr.store.has(title):
            env = mgr.store.get(title)
            return {"title": env.title, "family": env.family, "agent": env.agent}
    found = title_families(mgr, title)
    if not found:
        raise InvalidRequest(f"no computer is titled \"{title}\" yet, so this binding would name none. Open it first "
                             f"(the agent's environment_open(title=\"{title}\")) and bind it then, or bind it to "
                             "a repository with --repo, or to every computer with --all")
    if len(found) > 1:
        ids = "; ".join(f"{(rows[0].agent or 'an agent')}: {', '.join(str(r.id) for r in rows[:3])}"
                        for rows in found.values())
        raise InvalidRequest(f"computers of {len(found)} agents are titled \"{title}\" ({ids}). Bind the one you mean "
                             "by its environment id instead of the title (--title <id>, while it is open)")
    family, rows = next(iter(found.items()))
    return {"title": title, "family": family, "agent": rows[0].agent or "an agent"}


def same_scope(a: dict, b: dict) -> bool:
    """Do two scopes name the same computers? A title is one, whatever family it was recorded with."""
    if a.get("title") or b.get("title"):
        from agentos.environments.picking import same_title

        return bool(a.get("title") and b.get("title")) and same_title(str(a["title"]), str(b["title"]))
    return a == b


def scope_words(scope: dict, *, stored: bool = True) -> str:
    """A scope in words. `stored`: one a binding holds (a title there without a family covers nothing);
    else what the owner typed (`remove --title`)."""
    if scope.get("all"):
        return "every environment"
    if scope.get("repo"):
        return f"environments of {scope['repo']}"
    if stored and scope.get("title") and not scope.get("family"):
        return f"no computer (\"{scope['title']}\" was bound before bindings named the agent: bind it again)"
    who = f" of {scope['agent']}" if scope.get("agent") else ""
    return f"the environment titled \"{scope.get('title')}\"{who}"


def parse_site(site: str) -> tuple[str, str | None]:
    """(the vault domain, the login page) for a site the owner names: a domain (github.com) or the
    login page's address (https://github.com/login, http://localhost:8080/login)."""
    from agentos.vault.domains import host_of, registrable_domain

    raw = site.strip()
    url = raw if raw.startswith(("https://", "http://")) else None
    try:
        domain = registrable_domain(host_of(raw))
    except ValueError:
        domain = None
    if not domain:
        raise InvalidRequest(f"{site!r} is not a site: give a domain like github.com, or the login page's address")
    return domain, url


def _bound(meta: Any) -> BoundLogin | None:
    envs = (getattr(meta, "config", None) or {}).get("envs")
    kind = getattr(getattr(meta, "kind", None), "value", getattr(meta, "kind", None))
    if kind != "password" or not isinstance(envs, dict):
        return None
    return BoundLogin(meta=meta, user=envs.get("user"), url=envs.get("url"),
                      scopes=tuple(envs.get("scopes") or ()), made=bool(envs.get("made")))


def _any_login(meta: Any) -> BoundLogin:
    """A password entry bound to an environment's lane some other way (`agentos vault bind`)."""
    return _bound(meta) or BoundLogin(meta=meta, user=None, url=None, scopes=({"all": True},), made=False)


async def all_bound(vault: Any) -> list[BoundLogin]:
    """Every login bound to environments with `login add` (names and scopes only: no value is read)."""
    if vault is None:
        return []
    return [b for b in (_bound(m) for m in await vault.list_entries()) if b is not None]


async def bound_for(vault: Any, lane_id: str) -> list[BoundLogin]:
    """The logins this environment's browser may use, as the vault decides it (entry_bound)."""
    if vault is None:
        return []
    from agentos.vault.models import EntryKind

    return [_any_login(m) for m in await vault.list_entries(lane=lane_id) if m.kind is EntryKind.PASSWORD]


def find(logins: list[BoundLogin], ref: str) -> list[BoundLogin]:
    """The logins a name, an entry id or a site names (a site names each login of it)."""
    ref = ref.strip()
    exact = [b for b in logins if ref in (b.name, b.ref)]
    if exact:
        return exact
    try:
        domain, _url = parse_site(ref)
    except InvalidRequest:
        return []
    return [b for b in logins if b.site == domain]


def entry_name(domain: str, name: str | None = None) -> str:
    """The login's name: the site, or the owner's own (--name) for a second login of one site."""
    return " ".join(name.split())[:80] if name and name.strip() else domain


async def add(vault: Any, site: str, *, user: str | None, password: Any, totp: Any = None,
              scope: dict, replace: bool = False, name: str | None = None) -> BoundLogin:
    """Store a login in the vault, bound to the environments of `scope`."""
    domain, url = parse_site(site)
    config = {"envs": {"scopes": [scope], "user": user, "url": url, "made": True}}
    meta = await vault.add_entry(name=entry_name(domain, name), kind="password", domain=domain, lanes=[ENV_LANES],
                                 username=user, secret=password, totp=totp, config=config, replace=replace)
    return _any_login(meta)


async def existing(vault: Any, site: str, user: str | None, name: str | None = None) -> BoundLogin | None:
    """The login `add` made with this name, if there is one; InvalidRequest when that name is another
    username's login (the owner names this one with --name) or a vault entry `add` did not make."""
    from agentos.vault.errors import EntryNotFound

    domain, _url = parse_site(site)
    named = entry_name(domain, name)
    try:
        meta = await vault.get_entry(named)
    except EntryNotFound:
        return None
    found = _bound(meta)
    if found is None or not found.made:
        raise InvalidRequest(f"the vault has an entry named {named!r} already: bind it with --entry \"{named}\", "
                             "or give this login another name with --name")
    if found.site != domain or (user or None) != (found.user or None):
        raise InvalidRequest(f"a login named {named!r} is stored already for another username or site; give this "
                             "one a name of its own with --name")
    return found


async def bind(vault: Any, ref: str, scope: dict, *, user: str | None = None, url: str | None = None) -> BoundLogin:
    """Bind a login already in the vault (by name or id) to the environments of `scope` too."""
    from agentos.vault.errors import EntryNotFound

    try:
        meta = await vault.get_entry(ref)
    except EntryNotFound:
        raise NotFound(f"no vault entry named {ref!r}; `agentos vault list` shows them") from None
    if getattr(meta.kind, "value", meta.kind) != "password":
        raise InvalidRequest(f"{meta.name!r} is a {getattr(meta.kind, 'value', meta.kind)} entry; only a login "
                             "(a password entry) signs in")
    config = dict(meta.config or {})
    envs = dict(config.get("envs") or {"scopes": [], "user": None, "url": None, "made": False})
    envs["scopes"] = [*envs.get("scopes", []), scope]
    envs["user"] = user or envs.get("user")
    envs["url"] = url or envs.get("url")
    config["envs"] = envs
    await vault.set_config(meta.id, config)
    return _any_login(await vault.bind(meta.id, [ENV_LANES]))


async def remove(vault: Any, ref: str, scope: dict | None = None) -> list[BoundLogin]:
    """Take a login's binding off environments: the one of `scope`, or all of them. A login `add` made
    leaves the vault with its last binding; one bound with --entry stays in the vault, unbound."""
    found = find(await all_bound(vault), ref)
    if not found:
        raise NotFound(f"no login {ref!r} is bound to environments; `agentos environments login list` shows them")
    out = []
    for b in found:
        if scope is not None and not any(same_scope(scope, s) for s in b.scopes):
            continue
        left = [s for s in b.scopes if scope is not None and not same_scope(scope, s)]
        if left:
            config = dict(b.meta.config)
            config["envs"] = {**config["envs"], "scopes": left}
            await vault.set_config(b.ref, config)
        elif b.made:
            await vault.remove_entry(b.ref)
        else:
            config = {k: v for k, v in b.meta.config.items() if k != "envs"}
            await vault.set_config(b.ref, config)
            await vault.unbind(b.ref, [ENV_LANES])
        out.append(b)
    if not out:
        raise NotFound(f"{ref!r} is not bound to {scope_words(scope or {})}; `agentos environments login list` "
                       "shows its bindings")
    return out


class Logins:
    """For EnvironmentManager (its store, runtime, settings, _get, _clock and the replay's _track)."""

    def logins_install(self) -> None:
        """The vault reads each environment lane's title and repository from here (entry_bound)."""
        vault = self._get("vault")  # type: ignore[attr-defined]
        if vault is not None and hasattr(vault, "lane_facts"):
            vault.lane_facts = self.lane_facts

    def lane_facts(self, lane_id: str) -> dict[str, Any] | None:
        """{title, repo, family} of the open environment whose lane this is, else None."""
        found = self.env_of_lane(lane_id)  # type: ignore[attr-defined]
        if found is None or not found[0].open:
            return None
        env = found[0]
        return {"title": env.title, "repo": env.repo, "family": env.family}

    async def login_names(self, env: Any) -> list[dict[str, Any]]:
        """The logins the environment's browser may use, by name (environment() and environment_open)."""
        try:
            return [b.public() for b in await bound_for(self._get("vault"), env.browser_lane)]  # type: ignore[attr-defined]
        except Exception:
            log.warning("environment %s: could not list its logins", env.id, exc_info=True)
            return []

    @contextlib.contextmanager
    def filling(self, env_id: str) -> Iterator[None]:
        """While a fill runs on the environment's browser, the replay takes no frame of it."""
        track = self._track(env_id)  # type: ignore[attr-defined]
        track.filling += 1
        try:
            yield
        finally:
            track.filling -= 1

    async def _login_target(self, env: Any, url_now: str, entry: str | None) -> tuple[BoundLogin | None, bool]:
        """(the login to fill, or None to let the vault pick for this page; whether the browser is off its
        site, so its login page opens first) for a sign-in on this browser."""
        from agentos.vault.policy import check_origin

        logins = await bound_for(self._get("vault"), env.browser_lane)  # type: ignore[attr-defined]
        if entry is not None:
            named = find(logins, entry)
            if len(named) != 1:
                raise NotFound(_none_words(env, logins, entry) if not named else _several_words(named))
            return named[0], not check_origin(named[0].meta, url_now).allowed
        here = [b for b in logins if check_origin(b.meta, url_now).allowed]
        if here:  # on a bound site already
            return (here[0] if len(here) == 1 else None), False
        if len(logins) == 1:
            return logins[0], True
        if not logins:
            raise NotFound(_none_words(env, logins, None))
        raise InvalidRequest(_several_words(logins))

    async def env_login(self, ctx: Any, vault: Any, typist: Any, page: Any, *, lane_id: str,
                        entry: str | None = None) -> dict[str, Any] | None:
        """`login` on an environment's browser: open the bound login's page when the browser is not on its
        site (or is, on a page with no login form), then fill it. None: not an environment's browser (the
        caller runs the plain login)."""
        found = self.env_of_lane(lane_id)  # type: ignore[attr-defined]
        if found is None or found[1] != "browser":
            return None
        env = found[0]
        chosen, off_site = await self._login_target(env, str(getattr(page, "url", "") or ""), entry)
        ref = chosen.ref if chosen is not None else None
        if off_site and chosen is not None:
            await self._login_page(page, typist, chosen)
        result = await self._fill(ctx, vault, typist, env, lane_id, ref)
        if _no_form(result) and not off_site and chosen is not None:  # on the site, not on its login page
            await self._login_page(page, typist, chosen)
            result = await self._fill(ctx, vault, typist, env, lane_id, ref)
        if _no_form(result):
            result["note"] = ("no login form on the login page: the site may have this browser signed in already "
                              "(browser_snapshot shows it)")
        return result

    async def _login_page(self, page: Any, typist: Any, chosen: BoundLogin) -> None:
        await _open_page(self.runtime, page, chosen.login_url)  # type: ignore[attr-defined]
        with contextlib.suppress(Exception):  # a form a script draws after the load
            await typist.settle(SETTLE_S)

    async def _fill(self, ctx: Any, vault: Any, typist: Any, env: Any, lane_id: str, ref: str | None) -> dict:
        from agentos.vault.login import login_flow

        with self.filling(env.id):
            return await login_flow(ctx, vault, typist, lane_id=lane_id, entry=ref)

    async def sign_in_now(self, title: str, site: str, *, by: str = "owner") -> dict[str, Any]:
        """The owner's `agentos environments login <title> <site>`: fill that login in that environment's
        browser now, and say how it ended (with the live view's link when the person is needed)."""
        from agentos.node.claims import Holder
        from agentos.node.login_assist import OwnerVaultContext
        from agentos.node.tools.session import lane_session

        env = self._open_titled(title)
        vault = self._get("vault")  # type: ignore[attr-defined]
        lanes = self._get("lanes")  # type: ignore[attr-defined]
        if vault is None or lanes is None:
            raise InvalidRequest("this node has no vault or no browser, so it cannot sign an environment in")
        logins = await bound_for(vault, env.browser_lane)
        named = find(logins, site)
        if len(named) != 1:
            raise NotFound(_none_words(env, logins, site) if not named else _several_words(named))
        chosen = named[0]
        lane = env.browser_lane
        ctx = OwnerVaultContext(self.runtime, lane_id=lane, by=by)  # type: ignore[attr-defined]
        holder = Holder(key=f"login:{env.id}", label="owner sign-in", kind="human", identity=f"login:{by}")
        async with lane_session(lanes, lane, "human", holder=holder) as page:
            typist = vault.typist_for(self.runtime)(page)  # type: ignore[attr-defined]
            result = await self.env_login(ctx, vault, typist, page, lane_id=lane, entry=chosen.ref) or {}
        out = {"environment": {"id": env.id, "title": env.title}, "login": chosen.public(), **result}
        if not result.get("ok"):
            out["links"] = self._live_link(env)
        return out

    def _open_titled(self, title: str) -> Any:
        from agentos.environments.errors import EnvironmentNotReady
        from agentos.environments.picking import same_title

        live = [e for e in self.store.open() if same_title(e.title, title)]  # type: ignore[attr-defined]
        if not live:
            raise NotFound(f"no open environment is titled \"{title}\". A saved one comes back with "
                           f"`agentos environments resume \"{title}\"`, then sign it in")
        env = max(live, key=lambda e: e.created_at)
        if env.state != "ready":
            raise EnvironmentNotReady(f"environment {env.id} (\"{env.title}\") is {env.state}; run this again once "
                                      "it is ready")
        return env

    def _live_link(self, env: Any) -> dict[str, Any]:
        try:
            from agentos.node.control_tools import take_over_link

            link = take_over_link(self.runtime, env.browser_lane, control=True)  # type: ignore[attr-defined]
            out = {"browser": link["url"], "browser_expires_at": link["expires_at"]}
            if not getattr(self, "on_laptop", False):
                out["tunnel"] = link["tunnel"]
            return out
        except Exception:
            log.warning("environment %s: no live-view link for the owner", env.id, exc_info=True)
            return {"browser": None}


async def _open_page(runtime: Any, page: Any, url: str) -> None:
    """Open the login page in the lane's tab, through the lane's egress guard as browser_navigate does."""
    from agentos.node import egress

    guard = egress.guard_for(page)
    if guard is not None:
        guard.check_url(url)
    interaction = runtime.get("interaction") if runtime is not None else None
    if interaction is not None:
        await interaction.navigate(page, url)
    else:
        await page.goto(url, wait_until="domcontentloaded")


def _no_form(result: dict) -> bool:
    return result.get("step") == "no_login_form" and not result.get("needs")


def _none_words(env: Any, logins: list[BoundLogin], ref: str | None) -> str:
    have = ", ".join(repr(b.name) for b in logins)
    what = f"no login {ref!r} is" if ref else "no login is"
    return (f"{what} bound to this environment" + (f" (it has {have})" if have else "") +
            f". The owner binds one at the node: agentos environments login add <site> --title \"{env.title}\"")


def _several_words(logins: list[BoundLogin]) -> str:
    return (f"{len(logins)} logins are bound here ({', '.join(repr(b.name) for b in logins)}); pass "
            "entry=<name>")
