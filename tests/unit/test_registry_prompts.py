"""PromptSpecs on the MCP SDK: listed with their arguments, rendered, masked, hidden without
their service, and refused when two share a name."""

from __future__ import annotations

import pytest
from mcp import Client

from agentos.core.runtime import Runtime
from agentos.core.tools import PromptArg, PromptSpec, ToolSpec
from agentos.mcp.registry import build_mcp, collect


async def plain(ctx) -> dict:
    """A tool."""
    return {}


def greet(on: str = "<lane>", reason: str = "") -> str:
    return f"Open {on}{' because ' + reason if reason else ''}. Proxy: http://user:hunter22@proxy.example:8080"


GREET = PromptSpec(name="greet", title="Greet", description="Say hello to a lane.", render=greet,
                   arguments=(PromptArg("on", "The lane.", required=True), PromptArg("reason", "Why.")))


def provider(*items):
    return lambda runtime: list(items)


async def test_prompts_are_listed_and_rendered_with_only_the_arguments_given():
    server = build_mcp(Runtime(), [provider(ToolSpec(name="plain_tool", handler=plain), GREET)])
    async with Client(server) as client:
        [prompt] = (await client.list_prompts()).prompts
        assert (prompt.name, prompt.title, prompt.description) == ("greet", "Greet", "Say hello to a lane.")
        assert [(a.name, a.required) for a in prompt.arguments] == [("on", True), ("reason", False)]
        result = await client.get_prompt("greet", {"on": "personal/reddit", "reason": "  "})
        text = result.messages[0].content.text
        assert result.messages[0].role == "user"
        assert text.startswith("Open personal/reddit. Proxy:")  # the blank reason is not passed
        assert "hunter22" not in text and "***@" in text  # masked like every tool result
        assert {t.name for t in (await client.list_tools()).tools} == {"plain_tool"}


async def test_a_missing_required_argument_is_an_error():
    server = build_mcp(Runtime(), [provider(GREET)])
    async with Client(server) as client:
        with pytest.raises(Exception):  # noqa: B017 - the SDK reports it as an internal error
            await client.get_prompt("greet", {})


def test_prompts_hide_without_their_service_and_names_must_be_unique():
    hidden = PromptSpec(name="vaulted", description="x", render=greet, hidden_without="vault")
    tools, prompts = collect(Runtime(), [provider(GREET, hidden)])
    assert tools == [] and [p.name for p in prompts] == ["greet"]
    assert [p.name for p in collect(Runtime(vault=object()), [provider(GREET, hidden)])[1]] == ["greet", "vaulted"]
    with pytest.raises(ValueError, match="duplicate prompt name 'greet'"):
        collect(Runtime(), [provider(GREET), provider(GREET)])
    # a prompt and a tool may share a name: they are different lists in MCP
    same = ToolSpec(name="greet", handler=plain)
    assert len(collect(Runtime(), [provider(GREET, same)])[0]) == 1
