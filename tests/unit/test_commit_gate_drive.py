"""The commit gate covers the agent's whole drive (AUD-03, docs/design/egress-guard.md section 8):
the lane manager ends the drive when control leaves the agent, a publish that arrived by a
redirect holds the lane, and uploads are remembered on every path. Fake requests and contexts
(test_egress), the lane manager with fake sessions (test_lanes)."""

from __future__ import annotations

import asyncio

from test_commit_gate_guard import Gate
from test_egress import AGENT, COMMITTER, FakeContext, FakePage, FakeRequest, FakeRoute, make_guard
from test_egress_wiring import GuardedEngine
from test_lanes import make

from agentos.core.models import ControlState, LaneSpec
from agentos.node.claims import Holder
from agentos.node.routes import RouteDispatcher

LANE = "biz-a/instagram"
CONFIGURE = "https://i.instagram.com/api/v1/media/configure/"
RUPLOAD = "https://i.instagram.com/rupload_igphoto/fb_uploader_1699999999"


async def _driving_lane(tmp_path):
    mgr, engine, _, _ = make(tmp_path, engine=GuardedEngine(), self_check=False)
    await mgr.create(LaneSpec(id=LANE))
    async with mgr.using(LANE):
        pass
    guard = engine.sessions[-1].guard
    ctx = FakeContext()
    guard.attach(ctx, RouteDispatcher(ctx))
    guard.drive(COMMITTER)
    async with guard.acting(gate=Gate()):
        pass
    assert guard.driving() and len(ctx.routes) == 1
    return mgr, guard, ctx


async def test_a_take_over_ends_the_drive_and_a_hand_back_does_not_start_one(tmp_path):
    mgr, guard, ctx = await _driving_lane(tmp_path)
    await mgr.take_control(LANE, by="owner")
    await asyncio.sleep(0)
    assert not guard.driving() and ctx.routes == []
    await mgr.hand_back(LANE, by="owner")
    assert not guard.driving()  # the agent's next act starts the next drive


async def test_a_stop_and_a_released_claim_end_the_drive(tmp_path):
    mgr, guard, _ = await _driving_lane(tmp_path)
    await mgr.stop_agent(LANE, by="owner")
    await asyncio.sleep(0)
    assert not guard.driving() and mgr.control(LANE).state == ControlState.PAUSED

    mgr, guard, ctx = await _driving_lane(tmp_path / "b")
    holder = Holder(key="s1", label="claude", identity="claude")
    await mgr.claim(LANE, holder, purpose="a post")
    async with guard.acting(gate=Gate()):
        pass
    await mgr.release(LANE, holder)  # the agent hands the lane back
    await asyncio.sleep(0)
    assert not guard.driving() and ctx.routes == []


async def test_closing_the_lane_ends_the_drive(tmp_path):
    mgr, guard, _ = await _driving_lane(tmp_path)
    await mgr.close(LANE, principal_kind="human")
    await asyncio.sleep(0)
    assert not guard.driving()


async def test_a_publish_that_arrives_by_a_redirect_during_the_drive_holds_the_lane(tmp_path):
    """The review's probe 13: a hop is never routed. Its first hop on a rule host is (and an
    unknown POST there is held), and a publish that arrives by a hop anyway is a violation."""
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    held: list = []

    async def violation(block):
        held.append(block)

    guard.on_violation = violation
    gate = Gate()
    async with guard.acting(gate=gate):
        first = FakeRequest("https://www.instagram.com/submit-form", "POST", headers={"content-type": "application/json"},
                            body=b"{}")
        route = FakeRoute()
        await ctx.routes[0][1](route, first)
    assert route.aborted and gate.seen == ["instagram.unknown"]  # the first hop never left
    first_off_site = FakeRequest("https://evil.example/go", "POST")
    hop = FakeRequest(CONFIGURE, "POST", body=b"caption=x", hop=first_off_site)
    ctx.emit("request", hop)
    await asyncio.sleep(0)
    [block] = held
    assert (block.rule, block.prevented, block.effect_rule, block.kind) == ("commit_gate", False, "instagram.post",
                                                                          "request")


def _photo(upload_id: str, data: bytes) -> FakeRequest:
    body = (b'--BB\r\nContent-Disposition: form-data; name="upload_id"\r\n\r\n' + upload_id.encode()
            + b'\r\n--BB\r\nContent-Disposition: form-data; name="photo"; filename="a.jpg"\r\n\r\n' + data
            + b"\r\n--BB--\r\n")
    return FakeRequest(RUPLOAD, "POST", headers={"content-type": "multipart/form-data; boundary=BB"}, body=body)


async def test_a_strict_lane_remembers_its_uploads(tmp_path):
    """The review's probe 14: under the full route the listener used to return before it read the upload."""
    guard, ctx, _, _ = make_guard(tmp_path, sites=("www.instagram.com", "i.instagram.com"), driver=COMMITTER,
                                  strict=True)
    async with guard.acting(gate=Gate()):
        ctx.emit("request", _photo("1699999999", b"PHOTO_A_BYTES"))
    [up] = guard.uploads
    assert up.size == len(b"PHOTO_A_BYTES") and "1699999999" in up.ids


async def test_a_capped_act_remembers_its_uploads(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)

    class Capped:
        def charge(self, rule, *, sent):
            return "past the limit"

        def capped(self):
            return True

    async with guard.acting(gate=Gate(), meter=Capped()):
        ctx.emit("request", _photo("1699999999", b"PHOTO_A_BYTES"))
    assert len(guard.uploads) == 1


async def test_a_publish_held_between_the_agents_calls_is_reported_by_its_next_act(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    async with guard.acting(gate=Gate()):
        pass
    await ctx.routes[0][1](FakeRoute(), FakeRequest(CONFIGURE, "POST"))  # the page's own timer, between calls
    async with guard.acting(gate=Gate()) as mark:
        pass
    [block] = guard.blocks_since(mark)
    assert (block.rule, block.effect_rule) == ("commit_gate", "instagram.post")
    async with guard.acting(gate=Gate()) as mark:
        pass
    assert guard.blocks_since(mark) == []  # told once


async def test_a_flow_run_during_a_drive_is_not_held_as_the_agents_publish(tmp_path):
    """A flow is authorized at its own effect (its approval or proof binds the flow): while it holds
    the lane, the drive's route lets its requests go to the listener, never gated or charged as
    the agent's publish, and the agent's draft-only grant does not judge them. The drive's route
    stays on and gates again once the flow lets go."""
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=AGENT)  # the agent may only draft
    held: list = []
    guard.on_violation = held.append
    gate = Gate()
    async with guard.acting(gate=gate):
        pass
    [(_, handler)] = ctx.routes
    page = FakePage()
    async with guard.flow(page):
        assert page.reloads == 1  # the page the agent drove starts clean, before the flow's window opens
        route, post = FakeRoute(), FakeRequest(CONFIGURE, "POST", frame=page.main_frame)
        await handler(route, post)
        assert not route.aborted  # the flow's publish goes on
        ctx.emit("request", post)  # the listener sees it, as before the drive
        unknown = FakeRequest("https://i.instagram.com/api/v1/some/new/endpoint/", "PUT", frame=page.main_frame)
        await handler(other := FakeRoute(), unknown)
        assert not other.aborted
        ctx.emit("request", unknown)
        assert guard.driving()
    await asyncio.sleep(0)
    assert gate.seen == [] and held == [] and guard.blocks_since(0) == []
    assert guard.stats["observed_commits"] == 1 and guard.stats["gated"] == 0

    guard.drive(COMMITTER)
    after = FakeRoute()
    await handler(after, FakeRequest(CONFIGURE, "POST"))  # the flow let go: the drive gates again
    assert after.aborted and gate.seen == ["instagram.post"]


async def test_a_timer_the_agent_armed_cannot_publish_in_a_flows_window(tmp_path):
    """The review's probe: an agent presses Share on a page whose timer publishes later, then starts
    a flow on the lane. The flow reloads its page before its window opens (the old document's timer
    dies with it, its unload is still gated), and a request from any page the flow did not reload
    (another tab, a page that could not be reloaded) stays gated as the agent's."""
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    gate = Gate()
    page, other = FakePage(), FakePage("https://www.instagram.com/p/x/")
    async with guard.acting(gate=gate):
        pass  # the agent's press armed the page's timer
    [(_, handler)] = ctx.routes
    async with guard.flow(page):
        for timer in (FakeRequest(CONFIGURE, "POST", frame=other.main_frame),  # another tab's timer
                      FakeRequest(CONFIGURE, "POST", no_frame=True)):  # a request with no page (a worker)
            ctx.emit("request", timer)
            await handler(route := FakeRoute(), timer)
            assert route.aborted
        flows = FakeRequest(CONFIGURE, "POST", frame=page.main_frame)
        await handler(route := FakeRoute(), flows)
        assert not route.aborted
    assert gate.seen == ["instagram.post", "instagram.post"]

    stuck = FakePage()
    stuck.reload_fails = True  # its old document may still run: nothing of it is the flow's
    async with guard.flow(stuck):
        await handler(route := FakeRoute(), FakeRequest(CONFIGURE, "POST", frame=stuck.main_frame))
        assert route.aborted
    assert gate.seen[-1] == "instagram.post"


async def test_a_flow_with_no_drive_on_does_not_reload_its_page(tmp_path):
    guard, _, _, _ = make_guard(tmp_path, sites=(), driver=None)
    page = FakePage()
    async with guard.flow(page):
        assert guard._flow_owns(FakeRequest(CONFIGURE, "POST"))
    assert page.reloads == 0 and not guard._flow_owns(FakeRequest(CONFIGURE, "POST"))


async def test_a_flows_non_get_is_still_held_to_the_lanes_site_list(tmp_path):
    """The review's probe: a flow is not the agent, yet a POST off the lane's site list is refused
    during a flow as it is outside one (the listener records it), with or without a drive."""
    for drive in (True, False):
        guard, ctx, _, _ = make_guard(tmp_path / str(drive), sites=("www.reddit.com",), driver=COMMITTER)
        if drive:
            async with guard.acting(gate=Gate()):
                pass
        page = FakePage("https://www.reddit.com/")
        async with guard.flow(page):
            ctx.emit("request", FakeRequest("https://collector.example/x", "POST", frame=page.main_frame))
            ctx.emit("request", FakeRequest("https://www.reddit.com/svc/x", "POST", frame=page.main_frame))
        [block] = guard.blocks_since(0)
        assert (block.rule, block.host, block.method) == ("non_get", "collector.example", "POST")
        assert "this lane allows" in block.reason
