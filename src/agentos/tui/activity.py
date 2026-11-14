"""The dashboard's Activity: the action log as a table that follows new rows."""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual.binding import Binding, BindingType
from textual.widgets import DataTable, Static

from agentos.tui import fmt
from agentos.tui.models import TraceEvent
from agentos.tui.state import ActivityFilter, DashState
from agentos.tui.views import Table
from agentos.tui.widgets import View, resource_note


def activity_columns(width: int) -> list[tuple[str, str, int]]:
    """(key, label, width) for the activity table at this terminal width."""
    cols = [("at", "Time", 11), ("lane", "Lane", 18), ("status", "Status", 18)]
    if width >= 110:
        cols.insert(2, ("tool", "What", 16))
        cols.insert(3, ("effect", "Effect", 12))
    if width >= 140:
        cols.insert(2, ("who", "Who", 12))
        cols.append(("cost", "Cost", 8))
    used = sum(w + 2 for _, _, w in cols) + 3
    cols.append(("summary", "Summary", max(16, width - used)))
    return cols


def activity_cells(state: DashState, e: TraceEvent, cols: list[tuple[str, str, int]]) -> list[Text]:
    out = []
    for key, _, width in cols:
        if key == "at":
            out.append(Text(fmt.clock(e.at, state.tz, seconds=False) if e.at else "", style=fmt.MUTED))
        elif key == "lane":
            out.append(Text(fmt.clip(e.lane or (e.kind or ""), width), style=fmt.TEXT))
        elif key == "who":
            out.append(Text(fmt.clip(e.principal or "", width), style=fmt.MUTED))
        elif key == "tool":
            out.append(Text(fmt.clip(e.tool or e.kind or "", width), style=fmt.MUTED))
        elif key == "effect":
            effect = fmt.norm(e.effect)
            out.append(Text(effect.replace("_", " "), style=fmt.EFFECT_STYLE.get(effect, fmt.MUTED)))
        elif key == "status":
            out.append(fmt.status_text(e.status, e.effect))
        elif key == "cost":
            out.append(Text(fmt.usd(e.cost.usd) if e.cost else "", style=fmt.MUTED))
        else:
            out.append(Text(fmt.clip(fmt.one_line(e.summary), width), style=fmt.TEXT))
    return out


class ActivityView(View):
    TOPICS = frozenset({"activity", "conn"})
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("f", "filter", "Filter"),
        Binding("c", "clear_filter", "Clear filter"),
        Binding("F", "follow", "Follow"),
        Binding("end", "follow_on", "Newest", show=False),
    ]

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self.filter = ActivityFilter()
        self.follow = True
        self._shown: list[str] = []
        self._cols: list[tuple[str, str, int]] = []
        self._moving = False

    def compose(self):
        yield Static(id="act-bar", classes="filter-bar")
        yield Static(id="act-note", classes="note")
        yield Table(id="act-table")

    @property
    def table(self) -> DataTable:
        return self.query_one("#act-table", DataTable)

    def _bar(self) -> Text:
        t = Text()
        t.append("Showing ", style=fmt.MUTED)
        t.append(self.filter.describe(), style=fmt.TEXT)
        t.append(fmt.SEP, style=fmt.DIM)
        if self.follow:
            t.append("following new events", style=fmt.OK)
        else:
            t.append("paused, F follows", style=fmt.WARN)
        return t

    def refresh_view(self, topics: set[str]) -> None:
        state = self.state
        self.query_one("#act-bar", Static).update(self._bar())
        note = resource_note(state, "activity")
        note_w = self.query_one("#act-note", Static)
        note_w.update(note or "")
        note_w.display = note is not None
        width = self.size.width or self.app.size.width or 80
        cols = activity_columns(width)
        f = self.filter
        events = state.filtered_activity(workspace=f.workspace, lane=f.lane, status=f.status, effect=f.effect)
        ids = [e.id for e in events]
        table = self.table
        if cols != self._cols:
            table.clear(columns=True)
            for key, label, w in cols:
                table.add_column(label, width=w, key=key)
            self._cols = cols
            self._shown = []
        if ids[: len(self._shown)] != self._shown:
            table.clear()
            self._shown = []
        for e in events[len(self._shown):]:
            table.add_row(*activity_cells(state, e, cols), key=e.id)
        self._shown = ids
        if self.follow and table.row_count:
            self._moving = True
            table.move_cursor(row=table.row_count - 1, animate=False)
            table.scroll_end(animate=False)
            self._moving = False

    def on_resize(self) -> None:
        if self._cols and activity_columns(self.size.width) != self._cols:
            self.refresh_view({"activity"})

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if self._moving or event.data_table is not self.table:
            return
        last = self.table.row_count - 1
        if event.cursor_row < last and self.follow:
            self.follow = False
            self.query_one("#act-bar", Static).update(self._bar())
        elif event.cursor_row == last and not self.follow:
            self.follow = True
            self.query_one("#act-bar", Static).update(self._bar())

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table is not self.table:
            return
        event_id = event.row_key.value if event.row_key else None
        e = next((x for x in self.state.activity if x.id == event_id), None)
        if e is None:
            return
        if e.run_id:
            self.dash.show_run(e.run_id)
        elif e.lane:
            self.dash.open_lane(e.lane)

    def action_filter(self) -> None:
        self.dash.edit_activity_filter(self)

    def set_filter(self, new: ActivityFilter) -> None:
        self.filter = new
        self.follow = True
        self._shown = []
        self.table.clear()
        self.refresh_view({"activity"})

    def action_clear_filter(self) -> None:
        self.set_filter(ActivityFilter())

    def action_follow(self) -> None:
        self.follow = not self.follow
        self.refresh_view({"activity"})

    def action_follow_on(self) -> None:
        self.follow = True
        self.refresh_view({"activity"})
