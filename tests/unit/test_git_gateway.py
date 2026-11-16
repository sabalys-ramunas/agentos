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
from agentos.git.wire import Remote, pkt

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
    terminal of an open environment of the grant family that holds the VM's token. `own`: the
    branches it created, as EnvironmentManager keeps them on the record."""

    def __init__(self) -> None:
        self.own: dict[str, list[str]] = {}
        self.heads: dict[str, dict[str, str]] = {}

    def holds_terminal(self, lane_id: str) -> bool:
        return lane_id == ENV_LANE

    def branches_created(self, lane_id: str) -> list[str]:
        return list(self.own.get(lane_id, []))

    def note_branches(self, lane_id: str, *, created: list[str], deleted: list[str]) -> None:
        have = [b for b in self.own.get(lane_id, []) if b not in deleted]
        self.own[lane_id] = have + [b for b in created if b not in have]
        self.heads[lane_id] = {b: sha for b, sha in self.heads.get(lane_id, {}).items() if b in self.own[lane_id]}

    def branch_heads(self, lane_id: str) -> dict[str, str]:
        return dict(self.heads.get(lane_id, {}))

    def note_heads(self, lane_id: str, heads: dict[str, str]) -> None:
        own = self.own.get(lane_id, [])
        self.heads.setdefault(lane_id, {}).update({b: sha for b, sha in heads.items() if b in own})

    def own_lane(self, lane_id: str, grant) -> bool:
        return lane_id == ENV_LANE

    def may_reach(self, lane_id: str, grant, *, mcp: bool) -> bool:
        return True


async def vm(node: Node):
    """An environment's VM: its own gateway listener and its one token, for the repository only."""
    node.rt.register("environments", Environments())  # node.rt.get("environments").own: its branches
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


# ------------------------------------------------------------------ an environment and branches it did not create


def mirror_ref(node: Node, ref: str) -> str:
    mirror = node.git.gateway.mirror(Remote(HOST, REPO))
    return git("rev-parse", "--verify", "-q", ref, cwd=mirror.path, check=False).stdout.strip()


async def test_an_environment_may_not_delete_a_branch_it_did_not_create(node):
    """P1-1 of the 2026-11-15 test: `feature` was on the remote before the environment (a colleague's
    work). Deleting it is refused, asking nobody, and the mirror keeps it."""
    _g, run, work, _env = await vm(node)
    before = node.upstream_ref("feature")
    r = await run("push", "origin", "--delete", "feature", cwd=work)
    assert r.returncode != 0
    assert "push_refused" in r.stderr and "deletes feature, a branch this environment did not create" in r.stderr
    assert "new branch" in r.stderr and "waits for a person" not in r.stderr
    assert node.upstream_ref("feature") == before and mirror_ref(node, "refs/heads/feature") == before
    assert node.policy.approvals.list(state="pending") == []
    [ev] = [e for e in node.traces("git_push") if e.args.get("result") == "refused"]
    assert ev.args["reason"] == "push_refused" and ev.args["refs"][0]["kind"] == "delete"


async def test_an_environment_may_not_force_push_a_branch_it_did_not_create(node):
    _g, run, work, env = await vm(node)
    git("checkout", "-q", "feature", cwd=work)
    before = node.upstream_ref("feature")
    git("commit", "-q", "--amend", "-m", "rewritten", cwd=work, env=env)
    r = await run("push", "-f", "origin", "feature", cwd=work)
    assert r.returncode != 0
    assert "push_refused" in r.stderr and "force-pushes feature, a branch this environment did not create" in r.stderr
    assert node.upstream_ref("feature") == before and mirror_ref(node, "refs/heads/feature") == before  # put back
    assert node.policy.approvals.list(state="pending") == []


async def test_an_environment_fast_forwards_a_branch_it_did_not_create(node):
    _g, run, work, env = await vm(node)
    git("checkout", "-q", "feature", cwd=work)
    new = vm_commit(work, env, "on-feature")
    r = await run("push", "origin", "feature", cwd=work)
    assert r.returncode == 0, r.stderr
    assert node.upstream_ref("feature") == new
    assert node.rt.get("environments").own.get(ENV_LANE, []) == []  # an update makes it no one's own


async def test_an_environment_rewrites_and_deletes_the_branches_it_created(node):
    _g, run, work, env = await vm(node)
    envs = node.rt.get("environments")
    git("checkout", "-q", "-b", "agent/y", cwd=work)
    vm_commit(work, env, "y")
    assert (await run("push", "origin", "agent/y", cwd=work)).returncode == 0
    assert envs.own[ENV_LANE] == ["agent/y"]
    git("commit", "-q", "--amend", "-m", "y again", cwd=work, env=env)
    r = await run("push", "-f", "origin", "agent/y", cwd=work)
    assert r.returncode == 0, r.stderr
    assert node.upstream_ref("agent/y") == git("rev-parse", "HEAD", cwd=work).stdout.strip()
    r = await run("push", "origin", "--delete", "agent/y", cwd=work)
    assert r.returncode == 0, r.stderr
    assert git("rev-parse", "--verify", "-q", "refs/heads/agent/y", cwd=node.upstream, check=False).returncode != 0
    assert envs.own[ENV_LANE] == []  # deleted: the name is no longer its own


async def test_a_mixed_push_with_one_foreign_rewrite_sends_nothing(node):
    """A push that creates a branch and deletes someone else's: all of it is refused, and the branch it
    would have created is not recorded as its own."""
    _g, run, work, env = await vm(node)
    git("checkout", "-q", "-b", "agent/z", cwd=work)
    vm_commit(work, env, "z")
    r = await run("push", "origin", "agent/z", ":feature", cwd=work)
    assert r.returncode != 0 and "push_refused" in r.stderr
    assert node.upstream_ref("feature")
    assert git("rev-parse", "--verify", "-q", "refs/heads/agent/z", cwd=node.upstream, check=False).returncode != 0
    assert mirror_ref(node, "refs/heads/agent/z") == ""
    assert node.rt.get("environments").own.get(ENV_LANE, []) == []


# ------------------------------------------------------------------ speed: one fetch, a warm mirror, a blobless clone


async def test_requests_that_wait_for_the_mirror_share_the_fetch_that_ran_meanwhile(node, monkeypatch):
    """A clone, the agent's own fetch and the node's warm-up of one repository at once: each waited for
    the mirror's lock and then fetched from the remote again (the 2026-11-16 run: 163 s and 124 s for
    fetches the one before had just done). One fetch serves the requests that came while it ran."""
    from agentos.git.upstream import Mirror

    real, fetches = Mirror.fetch, []

    async def slow_fetch(self, target, auth):
        fetches.append(target.remote.slug)
        await asyncio.sleep(0.3)
        return await real(self, target, auth)

    monkeypatch.setattr(Mirror, "fetch", slow_fetch)
    g, _ = node.grant()
    tok = node.token(g.id)
    a, b = await asyncio.gather(node.run("ls-remote", f"https://{HOST}/{REPO}.git", token=tok),
                                node.run("ls-remote", f"https://{HOST}/{REPO}.git", token=tok))
    assert a.returncode == b.returncode == 0 and "refs/heads/main" in a.stdout and a.stdout == b.stdout
    assert len(fetches) == 1
    node.git.gateway.mirror(Remote(HOST, REPO)).fetched_at -= 60  # a minute later: the remote is asked again
    c = await node.run("ls-remote", f"https://{HOST}/{REPO}.git", token=tok)
    assert c.returncode == 0 and len(fetches) == 2


async def test_the_warm_up_fetches_the_mirror_before_the_vm_asks_under_the_same_checks(node):
    """While an environment's VM boots, the node brings the mirror up to date (a cold one of a big
    repository took 254 s, after the VM was up): declared, under the grant, and on the record."""
    node.rt.register("environments", Environments())
    node.git.remotes.allow("env", f"{HOST}/{REPO}", url=str(node.upstream))
    g, _ = node.policy.issue(subject={"id": "agent-1"}, workspaces=["env"], ceiling=Effect.COMMIT)
    mirror = node.git.gateway.mirror(Remote(HOST, REPO))
    assert not (mirror.path / "HEAD").exists()
    assert await node.git.warm(ENV_LANE, grant_id=g.id, repo=f"{HOST}/{REPO}") is True
    assert git("rev-parse", "refs/heads/main", cwd=mirror.path).stdout.strip() == node.upstream_ref("main")
    assert await node.git.warm(ENV_LANE, grant_id=g.id, repo=f"{HOST}/{REPO}") is False  # just fetched
    assert await node.git.warm(ENV_LANE, grant_id=g.id, repo=f"{HOST}/owner/other") is False  # not declared
    [t, *_] = [t for t in node.traces("git_fetch") if (t.args or {}).get("purpose") == "warm"]
    assert t.grant_id == g.id and t.args["remote"] == f"{HOST}/{REPO}"


async def test_an_environment_clones_blobless_and_git_fetches_a_file_when_a_command_needs_it(node):
    """The VM's clone through the gateway is blobless, so the pack that crosses the node's upload holds
    the commits, the trees and the checkout's files only. A file of another branch comes when a command
    needs it, by its id, through the gateway too; the mirror serves only objects a branch or tag reaches."""
    seed, env0 = node.tmp / "seed", plain_env(node.client_home)
    git("checkout", "-q", "-b", "other", cwd=seed, env=env0)
    (seed / "big.txt").write_text("x" * 200_000 + "\n")
    git("add", ".", cwd=seed, env=env0)
    git("commit", "-qm", "big", cwd=seed, env=env0)
    git("push", "-q", str(node.upstream), "other", cwd=seed, env=env0)
    _g, run, _work, _env = await vm(node)
    dest = node.tmp / "thin"
    r = await run("clone", "-q", "--filter=blob:none", f"https://{HOST}/{REPO}.git", str(dest))
    assert r.returncode == 0, r.stderr
    missing = git("rev-list", "--objects", "--missing=print", "--all", cwd=dest).stdout.split()
    assert any(x.startswith("?") for x in missing)  # big.txt is not in the clone
    r = await run("checkout", "-q", "other", cwd=dest)
    assert r.returncode == 0, r.stderr
    assert (dest / "big.txt").read_text() == "x" * 200_000 + "\n"
    reach = git("config", "uploadpack.allowReachableSHA1InWant", cwd=node.git.gateway.mirror(Remote(HOST, REPO)).path)
    assert reach.stdout.strip() == "true" and git("config", "uploadpack.allowAnySHA1InWant",
                                                    cwd=node.git.gateway.mirror(Remote(HOST, REPO)).path,
                                                    check=False).stdout.strip() == ""


def test_a_public_repository_vm_config_sends_only_pushes_to_the_gateway(tmp_path):
    """fetch_direct: git on the VM fetches the repository from its host, in any form it is written (ssh
    forms become https, which needs no key), and pushes it to the gateway with the token."""
    from agentos.git.service import vm_gitconfig

    home = tmp_path / "vm"
    home.mkdir()
    (home / ".gitconfig").write_text(vm_gitconfig(f"{HOST}/{REPO}", "agtg_x", fetch_direct=True))
    env = {**plain_env(home), "GIT_CONFIG_GLOBAL": str(home / ".gitconfig")}
    repo = tmp_path / "r"
    git("init", "-q", str(repo), env=env)
    for url in (f"https://{HOST}/{REPO}.git", f"git@{HOST}:{REPO}.git"):
        git("remote", "add", "o" + str(len(url)), url, cwd=repo, env=env)
        name = "o" + str(len(url))
        fetch = git("remote", "get-url", name, cwd=repo, env=env).stdout.strip()
        push = git("remote", "get-url", "--push", name, cwd=repo, env=env).stdout.strip()
        assert fetch == f"https://{HOST}/{REPO}.git", url
        assert push == f"http://127.0.0.1:3129/{HOST}/{REPO}.git", url
    gateway = vm_gitconfig(f"{HOST}/{REPO}", "agtg_x")
    (home / ".gitconfig").write_text(gateway)
    assert git("remote", "get-url", "o" + str(len(f"https://{HOST}/{REPO}.git")), cwd=repo,
               env=env).stdout.strip() == f"http://127.0.0.1:3129/{HOST}/{REPO}.git"


# What a VM can send by hand: the gateway's receive-pack POST without git, or without the GET before it.

def vm_token(env: dict) -> str:
    """The gateway token a VM's git config carries (its X-Agentos-Git header)."""
    for k, v in env.items():
        if k.startswith("GIT_CONFIG_VALUE_") and v.startswith("x-agentos-git: "):
            return v.split(": ", 1)[1]
    raise AssertionError("no gateway token in the VM's git config")


def pack_of(work: Path, new: str, have: str) -> bytes:
    """The pack a push of `new` sends to a remote that has `have`."""
    return subprocess.run(["git", "pack-objects", "--revs", "--stdout", "-q"], cwd=work, check=True,
                          input=f"{new}\n^{have}\n".encode(), capture_output=True, timeout=60).stdout


async def receive_pack(node: Node, env: dict, body: bytes):
    from agentos.git.gateway import Request

    return await node.git.gateway.handle(
        Request("POST", f"/{HOST}/{REPO}.git/git-receive-pack", {"x-agentos-git": vm_token(env)}, body),
        listener_lane=ENV_LANE)


def colleague_adds(node: Node, branch: str) -> str:
    """Someone else pushes a commit onto `branch` of the remote, as on GitHub. Returns their commit."""
    seed, env = node.tmp / f"colleague-{branch.replace('/', '-')}", plain_env(node.client_home)
    git("clone", "-q", "-b", branch, str(node.upstream), str(seed), env=env)
    (seed / "review.txt").write_text("a reviewer's fix\n")
    git("add", ".", cwd=seed, env=env)
    git("commit", "-qm", "a reviewer's fix", cwd=seed, env=env)
    git("push", "-q", "origin", branch, cwd=seed, env=env)
    return node.upstream_ref(branch)


async def test_a_force_push_of_its_own_branch_never_overwrites_a_commit_pushed_meanwhile(node, monkeypatch):
    """The lease holds for a force push too: a colleague who pushes between the gateway's refresh and its
    push upstream keeps their commit (the refspec has no `+`, which would make git skip the lease)."""
    from agentos.git import gateway as gw

    _g, run, work, env = await vm(node)
    git("checkout", "-q", "-b", "agent/own", cwd=work)
    vm_commit(work, env, "first")
    assert (await run("push", "origin", "agent/own", cwd=work)).returncode == 0
    theirs: list[str] = []
    real = gw.GitGateway._receive

    async def colleague_meanwhile(self, req, remote, listener_lane):
        theirs.append(colleague_adds(node, "agent/own"))  # after the GET refreshed the mirror
        return await real(self, req, remote, listener_lane)

    monkeypatch.setattr(gw.GitGateway, "_receive", colleague_meanwhile)
    git("commit", "-q", "--amend", "-m", "first, again", cwd=work, env=env)
    r = await run("push", "-f", "origin", "agent/own", cwd=work)
    assert r.returncode != 0 and "did not reach" in r.stderr
    assert node.upstream_ref("agent/own") == theirs[0]


async def test_a_vm_that_skips_the_refresh_cannot_force_push_over_a_colleague(node):
    """A hand-made POST with no GET first: the mirror still has the VM's last head, so the branch reads
    as its own, and the push goes upstream. The lease on that stale head refuses it there."""
    _g, run, work, env = await vm(node)
    git("checkout", "-q", "-b", "agent/pr", cwd=work)
    head = vm_commit(work, env, "mine")
    assert (await run("push", "origin", "agent/pr", cwd=work)).returncode == 0
    theirs = colleague_adds(node, "agent/pr")
    git("commit", "-q", "--amend", "-m", "mine, rewritten", cwd=work, env=env)
    new = git("rev-parse", "HEAD", cwd=work).stdout.strip()
    body = (pkt(f"{head} {new} refs/heads/agent/pr\0report-status ofs-delta agent=git/2\n") + b"0000"
            + pack_of(work, new, head))
    resp = await receive_pack(node, env, body)
    assert b"ng refs/heads/agent/pr" in resp.body
    assert node.upstream_ref("agent/pr") == theirs
    assert mirror_ref(node, "refs/heads/agent/pr") == head  # put back


async def test_a_push_that_asks_for_no_report_is_refused_before_the_mirror_takes_it(node):
    """Without report-status the gateway could not tell which refs the mirror took. Refused first, so
    the mirror never serves the next clone a commit or a delete the policy never saw."""
    _g, _run, work, env = await vm(node)
    main, feature = node.upstream_ref("main"), node.upstream_ref("feature")
    git("checkout", "-q", "--orphan", "other", cwd=work)
    orphan = vm_commit(work, env, "orphan")
    planted = pkt(f"{main} {orphan} refs/heads/main\0ofs-delta agent=git/2\n") + b"0000" + pack_of(work, orphan, main)
    deleted = pkt(f"{feature} {'0' * 40} refs/heads/feature\0agent=git/2\n") + b"0000"
    for body in (planted, deleted):
        resp = await receive_pack(node, env, body)
        assert resp.status == 400 and b"report-status" in resp.body
    assert mirror_ref(node, "refs/heads/main") == main and mirror_ref(node, "refs/heads/feature") == feature
    assert node.upstream_ref("main") == main and node.upstream_ref("feature") == feature
    g2, _ = node.grant()
    dest = node.tmp / "next-clone"
    r = await node.run("clone", "-q", f"https://{HOST}/{REPO}.git", str(dest), token=node.token(g2.id))
    assert r.returncode == 0, r.stderr
    assert git("rev-parse", "HEAD", cwd=dest).stdout.strip() == main


async def test_an_unexpected_error_after_the_mirror_took_the_pack_puts_every_ref_back(node, monkeypatch):
    from agentos.git import gateway as gw

    _g, run, work, env = await vm(node)
    git("checkout", "-q", "-b", "agent/err", cwd=work)
    vm_commit(work, env, "err")

    async def broken(*_a, **_k):
        raise RuntimeError("a bug in reading the refs")

    monkeypatch.setattr(gw, "_plan", broken)
    r = await run("push", "origin", "agent/err", cwd=work)
    assert r.returncode != 0
    upstream = git("rev-parse", "--verify", "-q", "refs/heads/agent/err", cwd=node.upstream, check=False)
    assert mirror_ref(node, "refs/heads/agent/err") == "" and upstream.stdout.strip() == ""
