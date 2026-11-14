"""login / vault_fill procedures and the MCP tool handlers, against a scripted typist."""

import fnmatch
import json
from dataclasses import dataclass
from types import SimpleNamespace

import pyotp
import pytest
from test_vault_support import (
    PASSWORD,
    SEED,
    USERNAME,
    FakeCtx,
    FakeLanes,
    FakeTypist,
    Page,
    fld,
    login_form,
    make_vault,
    private_home,
    runtime_with,
)

from agentos.core.errors import ApprovalPending, InvalidRequest, LaneNotFound, RefNotFound, ServiceUnavailable
from agentos.core.models import Effect, LaneKind, Signal
from agentos.core.runtime import Runtime
from agentos.vault import tools
from agentos.vault.errors import EntryNotFound, ResolutionDenied, SecretTooLong, VaultSealed
from agentos.vault.login import _result, fill_flow, login_flow
from agentos.vault.secret import Secret

IG = "https://www.instagram.com"
LOGIN = IG + "/accounts/login/"
LANE = "biz-a/instagram"
CODE_FORM = [fld("otp", IG, type="text", autocomplete="one-time-code", form="o")]


@pytest.fixture
def home(tmp_path):
    return private_home(tmp_path)


async def vault_with_ig(home, **kw):
    opts = ("mode", "passphrase", "catcher_factory", "clock", "sleep")
    v = make_vault(home, **{k: kw.pop(k) for k in list(kw) if k in opts})
    base = {"name": "ig", "kind": "password", "domain": "instagram.com", "lanes": [LANE], "username": USERNAME,
                "secret": PASSWORD}
    base.update(kw)
    await v.add_entry(**base)
    return v


def simple_site(after_login: Page | None = None) -> FakeTypist:
    pages = {
        "login": Page(LOGIN, login_form(IG), on_enter="next"),
        "next": after_login or Page(IG + "/", []),
    }
    return FakeTypist(pages, "login")


def assert_clean(result, ctx, *, shape=True):
    if shape:
        assert set(result) <= {"ok", "step", "needs", "signal", "field"}
    blob = json.dumps([result, ctx.traces, ctx.authorized], default=str)
    for s in (PASSWORD, USERNAME, SEED):
        assert s not in blob


async def test_full_login(home):
    v = await vault_with_ig(home)
    t = simple_site()
    ctx = FakeCtx()
    result = await login_flow(ctx, v, t, lane_id=LANE)
    assert result == {"ok": True, "step": "done", "needs": None, "signal": "ok"}
    assert t.typed_text("user") == [USERNAME] and t.typed_text("pass") == [PASSWORD]
    assert t.typed_text("search") == []
    assert t.pressed == [("pass", "Enter")]
    assert len(ctx.authorized) == 1
    auth = ctx.authorized[0]
    assert auth["effect"] is Effect.SECRET and auth["lane_id"] == LANE and auth["domain"] == "instagram.com"
    assert auth["summary"] == f"Log in to www.instagram.com on lane {LANE} with vault entry 'ig'"
    assert "—" not in auth["summary"]
    assert ctx.kinds() == ["vault.use", "vault.use"]
    assert_clean(result, ctx)


async def test_username_first_flow(home):
    v = await vault_with_ig(home)
    pages = {
        "id": Page(LOGIN, [fld("email", IG, type="email", name="identifier", form="g")], on_enter="pw"),
        "pw": Page(LOGIN + "pwd", [fld("pass", IG, type="password", form="g2")], on_enter="home"),
        "home": Page(IG + "/", []),
    }
    t = FakeTypist(pages, "id")
    result = await login_flow(FakeCtx(), v, t, lane_id=LANE)
    assert result["ok"] and t.typed_text("email") == [USERNAME] and t.typed_text("pass") == [PASSWORD]
    assert t.pressed == [("email", "Enter"), ("pass", "Enter")]


async def test_totp_step_is_filled_from_the_seed(home):
    now = 1_800_000_010.0
    v = await vault_with_ig(home, totp=SEED, clock=lambda: now)
    t = simple_site(Page(LOGIN + "2fa", CODE_FORM, on_enter="home"))
    t.pages["home"] = Page(IG + "/", [])
    ctx = FakeCtx()
    result = await login_flow(ctx, v, t, lane_id=LANE)
    assert result == {"ok": True, "step": "done", "needs": None, "signal": "ok"}
    code = t.typed_text("otp")[0]
    assert code == pyotp.TOTP(SEED).at(int(now))
    assert code not in json.dumps(ctx.traces)
    assert [f["sink"] for k, f in ctx.traces if k == "vault.use"] == ["form_username", "form_password", "form_otp"]


async def test_otp_without_a_source_asks_for_totp(home):
    v = await vault_with_ig(home)
    t = simple_site(Page(LOGIN + "2fa", CODE_FORM))
    result = await login_flow(FakeCtx(), v, t, lane_id=LANE)
    assert result == {"ok": False, "step": "submitted", "needs": "totp", "signal": "ok"}
    assert t.typed_text("otp") == []


async def test_already_at_the_code_step(home):
    t = FakeTypist({"c": Page(LOGIN + "2fa", CODE_FORM, on_enter="home"), "home": Page(IG + "/", [])}, "c")
    ctx = FakeCtx()
    v = await vault_with_ig(home)
    assert await login_flow(ctx, v, t, lane_id=LANE) == {"ok": False, "step": "start", "needs": "totp",
                                                        "signal": "ok"}
    assert ctx.authorized == [] and t.typed == []


async def test_already_at_the_code_step_with_a_seed(tmp_path):
    v = await vault_with_ig(private_home(tmp_path), totp=SEED, clock=lambda: 1_800_000_010.0)
    t = FakeTypist({"c": Page(LOGIN + "2fa", CODE_FORM, on_enter="home"), "home": Page(IG + "/", [])}, "c")
    result = await login_flow(FakeCtx(), v, t, lane_id=LANE)
    assert result["ok"] and t.typed_text("otp") == [pyotp.TOTP(SEED).at(1_800_000_010)]


async def test_split_code_boxes(home):
    v = await vault_with_ig(home, totp=SEED, clock=lambda: 1_800_000_010.0)
    boxes = [fld(f"d{i}", IG, type="text", inputmode="numeric", maxlength=1, form="o") for i in range(6)]
    t = simple_site(Page(LOGIN + "2fa", boxes, on_enter="home"))
    t.pages["home"] = Page(IG + "/", [])
    result = await login_flow(FakeCtx(), v, t, lane_id=LANE)
    assert result["ok"]
    digits = [t.typed_text(f"d{i}")[0] for i in range(6)]
    assert all(len(d) == 1 for d in digits) and "".join(digits).isdigit()


class FakeCatcher:
    def __init__(self, code):
        self.code = code
        self.calls = []

    async def wait_for_code(self, rule, *, since):
        self.calls.append((rule, since))
        return Secret(self.code) if self.code else None


async def test_email_code_catcher(home):
    catchers = []

    def factory(host, port, user, pw):
        assert (host, port, user.reveal(), pw.reveal()) == ("imap.gmail.com", 993, "me@gmail.com", "mail-pw-1")
        catchers.append(FakeCatcher("424242"))
        return catchers[-1]

    v = await vault_with_ig(home, catcher_factory=factory)
    await v.add_entry(name="mail", kind="password", domain="gmail.com", lanes=["biz-a/*"],
                      username="me@gmail.com", secret="mail-pw-1")
    await v.set_config("ig", {"catcher": {"account": "mail", "host": "imap.gmail.com",
                                          "sender": "@mail.instagram.com"}})
    t = simple_site(Page(LOGIN + "code", CODE_FORM, on_enter="home"))
    t.pages["home"] = Page(IG + "/", [])
    ctx = FakeCtx()
    result = await login_flow(ctx, v, t, lane_id=LANE)
    assert result["ok"] and t.typed_text("otp") == ["424242"]
    assert catchers[0].calls[0][0].sender == "@mail.instagram.com"
    blob = json.dumps(ctx.traces)
    assert "424242" not in blob and "mail-pw-1" not in blob and "me@gmail.com" not in blob
    assert ("vault.use" in ctx.kinds()) and any(f.get("sink") == "imap_login" for _, f in ctx.traces)


async def test_email_code_catcher_timeout(home):
    v = await vault_with_ig(home, catcher_factory=lambda *a: FakeCatcher(None))
    await v.add_entry(name="mail", kind="password", domain="gmail.com", lanes=[LANE], username="u", secret="p1234")
    await v.set_config("ig", {"catcher": {"account": "mail", "host": "imap.gmail.com", "sender": "@x.com"}})
    t = simple_site(Page(LOGIN + "code", CODE_FORM))
    ctx = FakeCtx()
    result = await login_flow(ctx, v, t, lane_id=LANE)
    assert result == {"ok": False, "step": "submitted", "needs": "totp", "signal": "ok"}
    assert "vault.code_timeout" in ctx.kinds()


async def test_catcher_mailbox_must_be_bound_and_on_its_domain(home):
    v = await vault_with_ig(home, catcher_factory=lambda *a: FakeCatcher("1"))
    await v.add_entry(name="mail", kind="password", domain="gmail.com", lanes=["other/*"], username="u",
                      secret="p1234")
    await v.set_config("ig", {"catcher": {"account": "mail", "host": "imap.gmail.com", "sender": "@x.com"}})
    t = simple_site(Page(LOGIN + "code", CODE_FORM))
    with pytest.raises(ResolutionDenied) as exc:
        await login_flow(FakeCtx(), v, t, lane_id=LANE)
    assert exc.value.reason == "lane_not_bound"
    assert t.typed_text("otp") == []


async def test_captcha_before_anything(home):
    v = await vault_with_ig(home)
    t = simple_site()
    t.pages["login"].signal = Signal.CAPTCHA
    ctx = FakeCtx()
    result = await login_flow(ctx, v, t, lane_id=LANE)
    assert result == {"ok": False, "step": "start", "needs": "captcha", "signal": "captcha"}
    assert ctx.authorized == [] and t.typed == []


async def test_captcha_after_submit(home):
    v = await vault_with_ig(home)
    t = simple_site(Page(LOGIN + "challenge", [], signal=Signal.CAPTCHA))
    result = await login_flow(FakeCtx(), v, t, lane_id=LANE)
    assert result == {"ok": False, "step": "submitted", "needs": "captcha", "signal": "captcha"}


async def test_wrong_password_leaves_the_form(home):
    v = await vault_with_ig(home)
    t = FakeTypist({"login": Page(LOGIN, login_form(IG), on_enter="login")}, "login")
    result = await login_flow(FakeCtx(), v, t, lane_id=LANE)
    assert result == {"ok": False, "step": "submitted", "needs": "human", "signal": "ok"}


@pytest.mark.parametrize("signal", [Signal.LOGGED_OUT, Signal.BLOCKED, Signal.RATE_LIMITED])
async def test_bad_signals_after_submit(home, signal):
    v = await vault_with_ig(home)
    t = simple_site(Page(IG + "/", [], signal=signal))
    result = await login_flow(FakeCtx(), v, t, lane_id=LANE)
    assert result["ok"] is False and result["needs"] == "human" and result["signal"] == signal.value


async def test_redirect_off_site_is_not_reported_as_success(home):
    v = await vault_with_ig(home)
    t = simple_site(Page("https://www.facebook.com/checkpoint", []))
    result = await login_flow(FakeCtx(), v, t, lane_id=LANE)
    assert result == {"ok": False, "step": "left_site", "needs": "human", "signal": "ok"}


async def test_no_login_form(home):
    v = await vault_with_ig(home)
    t = FakeTypist({"home": Page(IG + "/", [fld("q", IG, type="search")])}, "home")
    ctx = FakeCtx()
    assert await login_flow(ctx, v, t, lane_id=LANE) == {"ok": False, "step": "no_login_form", "needs": None,
                                                        "signal": "ok"}
    assert ctx.authorized == []


async def test_no_entry_for_the_site(home):
    v = await vault_with_ig(home)
    t = FakeTypist({"l": Page("https://www.reddit.com/login", login_form("https://www.reddit.com"))}, "l")
    ctx = FakeCtx()
    with pytest.raises(ResolutionDenied) as exc:
        await login_flow(ctx, v, t, lane_id=LANE)
    assert exc.value.reason == "no_entry" and ctx.authorized == [] and t.typed == []
    assert ctx.kinds() == ["vault.deny"]


@pytest.mark.parametrize("url,reason", [
    ("https://instagram.com.evil.io/accounts/login/", "no_entry"),
    ("https://www.xn--instgram-46g.com/accounts/login/", "no_entry"),
    ("http://www.instagram.com/accounts/login/", "insecure_scheme"),
    ("about:blank", "bad_url"),
])
async def test_lookalike_and_insecure_pages_get_nothing(home, url, reason):
    v = await vault_with_ig(home)
    origin = url.split("/accounts")[0] if url.startswith("http") else IG
    t = FakeTypist({"l": Page(url, login_form(origin))}, "l")
    ctx = FakeCtx()
    with pytest.raises(ResolutionDenied) as exc:
        await login_flow(ctx, v, t, lane_id=LANE)
    assert exc.value.reason == reason and t.typed == [] and ctx.authorized == []


async def test_named_entry_on_wrong_site_or_lane(home):
    v = await vault_with_ig(home)
    t = FakeTypist({"l": Page("https://evil.io/login", login_form("https://evil.io"))}, "l")
    with pytest.raises(ResolutionDenied) as exc:
        await login_flow(FakeCtx(), v, t, lane_id=LANE, entry="ig")
    assert exc.value.reason == "domain_mismatch"
    with pytest.raises(ResolutionDenied) as exc:
        await login_flow(FakeCtx(), v, simple_site(), lane_id="biz-b/instagram", entry="ig")
    assert exc.value.reason == "lane_not_bound"
    with pytest.raises(EntryNotFound):
        await login_flow(FakeCtx(), v, simple_site(), lane_id=LANE, entry="nope")


async def test_several_matching_entries_need_a_name(home):
    v = await vault_with_ig(home)
    await v.add_entry(name="ig second", kind="password", domain="instagram.com", lanes=["biz-a/*"],
                      username="second", secret="second-pw")
    with pytest.raises(InvalidRequest) as exc:
        await login_flow(FakeCtx(), v, simple_site(), lane_id=LANE)
    assert "'ig'" in exc.value.message and "'ig second'" in exc.value.message
    t = simple_site()
    assert (await login_flow(FakeCtx(), v, t, lane_id=LANE, entry="ig second"))["ok"]
    assert t.typed_text("pass") == ["second-pw"]


async def test_cross_origin_iframe_form_is_refused_before_approval(home):
    v = await vault_with_ig(home)
    t = FakeTypist({"l": Page(LOGIN, login_form(IG, chain=(IG, "https://evil.io")))}, "l")
    ctx = FakeCtx()
    with pytest.raises(ResolutionDenied) as exc:
        await login_flow(ctx, v, t, lane_id=LANE)
    assert exc.value.reason == "cross_origin_frame" and ctx.authorized == [] and t.typed == []


async def test_approval_pending_types_nothing(home):
    v = await vault_with_ig(home)
    t = simple_site()
    with pytest.raises(ApprovalPending):
        await login_flow(FakeCtx(deny=ApprovalPending("needs you", approval_id="ap_1")), v, t, lane_id=LANE)
    assert t.typed == [] and t.focused == []


async def test_page_swapped_to_evil_between_fields(home):
    """The page navigates away when the password field is focused: no password is typed."""
    v = await vault_with_ig(home)
    t = simple_site()
    t.pages["evil"] = Page("https://instagram-login.evil.io/", login_form("https://instagram-login.evil.io"))
    t.navigate_on_focus["pass"] = "evil"
    ctx = FakeCtx()
    with pytest.raises(ResolutionDenied) as exc:
        await login_flow(ctx, v, t, lane_id=LANE)
    assert exc.value.reason == "domain_mismatch"
    assert t.typed_text("pass") == [] and t.typed_text("user") == [USERNAME]
    assert "vault.deny" in ctx.kinds()


async def test_field_moved_into_a_foreign_frame_after_focus(home):
    v = await vault_with_ig(home)
    framed = login_form(IG, chain=(IG, "https://evil.io"))
    t = simple_site()
    t.pages["framed"] = Page(LOGIN, framed)
    t.navigate_on_focus["pass"] = "framed"
    with pytest.raises(ResolutionDenied) as exc:
        await login_flow(FakeCtx(), v, t, lane_id=LANE)
    assert exc.value.reason == "cross_origin_frame" and t.typed_text("pass") == []


async def test_typist_abort_reports_human(home):
    v = await vault_with_ig(home)
    t = simple_site()
    t.abort_on.add("pass")
    ctx = FakeCtx()
    result = await login_flow(ctx, v, t, lane_id=LANE)
    assert result == {"ok": False, "step": "username", "needs": "human", "signal": "ok"}
    assert "vault.abort" in ctx.kinds()


async def test_sealed_vault_fails_before_approval(home):
    v = make_vault(home, mode="passphrase", passphrase="a long test passphrase")
    await v.unseal("a long test passphrase")
    await v.add_entry(name="ig", kind="password", domain="instagram.com", lanes=[LANE], secret=PASSWORD)
    v.seal()
    ctx = FakeCtx()
    t = simple_site()
    with pytest.raises(VaultSealed):
        await login_flow(ctx, v, t, lane_id=LANE)
    assert ctx.authorized == [] and t.typed == []


async def test_secret_too_long_to_type(home):
    v = await vault_with_ig(home, secret="x" * 1500, username=None)
    t = simple_site()
    with pytest.raises(SecretTooLong) as exc:
        await login_flow(FakeCtx(), v, t, lane_id=LANE)
    assert t.typed == [] and "x" * 20 not in exc.value.message


async def test_entry_without_username_types_only_the_password(home):
    v = await vault_with_ig(home, username=None)
    t = simple_site()
    assert (await login_flow(FakeCtx(), v, t, lane_id=LANE))["ok"]
    assert t.typed_text("user") == [] and t.typed_text("pass") == [PASSWORD]


async def test_unicode_password(home):
    pw = "pässwörd-密码-🔐-ﬁ"
    v = await vault_with_ig(home, secret=pw)
    t = simple_site()
    assert (await login_flow(FakeCtx(), v, t, lane_id=LANE))["ok"]
    assert t.typed_text("pass") == [pw]


# ---- vault_fill ----------------------------------------------------------------

def fill_site():
    fields = [*login_form(IG), fld("comment", IG, type="text", name="comment", form="c"), *CODE_FORM]
    refs = {"e1": "user", "e2": "pass", "e3": "comment", "e4": "otp", "e5": "search"}
    return FakeTypist({"p": Page(LOGIN, fields, refs=refs)}, "p")


async def test_fill_picks_the_value_from_the_field_structure(home):
    v = await vault_with_ig(home, totp=SEED, clock=lambda: 1_800_000_010.0)
    t = fill_site()
    ctx = FakeCtx()
    assert (await fill_flow(ctx, v, t, lane_id=LANE, field_ref="e2", entry="ig")) == {
        "ok": True, "field": "password", "needs": None, "signal": "ok"}
    assert (await fill_flow(ctx, v, t, lane_id=LANE, field_ref="e1", entry="ig"))["field"] == "username"
    assert (await fill_flow(ctx, v, t, lane_id=LANE, field_ref="e4", entry="ig"))["field"] == "one_time_code"
    assert t.typed_text("pass") == [PASSWORD] and t.typed_text("user") == [USERNAME]
    assert t.typed_text("otp")[0].isdigit() and t.pressed == []
    assert ctx.authorized[0]["summary"] == (
        f"Fill the password field on www.instagram.com on lane {LANE} from vault entry 'ig'")
    assert_clean({}, ctx)


@pytest.mark.parametrize("ref", ["e3", "e5"])
async def test_fill_refuses_arbitrary_inputs(home, ref):
    v = await vault_with_ig(home)
    t = fill_site()
    ctx = FakeCtx()
    with pytest.raises(ResolutionDenied) as exc:
        await fill_flow(ctx, v, t, lane_id=LANE, field_ref=ref, entry="ig")
    assert exc.value.reason == "not_a_login_field" and t.typed == [] and ctx.authorized == []


async def test_fill_refuses_wrong_lane_and_missing_refs(home):
    v = await vault_with_ig(home)
    with pytest.raises(ResolutionDenied):
        await fill_flow(FakeCtx(), v, fill_site(), lane_id="biz-b/x", field_ref="e2", entry="ig")
    with pytest.raises(RefNotFound):
        await fill_flow(FakeCtx(), v, fill_site(), lane_id=LANE, field_ref="e99", entry="ig")


# ---- tool handlers -------------------------------------------------------------

def test_tool_specs():
    specs = {s.name: s for s in tools.tool_specs(Runtime())}
    assert set(specs) == {"login", "vault_fill", "vault_list"}
    assert specs["login"].effect is Effect.SECRET and specs["vault_fill"].effect is Effect.SECRET
    assert specs["vault_list"].effect is Effect.READ
    for s in specs.values():
        assert s.description and "—" not in s.description


async def test_login_tool_end_to_end(home):
    v = await vault_with_ig(home)
    t = simple_site()
    rt = runtime_with(v, t)
    ctx = FakeCtx(rt)
    result = await tools.login(ctx, on=LANE)
    assert result["ok"] and rt.lanes.used == [LANE]
    assert_clean(result, ctx)


async def test_login_tool_input_checks(home):
    v = await vault_with_ig(home)
    rt = runtime_with(v, simple_site(), lanes={LANE: LaneKind.BROWSER, "biz-a/shell": LaneKind.TERMINAL})
    with pytest.raises(InvalidRequest):
        await tools.login(FakeCtx(rt), on="not a lane")
    with pytest.raises(InvalidRequest):
        await tools.login(FakeCtx(rt), on="biz-a/shell")
    with pytest.raises(LaneNotFound):
        await tools.login(FakeCtx(rt), on="biz-a/ghost")
    with pytest.raises(ServiceUnavailable):
        await tools.login(FakeCtx(runtime_with(v, None)), on=LANE)


async def test_vault_fill_tool(home):
    v = await vault_with_ig(home)
    rt = runtime_with(v, fill_site())
    assert (await tools.vault_fill(FakeCtx(rt), on=LANE, field="e2", entry="ig"))["ok"]


@dataclass
class Listed:
    """An entry as the vault lists it: vault_list reads only `listing()`."""

    name: str
    lanes: list[str]

    def listing(self) -> dict:
        return {"name": self.name, "kind": "password", "domain": "example.com", "lanes": list(self.lanes)}


class ListingVault:
    def __init__(self, entries):
        self.entries = entries

    async def list_entries(self, lane=None):
        return [e for e in self.entries if lane is None or lane in e.lanes]

    def state(self):
        return "unsealed"


class ListingLanes(FakeLanes):
    def list(self):
        return list(self.infos.values())


class GrantCtx(FakeCtx):
    """A caller whose grant covers the lanes `pattern` matches; an admin grant when `admin`."""

    def __init__(self, runtime, *, pattern="biz-a/*", admin=False):
        super().__init__(runtime)
        self.grant = SimpleNamespace(id="gr_test", admin=admin)
        self.pattern = pattern

    def can_access_lane(self, lane_id):
        return fnmatch.fnmatchcase(lane_id, self.pattern)


async def test_vault_list_tool(home):
    v = await vault_with_ig(home, totp=SEED)
    await v.add_entry(name="reddit", kind="password", domain="reddit.com", lanes=["personal/reddit"], secret="r1234")
    rt = runtime_with(v)
    ctx = GrantCtx(rt, admin=True)  # the owner's view: the whole vault
    out = await tools.vault_list(ctx)
    assert out["entries"] == [
        {"name": "ig", "kind": "password", "domain": "instagram.com", "lanes": [LANE]},
        {"name": "reddit", "kind": "password", "domain": "reddit.com", "lanes": ["personal/reddit"]},
    ]
    assert ctx.authorized[0]["effect"] is Effect.READ
    only = await tools.vault_list(ctx, on=LANE)
    assert [e["name"] for e in only["entries"]] == ["ig"]
    assert_clean(out, ctx, shape=False)


async def test_vault_list_without_on_shows_only_the_lanes_the_grant_reaches():
    vault = ListingVault([
        Listed("own", ["biz-a/instagram"]),
        Listed("other", ["biz-b/x"]),
        Listed("mixed", ["biz-a/instagram", "biz-b/x"]),
        Listed("ws-own", ["biz-a/*"]),
        Listed("ws-other", ["biz-b/*"]),
    ])
    lanes = ListingLanes({"biz-a/instagram": LaneKind.BROWSER, "biz-b/x": LaneKind.BROWSER})
    rt = Runtime(vault=vault, lanes=lanes)
    out = await tools.vault_list(GrantCtx(rt))  # a grant for biz-a/*
    assert [(e["name"], e["lanes"]) for e in out["entries"]] == [
        ("own", ["biz-a/instagram"]), ("mixed", ["biz-a/instagram"]), ("ws-own", ["biz-a/*"])]
    everything = await tools.vault_list(GrantCtx(rt, admin=True))
    assert [e["name"] for e in everything["entries"]] == ["own", "other", "mixed", "ws-own", "ws-other"]


def test_a_code_result_says_how_to_pick_the_login_up_again():
    out = _result(False, "submitted", "code", Signal.OK)
    assert out["needs"] == "code" and "login" in out["next"]
    assert "next" not in _result(False, "submitted", "totp", Signal.OK)
    assert "next" not in _result(True, "done", None, Signal.OK)
