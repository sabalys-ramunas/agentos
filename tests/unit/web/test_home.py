"""Home's summary and While you slept, as the app reads them (web/src/features/home/summary.ts and
web/src/api/types.ts `TopologyNight`). Each test writes a few real runs and calls through the node's own stores,
then checks what the two routes say about them. The rules that turn a lane into a problem are checked on their own.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from starlette.testclient import TestClient

from agentos.api.auth import SESSION_COOKIE
from agentos.api.members import Role
from agentos.api.web import home, topology
from agentos.core.models import RunStatus
from agentos.policy.traces import Cost, TraceEvent
from agentos.runner.models import Run

ORG = "/api/orgs/local"
ANA = "m_ana0001"
HOME_KEYS = {"timezone", "date", "needs_you", "all_fine", "sentence", "problems", "today", "coming_up", "ai_usage"}
TODAY_KEYS = {"done", "on_its_own", "went_out_on_its_own", "checked", "not_checked", "by_kind", "chips",
              "working_now", "line", "pictures"}
NIGHT_KEYS = {"timezone", "from", "to", "label", "over", "actions", "accounts", "failed", "posts", "outages",
              "stopped", "hours", "sentence"}


def _run(node, run_id: str, status: str, *, lane: str = "biz-a/social", minutes_ago: int = 5, error=None) -> None:
    started = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    finished = None if status == "running" else started + timedelta(seconds=30)
    run = Run(id=run_id, lane=lane, flow_id="biz-a.social.post", status=RunStatus(status), started_at=started,
              finished_at=finished, error=error, source="panel",
              principal={"kind": "agent", "id": "claude-code", "thread": "T-1"})
    assert node.get("runner").store.insert_run(run)[1] is True


def _call(node, tool: str, status: str, *, lane: str = "biz-a/shop", effect: str = "read", usd: float = 0.0,
          summary: str = "looked") -> None:
    node.get("traces").write(TraceEvent(kind="tool_end", principal="claude-code", principal_kind="agent", tool=tool,
                                        lane=lane, workspace=lane.split("/", 1)[0], effect=effect, status=status,
                                        summary=summary, cost=Cost(tokens=0, usd=usd)))


def _writing_at(node, moment: datetime):
    """Write the events that follow at `moment`: the node's trace store takes its clock from here."""
    traces = node.get("traces")
    old = traces.clock
    traces.clock = lambda: moment.timestamp()
    return old


def _zone(client) -> ZoneInfo:
    return ZoneInfo(client.get(f"{ORG}/home").json()["timezone"])


# ---- Home ----


def test_home_is_the_summary_the_app_reads(client) -> None:
    body = client.get(f"{ORG}/home")
    assert body.status_code == 200, body.text
    body = body.json()
    assert set(body) == HOME_KEYS
    assert set(body["today"]) == TODAY_KEYS
    assert set(body["sentence"]) == {"lead", "rest"}
    assert body["needs_you"] == client.get(f"{ORG}/status").json()["needs_you"]
    assert body["all_fine"] is (body["problems"] == [])
    assert date.fromisoformat(body["date"]) <= date.today() + timedelta(days=1)


def test_home_counts_what_ran_today_and_what_was_checked(client, node) -> None:
    _run(node, "run_checked", "succeeded_verified", minutes_ago=3)
    _run(node, "run_unchecked", "succeeded_unverified", minutes_ago=4)
    today = client.get(f"{ORG}/home").json()["today"]
    assert today["done"] == 2 and today["checked"] == 1 and today["not_checked"] == 1
    assert today["on_its_own"] == 2
    assert today["by_kind"]["posts"] == 2
    assert "2 posts published" in today["chips"]
    # Nothing urgent waits, so Home leads with what ran on its own, and the line says how sure we are.
    assert today["line"] == "One of them was checked on the real site afterwards."


def test_a_day_with_nothing_finished_says_so(client) -> None:
    today = client.get(f"{ORG}/home").json()["today"]
    assert today["done"] == 0
    assert today["line"] == "Nothing has finished here today."


def test_a_run_still_going_is_working_now(client, node) -> None:
    _run(node, "run_now", "running", minutes_ago=1)
    today = client.get(f"{ORG}/home").json()["today"]
    assert today["working_now"] == 1
    assert "One is working right now." in today["line"]
    assert today["done"] == 0


def test_a_member_does_not_see_the_spend(client) -> None:
    api = client.app.state.api
    api.members.store.create_member(member_id=ANA, handle="ana", display_name="Ana", email="ana@example.com",
                                    timezone="UTC", by="owner", roles=[Role(scope="biz-a", role="operator", options={}, expires_at=None)],
                                    member_ttl_s=None)
    api.members.sync(ANA, by="owner", force=True)
    sid, _ = api.store.create_session(ANA, 3600, member_id=ANA, via="code")
    member = TestClient(client.app, base_url="http://localhost")
    member.cookies.set(SESSION_COOKIE, sid)
    body = member.get(f"{ORG}/home")
    assert body.status_code == 200, body.text
    assert body.json()["ai_usage"] is None
    assert client.get(f"{ORG}/home").json()["ai_usage"] is not None  # the owner, on the same node, does


def test_the_owner_sees_what_the_model_cost_today(client, node) -> None:
    _call(node, "browser_read", "ok", usd=0.5)
    usage = client.get(f"{ORG}/home").json()["ai_usage"]
    assert usage["today_usd"] == 0.5
    assert usage["usual_usd"] is None  # no two weeks of spend yet, so no usual day to compare with
    assert usage["label"] == "No usual day to compare with yet."
    assert usage["unusual"] is False


def test_coming_up_names_the_next_routine_on_a_schedule(client) -> None:
    saved = client.post(f"{ORG}/flows", json={
        "name": "Weekday morning note", "workspace": "biz-a",
        "trigger": {"kind": "schedule", "cron": "0 9 * * 1-5", "label": "every weekday at 09:00"},
        "steps": [{"id": "tell", "kind": "notify", "label": "tell you", "message": "Good morning"}],
    }).json()
    on = client.post(f"/api/workflows/{saved['id']}/enable", json={})
    assert on.status_code == 200, on.text
    [up] = [item for item in client.get(f"{ORG}/home").json()["coming_up"] if item["flow"] == saved["id"]]
    assert up["title"] == "Weekday morning note"
    assert up["workspace_name"] == "Biz A"
    assert up["sentence"].startswith("Biz A: Weekday morning note · ")
    assert " at 09:00" in up["when"] and up["at"]


def test_a_stuck_account_and_an_offline_computer_are_problems() -> None:
    lanes = [{"id": "biz-a/shop", "workspace": "biz-a", "node": "dev", "state": "quarantined", "signal": "ok",
              "logged_in_hint": "unknown"}]
    workspaces = [{"id": "biz-a", "name": "Biz A", "lanes": lanes}]
    [problem] = home.stuck_accounts(workspaces)
    assert problem["kind"] == "account_stuck" and problem["lane"] == "biz-a/shop" and problem["state"] == "quarantined"
    assert problem["lead"].endswith("is switched off for safety.")
    status = {"nodes": [{"id": "dev", "state": "down"}]}
    [offline] = home.offline_computers(status, workspaces)
    assert offline["kind"] == "computer_offline" and offline["node"] == "dev"
    assert offline["lead"] == "dev is offline." and offline["rest"] == "Biz A's accounts are paused until it is back."
    assert home.offline_computers({"nodes": [{"id": "dev", "state": "up"}]}, workspaces) == []


def test_only_a_lane_that_will_not_work_is_stuck() -> None:
    assert home.account_state({"state": "warm"}) == "ok"
    assert home.account_state({"state": "cold"}) == "asleep"
    assert home.account_state({"state": "active"}) == "working"
    assert home.account_state({"state": "warm", "signal": "captcha"}) == "captcha"
    assert home.account_state({"state": "warm", "logged_in_hint": "needs_login"}) == "logged_out"
    assert home.face_of("captcha") == "waiting" and home.face_of("blocked") == "stuck"
    assert home.face_of("asleep") == "all_good"


def test_a_failed_check_is_stuck_whatever_the_signal_says() -> None:
    # The account page's own rule: its last check failed (safe false, or level fail). A warning is not a stop.
    assert home.account_state({"state": "warm", "check": {"level": "fail", "safe": False}}) == "not_safe"
    assert home.account_state({"state": "active", "check": {"level": "fail", "safe": False}}) == "not_safe"
    assert home.account_state({"state": "warm", "signal": "captcha", "check": {"level": "fail", "safe": False}}) == "not_safe"
    assert home.account_state({"state": "warm", "check": {"level": "warn", "safe": True}}) == "ok"
    assert home.account_state({"state": "warm", "check": None}) == "ok"
    assert home.face_of("not_safe") == "stuck"


def test_a_frozen_account_is_stuck_and_says_so() -> None:
    assert home.account_state({"state": "warm", "frozen": True}) == "frozen"
    assert home.account_state({"state": "active", "frozen": True}) == "frozen"
    assert home.account_state({"state": "warm", "frozen": False}) == "ok"
    assert home.face_of("frozen") == "stuck"


def test_a_failed_check_is_a_problem_and_home_does_not_say_all_fine() -> None:
    lanes = [{"id": "biz-a/shop", "workspace": "biz-a", "state": "warm", "signal": "ok", "logged_in_hint": "unknown",
              "check": {"level": "fail", "safe": False}}]
    [problem] = home.stuck_accounts([{"id": "biz-a", "name": "Biz A", "lanes": lanes}])
    assert problem["state"] == "not_safe"
    assert problem["lead"].endswith("is not safe to use right now.")
    # Nothing needs a yes, so the sentence leads with the problem, never with "All good".
    assert home.hero_sentence(0, {"accounts": 1}, [problem], 0) == (problem["lead"], "Nothing needs you.")
    # A yes waits as well: the problem is what the second line names, not "Everything else is fine".
    assert home.hero_sentence(1, {"accounts": 1}, [problem], 0) == ("One thing needs you.", problem["lead"])


def test_a_frozen_account_is_a_problem_beside_a_yes() -> None:
    lanes = [{"id": "biz-a/shop", "workspace": "biz-a", "state": "warm", "signal": "ok", "logged_in_hint": "unknown",
              "frozen": True}]
    [problem] = home.stuck_accounts([{"id": "biz-a", "name": "Biz A", "lanes": lanes}])
    assert problem["state"] == "frozen" and problem["lead"].endswith("is frozen.")


def test_home_reads_each_accounts_own_check_as_the_account_page_does() -> None:
    workspaces = [{"id": "biz-a", "name": "Biz A", "lanes": [{"id": "biz-a/shop", "state": "warm"}, {"id": "biz-a/social", "state": "warm"}]}]
    checks = {"biz-a/shop": {"level": "fail", "safe": False}}
    [ws] = home.with_checks(workspaces, checks)
    assert [lane["check"] for lane in ws["lanes"]] == [{"level": "fail", "safe": False}, None]
    assert workspaces[0]["lanes"][0].get("check") is None  # the rows it was given are not changed


def test_the_sentence_leads_with_what_is_urgent() -> None:
    assert home.hero_sentence(0, {}, [], 0) == ("All good.", "Nothing needs you.")
    assert home.hero_sentence(2, {}, [], 3) == ("Your agents did 3 things on their own today.", "Two need you.")
    offline = [{"kind": "computer_offline", "lead": "dev is offline.", "rest": "Its accounts are paused."}]
    assert home.hero_sentence(0, {}, offline, 3) == ("dev is offline.", "Its accounts are paused.")
    assert home.hero_sentence(1, {"failures": 1}, [], 3) == ("One thing needs you.", "Everything else is fine.")


def test_the_usual_day_says_how_today_compares() -> None:
    assert home.usage_words(0.0, None)["label"] == "No AI usage yet today."
    assert home.usage_words(2.0, None)["label"] == "No usual day to compare with yet."
    same = home.usage_words(1.0, 1.0)
    assert same["label"] == "AI usage today: $1.00, about the same as a usual day." and same["ratio"] == 1.0
    much = home.usage_words(3.0, 1.0)
    assert much["unusual"] is True and much["label"] == "AI usage today: $3.00, 3 times a usual day."


# ---- While you slept ----


def test_a_night_is_eleven_to_seven_in_the_computers_own_time() -> None:
    zone = ZoneInfo("Europe/Vilnius")
    start, end = topology.night_window(datetime(2026, 11, 7, 12, tzinfo=UTC), zone, date(2026, 11, 7))
    assert start == datetime(2026, 11, 6, 21, 0, tzinfo=UTC)  # 23:00 Vilnius, the evening before
    assert end == datetime(2026, 11, 7, 5, 0, tzinfo=UTC)  # 07:00 Vilnius


def test_the_night_counts_what_ran_and_what_failed(client, node) -> None:
    zone = _zone(client)
    morning = date.today() - timedelta(days=10)
    last_night = datetime.combine(morning - timedelta(days=1), time(23, 30), tzinfo=zone)
    old = _writing_at(node, last_night.astimezone(UTC))
    try:
        _call(node, "browser_read", "ok", lane="biz-a/shop")
        _call(node, "browser_read", "ok", lane="biz-a/shop")
        _call(node, "browser_act", "error", lane="biz-a/social", effect="draft", summary="The page changed")
    finally:
        node.get("traces").clock = old
    body = client.get(f"{ORG}/topology/night", params={"morning": morning.isoformat()})
    assert body.status_code == 200, body.text
    body = body.json()
    assert set(body) == NIGHT_KEYS
    assert body["label"] == "11 pm to 7 am" and body["over"] is True
    assert body["actions"] == 3 and body["accounts"] == 2 and body["failed"] == 1
    assert body["outages"] == []  # a node keeps no record of its own outages, so it says none
    assert sum(hour["actions"] for hour in body["hours"]) == 3
    assert body["sentence"].startswith("While you slept, 11 pm to 7 am: 3 actions on 2 accounts.")


def test_a_night_with_nothing_in_it_says_so(client) -> None:
    body = client.get(f"{ORG}/topology/night", params={"morning": "2020-01-02"}).json()
    assert body["actions"] == 0 and body["accounts"] == 0 and body["hours"] == []
    assert body["sentence"] == "While you slept, 11 pm to 7 am: 0 actions on 0 accounts."


def test_a_night_that_has_not_happened_is_refused(client) -> None:
    later = (date.today() + timedelta(days=5)).isoformat()
    refused = client.get(f"{ORG}/topology/night", params={"morning": later})
    assert refused.status_code == 400
    assert "has not happened yet" in refused.text
    assert client.get(f"{ORG}/topology/night", params={"morning": "not-a-day"}).status_code == 400
