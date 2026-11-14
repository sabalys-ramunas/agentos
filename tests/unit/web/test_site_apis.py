"""Site APIs and their keys on a node, as the app reads them (web/src/api/types.ts). Each answer is what the
node really knows, and each action is the node's own route, so a refusal here is the node's refusal."""

from __future__ import annotations

import copy

from agentos.api.web.site_apis import try_answer
from agentos.runner.siteapis.models import SAMPLE

API = "harbor-supplier"
LANE = "biz-a/shop"
BASE = "/api/orgs/local/site-apis"
KEYS = "/api/orgs/local/site-keys"
TOKEN = "t" * 43


def save(client) -> None:
    """A draft on the node, saved by the owner in person: the node's own route, not the contract's."""
    doc = copy.deepcopy(SAMPLE)
    doc["surfaces"] = [LANE]
    saved = client.post("/api/site-apis", json=doc)
    assert saved.status_code == 201, saved.text


def live(client) -> None:
    """Saved, then made live by the owner: keys can only be scoped to APIs that are live on this node."""
    save(client)
    assert client.post(f"{BASE}/{API}/activate", json={"kind": "activate"}).status_code == 200


def test_a_node_with_no_site_api_says_so(client) -> None:
    assert client.get(BASE).json() == []
    assert client.get(KEYS).json() == []


def test_a_draft_is_proposed_and_not_yet_read(client) -> None:
    save(client)
    [item] = client.get(BASE).json()
    assert item["key"] == API
    assert item["title"] == "Harbor supplier portal"
    assert item["site"] == {"name": "portal.northsupply.example", "domain": "portal.northsupply.example"}
    assert item["workspace"] == "biz-a" and item["lane"] == LANE
    assert item["stage"] == "proposed"
    assert item["health"] == "unread"
    assert item["problem"] is None
    assert item["read_at"] is None
    assert item["calls_7d"] == 0 and item["cached_pct"] is None
    assert len(item["spark"]) == 28
    assert item["sample"]["kind"] == "rows" and item["sample"]["rows"] == []


def test_one_api_shows_its_endpoints_and_no_invented_page(client) -> None:
    save(client)
    detail = client.get(f"{BASE}/{API}").json()
    [endpoint] = detail["endpoints"]
    assert endpoint["key"] == "open_orders"
    assert endpoint["title"] == "Open orders"
    assert endpoint["effect"] == "read"
    assert endpoint["cache_s"] == 300
    assert endpoint["pace_per_hour"] == 60
    assert endpoint["expect_min"] == 1
    assert endpoint["page"] is None and endpoint["total"] is None and endpoint["rows"] == []
    assert endpoint["p50_ms"] is None
    [status] = endpoint["input"]
    assert status == {"key": "status", "label": "status", "type": "choice", "required": False,
                      "options": [{"value": "open", "label": "open"}, {"value": "late", "label": "late"}],
                      "default": "open"}
    assert {field["key"] for field in endpoint["fields"]} == {"number", "total", "url"}
    assert detail["keep_updated"] == "off"
    assert detail["computer"] == "dev"
    assert detail["pace"]["per_hour"] == 60 and detail["pace"]["used"] == 0 and detail["pace"]["others"] == []


def test_a_missing_api_is_not_found(client) -> None:
    assert client.get(f"{BASE}/nothing-here").status_code == 404
    assert client.get(f"{BASE}/nothing-here/usage").status_code == 404
    assert client.post(f"{BASE}/nothing-here/pause", json={"kind": "pause"}).status_code == 404


def test_usage_is_a_week_of_hours_even_for_a_draft(client) -> None:
    save(client)
    hours = client.get(f"{BASE}/{API}/usage").json()
    assert len(hours) == 168
    assert set(hours[0]) == {"at", "calls"}
    assert hours[0]["calls"] == {"open_orders": {"cached": 0, "fresh": 0, "errors": 0}}


def test_discover_without_a_lane_is_nothing(client) -> None:
    assert client.get(f"{BASE}/discover").json() is None


def test_discover_on_a_lane_that_is_not_there_is_refused(client) -> None:
    refused = client.get(f"{BASE}/discover", params={"lane": "nowhere/at-all"})
    assert refused.status_code == 404


def test_activate_then_pause_then_resume(client) -> None:
    save(client)
    live = client.post(f"{BASE}/{API}/activate", json={"kind": "activate"})
    assert live.status_code == 200, live.text
    assert live.json()["stage"] == "active"

    paused = client.post(f"{BASE}/{API}/pause", json={"kind": "pause"}).json()
    assert paused["stage"] == "paused" and paused["health"] == "paused"
    assert paused["problem"]["since"]

    woken = client.post(f"{BASE}/{API}/resume", json={"kind": "resume"})
    assert woken.status_code == 200, woken.text
    assert woken.json()["stage"] == "active"


def test_resume_needs_a_paused_api(client) -> None:
    save(client)
    refused = client.post(f"{BASE}/{API}/resume", json={"kind": "resume"})
    assert refused.status_code == 400
    assert "not paused" in refused.json()["error"]["message"]


def test_a_draft_cannot_be_paused(client) -> None:
    save(client)
    refused = client.post(f"{BASE}/{API}/pause", json={"kind": "pause"})
    assert refused.status_code == 400


def test_decline_needs_a_proposal(client) -> None:
    save(client)
    refused = client.post(f"{BASE}/{API}/decline", json={"kind": "decline"})
    assert refused.status_code == 400
    assert "nobody has proposed" in refused.json()["error"]["message"]


def test_declining_a_proposal_nobody_turned_on_removes_the_draft(client, node) -> None:
    save(client)
    svc = node.require("site_apis")
    draft = svc.drafts.get(API)
    draft.proposal = {"approval_id": "ap_not_on_this_node", "digest": draft.digest}
    draft.state = "proposed"
    svc.drafts.put(draft)
    assert client.get(f"{BASE}/{API}").json()["proposal"]["steps"]

    declined = client.post(f"{BASE}/{API}/decline", json={"kind": "decline"})
    assert declined.status_code == 204
    assert client.get(f"{BASE}/{API}").status_code == 404


def test_repair_is_refused_plainly(client) -> None:
    save(client)
    refused = client.post(f"{BASE}/{API}/repair", json={"kind": "repair"})
    assert refused.status_code == 400
    assert "not repaired from here" in refused.json()["error"]["message"]


def test_keep_updated_is_refused_plainly(client) -> None:
    save(client)
    refused = client.post(f"{BASE}/{API}/keep-updated", json={"kind": "keep-updated", "every": "hourly"})
    assert refused.status_code == 400
    assert "does not keep a Site API up to date" in refused.json()["error"]["message"]


def test_accept_empty_lowers_the_minimum_and_activates_it(client) -> None:
    save(client)
    accepted = client.post(f"{BASE}/{API}/accept-empty", json={"kind": "accept-empty"})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["endpoints"][0]["expect_min"] == 0
    assert accepted.json()["stage"] == "active"


def test_accept_empty_on_a_paused_api_is_refused(client) -> None:
    save(client)
    client.post(f"{BASE}/{API}/activate", json={"kind": "activate"})
    client.post(f"{BASE}/{API}/pause", json={"kind": "pause"})
    refused = client.post(f"{BASE}/{API}/accept-empty", json={"kind": "accept-empty"})
    assert refused.status_code == 400


def test_a_finished_run_is_a_caller_answer_with_its_data(client) -> None:
    answer = try_answer(API, "open_orders", {"run": "run_1", "status": "succeeded_verified", "ms": 412,
                                             "data": {"orders": [{"number": "A-1"}]}, "untrusted_fields": ["orders[].number"],
                                             "private_fields": [], "truncated": False, "problem": None})
    assert answer["status"] == 200 and answer["ms"] == 412
    assert answer["headers"] == {"Agentos-Run": "run_1"}
    assert answer["body"]["data"] == {"orders": [{"number": "A-1"}]}
    assert answer["body"]["cache"] == {"hit": False, "age_s": 0}
    assert answer["body"]["untrusted_fields"] == ["orders[].number"]


def test_a_run_that_ended_without_a_result_says_what_a_caller_gets(client) -> None:
    problem = {"type": "urn:agentos:site-api:needs_human", "title": "A person is needed", "status": 503,
               "code": "needs_human", "detail": "A person is needed.", "run": "run_2"}
    answer = try_answer(API, "open_orders", {"run": "run_2", "status": "failed", "ms": 88, "problem": problem})
    assert answer["status"] == 503 and answer["body"] == problem


def test_a_run_still_going_is_pending_not_an_empty_answer(client) -> None:
    answer = try_answer(API, "open_orders", {"run": "run_3", "status": "queued", "ms": None, "problem": None})
    assert answer["status"] == 202 and answer["ms"] == 0
    assert answer["body"] == {"code": "pending", "status": "queued", "run": "run_3"}


def test_a_try_on_a_missing_endpoint_is_refused(client) -> None:
    live(client)
    assert client.post(f"{BASE}/{API}/try", json={"endpoint": "nope", "params": {}}).status_code == 404


def test_a_key_is_shown_once_and_listed_without_its_secret(client) -> None:
    live(client)
    created = client.post(KEYS, json={"label": "Shop reader", "apis": [API], "endpoints": None, "ceiling": "read",
                                      "rate_per_min": 60, "expires_at": None})
    assert created.status_code == 201, created.text
    made = created.json()
    assert made["secret"].startswith("aos_")
    assert made["label"] == "Shop reader" and made["apis"] == [API]

    [listed] = client.get(KEYS).json()
    assert listed["id"] == made["id"]
    assert "secret" not in listed
    assert listed["prefix"] == made["prefix"]


def test_a_revoked_key_leaves_the_list(client) -> None:
    live(client)
    made = client.post(KEYS, json={"label": "Gone soon", "apis": [API], "endpoints": None, "ceiling": "read",
                                   "rate_per_min": 60, "expires_at": None}).json()
    assert client.delete(f"{KEYS}/{made['id']}").status_code == 204
    assert client.get(KEYS).json() == []


def test_a_key_needs_a_known_ceiling_and_a_person(client) -> None:
    live(client)
    bad = client.post(KEYS, json={"label": "x", "apis": [API], "endpoints": None, "ceiling": "pay",
                                  "rate_per_min": 60, "expires_at": None})
    assert bad.status_code == 400


def test_a_token_cannot_make_or_revoke_a_key(client) -> None:
    client.cookies.clear()
    client.headers["authorization"] = "Bearer " + TOKEN
    made = client.post(KEYS, json={"label": "By a token", "apis": [API], "endpoints": None, "ceiling": "read",
                                   "rate_per_min": 60, "expires_at": None})
    assert made.status_code == 403
    assert client.delete(f"{KEYS}/key_none").status_code == 403


def test_a_token_cannot_activate_an_api(client) -> None:
    save(client)
    client.cookies.clear()
    client.headers["authorization"] = "Bearer " + TOKEN
    assert client.post(f"{BASE}/{API}/activate", json={"kind": "activate"}).status_code == 403


def test_a_read_starts_each_endpoint_that_needs_no_input_and_answers_at_once(client) -> None:
    live(client)
    started = client.post(f"{BASE}/{API}/read", json={"kind": "read"})
    assert started.status_code == 200, started.text
    assert started.json()["endpoints"][0]["key"] == "open_orders"
