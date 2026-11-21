"""The dashboard's Runs and schedules: flow runs with their proof, and what is set to run."""

from __future__ import annotations

import json
from typing import ClassVar

from rich.text import Text
from textual.binding import Binding, BindingType
from textual.widgets import DataTable, Static

from openajar.tui import fmt
from openajar.tui.state import DashState
from openajar.tui.views import Table, _cursor_key, _restore_cursor
from openajar.tui.widgets import View, resource_note


def proof_word(run, width: int = 30) -> Text:
    if run.proof and run.proof.url:
        url = run.proof.url.split("://", 1)[-1].removeprefix("www.")
        return Text(fmt.clip(url, width), style=fmt.ACCENT)
    if fmt.norm(run.status) == "succeeded_unverified":
        return Text("no proof yet", style=fmt.WARN)
    return Text("", style=fmt.DIM)


def schedule_when(s) -> str:
    if s.cron:
        return s.cron
    watch = s.watch
    if isinstance(watch, str) and watch.lstrip().startswith("{"):  # the API sends the watch as JSON text
        try:
            watch = json.loads(watch)
        except ValueError:
            pass
    if isinstance(watch, dict):
        every = watch.get("every_minutes")
        return f"watch every {every}m" if every else "watch"
    return "watch" if watch else ""


class RunsView(View):
    TOPICS = frozenset({"runs", "schedules", "flows", "conn"})
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("o", "proof", "Open proof"),
        Binding("e", "toggle_schedule", "Enable/disable"),
        Binding("n", "run_now", "Run now"),
        Binding("a", "add_schedule", "Add schedule"),
        Binding("delete", "delete_schedule", "Delete", show=False),
    ]

    def compose(self):
        yield Static("Runs", classes="section-title")
        yield Static(id="runs-note", classes="note")
        yield Table(id="runs-table")
        yield Static(id="sched-title", classes="section-title")
        yield Static(id="sched-note", classes="note")
        yield DataTable(id="sched-table", cursor_type="row")

    @property
    def runs_table(self) -> DataTable:
        return self.query_one("#runs-table", DataTable)

    @property
    def sched_table(self) -> DataTable:
        return self.query_one("#sched-table", DataTable)

    def refresh_view(self, topics: set[str]) -> None:
        state = self.state
        width = self.size.width or self.app.size.width or 80
        self.query_one("#sched-title", Static).update(
            Text.assemble(("Schedules", f"bold {fmt.MUTED}"), (f"  times in {state.tz_name}", fmt.DIM)))
        for key, table_id in (("runs", "#runs-note"), ("schedules", "#sched-note")):
            note = resource_note(state, key)
            if note is None and key == "runs" and not state.runs:
                note = Text("No routine has run yet. Commands an agent runs in its environment are under "
                            "Environments (6).", style=fmt.MUTED)
            w = self.query_one(table_id, Static)
            w.update(note or "")
            w.display = note is not None
        self._fill_runs(state, width)
        self._fill_schedules(state, width)
        self.refresh_bindings()

    def _fill_runs(self, state: DashState, width: int) -> None:
        table = self.runs_table
        keep = _cursor_key(table)
        wide = width >= 110
        table.clear(columns=True)
        widths = {"Started": 11, "Flow": 24 if wide else 22, "Lane": 18, "Status": 18, "Cost": 8}
        used = sum(w + 2 for k, w in widths.items() if wide or k not in ("Lane", "Cost")) + 3
        proof_w = max(12, width - used - 2)
        for name, w in widths.items():
            if name in ("Lane", "Cost") and not wide:
                continue
            if name == "Cost":
                table.add_column("Proof", width=proof_w)
            table.add_column(name, width=w)
        if not wide:
            table.add_column("Proof", width=proof_w)
        for run in state.sorted_runs():
            cells = [Text(fmt.clock(run.started_at, state.tz) or "queued", style=fmt.MUTED),
                     Text(fmt.clip(run.flow or run.id, 24), style=fmt.TEXT)]
            if wide:
                cells.append(Text(run.lane or "", style=fmt.MUTED))
            cells.append(fmt.status_text(run.status))
            cells.append(proof_word(run, proof_w) if run.proof or fmt.norm(run.status) != "failed"
                         else Text(fmt.clip(fmt.one_line(run.error), proof_w), style=fmt.BAD))
            if wide:
                cells.append(Text(fmt.usd(run.cost.usd) if run.cost and run.cost.usd else "", style=fmt.MUTED))
            table.add_row(*cells, key=run.id)
        _restore_cursor(table, keep)

    def _fill_schedules(self, state: DashState, width: int) -> None:
        table = self.sched_table
        keep = _cursor_key(table)
        wide = width >= 110
        table.clear(columns=True)
        table.add_column("Flow", width=24 if wide else 22)
        table.add_column("When", width=18 if wide else 14)
        table.add_column("Next", width=20)
        table.add_column("", width=4)
        if wide:
            table.add_column("Last run", width=16)
        tz = state.tz
        for s in state.sorted_schedules():
            if s.enabled and s.next_fire_at:
                nxt = Text(f"{fmt.clock(s.next_fire_at, tz)} ({fmt.until(s.next_fire_at)})", style=fmt.TEXT)
            else:
                nxt = Text("off" if not s.enabled else "", style=fmt.DIM)
            cells = [Text(fmt.clip(s.flow, 24 if wide else 22), style=fmt.TEXT if s.enabled else fmt.DIM),
                     Text(fmt.clip(schedule_when(s), 18 if wide else 14), style=fmt.MUTED), nxt,
                     Text("on", style=fmt.OK) if s.enabled else Text("off", style=fmt.DIM)]
            if wide:
                last = s.last_run or (state.flows[s.flow].last_run if s.flow in state.flows else None)
                cells.append(fmt.status_text(last.get("status")) if last else Text(""))
            table.add_row(*cells, key=s.id)
        _restore_cursor(table, keep)

    def selected_run(self):
        key = _cursor_key(self.runs_table)
        return self.state.runs.get(key) if key else None

    def selected_schedule(self):
        key = _cursor_key(self.sched_table)
        return self.state.schedules.get(key) if key else None

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        focused = self.app.focused
        if action == "proof":
            run = self.selected_run() if focused is self.runs_table else None
            return bool(run and run.proof and run.proof.url)
        if action in ("toggle_schedule", "run_now", "delete_schedule"):
            return focused is self.sched_table and self.selected_schedule() is not None
        return True

    def on_descendant_focus(self) -> None:
        self.refresh_bindings()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.refresh_bindings()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table is self.runs_table and event.row_key and event.row_key.value:
            self.dash.show_run(event.row_key.value)

    def action_proof(self) -> None:
        run = self.selected_run()
        if run and run.proof and run.proof.url:
            self.dash.show_link(run.proof.url, f"Proof for {run.flow or run.id}")

    def action_toggle_schedule(self) -> None:
        s = self.selected_schedule()
        if s is not None:
            self.dash.set_schedule_enabled(s, not s.enabled)

    def action_run_now(self) -> None:
        s = self.selected_schedule()
        if s is not None:
            self.dash.run_schedule_now(s)

    def action_add_schedule(self) -> None:
        self.dash.add_schedule()

    def action_delete_schedule(self) -> None:
        s = self.selected_schedule()
        if s is not None:
            self.dash.delete_schedule(s)
