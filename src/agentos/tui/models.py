"""Lenient views of the panel API objects (docs/API.md).

The dashboard must keep working while the API grows, so every field has a default,
unknown fields are kept, unknown enum values pass through as plain strings, and a
malformed item is skipped instead of failing the whole list.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, model_validator


def lenient_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, int | float):
        dt = datetime.fromtimestamp(value, UTC)
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _who(value: Any) -> str | None:
    """A principal may arrive as a string or as an object; the dashboard shows its id."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("id") or value.get("kind")
    return str(value)


Dt = Annotated[datetime | None, BeforeValidator(lenient_dt)]
Who = Annotated[str | None, BeforeValidator(_who)]


class _M(BaseModel):
    model_config = ConfigDict(extra="allow")


class Cost(_M):
    tokens: int | None = None
    usd: float | None = None


class Control(_M):
    state: str = "agent"
    by: str | None = None
    since: Dt = None
    takeback_requested: bool = False
    takeback_reason: str | None = None
    takeback_by: str | None = None
    last_human_input: Dt = None


class Proxy(_M):
    label: str | None = None
    host: str | None = None
    port: int | None = None


class Lane(_M):
    id: str
    workspace: str = ""
    name: str = ""
    kind: str = "browser"
    engine: str | None = None
    preset: str | None = None
    state: str = "cold"
    signal: str = "unknown"
    proxy: Proxy | None = None
    exit_ip: str | None = None
    last_used: Dt = None
    node: str | None = None
    quarantine_reason: str | None = None
    logged_in_hint: str | None = None
    control: Control = Field(default_factory=Control)
    viewer: dict | None = None

    @model_validator(mode="after")
    def _split_id(self) -> Lane:
        if "/" in self.id:
            ws, name = self.id.split("/", 1)
            self.workspace = self.workspace or ws
            self.name = self.name or name
        return self


class Workspace(_M):
    id: str
    name: str | None = None
    color: str | None = None
    health: str | None = None
    summary: str | None = None
    lanes: list[Lane] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _skip_bad_lanes(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("lanes"), list):
            data = {**data, "lanes": parse_list(Lane, data["lanes"])}
        return data


class Mem(_M):
    used_mb: float | None = None
    total_mb: float | None = None


class Node(_M):
    id: str
    state: str = "up"
    degraded: bool = False
    capabilities: list[str] = Field(default_factory=list)
    cpu: float | None = None
    mem: Mem | None = None
    warm_lanes: int | None = None
    max_warm_lanes: int | None = None
    last_seen: Dt = None
    timezone: str | None = None


class BrainChip(_M):
    name: str | None = None
    state: str | None = None
    offline_ready: bool = False


class Status(_M):
    node: str | None = None
    version: str | None = None
    needs_you: int = 0
    cost_today: Cost | None = None
    brain: BrainChip | None = None
    nodes: list[dict] = Field(default_factory=list)
    timezone: str | None = None


class Requester(_M):
    kind: str | None = None
    id: str | None = None
    thread: str | None = None


class Preview(_M):
    text: str | None = None
    image: str | None = None


class Approval(_M):
    id: str
    kind: str | None = None          # "question" for an agent's question (listed under questions)
    state: str = "pending"
    effect: str = "commit"
    lane: str | None = None
    summary: str = ""
    content_hash: str | None = None
    preview: Preview | None = None
    requested_by: Requester | None = None
    created_at: Dt = None
    expires_at: Dt = None


class Question(_M):
    id: str
    state: str = "open"
    lane: str | None = None
    question: str = ""
    options: list[str] = Field(default_factory=list)
    asked_by: Requester | None = None
    created_at: Dt = None
    expires_at: Dt = None

    @model_validator(mode="before")
    @classmethod
    def _text_alias(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("question") and data.get("text"):
            data = {**data, "question": data["text"]}
        return data


class TraceEvent(_M):
    id: str
    at: Dt = None
    kind: str | None = None
    tool: str | None = None
    lane: str | None = None
    principal: Who = None
    effect: str | None = None
    status: str | None = None
    summary: str = ""
    cost: Cost | None = None
    run_id: str | None = None

    @property
    def workspace(self) -> str | None:
        return self.lane.split("/", 1)[0] if self.lane and "/" in self.lane else None


class Proof(_M):
    url: str | None = None
    id: str | None = None
    screenshot: str | None = None


class Run(_M):
    id: str
    flow: str | None = None
    lane: str | None = None
    status: str = "queued"
    started_at: Dt = None
    finished_at: Dt = None
    proof: Proof | None = None
    cost: Cost | None = None
    error: str | None = None
    trigger: str | None = None
    steps: list[dict] = Field(default_factory=list)


class Flow(_M):
    id: str
    lane: str | None = None
    version: int | str | None = None
    params: dict = Field(default_factory=dict)
    effect: str | None = None
    last_run: dict | None = None


class Schedule(_M):
    id: str
    flow: str = ""
    cron: str | None = None
    watch: Any = None
    params: dict = Field(default_factory=dict)
    enabled: bool = True
    next_fire_at: Dt = None
    lane: str | None = None
    deadman_minutes: int | None = None
    last_run: dict | None = None

    @model_validator(mode="before")
    @classmethod
    def _next_alias(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("next_fire_at"):
            for alias in ("next_fire", "next_run_at", "next_at"):
                if data.get(alias):
                    data = {**data, "next_fire_at": data[alias]}
                    break
        return data


class Seats(_M):
    in_use: int = 0
    total: int = 0


class Brain(_M):
    name: str
    kind: str | None = None
    provider: str | None = None
    state: str = "ready"
    seats: Seats | None = None
    window_used_pct: float | None = None


class BrainTask(_M):
    id: str
    brain: str | None = None
    lane: str | None = None
    status: str = "running"
    started_at: Dt = None
    finished_at: Dt = None
    summary: str = ""
    tail: list[str] | str | None = None

    @property
    def tail_lines(self) -> list[str]:
        if self.tail is None:
            return []
        if isinstance(self.tail, str):
            return self.tail.splitlines()
        return [str(line) for line in self.tail]


def parse_list(model: type[_M], items: Any) -> list:
    """Parse a list of API objects, skipping the malformed ones."""
    if isinstance(items, dict):
        # tolerate `{ "items": [...] }` style envelopes
        for key in ("items", "data", "results"):
            if isinstance(items.get(key), list):
                items = items[key]
                break
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if isinstance(item, model):
            out.append(item)
            continue
        try:
            out.append(model.model_validate(item))
        except ValidationError:
            continue
    return out


def parse_one(model: type[_M], item: Any):
    if isinstance(item, model):
        return item
    try:
        return model.model_validate(item)
    except ValidationError:
        return None
