"""The one route dispatcher of a lane's browser context (docs/design/egress-guard.md).

Measured on Camoufox 158 (Juggler): any `context.route`, whatever its pattern, turns request
interception on for the whole context, so Firefox pauses every request for the driver and stops
storing responses in the HTTP cache (`INHIBIT_CACHING` on every page while a route exists;
entries already stored are still read). So the context has one owner of its routes, and a route
exists only while someone needs it:

- owners (the Web Bot Auth LaneSigner, an exposed dev server: agentos.node.expose) register
  Playwright-style `route(pattern, handler)` here, exactly as they would on the context (a glob, or a
  compiled regex); the dispatcher installs one real route per pattern;
- guards (the strict egress guard while an agent acts, and the commit gate's route while an agent
  drives) run first on every routed request. A guard that aborts a request returns True, and no owner sees it,
  so a refused request is never signed. A guard that raises fails closed: the request is aborted,
  and no owner sees it either. A guard names the URLs it needs routed: the strict guard every URL
  (GUARD_PATTERN), the commit gate the rule hosts (a compiled regex, which Playwright matches in
  the browser's driver, so requests to other hosts never wait for Python).

A routed request is dispatched once: the guards, then each owner whose pattern matches, in the
order they registered. An owner's `route.fallback()` passes to the next owner; after the last one
the request goes to the network unchanged (`route.continue_()`), never to another of our routes.
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any

log = logging.getLogger("agentos.node.routes")

Handler = Callable[[Any, Any], Awaitable[Any]]
Guard = Callable[[Any, Any], Awaitable[bool]]
Pattern = str | re.Pattern[str]
GUARD_PATTERN = "**/*"


@lru_cache(maxsize=256)
def _glob(pattern: str) -> re.Pattern[str]:
    """Playwright's URL glob: `**` any characters, `*` any but `/`, everything else literal."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def glob_match(pattern: str, url: str) -> bool:
    return bool(_glob(pattern).match(url))


def pattern_match(pattern: Pattern, url: str) -> bool:
    """A glob as Playwright reads it, or a compiled regex searched in the URL (as Playwright does)."""
    if isinstance(pattern, re.Pattern):
        return bool(pattern.search(url))
    return glob_match(pattern, url)


class _Step:
    """The route as one owner sees it: fallback() hands the request to the next owner."""

    def __init__(self, route: Any):
        self._route = route
        self.passed = False

    async def fallback(self, **overrides: Any) -> None:
        if overrides:
            raise ValueError("route.fallback with overrides is not supported by the lane's route dispatcher")
        self.passed = True

    def __getattr__(self, name: str) -> Any:
        return getattr(self._route, name)


class RouteDispatcher:
    """Owns every route of one browser context. See the module docstring."""

    def __init__(self, context: Any):
        self._ctx = context
        self._owners: list[tuple[Pattern, Handler]] = []
        self._guards: list[tuple[Pattern, Guard]] = []
        self._installed: dict[Pattern, Handler] = {}  # pattern -> the real handler registered for it

    # ---- the context surface an owner uses (as on a Playwright BrowserContext) ------------

    async def route(self, pattern: Pattern, handler: Handler) -> None:
        self._owners.append((pattern, handler))
        try:
            await self._ensure(pattern)
        except BaseException:
            self._owners.remove((pattern, handler))
            raise

    async def unroute(self, pattern: Pattern, handler: Handler | None = None) -> None:
        self._owners = [(p, h) for p, h in self._owners if not (p == pattern and (handler is None or h == handler))]
        await self._release(pattern)

    def on(self, event: str, fn: Callable[..., Any]) -> None:
        self._ctx.on(event, fn)

    def remove_listener(self, event: str, fn: Callable[..., Any]) -> None:
        self._ctx.remove_listener(event, fn)

    async def cookies(self, urls: Any = None) -> Any:
        return await self._ctx.cookies(urls) if urls is not None else await self._ctx.cookies()

    @property
    def context(self) -> Any:
        return self._ctx

    # ---- guards --------------------------------------------------------------------------

    async def add_guard(self, guard: Guard, pattern: Pattern = GUARD_PATTERN) -> None:
        entry = (pattern, guard)
        self._guards.append(entry)
        try:
            await self._ensure(pattern)
        except BaseException:
            self._guards.remove(entry)
            raise

    async def remove_guard(self, guard: Guard, pattern: Pattern = GUARD_PATTERN) -> None:
        with contextlib.suppress(ValueError):
            self._guards.remove((pattern, guard))
        await self._release(pattern)

    def patterns(self) -> list[Pattern]:
        """The routes installed on the context right now (none: the browser is untouched)."""
        return list(self._installed)

    # ---- installation --------------------------------------------------------------------

    def _wanted(self, pattern: Pattern) -> bool:
        if any(p == pattern for p, _ in self._guards):
            return True
        return any(p == pattern for p, _ in self._owners)

    async def _ensure(self, pattern: Pattern) -> None:
        if pattern in self._installed:
            return

        async def handler(route: Any, request: Any) -> None:
            await self._dispatch(route, request)

        self._installed[pattern] = handler
        try:
            await self._ctx.route(pattern, handler)
        except BaseException:
            self._installed.pop(pattern, None)
            raise

    async def _release(self, pattern: Pattern) -> None:
        if self._wanted(pattern):
            return
        handler = self._installed.pop(pattern, None)
        if handler is not None:
            with contextlib.suppress(Exception):
                await self._ctx.unroute(pattern, handler)

    # ---- dispatch ------------------------------------------------------------------------

    async def _dispatch(self, route: Any, request: Any) -> None:
        for guard in list(dict.fromkeys(g for _, g in self._guards)):  # a guard on two patterns runs once
            try:
                if await guard(route, request):
                    return
            except Exception:  # fail closed: a guard that cannot judge a request must not let it go
                log.warning("a route guard failed; the request is aborted", exc_info=True)
                with contextlib.suppress(Exception):
                    await route.abort("blockedbyclient")
                return
        url = getattr(request, "url", "")
        for pattern, handler in list(self._owners):
            if not pattern_match(pattern, url):
                continue
            step = _Step(route)
            await handler(step, request)
            if not step.passed:
                return
        with contextlib.suppress(Exception):
            await route.continue_()
