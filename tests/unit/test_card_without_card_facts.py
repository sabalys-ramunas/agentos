"""A publish card made without the node's card facts says so, and the runtimes that make approvals
carry those facts.

Approval cards read what only the node knows (the effect rules, who a body acts as, the sites) from
the runtime service `card_facts` (agentos.core.cardfacts), which agentos.node.daemon.node_runtime
registers. A card made without it, by a hand-built `Runtime()` for example, cannot run those
checks. It must not look like a full card: it carries a warning, and denying it is marked safe.
"""

from __future__ import annotations

import pytest

from agentos.core.cardfacts import SERVICE as CARD_FACTS
from agentos.core.config import NodeConfig
from agentos.core.runtime import Runtime
from agentos.node import egress
from agentos.node.cardfacts import NodeCardFacts
from agentos.node.daemon import node_runtime
from agentos.policy import cards

RULE = egress.EFFECT_RULES[0].id
SITE = RULE.split(".", 1)[0]


def _unrecognized() -> dict:
    return {"publish": RULE, "lane": "biz-a/x", "record": {
        "kind": "post", "text": "hi",
        "acts_as": {"status": "unrecognized", "id": "123", "field": "actor", "why": "no surface names it",
                    "through": "biz-a/y"}}}


def _unknown_request() -> dict:
    return {"publish": f"{SITE}.unknown", "lane": "biz-a/x", "record": {
        "kind": egress.UNKNOWN, "fields": {"q": "x"}, "request": {"method": "PUT", "path": "/p"},
        "carries_typed": "text"}}


@pytest.mark.parametrize("content", [_unrecognized, _unknown_request], ids=["unknown-identity", "unknown-request"])
@pytest.mark.parametrize("runtime", [None, Runtime()], ids=["no-runtime", "bare-runtime"])
def test_a_card_without_the_node_facts_warns_and_is_safe_to_deny(runtime, content, caplog):
    card = cards.build_card("browser_act", "biz-a/x", content(), runtime=runtime)
    assert card is not None
    assert card["facts_missing"] is True
    assert card["deny_safe"] is True
    assert card["identity_warning"] == cards.NO_FACTS_WARNING
    assert "without the node's card facts" in caplog.text


def test_with_the_node_facts_the_card_has_its_full_checks():
    runtime = Runtime(card_facts=NodeCardFacts())
    unknown_identity = cards.build_card("browser_act", "biz-a/x", _unrecognized(), runtime=runtime)
    assert "identity agentos does not know" in (unknown_identity.get("identity_warning") or "")
    assert "facts_missing" not in unknown_identity
    unknown_request = cards.build_card("browser_act", "biz-a/x", _unknown_request(), runtime=runtime)
    assert unknown_request["deny_safe"] is True
    assert "does not recognise" in unknown_request["headline"]
    assert "facts_missing" not in unknown_request


def test_the_node_runtime_serves_the_card_facts(tmp_path):
    """The daemon and the dev node build their runtime here, and the policy service makes every
    approval card with that runtime."""
    runtime = node_runtime(NodeConfig(home=tmp_path), lanes=object(), interaction=object())
    assert isinstance(runtime.get(CARD_FACTS), NodeCardFacts)
