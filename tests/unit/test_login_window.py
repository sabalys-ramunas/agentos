"""A login that runs with no confirmation when its password is in the vault: the egress guard's login
window (docs/design/egress-guard.md, 8.11), who can open it, and the attacks on it. Real guard, real
fill flows, fake requests and a scripted typist."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from urllib.parse import quote

import pytest
from test_commit_gate_guard import Gate
from test_egress import COMMITTER, FakeContext, FakePage, FakeRequest, FakeRoute, make_guard
from test_policy_support import Clock as PolicyClock
from test_policy_support import make_service
from test_vault_support import (
    PASSWORD,
    USERNAME,
    FakeCtx,
    FakeTypist,
    Page,
    fld,
    login_form,
    make_vault,
    private_home,
)

from openajar.core.errors import ApprovalPending
from openajar.core.models import Effect
from openajar.node import egress
from openajar.node import vault_tools as tools
from openajar.node.egress import LOGIN_WINDOW_S, Driver, EffectRule, EgressTrace, LaneGuard
from openajar.node.routes import RouteDispatcher
from openajar.node.typist import PlaywrightTypist
from openajar.policy.errors import Denied
from openajar.policy.grants import LaneCap, Specials
from openajar.vault.errors import ResolutionDenied, SecretTooLong
from openajar.vault.login import TYPE_MAX, fill_flow, login_flow

JSON = {"content-type": "application/json"}
FORM = {"content-type": "application/x-www-form-urlencoded"}
REDDIT_LOGIN = "https://www.reddit.com/svc/shreddit/account/login"
FB_LOGIN = "https://www.facebook.com/login/device-based/regular/login/"
REDDIT_GQL = "https://www.reddit.com/svc/shreddit/graphql"
FB_GQL = "https://www.facebook.com/api/graphql/"
IG = "https://www.instagram.com"
LANE = "biz-a/instagram"
DRAFT = Driver(kind="agent", id="worker", grant_id="g", ceiling="draft")


def login_post(url: str = REDDIT_LOGIN, method: str = "POST") -> FakeRequest:
    # the body is the site's own business: Facebook and Instagram encrypt the password in the browser
    return FakeRequest(url, method, headers=FORM, body=b"username=a&password=%23PWD_BROWSER%3A5%3Aencrypted")


async def send(guard, ctx, gate, *requests) -> list[FakeRoute]:
    """Each request through the gate's route during an agent's act."""
    routes = []
    async with guard.acting(gate=gate):
        [(_, handler)] = ctx.routes
        for request in requests:
            route = FakeRoute()
            await handler(route, request)
            routes.append(route)
    return routes


def driven(tmp_path, driver=COMMITTER, **kw):
    guard, ctx, _, clock = make_guard(tmp_path, sites=(), driver=driver, **kw)
    return guard, ctx, clock, Gate()


def trace_rows(tmp_path) -> list[dict]:
    path = tmp_path / "trace.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


# ------------------------------------------------------------------ what the window lets go

async def test_a_login_post_is_held_with_no_window(tmp_path):
    guard, ctx, _, gate = driven(tmp_path)
    [route] = await send(guard, ctx, gate, login_post())
    assert route.aborted and gate.seen == ["reddit.unknown"]


async def test_a_login_post_goes_inside_the_window_and_is_traced_as_a_login(tmp_path):
    guard, ctx, _, gate = driven(tmp_path)
    guard.open_login("reddit.com")
    [route] = await send(guard, ctx, gate, login_post())
    assert route.continued == 1 and not route.aborted and gate.seen == []
    [row] = [r for r in trace_rows(tmp_path) if r["kind"] == "gate_classified"]
    assert row["decision"] == "login" and row["host"] == "www.reddit.com" and row["method"] == "POST"
    assert "encrypted" not in json.dumps(row)  # names only, never a value


async def test_a_draft_grant_logs_in_where_the_unknown_rule_refused_it(tmp_path):
    """The benchmark's case: a login POST refused as `<site>.unknown` under a draft grant, before the
    gate was even asked."""
    guard, ctx, _, gate = driven(tmp_path, driver=DRAFT)
    [held] = await send(guard, ctx, gate, login_post())
    assert held.aborted and guard.blocks_since(0)[-1].rule == "commit"
    guard.open_login("reddit.com")
    [route] = await send(guard, ctx, gate, login_post())
    assert route.continued == 1 and gate.seen == []


async def test_a_login_with_no_op_on_a_subdomain_of_the_entrys_domain_goes(tmp_path):
    guard, ctx, _, gate = driven(tmp_path)
    guard.open_login("facebook.com")
    routes = await send(guard, ctx, gate, login_post(FB_LOGIN), login_post("https://m.facebook.com/login/"))
    assert [r.continued for r in routes] == [1, 1] and gate.seen == []


async def test_a_strict_ops_read_that_falls_to_unknown_goes_inside_the_window(tmp_path):
    guard, ctx, _, gate = driven(tmp_path)
    request = FakeRequest(REDDIT_GQL, "POST", headers=JSON, body=json.dumps({"operation": "SignInStep"}).encode())
    [held] = await send(guard, ctx, gate, request)
    assert held.aborted and gate.seen == ["reddit.unknown"]
    guard.open_login("reddit.com")
    [route] = await send(guard, ctx, gate, request)
    assert route.continued == 1 and gate.seen == ["reddit.unknown"]
    assert [r["decision"] for r in trace_rows(tmp_path) if r["kind"] == "gate_classified"][-1] == "login"


async def test_a_promoted_read_whose_body_is_not_covered_goes_inside_the_window(tmp_path):
    guard, ctx, _, gate = driven(tmp_path)
    guard.rules = (EffectRule("reddit.read.shape", "reddit", "www.reddit.com", r"/svc/shreddit/account/login$",
                              effect="read", fields=frozenset({"other"})), *guard.rules)
    [held] = await send(guard, ctx, gate, login_post())
    assert held.aborted
    guard.open_login("reddit.com")
    [route] = await send(guard, ctx, gate, login_post())
    assert route.continued == 1


# ------------------------------------------------------------------ the attacks

@pytest.mark.parametrize("url,seen", [
    (FB_LOGIN, "facebook.unknown"),
    ("https://www.instagram.com/api/v1/web/accounts/login/ajax/", "instagram.unknown"),
])
async def test_a_window_does_not_cover_another_domain(tmp_path, url, seen):
    guard, ctx, _, gate = driven(tmp_path)
    guard.open_login("reddit.com")
    [route] = await send(guard, ctx, gate, login_post(url))
    assert route.aborted and gate.seen == [seen]


@pytest.mark.parametrize("url,body,rule", [
    ("https://www.reddit.com/svc/shreddit/t1_abc123/create-comment", b"content=x", "reddit.comment"),
    (REDDIT_GQL, json.dumps({"operation": "CreatePost"}).encode(), "reddit.post"),
    ("https://old.reddit.com/api/submit", b"title=x", "reddit.post.oldreddit"),
])
async def test_a_named_commit_rule_is_gated_inside_a_window(tmp_path, url, body, rule):
    guard, ctx, _, gate = driven(tmp_path)
    guard.open_login("reddit.com")
    headers = JSON if body.startswith(b"{") else FORM  # a body the window can read, so only the rule stops it
    [route] = await send(guard, ctx, gate, FakeRequest(url, "POST", headers=headers, body=body))
    assert route.aborted and gate.seen == [rule]


async def test_a_named_write_rule_is_judged_inside_a_window(tmp_path):
    guard, ctx, _, gate = driven(tmp_path, driver=Driver(kind="agent", id="w", grant_id="g", ceiling="read"))
    guard.open_login("reddit.com")
    guard.rules = (EffectRule("reddit.draft.x", "reddit", "www.reddit.com", r"/write$", effect="write"), *guard.rules)
    [route] = await send(guard, ctx, gate, FakeRequest("https://www.reddit.com/write", "POST", headers=FORM, body=b"a=1"))
    assert route.aborted and guard.blocks_since(0)[-1].rule == "write"


@pytest.mark.parametrize("site,url,headers,body,rule", [
    ("reddit.com", REDDIT_GQL, JSON,
     json.dumps([{"operation": "ExposeVariant"}, {"operation": "CreatePost"}]).encode(), "reddit.unknown"),
    ("facebook.com", FB_GQL, FORM,
     b"queries=" + quote(json.dumps({"q0": {"doc_id": "1", "query_name": "CometFooQuery"},
                                     "q1": {"doc_id": "2", "query_name": "ComposerStoryCreateMutation"}})).encode(),
     "facebook.unknown"),
])
async def test_a_batch_cannot_hide_a_named_commit_inside_a_window(tmp_path, site, url, headers, body, rule):
    """classify reads one operation: the commit in a batch is only in the window's own check."""
    guard, ctx, _, gate = driven(tmp_path)
    guard.open_login(site)
    [route] = await send(guard, ctx, gate, FakeRequest(url, "POST", headers=headers, body=body))
    assert route.aborted and gate.seen == [rule]


async def test_a_body_that_cannot_be_read_is_gated_on_a_graphql_endpoint_only(tmp_path):
    guard, ctx, _, gate = driven(tmp_path)
    guard.open_login("reddit.com")
    multipart = {"content-type": "multipart/form-data; boundary=x"}
    routes = await send(guard, ctx, gate, FakeRequest(REDDIT_GQL, "POST", headers=multipart, body=b"--x--"),
                        FakeRequest(REDDIT_LOGIN, "POST", headers=multipart, body=b"--x--"))
    assert routes[0].aborted and routes[1].continued == 1


async def test_a_window_expires_and_each_fill_renews_it(tmp_path):
    guard, ctx, clock, gate = driven(tmp_path)
    guard.open_login("reddit.com")
    clock.now += LOGIN_WINDOW_S - 1
    guard.open_login("reddit.com")  # the next step of a login of several steps
    clock.now += LOGIN_WINDOW_S - 1
    [inside] = await send(guard, ctx, gate, login_post())
    assert inside.continued == 1
    clock.now += 1
    [after] = await send(guard, ctx, gate, login_post())
    assert after.aborted and gate.seen == ["reddit.unknown"] and not guard._login_until


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("url", [REDDIT_LOGIN, REDDIT_GQL])
async def test_a_put_patch_or_delete_is_gated_inside_a_window(tmp_path, method, url):
    guard, ctx, _, gate = driven(tmp_path)
    guard.open_login("reddit.com")
    [route] = await send(guard, ctx, gate, login_post(url, method))
    assert route.aborted and gate.seen == ["reddit.unknown"]


async def test_another_lane_is_unaffected(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    guard, _, _, _ = driven(tmp_path / "a")
    other, ctx, _, gate = driven(tmp_path / "b")
    guard.open_login("reddit.com")
    [route] = await send(other, ctx, gate, login_post())
    assert route.aborted and gate.seen == ["reddit.unknown"]


async def test_the_lane_and_grant_lists_still_hold_inside_a_window(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, sites=("www.instagram.com",), driver=COMMITTER)
    guard.open_login("reddit.com")
    [route] = await send(guard, ctx, Gate(), login_post())
    assert route.aborted and guard.blocks_since(0)[-1].rule == "non_get"


async def test_a_person_taking_the_lane_closes_the_window(tmp_path):
    guard, ctx, _, gate = driven(tmp_path)
    guard.open_login("reddit.com")
    guard.drive(Driver(kind="human", id="owner"))
    guard.drive(COMMITTER)
    [route] = await send(guard, ctx, gate, login_post())
    assert route.aborted and gate.seen == ["reddit.unknown"]


async def test_the_end_of_the_drive_closes_the_window_and_it_survives_the_first_act(tmp_path):
    guard, ctx, _, gate = driven(tmp_path)
    guard.open_login("reddit.com")  # filled on a lane no act has driven yet: the click starts the drive
    [route] = await send(guard, ctx, gate, login_post())
    assert route.continued == 1
    guard.end_drive("the claim ended")
    [held] = await send(guard, ctx, gate, login_post())
    assert held.aborted


async def test_a_keepalive_visit_gets_no_window(tmp_path):
    guard, ctx, _, _ = driven(tmp_path, driver=Driver(kind="agent", id="keepalive", ceiling="read"))
    guard.open_login("reddit.com")
    async with guard.acting(effects_route=True):
        [(_, handler)] = ctx.routes
        route = FakeRoute()
        await handler(route, login_post())
    assert route.aborted


def test_only_the_typist_and_the_vault_can_open_a_window():
    """Not a page, a tool or a model: the guard's method has one caller, and that one has one caller."""
    root = Path(__file__).resolve().parents[2]
    # a plain search, not git grep: an exported tree that is not a git repository yet runs it too
    found = [str(path.relative_to(root)) for path in (root / "src" / "openajar").rglob("*.py")
             if "open_login" in path.read_text(encoding="utf-8")]
    assert sorted(found) == ["src/openajar/node/egress/__init__.py", "src/openajar/node/egress/guard.py",
                             "src/openajar/node/egress/guard_login.py", "src/openajar/node/egress/rules.py", "src/openajar/node/typist.py",
                             "src/openajar/vault/login.py",
                             "src/openajar/vault/typist.py"]  # the guard (its window's length), its adapter, the fill, the protocol's docs
    assert "guard.open_login(domain)" in inspect.getsource(PlaywrightTypist.open_login)


@pytest.mark.parametrize("tool,args", [(tools.login, {"ctx", "on", "entry"}),
                                       (tools.vault_fill, {"ctx", "on", "field", "entry"})])
def test_no_tool_argument_names_a_domain_or_a_window(tool, args):
    assert set(inspect.signature(tool).parameters) == args


async def test_the_typist_tells_the_guard_of_its_own_page_and_only_that(tmp_path):
    guard, _, _, _ = driven(tmp_path)
    other = LaneGuard("biz-a/other", trace=EgressTrace(tmp_path / "other.jsonl"))
    context = FakeContext()
    other.attach(context, RouteDispatcher(context))
    page = FakePage()
    page.context = guard._context
    typist = PlaywrightTypist(page)
    await typist.open_login("reddit.com")
    assert set(guard._login_until) == {"reddit.com"} and not other._login_until
    await typist.close_login("reddit.com")
    assert not guard._login_until
    bare = FakePage()
    bare.context = object()
    await PlaywrightTypist(bare).open_login("reddit.com")  # a browser with no guard: nothing to tell


# ------------------------------------------------------------------ who opens it: the vault's fill

class WiredTypist(FakeTypist):
    """The scripted typist, telling a real guard what PlaywrightTypist tells it."""

    def __init__(self, guard, *a, **kw):
        super().__init__(*a, **kw)
        self.guard = guard
        self.open_while_typing: list[bool] = []

    async def open_login(self, domain):
        self.guard.open_login(domain)

    async def close_login(self, domain):
        self.guard.close_login(domain)

    async def type_secret(self, f, text):
        self.open_while_typing.append(bool(self.guard._login_until))
        await super().type_secret(f, text)


@pytest.fixture
async def vault(tmp_path):
    v = make_vault(private_home(tmp_path))
    await v.add_entry(name="ig", kind="password", domain="instagram.com", lanes=[LANE], username=USERNAME,
                      secret=PASSWORD)
    await v.add_entry(name="fb", kind="password", domain="facebook.com", lanes=[LANE], username=USERNAME,
                      secret=PASSWORD)
    await v.add_entry(name="elsewhere", kind="password", domain="instagram.com", lanes=["biz-b/instagram"],
                      username=USERNAME, secret=PASSWORD)
    return v


def site(guard, **kw) -> WiredTypist:
    form = kw.pop("form", None) or login_form(IG)
    url = kw.pop("url", IG + "/accounts/login/")
    refs = {"e1": "pass", "e2": "user", "e3": "search"}
    return WiredTypist(guard, {"login": Page(url, form, refs=refs, on_enter="home"), "home": Page(IG + "/", [])}, "login")


IG_POST = IG + "/api/v1/web/accounts/login/ajax/"


async def test_the_login_tool_flow_opens_the_window_for_the_entrys_domain(tmp_path, vault):
    guard, ctx, _, gate = driven(tmp_path)
    t = site(guard)
    result = await login_flow(FakeCtx(), vault, t, lane_id=LANE)
    assert result["ok"] and set(guard._login_until) == {"instagram.com"}
    assert t.open_while_typing == [True, True]  # before the first key: a code field can submit itself
    [route] = await send(guard, ctx, gate, login_post(IG_POST))
    assert route.continued == 1 and gate.seen == []


async def test_vault_fill_then_a_click_is_a_login(tmp_path, vault):
    """The pilot's way: vault_fill, then a click through browser_act."""
    guard, ctx, _, gate = driven(tmp_path)
    t = site(guard)
    assert (await fill_flow(FakeCtx(), vault, t, lane_id=LANE, field_ref="e1", entry="ig"))["ok"]
    [route] = await send(guard, ctx, gate, login_post(IG_POST))
    assert route.continued == 1 and gate.seen == []


async def test_a_page_that_is_not_the_entrys_opens_nothing(tmp_path, vault):
    guard, _, _, _ = driven(tmp_path)
    evil = "https://www.instagram.com.evil.example"
    t = site(guard, url=evil + "/login", form=login_form(evil))
    with pytest.raises(ResolutionDenied):
        await fill_flow(FakeCtx(), vault, t, lane_id=LANE, field_ref="e1", entry="ig")
    assert not guard._login_until and not t.typed


@pytest.mark.parametrize("entry,field", [
    ("fb", "e1"),         # a model names another site's entry: the page is not on its domain
    ("elsewhere", "e1"),  # an entry of another lane
    ("ig", "e3"),         # a field that is no login field
])
async def test_a_refused_fill_opens_nothing_whatever_a_model_names(tmp_path, vault, entry, field):
    guard, _, _, _ = driven(tmp_path)
    t = site(guard)
    with pytest.raises(ResolutionDenied):
        await fill_flow(FakeCtx(), vault, t, lane_id=LANE, field_ref=field, entry=entry)
    assert not guard._login_until and not t.typed


async def test_a_field_in_a_foreign_frame_opens_nothing(tmp_path, vault):
    guard, _, _, _ = driven(tmp_path)
    form = [fld("pass", IG, chain=(IG, "https://evil.example"), type="password", form="f1")]
    t = site(guard, form=form)
    with pytest.raises(ResolutionDenied):
        await fill_flow(FakeCtx(), vault, t, lane_id=LANE, field_ref="e1", entry="ig")
    assert not guard._login_until


async def test_a_fill_that_fails_while_typing_leaves_no_window(tmp_path, vault):
    guard, ctx, _, gate = driven(tmp_path)
    guard.open_login("instagram.com")  # an earlier fill's window goes with it
    t = site(guard)
    t.abort_on.add("pass")
    result = await fill_flow(FakeCtx(), vault, t, lane_id=LANE, field_ref="e1", entry="ig")
    assert not result["ok"] and result["needs"] == "human" and not guard._login_until
    [route] = await send(guard, ctx, gate, login_post(IG_POST))
    assert route.aborted


async def test_a_secret_too_long_to_type_opens_nothing(tmp_path, vault):
    await vault.add_entry(name="long", kind="password", domain="instagram.com", lanes=[LANE], username=USERNAME,
                          secret="x" * (TYPE_MAX + 1))
    guard, _, _, _ = driven(tmp_path)
    with pytest.raises(SecretTooLong):
        await fill_flow(FakeCtx(), vault, site(guard), lane_id=LANE, field_ref="e1", entry="long")
    assert not guard._login_until


async def test_a_grant_that_is_refused_opens_nothing(tmp_path, vault):
    guard, _, _, _ = driven(tmp_path)
    ctx = FakeCtx(deny=ApprovalPending("a1", "no"))
    with pytest.raises(ApprovalPending):
        await fill_flow(ctx, vault, site(guard), lane_id=LANE, field_ref="e1", entry="ig")
    assert not guard._login_until


async def test_a_typist_with_no_guard_to_tell_still_logs_in(tmp_path, vault):
    t = FakeTypist({"login": Page(IG + "/accounts/login/", login_form(IG), on_enter="home"),
                    "home": Page(IG + "/", [])}, "login")
    assert (await login_flow(FakeCtx(), vault, t, lane_id=LANE))["ok"]


def test_the_window_is_about_thirty_seconds():
    assert LOGIN_WINDOW_S == 30 and LaneGuard("biz-a/x").login_window_s == 30
    assert egress.LOGIN_WINDOW_S == LOGIN_WINDOW_S




# ------------------------------------------------------------------ end to end, on the real policy

@pytest.fixture
def policy(tmp_path):
    svc, _ = make_service(tmp_path / "policy", clock=PolicyClock())
    yield svc
    svc.close()


def tainted_ctx(svc, tool: str, **kw):
    """An agent's call in a context that read the login page, as every login's has."""
    kw.setdefault("specials", Specials(secret=LaneCap()))
    _, token = svc.issue(subject={"id": "agent-1"}, workspaces=["biz-a"], ceiling=Effect.DRAFT, **kw)
    ctx = svc.context_for_token(token, tool=tool)
    ctx.taint("untrusted", source="browser_snapshot", kind="web_page", host="www.instagram.com")
    return ctx


async def test_a_login_runs_through_with_nobody_asked_after_reading_the_page(tmp_path, vault, policy):
    """login, a draft grant, a tainted context, a drive already on: nothing asks and nothing refuses."""
    guard, ctx, _, gate = driven(tmp_path, driver=DRAFT)
    result = await login_flow(tainted_ctx(policy, "login"), vault, site(guard), lane_id=LANE)
    assert result["ok"]
    [route] = await send(guard, ctx, gate, login_post(IG_POST))
    assert route.continued == 1 and gate.seen == [] and policy.approvals.list(state="pending") == []


async def test_a_grant_without_secret_is_refused_and_opens_nothing(tmp_path, vault, policy):
    guard, _, _, _ = driven(tmp_path)
    ctx = tainted_ctx(policy, "vault_fill", specials=Specials())
    with pytest.raises(Denied) as e:
        await fill_flow(ctx, vault, site(guard), lane_id=LANE, field_ref="e1", entry="ig")
    assert e.value.reason == "special" and not guard._login_until
