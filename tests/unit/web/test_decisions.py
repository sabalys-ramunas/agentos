"""Needs you on a node: every decision the app makes goes through the node's own route and comes back in
the app's shape (web/src/api/client.ts, types.ts). Each route is tried once, and each refusal the node
gives is passed on as it is (the app shows the words, and nothing is decided)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

ORG = "/api/orgs/local"


def _in_days(days: int) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat().replace("+00:00", "Z")


def _pending_action(client) -> dict:
    [item] = [a for a in client.get(f"{ORG}/approvals").json() if a["state"] == "pending"]
    return item


def test_needs_you_lists_actions_only_and_in_the_app_shape(client) -> None:
    items = client.get(f"{ORG}/approvals").json()
    assert items, "the seed has a pending action"
    for item in items:
        assert item["kind"] == "action"
        assert isinstance(item["effect"], str) and isinstance(item["content_hash"], str)
        assert isinstance(item["lane"], str)
        assert item["needs_you"] is True and item["undo_until"] is None
    questions = client.get("/api/questions").json()
    assert questions, "the seed has an open question, asked on /questions and not here"
    assert all(q["id"] not in {i["id"] for i in items} for q in questions)


def test_one_approval_and_a_question_is_not_one(client) -> None:
    item = _pending_action(client)
    assert client.get(f"{ORG}/approvals/{item['id']}").json()["id"] == item["id"]
    question = client.get("/api/questions").json()[0]
    assert client.get(f"{ORG}/approvals/{question['id']}").status_code == 404


def test_a_stale_content_hash_is_refused_and_nothing_is_decided(client) -> None:
    item = _pending_action(client)
    refused = client.post(f"{ORG}/approvals/{item['id']}/approve", json={"content_hash": "sha256:stale"})
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "conflict"
    assert client.get(f"{ORG}/approvals/{item['id']}").json()["state"] == "pending"


def test_deny_decides_and_the_app_reads_the_decision(client) -> None:
    item = _pending_action(client)
    denied = client.post(f"{ORG}/approvals/{item['id']}/deny", json={"content_hash": item["content_hash"]})
    assert denied.status_code == 200, denied.text
    body = denied.json()
    assert body["state"] == "denied" and body["id"] == item["id"] and body["lane"] == item["lane"]
    assert body["needs_you"] is False and body["outcome"] is None


def test_approve_with_the_hash_on_screen_decides_it(client) -> None:
    item = _pending_action(client)
    approved = client.post(f"{ORG}/approvals/{item['id']}/approve", json={"content_hash": item["content_hash"]})
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "approved"


def test_a_batch_answers_each_item_and_refuses_the_stale_one(client) -> None:
    items = [i for i in client.get(f"{ORG}/approvals").json() if i["state"] == "pending"]
    [first] = items[:1]
    reply = client.post(f"{ORG}/approvals/batch", json={
        "ids": [first["id"], "ap_missing"], "decision": "deny",
        "content_hashes": {first["id"]: first["content_hash"], "ap_missing": "sha256:x"},
    })
    assert reply.status_code == 200, reply.text
    by_id = {row["id"]: row for row in reply.json()}
    assert by_id[first["id"]]["ok"] is True and by_id[first["id"]]["state"] == "denied"
    assert by_id["ap_missing"]["ok"] is False and by_id["ap_missing"]["error"]["code"]


def test_clearing_says_got_it_only_to_an_approved_action(client) -> None:
    item = _pending_action(client)
    refused = client.post(f"{ORG}/approvals/{item['id']}/clear")
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "invalid"
    client.post(f"{ORG}/approvals/{item['id']}/approve", json={"content_hash": item["content_hash"]})
    cleared = client.post(f"{ORG}/approvals/{item['id']}/clear")
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["needs_you"] is False


def test_undo_is_refused_because_a_decision_stands_on_this_computer(client) -> None:
    item = _pending_action(client)
    refused = client.post(f"{ORG}/approvals/{item['id']}/undo")
    assert refused.status_code >= 400
    assert refused.json()["error"]["code"] == "unavailable"
    # the fix is for this decision, in plain words, and not the catalog's line about setting a feature up
    assert refused.json()["error"]["fix"] and "vault" not in refused.json()["error"]["fix"]


def test_failed_outcomes_are_an_empty_list_until_a_run_fails(client) -> None:
    assert client.get(f"{ORG}/approvals", params={"outcome": "needs_you"}).json() == []


def test_a_review_link_without_its_signature_is_refused_and_a_signed_in_person_reads_the_approval(client) -> None:
    item = _pending_action(client)
    read = client.get(f"{ORG}/approvals/{item['id']}/review")
    assert read.status_code == 200 and read.json()["id"] == item["id"]
    bad = client.get(f"{ORG}/approvals/{item['id']}/review", params={"e": "1", "s": "nope"})
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "unauthorized"


def test_the_passkey_check_for_a_heavy_yes_needs_a_passkey_first(client) -> None:
    item = _pending_action(client)
    refused = client.post(f"{ORG}/approvals/{item['id']}/proof/options")
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "conflict"


def test_a_passkey_check_that_is_not_for_this_approval_is_refused(client) -> None:
    item = _pending_action(client)
    refused = client.post(f"{ORG}/approvals/{item['id']}/proof", json={"id": "nope"})
    assert refused.status_code >= 400
    assert refused.json()["error"]["code"] in {"unauthorized", "invalid"}


def test_standing_approvals_are_listed_in_the_app_shape_and_removed(client, node) -> None:
    """A commit standing approval must name what it covers and the content the owner saw (the node's rule).
    The seed has no such content, so this one is made in the node's own store, as the approval path makes it."""
    digest = "sha256:" + "ab" * 32
    made = node.get("approvals").add_standing(effects=["commit"], workspaces=["biz-a"], lanes=None,
                                              until=datetime.now(UTC).timestamp() + 7 * 86400, count=3,
                                              by="owner:panel", targets=["tool:browser_act"], contents=[digest])
    [body] = [row for row in client.get(f"{ORG}/standing-approvals").json() if row["id"] == made.id]
    assert body["workspace"] == "biz-a" and body["count"] == 3 and body["used"] == 0
    assert body["state"] == "active" and body["lanes"] is None and body["contents"] == 1
    assert body["covers_publishing"] is True and body["enforcement"]["state"] == "applied"
    removed = client.delete(f"{ORG}/standing-approvals/{made.id}")
    assert removed.status_code == 200 and removed.json() == {"ok": True}
    everything = client.get(f"{ORG}/standing-approvals", params={"all": "true"}).json()
    assert next(row for row in everything if row["id"] == made.id)["state"] == "revoked"
    assert made.id not in {row["id"] for row in client.get(f"{ORG}/standing-approvals").json()}


def test_a_standing_approval_that_names_nothing_it_covers_is_refused_as_the_node_refuses_it(client) -> None:
    refused = client.post(f"{ORG}/standing-approvals", json={
        "workspace": "biz-a", "effect": "commit", "count": 3, "until": _in_days(7), "targets": [], "from_approvals": [],
    })
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "invalid"


def test_a_standing_approval_must_name_its_accounts_and_a_real_end(client) -> None:
    no_scope = client.post(f"{ORG}/standing-approvals", json={"effect": "commit", "count": 1, "until": _in_days(7)})
    assert no_scope.status_code == 400
    no_end = client.post(f"{ORG}/standing-approvals", json={"workspace": "biz-a", "effect": "commit", "count": 1, "until": "soon"})
    assert no_end.status_code == 400


def test_support_says_whether_a_workspace_has_accounts_for_it(client) -> None:
    covered = client.get(f"{ORG}/standing-approvals/support", params={"workspace": "biz-a"}).json()
    assert covered["state"] == "applied"
    empty = client.get(f"{ORG}/standing-approvals/support", params={"workspace": "nowhere"}).json()
    assert empty["state"] == "no_lanes"


def test_ask_first_is_read_and_set_per_account_and_per_routine(client) -> None:
    set_lane = client.put(f"{ORG}/lanes/biz-a/social/ask-first", json={"ask_first": True})
    assert set_lane.status_code == 200, set_lane.text
    assert set_lane.json()["lane"] == "biz-a/social" and set_lane.json()["ask_first"] is True
    listed = client.get(f"{ORG}/ask-first").json()
    assert "biz-a/social" in {row["lane"] for row in listed}
    set_flow = client.put(f"{ORG}/flows/biz-a.social.post/ask-first", json={"ask_first": True})
    assert set_flow.status_code == 200, set_flow.text
    assert set_flow.json()["id"] == "biz-a.social.post"


def test_ask_first_for_a_routine_that_is_not_there_is_refused(client) -> None:
    refused = client.put(f"{ORG}/flows/not%20a%20flow/ask-first", json={"ask_first": True})
    assert refused.status_code == 400


def test_the_confirmation_setting_reads_and_changes_in_the_app_shape(client) -> None:
    view = client.get(f"{ORG}/confirmation").json()
    assert set(view) >= {"setting", "system", "overlays", "day", "clients"}
    setting = view["setting"]
    assert setting["chat"] is True
    setting["chat"] = False
    saved = client.put(f"{ORG}/confirmation", json={"setting": setting})
    assert saved.status_code == 200, saved.text
    assert saved.json()["setting"]["chat"] is False
    setting["chat"] = True
    client.put(f"{ORG}/confirmation", json={"setting": setting})


def test_saving_the_setting_in_the_app_keeps_telegram_as_the_node_holds_it(client, node) -> None:
    node.get("confirmation").change({"methods_off": ["telegram"]}, by="owner:cli", role="owner")
    setting = client.get(f"{ORG}/confirmation").json()["setting"]
    setting["chat"] = False
    assert client.put(f"{ORG}/confirmation", json={"setting": setting}).status_code == 200
    assert set(node.get("confirmation").current().methods_off) == {"chat", "telegram"}
    setting["chat"] = True
    assert client.put(f"{ORG}/confirmation", json={"setting": setting}).status_code == 200
    assert node.get("confirmation").current().methods_off == ["telegram"]


def test_saving_one_workspaces_overlay_keeps_the_others_and_what_is_turned_off_in_it(client, node) -> None:
    store = node.get("confirmation")
    store.change({"workspaces": {"biz-a": {"methods_off": ["telegram"]},
                                 "biz-b": {"commit": {"when": "ask", "level": "chat"}}}}, by="owner:cli", role="owner")
    overlay = {"commit": {"when": "ask", "level": "signed_in"}}
    saved = client.put(f"{ORG}/confirmation", json={"business": "biz-a", "overlay": overlay})
    assert saved.status_code == 200, saved.text
    held = store.own().workspaces
    assert held["biz-a"].methods_off == ["telegram"]  # Telegram stays off where it was turned off
    assert held["biz-a"].commit is not None and held["biz-a"].commit.level == "signed_in"
    assert held["biz-b"].commit is not None and held["biz-b"].commit.when == "ask"  # the other workspace stays
    assert set(saved.json()["overlays"]) >= {"biz-a", "biz-b"}


def test_a_setting_that_leaves_a_row_out_is_refused_before_it_is_read(client) -> None:
    refused = client.put(f"{ORG}/confirmation", json={"setting": {"commit": {"when": "ask", "level": "chat"}}})
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "invalid"


def test_pay_cannot_be_lowered_below_its_floor(client) -> None:
    setting = client.get(f"{ORG}/confirmation").json()["setting"]
    setting["pay"] = {"when": "ask", "level": "chat"}
    refused = client.put(f"{ORG}/confirmation", json={"setting": setting})
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "denied"


def test_the_confirmation_methods_say_which_are_yours_and_ready(client) -> None:
    methods = client.get(f"{ORG}/confirmation/methods").json()
    panel = next(m for m in methods if m["id"] == "panel")
    assert panel["yours"] is True and panel["status"] == "ready" and panel["detail"] == "This computer"
    assert {"last_used_at", "level", "status", "yours", "detail"} <= set(panel)
    chat = next(m for m in methods if m["id"] == "chat")
    assert chat["yours"] is False


def test_a_grant_is_issued_with_its_token_once_and_listed_without_it(client) -> None:
    issued = client.post(f"{ORG}/grants", json={
        "subject": "claude-code", "workspaces": ["biz-a"], "ceiling": "draft", "specials": [], "ttl": "7d",
    })
    assert issued.status_code == 200, issued.text
    body = issued.json()
    assert body["id"] and body["token"]
    listed = client.get(f"{ORG}/grants").json()
    mine = next(g for g in listed if g["id"] == body["id"])
    assert mine["subject"] == "claude-code" and mine["workspaces"] == ["biz-a"]
    assert "token" not in mine
    assert client.post(f"{ORG}/grants/{body['id']}/revoke").status_code == 200


def test_a_budget_is_refused_here_because_the_computer_keeps_it(client) -> None:
    refused = client.post(f"{ORG}/grants", json={
        "subject": "claude-code", "workspaces": ["biz-a"], "ceiling": "draft", "specials": [], "ttl": "7d",
        "budget": "10 EUR",
    })
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "invalid"
