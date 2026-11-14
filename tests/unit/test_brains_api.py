"""Panel API routes for brains (docs/API.md)."""

import json

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from testkit.brains import OAUTH, add, make_service, wait_status

from agentos.brains.api import routes
from agentos.core.models import Principal


@pytest.fixture
def svc(tmp_path):
    return make_service(tmp_path)


def client(svc):
    return TestClient(Starlette(routes=routes(svc.runtime)))


async def test_brains_list_order_and_chip(svc):
    await add(svc, "claude-max", "subscription", "anthropic")
    await add(svc, "ollama", "local", "ollama")
    c = client(svc)
    r = c.get("/api/brains")
    assert r.status_code == 200
    body = r.json()
    b = {x["name"]: x for x in body}
    assert b["claude-max"]["kind"] == "subscription" and b["claude-max"]["seats"] == {"in_use": 0, "total": 1}
    assert "window_used_pct" in b["claude-max"] and b["claude-max"]["state"] == "ready"
    assert OAUTH not in r.text
    r = c.post("/api/brains/order", json={"order": ["ollama", "claude-max"]})
    assert r.status_code == 200 and [x["name"] for x in r.json()] == ["ollama", "claude-max"]
    r = c.post("/api/brains/order", json={"order": ["nope"]})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    r = c.post("/api/brains/order", json={"order": "x"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid"
    chip = c.get("/api/brains/chip").json()
    assert chip["name"] == "claude-max" and chip["offline_ready"] is True and chip["local_ready"] is True


async def test_tasks_routes(svc):
    await add(svc, "claude-max", "subscription", "anthropic")
    r = await svc.submit(Principal(id="a"), prompt="FAKE:ok", workspace="personal")
    await wait_status(svc, r["id"])
    c = client(svc)
    tasks = c.get("/api/brains/tasks?limit=5").json()
    assert tasks[0]["id"] == r["id"] and tasks[0]["status"] == "succeeded_unverified"
    one = c.get(f"/api/brains/tasks/{r['id']}?limit=3").json()
    assert one["id"] == r["id"] and len(one["transcript"]["events"]) == 3
    assert "prompt" not in json.dumps({k: v for k, v in one.items() if k != "prompt_preview"}).replace(
        "prompt_preview", "")
    assert c.get("/api/brains/tasks/bt_000000000000").status_code == 404
    assert c.post(f"/api/brains/tasks/{r['id']}/cancel").json()["status"] == "succeeded_unverified"
    res = c.post(f"/api/brains/tasks/{r['id']}/resume")
    assert res.status_code == 400 and "only interrupted" in res.json()["error"]["message"]
