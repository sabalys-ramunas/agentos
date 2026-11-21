"""Surfaces and connectors, as the app reads them (openajar.api.web.surfaces).

The seeded dev node (openajar.dev.node.seed_surfaces) connects two accounts: biz-a/social (Instagram,
as a business account) and biz-b/forum (Reddit). Every write is the owner's, in person: a bearer
token, the owner's included, and a member are refused before anything changes.
"""

from __future__ import annotations

from .test_environments import add_operator, as_member, as_token

ORG = "/api/orgs/local"


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
