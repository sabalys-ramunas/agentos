"""Brand: product and business profiles as the app's Brand page and Needs you read them (agentos.api.web.brand).

Through the node's own profile routes, so only the owner in person reads and decides. An agent's proposal is
saved by the profiles service as it is from profile_propose: a draft with its reason and a readable diff.
"""

from __future__ import annotations

from typing import Any

from starlette.testclient import TestClient

from agentos.core.models import Principal

BRAND = "/api/orgs/local/brand"
TOKEN = "t" * 43
AGENT = Principal(kind="agent", id="claude-code")

SHOP = {
    "profile": "shop", "workspace": "harbor", "kind": "product", "display_name": "Harbor Supply shop",
    "headline": "Boat parts, shipped in a day",
    "brand": {
        "tokens": {"color": {"$type": "color", "accent": {"$value": "#1F5FAD"}, "ink": {"$value": "{color.accent}"}},
                   "space": {"$type": "dimension", "2": {"$value": "8px"}}},
        "fonts": [{"family": "Inter", "role": "body", "weights": [400, 600]}],
        "components": [{"name": "Button", "attr": "data-slot=button"}],
        "widths": [360, 1280],
    },
    "guidelines": [{"id": "contrast", "text": "Body text is easy to read", "check": "contrast.text", "threshold": 4.5}],
    "references": [{"name": "Linear", "url": "https://linear.app", "note": "the density we want"}],
    "environments": [{"name": "staging", "url": "https://staging.harbor.example", "keys": {"stripe": "harbor/stripe"}}],
}
PERSON = {"profile": "ada", "workspace": "harbor", "kind": "person", "display_name": "Ada"}


def save(client: TestClient, body: dict[str, Any]) -> None:
    saved = client.post("/api/profiles", json=body)
    assert saved.status_code == 200, saved.text


def propose(node: Any) -> dict[str, Any]:
    return node.get("profiles").propose("harbor/shop", {"brand": {"tokens": {"color": {"accent": {"$value": "#0E7C66"}}}}},
                                        "The blue accent fails contrast on the dark header", by=AGENT)


def as_token(client: TestClient) -> None:
    client.cookies.clear()
    client.headers.pop("x-agentos-csrf", None)
    client.headers["authorization"] = f"Bearer {TOKEN}"


def test_only_product_and_business_profiles_are_brands(client: TestClient) -> None:
    save(client, SHOP)
    save(client, PERSON)
    body = client.get(BRAND).json()
    assert body["available"] is True
    assert [p["ref"] for p in body["profiles"]] == ["harbor/shop"]
    row = body["profiles"][0]
    assert row["kind"] == "product" and row["draft_waiting"] is None and row["state"] == "approved"
    assert client.get(f"{BRAND}/harbor/ada").status_code == 400


def test_a_brand_has_flat_tokens_guidelines_references_and_no_vault_names(client: TestClient) -> None:
    save(client, SHOP)
    body = client.get(f"{BRAND}/harbor/shop").json()
    tokens = {t["name"]: t for t in body["brand"]["tokens"]}
    assert tokens["color.accent"]["type"] == "color" and tokens["color.accent"]["value"].lower() == "#1f5fad"
    assert tokens["color.ink"]["value"] == "{color.accent}" and tokens["space.2"]["value"] == "8px"
    assert body["brand"]["components"] == [{"name": "Button", "attr": "data-slot=button", "class_pattern": None}]
    assert body["guidelines"][0]["id"] == "contrast" and body["references"][0]["name"] == "Linear"
    assert body["environments"] == [{"name": "staging", "url": "https://staging.harbor.example"}]
    assert "harbor/stripe" not in str(body)
    assert body["draft"] is None


def test_a_proposal_waits_with_its_reason_and_diff_and_is_in_needs_you(client: TestClient, node: Any) -> None:
    save(client, SHOP)
    proposed = propose(node)
    body = client.get(f"{BRAND}/harbor/shop").json()
    assert body["draft_waiting"] == proposed["version"] and body["version"] == 1
    draft = body["draft"]
    assert draft["version"] == proposed["version"] and draft["reason"].startswith("The blue accent")
    assert any("color.accent" in line for line in draft["diff"]) and draft["created_by"] == "claude-code"
    proposed_tokens = {t["name"]: t["value"] for t in draft["tokens"]}
    assert "color.accent" in proposed_tokens and isinstance(draft["guidelines"], list)
    [waiting] = client.get(f"{BRAND}/proposals").json()
    assert waiting["id"] == f"brand:harbor/shop@{proposed['version']}" and waiting["kind"] == "brand"
    assert waiting["reason"] == draft["reason"] and waiting["diff"] == draft["diff"]


def test_the_owner_approves_the_version_read(client: TestClient, node: Any) -> None:
    save(client, SHOP)
    version = propose(node)["version"]
    assert client.post(f"{BRAND}/harbor/shop/approve", json={}).status_code == 400
    approved = client.post(f"{BRAND}/harbor/shop/approve", json={"version": version})
    assert approved.status_code == 200, approved.text
    assert approved.json() == {"ref": "harbor/shop", "version": version, "state": "approved"}
    assert client.get(f"{BRAND}/proposals").json() == []
    assert client.get(f"{BRAND}/harbor/shop").json()["version"] == version


def test_the_owner_rejects_with_a_note(client: TestClient, node: Any) -> None:
    save(client, SHOP)
    version = propose(node)["version"]
    rejected = client.post(f"{BRAND}/harbor/shop/reject", json={"version": version, "note": "Keep the blue"})
    assert rejected.status_code == 200 and rejected.json()["state"] == "rejected"
    assert client.get(f"{BRAND}/harbor/shop").json()["draft"] is None


def test_a_token_is_refused_proposals_included(client: TestClient, node: Any) -> None:
    save(client, SHOP)
    propose(node)
    as_token(client)
    assert client.get(BRAND).status_code == 403
    assert client.get(f"{BRAND}/harbor/shop").status_code == 403
    assert client.post(f"{BRAND}/harbor/shop/approve", json={"version": 2}).status_code == 403
    assert client.get(f"{BRAND}/proposals").status_code == 403  # the app reads this as none for you
