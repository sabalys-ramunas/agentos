"""Classifying a request by the effect rules: which rule it matches, which hosts the rules name, and the commit gate's route pattern."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from agentos.core import globs
from agentos.core.models import split_site
from agentos.node.egress import rules as _rules
from agentos.node.egress.constants import SAFE_METHODS, SHAPE_HEADERS
from agentos.node.egress.rules import EFFECT_KINDS, EffectRule
from agentos.node.egress.shapes import operations, request_op
from agentos.node.egress.sites import host_of, norm_path
from agentos.node.egress.suggested import SUGGESTED_SITES


def classify(url: str, method: str, headers: dict[str, str] | None = None,
             rules: tuple[EffectRule, ...] | None = None,
             body: bytes | Callable[[], bytes | None] | None = None) -> EffectRule | None:
    """The first rule that matches this request (of `rules`, else EFFECT_RULES), or None (an
    unknown request). `body` (bytes, or a function that returns them, called only when a rule
    with `op` gets that far) gives the operation name for rules that need one."""
    rules = _rules.EFFECT_RULES if rules is None else rules
    host = host_of(url)
    if host is None:
        return None
    path = norm_path(url)
    method = method.upper()
    low = {k.lower(): v for k, v in (headers or {}).items()}
    found: list[str | None] = []

    def op() -> str | None:
        if not found:
            raw = body() if callable(body) else body
            found.append(request_op(low, raw))
        return found[0]

    for rule in rules:
        if rule.matches(host, path, method, low, op):
            return rule
    return None


def effects_of(url: str, method: str, headers: dict[str, str] | None = None,
               rules: tuple[EffectRule, ...] | None = None,
               body: bytes | Callable[[], bytes | None] | None = None) -> list[EffectRule]:
    """Every action (EFFECT_KINDS, a write or commit) one request carries, for the action ledger.

    A request with one operation is what classify() says. A batch (a JSON array, Meta's `queries`
    form) is judged operation by operation: each named operation is classified on its own, and on
    an endpoint whose rules all read (a batch URL, a GraphQL catch-all) a name an action rule of the
    same site names counts too, the path aside. A rule that names no operation counts once."""
    rules = _rules.EFFECT_RULES if rules is None else rules
    raw = body() if callable(body) else body
    first = classify(url, method, headers, rules=rules, body=raw)
    low = {k.lower(): v for k, v in (headers or {}).items()}
    ops = operations(low, raw) if method.upper() not in SAFE_METHODS else None
    # operations() adds the names operation headers carry after the body's elements: a header names
    # the request's own operation (Instagram's x-fb-friendly-name), never one more of a batch
    in_body = (ops or [])[:max(0, len(ops or ()) - sum(1 for h in SHAPE_HEADERS
                                                        if h != "x-restli-method" and low.get(h)))]
    names = [o["name"] for o in in_body if o.get("name")]
    if ops is None and raw and not _acts(first) and method.upper() not in SAFE_METHODS \
            and not (first is not None and first.kind == "draft"):   # an upload's bytes name nothing
        return _scan_body(url, method, low, raw, rules, _acts)   # a body no parser reads: its names decide
    if len(in_body) <= 1 or not names:
        return [first] if _acts(first) else []
    return _batch_effects(url, method, low, names, rules, first)


def _acts(rule: EffectRule | None) -> bool:
    """Is this rule an action the ledger counts (a write or commit of an EFFECT_KINDS kind)?"""
    return rule is not None and rule.kind in EFFECT_KINDS and rule.effect in ("commit", "write")


def _batch_effects(url: str, method: str, low: dict[str, str], names: list[str], rules: tuple[EffectRule, ...],
                   first: EffectRule | None) -> list[EffectRule]:
    """A batch's actions, operation by operation (`first`: what classify() said of the request)."""
    host, path = host_of(url), norm_path(url)
    if host is None:
        return []
    up = method.upper()
    out: list[EffectRule] = []
    once: set[str] = set()
    site = first.site if first is not None else None
    for name in names:
        rule = next((r for r in rules if r.matches(host, path, up, low, lambda n=name: n)), None)
        if not _acts(rule) and site is not None:  # the batch endpoint's own rule reads: the name decides
            rule = next((r for r in rules if r.op is not None and r.site == site and _acts(r)
                         and globs.domain_match(r.host, host) and re.search(r.op, name)), None)
        if not _acts(rule):
            continue
        assert rule is not None
        if rule.op is None:   # the endpoint's rule, whatever the operation: one action for the request
            if rule.id in once:
                continue
            once.add(rule.id)
        out.append(rule)
    if not out and _acts(first):
        out.append(first)
    return out


_NAME_TOKEN = re.compile(rb"[A-Za-z_][A-Za-z0-9_.]{2,119}")
SCAN_MAX = 2 * 1024 * 1024   # how much of a body no parser reads is searched for operation names


def _scan_body(url: str, method: str, headers: dict[str, str], raw: bytes, rules: tuple[EffectRule, ...],
               acts: Callable[[EffectRule | None], bool]) -> list[EffectRule]:
    """A body the parsers cannot read (text/plain, multipart, too large) on a host an action rule
    names: every word in it that an action rule's operation pattern matches counts, on the side of
    more. The words are names a client sends (never matched against a pattern that is not anchored
    to a whole name), so a person's typed text that happens to equal one only over-counts."""
    host, path = host_of(url), norm_path(url)
    if host is None:
        return []
    up = method.upper()
    candidates = [r for r in rules if r.op is not None and acts(r) and globs.domain_match(r.host, host)]
    if not candidates:
        return []
    out: list[EffectRule] = []
    for m in _NAME_TOKEN.finditer(raw[:SCAN_MAX]):
        name = m.group(0).decode("ascii", "replace")
        rule = next((r for r in candidates if r.matches(host, path, up, headers, lambda n=name: n)), None)
        if rule is None:   # the name of an action of the site sent to another of its endpoints (a batch)
            rule = next((r for r in candidates if re.search(r.op, name)), None)
        if rule is not None:
            out.append(rule)
    return out


# A failed request the site may still have processed is counted (the ledger stays on the side of
# more); one of these failures happened before a byte reached the site, so it did not act.
NEVER_SENT = ("NS_ERROR_UNKNOWN_HOST", "NS_ERROR_UNKNOWN_PROXY_HOST", "NS_ERROR_CONNECTION_REFUSED",
              "NS_ERROR_PROXY_CONNECTION_REFUSED", "NS_ERROR_UNKNOWN_SOCKET_TYPE",
              "ERR_NAME_NOT_RESOLVED", "ERR_CONNECTION_REFUSED", "ERR_PROXY_CONNECTION_FAILED", "ERR_ADDRESS_UNREACHABLE",
              "ERR_TUNNEL_CONNECTION_FAILED", "ERR_NAME_RESOLUTION_FAILED")


def never_sent(failure: str | None) -> bool:
    """True when a request's failure text proves it never reached the site (no name, no connection)."""
    text = (failure or "").upper()
    return any(code in text for code in NEVER_SENT)


def rule_hosts(rules: tuple[EffectRule, ...] | None = None) -> tuple[str, ...]:
    """The host patterns the effect rules name (globs syntax): the sites the commit gate knows."""
    rules = _rules.EFFECT_RULES if rules is None else rules
    return tuple(dict.fromkeys(r.host for r in rules))


def ungated_sites(sites: Iterable[str], rules: tuple[EffectRule, ...] | None = None) -> list[str]:
    """The entries of a lane's site list that admit a host the commit gate does not read: a request to
    such a host is never held, so nobody is asked, whatever it does. A `frame:` entry counts, because
    a frame may post to its own host. A rule's `*.site` covers `site` too (unknown_rule)."""
    hosts = rule_hosts(rules)

    def covered(entry: str) -> bool:
        pattern = split_site(entry)[1]
        return any(globs.domain_pattern_within(pattern, host) or host == f"*.{pattern}" for host in hosts)

    return [entry for entry in sites if not covered(entry)]


def unreviewed_sites(sites: Iterable[str], rules: tuple[EffectRule, ...] | None = None) -> list[str]:
    """The entries of a lane's site list the gate does not read and nobody reviewed. A platform's own
    CDNs and sign-in hosts (SUGGESTED_SITES) are not read by the gate, but they are named here by
    hand and are no shop, so a lane built from them is known ground. Any other host is not."""
    reviewed = {entry for hosts in SUGGESTED_SITES.values() for entry in hosts}
    return [entry for entry in ungated_sites(sites, rules) if entry not in reviewed]


def gate_pattern(rules: tuple[EffectRule, ...] | None = None) -> re.Pattern[str]:
    """One URL regex for the commit gate's route: every URL on a host an effect rule (of `rules`,
    else EFFECT_RULES) names, whatever its path, so an unknown publish endpoint on the site and the
    first hop of a redirect into a commit endpoint are routed too (GETs go on at once). It matches
    a superset of what `host_of` normalizes to a rule host: any case, userinfo, a trailing dot, any
    port, and any host with a non-ASCII or percent-encoded character (the classifier then decides
    with the normalized host). Written in the regex syntax Python and JavaScript share, with no
    flags (letters are spelled `[aA]`: Python's IGNORECASE folds the Kelvin sign into `k`, the
    driver's does not), because Playwright matches it in its driver."""
    alts: list[str] = []
    for host in rule_hosts(rules):
        alt = (r"(?:[^/?#@:]*\.)?" + _anycase(host[2:])) if host.startswith("*.") else _anycase(host)
        if alt not in alts:
            alts.append(alt)
    alts.append(r"[^/?#@]*[%\u0080-\uffff][^/?#@]*")
    return re.compile(r"^(?:" + _anycase("http") + "[sS]?|" + _anycase("ws") + r"[sS]?)://(?:[^/?#@]*@)?(?:"
                      + "|".join(alts) + r")\.?(?::\d*)?(?:[/?#]|$)")


def _anycase(text: str) -> str:
    return "".join(f"[{c.lower()}{c.upper()}]" if c.isalpha() else re.escape(c) for c in text)
