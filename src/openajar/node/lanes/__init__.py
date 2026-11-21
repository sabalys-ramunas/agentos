"""Lane manager: lane specs on disk, warm browsers in memory.

Lifecycle: cold -> warming -> warm <-> active, plus quarantined.

Locks, always taken in this order:
  lane.access  who is on the lane (openajar.node.claims): reads share, acts go one at a time, a
               claim keeps other callers' acts out, and everyone else waits in a FIFO queue for
               at most queue_timeout (then busy, naming the holder and the caller's place)
  lane.op      transition lock: open, close, eviction and reaping of that lane
  _cond        capacity: guards slot reservation; never held across a slow await

Claims: claim() gives a caller a lease on the lane for a
multi-step job; it renews on each of the holder's calls and expires on its own. A take-over, a
pause or hold, a freeze, a revoked grant, or closing the lane to free memory ends it, and the
holder's next call fails once with claim_lost. A claimed lane is never reaped or evicted for the
warm cap; only a memory floor that nothing else can meet closes it. Flow and connector runs claim
implicitly for the whole run (using(mode="run")). Claims live in memory; the ones held at a
restart are noted on disk only so their holders hear, on their next call, that they are gone.

A lane counts against max_warm_lanes while warming, warm or active. A quarantined lane
has no browser and no display, so it does not. A new lane is not warmed while the node's
available memory is below a floor (the least recently used idle lane that agents hold is
closed first; never one a human holds or one in use) or while the disk that holds its
profile is nearly full (a failed cookie write can log the account out).

Closing a browser always runs to the end: a close, eviction or reap that is cancelled part
way finishes tearing the lane down, then raises the cancellation. Every teardown bumps the
lane's epoch, so an open still starting when shutdown gave up on it closes what it started
instead of leaving a browser nothing owns. After shutdown() nothing opens again.

Control (docs/API.md, "Lane control"): every lane is AGENT, HUMAN or PAUSED.
The gate is `_check_control`, which every non-human caller of open(), close() and using()
passes; using() checks it again right before it yields the page, so a take-over that lands
while an agent waits for the lane wins. Control changes are plain synchronous updates of
`_controls` (no await between read and write), persisted to the store, then announced:
sync listeners run first, in the same tick (the viewer relay must stop forwarding input
at once), then async listeners are awaited for up to LISTENER_TIMEOUT. A lane a human
holds is never reaped or evicted; it returns to agents after control_idle_seconds
without human input.

Holds (AUD-07): hold() pauses agents on a lane with a reason, either until a
human hands it back or, for a self-clearing hold (a rate limit), until `hold_until`.
report_signal() turns a bad signal into a hold through `hold_policy` (by default
openajar.node.holds.hold_for when that module exists, else no holds). A hold is a PAUSED
control record with `reason` set, so every agent call, flow step and schedule stops at the
same gate. A human who takes over a held lane and walks away gets the hold back on the idle
hand-back, never agents. quarantine() is a hold plus a closed browser and a persisted
QUARANTINED state; a human's hand back clears both.

Exit IP (AUD-14): the self-check reads the exit IP inside the lane. A lane without a proxy
must leave from the node's own IP (when the node can read it), a proxied lane never may.
A proxy whose host is an IP address must exit from that address. A gateway proxy (a host
name) may exit anywhere: the first exit seen through a label is pinned, and a later change
quarantines every lane on it until a human accepts the new address by handing the lane back.

Gates (the freeze, openajar.freeze): add_gate(check, keep_warm=...) adds a check every non-human
caller passes inside `_check_control`, before the control state is read. A frozen lane is never
reaped or evicted (keep_warm), so its browser stays as it was for a human to inspect. Agent work
inside using() is tracked per lane; interrupt_agents() cancels it at its next await (atomic input
steps run shielded), and using() turns that cancellation into the gate's own error.

Deadlines (openajar.node.deadlines): a tool call takes the lane with an overall deadline from that
module's table. Past it, using() interrupts the call, releases its turn and fails it with
engine_error; a page that then does not answer a probe gets its browser closed (cold, reopened by
the next call). close() and the memory watchdog stop a call that is stuck past its deadline, or on
a page that stopped answering, instead of waiting for it forever.

The package, by module: `base` (_Lane, the callback types, the refusals ResourcesLow and
LaneFormatMismatch, clean_reason), `resources` (memory and disk probes and floors, ScopeLimiter),
`watch` (SignalWatch), and LaneManagerImpl in `manager`, made of the mixins `specs` (create,
import, delete, proxy, sites), `launch` (open and close), `resources`, `quarantine` (the exit IP
self-check, quarantine), `access` (using, the queue and claims) and `control` (control and holds),
with DevicesMixin from openajar.node.devices. This module re-exports every name the single file had,
private ones too. A name tests patch is read in its home module, so patch it there:
`resources.available_memory_mb`, `resources.disk_free_bytes` and `resources.default_scopes_fit`
(read when a manager is made), `quarantine._proxy_ips`, `quarantine.SELF_CHECK_JS`,
`control.LISTENER_TIMEOUT`, and `manager.asyncio` for the waits of shutdown.
"""

from __future__ import annotations

# What the single module had in scope, kept for attribute reach-through (tests import LaneSpec from
# here).
import asyncio  # noqa: F401
import contextlib  # noqa: F401
import errno  # noqa: F401
import importlib  # noqa: F401
import inspect  # noqa: F401
import ipaddress  # noqa: F401
import logging  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
import shutil  # noqa: F401
import socket  # noqa: F401
import time  # noqa: F401
import unicodedata  # noqa: F401
import urllib.request  # noqa: F401
import uuid  # noqa: F401
import weakref  # noqa: F401
from collections.abc import AsyncIterator, Awaitable, Callable  # noqa: F401
from contextlib import asynccontextmanager  # noqa: F401
from dataclasses import dataclass, field  # noqa: F401
from datetime import UTC, datetime, timedelta  # noqa: F401
from functools import partial  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any  # noqa: F401

from openajar.core.aio import run_to_end as _run_to_end  # noqa: F401
from openajar.core.config import NodeConfig, load_proxies  # noqa: F401
from openajar.core.errors import (  # noqa: F401
    AgentPaused,
    ConfirmRequired,
    EngineError,
    HumanInControl,
    InvalidRequest,
    LaneBusy,
    LaneExists,
    LaneNotFound,
    LaneQuarantined,
    OpenajarError,
    ServiceUnavailable,
)
from openajar.core.logs import log_context  # noqa: F401
from openajar.core.models import (  # noqa: F401
    ControlState,
    LaneControl,
    LaneInfo,
    LaneKind,
    LaneSpec,
    LaneState,
    ProxyConfig,
    Signal,
    ViewerInfo,
    utcnow,
)
from openajar.core.signals import QUIET  # noqa: F401
from openajar.core.system import meminfo_mb  # noqa: F401
from openajar.node import gateshapes, human, interrupts, lanefmt  # noqa: F401
from openajar.node.browser_output import browser_error  # noqa: F401
from openajar.node.claims import (  # noqa: F401
    ACT,
    CLAIM,
    DEFAULT_MAX_TOTAL_S,
    READ,
    RUN,
    SYSTEM,
    TICK_S,
    Claim,
    ClaimEnd,
    ClaimLost,
    ClaimsFile,
    Holder,
    LaneAccess,
    Waiter,
    clamp_ttl,
    clean_purpose,
    holder_text,
)
from openajar.node.devices import DevicesMixin  # noqa: F401
from openajar.node.handback import HandbackTracker  # noqa: F401
from openajar.node.interfaces import BrowserSession, DisplayManager, Engine, Page  # noqa: F401
from openajar.node.lanefmt import LaneLock  # noqa: F401
from openajar.node.lanes.access import (  # noqa: F401 - re-exported
    AccessMixin,
)
from openajar.node.lanes.base import (  # noqa: F401 - re-exported
    _HOLDS_SLOT,
    EXIT_IP_QUARANTINES,
    HUMAN,
    IDLE_HANDBACK_BY,
    MAX_REASON_CHARS,
    NODE_BY,
    QUARANTINE_HOLD,
    ControlListener,
    Gate,
    HoldPolicy,
    LaneFormatMismatch,
    NodeIpProbe,
    NoticeListener,
    ProxiesLoader,
    ResourcesLow,
    _host,
    _Lane,
    _new_lease,
    _private_dir,
    _strip_group_other,
    _who,
    clean_reason,
    log,
)
from openajar.node.lanes.control import (  # noqa: F401 - re-exported
    HANDBACK_NOTE_CHARS,
    HANDBACK_READ_S,
    HOLD_EXPIRED_BY,
    INPUT_PERSIST_SECONDS,
    LISTENER_TIMEOUT,
    REQUEST_COOLDOWN_SECONDS,
    SIGNAL_PERSIST_SECONDS,
    ControlMixin,
    _as_hold,
    _aware,
    _is_hold,
    _merge_hold,
    default_hold_policy,
    hold_for_a_human,
)
from openajar.node.lanes.launch import (  # noqa: F401 - re-exported
    LaunchMixin,
)
from openajar.node.lanes.manager import (  # noqa: F401 - re-exported
    _DEFAULT,
    SHUTDOWN_USE_WAIT,
    LaneManagerImpl,
    _setting,
    build_lane_manager,
)
from openajar.node.lanes.quarantine import (  # noqa: F401 - re-exported
    NODE_IP_RETRY,
    NODE_IP_TTL,
    PROBE_USE_WAIT,
    QUARANTINE_USE_WAIT,
    SELF_CHECK_JS,
    SELF_CHECK_URL,
    QuarantineMixin,
    _is_ip,
    _proxy_ips,
    _same_family,
    direct_exit_ip_probe,
)
from openajar.node.lanes.resources import (  # noqa: F401 - re-exported
    DEFAULT_DISK_FLOOR_BYTES,
    DEFAULT_MEMORY_FLOOR_MB,
    LANE_SLICE,
    LANE_TASKS_MAX,
    MEMORY_HIGH_SHARE,
    ResourcesMixin,
    ScopeLimiter,
    _run_command,
    _size,
    available_memory_mb,
    default_scopes_fit,
    disk_free_bytes,
)
from openajar.node.lanes.specs import (  # noqa: F401 - re-exported
    NOT_RUNNING_KINDS,
    PROFILE_LOCKS,
    SpecsMixin,
    _browsers_using,
    _claim,
    _clean_profile_copy,
    _first_error,
    _profile_lock_owner,
    _read_fingerprint,
)
from openajar.node.lanes.watch import (  # noqa: F401 - re-exported
    SignalWatch,
)
from openajar.node.memwatch import MemoryWatch, resolve_ceiling  # noqa: F401
from openajar.node.store import LaneRecord, LaneStore, QuarantineRecord, SignalRecord  # noqa: F401
