"""`openajar memory`: the owner in person on the node."""

from __future__ import annotations

import json

import pytest
from testkit.memory import fake_memory
from typer.testing import CliRunner

from openajar.cli import app

runner = CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path))
    return tmp_path


def run(*args, input=None):
    result = runner.invoke(app, ["memory", *args], input=input)
    return result


def test_write_list_show_search_as_owner(home):
    out = run("write", "biz-a", "--type", "decision", "--title", "No posts on Sundays", "--body-from", "-",
              "--json", input="biz-a does not post on Sundays.\n")
    assert out.exit_code == 0, out.output
    entry = json.loads(out.output)
    assert entry["trust"] == "owner" and entry["type"] == "decision"
    listed = run("list", "biz-a")
    assert listed.exit_code == 0 and "No posts on Sundays" in listed.output and "owner" in listed.output
    shown = run("show", entry["id"])
    assert shown.exit_code == 0 and "does not post on Sundays" in shown.output
    found = run("search", "biz-a", "sundays", "--json")
    assert [h["id"] for h in json.loads(found.output)["hits"]] == [entry["id"]]
    assert "\u2014" not in listed.output + shown.output + found.output


def test_confirm_promotes_an_agent_note_and_review_dismisses(home):
    m = fake_memory(home)
    note = m.service.write(m.agent(), "biz-a", {"type": "fact", "title": "r/x removes link titles"})
    other = m.service.write(m.agent(), "biz-a", {"type": "fact", "title": "another note"})
    m.service.close()
    unreviewed = json.loads(run("list", "biz-a", "--unreviewed", "--json").output)["entries"]
    assert {e["id"] for e in unreviewed} == {note["id"], other["id"]}
    out = run("confirm", note["id"], "1", "--json")
    assert out.exit_code == 0, out.output
    assert json.loads(out.output)["trust"] == "owner"
    assert run("review", other["id"]).exit_code == 0
    assert json.loads(run("list", "biz-a", "--unreviewed", "--json").output)["entries"] == []
    stale = run("confirm", note["id"], "1")
    assert stale.exit_code == 1 and "no longer current" in stale.output


def test_forget_then_verify(home):
    entry = json.loads(run("write", "biz-a", "--type", "contact", "--title", "Jane", "--json").output)
    assert run("forget", entry["id"], "--reason", "asked to be forgotten").exit_code == 1   # asks first: no input
    out = run("forget", entry["id"], "--reason", "asked to be forgotten", "--yes")
    assert out.exit_code == 0 and "forgot" in out.output
    shown = json.loads(run("show", entry["id"], "--json").output)
    assert shown["title"] is None and shown["tombstone"]["reason"] == "asked to be forgotten"
    ok = run("verify")
    assert ok.exit_code == 0 and "biz-a: ok" in ok.output


def test_an_owners_forget_is_on_the_action_log_and_show_keeps_the_write_time(home):
    """QA 2026-11-15 P2-12 and P2-14."""
    import sqlite3

    from openajar.policy.service import PolicyService

    PolicyService(home).close()  # the node's action log, as agentosd leaves it
    entry = json.loads(run("write", "biz-a", "--type", "fact", "--title", "Marker", "--json").output)
    written = json.loads(run("show", entry["id"], "--json").output)["written_at"]
    out = run("forget", entry["id"], "--reason", "a test note", "--yes")
    assert out.exit_code == 0 and "no action log" not in out.output
    with sqlite3.connect(home / "traces.db") as db:
        rows = db.execute("SELECT principal, summary FROM traces WHERE kind = 'memory_forget'").fetchall()
    assert rows == [("owner:cli", f"forgot {entry['id']}")]
    shown = json.loads(run("show", entry["id"], "--json").output)
    assert shown["written_at"] == written and shown["state"] == "forgotten"
    assert f"at {written}" in run("show", entry["id"]).output  # the write time, not the forget time


def test_verify_fails_on_a_rewritten_file(home):
    import sqlite3

    run("write", "biz-a", "--type", "fact", "--title", "a fact")
    db = sqlite3.connect(home / "memory" / "biz-a.db")
    db.execute("UPDATE blobs SET data = 'changed'")
    db.commit()
    db.close()
    out = run("verify", "biz-a")
    assert out.exit_code == 1 and "FAILED" in out.output and "altered" in out.output


def test_export_md_and_jsonl_are_private(home, tmp_path):
    run("write", "biz-a", "--type", "decision", "--title", "No discounts", "--body-from", "-", input="never\n")
    run("write", "biz-a", "--type", "fact", "--title", "4,210 followers")
    out = run("export", "biz-a", "--md", str(tmp_path / "md"))
    assert out.exit_code == 0, out.output
    assert "plaintext" in out.output
    files = sorted(p.name for p in (tmp_path / "md").iterdir())
    assert files == ["biz-a-decision.md", "biz-a-fact.md"]
    text = (tmp_path / "md" / "biz-a-decision.md").read_text()
    assert "## No discounts" in text and "owner trust" in text and "sha256:" in text
    assert ((tmp_path / "md" / "biz-a-fact.md").stat().st_mode & 0o777) == 0o600
    assert run("export", "biz-a", "--jsonl", str(tmp_path / "m.jsonl")).exit_code == 0
    lines = (tmp_path / "m.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["record"] == "memory" and len(lines) > 3


def test_import_markdown_and_jsonl_as_owner_documents(home, tmp_path):
    md = tmp_path / "voice.md"
    md.write_text("# Brand voice\n\nWarm, short, no slang.\n")
    out = run("import", "biz-a", str(md), "--json")
    assert out.exit_code == 0, out.output
    [doc] = json.loads(out.output)
    assert doc["type"] == "document" and doc["trust"] == "owner"
    lines = tmp_path / "facts.jsonl"
    lines.write_text(json.dumps({"type": "fact", "title": "a"}) + "\n" + json.dumps({"type": "nope", "title": "b"}))
    bad = run("import", "biz-a", str(lines))
    assert bad.exit_code == 1
    assert "a" not in [e["title"] for e in json.loads(run("list", "biz-a", "--json").output)["entries"]]


def test_a_secret_is_refused_on_the_cli_too(home):
    out = run("write", "biz-a", "--type", "fact", "--title", "key", "--body-from", "-",
              input="sk-ant-api03-" + "a" * 40)
    assert out.exit_code == 1 and "secret_in_memory" in out.output
