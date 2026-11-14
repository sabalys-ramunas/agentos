"""Git over real SSH from a real isolated lane (docs/GIT.md), on a node: a deploy key made by
`agentos git key add`, a private sshd on the node's loopback standing in for the git host, and a
terminal lane (bubblewrap, its own network namespace) whose git clones and pushes through the
gateway's listener in that namespace. The key never reaches the lane, is never written to a file,
and an undeclared repository and a force-push are refused.

Needs Linux, root, bubblewrap, git and sshd (openssh-server). The sshd listens on 127.0.0.1 only, on
a port of the test's own, with an authorized_keys file in the test's directory, and is stopped
afterwards; nothing of the node's own ssh, nor of any installed agentos, is touched.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest
from test_env_node_support import CAN_RUN, new_home

from agentos.core.models import Effect

SSHD = shutil.which("sshd") or "/usr/sbin/sshd"
pytestmark = [pytest.mark.node,
              pytest.mark.skipif(not (CAN_RUN and os.path.exists(SSHD) and shutil.which("git")),
                                 reason="needs a Linux node, root, bubblewrap, git and sshd")]
LANE = "gitnode/code"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(*args, cwd=None, env=None, check=True):
    return subprocess.run(list(args), cwd=cwd, env=env, capture_output=True, text=True, check=check, timeout=120)


def serve_repo(srv: Path) -> tuple[Path, int, subprocess.Popen]:
    """The stand-in git host: a bare repository served over ssh as root, on 127.0.0.1 only, to the one
    key in srv/authorized_keys (written once `agentos git key add` printed it)."""
    port = free_port()
    # the stand-in git host: a bare repository served over ssh as root, to one authorized key
    upstream = srv / "repos" / "app.git"
    run("git", "init", "-q", "--bare", "-b", "main", str(upstream))
    seed = srv / "seed"
    run("git", "init", "-q", "-b", "main", str(seed))
    (seed / "README").write_text("hello from the node test\n")
    genv = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@x"}
    run("git", "add", ".", cwd=seed, env=genv)
    run("git", "commit", "-qm", "first", cwd=seed, env=genv)
    run("git", "push", "-q", str(upstream), "main", "main:feature", cwd=seed, env=genv)
    run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(srv / "hostkey"))
    (srv / "sshd_config").write_text(
        f"Port {port}\nListenAddress 127.0.0.1\nHostKey {srv / 'hostkey'}\nPidFile {srv / 'sshd.pid'}\n"
        f"AuthorizedKeysFile {srv / 'authorized_keys'}\nStrictModes no\nUsePAM yes\nPasswordAuthentication no\n"
        "KbdInteractiveAuthentication no\nPermitRootLogin prohibit-password\nAllowUsers root\n"
        "AllowAgentForwarding no\nAllowTcpForwarding no\nX11Forwarding no\nLogLevel ERROR\n")
    os.makedirs("/run/sshd", exist_ok=True)
    sshd = subprocess.Popen([SSHD, "-D", "-e", "-f", str(srv / "sshd_config")], stderr=subprocess.PIPE)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)

    return upstream, port, sshd


@pytest.fixture
async def world(monkeypatch):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
    from test_policy_support import Clock, make_service
    from test_vault_support import make_vault
    from typer.testing import CliRunner

    from agentos.cli import app
    from agentos.core.runtime import Runtime  # noqa: F401
    from agentos.env.manager import EnvManager
    from agentos.env.paths import EnvSettings
    from agentos.git.service import GitService

    home = new_home("git-ssh")
    home.mkdir(parents=True, mode=0o700)
    srv = home.parent / (home.name + "-sshd")
    srv.mkdir(mode=0o700)
    upstream, port, sshd = serve_repo(srv)
    try:
        make_vault(home)
        monkeypatch.setenv("AGENTOS_HOME", str(home))
        cli = CliRunner()
        host_pub = (srv / "hostkey.pub").read_text().split()
        r = await asyncio.to_thread(cli.invoke, app, [
            "git", "key", "add", "--host", "localhost", "--port", str(port), "--repo", "owner/app",
            "--name", "node-test", "--host-key", f"{host_pub[0]} {host_pub[1]}"])
        assert r.exit_code == 0, r.output
        public = next(line for line in r.output.splitlines() if line.startswith("ssh-ed25519 "))
        (srv / "authorized_keys").write_text(public + "\n")
        r = await asyncio.to_thread(cli.invoke, app, [
            "git", "remote", "allow", "gitnode", "localhost/owner/app",
            "--url", f"ssh://root@localhost:{port}{upstream}"])
        assert r.exit_code == 0, r.output

        policy, rt = make_service(home, clock=Clock())
        from agentos.vault.service import VaultService
        vault = VaultService(home, socket_path=home / "run" / "vault.sock")
        await vault.start(control=False)
        rt.register("vault", vault)
        gitsvc = GitService(rt, home)
        rt.register("git", gitsvc)
        mgr = EnvManager(home, runtime=rt, settings=EnvSettings(lane_idle_s=3600))
        rt.register("envs", mgr)
        await mgr.start()
        mgr.create(LANE, overrides={"network": "isolated", "filesystem": "overlay"})
        g, _ = policy.issue(subject={"id": "node-agent"}, workspaces=["gitnode"], ceiling=Effect.IRREVERSIBLE)
        yield {"mgr": mgr, "grant": g, "upstream": upstream, "home": home, "srv": srv, "policy": policy,
               "vault": vault, "public": public, "git": gitsvc}
        await mgr.shutdown()
        await gitsvc.stop()
        await vault.stop()
        policy.close()
    finally:  # also when setting up failed: no sshd or directory is left behind
        sshd.terminate()
        sshd.wait(10)
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(srv, ignore_errors=True)


async def test_an_isolated_lane_clones_and_pushes_over_ssh_without_the_key(world):
    mgr, g = world["mgr"], world["grant"]
    script = ("set -e; cd /root; git clone -q git@localhost:owner/app.git app; cd app; git checkout -q feature; "
              "echo more > f; git add f; git -c user.name=a -c user.email=a@x commit -qm more; "
              "git push -q origin feature; git rev-parse HEAD; env; ls -la ~ ~/.ssh 2>&1 || true")
    res = await mgr.exec(LANE, script, grant_id=g.id, timeout_s=120)
    assert res.exit_code == 0, res.stderr + res.stdout
    head = res.stdout.splitlines()[0].strip()
    assert run("git", "rev-parse", "feature", cwd=world["upstream"]).stdout.strip() == head
    # the lane talked to its own listener, never to the ssh port, and saw no key
    assert "GIT_CONFIG_COUNT" in res.stdout and "SSH_AUTH_SOCK" not in res.stdout
    assert "PRIVATE KEY" not in res.stdout + res.stderr
    # the owner reads the key back from the vault, only to look for it: no file in the lane, or the
    # node's home or the agent's socket directory, holds a line of it
    private = await world["vault"].get_secret("git:node-test", purpose="node test: look for leaks")
    line = private.splitlines()[2]
    found = await mgr.exec(LANE, f"grep -rlF '{line}' /root /tmp /run /etc /var /opt 2>/dev/null | head -5; "
                                 "echo done", timeout_s=120)
    assert found.stdout.strip() == "done", found.stdout
    agent_dir = world["git"].gateway.agent_dir
    leaked = run("grep", "-rlF", line, str(world["home"]), str(agent_dir), check=False)
    assert leaked.stdout.strip() == "", leaked.stdout
    assert list(agent_dir.iterdir()) == []  # each operation's agent socket is gone with it
    direct = await mgr.exec(LANE, "timeout 5 bash -c 'echo > /dev/tcp/127.0.0.1/22' 2>&1; echo rc=$?")
    assert "rc=0" not in direct.stdout  # the node's ssh port is not reachable from the lane


async def test_undeclared_and_force_push_are_refused_on_the_node(world):
    mgr, g = world["mgr"], world["grant"]
    res = await mgr.exec(LANE, "cd /root && git clone -q git@localhost:owner/other.git other 2>&1; echo rc=$?",
                         grant_id=g.id, timeout_s=60)
    assert "is not declared for gitnode/code" in res.stdout and "rc=0" not in res.stdout
    script = ("set -e; cd /root; rm -rf app; git clone -q git@localhost:owner/app.git app; cd app; "
              "git checkout -q feature; git -c user.name=a -c user.email=a@x commit -q --amend -m rewritten; "
              "git push -f origin feature 2>&1 || true")
    before = run("git", "rev-parse", "feature", cwd=world["upstream"]).stdout.strip()
    res = await mgr.exec(LANE, script, grant_id=g.id, timeout_s=120)
    assert "waits for a person" in res.stdout and "force-pushes refs/heads/feature" in res.stdout
    assert run("git", "rev-parse", "feature", cwd=world["upstream"]).stdout.strip() == before
    [used] = [m for m in await world["vault"].list_secrets() if m["ref"] == "git:node-test"]
    assert used["use_count"] >= 1 and used["last_purpose"].startswith("git ")


async def test_a_lane_with_no_network_reaches_only_its_declared_remote(world):
    mgr, g = world["mgr"], world["grant"]
    mgr.create("gitnode/offline", overrides={"filesystem": "overlay"})
    assert mgr.get("gitnode/offline").profile.network == "off"
    res = await mgr.exec("gitnode/offline", "cd /root && git clone -q git@localhost:owner/app.git app && cat app/README",
                         grant_id=g.id, timeout_s=120)
    assert res.exit_code == 0 and "hello from the node test" in res.stdout, res.stderr
    out = await mgr.exec("gitnode/offline", "curl -sS -m 5 https://example.com -o /dev/null 2>&1; echo rc=$?")
    assert "rc=0" not in out.stdout
