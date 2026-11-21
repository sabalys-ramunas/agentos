"""workspaces.db: applying a plan waits for a lock another process holds off the event loop,
and its lane records still land before apply returns (openajar.core.sqlite)."""

from __future__ import annotations

import pytest
from lockheld import loop_stays_free
from testkit.workspaces import Harness

from openajar.workspaces.store import WorkspaceStore


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path / "home")
    yield harness
    harness.close()


async def test_apply_records_its_lanes_off_the_loop_while_the_lock_is_held(h):
    h.service.create_workspace("acme")
    path = h.service.store.path
    result = await loop_stays_free(
        lambda: h.service.apply("social-6", ["acme"], by="owner", proxy_per_workspace={"acme": "lt1"}), path)
    assert result.status == "complete" and result.created
    rows = WorkspaceStore(path).lane_rows()
    assert set(result.created) <= set(rows) and all(rows[lane]["readiness"] == "needs_login" for lane in result.created)
