"""The worker per lane, end to end on the dev node: the lane's Ask me first decides how a publish
goes, a person turns the worker off on one lane, and a person takes the lane from a run. The real
policy, approvals, commit gate, runner and claims, with only the models scripted."""

from __future__ import annotations

import dataclasses
import inspect
import re
from typing import Any

import pytest
from test_pilot_review_node import (
    INJECTED,
    finish,
    owner_config,
    unread_by_any_rule,
    write_caption,
)
from test_pilot_service import (  # noqa: F401 (close_the_nodes is an autouse fixture)
    CAPTION,
    FINISHED,
    LANE,
    click_share,
    close_the_nodes,
    node_with,
    type_caption,
)

from openajar.core.errors import OpenajarError
from openajar.core.models import ActionKind
from openajar.node import human, publish
from openajar.node import tools as node_tools
from openajar.pilot import errors as pilot_errors
from openajar.pilot import service as pilot_service
from openajar.pilot.deciders import BrainText, Text
from openajar.pilot.service import load_settings
from openajar.pilot.tools import lane_do
from openajar.plugins import admin
from openajar.plugins.service import LANE_WORKER, PluginService
from openajar.plugins.store import PluginStore
from openajar.runner import tools as runner_tools
from openajar.runner.fakes import BASE

WRITTEN = {"values": None, "goal": "Reply to the post"}


@pytest.fixture
def quick(monkeypatch: Any) -> None:
    """No real pause: the look at a fresh page takes no time."""
    async def dwell(page: Any, lo: int, hi: int) -> None:
        return None

    monkeypatch.setattr(human, "dwell", dwell)


def plugins_on(node: Any, tmp_path: Any, monkeypatch: Any, **jobs: str) -> PluginService:
    """The real switch in a real plugins.db, with the brains the test asks for."""
    home = tmp_path / "plugin-home"
    home.mkdir(exist_ok=True)
    service = PluginService(node.runtime, home=home)
    monkeypatch.setattr(service, "brain_for", lambda _, job: jobs.get(job))
    monkeypatch.delenv(admin.LANE_ENV, raising=False)
    monkeypatch.delenv(admin.BRAIN_TASK_ENV, raising=False)
    node.runtime.register("plugins", service)
    return service


def writer(node: Any, tmp_path: Any, monkeypatch: Any, words: str = INJECTED) -> PluginService:
    async def complete(self: Any, prompt: str, *, must_not_send: Any) -> Text:
        return Text(words)

    monkeypatch.setattr(BrainText, "complete", complete)
    return plugins_on(node, tmp_path, monkeypatch, write="claude-haiku")


def person_says(node: Any, *, ask_first: bool) -> None:
    node.policy.ask_first.set("lane", LANE, ask_first, by="owner", person=True)


def before_decision(node: Any, n: int, act: Any) -> None:
    """Run `act` right before the model's nth decision, while the run is going."""
    decide, calls = node.brains.decide, []

    async def wrapped(*args: Any, **kw: Any) -> dict[str, Any]:
        calls.append(1)
        if len(calls) == n:
            done = act()
            if inspect.isawaitable(done):
                await done
        return await decide(*args, **kw)

    node.brains.decide = wrapped


# ---- the matrix: Ask me first by who wrote the words ------------------------------------------------

@pytest.mark.parametrize(("choice", "written", "asks"), [
    (None, False, False),   # no choice, a caller's words: the node's policy
    (None, True, True),     # no choice, written words: a person has to choose first
    (True, False, True),    # Asks you first
    (True, True, True),
    (False, False, False),  # Runs on its own
    (False, True, False),
])
async def test_how_a_publish_goes(tmp_path, quick, monkeypatch, choice, written, asks):
    owner_config(tmp_path)  # commit out of `[taint] downgrade`, so only the lane's switch is left to ask
    script = (write_caption if written else type_caption, click_share, FINISHED)
    node = await node_with(tmp_path, *script)
    writer(node, tmp_path, monkeypatch)
    if choice is not None:
        person_says(node, ask_first=choice)
    try:
        run = await node.start(**(WRITTEN if written else {}))
        words = INJECTED if written else CAPTION
        if asks:
            assert run["status"] == "waiting_approval" and node.posts == []
            assert node.approvals.get(run["approval_id"]).card["text"]["value"] == words  # the exact content
            await finish(node, run)
        else:
            assert run["status"] != "waiting_approval"
        assert [p["caption"] for p in node.posts] == [words]
    finally:
        await node.lanes.shutdown()


@pytest.mark.parametrize("written", [False, True])
async def test_runs_on_its_own_does_not_lift_the_rule_about_a_page_it_read(tmp_path, quick, monkeypatch, written):
    """With `commit` in `[taint] downgrade`, the shipped default, a publish that follows a page asks,
    whatever the lane says. The worker reads a page before anything it does."""
    node = await node_with(tmp_path, write_caption if written else type_caption, click_share, FINISHED)
    writer(node, tmp_path, monkeypatch)
    person_says(node, ask_first=False)
    try:
        run = await node.start(**(WRITTEN if written else {}))
        assert run["status"] == "waiting_approval" and node.posts == []
        assert "untrusted" in node.approvals.get(run["approval_id"]).reason
    finally:
        await node.lanes.shutdown()


async def test_a_person_turning_ask_first_on_during_a_run_holds_its_next_publish(tmp_path, quick):
    owner_config(tmp_path)
    node = await node_with(tmp_path, type_caption, click_share, FINISHED)
    person_says(node, ask_first=False)
    before_decision(node, 2, lambda: person_says(node, ask_first=True))
    try:
        run = await node.start()
        assert run["status"] == "waiting_approval" and node.posts == []
    finally:
        await node.lanes.shutdown()


async def test_an_agents_off_is_no_choice_so_written_words_still_ask(tmp_path, quick, monkeypatch):
    owner_config(tmp_path)
    node = await node_with(tmp_path, write_caption, click_share, FINISHED)
    writer(node, tmp_path, monkeypatch)
    node.policy.ask_first.set("lane", LANE, False, by="agent:x", person=False)
    assert node.policy.ask_first.get("lane", LANE) is None
    try:
        assert (await node.start(**WRITTEN))["status"] == "waiting_approval" and node.posts == []
    finally:
        await node.lanes.shutdown()


async def test_a_row_only_a_person_can_write_is_the_only_row_that_counts(tmp_path, quick):
    node = await node_with(tmp_path, FINISHED)
    store = node.policy.ask_first
    assert not store.chosen_off("lane", LANE)
    store.db.execute("INSERT INTO ask_first (scope, id, ask_first, set_by, by_person, set_at) VALUES "
                     "('lane', ?, 0, 'agent:x', 0, 0)", (LANE,))
    assert not store.chosen_off("lane", LANE)
    person_says(node, ask_first=False)
    assert store.chosen_off("lane", LANE) and not store.is_on("lane", LANE)
    person_says(node, ask_first=True)
    assert not store.chosen_off("lane", LANE)


async def test_a_free_form_standing_approval_does_not_cover_a_workers_publish_on_a_lane_that_asks(tmp_path, quick):
    owner_config(tmp_path)
    node = await node_with(tmp_path, type_caption, click_share, FINISHED)
    person_says(node, ask_first=True)
    node.approvals.add_standing(effects=["commit"], lanes=[LANE], until=node.policy.clock() + 3600, by="owner",
                                count=10)
    try:
        assert (await node.start())["status"] == "waiting_approval" and node.posts == []
    finally:
        await node.lanes.shutdown()


async def test_a_request_no_rule_reads_asks_even_on_a_lane_that_runs_on_its_own(tmp_path, quick, monkeypatch):
    owner_config(tmp_path)
    monkeypatch.setitem(publish.RULE_VOLATILE, "social.unknown", (*publish.RULE_VOLATILE["social.post"],
                                                                  "variables.input.client_mutation_id"))
    node = await node_with(tmp_path, type_caption, click_share, FINISHED)
    person_says(node, ask_first=False)
    try:
        await unread_by_any_rule(node)
        run = await node.start()
        assert run["status"] == "waiting_approval" and node.posts == []
        assert "may spend money" in node.approvals.get(run["approval_id"]).reason
    finally:
        await node.lanes.shutdown()


def test_an_old_settings_file_keeps_its_limits(tmp_path) -> None:
    path = pilot_service.settings_path(tmp_path)
    path.parent.mkdir()
    path.write_text("max_steps = 7\nwritten_text_asks = false\n")
    settings = load_settings(tmp_path)
    assert settings.max_steps == 7 and not hasattr(settings, "written_text_asks")


# ---- the worker off on a lane -----------------------------------------------------------------------

async def test_lane_do_is_refused_before_anything_runs(tmp_path, quick, monkeypatch):
    node = await node_with(tmp_path, FINISHED)
    admin.set_lane(plugins_on(node, tmp_path, monkeypatch), LANE, False)
    try:
        await node_tools.browser_navigate(node.ctx(), on=LANE, url=f"{BASE}/compose")
        with pytest.raises(pilot_errors.PilotLaneOff) as refused:
            await lane_do(node.ctx(), on=LANE, goal="Post it", wait_s=0)
        words = refused.value.message
        assert refused.value.code == "pilot_lane_off" and "—" not in words
        assert "person turned" in words and "browser tools" in words and "take over" in words
        assert node.brains.calls == [] and node.runner.store.list_runs(lane=LANE, limit=5) == []
    finally:
        await node.lanes.shutdown()


async def test_another_lane_is_not_touched(tmp_path, quick, monkeypatch):
    node = await node_with(tmp_path, FINISHED)
    admin.set_lane(plugins_on(node, tmp_path, monkeypatch), "biz-a/other", False)
    try:
        assert (await node.start())["status"] != "failed" and node.brains.calls
    finally:
        await node.lanes.shutdown()


async def test_a_queued_run_ends_when_it_comes_up(tmp_path, quick, monkeypatch):
    node = await node_with(tmp_path, FINISHED)
    plugins = plugins_on(node, tmp_path, monkeypatch)
    try:
        await node_tools.browser_navigate(node.ctx(), on=LANE, url=f"{BASE}/compose")
        out = await lane_do(node.ctx(), on=LANE, goal="Post it", wait_s=0)
        admin.set_lane(plugins, LANE, False)
        claim = node.specs["browser_claim"]
        node.specs["browser_claim"] = dataclasses.replace(claim, handler=None)  # a run that got this far would crash
        await node.runner.run_until_idle()
        status = await runner_tools.run_status(node.ctx(), id=out["id"])
        assert status["status"] == "failed" and status["error"]["code"] == "pilot_lane_off"
        assert node.brains.calls == [] and out["id"] not in node.pilot.sessions
    finally:
        await node.lanes.shutdown()


async def test_off_during_a_run_stops_it_at_its_next_step_and_nothing_more_happens(tmp_path, quick, monkeypatch):
    owner_config(tmp_path)
    node = await node_with(tmp_path, type_caption, click_share, FINISHED)
    plugins = plugins_on(node, tmp_path, monkeypatch)
    before_decision(node, 2, lambda: admin.set_lane(plugins, LANE, False))
    try:
        run = await node.start()
        assert run["status"] == "partial" and run["error"]["code"] == "pilot_lane_off"  # the caption was typed
        assert run["output"]["pilot_status"] == "refused" and run["output"]["reason"] == "pilot_lane_off"
        assert "person turned" in run["error"]["message"] and node.posts == [] and run["output"]["effects"] == 1
        assert len(node.brains.calls) == 2 and run["id"] not in node.pilot.sessions
    finally:
        await node.lanes.shutdown()


async def test_off_before_its_first_action_says_failed(tmp_path, quick, monkeypatch):
    node = await node_with(tmp_path, type_caption, click_share, FINISHED)
    plugins = plugins_on(node, tmp_path, monkeypatch)
    before_decision(node, 1, lambda: admin.set_lane(plugins, LANE, False))
    try:
        run = await node.start()
        assert run["status"] == "failed" and run["error"]["code"] == "pilot_lane_off"
        assert run["output"]["effects"] == 0 and node.posts == []
    finally:
        await node.lanes.shutdown()


async def test_a_parked_run_sends_nothing_after_off_even_when_approved(tmp_path, quick, monkeypatch):
    node = await node_with(tmp_path, type_caption, click_share, FINISHED)
    plugins = plugins_on(node, tmp_path, monkeypatch)
    try:
        run = await node.start()
        assert run["status"] == "waiting_approval"
        admin.set_lane(plugins, LANE, False)
        done = await finish(node, run)
        assert done["status"] == "partial" and done["error"]["code"] == "pilot_lane_off" and node.posts == []
    finally:
        await node.lanes.shutdown()


async def test_state_that_cannot_be_read_stops_a_run_and_says_why(tmp_path, quick, monkeypatch):
    node = await node_with(tmp_path, type_caption, click_share, FINISHED)
    plugins = plugins_on(node, tmp_path, monkeypatch)
    before_decision(node, 1, lambda: plugins.store.path.write_bytes(b"not a database, not at all, not even close"))
    try:
        run = await node.start()
        assert run["status"] == "failed" and run["error"]["code"] == "pilot_lane_off"
        assert "could not be read" in run["error"]["message"] and "person turned" not in run["error"]["message"]
    finally:
        await node.lanes.shutdown()


async def test_off_leaves_direct_control_alone(tmp_path, quick, monkeypatch):
    """An agent drives the lane with the browser tools, and a person takes it, as if nothing changed."""
    owner_config(tmp_path)
    node = await node_with(tmp_path, FINISHED)
    admin.set_lane(plugins_on(node, tmp_path, monkeypatch), LANE, False)
    try:
        await node_tools.browser_navigate(node.ctx(), on=LANE, url=f"{BASE}/compose")
        snapshot = await node_tools.browser_snapshot(node.ctx(), on=LANE)
        box = re.search(r'textbox "[^"]*[Cc]aption[^"]*".*?\[ref=(\w+)\]', snapshot).group(1)
        share = re.search(r'button "Share".*?\[ref=(\w+)\]', snapshot).group(1)
        await node_tools.browser_act(node.ctx(), on=LANE, ref=box, action=ActionKind.FILL, text=CAPTION)
        await node_tools.browser_act(node.ctx(), on=LANE, ref=share, action=ActionKind.CLICK)
        assert [p["caption"] for p in node.posts] == [CAPTION]

        await node.lanes.take_control(LANE, by="owner")
        with pytest.raises(OpenajarError) as stopped:
            await node_tools.browser_snapshot(node.ctx(), on=LANE)
        assert stopped.value.code == "human_in_control"
        await node.lanes.hand_back(LANE, by="owner")
        assert await node_tools.browser_snapshot(node.ctx(), on=LANE)
    finally:
        await node.lanes.shutdown()


# ---- a person takes the lane from a run -------------------------------------------------------------

async def test_a_take_over_stops_the_run_and_the_run_says_so(tmp_path, quick):
    owner_config(tmp_path)
    node = await node_with(tmp_path, type_caption, click_share, FINISHED)
    before_decision(node, 2, lambda: node.lanes.take_control(LANE, by="owner"))
    try:
        run = await node.start()
        assert run["status"] == "partial" and run["error"]["code"] == "pilot_held"  # the caption was typed
        assert run["output"]["pilot_status"] == "held" and run["output"]["reason"] in ("human_in_control", "claim_lost")
        assert node.posts == [] and len(node.brains.calls) == 2 and run["id"] not in node.pilot.sessions
    finally:
        await node.lanes.shutdown()


async def test_a_take_over_right_after_a_click_landed_still_counts_it(tmp_path, quick):
    """What matters most: the post went, the person took the lane, and `failed` would invite a retry."""
    owner_config(tmp_path)
    node = await node_with(tmp_path, type_caption, click_share, FINISHED)
    settle = node.specs["browser_wait"]
    taken: list[int] = []

    async def take_after_the_post(*args: Any, **kw: Any) -> Any:
        if node.posts and not taken:
            taken.append(1)
            await node.lanes.take_control(LANE, by="owner")
        return await settle.handler(*args, **kw)

    node.specs["browser_wait"] = dataclasses.replace(settle, handler=take_after_the_post)
    try:
        run = await node.start()
        assert [p["caption"] for p in node.posts] == [CAPTION] and taken
        assert run["status"] == "partial" and run["error"]["code"] == "pilot_held"
        assert run["output"]["effects"] == 2 and any(a["operation"] == "CLICK" for a in run["output"]["actions"])
    finally:
        await node.lanes.shutdown()


async def test_a_queued_run_waits_for_the_person_and_goes_after_the_hand_back(tmp_path, quick):
    owner_config(tmp_path)
    node = await node_with(tmp_path, FINISHED)
    node.runner.listen_for_lane_control()  # as the daemon's start does
    try:
        await node_tools.browser_navigate(node.ctx(), on=LANE, url=f"{BASE}/compose")
        out = await lane_do(node.ctx(), on=LANE, goal="Post it", wait_s=0)
        await node.lanes.take_control(LANE, by="owner")
        await node.runner.run_until_idle()
        status = await runner_tools.run_status(node.ctx(), id=out["id"])
        assert status["status"] == "queued" and node.brains.calls == []
        assert status["error"]["code"] == "deferred" and status["error"]["control"] == "human"
        await node.lanes.hand_back(LANE, by="owner")
        await node.runner.run_until_idle()
        assert node.brains.calls
    finally:
        await node.lanes.shutdown()


async def test_a_take_over_just_before_the_claim_says_held(tmp_path, quick):
    """The runner saw a free lane, and a person took it before the run claimed it."""
    node = await node_with(tmp_path, FINISHED)
    claim = node.specs["browser_claim"]

    async def taken_first(*args: Any, **kw: Any) -> Any:
        await node.lanes.take_control(LANE, by="owner")
        return await claim.handler(*args, **kw)

    node.specs["browser_claim"] = dataclasses.replace(claim, handler=taken_first)
    try:
        run = await node.start()
        assert run["status"] == "failed" and run["error"]["code"] == "pilot_held"
        assert "human" in run["error"]["message"] and node.brains.calls == [] and run["id"] not in node.pilot.sessions
    finally:
        await node.lanes.shutdown()


async def test_a_take_over_while_a_publish_waits_sends_nothing_until_it_is_handed_back(tmp_path, quick):
    node = await node_with(tmp_path, type_caption, click_share, FINISHED)
    node.runner.listen_for_lane_control()
    try:
        run = await node.start()
        assert run["status"] == "waiting_approval"
        await node.lanes.take_control(LANE, by="owner")
        waiting = await finish(node, run)
        assert waiting["status"] == "queued" and node.posts == []
        await node.lanes.hand_back(LANE, by="owner")
        await node.runner.run_until_idle()
        assert [p["caption"] for p in node.posts] == [CAPTION]  # a person approved it, and the lane is back
    finally:
        await node.lanes.shutdown()


def test_a_lane_nobody_touched_is_on(tmp_path) -> None:
    assert PluginStore(tmp_path).read().off_on(LANE_WORKER) == frozenset()
