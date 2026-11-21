"""A mailbox is read beside the scheduler's tick and never in it: a slow or stuck server holds up
only itself, and no schedule, card, watch or other mailbox waits for it."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from workflow_support import MAIL, OWNER, make_node, workflow

from openajar.runner.workflows.mail import Received

OTHER = "biz-a/support"
TICK_S = 1.0  # far longer than a tick that waits for nothing


class Gate:
    """`node.mail.fetch` for the mailboxes in `stuck`: it does not come back until `release`."""

    def __init__(self, node, *stuck: str):
        self.stuck, self.real, self.held, self.open = set(stuck), node.mail.fetch, [], asyncio.Event()
        node.mail.fetch = self.fetch

    async def fetch(self, box, password, after_uid):
        if box.id in self.stuck:
            self.held.append(box.id)
            await self.open.wait()
        return await self.real(box, password, after_uid)


@pytest.fixture
def node(tmp_path):
    n = make_node(tmp_path)
    n.wf.mail.add({"id": OTHER, "address": "help@biz-a.example", "imap_host": "imap.invalid", "smtp_host": "smtp.invalid",
                   "username": "help", "secret_ref": "biz-a.support"}, person=True)
    n.h.clock.set(datetime(2026, 11, 4, 10, 0, tzinfo=UTC))
    return n


async def tick(node) -> None:
    """One tick of the scheduler, and a look at what it started. It has to come back on its own."""
    await asyncio.wait_for(node.runner.scheduler.tick(), TICK_S)
    for _ in range(5):
        await asyncio.sleep(0)


async def next_poll(node) -> None:
    node.h.clock.advance(61)
    await tick(node)


async def test_a_stuck_mailbox_does_not_hold_up_the_tick(node):
    gate = Gate(node, MAIL)
    await tick(node)
    assert gate.held == [MAIL]
    gate.open.set()


async def test_a_stuck_mailbox_does_not_stop_another_being_read(node):
    gate = Gate(node, MAIL)
    await tick(node)
    assert node.mail.seen_uid == [None]  # the other one, and only it
    await next_poll(node)
    assert node.mail.seen_uid == [None, 0]
    gate.open.set()


async def test_one_read_of_a_mailbox_at_a_time_however_long_it_takes(node):
    gate = Gate(node, MAIL)
    await tick(node)
    await next_poll(node)
    await next_poll(node)
    assert gate.held == [MAIL]
    gate.open.set()
    await tick(node)
    node.h.clock.advance(61)
    await tick(node)
    assert gate.held == [MAIL, MAIL]  # it finished, so the next interval reads it again
    gate.open.set()


async def test_a_stuck_mailbox_stops_no_schedule_card_or_watch(node, monkeypatch):
    Gate(node, MAIL, OTHER).open.clear()
    ran = []
    monkeypatch.setattr(node.wf.leads, "check_cards", lambda: ran.append("card"))
    monkeypatch.setattr(node.wf.site_changes, "tick", lambda: ran.append("watch"))
    wid = node.wf.save(workflow({"id": "n", "kind": "notify", "message": "It is time"},
                                trigger={"kind": "schedule", "cron": "* * * * *", "timezone": "UTC"}), OWNER)["id"]
    node.wf.enable(wid, OWNER)
    await tick(node)
    for _ in range(3):
        await next_poll(node)
    await asyncio.wait_for(node.settle(3), TICK_S * 5)
    assert ran.count("card") >= 4 and ran.count("watch") >= 4
    assert node.wf.runs(wid)


async def test_what_a_slow_mailbox_held_starts_its_run_when_it_comes_back(node):
    wid = node.wf.save(workflow({"id": "n", "kind": "notify", "message": "It came"}, trigger={"kind": "email", "lane": MAIL}), OWNER)["id"]
    node.wf.enable(wid, OWNER)
    await tick(node)  # where the mailbox is
    gate = Gate(node, MAIL)
    node.mail.inbox.append(Received(1, "a@example.com", "x", "y"))
    await next_poll(node)
    await node.settle(2)
    assert node.wf.runs(wid) == []  # nothing yet, and nothing was lost
    gate.open.set()
    await node.settle(3)
    assert len(node.wf.runs(wid)) == 1


async def test_a_read_that_fails_is_tried_again_at_the_next_interval(node):
    tried = []

    async def fetch(box, password, after_uid):
        tried.append(box.id)
        raise OSError("the server went away")

    node.mail.fetch = fetch
    await tick(node)
    await next_poll(node)
    assert sorted(tried) == sorted([MAIL, OTHER, MAIL, OTHER])
