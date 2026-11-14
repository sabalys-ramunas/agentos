import pytest

from agentos.core.errors import InvalidRequest
from agentos.core.models import LaneKind, Preset
from agentos.env import presets
from agentos.env.models import EnvLane


def lane(preset, kind=LaneKind.TERMINAL, **over):
    profile, limits, policy = presets.resolve(preset, kind=kind, overrides=over)
    return EnvLane(id="ws/x", kind=kind, preset=Preset(preset), profile=profile, limits=limits, policy=policy)


def test_sandbox_defaults():
    p, lim, pol = presets.resolve("sandbox")
    assert (p.network, p.filesystem, p.processes, p.programs) == ("off", "ephemeral", "isolated", "overlay")
    assert lim.disk_mb and lim.memory_mb and lim.pids
    assert pol.shell and pol.exec and pol.install and pol.tier == 0 and pol.seccomp


def test_workbench_is_env0_and_labelled_weaker():
    p, _lim, pol = presets.resolve("workbench")
    assert (p.network, p.filesystem, p.processes) == ("shared", "shared", "shared")
    assert pol.tier == 2 and pol.warning and not pol.seccomp
    lw = lane("workbench")
    assert not lw.isolated
    assert "No isolation" in presets.describe(lw)


def test_service_and_locked_deny_egress_by_default():
    _, _, svc = presets.resolve("service", kind="service")
    assert svc.egress_default == "deny" and not svc.shell and not svc.install
    _, _, locked = presets.resolve("locked")
    assert locked.egress_default == "deny" and locked.signed_only and locked.noexec_writable and not locked.shell


def test_service_lanes_never_share_the_network():
    with pytest.raises(InvalidRequest, match="never share"):
        presets.resolve("workbench", kind="service")
    with pytest.raises(InvalidRequest):
        presets.resolve("service", kind="service", overrides={"network": "shared"})


def test_overrides_validated():
    with pytest.raises(InvalidRequest, match="allowlist needs network=isolated"):
        presets.resolve("sandbox", overrides={"egress_allowlist": ["example.com"]})
    with pytest.raises(InvalidRequest, match="unknown dial"):
        presets.resolve("sandbox", overrides={"gpu": True})
    with pytest.raises(InvalidRequest, match="not a host name"):
        presets.resolve("sandbox", overrides={"network": "isolated", "egress_allowlist": ["exa mple.com"]})
    with pytest.raises(InvalidRequest):
        presets.resolve("sandbox", overrides={"network": "wide"})
    with pytest.raises(InvalidRequest):
        presets.resolve("sandbox", overrides={"memory_mb": 0})
    p, _, _ = presets.resolve("sandbox", overrides={"network": "isolated",
                                                    "egress_allowlist": ["Example.COM.", "*.npmjs.org", "1.2.3.4:8443"]})
    assert p.egress_allowlist == ["*.npmjs.org", "1.2.3.4:8443", "example.com"]


def test_programs_dial_controls_installs():
    _, _, pol = presets.resolve("sandbox", overrides={"programs": "base"})
    assert not pol.install
    _, _, pol = presets.resolve("service", kind="service", overrides={"programs": "overlay"})
    assert pol.install


def test_weaker_dials_raise_the_tier_with_a_warning():
    _, _, pol = presets.resolve("sandbox", overrides={"network": "shared"})
    assert pol.tier == 2 and "shares the node network" in pol.warning
    _, _, pol = presets.resolve("sandbox", overrides={"filesystem": "shared"})
    assert pol.tier == 1 and pol.warning


def test_describe_is_honest_and_has_no_em_dash():
    for preset in ("workbench", "sandbox", "locked"):
        text = presets.describe(lane(preset))
        assert "\u2014" not in text
    sb = presets.describe(lane("sandbox"))
    assert "Not a VM" in sb and "network off" in sb and "tmpfs" in sb
    lk = presets.describe(lane("locked", network="isolated", egress_allowlist=["example.com"]))
    assert "microVM" in lk and "only to example.com" in lk and "noexec" in lk
    svc = presets.describe(lane("service", kind=LaneKind.SERVICE))
    assert "deny-all" in svc
