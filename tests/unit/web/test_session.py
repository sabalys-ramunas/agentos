"""The app's sign-in contract on a node (web/src/api/types.ts, `Session`)."""

from __future__ import annotations


def test_a_node_is_one_org_and_says_how_to_sign_in(client) -> None:
    providers = client.get("/api/auth/providers").json()
    assert providers == {"magic_link": False, "google": False, "passkey": True, "code": True}

    session = client.get("/api/session").json()
    assert session["user"]["id"] == "owner"
    assert session["csrf"]
    [org] = session["orgs"]
    assert (org["slug"], org["role"], org["plan"]) == ("local", "owner", "self_hosted")


def test_the_session_route_needs_a_session(client) -> None:
    client.cookies.clear()
    assert client.get("/api/session").status_code == 401


def test_anything_the_contract_does_not_reshape_is_the_nodes_own_route(client) -> None:
    assert client.get("/api/orgs/local/lanes").json() == client.get("/api/lanes").json()


def test_any_other_org_is_not_here(client) -> None:
    assert client.get("/api/orgs/acme/lanes").status_code == 404


def test_a_node_serves_the_web_app(tmp_path, monkeypatch) -> None:
    from agentos.api.app import _default_panel_dir

    monkeypatch.delenv("AGENTOS_PANEL_DIR", raising=False)
    root = tmp_path / "repo"
    assert _default_panel_dir(root) == root / "web" / "dist"
    monkeypatch.setenv("AGENTOS_PANEL_DIR", str(tmp_path / "built"))
    assert _default_panel_dir(root) == tmp_path / "built"


def test_a_node_with_no_ai_model_says_so_with_null(client) -> None:
    # The app reads `brain` as a model or null. A node with none configured has no model to name.
    assert client.get("/api/orgs/local/status").json()["brain"] is None
