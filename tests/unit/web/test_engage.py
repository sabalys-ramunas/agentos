"""The engagement day, programs, caps, holds, items, surfaces and connectors, as the app reads them.

The seeded dev node (agentos.dev.node.seed_engagement) has two accounts with an approved program
(biz-a/social, Instagram, with a rate-limit hold; biz-b/forum, Reddit) and a
draft program waiting for the owner (biz-b/social). The dev node has no Reddit connector, so it queues no
item (one that could never go out is refused); the item tests queue two as if one were installed. Every write is the owner's, in person: a bearer
token, the owner's included, and a member are refused before anything changes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

from starlette.testclient import TestClient

from agentos.api.auth import SESSION_COOKIE
from agentos.api.members import Role
from agentos.engage.caps import Kind

TOKEN = "t" * 43
MEMBER = "m_ana0001"
ORG = "/api/orgs/local"
# The seeded hold began two hours ago, so it is on today's record (UTC, in every zone the seed uses),
# and on yesterday's only near midnight. The holds in force now are read from today.
TODAY = datetime.now(UTC).date().isoformat()


def add_operator(client: TestClient, *, scopes: tuple[str, ...] = ("biz-a",), member: str = MEMBER) -> None:
    """A member with operator in each of `scopes`, as an accepted invite leaves them (test_people's add_member)."""
    api = client.app.state.api
    api.members.store.create_member(
        member_id=member, handle=member[2:5], display_name="Ana", email=f"{member}@example.com", timezone="UTC",
        by="owner", roles=[Role(scope=scope, role="operator", options={}, expires_at=None) for scope in scopes],
        member_ttl_s=None,
    )
    api.members.sync(member, by="owner", force=True)


def as_member(client: TestClient, member: str = MEMBER) -> TestClient:
    """A second client signed in as the member, with a session cookie and its CSRF header."""
    api = client.app.state.api
    sid, session = api.store.create_session(member, 3600, member_id=member, via="passkey")
    member = TestClient(client.app, base_url="http://localhost")
    member.cookies.set(SESSION_COOKIE, sid)
    member.headers["x-agentos-csrf"] = session.csrf
    return member


def as_token(client: TestClient) -> TestClient:
    """The same node, reached with the owner's bearer token: an agent's way in."""
    bearer = TestClient(client.app, base_url="http://localhost")
    bearer.headers["authorization"] = f"Bearer {TOKEN}"
    return bearer


def node_of(client: TestClient) -> Any:
    return client.app.state.api.runtime


def queue_items(node: Any, surface: str = "biz-b/forum") -> None:
    """Two posts queued on the Reddit account, as if its connector were installed (the dev node has none, and
    an item that could never go out is refused: engage.items._can_run). The rest of the check runs."""
    items = node.get("engage").items
    expires = (datetime.now(UTC) + timedelta(days=5)).replace(microsecond=0).isoformat()
    with mock.patch.object(items, "_can_run", lambda *_: None):
        for text, title in (("Our pastry case is restocked.", "Restocked"), ("Free coffee on Saturday.", "Book Saturday")):
            plan = items.check(surface, "post", text, title=title, targets=["r/coffee"], expires_at=expires)
            items.submit(plan, in_person=True, requested_by=None)


def item_of(node: Any, surface: str) -> str:
    if not node.get("engage").items.list(surface):
        queue_items(node, surface)
    return node.get("engage").items.list(surface)[0]["id"]


# ---- the day -----------------------------------------------------------------------------------------


def test_the_day_lists_each_account_with_its_program_and_its_holds(client) -> None:
    day = client.get(f"{ORG}/engage/digest?day={TODAY}").json()
    assert day["available"] is True and day["sessions_on"] is False
    by_surface = {row["surface"]: row for row in day["surfaces"]}
    assert set(by_surface) == {"biz-a/social", "biz-b/forum", "biz-b/social"}
    social = by_surface["biz-a/social"]
    assert social["program"] == {"id": "biz-a-ig-coffee", "version": 1, "approved": True}
    assert [h["signal"] for h in social["holds"] if h["active_now"]] == ["rate_limit"]
    assert {c["kind"] for c in social["caps"]} >= {"like", "comment", "follow"}
    assert by_surface["biz-b/social"]["program"]["approved"] is False  # the draft waits for the owner


def test_one_accounts_day_carries_its_week_as_well(client) -> None:
    one = client.get(f"{ORG}/engage/digest/biz-a/social").json()
    assert one["available"] is True and one["record"]["surface"] == "biz-a/social"
    assert len(one["week"]) == 7


def test_the_day_with_a_bad_date_is_refused(client) -> None:
    refused = client.get(f"{ORG}/engage/digest?day=yesterday")
    assert refused.status_code == 400


# ---- a program --------------------------------------------------------------------------------------


def test_a_program_is_read_with_its_versions_yaml_and_caps(client) -> None:
    program = client.get(f"{ORG}/engage/programs/biz-a/social").json()
    assert program["state"] == "approved" and program["site"] == "instagram" and program["may_change"] is True
    assert client.get(f"{ORG}/engage/programs/biz-a/social").json()["may_change"] is True  # the owner in person
    assert program["program"]["id"] == "biz-a-ig-coffee" and program["program"]["approved_by"] == "owner"
    assert program["program"]["yaml"].startswith("program: biz-a-ig-coffee")
    assert [v["version"] for v in program["versions"]] == [1]
    like = next(c for c in program["caps"] if c["kind"] == "like")
    assert like["manual"] is None and like["hard_ceiling"] == 80 and like["careful_start"] == 10
    draft = client.get(f"{ORG}/engage/programs/biz-b/social").json()
    assert draft["state"] == "draft" and draft["program"]["state"] == "draft"


def test_the_owner_approves_a_draft_program(client) -> None:
    before = client.get(f"{ORG}/engage/programs/biz-b/social").json()
    assert before["state"] == "draft"
    done = client.post(f"{ORG}/engage/programs/biz-b-ig-bakery/approve", json={})
    assert done.status_code == 200, done.text
    assert done.json()["state"] == "approved" and done.json()["program"]["approved_by"] == "owner"
    again = client.post(f"{ORG}/engage/programs/biz-b-ig-bakery/approve", json={"version": 1})
    assert again.status_code == 200 and again.json()["state"] == "approved"  # approving twice changes nothing


def test_an_unknown_program_is_not_found(client) -> None:
    assert client.post(f"{ORG}/engage/programs/no-such-program/approve", json={}).status_code == 404


# ---- caps and holds --------------------------------------------------------------------------------


def test_a_cap_is_set_within_the_hard_ceiling_and_goes_back_to_the_ramp(client) -> None:
    set_it = client.put(f"{ORG}/engage/caps/biz-a/social/like", json={"value": 3})
    assert set_it.status_code == 200, set_it.text
    assert set_it.json() == {"surface": "biz-a/social", "kind": "like", "cap": 3, "hard_ceiling": 80}
    program = client.get(f"{ORG}/engage/programs/biz-a/social").json()
    assert next(c for c in program["caps"] if c["kind"] == "like")["manual"] == 3
    above = client.put(f"{ORG}/engage/caps/biz-a/social/like", json={"value": 999})
    assert above.status_code == 400 and above.json()["error"]["code"] == "cap_ceiling"
    back = client.put(f"{ORG}/engage/caps/biz-a/social/like", json={"value": None})
    assert back.status_code == 200
    program = client.get(f"{ORG}/engage/programs/biz-a/social").json()
    assert next(c for c in program["caps"] if c["kind"] == "like")["manual"] is None


def test_a_kind_that_is_not_an_action_is_refused(client) -> None:
    bad = client.put(f"{ORG}/engage/caps/biz-a/social/teleport", json={"value": 1})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid"


def test_the_owner_clears_a_hold_once(client) -> None:
    cleared = client.post(f"{ORG}/engage/holds/biz-a/social/clear", json={})
    assert cleared.status_code == 200 and cleared.json() == {"surface": "biz-a/social", "cleared": 1}
    day = client.get(f"{ORG}/engage/digest/biz-a/social").json()["record"]
    assert not [h for h in day["holds"] if h["active_now"]]
    assert client.post(f"{ORG}/engage/holds/biz-a/social/clear", json={}).json()["cleared"] == 0


# ---- items -------------------------------------------------------------------------------------------


def test_items_are_listed_with_their_words_for_the_owner_and_one_is_withdrawn(client) -> None:
    queue_items(node_of(client))
    items = client.get(f"{ORG}/engage/items/biz-b/forum").json()
    assert items["available"] is True and len(items["items"]) == 2 and items["surface"] == "biz-b/forum"
    assert all(item["text"] for item in items["items"])  # the owner in person reads its own words
    first = items["items"][0]["id"]
    gone = client.post(f"{ORG}/engage/items/{first}/withdraw", json={})
    assert gone.status_code == 200 and gone.json()["status"] == "withdrawn"
    again = client.post(f"{ORG}/engage/items/{first}/withdraw", json={})
    assert again.status_code == 400  # a withdrawn item is not queued any more


# ---- refusals: a token, and a member who may not ----------------------------------------------------


WRITES = (
    ("POST", "/engage/programs/biz-b-ig-bakery/approve", {}),
    ("PUT", "/engage/caps/biz-a/social/like", {"value": 3}),
    ("POST", "/engage/holds/biz-a/social/clear", {}),
    ("POST", "/engage/items/{item}/withdraw", {}),
)


def test_a_token_is_refused_every_change_and_nothing_changes(client) -> None:
    node = node_of(client)
    item = item_of(node, "biz-b/forum")
    bearer = as_token(client)
    for method, path, body in WRITES:
        r = bearer.request(method, ORG + path.replace("{item}", item), json=body)
        assert r.status_code == 403, (method, path, r.text)
        assert r.json()["error"]["code"] == "denied"
    assert client.get(f"{ORG}/engage/programs/biz-b/social").json()["state"] == "draft"
    assert node.get("engage").items.list("biz-b/forum")[0]["status"] == "queued"
    assert node.get("engage").store.caps("biz-a/social").get(Kind.LIKE) is None
    day = client.get(f"{ORG}/engage/digest/biz-a/social?day={TODAY}").json()["record"]
    assert [h["signal"] for h in day["holds"] if h["active_now"]] == ["rate_limit"]


def test_a_member_who_may_see_the_account_still_cannot_change_it(client) -> None:
    add_operator(client)
    node = node_of(client)
    item = item_of(node, "biz-b/forum")
    member = as_member(client)
    seen = member.get(f"{ORG}/engage/programs/biz-a/social")
    assert seen.status_code == 200 and seen.json()["program"]["id"] == "biz-a-ig-coffee"
    for method, path, body in WRITES:
        r = member.request(method, ORG + path.replace("{item}", item), json=body)
        assert r.status_code == 403, (method, path, r.text)
        assert r.json()["error"]["code"] == "denied"
    assert client.get(f"{ORG}/engage/programs/biz-b/social").json()["state"] == "draft"


def test_a_member_sees_an_items_list_without_its_words(client) -> None:
    add_operator(client, scopes=("biz-a", "biz-b"), member="m_ben0001")
    listed = as_member(client, "m_ben0001").get(f"{ORG}/engage/items/biz-b/forum")
    assert listed.status_code == 200
    assert all(item["text"] is None for item in listed.json()["items"])


def test_a_member_outside_their_workspace_sees_nothing_of_the_account(client) -> None:
    add_operator(client)  # operator in biz-a only
    outside = as_member(client).get(f"{ORG}/engage/programs/biz-b/forum")
    assert outside.status_code == 403 and outside.json()["error"]["code"] == "denied"


# ---- surfaces and connectors -------------------------------------------------------------------------


def test_surfaces_are_listed_as_the_app_reads_them(client) -> None:
    rows = client.get(f"{ORG}/surfaces").json()
    social = next(row for row in rows if row["id"] == "biz-a/social")
    assert social["workspace"] == "biz-a" and social["name"] == "social" and social["site"] == "instagram"
    assert social["lane"] == "biz-a/social" and social["acts_as"]["kind"] == "ig_business"
    one = client.get(f"{ORG}/surfaces/biz-b/forum").json()
    assert one["site"] == "reddit" and one["acts_as"]["kind"] == "account"


def test_a_surface_is_added_and_removed_by_the_owner(client) -> None:
    added = client.post(f"{ORG}/surfaces", json={"id": "biz-a/shop", "site": "instagram", "lane": "biz-a/shop",
                                                  "acts_as": {"kind": "ig_business", "id": "17841400000002",
                                                              "name": "@biza.shop"}})
    assert added.status_code in (200, 201), added.text
    assert added.json()["id"] == "biz-a/shop" and added.json()["workspace"] == "biz-a"
    removed = client.delete(f"{ORG}/surfaces/biz-a/shop")
    assert removed.status_code == 200 and removed.json() == {"deleted": "biz-a/shop"}


def test_a_token_cannot_add_or_remove_a_surface_and_nothing_changes(client) -> None:
    bearer = as_token(client)
    body = {"id": "biz-a/token", "site": "instagram", "lane": "biz-a/social",
            "acts_as": {"kind": "ig_business", "id": "17841400000009", "name": "@token.try"}}
    added = bearer.post(f"{ORG}/surfaces", json=body)
    assert added.status_code == 403 and added.json()["error"]["code"] == "denied", added.text
    removed = bearer.delete(f"{ORG}/surfaces/biz-a/social")
    assert removed.status_code == 403 and removed.json()["error"]["code"] == "denied", removed.text
    assert client.get(f"{ORG}/surfaces/biz-a/social").status_code == 200  # the owner still sees it
    assert client.get(f"{ORG}/surfaces/biz-a/token").status_code == 404  # and nothing was added


def test_a_member_cannot_add_or_remove_a_surface(client) -> None:
    add_operator(client)
    member = as_member(client)
    assert member.get(f"{ORG}/surfaces").status_code == 403
    assert member.delete(f"{ORG}/surfaces/biz-a/social").status_code == 403


def test_connectors_are_listed_and_a_member_cannot_run_one(client) -> None:
    assert client.get(f"{ORG}/connectors").json() == []  # the dev node installs no connector
    add_operator(client)
    run = as_member(client).post(f"{ORG}/connectors/reddit_post_text/run", json={"on": "biz-b/forum"})
    assert run.status_code == 403 and run.json()["error"]["code"] == "denied"
