"""The screen tools (openajar.environments.screen): an agent's eyes and hands on its environment's desktop, for
apps that are not web pages. Where they work (no_desktop elsewhere), the person first (human_in_control, also
between the parts of a long text), what the machine sends back bounded, the request on stdin only, and the
machine-side script (desktop/screen_ctl.py) with stand-ins for python-xlib and PIL: field codes, key names,
pacing and every key let go.

No X, no SSH and no machine here: tests/unit/test_environments_vmbrowser_docker.py has the real machine, and
the handback of this change says what was checked by hand on one."""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from openajar.core.errors import (
    AgentPaused,
    DeadlineExceeded,
    EngineError,
    HumanInControl,
    InvalidRequest,
    NotFound,
)
from openajar.core.models import ControlState, LaneControl
from openajar.core.tools import ToolImage
from openajar.core.toolsets import CORE, TOOLSETS
from openajar.env.models import ExecResult
from openajar.environments import routing, screen
from openajar.environments.errors import NoDesktop

ROOT = Path(__file__).resolve().parents[2]
CTL = ROOT / "src" / "openajar" / "environments" / "desktop" / "screen_ctl.py"
ON = "env/e1234567890-browser"
EM_DASH = "—"


def _png(w: int = 1280, h: int = 720) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h) + b"\x08\x02\x00\x00\x00"


class Machine:
    """The terminal lane's exec: records what the node sends, answers with canned JSON lines."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.answers: list[dict] = []

    async def exec(self, lane_id, cmd, *, stdin=None, timeout_s=None, max_bytes=None, **_):
        req = json.loads(stdin)
        assert req.pop("script") == screen.ctl_script()
        self.calls.append((cmd, req))
        out = self.answers.pop(0) if self.answers else {"ok": True}
        return ExecResult(exit_code=0, stdout=json.dumps(out) + "\n", stderr="", duration_s=0.1)


class Vm:
    def __init__(self, size=(1920, 1080), wants=True):
        self.size, self.wants, self.soon = size, wants, []

    def screen_size(self, env_id):
        return self.size

    def wants_desktop(self):
        return self.wants

    def screen_soon(self, env):
        self.soon.append(env.id)


class Mgr:
    def __init__(self, browser="vm"):
        self.env = SimpleNamespace(id="e1234567890", browser=browser, browser_lane=ON,
                                   terminal_lane="env/e1234567890", lanes={}, title="t")
        self.machine, self.vm, self.proofs = Machine(), Vm(), []

    def lane_env(self, lane_id):
        return (self.env, "browser") if lane_id in (ON, "env/e1234567890") else None

    def vm_browsers(self):
        return self.vm

    def envs(self):
        return self.machine

    def keep_proof(self, env, png, **kw):
        self.proofs.append(kw)
        return {"id": "pabcdefghjk", "title": kw["title"], "url": "http://x/proof.png"}


class Lanes:
    def __init__(self):
        self.state = ControlState.AGENT
        self.listeners: list = []

    def control(self, lane_id):
        assert lane_id == ON
        return LaneControl(state=self.state)

    def add_control_listener(self, cb):
        self.listeners.append(cb)

    def remove_control_listener(self, cb):
        self.listeners.remove(cb)

    def take_over(self, state=ControlState.HUMAN):
        old, self.state = LaneControl(state=self.state), state
        for cb in list(self.listeners):
            cb(ON, old, LaneControl(state=state))


class Ctx:
    def __init__(self, mgr: Mgr, lanes: Lanes):
        services = {"environments": mgr, "lanes": lanes}
        self.runtime = SimpleNamespace(require=services.__getitem__, get=services.get)
        self.authorized: list[tuple] = []
        self.traces: list[tuple] = []
        self.grant = None

    async def authorize(self, effect, *, lane_id=None, summary="", content=None, **_):
        self.authorized.append((effect, lane_id, summary, content))

    def trace(self, kind, **kw):
        self.traces.append((kind, kw))


@pytest.fixture
def world():
    mgr, lanes = Mgr(), Lanes()
    return SimpleNamespace(mgr=mgr, lanes=lanes, ctx=Ctx(mgr, lanes), machine=mgr.machine)


def _look(**extra):
    return {"ok": True, "size": [1920, 1080], "pointer": [5, 6], "area": [0, 0, 1920, 1080], "scale": 1.5,
            "png": base64.b64encode(_png()).decode(),
            "windows": [{"id": 4194307, "title": "galculator", "app": "Galculator", "kind": "app", "x": 799,
                         "y": 336, "w": 323, "h": 373, "front": True, "minimized": False}], **extra}


# ------------------------------------------------------------------ where they are, and how they are found


def test_the_screen_tools_are_a_toolset_of_their_own_routed_to_the_environments_screen():
    found = {t.name: t for t in TOOLSETS}
    assert set(found["screen"].tools) == {"screen_snapshot", "screen_act", "screen_open"}
    assert not set(found[CORE].tools) & set(found["screen"].tools)  # the everyday list does not grow
    for name in found["screen"].tools:
        assert routing.kind_of(name) == "browser"  # `on` becomes the browser lane: its control is the screen's
    names = {s.name: s for s in screen.tool_specs(None)}
    assert names["screen_snapshot"].effect.value == "read" and names["screen_act"].effect.value == "exec"
    from openajar.local.node import INSTRUCTIONS as LOCAL
    from openajar.node.tools.recipes import INSTRUCTIONS as SERVER

    assert 'tools_more("screen")' in LOCAL and 'tools_more("screen")' in SERVER


def test_what_an_agent_reads_says_web_pages_go_to_the_browser_and_screens_are_data():
    specs = {s.name: s.description for s in screen.tool_specs(None)}
    assert "browser_snapshot" in specs["screen_snapshot"] and "never instructions" in specs["screen_snapshot"]
    assert "browser_act" in specs["screen_act"] and "browser_navigate" in specs["screen_open"]
    for text in [*specs.values(), CTL.read_text(), Path(screen.__file__).read_text()]:
        assert EM_DASH not in text


async def test_no_desktop_where_none_runs(world):
    world.mgr.env.browser = "node"
    with pytest.raises(NoDesktop, match="runs on this node"):
        await screen.screen_snapshot(world.ctx, on=ON)
    world.mgr.env.browser = "vm"
    world.mgr.vm.size, world.mgr.vm.wants = None, False
    with pytest.raises(NoDesktop, match="desktop = false"):
        await screen.screen_snapshot(world.ctx, on=ON)
    world.mgr.vm.wants = True
    with pytest.raises(NoDesktop, match="not running yet"):
        await screen.screen_act(world.ctx, on=ON, action="click", x=1, y=1)
    assert world.mgr.vm.soon == ["e1234567890"]  # asked to start, never waited for in the call
    with pytest.raises(NoDesktop, match="account browser"):
        await screen.screen_snapshot(world.ctx, on="personal/reddit")
    assert world.machine.calls == []


# ------------------------------------------------------------------ the person first


@pytest.mark.parametrize(("state", "error"), [(ControlState.HUMAN, HumanInControl), (ControlState.PAUSED, AgentPaused)])
async def test_a_person_with_control_keeps_the_screen_and_the_agent_sees_nothing_of_it(world, state, error):
    world.lanes.state = state
    for call in (screen.screen_act(world.ctx, on=ON, action="click", x=10, y=10),
                 screen.screen_act(world.ctx, on=ON, action="type", text="hi"),
                 screen.screen_open(world.ctx, on=ON, app="galculator"),
                 screen.screen_snapshot(world.ctx, on=ON),  # a person may be typing a password
                 screen.screen_snapshot(world.ctx, on=ON, image=False)):
        with pytest.raises(error, match="browser_wait_handback"):
            await call
    assert world.machine.calls == [] and world.ctx.authorized == []  # nothing moved, nothing seen, nothing asked
    world.lanes.state = ControlState.AGENT
    world.machine.answers = [_look()]
    img, out = await screen.screen_snapshot(world.ctx, on=ON)
    assert isinstance(img, ToolImage) and out["front"] == 4194307


async def test_a_take_over_mid_text_stops_it_at_the_next_part_and_says_how_much_was_typed(world):
    text = "x" * (screen.TYPE_PART * 2 + 5)
    calls = []

    class Flip(Machine):
        async def exec(self, lane_id, cmd, **kw):
            res = await Machine.exec(self, lane_id, cmd, **kw)
            calls.append(1)
            world.lanes.state = ControlState.HUMAN  # the person takes over while the first part is typed
            return res

    world.mgr.machine = Flip()
    with pytest.raises(HumanInControl, match=f"{screen.TYPE_PART} of {len(text)} characters were typed"):
        await screen.screen_act(world.ctx, on=ON, action="type", text=text)
    assert len(calls) == 1


class Typist(Machine):
    """A machine that types until the node's stop is set, as screen_ctl.py does on SIGTERM."""

    def __init__(self):
        super().__init__()
        self.stops: list = []

    async def exec(self, lane_id, cmd, *, stdin=None, stop=None, **kw):
        req = json.loads(stdin)
        self.stops.append(stop)
        if req.get("action") == "type" and len(self.stops) == 2 or req.get("action") == "drag":
            await asyncio.wait_for(stop.wait(), 5)  # it runs until the node stops it
            out = {"ok": False, "kind": "stopped", "error": "the action was stopped", "typed": 7}
            return ExecResult(exit_code=0, stdout=json.dumps(out) + "\n", stderr="", duration_s=0.1)
        return await Machine.exec(self, lane_id, cmd, stdin=stdin, **kw)


@pytest.mark.parametrize(("state", "error"), [(ControlState.HUMAN, HumanInControl), (ControlState.PAUSED, AgentPaused)])
async def test_a_take_over_stops_the_part_being_typed_at_once_and_says_how_much_was_typed(world, state, error):
    world.mgr.machine = typist = Typist()
    text = "z" * (screen.TYPE_PART * 3)
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(screen.screen_act(world.ctx, on=ON, action="type", text=text))
    while len(typist.stops) < 2:
        await asyncio.sleep(0.01)
    began = loop.time()
    world.lanes.take_over(state)
    with pytest.raises(error, match=f"{screen.TYPE_PART + 7} of {len(text)} characters were typed before it was "
                                    "stopped; the rest was not, and every key it held was let go"):
        await task
    assert loop.time() - began < 1.0 and len(typist.stops) == 2  # the third part never went
    assert world.lanes.listeners == []  # the listener goes with the call


async def test_a_take_over_stops_a_drag_and_every_button_is_let_go(world):
    world.mgr.machine = typist = Typist()
    task = asyncio.ensure_future(screen.screen_act(world.ctx, on=ON, action="drag", x=1, y=1, to_x=9, to_y=9))
    while not typist.stops:
        await asyncio.sleep(0.01)
    world.lanes.take_over()
    with pytest.raises(HumanInControl, match="drag was stopped part way, and every key and button it held"):
        await task
    assert world.lanes.listeners == []


async def test_control_changes_on_other_lanes_do_not_stop_the_agent(world):
    world.mgr.machine = typist = Typist()
    task = asyncio.ensure_future(screen.screen_act(world.ctx, on=ON, action="drag", x=1, y=1, to_x=9, to_y=9))
    while not typist.stops:
        await asyncio.sleep(0.01)
    for cb in list(world.lanes.listeners):
        cb("personal/reddit", LaneControl(state=ControlState.AGENT), LaneControl(state=ControlState.HUMAN))
    await asyncio.sleep(0.05)
    assert not task.done() and not typist.stops[0].is_set()
    world.lanes.take_over()
    with pytest.raises(HumanInControl):
        await task


async def test_long_text_goes_in_parts_on_stdin_and_never_on_a_command_line(world):
    secret = "pa55-" + "y" * (screen.TYPE_PART + 10)
    await screen.screen_act(world.ctx, on=ON, action="type", text=secret)
    sent = world.machine.calls
    assert [r["text"] for _c, r in sent] == [secret[:screen.TYPE_PART], secret[screen.TYPE_PART:]]
    assert [r["settle"] for _c, r in sent] == [False, True]  # only the last part waits for the screen to settle
    assert all("pa55" not in cmd and cmd.startswith(screen.PYTHON + " -I -c ") for cmd, _r in sent)
    effect, lane, _summary, content = world.ctx.authorized[0]
    assert effect.value == "exec" and lane == ON and content["text"] == secret  # the audit trail has what was typed
    assert routing._summary("screen_act", {"action": "type", "text": secret}) == f"type {len(secret)} characters"
    assert routing._summary("screen_act", {"action": "click", "x": 3, "y": 4}) == "click 3,4"


# ------------------------------------------------------------------ what goes to the machine, and back


async def test_a_snapshot_says_how_its_picture_maps_to_the_screen_and_can_be_kept_as_proof(world):
    world.machine.answers = [_look(colors=256)]
    img, out = await screen.screen_snapshot(world.ctx, on=ON, proof="calculator shows 15")
    assert img.data == _png() and img.mime_type == "image/png"
    assert out["size"] == [1920, 1080] and out["pointer"] == [5, 6]
    assert out["image"] == {"width": 1280, "height": 720, "scale": 1.5, "colors": 256,
                            "area": {"x": 0, "y": 0, "w": 1920, "h": 1080}}
    assert out["proof"]["id"] == "pabcdefghjk" and world.mgr.proofs[0]["title"] == "calculator shows 15"
    assert world.ctx.traces[0][0] == "screen_snapshot_proof"
    assert world.machine.calls[0][1] == {"action_kind": "look", "image": True, "max_side": screen.MAX_SIDE,
                                         "png_max": screen.PNG_MAX}
    world.machine.answers = [{**_look(), "png": None}]
    out = await screen.screen_snapshot(world.ctx, on=ON, image=False, window=4194307)
    assert "image" not in out and world.machine.calls[1][1]["window"] == 4194307


async def test_what_the_machine_sends_back_is_bounded(world):
    junk = [{"id": i + 1, "title": "T\x1b]0;evil\x07" + "a" * 900, "app": "A" * 300, "kind": "root", "x": 1, "y": 2,
             "w": 3, "h": 4, "front": "yes"} for i in range(500)]
    junk += [{"id": True, "x": 1, "y": 1, "w": 1, "h": 1}, {"id": 9, "x": 1e9, "y": 0, "w": 1, "h": 1}, "nope"]
    world.machine.answers = [_look(windows=junk, pointer=["a", 1], size=[1920])]
    _img, out = await screen.screen_snapshot(world.ctx, on=ON)
    assert len(out["windows"]) == screen.WINDOWS_MAX
    w = out["windows"][0]
    assert len(w["title"]) == screen.TITLE_MAX and "\x1b" not in w["title"] and "\x07" not in w["title"]
    assert len(w["app"]) == 60 and w["kind"] == "app" and "front" not in w
    assert out["pointer"] is None and out["size"] is None
    for png in (b"GIF89a" + b"0" * 40, b"\x89PNG" + b"0" * 10, _png(9000, 10)):
        world.machine.answers = [_look(png=base64.b64encode(png).decode())]
        with pytest.raises(EngineError):
            await screen.screen_snapshot(world.ctx, on=ON)
    world.machine.answers = [_look(png="A" * (screen.PNG_MAX * 2))]
    with pytest.raises(EngineError, match="too large"):
        await screen.screen_snapshot(world.ctx, on=ON)


async def test_an_actions_result_names_the_window_in_front_and_what_opened(world):
    win = _look()["windows"][0]
    world.machine.answers = [{"ok": True, "front": win, "opened": [win], "closed": [7, "x", -1], "pointer": [1, 2]}]
    out = await screen.screen_act(world.ctx, on=ON, action="drag", x=10, y=20, to_x=300, to_y=400)
    assert out["front"]["title"] == "galculator" and out["opened"][0]["id"] == win["id"] and out["closed"] == [7]
    req = world.machine.calls[0][1]
    assert req["action"] == "drag" and (req["x"], req["y"], req["to_x"], req["to_y"]) == (10, 20, 300, 400)
    assert req["deadline_s"] == screen.ACT_S


async def test_typing_says_which_window_took_the_keys_and_can_bring_one_forward_first(world):
    """Eval t11: the keys went to the browser and only `front` (after) hinted at it. keys_to says where they went;
    `window` with type or key brings that window to the front first."""
    term = {"id": 10485763, "title": "root@vm: /work", "app": "Lxterminal", "kind": "terminal", "x": 180, "y": 204,
            "w": 658, "h": 448, "front": True}
    world.machine.answers = [{"ok": True, "front": term, "keys_to": term, "pointer": [1, 2]}]
    out = await screen.screen_act(world.ctx, on=ON, action="type", text="echo hi", window=10485763)
    assert out["keys_to"]["kind"] == "terminal" and world.machine.calls[0][1]["window"] == 10485763
    world.machine.answers = [{"ok": True, "front": term, "pointer": [1, 2]}]
    assert (await screen.screen_act(world.ctx, on=ON, action="key", keys="Return"))["keys_to"] is None
    world.machine.answers = [{"ok": True, "front": term, "pointer": [1, 2]}]
    assert "keys_to" not in await screen.screen_act(world.ctx, on=ON, action="click", x=5, y=5)
    with pytest.raises(InvalidRequest, match="window"):
        await screen.screen_act(world.ctx, on=ON, action="type", text="x", window=-4)


@pytest.mark.parametrize(("kwargs", "match"), [
    ({"action": "click"}, "needs x and y"),
    ({"action": "click", "x": 1920, "y": 5}, "outside the screen"),
    ({"action": "move", "x": -1, "y": 5}, "outside the screen"),
    ({"action": "drag", "x": 1, "y": 1}, "to_x and to_y"),
    ({"action": "type"}, "needs text"),
    ({"action": "type", "text": "a" * (screen.TEXT_MAX + 1)}, "file_write"),
    ({"action": "key", "keys": " "}, "ctrl\\+s"),
    ({"action": "focus"}, "window"),
    ({"action": "scroll", "amount": 99}, "1 to 30"),
])
async def test_an_action_without_what_it_needs_moves_nothing(world, kwargs, match):
    with pytest.raises(InvalidRequest, match=match):
        await screen.screen_act(world.ctx, on=ON, **kwargs)
    assert world.machine.calls == []


@pytest.mark.parametrize(("kind", "error"), [("invalid", InvalidRequest), ("no_window", NotFound),
                                             ("time_limit", DeadlineExceeded), ("failed", EngineError)])
async def test_the_machines_refusals_keep_their_meaning(world, kind, error):
    world.machine.answers = [{"ok": False, "kind": kind, "error": "said\x00 there"}]
    with pytest.raises(error, match="said  there"):
        await screen.screen_act(world.ctx, on=ON, action="key", keys="Return")


async def test_screen_open_lists_the_apps_or_starts_one_and_says_its_window(world):
    world.machine.answers = [{"ok": True, "apps": [{"name": "Galculator", "id": "galculator", "runs": "galculator"},
                                                   {"name": 5}]}]
    out = await screen.screen_open(world.ctx, on=ON)
    assert out["apps"] == [{"name": "Galculator", "id": "galculator", "runs": "galculator"}]
    assert world.ctx.authorized[-1][0].value == "read"
    world.machine.answers = [{"ok": True, "app": "Galculator", "running": True, "opened": [_look()["windows"][0]]}]
    out = await screen.screen_open(world.ctx, on=ON, app="galculator", file="/work/a.txt")
    assert out["window"]["title"] == "galculator" and world.ctx.authorized[-1][0].value == "exec"
    assert world.machine.calls[-1][1] == {"action_kind": "open", "app": "galculator", "file": "/work/a.txt",
                                          "wait_s": screen.OPEN_WAIT_S}
    world.machine.answers = [{"ok": True, "app": "false", "running": False, "opened": [], "exit_code": 1,
                              "output": "boom\x1b[31m"}]
    out = await screen.screen_open(world.ctx, on=ON, app="false")
    assert out["ok"] is False and out["exit_code"] == 1 and "\x1b" not in out["output"]
    with pytest.raises(InvalidRequest):
        await screen.screen_open(world.ctx, on=ON, app="a\nb")


# ------------------------------------------------------------------ the machine's side


@pytest.fixture
def ctl(monkeypatch):
    """desktop/screen_ctl.py with stand-ins for python-xlib and PIL (the machine has them, the node does not)."""
    xlib = types.ModuleType("Xlib")
    for name in ("X", "XK", "Xatom", "display", "xauth", "error", "ext", "ext.xtest", "protocol", "protocol.event"):
        sub = types.ModuleType(f"Xlib.{name}")
        monkeypatch.setitem(sys.modules, f"Xlib.{name}", sub)
        parent, _, leaf = f"Xlib.{name}".rpartition(".")
        setattr(sys.modules[parent] if parent != "Xlib" else xlib, leaf, sub)
    xlib.error.XError = type("XError", (Exception,), {})
    xlib.error.DisplayError = type("DisplayError", (Exception,), {})
    xlib.X.AnyPropertyType = 0
    names = {"Return": 0xFF0D, "Tab": 0xFF09, "Shift_L": 0xFFE1, "Control_L": 0xFFE3, "Alt_L": 0xFFE9, "F5": 0xFFC2,
             "Delete": 0xFFFF, "Page_Up": 0xFF55, "plus": 0x2B, "Super_L": 0xFFEB}
    xlib.XK.string_to_keysym = lambda n: names.get(n, 0)
    pil = types.ModuleType("PIL")
    pil.Image = types.ModuleType("PIL.Image")
    monkeypatch.setitem(sys.modules, "Xlib", xlib)
    monkeypatch.setitem(sys.modules, "PIL", pil)
    monkeypatch.setitem(sys.modules, "PIL.Image", pil.Image)
    ns: dict = {"__name__": "not-the-machine"}
    exec(compile(CTL.read_text(), "screen_ctl.py", "exec"), ns)  # noqa: S102 - our own file
    return ns


def test_the_machine_script_does_nothing_on_import_and_agrees_with_the_node(ctl):
    assert "main" in ctl and ctl["PNG_MAX"] == screen.PNG_MAX and ctl["MAX_SIDE"] == screen.MAX_SIDE
    assert ctl["TEXT_MAX"] >= screen.TEXT_MAX
    from openajar.environments import desklink

    assert {k: ctl["APP_ENV"][k] for k in desklink.SCREEN_ENV} == desklink.SCREEN_ENV


def test_a_desktop_entrys_exec_line_takes_the_file_where_it_says(ctl):
    argv = ctl["exec_argv"]
    assert argv("mousepad %U", "/work/a b.txt") == ["mousepad", "/work/a b.txt"]
    assert argv("galculator") == ["galculator"]
    assert argv("app --x %f %i %c", "f") == ["app", "--x", "f"]
    assert argv("app 100%%") == ["app", "100%"]
    assert argv("viewer", "f") == ["viewer", "f"]
    assert argv('broken "quote') is None


def test_the_app_list_never_offers_a_vnc_server(ctl, tmp_path, monkeypatch):
    for name, exe in (("x11vnc", "x11vnc -gui"), ("galculator", "galculator"), ("hidden", "h")):
        extra = "NoDisplay=true\n" if name == "hidden" else ""
        (tmp_path / f"{name}.desktop").write_text(f"[Desktop Entry]\nType=Application\nName={name.title()}\n"
                                                  f"Exec={exe}\n{extra}")
    monkeypatch.setitem(ctl, "APP_DIRS", (str(tmp_path),))
    assert [e[1] for e in ctl["desktop_entries"]()] == ["galculator"]
    assert ctl["find_app"]("GALCULATOR")[0] == ["galculator"]


def test_typed_characters_become_keysyms_and_control_characters_are_refused(ctl):
    sym = ctl["Screen"].keysym_of_char
    assert sym("a") == ord("a") and sym("é") == 0xE9 and sym("\n") == 0xFF0D and sym("\t") == 0xFF09
    assert sym("ą") == 0x01000105 and sym("✔") == 0x01002714  # Unicode keysyms: a spare key is mapped for them
    with pytest.raises(ctl["Stop"], match="control character"):
        sym("\x1b")


def test_pauses_are_a_persons(ctl):
    draws = [ctl["draw_ms"](90, 260) for _ in range(300)]
    assert all(90 <= d <= 260 for d in draws) and len({round(d) for d in draws}) > 50
    assert all(40 <= ctl["hold_ms"]() <= 220 for _ in range(200))
    assert ctl["glide_ms"](1000) > ctl["glide_ms"](0) * 0.6  # far reaches take longer, within 110 to 1200 ms
    assert all(110 <= ctl["glide_ms"](d) <= 1200 for d in (0, 50, 4000) for _ in range(50))


class Keys:
    """A Screen whose keyboard is a list: (code, down), with a US-like map: capitals need shift."""

    def __init__(self, ctl):
        scr = ctl["Screen"].__new__(ctl["Screen"])
        scr.pressed_keys, scr.pressed_buttons, scr.mapped, scr.deadline, scr.typed = [], [], {}, None, 0
        self.events: list[tuple[int, bool]] = []

        def key(code, down):
            self.events.append((code, down))
            (scr.pressed_keys.append if down else scr.pressed_keys.remove)(code)

        scr.key = key
        scr.sleep_ms = lambda ms: None
        scr.code_for = lambda s: (s & 0xFF if s < 0x100 else s, chr(s).isupper() if s < 0x100 else False)
        scr.d = SimpleNamespace(sync=lambda: None)
        self.scr = scr


def _balanced(events):
    held: list[int] = []
    for code, down in events:
        assert (code not in held) if down else (code in held), (code, down)
        (held.append if down else held.remove)(code)
    return held


def test_typing_holds_shift_round_capitals_and_lets_every_key_go(ctl):
    k = Keys(ctl)
    k.scr.type_text("Hello WOrld")
    assert _balanced(k.events) == []
    shift = 0xFFE1
    downs = [c for c, d in k.events if d]
    assert downs.count(shift) == 2 and downs[0] == shift and downs[1] == ord("H")
    assert "".join(chr(c) for c in downs if c != shift) == "Hello WOrld"


def test_a_key_combination_presses_its_modifiers_first_and_lets_go_after(ctl):
    k = Keys(ctl)
    k.scr.combo("ctrl+shift+T")
    assert k.events == [(0xFFE3, True), (0xFFE1, True), (ord("t"), True), (ord("t"), False), (0xFFE1, False),
                        (0xFFE3, False)]
    with pytest.raises(ctl["Stop"], match="not a modifier"):
        k.scr.combo("ctrl+bogus+t")
    with pytest.raises(ctl["Stop"], match="no key is named"):
        k.scr.combo("ctrl+nosuchkey")
    assert _balanced(k.events) == []


def test_a_stop_is_seen_at_the_next_pause_and_every_key_comes_up(ctl, monkeypatch):
    """SIGTERM (the node's stop on a take over) while keys are down: never raised inside an X request, raised
    at the next pause, the characters typed counted, and act() lets go of the rest."""
    k = Keys(ctl)
    stop = ctl["STOP"]
    monkeypatch.setitem(stop, "asked", False)
    monkeypatch.setitem(stop, "at_once", False)
    down = k.scr.key

    def key(code, is_down):
        down(code, is_down)
        if is_down and code == ord("c"):
            ctl["_stopped"]()  # the signal lands while a key goes down: only noted
    k.scr.key = key
    k.scr.sleep_ms = ctl["Screen"].sleep_ms.__get__(k.scr)
    monkeypatch.setattr(ctl["time"], "sleep", lambda s: None)
    with pytest.raises(ctl["Stop"], match="stopped") as e:
        k.scr.type_text("abcdef")
    assert e.value.kind == "stopped" and k.scr.typed == 3
    k.scr.button = lambda n, down: None
    k.scr.let_go()
    assert k.scr.pressed_keys == [] and _balanced(k.events) == []
    stop["at_once"] = True
    with pytest.raises(ctl["Stop"], match="stopped"):
        ctl["_stopped"]()  # outside an act (a look, an open): at once


def test_keys_go_to_the_window_asked_for_and_the_answer_names_where_they_went(ctl):
    k = Keys(ctl)
    wins = [{"id": 5, "kind": "terminal"}, {"id": 9, "kind": "browser"}]
    active = [9]
    k.scr.windows = lambda: wins
    k.scr.active_id = lambda: active[0]
    k.scr.front = lambda: next(w for w in wins if w["id"] == active[0])
    k.scr.bring = lambda wid, wait_s=1.5: active.__setitem__(0, wid) or True
    assert ctl["_do_keys"](k.scr, "key", {"keys": "Return"})["id"] == 9  # where the focus was: the browser
    assert ctl["_do_keys"](k.scr, "type", {"text": "ab", "window": 5})["id"] == 5
    assert "".join(chr(c) for c, d in k.events if d and c < 0x100) == "ab"
    with pytest.raises(ctl["Stop"], match="no window 77"):
        ctl["_do_keys"](k.scr, "type", {"text": "a", "window": 77})
    k.scr.bring = lambda wid, wait_s=1.5: False
    with pytest.raises(ctl["Stop"], match="did not take the keyboard"):
        ctl["_do_keys"](k.scr, "type", {"text": "a", "window": 9})


def test_an_action_that_runs_out_of_time_lets_go_of_what_it_held(ctl):
    k = Keys(ctl)
    k.scr.deadline = 0.0  # long past

    def late(_ms):
        k.scr.check_time()

    k.scr.sleep_ms = late
    with pytest.raises(ctl["Stop"], match="out of time"):
        k.scr.type_text("abc")
    assert k.scr.pressed_keys  # the key it pressed is still down here ...
    k.scr.button = lambda n, down: None
    k.scr.let_go()  # ... and act() always ends with this
    assert k.scr.pressed_keys == [] and _balanced(k.events) == []


async def test_the_live_views_agent_pointer_follows_the_screen_tools(world):
    """screen_act moves the screen's real pointer: the live view's labelled pointer goes there too, in
    screen pixels, and typing shows where the pointer is."""
    from openajar.node.agent_pointer import POINTERS

    seen: list[dict] = []
    unwatch = POINTERS.watch(ON, seen.append)
    try:
        world.machine.answers = [{"ok": True, "pointer": [10, 20]}, {"ok": True, "pointer": [301, 401]},
                                 {"ok": True, "pointer": [301, 401]}]
        await screen.screen_act(world.ctx, on=ON, action="click", x=10, y=20)
        await screen.screen_act(world.ctx, on=ON, action="drag", x=10, y=20, to_x=300, to_y=400)
        await screen.screen_act(world.ctx, on=ON, action="type", text="hi")
        assert seen == [{"x": 10, "y": 20, "w": 1920, "h": 1080, "kind": "press"},
                        {"x": 300, "y": 400, "w": 1920, "h": 1080, "kind": "press"},
                        {"x": 301, "y": 401, "w": 1920, "h": 1080, "kind": "type"}]
        world.lanes.state = ControlState.HUMAN
        with pytest.raises(HumanInControl):
            await screen.screen_act(world.ctx, on=ON, action="click", x=5, y=5)
        assert len(seen) == 3  # nothing while a person has control
    finally:
        unwatch()
