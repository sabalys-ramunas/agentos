"""Who drives a lane, and the verdicts the guard reaches: refusals, blocks, a flow's commit scope."""

from __future__ import annotations

import inspect
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from agentos.core.models import Effect
from agentos.core.tools import gate_kind
from agentos.node.egress.constants import SAFE_METHODS, SHAPE_PARSE_MAX
from agentos.node.egress.rules import EffectRule
from agentos.node.egress.shapes import body_covered, gql_verdict, request_op
from agentos.node.egress.sites import host_of, in_list, in_sites, site_scope

# ---- who drives the lane -----------------------------------------------------------------

HUMAN_KINDS = frozenset({"human", "panel"})


@dataclass(frozen=True)
class Driver:
    """Who acts in the lane, and what their grant allows. `lists` holds one egress list per grant
    block that sets one (its `egress`, else its `domains`); None when no block sets one, which is
    no grant restriction on the lane. `ceiling` is the grant's effect ceiling; None is unknown."""
    kind: str
    id: str
    grant_id: str | None = None
    lists: tuple[tuple[str, ...], ...] | None = None
    ceiling: str | None = None

    @property
    def human(self) -> bool:
        return self.kind in HUMAN_KINDS

    @classmethod
    def from_context(cls, ctx: Any) -> Driver:
        principal = getattr(ctx, "principal", None)
        kind = gate_kind(ctx)  # a member acting through the controller is held to the grant's list
        pid = getattr(principal, "id", None) or "unknown"
        grant = getattr(ctx, "grant", None)
        if grant is None:
            return cls(kind=kind, id=pid)
        return cls(kind=kind, id=pid, grant_id=getattr(grant, "id", None), lists=grant_lists(grant),
                   ceiling=_ceiling(grant))

    def allows_host(self, host: str) -> bool:
        return self.lists is None or all(in_list(lst, host) for lst in self.lists)

    def allows(self, effect: str) -> bool:
        if self.ceiling is None:
            return True
        need = Effect(effect).rank or 0
        have = Effect(self.ceiling).rank
        return have is not None and have >= need

    def public(self) -> dict[str, Any]:
        return {"kind": self.kind, "id": self.id, "grant": self.grant_id}


def grant_lists(grant: Any) -> tuple[tuple[str, ...], ...] | None:
    lists = []
    for b in getattr(grant, "blocks", ()):
        lst = b.egress if getattr(b, "egress", None) is not None else getattr(b, "domains", None)
        if lst is not None:
            lists.append(tuple(lst))
    return tuple(lists) if lists else None


def _ceiling(grant: Any) -> str | None:
    """The grant's effect ceiling. None only for a grant-like object that has no ceiling at all (or
    holds None); a ceiling that is there but cannot be read (a grant with no blocks, a block whose
    ceiling is not an Effect) is the lowest rung, so the guard refuses writes and publishes rather
    than lift every limit (fail closed)."""
    if inspect.getattr_static(grant, "ceiling", None) is None:
        return None
    try:
        ceiling = grant.ceiling
        return None if ceiling is None else Effect(ceiling).value
    except Exception:  # noqa: BLE001 - unreadable: the lowest rung, never no limit
        return Effect.READ.value


class CommitMeter(Protocol):
    """What a publish during an agent's act is charged to (agentos.node.tools.guarding.ActMeter)."""

    def charge(self, rule: EffectRule, *, sent: bool) -> str | None:
        """One commit; None within the limits, else the sentence saying which limit it passes.
        `sent` False: the request is held and will be aborted, so nothing is debited. A meter may
        set `code` to the refusal's code (lane_paced for the lane's posting pace)."""

    def capped(self) -> bool:
        """Is a commit past a limit already (the act then runs under the strict route)?"""


class PublishGate(Protocol):
    """What holds a publish during an agent's act until a person approved exactly what it sends
    (agentos.node.commitgate.ActGate, AUD-03). The guard routes the rule hosts from the agent's
    first gated act until control leaves it, and asks the latest act's gate about each request that
    matches a commit rule or that it cannot tell from one (`<site>.unknown`)."""

    submit_like: bool  # the act presses (a click, Enter): the act settles, and an unmatched non-GET is recorded

    async def hold(self, rule: EffectRule, request: Any, url: str, method: str) -> Refusal | None:
        """None: the request may leave (approved, standing, or the routing table lets it); else
        why it is aborted. Never raises (a failure is a refusal: fail closed)."""


# ---- verdicts ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Refusal:
    rule: str        # lane_sites | grant_egress | non_get | commit | write | ask_first | unreadable | scheme |
    #                  limit_reached | lane_paced | flow_commit
    reason: str


def _body_values(body: bytes | None, path: str) -> list[Any] | None:
    """Every value at a `json:<a.b[].c>` or `form:<name>` path of a request body (a list for
    each `[]`), or None when the body cannot be read as that kind."""
    from urllib.parse import parse_qsl

    if not body or len(body) > SHAPE_PARSE_MAX:
        return None
    kind, _, where = path.partition(":")
    text = body.decode("utf-8", "replace")
    if kind == "form":
        return [v for k, v in parse_qsl(text, keep_blank_values=True) if k == where]
    try:
        cur: list[Any] = [json.loads(text)]
    except ValueError:
        return None
    for part in where.split("."):
        each = part.endswith("[]")
        key = part[:-2] if each else part
        nxt: list[Any] = []
        for value in cur:
            if isinstance(value, list):  # a batch body: each element
                value = [v.get(key) for v in value if isinstance(v, dict) and key in v]
                nxt += [x for v in value for x in (v if each and isinstance(v, list) else [v])]
            elif isinstance(value, dict) and key in value:
                v = value[key]
                nxt += v if each and isinstance(v, list) else [v]
        cur = nxt
    return cur


@dataclass(frozen=True)
class FlowCommit:
    """The commit request a connector run declared (agentos.runner.connectors.models.CommitRequest),
    which scopes what the run's page may send (LaneGuard.scope_flow). While the run holds the lane,
    a request of its page that a commit or write rule matches (an upload, a `draft` rule, aside)
    leaves only when it is the declared request: the same method, its URL, its operation name and
    every `match` field. Anything else is refused before it leaves (rule `flow_commit`): a like's
    `reactions.delete`, a join's leave (`subscribeState` other than SUBSCRIBED), a read operation's
    any write at all. The egress rule stays wide (it proves both directions); the run is narrow.

    `strict` (a learned operation's run, docs/design/learn-a-site.md section 3): the run's page sends
    no non-GET to a rule host but the declared request, an upload (a `draft` rule) or a known read
    (a read rule whose operations are all known reads, or whose body it covers). Anything else, a
    request no rule names included, is refused before it leaves (strict_problem)."""
    flow: str
    method: str | None = None
    url: str | None = None
    op: str | None = None
    match: tuple[tuple[str, str], ...] = ()
    strict: bool = False

    @classmethod
    def of(cls, flow: str, request: Mapping[str, Any] | None, *, strict: bool = False) -> FlowCommit:
        strict = strict or bool(request and request.get("strict") is True)
        if not request or request.get("url") is None and set(request) <= {"strict"}:
            return cls(flow, strict=strict)
        match = request.get("match") or {}
        return cls(flow, method=str(request.get("method") or "POST").upper(), url=request.get("url"),
                   op=request.get("op"), match=tuple(sorted((str(k), str(v)) for k, v in match.items())),
                   strict=strict)

    def problem(self, rule: EffectRule | None, url: str, method: str, headers: dict[str, str] | None,
                body: Callable[[], bytes | None],
                read_ops: Mapping[str, frozenset[str]] | None = None) -> str | None:
        """None when this request may leave during the run, else why not. `read_ops`: the operations
        the owner promoted to known reads on this request's endpoint (strict runs only)."""
        if self.strict:
            return self.strict_problem(rule, url, method, headers, body, read_ops)
        if rule is None or rule.effect not in ("commit", "write") or rule.kind == "draft":
            return None
        return self._undeclared(rule, url, method, headers, body)

    def strict_problem(self, rule: EffectRule | None, url: str, method: str, headers: dict[str, str] | None,
                       body: Callable[[], bytes | None],
                       read_ops: Mapping[str, frozenset[str]] | None = None) -> str | None:
        """A strict run's verdict on one of its page's requests to a rule host: None for a GET, an
        upload (`draft`), a known read or the declared request; else why it is refused."""
        if method.upper() in SAFE_METHODS or (rule is not None and rule.kind == "draft"):
            return None
        raw: list[bytes | None] = []

        def data() -> bytes | None:
            if not raw:
                raw.append(body())
            return raw[0]

        if rule is not None and rule.effect == "read":
            if rule.strict_ops:
                read, _ = gql_verdict(rule.site, headers, data(), extra=read_ops)
                if read:
                    return None
            elif rule.fields is not None:
                if body_covered(rule.fields, headers, data())[0]:
                    return None
            else:
                return None
        named = rule if rule is not None else EffectRule(f"{host_of(url) or '?'}.unknown", "", "", "")
        why = self._undeclared(named, url, method, headers, data)
        if why is None:
            return None
        if rule is None:
            return f"{self.flow} sends a {method.upper()} no rule names, and its scope is strict"
        if rule.effect == "read":
            return f"{self.flow} sends {rule.id} that is not a known read, and its scope is strict"
        return why

    def _undeclared(self, rule: EffectRule, url: str, method: str, headers: dict[str, str] | None,
                    body: Callable[[], bytes | None]) -> str | None:
        """None when the request is the declared one, else why it is not."""
        what = f"{self.flow} sends {rule.id}"
        if self.url is None:
            return f"{what}, and the operation declares no commit request (a read never writes)"
        if method.upper() != self.method:
            return f"{what} as a {method.upper()}, and it declares a {self.method}"
        if re.match(self.url, url) is None:
            return f"{what} to another address than the one it declares"
        raw: list[bytes | None] = []

        def data() -> bytes | None:
            if not raw:
                raw.append(body())
            return raw[0]

        if self.op is not None:
            name = request_op(headers, data())
            if name is None or re.match(self.op, name) is None:
                return f"{what} as {name or 'an unnamed operation'}, not the operation it declares"
        for path, rx in self.match:
            values = _body_values(data(), path)
            if not values or any(not isinstance(v, str | int | bool) or re.fullmatch(rx, str(v)) is None
                                 for v in values):
                return f"{what} with {path} other than the operation declares (its undo)"
        return None


def judge(url: str, method: str, *, navigation: bool, sites: tuple[str, ...] | list[str],
          driver: Driver | None, human: bool, effect: EffectRule | None = None,
          frame: bool = False, asks_first: Callable[[], bool] | None = None, flow: bool = False,
          exposed: frozenset[str] | set[str] | tuple[str, ...] = ()) -> Refusal | None:
    """None when the guard lets this request go, else why not. `human`: a human has control of
    the lane, and then nothing is refused. The guard stops an agent being steered off the lane's
    sites by text it read; a person who took control decides where to go, and sign-in providers
    send them through hosts no list predicts (accounts.google.<country>, gds.google.com, a
    captcha's own sandbox domain). Off-list documents are noted instead (LaneEgress._note_human).
    `frame`: the document loads in a subframe, where `frame:` entries of the site list admit it too.
    `asks_first`: is "Ask me first" on for this lane (read only for an agent's commit). `flow`: a
    flow run sends it (no driver judges it): a non-GET is still held to the lane's site list.
    `exposed`: the hosts of the owner's dev servers exposed to this lane right now
    (agentos.node.expose): inside the lane's site list and every egress list of the grant while the
    expose lasts. Effect rules and Ask me first still judge what is sent to them."""
    host = host_of(url)
    method = method.upper()
    agent = driver is not None and not driver.human and not human
    if host is None or human:
        return None
    if not (host and host in exposed):  # the owner's own dev server, exposed to this lane: no host list applies
        if navigation and host == "":  # decided here: an address that cannot be read is judged no further
            grant_lists_set = agent and driver is not None and driver.lists is not None
            return Refusal("unreadable", "the address cannot be read") if sites or grant_lists_set else None
        where = (_page_refusal(host, sites, frame=frame, agent=agent, driver=driver) if navigation
                 else _send_refusal(host, method, sites, agent=agent, flow=flow, driver=driver))
        if where is not None:
            return where
    return _effect_refusal(host, method, agent=agent, driver=driver, effect=effect, asks_first=asks_first)


def _page_refusal(host: str, sites: tuple[str, ...] | list[str], *, frame: bool, agent: bool,
                  driver: Driver | None) -> Refusal | None:
    """A document load (to a readable address): on the lane's site list (a frame entry only inside
    a page), and on the egress list of an agent's grant."""
    if sites and not in_sites(sites, host, frame=frame):
        if site_scope(sites, host) == "frame":
            return Refusal("lane_sites", f"{host} is on this browser's site list only as a frame inside a page, "
                                         "not as a page of its own")
        return Refusal("lane_sites", f"{host} is not on this browser's site list")
    if agent and driver is not None and not driver.allows_host(host):
        return Refusal("grant_egress", f"{host} is outside the egress list of the grant driving this browser")
    return None


def _send_refusal(host: str, method: str, sites: tuple[str, ...] | list[str], *, agent: bool, flow: bool,
                  driver: Driver | None) -> Refusal | None:
    """An agent's or a flow's non-GET: to a host the lane allows (a frame the list admits may post
    to its own host: a captcha checks its answer there), and the grant too for an agent."""
    if not ((agent or flow) and method not in SAFE_METHODS):
        return None
    if host == "":
        grant_lists_set = agent and driver is not None and driver.lists is not None
        if sites or grant_lists_set:
            return Refusal("non_get", f"a {method} to an unreadable address")
    elif sites and not in_sites(sites, host, frame=True):
        return Refusal("non_get", f"a {method} to {host} goes outside the hosts this browser allows")
    elif agent and driver is not None and not driver.allows_host(host):
        return Refusal("non_get", f"a {method} to {host} goes outside the hosts this browser and its grant allow")
    return None


def _effect_refusal(host: str, method: str, *, agent: bool, driver: Driver | None, effect: EffectRule | None,
                    asks_first: Callable[[], bool] | None) -> Refusal | None:
    """An agent's publish or write: within the grant's ceiling, and never a publish by clicking
    while the account asks a person first."""
    if not agent or driver is None or effect is None:
        return None
    if effect.effect in ("commit", "write"):
        need = "commit" if effect.effect == "commit" else "draft"
        if not driver.allows(need):
            what = "publishes" if effect.effect == "commit" else "writes"
            return Refusal(effect.effect, f"this {method} to {host} {what} ({effect.id}), and the grant driving "
                                          f"this browser allows up to {driver.ceiling}")
    if effect.effect == "commit" and asks_first is not None and asks_first():
        return Refusal("ask_first", f"this {method} to {host} publishes ({effect.id}), and this account asks a person "
                                    "first, so an agent cannot publish here by clicking. Run a routine for it, "
                                    "which asks the person, or ask the person with human_ask")
    return None


@dataclass
class Block:
    """One refused navigation or request, as tool results, events and the trace show it."""
    seq: int
    host: str
    rule: str
    reason: str
    kind: str              # action | navigation | subframe | popup | redirect | committed | request
    method: str
    path: str
    prevented: bool        # True: refused before it was sent; False: it reached the host, then stopped
    at: float = field(default_factory=time.time)
    driver: dict[str, Any] | None = None
    effect_rule: str | None = None
    frame: bool = False    # the document was to load in a subframe

    def public(self) -> dict[str, Any]:
        out = {"host": self.host, "rule": self.rule, "reason": self.reason, "kind": self.kind,
               "method": self.method, "prevented": self.prevented}
        if self.effect_rule:
            out["effect_rule"] = self.effect_rule
        return out


def refusal_message(block: Block | dict[str, Any]) -> str:
    """The tool error for a refused navigation (a Block or its public() dict)."""
    reason = block.reason if isinstance(block, Block) else block["reason"]
    prevented = block.prevented if isinstance(block, Block) else block["prevented"]
    sent = "nothing was sent" if prevented else "the request reached the host and was stopped"
    if (block.rule if isinstance(block, Block) else block["rule"]) == "ask_first":
        return f"{reason} ({sent})."
    return (f"{reason}; the page stays where it was ({sent}). Do not retry or work around it: "
            "tell the human which host was refused")
