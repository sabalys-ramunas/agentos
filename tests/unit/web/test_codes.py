"""Code cards on a node, as the app reads and answers them (web/src/api/types.ts, `CodeRequest`).

A code is one-time and write-only: it goes into the site and nowhere else. Every check here reads the
answer the way the app does, and the code's own digits must never come back in one.
"""

from __future__ import annotations

import asyncio

TOKEN = "t" * 43
CODE = "424 242"
CLEAN = "424242"
LANE = "biz-a/social"


def open_request(node, lane: str = LANE, site: str = "instagram.com") -> str:
    """Opens a code card the way a login does at its code step, and returns its id."""
    relay = node.require("vault").relay

    async def open_it() -> str:
        request, _slot = relay.open(lane=lane, site=site, entry="entry_1", requester={"kind": "agent", "id": "claude-code"},
                                    grant_path=None, window_seconds=600)
        return request.id

    return asyncio.run(open_it())


def test_an_open_card_is_listed_as_the_app_reads_it(client, node) -> None:
    request_id = open_request(node)
    [card] = client.get("/api/orgs/local/codes").json()
    assert card["id"] == request_id
    assert card["state"] == "open"
    assert card["lane"] == LANE and card["site"] == "instagram.com"
    assert card["requested_by"] == {"kind": "agent", "id": "claude-code"}
    assert card["created_at"].endswith("Z") and card["expires_at"].endswith("Z")


def test_no_card_is_listed_when_none_is_open(client) -> None:
    assert client.get("/api/orgs/local/codes").json() == []


def test_the_owner_types_the_code_once_and_it_is_taken(client, node) -> None:
    request_id = open_request(node)
    answered = client.post(f"/api/orgs/local/codes/{request_id}", json={"code": CODE})
    assert answered.status_code == 200, answered.text
    assert answered.json()["state"] == "used"
    assert CLEAN not in answered.text and CODE not in answered.text, "the code never comes back"

    assert client.get("/api/orgs/local/codes").json() == []
    again = client.post(f"/api/orgs/local/codes/{request_id}", json={"code": CODE})
    assert again.status_code == 404
    assert again.json()["error"]["code"] == "vault_code_request_closed"


def test_a_code_is_four_to_twelve_letters_or_digits(client, node) -> None:
    request_id = open_request(node)
    too_short = client.post(f"/api/orgs/local/codes/{request_id}", json={"code": "12"})
    assert too_short.status_code in (400, 422)
    assert [card["id"] for card in client.get("/api/orgs/local/codes").json()] == [request_id]


def test_declining_stops_the_login_and_the_card_leaves(client, node) -> None:
    request_id = open_request(node)
    declined = client.post(f"/api/orgs/local/codes/{request_id}/decline")
    assert declined.status_code == 200, declined.text
    assert declined.json()["state"] == "declined"
    assert client.get("/api/orgs/local/codes").json() == []


def test_a_grant_cannot_see_or_answer_a_card(client, node) -> None:
    request_id = open_request(node)
    issued = client.post("/api/orgs/local/grants", json={"subject": "helper", "workspaces": ["biz-a"],
                                                         "ceiling": "commit"})
    assert issued.status_code == 200, issued.text
    token = issued.json()["token"]
    client.cookies.clear()
    client.headers["authorization"] = f"Bearer {token}"
    assert client.get("/api/orgs/local/codes").status_code == 403
    assert client.post(f"/api/orgs/local/codes/{request_id}", json={"code": CODE}).status_code == 403
    assert client.post(f"/api/orgs/local/codes/{request_id}/decline").status_code == 403
    client.headers.pop("authorization")
