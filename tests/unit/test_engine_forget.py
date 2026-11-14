"""A closed lane lets go of its browser context: the registries keyed weakly by context or page
hold values that point back at the key, so without forget_context() every closed lane kept its
whole Playwright connection alive (found by the soak run, docs/BENCH.md)."""

from __future__ import annotations

import gc
import weakref

from agentos.node import human, interaction, typist
from agentos.node.camoufox.engine import PageTracker, forget_context, tracker_for


class FakeContext:
    def __init__(self) -> None:
        self.pages: list = []
        self.handlers: list = []

    def on(self, event, handler) -> None:
        self.handlers.append((event, handler))


class FakeRequest:
    def __init__(self, page: FakePage) -> None:
        self.page = page


class FakePage:
    def __init__(self, context: FakeContext) -> None:
        self.context = context


def test_a_closed_context_is_freed_once_forgotten():
    ctx = FakeContext()
    page = FakePage(ctx)
    PageTracker(ctx)  # registers itself, a dialog guard and a devtools capture for the context
    state = interaction._PageState()
    state.inflight[FakeRequest(page)] = 0.0  # a real request reaches its page through its frame
    interaction._PAGES[page] = state
    network = typist._Network()
    network.inflight[FakeRequest(page)] = 0.0
    typist._NETWORKS[page] = network
    other = FakeContext()
    PageTracker(other)

    alive = weakref.ref(ctx)
    del page, state, network
    gc.collect()
    assert alive() is not None and tracker_for(ctx) is not None  # what kept every closed lane

    assert forget_context(ctx) == 5  # its tracker, dialog guard and devtools capture, the page's state and network
    del ctx
    gc.collect()
    assert alive() is None
    assert tracker_for(other) is not None  # another lane's entries stay
    forget_context(other)


def test_forgetting_drops_where_the_lane_was_reading():
    ctx, _page = FakeContext(), None
    human._GROUND[ctx] = "covered"
    forget_context(ctx)
    assert ctx not in human._GROUND
