"""A grant in plain language: what it lets its holder do, where, until when, and what waits for you.

Pure (no I/O). The sentences follow the grant's whole chain (a narrowed grant never does more than
the grant it came from) and the routing table in force, so a custom policy.toml is described as it
is, not as the defaults would be. `openajar grants explain`, `grant_info` and the panel use it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from openajar.core.models import Effect
from openajar.policy import memory_rules
from openajar.policy.confirm import ALWAYS_ASK
from openajar.policy.grants import Grant, _remaining_depth
from openajar.policy.routing import RoutingTable
from openajar.policy.util import iso

LADDER_WORDS = {
    Effect.READ: ("Reading", "look at pages"),
    Effect.DRAFT: ("Drafting", "prepare private drafts"),
    Effect.COMMIT: ("Publishing", "publish (posts, messages, comments, submitted forms)"),
    Effect.PAY: ("Paying", "spend money"),
    Effect.IRREVERSIBLE: ("An irreversible step", "take steps that cannot be undone"),
}
SPECIAL_WORDS = {
    "secret": "Typing a vault secret",
    "exec": "Running a command",
    "install_overlay": "Installing a package into a lane",
    "install_base": "Installing into the base image",
    "service": "Starting a service",
    "brain": "Using the brain",
    "vm": "Renting a cloud VM",
}
SUBJECT_WORDS = {"agent": "An agent", "human": "A person", "flow": "A flow", "brain": "The node's brain",
                 "panel": "A panel session"}


def explain(grant: Grant, *, routing: RoutingTable | None = None, remaining: dict | None = None,
            now: float | None = None) -> list[str]:
    """Short sentences, most important first. `remaining` is PolicyService.remaining(grant)."""
    routing = routing or RoutingTable()
    now = time.time() if now is None else now
    if grant.owner_token:
        return ["This is the node's owner token: it reaches every lane and every effect on this node.",
                *_routing_lines(Effect.IRREVERSIBLE, routing),
                "Keep it on your own computers; give agents a narrower grant (`openajar grants issue`)."]
    out = [_who(grant), _where(grant)]
    ceiling = grant.ceiling
    if ceiling == Effect.READ:
        out.append("It may only look at pages: it cannot change anything (ceiling: read).")
    else:
        out.append(f"It may {_ladder_reach(ceiling)} (ceiling: {ceiling.value}).")
    out += _routing_lines(ceiling, routing)
    out += _sites(grant, routing)
    out += _specials(grant, routing)
    out += _person(grant)
    if grant.admin:
        out.append("It is an admin grant: it can manage grants, approvals, the vault and node settings.")
    out += _limits(grant, remaining, now)
    return [s for s in out if s]


def _who(grant: Grant) -> str:
    s = grant.subject
    who = f"{SUBJECT_WORDS.get(s.kind, 'A holder')} grant for {s.id}"
    if grant.depth:
        return f"{who}, narrowed from {grant.root_id}, so it can never do more than that grant."
    return f"{who}, issued by {s.owner}."


def _lane_words(patterns: list[str]) -> str:
    words = []
    for p in patterns:
        ws, _, name = p.partition("/")
        if p == "*/*" or p == "*":
            words.append("every lane on this node")
        elif name in ("*", "") and "*" not in ws:
            words.append(f"every lane in {ws}")
        elif "*" in p:
            words.append(f"lanes matching {p}")
        else:
            words.append(p)
    return _join(words)


def _effective_lanes(grant: Grant) -> list[str] | None:
    """The tightest lane patterns along the chain (the narrowing check keeps each inside the last)."""
    found: list[str] | None = None
    for i, b in enumerate(grant.blocks):
        pats = b.lane_patterns(authority=i == 0)
        if pats is not None:
            found = pats
    return found


def _where(grant: Grant) -> str:
    if not grant.has_lane_scope():
        return "It reaches no lanes: it can only read node-level information, such as its own grant."
    return f"It reaches {_lane_words(_effective_lanes(grant) or [])}."


def _ladder_reach(ceiling: Effect) -> str:
    parts = [words for eff, (_, words) in LADDER_WORDS.items() if eff.rank is not None and eff.rank <= ceiling.rank]
    return _join(parts)


def _route(effect: str, routing: RoutingTable) -> tuple[str, list[str]]:
    """(what usually happens, the notes of the rules that do something else first). An auto rule
    never holds for pay or irreversible (RoutingTable.decide), so it is no exception for them."""
    mine = [r for r in routing.rules if r.effect in (effect, "*") and not (effect in ALWAYS_ASK and r.then == "auto")]
    default = next((r for r in mine if not r.when), None)
    then = default.then if default else "approval"   # no matching rule: approval (fail closed)
    before = mine[: mine.index(default)] if default else mine
    return then, [r.note or ", ".join(f"{k}={v}" for k, v in r.when.items()) for r in before if r.then != then]


def _route_phrase(label: str, effect: str, routing: RoutingTable) -> str:
    then, exceptions = _route(effect, routing)
    standing = effect in routing.standing_effects
    if any(r.effect == effect and r.when.get("runs_on_own") and r.then == "auto" for r in routing.rules):
        # Ask me first (openajar.policy.ask_first) decides a commit
        cover = " or a standing approval you gave covers it" if standing else ""
        if routing.ask_first_default:
            return (f"{label} waits for your approval unless a person turned Ask me first off for the routine or "
                    f"account{cover}.")
        return (f"{label} runs on its own, unless Ask me first is on for the routine or account: then it waits "
                f"for your approval{(' unless a standing approval you gave covers it' if standing else '')}.")
    if then == "deny":
        return f"{label} is refused."
    if then == "auto":
        if not exceptions:
            return f"{label} runs on its own."
        return f"{label} runs on its own, but waits for your approval when: {'; '.join(exceptions)}."
    tail = " unless a standing approval you gave covers it" if standing else ", and only a person can approve it"
    if exceptions:
        return f"{label} waits for your approval{tail} (it runs on its own when: {'; '.join(exceptions)})."
    return f"{label} waits for your approval{tail}."


def _routing_lines(ceiling: Effect, routing: RoutingTable) -> list[str]:
    return [_route_phrase(label, eff.value, routing) for eff, (label, _) in LADDER_WORDS.items()
            if eff.rank is not None and eff.rank <= ceiling.rank]


def _tightest(lists: list[list[str]]) -> list[str]:
    return lists[-1] if lists else []


def _sites(grant: Grant, routing: RoutingTable) -> list[str]:
    out = []
    domains = next((b.domains for b in reversed(grant.blocks) if b.domains is not None), None)
    if domains is not None:
        out.append(f"In those lanes it may only use {_join(domains)}." if domains
                   else "It may not open any site.")
    egress = _tightest(grant.egress_lists())
    where = f"a site outside {_join(egress)}" if egress else "any site"
    out.append(f"Once a session has read both untrusted page text and private data, sending anything to "
               f"{where} waits for your approval.")
    return out


def _cap_lanes(caps: list[Any], fallback: str) -> str:
    lanes = next((c.lanes for c in reversed(caps) if getattr(c, "lanes", None) is not None), None)
    return _lane_words(lanes) if lanes else fallback


def _secret_words(caps: list[Any]) -> str:
    return f"The vault may type passwords and codes for it in {_cap_lanes(caps, 'its lanes')}; it never sees them."


def _exec_words(caps: list[Any]) -> str:
    return f"It may run commands in {_cap_lanes(caps, 'its lanes')}."


def _install_words(caps: list[Any]) -> str:
    sources = next((c.sources for c in reversed(caps) if c.sources is not None), None)
    return "It may install packages into a lane" + (f", only {_join(sources)}." if sources else ".")


def _install_base_words(caps: list[Any]) -> str:
    return "It may install into the base image that every lane shares."


def _service_words(caps: list[Any]) -> str:
    ports = next((c.ports for c in reversed(caps) if c.ports is not None), None)
    ttls = [c.max_ttl_s for c in caps if c.max_ttl_s is not None]
    return ("It may run background services"
            + (f" on ports {_join([str(p) for p in ports])}" if ports else "")
            + (f" for up to {_duration(min(ttls))} each" if ttls else "") + ".")


def _brain_words(caps: list[Any]) -> str:
    bits = []
    if (p := next((c.provider for c in reversed(caps) if c.provider), None)):
        bits.append(f"through {p}")
    if (t := next((c.tier for c in reversed(caps) if c.tier), None)):
        bits.append(f"up to the {t} tier")
    tok = [c.quota_tokens for c in caps if c.quota_tokens is not None]
    usd = [c.quota_usd for c in caps if c.quota_usd is not None]
    if tok:
        bits.append(f"at most {min(tok):,} tokens a day")
    if usd:
        bits.append(f"at most {min(usd):.2f} USD a day")
    return "It may use the node's brain" + (f" {', '.join(bits)}" if bits else "") + "."


def _vm_words(caps: list[Any]) -> str:
    profiles = next((c.profiles for c in reversed(caps) if c.profiles is not None), None)
    vm_repos = next((c.repos for c in reversed(caps) if c.repos is not None), None)
    return (f"It may rent disposable cloud VMs as {_cap_lanes(caps, 'lanes it may use')}"
            + (f", only the {_join(profiles)} profiles" if profiles else "")
            + "; they bill by the hour until the node deletes them."
            + (f" It may copy only {_join(vm_repos)} onto them." if vm_repos else ""))


# each special a grant may hold, in the order the explanation lists them, and how it says it
_SPECIAL_SENTENCES: tuple[tuple[str, Callable[[list[Any]], str]], ...] = (
    ("secret", _secret_words), ("exec", _exec_words), ("install_overlay", _install_words),
    ("install_base", _install_base_words), ("service", _service_words), ("brain", _brain_words), ("vm", _vm_words),
)


def _memory_words(grant: Grant) -> str | None:
    if (mem := memory_rules.describe(grant)) is None:
        return None
    scope = _join(mem["workspaces"]) if mem["workspaces"] else "the workspaces of its lanes"
    verb = "read and write" if mem["write"] else "read"
    extra = []
    if mem["write"] and mem["types"]:
        extra.append(f"it writes only {_join(mem['types'])} entries")
    if mem["private"]:
        extra.append("private entries included")
    return f"It may {verb} workspace memory in {scope}" + (f" ({'; '.join(extra)})" if extra else "") + "."


def _specials(grant: Grant, routing: RoutingTable) -> list[str]:
    out = [words(caps) for name, words in _SPECIAL_SENTENCES if (caps := grant.caps(name)) is not None]
    if (memory := _memory_words(grant)) is not None:
        out.append(memory)
    for name, label in SPECIAL_WORDS.items():
        if grant.caps(name) is not None:
            out.append(_route_phrase(label, name, routing))
    return out


def _person(grant: Grant) -> list[str]:
    out = []
    if (caps := grant.caps("approve")) is not None:
        effects = set(caps[0].effects)
        for c in caps[1:]:
            effects &= set(c.effects)
        limits = [c.max_usd for c in caps if c.max_usd is not None]
        pay = f", payments up to {min(limits):.2f} USD" if Effect.PAY in effects and limits else ""
        out.append(f"They may decide approvals for {_join(sorted(e.value for e in effects))} "
                   f"on {_cap_lanes(caps, 'their lanes')}{pay}.")
    if (caps := grant.caps("control")) is not None:
        watch = any(c.watch_only for c in caps)
        limits = [c.max_session_s for c in caps if c.max_session_s is not None]
        verb = "watch" if watch else "watch and take control of"
        out.append(f"They may {verb} {_cap_lanes(caps, 'their lanes')}"
                   + (f", for at most {_duration(min(limits))} at a time" if limits else "") + ".")
    return out


def _limits(grant: Grant, remaining: dict | None, now: float) -> list[str]:
    return [*_spend_words(grant, remaining), *_window_words(grant, now), *_delegation_words(grant)]


def _spend_words(grant: Grant, remaining: dict | None) -> list[str]:
    """Its budget (and what is left of it) and its meters."""
    out = []
    usd = [b.usd for _, b in grant.budgets() if b.usd is not None]
    tok = [b.tokens for _, b in grant.budgets() if b.tokens is not None]
    if usd or tok:
        parts = ([f"{min(usd):.2f} USD"] if usd else []) + ([f"{min(tok):,} tokens"] if tok else [])
        left = []
        if remaining and remaining.get("usd") is not None:
            left.append(f"{remaining['usd']:.2f} USD")
        if remaining and remaining.get("tokens") is not None:
            left.append(f"{remaining['tokens']:,} tokens")
        out.append(f"It may spend at most {' and '.join(parts)} in total"
                   + (f" ({' and '.join(left)} left)." if left else "."))
    daily = []
    for name, words in (("usd_per_day", "{:.2f} USD a day"), ("tokens_per_day", "{:,} tokens a day"),
                        ("commits_per_hour", "{} commits an hour"), ("commits_per_day", "{} commits a day")):
        values = [getattr(b, name) for _, b in grant.budgets() if getattr(b, name, None) is not None]
        if values:
            daily.append(words.format(min(values)))
    if daily:
        out.append(f"Its meters stop it at {', '.join(daily)} (days end at the owner's midnight).")
    return out


def _window_words(grant: Grant, now: float) -> list[str]:
    """When it starts working and when it expires."""
    out = []
    nbf, exp = grant.not_before, grant.expires_at
    if nbf is not None and nbf > now:
        out.append(f"It starts working at {iso(nbf)} (in {_duration(nbf - now)}).")
    if exp is None:
        out.append("It does not expire; revoke it with `openajar grants revoke` when it is no longer needed.")
    elif exp <= now:
        out.append(f"It expired at {iso(exp)}.")
    else:
        out.append(f"It expires at {iso(exp)} (in {_duration(exp - now)}).")
    return out


def _delegation_words(grant: Grant) -> list[str]:
    """Whether it may hand narrower copies of itself on, and its tool pins."""
    out = []
    depth = _remaining_depth(grant)
    fanout = grant.fanout_limit()
    if depth == 0 or fanout == 0:
        out.append("It cannot hand a narrower copy of itself to anyone.")
    else:
        levels = "" if depth is None else f", {depth} more level{'s' if depth != 1 else ''} down"
        most = "" if fanout is None else f", at most {fanout}"
        out.append(f"It may hand narrower copies of itself to other agents{levels}{most}.")
    if grant.pins():
        out.append("Only tools pinned to a reviewed version run under it; a changed tool is refused.")
    return out


def _duration(seconds: float) -> str:
    s = int(seconds)
    if s < 120:
        return f"{s} seconds"
    if s < 7200:
        return f"{s // 60} minutes"
    if s < 172800:
        return f"{s // 3600} hours"
    return f"{s // 86400} days"


def _join(items: Any) -> str:
    items = [str(i) for i in items or []]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]
