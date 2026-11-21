"""Who an environment's commits are by.

With a GitHub login saved (`openajar setup github`), an environment's git commits as the owner: the
login's display name and its GitHub noreply address, so the commits link to the owner's account
without showing their real email (the owner's choice of 2026-11-16). The account's number, which that
address needs, is asked from GitHub once with the saved login and kept in git/login.json. Without a
login, or when GitHub cannot be asked, commits are by the agent (its name, agent@openajar.invalid), as
before. Nothing here puts a token on the machine: only the name and the address reach it.
"""

from __future__ import annotations

import logging
import shlex
from typing import Any

log = logging.getLogger("openajar.environments")

AGENT_EMAIL = "agent@openajar.invalid"


async def committer(mgr: Any) -> tuple[str, str] | None:
    """(name, email) of the saved GitHub login, asking GitHub for its number and name the first time,
    or None (no login, or GitHub could not be asked: the agent's identity then)."""
    from openajar.git import login

    try:
        saved = login.load(mgr.home)
        if saved is None or saved.host != login.HOST or not saved.login:
            return None
        if saved.user_id is None:
            profile = await _profile(mgr)
            if profile is None:
                return None
            saved.user_id = int(profile["id"])
            saved.name = str(profile.get("name") or "")[:100]
            if str(profile.get("login") or saved.login).lower() == saved.login.lower():
                login.save(mgr.home, saved)
            else:  # the token now belongs to another account: say nothing in its name
                log.warning("the saved GitHub login %s now answers as %s; run `openajar setup github`",
                            saved.login, profile.get("login"))
                return None
        return login.committer(saved)
    except Exception as e:  # noqa: BLE001 - an identity is never a reason a machine does not come up
        log.info("commit identity: the GitHub login's account could not be read (%s); commits are by the agent", e)
        return None


async def _profile(mgr: Any) -> dict | None:
    """GitHub's /user for the saved login's token, or None."""
    from openajar.environments.pulls import _client
    from openajar.git import login

    git = mgr._get("git")
    gateway = getattr(git, "gateway", None)
    if gateway is None:
        return None
    token = await gateway._read_secret(login.REF, "read the GitHub login's name for the environments' commits")
    if not token:
        return None
    async with _client(mgr) as client:
        resp = await client.get("/user", headers={"Authorization": f"Bearer {token}"})
    if resp.status_code != 200:
        return None
    data = resp.json()
    return data if isinstance(data, dict) and isinstance(data.get("id"), int) else None


def git_identity_cmd(ident: tuple[str, str] | None, agent: str) -> str:
    """The shell that sets git's identity on the machine: the owner's when known (always, so a machine
    back from a save gets it too), else the agent's only where none is set yet."""
    q = shlex.quote
    if ident is not None:
        name, email = ident
        return f"git config --global user.name {q(name)} && git config --global user.email {q(email)}"
    return (f"(git config --global user.name >/dev/null || git config --global user.name {q(agent[:60] or 'openajar agent')})"
            f" && (git config --global user.email >/dev/null || git config --global user.email {AGENT_EMAIL})")
