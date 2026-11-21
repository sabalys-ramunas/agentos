"""The Textual app behind `openajar dash`."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import webbrowser
from collections.abc import Awaitable, Callable
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, ClassVar

import httpx
from rich.text import Text
from textual.app import App
from textual.binding import Binding, BindingType
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Footer, Static, TabbedContent, TabPane

from openajar.tui import fmt
from openajar.tui.activity import ActivityView
from openajar.tui.brains import BrainsView
from openajar.tui.client import ApiClient, ApiError, NodeUnreachable
from openajar.tui.connect import Connection
from openajar.tui.envs import EnvironmentsView
from openajar.tui.lane import LaneScreen
from openajar.tui.modals import (
    AddScheduleModal,
    AnswerModal,
    ConfirmModal,
    DenyModal,
    FilterModal,
    HelpModal,
    LinkModal,
    RunModal,
)
from openajar.tui.models import (
    Approval,
    BrainTask,
    Question,
    Run,
    Schedule,
    TraceEvent,
    lenient_dt,
    parse_list,
    parse_one,
)
from openajar.tui.needs import NeedsView
from openajar.tui.overview import LaneTree, OverviewView
from openajar.tui.runs import RunsView
from openajar.tui.state import ActivityFilter, DashState, merge_lane_detail
from openajar.tui.sync import Syncer
from openajar.tui.widgets import Banner, TopBar, View

THEME = Theme(
    name="openajar", primary="#7aa6e6", secondary="#8a93a6", accent="#c39be8",
    warning=fmt.WARN, error=fmt.BAD, success=fmt.OK, foreground=fmt.TEXT,
    background="#111318", surface="#171a20", panel="#1d2129", dark=True,
)

FOOTER_KEYS = Text.assemble(("?", f"bold {fmt.ACCENT}"), (" Help  ", fmt.TEXT), ("q", f"bold {fmt.ACCENT}"),
                            (" Quit", fmt.TEXT))

TABS = ("overview", "environments", "needs", "activity", "runs", "brains")
TAB_FOCUS = {"overview": "#lane-tree", "environments": "#envs-table", "needs": "#needs-list", "activity": "#act-table",
             "runs": "#runs-table", "brains": "#tasks-table"}

CONTROL_DONE = {
    "take": "You have control of {lane}. Agent calls there wait until you hand back (h).",
    "hand_back": "Handed {lane} back to agents.",
    "stop_agent": "Stopped the agent on {lane}. Its runs and brain tasks there were cancelled.",
    "deny_request": "Refused the agent's request. You keep control of {lane}.",
}
ACTION_TITLES = {"open": "Open lane", "close": "Close lane", "take": "Take control", "hand_back": "Hand back",
                 "stop_agent": "Stop agent", "deny_request": "Refuse request", "view": "Live view",
                 "screenshot": "Screenshot"}


def browser_available(env: dict | None = None, platform: str | None = None) -> bool:
    """True when a local graphical browser is likely: never over SSH (a text browser
    would take over this terminal, and a GUI one would open on a screen nobody watches)."""
    env = os.environ if env is None else env
    platform = platform or sys.platform
    if env.get("SSH_CONNECTION") or env.get("SSH_TTY") or env.get("SSH_CLIENT"):
        return False
    if platform == "darwin" or platform.startswith("win"):
        return True
    return bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


def open_in_browser(url: str) -> bool:
    if not browser_available():
        return False
    try:
        return bool(webbrowser.open(url, new=2))
    except webbrowser.Error:
        return False


class DashCommands(Provider):
    """Search for Cmd/Ctrl-K and `/`: lanes, workspaces, Needs you items, runs and the tabs."""

    def _candidates(self) -> list[tuple[str, str, Callable[[], Any]]]:
        app: DashApp = self.app  # type: ignore[assignment]
        state = app.state
        out: list[tuple[str, str, Callable[[], Any]]] = [
            ("Go to Overview", "1", partial(app.show_tab, "overview")),
            ("Go to Needs you", "2", partial(app.show_tab, "needs")),
            ("Go to Activity", "3", partial(app.show_tab, "activity")),
            ("Go to Runs and schedules", "4", partial(app.show_tab, "runs")),
            ("Go to Brains", "5", partial(app.show_tab, "brains")),
            ("Go to Environments", "6", partial(app.show_tab, "environments")),
        ]
        for env in state.environments:  # found by its title, the one name a person knows it by
            out.append((env.title or env.id, f"environment, {env.state}", partial(app.show_tab, "environments")))
        for item in state.needs_you():
            out.append((item.title, "Needs you", partial(app.show_need, item.key)))
        for ws in state.workspaces:
            out.append((f"workspace {ws.id}", state.workspace_line(ws), partial(app.focus_workspace, ws.id)))
        for lane in state.lanes():
            if state.environment_of(lane.id) is not None:
                continue  # listed above by its environment's title
            out.append((lane.id, f"{lane.kind} lane, {lane.state}, control {lane.control.state}",
                        partial(app.open_lane, lane.id)))
        for run in state.sorted_runs()[:60]:
            word = fmt.status_info(run.status)[0]
            out.append((f"run {run.flow or ''} {run.id}", word, partial(app.show_run, run.id)))
        return out

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        wanted = query.strip().lower()
        for label, help_text, callback in self._candidates():
            score = matcher.match(label)
            if score <= 0:
                continue
            # an exact name wins, then names that start with what was typed, then fuzzy matches
            if label.lower() == wanted:
                score += 2000.0
            elif label.lower().startswith(wanted):
                score += 1000.0
            yield Hit(score, matcher.highlight(label), callback, text=label, help=help_text)

    async def discover(self) -> Hits:
        for label, help_text, callback in self._candidates()[:5]:
            yield DiscoveryHit(label, callback, text=label, help=help_text)
        app: DashApp = self.app  # type: ignore[assignment]
        for item in app.state.needs_you()[:8]:
            yield DiscoveryHit(item.title, partial(app.show_need, item.key), text=item.title, help="Needs you")


class DashApp(App):
    CSS_PATH = "dash.tcss"
    TITLE = "openajar dash"
    COMMANDS: ClassVar[set] = App.COMMANDS | {DashCommands}
    HORIZONTAL_BREAKPOINTS: ClassVar[list[tuple[int, str]]] = [(0, "-narrow"), (110, "-wide")]
    VERTICAL_BREAKPOINTS: ClassVar[list[tuple[int, str]]] = [(0, "-short"), (32, "-tall")]
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("1", "show('overview')", "Overview", show=False),
        Binding("2", "show('needs')", "Needs you", show=False),
        Binding("3", "show('activity')", "Activity", show=False),
        Binding("4", "show('runs')", "Runs", show=False),
        Binding("5", "show('brains')", "Brains", show=False),
        Binding("6", "show('environments')", "Environments", show=False),
        Binding("slash", "command_palette", "Search"),
        # our own palette bindings replace Textual's default one, so ctrl+k is listed here too
        Binding("ctrl+k", "command_palette", "Search", show=False),
        Binding("r", "refresh", "Refresh", show=False),
        # shown in their own cell at the footer's end (FOOTER_KEYS), so a long list of a view's keys never
        # pushes them off an 80-column terminal (2026-11-15 test, F11)
        Binding("question_mark", "help", "Help", show=False),
        Binding("q", "quit", "Quit", show=False),
    ]

    def __init__(self, connection: Connection, *, transport: httpx.AsyncBaseTransport | None = None,
                 shots_dir: Path | None = None, opener: Callable[[str], bool] | None = None,
                 status_every: float = 10.0, full_every: float = 30.0, first_backoff: float = 2.0):
        super().__init__()
        self.connection = connection
        self.client = ApiClient(connection.url, connection.token, transport=transport, session=connection.session,
                                home=connection.home)
        self.state = DashState()
        self.syncer = Syncer(self.client, self.state, self.on_state_change, status_every=status_every,
                             full_every=full_every, first_backoff=first_backoff)
        self.shots_dir = shots_dir or Path.home() / "openajar-shots"
        self.opener = opener or open_in_browser
        self._dirty: set[str] = set()
        self._flush_pending = False

    # ---- layout ----
    def compose(self):
        yield TopBar()
        yield Banner()
        with TabbedContent(initial="overview", id="tabs"):
            with TabPane("Overview", id="overview"):
                yield OverviewView(id="overview-view")
            with TabPane("Environments", id="environments"):
                yield EnvironmentsView(id="environments-view")
            with TabPane("Needs you", id="needs"):
                yield NeedsView(id="needs-view")
            with TabPane("Activity", id="activity"):
                yield ActivityView(id="activity-view")
            with TabPane("Runs", id="runs"):
                yield RunsView(id="runs-view")
            with TabPane("Brains", id="brains"):
                yield BrainsView(id="brains-view")
        with Horizontal(id="footer-row"):
            yield Footer()
            yield Static(FOOTER_KEYS, id="footer-keys")

    def on_mount(self) -> None:
        self.register_theme(THEME)
        self.theme = "openajar"
        self.run_worker(self._sync_main(), group="sync", exclusive=True, exit_on_error=False)
        self.query_one(LaneTree).focus()

    async def _sync_main(self) -> None:
        try:
            await self.syncer.start()
            await asyncio.Event().wait()
        finally:
            await self.syncer.stop()
            await self.client.aclose()

    # ---- redraws ----
    def on_state_change(self, topics: set[str]) -> None:
        """Called by the sync layer; coalesces bursts of events into one redraw."""
        self._dirty |= topics
        if not self._flush_pending:
            self._flush_pending = True
            self.call_later(self._redraw_now)

    def _redraw_now(self) -> None:
        self._flush_pending = False
        topics, self._dirty = self._dirty, set()
        for screen in self.screen_stack:
            for bar in screen.query(TopBar):
                bar.redraw()
            for banner in screen.query(Banner):
                banner.redraw()
            for view in screen.query(View):
                if view.wants(topics):
                    view.refresh_view(topics)
        n = self.state.needs_you_count()
        with contextlib.suppress(NoMatches):
            self.query_one(TabbedContent).get_tab("needs").label = f"Needs you {n}" if n else "Needs you"

    def changed(self, *topics: str) -> None:
        self.on_state_change(set(topics))

    # ---- navigation ----
    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # a dialog owns the keyboard: digits, r and ? must not act behind it
        return not (action in ("show", "refresh", "help", "command_palette")
                    and isinstance(self.screen, ModalScreen))

    def action_show(self, tab: str) -> None:
        self.show_tab(tab)

    def show_tab(self, tab: str) -> None:
        while len(self.screen_stack) > 1:
            self.pop_screen()
        tabs = self.query_one(TabbedContent)
        tabs.active = tab
        # move focus now, before the old pane hides: a reset focus could land in the old pane
        # and pull it back into view
        self._focus_tab(tab)

    def active_tab(self) -> str:
        try:
            return self.query_one(TabbedContent).active
        except NoMatches:
            return ""

    def _focus_tab(self, tab: str) -> None:
        with contextlib.suppress(NoMatches):
            self.query_one(TAB_FOCUS[tab]).focus()

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        pane = event.pane.id or ""
        if pane in TAB_FOCUS and not (self.focused is not None and event.pane in self.focused.ancestors):
            self._focus_tab(pane)

    def open_lane(self, lane_id: str) -> None:
        while len(self.screen_stack) > 1:
            self.pop_screen()
        self.push_screen(LaneScreen(lane_id))

    def focus_workspace(self, ws_id: str) -> None:
        self.show_tab("overview")
        tree = self.query_one(LaneTree)
        tree.focus_key(("ws", ws_id))
        tree.focus()

    def show_need(self, key: str) -> None:
        self.show_tab("needs")
        self.call_after_refresh(self.query_one(NeedsView).focus_key, key)

    def action_help(self) -> None:
        self.push_screen(HelpModal(self.connection.describe(), self.state.conn.live))

    def action_refresh(self) -> None:
        self.syncer.retry_now()
        self.run_worker(self.syncer.refresh(), group="actions")

    # ---- the one place actions talk to the node ----
    async def act(self, title: str, call: Awaitable[Any]) -> tuple[bool, Any]:
        try:
            return True, await call
        except NodeUnreachable as exc:
            self.syncer.went_offline(exc.message)
            self.changed("conn")
            if exc.sent:
                msg = ("The node stopped answering, so this may or may not have happened. "
                       "It will show here once the node answers again.")
            else:
                msg = "Not sent: the node did not answer."
            self.notify(msg, title=title, severity="error", markup=False)
        except ApiError as exc:
            if exc.status in (404, 405) and exc.code in ("error", "not_found"):
                message = "This node does not support that yet."
            else:
                message = exc.message
            self.notify(f"{message} ({exc.code})", title=f"{title}: refused", severity="error",
                        markup=False)
        return False, None

    def _fold_lane(self, result: Any) -> None:
        if isinstance(result, dict) and "/" in str(result.get("id", "")):
            merge_lane_detail(self.state, result)
            self.changed("workspaces")

    def lane_action(self, lane_id: str, action: str) -> None:
        if action == "stop_agent":
            rest = (" and cancels its runs and brain tasks there. Nothing runs on the lane until you "
                    "hand back.")
            body = Text.assemble(("This pauses the agent on ", fmt.TEXT), (lane_id, f"bold {fmt.TEXT}"),
                                 (rest, fmt.TEXT))

            def confirmed(ok: bool | None) -> None:
                if ok:
                    self.run_worker(self._lane_action(lane_id, "stop_agent"), group="actions")
            self.push_screen(ConfirmModal("Stop the agent?", body, "Stop agent", danger=True), confirmed)
            return
        self.run_worker(self._lane_action(lane_id, action), group="actions")

    async def _lane_action(self, lane_id: str, action: str) -> None:
        title = ACTION_TITLES.get(action, action)
        lane = self.state.lane(lane_id)
        if action == "open":
            ok, res = await self.act(title, self.client.lane_open(lane_id))
            if ok:
                self._fold_lane(res)
                new = self.state.lane(lane_id)
                self.notify(f"{lane_id} is {new.state if new else 'opening'}.", markup=False)
        elif action == "close":
            ok, res = await self.act(title, self.client.lane_close(lane_id))
            if ok:
                self._fold_lane(res)
                self.notify(f"Closed {lane_id}.", markup=False)
        elif action in CONTROL_DONE:
            ok, res = await self.act(title, self.client.lane_control(lane_id, action))
            if not ok:
                return
            self._fold_lane(res)
            self.notify(CONTROL_DONE[action].format(lane=lane_id), title=title, markup=False)
            if action == "take":
                lane = self.state.lane(lane_id)
                if lane is not None and lane.state in ("warm", "active"):
                    await self._open_viewer(lane_id, "control")
                else:
                    self.notify("The lane is cold. o opens it, then v shows the live view.", markup=False)
        elif action == "view":
            mode = "control" if lane is not None and lane.control.state == "human" else "view"
            await self._open_viewer(lane_id, mode)
        elif action == "screenshot":
            await self._save_lane_shot(lane_id)

    async def _open_viewer(self, lane_id: str, mode: str) -> None:
        ok, res = await self.act("Live view", self.client.view_link(lane_id, mode))
        if not ok or not isinstance(res, dict) or not res.get("url"):
            return
        url = self.client.absolute(str(res["url"]))
        what = "control" if mode == "control" else "read-only"
        note = ["Control: your keyboard and mouse reach the lane while you have control." if mode == "control"
                else "Read-only: you watch what the agent does. t takes control."]
        expires = lenient_dt(res.get("expires_at"))
        if expires:
            note.append(f"The link expires at {fmt.clock(expires, self.state.tz)} ({fmt.until(expires)}).")
        if self.connection.tunnel_hint:
            note.append(f"It points at this node. From your Mac: {self.connection.tunnel_hint}, then open it there.")
        await self._present_url(url, f"Live view of {lane_id} ({what})", " ".join(note),
                                opened_msg=f"Opened the {what} live view of {lane_id} in your browser.")

    async def _present_url(self, url: str, title: str, note: str, *, opened_msg: str) -> None:
        try:
            opened = await asyncio.to_thread(self.opener, url)
        except Exception:  # noqa: BLE001 - any opener failure just means: show the link instead
            opened = False
        if opened:
            self.notify(opened_msg, markup=False)
        else:
            self.push_screen(LinkModal(title, url, note))

    def open_url(self, url: str, *, new_tab: bool = True) -> None:
        """Textual's own link opener, routed through the SSH-safe path."""
        self.show_link(url, url)

    def show_link(self, url: str, title: str) -> None:
        self.run_worker(self._present_url(url, title, "No local browser here, so copy the link.",
                                          opened_msg=f"Opened {url} in your browser."), group="actions")

    async def _save(self, name: str, data: bytes) -> Path:
        path = self.shots_dir / name

        def write() -> None:
            self.shots_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        await asyncio.to_thread(write)
        return path

    async def _save_lane_shot(self, lane_id: str) -> None:
        ok, data = await self.act("Screenshot", self.client.screenshot(lane_id))
        if not ok:
            return
        ws, name = lane_id.split("/", 1)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = await self._save(f"{ws}-{name}-{stamp}.png", data)
        self.notify(f"Saved a screenshot of {lane_id} to {path}", title="Screenshot", markup=False)

    def load_lane_detail(self, lane_id: str) -> None:
        async def load() -> None:
            try:
                merge_lane_detail(self.state, await self.client.lane(lane_id))
                events = parse_list(TraceEvent, await self.client.activity(lane=lane_id, limit=100))
            except NodeUnreachable as exc:
                self.syncer.went_offline(exc.message)
                self.changed("conn")
                return
            except ApiError:
                return
            for e in sorted(events, key=lambda e: e.at.timestamp() if e.at else 0.0):
                self.state.add_trace(e)
            self.state.activity.sort(key=lambda e: e.at.timestamp() if e.at else 0.0)
            self.changed("workspaces", "activity")
        self.run_worker(load(), group="actions")

    # ---- Needs you ----
    def decide(self, approvals: list[Approval], decision: str, *, batch: bool = False) -> None:
        if not approvals:
            return
        risky = [a for a in approvals if fmt.norm(a.effect) in ("pay", "irreversible")]
        run = partial(self._decide, approvals, decision, batch)

        if decision == "deny" and not batch:
            def denied(note: str | None) -> None:
                if note is not None:
                    self.run_worker(self._decide(approvals, "deny", False, note or None), group="actions")
            self.push_screen(DenyModal(approvals[0].summary), denied)
            return
        if batch or (decision == "approve" and risky):
            body = Text()
            for a in approvals[:12]:
                body.append(f"{fmt.norm(a.effect).replace('_', ' ')}: ", style=fmt.EFFECT_STYLE.get(
                    fmt.norm(a.effect), fmt.MUTED))
                body.append(f"{a.summary}\n", style=fmt.TEXT)
            if len(approvals) > 12:
                body.append(f"and {len(approvals) - 12} more\n", style=fmt.MUTED)
            if risky and decision == "approve":
                body.append("\nThis includes effects that cannot be undone.", style=fmt.WARN)
            verb = "Approve" if decision == "approve" else "Deny"
            title = f"{verb} {fmt.plural(len(approvals), 'approval')}?"

            def confirmed(ok: bool | None) -> None:
                if ok:
                    self.run_worker(run(), group="actions")
            self.push_screen(ConfirmModal(title, body, verb, danger=bool(risky) or decision == "deny"), confirmed)
            return
        self.run_worker(run(), group="actions")

    async def _decide(self, approvals: list[Approval], decision: str, batch: bool,
                      note: str | None = None) -> None:
        if batch:
            call = self.client.batch_decide([a.id for a in approvals], decision)
        elif decision == "approve":
            call = self.client.approve(approvals[0].id, note)
        else:
            call = self.client.deny(approvals[0].id, note)
        ok, _ = await self.act("Approve" if decision == "approve" else "Deny", call)
        if not ok:
            return
        for a in approvals:
            self.state.approvals.pop(a.id, None)
        self.changed("approvals")
        verb = "Approved" if decision == "approve" else "Denied"
        if len(approvals) == 1:
            self.notify(f"{verb}: {approvals[0].summary}", markup=False)
        else:
            self.notify(f"{verb} {len(approvals)} approvals.", markup=False)

    def answer_question(self, question: Question) -> None:
        def answered(text: str | None) -> None:
            if text:
                self.run_worker(self._answer(question, text), group="actions")
        self.push_screen(AnswerModal(question), answered)

    async def _answer(self, question: Question, text: str) -> None:
        ok, _ = await self.act("Answer", self.client.answer(question.id, text))
        if ok:
            self.state.questions.pop(question.id, None)
            self.changed("questions")
            who = question.asked_by.id if question.asked_by and question.asked_by.id else "the agent"
            self.notify(f"Sent your answer to {who}.", markup=False)

    def save_preview_image(self, approval: Approval) -> None:
        async def save() -> None:
            if not approval.preview or not approval.preview.image:
                return
            ok, data = await self.act("Preview image", self.client.fetch_bytes(approval.preview.image))
            if ok:
                path = await self._save(f"approval-{approval.id}.png", data)
                self.notify(f"Saved the preview image to {path}", markup=False)
        self.run_worker(save(), group="actions")

    # ---- activity ----
    def edit_activity_filter(self, view: ActivityView) -> None:
        def chosen(new: ActivityFilter | None) -> None:
            if new is None:
                return
            view.set_filter(new)
            if new.lane or new.workspace:
                self.run_worker(self._load_filtered(new), group="actions")
        self.push_screen(FilterModal(self.state, view.filter), chosen)

    async def _load_filtered(self, f: ActivityFilter) -> None:
        ok, data = await self.act("Activity", self.client.activity(lane=f.lane, workspace=f.workspace, limit=300))
        if ok:
            for e in parse_list(TraceEvent, data):
                self.state.add_trace(e)
            self.state.activity.sort(key=lambda e: e.at.timestamp() if e.at else 0.0)
            self.changed("activity")

    # ---- runs and schedules ----
    def show_run(self, run_id: str) -> None:
        run = self.state.runs.get(run_id) or Run(id=run_id, status="unknown")
        modal = RunModal(self.state, run, self.show_link)
        self.push_screen(modal)

        async def load() -> None:
            try:
                full = parse_one(Run, await self.client.run(run_id))
            except (NodeUnreachable, ApiError):
                return
            if full is not None:
                self.state.runs[run_id] = full
                if modal.is_attached:
                    modal.update_run(full)
        self.run_worker(load(), group="actions")

    def set_schedule_enabled(self, schedule: Schedule, enabled: bool) -> None:
        async def go() -> None:
            ok, res = await self.act("Schedule", self.client.set_schedule_enabled(schedule.id, enabled))
            if not ok:
                return
            updated = parse_one(Schedule, res) if isinstance(res, dict) else None
            self.state.schedules[schedule.id] = updated or schedule.model_copy(update={"enabled": enabled})
            self.changed("schedules")
            self.notify(f"{'Enabled' if enabled else 'Disabled'} the schedule for {schedule.flow}.", markup=False)
        self.run_worker(go(), group="actions")

    def run_schedule_now(self, schedule: Schedule) -> None:
        params = ", ".join(f"{k}={v}" for k, v in schedule.params.items()) or "no params"

        def confirmed(ok: bool | None) -> None:
            if ok:
                self.run_worker(self._run_flow(schedule.flow, schedule.params), group="actions")
        self.push_screen(ConfirmModal(f"Run {schedule.flow} now?",
                                      f"Runs the flow once with the schedule's params ({params}). Anything it "
                                      "would publish still goes through Needs you.", "Run now"), confirmed)

    async def _run_flow(self, flow: str, params: dict) -> None:
        ok, res = await self.act("Run now", self.client.run_flow(flow, params))
        if ok:
            run = parse_one(Run, res) if isinstance(res, dict) else None
            if run is not None:
                self.state.runs[run.id] = run
                self.changed("runs")
            self.notify(f"Started {flow}" + (f" as {run.id}." if run else "."), markup=False)

    def add_schedule(self) -> None:
        self.push_screen(AddScheduleModal(self.state, self._submit_schedule))

    async def _submit_schedule(self, flow: str, cron: str, params: dict) -> str | None:
        try:
            res = await self.client.add_schedule(flow, cron, params)
        except NodeUnreachable as exc:
            self.syncer.went_offline(exc.message)
            self.changed("conn")
            return "The node did not answer, so the schedule was not added."
        except ApiError as exc:
            return f"The node refused: {exc.message}"
        schedule = parse_one(Schedule, res) if isinstance(res, dict) else None
        if schedule is not None:
            self.state.schedules[schedule.id] = schedule
            self.changed("schedules")
        self.notify(f"Scheduled {flow} at {cron} ({self.state.tz_name}).", markup=False)
        return None

    def delete_schedule(self, schedule: Schedule) -> None:
        async def go() -> None:
            ok, _ = await self.act("Delete schedule", self.client.delete_schedule(schedule.id))
            if ok:
                self.state.schedules.pop(schedule.id, None)
                self.changed("schedules")
                self.notify(f"Removed the schedule for {schedule.flow}.", markup=False)

        def confirmed(ok: bool | None) -> None:
            if ok:
                self.run_worker(go(), group="actions")
        self.push_screen(ConfirmModal("Remove this schedule?", f"{schedule.flow} at "
                                      f"{schedule.cron or 'its watch'} stops firing.", "Remove", danger=True),
                         confirmed)

    # ---- brains ----
    def load_task(self, task_id: str) -> None:
        async def load() -> None:
            try:
                task = parse_one(BrainTask, await self.client.brain_task(task_id))
            except (NodeUnreachable, ApiError):
                return
            if task is not None:
                self.state.tasks[task_id] = task
                self.changed("tasks")
        self.run_worker(load(), group="actions")

