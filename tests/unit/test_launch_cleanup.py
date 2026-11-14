"""A launch that fails or is cancelled after the browser started closes that browser.

The lane only gets a session once `launch` returns, so a browser left running by a failed
launch has nobody to close it: camoufox-bin and the Playwright driver keep running and the warm
slot looks free. Each step after AsyncNewBrowser is made to raise, and to be cancelled."""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import MagicMock

import pytest

from agentos.core.errors import EngineError, InvalidRequest
from agentos.core.models import LaneSpec
from agentos.node.camoufox import engine as ec
from agentos.node.camoufox import session as camoufox_session

STEPS = ["wait_for_pid", "settle_pin", "place_window", "keyboard", "guard", "identity"]


class _Playwright:
    async def start(self):
        return self

    async def stop(self):
        return None


@pytest.fixture
def rig(monkeypatch, tmp_path):
    """An engine whose browser start succeeds with fakes, and a hook to break one later step."""
    camoufox_api = types.ModuleType("camoufox.async_api")

    async def new_browser(pw, **kw):
        return MagicMock(name="context")

    camoufox_api.AsyncNewBrowser = new_browser
    playwright_api = types.ModuleType("playwright.async_api")
    playwright_api.async_playwright = lambda: _Playwright()
    monkeypatch.setitem(sys.modules, "camoufox.async_api", camoufox_api)
    monkeypatch.setitem(sys.modules, "playwright.async_api", playwright_api)
    for name in ("capture_driver_stderr", "watch_profile", "watch_browser", "forget_browser"):
        monkeypatch.setattr(ec, name, lambda *a, **k: None)
    monkeypatch.setattr(ec, "browser_pids", lambda *a, **k: [])
    # a session reads these from its own module
    monkeypatch.setattr(camoufox_session, "forget_browser", lambda *a, **k: None)
    monkeypatch.setattr(camoufox_session, "browser_pids", lambda *a, **k: [])

    engine = ec.CamoufoxEngine()
    plan = ec.LaunchPlan(options={}, config={}, first=False)
    monkeypatch.setattr(engine, "_options", lambda spec, **kw: {})
    monkeypatch.setattr(engine, "plan_launch", lambda lane_id, opts, pin: plan)

    state = {"broken": None, "how": "raise", "closed": 0, "reached": []}

    def step(name):
        state["reached"].append(name)
        if state["broken"] == name:
            if state["how"] == "cancel":
                raise asyncio.CancelledError
            if state["how"] == "agentos":
                raise InvalidRequest(f"{name} broke")
            raise RuntimeError(f"{name} broke")

    async def wait_for_pid(profile_dir, timeout=5.0):
        step("wait_for_pid")
        return 4242

    def settle_pin(*a):
        step("settle_pin")

    async def place_window(*a, **k):
        step("place_window")

    async def prepare(*a):
        step("keyboard")

    class Guard:
        def __init__(self, *a, **k):
            pass

        def attach(self, *a):
            step("guard")
            return self

    async def install_identity(*a, **k):
        step("identity")

    async def close(self):
        state["closed"] += 1

    monkeypatch.setattr(ec, "_wait_for_pid", wait_for_pid)
    monkeypatch.setattr(engine, "settle_pin", settle_pin)
    monkeypatch.setattr(engine, "_place_window", place_window)
    monkeypatch.setattr(ec.xkeyboard, "prepare", prepare)
    monkeypatch.setattr(ec, "LaneGuard", Guard)
    monkeypatch.setattr(engine, "_install_identity", install_identity)
    monkeypatch.setattr(ec.CamoufoxSession, "close", close)

    async def launch():
        return await engine.launch(LaneSpec(id="a/b"), profile_dir=tmp_path / "profile",
                                   fingerprint_path=tmp_path / "fingerprint.json", display=":99", proxy=None)

    return state, launch


def test_a_launch_that_goes_through_closes_nothing(rig):
    state, launch = rig
    session = asyncio.run(launch())
    assert state["reached"] == STEPS
    assert state["closed"] == 0
    assert session.pid == 4242


@pytest.mark.parametrize("step", STEPS)
def test_a_step_that_raises_closes_the_browser(rig, step):
    state, launch = rig
    state["broken"] = step
    with pytest.raises(Exception) as caught:
        asyncio.run(launch())
    if step == "settle_pin":  # a pin failure reads as the engine's own error, as before
        assert isinstance(caught.value, EngineError) and "could not pin the fingerprint" in str(caught.value)
    else:
        assert isinstance(caught.value, RuntimeError) and f"{step} broke" in str(caught.value)
    assert state["closed"] == 1
    assert state["reached"][-1] == step


def test_an_agentos_error_from_the_pin_goes_up_as_it_is(rig):
    state, launch = rig
    state["broken"], state["how"] = "settle_pin", "agentos"
    with pytest.raises(InvalidRequest, match="settle_pin broke"):
        asyncio.run(launch())
    assert state["closed"] == 1


def test_a_session_that_cannot_be_built_still_stops_the_browser(rig, monkeypatch):
    state, launch = rig
    stopped = []

    def broken_init(self, *a, **k):
        raise RuntimeError("tracker broke")

    async def abandon(pw, profile_dir):
        stopped.append(profile_dir)

    monkeypatch.setattr(ec.CamoufoxSession, "__init__", broken_init)
    monkeypatch.setattr(ec.CamoufoxEngine, "_abandon_start", staticmethod(abandon))
    with pytest.raises(RuntimeError, match="tracker broke"):
        asyncio.run(launch())
    assert len(stopped) == 1 and state["closed"] == 0


@pytest.mark.parametrize("step", STEPS)
def test_a_step_that_is_cancelled_closes_the_browser(rig, step):
    state, launch = rig
    state["broken"] = step
    state["how"] = "cancel"
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(launch())
    assert state["closed"] == 1


def test_cancelling_the_open_while_it_waits_closes_the_browser(rig, monkeypatch):
    state, launch = rig
    started = asyncio.Event()

    async def slow_wait(profile_dir, timeout=5.0):
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(ec, "_wait_for_pid", slow_wait)

    async def main():
        task = asyncio.create_task(launch())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert state["closed"] == 1


def test_run_to_end_keeps_the_cancellation_when_the_teardown_fails():
    from agentos.core.aio import run_to_end

    async def teardown(started):
        started.set()
        await asyncio.sleep(0.05)
        raise RuntimeError("close blew up")

    async def main():
        started = asyncio.Event()
        task = asyncio.create_task(run_to_end(teardown(started)))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
