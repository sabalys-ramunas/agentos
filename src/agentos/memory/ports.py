"""What the memory service needs from the rest of the node, as small ports.

The service never imports another package's implementation. `build(runtime)` wires these
ports to the runtime's services through `RuntimePolicyPort` and the helpers below; tests use the
fakes in tests/testkit/memory.py instead.

Policy port (Brief B implements the missing pieces in agentos.policy; until then the adapter
falls back as documented on each method):

    taint_ever(family) -> set[str]        labels the family has seen since its root grant
    taint_add(family, labels) -> None     add labels to the family (reading memory re-taints)
    trace_visible(trace_id, family, workspace) -> bool
    trace_snapshot(trace_id) -> {tool, at, status, summary, lane, labels} | None
    lane_url_at(trace_id) -> str | None   the lane's URL as the node saw it at that trace
    record_receipt(family, workspace, digest) -> None   optional (context receipts)
    pin(trace_id, ref) / unpin(ref) -> ...              optional (evidence survives trace retention)

On a node with Brief B's policy, every call goes to `runtime.require("policy").memory_rules`
(agentos.policy.memory_port.MemoryPolicy); the older fallbacks stay for nodes without it.

`family` is the policy family key ("family:<root grant id>"); None means the owner in person
or the node, who see every trace and neither add nor receive labels.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger("agentos.memory")


@runtime_checkable
class PolicyPort(Protocol):
    def taint_ever(self, family: str) -> set[str]: ...

    def taint_add(self, family: str, labels: Iterable[str]) -> None: ...

    def trace_visible(self, trace_id: str, family: str | None, workspace: str) -> bool: ...

    def trace_snapshot(self, trace_id: str) -> dict[str, Any] | None: ...

    def lane_url_at(self, trace_id: str) -> str | None: ...


def _root_of(family: str | None) -> str | None:
    if not family:
        return None
    return family.split(":", 1)[1] if family.startswith("family:") else family


def lane_workspace(lane: str | None) -> str | None:
    return lane.split("/", 1)[0] if lane and "/" in lane else None


class RuntimePolicyPort:
    """The policy port over the runtime's `policy` and `traces` services."""

    def __init__(self, runtime: Any):
        self.runtime = runtime
        self._warned = False

    def _svc(self, name: str) -> Any:
        return self.runtime.get(name) if self.runtime is not None else None

    def rules(self) -> Any:
        """The policy's memory port (`policy.memory_rules`, Brief B), or None."""
        return getattr(self._svc("policy"), "memory_rules", None)

    def taint_ever(self, family: str) -> set[str]:
        """`policy.memory_rules.taint_ever(family)`. Without it: `policy.taint_ever`, else the
        12-hour ledger, which is weaker for trust (an agent that read a page yesterday writes
        `agent`), logged once."""
        rules = self.rules()
        if rules is not None:
            return set(rules.taint_ever(family))
        policy = self._svc("policy")
        if policy is None:
            return set()
        fn = getattr(policy, "taint_ever", None)
        if callable(fn):
            return set(fn(family))
        ledger = getattr(policy, "taint", None)
        ever = getattr(ledger, "ever", None)
        if callable(ever):
            return set(ever(family))
        if not self._warned:
            log.warning("policy has no taint_ever ledger yet; memory trust uses the 12 hour labels")
            self._warned = True
        get = getattr(ledger, "get", None)
        return set(get(family)) if callable(get) else set()

    def taint_add(self, family: str, labels: Iterable[str]) -> None:
        labels = sorted(set(labels))
        if not labels:
            return
        rules = self.rules()
        if rules is not None:
            rules.taint_add(family, labels)
            return
        policy = self._svc("policy")
        if policy is None:
            return
        fn = getattr(policy, "add_labels", None)
        if callable(fn):
            fn(family, labels)
            return
        ledger = getattr(policy, "taint", None)
        if ledger is not None and callable(getattr(ledger, "add", None)):
            ledger.add(family, labels, "memory")

    def _trace(self, trace_id: str) -> Any:
        traces = self._svc("traces")
        get = getattr(traces, "get", None)
        return get(trace_id) if callable(get) else None

    def trace_visible(self, trace_id: str, family: str | None, workspace: str) -> bool:
        """`traces.visible(trace_id, viewer_path, workspace)` (Brief B), else the same rule
        read from the trace: it exists, it is in this workspace, and the family is on its
        grant path (the `viewer_path` rule of traces_query)."""
        rules = self.rules()
        if rules is not None:
            return bool(rules.trace_visible(trace_id, family, workspace))
        traces = self._svc("traces")
        root = _root_of(family)
        fn = getattr(traces, "visible", None)
        if callable(fn):
            return bool(fn(trace_id, root, workspace))
        ev = self._trace(trace_id)
        if ev is None:
            return False
        ws = getattr(ev, "workspace", None) or lane_workspace(getattr(ev, "lane", None))
        if ws != workspace:
            return False
        if root is None:
            return True
        return f"/{root}/" in (getattr(ev, "grant_path", None) or "")

    def trace_snapshot(self, trace_id: str) -> dict[str, Any] | None:
        """A small record of the trace, with the labels its kind carries (`taint_kinds`)."""
        policy = self._svc("policy")
        kinds = getattr(getattr(policy, "cfg", None), "taint_kinds", {}) or {}
        rules = self.rules()
        if rules is not None:
            snap = rules.trace_snapshot(trace_id)
            if snap is None:
                return None
            return {**snap, "labels": sorted(kinds.get(snap.get("kind") or "", []))}
        ev = self._trace(trace_id)
        if ev is None:
            return None
        return {"tool": getattr(ev, "tool", None), "at": getattr(ev, "at", None),
                "status": getattr(ev, "status", None), "summary": getattr(ev, "summary", None),
                "lane": getattr(ev, "lane", None), "labels": sorted(kinds.get(getattr(ev, "kind", ""), []))}

    def lane_url_at(self, trace_id: str) -> str | None:
        """`traces.lane_url_at(trace_id)` when the trace store records it. Never a URL from
        the call's arguments: that is what the agent typed, not what the node saw."""
        rules = self.rules()
        if rules is not None:
            return rules.lane_url_at(trace_id)
        traces = self._svc("traces")
        fn = getattr(traces, "lane_url_at", None)
        return fn(trace_id) if callable(fn) else None

    def record_receipt(self, family: str, workspace: str, digest: str) -> None:
        fn = getattr(self.rules() or self._svc("policy"), "record_receipt", None)
        if callable(fn):
            fn(family, workspace, digest)

    def pin(self, trace_id: str, ref: str) -> bool:
        """Keep an evidence trace through retention while the entry `ref` needs it."""
        fn = getattr(self.rules() or self._svc("traces"), "pin", None)
        return bool(fn(trace_id, ref)) if callable(fn) else False

    def unpin(self, ref: str) -> int:
        fn = getattr(self.rules() or self._svc("traces"), "unpin", None)
        return int(fn(ref) or 0) if callable(fn) else 0


def runtime_vault_scrub(runtime: Any) -> Callable[[str], str] | None:
    vault = runtime.get("vault") if runtime is not None else None
    fn = getattr(vault, "scrub", None)
    if not callable(fn):
        return None
    return lambda text: fn(text)


def runtime_privacy_for(runtime: Any) -> Callable[[str], str]:
    """The brains package's privacy level per workspace; full-cloud (the brains default) when
    brains is not installed."""
    def privacy_for(workspace: str) -> str:
        brains = runtime.get("brains") if runtime is not None else None
        fn = getattr(brains, "privacy_for", None)
        if not callable(fn):
            return "full-cloud"
        return str(getattr(fn(workspace), "value", fn(workspace)))
    return privacy_for


def runtime_cloud_ok(runtime: Any) -> Callable[[str], bool]:
    """`memory_cloud_ok` on the workspace record (Brief C adds it to agentos.workspaces)."""
    def cloud_ok(workspace: str) -> bool:
        ws = runtime.get("workspaces") if runtime is not None else None
        get = getattr(ws, "get", None)
        if not callable(get):
            return False
        try:
            record = get(workspace)
        except Exception:
            log.debug("no workspace record for %s; memory_cloud_ok is off", workspace, exc_info=True)
            return False
        value = record.get("memory_cloud_ok") if isinstance(record, dict) else getattr(record, "memory_cloud_ok", False)
        return bool(value)
    return cloud_ok


@dataclass
class MemoryPorts:
    policy: PolicyPort | None = None
    vault_scrub: Callable[[str], str] | None = None
    privacy_for: Callable[[str], str] = field(default=lambda _ws: "full-cloud")
    cloud_ok: Callable[[str], bool] = field(default=lambda _ws: False)
    egress_allowed: Callable[[str, str], bool] | None = None       # (workspace, host) -> on the allowlist
    workspace_exists: Callable[[str], bool] | None = None
    on_event: Callable[[str, dict[str, Any]], None] | None = None  # "memory_write" and friends, no content
    node_id: str | None = None
