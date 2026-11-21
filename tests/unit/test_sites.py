"""The site table (openajar.node.sites): hosts, URL rules, session and identity cookies, expectations."""

from __future__ import annotations

import base64
import json
import re

import pytest

from openajar.core.models import AccountRef, LaneSpec, Signal
from openajar.node import sites
from openajar.node.sites import (
    SITES,
    VENDORS,
    Cookie,
    Identity,
    LaneExpectation,
    PageFacts,
    account_identity,
    account_matches,
    check_account,
    classify_page,
    describe,
    expectation_for,
    identity_from_cookies,
    site,
    site_for,
)

EM_DASH = "—"
C, L, B, R, A, OK = (Signal.CHECKPOINT, Signal.LOGGED_OUT, Signal.BLOCKED, Signal.RATE_LIMITED, Signal.CAPTCHA,
                     Signal.OK)


def jar(**values: str) -> list[dict[str, str]]:
    return [{"name": k, "value": v, "domain": ".example", "path": "/"} for k, v in values.items()]


SIGNED_IN = {
    "facebook": jar(c_user="100012345678901", xs="35%3Aabc"),
    "instagram": jar(sessionid="123%3Aabc", ds_user_id="4567890123"),
    "linkedin": jar(li_at="AQEDAR-opaque"),
    "reddit": jar(reddit_session="12345%2C2026-01-01T00%3A00%3A00%2Cabc"),
    "medium": jar(sid="1:abc", uid="a1b2c3d4e5f6"),
    "wordpress": jar(wordpress_logged_in_0123abcd="ada%7C1767225600%7Ctoken%7Chmac"),
    "google": jar(SID="g.a000opaque"),
    "x": jar(auth_token="deadbeef", twid="u%3D1234567890"),
}


def expect(key: str) -> LaneExpectation:
    return LaneExpectation("biz-a/" + key, frozenset({key}))


# ---- the table itself ------------------------------------------------------------------------

def test_the_table_is_well_formed():
    keys = [s.key for s in SITES]
    assert len(keys) == len(set(keys))
    words = [w for s in SITES for w in (s.key, *s.aliases)]
    assert len(words) == len(set(words)), "an alias may name only one site"
    for s in SITES:
        assert s.session, f"{s.key} needs a session cookie for logged_out"
        assert all(h == h.lower() and not h.startswith(".") for h in s.hosts)
        assert s.home is None or s.home.startswith("https://")
        for rule in s.urls:
            re.compile(rule.pattern)
            assert rule.signal in (C, L, B, R, A) and rule.label
            assert EM_DASH not in rule.label
        assert set(SIGNED_IN) >= {s.key}, f"add signed-in cookies for {s.key} to this test"
    for v in VENDORS:
        assert v.kind in ("frame", "script", "element")
        assert v.signal in (None, A, B)
        assert (v.selector is not None) == (v.kind == "element")
        assert EM_DASH not in v.detail
        if v.pattern:
            re.compile(v.pattern)


def test_the_table_covers_the_sites_and_vendors_the_owner_uses():
    assert {"instagram", "facebook", "reddit", "linkedin", "medium", "wordpress", "google", "x"} <= \
        {s.key for s in SITES}
    frame_hosts = {v.host for v in VENDORS if v.kind == "frame" and v.signal is not None}
    assert {"arkoselabs.com", "funcaptcha.com", "captcha-delivery.com", "px-cloud.net", "challenges.cloudflare.com",
            "hcaptcha.com", "google.com"} <= frame_hosts
    assert {"perimeterx.net", "datadome.co"} <= {v.host for v in VENDORS if v.kind == "script" and v.signal is None}


def test_describe_lists_every_site_as_plain_data():
    table = describe()
    assert [row["site"] for row in table] == [s.key for s in SITES]
    json.dumps(table)  # plain data
    assert next(r for r in table if r["site"] == "linkedin")["statuses"] == {999: "blocked"}


@pytest.mark.parametrize("url, key", [
    ("https://www.instagram.com/p/abc/", "instagram"),
    ("https://instagram.com/", "instagram"),
    ("https://WWW.INSTAGRAM.COM./", "instagram"),
    ("https://m.facebook.com/home.php", "facebook"),
    ("https://web.facebook.com/", "facebook"),
    ("https://old.reddit.com/r/x/", "reddit"),
    ("https://www.linkedin.com/feed/", "linkedin"),
    ("https://acme.medium.com/post-1", "medium"),
    ("https://accounts.google.com/v3/signin/identifier", "google"),
    ("https://twitter.com/home", "x"),
    ("https://x.com/home", "x"),
    ("https://acme.wordpress.com/", "wordpress"),
    ("https://blog.acme.example/wp-admin/", "wordpress"),
    ("https://blog.acme.example/wp-login.php?redirect_to=x", "wordpress"),
])
def test_site_for_matches_hosts_and_subdomains(url, key):
    assert site_for(url).key == key


@pytest.mark.parametrize("url", [
    "https://evilinstagram.com/challenge/", "https://instagram.com.evil.example/challenge/",
    "https://example.com/www.facebook.com/checkpoint/", "https://notreddit.com/login",
    "https://blog.acme.example/", "https://blog.acme.example/wp-admins", "about:blank", "", "ftp://x.com/",
    "https://xcom.example/login",
])
def test_site_for_refuses_look_alikes_and_non_web_urls(url):
    assert site_for(url) is None


def test_site_lookup_by_key_and_alias():
    assert site("ig").key == "instagram" and site("FB").key == "facebook" and site("twitter").key == "x"
    assert site("blog").key == "wordpress" and site("gmail").key == "google" and site("nope") is None
    assert sites.site_key("IG") == "instagram" and sites.site_key("Mastodon") == "mastodon"


# ---- every URL rule, expected to be signed in (the lane is that site's lane) ----------------

URL_CASES = [
    # facebook
    ("https://www.facebook.com/checkpoint/?next=https%3A%2F%2Fwww.facebook.com%2F", C, "facebook checkpoint URL"),
    ("https://www.facebook.com/checkpoint/1501092823525282/", C, "facebook checkpoint URL"),
    ("https://m.facebook.com/checkpoint", C, "facebook checkpoint URL"),
    ("https://www.facebook.com/two_step_verification/authentication/?flow=x", C,
     "facebook two-step verification URL"),
    ("https://www.facebook.com/auth_platform/afad/?apc=x", C, "facebook login approval URL"),
    ("https://www.facebook.com/login/?next=x", L, "facebook login URL"),
    ("https://www.facebook.com/login.php", L, "facebook login URL"),
    ("https://www.facebook.com/login/device-based/regular/login/", L, "facebook login URL"),
    ("https://www.facebook.com/recover/initiate/", L, "facebook account recovery URL"),
    # instagram
    ("https://www.instagram.com/challenge/?next=/", C, "instagram challenge URL"),
    ("https://www.instagram.com/challenge/action/AXG/abc/", C, "instagram challenge URL"),
    ("https://www.instagram.com/accounts/login/two_factor?next=%2F", C, "instagram two-factor URL"),
    ("https://www.instagram.com/accounts/suspended/?next=%2F", C, "instagram suspended account URL"),
    ("https://www.instagram.com/accounts/disabled/", C, "instagram suspended account URL"),
    ("https://www.instagram.com/accounts/login/?next=%2F", L, "instagram login URL"),
    ("https://www.instagram.com/accounts/login", L, "instagram login URL"),
    # linkedin
    ("https://www.linkedin.com/checkpoint/challenge/AgG1x?ut=abc", C, "linkedin checkpoint challenge URL"),
    ("https://www.linkedin.com/checkpoint/challengesV2/AQ", C, "linkedin checkpoint challenge URL"),
    ("https://www.linkedin.com/checkpoint/restrictions/", C, "linkedin checkpoint URL"),
    ("https://www.linkedin.com/checkpoint/lg/login-submit", L, "linkedin login URL"),
    ("https://www.linkedin.com/checkpoint/rm/sign-in-another-account", L, "linkedin login URL"),
    ("https://www.linkedin.com/login?fromSignIn=true", L, "linkedin login URL"),
    ("https://www.linkedin.com/uas/login", L, "linkedin login URL"),
    ("https://www.linkedin.com/authwall?trk=x", L, "linkedin login URL"),
    # reddit
    ("https://www.reddit.com/login/", L, "reddit login URL"),
    ("https://old.reddit.com/login", L, "reddit login URL"),
    ("https://www.reddit.com/account/login/?dest=x", L, "reddit login URL"),
    # medium
    ("https://medium.com/m/signin?operation=login", L, "medium sign-in URL"),
    # wordpress
    ("https://blog.acme.example/wp-login.php?action=validate_2fa&wp-auth-id=1", C, "wordpress two-factor URL"),
    ("https://blog.acme.example/wp-login.php?redirect_to=%2Fwp-admin%2F&reauth=1", L, "wordpress login URL"),
    ("https://wordpress.com/log-in?redirect_to=x", L, "wordpress login URL"),
    # google
    ("https://accounts.google.com/v3/signin/challenge/recaptcha?TL=x", A, "google sign-in reCAPTCHA URL"),
    ("https://accounts.google.com/v3/signin/challenge/ipp/consent?TL=x", C, "google verification challenge URL"),
    ("https://accounts.google.com/signin/v2/challenge/az?TL=x", C, "google verification challenge URL"),
    ("https://accounts.google.com/v3/signin/challenge/dp?TL=x", C, "google verification challenge URL"),
    ("https://accounts.google.com/speedbump/idvreenable?TL=x", C, "google verify-it's-you URL"),
    ("https://accounts.google.com/v3/signin/rejected?TL=x", B, "google sign-in rejected URL"),
    ("https://accounts.google.com/signin/rejected", B, "google sign-in rejected URL"),
    ("https://accounts.google.com/v3/signin/identifier?continue=x", L, "google sign-in URL"),
    ("https://accounts.google.com/ServiceLogin?service=mail", L, "google sign-in URL"),
    ("https://accounts.google.com/v3/signin/challenge/pwd?TL=x", L, "google password page"),
    ("https://www.google.com/sorry/index?continue=x", R, "google unusual traffic page"),
    # x
    ("https://x.com/account/access", C, "x locked account URL"),
    ("https://twitter.com/account/login_challenge?challenge_type=x", C, "x login challenge URL"),
    ("https://x.com/account/login_verification", C, "x login challenge URL"),
    ("https://x.com/i/flow/login", L, "x login URL"),
    ("https://twitter.com/login", L, "x login URL"),
]


@pytest.mark.parametrize("url, signal, detail", URL_CASES)
def test_url_rules(url, signal, detail):
    key = site_for(url).key
    got = classify_page(PageFacts(url, status=200, cookies=[]), expect(key))
    assert (got.signal, got.detail, got.site) == (signal, detail, key)


@pytest.mark.parametrize("url, signal, detail", [c for c in URL_CASES if c[1] is not L])
def test_url_rules_hold_whatever_the_cookies_and_expectation(url, signal, detail):
    key = site_for(url).key
    for cookies, exp in ((SIGNED_IN[key], expect(key)), ([], None), (None, None)):
        got = classify_page(PageFacts(url, status=200, cookies=cookies), exp)
        assert (got.signal, got.detail) == (signal, detail)


@pytest.mark.parametrize("url, signal, detail", [c for c in URL_CASES if c[1] is L])
def test_a_login_page_is_logged_out_only_for_the_lane_that_should_be_signed_in(url, signal, detail):
    key = site_for(url).key
    assert classify_page(PageFacts(url, status=200, cookies=[]), None).signal is OK
    other = LaneExpectation("biz-a/other", frozenset({"medium" if key != "medium" else "reddit"}))
    assert classify_page(PageFacts(url, status=200, cookies=[]), other).signal is OK
    assert classify_page(PageFacts(url, status=200, cookies=[]), expect(key)).signal is L


def test_googles_password_page_while_signed_in_is_verify_its_you():
    url = "https://accounts.google.com/v3/signin/challenge/pwd?TL=x&continue=https://myaccount.google.com/"
    got = classify_page(PageFacts(url, status=200, cookies=SIGNED_IN["google"]), None)
    assert (got.signal, got.detail) == (C, "google password page while signed in")


@pytest.mark.parametrize("url", [
    "https://www.instagram.com/p/abc/", "https://www.instagram.com/challenges_of_life/",
    "https://www.facebook.com/checkpointcharlie/", "https://www.facebook.com/groups/login-tips/",
    "https://www.linkedin.com/in/someone/", "https://www.reddit.com/r/login/",
    "https://accounts.google.com/", "https://www.google.com/search?q=sorry",
    "https://x.com/account/settings", "https://x.com/someone/status/1",
    "https://medium.com/@someone/signin-tips-123", "https://blog.acme.example/wp-content/x.png",
])
def test_ordinary_pages_on_known_sites_are_ok_when_signed_in(url):
    key = site_for(url).key if site_for(url) else None
    cookies = SIGNED_IN.get(key or "", [])
    got = classify_page(PageFacts(url, status=200, cookies=cookies), expect(key) if key else None)
    assert got.signal is OK, got


def test_linkedin_999_is_blocked_but_999_elsewhere_is_not():
    got = classify_page(PageFacts("https://www.linkedin.com/feed/", status=999, cookies=SIGNED_IN["linkedin"]))
    assert (got.signal, got.detail) == (B, "linkedin HTTP 999")
    assert classify_page(PageFacts("https://shop.example/", status=999)).signal is OK


# ---- session cookies and logged_out -------------------------------------------------------------

@pytest.mark.parametrize("key, home", [(s.key, s.home or "https://blog.acme.example/wp-admin/") for s in SITES])
def test_every_site_is_signed_in_with_its_session_cookies_and_logged_out_without(key, home):
    signed = classify_page(PageFacts(home, status=200, cookies=SIGNED_IN[key]), expect(key))
    assert signed.signal is OK and signed.site == key
    out = classify_page(PageFacts(home, status=200, cookies=[]), expect(key))
    first = site(key).session[0].name
    assert (out.signal, out.detail) == (L, f"{key} session cookie {first} missing")


def test_facebook_needs_both_session_cookies():
    got = classify_page(PageFacts("https://www.facebook.com/", cookies=jar(c_user="1")), expect("facebook"))
    assert (got.signal, got.detail) == (L, "facebook session cookie xs missing")


def test_an_empty_cookie_does_not_count():
    got = classify_page(PageFacts("https://www.instagram.com/", cookies=jar(sessionid="")), expect("instagram"))
    assert got.signal is L


def test_medium_signed_out_visitors_have_a_lo_uid():
    visitor = jar(sid="1:abc", uid="lo_9f8e7d")
    got = classify_page(PageFacts("https://medium.com/", cookies=visitor), expect("medium"))
    assert (got.signal, got.detail) == (L, "medium session cookie uid missing")


def test_a_wildcard_cookie_name_is_a_prefix():
    rule = Cookie("wordpress_logged_in*")
    assert rule.find(jar(wordpress_logged_in_abc="ada|1|t|h"))["name"] == "wordpress_logged_in_abc"
    assert rule.find(jar(wordpress_logged_in="ada|1|t|h")) is not None
    assert rule.find(jar(wordpress_sec_abc="x", wp_logged_in="y")) is None
    assert Cookie("sid").find(jar(sidx="1")) is None


def test_no_cookie_jar_never_guesses_logged_out():
    got = classify_page(PageFacts("https://www.instagram.com/", status=200, cookies=None), expect("instagram"))
    assert got.signal is OK


def test_a_lane_that_does_not_use_a_site_is_never_logged_out_there():
    reddit_lane = expect("reddit")
    for url in ("https://www.instagram.com/", "https://www.facebook.com/somepage", "https://medium.com/@a/b"):
        assert classify_page(PageFacts(url, status=200, cookies=[]), reddit_lane).signal is OK
    assert classify_page(PageFacts("https://www.instagram.com/", status=200, cookies=[]), None).signal is OK


def test_status_rules_win_over_a_missing_session():
    got = classify_page(PageFacts("https://www.reddit.com/", status=429, cookies=[]), expect("reddit"))
    assert (got.signal, got.detail) == (R, "HTTP 429")


# ---- identity --------------------------------------------------------------------------------

def _jwt(payload: dict) -> str:
    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode().rstrip("=")
    return f"{b64(b'{\"alg\":\"RS256\"}')}.{b64(json.dumps(payload).encode())}.c2ln"


@pytest.mark.parametrize("key, cookies, user_id, source", [
    ("facebook", SIGNED_IN["facebook"], "100012345678901", "cookie c_user"),
    ("instagram", SIGNED_IN["instagram"], "4567890123", "cookie ds_user_id"),
    ("reddit", jar(reddit_session=_jwt({"sub": "t2_abc123", "exp": 1, "loggedIn": True})), "t2_abc123",
     "cookie reddit_session"),
    ("reddit", SIGNED_IN["reddit"], "t2_9ix", "cookie reddit_session"),  # legacy decimal id 12345
    ("medium", SIGNED_IN["medium"], "a1b2c3d4e5f6", "cookie uid"),
    ("wordpress", SIGNED_IN["wordpress"], "ada", "cookie wordpress_logged_in_0123abcd"),
    ("x", SIGNED_IN["x"], "1234567890", "cookie twid"),
])
def test_identity_from_the_sites_own_cookie(key, cookies, user_id, source):
    assert identity_from_cookies(site(key), cookies) == Identity(key, user_id, source)


@pytest.mark.parametrize("key, cookies", [
    ("linkedin", SIGNED_IN["linkedin"]),          # li_at is opaque
    ("google", SIGNED_IN["google"]),              # SID is opaque
    ("instagram", jar(ds_user_id="1")),          # an id without a session is not signed in
    ("reddit", jar(reddit_session="not-a-jwt")),
    ("reddit", jar(reddit_session=_jwt({"sub": "loid"}))),
    ("reddit", jar(reddit_session="a.%%%.b")),
    ("x", jar(auth_token="t", twid="garbage")),
    ("wordpress", jar(wordpress_logged_in_x="no-separators")),
])
def test_no_identity_when_the_site_does_not_show_one(key, cookies):
    assert identity_from_cookies(site(key), cookies) is None


class _Context:
    def __init__(self, cookies):
        self.jar, self.asked = cookies, []

    async def cookies(self, urls):
        self.asked.append(urls)
        return self.jar


async def test_account_identity_reads_the_context_cookie_jar_only():
    ctx = _Context(SIGNED_IN["instagram"])
    assert await account_identity(ctx, "instagram") == {"site": "instagram", "user_id": "4567890123",
                                                         "source": "cookie ds_user_id"}
    assert ctx.asked == [["https://www.instagram.com/"]]
    assert await account_identity(_Context([]), "ig") is None
    assert await account_identity(_Context(SIGNED_IN["linkedin"]), "linkedin") is None
    assert await account_identity(ctx, "wordpress") is None  # no fixed home: needs the blog's URL
    ctx = _Context(SIGNED_IN["wordpress"])
    assert (await account_identity(ctx, "wordpress", "https://blog.acme.example/wp-admin/"))["user_id"] == "ada"
    assert await account_identity(ctx, "unknown-site") is None


def test_account_matches():
    seen = Identity("instagram", "4567890123", "cookie ds_user_id")
    assert account_matches(AccountRef(site="instagram", user_id="4567890123"), seen) is True
    assert account_matches(AccountRef(site="ig", user_id=" 4567890123 "), seen) is True
    assert account_matches(AccountRef(site="instagram", user_id="1"), seen) is False
    assert account_matches(AccountRef(site="instagram", user_id="1"), seen.public()) is False
    assert account_matches(AccountRef(site="instagram", handle="@acme"), seen) is None
    assert account_matches(AccountRef(site="facebook", user_id="4567890123"), seen) is None
    assert account_matches(None, seen) is None
    assert account_matches(AccountRef(site="instagram", user_id="1"), None) is None
    reddit = Identity("reddit", "t2_ABC", "cookie reddit_session")
    assert account_matches(AccountRef(site="reddit", user_id="t2_abc"), reddit) is True


def test_check_account_statuses():
    seen = Identity("instagram", "555", "cookie ds_user_id")
    pinned = LaneExpectation("biz-a/ig", frozenset({"instagram"}),
                             AccountRef(site="instagram", handle="@acme.coffee", user_id="444"))
    wrong = check_account(pinned, seen)
    assert wrong.status == "wrong_account"
    assert wrong.public() == {"site": "instagram", "user_id": "555", "source": "cookie ds_user_id",
                              "status": "wrong_account", "expected": {"user_id": "444", "handle": "@acme.coffee"}}
    right = LaneExpectation("biz-a/ig", frozenset({"instagram"}), AccountRef(site="instagram", user_id="555"))
    assert check_account(right, seen).status == "match"
    assert check_account(expect("instagram"), seen).public() == {
        "site": "instagram", "user_id": "555", "source": "cookie ds_user_id", "status": "unpinned"}
    fb_pinned = LaneExpectation("biz-a/fb", frozenset({"facebook"}), AccountRef(site="facebook", user_id="1"))
    assert check_account(fb_pinned, seen).status == "unpinned"  # another site's pin says nothing here
    assert check_account(None, seen).status == "unpinned"
    assert check_account(pinned, None) is None


def test_wrong_account_is_reported_with_the_page_and_named_in_the_detail():
    pinned = LaneExpectation("biz-a/ig", frozenset({"instagram"}),
                             AccountRef(site="instagram", handle="@acme.coffee", user_id="444"))
    got = classify_page(PageFacts("https://www.instagram.com/", status=200, cookies=SIGNED_IN["instagram"]), pinned)
    assert got.signal is OK and got.wrong_account
    assert got.detail == "wrong account: instagram user 4567890123 is signed in, the lane expects @acme.coffee (444)"
    on_checkpoint = classify_page(PageFacts("https://www.instagram.com/challenge/", cookies=SIGNED_IN["instagram"]),
                                  pinned)
    assert on_checkpoint.signal is C and on_checkpoint.wrong_account


# ---- what a lane expects ----------------------------------------------------------------------

@pytest.mark.parametrize("lane, sites_expected", [
    ("biz-a/instagram", {"instagram"}),
    ("acme/ig", {"instagram"}),
    ("personal/reddit", {"reddit"}),
    ("biz-c/facebook", {"facebook"}),
    ("biz-b/fb-page", {"facebook"}),
    ("biz-b/linkedin-2", {"linkedin"}),
    ("acme/blog", {"wordpress"}),
    ("acme/medium", {"medium"}),
    ("acme/x", {"x"}),
    ("personal/gmail", {"google"}),
    ("acme/ig-fb", {"instagram", "facebook"}),
    ("personal/research", set()),
    ("personal/instagramish", set()),
])
def test_expectation_from_the_lane_name(lane, sites_expected):
    exp = expectation_for(LaneSpec(id=lane))
    assert exp.sites == frozenset(sites_expected) and exp.account is None
    assert exp.source == ("lane name" if sites_expected else "")


def test_the_pinned_account_wins_over_the_lane_name():
    spec = LaneSpec(id="biz-a/instagram", account=AccountRef(site="FB", user_id="1"))
    exp = expectation_for(spec)
    assert exp.sites == frozenset({"facebook"}) and exp.account == spec.account and exp.source == "account"
    odd = expectation_for(LaneSpec(id="biz-a/social", account=AccountRef(site="Mastodon")))
    assert odd.sites == frozenset({"mastodon"})
    assert not odd.expects(None) and odd.expects("mastodon")
