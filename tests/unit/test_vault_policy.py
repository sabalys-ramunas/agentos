"""The No-Body-Injection rule: pure decision tests."""

from datetime import UTC, datetime

import pytest
from test_vault_support import fld, login_form

from openajar.vault.errors import ResolutionDenied
from openajar.vault.models import EntryKind, EntryMeta
from openajar.vault.policy import (
    FieldRole,
    Reason,
    Sink,
    check_origin,
    classify_fields,
    decide,
    lane_bound,
    matching_entries,
    otp_group,
)

NOW = datetime(2026, 11, 4, tzinfo=UTC)
TOP = "https://www.instagram.com/accounts/login/"
ORIGIN = "https://www.instagram.com"


def entry(**kw) -> EntryMeta:
    base = {"id": "v_1", "name": "ig", "kind": EntryKind.PASSWORD, "domain": "instagram.com", "hosts": [],
                "lanes": ["biz-a/instagram"], "has_username": True, "has_secret": True, "has_totp": False,
                "created_at": NOW, "updated_at": NOW}
    base.update(kw)
    return EntryMeta(**base)


def pw_decision(url=TOP, field=None, fields=None, lane="biz-a/instagram", e=None, sink=Sink.FORM_PASSWORD):
    fields = fields if fields is not None else login_form(ORIGIN)
    field = field or next(f for f in fields if f.locator == ("pass" if sink is Sink.FORM_PASSWORD else "user"))
    return decide(e or entry(), lane_id=lane, sink=sink, url=url, field=field, fields=fields)


# ---- lane binding --------------------------------------------------------------

@pytest.mark.parametrize("patterns,lane,ok", [
    (["biz-a/instagram"], "biz-a/instagram", True),
    (["biz-a/*"], "biz-a/instagram", True),
    (["biz-a/*"], "biz-ab/instagram", False),
    (["biz-a/instagram"], "biz-a/instagram2", False),
    (["biz-a/instagram"], "biz-b/instagram", False),
    ([], "biz-a/instagram", False),
    (["*"], "biz-a/instagram", False),
    (["*/*"], "biz-a/instagram", False),
    (["biz-a/*"], "biz-a/../x", False),
    (["biz-a/instagram"], "BIZ-A/instagram", False),
])
def test_lane_bound(patterns, lane, ok):
    assert lane_bound(patterns, lane) is ok


def test_entry_bound_to_another_lane_is_refused():
    d = pw_decision(lane="biz-b/instagram")
    assert not d.allowed and d.reason is Reason.LANE_NOT_BOUND


# ---- origins -------------------------------------------------------------------

@pytest.mark.parametrize("domain,url", [
    ("google.com", "https://accounts.google.lt/v3/signin"),
    ("google.com", "https://accounts.google.co.uk/"),
    ("google.lt", "https://accounts.google.com/"),
])
def test_google_country_domains_are_one_site(domain, url):
    assert check_origin(entry(domain=domain), url).allowed


@pytest.mark.parametrize("url", [
    "https://accounts.google.evil.io/",
    "https://google.lt.evil.io/",
    "https://evilgoogle.com/",
    "https://accounts.google.co/",        # not one of Google's domains
    "https://accounts.googIe.com/",       # a capital i for the l
])
def test_only_googles_own_domains_count(url):
    d = check_origin(entry(domain="google.com"), url)
    assert not d.allowed and d.reason is Reason.DOMAIN_MISMATCH


def test_a_google_entry_still_keeps_to_its_hosts():
    e = entry(domain="google.com", hosts=["accounts.google.com"])
    assert check_origin(e, "https://accounts.google.lt/").reason is Reason.HOST_NOT_ALLOWED


def test_happy_path_password_and_username():
    assert pw_decision().allowed
    assert pw_decision(sink=Sink.FORM_USERNAME).allowed
    assert pw_decision().host == "www.instagram.com"


@pytest.mark.parametrize("url,reason", [
    ("https://instagram.com.evil.io/login", Reason.DOMAIN_MISMATCH),
    ("https://evilinstagram.com/login", Reason.DOMAIN_MISMATCH),
    ("https://instagram.co/login", Reason.DOMAIN_MISMATCH),
    ("https://www.xn--instgram-46g.com/", Reason.DOMAIN_MISMATCH),       # Cyrillic a, punycode
    ("https://www.instаgram.com/", Reason.DOMAIN_MISMATCH),              # Cyrillic a, unicode
    ("https://instagram.com@evil.io/", Reason.BAD_URL),
    ("https://evil.io\\@instagram.com/", Reason.BAD_URL),
    ("http://www.instagram.com/accounts/login/", Reason.INSECURE_SCHEME),
    ("about:blank", Reason.BAD_URL),
    ("null", Reason.BAD_URL),
    ("data:text/html,<form>", Reason.BAD_URL),
    ("javascript:void(0)", Reason.BAD_URL),
])
def test_lookalikes_and_bad_origins_are_refused(url, reason):
    fields = login_form(url if url.startswith("http") and "@" not in url and "\\" not in url else ORIGIN)
    d = pw_decision(url=url, fields=fields)
    assert not d.allowed and d.reason is reason


def test_subdomains_of_the_domain_match_and_hosts_narrow():
    assert pw_decision(url="https://instagram.com/", fields=login_form("https://instagram.com")).allowed
    assert pw_decision(url="https://m.instagram.com/", fields=login_form("https://m.instagram.com")).allowed
    narrow = entry(hosts=["www.instagram.com"])
    assert pw_decision(e=narrow).allowed
    d = pw_decision(url="https://m.instagram.com/", fields=login_form("https://m.instagram.com"), e=narrow)
    assert d.reason is Reason.HOST_NOT_ALLOWED


def test_idn_entry_matches_both_forms_of_its_own_domain():
    e = entry(domain="xn--bcher-kva.de")
    for url in ("https://bücher.de/login", "https://www.xn--bcher-kva.de/login"):
        origin = "https://" + url.split("/")[2]
        assert pw_decision(url=url, fields=login_form(origin), e=e).allowed, url
    d = pw_decision(url="https://bucher.de/login", fields=login_form("https://bucher.de"), e=e)
    assert d.reason is Reason.DOMAIN_MISMATCH


def test_http_only_on_loopback_for_loopback_entries():
    e = entry(domain="127.0.0.1")
    url = "http://127.0.0.1:8080/login"
    assert pw_decision(url=url, fields=login_form("http://127.0.0.1:8080"), e=e).allowed
    local = entry(domain="localhost")
    assert pw_decision(url="http://localhost:9000/", fields=login_form("http://localhost:9000"), e=local).allowed
    # a loopback page never matches a real domain, and http elsewhere is refused
    d = pw_decision(url="http://127.0.0.1/", fields=login_form("http://127.0.0.1"))
    assert d.reason is Reason.DOMAIN_MISMATCH
    d = pw_decision(url="http://10.0.0.1/", fields=login_form("http://10.0.0.1"), e=entry(domain="10.0.0.1"))
    assert d.reason is Reason.INSECURE_SCHEME


# ---- frames --------------------------------------------------------------------

def test_cross_origin_iframe_is_refused():
    evil_frame = login_form(chain=(ORIGIN, "https://evil.io"))
    d = pw_decision(fields=evil_frame)
    assert not d.allowed and d.reason is Reason.CROSS_ORIGIN_FRAME


def test_instagram_form_framed_by_a_foreign_page_is_refused():
    framed = login_form(chain=("https://evil.io", ORIGIN))
    d = pw_decision(url="https://evil.io/", fields=framed)
    assert d.reason is Reason.DOMAIN_MISMATCH


def test_nested_frames_must_all_be_same_origin():
    nested = login_form(chain=(ORIGIN, "https://ads.example.net", ORIGIN))
    assert pw_decision(fields=nested).reason is Reason.CROSS_ORIGIN_FRAME
    same = login_form(chain=(ORIGIN, ORIGIN))
    assert pw_decision(fields=same).allowed
    sibling = login_form(chain=(ORIGIN, "https://login.instagram.com"))   # same site, other origin
    assert pw_decision(fields=sibling).reason is Reason.CROSS_ORIGIN_FRAME
    opaque = login_form(chain=(ORIGIN, "null"))
    assert pw_decision(fields=opaque).reason is Reason.CROSS_ORIGIN_FRAME


def test_frame_chain_must_start_at_the_reported_top():
    lying = login_form(chain=("https://m.instagram.com",))
    assert pw_decision(fields=lying).reason is Reason.CROSS_ORIGIN_FRAME


def test_empty_frame_chain_is_refused():
    f = fld("pass", type="password", chain=None)
    f = type(f)(**{**f.__dict__, "frame_origins": ()})
    assert pw_decision(field=f, fields=[f]).reason is Reason.CROSS_ORIGIN_FRAME


# ---- sinks and kinds -----------------------------------------------------------

@pytest.mark.parametrize("kind,sink,ok", [
    (EntryKind.PASSWORD, Sink.FORM_PASSWORD, True),
    (EntryKind.API_KEY, Sink.FORM_PASSWORD, False),
    (EntryKind.COOKIE, Sink.FORM_PASSWORD, False),
    (EntryKind.NOTE, Sink.FORM_PASSWORD, False),
    (EntryKind.TOTP, Sink.FORM_PASSWORD, False),
    (EntryKind.PASSWORD, Sink.AUTH_HEADER, False),
    (EntryKind.NOTE, Sink.AUTH_HEADER, False),
    (EntryKind.NOTE, Sink.COOKIE, False),
])
def test_kind_sink_matrix(kind, sink, ok):
    d = decide(entry(kind=kind), lane_id="biz-a/instagram", sink=sink, url=TOP,
               field=login_form()[2], fields=login_form())
    assert d.allowed is ok
    if not ok:
        assert d.reason is Reason.KIND_SINK


def test_api_key_goes_only_into_an_auth_header_on_its_domain():
    key = entry(kind=EntryKind.API_KEY, domain="example.com", has_username=False)
    ok = decide(key, lane_id="biz-a/instagram", sink=Sink.AUTH_HEADER, url="https://api.example.com/v1/x")
    assert ok.allowed
    bad = decide(key, lane_id="biz-a/instagram", sink=Sink.AUTH_HEADER, url="https://api.example.com.evil.io/")
    assert bad.reason is Reason.DOMAIN_MISMATCH
    plain = decide(key, lane_id="biz-a/instagram", sink=Sink.AUTH_HEADER, url="http://api.example.com/")
    assert plain.reason is Reason.INSECURE_SCHEME


def test_username_needs_a_username_and_otp_needs_a_source():
    assert pw_decision(sink=Sink.FORM_USERNAME, e=entry(has_username=False)).reason is Reason.NO_USERNAME
    otp_fields = [fld("otp", type="text", autocomplete="one-time-code", form="f2")]
    d = decide(entry(), lane_id="biz-a/instagram", sink=Sink.FORM_OTP, url=TOP, field=otp_fields[0],
               fields=otp_fields)
    assert d.reason is Reason.NO_CODE_SOURCE
    assert decide(entry(has_totp=True), lane_id="biz-a/instagram", sink=Sink.FORM_OTP, url=TOP,
                  field=otp_fields[0], fields=otp_fields).allowed


def test_imap_login_sink_requires_tls_and_the_mailbox_domain():
    mailbox = entry(domain="gmail.com", lanes=["biz-a/*"])
    assert decide(mailbox, lane_id="biz-a/instagram", sink=Sink.IMAP_LOGIN, url="imaps://imap.gmail.com:993").allowed
    assert decide(mailbox, lane_id="biz-a/instagram", sink=Sink.IMAP_LOGIN,
                  url="imap://imap.gmail.com:143").reason is Reason.INSECURE_SCHEME
    assert decide(mailbox, lane_id="biz-a/instagram", sink=Sink.IMAP_LOGIN,
                  url="imaps://imap.evil.io:993").reason is Reason.DOMAIN_MISMATCH


# ---- structural field roles ----------------------------------------------------

def test_password_never_goes_into_a_text_field():
    fields = login_form()
    d = decide(entry(), lane_id="biz-a/instagram", sink=Sink.FORM_PASSWORD, url=TOP, field=fields[1],
               fields=fields)
    assert d.reason is Reason.FIELD_ROLE_MISMATCH


def test_arbitrary_inputs_are_not_login_fields():
    comment = fld("comment", type="text", name="comment", form="c")
    search = fld("q", type="search", name="q")
    textarea = fld("ta", tag="textarea", type="", name="password")
    for f in (comment, search, textarea):
        d = decide(entry(), lane_id="biz-a/instagram", sink=Sink.FORM_PASSWORD, url=TOP, field=f, fields=[f])
        assert d.reason in (Reason.NOT_A_LOGIN_FIELD, Reason.FIELD_NOT_INTERACTABLE), f.locator
        d = decide(entry(), lane_id="biz-a/instagram", sink=Sink.FORM_USERNAME, url=TOP, field=f, fields=[f])
        assert not d.allowed


@pytest.mark.parametrize("attr", [{"visible": False}, {"disabled": True}, {"readonly": True}])
def test_hidden_disabled_readonly_fields_are_refused(attr):
    f = fld("pass", type="password", form="f1", **attr)
    d = decide(entry(), lane_id="biz-a/instagram", sink=Sink.FORM_PASSWORD, url=TOP, field=f, fields=[f])
    assert d.reason is Reason.FIELD_NOT_INTERACTABLE


def test_classify_typical_forms():
    roles = classify_fields(login_form())
    assert roles == {"user": FieldRole.USERNAME, "pass": FieldRole.PASSWORD}

    # no autocomplete hints: the text input right before the password in the same form
    plain = [fld("n", type="text", name="newsletter", form="news"),
             fld("u", type="email", form="login"), fld("p", type="password", form="login")]
    assert classify_fields(plain) == {"u": FieldRole.USERNAME, "p": FieldRole.PASSWORD}

    # sign-up form: new-password is not a login password
    signup = [fld("u", type="text", autocomplete="username", form="s"),
              fld("p", type="password", autocomplete="new-password", form="s")]
    assert classify_fields(signup) == {"u": FieldRole.USERNAME}

    # username-first step
    first = [fld("id", type="email", name="identifier", form="g"), fld("q", type="search")]
    assert classify_fields(first) == {"id": FieldRole.USERNAME}

    # a lone text box with no structural hint is nothing
    assert classify_fields([fld("x", type="text", name="title", form="t")]) == {}


def test_classify_one_time_code_fields():
    explicit = [fld("c", type="text", autocomplete="one-time-code", form="o")]
    assert classify_fields(explicit) == {"c": FieldRole.OTP}
    numeric = [fld("c", type="text", inputmode="numeric", maxlength=6, name="verificationCode", form="o")]
    assert classify_fields(numeric) == {"c": FieldRole.OTP}
    split = [fld(f"d{i}", type="text", inputmode="numeric", maxlength=1, form="o") for i in range(6)]
    roles = classify_fields(split)
    assert set(roles.values()) == {FieldRole.OTP} and len(roles) == 6
    assert [f.locator for f in otp_group(split, split[0])] == [f"d{i}" for i in range(6)]
    # a numeric field next to a password field is not a code (e.g. a PIN on a payment form)
    mixed = [fld("pin", type="tel", name="pin", maxlength=6, form="f"), fld("p", type="password", form="f")]
    assert classify_fields(mixed).get("pin") is not FieldRole.OTP


def test_decision_check_raises_with_reason_and_no_values():
    d = pw_decision(url="https://evil.io/", fields=login_form("https://evil.io"))
    with pytest.raises(ResolutionDenied) as exc:
        d.check()
    assert exc.value.reason == "domain_mismatch" and exc.value.code == "vault_denied"
    assert "evil.io" in exc.value.message


def test_matching_entries_filters_by_lane_origin_and_kind():
    entries = [entry(id="a", name="a"), entry(id="b", name="b", lanes=["biz-b/instagram"]),
               entry(id="c", name="c", domain="reddit.com"), entry(id="d", name="d", kind=EntryKind.API_KEY)]
    assert [e.id for e in matching_entries(entries, lane_id="biz-a/instagram", url=TOP)] == ["a"]
    assert matching_entries(entries, lane_id="biz-a/instagram", url="http://www.instagram.com/") == []
