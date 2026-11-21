"""Policy rules for workspace memory (docs/design/workspace-memory.md, sections 4.5 to 4.7).

Pure functions over grants and routing facts, no I/O. The hooks that call them in grants.py,
service.py, routing.py and tools.py are each marked with a `memory:` comment.

The `memory` special in a grant block's specials:

    "memory": {"workspaces": ["biz-a"], "write": true, "types": ["fact", "document"],
               "private": false, "audience_owner": false}

- `workspaces`: workspace names or patterns (`biz-*`); None means every workspace the block's
  lane scope reaches. A workspace is reached when one of the block's lane patterns has a
  workspace part matching it, so a grant scoped to `biz-a/instagram` that holds memory for biz-a
  reads the whole workspace's memory.
- `write`: false means the read tools only. `types`: the entry types the grant may write (None:
  every type). `private`: may read entries labelled private. `audience_owner`: may read
  `audience: owner` entries; it only takes effect in an admin grant.

Every flag defaults to off, so `{"memory": {}}` is read-only memory over the lane scope. A call
must satisfy every block on its own, like every special: workspaces and types are intersected
along the chain, and a flag holds only when every block sets it.

Routing facts: `memory_owner_trust` (a memory write that would carry owner trust, a decision or
a supersede of an owner entry) always waits for a one-off approval, never a standing one, even
if an operator's routing table says auto. `internal` (the destination is the workspace's own
memory on this node) is inside the egress allowlist for the lethal-trifecta overlay, but only
for a call that names no domain.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, field_validator

from openajar.core import globs
from openajar.core.errors import InvalidRequest
from openajar.core.models import Effect
from openajar.policy.errors import Denied

if TYPE_CHECKING:
    from openajar.policy.grants import Grant, GrantBlock
    from openajar.policy.routing import Rule

log = logging.getLogger("openajar.policy.memory")

MEMORY_TYPES = ("fact", "document", "decision", "preference", "contact", "run_summary")
MEMORY_TAG = "memory"          # the ToolSpec tag of memory tools: hidden from grants without the special
FLAGS = ("write", "private", "audience_owner")
OWNER_TRUST_FACT = "memory_owner_trust"
INTERNAL_FACT = "internal"


class MemoryCap(BaseModel):
    """The `memory` special (design 4.7)."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspaces: list[str] | None = None
    write: bool = False
    types: list[str] | None = None
    private: bool = False
    audience_owner: bool = False

    @field_validator("workspaces")
    @classmethod
    def _workspaces(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else [globs.validate_segment(w) for w in v]

    @field_validator("types")
    @classmethod
    def _types(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            bad = [t for t in v if t not in MEMORY_TYPES]
            if bad:
                raise ValueError(f"memory types are {', '.join(MEMORY_TYPES)}, not {bad[0][:40]!r}")
        return v

    @classmethod
    def everything(cls) -> MemoryCap:
        return cls(write=True, private=True, audience_owner=True)


@dataclass(frozen=True)
class MemoryScope:
    """What a grant may do with one workspace's memory, after every block of its chain."""
    workspace: str
    write: bool
    types: frozenset[str] | None   # None: every type
    private: bool
    audience_owner: bool

    def may_write(self, entry_type: str | None = None) -> bool:
        return self.write and (entry_type is None or self.types is None or entry_type in self.types)

    def public(self) -> dict:
        return {"workspace": self.workspace, "write": self.write,
                "types": sorted(self.types) if self.types is not None else None,
                "private": self.private, "audience_owner": self.audience_owner}


# ---------------------------------------------------------------- scope

def _pattern_reaches(lane_pattern: str, workspace: str) -> bool:
    """Does a lane pattern cover at least one lane of `workspace`?"""
    if lane_pattern == "*":
        return True
    ws_part = lane_pattern.split("/", 1)[0]
    return globs.segment_match(ws_part, workspace)


def _segments_overlap(a: str, b: str) -> bool:
    return globs.segment_within(a, b) or globs.segment_within(b, a)


def lane_scope_reaches(grant: Grant, workspace: str) -> bool:
    """Every block's lane scope reaches some lane of `workspace` (a block with no scope of its own
    inherits). The authority block with no scope reaches nothing."""
    for i, b in enumerate(grant.blocks):
        pats = b.lane_patterns(authority=i == 0)
        if pats is None:
            continue
        if not any(_pattern_reaches(p, workspace) for p in pats):
            return False
    return True


def has_memory(grant: Grant) -> bool:
    return grant.caps("memory") is not None


def memory_scope(grant: Grant, workspace: str) -> MemoryScope:
    """The grant's memory scope for one workspace. Raises Denied: `special` without the memory
    special, `out_of_scope` when the workspace is outside its memory workspaces or lane scope."""
    try:
        globs.validate_segment(workspace)
    except InvalidRequest:
        raise Denied("out_of_scope", "a memory call names one workspace by its id") from None
    if "*" in workspace:
        raise Denied("out_of_scope", "a memory call names one workspace, not a pattern")
    caps = grant.caps("memory")
    if caps is None:
        raise Denied("special", "this grant does not include memory")
    for cap in caps:
        if cap.workspaces is not None and not any(globs.segment_match(p, workspace) for p in cap.workspaces):
            raise Denied("out_of_scope", f"this grant's memory does not cover workspace {workspace}")
    if not lane_scope_reaches(grant, workspace):
        raise Denied("out_of_scope", f"this grant does not cover workspace {workspace}")
    types: frozenset[str] | None = None
    for cap in caps:
        if cap.types is not None:
            types = frozenset(cap.types) if types is None else types & frozenset(cap.types)
    return MemoryScope(
        workspace=workspace,
        write=all(c.write for c in caps),
        types=types,
        private=all(c.private for c in caps),
        audience_owner=grant.admin and all(c.audience_owner for c in caps),
    )


def check_memory(grant: Grant, workspace: str, *, write: bool = False, entry_type: str | None = None,
                 private: bool = False, audience_owner: bool = False) -> MemoryScope:
    """memory_scope plus the needs of one call. Raises Denied (`special`) for each one missing."""
    scope = memory_scope(grant, workspace)
    if write and not scope.write:
        raise Denied("special", f"this grant may read workspace {workspace}'s memory, not write it")
    if write and entry_type is not None and not scope.may_write(entry_type):
        raise Denied("special", f"this grant may not write {entry_type} entries")
    if private and not scope.private:
        raise Denied("special", "this grant may not read private memory entries")
    if audience_owner and not scope.audience_owner:
        raise Denied("special", "this grant may not read entries meant for the owner only")
    return scope


def describe(grant: Grant) -> dict | None:
    """The memory special as grant_info shows it: the workspace patterns every block allows (None:
    the lane scope decides) and the flags that hold along the chain."""
    caps = grant.caps("memory")
    if caps is None:
        return None
    # a narrowed block's list sits inside its parents' (the widening check), so the last one is the
    # tightest; evaluation intersects them all anyway
    workspaces = next((c.workspaces for c in reversed(caps) if c.workspaces is not None), None)
    types: set[str] | None = None
    for c in caps:
        if c.types is not None:
            types = set(c.types) if types is None else types & set(c.types)
    return {
        "workspaces": workspaces,
        "write": all(c.write for c in caps), "types": sorted(types) if types is not None else None,
        "private": all(c.private for c in caps),
        "audience_owner": grant.admin and all(c.audience_owner for c in caps),
    }


def spec_visible(grant: Grant, spec: Any) -> bool:
    """Memory tools (tagged `memory`) are hidden from grants without the special, and the ones
    that write (any effect but read) from grants whose memory is read-only."""
    if MEMORY_TAG not in getattr(spec, "tags", ()):
        return True
    caps = grant.caps("memory")
    if caps is None:
        return False
    return spec.effect == Effect.READ or all(c.write for c in caps)


def child_cap(parent: Grant, workspace: str) -> MemoryCap | None:
    """The memory special a brain's child token gets for its task's workspace (design 4.7): write,
    the parent's private setting, never audience_owner. None when the parent cannot write there."""
    try:
        scope = memory_scope(parent, workspace)
    except Denied:
        return None
    if not scope.write:
        return None
    return MemoryCap(workspaces=[workspace], write=True,
                     types=sorted(scope.types) if scope.types is not None else None,
                     private=scope.private, audience_owner=False)


# ---------------------------------------------------------------- narrowing

def cap_widening(child: MemoryCap, parent: MemoryCap) -> list[str]:
    """Fields of a child's memory special wider than one parent block's: workspaces and types may
    only shrink, flags may only turn off."""
    out: list[str] = []
    if parent.workspaces is not None and (
            child.workspaces is None
            or not all(any(globs.segment_within(c, p) for p in parent.workspaces) for c in child.workspaces)):
        out.append("workspaces")
    if parent.types is not None and (child.types is None or not set(child.types) <= set(parent.types)):
        out.append("types")
    out += [f for f in FLAGS if getattr(child, f) and not getattr(parent, f)]
    return out


def _scope_problems(cap: MemoryCap | None, lane_patterns: list[str] | None) -> list[str]:
    if cap is None or cap.workspaces is None or lane_patterns is None:
        return []
    ws_parts = ["*" if p == "*" else p.split("/", 1)[0] for p in lane_patterns]
    if all(any(_segments_overlap(w, part) for part in ws_parts) for w in cap.workspaces):
        return []
    return ["specials.memory.workspaces"]


def block_scope_problems(block: GrantBlock, parent: Grant | None = None) -> list[str]:
    """A block's memory workspaces must sit inside its lane scope (its own, else the parent's
    chain): `specials.memory.workspaces` when one reaches no lane of it."""
    cap = block.specials.memory if block.specials is not None else None
    pats = block.lane_patterns(authority=parent is None)
    problems = _scope_problems(cap, pats)
    if pats is None and parent is not None:
        for i, b in enumerate(parent.blocks):
            problems += _scope_problems(cap, b.lane_patterns(authority=i == 0))
    return sorted(set(problems))


def check_authority_scope(block: GrantBlock) -> None:
    """For issuing a root grant: ValueError when the memory workspaces fall outside the lane scope."""
    if block.parent is None and block_scope_problems(block):
        raise ValueError("the memory special names a workspace outside the grant's lanes and workspaces")


# ---------------------------------------------------------------- routing

def routing_facts(facts: dict) -> dict[str, bool]:
    """The memory routing facts a handler stated (only an explicit True counts)."""
    return {OWNER_TRUST_FACT: facts.get(OWNER_TRUST_FACT) is True, INTERNAL_FACT: facts.get(INTERNAL_FACT) is True}


def internal_destination(facts: dict, host: str | None) -> bool:
    """An internal call (the workspace's own memory on this node) is inside the egress allowlist.
    A call that also names a domain is not internal: it would carry data there."""
    return facts.get(INTERNAL_FACT) is True and host is None


def standing_allowed(rfacts: dict) -> bool:
    """A standing approval never covers an owner-trust memory write: the owner sees the exact text."""
    return not rfacts.get(OWNER_TRUST_FACT)


def guard(rule: Rule, rfacts: dict) -> Rule:
    """An owner-trust memory write needs a human even when a replaced routing table says auto."""
    if rfacts.get(OWNER_TRUST_FACT) and rule.then == "auto":
        return rule.model_copy(update={"then": "approval",
                                       "note": "owner-trust memory always waits for the owner"})
    return rule


# ---------------------------------------------------------------- context receipts on commits

def commit_receipt(ctx: Any, effect: Any, lane_id: str | None) -> dict:
    """`{"decisions_loaded": current | stale | never}` for a commit on a workspace lane, when the
    memory service has wired its decisions digest getter (see memory_port.MemoryPolicy); else {}.
    It decides nothing and never fails the call."""
    try:
        if str(getattr(effect, "value", effect)) != Effect.COMMIT.value or ctx.in_person:
            return {}
        lane = lane_id or ctx.lane_id
        port = getattr(ctx.policy, "memory_rules", None)
        if not lane or "/" not in lane or port is None:
            return {}
        state = port.decisions_loaded(ctx.taint_key, lane.split("/", 1)[0])
        return {"decisions_loaded": state} if state else {}
    except Exception:
        log.exception("decisions_loaded could not be computed")
        return {}
