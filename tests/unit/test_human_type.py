"""human.type_text: real key holds, rollover, Shift around capitals, typos fixed with Backspace, and
emoji outside the BMP inserted once, never typed as keys."""

from __future__ import annotations

import asyncio
import itertools
import random

import pytest

from agentos.node import human, human_fields


class Keyboard:
    """Records key events on the page's clock and keeps the text a field would hold."""

    def __init__(self, page: Page, fail_on: str | None = None, stall_on: str | None = None) -> None:
        self.page = page
        self.fail_on = fail_on
        self.stall_on = stall_on  # this key's keydown reaches the browser, then the reply never comes
        self.stalled = asyncio.Event() if stall_on else None
        self.calls: list[tuple[str, str]] = []
        self.events: list[tuple[float, str, str]] = []  # (ms, "down" | "up", key)
        self.down_keys: set[str] = set()
        self.stray_ups: list[str] = []
        self.value = ""

    async def down(self, key: str) -> None:
        if key == self.fail_on:
            raise RuntimeError("the page went away")
        assert key not in self.down_keys, f"{key} went down twice (an auto-repeat)"
        self.down_keys.add(key)
        if key == self.stall_on:
            self.stalled.set()
            await asyncio.Event().wait()
        self.calls.append(("down", key))
        self.events.append((self.page.clock, "down", key))
        if key == "Backspace":
            self.value = self.value[:-1]
        elif key == "Enter":
            self.value += "\n"
        elif len(key) == 1:
            self.value += key

    async def up(self, key: str) -> None:
        if key not in self.down_keys:
            self.stray_ups.append(key)
        self.down_keys.discard(key)
        self.calls.append(("up", key))
        self.events.append((self.page.clock, "up", key))

    async def type(self, ch: str) -> None:
        self.calls.append(("type", ch))
        self.value += ch

    async def insert_text(self, ch: str) -> None:
        self.calls.append(("insert", ch))
        self.value += ch


class Page:
    def __init__(self, fail_on: str | None = None, stall_on: str | None = None) -> None:
        self.clock = 0.0
        self.keyboard = Keyboard(self, fail_on, stall_on)

    async def wait_for_timeout(self, ms: float) -> None:
        self.clock += ms


def strokes(events: list[tuple[float, str, str]]) -> list[dict]:
    """Non-Shift key presses: when each went down and came up."""
    out, open_ = [], {}
    for t, kind, key in events:
        if key == "Shift":
            continue
        if kind == "down":
            open_[key] = {"key": key, "down": t, "up": None}
            out.append(open_[key])
        else:
            open_.pop(key)["up"] = t
    return out


def test_emoji_are_inserted_once_and_other_letters_go_through_type():
    page = Page()
    asyncio.run(human.type_text(page, "Ačiū 🌄!", typos=False))
    assert [c for c in page.keyboard.calls if c[0] in ("type", "insert")] == \
        [("type", "č"), ("type", "ū"), ("insert", "🌄")]
    assert page.keyboard.value == "Ačiū 🌄!"
    assert not page.keyboard.down_keys


@pytest.mark.parametrize("seed", range(30))
def test_the_field_ends_up_with_exactly_the_text_however_the_typos_fall(seed, monkeypatch):
    random.seed(seed)
    monkeypatch.setattr(human, "TYPO_RATE", 0.3)
    text = "Labas, the quick brown fox jumps over the lazy dog!\nĄžuolas 😀 sees NASA at 10:30 (ok)"
    page = Page()
    asyncio.run(human.type_text(page, text, typos=True))
    assert page.keyboard.value == text
    assert not page.keyboard.down_keys


def test_typos_are_fixed_with_backspace_and_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(human, "TYPO_RATE", 1.0)
    random.seed(1)
    page = Page()
    asyncio.run(human.type_text(page, "hello world", typos=True))
    assert ("down", "Backspace") in page.keyboard.calls and page.keyboard.value == "hello world"

    for off in ({"typos": False}, {}):  # off unless asked for, so no caller slips by accident
        page = Page()
        asyncio.run(human.type_text(page, "hello world", **off))
        assert ("down", "Backspace") not in page.keyboard.calls and page.keyboard.value == "hello world"


def test_a_typo_is_noticed_before_a_space_or_enter(monkeypatch):
    """A slip is fixed before any key that can act on the word (a tag field turns it into a chip,
    Enter submits): only letters and digits are typed between the slip and its Backspaces."""
    monkeypatch.setattr(human, "TYPO_RATE", 1.0)
    for seed in range(40):
        random.seed(seed)
        plan = human._keystrokes("ab cd\nef", typos=True)
        for i, (kind, _) in enumerate(plan):
            if kind == "notice":
                erased = 0
                while plan[i + 1 + erased] == ("key", "Backspace"):
                    erased += 1
                after_slip = [ch for _, ch in plan[i - erased + 1:i]]
                assert all(ch.isalnum() for ch in after_slip), plan


def test_keys_are_held_a_human_time_and_some_letters_overlap():
    random.seed(4)
    page = Page()
    asyncio.run(human.type_text(page, "the quick brown fox jumps over the lazy dog " * 4, typos=False))
    done = strokes(page.keyboard.events)
    holds = sorted(s["up"] - s["down"] for s in done)
    assert 40 <= holds[0] and holds[-1] <= 220
    assert 60 <= holds[len(holds) // 2] <= 120
    overlaps = sum(1 for a, b in itertools.pairwise(done) if b["down"] < a["up"])
    assert 0.1 * len(done) < overlaps < 0.4 * len(done)
    # never the same key twice at once (that would be an auto-repeat)
    assert all(a["key"] != b["key"] for a, b in itertools.pairwise(done) if b["down"] < a["up"])


def test_a_lane_types_at_its_own_pace(monkeypatch):
    """Two lanes under the same text: each keeps one pace, and their medians differ."""
    def median_hold(offset):
        monkeypatch.setattr(human, "lane_offset", lambda page: offset)
        random.seed(11)
        page = Page()
        asyncio.run(human.type_text(page, "held keys keep their own pace", typos=False))
        holds = sorted(s["up"] - s["down"] for s in strokes(page.keyboard.events))
        return holds[len(holds) // 2], holds[0], holds[-1]

    quick, quick_lo, quick_hi = median_hold(-0.35)
    heavy, heavy_lo, heavy_hi = median_hold(0.35)
    assert quick < heavy  # the same run at two lanes' paces
    assert quick_lo >= 40 / 1.45 and heavy_hi <= 220 * 1.45  # the floor and ceiling move with it
    assert quick_hi <= 300 and heavy_lo >= 40  # and neither lane leaves a hand's reach


def test_capitals_and_shifted_symbols_come_with_shift_held():
    random.seed(2)
    page = Page()
    asyncio.run(human.type_text(page, "Hi NASA!", typos=False))
    shift_down = False
    for t, kind, key in page.keyboard.events:
        if key == "Shift":
            shift_down = kind == "down"
        elif kind == "down" and (key.isupper() or key == "!"):
            assert shift_down, key
        elif kind == "down":
            assert not shift_down, key
    # one Shift for "H" and one held across "NASA!", each pressed a moment before its first key
    shifts = [(t, kind) for t, kind, key in page.keyboard.events if key == "Shift"]
    assert [kind for _, kind in shifts] == ["down", "up", "down", "up"]
    firsts = [t for t, kind, key in page.keyboard.events if kind == "down" and key in ("H", "N")]
    assert all(40 <= first - down <= 130 for first, (down, _) in zip(firsts, shifts[::2], strict=True))
    assert page.keyboard.value == "Hi NASA!"


def test_an_interrupted_typing_lets_every_key_up():
    page = Page(fail_on="Q")
    with pytest.raises(RuntimeError):
        asyncio.run(human.type_text(page, "abcQ", typos=False))
    assert not page.keyboard.down_keys


@pytest.mark.parametrize("text", ["banana", "abababab", "aba", "assess", "minimum", "Hello World"])
def test_a_key_never_goes_down_while_it_is_still_held(text, monkeypatch):
    """Rollover presses the next key before the last is up; a key two strokes back ("aba") can still
    be down, and a second keydown of it would be an auto-repeat with one orphan keyup. The fake
    keyboard asserts on a second keydown; here every keyup also matches a keydown."""
    monkeypatch.setattr(human, "ROLLOVER_RATE", 0.9)
    for seed in range(400):
        random.seed(seed)
        page = Page()
        asyncio.run(human.type_text(page, text, typos=seed % 2 == 0))
        assert page.keyboard.value == text and not page.keyboard.down_keys and not page.keyboard.stray_ups, seed


def test_line_breaks_press_enter_and_a_tab_goes_in_as_text():
    """As keyboard.type did: a tab pressed as Tab would move focus and send the rest elsewhere."""
    page = Page()
    asyncio.run(human.type_text(page, "a\tb\nc\rd"))
    assert ("type", "\t") in page.keyboard.calls and ("down", "Tab") not in page.keyboard.calls
    assert [c for c in page.keyboard.calls if c[1] == "Enter"] == [("down", "Enter"), ("up", "Enter")] * 2
    assert page.keyboard.value == "a\tb\nc\nd" and not page.keyboard.down_keys


def test_a_cancel_while_a_key_goes_down_still_lets_it_up():
    """The keydown may already be with the browser when a cancel (a freeze, a timeout) lands during
    its round trip: the key must be up afterwards all the same."""
    async def run() -> Page:
        page = Page(stall_on="c")
        task = asyncio.ensure_future(human.type_text(page, "abcd"))
        await page.keyboard.stalled.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return page
    page = asyncio.run(run())
    assert not page.keyboard.down_keys and not page.keyboard.stray_ups


def test_before_key_runs_right_before_each_key_and_stops_the_typing():
    """The typist's probe: before Shift and again before the capital after Shift's lead, so a check
    still holds when the letter lands; a failing check lets every key up and types nothing more."""
    page = Page()
    checks: list[tuple[float, set[str]]] = []

    async def check() -> None:
        checks.append((page.clock, set(page.keyboard.down_keys)))
    asyncio.run(human.type_text(page, "aB", before_key=check))
    downs = [t for t, kind, _ in page.keyboard.events if kind == "down"]
    assert [t for t, _ in checks] == downs  # a, Shift, B: each checked at the moment it goes down
    assert checks[2][1] >= {"Shift"}  # the capital's check comes after Shift is down

    class Moved(Exception):
        pass

    page = Page()
    seen = 0

    async def moves_on_the_capital() -> None:
        nonlocal seen
        seen += 1
        if "Shift" in page.keyboard.down_keys:
            raise Moved
    with pytest.raises(Moved):
        asyncio.run(human.type_text(page, "aBc", before_key=moves_on_the_capital))
    assert page.keyboard.value == "a" and not page.keyboard.down_keys and seen == 3


@pytest.mark.parametrize("facts, fit", [
    ({"maxLength": -1, "type": "text"}, True),
    ({"maxLength": -1, "type": ""}, True),                                    # a textarea or contenteditable
    ({"maxLength": 280, "type": "text"}, False),                              # a length limit
    ({"maxLength": -1, "type": "password"}, False),
    ({"maxLength": -1, "type": "text", "combobox": True}, False),             # role=combobox, or inside one
    ({"maxLength": -1, "type": "", "combobox": True}, False),                 # a contenteditable combobox
    ({"maxLength": -1, "type": "text", "ariaAutocomplete": "list"}, False),
    ({"maxLength": -1, "type": "text", "ariaAutocomplete": "none"}, True),
    ({"maxLength": -1, "type": "text", "autocomplete": "one-time-code"}, False),
    ({"maxLength": -1, "type": "text", "autocomplete": "section-2fa one-time-code"}, False),
    ({"maxLength": -1, "type": "text", "autocomplete": "email"}, True),
    ({"maxLength": -1, "type": "text", "inputmode": "numeric"}, False),
    ({"maxLength": -1, "type": "text", "inputmode": "decimal"}, False),
    ({"maxLength": -1, "type": "search", "suggestions": True}, False),        # <input list=...>
    (None, False),                                                            # unreadable
    ({"maxLength": "x"}, False),
])
def test_typos_allowed(facts, fit):
    assert human_fields.typos_allowed(facts) is fit


def test_typos_fit_reads_the_field_and_counts_unreadable_as_unfit():
    class Field:
        def __init__(self, facts):
            self.facts = facts

        async def evaluate(self, js):
            if isinstance(self.facts, Exception):
                raise self.facts
            return self.facts
    assert asyncio.run(human_fields.typos_fit(Field({"maxLength": -1, "type": "text"}))) is True
    assert asyncio.run(human_fields.typos_fit(Field({"maxLength": -1, "type": "password"}))) is False
    assert asyncio.run(human_fields.typos_fit(Field(RuntimeError("detached")))) is False


class Mouse:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    async def move(self, x: float, y: float) -> None:
        self.order.append("move")

    async def down(self) -> None:
        self.order.append("down")

    async def up(self) -> None:
        self.order.append("up")


class PressKeyboard:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    async def press(self, key: str, delay: int = 0) -> None:
        self.order.append("key")


class PressPage(Page):
    def __init__(self) -> None:
        super().__init__()
        self.order: list[str] = []
        self.mouse = Mouse(self.order)
        self.keyboard = PressKeyboard(self.order)


class Stopped(Exception):
    pass


def _still(monkeypatch) -> None:
    """A target already in view, under the pointer."""
    async def nothing(*args, **kwargs):
        return None
    monkeypatch.setattr(human, "scroll_into_view", nothing)
    monkeypatch.setattr(human, "_glide_onto", nothing)


@pytest.mark.parametrize("name, press, arg", [("click", ["down", "up"], object()), ("press", ["key"], "Enter")])
def test_the_before_press_check_comes_right_before_the_real_press_and_can_stop_it(monkeypatch, name, press, arg):
    """Not a stand-in for human.click: the check that browser_act and link following rely on."""
    _still(monkeypatch)
    page = PressPage()

    async def check() -> None:
        page.order.append("check")

    async def run(check) -> None:
        with human.before_press(check):
            await getattr(human, name)(page, arg)
    asyncio.run(run(check))
    assert page.order == ["check", *press]

    page = PressPage()

    async def stops() -> None:
        raise Stopped
    with pytest.raises(Stopped):
        asyncio.run(run(stops))
    assert page.order == []
