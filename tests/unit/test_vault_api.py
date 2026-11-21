"""Panel API routes: names only out, secrets write-only in, clean errors."""

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient
from test_vault_support import PASSWORD, SEED, USERNAME, make_vault, private_home

from openajar.core.runtime import Runtime
from openajar.vault.api import routes

PHRASE = "a long test passphrase"


def client_for(vault):
    app = Starlette(routes=[Mount("/api", routes=routes(Runtime(vault=vault)))])
    return TestClient(app)


@pytest.fixture
def client(tmp_path):
    return client_for(make_vault(private_home(tmp_path)))


def body(**kw):
    base = {"name": "ig", "kind": "password", "domain": "instagram.com", "lanes": ["biz-a/instagram"],
            "username": USERNAME, "secret": PASSWORD}
    base.update(kw)
    return base


def clean(resp):
    for s in (PASSWORD, USERNAME, SEED, PHRASE):
        assert s not in resp.text
    return resp


def test_create_list_delete(client):
    assert client.get("/api/vault").json() == []
    r = clean(client.post("/api/vault", json=body(totp=SEED)))
    assert r.status_code == 201
    created = r.json()
    assert set(created) == {"id", "name", "kind", "domain", "lanes", "created_at", "last_used"}
    assert created["domain"] == "instagram.com" and created["lanes"] == ["biz-a/instagram"]
    listed = clean(client.get("/api/vault")).json()
    assert listed == [created]
    r = client.delete(f"/api/vault/{created['id']}")
    assert r.status_code == 200 and r.json() == {"ok": True, "id": created["id"]}
    r = client.delete(f"/api/vault/{created['id']}")
    assert r.status_code == 404 and r.json()["error"]["code"] == "vault_entry_not_found"


def test_conflict(client):
    client.post("/api/vault", json=body())
    r = clean(client.post("/api/vault", json=body()))
    assert r.status_code == 409 and r.json()["error"]["code"] == "vault_entry_exists"


@pytest.mark.parametrize("payload,needle", [
    (body(secret=None), "needs a secret"),
    (body(secret="x" * 5000 + PASSWORD), "longer than"),
    (body(domain="com"), "registrable"),
    (body(lanes=["*"]), "lane binding"),
    (body(kind="sshkey"), "kind"),
    ({**body(), "password": PASSWORD}, "password"),
    (body(secret=12345), "secret"),
    (body(totp="not-a-seed-" + PASSWORD), "base32"),
])
def test_validation_errors_never_echo_input(client, payload, needle):
    r = clean(client.post("/api/vault", json=payload))
    assert r.status_code == 400 and r.json()["error"]["code"] in ("invalid", "vault_entry_invalid")
    assert needle in r.json()["error"]["message"]


def test_bad_bodies(client):
    r = client.post("/api/vault", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 400
    r = client.post("/api/vault", content=b"x" * 300_000)
    assert r.status_code == 400 and "too large" in r.json()["error"]["message"]


def test_sealed_passphrase_vault(tmp_path):
    client = client_for(make_vault(private_home(tmp_path), mode="passphrase", passphrase=PHRASE))
    assert client.get("/api/vault/status").json()["state"] == "sealed"
    r = clean(client.post("/api/vault", json=body()))
    assert r.status_code == 503 and r.json()["error"]["code"] == "vault_sealed"
    assert client.get("/api/vault").status_code == 200          # names stay readable while sealed
    r = clean(client.post("/api/vault/unseal", json={"passphrase": "wrong wrong wrong"}))
    assert r.status_code == 403 and r.json()["error"]["code"] == "vault_wrong_passphrase"
    assert "wrong wrong wrong" not in r.text
    r = clean(client.post("/api/vault/unseal", json={"passphrase": PHRASE}))
    assert r.status_code == 200 and r.json()["state"] == "unsealed"
    assert client.post("/api/vault", json=body()).status_code == 201
    assert client.post("/api/vault/seal").json()["state"] == "sealed"
    assert client.post("/api/vault/unseal", json={}).status_code == 400


def test_uninitialized_vault_is_503(tmp_path):
    from openajar.vault.service import VaultService

    client = client_for(VaultService(private_home(tmp_path)))
    r = client.post("/api/vault", json=body())
    assert r.status_code == 503 and r.json()["error"]["code"] == "vault_not_initialized"
    assert client.get("/api/vault/status").json() == {"initialized": False, "state": "uninitialized"}


def test_missing_vault_service_is_503():
    app = Starlette(routes=[Mount("/api", routes=routes(Runtime()))])
    r = TestClient(app).get("/api/vault")
    assert r.status_code == 503 and r.json()["error"]["code"] == "unavailable"
