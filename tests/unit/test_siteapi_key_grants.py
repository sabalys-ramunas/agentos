"""A key and its grants over time: the end of a grant, a renewal, and a freeze of the lane behind a surface."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from siteapi_http_support import API, COMMIT_PATH, OWNER, READ_PATH, SURFACE, Node, definition, poll

from agentos.core.models import LaneSpec, RunStatus
from agentos.policy.taint import family_key
from agentos.runner.siteapis.models import web_surface

DAY = 86400
LOGIN = "harbor/login"
NOTE = {"json": {"note": "hello"}, "headers": {"Idempotency-Key": "one"}}


@pytest.fixture
async def node(tmp_path):
    async with Node(tmp_path) as n:
        yield n


@contextmanager
def near_the_end(node: Node) -> Iterator[None]:
    """The key sees its grant inside the renewal window. The policy's own time stands still, so the
    grant and what its family read are as they were."""
    real = node.keys.clock
    node.keys.clock = lambda: real() + 340 * DAY
    try:
        yield
    finally:
        node.keys.clock = real


@contextmanager
def past_the_end(node: Node) -> Iterator[None]:
    """Keys and the policy both a little over a year on: a grant of the longest life has run out."""
    keys, policy = node.keys.clock, node.policy.clock
    node.keys.clock = lambda: keys() + 400 * DAY
    node.policy.clock = lambda: policy() + 400 * DAY
    try:
        yield
    finally:
        node.keys.clock, node.policy.clock = keys, policy


def state(node: Node, grant_id: str) -> str:
    return node.policy.grant_record(grant_id)["state"]


def grants(node: Node) -> int:
    return len(node.policy.list_grants(include_inactive=True))


async def login_lane(node: Node) -> None:
    """Point SURFACE at a lane with another id, as a surface that acts through someone's login does."""
    await node.lanes.create(LaneSpec(id=LOGIN))
    node.runner.surfaces.put(web_surface(SURFACE, LOGIN), by=OWNER)


# ---- a key with an end ----

async def test_a_key_with_an_end_gets_a_grant_that_outlives_it(node):
    row, _ = node.key_row(surfaces=[SURFACE], expires_at=node.keys.clock() + 5 * DAY + 0.7)
    assert node.policy.grant_by_id(row.grant_id).expires_at >= row.expires_at


async def test_calls_near_a_keys_end_issue_no_grant(node):
    row, key = node.key_row(surfaces=[SURFACE], expires_at=node.keys.clock() + 5 * DAY + 0.7)
    before = grants(node)
    async with node.client(key) as c:
        for _ in range(3):
            assert (await c.get(READ_PATH)).status_code == 200
    assert node.keys.get(row.id).grant_id == row.grant_id and grants(node) == before
    assert state(node, row.grant_id) == "active"


async def test_near_its_end_a_key_that_read_page_text_still_asks_before_a_commit(node):
    _, key = node.key_row(ceiling="commit", surfaces=[SURFACE], expires_at=node.keys.clock() + 5 * DAY + 0.7)
    async with node.client(key) as c:
        assert (await c.get(READ_PATH)).status_code == 200
        r = await c.post(COMMIT_PATH, **NOTE)
    assert r.status_code == 202 and r.json()["code"] == "approval_pending" and node.site.state["notes"] == []


async def test_polling_a_run_near_a_keys_end_does_not_end_the_run(node):
    node.policy.ask_first.set("lane", SURFACE, True, by="owner", person=True)
    _, key = node.key_row(ceiling="commit", surfaces=[SURFACE], expires_at=node.keys.clock() + 5 * DAY + 0.7)
    async with node.client(key) as c:
        run = (await c.post(COMMIT_PATH, **NOTE)).json()["run"]
        assert (await c.get(f"/v1/sites/{API}/runs/{run}")).status_code == 202
        node.policy.decide(node.runner.get(run).approval_id, "approve")
        await node.runner.poll_approvals()
        assert (await poll(c, f"/v1/sites/{API}/runs/{run}")).status_code == 200
    assert node.site.state["notes"] == ["hello"]


async def test_a_key_that_ends_after_the_longest_grant_is_renewed_up_to_its_end(node):
    node.policy.cfg.max_grant_ttl_s = 40 * DAY
    row, _ = node.key_row(surfaces=[SURFACE], expires_at=node.keys.clock() + 60 * DAY)
    assert node.policy.grant_by_id(row.grant_id).expires_at < row.expires_at
    real = node.keys.clock
    node.keys.clock = lambda: real() + 35 * DAY
    renewed = node.keys.used(row)
    node.keys.clock = real
    assert renewed.grant_id != row.grant_id


# ---- a renewal ----

async def test_a_renewal_leaves_the_old_grant_live_and_keeps_the_key_working(node):
    row, key = node.key_row(surfaces=[SURFACE])
    async with node.client(key) as c:
        with near_the_end(node):
            assert (await c.get(READ_PATH)).status_code == 200
    renewed = node.keys.get(row.id)
    assert renewed.grant_id != row.grant_id and state(node, row.grant_id) == "active"
    assert list(renewed.old_grants) == [row.grant_id]
    assert "old_grants" not in renewed.public()


async def test_a_node_with_short_grants_does_not_renew_on_every_call(node):
    node.policy.cfg.max_grant_ttl_s = 20 * DAY  # every grant is inside 30 days from the start
    row, key = node.key_row(surfaces=[SURFACE])
    before = grants(node)
    async with node.client(key) as c:
        for _ in range(3):
            assert (await c.get(READ_PATH)).status_code == 200
    assert node.keys.get(row.id).grant_id == row.grant_id and grants(node) == before


async def test_after_a_renewal_a_key_that_read_page_text_still_asks_before_a_commit(node):
    row, key = node.key_row(ceiling="commit", surfaces=[SURFACE])
    async with node.client(key) as c:
        assert (await c.get(READ_PATH)).status_code == 200
        with near_the_end(node):
            r = await c.post(COMMIT_PATH, **NOTE)
    assert node.keys.get(row.id).grant_id != row.grant_id
    assert r.status_code == 202 and r.json()["code"] == "approval_pending" and node.site.state["notes"] == []


async def test_a_label_that_lands_on_the_old_grant_after_a_renewal_still_counts(node):
    """A call still in the air when the key renewed holds the old grant, and labels the old family."""
    row, key = node.key_row(ceiling="commit", surfaces=[SURFACE])
    with near_the_end(node):
        renewed = node.keys.used(row)
    node.keys.context(row, tool="harbor_supplier_open_orders").taint("untrusted", source="a call in the air")
    async with node.client(key) as c:
        r = await c.post(COMMIT_PATH, **NOTE)
    assert "untrusted" in node.policy.taint.get(family_key(node.policy.grant_by_id(renewed.grant_id)))
    assert r.status_code == 202 and r.json()["code"] == "approval_pending"


async def test_a_run_parked_before_a_renewal_still_completes(node):
    node.policy.ask_first.set("lane", SURFACE, True, by="owner", person=True)
    row, key = node.key_row(ceiling="commit", surfaces=[SURFACE])
    async with node.client(key) as c:
        run = (await c.post(COMMIT_PATH, **NOTE)).json()["run"]
        with near_the_end(node):
            assert (await c.get(f"/v1/sites/{API}/runs/{run}")).status_code == 202
        assert node.keys.get(row.id).grant_id != row.grant_id
        node.policy.decide(node.runner.get(run).approval_id, "approve")
        await node.runner.poll_approvals()
        done = await poll(c, f"/v1/sites/{API}/runs/{run}")
    assert done.status_code == 200 and node.site.state["notes"] == ["hello"]


async def test_revoking_after_a_renewal_ends_every_grant_of_the_key(node):
    node.policy.ask_first.set("lane", SURFACE, True, by="owner", person=True)
    row, key = node.key_row(ceiling="commit", surfaces=[SURFACE])
    async with node.client(key) as c:
        run = (await c.post(COMMIT_PATH, **NOTE)).json()["run"]
    with near_the_end(node):
        renewed = node.keys.used(row)
    assert state(node, row.grant_id) == "active"
    node.keys.revoke(row.id, by=OWNER)
    assert state(node, row.grant_id) == state(node, renewed.grant_id) == "revoked"
    node.policy.decide(node.runner.get(run).approval_id, "approve")
    await node.runner.poll_approvals()
    ended = await node.runner.wait(run, 5)
    assert ended.status == RunStatus.FAILED and ended.error["code"] == "grant_revoked" and node.site.state["notes"] == []


async def test_an_edit_after_a_renewal_ends_every_grant_and_keeps_the_labels(node):
    row, key = node.key_row(ceiling="commit", surfaces=[SURFACE])
    async with node.client(key) as c:
        assert (await c.get(READ_PATH)).status_code == 200
        with near_the_end(node):
            renewed = node.keys.used(node.keys.get(row.id))
        edited = node.keys.edit(row.id, rate_per_min=5, ceiling="commit", by=OWNER)
        r = await c.post(COMMIT_PATH, **NOTE)
    assert state(node, row.grant_id) == state(node, renewed.grant_id) == "revoked" and edited.old_grants == {}
    assert r.status_code == 202 and r.json()["code"] == "approval_pending"


async def test_a_freeze_after_a_renewal_ends_every_grant_of_the_key(node):
    row, _ = node.key_row(surfaces=[SURFACE])
    with near_the_end(node):
        renewed = node.keys.used(row)
    await node.runtime.get("freeze").freeze("lane", SURFACE, reason="stop", by="owner")
    assert state(node, row.grant_id) == state(node, renewed.grant_id) == "revoked"


# ---- a grant that ran out ----

async def test_a_key_unused_past_its_grants_end_gets_a_new_grant(node):
    row, key = node.key_row(surfaces=[SURFACE])
    async with node.client(key) as c:
        with past_the_end(node):
            assert node.keys.state(row) == "active"
            assert (await c.get(READ_PATH)).status_code == 200
            renewed = node.keys.get(row.id)
            assert renewed.grant_id != row.grant_id and node.policy.grant_by_id(renewed.grant_id)
            assert renewed.old_grants == {}


async def test_what_a_key_read_counts_after_its_grant_ran_out(node):
    _, key = node.key_row(ceiling="commit", surfaces=[SURFACE])
    async with node.client(key) as c:
        assert (await c.get(READ_PATH)).status_code == 200
        with past_the_end(node):
            r = await c.post(COMMIT_PATH, **NOTE)
    assert r.status_code == 202 and r.json()["code"] == "approval_pending"


async def test_a_new_grant_after_the_end_covers_what_the_old_one_did(node):
    row, _ = node.key_row(surfaces=[SURFACE])
    with past_the_end(node):
        new = node.policy.grant_by_id(node.keys.used(row).grant_id)
    assert new.lane_allowed(SURFACE) and new.domain_allowed("portal.northsupply.example")
    assert new.ceiling.value == "read" and new.subject.id == row.subject


async def test_a_revoked_grant_past_its_end_is_not_replaced(node):
    row, key = node.key_row(surfaces=[SURFACE])
    node.policy.revoke(row.grant_id, by="owner", reason="the grants page")
    before = grants(node)
    async with node.client(key) as c:
        with past_the_end(node):
            assert node.keys.state(row) == "grant_ended"
            assert (await c.get(READ_PATH)).status_code == 401
    assert grants(node) == before and node.keys.get(row.id).grant_id == row.grant_id


async def test_a_grant_a_freeze_swept_is_not_replaced_once_it_is_past_its_end(node):
    _, key = node.key_row(surfaces=[SURFACE])
    freeze = node.runtime.get("freeze")
    await freeze.freeze("lane", SURFACE, reason="stop", by="owner")
    await freeze.unfreeze("lane", SURFACE, by="owner", via="cli")
    before = grants(node)
    async with node.client(key) as c:
        with past_the_end(node):
            assert (await c.get(READ_PATH)).status_code == 401
    assert grants(node) == before


async def test_a_freeze_ends_a_key_whose_grant_had_already_run_out(node):
    _, key = node.key_row(surfaces=[SURFACE])
    freeze = node.runtime.get("freeze")
    async with node.client(key) as c:
        with past_the_end(node):
            await freeze.freeze("lane", SURFACE, reason="stop", by="owner")
            await freeze.unfreeze("lane", SURFACE, by="owner", via="cli")
            assert (await c.get(READ_PATH)).status_code == 401  # lying idle did not get it past the freeze


async def test_no_new_grant_is_issued_during_a_freeze(node):
    _, key = node.key_row(surfaces=[SURFACE])
    before = grants(node)
    async with node.client(key) as c:
        with past_the_end(node):  # the sweep passes over a grant that already ran out
            await node.runtime.get("freeze").freeze("lane", SURFACE, reason="stop", by="owner")
            r = await c.get(READ_PATH)
    assert r.status_code == 503 and r.json()["code"] == "frozen" and grants(node) == before


# ---- a surface whose id is not its lane ----

async def test_a_keys_grant_reaches_the_lane_behind_its_surface(node):
    await login_lane(node)
    row, _ = node.key_row(surfaces=[SURFACE])
    g = node.policy.grant_by_id(row.grant_id)
    assert g.lane_allowed(SURFACE) and g.lane_allowed(LOGIN)


async def test_a_freeze_of_the_lane_behind_a_surface_stops_the_key_until_the_unfreeze(node):
    """The key's grant reaches the surface and its login lane: a freeze of one of them keeps the grant
    (it reaches beyond), and the key's calls there answer frozen until the owner unfreezes."""
    await login_lane(node)
    row, key = node.key_row(surfaces=[SURFACE])
    freeze = node.runtime.get("freeze")
    async with node.client(key) as c:
        assert (await c.get(READ_PATH)).status_code == 200
        await freeze.freeze("lane", LOGIN, reason="stop", by="owner")
        r = await c.get(READ_PATH)
        assert r.status_code == 503 and r.json()["code"] == "frozen" and state(node, row.grant_id) == "active"
        await freeze.unfreeze("lane", LOGIN, by="owner", via="cli")
        assert (await c.get(READ_PATH)).status_code == 200


async def test_a_freeze_that_holds_every_lane_of_the_key_ends_it(node):
    await login_lane(node)
    row, key = node.key_row(surfaces=[SURFACE])
    freeze = node.runtime.get("freeze")
    async with node.client(key) as c:
        assert (await c.get(READ_PATH)).status_code == 200
        await freeze.freeze("workspace", "harbor", reason="stop", by="owner")
        r = await c.get(READ_PATH)
        assert r.status_code == 503 and r.json()["code"] == "frozen" and state(node, row.grant_id) == "revoked"
        await freeze.unfreeze("workspace", "harbor", by="owner", via="cli")
        assert (await c.get(READ_PATH)).status_code == 401


@pytest.mark.parametrize("params", [{}, {"max_age": 0}], ids=["a hit", "a miss"])
async def test_the_lane_behind_a_surface_is_checked_on_every_call(node, params):
    """A key made before the surface moved: the sweep cannot find it, so the call's own check must."""
    _, key = node.key_row(surfaces=[SURFACE])
    await login_lane(node)
    async with node.client(key) as c:
        assert (await c.get(READ_PATH)).status_code == 200
        reads = node.site.state["reads"]
        await node.runtime.get("freeze").freeze("lane", LOGIN, reason="stop", by="owner")
        r = await c.get(READ_PATH, params=params)
    assert r.status_code == 503 and r.json()["code"] == "frozen" and node.site.state["reads"] == reads


async def test_a_run_a_freeze_of_the_lane_cancelled_answers_frozen(node):
    node.policy.ask_first.set("lane", SURFACE, True, by="owner", person=True)
    _, key = node.key_row(ceiling="commit", surfaces=[SURFACE])
    await login_lane(node)
    async with node.client(key) as c:
        run = (await c.post(COMMIT_PATH, **NOTE)).json()["run"]
        await node.runtime.get("freeze").freeze("lane", LOGIN, reason="stop", by="owner")
        assert node.runner.get(run).status == RunStatus.CANCELLED
        r = await c.get(f"/v1/sites/{API}/runs/{run}")
    assert r.status_code == 503 and r.json()["code"] == "frozen"


async def test_a_run_a_freeze_of_the_lane_stops_under_a_waiting_call_answers_frozen(tmp_path):
    doc = definition()
    doc["endpoints"]["open_orders"]["steps"].insert(1, {"type": "pause", "min_ms": 3000, "max_ms": 3000})
    async with Node(tmp_path, doc=doc) as n:
        _, key = n.key_row(surfaces=[SURFACE])
        await login_lane(n)
        async with n.client(key) as c:
            call = asyncio.create_task(c.get(READ_PATH, params={"wait": 30}))
            await asyncio.sleep(0.3)
            await n.runtime.get("freeze").freeze("lane", LOGIN, reason="stop", by="owner")
            r = await asyncio.wait_for(call, 5)
    assert r.status_code == 503 and r.json()["code"] == "frozen"
