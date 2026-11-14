"""The owner can issue a memory grant: `agentos grants issue --special memory` and the panel's
POST /api/grants with specials ["memory"]."""

from __future__ import annotations

import pytest
from test_policy_support import OWNER, Clock, make_service
from typer.testing import CliRunner

from agentos.api.bridge import PolicyAdapter
from agentos.api.routes_settings import SPECIALS, GrantCreate
from agentos.core.config import NodeConfig, save_config
from agentos.core.models import Principal
from agentos.policy.cli import app
from agentos.policy.memory_rules import memory_scope
from agentos.policy.service import PolicyService

runner = CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path))
    save_config(NodeConfig(home=tmp_path, mcp_token=OWNER))
    return tmp_path


def _grant(home, token: str):
    svc = PolicyService(home, node_config=NodeConfig(home=home, mcp_token=OWNER))
    try:
        return svc.resolve(token)[1]
    finally:
        svc.close()


def test_cli_issues_a_memory_grant(home):
    res = runner.invoke(app, ["grants", "issue", "--subject", "va-1", "--workspace", "biz-b", "--ceiling", "draft",
                              "--special", "memory", "--memory-write", "--memory-type", "fact",
                              "--memory-type", "document"])
    assert res.exit_code == 0, res.output
    scope = memory_scope(_grant(home, res.stdout.strip().splitlines()[-1]), "biz-b")
    assert scope.write and scope.types == {"fact", "document"} and not scope.private
    assert "\u2014" not in res.output


def test_cli_memory_is_read_only_by_default_and_checks_its_fields(home):
    res = runner.invoke(app, ["grants", "issue", "--subject", "reader", "--workspace", "biz-a",
                              "--special", "memory"])
    assert res.exit_code == 0, res.output
    assert not memory_scope(_grant(home, res.stdout.strip().splitlines()[-1]), "biz-a").write
    bad = runner.invoke(app, ["grants", "issue", "--subject", "x", "--workspace", "biz-a", "--special", "memory",
                              "--memory-type", "secrets"])
    assert bad.exit_code != 0 and "memory types" in bad.output
    outside = runner.invoke(app, ["grants", "issue", "--subject", "x", "--workspace", "biz-a", "--special", "memory",
                                  "--memory-workspace", "biz-b"])
    assert outside.exit_code != 0


def test_the_panel_can_grant_memory(tmp_path):
    assert "memory" in SPECIALS
    assert GrantCreate(subject="va", workspaces=["biz-b"], specials=["memory"]).specials == ["memory"]
    svc, _ = make_service(tmp_path, clock=Clock())
    try:
        out = PolicyAdapter(svc).issue_grant(subject="va", workspaces=["biz-b"], lanes=None, ceiling="draft",
                                             specials=["memory"], budget=None, ttl="7d",
                                             principal=Principal(kind="human", id="owner", owner="owner"))
        assert "memory" in out["specials"]
        scope = memory_scope(svc.resolve(out["token"])[1], "biz-b")
        assert scope.write and not scope.private and not scope.audience_owner
    finally:
        svc.close()
