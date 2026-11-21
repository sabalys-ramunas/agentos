"""`openajar identity` and the doctor's clock warning for signing lanes."""

from __future__ import annotations

import json
import stat

import pytest
from test_ops_support import FakeHost
from typer.testing import CliRunner

from openajar.cli import app
from openajar.core.config import load_config
from openajar.core.models import LaneSpec
from openajar.node.store import LaneStore
from openajar.ops import doctor
from openajar.ops.doctor import PASS, WARN, DoctorContext
from openajar.webbotauth import keys, settings

LANE = "acme/research"
EM_DASH = "—"


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "agentos-home"
    monkeypatch.setenv("OPENAJAR_HOME", str(home))
    result = CliRunner().invoke(app, ["node", "init"])
    assert result.exit_code == 0, result.output
    cfg = load_config(home)
    LaneStore(cfg.db_path).add(LaneSpec(id=LANE))
    cfg.profile_dir(LANE).mkdir(parents=True)
    return home


def run(*args: str):
    result = CliRunner().invoke(app, ["identity", *args])
    assert EM_DASH not in result.output
    return result


def test_init_show_and_the_default_is_off(home):
    result = run("init", LANE, "--agent", "https://Agents.example/")
    assert result.exit_code == 0, result.output
    assert "off: no hosts to sign for" in result.output
    cfg = load_config(home)
    key = keys.key_path(cfg.secrets_dir, LANE)
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    view = json.loads(run("show", LANE, "--json").output)
    assert view["signature_agent"] == "https://agents.example" and view["form"] == "bare"
    assert view["hosts"] == [] and view["published"] is False
    assert view["covered"] == ["@authority", "@method", "signature-agent"] and view["window_s"] == 120
    assert "d" not in view["public_jwk"] and "PRIVATE" not in json.dumps(view)
    again = json.loads(run("init", LANE, "--json").output)
    assert again["keyid"] == view["keyid"]  # init again keeps the key


def test_init_needs_an_existing_lane_and_an_https_agent(home):
    assert run("init", "acme/nope", "--agent", "https://agents.example").exit_code == 1
    result = run("init", LANE, "--agent", "http://agents.example")
    assert result.exit_code == 1 and "https" in result.output
    assert run("init", LANE, "--form", "weird").exit_code == 1


def test_hosts_add_and_remove(home):
    run("init", LANE, "--agent", "https://agents.example")
    assert run("hosts", "add", LANE, "example.com").exit_code != 0  # --reason is required
    result = run("hosts", "add", LANE, "example.com", "--reason", "read-only research")
    assert result.exit_code == 0, result.output
    assert "https://example.com" in result.output and "automated agent" in result.output
    assert run("hosts", "add", LANE, "http://example.com", "--reason", "x").exit_code == 1
    view = json.loads(run("show", LANE, "--json").output)
    assert [h["origin"] for h in view["hosts"]] == ["https://example.com"]
    assert view["hosts"][0]["added_by"] == "owner:cli"
    assert "signing for 1 host" in view["state"]
    result = run("hosts", "remove", LANE, "https://example.com")
    assert result.exit_code == 0 and "no route" in result.output
    assert run("hosts", "remove", LANE, "https://example.com").exit_code == 1


def test_hosts_need_an_identity_and_an_agent(home):
    assert "openajar identity init" in run("hosts", "add", LANE, "example.com", "--reason", "x").output
    run("init", LANE)
    assert "signature agent" in run("hosts", "add", LANE, "example.com", "--reason", "x").output


def test_a_recreated_lane_needs_force_and_starts_off(home, tmp_path):
    run("init", LANE, "--agent", "https://agents.example")
    run("hosts", "add", LANE, "example.com", "--reason", "research")
    first = json.loads(run("show", LANE, "--json").output)["keyid"]
    cfg = load_config(home)
    cfg.profile_dir(LANE).rename(tmp_path / "trashed")
    cfg.profile_dir(LANE).mkdir()
    assert "profile changed" in json.loads(run("show", LANE, "--json").output)["state"]
    result = run("init", LANE)
    assert result.exit_code == 1 and "--force" in result.output
    view = json.loads(run("init", LANE, "--force", "--json").output)
    assert view["keyid"] != first and view["hosts"] == []
    assert view["signature_agent"] == "https://agents.example"


def test_test_sign_hides_the_signature_and_refuses_unlisted_hosts(home):
    run("init", LANE, "--agent", "https://agents.example")
    run("hosts", "add", LANE, "example.com", "--reason", "research")
    result = run("test-sign", LANE, "https://example.com/a?b=c")
    assert result.exit_code == 0, result.output
    assert 'Signature-Agent: "https://agents.example"' in result.output
    assert "hidden" in result.output and "local strict check: valid" in result.output
    signature_line = next(line for line in result.output.splitlines() if line.startswith("Signature:"))
    assert signature_line == "Signature: sig1=:<64-byte signature, hidden>:"
    revealed = run("test-sign", LANE, "https://example.com/", "--reveal").output
    assert "hidden" not in revealed and "Signature: sig1=:" in revealed
    refused = run("test-sign", LANE, "https://other.example/")
    assert refused.exit_code == 1 and "never signs" in refused.output


def test_settings_written_by_the_cli_load_in_the_provider(home):
    run("init", LANE, "--agent", "https://agents.example")
    run("hosts", "add", LANE, "example.com", "--reason", "research")
    from openajar.webbotauth.route import IdentityProvider

    cfg = load_config(home)
    signer = IdentityProvider(cfg).signer_for(LANE, profile_dir=cfg.profile_dir(LANE), proxied=True)
    assert signer is not None and signer.origins == {"https://example.com"}
    assert settings.load(cfg.secrets_dir, LANE).hosts[0].reason == "research"


# ---- doctor ---------------------------------------------------------------------------------

def test_doctor_clock_warning_names_signing_lanes(home):
    host = FakeHost()
    ctx = DoctorContext(home=home, host=host)
    assert doctor.check_clock(ctx)[0].status == PASS
    host.commands["timedatectl show"] = (0, "NTP=yes\nNTPSynchronized=no\n")
    plain = doctor.check_clock(ctx)[0]
    assert plain.status == WARN and "sign" not in plain.detail
    run("init", LANE, "--agent", "https://agents.example")
    assert "sign" not in doctor.check_clock(ctx)[0].detail  # an identity with no hosts signs nothing
    run("hosts", "add", LANE, "example.com", "--reason", "research")
    r = doctor.check_clock(ctx)[0]
    assert r.status == WARN and "1 lane(s) sign requests" in r.detail and r.data == {"signing_lanes": [LANE]}
    assert EM_DASH not in r.detail
    host.commands["timedatectl show"] = (0, "NTP=yes\nNTPSynchronized=yes\nTimezone=UTC\n")
    assert doctor.check_clock(ctx)[0].status == PASS
