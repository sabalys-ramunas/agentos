"""Proof, as the app reads it: a day of one account replayed, the log check, and read-only share links
(web/src/features/activity/proof, web/src/features/shared). The node's own tests cover the chain itself
(tests/unit/test_audit_api.py); these cover what the app is given through the contract, and what a
read-only link may show to someone with no sign-in."""

from __future__ import annotations

from datetime import UTC, datetime

from openajar.policy.traces import TraceEvent

ORG = "/api/orgs/local"
LANE = "biz-a/shop"


def _day() -> str:
    return datetime.now(UTC).date().isoformat()


def _act(node, tool: str = "shop.list", status: str = "ok", lane: str = LANE) -> None:
    node.get("traces").write(TraceEvent(kind="tool_end", principal="claude-code", principal_kind="agent", tool=tool,
                                        lane=lane, workspace=lane.split("/", 1)[0], effect="read", status=status,
                                        summary=f"{tool} on {lane}"))


def _anonymous(client):
    """The same app with no cookie, as someone who was sent a link would have it."""
    saved = dict(client.cookies)
    client.cookies.clear()
    return client, saved


def test_the_owner_replays_a_day_and_the_index_names_the_accounts_that_acted(client, node) -> None:
    _act(node)
    _act(node, "shop.open")
    index = client.get(f"{ORG}/audit/days?day={_day()}&tz=UTC").json()
    assert index["day"] == _day() and index["timezone"] == "UTC"
    assert LANE in [row["lane"] for row in index["lanes"]]
    day = client.get(f"{ORG}/audit/lanes/{LANE}?day={_day()}&tz=UTC").json()
    assert day["lane"] == LANE and len(day["steps"]) >= 2
    assert set(day["chain"]) >= {"status", "proven_through", "findings"}
    for step in day["steps"]:
        assert set(step) >= {"id", "at", "summary", "status", "chain", "principal", "screenshot"}


def test_the_log_check_says_what_the_node_checked_and_only_the_owner_asks(client, node) -> None:
    _act(node)
    report = client.get(f"{ORG}/audit/verify").json()
    assert report["status"] in {"ok", "pending", "tampered", "unverifiable"}
    assert report["checked"] >= 1 and set(report) >= {"rows", "chain", "keys", "findings"}


def test_a_share_is_made_listed_without_its_token_and_stopped(client, node) -> None:
    _act(node)
    made = client.post(f"{ORG}/audit/shares", json={"lane": LANE, "day": _day(), "ttl": "24h"})
    assert made.status_code == 200, made.text
    share = made.json()
    assert share["url"].endswith(f"/shared#token={share['token']}") and share["kind"] == "lane_day"
    listed = client.get(f"{ORG}/audit/shares").json()
    row = next(item for item in listed if item["id"] == share["id"])
    assert row["state"] == "active" and row["views"] == 0 and "token" not in row
    stopped = client.delete(f"{ORG}/audit/shares/{share['id']}")
    assert stopped.status_code == 200 and stopped.json()["state"] == "revoked"


def test_a_read_only_link_opens_with_no_sign_in_and_hides_grant_ids(client, node) -> None:
    _act(node)
    token = client.post(f"{ORG}/audit/shares", json={"lane": LANE, "day": _day()}).json()["token"]
    anonymous, saved = _anonymous(client)
    try:
        opened = anonymous.get(f"{ORG}/audit/shared/{token}")
        assert opened.status_code == 200, opened.text
        body = opened.json()
        assert body["lane"] == LANE and body["share"]["kind"] == "lane_day"
        for step in body["steps"]:
            assert (step.get("grant") or {}).get("id") in (None,), "a grant id is never shown to a link"
        assert anonymous.get(f"{ORG}/audit/lanes/{LANE}?day={_day()}").status_code == 401
        assert anonymous.get(f"{ORG}/audit/shares").status_code == 401
    finally:
        client.cookies.update(saved)


def test_a_bad_or_stopped_link_answers_not_found_and_says_why(client, node) -> None:
    _act(node)
    share = client.post(f"{ORG}/audit/shares", json={"lane": LANE, "day": _day()}).json()
    assert client.delete(f"{ORG}/audit/shares/{share['id']}").status_code == 200  # the owner stops it, signed in
    anonymous, saved = _anonymous(client)
    try:
        forged = anonymous.get(f"{ORG}/audit/shared/shr_forged")
        assert forged.status_code == 404 and forged.json()["error"]["code"] == "share_invalid"
        stopped = anonymous.get(f"{ORG}/audit/shared/{share['token']}")
        assert stopped.status_code == 404 and stopped.json()["error"]["code"] == "share_invalid"
    finally:
        client.cookies.update(saved)


def test_a_share_needs_one_step_or_one_account_and_a_day(client, node) -> None:
    _act(node)
    refused = client.post(f"{ORG}/audit/shares", json={"ttl": "24h"})
    assert refused.status_code == 400
    too_long = client.post(f"{ORG}/audit/shares", json={"lane": LANE, "day": _day(), "ttl": "30d"})
    assert too_long.status_code == 400
