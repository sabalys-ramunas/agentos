"""`agentos expose` and `agentos forward` (agentos.node.expose): the reserved names, the lane's route,
the registry's ends (hard end, lease, the owner), the egress guard's view of an exposed host, the API's
refusal of every credential an agent can hold, and the action log. The real browser is in
tests/node/test_expose_node.py."""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_egress import AGENT, SITES, FakeContext, FakeFrame, FakeRequest, make_guard

from agentos.core.errors import Conflict, EgressRefused, InvalidRequest, LaneNotFound, NotFound
from agentos.node.egress import Driver, judge
from agentos.node.expose import names, relay
from agentos.node.expose.forward import Forward, lane_dialer
from agentos.node.expose.route import LaneExposeRoute
from agentos.node.expose.service import ExposeService
from agentos.node.routes import RouteDispatcher

LANE = "personal/linkedin"
OTHER = "personal/reddit"
HOST = "myapp.expose.agentos.invalid"
URL = f"https://{HOST}/"


# ---- fakes ---------------------------------------------------------------------------------

class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class FakeLanes:
    def __init__(self, kinds: dict[str, str] | None = None) -> None:
        self.kinds = kinds or {LANE: "browser", OTHER: "browser", "dev/web": "service"}
        self.sessions: dict[str, object] = {}
        self.events: list[tuple[str, str, dict]] = []
        self.listeners: list = []

    def get(self, lane_id: str):
        if lane_id not in self.kinds:
            raise LaneNotFound(f"no lane {lane_id}")
        return SimpleNamespace(spec=SimpleNamespace(id=lane_id, kind=self.kinds[lane_id]))

    def live_session(self, lane_id: str):
        return self.sessions.get(lane_id)

    def add_session_listener(self, fn) -> None:
        self.listeners.append(fn)

    def _event(self, lane_id: str, kind: str, detail: dict) -> None:
        self.events.append((lane_id, kind, detail))


class FakeTraces:
    def __init__(self) -> None:
        self.rows: list = []

    def write(self, event) -> None:
        self.rows.append(event)


class Runtime:
    def __init__(self, **services) -> None:
        self.services = services

    def get(self, name: str):
        return self.services.get(name)


def session_for(ctx: FakeContext | None = None):
    ctx = ctx or FakeContext()
    return SimpleNamespace(context=ctx, routes=RouteDispatcher(ctx), guard=SimpleNamespace(exposed=frozenset))


@pytest.fixture
def short_home():
    """A home with a short path: a unix socket path is at most 104 bytes on macOS."""
    path = Path(tempfile.mkdtemp(prefix="ax", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def svc(short_home):
    lanes, traces, clock = FakeLanes(), FakeTraces(), Clock()
    service = ExposeService(Runtime(lanes=lanes, traces=traces), home=short_home, clock=clock)
    service.dir.mkdir(parents=True)
    service.test = SimpleNamespace(lanes=lanes, traces=traces, clock=clock)
    return service


def bind(path: str) -> socket.socket:
    """What sshd does for `ssh -R <path>:...`: a listening unix socket, mode 600."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    s.listen(1)
    os.chmod(path, 0o600)
    return s


async def active_expose(svc: ExposeService, lane: str = LANE, name: str = "myapp", ttl_s: float | None = None):
    link = await svc.create_expose(lane, name=name, port=5173, ttl_s=ttl_s, by="owner on macbook")
    sock = bind(link["socket"])
    out = await svc.ready(link["id"])
    return link | out, sock


def run(coro):
    return asyncio.run(coro)


# ---- names ---------------------------------------------------------------------------------

def test_a_name_is_one_dns_label_under_the_reserved_suffix():
    assert names.host_for("MyApp") == HOST
    for bad in ("", "-x", "x-", "a.b", "a_b", "x" * 33, "../x", "a b"):
        with pytest.raises(InvalidRequest):
            names.validate_name(bad)
    assert names.name_of(HOST) == "myapp" and names.name_of("myapp.expose.agentos.invalid.evil.com") is None


@pytest.mark.parametrize("url", [URL, f"http://{HOST}/x", f"https://{HOST}:5173/", f"wss://{HOST}/hmr",
                                 f"https://{HOST}?q=1", f"https://{HOST.upper()}/"])
def test_the_route_pattern_takes_every_url_under_the_suffix(url):
    assert names.URL_RE.search(url)


@pytest.mark.parametrize("url", ["https://localhost:5173/", "http://127.0.0.1:5173/", "http://10.0.0.1/",
                                 f"https://{HOST}.evil.com/", f"https://evil.com/{HOST}/",
                                 f"https://{HOST}@evil.com/", "https://expose.agentos.invalid.evil.com/",
                                 f"https://evil.com/?next=https://{HOST}/"])
def test_the_route_pattern_never_takes_another_host(url):
    assert not names.URL_RE.search(url)


# ---- the egress guard ----------------------------------------------------------------------

def test_an_exposed_host_is_inside_the_lane_and_grant_lists_only_while_exposed():
    assert judge(URL, "GET", navigation=True, sites=SITES, driver=AGENT, human=False).rule == "lane_sites"
    assert judge(URL, "GET", navigation=True, sites=SITES, driver=AGENT, human=False, exposed={HOST}) is None
    assert judge(URL, "POST", navigation=False, sites=SITES, driver=AGENT, human=False, exposed={HOST}) is None
    only_grant = Driver(kind="agent", id="a", lists=(("linkedin.com",),), ceiling="commit")
    assert judge(URL, "GET", navigation=True, sites=(), driver=only_grant, human=False).rule == "grant_egress"
    assert judge(URL, "GET", navigation=True, sites=(), driver=only_grant, human=False, exposed={HOST}) is None
    # another exposed name is no wildcard
    other = "https://other.expose.agentos.invalid/"
    assert judge(other, "GET", navigation=True, sites=SITES, driver=AGENT, human=False, exposed={HOST}) is not None


def test_an_exposed_host_still_meets_the_effect_rules():
    from agentos.node.egress.rules import EffectRule

    rule = EffectRule(id="myapp.post", site="myapp", host=HOST, path="/api/post", effect="commit")
    refusal = judge(URL + "api/post", "POST", navigation=False, sites=SITES, driver=AGENT, human=False,
                    effect=rule, exposed={HOST})
    assert refusal is not None and refusal.rule == "commit"  # the grant may only draft


def test_the_guard_reads_the_exposed_hosts_live_and_fails_closed(tmp_path):
    guard, _ctx, _events, _clock = make_guard(tmp_path)
    with pytest.raises(EgressRefused):
        guard.check_url(URL)
    hosts = {HOST}
    guard.exposed = lambda: frozenset(hosts)
    guard.check_url(URL)  # no raise
    hosts.clear()  # the expose ended
    with pytest.raises(EgressRefused):
        guard.check_url(URL)

    def broken():
        raise RuntimeError("registry gone")

    guard.exposed = broken
    with pytest.raises(EgressRefused):
        guard.check_url(URL)


# ---- the registry --------------------------------------------------------------------------

def test_an_expose_routes_only_once_its_socket_is_up_and_only_in_its_lane(svc):
    lanes = svc.test.lanes
    session, other_session = session_for(), session_for()
    lanes.sessions = {LANE: session, OTHER: other_session}

    async def go():
        link = await svc.create_expose(LANE, name="myapp", port=5173, by="owner on macbook")
        assert link["state"] == "waiting" and link["host"] == HOST and link["url"] == URL
        assert svc.hosts(LANE) == frozenset() and session.routes.patterns() == []
        with pytest.raises(InvalidRequest, match="not on the node yet"):
            await svc.ready(link["id"])
        sock = bind(link["socket"])
        try:
            out = await svc.ready(link["id"])
            assert out["state"] == "active" and out["routed"] is True
            assert session.routes.patterns() == [names.URL_RE]
            assert other_session.routes.patterns() == []  # another lane gets no route
            assert svc.lookup(LANE, HOST) is not None and svc.lookup(OTHER, HOST) is None
            assert session.guard.exposed() == frozenset({HOST}) and svc.hosts(OTHER) == frozenset()
            notes = svc.lane_notes(LANE)
            assert notes[0]["url"] == URL and svc.lane_notes(OTHER) == []
        finally:
            sock.close()

    run(go())
    assert [r.tool for r in svc.test.traces.rows] == ["expose.start"]
    row = svc.test.traces.rows[0]
    assert row.lane == LANE and row.principal == "owner on macbook" and row.args["host"] == HOST
    assert ("expose_started" in [k for _, k, _ in lanes.events])


def test_a_socket_that_is_not_agentosds_is_refused(svc):
    svc.uid = os.geteuid() + 1  # sshd made it as someone else, and nobody handed it over

    async def go():
        link = await svc.create_expose(LANE, name="myapp", port=5173)
        sock = bind(link["socket"])
        try:
            with pytest.raises(InvalidRequest, match="belongs to uid"):
                await svc.ready(link["id"])
        finally:
            sock.close()
        assert svc.hosts(LANE) == frozenset()

    run(go())


def test_the_hard_end_holds_before_the_reaper_runs(svc):
    lanes, clock = svc.test.lanes, svc.test.clock
    session = session_for()
    lanes.sessions = {LANE: session}

    async def go():
        link, sock = await active_expose(svc, ttl_s=600)
        try:
            for _ in range(5):  # renewing never moves the hard end
                clock.now += 25
                svc.heartbeat(link["id"])
            clock.now = link_end(link) + 1
            assert svc.lookup(LANE, HOST) is None  # refused at once, before any reaper pass
            with pytest.raises(NotFound, match="time is up"):
                svc.heartbeat(link["id"])
            assert await svc.reap() == [link["id"]]
            assert session.routes.patterns() == []
            with pytest.raises(NotFound, match="time is up"):
                svc.heartbeat(link["id"])
        finally:
            sock.close()

    run(go())
    ends = [r for r in svc.test.traces.rows if r.tool == "expose.end"]
    assert len(ends) == 1 and ends[0].args["reason"] == "its time is up"


def link_end(link: dict) -> float:
    return datetime.strptime(link["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()


def test_a_link_the_computer_stops_renewing_ends_after_its_lease(svc):
    """Ctrl-C that never reached the node, a laptop asleep, the tunnel dropped: no heartbeat."""
    clock = svc.test.clock
    svc.test.lanes.sessions = {LANE: session_for()}

    async def go():
        link, sock = await active_expose(svc)
        try:
            clock.now += names.LEASE_S - 1
            svc.heartbeat(link["id"])
            clock.now += names.LEASE_S - 1
            assert svc.lookup(LANE, HOST) is not None
            clock.now += 2
            assert svc.lookup(LANE, HOST) is None
            await svc.reap()
            assert svc.links[link["id"]].state == "ended"
            with pytest.raises(NotFound, match="stopped renewing"):
                svc.heartbeat(link["id"])
        finally:
            sock.close()

    run(go())
    assert "stopped renewing" in svc.test.traces.rows[-1].args["reason"]


def test_an_expose_whose_forward_never_came_up_ends_quietly(svc):
    async def go():
        link = await svc.create_expose(LANE, name="myapp", port=5173)
        svc.test.clock.now += names.READY_WITHIN_S + 1
        assert await svc.reap() == [link["id"]]

    run(go())
    assert svc.test.traces.rows == []  # nothing was ever exposed


def test_the_owner_ends_it_and_the_route_goes(svc):
    session = session_for()
    svc.test.lanes.sessions = {LANE: session}

    async def go():
        link, sock = await active_expose(svc)
        second, sock2 = await active_expose(svc, name="api")
        try:
            await svc.end(link["id"])
            assert session.routes.patterns() == [names.URL_RE]  # the api expose still needs it
            assert svc.lookup(LANE, HOST) is None and svc.lookup(LANE, "api.expose.agentos.invalid")
            await svc.end(second["id"])
            assert session.routes.patterns() == [] and session.guard.exposed() == frozenset()
            assert not Path(link["socket"]).exists()
            with pytest.raises(NotFound, match="owner ended it"):
                svc.heartbeat(link["id"])
        finally:
            sock.close()
            sock2.close()

    run(go())


def test_a_lane_that_wakes_later_gets_the_route(svc):
    async def go():
        link, sock = await active_expose(svc)  # the lane is cold: nothing to route yet
        try:
            assert link["routed"] is False
            session, other = session_for(), session_for()
            await svc.on_session(LANE, session)
            await svc.on_session(OTHER, other)
            assert session.routes.patterns() == [names.URL_RE] and other.routes.patterns() == []
            assert other.guard.exposed() == frozenset()
            await svc.on_session(LANE, session)  # the same browser again: one route
            assert len(session.context.routes) == 1
        finally:
            sock.close()

    run(go())


def test_what_an_expose_refuses_to_start(svc):
    async def go():
        with pytest.raises(InvalidRequest, match="not a browser lane"):
            await svc.create_expose("dev/web", name="x", port=1)
        with pytest.raises(LaneNotFound):
            await svc.create_expose("nobody/here", name="x", port=1)
        with pytest.raises(InvalidRequest):
            await svc.create_expose(LANE, name="bad name", port=1)
        with pytest.raises(InvalidRequest):
            await svc.create_expose(LANE, name="x", port=70000)
        with pytest.raises(InvalidRequest, match="at most 12 hours"):
            await svc.create_expose(LANE, name="x", port=1, ttl_s=13 * 3600)
        await svc.create_expose(LANE, name="x", port=1)
        with pytest.raises(Conflict):
            await svc.create_expose(LANE, name="x", port=2)
        await svc.create_expose(OTHER, name="x", port=2)  # the same name on another lane is another link
        for i in range(names.MAX_PER_LANE - 1):
            await svc.create_expose(LANE, name=f"n{i}", port=3)
        with pytest.raises(Conflict, match="already has"):
            await svc.create_expose(LANE, name="one-more", port=4)

    run(go())


def test_shutdown_ends_everything(svc):
    async def go():
        _link, sock = await active_expose(svc)
        try:
            await svc.shutdown()
            assert svc.list() == [] and svc.hosts(LANE) == frozenset()
        finally:
            sock.close()

    run(go())
    assert svc.test.traces.rows[-1].args["reason"] == "the node stopped"


# ---- the lane's route ----------------------------------------------------------------------

class Route:
    def __init__(self) -> None:
        self.aborted: str | None = None
        self.fulfilled: dict | None = None

    async def abort(self, code: str = "failed") -> None:
        self.aborted = code

    async def fulfill(self, **kw) -> None:
        self.fulfilled = kw


class Req(FakeRequest):
    def __init__(self, url: str, method: str = "GET", *, all_headers: dict | None = None, **kw) -> None:
        super().__init__(url, method, **kw)
        self._all = all_headers

    async def all_headers(self):
        return dict(self._all if self._all is not None else self.headers)


def lane_route(lookup_hosts: dict[str, object], fetched: list, *, answer=None, fail: str | None = None,
               ctx: FakeContext | None = None) -> LaneExposeRoute:
    async def fetch(sock, method, host, target, headers, body):
        fetched.append(SimpleNamespace(sock=sock, method=method, host=host, target=target,
                                       headers=dict(headers), body=body))
        if fail:
            raise relay.RelayError(fail)
        return answer or relay.Answer(200, [("content-type", "text/html"), ("set-cookie", "a=1; Secure"),
                                            ("set-cookie", "b=2; HttpOnly"), ("transfer-encoding", "chunked")],
                                      b"<h1>hi</h1>")

    def lookup(lane, host):
        return lookup_hosts.get(host) if lane == LANE else None

    return LaneExposeRoute(LANE, lookup=lookup, context=ctx or FakeContext(), fetch=fetch)


EXPOSE = SimpleNamespace(socket=Path("/srv/agentos/run/expose/abc.sock"))


def test_a_navigation_is_answered_from_the_socket_with_cookies_both_ways():
    fetched: list = []
    r = lane_route({HOST: EXPOSE}, fetched)
    route = Route()
    req = Req(URL + "auth/callback?code=xyz", nav=True, frame=FakeFrame("https://www.linkedin.com/"),
              all_headers={"cookie": "sid=1", "user-agent": "Firefox", "connection": "keep-alive",
                           "x-forwarded-for": "6.6.6.6"})
    run(r.handle(route, req))
    assert route.aborted is None and route.fulfilled["status"] == 200
    assert route.fulfilled["headers"]["set-cookie"] == "a=1; Secure\nb=2; HttpOnly"
    assert "transfer-encoding" not in route.fulfilled["headers"]
    sent = fetched[0]
    assert sent.sock == EXPOSE.socket and sent.host == HOST and sent.target == "/auth/callback?code=xyz"
    assert sent.headers["host"] == HOST and sent.headers["x-forwarded-proto"] == "https"
    assert sent.headers["cookie"] == "sid=1" and "connection" not in sent.headers
    assert sent.headers["x-forwarded-host"] == HOST and "x-forwarded-for" not in sent.headers


@pytest.mark.parametrize("url", [
    "https://nobody.expose.agentos.invalid/",       # no such expose
    f"https://{HOST}:5173/",                         # another port: never another service
    f"https://{HOST}:22/",
    f"wss://{HOST}/hmr",                             # no WebSockets
])
def test_anything_but_an_active_exposes_exact_origin_does_not_resolve(url):
    fetched: list = []
    route = Route()
    run(lane_route({HOST: EXPOSE}, fetched).handle(route, Req(url, nav=True)))
    assert route.aborted == "namenotresolved" and fetched == [] and route.fulfilled is None


def test_another_lane_never_reaches_it():
    fetched: list = []
    r = lane_route({HOST: EXPOSE}, fetched)
    r.lane_id = OTHER
    route = Route()
    run(r.handle(route, Req(URL, nav=True)))
    assert route.aborted == "namenotresolved" and fetched == []


def test_plain_http_goes_to_https():
    route = Route()
    run(lane_route({HOST: EXPOSE}, []).handle(route, Req(f"http://{HOST}/x?y=1", nav=True)))
    assert route.fulfilled["status"] == 308 and route.fulfilled["headers"]["location"] == URL + "x?y=1"


def test_another_sites_page_can_navigate_there_but_not_fetch_from_it():
    fetched: list = []
    r = lane_route({HOST: EXPOSE, "api.expose.agentos.invalid": EXPOSE}, fetched)
    linkedin = FakeFrame("https://www.linkedin.com/feed/")
    cross = Route()
    run(r.handle(cross, Req(URL + "api/me", headers={"sec-fetch-site": "cross-site",
                                                     "origin": "https://www.linkedin.com"}, frame=linkedin)))
    assert cross.aborted == "blockedbyclient" and fetched == []
    worker = Route()  # a service worker's request has no frame and says nothing: refused
    run(r.handle(worker, Req(URL + "api/me", no_frame=True)))
    assert worker.aborted == "blockedbyclient"
    same = Route()
    run(r.handle(same, Req(URL + "assets/app.js", headers={"sec-fetch-site": "same-origin"},
                           frame=FakeFrame(URL))))
    assert same.fulfilled["status"] == 200
    sibling = Route()  # the app on one exposed name calling its API on another
    run(r.handle(sibling, Req("https://api.expose.agentos.invalid/v1", "POST", body=b"{}",
                              headers={"sec-fetch-site": "same-site", "origin": URL.rstrip("/")},
                              frame=FakeFrame(URL))))
    assert sibling.fulfilled["status"] == 200 and fetched[-1].body == b"{}"
    form = Route()  # a form on another site posting there is a navigation: it goes (as any CSRF would)
    run(r.handle(form, Req(URL + "login", "POST", nav=True, frame=linkedin)))
    assert form.fulfilled["status"] == 200


def test_a_dev_server_that_does_not_answer_shows_a_page_and_a_fetch_fails():
    route = Route()
    run(lane_route({HOST: EXPOSE}, [], fail="the dev server on your computer did not answer").handle(
        route, Req(URL, nav=True)))
    assert route.fulfilled["status"] == 502 and b"did not answer" in route.fulfilled["body"]
    assert "—".encode() not in route.fulfilled["body"]
    xhr = Route()
    run(lane_route({HOST: EXPOSE}, [], fail="x").handle(
        xhr, Req(URL + "api", headers={"sec-fetch-site": "same-origin"})))
    assert xhr.aborted == "connectionrefused"


def test_a_handler_that_breaks_fails_closed():
    async def boom(*a, **k):
        raise RuntimeError("bug")

    r = LaneExposeRoute(LANE, lookup=lambda lane, host: EXPOSE, context=FakeContext(), fetch=boom)
    route = Route()
    run(r.handle(route, Req(URL, nav=True)))
    assert route.aborted == "failed"


def test_cookies_come_from_the_context_when_the_driver_hides_them():
    class Ctx(FakeContext):
        async def cookies(self, urls=None):
            return [{"name": "lax", "value": "1", "sameSite": "Lax"},
                    {"name": "strict", "value": "2", "sameSite": "Strict"},
                    {"name": "none", "value": "3", "sameSite": "None"}]

    fetched: list = []
    r = lane_route({HOST: EXPOSE}, fetched, ctx=Ctx())
    main = FakeFrame("https://www.linkedin.com/")
    run(r.handle(Route(), Req(URL + "cb", nav=True, frame=main, headers={"sec-fetch-site": "cross-site"})))
    assert fetched[-1].headers["cookie"] == "lax=1; none=3"  # a cross-site top-level navigation
    run(r.handle(Route(), Req(URL + "x", headers={"sec-fetch-site": "same-origin"}, frame=FakeFrame(URL))))
    assert fetched[-1].headers["cookie"] == "lax=1; strict=2; none=3"


def test_an_oauth_redirect_hop_back_is_navigated_again_once():
    calls: list[str] = []

    class Frame(FakeFrame):
        async def goto(self, url, **kw):
            calls.append(url)

    async def go():
        r = lane_route({HOST: EXPOSE}, [])
        frame = Frame("https://www.linkedin.com/oauth/v2/authorization")
        hop = Req(URL + "auth/callback?code=abc", nav=True, frame=frame, hop=object())
        r.on_request(hop)
        r.on_request(hop)  # the same hop again inside the window: not twice
        r.on_request(Req("https://evil.example/", nav=True, frame=frame, hop=object()))
        r.on_request(Req("https://nobody.expose.agentos.invalid/", nav=True, frame=frame, hop=object()))
        r.on_request(Req(URL + "post", "POST", nav=True, frame=frame, hop=object()))  # a 307 body is gone
        r.on_request(Req(URL + "not-a-hop", nav=True, frame=frame))  # routed already
        await asyncio.sleep(0)
        assert calls == [URL + "auth/callback?code=abc"] and r.stats["rescued"] == 1

    run(go())


# ---- the relay, on a real unix socket ------------------------------------------------------

def test_the_relay_speaks_http_over_the_socket(short_home):
    path = str(short_home / "dev.sock")
    seen: dict = {}

    async def serve(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        seen["head"] = head.decode()
        length = [int(line.split(":")[1]) for line in seen["head"].split("\r\n") if line.lower().startswith("content-length")]
        seen["body"] = await reader.readexactly(length[0]) if length else b""
        writer.write(b"HTTP/1.1 302 Found\r\nLocation: /home\r\nSet-Cookie: a=1\r\nSet-Cookie: b=2\r\n"
                     b"Content-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()

    async def go():
        server = await asyncio.start_unix_server(serve, path=path)
        try:
            return await relay.fetch(Path(path), "POST", HOST, "/login?x=1",
                                     relay.request_headers({"cookie": "s=1", "Host": "evil"}, HOST), b"user=a")
        finally:
            server.close()

    answer = run(go())
    assert answer.status == 302 and answer.body == b"ok"
    assert answer.fulfil_headers()["set-cookie"] == "a=1\nb=2" and answer.fulfil_headers()["location"] == "/home"
    assert seen["head"].startswith("POST /login?x=1 HTTP/1.1") and f"host: {HOST}" in seen["head"].lower()
    assert "evil" not in seen["head"] and seen["body"] == b"user=a"


def test_the_relay_says_plainly_when_nothing_listens(short_home):
    with pytest.raises(relay.RelayError, match="did not answer"):
        run(relay.fetch(short_home / "none.sock", "GET", HOST, "/", [], None))


# ---- forward -------------------------------------------------------------------------------

def test_a_forward_carries_bytes_into_the_lane_and_its_socket_is_private(short_home):
    async def go():
        async def echo(reader, writer):
            writer.write(b"lane:" + await reader.read(100))
            await writer.drain()
            writer.close()

        lane_server = await asyncio.start_server(echo, "127.0.0.1", 0)
        port = lane_server.sockets[0].getsockname()[1]
        dialled: list[int] = []

        async def dial(p: int) -> socket.socket:  # stands in for a socket in the lane's namespace
            dialled.append(p)
            s = socket.socket()
            s.setblocking(False)
            await asyncio.get_running_loop().sock_connect(s, ("127.0.0.1", p))
            return s

        fwd = Forward(short_home / "f.sock", port, dial)
        await fwd.start()
        try:
            assert (os.stat(fwd.path).st_mode & 0o777) == 0o600
            reader, writer = await asyncio.open_unix_connection(str(fwd.path))
            writer.write(b"hello")
            await writer.drain()
            got = await asyncio.wait_for(reader.read(100), 5)
            writer.close()
            assert got == b"lane:hello" and dialled == [port]  # only the forward's own port, ever
        finally:
            await fwd.close()
            lane_server.close()
        assert not fwd.path.exists()

    run(go())


def test_a_forward_needs_a_warm_lane_with_its_own_network():
    class Envs:
        def __init__(self, state="warm", netns=Path("/run/netns/x")):
            self._state, self.netns = state, netns

        def has(self, lane):
            return lane == "dev/web"

        def state(self, lane):
            return SimpleNamespace(value=self._state)

        async def warm(self, lane):
            return SimpleNamespace(netns=self.netns, host=object())

    async def go():
        with pytest.raises(Exception, match="no terminal or service lanes"):
            await lane_dialer(None, "dev/web")
        with pytest.raises(InvalidRequest, match="not a terminal or service lane"):
            await lane_dialer(Envs(), "personal/linkedin")
        with pytest.raises(InvalidRequest, match="cold"):
            await lane_dialer(Envs(state="cold"), "dev/web")
        with pytest.raises(InvalidRequest, match="shares the node's network"):
            await lane_dialer(Envs(netns=None), "dev/web")
        assert callable(await lane_dialer(Envs(), "dev/web"))

    run(go())


# ---- what agents see -----------------------------------------------------------------------

def test_lanes_list_tells_an_agent_the_address_while_it_lasts(svc):
    from agentos.core.models import LaneInfo, LaneSpec
    from agentos.node.tools import lanes_list

    infos = [LaneInfo(spec=LaneSpec(id=LANE)), LaneInfo(spec=LaneSpec(id=OTHER))]

    class Ctx:
        runtime = SimpleNamespace(lanes=SimpleNamespace(list=lambda: infos),
                                  get=lambda name: svc if name == "expose" else None)

        async def authorize(self, *a, **k):
            return None

    async def go():
        _link, sock = await active_expose(svc)
        try:
            out = await lanes_list(Ctx())
        finally:
            sock.close()
        return out

    out = run(go())
    by_id = {row["id"]: row for row in out["lanes"]}
    assert by_id[LANE]["exposed"][0]["url"] == URL and "exposed" not in by_id[OTHER]


# ---- the lane manager's wiring -------------------------------------------------------------

async def test_the_lane_manager_tells_the_service_of_each_new_browser(tmp_path):
    from test_lanes import make

    from agentos.core.models import LaneSpec

    mgr, _engine, *_ = make(tmp_path, self_check=False)
    await mgr.create(LaneSpec(id=LANE))
    seen: list[tuple[str, object]] = []

    async def listener(lane_id, session):
        seen.append((lane_id, session))

    def broken(lane_id, session):
        raise RuntimeError("a listener with a bug")

    mgr.add_session_listener(broken)  # its failure is logged, never the lane's
    mgr.add_session_listener(listener)
    assert mgr.live_session(LANE) is None
    await mgr.open(LANE)
    assert [lane for lane, _ in seen] == [LANE]
    assert mgr.live_session(LANE) is seen[0][1]
    await mgr.close(LANE)
    assert mgr.live_session(LANE) is None
    await mgr.shutdown()
