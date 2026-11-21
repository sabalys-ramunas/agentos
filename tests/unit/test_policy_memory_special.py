"""The `memory` grant special (docs/design/workspace-memory.md 4.7): scope by workspace, flags that
only turn off, workspaces inside the lane scope, tool visibility and grant_info."""

from __future__ import annotations

import pytest
from test_policy_support import OWNER, make_service

from openajar.core.errors import InvalidRequest
from openajar.core.models import Effect
from openajar.core.tools import ToolSpec
from openajar.policy import memory_rules
from openajar.policy.errors import Denied
from openajar.policy.grants import Grant, GrantBlock, Specials, Subject, owner_block, widening_problems
from openajar.policy.memory_rules import MemoryCap, check_memory, memory_scope
from openajar.policy.tools import grant_info

FULL = {"write": True, "private": True, "audience_owner": True}


@pytest.fixture
def svc(tmp_path):
    s, _ = make_service(tmp_path)
    yield s
    s.close()


def issue(svc, memory: dict | None, **kw):
    kw.setdefault("subject", {"id": "agent-1"})
    kw.setdefault("workspaces", ["biz-a", "biz-b"])
    kw.setdefault("ceiling", Effect.IRREVERSIBLE)
    specials = Specials(memory=MemoryCap(**memory)) if memory is not None else None
    return svc.issue(specials=specials, **kw)


def scope_of(svc, tok, ws):
    _, g = svc.resolve(tok)
    return memory_scope(g, ws)


def test_the_cap_validates_workspaces_and_types():
    assert MemoryCap(workspaces=["biz-a", "biz-*"], types=["fact", "decision"]).write is False
    with pytest.raises(ValueError):
        MemoryCap(types=["secret"])
    with pytest.raises((ValueError, InvalidRequest)):
        MemoryCap(workspaces=["Biz A"])
    with pytest.raises(ValueError):
        MemoryCap(read=True)  # extra fields are refused, like every special


def test_flags_default_to_off_so_an_empty_special_is_read_only(svc):
    _, tok = issue(svc, {})
    s = scope_of(svc, tok, "biz-a")
    assert (s.write, s.private, s.audience_owner, s.types) == (False, False, False, None)
    _, g = svc.resolve(tok)
    with pytest.raises(Denied) as e:
        check_memory(g, "biz-a", write=True)
    assert e.value.reason == "special"


def test_a_grant_without_the_special_has_no_memory(svc):
    _, tok = issue(svc, None)
    _, g = svc.resolve(tok)
    with pytest.raises(Denied) as e:
        memory_scope(g, "biz-a")
    assert e.value.reason == "special"
    assert not memory_rules.has_memory(g)


def test_another_workspace_is_out_of_scope(svc):
    _, tok = issue(svc, {"workspaces": ["biz-a"], "write": True})
    assert scope_of(svc, tok, "biz-a").write
    for ws in ("biz-b", "personal"):
        with pytest.raises(Denied) as e:
            scope_of(svc, tok, ws)
        assert e.value.reason == "out_of_scope"


def test_memory_never_reaches_past_the_lane_scope(svc):
    _, tok = issue(svc, {"write": True}, workspaces=["biz-a"])   # memory workspaces unset: the lane scope
    assert scope_of(svc, tok, "biz-a").write
    with pytest.raises(Denied) as e:
        scope_of(svc, tok, "biz-b")
    assert e.value.reason == "out_of_scope"


def test_a_pattern_or_a_bad_name_is_not_a_workspace(svc):
    _, tok = issue(svc, {"workspaces": ["biz-*"]})
    assert scope_of(svc, tok, "biz-a").workspace == "biz-a"
    for ws in ("biz-*", "*", "../biz-a", ""):
        with pytest.raises(Denied) as e:
            scope_of(svc, tok, ws)
        assert e.value.reason == "out_of_scope"


def test_a_lane_only_grant_reads_its_whole_workspace(svc):
    _, tok = issue(svc, {"workspaces": ["biz-a"]}, workspaces=None, lanes=["biz-a/instagram"])
    assert scope_of(svc, tok, "biz-a").workspace == "biz-a"


def test_memory_workspaces_outside_the_lane_scope_are_refused_when_issued(svc):
    with pytest.raises(InvalidRequest, match="outside"):
        issue(svc, {"workspaces": ["biz-b"]}, workspaces=["biz-a"])
    with pytest.raises(InvalidRequest, match="outside"):
        issue(svc, {"workspaces": ["personal"]}, workspaces=None, lanes=["biz-a/instagram"])
    issue(svc, {"workspaces": ["biz-*"]}, workspaces=["biz-a"])   # overlaps: biz-a is reached


def test_types_narrow_what_may_be_written(svc):
    _, tok = issue(svc, {"write": True, "types": ["fact", "document"]})
    _, g = svc.resolve(tok)
    assert check_memory(g, "biz-a", write=True, entry_type="fact").may_write("document")
    with pytest.raises(Denied, match="decision"):
        check_memory(g, "biz-a", write=True, entry_type="decision")


def test_audience_owner_takes_effect_only_in_an_admin_grant(svc):
    _, tok = issue(svc, FULL)
    assert scope_of(svc, tok, "biz-a").audience_owner is False
    _, tok = issue(svc, FULL, admin=True)
    s = scope_of(svc, tok, "biz-a")
    assert s.audience_owner and s.private and s.write


def test_the_owner_grant_has_it_all(svc):
    _, g = svc.resolve(OWNER)
    s = memory_scope(g, "any-ws")
    assert (s.write, s.private, s.audience_owner, s.types) == (True, True, True, None)
    assert owner_block().specials.memory == MemoryCap.everything()


# ---------------------------------------------------------------- narrowing

@pytest.mark.parametrize("field", ["write", "private", "audience_owner"])
def test_a_child_cannot_turn_a_flag_on(svc, field):
    parent = {"write": True, "private": False, "audience_owner": False}
    parent[field] = False
    _, tok = issue(svc, parent, admin=True)
    with pytest.raises(InvalidRequest, match=f"specials.memory.{field}"):
        svc.attenuate(tok, specials=Specials(memory=MemoryCap(**{field: True})), admin=True)


def test_a_child_turns_flags_off_and_shrinks_workspaces_and_types(svc):
    _, tok = issue(svc, {"workspaces": ["biz-a", "biz-b"], "types": ["fact", "document"], **FULL}, admin=True)
    _, child = svc.attenuate(tok, specials=Specials(memory=MemoryCap(workspaces=["biz-a"], types=["fact"],
                                                                      write=True)))
    s = scope_of(svc, child, "biz-a")
    assert (s.write, s.private, s.audience_owner, s.types) == (True, False, False, frozenset({"fact"}))
    with pytest.raises(Denied):
        scope_of(svc, child, "biz-b")
    for wider in ({"workspaces": ["biz-a", "personal"]}, {"workspaces": None}, {"types": ["fact", "decision"]},
                  {"types": None}):
        with pytest.raises(InvalidRequest, match="specials.memory"):
            svc.attenuate(tok, specials=Specials(memory=MemoryCap(**wider)))


def test_a_child_that_narrows_its_lanes_away_from_memory_is_refused(svc):
    _, tok = issue(svc, {"workspaces": ["biz-b"]})
    with pytest.raises(InvalidRequest, match="specials.memory.workspaces"):
        svc.attenuate(tok, workspaces=["biz-a"], specials=Specials(memory=MemoryCap(workspaces=["biz-b"])))
    # inheriting the special while narrowing the lanes is not a widening: it simply reaches nothing
    _, child = svc.attenuate(tok, workspaces=["biz-a"])
    with pytest.raises(Denied) as e:
        scope_of(svc, child, "biz-b")
    assert e.value.reason == "out_of_scope"


def test_an_offline_widening_is_ignored_at_evaluation():
    """A block built without the node's widening check still cannot widen: every block counts."""
    sub = Subject(id="agent-1")
    root = GrantBlock(id="gr_aaaaaaaaaaaaaaaa", rid="rv_aaaaaaaaaaaaaaaa", iat=0, sub=sub, workspaces=["biz-a"],
                      exp=10, specials=Specials(memory=MemoryCap(workspaces=["biz-a"])))
    child = GrantBlock(id="gr_bbbbbbbbbbbbbbbb", rid="rv_bbbbbbbbbbbbbbbb", parent=root.id, iat=0,
                       specials=Specials(memory=MemoryCap(**FULL)), admin=True)
    g = Grant([root, child])
    assert widening_problems(Grant([root]), child, tiers=["small"], aliases={}) == [
        "admin", "specials.memory.audience_owner", "specials.memory.private", "specials.memory.workspaces",
        "specials.memory.write"]
    s = memory_scope(g, "biz-a")
    assert (s.write, s.private, s.audience_owner) == (False, False, False)


def test_the_special_survives_the_token_both_versions(svc):
    from test_policy_support import issue_v1

    _, v2 = issue(svc, {"workspaces": ["biz-a"], "write": True, "types": ["fact"]})
    assert scope_of(svc, v2, "biz-a").types == frozenset({"fact"})
    _, v1 = issue_v1(svc, workspaces=["biz-a"], specials=Specials(memory=MemoryCap(write=True)))
    assert scope_of(svc, v1, "biz-a").write


# ---------------------------------------------------------------- surface

async def _h(ctx, *, workspace: str) -> dict:
    """A memory tool."""
    await ctx.authorize(Effect.READ)
    return {}


MEM_READ = ToolSpec(name="memory_search", handler=_h, lane_arg=None, tags=("memory",))
MEM_WRITE = ToolSpec(name="memory_write", handler=_h, lane_arg=None, effect=Effect.DRAFT, tags=("memory",))
OTHER = ToolSpec(name="node_info", handler=_h, lane_arg=None)


def test_memory_tools_are_hidden_without_the_special_and_writes_without_write(svc):
    names = lambda tok: {s.name for s in svc.visible_tools(tok, [MEM_READ, MEM_WRITE, OTHER])}
    assert names(issue(svc, None)[1]) == {"node_info"}
    assert names(issue(svc, {})[1]) == {"node_info", "memory_search"}
    assert names(issue(svc, {"write": True})[1]) == {"node_info", "memory_search", "memory_write"}
    assert names(OWNER) == {"node_info", "memory_search", "memory_write"}


async def test_grant_info_shows_the_special(svc):
    _, tok = issue(svc, {"workspaces": ["biz-a"], "write": True, "types": ["fact"]})
    info = await grant_info(svc.context_for_token(tok, tool="grant_info"))
    assert "memory" in info["specials"]
    assert info["memory"] == {"workspaces": ["biz-a"], "write": True, "types": ["fact"], "private": False,
                              "audience_owner": False}
    assert "It may read and write workspace memory in biz-a (it writes only fact entries)." in info["explain"]
    _, tok = issue(svc, None)
    assert (await grant_info(svc.context_for_token(tok, tool="grant_info")))["memory"] is None


def test_a_brain_child_cap_writes_its_workspace_never_audience_owner(svc):
    _, tok = issue(svc, {"workspaces": ["biz-a"], "write": True, "private": True, "audience_owner": True},
                   admin=True)
    _, g = svc.resolve(tok)
    cap = memory_rules.child_cap(g, "biz-a")
    assert cap == MemoryCap(workspaces=["biz-a"], write=True, private=True, audience_owner=False)
    assert memory_rules.child_cap(g, "biz-b") is None
    _, ro = issue(svc, {})
    assert memory_rules.child_cap(svc.resolve(ro)[1], "biz-a") is None


def test_a_special_with_no_grant_field_yet_is_denied_not_an_error():
    """Effect.AGENT exists for agent lanes before any grant can carry it: deny, never raise."""
    from openajar.core.models import PENDING_SPECIALS, Effect
    from openajar.policy.grants import Grant, owner_block

    assert Effect.AGENT in PENDING_SPECIALS
    assert Grant([owner_block()]).effect_allowed(Effect.AGENT) is False
