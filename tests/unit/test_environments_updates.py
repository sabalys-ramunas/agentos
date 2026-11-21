"""No upgrades in the middle of a session (field report 2026-11-16: a VM's unattended-upgrades reloaded and
re-executed systemd and restarted networkd, resolved, timesyncd, journald and udevd while an agent worked).
Updates are off on every environment machine, applied when a fresh VM is set up and when the `dev`
snapshot is built, and a keep "live" machine gets them at a quiet moment (openajar.environments.machine,
openajar.environments.updates)."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from openajar.environments import machine, scripts
from openajar.environments.models import Environment, EnvSettings
from openajar.environments.updates import Updates

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def test_every_machine_gets_the_policy_on_its_first_boot_and_at_each_bring_up():
    cfg = machine.cloud_config()
    assert cfg.startswith("bootcmd:\n")  # before apt-daily can start
    b64 = cfg.split("echo ", 1)[1].split(" |", 1)[0]
    assert base64.b64decode(b64).decode() == machine.UPDATES_OFF
    try:
        import yaml
    except ImportError:
        yaml = None
    if yaml is not None:
        doc = yaml.safe_load(cfg)
        assert doc["bootcmd"][0][:2] == ["sh", "-c"] and doc["runcmd"]
    assert machine.UPDATES_CONF in machine.apt_ready()
    assert machine.UPDATES_CONF not in machine.apt_ready("/tmp/root")  # a test root never writes /etc
    off = machine.UPDATES_OFF
    for want in ('APT::Periodic::Unattended-Upgrade "0"', 'APT::Periodic::Update-Package-Lists "0"',
                 "$nrconf{restart} = 'l'", "apt-daily-upgrade.timer", "systemctl is-enabled --quiet"):
        assert want in off


def test_the_dev_snapshot_and_a_fresh_vm_get_the_security_updates_once():
    dev = machine.dev_image_script()
    assert dev.index(machine.UPDATES_CONF) < dev.index(machine.SETUP_PATH)  # baked into the snapshot
    setup = machine.setup_script()
    assert "NEEDRESTART_MODE=l unattended-upgrade" in setup and "/var/lib/agentos-env/upgraded" in setup
    assert "It stays on" not in setup
    assert setup.index("unattended-upgrade;") < setup.index('date -u +%Y-%m-%dT%H:%M:%SZ > "$READY"')


def test_a_snapshot_waits_for_an_upgrade_that_runs():
    assert scripts.SCRUB.startswith(machine.UPGRADE_WAIT)
    assert machine.UPGRADE_WAIT_S < 120  # within a save's step


def test_the_upgrade_script_starts_only_when_asked():
    start, check = machine.upgrade_script(start=True), machine.upgrade_script(start=False)
    assert "setsid -f sh -c" in start and "upgrade: started" in start
    assert "setsid" not in check and "upgrade: due" in check
    assert "NEEDRESTART_MODE=l" in start and f"-mmin -{machine.UPGRADE_EVERY_H * 60}" in start


def test_the_words_for_the_machine_notes():
    w = machine.updates_words
    assert w("upgrade: none\n", NOW) is None and w("", NOW) is None
    assert "being applied now" in w("upgrade: started\n", NOW)
    assert w("upgrade: done 2026-11-16T09:27:27Z\n", NOW) == "security updates applied 2026-11-16 09:27 UTC"
    assert "the next at a quiet moment" in w("upgrade: due 2026-11-14T09:00:00Z\n", NOW)
    late = w("upgrade: due 2026-11-01T09:00:00Z\n", NOW)
    assert late.startswith("security updates waiting since 2026-11-01") and "quiet" in late
    assert w("upgrade: due \n", NOW).startswith("security updates waiting:")  # never applied
    assert w("reboot: required\nupgrade: done 2026-11-16T09:27:27Z\n", NOW).endswith(
        "a new kernel waits for a restart of the machine")
    for text in (late, w("upgrade: started", NOW)):
        assert "\u2014" not in text


class FakeEnvs:
    def __init__(self, out: str):
        self.out = out
        self.scripts: list[str] = []
        self.used = {"env/e7k2": NOW - timedelta(hours=2)}
        self.store = SimpleNamespace(last_used=lambda lane: self.used.get(lane),
                                     touch=lambda lane, at=None: self.used.__setitem__(lane, at))
        self.jobs: list = []

    async def exec(self, lane, script, timeout_s):
        self.scripts.append(script)
        self.used[lane] = NOW  # an exec notes use of the lane
        return SimpleNamespace(stdout=self.out)

    def job_list(self, lane):
        return self.jobs


class Mgr(Updates):
    def __init__(self, envs: FakeEnvs, env: Environment):
        self.envs_ = envs
        self.env = env
        self.settings = EnvSettings()
        self.store = SimpleNamespace(update=self._update)

    def _get(self, name):
        return self.envs_ if name == "envs" else None

    def _update(self, env_id, fn):
        fn(self.env)

    def _busy(self, env):
        return any(j.state == "running" for j in self.envs_.jobs)

    def _last_used(self, env):
        return self.envs_.used["env/e7k2"]


def live_env(**kw) -> Environment:
    return Environment(id="e7k2", title="t", state="ready", grant_id="owner", binding="grant:owner", keep="live",
                       terminal_lane="env/e7k2", browser_lane="env/e7k2-browser", vm_id="vm_1",
                       created_at=NOW - timedelta(days=3), expires_at=NOW + timedelta(days=30), **kw)


async def test_a_quiet_live_machine_starts_its_updates_and_notes_it():
    envs = FakeEnvs("upgrade: started\n")
    env = live_env()
    mgr = Mgr(envs, env)
    words = await mgr.quiet_upgrade(env, NOW)
    assert "being applied now" in words and env.machine.updates == words
    assert "setsid -f" in envs.scripts[-1]
    assert envs.used["env/e7k2"] == NOW - timedelta(hours=2), "the node's own command is no use of the machine"
    assert await mgr.quiet_upgrade(env, NOW + timedelta(minutes=5)) is None  # asked at most hourly


async def test_a_busy_live_machine_only_asks_and_other_machines_are_left_alone():
    envs = FakeEnvs("upgrade: due 2026-11-01T09:00:00Z\n")
    envs.jobs = [SimpleNamespace(state="running")]  # a dev server that always runs
    env = live_env()
    mgr = Mgr(envs, env)
    words = await mgr.quiet_upgrade(env, NOW)
    assert "setsid" not in envs.scripts[-1] and words.startswith("security updates waiting since")
    envs.jobs = []
    envs.used["env/e7k2"] = NOW + timedelta(hours=2, minutes=-5)  # used a moment ago
    await mgr.quiet_upgrade(env, NOW + timedelta(hours=2))
    assert "setsid" not in envs.scripts[-1]
    saved = live_env()
    saved.keep = "save"
    assert await Mgr(envs, saved).quiet_upgrade(saved, NOW) is None
