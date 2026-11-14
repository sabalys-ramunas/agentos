"""In-memory stand-ins for the policy, vault and privacy ports, so memory runs without a real
policy service (tests, and `agentos dev up` until Brief B lands). Same pattern as
workspaces/fakes.py.

`FakePolicy` keeps two ledgers like the real policy will: the 12-hour labels routing uses
(`taint_live`) and the `ever` view memory trust uses, plus traces and context receipts.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentos.memory.models import Caller, MemoryScope
from agentos.memory.ports import MemoryPorts
from agentos.memory.service import MemoryService

TAINT_TTL_S = 12 * 3600


class FakeClock:
    def __init__(self, start: float = 1_793_000_000.0):   # 2026-10-26
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class FakeTrace:
    id: str
    workspace: str
    family: str | None             # the family whose grant made the call ("family:<root>")
    tool: str = "browser_snapshot"
    at: float = 0.0
    status: str = "ok"
    summary: str | None = None
    lane: str | None = None
    labels: list[str] = field(default_factory=list)
    url: str | None = None


class FakePolicy:
    """The memory policy port over dicts."""

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.traces: dict[str, FakeTrace] = {}
        self._live: dict[str, dict[str, float]] = {}      # family -> label -> last added
        self._ever: dict[str, set[str]] = {}
        self.receipts: dict[tuple[str, str], tuple[str, float]] = {}
        self.added: list[tuple[str, tuple[str, ...]]] = []

    # what other packages would do: a tool call reading a page labels the family
    def read_untrusted(self, family: str) -> None:
        self.label(family, ["untrusted"])

    def label(self, family: str, labels: Iterable[str]) -> None:
        for label in labels:
            self._live.setdefault(family, {})[label] = self.clock()
            self._ever.setdefault(family, set()).add(label)

    def taint_live(self, family: str) -> set[str]:
        now = self.clock()
        return {k for k, at in self._live.get(family, {}).items() if at > now - TAINT_TTL_S}

    def add_trace(self, trace: FakeTrace) -> FakeTrace:
        if not trace.at:
            trace.at = self.clock()
        self.traces[trace.id] = trace
        return trace

    # ---- the port ----
    def taint_ever(self, family: str) -> set[str]:
        return set(self._ever.get(family, set()))

    def taint_add(self, family: str, labels: Iterable[str]) -> None:
        labels = tuple(sorted(labels))
        self.added.append((family, labels))
        self.label(family, labels)

    def trace_visible(self, trace_id: str, family: str | None, workspace: str) -> bool:
        t = self.traces.get(trace_id)
        if t is None or t.workspace != workspace:
            return False
        return family is None or t.family == family

    def trace_snapshot(self, trace_id: str) -> dict[str, Any] | None:
        t = self.traces.get(trace_id)
        if t is None:
            return None
        return {"tool": t.tool, "at": t.at, "status": t.status, "summary": t.summary, "lane": t.lane,
                "labels": list(t.labels)}

    def lane_url_at(self, trace_id: str) -> str | None:
        t = self.traces.get(trace_id)
        return t.url if t else None

    def record_receipt(self, family: str, workspace: str, digest: str) -> None:
        self.receipts[(family, workspace)] = (digest, self.clock())


class FakeVault:
    """`scrub` replaces known secret values, like VaultService.scrub."""

    def __init__(self, values: Iterable[str] = ()):
        self.values = set(values)

    def scrub(self, text: str) -> str:
        for v in sorted(self.values, key=len, reverse=True):
            text = text.replace(v, "[redacted]")
        return text


@dataclass
class FakeMemory:
    service: MemoryService
    policy: FakePolicy
    vault: FakeVault
    clock: FakeClock
    privacy: dict[str, str]
    cloud_ok: set[str]
    events: list[tuple[str, dict]]

    def agent(self, workspace: str | Iterable[str] = "biz-a", *, family: str = "family:gr_a",
              principal: str = "claude-code", write: bool = True, private: bool = False,
              audience_owner: bool = False, types: Iterable[str] | None = None, lanes: Iterable[str] = (),
              cloud: bool = True, kind: str = "agent", lane: str | None = None) -> Caller:
        """An MCP caller with the `memory` special for `workspace`."""
        wss = [workspace] if isinstance(workspace, str) else list(workspace)
        scope = MemoryScope.from_cap({"workspaces": wss, "write": write, "private": private,
                                      "audience_owner": audience_owner,
                                      "types": list(types) if types is not None else None}, lanes=tuple(lanes))
        root = family.split(":", 1)[-1]
        return Caller(kind=kind, principal=principal, family=family, scope=scope, grant_id=root, grant_root=root,
                      grant_path=f"/{root}/", lane=lane, cloud=cloud, write_trace="tr_call")


def fake_memory(home: Path, *, vault_values: Iterable[str] = (), node_id: str = "box-1", **service_kw: Any) -> FakeMemory:
    clock = service_kw.pop("clock", None) or FakeClock()
    policy = FakePolicy(clock)
    vault = FakeVault(vault_values)
    privacy: dict[str, str] = {}
    cloud_ok: set[str] = set()
    events: list[tuple[str, dict]] = []
    ports = MemoryPorts(policy=policy, vault_scrub=vault.scrub,
                        privacy_for=lambda ws: privacy.get(ws, "full-cloud"), cloud_ok=lambda ws: ws in cloud_ok,
                        on_event=lambda kind, payload: events.append((kind, payload)), node_id=node_id)
    service = MemoryService(home, ports, clock=clock, **service_kw)
    return FakeMemory(service, policy, vault, clock, privacy, cloud_ok, events)
