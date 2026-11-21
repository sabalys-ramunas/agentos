"""Edge cases in grant scope: how a domain string is read, and what an empty scope reaches.

Bugs are marked xfail(strict=True) with a BUG number, as in test_policy_edge_authorize.py.
"""

from __future__ import annotations

import pytest
from test_policy_support import make_service

from openajar.core import globs
from openajar.core.errors import InvalidRequest
from openajar.core.models import Effect
from openajar.policy.errors import Denied


@pytest.mark.parametrize("raw", [
    "https://evil.example\\@instagram.com/post",   # WHATWG treats the backslash as a slash: host evil.example
    "instagram.com:@evil.example",                  # checked as instagram.com; a tool that adds https:// reaches evil
])
def test_ambiguous_domain_strings_are_refused(raw):
    with pytest.raises(InvalidRequest):
        globs.normalize_domain(raw)


@pytest.mark.parametrize("raw,host", [
    ("https://Instagram.com:443/p?x=1", "instagram.com"),
    ("instagram.com.", "instagram.com"),
    ("www.instagram.com/some/path", "www.instagram.com"),
])
def test_ordinary_urls_and_hosts_normalize_to_their_host(raw, host):
    assert globs.normalize_domain(raw) == host


async def test_an_explicit_empty_domain_list_reaches_no_domain(tmp_path):
    svc, _ = make_service(tmp_path)
    try:
        _, tok = svc.issue(subject={"id": "a"}, workspaces=["biz-a"], domains=[], ceiling=Effect.READ)
        p, g = svc.resolve(tok)
        ctx = svc.context(g, principal=p, tool="t", token=tok)
        with pytest.raises(Denied) as e:
            await ctx.authorize(Effect.READ, lane_id="biz-a/x", domain="instagram.com")
        assert e.value.reason == "domain"
    finally:
        svc.close()


def test_a_narrowed_grant_cannot_rename_its_subject(tmp_path):
    svc, _ = make_service(tmp_path)
    try:
        _, tok = svc.issue(subject={"id": "claude-code"}, workspaces=["biz-a"], ceiling=Effect.READ)
        with pytest.raises(InvalidRequest, match="widen"):
            svc.attenuate(tok, subject={"id": "owner", "kind": "human"})
    finally:
        svc.close()


@pytest.mark.parametrize("raw", [
    "https:///evil.example/",              # browsers skip the extra slash; urlsplit sees no host
    "https:evil.example",                  # browsers read evil.example as the host
    "https://a@b@evil.example/",           # two @: refused rather than guessed
    "https://instagram%2Ecom/",            # browsers percent-decode the host
    "https://0x7f.1/",                     # browsers read 127.0.0.1
    "https://2130706433/",                 # browsers read 127.0.0.1
    "https://insta\tgram.com/",            # browsers drop tabs and newlines inside a URL
    "javascript://instagram.com/%0aalert(1)",
    "evil.example\\.instagram.com",
])
def test_strings_a_browser_could_read_differently_are_refused(raw):
    with pytest.raises(InvalidRequest):
        globs.normalize_domain(raw)


@pytest.mark.parametrize("raw,host", [
    ("https://user:pw@Instagram.com:443/p", "instagram.com"),
    ("https://ＥＸＡＭＰＬＥ.com/", "example.com"),        # fullwidth letters map like a browser maps them
    ("https://x.straße.de/", "x.xn--strae-oqa.de"),
    ("https://127.0.0.1:9000/", "127.0.0.1"),
    ("instagram.com:443", "instagram.com"),
])
def test_hosts_are_what_a_browser_contacts(raw, host):
    assert globs.normalize_domain(raw) == host


@pytest.mark.parametrize("url", [
    "https://evil.example\\@instagram.com/post",
    "https://instagram.com\\@allowed.example/",
    "https:///evil.example/",
    "https://0x7f.1/",
    "https://@instagram.com/",
])
def test_the_node_refuses_the_same_urls(url):
    from openajar.node.tools import normalize_url

    with pytest.raises(InvalidRequest):
        normalize_url(url)


def test_the_node_authorizes_the_host_the_policy_reads():
    from openajar.node.tools import normalize_url, url_host

    for raw in ("https://x.straße.de/a?b=1", "HTTPS://Instagram.com:443/p", "reddit.com/r/x"):
        assert globs.normalize_domain(url_host(normalize_url(raw))) == globs.normalize_domain(raw)


@pytest.mark.parametrize("subject", [
    {"id": "worker"},                          # a label: becomes claude-code/worker
    {"id": "claude-code/worker"},              # already under the parent
    {"id": "worker", "kind": "brain"},
    {"id": "claude-code", "thread": "T-9"},    # the same identity, pinned to a thread
])
def test_a_narrowed_grant_may_name_a_sub_agent(tmp_path, subject):
    svc, _ = make_service(tmp_path)
    try:
        _, tok = svc.issue(subject={"id": "claude-code"}, workspaces=["biz-a"], ceiling=Effect.READ)
        p, _ = svc.resolve(svc.attenuate(tok, subject=subject)[1])
        assert p.id in ("claude-code", "claude-code/worker") and p.kind in ("agent", "brain")
        assert p.owner == "owner"
    finally:
        svc.close()


@pytest.mark.parametrize("sub", [
    {"id": "owner", "kind": "human"},
    {"id": "claude-code/x", "kind": "panel"},
    {"id": "codex"},                           # not under the parent's id
    {"id": "claude-code/"},                    # an empty label
    {"id": "claude-codex"},                    # a prefix is not a sub-agent
])
def test_offline_children_cannot_take_another_identity(tmp_path, sub):
    from openajar.policy import tokens
    from openajar.policy.errors import TokenInvalid

    svc, _ = make_service(tmp_path)
    try:
        g, tok = svc.issue(subject={"id": "claude-code"}, workspaces=["biz-a"], ceiling=Effect.READ)
        child = tokens.append(tok, {"id": "gr_ssssssssssssssss", "v": 2, "parent": g.id,
                                    "iat": int(svc.clock()), "sub": sub})
        with pytest.raises(TokenInvalid):
            svc.resolve(child)
    finally:
        svc.close()


def test_a_brain_child_is_a_sub_agent_of_its_parent(tmp_path):
    svc, _ = make_service(tmp_path)
    try:
        g, _ = svc.issue(subject={"id": "claude-code", "thread": "T-1"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
        out = svc.mint_child_token(g.principal(), purpose="brain task bt_7", workspace="biz-a", lanes=["biz-a/x"])
        p, _ = svc.resolve(out["token"])
        assert (p.kind, p.id, p.thread) == ("brain", "claude-code/brain-task-bt_7", "T-1")
    finally:
        svc.close()
