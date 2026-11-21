"""The node's forms for a secret (openajar.node.forms, openajar.node.login_form, openajar.workspaces.form).

The web app starts a trip and reads how far it got. The page on this computer takes what only the owner may
type. These tests hold the lines that matter: who may start a trip, which plain fields a trip takes, that a
link ends after its use, and that no secret comes back in any answer.
"""

from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient

from openajar.node import forms as node_forms
from openajar.node import login_form
from openajar.runner.mail import form as mail_form

SECRET = "hunter2-correct-horse"
LANE = "biz-a/shop"


@pytest.fixture
def direct(monkeypatch: pytest.MonkeyPatch) -> None:
    """The page answers only over https or from this computer. A test client is neither, so say it is."""
    monkeypatch.setattr(mail_form, "direct", lambda request: True)


def start(client: TestClient, kind: str, fields: dict[str, str] | None = None) -> Any:
    return client.post("/api/forms", json={"kind": kind, "fields": fields or {}})


def token_of(trip: dict[str, Any]) -> str:
    return trip["form_url"].rsplit("/", 1)[1]


def page_of(client: TestClient, trip: dict[str, Any]) -> str:
    return client.get(trip["form_url"]).text


def test_only_the_owner_in_person_starts_a_form(client: TestClient) -> None:
    client.cookies.clear()  # no sign-in at all
    client.headers.pop("x-agentos-csrf", None)
    assert start(client, "vault_unlock").status_code in (401, 403)


def test_a_stranger_token_cannot_start_a_form(client: TestClient) -> None:
    client.cookies.clear()
    client.headers.pop("x-agentos-csrf", None)
    client.headers["authorization"] = "Bearer " + "s" * 43  # not any token this node issued
    assert start(client, "vault_unlock").status_code == 401
    client.headers.pop("authorization", None)


def test_a_start_takes_only_the_plain_fields_of_its_kind(client: TestClient) -> None:
    refused = start(client, "vault_unlock", {"passphrase": SECRET})
    assert refused.status_code == 400
    assert "passphrase is not a field of this form" in refused.text
    assert SECRET not in refused.text  # the message names the field, never its value


def test_a_start_needs_the_fields_it_is_for(client: TestClient) -> None:
    missing = start(client, "vault_entry", {"name": "shop"})
    assert missing.status_code == 400
    assert "domain" in missing.text or "kind" in missing.text or "lanes" in missing.text


def test_the_login_trip_is_not_started_by_the_plain_route(client: TestClient) -> None:
    assert start(client, "login").status_code == 400


def test_a_form_is_refused_unless_it_is_direct(client: TestClient) -> None:
    trip = start(client, "vault_unlock").json()
    page = client.get(trip["form_url"])
    assert page.status_code == 403
    assert "Open this through the tunnel" in page.text


def test_the_app_hears_how_far_a_trip_got(client: TestClient, direct: None) -> None:
    trip = start(client, "vault_unlock").json()
    assert trip["stage"] == "waiting" and trip["form_url"].startswith("/api/forms/")
    assert client.get(f"/api/forms/trips/{trip['id']}").json()["stage"] == "waiting"
    assert client.post(trip["form_url"], data={"passphrase": SECRET}).status_code == 200
    done = client.get(f"/api/forms/trips/{trip['id']}").json()
    assert done["stage"] == "done" and done["form_url"] is None
    assert client.get(trip["form_url"]).status_code == 410  # one trip: the link has ended


def test_a_key_file_vault_opens_with_nothing_typed(client: TestClient, node: Any, direct: None) -> None:
    """The key file on this computer opens a key-file vault, so the page has no field and asks for nothing."""
    node.require("vault").seal()
    trip = start(client, "vault_unlock").json()
    page = page_of(client, trip)
    assert "Unlock the vault" in page and "nothing needs typing" in page
    assert "<input" not in page
    opened = client.post(trip["form_url"], data={})
    assert opened.status_code == 200 and "Unlocked" in opened.text
    assert node.require("vault").status()["state"] == "unsealed"


def test_a_passphrase_vault_takes_its_passphrase_and_keeps_it_off_the_page(client: TestClient, node: Any, direct: None,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    vault = node.require("vault")
    monkeypatch.setattr(vault, "status", lambda: {"initialized": True, "state": "sealed", "mode": "passphrase"})
    sent: list[Any] = []

    async def unseal(passphrase: Any = None) -> dict[str, str]:
        sent.append(passphrase)
        return {"state": "unsealed"}

    monkeypatch.setattr(vault, "unseal", unseal)
    trip = start(client, "vault_unlock").json()
    assert 'name="passphrase"' in page_of(client, trip)
    empty = client.post(trip["form_url"], data={"passphrase": ""})
    assert empty.status_code == 400 and "was empty" in empty.text
    opened = client.post(trip["form_url"], data={"passphrase": SECRET})
    assert opened.status_code == 200 and "Unlocked" in opened.text
    assert SECRET not in opened.text
    assert [item.reveal() for item in sent] == [SECRET]


def test_a_vault_entry_is_kept_on_the_node_and_its_secret_never_comes_back(client: TestClient, direct: None) -> None:
    trip = start(client, "vault_entry", {"name": "shop-login", "kind": "password", "domain": "shop.example.com",
                                         "lanes": LANE}).json()
    page = page_of(client, trip)
    assert "shop.example.com" in page and SECRET not in page
    saved = client.post(trip["form_url"], data={"username": "owner@shop.example.com", "secret": SECRET})
    assert saved.status_code == 200 and "Saved" in saved.text
    assert SECRET not in saved.text
    names = client.get("/api/vault").json()
    assert any(entry["name"] == "shop-login" and LANE in entry["lanes"] for entry in names)
    assert SECRET not in client.get("/api/vault").text
    assert "owner@shop.example.com" not in client.get("/api/vault").text


def test_a_proxy_keeps_its_password_on_the_node(client: TestClient, direct: None) -> None:
    trip = start(client, "proxy", {"label": "de1", "host": "10.0.0.7", "port": "8080", "username": "u1"}).json()
    page = page_of(client, trip)
    assert "10.0.0.7" in page and SECRET not in page
    saved = client.post(trip["form_url"], data={"password": SECRET})
    assert saved.status_code == 200 and "Saved" in saved.text, saved.text
    assert SECRET not in saved.text
    listed = client.get("/api/proxies")
    assert listed.status_code == 200 and SECRET not in listed.text
    assert any(item.get("label") == "de1" for item in listed.json())


def test_a_proxy_refuses_to_repoint_an_existing_label_without_the_box(client: TestClient, direct: None) -> None:
    first = start(client, "proxy", {"label": "de2", "host": "10.0.0.8", "port": "8080"}).json()
    assert client.post(first["form_url"], data={}).status_code == 200
    second = start(client, "proxy", {"label": "de2", "host": "10.0.0.9", "port": "8080"}).json()
    refused = client.post(second["form_url"], data={"password": SECRET})
    assert refused.status_code == 400 and "already points at" in refused.text
    assert SECRET not in refused.text


def test_export_needs_a_passphrase_and_then_downloads_a_sealed_file(client: TestClient, direct: None) -> None:
    trip = start(client, "workspace_export", {"workspace": "biz-a"}).json()
    short = client.post(trip["form_url"], data={"passphrase": "short"})
    assert short.status_code == 400 and "at least 8 characters" in short.text
    done = client.post(trip["form_url"], data={"passphrase": SECRET})
    assert done.status_code == 200, done.text
    assert done.headers["content-disposition"].startswith("attachment;")
    assert SECRET.encode() not in done.content


def test_an_import_takes_a_new_name_into_its_own_box(client: TestClient, direct: None) -> None:
    """The browser sends the name it was given, and the page keeps it in a box of its own (never a flag)."""
    started = start(client, "workspace_import", {"name": "biz-b"})
    assert started.status_code == 200, started.text
    page = page_of(client, started.json())
    assert 'name="name"' in page and 'value="biz-b"' in page
    assert "--as" not in page and "passphrase" in page


def test_an_import_refuses_a_file_it_cannot_open(client: TestClient, direct: None) -> None:
    trip = start(client, "workspace_import").json()
    upload = {"file": ("biz-a.openajar", io.BytesIO(b"not an export at all"), "application/octet-stream")}
    refused = client.post(trip["form_url"], data={"passphrase": SECRET}, files=upload)
    assert refused.status_code == 400
    assert "did not open this file" in refused.text
    assert SECRET not in refused.text


def test_a_form_page_names_the_site_and_never_dresses_as_it(client: TestClient, direct: None, monkeypatch) -> None:
    fake = FakeLogin()
    monkeypatch.setattr(login_form, "_service", lambda runtime: fake)
    login = client.post(f"/api/lanes/{LANE}/login/form", json={})
    assert login.status_code == 200, login.text
    body = login.json()
    assert body["stage"] == "waiting" and body["kind"] == "login"
    page = page_of(client, body)
    assert "This page is openajar on this computer" in page
    assert 'type="password"' in page and 'name="password"' in page


def test_a_login_form_types_what_the_owner_sends_and_nothing_else(client: TestClient, direct: None, monkeypatch) -> None:
    fake = FakeLogin()
    monkeypatch.setattr(login_form, "_service", lambda runtime: fake)
    trip = client.post(f"/api/lanes/{LANE}/login/form", json={}).json()
    sent = client.post(trip["form_url"], data={"action": "step", "step": "s1", "username": "owner@shop.example",
                                               "password": SECRET, "junk": "ignored"})
    assert sent.status_code == 200, sent.text
    assert SECRET not in sent.text and "owner@shop.example" not in sent.text
    (_, step, names, choice, by), = [c for c in fake.calls if c[0] == "submit"]
    assert (step, names, choice, by) == ("s1", ["password", "username"], None, "owner")
    assert fake.values["password"] == SECRET  # the typist gets the value, the page never does


def test_a_sign_in_that_ended_closes_its_form(client: TestClient, direct: None, monkeypatch) -> None:
    fake = FakeLogin()
    monkeypatch.setattr(login_form, "_service", lambda runtime: fake)
    trip = client.post(f"/api/lanes/{LANE}/login/form", json={}).json()
    fake.login = {**fake.login, "state": "cancelled"}
    assert client.get(trip["form_url"]).status_code == 410
    assert client.get(f"/api/forms/trips/{trip['id']}").json()["stage"] == "ended"


def test_the_trips_store_drops_what_ran_out(monkeypatch) -> None:
    trips = node_forms.Trips()
    trip = trips.start("vault_unlock", "owner", {})
    assert trips.by_token(trip.token) is trip
    later = trip.expires_at + 1  # captured now: the store moves the trip's expiry when it closes it
    monkeypatch.setattr(node_forms, "time", SimpleNamespace(time=lambda: later))
    assert trips.by_token(trip.token) is None
    assert trips.by_id(trip.id).stage == "ended"


def test_the_public_trip_carries_its_link_and_no_secret_field() -> None:
    trip = node_forms.Trips().start("vault_entry", "owner", {"name": "x"})
    assert set(trip.public()) == {"id", "kind", "stage", "subject", "form_url", "expires_at"}
    assert trip.token in trip.public()["form_url"]  # the link is the one place a token shows, and only as the link


class FakeLogin:
    """The login service, as the form sees it: one step, a sign-in that types what it is given."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.values: dict[str, str] = {}
        self.login: dict[str, Any] = {
            "id": "la_1", "lane": LANE, "state": "ask", "site": "shop", "site_name": "Shop",
            "host": "shop.example", "error": None, "can_save": False, "saved": None,
            "step": {"id": "s1", "kind": "sign_in", "heading": "Sign in", "message": "", "number": None,
                     "fields": [
                         {"name": "username", "label": "Email", "secret": False, "length": None, "autocomplete": "username"},
                         {"name": "password", "label": "Password", "secret": True, "length": None, "autocomplete": "current-password"},
                     ], "choices": [], "submit": "Next", "reason": None},
        }

    def get(self, lane: str) -> dict[str, Any] | None:
        return self.login

    async def submit(self, lane: str, *, step: str, values: Any = None, choice: Any = None, by: str) -> dict[str, Any]:
        self.values = {name: secret.reveal() for name, secret in (values or {}).items()}
        self.calls.append(("submit", step, sorted(self.values), choice, by))
        self.login = {**self.login, "state": "signed_in", "can_save": True}
        return self.login

    async def refresh(self, lane: str, *, by: str) -> dict[str, Any]:
        self.calls.append(("refresh", by))
        return self.login

    async def take(self, lane: str, *, by: str) -> dict[str, Any]:
        self.calls.append(("take", by))
        return self.login

    async def resume(self, lane: str, *, by: str) -> dict[str, Any]:
        self.calls.append(("resume", by))
        return self.login

    async def save(self, lane: str, *, by: str, name: Any = None) -> dict[str, Any]:
        self.calls.append(("save", by, name))
        return self.login

    async def cancel(self, lane: str, *, by: str, done: bool = False) -> dict[str, Any]:
        self.calls.append(("cancel", by, done))
        self.login = {**self.login, "state": "done" if done else "cancelled"}
        return self.login
