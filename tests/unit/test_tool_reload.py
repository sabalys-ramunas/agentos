"""Live tool reload (openajar.mcp.reload): connector and flow tools appear, change and go while
agentosd runs, connected clients hear tools/list_changed, and nothing restarts."""

import asyncio

import pytest
from connector_samples import SITES, bind, connector, read_op
from mcp import Client

from openajar.core.config import NodeConfig
from openajar.core.errors import InvalidRequest, PolicyDenied
from openajar.core.models import Effect, Principal
from openajar.core.runtime import Runtime
from openajar.core.tools import ToolSpec
from openajar.mcp.registry import build_mcp
from openajar.mcp.reload import ToolWatch, fingerprint, refresh, reload_tools
from openajar.ops import control
from openajar.runner import tools as runner_tools
from openajar.runner.connectors.compile import compile_connector
from openajar.runner.connectors.service import ConnectorService
from openajar.runner.fakes import OWNER, Harness


async def one(ctx) -> dict:
    """The first tool."""
    return {"tool": "one"}


async def two(ctx, *, word: str) -> dict:
    """The second tool."""
    return {"tool": "two", "word": word}


async def two_v2(ctx, *, word: str, loud: bool = False) -> dict:
    """The second tool, now with a switch."""
    return {"tool": "two", "word": word.upper() if loud else word}


def spec(name, handler):
    return ToolSpec(name=name, handler=handler, effect=Effect.READ)


class Specs:
    """A provider whose tools a test changes, like a flows store after an install."""

    def __init__(self, *specs):
        self.specs = list(specs)

    def __call__(self, runtime):
        return list(self.specs)


def server_with(*specs):
    runtime = Runtime(config=NodeConfig())
    provider = Specs(*specs)
    return runtime, provider, build_mcp(runtime, [provider])


async def names(client) -> list[str]:
    return sorted(t.name for t in (await client.list_tools()).tools)


# ---------------------------------------------------------------- the diff


async def test_refresh_adds_removes_and_replaces_only_what_changed():
    runtime, provider, server = server_with(spec("one", one), spec("two", two))
    kept = server.specs["one"]
    provider.specs = [spec("one", one), spec("two", two_v2), spec("three", one)]
    changes = refresh(server, runtime)
    assert changes.added == ["three"] and changes.changed == ["two"] and changes.removed == []
    assert server.specs["one"] is kept  # the same tool, left alone
    async with Client(server) as client:
        assert await names(client) == ["one", "three", "two"]
        result = await client.call_tool("two", {"word": "hi", "loud": True})
        assert result.structured_content == {"tool": "two", "word": "HI"}
    provider.specs = [spec("one", one)]
    changes = refresh(server, runtime)
    assert sorted(changes.removed) == ["three", "two"] and not changes.added
    async with Client(server) as client:
        assert await names(client) == ["one"]
        gone = await client.call_tool("two", {"word": "hi"})
        assert gone.is_error and "not_found" in gone.content[0].text
    assert not refresh(server, runtime)  # nothing changed: nothing to tell anyone


def test_same_tool_same_fingerprint_and_a_changed_argument_differs():
    assert fingerprint(spec("two", two)) == fingerprint(spec("two", two))
    assert fingerprint(spec("two", two)) != fingerprint(spec("two", two_v2))


async def test_a_tool_that_cannot_be_served_does_not_stop_the_others():
    async def broken(ctx, word) -> dict:  # not keyword-only: refused by the registry
        """Broken."""
        return {}

    runtime, provider, server = server_with(spec("one", one))
    provider.specs = [spec("one", one), spec("bad", broken), spec("two", two)]
    changes = refresh(server, runtime)
    assert changes.added == ["two"] and "bad" in changes.failed
    assert set(server.specs) == {"one", "two"}


# ---------------------------------------------------------------- telling clients


async def test_clients_on_both_protocols_hear_that_the_list_changed():
    runtime, provider, server = server_with(spec("one", one))
    heard = []

    async def on_message(message):
        heard.append(getattr(message, "method", None))

    async with Client(server, mode="legacy", message_handler=on_message) as legacy:
        assert legacy.session.server_capabilities.tools.list_changed is True
        assert await names(legacy) == ["one"]  # a client that listed tools is told
        async with Client(server) as modern, modern.listen(tools_list_changed=True) as sub:
            provider.specs.append(spec("two", two))
            changes = await reload_tools(runtime, server)
            assert changes.added == ["two"]

            async def first():
                async for event in sub:
                    return event

            assert type(await asyncio.wait_for(first(), 5)).__name__ == "ToolsListChanged"
            assert await names(modern) == ["one", "two"]
        for _ in range(50):
            if heard:
                break
            await asyncio.sleep(0.02)
        assert heard == ["notifications/tools/list_changed"]
        assert await names(legacy) == ["one", "two"]


async def test_reload_tells_the_controller_channel():
    runtime, provider, server = server_with(spec("one", one))

    class Channel:
        announced = 0

        async def announce_tools(self):
            Channel.announced += 1

    runtime.register("channel", Channel())
    provider.specs.append(spec("two", two))
    await reload_tools(runtime, server)
    await reload_tools(runtime, server)  # no change: not announced again
    assert Channel.announced == 1


# ---------------------------------------------------------------- the watch


async def test_the_watch_reloads_when_its_signature_changes_and_when_poked():
    runtime, provider, server = server_with(spec("one", one))
    stamp = {"v": 1}
    watch = ToolWatch(runtime, server, poll_s=0.05, sources=[lambda _rt: stamp["v"]])
    await watch.start()
    try:
        provider.specs.append(spec("two", two))
        await asyncio.sleep(0.2)
        assert "two" not in server.specs  # the signature did not move: no reload
        stamp["v"] = 2
        for _ in range(100):
            if "two" in server.specs:
                break
            await asyncio.sleep(0.02)
        assert "two" in server.specs and watch.reloads == 1
        provider.specs.pop()
        watch.poke()
        for _ in range(100):
            if "two" not in server.specs:
                break
            await asyncio.sleep(0.02)
        assert "two" not in server.specs
    finally:
        await watch.stop()


# ---------------------------------------------------------------- connectors, end to end


def _install(h, version="1.0.0", ops=None):
    doc = connector(ops or {"profile_summary": read_op()}, version=version)
    return ConnectorService(h.flows, sites=SITES).install(compile_connector(doc, sites=SITES), by=OWNER)


async def test_a_connector_installed_by_another_process_appears_without_a_restart(tmp_path):
    daemon = Harness(tmp_path)  # agentosd's runtime
    server = build_mcp(daemon.runtime, [runner_tools.tool_specs])
    assert "social_profile_summary" not in server.specs
    watch = ToolWatch(daemon.runtime, server, poll_s=0.05)
    await watch.start()
    try:
        cli = Harness(tmp_path)  # `openajar connector install`: its own process, the same database
        _install(cli)
        bind(cli)
        for _ in range(100):
            if "social_profile_summary" in server.specs:
                break
            await asyncio.sleep(0.02)
        assert "social_profile_summary" in server.specs
        async with Client(server) as client:
            assert "social_profile_summary" in await names(client)
        _install(cli, "1.1.0", {"profile_summary": read_op(), "profile_again": read_op()})  # an upgrade
        changes = await watch.reload()
        assert changes.added == ["social_profile_again"]
        assert ConnectorService(cli.flows).remove("social", by=OWNER) == [
            "connector.social.profile_again", "connector.social.profile_summary"]
        changes = await watch.reload()
        assert sorted(changes.removed) == ["social_profile_again", "social_profile_summary"]
        assert not any(name.startswith("social_") for name in server.specs)
    finally:
        await watch.stop()


def test_only_the_owner_removes_a_connector_and_not_while_a_schedule_runs_it(tmp_path):
    h = Harness(tmp_path)
    _install(h)
    svc = ConnectorService(h.flows)
    with pytest.raises(PolicyDenied):
        svc.remove("social", by=Principal(kind="agent", id="claude-code"))
    with pytest.raises(InvalidRequest, match="no connector"):
        svc.remove("nothing", by=OWNER)

    class Sched:
        id, flow_id = "sch_1", "connector.social.profile_summary"

    h.flows.store.list_schedules = lambda: [Sched()]
    with pytest.raises(InvalidRequest, match="sch_1"):
        svc.remove("social", by=OWNER)


async def test_the_ops_socket_reloads_tools():
    runtime, provider, server = server_with(spec("one", one))
    watch = ToolWatch(runtime, server, sources=[])
    runtime.register("tool_reload", watch)

    class Service:
        pass

    service = Service()
    service.runtime = runtime
    provider.specs.append(spec("two", two))
    assert await control.dispatch(service, {"op": "reload_tools"}) == {
        "ok": True, "tools": {"added": ["two"], "removed": [], "changed": []}}
    service.runtime = Runtime()
    assert (await control.dispatch(service, {"op": "reload_tools"}))["ok"] is False
