"""The agent's pointer for the live view: human.py tells where it aims, a lane's placer puts it on the screen."""

from __future__ import annotations

from types import SimpleNamespace

from openajar.node import agent_pointer
from openajar.node.agent_pointer import POINTERS


class Context:  # a browser context: weakly referenced, as Playwright's are
    pass


class Page:
    def __init__(self, context):
        self.context = context


def test_a_page_point_reaches_the_lanes_live_views_on_the_screen():
    context = Context()
    page = Page(context)
    agent_pointer.name_lane(SimpleNamespace(context=context), "env/e1-browser")
    seen: list[dict] = []
    unwatch = POINTERS.watch("env/e1-browser", seen.append)
    try:
        agent_pointer.told(page, 10, 20)  # no viewport read yet, and no placer: nothing
        agent_pointer.saw_viewport(page, 1000, 500)
        agent_pointer.told(page, 10, 20)
        assert seen == []
        POINTERS.place_with("env/e1-browser", lambda x, y, vw, vh, fresh: (int(x) + 100, int(y) + 200, 1920, 1080))
        agent_pointer.told(page, 10, 20)
        agent_pointer.told_here(page, "press")
        agent_pointer.told(page, 1, 1, "teleport")  # not a kind: nothing
        assert seen == [{"x": 110, "y": 220, "w": 1920, "h": 1080, "kind": "move"},
                        {"x": 110, "y": 220, "w": 1920, "h": 1080, "kind": "press"}]
        POINTERS.replace("env/e1-browser")  # the window moved: the same point again
        assert seen[-1]["kind"] == "move"
        POINTERS.place_with("env/e1-browser", lambda *a: None)  # minimized: it hides
        agent_pointer.told(page, 10, 20)
        assert seen[-1] == {"hide": "away"}
    finally:
        unwatch()
        POINTERS.place_with("env/e1-browser", None)
    assert "env/e1-browser" not in POINTERS.watchers


def test_telling_never_raises_and_a_failing_watcher_or_placer_is_not_the_agents_problem():
    agent_pointer.told(object(), 1, 2)  # a stand-in without a context
    agent_pointer.told_here(object(), "scroll")
    context = Context()
    page = Page(context)
    agent_pointer.name_lane(SimpleNamespace(context=context), "env/e2-browser")
    agent_pointer.saw_viewport(page, 800, 600)

    def boom(*_a):
        raise RuntimeError("no")

    POINTERS.place_with("env/e2-browser", boom)
    seen: list[dict] = []
    unwatch_bad = POINTERS.watch("env/e2-browser", boom)
    unwatch = POINTERS.watch("env/e2-browser", seen.append)
    try:
        agent_pointer.told(page, 5, 5)
        assert seen == [{"hide": "away"}]  # a placer that fails places nothing
    finally:
        unwatch()
        unwatch_bad()
        POINTERS.place_with("env/e2-browser", None)
