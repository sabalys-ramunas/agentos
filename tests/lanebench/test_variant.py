"""Variants (`--variant DIR`, `export-text DIR`): the format round-trips, names that match nothing
are refused, and a variant changes what the bench's MCP server says, in this process only, for the
run only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lanebench import variant
from lanebench.executors.mcp_agent import agent_specs


@pytest.fixture
def specs(tmp_path):
    from openajar.core.config import NodeConfig
    from openajar.core.runtime import Runtime

    return agent_specs(Runtime(config=NodeConfig(home=tmp_path / "home")))


def names(specs) -> list[str]:
    return [s.name for s in specs]


def test_export_writes_the_four_files_and_loads_back_unchanged(tmp_path, specs):
    from openajar.core import catalog
    from openajar.node.tools import INSTRUCTIONS, RECIPES

    out = tmp_path / "base"
    counts = variant.export(out, specs)
    assert sorted(p.name for p in out.iterdir()) == sorted(variant.FILES)
    assert counts["tools.json"] == len(specs) and counts["errors.json"] == len(catalog.entries())
    v = variant.load(out, tool_names=names(specs))
    assert v.instructions == INSTRUCTIONS and set(v.recipes) == set(RECIPES) and len(v.digest) == 12
    assert json.loads((out / "tools.json").read_text())["browser_upload"].startswith("Attach inbox files")


def test_a_name_that_matches_nothing_is_refused(tmp_path, specs):
    (tmp_path / "tools.json").write_text(json.dumps({"browser_clik": "typo"}))
    with pytest.raises(variant.VariantError, match="browser_clik"):
        variant.load(tmp_path, tool_names=names(specs))
    (tmp_path / "tools.json").unlink()
    (tmp_path / "recipes.json").write_text(json.dumps({"post_image": {"args": []}}))
    with pytest.raises(variant.VariantError, match="only text fields"):
        variant.load(tmp_path)
    (tmp_path / "recipes.json").write_text(json.dumps({"no_such_topic": {"title": "x"}}))
    with pytest.raises(variant.VariantError, match="no_such_topic"):
        variant.load(tmp_path)
    (tmp_path / "recipes.json").unlink()
    with pytest.raises(variant.VariantError, match="holds none"):
        variant.load(tmp_path)


def test_the_digest_follows_the_contents(tmp_path):
    (tmp_path / "instructions.md").write_text("one")
    a = variant.digest(tmp_path)
    (tmp_path / "instructions.md").write_text("two")
    b = variant.digest(tmp_path)
    assert a[0] == b[0] == ("instructions.md",) and a[1] != b[1]


def test_recipes_and_error_lines_change_for_the_run_and_come_back(tmp_path):
    from openajar.core import catalog
    from openajar.mcp.registry import with_catalog_lines
    from openajar.node import tools as node_tools

    before_steps = node_tools.RECIPES["post_image"]["steps"]
    before_line = with_catalog_lines("busy: someone holds the lane")
    (tmp_path / "recipes.json").write_text(json.dumps({"post_image": {"summary": "shorter", "steps": "1. go"}}))
    (tmp_path / "errors.json").write_text(json.dumps({"busy": "Wait 30 s, then call again once."}))
    v = variant.load(tmp_path)
    with variant.applied(v):
        assert node_tools.recipe_text("post_image") == "1. go"
        assert node_tools.RECIPES["post_image"]["args"]  # the prompt arguments stay
        assert with_catalog_lines("busy: someone holds the lane").splitlines()[1] == \
            "fix: Wait 30 s, then call again once."
    assert node_tools.RECIPES["post_image"]["steps"] == before_steps
    assert with_catalog_lines("busy: someone holds the lane") == before_line
    assert catalog.entries()["busy"].agent != "Wait 30 s, then call again once."


def test_tool_descriptions_change_on_copies_only(tmp_path, specs):
    from openajar.mcp.registry import tool_description

    (tmp_path / "tools.json").write_text(json.dumps({"browser_snapshot": "Read the page."}))
    v = variant.load(tmp_path, tool_names=names(specs))
    described = {s.name: s for s in v.describe(specs)}
    assert tool_description(described["browser_snapshot"]) == "Read the page."
    original = next(s for s in specs if s.name == "browser_snapshot")
    assert tool_description(original) != "Read the page."
    assert v.public() == {"dir": str(tmp_path), "files": ["tools.json"], "digest": v.digest}


async def test_the_bench_mcp_server_says_the_variant(tmp_path, specs):
    """The server the executor builds: the variant's instructions and descriptions, nothing else."""
    from mcp import Client

    from lanebench.executors.mcp_agent import agent_providers
    from openajar.core.config import NodeConfig
    from openajar.core.runtime import Runtime
    from openajar.mcp.registry import build_mcp

    (tmp_path / "v").mkdir()
    (tmp_path / "v" / "instructions.md").write_text("Only these words.")
    (tmp_path / "v" / "tools.json").write_text(json.dumps({"browser_files": "List the inbox."}))
    v = variant.load(tmp_path / "v", tool_names=names(specs))
    runtime = Runtime(config=NodeConfig(home=tmp_path / "home"))
    runtime.register("lanes", object())  # lane tools are listed only on a node with lanes
    server = build_mcp(runtime, [v.provider(p) for p in agent_providers()], instructions=v.instructions)
    async with Client(server) as client:
        listed = {t.name: t.description for t in (await client.list_tools()).tools}
        assert client.instructions == "Only these words."
    assert listed["browser_files"] == "List the inbox."
    assert listed["browser_file_put"] != "List the inbox."


def test_the_cli_refuses_a_variant_without_the_mcp_agent(tmp_path):
    from typer.testing import CliRunner

    from openajar.lanebench_cli import app

    (tmp_path / "instructions.md").write_text("x")
    got = CliRunner().invoke(app, ["run", "--executor", "replay", "--variant", str(tmp_path), "--slot", "52"])
    assert got.exit_code == 2 and "mcp-agent" in got.output


def test_the_cli_exports_text(tmp_path):
    from typer.testing import CliRunner

    from openajar.lanebench_cli import app

    got = CliRunner().invoke(app, ["export-text", str(tmp_path / "out")])
    assert got.exit_code == 0, got.output
    assert sorted(p.name for p in Path(tmp_path / "out").iterdir()) == sorted(variant.FILES)
