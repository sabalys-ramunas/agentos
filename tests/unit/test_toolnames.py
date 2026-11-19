"""The 2026-11-19 names: no tool says lane, and every old name still works for one release, with a note."""

from __future__ import annotations

import json

from test_toolsets import every_spec, node_server

from agentos.core import toolnames
from agentos.core.models import Effect
from agentos.core.runtime import Runtime
from agentos.core.tools import ToolSpec
from agentos.core.toolsets import TOOLSETS
from agentos.mcp.registry import build_mcp


def test_no_tool_says_lane_and_every_new_name_is_served():
    _, specs = every_spec()
    names = {s.name for s in specs}
    assert not [n for n in names if "lane" in n]
    assert toolnames.NEW_NAMES <= names
    assert not set(toolnames.RENAMED) & names
    assert not {t for ts in TOOLSETS for t in ts.tools} & set(toolnames.RENAMED)
    assert len(toolnames.NEW_NAMES) == len(toolnames.RENAMED)  # one to one
    assert toolnames.current("lane_exec") == "terminal_exec" and toolnames.current("browser_act") == "browser_act"


def test_old_arguments_become_new_ones_and_say_so():
    args, notes = toolnames.rename_arguments("terminal_job_read", {"job": "j1", "wait_ms": 2500})
    assert args == {"job": "j1", "wait_s": 2.5} and notes == ["wait_ms is now wait_s (in seconds)"]
    assert toolnames.rename_arguments("terminal_job_read", {"job": "j1", "wait_ms": 1, "wait_s": 3})[0]["wait_s"] == 3
    assert toolnames.rename_arguments("browser_wait_handback", {"timeout_s": 60})[0] == {"wait_s": 60}
    assert toolnames.rename_arguments("browser_wait", {"timeout_s": 5}) == ({"timeout_s": 5}, [])
    assert toolnames.for_older_node("terminal_job_read", {"job": "j", "wait_s": 2}) == \
        ("lane_job_read", {"job": "j", "wait_ms": 2000})
    assert toolnames.for_older_node("browser_act", {}) is None


def _server():
    seen: list[dict] = []

    async def job_read(ctx, *, job: str, wait_s: float = 1) -> dict:
        """Read a job. Effect: read."""
        seen.append({"job": job, "wait_s": wait_s})
        return {"job": job, "wait_s": wait_s}

    async def close(ctx, *, on: str | None = None, discard: bool = False) -> dict:
        """Close. Effect: read."""
        return {"closed": True}

    specs = [ToolSpec("terminal_job_read", job_read, Effect.READ, lane_arg=None),
             ToolSpec("environment_close", close, Effect.READ, lane_arg=None)]
    return build_mcp(Runtime(), [lambda _rt: specs], toolsets=["core"]), seen


async def test_an_old_name_calls_the_new_tool_and_its_result_says_the_new_name():
    server, seen = _server()
    for result in (await server.call_tool("lane_job_read", {"job": "j1", "wait_ms": 2000}),
                   await server.call_tool("tools_call", {"name": "lane_job_read",
                                                         "arguments": {"job": "j1", "wait_ms": 2000}})):
        assert not result.is_error
        note = result.structured_content["renamed"]
        assert note.startswith("lane_job_read is now terminal_job_read; wait_ms is now wait_s")
        assert result.content[-1].text == f"renamed: {note}"
        assert json.loads(result.content[0].text)["wait_s"] == 2.0
    assert seen == [{"job": "j1", "wait_s": 2.0}] * 2
    plain = await server.call_tool("terminal_job_read", {"job": "j1"})
    assert "renamed" not in plain.structured_content and len(plain.content) == 1


async def test_keep_on_the_close_names_environment_open():
    server, _ = _server()
    refused = await server.call_tool("environment_close", {"keep": "save"})
    assert refused.is_error
    assert "keep is chosen at environment_open" in refused.content[0].text


async def test_tools_more_finds_a_tool_by_its_old_name_and_the_old_files_toolset():
    server = node_server(["core"])
    by_old = await server.tools_more({"toolset": "lane_claim"})
    body = by_old.structured_content
    assert body["toolset"] == "accounts" and [t["name"] for t in body["tools"]] == ["browser_claim"]
    files = (await server.tools_more({"toolset": "files"})).structured_content
    assert files["toolset"] == "browser" and "browser_file_get" in [t["name"] for t in files["tools"]]
