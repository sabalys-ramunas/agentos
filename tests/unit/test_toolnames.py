"""The 2026-11-19 names: no tool says lane, and a call by a name removed in 0.5.0 fails and names the new one."""

from __future__ import annotations

from test_toolsets import every_spec, node_server

from openajar.core import toolnames
from openajar.core.models import Effect
from openajar.core.runtime import Runtime
from openajar.core.tools import ToolSpec
from openajar.core.toolsets import TOOLSETS
from openajar.mcp.registry import build_mcp


def test_no_tool_says_lane_and_every_new_name_is_served():
    _, specs = every_spec()
    names = {s.name for s in specs}
    assert not [n for n in names if "lane" in n]
    assert toolnames.NEW_NAMES <= names
    assert not set(toolnames.RENAMED) & names
    assert not {t for ts in TOOLSETS for t in ts.tools} & set(toolnames.RENAMED)
    assert len(toolnames.NEW_NAMES) == len(toolnames.RENAMED)  # one to one
    assert toolnames.current("lane_exec") == "terminal_exec" and toolnames.current("browser_act") == "browser_act"
    merged = set(toolnames.MERGED)
    assert not merged & names and not {t for ts in TOOLSETS for t in ts.tools} & merged
    assert {merged[0] for merged in toolnames.MERGED.values()} <= names
    assert toolnames.current("approval_status") == "approval_wait" and toolnames.current("browser_tab") == "browser_tabs"


def test_a_removed_name_says_what_to_call_instead():
    assert toolnames.use_instead("lane_exec") == "lane_exec is terminal_exec"
    assert toolnames.use_instead("browser_tab") == "browser_tab is browser_tabs(select=...)"
    assert toolnames.use_instead("approval_status") == "approval_status is approval_wait(wait_s=0)"
    assert toolnames.use_instead("browser_dialog_answer") == "browser_dialog_answer is browser_dialog(accept=...)"
    assert toolnames.use_instead("terminal_exec") is None and toolnames.use_instead("nope") is None
    assert toolnames.call_instead("lane_job_read") == \
        "terminal_job_read with the same arguments (wait_ms is wait_s, in seconds)"
    assert toolnames.call_instead("lane_exec") == "terminal_exec with the same arguments"
    assert toolnames.old_argument("terminal_job_read", "wait_ms") == "wait_ms is wait_s, in seconds"
    assert toolnames.old_argument("browser_wait", "timeout_s") == "timeout_s is wait_s"
    assert toolnames.old_argument("terminal_exec", "timeout_s") is None  # a command's own limit


def test_the_cli_calls_a_node_from_before_the_rename_by_its_old_names():
    assert toolnames.for_older_node("approval_wait", {"approval_id": "a", "wait_s": 5}) == \
        ("approval_wait", {"approval_id": "a", "timeout_s": 5})
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


async def test_an_old_name_or_argument_fails_and_names_the_new_one():
    server, seen = _server()
    for result in (await server.call_tool("lane_job_read", {"job": "j1", "wait_ms": 2000}),
                   await server.call_tool("tools_call", {"name": "lane_job_read",
                                                         "arguments": {"job": "j1", "wait_ms": 2000}})):
        assert result.is_error
        text = result.content[0].text
        assert text.startswith("not_found: no tool named 'lane_job_read': lane_job_read is terminal_job_read")
        assert "fix: Call terminal_job_read with the same arguments (wait_ms is wait_s, in seconds)." in text
    old_arg = await server.call_tool("terminal_job_read", {"job": "j1", "wait_ms": 2000})
    assert old_arg.is_error
    assert "'wait_ms' (wait_ms is wait_s, in seconds; the old name was removed in 0.5.0)" in old_arg.content[0].text
    merged = (await server.call_tool("browser_tab", {"index": 1})).content[0].text
    assert "browser_tab is browser_tabs(select=...)" in merged and "fix: Call browser_tabs(select=...)." in merged
    assert seen == []  # nothing ran under an old name
    plain = await server.call_tool("terminal_job_read", {"job": "j1"})
    assert not plain.is_error and "renamed" not in plain.structured_content and len(plain.content) == 1


async def test_keep_on_the_close_names_environment_open():
    server, _ = _server()
    refused = await server.call_tool("environment_close", {"keep": "save"})
    assert refused.is_error
    assert "keep is chosen at environment_open" in refused.content[0].text


async def test_tools_more_with_an_old_name_says_the_new_one_and_the_old_files_toolset_still_loads():
    server = node_server(["core"])
    by_old = await server.tools_more({"toolset": "lane_claim"})
    assert by_old.is_error
    assert "lane_claim is browser_claim, removed in 0.5.0" in by_old.content[0].text
    files = (await server.tools_more({"toolset": "files"})).structured_content  # a setting, not a tool name
    assert files["toolset"] == "browser" and "browser_file_get" in [t["name"] for t in files["tools"]]
