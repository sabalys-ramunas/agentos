import pytest

from openajar.core.errors import ApprovalPending, ServiceUnavailable
from openajar.core.models import Effect, IsolationProfile, LaneKind, Preset, RunStatus
from openajar.core.runtime import Runtime
from openajar.core.tools import ToolImage, ToolSpec


def test_effect_ladder_and_specials():
    assert Effect.READ.rank < Effect.DRAFT.rank < Effect.COMMIT.rank < Effect.PAY.rank < Effect.IRREVERSIBLE.rank
    assert Effect.EXEC.rank is None and Effect.BRAIN.rank is None


def test_lane_kinds_and_presets():
    assert {k.value for k in LaneKind} >= {"browser", "terminal", "service", "android"}
    assert Preset.ACCOUNT == "account"
    assert IsolationProfile().network == "isolated"
    assert RunStatus.SUCCEEDED_VERIFIED != RunStatus.SUCCEEDED_UNVERIFIED


def test_runtime_require_and_attribute():
    rt = Runtime(config="cfg")
    assert rt.config == "cfg" and rt.has("config") and not rt.has("vault")
    with pytest.raises(ServiceUnavailable):
        rt.require("vault")
    with pytest.raises(ServiceUnavailable):
        _ = rt.vault


async def _h(ctx, *, on: str) -> dict:
    """Do a thing on a lane."""
    return {"on": on}


def test_toolspec_validates_name_and_uses_docstring():
    spec = ToolSpec(name="lane_thing", handler=_h)
    assert spec.description == "Do a thing on a lane."
    with pytest.raises(ValueError):
        ToolSpec(name="lane.thing", handler=_h)


def test_approval_pending_carries_id():
    e = ApprovalPending("needs you", approval_id="ap_1")
    assert e.code == "approval_pending" and e.approval_id == "ap_1"
    assert ToolImage(b"x").mime_type == "image/png"
