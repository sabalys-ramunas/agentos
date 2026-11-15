"""Security review of B1 (d2cc2706..acfb035b): proof tests for the git push rules of an environment.
Each test states what the design forbids; it fails while the gap is open."""

from __future__ import annotations

import pytest
from test_git_gateway import git, node, vm, vm_commit  # noqa: F401 - node is a fixture

pytestmark = pytest.mark.skipif(not __import__("shutil").which("git"), reason="needs git")


async def test_a_name_the_environment_once_created_is_not_its_own_after_someone_else_recreates_it(node):  # noqa: F811
    """The environment created agent/y; it was deleted on the remote outside the environment (GitHub's
    delete-after-merge), and a colleague pushed new work under the same name. The record still says
    agent/y is the environment's own, so a force push of it goes and the colleague's commit is lost."""
    _g, run, work, env = await vm(node)
    git("checkout", "-q", "-b", "agent/y", cwd=work)
    vm_commit(work, env, "y")
    assert (await run("push", "origin", "agent/y", cwd=work)).returncode == 0
    up = node.upstream
    git("update-ref", "-d", "refs/heads/agent/y", cwd=up)  # merged and deleted on the remote
    # a colleague's agent/y: new work on main, not in the environment's history (a fast-forward of it
    # would keep the colleague's commit and is allowed; this push must rewrite it)
    main = node.upstream_ref("main")
    tree = git("rev-parse", f"{main}^{{tree}}", cwd=up).stdout.strip()
    colleague = git("commit-tree", tree, "-p", main, "-m", "a colleague's agent/y", cwd=up, env=env).stdout.strip()
    git("update-ref", "refs/heads/agent/y", colleague, cwd=up)
    r = await run("push", "-f", "origin", "agent/y", cwd=work)
    assert node.upstream_ref("agent/y") == colleague, "the environment force-pushed over a colleague's branch"
    assert r.returncode != 0


async def test_a_branch_a_colleague_added_to_is_not_force_pushed_from_the_environment(node):  # noqa: F811
    """The environment created agent/w; a colleague then pushed a commit on top of it. A force push from
    the environment would drop that commit: refused. A fast-forward on top of the colleague's stays allowed."""
    _g, run, work, env = await vm(node)
    git("checkout", "-q", "-b", "agent/w", cwd=work)
    vm_commit(work, env, "w")
    assert (await run("push", "origin", "agent/w", cwd=work)).returncode == 0
    up = node.upstream
    mine = node.upstream_ref("agent/w")
    tree = git("rev-parse", f"{mine}^{{tree}}", cwd=up).stdout.strip()
    theirs = git("commit-tree", tree, "-p", mine, "-m", "a colleague's commit", cwd=up, env=env).stdout.strip()
    git("update-ref", "refs/heads/agent/w", theirs, cwd=up)
    git("commit", "-q", "--amend", "-m", "w again", cwd=work, env=env)
    r = await run("push", "-f", "origin", "agent/w", cwd=work)
    assert r.returncode != 0
    assert node.upstream_ref("agent/w") == theirs, "the environment force-pushed over a colleague's commit"
