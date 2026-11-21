"""QA 2026-11-07 #4: memory_context on the default agent grant. The node's instructions ask an agent
to call it first in every workspace, so `openajar connect` and `dev up` give memory_read (the memory
special without write); writing memory stays a special the human adds. A grant without it gets a
hint that names the special, not a ceiling."""

from __future__ import annotations

from test_policy_support import make_service

from openajar.api.bridge import panel_specials
from openajar.core.models import Effect
from openajar.core.tools import ToolSpec
from openajar.dev.node import issue_agent_grant
from openajar.mcp.registry import grant_hint
from openajar.policy import memory_rules


async def _noop(ctx, **kw):
    return None


def spec(name: str, effect: Effect) -> ToolSpec:
    return ToolSpec(name=name, handler=_noop, effect=effect, tags=("memory",))


def test_panel_specials_maps_memory_read_to_a_read_only_memory_special():
    assert panel_specials(["memory_read"]) == {"memory": {}}
    assert panel_specials(["memory"]) == {"memory": {"write": True}}
    assert panel_specials(["memory_read", "memory"]) == {"memory": {"write": True}}
    assert panel_specials(["memory", "memory_read"]) == {"memory": {"write": True}}
    assert panel_specials(["exec"]) == {"exec": {}}


def test_dev_up_grant_sees_memory_context_and_not_memory_write(tmp_path):
    svc, rt = make_service(tmp_path)
    try:
        _, token = issue_agent_grant(rt)
        _, g = svc.resolve(token)
        assert memory_rules.spec_visible(g, spec("memory_context", Effect.READ))
        assert not memory_rules.spec_visible(g, spec("memory_write", Effect.DRAFT))
    finally:
        svc.close()


def test_grant_hint_for_a_memory_tool_names_the_special():
    read = grant_hint(spec("memory_context", Effect.READ))
    assert "--special memory_read" in read and "--ceiling" not in read
    write = grant_hint(spec("memory_write", Effect.DRAFT))
    assert "--special memory " in write and "--ceiling" not in write


def test_grant_hint_for_forget_on_a_memory_writer_names_the_ceiling():
    """Finding 34 of 2026-11-10: a grant that writes memory but stops at commit lacks irreversible,
    not the memory special."""
    from types import SimpleNamespace

    from openajar.mcp.registry import ToolServer

    forget, write, read = (spec("memory_forget", Effect.IRREVERSIBLE), spec("memory_write", Effect.DRAFT),
                           spec("memory_context", Effect.READ))
    reg = SimpleNamespace(specs={s.name: s for s in (forget, write, read)})
    same = lambda names: ToolServer._has_same_special(reg, forget, names)
    assert same({"memory_write", "memory_context"})
    assert not same({"memory_context"}) and not same(None)
    hint = grant_hint(forget, same({"memory_write"}))
    assert "--ceiling irreversible" in hint and "--special" not in hint
    assert "--special memory " in grant_hint(forget, same({"memory_context"}))


def test_grant_hint_for_an_environment_tool_names_the_environments_bundle():
    """`--special vm` alone gives no exec on the machine: the hint names the bundle connect uses."""
    hint = grant_hint(ToolSpec(name="environment_open", handler=_noop, effect=Effect.VM))
    assert "--special environments --new-grant" in hint and "--special vm " not in hint
