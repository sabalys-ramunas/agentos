"""The dashboard's Overview: every workspace and its lanes as a tree, with what needs a look."""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual.binding import Binding, BindingType
from textual.widgets import Static, Tree
from textual.widgets.tree import TreeNode

from agentos.tui import fmt
from agentos.tui.models import Lane
from agentos.tui.state import DashState
from agentos.tui.views import (
    lane_actions,
    lane_label,
    lane_name_width,
    needs_line,
    node_line,
    workspace_label,
)
from agentos.tui.widgets import View, resource_note


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
