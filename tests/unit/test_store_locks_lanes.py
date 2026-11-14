"""A CLI write holding the lane or env store's write lock never freezes the daemon: control,
quarantine, exit pins and env states wait for it off the event loop, and still land before
the call returns (agentos.core.sqlite)."""

from __future__ import annotations

from lockheld import loop_stays_free
from test_lanes_holds import build

from agentos.core.models import ControlState, LaneSpec
from agentos.node.store import LaneStore


async def test_a_hold_waits_for_the_lock_off_the_loop_and_is_saved(tmp_path):
    t = build(tmp_path)
    await t.mgr.create(LaneSpec(id="a/one"))
    await loop_stays_free(lambda: t.mgr.hold("a/one", reason="needs_human:captcha"), t.config.db_path)
    rec = LaneStore(t.config.db_path).require("a/one")  # another process sees it at once
    assert rec.control.state == ControlState.PAUSED and rec.control.reason == "needs_human:captcha"


async def test_a_quarantine_and_its_clearing_wait_off_the_loop_and_are_saved(tmp_path):
    t = build(tmp_path)
    await t.mgr.create(LaneSpec(id="a/one"))
    await loop_stays_free(lambda: t.mgr.quarantine("a/one", "manual check"), t.config.db_path)
    rec = LaneStore(t.config.db_path).require("a/one")
    assert rec.quarantine is not None and rec.quarantine.reason == "manual check"
    assert rec.control.state == ControlState.PAUSED
    assert "quarantined" in [e.kind for e in LaneStore(t.config.db_path).events("a/one")]
    await loop_stays_free(lambda: t.mgr.clear_quarantine("a/one", by="owner"), t.config.db_path)
    assert LaneStore(t.config.db_path).require("a/one").quarantine is None


async def test_an_accepted_exit_pin_waits_off_the_loop_and_is_saved(tmp_path):
    t = build(tmp_path)
    await t.mgr.create(LaneSpec(id="a/one", proxy_label="gw"))
    await t.mgr.quarantine("a/one", "exit moved", kind="exit_ip_changed", proxy_label="gw", exit_ip="203.0.113.7")
    await loop_stays_free(lambda: t.mgr.clear_quarantine("a/one", by="owner"), t.config.db_path)
    pin = LaneStore(t.config.db_path).proxy_exit("gw")
    assert pin is not None and pin.exit_ip == "203.0.113.7" and pin.pinned_by.startswith("accepted by")


async def test_a_proxy_change_and_a_site_change_at_once_both_stick(tmp_path):
    # Both stay plain writes: no await between reading the spec and writing it back.
    import asyncio

    t = build(tmp_path)
    await t.mgr.create(LaneSpec(id="a/one"))
    await asyncio.gather(t.mgr.set_sites("a/one", ["example.test"], strict=True), t.mgr.set_proxy("a/one", "lt1"))
    rec = LaneStore(t.config.db_path).require("a/one")
    assert rec.spec.proxy_label == "lt1" and rec.spec.sites == ["example.test"] and rec.spec.egress_strict


async def test_env_warm_exec_and_reset_wait_off_the_loop_and_are_saved(tmp_path):
    from test_env_manager import make

    from agentos.env.store import EnvStore

    mgr = make(tmp_path)
    try:
        mgr.create("ws/sb")
        db = mgr.paths.db
        await loop_stays_free(lambda: mgr.warm("ws/sb"), db)
        other = EnvStore(db)
        assert other.get_state("ws/sb")[0] == "warm" and other.last_used("ws/sb") is not None
        before = other.last_used("ws/sb")
        await loop_stays_free(lambda: mgr.exec("ws/sb", "true"), db)
        assert other.last_used("ws/sb") >= before
        await mgr.cool("ws/sb")
        mgr.quarantine("ws/sb", "disk damaged")
        await loop_stays_free(lambda: mgr.reset("ws/sb"), db)
        assert other.get_state("ws/sb") == ("cold", None)
    finally:
        await mgr.shutdown()


async def test_a_clear_while_the_quarantine_waits_for_the_lock_is_not_lost(tmp_path):
    import asyncio

    t = build(tmp_path)
    await t.mgr.create(LaneSpec(id="a/one"))

    async def both():
        quarantining = asyncio.create_task(t.mgr.quarantine("a/one", "manual check"))
        await asyncio.sleep(0.05)  # its write waits for the lock on the worker
        await t.mgr.clear_quarantine("a/one", by="owner")
        await quarantining

    await loop_stays_free(both, t.config.db_path)
    assert LaneStore(t.config.db_path).require("a/one").quarantine is None
