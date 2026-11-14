"""Compatibility front for the M1 API: `build_server(manager, interaction, config)`.

The real pieces live in agentos.mcp.registry (ToolSpecs on the SDK, one wrapper per call),
agentos.mcp.http (the node's HTTP app) and agentos.node.tools (the M1 tools). agentosd uses
those directly (agentos.node.daemon); this module keeps older callers and imports working.
"""

from __future__ import annotations

from starlette.types import ASGIApp

from agentos.core.config import NodeConfig
from agentos.core.runtime import Runtime
from agentos.mcp.http import MCP_PATH, BearerAuth, build_app
from agentos.mcp.redact import Redactor
from agentos.mcp.registry import ToolServer, Wrap, allow_all_wrap, build_mcp, error_message, error_result
from agentos.node.interfaces import Interaction, LaneManager
from agentos.node.tools import (
    INSTRUCTIONS,
    NEEDS_TEXT,
    lane_id,
    lane_summary,
    normalize_ref,
    normalize_url,
    tool_specs,
    viewer_dict,
)

__all__ = [
    "INSTRUCTIONS", "MCP_PATH", "NEEDS_TEXT", "BearerAuth", "Redactor", "build_http_app", "build_server",
    "error_message", "error_result", "lane_id", "lane_summary", "normalize_ref", "normalize_url", "viewer_dict",
]


def build_server(
    manager: LaneManager,
    interaction: Interaction,
    config: NodeConfig,
    *,
    ssh_host: str | None = None,
    wrap: Wrap = allow_all_wrap,
) -> ToolServer:
    """The M1 tools on `manager` and `interaction`. `ssh_host` overrides node.toml's alias in
    tunnel hints (AGENTOS_SSH_HOST still wins)."""
    if ssh_host is not None:
        config = config.model_copy(update={"ssh_host": ssh_host})
    runtime = Runtime(config=config, lanes=manager, interaction=interaction)
    return build_mcp(runtime, [tool_specs], wrap, instructions=INSTRUCTIONS)


def build_http_app(server: ToolServer, config: NodeConfig) -> ASGIApp:
    """/mcp behind the bearer check and the trusted-Host check, as agentosd serves it."""
    return build_app(Runtime(config=config), server)
