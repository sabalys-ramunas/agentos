"""The routes the app reads that have no node route under them: retention and the export, push,
billing and webhooks. Each answer is what the node really has, and each route refuses what a
non-owner may not do.
"""

from __future__ import annotations

import csv
import io
import json

import pytest

from openajar.policy.traces import TraceEvent

TOKEN = "t" * 43


def as_token(client) -> None:
    """Call as the bearer token, which is never the owner's session."""
    client.cookies.clear()
    client.headers["authorization"] = "Bearer " + TOKEN


def write_trace(node, summary: str, **fields) -> None:
    fields = {"workspace": "biz-a", "lane": "biz-a/social", **fields}
    node.get("traces").write(TraceEvent(kind="tool_end", tool="browser.navigate", status="ok", summary=summary,
                                        **fields))
    node.get("traces").flush()


def test_retention_is_the_window_the_sweep_applies(client, node) -> None:
    answer = client.get("/api/orgs/local/retention")
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["retention_days"] == node.get("traces").retention_days == 90
    assert body["kept_days"] == body["retention_days"]
    assert body["hold_until"] is None
    assert "plan" not in body  # a computer has no plan
    assert body["can_export"] is True


def test_retention_says_when_the_oldest_row_was_written(client, node) -> None:
    before = client.get("/api/orgs/local/retention").json()["oldest_at"]
    write_trace(node, "first thing")
    after = client.get("/api/orgs/local/retention").json()["oldest_at"]
    assert after is not None and after != before


def test_a_token_sees_the_window_but_cannot_export(client) -> None:
    """A token is not a person, the owner's included: it may read the window, never the history."""
    as_token(client)
    body = client.get("/api/orgs/local/retention").json()
    assert body["can_export"] is False
    refused = client.get("/api/orgs/local/activity/export?format=csv")
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "denied"


def test_the_export_is_the_action_log_as_csv(client, node) -> None:
    write_trace(node, "posted the photo")
    write_trace(node, "=HYPERLINK(\"https://evil.example\")")
    answer = client.get("/api/orgs/local/activity/export?format=csv")
    assert answer.status_code == 200, answer.text
    assert answer.headers["content-type"].startswith("text/csv")
    assert answer.headers["content-disposition"].startswith('attachment; filename="openajar-activity-local-')
    rows = list(csv.DictReader(io.StringIO(answer.text)))
    assert [row["summary"] for row in rows[:2]] == ["posted the photo", "'=HYPERLINK(\"https://evil.example\")"]
    assert rows[-1]["summary"] == "exported the action log as csv"  # the export is itself in the log
    assert set(rows[0]) == {"id", "at", "kind", "tool", "lane", "workspace", "principal", "effect", "status",
                            "summary", "run_id"}


def test_the_export_as_ndjson_has_one_row_per_line(client, node) -> None:
    write_trace(node, "one")
    write_trace(node, "two")
    answer = client.get("/api/orgs/local/activity/export?format=ndjson")
    assert answer.headers["content-type"] == "application/x-ndjson"
    lines = [json.loads(line) for line in answer.text.splitlines()]
    assert [row["summary"] for row in lines[:2]] == ["one", "two"]


def test_the_export_is_filtered_by_time_and_workspace(client, node) -> None:
    write_trace(node, "in biz-a")
    write_trace(node, "in biz-b", workspace="biz-b")
    only_b = client.get("/api/orgs/local/activity/export?format=ndjson&workspace=biz-b").text.splitlines()
    assert [json.loads(line)["summary"] for line in only_b] == ["in biz-b"]
    future = client.get("/api/orgs/local/activity/export?format=ndjson&start=2999-01-01T00:00:00.000Z").text
    assert future == ""


def test_the_export_refuses_a_format_it_does_not_have(client) -> None:
    refused = client.get("/api/orgs/local/activity/export?format=pdf")
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "invalid"


def test_the_export_refuses_a_date_it_cannot_read(client) -> None:
    refused = client.get("/api/orgs/local/activity/export?format=csv&start=last%20tuesday")
    assert refused.status_code == 400
    assert "start" in refused.json()["error"]["message"]


def as_grant(client, node) -> None:
    """Call with an agent's grant, which is not the owner: the owner's token and session are not used."""
    _, token = node.get("policy").issue(subject={"id": "claude-code"}, workspaces=["biz-a"], ceiling="read")
    client.cookies.clear()
    client.headers["authorization"] = f"Bearer {token}"


def test_push_says_this_computer_cannot_send_it_so_the_app_stops_asking(client) -> None:
    answer = client.get("/api/orgs/local/push/config")
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"available": False, "reason": "not_configured", "public_key": None}


def test_no_device_can_be_added_or_tested_here(client) -> None:
    assert client.get("/api/orgs/local/push/devices").json() == []
    added = client.post("/api/orgs/local/push/devices",
                        json={"endpoint": "https://push.example/x", "keys": {"p256dh": "a", "auth": "b"}})
    assert added.status_code == 503
    assert added.json()["error"]["code"] == "unavailable"
    assert client.post("/api/orgs/local/push/test").status_code == 503
    assert client.delete("/api/orgs/local/push/devices/none").status_code == 503  # no device can exist here


def test_push_config_is_for_any_caller(client, node) -> None:
    as_grant(client, node)
    assert client.get("/api/orgs/local/push/config").status_code == 200


def test_billing_has_no_portal_to_open(client) -> None:
    refused = client.post("/api/orgs/local/billing/portal")
    assert refused.status_code == 404
    assert refused.json()["error"]["code"] == "not_found"
    assert "no billing" in refused.json()["error"]["message"]


def test_a_grant_cannot_open_billing(client, node) -> None:
    as_grant(client, node)
    refused = client.post("/api/orgs/local/billing/portal")
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "denied"


WEBHOOK_URL = "https://hooks.example.com/openajar/receive"


@pytest.fixture
def hooks(node):
    """The node's webhook service with its names and sends replaced: a route test never reaches the network."""
    svc = node.get("webhooks")

    async def public(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    async def never_sent(url: str, body: bytes, headers: dict) -> None:
        raise AssertionError("a route test must not send anything")

    svc.resolver = public
    svc.sender = never_sent
    return svc


def make_hook(client, url: str = WEBHOOK_URL, events: list[str] | None = None) -> dict:
    body = {"url": url} if events is None else {"url": url, "events": events}
    created = client.post("/api/orgs/local/webhooks", json=body)
    assert created.status_code == 200, created.text
    return created.json()


def test_the_events_the_app_offers_are_the_ones_the_service_sends(hooks) -> None:
    from typing import get_args

    from openajar.api.web.webhooks import Event
    from openajar.webhooks.service import EVENTS

    assert set(get_args(Event)) == set(EVENTS) == {"approval.requested", "approval.decided", "approval.expired",
                                                    "run.failed"}


def test_no_webhook_at_first(client, hooks) -> None:
    assert client.get("/api/orgs/local/webhooks").json() == []


def test_a_webhook_is_made_and_its_secret_is_shown_once(client, hooks) -> None:
    made = make_hook(client)
    assert made["secret"].startswith("whsec_")
    assert made["origin"] == "https://hooks.example.com"
    assert made["events"] == ["approval.requested", "approval.decided", "approval.expired", "run.failed"]
    [listed] = client.get("/api/orgs/local/webhooks").json()
    assert "secret" not in listed and listed["origin"] == "https://hooks.example.com"
    assert "/openajar/receive" not in json.dumps(listed)  # the address never comes back
    assert listed["last_delivery"] is None


@pytest.mark.parametrize("url", [
    "http://hooks.example.com/x",
    "https://127.0.0.1/x",
    "https://10.0.0.5/x",
    "https://localhost/x",
    "https://169.254.169.254/x",
])
def test_an_address_inside_this_computer_or_its_network_is_refused(client, hooks, url: str) -> None:
    refused = client.post("/api/orgs/local/webhooks", json={"url": url})
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["code"] == "invalid"
    assert client.get("/api/orgs/local/webhooks").json() == []


def test_a_name_that_answers_with_a_private_address_is_refused(client, hooks) -> None:
    async def private(host: str, port: int) -> list[str]:
        return ["192.168.1.20"]

    hooks.resolver = private
    refused = client.post("/api/orgs/local/webhooks", json={"url": WEBHOOK_URL})
    assert refused.status_code == 400
    assert "private network" in refused.json()["error"]["message"]


def test_an_event_the_service_does_not_know_is_refused(client, hooks) -> None:
    refused = client.post("/api/orgs/local/webhooks", json={"url": WEBHOOK_URL, "events": ["payment.made"]})
    assert refused.status_code == 400


def test_an_owner_can_switch_a_webhook_off_and_change_its_events(client, hooks) -> None:
    made = make_hook(client, events=["run.failed"])
    changed = client.patch(f"/api/orgs/local/webhooks/{made['id']}",
                           json={"enabled": False, "events": ["approval.requested"]})
    assert changed.status_code == 200, changed.text
    assert changed.json()["enabled"] is False and changed.json()["events"] == ["approval.requested"]
    assert client.patch(f"/api/orgs/local/webhooks/{made['id']}", json={"enabled": "yes"}).status_code == 400


def test_a_test_is_a_ping_that_shows_in_the_log(client, hooks) -> None:
    made = make_hook(client)
    sent = client.post(f"/api/orgs/local/webhooks/{made['id']}/test")
    assert sent.status_code == 200, sent.text
    [entry] = client.get(f"/api/orgs/local/webhooks/{made['id']}/deliveries").json()
    assert entry["id"] == sent.json()["id"] and entry["kind"] == "ping" and entry["state"] == "pending"
    [listed] = client.get("/api/orgs/local/webhooks").json()
    assert listed["last_delivery"]["kind"] == "ping"


def test_a_turned_off_webhook_is_not_tested(client, hooks) -> None:
    made = make_hook(client)
    client.patch(f"/api/orgs/local/webhooks/{made['id']}", json={"enabled": False})
    assert client.post(f"/api/orgs/local/webhooks/{made['id']}/test").status_code == 400


def test_rotating_gives_a_new_secret_once(client, hooks) -> None:
    made = make_hook(client)
    rotated = client.post(f"/api/orgs/local/webhooks/{made['id']}/rotate")
    assert rotated.status_code == 200
    assert rotated.json()["secret"].startswith("whsec_") and rotated.json()["secret"] != made["secret"]
    assert "secret" not in client.get("/api/orgs/local/webhooks").json()[0]


def test_deleting_a_webhook_removes_it_and_its_log(client, hooks) -> None:
    made = make_hook(client)
    assert client.delete(f"/api/orgs/local/webhooks/{made['id']}").status_code == 204
    assert client.get("/api/orgs/local/webhooks").json() == []
    assert client.delete(f"/api/orgs/local/webhooks/{made['id']}").status_code == 404


def test_an_unknown_webhook_is_not_found(client, hooks) -> None:
    assert client.patch("/api/orgs/local/webhooks/wh_none", json={"enabled": False}).status_code == 404
    assert client.get("/api/orgs/local/webhooks/wh_none/deliveries").status_code == 404


def test_the_log_takes_a_whole_number_limit(client, hooks) -> None:
    made = make_hook(client)
    assert client.get(f"/api/orgs/local/webhooks/{made['id']}/deliveries?limit=abc").status_code == 400
    assert client.get(f"/api/orgs/local/webhooks/{made['id']}/deliveries?limit=5").status_code == 200


def test_the_owner_token_is_not_the_owner_in_person_for_webhooks(client, hooks) -> None:
    """A token acts for the owner, but a webhook sends this computer's approvals out of it: only the person does that."""
    made = make_hook(client)
    as_token(client)
    assert client.get("/api/orgs/local/webhooks").status_code == 403
    assert client.post("/api/orgs/local/webhooks", json={"url": WEBHOOK_URL}).status_code == 403
    assert client.patch(f"/api/orgs/local/webhooks/{made['id']}", json={"enabled": False}).status_code == 403
    assert client.post(f"/api/orgs/local/webhooks/{made['id']}/test").status_code == 403
    assert client.delete(f"/api/orgs/local/webhooks/{made['id']}").status_code == 403


def test_a_grant_cannot_read_or_change_a_webhook(client, node, hooks) -> None:
    made = make_hook(client)
    as_grant(client, node)
    assert client.get("/api/orgs/local/webhooks").status_code == 403
    assert client.post("/api/orgs/local/webhooks", json={"url": WEBHOOK_URL}).status_code == 403
    assert client.patch(f"/api/orgs/local/webhooks/{made['id']}", json={"enabled": False}).status_code == 403
    assert client.post(f"/api/orgs/local/webhooks/{made['id']}/rotate").status_code == 403
    assert client.post(f"/api/orgs/local/webhooks/{made['id']}/test").status_code == 403
    assert client.get(f"/api/orgs/local/webhooks/{made['id']}/deliveries").status_code == 403
    refused = client.delete(f"/api/orgs/local/webhooks/{made['id']}")
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "denied"


def test_the_window_is_the_one_the_daily_sweep_applies(tmp_path) -> None:
    """The window the screen shows is `retention_days`, and the sweep (`purge`) removes what is older than
    it: the same number, read in the same place. The log is a prefix, so the old row comes first."""
    import time

    from openajar.policy.traces import TraceStore

    store = TraceStore(tmp_path / "traces.db", retention_days=30)
    store.write(TraceEvent(kind="tool_end", tool="x", status="ok", summary="long ago", at=time.time() - 40 * 86400))
    store.write(TraceEvent(kind="tool_end", tool="x", status="ok", summary="this week", at=time.time() - 3 * 86400))
    assert store.retention_days == 30
    assert store.purge() == 1
    assert [event.summary for _, event in store.export_page()] == ["this week"]
    assert store.oldest_at() is not None and store.oldest_at() > time.time() - 10 * 86400
    store.close()


def test_the_export_pages_through_a_long_log_without_gaps_or_repeats(client, node) -> None:
    store = node.get("traces")
    before = store.db.one("SELECT COUNT(*) AS n FROM traces")["n"]
    for number in range(1100):
        store.write(TraceEvent(kind="tool_end", tool="x", status="ok", summary=f"row {number}", workspace="biz-a"))
    store.flush()
    answer = client.get("/api/orgs/local/activity/export?format=ndjson")
    rows = [json.loads(line) for line in answer.text.splitlines()]
    assert len(rows) == before + 1100 + 1  # the export is itself the last row of its own log
    assert len({row["id"] for row in rows}) == len(rows)
    numbered = [row["summary"] for row in rows if row["summary"].startswith("row ")]
    assert numbered == [f"row {number}" for number in range(1100)]
