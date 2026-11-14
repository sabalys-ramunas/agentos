"""The routing table: which calls run on their own and which wait for a human.

Data, not code. Rules are checked top to bottom; the first rule whose effect matches and
whose `when` facts all hold decides `auto`, `approval` or `deny`. An effect with no
matching rule needs approval (fail closed), and pay and irreversible never come out auto. A commit
runs on its own unless "Ask me first" is on for its routine or account (ask_first.py). The table,
the facts and the overlays that run after it (standing approvals, the lethal trifecta, the untrusted-content downgrade) are documented
in ROUTING.md, and an operator can replace the rules in `$AGENTOS_HOME/policy.toml` under `[routing]`
and the downgrade under `[taint]`.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentos.core.models import Effect
from agentos.policy.approvals import ApprovalPolicy
from agentos.policy.confirm import ALWAYS_ASK
from agentos.policy.confirmation import ConfirmationConfig
from agentos.policy.meters import MeterPolicy

log = logging.getLogger("agentos.policy.routing")

FACTS = {
    "first_use": "first use of this effect on this lane by this grant family in the period",
    "isolated": "the lane is isolated (preset account, sandbox, service or locked)",
    "source_allowlisted": "the install source is pinned and on an allowlist",
    "ttl_within_grant": "the service TTL fits the grant's max_ttl_s and expiry",
    "metered": "the brain call spends metered money (an API key, not a flat subscription)",
    "over_quota": "the brain call would cross the quota threshold for the day",
    # workspace memory (memory_rules.py)
    "memory_owner_trust": "a memory write that would carry owner trust: a decision, or a supersede of an owner entry",
    "internal": "the destination is the workspace's own memory on this node, not a site",
    # Ask me first (ask_first.py): computed by the node, never taken from a handler's facts
    "runs_on_own": "the call acts on one account and Ask me first is off for its routine and that account",
    "ask_first": "Ask me first is on for the call's routine or its account",
    # agent environments (agentos.environments): computed by the node from its own records, never from a handler
    "environment": ("the lane is the terminal or the browser of the caller's own agent environment: a VM and a "
                    "browser that hold no account and no key, only a gateway token for its own repository"),
}
ASK_FIRST_DEFAULT = False  # routines and accounts run on their own unless someone turned "Ask me first" on


class Rule(BaseModel):
    # a misspelled key is refused, never dropped: `whn = {isolated = true}` read as no condition
    # at all would turn a conditional auto rule into an unconditional one
    model_config = ConfigDict(extra="forbid")
    effect: str
    when: dict[str, bool] = Field(default_factory=dict)
    then: Literal["auto", "approval", "deny"]
    note: str = ""

    @field_validator("effect")
    @classmethod
    def _effect(cls, v: str) -> str:
        if v != "*":
            Effect(v)
        return v

    @field_validator("when")
    @classmethod
    def _when(cls, v: dict[str, bool]) -> dict[str, bool]:
        unknown = set(v) - set(FACTS)
        if unknown:
            raise ValueError(f"unknown routing facts: {sorted(unknown)}")
        return v


DEFAULT_RULES = [
    # the caller's own environment: its VM and its browser hold nothing to protect, so nothing there waits
    # for a person, also after untrusted text (PolicyService._inside_environment holds this whatever a
    # replaced table says; pay and irreversible still ask, through RoutingTable.decide)
    Rule(effect="*", when={"environment": True}, then="auto", note="an agent's own environment"),
    Rule(effect="read", then="auto", note="reading is free"),
    Rule(effect="draft", when={"memory_owner_trust": True}, then="approval",
         note="owner-trust memory (the owner sees the exact text; never standing)"),
    Rule(effect="draft", then="auto", note="private writes"),
    Rule(effect="commit", when={"runs_on_own": True}, then="auto",
         note="runs on its own (Ask me first is off)"),
    Rule(effect="commit", when={"ask_first": True}, then="approval", note="Ask me first is on"),
    Rule(effect="commit", then="approval", note="it does not act on one account, so a person decides"),
    Rule(effect="pay", then="approval", note="always a human co-sign"),
    Rule(effect="irreversible", then="approval", note="always a human co-sign"),
    # storing a password is the owner's permission to use it: a login never asks (docs/PRINCIPLES.md, 3).
    # What guards it is the vault's own check of the lane, domain, frames and field (agentos.vault.policy)
    Rule(effect="secret", then="auto", note="the password in the vault is the owner's permission"),
    Rule(effect="exec", when={"isolated": False}, then="approval", note="workbench or shared lane"),
    Rule(effect="exec", when={"first_use": True}, then="approval", note="first use in this lane"),
    Rule(effect="exec", then="auto", note="isolated lane, seen before"),
    Rule(effect="install_overlay", when={"source_allowlisted": True}, then="auto", note="pinned, allowlisted"),
    Rule(effect="install_overlay", then="approval", note="unpinned or not allowlisted"),
    Rule(effect="install_base", then="approval", note="touches every lane on the image"),
    Rule(effect="service", when={"ttl_within_grant": False}, then="approval", note="exceeds the grant TTL"),
    Rule(effect="service", then="auto", note="within the grant"),
    Rule(effect="brain", when={"metered": True}, then="approval", note="metered spend is treated like pay"),
    Rule(effect="brain", when={"over_quota": True}, then="approval", note="past the quota threshold"),
    Rule(effect="brain", then="auto", note="within quota"),
    Rule(effect="vm", then="approval", note="rents a machine that bills by the hour"),
]


class RoutingTable(BaseModel):
    rules: list[Rule] = Field(default_factory=lambda: list(DEFAULT_RULES))
    # how long a first use lasts: "day" (UTC day) or "ever"
    first_use_period: dict[str, Literal["day", "ever"]] = Field(
        default_factory=lambda: {"exec": "ever"})
    # effects a standing approval may cover (pay, irreversible, install_base never)
    standing_effects: list[str] = Field(
        default_factory=lambda: ["commit", "secret", "exec", "service", "brain", "install_overlay", "vm"])
    # effects whose content is published: a standing approval for them names a flow version or a
    # tool and the contents the owner saw (AUD-10)
    content_effects: list[str] = Field(default_factory=lambda: ["commit"])
    # effects that send data out, for the lethal-trifecta check
    sink_effects: list[str] = Field(
        default_factory=lambda: ["draft", "commit", "pay", "irreversible", "exec", "service", "brain", "vm"])
    # fraction of a brain quota after which approval is needed
    brain_quota_threshold: float = Field(default=1.0, gt=0)
    # "Ask me first" for a routine or account nobody set (ask_first.py): off, so they run on their own.
    # A cautious owner sets it true in policy.toml: then everything asks until a person turns it off.
    ask_first_default: bool = Field(default_factory=lambda: ASK_FIRST_DEFAULT)

    @model_validator(mode="after")
    def _never_standing(self) -> RoutingTable:
        banned = {"pay", "irreversible", "install_base"} & set(self.standing_effects)
        if banned:
            raise ValueError(f"standing approvals can never cover {sorted(banned)}")
        for e in [*self.standing_effects, *self.sink_effects, *self.first_use_period, *self.content_effects]:
            Effect(e)
        return self

    def decide(self, effect: Effect, facts: dict[str, bool]) -> Rule:
        rule = self._first(effect, facts)
        if effect.value in ALWAYS_ASK and rule.then == "auto":
            # payments and what cannot be undone always ask, whatever a replaced table says
            return Rule(effect=effect.value, then="approval", note="always a human co-sign")
        return rule

    def _first(self, effect: Effect, facts: dict[str, bool]) -> Rule:
        for rule in self.rules:
            if rule.effect not in ("*", effect.value):
                continue
            if all(facts.get(k, False) == v for k, v in rule.when.items()):
                return rule
        return Rule(effect=effect.value, then="approval", note="no rule matched; fail closed")

    def render(self) -> str:
        lines = ["effect           when                      then      note"]
        for r in self.rules:
            when = ", ".join(f"{k}={'yes' if v else 'no'}" for k, v in r.when.items()) or "always"
            lines.append(f"{r.effect:<16} {when:<25} {r.then:<9} {r.note}")
        lines.append("")
        lines.append("standing approvals may cover: " + ", ".join(self.standing_effects))
        lines.append("first use periods: " + ", ".join(f"{k}={v}" for k, v in self.first_use_period.items()))
        lines.append("trifecta sinks: " + ", ".join(self.sink_effects))
        lines.append("internal destinations (facts.internal, no domain): inside the egress allowlist")
        lines.append("memory_owner_trust: always a one-off approval, never auto or standing")
        lines.append("pay, irreversible: always a person, whatever a rule says")
        lines.append("Ask me first: per routine and per account, "
                     + ("on" if self.ask_first_default else "off") + " unless set (agentos flows ask-first)")
        return "\n".join(lines)


DEFAULT_TAINT_KINDS = {
    "untrusted_content": ["untrusted"],
    "web_content": ["untrusted"],
    "terminal_output": ["untrusted"],
    "program_output": ["untrusted"],
    "model_output": ["untrusted"],
    "private_data": ["private"],
    "secret_used": ["private"],
    "owner_input": ["owner"],
}


DEFAULT_TAINT_TAGS = {"browser": ["untrusted"]}

# effects that need a person once a context has read untrusted content (ROUTING.md, "After untrusted
# content"): the ladder steps that act outside, and the specials that run, serve, install or make a VM. `secret`
# is not one and cannot be made one (TaintPolicy): reading a login page is how every login starts, and
# the vault decides where a password may go by the page itself, whatever the agent read
DEFAULT_DOWNGRADE = ["commit", "pay", "irreversible", "exec", "service", "install_overlay", "install_base",
                     "vm"]


class TaintPolicy(BaseModel):
    """The `[taint]` table of policy.toml: which effects wait for a person after untrusted content,
    and how long the owner's denial of such a call quiets the whole grant family."""
    model_config = ConfigDict(extra="forbid")
    downgrade: list[str] = Field(default_factory=lambda: list(DEFAULT_DOWNGRADE))
    denied_cooldown_s: int = Field(default=600, ge=0, le=86400)

    @field_validator("downgrade")
    @classmethod
    def _downgrade(cls, v: list[str]) -> list[str]:
        known = {e.value for e in Effect}
        unknown = sorted(set(v) - known)
        if unknown:
            raise ValueError(f"unknown effects in [taint] downgrade: {unknown} (known: {sorted(known)})")
        if "read" in v:
            raise ValueError("[taint] downgrade cannot hold read: reads stay free")
        if "secret" in v:
            # dropped, not refused: the list can only be written whole, so most files that hold it are
            # a copy of the old default, and a node must still start. A node that does want a person
            # at every login says so with a [routing] rule for `secret`
            log.warning('policy.toml: [taint] downgrade holds "secret", which is ignored: a password in the '
                        "vault is the owner's permission, so a login never waits for a person. Remove it")
        return [e for e in dict.fromkeys(v) if e != "secret"]


class DeviceGrants(BaseModel):
    """The most a grant issued with the owner's device credential may hold (docs/THREAT-MODEL.md). The
    credential sits in a Keychain an agent on that computer can read, so what it issues is bounded like
    what an admin grant issues: never wider than this, in lanes, ceiling, specials and life."""
    model_config = ConfigDict(extra="forbid")
    workspaces: list[str] = Field(default_factory=lambda: ["*"])
    lanes: list[str] | None = None
    ceiling: Effect = Effect.COMMIT
    specials: dict[str, dict] = Field(default_factory=dict)  # name -> its cap, as in a grant's specials
    max_ttl_s: int = Field(default=90 * 86400, ge=60)


class PolicyConfig(BaseModel):
    clock_skew_s: int = Field(default=60, ge=0, le=3600)
    approval_ttl_s: int = Field(default=24 * 3600, ge=60)
    question_ttl_s: int = Field(default=24 * 3600, ge=60)
    denied_cooldown_s: int = Field(default=3600, ge=0)
    max_pending_per_grant: int = Field(default=50, ge=1)
    # a one-off approval of a publish the commit gate caught (AUD-03) covers the retried click only
    # this long after it was asked for; older, it is refused (approval_stale) and asked again (AUD-20)
    publish_approval_max_age_s: int = Field(default=2 * 3600, ge=60)
    default_grant_ttl_s: int = Field(default=7 * 86400, ge=60)
    max_grant_ttl_s: int = Field(default=366 * 86400, ge=60)
    device_grants: DeviceGrants = Field(default_factory=DeviceGrants)
    default_depth: int = Field(default=2, ge=0, le=10)
    allow_offline_attenuation: bool = True
    # token v1 migration window (TOKENS.md): v1 grants keep verifying until this is false or, for
    # this node's own grants, until the owner first rotates the grant key; new grants are v2
    accept_v1_tokens: bool = True
    session_ttl_s: int = Field(default=12 * 3600, ge=60)
    trace_retention_days: int = Field(default=90, ge=1)
    trace_busy_timeout_ms: int = Field(default=250, ge=1)
    # a call that changes anything is refused (audit_unwritable) once the action log has recorded
    # nothing for this long, the spill file included; reads keep working (agentos.policy.audit_health)
    audit_fail_closed_s: int = Field(default=120, ge=10)
    db_busy_timeout_ms: int = Field(default=5000, ge=1)
    brain_tiers: list[str] = Field(default_factory=lambda: ["small", "medium", "large"])
    brain_tier_aliases: dict[str, str] = Field(
        default_factory=lambda: {"haiku": "small", "sonnet": "medium", "opus": "large"})
    install_allowlist: list[str] = Field(default_factory=list)
    taint_kinds: dict[str, list[str]] = Field(default_factory=lambda: dict(DEFAULT_TAINT_KINDS))
    # labels added after every call of a tool with this tag; browser tools bring page text, which
    # anyone can write, into the agent's context (AUD-31). Add "private" for lanes whose pages are
    # the owner's private data (inbox, DMs): then sinks outside the egress allowlist ask first.
    taint_tags: dict[str, list[str]] = Field(default_factory=lambda: {k: list(v) for k, v in DEFAULT_TAINT_TAGS.items()})
    routing: RoutingTable = Field(default_factory=RoutingTable)
    # after untrusted content in a context, these effects need a person (ROUTING.md)
    taint: TaintPolicy = Field(default_factory=TaintPolicy)
    # who may decide beyond the member grants: four-eyes per workspace and effect (default: none)
    approvals: ApprovalPolicy = Field(default_factory=ApprovalPolicy)
    # daily and hourly meters: the node's day, warning thresholds, per-lane limits (meters.py)
    meters: MeterPolicy = Field(default_factory=MeterPolicy)
    # how strong a yes must be once something asks (confirmation.py); its floors refuse to load
    confirmation: ConfirmationConfig = Field(default_factory=ConfirmationConfig)

    @model_validator(mode="after")
    def _one_switch(self) -> PolicyConfig:
        """`[confirmation] commit.when` and `[routing] ask_first_default` are one setting, so a file
        that says both must say the same thing."""
        commit = self.confirmation.commit
        if "when" not in commit.model_fields_set:
            commit.when = "ask" if self.routing.ask_first_default else "auto"
            return self
        ask = commit.when == "ask"
        if "ask_first_default" in self.routing.model_fields_set and self.routing.ask_first_default != ask:
            raise ValueError("[confirmation] commit.when and [routing] ask_first_default are one setting and "
                             "disagree: keep one")
        self.routing.ask_first_default = ask
        return self


def load_policy_config(home: Path) -> PolicyConfig:
    path = home / "policy.toml"
    if not path.exists():
        return PolicyConfig()
    return PolicyConfig.model_validate(tomllib.loads(path.read_text()))
