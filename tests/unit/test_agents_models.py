"""Agent lane contracts (brief B0): specs stay inside their workspace, ceilings are on the
ladder, ids are well formed, and the port shapes validate."""

import pytest
from pydantic import ValidationError

from agentos.agents.models import AgentLaneSpec, AgentState, Session, Turn, TurnStatus
from agentos.agents.ports import AgentsPort, TurnGrant, TurnReceipt, TurnRequest
from agentos.core.models import PENDING_SPECIALS, Effect, LaneKind, Preset


def test_core_contracts_are_additive():
    assert LaneKind.AGENT == "agent" and Preset.AGENT == "agent"
    assert Effect.AGENT.rank is None  # a special, not on the ladder
    assert Effect.AGENT in PENDING_SPECIALS


def test_spec_defaults_follow_the_design():
    spec = AgentLaneSpec(id="biz-a/claude", runner="claude", reach={"lanes": ["biz-a/*"], "ceiling": "commit"})
    assert spec.workspace == "biz-a" and spec.name == "claude"
    assert spec.credential == "cli-native" and spec.preset == "agent"
    assert spec.sessions.events == "new" and spec.sessions.owner == "continue"
    assert spec.limits.turns_per_hour == 6 and spec.limits.turns_per_day == 48
    assert spec.remote.vendor_remote_control is False
    assert spec.grant_ttl_days == 30


def test_spec_never_reaches_another_workspace():
    with pytest.raises(ValidationError, match="outside workspace"):
        AgentLaneSpec(id="biz-a/claude", runner="claude", reach={"lanes": ["biz-b/instagram"]})
    with pytest.raises(ValidationError, match="outside workspace"):
        AgentLaneSpec(id="biz-a/claude", runner="claude", reach={"lanes": ["*/instagram"]})
    with pytest.raises(ValidationError, match="itself"):
        AgentLaneSpec(id="biz-a/claude", runner="claude", reach={"lanes": ["biz-a/claude"]})


def test_spec_validation():
    with pytest.raises(ValidationError, match="ceiling"):
        AgentLaneSpec(id="biz-a/claude", runner="claude", reach={"ceiling": "exec"})
    with pytest.raises(ValidationError):
        AgentLaneSpec(id="Biz A/claude", runner="claude")
    with pytest.raises(ValidationError):
        AgentLaneSpec(id="biz-a/claude", runner="gpt-cli")
    with pytest.raises(ValidationError, match="credential"):
        AgentLaneSpec(id="biz-a/claude", runner="claude", credential={"token": "x"})
    with pytest.raises(ValidationError, match="cli-native"):
        AgentLaneSpec(id="biz-a/hermes", runner="hermes", credential={"brain": "claude-max"})
    with pytest.raises(ValidationError, match="remote"):
        AgentLaneSpec(id="biz-a/claude", runner="claude", repos=[{"remote": "file:///etc"}])
    with pytest.raises(ValidationError, match="host"):
        AgentLaneSpec(id="biz-a/claude", runner="claude", egress_extra=["not a host"])
    spec = AgentLaneSpec(id="biz-a/claude", runner="codex", credential={"brain": "codex-key"},
                         egress_extra=["Registry.npmjs.org.", "registry.npmjs.org"],
                         repos=[{"remote": "git@github.com:biz-a/content.git"}], volumes=[{"name": "brandkit"}])
    assert spec.egress_extra == ["registry.npmjs.org"]
    assert spec.volumes[0].mode == "ro"


def test_turn_and_session_ids():
    Turn(id="tu_0123456789ab", lane="biz-a/claude", key="rt_x:ev_y:1")
    Session(id="se_0123456789ab", lane="biz-a/claude", taint=["untrusted"])
    with pytest.raises(ValidationError):
        Turn(id="turn-1", lane="biz-a/claude", key="k")
    with pytest.raises(ValidationError):
        Session(id="se_0123456789ab", lane="biz-a/claude", taint=["secret"])


def test_states():
    assert AgentState.COLD.accepts_turns and not AgentState.ATTACHED.accepts_turns
    assert AgentState.ATTACHED.queues_turns and AgentState.NEEDS_AUTH.queues_turns
    assert TurnStatus.DONE_UNVERIFIED.terminal and not TurnStatus.RUNNING.terminal
    assert "done" not in {s.value for s in TurnStatus}  # never a bare done


def test_port_shapes():
    req = TurnRequest(lane="biz-a/claude", delivery_id="dl_0123456789ab", key="rt_a:ev_b:1",
                      event_ids=["ev_0123456789ab"], prompt="hi", grant=TurnGrant(lanes=["biz-a/instagram"]),
                      taint_seed=["untrusted"])
    assert req.grant.ceiling == Effect.DRAFT and req.session == "new"
    with pytest.raises(ValidationError):
        TurnRequest(lane="biz-a/claude", delivery_id="d", key="k", event_ids=[], prompt="p")
    assert TurnReceipt(turn_id="tu_0123456789ab").status == TurnStatus.QUEUED

    class Fake:
        def lane_state(self, lane):
            return AgentState.COLD

        async def deliver(self, request):
            return TurnReceipt()

    assert isinstance(Fake(), AgentsPort)
