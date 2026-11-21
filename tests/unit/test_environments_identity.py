"""Who an environment's commits are by (openajar.environments.identity): the owner's GitHub account when
a login is saved (its name and noreply address, the owner's choice of 2026-11-16), else the agent."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx

from openajar.environments import identity
from openajar.git import login

TOKEN = "ghp_" + "x" * 36


class Gateway:
    def __init__(self, token: str | None = TOKEN):
        self.token, self.asked = token, []

    async def _read_secret(self, ref: str, purpose: str) -> str | None:
        self.asked.append((ref, purpose))
        return self.token


def node(tmp_path: Path, answer: dict | None, *, status: int = 200, token: str | None = TOKEN) -> SimpleNamespace:
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(status, json=answer or {})

    gw = Gateway(token)
    git = SimpleNamespace(gateway=gw)
    return SimpleNamespace(home=tmp_path, github_transport=httpx.MockTransport(handler), calls=calls, gw=gw,
                           _get=lambda name: git if name == "git" else None)


def saved(tmp_path: Path, **kw) -> None:
    login.save(tmp_path, login.Login(login="octo-dev", source="gh", any_repo=True, **kw))


async def test_the_first_open_asks_github_once_and_commits_are_by_the_owner(tmp_path):
    saved(tmp_path)
    mgr = node(tmp_path, {"id": 4242, "login": "octo-dev", "name": "Octo Dev"})
    assert await identity.committer(mgr) == ("Octo Dev", "4242+octo-dev@users.noreply.github.com")
    assert len(mgr.calls) == 1 and mgr.calls[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert mgr.gw.asked[0][0] == login.REF
    kept = json.loads((tmp_path / login.SETTINGS_FILE).read_text())
    assert kept["user_id"] == 4242 and kept["name"] == "Octo Dev" and TOKEN not in json.dumps(kept)
    assert await identity.committer(mgr) == ("Octo Dev", "4242+octo-dev@users.noreply.github.com")
    assert len(mgr.calls) == 1  # kept: never asked again


async def test_no_display_name_uses_the_login(tmp_path):
    saved(tmp_path)
    mgr = node(tmp_path, {"id": 7, "login": "octo-dev", "name": None})
    assert await identity.committer(mgr) == ("octo-dev", "7+octo-dev@users.noreply.github.com")


async def test_without_a_login_or_an_answer_commits_are_by_the_agent(tmp_path):
    assert await identity.committer(node(tmp_path, {"id": 1, "login": "x"})) is None  # no login saved
    saved(tmp_path)
    assert await identity.committer(node(tmp_path, {}, status=401)) is None  # GitHub refused the token
    assert await identity.committer(node(tmp_path, {"id": 1, "login": "octo-dev"}, token=None)) is None
    assert login.load(tmp_path).user_id is None  # nothing kept from a failed ask


async def test_a_token_that_now_belongs_to_another_account_names_nobody(tmp_path):
    saved(tmp_path)
    mgr = node(tmp_path, {"id": 99, "login": "someone-else", "name": "Someone"})
    assert await identity.committer(mgr) is None
    assert login.load(tmp_path).user_id is None


def test_the_identity_commands_on_a_machine(tmp_path):
    def run(cmd: str, home: Path) -> dict[str, str]:
        home.mkdir(exist_ok=True)
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
        subprocess.run(["sh", "-c", cmd], check=True, env=env)
        got = {}
        for key in ("user.name", "user.email"):
            got[key] = subprocess.run(["git", "config", "--global", key], check=False, capture_output=True, text=True,
                                      env=env).stdout.strip()
        return got

    owner = identity.git_identity_cmd(("Octo O'Dev", "4242+octo-dev@users.noreply.github.com"), "claude-code@mac")
    home = tmp_path / "a"
    run(identity.git_identity_cmd(None, "claude-code@mac"), home)  # an older machine: the agent's
    assert run(owner, home) == {"user.name": "Octo O'Dev", "user.email": "4242+octo-dev@users.noreply.github.com"}
    agent = run(identity.git_identity_cmd(None, "claude-code@mac"), tmp_path / "b")
    assert agent == {"user.name": "claude-code@mac", "user.email": identity.AGENT_EMAIL}
    kept = run(identity.git_identity_cmd(None, "another@mac"), tmp_path / "b")  # never over one already set
    assert kept["user.name"] == "claude-code@mac"
