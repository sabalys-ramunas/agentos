"""The agent's own ssh in local mode (agentos.environments.sshaccess): the host entry in
AGENTOS_HOME/ssh/config is written when an environment is ready and goes when it closes or fails, the
Include at the top of ~/.ssh/config, no `ssh` on a server node, and the agent's ssh sessions counting as
use for the idle rule. HOME is a temporary folder in every test: nothing reaches this computer's ~/.ssh.
The fake machine answers the node's commands (no SSH here)."""

from __future__ import annotations

import stat
from datetime import timedelta

import pytest
from test_environments import node, opened  # noqa: F401  (node is a fixture)

from agentos.core.models import utcnow
from agentos.env.models import ExecResult
from agentos.environments import sshaccess


@pytest.fixture(autouse=True)
def user_home(tmp_path, monkeypatch):
    user = tmp_path / "user"
    user.mkdir()
    monkeypatch.setenv("HOME", str(user))
    return user


class Ssh:
    """What the machine says about the agent's ssh (sshaccess.ACTIVITY), and what was authorized."""

    def __init__(self, node):  # noqa: F811
        self.age_s, self.live = -1, 0
        self.authorized: list[str] = []
        self.refuse = False
        fake = node["machine"]
        inner = fake.exec

        async def exec(lane_id, cmd, *, timeout_s=None, stdin=None, grant_id=None, max_bytes=None):
            if cmd == sshaccess.ACTIVITY:
                fake.ran.append((cmd, grant_id))
                node["envs"].store.touch(lane_id)  # as EnvManager.exec does after every command
                last = 0 if self.age_s < 0 else 1_000_000 - self.age_s
                return ExecResult(exit_code=0, stdout=f"agentos-ssh now=1000000 last={last} live={self.live}\n",
                                  stderr="", duration_s=0.01)
            if cmd == "sh -s" and stdin and sshaccess.WRAPPER in stdin:
                fake.ran.append((cmd, grant_id))
                if self.refuse:
                    return ExecResult(exit_code=1, stdout="", stderr="read-only file system", duration_s=0.01)
                self.authorized.append(stdin)
                return ExecResult(exit_code=0, stdout="", stderr="", duration_s=0.01)
            return await inner(lane_id, cmd, timeout_s=timeout_s, stdin=stdin, grant_id=grant_id,
                               max_bytes=max_bytes)

        node["envs"].exec = exec


@pytest.fixture
def local(node):  # noqa: F811
    node["mgr"].on_laptop = True
    node["ssh"] = Ssh(node)
    return node


def config(node) -> str:  # noqa: F811
    path = sshaccess.config_path(node["mgr"].home)
    return path.read_text() if path.exists() else ""


async def test_ready_writes_the_entry_and_the_result_says_how_to_use_it(local, user_home):
    out = await opened(local, repo="example.test/me/shop")
    eid, home = out["id"], local["mgr"].home
    text = config(local)
    rec = local["vms"].store.get(local["mgr"].get(eid).vm_id)
    key = home / "ssh" / eid / "id_ed25519"
    kh = home / "ssh" / eid / "known_hosts"
    for line in (f"Host agentos-{eid}", f"HostName {rec.host}", "User root", f"IdentityFile {key}",
                 "IdentitiesOnly yes", "ForwardAgent no", f"HostKeyAlias agentos-{eid}",
                 f"UserKnownHostsFile {kh}", "StrictHostKeyChecking yes", "ServerAliveInterval 15"):
        assert f"\n  {line}\n" in text or text.startswith(line) or f"\n{line}\n" in text, line
    # the agent's own key, not the node's VM key; the host key the node pinned, under the entry's alias
    assert key.exists() and stat.S_IMODE(key.stat().st_mode) == 0o600
    pinned = (home / "vms" / rec.id / "known_hosts").read_text().split()[1:3]
    assert kh.read_text().split() == [f"agentos-{eid}", *pinned]
    assert stat.S_IMODE(sshaccess.config_path(home).stat().st_mode) == 0o600
    # on the machine: that key, restricted, through the forced command that notes each session
    [script] = local["ssh"].authorized
    pub = (home / "ssh" / eid / "id_ed25519.pub").read_text().split()[1]
    assert f'restrict,pty,command="{sshaccess.WRAPPER}" ssh-ed25519 {pub} {sshaccess.KEY_COMMENT}' in script
    # no Include in ~/.ssh/config yet: the command names the agentos config
    cfg = str(sshaccess.config_path(home))  # this node's home is not under HOME: the full path
    assert out["ssh"] == {"host": f"agentos-{eid}", "command": f"ssh -F {cfg} agentos-{eid}",
                          "examples": [f"ssh -F {cfg} agentos-{eid} 'cd /work/shop && pnpm test'",
                                       (f"rsync -az -e 'ssh -F {cfg}' --exclude node_modules --exclude .git ./ "
                                       f"agentos-{eid}:/work/shop/"),
                                       f"scp -F {cfg} agentos-{eid}:/work/shop/dist.tgz ."]}
    assert "You may also use your own shell: ssh -F" in out["next"] and "not in agentos's call log" in out["next"]
    assert "—" not in out["next"]
    # with the Include: plain ssh, and environment() says the same
    assert sshaccess.add_include(home)
    got = await local["call"]("environment")
    assert got["ssh"]["command"] == f"ssh agentos-{eid}"
    assert got["ssh"]["examples"][0] == f"ssh agentos-{eid} 'cd /work/shop && pnpm test'"
    assert got["ssh"]["examples"][1].startswith("rsync -az --exclude node_modules")
    # never in the rows that list environments
    assert all("ssh" not in row for row in got["open"])


async def test_close_removes_the_entry_and_its_key_and_keeps_the_others(local):
    one = await opened(local, title="One")
    two = await opened(local, title="Two", session="chat-2")
    home = local["mgr"].home
    assert f"Host agentos-{one['id']}" in config(local) and f"Host agentos-{two['id']}" in config(local)
    await local["call"]("environment_close", on="One")
    text = config(local)
    assert f"agentos-{one['id']}" not in text and f"Host agentos-{two['id']}" in text
    assert not (home / "ssh" / one["id"]).exists() and (home / "ssh" / two["id"] / "id_ed25519").exists()


async def test_a_failed_open_leaves_no_entry(local):
    local["machine"].setup = "status: error\nE: Unable to locate package nodejs"
    with pytest.raises(Exception, match="environment_failed"):
        await opened(local)
    env = local["mgr"].store.recent(1)[0]
    assert "agentos-" not in config(local) and not (local["mgr"].home / "ssh" / env.id).exists()


async def test_the_entry_goes_when_the_reaper_closes_it_and_comes_back_after_a_restart(local):
    out = await opened(local)
    mgr = local["mgr"]
    sshaccess.remove_entry(mgr.home, out["id"])  # as after a node restart that lost it
    await mgr.reap_once()
    assert f"Host agentos-{out['id']}" in config(local) and len(local["ssh"].authorized) == 2
    later = utcnow() + timedelta(minutes=mgr.settings.idle_minutes + 1)
    got = await mgr.reap_once(later)
    assert got["closed"] == [out["id"]] and "agentos-" not in config(local)


async def test_a_machine_that_refuses_the_key_still_comes_up_without_ssh(local):
    local["ssh"].refuse = True
    out = await opened(local)
    assert out["state"] == "ready" and "ssh" not in out and "agentos-" not in config(local)
    await local["mgr"].reap_once()  # tried once: the reaper does not try again every minute
    assert sum(1 for c, _ in local["machine"].ran if c == "sh -s") == 1  # tried at ready, not again


async def test_a_server_node_gives_no_ssh(node, user_home):  # noqa: F811
    assert node["mgr"].on_laptop is False
    out = await opened(node, repo="example.test/me/shop")
    assert "ssh" not in out and "own shell" not in out["next"]
    assert "ssh" not in await node["call"]("environment")
    assert not sshaccess.config_path(node["mgr"].home).exists()
    await node["mgr"].reap_once()
    assert not any(c == sshaccess.ACTIVITY for c, _ in node["machine"].ran)


# ------------------------------------------------------------------ idle


async def test_a_running_ssh_command_keeps_the_machine(local):
    """An agent that works only over ssh makes no agentos call: its running session is use."""
    out = await opened(local)
    mgr = local["mgr"]
    later = utcnow() + timedelta(minutes=mgr.settings.idle_minutes + 1)
    local["ssh"].age_s, local["ssh"].live = 0, 1  # `ssh agentos-<id> 'pnpm test'` runs now
    got = await mgr.reap_once(later)
    assert got["closed"] == [] and mgr.get(out["id"]).state == "ready"
    # it ended 10 minutes before the next pass: still in use within the idle window
    local["ssh"].age_s, local["ssh"].live = 600, 0
    got = await mgr.reap_once(later + timedelta(minutes=10))
    assert got["closed"] == []
    # and once nothing ran for longer than the window, it is idle again
    local["ssh"].age_s = 60 * (mgr.settings.idle_minutes + 1)
    got = await mgr.reap_once(later + timedelta(minutes=10 + mgr.settings.idle_minutes + 1))
    assert got["closed"] == [out["id"]]


async def test_the_nodes_own_check_is_not_use(local):
    """The check is a command of the node's on the terminal lane: it must not count as use itself."""
    out = await opened(local)
    mgr = local["mgr"]
    before = mgr._last_used(mgr.get(out["id"]))
    await mgr.reap_once()
    assert any(c == sshaccess.ACTIVITY for c, _ in local["machine"].ran)
    assert mgr._last_used(mgr.get(out["id"])) == before
    later = utcnow() + timedelta(minutes=mgr.settings.idle_minutes + 1)
    assert (await mgr.reap_once(later))["closed"] == [out["id"]]


def test_the_activity_line_is_read_by_the_machines_clock():
    assert sshaccess.parse_activity("agentos-ssh now=5000 last=4400 live=0\n") == (600, 0)
    assert sshaccess.parse_activity("agentos-ssh now=5000 last=5000 live=2") == (0, 2)
    assert sshaccess.parse_activity("agentos-ssh now=5000 last=0 live=0") == (-1, 0)  # never used
    assert sshaccess.parse_activity("bash: stat: not found") is None


# ------------------------------------------------------------------ ~/.ssh/config


def test_the_include_goes_at_the_top_once_and_keeps_the_rest(tmp_path, user_home):
    home = user_home / ".agentos"
    cfg = user_home / ".ssh" / "config"
    cfg.parent.mkdir(mode=0o700)
    mine = "Host *\n  ForwardAgent yes\n\nHost box\n  HostName 192.0.2.4\n  User me\n"
    cfg.write_text(mine)
    cfg.chmod(0o644)
    assert not sshaccess.included(home)
    assert sshaccess.add_include(home) is True
    text = cfg.read_text()
    first = next(line for line in text.splitlines() if line and not line.startswith("#"))
    assert first == "Include ~/.agentos/ssh/config" and text.endswith(mine)
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o644  # the person's own mode stays
    assert sshaccess.included(home) and sshaccess.config_path(home).exists()
    assert sshaccess.add_include(home) is False and cfg.read_text() == text  # idempotent


def test_a_linked_ssh_config_stays_a_link(user_home):
    """A dotfiles setup links ~/.ssh/config to its repository: the Include goes into that file."""
    home = user_home / ".agentos"
    real = user_home / "dotfiles" / "ssh_config"
    real.parent.mkdir()
    real.write_text("Host box\n  User me\n")
    cfg = user_home / ".ssh" / "config"
    cfg.parent.mkdir(mode=0o700)
    cfg.symlink_to(real)
    assert sshaccess.add_include(home) is True
    assert cfg.is_symlink() and "Include ~/.agentos/ssh/config" in real.read_text()
    assert real.read_text().endswith("Host box\n  User me\n")


def test_a_missing_ssh_config_is_made_private(user_home):
    home = user_home / ".agentos"
    assert sshaccess.add_include(home) is True
    cfg = user_home / ".ssh" / "config"
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600 and stat.S_IMODE(cfg.parent.stat().st_mode) == 0o700
    assert "Include ~/.agentos/ssh/config" in cfg.read_text()


def test_an_include_the_person_wrote_another_way_counts(user_home):
    home = user_home / ".agentos"
    cfg = user_home / ".ssh" / "config"
    cfg.parent.mkdir()
    cfg.write_text(f"Host box\n  User me\n\ninclude {home}/ssh/config\n")
    assert sshaccess.included(home) and sshaccess.add_include(home) is False


def test_another_home_is_included_by_its_full_path(tmp_path, user_home):
    home = tmp_path / "elsewhere"
    assert sshaccess.add_include(home)
    assert f"Include {home}/ssh/config" in (user_home / ".ssh" / "config").read_text()


def test_entries_are_replaced_and_removed_alone(tmp_path):
    from types import SimpleNamespace

    home = tmp_path / "h"
    kh = tmp_path / "kh"
    kh.write_text("agentos-vm_1 ssh-ed25519 AAAAhost\n")
    remote = SimpleNamespace(host="192.0.2.9", port=22, user="root", known_hosts=str(kh), vm_id="vm_1")
    for eid in ("eaaaa", "ebbbb"):
        key, _pub = sshaccess.make_key(home, eid)
        sshaccess.write_entry(home, eid, remote, key)
    remote.host = "192.0.2.10"
    sshaccess.write_entry(home, "eaaaa", remote, home / "ssh" / "eaaaa" / "id_ed25519")  # a resume: new address
    text = sshaccess.config_path(home).read_text()
    assert text.count("Host agentos-eaaaa") == 1 and "HostName 192.0.2.10" in text and "HostName 192.0.2.9" in text
    assert sshaccess.remove_entry(home, "ebbbb") is True and sshaccess.remove_entry(home, "ebbbb") is False
    assert "ebbbb" not in sshaccess.config_path(home).read_text()
    with pytest.raises(ValueError):
        sshaccess.make_key(home, "../x")

