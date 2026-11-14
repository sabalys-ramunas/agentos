"""Routines and schedules on a node, as the app reads them (web/src/api/types.ts: `Flow`, `RoutineRun`,
`Run`, `RoutineCatalog`, `RoutineDay`, `Schedule`). A routine is a workflow, so each answer is the node's own.

A routine is turned on by a person, through the node's own route. A bearer token may stop one, never start one.
"""

from __future__ import annotations

TOKEN = "t" * 43
ROUTINE = {
    "name": "Remind me to check orders", "workspace": "biz-a", "trigger": {"kind": "manual"},
    "steps": [{"id": "tell", "kind": "notify", "label": "tell you", "message": "Check the orders"}],
}
WEEKDAYS = {
    "name": "Weekday morning note", "workspace": "biz-a",
    "trigger": {"kind": "schedule", "cron": "0 9 * * 1-5", "label": "every weekday at 09:00"},
    "steps": [{"id": "tell", "kind": "notify", "label": "tell you", "message": "Good morning"}],
}


def as_token(client) -> None:
    client.cookies.clear()
    client.headers["authorization"] = f"Bearer {TOKEN}"


def make(client, body: dict) -> dict:
    created = client.post("/api/orgs/local/flows", json=body)
    assert created.status_code == 200, created.text
    return created.json()


def turn_on(client, flow_id: str) -> None:
    """Turning a routine on is the node's own route, and only a person may use it."""
    on = client.post(f"/api/workflows/{flow_id}/enable", json={})
    assert on.status_code == 200, on.text


def test_the_routines_are_the_workflows_the_node_keeps(client) -> None:
    saved = make(client, ROUTINE)
    listed = client.get("/api/orgs/local/flows")
    assert listed.status_code == 200, listed.text
    [row] = [item for item in listed.json() if item["id"] == saved["id"]]
    assert row["name"] == "Remind me to check orders"
    assert row["lane"] == "biz-a/*"
    assert row["enabled"] is False  # saved is not on: nothing starts it until a person turns it on
    assert row["version"] == 1
    assert row["steps"][0]["kind"] == "notify"
    assert row["trigger"]["kind"] == "manual"
    assert row["ask_first"] is False
    assert row["latest"] is None and row["next_at"] is None


def test_a_routine_saves_a_change_and_keeps_its_id(client) -> None:
    saved = make(client, ROUTINE)
    changed = client.put(f"/api/orgs/local/flows/{saved['id']}/routine", json={**ROUTINE, "name": "Check the orders"})
    assert changed.status_code == 200, changed.text
    assert changed.json()["id"] == saved["id"]
    assert changed.json()["name"] == "Check the orders"
    assert changed.json()["version"] == 2


def test_a_routine_that_breaks_a_rule_is_refused_and_says_where(client) -> None:
    broken = {**ROUTINE, "trigger": {"kind": "telepathy"}}
    refused = client.post("/api/orgs/local/flows", json=broken)
    assert refused.status_code == 400, refused.text
    assert all(item["name"] != "Remind me to check orders" for item in client.get("/api/orgs/local/flows").json())


def test_a_routine_runs_by_hand_and_its_run_is_listed(client) -> None:
    saved = make(client, ROUTINE)
    started = client.post(f"/api/orgs/local/flows/{saved['id']}/run", json={"params": {}})
    assert started.status_code == 200, started.text
    run = started.json()
    assert run["flow"] == saved["id"]
    assert run["lane"] == "biz-a/*"
    assert run["principal"]["kind"] == "human"
    assert run["id"].startswith("run_")

    runs = client.get(f"/api/orgs/local/flows/{saved['id']}/runs")
    assert runs.status_code == 200, runs.text
    [listed] = runs.json()
    assert listed["id"] == run["id"]
    assert listed["by"] == "person"
    assert listed["cause"] == "Started by a person"
    assert listed["steps"][0]["kind"] == "trigger"
    assert listed["steps"][1]["kind"] == "notify"


def test_a_run_that_has_not_failed_cannot_be_retried(client) -> None:
    saved = make(client, ROUTINE)
    run = client.post(f"/api/orgs/local/flows/{saved['id']}/run", json={"params": {}}).json()
    refused = client.post(f"/api/orgs/local/flows/{saved['id']}/runs/{run['id']}/retry")
    assert refused.status_code == 400
    assert "only a failed or partial run" in refused.text


def test_running_a_routine_that_is_not_here_is_not_found(client) -> None:
    assert client.post("/api/orgs/local/flows/biz-a.routine.nothing/run", json={"params": {}}).status_code == 404
    assert client.get("/api/orgs/local/flows/biz-a.routine.nothing/runs").status_code == 404


def test_the_ask_first_switch_is_the_routines_own(client) -> None:
    saved = make(client, ROUTINE)
    on = client.put(f"/api/orgs/local/flows/{saved['id']}/ask-first", json={"ask_first": True})
    assert on.status_code == 200, on.text
    assert on.json()["ask_first"] is True
    assert on.json()["asks_first"] is True
    assert on.json()["asks_because"] == "routine"
    off = client.put(f"/api/orgs/local/flows/{saved['id']}/ask-first", json={"ask_first": False})
    assert off.status_code == 200, off.text
    assert off.json()["ask_first"] is False


def test_a_token_may_ask_first_on_but_not_let_a_routine_run_on_its_own(client) -> None:
    saved = make(client, ROUTINE)
    client.put(f"/api/orgs/local/flows/{saved['id']}/ask-first", json={"ask_first": True})
    as_token(client)
    refused = client.put(f"/api/orgs/local/flows/{saved['id']}/ask-first", json={"ask_first": False})
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "presence_required"  # a token is never a person (agentos.core.presence)
    still = client.get("/api/orgs/local/flows").json()
    assert [item["ask_first"] for item in still if item["id"] == saved["id"]] == [True]


def test_a_token_cannot_turn_a_routine_on(client) -> None:
    saved = make(client, WEEKDAYS)
    as_token(client)
    refused = client.post(f"/api/workflows/{saved['id']}/enable", json={})
    assert refused.status_code >= 400
    assert all(item["enabled"] is False for item in client.get("/api/orgs/local/flows").json()
               if item["id"] == saved["id"])


def test_the_catalog_says_what_a_routine_can_use(client) -> None:
    catalog = client.get("/api/orgs/local/routines/catalog")
    assert catalog.status_code == 200, catalog.text
    body = catalog.json()
    assert set(body) >= {"operations", "lists", "templates", "trigger_fields"}
    ids = {template["id"] for template in body["templates"]}
    assert ids >= {"invoice", "outreach", "post", "newsletter"}
    assert not ids & {"order", "webhook"}  # both wait for a webhook address, which this computer does not show yet
    assert {"schedule", "webhook", "email", "page_change", "manual"} <= set(body["trigger_fields"])


def test_a_computer_offers_only_the_ways_it_can_start_a_routine(client) -> None:
    """A webhook needs an address this computer does not show yet, so no template waits for one."""
    body = client.get("/api/orgs/local/routines/catalog").json()
    assert body["triggers"] == ["schedule", "email", "page_change", "manual"]
    assert {template["trigger"]["kind"] for template in body["templates"]} <= set(body["triggers"])
    assert "webhook" not in {template["id"] for template in body["templates"]} and body["templates"]


def test_a_template_that_sends_or_reads_mail_says_it_needs_a_mailbox(client) -> None:
    """Mail goes out from a mailbox connected on this computer, so a routine that sends or reads mail says so."""
    by_id = {template["id"]: template for template in client.get("/api/orgs/local/routines/catalog").json()["templates"]}
    assert by_id["to_list"]["needs"] == ["mailbox"]  # a step that sends mail
    assert by_id["reply"]["needs"] == ["mailbox"]  # starts from a mail
    assert by_id["invoice"]["needs"] == ["mailbox"]  # sends the reminder inside a loop
    assert "needs" not in by_id["post"]  # a post goes to a site, not by mail


def test_every_template_branch_has_both_arms_and_every_loop_a_body(client) -> None:
    """The app fills a template's arms as lists. A branch the node left one arm off reads as an empty arm."""

    def check(steps: list[dict]) -> None:
        for step in steps:
            if step["kind"] == "branch":
                assert isinstance(step["then"], list) and isinstance(step["otherwise"], list), step["id"]
                check(step["then"])
                check(step["otherwise"])
            if step["kind"] == "loop":
                assert isinstance(step["body"], list), step["id"]
                check(step["body"])

    body = client.get("/api/orgs/local/routines/catalog").json()
    for template in body["templates"]:
        check(template["steps"])


def test_the_day_counts_what_ran_and_what_the_cron_says_is_due(client) -> None:
    saved = make(client, ROUTINE)
    run = client.post(f"/api/orgs/local/flows/{saved['id']}/run", json={"params": {}}).json()
    day = client.get("/api/orgs/local/routines/day")
    assert day.status_code == 200, day.text
    body = day.json()
    assert body["ran_today"] >= 1
    assert body["checked_today"] == 0
    assert any(mark["flow"] == saved["id"] and mark["status"] is not None for mark in body["marks"])
    assert run["id"]


def test_a_routine_on_a_schedule_is_a_schedule_of_that_routine(client) -> None:
    saved = make(client, WEEKDAYS)
    turn_on(client, saved["id"])
    [schedule] = [item for item in client.get("/api/orgs/local/schedules").json() if item["flow"] == saved["id"]]
    assert schedule["cron"] == "0 9 * * 1-5"
    assert schedule["enabled"] is True
    assert schedule["next_fire_at"]
    assert schedule["next_run"]

    fetched = client.get(f"/api/orgs/local/schedules/{schedule['id']}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["flow"] == saved["id"]


def test_pausing_a_routine_schedule_pauses_the_routine(client) -> None:
    saved = make(client, WEEKDAYS)
    turn_on(client, saved["id"])
    [schedule] = [item for item in client.get("/api/orgs/local/schedules").json() if item["flow"] == saved["id"]]
    paused = client.patch(f"/api/orgs/local/schedules/{schedule['id']}", json={"enabled": False})
    assert paused.status_code == 200, paused.text
    assert paused.json()["enabled"] is False
    assert paused.json()["next_fire_at"] is None
    [row] = [item for item in client.get("/api/orgs/local/flows").json() if item["id"] == saved["id"]]
    assert row["enabled"] is False

    resumed = client.patch(f"/api/orgs/local/schedules/{schedule['id']}", json={"enabled": True})
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["enabled"] is True


def test_a_token_may_pause_a_routine_schedule_but_not_resume_it(client) -> None:
    saved = make(client, WEEKDAYS)
    turn_on(client, saved["id"])
    [schedule] = [item for item in client.get("/api/orgs/local/schedules").json() if item["flow"] == saved["id"]]
    as_token(client)
    refused = client.patch(f"/api/orgs/local/schedules/{schedule['id']}", json={"enabled": True})
    assert refused.status_code == 403
    paused = client.patch(f"/api/orgs/local/schedules/{schedule['id']}", json={"enabled": False})
    assert paused.status_code == 200, paused.text
    assert paused.json()["enabled"] is False


def test_deleting_a_routine_schedule_stops_it_and_keeps_the_routine(client) -> None:
    saved = make(client, WEEKDAYS)
    turn_on(client, saved["id"])
    [schedule] = [item for item in client.get("/api/orgs/local/schedules").json() if item["flow"] == saved["id"]]
    gone = client.delete(f"/api/orgs/local/schedules/{schedule['id']}")
    assert gone.status_code == 204, gone.text
    [row] = [item for item in client.get("/api/orgs/local/flows").json() if item["id"] == saved["id"]]
    assert row["enabled"] is False
    assert row["name"] == "Weekday morning note"


def test_a_schedule_that_is_not_a_routine_is_not_shown(client) -> None:
    assert client.get("/api/orgs/local/schedules/sch_nothing").status_code == 404


def test_a_token_cannot_start_a_schedule_of_a_plain_flow(client) -> None:
    params = {"caption": "A new blend", "image": "blend.jpg"}
    created = client.post("/api/orgs/local/schedules", json={"flow": "biz-a.social.post", "cron": "0 9 * * *",
                                                            "params": params})
    assert created.status_code in (200, 201), created.text
    body = created.json()
    assert body["flow"] == "biz-a.social.post"
    assert body["cron"] == "0 9 * * *"

    paused = client.patch(f"/api/orgs/local/schedules/{body['id']}", json={"enabled": False})
    assert paused.status_code == 200, paused.text
    as_token(client)
    assert client.patch(f"/api/orgs/local/schedules/{body['id']}", json={"enabled": True}).status_code == 403


def test_a_schedule_for_a_flow_that_is_not_there_is_refused(client) -> None:
    refused = client.post("/api/orgs/local/schedules", json={"flow": "biz-a.routine.nothing", "cron": "0 9 * * *",
                                                            "params": {}})
    assert refused.status_code >= 400


def test_this_computer_reports_its_schedules_and_no_other(client, node) -> None:
    saved = make(client, WEEKDAYS)
    turn_on(client, saved["id"])
    here = client.get(f"/api/orgs/local/nodes/{node.require('config').name}/schedules")
    assert here.status_code == 200, here.text
    [item] = [row for row in here.json() if row["flow"] == saved["id"]]
    assert item["recent_fires"] == []
    assert len(item["next_fires"]) == 3

    assert client.get("/api/orgs/local/nodes/elsewhere/schedules").status_code == 404


def test_a_routines_time_is_set_on_the_routine_not_in_a_schedule(client) -> None:
    saved = make(client, WEEKDAYS)
    refused = client.post("/api/orgs/local/schedules", json={"flow": saved["id"], "cron": "0 8 * * *", "params": {}})
    assert refused.status_code == 400
    assert "set on the routine itself" in refused.text


RECORDED = "biz-a.social.post"  # a recording the node keeps from the seed: no steps the app can draw, only its runs
RECORDED_VALUES = {"caption": "A new blend", "image": "blend.jpg"}


def listed_routine(client, flow_id: str) -> dict:
    [row] = [item for item in client.get("/api/orgs/local/flows").json() if item["id"] == flow_id]
    return row


def test_a_recorded_routine_is_listed_next_to_the_workflows(client) -> None:
    """B6: a recording the node keeps is a routine too, with the same shape, and nothing the app cannot show."""
    make(client, ROUTINE)
    row = listed_routine(client, RECORDED)
    assert row["name"] == "Post a photo"
    assert row["lane"] == "biz-a/social"
    assert row["version"].startswith("sha256:")  # what approvals bind to, so a standing approval can name it
    assert row["params"]["caption"] == {"type": "string", "label": "post text", "required": True}
    assert row["params"]["image"]["required"] is True
    assert "steps" not in row and "trigger" not in row and "enabled" not in row
    assert row["ask_first"] is False and row["asks_first"] is False
    assert row["latest"] is None or row["latest"]["result"]


def test_a_recorded_routine_runs_by_hand_and_its_runs_are_listed_with_its_result(client) -> None:
    started = client.post(f"/api/orgs/local/flows/{RECORDED}/run", json={"params": RECORDED_VALUES})
    assert started.status_code == 200, started.text
    run = started.json()
    assert run["flow"] == RECORDED and run["lane"] == "biz-a/social"
    assert run["id"].startswith("run_")

    runs = client.get(f"/api/orgs/local/flows/{RECORDED}/runs")
    assert runs.status_code == 200, runs.text
    [listed] = [item for item in runs.json() if item["id"] == run["id"]]
    assert listed["flow"] == RECORDED
    assert listed["by"] == "person"
    assert listed["cause"] == "Started by a person"
    assert listed["steps"] == [] and listed["output"] is None

    latest = listed_routine(client, RECORDED)["latest"]
    assert latest is not None and latest["status"] == run["status"]
    assert latest["result"]


def test_a_recorded_routine_run_counts_on_the_day_line(client) -> None:
    run = client.post(f"/api/orgs/local/flows/{RECORDED}/run", json={"params": RECORDED_VALUES}).json()
    body = client.get("/api/orgs/local/routines/day").json()
    assert body["ran_today"] >= 1
    assert any(mark["flow"] == RECORDED and mark["status"] == run["status"] for mark in body["marks"])


def test_a_recorded_routine_ask_first_reads_as_the_switch_is(client) -> None:
    """B7: the switch's answer says what the switch is, so the app's toast can match it."""
    on = client.put(f"/api/orgs/local/flows/{RECORDED}/ask-first", json={"ask_first": True})
    assert on.status_code == 200, on.text
    assert on.json()["ask_first"] is True and on.json()["asks_first"] is True
    assert on.json()["asks_because"] == "routine"
    off = client.put(f"/api/orgs/local/flows/{RECORDED}/ask-first", json={"ask_first": False})
    assert off.status_code == 200, off.text
    assert off.json()["ask_first"] is False and off.json()["asks_first"] is False
    assert listed_routine(client, RECORDED)["ask_first"] is False


def test_a_recorded_routine_that_is_not_there_is_not_found(client) -> None:
    assert client.put("/api/orgs/local/flows/biz-a.routine.nothing/ask-first", json={"ask_first": True}).status_code == 404


def test_a_saved_routine_is_turned_on_through_the_routine_route(client) -> None:
    saved = make(client, ROUTINE)
    on = client.post(f"/api/orgs/local/flows/{saved['id']}/enable", json={})
    assert on.status_code == 200, on.text
    assert on.json()["enabled"] is True
    assert listed_routine(client, saved["id"])["enabled"] is True


def test_a_token_cannot_turn_a_routine_on_through_the_routine_route(client) -> None:
    saved = make(client, ROUTINE)
    as_token(client)
    refused = client.post(f"/api/orgs/local/flows/{saved['id']}/enable", json={})
    assert refused.status_code >= 400, refused.text
    assert listed_routine(client, saved["id"])["enabled"] is False


def test_a_routines_own_schedule_is_named_by_it_and_on_only_while_it_is_on(client) -> None:
    """B9: a routine's own time shows under the routine, and it is on only while the routine is on."""
    saved = make(client, WEEKDAYS)
    turn_on(client, saved["id"])
    [schedule] = [item for item in client.get("/api/orgs/local/schedules").json() if item["flow"] == saved["id"]]
    assert schedule["routine"] is True and schedule["enabled"] is True
    off = client.post(f"/api/workflows/{saved['id']}/disable", json={})
    assert off.status_code == 200, off.text
    [now_off] = [item for item in client.get("/api/orgs/local/schedules").json() if item["id"] == schedule["id"]]
    assert now_off["enabled"] is False and now_off["next_fire_at"] is None
    assert now_off["routine"] is True


def test_a_recorded_routines_schedule_shows_its_next_run_on_the_routine(client) -> None:
    """B9 and B6: a schedule of a recording names the recording, and the routine says when it runs next."""
    created = client.post("/api/orgs/local/schedules", json={"flow": RECORDED, "cron": "0 9 * * *", "params": RECORDED_VALUES})
    assert created.status_code in (200, 201), created.text
    [item] = [row for row in client.get("/api/orgs/local/schedules").json() if row["id"] == created.json()["id"]]
    assert item["flow"] == RECORDED and "routine" not in item
    assert listed_routine(client, RECORDED)["next_at"] is not None
    client.delete(f"/api/orgs/local/schedules/{created.json()['id']}")


def test_a_schedule_with_no_routine_is_listed_and_a_routines_is_marked_as_one() -> None:
    """B9: the computer keeps a schedule that starts nothing a person knows, and the app still shows it, with no routine."""
    from agentos.api.web.schedules import owned, schedule_of

    plain = {"id": "sch_plain", "flow": None, "cron": None, "watch": '{"url": "x"}', "enabled": True}
    assert schedule_of(plain, "") == {"id": "sch_plain", "flow": "", "cron": None, "watch": '{"url": "x"}', "params": {},
                                      "deadman_minutes": None, "next_run": None, "enabled": True, "next_fire_at": None,
                                      "lane": None, "last_run": None}
    assert owned(plain, {}) is None
    owned_by = owned({**plain, "cron": "0 9 * * *", "next_fire_at": "2026-11-08T09:00:00+00:00"}, {"sch_plain": {"id": "biz-a.routine.x", "enabled": False}})
    assert owned_by["flow"] == "biz-a.routine.x" and owned_by["routine"] is True
    assert owned_by["enabled"] is False and owned_by["next_fire_at"] is None


def test_a_routine_saved_over_one_that_is_on_says_the_old_version_still_runs() -> None:
    """A member's save over a routine that is on is kept but not live: the app is told so, so it says so."""
    from agentos.api.web.routines import flow_of

    row = {"id": "biz-a.routine.x", "lane": "biz-a/*", "revision": 2, "params": {}, "effect": "commit", "last_run": None,
           "recent_runs": [], "ask_first": False, "always_asks_steps": [], "name": "X", "trigger": {"kind": "manual"},
           "steps": [], "sites": [], "enabled": True, "needs_review": True, "next_at": None, "latest": None, "stuck": None,
           "waiting": None, "send_limits": [], "steps_supported": True}
    assert flow_of(row)["needs_review"] is True
    assert flow_of({**row, "needs_review": False})["needs_review"] is False
