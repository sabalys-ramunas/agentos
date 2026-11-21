"""The agent's pointer on the panel's event stream: sent while the panel shows the lane's live view, never kept."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from openajar.api.events import EventBus
from openajar.api.realtime import Handlers
from openajar.node.agent_pointer import POINTERS


class Runtime:
    def __init__(self, bus):
        self.services = {"events": bus}

    def get(self, name):
        return self.services.get(name)

    def register(self, name, svc):
        self.services[name] = svc


def test_a_flash_reaches_open_streams_only_and_is_never_replayed():
    bus = EventBus()
    seen_by: list[object] = []
    bus.listen(seen_by.append)

    async def run():
        bus.publish("lane", {"id": "a/b"})
        async with bus.subscribe() as sub:
            bus.flash("pointer", {"lane": "a/b", "x": 1, "y": 2, "w": 10, "h": 10, "kind": "move"})
            got = await asyncio.wait_for(sub.get(), 1)
        return got

    got = asyncio.run(run())
    assert got.kind == "pointer" and got.id == 0 and "id:" not in got.sse()
    assert bus.last_id == 1 and [e.kind for e in bus.since(0)] == ["lane"]  # not in the replay, no id used
    assert [e.kind for e in seen_by] == ["lane"]  # listeners (webhooks) never hear it


def test_the_pointer_goes_out_while_a_live_view_is_open():
    bus = EventBus()
    handlers = Handlers(SimpleNamespace(bus=bus, runtime=Runtime(bus)))
    lane = "env/p1-browser"
    POINTERS.place_with(lane, lambda x, y, vw, vh, fresh: (int(x), int(y), 1920, 1080))
    sent: list[tuple[str, dict]] = []
    bus.flash = lambda kind, payload: sent.append((kind, payload))  # type: ignore[method-assign]
    try:
        POINTERS.moved(lane, 5, 6, 100, 100, "move")
        assert sent == []  # nobody watches here: nothing is sent
        stop_one, stop_two = handlers.feed_pointer(lane), handlers.feed_pointer(lane)
        POINTERS.moved(lane, 5, 6, 100, 100, "press")
        POINTERS.hide(lane, "terminal")
        assert sent == [("pointer", {"lane": lane, "x": 5, "y": 6, "w": 1920, "h": 1080, "kind": "press"}),
                        ("pointer", {"lane": lane, "hide": "terminal"})]  # once, with two views open
        stop_one()
        stop_one()  # twice is once
        POINTERS.moved(lane, 7, 8, 100, 100, "move")
        assert len(sent) == 3
        stop_two()
        POINTERS.moved(lane, 9, 9, 100, 100, "move")
        assert len(sent) == 3 and lane not in POINTERS.watchers and handlers.pointer_feeds == {}
    finally:
        POINTERS.place_with(lane, None)
