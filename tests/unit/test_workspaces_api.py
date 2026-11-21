"""Panel API for workspaces: status codes, error shape, plan and apply, the queue and readiness."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from testkit.workspaces import Harness

from openajar.workspaces.api import routes


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path / "home")
    yield harness
    harness.close()


@pytest.fixture
def client(h):
    app = Starlette(routes=routes(h.runtime))
    return TestClient(app)


def error(response):
    body = response.json()
    assert set(body) == {"error"} and {"code", "message"} <= set(body["error"]) <= {"code", "message", "fix"}, body
    return body["error"]


def test_create_list_show_and_patch_a_workspace(client):
    assert client.get("/api/workspaces").json() == []
    created = client.post("/api/workspaces", json={"id": "acme", "name": "Acme", "color": "#4f7cff"})
    assert created.status_code == 201 and created.json()["summary"] == "no lanes yet"
    assert [w["id"] for w in client.get("/api/workspaces").json()] == ["acme"]
    assert client.get("/api/workspaces/acme").json()["name"] == "Acme"
    patched = client.patch("/api/workspaces/acme", json={"notes": "main brand", "default_proxy": None})
    assert patched.status_code == 200 and patched.json()["notes"] == "main brand"


def test_error_codes_follow_the_documented_shape(client):
    assert client.post("/api/workspaces", json={"id": "acme"}).status_code == 201
    again = client.post("/api/workspaces", json={"id": "acme"})
    assert again.status_code == 409 and error(again)["code"] == "conflict"
    bad_id = client.post("/api/workspaces", json={"id": "Acme Ltd"})
    assert bad_id.status_code == 400 and error(bad_id)["code"] == "invalid"
    missing = client.get("/api/workspaces/ghost")
    assert missing.status_code == 404 and error(missing)["code"] == "not_found"
    unknown_proxy = client.post("/api/workspaces", json={"id": "beta", "default_proxy": "lt9"})
    assert unknown_proxy.status_code == 400 and "proxy 'lt9' is not configured" in error(unknown_proxy)["message"]
    unknown_field = client.patch("/api/workspaces/acme", json={"id": "other"})
    assert unknown_field.status_code == 400
    not_json = client.post("/api/workspaces", content=b"not json", headers={"content-type": "application/json"})
    assert not_json.status_code == 400 and error(not_json)["message"] == "the request body must be JSON"


def test_delete_is_refused_with_lanes_and_works_when_empty(client, h):
    client.post("/api/workspaces", json={"id": "acme"})
    h.lanes.add("acme/reddit")
    refused = client.delete("/api/workspaces/acme")
    assert refused.status_code == 409 and error(refused)["code"] == "conflict"
    h.lanes.lanes.clear()
    assert client.delete("/api/workspaces/acme").json() == {"deleted": "acme"}


def test_templates_list_the_builtins_and_report_bad_files(client, h):
    (h.home / "templates").mkdir()
    (h.home / "templates" / "broken.toml").write_text("[[surfaces\n", encoding="utf-8")
    body = client.get("/api/templates").json()
    assert [t["id"] for t in body["templates"]][:2] == ["personal", "social-6"]
    assert body["problems"][0]["file"] == "broken.toml" and body["problems"][0]["template"] == "broken"


def test_plan_is_read_only_and_shows_conflicts(client, h):
    client.post("/api/workspaces", json={"id": "acme"})
    client.post("/api/workspaces", json={"id": "beta"})
    body = {"template": "social-6", "workspaces": ["acme"], "proxy_per_workspace": {"acme": "lt1"}}
    plan = client.post("/api/templates/plan", json=body)
    assert plan.status_code == 200 and plan.json()["ok"] is True and plan.json()["counts"]["create"] == 6
    assert h.lanes.created == []
    h.lanes.add("acme/instagram", proxy="lt1")
    clash = client.post("/api/templates/plan", json={"template": "social-6", "workspaces": ["beta"],
                                                     "proxy_per_workspace": {"beta": "lt1"}})
    assert clash.status_code == 200 and clash.json()["ok"] is False
    assert clash.json()["counts"]["conflict"] == 6


def test_plan_errors_say_what_is_wrong(client):
    client.post("/api/workspaces", json={"id": "acme"})
    unknown = client.post("/api/templates/plan", json={"template": "ghost", "workspaces": ["acme"]})
    assert unknown.status_code == 404 and error(unknown)["code"] == "not_found"
    no_ws = client.post("/api/templates/plan", json={"template": "social-6", "workspaces": ["ghost"]})
    assert no_ws.status_code == 404 and "no workspace 'ghost'" in error(no_ws)["message"]
    bad_flag = client.post("/api/templates/plan", json={"template": "social-6", "workspaces": ["acme"],
                                                       "override_proxy_rules": "yes"})
    assert bad_flag.status_code == 400 and "must be true or false" in error(bad_flag)["message"]
    missing_list = client.post("/api/templates/plan", json={"template": "social-6"})
    assert missing_list.status_code == 400


def test_apply_creates_and_a_second_apply_is_complete_with_nothing_new(client, h):
    client.post("/api/workspaces", json={"id": "acme"})
    body = {"template": "social-6", "workspaces": ["acme"], "proxy_per_workspace": {"acme": "lt1"}}
    first = client.post("/api/templates/apply", json=body)
    assert first.status_code == 200 and first.json()["status"] == "complete"
    assert len(first.json()["created"]) == 6
    second = client.post("/api/templates/apply", json=body).json()
    assert second["status"] == "complete" and second["created"] == [] and len(second["existing"]) == 6


def test_apply_refused_on_a_conflict_is_409_and_creates_nothing(client, h):
    client.post("/api/workspaces", json={"id": "acme"})
    client.post("/api/workspaces", json={"id": "beta"})
    client.post("/api/templates/apply", json={"template": "social-6", "workspaces": ["acme"],
                                              "proxy_per_workspace": {"acme": "lt1"}})
    before = len(h.lanes.created)
    refused = client.post("/api/templates/apply", json={"template": "social-6", "workspaces": ["beta"],
                                                        "proxy_per_workspace": {"beta": "lt1"}})
    assert refused.status_code == 409 and error(refused)["code"] == "conflict"
    assert "6 conflicts, nothing was created" in error(refused)["message"]
    assert len(h.lanes.created) == before


def test_an_override_is_applied_only_with_a_reason_and_is_listed(client):
    client.post("/api/workspaces", json={"id": "acme"})
    client.post("/api/workspaces", json={"id": "beta"})
    client.post("/api/templates/apply", json={"template": "social-6", "workspaces": ["acme"],
                                              "proxy_per_workspace": {"acme": "lt1"}})
    no_reason = client.post("/api/templates/apply", json={
        "template": "social-6", "workspaces": ["beta"], "proxy_per_workspace": {"beta": "lt1"},
        "override_proxy_rules": True})
    assert no_reason.status_code == 400 and "needs a reason" in error(no_reason)["message"]
    done = client.post("/api/templates/apply", json={
        "template": "social-6", "workspaces": ["beta"], "proxy_per_workspace": {"beta": "lt1"},
        "override_proxy_rules": True, "reason": "same owner, two brands"})
    assert done.status_code == 200 and len(done.json()["overridden"]) == 6
    listed = client.get("/api/overrides?limit=3").json()
    assert len(listed) == 3 and listed[0]["reason"] == "same owner, two brands" and listed[0]["by"] == "owner"


def test_a_partial_apply_is_200_with_status_partial_and_what_is_left(client, h):
    client.post("/api/workspaces", json={"id": "acme"})
    h.lanes.fail_on = {"acme/reddit"}
    out = client.post("/api/templates/apply", json={"template": "social-6", "workspaces": ["acme"],
                                                    "proxy_per_workspace": {"acme": "lt1"}}).json()
    assert out["status"] == "partial" and out["created"] == ["acme/instagram", "acme/facebook"]
    assert out["failed"]["lane"] == "acme/reddit" and out["failed"]["code"] == "engine_error"
    assert out["remaining"][0] == "acme/reddit"


def test_the_queue_and_readiness_routes(client, h):
    client.post("/api/workspaces", json={"id": "acme"})
    client.post("/api/templates/apply", json={"template": "social-6", "workspaces": ["acme"],
                                              "proxy_per_workspace": {"acme": "lt1"}})
    queue = client.get("/api/workspaces/acme/queue").json()
    assert queue["total"] == 6 and queue["ready"] == 0 and len(queue["lanes"]) == 6
    assert client.get("/api/queue").json()["summary"] == "0 of 6 ready"

    ready = client.post("/api/lanes/acme/instagram/readiness", json={"state": "ready"})
    assert ready.status_code == 200 and ready.json()["readiness"] == "ready"
    assert ready.json()["updated_by"] == "owner"
    assert client.get("/api/workspaces/acme/queue").json()["ready"] == 1

    attention = client.post("/api/lanes/acme/reddit/readiness",
                            json={"state": "needs_attention", "note": "phone code asked"})
    assert attention.json()["note"] == "phone code asked"
    queue = client.get("/api/workspaces/acme/queue").json()
    assert queue["lanes"][0]["name"] == "reddit" and queue["needs_attention"] == 1


def test_readiness_errors(client, h):
    h.lanes.add("acme/reddit")
    missing = client.post("/api/lanes/acme/ghost/readiness", json={"state": "ready"})
    assert missing.status_code == 404 and error(missing)["code"] == "lane_not_found"
    no_note = client.post("/api/lanes/acme/reddit/readiness", json={"state": "needs_attention"})
    assert no_note.status_code == 400 and "needs a note" in error(no_note)["message"]
    bad_state = client.post("/api/lanes/acme/reddit/readiness", json={"state": "logged_in"})
    assert bad_state.status_code == 400 and "state must be one of" in error(bad_state)["message"]


def test_the_route_table_has_every_documented_path(h):
    served = {(route.path, method) for route in routes(h.runtime) for method in route.methods or ()}
    documented = {("/api/workspaces", "GET"), ("/api/workspaces", "POST"), ("/api/workspaces/{id}", "GET"),
                  ("/api/workspaces/{id}", "PATCH"), ("/api/workspaces/{id}", "DELETE"),
                  ("/api/workspaces/{id}/queue", "GET"), ("/api/queue", "GET"),
                  ("/api/lanes/{workspace}/{name}/readiness", "POST"), ("/api/templates", "GET"),
                  ("/api/templates/plan", "POST"), ("/api/templates/apply", "POST"), ("/api/overrides", "GET")}
    assert documented <= served
