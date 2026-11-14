"""Approve from your phone over Telegram (agentos.policy.telegram, docs/CONFIRMATIONS.md, Telegram).

Telegram is never called: a fake Bot API answers through httpx.MockTransport. Most of these are what a
tap must not be able to do: approve what chat cannot, come from someone else, count twice, count for
content that changed, or count while the method is off."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from test_policy_support import T0, Clock, make_service
from typer.testing import CliRunner

from agentos.core.models import Principal
from agentos.policy import telegram as tg
from agentos.policy.confirm import ConfirmLevel
from agentos.policy.notifier import TelegramConfig
from agentos.policy.telegram import BotApi, TelegramError

TOKEN = "123456789:AAH-test-token-abcdefghijklmnopqrstuv"
CHAT = 4242
USER = 4242  # a private chat's id is its account's id
LANE = "biz-a/instagram"
CONTENT = "Spring menu is out"
ROOT = "g_root1"


def ok(result):
    return httpx.Response(200, json={"ok": True, "result": result})


class FakeTelegram:
    """The Bot API as far as the node uses it. Records every call; `fail` makes the next calls fail."""

    def __init__(self):
        self.calls: list[tuple[str, dict, str]] = []
        self.updates: list[dict] = []
        self.next_id = 100
        self.fail: str | None = None  # "connect", "500"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[1]
        body = json.loads(request.content or b"{}")
        self.calls.append((method, body, str(request.url)))
        if self.fail == "connect":
            raise httpx.ConnectError(f"could not reach {request.url}", request=request)
        if self.fail == "readtimeout" and method == "sendMessage":  # it went out; the answer did not come back
            raise httpx.ReadTimeout(f"no answer from {request.url}", request=request)
        if self.fail == "409" and method == "getUpdates":
            return httpx.Response(409, json={"ok": False, "error_code": 409, "description": "Conflict: can't use "
                                             "getUpdates method while webhook is active; use deleteWebhook to "
                                             "delete the webhook first"})
        if self.fail == "500":
            return httpx.Response(500, json={"ok": False, "error_code": 500,
                                             "description": f"Internal error for {request.url}"})
        if method == "getMe":
            return ok({"id": 1, "is_bot": True, "username": "agentos_test_bot"})
        if method == "sendMessage":
            self.next_id += 1
            return ok({"message_id": self.next_id, "chat": {"id": body["chat_id"]}})
        if method == "getUpdates":
            out, self.updates = self.updates, []
            return ok(out)
        return ok(True)

    def sent(self, method: str) -> list[dict]:
        return [body for m, body, _ in self.calls if m == method]


@pytest.fixture
def fake():
    return FakeTelegram()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def svc(tmp_path, fake, clock):
    s, _ = make_service(tmp_path, clock=clock, transport=httpx.MockTransport(fake))
    yield s
    s.close()


@pytest.fixture
def paired(svc):
    svc.notifier.save_telegram(TelegramConfig(token=TOKEN, bot_username="agentos_test_bot", chat_id=CHAT,
                                              user_id=USER, username="the_owner", paired_at=T0))
    return svc


def ask(svc, *, effect="commit", owner="owner", follows=None, floor=None, preview=CONTENT, digest="sha256:" + "a" * 64,
        **kw):
    return svc.approvals.create_action(
        effect=effect, tool="lane_post", lane=LANE, domain="instagram.com", summary="Post a thing",
        content_hash="h:" + digest[-8:], content_digest=digest, preview=preview,
        requester=Principal(id="writer", owner=owner), grant_id=f"{ROOT}x", grant_path=f"/{ROOT}/{ROOT}x/",
        reason="a person reads a post before it goes out", follows=follows, floor=floor, **kw)


def question(svc):
    return svc.approvals.create_question(question="Which photo?", options=["A", "B"],
                                         requester=Principal(id="writer"), grant_id=f"{ROOT}x",
                                         grant_path=f"/{ROOT}/{ROOT}x/")


async def send(svc, fake) -> dict:
    """Deliver what waits and return the last message the bot sent."""
    await svc.notifier.flush()
    return fake.sent("sendMessage")[-1]


def buttons(message: dict) -> dict[str, str]:
    rows = (message.get("reply_markup") or {}).get("inline_keyboard") or []
    return {b["text"]: b.get("callback_data") or b.get("url") for row in rows for b in row}


def message_id(fake, message: dict) -> int:
    index = fake.sent("sendMessage").index(message)
    return 101 + index


_update = iter(range(1, 10_000))


def tap(data, mid, *, user=USER, chat=CHAT, chat_type="private"):
    return {"update_id": next(_update), "callback_query": {
        "id": f"q{data}", "data": data, "from": {"id": user, "is_bot": False, "username": "x"},
        "message": {"message_id": mid, "chat": {"id": chat, "type": chat_type}}}}


def start(text, *, user=USER, chat=None, chat_type="private", username="the_owner"):
    return {"update_id": next(_update), "message": {
        "message_id": 1, "text": text, "from": {"id": user, "is_bot": False, "username": username},
        "chat": {"id": user if chat is None else chat, "type": chat_type}}}


def answers(fake) -> list[str]:
    return [b["text"] for b in fake.sent("answerCallbackQuery")]


def traces(svc) -> list[dict]:
    svc.traces.flush()
    return [dict(r) for r in svc.traces.db.all("SELECT * FROM traces")]


# ---- pairing ----

async def test_pairing_takes_the_right_code_once_only_in_a_private_chat_and_it_expires(svc, fake, clock):
    assert await svc.telegram.set_token(TOKEN) == "agentos_test_bot"
    assert not svc.telegram.paired() and not svc.telegram.should_poll()
    code = svc.telegram.start_pairing()
    assert svc.telegram.pair_link(code) == f"https://t.me/agentos_test_bot?start={code}"
    assert svc.telegram.should_poll()
    for wrong in (start("/start not-the-code"), start(f"/start {code}", chat=-100, chat_type="group"),
                  start(f"/start {code}", chat=999), start("/start")):
        await svc.telegram.handle_update(wrong)
        assert not svc.telegram.paired()
    await svc.telegram.handle_update(start(f"/start {code}"))
    cfg = svc.telegram.config()
    assert (cfg.chat_id, cfg.user_id, cfg.username) == (USER, USER, "the_owner")
    assert "now paired" in fake.sent("sendMessage")[-1]["text"]
    await svc.telegram.handle_update(start(f"/start {code}", user=777))  # used: never again, for anyone
    assert svc.telegram.config().user_id == USER
    later = svc.telegram.start_pairing()
    clock.advance(tg.PAIR_TTL_S + 1)
    await svc.telegram.handle_update(start(f"/start {later}", user=888))
    assert svc.telegram.config().user_id == USER
    stored = svc.db.all("SELECT * FROM telegram_pairing")
    assert all(later not in json.dumps(dict(r)) for r in stored)  # only a hash of the code is kept


async def test_a_new_pairing_tells_the_account_it_replaces(paired, fake):
    code = paired.telegram.start_pairing()
    await paired.telegram.handle_update(start(f"/start {code}", user=5555))
    assert paired.telegram.config().user_id == 5555
    old = [m for m in fake.sent("sendMessage") if m["chat_id"] == CHAT]
    assert old and "another Telegram account" in old[-1]["text"]


async def test_a_token_that_is_not_one_is_refused_and_a_new_token_starts_unpaired(paired):
    with pytest.raises(ValueError):
        await paired.telegram.set_token("not a token")
    await paired.telegram.set_token(TOKEN)
    assert not paired.telegram.paired()


# ---- the message ----

async def test_a_chat_level_action_gets_approve_deny_and_its_whole_content_as_plain_text(paired, fake):
    paired.notifier.update_config({"public_base_url": "https://agentos.example.com"})
    ap = ask(paired)
    assert "telegram" in ap.methods
    msg = await send(paired, fake)
    assert msg["chat_id"] == CHAT and "parse_mode" not in msg
    assert "| Spring menu is out" in msg["text"] and "writer asks to commit on biz-a/instagram" in msg["text"]
    assert "Why it asks: a person reads a post before it goes out" in msg["text"] and "Expires:" in msg["text"]
    b = buttons(msg)
    assert set(b) == {"Approve", "Deny", "Review in the panel"}
    assert b["Review in the panel"].startswith("https://agentos.example.com/local/needs-you?approval=")
    assert all(len(b[k]) <= 64 and ap.id not in b[k] for k in ("Approve", "Deny"))  # an opaque id, nothing else


async def test_without_a_public_url_there_is_no_review_button(paired, fake):
    ask(paired)
    assert set(buttons(await send(paired, fake))) == {"Approve", "Deny"}


async def test_content_too_long_for_one_message_gets_no_approve(paired, fake, monkeypatch):
    monkeypatch.setattr(tg, "MAX_TEXT", 400)
    ask(paired, preview="x" * 600)
    msg = await send(paired, fake)
    assert set(buttons(msg)) == {"Deny"} and "too long" in msg["text"] and len(msg["text"]) <= 400


async def test_a_question_gets_a_review_link_and_no_answer_buttons(paired, fake):
    paired.notifier.update_config({"public_base_url": "https://agentos.example.com"})
    question(paired)
    msg = await send(paired, fake)
    assert set(buttons(msg)) == {"Review in the panel"} and "| Which photo?" in msg["text"]


# ---- who may tap ----

async def test_approve_after_a_second_is_recorded_as_telegram_at_chat_and_the_message_shows_it(paired, fake, clock):
    ap = ask(paired)
    msg = await send(paired, fake)
    mid = message_id(fake, msg)
    clock.advance(0.5)  # faster than a person reads: not counted, and the button still works
    await paired.telegram.handle_update(tap(buttons(msg)["Approve"], mid))
    assert paired.approvals.get(ap.id).state == "pending" and "faster than a person reads" in answers(fake)[-1]
    clock.advance(2)
    await paired.telegram.handle_update(tap(buttons(msg)["Approve"], mid))
    a = paired.approvals.get(ap.id)
    assert (a.state, a.decided_method, a.decided_level, a.decided_via) == ("approved", "telegram", "chat", "telegram")
    assert a.decided_by == "telegram:owner" and a.decided_detail == {"account": "@the_owner"}
    assert answers(fake)[-1] == "Approved."
    edit = fake.sent("editMessageText")[-1]
    assert edit["message_id"] == mid and edit["text"].startswith("Approved from Telegram.") and "reply_markup" not in edit


async def test_deny_decides_and_a_second_tap_on_the_same_message_counts_for_nothing(paired, fake):
    ap = ask(paired)
    msg = await send(paired, fake)
    mid = message_id(fake, msg)
    await paired.telegram.handle_update(tap(buttons(msg)["Deny"], mid))
    assert paired.approvals.get(ap.id).state == "denied"
    await paired.telegram.handle_update(tap(buttons(msg)["Approve"], mid))
    assert paired.approvals.get(ap.id).state == "denied" and answers(fake)[-1] == "This message was already answered."


@pytest.mark.parametrize("who", [{"user": 999}, {"chat": 999}, {"chat_type": "group"}])
async def test_a_tap_from_another_account_or_chat_is_ignored_and_logged(paired, fake, clock, who, caplog):
    ap = ask(paired)
    msg = await send(paired, fake)
    clock.advance(5)
    await paired.telegram.handle_update(tap(buttons(msg)["Approve"], message_id(fake, msg), **who))
    await paired.telegram.handle_update(tap(buttons(msg)["Deny"], message_id(fake, msg), **who))
    assert paired.approvals.get(ap.id).state == "pending" and not fake.sent("answerCallbackQuery")
    assert "not paired was ignored" in caplog.text
    assert any("not paired" in (t["summary"] or "") for t in traces(paired))


async def test_a_button_for_another_message_or_one_never_made_is_refused(paired, fake, clock):
    ap = ask(paired)
    msg = await send(paired, fake)
    clock.advance(5)
    await paired.telegram.handle_update(tap(buttons(msg)["Approve"], message_id(fake, msg) + 7))
    await paired.telegram.handle_update(tap("made-up-id", message_id(fake, msg)))
    assert paired.approvals.get(ap.id).state == "pending"
    assert answers(fake) == ["This button is not one this node made."] * 2


async def test_an_expired_prompt_is_refused_and_the_message_says_so(paired, fake, clock):
    ap = ask(paired)
    msg = await send(paired, fake)
    clock.advance(tg.PROMPT_TTL_S + 1)
    await paired.telegram.handle_update(tap(buttons(msg)["Approve"], message_id(fake, msg)))
    assert paired.approvals.get(ap.id).state == "pending" and "expired" in answers(fake)[-1]
    assert fake.sent("editMessageText")[-1]["text"].startswith("These buttons have expired")


async def test_content_changed_after_sending_is_a_conflict_and_decides_nothing(paired, fake, clock):
    ap = ask(paired)
    msg = await send(paired, fake)
    paired.db.execute("UPDATE approvals SET content_digest = ? WHERE id = ?", ("sha256:" + "b" * 64, ap.id))
    clock.advance(5)
    await paired.telegram.handle_update(tap(buttons(msg)["Approve"], message_id(fake, msg)))
    assert paired.approvals.get(ap.id).state == "pending"
    assert "content changed" in answers(fake)[-1]
    assert "content changed" in fake.sent("editMessageText")[-1]["text"]


# ---- what Telegram can never approve ----

def _forge(svc, ap, mid):
    """An Approve prompt for a message that never had one, as someone with the database might add it."""
    svc.db.execute("INSERT INTO telegram_prompts (prompt_id, approval_id, decision, chat_id, message_id, content_hash, "
                   "content_digest, created_at, sent_at, expires_at) VALUES (?, ?, 'approve', ?, ?, ?, ?, ?, ?, ?)",
                   ("forged", ap.id, CHAT, mid, ap.content_hash, ap.content_digest, T0, T0, T0 + 600))


CANNOT = {
    "pay": {"effect": "pay"},
    "irreversible": {"effect": "irreversible"},
    "present": {"floor": ConfirmLevel.PRESENT},
    "signed_in": {"floor": ConfirmLevel.SIGNED_IN},
    "after_untrusted": {"follows": [{"kind": "web_page", "host": "evil.example"}]},
    "members_agent": {"owner": "member-1"},
    "cut_preview": {"preview": CONTENT + "\n[preview truncated: 40 more characters]"},
}


@pytest.mark.parametrize("case", sorted(CANNOT))
async def test_what_chat_cannot_carry_has_no_approve_and_a_forged_approve_is_refused(paired, fake, clock, case):
    ap = ask(paired, **CANNOT[case])
    assert "telegram" not in (ap.methods or [])
    msg = await send(paired, fake)
    assert set(buttons(msg)) == {"Deny"} and "Telegram cannot approve this" in msg["text"]
    clock.advance(5)
    _forge(paired, ap, message_id(fake, msg))
    await paired.telegram.handle_update(tap("forged", message_id(fake, msg)))
    assert paired.approvals.get(ap.id).state == "pending"
    assert answers(fake)[-1].startswith("Nothing was decided")


@pytest.mark.parametrize("case", sorted(CANNOT))
async def test_with_telegrams_own_check_gone_the_store_still_refuses(paired, fake, clock, monkeypatch, case):
    """The decision-time rule does not depend on telegram.py: the method registry refuses the proof."""
    ap = ask(paired, **CANNOT[case])
    msg = await send(paired, fake)
    monkeypatch.setattr(paired.telegram, "approve_refusal", lambda ap: None)
    clock.advance(5)
    _forge(paired, ap, message_id(fake, msg))
    await paired.telegram.handle_update(tap("forged", message_id(fake, msg)))
    assert paired.approvals.get(ap.id).state == "pending"
    assert answers(fake)[-1].startswith("Nothing was decided: it was not approved")


async def test_a_question_cannot_be_approved_by_a_forged_tap(paired, fake, clock):
    q = question(paired)
    msg = await send(paired, fake)
    clock.advance(5)
    _forge(paired, q, message_id(fake, msg))
    await paired.telegram.handle_update(tap("forged", message_id(fake, msg)))
    assert paired.approvals.get(q.id).state == "pending"


async def test_the_store_itself_refuses_a_telegram_approve_for_what_chat_cannot_carry(paired):
    """Past every check in telegram.py: the method registry holds the proof to chat_refusal and the level."""
    from agentos.policy.confirm import Proof
    from agentos.policy.errors import ProofTooWeak

    ap = ask(paired, effect="pay")
    with pytest.raises(ProofTooWeak):
        paired.approvals.decide(ap.id, "approve", by="telegram:owner", via="telegram", in_person=True,
                                proof=Proof("telegram", ConfirmLevel.CHAT))
    assert paired.approvals.get(ap.id).state == "pending"


async def test_untrusted_text_read_since_the_message_closes_approve(paired, fake, clock):
    ap = ask(paired)
    msg = await send(paired, fake)
    assert "Approve" in buttons(msg)
    paired.taint.add(f"family:{ROOT}", ["untrusted"], "web_page", kind="web_page", host="evil.example")
    clock.advance(5)
    await paired.telegram.handle_update(tap(buttons(msg)["Approve"], message_id(fake, msg)))
    assert paired.approvals.get(ap.id).state == "pending" and "untrusted text" in answers(fake)[-1]


async def test_an_approval_made_before_pairing_was_not_offered_telegram(svc, fake, clock):
    ap = ask(svc)
    assert "telegram" not in ap.methods
    svc.notifier.save_telegram(TelegramConfig(token=TOKEN, bot_username="b", chat_id=CHAT, user_id=USER))
    assert "telegram" in ask(svc, digest="sha256:" + "c" * 64).methods
    assert svc.telegram.approve_refusal(ap) == "Telegram was not offered for this approval when it was made"


async def test_at_most_a_few_messages_offer_approve_for_one_approval(paired, fake):
    ap = ask(paired)
    for _ in range(tg.MAX_PROMPTS):
        assert "Approve" in buttons(await _resend(paired, fake, ap))
    assert set(buttons(await _resend(paired, fake, ap))) == {"Deny"}


async def _resend(svc, fake, ap):
    assert (await svc.telegram.deliver({"approval_id": ap.id})) == (True, None)
    return fake.sent("sendMessage")[-1]


# ---- the setting ----

async def test_turned_off_it_sends_nothing_and_refuses_taps(paired, fake, clock):
    ap = ask(paired)
    msg = await send(paired, fake)
    paired.confirmation.change({"methods_off": ["telegram"]}, by="owner", role="owner")
    assert "telegram" not in paired.notifier.channels_for("owner")
    clock.advance(5)
    await paired.telegram.handle_update(tap(buttons(msg)["Approve"], message_id(fake, msg)))
    await paired.telegram.handle_update(tap(buttons(msg)["Deny"], message_id(fake, msg)))
    assert paired.approvals.get(ap.id).state == "pending"
    assert answers(fake) == ["Telegram is turned off on this node. Open the panel to decide."] * 2
    count = len(fake.sent("sendMessage"))
    later = ask(paired, digest="sha256:" + "d" * 64)
    assert "telegram" not in later.methods
    await paired.notifier.flush()
    assert len(fake.sent("sendMessage")) == count
    assert await paired.telegram.deliver({"approval_id": later.id}) == (False, "telegram is turned off")


# ---- the token ----

@pytest.mark.parametrize("failure", ["connect", "500"])
async def test_the_token_is_in_no_trace_outbox_error_exception_or_log(paired, fake, failure, caplog):
    caplog.set_level(logging.DEBUG)
    fake.fail = failure
    ask(paired)
    await paired.notifier.flush()
    rows = paired.notifier.outbox(member="owner")
    assert rows and all(TOKEN not in (r["last_error"] or "") for r in rows)
    assert any(r["channel"] == "telegram" and r["last_error"] for r in rows)
    with pytest.raises(TelegramError) as e:
        await BotApi(TOKEN, transport=httpx.MockTransport(fake)).call("getMe")
    assert TOKEN not in str(e.value) and TOKEN not in repr(e.value) and e.value.__context__ is None
    assert TOKEN not in repr(BotApi(TOKEN))
    assert await paired.telegram.poll_once() > 0
    assert TOKEN not in json.dumps(paired.telegram.status()) and paired.telegram.status()["last_error"]
    assert TOKEN not in caplog.text
    assert failure == "connect" or "bot[redacted]" in caplog.text  # httpx logs the URL of each answer
    assert all(TOKEN not in json.dumps(t) for t in traces(paired))
    assert TOKEN not in json.dumps(paired.notifier.config.public())


# ---- outcomes from elsewhere ----

async def test_a_decision_made_in_the_panel_edits_the_message_and_takes_its_buttons(paired, fake):
    ap = ask(paired)
    msg = await send(paired, fake)
    paired.approvals.decide(ap.id, "deny", via="session", in_person=True)
    await paired.tick()
    edit = fake.sent("editMessageText")[-1]
    assert edit["message_id"] == message_id(fake, msg) and "reply_markup" not in edit
    assert edit["text"].startswith("Denied in the panel.")
    await paired.tick()
    assert len(fake.sent("editMessageText")) == 1  # once


async def test_a_question_answered_in_the_panel_edits_its_message(paired, fake):
    q = question(paired)
    await send(paired, fake)
    paired.approvals.decide(q.id, "approve", answer="A", via="session", in_person=True)
    await paired.telegram.sync_outcomes()
    assert fake.sent("editMessageText")[-1]["text"].startswith("Approved in the panel.")


# ---- the poller ----

async def test_the_poller_keeps_its_offset_and_survives_an_api_error(paired, fake, clock):
    fake.fail = "500"
    first = await paired.telegram.poll_once()
    assert first > 0 and paired.telegram.status()["last_error"].startswith("Telegram answered 500")
    assert await paired.telegram.poll_once() > first  # it backs off
    fake.fail = None
    ap = ask(paired)
    msg = await send(paired, fake)
    clock.advance(5)
    deny = tap(buttons(msg)["Deny"], message_id(fake, msg))
    fake.updates = [{"update_id": 7, "callback_query": "not an object"}, {"update_id": "x"}, deny]
    assert await paired.telegram.poll_once() == 0
    assert paired.approvals.get(ap.id).state == "denied"
    assert paired.telegram._offset() == deny["update_id"] + 1
    await paired.telegram.poll_once()
    assert fake.sent("getUpdates")[-1]["offset"] == deny["update_id"] + 1
    assert fake.sent("getUpdates")[-1]["timeout"] == tg.POLL_TIMEOUT_S


async def test_the_poller_runs_only_while_paired_or_pairing_and_stops_cleanly(svc, paired, monkeypatch):
    started = []

    async def run():
        started.append(True)
        import asyncio
        await asyncio.sleep(3600)

    monkeypatch.setattr(svc.telegram, "run", run)
    svc.telegram.remove()
    svc.telegram.supervise()
    assert svc.telegram._task is None
    svc.notifier.save_telegram(TelegramConfig(token=TOKEN, chat_id=CHAT, user_id=USER))
    svc.telegram.supervise()
    task = svc.telegram._task
    assert task is not None
    await svc.stop()
    assert task.cancelled() and svc.telegram._task is None


# ---- the CLI ----

def test_the_cli_reads_the_token_from_stdin_never_echoes_it_and_prints_a_pairing_link(svc, monkeypatch):
    from agentos.policy import cli

    monkeypatch.setattr(cli, "_service", lambda: svc)
    runner = CliRunner()
    out = runner.invoke(cli.app, ["notify", "telegram", "set-token", "--token-from", "-"], input=TOKEN + "\n")
    assert out.exit_code == 0, out.output
    assert "@agentos_test_bot" in out.output and TOKEN not in out.output
    out = runner.invoke(cli.app, ["notify", "telegram", "pair"])
    assert out.exit_code == 0 and "https://t.me/agentos_test_bot?start=" in out.output
    out = runner.invoke(cli.app, ["notify", "telegram", "status"])
    assert "a pairing link is waiting" in out.output and TOKEN not in out.output
    out = runner.invoke(cli.app, ["notify", "telegram", "remove"])
    assert out.exit_code == 0 and svc.telegram.config() is None
    out = runner.invoke(cli.app, ["notify", "telegram", "pair"])
    assert out.exit_code == 1


def test_a_save_elsewhere_never_undoes_a_pairing(paired, tmp_path):
    from agentos.policy.notifier import Notifier

    stale = Notifier(tmp_path, paired.db)  # loaded now: holds the pairing
    paired.telegram.remove()  # another process removes it
    stale.update_config({"rate_per_min": 9})
    assert paired.notifier.telegram_on_disk() is None


# ---- what the review found ----

async def test_off_in_one_workspace_nothing_of_it_goes_to_telegram_and_no_tap_decides_it(paired, fake, clock):
    ap = ask(paired)
    msg = await send(paired, fake)
    paired.confirmation.change({"workspaces": {"biz-a": {"methods_off": ["telegram"]}}}, by="owner", role="owner")
    clock.advance(5)
    await paired.telegram.handle_update(tap(buttons(msg)["Deny"], message_id(fake, msg)))
    assert paired.approvals.get(ap.id).state == "pending"
    assert answers(fake)[-1] == "Nothing was decided: Telegram is turned off in this workspace."
    count = len(fake.sent("sendMessage"))
    later = ask(paired, digest="sha256:" + "e" * 64)
    assert await paired.telegram.deliver({"approval_id": later.id}) == (False, "telegram is turned off in this workspace")
    assert len(fake.sent("sendMessage")) == count


def test_only_the_owner_turns_telegram_back_on_in_a_workspace(paired):
    from agentos.policy.errors import Denied

    paired.confirmation.change({"workspaces": {"biz-a": {"methods_off": ["telegram"]}}}, by="owner", role="owner")
    for patch in ({"workspaces": {"biz-a": {"methods_off": []}}}, {"workspaces": {}}):
        with pytest.raises(Denied):
            paired.confirmation.change(patch, by="ada", role="admin")
    assert "telegram" in paired.confirmation.current().off_in("biz-a")
    paired.confirmation.change({"workspaces": {"biz-a": {"methods_off": ["telegram", "chat"]}}}, by="ada", role="admin")
    paired.confirmation.change({"workspaces": {}}, by="owner", role="owner")
    assert "telegram" not in paired.confirmation.current().off_in("biz-a")


async def test_an_agent_that_keeps_rewording_stops_getting_approve_within_the_hour(paired, fake, clock):
    for i in range(tg.FAMILY_PROMPTS):
        ap = ask(paired, digest="sha256:" + f"{i:064x}")
        assert "Approve" in buttons(await _resend(paired, fake, ap))
    ap = ask(paired, digest="sha256:" + "f" * 64)
    assert set(buttons(await _resend(paired, fake, ap))) == {"Deny"}
    clock.advance(tg.FAMILY_WINDOW_S + 1)
    ap = ask(paired, digest="sha256:" + "9" * 64)
    assert "Approve" in buttons(await _resend(paired, fake, ap))


async def test_a_tap_the_node_fails_to_record_decides_nothing_and_the_buttons_work_again(paired, fake, clock, monkeypatch):
    import sqlite3

    ap = ask(paired)
    msg = await send(paired, fake)
    mid = message_id(fake, msg)
    clock.advance(5)
    real = paired.approvals.decide

    def broken(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(paired.approvals, "decide", broken)
    await paired.telegram.handle_update(tap(buttons(msg)["Approve"], mid))
    assert paired.approvals.get(ap.id).state == "pending" and "Nothing was decided" in answers(fake)[-1]
    monkeypatch.setattr(paired.approvals, "decide", real)
    await paired.telegram.handle_update(tap(buttons(msg)["Approve"], mid))
    assert paired.approvals.get(ap.id).state == "approved" and answers(fake)[-1] == "Approved."


async def test_an_outage_at_telegram_costs_one_failed_edit_a_round_not_one_per_message(paired, fake):
    for i in range(4):
        ap = ask(paired, digest="sha256:" + f"{i + 100:064x}")
        await _resend(paired, fake, ap)
        paired.approvals.decide(ap.id, "deny", by="owner", via="session", in_person=True)
    fake.fail = "500"
    assert await paired.telegram.sync_outcomes() == 0
    assert len(fake.sent("editMessageText")) == 1
    await paired.tick()  # and the background tick goes on
    fake.fail = None
    assert await paired.telegram.sync_outcomes() == 4


def test_a_message_is_cut_as_telegram_counts_it():
    text = "\U0001F600" * 3000  # 6000 UTF-16 units
    assert tg.text_length(tg.cut(text)) <= tg.MAX_TEXT and tg.cut("short") == "short"
    assert tg.text_length(tg.cut("a" + text)) == tg.MAX_TEXT - 1  # never half of a pair


# ---- what anyone can send, and what the network loses ----

async def test_a_start_flood_writes_one_row_and_one_reply_per_sender_a_window(svc, fake, clock):
    await svc.telegram.set_token(TOKEN)
    code = svc.telegram.start_pairing()
    for _ in range(20):
        await svc.telegram.handle_update(start("/start wrong", user=777))
        await svc.telegram.handle_update(start("/start", user=777, chat=-100, chat_type="group"))
    await svc.telegram.handle_update(start("/start wrong", user=778))
    rows = [t for t in traces(svc) if t["kind"] == "approval_telegram" and "ignored" in (t["summary"] or "")]
    assert len(rows) == 2  # one per sender (777 once for both kinds), not one per message
    assert len([m for m in fake.sent("sendMessage") if m["chat_id"] == 777]) == 1
    await svc.telegram.handle_update(start(f"/start {code}"))  # the owner's own code still pairs at once
    assert svc.telegram.paired()


async def test_a_message_whose_send_timed_out_keeps_working_buttons(paired, fake, clock):
    fake.fail = "readtimeout"
    ap = ask(paired)
    await paired.notifier.flush()
    fake.fail = None
    lost = fake.sent("sendMessage")[-1]  # Telegram took it; the node never heard which message it was
    rows = paired.db.all("SELECT * FROM telegram_prompts WHERE approval_id = ?", (ap.id,))
    assert rows and all(r["shown"] == "unknown" and r["message_id"] is None for r in rows)
    clock.advance(5)
    await paired.telegram.handle_update(tap(buttons(lost)["Deny"], 555))
    assert paired.approvals.get(ap.id).state == "denied"
    assert answers(fake)[-1] == "Denied."
    # a tap from a chat that is not paired adopts nothing
    other = ask(paired, digest="sha256:" + "e" * 64)
    fake.fail = "readtimeout"
    await paired.notifier.flush()
    fake.fail = None
    lost = fake.sent("sendMessage")[-1]
    await paired.telegram.handle_update(tap(buttons(lost)["Deny"], 556, user=9, chat=9))
    assert paired.approvals.get(other.id).state == "pending"
    assert all(r["message_id"] is None for r in paired.db.all(
        "SELECT * FROM telegram_prompts WHERE approval_id = ?", (other.id,)))


async def test_a_send_that_never_left_keeps_no_buttons(paired, fake):
    fake.fail = "connect"
    ap = ask(paired)
    await paired.notifier.flush()
    assert paired.db.all("SELECT * FROM telegram_prompts WHERE approval_id = ?", (ap.id,)) == []


async def test_set_token_removes_a_webhook_and_status_shows_a_conflict(svc, fake):
    await svc.telegram.set_token(TOKEN)
    assert fake.sent("deleteWebhook") == [{"drop_pending_updates": False}]
    svc.telegram.start_pairing()
    fake.fail = "409"
    assert await svc.telegram.poll_once() > 0
    status = svc.telegram.status()
    assert status["conflict"] is True
    said = tg.describe(status)
    assert "another program reads this bot's updates" in said and "\u2014" not in said
    fake.fail = None
    assert await svc.telegram.poll_once() == 0
    assert svc.telegram.status()["conflict"] is False
