"""The egress package split: a patched rule list is read where the
guard reads it. Fake requests and contexts (test_egress)."""

from __future__ import annotations

from test_egress import FakeRequest, make_guard, settle

from agentos.node import egress
from agentos.node.egress import Driver, EffectRule, classify

LIKE = EffectRule("test.like", "test", "www.instagram.com", r"/split-test/like$", effect="write")
URL = "https://www.instagram.com/split-test/like"
READER = Driver(kind="agent", id="r", ceiling="read")


def test_patching_the_rules_module_changes_what_classify_reads(monkeypatch):
    assert classify(URL, "POST") is None
    monkeypatch.setattr(egress.rules, "EFFECT_RULES", (LIKE,))
    assert classify(URL, "POST") is LIKE
    assert egress.effects_of(URL, "POST")[0] is LIKE
    assert egress.rule_hosts() == ("www.instagram.com",)


async def test_patching_the_rules_module_changes_the_guards_verdict(tmp_path, monkeypatch):
    guard, ctx, _, _ = make_guard(tmp_path, driver=READER)
    ctx.emit("request", FakeRequest(URL, "POST"))
    await settle()
    assert guard.blocks_since(0) == []  # no rule names it: nothing to refuse

    monkeypatch.setattr(egress.rules, "EFFECT_RULES", (LIKE,))
    guard, ctx, _, _ = make_guard(tmp_path, driver=READER)
    assert guard.rules == (LIKE,)
    ctx.emit("request", FakeRequest(URL, "POST"))
    await settle()
    assert [(b.rule, b.effect_rule) for b in guard.blocks_since(0)] == [("write", "test.like")]
