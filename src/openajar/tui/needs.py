"""The dashboard's Needs you: approvals, questions, codes and lanes that want a person, grouped,
with the detail of the one selected."""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual.binding import Binding, BindingType
from textual.containers import Horizontal
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option, OptionDoesNotExist

from openajar.tui import fmt
from openajar.tui.models import Approval, Lane, Question
from openajar.tui.state import DashState, NeedItem
from openajar.tui.views import lane_actions
from openajar.tui.widgets import Pane, View, resource_note

GROUP_TITLES = {"control": "Wants control back", "question": "Questions from agents",
                "lane": "Lanes that need you"}


def group_title(group: str, count: int, first: bool = True) -> Text:
    t = Text("" if first else "\n")
    if group.startswith("effect:"):
        effect = group.split(":", 1)[1]
        t.append(f"Approvals: {effect.replace('_', ' ')} ({count})", style=f"bold {fmt.TEXT}")
        hint = fmt.EFFECT_HINT.get(effect)
        if hint:
            t.append(f"  {hint}", style=fmt.MUTED)
    else:
        t.append(f"{GROUP_TITLES.get(group, group)} ({count})", style=f"bold {fmt.TEXT}")
    return t


def need_prompt(item: NeedItem, marked: bool, state: DashState | None = None) -> Text:
    place = state.place if state is not None else (lambda lane: lane or "")
    t = Text()
    if item.kind == "approval":
        t.append("● " if marked else "○ ", style=fmt.ACCENT if marked else fmt.DIM)
        effect = fmt.norm(item.ref.effect)
        t.append(item.title, style=fmt.EFFECT_STYLE.get(effect, fmt.TEXT) if effect in ("pay", "irreversible")
                 else fmt.TEXT)
    else:
        t.append("  ")
        t.append(item.title, style=fmt.TEXT)
    meta = []
    if item.kind == "approval":
        a: Approval = item.ref
        if a.lane:
            meta.append(place(a.lane))
        if a.requested_by and a.requested_by.id:
            meta.append(a.requested_by.id)
        meta.append(fmt.ago(a.created_at, never="new"))
        if a.expires_at:
            meta.append(f"expires {fmt.until(a.expires_at)}")
    elif item.kind == "question":
        q: Question = item.ref
        meta += [x for x in (place(q.lane) if q.lane else None, q.asked_by.id if q.asked_by else None) if x]
        meta.append(fmt.ago(q.created_at, never="new"))
    elif item.kind == "control":
        meta.append(f"asks: {fmt.one_line(item.detail)}")
    elif item.kind == "lane" and item.detail:
        meta.append(fmt.one_line(item.detail))
    if meta:
        t.append("\n    " + fmt.SEP.join(meta), style=fmt.MUTED)
    return t


def need_detail(state: DashState, item: NeedItem | None) -> Text:
    if item is None:
        return Text("Pick an item to see what exactly it will do.", style=fmt.MUTED)
    t = Text()

    def row(label: str, value: str, style: str = fmt.TEXT) -> None:
        t.append(f"{label:<12}", style=fmt.MUTED)
        t.append(value + "\n", style=style)

    if item.kind == "approval":
        a: Approval = item.ref
        effect = fmt.norm(a.effect)
        t.append(a.summary or a.id, style=f"bold {fmt.TEXT}")
        t.append("\n")
        t.append(f"{effect.replace('_', ' ')}", style=fmt.EFFECT_STYLE.get(effect, fmt.WARN))
        if fmt.EFFECT_HINT.get(effect):
            t.append(f"{fmt.SEP}{fmt.EFFECT_HINT[effect]}", style=fmt.MUTED)
        t.append("\n\n")
        row("Lane", state.place(a.lane) if a.lane else "none")
        if a.requested_by:
            who = " ".join(x for x in (a.requested_by.kind, a.requested_by.id) if x)
            if a.requested_by.thread:
                who += f" (thread {a.requested_by.thread})"
            row("Asked by", who)
        row("Asked", fmt.ago(a.created_at, never="unknown"))
        if a.expires_at:
            row("Expires", fmt.until(a.expires_at))
        if a.content_hash:
            row("Content", a.content_hash)
        t.append("The summary is written by the node from what it will actually do. Approving binds to "
                 "this exact content.\n", style=fmt.DIM)
        if a.preview and (a.preview.text or a.preview.image):
            t.append("\nPreview\n", style=f"bold {fmt.MUTED}")
            if a.preview.text:
                # a site's rich-text document (Reddit's {"document": [...]}) reads as its text, not as JSON
                for line in fmt.readable(a.preview.text).splitlines() or [""]:
                    t.append(f"  {line}\n", style=fmt.TEXT)
            if a.preview.image:
                t.append("  Image attached: i saves it to a file.\n", style=fmt.MUTED)
        t.append("\na approve   d deny   space mark   A approve marked", style=fmt.DIM)
    elif item.kind == "question":
        q: Question = item.ref
        t.append(q.question, style=f"bold {fmt.TEXT}")
        t.append("\n\n")
        row("Lane", state.place(q.lane) if q.lane else "none")
        if q.asked_by:
            row("Asked by", " ".join(x for x in (q.asked_by.kind, q.asked_by.id) if x))
        row("Asked", fmt.ago(q.created_at, never="unknown"))
        if q.options:
            row("Options", ", ".join(q.options))
        t.append("\nenter or a to answer", style=fmt.DIM)
    elif item.kind == "control":
        lane: Lane = item.ref
        c = lane.control
        t.append(f"{lane.id}: the agent wants control back", style=f"bold {fmt.TEXT}")
        t.append("\n\n")
        row("Reason", c.takeback_reason or "no reason given")
        if c.takeback_by:
            row("Asked by", c.takeback_by)
        row("Yours since", f" {fmt.ago(c.since, never='unknown')}" + (f" (by {c.by})" if c.by else ""))
        if c.last_human_input:
            row("Last input", fmt.ago(c.last_human_input))
        t.append("\nh hand back   x refuse (you keep control)   v live view", style=fmt.DIM)
    else:
        lane = item.ref
        need = state.lane_need(lane) or ""
        t.append(f"{lane.id}: {need}", style=f"bold {fmt.TEXT}")
        t.append("\n\n")
        row("State", lane.state)
        row("Signal", fmt.SIGNAL_WORD.get(lane.signal, lane.signal))
        if lane.quarantine_reason:
            row("Why", lane.quarantine_reason, fmt.WARN)
        t.append("\nt take control and open the live view   enter lane details", style=fmt.DIM)
    return t


class NeedsView(View):
    TOPICS = frozenset({"approvals", "questions", "workspaces", "conn"})
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("a", "approve", "Approve"),
        Binding("d", "deny", "Deny"),
        Binding("space", "mark", "Mark"),
        Binding("A", "batch('approve')", "Approve marked"),
        Binding("D", "batch('deny')", "Deny marked", show=False),
        Binding("h", "lane('hand_back')", "Hand back"),
        Binding("x", "lane('deny_request')", "Refuse"),
        Binding("t", "lane('take')", "Take control"),
        Binding("v", "lane('view')", "Viewer"),
        Binding("i", "save_image", "Save image", show=False),
    ]

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self.items: dict[str, NeedItem] = {}
        self.marked: set[str] = set()

    def compose(self):
        yield Static(id="needs-note", classes="note")
        with Horizontal(id="needs-body"):
            yield OptionList(id="needs-list")
            with Pane(id="needs-detail-box"):
                yield Static(id="needs-detail")

    @property
    def options(self) -> OptionList:
        return self.query_one("#needs-list", OptionList)

    def current(self) -> NeedItem | None:
        option = self.options.highlighted_option
        return self.items.get(option.id) if option is not None and option.id else None

    def refresh_view(self, topics: set[str]) -> None:
        state = self.state
        items = state.needs_you()
        self.items = {i.key: i for i in items}
        self.marked &= {k for k in self.items if k.startswith("approval:")}
        notes = [n for n in (resource_note(state, "approvals"), resource_note(state, "questions")) if n]
        note = self.query_one("#needs-note", Static)
        if notes:
            note.update(Text("\n").join(notes))
        note.display = bool(notes)
        ol = self.options
        keep = ol.highlighted_option.id if ol.highlighted_option is not None else None
        keep_index = ol.highlighted
        options: list[Option] = []
        groups: dict[str, list[NeedItem]] = {}
        for item in items:
            groups.setdefault(item.group, []).append(item)
        for n, (group, members) in enumerate(groups.items()):
            options.append(Option(group_title(group, len(members), first=n == 0), id=f"hdr:{group}",
                                  disabled=True))
            for item in members:
                options.append(Option(need_prompt(item, item.key in self.marked, self.state), id=item.key))
        if not items:
            options.append(Option(Text("Nothing needs you. Agents are on their own.", style=fmt.OK),
                                  id="hdr:empty", disabled=True))
        ol.set_options(options)
        target = None
        if keep and keep in self.items:
            target = ol.get_option_index(keep)
        elif items:
            index = min(keep_index if keep_index is not None else 0, len(options) - 1)
            enabled = [i for i, o in enumerate(options) if not o.disabled]
            after = [i for i in enabled if i >= index]
            target = after[0] if after else enabled[-1]
        ol.highlighted = target
        self._show_detail()

    def _show_detail(self) -> None:
        self.query_one("#needs-detail", Static).update(need_detail(self.state, self.current()))
        self.refresh_bindings()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self._show_detail()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        item = self.items.get(event.option.id or "")
        if item is None:
            return
        if item.kind == "question":
            self.dash.answer_question(item.ref)
        elif item.lane:
            self.dash.open_lane(item.lane)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        item = self.current()
        kind = item.kind if item else None
        if action == "approve":
            return kind in ("approval", "question")
        if action in ("deny", "mark", "save_image"):
            if action == "save_image":
                return kind == "approval" and bool(item.ref.preview and item.ref.preview.image)
            return kind == "approval"
        if action == "batch":
            return any(i.kind == "approval" for i in self.items.values())
        if action == "lane":
            if kind not in ("control", "lane"):
                return False
            lane = self.state.lane(item.lane or "")
            return parameters[0] in lane_actions(lane)
        return True

    def action_approve(self) -> None:
        item = self.current()
        if item is None:
            return
        if item.kind == "question":
            self.dash.answer_question(item.ref)
        elif item.kind == "approval":
            self.dash.decide([item.ref], "approve")

    def action_deny(self) -> None:
        item = self.current()
        if item is not None and item.kind == "approval":
            self.dash.decide([item.ref], "deny")

    def action_mark(self) -> None:
        item = self.current()
        if item is None or item.kind != "approval":
            return
        self.marked ^= {item.key}
        self.options.replace_option_prompt(item.key, need_prompt(item, item.key in self.marked, self.state))
        # move on, so marking a run of posts is space, space, space
        self.options.action_cursor_down()

    def action_batch(self, decision: str) -> None:
        if self.marked:
            chosen = [self.items[k].ref for k in self.items if k in self.marked]
        else:
            item = self.current()
            if item is None or item.kind != "approval":
                self.app.notify("Mark approvals with space first, or pick one in the group.", markup=False)
                return
            chosen = [i.ref for i in self.items.values() if i.group == item.group]
        self.dash.decide(chosen, decision, batch=True)

    def action_lane(self, action: str) -> None:
        item = self.current()
        if item is not None and item.lane:
            self.dash.lane_action(item.lane, action)

    def action_save_image(self) -> None:
        item = self.current()
        if item is not None and item.kind == "approval":
            self.dash.save_preview_image(item.ref)

    def focus_key(self, key: str) -> None:
        try:
            self.options.highlighted = self.options.get_option_index(key)
        except OptionDoesNotExist:
            return
        self.options.focus()
