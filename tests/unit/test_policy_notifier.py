"""Notifier: ntfy pushes with buttons or deep links, signed webhooks, the outbox, rate limits."""

from __future__ import annotations

import json
import stat

import httpx
import pytest
from test_policy_support import Clock, make_service

from agentos.core.errors import ApprovalPending, InvalidRequest
from agentos.core.models import Effect
from agentos.policy.errors import Conflict, Denied
from agentos.policy.notifier import LINK_TTL_S, generate_topic, weak_topic, webhook_signature

TOPIC = "agentos-test-k3Jx9QmZ2vR7tB5nW8yL"  # long and random, as an enabled ntfy channel needs


class FakeNet:
    def __init__(self):
        self.up = True
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not self.up:
            raise httpx.ConnectError("down", request=request)
        self.requests.append(request)
        return httpx.Response(200)

    @property
    def transport(self):
        return httpx.MockTransport(self.handler)

    def bodies(self):
        return [json.loads(r.content) for r in self.requests]


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    net = FakeNet()
    svc, _rt = make_service(tmp_path, clock=clock, transport=net.transport)
    svc.notifier.update_config({"ntfy": {"topic": TOPIC, "token": "tk_ntfy_secret_value"},
                                "webhook": {"url": "https://hooks.example/agentos",
                                            "secret": "whsec_0123456789abcdef"},
                                "panel_url": "http://localhost:7400"})
    yield svc, net, clock
    svc.close()


async def make_pending(svc, content="x"):
    _, tok = svc.issue(subject={"id": "agent"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    ctx = svc.context(svc.resolve(tok)[1], tool="lane_post", token=tok)
    with pytest.raises(ApprovalPending) as e:
        await ctx.authorize(Effect.COMMIT, lane_id="biz-a/insta", summary="Post a photo to instagram.com",
                            content=content)
    return e.value.approval_id, tok


def test_config_file_is_private_and_public_view_hides_secrets(env, tmp_path):
    svc, _, _ = env
    path = tmp_path / "secrets" / "notifier.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    pub = json.dumps(svc.notifier.config.public())
    assert "tk_ntfy_secret_value" not in pub and "whsec_" not in pub and '"has_token": true' in pub
    svc.notifier.update_config({"ntfy": {"topic": "renamed-" + TOPIC}})  # token kept when not resent
    assert svc.notifier.config.ntfy.token == "tk_ntfy_secret_value"


async def test_approval_push_with_deep_link_only(env):
    svc, net, _ = env
    ap, tok = await make_pending(svc, {"caption": "summer sale", "password": "hunter2"})
    await svc.notifier.flush()
    ntfy = [r for r in net.requests if "ntfy" in str(r.url)]
    assert len(ntfy) == 1 and ntfy[0].headers["authorization"] == "Bearer tk_ntfy_secret_value"
    body = json.loads(ntfy[0].content)
    assert body["topic"] == TOPIC and "Post a photo" in body["message"]
    assert body["click"] == f"http://localhost:7400/local/needs-you#{ap}"
    assert [a["action"] for a in body["actions"]] == ["view"]  # no public URL: no approve button
    raw = b"".join(r.content for r in net.requests)
    assert b"hunter2" not in raw and tok.encode() not in raw and b"summer sale" in raw


async def test_buttons_review_in_the_panel_or_deny_and_never_approve(env):
    svc, net, clock = env
    svc.notifier.update_config({"public_base_url": "https://agentos.example.com"})
    ap, _ = await make_pending(svc)
    await svc.notifier.flush()
    body = next(b for b in net.bodies() if "topic" in b)
    assert [a["label"] for a in body["actions"]] == ["Review", "Deny"]  # no Approve button (SEC-8)
    review, deny = body["actions"]
    assert review["action"] == "view" and review["url"].startswith("https://agentos.example.com/local/needs-you?")
    r = httpx.URL(review["url"]).params
    assert svc.approvals.check_review_link(ap, r["e"], r["s"]).id == ap  # opens it; decides nothing
    assert int(r["e"]) <= clock.t + LINK_TTL_S
    with pytest.raises(Denied):
        svc.approvals.decide_by_link(ap, "approve", r["e"], svc.approvals.sign_action(ap, "approve", int(r["e"])))
    with pytest.raises(InvalidRequest):
        svc.approvals.decide_by_link(ap, "deny", r["e"], r["s"])  # a review link cannot deny either
    assert svc.approvals.get(ap).state == "pending"
    q = httpx.URL(deny["url"]).params
    assert deny["method"] == "POST" and q["d"] == "deny"
    assert svc.approvals.decide_by_link(ap, q["d"], q["e"], q["s"]).state == "denied"
    # the links were built at delivery time and are not stored anywhere
    stored = json.dumps(svc.notifier.outbox(include_delivered=True))
    assert q["s"] not in stored and r["s"] not in stored
    clock.advance(LINK_TTL_S + 1)
    ap2, _ = await make_pending(svc, "other")
    old = int(clock.t - 1)
    with pytest.raises(Conflict):
        svc.approvals.check_review_link(ap2, old, svc.approvals.sign_action(ap2, "review", old))


async def test_a_code_request_links_to_its_card_and_carries_no_code(env):
    svc, net, _ = env
    svc.notifier.update_config({"public_base_url": "https://agentos.example.com"})
    svc.notifier.send({"type": "code.requested", "title": "Login code for instagram.com",
                       "body": "biz-a / instagram is at the code step on instagram.com.",
                       "code_request_id": "cr_01JC0DE", "lane": "biz-a/instagram", "site": "instagram.com",
                       "expires_ts": 1e12, "urgent": True})
    await svc.notifier.flush()
    body = next(b for b in net.bodies() if "topic" in b)
    assert body["click"] == "https://agentos.example.com/local/needs-you#cr_01JC0DE"
    assert body["priority"] == 5  # the login waits for it
    assert [a["label"] for a in body["actions"]] == ["Open"]  # no Review or Deny: it is not an approval
    hook = next(b for b in net.bodies() if "data" in b)
    assert hook["type"] == "code.requested" and hook["data"]["code_request_id"] == "cr_01JC0DE"


@pytest.mark.parametrize("topic", ["agentos", "my-phone", "a" * 40, "agentos-approvals-topic-for-my-phone"])
def test_guessable_topics_are_refused(env, topic):
    svc, _, _ = env
    assert weak_topic(topic) is not None
    with pytest.raises(ValueError):
        svc.notifier.update_config({"ntfy": {"topic": topic}})
    svc.notifier.update_config({"ntfy": {"topic": topic, "enabled": False}})  # stored off is fine


def test_a_topic_left_out_is_generated(env):
    svc, _, _ = env
    cfg = svc.notifier.update_config({"ntfy": {"topic": None, "server": "https://ntfy.tailnet.example"}})
    assert weak_topic(cfg.ntfy.topic) is None and cfg.ntfy.topic != TOPIC
    assert weak_topic(generate_topic()) is None


def test_a_saved_weak_topic_turns_ntfy_off_instead_of_failing(tmp_path):
    (tmp_path / "secrets").mkdir(parents=True)
    (tmp_path / "secrets" / "notifier.json").write_text(json.dumps({"ntfy": {"topic": "agentos"}}))
    svc, _ = make_service(tmp_path)
    try:
        assert svc.notifier.config.ntfy.enabled is False and "ntfy" not in svc.notifier.enabled_channels()
    finally:
        svc.close()


async def test_webhook_is_hmac_signed(env):
    svc, net, _ = env
    await make_pending(svc)
    await svc.notifier.flush()
    hook = next(r for r in net.requests if "hooks.example" in str(r.url))
    ts = hook.headers["x-agentos-timestamp"]
    assert hook.headers["x-agentos-signature"] == webhook_signature("whsec_0123456789abcdef", ts, hook.content)
    assert json.loads(hook.content)["type"] == "action.created"


async def test_outbox_survives_a_down_channel_and_a_restart(env, tmp_path):
    svc, net, clock = env
    net.up = False
    ap, _ = await make_pending(svc)
    await svc.notifier.flush()
    assert svc.approvals.get(ap).state == "pending"  # the approval is never lost
    rows = svc.notifier.outbox()
    assert {r["channel"] for r in rows} == {"ntfy", "webhook"}  # log delivered, the others wait
    failing = [r for r in rows if r["channel"] in ("ntfy", "webhook")]
    assert all(r["attempts"] == 1 and "unreachable" in r["last_error"] for r in failing)
    # restart the node: a new service, same home
    net2 = FakeNet()
    svc2, _ = make_service(tmp_path, clock=clock, transport=net2.transport)
    await svc2.notifier.flush()
    assert net2.requests == []  # still backing off
    clock.advance(10)
    await svc2.notifier.flush()
    assert len(net2.requests) == 2 and svc2.notifier.outbox() == []


async def test_dead_letter_after_max_attempts(env):
    svc, net, clock = env
    svc.notifier.update_config({"max_attempts": 2, "webhook": None})
    net.up = False
    await make_pending(svc)
    for _ in range(3):
        await svc.notifier.flush()
        clock.advance(1000)
    row = next(r for r in svc.notifier.outbox() if r["channel"] == "ntfy")
    assert row["dead"] and row["attempts"] == 2


async def test_ntfy_batches_and_rate_limits(env):
    svc, net, clock = env
    svc.notifier.update_config({"rate_per_min": 1, "webhook": None})
    for i in range(3):
        await make_pending(svc, f"c{i}")
    await svc.notifier.flush()
    pushes = [b for b in net.bodies() if "topic" in b]
    assert len(pushes) == 1 and pushes[0]["title"] == "3 things need you"
    await make_pending(svc, "c4")
    await svc.notifier.flush()
    assert len([b for b in net.bodies() if "topic" in b]) == 1  # rate limited, still queued
    clock.advance(61)
    await svc.notifier.flush()
    assert len([b for b in net.bodies() if "topic" in b]) == 2


async def test_send_never_raises(env):
    svc, _, _ = env
    svc.notifier.db.close()  # storage broken
    assert svc.notifier.send({"type": "x"}) == []


async def test_test_command(env):
    svc, net, _ = env
    res = await svc.notifier.test()
    assert res == {"ntfy": "delivered", "webhook": "delivered", "log": "delivered"}
    net.up = False
    res = await svc.notifier.test()
    assert res["ntfy"].startswith("failed") and res["log"] == "delivered"
