"""Workspace memory on a node, as the app reads it (web/src/api/types.ts, `MemoryEntry`).

The owner in person reads, confirms, reviews and forgets. A bearer token is refused, the owner token included,
because agents hold it. Memory text is written by agents after reading pages, so it comes back as text.
"""

from __future__ import annotations

from agentos.memory.models import Caller, MemoryScope
from agentos.memory.service import WARN_AGENT

TOKEN = "t" * 43
BASE = "/api/orgs/local/memory"
BIZ = "biz-a"


def _agent_note(node, title: str = "r/x removes link titles", body: str = "Reddit strips link titles on Tuesdays."):
    agent = Caller(kind="agent", principal="worker", scope=MemoryScope.everything())
    return node.get("memory").write(agent, BIZ, {"type": "fact", "title": title, "body": body})


def _owner_note(node, title: str = "No posts on Sundays"):
    return node.get("memory").write(Caller.owner(), BIZ, {"type": "decision", "title": title,
                                                          "body": "biz-a does not post on Sundays."})


def test_the_workspaces_say_what_waits_and_whether_the_record_checks_out(client, node) -> None:
    _owner_note(node)
    _agent_note(node)
    payload = client.get(f"{BASE}/workspaces").json()
    assert payload["available"] is True
    [row] = [item for item in payload["workspaces"] if item["workspace"] == BIZ]
    assert row["live_entries"] == 2 and row["live_entries_cap"] > row["live_entries"]
    assert row["content_bytes"] > 0 and row["content_bytes_cap"] > row["content_bytes"]
    assert row["waiting"] == 1, "only the agent's note waits for a look"
    assert row["chain_ok"] is True and row["forgotten"] == 0


def test_every_workspace_listed_names_each_entrys_own_workspace(client, node) -> None:
    """Listing every workspace at once must not give each entry the workspace of the query (none)."""
    _owner_note(node)
    node.get("memory").write(Caller.owner(), "biz-b", {"type": "decision", "title": "Biz B posts on Fridays",
                                                       "body": "Once a week, on Friday afternoon."})
    rows = client.get(BASE).json()["entries"]
    by_title = {row["title"]: row["workspace"] for row in rows}
    assert by_title["No posts on Sundays"] == BIZ
    assert by_title["Biz B posts on Fridays"] == "biz-b"


def test_entries_list_unreviewed_and_search_with_their_text_left_out(client, node) -> None:
    owner = _owner_note(node)
    agent = _agent_note(node)
    listed = client.get(BASE, params={"workspace": BIZ}).json()
    assert listed["available"] is True and listed["workspace"] == BIZ
    ids = {row["id"] for row in listed["entries"]}
    assert {owner["id"], agent["id"]} <= ids
    waiting = client.get(BASE, params={"workspace": BIZ, "unreviewed": "1"}).json()["entries"]
    assert [row["id"] for row in waiting] == [agent["id"]]
    assert waiting[0]["trust"] == "agent" and waiting[0]["reviewed"] is False
    hits = client.get(BASE, params={"workspace": BIZ, "q": "sundays"}).json()["entries"]
    assert [row["id"] for row in hits] == [owner["id"]]
    assert "body" not in listed["entries"][0], "the list carries titles, never the text"


def test_one_entry_is_its_text_as_text_with_its_warning_and_history(client, node) -> None:
    note = _agent_note(node, title="Cheap flights", body="<img src=x onerror=alert(1)> Book on Mondays.")
    entry = client.get(f"{BASE}/{note['ref'].split('@')[0]}", params={"workspace": BIZ}).json()
    assert entry["body"] == "<img src=x onerror=alert(1)> Book on Mondays."  # plain text: the app renders it as text
    assert entry["warning"] == WARN_AGENT
    assert entry["history"] and entry["history"][0]["version"] == 1
    assert entry["forgotten"] is None and entry["writer"] == "worker"


def test_the_owner_confirms_an_agent_note_and_it_leaves_the_waiting_list(client, node) -> None:
    note = _agent_note(node)
    entry_id = note["ref"].split("@")[0]
    out = client.post(f"{BASE}/{entry_id}/confirm", json={"workspace": BIZ, "version": 1})
    assert out.status_code == 200, out.text
    assert out.json()["trust"] == "owner" and out.json()["version"] == 2
    waiting = client.get(BASE, params={"workspace": BIZ, "unreviewed": "1"}).json()["entries"]
    assert waiting == []


def test_a_stale_version_is_refused_and_nothing_changes(client, node) -> None:
    note = _agent_note(node)
    entry_id = note["ref"].split("@")[0]
    out = client.post(f"{BASE}/{entry_id}/confirm", json={"workspace": BIZ, "version": 7})
    assert out.status_code == 409 and out.json()["error"]["code"] == "conflict"
    assert client.get(f"{BASE}/{entry_id}", params={"workspace": BIZ}).json()["trust"] == "agent"


def test_review_dismisses_a_note_without_promoting_it(client, node) -> None:
    note = _agent_note(node)
    entry_id = note["ref"].split("@")[0]
    out = client.post(f"{BASE}/{entry_id}/review", json={"workspace": BIZ})
    assert out.status_code == 200 and out.json()["reviewed_at"].endswith("Z")
    assert client.get(f"{BASE}/{entry_id}", params={"workspace": BIZ}).json()["trust"] == "agent"
    assert client.get(BASE, params={"workspace": BIZ, "unreviewed": "1"}).json()["entries"] == []


def test_forget_needs_a_reason_then_erases_the_text_in_every_version(client, node) -> None:
    note = _owner_note(node, title="Jane Doe, the accountant")
    entry_id = note["ref"].split("@")[0]
    refused = client.post(f"{BASE}/{entry_id}/forget", json={"workspace": BIZ, "reason": "  "})
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "invalid"
    assert client.get(f"{BASE}/{entry_id}", params={"workspace": BIZ}).json()["title"] == "Jane Doe, the accountant"
    out = client.post(f"{BASE}/{entry_id}/forget", json={"workspace": BIZ, "reason": "asked to be forgotten"})
    assert out.status_code == 200, out.text
    assert out.json()["state"] == "forgotten" and out.json()["already"] is False
    after = client.get(f"{BASE}/{entry_id}", params={"workspace": BIZ}).json()
    assert after["title"] is None and after["body"] is None
    assert after["forgotten"]["reason"] == "asked to be forgotten"


def test_export_is_one_attached_file_that_is_never_rendered(client, node) -> None:
    _owner_note(node, title="Sunday rule")
    jsonl = client.get(f"{BASE}/export", params={"workspace": BIZ, "format": "jsonl"})
    assert jsonl.status_code == 200
    assert jsonl.headers["content-disposition"] == f'attachment; filename="{BIZ}-memory.jsonl"'
    assert jsonl.headers["x-content-type-options"] == "nosniff"
    assert "Sunday rule" in jsonl.text and jsonl.text.splitlines()[0].startswith("{")
    md = client.get(f"{BASE}/export", params={"workspace": BIZ, "format": "md"})
    assert md.status_code == 200 and md.headers["content-disposition"].endswith('-memory.md"')
    assert "Sunday rule" in md.text
    bad = client.get(f"{BASE}/export", params={"workspace": BIZ, "format": "pdf"})
    assert bad.status_code == 400


def test_a_token_cannot_read_confirm_or_forget_memory(client, node) -> None:
    note = _agent_note(node)
    entry_id = note["ref"].split("@")[0]
    bearer = {"Authorization": f"Bearer {TOKEN}"}
    for method, url, body in (
        ("GET", f"{BASE}/workspaces", None),
        ("GET", BASE, None),
        ("GET", f"{BASE}/{entry_id}", None),
        ("GET", f"{BASE}/export", None),
        ("POST", f"{BASE}/{entry_id}/confirm", {"workspace": BIZ, "version": 1}),
        ("POST", f"{BASE}/{entry_id}/review", {"workspace": BIZ}),
        ("POST", f"{BASE}/{entry_id}/forget", {"workspace": BIZ, "reason": "by a token"}),
    ):
        response = client.request(method, url, json=body, headers=bearer)
        assert response.status_code == 403, (method, url, response.text)
        assert response.json()["error"]["code"] == "denied"
    still = client.get(f"{BASE}/{entry_id}", params={"workspace": BIZ}).json()
    assert still["trust"] == "agent" and still["title"] == "r/x removes link titles"
    assert still["forgotten"] is None
