"""The signing route against fake Playwright objects: what it signs, what it leaves to the browser,
the redirect detector, the provider's refusals, and the engine's installation (none when no host)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from openajar.core.config import NodeConfig
from openajar.node.camoufox.engine import CamoufoxEngine
from openajar.webbotauth import keys, settings
from openajar.webbotauth.keys import IdentityError
from openajar.webbotauth.route import IdentityProvider, LaneSigner, filter_accept_encoding
from openajar.webbotauth.settings import LaneIdentity
from openajar.webbotauth.trace import IdentityTrace
from openajar.webbotauth.verify import verify_request

LANE = "acme/research"
SIGNED = "https://shop.example"
AGENT = "https://agents.example"
BROWSER_HEADERS = [("Host", "shop.example"), ("User-Agent", "Mozilla/5.0 (X11; Linux x86_64; rv:158.0)"),
                   ("Accept", "text/html"), ("Accept-Language", "en-US,en;q=0.5"),
                   ("Accept-Encoding", "gzip, deflate, br, zstd"), ("Cookie", "sid=1"),
                   ("Sec-Fetch-Site", "same-origin")]


class Frame:
    def __init__(self, url: str, parent: Frame | None = None):
        self.url, self.parent_frame = url, parent


class Request:
    def __init__(self, url: str, *, method: str = "GET", rtype: str = "document", headers=None, frame=None,
                 nav: bool = False, redirected_from: Request | None = None, sent: dict | None = None):
        self.url, self.method, self.resource_type = url, method, rtype
        self._headers = BROWSER_HEADERS if headers is None else headers
        self._frame = Frame(SIGNED + "/page") if frame is None else frame
        self._nav = nav
        self.redirected_from = redirected_from
        self._sent = sent if sent is not None else {k.lower(): v for k, v in self._headers}
        self.headers = dict(self._sent)

    @property
    def frame(self):
        if self._frame == "worker":
            raise RuntimeError("service worker request")
        return self._frame

    def is_navigation_request(self) -> bool:
        return self._nav

    async def headers_array(self):
        return [{"name": k, "value": v} for k, v in self._headers]

    async def all_headers(self):
        return dict(self._sent)


class Response:
    def __init__(self, status: int = 200, headers: dict | None = None):
        self.status, self.headers = status, headers or {}


class Route:
    def __init__(self, response: Response | Exception | None = None):
        self.response = response or Response()
        self.calls: list[tuple[str, Any]] = []

    async def fallback(self):
        self.calls.append(("fallback", None))

    async def fetch(self, **kw):
        self.calls.append(("fetch", kw))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def fulfill(self, response):
        self.calls.append(("fulfill", response))

    async def abort(self, code="failed"):
        self.calls.append(("abort", code))

    def kinds(self) -> list[str]:
        return [k for k, _ in self.calls]

    def sent(self) -> dict[str, str]:
        return next(kw for k, kw in self.calls if k == "fetch")["headers"]


class Context:
    def __init__(self, cookies: list | None = None):
        self.routes: list[tuple[str, Any]] = []
        self.listeners: dict[str, list] = {}
        self._cookies = cookies or []
        self.cookie_calls = 0

    async def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    async def unroute(self, pattern, handler=None):
        self.routes = [r for r in self.routes if r[0] != pattern]

    def on(self, event, cb):
        self.listeners.setdefault(event, []).append(cb)

    def remove_listener(self, event, cb):
        self.listeners.get(event, []).remove(cb)

    async def cookies(self, urls):
        self.cookie_calls += 1
        return list(self._cookies)


def make_signer(tmp_path, *, origins=(SIGNED,), proxied=True, resolve=None, cookies=None, form="bare"):
    key = keys.create_key(tmp_path / "secrets", LANE)
    ident = LaneIdentity(lane_id=LANE, signature_agent=AGENT, form=form)
    for o in origins:
        settings.add_host(ident, o, reason="test", by="owner:cli")
    events: list[tuple[str, str, dict]] = []
    trace = IdentityTrace(tmp_path / "trace.jsonl")
    signer = LaneSigner(key, ident, proxied=proxied, trace=trace, events=lambda *a: events.append(a),
                        resolve=resolve)
    return signer, key, events, trace


async def installed(tmp_path, **kw):
    signer, key, events, trace = make_signer(tmp_path, **kw)
    ctx = Context(cookies=kw.get("cookies"))
    await signer.install(ctx)
    return signer, key, events, trace, ctx


async def test_no_hosts_installs_nothing(tmp_path):
    signer, *_ = make_signer(tmp_path, origins=())
    ctx = Context()
    assert await signer.install(ctx) == []
    assert ctx.routes == [] and ctx.listeners == {}


async def test_one_glob_per_origin_and_a_request_listener(tmp_path):
    signer, *_ = make_signer(tmp_path, origins=("https://b.example", "http://127.0.0.1:6351"))
    ctx = Context()
    assert await signer.install(ctx) == ["http://127.0.0.1:6351/**", "https://b.example/**"]
    assert [p for p, _ in ctx.routes] == ["http://127.0.0.1:6351/**", "https://b.example/**"]
    assert len(ctx.listeners["request"]) == 1
    await signer.stop("test")
    assert ctx.routes == [] and ctx.listeners["request"] == []


async def test_a_signed_request_keeps_the_browser_headers_and_appends_the_signature(tmp_path):
    signer, key, _, trace, _ = await installed(tmp_path)
    headers = [*BROWSER_HEADERS, ("signature-agent", '"https://evil.example"')]
    route = Route(Response(302, {"location": "https://elsewhere.example/x"}))
    await signer._handle(route, Request(SIGNED + "/a/b?token=secret", headers=headers))
    assert route.kinds() == ["fetch", "fulfill"]
    fetch = route.calls[0][1]
    assert fetch["max_redirects"] == 0
    sent = fetch["headers"]
    assert list(sent)[:7] == [k for k, _ in BROWSER_HEADERS]
    assert list(sent)[7:] == ["Signature-Agent", "Signature-Input", "Signature"]
    assert sent["Accept-Encoding"] == "gzip, deflate, br"
    assert sent["Signature-Agent"] == f'"{AGENT}"'
    result = verify_request("GET", SIGNED + "/a/b?token=secret", sent, {key.keyid: key.public_key()}, nonce_bytes=64)
    assert result.valid, result.reason
    assert route.calls[1][1] is route.response
    rows = trace.read()
    assert [r["kind"] for r in rows] == ["signed"]
    row = rows[0]
    assert row["origin"] == SIGNED and row["path"] == "/a/b" and row["status"] == 302
    assert row["location_origin"] == "https://elsewhere.example" and row["keyid"] == key.keyid
    text = trace.path.read_text()
    assert sent["Signature"].split("=", 1)[1].strip(":") not in text
    assert "secret" not in text and "Signature" not in text
    assert signer.stats["signed"] == 1


async def test_dictionary_form_when_the_owner_picks_it(tmp_path):
    signer, key, _, _, _ = await installed(tmp_path, form="dict")
    route = Route()
    await signer._handle(route, Request(SIGNED + "/"))
    assert route.sent()["Signature-Agent"] == f'sig1="{AGENT}"'
    assert verify_request("GET", SIGNED + "/", route.sent(), {key.keyid: key.public_key()}).valid


async def test_an_unlisted_origin_is_never_signed(tmp_path):
    signer, *_ = await installed(tmp_path)
    for url in ("https://other.example/", "http://shop.example/", "https://shop.example:8443/"):
        route = Route()
        await signer._handle(route, Request(url))
        assert route.kinds() == ["fallback"]


@pytest.mark.parametrize(("request_kw", "why"), [
    ({"rtype": "media"}, "streamed"),
    ({"rtype": "eventsource"}, "streamed"),
    ({"rtype": "websocket"}, "streamed"),
    ({"frame": "worker"}, "service worker"),
    ({"frame": Frame("https://news.example/")}, "third-party"),
    ({"frame": Frame(SIGNED + "/x", Frame("https://news.example/"))}, "third-party"),
    ({"nav": True, "frame": Frame("about:blank", Frame("https://news.example/"))}, "third-party"),
    ({"headers": [("Host", "evil.example"), ("Cookie", "a=1")]}, "Host header"),
])
async def test_requests_left_to_the_browser_unsigned(tmp_path, request_kw, why):
    signer, _, _, trace, _ = await installed(tmp_path)
    route = Route()
    await signer._handle(route, Request(SIGNED + "/r", **request_kw))
    assert route.kinds() == ["fallback"]
    row = trace.read()[-1]
    assert row["kind"] == "unsigned" and why in row["why"]
    assert signer.stats["unsigned"] == 1


async def test_first_party_contexts_are_signed(tmp_path):
    signer, *_ = await installed(tmp_path)
    for kw in ({"nav": True, "frame": Frame("https://news.example/")},       # a top-level navigation in
               {"frame": Frame(SIGNED + "/a", Frame(SIGNED + "/b"))},          # a same-origin iframe
               {"frame": Frame("about:blank", Frame(SIGNED + "/b"))}):         # an about:blank child
        route = Route()
        await signer._handle(route, Request(SIGNED + "/r", **kw))
        assert route.kinds() == ["fetch", "fulfill"], kw


async def test_withheld_cookies_leave_the_request_unsigned(tmp_path):
    no_cookie = [h for h in BROWSER_HEADERS if h[0] != "Cookie"]
    signer, _, _, trace, ctx = await installed(tmp_path, cookies=[{"name": "strict", "value": "1"}])
    route = Route()
    await signer._handle(route, Request(SIGNED + "/i.png", rtype="image", headers=no_cookie))
    assert route.kinds() == ["fallback"] and "withheld cookies" in trace.read()[-1]["why"]
    signer2, _, _, _, ctx2 = await installed(tmp_path / "2")
    route = Route()
    await signer2._handle(route, Request(SIGNED + "/i.png", rtype="image", headers=no_cookie))
    assert route.kinds() == ["fetch", "fulfill"] and ctx2.cookie_calls == 1
    route = Route()
    await signer._handle(route, Request(SIGNED + "/i.png", rtype="image"))  # the browser sent its Cookie
    assert route.kinds() == ["fetch", "fulfill"] and ctx.cookie_calls == 1


async def test_a_lane_without_a_proxy_signs_for_public_hosts_only(tmp_path):
    answers = {"shop.example": ["10.1.2.3"]}
    calls = []

    async def resolve(host):
        calls.append(host)
        return answers[host]

    signer, _, _, trace, _ = await installed(tmp_path, proxied=False, resolve=resolve)
    route = Route()
    await signer._handle(route, Request(SIGNED + "/"))
    assert route.kinds() == ["fallback"] and "private address" in trace.read()[-1]["why"]
    route = Route()
    await signer._handle(route, Request(SIGNED + "/again"))
    assert route.kinds() == ["fallback"] and calls == ["shop.example"]  # cached
    signer._dns.clear()
    answers["shop.example"] = ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"]
    route = Route()
    await signer._handle(route, Request(SIGNED + "/"))
    assert route.kinds() == ["fetch", "fulfill"]
    proxied, *_ = await installed(tmp_path / "p", proxied=True, resolve=resolve)
    calls.clear()
    route = Route()
    await proxied._handle(route, Request(SIGNED + "/"))
    assert route.kinds() == ["fetch", "fulfill"] and calls == []


async def test_a_network_error_aborts_like_the_browser_would(tmp_path):
    signer, _, _, trace, _ = await installed(tmp_path)
    route = Route(ConnectionError("connection refused"))
    await signer._handle(route, Request(SIGNED + "/"))
    assert route.kinds() == ["fetch", "abort"]
    assert trace.read()[-1]["error"] == "connection refused"


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_the_detector_records_hops_off_a_signed_host(tmp_path):
    signer, _, events, trace, ctx = await installed(tmp_path)
    listener = ctx.listeners["request"][0]
    first = Request(SIGNED + "/redir")
    listener(Request("https://elsewhere.example/landing", redirected_from=first, sent={"host": "elsewhere.example"}))
    listener(Request("https://elsewhere.example/landing2", redirected_from=first, sent={"host": "elsewhere.example"}))
    listener(Request(SIGNED + "/target", redirected_from=first, sent={"host": "shop.example"}))
    listener(Request("https://x.example/", redirected_from=Request("https://y.example/")))  # not ours
    listener(Request(SIGNED + "/plain"))  # not a hop
    await settle()
    hops = [r for r in trace.read() if r["kind"] == "redirect"]
    assert [(h["to_origin"], h["left_host"], h["carried_signature"]) for h in hops] == [
        ("https://elsewhere.example", True, False), ("https://elsewhere.example", True, False),
        (SIGNED, False, False)]
    assert events == [(LANE, "identity_redirect_off_host",
                       {"from": SIGNED, "to": "https://elsewhere.example", "carried_signature": False})]
    assert signer.stats["redirects"] == 3 and signer.stats["redirects_off_host"] == 2 and not signer.stopped


async def test_a_hop_that_carries_the_signature_stops_signing(tmp_path):
    signer, _, events, trace, ctx = await installed(tmp_path)
    leaked = {"host": "shop.example", "signature": "sig1=:AAAA:", "signature-input": "sig1=()"}
    ctx.listeners["request"][0](Request("https://elsewhere.example/", redirected_from=Request(SIGNED + "/r"),
                                        sent=leaked))
    await settle()
    assert signer.stopped and ctx.routes == []
    assert [e[1] for e in events] == ["identity_redirect_leak", "identity_signing_stopped"]
    assert trace.read()[-1]["carried_signature"] is True
    route = Route()
    await signer._handle(route, Request(SIGNED + "/"))
    assert route.kinds() == ["fallback"]


def test_accept_encoding_filter():
    assert filter_accept_encoding("gzip, deflate, br, zstd") == "gzip, deflate, br"
    assert filter_accept_encoding("zstd") is None
    assert filter_accept_encoding("br;q=1.0, identity;q=0.5") == "br;q=1.0, identity;q=0.5"


# ---- the provider ----------------------------------------------------------------------

@pytest.fixture
def node(tmp_path):
    config = NodeConfig(home=tmp_path / "home")
    profile = config.profile_dir(LANE)
    profile.mkdir(parents=True)
    return config, profile


def setup_identity(config, profile, *, hosts=(SIGNED,)) -> LaneIdentity:
    key = keys.create_key(config.secrets_dir, LANE)
    ident = LaneIdentity(lane_id=LANE, signature_agent=AGENT, keyid=key.keyid,
                         profile=settings.profile_binding(profile))
    for h in hosts:
        settings.add_host(ident, h, reason="test", by="owner:cli")
    settings.save(config.secrets_dir, ident)
    return ident


def test_provider_gives_nothing_without_hosts(node):
    config, profile = node
    provider = IdentityProvider(config)
    assert provider.signer_for(LANE, profile_dir=profile, proxied=False) is None
    setup_identity(config, profile, hosts=())
    assert provider.signer_for(LANE, profile_dir=profile, proxied=False) is None


def test_provider_builds_the_signer(node):
    config, profile = node
    setup_identity(config, profile)
    signer = IdentityProvider(config).signer_for(LANE, profile_dir=profile, proxied=True)
    assert signer is not None and signer.origins == {SIGNED} and signer.proxied
    assert signer.trace.path == config.lane_dir(LANE) / "identity-trace.jsonl"
    assert signer.summary()["covered"] == ["@authority", "@method", "signature-agent"]


def test_provider_refuses_another_profile(node, tmp_path):
    config, profile = node
    setup_identity(config, profile)
    profile.rename(tmp_path / "old-profile")
    profile.mkdir()
    with pytest.raises(IdentityError, match="init acme/research --force"):
        IdentityProvider(config).signer_for(LANE, profile_dir=profile, proxied=False)


def test_provider_refuses_a_missing_or_swapped_key(node):
    config, profile = node
    setup_identity(config, profile)
    keys.create_key(config.secrets_dir, LANE, replace=True)
    with pytest.raises(IdentityError, match="does not match"):
        IdentityProvider(config).signer_for(LANE, profile_dir=profile, proxied=False)
    keys.delete_key(config.secrets_dir, LANE)
    with pytest.raises(IdentityError, match="no key"):
        IdentityProvider(config).signer_for(LANE, profile_dir=profile, proxied=False)


# ---- the engine installs it, and only then --------------------------------------------------

class FakeSigner:
    def __init__(self):
        self.installed_on = None

    async def install(self, context):
        self.installed_on = context
        return ["https://shop.example/**"]

    def summary(self):
        return {"hosts": [SIGNED]}


class FakeProvider:
    def __init__(self, result):
        self.result = result
        self.calls: list[dict] = []

    def signer_for(self, lane_id, **kw):
        self.calls.append({"lane": lane_id, **kw})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def session():
    return SimpleNamespace(context=object(), pin_notes=[("fingerprint_pinned", {})], signer="unset")


async def test_engine_without_a_provider_installs_nothing(tmp_path):
    s = session()
    await CamoufoxEngine()._install_identity(LANE, s, profile_dir=tmp_path, proxied=False)
    assert s.signer is None and s.pin_notes == [("fingerprint_pinned", {})]


async def test_engine_with_no_signer_installs_nothing(tmp_path):
    provider = FakeProvider(None)
    s = session()
    await CamoufoxEngine(identity=provider)._install_identity(LANE, s, profile_dir=tmp_path, proxied=True)
    assert s.signer is None and len(s.pin_notes) == 1
    assert provider.calls == [{"lane": LANE, "profile_dir": tmp_path, "proxied": True, "allow_private": False}]


async def test_engine_installs_the_signer_and_notes_it(tmp_path):
    signer = FakeSigner()
    s = session()
    engine = CamoufoxEngine(identity=FakeProvider(signer))
    engine.allow_private_network = True
    await engine._install_identity(LANE, s, profile_dir=tmp_path, proxied=False)
    assert s.signer is signer and signer.installed_on is s.context
    assert s.pin_notes[-1] == ("identity_signing", {"hosts": [SIGNED]})


@pytest.mark.parametrize("error", [IdentityError("bad mode"), RuntimeError("boom")])
async def test_engine_runs_unsigned_when_the_identity_is_broken(tmp_path, error):
    s = session()
    await CamoufoxEngine(identity=FakeProvider(error))._install_identity(LANE, s, profile_dir=tmp_path,
                                                                         proxied=False)
    assert s.signer is None
    assert s.pin_notes[-1][0] == "identity_error" and json.dumps(s.pin_notes[-1][1])


async def test_a_signing_failure_leaves_the_request_to_the_browser(tmp_path, monkeypatch):
    signer, key, _, trace, _ = await installed(tmp_path)

    def broken(**kw):
        raise ValueError("bad")

    monkeypatch.setattr(signer, "key", SimpleNamespace(sign_request=broken, keyid=key.keyid))
    route = Route()
    await signer._handle(route, Request(SIGNED + "/"))
    assert route.kinds() == ["fallback"] and "could not prepare" in trace.read()[-1]["why"]
