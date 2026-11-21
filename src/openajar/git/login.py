"""A GitHub login for environments: any repository it reaches (`openajar setup github`).

The owner's github.com token (the one `gh auth token` prints, or one pasted) is sealed in the vault
as `git:github-login`, a git token whose scope says `any_repo`. The gateway uses it for a repository
of workspace env that has no key or token of its own (one made for that repository or its owner
always comes first), and for nothing outside workspace env.

The node setting `OPENAJAR_HOME/git/login.json` says whose login it is and whether `any_repo` is
on. Off (the default, and what the owner's no gives): repositories are still declared one by one,
and the login is only their credential. On: every repository on that host counts as declared for
workspace env, clone and push, as one declared by hand (`openajar environments repo add`): the same
push checks hold (no force push or delete of a branch the environment did not create, no tags).

The VM never holds the token. It holds the gateway token of its one repository, as before, and the
node runs git with the login. Nothing here calls gh at push time: the token gh gave is what is
stored, and a rotated one is stored again by running `openajar setup github`.
"""

from __future__ import annotations

import builtins
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from openajar.core.fs import write_private_json
from openajar.git import keys as keymod
from openajar.git.remotes import RemoteRule, Remotes, scope_workspace
from openajar.git.wire import Remote

HOST = "github.com"
REF = keymod.REF_PREFIX + "github-login"
SETTINGS_FILE = "git/login.json"
# openajar.environments.models.WORKSPACE (a layer above git): the workspace environments' lanes live in
ENV_WORKSPACE = "env"


class Login(BaseModel):
    """`git/login.json`: whose login the vault holds, and whether it serves any repository."""
    host: str = HOST
    ref: str = REF
    login: str = ""                          # the account name, for what people read
    source: Literal["gh", "token"] = "token"
    any_repo: bool = False
    saved_at: str = ""
    user_id: int | None = None               # the account's number, for its noreply address (asked once)
    name: str = ""                           # its display name, for commits


def committer(saved: Login | None) -> tuple[str, str] | None:
    """Who an environment's commits are by: the login's own name and GitHub noreply address
    (<id>+<login>@users.noreply.github.com, which links a commit to the account without its real email),
    or None without a GitHub login or before its id is known."""
    if saved is None or saved.host != HOST or not saved.login or saved.user_id is None:
        return None
    return (saved.name or saved.login)[:100], f"{saved.user_id}+{saved.login}@users.noreply.github.com"


def settings_path(home: Path) -> Path:
    return Path(home) / SETTINGS_FILE


def load(home: Path) -> Login | None:
    """The saved login, or None (never saved, or the file is unreadable: then nothing is widened)."""
    path = settings_path(home)
    try:
        return Login(**json.loads(path.read_text() or "{}"))
    except (OSError, ValueError):
        return None


def save(home: Path, login: Login) -> None:
    write_private_json(settings_path(home), login.model_dump(), newline=True)


def set_any_repo(home: Path, on: bool) -> Login | None:
    """Turn `any_repo` on or off for the saved login (None when there is none)."""
    login = load(home)
    if login is None:
        return None
    login.any_repo = on
    save(home, login)
    return login


def scope(login: str, source: str) -> dict[str, Any]:
    """The vault scope of the login's token: a git token for HOST that names no repository."""
    return {"host": HOST, "repos": "", "type": "token", "username": keymod.DEFAULT_TOKEN_USER, "any_repo": True,
            "login": login[:100], "source": source}


async def store(vault: Any, home: Path, token: str, *, login: str, source: Literal["gh", "token"],
                any_repo: bool) -> Login:
    """Seal `token` in the vault as the GitHub login and save the setting. Returns the setting."""
    secret = keymod.check_token(token)
    await vault.put_secret(REF, secret.reveal(), kind=keymod.TOKEN_KIND,
                           label=f"GitHub login {login}".strip(), scope=scope(login, source))
    saved = Login(login=login, source=source, any_repo=any_repo,
                  saved_at=datetime.now(UTC).isoformat(timespec="seconds"))
    save(home, saved)
    return saved


async def forget(vault: Any, home: Path) -> None:
    """Delete the login's token and its setting."""
    if vault is not None:
        await vault.delete_secret(REF)
    settings_path(home).unlink(missing_ok=True)


def pick(credentials: list[keymod.Credential], remote: Remote, rule_scope: str) -> keymod.Credential | None:
    """The credential for `remote` declared for `rule_scope`: keys.pick's, else the GitHub login when
    the declaration is workspace env's and the login is for the remote's host."""
    found = keymod.pick(credentials, remote)
    if found is not None or scope_workspace(rule_scope) != ENV_WORKSPACE:
        return found
    return next((c for c in credentials if c.any_repo and c.ref == REF and c.host == remote.host), None)


class LoginRemotes(Remotes):
    """The declared remotes, and, while the login's `any_repo` is on, every repository on its host
    for workspace env (clone and push, the default rules of a declaration)."""

    def __init__(self, path: Path, home: Path):
        super().__init__(path)
        self.home = Path(home)

    def any_repo_host(self) -> str | None:
        login = load(self.home)
        return login.host if login is not None and login.any_repo else None

    def find(self, remote: Remote, scopes: builtins.list[str]) -> RemoteRule | None:
        rule = super().find(remote, scopes)
        if rule is not None or ENV_WORKSPACE not in scopes:
            return rule
        host = self.any_repo_host()
        if host is None or remote.host != host:
            return None
        return RemoteRule(scope=ENV_WORKSPACE, host=host, repo=remote.repo, push=True)

    def hosts_for(self, scopes: builtins.list[str]) -> builtins.list[str]:
        hosts = super().hosts_for(scopes)
        host = self.any_repo_host() if ENV_WORKSPACE in scopes else None
        return sorted({*hosts, host}) if host else hosts
