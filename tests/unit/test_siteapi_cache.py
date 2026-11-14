"""The cache behind /v1/sites: a hit is never a way around anything, and concurrent misses share a run."""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest
from siteapi_http_support import (
    API,
    OTHER,
    OWNER,
    READ_PATH,
    SURFACE,
    Node,
    definition,
    poll,
    portal,
    search_endpoint,
)

from agentos.core.models import LaneSpec, RunStatus
from agentos.runner.fakes import FakeClock
from agentos.runner.siteapis.cache import SiteCache, cache_key
from agentos.runner.siteapis.http_store import HttpStore
from agentos.runner.siteapis.models import web_surface


class Pausing:
    """The site_apis service as far as /v1/sites reads it."""

    def __init__(self):
        self.paused = set()

    def state_of(self, api):
        return "paused" if api in self.paused else "active"


def key_of(node, name="open_orders", surface=SURFACE, **params):
    fv = node.flows.get(f"connector.{API}.{name}")
    return cache_key(fv.id, fv.version, surface, surface, {"status": "open", "acts_as_id": surface,
                                                           "account_language": "en", **params})


@pytest.fixture
async def node(tmp_path):
    async with Node(tmp_path) as n:
        yield n


async def filled(node, key=None):
    key = key or node.key(surfaces=[SURFACE])
    async with node.client(key) as c:
        r = await c.get(READ_PATH)
        assert r.status_code == 200, r.text
    return key


# ---- auth first, cache second ----

async def test_a_revoked_key_answers_401_while_its_result_is_cached(node):
    row, key = node.key_row(surfaces=[SURFACE])
    await filled(node, key)
    assert node.cache.get(key_of(node)) is not None
    node.keys.revoke(row.id, by=OWNER)
    async with node.client(key) as c:
        assert (await c.get(READ_PATH)).status_code == 401


async def test_an_expired_key_answers_401_while_its_result_is_cached(node):
    row, key = node.key_row(surfaces=[SURFACE])
    await filled(node, key)
    node.site_http.store.update_key(row.id, expires_at=node.keys.clock() - 1)
    async with node.client(key) as c:
        assert (await c.get(READ_PATH)).status_code == 401


async def test_a_grant_ended_behind_the_keys_back_stops_a_hit(node):
    row, key = node.key_row(surfaces=[SURFACE])
    await filled(node, key)
    node.policy.revoke(row.grant_id, by="owner")
    async with node.client(key) as c:
        assert (await c.get(READ_PATH)).status_code == 401


async def test_a_hit_counts_against_the_key_limit(node):
    key = node.key(rate_per_min=2, surfaces=[SURFACE])
    await filled(node, key)
    async with node.client(key) as c:
        assert (await c.get(READ_PATH)).json()["cache"]["hit"] is True
        r = await c.get(READ_PATH)
    assert r.status_code == 429 and r.json()["code"] == "rate_limited_key"


async def test_a_hit_respects_the_keys_scope(node):
    await filled(node)
    no_endpoint = node.key(endpoints=["send_note"], ceiling="commit", surfaces=[SURFACE])
    no_surface = node.key(surfaces=[OTHER])
    async with node.client(no_endpoint) as c:
        assert (await c.get(READ_PATH)).status_code == 404
    async with node.client(no_surface) as c:
        assert (await c.get(READ_PATH, params={"on": SURFACE})).status_code == 403
    assert node.site.state["reads"] == 1


async def test_a_hit_respects_a_pause(node):
    await_key = await filled(node)
    pausing = Pausing()
    node.runtime.register("site_apis", pausing)
    pausing.paused.add(API)
    async with node.client(await_key) as c:
        r = await c.get(READ_PATH)
    assert r.status_code == 409 and r.json()["code"] == "paused"


async def test_a_hit_on_a_held_lane_answers_and_a_miss_says_needs_human(node):
    """A hold keeps work off the lane; a hit touches nothing on it (docs/API.md and the http module say why)."""
    key = await filled(node)
    await node.lanes.hold(SURFACE, reason="needs_human:captcha")
    async with node.client(key) as c:
        hit = await c.get(READ_PATH)
        assert hit.status_code == 200 and hit.json()["cache"]["hit"] is True
        miss = await c.get(READ_PATH, params={"max_age": 0})
        assert miss.status_code == 503 and miss.json()["code"] == "needs_human" and "retry-after" not in miss.headers
        other = await c.get(READ_PATH, params={"status": "late"})
        assert other.status_code == 503 and other.json()["code"] == "needs_human"
    assert node.site.state["reads"] == 1 and node.runner.list(limit=10)[0].status == RunStatus.SUCCEEDED_VERIFIED


# ---- what a hit is ----

async def test_a_hit_starts_no_run_and_uses_no_pace(tmp_path):
    doc = definition()
    doc["endpoints"]["open_orders"]["pace"] = {"per_hour": 1}
    async with Node(tmp_path, doc=doc) as n:
        key = await filled(n)
        async with n.client(key) as c:
            for _ in range(5):
                assert (await c.get(READ_PATH)).json()["cache"]["hit"] is True
        assert len(n.runner.list(limit=10)) == 1


async def test_max_age_0_is_a_fresh_read_and_still_pays_pace(tmp_path):
    doc = definition()
    doc["endpoints"]["open_orders"]["pace"] = {"per_hour": 1}
    async with Node(tmp_path, doc=doc) as n:
        key = await filled(n)
        async with n.client(key) as c:
            r = await c.get(READ_PATH, params={"max_age": 0})
    assert r.status_code == 429 and r.json()["code"] == "pace_limited" and int(r.headers["retry-after"]) > 0
    assert r.json()["retry_at"]


async def test_max_age_only_lowers_the_cache_time(node):
    key = await filled(node)
    async with node.client(key) as c:
        assert (await c.get(READ_PATH, params={"max_age": 100_000})).json()["cache"]["hit"] is True
        node.site_http.store._conn().execute("UPDATE site_api_cache SET stored_at = stored_at - 100")
        assert (await c.get(READ_PATH, params={"max_age": 200})).json()["cache"]["age_s"] >= 100
        fresh = await c.get(READ_PATH, params={"max_age": 50})
    assert fresh.json()["cache"]["hit"] is False and node.site.state["reads"] == 2


async def test_a_changed_version_misses(node):
    key = await filled(node)
    doc = definition()
    doc["version"] = "1.0.1"
    doc["endpoints"]["open_orders"]["cache_s"] = 299
    from agentos.runner.siteapis.models import install_for_test

    install_for_test(node.flows, doc)
    async with node.client(key) as c:
        r = await c.get(READ_PATH)
    assert r.json()["cache"]["hit"] is False and r.json()["source"]["version"] == "1.0.1"
    assert node.site.state["reads"] == 2


async def test_params_are_part_of_the_key_and_a_default_equals_leaving_it_out(node):
    key = await filled(node)
    async with node.client(key) as c:
        assert (await c.get(READ_PATH, params={"status": "open"})).json()["cache"]["hit"] is True
        assert (await c.get(READ_PATH, params={"status": "late"})).json()["cache"]["hit"] is False


async def test_a_cached_value_keeps_its_untrusted_fields(node):
    key = await filled(node)
    async with node.client(key) as c:
        r = await c.get(READ_PATH)
    assert r.json()["cache"]["hit"] is True and r.json()["untrusted_fields"] == ["orders[].number", "orders[].total"]
    assert r.json()["source"]["run"] and int(r.headers["age"]) == r.json()["cache"]["age_s"]


async def test_etag_and_if_none_match_answer_304_after_the_key_check(node):
    row, key = node.key_row(surfaces=[SURFACE])
    await filled(node, key)
    async with node.client(key) as c:
        r = await c.get(READ_PATH)
        tag = r.headers["etag"]
        assert (await c.post(READ_PATH, json={}, headers={"If-None-Match": tag})).status_code == 200  # a GET's rule
        same = await c.get(READ_PATH, headers={"If-None-Match": tag})
        assert same.status_code == 304 and same.content == b"" and same.headers["etag"] == tag
        assert (await c.get(READ_PATH, headers={"If-None-Match": '"other"'})).status_code == 200
        node.keys.revoke(row.id, by=OWNER)
        assert (await c.get(READ_PATH, headers={"If-None-Match": tag})).status_code == 401


async def test_nothing_downstream_may_serve_a_result_without_the_node(node):
    key = await filled(node)
    async with node.client(key) as c:
        r = await c.get(READ_PATH)
    assert r.headers["cache-control"] == "private, no-cache"


async def test_a_private_endpoint_is_no_store_and_its_result_stays_on_the_node(tmp_path):
    doc = definition()
    doc["endpoints"]["open_orders"]["steps"][1]["fields"]["total"]["private"] = True
    async with Node(tmp_path, doc=doc) as n:
        key = n.key(surfaces=[SURFACE])
        async with n.client(key) as c:
            first = await c.get(READ_PATH)
            second = await c.get(READ_PATH)
    assert first.headers["cache-control"] == "no-store" and second.headers["cache-control"] == "no-store"
    assert first.json()["private_fields"] == ["orders[].total"] and second.json()["cache"]["hit"] is True


# ---- what a result belongs to ----

async def test_a_try_of_an_edited_draft_leaves_the_live_versions_results_alone(tmp_path):
    async with Node(tmp_path, install=False) as n:
        svc = n.runtime.get("site_apis")
        doc = definition()
        svc.save(doc, by=OWNER)
        svc.activate(API, by=OWNER)
        key = await filled(n)
        doc = copy.deepcopy(doc)
        doc["endpoints"]["open_orders"]["title"] = "Orders still open"
        svc.save(doc, by=OWNER, bump=True)
        out = await svc.try_endpoint(API, "open_orders", {"status": "open"}, by=OWNER, on=SURFACE)
        assert out["data"]["orders"]
        async with n.client(key) as c:
            assert (await c.get(READ_PATH)).json()["cache"]["hit"] is True


async def test_a_try_of_what_is_live_stores_nothing(tmp_path):
    async with Node(tmp_path, install=False) as n:
        svc = n.runtime.get("site_apis")
        svc.save(definition(), by=OWNER)
        svc.activate(API, by=OWNER)
        await svc.try_endpoint(API, "open_orders", {"status": "open"}, by=OWNER, on=SURFACE)
        assert n.site_http.store.cache_bytes() == 0


async def test_a_surface_on_another_lane_does_not_answer_from_the_old_lanes_read(node):
    key = await filled(node)
    await node.lanes.create(LaneSpec(id="harbor/login"))
    node.runner.surfaces.put(web_surface(SURFACE, "harbor/login"), by=OWNER)
    async with node.client(key) as c:
        first, second = await c.get(READ_PATH), await c.get(READ_PATH)
    assert first.json()["cache"]["hit"] is False and second.json()["cache"]["hit"] is True
    assert node.site.state["reads"] == 2


def test_the_cache_key_has_the_binding_in_it():
    params = {"status": "open", "acts_as_id": "page-1", "account_language": "en"}
    base = cache_key("f", "1", "s/a", "s/lane", params)
    assert base == cache_key("f", "1", "s/a", "s/lane", dict(reversed(params.items())))
    assert base != cache_key("f", "1", "s/a", "s/other", params)
    assert base != cache_key("f", "1", "s/a", "s/lane", {**params, "acts_as_id": "page-2"})
    assert base != cache_key("f", "1", "s/a", "s/lane", {**params, "account_language": "lt"})


async def test_a_result_is_stored_under_what_the_run_ran_as_not_what_the_surface_is_now(node):
    key = node.key(surfaces=[SURFACE])
    async with node.client(key) as c:
        run = (await c.get(READ_PATH)).json()["source"]["run"]
        await node.lanes.create(LaneSpec(id="harbor/login"))
        node.runner.surfaces.put(web_surface(SURFACE, "harbor/login"), by=OWNER)
        node.site_http.store._conn().execute("DELETE FROM site_api_cache")
        node.cache.record(node.runner.get(run))  # as if it ended just after the move
        assert (await c.get(READ_PATH)).json()["cache"]["hit"] is False


async def test_stale_ok_stops_at_seven_days(node):
    key = await filled(node)
    store = node.site_http.store
    await node.lanes.hold(SURFACE, reason="needs_human:captcha")
    async with node.client(key) as c:
        store._conn().execute("UPDATE site_api_cache SET stored_at = stored_at - ?", (6 * 86400,))
        r = await c.get(READ_PATH, params={"stale_ok": 1, "max_age": 0})
        assert r.status_code == 200 and r.json()["stale"] is True
        store._conn().execute("UPDATE site_api_cache SET stored_at = stored_at - ?", (2 * 86400,))
        r = await c.get(READ_PATH, params={"stale_ok": 1, "max_age": 0})
    assert r.status_code == 503 and r.json()["code"] == "needs_human"


# ---- concurrent misses ----

async def test_concurrent_misses_share_one_run(tmp_path):
    slow = definition()
    slow["endpoints"]["open_orders"]["steps"].insert(1, {"type": "pause", "min_ms": 300, "max_ms": 300})
    async with Node(tmp_path, doc=slow) as n:
        first, second = n.key(surfaces=[SURFACE]), n.key(surfaces=[SURFACE])
        async with n.client(first) as a, n.client(second) as b:
            out = await asyncio.gather(*[(a if i % 2 else b).get(READ_PATH) for i in range(8)])
        assert [r.status_code for r in out] == [200] * 8
        assert len({r.json()["source"]["run"] for r in out}) == 1
        assert n.site.state["reads"] == 1 and len(n.runner.list(limit=20)) == 1


def queue_behind_a_slow_read() -> dict:
    slow = search_endpoint()
    slow["steps"].insert(1, {"type": "pause", "min_ms": 400, "max_ms": 400})
    return definition(slow_read=slow)


async def test_a_joiner_whose_leaders_grant_ended_starts_its_own_run(tmp_path):
    async with Node(tmp_path, doc=queue_behind_a_slow_read()) as n:
        leader, key = n.key_row(surfaces=[SURFACE])
        other = n.key(surfaces=[SURFACE])
        async with n.client(key) as a, n.client(other) as b:
            assert (await b.get(f"/v1/sites/{API}/slow_read", params={"wait": 0})).status_code == 202
            first = (await a.get(READ_PATH, params={"wait": 0})).json()["run"]
            n.keys.revoke(leader.id, by=OWNER)
            r = await b.get(READ_PATH, params={"wait": 10})
        assert r.status_code == 200, r.text
        assert r.json()["source"]["run"] != first and n.runner.get(first).error["code"] == "grant_revoked"


async def test_a_live_key_polling_a_run_whose_grant_ended_is_not_told_its_key_is_bad(tmp_path):
    async with Node(tmp_path, doc=queue_behind_a_slow_read()) as n:
        leader, key = n.key_row(surfaces=[SURFACE])
        other = n.key(surfaces=[SURFACE])
        async with n.client(key) as a, n.client(other) as b:
            assert (await b.get(f"/v1/sites/{API}/slow_read", params={"wait": 0})).status_code == 202
            first = (await a.get(READ_PATH, params={"wait": 0})).json()["run"]
            n.keys.revoke(leader.id, by=OWNER)
            await n.runner.wait(first, 5)
            r = await poll(b, f"/v1/sites/{API}/runs/{first}")
        assert r.status_code == 500 and r.json()["code"] == "run_failed"


async def test_a_failed_start_does_not_fail_the_callers_who_joined_it():
    from agentos.runner.siteapis.cache import Flights

    flights = Flights()
    gate = asyncio.Event()
    calls = []

    async def bad():
        calls.append("bad")
        await gate.wait()
        raise ValueError("the first caller's grant")

    async def good():
        calls.append("good")
        return "run_2"

    leader = asyncio.create_task(flights.share("k", bad, lambda _r: True))
    await asyncio.sleep(0)
    joiner = asyncio.create_task(flights.share("k", good, lambda _r: True))
    await asyncio.sleep(0)
    gate.set()
    with pytest.raises(ValueError):
        await leader
    assert await joiner == ("run_2", True) and calls == ["bad", "good"]


async def test_a_cancelled_start_does_not_cancel_the_callers_who_joined_it():
    from agentos.runner.siteapis.cache import Flights

    flights = Flights()

    async def stuck():
        await asyncio.sleep(30)

    async def good():
        return "run_2"

    leader = asyncio.create_task(flights.share("k", stuck, lambda _r: True))
    await asyncio.sleep(0)
    joiner = asyncio.create_task(flights.share("k", good, lambda _r: True))
    await asyncio.sleep(0)
    leader.cancel()
    assert await joiner == ("run_2", True)


async def test_a_finished_run_is_not_joined(node):
    key = await filled(node)
    async with node.client(key) as c:
        r = await c.get(READ_PATH, params={"max_age": 0})
    assert r.json()["cache"]["hit"] is False and node.site.state["reads"] == 2


async def test_a_run_that_outlives_its_caller_still_fills_the_cache(tmp_path):
    slow = definition()
    slow["endpoints"]["open_orders"]["steps"].insert(1, {"type": "pause", "min_ms": 200, "max_ms": 200})
    async with Node(tmp_path, doc=slow) as n:
        key = n.key(surfaces=[SURFACE])
        async with n.client(key) as c:
            r = await c.get(READ_PATH, params={"wait": 0})
            assert r.status_code == 202 and r.json()["code"] == "pending"
            done = await poll(c, f"/v1/sites/{API}/runs/{r.json()['run']}")
            assert done.status_code == 200
            assert (await c.get(READ_PATH)).json()["cache"]["hit"] is True


# ---- stale_ok ----

async def stale_node(node):
    key = await filled(node)
    node.site_http.store._conn().execute("UPDATE site_api_cache SET stored_at = stored_at - 100000")
    return key


async def test_stale_ok_returns_the_last_good_result_on_needs_human(node):
    key = await stale_node(node)
    await node.lanes.hold(SURFACE, reason="needs_human:captcha")
    async with node.client(key) as c:
        plain = await c.get(READ_PATH)
        assert plain.status_code == 503 and plain.json()["code"] == "needs_human"
        r = await c.get(READ_PATH, params={"stale_ok": 1})
    assert r.status_code == 200 and r.json()["stale"] is True and r.json()["data"]["orders"]
    assert r.json()["cache"]["age_s"] >= 100000


async def test_stale_ok_returns_it_on_the_sites_rate_limit(node):
    key = await stale_node(node)
    await node.lanes.hold(SURFACE, reason="backoff:rate_limited")
    async with node.client(key) as c:
        r = await c.get(READ_PATH, params={"stale_ok": 1})
    assert r.status_code == 200 and r.json()["stale"] is True


async def test_stale_ok_returns_it_on_pace(tmp_path):
    doc = definition()
    doc["endpoints"]["open_orders"]["pace"] = {"per_hour": 1}
    async with Node(tmp_path, doc=doc) as n:
        key = await stale_node(n)
        async with n.client(key) as c:
            r = await c.get(READ_PATH, params={"stale_ok": 1})
    assert r.status_code == 200 and r.json()["stale"] is True


async def test_stale_ok_is_for_those_codes_only(node):
    key = await stale_node(node)
    pausing = Pausing()
    node.runtime.register("site_apis", pausing)
    pausing.paused.add(API)
    async with node.client(key) as c:
        assert (await c.get(READ_PATH, params={"stale_ok": 1})).status_code == 409
        pausing.paused.clear()
        node.driver.hooks.append(lambda op, target, d: (_ for _ in ()).throw(RuntimeError("no browser")))
        r = await c.get(READ_PATH, params={"stale_ok": 1, "max_age": 0, "wait": 5})
        assert r.status_code >= 500 and r.json()["code"] != "needs_human" and "data" not in r.json()
        await node.runtime.get("freeze").freeze("node", reason="stop", by="owner")
        assert (await c.get(READ_PATH, params={"stale_ok": 1})).json()["code"] == "frozen"


async def test_stale_ok_with_nothing_kept_answers_the_error(node):
    key = node.key(surfaces=[SURFACE])
    await node.lanes.hold(SURFACE, reason="needs_human:captcha")
    async with node.client(key) as c:
        r = await c.get(READ_PATH, params={"stale_ok": 1})
    assert r.status_code == 503


# ---- what is kept ----

def run_of(fv, *, status=RunStatus.SUCCEEDED_VERIFIED, data=None, surface=SURFACE, rid="run_1", clock=None):
    return SimpleNamespace(id=rid, flow_id=fv.id, flow_version=fv.version, surface=surface, lane=surface, source="tool",
                           status=status,
                           params={"status": "open", "acts_as_id": "x"}, finished_at=(clock or FakeClock()).now(),
                           output={"data": data if data is not None else {"orders": [{"number": "1"}]},
                                   "untrusted_fields": ["orders[].number"], "private_fields": [], "truncated": False})


async def test_a_result_under_expect_min_items_is_not_kept_and_does_not_replace_the_last(tmp_path):
    site = portal()
    async with Node(tmp_path, site=site) as n:
        key = await filled(n)
        site.state["rows"].clear()
        async with n.client(key) as c:
            r = await c.get(READ_PATH, params={"max_age": 0})
            assert r.status_code == 422 and r.json()["code"] == "empty_result"
            assert (await c.get(READ_PATH)).json()["data"]["orders"]  # the last good one


async def test_an_endpoint_with_cache_s_0_keeps_nothing(tmp_path):
    doc = definition()
    doc["endpoints"]["open_orders"]["cache_s"] = 0
    async with Node(tmp_path, doc=doc) as n:
        key = await filled(n)
        async with n.client(key) as c:
            assert (await c.get(READ_PATH)).json()["cache"]["hit"] is False
        assert n.site_http.store.cache_bytes() == 0


async def test_the_cache_is_bounded_by_size_with_the_oldest_used_out_first(tmp_path):
    from agentos.runner.flows import FlowService
    from agentos.runner.siteapis.models import install_for_test
    from agentos.runner.store import RunnerStore

    flows = FlowService(RunnerStore(tmp_path / "runner.db"))
    (fv,) = install_for_test(flows)
    clock = FakeClock()
    store = HttpStore(tmp_path)
    cache = SiteCache(store, flows, clock=clock)
    cache.record(run_of(fv, surface="harbor/size", clock=clock))
    one = store.cache_bytes()
    cache.max_bytes = one * 12
    keys = []
    for i in range(30):
        run = run_of(fv, surface=f"harbor/s{i}", rid=f"run_{i}", clock=clock)
        cache.record(run)
        keys.append(cache_key(fv.id, fv.version, run.surface, run.lane, run.params))
        clock.advance(120)
        cache.get(keys[0])  # the first stays in use
    assert store.cache_bytes() <= one * 12
    assert cache.get(keys[0]) is not None and cache.get(keys[1]) is None and cache.get(keys[-1]) is not None
    big = run_of(fv, data={"orders": [{"number": "x" * (cache.max_entry + 1)}]}, surface="harbor/big")
    cache.record(big)
    assert cache.get(cache_key(fv.id, fv.version, big.surface, big.lane, big.params)) is None


async def test_a_pause_or_a_removal_deletes_what_is_cached(node):
    await filled(node)
    assert node.site_http.store.cache_bytes() > 0
    assert node.site_http.api_dropped(API) == 1 and node.site_http.store.cache_bytes() == 0


async def test_a_read_in_the_air_when_a_person_pauses_does_not_put_it_back(tmp_path):
    slow = definition()
    slow["endpoints"]["open_orders"]["steps"].insert(1, {"type": "pause", "min_ms": 200, "max_ms": 200})
    async with Node(tmp_path, doc=slow) as n:
        key = n.key(surfaces=[SURFACE])
        pausing = Pausing()
        n.runtime.register("site_apis", pausing)
        async with n.client(key) as c:
            r = await c.get(READ_PATH, params={"wait": 0})
            assert r.status_code == 202
            pausing.paused.add(API)
            await n.runner.wait(r.json()["run"], 5)
        assert n.site_http.store.cache_bytes() == 0


async def test_only_a_successful_read_of_a_local_api_is_kept(tmp_path):
    from agentos.runner.flows import FlowService
    from agentos.runner.siteapis.models import install_for_test
    from agentos.runner.store import RunnerStore

    flows = FlowService(RunnerStore(tmp_path / "runner.db"))
    doc = copy.deepcopy(definition())
    fvs = {fv.id.rsplit(".", 1)[1]: fv for fv in install_for_test(flows, doc)}
    store = HttpStore(tmp_path)
    cache = SiteCache(store, flows)
    cache.record(run_of(fvs["open_orders"], status=RunStatus.FAILED))
    cache.record(run_of(fvs["send_note"]))
    assert store.cache_bytes() == 0
    cache.record(run_of(fvs["open_orders"]))
    assert store.cache_bytes() > 0
