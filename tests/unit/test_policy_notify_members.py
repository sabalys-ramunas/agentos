"""Notifier with members (B3): per-member channels, recipients, quiet hours, batching,
digests, urgency, removal and topic rotation, the lane-held push. No network: a fake transport."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from test_policy_notifier import TOPIC, FakeNet
from test_policy_notify_recipients import human
from test_policy_support import Clock, make_service

from openajar.core.errors import ApprovalPending
from openajar.core.models import ControlState, Effect, LaneControl
from openajar.policy.grants import Grant
from openajar.policy.notifier import generate_topic
from openajar.policy.recipients import could_control, could_decide

VILNIUS = ZoneInfo("Europe/Vilnius")


def at(s: str) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=VILNIUS).timestamp()


class Directory:
    """Members and their grants; the checks are the real ones from openajar.policy.recipients."""

    def __init__(self, members: dict[str, list[Grant]] | None = None):
        self.members = members or {}

    def member_ids(self) -> list[str]:
        return sorted(self.members)

    def can_decide(self, member_id, facts):
        return any(could_decide(g, facts) for g in self.members.get(member_id, []))

    def can_control(self, member_id, lane):
        return any(could_control(g, lane) for g in self.members.get(member_id, []))


VA_TOPIC = "openajar-va-Q8w2Lm4Zp7Rt1Xc9Vb3Nk6"
OP_TOPIC = "openajar-op-H5j8Kd2Sf6Gq9Wz3Yx7Pm1"


@pytest.fixture
def env(tmp_path):
    clock = Clock(at("2026-06-10 10:00"))
    net = FakeNet()
    svc, _rt = make_service(tmp_path, clock=clock, transport=net.transport)
    svc.notifier.update_config({"ntfy": {"topic": TOPIC}, "panel_url": "http://localhost:7400"})
    svc.notifier.directory = Directory({"m_va": [human("m_va", ["biz-c"], approve=["commit"])]})
    svc.notifier.update_member("m_va", {"topic": VA_TOPIC, "timezone": "Europe/Vilnius"})
    yield svc, net, clock
    svc.close()


async def make_pending(svc, lane="biz-c/insta", content="x", effect=Effect.COMMIT):
    _, tok = svc.issue(subject={"id": "agent"}, workspaces=[lane.split("/")[0]], ceiling=Effect.PAY)
    ctx = svc.context(svc.resolve(tok)[1], tool="lane_post", token=tok)
    with pytest.raises(ApprovalPending) as e:
        await ctx.authorize(effect, lane_id=lane, summary="Post the summer sale photo", content=content)
    return e.value.approval_id


def pushes(net, topic=None):
    out = [b for b in net.bodies() if "topic" in b]
    return [b for b in out if b["topic"] == topic] if topic else out


# ---- recipients ----
async def test_the_approver_of_the_workspace_is_pushed_and_the_owner_is_not(env):
    svc, net, _ = env
    ap = await make_pending(svc)
    await svc.notifier.flush()
    assert len(pushes(net, VA_TOPIC)) == 1 and pushes(net, TOPIC) == []
    push = pushes(net, VA_TOPIC)[0]
    assert push["title"] == "biz-c/insta: commit needs you" and push["message"] == "Open the panel to review it."
    assert push["click"].endswith(f"#{ap}")
    raw = b"".join(r.content for r in net.requests)
    assert b"summer sale" not in raw  # a member's push carries no content


async def test_other_workspaces_and_effects_go_to_the_owner(env):
    svc, net, _ = env
    await make_pending(svc, lane="biz-a/insta")
    await make_pending(svc, lane="biz-c/shop", content="y", effect=Effect.PAY)  # approve covers commit only
    await svc.notifier.flush()
    assert pushes(net, VA_TOPIC) == []
    owner = pushes(net, TOPIC)
    assert len(owner) == 1 and owner[0]["title"] == "2 things need you"
    assert "summer sale" in owner[0]["message"]  # the owner's pushes are as before


async def test_a_solo_node_pushes_the_owner_everything_at_once(tmp_path):
    clock = Clock(at("2026-06-10 03:00"))  # the middle of the night: the owner set no quiet hours
    net = FakeNet()
    svc, _ = make_service(tmp_path, clock=clock, transport=net.transport)
    try:
        svc.notifier.update_config({"ntfy": {"topic": TOPIC}})
        ap = await make_pending(svc)
        await svc.notifier.flush()
        assert [p["topic"] for p in pushes(net)] == [TOPIC] and "summer sale" in pushes(net)[0]["message"]
        assert svc.notifier.recipients({"type": "action.created", "approval_id": ap},
                                       svc.notifier._facts(ap)) == ["owner"]
    finally:
        svc.close()


async def test_a_member_without_a_channel_is_not_a_dead_end(env):
    svc, net, _ = env
    svc.notifier.update_member("m_va", {"channel": "none"})
    await make_pending(svc)
    await svc.notifier.flush()
    assert [p["topic"] for p in pushes(net)] == [TOPIC]


async def test_a_broken_directory_falls_back_to_the_owner(env):
    svc, net, _ = env

    class Broken:
        def member_ids(self):
            raise RuntimeError("panel.db is locked")

    svc.notifier.directory = Broken()
    await make_pending(svc)
    await svc.notifier.flush()
    assert [p["topic"] for p in pushes(net)] == [TOPIC]


# ---- quiet hours, batching, digests ----
async def test_quiet_hours_hold_until_one_morning_card(env):
    svc, net, clock = env
    clock.t = at("2026-06-10 22:00")
    for i in range(3):
        await make_pending(svc, content=f"c{i}")
        clock.advance(600)
    await svc.notifier.flush()
    assert pushes(net) == []
    held = svc.notifier.outbox(member="m_va")
    assert len(held) == 3 and {r["held"] for r in held} == {"quiet"}
    clock.t = at("2026-06-11 07:59")
    await svc.notifier.flush()
    assert pushes(net) == []
    clock.t = at("2026-06-11 08:00")
    await svc.notifier.flush()
    card = pushes(net, VA_TOPIC)
    assert len(card) == 1 and card[0]["title"] == "3 things need you"
    assert card[0]["message"].count("biz-c/insta: commit needs you") == 3 and "c1" not in card[0]["message"]


async def test_batching_collapses_what_follows_the_first_push(env):
    svc, net, clock = env
    await make_pending(svc, content="first")
    await svc.notifier.flush()
    assert len(pushes(net, VA_TOPIC)) == 1  # the first goes at once
    for i in range(4):
        clock.advance(60)
        await make_pending(svc, content=f"n{i}")
        await svc.notifier.flush()
    assert len(pushes(net, VA_TOPIC)) == 1  # the rest wait for the window
    clock.t = at("2026-06-10 10:10")
    await svc.notifier.flush()
    assert len(pushes(net, VA_TOPIC)) == 2 and pushes(net, VA_TOPIC)[1]["title"] == "4 things need you"


async def test_digest_times_collect_approvals_into_one_card(env):
    svc, net, clock = env
    svc.notifier.update_member("m_va", {"digest_at": ["13:00", "18:00"]})
    await make_pending(svc, content="a")
    clock.t = at("2026-06-10 12:30")
    await make_pending(svc, content="b")
    await svc.notifier.flush()
    assert pushes(net) == []
    clock.t = at("2026-06-10 13:00")
    await svc.notifier.flush()
    assert [p["title"] for p in pushes(net, VA_TOPIC)] == ["2 things need you"]


async def test_decided_meanwhile_is_dropped_and_expired_is_listed_as_missed(env):
    svc, net, clock = env
    clock.t = at("2026-06-10 23:00")
    decided = await make_pending(svc, content="a")
    expired = await make_pending(svc, content="b")
    svc.approvals.decide(decided, "deny", by="owner")
    svc.db.execute("UPDATE approvals SET expires_at = ? WHERE id = ?", (at("2026-06-11 06:00"), expired))
    clock.t = at("2026-06-11 08:00")
    svc.approvals.sweep()
    await svc.notifier.flush()
    card = pushes(net, VA_TOPIC)
    assert len(card) == 1 and card[0]["title"].startswith("Missed: ")
    assert [a["label"] for a in card[0]["actions"]] == ["Open"]  # nothing left to review or deny
    dropped = [r for r in svc.notifier.outbox(include_delivered=True, member="m_va") if r["dropped"]]
    assert [r["event"]["approval_id"] for r in dropped] == [decided]


async def test_the_owner_can_set_quiet_hours_too(env):
    svc, net, clock = env
    svc.notifier.update_member("owner", {"timezone": "Europe/Vilnius", "quiet_hours": ["23:00", "07:00"]})
    clock.t = at("2026-06-11 02:00")
    await make_pending(svc, lane="biz-a/insta")
    await svc.notifier.flush()
    assert pushes(net) == []
    clock.t = at("2026-06-11 07:00")
    await svc.notifier.flush()
    assert len(pushes(net, TOPIC)) == 1


# ---- urgency ----
async def announce_with_not_after(svc, not_after: float) -> str:
    """B1 adds approvals.not_after; until then the test adds the column, as its migration will."""
    cols = {r[1] for r in svc.db.all("PRAGMA table_info(approvals)")}
    if "not_after" not in cols:
        svc.db.execute("ALTER TABLE approvals ADD COLUMN not_after REAL")
    ap = await make_pending(svc, content=f"stale-{not_after}")
    svc.db.execute("DELETE FROM outbox")
    svc.db.execute("UPDATE approvals SET not_after = ? WHERE id = ?", (not_after, ap))
    svc.notifier.send({"type": "action.created", "approval_id": ap, "title": "t", "body": "b", "actionable": True})
    return ap


async def test_stale_before_the_morning_wakes_whoever_may_be_woken(env):
    svc, net, clock = env
    clock.t = at("2026-06-10 22:00")
    await announce_with_not_after(svc, at("2026-06-11 06:00"))
    await svc.notifier.flush()
    # the VA keeps quiet hours (urgent_breaks_quiet off), so the owner, who is awake, is asked now
    assert pushes(net, VA_TOPIC) == []
    owner = pushes(net, TOPIC)
    assert len(owner) == 1 and owner[0]["priority"] == 5 and owner[0]["title"].startswith("Urgent: ")
    assert [r["held"] for r in svc.notifier.outbox(member="m_va")] == ["quiet"]


async def test_urgent_breaks_quiet_when_the_member_allows_it(env):
    svc, net, clock = env
    svc.notifier.update_member("m_va", {"urgent_breaks_quiet": True})
    clock.t = at("2026-06-10 22:00")
    await announce_with_not_after(svc, at("2026-06-11 06:00"))
    await svc.notifier.flush()
    va = pushes(net, VA_TOPIC)
    assert len(va) == 1 and va[0]["priority"] == 5 and pushes(net, TOPIC) == []


async def test_not_urgent_when_it_keeps_until_the_morning(env):
    svc, net, clock = env
    clock.t = at("2026-06-10 22:00")
    await announce_with_not_after(svc, at("2026-06-11 12:00"))
    await svc.notifier.flush()
    assert pushes(net) == []


# ---- removal and topics ----
async def test_a_removed_member_gets_nothing_more(env):
    svc, net, clock = env
    clock.t = at("2026-06-10 22:00")
    await make_pending(svc)
    assert svc.notifier.remove_member("m_va") == 1
    clock.t = at("2026-06-11 08:00")
    await svc.notifier.flush()
    assert pushes(net, VA_TOPIC) == []
    # their grant is still live in this directory, but they have no channel: the owner is asked
    await make_pending(svc, content="after")
    await svc.notifier.flush()
    assert pushes(net, VA_TOPIC) == [] and len(pushes(net, TOPIC)) == 1
    with pytest.raises(ValueError, match="retired"):
        svc.notifier.update_member("m_va", {"topic": VA_TOPIC})
    with pytest.raises(ValueError, match="retired"):
        svc.notifier.update_config({"ntfy": {"topic": VA_TOPIC}})


async def test_rows_for_a_member_whose_settings_vanished_are_dropped(env):
    svc, net, clock = env
    clock.t = at("2026-06-10 22:00")
    await make_pending(svc)
    cfg = svc.notifier.config.model_copy(deep=True)
    cfg.members.pop("m_va")
    svc.notifier.save_config(cfg)  # removed by hand, or by another process
    clock.t = at("2026-06-11 08:00")
    await svc.notifier.flush()
    assert pushes(net) == [] and svc.notifier.outbox(member="m_va") == []


def test_member_settings_generate_a_topic_and_hide_it_from_others(env):
    svc, _, _ = env
    s = svc.notifier.update_member("m_new", {})
    assert s["channel"] == "ntfy" and s["topic"].startswith("openajar-") and s["quiet_hours"] == ["21:00", "08:00"]
    assert s["urgent_breaks_quiet"] is False and s["batch_window_s"] == 600
    pub = json.dumps(svc.notifier.config.public())
    assert s["topic"] not in pub and VA_TOPIC not in pub and s["topic"][-4:] in pub
    assert svc.notifier.member_settings("m_new")["topic"] == s["topic"]
    assert svc.notifier.member_settings("m_unknown")["channel"] == "none"
    with pytest.raises(ValueError):
        svc.notifier.update_member("m_new", {"topic": "my-phone"})
    with pytest.raises(ValueError):
        svc.notifier.update_member("m_new", {"timezone": "Mars/Base"})
    for bad in ("owner/../x", "", "node"):
        with pytest.raises(ValueError):
            svc.notifier.update_member(bad, {})


def test_rotating_a_topic_retires_the_old_one(env):
    svc, _, _ = env
    new = svc.notifier.rotate_topic("m_va")
    assert new != VA_TOPIC and svc.notifier.member_settings("m_va")["topic"] == new
    with pytest.raises(ValueError, match="retired"):
        svc.notifier.update_member("m_va", {"topic": VA_TOPIC})
    owner_new = svc.notifier.rotate_topic("owner")
    assert owner_new != TOPIC and svc.notifier.config.ntfy.topic == owner_new
    with pytest.raises(ValueError, match="retired"):
        svc.notifier.update_config({"ntfy": {"topic": TOPIC}})
    svc.notifier.update_member("m_va", {"topic": generate_topic()})  # a fresh one is fine


async def test_a_member_webhook_is_signed_with_their_secret_and_digests_are_one_post(env):
    svc, net, clock = env
    svc.notifier.update_member("m_va", {"channel": "webhook", "webhook": {"url": "https://va.example/hook",
                                                                          "secret": "whsec_va_0123456789"}})
    assert "whsec_va" not in json.dumps(svc.notifier.member_settings("m_va"))
    svc.notifier.update_member("m_va", {"webhook": {"url": "https://va.example/hook2"}})  # secret kept
    clock.t = at("2026-06-10 22:00")
    await make_pending(svc, content="a")
    await make_pending(svc, content="b")
    clock.t = at("2026-06-11 08:00")
    await svc.notifier.flush()
    hooks = [r for r in net.requests if r.url.host == "va.example"]
    assert len(hooks) == 1 and hooks[0].url.path == "/hook2"
    body = json.loads(hooks[0].content)
    assert body["type"] == "digest" and len(body["data"]["events"]) == 2
    assert b"summer sale" not in hooks[0].content


async def test_a_member_on_the_nodes_own_ntfy_server_uses_its_token(env):
    svc, net, _ = env
    svc.notifier.update_config({"ntfy": {"server": "https://ntfy.tailnet.example", "token": "tk_node_0123"}})
    await make_pending(svc)
    await svc.notifier.flush()
    req = next(r for r in net.requests if json.loads(r.content).get("topic") == VA_TOPIC)
    assert req.url.host == "ntfy.tailnet.example" and req.headers["authorization"] == "Bearer tk_node_0123"
    svc.notifier.update_member("m_va", {"server": "https://ntfy.va-home.example"})
    await make_pending(svc, content="z")
    clock_t = svc.notifier.clock()
    svc.notifier.clock = lambda: clock_t + 601
    await svc.notifier.flush()
    other = [r for r in net.requests if r.url.host == "ntfy.va-home.example"]
    assert len(other) == 1 and "authorization" not in other[0].headers  # the node's token stays home


async def test_a_member_test_push(env):
    svc, net, _ = env
    assert await svc.notifier.test("m_va") == {"ntfy": "delivered", "log": "delivered"}
    assert [p["topic"] for p in pushes(net)] == [VA_TOPIC]


# ---- a lane is held: one push ----
def control(state=ControlState.AGENT, reason=None, until=None) -> LaneControl:
    return LaneControl(state=state, reason=reason, hold_until=until)


async def test_one_push_when_a_lane_is_held(env):
    svc, net, _ = env
    n = svc.notifier
    held = control(ControlState.PAUSED, "needs_human:captcha")
    assert n.on_lane_control("biz-a/insta", control(), held)
    assert n.on_lane_control("biz-a/insta", held, held.model_copy(update={"by": "someone"})) == []  # same reason
    assert n.on_lane_control("biz-a/insta", held, control()) == []  # cleared
    await n.flush()
    owner = pushes(net, TOPIC)
    assert len(owner) == 1 and owner[0]["title"] == "biz-a/insta is held"
    assert "needs_human:captcha" in owner[0]["message"] and "—" not in json.dumps(owner)
    assert n.on_lane_control("biz-a/insta", held, control(ControlState.PAUSED, "wrong_account"))  # a new reason
    assert n.on_lane_control("biz-a/insta", control(), control(ControlState.PAUSED, "rate_limited", until=None))


async def test_a_held_lane_goes_to_the_operators_who_can_take_it_over(env):
    svc, net, _ = env
    svc.notifier.directory = Directory({
        "m_op": [human("m_op", ["biz-c"], control=True)],
        "m_watch": [human("m_watch", ["biz-c"], control=True, watch_only=True, gid="gr_mwatch0001")]})
    svc.notifier.update_member("m_op", {"topic": OP_TOPIC, "timezone": "Europe/Vilnius"})
    svc.notifier.update_member("m_watch", {"timezone": "Europe/Vilnius"})
    svc.notifier.on_lane_control("biz-c/insta", control(), control(ControlState.PAUSED, "needs_human:checkpoint"))
    await svc.notifier.flush()
    assert [p["topic"] for p in pushes(net)] == [OP_TOPIC]
    assert pushes(net)[0]["message"].startswith("Agents are paused on biz-c/insta: needs_human:checkpoint.")


async def test_the_listener_registers_once_with_the_lanes_service(env):
    svc, _, _ = env
    calls = []
    lanes = SimpleNamespace(add_control_listener=calls.append)
    assert svc.notifier.attach(SimpleNamespace(get=lambda name: lanes if name == "lanes" else None))
    assert svc.notifier.listen_for_lane_control(lanes)
    assert calls == [svc.notifier.on_lane_control]
    assert not svc.notifier.attach(SimpleNamespace(get=lambda name: None))


async def test_notify_is_what_the_runner_and_brains_call(env):
    svc, net, _ = env
    svc.notifier.notify(title="A run needs your decision", body="Run r1 stopped at step 3.", level="warning",
                        data={"run_id": "r1", "secret": "x"})
    svc.notifier.notify("Brain finished", "done", kind="brain.done", urgent=True)
    await svc.notifier.flush()
    titles = [p["title"] for p in pushes(net, TOPIC)]
    assert titles == ["2 things need you"]
    rows = svc.notifier.outbox(include_delivered=True, member="owner")
    events = {r["event"]["type"]: r["event"] for r in rows}
    assert events["notice"]["run_id"] == "r1" and "secret" not in events["notice"]
    assert events["brain.done"]["urgent"] is True


def test_no_em_dash_in_any_push_text():
    from pathlib import Path

    import openajar.policy.notifier as mod
    import openajar.policy.recipients as rec
    for m in (mod, rec):
        src = Path(m.__file__).read_text()
        strings = [line for line in src.splitlines() if "—" in line and not line.strip().startswith("#")]
        assert strings == []


def test_link_buttons_for_members_never_approve(env):
    svc, _, _ = env
    svc.notifier.update_config({"public_base_url": "https://openajar.example.com"})
    ev = {"approval_id": "ap_1", "actionable": True, "title": "t", "body": "b", "expires_ts": 2e9}
    payload = svc.notifier.ntfy_payload([ev], member="m_va")
    assert payload["topic"] == VA_TOPIC and [a["label"] for a in payload["actions"]] == ["Review", "Deny"]
    assert "approve" not in json.dumps(payload["actions"]).replace("approval=", "")
    assert httpx.URL(payload["actions"][1]["url"]).params["d"] == "deny"


async def test_rows_on_a_channel_the_member_left_are_dropped(env):
    svc, net, clock = env
    clock.t = at("2026-06-10 22:00")
    await make_pending(svc)
    svc.notifier.update_member("m_va", {"channel": "none"})
    clock.t = at("2026-06-11 08:00")
    await svc.notifier.flush()
    rows = svc.notifier.outbox(include_delivered=True, member="m_va")
    assert pushes(net) == [] and [r["dropped"] for r in rows] == ["channel changed"]


async def test_an_outbox_from_before_members_is_migrated_and_delivered(tmp_path):
    from openajar.policy.store import DB
    old = DB(tmp_path / "policy.db", """CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel TEXT NOT NULL, event TEXT NOT NULL, created_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL NOT NULL, last_error TEXT, delivered_at REAL, dead_at REAL);""")
    for ch in ("ntfy", "log"):
        old.execute("INSERT INTO outbox (channel, event, created_at, next_attempt_at) VALUES (?, ?, 1, 1)",
                    (ch, json.dumps({"type": "health.proxy", "title": "Proxy down", "body": "p1"})))
    old.close()
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "notifier.json").write_text(json.dumps({"ntfy": {"topic": TOPIC}}))  # pre-member config
    net = FakeNet()
    svc, _ = make_service(tmp_path, clock=Clock(at("2026-06-10 03:00")), transport=net.transport)
    try:
        assert svc.notifier.config.owner.quiet_hours is None and svc.notifier.config.members == {}
        out = await svc.notifier.flush()
        assert out["ntfy"] == 1 and out["log"] == 1 and pushes(net)[0]["title"] == "Proxy down"
    finally:
        svc.close()
