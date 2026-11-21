"""An agent's name as a person reads it (openajar.core.agent_names): the same cases the web app passes."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from openajar.api.web import telling
from openajar.core import agent_names
from openajar.node.viewer_state import agent_name, env_agent_name

CASES = json.loads((Path(__file__).parents[2] / "web/src/lib/agent-names.cases.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c['subject']!r}+{c['client']!r}")
def test_the_web_apps_cases(case: dict) -> None:
    subject, client = case["subject"], case["client"]
    assert agent_names.agent_view(subject, client) == {
        "name": subject, "client": client, "said": case["said"], "via": case["via"], "where": case["where"]}
    assert telling.agent_name(subject) == case["principal"]


def test_the_live_view_and_the_desktop_say_what_the_web_app_says() -> None:
    env = SimpleNamespace(agent="claude-code@studio-mac-mini", client="claude-code 2.1.328", calls=[])
    assert env_agent_name(env) == "Claude Code"
    unnamed = SimpleNamespace(agent="an agent", client="claude-code 2.1.0",
                              calls=[SimpleNamespace(client="codex-mcp-client 0.40.0")])
    assert env_agent_name(unnamed) == "Codex"  # its newest call's client
    view = agent_names.agent_view(unnamed.agent, unnamed.client, named=env_agent_name(unnamed))
    assert view["said"] == "Codex" and view["via"] is None
    assert env_agent_name(None) is None
    assert agent_name("claude-code 2.1.328") == "Claude Code"


def test_no_version_no_raw_host_no_long_name() -> None:
    assert agent_names.product("t3-code 0.9.2") == "T3 Code"
    assert agent_names.product("claude code") == "Claude Code"
    assert agent_names.product(None) is None and agent_names.product("  ") is None
    assert len(agent_names.product("x" * 200) or "") <= agent_names.MAX_NAME
    assert agent_names.where("claude-code@Mac-mini_2") == "Mac mini 2"
    assert telling.agent_name(None) == "An agent"
