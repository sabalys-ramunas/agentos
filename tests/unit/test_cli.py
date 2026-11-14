"""`agentos` CLI with AGENTOS_HOME in a temp dir. Lane commands run against FakeLaneManager."""

import asyncio
import json
import stat
import tomllib
from urllib.parse import urlsplit

import httpx
import pytest
from fakes import FakeLaneManager
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient
from typer.testing import CliRunner

from agentos import __version__
from agentos.cli import app
from agentos.commands import base as cli_base
from agentos.core.config import load_config
from agentos.core.models import LaneSpec
from agentos.node import daemon

PASSWORD = "Pa55:with:colons"
USERNAME = "brd-customer-zone-isp"

EM_DASH = "\u2014"


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "agentos-home"
    monkeypatch.setenv("AGENTOS_HOME", str(home))
    return home


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def inited(home, runner):
    result = runner.invoke(app, ["node", "init"])
    assert result.exit_code == 0, result.output
    return home


@pytest.fixture
def fake_manager(monkeypatch, inited):
    """One manager shared by every CLI call in a test, standing in for the store."""
    manager = FakeLaneManager(load_config(inited))
    monkeypatch.setattr(daemon, "build_lane_manager", lambda cfg: manager)

    def no_daemon(method, url, *, body, headers):  # agentosd is not running: the CLI writes the store
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(cli_base, "_send", no_daemon)
    return manager


def mode(path) -> str:
    return oct(stat.S_IMODE(path.stat().st_mode))[-3:]


def test_version(runner):
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0 and result.output.strip() == __version__


def test_init_writes_config_and_prints_connect_lines(home, runner, monkeypatch):
    monkeypatch.delenv("AGENTOS_SSH_HOST", raising=False)
    monkeypatch.setattr("socket.gethostname", lambda: "mynode")  # no ssh_host set: the hostname
    result = runner.invoke(app, ["node", "init"])
    assert result.exit_code == 0, result.output

    path = home / "node.toml"
    assert mode(path) == "600"
    assert mode(home) == "700" and mode(home / "secrets") == "700"
    token = tomllib.loads(path.read_text())["mcp_token"]
    assert len(token) >= 40

    assert "agentos connect mynode" in result.output
    assert "ssh -N -L 7400:localhost:7400 mynode" in result.output
    claude = (  # user scope, so agentos is there in every folder (AUD-32)
        "claude mcp add --scope user --transport http agentos http://localhost:7400/mcp "
        '--header "Authorization: Bearer <grant token>"'
    )
    assert claude in result.output
    # the by-hand way gives the agents a grant; the node token is never printed
    assert "agentos access grants issue --subject claude-code" in result.output and token not in result.output
    assert EM_DASH not in result.output


def test_init_is_idempotent_and_rotates_on_request(inited, runner):
    first = load_config(inited).mcp_token
    again = runner.invoke(app, ["node", "init"])
    assert again.exit_code == 0 and "Updated" in again.output
    assert load_config(inited).mcp_token == first

    rotated = runner.invoke(app, ["node", "init", "--new-token"])
    assert rotated.exit_code == 0
    assert load_config(inited).mcp_token not in ("", first)


def test_rotate_token_replaces_only_the_token(inited, runner):
    before = load_config(inited)
    result = runner.invoke(app, ["node", "rotate-token"])
    assert result.exit_code == 0, result.output
    after = load_config(inited)
    assert after.mcp_token not in ("", before.mcp_token)
    assert after.model_copy(update={"mcp_token": before.mcp_token}) == before
    assert "agentos node restart" in result.output and "agentos connect --upgrade" in result.output
    assert after.mcp_token not in result.output and EM_DASH not in result.output


def test_init_options_for_a_dev_node(home, runner):
    result = runner.invoke(app, ["node", "init", "--name", "dev-mcp", "--mcp-port", "7430",
                                 "--display-base", "130", "--viewer-base", "6130", "--vnc-base", "5930",
                                 "--max-warm-lanes", "2", "--ssh-host", "devbox"])
    assert result.exit_code == 0, result.output
    cfg = load_config(home)
    assert (cfg.name, cfg.mcp_port, cfg.display_base, cfg.viewer_base, cfg.vnc_base, cfg.max_warm_lanes) == (
        "dev-mcp", 7430, 130, 6130, 5930, 2)
    assert "ssh -N -L 7430:localhost:7430 devbox" in result.output
    assert "http://localhost:7430/mcp" in result.output


def test_commands_need_init(home, runner):
    for args in (["proxy", "list"], ["lane", "list"], ["node", "serve"], ["proxy", "add", "lt1", "h:1"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 1 and "agentos node init" in result.output, args


def test_proxy_add_stores_credentials_privately(inited, runner):
    result = runner.invoke(app, ["proxy", "add", "lt1", f"5.6.7.8:12323:{USERNAME}:{PASSWORD}"])
    assert result.exit_code == 0, result.output
    assert "lt1" in result.output and "5.6.7.8:12323" in result.output
    assert PASSWORD not in result.output and USERNAME not in result.output

    path = inited / "secrets" / "proxies.json"
    assert mode(path) == "600" and mode(path.parent) == "700"
    stored = json.loads(path.read_text())["lt1"]
    assert stored == {"host": "5.6.7.8", "port": 12323, "username": USERNAME, "password": PASSWORD}

    listed = runner.invoke(app, ["proxy", "list"])
    assert listed.exit_code == 0 and "lt1" in listed.output
    assert PASSWORD not in listed.output and USERNAME not in listed.output


def test_proxy_add_prompts_for_a_missing_password(inited, runner):
    result = runner.invoke(app, ["proxy", "add", "lt2", f"1.2.3.4:8000:{USERNAME}"], input=f"{PASSWORD}\n")
    assert result.exit_code == 0, result.output
    assert PASSWORD not in result.output
    stored = json.loads((inited / "secrets" / "proxies.json").read_text())["lt2"]
    assert stored["password"] == PASSWORD


def test_proxy_add_without_credentials(inited, runner):
    result = runner.invoke(app, ["proxy", "add", "open", "1.2.3.4:3128"])
    assert result.exit_code == 0 and "without credentials" in result.output


@pytest.mark.parametrize(
    ("label", "spec"),
    [("lt1", "nohost"), ("lt1", "1.2.3.4:http"), ("lt1", "1.2.3.4:70000"), ("Bad Label", "1.2.3.4:80")],
)
def test_proxy_add_rejects(inited, runner, label, spec):
    result = runner.invoke(app, ["proxy", "add", label, spec])
    assert result.exit_code == 2, result.output
    assert not (inited / "secrets" / "proxies.json").exists()


@pytest.mark.parametrize("bad", ["Personal/reddit", "reddit", "a/b/c", "a/", "/b", "a b/c", "../etc"])
@pytest.mark.parametrize("command", ["create", "import"])
def test_lane_commands_reject_invalid_ids(fake_manager, runner, tmp_path, bad, command):
    args = ["lane", command, bad] + (["--profile", str(tmp_path)] if command == "import" else [])
    result = runner.invoke(app, args)
    assert result.exit_code == 2, result.output
    assert "workspace/name" in result.output
    assert fake_manager.lanes == {}


def test_lane_create_and_list(fake_manager, runner):
    assert runner.invoke(app, ["proxy", "add", "lt1", "5.6.7.8:12323:u:p"]).exit_code == 0
    result = runner.invoke(app, ["lane", "create", "personal/reddit", "--proxy", "lt1"])
    assert result.exit_code == 0, result.output
    assert "Created lane personal/reddit (engine camoufox, proxy lt1)" in result.output
    assert fake_manager.lanes["personal/reddit"].spec.proxy_label == "lt1"

    listed = runner.invoke(app, ["lane", "list"])
    assert listed.exit_code == 0
    assert listed.output.splitlines()[0].split() == ["LANE", "STATE", "ENGINE", "DEVICE", "PROXY", "LAST", "USED"]
    assert listed.output.splitlines()[1].split() == ["personal/reddit", "cold", "camoufox", "own", "lt1", "never"]


def test_lane_create_reports_agentos_errors(fake_manager, runner):
    assert runner.invoke(app, ["lane", "create", "personal/reddit"]).exit_code == 0
    again = runner.invoke(app, ["lane", "create", "personal/reddit"])
    assert again.exit_code == 1 and "lane_exists:" in again.output


def test_lane_create_needs_a_known_proxy(fake_manager, runner):
    result = runner.invoke(app, ["lane", "create", "personal/reddit", "--proxy", "nope"])
    assert result.exit_code == 1 and "no proxy 'nope'" in result.output
    assert fake_manager.lanes == {}


def test_lane_import(fake_manager, runner, tmp_path):
    profile = tmp_path / "poc-profile"
    profile.mkdir()
    (profile / "cookies.sqlite").write_text("x")
    fingerprint = tmp_path / "fp.json"
    fingerprint.write_text("{}")
    result = runner.invoke(app, ["lane", "import", "personal/reddit", "--profile", str(profile),
                                 "--fingerprint", str(fingerprint)])
    assert result.exit_code == 0, result.output
    assert "Imported lane personal/reddit" in result.output
    assert (fake_manager.config.profile_dir("personal/reddit") / "cookies.sqlite").exists()


def test_lane_import_needs_an_existing_profile(fake_manager, runner, tmp_path):
    result = runner.invoke(app, ["lane", "import", "personal/reddit", "--profile", str(tmp_path / "missing")])
    assert result.exit_code == 2
    assert fake_manager.lanes == {}


def test_lane_list_empty(fake_manager, runner):
    result = runner.invoke(app, ["lane", "list"])
    assert result.exit_code == 0 and "No lanes" in result.output


def test_lane_list_asks_the_running_daemon(fake_manager, runner, monkeypatch):
    """Only agentosd knows which browsers run: a warm lane there is never listed cold (2026-11-10 re-test)."""
    seen = []

    def send(method, url, *, body, headers):
        seen.append((method, url))
        return httpx.Response(200, json=[{"id": "biz/linkedin", "state": "active", "engine": "camoufox",
                                          "device": None, "proxy": {"label": "lt1"},
                                          "last_used": "2026-11-10T11:24:49+00:00"}])

    monkeypatch.setattr(cli_base, "_send", send)
    result = runner.invoke(app, ["lane", "list"])
    assert result.exit_code == 0, result.output
    assert seen and seen[0][0] == "GET" and seen[0][1].endswith("/api/lanes")
    line = next(row for row in result.output.splitlines() if row.startswith("biz/linkedin"))
    assert line.split()[1] == "active" and "lt1" in line and "2026-11-10 11:24" in line


def test_no_em_dash_in_help(runner):
    for args in ([], ["node"], ["proxy"], ["lane"], ["node", "init"], ["proxy", "add"], ["lane", "import"]):
        result = runner.invoke(app, [*args, "--help"])
        assert result.exit_code == 0 and EM_DASH not in result.output, args


def test_missing_lane_modules_give_a_clean_error(inited, runner, monkeypatch):
    def missing(cfg):
        raise ModuleNotFoundError("No module named 'agentos.node.lanes'", name="agentos.node.lanes")

    def no_daemon(method, url, *, body, headers):  # lane list asks agentosd first
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(daemon, "build_lane_manager", missing)
    monkeypatch.setattr(cli_base, "_send", no_daemon)
    result = runner.invoke(app, ["lane", "list"])
    assert result.exit_code == 1
    assert "agentos.node.lanes is not part of this build" in result.output
    assert "Traceback" not in result.output


def test_tracebacks_hide_locals():
    assert app.pretty_exceptions_show_locals is False


# ---- lane sites (the egress guard's site list) ----

def test_lane_sites_add_remove_suggest_strict_and_show(fake_manager, runner):
    assert runner.invoke(app, ["lane", "create", "biz-a/instagram"]).exit_code == 0
    shown = runner.invoke(app, ["lane", "sites", "biz-a/instagram"])
    assert shown.exit_code == 0, shown.output
    assert "no site list: no lane restriction" in shown.output and "Strict egress: off." in shown.output

    added = runner.invoke(app, ["lane", "sites", "biz-a/instagram", "add", "Instagram.com", "*.instagram.com"])
    assert added.exit_code == 0, added.output
    assert "loads documents only from: instagram.com, *.instagram.com" in added.output
    listed = runner.invoke(app, ["lane", "sites", "biz-a/instagram", "suggest"])
    assert listed.exit_code == 0 and "instagram: instagram.com" in listed.output
    assert fake_manager.lanes["biz-a/instagram"].spec.sites == ["instagram.com", "*.instagram.com"]  # listing only

    suggested = runner.invoke(app, ["lane", "sites", "biz-a/instagram", "suggest", "instagram"])
    assert suggested.exit_code == 0, suggested.output
    sites = fake_manager.lanes["biz-a/instagram"].spec.sites
    assert sites[:2] == ["instagram.com", "*.instagram.com"] and "*.cdninstagram.com" in sites
    assert len(sites) == len(set(sites))

    removed = runner.invoke(app, ["lane", "sites", "biz-a/instagram", "remove", "instagram.com"])
    assert removed.exit_code == 0 and "instagram.com" not in fake_manager.lanes["biz-a/instagram"].spec.sites
    assert runner.invoke(app, ["lane", "sites", "biz-a/instagram", "strict", "on"]).exit_code == 0
    assert fake_manager.lanes["biz-a/instagram"].spec.egress_strict
    assert runner.invoke(app, ["lane", "sites", "biz-a/instagram", "clear"]).exit_code == 0
    assert fake_manager.lanes["biz-a/instagram"].spec.sites == []


def test_lane_sites_says_when_a_host_has_no_effect_rules(fake_manager, runner):
    assert runner.invoke(app, ["lane", "create", "biz-a/x"]).exit_code == 0
    added = runner.invoke(app, ["lane", "sites", "biz-a/x", "add", "x.com", "frame:www.google.com"])
    assert added.exit_code == 0, added.output
    assert "No effect rules for: x.com. Publishes there are not gated" in added.output
    assert EM_DASH not in added.output
    reddit = runner.invoke(app, ["lane", "sites", "biz-a/x", "clear"])
    assert "No effect rules" not in reddit.output


def test_lane_rules_shows_rule_health_and_drift(fake_manager, runner):
    from agentos.node import egress

    assert runner.invoke(app, ["lane", "create", "biz-a/reddit"]).exit_code == 0
    lane_dir = fake_manager.config.lane_dir("biz-a/reddit")
    lane_dir.mkdir(parents=True, exist_ok=True)
    trace = egress.EgressTrace(lane_dir / egress.TRACE_NAME)
    trace.write({"kind": "blocked", "lane": "biz-a/reddit", "rule": "commit_gate", "host": "www.reddit.com",
                 "path": "/svc/shreddit/t3_x/create-comment", "method": "POST", "effect_rule": "reddit.comment"})
    trace.write({"kind": "gate_classified", "lane": "biz-a/reddit", "host": "www.reddit.com",
                 "path": "/svc/shreddit/graphql", "method": "POST", "decision": "gated", "why": "x",
                 "rule": "reddit.read.graphql", "shape": {"op": "CreateCommentV2"}})
    shown = runner.invoke(app, ["lane", "rules"])
    assert shown.exit_code == 0, shown.output
    assert "reddit.comment" in shown.output and "x1" in shown.output and "CreateCommentV2" in shown.output
    assert EM_DASH not in shown.output
    as_json = runner.invoke(app, ["lane", "rules", "biz-a/reddit", "--json"])
    assert as_json.exit_code == 0 and json.loads(as_json.output)["rules"]


def test_lane_sites_go_through_a_running_daemon(fake_manager, runner, monkeypatch):
    """While agentosd runs, a change goes to its own lane manager, so a warm lane has it at once."""
    from agentos.node.viewer_routes import control_api_routes
    from agentos.node.viewer_tokens import ViewTokens

    asyncio.run(fake_manager.create(LaneSpec(id="personal/reddit")))
    daemon_lanes = FakeLaneManager(fake_manager.config)  # the daemon's manager, not the CLI's
    daemon_lanes.lanes = fake_manager.lanes
    fake_manager.set_sites = None  # the CLI must not write the store itself
    api = Starlette(routes=[Mount("/api", routes=control_api_routes(daemon_lanes, ViewTokens.for_config(
        fake_manager.config)))])
    sent: list[tuple[str, dict]] = []

    with TestClient(api) as client:
        def send(method, url, *, body, headers):
            assert headers["Authorization"].startswith("Bearer ")
            sent.append((urlsplit(url).path, body))
            return client.request(method, urlsplit(url).path, json=body, headers=headers)

        monkeypatch.setattr(cli_base, "_send", send)
        r = runner.invoke(app, ["lane", "sites", "personal/reddit", "suggest", "reddit"])
        assert r.exit_code == 0, r.output
        assert sent[-1][0] == "/api/lanes/personal/reddit/sites" and sent[-1][1]["op"] == "add"
        sites = fake_manager.lanes["personal/reddit"].spec.sites
        assert sites[0] == "reddit.com" and "frame:www.google.com" in sites and "accounts.google.@cc" in sites
        assert "frame:www.google.com" in r.output
        r = runner.invoke(app, ["lane", "sites", "personal/reddit", "remove", "nope.example"])
        assert r.exit_code != 0 and "not on the list: nope.example" in r.output and EM_DASH not in r.output
        assert runner.invoke(app, ["lane", "sites", "personal/reddit", "strict", "on"]).exit_code == 0
        assert sent[-1][1] == {"op": "add", "sites": [], "strict": True}
        assert runner.invoke(app, ["lane", "sites", "personal/reddit", "clear"]).exit_code == 0
        assert fake_manager.lanes["personal/reddit"].spec.sites == []

        # an agentosd from before the route: the store, as before
        monkeypatch.setattr(cli_base, "_send", lambda method, url, *, body, headers: httpx.Response(
            404, json={"error": {"code": "not_found", "message": "no such route"}}))
        fake_manager.set_sites = FakeLaneManager.set_sites.__get__(fake_manager)
        r = runner.invoke(app, ["lane", "sites", "personal/reddit", "add", "frame:challenges.cloudflare.com"])
        assert r.exit_code == 0, r.output
        assert fake_manager.lanes["personal/reddit"].spec.sites == ["frame:challenges.cloudflare.com"]


def test_lane_sites_observe_on_and_off(fake_manager, runner):
    from agentos.node import egress

    assert runner.invoke(app, ["lane", "create", "biz-a/instagram"]).exit_code == 0
    on = runner.invoke(app, ["lane", "sites", "biz-a/instagram", "observe", "on", "30"])
    assert on.exit_code == 0 and "traced by its shape until" in on.output, on.output
    f = fake_manager.config.lane_dir("biz-a/instagram") / egress.OBSERVE_NAME
    assert egress.read_observe(f) is not None
    assert "Observing requests until" in runner.invoke(app, ["lane", "sites", "biz-a/instagram"]).output
    off = runner.invoke(app, ["lane", "sites", "biz-a/instagram", "observe", "off"])
    assert off.exit_code == 0 and not f.exists()


@pytest.mark.parametrize("args", [["add"], ["add", "*"], ["add", "bad host.com"], ["remove", "nope.example"],
                                  ["suggest", "myspace"], ["strict", "maybe"], ["frobnicate"], ["show", "x.com"],
                                  ["observe"], ["observe", "on", "999"], ["observe", "on", "x"], ["observe", "off", "1"]])
def test_lane_sites_rejects(fake_manager, runner, args):
    assert runner.invoke(app, ["lane", "create", "biz-a/instagram"]).exit_code == 0
    result = runner.invoke(app, ["lane", "sites", "biz-a/instagram", *args])
    assert result.exit_code != 0, result.output
    assert fake_manager.lanes["biz-a/instagram"].spec.sites == []
    assert "—" not in result.output


# ---- lane gate-log (the commit gate's unknown shapes) ----

def test_lane_gate_log_sends_since_url_encoded(fake_manager, runner, inited, monkeypatch):
    """A zone's + would reach the daemon as a space (and fail to parse) unless it is encoded."""
    from agentos.commands import lane_sites

    sent: list[str] = []

    def request(cfg, method, path, body=None, **kw):
        sent.append(path)
        return {"lane": "biz-a/reddit", "shapes": [], "promoted": []}

    monkeypatch.setattr(lane_sites, "daemon_request", request)
    result = runner.invoke(app, ["lane", "gate-log", "biz-a/reddit", "--since", "2026-11-04T10:00:00+02:00"])
    assert result.exit_code == 0, result.output
    assert sent == ["/api/lanes/biz-a/reddit/gate-log?since=2026-11-04T10%3A00%3A00%2B02%3A00"]


def test_lane_gate_log_lists_and_promotes_with_an_audit_row(fake_manager, runner, inited):
    from agentos.node import egress, gateshapes
    from agentos.policy.service import PolicyService

    assert runner.invoke(app, ["lane", "create", "biz-a/reddit"]).exit_code == 0
    lane_dir = load_config(inited).lane_dir("biz-a/reddit")
    lane_dir.mkdir(parents=True, exist_ok=True)
    trace = egress.EgressTrace(lane_dir / egress.TRACE_NAME)
    row = {"kind": "gate_classified", "lane": "biz-a/reddit", "host": "www.reddit.com",
           "path": "/svc/shreddit/graphql", "method": "POST", "decision": "gated",
           "why": "an unknown operation FetchGoldBalance", "rule": "reddit.read.graphql",
           "shape": {"type": "application/json", "op": "FetchGoldBalance", "fields": ["consent"]}}
    trace.write(row)
    trace.write(row)
    empty = runner.invoke(app, ["lane", "gate-log", "biz-a/reddit", "--since", "2000-01-01T00:00:00+00:00"])
    assert empty.exit_code == 0, empty.output
    listed = runner.invoke(app, ["lane", "gate-log", "biz-a/reddit"])
    assert listed.exit_code == 0, listed.output
    sid = gateshapes.shape_id("www.reddit.com", "/svc/shreddit/graphql", "POST", "FetchGoldBalance")
    assert sid in listed.output and "x2 (gated 2)" in listed.output and "fields: consent" in listed.output
    assert EM_DASH not in listed.output
    bad = runner.invoke(app, ["lane", "gate-log", "biz-a/reddit", "--as-commit", "post.x"])
    assert bad.exit_code == 1 and "--shape" in bad.output
    done = runner.invoke(app, ["lane", "gate-log", "biz-a/reddit", "--allow-read", sid])
    assert done.exit_code == 0, done.output
    assert ("Promoted POST www.reddit.com/svc/shreddit/graphql FetchGoldBalance to the read allowlist on "
            "biz-a/reddit") in done.output
    assert "[promoted: read on biz-a/reddit]" in runner.invoke(app, ["lane", "gate-log", "biz-a/reddit"]).output
    traces = PolicyService(inited).traces
    [audit], _ = traces.query(kind="gate_shape")
    assert audit.lane == "biz-a/reddit" and audit.principal == "owner:cli" and audit.args["as"] == "read"
    missing = runner.invoke(app, ["lane", "gate-log", "biz-a/reddit", "--allow-read", "sh_nope"])
    assert missing.exit_code == 1 and "no shape sh_nope" in missing.output


# ---- lane pace (AUD-13) ----

def test_lane_pace_shows_sets_and_removes_a_lanes_pace_without_the_daemon(fake_manager, runner, inited):
    shown = runner.invoke(app, ["lane", "pace", "biz-a/instagram"])
    assert shown.exit_code == 0, shown.output
    assert "not paced" in shown.output and "--preset" in shown.output and EM_DASH not in shown.output
    preset = runner.invoke(app, ["lane", "pace", "biz-a/instagram", "--preset", "--json"])
    assert preset.exit_code == 0, preset.output
    assert json.loads(preset.output)["pace"] == {"commits_per_day": 3, "min_gap_minutes": 45,
                                                 "active_hours": ["08:00-22:00"]}
    tuned = runner.invoke(app, ["lane", "pace", "biz-a/instagram", "--gap", "90", "--hours", "mon-fri 09:00-18:00",
                                "--timezone", "America/New_York"])
    assert tuned.exit_code == 0, tuned.output
    assert "lane time is America/New_York (set on the lane)" in tuned.output
    assert "waking hours mon-fri 09:00-18:00; at least 90 minutes apart; 3 a day" in tuned.output
    bad = runner.invoke(app, ["lane", "pace", "biz-a/instagram", "--hours", "9-5"])
    assert bad.exit_code != 0 and EM_DASH not in bad.output
    ws = runner.invoke(app, ["lane", "pace", "--workspace", "biz-a", "--timezone", "Asia/Tokyo"])
    assert ws.exit_code == 0 and "read Asia/Tokyo" in ws.output
    shown_ws = runner.invoke(app, ["lane", "pace", "--workspace", "biz-a"])  # shows it, never clears it
    assert shown_ws.exit_code == 0 and "read Asia/Tokyo" in shown_ws.output and "--timezone off" in shown_ws.output
    cleared = runner.invoke(app, ["lane", "pace", "--workspace", "biz-a", "--timezone", "off", "--json"])
    assert cleared.exit_code == 0 and json.loads(cleared.output)["timezone"] is None
    off = runner.invoke(app, ["lane", "pace", "biz-a/instagram", "--off", "--json"])
    assert off.exit_code == 0 and json.loads(off.output)["paced"] is False


def test_device_writes_go_through_a_running_daemon(fake_manager, runner, monkeypatch):
    """While agentosd runs, `lane set-device`, `device create` and `device remove` go to its own lane
    manager (its device locks and running lanes), never a second one in the CLI's process."""
    from agentos.commands import devices, lanes

    sent: list[tuple[str, str, dict | None]] = []
    plan = {"lane": "biz-a/x", "from": None, "to": "biz-a-pc", "changes": [], "warnings": ["a new computer"],
            "confirm": True, "proxy": {"from": "lt1", "to": "lt1"}}

    def request(cfg, method, path, body=None, **kw):
        sent.append((method, path, body))
        if path.endswith("/device-move?to=biz-a-pc"):
            return plan
        if path.endswith("/device"):
            return {"plan": {**plan, "done": True}, "lane": {}}
        if method == "POST":
            return {"name": "biz-a-pc", "proxy": "lt1"}
        return {"removed": "biz-a-pc", "trash": "/trash/device--biz-a-pc"}

    monkeypatch.setattr(lanes, "daemon_request", request)
    monkeypatch.setattr(devices, "daemon_request", request)
    for name in ("set_device", "create_device", "remove_device", "plan_device_move"):
        monkeypatch.setattr(fake_manager, name, None, raising=False)  # the CLI must not write the store itself
    refused = runner.invoke(app, ["lane", "set-device", "biz-a/x", "biz-a-pc"])
    assert refused.exit_code != 0 and "--yes" in refused.output and len(sent) == 1
    moved = runner.invoke(app, ["lane", "set-device", "biz-a/x", "biz-a-pc", "--yes"])
    assert moved.exit_code == 0, moved.output
    assert sent[-1] == ("POST", "/api/lanes/biz-a/x/device", {"device": "biz-a-pc", "confirm": True})
    made = runner.invoke(app, ["device", "create", "biz-a-pc", "--from-lane", "biz-a/x", "--yes"])
    assert made.exit_code == 0, made.output
    assert sent[-1][1] == "/api/browser-devices" and sent[-1][2]["from_lane"] == "biz-a/x" and sent[-1][2]["confirm"]
    gone = runner.invoke(app, ["device", "remove", "biz-a-pc", "--yes"])
    assert gone.exit_code == 0 and "/trash/device--biz-a-pc" in gone.output, gone.output
    assert sent[-1][:2] == ("DELETE", "/api/browser-devices/biz-a-pc")


def test_proxy_add_over_an_existing_label_needs_a_yes(fake_manager, runner):
    assert runner.invoke(app, ["proxy", "add", "lt1", "5.6.7.8:12323:u:p"]).exit_code == 0
    assert runner.invoke(app, ["lane", "create", "personal/reddit", "--proxy", "lt1"]).exit_code == 0
    same = runner.invoke(app, ["proxy", "add", "lt1", "5.6.7.8:12323:u:new-password"])
    assert same.exit_code == 0, same.output  # the same proxy, a new password: no new IP
    moved = runner.invoke(app, ["proxy", "add", "lt1", "9.9.9.9:12323:u:p"])
    assert moved.exit_code != 0 and "new IP" in moved.output and EM_DASH not in moved.output
    stored = json.loads((fake_manager.config.home / "secrets" / "proxies.json").read_text())["lt1"]
    assert stored["host"] == "5.6.7.8"
    assert runner.invoke(app, ["proxy", "add", "lt1", "9.9.9.9:12323:u:p", "--yes"]).exit_code == 0
    stored = json.loads((fake_manager.config.home / "secrets" / "proxies.json").read_text())["lt1"]
    assert stored["host"] == "9.9.9.9"


def test_lane_pace_options_apply_over_the_current_pace():
    from agentos.commands.lane_sites import _changed_pace

    current = {"pace": {"active_hours": ["09:00-17:00"], "min_gap_minutes": 30, "timezone": "Europe/Vilnius"},
               "preset": {"active_hours": ["08:00-22:00"], "min_gap_minutes": 45, "commits_per_day": 3}}
    assert _changed_pace(current, preset=True, hours=None, gap=None, per_day=None, timezone="auto") == {
        "active_hours": ["08:00-22:00"], "min_gap_minutes": 45, "commits_per_day": 3}
    assert _changed_pace(current, preset=False, hours=["10:00-12:00"], gap=5, per_day=None, timezone=None) == {
        "active_hours": ["10:00-12:00"], "min_gap_minutes": 5, "timezone": "Europe/Vilnius"}
    assert _changed_pace({"pace": None}, preset=False, hours=None, gap=None, per_day=None, timezone="") is None
