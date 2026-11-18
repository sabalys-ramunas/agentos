"""The bring-up's steps and seconds (agentos.environments.timings) and the owner's view of the node's own
machines (agentos.environments.fleet): prepared machines, the warm pool's price and worst month, and
this month's opens. The machines are test_environments_prepared's fakes (FakeProvider, a fake shell)."""

from __future__ import annotations

import asyncio
import math
from datetime import timedelta

import pytest
from test_environments_prepared import REPO, _warm, builds_done, node  # noqa: F401

from agentos.core.models import utcnow
from agentos.environments import bringup, timings
from agentos.environments.errors import EnvironmentFailed
from agentos.environments.fleet import MONTH_HOURS, worst_eur_month
from agentos.environments.models import Environment, EnvSettings

STEP_KEYS = {"step", "at_s", "seconds", "ok"}


def _by(view: dict) -> dict[str, dict]:
    return {s["step"]: s for s in view["steps"]}


# ------------------------------------------------------------------ the bring-up's steps


async def test_a_cold_open_keeps_each_step_and_its_seconds(node):  # noqa: F811
    out = await node.call("environment_open", title="Shop", repo=REPO)
    assert out["state"] == "ready", out
    env = node.mgr.get(out["id"])
    kept = env.bringup
    assert kept["ok"] is True and kept["total_s"] >= 0
    names = [s["step"] for s in kept["steps"]]
    for name in ("VM create", "VM ready", "setup check", "tunnels and git", "network", "browser", "clone", "setup"):
        assert name in names, names
    assert all(set(s) == STEP_KEYS and s["ok"] is True for s in kept["steps"])
    view = (await node.mgr.detail(env))["bringup"]
    assert view["ok"] is True and view["running"] == [] and "restore_from" not in view
    assert [s["at_s"] for s in view["steps"]] == sorted(s["at_s"] for s in view["steps"])
    assert not any("skipped" in s for s in view["steps"])  # nothing was skipped: no warm or prepared machine
    assert env.id not in timings.LIVE  # the bring-up ended: nothing left in the registry


async def test_a_prepared_open_says_setup_was_skipped_and_what_it_saved(node):  # noqa: F811
    first = await node.call("environment_open", title="Shop", repo=REPO)
    await builds_done(node.mgr)
    await node.call("environment_close", on=first["on"], discard=True)
    out = await node.call("environment_open", title="Shop again", repo=REPO, session="chat-2")
    env = node.mgr.get(out["id"])
    by = _by((await node.mgr.detail(env))["bringup"])
    assert "checkout" in by and "refresh" in by
    setup = by["setup"]
    assert setup["skipped"] == "prepared machine" and setup["seconds"] == 0.0 and setup["ok"] is True
    assert setup["saved_s"] == out["prepared"]["setup_s"]["saved"]
    assert setup["at_s"] >= by["refresh"]["at_s"]  # where setup would have run
    assert "setup" not in {s["step"] for s in env.bringup["steps"]}  # only the view adds it


async def test_a_warm_open_marks_the_rent_and_boot_skipped(node):  # noqa: F811
    node.reads["text"] = None
    await _warm(node)
    out = await node.call("environment_open", title="Shop", repo=REPO)
    env = node.mgr.get(out["id"])
    assert env.warm_claimed
    by = _by((await node.mgr.detail(env))["bringup"])
    for name in ("VM create", "VM ready"):
        assert by[name]["skipped"] == "warm machine" and by[name]["seconds"] >= 0  # measured: the claim ran
    assert "skipped" not in by["clone"]


async def test_a_failed_bring_up_keeps_its_steps_with_the_one_that_failed(node, monkeypatch):  # noqa: F811
    async def broken(env):
        raise EnvironmentFailed("the machine's setup failed: no disk", reason="setup")

    monkeypatch.setattr(node.mgr, "_wait_setup", broken)
    with pytest.raises(Exception, match="no disk"):
        await node.call("environment_open", title="Shop", repo=REPO)
    [env] = node.mgr.store.recent(1)
    assert env.state == "failed"
    assert env.bringup["ok"] is False
    by = _by(env.bringup)
    assert by["VM create"]["ok"] is True and by["setup check"]["ok"] is False
    assert "clone" not in by  # it never began


async def test_while_it_starts_the_detail_shows_the_steps_so_far_and_the_running_ones(node):  # noqa: F811
    now = utcnow()
    env = Environment(id="estarting01", title="Slow", grant_id="gr_agent", binding="session:x", state="starting",
                      created_at=now, expires_at=now + timedelta(hours=1))
    node.mgr.store.put(env)
    steps = timings.LIVE[env.id] = bringup.Steps(env.id)
    gate = asyncio.Event()

    async def quick():
        return "done"

    async def slow():
        await gate.wait()

    try:
        await steps.run("VM create", quick())
        task = asyncio.create_task(steps.run("VM ready", slow()))
        await asyncio.sleep(0.01)
        view = timings.view(node.mgr, env)
        assert view["ok"] is None and view["total_s"] >= 0
        assert [s["step"] for s in view["steps"]] == ["VM create"]
        [running] = view["running"]
        assert running["step"] == "VM ready" and running["so_far_s"] >= 0 and set(running) == {"step", "at_s", "so_far_s"}
        gate.set()
        await task
    finally:
        timings.ended(env.id, steps)
    assert timings.view(node.mgr, env) is None  # an environment with no record (and none running here)


def test_a_resume_says_what_it_woke_from_and_a_failed_step_says_so() -> None:
    now = utcnow()
    kept = {"total_s": 40.0, "ok": True, "steps": [{"step": "VM create", "at_s": 0.0, "seconds": 20.0, "ok": True},
                                                   {"step": "restore", "at_s": 30.0, "seconds": 9.5, "ok": True}]}
    env = Environment(id="eresumed001", title="Back", grant_id="gr_agent", binding="session:x", state="ready",
                      created_at=now, expires_at=now + timedelta(hours=1), bringup=kept,
                      resumed={"save_id": "s1", "from": "paused"})
    view = timings.view(None, env)
    assert view["restore_from"] == "paused" and view["total_s"] == 40.0 and view["running"] == []
    assert env.bringup == kept  # the view never changes the record


async def test_a_step_that_raises_is_not_ok() -> None:
    steps = bringup.Steps("eraise00001")

    async def boom():
        raise RuntimeError("no")

    with pytest.raises(RuntimeError):
        await steps.run("clone", boom())
    [done] = steps.done
    assert done["step"] == "clone" and done["ok"] is False and done["seconds"] >= 0


# ------------------------------------------------------------------ the owner's prepared machines and pool


def test_the_worst_month_is_every_slot_always_warm_at_its_started_hours() -> None:
    assert worst_eur_month(1, 50, 0.01) == round(MONTH_HOURS * 60 / 50 * 1 * 0.01, 2)  # one hour per 50 min life
    assert worst_eur_month(2, 90, 0.01) == round(2 * MONTH_HOURS * 60 / 90 * math.ceil(90 / 60) * 0.01, 2)
    assert worst_eur_month(0, 50, 0.01) == 0.0  # off
    assert worst_eur_month(3, 50, 0.0) == 0.0  # docker
    assert worst_eur_month(1, 50, None) is None  # the price is unknown


async def test_the_overview_lists_machines_the_pool_with_its_price_and_the_months_opens(node):  # noqa: F811
    first = await node.call("environment_open", title="Shop", repo=REPO)
    await builds_done(node.mgr)
    await node.call("environment_close", on=first["on"], discard=True)
    second = await node.call("environment_open", title="Shop again", repo=REPO, session="chat-2")
    await node.call("environment_close", on=second["on"], discard=True)
    node.mgr.settings = EnvSettings(warm=1)
    await _warm(node)
    out = await node.mgr.prepared_overview()
    assert set(out) == {"machines", "eur_month", "eur_per_gb_month", "pool", "month"}
    [machine] = out["machines"]
    assert machine["repo"] == REPO and machine["state"] == "ready" and machine["uses"] == 1
    assert out["eur_per_gb_month"] == node.mgr.settings.snapshot_eur_per_gb_month
    pool = out["pool"]
    [warm] = pool["machines"]
    assert pool["warm"] == 1 and pool["target"] == 1 and pool["price_hourly_eur"] == warm["price_hourly_eur"]
    assert pool["worst_eur_month"] == worst_eur_month(1, pool["minutes"], warm["price_hourly_eur"])
    assert pool["month_eur"] >= 0
    month = out["month"]
    assert month["opens"] == 2 and month["from_prepared"] == 1 and month["from_warm"] == 0
    assert month["setup_saved_s"] == second["prepared"]["setup_s"]["saved"]
    assert [r["from"] for r in month["recent"]] == ["cold", "prepared"]  # oldest first
    assert all(r["seconds"] >= 0 and r["at"] for r in month["recent"])


async def test_with_no_warm_machine_the_pools_price_is_what_it_would_rent(node):  # noqa: F811
    out = await node.mgr.prepared_overview()
    pool = out["pool"]
    assert pool["warm"] == 0 and pool["target"] == 0 and pool["worst_eur_month"] == 0.0
    plan = await node.vms.plan("env/warm-x", ttl_minutes=50)
    assert pool["price_hourly_eur"] == plan["price_hourly_eur"]
    assert out["month"] == {"opens": 0, "from_prepared": 0, "from_warm": 0, "setup_saved_s": 0.0, "recent": []}
