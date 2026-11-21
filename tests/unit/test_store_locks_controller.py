"""controller.db: a node's heartbeat touch never waits for a lock another process holds, and a
mesh freeze waits for it off the event loop and is saved before the call returns
(openajar.core.sqlite)."""

from __future__ import annotations

from lockheld import loop_stays_free

from openajar.controller.store import ControllerStore


async def test_a_mesh_freeze_and_a_heartbeat_touch_leave_the_loop_free(tmp_path):
    store = ControllerStore(tmp_path / "controller.db")
    org = store.default_org()["id"]

    async def freeze():
        store.soon(store.seen, "nd_missing", {"load": 0.1, "lanes": 1, "degraded": False})
        state = await store.run(store.set_mesh_freeze, org, freeze_id="fz_test", reason="test", by="owner",
                                revoked=[])
        await store.flush()
        return state

    state = await loop_stays_free(freeze, store.path)
    assert state["freeze_id"] == "fz_test"
    assert ControllerStore(tmp_path / "controller.db").mesh_freeze(org)["freeze_id"] == "fz_test"
