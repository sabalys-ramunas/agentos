"""`agentos workspace` commands, run through typer's CliRunner against a temporary AGENTOS_HOME and an
in-memory lane manager. The proxy file holds dummy labels and addresses only."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from agentos.core.config import save_proxies
from agentos.core.models import ProxyConfig
from agentos.workspaces import cli
from agentos.workspaces.cli import app
from agentos.workspaces.fakes import FakeLanes

runner = CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch):
    from agentos.core.config import NodeConfig

    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("AGENTOS_HOME", str(root))
    save_proxies(NodeConfig(home=root), {
        "lt1": ProxyConfig(label="lt1", host="192.0.2.10", port=1000, username="u", password="not-a-real-secret"),
        "lt2": ProxyConfig(label="lt2", host="192.0.2.11", port=1000, username="u", password="not-a-real-secret"),
    })
    lanes = FakeLanes({"lt1", "lt2"})
    monkeypatch.setattr(cli, "_lane_manager", lambda cfg: lanes)
    return root, lanes


def run(*args: str, input_text: str | None = None):
    return runner.invoke(app, list(args), input=input_text)


def test_create_list_show_and_update(home):
    assert "No workspaces yet" in run("list").output
    out = run("create", "acme", "--name", "Acme", "--color", "#4F7CFF", "--proxy", "lt1")
    assert out.exit_code == 0 and "created workspace acme (Acme)" in out.output
    listed = run("list").output
    assert "acme" in listed and "no lanes yet" in listed
    shown = run("show", "acme").output
    assert "default proxy: lt1" in shown and "color: #4f7cff" in shown
    assert run("update", "acme", "--no-proxy", "--notes", "main brand").exit_code == 0
    assert "default proxy: none" in run("show", "acme").output
    assert run("update", "acme", "--proxy", "lt1", "--no-proxy").exit_code == 1


def test_json_output_is_parseable(home):
    run("create", "acme")
    data = json.loads(run("list", "--json").output)
    assert data[0]["id"] == "acme" and data[0]["derived"] is False
    assert json.loads(run("show", "acme", "--json").output)["name"] == "Acme"


def test_a_lane_prefix_without_a_record_is_listed_as_derived(home):
    _, lanes = home
    lanes.add("legacy/reddit", proxy="lt1")
    out = run("list").output
    assert "Legacy" in out and "name from lane ids" in out


def test_delete_asks_and_is_refused_with_lanes(home):
    _, lanes = home
    run("create", "acme")
    lanes.add("acme/reddit")
    refused = run("delete", "acme", "--yes")
    assert refused.exit_code == 1 and "error: conflict: workspace 'acme' still has 1 lane" in refused.output
    assert run("delete", "acme", input_text="n\n").exit_code == 1  # declined, nothing deleted
    lanes.lanes.clear()
    assert run("delete", "acme", "--yes").exit_code == 0


def test_errors_are_one_line_with_their_code_and_exit_one(home):
    out = run("show", "ghost")
    assert out.exit_code == 1 and out.output.strip().startswith("error: not_found: no workspace 'ghost'")
    assert "Traceback" not in out.output


def test_templates_list_the_builtins_and_report_a_bad_file(home):
    root, _ = home
    (root / "templates").mkdir()
    (root / "templates" / "broken.toml").write_text("[[surfaces\n", encoding="utf-8")
    out = run("templates")
    assert out.exit_code == 0 and "social-6" in out.output and "personal" in out.output
    assert "Not usable" in out.output and "broken.toml: not valid TOML" in out.output


def test_plan_shows_a_table_and_creates_nothing(home):
    _, lanes = home
    run("create", "acme")
    out = run("plan", "social-6", "-w", "acme", "--proxy", "acme=lt1")
    assert out.exit_code == 0
    assert "LANE" in out.output and "acme/instagram" in out.output and "create" in out.output
    assert "6 to create, 0 already there and matching, 0 conflicts" in out.output
    assert lanes.created == []


def test_plan_with_a_conflict_exits_one_and_says_why(home):
    _, lanes = home
    run("create", "acme")
    run("create", "beta")
    lanes.add("acme/instagram", proxy="lt1")
    out = run("plan", "social-6", "-w", "beta", "--proxy", "beta=lt1")
    assert out.exit_code == 1
    assert "conflict" in out.output and "beta/instagram" in out.output and "proxy lt1 is already used" in out.output


def test_apply_shows_the_plan_asks_and_creates_when_confirmed(home):
    _, lanes = home
    run("create", "acme")
    out = run("apply", "social-6", "-w", "acme", "--proxy", "acme=lt1", input_text="y\n")
    assert out.exit_code == 0 and "Create 6 lanes" in out.output
    assert "created: acme/instagram" in out.output and "done: every lane exists" in out.output
    assert len(lanes.created) == 6


def test_apply_declined_creates_nothing(home):
    _, lanes = home
    run("create", "acme")
    out = run("apply", "social-6", "-w", "acme", "--proxy", "acme=lt1", input_text="n\n")
    assert out.exit_code != 0 and lanes.created == []


def test_apply_with_yes_skips_the_question_and_is_idempotent(home):
    _, lanes = home
    run("create", "acme")
    run("apply", "social-6", "-w", "acme", "--proxy", "acme=lt1", "--yes")
    again = run("apply", "social-6", "-w", "acme", "--proxy", "acme=lt1", "--yes")
    assert again.exit_code == 0 and "Nothing to create: every lane already exists and matches." in again.output
    assert len(lanes.created) == 6


def test_apply_refused_on_conflict_says_nothing_was_created(home):
    _, lanes = home
    run("create", "acme")
    run("create", "beta")
    run("apply", "social-6", "-w", "acme", "--proxy", "acme=lt1", "--yes")
    before = len(lanes.created)
    out = run("apply", "social-6", "-w", "beta", "--proxy", "beta=lt1", "--yes")
    assert out.exit_code == 1 and "error: conflict: 6 conflicts, nothing was created" in out.output
    assert len(lanes.created) == before


def test_an_override_needs_a_reason_and_is_then_recorded(home):
    run("create", "acme")
    run("create", "beta")
    run("apply", "social-6", "-w", "acme", "--proxy", "acme=lt1", "--yes")
    no_reason = run("apply", "social-6", "-w", "beta", "--proxy", "beta=lt1", "--override-proxy-rules", "--yes")
    assert no_reason.exit_code == 1 and "needs a reason" in no_reason.output
    out = run("apply", "social-6", "-w", "beta", "--proxy", "beta=lt1", "--override-proxy-rules",
              "--reason", "same owner, two brands", "--yes")
    assert out.exit_code == 0 and "override recorded for beta/instagram" in out.output
    listed = run("overrides").output
    assert "same owner, two brands" in listed and "beta/" in listed


def test_a_partial_run_says_what_it_made_and_how_to_finish(home):
    _, lanes = home
    run("create", "acme")
    lanes.fail_on = {"acme/reddit"}
    out = run("apply", "social-6", "-w", "acme", "--proxy", "acme=lt1", "--yes")
    assert out.exit_code == 1
    assert "created: acme/instagram, acme/facebook" in out.output
    assert "stopped at acme/reddit (engine_error)" in out.output
    assert "still to do: acme/reddit, acme/medium, acme/blog, acme/linkedin. Run the same command again" in out.output
    lanes.fail_on = set()
    finish = run("apply", "social-6", "-w", "acme", "--proxy", "acme=lt1", "--yes")
    assert finish.exit_code == 0 and "created: acme/reddit, acme/medium, acme/blog, acme/linkedin" in finish.output


def test_lane_proxy_none_means_no_proxy_and_is_warned_about(home):
    run("create", "acme")
    out = run("plan", "social-6", "-w", "acme", "--lane-proxy", "acme/blog=none", "--proxy", "acme=lt1")
    blog = next(line for line in out.output.splitlines() if line.startswith("acme/blog"))
    assert " none " in blog


def test_bad_proxy_pairs_and_missing_workspaces_are_clear_errors(home):
    assert run("plan", "social-6").exit_code == 1
    out = run("plan", "social-6", "-w", "acme", "--proxy", "acme")
    assert out.exit_code == 1 and "--proxy takes NAME=LABEL" in out.output


def test_queue_ready_and_unready(home):
    _, lanes = home
    run("create", "acme")
    lanes.add("acme/reddit", proxy="lt1")
    queue = run("queue", "acme").output
    assert "acme: 0 of 1 ready" in queue and "acme/reddit" in queue and "needs_login" in queue
    assert "acme/reddit is ready" in run("ready", "acme/reddit").output
    assert "0 of 1" not in run("queue", "acme").output
    assert "acme/reddit is needs_attention" in run("unready", "acme/reddit", "--attention",
                                                    "--note", "phone code asked").output
    attention = run("queue", "acme").output
    assert "needs_attention" in attention and "phone code asked" in attention
    assert run("unready", "acme/reddit", "--attention").exit_code == 1  # no note, refused
    back = run("unready", "acme/reddit")
    assert back.exit_code == 0 and "acme/reddit is needs_login" in back.output


def test_readiness_of_a_lane_that_does_not_exist(home):
    out = run("ready", "acme/ghost")
    assert out.exit_code == 1 and out.output.strip().startswith("error: lane_not_found")


def test_queue_json_carries_login_urls(home):
    run("create", "acme")
    run("apply", "social-6", "-w", "acme", "--proxy", "acme=lt1", "--yes")
    data = json.loads(run("queue", "acme", "--json").output)
    by_name = {lane["name"]: lane for lane in data["lanes"]}
    assert by_name["instagram"]["login_url"] == "https://www.instagram.com/accounts/login/"
    assert by_name["blog"]["login_url"] is None
    assert data["total"] == 6 and data["ready"] == 0


def test_a_broken_node_config_is_a_clear_error_not_a_traceback(home):
    root, _ = home
    (root / "node.toml").write_text("this is = not [valid\n", encoding="utf-8")
    out = run("list")
    assert out.exit_code == 1 and "is not valid TOML" in out.output
