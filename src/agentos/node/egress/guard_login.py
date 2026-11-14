"""The vault's login window of a lane's egress guard (LaneGuard): a mixin. While a vault login
runs on a domain, its posts there are a login, not a publish.
"""

from __future__ import annotations

from typing import Any

from agentos.node.egress import sites as _sites
from agentos.node.egress.helpers import _post_data
from agentos.node.egress.rules import EffectRule
from agentos.node.egress.shapes import operations
from agentos.node.egress.sites import host_of, norm_path


class GuardLogin:
    """The vault's login window: while a login runs, its posts to the login's domain are let through."""

    # ---- the vault's login window ----------------------------------------------------------

    def open_login(self, domain: str) -> None:
        """The vault is filling a login field of `domain`, its entry's registrable domain, on this lane:
        open that domain's login window, or renew it (a login of several steps). Called by the typist
        (agentos.node.typist) only, after the entry passed the vault's checks, so a page, a model or a
        tool argument cannot reach it. See the module docstring."""
        self._login_until[domain] = self.clock() + self.login_window_s

    def close_login(self, domain: str) -> None:
        """The fill failed: no window stays open for it, an earlier fill's included."""
        self._login_until.pop(domain, None)

    def login_open(self, host: str) -> bool:
        """Is a login window open for this host's site now? The devtools capture asks, so the answer
        to a post sent inside one (it can carry the session) is never returned (agentos.node.devtools)."""
        now = self.clock()
        open_for = [d for d, until in self._login_until.items() if now < until]
        if not open_for or not host:
            return False
        try:
            site = _sites.registrable_domain(host)
        except ValueError:
            return True  # a host the window cannot be told apart from: treated as inside it
        if site is None:  # no public suffix to read a site from: the domain itself or below it
            return any(host == d or host.endswith("." + d) for d in open_for)
        return any(_sites.same_site(site, domain) for domain in open_for)

    def _in_login(self, rule: EffectRule | None, request: Any, url: str, method: str,
                  headers: dict[str, str]) -> bool:
        """Is this POST inside an open login window, so the gate need not hold it for a shape it cannot
        read? Never for what a commit or write rule names: classify reads one operation, so every
        operation of a batch is checked here. A body that cannot be read as operations is a login's
        only where no read catch-all matched: on a GraphQL endpoint it could carry anything."""
        method = method.upper()
        if method != "POST" or not self._login_until:
            return False
        now = self.clock()
        self._login_until = {d: until for d, until in self._login_until.items() if now < until}
        host = host_of(url) or ""
        try:
            site = _sites.registrable_domain(host)
        except ValueError:
            return False
        if not any(_sites.same_site(site, domain) for domain in self._login_until):
            return False
        ops = operations(headers, _post_data(request))
        if ops is None:
            return rule is None
        low, path = {k.lower(): v for k, v in headers.items()}, norm_path(url)
        return not any(r.effect in ("commit", "write") and r.matches(host, path, method, low, lambda n=op["name"]: n)
                       for op in ops for r in self.rules)
