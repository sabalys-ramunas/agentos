"""The git gateway end to end: a real git client clones and pushes through the gateway, under a real
policy service, to a local bare repository standing in for the remote (docs/GIT.md).

What is checked: a fetch is a read and a push a commit on the lane (so Ask me first and the taint
downgrade apply); a force-push, a deletion and a push to the default branch are irreversible and
always wait for a person; a refused or waiting push leaves the remote and the mirror as they were,
and goes after the person's yes; an undeclared remote, a fetch-only one, an unknown token and a
token of another lane are refused with a sentence; every use is traced with the remote, the refs
and the grant; nothing of the credential reaches the client's output. From an agent environment's VM
nothing asks: a push to the default branch goes, and a force push or a delete of it is refused.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest
from test_policy_support import PRESENT, Clock, make_service
from test_vault_support import make_vault, private_home

from agentos.core.models import Effect
from agentos.git.gateway import Caller
from agentos.git.service import GitService, rewrite_pairs
from agentos.git.upstream import config_env
from agentos.git.wire import Remote

HOST = "example.test"
REPO = "owner/app"
LANE = "dev/code"
TOKEN = "github_pat_" + "Z" * 40

pytestmark = pytest.mark.skipif(not __import__("shutil").which("git"), reason="needs git")


def git(*args: str, cwd: Path | None = None, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=check, timeout=60)


def plain_env(home: Path) -> dict:
    return {"PATH": os.environ["PATH"], "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1", "LANG": "C",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.test", "GIT_TERMINAL_PROMPT": "0"}


class Node:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = private_home(tmp)
        self.policy, self.rt = make_service(self.home, clock=Clock())
        self.vault = make_vault(self.home)
        self.rt.register("vault", self.vault)
        self.git = GitService(self.rt, self.home, allow_file=True, host_port=0)
        self.rt.register("git", self.git)
        self.upstream = tmp / "up" / "app.git"
        self.client_home = tmp / "client"
        self.client_home.mkdir()
        seed = tmp / "seed"
        git("init", "-q", "--bare", "-b", "main", str(self.upstream))
        git("init", "-q", "-b", "main", str(seed))
        env = plain_env(self.client_home)
        (seed / "README").write_text("hello\n")
        git("add", ".", cwd=seed, env=env)
        git("commit", "-qm", "first", cwd=seed, env=env)
        git("push", "-q", str(self.upstream), "main", cwd=seed, env=env)
        git("push", "-q", str(self.upstream), "main:feature", cwd=seed, env=env)

    def grant(self, **kw):
        kw.setdefault("ceiling", Effect.IRREVERSIBLE)
        return self.policy.issue(subject={"id": "agent-1"}, workspaces=["dev"], **kw)

    def token(self, grant_id: str, lane: str | None = LANE) -> str:
        return self.git.gateway.token_for(Caller(grant_id=grant_id, workspace="dev", lane=lane, lanes=(),
                                                 label=f"lane {lane}"))

    def client_env(self, token: str) -> dict:
        env = plain_env(self.client_home)
        env.update(config_env(rewrite_pairs(self.git.host_base, [HOST], token)))
        return env

    async def run(self, *args: str, cwd: Path | None = None, token: str) -> subprocess.CompletedProcess:
        return await asyncio.to_thread(git, *args, cwd=cwd, env=self.client_env(token), check=False)

    def upstream_ref(self, ref: str) -> str:
        return git("rev-parse", ref, cwd=self.upstream).stdout.strip()

    async def add_token(self) -> None:
        from agentos.git import keys
        await self.vault.put_secret("git:test-token", TOKEN, kind=keys.TOKEN_KIND, label="t",
                                    scope=keys.token_scope(HOST, [REPO], None))

    def traces(self, kind: str) -> list:
        return self.policy.traces.query(kind=kind, limit=100)[0]


@pytest.fixture
async def node(tmp_path):
    n = Node(tmp_path)
    await n.vault.start(control=False)
    n.git.remotes.allow("dev", f"{HOST}/{REPO}", url=str(n.upstream))
    await n.add_token()
    await n.git.start()
    yield n
    await n.git.stop()
    await n.vault.stop()
    n.policy.close()


async def clone(node: Node, token: str, name: str = "work") -> Path:
    work = node.tmp / name
    r = await node.run("clone", "-q", f"https://{HOST}/{REPO}.git", str(work), token=token)
    assert r.returncode == 0, r.stderr
    return work


async def commit(node: Node, work: Path, token: str, text: str = "more") -> str:
    env = node.client_env(token)
    (work / "file.txt").write_text(text + "\n")
    await asyncio.to_thread(git, "add", ".", cwd=work, env=env)
    await asyncio.to_thread(git, "commit", "-qm", text, cwd=work, env=env)
    return git("rev-parse", "HEAD", cwd=work).stdout.strip()


# ------------------------------------------------------------------ fetch


async def test_clone_through_the_gateway_is_a_traced_read(node):
    g, _ = node.grant()
    tok = node.token(g.id)
    work = await clone(node, tok)
    assert (work / "README").read_text() == "hello\n"
    # the clone remembers the real remote, not the gateway: a later push goes the same way
    assert git("remote", "get-url", "origin", cwd=work).stdout.strip() == f"https://{HOST}/{REPO}.git"
    [ev, *_] = node.traces("git_fetch")
    assert ev.grant_id == g.id and ev.args["remote"] == f"{HOST}/{REPO}" and ev.args["via"] == f"lane {LANE}"
    assert ev.args["vault_ref"] == "git:test-token"


async def test_an_undeclared_repository_is_refused_with_what_to_do(node):
    g, _ = node.grant()
    r = await node.run("clone", "-q", f"https://{HOST}/owner/other.git", str(node.tmp / "x"), token=node.token(g.id))
    assert r.returncode != 0
    assert "example.test/owner/other is not declared for dev/code" in r.stderr
    assert "agentos git remote allow dev example.test/owner/other" in r.stderr
    [ev] = node.traces("git_refused")
    assert ev.grant_id == g.id and ev.status == "denied" and ev.args["remote"] == "example.test/owner/other"


async def test_ssh_and_scp_remotes_are_rewritten_too(node):
    g, _ = node.grant()
    tok = node.token(g.id)
    for i, url in enumerate((f"git@{HOST}:{REPO}.git", f"ssh://git@{HOST}/{REPO}")):
        r = await node.run("clone", "-q", url, str(node.tmp / f"s{i}"), token=tok)
        assert r.returncode == 0, r.stderr


async def test_a_token_the_gateway_never_minted_is_refused(node):
    r = await node.run("clone", "-q", f"https://{HOST}/{REPO}.git", str(node.tmp / "x"), token="agtg_made-up")
    assert r.returncode != 0 and "no longer valid" in r.stderr


async def test_a_revoked_grant_cannot_fetch(node):
    g, _ = node.grant()
    tok = node.token(g.id)
    node.policy.revoke(g.id)
    r = await node.run("clone", "-q", f"https://{HOST}/{REPO}.git", str(node.tmp / "x"), token=tok)
    assert r.returncode != 0 and "no longer valid" in r.stderr


async def test_a_lane_token_on_another_lane_listener_is_refused(node):
    from agentos.git.gateway import Refused

    g, _ = node.grant()
    tok = node.token(g.id, lane="dev/other")
    with pytest.raises(Refused, match="another lane"):
        node.git.gateway.caller(tok, listener_lane=LANE)


# ------------------------------------------------------------------ push routing


async def test_a_push_to_a_feature_branch_is_a_commit_that_runs_on_its_own(node):
    g, _ = node.grant()
    tok = node.token(g.id)
    work = await clone(node, tok)
    git("checkout", "-q", "feature", cwd=work)
    new = await commit(node, work, tok)
    r = await node.run("push", "origin", "feature", cwd=work, token=tok)
    assert r.returncode == 0, r.stderr
    assert node.upstream_ref("feature") == new
    [ev] = [e for e in node.traces("git_push") if e.args.get("result") == "pushed"]
    assert ev.grant_id == g.id and ev.effect == "commit" and ev.lane == LANE
    [ref] = ev.args["refs"]
    assert ref["ref"] == "refs/heads/feature" and ref["kind"] == "update" and ref["new"] == f"sha1:{new}"
    assert TOKEN not in r.stderr + r.stdout


async def test_ask_me_first_on_the_lane_holds_a_push_until_a_person_says_yes(node):
    g, _ = node.grant()
    node.policy.ask_first.set("lane", LANE, True, by="owner", person=True)
    tok = node.token(g.id)
    work = await clone(node, tok)
    git("checkout", "-q", "feature", cwd=work)
    before = node.upstream_ref("feature")
    new = await commit(node, work, tok)
    r = await node.run("push", "origin", "feature", cwd=work, token=tok)
    assert r.returncode != 0
    assert "waits for a person (approval" in r.stderr and "Ask me first is on" in r.stderr
    assert node.upstream_ref("feature") == before
    mirror = node.git.gateway.mirror(Remote(HOST, REPO))
    assert git("rev-parse", "refs/heads/feature", cwd=mirror.path).stdout.strip() == before  # put back
    pending = [e for e in node.traces("git_push") if e.args.get("result") == "approval_pending"]
    approval = pending[0].args["approval"]
    node.policy.approvals.decide(approval, "approve", by="human:owner", via="session")
    r = await node.run("push", "origin", "feature", cwd=work, token=tok)
    assert r.returncode == 0, r.stderr
    assert node.upstream_ref("feature") == new


async def test_after_untrusted_content_a_push_waits_for_a_person(node):
    g, _ = node.grant()
    ctx = node.policy.context_for_grant(g.id, tool="browser_snapshot")
    ctx.taint("untrusted", source="browser_snapshot", kind="web_page", host="evil.example")
    tok = node.token(g.id)
    work = await clone(node, tok)
    git("checkout", "-q", "feature", cwd=work)
    before = node.upstream_ref("feature")
    await commit(node, work, tok)
    r = await node.run("push", "origin", "feature", cwd=work, token=tok)
    assert r.returncode != 0
    assert "follows untrusted text" in r.stderr and "evil.example" in r.stderr
    assert node.upstream_ref("feature") == before


async def test_a_force_push_is_irreversible_and_always_asks(node):
    g, _ = node.grant()
    tok = node.token(g.id)
    work = await clone(node, tok)
    git("checkout", "-q", "feature", cwd=work)
    before = node.upstream_ref("feature")
    git("commit", "-q", "--amend", "-m", "rewritten", cwd=work, env=node.client_env(tok))
    r = await node.run("push", "-f", "origin", "feature", cwd=work, token=tok)
    assert r.returncode != 0 and "force-pushes refs/heads/feature" in r.stderr
    assert node.upstream_ref("feature") == before
    [ev] = [e for e in node.traces("git_push") if e.args.get("result") == "approval_pending"]
    assert ev.effect == "irreversible" and ev.args["refs"][0]["force"] is True
    node.policy.approvals.decide(ev.args["approval"], "approve", by="human:owner", via="session", proof=PRESENT)
    r = await node.run("push", "-f", "origin", "feature", cwd=work, token=tok)
    assert r.returncode == 0, r.stderr
    assert node.upstream_ref("feature") == git("rev-parse", "HEAD", cwd=work).stdout.strip()


async def test_a_push_to_the_default_branch_asks_unless_the_owner_allowed_it(node):
    g, _ = node.grant()
    tok = node.token(g.id)
    work = await clone(node, tok)
    before = node.upstream_ref("main")
    await commit(node, work, tok)
    r = await node.run("push", "origin", "main", cwd=work, token=tok)
    assert r.returncode != 0 and "main is a protected branch" in r.stderr
    assert node.upstream_ref("main") == before
    node.git.remotes.allow("dev", f"{HOST}/{REPO}", url=str(node.upstream), allow_default_branch=True)
    r = await node.run("push", "origin", "main", cwd=work, token=tok)
    assert r.returncode == 0, r.stderr


async def test_deleting_a_branch_is_irreversible(node):
    g, _ = node.grant()
    tok = node.token(g.id)
    work = await clone(node, tok)
    r = await node.run("push", "origin", "--delete", "feature", cwd=work, token=tok)
    assert r.returncode != 0 and "deletes refs/heads/feature" in r.stderr
    assert node.upstream_ref("feature")


async def test_a_new_branch_is_a_commit(node):
    g, _ = node.grant()
    tok = node.token(g.id)
    work = await clone(node, tok)
    git("checkout", "-q", "-b", "agent/x", cwd=work)
    new = await commit(node, work, tok)
    r = await node.run("push", "origin", "agent/x", cwd=work, token=tok)
    assert r.returncode == 0, r.stderr
    assert node.upstream_ref("agent/x") == new
    [ev] = [e for e in node.traces("git_push") if e.args.get("result") == "pushed"]
    assert ev.args["refs"][0]["kind"] == "create" and ev.args["refs"][0]["commits"] == 1


async def test_a_grant_below_commit_cannot_push(node):
    g, _ = node.grant(ceiling=Effect.READ)
    tok = node.token(g.id)
    work = await clone(node, tok)
    git("checkout", "-q", "feature", cwd=work)
    await commit(node, work, tok)
    r = await node.run("push", "origin", "feature", cwd=work, token=tok)
    assert r.returncode != 0 and "may not push" in r.stderr


async def test_a_fetch_only_remote_takes_no_push(node):
    node.git.remotes.allow("dev", f"{HOST}/{REPO}", url=str(node.upstream), push=False)
    g, _ = node.grant()
    tok = node.token(g.id)
    work = await clone(node, tok)
    git("checkout", "-q", "feature", cwd=work)
    await commit(node, work, tok)
    r = await node.run("push", "origin", "feature", cwd=work, token=tok)
    assert r.returncode != 0 and "fetch-only" in r.stderr


async def test_a_push_the_remote_moved_under_is_refused_and_put_back(node):
    g, _ = node.grant()
    tok = node.token(g.id)
    work = await clone(node, tok)
    git("checkout", "-q", "feature", cwd=work)
    await commit(node, work, tok)
    # someone else pushes to the remote after the lane's fetch and before its push
    other = node.tmp / "other"
    git("clone", "-q", str(node.upstream), str(other))
    git("checkout", "-q", "feature", cwd=other)
    (other / "x").write_text("x")
    env = plain_env(node.client_home)
    git("add", ".", cwd=other, env=env)
    git("commit", "-qm", "theirs", cwd=other, env=env)
    git("push", "-q", "origin", "feature", cwd=other, env=env)
    theirs = node.upstream_ref("feature")
    # the gateway refreshes its mirror at the push's advertisement, so git itself sees the remote moved
    r = await node.run("push", "origin", "feature", cwd=work, token=tok)
    assert r.returncode != 0
    assert node.upstream_ref("feature") == theirs


async def test_no_credential_no_push(node):
    await node.vault.delete_secret("git:test-token")
    g, _ = node.grant()
    tok = node.token(g.id)
    work = await clone(node, tok)  # a fetch needs none (a public repository)
    git("checkout", "-q", "feature", cwd=work)
    await commit(node, work, tok)
    r = await node.run("push", "origin", "feature", cwd=work, token=tok)
    assert r.returncode != 0 and "holds no credential" in r.stderr and "agentos git key add" in r.stderr


# ------------------------------------------------------------------ an agent environment's VM


ENV_LANE = "env/eabcdefghjk"


class Environments:
    """The node's environment records, as the gateway and the policy read them: ENV_LANE is the
    terminal of an open environment of the grant family that holds the VM's token."""

    def holds_terminal(self, lane_id: str) -> bool:
        return lane_id == ENV_LANE

    def own_lane(self, lane_id: str, grant) -> bool:
        return lane_id == ENV_LANE

    def may_reach(self, lane_id: str, grant, *, mcp: bool) -> bool:
        return True


async def vm(node: Node):
    """An environment's VM: its own gateway listener and its one token, for the repository only."""
    node.rt.register("environments", Environments())
    node.git.remotes.allow("env", f"{HOST}/{REPO}", url=str(node.upstream))
    g, _ = node.policy.issue(subject={"id": "agent-1"}, workspaces=["env"], ceiling=Effect.COMMIT)
    port = await node.git.vm_listener(ENV_LANE)
    tok = node.git.vm_token(ENV_LANE, grant_id=g.id, repo=f"{HOST}/{REPO}")
    env = plain_env(node.client_home)
    env.update(config_env(rewrite_pairs(f"http://127.0.0.1:{port}", [HOST], tok)))

    async def run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return await asyncio.to_thread(git, *args, cwd=cwd, env=env, check=False)

    work = node.tmp / "vm"
    r = await run("clone", "-q", f"https://{HOST}/{REPO}.git", str(work))
    assert r.returncode == 0, r.stderr
    return g, run, work, env


def vm_commit(work: Path, env: dict, name: str) -> str:
    (work / f"{name}.txt").write_text(name + "\n")
    git("add", ".", cwd=work, env=env)
    git("commit", "-qm", name, cwd=work, env=env)
    return git("rev-parse", "HEAD", cwd=work).stdout.strip()


async def test_a_push_from_an_environment_to_the_default_branch_never_asks(node):
    """The owner's decision of 2026-11-13: nothing in an agent's environment asks, a push to the
    default branch included, after untrusted text and with Ask me first on."""
    g, run, work, env = await vm(node)
    node.policy.context_for_grant(g.id, tool="browser_snapshot").taint(
        "untrusted", source="browser_snapshot", kind="web_page", host="evil.example")
    node.policy.ask_first.set("lane", ENV_LANE, True, by="owner", person=True)
    new = vm_commit(work, env, "from-the-vm")
    r = await run("push", "origin", "main", cwd=work)
    assert r.returncode == 0, r.stderr
    assert node.upstream_ref("main") == new
    assert node.policy.approvals.list(state="pending") == []
    [ev] = [e for e in node.traces("git_push") if e.args.get("result") == "pushed"]
    assert ev.effect == "commit" and ev.lane == ENV_LANE and ev.args["refs"][0]["protected"] is True
    # a force push of its own branch goes too, asking nobody
    git("checkout", "-q", "-b", "agent/x", cwd=work)
    vm_commit(work, env, "b")
    assert (await run("push", "origin", "agent/x", cwd=work)).returncode == 0
    git("commit", "-q", "--amend", "-m", "b again", cwd=work, env=env)
    r = await run("push", "-f", "origin", "agent/x", cwd=work)
    assert r.returncode == 0, r.stderr
    assert node.upstream_ref("agent/x") == git("rev-parse", "HEAD", cwd=work).stdout.strip()


async def test_a_force_push_or_a_delete_of_the_default_branch_from_an_environment_is_refused(node):
    _g, run, work, env = await vm(node)
    before = node.upstream_ref("main")
    git("commit", "-q", "--amend", "-m", "rewritten", cwd=work, env=env)
    r = await run("push", "-f", "origin", "main", cwd=work)
    assert r.returncode != 0
    assert "push_refused" in r.stderr and "force-pushes main" in r.stderr and "pull request" in r.stderr
    assert "waits for a person" not in r.stderr
    assert node.upstream_ref("main") == before
    mirror = node.git.gateway.mirror(Remote(HOST, REPO))
    assert git("rev-parse", "refs/heads/main", cwd=mirror.path).stdout.strip() == before  # put back
    r = await run("push", "origin", "--delete", "main", cwd=work)  # git's own mirror refuses this one first
    assert r.returncode != 0 and "waits for a person" not in r.stderr
    assert node.upstream_ref("main") == before
    # a protected branch that is not the mirror's HEAD: deleting it is the gateway's refusal
    node.git.remotes.allow("env", f"{HOST}/{REPO}", url=str(node.upstream), protect=["feature"])
    r = await run("push", "origin", "--delete", "feature", cwd=work)
    assert r.returncode != 0 and "push_refused" in r.stderr and "deletes feature" in r.stderr
    assert node.upstream_ref("feature")
    assert node.policy.approvals.list(state="pending") == []  # refused, never asked
    refused = [e for e in node.traces("git_push") if e.args.get("result") == "refused"]
    assert len(refused) == 2 and all(e.args.get("reason") == "push_refused" for e in refused)
