"""The learn window (LaneGuard.open_learn, agentos.node.egress.learn; docs/design/learn-a-site.md
section 4), the reviewed edit pages and endpoints (agentos.node.sites), and a learned operation's
strict commit scope (FlowCommit.strict). Fake requests and contexts (test_egress). The adversarial
review's probes are here as regressions (a DM, another field's rule, a piggybacked edit, the value
in another field, a window with no grant)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from test_commit_gate_guard import Gate
from test_egress import COMMITTER, FakeMeter, FakePage, FakeRequest, FakeRoute, make_guard

from agentos.core.errors import InvalidRequest
from agentos.core.models import AccountRef
from agentos.node import egress
from agentos.node.egress import learn
from agentos.node.egress.learn_window import others_changed, path_under
from agentos.node.egress.verdicts import Driver
from agentos.node.learned_rules import LearnedRules
from agentos.node.sites import PROFILE_EDIT_ENDPOINTS, profile_edit_endpoint, profile_edit_paths

EDIT = "https://www.instagram.com/api/v1/web/accounts/edit/"
IG_GQL = "https://www.instagram.com/graphql/query"
EDIT_PAGE = "https://www.instagram.com/accounts/edit/"
FORM_DATA_URL = "https://www.instagram.com/api/v1/accounts/edit/web_form_data/"
FORM = {"content-type": "application/x-www-form-urlencoded"}
BIO = "Coffee roaster in Vilnius since 2019"
PIN = AccountRef(site="instagram", user_id="111")
LI_PIN = AccountRef(site="linkedin", user_id="acme-slug")
LI_RSC = "https://www.linkedin.com/flagship-web/rsc-action/actions/server-request"
# what web_form_data said the form holds now
CURRENT = {"biography": "Old bio", "chaining_enabled": True, "email": "owner@acme.test",
           "external_url": "https://acme.test", "first_name": "Acme", "phone_number": "", "username": "acme",
           "is_email_confirmed": True}
OTHER = Driver(kind="agent", id="other", grant_id="g_other", lists=None, ceiling="commit")


def form(**changes) -> bytes:
    """The edit form's post: every field of the form, as the page sends it, with `changes`."""
    fields = {"biography": CURRENT["biography"], "chaining_enabled": "on", "email": CURRENT["email"],
              "external_url": CURRENT["external_url"], "first_name": CURRENT["first_name"],
              "phone_number": "", "username": CURRENT["username"]}
    fields.update(changes)
    return urlencode({k: v for k, v in fields.items() if v is not None}).encode()


def bio_body(value: str = BIO) -> bytes:
    return form(biography=value)


def ig(op: str, **variables) -> bytes:
    return urlencode({"fb_api_req_friendly_name": op, "variables": json.dumps(variables)}).encode()


def signed_in(user_id: str | None = "111"):
    async def identity(site: str):
        return {"site": site, "user_id": user_id} if user_id else None
    return identity


class PressGate(Gate):
    """The act's gate during a press (ActGate: submit_like and active, keyed by its call)."""

    def __init__(self, *, submit_like: bool = True, active: bool = True):
        super().__init__()
        self.submit_like, self.active = submit_like, active
        self.key = ("g2", "biz-a/instagram", "callhash1")


def seed(guard, values=CURRENT) -> None:
    ep = profile_edit_endpoint("instagram", "bio")
    guard._form_data[(ep.form_data[0], ep.form_data[1])] = dict(values)


def setup(tmp_path, *, typed: str | None = BIO, driver=COMMITTER, baseline=CURRENT, **window):
    guard, ctx, _, clock = make_guard(tmp_path, sites=(), driver=driver)
    if baseline is not None:
        seed(guard, baseline)
    kw = {"field": "bio", "value": BIO, "path_prefixes": ["/accounts/edit/"], "pinned": lambda: PIN,
          "site": "instagram", "grant_id": "g2", "identity": signed_in()}
    kw.update(window)
    wid = guard.open_learn(**kw)
    if typed:
        guard.note_typed(typed)
    return guard, ctx, clock, wid


async def send(guard, ctx, gate, *, url=EDIT, body=None, page=EDIT_PAGE, headers=None):
    page_ = FakePage(page)
    request = FakeRequest(url, "POST", frame=page_.main_frame, headers=headers or FORM,
                          body=bio_body() if body is None else body)
    route = FakeRoute()
    _, handler = ctx.routes[-1]
    await handler(route, request)
    return route


def released(guard, wid) -> bool:
    return guard.learn_result(wid) is not None


async def test_it_releases_exactly_one_request_and_says_which_act_sent_it(tmp_path):
    guard, ctx, _, wid = setup(tmp_path)
    gate = PressGate()
    async with guard.acting(gate=gate):
        first = await send(guard, ctx, gate)
        assert not first.aborted and gate.seen == []           # let go, and nobody was asked
        second = await send(guard, ctx, gate)                    # the same request again: the gate holds it
        assert second.aborted and gate.seen == ["instagram.unknown"]
    result = guard.learn_result(wid)
    assert set(result) == {"host", "path", "method", "op", "url", "page_url", "at", "act_seq", "act_at", "act_call"}
    assert (result["host"], result["path"], result["method"]) == ("www.instagram.com", "/api/v1/web/accounts/edit/",
                                                                   "POST")
    assert result["page_url"] == EDIT_PAGE and result["url"] == EDIT
    assert result["act_seq"] == 1 and result["act_at"] and result["act_call"] == "callhash1"
    rows = [r for r in guard.trace.read(limit=100) if r.get("kind") == "learn_commit"]
    assert len(rows) == 1 and rows[0]["field"] == "bio" and rows[0]["act_seq"] == 1
    assert BIO not in json.dumps(rows) and "owner@acme.test" not in json.dumps(rows)   # names only
    assert guard.learn_window(wid) is None                       # used up: closed
    guard.close_learn(wid)
    assert guard.learn_result(wid) is None


async def test_the_act_sequence_counts_the_agents_acts(tmp_path):
    guard, ctx, _, wid = setup(tmp_path)
    gate = PressGate()
    async with guard.acting(gate=PressGate(submit_like=False)):  # a click elsewhere first
        pass
    async with guard.acting(gate=gate):
        assert not (await send(guard, ctx, gate)).aborted
    assert guard.learn_result(wid)["act_seq"] == 2


async def test_another_fields_learned_rule_of_the_same_form_is_released(tmp_path):
    """Bio and website are saved by one form post: a learned website rule classifies the bio's save too."""
    store = LearnedRules(tmp_path)
    store.add("instagram", "website", host="www.instagram.com", path="/api/v1/web/accounts/edit/", method="POST",
              op=None)
    guard, ctx, _, wid = setup(tmp_path)
    guard.learned_rules = lambda: tuple(store.list())
    guard.rules = guard._base_rules
    gate = PressGate()
    async with guard.acting(gate=gate):
        assert not (await send(guard, ctx, gate)).aborted
    assert released(guard, wid)


async def test_ask_me_first_on_the_lane_refuses_the_lessons_save_and_closes_the_window(tmp_path):
    """A lesson's save never asks for a card, so a lane that asks first never lets it go."""
    guard, ctx, _, wid = setup(tmp_path)
    guard.asks_first = lambda: True
    gate, meter = PressGate(), FakeMeter()
    async with guard.acting(gate=gate, meter=meter) as mark:
        route = await send(guard, ctx, gate)
    assert route.aborted and gate.seen == [] and meter.charged == []     # refused: nobody asked, nothing charged
    assert not released(guard, wid) and guard.learn_window(wid) is None
    [block] = guard.blocks_since(mark)
    assert block.rule == "ask_first" and "Ask me first is on" in block.reason and "\u2014" not in block.reason


async def test_ask_me_first_that_cannot_be_read_refuses_too(tmp_path):
    guard, ctx, _, wid = setup(tmp_path)

    def unreadable():
        raise RuntimeError("store down")

    guard.asks_first = unreadable
    gate = PressGate()
    async with guard.acting(gate=gate):
        assert (await send(guard, ctx, gate)).aborted
    assert not released(guard, wid)


async def test_the_lessons_save_is_charged_as_one_commit(tmp_path):
    guard, ctx, _, wid = setup(tmp_path)
    gate, meter = PressGate(), FakeMeter()
    async with guard.acting(gate=gate, meter=meter):
        assert not (await send(guard, ctx, gate)).aborted
    assert released(guard, wid) and meter.charged == [("instagram.unknown", False)]


async def test_a_lessons_save_past_a_limit_or_the_lanes_pace_is_refused(tmp_path):
    guard, ctx, _, wid = setup(tmp_path)
    gate = PressGate()
    meter = FakeMeter(over="the lane's posting pace allows one more at 14:00")
    meter.code = "lane_paced"
    async with guard.acting(gate=gate, meter=meter) as mark:
        route = await send(guard, ctx, gate)
    assert route.aborted and gate.seen == [] and not released(guard, wid)
    [block] = guard.blocks_since(mark)
    assert block.rule == "lane_paced" and "posting pace" in block.reason


# ---- only the field's reviewed endpoint, and only the field changes ---------------------------

async def test_piggybacked_changes_are_refused_and_close_the_window(tmp_path):
    """The review's probe: the save also changes the username, the email, the name and the phone."""
    guard, ctx, _, wid = setup(tmp_path)
    gate = PressGate()
    body = form(biography=BIO, first_name="Attacker", username="attacker_new", email="evil@x.com",
                phone_number="+1555")
    async with guard.acting(gate=gate):
        assert (await send(guard, ctx, gate, body=body)).aborted
    assert not released(guard, wid) and guard.learn_window(wid) is None
    assert gate.seen == ["instagram.unknown"]


@pytest.mark.parametrize("changes", [
    {"username": "acme2"}, {"email": "x@y.z"}, {"external_url": "https://evil.example"},
    {"chaining_enabled": None},                  # the checkbox turned off
    {"gender": "1"},                             # a field the form did not have
])
async def test_any_other_field_changed_closes_the_window(tmp_path, changes):
    guard, ctx, _, wid = setup(tmp_path)
    gate = PressGate()
    async with guard.acting(gate=gate):
        assert (await send(guard, ctx, gate, body=form(biography=BIO, **changes))).aborted
    assert not released(guard, wid) and guard.learn_window(wid) is None


async def test_the_value_in_another_field_is_not_the_field(tmp_path):
    """The review's probe: the value sits in first_name, the bio is unchanged."""
    guard, ctx, _, wid = setup(tmp_path)
    gate = PressGate()
    async with guard.acting(gate=gate):
        assert (await send(guard, ctx, gate, body=form(biography="old", first_name=BIO))).aborted
    assert not released(guard, wid)


@pytest.mark.parametrize("value", [BIO + "!", BIO[:-3], " " + BIO, "something else"])
async def test_only_exactly_the_value_in_the_field(tmp_path, value):
    guard, ctx, _, wid = setup(tmp_path)
    gate = PressGate()
    async with guard.acting(gate=gate):
        assert (await send(guard, ctx, gate, body=bio_body(value))).aborted
    assert not released(guard, wid)


async def test_the_value_twice_in_the_field_is_refused(tmp_path):
    guard, ctx, _, wid = setup(tmp_path)
    gate = PressGate()
    async with guard.acting(gate=gate):
        assert (await send(guard, ctx, gate, body=bio_body() + b"&biography=" + BIO.encode())).aborted
    assert not released(guard, wid)


async def test_without_the_forms_current_values_nothing_is_released(tmp_path):
    guard, ctx, _, wid = setup(tmp_path, baseline=None)
    gate = PressGate()
    async with guard.acting(gate=gate):
        assert (await send(guard, ctx, gate)).aborted
    assert not released(guard, wid) and guard.learn_window(wid) is not None   # still open: the page may load them


async def test_the_guard_keeps_the_forms_current_values_from_the_edit_page(tmp_path):
    guard, ctx, _, wid = setup(tmp_path, baseline=None)

    class Response:
        async def text(self):
            return json.dumps({"form_data": CURRENT, "status": "ok"})

    get = FakeRequest(FORM_DATA_URL, "GET")

    async def response():
        return Response()

    get.response = response
    ctx.emit("requestfinished", get)
    import asyncio

    for _ in range(5):
        await asyncio.sleep(0)
    gate = PressGate()
    async with guard.acting(gate=gate):
        assert not (await send(guard, ctx, gate)).aborted
    assert released(guard, wid)


def test_other_fields_compare_with_the_forms_types():
    assert others_changed(form(biography=BIO), "form:biography", CURRENT) == []
    fields = profile_edit_endpoint("instagram", "bio").fields
    assert others_changed(form(biography=BIO), "form:biography", CURRENT, fields) == []
    # an unchecked checkbox is left out of the post: off, so changed from on
    assert others_changed(form(biography=BIO, chaining_enabled=None), "form:biography", CURRENT, fields) \
        == ["chaining_enabled"]
    assert others_changed(form(biography=BIO, gender="1"), "form:biography", CURRENT, fields) == ["gender"]
    assert others_changed(form(biography=BIO, chaining_enabled="off"), "form:biography", CURRENT) == ["chaining_enabled"]
    assert others_changed(form(biography=BIO), "form:biography", None) is None


@pytest.mark.parametrize("url", [
    "https://www.instagram.com/api/v1/web/accounts/set_private/",   # another endpoint
    "https://i.instagram.com/api/v1/web/accounts/edit/",            # another host
    "https://www.instagram.com/api/v1/web/accounts/edit/x/",        # a longer path
])
async def test_only_the_reviewed_endpoint(tmp_path, url):
    guard, ctx, _, wid = setup(tmp_path)
    gate = PressGate()
    async with guard.acting(gate=gate):
        assert (await send(guard, ctx, gate, url=url)).aborted
    assert not released(guard, wid)


@pytest.mark.parametrize(("op", "rule"), [
    ("PolarisPostCommentInputRevampedMutation", "instagram.comment"),
    ("usePolarisLikeMediaXIGLikeMutation", "instagram.like"),
    ("usePolarisFollowMutation", "instagram.follow"),
])
async def test_a_post_comment_like_or_follow_is_never_released_even_with_the_value(tmp_path, op, rule):
    guard, ctx, _, wid = setup(tmp_path)
    gate = PressGate()
    async with guard.acting(gate=gate):
        route = await send(guard, ctx, gate, url=IG_GQL, body=ig(op, data={"comment_text": BIO}))
    assert not released(guard, wid)
    assert not [r for r in guard.trace.read(limit=100) if r.get("kind") == "learn_commit"]
    if rule == "instagram.comment":   # a commit: the gate holds it for a person, as without a window
        assert route.aborted and gate.seen == [rule]
    else:   # a like or a follow is a write: the gate never holds one (the grant's ceiling judges it)
        assert gate.seen == []


async def test_a_publish_rule_with_the_value_is_never_released(tmp_path):
    guard, ctx, _, wid = setup(tmp_path)
    gate = PressGate()
    async with guard.acting(gate=gate):
        route = await send(guard, ctx, gate, url="https://i.instagram.com/api/v1/media/configure/")
    assert route.aborted and gate.seen == ["instagram.post"] and not released(guard, wid)


def test_fields_and_sites_with_no_reviewed_endpoint_get_no_window(tmp_path):
    """The review's probes: a LinkedIn DM or a LinkedIn name change under a location window. LinkedIn
    has no reviewed endpoint, so no LinkedIn field opens a window at all."""
    guard, _, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    for site, field in (("linkedin", "location"), ("linkedin", "headline"), ("instagram", "name"),
                        ("facebook", "bio"), ("medium", "bio"), ("reddit", "about")):
        with pytest.raises(ValueError, match="no reviewed edit endpoint"):
            guard.open_learn(field=field, value=BIO, path_prefixes=["/in/acme-slug/"], site=site, grant_id="g2")
    assert set(PROFILE_EDIT_ENDPOINTS) == {"instagram"}


async def test_a_linkedin_request_never_matches_a_reviewed_endpoint(tmp_path, monkeypatch):
    """Even with a reviewed LinkedIn headline entry, a name change or a DM on the same window is refused."""
    from agentos.node import sites

    monkeypatch.setitem(sites.PROFILE_EDIT_ENDPOINTS, "linkedin", {"headline": sites.EditEndpoint(
        "POST", "www.linkedin.com", r"/flagship-web/rsc-action/actions/server-request", "json:serverRequest.headline",
        op="com.linkedin.sdui.requests.profile.updateHeadline")})
    guard, ctx, _, wid = setup(tmp_path, field="headline", site="linkedin", path_prefixes=["/in/acme-slug/"],
                               pinned=lambda: LI_PIN, identity=signed_in("acme-slug"))
    gate = PressGate()
    name = json.dumps({"requestId": "com.linkedin.sdui.requests.profile.updateProfileName",
                       "serverRequest": {"headline": BIO, "firstName": BIO}}).encode()
    dm = json.dumps({"message": {"body": {"text": BIO}}, "hostRecipientUrns": ["urn:li:fsd_profile:VICTIM"]}).encode()
    js = {"content-type": "application/json"}
    async with guard.acting(gate=gate):
        assert (await send(guard, ctx, gate, url=LI_RSC, body=name, page="https://www.linkedin.com/in/acme-slug/",
                           headers=js)).aborted
        assert (await send(guard, ctx, gate, body=dm, page="https://www.linkedin.com/in/some-stranger/", headers=js,
                           url="https://www.linkedin.com/voyager/api/voyagerMessagingDashMessengerMessages")).aborted
    assert not released(guard, wid)


# ---- the edit page names the pinned account ---------------------------------------------------

def test_the_reviewed_edit_pages_name_the_pinned_account():
    assert profile_edit_paths("instagram") == ["/accounts/edit/"] == profile_edit_paths("ig", "111")
    assert profile_edit_paths("linkedin", "acme-slug") == ["/in/acme-slug/"]
    assert profile_edit_paths("linkedin") == [] and profile_edit_paths("facebook") == []
    assert profile_edit_paths("facebook", "100012") == ["/profile.php?id=100012"]
    assert profile_edit_paths("linkedin", "../x") == []
    assert profile_edit_paths("reddit") == ["/settings/profile"] and profile_edit_paths("medium") == ["/me/settings"]
    assert profile_edit_paths("x") == [] and profile_edit_paths("nowhere") == []
    li = ("/in/acme-slug/",)
    assert path_under("https://www.linkedin.com/in/acme-slug/edit/intro/", li)
    assert not path_under("https://www.linkedin.com/in/some-stranger/", li)
    assert not path_under("https://www.linkedin.com/in/acme-slug-2/", li)
    fb = ("/profile.php?id=100012",)
    assert path_under("https://www.facebook.com/profile.php?id=100012&sk=about", fb)
    for url in ("https://www.facebook.com/profile.php?id=999", "https://www.facebook.com/profile.php",
                "https://www.facebook.com/profile.php?id=999&id=100012", "https://www.facebook.com/settings/",
                "https://www.facebook.com/me/"):
        assert not path_under(url, fb), url
    assert path_under("https://www.reddit.com/settings/profile/x?y=1", ("/settings/profile",))
    assert not path_under("https://www.reddit.com/settings/profiles", ("/settings/profile",))


@pytest.mark.parametrize("page", [
    "https://www.instagram.com/acme/",                     # the profile, not its edit page
    "https://www.instagram.com/p/abc123/",                 # a post's page
    "https://www.instagram.com/accounts/editor/",          # a look-alike path
    "https://evil.example/accounts/edit/",                 # another site
])
async def test_the_page_must_be_a_reviewed_edit_page_of_the_site(tmp_path, page):
    guard, ctx, _, wid = setup(tmp_path)
    gate = PressGate()
    async with guard.acting(gate=gate):
        assert (await send(guard, ctx, gate, page=page)).aborted
    assert not released(guard, wid)


# ---- what the agent did in the window ---------------------------------------------------------

@pytest.mark.parametrize("typing", [
    [],                                          # nothing typed since the window opened
    ["something else"],                          # another text
    [BIO, "more"],                               # a second text
    [BIO, BIO],
])
async def test_exactly_one_text_equal_to_the_value(tmp_path, typing):
    guard, ctx, _, wid = setup(tmp_path, typed=None)
    for text in typing:
        guard.note_typed(text)
    gate = PressGate()
    async with guard.acting(gate=gate):
        assert (await send(guard, ctx, gate)).aborted
    assert not released(guard, wid) and guard.learn_window(wid) is None


async def test_a_key_typed_one_character_at_a_time_closes_the_window(tmp_path):
    guard, _, _, wid = setup(tmp_path)
    guard.note_key("Enter")                     # not text: stays open
    assert guard.learn_window(wid) is not None
    guard.note_key("a")
    assert guard.learn_window(wid) is None


async def test_an_upload_closes_the_window(tmp_path):
    guard, ctx, _, wid = setup(tmp_path)
    ctx.emit("request", FakeRequest("https://i.instagram.com/rupload_igphoto/x", "POST", body=b"\x89PNG"))
    assert guard.learn_window(wid) is None


async def test_the_window_expires(tmp_path):
    guard, ctx, clock, wid = setup(tmp_path, ttl_s=60)
    clock.now += 61
    gate = PressGate()
    async with guard.acting(gate=gate):
        assert (await send(guard, ctx, gate)).aborted
    assert not released(guard, wid) and guard.learn_window(wid) is None


@pytest.mark.parametrize("window", [
    {"identity": signed_in("999")},             # wrong_account
    {"identity": signed_in(None)},              # the signed-in account cannot be read
    {"pinned": lambda: None},                   # unpinned
    {"pinned": lambda: AccountRef(site="instagram", handle="@acme")},   # pinned with no user id yet
    {"pinned": lambda: AccountRef(site="reddit", user_id="111")},       # pinned on another site
])
async def test_the_lane_must_be_signed_into_its_pinned_account(tmp_path, window):
    guard, ctx, _, wid = setup(tmp_path, **window)
    gate = PressGate()
    async with guard.acting(gate=gate):
        assert (await send(guard, ctx, gate)).aborted
    assert not released(guard, wid)


@pytest.mark.parametrize("gate_kw", [{"submit_like": False}, {"active": False}])
async def test_only_a_press_of_the_agents_act(tmp_path, gate_kw):
    guard, ctx, _, wid = setup(tmp_path)
    gate = PressGate(**gate_kw)
    async with guard.acting(gate=gate):
        assert (await send(guard, ctx, gate)).aborted
    assert not released(guard, wid)


# ---- bound to the agent that learns -------------------------------------------------------------

def test_a_window_needs_the_learning_agents_grant(tmp_path):
    """The review's probe: a window with no grant released any agent's save."""
    guard, _, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    for grant in (None, ""):
        with pytest.raises(ValueError, match="grant"):
            guard.open_learn(field="bio", value=BIO, path_prefixes=["/accounts/edit/"], site="instagram",
                             grant_id=grant)


async def test_another_drivers_act_closes_the_window(tmp_path):
    guard, ctx, _, wid = setup(tmp_path)
    guard.drive(OTHER)
    async with guard.acting(gate=PressGate()):
        pass
    assert guard.learn_window(wid) is None
    guard.drive(COMMITTER)
    gate = PressGate()
    async with guard.acting(gate=gate):
        assert (await send(guard, ctx, gate)).aborted
    assert not released(guard, wid)


async def test_a_window_opened_for_another_grant_releases_nothing(tmp_path):
    guard, ctx, _, wid = setup(tmp_path, grant_id="g_other")
    assert guard.learn_window(wid) is not None
    gate = PressGate()
    async with guard.acting(gate=gate):              # COMMITTER (g2) acts: the window closes
        assert (await send(guard, ctx, gate)).aborted
    assert not released(guard, wid) and guard.learn_window(wid) is None


async def test_a_person_taking_the_lane_closes_the_window(tmp_path):
    guard, _, _, wid = setup(tmp_path)
    guard.drive(Driver(kind="human", id="owner"))
    assert guard.learn_window(wid) is None


async def test_a_pilot_run_or_a_flow_gets_no_release(tmp_path):
    guard, ctx, _, wid = setup(tmp_path, excluded=lambda: "a pilot run drives the lane")
    gate = PressGate()
    async with guard.acting(gate=gate):
        assert (await send(guard, ctx, gate)).aborted
    assert not released(guard, wid)
    guard, ctx, _, wid = setup(tmp_path)
    with guard.routine():
        pass
    assert guard.learn_window(wid) is None


async def test_without_a_window_an_agents_edit_is_held_as_before(tmp_path):
    guard, ctx, _, wid = setup(tmp_path)
    guard.close_learn(wid)
    gate = PressGate()
    async with guard.acting(gate=gate):
        assert (await send(guard, ctx, gate)).aborted
    assert gate.seen == ["instagram.unknown"]


def test_opening_checks_its_arguments(tmp_path):
    guard, _, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    for bad in ({"field": "Bio"}, {"value": ""}, {"path_prefixes": "/accounts/edit/"}, {"path_prefixes": ["/"]},
                {"path_prefixes": []}, {"ttl_s": 0}, {"ttl_s": 7200}):
        kw = {"field": "bio", "value": BIO, "path_prefixes": ["/accounts/edit/"], "site": "instagram",
              "grant_id": "g2", **bad}
        with pytest.raises(ValueError):
            guard.open_learn(**kw)


# ---- the runtime helper ------------------------------------------------------------------------

class Lanes:
    def __init__(self, guard, account):
        self.guard, self.account = guard, account

    def get(self, lane_id):
        return SimpleNamespace(spec=SimpleNamespace(account=self.account))

    def live_session(self, lane_id):
        return SimpleNamespace(guard=self.guard) if self.guard is not None else None


class Runtime:
    def __init__(self, **services):
        self.services = services

    def get(self, name):
        return self.services.get(name)


async def test_the_runtime_helper_opens_reads_and_closes_a_lanes_window(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    seed(guard)
    rt = Runtime(lanes=Lanes(guard, PIN))
    wid = learn.open_learn(rt, "biz-a/instagram", field="bio", value=BIO, grant_id="g2")
    w = guard.learn_window(wid)
    assert w is not None and w.site == "instagram" and w.grant_id == "g2" and w.pinned() == PIN
    assert w.path_prefixes == ("/accounts/edit/",)
    w.identity = signed_in()
    guard.note_typed(BIO)
    assert learn.learn_result(rt, "biz-a/instagram", wid) is None
    gate = PressGate()
    async with guard.acting(gate=gate):
        assert not (await send(guard, ctx, gate)).aborted
    assert learn.learn_result(rt, "biz-a/instagram", wid)["act_seq"] == 1
    learn.close_learn(rt, "biz-a/instagram", wid)
    assert learn.learn_result(rt, "biz-a/instagram", wid) is None


def test_the_runtime_helper_refuses_what_may_not_learn(tmp_path):
    guard, _, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    args = {"field": "bio", "value": BIO, "grant_id": "g2"}
    with pytest.raises(InvalidRequest, match="grant"):
        learn.open_learn(Runtime(lanes=Lanes(guard, PIN)), "biz-a/instagram", **{**args, "grant_id": None})
    with pytest.raises(InvalidRequest, match="pins no account"):
        learn.open_learn(Runtime(lanes=Lanes(guard, None)), "biz-a/instagram", **args)
    with pytest.raises(InvalidRequest, match="pins no account"):
        learn.open_learn(Runtime(lanes=Lanes(guard, AccountRef(site="instagram"))), "biz-a/instagram", **args)
    with pytest.raises(InvalidRequest, match="no browser open"):
        learn.open_learn(Runtime(lanes=Lanes(None, PIN)), "biz-a/instagram", **args)
    pilot = SimpleNamespace(sessions={"run1": SimpleNamespace(request=SimpleNamespace(lane="biz-a/instagram"))})
    with pytest.raises(InvalidRequest, match="pilot run"):
        learn.open_learn(Runtime(lanes=Lanes(guard, PIN), pilot=pilot), "biz-a/instagram", **args)
    with pytest.raises(InvalidRequest, match="reviewed profile edit pages"):
        learn.open_learn(Runtime(lanes=Lanes(guard, PIN)), "biz-a/instagram", **{**args, "path_prefixes": ["/p/"]})
    with pytest.raises(InvalidRequest, match="not on linkedin"):
        learn.open_learn(Runtime(lanes=Lanes(guard, PIN)), "biz-a/instagram", **{**args, "site": "linkedin"})
    with pytest.raises(InvalidRequest, match="no reviewed edit endpoint"):
        learn.open_learn(Runtime(lanes=Lanes(guard, LI_PIN)), "biz-a/linkedin", **{**args, "field": "location"})
    # a LinkedIn lane's only edit page is its own member page: never another member's
    with pytest.raises(InvalidRequest, match="reviewed profile edit pages"):
        learn.open_learn(Runtime(lanes=Lanes(guard, LI_PIN)), "biz-a/linkedin",
                         **{**args, "field": "location", "path_prefixes": ["/in/"]})
    assert guard._learn is None
    learn.close_learn(Runtime(lanes=Lanes(None, PIN)), "biz-a/instagram", "lw_x")  # nothing to close: no error


# ---- a learned operation's strict commit scope -------------------------------------------------

DECLARED = {"method": "POST", "url": r"^https://www\.instagram\.com/api/v1/web/accounts/edit/$"}


def _problem(commit, url, body, method="POST", headers=FORM):
    rule = egress.classify(url, method, headers, body=body)
    return commit.problem(rule, url, method, headers, lambda: body)


def test_a_strict_scope_sends_only_its_declared_request_uploads_and_known_reads():
    strict = egress.FlowCommit.of("connector.instagram-profile.bio", {**DECLARED, "strict": True})
    loose = egress.FlowCommit.of("connector.instagram-profile.bio", DECLARED)
    assert strict.strict and not loose.strict
    assert _problem(strict, EDIT, bio_body()) is None                                    # the declared request
    other = "https://www.instagram.com/api/v1/web/accounts/set_private/"
    why = _problem(strict, other, b"is_private=true")
    assert why is not None and "strict" in why                                           # an undeclared POST
    assert _problem(loose, other, b"is_private=true") is None                            # unchanged when not strict
    # an unknown GraphQL mutation is no known read
    assert _problem(strict, IG_GQL, ig("PolarisSomethingMutation")) is not None
    assert _problem(loose, IG_GQL, ig("PolarisSomethingMutation")) is None
    assert _problem(strict, IG_GQL, ig("PolarisProfilePageContentQuery")) is None        # a known read
    upload = "https://i.instagram.com/rupload_igphoto/x"
    assert _problem(strict, upload, b"\x89PNG") is None                                  # an upload (draft)
    assert _problem(strict, "https://www.instagram.com/logging/falco", b"x=1") is None   # plumbing read
    # every commit and write it does not declare stays refused, as without strict
    comment = ig("PolarisPostCommentInputRevampedMutation", data={"comment_text": "hi"})
    assert _problem(strict, IG_GQL, comment) is not None and _problem(loose, IG_GQL, comment) is not None
    # a strict read operation (no request declared) writes nothing at all
    read_op = egress.FlowCommit.of("connector.instagram-profile.bio_read", {"strict": True})
    assert read_op.strict and read_op.url is None
    assert _problem(read_op, other, b"is_private=true") is not None
    assert _problem(read_op, EDIT, bio_body()) is not None


def test_a_non_strict_scope_is_built_as_before():
    assert egress.FlowCommit.of("f", None) == egress.FlowCommit("f")
    assert egress.FlowCommit.of("f", {}) == egress.FlowCommit("f")
    built = egress.FlowCommit.of("f", {"method": "put", "url": "^https://x/", "op": "A", "match": {"b": "c"}})
    assert built == egress.FlowCommit("f", method="PUT", url="^https://x/", op="A", match=(("b", "c"),))


PINNED = {**DECLARED, "match": {"form:biography": "^" + __import__("re").escape(BIO) + "$"}, "strict": True}


async def test_the_guard_aborts_a_strict_runs_undeclared_post(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    seed(guard)
    page = FakePage(EDIT_PAGE)
    async with guard.flow(page):
        await guard.scope_flow(egress.FlowCommit.of("connector.instagram-profile.bio", PINNED))
        [(_, handler)] = ctx.routes
        other = FakeRequest("https://www.instagram.com/api/v1/web/accounts/set_private/", "POST",
                            frame=page.main_frame, headers=FORM, body=b"is_private=true")
        await handler(route := FakeRoute(), other)
        assert route.aborted
        [block] = guard.blocks_since(0)
        assert block.rule == "flow_commit" and block.prevented
        declared = FakeRequest(EDIT, "POST", frame=page.main_frame, headers=FORM, body=bio_body())
        await handler(route := FakeRoute(), declared)
        assert not route.aborted


async def _strict_save(tmp_path, body, *, request=PINNED, baseline=CURRENT):
    tmp_path.mkdir(parents=True, exist_ok=True)
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    if baseline is not None:
        seed(guard, baseline)
    page = FakePage(EDIT_PAGE)
    async with guard.flow(page):
        await guard.scope_flow(egress.FlowCommit.of("connector.instagram-profile.bio", request))
        [(_, handler)] = ctx.routes
        await handler(route := FakeRoute(), FakeRequest(EDIT, "POST", frame=page.main_frame, headers=FORM, body=body))
    blocks = guard.blocks_since(0)
    return route, (blocks[-1].reason if blocks else None)


async def test_a_learned_save_sends_only_its_own_value_in_its_own_field(tmp_path):
    route, _ = await _strict_save(tmp_path, bio_body())
    assert not route.aborted
    # the field locator landed on another input: the bio is unchanged, the username holds the value
    route, why = await _strict_save(tmp_path / "a", form(username=BIO))
    assert route.aborted and "strict" in why
    route, _ = await _strict_save(tmp_path / "b", bio_body("Something else"))
    assert route.aborted
    route, _ = await _strict_save(tmp_path / "c", bio_body(BIO + "\n"))   # exactly: no trailing line break
    assert route.aborted
    commit = egress.FlowCommit.of("connector.instagram-profile.bio", PINNED)
    why = commit.problem(egress.EffectRule("instagram.profile.learned_bio", "instagram", "www.instagram.com",
                                           "/api/v1/web/accounts/edit/"), EDIT, "POST", FORM, lambda: form(username=BIO))
    assert why is not None and "form:biography" in why


@pytest.mark.parametrize("changes", [{"email": "x@y.z"}, {"username": "acme2"}, {"chaining_enabled": None},
                                     {"gender": "1"}])
async def test_a_learned_save_that_changes_another_field_of_the_form_is_refused(tmp_path, changes):
    route, why = await _strict_save(tmp_path, form(biography=BIO, **changes))
    assert route.aborted and "also changes" in why


async def test_a_learned_save_without_the_forms_current_values_is_refused(tmp_path):
    route, why = await _strict_save(tmp_path, bio_body(), baseline=None)
    assert route.aborted and "did not load" in why


async def test_a_learned_save_that_pins_no_field_is_refused(tmp_path):
    route, why = await _strict_save(tmp_path, bio_body(), request={**DECLARED, "strict": True})
    assert route.aborted and "learn it again" in why


def test_the_executor_fills_a_pinned_value_escaped():
    from agentos.runner.executor import _match_for

    got = _match_for({"form:biography": "^${value}$"}, {"value": "a+b (c)?"})
    assert got == {"form:biography": "^a\\+b\\ \\(c\\)\\?$"}
    import re as re_

    assert re_.fullmatch(got["form:biography"], "a+b (c)?") and not re_.fullmatch(got["form:biography"], "aab (c)")
    with pytest.raises(ValueError, match="no value"):
        _match_for({"form:biography": "^${value}$"}, {})


async def test_the_executor_scopes_a_learned_operations_run_strictly():
    from agentos.runner.executor import FlowExecutor, learned_flow

    seen = []

    class Driver_:
        async def scope_commit(self, flow_id, request):
            seen.append((flow_id, request))

    def state(strict, request):
        return SimpleNamespace(driver=Driver_(), flow=SimpleNamespace(id="f", connector="x"), strict_scope=strict,
                               meta={"commit": {"request": request}} if request is not None else {})

    await FlowExecutor._scope_commit(state(True, DECLARED))
    await FlowExecutor._scope_commit(state(True, None))
    await FlowExecutor._scope_commit(state(False, DECLARED))
    assert seen == [("f", {**DECLARED, "strict": True}), ("f", {"strict": True}), ("f", DECLARED)]
    pinned = state(True, {**DECLARED, "match": {"form:biography": "^${value}$"}})
    pinned.scope = {"value": "Fresh roast"}
    await FlowExecutor._scope_commit(pinned)
    assert seen[-1] == ("f", {**DECLARED, "match": {"form:biography": "^Fresh\\ roast$"}, "strict": True})
    assert learned_flow(SimpleNamespace(saved_by={"kind": "learned"}))
    assert not learned_flow(SimpleNamespace(saved_by={"kind": "local"}))
    assert not learned_flow(SimpleNamespace(saved_by=None))
