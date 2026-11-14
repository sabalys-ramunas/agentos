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


def test_a_node_serves_the_web_app_only_once_a_person_chose_it(tmp_path) -> None:
    from agentos.api.app import UI_CHOICE, _default_panel_dir, chosen_ui

    assert chosen_ui(tmp_path) == "classic"
    assert _default_panel_dir(tmp_path, tmp_path / "repo") == tmp_path / "repo" / "panel" / "dist"

    (tmp_path / UI_CHOICE).write_text("web\n")
    assert chosen_ui(tmp_path) == "web"


def test_a_checkout_with_only_the_web_app_built_serves_it(tmp_path, monkeypatch) -> None:
    from agentos.api.app import UI_CHOICE, _default_panel_dir

    monkeypatch.delenv("AGENTOS_PANEL_DIR", raising=False)
    home, root = tmp_path / "home", tmp_path / "repo"
    home.mkdir()
    for ui in ("web", "panel"):
        (root / ui / "dist").mkdir(parents=True)
    # nothing built: the panel's place, which serves the placeholder page
    assert _default_panel_dir(home, root) == root / "panel" / "dist"
    # only web/ built, as in the public repository, which has no panel/: the web app
    (root / "web" / "dist" / "index.html").write_text("<!doctype html>")
    assert _default_panel_dir(home, root) == root / "web" / "dist"
    # both built: the panel until a person chooses the web app
    (root / "panel" / "dist" / "index.html").write_text("<!doctype html>")
    assert _default_panel_dir(home, root) == root / "panel" / "dist"
    (home / UI_CHOICE).write_text("web\n")
    assert _default_panel_dir(home, root) == root / "web" / "dist"


def test_a_node_with_no_ai_model_says_so_with_null(client) -> None:
    # The app reads `brain` as a model or null. A node with none configured has no model to name.
    assert client.get("/api/orgs/local/status").json()["brain"] is None
