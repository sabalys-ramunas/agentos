"""/v1/sites over real HTTP: a call is the tool's call, with the key's own limits around it."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import pytest
from siteapi_http_support import (
    API,
    COMMIT_PATH,
    OTHER,
    OWNER,
    READ_PATH,
    SURFACE,
    TOKEN,
    Node,
    definition,
    poll,
)
from starlette.routing import Route

from agentos.api.auth import SESSION_COOKIE
from agentos.core.config import NodeConfig
from agentos.core.models import RunStatus
from agentos.node.daemon import _Uvicorn
from agentos.policy.redact import redact_text
from agentos.policy.taint import family_key
from agentos.runner.siteapis.keys import token_hash


@pytest.fixture
async def node(tmp_path):
    async with Node(tmp_path) as n:
        yield n


async def test_a_read_answers_typed_data_and_the_next_one_is_a_hit(node):
    key = node.key(surfaces=[SURFACE])
    async with node.client(key) as c:
        r = await c.get(READ_PATH)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["data"]["orders"][0]["number"] == "A-1042" and body["status"] == "succeeded_verified"
        assert body["cache"] == {"hit": False, "age_s": 0} and body["stale"] is False
        assert body["untrusted_fields"] == ["orders[].number", "orders[].total"]
        assert body["source"]["api"] == API and body["source"]["surface"] == SURFACE
        assert r.headers["ratelimit-limit"] == "60" and r.headers["agentos-run"] == body["source"]["run"]
        again = await c.get(READ_PATH)
        assert again.json()["cache"]["hit"] is True and again.json()["data"] == body["data"]
    assert node.site.state["reads"] == 1


# ---- a call is the tool's call ----

async def test_a_call_runs_under_the_keys_grant_like_an_agents_tool_call(node):
    row, key = node.key_row(surfaces=[SURFACE], label="billing sync")
    async with node.client(key) as c:
        r = await c.get(READ_PATH)
    run = node.runner.get(r.json()["source"]["run"])
    assert run.grant_id == row.grant_id and run.principal["id"] == f"site-key:{row.id}" and run.source == "tool"
    assert run.surface == SURFACE and run.lane == SURFACE
    assert node.runner.health.get(f"{API}.open_orders", SURFACE)["state"] == "healthy"  # health saw it
    rows, _ = node.policy.traces.query(grant_id=row.grant_id, limit=50)
    kinds = {t.kind for t in rows}
    assert {"connector_run", "site_api_call"} <= kinds  # Activity, written by run_operation and by the call
    call = next(t for t in rows if t.kind == "site_api_call")
    assert "billing sync" in call.summary and row.prefix in call.summary and key not in str(call.model_dump())


async def test_ask_me_first_on_the_lane_answers_202_and_the_run_waits_for_a_person(node):
    node.policy.ask_first.set("lane", SURFACE, True, by="owner", person=True)
    key = node.key(ceiling="commit", surfaces=[SURFACE])
    async with node.client(key) as c:
        r = await c.post(COMMIT_PATH, json={"note": "hello"}, headers={"Idempotency-Key": "one"})
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["code"] == "approval_pending" and body["why"] and r.headers["agentos-run"] == body["run"]
        assert node.site.state["notes"] == []
        run = node.runner.get(body["run"])
        assert run.status == RunStatus.WAITING_APPROVAL
        node.policy.decide(run.approval_id, "approve")
        await node.runner.poll_approvals()
        done = await poll(c, f"/v1/sites/{API}/runs/{body['run']}")
    assert done.status_code == 200 and done.json()["status"] == "succeeded_verified"
    assert node.site.state["notes"] == ["hello"]


async def test_ask_me_first_on_the_endpoint_answers_202(node):
    node.policy.ask_first.set("flow", f"connector.{API}.send_note", True, by="owner", person=True)
    key = node.key(ceiling="commit", surfaces=[SURFACE])
    async with node.client(key) as c:
        r = await c.post(COMMIT_PATH, json={"note": "hi"}, headers={"Idempotency-Key": "one"})
    assert r.status_code == 202 and r.json()["code"] == "approval_pending"
    assert node.site.state["notes"] == []


async def test_a_freeze_answers_503_frozen_and_a_hit_does_not_get_around_it(node):
    key = node.key(surfaces=[SURFACE])
    async with node.client(key) as c:
        assert (await c.get(READ_PATH)).status_code == 200
        await node.runtime.get("freeze").freeze("node", reason="stop", by="owner")
        r = await c.get(READ_PATH)
        assert r.status_code == 503 and r.json()["code"] == "frozen"
        assert r.headers["content-type"].startswith("application/problem+json")
        await node.runtime.get("freeze").unfreeze("node", by="owner", via="cli")
        assert (await c.get(READ_PATH)).status_code == 401  # a freeze revokes the grant, and nothing brings it back


async def test_a_freeze_interrupts_a_call_that_is_waiting_for_its_run(tmp_path):
    doc = definition()
    doc["endpoints"]["open_orders"]["steps"].insert(1, {"type": "pause", "min_ms": 3000, "max_ms": 3000})
    async with Node(tmp_path, doc=doc) as n:
        key = n.key(surfaces=[SURFACE])
        async with n.client(key) as c:
            call = asyncio.create_task(c.get(READ_PATH, params={"wait": 30}))
            await asyncio.sleep(0.3)
            await n.runtime.get("freeze").freeze("node", reason="stop", by="owner")
            r = await asyncio.wait_for(call, 5)
    assert r.status_code == 503 and r.json()["code"] == "frozen"


async def test_a_lane_freeze_stops_a_key_on_that_lane(node):
    key = node.key()
    async with node.client(key) as c:
        await node.runtime.get("freeze").freeze("lane", SURFACE, reason="stop", by="owner")
        r = await c.get(READ_PATH, params={"on": SURFACE})
    assert r.status_code == 503 and r.json()["code"] == "frozen" and node.site.state["reads"] == 0


async def test_a_lane_freeze_does_not_tell_a_key_about_a_surface_it_cannot_see(node):
    key = node.key(surfaces=[OTHER])
    async with node.client(key) as c:
        await node.runtime.get("freeze").freeze("lane", SURFACE, reason="stop", by="owner")
        r = await c.get(READ_PATH, params={"on": SURFACE})
    assert r.status_code == 403 and r.json()["code"] == "denied" and node.site.state["reads"] == 0


async def test_a_read_labels_the_keys_family_and_what_follows_it_asks_first(node):
    row, key = node.key_row(ceiling="commit", surfaces=[SURFACE])
    async with node.client(key) as c:
        assert (await c.get(READ_PATH)).status_code == 200
        assert "untrusted" in node.policy.taint.get(family_key(node.policy.grant_by_id(row.grant_id)))
        r = await c.post(COMMIT_PATH, json={"note": "hi"}, headers={"Idempotency-Key": "one"})
    assert r.status_code == 202 and r.json()["code"] == "approval_pending"


async def test_a_hit_labels_the_family_of_the_key_that_read_it(node):
    first, second = node.key(surfaces=[SURFACE]), node.key_row(surfaces=[SURFACE])
    async with node.client(first) as a, node.client(second[1]) as b:
        await a.get(READ_PATH)
        hit = await b.get(READ_PATH)
    assert hit.json()["cache"]["hit"] is True
    assert "untrusted" in node.policy.taint.get(family_key(node.policy.grant_by_id(second[0].grant_id)))


async def test_a_lanes_commit_limit_is_the_accounts_pace_to_a_key(node):
    node.policy.set_lane_limits(SURFACE, {"commits_per_day": 0}, by="owner")
    key = node.key(ceiling="commit", surfaces=[SURFACE])
    async with node.client(key) as c:
        r = await c.post(COMMIT_PATH, json={"note": "hi"}, headers={"Idempotency-Key": "one"})
    assert r.status_code == 429 and r.json()["code"] == "pace_limited" and int(r.headers["retry-after"]) > 0
    assert node.site.state["notes"] == []


# ---- a key opens /v1/sites and nothing else ----

def every_route(app) -> list[tuple[str, str]]:
    out = []
    for route in app.routes:
        if isinstance(route, Route) and not route.path.startswith("/v1/"):
            path = re.sub(r"\{(\w+)(?::\w+)?\}", "x1", route.path)
            out += [(m, path) for m in sorted((route.methods or {"GET"}) - {"HEAD"})]
    return out


async def test_a_key_answers_401_on_every_route_but_v1_sites(node):
    key = node.key()
    routes = every_route(node.api_app)
    assert len(routes) > 100 and ("POST", "/api/grants") in routes and ("GET", "/api/status") in routes
    async with node.client(key) as c:
        for method, path in routes:
            r = await c.request(method, path, json={})
            assert r.status_code == 401, (method, path, r.status_code)
        for path in ("/", "/assets/app.js", "/approvals", "/mcp"):
            assert (await c.get(path)).status_code == 401, path
        assert (await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})).status_code == 401


async def test_nothing_but_a_key_or_the_owners_session_opens_v1_sites(node):
    admin = node.grant(subject={"id": "root"}, admin=True, workspaces=["*"])
    for token in (TOKEN, node.grant(), admin):
        async with node.client(token) as c:
            for path in ("/v1/sites", READ_PATH, f"/v1/sites/{API}/openapi.json"):
                r = await c.get(path)
                assert r.status_code == 401 and r.json()["code"] == "unauthorized", (path, r.text)
    assert node.site.state["reads"] == 0


async def test_a_members_session_does_not_open_v1_sites(node):
    sid, session = node.store.create_session("m_abc123", 600, member_id="m_abc123", via="passkey")
    async with node.client(cookies={SESSION_COOKIE: sid}) as c:
        r = await c.get("/v1/sites", headers={"X-Agentos-CSRF": session.csrf})
    assert r.status_code == 401


async def test_the_device_credential_does_not_open_v1_sites_or_make_keys(node):
    """It is a cookie of the owner's, but it sits in a Keychain an agent can read: never the owner in person."""
    sid, session = node.store.create_session("owner", 600, via="device")
    async with node.client(cookies={SESSION_COOKIE: sid}) as c:
        csrf = {"X-Agentos-CSRF": session.csrf}
        r = await c.get(READ_PATH, params={"on": SURFACE}, headers=csrf)
        assert r.status_code == 401 and "not a person" in r.json()["detail"]
        made = await c.post("/api/site-keys", json={"name": "k", "apis": [API]}, headers=csrf)
        assert made.status_code == 403
    assert node.site.state["reads"] == 0


async def test_the_owners_session_opens_it_with_the_csrf_header_only(node):
    async with node.client() as c:
        csrf = await node.session(c)
        assert (await c.get(READ_PATH)).status_code == 401
        assert (await c.get(READ_PATH, headers={"X-Agentos-CSRF": "wrong"})).status_code == 401
        foreign = {**csrf, "Origin": "https://evil.example"}
        assert (await c.get(READ_PATH, headers=foreign)).status_code == 401
        r = await c.get(READ_PATH, params={"on": SURFACE}, headers=csrf)
        assert r.status_code == 200 and "ratelimit-limit" not in r.headers
        assert node.runner.get(r.json()["source"]["run"]).principal["id"] == "owner"


async def test_a_grant_token_that_happens_to_hold_the_keys_prefix_is_not_turned_away(node):
    token = node.grant()
    async with node.client(f"{token}aos_") as c:  # refused by the policy as a bad token, not here as a key
        assert (await c.get("/api/status")).json()["error"]["code"] == "unauthorized"
    async with node.client(token) as c:
        assert (await c.get("/api/status")).status_code == 200


async def test_a_key_in_the_query_string_is_refused_even_beside_a_good_header(node):
    key = node.key()
    async with node.client(key) as c:
        for params in ({"api_key": key}, {"token": key}, {"status": key}, {key: "1"}):
            r = await c.get(READ_PATH, params=params)
            assert r.status_code == 401 and key not in r.text, params
    async with node.client() as c:
        assert (await c.get(READ_PATH, params={"key": key})).status_code == 401
    assert node.site.state["reads"] == 0


def test_the_node_writes_no_access_log_so_a_query_string_is_never_logged(tmp_path):
    server = _Uvicorn.server(object(), NodeConfig(home=tmp_path, mcp_token=TOKEN, mcp_port=7400))
    assert server.config.access_log is False
    assert not logging.getLogger("uvicorn.access").hasHandlers()  # uvicorn asks this on every connection


async def test_a_key_never_reaches_a_log_or_a_stored_row(node, caplog):
    caplog.set_level(logging.DEBUG)
    row, key = node.key_row(surfaces=[SURFACE], label="billing sync")
    async with node.client(key) as c:
        await c.get(READ_PATH)
        await c.get(READ_PATH, params={"api_key": key})
        await c.get("/v1/sites/nope/nope")
    # the test client's own lines, and the access log pytest turns back on by attaching its handlers to it
    server = [r.getMessage() for r in caplog.records if not r.name.startswith(("httpx", "httpcore", "uvicorn.access"))]
    assert key not in "\n".join(server)
    home = Path(node.config.home)
    for path in [*home.rglob("*.db"), *home.rglob("*.db-wal"), *home.rglob("*.jsonl")]:
        assert key.encode() not in path.read_bytes(), path
    assert redact_text(f"saw {key} here") == "saw [redacted:site-key] here"
    assert row.hash == token_hash(key) and key not in str(row.public())


# ---- with the real service: activate, pause, remove ----

async def test_a_real_api_from_activation_to_removal(tmp_path):
    async with Node(tmp_path, install=False) as n:
        service = n.runtime.get("site_apis")
        service.save(definition(), by=OWNER)
        async with n.client() as c:
            csrf = await n.session(c)
            assert (await c.get("/v1/sites", headers=csrf)).json() == {"apis": []}  # a draft has no flows
        service.activate(API, by=OWNER)
        key = n.key(surfaces=[SURFACE])
        async with n.client(key) as c:
            listed = (await c.get("/v1/sites")).json()["apis"]
            assert [(a["api"], a["state"]) for a in listed] == [(API, "active")]
            assert (await c.get(READ_PATH)).status_code == 200
            assert (await c.get(READ_PATH)).json()["cache"]["hit"] is True
            assert n.site_http.store.cache_bytes() > 0
            service.pause(API, by=OWNER)
            assert n.site_http.store.cache_bytes() == 0  # the contract: deleted on pause
            r = await c.get(READ_PATH)
            assert r.status_code == 409 and r.json()["code"] == "paused"
            assert (await c.get("/v1/sites")).json()["apis"][0]["state"] == "paused"
            service.activate(API, by=OWNER)
            again = await c.get(READ_PATH)
            assert again.status_code == 200 and again.json()["cache"]["hit"] is False
            service.remove(API, by=OWNER)
            assert n.site_http.store.cache_bytes() == 0
            assert (await c.get(READ_PATH)).status_code == 404
            assert (await c.get("/v1/sites")).json() == {"apis": []}
