"""Activity and approvals say who decided and how, one way (QA-2 #8).

A deny with the bare node token was traced as a human owner (`principal_kind: human`), an approve with
the node's CLI code was traced `via: session`, and the owner showed under three names (`human:owner`,
`node:owner`, `owner:cli`). Now the owner is one name, the node token is "the node's token" (it proves
no person), and the CLI code is "on the node, in person"."""

from __future__ import annotations

import pytest
from test_api_members import owner, pending
from test_node_panel import TOKEN, Node, bearer

from agentos.api.actors import actor
from agentos.api.auth import PRESENCE_HEADER
from agentos.core.ratelimit import RateLimiter

LANE = "biz-a/instagram"


@pytest.fixture
def node(tmp_path):
    n = Node(tmp_path)
    n.api_app.state.api.limiter = RateLimiter(per_key=10_000, global_max=10_000)
    yield n
    n.close()


def test_each_way_of_deciding_reads_one_way_in_activity_and_on_the_approval(node):
    node.policy.ask_first.set("lane", LANE, True, by="owner", person=True)
    by_token, by_cli, in_panel = pending(node, LANE), pending(node, LANE), pending(node, LANE)
    with node.client() as c:
        assert c.post(f"/api/approvals/{by_token}/deny", json={}, headers=bearer(TOKEN)).status_code == 200
        node.store.add_code("cli-1", ttl_seconds=60)
        r = c.post(f"/api/approvals/{by_cli}/approve", json={}, headers={**bearer(TOKEN), PRESENCE_HEADER: "cli-1"})
        assert r.status_code == 200, r.text
    boss = owner(node)
    assert boss.post(f"/api/approvals/{in_panel}/approve").status_code == 200

    shown = {a["id"]: a for a in boss.get("/api/approvals?limit=10").json()}
    assert {shown[i]["decided_by"] for i in (by_token, by_cli, in_panel)} == {"owner"}
    assert (shown[by_token]["decided_who"], shown[by_token]["decided_how"]) == ("the node's token", None)
    assert (shown[by_cli]["decided_who"], shown[by_cli]["decided_how"]) == ("the owner", "on the node, in person")
    assert (shown[in_panel]["decided_who"], shown[in_panel]["decided_how"]) == ("the owner", "in the panel")

    events = {e["args"]["approval"]: e for e in boss.get("/api/activity?limit=50").json()
              if e.get("kind") == "panel" and (e.get("args") or {}).get("approval")}
    token_ev, cli_ev, panel_ev = events[by_token], events[by_cli], events[in_panel]
    assert token_ev["principal_kind"] == "node" and token_ev["args"]["via"] == "node_token"
    assert (token_ev["who"], token_ev["how"]) == ("the node's token", None)
    assert cli_ev["principal_kind"] == "human" and cli_ev["args"]["via"] == "cli"
    assert (cli_ev["who"], cli_ev["how"]) == ("the owner", "on the node, in person")
    assert panel_ev["args"]["via"] == "session" and (panel_ev["who"], panel_ev["how"]) == ("the owner", "in the panel")


@pytest.mark.parametrize(("by", "via", "want"), [
    ("human:owner", "session", ("the owner", "in the panel")),
    ("node:owner", "cli", ("the owner", "on the node, in person")),
    ("owner:cli", None, ("the owner", "on the node, in person")),
    ("node:owner", "owner_token", ("the node's token", None)),
    ("va", "passkey", ("va", "with a passkey")),
    ("agent:helper", "token", ("agent:helper", "with its grant's token")),
])
def test_one_wording_for_every_spelling(by, via, want):
    assert actor(by, via) == want
