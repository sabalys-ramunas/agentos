"""Installer and helper scripts (deploy/, scripts/) in a sandbox: no root, no apt, no network.

The installer runs with --dev --skip-apt --skip-browser. Its uv, chmod and realpath are PATH shims
(edge_shell.py), ssh is logged rather than run, and rsync is logged for sync-node.sh. The installer
uses real rsync to copy the checkout into a temp prefix, and runs the real agentos CLI for
`node init` unless the test sets cli="log". Nothing outside the test's temp dir is written.
"""

import os
import re
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
from edge_shell import (
    BASH,
    INSTALLER,
    NODE_TEST,
    REPO,
    SYNC,
    has_rsync,
    installer,
    run_script,
    sandbox_env,
    shim_calls,
    shim_text,
)

INSTALL_SHIMS = ("realpath", "uv", "chmod")
NOT_ROOT = not (hasattr(os, "geteuid") and os.geteuid() == 0)
WIPE_BUG = "BUG-1: a trailing slash on AGENTOS_HOME or INSTALL_PREFIX makes rsync --delete wipe node data"


def fresh_prefix(root: Path) -> Path:
    """A prefix that already holds an agentos checkout, so the installer treats it as its own."""
    prefix = root / "prefix"
    prefix.mkdir(parents=True)
    shutil.copy(REPO / "pyproject.toml", prefix / "pyproject.toml")
    return prefix


def seed_node_data(home: Path) -> Path:
    profile = home / "lanes" / "personal" / "reddit" / "profile"
    profile.mkdir(parents=True)
    (home / "node.toml").write_text('mcp_token = "KEEP-ME"\n')
    (profile / "cookies.sqlite").write_text("logged in")
    return home


# ---- install-node.sh: arguments and guards ------------------------------------------


def test_help_lists_the_options(tmp_path):
    result = run_script(INSTALLER, ["--help"], sandbox_env(tmp_path, shims=INSTALL_SHIMS), tmp_path)
    assert result.returncode == 0 and "--skip-browser" in result.stdout


def test_unknown_option_is_a_usage_error(tmp_path):
    result = run_script(INSTALLER, ["--yolo"], sandbox_env(tmp_path, shims=INSTALL_SHIMS), tmp_path)
    assert result.returncode == 2 and "unknown option" in result.stderr


@pytest.mark.parametrize("prefix", ["/", "/root", "/home", "/usr", "/opt", "/var", "/etc"])
def test_system_prefixes_are_refused(tmp_path, prefix):
    env = sandbox_env(tmp_path, shims=INSTALL_SHIMS)
    result = installer(env, tmp_path, prefix=prefix, home=str(tmp_path / "home-data"))
    assert result.returncode == 1 and f"refusing INSTALL_PREFIX={prefix}" in result.stderr
    assert shim_calls(env) == []


@pytest.mark.parametrize("home", ["/", "/root", "/home", "/usr", "/opt", "/var", "/etc"])
def test_system_homes_without_a_trailing_slash_are_refused(tmp_path, home):
    env = sandbox_env(tmp_path, shims=INSTALL_SHIMS)
    result = installer(env, tmp_path, prefix=str(tmp_path / "prefix"), home=home)
    assert result.returncode == 1 and f"refusing AGENTOS_HOME={home}" in result.stderr
    assert shim_calls(env) == []


def test_system_home_with_a_trailing_slash_is_refused(tmp_path):
    env = sandbox_env(tmp_path, shims=INSTALL_SHIMS, cli="log")
    result = installer(env, tmp_path, prefix=str(tmp_path / "prefix"), home="/etc/")
    assert result.returncode != 0 and "refusing AGENTOS_HOME" in result.stderr
    assert not [c for c in shim_calls(env) if c.startswith("chmod") and "/etc" in c]


def test_prefix_inside_the_checkout_is_refused(tmp_path):
    env = sandbox_env(tmp_path, shims=INSTALL_SHIMS)
    result = installer(env, tmp_path, prefix=str(REPO / "not-created-by-tests"), home=str(tmp_path / "home"))
    assert result.returncode == 1 and "inside the checkout" in result.stderr
    assert not (REPO / "not-created-by-tests").exists()


def test_prefix_holding_foreign_files_is_left_alone(tmp_path):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    (prefix / "notes.txt").write_text("mine")
    env = sandbox_env(tmp_path, shims=INSTALL_SHIMS)
    result = installer(env, tmp_path, prefix=str(prefix), home=str(tmp_path / "home"))
    assert result.returncode == 1 and "not an agentos install" in result.stderr
    assert (prefix / "notes.txt").read_text() == "mine"


@pytest.mark.skipif(not has_rsync(), reason="needs rsync")
def test_dev_install_works_with_spaces_in_both_paths(tmp_path):
    env = sandbox_env(tmp_path, shims=INSTALL_SHIMS, cli="run")
    prefix, home = tmp_path / "my agentos", tmp_path / "my home"
    result = installer(env, tmp_path, prefix=str(prefix), home=str(home))
    assert result.returncode == 0, result.stderr
    assert (prefix / ".venv" / "bin" / "agentos").exists()
    assert "mcp_token" in tomllib.loads((home / "node.toml").read_text())


@pytest.mark.skipif(not has_rsync(), reason="needs rsync")
def test_rerun_keeps_the_token_and_the_node_data(tmp_path):
    env = sandbox_env(tmp_path, shims=INSTALL_SHIMS, cli="run")
    prefix = tmp_path / "prefix"
    home = prefix / ".agentos-home"
    assert installer(env, tmp_path, prefix=str(prefix), home=str(home)).returncode == 0
    token = tomllib.loads((home / "node.toml").read_text())["mcp_token"]

    again = installer(env, tmp_path, prefix=str(prefix), home=str(home))
    assert again.returncode == 0, again.stderr
    assert "keeping its token" in again.stdout
    assert tomllib.loads((home / "node.toml").read_text())["mcp_token"] == token


# ---- install-node.sh: re-runs must never delete node data ----------------------------


@pytest.mark.skipif(not has_rsync(), reason="needs rsync")
@pytest.mark.parametrize(
    ("prefix_slash", "home_slash"),
    [
        pytest.param("", "", id="no-trailing-slash"),
        pytest.param("", "/", id="agentos-home-trailing-slash"),
        pytest.param("/", "", id="install-prefix-trailing-slash"),
    ],
)
def test_rerun_keeps_node_data_that_lives_inside_the_prefix(tmp_path, prefix_slash, home_slash):
    env = sandbox_env(tmp_path, shims=INSTALL_SHIMS, cli="log")
    prefix = fresh_prefix(tmp_path)
    home = seed_node_data(prefix / ".agentos-home")
    result = installer(env, tmp_path, prefix=f"{prefix}{prefix_slash}", home=f"{home}{home_slash}")
    assert result.returncode == 0, result.stderr
    assert (home / "node.toml").is_file() and "KEEP-ME" in (home / "node.toml").read_text()
    assert (home / "lanes" / "personal" / "reddit" / "profile" / "cookies.sqlite").exists()


# ---- install-node.sh: arguments with spaces and globs, and the systemd path ----------


@pytest.mark.skipif(not has_rsync(), reason="needs rsync")
def test_init_args_keep_a_quoted_value_together(tmp_path):
    env = sandbox_env(tmp_path, shims=INSTALL_SHIMS, cli="run")
    home = tmp_path / "home"
    result = run_script(INSTALLER, ["--dev", "--skip-apt", "--skip-browser"], env, tmp_path,
                        INSTALL_PREFIX=str(tmp_path / "prefix"), AGENTOS_HOME=str(home),
                        AGENTOS_INIT_ARGS="--name 'my node'")
    assert result.returncode == 0, result.stderr
    assert tomllib.loads((home / "node.toml").read_text())["name"] == "my node"


@pytest.mark.skipif(not has_rsync(), reason="needs rsync")
def test_init_args_do_not_glob_expand(tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "a").write_text("")
    (cwd / "b").write_text("")
    env = sandbox_env(tmp_path, shims=INSTALL_SHIMS, cli="run")
    home = tmp_path / "home"
    result = run_script(INSTALLER, ["--dev", "--skip-apt", "--skip-browser"], env, cwd,
                        INSTALL_PREFIX=str(tmp_path / "prefix"), AGENTOS_HOME=str(home),
                        AGENTOS_INIT_ARGS="--ssh-host *")
    assert result.returncode == 0, result.stderr
    assert tomllib.loads((home / "node.toml").read_text())["ssh_host"] == "*"


@pytest.mark.skipif(not NOT_ROOT, reason="needs a non-root user, which is the case being tested")
def test_systemd_without_root_refuses_before_touching_node_data(tmp_path):
    env = sandbox_env(tmp_path, shims=INSTALL_SHIMS, cli="log")
    result = installer(env, tmp_path, prefix=str(tmp_path / "prefix"), home=str(tmp_path / "home"),
                       flags=("--systemd", "--skip-apt", "--skip-browser"))
    assert result.returncode != 0 and "need root" in result.stderr
    assert not any(call.startswith("agentos node init") for call in shim_calls(env))


def test_unit_file_keeps_a_prefix_with_spaces_in_one_argument():
    template = (REPO / "deploy" / "agentosd.service").read_text()
    # The same substitution as deploy/install-node.sh (its sed on the unit file, around line 158).
    rendered = template.replace("/opt/agentos", "/root/my agentos")
    exec_start = next(line for line in rendered.splitlines() if line.startswith("ExecStart="))
    assert shlex.split(exec_start.removeprefix("ExecStart="))[0] == "/root/my agentos/.venv/bin/agentos"


def test_unit_keeps_the_graceful_stop_contract():
    lines = (REPO / "deploy" / "agentosd.service").read_text().splitlines()
    for setting in ("Type=simple", "Restart=on-failure", "KillMode=mixed", "TimeoutStopSec=30", "UMask=0077"):
        assert setting in lines, setting


def test_the_runas_unit_renders_its_users_and_prefix():
    """install-node.sh's render_runas_unit: the same sed, run on the real template."""
    sed = ["sed", "-e", "s#/opt/agentos#/srv/ag#g", "-e", "s#--caller agentos #--caller svc #g",
           "-e", "s#--brain-user agentos-brain$#--brain-user brainy#", str(REPO / "deploy" / "agentos-runas.service")]
    rendered = subprocess.run(sed, capture_output=True, text=True, check=True).stdout
    execs = [line for line in rendered.splitlines() if line.startswith(("ExecStart=", "ExecStartPre=", "ExecStopPost="))]
    assert len(execs) == 3
    for line in execs:
        argv = shlex.split(line.split("=", 1)[1].removeprefix("-"))
        # a script, without site: nothing of site-packages (or a .pth file) runs as root
        assert argv[:5] == ["/srv/ag/.venv/bin/python", "-I", "-S", "-B", "/srv/ag/src/agentos/runas.py"]
        assert argv[-4:] == ["--caller", "svc", "--brain-user", "brainy"]
    assert "agentos-brain" not in "".join(execs)
    assert not any(line.startswith("User=") for line in rendered.splitlines())  # root; see the unit
    for setting in ("NoNewPrivileges=yes", "RestrictSUIDSGID=yes", "RuntimeDirectory=agentos-runas"):
        assert setting in rendered.splitlines(), setting
    assert "agentos-runas.service" in (REPO / "deploy" / "agentosd-hardened.service").read_text()


# ---- scripts/sync-node.sh --------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    ["/root/agentos", "/root/agentos/poc", "/srv/agentos-dev", "/srv/agentos-dev/",
     "/srv/agentos-devil/x", "/home/x", "relative/dir"],
)
def test_sync_refuses_targets_outside_agentos_dev(tmp_path, target):
    env = sandbox_env(tmp_path, shims=("rsync", "ssh"))
    result = run_script(SYNC, [target], env, tmp_path)
    assert result.returncode == 2
    assert not any(call.startswith("rsync") for call in shim_calls(env))


def test_sync_copies_into_a_directory_under_agentos_dev(tmp_path):
    env = sandbox_env(tmp_path, shims=("rsync", "ssh"))
    result = run_script(SYNC, ["/srv/agentos-dev/qa"], env, tmp_path)
    assert result.returncode == 0, result.stderr
    [call] = [c for c in shim_calls(env) if c.startswith("rsync")]
    assert "--delete" in call and "mynode:/srv/agentos-dev/qa/" in call


@pytest.mark.parametrize("script", [SYNC, NODE_TEST])
def test_node_scripts_say_which_setting_is_missing(tmp_path, script):
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    for name in (script.name, "node-env.sh", "sync-node.sh"):
        (tree / "scripts" / name).write_text((SYNC.parent / name).read_text())
        (tree / "scripts" / name).chmod(0o755)
    (tree / "deploy").mkdir()
    shutil.copy(REPO / "deploy" / "web-build.sh", tree / "deploy" / "web-build.sh")  # sync-node.sh sources it
    env = sandbox_env(tmp_path, shims=("rsync", "ssh"))
    for var in ("AGENTOS_NODE", "AGENTOS_DEV_ROOT", "NODE_HOST"):
        env.pop(var, None)
    args = ["qa"] if script == NODE_TEST else ["/srv/agentos-dev/qa"]
    result = run_script(tree / "scripts" / script.name, args, env, tmp_path)
    assert result.returncode == 2 and ".agentos-node.env" in result.stderr, result.stderr
    assert shim_calls(env) == []
    # one line in the gitignored file sets the host and the dev root
    (tree / ".agentos-node.env").write_text("AGENTOS_NODE=boxy:/srv/agentos-dev\n")
    result = run_script(tree / "scripts" / script.name, args, env, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "boxy:/srv/agentos-dev/qa/" in shim_text(env)


@pytest.mark.parametrize(
    "target", ["/srv/agentos-dev/../agentos", "/srv/agentos-dev/qa/../lanes", "/srv/agentos-dev/."],
)
def test_sync_refuses_dot_segments_that_escape_agentos_dev(tmp_path, target):
    env = sandbox_env(tmp_path, shims=("rsync", "ssh"))
    result = run_script(SYNC, [target], env, tmp_path)
    assert result.returncode == 2
    assert not any(call.startswith("rsync") for call in shim_calls(env))


def _watch_tree(tmp_path, changed: str):
    """A copy of sync-node.sh in a tree of its own (the watch loop scans the checkout), with an
    rsync shim that reports `changed` as the files it copied."""
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    (tree / "deploy").mkdir()
    script = tree / "scripts" / "sync-node.sh"
    script.write_text(SYNC.read_text())
    (tree / "scripts" / "node-env.sh").write_text((SYNC.parent / "node-env.sh").read_text())
    shutil.copy(WEB_BUILD, tree / "deploy" / "web-build.sh")  # sync-node.sh sources it
    env = sandbox_env(tmp_path, shims=("rsync", "ssh"))
    shim = Path(env["PATH"].split(os.pathsep)[0]) / "rsync"
    shim.write_text('#!/bin/bash\necho "rsync $*" >> "$SHIM_LOG"\nprintf \'%s\\n\' ' + changed + "\n")
    return script, env


@pytest.mark.parametrize("args", [
    ["--restart", "x", "/srv/agentos-dev/qa"],             # restart without --watch
    ["--panel", "/srv/agentos-dev/qa"],
    ["--watch", "/srv/agentos-dev/qa", "/srv/agentos-dev/other"],
    ["--watch"],
    ["--bogus", "/srv/agentos-dev/qa"],
])
def test_sync_watch_options_are_checked_before_anything_runs(tmp_path, args):
    env = sandbox_env(tmp_path, shims=("rsync", "ssh"))
    result = run_script(SYNC, args, env, tmp_path)
    assert result.returncode == 2, result.stdout
    assert shim_calls(env) == []


@pytest.mark.parametrize("cmd", [
    "systemctl restart agentosd", "systemctl restart agentosd.service", "pkill -f agentosd",
    "rm -rf /var/lib/agentos/lanes", "cd /root/agentos && ls", "sudo systemctl stop agentosd;true",
])
def test_sync_watch_refuses_restarts_that_touch_production_or_the_poc(tmp_path, cmd):
    env = sandbox_env(tmp_path, shims=("rsync", "ssh"))
    result = run_script(SYNC, ["--watch", "/srv/agentos-dev/qa", "--restart", cmd], env, tmp_path)
    assert result.returncode == 2 and "refusing a restart command" in result.stderr
    assert shim_calls(env) == []


def test_sync_watch_restarts_only_when_code_changed(tmp_path):
    script, env = _watch_tree(tmp_path, "'src/agentos/x.py' 'uv.lock'")
    result = run_script(script, ["--watch", "/srv/agentos-dev/qa", "--restart", "systemctl restart agentosd-qa"],
                        env, tmp_path, SYNC_WATCH_ROUNDS="1")
    assert result.returncode == 0, result.stderr
    log = shim_text(env)
    assert "ControlPath=/tmp/agentos-sync-%C" in log and "mynode:/srv/agentos-dev/qa/" in log
    assert "cd '/srv/agentos-dev/qa' && uv sync --frozen --extra node" in log
    assert "cd '/srv/agentos-dev/qa' && systemctl restart agentosd-qa" in log
    assert "synced 2 change(s)" in result.stdout

    script, env = _watch_tree(tmp_path / "docs-only", "'docs/ARCHITECTURE.md' 'tests/unit/test_x.py'")
    result = run_script(script, ["--watch", "/srv/agentos-dev/qa", "--restart", "systemctl restart agentosd-qa"],
                        env, tmp_path, SYNC_WATCH_ROUNDS="1")
    assert result.returncode == 0, result.stderr
    assert "systemctl restart" not in shim_text(env) and "uv sync" not in shim_text(env)


@pytest.mark.skipif(not has_rsync(), reason="needs rsync")
def test_installer_builds_the_web_app_once_and_keeps_its_build_on_a_rerun(tmp_path):
    """A rerun copies the checkout over the prefix. It must keep the prefix's own web/dist and its stamp, so an
    unchanged web app is not built again, and a build that failed earlier never replaces the last good one."""
    env = sandbox_env(tmp_path, shims=INSTALL_SHIMS + ("npx",), cli="log")
    prefix = fresh_prefix(tmp_path)
    home = tmp_path / "home"
    first = installer(env, tmp_path, prefix=str(prefix), home=str(home))
    assert first.returncode == 0, first.stderr
    builds = [c for c in shim_calls(env) if c.startswith("npx") and c.endswith(" build")]
    assert builds == ["npx --yes pnpm@10.33.0 build"]
    built = prefix / "web" / "dist"
    assert (built / "index.html").is_file() and (built / ".agentos-build").is_file()

    second = installer(env, tmp_path, prefix=str(prefix), home=str(home))
    assert second.returncode == 0, second.stderr
    assert [c for c in shim_calls(env) if c.startswith("npx") and c.endswith(" build")] == builds  # not built again
    assert (built / ".agentos-build").is_file()


# ---- the web build: rebuilt when its sources changed, the last good build kept on failure ----

WEB_BUILD = REPO / "deploy" / "web-build.sh"
def _web_tree(root: Path) -> Path:
    web = root / "web"
    (web / "src").mkdir(parents=True)
    (web / "node_modules").mkdir()
    (web / "package.json").write_text("{}")
    (web / "src" / "version.txt").write_text("one\n")
    return web


def _helper(env, cwd: Path, body: str):
    return subprocess.run([BASH, "-c", f'. "{WEB_BUILD}"\n{body}'], env=env, cwd=cwd, capture_output=True, text=True,
                          timeout=120, check=False)


def test_web_build_rebuilds_when_sources_change_and_keeps_the_last_good_build(tmp_path):
    web = _web_tree(tmp_path)
    env = sandbox_env(tmp_path, shims=())
    ok, bad = tmp_path / "ok.sh", tmp_path / "bad.sh"
    ok.write_text("mkdir -p dist && echo good > dist/index.html\n")
    bad.write_text("mkdir -p dist && echo broken > dist/index.html\nexit 3\n")

    assert _helper(env, tmp_path, f'web_is_stale "{web}"').returncode == 0  # no build yet
    assert _helper(env, tmp_path, f'web_rebuild "{web}" bash "{ok}"').returncode == 0
    assert _helper(env, tmp_path, f'web_is_stale "{web}"').returncode == 1  # built from these sources: current

    (web / "src" / "version.txt").write_text("two\n")
    assert _helper(env, tmp_path, f'web_is_stale "{web}"').returncode == 0  # a source changed: stale
    failed = _helper(env, tmp_path, f'web_rebuild "{web}" bash "{bad}"')
    assert failed.returncode == 1
    assert (web / "dist" / "index.html").read_text() == "good\n"  # the last good build is still served
    assert not (web / "dist.previous").exists()
    assert _helper(env, tmp_path, f'web_is_stale "{web}"').returncode == 0  # still stale: tried again next time
    assert _helper(env, tmp_path, f'web_rebuild "{web}" bash "{ok}"').returncode == 0
    assert _helper(env, tmp_path, f'web_is_stale "{web}"').returncode == 1


def test_web_fingerprint_ignores_dist_and_node_modules(tmp_path):
    web = _web_tree(tmp_path)
    env = sandbox_env(tmp_path, shims=())
    before = _helper(env, tmp_path, f'web_fingerprint "{web}"').stdout
    (web / "node_modules" / "x.js").write_text("noise")
    (web / "dist").mkdir()
    (web / "dist" / "index.html").write_text("built")
    assert _helper(env, tmp_path, f'web_fingerprint "{web}"').stdout == before
    (web / "src" / "new.ts").write_text("export {}")
    assert _helper(env, tmp_path, f'web_fingerprint "{web}"').stdout != before


def _sync_tree(root: Path) -> tuple[Path, Path, dict[str, str]]:
    """A copy of sync-node.sh and the web build helper in a tree of their own, with a web/ to build."""
    tree = root / "tree"
    (tree / "scripts").mkdir(parents=True)
    (tree / "deploy").mkdir()
    shutil.copy(SYNC, tree / "scripts" / "sync-node.sh")
    shutil.copy(SYNC.parent / "node-env.sh", tree / "scripts" / "node-env.sh")  # sync-node.sh sources it
    shutil.copy(WEB_BUILD, tree / "deploy" / "web-build.sh")
    web = _web_tree(tree)
    return tree / "scripts" / "sync-node.sh", web, sandbox_env(root, shims=("rsync", "ssh", "npx"))


def test_sync_web_builds_only_when_the_sources_changed(tmp_path):
    script, web, env = _sync_tree(tmp_path)
    result = run_script(script, ["--web", "/srv/agentos-dev/qa"], env, tmp_path)
    assert result.returncode == 0, result.stderr
    assert [c for c in shim_calls(env) if c.startswith("npx")] == ["npx --yes pnpm@10.33.0 build"]
    assert (web / "dist" / "index.html").read_text() == "built one\n"

    result = run_script(script, ["--web", "/srv/agentos-dev/qa"], env, tmp_path)  # nothing changed: no build
    assert result.returncode == 0, result.stderr
    assert sum(c.startswith("npx") for c in shim_calls(env)) == 1

    (web / "src" / "version.txt").write_text("two\n")
    result = run_script(script, ["--web", "/srv/agentos-dev/qa"], env, tmp_path, SHIM_NPX_FAIL="1")
    assert result.returncode == 0, result.stderr  # a failed build still syncs the rest
    assert "web build failed" in result.stderr
    assert (web / "dist" / "index.html").read_text() == "built one\n"  # the last good build is kept

    result = run_script(script, ["--web", "/srv/agentos-dev/qa"], env, tmp_path)
    assert result.returncode == 0 and (web / "dist" / "index.html").read_text() == "built two\n"


def test_sync_without_web_leaves_the_web_build_alone(tmp_path):
    script, web, env = _sync_tree(tmp_path)
    result = run_script(script, ["/srv/agentos-dev/qa"], env, tmp_path)
    assert result.returncode == 0, result.stderr
    assert not any(c.startswith("npx") for c in shim_calls(env))
    assert not (web / "dist").exists()
    assert "--web rebuilds it" in result.stderr  # and it says so when the build is stale


# ---- scripts/node-test.sh --------------------------------------------------------------


@pytest.mark.parametrize("name", ["Bad Name", "../x", "UPPER"])
def test_node_test_rejects_names_that_are_not_plain_slugs(tmp_path, name):
    env = sandbox_env(tmp_path, shims=("rsync", "ssh"))
    result = run_script(NODE_TEST, [name], env, tmp_path)
    assert result.returncode == 2 and "name must be" in result.stderr
    assert shim_calls(env) == []


@pytest.mark.parametrize(
    ("name", "display", "viewer", "vnc", "mcp"),
    [
        ("lanes", "110", "6110", "5110", "7410"),
        ("interaction", "120", "6120", "5120", "7420"),
        ("mcp", "130", "6130", "5130", "7430"),
    ],
)
def test_named_agents_get_their_documented_ranges(tmp_path, name, display, viewer, vnc, mcp):
    env = sandbox_env(tmp_path, shims=("rsync", "ssh"))
    result = run_script(NODE_TEST, [name], env, tmp_path)
    assert result.returncode == 0, result.stderr
    text = shim_text(env)
    assert f"AGENTOS_TEST_DISPLAY_BASE={display}" in text
    assert f"AGENTOS_TEST_VIEWER_BASE={viewer}" in text
    assert f"AGENTOS_TEST_VNC_BASE={vnc}" in text
    assert f"AGENTOS_TEST_MCP_PORT_BASE={mcp}" in text


RESERVED_PORTS = {5900, 6080, 7400, *range(5910, 5920), *range(6100, 6110)}


@pytest.mark.parametrize("slot", ["11", "20", "21", "22", "59"])
def test_custom_slots_never_use_the_poc_or_production_ports(tmp_path, slot):
    """BUG-5: slots 10 and 20 once mapped VNC to 5900 and MCP to 7400, the PoC's ports."""
    env = sandbox_env(tmp_path, shims=("rsync", "ssh"))
    result = run_script(NODE_TEST, ["qa"], env, tmp_path, AGENTOS_TEST_SLOT=slot)
    assert result.returncode == 0, result.stderr
    text = shim_text(env)
    for var in ("VIEWER_BASE", "VNC_BASE", "MCP_PORT_BASE"):
        base = int(re.search(rf"AGENTOS_TEST_{var}=(\d+)", text).group(1))
        assert not RESERVED_PORTS & set(range(base, base + 10)), (var, base)


@pytest.mark.parametrize("slot", ["10", "60", "9", "1x", "011"])
def test_slots_that_would_reach_reserved_ports_are_refused(tmp_path, slot):
    env = sandbox_env(tmp_path, shims=("rsync", "ssh"))
    result = run_script(NODE_TEST, ["qa"], env, tmp_path, AGENTOS_TEST_SLOT=slot)
    assert result.returncode == 2 and "AGENTOS_TEST_SLOT" in result.stderr
    assert not any(call.startswith("rsync") for call in shim_calls(env))
