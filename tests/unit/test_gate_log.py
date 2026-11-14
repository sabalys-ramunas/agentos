"""The owner's gate log (agentos.node.gateshapes, docs/design/egress-guard.md 8.9): the shapes the
commit gate classified without a rule, grouped with counts; the owner promotes one to the node's
read allowlist or to a named commit rule, every lane's guard reads the change, an audit row is
written, and only the owner may (members and grants are refused). Fake requests (test_egress)."""

from __future__ import annotations

import json

import pytest
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Mount
from starlette.testclient import TestClient
from test_commit_gate_guard import Gate
from test_egress import COMMITTER, Clock, FakeContext, FakeRequest, FakeRoute

from agentos.api.auth import PRESENCE_HEADER, PanelStore
from agentos.core.config import NodeConfig
from agentos.core.errors import InvalidRequest, LaneNotFound, NotFound
from agentos.core.presence import Presence
from agentos.node import egress, gateshapes
from agentos.node.egress import EgressTrace, LaneGuard
from agentos.node.routes import RouteDispatcher
from agentos.node.viewer_routes import gate_log_api_routes

LANE = "biz-a/reddit"
GQL = "https://www.reddit.com/svc/shreddit/graphql"
TRACK = "https://www.reddit.com/svc/shreddit/data-protection-consent"
SCHEDULE = "https://www.reddit.com/svc/shreddit/scheduled-posts/save"
# a host no commit rule names (a site whose rules only read): a read with no operation can be promoted
# there. No built-in host is one: each site's rules publish on every host of the site
BEACON = "https://events.example/beacon"
BASE = (*egress.EFFECT_RULES, egress.EffectRule("events.read.known", "events", "events.example", r"/known$",
                                                effect="read"))
JSON = {"content-type": "application/json"}


def gql(*ops: str) -> bytes:
    batch = [{"operationName": op, "variables": {"input": {"x": 1}}} for op in ops]
    return json.dumps(batch[0] if len(batch) == 1 else batch).encode()


def node(tmp_path):
    cfg = NodeConfig(home=tmp_path)
    lane_dir = cfg.lane_dir(LANE)
    lane_dir.mkdir(parents=True)
    clock = Clock()
    guard = LaneGuard(LANE, trace=EgressTrace(lane_dir / egress.TRACE_NAME), clock=clock)
    ctx = FakeContext()
    guard.attach(ctx, RouteDispatcher(ctx))
    guard.drive(COMMITTER)
    guard.owner_shapes = gateshapes.ShapeSource(gateshapes.store_path(tmp_path))
    guard.rules = BASE
    return cfg, lane_dir, guard, ctx, clock


async def send(guard, ctx, gate, url: str, body: bytes | None = None, method: str = "POST") -> bool:
    """One request on the drive's route: True when it was held."""
    route = FakeRoute()
    request = FakeRequest(url, method, headers=JSON, body=body)
    ctx.emit("request", request)
    await ctx.routes[0][1](route, request)
    return route.aborted is not None


async def test_the_gate_log_groups_shapes_and_a_promoted_read_goes(tmp_path):
    _, lane_dir, guard, ctx, clock = node(tmp_path)
    gate = Gate()
    async with guard.acting(gate=gate):
        assert await send(guard, ctx, gate, GQL, gql("FetchGoldBalance"))
        clock.now += 2  # one gate_classified row per shape a second
        assert await send(guard, ctx, gate, GQL, gql("FetchGoldBalance"))
        assert await send(guard, ctx, gate, TRACK, b'{"consent": true}')
        assert await send(guard, ctx, gate, GQL, json.dumps({"variables": {}}).encode())  # no operation name
    shapes = gateshapes.log(gateshapes.trace_rows(lane_dir))
    by_op = {s["op"]: s for s in shapes}
    gold = by_op["FetchGoldBalance"]
    assert gold["count"] == 2 and gold["decisions"] == {"gated": 2} and gold["method"] == "POST"
    assert gold["why"] == {"an unknown operation FetchGoldBalance": 2} and gold["rule"] == "reddit.read.graphql"
    assert "variables.input.x" in gold["fields"] and gold["id"] == gateshapes.shape_id(
        "www.reddit.com", "/svc/shreddit/graphql", "POST", "FetchGoldBalance")
    track = next(s for s in shapes if s["path"].endswith("consent"))
    assert track["op"] is None and "consent" in track["fields"]
    assert gateshapes.log(gateshapes.trace_rows(lane_dir), since=gateshapes.since_of("1m") + 3600) == []

    unnamed = next(s for s in shapes if s["path"].endswith("graphql") and s["op"] is None)
    with pytest.raises(InvalidRequest, match="names no operation"):
        gateshapes.promote(tmp_path, lane_dir, unnamed["id"], as_="read", lane=LANE, by="owner")
    with pytest.raises(InvalidRequest, match="publishes on"):  # www.reddit.com publishes: no body-blind read
        gateshapes.promote(tmp_path, lane_dir, track["id"], as_="read", lane=LANE, by="owner")
    gateshapes.promote(tmp_path, lane_dir, gold["id"], as_="read", lane=LANE, by="owner")
    async with guard.acting(gate=gate):
        assert await send(guard, ctx, gate, BEACON, b'{"consent": true, "other": 1}')
    beacon = next(s for s in gateshapes.log(gateshapes.trace_rows(lane_dir)) if s["path"] == "/beacon")
    gateshapes.promote(tmp_path, lane_dir, beacon["id"], as_="read", lane=LANE, by="owner", base=BASE)
    assert oct(gateshapes.store_path(tmp_path).stat().st_mode & 0o777) == "0o600"

    clock.now += egress.SITES_TTL_S  # the guard reads the node's allowlist again once it changed
    seen = list(gate.seen)
    async with guard.acting(gate=gate):
        assert not await send(guard, ctx, gate, GQL, gql("FetchGoldBalance"))
        assert not await send(guard, ctx, gate, BEACON, b'{"consent": false}')  # an optional field may go
        # a batch is a read only when every operation is: a promoted read does not carry a mutation
        assert await send(guard, ctx, gate, GQL, gql("FetchGoldBalance", "CreatePostDraft"))
        assert await send(guard, ctx, gate, BEACON, b"{}", method="PUT")  # another method is another shape
    assert gate.seen == [*seen, "reddit.unknown", "events.unknown"]
    promoted = gateshapes.load(gateshapes.store_path(tmp_path))
    assert {p["id"] for p in promoted} == {gold["id"], beacon["id"]}
    assert {s["id"]: s.get("promoted") for s in gateshapes.log(gateshapes.trace_rows(lane_dir),
                                                                promoted=promoted)}[gold["id"]] == "read"

    gateshapes.remove(tmp_path, gold["id"])  # undone: fail closed again
    clock.now += egress.SITES_TTL_S
    async with guard.acting(gate=gate):
        assert await send(guard, ctx, gate, GQL, gql("FetchGoldBalance"))
    with pytest.raises(NotFound):
        gateshapes.remove(tmp_path, gold["id"])


async def test_a_shape_promoted_to_a_named_commit_rule_is_held_by_that_name(tmp_path):
    _, lane_dir, guard, ctx, clock = node(tmp_path)
    gate = Gate()
    async with guard.acting(gate=gate):
        assert await send(guard, ctx, gate, SCHEDULE, b'{"title": "t", "body": "b"}')
    [shape] = gateshapes.log(gateshapes.trace_rows(lane_dir))
    for bad in ("read", "post.Media", "draft.x", "reddit.post"):  # a read, bad letters, no draft, a built-in
        with pytest.raises(InvalidRequest):
            gateshapes.promote(tmp_path, lane_dir, shape["id"], as_="commit", name=bad, lane=LANE, by="owner")
    with pytest.raises(NotFound):
        gateshapes.promote(tmp_path, lane_dir, "sh_000000000000", as_="read", lane=LANE, by="owner")
    entry = gateshapes.promote(tmp_path, lane_dir, shape["id"], as_="commit", name="post.scheduled", lane=LANE,
                               by="owner")
    assert entry["rule"] == "reddit.post.scheduled"
    clock.now += egress.SITES_TTL_S
    async with guard.acting(gate=gate):
        assert await send(guard, ctx, gate, SCHEDULE, b'{"title": "t", "body": "b"}')
    assert gate.seen == ["reddit.unknown", "reddit.post.scheduled"]
    rule = next(r for r in guard.rules if r.id == "reddit.post.scheduled")
    assert rule.effect == "commit" and not rule.verified and guard.rules[0] is rule  # before the built-in ones


# ---- the owner's API -----------------------------------------------------------------------

class Lanes:
    def __init__(self, cfg: NodeConfig) -> None:
        self.config = cfg

    def get(self, lane_id: str):
        if lane_id != LANE:
            raise LaneNotFound(f"no lane {lane_id}")
        return object()


class Traces:
    def __init__(self) -> None:
        self.rows: list = []

    def write(self, ev):
        self.rows.append(ev)
        return ev


class Access:
    def __init__(self, *, owner: bool, member=None, presence: Presence = Presence.PERSON) -> None:
        self.owner, self.member, self.presence, self.traced = owner, member, presence, []

    def trace(self, action, **fields):
        self.traced.append((action, fields))


class Principal:
    def __init__(self, kind: str) -> None:
        self.kind = kind


def api(cfg: NodeConfig, traces: Traces, who: dict) -> TestClient:
    class As(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if who:
                request.state.principal = Principal(who["kind"])
                request.state.auth = type("Auth", (), {"access": who["access"]})()
            return await call_next(request)

    app = Starlette(routes=[Mount("/api", routes=gate_log_api_routes(Lanes(cfg), traces=traces))])
    app.add_middleware(As)
    return TestClient(app)


def person(cfg: NodeConfig, code: str) -> dict:
    """The node's CLI as a person: a one-time code it writes into panel.db, beside the node token."""
    PanelStore.for_home(cfg.home).add_code(code, ttl_seconds=60)
    return {PRESENCE_HEADER: code}


async def test_only_the_owner_reads_the_gate_log_and_promotes(tmp_path):
    cfg, lane_dir, guard, ctx, _ = node(tmp_path)
    async with guard.acting(gate=Gate()):
        await send(guard, ctx, None, GQL, gql("FetchGoldBalance"))
    [shape] = gateshapes.log(gateshapes.trace_rows(lane_dir))
    path = "/api/lanes/biz-a/reddit/gate-log"
    for who in ({"kind": "human", "access": Access(owner=True, member="m1")},  # a member in person
                {"kind": "human", "access": Access(owner=False)}):
        traces = Traces()
        with api(cfg, traces, who) as client:
            assert client.get(path).status_code == 403
            r = client.post(path, json={"allow_read": shape["id"]})
            assert r.status_code == 403 and "Only the owner" in r.text and "—" not in r.text
        assert traces.rows == [] and who["access"].traced[0][1]["status"] == "denied"
    # an admin grant: an agent's credential, below a person, so a change is presence_required
    admin = {"kind": "agent", "access": Access(owner=True, presence=Presence.AGENT)}
    traces = Traces()
    with api(cfg, traces, admin) as client:
        assert client.get(path).status_code == 403
        r = client.post(path, json={"allow_read": shape["id"]})
        assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required", r.text
    assert traces.rows == [] and admin["access"].traced[-1][1]["status"] == "denied"
    assert gateshapes.load(gateshapes.store_path(tmp_path)) == []

    traces = Traces()
    with api(cfg, traces, {}) as client:  # the node token: no principal behind the bare bearer check
        r = client.get(path + "?since=1h")
        assert r.status_code == 200 and [s["id"] for s in r.json()["shapes"]] == [shape["id"]]  # reading is open to it
        r = client.post(path, json={"allow_read": shape["id"]})  # a change is not: the token is no person
        assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required", r.text
        assert traces.rows == [] and gateshapes.load(gateshapes.store_path(tmp_path)) == []
        r = client.post(path, json={"allow_read": shape["id"]}, headers=person(cfg, "gate-code-1"))
        assert r.status_code == 200 and r.json()["promoted"]["as"] == "read"
        assert client.get("/api/lanes/biz-a/nope/gate-log").status_code == 404
        assert client.post(path, json={"fly": 1}, headers=person(cfg, "gate-code-2")).status_code == 400
        removal = client.post(path, json={"remove": shape["id"]}, headers=person(cfg, "gate-code-3"))
        assert removal.json()["removed"]["id"] == shape["id"]
    [added, removed] = traces.rows
    assert added.kind == "gate_shape" and added.principal == "owner" and added.lane == LANE
    assert added.args == {"shape": shape["id"], "as": "read", "rule": None, "lanes": [LANE], "removed": False}
    assert "promoted POST www.reddit.com/svc/shreddit/graphql FetchGoldBalance" in added.summary
    assert added.summary.endswith("on biz-a/reddit")
    assert removed.args["removed"] is True

    owner_session = {"kind": "human", "access": Access(owner=True)}
    with api(cfg, Traces(), owner_session) as client:
        assert client.get(path).status_code == 200


# ---- the store -------------------------------------------------------------------------------

def _entry(i: int) -> dict:
    return {"id": f"sh_{i:012x}", "as": "commit", "site": "reddit", "host": "www.reddit.com", "path": f"/p{i}",
            "method": "POST", "op": None, "doc_ids": [], "fields": [], "lanes": None, "rule": f"reddit.post.n{i}", "by": "owner",
            "at": "x"}


def test_concurrent_saves_keep_every_promotion(tmp_path, monkeypatch):
    """Two writers (the daemon and the CLI) at once: a lock, a unique temp file each, no lost update."""
    import threading

    lane_dir = tmp_path / "lane"
    lane_dir.mkdir()
    entries = {e["id"]: e for e in (_entry(i) for i in range(12))}
    monkeypatch.setattr(gateshapes, "log", lambda rows, **kw: list(entries.values()))
    monkeypatch.setattr(gateshapes, "commit_rule_id", lambda site, name: name)
    start = threading.Barrier(len(entries))
    errors: list = []

    def promote(e):
        start.wait()
        try:
            gateshapes.promote(tmp_path, lane_dir, e["id"], as_="commit", name=e["rule"], lane=LANE, by="owner")
        except Exception as x:  # noqa: BLE001
            errors.append(x)

    threads = [threading.Thread(target=promote, args=(e,)) for e in entries.values()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert {s["id"] for s in gateshapes.load(gateshapes.store_path(tmp_path))} == set(entries)
    assert not list(tmp_path.glob("*.tmp"))


def test_a_torn_write_falls_back_to_the_last_copy(tmp_path):
    path = gateshapes.store_path(tmp_path)
    gateshapes._save(path, [_entry(1)])
    gateshapes._save(path, [_entry(1), _entry(2)])  # the copy kept is the one with one shape
    assert oct(gateshapes.backup_path(path).stat().st_mode & 0o777) == "0o600"
    path.write_text('{"version": 1, "shapes": [{"id"')  # torn
    assert [s["id"] for s in gateshapes.load(path)] == [_entry(1)["id"]]
    gateshapes._save(path, [_entry(3)])  # a torn file is never kept as the copy
    assert [s["id"] for s in gateshapes._read(gateshapes.backup_path(path))] == [_entry(1)["id"]]


def test_a_malformed_or_hand_edited_store_never_compiles_into_a_rule(tmp_path):
    """Every entry is validated strictly: one with no id, a non-string op (once body-blind), a site
    or id that is not its shape's, a rule promote() would refuse; each is skipped, the rest apply."""
    good = {"id": gateshapes.shape_id("www.reddit.com", "/svc/x", "POST", None), "as": "commit", "site": "reddit",
            "host": "www.reddit.com", "path": "/svc/x", "method": "POST", "op": None, "fields": [],
            "lanes": None, "rule": "reddit.post.x"}
    bad = [{k: v for k, v in good.items() if k != "id"},
           good | {"as": "read", "rule": None, "op": 7},
           good | {"site": "facebook"},
           good | {"path": "/svc/y"},  # the id is another shape's
           good | {"rule": "reddit.post"},  # a built-in
           good | {"method": "GET"},
           {k: v for k, v in good.items() if k != "lanes"},
           "not an object"]
    rules = gateshapes.compile_rules([*bad, good], egress.EFFECT_RULES)
    assert [r.id for r in rules.commit] == ["reddit.post.x"] and rules.read == () and rules.read_ops == {}


def test_the_guard_falls_back_to_the_built_in_rules_when_the_promotions_fail(tmp_path, monkeypatch):
    _, _, guard, _, clock = node(tmp_path)
    entry = {"id": gateshapes.shape_id("www.reddit.com", "/svc/x", "POST", None), "as": "commit",
             "site": "reddit", "host": "www.reddit.com", "path": "/svc/x", "method": "POST", "op": None,
             "fields": [], "lanes": [LANE], "rule": "reddit.post.x"}
    gateshapes._save(gateshapes.store_path(tmp_path), [entry])
    clock.now += egress.SITES_TTL_S
    assert guard.rules[0].id == "reddit.post.x"

    def broken(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(gateshapes, "compile_rules", broken)
    gateshapes._save(gateshapes.store_path(tmp_path), [entry, entry | {"path": "/svc/y"}])
    clock.now += egress.SITES_TTL_S
    assert guard.rules == guard._base_rules  # not the stale promotions
    monkeypatch.undo()
    clock.now += egress.SITES_TTL_S  # not recorded as seen: compiled again once it can be
    assert guard.rules[0].id == "reddit.post.x"

    guard.owner_shapes = broken  # a source that fails: the built-in rules alone, not the last ones
    clock.now += egress.SITES_TTL_S
    assert guard.rules == guard._base_rules


async def test_a_promoted_read_binds_the_doc_ids_it_was_seen_with(tmp_path):
    """The review's probe: a promoted name was trusted whatever doc_id it came with, so a persisted
    mutation sent under a promoted read's name went. Its promotion binds the doc_id seen."""
    _, lane_dir, guard, ctx, clock = node(tmp_path)
    gate = Gate()

    def named(doc: str | None) -> bytes:
        return json.dumps({"operationName": "FetchGoldBalance", **({"documentId": doc} if doc else {}),
                           "variables": {}}).encode()

    async with guard.acting(gate=gate):
        assert await send(guard, ctx, gate, GQL, named("d1111"))
    [shape] = gateshapes.log(gateshapes.trace_rows(lane_dir))
    assert shape["doc_ids"] == ["d1111"] and shape["id"] == gateshapes.shape_id(
        "www.reddit.com", "/svc/shreddit/graphql", "POST", "FetchGoldBalance", ["d1111"])
    gateshapes.promote(tmp_path, lane_dir, shape["id"], as_="read", lane=LANE, by="owner")
    clock.now += egress.SITES_TTL_S
    async with guard.acting(gate=gate):
        assert not await send(guard, ctx, gate, GQL, named("d1111"))
        assert await send(guard, ctx, gate, GQL, named("d2222"))  # the same name, another persisted query
        assert await send(guard, ctx, gate, GQL, named(None))


async def test_a_promotion_holds_on_its_own_endpoint_and_lane(tmp_path):
    """The review's probe: a promoted name was trusted on every strict endpoint of the site and on
    every lane of the node. It holds for the host and path it was seen on, on its lane, unless the
    owner widens it (--all-lanes)."""
    _, lane_dir, guard, ctx, clock = node(tmp_path)
    gate = Gate()
    fb = "https://www.facebook.com/api/graphql/"
    async with guard.acting(gate=gate):
        assert await send(guard, ctx, gate, fb, gql("CometGoldBalance"))
    [shape] = gateshapes.log(gateshapes.trace_rows(lane_dir))
    entry = gateshapes.promote(tmp_path, lane_dir, shape["id"], as_="read", lane=LANE, by="owner")
    assert entry["lanes"] == [LANE]
    other = LaneGuard("biz-b/reddit", clock=clock)
    other.owner_shapes = guard.owner_shapes
    clock.now += egress.SITES_TTL_S
    rules = gateshapes.compile_rules(gateshapes.load(gateshapes.store_path(tmp_path)), egress.EFFECT_RULES, lane=LANE)
    assert rules.read_ops == {("www.facebook.com", "/api/graphql/"): {"CometGoldBalance": frozenset({""})}}
    async with guard.acting(gate=gate):
        assert not await send(guard, ctx, gate, fb, gql("CometGoldBalance"))
        # another strict endpoint of the site is not the one it was seen on
        assert await send(guard, ctx, gate, "https://web.facebook.com/api/graphql/", gql("CometGoldBalance"))
        assert await send(guard, ctx, gate, "https://www.facebook.com/api/graphqlbatch/", gql("CometGoldBalance"))
    assert other._read_ops == {} and other.rules == other._base_rules  # another lane: not promoted there
    [row] = [s for s in gateshapes.log(gateshapes.trace_rows(lane_dir), promoted=gateshapes.load(
        gateshapes.store_path(tmp_path))) if s["id"] == shape["id"]]
    assert row["promoted_lanes"] == [LANE]

    wide = gateshapes.promote(tmp_path, lane_dir, shape["id"], as_="read", lane=LANE, by="owner", all_lanes=True)
    assert wide["lanes"] is None
    clock.now += egress.SITES_TTL_S
    assert ("www.facebook.com", "/api/graphql/") in (other.rules and other._read_ops)
    again = gateshapes.promote(tmp_path, lane_dir, shape["id"], as_="read", lane="biz-c/reddit", by="owner")
    assert again["lanes"] is None  # promoting from another lane never narrows a wider promotion


async def test_a_read_promoted_without_an_operation_binds_its_field_names(tmp_path):
    """The review's probe: a read promoted for a request that names no operation let every body
    sent there go. It binds the shape's field names: a body with another field, an operation name,
    a doc_id or a GraphQL mutation is held; a shape that mixes bodies on one path is not promoted."""
    _, lane_dir, guard, ctx, clock = node(tmp_path)
    gate = Gate()
    async with guard.acting(gate=gate):
        assert await send(guard, ctx, gate, BEACON, b'{"event": "view", "meta": {"page": "feed"}}')
    [shape] = gateshapes.log(gateshapes.trace_rows(lane_dir))
    assert not shape["mixed"] and not shape["partial"]
    entry = gateshapes.promote(tmp_path, lane_dir, shape["id"], as_="read", lane=LANE, by="owner", base=BASE)
    assert entry["fields"] == ["event", "meta", "meta.page"]
    clock.now += egress.SITES_TTL_S
    [rule] = [r for r in guard.rules if r.id.startswith("events.read.owner_")]
    assert rule.fields == frozenset({"event", "meta", "meta.page"})
    async with guard.acting(gate=gate):
        assert not await send(guard, ctx, gate, BEACON, b'{"event": "click", "meta": {"page": "post"}}')
        assert not await send(guard, ctx, gate, BEACON, b'{"event": "view"}')
        assert not await send(guard, ctx, gate, BEACON, b"")
        for held in (b'{"event": "view", "title": "a post", "body": "text"}',  # fields it never had
                     b'{"event": "view", "operationName": "CreatePost"}',
                     b'{"event": "view", "doc_id": "123"}',
                     b'{"query": "mutation { createPost(title: \\"x\\") { id } }"}',
                     b'[{"event": "view"}, {"event": "view", "text": "x"}]',  # every element is read
                     b"not json at all"):
            assert await send(guard, ctx, gate, BEACON, held), held
    rows = [r for r in gateshapes.trace_rows(lane_dir) if r.get("kind") == "gate_classified"]
    assert {r["why"] for r in rows if r["rule"] == rule.id} >= {
        "fields the promoted read never had (body, title)", "a body that names an operation (operationName)"}

    mixed = "https://events.example/mixed"
    async with guard.acting(gate=gate):
        assert await send(guard, ctx, gate, mixed, b'{"event": "view"}')
        clock.now += 2
        assert await send(guard, ctx, gate, mixed, b'{"title": "t", "body": "b"}')
    shape = next(s for s in gateshapes.log(gateshapes.trace_rows(lane_dir)) if s["path"] == "/mixed")
    assert shape["mixed"] and shape["field_sets"] == 2
    with pytest.raises(InvalidRequest, match="mixes 2 bodies on one path"):
        gateshapes.promote(tmp_path, lane_dir, shape["id"], as_="read", lane=LANE, by="owner", base=BASE)
    # a hand-edited op-less read on a host that publishes never compiles into a rule
    forged = entry | {"host": "www.reddit.com", "site": "reddit",
                      "id": gateshapes.shape_id("www.reddit.com", "/beacon", "POST", None)}
    assert gateshapes.compile_rules([forged], BASE, lane=LANE).read == ()


# ---- shapes observe mode saw, and reads that never outrank a built-in rule (8.12) ----------------

async def test_a_shape_only_observe_mode_saw_can_be_named_a_commit_never_a_read(tmp_path):
    """The canary's way to find a new publish: the owner observes the lane and comments by hand. The
    request passed the GraphQL catch-all (no agent drove), so the gate log never had it; with
    --observed it is listed, and naming it a commit makes the gate hold it under that name."""
    _, lane_dir, guard, ctx, clock = node(tmp_path)
    guard.drive(egress.Driver(kind="human", id="owner"))
    guard.human_in_control = lambda: True
    egress.write_observe(guard.observe_path, 10)
    ctx.emit("request", FakeRequest(GQL, "POST", headers=JSON, body=gql("CreateCommentV2")))
    assert gateshapes.log(gateshapes.trace_rows(lane_dir)) == []  # the gate never classified it
    [shape] = gateshapes.log(gateshapes.trace_rows(lane_dir), observed=True)
    assert shape["op"] == "CreateCommentV2" and shape["observed_only"] and shape["decisions"] == {"observed": 1}
    with pytest.raises(NotFound):
        gateshapes.promote(tmp_path, lane_dir, shape["id"], as_="read", lane=LANE, by="owner")
    entry = gateshapes.promote(tmp_path, lane_dir, shape["id"], as_="commit", name="comment.v2", lane=LANE,
                               by="owner")
    assert entry["rule"] == "reddit.comment.v2"
    guard.human_in_control = lambda: False
    guard.drive(COMMITTER)
    clock.now += egress.SITES_TTL_S
    gate = Gate()
    async with guard.acting(gate=gate):
        assert await send(guard, ctx, gate, GQL, gql("CreateCommentV2"))
    assert gate.seen == ["reddit.comment.v2"]


async def test_a_promoted_read_never_downgrades_a_built_in_commit_or_write_rule(tmp_path):
    """A read promotion comes after the built-in rules, and a known read name counts only on the
    catch-all: a stored read for CreatePost (a hand edit, or a shape promoted before a rule named it)
    leaves reddit.post a commit and UpdatePostVoteState a write."""
    _, _, guard, ctx, clock = node(tmp_path)
    entries = [{"id": gateshapes.shape_id("www.reddit.com", "/svc/shreddit/graphql", "POST", op), "as": "read",
                "site": "reddit", "host": "www.reddit.com", "path": "/svc/shreddit/graphql", "method": "POST",
                "op": op, "doc_ids": [], "fields": [], "lanes": None, "rule": None}
               for op in ("CreatePost", "UpdatePostVoteState")]
    gateshapes._save(gateshapes.store_path(tmp_path), entries)
    clock.now += egress.SITES_TTL_S
    assert guard.rules and {"CreatePost", "UpdatePostVoteState"} <= set(
        guard._read_ops[("www.reddit.com", "/svc/shreddit/graphql")])  # the promotions did compile
    assert egress.classify(GQL, "POST", JSON, rules=guard.rules, body=gql("CreatePost")).id == "reddit.post"
    assert egress.classify(GQL, "POST", JSON, rules=guard.rules, body=gql("UpdatePostVoteState")).id \
        == "reddit.like.post"
    gate = Gate()
    async with guard.acting(gate=gate):
        assert await send(guard, ctx, gate, GQL, gql("CreatePost"))
    assert gate.seen == ["reddit.post"]
