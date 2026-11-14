"""The node templates in deploy/templates: the cloud-init files parse and carry what the first boot
needs, the shell scripts parse, create.sh refuses bad input and never prints a token, and each
manifest matches its files. Nothing here touches a real server, account or token."""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "deploy" / "templates"
COMMON = TEMPLATES / "common"
HETZNER = TEMPLATES / "hetzner"
ENV_PATH = "/etc/agentos/bootstrap.env"
FIRST_BOOT = "/usr/local/sbin/agentos-first-boot"
FAKE_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITESTONLY test@example"
SENTINEL_TOKEN = "ajt_SENTINEL_do_not_print"


def manifest(name: str) -> dict:
    return tomllib.loads((TEMPLATES / name / "template.toml").read_text())


def variables(name: str) -> dict[str, dict]:
    return {v["name"]: v for v in manifest(name)["variables"]}


def load_cloud_config(path: Path) -> dict:
    text = path.read_text()
    assert text.splitlines()[0] == "#cloud-config", f"{path} must start with #cloud-config"
    doc = yaml.safe_load(text)
    assert isinstance(doc, dict), path
    return doc


def written_file(doc: dict, path: str) -> dict:
    (entry,) = [f for f in doc.get("write_files", []) if f["path"] == path]
    return entry


def env_keys(text: str) -> set[str]:
    return {line.split("=", 1)[0] for line in text.splitlines() if "=" in line and not line.startswith("#")}


def test_hetzner_user_data_is_cloud_config_with_the_first_boot_pieces():
    doc = load_cloud_config(HETZNER / "cloud-init.yaml")
    assert {"packages", "package_update", "write_files", "runcmd"} <= doc.keys()
    env = written_file(doc, ENV_PATH)
    assert env["owner"] == "root:root" and env["permissions"] == "0600"
    stub = written_file(doc, FIRST_BOOT)
    assert stub["permissions"] == "0755"
    assert stub["content"].startswith("#!/bin/sh\n")
    assert doc["runcmd"] == [[FIRST_BOOT]]


def test_hetzner_env_block_holds_exactly_the_manifest_variables():
    doc = load_cloud_config(HETZNER / "cloud-init.yaml")
    assert env_keys(written_file(doc, ENV_PATH)["content"]) == set(variables("hetzner"))
    required = {name for name, v in variables("hetzner").items() if v.get("required", True)}
    assert required == {"ADMIN_SSH_KEY", "AGENTOS_GIT_URL"}


def test_first_boot_reads_only_variables_the_env_file_defines():
    doc = load_cloud_config(HETZNER / "cloud-init.yaml")
    stub = written_file(doc, FIRST_BOOT)["content"]
    read = set(re.findall(r"\b(?:envval|env_get) ([A-Z_]+)", (COMMON / "bootstrap.sh").read_text() + stub))
    assert read, "the bootstrap reads no variables"
    assert read <= env_keys(written_file(doc, ENV_PATH)["content"])


def test_shell_scripts_parse_and_are_executable():
    stub = written_file(load_cloud_config(HETZNER / "cloud-init.yaml"), FIRST_BOOT)["content"]
    subprocess.run(["sh", "-n", "-c", stub], check=True)
    for script in (COMMON / "bootstrap.sh", HETZNER / "create.sh"):
        subprocess.run(["bash", "-n", str(script)], check=True)
        assert os.access(script, os.X_OK), f"{script} must be executable"


@pytest.mark.parametrize("user_data", [HETZNER / "cloud-init.yaml", TEMPLATES / "home-box" / "autoinstall.yaml"],
                         ids=["hetzner", "home-box"])
def test_user_data_passes_cloud_init_schema(user_data):
    if shutil.which("cloud-init") is None:
        pytest.skip("cloud-init is not installed here")
    subprocess.run(["cloud-init", "schema", "--config-file", str(user_data)], check=True, capture_output=True)


# ---- create.sh: refusals, the hcloud call, and no token in any output ----

CREATE = HETZNER / "create.sh"
OPTIONS = {"--name": "node-1", "--type": "TYPE-X", "--location": "LOC-X", "--image": "IMAGE-X",
           "--ssh-key": "my-key"}


@pytest.fixture
def fake_hcloud(tmp_path):
    """An hcloud on PATH that records its arguments and exits with FAKE_HCLOUD_EXIT (default 0)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    script = bin_dir / "hcloud"
    script.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$FAKE_HCLOUD_LOG"\necho "Server created (fake)"\n'
                      'exit "${FAKE_HCLOUD_EXIT:-0}"\n')
    script.chmod(0o755)
    return bin_dir, log_dir / "args.txt"


@pytest.fixture
def user_data(tmp_path):
    """The template with a fake admin key and a sentinel join token, private, outside any checkout."""
    text = (HETZNER / "cloud-init.yaml").read_text()
    text = text.replace("ADMIN_SSH_KEY=CHANGE-ME", f"ADMIN_SSH_KEY={FAKE_KEY}", 1)
    text = text.replace("JOIN_TOKEN=\n", f"JOIN_TOKEN={SENTINEL_TOKEN}\n", 1)
    text = text.replace("AGENTOS_GIT_URL=CHANGE-ME", "AGENTOS_GIT_URL=https://git.example.com/agentos.git", 1)
    path = tmp_path / "my-node.yaml"
    path.write_text(text)
    path.chmod(0o600)
    return path


def run_create(args: list[str], *, cwd: Path, fake_bin: Path, log: Path, exit_code: int = 0):
    env = {k: v for k, v in os.environ.items() if k != "HCLOUD_TOKEN"}
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["FAKE_HCLOUD_LOG"] = str(log)
    env["FAKE_HCLOUD_EXIT"] = str(exit_code)
    return subprocess.run(["bash", str(CREATE), *args], check=False, cwd=cwd, env=env, capture_output=True,
                          text=True)


def options(user_data: Path, **overrides: str) -> list[str]:
    values = {**OPTIONS, "--user-data": str(user_data)}
    values.update({f"--{k.replace('_', '-')}": v for k, v in overrides.items()})
    return [part for pair in values.items() for part in pair]


def test_create_refuses_to_run_without_arguments(tmp_path, fake_hcloud):
    bin_dir, log = fake_hcloud
    r = run_create([], cwd=tmp_path, fake_bin=bin_dir, log=log)
    assert r.returncode == 2 and "usage:" in r.stderr
    assert not log.exists(), "hcloud must not be called"


@pytest.mark.parametrize("missing", [*OPTIONS, "--user-data"])
def test_create_requires_every_option(missing, tmp_path, user_data, fake_hcloud):
    bin_dir, log = fake_hcloud
    args = options(user_data)
    i = args.index(missing)
    del args[i:i + 2]
    r = run_create(args, cwd=tmp_path, fake_bin=bin_dir, log=log)
    assert r.returncode == 2 and missing in r.stderr
    assert not log.exists()


def test_create_refuses_a_bad_name(tmp_path, user_data, fake_hcloud):
    bin_dir, log = fake_hcloud
    r = run_create(options(user_data, name="Bad_Name"), cwd=tmp_path, fake_bin=bin_dir, log=log)
    assert r.returncode == 1 and "--name must be a hostname" in r.stderr
    assert not log.exists()


def _user_data_with(tmp_path: Path, text: str, mode: int) -> Path:
    path = tmp_path / "bad.yaml"
    path.write_text(text)
    path.chmod(mode)
    return path


@pytest.mark.parametrize(("label", "build", "mode", "expect"), [
    ("unfilled", lambda t: (HETZNER / "cloud-init.yaml").read_text(), 0o600, "CHANGE-ME"),
    ("world-readable", lambda t: t.replace("ADMIN_SSH_KEY=CHANGE-ME", f"ADMIN_SSH_KEY={FAKE_KEY}"), 0o644,
     "readable by other users"),
    ("not cloud-config", lambda t: "#!/bin/sh\necho hi\n", 0o600, "#cloud-config"),
    ("empty admin key", lambda t: t.replace("ADMIN_SSH_KEY=CHANGE-ME", "ADMIN_SSH_KEY="), 0o600,
     "ADMIN_SSH_KEY is empty"),
])
def test_create_refuses_user_data_it_cannot_trust(label, build, mode, expect, tmp_path, fake_hcloud):
    bin_dir, log = fake_hcloud
    base = f"#cloud-config\nADMIN_SSH_KEY=CHANGE-ME\nJOIN_TOKEN={SENTINEL_TOKEN}\n"
    path = _user_data_with(tmp_path, build(base), mode)
    r = run_create(options(path), cwd=tmp_path, fake_bin=bin_dir, log=log)
    assert r.returncode == 1, label
    assert expect in r.stderr, label
    assert SENTINEL_TOKEN not in r.stdout + r.stderr
    assert not log.exists(), label


def test_create_hands_the_file_to_hcloud_and_never_prints_the_token(tmp_path, user_data, fake_hcloud):
    bin_dir, log = fake_hcloud
    before = sorted(p.name for p in tmp_path.iterdir())
    r = run_create(options(user_data), cwd=tmp_path, fake_bin=bin_dir, log=log)
    assert r.returncode == 0, r.stderr
    args = log.read_text().splitlines()
    assert args[:2] == ["server", "create"]
    assert args[args.index("--user-data-from-file") + 1] == str(user_data)
    assert args[args.index("--ssh-key") + 1] == "my-key"
    assert args[args.index("--type") + 1] == "TYPE-X"
    assert SENTINEL_TOKEN not in r.stdout + r.stderr
    assert "Created node-1" in r.stdout
    assert sorted(p.name for p in tmp_path.iterdir()) == before, "create.sh must not write files"


def test_create_passes_hcloud_failure_through(tmp_path, user_data, fake_hcloud):
    bin_dir, log = fake_hcloud
    r = run_create(options(user_data), cwd=tmp_path, fake_bin=bin_dir, log=log, exit_code=3)
    assert r.returncode == 3
    assert "Created" not in r.stdout
    assert SENTINEL_TOKEN not in r.stdout + r.stderr


# ---- home box: the autoinstall file the USB seed carries ----

HOME_BOX = TEMPLATES / "home-box"
IDENTITY_VARIABLES = {"HOSTNAME", "PASSWORD_HASH"}


def autoinstall_of(path: Path) -> dict:
    return load_cloud_config(path)["autoinstall"]


def env_value(text: str, key: str) -> str:
    (value,) = [line.split("=", 1)[1] for line in text.splitlines() if line.startswith(f"{key}=")]
    return value


def test_home_box_autoinstall_has_the_keys_the_installer_needs():
    auto = autoinstall_of(HOME_BOX / "autoinstall.yaml")
    assert auto["version"] == 1
    assert {"hostname", "username", "password"} <= auto["identity"].keys()
    assert auto["ssh"]["install-server"] is True and auto["ssh"]["allow-pw"] is False
    assert auto["ssh"]["authorized-keys"]
    assert auto["storage"]["layout"]["name"] in {"direct", "lvm"}
    user_data = auto["user-data"]
    assert {"packages", "write_files", "runcmd"} <= user_data.keys()
    assert user_data["runcmd"] == [[FIRST_BOOT]]


def test_home_box_first_boot_is_the_same_as_the_hetzner_one():
    hetzner = load_cloud_config(HETZNER / "cloud-init.yaml")
    home = autoinstall_of(HOME_BOX / "autoinstall.yaml")["user-data"]
    for path in (FIRST_BOOT, ENV_PATH):
        assert written_file(home, path)["content"] == written_file(hetzner, path)["content"], path


def test_home_box_variables_match_the_manifest():
    auto = autoinstall_of(HOME_BOX / "autoinstall.yaml")
    env = written_file(auto["user-data"], ENV_PATH)["content"]
    assert env_keys(env) | IDENTITY_VARIABLES == set(variables("home-box"))
    assert env_value(env, "ADMIN_USER") == auto["identity"]["username"]


def test_home_box_admin_key_placeholder_is_in_both_places():
    auto = autoinstall_of(HOME_BOX / "autoinstall.yaml")
    env = written_file(auto["user-data"], ENV_PATH)["content"]
    assert env_value(env, "ADMIN_SSH_KEY") == auto["ssh"]["authorized-keys"][0] == "CHANGE-ME"


# ---- the templates carry no em dash (the UI rule applies to anything a person reads) ----

def test_templates_and_their_docs_have_no_em_dash():
    paths = [p for p in TEMPLATES.rglob("*") if p.is_file()]
    paths.append(ROOT / "docs" / "NODE-TEMPLATES.md")
    offenders = [str(p) for p in paths if p.exists() and "—" in p.read_text()]
    assert offenders == []


# ---- mac mini: the controller as a LaunchAgent, its renderer, and the scripts ----

MAC = TEMPLATES / "mac-mini"
PLIST = MAC / "dev.agentos.controller.plist"
RENDER = MAC / "render_plist.py"
SETUP = MAC / "setup.sh"
POWER = MAC / "power.sh"


def test_the_launch_agent_template_is_a_plist_with_the_placeholders():
    data = plistlib.loads(PLIST.read_bytes())
    assert data["Label"] == "dev.agentos.controller"
    assert data["ProgramArguments"] == ["__AGENTOS_BIN__", "controller", "serve"]
    assert data["RunAtLoad"] is True and data["KeepAlive"] is True
    assert data["EnvironmentVariables"]["AGENTOS_HOME"] == "__AGENTOS_HOME__"
    assert data["StandardErrorPath"] == "__LOG_DIR__/controller.log"


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil is macOS only")
def test_plutil_accepts_the_launch_agent_template():
    subprocess.run(["plutil", "-lint", str(PLIST)], check=True, capture_output=True)


def test_render_fills_the_paths_and_keeps_special_characters(tmp_path):
    out = tmp_path / "rendered.plist"
    binary = "/Users/me/agentos & co/.venv/bin/agentos"
    home, logs = "/Users/me/.agentos", "/Users/me/Library/Logs/agentos"
    r = subprocess.run([sys.executable, str(RENDER), str(PLIST), str(out), binary, home, logs],
                       check=False, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    data = plistlib.loads(out.read_bytes())
    assert data["ProgramArguments"][0] == binary
    assert data["EnvironmentVariables"]["AGENTOS_HOME"] == home
    assert data["WorkingDirectory"] == home
    assert data["StandardOutPath"] == f"{logs}/controller.log"
    assert "__AGENTOS" not in out.read_text() and "__LOG_DIR__" not in out.read_text()


def test_render_refuses_the_wrong_number_of_arguments(tmp_path):
    r = subprocess.run([sys.executable, str(RENDER), str(PLIST)], check=False, capture_output=True, text=True)
    assert r.returncode == 2 and "usage:" in r.stderr


def test_brewfile_lines_are_bundle_entries():
    lines = [line for line in (MAC / "Brewfile").read_text().splitlines() if line.strip() and not line.startswith("#")]
    assert 'brew "uv"' in lines
    for line in lines:
        assert re.fullmatch(r'(brew|cask|tap) "[^"]+"', line), line


def test_mac_scripts_parse_and_are_executable():
    for script in (SETUP, POWER):
        subprocess.run(["bash", "-n", str(script)], check=True)
        assert os.access(script, os.X_OK), f"{script} must be executable"


def test_mac_scripts_refuse_to_run_off_macos(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uname = fake_bin / "uname"
    uname.write_text("#!/bin/sh\necho Linux\n")
    uname.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}
    for script, message in ((SETUP, "macOS"), (POWER, "macOS")):
        r = subprocess.run(["bash", str(script)], check=False, capture_output=True, text=True, env=env)
        assert r.returncode != 0 and message in r.stderr, script


def test_setup_rejects_unknown_arguments():
    r = subprocess.run(["bash", str(SETUP), "--bogus"], check=False, capture_output=True, text=True)
    assert r.returncode == 2 and "usage: setup.sh" in r.stderr


# ---- the bootstrap refuses bad variables before it changes anything ----

BOOTSTRAP = COMMON / "bootstrap.sh"


def run_bootstrap(tmp_path: Path, env_text: str, *, uid: int = 0) -> tuple[subprocess.CompletedProcess, Path]:
    """Run a copy of bootstrap.sh in a temp checkout, with its env file in the temp dir and fake
    root tools on PATH that only log the calls. Refusals must exit before any of them is called."""
    tree = tmp_path / "checkout"
    (tree / "deploy" / "templates" / "common").mkdir(parents=True)
    (tree / "deploy" / "install-node.sh").write_text("#!/bin/sh\necho installer must not run >&2\nexit 9\n")
    script = tree / "deploy" / "templates" / "common" / "bootstrap.sh"
    env_file = tmp_path / "bootstrap.env"
    env_file.write_text(env_text)
    script.write_text(BOOTSTRAP.read_text().replace("/etc/agentos/bootstrap.env", str(env_file)))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    changes = tmp_path / "changes.log"
    (bin_dir / "id").write_text(f'#!/bin/sh\nif [ "$1" = -u ] && [ $# -eq 1 ]; then echo {uid}; fi\n')
    for tool in ("useradd", "passwd", "visudo", "apt-get", "systemctl", "sshd"):
        (bin_dir / tool).write_text(f'#!/bin/sh\necho "{tool} $*" >> "{changes}"\n')
    for path in bin_dir.iterdir():
        path.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    r = subprocess.run(["bash", str(script)], check=False, capture_output=True, text=True, env=env)
    return r, changes


@pytest.mark.parametrize(("env_text", "uid", "message"), [
    ("ADMIN_SSH_KEY=\nNODE_NAME=node-1\n", 0, "ADMIN_SSH_KEY is empty"),
    ("ADMIN_SSH_KEY=not a public key at all\nNODE_NAME=node-1\n", 0, "is not a public key"),
    (f"ADMIN_SSH_KEY={FAKE_KEY}\nNODE_NAME=node-1\nADMIN_USER=Root!\n", 0, "ADMIN_USER must be a plain user name"),
    (f"ADMIN_SSH_KEY={FAKE_KEY}\nNODE_NAME=Bad_Name\n", 0, "NODE_NAME must be lowercase"),
    (f"ADMIN_SSH_KEY={FAKE_KEY}\nNODE_NAME=node-1\n", 1000, "run as root"),
])
def test_bootstrap_refuses_before_changing_anything(env_text, uid, message, tmp_path):
    r, changes = run_bootstrap(tmp_path, env_text, uid=uid)
    assert r.returncode == 1, r.stdout + r.stderr
    assert message in r.stderr
    assert not changes.exists(), f"a root tool ran before the refusal: {changes.read_text()}"
