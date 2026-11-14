"""Members on a node, as the app reads them (web/src/api/types.ts, `Member`). The owner is the one person
a new node has, and the app names people by it."""

from __future__ import annotations


def test_the_owner_is_the_one_member_and_the_app_can_name_them(client) -> None:
    [member] = client.get("/api/orgs/local/members").json()
    assert member["id"] == "owner" and member["user"]["id"] == "owner"
    assert member["role"] == "owner" and member["workspaces"] is None
    assert member["user"]["name"], "the app shows a name, never an empty one"
    assert member["joined_at"] and member["last_active_at"] is None


def test_a_member_lists_the_devices_they_are_signed_in_on_in_the_app_shape(client) -> None:
    detail = client.get("/api/orgs/local/members/owner")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["id"] == "owner" and body["role"] == "owner"
    assert set(body["devices"]) == {"passkeys", "sessions"}
    assert body["devices"]["sessions"], "the owner is signed in on at least one device"
    for session in body["devices"]["sessions"]:
        assert set(session) >= {"id", "label", "via", "created_at", "last_seen_at", "expires_at", "current"}
        assert session["label"], "a device always has words, never an empty label"


def test_ending_a_device_that_is_not_there_is_not_found(client) -> None:
    assert client.delete("/api/orgs/local/members/owner/devices/nope").status_code == 404


def test_a_stranger_reads_no_member_or_their_devices(client) -> None:
    saved = dict(client.cookies)
    client.cookies.clear()
    try:
        assert client.get("/api/orgs/local/members/owner").status_code == 401
        assert client.delete("/api/orgs/local/members/owner/devices/nope").status_code == 401
    finally:
        client.cookies.update(saved)
