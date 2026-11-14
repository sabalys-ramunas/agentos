"""The profiles routes (agentos.api.routes_profiles): the owner in person reads and edits a profile in the
panel; a token, the owner's included, a grant and a member are refused; an agent's draft waits for the
owner's Approve here."""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from agentos.profiles.service import ProfilesService

LI = "biz-a/linkedin"
IG = "biz-a/instagram"
AGENT = {"kind": "agent", "id": "agent-1"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 64

ADA = {
    "profile": "ada", "workspace": "biz-a", "kind": "person", "display_name": "Ada Lovelace",
    "name": {"first": "Ada", "last": "Lovelace"}, "headline": "Analyst of engines",
    "bio": {"short": "Engines and notes.", "long": "Notes on the analytical engine."},
    "phone": "+370 600 00000", "email": "ada@example.com", "website": "https://ada.example",
    "voice": {"personality": "Dry and exact.", "emoji": "none"},
    "accounts": [{"surface": LI}, {"surface": IG}],
}


@pytest.fixture
def panel(tmp_path):
    from test_node_panel import Node

    (tmp_path / "node").mkdir()
    n = Node(tmp_path / "node")
    n.api_app.state.api.limiter.per_key = n.api_app.state.api.limiter.global_max = 10_000
    svc = ProfilesService(home=tmp_path / "profiles")
    n.runtime.register("profiles", svc)
    yield n
    svc.store.close()
    n.close()


def client_of(panel) -> TestClient:
    return TestClient(panel.app, base_url="http://localhost:7400")


def test_only_the_owner_in_person_reads_or_changes_profiles(panel):
    from test_node_panel import TOKEN, bearer

    grant = panel.grant()
    admin = panel.grant(admin=True)
    with client_of(panel) as client:
        assert client.get("/api/profiles").status_code == 401
        for who in (TOKEN, admin, grant):
            for method, path, body in (("GET", "/api/profiles", None), ("POST", "/api/profiles", ADA),
                                       ("GET", "/api/profiles/biz-a/ada", None),
                                       ("POST", "/api/profiles/biz-a/ada/approve", {"version": 1})):
                r = client.request(method, path, json=body, headers=bearer(who))
                assert r.status_code == 403, (who[:6], method, path, r.text)
                assert r.json()["error"]["code"] in ("denied", "presence_required") and "—" not in r.text
        assert panel.runtime.get("profiles").list() == []                        # nothing was saved
        csrf = panel.sign_in(client)
        assert client.post("/api/profiles", json=ADA).status_code == 403          # a cookie write needs the CSRF header
        r = client.post("/api/profiles", json=ADA, headers=csrf)
        assert r.status_code == 200, r.text
    denied = panel.policy.traces.query(kind="panel", status="denied", limit=100)[0]
    assert any(e.tool == "panel.profiles.list" for e in denied)


def test_without_the_service_the_list_says_so(panel):
    panel.runtime._services.pop("profiles")
    with client_of(panel) as client:
        panel.sign_in(client)
        assert client.get("/api/profiles").json() == {"available": False, "profiles": []}


def test_the_owner_saves_edits_and_attaches_live(panel):
    with client_of(panel) as client:
        csrf = panel.sign_in(client)
        saved = client.post("/api/profiles", json=ADA, headers=csrf).json()
        assert saved["state"] == "approved" and saved["version"] == 1 and saved["profile"] == "biz-a/ada"
        assert "website changes on 2 accounts" in saved["summary"]

        listed = client.get("/api/profiles?workspace=biz-a").json()
        assert listed["available"] is True
        assert listed["profiles"] == [{"profile": "biz-a/ada", "kind": "person", "display_name": "Ada Lovelace",
                                       "headline": "Analyst of engines", "accounts": [LI, IG], "version": 1,
                                       "state": "approved", "draft_waiting": None}]
        assert client.get("/api/profiles?workspace=biz-b").json()["profiles"] == []

        one = client.get("/api/profiles/biz-a/ada").json()
        assert one["definition"]["phone"] == "+37060000000" and "sites" not in one
        sites = {s["site"]: s for s in client.get("/api/profiles/sites").json()}
        assert sites["instagram"]["domain"] == "instagram.com"
        assert {"name": "bio", "source": "bio.short", "limit": 150, "people_only": False} \
            in sites["instagram"]["fields"]
        assert next(f for f in sites["linkedin"]["fields"] if f["name"] == "headline")["limit"] == 220

        r = client.patch("/api/profiles/biz-a/ada", json={"changes": {"bio": {"short": "Engines."}}}, headers=csrf)
        assert r.status_code == 200 and r.json()["version"] == 2 and r.json()["summary"] == ["bio changes on 1 account"]
        one = client.get("/api/profiles/biz-a/ada").json()["definition"]
        assert one["bio"] == {"short": "Engines.", "long": "Notes on the analytical engine."}

        r = client.patch("/api/profiles/biz-a/ada", json={"changes": {"phone": "+37060000001"}}, headers=csrf)
        assert r.json()["summary"] == ["phone changes on 2 accounts"]               # LinkedIn and Instagram show it
        r = client.patch("/api/profiles/biz-a/ada", json={"changes": {"public": ["phone"]}}, headers=csrf)
        assert r.status_code == 400 and r.json()["error"]["code"] == "profile_invalid"   # nothing is private

        bad = client.patch("/api/profiles/biz-a/ada", json={"changes": {"bio": {"short": "x" * 200}}}, headers=csrf)
        assert bad.status_code == 400 and bad.json()["error"]["code"] == "profile_invalid"
        assert "biz-a/instagram: bio has 200 characters" in bad.json()["error"]["message"]
        assert client.patch("/api/profiles/biz-a/ada", json={"changes": {}}, headers=csrf).status_code == 400
        r = client.patch("/api/profiles/biz-a/ada", json={"changes": {"workspace": "biz-b"}}, headers=csrf)
        assert r.json()["error"]["code"] == "profile_invalid"

        r = client.post("/api/profiles/biz-a/ada/detach", json={"surface": IG}, headers=csrf)
        assert r.status_code == 200 and client.get("/api/profiles").json()["profiles"][0]["accounts"] == [LI]
        r = client.post("/api/profiles/biz-a/ada/attach", json={"surface": IG, "overrides": {"bio": "IG only"}},
                        headers=csrf)
        assert r.status_code == 200 and r.json()["changes"][IG][:2] == ["name", "bio"]
        accounts = client.get("/api/profiles/biz-a/ada").json()["definition"]["accounts"]
        assert accounts[1] == {"surface": IG, "site": "instagram", "overrides": {"bio": "IG only"}, "map": {}}
        r = client.post("/api/profiles/biz-a/ada/attach", json={"surface": "biz-b/facebook"}, headers=csrf)
        assert r.status_code == 400 and "another workspace" in r.json()["error"]["message"]
        r = client.post("/api/profiles/biz-a/ada/detach", json={"surface": "biz-a/x"}, headers=csrf)
        assert r.status_code == 404
        assert "—" not in json.dumps(listed) + bad.text


def test_status_and_marking_a_manual_field_done(panel):
    with client_of(panel) as client:
        csrf = panel.sign_in(client)
        client.post("/api/profiles", json=ADA, headers=csrf)
        status = client.get("/api/profiles/biz-a/ada/status").json()
        li, ig = status["accounts"]
        assert li["surface"] == LI and li["fields"]["headline"]["state"] == "manual"
        assert li["fields"]["website"]["state"] == "manual"
        assert ig["fields"]["bio"] == {"value": "Engines and notes.", "state": "manual", "learnable": True}
        r = client.post("/api/profiles/biz-a/ada/mark", json={"surface": IG, "fields": ["bio", "name"]}, headers=csrf)
        assert r.status_code == 200 and r.json() == {"surface": IG, "marked": ["bio", "name"]}
        ig = client.get("/api/profiles/biz-a/ada/status").json()["accounts"][1]
        assert ig["fields"]["bio"]["state"] == ig["fields"]["name"]["state"] == "done_by_hand"
        r = client.post("/api/profiles/biz-a/ada/mark", json={"surface": LI, "fields": ["bio"]}, headers=csrf)
        assert r.status_code == 400 and "not a manual field" in r.json()["error"]["message"]
        r = client.post("/api/profiles/biz-a/ada/mark", json={"surface": "biz-a/x", "fields": ["bio"]}, headers=csrf)
        assert r.status_code == 404
        assert client.get("/api/profiles/biz-a/nobody/status").status_code == 404


def test_an_agents_draft_waits_and_the_owner_approves_or_rejects_it(panel):
    svc = panel.runtime.get("profiles")
    with client_of(panel) as client:
        csrf = panel.sign_in(client)
        client.post("/api/profiles", json=ADA, headers=csrf)
        svc.update("biz-a/ada", {"headline": "Poet of numbers"}, by=AGENT, as_owner=False)
        assert client.get("/api/profiles").json()["profiles"][0]["draft_waiting"] == 2
        one = client.get("/api/profiles/biz-a/ada").json()
        assert one["version"] == 1 and one["draft_waiting"] == 2
        draft = client.get("/api/profiles/biz-a/ada?version=2").json()
        assert draft["state"] == "draft" and draft["definition"]["headline"] == "Poet of numbers"
        r = client.post("/api/profiles/biz-a/ada/approve", json={"version": 2}, headers=csrf)
        assert r.status_code == 200 and r.json()["state"] == "approved" and r.json()["summary"] == [
            "headline changes on 1 account"]
        assert svc.for_surface(LI).definition.headline == "Poet of numbers"

        svc.update("biz-a/ada", {"headline": "Countess"}, by=AGENT, as_owner=False)
        assert client.post("/api/profiles/biz-a/ada/reject", json={}, headers=csrf).status_code == 400
        assert client.post("/api/profiles/biz-a/ada/approve", json={}, headers=csrf).status_code == 400
        r = client.post("/api/profiles/biz-a/ada/reject", json={"version": 3, "note": "not that"}, headers=csrf)
        assert r.status_code == 200 and r.json()["state"] == "rejected"
        assert client.get("/api/profiles/biz-a/ada").json().get("draft_waiting") is None
        # the owner's edit builds on the approved version, never on an agent's waiting draft
        svc.update("biz-a/ada", {"website": "https://evil.example"}, by=AGENT, as_owner=False)
        r = client.patch("/api/profiles/biz-a/ada", json={"changes": {"headline": "Countess"}}, headers=csrf)
        assert r.json()["state"] == "approved" and svc.for_surface(LI).definition.website == "https://ada.example"
        assert client.post("/api/profiles/biz-a/ada/approve", json={"version": "3"}, headers=csrf).status_code == 400


def test_a_photo_is_kept_set_and_served_to_the_owner_only(panel):
    from test_node_panel import TOKEN, bearer

    with client_of(panel) as client:
        csrf = panel.sign_in(client)
        client.post("/api/profiles", json=ADA, headers=csrf)
        r = client.post("/api/profiles/biz-a/ada/photo/avatar?name=me.png", content=PNG,
                        headers={**csrf, "content-type": "image/png"})
        assert r.status_code == 200, r.text
        out = r.json()
        sha = out["photo"]["photo"]
        assert sha.startswith("sha256:") and out["photo"]["type"] == "image/png" and out["version"] == 2
        assert "photo changes on 2 accounts" in out["summary"]
        assert client.get("/api/profiles/biz-a/ada").json()["definition"]["photos"] == {"avatar": sha}
        img = client.get(f"/api/profile-photos/{sha.removeprefix('sha256:')}")
        assert img.status_code == 200 and img.content == PNG and img.headers["content-type"] == "image/png"
        assert img.headers["x-content-type-options"] == "nosniff"
        assert client.get("/api/profile-photos/sha256-abc").status_code == 400
        assert client.get("/api/profile-photos/" + "0" * 64).status_code == 404
        r = client.post("/api/profiles/biz-a/ada/photo/banner", content=PNG, headers=csrf)
        assert r.status_code == 400
        r = client.post("/api/profiles/biz-a/ada/photo/cover?name=x.txt", content=b"just text", headers=csrf)
        assert r.status_code == 400 and "JPEG, PNG, WebP or GIF" in r.json()["error"]["message"]
        client.cookies.clear()                                                     # a token alone, no session
        r = client.get(f"/api/profile-photos/{sha.removeprefix('sha256:')}", headers=bearer(TOKEN))
        assert r.status_code == 403 and r.json()["error"]["code"] == "denied"


def test_a_login_saved_for_an_account_carries_its_2fa_seed(panel):
    """The Add account flow saves the login with POST /api/vault: one password entry bound to the lane, with
    its TOTP seed, which is the entry the login tool picks and types codes from."""
    import asyncio

    with client_of(panel) as client:
        csrf = panel.sign_in(client)
        r = client.post("/api/vault", headers=csrf, json={
            "name": "biz-a/instagram login", "kind": "password", "domain": "instagram.com",
            "lanes": [IG], "username": "ada", "secret": "correct-horse-battery-1", "totp": "JBSWY3DPEHPK3PXP"})
        assert r.status_code == 200, r.text
        assert "correct-horse" not in r.text and "JBSWY3DP" not in r.text
        listed = client.get("/api/vault").json()
        assert [e["lanes"] for e in listed] == [[IG]] and "correct-horse" not in json.dumps(listed)
    entries = asyncio.run(panel.runtime.get("vault").list_entries())
    assert [(e.kind.value, e.has_totp) for e in entries] == [("password", True)]
