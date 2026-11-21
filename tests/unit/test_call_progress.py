"""Progress notifications and notices on long tool calls (openajar.core.progress, openajar.mcp.registry)."""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp import Client

from openajar.core import progress
from openajar.core.config import NodeConfig
from openajar.core.models import Effect
from openajar.core.runtime import Runtime
from openajar.core.tools import ToolSpec
from openajar.mcp.registry import build_mcp


def _runtime() -> Runtime:
    return Runtime(config=NodeConfig(mcp_token="t" * 32))


async def slow(ctx, *, seconds: float = 0.3) -> dict:
    """A call that takes a while and names its step."""
    progress.step("taking the disk snapshot")
    await asyncio.sleep(seconds)
    progress.notice("your own stuck browser was closed")
    return {"ok": True}


async def quick_text(ctx) -> str:
    """Text, with a notice."""
    progress.notice("noted")
    return "done"


def _server():
    specs = [ToolSpec(name="slow", handler=slow, effect=Effect.READ),
             ToolSpec(name="quick_text", handler=quick_text, effect=Effect.READ)]
    return build_mcp(_runtime(), [lambda rt: specs])


async def test_a_long_call_sends_progress_with_its_step(monkeypatch):
    monkeypatch.setattr(progress, "HEARTBEAT_S", 0.05)
    got: list[tuple[float, str | None]] = []

    async def on_progress(p: float, total: float | None, message: str | None) -> None:
        got.append((p, message))

    async with Client(_server()) as client:
        result = await client.call_tool("slow", {"seconds": 0.4}, progress_callback=on_progress)
    assert not result.is_error
    assert len(got) >= 2
    assert all("taking the disk snapshot" in (m or "") for _, m in got)
    assert got[0][0] < got[-1][0]  # progress only grows


async def test_a_notice_reaches_the_result():
    async with Client(_server()) as client:
        result = await client.call_tool("slow", {"seconds": 0.0})
        text = await client.call_tool("quick_text", {})
    assert result.structured_content["notice"] == "your own stuck browser was closed"
    assert json.loads(result.content[0].text)["notice"] == "your own stuck browser was closed"
    assert text.content[-1].text == "notice: noted"


async def test_background_work_goes_on_past_the_budget_and_a_second_call_joins_it():
    bg = progress.Background()
    started = 0
    release = asyncio.Event()

    async def work() -> str:
        nonlocal started
        started += 1
        await release.wait()
        return "saved"

    with pytest.raises(progress.StillWorking) as info:
        await bg.run("close:e1", work, budget_s=0.05, still=lambda: {"state": "saving", "next": "call environment()"})
    assert info.value.result["state"] == "saving"
    assert bg.running("close:e1")
    release.set()
    assert await bg.run("close:e1", work, budget_s=1, still=dict) == "saved"
    assert started == 1  # joined, never a second copy


async def test_abandon_after_does_not_wait_for_work_that_ignores_cancellation():
    release = asyncio.Event()

    async def stubborn() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await release.wait()  # a call stuck on a dead connection: it takes its cancellation late

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with pytest.raises(TimeoutError):
        await progress.abandon_after(stubborn(), 0.05)
    assert loop.time() - t0 < 1
    release.set()


# ---- over HTTP, as Claude Code connects: both protocols ----

TOKEN = "p" * 40


@pytest.mark.parametrize("mode", ["legacy", "2026-07-28"])
async def test_progress_reaches_a_client_over_http(tmp_path, monkeypatch, mode):
    import httpx2
    from fakes import mcp_port_for_tests
    from mcp.client.streamable_http import streamable_http_client

    from openajar.mcp.http import build_app
    from openajar.node import daemon

    monkeypatch.setattr(progress, "HEARTBEAT_S", 0.05)
    config = NodeConfig(home=tmp_path, mcp_token=TOKEN, mcp_port=mcp_port_for_tests())
    runtime = Runtime(config=config)
    specs = [ToolSpec(name="slow", handler=slow, effect=Effect.READ)]
    server = daemon._Uvicorn.server(build_app(runtime, build_mcp(runtime, [lambda rt: specs])), config)
    task = asyncio.create_task(server.serve())
    got: list[str | None] = []

    async def on_progress(p: float, total: float | None, message: str | None) -> None:
        got.append(message)

    try:
        while not server.started:
            await asyncio.sleep(0.02)
        url = f"http://localhost:{daemon.bound_port(server)}/mcp"
        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}) as http,
            Client(streamable_http_client(url, http_client=http), mode=mode) as client,
        ):
            result = await client.call_tool("slow", {"seconds": 0.5}, progress_callback=on_progress)
        assert not result.is_error
        assert got and "taking the disk snapshot" in (got[0] or "")
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 15)
