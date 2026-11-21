"""Panel API: every resource route against the in-memory fakes."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

import openajar
from openajar.api import ApiSettings, PanelStore, build_api
from openajar.api.lanes_view import workspace_health
from openajar.core.config import NodeConfig, load_proxies
from openajar.core.runtime import Runtime
from openajar.dev.apifakes import demo_runtime

H = {"Authorization": "Bearer owner"}


@pytest.fixture
def rt():
    return demo_runtime()


@pytest.fixture
def store():
    return PanelStore(None)


@pytest.fixture
def client(rt, store):
    return TestClient(build_api(rt, ApiSettings(store=store, owner_token="owner")))


@pytest.fixture
def person(client, store) -> dict:
    """The owner signed in to the web app (a one-time code): approving, answering and standing approvals
    need a person, which a bearer token never is, the node token included (docs/THREAT-MODEL.md)."""
    store.add_code("code-for-the-routes-tests")
    r = client.post("/api/session", json={"code": "code-for-the-routes-tests"})
    assert r.status_code == 200, r.text
    return {"X-Agentos-CSRF": r.json()["csrf"]}


def test_status(client, rt):
    s = client.get("/api/status", headers=H).json()
    assert s["version"] == openajar.__version__ and s["node"] == "node-1"
    # 1 pending approval + 1 lane with a captcha
    assert s["needs_you"] == 2
    # the older parts stay as they were; the web's extra parts (failed outcomes, stopped runs, plans,
    # agent login requests) are added beside them and are empty here
    assert s["needs_you_detail"] == {"approvals": 1, "questions": 0, "codes": 0, "lanes": ["biz-b/reddit"],
                                     "hands": ["biz-b/reddit"], "outcomes": 0, "runs": 0, "logins": []}
    assert s["needs_you_all"] == 2
    assert s["cost_today"] == {"tokens": 1200, "usd": 0.01}
    assert s["brain"] == {"name": "claude-max", "state": "ready", "offline_ready": True}
    assert s["nodes"] == [{"id": "node-1", "state": "up", "degraded": False}]
    assert s["lanes"]["total"] == 13 and s["lanes"]["warm"] == 1
    assert "lanes" in s["services"]


def test_workspaces_group_lanes_with_one_line_health(client):
    ws = {w["id"]: w for w in client.get("/api/workspaces", headers=H).json()}
    assert set(ws) == {"biz-a", "biz-b", "personal"}
    assert ws["biz-a"]["health"] == "attention"  # a post waits for approval
    assert ws["biz-a"]["summary"] == "6 lanes, 1 waiting for approval"
    assert ws["biz-b"]["health"] == "attention" and ws["biz-b"]["summary"] == "6 lanes, 1 captcha"
    assert ws["personal"]["health"] == "idle" and ws["personal"]["summary"] == "1 lane, sleeping"
    lane = next(lane for lane in ws["biz-a"]["lanes"] if lane["name"] == "instagram")
    assert lane["proxy"] == {"label": "lt1", "host": "203.0.113.10", "port": 12323}
    assert lane["viewer"]["ws_path"] == "/api/lanes/biz-a/instagram/vnc"
    assert lane["preset"] == "account" and lane["signal"] == "ok"
    shell = ws["personal"]["lanes"][0]
    assert shell["terminal"] == {"ws_path": "/api/lanes/personal/shell/terminal"}
    assert "password" not in str(ws)


def test_workspace_health_words():
    ok = {"state": "warm", "signal": "ok"}
    cold = {"state": "cold", "signal": "ok"}
    assert workspace_health([]) == ("idle", "No lanes yet")
    assert workspace_health([ok, cold]) == ("ok", "2 lanes, all ok, 1 open")
    assert workspace_health([{"state": "quarantined", "signal": "ok"}, ok])[0] == "blocked"
    h, s = workspace_health([{"state": "warm", "signal": "logged_out"}, {"state": "warm", "signal": "captcha"}])
    assert h == "attention" and s == "2 lanes, 1 logged out, 1 captcha"


def test_workspace_create_and_conflict(client):
    r = client.post("/api/workspaces", headers=H, json={"id": "biz-c", "name": "Biz C", "color": "#123456"})
    assert r.status_code == 200 and r.json()["summary"] == "No lanes yet"
    assert client.post("/api/workspaces", headers=H, json={"id": "biz-c", "name": "again"}).status_code == 409
    assert client.post("/api/workspaces", headers=H, json={"id": "Bad Id", "name": "x"}).status_code == 400
    ids = [w["id"] for w in client.get("/api/workspaces", headers=H).json()]
    assert "biz-c" in ids


def test_templates_apply_creates_six_per_company(client, rt, person):
    assert {t["id"] for t in client.get("/api/templates", headers=H).json()} >= {"social-6"}
    body = {"template": "social-6", "workspaces": ["biz-c", "biz-d", "biz-a"], "proxy_per_workspace": {"biz-c": "lt1"}}
    # lt1 is already biz-a's exit: new lanes behind it are a person's call, never the node token's
    assert client.post("/api/templates/apply", headers=H, json=body).status_code == 403
    r = client.post("/api/templates/apply", headers=person, json=body)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["created"]) == 12 and len(body["skipped"]) == 6
    assert body["created"][0]["proxy"]["label"] == "lt1"
    assert body["created"][0]["logged_in_hint"] == "needs_login"
    bad = client.post("/api/templates/apply", headers=H, json={
        "template": "social-6", "workspaces": ["biz-e"], "proxy_per_workspace": {"biz-e": "nope"}})
    assert bad.status_code == 400
    assert client.post("/api/templates/apply", headers=H,
                       json={"template": "nope", "workspaces": ["x"]}).status_code == 400


def test_lane_get_create_open_close(client, person):
    assert client.get("/api/lanes", headers=H).status_code == 200
    lane = client.get("/api/lanes/biz-a/instagram", headers=H).json()
    assert lane["id"] == "biz-a/instagram" and lane["state"] == "warm"
    assert client.get("/api/lanes/biz-a/nope", headers=H).json()["error"]["code"] == "lane_not_found"
    assert client.get("/api/lanes/BAD/x", headers=H).status_code == 404

    shared = {"id": "biz-z/tiktok", "kind": "browser", "proxy_label": "lt1"}  # lt1 is biz-a's exit already
    assert client.post("/api/lanes", headers=H, json=shared).json()["error"]["code"] == "presence_required"
    r = client.post("/api/lanes", headers=person, json=shared)
    assert r.status_code == 200 and r.json()["workspace"] == "biz-z"
    dup = client.post("/api/lanes", headers=H, json={"id": "biz-z/tiktok"})
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "lane_exists"
    assert client.post("/api/lanes", headers=H, json={"id": "no-slash"}).status_code == 400
    assert client.post("/api/lanes", headers=H, json={"id": "biz-z/x", "proxy_label": "zz"}).status_code == 400

    opened = client.post("/api/lanes/biz-z/tiktok/open", headers=H).json()
    assert opened["state"] == "warm" and opened["viewer"]["ws_path"].endswith("/vnc")
    closed = client.post("/api/lanes/biz-z/tiktok/close", headers=H).json()
    assert closed["state"] == "cold" and closed["viewer"] is None


def test_screenshot_only_for_warm_lanes(client):
    r = client.get("/api/lanes/biz-a/instagram/screenshot", headers=H)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:4] == b"\x89PNG"
    cold = client.get("/api/lanes/biz-a/facebook/screenshot", headers=H)
    assert cold.status_code == 409 and cold.json()["error"]["code"] == "lane_cold"


def test_approvals_list_approve_deny_and_hash_binding(client, rt, person):
    a2 = rt.approvals.add("biz-b/facebook", text="Second post")
    a3 = rt.approvals.add("biz-b/medium", effect="irreversible", text="Delete draft")
    pending = client.get("/api/approvals?state=pending", headers=H).json()
    assert len(pending) == 3
    assert client.get("/api/approvals?state=weird", headers=H).status_code == 400
    assert client.get("/api/approvals?limit=0", headers=H).status_code == 400

    token = client.post(f"/api/approvals/{a2['id']}/approve", headers=H, json={})
    assert token.status_code == 403 and token.json()["error"]["code"] == "presence_required"
    stale = client.post(f"/api/approvals/{a2['id']}/approve", headers=person, json={"content_hash": "sha256:other"})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "conflict"

    ok = client.post(f"/api/approvals/{a2['id']}/approve", headers=person,
                     json={"content_hash": a2["content_hash"], "note": "looks good"})
    assert ok.status_code == 200 and ok.json()["state"] == "approved" and ok.json()["decided_by"] == "owner"
    again = client.post(f"/api/approvals/{a2['id']}/approve", headers=person, json={})
    assert again.status_code == 409

    denied = client.post(f"/api/approvals/{a3['id']}/deny", headers=H, json={})
    assert denied.json()["state"] == "denied"
    assert client.get(f"/api/approvals/{a3['id']}", headers=H).json()["state"] == "denied"
    assert client.get("/api/approvals/ap_999", headers=H).status_code == 404
    png = client.get(f"/api/approvals/{a2['id']}/preview.png", headers=H)
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"


def test_approvals_batch_reports_each(client, rt, person):
    first = rt.approvals.list(state="pending")[0]["id"]
    second = rt.approvals.add("biz-b/blog", text="x")["id"]
    token = client.post("/api/approvals/batch", headers=H, json={"ids": [first], "decision": "approve"})
    assert token.status_code == 403 and token.json()["error"]["code"] == "presence_required"
    r = client.post("/api/approvals/batch", headers=person,
                    json={"ids": [first, second, "ap_404", first], "decision": "approve"})
    body = r.json()
    assert r.status_code == 200 and body["ok"] is False
    assert [x["ok"] for x in body["results"]] == [True, True, False]
    assert body["results"][2]["error"]["code"] == "not_found"


def test_standing_approvals(client, person):
    body = {"lane": "biz-a/instagram", "effect": "commit", "ttl_seconds": 7 * 86400}
    token = client.post("/api/approvals/standing", headers=H, json=body)  # the node token is not a person
    assert token.status_code == 403 and token.json()["error"]["code"] == "presence_required"
    created = client.post("/api/approvals/standing", headers=person, json=body).json()
    assert created["lane"] == "biz-a/instagram"
    assert len(client.get("/api/approvals/standing", headers=H).json()) == 1
    assert client.delete(f"/api/approvals/standing/{created['id']}", headers=H).status_code == 200
    assert client.get("/api/approvals/standing", headers=H).json() == []
    assert client.post("/api/approvals/standing", headers=person,
                       json={"lane": "biz-a/instagram", "effect": "pay", "ttl_seconds": 3600}).status_code == 400


def test_questions_list_open_and_answer_once(client, rt, person):
    q = rt.approvals.ask("Post the rye photo at 9?", options=["yes", "no"])
    asked = client.get("/api/questions?state=open", headers=H).json()
    assert [x["id"] for x in asked] == [q["id"]] and client.get("/api/questions", headers=H).json() == asked
    assert asked[0] == {"id": q["id"], "state": "open", "lane": None, "question": "Post the rye photo at 9?",
                        "options": ["yes", "no"], "answer": None,
                        "asked_by": {"kind": "agent", "id": "claude-code", "thread": None},
                        "created_at": q["created_at"], "expires_at": q["expires_at"],
                        "answered_at": None, "answered_by": None}
    assert client.get("/api/questions?state=weird", headers=H).status_code == 400
    assert client.post(f"/api/questions/{q['id']}/answer", headers=person, json={"answer": ""}).status_code == 400
    assert client.post(f"/api/questions/{q['id']}/answer", headers=person, json={"answer": "maybe"}).status_code == 400
    ok = client.post(f"/api/questions/{q['id']}/answer", headers=person, json={"answer": "yes"})
    assert ok.status_code == 200 and ok.json()["state"] == "answered" and ok.json()["answer"] == "yes"
    assert ok.json()["answered_by"] == "owner"
    assert client.get("/api/questions", headers=H).json() == []
    assert client.get("/api/questions?state=answered", headers=H).json()[0]["answer"] == "yes"
    assert client.post(f"/api/questions/{q['id']}/answer", headers=person, json={"answer": "no"}).status_code == 409
    action = rt.approvals.add("biz-a/instagram")
    assert client.post(f"/api/questions/{action['id']}/answer", headers=person, json={"answer": "x"}).status_code == 404


def test_status_counts_open_questions_with_the_approvals(client, rt):
    rt.approvals.ask("Ready?")
    s = client.get("/api/status", headers=H).json()
    assert s["needs_you"] == 3 and s["needs_you_detail"] == {"approvals": 1, "questions": 1, "codes": 0,
                                                       "lanes": ["biz-b/reddit"], "hands": ["biz-b/reddit"],
                                                       "outcomes": 0, "runs": 0, "logins": []}


def test_activity_and_runs(client, rt):
    rt.traces.add(lane="biz-b/reddit", kind="signal", summary="captcha seen")
    all_ = client.get("/api/activity", headers=H).json()
    assert len(all_) == 2 and all_[0]["summary"] == "captcha seen"
    assert len(client.get("/api/activity?workspace=biz-a", headers=H).json()) == 1
    assert len(client.get("/api/activity?kind=signal&limit=5", headers=H).json()) == 1

    runs = client.get("/api/runs", headers=H).json()
    assert runs[0]["status"] == "succeeded_verified" and runs[0]["proof"]["url"].startswith("https://")
    assert client.get("/api/runs?status=done", headers=H).status_code == 400
    assert client.get(f"/api/runs/{runs[0]['id']}", headers=H).json()["steps"] == []
    assert client.get("/api/runs/run_404", headers=H).status_code == 404


def test_flows_and_schedules(client, person):
    flows = client.get("/api/flows", headers=H).json()
    assert flows[0]["params"]["required"] == ["caption"]
    run = client.post(f"/api/flows/{flows[0]['id']}/run", headers=H, json={"params": {"caption": "hi"}}).json()
    assert run["status"] == "queued" and run["params"] == {"caption": "hi"}
    assert client.post("/api/flows/nope/run", headers=H, json={}).status_code == 404

    body = {"flow": flows[0]["id"], "cron": "0 9 * * 1-5", "params": {}, "deadman_minutes": 90}
    # it would run as the owner and never end: the owner in person, never the node token
    assert client.post("/api/schedules", headers=H, json=body).json()["error"]["code"] == "presence_required"
    sc = client.post("/api/schedules", headers=person, json=body)
    assert sc.status_code == 200 and sc.json()["created_by"] == "owner"
    assert client.post("/api/schedules", headers=person, json={"flow": "x"}).status_code == 400
    assert client.post("/api/schedules", headers=person, json={"flow": "x", "cron": "* * * * *",
                                                               "watch": "https://x"}).status_code == 400
    assert len(client.get("/api/schedules", headers=H).json()) == 1
    assert client.delete(f"/api/schedules/{sc.json()['id']}", headers=H).status_code == 200
    assert client.delete("/api/schedules/sc_404", headers=H).status_code == 404


def test_schedules_toggle_with_patch_and_publish_once(client, rt, person):
    sc = client.post("/api/schedules", headers=person,
                     json={"flow": "biz-a.instagram.post", "cron": "0 9 * * 1-5"}).json()
    off = client.patch(f"/api/schedules/{sc['id']}", headers=H, json={"enabled": False})
    assert off.status_code == 200 and off.json()["enabled"] is False and off.json()["next_fire_at"] is None
    assert off.json()["grant_id"] == sc["grant_id"] == "owner"  # toggling keeps the grant it runs under
    assert client.patch(f"/api/schedules/{sc['id']}", headers=H, json={}).status_code == 400
    assert client.patch(f"/api/schedules/{sc['id']}", headers=H, json={"enabled": "maybe"}).status_code == 400
    assert client.patch("/api/schedules/sc_404", headers=H, json={"enabled": True}).status_code == 404
    assert client.get(f"/api/schedules/{sc['id']}", headers=H).json()["enabled"] is False
    assert client.get("/api/schedules/sc_404", headers=H).status_code == 404
    assert [e.data["enabled"] for e in rt.events.since(0) if e.kind == "schedule"] == [True, False]
    assert client.delete(f"/api/schedules/{sc['id']}", headers=H).status_code == 200
    gone = rt.events.since(0)[-1]
    assert gone.kind == "schedule" and gone.data["deleted"] is True and gone.data["id"] == sc["id"]


def test_nodes_default_to_this_node():
    rt = Runtime(config=NodeConfig(name="node-9", max_warm_lanes=3))
    client = TestClient(build_api(rt, ApiSettings(store=PanelStore(None), owner_token="owner")))
    nodes = client.get("/api/nodes", headers=H).json()
    assert nodes[0]["id"] == "node-9" and nodes[0]["max_warm_lanes"] == 3 and nodes[0]["state"] == "up"


def test_brains(client):
    assert client.get("/api/brains", headers=H).json()[0]["name"] == "claude-max"
    order = ["ollama-local", "claude-max", "anthropic-api"]
    r = client.post("/api/brains/order", headers=H, json={"order": order})
    assert [b["name"] for b in r.json()] == order
    assert client.post("/api/brains/order", headers=H, json={"order": ["x"]}).status_code == 400
    assert client.get("/api/brains/tasks", headers=H).json()[0]["id"] == "bt_1"
    assert client.get("/api/brains/tasks/bt_1", headers=H).json()["status"] == "running"


def test_vault_names_only(client, rt):
    r = client.post("/api/vault", headers=H, json={
        "name": "biz-a instagram", "kind": "password", "domain": "instagram.com",
        "lanes": ["biz-a/instagram"], "username": "biz.a", "secret": "hunter2-secret"})
    assert r.status_code == 200
    assert "hunter2" not in r.text and "secret" not in r.json()
    listed = client.get("/api/vault", headers=H)
    assert "hunter2" not in listed.text and "owner@example.com" not in listed.text
    assert set(listed.json()[0]) == {"id", "name", "kind", "domain", "lanes", "created_at", "last_used"}
    assert rt.vault.secrets[r.json()["id"]] == "hunter2-secret"  # it did reach the vault
    assert client.delete(f"/api/vault/{r.json()['id']}", headers=H).status_code == 200
    assert client.post("/api/vault", headers=H, json={"name": "x", "kind": "password", "domain": "d",
                                                      "lanes": ["bad"], "secret": "s"}).status_code == 400


def test_grants_token_shown_once(client):
    r = client.post("/api/grants", headers=H, json={
        "subject": "codex-worker", "workspaces": ["biz-a"], "ceiling": "draft", "specials": ["brain"], "ttl": "7d"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"].startswith("agt_") and "token" not in body["grant"]
    listed = client.get("/api/grants", headers=H)
    assert body["token"] not in listed.text and listed.json()[0]["subject"] == "codex-worker"
    revoked = client.post(f"/api/grants/{body['id']}/revoke", headers=H).json()
    assert revoked["revoked"] is True and "token" not in revoked
    for bad in ({"ceiling": "god"}, {"specials": ["root"]}, {"ttl": "forever"}):
        payload = {"subject": "x", "workspaces": ["biz-a"], **bad}
        assert client.post("/api/grants", headers=H, json=payload).status_code == 400


def test_proxies_public_fields_only(client, rt):
    listed = client.get("/api/proxies", headers=H).json()
    assert listed == [{"label": "lt1", "host": "203.0.113.10", "port": 12323}]
    r = client.post("/api/proxies", headers=H, json={"label": "de1", "host": "10.0.0.2", "port": 8080,
                                                     "username": "u", "password": "pw-secret"})
    assert r.json() == {"label": "de1", "host": "10.0.0.2", "port": 8080}
    assert rt.proxies.items["de1"].password == "pw-secret"


def test_pointing_a_proxy_label_elsewhere_needs_a_confirm(client, rt, person):
    moved = {"label": "lt1", "host": "198.51.100.7", "port": 12323, "username": "u", "password": "p"}
    r = client.post("/api/proxies", headers=H, json=moved)  # the node token is no person
    assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"
    r = client.post("/api/proxies", headers=person, json=moved)
    assert r.status_code == 409 and r.json()["error"]["code"] == "confirm_required"
    assert "new IP" in r.json()["error"]["message"] and rt.proxies.items["lt1"].host == "203.0.113.10"
    same = {**moved, "host": "203.0.113.10", "password": "rotated"}  # a new password is the same exit
    assert client.post("/api/proxies", headers=person, json=same).status_code == 200
    assert client.post("/api/proxies", headers=person, json={**moved, "confirm": True}).status_code == 200
    assert rt.proxies.items["lt1"].host == "198.51.100.7"


def test_proxies_fall_back_to_config_file(tmp_path):
    cfg = NodeConfig(home=tmp_path)
    client = TestClient(build_api(Runtime(config=cfg), ApiSettings(store=PanelStore(None), owner_token="owner")))
    r = client.post("/api/proxies", headers=H, json={"label": "lt2", "host": "1.2.3.4", "port": 1, "password": "p"})
    assert r.status_code == 200
    assert load_proxies(cfg)["lt2"].password == "p"
    assert client.get("/api/proxies", headers=H).json() == [{"label": "lt2", "host": "1.2.3.4", "port": 1}]


def test_notifications(client, rt):
    s = client.get("/api/settings/notifications", headers=H)
    assert "s3cret" not in s.text and s.json()["channels"][0]["kind"] == "ntfy"
    put = client.put("/api/settings/notifications", headers=H, json={"quiet_hours": "22-07"})
    assert put.json()["quiet_hours"] == "22-07"
    t = client.post("/api/settings/notifications/test", headers=H).json()
    assert t["ok"] is True and rt.notifier.tested == 1


def test_status_and_nodes_name_the_node_timezone(rt):
    client = TestClient(build_api(rt, ApiSettings(store=PanelStore(None), owner_token="owner",
                                                  timezone="Europe/Vilnius")))
    assert client.get("/api/status", headers=H).json()["timezone"] == "Europe/Vilnius"
    assert client.get("/api/nodes", headers=H).json()[0]["timezone"] == "Europe/Vilnius"
    with pytest.raises(ValueError):
        build_api(rt, ApiSettings(store=PanelStore(None), owner_token="owner", timezone="Mars/Olympus"))


def test_host_timezone_comes_from_tz_then_the_clock(monkeypatch):
    from openajar.core import zone

    monkeypatch.setenv("TZ", ":Europe/Paris")
    assert zone.host_zone() == "Europe/Paris"
    monkeypatch.setenv("TZ", "Nowhere/Land")  # not a zone: the host's own setting answers instead
    assert zone.valid_zone(zone.host_zone()) == zone.host_zone()
    monkeypatch.setattr(zone, "_from_localtime", lambda: None)
    monkeypatch.setattr(zone, "_from_etc_timezone", lambda: None)
    assert zone.host_zone() == "UTC"
