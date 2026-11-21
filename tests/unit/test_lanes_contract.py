"""The lane manager, its fakes and the LaneManager protocol agree on names and keyword arguments."""

from __future__ import annotations

import inspect

from fakes import ControlFakeLaneManager, FakeLaneManager

from openajar.node.interfaces import DisplayManager, LaneManager
from openajar.node.lanes import LaneManagerImpl


def methods(proto) -> dict:
    return {name: fn for name, fn in vars(proto).items() if callable(fn) and not name.startswith("_")}


def keywords(fn) -> set[str]:
    return {p.name for p in inspect.signature(fn).parameters.values() if p.kind is p.KEYWORD_ONLY}


def test_the_real_manager_and_the_control_fake_implement_the_whole_protocol():
    for impl in (LaneManagerImpl, ControlFakeLaneManager):
        for name, fn in methods(LaneManager).items():
            assert hasattr(impl, name), f"{impl.__name__} has no {name}"
            if name == "using":
                continue  # a context manager factory in both
            missing = keywords(fn) - keywords(getattr(impl, name))
            assert not missing, f"{impl.__name__}.{name} lacks keyword arguments {missing}"


def test_the_plain_fake_leaves_out_only_the_human_control_actions():
    human = {"take_control", "hand_back", "stop_agent", "request_control", "deny_request", "note_human_input",
             "wait_for_agent_control"}
    missing = {name for name in methods(LaneManager) if not hasattr(FakeLaneManager, name)}
    assert missing == human


def test_displays_take_a_size():
    from openajar.node.display import DisplayManagerImpl

    assert "size" in keywords(DisplayManager.acquire) and "size" in keywords(DisplayManagerImpl.acquire)
    assert callable(DisplayManagerImpl.healthy)
