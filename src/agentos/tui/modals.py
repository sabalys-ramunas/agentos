"""Dialogs: confirm, link to copy, answer a question, deny with a note, filters, add a schedule,
run details, help."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from rich.text import Text
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

from agentos.tui import fmt
from agentos.tui.models import Question, Run, lenient_dt
from agentos.tui.state import ActivityFilter, DashState


def button(label: str, id: str, variant: str = "default", classes: str = "") -> Button:
    return Button(label, variant=variant, id=id, classes=classes, compact=True)


class Dialog(ModalScreen):
    DEFAULT_CLASSES = "dialog"
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmModal(Dialog):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape,n", "cancel", "Cancel"), Binding("y", "confirm", "Confirm")]

    def __init__(self, title: str, body: str | Text, confirm: str = "Confirm", *, danger: bool = False):
        super().__init__()
        self.title_text = title
        self.body = body
        self.confirm_label = confirm
        self.danger = danger

    def compose(self):
        with Vertical(classes="box"):
            yield Label(Text(self.title_text, style="bold"), classes="dialog-title")
            yield Static(self.body if isinstance(self.body, Text) else Text(self.body), classes="dialog-body")
            with Horizontal(classes="buttons"):
                yield button("Cancel (n)", "cancel")
                yield button(f"{self.confirm_label} (y)", "confirm", "error" if self.danger else "primary")

    def on_mount(self) -> None:
        self.query_one("#confirm", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class LinkModal(Dialog):
    """Shows a URL to copy. `c` copies it through the terminal (OSC 52)."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape,q", "cancel", "Close"), Binding("c", "copy", "Copy")]

    def __init__(self, title: str, url: str, note: str = ""):
        super().__init__()
        self.title_text = title
        self.url = url
        self.note = note

    def compose(self):
        with Vertical(classes="box wide"):
            yield Label(Text(self.title_text, style="bold"), classes="dialog-title")
            yield Static(Text(self.url, style=f"underline {fmt.ACCENT}", overflow="fold"),
                         id="link-url", classes="dialog-body")
            if self.note:
                yield Static(Text(self.note, style=fmt.MUTED), classes="dialog-body")
            with Horizontal(classes="buttons"):
                yield button("Copy (c)", "copy")
                yield button("Close (esc)", "close", "primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "copy":
            self.action_copy()
        else:
            self.dismiss(None)

    def action_copy(self) -> None:
        self.app.copy_to_clipboard(self.url)
        self.app.notify("Copied the link (if your terminal allows clipboard access).", markup=False)


class AnswerModal(Dialog):
    def __init__(self, question: Question):
        super().__init__()
        self.question = question

    def compose(self):
        q = self.question
        with Vertical(classes="box wide"):
            yield Label(Text("Answer the agent", style="bold"), classes="dialog-title")
            yield Static(Text(q.question, style=fmt.TEXT), classes="dialog-body")
            meta = fmt.SEP.join(x for x in (q.lane, q.asked_by.id if q.asked_by else None) if x)
            if meta:
                yield Static(Text(meta, style=fmt.MUTED), classes="dialog-body")
            if q.options:
                with Horizontal(classes="buttons options"):
                    for i, option in enumerate(q.options[:9], 1):
                        yield button(f"{i}. {option}", f"opt-{i}", classes="option")
            yield Input(placeholder="Type an answer and press enter", id="answer", compact=True)
            with Horizontal(classes="buttons"):
                yield button("Cancel (esc)", "cancel")
                yield button("Send", "send", "primary")

    def on_mount(self) -> None:
        self.query_one("#answer", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid.startswith("opt-"):
            self.dismiss(self.question.options[int(bid[4:]) - 1])
        elif bid == "send":
            self._send()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._send()

    def _send(self) -> None:
        text = self.query_one("#answer", Input).value.strip()
        if text:
            self.dismiss(text)
        else:
            self.app.notify("Type an answer first, or pick an option.", severity="warning", markup=False)


class DenyModal(Dialog):
    def __init__(self, summary: str, count: int = 1):
        super().__init__()
        self.summary = summary
        self.count = count

    def compose(self):
        title = "Deny this approval?" if self.count == 1 else f"Deny {self.count} approvals?"
        with Vertical(classes="box wide"):
            yield Label(Text(title, style="bold"), classes="dialog-title")
            yield Static(Text(self.summary, style=fmt.TEXT), classes="dialog-body")
            yield Input(placeholder="Note for the agent (optional)", id="note", compact=True)
            with Horizontal(classes="buttons"):
                yield button("Cancel (esc)", "cancel")
                yield button("Deny", "deny", "error")

    def on_mount(self) -> None:
        self.query_one("#note", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "deny":
            self.dismiss(self.query_one("#note", Input).value.strip())
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())


class FilterModal(Dialog):
    def __init__(self, state: DashState, current: ActivityFilter):
        super().__init__()
        self.state = state
        self.current = current

    def compose(self):
        s = self.state
        workspaces = [(ws.id, ws.id) for ws in s.workspaces]
        lanes = [(lane.id, lane.id) for lane in s.lanes()]
        effects = sorted({fmt.norm(e.effect) for e in s.activity if e.effect} | {"read", "commit"})
        with Vertical(classes="box"):
            yield Label(Text("Filter activity", style="bold"), classes="dialog-title")
            yield Label("Workspace", classes="field")
            yield self._select(workspaces, self.current.workspace, "f-ws")
            yield Label("Lane", classes="field")
            yield self._select(lanes, self.current.lane, "f-lane")
            yield Label("Status", classes="field")
            yield self._select([(x, x) for x in fmt.STATUS_FILTERS], self.current.status, "f-status")
            yield Label("Effect", classes="field")
            yield self._select([(e.replace("_", " "), e) for e in effects], self.current.effect, "f-effect")
            with Horizontal(classes="buttons"):
                yield button("Cancel (esc)", "cancel")
                yield button("Apply", "apply", "primary")

    @staticmethod
    def _select(options: list[tuple[str, str]], value: str | None, sel_id: str) -> Select:
        known = {v for _, v in options}
        return Select(options, value=value if value in known else Select.NULL, id=sel_id, prompt="any",
                      compact=True)

    def _value(self, sel_id: str) -> str | None:
        value = self.query_one(sel_id, Select).value
        return None if value is Select.NULL or value is None else str(value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "apply":
            self.dismiss(None)
            return
        lane = self._value("#f-lane")
        ws = self._value("#f-ws")
        if lane and ws and not lane.startswith(ws + "/"):
            ws = None  # a lane wins over a workspace it is not in
        self.dismiss(ActivityFilter(workspace=ws, lane=lane, status=self._value("#f-status"),
                                    effect=self._value("#f-effect")))


def parse_params(text: str) -> dict:
    """`{"caption": "hi"}` or `caption=hi, image=a.png`. Raises ValueError with a readable message."""
    text = text.strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            value = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"params are not valid JSON: {exc.msg}") from None
        if not isinstance(value, dict):
            raise ValueError("params must be a JSON object")
        return value
    out: dict[str, Any] = {}
    for part in text.split(","):
        if not part.strip():
            continue
        key, sep, value = part.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"expected key=value, got {part.strip()!r}")
        out[key.strip()] = value.strip()
    return out


def check_cron(cron: str) -> str | None:
    fields = cron.split()
    if len(fields) != 5:
        return "cron needs 5 fields: minute hour day month weekday, for example 0 9 * * 1-5"
    allowed = set("0123456789*/,-")
    for f in fields:
        if not set(f) <= allowed and not f.isalpha():
            return f"cron field {f!r} has characters cron does not use"
    return None


class AddScheduleModal(Dialog):
    def __init__(self, state: DashState, submit: Callable[[str, str, dict], Awaitable[str | None]]):
        super().__init__()
        self.state = state
        self.submit = submit

    def compose(self):
        flows = [(f.id, f.id) for f in self.state.flows.values()]
        with Vertical(classes="box wide"):
            yield Label(Text("Add a schedule", style="bold"), classes="dialog-title")
            yield Label("Flow", classes="field")
            if flows:
                yield Select(flows, id="s-flow", prompt="pick a flow", compact=True)
            else:
                yield Input(placeholder="flow id, for example biz-a.instagram.post", id="s-flow-text", compact=True)
            yield Label(f"Cron (in {self.state.tz_name})", classes="field")
            yield Input(placeholder="0 9 * * 1-5", id="s-cron", compact=True)
            yield Label("Params", classes="field")
            yield Input(placeholder='caption=Autumn menu, image=menu.jpg   or   {"caption": "..."}',
                        id="s-params", compact=True)
            yield Static(id="s-hint", classes="dialog-body hint")
            yield Static(id="s-error", classes="dialog-body error")
            with Horizontal(classes="buttons"):
                yield button("Cancel (esc)", "cancel")
                yield button("Add", "add", "primary")

    def _flow(self) -> str:
        if self.query("#s-flow"):
            value = self.query_one("#s-flow", Select).value
            return "" if value is Select.NULL or value is None else str(value)
        return self.query_one("#s-flow-text", Input).value.strip()

    def on_select_changed(self, event: Select.Changed) -> None:
        flow = self.state.flows.get(self._flow())
        hint = self.query_one("#s-hint", Static)
        if flow and flow.params:
            hint.update(Text("Params: " + ", ".join(flow.params), style=fmt.MUTED))
        else:
            hint.update(Text("This flow takes no params." if flow else "", style=fmt.MUTED))

    def error(self, message: str) -> None:
        self.query_one("#s-error", Static).update(Text(message, style=fmt.BAD))

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "add":
            self.dismiss(None)
            return
        await self._try_add()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        await self._try_add()

    async def _try_add(self) -> None:
        flow = self._flow()
        cron = self.query_one("#s-cron", Input).value.strip()
        if not flow:
            self.error("Pick a flow.")
            return
        if problem := check_cron(cron):
            self.error(problem.capitalize() + ".")
            return
        try:
            params = parse_params(self.query_one("#s-params", Input).value)
        except ValueError as exc:
            self.error(str(exc).capitalize() + ".")
            return
        problem = await self.submit(flow, cron, params)
        if problem:
            self.error(problem)
        else:
            self.dismiss({"flow": flow, "cron": cron, "params": params})


class RunModal(Dialog):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape,q", "cancel", "Close"), Binding("o", "proof", "Open proof")]

    def __init__(self, state: DashState, run: Run, show_link: Callable[[str, str], None]):
        super().__init__()
        self.state = state
        self.run = run
        self.show_link = show_link

    def compose(self):
        with Vertical(classes="box wide tall"):
            yield Label(Text(f"Run {self.run.id}", style="bold"), classes="dialog-title")
            with VerticalScroll(classes="dialog-scroll"):
                yield Static(self.render_run(), id="run-body")
            with Horizontal(classes="buttons"):
                if self.run.proof and self.run.proof.url:
                    yield button("Open proof (o)", "proof")
                yield button("Close (esc)", "close", "primary")

    def render_run(self) -> Text:
        r, tz = self.run, self.state.tz
        t = Text()

        def row(label: str, value: str | Text) -> None:
            t.append(f"{label:<10}", style=fmt.MUTED)
            t.append_text(value if isinstance(value, Text) else Text(value, style=fmt.TEXT))
            t.append("\n")

        row("Flow", r.flow or "")
        row("Lane", r.lane or "")
        row("Status", fmt.status_text(r.status))
        if r.trigger:
            row("Trigger", r.trigger)
        row("Started", fmt.clock(r.started_at, tz, seconds=True) or "not yet")
        if r.finished_at:
            row("Took", fmt.duration(r.started_at, r.finished_at))
        if r.cost and (r.cost.usd or r.cost.tokens):
            row("Cost", " ".join(x for x in (fmt.usd(r.cost.usd), fmt.tokens(r.cost.tokens)) if x))
        if r.proof and (r.proof.url or r.proof.id):
            row("Proof", Text(r.proof.url or r.proof.id or "", style=fmt.ACCENT))
        elif fmt.norm(r.status) == "succeeded_unverified":
            row("Proof", Text("none yet: the readback did not find the result", style=fmt.WARN))
        if r.error:
            row("Problem", Text(r.error, style=fmt.BAD))
        if r.steps:
            t.append("\nSteps\n", style=f"bold {fmt.MUTED}")
            for step in r.steps:
                at = fmt.clock(lenient_dt(step.get("at")), tz, seconds=True)
                t.append(f"  {at:<9}", style=fmt.DIM)
                t.append_text(fmt.status_text(step.get("status"), step.get("effect")))
                t.append(f"  {step.get('tool') or ''}  ", style=fmt.MUTED)
                t.append(fmt.one_line(str(step.get("summary") or "")) + "\n", style=fmt.TEXT)
        return t

    def update_run(self, run: Run) -> None:
        self.run = run
        self.query_one("#run-body", Static).update(self.render_run())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "proof":
            self.action_proof()
        else:
            self.dismiss(None)

    def action_proof(self) -> None:
        if self.run.proof and self.run.proof.url:
            self.show_link(self.run.proof.url, f"Proof for {self.run.flow or self.run.id}")




HELP = [
    ("Everywhere", [
        ("1 to 5", "Overview, Needs you, Activity, Runs, Brains"),
        ("/  or  ctrl+k", "search lanes, workspaces, approvals, runs"),
        ("r", "refresh now (and retry when the node is unreachable)"),
        ("?", "this help"), ("q", "quit"), ("esc", "back"),
    ]),
    ("Lanes (Overview and lane details)", [
        ("enter", "lane details, or expand a workspace"), ("space", "expand or collapse a workspace"),
        ("e / E", "expand all / collapse all"),
        ("o / c", "open (warm) / close the lane"),
        ("t", "take control (agents wait) and open the live view"),
        ("h", "hand back to agents"), ("s", "stop the agent (asks first)"),
        ("x", "refuse an agent's request for control back"),
        ("v", "live view in your browser, or a link to copy"), ("p", "screenshot to a file"),
    ]),
    ("Needs you", [
        ("a", "approve (pay and irreversible ask first)"), ("d", "deny, with an optional note"),
        ("space", "mark"), ("A / D", "approve / deny marked (or the whole group)"),
        ("enter", "answer a question, or open the lane"), ("i", "save the preview image"),
    ]),
    ("Activity", [("f", "filter"), ("c", "clear the filter"), ("F", "follow new events on or off"),
                  ("enter", "run or lane behind the event")]),
    ("Runs and schedules", [("enter", "run details"), ("o", "open the proof link"),
                            ("tab", "switch between runs and schedules"),
                            ("e", "enable or disable"), ("n", "run now"), ("a", "add a schedule"),
                            ("delete", "remove a schedule")]),
]


class HelpModal(Dialog):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape,q,question_mark", "cancel", "Close")]

    def __init__(self, connection_line: str, live: str):
        super().__init__()
        self.connection_line = connection_line
        self.live = live

    def compose(self):
        t = Text()
        for section, keys in HELP:
            t.append(f"{section}\n", style=f"bold {fmt.TEXT}")
            for key, what in keys:
                t.append(f"  {key:<15}", style=fmt.ACCENT)
                t.append(f"{what}\n", style=fmt.TEXT)
            t.append("\n")
        t.append("Connection\n", style=f"bold {fmt.TEXT}")
        t.append(f"  {self.connection_line}\n", style=fmt.MUTED)
        t.append(f"  live updates: {self.live}\n", style=fmt.MUTED)
        with Vertical(classes="box wide tall"):
            yield Label(Text("agentos dash", style="bold"), classes="dialog-title")
            with VerticalScroll(classes="dialog-scroll"):
                yield Static(t)
            with Horizontal(classes="buttons"):
                yield button("Close (esc)", "close", "primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)
