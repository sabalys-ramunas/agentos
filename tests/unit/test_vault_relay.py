"""The code card (the "relay" code source): one request per login step, tied to its lane, site
and window, answered once by the owner, typed by the node, and never in a trace or an event."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from test_vault_support import FakeCtx, FakeTypist, Page, fld, login_form, make_vault, private_home
from testkit.timing import until

from openajar.core.errors import InvalidRequest
from openajar.core.models import Principal
from openajar.core.runtime import Runtime
from openajar.policy.redact import redact_text
from openajar.vault.catcher import validate_catcher_config
from openajar.vault.errors import CodeRefused, CodeRequestClosed, EntryInvalid, ResolutionDenied
from openajar.vault.login import fill_flow, login_flow
from openajar.vault.relay import new_request_id, normalize_code
from openajar.vault.secret import Secret

IG = "https://www.instagram.com"
LOGIN = IG + "/accounts/login/"
LANE = "biz-a/instagram"
CODE = "424242"
CODE_FORM = [fld("otp", IG, type="text", autocomplete="one-time-code", inputmode="numeric", form="o")]


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, event):
        self.sent.append(event)
        return [1]


class FakeBus:
    def __init__(self):
        self.events = []

    def publish(self, kind, payload):
        self.events.append((kind, payload))


@pytest.fixture
def home(tmp_path):
    return private_home(tmp_path)


async def relay_vault(home, **cfg):
    v = make_vault(home)
    await v.add_entry(name="ig", kind="password", domain="instagram.com", lanes=[LANE], username="me@example.org",
                      secret="Tr0ub4dor&3-correct-horse")
    await v.set_config("ig", {"catcher": {"kind": "relay", **cfg}})
    v.relay.runtime = Runtime(notifier=FakeNotifier(), events=FakeBus())
    return v


def code_site() -> FakeTypist:
    return FakeTypist({
        "login": Page(LOGIN, login_form(IG), on_enter="code"),
        "code": Page(LOGIN + "code", CODE_FORM, on_enter="home"),
        "home": Page(IG + "/", []),
    }, "login")


async def opened(v, n=1):
    assert await until(lambda: len(v.relay.list()) >= n, every=0.005), "no code request opened"
    return v.relay.list()


def everything(v, ctx, *extra) -> str:
    rt = v.relay.runtime
    return json.dumps([ctx.traces, rt.notifier.sent, rt.events.events, *extra], default=str)


# ---- config and ids ---------------------------------------------------------------

def test_relay_config():
    assert validate_catcher_config({"kind": "relay"}) == {"kind": "relay", "window_seconds": 300,
                                                          "timeout_seconds": 300}
    assert validate_catcher_config({"kind": "relay", "window_seconds": 120, "timeout_seconds": 60})["timeout_seconds"] \
        == 60
    for bad in ({"kind": "relay", "sender": "@x.com"}, {"kind": "relay", "window_seconds": 5},
                {"kind": "relay", "timeout_seconds": 10_000}, {"kind": "relay", "timeout_seconds": True}):
        with pytest.raises(EntryInvalid):
            validate_catcher_config(bad)


def test_ids_and_code_normalization():
    a, b = new_request_id(), new_request_id()
    assert a.startswith("cr_") and len(a) == 29 and a != b and a == a.lower()
    assert redact_text(f"request {a}") == f"request {a}"  # an audit id, not a secret
    assert normalize_code(Secret(" 424 242 ")).reveal() == CODE
    assert normalize_code(Secret("G-12345")).reveal() == "G12345"
    for bad in ("987", "9876543210987", "98 76 5!", ""):
        with pytest.raises(InvalidRequest) as exc:
            normalize_code(Secret(bad))
        assert exc.value.message == "A code is 4 to 12 letters or digits."


# ---- the login ----------------------------------------------------------------------

async def test_login_waits_for_the_owner_and_types_the_code(home):
    v = await relay_vault(home)
    t = code_site()
    ctx = FakeCtx()
    task = asyncio.create_task(login_flow(ctx, v, t, lane_id=LANE))
    [req] = await opened(v)
    public = req.public()
    assert set(public) == {"id", "state", "lane", "site", "requested_by", "created_at", "expires_at"}
    assert (public["lane"], public["site"], public["state"]) == (LANE, "instagram.com", "open")
    assert public["requested_by"] == {"kind": "agent", "id": "test-agent"}

    [push] = v.relay.runtime.notifier.sent
    assert push["type"] == "code.requested" and push["code_request_id"] == req.id and push["urgent"]
    assert (push["lane"], push["site"]) == (LANE, "instagram.com") and "—" not in push["title"] + push["body"]

    done = v.relay.submit(req.id, Secret("424 242"), by="human:owner")
    assert done.state == "used" and done.closed_by == "human:owner"
    result = await task
    assert result == {"ok": True, "step": "done", "needs": None, "signal": "ok"}
    assert t.typed_text("otp") == [CODE] and ("otp", "Enter") in t.pressed
    assert v.relay.list() == []

    kinds = ctx.kinds()
    assert kinds.index("vault.code_requested") < kinds.index("vault.code_used")
    used = dict(ctx.traces)["vault.code_used"]
    assert used["source"] == "manual" and used["consent"] == "manual" and used["request"] == req.id
    assert used["by"] == "human:owner" and used["requester"] == "test-agent" and used["host"] == "www.instagram.com"
    otp_use = [f for k, f in ctx.traces if k == "vault.use" and f["sink"] == "form_otp"]
    assert otp_use[0]["source"] == "manual"
    states = [p["state"] for kind, p in v.relay.runtime.events.events if kind == "code"]
    assert states == ["open", "used"]
    assert CODE not in everything(v, ctx, result, public, done.public())
    assert v.scrub(f"page says {CODE}", LANE) == "page says [redacted]"  # the scrubber knows it now

    with pytest.raises(CodeRequestClosed):  # single use
        v.relay.submit(req.id, Secret("111111"), by="human:owner")


async def test_no_code_in_time_needs_code_and_closes_the_card(home):
    v = await relay_vault(home)
    v.relay.max_wait = 0.05
    t = code_site()
    ctx = FakeCtx()
    result = await login_flow(ctx, v, t, lane_id=LANE)
    next_step = result.pop("next")  # the agent is told to call login again once
    assert "login" in next_step
    assert result == {"ok": False, "step": "submitted", "needs": "code", "signal": "ok"}
    assert t.typed_text("otp") == [] and v.relay.list() == []
    timeout = dict(ctx.traces)["vault.code_timeout"]
    assert timeout["request"].startswith("cr_") and timeout["lane"] == LANE
    assert [p["state"] for _, p in v.relay.runtime.events.events] == ["open", "expired"]
    with pytest.raises(CodeRequestClosed):
        v.relay.submit(timeout["request"], Secret(CODE), by="human:owner")


async def test_the_owner_declines(home):
    v = await relay_vault(home)
    t = code_site()
    ctx = FakeCtx()
    task = asyncio.create_task(login_flow(ctx, v, t, lane_id=LANE))
    [req] = await opened(v)
    v.relay.decline(req.id, by="human:owner")
    assert await task == {"ok": False, "step": "submitted", "needs": "human", "signal": "ok"}
    assert t.typed_text("otp") == []
    declined = dict(ctx.traces)["vault.code_declined"]
    assert declined["request"] == req.id and declined["by"] == "human:owner"
    assert "vault.code_used" not in ctx.kinds()


async def test_a_grant_never_answers_its_own_request(home):
    v = await relay_vault(home)
    t = code_site()
    ctx = FakeCtx()
    ctx.principal = Principal(id="va-dana", kind="agent", grant_id="g_leaf")
    ctx.grant = SimpleNamespace(path="/g_root/g_leaf/")
    task = asyncio.create_task(login_flow(ctx, v, t, lane_id=LANE))
    [req] = await opened(v)
    with pytest.raises(CodeRefused):
        v.relay.submit(req.id, Secret(CODE), by="agent:va-dana", family="g_root")
    assert v.relay.list() == [req]  # still open for the owner
    v.relay.submit(req.id, Secret(CODE), by="agent:approver", family="g_other")
    assert (await task)["ok"] and t.typed_text("otp") == [CODE]


async def test_a_bad_code_does_not_use_up_the_request(home):
    v = await relay_vault(home)
    t = code_site()
    task = asyncio.create_task(login_flow(FakeCtx(), v, t, lane_id=LANE))
    [req] = await opened(v)
    with pytest.raises(InvalidRequest):
        v.relay.submit(req.id, Secret("12"), by="human:owner")
    with pytest.raises(InvalidRequest):
        v.relay.submit(req.id, "424242", by="human:owner")  # a plain str is refused
    v.relay.submit(req.id, Secret(CODE), by="human:owner")
    assert (await task)["ok"]


async def test_the_page_is_checked_again_before_typing(home):
    v = await relay_vault(home)
    t = code_site()
    t.pages["evil"] = Page("https://instagram-login.example.com/code", CODE_FORM)
    ctx = FakeCtx()
    task = asyncio.create_task(login_flow(ctx, v, t, lane_id=LANE))
    [req] = await opened(v)
    t.current = "evil"  # the page moved off the entry's site while the owner was reading the SMS
    v.relay.submit(req.id, Secret(CODE), by="human:owner")
    with pytest.raises(ResolutionDenied):
        await task
    assert t.typed_text("otp") == [] and "vault.deny" in ctx.kinds()


async def test_already_at_the_code_step_asks_the_owner(home):
    v = await relay_vault(home)
    t = FakeTypist({"c": Page(LOGIN + "code", CODE_FORM, on_enter="home"), "home": Page(IG + "/", [])}, "c")
    task = asyncio.create_task(login_flow(FakeCtx(), v, t, lane_id=LANE))
    [req] = await opened(v)
    v.relay.submit(req.id, Secret(CODE), by="human:owner")
    result = await task
    assert result["ok"] and result["step"] == "done" and t.typed_text("otp") == [CODE]


async def test_a_rejected_code_needs_a_human(home):
    v = await relay_vault(home)
    t = code_site()
    t.pages["code"].on_enter = "code"  # the site says the code is wrong
    task = asyncio.create_task(login_flow(FakeCtx(), v, t, lane_id=LANE))
    [req] = await opened(v)
    v.relay.submit(req.id, Secret(CODE), by="human:owner")
    assert await task == {"ok": False, "step": "code", "needs": "human", "signal": "ok"}
    assert v.relay.list() == []  # no second request in the same call


async def test_vault_fill_uses_the_code_card(home):
    v = await relay_vault(home)
    t = FakeTypist({"c": Page(LOGIN + "code", CODE_FORM, refs={"e7": "otp"})}, "c")
    ctx = FakeCtx()
    task = asyncio.create_task(fill_flow(ctx, v, t, lane_id=LANE, field_ref="e7", entry="ig"))
    [req] = await opened(v)
    v.relay.submit(req.id, Secret(CODE), by="human:owner")
    assert await task == {"ok": True, "field": "one_time_code", "needs": None, "signal": "ok"}
    assert t.typed_text("otp") == [CODE] and t.pressed == []
    v.relay.max_wait = 0.02
    assert (await fill_flow(ctx, v, t, lane_id=LANE, field_ref="e7", entry="ig"))["needs"] == "code"


async def test_a_cancelled_login_closes_its_card(home):
    v = await relay_vault(home)
    task = asyncio.create_task(login_flow(FakeCtx(), v, code_site(), lane_id=LANE))
    [req] = await opened(v)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert v.relay.list() == [] and req.state == "cancelled"
    with pytest.raises(CodeRequestClosed):
        v.relay.submit(req.id, Secret(CODE), by="human:owner")


async def test_sealing_the_vault_closes_open_cards(home):
    v = await relay_vault(home)
    t = code_site()
    task = asyncio.create_task(login_flow(FakeCtx(), v, t, lane_id=LANE))
    [req] = await opened(v)
    v.seal()
    assert (await task)["needs"] == "code" and t.typed_text("otp") == []
    assert req.state == "cancelled" and v.relay.list() == []


async def test_a_card_past_its_window_is_closed(home):
    v = await relay_vault(home, window_seconds=60)
    now = [1_800_000_000.0]
    v.relay._clock = lambda: now[0]
    task = asyncio.create_task(login_flow(FakeCtx(), v, code_site(), lane_id=LANE))
    [req] = await opened(v)
    now[0] += 61
    assert v.relay.list() == []
    with pytest.raises(CodeRequestClosed):
        v.relay.submit(req.id, Secret(CODE), by="human:owner")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_codes_from_another_thread_reach_the_waiting_login(home):
    v = await relay_vault(home)
    t = code_site()
    task = asyncio.create_task(login_flow(FakeCtx(), v, t, lane_id=LANE))
    [req] = await opened(v)
    await asyncio.to_thread(v.relay.submit, req.id, Secret(CODE), by="human:owner")
    assert (await task)["ok"] and t.typed_text("otp") == [CODE]
