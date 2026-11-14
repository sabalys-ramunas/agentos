"""An account's page on a node, as the app reads it (web/src/api/types.ts: `LanePace`, `Meter`, `LaneSites`,
`LaneTaint`, `LaneQueue`, `LaneCheck`, `LaneKeepalive`). Each answer is the node's own route.

Reading an account is for anyone who can see it. Changing its pace, limits or site list is the owner's
alone, and a bearer token, even one from a grant, never does it.
"""

from __future__ import annotations

LANE = "biz-a/social"
BASE = f"/api/orgs/local/lanes/{LANE}"
TOKEN = "t" * 43


def as_grant(client, node) -> None:
    """A grant that can see biz-a and read: what an agent holds."""
    _, token = node.get("policy").issue(subject={"id": "worker"}, workspaces=["biz-a"], ceiling="read")
    client.cookies.clear()
    client.headers["authorization"] = f"Bearer {token}"


def as_node_token(client) -> None:
    client.cookies.clear()
    client.headers["authorization"] = f"Bearer {TOKEN}"


def test_the_account_reads_are_served_for_the_owner(client) -> None:
    for part in ("pace", "meter", "queue", "taint", "check", "keepalive", "sites"):
        answer = client.get(f"{BASE}/{part}")
        assert answer.status_code == 200, f"{part}: {answer.text}"
        assert LANE in answer.text, part  # a meter names its lane as `id`, the rest as `lane`


def test_the_site_list_is_what_the_owner_set(client) -> None:
    assert client.get(f"{BASE}/sites").json() == {"lane": LANE, "sites": [], "strict": False}
    added = client.post(f"{BASE}/sites", json={"op": "add", "sites": ["instagram.com"]})
    assert added.status_code == 200, added.text
    assert client.get(f"{BASE}/sites").json()["sites"] == ["instagram.com"]
    removed = client.post(f"{BASE}/sites", json={"op": "remove", "sites": ["instagram.com"]})
    assert removed.status_code == 200, removed.text
    assert client.get(f"{BASE}/sites").json()["sites"] == []


def test_a_grant_reads_the_site_list_but_never_changes_it(client, node) -> None:
    as_grant(client, node)
    assert client.get(f"{BASE}/sites").status_code == 200
    refused = client.post(f"{BASE}/sites", json={"op": "add", "sites": ["evil.example"]})
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "denied"


def test_a_grant_reads_the_pace_and_the_limits_but_never_sets_them(client, node) -> None:
    as_grant(client, node)
    assert client.get(f"{BASE}/pace").status_code == 200
    assert client.get(f"{BASE}/meter").status_code == 200
    assert client.put(f"{BASE}/pace", json={"pace": {"commits_per_day": 2}}).status_code == 403
    assert client.put(f"{BASE}/meter", json={"limits": {"commits_per_day": 2}}).status_code == 403


def test_a_grant_cannot_clear_taint(client, node) -> None:
    grant_id = node.get("policy").issue(subject={"id": "worker"}, workspaces=["biz-a"], ceiling="read")[0].id
    as_node_token(client)
    refused = client.post(f"/api/orgs/local/grants/{grant_id}/taint/clear", json={"confirm": "clear"})
    assert refused.status_code == 403, refused.text


def test_the_owner_sets_a_pace_and_its_limits_and_reads_them_back(client) -> None:
    pace = client.put(f"{BASE}/pace", json={"pace": {"commits_per_day": 3, "min_gap_minutes": 45,
                                                    "active_hours": ["08:00-22:00"]}})
    assert pace.status_code == 200, pace.text
    read = client.get(f"{BASE}/pace").json()
    assert read["paced"] is True and read["pace"]["commits_per_day"] == 3
    limits = client.put(f"{BASE}/meter", json={"limits": {"commits_per_day": 6}})
    assert limits.status_code == 200, limits.text
    assert client.get(f"{BASE}/meter").json()["limits"][0]["value"] == 6


def test_a_lane_that_is_not_here_has_no_site_list(client) -> None:
    assert client.get("/api/orgs/local/lanes/biz-a/nowhere/sites").status_code == 404
