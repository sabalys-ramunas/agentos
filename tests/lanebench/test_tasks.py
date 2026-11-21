"""Every task file loads, names only paths on the fixture site, and is scored by checks and
replay steps that exist. The suite covers what the design asks for."""

from __future__ import annotations

import pytest

from lanebench import adversarial, executors, mutations
from lanebench.executors.mcp_agent import BenchContext, parse_result
from lanebench.harness import Slot, assert_checker_unlisted
from lanebench.tasks import Task, load_all

TASKS = load_all()
STEPS = {"navigate", "click", "click_if", "type", "fill", "press", "upload", "wait_for", "wait_gone", "scroll_until",
         "expect", "read", "dwell", "hold", "patch"}


def test_there_are_enough_tasks_and_ids_are_unique():
    assert len(TASKS) >= 20
    assert len({t.id for t in TASKS}) == len(TASKS)


@pytest.mark.parametrize("task", TASKS, ids=[t.id for t in TASKS])
def test_each_task_is_local_and_replayable(task):
    assert task.start.startswith("/") and not task.start.startswith("//")
    assert task.replay, "every task has a replay"
    for step in task.replay:
        assert step["do"] in STEPS, step
        if step["do"] == "navigate":
            assert step["url"].startswith("/")
    text = task.model_dump_json()
    for host in ("http://", "https://"):
        assert host not in text.replace("http://prize.socia1-lanebench.test", ""), "a task names a host"
    if task.mode == "repair":
        assert task.repair and task.repair.parity


def test_the_suite_covers_every_mutation_fixture_and_login_variant():
    used = {m["kind"] for t in TASKS for m in t.mutation}
    assert used == set(mutations.MUTATIONS)
    fixtures = {a for t in TASKS for a in t.adversarial}
    assert fixtures >= set(adversarial.FIXTURES) - {"promo_account", "lab_traps"}  # the lab's own, not a task's
    assert {t.after_login for t in TASKS} >= {"save_login", "consent"}
    assert any(fx.plan.get("after_login") == "device" for t in TASKS for fx in adversarial.fixtures(t.adversarial))
    assert any(t.cookie_wall for t in TASKS)
    assert {t.mode for t in TASKS} >= {"goal", "repair", "login_assist"}


def test_a_task_cannot_name_another_host():
    base = {"id": "x", "title": "x", "goal": "x", "check": [{"kind": "signed_in"}]}
    with pytest.raises(ValueError, match="paths on the fixture site"):
        Task.model_validate({**base, "start": "https://www.instagram.com/"})
    with pytest.raises(ValueError, match="paths on the fixture site"):
        Task.model_validate({**base, "replay": [{"do": "navigate", "url": "//evil.example/"}]})
    with pytest.raises(ValueError, match="unknown check"):
        Task.model_validate({**base, "check": [{"kind": "vibes"}]})


def test_the_checker_host_is_never_on_a_lane_site_list():
    assert_checker_unlisted(["127.0.0.1"], "127.0.0.2")
    assert_checker_unlisted(["127.0.0.1"], "localhost")
    with pytest.raises(RuntimeError):
        assert_checker_unlisted(["127.0.0.1"], "127.0.0.1")


def test_slots_follow_node_test_ranges_and_refuse_production_ports():
    s = Slot.of(50)
    assert (s.display_base, s.vnc_base, s.viewer_base) == (500, 5500, 6500)
    assert (s.mcp_port, s.site_port, s.checker_port) == (6507, 6508, 6509)
    with pytest.raises(ValueError):
        Slot.of(10)
    with pytest.raises(ValueError):
        Slot.of(60)


def test_executors_by_name():
    assert executors.get("replay").name == "replay"
    assert executors.get("mcp-agent").name == "mcp-agent"
    assert executors.get("planted:center_click").name == "planted:center_click"
    assert executors.get("pilot").name == "pilot"
    with pytest.raises(ValueError):
        executors.get("nope")


async def test_the_mcp_agent_grant_stand_in_keeps_to_the_lane_and_the_ceiling():
    from openajar.core.errors import PolicyDenied
    from openajar.core.models import Effect

    ctx = BenchContext(runtime=None, tool="browser_act", lane="lanebench/feed-like", ceiling="draft",
                       sites=["127.0.0.1"])
    await ctx.authorize(Effect.DRAFT, lane_id="lanebench/feed-like")
    for effect, lane in ((Effect.COMMIT, None), (Effect.SECRET, None), (Effect.READ, "other/lane")):
        with pytest.raises(PolicyDenied):
            await ctx.authorize(effect, lane_id=lane)
    from openajar.node.egress import Driver

    driver = Driver.from_context(ctx)
    assert driver.kind == "agent" and driver.ceiling == "draft" and driver.lists == (("127.0.0.1",),)


def test_the_agent_result_line():
    assert parse_result('thinking\n{"status": "held", "reason": "device"}\n', 0).status == "held"
    got = parse_result('{"status": "done", "answer": {"latest_comment": "hi"}}', 0)
    assert got.answer == {"latest_comment": "hi"}
    assert parse_result("no json here", 0).status == "done"
    assert parse_result("", 3).status == "failed"
