"""agentos dash data layer: connection resolution, the API client and its SSE stream, the state
and its derived views, and the sync loop, all against the in-process fake node (no sockets)."""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentos.core.config import NodeConfig, save_config
from agentos.dev.tuidemo import FakeNode
from agentos.tui import fmt
from agentos.tui.client import ApiClient, ApiError, AuthFailed, NodeUnreachable, SseParser, Unavailable
from agentos.tui.connect import DEFAULT_URL, normalize_url, resolve
from agentos.tui.state import RESOURCES, DashState
from agentos.tui.sync import Syncer

NOW = datetime(2026, 11, 4, 8, 0, tzinfo=UTC)  # 10:00 in Europe/Vilnius
EM_DASH = "—"


@pytest.fixture
def node():
    return FakeNode(now=NOW)


@pytest.fixture
async def client(node):
    c = ApiClient("http://node.test", "demo", transport=node.transport())
    yield c
    await c.aclose()


async def loaded(node) -> DashState:
    state = DashState()
    client = ApiClient("http://node.test", "demo", transport=node.transport())
    await Syncer(client, state, lambda topics: None).refresh()
    await client.aclose()
    return state


# ---------------------------------------------------------------- connection
def test_flags_beat_env_beat_node_toml(tmp_path):
    save_config(NodeConfig(home=tmp_path, mcp_port=7431, mcp_token="toml-token", ssh_host="mynode"))
    env = {"AGENTOS_HOME": str(tmp_path)}
    conn = resolve(env=env)
    assert conn.url == "http://127.0.0.1:7431"
    assert conn.token == "toml-token" and conn.token_source.endswith("node.toml")
    assert conn.tunnel_hint == "ssh -N -L 7431:localhost:7431 mynode"

    env |= {"AGENTOS_URL": "http://127.0.0.1:7400/", "AGENTOS_TOKEN": "env-token"}
    conn = resolve(env=env)
    assert (conn.url, conn.token, conn.token_source) == ("http://127.0.0.1:7400", "env-token", "AGENTOS_TOKEN")

    conn = resolve(url="localhost:7499/api", token="flag-token", env=env)
    assert (conn.url, conn.token, conn.url_source) == ("http://localhost:7499", "flag-token", "--url")


def test_defaults_when_nothing_is_set(tmp_path):
    conn = resolve(env={"AGENTOS_HOME": str(tmp_path / "missing")})
    assert conn.url == DEFAULT_URL and not conn.has_token and conn.tunnel_hint is None


def test_unreadable_node_toml_falls_back(tmp_path):
    (tmp_path / "node.toml").write_text("this is [not toml")
    conn = resolve(env={"AGENTOS_HOME": str(tmp_path)})
    assert conn.url == DEFAULT_URL and not conn.has_token


def test_token_stays_out_of_repr_and_descriptions():
    conn = resolve(url="http://x", token="sup3r-secret", env={})
    assert "sup3r-secret" not in repr(conn)
    assert "sup3r-secret" not in conn.describe()
    assert "token from --token" in conn.describe()


def test_normalize_url():
    assert normalize_url(" 127.0.0.1:7400/ ") == "http://127.0.0.1:7400"
    assert normalize_url("https://node.example/api") == "https://node.example"


# ---------------------------------------------------------------- SSE parser
def test_sse_parser_handles_events_comments_ids_and_multiline_data():
    p = SseParser()
    lines = [": heartbeat", "id: 7", "event: lane", 'data: {"id": "biz-a/instagram",', 'data: "state": "warm"}',
             "", "retry: 5000", "data: not json", "", "event: empty", ""]
    events = [e for e in (p.feed(line) for line in lines) if e is not None]
    assert events[0].event == "lane" and events[0].id == "7"
    assert events[0].data == {"id": "biz-a/instagram", "state": "warm"}
    assert events[1].event == "message" and events[1].data == "not json"
    assert len(events) == 2 and p.retry_ms == 5000 and p.last_id == "7"


# ---------------------------------------------------------------- client
async def test_client_sends_bearer_and_reads_resources(node, client):
    status = await client.status()
    assert status["node"] == "node-1" and status["timezone"] == "Europe/Vilnius"
    workspaces = await client.workspaces()
    assert sum(len(ws["lanes"]) for ws in workspaces) == 31  # 29 accounts and an environment's terminal and browser
    assert {ws["id"] for ws in workspaces} == {"biz-a", "biz-b", "biz-c", "biz-d", "personal", "env"}


async def test_client_maps_errors(node, client):
    node.unavailable = {"approvals"}
    with pytest.raises(Unavailable) as exc:
        await client.approvals()
    assert "not configured" in exc.value.message
    with pytest.raises(ApiError) as exc:
        await client.lane("biz-a/nope")
    assert exc.value.status == 404 and exc.value.code == "lane_not_found"
    with pytest.raises(ApiError) as exc:
        await client.lane_control("biz-a/instagram", "deny_request")
    assert exc.value.status == 409
    bad = ApiClient("http://node.test", "wrong-token", transport=node.transport())
    with pytest.raises(AuthFailed):
        await bad.status()
    await bad.aclose()
    node.go_down()
    with pytest.raises(NodeUnreachable) as exc:
        await client.status()
    assert exc.value.sent is False and "refused" in exc.value.message


async def test_missing_endpoint_reads_as_unavailable(node, client):
    with pytest.raises(Unavailable):
        await client.get("/api/not-there")


async def test_action_bodies(node, client):
    await client.lane_control("biz-a/instagram", "take")
    await client.view_link("biz-a/instagram", "control")
    await client.approve("ap_7f3")
    await client.deny("ap_81c", "wrong tone")
    await client.batch_decide(["ap_92d"], "approve")
    await client.answer("q_51", "bright")
    await client.add_schedule("biz-a.reddit.reply", "0 9 * * 1-5", {"text": "hi"})
    await client.set_schedule_enabled("s_1", False)
    await client.run_flow("biz-a.blog.publish", {"draft_id": "d1"})
    await client.delete_schedule("s_6")
    assert node.calls("POST", "/api/lanes/biz-a/instagram/control") == [{"action": "take"}]
    assert node.calls("POST", "/api/lanes/biz-a/instagram/view-link") == [{"mode": "control"}]
    assert node.calls("POST", "/api/approvals/ap_7f3/approve") == [{}]
    assert node.calls("POST", "/api/approvals/ap_81c/deny") == [{"note": "wrong tone"}]
    assert node.calls("POST", "/api/approvals/batch") == [{"ids": ["ap_92d"], "decision": "approve"}]
    assert node.calls("POST", "/api/questions/q_51/answer") == [{"answer": "bright"}]
    assert node.calls("POST", "/api/schedules") == [{"flow": "biz-a.reddit.reply", "cron": "0 9 * * 1-5",
                                                     "params": {"text": "hi"}}]
    assert node.calls("PATCH", "/api/schedules/s_1") == [{"enabled": False}]
    assert node.calls("POST", "/api/flows/biz-a.blog.publish/run") == [{"params": {"draft_id": "d1"}}]
    assert node.calls("DELETE", "/api/schedules/s_6") == [None]


async def test_screenshot_is_png_and_cold_lane_is_refused(client):
    png = await client.screenshot("biz-a/instagram")
    assert png.startswith(b"\x89PNG")
    with pytest.raises(ApiError) as exc:
        await client.screenshot("biz-a/medium")
    assert "open it first" in exc.value.message


async def test_event_stream_opens_then_delivers(node, client):
    stream = client.events()
    first = await anext(stream)
    assert first.event == "open"
    node.emit("node", {"id": "node-2", "state": "up"})
    ev = await asyncio.wait_for(anext(stream), 2)
    assert ev.event == "node" and ev.data["state"] == "up" and ev.id == "1"
    await stream.aclose()


async def test_event_stream_404_is_unavailable(node, client):
    node.events_enabled = False
    with pytest.raises(Unavailable):
        async for _ in client.events():
            pass


# ---------------------------------------------------------------- state and derived views
async def test_workspace_health_lines(node):
    state = await loaded(node)
    ws = {w.id: w for w in state.workspaces}
    assert len(list(state.lanes())) == 31
    line = {k: state.workspace_line(w, now=NOW) for k, w in ws.items()}
    health = {k: state.workspace_health(w) for k, w in ws.items()}
    assert line["biz-a"] == "6 lanes, all ok, 2 posts scheduled today" and health["biz-a"] == "ok"
    assert line["biz-b"] == "6 lanes, 1 captcha, 1 with you" and health["biz-b"] == "attention"
    assert line["biz-c"] == "6 lanes, 1 wants control back, 1 with you" and health["biz-c"] == "attention"
    assert "1 quarantined" in line["biz-d"] and "1 logged out" in line["biz-d"] and health["biz-d"] == "blocked"
    assert line["personal"] == "5 lanes, node-2 down" and health["personal"] == "attention"
    assert not state.should_expand(ws["biz-a"]) and state.should_expand(ws["biz-b"])
    assert all(EM_DASH not in text for text in line.values())


async def test_needs_you_queue_is_grouped_by_effect(node):
    state = await loaded(node)
    items = state.needs_you()
    assert [i.group for i in items] == ["control", "question", "effect:pay", "effect:irreversible",
                                        "effect:commit", "effect:commit", "lane", "lane", "lane"]
    assert items[0].lane == "biz-c/linkedin" and "Finish the 14:00 post" in items[0].detail
    assert state.needs_you_count() == 9 == (await node_status(node))["needs_you"]
    assert state.needs_you_summary().startswith("1 wants control back, 1 question, 1 pay approval")


async def node_status(node):
    client = ApiClient("http://node.test", "demo", transport=node.transport())
    try:
        return await client.status()
    finally:
        await client.aclose()


async def test_events_fold_into_state(node):
    state = await loaded(node)
    lane = state.lane("biz-a/reddit").model_dump(mode="json")
    assert state.apply_event("lane", {**lane, "signal": "captcha"}) == {"workspaces"}
    assert state.workspace_health(state.workspace("biz-a")) == "attention"
    ctrl = {"state": "human", "by": "owner", "takeback_requested": False}
    assert state.apply_event("control", {"lane": "biz-a/reddit", "control": ctrl}) == {"workspaces"}
    assert state.lane("biz-a/reddit").control.state == "human"
    assert state.apply_event("approval", {"id": "ap_7f3", "state": "approved", "effect": "commit"}) == {"approvals"}
    assert "ap_7f3" not in state.approvals
    trace = {"id": "ev_new", "at": "2026-11-04T08:01:00Z", "lane": "biz-a/reddit", "status": "ok"}
    assert state.apply_event("trace", trace) == {"activity"}
    assert state.apply_event("trace", trace) == set()  # deduplicated
    assert state.apply_event("run", {"id": "r_200", "flow": "biz-a.reddit.reply", "status": "running"}) == {"runs"}
    assert state.apply_event("node", {"id": "node-2", "state": "up"}) == {"nodes"}
    assert state.workspace_line(state.workspace("personal")) == "5 lanes, sleeping"
    assert state.apply_event("mystery", {"x": 1}) == set()
    assert state.apply_event("lane", {"no": "id"}) == set()
    assert state.apply_event("lane", "not an object") == set()


def test_status_words_are_honest():
    word = lambda status, effect=None: fmt.status_info(status, effect)[0]
    assert word("succeeded_verified") == "verified"
    assert word("succeeded-unverified") == "not verified"
    assert word("partial") == "partial"
    assert word("failed") == "failed"
    assert word("done", "commit") == "not verified"
    assert word("ok", "commit") == "not verified"
    assert word("ok", "read") == "ok"
    assert word("error") == "failed"
    assert word("waiting_approval") == "waiting for you"
    for status in ("done", "success", "succeeded", "completed", "ok", "weird_new_state"):
        assert word(status, "commit") != "done"


def test_times_render_in_the_node_timezone():
    tz = fmt.zone("Europe/Vilnius")
    at = datetime(2026, 11, 4, 11, 0, tzinfo=UTC)
    assert fmt.clock(at, tz, now=NOW) == "13:00"
    assert fmt.clock(at + timedelta(days=1), tz, now=NOW) == "Nov 5 13:00"
    assert fmt.until(at, NOW) == "in 3h"
    assert fmt.ago(NOW - timedelta(minutes=5), NOW) == "5m ago"


async def test_activity_filters(node):
    state = await loaded(node)
    failed = state.filtered_activity(status="failed")
    assert failed and all(fmt.status_info(e.status, e.effect)[0] == "failed" for e in failed)
    assert all(e.lane == "biz-c/medium" for e in state.filtered_activity(lane="biz-c/medium"))
    assert all(e.workspace == "biz-a" for e in state.filtered_activity(workspace="biz-a"))
    assert all(e.effect == "irreversible" for e in state.filtered_activity(effect="irreversible"))


# ---------------------------------------------------------------- sync
async def test_unconfigured_resources_do_not_block_the_rest(node):
    node.unavailable = {"approvals", "brains", "schedules"}
    state = await loaded(node)
    assert {n for n in RESOURCES if state.res[n].status == "unavailable"} == {"approvals", "brains", "tasks",
                                                                               "schedules"}
    assert state.res["workspaces"].ok and len(list(state.lanes())) == 31
    # the queue cannot be read, so the node's own count is shown
    assert state.needs_you_count() == 9


async def wait_for(cond, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


async def test_offline_keeps_data_then_recovers_and_reconnects(node):
    state = DashState()
    client = ApiClient("http://node.test", "demo", transport=node.transport())
    syncer = Syncer(client, state, lambda topics: None, status_every=0.1, full_every=0.5, first_backoff=0.05,
                    max_backoff=0.2)
    await syncer.start()
    try:
        await wait_for(lambda: state.conn.live == "live")
        node.go_down()
        await wait_for(lambda: state.conn.online is False)
        assert state.conn.reason == "connection refused"
        assert len(list(state.lanes())) == 31  # last known data stays
        node.come_back()
        await wait_for(lambda: state.conn.online and state.conn.live == "live" and node.subscribers == 1)
        node.emit("lane", {**node.lane("biz-a/medium"), "state": "warm"})
        await wait_for(lambda: state.lane("biz-a/medium").state == "warm")
    finally:
        await syncer.stop()
        await client.aclose()


async def test_wrong_token_marks_auth_failed(node):
    state = DashState()
    client = ApiClient("http://node.test", "nope", transport=node.transport())
    await Syncer(client, state, lambda topics: None).refresh()
    await client.aclose()
    assert state.conn.auth_failed and state.conn.online is True


async def test_events_off_falls_back_to_polling(node):
    node.events_enabled = False
    state = DashState()
    client = ApiClient("http://node.test", "demo", transport=node.transport())
    syncer = Syncer(client, state, lambda topics: None, status_every=0.05, full_every=0.1)
    await syncer.start()
    try:
        await wait_for(lambda: state.conn.live == "polling")
        node.lane("biz-a/blog")["state"] = "warm"  # changed behind our back, no event
        await wait_for(lambda: state.lane("biz-a/blog").state == "warm")
    finally:
        await syncer.stop()
        await client.aclose()


def test_no_em_dashes_in_dashboard_sources():
    root = Path(__file__).resolve().parents[2] / "src" / "agentos" / "tui"
    for path in root.rglob("*"):
        if path.suffix in (".py", ".tcss"):
            assert EM_DASH not in path.read_text(), path


def test_a_lane_at_a_checkpoint_needs_you():
    """A checkpoint (the site asks the account holder a question) is the lane that needs the person
    most; it was missing from the dash's Needs you."""
    from agentos.tui.models import Lane

    state = DashState()
    lane = Lane(id="biz/facebook", state="warm", signal="checkpoint")
    assert state.lane_need(lane) == "the site asks you to confirm it is you"
    assert state.lane_need(Lane(id="biz/reddit", state="warm", signal="ok")) is None


# ---------------------------------------------------------------- environments (2026-11-15 test, F11)
async def test_an_environment_is_named_by_its_title_and_kept_out_of_the_accounts(node):
    state = await loaded(node)
    assert state.res["environments"].ok
    assert [e.title for e in state.environments] == ["qa-env-2026-11-15", "pricing page copy"]
    open_one = state.environments[0]
    assert (open_one.ahead, open_one.pushed, open_one.proofs) == (2, 1, 3)
    assert state.place("env/esrkyfqerme") == "qa-env-2026-11-15"
    assert state.place("env/esrkyfqerme-browser") == "qa-env-2026-11-15 (browser)"
    assert state.place("biz-a/instagram") == "biz-a/instagram"
    assert "env" not in {ws.id for ws in state.account_workspaces()}


def test_a_rich_text_post_reads_as_its_text():
    from agentos.tui.fmt import readable

    doc = '{"document":[{"e":"par","c":[{"e":"text","t":"QA check of the approval gate."}]},{"e":"par","c":[{"e":"text","t":"Line two"}]}]}'
    assert readable(doc) == "QA check of the approval gate.\nLine two"
    assert readable(f'text: "{doc}"') == "text:\nQA check of the approval gate.\nLine two"
    assert readable("Autumn menu is here.") == "Autumn menu is here."
    assert readable("{not json") == "{not json"
