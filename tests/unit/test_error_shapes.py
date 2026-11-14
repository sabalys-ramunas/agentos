"""One error shape on every HTTP surface (QA-2 #17).

The node's sign-in had no Origin check while the controller's had one; the controller answered an
unknown route in plain text; and the viewer's errors had no `fix` line for a person."""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient
from test_node_panel import Node

from agentos.controller.app import Controller, build_app
from agentos.controller.config import ControllerConfig
from agentos.node import viewer_app


@pytest.fixture
def node(tmp_path):
    n = Node(tmp_path)
    yield n
    n.close()


def test_the_nodes_sign_in_refuses_another_site_and_keeps_the_code(node):
    node.store.add_code("one-time-code-x")
    with node.client() as c:
        r = c.post("/api/session", json={"code": "one-time-code-x"}, headers={"origin": "https://evil.example"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed" and r.json()["error"]["fix"], r.text
        assert "agentos_session" not in r.headers.get("set-cookie", "")
        r = c.post("/api/session", json={"code": "one-time-code-x"}, headers={"origin": "http://localhost:7400"})
        assert r.status_code == 200, r.text  # the same site, and the code was not spent by the refusal


@pytest.fixture
def controller(tmp_path):
    config = ControllerConfig(home=tmp_path / "controller", owner_token="owner-token-for-error-shapes",
                              agent_token="aga_error_shapes_agent_token_for_tests")
    c = Controller(config)
    yield c, TestClient(build_app(c), base_url="http://localhost")
    c.close()


def test_the_controller_answers_an_unknown_route_and_a_wrong_method_with_the_shared_body(controller):
    _c, client = controller
    r = client.get("/api/no-such-route")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/json"), r.text
    assert r.json()["error"]["code"] == "not_found" and r.json()["error"]["fix"]
    r = client.delete("/api/session/../health")
    assert r.status_code in (404, 405) and r.json()["error"]["code"] in ("not_found", "method_not_allowed")
    r = client.put("/api/session")
    assert r.status_code == 405 and r.json()["error"]["code"] == "method_not_allowed", r.text
    assert r.json()["error"]["fix"] and "—" not in r.text


@pytest.mark.parametrize(("status", "code"), [(403, "denied"), (403, "presence_required"), (400, "invalid"),
                                              (404, "not_found"), (409, "frozen")])
def test_viewer_errors_carry_a_fix(status, code):
    body = json.loads(viewer_app._error(status, code, "x").body)
    assert body["error"]["code"] == code and body["error"]["fix"], body


def test_both_sign_ins_refuse_another_site_the_same_way(controller):
    c, client = controller
    url, _ = c.login_link()
    r = client.post("/api/session", json={"code": url.split("#code=")[1]}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed" and r.json()["error"]["fix"], r.text
