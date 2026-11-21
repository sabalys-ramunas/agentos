"""QA 2026-11-07 #18: the action log says what happened. A panel action that fails after it was
allowed is traced `error`, not `ok`; a damaged traces.db makes `audit verify` exit 4 (cannot be
proven), as its help says; a fresh node is at the latest schema, with nothing pending."""

from __future__ import annotations

import pytest
from test_node_panel import TOKEN, Node, bearer
from typer.testing import CliRunner

from openajar.cli import app
from openajar.core.errors import EngineError

runner = CliRunner()


@pytest.fixture
def node(tmp_path):
    n = Node(tmp_path)
    yield n
    n.close()


def test_a_panel_action_that_fails_is_traced_error(node, monkeypatch):
    async def broken(lane_id):
        raise EngineError("the browser did not start")

    monkeypatch.setattr(node.lanes, "open", broken)
    with node.client() as client:
        r = client.post("/api/lanes/biz-a/instagram/open", headers=bearer(TOKEN))
        assert r.status_code >= 400
        ok = client.post("/api/lanes/biz-a/instagram/close", headers=bearer(TOKEN))
        assert ok.status_code == 200, ok.text
    rows = node.policy.traces.query(kind="panel", limit=50)[0]
    opened = [e for e in rows if e.tool == "panel.lane.open"]
    closed = [e for e in rows if e.tool == "panel.lane.close"]
    assert [e.status for e in opened] == ["error"], [(e.tool, e.status) for e in rows]
    assert "did not start" in (opened[0].summary or "")
    assert [e.status for e in closed] == ["ok"]


def test_audit_verify_on_a_damaged_log_exits_4(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path))
    (tmp_path / "traces.db").write_bytes(b"this is not sqlite at all" * 200)
    result = runner.invoke(app, ["audit", "verify"])
    assert result.exit_code == 4, result.output
    assert "nothing in it can be proven" in result.output


def test_a_fresh_node_has_no_pending_migrations(tmp_path, monkeypatch):
    from openajar.core import migrate

    home = tmp_path / "h"
    monkeypatch.setenv("OPENAJAR_HOME", str(home))
    assert runner.invoke(app, ["node", "init", "--mcp-port", "7690"]).exit_code == 0
    from openajar.node.store import LaneStore

    LaneStore(home / "openajar.db")  # what agentosd opens first
    node_set = next(s for s in migrate.status(home) if s.package == "node")
    assert node_set.current == node_set.latest and node_set.pending == []
