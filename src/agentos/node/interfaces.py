"""Contracts between node modules. Changed only by the lead.

Implementations:
  Engine, BrowserSession   -> agentos.node.engine_camoufox   (lanes agent)
  DisplayManager           -> agentos.node.display           (lanes agent)
  LaneManager              -> agentos.node.lanes             (lanes agent)
  Interaction              -> agentos.node.interaction       (interaction agent)
  MCP server, CLI, daemon  -> agentos.mcp, agentos.cli       (mcp-cli agent)

Rules:
- Never import camoufox or playwright at module import time in code that unit tests
  load on a Mac; import inside the function that launches a browser.
- `Page` below is playwright.async_api.Page at runtime; typed loosely here so this
  module imports without Playwright installed.
"""

from __future__ import annotations

import builtins
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from agentos.core.models import (
    ActionKind,
    ActionResult,
    LaneControl,
    LaneInfo,
    LaneSpec,
    NavigateResult,
    ProxyConfig,
    Signal,
    SnapshotResult,
    ViewerInfo,
)

if TYPE_CHECKING:
    from agentos.node.claims import Holder

Page = Any


class BrowserSession(Protocol):
    """One running browser for one lane (one Firefox process)."""

    @property
    def pid(self) -> int | None: ...

    @property
    def alive(self) -> bool:
        """False once the browser process has exited (crash or kill); the manager relaunches."""

    async def page(self) -> Page:
        """The lane's single tab (created if missing)."""

    async def close(self) -> None:
        """Graceful close (flushes cookies), then force, then sweep orphans of this profile.
        Every step runs even when the caller is cancelled part way; the cancellation is
        raised at the end."""

    # Optional, read with getattr by the lane manager (a fake engine may leave them out):
    # `pids` (list[int]), every process of this browser, for per-lane memory limits;
    # `pin_notes` (list of (event kind, detail)), what the launch did to the lane's pin;
    # `guard` (agentos.node.egress.LaneGuard), the lane's egress guard;
    # `read_signed_out(url, text, *, display, timeout_ms, lane_id, sites, strict)`, the page as a
    # signed-out visitor sees it (agentos.node.signed_out).


class Engine(Protocol):
    name: str

    async def launch(
        self,
        spec: LaneSpec,
        *,
        profile_dir: Path,
        fingerprint_path: Path,
        display: str,
        xauthority: str | None = None,
        proxy: ProxyConfig | None,
    ) -> BrowserSession:
        """Start the browser on `display`, with XAUTHORITY set to `xauthority` (the display's cookie
        file). If fingerprint_path does not exist, capture the fingerprint the engine generated and
        pin it there (chmod 600); otherwise launch with the pinned one."""

    async def sweep_orphans(self, root: Path) -> list[int]:
        """Kill browser processes whose profile lives under root (left by a crashed daemon).
        Returns the pids it killed."""

    # Optional: `display_size(fingerprint_path) -> tuple[int, int] | None`, the size in pixels the
    # lane's display is made from the pin (None before the first launch), or else
    # `window_size(fingerprint_path)`, the browser window's size, which the display is then made.


class DisplayManager(Protocol):
    async def acquire(self, lane_id: str, *, size: tuple[int, int] | None = None) -> ViewerInfo:
        """Start Xvfb (size pixels, default 1920x1080) and x11vnc on 127.0.0.1 only, on a free
        slot. No websockify: humans reach x11vnc through agentosd's signed-link relay. A display
        the lane already holds is returned while it runs, and replaced once it died."""

    async def release(self, lane_id: str) -> None: ...

    async def release_all(self) -> None: ...

    async def sweep(self) -> None:
        """Stop stale Xvfb/x11vnc (and old websockify) processes in this manager's own slot range only."""

    def get(self, lane_id: str) -> ViewerInfo | None: ...

    def healthy(self, lane_id: str) -> bool:
        """The lane's display and VNC server still run (optional; assumed True when missing)."""


class LaneManager(Protocol):
    async def start(self) -> None:
        """Load lanes from the store, sweep orphans left by a previous run, start the idle reaper."""

    async def shutdown(self) -> None:
        """Close every warm lane gracefully."""

    async def create(self, spec: LaneSpec) -> LaneInfo: ...

    async def import_profile(self, spec: LaneSpec, profile_src: Path, fingerprint_src: Path | None) -> LaneInfo:
        """Create a lane from an existing browser profile (e.g. the PoC), copying it in."""

    def list(self) -> list[LaneInfo]: ...

    def get(self, lane_id: str) -> LaneInfo:
        """Raises LaneNotFound."""

    async def open(self, lane_id: str, *, principal_kind: str = "agent") -> LaneInfo:
        """Warm the lane (display, viewer, browser, self-check). Enforces max_warm_lanes and
        the memory floor by closing the least recently used idle lane agents hold; raises
        LaneBusy after busy_timeout, ResourcesLow (code resources_low) when memory or disk is
        short, LaneQuarantined for a quarantined lane, and the control errors for a non-human
        principal. A failed self-check does not raise: it returns state=quarantined."""

    async def close(self, lane_id: str, *, principal_kind: str = "agent") -> LaneInfo:
        """Send the lane to cold (waits for a tool call in flight). Passes the control gate."""

    async def delete(self, lane_id: str, *, by: str = "owner") -> Path | None:
        """Remove a cold lane; its directory moves to AGENTOS_HOME/trash/<ws>--<name>--<time>
        (returned). InvalidRequest for a lane that is warm, in use or held by a browser."""

    async def set_proxy(self, lane_id: str, label: str | None, *, confirm: bool = False,
                        by: str = "owner") -> LaneInfo:
        """Move a cold lane to another proxy label (None: no proxy); recorded as a lane event. A
        lane that has launched needs confirm=True (ConfirmRequired otherwise): a new IP to its site."""

    def events(self, lane_id: str | None = None, *, limit: int = 100) -> builtins.list[Any]:
        """Lane events, newest first: held, hold_cleared, quarantined, quarantine_cleared,
        exit_ip_pinned, proxy_changed, deleted (each with .lane_id, .at, .kind, .detail)."""

    def view(self, lane_id: str) -> ViewerInfo | None: ...

    def using(self, lane_id: str, *, principal_kind: str = "agent", holder: Holder | None = None,
              mode: str = "act", purpose: str | None = None) -> AbstractAsyncContextManager[Page]:
        """`async with manager.using(id) as page:` warms the lane if needed, takes a place on it,
        marks it active, and touches last_used on exit. Raises LaneQuarantined for a
        quarantined lane, LaneBusy when no warm slot frees up in time or the lane stays taken
        for queue_timeout. For any non-human principal it raises HumanInControl when control is
        HUMAN and AgentPaused when PAUSED, checked both before acquiring the lane and again
        right before yielding the page. `holder` says who calls (agentos.node.claims.Holder),
        `mode` is "read", "act" or "run" (a flow or connector run, `purpose` naming it)."""

    # ---- control (take over, hand back, stop) ----
    async def take_control(self, lane_id: str, *, by: str) -> LaneInfo:
        """A human takes control. In-flight agent work finishes its current atomic step; every
        later agent call on the lane raises HumanInControl. Fires control listeners."""

    async def hand_back(self, lane_id: str, *, by: str, note: str | None = None) -> LaneInfo:
        """Control returns to agents (also clears a pause and a pending take-back request). `note`
        is what the human tells the agents (the hand-back note carries it)."""

    async def stop_agent(self, lane_id: str, *, by: str) -> LaneInfo:
        """PAUSED: cancel agent work bound to this lane (listeners cancel runs and brain tasks)."""

    async def request_control(self, lane_id: str, *, by: str, reason: str) -> LaneInfo:
        """An agent asks for control back. Notifies the human; granted by hand_back, refused by
        deny_request, or granted automatically after control_idle_seconds without human input."""

    async def deny_request(self, lane_id: str, *, by: str) -> LaneInfo: ...

    def note_human_input(self, lane_id: str) -> None:
        """Called by the viewer relay on every human input event (for idle hand-back)."""

    def viewer_attached(self, lane_id: str) -> None:
        """The viewer relay opened a live view for a human: the lane is not reaped or evicted
        until the matching viewer_detached (AUD-16)."""

    def viewer_detached(self, lane_id: str) -> None: ...

    def view_opened(self, lane_id: str) -> None:
        """The viewer relay opened any live view of the lane (view-only links too): until the
        matching view_closed, the node does nothing of its own that shows on the lane's screen."""

    def view_closed(self, lane_id: str) -> None: ...

    def views(self, lane_id: str) -> tuple[int, int]:
        """(live views open now, live views ever opened) of the lane, view-only links too: the node
        never closes a lane on its own while one is open, or after one opened during its own use."""

    def driven(self, lane_id: str) -> bool:
        """Is an agent driving the lane now (its commit-gate drive is on, control not yet left the agent)."""

    async def wait_for_agent_control(self, lane_id: str, timeout: float) -> LaneInfo:
        """Return when agents may act again (or on timeout, with the current state)."""

    def add_control_listener(self, callback: Any) -> None:
        """callback(lane_id: str, old: LaneControl, new: LaneControl) -> None | Awaitable."""

    def remove_control_listener(self, callback: Any) -> None: ...

    def control(self, lane_id: str) -> LaneControl:
        """The lane's current control record (raises LaneNotFound). Cheap: no store read."""

    # ---- holds, signals, quarantine (AUD-07, AUD-14) ----
    async def hold(self, lane_id: str, *, reason: str, until: datetime | None = None,
                   by: str = "node") -> LaneInfo:
        """Hold agents off the lane: control PAUSED with `reason` (and `hold_until`), persisted,
        announced to control listeners. Cleared by a human's hand_back, or at `until`. Every
        agent, flow and brain stops at the same gate in using() (AgentPaused). A human who
        takes over a held lane and goes idle gets the hold back, not agents."""

    async def report_signal(self, lane_id: str, signal: Signal | str, detail: str | None = None) -> LaneInfo:
        """A tool saw `signal` in the lane: recorded as LaneInfo.signal; a bad one becomes a hold
        through the manager's hold policy (agentos.node.holds.hold_for when installed)."""

    def watch(self, interaction: Any) -> Any:
        """`interaction`, reporting every signal it reads (navigate, act, signal) for the lane
        whose page it ran on. Install once where the runtime is composed."""

    async def quarantine(self, lane_id: str, reason: str, *, by: str = "node") -> LaneInfo:
        """QUARANTINED until a human hands the lane back: agents held off at once, the browser
        closed after the step in flight. Persisted across restarts."""

    async def clear_quarantine(self, lane_id: str, *, by: str) -> LaneInfo:
        """A human clears a quarantine (hand_back also does). For an exit IP change it accepts
        the new exit IP for the proxy label."""

    async def probe_exit_ip(self, lane_id: str) -> str | None:
        """The self-check on demand for a warm lane: its exit IP read in its own browser,
        quarantining on the open-time rules. None when cold, busy, watched in a live view (the
        read opens a window over the screen) or unreadable."""

    # ---- resources ----
    def reserve_memory(self, key: str, mb: int) -> None:
        """Count `mb` against the memory floor until release_memory(key) (brains, AUD-43).
        Raises ResourcesLow when that would take the node below the floor."""

    def release_memory(self, key: str) -> None: ...

    def memory_status(self) -> dict[str, Any]:
        """available_mb, reserved_mb, floor_mb, disk_free_bytes, disk_floor_bytes."""


class Interaction(Protocol):
    async def navigate(self, page: Page, url: str, *, follow_link: bool = False,
                       on_click: Callable[[], None] | None = None) -> NavigateResult:
        """Open `url`. `follow_link` lets the engine click a link to exactly that address;
        `on_click` runs right before such a press, and only then."""

    async def snapshot(self, page: Page) -> SnapshotResult:
        """Accessibility outline with [ref=eN] on actionable nodes. Must not modify the page."""

    async def act(self, page: Page, ref: str, action: ActionKind, text: str | None = None) -> ActionResult:
        """Human-like input only (wheel scroll, pause, random point, real press, typing rhythm).
        Raises RefNotFound if the ref no longer resolves."""

    async def screenshot(self, page: Page) -> bytes: ...

    async def signal(self, page: Page) -> Signal: ...
