"""The items inbox, brief B4 (agentos.engage.items): approved content made outside agentOS joins a
surface's schedule, under the owner, inside the plan's caps."""

from __future__ import annotations

import asyncio
import threading
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace as NS

import pytest

from agentos.core.errors import ApprovalPending, InvalidRequest, NotFound, PolicyDenied
from agentos.core.models import Effect, Principal
from agentos.core.runtime import Runtime
from agentos.engage import tools
from agentos.engage.errors import OwnerOnly
from agentos.engage.executor import Executor
from agentos.engage.fakes import FakeContent, FakeLanes, FakeOperations, FakeTargets
from agentos.engage.items import ChainedContent, ItemsContent
from agentos.engage.operations import Registry
from agentos.engage.persona import draw_persona
from agentos.engage.ports import CONTENT_SESSION, Content, ContentOrigin, OpResult, OpStatus
from agentos.engage.service import EngageService
from agentos.engage.store import EngageStore

OWNER = {"kind": "human", "id": "owner", "owner": "owner"}
OWNER_P = Principal(kind="human", id="owner")
AGENT = Principal(kind="agent", id="agent-1")
OTHER = Principal(kind="agent", id="agent-2")
MONDAY = date(2026, 11, 2)
RD = "biz-a/reddit"
POST_URL = "https://www.reddit.com/r/coffee/comments/abc123/latte_art/"

RD_YAML = """
program: biz-a-rd
surface: biz-a/reddit
timezone: Europe/Vilnius
phases:
  - kind: steady
    daily: {comment: 1, post: 1, join: 1}
comments: {mode: approve, max_chars: 140}
"""

RD_POSTS_ONLY = """
program: biz-a-rd-posts
surface: biz-a/reddit
timezone: Europe/Vilnius
phases:
  - kind: steady
    daily: {post: 1}
"""


class Approvals:
    """The approval store as ItemsInbox reads it: `get(id)`, an Approval-shaped record."""

    def __init__(self):
        self.found: dict[str, NS] = {}

    def add(self, ap_id, content, *, state="consumed", tool="engage_item_submit", kind="action"):
        from agentos.policy.approvals import content_digest

        self.found[ap_id] = NS(id=ap_id, kind=kind, tool=tool, state=state, content_digest=content_digest(content))

    def get(self, ap_id):
        try:
            return self.found[ap_id]
        except KeyError:
            raise NotFound(f"no approval {ap_id}") from None


class Files:
    """A FileCheck over a dict of (surface, id) -> stored file."""

    def __init__(self, **found):
        self.found = {("biz-a/reddit" if k.startswith("in_") else k, k): v for k, v in found.items()}

    def __call__(self, surface, file_id):
        try:
            return self.found[(surface, file_id)]
        except KeyError:
            raise NotFound(f"no file {file_id}") from None


class Rig:
    def __init__(self, home, yaml_text: str = RD_YAML, *, files=None):
        self.t = datetime(2026, 11, 1, 23, 0, tzinfo=UTC).timestamp()      # Monday 01:00 in Vilnius
        self.svc = EngageService(home=home, clock=lambda: self.t)
        self.svc.store.put_persona(draw_persona(RD, "items-seed-1"))
        out = self.svc.draft(RD, yaml_text, by=OWNER)
        self.svc.approve(out["program"], by=OWNER)
        self.inbox = self.svc.items
        self.inbox.files = files
        self.approvals = Approvals()
        self.inbox.approvals = lambda: self.approvals
        self.runtime = Runtime(engage=self.svc, approvals=self.approvals)
        self.ops = FakeOperations()
        self.lanes = FakeLanes()
        self.content = ChainedContent([ItemsContent(self.svc.store, clock=lambda: self.t)])

        async def sleep(s: float) -> None:
            self.t += s

        registry = Registry(installed=lambda f: True, verified=lambda f: "test canary")     # canaries ran
        self.inbox.registry = registry
        self.ex = Executor(self.svc.store, registry=registry, operations=self.ops,
                           targets=FakeTargets(), content=self.content, lanes=self.lanes, clock=lambda: self.t,
                           sleep=sleep, plan_for=self.svc.day_plan, tz_for=self.svc.timezone)
        self.svc.executor = self.ex

    @property
    def now(self) -> datetime:
        return datetime.fromtimestamp(self.t, UTC)

    def at(self, t: datetime) -> None:
        self.t = t.timestamp()

    def add(self, kind="post", text="Hello coffee people", **kw) -> dict:
        kw.setdefault("targets", ["r/coffee"] if kind == "post" else [POST_URL])
        return self.inbox.submit(self.inbox.check(RD, kind, text, **kw), in_person=True)

    def run(self, day: date, idx: int) -> dict:
        w = self.ex.window(RD, day, idx)
        self.at(w.start)
        return asyncio.run(self.ex.run_window(RD, day, idx))

    def windows_with(self, kind: str, days: int = 7):
        return [(d, w) for d in (MONDAY + timedelta(days=i) for i in range(days)) for w in self.ex.windows(RD, d)
                if kind in w.kinds]

    def run_days(self, start: date, days: int) -> None:
        for n in range(days):
            day = start + timedelta(days=n)
            for w in sorted(self.ex.windows(RD, day), key=lambda x: x.start):
                self.run(day, w.idx)


class Ctx:
    """A ToolContext: `in_person` as a signed-in panel session sets it. With `approve`, a commit
    comes back approved and its approval is recorded (consumed) in the runtime's approvals."""

    def __init__(self, runtime, principal, *, approve: bool = False, lanes: str = "biz-a/",
                 in_person: bool = False):
        self.runtime = runtime
        self.principal = principal
        self.approve = approve
        self.lanes = lanes
        self.in_person = in_person
        self.seen: list[tuple[str, str | None, object]] = []
        self.grant = NS(path=f"/gr_{principal.id}/")  # its grant: one per principal here

    async def authorize(self, effect, *, lane_id=None, summary=None, content=None, **kw):
        self.seen.append((effect.value, lane_id, content))
        if self.in_person or effect in (Effect.READ, Effect.DRAFT):
            return NS(route="auto", via=None)
        if self.approve:
            approvals = self.runtime.get("approvals")
            ap_id = f"ap_{len(approvals.found) + 1}"
            approvals.add(ap_id, content)
            return NS(route="approved", via=ap_id)
        raise ApprovalPending("the owner approves this item first", "ap_1")

    def can_access_lane(self, lane_id):
        return lane_id.startswith(self.lanes)


def call(fn, ctx, **kw):
    return asyncio.run(fn(ctx, **kw))


# ---------------------------------------------------------------- submitting


def test_the_owner_submits_and_the_item_is_queued_as_approved_with_history(tmp_path):
    rig = Rig(tmp_path)
    ctx = Ctx(rig.runtime, OWNER_P, in_person=True)
    out = call(tools.engage_item_submit, ctx, surface=RD, kind="post", text="Fresh beans today",
               title="Fresh beans", targets=["r/coffee"])
    assert out["status"] == "queued" and out["submitted_by"] == "owner" and out["requested_by"] is None
    assert out["id"].startswith("it_") and out["warnings"] == []
    assert ctx.seen[0][:2] == ("draft", RD)
    expires = datetime.fromisoformat(out["expires_at"])
    assert expires - rig.now == timedelta(days=7)
    [h] = rig.svc.store.history(RD, kinds=("item_submitted",))
    assert h["by"] == "owner" and h["payload"]["id"] == out["id"] and h["payload"]["requested_by"] is None
    [c] = ItemsContent(rig.svc.store, clock=lambda: rig.t).take(RD, "post", 3)
    assert c.approved and c.origin is ContentOrigin.ITEM and c.targets == ("r/coffee",) and c.title == "Fresh beans"
    assert rig.svc.status(RD)["surfaces"][0]["items"] == {"reserved": 1}


def test_an_agent_submission_waits_for_the_owner_and_queues_nothing(tmp_path):
    rig = Rig(tmp_path)
    runtime = rig.runtime
    ctx = Ctx(runtime, AGENT)
    with pytest.raises(ApprovalPending):
        call(tools.engage_item_submit, ctx, surface=RD, kind="post", text="Hi", targets=["r/coffee"])
    assert rig.svc.store.items(RD) == [] and rig.svc.store.history(RD, kinds=("item_submitted",)) == []
    # commit with no lane: always the owner's one-off approval, never auto, never a standing one
    effect, lane, content = ctx.seen[0]
    assert effect == "commit" and lane is None
    assert content == {"item": {"surface": RD, "kind": "post", "text": "Hi", "title": None, "targets": ["r/coffee"],
                                "attachments": [], "expires_at": "7 days after it is queued"}}
    # the approved retry (same arguments, same content) queues it, under the owner
    ok = Ctx(runtime, AGENT, approve=True)
    out = call(tools.engage_item_submit, ok, surface=RD, kind="post", text="Hi", targets=["r/coffee"])
    assert ok.seen[0][2] == content
    assert out["status"] == "queued" and out["submitted_by"] == "owner"
    assert out["requested_by"] == {"kind": "agent", "id": "agent-1", "owner": "owner"}
    [h] = rig.svc.store.history(RD, kinds=("item_submitted",))
    assert h["by"] == "owner" and h["payload"]["requested_by"]["id"] == "agent-1"
    assert h["payload"]["approval_id"] == "ap_1"
    # a routing table that let it through on its own does not queue an agent's item
    auto = Ctx(runtime, AGENT)
    auto.authorize = lambda effect, **kw: asyncio.sleep(0, NS(route="auto", via=None))
    with pytest.raises(PolicyDenied):
        call(tools.engage_item_submit, auto, surface=RD, kind="post", text="Hi again", targets=["r/coffee"])
    # out of the grant's lanes
    with pytest.raises(PolicyDenied):
        call(tools.engage_item_submit, Ctx(runtime, AGENT, approve=True, lanes="biz-b/"), surface=RD, kind="post",
             text="Hi", targets=["r/coffee"])
    assert len(rig.svc.store.items(RD)) == 1
    with pytest.raises(OwnerOnly):                        # the inbox itself refuses an agent with no approval
        rig.inbox.submit(rig.inbox.check(RD, "post", "x", targets=["r/coffee"]), requested_by=AGENT)


@pytest.mark.parametrize(("kw", "words"), [
    ({"surface": "biz-a/nothing"}, "no approved engagement program"),
    ({"kind": "like"}, "has no 'like' that takes words"),
    ({"kind": "join"}, "has no 'join' that takes words"),
    ({"text": "   "}, "no text"),
    ({"kind": "comment", "text": "x" * 141, "targets": [POST_URL]}, "at most 140"),
    ({"text": "x" * 40_001}, "at most 40000"),
    ({"title": "t" * 301}, "at most 300"),
    ({"kind": "comment", "text": "nice", "title": "no", "targets": [POST_URL]}, "has no title"),
    ({"targets": ["not a subreddit!"]}, "is not a subreddit name"),
    ({"targets": ["r/a", "r/b"]}, "one target per item"),
    ({"kind": "comment", "text": "nice", "targets": ["https://evil.example/r/x/comments/a/"]},
     "is not a Reddit post link"),
    ({"attachments": ["in_00aa11bb22cc"]}, "takes no attachments"),
    ({"expires_at": "2026-10-01T00:00:00Z"}, "in the past"),
    ({"expires_at": "2026-12-25T00:00:00Z"}, "more than 30 days"),
    ({"expires_at": "next week"}, "is not a time"),
])
def test_bad_items_are_refused_and_nothing_is_queued(tmp_path, kw, words):
    rig = Rig(tmp_path)
    args = {"surface": RD, "kind": "post", "text": "Hello", "targets": ["r/coffee"]} | kw
    with pytest.raises(InvalidRequest) as e:
        call(tools.engage_item_submit, Ctx(rig.runtime, OWNER_P, in_person=True), **args)
    assert words in str(e.value)
    assert rig.svc.store.items() == []
    assert "—" not in str(e.value)


def test_attachments_are_complete_images_or_videos_on_the_surfaces_lane(tmp_path):
    from agentos.engage.items import SITE_LIMITS, Limits

    rig = Rig(tmp_path, files=Files(in_00aa11bb22cc=NS(kind="image", status="complete", mime="image/png",
                                                       name="latte.png", size=2048, sha256="ab" * 32),
                                    in_00aa11bb22dd=NS(kind="pdf", status="complete", mime="application/pdf"),
                                    in_00aa11bb22ee=NS(kind="video", status="downloading", mime="video/mp4")))
    SITE_LIMITS[("reddit", "post")], saved = Limits(text=40_000, attachments=2, target=SITE_LIMITS[
        ("reddit", "post")].target, target_hint="a subreddit"), SITE_LIMITS[("reddit", "post")]
    try:
        ok = rig.inbox.check(RD, "post", "pic", targets=["r/coffee"], attachments=["in_00aa11bb22cc"])
        assert ok.attachments == ("in_00aa11bb22cc",)
        for fid, words in (("in_00aa11bb22dd", "images or videos only"), ("in_00aa11bb22ee", "is downloading"),
                           ("in_00aa11bb22ff", "no file in_00aa11bb22ff"), ("../etc/passwd", "not a file id")):
            with pytest.raises(InvalidRequest, match=words):
                rig.inbox.check(RD, "post", "pic", targets=["r/coffee"], attachments=[fid])
        rig.inbox.files = None
        with pytest.raises(InvalidRequest, match="not available"):
            rig.inbox.check(RD, "post", "pic", targets=["r/coffee"], attachments=["in_00aa11bb22cc"])
    finally:
        SITE_LIMITS[("reddit", "post")] = saved


@pytest.mark.parametrize(("registry", "words"), [
    (Registry(installed=lambda f: False), "no reddit connector with a comment flow is installed"),
    (Registry(installed=lambda f: True), "has not passed its canary"),
    (Registry(installed=lambda f: True, verified=lambda f: "x", rules=()), "no rule that checks"),
])
def test_an_item_whose_operation_cannot_run_is_refused(tmp_path, registry, words):
    rig = Rig(tmp_path)
    rig.inbox.registry = registry
    with pytest.raises(InvalidRequest, match=words) as e:
        call(tools.engage_item_submit, Ctx(rig.runtime, OWNER_P, in_person=True), surface=RD, kind="comment",
             text="nice", targets=[POST_URL])
    assert rig.svc.store.items() == [] and "—" not in str(e.value)


def test_an_item_checked_while_it_could_run_is_refused_if_it_no_longer_can(tmp_path):
    rig = Rig(tmp_path)
    plan = rig.inbox.check(RD, "comment", "nice", targets=[POST_URL])
    rig.inbox.registry = Registry(installed=lambda f: False)    # the connector was removed before approval
    with pytest.raises(InvalidRequest, match="could never go out"):
        rig.inbox.submit(plan, in_person=True)
    assert rig.svc.store.items() == []


def test_warnings_say_when_an_item_would_wait(tmp_path):
    rig = Rig(tmp_path, RD_POSTS_ONLY)
    plan = rig.inbox.check(RD, "comment", "nice", targets=[POST_URL])
    assert any("plans no comment" in w for w in plan.warnings)
    plan = rig.inbox.check(RD, "post", "hello")
    assert any("names no target" in w for w in plan.warnings)


# ---------------------------------------------------------------- the content port


def test_take_reserves_the_oldest_once_and_settle_uses_or_hands_back(tmp_path):
    rig = Rig(tmp_path)
    a = rig.add(text="first")
    rig.t += 60
    b = rig.add(text="second")
    rig.add(kind="comment", text="a comment")
    port = ItemsContent(rig.svc.store, clock=lambda: rig.t)
    [c] = port.take(RD, "post", 1)
    assert c.id == a["id"] and c.text == "first"
    assert [x.id for x in port.take(RD, "post", 5)] == [b["id"]]          # never the reserved one again
    assert port.take(RD, "post", 5) == []
    port.settle(RD, c, used=False)
    assert rig.svc.store.item(a["id"])["status"] == "queued"
    [again] = port.take(RD, "post", 1)
    port.settle(RD, again, used=True, run_id="run-9")
    item = rig.svc.store.item(a["id"])
    assert item["status"] == "used" and item["used_run_id"] == "run-9"
    [h] = rig.svc.store.history(RD, kinds=("item_used",))
    assert h["payload"] == {"id": a["id"], "run_id": "run-9"}
    port.settle(RD, again, used=False)                                    # a late settle changes nothing
    assert rig.svc.store.item(a["id"])["status"] == "used"


def test_concurrent_ticks_never_reserve_the_same_item_twice(tmp_path):
    rig = Rig(tmp_path)
    ids = {rig.add(text=f"post {i}")["id"] for i in range(20)}
    other = EngageStore(tmp_path)                       # a second connection, as another process would have
    ports = [ItemsContent(rig.svc.store, clock=lambda: rig.t), ItemsContent(other, clock=lambda: rig.t)]
    got: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker(n: int) -> None:
        barrier.wait()
        for _ in range(10):
            mine = ports[n % 2].take(RD, "post", 1)
            with lock:
                got.extend(c.id for c in mine)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    other.close()
    assert len(got) == len(set(got)) == 20 and set(got) == ids


def test_chained_content_asks_in_order_and_routes_settle_back(tmp_path):
    rig = Rig(tmp_path)
    item = rig.add(text="from the owner")
    drafts = FakeContent()                               # an endless drafter (brief B3 stands in)
    chain = ChainedContent([ItemsContent(rig.svc.store, clock=lambda: rig.t), drafts])
    got = chain.take(RD, "post", 3)
    assert [c.origin for c in got] == [ContentOrigin.ITEM, ContentOrigin.DRAFT, ContentOrigin.DRAFT]
    assert got[0].id == item["id"] and got[0].approved and not got[1].approved
    chain.settle(RD, got[1], used=True, run_id="r1")
    chain.settle(RD, got[2], used=False)
    chain.settle(RD, got[0], used=True, run_id="r2")
    assert drafts.used == [("c1", "r1")] and drafts.returned == ["c2"]
    assert rig.svc.store.item(item["id"])["status"] == "used"
    # after a restart the chain no longer remembers: the content's origin picks the port
    fresh = ChainedContent([ItemsContent(rig.svc.store, clock=lambda: rig.t), drafts])
    rig.add(text="another")
    [c] = ChainedContent([ItemsContent(rig.svc.store, clock=lambda: rig.t)]).take(RD, "post", 1)
    fresh.settle(RD, c, used=False)
    assert rig.svc.store.item(c.id)["status"] == "queued"
    fresh.settle(RD, Content(id="x", origin=ContentOrigin.DRAFT), used=False)
    assert drafts.returned == ["c2", "x"]


# ---------------------------------------------------------------- the executor


def test_a_session_consumes_the_item_without_asking_again_and_records_it(tmp_path):
    rig = Rig(tmp_path)
    item = rig.add(text="Our new roast is out", title="New roast")
    day, w = rig.windows_with("post")[0]
    rep = rig.run(day, w.idx)
    [post] = [o for o in rep["ops"] if o["kind"] == "post"]
    assert post["status"] == "verified" and post["content_origin"] == "item" and post["content_id"] == item["id"]
    assert post["target"] == "r/coffee"
    [c] = [c for c in rig.ops.calls if c.kind == "post"]
    assert not c.ask                                           # approved content: no second approval
    assert c.params == {"subreddit": "coffee", "title": "New roast", "body": "Our new roast is out"}
    done = rig.svc.store.item(item["id"])
    assert done["status"] == "used" and done["used_run_id"] == post["run_id"]
    kinds = [h["kind"] for h in rig.svc.store.history(RD)]
    assert "item_used" in kinds and "item_submitted" in kinds
    # the comment had no item: skipped, as before
    assert {o["reason"] for o in rep["ops"] if o["kind"] == "comment"} <= {"no_content"}


def test_a_failed_operation_that_provably_did_not_act_hands_the_item_back(tmp_path):
    rig = Rig(tmp_path)
    item = rig.add()
    rig.ops.script = {i: OpResult(OpStatus.FAILED, error="lane_not_found", acted=False) for i in range(10)}
    day, w = rig.windows_with("post")[0]
    rig.run(day, w.idx)
    assert rig.svc.store.item(item["id"])["status"] == "queued"


def test_items_add_no_work_one_post_a_day_uses_one_item_a_day(tmp_path):
    rig = Rig(tmp_path, RD_POSTS_ONLY)
    ids = [rig.add(text=f"post {i}", expires_at="2026-11-30T00:00:00Z")["id"] for i in range(5)]
    plans = rig.svc.plan(RD, days=4, start=MONDAY)
    rig.run_days(MONDAY, 4)
    per_day = Counter(c.idempotency_key.split(":")[2] for c in rig.ops.calls if c.kind == "post")
    planned = {p.day.isoformat(): p.totals().get("post", 0) for p in plans}
    assert all(n <= 1 for n in per_day.values()) and sum(per_day.values()) >= 2
    assert dict(per_day) == {d: n for d, n in planned.items() if n}             # exactly the plan's posts
    used = [i for i in ids if rig.svc.store.item(i)["status"] == "used"]
    assert used == ids[:sum(per_day.values())]                                  # oldest first
    assert all(rig.svc.store.item(i)["status"] == "queued" for i in ids[len(used):])
    assert all(p.caps["post"] == 1 for p in plans)                              # the caps are the plan's


# ---------------------------------------------------------------- expiry and abandoned reservations


def test_items_expire_with_one_history_entry_each(tmp_path):
    rig = Rig(tmp_path)
    soon = rig.add(expires_at=(rig.now + timedelta(hours=1)).isoformat())
    later = rig.add()
    rig.t += 2 * 3600
    assert ItemsContent(rig.svc.store, clock=lambda: rig.t).take(RD, "post", 5)[0].id == later["id"]  # never expired
    asyncio.run(rig.svc.tick())                       # the switch is off: the sweep still runs
    assert rig.svc.store.item(soon["id"])["status"] == "expired"
    asyncio.run(rig.svc.tick())
    [h] = rig.svc.store.history(RD, kinds=("item_expired",))
    assert h["payload"]["id"] == soon["id"]
    rig.svc.nightly()
    assert len(rig.svc.store.history(RD, kinds=("item_expired",))) == 1


def test_a_reservation_whose_window_ended_goes_back_to_the_queue_or_expires(tmp_path):
    rig = Rig(tmp_path)
    keep = rig.add(text="keep")
    lapse = rig.add(text="lapse", expires_at=(rig.now + timedelta(hours=1)).isoformat())

    class Crash(FakeOperations):
        async def run(self, *a, **kw):
            raise RuntimeError("the node went down mid window")

    rig.ex.operations = Crash()
    day, w = rig.windows_with("post")[0]
    rep = rig.run(day, w.idx)
    assert rep["status"] == "aborted"
    # it broke inside the run, which may have acted: the item is failed for the owner, never queued again
    assert rig.svc.store.item(keep["id"])["status"] == "failed"
    assert rig.svc.store.items(RD, status="reserved") == []
    out = rig.inbox.sweep()
    assert out["released"] == []
    assert rig.svc.store.item(lapse["id"])["status"] == "expired"           # it was past its expiry

    # a process that died mid-run leaves a reservation and an `attempted` op: the sweep fails it too
    again = rig.add(text="again")
    row = rig.svc.store.put_session(RD, MONDAY, 7, planned_start=rig.now, minutes=10)
    [op] = rig.svc.store.add_ops(row["id"], RD, MONDAY, ["post"])
    token = CONTENT_SESSION.set(row["id"])
    try:
        [c] = ItemsContent(rig.svc.store, clock=lambda: rig.t).take(RD, "post", 1)
    finally:
        CONTENT_SESSION.reset(token)
    assert c.id == again["id"]
    rig.svc.store.set_op(op["id"], status="attempted", content_origin="item", content_id=c.id)
    rig.svc.store.set_session(row["id"], status="aborted")
    out = rig.inbox.sweep()
    assert out["released"] == [] and out["failed"] == [again["id"]]
    assert rig.svc.store.item(again["id"])["status"] == "failed"
    assert [h["payload"]["id"] for h in rig.svc.store.history(RD, kinds=("item_released",))] == []


def test_a_reservation_inside_a_running_window_is_left_alone(tmp_path):
    rig = Rig(tmp_path)
    item = rig.add()
    row = rig.svc.store.put_session(RD, MONDAY, 0, planned_start=rig.now, minutes=10)
    rig.svc.store.set_session(row["id"], status="running")
    token = CONTENT_SESSION.set(row["id"])
    try:
        [c] = ItemsContent(rig.svc.store, clock=lambda: rig.t).take(RD, "post", 1)
    finally:
        CONTENT_SESSION.reset(token)
    assert rig.svc.store.item(item["id"])["reserved_by_window"] == row["id"]
    assert rig.inbox.sweep()["released"] == []
    rig.svc.store.set_session(row["id"], status="done")
    assert rig.inbox.sweep()["released"] == [c.id]
    # with no session named, a reservation is abandoned after two hours
    [c] = ItemsContent(rig.svc.store, clock=lambda: rig.t).take(RD, "post", 1)
    assert rig.inbox.sweep()["released"] == []
    rig.t += 3 * 3600
    assert rig.inbox.sweep()["released"] == [c.id]


# ---------------------------------------------------------------- withdraw and read


def test_withdraw_is_for_the_owner_or_whoever_asked_before_it_is_used(tmp_path):
    rig = Rig(tmp_path)
    runtime = rig.runtime
    mine = call(tools.engage_item_submit, Ctx(runtime, AGENT, approve=True), surface=RD, kind="post", text="a",
                targets=["r/coffee"])
    owners = rig.add(text="b")
    with pytest.raises(OwnerOnly):
        call(tools.engage_item_withdraw, Ctx(runtime, OTHER), item_id=mine["id"])
    with pytest.raises(OwnerOnly):
        call(tools.engage_item_withdraw, Ctx(runtime, AGENT), item_id=owners["id"])
    out = call(tools.engage_item_withdraw, Ctx(runtime, AGENT), item_id=mine["id"])
    assert out["status"] == "withdrawn"
    with pytest.raises(InvalidRequest, match="withdrawn"):
        call(tools.engage_item_withdraw, Ctx(runtime, OWNER_P, in_person=True), item_id=mine["id"])
    [c] = ItemsContent(rig.svc.store, clock=lambda: rig.t).take(RD, "post", 1)
    with pytest.raises(InvalidRequest, match="a session has it"):
        call(tools.engage_item_withdraw, Ctx(runtime, OWNER_P, in_person=True), item_id=owners["id"])
    ItemsContent(rig.svc.store, clock=lambda: rig.t).settle(RD, c, used=False)
    assert call(tools.engage_item_withdraw, Ctx(runtime, OWNER_P, in_person=True), item_id=owners["id"])["status"] == "withdrawn"
    with pytest.raises(NotFound):
        call(tools.engage_item_withdraw, Ctx(runtime, OWNER_P, in_person=True), item_id="it_0000000000000000")
    with pytest.raises(InvalidRequest):
        call(tools.engage_item_withdraw, Ctx(runtime, OWNER_P, in_person=True), item_id="nope")
    kinds = [(h["kind"], h["by"]) for h in rig.svc.store.history(RD, kinds=("item_withdrawn",))]
    assert kinds == [("item_withdrawn", "owner"), ("item_withdrawn", "agent-1")]


def test_engage_items_reads_only_what_the_grant_reaches(tmp_path):
    rig = Rig(tmp_path)
    runtime = rig.runtime
    a = rig.add()
    rig.svc.store.add_item({"id": "it_00000000000000bb", "surface": "biz-b/reddit", "kind": "post", "text": "x",
                            "expires_at": "2026-11-30T00:00:00+00:00", "submitted_by": "owner",
                            "submitted_at": "2026-11-01T00:00:00+00:00"})
    out = call(tools.engage_items, Ctx(runtime, AGENT))
    assert [i["id"] for i in out["items"]] == [a["id"]]
    one = call(tools.engage_items, Ctx(runtime, AGENT), item_id=a["id"])
    assert one["items"][0]["text"] == "Hello coffee people"
    assert call(tools.engage_items, Ctx(runtime, AGENT), surface=RD, status="used") == {"items": []}
    with pytest.raises(InvalidRequest):
        call(tools.engage_items, Ctx(runtime, AGENT), status="bogus")


def test_the_cli_lists_and_withdraws_items(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from agentos.engage.cli import app

    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path))
    rig = Rig(tmp_path)
    item = rig.add(text="A post from the owner")
    rig.svc.store.close()
    runner = CliRunner()
    out = runner.invoke(app, ["items", RD])
    assert out.exit_code == 0, out.output
    assert item["id"] in out.output and "queued" in out.output and "—" not in out.output
    out = runner.invoke(app, ["item", "withdraw", item["id"]])
    assert out.exit_code == 0, out.output
    assert "withdrawn" in out.output
    out = runner.invoke(app, ["items", RD, "--json"])
    assert '"withdrawn"' in out.output
    assert runner.invoke(app, ["item", "withdraw", item["id"]]).exit_code == 1


# ---------------------------------------------------------------- review fixes


def test_the_node_token_over_mcp_asks_the_owner_and_queues_nothing(tmp_path):
    """The node's MCP token resolves to the owner, and agents connect with it: it is a bearer
    token, never the owner in person, so an item it submits waits for the owner's approval like an
    agent's (the real PolicyService and wrap_tool, as the MCP layer calls them)."""
    from test_policy_support import OWNER as NODE_TOKEN
    from test_policy_support import Clock, make_service

    from agentos.policy.wrap import ToolCallError, bind_request_token, wrap_tool

    clock = Clock(datetime(2026, 11, 1, 23, 0, tzinfo=UTC).timestamp())
    policy, rt = make_service(tmp_path, clock=clock, owner=NODE_TOKEN)
    svc = EngageService(rt, home=tmp_path, clock=clock)
    svc.items.registry = Registry(installed=lambda f: True, verified=lambda f: "test canary")   # it can run
    rt.register("engage", svc)
    svc.store.put_persona(draw_persona(RD, "items-seed-1"))
    svc.approve(svc.draft(RD, RD_YAML, by=OWNER)["program"], by=OWNER)
    specs = {s.name: s for s in tools.tool_specs(rt)}
    submit = wrap_tool(specs["engage_item_submit"], rt)
    kw = {"surface": RD, "kind": "post", "text": "Posted with no card?", "targets": ["r/coffee"]}

    with bind_request_token(NODE_TOKEN):
        out = asyncio.run(submit(**kw))
    assert out["status"] == "approval_pending"
    assert svc.store.items() == [] and svc.store.history(RD, kinds=("item_submitted",)) == []
    ap = policy.approvals.get(out["approval_id"])
    assert ap.tool == "engage_item_submit" and ap.lane is None and ap.effect == "commit"

    # once the owner approves that card, the same call queues it, recorded with the approval
    policy.decide(out["approval_id"], "approve")
    with bind_request_token(NODE_TOKEN):
        item = asyncio.run(submit(**kw))
    assert item["status"] == "queued" and item["requested_by"] == {"kind": "human", "id": "owner", "owner": "owner"}
    assert svc.store.item(item["id"])["approval_id"] == out["approval_id"]
    assert policy.approvals.get(out["approval_id"]).state == "consumed"
    # that approval queues one item, once, and never another item's content
    plan = svc.items.check(RD, "post", "Posted with no card?", targets=["r/coffee"])
    with pytest.raises(OwnerOnly, match="already queued"):
        svc.items.submit(plan, requested_by=OWNER_P, approval_id=out["approval_id"])
    other = svc.items.check(RD, "post", "Something else", targets=["r/coffee"])
    with pytest.raises(OwnerOnly, match="different content"):
        svc.items.submit(other, requested_by=OWNER_P, approval_id=out["approval_id"])
    assert len(svc.store.items()) == 1 and len(svc.store.history(RD, kinds=("item_submitted",))) == 1

    # the owner in person (a signed-in panel session) queues at once, with no card
    ctx = policy.owner_context(tool="engage_item_submit")
    item = asyncio.run(tools.engage_item_submit(ctx, surface=RD, kind="post", text="From the panel",
                                                targets=["r/coffee"]))
    assert item["status"] == "queued" and item["requested_by"] is None

    # a program draft over the node token is an agent's draft: no owner-only parts
    draft = wrap_tool(specs["engage_program_draft"], rt)
    risky = "program: ig\nsurface: biz-a/instagram\nrisk: normal\nphases:\n  - kind: steady\n    daily: {like: 5}\n"
    with bind_request_token(NODE_TOKEN), pytest.raises(ToolCallError, match="owner_only"):
        asyncio.run(draft(surface="biz-a/instagram", program=risky))
    assert asyncio.run(tools.engage_program_draft(policy.owner_context(tool="engage_program_draft"),
                                                  surface="biz-a/instagram", program=risky))["program"]


def test_submit_checks_the_approval_is_this_item_used_once(tmp_path):
    rig = Rig(tmp_path)
    plan = rig.inbox.check(RD, "post", "Hi", targets=["r/coffee"])
    for ap_id, kw, words in (("ap_pending", {"state": "approved"}, "not approved and used"),
                             ("ap_tool", {"tool": "browser_act"}, "not an approval of an item"),
                             ("ap_question", {"kind": "question"}, "not an approval of an item")):
        rig.approvals.add(ap_id, plan.canonical, **kw)
        with pytest.raises(OwnerOnly, match=words):
            rig.inbox.submit(plan, requested_by=AGENT, approval_id=ap_id)
    with pytest.raises(OwnerOnly, match="no approval"):
        rig.inbox.submit(plan, requested_by=AGENT, approval_id="ap_nope")
    with pytest.raises(OwnerOnly):
        rig.inbox.submit(plan, requested_by=OWNER_P)            # the owner principal is not in person by itself
    rig.inbox.approvals = None
    with pytest.raises(OwnerOnly, match="not available"):
        rig.inbox.submit(plan, requested_by=AGENT, approval_id="ap_pending")
    assert rig.svc.store.items() == [] and rig.svc.store.history(RD, kinds=("item_submitted",)) == []


def test_only_item_content_skips_the_card(tmp_path):
    rig = Rig(tmp_path)
    forged = Content(id="d1", origin=ContentOrigin.DRAFT, text="a draft that says approved", title="t",
                     targets=("r/coffee",), approved=True)
    rig.ex.content = FakeContent(items=[forged], endless=False)
    day, w = rig.windows_with("post")[0]
    rig.run(day, w.idx)
    [c] = [c for c in rig.ops.calls if c.kind == "post"]
    assert c.ask                                           # a draft always asks, whatever it claims


def test_a_cancelled_window_does_not_strand_its_items(tmp_path):
    rig = Rig(tmp_path)
    keep = rig.add(text="will be cancelled mid-window")
    lapse = rig.add(text="lapses meanwhile", expires_at=(rig.now + timedelta(hours=30)).isoformat())

    class Cancelled(FakeOperations):
        async def run(self, *a, **kw):
            raise asyncio.CancelledError()          # what a daemon stop delivers to the window task

    rig.ex.operations = Cancelled()
    day, w = rig.windows_with("post")[0]
    rig.at(w.start)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(rig.ex.run_window(RD, day, w.idx))
    session = rig.svc.store.session(RD, day, w.idx)
    # cancelled inside its run, which may have acted: failed for the owner, never queued again
    assert rig.svc.store.items(RD, status="reserved") == []
    assert rig.svc.store.item(keep["id"])["status"] == "failed"
    out = rig.inbox.sweep(w.start + timedelta(minutes=w.minutes) + timedelta(hours=2, seconds=1))
    assert out["released"] == [] and rig.svc.store.item(keep["id"])["status"] == "failed"
    assert [h["payload"]["id"] for h in rig.svc.store.history(RD, kinds=("item_failed",))] == [keep["id"]]
    # a reservation past its expiry, whose window is over, expires with a history line
    rig.svc.store.set_session(session["id"], status="running")
    token = CONTENT_SESSION.set(session["id"])
    try:
        rig.at(datetime.fromisoformat(lapse["expires_at"]) - timedelta(minutes=1))
        got = ItemsContent(rig.svc.store, clock=lambda: rig.t).take(RD, "post", 5)
    finally:
        CONTENT_SESSION.reset(token)
    assert lapse["id"] in [c.id for c in got]
    out = rig.inbox.sweep(rig.now + timedelta(hours=1))
    assert lapse["id"] in out["expired"] and rig.svc.store.item(lapse["id"])["status"] == "expired"
    [h] = [h for h in rig.svc.store.history(RD, kinds=("item_expired",)) if h["payload"]["id"] == lapse["id"]]
    assert h["payload"]["reason"] == "its window ended without using it"


def test_a_reservation_names_the_window_that_took_it_not_the_newest(tmp_path):
    rig = Rig(tmp_path)
    item = rig.add()
    a = rig.svc.store.put_session(RD, MONDAY, 0, planned_start=rig.now, minutes=600)
    b = rig.svc.store.put_session(RD, MONDAY, 1, planned_start=rig.now, minutes=600)
    for row in (a, b):
        rig.svc.store.set_session(row["id"], status="running")
    token = CONTENT_SESSION.set(a["id"])
    try:
        ItemsContent(rig.svc.store, clock=lambda: rig.t).take(RD, "post", 1)
    finally:
        CONTENT_SESSION.reset(token)
    assert rig.svc.store.item(item["id"])["reserved_by_window"] == a["id"]
    rig.svc.store.set_session(a["id"], status="aborted")
    assert rig.inbox.sweep()["released"] == [item["id"]]          # b still running changes nothing


def test_attachments_are_approved_as_the_files_themselves(tmp_path):
    from agentos.engage.items import SITE_LIMITS, Limits

    png = NS(kind="image", status="complete", mime="image/png", name="latte.png", size=2048, sha256="ab" * 32)
    files = Files(in_00aa11bb22cc=png)
    rig = Rig(tmp_path, files=files)
    saved = SITE_LIMITS[("reddit", "post")]
    SITE_LIMITS[("reddit", "post")] = Limits(text=40_000, attachments=2, target=saved.target, target_hint="a subreddit")
    try:
        plan = rig.inbox.check(RD, "post", "pic", targets=["r/coffee"], attachments=["in_00aa11bb22cc"])
        assert plan.canonical["item"]["attachments"] == [{"id": "in_00aa11bb22cc", "name": "latte.png",
                                                          "mime": "image/png", "size": 2048, "sha256": "ab" * 32}]
        assert "latte.png (image/png, 2.0 KB, sha256 abababababababab)" in plan.summary
        assert "—" not in plan.summary
        # an approval of these bytes does not queue other bytes under the same file id
        rig.approvals.add("ap_png", plan.canonical)
        files.found[(RD, "in_00aa11bb22cc")] = NS(**{**vars(png), "sha256": "cd" * 32})
        swapped = rig.inbox.check(RD, "post", "pic", targets=["r/coffee"], attachments=["in_00aa11bb22cc"])
        with pytest.raises(OwnerOnly, match="different content"):
            rig.inbox.submit(swapped, requested_by=AGENT, approval_id="ap_png")
        item = rig.inbox.submit(plan, requested_by=AGENT, approval_id="ap_png")
        assert item["files"][0]["sha256"] == "ab" * 32 and item["attachments"] == ["in_00aa11bb22cc"]
        files.found[(RD, "in_00aa11bb22cc")] = NS(**{**vars(png), "sha256": None})
        with pytest.raises(InvalidRequest, match="no sha256"):
            rig.inbox.check(RD, "post", "pic", targets=["r/coffee"], attachments=["in_00aa11bb22cc"])
    finally:
        SITE_LIMITS[("reddit", "post")] = saved


def test_engage_items_filters_by_the_grants_surfaces_before_the_limit(tmp_path):
    rig = Rig(tmp_path)
    for n in range(205):                                    # older than anything the grant may see
        rig.svc.store.add_item({"id": f"it_{n:016x}", "surface": "biz-b/reddit", "kind": "post", "text": "x",
                                "expires_at": "2026-11-30T00:00:00+00:00", "submitted_by": "owner",
                                "submitted_at": "2026-10-01T00:00:00+00:00"})
    mine = rig.add()
    out = call(tools.engage_items, Ctx(rig.runtime, AGENT))
    assert [i["id"] for i in out["items"]] == [mine["id"]]
    assert len(call(tools.engage_items, Ctx(rig.runtime, AGENT, lanes="biz-"))["items"]) == 200
    assert call(tools.engage_items, Ctx(rig.runtime, AGENT, lanes="biz-c/")) == {"items": []}


@pytest.mark.parametrize(("value", "words"), [
    ("2026-11-12T18:00:00", "has no time zone"),
    ("2026-11-12 18:00", "has no time zone"),
    ("2026-11-12", "date without a time"),
])
def test_expires_at_needs_a_time_zone(tmp_path, value, words):
    rig = Rig(tmp_path)
    with pytest.raises(InvalidRequest, match=words) as e:
        rig.inbox.check(RD, "post", "Hello", targets=["r/coffee"], expires_at=value)
    assert "—" not in str(e.value)
    assert rig.inbox.check(RD, "post", "Hello", targets=["r/coffee"], expires_at="2026-11-12T18:00:00Z").expires_at


def test_an_item_waits_with_its_approval_card_and_follows_the_decision(tmp_path):
    rig = Rig(tmp_path)
    denied = rig.add(text="denied one")
    rig.t += 1
    approved = rig.add(text="approved one")
    rig.ops.script = {i: OpResult(OpStatus.AWAITING_APPROVAL) for i in range(20)}
    (d1, w1), (d2, w2) = rig.windows_with("post")[:2]
    rig.run(d1, w1.idx)
    first = rig.svc.store.item(denied["id"])
    assert first["status"] == "awaiting" and first["used_run_id"]
    assert ItemsContent(rig.svc.store, clock=lambda: rig.t).take(RD, "post", 5)[0].id == approved["id"]
    rig.svc.store.settle_item(approved["id"], "queued", now=rig.now)
    rig.run(d2, w2.idx)
    second = rig.svc.store.item(approved["id"])
    assert second["status"] == "awaiting"
    assert rig.inbox.sweep(rig.now)["released"] == []           # awaiting is not a stranded reservation
    rig.ops.later = {first["used_run_id"]: OpResult(OpStatus.FAILED, error="denied"),
                     second["used_run_id"]: OpResult(OpStatus.VERIFIED)}
    asyncio.run(rig.ex.reconcile())
    out = rig.inbox.sweep()
    assert out["requeued"] == [denied["id"]] and out["used"] == [approved["id"]]
    assert rig.svc.store.item(denied["id"])["status"] == "queued"
    assert rig.svc.store.item(denied["id"])["used_run_id"] is None
    assert rig.svc.store.item(approved["id"])["status"] == "used"
    [h] = rig.svc.store.history(RD, kinds=("item_requeued",))
    assert h["payload"]["id"] == denied["id"] and h["payload"]["reason"] == "denied"


def test_a_failed_run_that_may_have_acted_is_left_for_the_owner(tmp_path):
    rig = Rig(tmp_path)
    item = rig.add()
    rig.ops.script = {i: OpResult(OpStatus.FAILED, error="internal") for i in range(10)}    # acted unknown
    day, w = rig.windows_with("post")[0]
    rig.run(day, w.idx)
    done = rig.svc.store.item(item["id"])
    assert done["status"] == "failed" and done["used_run_id"]
    [h] = rig.svc.store.history(RD, kinds=("item_failed",))
    assert h["payload"] == {"id": item["id"], "run_id": done["used_run_id"], "reason": "internal"}
    assert ItemsContent(rig.svc.store, clock=lambda: rig.t).take(RD, "post", 5) == []      # never offered again
    assert rig.inbox.sweep(rig.now + timedelta(days=30))["expired"] == []


def test_the_adapter_says_a_failed_run_did_not_act_only_with_proof():
    from agentos.engage.adapters import result_of

    started = datetime(2026, 11, 2, tzinfo=UTC)
    clean = NS(status="failed", error={"code": "denied"}, output=None, id="r1", started_at=None, proof=None,
               decision=None)
    assert result_of(clean).acted is False                                   # never started
    proved = NS(**{**vars(clean), "started_at": started, "proof": {"acted": 0, "writes": 0}})
    assert result_of(proved).acted is False
    crashed = NS(**{**vars(clean), "started_at": started, "error": {"code": "internal"}})
    assert result_of(crashed).acted is None                                  # no proof
    decided = NS(**{**vars(proved), "decision": {"choice": "fail", "path": "3"}})
    assert result_of(decided).acted is None                                  # a partial the owner failed


@pytest.mark.parametrize(("site", "kind", "value", "ok"), [
    ("reddit", "comment", POST_URL, True),
    ("reddit", "comment", "https://old.reddit.com/r/coffee/comments/abc123", True),
    ("reddit", "comment", "https://www.reddit.com@evil.tld/r/coffee/comments/abc123/", False),
    ("reddit", "comment", "https://user:pw@www.reddit.com/r/coffee/comments/abc123/", False),
    ("reddit", "comment", "https://www.reddit.com.evil.tld/r/coffee/comments/abc123/", False),
    ("reddit", "comment", "https://www.reddit.com:8443/r/coffee/comments/abc123/", False),
    ("reddit", "comment", "http://www.reddit.com/r/coffee/comments/abc123/", False),
    ("reddit", "post", "r/ab", False),
    ("reddit", "post", "r/abc", True),
    ("linkedin", "comment", "https://www.linkedin.com/feed/update/urn:li:activity:1/", True),
    ("linkedin", "comment", "https://www.linkedin.com@evil.tld/feed/update/x", False),
    ("instagram", "comment", "https://www.instagram.com/p/abc/", True),
    ("instagram", "comment", "https://www.instagram.com@evil.tld/x", False),
    ("instagram", "comment", "https://evil.tld/p/abc/", False),
    ("blog", "comment", "https://my.blog.example/post/1", True),
])
def test_targets_are_parsed_and_the_host_compared_exactly(site, kind, value, ok):
    from agentos.engage.items import limits_for

    check = limits_for(site, kind).target
    assert check is not None and check(value) is ok
