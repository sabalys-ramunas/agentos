"""The node typist without a browser: locator encoding and signatures (pure), and the
per-keystroke guard against a scripted fake page that answers the typist's scripts."""

from __future__ import annotations

import asyncio
import traceback
from dataclasses import replace
from typing import Any

import pytest

from openajar.core.errors import InvalidRequest, RefNotFound, ServiceUnavailable
from openajar.core.models import Signal
from openajar.core.runtime import Runtime
from openajar.node import human
from openajar.node import typist as T
from openajar.node.typist import (
    Locator,
    PlaywrightTypist,
    field_info,
    field_signature,
    has_control_chars,
    protocol_logging,
    typist_factory,
    url_origin,
)
from openajar.vault.errors import TypistAborted
from openajar.vault.typist import FieldInfo

A = "https://login.example.com"
B = "https://widgets.example.net"
SECRET = "hunter2-Secret!"
SIG = "0123456789abcdef0123456789abcdef"


# ---- locators and signatures (pure) ---------------------------------------------------

@pytest.mark.parametrize("frames", [(), (0,), (1, 12, 3), tuple(range(8))])
def test_locator_round_trip(frames):
    loc = Locator(frames=frames, index=7, sig=SIG)
    raw = loc.encode()
    assert raw.startswith("pw1:") and Locator.parse(raw) == loc
    assert raw.split(":")[1] == (".".join(map(str, frames)) or "top")


@pytest.mark.parametrize("raw", [
    "", "pw1", "pw2:top:0:" + SIG, "pw1:top:0:" + SIG.upper(), "pw1:top:0:" + SIG[:-1],
    "pw1:top:-1:" + SIG, "pw1:top:01:" + SIG, "pw1:01:0:" + SIG, "pw1::0:" + SIG,
    "pw1:0..1:0:" + SIG, "pw1:" + ".".join(["0"] * 9) + ":0:" + SIG, "pw1:top:0:" + SIG + ":x",
    " pw1:top:0:" + SIG, "pw1:top:1234567:" + SIG, "#password", None, 12,
])
def test_locator_parse_rejects_anything_else(raw):
    with pytest.raises(ValueError):
        Locator.parse(raw)


BASE = ("input", "password", "password", "pw", "current-password")


def test_signature_is_stable_and_covers_every_part():
    sig = field_signature(BASE, (A,))
    assert sig == field_signature(BASE, [A]) and len(sig) == 32 and int(sig, 16) >= 0
    for i in range(5):
        changed = list(BASE)
        changed[i] = changed[i] + "x"
        assert field_signature(tuple(changed), (A,)) != sig
    assert field_signature(BASE, (A, A)) != sig
    assert field_signature(BASE, (B,)) != sig
    assert field_signature(BASE, ("null",)) != sig


def test_signature_has_no_delimiter_collisions():
    assert field_signature(("input", "text", 'a","b', "", ""), (A,)) != field_signature(("input", "text", "a", "b", ""), (A,))
    assert field_signature(("input", "text", "", "", ""), ("a", "b")) != field_signature(("input", "text", "", "", ""), ("a,b",))


def desc(**kw: Any) -> dict[str, Any]:
    base = {"k": "f", "index": 3, "tag": "input", "type": "password", "name": "password", "id": "pw",
            "autocomplete": "current-password", "inputmode": "", "maxlength": None, "form": 0,
            "visible": True, "disabled": False, "readonly": False}
    base.update(kw)
    return base


def test_field_info_from_a_description():
    info = field_info(desc(), (2, 0), (A, A, A))
    loc = Locator.parse(info.locator)
    assert (loc.frames, loc.index, loc.sig) == ((2, 0), 3, field_signature(BASE, (A, A, A)))
    assert info.frame_origins == (A, A, A)
    assert (info.tag, info.type, info.name, info.id, info.autocomplete) == BASE
    assert info.form == "2.0/form0" and info.visible and not info.disabled and info.ref is None
    assert field_info(desc(form=0), (), (A,)).form == "top/form0"
    assert field_info(desc(form=None), (), (A,)).form is None
    assert field_info(desc(), (2, 0), (A, A, A), frames_visible=False).visible is False
    assert field_info(desc(visible=False), (), (A,)).visible is False
    assert field_info(desc(), (), (A,), ref="f1e4").ref == "f1e4"
    for raw, want in ((None, None), (-1, None), (True, None), ("6", None), (0, 0), (6, 6)):
        assert field_info(desc(maxlength=raw), (), (A,)).maxlength == want


@pytest.mark.parametrize(("url", "want"), [
    ("https://Login.Example.com/path?q#f", "https://login.example.com"),
    ("https://example.com:443/", "https://example.com"),
    ("http://127.0.0.1:7495/login.html", "http://127.0.0.1:7495"),
    ("http://[::1]:8080/", "http://[::1]:8080"),
    ("https://user:pw@example.com/", "https://example.com"),
    ("about:blank", "null"), ("data:text/html,x", "null"), ("", "null"), ("http://:80/", "null"),
    ("https://example.com:99999/", "null"),
])
def test_url_origin(url, want):
    assert url_origin(url) == want


def test_control_characters_and_protocol_logging():
    assert not has_control_chars("ok pass-word é ü 中文 😀")
    for ch in "\t\n\r\x00\x1b\x7f\x85":
        assert has_control_chars("a" + ch)
    assert not protocol_logging({}) and not protocol_logging({"DEBUG": ""})
    assert not protocol_logging({"DEBUG": "pw:browser"}) and not protocol_logging({"DEBUG": "-pw:api"})
    for value in ("pw:api", "pw:protocol", "pw:*", "*", "foo,pw:channel", "pw:browser pw:api"):
        assert protocol_logging({"DEBUG": value}), value


# ---- a fake page that answers the typist's scripts --------------------------------------

class El:
    def __init__(self, name: str, type: str = "text", *, tag: str = "input", autocomplete: str = "",
                 id: str = "", form: int | None = 0, visible: bool = True):
        self.tag, self.type, self.name, self.id, self.autocomplete = tag, type, name, id or name, autocomplete
        self.form, self.visible = form, visible
        self.value, self.selected, self.connected = "", False, True
        self.doc: Doc | None = None


class IFrame:
    def __init__(self, child: Doc, *, visible: bool = True):
        self.visible, self.connected = visible, True
        self.doc: Doc | None = None
        self.child = child
        child.holder = self

    def navigate(self, doc: Doc) -> None:
        self.child.alive = False
        self.child = doc
        doc.holder = self


class Doc:
    def __init__(self, origin: str, nodes: list[Any]):
        self.origin, self.nodes, self.alive = origin, list(nodes), True
        self.active: Any = None
        self.holder: IFrame | None = None
        for n in self.nodes:
            n.doc = self

    @property
    def controls(self) -> list[El]:
        return [n for n in self.nodes if isinstance(n, El)]

    @property
    def frames(self) -> list[IFrame]:
        return [n for n in self.nodes if isinstance(n, IFrame)]

    def has_focus(self) -> bool:
        if self.holder is None:
            return True
        parent = self.holder.doc
        return parent is not None and parent.active is self.holder and parent.has_focus()

    def remove(self, node: Any) -> None:
        self.nodes.remove(node)
        node.connected = False


def focus(el: El) -> None:
    doc = el.doc
    doc.active = el
    while doc.holder is not None:
        doc.holder.doc.active = doc.holder
        doc = doc.holder.doc


def shape(el: El) -> dict[str, Any]:
    return {"tag": el.tag, "type": el.type, "name": el.name, "id": el.id, "autocomplete": el.autocomplete}


def describe(el: El, index: int) -> dict[str, Any]:
    return {**shape(el), "k": "f", "index": index, "inputmode": "", "maxlength": None, "form": el.form,
            "visible": el.visible, "disabled": False, "readonly": False}


class Handle:
    def __init__(self, page: FakePage, node: Any):
        self.page, self.node = page, node
        self.doc = node.doc if node is not None else None

    def as_element(self) -> Handle | None:
        return self if self.node is not None else None

    async def dispose(self) -> None:
        self.page.disposed += 1

    async def content_frame(self) -> Frame | None:
        return self.page.frame_for(self.node) if isinstance(self.node, IFrame) else None

    async def bounding_box(self) -> dict[str, float]:
        return {"x": 100.0, "y": 200.0, "width": 300.0, "height": 30.0}

    async def evaluate(self, js: str, arg: Any = None) -> Any:
        doc, node = self.doc, self.node
        if doc is None or not doc.alive:
            raise RuntimeError("Execution context was destroyed")
        focused = doc.active is node and doc.has_focus()
        if js is T._DESCRIBE_JS:
            index = doc.controls.index(node) if node in doc.controls else -1
            return {**describe(node, index), "connected": node.connected, "origin": doc.origin}
        if js is T._CONTAINER_JS:
            return {"connected": node.connected, "origin": doc.origin, "focused": focused, "visible": node.visible}
        if js is T._PROBE_JS:
            return {**shape(node), "connected": node.connected, "origin": doc.origin, "focused": focused,
                    "length": len(node.value)}
        if js is human._SCROLL_AREA_JS:
            return {"x": 0, "y": 0, "width": 1280, "height": 900}
        if js is human._HITS_TARGET_JS:
            self.page.aimed = node
            return True
        raise AssertionError(f"unexpected script: {js[:60]!r}")


class Frame:
    def __init__(self, page: FakePage, holder: IFrame | None, parent: Frame | None):
        self.page, self.holder, self.parent_frame = page, holder, parent

    @property
    def doc(self) -> Doc:
        return self.page.top if self.holder is None else self.holder.child

    async def evaluate(self, js: str, arg: Any = None) -> Any:
        if js is T._SCAN_JS:
            items: list[dict[str, Any]] = []
            for n in self.doc.nodes:
                if isinstance(n, El):
                    items.append(describe(n, self.doc.controls.index(n)))
                else:
                    items.append({"k": "i", "index": self.doc.frames.index(n), "visible": n.visible})
            return {"origin": self.doc.origin, "items": items}
        raise AssertionError(f"unexpected script: {js[:60]!r}")

    async def evaluate_handle(self, js: str, arg: Any = None) -> Handle:
        nodes = {T._CONTROL_AT_JS: self.doc.controls, T._FRAME_AT_JS: self.doc.frames}[js]
        return Handle(self.page, nodes[arg] if 0 <= arg < len(nodes) else None)


class Keyboard:
    def __init__(self, page: FakePage):
        self.page = page
        self.fail_on: str | None = None

    async def type(self, ch: str) -> None:
        if self.fail_on == ch:
            raise RuntimeError(f"Keyboard.type: could not type {ch!r}")
        el = self.page.focused()
        if el is not None:
            el.value = ch if el.selected else el.value + ch
            el.selected = False
        self.page.keys.append(ch)
        self.page.after_key()

    async def down(self, key: str) -> None:
        """human.type_text holds each key: a character key types its character, Shift types nothing."""
        if len(key) == 1:
            await self.type(key)
        elif key == "Shift" and self.page.on_shift is not None:
            self.page.on_shift()

    async def up(self, key: str) -> None:
        return None

    async def press(self, key: str, delay: int | None = None) -> None:
        el = self.page.focused()
        if el is not None and key == "ControlOrMeta+a":
            el.selected = True
        elif el is not None and key == "Backspace" and el.selected:
            el.value, el.selected = "", False
        self.page.presses.append(key)
        self.page.after_key()


class Mouse:
    def __init__(self, page: FakePage):
        self.page = page

    async def move(self, x: float, y: float) -> None:
        pass

    async def wheel(self, dx: float, dy: float) -> None:
        pass

    async def down(self) -> None:
        pass

    async def up(self) -> None:
        if isinstance(self.page.aimed, El):
            focus(self.page.aimed)


class FakePage:
    def __init__(self, top: Doc, url: str = A + "/login"):
        self.top, self.url = top, url
        self.keyboard, self.mouse = Keyboard(self), Mouse(self)
        self.main_frame = Frame(self, None, None)
        self._frames: dict[int, Frame] = {}
        self.aimed: Any = None
        self.keys: list[str] = []
        self.presses: list[str] = []
        self.on_key: Any = None
        self.on_shift: Any = None  # runs when Shift goes down (before a capital)
        self.disposed = 0
        self.listeners: dict[str, list[Any]] = {}

    def on(self, event: str, fn: Any) -> None:
        self.listeners.setdefault(event, []).append(fn)

    def emit(self, event: str, arg: Any) -> None:
        for fn in self.listeners.get(event, []):
            fn(arg)

    async def wait_for_timeout(self, ms: float) -> None:
        pass

    async def wait_for_load_state(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def evaluate(self, js: str, arg: Any = None) -> Any:
        assert "innerWidth" in js
        return [1280, 900]

    def frame_for(self, iframe: IFrame) -> Frame:
        frame = self._frames.get(id(iframe))
        if frame is None:
            parent = self.main_frame if iframe.doc is self.top else self.frame_for(iframe.doc.holder)
            frame = self._frames[id(iframe)] = Frame(self, iframe, parent)
        return frame

    def focused(self) -> El | None:
        a = self.top.active
        while isinstance(a, IFrame):
            a = a.child.active
        return a

    def after_key(self) -> None:
        if self.on_key is not None:
            self.on_key(len(self.keys) + len(self.presses))


def login_page(*, framed: bool = False) -> tuple[FakePage, dict[str, Any]]:
    """search, [username, password] (in a same-origin frame when framed), comment."""
    user = El("username", autocomplete="username")
    pw = El("password", "password", autocomplete="current-password")
    decoy = El("comment", form=None)
    widget = Doc(B, [El("cap", form=None)])
    trap = IFrame(widget)
    if framed:
        inner = Doc(A, [user, pw])
        holder = IFrame(inner)
        top = Doc(A, [El("q", "search", form=None), holder, decoy, trap])
    else:
        holder = None
        top = Doc(A, [El("q", "search", form=None), user, pw, decoy, trap])
    return FakePage(top), {"user": user, "pw": pw, "decoy": decoy, "trap": trap, "cap": widget.controls[0],
                           "holder": holder}


def named(fields: list[FieldInfo], name: str) -> FieldInfo:
    return next(f for f in fields if f.name == name)


def secret_in_traceback(exc: BaseException) -> bool:
    for frame, _ in traceback.walk_tb(exc.__traceback__):
        if frame.f_code.co_name.startswith("test_"):
            continue
        if any(isinstance(v, str) and SECRET in v for v in frame.f_locals.values()):
            return True
    return False


# ---- reading and focusing ----------------------------------------------------------------

async def test_fields_in_document_order_across_frames():
    page, _ = login_page(framed=True)
    fields = await PlaywrightTypist(page).fields()
    assert [(f.name, f.frame_origins) for f in fields] == [
        ("q", (A,)), ("username", (A, A)), ("password", (A, A)), ("comment", (A,)), ("cap", (A, B))]
    assert Locator.parse(named(fields, "password").locator).frames == (0,)
    assert Locator.parse(named(fields, "cap").locator).frames == (1,)
    assert named(fields, "username").form == named(fields, "password").form == "0/form0"


async def test_fields_in_a_hidden_frame_are_not_visible():
    user = El("username", autocomplete="username")
    page = FakePage(Doc(A, [IFrame(Doc(A, [user]), visible=False)]))
    (info,) = await PlaywrightTypist(page).fields()
    assert info.visible is False


async def test_focus_clicks_and_returns_fresh_metadata():
    page, els = login_page(framed=True)
    t = PlaywrightTypist(page)
    pw = named(await t.fields(), "password")
    fresh = await t.focus(replace(pw, ref="f1e3"))
    assert page.focused() is els["pw"] and fresh.ref == "f1e3" and fresh.locator == pw.locator
    assert page.disposed > 0  # handles are released


async def test_focus_on_a_stale_locator_raises_ref_not_found():
    page, _ = login_page()
    t = PlaywrightTypist(page)
    user = named(await t.fields(), "username")
    page.top.nodes.insert(1, El("promo", form=0))  # same place, other element
    page.top.nodes[1].doc = page.top
    with pytest.raises(RefNotFound):
        await t.focus(user)
    with pytest.raises(RefNotFound):
        await t.focus(replace(user, locator="not-a-locator"))
    assert page.focused() is None


# ---- typing ------------------------------------------------------------------------------

async def typed_into(page: FakePage, field: str) -> tuple[PlaywrightTypist, FieldInfo]:
    t = PlaywrightTypist(page)
    return t, await t.focus(named(await t.fields(), field))


async def test_type_secret_types_every_character_into_the_field():
    page, els = login_page(framed=True)
    t, pw = await typed_into(page, "password")
    await t.type_secret(pw, SECRET)
    assert els["pw"].value == SECRET and page.keys == list(SECRET) and page.presses == []
    assert els["decoy"].value == "" and els["cap"].value == ""


async def test_type_secret_clears_the_field_first():
    page, els = login_page()
    els["user"].value = "someone-else"
    t, user = await typed_into(page, "username")
    await t.type_secret(user, "ada")
    assert els["user"].value == "ada" and page.presses == ["ControlOrMeta+a", "Backspace"]


async def test_a_field_that_will_not_clear_is_not_typed_into():
    page, els = login_page()
    els["user"].value = "stuck"
    t, user = await typed_into(page, "username")
    page.on_key = lambda n: setattr(els["user"], "value", "stuck")  # the page refills it
    with pytest.raises(TypistAborted, match="could not be cleared"):
        await t.type_secret(user, SECRET)
    assert page.keys == []


def steal_after(page: FakePage, n: int, action: Any) -> None:
    def hook(count: int) -> None:
        if count == n:
            action()
    page.on_key = hook


@pytest.mark.parametrize("framed", [False, True])
async def test_focus_moving_to_another_field_stops_typing_at_once(framed):
    page, els = login_page(framed=framed)
    t, pw = await typed_into(page, "password")
    steal_after(page, 3, lambda: focus(els["decoy"]))
    with pytest.raises(TypistAborted) as info:
        await t.type_secret(pw, SECRET)
    assert str(info.value) == "typing stopped: focus left the field"
    assert page.keys == list(SECRET[:3]) and els["pw"].value == SECRET[:3] and els["decoy"].value == ""
    assert info.value.__context__ is None and info.value.__cause__ is None
    assert not secret_in_traceback(info.value)


async def test_focus_moving_while_shift_is_down_stops_before_the_capital():
    """Shift goes down 50 to 120 ms before a capital: the probe runs again right before the capital's
    key, so a page that moves focus in that time gets nothing."""
    page, els = login_page()
    t, pw = await typed_into(page, "password")
    capital = next(i for i, ch in enumerate(SECRET) if ch.isupper())
    page.on_shift = lambda: focus(els["decoy"])
    with pytest.raises(TypistAborted) as info:
        await t.type_secret(pw, SECRET)
    assert str(info.value) == "typing stopped: focus left the field"
    assert page.keys == list(SECRET[:capital]) and els["decoy"].value == ""
    assert info.value.__context__ is None and info.value.__cause__ is None
    frames = [frame.f_code.co_name for frame, _ in traceback.walk_tb(info.value.__traceback__)]
    assert "type_text" not in frames  # its frame holds a character of the secret
    assert not secret_in_traceback(info.value)


async def test_focus_moving_into_a_cross_origin_frame_stops_typing():
    page, els = login_page()
    t, pw = await typed_into(page, "password")

    def into_frame() -> None:
        els["cap"].doc.active = els["cap"]
        page.top.active = els["trap"]

    steal_after(page, 4, into_frame)
    with pytest.raises(TypistAborted, match="focus left the field"):
        await t.type_secret(pw, SECRET)
    assert page.keys == list(SECRET[:4]) and els["cap"].value == ""


async def test_focus_moving_inside_the_field_frame_but_out_of_the_frame_stops_typing():
    page, _ = login_page(framed=True)
    t, pw = await typed_into(page, "password")
    steal_after(page, 2, lambda: setattr(page.top, "active", page.top.controls[0]))  # top focuses search
    with pytest.raises(TypistAborted, match="focus left the field"):
        await t.type_secret(pw, SECRET)
    assert page.keys == list(SECRET[:2])  # the frame's own activeElement still says "password"


async def test_top_level_navigation_stops_typing():
    page, _ = login_page()
    t, pw = await typed_into(page, "password")
    steal_after(page, 5, lambda: setattr(page, "url", "https://evil.example.org/"))
    with pytest.raises(TypistAborted, match="the page navigated"):
        await t.type_secret(pw, SECRET)
    assert page.keys == list(SECRET[:5])


async def test_the_field_frame_navigating_stops_typing():
    page, els = login_page(framed=True)
    t, pw = await typed_into(page, "password")
    look_alike = El("password", "password", autocomplete="current-password")
    other = Doc(B, [El("username", autocomplete="username"), look_alike])

    def navigate() -> None:
        els["holder"].navigate(other)
        focus(look_alike)

    steal_after(page, 3, navigate)
    with pytest.raises(TypistAborted):
        await t.type_secret(pw, SECRET)
    assert page.keys == list(SECRET[:3]) and look_alike.value == ""


async def test_the_frame_origin_changing_under_the_same_document_stops_typing():
    page, els = login_page(framed=True)
    t, pw = await typed_into(page, "password")
    steal_after(page, 3, lambda: setattr(els["pw"].doc, "origin", B))
    with pytest.raises(TypistAborted, match="frame"):
        await t.type_secret(pw, SECRET)
    assert page.keys == list(SECRET[:3])


@pytest.mark.parametrize(("change", "message"), [
    (lambda el, doc: setattr(el, "type", "text"), "the field changed"),
    (lambda el, doc: setattr(el, "name", "comment"), "the field changed"),
    (lambda el, doc: doc.remove(el), "the field left the page"),
])
async def test_the_field_changing_stops_typing(change, message):
    page, els = login_page()
    t, pw = await typed_into(page, "password")
    steal_after(page, 2, lambda: change(els["pw"], page.top))
    with pytest.raises(TypistAborted, match=message):
        await t.type_secret(pw, SECRET)
    assert page.keys == list(SECRET[:2])


async def test_expected_origins_are_the_ones_the_policy_saw():
    page, _ = login_page()
    t, pw = await typed_into(page, "password")
    with pytest.raises(TypistAborted):
        await t.type_secret(replace(pw, frame_origins=(B,)), SECRET)
    with pytest.raises(TypistAborted):
        await t.type_secret(replace(pw, frame_origins=(A, A)), SECRET)
    assert page.keys == []


async def test_a_stale_locator_never_types():
    page, els = login_page()
    t, pw = await typed_into(page, "password")
    els["pw"].type = "text"
    with pytest.raises(TypistAborted, match="no longer on the page"):
        await t.type_secret(pw, SECRET)
    with pytest.raises(TypistAborted, match="no longer on the page"):
        await t.press(pw, "Enter")
    assert page.keys == [] and page.presses == []


async def test_a_failing_key_reports_a_fixed_reason_without_the_text():
    page, _ = login_page()
    t, pw = await typed_into(page, "password")
    page.keyboard.fail_on = SECRET[4]
    with pytest.raises(TypistAborted) as info:
        await t.type_secret(pw, SECRET)
    assert str(info.value) == "typing stopped: a key could not be sent"
    assert info.value.__context__ is None and info.value.__cause__ is None
    assert repr(SECRET[4]) not in "".join(traceback.format_exception(info.value))
    assert not secret_in_traceback(info.value)


async def test_control_characters_are_refused_before_any_key():
    page, els = login_page()
    t, pw = await typed_into(page, "password")
    for bad in ("pass\tword", "pass\nword", "pass\rword"):
        with pytest.raises(InvalidRequest) as info:
            await t.type_secret(pw, bad)
        assert bad not in str(info.value) and "pass" not in str(info.value)
    assert page.keys == [] and els["pw"].value == ""


async def test_typing_is_refused_while_playwright_logs_the_protocol(monkeypatch):
    page, _ = login_page()
    t, pw = await typed_into(page, "password")
    monkeypatch.setenv("DEBUG", "pw:protocol")
    with pytest.raises(ServiceUnavailable):
        await t.type_secret(pw, SECRET)
    assert page.keys == []


async def test_press_checks_focus_first():
    page, els = login_page()
    t, pw = await typed_into(page, "password")
    await t.press(pw, "Enter")
    assert page.presses == ["Enter"]
    focus(els["decoy"])
    with pytest.raises(TypistAborted, match="focus left the field"):
        await t.press(pw, "Enter")
    assert page.presses == ["Enter"]


# ---- settle, signal and the factory --------------------------------------------------------

async def test_settle_waits_for_what_the_press_started(monkeypatch):
    monkeypatch.setattr(T, "SETTLE_START_S", 0.2)
    monkeypatch.setattr(T, "SETTLE_QUIET_S", 0.05)
    page, _ = login_page()
    t, pw = await typed_into(page, "password")
    await t.press(pw, "Enter")
    request = object()
    page.emit("request", request)

    async def finish() -> None:
        await asyncio.sleep(0.15)
        page.emit("requestfinished", request)

    loop = asyncio.get_running_loop()
    started = loop.time()
    task = asyncio.ensure_future(finish())
    await t.settle(5)
    await task
    took = loop.time() - started
    assert 0.15 <= took < 1.0
    await asyncio.wait_for(t.settle(0.1), 5)  # nothing pressed since: bounded by the timeout, not the 10 s default


class FakeInteraction:
    def __init__(self) -> None:
        self.pages: list[Any] = []

    async def signal(self, page: Any) -> Signal:
        self.pages.append(page)
        return Signal.CAPTCHA


async def test_factory_uses_the_runtime_interaction_for_signals():
    page, _ = login_page()
    interaction = FakeInteraction()
    runtime = Runtime()
    factory = typist_factory(runtime)
    runtime.register("interaction", interaction)  # registered after the factory: looked up late
    runtime.register("typist", factory)
    made = runtime.require("typist")(page)
    assert isinstance(made, PlaywrightTypist) and await made.signal() is Signal.CAPTCHA
    assert interaction.pages == [page]
    assert await made.page_url() == page.url
    assert "Secret" not in repr(made)
