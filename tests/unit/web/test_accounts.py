"""The accounts area on a node: status, workspaces, templates, the login queue, lanes and their
control, ask-first and run bars, the live stream, and the live view and terminal sockets.

Each route is called as the app calls it (web/src/api/client.ts), on the dev node's real services,
and each one has a refusal the app is shown as it was. The shapes are the ones in web/src/api/types.ts.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agentos.api import ApiSettings, PanelStore, build_api
from agentos.api.auth import SESSION_COOKIE
from agentos.api.fakes import demo_runtime
from agentos.api.lane_metrics import lane_metrics
from agentos.api.web.accounts import lane_for_app, set_by_for_app, slug, template_for_app
from agentos.api.web.events import reshape_frame
from agentos.core.models import RunStatus
from agentos.ops.lanecheck import Check, LaneReport
from agentos.runner.models import Run

ORG = "/api/orgs/local"
LANE = "biz-a/shop"


def socket_headers(client) -> dict[str, str]:
    """The signed-in owner's handshake: the session cookie, and the origin the socket is opened from."""
    # The socket's host is the test server's (testserver), and a browser's origin must be that host.
    return {"Origin": "http://testserver", "cookie": f"{SESSION_COOKIE}={client.cookies.get(SESSION_COOKIE)}"}


def open_lane(client, lane: str = LANE) -> dict:
    ws, name = lane.split("/")
    response = client.post(f"{ORG}/lanes/{ws}/{name}/open")
    assert response.status_code == 200, response.text
    return response.json()


def close_lane(client, lane: str = LANE) -> None:
    ws, name = lane.split("/")
    client.post(f"{ORG}/lanes/{ws}/{name}/close")


def events_of(client, last_event_id: int = 0) -> list[tuple[str, dict]]:
    """Every event on the stream after `last_event_id`, read until the stream ends (the test's deadline)."""
    with client.stream("GET", f"{ORG}/events", params={"last_event_id": last_event_id}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        text = "".join(response.iter_text())
    found: list[tuple[str, dict]] = []
    for block in text.split("\n\n"):
        kind = data = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                kind = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        if kind and data is not None:
            found.append((kind, data))
    return found


# ---- status ----

def test_status_is_the_nodes_own_with_the_app_plan_and_usage(client) -> None:
    status = client.get(f"{ORG}/status").json()
    assert status["node"] == "dev" and status["csrf"]
    assert "plan" not in status  # a computer has no plan, so it says none: nothing to pay, nothing to upgrade
    assert status["mcp_url"] == "http://localhost:7999/mcp"  # this computer's own MCP address, not the cloud's
    assert status["brain"] is None  # the dev node has no brain: none is said, rather than a made-up one
    assert "limits" not in status  # a computer has no plan caps
    usage = status["usage"]
    assert usage["nodes"] == 1 and usage["active_lanes"] == 4 and usage["workspaces"] >= 2 and usage["members"] >= 1


def test_status_needs_a_session(client) -> None:
    client.cookies.clear()
    assert client.get(f"{ORG}/status").status_code == 401


# ---- workspaces ----

def test_workspaces_list_each_lane_as_the_app_reads_it(client) -> None:
    workspaces = client.get(f"{ORG}/workspaces").json()
    lane = next(lane for workspace in workspaces for lane in workspace["lanes"])
    assert set(lane["control"]) >= {"state", "by", "since", "takeback_requested", "takeback_reason",
                                    "takeback_by", "last_human_input"}
    assert lane["signal_detail"] is None and lane["account"] is None
    assert lane["viewer"] is None  # cold until opened


def test_a_lane_carries_its_own_last_check_so_a_failing_one_reads_the_same_everywhere(client, node) -> None:
    """The account's own check says whether it is safe to use. The lane, its list and its business carry it,
    so the account page, Accounts and Home can say the same thing (a failing check is stuck, not Ready)."""
    checker = node.get("lane_check")
    assert checker is not None
    checker.last[LANE] = LaneReport(LANE, "cold", datetime.now(UTC),
                                    [Check("profile", "fail", "The profile folder is missing, so the lane has no logins.")])
    try:
        lane = client.get(f"{ORG}/lanes/biz-a/shop").json()
        assert lane["check"] == {"level": "fail", "safe": False}
        listed = next(w for w in client.get(f"{ORG}/workspaces").json() if w["id"] == "biz-a")["lanes"]
        assert next(item for item in listed if item["id"] == LANE)["check"] == {"level": "fail", "safe": False}
        other = next(item for item in listed if item["id"] != LANE)
        assert other["check"] is None or set(other["check"]) == {"level", "safe"}
    finally:
        checker.last.pop(LANE, None)


def test_a_warm_lane_says_where_its_live_view_is(client) -> None:
    open_lane(client)
    try:
        lane = client.get(f"{ORG}/lanes/biz-a/shop").json()
        assert lane["viewer"] == {"url": "/api/orgs/local/lanes/biz-a/shop/vnc"}
    finally:
        close_lane(client)


def test_create_a_workspace_from_its_name(client) -> None:
    created = client.post(f"{ORG}/workspaces", json={"name": "Aster Coffee", "goal": "post"})
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["id"] == "aster-coffee" and body["name"] == "Aster Coffee" and body["lanes"] == []
    assert any(item["id"] == "aster-coffee" for item in client.get(f"{ORG}/workspaces").json())


def test_a_workspace_needs_a_name(client) -> None:
    refused = client.post(f"{ORG}/workspaces", json={"color": "#4f7cff"})
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "invalid"


def test_a_name_with_no_letters_cannot_make_an_address(client) -> None:
    refused = client.post(f"{ORG}/workspaces", json={"name": "!!!"})
    assert refused.status_code == 400
    assert slug("Aster Coffee!") == "aster-coffee"


def test_rename_a_workspace_and_read_it_back_with_its_lanes(client) -> None:
    client.post(f"{ORG}/workspaces", json={"name": "Rename Me"})
    renamed = client.patch(f"{ORG}/workspaces/rename-me", json={"name": "Renamed"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Renamed" and renamed.json()["lanes"] == []


def test_renaming_a_workspace_that_is_not_there_is_refused(client) -> None:
    refused = client.patch(f"{ORG}/workspaces/no-such-place", json={"name": "Nope"})
    assert refused.status_code == 404


def test_a_workspace_with_lanes_cannot_be_deleted(client) -> None:
    refused = client.delete(f"{ORG}/workspaces/biz-a")
    assert refused.status_code == 409


def test_delete_an_empty_workspace(client) -> None:
    client.post(f"{ORG}/workspaces", json={"name": "Gone Soon"})
    assert client.delete(f"{ORG}/workspaces/gone-soon").status_code == 200


# ---- templates ----

def test_templates_are_the_app_s_template_info(client) -> None:
    templates = client.get(f"{ORG}/templates").json()["templates"]
    personal = next(item for item in templates if item["id"] == "personal")
    assert personal["title"] and isinstance(personal["surfaces"], list)
    assert all({"name", "platform"} <= set(surface) for surface in personal["surfaces"])


def test_apply_a_template_and_get_the_lanes_it_made(client) -> None:
    client.post(f"{ORG}/workspaces", json={"name": "Templated"})
    applied = client.post(f"{ORG}/templates/apply", json={"template": "personal", "workspaces": ["templated"]})
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["status"] == "complete" and body["failed"] is None
    assert body["created"] and all(lane.startswith("templated/") for lane in body["created"])
    assert all(isinstance(lane, str) for lane in body["created"])


def test_apply_an_unknown_template_is_refused(client) -> None:
    refused = client.post(f"{ORG}/templates/apply", json={"template": "no-such", "workspaces": ["biz-a"]})
    assert refused.status_code >= 400


def test_plan_a_template_before_applying_it(client) -> None:
    plan = client.post(f"{ORG}/templates/plan", json={"template": "personal", "workspaces": ["biz-b"]})
    assert plan.status_code == 200, plan.text
    body = plan.json()
    assert body["template"] == "personal" and body["ok"] is True
    assert set(body["counts"]) == {"create", "exists", "conflict"}
    assert isinstance(body["steps"], list)


def test_plan_an_unknown_template_is_refused(client) -> None:
    assert client.post(f"{ORG}/templates/plan", json={"template": "no-such", "workspaces": ["biz-b"]}).status_code >= 400


# ---- the login queue ----

def test_the_login_queue(client) -> None:
    queue = client.get(f"{ORG}/queue").json()
    assert {"summary", "ready", "total", "needs_login", "needs_attention", "workspaces", "lanes"} <= set(queue)


def test_the_login_queue_needs_a_session(client) -> None:
    client.cookies.clear()
    assert client.get(f"{ORG}/queue").status_code == 401


# ---- lanes ----

def test_get_a_lane(client) -> None:
    lane = client.get(f"{ORG}/lanes/biz-a/shop").json()
    assert lane["id"] == LANE and lane["control"]["state"] in ("agent", "human", "paused")
    assert set(lane) >= {"workspace", "name", "kind", "state", "signal", "proxy", "node", "logged_in_hint", "control"}


def test_a_lane_that_is_not_there_is_not_found(client) -> None:
    assert client.get(f"{ORG}/lanes/biz-a/no-such").status_code == 404


def test_create_a_lane(client) -> None:
    created = client.post(f"{ORG}/lanes", json={"id": "biz-a/fresh", "kind": "browser", "preset": "account"})
    assert created.status_code == 200, created.text
    assert created.json()["id"] == "biz-a/fresh" and created.json()["kind"] == "browser"


def test_create_a_lane_with_a_proxy_that_is_not_there_is_refused(client) -> None:
    refused = client.post(f"{ORG}/lanes", json={"id": "biz-a/proxied", "kind": "browser", "proxy_label": "nowhere"})
    assert refused.status_code == 400


def test_open_and_close_a_lane(client) -> None:
    opened = open_lane(client)
    assert opened["state"] in ("warm", "active", "warming")
    closed = client.post(f"{ORG}/lanes/biz-a/shop/close")
    assert closed.status_code == 200 and closed.json()["state"] == "cold"


def test_open_a_lane_that_is_not_there_is_refused(client) -> None:
    assert client.post(f"{ORG}/lanes/biz-a/no-such/open").status_code == 404


def test_take_and_hand_back_control_of_a_lane(client) -> None:
    taken = client.post(f"{ORG}/lanes/biz-a/shop/control", json={"action": "take"})
    assert taken.status_code == 200, taken.text
    assert taken.json()["control"]["state"] == "human"
    handed = client.post(f"{ORG}/lanes/biz-a/shop/control", json={"action": "hand_back"})
    assert handed.status_code == 200 and handed.json()["control"]["state"] == "agent"


def test_a_lane_held_by_the_owner_says_who_and_by_id(client) -> None:
    """The app says "You have control" only when `by_id` is the person looking, so the id is always sent.
    A holder is named in words, never as an id."""
    taken = client.post(f"{ORG}/lanes/biz-a/shop/control", json={"action": "take"})
    assert taken.status_code == 200, taken.text
    control = taken.json()["control"]
    assert control["state"] == "human"
    assert control["by"] == "Owner" and control["by_id"] == "owner"  # the owner member, by name
    client.post(f"{ORG}/lanes/biz-a/shop/control", json={"action": "hand_back"})
    handed = client.get(f"{ORG}/lanes/biz-a/shop").json()["control"]
    assert handed["by"] is None and handed["by_id"] is None


def test_an_unknown_control_action_is_refused(client) -> None:
    refused = client.post(f"{ORG}/lanes/biz-a/shop/control", json={"action": "fly"})
    assert refused.status_code == 400


def test_release_has_nothing_to_release_on_a_computer(client) -> None:
    refused = client.post(f"{ORG}/lanes/biz-a/shop/release")
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "conflict"


def test_release_a_lane_that_is_not_there_is_not_found(client) -> None:
    assert client.post(f"{ORG}/lanes/biz-a/no-such/release").status_code == 404


def test_the_screenshot_of_a_cold_lane_is_refused(client) -> None:
    close_lane(client)
    refused = client.get(f"{ORG}/lanes/biz-a/shop/screenshot")
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "lane_cold"


def test_readiness_is_set_and_refused_when_unknown(client) -> None:
    assert client.post(f"{ORG}/lanes/biz-a/shop/readiness", json={"state": "ready"}).status_code == 200
    assert client.post(f"{ORG}/lanes/biz-a/shop/readiness", json={"state": "nonsense"}).status_code >= 400


# ---- ask me first, per account ----

def test_every_account_has_an_ask_first_switch(client) -> None:
    switches = client.get(f"{ORG}/ask-first").json()
    lanes = {item["lane"] for item in switches}
    assert "biz-a/shop" in lanes
    for item in switches:
        assert set(item) == {"lane", "ask_first", "asks_first", "ask_first_set_by", "ask_first_set_at",
                             "ask_first_supported", "ask_first_online"}


def test_turn_ask_first_on_and_off_for_an_account(client) -> None:
    on = client.put(f"{ORG}/lanes/biz-a/shop/ask-first", json={"ask_first": True})
    assert on.status_code == 200, on.text
    assert on.json()["ask_first"] is True and on.json()["ask_first_set_by"] == {"kind": "computer", "id": None}
    listed = {item["lane"]: item for item in client.get(f"{ORG}/ask-first").json()}
    assert listed["biz-a/shop"]["ask_first"] is True
    off = client.put(f"{ORG}/lanes/biz-a/shop/ask-first", json={"ask_first": False})
    assert off.status_code == 200 and off.json()["ask_first"] is False


def test_ask_first_needs_a_yes_or_no(client) -> None:
    refused = client.put(f"{ORG}/lanes/biz-a/shop/ask-first", json={"ask_first": "maybe"})
    assert refused.status_code >= 400


def test_an_agent_setting_ask_first_is_read_as_the_agent(client) -> None:
    row = {"set_by": "agent:codex", "by_person": False, "set_at": "2026-11-06T00:00:00Z", "ask_first": True}
    assert set_by_for_app(row) == {"kind": "agent", "id": "codex"}
    assert set_by_for_app({"set_by": "member-7", "by_person": True}) == {"kind": "user", "id": "member-7"}
    assert set_by_for_app({"set_by": None}) is None
    # the owner on the node's command line is the owner, from the command line: never "owner:cli" (finding 28)
    assert set_by_for_app({"set_by": "owner:cli", "by_person": True}) == {"kind": "user", "id": "owner", "via": "cli"}


# ---- run bars ----

def test_a_lane_s_run_bars_count_its_runs_by_bar(client, node) -> None:
    now = datetime.now(UTC)
    store = node.get("runner").store
    store.insert_run(Run(id="run_metrics_a", lane=LANE, status=RunStatus.SUCCEEDED_VERIFIED,
                         created_at=now - timedelta(hours=1)))
    store.insert_run(Run(id="run_metrics_b", lane=LANE, status=RunStatus.FAILED,
                         created_at=now - timedelta(hours=1, minutes=5)))
    metrics = client.get(f"{ORG}/lanes/biz-a/shop/metrics", params={"range": "7d"}).json()
    assert metrics["range"] == "7d" and metrics["bucket_seconds"] == 6 * 3600
    assert len(metrics["buckets"]) == 28
    assert sum(bucket["runs"] for bucket in metrics["buckets"]) >= 2
    assert sum(bucket["failed"] for bucket in metrics["buckets"]) >= 1
    assert all("signal" not in bucket for bucket in metrics["buckets"])


def test_run_bars_refuse_a_range_they_do_not_know(client) -> None:
    refused = client.get(f"{ORG}/lanes/biz-a/shop/metrics", params={"range": "1y"})
    assert refused.status_code == 400


def test_lane_metrics_bucket_a_run_into_its_bar_and_drop_the_rest() -> None:
    now = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    runs = [(now - timedelta(hours=1), "failed"), (now - timedelta(days=8), "ok"), (now + timedelta(hours=1), "ok")]
    out = lane_metrics(runs, range_name="7d", now=now)
    assert len(out["buckets"]) == 28
    assert sum(bucket["runs"] for bucket in out["buckets"]) == 1
    assert out["buckets"][-1]["failed"] == 1 and out["buckets"][-1]["runs"] == 1
    assert out["buckets"][0]["at"].startswith("2026-10-30T12:00:00")


# ---- shapes the reshapes make (pure) ----

def test_a_lane_from_the_node_is_the_app_s_lane() -> None:
    node_lane = {"id": "biz-a/shop", "workspace": "biz-a", "name": "shop", "viewer": {"ws_path": "/x", "display": None},
                 "control": {"state": "agent", "by": None, "since": None, "takeback_requested": False,
                             "takeback_reason": None, "takeback_by": None, "last_human_input": None}}
    lane = lane_for_app(node_lane)
    assert lane["viewer"] == {"url": "/api/orgs/local/lanes/biz-a/shop/vnc"}
    assert lane["signal_detail"] is None and lane["account"] is None
    assert lane_for_app({**node_lane, "viewer": None})["viewer"] is None


def test_a_template_is_the_app_s_template_info() -> None:
    template = {"id": "personal", "name": "Personal accounts", "description": "d",
                "lanes": [{"name": "instagram", "platform": "instagram", "kind": "browser", "preset": "account"}]}
    assert template_for_app(template) == {
        "id": "personal", "title": "Personal accounts", "description": "d",
        "surfaces": [{"name": "instagram", "platform": "instagram"}],
    }


# ---- the live stream ----

def test_the_live_stream_reshapes_a_lane_as_the_app_reads_it(client) -> None:
    open_lane(client)
    try:
        lane_events = [data for kind, data in events_of(client) if kind == "lane"]
        assert lane_events, "opening a lane publishes a lane event"
        for lane in lane_events:
            assert "signal_detail" in lane and "account" in lane
            assert set(lane["control"]) >= {"takeback_requested", "takeback_reason", "takeback_by"}
    finally:
        close_lane(client)


def test_the_live_stream_reads_a_trace_s_status_as_a_run_s(client) -> None:
    open_lane(client)
    try:
        traces = [data for kind, data in events_of(client) if kind == "trace"]
        assert traces, "a panel action is traced"
        assert all(trace["status"] in {"queued", "running", "waiting_approval", "succeeded_verified",
                                       "succeeded_unverified", "partial", "failed", "cancelled"} for trace in traces)
    finally:
        close_lane(client)


def test_the_live_stream_needs_a_session(client) -> None:
    client.cookies.clear()
    assert client.get(f"{ORG}/events").status_code == 401


def test_frames_the_app_does_not_read_pass_as_the_node_wrote_them() -> None:
    ping = b": ping\n\n"
    assert reshape_frame(ping) == ping
    retry = b"retry: 3000\nid: 0\n\n"
    assert reshape_frame(retry) == retry
    workspace = b'id: 9\nevent: workspace\ndata: {"id":"x"}\n\n'
    assert reshape_frame(workspace) == workspace
    resync = b'event: resync\ndata: {}\n\n'
    assert reshape_frame(resync) == resync


def test_a_question_s_approval_is_not_sent_as_an_approval() -> None:
    question = b'id: 4\nevent: approval\ndata: {"id":"ap_1","kind":"question","effect":null}\n\n'
    assert reshape_frame(question) is None


def test_an_action_s_approval_passes_as_the_node_wrote_it() -> None:
    action = b'id: 4\nevent: approval\ndata: {"id":"ap_2","kind":"action","effect":"commit"}\n\n'
    assert json.loads(reshape_frame(action).split(b"data: ", 1)[1]) == {"id": "ap_2", "kind": "action", "effect": "commit"}


def test_a_run_with_no_lane_or_start_is_the_app_s_run() -> None:
    frame = b'id: 5\nevent: run\ndata: {"id":"run_1","lane":null,"started_at":null,"created_at":"2026-11-06T10:00:00Z"}\n\n'
    payload = json.loads(reshape_frame(frame).split(b"data: ", 1)[1])
    assert payload["lane"] == "" and payload["started_at"] == "2026-11-06T10:00:00Z"


def test_a_frame_the_node_wrote_unreadably_passes_untouched() -> None:
    broken = b"id: 3\nevent: lane\ndata: {not json\n\n"
    assert reshape_frame(broken) == broken


# ---- the sockets: which view a caller gets ----

def test_a_control_view_of_a_lane_nobody_holds_is_refused_not_holding(client) -> None:
    open_lane(client)
    try:
        with client.websocket_connect("/api/lanes/biz-a/shop/vnc?mode=control", headers=socket_headers(client)) as ws, \
                pytest.raises(WebSocketDisconnect) as refused:
            ws.receive_bytes()
        assert refused.value.code == 4403
        assert refused.value.reason == "not_holding"
    finally:
        close_lane(client)


def test_a_cold_lane_closes_as_closed_before_it_was_ready(client) -> None:
    close_lane(client)
    with client.websocket_connect("/api/lanes/biz-a/shop/vnc", headers=socket_headers(client)) as ws, \
            pytest.raises(WebSocketDisconnect) as closed:
        ws.receive_bytes()
    assert closed.value.code == 4410


def test_a_control_view_closes_as_handed_back_when_control_moves_away(client) -> None:
    open_lane(client)
    try:
        client.post(f"{ORG}/lanes/biz-a/shop/control", json={"action": "take"})
        with client.websocket_connect("/api/lanes/biz-a/shop/vnc?mode=control", headers=socket_headers(client)) as ws:
            client.post(f"{ORG}/lanes/biz-a/shop/control", json={"action": "hand_back"})
            with pytest.raises(WebSocketDisconnect) as handed:
                ws.receive_bytes()
        assert handed.value.code == 4403
        assert handed.value.reason == "handed_back"
    finally:
        close_lane(client)


def test_the_terminal_view_reads_the_shell_and_types_nothing() -> None:
    """A view of a shell takes its size and its output, and nothing typed reaches it (the demo shell)."""
    client = TestClient(build_api(demo_runtime(), ApiSettings(store=PanelStore(None), owner_token="owner")))
    with client.websocket_connect("/api/lanes/personal/shell/terminal?mode=view&cols=100&rows=30",
                                  headers={"Authorization": "Bearer owner"}) as ws:
        assert ws.receive_text() == "$ connected to personal/shell (100x30)\r\n"
        ws.send_text("ls\r")  # typed in a view: the shell never sees it, so it never echoes it
        ws.send_text('{"type":"resize","cols":120,"rows":40}')
        assert ws.receive_text() == "[120x40]"  # the next thing out is the resize's answer, not an echo
