"""Fail-closed audit of the node's security edge (2026-11-07): each test injects the failure a path
used to read as "allow" and asserts the refusal. The channel's non-finite `ts` is in
test_channel_node.py beside the other freshness checks."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_publish_normalize import facebook_comment

from agentos.core.models import ActsAs, Surface
from agentos.core.runtime import Runtime
from agentos.mcp.redact import Redactor
from agentos.node import commitgate, daemon, egress, publish
from agentos.node import tools as node_tools
from agentos.node.egress import verdicts
from agentos.policy.errors import Denied

LANE = "personal/facebook"


def rule(rule_id: str) -> egress.EffectRule:
    return next(r for r in egress.EFFECT_RULES if r.id == rule_id)


# ---- the egress guard: a grant whose ceiling cannot be read ---------------------------------

class _BrokenGrant:
    """A grant whose ceiling raises (Grant.ceiling on a grant with no blocks: IndexError)."""
    id = "g-broken"
    blocks = ()

    @property
    def ceiling(self):
        raise IndexError("no blocks")


def test_a_grant_whose_ceiling_cannot_be_read_is_held_to_read_not_unlimited():
    ctx = SimpleNamespace(principal=SimpleNamespace(id="agent-1", kind="agent"), grant=_BrokenGrant())
    driver = verdicts.Driver.from_context(ctx)
    assert driver.ceiling == "read"
    post = rule("facebook.post")
    refusal = egress.judge("https://www.facebook.com/api/graphql/", "POST", navigation=False, sites=(),
                           driver=driver, human=False, effect=post)
    assert refusal is not None and refusal.rule == "commit"


def test_a_grant_like_object_with_no_ceiling_at_all_is_still_unrestricted():
    assert verdicts._ceiling(SimpleNamespace(id="g")) is None
    assert verdicts._ceiling(SimpleNamespace(id="g", ceiling=None)) is None
    assert verdicts._ceiling(SimpleNamespace(id="g", ceiling="commit")) == "commit"
    assert verdicts._ceiling(SimpleNamespace(id="g", ceiling="not-an-effect")) == "read"


# ---- the commit gate: the account pin and a flow's identity check ----------------------------

class _Lanes:
    def get(self, lane_id):
        raise RuntimeError("the lane manager could not read its store")


async def test_the_gate_refuses_a_publish_when_the_lanes_pinned_account_cannot_be_read():
    authorized: list = []

    async def authorize(*args, **kwargs):  # never reached: nothing may be approved as an unknown account
        authorized.append(kwargs)

    runtime = SimpleNamespace(get=lambda name: _Lanes() if name == "lanes" else None)
    ctx = SimpleNamespace(runtime=runtime, authorize=authorize, grant=SimpleNamespace(id="g1"))
    gate = commitgate.ActGate(ctx, LANE, call={"tool": "browser_act"}, submit_like=True)
    url, headers, body = facebook_comment()
    found = egress.classify(url, "POST", headers, body=body)
    assert found is not None and found.effect == "commit"
    request = SimpleNamespace(post_data_buffer=body, headers=headers)
    refusal = await gate.hold(found, request, url, "POST")
    assert refusal is not None and refusal.rule == "commit_gate"
    assert isinstance(gate.outcome, Denied) and gate.outcome.reason == "account_unreadable"
    assert authorized == []
    with pytest.raises(Denied):
        gate.raise_outcome()


def test_a_flow_on_a_lane_the_manager_cannot_describe_is_checked_whatever_its_surfaces_say():
    """Its surfaces alone (here one on a site whose login posts only as itself) never prove the lane
    cannot post as another identity: the lane's own spec may list Facebook."""
    reddit = Surface(id="biz-a/reddit", site="reddit", lane=LANE, acts_as=ActsAs(kind="account", user_id="u1"))

    def get(name):
        if name == "lanes":
            return _Lanes()
        return SimpleNamespace(list=lambda: [reddit]) if name == "surfaces" else None

    check = commitgate.flow_identity_check(SimpleNamespace(get=get), LANE, SimpleNamespace(trace=None))
    assert check is not None


# ---- the act's commit meter: a trace that fails keeps the refusal ---------------------------

def test_a_commit_past_the_limit_stays_refused_when_its_trace_fails():
    def trace(*args, **kwargs):
        raise RuntimeError("the action log refused the row")

    ctx = SimpleNamespace(charge_commit=lambda lane, sent: "the day's cap of 5 posts is reached",
                          commit_code="limit_reached", commits_capped=lambda lane: False, trace=trace)
    meter = node_tools.ActMeter(ctx, LANE)
    post = rule("facebook.post")
    assert meter.charge(post, sent=False) == "the day's cap of 5 posts is reached"
    refusal = egress.LaneGuard(LANE)._charge(post, sent=False, meter=meter)
    assert refusal is not None and refusal.rule == "limit_reached"


# ---- the daemon: a required service that is not installed ----------------------------------

def test_a_required_service_that_is_not_installed_stops_the_node(monkeypatch):
    name = next(iter(daemon.REQUIRED_SERVICES))
    monkeypatch.setattr(daemon, "_installed", lambda module: None)
    runtime = Runtime()
    with pytest.raises(RuntimeError, match="refusing to start"):
        daemon.build_services(runtime, [name])


def test_an_optional_service_that_is_not_installed_is_still_skipped(monkeypatch):
    monkeypatch.setattr(daemon, "_installed", lambda module: None)
    runtime = Runtime()
    assert daemon.build_services(runtime, ["agentos.runner.service"]) == []


# ---- MCP redaction: an unreadable proxies file ----------------------------------------------

def test_an_unreadable_proxies_file_still_masks_the_logins_last_read(monkeypatch):
    from agentos.mcp import redact

    good = {"home": SimpleNamespace(password="s3cret-proxy-pass", username="proxyuser01")}
    monkeypatch.setattr(redact, "load_proxies", lambda config: good)
    redactor = Redactor(SimpleNamespace())
    assert "s3cret" not in redactor.text("upstream said: s3cret-proxy-pass")

    def broken(config):
        raise ValueError("proxies.toml: unexpected end of file")

    monkeypatch.setattr(redact, "load_proxies", broken)
    masked = redactor.text("upstream said: s3cret-proxy-pass for proxyuser01")
    assert "s3cret-proxy-pass" not in masked and "proxyuser01" not in masked


def test_publish_capture_of_the_test_shape_binds_whole():
    """The shape the gate test uses carries no media, so the pin is what the gate reaches first."""
    url, headers, body = facebook_comment()
    found = egress.classify(url, "POST", headers, body=body)
    assert found is not None
    cap = publish.capture(found, url, "POST", headers, body)
    assert not publish.unbound_media(cap.record)

