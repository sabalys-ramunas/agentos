"""A co-sign to send a git bundle binds the commit its branch is at, and a branch that moved after
the yes hands nothing to the brain."""

from __future__ import annotations

import os
import subprocess

import pytest
from testkit.brains import FakeCtx, add, make_service, wait_status

from agentos.brains import bundle as bundle_mod
from agentos.brains.models import ContextBundle, GitRef
from agentos.core.errors import ApprovalPending, InvalidRequest
from agentos.core.models import Effect, Principal
from agentos.core.runtime import Runtime

P = Principal(id="claude-code", thread="T-1")
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@x"}


def commit(repo, text):
    (repo / "app.py").write_text(text)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=ENV)
    subprocess.run(["git", "commit", "-qm", text[:20]], cwd=repo, check=True, env=ENV)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, env=ENV, capture_output=True,
                          text=True).stdout.strip()


def rev(repo):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, env=ENV, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "wip"], cwd=r, check=True, env=ENV)
    commit(r, "print('one')\n")
    return r


@pytest.fixture
def svc(tmp_path):
    return make_service(tmp_path, runtime=Runtime())


def private(repo, **git):
    return ContextBundle(taint=["private"], git=GitRef(remote=str(repo), ref="wip", **git))


async def test_the_card_binds_the_commit_the_branch_is_at(svc, repo):
    await add(svc, "claude-max", "subscription", "anthropic")
    head = rev(repo)
    ctx = FakeCtx(svc.runtime, P, pending={Effect.IRREVERSIBLE})
    with pytest.raises(ApprovalPending):
        await svc.submit(P, prompt="FAKE:ok", workspace="personal", context_bundle=private(repo), ctx=ctx)
    first = ctx.authorized[-1]["content"]["bundle"]
    assert first["git"]["commit"] == head and first["git"]["ref"] == "wip"
    commit(repo, "print('two')\n")  # the branch moves: the same call is another card
    with pytest.raises(ApprovalPending):
        await svc.submit(P, prompt="FAKE:ok", workspace="personal", context_bundle=private(repo), ctx=ctx)
    assert ctx.authorized[-1]["content"]["bundle"]["git"]["commit"] != head


async def test_a_branch_that_moved_after_the_yes_hands_nothing_to_the_brain(svc, repo, monkeypatch):
    await add(svc, "claude-max", "subscription", "anthropic")
    real = bundle_mod.resolve_commit

    def resolve_then_move(git, home, *, allow_file):
        sha = real(git, home, allow_file=allow_file)
        commit(repo, "print('pushed after the yes')\n")
        return sha

    monkeypatch.setattr(bundle_mod, "resolve_commit", resolve_then_move)
    r = await svc.submit(P, prompt="FAKE:ok", workspace="personal", context_bundle=private(repo),
                         ctx=FakeCtx(svc.runtime, P), wait_prepare=True)
    st = await wait_status(svc, r["id"])
    assert st["status"] == "failed" and "moved since the co-sign" in st["status_reason"]
    assert not (svc.task_dir(r["id"]) / "work" / "app.py").exists()


async def test_a_task_with_no_co_sign_is_not_pinned(svc, repo):
    await add(svc, "claude-max", "subscription", "anthropic")
    r = await svc.submit(P, prompt="FAKE:ok", workspace="personal",
                         context_bundle=ContextBundle(git=GitRef(remote=str(repo), ref="wip")), ctx=FakeCtx(svc.runtime, P),
                         wait_prepare=True)
    assert (await wait_status(svc, r["id"]))["status"] == "succeeded_unverified"


def test_a_short_commit_id_cannot_be_co_signed(repo, tmp_path):
    with pytest.raises(InvalidRequest, match="full commit"):
        bundle_mod.resolve_commit(GitRef(remote=str(repo), ref="abcdef1"), tmp_path, allow_file=True)


def test_a_pinned_commit_must_be_a_full_id():
    with pytest.raises(InvalidRequest, match="full commit"):
        bundle_mod.validate(ContextBundle(git=GitRef(remote="https://x/y.git", commit="abc")))
