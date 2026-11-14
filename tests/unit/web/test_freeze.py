"""Freeze (break glass) on a node, as the app reads and sends it (web/src/api/types.ts, `FreezeStatus`).

A freeze stops every agent in its scope and revokes the grants that reach it. Only the owner lifts one,
signed in, and nothing comes back by itself. A token or an agent's grant never freezes for the owner,
and never lifts anything.
"""

from __future__ import annotations

LANE = "biz-a/social"
TOKEN = "t" * 43
FREEZE = "/api/orgs/local/freeze"
UNFREEZE = "/api/orgs/local/unfreeze"


def as_agent(client, token: str) -> dict:
    """Calls as an agent's token instead of the owner's session. Returns the session to put back."""
    saved = dict(client.cookies)
    client.cookies.clear()
    client.headers["authorization"] = f"Bearer {token}"
    return saved


def as_owner(client, saved: dict) -> None:
    client.headers.pop("authorization", None)
    client.cookies.update(saved)


def test_nothing_is_frozen_on_a_new_node(client) -> None:
    status = client.get(FREEZE).json()
    assert status["available"] is True
    assert status["frozen"] == [] and status["lanes"] == []
    assert status["node_frozen"] is False
    assert status["problem"] is None


def test_the_owner_freezes_one_account_and_the_app_sees_it(client) -> None:
    report = client.post(FREEZE, json={"scope": "lane", "target": LANE, "reason": "a login we did not expect"})
    assert report.status_code == 200, report.text
    body = report.json()
    assert (body["scope"], body["target"], body["already_frozen"]) == ("lane", LANE, False)
    assert body["reason"] == "a login we did not expect"
    assert {"stopped", "revoked", "errors", "took_ms"} <= body.keys()

    status = client.get(FREEZE).json()
    assert [(record["scope"], record["target"]) for record in status["frozen"]] == [("lane", LANE)]
    assert [row["lane"] for row in status["lanes"]] == [LANE]
    assert status["node_frozen"] is False


def test_freezing_again_is_harmless(client) -> None:
    client.post(FREEZE, json={"scope": "lane", "target": LANE, "reason": "first"})
    again = client.post(FREEZE, json={"scope": "lane", "target": LANE, "reason": "second"})
    assert again.status_code == 200
    assert again.json()["already_frozen"] is True
    assert len(client.get(FREEZE).json()["frozen"]) == 1


def test_the_whole_computer_freezes_every_account_on_it(client) -> None:
    report = client.post(FREEZE, json={"scope": "node", "reason": "the whole machine"})
    assert report.status_code == 200, report.text
    status = client.get(FREEZE).json()
    assert status["node_frozen"] is True
    assert {row["lane"] for row in status["lanes"]} == {lane["id"] for lane in client.get("/api/lanes").json()}


def test_a_computer_scope_names_this_computer_only(client) -> None:
    refused = client.post(FREEZE, json={"scope": "node", "target": "elsewhere", "reason": "x"})
    assert refused.status_code == 400
    assert client.get(FREEZE).json()["node_frozen"] is False


def test_a_freeze_needs_a_reason(client) -> None:
    refused = client.post(FREEZE, json={"scope": "lane", "target": LANE, "reason": ""})
    assert refused.status_code in (400, 422)


def test_the_owner_unfreezes_and_nothing_comes_back(client) -> None:
    client.post(FREEZE, json={"scope": "lane", "target": LANE, "reason": "pause"})
    lifted = client.post(UNFREEZE, json={"scope": "lane", "target": LANE})
    assert lifted.status_code == 200, lifted.text
    assert lifted.json()["unfrozen"] is True and lifted.json()["was_frozen"] is True
    assert "Nothing was restored" in lifted.json()["next"]
    assert client.get(FREEZE).json()["frozen"] == []


def test_lifting_what_is_not_frozen_says_so(client) -> None:
    body = client.post(UNFREEZE, json={"scope": "lane", "target": LANE}).json()
    assert body["unfrozen"] is False and body["was_frozen"] is False


def test_a_wider_freeze_still_covers_an_account_after_its_own_is_lifted(client) -> None:
    client.post(FREEZE, json={"scope": "lane", "target": LANE, "reason": "one"})
    client.post(FREEZE, json={"scope": "node", "reason": "all"})
    body = client.post(UNFREEZE, json={"scope": "lane", "target": LANE}).json()
    assert body["unfrozen"] is True
    assert body["still_frozen_by"]["scope"] == "node"


def test_a_token_cannot_unfreeze(client) -> None:
    client.post(FREEZE, json={"scope": "lane", "target": LANE, "reason": "pause"})
    saved = as_agent(client, TOKEN)
    refused = client.post(UNFREEZE, json={"scope": "lane", "target": LANE})
    assert refused.status_code == 403
    as_owner(client, saved)
    assert [record["target"] for record in client.get(FREEZE).json()["frozen"]] == [LANE]


def test_an_agent_grant_cannot_freeze_or_unfreeze(client) -> None:
    issued = client.post("/api/orgs/local/grants", json={"subject": "helper", "workspaces": ["biz-a"], "ceiling": "commit"})
    assert issued.status_code == 200, issued.text
    saved = as_agent(client, issued.json()["token"])
    frozen = client.post(FREEZE, json={"scope": "lane", "target": LANE, "reason": "not mine to say"})
    assert frozen.status_code == 403
    assert client.post(UNFREEZE, json={"scope": "lane", "target": LANE}).status_code == 403
    as_owner(client, saved)
    assert client.get(FREEZE).json()["frozen"] == []
