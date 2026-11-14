"""A business and what is left to log in in it, as the app reads them (web/src/features/workspaces/).
A business's accounts are listed for anyone who sees the business. The login queue is the owner's:
the node keeps it for the owner alone, and a stranger is refused before anything is read."""

from __future__ import annotations

ORG = "/api/orgs/local"


def test_a_business_is_read_with_its_accounts_in_the_app_shape(client) -> None:
    [first, *_] = client.get(f"{ORG}/workspaces").json()
    detail = client.get(f"{ORG}/workspaces/{first['id']}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["id"] == first["id"] and body["name"]
    assert isinstance(body["lanes"], list)
    for lane in body["lanes"]:
        assert set(lane) >= {"id", "workspace", "name", "state"}


def test_a_business_that_is_not_there_is_not_found(client) -> None:
    missing = client.get(f"{ORG}/workspaces/no-such-business")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"


def test_the_owner_reads_the_login_queue_of_a_business(client) -> None:
    [first, *_] = client.get(f"{ORG}/workspaces").json()
    queue = client.get(f"{ORG}/workspaces/{first['id']}/queue")
    assert queue.status_code == 200, queue.text
    body = queue.json()
    assert set(body) >= {"summary", "ready", "total", "needs_login", "needs_attention", "lanes"}
    assert body["ready"] <= body["total"]
    for lane in body["lanes"]:
        assert lane["readiness"] in {"needs_login", "needs_attention"}, "only what is not ready yet is queued"


def test_a_stranger_reads_no_queue_and_no_business(client) -> None:
    saved = dict(client.cookies)
    client.cookies.clear()
    try:
        assert client.get(f"{ORG}/workspaces/x/queue").status_code == 401
        assert client.get(f"{ORG}/workspaces/x").status_code == 401
    finally:
        client.cookies.update(saved)
