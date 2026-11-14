"""This computer's health and backups, as the app reads them (agentos.api.routes_ops).

No test here starts a real backup or check. The runners are replaced by fakes that say what they were
asked, and the one real check reads a file that is not a backup, so it can only fail.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from agentos import __version__
from agentos.api import routes_ops

LOCAL = "/api/orgs/local/nodes/dev"
TOKEN = "t" * 43
NAME = "agentos-dev-20260101T000000Z.tar.gz.age"
PASSPHRASE = "a passphrase of twelve or more"


def as_token(client: TestClient) -> None:
    """The same client, now only holding the owner's bearer token: no cookie, no CSRF."""
    client.cookies.clear()
    client.headers.pop("x-agentos-csrf", None)
    client.headers["authorization"] = f"Bearer {TOKEN}"


def at_this_computer(client: TestClient) -> TestClient:
    """A client on this computer's own address, as the form is read by a person sitting at it."""
    return TestClient(client.app, base_url="http://localhost", client=("127.0.0.1", 50123))


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Fake runners. They record what they were asked, write what the real ones would, and finish when
    `release` is set (at once when it is not held)."""
    seen: dict[str, Any] = {"calls": [], "release": threading.Event(), "hold": False}

    def backup_run(home: Path, node: str, passphrase: str, job: Any, jobs: Any) -> None:
        seen["calls"].append(("backup", node, len(passphrase)))
        if seen["hold"]:
            seen["release"].wait(5)
        folder = Path(home) / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / NAME).write_bytes(b"not read here")
        routes_ops._write_check(folder, NAME, ok=True)
        jobs.finish(job, state="done", message=f"Written and checked: {NAME}.")

    def check_run(home: Path, name: str, passphrase: str, job: Any, jobs: Any) -> None:
        seen["calls"].append(("check", name, len(passphrase)))
        if seen["hold"]:
            seen["release"].wait(5)
        routes_ops._write_check(Path(home) / "backups", name, ok=True)
        jobs.finish(job, state="done", message=f"{name} checks out.")

    monkeypatch.setattr(routes_ops, "_run_backup", backup_run)
    monkeypatch.setattr(routes_ops, "_run_check", check_run)
    yield seen
    seen["release"].set()


def wait_until(client: TestClient, done: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    deadline = time.time() + 5
    while time.time() < deadline:
        view = client.get(f"{LOCAL}/backups").json()
        if done(view):
            return view
        time.sleep(0.02)
    raise AssertionError("the job never finished")


def finished(view: dict[str, Any]) -> bool:
    return view["job"] is not None and view["job"]["state"] != "running"


def trip_form(client: TestClient, trip: dict[str, Any]) -> str:
    assert trip["form_url"].startswith("/api/backups/form/"), trip
    return trip["form_url"]


# ---- health ---------------------------------------------------------------------------------

def test_health_names_a_fix_for_each_check_that_needs_one(client: TestClient) -> None:
    rows = client.get(f"{LOCAL}/health")
    assert rows.status_code == 200, rows.text
    checks = rows.json()
    assert checks, "a node always has checks to run"
    for row in checks:
        assert row["status"] in ("ok", "warn", "fail")
        assert row["id"] and "fix" in row
        if row["status"] != "ok":
            assert row["fix"], f"{row['name']} needs a way to fix it"


def test_a_full_check_runs_on_request_and_keeps_its_secrets(client: TestClient) -> None:
    ran = client.post(f"{LOCAL}/health/run")
    assert ran.status_code == 200, ran.text
    body = ran.json()
    assert body["ran_at"] and body["checks"]
    assert {row["status"] for row in body["checks"]} <= {"ok", "warn", "fail"}  # a skipped check is left out
    assert TOKEN not in ran.text


def test_the_daemon_round_says_nothing_when_no_daemon_answers(client: TestClient) -> None:
    rounds = client.get(f"{LOCAL}/health/rounds")
    assert rounds.status_code == 200
    assert rounds.json() is None


def test_the_version_is_the_running_one_and_no_newer_one_is_claimed(client: TestClient) -> None:
    body = client.get(f"{LOCAL}/upgrade").json()
    assert body["running"] == __version__
    assert body["newer"] is None, "this computer cannot see a newer release, so it must not say there is one"
    assert body["command"].startswith("agentos upgrade")


# ---- backups: who may start them, and the passphrase form -----------------------------------

def test_a_token_cannot_start_a_backup_or_check_one(client: TestClient, runs: dict[str, Any]) -> None:
    as_token(client)
    refused = client.post(f"{LOCAL}/backups")
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "denied"
    assert "A token cannot" in refused.json()["error"]["message"]
    assert client.post(f"{LOCAL}/backups/check", json={"name": NAME}).status_code == 403
    assert runs["calls"] == []


def test_a_backup_is_handed_to_a_form_the_node_serves(client: TestClient, runs: dict[str, Any]) -> None:
    trip = client.post(f"{LOCAL}/backups").json()
    assert trip["kind"] == "backup" and trip["stage"] == "waiting"
    page = at_this_computer(client).get(trip_form(client, trip))
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert "Make a backup" in page.text
    assert page.text.count('type="password"') == 2, "a backup asks twice, so a typo cannot lock it away"
    assert "Keep it safe" in page.text
    assert "<script" not in page.text
    assert runs["calls"] == []


def test_the_form_refuses_anyone_not_at_this_computer(client: TestClient, runs: dict[str, Any]) -> None:
    trip = client.post(f"{LOCAL}/backups").json()
    away = TestClient(client.app, base_url="http://localhost", client=("203.0.113.9", 50000))
    refused = away.get(trip_form(client, trip))
    assert refused.status_code == 403
    assert "Open this through the tunnel" in refused.text
    assert runs["calls"] == []


def test_the_passphrase_runs_one_backup_and_is_never_kept(client: TestClient, runs: dict[str, Any]) -> None:
    trip = client.post(f"{LOCAL}/backups").json()
    form = trip_form(client, trip)
    sent = at_this_computer(client).post(form, data={"value": PASSPHRASE, "again": PASSPHRASE})
    assert sent.status_code == 200, sent.text
    assert "Making the backup" in sent.text
    view = wait_until(client, finished)
    assert view["job"]["state"] == "done" and view["job"]["kind"] == "backup"
    assert runs["calls"] == [("backup", "dev", len(PASSPHRASE))]
    assert view["items"][0]["name"] == NAME
    assert view["items"][0]["check"]["state"] == "verified"
    assert view["keep"] == 7
    # the passphrase is in no answer and in no file the node wrote
    assert PASSPHRASE not in json.dumps(view)
    for path in (Path(client.app.state.api.config.home) / "backups").glob("*"):
        assert PASSPHRASE not in path.read_text(errors="ignore")
    # one form works once
    again = at_this_computer(client).post(form, data={"value": PASSPHRASE, "again": PASSPHRASE})
    assert again.status_code == 404 and "This link has ended" in again.text


def test_a_short_or_mismatched_passphrase_is_refused_and_the_form_stays_open(client: TestClient,
                                                                              runs: dict[str, Any]) -> None:
    trip = client.post(f"{LOCAL}/backups").json()
    form = trip_form(client, trip)
    short = at_this_computer(client).post(form, data={"value": "short", "again": "short"})
    assert short.status_code == 400 and "at least 12 characters" in short.text
    mismatch = at_this_computer(client).post(form, data={"value": PASSPHRASE, "again": PASSPHRASE + "x"})
    assert mismatch.status_code == 400 and "do not match" in mismatch.text
    assert runs["calls"] == [], "nothing runs on a refused passphrase"
    ok = at_this_computer(client).post(form, data={"value": PASSPHRASE, "again": PASSPHRASE})
    assert ok.status_code == 200
    wait_until(client, finished)


def test_a_second_backup_waits_for_the_first(client: TestClient, runs: dict[str, Any]) -> None:
    runs["hold"] = True
    trip = client.post(f"{LOCAL}/backups").json()
    at_this_computer(client).post(trip_form(client, trip), data={"value": PASSPHRASE, "again": PASSPHRASE})
    busy = client.post(f"{LOCAL}/backups")
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "busy"
    running = client.get(f"{LOCAL}/backups").json()["job"]
    assert running["state"] == "running"
    runs["release"].set()
    wait_until(client, finished)


# ---- backups: checking one ------------------------------------------------------------------

def test_checking_a_backup_names_a_real_file_only(client: TestClient, runs: dict[str, Any]) -> None:
    unknown = client.post(f"{LOCAL}/backups/check", json={"name": NAME})
    assert unknown.status_code == 404
    traversal = client.post(f"{LOCAL}/backups/check", json={"name": "../" + NAME})
    assert traversal.status_code == 404
    assert runs["calls"] == []


def test_a_check_records_the_result_and_the_list_shows_it(client: TestClient, runs: dict[str, Any]) -> None:
    folder = Path(client.app.state.api.config.home) / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / NAME).write_bytes(b"stored, not read here")
    listed = client.get(f"{LOCAL}/backups").json()
    assert listed["items"][0]["check"]["state"] == "not_checked"

    trip = client.post(f"{LOCAL}/backups/check", json={"name": NAME}).json()
    assert trip["kind"] == "check" and trip["name"] == NAME
    at_this_computer(client).post(trip_form(client, trip), data={"value": PASSPHRASE})
    view = wait_until(client, finished)
    assert view["job"]["state"] == "done"
    assert view["items"][0]["check"]["state"] == "verified" and view["items"][0]["check"]["at"]
    assert runs["calls"] == [("check", NAME, len(PASSPHRASE))]


def test_a_check_that_fails_says_so_and_keeps_the_file(client: TestClient) -> None:
    """The real check, on a file that is not a backup: it must fail, say so, and leave the file alone."""
    folder = Path(client.app.state.api.config.home) / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / NAME
    archive.write_bytes(b"not an age file at all")
    trip = client.post(f"{LOCAL}/backups/check", json={"name": NAME}).json()
    at_this_computer(client).post(trip_form(client, trip), data={"value": PASSPHRASE})
    view = wait_until(client, finished)
    assert view["job"]["state"] == "failed"
    check = view["items"][0]["check"]
    assert check["state"] == "failed" and check["error"]
    assert archive.read_bytes() == b"not an age file at all"


def test_the_check_file_holds_names_and_times_only(client: TestClient) -> None:
    folder = Path(client.app.state.api.config.home) / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    routes_ops._write_check(folder, NAME, ok=False, error="wrong passphrase")
    rows = json.loads((folder / routes_ops.CHECKS_FILE).read_text())
    assert set(rows[NAME]) == {"at", "ok", "error"} and rows[NAME]["ok"] is False
    assert PASSPHRASE not in (folder / routes_ops.CHECKS_FILE).read_text()


def test_a_message_from_a_failure_is_the_nodes_own_words(client: TestClient) -> None:
    from agentos.core.errors import AgentosError

    assert routes_ops._said(AgentosError("the passphrase did not open it"), "fallback") == "the passphrase did not open it"
    assert routes_ops._said(ValueError("/home/secret/path"), "The backup could not be read.") == "The backup could not be read."
