"""What the five tabs share: lane and workspace labels, the node and Needs you lines, a table that
keeps its cursor. The tabs are overview.py, needs.py, activity.py, runs.py and brains.py."""

from __future__ import annotations

import contextlib
from typing import Any, ClassVar

from rich.cells import cell_len
from rich.text import Text
from textual.binding import Binding, BindingType
from textual.widgets import DataTable
from textual.widgets.data_table import CellDoesNotExist, RowDoesNotExist

from agentos.tui import fmt
from agentos.tui.models import Lane, Workspace
from agentos.tui.state import DashState

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


# ---------------------------------------------------------------- Needs you


# ---------------------------------------------------------------- Activity


# ---------------------------------------------------------------- Runs and schedules


# ---------------------------------------------------------------- Brains


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
