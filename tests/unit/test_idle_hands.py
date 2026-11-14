"""The pointer's drift while the model thinks (node/human.py), and what must always beat it."""

from __future__ import annotations

import asyncio

import pytest

from agentos.node import human


class Mouse:
    def __init__(self, page, move_s=0.0):
        self.page, self.move_s, self.moves = page, move_s, []
        self.moving = False

    async def move(self, x, y):
        self.moving = True
        await asyncio.sleep(self.move_s)
        self.moves.append((x, y))
        self.moving = False

    async def wheel(self, dx, dy):
        assert not self.moving, "input while a drift was in flight"


class Context:
    pass


class Page:
    def __init__(self, move_s=0.0):
        self.context, self.mouse = Context(), Mouse(self, move_s)

    async def evaluate(self, *_):
        return {"w": 1000, "h": 800}

    async def wait_for_timeout(self, ms):
        await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(human, "_draw_ms", lambda lo, hi: 1)
    monkeypatch.setattr(human, "_idle_span_s", lambda: 30.0)

    async def size(page):
        return 1000.0, 800.0
    monkeypatch.setattr(human, "_inner_size", size)


def allow(page, fn=lambda: True):
    human.IDLE_LANES[page.context] = fn


async def settle(seconds=0.05):
    await asyncio.sleep(seconds)


def test_a_lane_that_was_not_allowed_never_drifts():
    async def go():
        page = Page()
        human.begin_idle(page)
        await settle()
        assert page.mouse.moves == []
    asyncio.run(go())


def test_the_pointer_drifts_inside_the_page_after_an_action():
    async def go():
        page = Page()
        allow(page)
        human.begin_idle(page)
        await settle()
        await human.still(page)
        assert page.mouse.moves
        assert all(50 <= x <= 950 and 64 <= y <= 760 for x, y in page.mouse.moves)
    asyncio.run(go())


def test_still_waits_out_a_move_in_flight():
    async def go():
        page = Page(move_s=0.05)
        allow(page)
        human.begin_idle(page)
        await asyncio.sleep(0.02)
        assert page.mouse.moving
        await human.still(page)
        assert not page.mouse.moving
        count = len(page.mouse.moves)
        await settle()
        assert len(page.mouse.moves) == count  # and nothing after it
    asyncio.run(go())


def test_a_wheel_never_overlaps_a_drift():
    async def go():
        page = Page(move_s=0.03)
        allow(page)
        human.begin_idle(page)
        await asyncio.sleep(0.01)
        await human._wheel(page, 100)  # Mouse.wheel asserts
    asyncio.run(go())


def test_a_lane_a_human_took_stops_moving():
    async def go():
        held = {"agents": True}
        page = Page()
        allow(page, lambda: held["agents"])
        human.begin_idle(page)
        await settle()
        held["agents"] = False
        await settle()
        count = len(page.mouse.moves)
        await settle()
        assert len(page.mouse.moves) == count
    asyncio.run(go())


def test_the_drift_ends_by_itself(monkeypatch):
    monkeypatch.setattr(human, "_idle_span_s", lambda: 0.02)

    async def go():
        page = Page()
        allow(page)
        human.begin_idle(page)
        await settle()
        count = len(page.mouse.moves)
        await settle()
        assert len(page.mouse.moves) == count
    asyncio.run(go())


def test_a_closed_page_ends_the_drift_quietly():
    async def go():
        page = Page()
        allow(page)

        async def gone(x, y):
            raise RuntimeError("Target closed")
        page.mouse.move = gone
        human.begin_idle(page)
        await settle()
        await human.still(page)
    asyncio.run(go())


def test_a_second_action_does_not_start_a_second_drift():
    async def go():
        page = Page()
        allow(page)
        human.begin_idle(page)
        first = human._IDLE[page]
        human.begin_idle(page)
        assert human._IDLE[page] is first
        await human.still(page)
    asyncio.run(go())


def test_forget_context_drops_a_closed_lanes_entries():
    from agentos.node.engine_camoufox import forget_context

    async def go():
        page = Page()
        allow(page)
        human.POINTERS[page] = (1.0, 2.0)
        human.begin_idle(page)
        await human.still(page)
        forget_context(page.context)
        assert page.context not in human.IDLE_LANES and page not in human.POINTERS
    asyncio.run(go())


def test_a_short_reach_never_overshoots():
    async def go():
        page = Page()
        human.POINTERS[page] = (100.0, 100.0)
        for _ in range(50):
            await human._overshoot(page, 150.0, 120.0)
        assert page.mouse.moves == []
    asyncio.run(go())


def test_a_long_reach_goes_past_the_target_along_the_line(monkeypatch):
    monkeypatch.setattr(human.random, "random", lambda: 0.0)

    async def go():
        page = Page()
        human.POINTERS[page] = (100.0, 100.0)
        await human._overshoot(page, 500.0, 100.0)
        (x, y), = page.mouse.moves
        assert 512 <= x <= 536 and y == 100.0
    asyncio.run(go())


def test_a_pause_is_this_lanes_own_band(monkeypatch):
    """One call site, two lanes: the band is each lane's, and it scales the whole band, not its floor
    or its ceiling alone."""
    monkeypatch.setattr(human, "lane_offset", lambda page: 0.4)
    slow = human._locate(Page(), 90, 260)
    monkeypatch.setattr(human, "lane_offset", lambda page: -0.4)
    quick = human._locate(Page(), 90, 260)
    assert slow[0] > 90 and slow[1] > 260  # both bounds move, by the same share
    assert quick[0] < 90 and quick[1] < 260
    # the floor keeps a draw from reading as a constant, and scales with the lane like the rest
    assert human._locate(Page(), 0, 0)[0] >= round(human.SECONDS_FLOOR / 1.492)


def test_a_rest_lands_in_the_lanes_band_and_never_past_the_area(monkeypatch):
    """The band is the lane's own, and it stays on the area however wide it gets."""
    area = {"x": 0.0, "y": 0.0, "width": 1000.0, "height": 800.0}

    async def rest_at(offset):
        monkeypatch.setattr(human.random, "gauss", lambda mu, sigma: offset)
        page = Page()
        await human._rest(page, area)
        return page.mouse.moves[-1]

    def in_band(point, offset):
        x, y = point
        (xlo, xhi), (ylo, yhi) = human.rest_span(offset)
        assert 0 <= x <= 1000 and 0 <= y <= 800  # never past the area
        assert 1000 * xlo <= x <= 1000 * xhi and 800 * ylo <= y <= 800 * yhi  # and in its own band

    async def go():
        for _ in range(40):  # the draw is free, so check it over many
            in_band(await rest_at(0.0), 0.0)
            in_band(await rest_at(0.12), 0.12)  # a wide lane stays on the page all the same
        narrow, wide = human.rest_span(0.0), human.rest_span(0.12)
        assert narrow[0][0] > wide[0][0] and narrow[0][1] < wide[0][1]  # the wide one is wider
        assert 0.25 <= narrow[0][0] and narrow[0][1] <= 0.75  # the code's own band is around the middle
    asyncio.run(go())


def test_the_span_differs_and_stays_short(monkeypatch):
    monkeypatch.undo()
    spans = {human._idle_span_s() for _ in range(200)}
    assert len(spans) > 50 and min(spans) >= 3 and max(spans) <= human.IDLE_FOR_S


class Box:
    def __init__(self, y, hits):
        self.y, self.hits = y, hits

    async def bounding_box(self):
        return {"x": 100.0, "y": self.y, "width": 80.0, "height": 30.0}

    async def evaluate(self, *_):
        return self.hits.pop(0) if self.hits else True


def covered(times):
    return [False] * times


def test_a_target_under_a_sticky_bar_gets_one_nudge(monkeypatch):
    page = Page()
    box = Box(30.0, covered(5))
    wheels = []
    page.mouse.wheel = lambda dx, dy: wheels.append(dy) or asyncio.sleep(0)

    async def go():
        await human._aim(page, box)
    asyncio.run(go())
    assert wheels and all(w < 0 for w in wheels)  # up, so the target comes down from under the bar


def test_a_target_covered_mid_page_is_a_dialog_and_stays_an_error():
    page = Page()
    box = Box(380.0, covered(50))

    async def go():
        await human._aim(page, box)
    with pytest.raises(human.TargetCovered):
        asyncio.run(go())


def test_it_nudges_once_and_then_tells_the_agent():
    page = Page()
    box = Box(30.0, covered(50))

    async def go():
        await human._aim(page, box)
    with pytest.raises(human.TargetCovered):
        asyncio.run(go())
