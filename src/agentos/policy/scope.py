"""Scope and effect: steps 3 and 4 of the authorization order.

Part of PolicyService (agentos.policy.service), as a mixin. The lane and domain of a call must be
inside every block of the grant's chain, and its effect under the ceiling or a special the grant
holds, with the special's parameters (ports, brain provider and tier, VM profiles and repos). A parameter the grant limits
and the call does not state is outside the limit. The two routing facts read against a grant's
specials (an install source on its allowlist, a service's lifetime within the grant) live here too.
"""

from __future__ import annotations

import re
from typing import Any

from agentos.core import globs
from agentos.core.errors import InvalidRequest
from agentos.core.models import Effect, parse_lane_id
from agentos.policy.errors import Denied
from agentos.policy.grants import Grant, tier_rank

_PINNED_RE = re.compile(r"^(?P<eco>[a-z0-9]+):(?P<name>@?[^@\s]+)@(?P<ver>[A-Za-z0-9._+:-]+)$")


class ScopeMixin:
    """The scope checks of PolicyService (the attribute it reads, `cfg`, is set by
    PolicyService.__init__)."""

    def _check_lane(self, g: Grant, lane_id: str | None) -> str | None:
        if lane_id is None:
            return None
        try:
            parse_lane_id(lane_id)
        except ValueError as e:
            raise InvalidRequest(str(e)) from None
        if not g.lane_allowed(lane_id):
            raise Denied("out_of_scope", f"this grant does not cover {lane_id}")
        return lane_id

    def _check_domain(self, g: Grant, domain: str | None) -> str | None:
        if domain is None:
            return None
        host = globs.normalize_domain(domain)
        if not g.domain_allowed(host):
            raise Denied("domain", f"this grant does not cover {host}")
        return host

    def _check_effect(self, g: Grant, eff: Effect, lane_id: str | None, facts: dict, *, params: bool = True,
                      lane_later: bool = False) -> None:
        if g.limit is not None:
            g.limit.check_effect(eff)
        if eff.rank is not None:
            if eff.rank > g.ceiling.rank:
                raise Denied("ceiling", f"this grant allows up to {g.ceiling.value}, not {eff.value}")
            return
        caps = g.caps(eff.value)
        if caps is None:
            raise Denied("special", f"this grant does not include {eff.value}")
        for cap in caps:
            if eff in (Effect.SECRET, Effect.EXEC, Effect.VM) and cap.lanes is not None and not lane_later and (
                    lane_id is None or not any(globs.lane_match(p, lane_id) for p in cap.lanes)):
                raise Denied("special", f"{eff.value} is not granted for {lane_id or 'calls without `on`'}")
            if not params:
                continue
            if eff == Effect.SERVICE and cap.ports is not None:
                # an unstated fact the grant limits is outside the limit (BUG-8)
                ports = facts.get("ports")
                if not isinstance(ports, list | tuple) or not all(isinstance(p, int) for p in ports):
                    raise Denied("special", "this grant limits the service ports; state them as facts.ports")
                if not set(ports) <= set(cap.ports):
                    raise Denied("special", "the service asks for ports this grant does not allow")
            if eff == Effect.BRAIN:
                self._check_brain(cap, facts)
            if eff == Effect.VM and "repo" in facts:  # vm_clone
                if cap.repos is not None and not any(globs.repo_match(q, str(facts.get("repo"))) for q in cap.repos):
                    raise Denied("special", f"this grant allows only the repos {', '.join(cap.repos)} on VMs")
            elif eff == Effect.VM and cap.profiles is not None and facts.get("profile") not in cap.profiles:
                raise Denied("special", "this grant allows VMs of the profiles "
                             f"{', '.join(cap.profiles)} only")

    def _check_brain(self, cap: Any, facts: dict) -> None:
        if cap.provider is not None and facts.get("provider") != cap.provider:
            raise Denied("provider", f"this grant only allows the {cap.provider} brain")
        if cap.credential_owner is not None and facts.get("credential_owner") != cap.credential_owner:
            raise Denied("provider", "this grant does not allow that credential")
        if cap.tier is not None:
            want = facts.get("tier")
            if want is None:
                raise Denied("tier", "this grant limits the model tier; state the tier")
            cr = tier_rank(cap.tier, self.cfg.brain_tiers, self.cfg.brain_tier_aliases)
            wr = tier_rank(str(want), self.cfg.brain_tiers, self.cfg.brain_tier_aliases)
            if cr is None or wr is None or wr > cr:
                raise Denied("tier", f"this grant allows models up to the {cap.tier} tier")

    def _source_allowlisted(self, g: Grant, source: Any) -> bool:
        if not isinstance(source, str):
            return False
        m = _PINNED_RE.match(source)
        if not m or m.group("ver").lower() in ("latest", "next", "stable"):
            return False
        caps = g.caps("install_overlay") or []
        # only the authority block (or the node's allowlist) can list a source; a narrowed
        # grant's list can only take sources away
        authority = caps[0].sources if caps else None
        listed = source in self.cfg.install_allowlist or (authority is not None and source in authority)
        return listed and all(source in c.sources for c in caps if c.sources is not None)

    def _ttl_within(self, g: Grant, ttl: Any, now: float) -> bool:
        try:
            ttl = int(ttl)
        except (TypeError, ValueError):
            return False
        limits = [c.max_ttl_s for c in g.caps("service") or [] if c.max_ttl_s is not None]
        if g.expires_at is not None:
            limits.append(g.expires_at - now)
        return ttl > 0 and all(ttl <= lim for lim in limits)
