"""What happened on a node, as the app reads it: Activity, the audit list, runs (with cancel and decide), and
History as stories. The node's own stores hold the facts (runs in the runner, calls in the action log); each
test writes a few real ones and checks what the routes say about them."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from openajar.core.models import RunStatus
from openajar.policy.traces import TraceEvent
from openajar.runner.models import Run

ORG = "/api/orgs/local"


def _run(node, run_id: str, status: str, *, lane: str = "biz-a/social", minutes_ago: int = 5, error=None,
         principal=None, source: str = "panel") -> None:
    started = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    finished = None if status == "running" else started + timedelta(seconds=30)
    run = Run(id=run_id, lane=lane, flow_id="biz-a.social.post", status=RunStatus(status), started_at=started,
              finished_at=finished, error=error, source=source,
              principal=principal if principal is not None else {"kind": "agent", "id": "claude-code", "thread": "T-1"})
    assert node.get("runner").store.insert_run(run)[1] is True


def _call(node, tool: str, status: str, *, lane: str = "biz-a/shop", effect: str = "read", summary: str = "looked") -> None:
    node.get("traces").write(TraceEvent(kind="tool_end", principal="claude-code", principal_kind="agent", tool=tool,
                                        lane=lane, workspace=lane.split("/", 1)[0], effect=effect, status=status,
                                        summary=summary))


# ---- Activity: the action log, as trace events ----

def test_activity_is_a_page_of_trace_events_in_the_app_shape(client, node) -> None:
    _call(node, "browser_act", "error", effect="draft", summary="The page changed, so the selector was missing")
    _call(node, "browser_read", "ok")
    page = client.get(f"{ORG}/activity").json()
    assert set(page) == {"items", "next_before"}
    first = page["items"][0]
    assert {"id", "at", "kind", "tool", "lane", "principal", "effect", "status", "summary", "cost", "run_id"} <= set(first)
    assert first["principal"] == {"kind": "agent", "id": "claude-code", "thread": None}
    statuses = {item["tool"]: item["status"] for item in page["items"]}
    assert statuses["browser_act"] == "failed" and statuses["browser_read"] == "succeeded_unverified"


def test_activity_filters_and_pages_by_trace(client, node) -> None:
    for index in range(3):
        _call(node, "browser_read", "ok", summary=f"look {index}")
    _call(node, "browser_act", "ok", lane="biz-a/other", effect="commit")
    only = client.get(f"{ORG}/activity", params={"lane": "biz-a/other"}).json()["items"]
    assert [item["lane"] for item in only] == ["biz-a/other"]
    one = client.get(f"{ORG}/activity", params={"limit": 2}).json()
    assert len(one["items"]) == 2 and one["next_before"] == one["items"][-1]["id"]
    rest = client.get(f"{ORG}/activity", params={"limit": 2, "before": one["next_before"]}).json()
    assert len(rest["items"]) == 2 and rest["next_before"] is None
    assert not {i["id"] for i in one["items"]} & {i["id"] for i in rest["items"]}


def test_activity_refuses_a_limit_that_is_not_a_number(client) -> None:
    refused = client.get(f"{ORG}/activity", params={"limit": "many"})
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "invalid"


def test_the_activity_export_is_a_csv_or_ndjson_file_of_the_same_log(client, node) -> None:
    _call(node, "browser_act", "ok", effect="commit")
    csv_reply = client.get(f"{ORG}/activity/export", params={"format": "csv"})
    assert csv_reply.status_code == 200
    assert csv_reply.headers["content-disposition"].startswith('attachment; filename="openajar-activity-')
    lines = csv_reply.text.splitlines()
    assert lines[0] == "id,at,kind,tool,lane,workspace,principal,effect,status,summary,run_id"
    assert "browser_act" in lines[1]
    ndjson = client.get(f"{ORG}/activity/export", params={"format": "ndjson"})
    assert json.loads(ndjson.text.splitlines()[0])["tool"] == "browser_act"
    refused = client.get(f"{ORG}/activity/export", params={"format": "xlsx"})
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "invalid"


def test_the_activity_histogram_counts_what_happened_in_each_bucket(client, node) -> None:
    _run(node, "run_fail", "failed", error={"message": "captcha shown"}, minutes_ago=2)
    _run(node, "run_ok", "succeeded_verified", minutes_ago=3)
    _call(node, "browser_act", "ok", effect="commit")
    chart = client.get(f"{ORG}/metrics/activity", params={"range": "24h"}).json()
    assert chart["range"] == "24h" and chart["bucket_seconds"] == 3600 and len(chart["buckets"]) == 24
    assert chart["totals"]["events"] >= 1 and chart["totals"]["runs"] == 2
    seen = [b for b in chart["buckets"] if b["runs"]["failed"] or b["runs"]["succeeded_verified"] or b["events"]]
    assert seen, "the things done now fall in the last bucket"
    assert sum(b["runs"]["failed"] for b in chart["buckets"]) == 1
    assert sum(b["runs"]["succeeded_verified"] for b in chart["buckets"]) == 1
    assert all(b["at"].endswith("Z") for b in chart["buckets"])


def test_the_histogram_refuses_a_range_it_does_not_draw(client) -> None:
    refused = client.get(f"{ORG}/metrics/activity", params={"range": "1y"})
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "invalid"


def test_a_lane_histogram_leaves_out_the_other_accounts(client, node) -> None:
    _call(node, "browser_read", "ok", lane="biz-a/shop")
    _call(node, "browser_read", "ok", lane="biz-a/other")
    only = client.get(f"{ORG}/metrics/activity", params={"range": "7d", "lane": "biz-a/other"}).json()
    assert only["totals"]["events"] == 1


# ---- the audit list: who did what to what, from the same log ----

def test_the_audit_list_says_who_did_what_and_to_what(client, node) -> None:
    _call(node, "browser_act", "ok", effect="commit")
    page = client.get(f"{ORG}/audit").json()
    item = page["items"][0]
    assert set(item) == {"id", "at", "actor", "action", "target", "ip"}
    assert item["actor"] == "Claude Code" and item["target"] == "biz-a/shop" and item["ip"] is None
    assert item["action"].endswith("browser_act")


# ---- runs ----

def test_runs_are_listed_and_read_with_their_honest_status(client, node) -> None:
    _run(node, "run_fail", "failed", error={"message": "captcha shown on the page"})
    _run(node, "run_ok", "succeeded_verified", minutes_ago=9)
    listed = client.get(f"{ORG}/runs").json()
    by_id = {row["id"]: row for row in listed}
    assert by_id["run_fail"]["status"] == "failed" and by_id["run_ok"]["status"] == "succeeded_verified"
    assert by_id["run_fail"]["principal"] == {"kind": "agent", "id": "claude-code", "thread": "T-1"}
    only = client.get(f"{ORG}/runs", params={"status": "failed"}).json()
    assert [row["id"] for row in only] == ["run_fail"]
    one = client.get(f"{ORG}/runs/run_ok").json()
    assert one["id"] == "run_ok" and isinstance(one["steps"], list)


def test_cancelling_a_finished_run_changes_nothing_and_says_so(client, node) -> None:
    _run(node, "run_done", "succeeded_verified")
    answered = client.post(f"{ORG}/runs/run_done/cancel")
    assert answered.status_code == 200, answered.text
    assert answered.json()["id"] == "run_done" and answered.json()["status"] == "succeeded_verified"


def test_a_run_decision_is_one_of_three_words(client, node) -> None:
    _run(node, "run_partial", "partial")
    refused = client.post(f"{ORG}/runs/run_partial/decide", json={"decision": "maybe"})
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "invalid"


def test_a_run_decision_goes_to_the_node_which_keeps_the_rules(client, node) -> None:
    _run(node, "run_done", "succeeded_verified")
    refused = client.post(f"{ORG}/runs/run_done/decide", json={"decision": "retry"})
    assert refused.status_code >= 400
    assert refused.json()["error"]


# ---- History: stories, the day they fall on, and what the person can filter by ----

def test_history_tells_a_failed_run_and_a_failed_call_as_problems(client, node) -> None:
    _run(node, "run_fail", "failed", error={"message": "captcha shown on the page"})
    _call(node, "browser_act", "error", effect="draft", summary="The page changed, so the selector was missing")
    page = client.get(f"{ORG}/history", params={"days": 2}).json()
    assert page["timezone"] and page["retention_days"] == 90
    assert page["counts"]["everything"] == 2 and page["counts"]["problems"] == 2
    [today] = [day for day in page["days"] if day["label"] == "Today"]
    assert today["problems"] == 2 and today["done"] == 0
    stories = {story["id"]: story for story in today["stories"]}
    run = stories["run_fail"]
    assert run["source"] == "run" and run["verdict"] == "failed" and run["place"] == "Biz A's Social"
    assert run["sentence"] == "Could not post on Biz A's Social"
    assert run["reason"] == "The site asked for proof that a real person was there."
    assert run["who"]["name"] == "Claude Code" and run["why"]["text"] == "Started here"
    call = stories[next(i for i in stories if i != "run_fail")]
    assert call["source"] == "call" and call["reason"] == "The site did not look as expected, so the agent stopped."


def test_history_folds_routine_reads_into_one_line_a_day(client, node) -> None:
    _call(node, "browser_read", "ok", summary="looked")
    _call(node, "browser_read", "ok", summary="looked again")
    page = client.get(f"{ORG}/history", params={"days": 2}).json()
    [today] = [day for day in page["days"] if day["label"] == "Today"]
    assert today["stories"] == []
    assert today["reads"]["times"] == 2 and today["reads"]["accounts"] == 1
    assert today["reads"]["sentence"] == "Also checked 1 account 2 times."


def test_history_filters_show_only_what_they_name(client, node) -> None:
    _run(node, "run_fail", "failed", error={"message": "captcha shown"})
    _run(node, "run_ok", "succeeded_verified", minutes_ago=8)
    problems = client.get(f"{ORG}/history", params={"days": 2, "show": "problems"}).json()
    assert [s["id"] for d in problems["days"] for s in d["stories"]] == ["run_fail"]
    posts = client.get(f"{ORG}/history", params={"days": 2, "show": "posts"}).json()
    assert {s["id"] for d in posts["days"] for s in d["stories"]} == {"run_fail", "run_ok"}
    spending = client.get(f"{ORG}/history", params={"days": 2, "show": "spending"}).json()
    assert spending["days"] == [] and spending["counts"]["spending"] == 0


def test_a_run_in_progress_is_working_now_and_not_yet_a_story(client, node) -> None:
    _run(node, "run_now", "running", minutes_ago=1)
    page = client.get(f"{ORG}/history", params={"days": 2}).json()
    assert [s["id"] for s in page["working_now"]] == ["run_now"]
    assert page["working_now"][0]["headline"].startswith("Claude Code is ")
    assert all(s["id"] != "run_now" for d in page["days"] for s in d["stories"])


def test_history_refuses_a_day_it_cannot_read(client) -> None:
    refused = client.get(f"{ORG}/history", params={"before": "last tuesday"})
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "invalid"


def test_history_refuses_a_show_it_does_not_have(client) -> None:
    refused = client.get(f"{ORG}/history", params={"show": "everything-else"})
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "invalid"


def test_history_pages_back_by_day(client, node) -> None:
    _call(node, "browser_act", "ok", effect="commit")
    page = client.get(f"{ORG}/history", params={"days": 1}).json()
    assert page["next_before"] and page["days"][0]["label"] == "Today"
    older = client.get(f"{ORG}/history", params={"days": 1, "before": page["next_before"]}).json()
    assert all(day["date"] < page["days"][0]["date"] for day in older["days"])
    assert older["working_now"] == []


def _today_story(client, run_id: str) -> dict:
    page = client.get(f"{ORG}/history", params={"days": 2}).json()
    [today] = [day for day in page["days"] if day["label"] == "Today"]
    return {story["id"]: story for story in today["stories"]}[run_id]


def test_a_routine_run_is_told_by_the_routines_name_and_how_it_started(client, node) -> None:
    """C7: the routine is named, not by its id, and the story says it ran on its schedule."""
    _run(node, "run_sched", "succeeded_verified", principal={"kind": "flow", "id": "schedule:sch_1"}, source="cron:sch_1")
    story = _today_story(client, "run_sched")
    assert story["who"]["name"] == "Post a photo"
    assert story["why"]["text"] == "On its schedule"
    assert story["why"]["on_its_own"] is True


def test_a_run_started_by_hand_never_says_on_its_own(client, node) -> None:
    """C7: a run a person started from the panel is not "on its own", whether or not it had to be approved."""
    _run(node, "run_hand", "succeeded_verified", principal={"kind": "human", "id": "owner"}, source="panel")
    _run(node, "run_hand_routine", "succeeded_verified", principal={"kind": "human", "id": "owner"}, source="workflow:run")
    for run_id in ("run_hand", "run_hand_routine"):
        story = _today_story(client, run_id)
        assert story["why"]["on_its_own"] is False, run_id
        assert story["why"]["text"] == "Started here", run_id


def test_a_call_a_person_made_is_started_by_hand_on_the_row_and_in_its_detail(client, node) -> None:
    """C7: a call the owner made is started by hand, on the row and in its detail alike. It is not "on its own",
    and its detail does not say an agent asked for it."""
    node.get("traces").write(TraceEvent(kind="tool_end", principal="owner", principal_kind="human", tool="shop.post",
                                        lane="biz-a/shop", workspace="biz-a", effect="commit", status="ok", summary="posted"))
    page = client.get(f"{ORG}/history", params={"days": 2}).json()
    [today] = [day for day in page["days"] if day["label"] == "Today"]
    [story] = [item for item in today["stories"] if item["source"] == "call" and item["who"]["id"] == "owner"]
    assert story["why"]["on_its_own"] is False
    assert story["why"]["text"] == "Started by hand"


def test_a_lane_control_row_says_which_action_it_was() -> None:
    """The action rides on the trace's own args, so History can say took over or handed back. Anything else is None."""
    from openajar.api.web import history

    row = {"id": "tr_1", "at": "2026-11-07T10:00:00Z", "kind": "panel", "tool": "panel.lane.control", "lane": "biz-a/social",
           "principal": "owner", "principal_kind": "human", "effect": None, "status": "ok",
           "summary": "lane.control biz-a social", "args": {"control": "take"}}
    assert history.trace_out(row)["control"] == "take"
    assert history.trace_out({**row, "args": {"control": "hand_back"}})["control"] == "hand_back"
    assert history.trace_out({**row, "args": {"control": "pwn; rm -rf"}})["control"] is None
    assert history.trace_out({**row, "args": None})["control"] is None
    assert history.trace_out({**row, "tool": "panel.lane.check", "args": None})["summary"] == "lane.control biz-a social"


# ---- a browser act in words: only what its own rows know (finding 23 of the 2026-11-10 full test) ----

def _act(node, *, effect: str = "draft", action: str = "click", status: str = "ok", lane: str = "biz-a/instagram",
         about: tuple[tuple[str, str, dict], ...] = ()) -> str:
    """One browser_act call as wrap_tool and the act write it: a start, the rows about it, then its end."""
    traces = node.get("traces")
    common = {"principal": "claude-code", "principal_kind": "agent", "tool": "browser_act", "lane": lane}
    start = traces.write(TraceEvent(kind="tool_start", status="started", effect="draft", args={"action": action}, **common))
    for kind, row_status, args in about:
        traces.write(TraceEvent(kind=kind, status=row_status, effect="commit", args=args, parent_id=start.id, **common))
    traces.write(TraceEvent(kind="tool_end", status=status, effect=effect, args={"action": action}, parent_id=start.id,
                            **common))
    return start.id


def _today_calls(client) -> list[dict]:
    page = client.get(f"{ORG}/history", params={"days": 1}).json()
    return [story for day in page["days"] for story in day["stories"] if story["source"] == "call"]


def test_a_like_and_an_unlike_are_clicks_never_a_post_or_a_draft(client, node) -> None:
    """A like and an unlike on Instagram: no commit rule left, so nothing reads as a photo posted or a draft
    saved. A background request the gate let go as unknown names no publish, and its own row is not a story."""
    _act(node, effect="commit", about=(("commit_gate", "ok", {"effect_rule": "instagram.unknown"}),))
    _act(node, effect="draft", about=(("egress_blocked", "denied", {"rule": "commit_gate"}),))
    stories = _today_calls(client)
    assert [story["sentence"] for story in stories] == ["Clicked on Biz A's Instagram"] * 2
    assert not any("Posted" in story["sentence"] or "draft" in story["sentence"] for story in stories)
    assert all(story["action"] == "change" for story in stories)


def test_an_act_whose_commit_rule_left_is_named_by_that_rule(client, node) -> None:
    _act(node, effect="commit", about=(("commit_gate", "ok", {"effect_rule": "instagram.comment"}),))
    _act(node, effect="commit", about=(("commit_observed", "ok", {"effect_rule": "instagram.post", "sent": True}),))
    _act(node, effect="commit", about=(("commit_gate", "pending", {"effect_rule": "instagram.post"}),))
    sentences = sorted(story["sentence"] for story in _today_calls(client))
    assert sentences == ["Clicked on Biz A's Instagram", "Posted on Biz A's Instagram", "Replied on Biz A's Instagram"]


def test_an_act_that_read_its_target_back_changed_counts_as_checked(client, node) -> None:
    _act(node, about=(("act_read_back", "ok", {"changed": True, "action": "click"}),))
    _act(node, action="type", about=(("act_read_back", "ok", {"changed": False, "action": "type"}),))
    page = client.get(f"{ORG}/history", params={"days": 1}).json()
    [today] = page["days"]
    by_sentence = {story["sentence"]: story for story in today["stories"]}
    assert by_sentence["Clicked on Biz A's Instagram"]["verdict"] == "checked"
    assert by_sentence["Typed on Biz A's Instagram"]["verdict"] == "not_checked"
    assert today["done"] == 2 and today["checked"] == 1
    assert today["summary"].startswith("2 things done · 1 checked")


def test_a_refused_call_says_it_was_stopped_before_it_ran(client, node) -> None:
    _act(node, status="denied")
    [story] = _today_calls(client)
    assert story["verdict"] == "not_run"
    assert story["sentence"] == "Stopped before clicking on Biz A's Instagram"
    assert story["verdict_text"].startswith("openajar refused this call before it ran, so nothing happened.")
    assert "Did not" not in story["sentence"]


def test_an_agents_own_mistake_is_not_blamed_on_the_site():
    """Full test 2026-11-10: a call with a bad ref read "Something went wrong on the site. Watch the account"."""
    from openajar.api.web import telling

    assert telling.plain_failure("ref must look like e12 (or f3e12 inside a frame); copy it from the latest "
                                 "browser_snapshot") == "The agent's request was not valid, so nothing happened on the site."
    assert telling.plain_failure("ref is empty; copy a ref like e12 from the latest browser_snapshot").startswith(
        "The agent's request was not valid")
    assert "busy" not in telling.plain_failure("lane test/linkedin is busy: agent x is using it since 11:24:49 UTC")
    assert telling.plain_failure("lane test/linkedin is busy: agent x is using it").startswith("Another job")
    assert telling.plain_failure("a timeout nobody can name better") == "It took too long, so the agent stopped."
    assert telling.plain_failure("no known words") == telling.SOMETHING_WENT_WRONG


def test_the_histogram_reads_the_four_approval_states_at_once_and_counts_them(monkeypatch) -> None:
    import asyncio
    from datetime import UTC, datetime

    from openajar.api.web import history

    now = datetime.now(UTC).isoformat()
    waiting: list[str] = []
    both = asyncio.Event()

    async def fake_read(request, path, query=None):
        if path == "/api/runs":
            return []
        waiting.append(query["state"])
        if len(waiting) == 4:
            both.set()
        await asyncio.wait_for(both.wait(), 2)  # returns only once all four reads are in flight
        if query["state"] == "approved":
            return [{"lane": "biz-a/shop", "state": "approved", "created_at": now, "decided_at": now}]
        if query["state"] == "pending":
            return [{"lane": "biz-a/shop", "state": "pending", "created_at": now},
                    {"lane": "biz-a/shop", "kind": "question", "created_at": now}]
        return []

    async def no_rows(*args, **kwargs):
        return []

    monkeypatch.setattr(history, "read", fake_read)
    monkeypatch.setattr(history, "_window", no_rows)

    from types import SimpleNamespace

    chart = asyncio.run(history._activity_chart(SimpleNamespace(query_params={"range": "24h"}), None))
    assert sorted(waiting) == ["approved", "denied", "expired", "pending"]
    assert chart["totals"]["approvals"] == 2
    assert sum(b["approvals"]["approved"] for b in chart["buckets"]) == 1


def test_an_environment_session_is_one_done_story_named_by_its_title(client, node, monkeypatch) -> None:
    """2026-11-15 test, F4: eleven commands in an environment were eleven "Not checked" warnings on
    "Env's Esrkyfqerme". They are one story, by the environment's title, done, with the commands behind
    Technical details."""
    from openajar.api.web import history

    monkeypatch.setattr(history.envs_web, "titles", lambda api: {"esrkyfqerme": "qa-env-2026-11-15"})
    for index in range(3):  # a call from before the 2026-11-19 rename keeps its old name, and counts the same
        tool = "lane_exec" if index == 0 else "terminal_exec"
        _call(node, tool, "ok", lane="env/esrkyfqerme", effect="exec", summary=f"pnpm test {index}")
    _call(node, "terminal_exec", "error", lane="env/esrkyfqerme", effect="exec", summary="pnpm lint")
    _call(node, "browser_navigate", "ok", lane="env/esrkyfqerme-browser", summary="http://localhost:5173")
    stories = [s for day in client.get(f"{ORG}/history").json()["days"] for s in day["stories"]]
    mine = [s for s in stories if s["lane"].startswith("env/")]
    assert len(mine) == 1, [s["sentence"] for s in mine]
    story = mine[0]
    assert story["sentence"] == "Worked in qa-env-2026-11-15: ran 4 commands and used the browser once"
    assert story["verdict"] == "done" and "no account to check" in story["verdict_text"]
    assert "1 of the 5 steps ended with an error" in story["verdict_text"]
    assert "Esrkyfqerme" not in story["sentence"] + story["verdict_text"] + story["place"]
    assert "Env's" not in story["sentence"] and "pnpm lint" in story["detail"]
    assert story["steps"][0]["label"] == "Ran a command in the terminal"
