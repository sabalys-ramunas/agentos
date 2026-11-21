"""The dashboard's Brains: the models that do the node's own jobs, in order, and their tasks."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.widgets import DataTable, Static

from openajar.tui import fmt
from openajar.tui.views import _cursor_key, _restore_cursor
from openajar.tui.widgets import Pane, View, resource_note


class BrainsView(View):
    TOPICS = frozenset({"brains", "tasks", "conn"})

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self.task_id: str | None = None
        self._asked: set[str] = set()

    def compose(self):
        yield Static("Brains", classes="section-title")
        yield Static(id="brains-note", classes="note")
        yield DataTable(id="brains-table", cursor_type="row")
        yield Static("Tasks", classes="section-title")
        yield Static(id="tasks-note", classes="note")
        yield DataTable(id="tasks-table", cursor_type="row")
        yield Static(id="tail-title", classes="section-title")
        with Pane(id="tail-box"):
            yield Static(id="task-tail")

    def on_mount(self) -> None:
        super().on_mount()
        self.set_interval(5.0, self._poll_tail)

    @property
    def tasks_table(self) -> DataTable:
        return self.query_one("#tasks-table", DataTable)

    def refresh_view(self, topics: set[str]) -> None:
        state = self.state
        width = self.size.width or self.app.size.width or 80
        for key, note_id in (("brains", "#brains-note"), ("tasks", "#tasks-note")):
            note = resource_note(state, key)
            w = self.query_one(note_id, Static)
            w.update(note or "")
            w.display = note is not None
        wide = width >= 110
        table = self.query_one("#brains-table", DataTable)
        keep = _cursor_key(table)
        table.clear(columns=True)
        table.add_column("Brain", width=16)
        if wide:
            table.add_column("Kind", width=14)
            table.add_column("Provider", width=11)
        table.add_column("State", width=14)
        table.add_column("Seats", width=9)
        table.add_column("Window used", width=16)
        for b in state.brains:
            word, color = fmt.BRAIN_STATE.get(fmt.norm(b.state), (b.state.replace("_", " "), fmt.MUTED))
            cells = [Text(b.name, style=fmt.TEXT)]
            if wide:
                cells += [Text(b.kind or "", style=fmt.MUTED), Text(b.provider or "", style=fmt.MUTED)]
            seats = f"{b.seats.in_use} of {b.seats.total}" if b.seats and b.seats.total else ""
            cells += [Text(word, style=color), Text(seats, style=fmt.TEXT), fmt.bar(b.window_used_pct, 8)]
            table.add_row(*cells, key=b.name)
        _restore_cursor(table, keep)

        tasks = self.tasks_table
        keep = _cursor_key(tasks)
        tasks.clear(columns=True)
        tasks.add_column("Started", width=8)
        tasks.add_column("Brain", width=13)
        tasks.add_column("Lane", width=17)
        tasks.add_column("Status", width=16)
        tasks.add_column("Task", width=max(16, width - (8 + 13 + 17 + 16 + 14)))
        ordered = sorted(state.tasks.values(), reverse=True,
                         key=lambda t: t.started_at.timestamp() if t.started_at else float("inf"))
        for task in ordered:
            tasks.add_row(Text(fmt.clock(task.started_at, state.tz), style=fmt.MUTED),
                          Text(task.brain or "", style=fmt.TEXT), Text(task.lane or "", style=fmt.MUTED),
                          fmt.status_text(task.status), Text(fmt.clip(task.summary, 60), style=fmt.TEXT),
                          key=task.id)
        _restore_cursor(tasks, keep)
        self._show_tail()

    def _show_tail(self) -> None:
        task_id = _cursor_key(self.tasks_table)
        self.task_id = task_id
        title = self.query_one("#tail-title", Static)
        body = self.query_one("#task-tail", Static)
        task = self.state.tasks.get(task_id) if task_id else None
        if task is None:
            title.update(Text("Tail", style=f"bold {fmt.MUTED}"))
            body.update(Text("Pick a task to see the end of its transcript.", style=fmt.MUTED))
            return
        title.update(Text.assemble(("Tail ", f"bold {fmt.MUTED}"), (task.id, fmt.DIM)))
        if task.tail is None and task.id not in self._asked:
            self._asked.add(task.id)  # once; a failing load must not loop
            self.dash.load_task(task.id)
        lines = task.tail_lines
        if not lines:
            body.update(Text("Loading…" if task.tail is None else "No output yet.", style=fmt.MUTED))
        else:
            body.update(Text("\n".join(lines[-200:]), style=fmt.TEXT))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table is self.tasks_table:
            key = event.row_key.value if event.row_key else None
            if key != self.task_id:
                self._show_tail()
                if key:
                    self.dash.load_task(key)

    def _poll_tail(self) -> None:
        task = self.state.tasks.get(self.task_id) if self.task_id else None
        if task is None or fmt.norm(task.status) not in ("running", "waiting", "queued"):
            return
        if self.screen is self.app.screen and self.dash.active_tab() == "brains":
            self.dash.load_task(task.id)
