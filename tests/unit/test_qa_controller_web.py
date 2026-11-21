"""QA for the controller's owner API (openajar.controller.web) through the real ASGI app: the cookie
session's CSRF rule, bearer scripts, and that routes which act on an id only reach their own org.

Bugs are strict xfails pinned to their reason; remove the marker once fixed.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from openajar.controller.app import Controller, build_app
from openajar.controller.config import ControllerConfig

OWNER = "owner-token-for-qa-web-tests"
BEARER = {"Authorization": f"Bearer {OWNER}"}
OTHER_NODE = "node_01QAWEBOTHER0000000000000"


@pytest.fixture
def api(tmp_path):
    config = ControllerConfig(home=tmp_path / "controller", owner_token=OWNER,
                              agent_token="aga_qa_web_agent_token_for_tests")
    controller = Controller(config)
    client = TestClient(build_app(controller), base_url="http://localhost")
    yield controller, client
    controller.close()


def signed_in(controller: Controller, client: TestClient) -> str:
    """Sign in with a one-time code (the cookie stays in the client); returns the CSRF token."""
    url, _ = controller.login_link()
    response = client.post("/api/session", json={"code": url.split("#code=")[1]})
    assert response.status_code == 200
    return response.json()["csrf"]


def test_a_cookie_session_cannot_skip_csrf_by_sending_another_authorization_header(api):
    controller, client = api
    signed_in(controller, client)
    response = client.post("/api/join-tokens", json={"name": "box-qa"},
                           headers={"Authorization": "Basic Zm9vOmJhcg=="})
    assert response.status_code == 403


def test_a_bearer_owner_script_posts_without_the_csrf_header(api):
    _, client = api
    response = client.post("/api/join-tokens", json={"name": "box-qa"}, headers=BEARER)
    assert response.status_code == 201 and response.json()["join_command"].startswith("openajar node join --self-hosted ")


def test_a_cookie_session_still_needs_the_csrf_header_for_changes(api):
    controller, client = api
    signed_in(controller, client)
    assert client.post("/api/join-tokens", json={"name": "box-qa"}).status_code == 403


def test_an_id_from_another_org_is_not_found_on_every_route_that_acts_on_one(api):
    controller, client = api
    other = controller.store.create_org("other-qa", "Other QA")
    grant, _ = controller.grants.create(other["id"], subject="their-agent")
    _, token = controller.create_join_token(other["id"], name="theirs")
    row = controller.store.join_tokens(other["id"])[0]
    assert client.delete(f"/api/grants/{grant.id}", headers=BEARER).status_code == 404
    assert controller.grants.get(other["id"], grant.id).revoked_at is None
    assert client.delete(f"/api/join-tokens/{row['id']}", headers=BEARER).status_code == 404
    assert controller.store.join_tokens(other["id"])[0]["used_at"] is None and token.startswith("ajt_")
    lane = client.post(f"/api/nodes/{OTHER_NODE}/lanes/personal/reddit/control", json={"action": "take"},
                       headers=BEARER)
    assert lane.status_code == 404
    assert client.delete(f"/api/nodes/{OTHER_NODE}", headers=BEARER).status_code == 404


def test_the_grant_list_shows_only_the_owners_org(api):
    controller, client = api
    other = controller.store.create_org("other-qa", "Other QA")
    controller.grants.create(other["id"], subject="their-agent")
    subjects = [g["subject"]["id"] for g in client.get("/api/grants", headers=BEARER).json()]
    assert "their-agent" not in subjects and "owner-agent" in subjects
