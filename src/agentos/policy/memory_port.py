"""The policy port of the memory service (docs/design/workspace-memory.md, Brief A and C).

`PolicyService.memory_rules` is a `MemoryPolicy`. agentos.memory takes it as its `policy` port,
so the memory package never reaches into policy internals:

    port = runtime.require("policy").memory_rules
    caller = port.caller(ctx, "biz-a", write=True, entry_type="fact")  # Denied when out of scope
    labels = port.taint_ever(caller.family)                            # trust uses the `ever` view
    port.trace_visible(trace_id, caller.viewer, "biz-a")               # evidence check
    port.trace_snapshot(trace_id); port.lane_url_at(trace_id)          # provenance
    port.taint_add(caller.family, ["untrusted"])                       # labels travel on read
    port.pin(trace_id, "mem_..."); port.unpin("mem_...")               # evidence survives retention
    port.record_receipt(caller.family, "biz-a", digest)                # memory_context receipts
    port.set_decisions_digest(memory.decisions_digest)                 # commit traces: decisions_loaded

`family` arguments are taint ledger keys (`family:<root grant id>`, see service.family_key).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from agentos.core.errors import InvalidRequest
from agentos.policy.labels import KNOWN_LABELS, ReceiptState, ReceiptStore
from agentos.policy.memory_rules import MemoryScope, check_memory


@dataclass(frozen=True)
class MemoryCaller:
    """Who is calling a memory operation, as the node sees it (never from the call's arguments)."""
    principal_id: str
    principal_kind: str
    thread: str | None
    grant_id: str
    root_id: str
    grant_path: str
    family: str             # the taint ledger key of the grant family
    viewer: str | None      # trace visibility: the grant id, None for the owner and admin grants
    in_person: bool         # a signed-in panel session or the node CLI: the only owner trust
    owner_token: bool       # the node token over MCP: still a model, never owner trust
    admin: bool
    lane: str | None
    run_id: str | None
    scope: MemoryScope

    def prov(self) -> dict:
        """The provenance fields the node fills for a version (design 4.3)."""
        return {"principal": {"id": self.principal_id, "kind": self.principal_kind, "thread": self.thread},
                "grant": {"id": self.grant_id, "root": self.root_id, "path": self.grant_path},
                "in_person": self.in_person, "lane": self.lane, "run_id": self.run_id}


class MemoryPolicy:
    """See the module docstring. Built by PolicyService; holds no state of its own besides the
    context receipts table and the decisions digest getter memory wires in."""

    def __init__(self, policy: Any):
        self.policy = policy
        self.receipts = ReceiptStore(policy.db, clock=policy.clock)
        self._decisions_digest: Callable[[str], str | None] | None = None

    # ---- callers and scope ----
    def caller(self, ctx: Any, workspace: str, *, write: bool = False, entry_type: str | None = None,
               private: bool = False, audience_owner: bool = False) -> MemoryCaller:
        """The caller of one memory call on `workspace`, after the grant's memory scope check
        (Denied `special` or `out_of_scope`)."""
        g = ctx.grant
        scope = check_memory(g, workspace, write=write, entry_type=entry_type, private=private,
                             audience_owner=audience_owner)
        self._check_frozen(ctx, workspace)
        p = ctx.principal
        return MemoryCaller(
            principal_id=p.id, principal_kind=p.kind, thread=getattr(p, "thread", None), grant_id=g.id,
            root_id=g.root_id, grant_path=g.path, family=ctx.taint_key,
            viewer=None if g.admin else g.id, in_person=bool(getattr(ctx, "in_person", False)),
            owner_token=bool(g.owner_token), admin=g.admin, lane=ctx.lane_id,
            run_id=getattr(getattr(ctx, "meta", None), "run_id", None), scope=scope)

    def _check_frozen(self, ctx: Any, workspace: str) -> None:
        """agentos.freeze: no agent reads or changes the memory of a frozen workspace, whatever its
        grant (a wide grant a workspace freeze kept included). A person in person still looks."""
        if getattr(ctx, "in_person", False):
            return
        runtime = getattr(self.policy, "runtime", None)
        freeze = runtime.get("freeze") if runtime is not None else None
        if freeze is not None and callable(getattr(freeze, "frozen_for", None)):
            freeze.check_call(None, tool=getattr(ctx, "tool", None), workspace=workspace)

    def scope(self, grant: Any, workspace: str) -> MemoryScope:
        return check_memory(grant, workspace)

    # ---- labels ----
    def taint_ever(self, family: str) -> set[str]:
        """Every label the family has seen since its root grant was issued (trust, not routing)."""
        return self.policy.taint.ever(family)

    def taint_live(self, family: str) -> set[str]:
        """The family's 12-hour labels (what routing and the trifecta use)."""
        return self.policy.taint.get(family)

    def taint_add(self, family: str, labels: Iterable[str], source: str = "memory") -> None:
        """Add labels to a family (both views), as reading memory does. Callers skip this for the
        owner in person, who neither adds nor receives labels."""
        labels = set(labels)
        bad = labels - KNOWN_LABELS
        if bad:
            raise InvalidRequest(f"unknown taint labels: {sorted(bad)}")
        if labels:
            self.policy.taint.add(family, labels, source)

    add_labels = taint_add

    # ---- evidence ----
    def trace_visible(self, trace_id: str, viewer: str | None, workspace: str) -> bool:
        """The traces_query rule: `viewer` is a grant id (it sees its own calls and its narrowed
        grants'), a family key `family:<root>` (the whole family), or None (owner and admin)."""
        if isinstance(viewer, str) and viewer.startswith("family:"):
            viewer = viewer.removeprefix("family:")
        return self.policy.traces.visible(trace_id, viewer, workspace)

    def trace_snapshot(self, trace_id: str) -> dict | None:
        return self.policy.traces.snapshot(trace_id)

    def lane_url_at(self, trace_id: str) -> str | None:
        return self.policy.traces.page_url_at(trace_id)

    def pin(self, trace_id: str, ref: str) -> bool:
        return self.policy.traces.pin(trace_id, ref)

    def unpin(self, ref: str, trace_id: str | None = None) -> int:
        return self.policy.traces.unpin(ref, trace_id)

    # ---- context receipts ----
    def record_receipt(self, family: str, workspace: str, digest: str) -> None:
        self.receipts.record(family, workspace, digest)

    def receipt_state(self, family: str, workspace: str, digest: str) -> ReceiptState:
        return self.receipts.state(family, workspace, digest)

    def set_decisions_digest(self, fn: Callable[[str], str | None] | None) -> None:
        """Memory's getter for a workspace's current decisions digest (None: nothing to load)."""
        self._decisions_digest = fn

    def decisions_loaded(self, family: str, workspace: str) -> ReceiptState | None:
        """For a commit trace: None when memory is not wired or the workspace has no decisions."""
        if self._decisions_digest is None:
            return None
        digest = self._decisions_digest(workspace)
        if not digest:
            return None
        return self.receipts.state(family, workspace, digest)

    # ---- retention ----
    def purge(self) -> int:
        return self.receipts.purge()
