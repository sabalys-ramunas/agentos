"""Small shared widgets: the top bar, the degraded-mode banner, and the View base class."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from rich.cells import cell_len
from rich.text import Text
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static

from openajar.tui import fmt
from openajar.tui.state import DashState

if TYPE_CHECKING:
    from openajar.tui.app import DashApp


RESOURCE_WORDS = {
    "workspaces": "Workspaces and lanes", "nodes": "Nodes", "approvals": "Approvals",
    "questions": "Questions", "activity": "Activity", "runs": "Runs", "schedules": "Schedules",
    "flows": "Flows", "brains": "Brains", "tasks": "Brain tasks", "status": "Status",
    "environments": "Environments",
}


def resource_note(state: DashState, name: str) -> Text | None:
    """What to show instead of a section that did not load. None when it is fine."""
    res = state.res[name]
    what = RESOURCE_WORDS.get(name, name)
    if res.status == "ok":
        return None
    if res.status == "loading":
        if state.conn.online is False:
            return Text(f"{what}: waiting for the node to answer.", style=fmt.MUTED)
        return Text(f"Loading {what.lower()}…", style=fmt.MUTED)
    if res.status == "unavailable":
        out = Text(f"{what}: not configured on this node.", style=fmt.MUTED)
        if res.message and "not configured" not in res.message and "not available" not in res.message:
            out.append(f" ({res.message})", style=fmt.DIM)
        return out
    if res.status == "denied":
        return Text(f"{what}: this token may not see them. ({res.message})", style=fmt.WARN)
    return Text(f"{what} could not be loaded: {res.message}", style=fmt.WARN)


class Pane(VerticalScroll, can_focus=False):
    """A scrolling detail area that never takes focus. A focusable one in a hidden tab would
    pull the tab back into view when focus is reset."""


class View(Vertical):
    """A section that redraws from the DashState when one of its topics changes."""

    TOPICS: frozenset[str] = frozenset()

    @property
    def dash(self) -> DashApp:
        return self.app  # type: ignore[return-value]

    @property
    def state(self) -> DashState:
        return self.dash.state

    def wants(self, topics: set[str]) -> bool:
        return not self.TOPICS or bool(self.TOPICS & topics)

    def refresh_view(self, topics: set[str]) -> None:  # pragma: no cover - overridden
        pass

    def on_mount(self) -> None:
        self.refresh_view(set(self.TOPICS) | {"all"})


class TopBar(Static):
    """openajar, node and connection on the left; Needs you, brain, cost and nodes on the right."""

    DEFAULT_CLASSES = "topbar"

    def on_mount(self) -> None:
        self.redraw()

    def on_resize(self) -> None:
        self.redraw()

    def redraw(self) -> None:
        app: DashApp = self.app  # type: ignore[assignment]
        state = app.state
        width = self.size.width or app.size.width or 80
        left = Text()
        left.append("openajar", style=f"bold {fmt.ACCENT}")
        node = state.status.node if state.status and state.status.node else None
        if node:
            left.append(f"  {node}", style=fmt.TEXT)
        left.append("  ")
        left.append_text(conn_chip(state))

        parts: list[Text] = []
        n = state.needs_you_count()
        needs = Text()
        if n:
            needs.append(f" Needs you {n} ", style=f"bold #15171c on {fmt.WARN}")
        else:
            needs.append("Needs you 0", style=fmt.MUTED)
        parts.append(needs)
        if state.status and state.status.brain and state.status.brain.name:
            b = state.status.brain
            word, color = fmt.BRAIN_STATE.get(fmt.norm(b.state), (b.state or "unknown", fmt.MUTED))
            chip = Text("Brain ", style=fmt.MUTED)
            chip.append(b.name, style=fmt.TEXT)
            chip.append(f" {word}", style=color)
            if b.offline_ready:
                chip.append(" (offline-ready)", style=fmt.DIM)
            parts.append(chip)
        usd, tok = state.cost_today()
        if usd is not None or tok:
            cost = Text(fmt.usd(usd) if usd is not None else fmt.tokens(tok), style=fmt.TEXT)
            cost.append(" today", style=fmt.MUTED)
            parts.append(cost)
        nodes = state.nodes or []
        if nodes:
            down = sum(1 for x in nodes if x.state == "down")
            chip = Text(fmt.plural(len(nodes), "node"), style=fmt.MUTED)
            if down:
                chip.append(f", {down} down", style=fmt.BAD)
            parts.append(chip)

        sep = Text(fmt.SEP, style=fmt.DIM)
        # drop the least important parts until it fits; the brain chip loses its suffix first
        while True:
            right = Text()
            for i, part in enumerate(parts):
                if i:
                    right.append_text(sep)
                right.append_text(part)
            if cell_len(left.plain) + cell_len(right.plain) + 2 <= width or len(parts) <= 1:
                break
            if len(parts) >= 2 and "(offline-ready)" in parts[1].plain:
                parts[1] = parts[1][: len(parts[1].plain) - len(" (offline-ready)")]
                continue
            parts.pop()
        gap = max(1, width - cell_len(left.plain) - cell_len(right.plain) - 1)
        self.update(Text.assemble(" ", left, " " * gap, right))


def conn_chip(state: DashState) -> Text:
    conn = state.conn
    if conn.auth_failed:
        return Text("● token refused", style=fmt.BAD)
    if conn.online is False:
        return Text("● offline", style=fmt.BAD)
    if conn.online is None:
        return Text("○ connecting", style=fmt.MUTED)
    if conn.live == "live":
        return Text("● live", style=fmt.OK)
    if conn.live == "polling":
        return Text("○ polling", style=fmt.MUTED)
    return Text("● reconnecting", style=fmt.WARN)


class Banner(Static):
    """Shown only when something is wrong with the connection itself."""

    DEFAULT_CLASSES = "banner"

    def on_mount(self) -> None:
        self.set_interval(1.0, self.redraw)
        self.redraw()

    def redraw(self) -> None:
        app: DashApp = self.app  # type: ignore[assignment]
        state = app.state
        conn = state.conn
        url = app.connection.url
        if conn.auth_failed:
            self.set_class(True, "show", "bad")
            self.update(Text(
                f"The node at {url} refused the token ({conn.reason or '401'}). "
                "Check OPENAJAR_TOKEN or --token, or run `openajar dash` on the node itself.",
                style=fmt.BAD))
            return
        if conn.online is not False:
            self.set_class(False, "show", "bad")
            self.update("")
            return
        self.set_class(True, "show")
        self.set_class(False, "bad")
        msg = Text(f"Node unreachable at {url}: {conn.reason or 'no answer'}. ", style=fmt.WARN)
        if conn.last_ok:
            msg.append(f"Showing what it said at {fmt.clock(conn.last_ok, state.tz, seconds=True)}. ",
                       style=fmt.TEXT)
        else:
            msg.append("Nothing loaded yet. ", style=fmt.TEXT)
        if conn.retry_at:
            secs = max(0, int((conn.retry_at - fmt.now_utc()).total_seconds()))
            msg.append(f"Retrying in {secs}s, r retries now.", style=fmt.MUTED)
        else:
            msg.append("Retrying, r retries now.", style=fmt.MUTED)
        if not app.connection.tunnel_hint and ("127.0.0.1" in url or "localhost" in url):
            # on the Mac the node is usually behind `ssh -L`; on the node itself the hint is moot
            port = httpx.URL(url).port or 7400
            msg.append(f"\nIs the tunnel up? ssh -N -L {port}:localhost:{port} <your node>", style=fmt.MUTED)
        self.update(msg)
