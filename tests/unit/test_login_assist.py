"""Assisted login pieces that need no browser: the screenshot redaction, the terminal form, the live
view's routes, and how values are read from a body. The whole flow on a real Camoufox is
tests/node/test_login_assist_node.py; the step reader is tests/unit/test_login_steps.py."""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import numpy as np
import pytest
from fakes import FakeLaneManager
from starlette.testclient import TestClient
from typer.testing import CliRunner

from agentos.cli import app
from agentos.commands import base as cli_base
from agentos.core.config import NodeConfig
from agentos.core.errors import InvalidRequest
from agentos.core.models import LaneSpec
from agentos.node.login_assist import read_values
from agentos.node.login_page import REDACT_RGBA, _unfilter, action_locator, parse_action, redact_png
from agentos.node.viewer_app import build_viewer_app
from agentos.node.viewer_tokens import ViewMode, ViewTokens

PASSWORD = "Tr0ub4dor&3-horse"


# ---- the screenshot -------------------------------------------------------------------------------

def _png(pixels: np.ndarray, filters: list[int]) -> bytes:
    """A PNG of `pixels` (h, w, bpp) with the given filter per row: a small reference encoder."""
    h, w, bpp = pixels.shape
    flat = pixels.reshape(h, w * bpp).astype(np.int32)
    rows = []
    for y in range(h):
        cur, up = flat[y], flat[y - 1] if y else np.zeros(w * bpp, dtype=np.int32)
        left = np.concatenate([np.zeros(bpp, dtype=np.int32), cur[:-bpp]])
        upleft = np.concatenate([np.zeros(bpp, dtype=np.int32), up[:-bpp]])
        f = filters[y % len(filters)]
        if f == 0:
            out = cur
        elif f == 1:
            out = cur - left
        elif f == 2:
            out = cur - up
        elif f == 3:
            out = cur - (left + up) // 2
        else:
            p = left + up - upleft
            pa, pb, pc = abs(p - left), abs(p - up), abs(p - upleft)
            pred = np.where((pa <= pb) & (pa <= pc), left, np.where(pb <= pc, up, upleft))
            out = cur - pred
        rows.append(bytes([f]) + (out % 256).astype(np.uint8).tobytes())
    color = 6 if bpp == 4 else 2

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    head = struct.pack(">IIBBBBB", w, h, 8, color, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", head) + chunk(b"IDAT", zlib.compress(b"".join(rows))) + \
        chunk(b"IEND", b"")


def _decode(data: bytes) -> np.ndarray:
    i, idat, head = 8, b"", b""
    while i < len(data):
        (n,) = struct.unpack(">I", data[i:i + 4])
        kind, body = data[i + 4:i + 8], data[i + 8:i + 8 + n]
        head = body if kind == b"IHDR" else head
        idat += body if kind == b"IDAT" else b""
        i += 12 + n
    w, h, _, color, *_ = struct.unpack(">IIBBBBB", head)
    bpp = 4 if color == 6 else 3
    return _unfilter(zlib.decompress(idat), w, h, bpp).reshape(h, w, bpp)


@pytest.mark.parametrize("bpp", [3, 4])
def test_the_screenshot_is_painted_over_each_hidden_field_and_nowhere_else(bpp):
    rng = np.random.default_rng(7)
    pixels = rng.integers(0, 256, size=(40, 60, bpp), dtype=np.uint8)
    for filters in ([1], [0, 1, 2, 3, 4]):  # Firefox writes Sub; the decoder reads them all
        png = _png(pixels, filters)
        assert np.array_equal(_decode(png), pixels)
        out = _decode(redact_png(png, [{"x": 10, "y": 5, "width": 20, "height": 8}], pad=2))
        assert (out[3:16, 8:33] == np.array(REDACT_RGBA[:bpp], dtype=np.uint8)).all()
        keep = np.ones(pixels.shape[:2], dtype=bool)
        keep[3:16, 8:33] = False
        assert np.array_equal(out[keep], pixels[keep])
    # boxes that run off the picture are clipped; no box gives the picture back as it was
    assert _decode(redact_png(png, [{"x": -5, "y": 30, "width": 500, "height": 50}])).shape == pixels.shape
    assert redact_png(png, []) == png
    with pytest.raises(ValueError):
        redact_png(b"not a png", [{"x": 0, "y": 0, "width": 1, "height": 1}])


def test_action_locators_round_trip_and_bind_the_frame_chain():
    shape = {"tag": "button", "type": "submit", "role": "", "id": "next", "name": "", "host": "", "form": 0}
    a = action_locator((), 3, shape, ["https://accounts.google.com"])
    b = action_locator((), 3, shape, ["https://evil.example"])
    assert a != b and parse_action(a)[:2] == ((), 3)
    assert parse_action(action_locator((2, 0), 1, shape, ["https://a.example", "https://a.example"]))[0] == (2, 0)
    for bad in ("", "pw1:top:1:" + "0" * 32, "ac1:top:x:" + "0" * 32):
        with pytest.raises(ValueError):
            parse_action(bad)


# ---- values from a body ---------------------------------------------------------------------------

def test_values_become_secrets_and_errors_never_quote_them():
    got = read_values({"username": "ada@example.org", "password": PASSWORD})
    assert got["password"].reveal() == PASSWORD and PASSWORD not in repr(got)
    for bad in ({"pin": PASSWORD}, {"password": 12}, {"password": ""}, {"password": "x" * 1025}, ["x"]):
        with pytest.raises(InvalidRequest) as refused:
            read_values(bad)
        assert PASSWORD not in refused.value.message and "—" not in refused.value.message


# ---- the terminal form ----------------------------------------------------------------------------

def _login(state: str, **kw) -> dict:
    return {"id": "la_1", "lane": "biz-a/instagram", "state": state, "version": kw.pop("version", 1),
            "note": kw.pop("note", ""), "error": None, "can_save": kw.pop("can_save", False), **kw}


def test_agentos_lane_login_asks_for_each_field_and_hides_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path / "home"))
    runner = CliRunner()
    assert runner.invoke(app, ["node", "init"]).exit_code == 0
    sign_in = {"id": "s1", "kind": "sign_in", "heading": "Instagram", "message": "", "number": None,
               "fields": [{"name": "username", "label": "Phone number, username, or email", "secret": False},
                          {"name": "password", "label": "Password", "secret": True}],
               "choices": [], "submit": "Log in"}
    code = {**sign_in, "id": "s2", "kind": "code", "fields": [{"name": "code", "label": "Code", "secret": True}]}
    script = [
        ("POST", "/api/lanes/biz-a/instagram/login", _login("ask", step=sign_in, note="Sign in to Instagram.")),
        ("POST", "/api/lanes/biz-a/instagram/login/step", _login("working", version=2, step=sign_in)),
        ("GET", "/api/lanes/biz-a/instagram/login", _login("ask", version=3, step=code)),
        ("POST", "/api/lanes/biz-a/instagram/login/step", _login("signed_in", version=4, can_save=True,
                                                                 note="Signed in to Instagram.")),
        ("POST", "/api/lanes/biz-a/instagram/login/save", _login("signed_in", version=5, note="Saved as x.")),
    ]
    sent: list[tuple[str, str, dict | None, dict]] = []

    def send(method, url, *, body, headers, timeout=15.0):
        want_method, want_path, answer = script[len(sent)]
        parts = urlsplit(url)
        sent.append((method, parts.path, json.loads(json.dumps(body)), parse_qs(parts.query)))  # the CLI clears it
        assert (method, parts.path) == (want_method, want_path)
        return httpx.Response(200, json=answer)

    monkeypatch.setattr(cli_base, "_send", send)
    r = runner.invoke(app, ["lane", "login", "biz-a/instagram"],
                      input=f"ada@example.org\n{PASSWORD}\n424 242\ny\n")
    assert r.exit_code == 0, r.output
    assert PASSWORD not in r.output and "424 242" not in r.output  # hidden input; the username echoes as typed
    assert "Sign in to Instagram." in r.output and "Saved as x." in r.output and "—" not in r.output
    assert sent[1][2] == {"step": "s1", "values": {"username": "ada@example.org", "password": PASSWORD}}
    assert sent[2][3] == {"after": ["2"], "wait": ["25"]}
    assert sent[3][2] == {"step": "s2", "values": {"code": "424 242"}}


# ---- the live view's section ----------------------------------------------------------------------

class FakeLogin:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def get(self, lane_id):
        return None

    async def wait_change(self, lane_id, version, timeout):
        return None

    def screenshot(self, lane_id):
        return None

    async def start(self, lane_id, *, by, url=None):
        self.calls.append(("start", {"by": by, "url": url}))
        return _login("ask")

    async def submit(self, lane_id, *, step, values, choice, by):
        self.calls.append(("step", {"step": step, "names": sorted(values), "kinds": {type(v).__name__
                                                                                      for v in values.values()}}))
        return _login("working")

    async def save(self, lane_id, *, by, name=None):
        self.calls.append(("save", {}))
        return _login("signed_in", saved="x")


def test_the_live_view_runs_the_form_only_for_a_person(tmp_path):
    """Logging in is a person's: an agent's control link (browser_view's) is refused every step of it, and an
    owner link (made in person) runs the form and saves to the vault (docs/THREAT-MODEL.md)."""
    config = NodeConfig(home=tmp_path)
    lanes = FakeLaneManager(config)
    import asyncio

    asyncio.run(lanes.create(LaneSpec(id="biz-a/instagram")))
    tokens = ViewTokens(Path(tmp_path) / "secrets" / "viewer.key")
    fake = FakeLogin()
    client = TestClient(build_viewer_app(lanes, config, tokens=tokens, login=lambda: fake))
    view = tokens.issue("biz-a/instagram", ViewMode.VIEW).token
    ctrl = tokens.issue("biz-a/instagram", ViewMode.CONTROL).token
    owner = tokens.issue("biz-a/instagram", ViewMode.CONTROL, owner=True).token
    base = "/view/biz-a/instagram/login"
    r = client.get(base, params={"t": view})
    assert r.status_code == 403 and "control link" in r.json()["error"]["message"]
    for method, body in (("GET", None), ("POST", {"action": "start"}), ("POST", {"action": "save"})):
        r = client.request(method, base, params={"t": ctrl}, json=body)
        assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required", (method, body)
    assert client.get(base + ".png", params={"t": ctrl}).status_code == 403
    assert fake.calls == []
    assert client.get(base, params={"t": owner}).json() == {"lane": "biz-a/instagram", "state": "none"}
    page = client.get("/view/biz-a/instagram", params={"t": owner}).text
    assert "login.js" in page and '"login":"/view/biz-a/instagram/login"' in page
    r = client.post(base, params={"t": owner}, json={"action": "start"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert client.post(base, params={"t": owner}, json={"action": "start"}).json()["owner_link"] is True
    r = client.post(base, params={"t": owner}, json={"action": "step", "step": "s1", "values": {"password": PASSWORD}})
    assert r.status_code == 200 and PASSWORD not in r.text
    assert fake.calls[-1] == ("step", {"step": "s1", "names": ["password"], "kinds": {"Secret"}})
    assert client.post(base, params={"t": owner}, json={"action": "save"}).status_code == 200
    assert client.get(base + ".png", params={"t": owner}).status_code == 404


def test_a_resume_after_hand_back_is_kept_until_it_ends_and_its_failure_is_logged(caplog):
    import asyncio
    import logging
    import time as _time
    from types import SimpleNamespace

    from agentos.core.models import ControlState
    from agentos.node.login_assist import LoginAssist, Session

    assist = LoginAssist(SimpleNamespace())
    now = _time.time()
    assist._sessions["a/b"] = Session(id="s1", lane="a/b", by="owner", started_at=now, updated_at=now,
                                      state="paused")
    gate = {}

    async def resume(lane, *, by):
        await gate["go"].wait()
        raise RuntimeError("boom")

    assist.resume = resume

    async def main():
        gate["go"] = asyncio.Event()
        assist._on_control("a/b", None, SimpleNamespace(state=ControlState.AGENT))
        assert len(assist._background) == 1  # held, so it cannot be collected mid-flight
        gate["go"].set()
        await asyncio.gather(*assist._background)
        await asyncio.sleep(0)
        assert not assist._background

    with caplog.at_level(logging.ERROR, logger="agentos.login"):
        asyncio.run(main())
    assert "resuming after the hand back failed" in caplog.text
