"""Brain data: what a brain is, what a task asks for, and what a task did.

Pure pydantic models; no I/O. The router, the store, the tools and the panel API all
speak these shapes.
"""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

from agentos.core.models import utcnow

BRAIN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
WORKSPACE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
TASK_ID_RE = re.compile(r"^bt_[a-f0-9]{12}$")


class BrainKind(StrEnum):
    SUBSCRIPTION = "subscription"  # flat-rate seat the owner already pays for (BYO, never pooled)
    LOCAL = "local"                # a model on the owner's own hardware (Ollama or OpenAI-compatible)
    METERED = "metered"            # pay-per-token API key (the fallback)


class BrainState(StrEnum):
    READY = "ready"
    COOLING_DOWN = "cooling_down"  # hit a rate or usage limit; back after a backoff
    NEEDS_AUTH = "needs_auth"      # credential missing, expired or rejected: the owner must sign in
    DOWN = "down"                  # CLI missing, CLI drift, local server unreachable, disabled


class Privacy(StrEnum):
    """Where a workspace's prompts may go. Set by the owner, never by an agent."""
    LOCAL_ONLY = "local-only"          # never touches a cloud model
    REDACTED_CLOUD = "redacted-cloud"  # cloud allowed after the redactor runs
    FULL_CLOUD = "full-cloud"


class Runner(StrEnum):
    CLAUDE = "claude"  # `claude -p` (Claude Code)
    CODEX = "codex"    # `codex exec` (OpenAI Codex)
    CHAT = "chat"      # agentos.brains.chatcli: one call to an OpenAI-compatible chat-completions endpoint
    SYSTEMONE = "systemone"  # agentos.brains.decisions: typed questions in process, never a task


class Locality(StrEnum):
    LOCAL = "local"
    CLOUD = "cloud"


class TaskStatus(StrEnum):
    """Honest task outcomes. A brain finishing is never proof the work is right, so success
    is always `succeeded_unverified` here (proof of done belongs to the runner's readback)."""
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED_UNVERIFIED = "succeeded_unverified"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"  # the node restarted under it; resumable with transcript context

    @property
    def active(self) -> bool:
        return self in (TaskStatus.STARTING, TaskStatus.RUNNING)

    @property
    def terminal(self) -> bool:
        return self in (TaskStatus.SUCCEEDED_UNVERIFIED, TaskStatus.FAILED, TaskStatus.CANCELLED)


class Outcome(StrEnum):
    """How one attempt (one CLI process on one brain) ended."""
    SUCCEEDED = "succeeded"
    FAILED = "failed"            # the agent reported an error result
    RATE_LIMITED = "rate_limited"  # 429 or a subscription usage limit
    AUTH_FAILED = "auth_failed"  # missing, expired or rejected credential
    SERVER_ERROR = "server_error"  # 5xx or overloaded after the CLI's own retries
    CANCELLED = "cancelled"
    LIMIT_TURNS = "limit_turns"
    LIMIT_WALL = "limit_wall"
    LIMIT_TOKENS = "limit_tokens"
    LIMIT_COST = "limit_cost"
    LIMIT_OUTPUT = "limit_output"
    STALLED = "stalled"          # no output for too long (a hang)
    CRASHED = "crashed"          # exited without a result
    SPAWN_FAILED = "spawn_failed"
    INTERRUPTED = "interrupted"

    @property
    def retry_elsewhere(self) -> bool:
        """Errors the fallback chain answers by trying the next brain."""
        return self in (Outcome.RATE_LIMITED, Outcome.AUTH_FAILED, Outcome.SERVER_ERROR,
                        Outcome.CRASHED, Outcome.STALLED, Outcome.SPAWN_FAILED)


KIND_RANK = {BrainKind.SUBSCRIPTION: 0, BrainKind.LOCAL: 1, BrainKind.METERED: 2}

# Where each typed-decision provider lives. A template copies these into the spec, so `--base-url`,
# `--model` and the price flags override them for one brain, and a correction here reaches new brains.
DECISION_PROVIDERS: dict[str, dict[str, Any]] = {
    # verified live against the API
    "typesafe": {"base_url": "https://api.typesafe.ai/v1", "model": "jev-latest",
                 "price_in_per_mtok": 0.042, "price_out_per_mtok": 0.0},
    # NOT verified: from a community repo's source. No price default, the owner reads it off OpenRouter.
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "model": "~typesafe/jev-latest"},
}


class Capabilities(BaseModel):
    context_tokens: int = 200_000
    tools: bool = True
    vision: bool = False
    tier: int = 2  # 1 small, 2 standard, 3 frontier; a grant's model tier caps this
    decisions: bool = False  # answers typed questions in process (BrainService.decide) and runs no task


def _is_private_host(host: str) -> bool:
    """True when `host` is on the owner's own machine or network (loopback, RFC 1918,
    link-local, CGNAT/mesh 100.64/10, IPv6 ULA, `localhost`, `*.local`)."""
    host = (host or "").strip("[]").lower()
    if host in ("localhost",) or host.endswith((".local", ".internal")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return True
    return isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10")


class BrainSpec(BaseModel):
    """One registered brain. Credentials are never stored here, only in the vault."""
    name: str
    kind: BrainKind
    provider: str                      # anthropic | openai | ollama | openai-compatible | typesafe | openrouter
    runner: Runner
    model: str | None = None
    effort: str | None = None          # claude: how hard it thinks (--effort)
    base_url: str | None = None        # local brains: the model server (on the owner's network); chat: the endpoint
    wire_api: str = "responses"        # codex custom providers; codex 0.166 dropped "chat"
    seats: int = 1                     # concurrent tasks this brain may run (subscription: keep low)
    window_hours: float = 5.0          # provider's rolling usage window (estimate)
    window_tokens: int | None = None   # owner-set window capacity estimate; learned from 429s otherwise
    caps: Capabilities = Field(default_factory=Capabilities)
    order: int = 100                   # owner's order within a kind (lower first)
    enabled: bool = True
    allowed_tools: list[str] | None = None   # claude: overrides the default allowlist
    permission_mode: str | None = None       # claude: overrides settings.permission_mode
    price_in_per_mtok: float | None = None   # metered: USD per million input tokens (estimate)
    price_out_per_mtok: float | None = None
    credential: str | None = None      # oauth_token | codex_auth | api_key | None (no credential)
    created_at: datetime = Field(default_factory=utcnow)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if not BRAIN_NAME_RE.match(v):
            raise ValueError("brain name must be lowercase letters, digits and dashes")
        return v

    @field_validator("seats")
    @classmethod
    def _valid_seats(cls, v: int) -> int:
        if v < 1 or v > 16:
            raise ValueError("seats must be between 1 and 16")
        return v

    @model_validator(mode="after")
    def _check_kind(self) -> BrainSpec:
        if self.kind == BrainKind.LOCAL:
            if self.provider != "ollama" and not self.base_url:
                raise ValueError("a local brain needs --base-url (its model server)")
            if self.base_url:
                host = urlparse(self.base_url).hostname or ""
                if not _is_private_host(host):
                    raise ValueError(
                        f"{host or self.base_url} is not on your own machine or network, so it is "
                        "not a local brain. Add it as a metered brain instead.")
            if self.runner != Runner.CODEX:
                raise ValueError("local brains run through codex exec")
        if self.runner == Runner.CHAT:
            self._check_chat()
        if self.runner == Runner.SYSTEMONE or self.caps.decisions:
            self._check_decisions()
        return self

    def _check_chat(self) -> None:
        """What a chat brain needs before it can spend the owner's money. The messages never quote
        the URL: an address with a password in it must not reach a log."""
        if self.kind != BrainKind.METERED:
            raise ValueError("a chat-completions brain is metered: use --kind metered")
        url = urlparse(self.base_url or "")
        if url.scheme not in ("http", "https") or not url.hostname:
            raise ValueError("a chat-completions brain needs --base-url (http or https, up to and including /v1)")
        self._check_url_holds_no_secret()
        if url.scheme != "https" and not _is_private_host(url.hostname):
            raise ValueError("--base-url must be https: plain http would send the API key and every prompt "
                             "across the internet in the clear")
        if not self.model:
            raise ValueError("a chat-completions brain needs --model (the endpoint has no default we can know)")
        if self.price_in_per_mtok is None or self.price_out_per_mtok is None:
            raise ValueError("a chat-completions brain needs --price-in and --price-out: the endpoint reports "
                             "tokens, not dollars, so without prices the budget gate counts nothing")
        self._check_prices_not_negative()
        if self.caps.tools or self.caps.vision:
            raise ValueError("a chat-completions brain sends one text prompt, so it has no tools and no vision")

    def _check_decisions(self) -> None:
        """A decision brain is a cloud brain that spends the owner's money on every call, in process
        and with no task around it, so its rules are the chat brain's and stricter: https even at a
        private address. The messages never quote the URL."""
        if self.runner != Runner.SYSTEMONE or not self.caps.decisions:
            raise ValueError("a decision brain needs the systemone runner and the decisions capability, together")
        if self.kind != BrainKind.METERED:
            raise ValueError("a decision brain is metered: use --kind metered")
        url = urlparse(self.base_url or "")
        if url.scheme != "https" or not url.hostname:
            raise ValueError("a decision brain's --base-url must be https, up to and including /v1: anything "
                             "else would send the API key and every state in the clear")
        self._check_url_holds_no_secret()
        if not self.model:
            raise ValueError("a decision brain needs --model")
        if self.credential != "api_key":
            raise ValueError("a decision brain signs in with an API key")
        if self.price_in_per_mtok is None or self.price_out_per_mtok is None:
            raise ValueError(f"--provider {self.provider} needs --price-in and --price-out: the endpoint reports "
                             "tokens, not dollars, so without prices the budget gate counts nothing")
        self._check_prices_not_negative()
        if self.caps.tools or self.caps.vision:
            raise ValueError("a decision brain answers typed questions, so it has no tools and no vision")

    def _check_url_holds_no_secret(self) -> None:
        url = urlparse(self.base_url or "")
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("--base-url may not hold a user name, password or query: "
                             "the API key is stored with `agentos brains auth`")

    def _check_prices_not_negative(self) -> None:
        if (self.price_in_per_mtok or 0) < 0 or (self.price_out_per_mtok or 0) < 0:
            raise ValueError("--price-in and --price-out cannot be negative")

    @property
    def locality(self) -> Locality:
        return Locality.LOCAL if self.kind == BrainKind.LOCAL else Locality.CLOUD

    def public(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def template(cls, name: str, kind: BrainKind | str, provider: str, **overrides: Any) -> BrainSpec:
        """Sensible defaults per kind and provider; overrides win."""
        kind = BrainKind(kind)
        base: dict[str, Any] = {"name": name, "kind": kind, "provider": provider}
        if kind == BrainKind.LOCAL:
            base.update(runner=Runner.CODEX, credential=None, window_hours=0,
                        caps=Capabilities(context_tokens=32_768, tools=True, vision=False, tier=1))
        elif provider == "anthropic":
            base.update(runner=Runner.CLAUDE,
                        credential="oauth_token" if kind == BrainKind.SUBSCRIPTION else "api_key",
                        caps=Capabilities(context_tokens=200_000, tools=True, vision=True, tier=3))
        elif provider == "openai":
            base.update(runner=Runner.CODEX,
                        credential="codex_auth" if kind == BrainKind.SUBSCRIPTION else "api_key",
                        caps=Capabilities(context_tokens=200_000, tools=True, vision=True, tier=3))
        elif provider == "openai-compatible" and kind == BrainKind.METERED:
            base.update(runner=Runner.CHAT, credential="api_key", window_hours=0,
                        caps=Capabilities(context_tokens=32_768, tools=False, vision=False, tier=1))
        elif provider in DECISION_PROVIDERS and kind == BrainKind.METERED:
            base.update(runner=Runner.SYSTEMONE, credential="api_key", window_hours=0,
                        caps=Capabilities(context_tokens=64_000, tools=False, vision=False, tier=1, decisions=True),
                        **DECISION_PROVIDERS[provider])
        else:
            raise ValueError(f"unknown provider {provider!r} for a {kind} brain")
        if kind == BrainKind.METERED:
            base["seats"] = 2
        if isinstance(overrides.get("caps"), dict):
            overrides["caps"] = Capabilities(**{**base["caps"].model_dump(), **overrides["caps"]})
        base.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**base)


class Budget(BaseModel):
    """Per-task limits. Unset fields take the node defaults (brains settings)."""
    max_usd: float | None = None      # real money; only metered brains spend it
    max_tokens: int | None = None     # input + output, all attempts together
    max_turns: int | None = None
    max_minutes: float | None = None  # wall clock per attempt
    max_queue_minutes: float | None = None


class Requirements(BaseModel):
    min_context: int = 0
    tools: bool = True
    vision: bool = False
    min_tier: int = 0
    decisions: bool = False  # True only from BrainService.decide: the two kinds never route to each other
    no_tools: bool = False   # must run with no tools at all, for a prompt that holds a stranger's text


class InlineFile(BaseModel):
    path: str
    content: str


class GitRef(BaseModel):
    remote: str
    ref: str = "HEAD"
    depth: int | None = 50
    # the commit `ref` was at when a person co-signed sending it (agentos pins it then): the clone
    # must be at exactly this commit, or nothing is handed to the brain
    commit: str | None = None


class ContextBundle(BaseModel):
    """What a laptop session hands to the node so the brain can pick up the work."""
    summary: str | None = None             # what is done, what is next, decisions made
    files: list[str | InlineFile] = Field(default_factory=list)  # paths to look at, or inline files
    archive_b64: str | None = None         # a .tar.gz of the working tree (size capped)
    git: GitRef | None = None              # preferred: a pushed branch the node clones
    taint: list[str] = Field(default_factory=list)  # "private", "untrusted": can only tighten routing


class GrantView(BaseModel):
    """What the caller's grant allows for brains (from the policy service, or node defaults)."""
    allow_metered: bool = False
    remaining_usd: float | None = None     # None = no grant-level dollar cap
    remaining_tokens: int | None = None
    max_tier: int | None = None
    brains: list[str] | None = None        # allowlist of brain names; None = any
    allow_private_cloud: bool = False      # set only after a human co-signed
    # the brains a co-sign named (brain_identity): a private-data or metered co-sign covers only them,
    # so a retry on another brain, or the same name signed in to another account, asks again
    private_cloud_brains: list[str] | None = None
    metered_brains: list[str] | None = None


class BrainView(BaseModel):
    """A brain as the router sees it right now."""
    name: str
    kind: BrainKind
    locality: Locality
    state: BrainState
    state_reason: str | None = None
    caps: Capabilities
    order: int = 100
    seats_total: int = 1
    seats_in_use: int = 0
    window_saturated: bool = False
    enabled: bool = True
    identity: str | None = None  # brain_identity: who it is, for what a co-sign covers

    @property
    def busy(self) -> bool:
        return self.seats_in_use >= self.seats_total or self.window_saturated


class Candidate(BaseModel):
    name: str
    kind: BrainKind
    reason: str
    wait: bool = False      # eligible but its seats are taken or its window is spent
    redact: bool = False    # redacted-cloud: prompt and bundle go through the redactor


class Rejection(BaseModel):
    name: str
    reason: str
    approvable: str | None = None  # "metered" | "private_cloud" when a human co-sign could allow it


class RouteDecision(BaseModel):
    action: str             # "run" | "wait" | "queue" | "needs_approval"
    reason: str
    candidates: list[Candidate] = Field(default_factory=list)
    rejected: list[Rejection] = Field(default_factory=list)
    approval: dict[str, Any] | None = None  # {"kind": "metered"|"private_cloud", "brains": [...]}

    def summary(self) -> str:
        chain = " > ".join(c.name for c in self.candidates) or "none"
        return f"{self.action}: {self.reason} (chain: {chain})"


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_write_tokens


class TaskRecord(BaseModel):
    id: str
    workspace: str
    principal_id: str
    owner: str = "owner"
    thread: str | None = None
    # the path of the grant that started it ("/root/child/"): it, or a grant it was narrowed from,
    # steers the task; a subject id is not unique, and a narrower child of it is not its starter
    grant_path: str | None = None
    status: TaskStatus = TaskStatus.QUEUED
    status_reason: str | None = None
    brain: str | None = None
    pinned_brain: str | None = None
    attempt: int = 0
    privacy: Privacy = Privacy.FULL_CLOUD
    taint: list[str] = Field(default_factory=list)
    lanes: list[str] = Field(default_factory=list)
    budget: Budget = Field(default_factory=Budget)
    requires: Requirements = Field(default_factory=Requirements)
    grant: GrantView = Field(default_factory=GrantView)
    prompt_preview: str = ""
    turns: int = 0
    tokens: int = 0
    cost_usd: float = 0.0           # real spend (metered only)
    notional_usd: float = 0.0       # what a subscription run would have cost at API prices
    session_id: str | None = None
    session_brain: str | None = None   # the brain whose CLI owns session_id (for --resume)
    result_preview: str | None = None
    resume_of: str | None = None
    resume_from_attempt: int = 0       # attempts before this do not exclude brains any more
    prepared: bool = False             # context bundle materialized into the work dir
    handoff: str | None = None         # handoff text appended to the prompt
    child_grant_id: str | None = None  # revoked when the task ends
    spend_grant_id: str | None = None  # what its spend is charged to: the child grant, else the caller's
    notified: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    queued_since: datetime | None = None

    def public(self) -> dict[str, Any]:
        """What a tool or the panel may return: no prompt body, no credentials."""
        return self.model_dump(mode="json", exclude={"grant", "handoff", "child_grant_id", "spend_grant_id",
                                                     "notified", "prepared"})


def brain_facts(spec: BrainSpec, signin: str | None) -> dict[str, Any]:
    """What a co-sign names of a brain: the provider, the runner, the model and the subscription or
    key it is signed in with (`signin` changes with each new sign-in, never with a token refresh)."""
    return {"name": spec.name, "kind": spec.kind.value, "provider": spec.provider, "runner": spec.runner.value,
            "model": spec.model, "base_url": spec.base_url, "credential": spec.credential, "signin": signin,
            "added": spec.created_at.isoformat()}


def brain_identity(spec: BrainSpec, signin: str | None) -> str:
    """`<name>:<digest of brain_facts>`: the same name with another model, provider or sign-in is
    another brain to a co-sign."""
    import hashlib
    import json

    facts = json.dumps(brain_facts(spec, signin), sort_keys=True)
    return f"{spec.name}:{hashlib.sha256(facts.encode()).hexdigest()[:16]}"
