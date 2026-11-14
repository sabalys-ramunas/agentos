"""The owner's device credential (`POST /api/session/device`): made only with the node token, used
as the session cookie with the CSRF header, never as a bearer token."""

from __future__ import annotations

from starlette.testclient import TestClient

from agentos.api import ApiSettings, PanelStore, build_api
from agentos.api.auth import SESSION_COOKIE
from agentos.api.fakes import demo_runtime

OWNER = "owner-token-abc"
EM_DASH = "—"


def make():
    store = PanelStore(None)
    app = build_api(demo_runtime(), ApiSettings(store=store, owner_token=OWNER))
    return TestClient(app), store


def mint(client: TestClient, token: str = OWNER, **body):
    return client.post("/api/session/device", json={"label": "macbook", **body},
                       headers={"Authorization": f"Bearer {token}"})


def test_the_node_token_mints_a_long_lived_owner_device_session():
    client, store = make()
    r = mint(client)
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(out) == {"session", "csrf", "id", "label", "expires_at"} and out["label"] == "macbook"
    assert "set-cookie" not in r.headers  # the CLI keeps it; no browser cookie is set
    session = store.get_session(out["session"])
    assert session is not None and session.via == "device" and session.member_id == "owner"
    assert session.expires_at - session.created_at == 90 * 86400

    device = TestClient(client.app)
    device.cookies.set(SESSION_COOKIE, out["session"])
    status = device.get("/api/status").json()
    assert status["principal"] == {"kind": "human", "id": "owner", "owner": True} and status["csrf"] == out["csrf"]
    # writes need the CSRF header, like any session
    assert device.post("/api/lanes/biz-a/instagram/open").status_code == 403
    assert device.post("/api/lanes/biz-a/instagram/open", headers={"X-Agentos-CSRF": out["csrf"]}).status_code == 200
    # it is never a bearer token
    bearer = TestClient(client.app).get("/api/status", headers={"Authorization": f"Bearer {out['session']}"})
    assert bearer.status_code == 401
    # ending it (DELETE /api/session) revokes it
    assert device.delete("/api/session", headers={"X-Agentos-CSRF": out["csrf"]}).status_code == 200
    assert store.get_session(out["session"]) is None


def test_only_the_node_token_may_mint_one():
    client, _ = make()
    assert mint(client, "nope").status_code == 401
    # a policy grant (the fake's panel token) is refused, even with the owner's rights
    refused = mint(client, "panel-token-123")
    assert refused.status_code == 403 and EM_DASH not in refused.json()["error"]["message"]
    # a signed-in session cannot make another one either
    out = mint(client).json()
    device = TestClient(client.app)
    device.cookies.set(SESSION_COOKIE, out["session"])
    again = device.post("/api/session/device", json={"label": "x"}, headers={"X-Agentos-CSRF": out["csrf"]})
    assert again.status_code == 403


def test_ttl_is_capped_and_labels_are_checked():
    client, store = make()
    out = mint(client, ttl_s=10 * 366 * 86400).json()
    session = store.get_session(out["session"])
    assert session is not None and session.expires_at - session.created_at == 366 * 86400
    assert mint(client, label="").status_code == 400
    assert mint(client, label="<script>").status_code == 400
    assert mint(client, ttl_s=60).status_code == 400
