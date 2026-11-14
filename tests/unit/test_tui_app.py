"""agentos dash, driven with Textual's Pilot against the in-process fake node: navigation,
every action hitting the right endpoint with the right body, live updates, degraded mode,
unconfigured sections, narrow terminals, and the token never reaching the screen."""

import time

import pytest
from textual.command import CommandInput, CommandPalette
from typer.testing import CliRunner

from agentos.tui.app import DashApp, browser_available
from agentos.tui.cli import app as cli_app
from agentos.tui.connect import Connection
from agentos.tui.demo import FakeNode
from agentos.tui.lane import LaneScreen, LaneView
from agentos.tui.modals import (
    AddScheduleModal,
    AnswerModal,
    ConfirmModal,
    DenyModal,
    FilterModal,
    HelpModal,
    LinkModal,
    RunModal,
)
from agentos.tui.views import ActivityView, BrainsView, LaneTree, NeedsView, RunsView
from agentos.tui.widgets import Banner, TopBar

TOKEN = "tok-3f9a-very-secret"
EM_DASH = "—"


class Browser:
    def __init__(self, works: bool):
        self.works = works
        self.opened: list[str] = []

    def __call__(self, url: str) -> bool:
        self.opened.append(url)
        return self.works


@pytest.fixture
def node():
    return FakeNode(token=TOKEN)


def make_app(node, tmp_path, *, browser: Browser | None = None, token: str = TOKEN) -> DashApp:
    return DashApp(Connection("http://node.test", token, "--url", "--token"), transport=node.transport(),
                   shots_dir=tmp_path / "shots", opener=browser or Browser(False),
                   status_every=5.0, full_every=30.0, first_backoff=0.1)


async def wait_until(pilot, cond, timeout: float = 4.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await pilot.pause(0.02)
    raise AssertionError("condition not met in time")


async def settle(pilot) -> None:
    app = pilot.app
    for _ in range(3):
        await pilot.pause()
        busy = [w for w in app.workers if w.group == "actions" and not w.is_finished]
        if busy:
            await app.workers.wait_for_complete(busy)
    await pilot.pause()


async def ready(pilot) -> None:
    app = pilot.app
    await wait_until(pilot, lambda: app.state.res["workspaces"].ok and app.state.conn.live == "live")
    await settle(pilot)


def plain(widget) -> str:
    content = widget.content
    return content.plain if hasattr(content, "plain") else str(content)


def screen_text(app) -> str:
    """Everything drawn on the screen, as plain text, from the SVG export."""
    import html
    import re
    svg = app.export_screenshot()
    return html.unescape(re.sub(r"<[^>]+>", "", svg)).replace("\xa0", " ")


async def open_lane(pilot, lane_id: str) -> LaneView:
    pilot.app.open_lane(lane_id)
    await wait_until(pilot, lambda: isinstance(pilot.app.screen, LaneScreen))
    await settle(pilot)
    return pilot.app.screen.query_one(LaneView)


async def needs_item(pilot, key: str) -> NeedsView:
    await pilot.press("2")
    await settle(pilot)
    view = pilot.app.query_one(NeedsView)
    view.focus_key(key)
    await settle(pilot)
    assert view.current() is not None and view.current().key == key
    return view


# ---------------------------------------------------------------- overview and navigation
async def test_overview_shows_workspaces_collapsed_to_health_lines(node, tmp_path):
    app = make_app(node, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(pilot)
        tree = app.query_one(LaneTree)
        ws_nodes = {n.data[1]: n for n in tree.root.children}
        assert list(ws_nodes) == ["biz-a", "biz-b", "biz-c", "biz-d", "personal"]
        assert not ws_nodes["biz-a"].is_expanded  # healthy: one calm line
        assert "6 lanes, all ok" in ws_nodes["biz-a"].label.plain
        assert ws_nodes["biz-b"].is_expanded  # needs attention: opens by itself
        shown = [c.data for c in ws_nodes["biz-b"].children]
        assert shown == [("lane", "biz-b/instagram"), ("lane", "biz-b/facebook"), ("more", "biz-b")]
        labels = {c.data[1]: c.label.plain for c in ws_nodes["biz-b"].children if c.data[0] == "lane"}
        assert "captcha" in labels["biz-b/instagram"] and "human (owner)" in labels["biz-b/facebook"]
        assert "wants control back" in ws_nodes["biz-c"].children[0].label.plain
        assert "Needs you 9" in plain(app.screen.query_one(TopBar))
        assert "node-2 down" in plain(app.query_one("#ov-nodes"))


async def test_expanding_a_workspace_shows_every_lane(node, tmp_path):
    app = make_app(node, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(pilot)
        tree = app.query_one(LaneTree)
        tree.focus()
        await pilot.press("enter")  # biz-a has the cursor
        await settle(pilot)
        biz_a = tree.root.children[0]
        assert biz_a.is_expanded and len(biz_a.children) == 6
        tree.show_all("biz-b")
        await settle(pilot)
        assert len(tree.root.children[1].children) == 6


async def test_number_keys_switch_tabs_and_help_opens(node, tmp_path):
    app = make_app(node, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(pilot)
        for key, tab in (("2", "needs"), ("3", "activity"), ("4", "runs"), ("5", "brains"), ("1", "overview")):
            await pilot.press(key)
            await settle(pilot)
            assert app.active_tab() == tab
        await pilot.press("question_mark")
        await settle(pilot)
        assert isinstance(app.screen, HelpModal)
        await pilot.press("2")  # digits do nothing behind a dialog
        assert app.active_tab() == "overview"
        await pilot.press("escape")
        await settle(pilot)
        assert not isinstance(app.screen, HelpModal)


async def test_enter_opens_lane_details_and_escape_returns(node, tmp_path):
    app = make_app(node, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(pilot)
        tree = app.query_one(LaneTree)
        tree.focus()
        tree.focus_key(("lane", "biz-b/instagram"))
        await settle(pilot)
        await pilot.press("enter")
        await wait_until(pilot, lambda: isinstance(app.screen, LaneScreen))
        await settle(pilot)
        view = app.screen.query_one(LaneView)
        facts = plain(view.query_one("#lane-facts"))
        assert "captcha" in facts and "lt2 203.0.113.11:12323" in facts
        assert ("GET", "/api/lanes/biz-b/instagram") in [(c.method, c.path) for c in node.requests]
        assert view.query_one("#lane-activity").row_count >= 1
        await pilot.press("escape")
        await settle(pilot)
        assert not isinstance(app.screen, LaneScreen)


async def test_search_jumps_to_a_lane(node, tmp_path):
    app = make_app(node, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(pilot)
        await pilot.press("ctrl+k")
        await wait_until(pilot, lambda: isinstance(app.screen, CommandPalette)
                         and isinstance(app.focused, CommandInput))
        await pilot.press(*"biz-c/linkedin")
        await wait_until(pilot, lambda: app.focused.value == "biz-c/linkedin")
        await pilot.pause(0.4)
        await pilot.press("enter")
        await wait_until(pilot, lambda: isinstance(app.screen, LaneScreen))
        assert app.screen.lane_id == "biz-c/linkedin"


# ---------------------------------------------------------------- lane actions
async def test_lane_actions_hit_the_right_endpoints(node, tmp_path):
    app = make_app(node, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(pilot)
        await open_lane(pilot, "biz-a/medium")  # cold
        await pilot.press("o")
        await settle(pilot)
        assert node.calls("POST", "/api/lanes/biz-a/medium/open") == [{}]
        await wait_until(pilot, lambda: app.state.lane("biz-a/medium").state == "warm")
        await pilot.press("c")
        await settle(pilot)
        assert node.calls("POST", "/api/lanes/biz-a/medium/close") == [{}]

        await open_lane(pilot, "biz-b/facebook")  # the owner has it
        await pilot.press("h")
        await settle(pilot)
        assert node.calls("POST", "/api/lanes/biz-b/facebook/control") == [{"action": "hand_back"}]
        await wait_until(pilot, lambda: app.state.lane("biz-b/facebook").control.state == "agent")

        await open_lane(pilot, "biz-c/linkedin")  # the agent wants it back
        await pilot.press("x")
        await settle(pilot)
        assert node.calls("POST", "/api/lanes/biz-c/linkedin/control") == [{"action": "deny_request"}]
        await wait_until(pilot, lambda: not app.state.lane("biz-c/linkedin").control.takeback_requested)


async def test_actions_that_do_not_apply_are_not_offered(node, tmp_path):
    app = make_app(node, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(pilot)
        view = await open_lane(pilot, "biz-a/medium")  # cold, agents in control
        await pilot.press("h", "x", "v", "p")
        await settle(pilot)
        assert node.writes() == []
        assert view.check_action("lane", ("open",)) and not view.check_action("lane", ("close",))


async def test_take_control_opens_the_control_view_in_the_browser(node, tmp_path):
    browser = Browser(True)
    app = make_app(node, tmp_path, browser=browser)
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(pilot)
        await open_lane(pilot, "biz-b/instagram")
        await pilot.press("t")
        await settle(pilot)
        assert node.calls("POST", "/api/lanes/biz-b/instagram/control") == [{"action": "take"}]
        assert node.calls("POST", "/api/lanes/biz-b/instagram/view-link") == [{"mode": "control"}]
        assert len(browser.opened) == 1
        assert browser.opened[0].startswith("http://node.test/view/biz-b/instagram?t=")
        assert not isinstance(app.screen, LinkModal)
        assert app.state.lane("biz-b/instagram").control.state == "human"


async def test_viewer_link_is_shown_to_copy_when_no_browser(node, tmp_path):
    browser = Browser(False)
    app = make_app(node, tmp_path, browser=browser)
    async with app.run_test(size=(100, 30)) as pilot:
        await ready(pilot)
        await open_lane(pilot, "biz-a/instagram")
        await pilot.press("v")
        await settle(pilot)
        assert node.calls("POST", "/api/lanes/biz-a/instagram/view-link") == [{"mode": "view"}]
        assert isinstance(app.screen, LinkModal)
        assert app.screen.url.startswith("http://node.test/view/biz-a/instagram?t=")
        assert app.screen.url in screen_text(app).replace("\n", "")
        await pilot.press("c")
        assert app.clipboard == app.screen.url


async def test_stop_agent_asks_first(node, tmp_path):
    app = make_app(node, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(pilot)
        await open_lane(pilot, "biz-a/linkedin")
        await pilot.press("s")
        await settle(pilot)
        assert isinstance(app.screen, ConfirmModal)
        await pilot.press("n")
        await settle(pilot)
        assert node.calls("POST", "/api/lanes/biz-a/linkedin/control") == []
        await pilot.press("s")
        await settle(pilot)
        await pilot.press("y")
        await settle(pilot)
        assert node.calls("POST", "/api/lanes/biz-a/linkedin/control") == [{"action": "stop_agent"}]
        await wait_until(pilot, lambda: app.state.lane("biz-a/linkedin").control.state == "paused")
        await wait_until(pilot, lambda: app.state.runs["r_120"].status == "cancelled")


async def test_screenshot_is_saved_to_a_file(node, tmp_path):
    app = make_app(node, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(pilot)
        await open_lane(pilot, "biz-a/instagram")
        await pilot.press("p")
        await settle(pilot)
        shots = list((tmp_path / "shots").glob("biz-a-instagram-*.png"))
        assert len(shots) == 1 and shots[0].read_bytes().startswith(b"\x89PNG")


async def test_overview_keys_act_on_the_highlighted_lane(node, tmp_path):
    app = make_app(node, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(pilot)
        tree = app.query_one(LaneTree)
        tree.focus()
        tree.focus_key(("lane", "biz-b/facebook"))
        await settle(pilot)
        assert tree.highlighted_lane() == "biz-b/facebook"
        await pilot.press("h")
        await settle(pilot)
        assert node.calls("POST", "/api/lanes/biz-b/facebook/control") == [{"action": "hand_back"}]


# ---------------------------------------------------------------- Needs you
async def test_approve_and_deny_with_a_note(node, tmp_path):
    app = make_app(node, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(pilot)
        view = await needs_item(pilot, "approval:ap_7f3")
        detail = plain(view.query_one("#needs-detail"))
        assert "Post 1 photo with caption to instagram.com as biz-a" in detail
        assert "Autumn menu is here" in detail and "sha256:" in detail
        await pilot.press("a")
        await settle(pilot)
        assert node.calls("POST", "/api/approvals/ap_7f3/approve") == [{}]
        assert "approval:ap_7f3" not in view.items

        view.focus_key("approval:ap_81c")
        await settle(pilot)
        await pilot.press("d")
        await settle(pilot)
        assert isinstance(app.screen, DenyModal)
        await pilot.press(*"wrong tone", "enter")
        await settle(pilot)
        assert node.calls("POST", "/api/approvals/ap_81c/deny") == [{"note": "wrong tone"}]


async def test_risky_approval_asks_first(node, tmp_path):
    app = make_app(node, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(pilot)
        await needs_item(pilot, "approval:ap_a04")  # pay
        await pilot.press("a")
        await settle(pilot)
        assert isinstance(app.screen, ConfirmModal)
        assert node.calls("POST", "/api/approvals/ap_a04/approve") == []
        await pilot.press("y")
        await settle(pilot)
        assert node.calls("POST", "/api/approvals/ap_a04/approve") == [{}]


async def test_batch_approve_marked(node, tmp_path):
    app = make_app(node, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(pilot)
        view = await needs_item(pilot, "approval:ap_81c")
        await pilot.press("space")  # marks and moves on to the next post
        await settle(pilot)
        assert view.current().key == "approval:ap_7f3"
        await pilot.press("space", "A")
        await settle(pilot)
        assert isinstance(app.screen, ConfirmModal)
        await pilot.press("y")
        await settle(pilot)
        assert node.calls("POST", "/api/approvals/batch") == [{"ids": ["ap_81c", "ap_7f3"], "decision": "approve"}]
        assert not any(k in view.items for k in ("approval:ap_81c", "approval:ap_7f3"))


async def test_answer_a_question(node, tmp_path):
    app = make_app(node, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(pilot)
        await needs_item(pilot, "question:q_51")
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, AnswerModal)
        await pilot.click("#opt-2")
        await settle(pilot)
        assert node.calls("POST", "/api/questions/q_51/answer") == [{"answer": "dark"}]
        assert "q_51" not in app.state.questions


async def test_control_request_handed_back_from_the_queue(node, tmp_path):
    app = make_app(node, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(pilot)
        await needs_item(pilot, "control:biz-c/linkedin")
        await pilot.press("h")
        await settle(pilot)
        assert node.calls("POST", "/api/lanes/biz-c/linkedin/control") == [{"action": "hand_back"}]


async def test_preview_image_is_saved(node, tmp_path):
    app = make_app(node, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(pilot)
        await needs_item(pilot, "approval:ap_7f3")
        await pilot.press("i")
        await settle(pilot)
        assert (tmp_path / "shots" / "approval-ap_7f3.png").read_bytes().startswith(b"\x89PNG")


# ---------------------------------------------------------------- Activity
async def test_activity_filter_and_follow(node, tmp_path):
    app = make_app(node, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(pilot)
        await pilot.press("3")
        await settle(pilot)
        view = app.query_one(ActivityView)
        everything = view.table.row_count
        assert everything == len(app.state.activity)
        await pilot.press("f")
        await settle(pilot)
        assert isinstance(app.screen, FilterModal)
        app.screen.query_one("#f-status").value = "failed"
        await pilot.click("#apply")
        await settle(pilot)
        assert view.filter.status == "failed"
        statuses = [str(cell) for cell in view.table.get_column("status")]
        assert statuses and all("failed" in s for s in statuses)
        await pilot.press("c")
        await settle(pilot)
        assert view.table.row_count == everything
        assert view.follow
        await pilot.press("F")
        assert not view.follow


async def test_live_events_reach_the_screens(node, tmp_path):
    app = make_app(node, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(pilot)
        node.emit("lane", {**node.lane("biz-a/reddit"), "signal": "captcha"})
        await wait_until(pilot, lambda: app.query_one(LaneTree).root.children[0].is_expanded)
        assert "1 captcha" in app.query_one(LaneTree).root.children[0].label.plain

        await pilot.press("3")
        await settle(pilot)
        table = app.query_one(ActivityView).table
        before = table.row_count
        node.emit("trace", {"id": "ev_live", "at": "2030-01-01T00:00:00Z", "lane": "biz-a/reddit",
                            "tool": "browser_navigate", "status": "ok", "effect": "read", "summary": "live one"})
        await wait_until(pilot, lambda: table.row_count == before + 1)
        assert table.cursor_row == table.row_count - 1  # following

        node.emit("approval", {"id": "ap_new", "state": "pending", "effect": "commit", "lane": "biz-a/blog",
                               "summary": "Publish 1 post to biz-a.example as biz-a"})
        await wait_until(pilot, lambda: "Needs you 10" in plain(app.screen.query_one(TopBar)))


# ---------------------------------------------------------------- Runs and schedules
async def test_runs_and_schedule_actions(node, tmp_path):
    browser = Browser(True)
    app = make_app(node, tmp_path, browser=browser)
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(pilot)
        await pilot.press("4")
        await settle(pilot)
        view = app.query_one(RunsView)
        runs = view.runs_table
        statuses = [str(runs.get_row_at(i)[3]) for i in range(runs.row_count)]
        assert any("verified" in s for s in statuses) and any("failed" in s for s in statuses)
        assert not any(s.strip().endswith("done") for s in statuses)

        runs.move_cursor(row=runs.get_row_index("r_117"))
        await settle(pilot)
        await pilot.press("o")
        await settle(pilot)
        assert browser.opened == ["https://www.instagram.com/p/C9xYz1/"]
        await pilot.press("enter")
        await settle(pilot)
        assert isinstance(app.screen, RunModal)
        assert ("GET", "/api/runs/r_117") in [(c.method, c.path) for c in node.requests]
        await pilot.press("escape")
        await settle(pilot)

        sched = view.sched_table
        sched.focus()
        sched.move_cursor(row=sched.get_row_index("s_1"))
        await settle(pilot)
        await pilot.press("e")
        await settle(pilot)
        assert node.calls("PATCH", "/api/schedules/s_1") == [{"enabled": False}]
        assert app.state.schedules["s_1"].enabled is False

        sched.move_cursor(row=sched.get_row_index("s_2"))
        await settle(pilot)
        await pilot.press("n")
        await settle(pilot)
        await pilot.press("y")
        await settle(pilot)
        assert node.calls("POST", "/api/flows/biz-a.linkedin.post/run") == [{"params": {}}]

        await pilot.press("a")
        await settle(pilot)
        assert isinstance(app.screen, AddScheduleModal)
        modal = app.screen
        modal.query_one("#s-flow").value = "biz-a.reddit.reply"
        modal.query_one("#s-cron").value = "0 9 * * 1"  # one field short of five? no: five fields
        modal.query_one("#s-params").value = "text=Thanks for asking, thread_url=https://reddit.com/r/x"
        await pilot.click("#add")
        await settle(pilot)
        assert node.calls("POST", "/api/schedules") == [{
            "flow": "biz-a.reddit.reply", "cron": "0 9 * * 1",
            "params": {"text": "Thanks for asking", "thread_url": "https://reddit.com/r/x"}}]
        assert not isinstance(app.screen, AddScheduleModal)


async def test_add_schedule_validates_before_sending(node, tmp_path):
    app = make_app(node, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(pilot)
        await pilot.press("4")
        await settle(pilot)
        await pilot.press("a")
        await settle(pilot)
        modal = app.screen
        modal.query_one("#s-flow").value = "biz-a.reddit.reply"
        modal.query_one("#s-cron").value = "every morning"
        await pilot.click("#add")
        await settle(pilot)
        assert isinstance(app.screen, AddScheduleModal)
        assert "5 fields" in plain(modal.query_one("#s-error"))
        assert node.calls("POST", "/api/schedules") == []


# ---------------------------------------------------------------- Brains
async def test_brains_and_task_tail(node, tmp_path):
    app = make_app(node, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(pilot)
        await pilot.press("5")
        await settle(pilot)
        view = app.query_one(BrainsView)
        brains = view.query_one("#brains-table")
        assert brains.row_count == 4
        assert "cooling down" in str(brains.get_row("codex-pro"))
        tasks = view.tasks_table
        tasks.move_cursor(row=tasks.get_row_index("t_31"))
        await wait_until(pilot, lambda: "browser_act click Start a post" in plain(view.query_one("#task-tail")))
        assert ("GET", "/api/brains/tasks/t_31") in [(c.method, c.path) for c in node.requests]


# ---------------------------------------------------------------- degraded and partial nodes
async def test_unreachable_node_shows_banner_keeps_data_and_recovers(node, tmp_path):
    app = make_app(node, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await ready(pilot)
        node.go_down()
        await pilot.press("r")
        await wait_until(pilot, lambda: app.state.conn.online is False)
        await settle(pilot)
        banner = app.screen.query_one(Banner)
        assert banner.has_class("show")
        text = plain(banner)
        assert "Node unreachable at http://node.test: connection refused" in text
        assert "Showing what it said at" in text
        assert "offline" in plain(app.screen.query_one(TopBar))
        assert len(app.query_one(LaneTree).root.children) == 5  # last known data stays

        await open_lane(pilot, "biz-a/instagram")
        await pilot.press("c")
        await settle(pilot)
        assert node.calls("POST", "/api/lanes/biz-a/instagram/close") == []  # never reached the node

        node.come_back()
        await pilot.press("r")
        await wait_until(pilot, lambda: app.state.conn.online and app.state.conn.live == "live")
        await settle(pilot)
        assert not app.screen.query_one(Banner).has_class("show")


async def test_node_unreachable_from_the_start(node, tmp_path):
    node.go_down()
    app = make_app(node, tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await wait_until(pilot, lambda: app.state.conn.online is False)
        await settle(pilot)
        assert "Nothing loaded yet" in plain(app.screen.query_one(Banner))
        assert "waiting for the node" in plain(app.query_one("#ov-ws-note"))


async def test_unconfigured_sections_say_so(node, tmp_path):
    node.unavailable = {"approvals", "questions", "brains", "schedules", "activity"}
    app = make_app(node, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_until(pilot, lambda: app.state.res["workspaces"].ok)
        await settle(pilot)
        await pilot.press("2")
        await settle(pilot)
        note = plain(app.query_one("#needs-note"))
        assert "Approvals: not configured on this node." in note
        assert "Questions: not configured on this node." in note
        await pilot.press("3")
        await settle(pilot)
        assert "Activity: not configured on this node." in plain(app.query_one("#act-note"))
        await pilot.press("4")
        await settle(pilot)
        assert "Schedules: not configured on this node." in plain(app.query_one("#sched-note"))
        await pilot.press("5")
        await settle(pilot)
        assert "Brains: not configured on this node." in plain(app.query_one("#brains-note"))
        assert "Needs you 9" in plain(app.screen.query_one(TopBar))  # the node's own count


async def test_refused_token_says_so(node, tmp_path):
    app = make_app(node, tmp_path, token="not-the-token")
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_until(pilot, lambda: app.state.conn.auth_failed)
        await settle(pilot)
        assert "refused the token" in plain(app.screen.query_one(Banner))
        assert "token refused" in plain(app.screen.query_one(TopBar))


# ---------------------------------------------------------------- narrow terminals and hygiene
@pytest.mark.parametrize("size", [(80, 24), (100, 30), (160, 50)])
async def test_every_screen_renders_at_any_width(node, tmp_path, size):
    app = make_app(node, tmp_path)
    async with app.run_test(size=size) as pilot:
        await ready(pilot)
        texts = []
        for key in ("1", "2", "3", "4", "5"):
            await pilot.press(key)
            await settle(pilot)
            texts.append(screen_text(app))
        await open_lane(pilot, "biz-c/linkedin")
        texts.append(screen_text(app))
        await pilot.press("question_mark")
        await settle(pilot)
        texts.append(screen_text(app))
        for text in texts:
            assert EM_DASH not in text
            assert TOKEN not in text
        assert app.screen_stack[0].query_one(TopBar).size.height == 1
        if size[0] == 80:
            assert app.screen_stack[0].has_class("-narrow")


def test_browser_is_never_opened_over_ssh():
    assert not browser_available({"SSH_CONNECTION": "1.2.3.4 22 5.6.7.8 22"}, "darwin")
    assert not browser_available({}, "linux")
    assert browser_available({"DISPLAY": ":0"}, "linux")
    assert browser_available({}, "darwin")


def test_cli_needs_a_token(tmp_path, monkeypatch):
    for var in ("AGENTOS_URL", "AGENTOS_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # no `agentos connect` connections here
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    result = CliRunner().invoke(cli_app, [])
    assert result.exit_code == 2
    assert "no credential" in result.output and "agentos connect" in result.output and EM_DASH not in result.output
    help_text = CliRunner().invoke(cli_app, ["--help"]).output
    assert "--url" in help_text and "--demo" in help_text and EM_DASH not in help_text
