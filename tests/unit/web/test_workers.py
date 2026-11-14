"""Workers on a node, as the app reads them. Each answer is what the node really knows."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from agentos.api.web.workers import thing_of
from agentos.pilot.day import count
from agentos.pilot.models import Result, Status
from agentos.pilot.service import load_settings, save_settings
from agentos.pilot.trail import trail


def run(status: str, **fields) -> SimpleNamespace:
    return SimpleNamespace(id="run_1", status=status, lane="biz-a/social", output=fields.pop("output", None),
                           error=fields.pop("error", None), terminal=fields.pop("terminal", True),
                           params={"goal": "Post the photo", "max_steps": 40},
                           principal={"kind": "agent", "id": "claude-code"}, created_at=datetime.now(UTC),
                           started_at=None, finished_at=None)


def test_the_overview_says_what_a_new_node_has(client) -> None:
    answer = client.get("/api/orgs/local/workers")
    assert answer.status_code == 200, answer.text
    view = answer.json()
    assert view["plugin"] == "browser-worker"
    [place] = view["places"]
    assert place["node"] == "dev"
    assert place["worker"] == "needs_service"
    assert [row["job"] for row in view["jobs"]] == ["decide", "write", "rescue"]
    assert view["today"]["things"] == 0
    assert view["latest"] is None
    assert {item["kind"] for item in view["later"]} == {"android", "desktop"}


def test_a_token_cannot_turn_a_worker_on(client) -> None:
    client.cookies.clear()
    client.headers["authorization"] = "Bearer " + "t" * 43
    refused = client.post("/api/orgs/local/workers/computers/dev", json={"on": True})
    assert refused.status_code == 403


def test_a_token_cannot_give_the_worker_a_job(client) -> None:
    client.cookies.clear()
    client.headers["authorization"] = "Bearer " + "t" * 43
    refused = client.post("/api/orgs/local/workers/things", json={"lane": "biz-a/social", "told": "Post the photo"})
    assert refused.status_code == 403


def test_a_job_needs_the_worker_running(client) -> None:
    refused = client.post("/api/orgs/local/workers/things", json={"lane": "biz-a/social", "told": "Post the photo"})
    assert refused.status_code == 409
    assert "not running" in refused.json()["error"]["message"]


def test_a_job_says_what_to_do_on_an_account_that_exists(client) -> None:
    empty = client.post("/api/orgs/local/workers/things", json={"lane": "biz-a/social", "told": "   "})
    assert empty.status_code == 400
    gone = client.post("/api/orgs/local/workers/things", json={"lane": "biz-a/nothing", "told": "Post the photo"})
    assert gone.status_code == 404


def test_a_job_from_a_person_is_a_run_like_an_agents(client, node) -> None:
    class Worker:
        settings = load_settings(None)

        def __init__(self) -> None:
            self.checked: list[str] = []

        def check_worker(self, lane: str) -> None:
            self.checked.append(lane)

        def check_lane(self, lane: str) -> None:
            self.checked.append(lane)

        def live_rows(self, run_id: str) -> list[dict]:
            return []

    worker = Worker()
    node.register("pilot", worker)
    answer = client.post("/api/orgs/local/workers/things", json={"lane": "biz-a/social", "told": "  Post   the photo "})
    assert answer.status_code == 200, answer.text
    thing = answer.json()
    assert thing["told"] == "Post the photo"
    assert thing["said_by"]["kind"] == "human"
    assert worker.checked == ["biz-a/social", "biz-a/social"]
    run = node.require("runner").get(thing["run_id"])
    assert (str(run.kind), run.lane, run.source) == ("pilot", "biz-a/social", "panel")


def test_it_cannot_turn_on_with_nothing_to_decide(client) -> None:
    refused = client.post("/api/orgs/local/workers/computers/dev", json={"on": True})
    assert refused.status_code >= 400
    assert client.get("/api/orgs/local/workers").json()["places"][0]["worker"] == "needs_service"


def test_another_computer_is_not_here(client) -> None:
    assert client.get("/api/orgs/local/workers/computers/elsewhere").status_code == 404


def test_limits_change_and_stay(client, node) -> None:
    changed = client.post("/api/orgs/local/workers/limits", json={"steps_per_day": 90, "steps_per_thing": 12}).json()
    assert changed["limits"]["steps_per_day"] == 90
    assert changed["limits"]["steps_per_thing"] == 12
    home = node.require("config").home
    assert load_settings(home).steps_per_day == 90
    assert load_settings(home).max_steps == 12


def test_a_bad_limit_changes_nothing(client) -> None:
    assert client.post("/api/orgs/local/workers/limits", json={"steps_per_day": 0}).status_code >= 400
    assert client.get("/api/orgs/local/workers").json()["limits"]["steps_per_day"] == 500


def test_settings_keep_what_they_do_not_change(tmp_path) -> None:
    save_settings(tmp_path, {"max_steps": 10})
    assert save_settings(tmp_path, {"help_per_day": 3}).max_steps == 10


def test_a_step_says_what_it_did_and_never_a_value() -> None:
    rows = [{"n": 1, "op": "CLICK", "element": "Create", "ms": 90, "usd": 0.0002, "model": "jev"},
            {"n": 2, "op": "TYPE", "element": "Caption", "value": "v1"},
            {"n": 3, "op": "CLICK", "element": "Next", "rescued": True},
            {"n": 4, "op": "WRITE", "element": "Caption"}]
    steps = trail(rows)
    assert [step["kind"] for step in steps] == ["decide", "decide", "rescue", "write"]
    assert steps[0] == {"kind": "decide", "what": "Pressed Create", "model": "jev", "ms": 90, "usd": 0.0002}
    assert "v1" not in steps[1]["what"]


def test_the_trail_is_named_as_page_text() -> None:
    result = Result(status=Status.DONE_VERIFIED, rows=[{"op": "CLICK", "element": "Next"}]).public()
    assert result["trail"][0]["what"] == "Pressed Next"
    assert "trail" in result["untrusted_fields"]


def test_a_thing_is_a_run_in_the_apps_words() -> None:
    done = thing_of(run("succeeded_verified", output={"trail": [{"kind": "decide", "what": "Pressed Next", "model": None,
                                                                 "ms": 1, "usd": 0}]}), "dev", [], 25)
    assert (done["state"], done["told"], done["limit"]) == ("done", "Post the photo", 40)

    waiting = thing_of(run("waiting_approval", terminal=False), "dev", [{"op": "CLICK", "element": "Share"}], 25)
    assert waiting["state"] == "asked"
    assert [step["kind"] for step in waiting["steps"]] == ["decide", "yes"]

    held = thing_of(run("failed", output={"pilot_status": "held", "message": "A robot check came up"}), "dev", [], 25)
    assert (held["state"], held["stop"]) == ("stopped", "robot_check")
    assert held["steps"][-1] == {"kind": "you", "what": "A robot check came up", "model": None, "ms": 0, "usd": 0.0}


def test_a_day_counts_runs_and_what_is_still_going() -> None:
    finished = run("succeeded_verified", output={"steps": 4, "escalations": [{}], "spend": {"total_usd": 0.5}})
    going = run("running", terminal=False)
    going.id = "run_2"
    day = count([finished, going], {"run_2": 3})
    assert (day.things, day.steps, day.helped, day.longest, day.usd) == (2, 7, 1, 4, 0.5)
