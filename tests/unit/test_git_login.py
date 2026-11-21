"""The GitHub login for environments (openajar.git.login): a token in the vault that serves any
repository of workspace env, and, while the owner said yes, counts every repository on its host as
declared for env. No network: probes and vaults are fakes, the one real vault lives in tmp_path."""

from __future__ import annotations

import pytest
from test_environments import TITLE, node, opened  # noqa: F401  (node is a fixture)

from openajar.git import keys as keymod
from openajar.git import login
from openajar.git.remotes import Remotes
from openajar.git.wire import Remote, parse_remote

GH = Remote("github.com", "me/shop")
TOKEN = "gho_" + "a" * 36


def _login_meta(host: str = "github.com") -> dict:
    return {"ref": login.REF, "kind": keymod.TOKEN_KIND, "label": "GitHub login me",
            "scope": {**login.scope("me", "gh"), "host": host}}


class LoginVault:
    """The vault as the gateway reads it: the GitHub login, and optionally a token for one repository."""

    def __init__(self, host: str = "github.com", *, also: list[dict] | None = None):
        self.metas = [_login_meta(host), *(also or [])]
        self.reads: list[tuple[str, str]] = []

    async def list_secrets(self):
        return self.metas

    async def get_secret(self, ref, *, purpose=""):
        self.reads.append((ref, purpose))
        return TOKEN


def test_off_by_default_nothing_is_declared(tmp_path):
    remotes = login.LoginRemotes(tmp_path / "git" / "remotes.json", tmp_path)
    assert login.load(tmp_path) is None
    assert remotes.find(GH, ["env/t-1", "env"]) is None
    login.save(tmp_path, login.Login(login="me", any_repo=False))
    assert remotes.find(GH, ["env/t-1", "env"]) is None and remotes.hosts_for(["env"]) == []


def test_on_every_github_repo_is_declared_for_env_only(tmp_path):
    remotes = login.LoginRemotes(tmp_path / "git" / "remotes.json", tmp_path)
    login.save(tmp_path, login.Login(login="me", any_repo=True))
    rule = remotes.find(GH, ["env/t-1", "env"])
    assert rule is not None and rule.scope == "env" and rule.push and rule.slug == "github.com/me/shop"
    # the default rules of a declaration: the default branch is protected for history
    assert rule.protected("main", "main") and not rule.allow_default_branch
    assert remotes.find(GH, ["personal/code", "personal"]) is None  # never outside workspace env
    assert remotes.find(Remote("gitlab.com", "me/shop"), ["env"]) is None  # never another host
    assert remotes.hosts_for(["env"]) == ["github.com"] and remotes.hosts_for(["personal"]) == []
    assert login.set_any_repo(tmp_path, False).any_repo is False
    assert remotes.find(GH, ["env"]) is None


def test_a_declaration_by_hand_still_wins(tmp_path):
    path = tmp_path / "git" / "remotes.json"
    Remotes(path).allow("env", "github.com/me/shop", push=False)
    login.save(tmp_path, login.Login(login="me", any_repo=True))
    rule = login.LoginRemotes(path, tmp_path).find(GH, ["env"])
    assert rule is not None and rule.push is False  # the owner said clone only for this one


def test_the_login_is_a_credential_for_env_after_any_made_for_the_repo():
    cred = keymod.from_meta(_login_meta())
    assert cred is not None and cred.any_repo and cred.repos == () and cred.public()["any_repo"] is True
    assert keymod.pick([cred], GH) is None  # keys.pick alone never widens it
    assert login.pick([cred], GH, "env") is cred
    assert login.pick([cred], GH, "env/t-1") is cred
    assert login.pick([cred], GH, "personal") is None  # an account lane's declaration never uses it
    assert login.pick([cred], Remote("gitlab.com", "me/shop"), "env") is None
    own = keymod.from_meta({"ref": "git:shop-token", "kind": keymod.TOKEN_KIND, "label": "t",
                            "scope": {"host": "github.com", "repos": "me/*"}})
    assert login.pick([cred, own], GH, "env") is own


async def test_store_seals_the_token_and_never_writes_it_in_the_setting(tmp_path):
    from test_vault_support import make_vault, private_home

    home = private_home(tmp_path)
    vault = make_vault(home)
    saved = await login.store(vault, home, TOKEN, login="me", source="gh", any_repo=True)
    assert saved.any_repo and saved.source == "gh" and saved.login == "me"
    assert TOKEN not in login.settings_path(home).read_text()
    assert oct(login.settings_path(home).stat().st_mode & 0o777) == "0o600"
    [cred] = [c for c in (keymod.from_meta(m) for m in await vault.list_secrets()) if c is not None]
    assert cred.ref == login.REF and cred.any_repo and cred.host == "github.com"
    assert await vault.get_secret(login.REF, purpose="test") == TOKEN
    with pytest.raises(Exception, match="printable"):
        await login.store(vault, home, "short", login="me", source="token", any_repo=False)
    await login.forget(vault, home)
    assert login.load(home) is None and await vault.get_secret(login.REF, purpose="test") is None


async def test_vm_token_for_an_undeclared_repo_follows_the_setting(tmp_path):
    from openajar.git.gateway import token_digest
    from openajar.git.service import GitService

    svc = GitService(None, tmp_path, host_port=0)
    lane = "env/t-1"
    await svc.vm_listener(lane)
    try:
        assert svc.vm_token(lane, grant_id="g1", repo="github.com/me/shop") is None
        login.save(tmp_path, login.Login(login="me", any_repo=True))
        token = svc.vm_token(lane, grant_id="g1", repo="github.com/me/shop")
        assert token is not None
        caller = svc.gateway._callers[token_digest(token)]
        assert caller.repos == ("github.com/me/shop",) and caller.bound  # that one repository, on its listener
    finally:
        await svc.vm_down(lane)


async def test_access_checks_with_the_login_before_anything_is_rented(tmp_path):
    from openajar.core.runtime import Runtime
    from openajar.git.service import GitService

    rt = Runtime()
    vault = LoginVault()
    rt.register("vault", vault)
    svc = GitService(rt, tmp_path, host_port=0)
    login.save(tmp_path, login.Login(login="me", any_repo=True))
    seen = []

    async def probe(url, env, *, timeout_s=20, allow_file=False):
        seen.append((url, env))
        return True, b""

    got = await svc.access("github.com/me/shop", ["env/t-1", "env"], probe=probe)
    assert got.ok and got.via == "gateway" and got.declared and got.credential == "github-login"
    [(url, env)] = seen
    assert url == "https://github.com/me/shop.git" and TOKEN not in url
    assert any(v.startswith("Authorization: Basic ") for k, v in env.items() if k.startswith("GIT_CONFIG_VALUE"))
    assert vault.reads and vault.reads[0][0] == login.REF


async def test_an_environment_opens_on_a_private_repo_the_login_reaches(node):  # noqa: F811
    """End to end on the fake node: nothing declared, the login on, the check passes with the login."""
    from openajar.git.service import GitService

    repo = "example.test/me/private"
    home = node["mgr"].home
    svc = GitService(node["rt"], home, host_port=0)
    node["rt"].register("git", svc)
    node["rt"].register("vault", LoginVault("example.test"))
    login.save(home, login.Login(host="example.test", login="me", any_repo=True))
    with_login = []

    async def probe(url, env, *, timeout_s=20, allow_file=False):
        auth = any(k.startswith("GIT_CONFIG_VALUE") and v.startswith("Authorization: Basic ") for k, v in env.items())
        with_login.append(auth)
        return auth, b"" if auth else b"fatal: could not read Username: terminal prompts disabled\n"

    node["mgr"].repo_probe = probe
    try:
        out = await opened(node, repo=repo)
        assert out["state"] == "ready" and out["repo"] == repo and with_login == [True]
        login.set_any_repo(home, False)  # the owner turned it off: the same repository is not connected,
        # so the agent gets the card to connect it (openajar.environments.connect), and nothing is rented
        again = await node["call"]("environment_open", title=TITLE + " two", repo=repo, session="chat-2")
        assert again["status"] == "approval_pending" and again["kind"] == "repo_connect", again
    finally:
        await svc.stop()
        for lane in list(svc._vm_listeners):
            await svc.vm_down(lane)


def test_parse_remote_of_the_synthetic_rule_round_trips(tmp_path):
    login.save(tmp_path, login.Login(any_repo=True))
    rule = login.LoginRemotes(tmp_path / "r.json", tmp_path).find(parse_remote("https://github.com/Me/Shop.git"),
                                                                 ["env"])
    assert rule is not None and rule.repo == "me/shop"
