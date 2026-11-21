"""brains.db: adding a brain and taking a task wait for a lock another process holds off the
event loop, and are saved before the call returns (openajar.core.sqlite)."""

from __future__ import annotations

from lockheld import loop_stays_free
from test_brains_runner import P, _kill_leftovers
from testkit.brains import FakeNotifier, add, make_service, wait_status

from openajar.brains.store import Store
from openajar.core.runtime import Runtime


async def test_a_brain_and_a_task_are_saved_off_the_loop_while_the_lock_is_held(tmp_path):
    svc = make_service(tmp_path, runtime=Runtime(notifier=FakeNotifier()))
    try:
        path = svc.store.path

        async def work():
            await add(svc, "claude-max", "subscription", "anthropic")
            return await svc.submit(P, prompt="List the files. FAKE:ok", workspace="personal")

        r = await loop_stays_free(work, path)
        other = Store(path)
        assert other.get_brain("claude-max") is not None and other.get_task(r["id"]) is not None
        st = await wait_status(svc, r["id"])
        assert st["status"] == "succeeded_unverified"
    finally:
        _kill_leftovers(svc)
