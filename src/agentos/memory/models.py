"""Memory models: entry types and their attributes, trust, who is calling, limits.

The shapes follow docs/design/workspace-memory.md sections 4.2 to 4.12. Everything a writer
sends is validated here; everything the node decides (trust, labels, provenance) is computed
by the service and never read from the request.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from agentos.core.errors import InvalidRequest

WORKSPACE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,119}$")
ENTRY_ID_RE = re.compile(r"^mem_[a-z0-9]{12,32}$")
REF_RE = re.compile(r"^(mem_[a-z0-9]{12,32})@([1-9][0-9]{0,8})$")
TRACE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

LABELS = frozenset({"untrusted", "private"})   # the policy labels that travel with memory
RUN_STATUSES = ("succeeded-verified", "succeeded-unverified", "partial", "failed", "cancelled")
DAY = 86400.0


class EntryType(StrEnum):
    FACT = "fact"
    DOCUMENT = "document"
    DECISION = "decision"
    PREFERENCE = "preference"
    CONTACT = "contact"
    RUN_SUMMARY = "run_summary"


class Trust(StrEnum):
    """Computed by the node at write time (4.4). A writer never chooses it."""
    OWNER = "owner"
    NODE = "node"
    AGENT = "agent"
    UNTRUSTED = "untrusted"

    @property
    def rank(self) -> int:
        return _TRUST_RANK[self]

    @classmethod
    def lowest(cls, *levels: Trust) -> Trust:
        return min(levels, key=lambda t: t.rank)


_TRUST_RANK = {Trust.OWNER: 3, Trust.NODE: 2, Trust.AGENT: 1, Trust.UNTRUSTED: 0}


class Audience(StrEnum):
    WORKSPACE = "workspace"
    OWNER = "owner"


class State(StrEnum):
    LIVE = "live"
    SUPERSEDED = "superseded"   # reserved; versions supersede each other inside one entry
    EXPIRED = "expired"
    FORGOTTEN = "forgotten"


class LinkKind(StrEnum):
    RELATES = "relates"
    DERIVED_FROM = "derived_from"
    CONTESTS = "contests"


# ---- time ------------------------------------------------------------------------

_REL_RE = re.compile(r"^(\d{1,5})([mhdw])$")
_REL_UNIT = {"m": 60.0, "h": 3600.0, "d": DAY, "w": 7 * DAY}


def parse_when(value: Any, *, now: float | None = None, name: str = "time") -> float | None:
    """Epoch seconds from an epoch number, an ISO 8601 string, or a relative age before `now`
    ("30m", "24h", "7d", "2w")."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise InvalidRequest(f"{name} must be a time")
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).timestamp()
    text = str(value).strip()
    m = _REL_RE.match(text)
    if m and now is not None:
        return now - int(m.group(1)) * _REL_UNIT[m.group(2)]
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise InvalidRequest(f"{name} must be an ISO 8601 time or an age like 7d or 24h") from None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---- attributes per type (4.2) ----------------------------------------------------

class _Attrs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FactAttrs(_Attrs):
    subject: str | None = Field(default=None, max_length=200)
    valid_from: float | str | None = None
    valid_to: float | str | None = None


class FileRef(_Attrs):
    path: str = Field(max_length=500)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DocumentAttrs(_Attrs):
    summary: str | None = Field(default=None, max_length=500)
    pinned: bool = False                         # owner only
    refs: list[FileRef] = Field(default_factory=list, max_length=50)


class DecisionAttrs(_Attrs):
    scope: list[str] = Field(default_factory=list, max_length=50)   # lane patterns; empty: the workspace
    enforced_by: str | None = Field(default=None, max_length=200)   # owner only
    review_at: float | str | None = None


class PreferenceAttrs(_Attrs):
    subject: str | None = Field(default=None, max_length=200)


class ContactAttrs(_Attrs):
    handles: dict[str, str] = Field(default_factory=dict, max_length=30)
    org: str | None = Field(default=None, max_length=200)
    role: str | None = Field(default=None, max_length=200)
    relation: str | None = Field(default=None, max_length=200)


class RunSummaryAttrs(_Attrs):
    run_id: str | None = Field(default=None, max_length=80)
    brain_task_id: str | None = Field(default=None, max_length=80)
    flow: str | None = Field(default=None, max_length=200)
    flow_version: str | int | None = None
    lane: str | None = Field(default=None, max_length=200)
    caller: str | None = Field(default=None, max_length=200)    # schedule id or principal
    status: Literal["succeeded-verified", "succeeded-unverified", "partial", "failed", "cancelled"]
    proof: list[dict[str, Any]] = Field(default_factory=list, max_length=20)
    content_digest: str | None = Field(default=None, max_length=100)
    approved_text: str | None = Field(default=None, max_length=3000)
    cost: dict[str, float] | None = None
    started_at: float | str | None = None
    ended_at: float | str | None = None


ATTRS_MODEL: dict[EntryType, type[_Attrs]] = {
    EntryType.FACT: FactAttrs, EntryType.DOCUMENT: DocumentAttrs, EntryType.DECISION: DecisionAttrs,
    EntryType.PREFERENCE: PreferenceAttrs, EntryType.CONTACT: ContactAttrs,
    EntryType.RUN_SUMMARY: RunSummaryAttrs,
}
OWNER_ONLY_ATTRS = {EntryType.DOCUMENT: ("pinned",), EntryType.DECISION: ("enforced_by",)}
_TIME_ATTRS = ("valid_from", "valid_to", "review_at", "started_at", "ended_at")


def validate_attrs(etype: EntryType, attrs: dict[str, Any] | None) -> dict[str, Any]:
    """The type's attributes, validated, with times as epoch seconds and defaults dropped."""
    try:
        model = ATTRS_MODEL[etype].model_validate(attrs or {})
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ())) or "attrs"
        raise InvalidRequest(f"attrs for a {etype.value}: {where}: {first.get('msg', 'invalid')}") from None
    out = model.model_dump(mode="json", exclude_defaults=True)
    for name in _TIME_ATTRS:
        if name in out:
            out[name] = parse_when(out[name], name=name)
    return out


# ---- limits (4.11) -----------------------------------------------------------------

class Limits(BaseModel):
    """Caps from `$AGENTOS_HOME/memory.toml` (every key optional). Sizes are characters,
    except `attrs_bytes` and `workspace_bytes`."""
    model_config = ConfigDict(extra="forbid")

    title_chars: int = 200
    body_chars: dict[str, int] = Field(default_factory=lambda: {
        "fact": 2000, "decision": 2000, "preference": 2000, "contact": 4096, "run_summary": 4096,
        "document": 65536})
    attrs_bytes: int = 4096
    evidence_per_version: int = 20
    links_per_version: int = 50
    live_entries: int = 5000
    workspace_bytes: int = 50 * 1024 * 1024
    writes_per_hour: int = 60
    writes_per_day: int = 500
    untrusted_writes_per_hour: int = 20
    context_budget_default: int = 1500
    context_budget_max: int = 8000
    context_budget_min: int = 200
    search_limit_default: int = 20
    search_limit_max: int = 100
    # retention (4.9); days, 0 means "until superseded"
    agent_fact_days: int = 365
    untrusted_fact_days: int = 30
    run_summary_days: int = 180
    purge_after_days: int = 90
    recent_runs_days: int = 7

    def body_limit(self, etype: EntryType) -> int:
        return self.body_chars.get(etype.value, 2000)


def limits_path(home: Path) -> Path:
    return home / "memory.toml"


def load_limits(home: Path) -> Limits:
    path = limits_path(home)
    if not path.exists():
        return Limits()
    try:
        data = tomllib.loads(path.read_text())
        defaults = Limits().body_chars
        if isinstance(data.get("body_chars"), dict):
            data["body_chars"] = {**defaults, **data["body_chars"]}
        return Limits(**data)
    except (tomllib.TOMLDecodeError, ValidationError, TypeError) as exc:
        raise InvalidRequest(f"{path} is not valid: {exc}") from None


# ---- who is calling ------------------------------------------------------------------

@dataclass(frozen=True)
class MemoryScope:
    """What a caller may do with memory: the `memory` grant special (4.7). Built by the tools
    layer from the grant (Brief B's MemoryCap); `everything()` for the owner and the node."""
    workspaces: frozenset[str] | None = frozenset()   # None: every workspace
    write: bool = False
    types: frozenset[EntryType] | None = None         # None: every type the caller may write
    private: bool = False
    audience_owner: bool = False
    lanes: tuple[str, ...] = ()                        # the grant's lane patterns, for decision scope

    @classmethod
    def everything(cls) -> MemoryScope:
        return cls(workspaces=None, write=True, types=None, private=True, audience_owner=True)

    @classmethod
    def from_cap(cls, cap: Any, *, lanes: tuple[str, ...] | list[str] = ()) -> MemoryScope:
        """From an object or dict with the 4.7 fields (`workspaces`, `write`, `types`, `private`,
        `audience_owner`). A missing cap gives an empty scope (no memory)."""
        if cap is None:
            return cls()

        def get(name: str, default: Any) -> Any:
            return cap.get(name, default) if isinstance(cap, dict) else getattr(cap, name, default)

        ws = get("workspaces", [])
        types = get("types", None)
        return cls(workspaces=None if ws is None else frozenset(ws), write=bool(get("write", False)),
                   types=None if types is None else frozenset(EntryType(t) for t in types),
                   private=bool(get("private", False)), audience_owner=bool(get("audience_owner", False)),
                   lanes=tuple(lanes))

    def allows(self, workspace: str) -> bool:
        return self.workspaces is None or workspace in self.workspaces

    def may_write(self, etype: EntryType) -> bool:
        return self.write and (self.types is None or etype in self.types)


@dataclass(frozen=True)
class Approval:
    """An approval the owner decided for this exact write (`OwnerTrustRequired.digest`)."""
    id: str
    digest: str | None = None


@dataclass(frozen=True)
class Caller:
    """Who is calling, as the node knows it: never from tool arguments.

    `kind`: "owner" (the owner in person: a signed-in panel session or the node CLI), "node"
    (runner and brains writing run summaries), or the principal kind of an MCP caller ("agent",
    "brain", "flow"). Only `owner` and `node` skip taint and rate limits. `cloud` is True for
    any reader whose model may be in the cloud (every attached agent; a node brain routed to a
    cloud model) and decides the privacy table in 4.10.
    """
    kind: str
    principal: str
    family: str | None = None          # policy family key ("family:<root grant>")
    scope: MemoryScope = field(default_factory=MemoryScope)
    thread: str | None = None
    grant_id: str | None = None
    grant_root: str | None = None
    grant_path: str | None = None
    session: str | None = None         # owner session id (confirmed_by), when in person
    lane: str | None = None
    run_id: str | None = None
    brain_task_id: str | None = None
    write_trace: str | None = None     # the trace id of this call (prov.write_trace)
    cloud: bool = True
    approval: Approval | None = None

    @property
    def in_person(self) -> bool:
        return self.kind == "owner"

    @property
    def is_node(self) -> bool:
        return self.kind == "node"

    @classmethod
    def owner(cls, *, session: str = "cli", principal: str = "owner", write_trace: str | None = None) -> Caller:
        return cls(kind="owner", principal=principal, scope=MemoryScope.everything(), session=session,
                   cloud=False, write_trace=write_trace)

    @classmethod
    def node(cls, *, principal: str = "node", run_id: str | None = None, brain_task_id: str | None = None,
             lane: str | None = None) -> Caller:
        return cls(kind="node", principal=principal, scope=MemoryScope.everything(), cloud=False,
                   run_id=run_id, brain_task_id=brain_task_id, lane=lane)

    @classmethod
    def from_context(cls, ctx: Any, scope: MemoryScope, *, cloud: bool = True) -> Caller:
        """From a policy ToolContext (agentos.policy.PolicyContext or a fake with the same
        attributes). The owner in person (`ctx.in_person`) is the owner; everyone else, the
        owner's own token included, is an agent (open question 1)."""
        principal = ctx.principal
        grant = ctx.grant
        meta = getattr(ctx, "meta", None)
        if getattr(ctx, "in_person", False):
            return replace(cls.owner(session=getattr(principal, "thread", None) or principal.id,
                                     principal=principal.id, write_trace=getattr(ctx, "start_id", None)),
                           lane=getattr(ctx, "lane_id", None))
        return cls(kind=getattr(principal, "kind", "agent") or "agent", principal=principal.id,
                   family=getattr(ctx, "taint_key", None), scope=scope, thread=getattr(principal, "thread", None),
                   grant_id=getattr(grant, "id", None), grant_root=getattr(grant, "root_id", None),
                   grant_path=getattr(grant, "path", None), lane=getattr(ctx, "lane_id", None),
                   run_id=getattr(meta, "run_id", None), write_trace=getattr(ctx, "start_id", None), cloud=cloud)

    def with_approval(self, approval_id: str, digest: str | None = None) -> Caller:
        return replace(self, approval=Approval(approval_id, digest))

    def can_read_owner_audience(self) -> bool:
        return self.in_person or self.scope.audience_owner

    def can_read_private(self) -> bool:
        return self.in_person or self.is_node or self.scope.private


# ---- requests --------------------------------------------------------------------------

class WriteRequest(BaseModel):
    """What a writer sends to `write` (the memory_write arguments, minus `workspace`)."""
    model_config = ConfigDict(extra="forbid")

    type: EntryType
    title: str
    body: str = ""
    key: str | None = None
    attrs: dict[str, Any] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
    supersedes: str | None = None          # "mem_...@N": compare-and-set
    relates: list[str] = Field(default_factory=list)
    derived_from: list[str] = Field(default_factory=list)
    contests: list[str] = Field(default_factory=list)
    audience: Audience = Audience.WORKSPACE

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("title cannot be empty")
        return v

    @field_validator("key")
    @classmethod
    def _key(cls, v: str | None) -> str | None:
        if v is not None and not KEY_RE.match(v):
            raise ValueError("key must be a lowercase slug (letters, digits, . _ : / -), at most 120 characters")
        return v

    @field_validator("supersedes")
    @classmethod
    def _supersedes(cls, v: str | None) -> str | None:
        if v is not None and not REF_RE.match(v):
            raise ValueError("supersedes must be mem_<id>@<version>")
        return v

    @field_validator("relates", "derived_from", "contests")
    @classmethod
    def _ids(cls, v: list[str]) -> list[str]:
        for item in v:
            if not ENTRY_ID_RE.match(item):
                raise ValueError(f"{item[:40]!r} is not an entry id")
        return list(dict.fromkeys(v))

    @field_validator("evidence")
    @classmethod
    def _evidence(cls, v: list[str]) -> list[str]:
        for item in v:
            if not TRACE_ID_RE.match(item):
                raise ValueError(f"{item[:40]!r} is not a trace id")
        return list(dict.fromkeys(v))

    @classmethod
    def parse(cls, data: dict[str, Any] | WriteRequest) -> WriteRequest:
        if isinstance(data, WriteRequest):
            return data
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first.get("loc", ())) or "request"
            raise InvalidRequest(f"{where}: {first.get('msg', 'invalid')}") from None


def parse_ref(ref: str) -> tuple[str, int]:
    m = REF_RE.match(ref or "")
    if not m:
        raise InvalidRequest("a version reference is mem_<id>@<version>")
    return m.group(1), int(m.group(2))


def check_workspace(workspace: str) -> str:
    if not isinstance(workspace, str) or not WORKSPACE_RE.match(workspace):
        raise InvalidRequest("workspace must be lowercase letters, digits and dashes (at most 31)")
    return workspace


def check_entry_id(entry_id: str) -> str:
    if not isinstance(entry_id, str) or not ENTRY_ID_RE.match(entry_id):
        raise InvalidRequest("an entry id looks like mem_<letters and digits>")
    return entry_id
