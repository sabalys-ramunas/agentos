"""The day plan on a node, as the app reads it on Home: one view across accounts and programs, and a draft.

Both are reads. Nothing here queues, sends or changes an account, and the draft says so about itself.
"""

from __future__ import annotations

import pytest

from agentos.core.models import LaneSpec
from agentos.node.store import LaneStore

TOKEN = "t" * 43


@pytest.fixture(autouse=True)
def accounts_on_disk(node):
    """The plan reads the lanes the node keeps on disk, which a dev node writes as it creates them."""
    store = LaneStore(node.get("config").db_path)
    for lane in ("biz-a/social", "biz-b/social"):
        store.add(LaneSpec(id=lane))
    yield


def test_the_status_lists_every_account_with_its_readiness(client) -> None:
    state = client.get("/api/orgs/local/plan/status").json()
    assert {"at", "today", "sessions_on", "lanes", "unattached_approvals", "unavailable"} <= state.keys()
    assert state["lanes"], "the seeded node has accounts, and the plan lists them"
    for row in state["lanes"]:
        assert row["lane"].count("/") == 1
        assert row["readiness"] in {"ready", "human", "frozen", "quarantined", "needs_human", "paused",
                                    "backoff", "connector_broken", "no_lane"}
        assert set(row["pending"]) == {"approvals", "runs", "broken"}


def test_the_status_narrows_to_one_workspace(client) -> None:
    everything = client.get("/api/orgs/local/plan/status").json()
    biz = client.get("/api/orgs/local/plan/status", params={"workspace": "biz-a"}).json()
    assert len(biz["lanes"]) <= len(everything["lanes"])
    assert all("biz-a" in row["workspaces"] for row in biz["lanes"])


def test_the_draft_is_a_draft_that_queues_nothing(client) -> None:
    plan = client.get("/api/orgs/local/plan/draft").json()
    assert plan["status"] == "draft" and plan["dry_run"] in (True, False)
    assert set(plan) >= {"day", "status", "dry_run", "items", "content", "unavailable"}
    assert plan["content"] == [], "no content is drafted until a brain writes it"
    for item in plan["items"]:
        assert item["section"] in {"blocked", "queue", "due", "programs", "upkeep"}
        assert item["text"].endswith(".")
        assert "\u2014" not in item["text"]
        if item["section"] == "blocked":
            # the screen says a blocked account in its own words, from this key; the text is the CLI's sentence
            assert item["readiness"] in {"frozen", "quarantined", "needs_human", "paused", "human", "backoff", "no_lane"}


def test_a_token_cannot_read_the_day_plan_because_it_is_a_persons_view(client) -> None:
    # agents hold the node's token, so the day's plan is read in the panel by a person, never by a token
    for url in ("/api/orgs/local/plan/status", "/api/orgs/local/plan/draft"):
        response = client.get(url, headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.status_code == 403, url
        assert response.json()["error"]["code"] == "denied"
