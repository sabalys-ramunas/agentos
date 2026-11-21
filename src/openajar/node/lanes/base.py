"""What every part of the lane manager shares (openajar.node.lanes): the in-memory record of a
lane (_Lane), the two refusals for a lane the node will not warm (ResourcesLow, LaneFormatMismatch),
the callback types, the `by` names the node writes on holds and hand-backs, and the helpers that
clean what a human will read. Nothing here acts on a lane."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openajar.core.errors import EngineError, OpenajarError
from openajar.core.models import LaneControl, LaneState, ProxyConfig, ViewerInfo
from openajar.node.claims import LaneAccess
from openajar.node.interfaces import BrowserSession
from openajar.node.lanefmt import LaneLock

log = logging.getLogger("openajar.lanes")

ProxiesLoader = Callable[[], dict[str, ProxyConfig]]
ControlListener = Callable[[str, LaneControl, LaneControl], Any]
NoticeListener = Callable[[str, dict[str, Any]], Any]  # (lane id, a block notice from the egress guard)
Gate = Callable[[str, str], None]  # (lane_id, principal_kind) -> None, or raise to refuse
HoldPolicy = Callable[..., Any]  # (signal, *, now) -> None | hold (see _as_hold)
NodeIpProbe = Callable[[], Awaitable[str | None]]

HUMAN = "human"                    # the principal kind that passes the control gate
MAX_REASON_CHARS = 300
IDLE_HANDBACK_BY = "idle"          # `by` of an automatic hand-back
NODE_BY = "node"                   # `by` of holds and quarantines the node sets itself
QUARANTINE_HOLD = "quarantined:"   # a quarantine's own hold reason starts with this
# quarantines the self-check writes about a proxy's exit: moving the lane to another proxy ends them
EXIT_IP_QUARANTINES = frozenset({"exit_ip_changed", "exit_ip_mismatch", "exit_ip_is_node"})

_HOLDS_SLOT = (LaneState.WARMING, LaneState.WARM, LaneState.ACTIVE)


class ResourcesLow(EngineError):
    """The node is short of memory or disk, so a lane was not warmed."""
    code = "resources_low"


class LaneFormatMismatch(OpenajarError):
    """The lane's directory is in a format this openajar does not run (newer, or older with a
    migration pending), so it was not warmed (openajar.node.lanefmt)."""
    code = "lane_format"


@dataclass(eq=False)
class _Call:
    """One non-human call inside using() (openajar.node.deadlines): what it is, since when (the
    loop's clock), its deadline, and what happened when the deadline passed."""
    task: asyncio.Task
    tool: str
    started: float
    deadline_s: float | None = None
    waiter: Any = None          # its place on the lane (openajar.node.claims.Waiter)
    page: Any = None
    overdue: bool = False       # the deadline passed while it ran
    timer: asyncio.TimerHandle | None = None
    check: asyncio.Task | None = None  # the probe after the deadline: True when it closed the browser
    free: asyncio.Task | None = None   # then: takes its place from it if it does not stop
    stopped_by: str | None = None      # why it was interrupted: interrupts.DEADLINE or CLOSED


@dataclass
class _Lane:
    state: LaneState = LaneState.COLD
    session: BrowserSession | None = None
    viewer: ViewerInfo | None = None
    exit_ip: str | None = None
    quarantine_reason: str | None = None  # a failed self-check; reopening tries again
    last_active: float = 0.0  # monotonic; for LRU eviction and the idle reaper
    human_at: float | None = None  # monotonic time of the last human input through the viewer
    users: int = 0            # callers inside or waiting to enter using(); never evicted
    viewers: int = 0          # live views a human has open (viewer_attached); never reaped or evicted
    watchers: int = 0         # every open live view, view-only links too (view_opened)
    views_opened: int = 0     # live views ever opened (viewer_attached or view_opened), for views()
    evicting: bool = False
    epoch: int = 0            # bumped by every teardown
    access: LaneAccess = field(default_factory=LaneAccess)  # who is on the lane, who waits, the claim
    op: asyncio.Lock = field(default_factory=asyncio.Lock)
    format_lock: LaneLock | None = None  # shared while warming or warm: migrations keep off
    calls: list[_Call] = field(default_factory=list)  # non-human calls inside using() (deadlines)
    close_note: dict[str, Any] | None = None  # what close() had to do about a stuck call (browser_close says so)

    def idle(self) -> bool:
        return (self.state == LaneState.WARM and self.users == 0 and self.viewers == 0 and not self.op.locked()
                and not self.evicting and not self.access.busy())

    def claimed(self) -> bool:
        return self.access.claim is not None


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def _strip_group_other(root: Path) -> None:
    """chmod go-rwx on a whole tree (profiles hold session cookies)."""
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath, name)
            if not p.is_symlink():
                p.chmod(p.stat().st_mode & 0o700)
    root.chmod(0o700)


def _new_lease() -> str:
    return "lease_" + uuid.uuid4().hex[:16]


def _who(by: str | None) -> str:
    by = (by or "").strip()
    return by[:80] if by else "unknown"


def clean_reason(reason: str | None) -> str:
    """An agent's reason as the human will see it: one line, no control characters, no em
    dashes (UI text rule), at most MAX_REASON_CHARS."""
    text = "".join(" " if unicodedata.category(ch)[0] in "CZ" else ch for ch in reason or "")
    text = re.sub(r"\s*\u2014\s*", ", ", text)
    text = " ".join(text.split()).strip(" ,")
    if len(text) > MAX_REASON_CHARS:
        text = text[: MAX_REASON_CHARS - 3].rstrip() + "..."
    return text


def _host(url: Any) -> str | None:
    """Only the host of a URL goes into a hold's reason (a query string can carry tokens)."""
    if not isinstance(url, str) or "://" not in url:
        return None
    from urllib.parse import urlsplit
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None
