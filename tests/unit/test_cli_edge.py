"""`openajar` CLI edge cases: proxy specs, lane ids, node init options, corrupt state.

Passing tests pin edge cases that already work. `xfail(strict=True)` tests demonstrate a bug.
"""

import json
import stat

import pytest
from fakes import FakeLaneManager
from typer.testing import CliRunner

from openajar.cli import app
from openajar.core.config import NodeConfig, load_config, save_config
from openajar.node import daemon

PASSWORD = "Pa55:with:colons"
USERNAME = "brd-customer-zone-isp"


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "agentos-home"
    monkeypatch.setenv("OPENAJAR_HOME", str(home))
    return home


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def inited(home, runner):
    assert runner.invoke(app, ["node", "init"]).exit_code == 0
    return home


@pytest.fixture
def fake(monkeypatch, inited):
    manager = FakeLaneManager(load_config(inited))
    monkeypatch.setattr(daemon, "build_lane_manager", lambda cfg: manager)
    return manager


def stored_proxies(home) -> dict:
    return json.loads((home / "secrets" / "proxies.json").read_text())


def mode(path) -> str:
    return oct(stat.S_IMODE(path.stat().st_mode))[-3:]


# ---- proxy specs ---------------------------------------------------------------------


@pytest.mark.parametrize("spec", ["1.2.3.4:0", "1.2.3.4:-1", "1.2.3.4:65536", ":8080", "1.2.3.4"])
def test_proxy_specs_with_bad_port_or_host_are_refused(inited, runner, spec):
    result = runner.invoke(app, ["proxy", "add", "lt1", spec])
    assert result.exit_code == 2, result.output
    assert not (inited / "secrets" / "proxies.json").exists()


@pytest.mark.parametrize("spec", [f"1.2.3.4:abc:{USERNAME}:{PASSWORD}", f"1.2.3.4:99999:{USERNAME}:{PASSWORD}"])
def test_proxy_errors_never_echo_the_password(inited, runner, spec):
    result = runner.invoke(app, ["proxy", "add", "lt1", spec])
    assert result.exit_code == 2
    assert PASSWORD not in result.output and USERNAME not in result.output


def test_proxy_password_keeps_spaces_and_unicode(inited, runner):
    secret = "  pw žąsis 42 "
    assert runner.invoke(app, ["proxy", "add", "lt1", f"1.2.3.4:80:{USERNAME}:{secret}"]).exit_code == 0
    assert stored_proxies(inited)["lt1"]["password"] == secret


def test_empty_password_field_prompts_for_it(inited, runner):
    result = runner.invoke(app, ["proxy", "add", "lt2", f"1.2.3.4:80:{USERNAME}:"], input="pw-typed\n")
    assert result.exit_code == 0, result.output
    assert stored_proxies(inited)["lt2"]["password"] == "pw-typed"


def test_proxy_label_length_boundary(inited, runner):
    assert runner.invoke(app, ["proxy", "add", "a" * 41, "1.2.3.4:80"]).exit_code == 0
    assert runner.invoke(app, ["proxy", "add", "b" * 42, "1.2.3.4:80"]).exit_code == 2


def test_proxy_add_overwrites_an_existing_label_only_with_a_yes(inited, runner):
    runner.invoke(app, ["proxy", "add", "lt1", "1.2.3.4:80"])
    refused = runner.invoke(app, ["proxy", "add", "lt1", "5.6.7.8:3128"])
    assert refused.exit_code != 0 and "new IP" in refused.output
    assert stored_proxies(inited)["lt1"]["host"] == "1.2.3.4"
    again = runner.invoke(app, ["proxy", "add", "lt1", "5.6.7.8:3128", "--yes"])
    assert again.exit_code == 0 and "Updated proxy lt1: 5.6.7.8:3128" in again.output
    assert stored_proxies(inited)["lt1"]["host"] == "5.6.7.8"


def test_password_without_username_is_refused(inited, runner):
    result = runner.invoke(app, ["proxy", "add", "lt1", "1.2.3.4:8080::secret"])
    assert result.exit_code == 2 and not (inited / "secrets" / "proxies.json").exists()


def test_newline_in_a_proxy_label_is_refused(inited, runner):
    assert runner.invoke(app, ["proxy", "add", "lt1\n", "1.2.3.4:80"]).exit_code == 2


def test_ipv6_proxy_host_gets_a_clear_error(inited, runner):
    result = runner.invoke(app, ["proxy", "add", "lt6", f"[2001:db8::1]:8080:{USERNAME}:{PASSWORD}"])
    assert result.exit_code == 2 and "IPv6" in result.output


# ---- lanes ---------------------------------------------------------------------------


def test_lane_id_length_boundaries(fake, runner):
    ok = f"{'w' * 31}/{'n' * 41}"
    assert runner.invoke(app, ["lane", "create", ok]).exit_code == 0
    assert runner.invoke(app, ["lane", "create", f"{'w' * 32}/x"]).exit_code == 2
    assert runner.invoke(app, ["lane", "create", f"x/{'n' * 42}"]).exit_code == 2


def test_lane_import_rejects_a_file_as_the_profile(fake, runner, tmp_path):
    profile_file = tmp_path / "cookies.sqlite"
    profile_file.write_text("x")
    result = runner.invoke(app, ["lane", "import", "personal/reddit", "--profile", str(profile_file)])
    assert result.exit_code == 2 and fake.lanes == {}


def test_lane_import_refuses_an_existing_lane(fake, runner, tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    assert runner.invoke(app, ["lane", "create", "personal/reddit"]).exit_code == 0
    result = runner.invoke(app, ["lane", "import", "personal/reddit", "--profile", str(profile)])
    assert result.exit_code == 1 and "lane_exists:" in result.output


def test_newline_in_a_lane_id_is_refused(fake, runner):
    assert runner.invoke(app, ["lane", "create", "personal/reddit\n"]).exit_code == 2
    assert "personal/reddit\n" not in fake.lanes


# ---- node init and serve -------------------------------------------------------------


def test_node_init_keeps_the_token_when_other_options_change(inited, runner):
    token = load_config(inited).mcp_token
    result = runner.invoke(app, ["node", "init", "--mcp-port", "7460", "--display-base", "160"])
    assert result.exit_code == 0 and "Updated" in result.output
    cfg = load_config(inited)
    assert cfg.mcp_token == token and cfg.mcp_port == 7460 and cfg.display_base == 160


def test_node_serve_refuses_a_blank_token(home, runner):
    save_config(NodeConfig(home=home))  # no token yet
    result = runner.invoke(app, ["node", "serve"])
    assert result.exit_code == 1 and "no mcp_token" in result.output


@pytest.mark.parametrize("option", [["--mcp-port", "70000"], ["--mcp-port", "0"], ["--max-warm-lanes", "0"]])
def test_node_init_rejects_out_of_range_values(home, runner, option):
    assert runner.invoke(app, ["node", "init", *option]).exit_code != 0


def test_node_init_does_not_change_an_existing_directory(tmp_path, monkeypatch, runner):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "notes.txt").write_text("not ours")
    shared.chmod(0o755)
    monkeypatch.setenv("OPENAJAR_HOME", str(shared))
    assert runner.invoke(app, ["node", "init"]).exit_code == 0
    assert mode(shared) == "755"


def test_corrupt_node_toml_gives_a_clean_error(home, runner):
    home.mkdir(parents=True)
    (home / "node.toml").write_text("mcp_port = [\n")
    result = runner.invoke(app, ["node", "init"])
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit)


def test_corrupt_database_gives_a_clean_error(inited, runner):
    (inited / "openajar.db").write_bytes(b"this is not a database" * 100)
    result = runner.invoke(app, ["lane", "list"])
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit)


def test_invalid_node_toml_error_never_echoes_the_token(home, runner):
    home.mkdir(parents=True)
    (home / "node.toml").write_text('mcp_token = "tok-SECRET-123"\nmax_warm_lanes = 0\n')
    result = runner.invoke(app, ["lane", "list"])
    assert result.exit_code == 1 and "max_warm_lanes" in result.output
    assert "tok-SECRET-123" not in result.output


def test_node_init_refuses_options_that_break_the_limits_without_writing(home, runner):
    assert runner.invoke(app, ["node", "init"]).exit_code == 0
    before = (home / "node.toml").read_text()
    result = runner.invoke(app, ["node", "init", "--max-warm-lanes", "99"])
    assert result.exit_code == 2 and "max_warm_lanes" in result.output
    assert (home / "node.toml").read_text() == before
