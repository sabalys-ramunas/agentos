"""A misspelt id or command in the owner's CLI names the close one before any "create it" hint."""

from __future__ import annotations

import sqlite3

import pytest
from typer.testing import CliRunner

from openajar.cli import app
from openajar.core.suggest import owner_message
from openajar.core.text import close_match

EM_DASH = "—"


def test_close_match_names_the_near_one_and_nothing_when_far():
    assert close_match("test/redit", ["personal/reddit", "test/reddit"]) == "test/reddit"
    assert close_match("tset", ["env", "personal", "test"]) == "test"
    assert close_match("zzz", ["env", "personal", "test"]) is None
    assert close_match("Test", ["test"]) == "test"
    assert close_match("test", ["test"]) is None  # the same id is not a suggestion


@pytest.fixture
def node_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "node.toml").write_text('name = "box"\n')
    with sqlite3.connect(home / "openajar.db") as conn:
        conn.execute("CREATE TABLE lanes (id TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO lanes VALUES (?)", [("personal/reddit",), ("test/reddit",)])
    with sqlite3.connect(home / "workspaces.db") as conn:
        conn.execute("CREATE TABLE workspaces (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO workspaces VALUES ('studio')")
    monkeypatch.setenv("OPENAJAR_HOME", str(home))
    return home


def test_owner_message_suggests_a_lane_and_a_workspace(node_home):
    lane = owner_message("lane_not_found", "no lane named test/redit. Create it with: openajar lane create test/redit")
    assert lane.startswith("no lane named test/redit. Did you mean test/reddit?") and "create" not in lane
    ws = owner_message("not_found", "no workspace 'tset'; create it with: openajar workspace create tset")
    assert "Did you mean 'test'?" in ws and "create it" not in ws
    assert "Did you mean 'studio'?" in owner_message("not_found", "no workspace 'studoi'")
    far = "no lane named qa/zzz. Create it with: openajar lane create qa/zzz"
    assert owner_message("lane_not_found", far) == far  # nothing close: the create hint stays
    other = "no lane named test/redit"
    assert owner_message("denied", other) == other


def test_owner_message_off_a_node_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path / "nothing"))
    msg = "no lane named test/redit. Create it with: openajar lane create test/redit"
    assert owner_message("lane_not_found", msg) == msg


@pytest.mark.parametrize(("args", "meant"), [
    (["lane", "show", "x"], "openajar lane check"),
    (["lane", "info"], "openajar lane check"),
    (["workspace", "info", "x"], "openajar workspace show"),
])
def test_unknown_detail_command_names_the_one_that_shows_one_thing(args, meant):
    result = CliRunner().invoke(app, args, prog_name="openajar", env={"COLUMNS": "200"})
    assert result.exit_code == 2
    assert f"Did you mean '{meant}'?" in " ".join(result.output.split()).replace("│", "")
    assert EM_DASH not in result.output


def test_a_close_spelling_keeps_typers_own_suggestion():
    result = CliRunner().invoke(app, ["lane", "lsit"])
    assert result.exit_code == 2 and "'list'" in result.output and "openajar lane check" not in result.output
