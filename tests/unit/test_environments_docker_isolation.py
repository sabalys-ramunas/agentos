"""The VM's one gateway token, on a real machine (the docker provider's container, as
tests/unit/test_environments_docker.py): it fetches and pushes the environment's own repository and
nothing else, is good only through its own VM's tunnel, and dies when the environment closes. The
reviewer's probes 2 and 5 of 2026-11-13. Skipped unless docker answers and the image is built."""

from __future__ import annotations

from test_environments_docker import HOST, REPO, git, node, pytestmark  # noqa: F401  (fixture, skip mark)


async def _open(node, sid: str, title: str, repo: str | None = f"{HOST}/{REPO}"):  # noqa: F811
    mgr, grant = node["mgr"], node["grant"]
    eid = mgr.planned_id(f"session:{sid}", title)
    plan = await mgr.plan(eid, size=None, hours=1)
    env = await mgr.create(eid, plan, binding=f"session:{sid}", title=title, grant_id=grant.id,
                           grant_root=grant.root_id, agent="agent-1", client=None, repo=repo, ref=None)
    env = await mgr.wait_ready(env.id, 150)
    assert env.state == "ready", env.error
    return env


async def test_the_vm_token_reaches_its_repository_only_and_dies_with_it(node, tmp_path):  # noqa: F811
    mgr, envs = node["mgr"], node["envs"]
    gitsvc = node["rt"].get("git")
    secret, seed = tmp_path / "secret.git", tmp_path / "seed2"
    git("init", "-q", "--bare", "-b", "main", str(secret))
    git("init", "-q", "-b", "main", str(seed))
    (seed / "KEYS.txt").write_text("the owner's private code\n")
    git("add", ".", cwd=seed)
    git("commit", "-qm", "s", cwd=seed)
    git("push", "-q", str(secret), "main", cwd=seed)
    gitsvc.remotes.allow("env", f"{HOST}/me/secret", url=str(secret))

    a = await _open(node, "s1", "Env A")
    b = await _open(node, "s2", "Env B", repo=None)

    # no token in a command's environment; the one token is in the VM's own git config
    seen = await envs.exec(a.terminal_lane, "env | grep -c agtg_ || true; cat ~/.config/agentos/gitconfig",
                           grant_id=node["grant"].id)
    lines = seen.stdout.split()
    assert lines[0] == "0", seen.stdout
    token = next(w for w in lines if w.startswith("agtg_"))
    assert f"{HOST}/{REPO}" in seen.stdout and "me/secret" not in seen.stdout

    # the environment's own repository: fetched through the gateway
    own = await envs.exec(a.terminal_lane, "cd /work/shop && git ls-remote origin main 2>&1", timeout_s=60)
    assert own.exit_code == 0 and "refs/heads/main" in own.stdout, own.stdout

    # another declared private repository, with the same token: refused
    other = await envs.exec(a.terminal_lane, f"cd /tmp && git -c http.extraHeader='x-agentos-git: {token}' clone -q "
                                             f"http://127.0.0.1:3129/{HOST}/me/secret sec 2>&1; ls sec 2>&1",
                            timeout_s=60)
    assert "KEYS.txt" not in other.stdout and "reaches only" in other.stdout, other.stdout

    # an environment without a declared repository holds no token at all
    none = await envs.exec(b.terminal_lane, "cat ~/.config/agentos/gitconfig 2>/dev/null | grep -c agtg_ || true")
    assert none.stdout.strip() == "0"

    # A's token through B's tunnel: another lane's token
    via_b = await envs.exec(b.terminal_lane, f"git -c http.extraHeader='x-agentos-git: {token}' ls-remote "
                                             f"http://127.0.0.1:3129/{HOST}/{REPO} 2>&1 | head -3", timeout_s=60)
    assert "refs/heads/main" not in via_b.stdout and "another lane" in via_b.stdout, via_b.stdout

    # closed: A's token is gone, wherever it is used from
    await mgr.close(a.id, discard=True)
    assert gitsvc.gateway._callers == {} or all(c.lane != a.terminal_lane for c in gitsvc.gateway._callers.values())
    after = await envs.exec(b.terminal_lane, f"git -c http.extraHeader='x-agentos-git: {token}' ls-remote "
                                             f"http://127.0.0.1:3129/{HOST}/{REPO} 2>&1 | head -3", timeout_s=60)
    assert "refs/heads/main" not in after.stdout, after.stdout
