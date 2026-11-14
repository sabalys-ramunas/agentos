"""The five tabs: Overview, Needs you, Activity, Runs and schedules, Brains."""

from __future__ import annotations

import contextlib
import json
from typing import Any, ClassVar

from rich.cells import cell_len
from rich.text import Text
from textual.binding import Binding, BindingType
from textual.containers import Horizontal
from textual.widgets import DataTable, OptionList, Static, Tree
from textual.widgets.data_table import CellDoesNotExist, RowDoesNotExist
from textual.widgets.option_list import Option, OptionDoesNotExist
from textual.widgets.tree import TreeNode

from agentos.tui import fmt
from agentos.tui.models import Approval, Lane, Question, TraceEvent, Workspace
from agentos.tui.state import ActivityFilter, DashState, NeedItem
from agentos.tui.widgets import Pane, View, resource_note

LANE_ACTIONS = ("open", "close", "take", "hand_back", "stop_agent", "deny_request", "view", "screenshot")


class Table(DataTable):
    """A row-cursor table that says what enter does."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("enter", "select_cursor", "Details")]

    def __init__(self, **kwargs: Any):
        kwargs.setdefault("cursor_type", "row")
        super().__init__(**kwargs)


# ---------------------------------------------------------------- lane helpers
def lane_actions(lane: Lane | None) -> set[str]:
    """The lane actions that make sense right now, so the footer only offers those."""
    if lane is None:
        return set()
    out: set[str] = set()
    if lane.state in ("cold", "quarantined"):
        out.add("open")
    if lane.state in ("warm", "active", "warming", "quarantined"):
        out.add("close")
    control = lane.control
    if control.state != "human":
        out.add("take")
    if control.state in ("human", "paused"):
        out.add("hand_back")
    if control.state != "paused":
        out.add("stop_agent")
    if control.takeback_requested:
        out.add("deny_request")
    if lane.state in ("warm", "active"):
        out |= {"view", "screenshot"}
    return out


def control_info(lane: Lane) -> tuple[str, str]:
    c = lane.control
    if c.takeback_requested:
        return "wants control back", fmt.WARN
    if c.state == "human":
        return (f"human ({c.by})" if c.by else "human"), fmt.HUMAN
    if c.state == "paused":
        return "paused", fmt.WARN
    return "agent", fmt.MUTED


def signal_info(state: DashState, lane: Lane) -> tuple[str, str]:
    node = state.node(lane.node)
    if node is not None and node.state == "down":
        return f"{node.id} down", fmt.WARN
    if lane.kind != "browser" and lane.signal == "unknown":
        return "", fmt.DIM
    word = fmt.SIGNAL_WORD.get(lane.signal, lane.signal.replace("_", " "))
    if lane.logged_in_hint == "needs_login" and lane.signal == "ok":
        return "needs login", fmt.WARN
    return word, fmt.SIGNAL_STYLE.get(lane.signal, fmt.MUTED)


def lane_label(state: DashState, lane: Lane, width: int, name_w: int) -> Text:
    """One lane row: icon, name, state, signal, control, last used (plus proxy and node when wide)."""
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append(fmt.KIND_ICON.get(lane.kind, "·") + " ", style=fmt.MUTED)
    t.append(fmt.fit(lane.name, name_w) + " ", style=fmt.TEXT)
    t.append(fmt.fit(lane.state, 11) + " ", style=fmt.LANE_STATE_STYLE.get(lane.state, fmt.MUTED))
    word, color = signal_info(state, lane)
    t.append(fmt.fit(word, 12) + " ", style=color)
    word, color = control_info(lane)
    t.append(fmt.fit(word, 19) + " ", style=color)
    t.append(fmt.fit(fmt.ago(lane.last_used), 9), style=fmt.MUTED)
    rest = width - cell_len(t.plain)
    if rest >= 20:
        proxy = lane.proxy.label if lane.proxy and lane.proxy.label else ""
        t.append(" " + fmt.fit(proxy, 8), style=fmt.DIM)
        t.append(" " + fmt.fit(lane.node or "", 8), style=fmt.DIM)
    return t


def lane_name_width(state: DashState, width: int) -> int:
    longest = max((cell_len(lane.name) for lane in state.lanes()), default=8)
    return max(8, min(longest, 18, width - 58))


def workspace_label(state: DashState, ws: Workspace, width: int) -> Text:
    health = state.workspace_health(ws)
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append("● ", style=ws.color or fmt.MUTED)
    t.append(ws.id, style=f"bold {fmt.TEXT}")
    if ws.name and _squash(ws.name) != _squash(ws.id):
        t.append(f" {ws.name}", style=fmt.MUTED)
    t.append("  ")
    line = state.workspace_line(ws)
    room = width - cell_len(t.plain) - len(health) - 2
    t.append(fmt.clip(line, max(8, room)), style=fmt.TEXT if health == "ok" else fmt.MUTED
             if health == "idle" else fmt.TEXT)
    gap = max(1, width - cell_len(t.plain) - len(health))
    t.append(" " * gap)
    t.append(health, style=fmt.HEALTH_STYLE.get(health, fmt.MUTED))
    return t


def _squash(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def node_line(state: DashState) -> Text:
    out = Text()
    for i, node in enumerate(state.nodes):
        if i:
            out.append("\n")
        word, color = fmt.NODE_STATE.get(node.state, (node.state, fmt.MUTED))
        out.append("● ", style=color)
        out.append(node.id, style=f"bold {fmt.TEXT}")
        out.append(f" {word}", style=color)
        if node.state == "down":
            out.append(f", last seen {fmt.ago(node.last_seen)}", style=fmt.MUTED)
            continue
        bits = []
        if node.cpu is not None:
            bits.append(f"CPU {node.cpu:.0f}%")
        if node.mem and node.mem.total_mb:
            bits.append(f"mem {fmt.gb(node.mem.used_mb)} of {fmt.gb(node.mem.total_mb)} GB")
        if node.warm_lanes is not None and node.max_warm_lanes:
            bits.append(f"warm {node.warm_lanes} of {node.max_warm_lanes}")
        if bits:
            out.append(", " + ", ".join(bits), style=fmt.MUTED)
        if node.degraded:
            out.append(" (degraded)", style=fmt.WARN)
    return out


def needs_line(state: DashState, width: int = 200) -> Text:
    n = state.needs_you_count()
    if not n:
        return Text("✓ Nothing needs you right now.", style=fmt.OK)
    t = Text()
    badge = f" Needs you {n} "
    t.append(badge, style=f"bold #15171c on {fmt.WARN}")
    tail = "  2 to review"
    room = width - len(badge) - len(tail) - 4
    t.append("  " + fmt.clip(state.needs_you_summary(), max(10, room)), style=fmt.TEXT)
    t.append(tail, style=fmt.DIM)
    return t


# ---------------------------------------------------------------- Overview
class LaneTree(Tree[tuple[str, str]]):
    """Workspaces collapsed to one health line each.

    A workspace that needs attention opens on its own and shows only the lanes that
    need a look, with the rest folded into one "N more lanes, all ok" row. Opening a
    workspace by hand (space, a click, or that row) shows every lane."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("enter", "select_cursor", "Details"),
                Binding("space", "toggle_node", "Expand"),
                Binding("e", "expand_all", "Expand all", show=False),
                Binding("E", "collapse_all", "Collapse all", show=False)]

    def __init__(self, **kwargs: Any):
        super().__init__("workspaces", **kwargs)
        self.show_root = False
        self.guide_depth = 3
        self.auto_expand = False
        self.overrides: dict[str, bool] = {}   # the user's own expand or collapse
        self.full: set[str] = set()            # workspaces showing every lane
        self._structure: list = []
        self._by_key: dict[tuple[str, str], TreeNode] = {}
        self._width = 0

    @property
    def state(self) -> DashState:
        return self.app.state  # type: ignore[attr-defined]

    def label_width(self) -> int:
        width = self.size.width or self.app.size.width or 80
        return max(30, width - 3)

    def _layout(self) -> list[tuple[str, str, tuple[str, ...], int]]:
        """(workspace, mode, visible lane ids, folded count) for every workspace."""
        state = self.state
        out = []
        for ws in state.workspaces:
            expanded = self.overrides.get(ws.id, state.should_expand(ws))
            if not expanded:
                out.append((ws.id, "collapsed", tuple(lane.id for lane in ws.lanes), 0))
            elif ws.id in self.full or self.overrides.get(ws.id) is True:
                out.append((ws.id, "full", tuple(lane.id for lane in ws.lanes), 0))
            else:
                shown = tuple(lane.id for lane in ws.lanes if needs_a_look(state, lane))
                out.append((ws.id, "focus", shown, len(ws.lanes) - len(shown)))
        return out

    def sync(self) -> None:
        layout = self._layout()
        if layout != self._structure:
            self._rebuild(layout)
        else:
            self._relabel()

    def _rebuild(self, layout, keep: tuple[str, str] | None = None) -> None:
        state = self.state
        if keep is None and self.cursor_node is not None:
            keep = self.cursor_node.data
        self.clear()
        self._by_key = {}
        width = self.label_width()
        name_w = lane_name_width(state, width - self.guide_depth)
        for ws_id, mode, lane_ids, folded in layout:
            ws = state.workspace(ws_id)
            if ws is None:
                continue
            node = self.root.add(workspace_label(state, ws, width - 2), data=("ws", ws_id),
                                 expand=mode != "collapsed")
            self._by_key[("ws", ws_id)] = node
            for lane_id in lane_ids:
                lane = state.lane(lane_id)
                if lane is None:
                    continue
                leaf = node.add_leaf(lane_label(state, lane, width - self.guide_depth, name_w),
                                     data=("lane", lane_id))
                self._by_key[("lane", lane_id)] = leaf
            if folded:
                more = node.add_leaf(more_label(folded), data=("more", ws_id))
                self._by_key[("more", ws_id)] = more
        self._structure = layout
        self._width = width
        target = self._by_key.get(keep) if keep else None
        if target is None and keep and keep[0] in ("more", "lane"):
            target = self._by_key.get(("ws", keep[1].split("/", 1)[0]))
        if target is not None:
            self.call_after_refresh(self._restore_cursor, target)
        elif self.root.children:
            self.call_after_refresh(self._restore_cursor, self.root.children[0])

    def _restore_cursor(self, node: TreeNode) -> None:
        if node.parent is not None and node.parent.data is not None and not node.parent.is_expanded:
            node = node.parent
        self.move_cursor(node)

    def _relabel(self) -> None:
        state = self.state
        width = self.label_width()
        name_w = lane_name_width(state, width - self.guide_depth)
        for ws in state.workspaces:
            node = self._by_key.get(("ws", ws.id))
            if node is not None:
                node.set_label(workspace_label(state, ws, width - 2))
            for lane in ws.lanes:
                leaf = self._by_key.get(("lane", lane.id))
                if leaf is not None:
                    leaf.set_label(lane_label(state, lane, width - self.guide_depth, name_w))
        self._width = width

    def on_resize(self) -> None:
        if self._structure and self.label_width() != self._width:
            self._relabel()

    def _toggle_node(self, node: TreeNode) -> None:  # a click on the arrow, or space
        if node.data and node.data[0] == "ws":
            self.toggle_workspace(node.data[1])
        elif node.data and node.data[0] == "more":
            self.show_all(node.data[1])
        else:
            super()._toggle_node(node)

    def toggle_workspace(self, ws_id: str) -> None:
        node = self._by_key.get(("ws", ws_id))
        if node is None:
            return
        opening = not node.is_expanded
        self.overrides[ws_id] = opening
        if opening:
            self.full.add(ws_id)
        else:
            self.full.discard(ws_id)
        self._rebuild(self._layout(), keep=("ws", ws_id))

    def show_all(self, ws_id: str) -> None:
        self.overrides[ws_id] = True
        self.full.add(ws_id)
        self._rebuild(self._layout(), keep=("ws", ws_id))

    def focus_key(self, key: tuple[str, str]) -> None:
        ws_id = key[1].split("/", 1)[0]
        ws_node = self._by_key.get(("ws", ws_id))
        hidden = key not in self._by_key or (ws_node is not None and not ws_node.is_expanded)
        if key[0] == "lane" and hidden:
            self.overrides[ws_id] = True
            self.full.add(ws_id)
            self._rebuild(self._layout(), keep=key)
            return
        node = self._by_key.get(key)
        if node is not None:
            self.call_after_refresh(self.move_cursor, node)

    def action_expand_all(self) -> None:
        for ws in self.state.workspaces:
            self.overrides[ws.id] = True
            self.full.add(ws.id)
        self._rebuild(self._layout())

    def action_collapse_all(self) -> None:
        for ws in self.state.workspaces:
            self.overrides[ws.id] = False
        self.full.clear()
        self._rebuild(self._layout())

    def highlighted_lane(self) -> str | None:
        node = self.cursor_node
        if node is not None and node.data and node.data[0] == "lane":
            return node.data[1]
        return None


def needs_a_look(state: DashState, lane: Lane) -> bool:
    return bool(state.lane_issues(lane)) or lane.control.state != "agent"


def more_label(count: int) -> Text:
    return Text(f"+ {fmt.plural(count, 'more lane')}, all ok", style=fmt.DIM)


class OverviewView(View):
    TOPICS = frozenset({"workspaces", "nodes", "approvals", "questions", "status", "schedules", "flows",
                        "conn"})
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

    def compose(self):
        yield Static(id="ov-needs", classes="needs-line")
        yield Static("Nodes", classes="section-title")
        yield Static(id="ov-nodes", classes="body")
        yield Static("Workspaces", classes="section-title")
        yield Static(id="ov-ws-note", classes="note")
        yield LaneTree(id="lane-tree")

    @property
    def tree(self) -> LaneTree:
        return self.query_one(LaneTree)

    def refresh_view(self, topics: set[str]) -> None:
        state = self.state
        width = self.size.width or self.app.size.width or 80
        self.query_one("#ov-needs", Static).update(needs_line(state, width))
        nodes_note = resource_note(state, "nodes")
        self.query_one("#ov-nodes", Static).update(nodes_note or node_line(state) or
                                                   Text("No nodes reported.", style=fmt.MUTED))
        note = resource_note(state, "workspaces")
        note_w = self.query_one("#ov-ws-note", Static)
        note_w.update(note or "")
        note_w.display = note is not None and not state.workspaces
        self.tree.display = bool(state.workspaces) or note is None
        self.tree.sync()
        self.refresh_bindings()

    def selected_lane(self) -> Lane | None:
        lane_id = self.tree.highlighted_lane()
        return self.state.lane(lane_id) if lane_id else None

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "lane":
            return parameters[0] in lane_actions(self.selected_lane())
        return True

    def action_lane(self, action: str) -> None:
        lane = self.selected_lane()
        if lane is not None:
            self.dash.lane_action(lane.id, action)

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        self.refresh_bindings()

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        data = event.node.data
        if not data:
            return
        if data[0] == "ws":
            self.tree.toggle_workspace(data[1])
        elif data[0] == "more":
            self.tree.show_all(data[1])
        else:
            self.dash.open_lane(data[1])


# ---------------------------------------------------------------- Needs you
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


def need_prompt(item: NeedItem, marked: bool) -> Text:
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
            meta.append(a.lane)
        if a.requested_by and a.requested_by.id:
            meta.append(a.requested_by.id)
        meta.append(fmt.ago(a.created_at, never="new"))
        if a.expires_at:
            meta.append(f"expires {fmt.until(a.expires_at)}")
    elif item.kind == "question":
        q: Question = item.ref
        meta += [x for x in (q.lane, q.asked_by.id if q.asked_by else None) if x]
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
        row("Lane", a.lane or "none")
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
                for line in a.preview.text.splitlines() or [""]:
                    t.append(f"  {line}\n", style=fmt.TEXT)
            if a.preview.image:
                t.append("  Image attached: i saves it to a file.\n", style=fmt.MUTED)
        t.append("\na approve   d deny   space mark   A approve marked", style=fmt.DIM)
    elif item.kind == "question":
        q: Question = item.ref
        t.append(q.question, style=f"bold {fmt.TEXT}")
        t.append("\n\n")
        row("Lane", q.lane or "none")
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
                options.append(Option(need_prompt(item, item.key in self.marked), id=item.key))
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
        self.options.replace_option_prompt(item.key, need_prompt(item, item.key in self.marked))
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


# ---------------------------------------------------------------- Activity
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


# ---------------------------------------------------------------- Runs and schedules
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


# ---------------------------------------------------------------- Brains
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


# ---------------------------------------------------------------- table helpers
def _cursor_key(table: DataTable) -> str | None:
    if not table.row_count:
        return None
    try:
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
    except CellDoesNotExist:
        return None
    return row_key.value if row_key is not None else None


def _restore_cursor(table: DataTable, key: str | None) -> None:
    if key is None or not table.row_count:
        return
    with contextlib.suppress(RowDoesNotExist):
        table.move_cursor(row=table.get_row_index(key), animate=False)
