"""Lane details: everything about one lane, its control state, and its recent activity."""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.binding import Binding, BindingType
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from agentos.tui import fmt
from agentos.tui.activity import activity_cells, activity_columns
from agentos.tui.models import Lane
from agentos.tui.state import DashState
from agentos.tui.views import control_info, lane_actions, signal_info
from agentos.tui.widgets import Banner, TopBar, View


def lane_facts(state: DashState, lane: Lane) -> Text:
    t = Text()

    def row(label: str, value: str, style: str = fmt.TEXT) -> None:
        t.append(f"{label:<11}", style=fmt.MUTED)
        t.append(f"{value}\n", style=style)

    row("Kind", " ".join(x for x in (fmt.KIND_WORD.get(lane.kind, lane.kind), lane.engine) if x))
    if lane.preset:
        row("Preset", lane.preset)
    row("State", lane.state, fmt.LANE_STATE_STYLE.get(lane.state, fmt.TEXT))
    word, color = signal_info(state, lane)
    if word:
        row("Signal", word, color)
    if lane.logged_in_hint and lane.logged_in_hint != "unknown":
        row("Login", "ok" if lane.logged_in_hint == "ok" else "needs login",
            fmt.OK if lane.logged_in_hint == "ok" else fmt.WARN)
    if lane.proxy and (lane.proxy.label or lane.proxy.host):
        where = f"{lane.proxy.host}:{lane.proxy.port}" if lane.proxy.host else ""
        row("Proxy", " ".join(x for x in (lane.proxy.label, where) if x))
    if lane.exit_ip:
        matches = lane.proxy and lane.proxy.host and lane.proxy.host == lane.exit_ip
        row("Exit IP", lane.exit_ip + (" (matches the proxy)" if matches else ""),
            fmt.TEXT if matches or not lane.proxy else fmt.WARN)
    node = state.node(lane.node)
    if lane.node:
        row("Node", lane.node + (f" ({node.state})" if node and node.state != "up" else ""),
            fmt.BAD if node and node.state == "down" else fmt.TEXT)
    row("Last used", fmt.ago(lane.last_used))
    if lane.quarantine_reason:
        row("Quarantine", lane.quarantine_reason, fmt.BAD)
    if lane.viewer:
        row("Live view", "available, v opens it", fmt.OK)
    elif lane.state not in ("warm", "active"):
        row("Live view", "open the lane first (o)", fmt.DIM)
    return t


def lane_control(lane: Lane) -> Text:
    c = lane.control
    t = Text()
    if c.state == "agent":
        t.append("Agents have control\n", style=f"bold {fmt.TEXT}")
        t.append("Your live view is read-only. t takes control, s stops the agent.\n", style=fmt.MUTED)
    elif c.state == "human":
        t.append(f"You have control{f' ({c.by})' if c.by else ''}\n", style=f"bold {fmt.HUMAN}")
        t.append(f"since {fmt.ago(c.since, never='unknown')}", style=fmt.MUTED)
        if c.last_human_input:
            t.append(f", last input {fmt.ago(c.last_human_input)}", style=fmt.MUTED)
        t.append("\nAgent calls on this lane wait. h hands back.\n", style=fmt.MUTED)
        t.append("With no input for a while, control returns to agents on its own.\n", style=fmt.DIM)
    else:
        t.append("Agent stopped\n", style=f"bold {fmt.WARN}")
        t.append(f"paused by {c.by or 'a human'} {fmt.ago(c.since, never='')}\n", style=fmt.MUTED)
        t.append("Nothing runs here until you hand back (h).\n", style=fmt.MUTED)
    if c.takeback_requested:
        t.append("\nThe agent asks for control back", style=f"bold {fmt.WARN}")
        if c.takeback_by:
            t.append(f" ({c.takeback_by})", style=fmt.WARN)
        t.append("\n")
        if c.takeback_reason:
            t.append(f"\"{c.takeback_reason}\"\n", style=fmt.TEXT)
        t.append("h hands back, x refuses and keeps control.\n", style=fmt.MUTED)
    return t


class LaneView(View):
    TOPICS = frozenset({"workspaces", "activity", "nodes", "conn"})
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("o", "lane('open')", "Open"),
        Binding("c", "lane('close')", "Close"),
        Binding("t", "lane('take')", "Take control"),
        Binding("h", "lane('hand_back')", "Hand back"),
        Binding("s", "lane('stop_agent')", "Stop agent"),
        Binding("x", "lane('deny_request')", "Refuse request"),
        Binding("v", "lane('view')", "Viewer"),
        Binding("p", "lane('screenshot')", "Screenshot"),
    ]

    def __init__(self, lane_id: str, **kwargs):
        super().__init__(**kwargs)
        self.lane_id = lane_id
        self._shown: list[str] = []
        self._cols: list = []

    def compose(self):
        yield Static(id="lane-head", classes="lane-head")
        with Horizontal(id="lane-panels"):
            yield Static(id="lane-facts", classes="panel")
            yield Static(id="lane-control", classes="panel")
        yield Static("Recent activity", classes="section-title")
        yield DataTable(id="lane-activity", cursor_type="row")

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#lane-activity", DataTable).focus()

    @property
    def lane(self) -> Lane | None:
        return self.state.lane(self.lane_id)

    def refresh_view(self, topics: set[str]) -> None:
        state = self.state
        lane = self.lane
        head = self.query_one("#lane-head", Static)
        if lane is None:
            head.update(Text(f"{self.lane_id}: not on this node (or not loaded yet)", style=fmt.WARN))
            return
        ws = state.workspace(lane.workspace)
        t = Text()
        t.append(fmt.KIND_ICON.get(lane.kind, "·") + " ", style=fmt.MUTED)
        t.append(lane.id, style=f"bold {fmt.TEXT}")
        if ws and ws.name:
            t.append(f"  {ws.name}", style=fmt.MUTED)
        word, color = control_info(lane)
        t.append("   ")
        t.append(f" {word} ", style=f"bold #15171c on {color}" if lane.control.state != "agent" or
                 lane.control.takeback_requested else fmt.MUTED)
        head.update(t)
        self.query_one("#lane-facts", Static).update(lane_facts(state, lane))
        self.query_one("#lane-control", Static).update(lane_control(lane))
        self._fill_activity(state)
        self.refresh_bindings()

    def _fill_activity(self, state: DashState) -> None:
        table = self.query_one("#lane-activity", DataTable)
        width = self.size.width or self.app.size.width or 80
        cols = [c for c in activity_columns(width) if c[0] != "lane"]
        events = [e for e in state.activity if e.lane == self.lane_id][-200:]
        ids = [e.id for e in events]
        if cols != self._cols:
            table.clear(columns=True)
            for key, label, w in cols:
                table.add_column(label, width=w + (18 if key == "summary" else 0), key=key)
            self._cols = cols
            self._shown = []
        if ids[: len(self._shown)] != self._shown:
            table.clear()
            self._shown = []
        for e in events[len(self._shown):]:
            table.add_row(*activity_cells(state, e, cols), key=e.id)
        self._shown = ids
        if table.row_count:
            table.move_cursor(row=table.row_count - 1, animate=False)

    def on_resize(self) -> None:
        if self._cols:
            self._cols = []
            self.refresh_view({"workspaces"})

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "lane":
            return parameters[0] in lane_actions(self.lane)
        return True

    def action_lane(self, action: str) -> None:
        self.dash.lane_action(self.lane_id, action)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event_id = event.row_key.value if event.row_key else None
        e = next((x for x in self.state.activity if x.id == event_id), None)
        if e is not None and e.run_id:
            self.dash.show_run(e.run_id)


class LaneScreen(Screen):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "app.pop_screen", "Back")]

    def __init__(self, lane_id: str):
        super().__init__()
        self.lane_id = lane_id

    def compose(self):
        yield TopBar()
        yield Banner()
        yield LaneView(self.lane_id, id="lane-view")
        yield Footer()

    def on_mount(self) -> None:
        self.app.load_lane_detail(self.lane_id)  # type: ignore[attr-defined]
