"""Sign-in and captcha hosts on a lane's site list (openajar.node.egress): the shared identity
group in every suggestion, `frame:` entries that admit a host only as a subframe, Google's
country domains (`accounts.google.@cc`), and the live view's block notices while a human has
control. The real browser is in tests/node/test_egressux_node.py."""

from __future__ import annotations

import pytest
from test_egress import AGENT, OWNER, FakeFrame, FakeRequest, make_guard, settle

from openajar.core.errors import EgressRefused
from openajar.core.models import LaneSpec, validate_sites
from openajar.node import egress
from openajar.node.egress import (
    GOOGLE_COUNTRY_SUFFIXES,
    IDENTITY_SITES,
    PLATFORM_SITES,
    SUGGESTED_SITES,
    Driver,
    allow_entry,
    in_sites,
    judge,
    site_match,
    site_scope,
)

LANE_SITES = ("reddit.com", "*.reddit.com", *IDENTITY_SITES)
EM_DASH = "—"


# ---- the site-list syntax --------------------------------------------------------------------

def test_frame_entries_and_the_country_form_are_validated_and_normalized():
    assert validate_sites(["FRAME:www.Google.com", "frame:www.google.com", "Accounts.Google.@CC",
                           "frame:https://challenges.cloudflare.com/x", "frame:*.hcaptcha.com"]) == [
        "frame:www.google.com", "accounts.google.@cc", "frame:challenges.cloudflare.com", "frame:*.hcaptcha.com"]
    assert validate_sites(["google.@cc", "*.google.@cc", "frame:www.google.@cc"]) == [
        "google.@cc", "*.google.@cc", "frame:www.google.@cc"]
    # never a bare TLD wildcard, and @cc only after google.
    for bad in ("@cc", "*.@cc", "accounts.@cc", "accounts.notgoogle.@cc", "accounts.google.@cc.com",
                "accounts.google.@cc.@cc", "frame:", "frame:*", "frame:bad host.com", "evil.@cc"):
        with pytest.raises(ValueError):
            validate_sites([bad])
    assert LaneSpec(id="personal/reddit", sites=["frame:www.google.com"]).sites == ["frame:www.google.com"]


def test_google_country_domains_match_only_googles_own_suffixes():
    assert len(GOOGLE_COUNTRY_SUFFIXES) == 187 and {"com", "lt", "co.uk", "com.au", "cat"} <= GOOGLE_COUNTRY_SUFFIXES
    for host in ("accounts.google.lt", "accounts.google.com", "accounts.google.co.uk", "accounts.google.com.br",
                 "accounts.google.cat"):
        assert site_match("accounts.google.@cc", host), host
    for host in ("accounts.google.evil", "accounts.google.lt.evil.example", "xaccounts.google.lt",
                 "accounts.notgoogle.lt", "accounts.evilgoogle.lt", "google.lt", "www.google.lt",
                 "accounts.google.co", "accounts.google.uk"):
        assert not site_match("accounts.google.@cc", host), host
    assert site_match("*.google.@cc", "www.google.de") and not site_match("*.google.@cc", "google.de")
    assert site_match("google.@cc", "google.de") and not site_match("google.@cc", "www.google.de")


def test_every_suggestion_carries_the_identity_group_and_is_valid():
    assert set(SUGGESTED_SITES) == set(PLATFORM_SITES) == {"instagram", "facebook", "reddit", "linkedin", "medium"}
    for platform, sites in SUGGESTED_SITES.items():
        assert validate_sites(list(sites)) == list(sites), platform
        assert sites[-len(IDENTITY_SITES):] == IDENTITY_SITES, platform
    frames = {s for s in IDENTITY_SITES if s.startswith("frame:")}
    assert {"frame:www.google.com", "frame:www.recaptcha.net", "frame:recaptcha.net", "frame:*.hcaptcha.com",
            "frame:challenges.cloudflare.com", "frame:*.arkoselabs.com", "frame:idmsa.apple.com"} <= frames
    pages = set(IDENTITY_SITES) - frames
    assert {"accounts.google.com", "accounts.google.@cc", "accounts.youtube.com", "appleid.apple.com"} <= pages
    assert "fbsbx.com" in SUGGESTED_SITES["instagram"]  # Meta's captcha frame host


# ---- verdicts --------------------------------------------------------------------------------

def test_a_frame_entry_admits_subframes_only():
    assert site_scope(LANE_SITES, "www.google.com") == "frame"
    assert site_scope(LANE_SITES, "accounts.google.lt") == "page"
    assert site_scope(LANE_SITES, "evil.example") is None
    assert in_sites(LANE_SITES, "www.google.com", frame=True) and not in_sites(LANE_SITES, "www.google.com")
    url = "https://www.google.com/recaptcha/api2/anchor?k=site-key"
    open_agent = Driver(kind="agent", id="claude", grant_id="g0")  # its grant sets no list of its own
    for driver in (open_agent, None):
        assert judge(url, "GET", navigation=True, sites=LANE_SITES, driver=driver, human=False, frame=True) is None
        top = judge(url, "GET", navigation=True, sites=LANE_SITES, driver=driver, human=False)
        assert top is not None and top.rule == "lane_sites" and "only as a frame" in top.reason
    # a person who took control is never refused (the trace notes what was off the list)
    assert judge(url, "GET", navigation=True, sites=LANE_SITES, driver=OWNER, human=True) is None
    # a grant's own list still applies to an agent, frames included
    assert judge(url, "GET", navigation=True, sites=LANE_SITES, driver=AGENT, human=False,
                 frame=True).rule == "grant_egress"
    # a captcha frame posts its answer to its own host: not exfiltration by form
    assert judge("https://www.google.com/recaptcha/api2/userverify", "POST", navigation=False, sites=LANE_SITES,
                 driver=open_agent, human=False) is None
    assert judge("https://evil.example/x", "POST", navigation=False, sites=LANE_SITES, driver=open_agent,
                 human=False).rule == "non_get"
    # the redirect through Google's country domain of the exit IP
    assert judge("https://accounts.google.lt/accounts/SetSID?ssdc=1", "GET", navigation=True, sites=LANE_SITES,
                 driver=None, human=True) is None


def test_an_agent_cannot_open_a_frame_only_host_as_a_page(tmp_path):
    guard, *_ = make_guard(tmp_path, sites=LANE_SITES, driver=Driver(kind="agent", id="claude", grant_id="g0"))
    with pytest.raises(EgressRefused) as refused:
        guard.check_url("https://www.google.com/search?q=secret")
    assert "only as a frame" in str(refused.value) and "secret" not in str(refused.value)
    guard.check_url("https://accounts.google.com/v3/signin/identifier")  # a sign-in page is a page


async def test_the_listener_tells_subframes_from_pages(tmp_path):
    guard, ctx, *_ = make_guard(tmp_path, sites=LANE_SITES, driver=None)
    top = FakeFrame("https://www.reddit.com/login/")
    sub = FakeFrame("about:blank", parent=top)
    ctx.emit("request", FakeRequest("https://www.google.com/recaptcha/api2/anchor?k=x", nav=True, frame=sub))
    hop = FakeRequest("https://www.google.com/recaptcha/api2/bframe", nav=True, frame=sub, hop=object())
    ctx.emit("request", hop)  # a redirect hop inside the frame is still a frame
    await settle()
    assert guard.stats["blocked"] == 0 and sub.evaluated == []
    ctx.emit("request", FakeRequest("https://www.google.com/", nav=True, frame=top))
    await settle()
    assert guard.stats["blocked"] == 1 and top.evaluated == ["window.stop()"]
    assert guard.blocks_since(0)[0].frame is False


# ---- notices in the live view ----------------------------------------------------------------

def test_the_allow_entry_for_a_refused_document():
    assert allow_entry("www.google.com", True) == "frame:www.google.com"
    assert allow_entry("accounts.google.lt", False) == "accounts.google.@cc"
    assert allow_entry("accounts.google.com", False) == "accounts.google.com"
    assert allow_entry("www.google.co.uk", True) == "frame:www.google.@cc"
    assert allow_entry("evil.google.example", False) == "evil.google.example"
    assert allow_entry("?", False) is None and allow_entry("", True) is None
    for entry in ("frame:www.google.com", "accounts.google.@cc", "frame:www.google.@cc"):
        assert validate_sites([entry]) == [entry]  # every suggestion is a valid entry


async def test_an_off_list_document_while_a_human_has_control_sends_one_notice(tmp_path):
    guard, ctx, *_ = make_guard(tmp_path, sites=("reddit.com", "*.reddit.com"), driver=None)
    notices: list[dict] = []
    guard.on_notice = notices.append
    guard.human_in_control = lambda: True
    top = FakeFrame("https://www.reddit.com/login/")
    sub = FakeFrame("about:blank", parent=top)
    url = "https://www.google.com/recaptcha/api2/anchor?k=site-key&token=SECRET"
    for _ in range(3):
        ctx.emit("request", FakeRequest(url, nav=True, frame=sub))
    ctx.emit("request", FakeRequest("https://accounts.google.lt/accounts/SetSID?sid=SECRET", nav=True, frame=top,
                                    hop=object()))
    await settle()
    assert [n["host"] for n in notices] == ["www.google.com", "accounts.google.lt"]
    first, hop = notices
    assert first["kind"] == "subframe" and first["frame"] is True and first["rule"] == "noted"
    assert guard.stats["blocked"] == 0  # let through: the human decides; agents would be refused
    assert first["path"] == "/recaptcha/api2/anchor" and first["allow"] == "frame:www.google.com"
    assert first["command"] == "openajar lane sites biz-a/instagram add frame:www.google.com"
    assert hop["kind"] == "redirect" and hop["allow"] == "accounts.google.@cc"
    text = repr(notices)
    assert "SECRET" not in text and "site-key" not in text and "?" not in text and EM_DASH not in text


async def test_notices_are_rate_limited_and_count_what_they_fold(tmp_path):
    guard, ctx, _events, clock = make_guard(tmp_path, sites=("reddit.com",), driver=None)
    notices: list[dict] = []
    guard.on_notice = notices.append
    guard.human_in_control = lambda: True
    top = FakeFrame("https://www.reddit.com/")
    for _ in range(4):
        ctx.emit("request", FakeRequest("https://a.example/", nav=True, frame=top))
    assert len(notices) == 1
    clock.now += egress.NOTICE_EVERY_S
    ctx.emit("request", FakeRequest("https://a.example/", nav=True, frame=top))
    assert len(notices) == 2 and notices[-1]["repeats"] == 3
    for i in range(10):  # many hosts at once: at most NOTICE_BURST in a window (a.example's second is one)
        ctx.emit("request", FakeRequest(f"https://h{i}.example/", nav=True, frame=top))
    assert len(notices) == 1 + egress.NOTICE_BURST
    clock.now += egress.NOTICE_WINDOW_S
    ctx.emit("request", FakeRequest("https://h9.example/", nav=True, frame=top))
    assert len(notices) == 2 + egress.NOTICE_BURST and notices[-1]["host"] == "h9.example"
    await settle()


async def test_no_notice_while_an_agent_drives_or_for_requests(tmp_path):
    guard, ctx, *_ = make_guard(tmp_path, sites=("reddit.com",), driver=AGENT)
    notices: list[dict] = []
    guard.on_notice = notices.append
    ctx.emit("request", FakeRequest("https://evil.example/", nav=True, frame=FakeFrame("https://www.reddit.com/")))
    ctx.emit("request", FakeRequest("https://evil.example/x", "POST"))
    await settle()
    assert guard.stats["blocked"] == 2 and notices == []
    guard.human_in_control = lambda: True
    guard.on_notice = lambda notice: (_ for _ in ()).throw(RuntimeError("the live view went away"))
    ctx.emit("request", FakeRequest("https://evil.example/", nav=True, frame=FakeFrame("https://www.reddit.com/")))
    await settle()  # a failing listener never breaks the guard; a human in control is let through
    assert guard.stats["blocked"] == 2
