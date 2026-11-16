"""`agentos setup`, the guided setup of local mode, run end to end with fakes.

Every outside thing is a fake: the Hetzner and GitHub APIs, gh, docker, the local service and its
registration of Claude Code and Codex (agentos.local, built by another builder), and the test VM.
HOME and AGENTOS_HOME are temp folders, so nothing touches this computer's real setup. The vault
is a real one, in the temp home.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from agentos.cli import app
from agentos.setup import steps

HZ_TOKEN = "hz_" + "h" * 40
GH_TOKEN = "gho_" + "g" * 36
PASTED = "github_pat_" + "p" * 40


@dataclass
class State:
    running: bool
    pid: int | None = None
    port: int = 7410
    url: str = "http://127.0.0.1:7410/mcp"
    log_path: Path | None = None
    note: str = ""


@dataclass
class World:
    """What the fakes saw and what they answer."""
    home: Path
    hetzner_ok: set = field(default_factory=lambda: {HZ_TOKEN})
    docker: tuple = (True, True, False)
    gh_installed: bool = True
    gh_signed_in: bool = True
    github_users: dict = field(default_factory=lambda: {GH_TOKEN: "octo", PASTED: "octo"})
    running: bool = False
    snapshot: bool | None = True  # the Hetzner project has the dev snapshot (None: Hetzner did not say)
    snapshot_fails: str | None = None
    calls: list = field(default_factory=list)

    # the outside world
    def hetzner_check(self, token, api_url):
        self.calls.append(("hetzner_check", token == HZ_TOKEN))
        return None if token in self.hetzner_ok else "Hetzner: unauthorized: the token was refused"

    def dev_snapshot_state(self, home):
        self.calls.append(("snapshot?",))
        return self.snapshot

    def build_dev_snapshot(self, home, say):
        self.calls.append(("build snapshot",))
        if self.snapshot_fails:
            from agentos.vms.models import VmError

            raise VmError(self.snapshot_fails)
        say("creating build VM vm_1 (cx23 in nbg1 from ubuntu-24.04)")
        self.snapshot = True
        return 0.0123

    def docker_state(self, docker_bin, image):
        return self.docker

    def build_docker_image(self, image, docker_bin):
        self.calls.append(("build", image))
        self.docker = (True, True, True)
        return True

    def gh_available(self):
        return self.gh_installed

    def gh(self, *args, interactive=False):
        self.calls.append(("gh", args[:2], interactive))
        if args[:2] == ("auth", "status"):
            return (0, "github.com\n  ✓ Logged in to github.com account octo (keyring)\n") if self.gh_signed_in \
                else (1, "You are not logged into any GitHub hosts.")
        if args[:2] == ("auth", "login"):
            self.gh_signed_in = True
            return 0, ""
        if args[:2] == ("auth", "token"):
            return (0, GH_TOKEN + "\n") if self.gh_signed_in else (1, "")
        raise AssertionError(args)

    def github_user(self, token):
        self.calls.append(("github_user",))
        user = self.github_users.get(token)
        return (user, None) if user else (None, "GitHub refused it (expired, revoked or mistyped)")

    def open_test_vm(self, home):
        self.calls.append(("test_vm",))
        return "Linux agentos-vm 6.8.0-49-generic #49-Ubuntu SMP x86_64 GNU/Linux", 0.0106

    # agentos.local (the contract of builder L1a)
    def status(self, home):
        return State(running=self.running, log_path=home / "logs" / "service.log")

    def start(self, home, port=None):
        self.calls.append(("start", port))
        self.running = True
        return self.status(home)

    def stop(self, home):
        self.calls.append(("stop",))
        self.running = False
        return self.status(home)

    def register_clients(self, home, port, *, claude=True, codex=True, skills=True):
        self.calls.append(("register", port, claude, codex, skills))
        return [w for w, on in (("Claude Code: added agentos to ~/.claude.json", claude),
                                ("Codex: added agentos to ~/.codex/config.toml", codex),
                                ("Skills: agentos-environment", skills)) if on]


@pytest.fixture
def world(tmp_path, monkeypatch):
    user = tmp_path / "user"
    user.mkdir()
    home = tmp_path / "agentos-home"
    monkeypatch.setenv("HOME", str(user))  # nothing reaches the real ~/.claude.json, ~/.codex, ~/.agentos
    monkeypatch.setenv("AGENTOS_HOME", str(home))
    w = World(home)
    for name in ("hetzner_check", "docker_state", "build_docker_image", "gh_available", "gh", "github_user",
                 "open_test_vm", "dev_snapshot_state", "build_dev_snapshot"):
        monkeypatch.setattr(steps, name, getattr(w, name))
    modules = {"service": SimpleNamespace(status=w.status, start=w.start, stop=w.stop),
               "register": SimpleNamespace(register_clients=w.register_clients),
               "paths": SimpleNamespace(default_home=lambda: user / ".agentos", DEFAULT_PORT=7410)}
    monkeypatch.setattr(steps, "local", lambda name: modules[name])
    _keep_fields(monkeypatch)
    return w


def _keep_fields(monkeypatch):
    """environments.toml's keep keys (default_keep, allow_keep_live) come with kept environments
    (agentos.environments.models). Until that model has them, the tests read and write them on a model
    that does, so the step is tested as it runs with them."""
    from typing import Literal

    from agentos.environments import models

    if "default_keep" in models.EnvSettings.model_fields:
        return

    class WithKeep(models.EnvSettings):
        default_keep: Literal["none", "save", "live"] = "none"
        allow_keep_live: bool = False

    monkeypatch.setattr(models, "EnvSettings", WithKeep)


def run(*args, input=None):
    return CliRunner().invoke(app, ["setup", *args], input=input, prog_name="agentos")


def _no_secrets(out: str) -> None:
    for token in (HZ_TOKEN, GH_TOKEN, PASTED):
        assert token not in out
    assert "—" not in out  # never an em dash in what a person reads


def test_quick_start_with_hetzner_and_gh(world):
    home = world.home
    # quick start; Hetzner; the token; gh's login; yes to any repository; yes to the test VM
    r = run(input="1\n1\n" + HZ_TOKEN + "\n1\n\n\ny\n")  # the keep question takes its default
    out = r.output
    assert r.exit_code == 0, out
    _no_secrets(out)
    assert "Welcome to agentos" in out and "about EUR 0.011 an hour" in out
    assert "nothing until a VM opens" in out
    for n, title in enumerate(("VMs", "GitHub", "Limits", "Your agents", "Health check", "A test VM"), 1):
        assert f"Step {n} of 6: {title}" in out
    # VMs: the token where `agentos vm setup` keeps it, 0600; vms.toml says hetzner
    token_file = home / "secrets" / "hetzner.token"
    assert token_file.read_text().strip() == HZ_TOKEN and oct(token_file.stat().st_mode & 0o777) == "0o600"
    vms = tomllib.loads((home / "vms.toml").read_text())
    assert vms["provider"] == "hetzner" and vms["location"] == "nbg1"
    # GitHub: gh's token, sealed in the vault, any repository on
    saved = json.loads((home / "git" / "login.json").read_text())
    assert saved["login"] == "octo" and saved["source"] == "gh" and saved["any_repo"] is True
    assert GH_TOKEN not in (home / "git" / "login.json").read_text()
    assert (home / "vault.db").exists() and (home / "secrets" / "vault.key").exists()
    assert "When gh signs in again or the token changes, run `agentos setup github` again" in out
    # limits: the low local defaults
    env = tomllib.loads((home / "environments.toml").read_text())
    assert env["monthly_eur"] == 10 and env["max_hours"] == 4 and env["max_live"] == 2
    # single use by default, and never kept running unless the owner says so
    assert env["default_keep"] == "none" and "allow_keep_live" not in env
    assert "How should an environment stay after the work?" in out and "Single use" in out
    # the service started, both agents and the skills connected on its port
    assert ("start", None) in world.calls and ("register", 7410, True, True, True) in world.calls
    assert "runs on this computer at http://127.0.0.1:7410/mcp" in out
    # the check, the test VM, the summary and the one next step
    assert "ready  Service  running at http://127.0.0.1:7410/mcp" in out
    assert "ready  GitHub   octo (from gh): any repository it can reach" in out
    assert ("test_vm",) in world.calls and "It answered: Linux agentos-vm" in out and "EUR 0.011" in out
    assert "All set. Nothing bills until an agent opens a VM." in out
    assert "Next: start a new Claude Code session and ask:" in out
    assert "work in agentos on github.com/octo/<repo>" in out


def test_the_vault_holds_the_gh_token_for_the_gateway(world):
    import asyncio

    from agentos.commands.base import cli_runtime
    from agentos.git import keys as keymod
    from agentos.git import login

    r = run("github", input="1\n\n")
    assert r.exit_code == 0, r.output
    vault = cli_runtime().get("vault")
    assert asyncio.run(vault.get_secret(login.REF, purpose="test")) == GH_TOKEN
    [cred] = [c for c in (keymod.from_meta(m) for m in asyncio.run(vault.list_secrets())) if c]
    assert cred.any_repo and cred.host == "github.com"


def test_step_by_step_asks_each_setting(world):
    home = world.home
    world.docker = (False, False, False)
    # step by step; Hetzner; token; Helsinki; medium; no gh: paste a token, no to any repository;
    # limits 25, 6, 3; saves when idle; Claude Code yes, Codex no, skills yes; no test VM
    world.gh_installed = False
    r = run(input="2\n1\n" + HZ_TOKEN + "\n3\n2\n1\n" + PASTED + "\nn\n25\n6\n3\n2\ny\nn\ny\nn\n")
    out = r.output
    assert r.exit_code == 0, out
    _no_secrets(out)
    vms = tomllib.loads((home / "vms.toml").read_text())
    assert vms["location"] == "hel1" and vms["default_profile"] == "medium"
    assert vms["profiles"]["medium"]["min_cpu"] == 4 and "small" in vms["profiles"]
    saved = json.loads((home / "git" / "login.json").read_text())
    assert saved["source"] == "token" and saved["any_repo"] is False
    assert "Connect repositories one by one: agentos environments repo add github.com/<owner>/<name>" in out
    env = tomllib.loads((home / "environments.toml").read_text())
    assert (env["monthly_eur"], env["max_hours"], env["max_live"]) == (25, 6, 3)
    assert env["default_keep"] == "save" and "allow_keep_live" not in env
    assert "Environments: saved when idle." in out
    assert ("register", 7410, True, False, True) in world.calls
    assert ("test_vm",) not in world.calls and "No test VM" in out


def test_yes_with_docker_asks_nothing_and_never_opens_a_test_vm(world):
    world.gh_signed_in = False
    r = run("--yes", "--docker", input="")
    out = r.output
    assert r.exit_code == 0, out
    _no_secrets(out)
    assert tomllib.loads((world.home / "vms.toml").read_text())["provider"] == "docker"
    assert ("build", "agentos-env:latest") in world.calls
    assert "public repositories only" in out and not (world.home / "git" / "login.json").exists()
    assert ("test_vm",) not in world.calls and ("register", 7410, True, True, True) in world.calls
    assert "Choose" not in out  # no question was asked


def test_yes_reads_the_tokens_from_files(world, tmp_path):
    hz, ghf = tmp_path / "hz", tmp_path / "gh"
    hz.write_text(HZ_TOKEN + "\n")
    ghf.write_text(PASTED + "\n")
    r = run("--yes", "--hetzner-token-file", str(hz), "--github-token-file", str(ghf))
    assert r.exit_code == 0, r.output
    _no_secrets(r.output)
    assert (world.home / "secrets" / "hetzner.token").read_text().strip() == HZ_TOKEN
    saved = json.loads((world.home / "git" / "login.json").read_text())
    assert saved["source"] == "token" and saved["any_repo"] is True  # yes is the default in local mode


def test_a_refused_hetzner_token_is_asked_again_and_never_saved(world):
    r = run("vms", input="1\nnot-a-token\n" + HZ_TOKEN + "\n\n\n")  # then the location and size defaults
    out = r.output
    assert r.exit_code == 0, out
    assert "That token did not work: Hetzner: unauthorized" in out and "not-a-token" not in out
    assert (world.home / "secrets" / "hetzner.token").read_text().strip() == HZ_TOKEN


def test_hetzner_offers_the_dev_snapshot_and_says_what_it_costs(world):
    world.snapshot = False
    r = run("vms", input="1\n" + HZ_TOKEN + "\n\n\ny\n")  # token, location, size, yes to the snapshot
    out = r.output
    assert r.exit_code == 0, out
    _no_secrets(out)
    assert "each VM installs the browser when it opens. That adds 2 to 4 minutes" in out
    assert "a few minutes and costs a cent or two" in out and "about 4 GB" in out and "per GB a month" in out
    assert ("build snapshot",) in world.calls and "  creating build VM vm_1" in out
    assert "The dev snapshot is built" in out and "EUR 0.012" in out


def test_no_to_the_dev_snapshot_builds_nothing_and_says_how_to_later(world):
    world.snapshot = False
    r = run("vms", input="1\n" + HZ_TOKEN + "\n\n\nn\n")
    assert r.exit_code == 0, r.output
    assert ("build snapshot",) not in world.calls
    assert "`agentos vm image build dev`" in r.output and "—" not in r.output


def test_yes_never_builds_the_dev_snapshot(world, tmp_path):
    world.snapshot = False
    hz = tmp_path / "hz"
    hz.write_text(HZ_TOKEN + "\n")
    r = run("--yes", "--hetzner-token-file", str(hz))
    assert r.exit_code == 0, r.output
    assert ("build snapshot",) not in world.calls and "No snapshot for now" in r.output


def test_a_dev_snapshot_there_already_is_not_offered_again(world):
    r = run("vms", input="1\n" + HZ_TOKEN + "\n\n\n")
    assert r.exit_code == 0, r.output
    assert "The dev snapshot is there" in r.output and ("build snapshot",) not in world.calls


def test_a_failed_dev_snapshot_build_says_environments_still_work(world):
    world.snapshot, world.snapshot_fails = False, "the setup script failed with exit code 100: E: no xvfb"
    r = run("vms", input="1\n" + HZ_TOKEN + "\n\n\ny\n")
    assert r.exit_code == 0, r.output
    assert "The dev snapshot was not built: the setup script failed" in r.output
    assert "Environments still work" in r.output


def test_hetzner_that_cannot_say_about_the_snapshot_offers_nothing(world):
    world.snapshot = None
    r = run("vms", input="1\n" + HZ_TOKEN + "\n\n\n")
    assert r.exit_code == 0, r.output
    assert "could not check for the dev snapshot" in r.output and ("build snapshot",) not in world.calls


def test_docker_says_its_image_has_the_browser(world):
    r = run("--yes", "--docker", input="")
    assert r.exit_code == 0, r.output
    assert "The machine image has each environment's browser" in r.output
    assert ("snapshot?",) not in world.calls


def test_skipping_the_hetzner_token_saves_nothing(world):
    r = run("vms", input="1\n\n")
    assert r.exit_code == 0, r.output
    assert "No Hetzner token saved" in r.output and not (world.home / "secrets" / "hetzner.token").exists()


def test_a_refused_github_token_saves_nothing(world):
    world.gh_installed = False
    r = run("github", input="1\nghp_wrongwrongwrong\n")
    assert r.exit_code == 0, r.output
    assert "That token did not work: GitHub refused it" in r.output and "ghp_wrong" not in r.output
    assert not (world.home / "git" / "login.json").exists()


def test_gh_login_runs_interactively_when_not_signed_in(world):
    world.gh_signed_in = False
    r = run("github", input="1\n\n")
    assert r.exit_code == 0, r.output
    assert ("gh", ("auth", "login"), True) in world.calls
    assert json.loads((world.home / "git" / "login.json").read_text())["source"] == "gh"


def test_github_again_keeps_or_removes_the_saved_login(world):
    assert run("github", input="1\n\n").exit_code == 0
    kept = run("github", input="1\nn\n")  # keep it, but no to any repository now
    assert kept.exit_code == 0, kept.output
    assert "Keep the saved login (octo)" in kept.output
    assert json.loads((world.home / "git" / "login.json").read_text())["any_repo"] is False
    gone = run("github", input="5\n")  # keep, gh, token, skip, remove
    assert gone.exit_code == 0, gone.output
    assert "Removed the saved GitHub login" in gone.output and not (world.home / "git" / "login.json").exists()


def test_vms_again_restarts_a_running_service(world):
    world.running = True
    r = run("vms", "--docker", input="\nn\n")  # Docker (the default with --docker), no image build
    assert r.exit_code == 0, r.output
    assert ("stop",) in world.calls and "Restarted the agentos service" in r.output


def test_without_the_local_runtime_it_says_so(world, monkeypatch):
    def missing(name):
        raise ModuleNotFoundError(f"No module named 'agentos.local.{name}'", name=f"agentos.local.{name}")

    monkeypatch.setattr(steps, "local", missing)
    r = run("agents")
    assert r.exit_code == 0 and "not part of this build of agentos yet" in r.output
    r = run("check")
    assert "to do  Service  not part of this build yet" in r.output


def test_check_says_each_piece_in_plain_words(world):
    r = run("check")
    out = r.output
    assert r.exit_code == 0, out
    assert "to do  Service  not running: `agentos setup agents` starts it" in out
    assert "to do  VMs      not set up: agentos setup vms" in out
    assert "note   GitHub   no login: public repositories only" in out
    assert "ready  Limits" in out


def test_stopping_early_says_how_to_go_on(world):
    r = run(input="1\n")  # quick start, then the input ends
    assert r.exit_code == 1 and "Setup stopped" in r.output and "agentos setup --yes" in r.output


def test_an_unknown_step_is_refused(world):
    r = run("browsers")
    assert r.exit_code == 2 and "is not a step" in r.output


def test_home_defaults_to_the_local_one(world, monkeypatch):
    monkeypatch.delenv("AGENTOS_HOME")
    r = run("limits", "--yes")
    assert r.exit_code == 0, r.output
    assert (Path(os.environ["HOME"]) / ".agentos" / "environments.toml").exists()


# ------------------------------------------------------------------ the snapshot's outside calls, with a fake Hetzner


def test_the_snapshot_check_reads_hetzner_and_tells_missing_from_unknown(tmp_path, monkeypatch):
    from agentos.core.errors import ServiceUnavailable
    from agentos.vms.manager import VmManager
    from agentos.vms.models import VmError

    answers: list = []

    class Hetzner:
        async def resolve_image(self, image):
            assert image == "snapshot:dev"
            if answers[0] is not None:
                raise answers[0]
            return "123"

    monkeypatch.setattr(VmManager, "provider", lambda self: Hetzner())
    for answer, state in ((None, True), (VmError("no snapshot named dev; build it"), False),
                          (ServiceUnavailable("Hetzner did not answer"), None)):
        answers[:] = [answer]
        assert steps.dev_snapshot_state(tmp_path) is state


def test_the_snapshot_build_is_vm_image_build_dev_with_the_browser(tmp_path, monkeypatch):
    from agentos.environments.vmbrowser import install_script
    from agentos.vms.manager import VmManager, image_script

    seen = {}

    async def build_image(self, name, *, profile=None, script=None, on_step=None):
        seen.update(name=name, script=script, follow_ip=self.follow_ip)
        on_step("taking the snapshot")
        return {"image": name, "cost_eur": 0.02}

    monkeypatch.setattr(VmManager, "build_image", build_image)
    said: list = []
    assert steps.build_dev_snapshot(tmp_path, said.append) == 0.02
    assert seen == {"name": "dev", "script": image_script(install_script()), "follow_ip": True}
    assert said == ["taking the snapshot"]


def test_keep_running_is_allowed_only_after_the_cost_is_said_and_confirmed(world):
    # the limits as they are, the third option, then no to the cost: it saves when idle and live stays off
    keep = "\n\n\n"
    r = run("limits", input=keep + "3\nn\n")
    out = r.output
    assert r.exit_code == 0, out
    _no_secrets(out)
    assert "about EUR 7.80 a month" in out and "Not allowed." in out
    env = tomllib.loads((world.home / "environments.toml").read_text())
    assert env["default_keep"] == "save" and "allow_keep_live" not in env
    # again, and yes: allowed, while the default stays save (an agent keeps one running only when asked)
    r = run("limits", input=keep + "3\ny\n")
    assert r.exit_code == 0, r.output
    env = tomllib.loads((world.home / "environments.toml").read_text())
    assert env["default_keep"] == "save" and env["allow_keep_live"] is True
    assert "kept running when you ask (it bills every hour)" in r.output
    # again: the default is what is set now, so Enter keeps it and nothing asks about the cost
    r = run("limits", input=keep + "\n")
    assert r.exit_code == 0 and "about EUR 7.80" not in r.output
    assert tomllib.loads((world.home / "environments.toml").read_text())["allow_keep_live"] is True
    # single use now: live is turned off with it
    r = run("limits", input=keep + "1\n")
    env = tomllib.loads((world.home / "environments.toml").read_text())
    assert env["default_keep"] == "none" and env["allow_keep_live"] is False


def test_yes_keeps_single_use_and_asks_nothing_about_keeping(world):
    r = run("--yes", "--docker", input="")
    assert r.exit_code == 0, r.output
    env = tomllib.loads((world.home / "environments.toml").read_text())
    assert env["default_keep"] == "none" and "allow_keep_live" not in env
    assert "Keeping  single use, deleted when the work is done" in r.output
