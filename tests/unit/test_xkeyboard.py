"""Lithuanian letters as real key presses, notch-by-notch wheels and skewed pauses (node/human.py,
node/xkeyboard.py). What the browser does with them is checked by the red team on a node."""

from __future__ import annotations

import asyncio
import statistics

import pytest

from agentos.node import human, xkeyboard
from agentos.node.xkeyboard import KEYSYMS, XKeyboard


class Keyboard:
    def __init__(self):
        self.log: list[tuple[str, str]] = []

    async def down(self, key):
        self.log.append(("down", key))

    async def up(self, key):
        self.log.append(("up", key))

    async def type(self, ch):
        self.log.append(("type", ch))

    async def insert_text(self, ch):
        self.log.append(("insert", ch))


class Mouse:
    def __init__(self):
        self.wheels: list[int] = []

    async def wheel(self, dx, dy):
        self.wheels.append(dy)


class Context:
    pass


class Page:
    def __init__(self):
        self.keyboard, self.mouse, self.context = Keyboard(), Mouse(), Context()
        self.waits: list[int] = []

    async def wait_for_timeout(self, ms):
        self.waits.append(ms)


class FakeX(XKeyboard):
    def __init__(self, works=True):
        super().__init__(":1")
        self.works, self.pressed = works, []

    async def press(self, ch, hold_ms):
        self.pressed.append(ch)
        self.broken = not self.works
        return self.works


def test_a_lithuanian_letter_is_a_key_press_on_the_x_keyboard():
    page = Page()
    x = FakeX()
    human.X_KEYBOARDS[page.context] = x
    asyncio.run(human.type_text(page, "aąb"))
    assert x.pressed == ["ą"]
    assert not [e for e in page.keyboard.log if e[0] in ("type", "insert")]


def test_without_an_x_keyboard_the_letter_goes_in_as_before():
    page = Page()
    asyncio.run(human.type_text(page, "ą"))
    assert ("type", "ą") in page.keyboard.log


def test_a_failed_press_still_gets_the_letter_in_once_and_stops_trying():
    page = Page()
    x = FakeX(works=False)
    human.X_KEYBOARDS[page.context] = x
    asyncio.run(human.type_text(page, "ąč"))
    assert x.pressed == ["ą"]
    assert [e for e in page.keyboard.log if e[0] == "type"] == [("type", "ą"), ("type", "č")]


def test_an_emoji_stays_a_text_insertion():
    page = Page()
    x = FakeX()
    human.X_KEYBOARDS[page.context] = x
    asyncio.run(human.type_text(page, "😀"))
    assert x.pressed == []
    assert ("insert", "😀") in page.keyboard.log


def test_every_letter_has_both_cases():
    assert {c.upper() for c in KEYSYMS if c.islower()} == {c for c in KEYSYMS if c.isupper()}


def test_prepare_gives_up_quietly_without_the_tools(monkeypatch):
    monkeypatch.setattr(xkeyboard.shutil, "which", lambda _: None)
    assert asyncio.run(xkeyboard.prepare(":1")) is None


def test_the_keyboard_holds_no_reference_to_a_page():
    assert set(vars(XKeyboard(":1", "/x"))) == {"display", "xauthority", "broken"}


def test_forget_context_drops_the_keyboard():
    from agentos.node.engine_camoufox import forget_context

    context = Context()
    human.X_KEYBOARDS[context] = XKeyboard(":1")
    forget_context(context)
    assert context not in human.X_KEYBOARDS


@pytest.fixture
def steady(monkeypatch):
    """A lane with no offset of its own, so a gesture is the code's own shape."""
    monkeypatch.setattr(human, "lane_offset", lambda page: 0.0)


def notches_of(gesture: list[int]) -> int:
    """Notches a gesture asked for: a call asks about ASK_PX a notch, so it rounds to the next."""
    return sum(max(1, round(abs(w) / human.ASK_PX)) for w in gesture)


@pytest.mark.parametrize("dy,notches", [(132, 1), (400, 3), (-600, 5)])
def test_a_wheel_gesture_asks_for_notches_a_call(steady, dy, notches):
    page = Page()
    asyncio.run(human._wheel(page, dy))
    assert notches_of(page.mouse.wheels) == notches
    assert all((w > 0) == (dy > 0) for w in page.mouse.wheels)
    assert len(page.waits) == len(page.mouse.wheels) - 1


def test_no_call_is_under_one_notch_and_the_ask_varies_around_it(steady):
    page = Page()
    asyncio.run(human._wheel(page, 1200))
    assert all(abs(w) >= human.ASK_PX * 0.8 for w in page.mouse.wheels)  # never a sliver of a call
    assert len(set(page.mouse.wheels)) > 1  # nor one size repeated


def test_a_lane_turns_its_wheel_its_own_number_of_notches(monkeypatch):
    # a lane that dwells longer between notches turns more, smaller ones for the same ground: the
    # gap and the notch count move together
    monkeypatch.setattr(human, "lane_offset", lambda page: 0.7)  # a slower lane
    page = Page()
    asyncio.run(human._wheel(page, 900))
    monkeypatch.setattr(human, "lane_offset", lambda page: -0.7)  # a quicker one
    quick = Page()
    asyncio.run(human._wheel(quick, 900))
    assert notches_of(page.mouse.wheels) > notches_of(quick.mouse.wheels)


def test_a_lane_keeps_one_offset_and_another_lane_draws_its_own():
    slow, other = Page(), Page()
    assert slow.context is not other.context
    assert len({human.lane_offset(slow) for _ in range(20)}) == 1  # no redraw between calls
    assert len({human.lane_offset(other) for _ in range(20)}) == 1
    assert {human.lane_offset(p) for p in ([slow] * 200 + [other] * 200)} != set()


def test_without_a_context_every_call_draws_again():
    page = Page()
    del page.context
    assert len({human.lane_offset(page) for _ in range(40)}) > 1


def test_calls_are_cut_into_whole_notches(monkeypatch):
    draws = iter([1.1, 0.9, 1.0] * 10)  # jitter range ends and the midpoint, over and over
    monkeypatch.setattr(human.random, "uniform", lambda a, b: a + (b - a) * next(draws))
    calls = human._call_notches(7, 3)
    assert calls == [3, 3, 1]  # every call at least one notch and together no more than asked
    assert sum(human._call_notches(4, 3)) == 4  # the last call takes what is left, never more
    assert human._call_notches(1, 3) == [1]


def test_a_pause_stays_in_its_range_and_leans_low():
    draws = [human._draw_ms(90, 260) for _ in range(4000)]
    assert min(draws) >= 90 and max(draws) <= 260
    assert statistics.median(draws) < 150  # well under the middle of a flat range (175)


def test_an_empty_range_is_its_bound():
    assert human._draw_ms(50, 50) == 50


def test_going_back_up_is_a_flick_not_a_reading_gesture(steady):
    """Up is content already seen, so its notches come back to back; down they are spaced, to read
    between them. The ground is the same either way, one notch a call as a real wheel sends."""
    def gesture(dy, pace="read"):
        page = Page()
        asyncio.run(human._wheel(page, dy, pace=pace))
        return page.mouse.wheels, page.waits

    down, down_waits = gesture(1200)
    up, up_waits = gesture(-1200, pace="return")
    assert notches_of(down) == notches_of(up)  # the same ground, at two speeds
    # measured over many draws, not one gesture, which is a couple of waits
    typical = lambda pace: statistics.median([human._notch_gap_ms(Page(), pace) for _ in range(400)])
    assert typical("return") * 1.5 < typical("read")  # back to back, not a pause a reader takes
    assert sum(up_waits) < sum(down_waits)


def test_a_return_still_follows_the_lanes_offset(monkeypatch):
    monkeypatch.setattr(human, "lane_offset", lambda page: 0.7)
    page = Page()
    asyncio.run(human._wheel(page, -600, pace="return"))
    assert len(page.mouse.wheels) > 1  # a flick is several calls, not one long one
    assert all(w < 0 for w in page.mouse.wheels)


def test_over_ground_already_covered_a_skim_dips_and_comes_straight_back(steady, monkeypatch):
    """A person who went back up keeps going back up: one small dip, one flick back, still covered.
    Fresh ground reads on down and ends covered."""
    monkeypatch.setattr(human, "_draw_ms", lambda lo, hi: (lo + hi) // 2)
    pages = []
    seen = []

    async def wheel(page, dy, *, pace="read"):
        seen.append(dy)
        await page.wait_for_timeout(1)

    async def rest(page, area=None):
        return None

    async def go():
        monkeypatch.setattr(human, "_wheel", wheel)
        monkeypatch.setattr(human, "_rest_pointer", rest)
        page = Page()
        pages.append(page)
        await human.wheel_down(page)          # reading down: covered ground is left behind
        assert not human.on_covered(page)
        seen.clear()
        await human.glance(page)              # first skim down, ending up
        assert human.on_covered(page)
        down_first = sum(d for d in seen if d > 0)
        back = -sum(d for d in seen if d < 0)
        assert back > 0 and down_first > 0
        assert back > 320 and back > down_first * 0.6  # a dip, then a long way back
        seen.clear()
        await human.glance(page)              # over covered ground it stays up
        net = sum(seen)
        assert human.on_covered(page) and net < 0
    asyncio.run(go())


def test_a_new_lane_starts_over_fresh_ground():
    assert not human.on_covered(Page())
