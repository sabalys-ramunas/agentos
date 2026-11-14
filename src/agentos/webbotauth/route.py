"""The signing route of a browser lane, and the redirect guard (design sections 4, 5.4 and 12).

`CamoufoxEngine.launch` asks `IdentityProvider.signer_for(lane)` for a `LaneSigner` and installs
it only when the lane's `sign_hosts` list is non-empty: one Playwright glob per origin, never a
callable matcher, because Firefox pauses every request of the context once any route exists
(Phase 0: +10 to 16 ms per navigation on every host).

**How a request is signed (redirect-safe delivery).** Phase 0 found that `route.continue_(headers=)`
is unsafe: Firefox follows a redirect without calling the handler again and reuses the whole
overridden header set on the next hop, `Host` and the live `Signature` included, so a signed
host that redirects elsewhere hands a valid signature to a third party. This route therefore
never overrides headers on a request the browser sends. For a request to a signed origin it:

1. takes the browser's own header set (`headers_array()`, names keep their case), drops any
   `Signature*` header a page set, and appends the three Web Bot Auth headers;
2. sends exactly that one request from the Playwright driver with `route.fetch(max_redirects=0)`,
   through the lane's proxy and with the browser's cookies (measured on the node);
3. hands the response, a 3xx included, back to the browser with `route.fulfill`.

The browser then follows a redirect itself, as a new request that carries only its own headers.
Measured on Camoufox 158 (tests/node/test_identity_node.py): the hop to another host arrives with
its own `Host` and no signature, and a hop back to a signed host is not routed, so it goes unsigned
instead of with a stale signature. The costs, all limited to signed origins: the TLS and HTTP
client of these requests is the driver's (Node), not Firefox's; header order is the driver's;
responses are buffered, so streamed types (media, event streams, websockets) are left to the
browser unsigned; and while any route exists the browser stores no response of the lane in its
HTTP cache, signed or not (measured, docs/design/egress-guard.md). The engine installs this route
through the session's RouteDispatcher, behind the egress guard.

**Never more than the browser would send.** The driver keeps no partitioned cookie jar and no
SameSite rules, so a request is signed only where its cookies are unambiguous: a top-level
navigation, or a request whose frames all belong to the signed origin. A request the browser sent
without cookies while the lane holds some for that URL (SameSite, partitioning) is left unsigned.
On a lane without a proxy the driver skips the private-network PAC, so a signed origin must
resolve to public addresses only. Each refusal falls back to the browser, unsigned, and is traced.

**The detector.** A `request` listener watches every redirect hop whose previous hop was on a
signed origin. It writes a `redirect` trace row (from and to origin, whether the hop left the
host, whether it carried a signature), records a lane event the first time a signed origin
redirects to another host, and if a hop off the host ever carries a signature (a regression in
Playwright or Camoufox) it logs an error and stops signing for the lane at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agentos.core.config import NodeConfig
from agentos.core.text import first_line
from agentos.webbotauth import base, keys, settings
from agentos.webbotauth.keys import IdentityError, LaneKey
from agentos.webbotauth.settings import LaneIdentity
from agentos.webbotauth.trace import IdentityTrace

log = logging.getLogger("agentos.webbotauth")

# What the driver signs and fetches. Streams stay with the browser (route.fetch buffers bodies).
SIGNABLE_TYPES = frozenset({"document", "stylesheet", "image", "font", "script", "xhr", "fetch", "manifest",
                            "texttrack", "other"})
DECODABLE = ("gzip", "x-gzip", "deflate", "br", "identity")  # what the driver can decode for the browser
FETCH_TIMEOUT_MS = 90_000
DNS_TTL = 60.0
TRACE_NAME = "identity-trace.jsonl"

Events = Callable[[str, str, dict[str, Any]], None]


def _path_of(url: str) -> str:
    return urlsplit(url).path or "/"


def filter_accept_encoding(value: str) -> str | None:
    """Keep the codings the driver decodes (Firefox also offers zstd, which it does not)."""
    kept = [t.strip() for t in value.split(",") if t.split(";", 1)[0].strip().lower() in DECODABLE]
    return ", ".join(kept) or None


def _is_public(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_global and not ip.is_multicast


class LaneSigner:
    """Signs a lane's requests to its `sign_hosts` origins. See the module docstring."""

    def __init__(self, key: LaneKey, ident: LaneIdentity, *, proxied: bool, allow_private: bool = False,
                 trace: IdentityTrace | None = None, events: Events | None = None,
                 clock: Callable[[], float] = time.time,
                 resolve: Callable[[str], Any] | None = None,
                 secrets_dir: Path | None = None):
        if not ident.signature_agent:
            raise IdentityError(f"{ident.lane_id} has no signature agent URL")
        self.key = key
        self.lane_id = ident.lane_id
        self.origins = frozenset(ident.origins)   # what the routes cover (fixed at launch)
        self._ident = ident
        # `agentos identity hosts remove` runs in another process: the signer re-reads the
        # lane's settings when the file changes, so a removed host stops being signed at once
        # (an added one waits for the next launch, which installs its route).
        self._settings = settings.settings_path(secrets_dir, ident.lane_id) if secrets_dir else None
        self._secrets_dir = secrets_dir
        self._stamp = self._settings_stamp()
        self.agent = ident.signature_agent
        self.form = ident.form
        self.components = base.COMPONENTS
        self.window = base.WINDOW
        self.proxied = proxied
        self.allow_private = allow_private
        self.trace = trace
        self.events = events
        self.clock = clock
        self._resolve = resolve
        self._context: Any = None
        self._patterns: list[str] = []
        self._dns: dict[str, tuple[float, str | None]] = {}
        self._announced: set[tuple[str, str]] = set()
        self._tasks: set[asyncio.Task] = set()
        self.stopped = False
        self.stats: dict[str, Any] = {"signed": 0, "unsigned": 0, "redirects": 0, "redirects_off_host": 0,
                                      "last_signed_at": None}

    # ---- the hosts in force ---------------------------------------------------------------

    def _settings_stamp(self) -> tuple[int, int] | None:
        if self._settings is None:
            return None
        try:
            st = self._settings.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def signing_origins(self) -> frozenset[str]:
        """The origins this signer may sign for right now: those its routes cover that the
        lane's settings still list. A missing or broken settings file stops all signing."""
        if self._settings is not None:
            stamp = self._settings_stamp()
            if stamp != self._stamp:
                self._stamp = stamp
                try:
                    loaded = settings.load(self._secrets_dir, self.lane_id) if stamp else None
                except IdentityError:
                    loaded = None
                if loaded is None:
                    self.stopped = True
                    self._event("identity_signing_stopped", {"reason": "the lane's identity settings are gone"})
                    return frozenset()
                self._ident = loaded
        return self.origins & frozenset(self._ident.origins)

    # ---- installation ------------------------------------------------------------------

    def patterns(self) -> list[str]:
        return [f"{o}/**" for o in sorted(self.origins)]

    def summary(self) -> dict[str, Any]:
        return {"hosts": sorted(self.origins), "keyid": self.key.keyid, "signature_agent": self.agent,
                "form": self.form, "covered": list(self.components), "window": self.window,
                "delivery": "driver fetch, browser follows redirects"}

    async def install(self, context: Any) -> list[str]:
        """Route each origin. Nothing at all when there are none (any route slows the lane)."""
        if not self.origins or self.stopped:
            return []
        self._context = context  # before the first route: a request may arrive at once
        try:
            for pat in self.patterns():
                await context.route(pat, self._handle)
                self._patterns.append(pat)
            context.on("request", self._on_request)
        except BaseException:
            await self._unroute(context)
            self._context = None
            raise
        return list(self._patterns)

    async def _unroute(self, context: Any) -> None:
        for pat in self._patterns:
            with contextlib.suppress(Exception):
                await context.unroute(pat, self._handle)
        self._patterns = []
        with contextlib.suppress(Exception):
            context.remove_listener("request", self._on_request)

    async def stop(self, reason: str) -> None:
        """Stop signing now: no request after this one is signed."""
        self.stopped = True
        if self._context is not None:
            await self._unroute(self._context)
        self._event("identity_signing_stopped", {"reason": reason})

    # ---- the route -----------------------------------------------------------------------

    async def _handle(self, route: Any, request: Any) -> None:
        url = request.url
        try:
            origin = base.origin_of(url)
        except ValueError:
            origin = None
        if self.stopped or origin not in self.signing_origins():
            await route.fallback()
            return
        signed = None
        try:
            headers, why = await self._prepare(request, url, origin)
            if headers is not None and (self.stopped or origin not in self.signing_origins()):
                headers, why = None, "signing stopped while the request was being prepared"
            if headers is not None:
                signed = self.key.sign_request(method=request.method, url=url, signature_agent=self.agent,
                                               form=self.form, components=self.components, window=self.window,
                                               now=self.clock())
        except Exception as e:  # noqa: BLE001 - anything unexpected: the browser sends it, unsigned
            headers, why = None, f"could not prepare the request: {type(e).__name__}"
        if headers is None or signed is None:
            self._unsigned(request, origin, why or "refused")
            await route.fallback()
            return
        headers.update(signed.headers)
        row = {"kind": "signed", "lane": self.lane_id, "method": request.method, "origin": origin,
               "path": _path_of(url), "type": request.resource_type, "agent": self.agent, **signed.record()}
        try:
            response = await route.fetch(headers=headers, max_redirects=0, timeout=FETCH_TIMEOUT_MS)
        except Exception as e:  # noqa: BLE001 - a network error, as the browser would have seen it
            self._write({**row, "status": None, "error": first_line(e)})
            with contextlib.suppress(Exception):
                await route.abort("failed")
            return
        row["status"] = response.status
        if 300 <= response.status < 400:
            row["location_origin"] = _location_origin(url, response.headers.get("location"))
        self.stats["signed"] += 1
        self.stats["last_signed_at"] = self.clock()
        self._write(row)
        try:
            await route.fulfill(response=response)
        except Exception as e:  # noqa: BLE001 - the page went away while the response was on its way
            log.debug("%s: fulfilling %s failed: %s", self.lane_id, origin, first_line(e))

    async def _prepare(self, request: Any, url: str, origin: str) -> tuple[dict[str, str] | None, str | None]:
        """The header set to sign and send, or (None, why) when this request must go unsigned."""
        if request.resource_type not in SIGNABLE_TYPES:
            return None, f"{request.resource_type} requests are streamed; the browser sends them"
        why = self._context_problem(request, origin)
        if why:
            return None, why
        headers: dict[str, str] = {}
        for h in await request.headers_array():
            name, value = h["name"], h["value"]
            low = name.lower()
            if low in base.SIGNATURE_HEADERS:
                continue  # a page cannot choose what this lane signs
            if low == "accept-encoding":
                filtered = filter_accept_encoding(value)
                if filtered is None:
                    continue
                value = filtered
            if name in headers:
                headers[name] = f"{headers[name]}, {value}"
            else:
                headers[name] = value
        lowered = {k.lower(): v for k, v in headers.items()}
        if "host" in lowered and lowered["host"].strip().lower() != base.authority_of(url):
            return None, "the Host header does not match the URL"
        if "cookie" not in lowered and self._context is not None and await self._context.cookies([url]):
            return None, "the browser withheld cookies the lane holds for this URL"
        if not self.proxied and not self.allow_private:
            why = await self._public_only(urlsplit(url).hostname or "")
            if why:
                return None, why
        return headers, None

    def _context_problem(self, request: Any, origin: str) -> str | None:
        """None when this is a first-party request of the signed origin (see the module docstring)."""
        try:
            frame = request.frame
        except Exception:  # noqa: BLE001 - a service worker request has no frame
            return "a service worker request"
        if request.is_navigation_request():
            if frame.parent_frame is None:
                return None  # a top-level navigation is first-party for the new document
            frame = frame.parent_frame
        while frame is not None:
            furl = frame.url or ""
            if furl.startswith("data:"):
                return "a request from a data: frame (an opaque origin, not the signed one)"
            furl = furl.removeprefix("blob:")  # a blob URL carries the origin that made it
            if furl and not furl.startswith("about:"):  # about:blank and srcdoc inherit the parent's origin
                try:
                    if base.origin_of(furl) != origin:
                        return "a request from another origin's frame (third-party context)"
                except ValueError:
                    return "a frame with an unreadable URL"
            frame = frame.parent_frame
        return None

    async def _public_only(self, host: str) -> str | None:
        cached = self._dns.get(host)
        now = time.monotonic()
        if cached and now - cached[0] < DNS_TTL:
            return cached[1]
        why: str | None = None
        try:
            if self._resolve is not None:
                addrs = list(await self._resolve(host))
            else:
                infos = await asyncio.get_running_loop().getaddrinfo(host, None)
                addrs = [info[4][0] for info in infos]
        except OSError:
            addrs = []
        if not addrs:
            why = f"{host} does not resolve"
        elif not all(_is_public(a) for a in addrs):
            why = f"{host} resolves to a private address; a lane without a proxy signs for public hosts only"
        self._dns[host] = (now, why)
        return why

    # ---- the redirect detector -----------------------------------------------------------

    def _on_request(self, request: Any) -> None:
        prev = request.redirected_from
        if prev is None:
            return
        try:
            src, dst = base.origin_of(prev.url), base.origin_of(request.url)
        except ValueError:
            return
        if src not in self.origins:
            return
        task = asyncio.get_running_loop().create_task(self._judge_hop(request, src, dst))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _judge_hop(self, request: Any, src: str, dst: str) -> None:
        try:
            hop = await asyncio.wait_for(request.all_headers(), 10.0)
        except Exception:  # noqa: BLE001 - the hop failed or the page closed: use what was announced
            hop = {k.lower(): v for k, v in (request.headers or {}).items()}
        carried = any(h in hop for h in ("signature", "signature-input"))
        left = dst != src
        self.stats["redirects"] += 1
        row = {"kind": "redirect", "lane": self.lane_id, "from_origin": src, "to_origin": dst,
               "path": _path_of(request.url), "left_host": left, "carried_signature": carried,
               "type": request.resource_type}
        self._write(row)
        if left:
            self.stats["redirects_off_host"] += 1
            if carried:
                log.error("%s: a redirect from %s to %s carried this lane's signature; signing stops now",
                          self.lane_id, src, dst)
                self._event("identity_redirect_leak", {"from": src, "to": dst})
                await self.stop(f"a redirect hop to {dst} carried the signature")
            elif (src, dst) not in self._announced:
                self._announced.add((src, dst))
                self._event("identity_redirect_off_host", {"from": src, "to": dst, "carried_signature": False})

    # ---- records ---------------------------------------------------------------------------

    def _unsigned(self, request: Any, origin: str, why: str) -> None:
        self.stats["unsigned"] += 1
        self._write({"kind": "unsigned", "lane": self.lane_id, "method": request.method, "origin": origin,
                     "path": _path_of(request.url), "type": request.resource_type, "why": why})

    def _write(self, row: dict[str, Any]) -> None:
        if self.trace is not None:
            self.trace.write(row)

    def _event(self, kind: str, detail: dict[str, Any]) -> None:
        if self.events is None:
            return
        try:
            self.events(self.lane_id, kind, detail)
        except Exception:
            log.warning("%s: could not record a %s event", self.lane_id, kind, exc_info=True)


def _location_origin(url: str, location: str | None) -> str | None:
    if not location:
        return None
    from urllib.parse import urljoin

    try:
        return base.origin_of(urljoin(url, location))
    except ValueError:
        return None


class IdentityProvider:
    """What the engine asks at launch: the lane's signer, or None when it signs for nothing."""

    def __init__(self, config: NodeConfig, *, events: Events | None = None):
        self.config = config
        self.events = events

    def signer_for(self, lane_id: str, *, profile_dir: Any, proxied: bool,
                   allow_private: bool = False) -> LaneSigner | None:
        secrets_dir = self.config.secrets_dir
        ident = settings.load(secrets_dir, lane_id)
        if ident is None or not ident.hosts:
            return None
        if ident.profile is None or tuple(ident.profile) != settings.profile_binding(profile_dir):
            raise IdentityError(f"{lane_id}: the lane's profile is not the one its identity was made for "
                                f"(a recreated or imported lane); run: agentos identity init {lane_id} --force")
        key = keys.load_key(secrets_dir, lane_id)
        if key is None:
            raise IdentityError(f"{lane_id} has hosts to sign for but no key; run: agentos identity init {lane_id}")
        if ident.keyid and ident.keyid != key.keyid:
            raise IdentityError(f"{lane_id}: the key file does not match the identity settings")
        trace = IdentityTrace(self.config.lane_dir(lane_id) / TRACE_NAME)
        return LaneSigner(key, ident, proxied=proxied, allow_private=allow_private, trace=trace, events=self.events,
                          secrets_dir=secrets_dir)
