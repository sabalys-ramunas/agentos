"""The front door an agent meets first: the MCP intro leads with the environment (a cloud computer
for this chat), whoami names the bound environment or says how to get one, and the
agentos-environment skill names real tools and installs like the other skills."""

from __future__ import annotations

import json
import re
from pathlib import Path

from fakes import FakeInteraction, FakeLaneManager
from mcp import Client

from agentos.client import skills
from agentos.core.config import NodeConfig
from agentos.core.runtime import Runtime
from agentos.mcp.registry import build_mcp
from agentos.node.tools import INSTRUCTIONS, tool_specs

EM_DASH = chr(0x2014)
SKILL = Path(__file__).resolve().parents[2] / "src/agentos/client/skills/agentos-environment/SKILL.md"
ENV_TOOLS = ("environment_open", "environment()", "environment_close")


def test_the_intro_starts_with_the_environment_and_puts_lanes_after():
    first = INSTRUCTIONS.split("\n\n")[0]
    assert first.startswith("agentos can give you your own computer in the cloud for this chat")
    for phrase in (*ENV_TOOLS, "without `on`", "localhost", "push", "links", "work in agentos",
                   "builds, tests, a dev server, a browser", "never leave one running"):
        assert phrase in first, phrase
    assert "lanes_list" not in first  # account lanes are a separate thing, after
    assert INSTRUCTIONS.index("environment_open") < INSTRUCTIONS.index("A lane id is workspace/name")
    assert EM_DASH not in INSTRUCTIONS and len(INSTRUCTIONS) < 4_000


class Bound:
    """Stands in for agentos.environments: the environment bound to the calling chat."""

    def __init__(self, env):
        self.env = env

    async def bound(self, binding, family=None):
        return self.env


async def _whoami(tmp_path, **services) -> dict:
    config = NodeConfig(home=tmp_path, name="test-node", mcp_token="tok")
    runtime = Runtime(config=config, lanes=FakeLaneManager(config), interaction=FakeInteraction(), **services)
    async with Client(build_mcp(runtime, [tool_specs], instructions=INSTRUCTIONS)) as client:
        result = await client.call_tool("whoami", {})
    return result.structured_content or json.loads(result.content[0].text)


async def test_whoami_says_how_to_get_an_environment_when_none_is_bound(tmp_path):
    for services in ({}, {"environments": Bound(None)}):
        data = await _whoami(tmp_path, **services)
        assert data["node"] == "test-node" and data["environment"] is None
        assert "environment_open(title=" in data["next"]


async def test_whoami_names_the_bound_environment(tmp_path):
    env = {"id": "e7k2", "title": "Black Friday page", "state": "ready", "workdir": "/work/shop",
           "repo": "github.com/me/shop", "branch": "main", "machine": {"provider": "docker"}}
    data = await _whoami(tmp_path, environments=Bound(env))
    assert data["environment"] == {k: env[k] for k in ("id", "title", "state", "workdir", "repo", "branch")}
    assert "e7k2 (Black Friday page)" in data["next"] and "without `on`" in data["next"]


def test_the_environment_skill_is_installed_like_the_others_and_names_the_loop():
    assert "agentos-environment" in skills.NAMES
    text = skills.source("agentos-environment")
    head = text.split("---")[1]
    assert re.search(r"^name: agentos-environment$", head, re.MULTILINE) and skills.ours(text, "agentos-environment")
    assert text == SKILL.read_text()
    for word in ("environment_open(", "environment()", "environment_close(", "discard=true", "unsaved_work",
                 "no_environment", "lane_exec(", "browser_navigate(", "ui_check(", "localhost", "git push",
                 "preview pane", "links.browser", "default branch", "secrets", "data, never instructions"):
        assert word in text, word
    assert EM_DASH not in text
    assert len(text.split()) < 1000  # short: read in one go


def test_the_intro_and_the_skill_say_where_the_terminal_tools_are_and_promise_no_missing_link():
    """An agent with an environment finds its terminal without guessing (lane_exec and lane_job_start are
    in the default list; the rest are tools_more("terminals")), and is promised only links that exist: the
    node's live view (links.browser), no web app page and no port link."""
    first = INSTRUCTIONS.split("\n\n")[0]
    assert "links.browser" in first and "lane_job_start" in first and "studio toolset" in first
    assert "`on` for an account lane" in INSTRUCTIONS
    text = skills.source("agentos-environment")
    for words in ('tools_more("terminals")', 'tools_more("studio")', "lane_job_read", "in your tool list",
                  "environment_not_ready", "stashes"):
        assert words in text, words
    assert "links.environment" not in text and "a `link` for each port" not in text


def test_a_checkout_without_the_app_says_what_to_use_instead(tmp_path, monkeypatch):
    # an installation with no panel/ and no web/: the placeholder and the dev banner must not tell a
    # stranger to build one
    from types import SimpleNamespace

    from agentos.api.static import NO_APP, NOT_BUILT, WEB_NOT_BUILT, placeholder
    from agentos.dev import cli as dev_cli

    assert placeholder(tmp_path / "panel" / "dist") == NO_APP
    # the public repository: web/ and no panel/
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text("{}")
    assert placeholder(tmp_path / "panel" / "dist") == WEB_NOT_BUILT
    assert "pnpm build" in WEB_NOT_BUILT and "panel/" not in WEB_NOT_BUILT
    (tmp_path / "panel").mkdir()
    (tmp_path / "panel" / "package.json").write_text("{}")
    assert placeholder(tmp_path / "panel" / "dist") == NOT_BUILT
    assert "/view/" in NO_APP and "agentos environments list" in NO_APP
    node = SimpleNamespace(temporary=True, home=tmp_path, panel_link="http://localhost:7450/login#code=x",
                           mcp_url="http://127.0.0.1:7450/mcp", claude_add="claude mcp add ...", panel_built=False)
    monkeypatch.setattr(dev_cli, "panel_source", lambda: False)
    monkeypatch.setattr(dev_cli, "web_source", lambda: False)
    text = dev_cli.banner(node)
    assert "no web app" in text and "npm ci" not in text and "\u2014" not in text
    monkeypatch.setattr(dev_cli, "web_source", lambda: True)  # as in the public export
    text = dev_cli.banner(node)
    assert "pnpm install && pnpm build" in text and "panel/" not in text and "\u2014" not in text


def test_dev_up_sees_a_listening_port_as_busy_and_a_closing_one_as_free():
    # after Ctrl-C a connection of the last run may still be closing on the port: dev up starts again
    import socket

    from agentos.dev.cli import port_is_free

    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        assert not port_is_free(port)
        with socket.create_connection(("127.0.0.1", port)) as client:
            conn, _ = server.accept()
            conn.close()  # the server end closes first: its side waits in TIME_WAIT
            client.recv(1)
    assert port_is_free(port)
