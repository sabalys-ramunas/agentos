"""`openajar grants explain` and grant_info's `explain`: a grant in plain sentences."""

from __future__ import annotations

import pytest
from test_policy_support import OWNER, make_service
from typer.testing import CliRunner

from openajar.core.config import NodeConfig, save_config
from openajar.policy.cli import app
from openajar.policy.explain import explain
from openajar.policy.grants import BrainCap, LaneCap, Specials
from openajar.policy.routing import RoutingTable, Rule

EM_DASH = "—"


@pytest.fixture
def svc(tmp_path):
    s, _ = make_service(tmp_path)
    yield s
    s.close()


def text(svc, grant_id, **kw) -> str:
    g = svc.grant_by_id(grant_id)
    lines = explain(g, routing=svc.cfg.routing, remaining=svc.remaining(g), now=svc.clock(), **kw)
    out = "\n".join(lines)
    assert EM_DASH not in out
    return out


def test_a_posting_agent_reads_like_what_it_may_do(svc):
    g, _ = svc.issue(subject={"id": "claude-code"}, workspaces=["biz-a"], ceiling="commit",
                     domains=["instagram.com"], budget={"usd": 5}, ttl_s=2 * 86400)
    out = text(svc, g.id)
    assert "An agent grant for claude-code, issued by owner." in out
    assert "It reaches every lane in biz-a." in out
    assert "It may look at pages, prepare private drafts and publish (posts" in out and "spend money" not in out
    assert "Reading runs on its own." in out
    assert ("Publishing runs on its own, unless Ask me first is on for the routine or account: then it waits for "
            "your approval unless a standing approval you gave covers it.") in out
    assert "Paying" not in out
    assert "In those lanes it may only use instagram.com." in out
    assert "outside instagram.com" in out
    assert "at most 5.00 USD in total (5.00 USD left)." in out
    assert "It expires at" in out and "(in 2 days)" in out


def test_specials_and_limits_are_named(svc):
    g, _ = svc.issue(subject={"id": "ops"}, lanes=["biz-a/instagram", "personal/*"], ceiling="read",
                     specials=Specials(secret=LaneCap(lanes=["biz-a/instagram"]),
                                       brain=BrainCap(tier="medium", quota_tokens=200_000)),
                     depth=0)
    out = text(svc, g.id)
    assert "It reaches biz-a/instagram and every lane in personal." in out
    assert "It may only look at pages: it cannot change anything (ceiling: read)." in out
    assert "Publishing" not in out and "Drafting" not in out
    assert "The vault may type passwords and codes for it in biz-a/instagram; it never sees them." in out
    assert "Typing a vault secret runs on its own." in out
    assert "up to the medium tier, at most 200,000 tokens a day." in out
    assert "It expires at" in out  # the node's default lifetime
    assert "It cannot hand a narrower copy of itself to anyone." in out


def test_a_narrowed_grant_says_where_it_came_from(svc):
    parent, token = svc.issue(subject={"id": "claude-code"}, workspaces=["biz-a"], ceiling="commit")
    child, _ = svc.attenuate(token, ceiling="draft", lanes=["biz-a/instagram"])
    out = text(svc, child.id)
    assert f"narrowed from {parent.id}, so it can never do more than that grant." in out
    assert "It reaches biz-a/instagram." in out
    assert "Publishing" not in out and "Drafting runs on its own" in out


def test_a_custom_routing_table_is_described_as_it_is(svc):
    g, _ = svc.issue(subject={"id": "claude-code"}, workspaces=["biz-a"], ceiling="commit")
    routing = RoutingTable(rules=[Rule(effect="read", then="auto"), Rule(effect="draft", then="deny"),
                                  Rule(effect="commit", then="auto")])
    out = "\n".join(explain(svc.grant_by_id(g.id), routing=routing, now=svc.clock()))
    assert "Drafting is refused." in out and "Publishing runs on its own." in out


def test_the_owner_token_is_described_without_a_lane_scope(svc):
    from openajar.policy.grants import Grant, owner_block

    out = "\n".join(explain(Grant([owner_block()], owner_token=True)))
    assert "owner token" in out and "It reaches" not in out
    assert "Paying waits for your approval, and only a person can approve it." in out
    assert "An irreversible step waits for your approval, and only a person can approve it." in out


def test_cli_explains_live_and_revoked_grants(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path))
    save_config(NodeConfig(home=tmp_path, mcp_token=OWNER))
    runner = CliRunner()
    issued = runner.invoke(app, ["grants", "issue", "--subject", "claude-code", "--workspace", "biz-a",
                                 "--ceiling", "commit", "--ttl", "2d"])
    assert issued.exit_code == 0, issued.output
    gid = issued.stdout.split()[1]
    token = issued.stdout.strip().splitlines()[-1]
    shown = runner.invoke(app, ["grants", "explain", gid])
    assert shown.exit_code == 0, shown.output
    assert "every lane in biz-a" in shown.stdout and token not in shown.stdout
    assert runner.invoke(app, ["grants", "revoke", gid]).exit_code == 0
    gone = runner.invoke(app, ["grants", "explain", gid])
    assert gone.exit_code == 0 and "is revoked, so it allows nothing now." in gone.stdout
    missing = runner.invoke(app, ["grants", "explain", "gr_nope12345"])
    assert missing.exit_code == 1
    assert EM_DASH not in shown.output + gone.output + missing.output
