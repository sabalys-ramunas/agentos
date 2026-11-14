"""Security review of grant evaluation, scope matching, taint, approvals and budgets.

A test marked `xfail(strict=True, reason="SEC-<n>: ...")` demonstrates an open finding:
it fails today and passes once the finding is fixed (then drop the marker). The other
tests pin properties that hold and must keep holding.
"""

from __future__ import annotations

import threading

import idna
import pytest
from test_policy_support import T0, make_service

from agentos.core import globs
from agentos.core.errors import ApprovalPending, InvalidRequest
from agentos.core.models import Effect, Principal
from agentos.policy import tokens
from agentos.policy.context import CallMeta
from agentos.policy.errors import Denied

LANE = "biz-a/x"


@pytest.fixture
def svc(tmp_path):
    s, _ = make_service(tmp_path)
    yield s
    s.close()


def issue(svc, **kw):
    kw.setdefault("subject", {"id": "agent-1"})
    kw.setdefault("workspaces", ["biz-a"])
    kw.setdefault("ceiling", Effect.COMMIT)
    return svc.issue(**kw)


def child(parent_id: str, letter: str, **fields) -> dict:
    return {"id": f"gr_{letter * 16}", "v": 2, "parent": parent_id, "iat": int(T0), **fields}


async def pending(coro) -> str:
    with pytest.raises(ApprovalPending) as e:
        await coro
    return e.value.approval_id


def post(ctx, caption: str = "Autumn"):
    return ctx.authorize(Effect.COMMIT, lane_id=LANE, domain="instagram.com", summary="Post a caption",
                         content={"caption": caption})


# ------------------------------------------------------------------ scope matching (holds)

LANES = [
    ("biz-a/x", True), ("biz-a/instagram", True), ("biz-b/instagram", True), ("biz-b/insta", True),
    ("biz-ab/x", False), ("biz-a-x/y", False), ("biz-b/inst", False), ("biz-bb/instagram", False),
    ("biz-b/x", False),
    ("BIZ-A/x", None), ("biz-a/x\n", None), ("biz-a/x/y", None), ("bíz-a/x", None), ("biz-a/", None),
    ("/x", None), ("biz‐a/x", None), ("biz-a/x ", None),
]


@pytest.mark.parametrize("lane,ok", LANES)
async def test_lane_scope_matches_exactly(svc, lane, ok):
    _, tok = issue(svc, workspaces=["biz-a"], lanes=["biz-b/insta*"])
    ctx = svc.context_for_token(tok, tool="t")
    if ok:
        await ctx.authorize(Effect.READ, lane_id=lane)
        return
    with pytest.raises(Denied if ok is False else InvalidRequest):
        await ctx.authorize(Effect.READ, lane_id=lane)


DOMAINS = [
    ("instagram.com", True), ("INSTAGRAM.COM.", True), ("https://user:pw@instagram.com:443/p", True),
    ("a.example.com", True), ("a.b.example.com", True),
    ("badinstagram.com", False), ("instagram.com.evil.net", False), ("example.com", False),
    ("evilexample.com", False), ("https://instagram.com@evil.net/", False), ("https://evil.net/?instagram.com", False),
    ("https://evil.net#@instagram.com", False), ("inst agram.com", None), ("-instagram.com", None),
]


@pytest.mark.parametrize("domain,ok", DOMAINS)
async def test_domain_scope_matches_exactly(svc, domain, ok):
    _, tok = issue(svc, domains=["instagram.com", "*.example.com"])
    ctx = svc.context_for_token(tok, tool="t")
    if ok:
        await ctx.authorize(Effect.READ, lane_id=LANE, domain=domain)
        return
    with pytest.raises(Denied if ok is False else InvalidRequest):
        await ctx.authorize(Effect.READ, lane_id=LANE, domain=domain)


@pytest.mark.parametrize("kw", [
    {"lanes": ["biz-*/x"]}, {"lanes": ["biz-a*/x"]}, {"lanes": ["*"]}, {"workspaces": ["biz-a*"]},
    {"domains": ["*.instagram.com"]}, {"domains": ["*"]}, {"egress": ["*.instagram.com"]},
])
def test_pattern_containment_refuses_wider_children(svc, kw):
    _, tok = issue(svc, domains=["instagram.com"], egress=["instagram.com"])
    with pytest.raises(InvalidRequest, match="widen"):
        svc.attenuate(tok, **kw)


# ------------------------------------------------------------------ races and approval binding (holds)

def test_budget_cannot_be_overspent_by_racing_processes(tmp_path):
    a, _ = make_service(tmp_path)
    b, _ = make_service(tmp_path)  # a second process on the same home
    _, tok = issue(a, budget={"usd": 5})
    grants = [(a, a.resolve(tok)[1]), (b, b.resolve(tok)[1])]
    wins: list[int] = []
    barrier = threading.Barrier(8)

    def worker(svc, grant):
        barrier.wait()
        for _ in range(5):
            try:
                svc.reserve(grant, usd=1.0)
                wins.append(1)
            except Denied:
                pass

    threads = [threading.Thread(target=worker, args=g) for g in grants * 4]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(wins) == 5
    assert a.remaining(grants[0][1])["usd"] == 0


async def test_one_approval_is_consumed_once_under_a_race(tmp_path):
    a, _ = make_service(tmp_path)
    b, _ = make_service(tmp_path)
    _, tok = issue(a)
    ctx = a.context_for_token(tok, tool="t")
    ap = await pending(post(ctx))
    a.approvals.decide(ap, "approve", by="owner")
    h = a.approvals.get(ap).content_hash
    got: list[object] = []
    barrier = threading.Barrier(6)

    def worker(store):
        barrier.wait()
        got.append(store.consume_approved(h, ctx.grant.id))

    threads = [threading.Thread(target=worker, args=(s.approvals,)) for s in (a, b) * 3]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(x is not None for x in got) == 1


async def test_approval_does_not_transfer_to_a_child_or_survive_revocation(svc):
    _, tok = issue(svc)
    ctx = svc.context_for_token(tok, tool="t")
    ap = await pending(post(ctx))
    svc.approvals.decide(ap, "approve", by="owner")
    _, child_tok = svc.attenuate(tok, ttl_s=3600)
    await pending(post(svc.context_for_token(child_tok, tool="t")))  # its own approval, not the parent's
    svc.revoke(ctx.grant.id)
    with pytest.raises(Denied) as e:
        await post(ctx)
    assert e.value.reason == "revoked"
    assert svc.approvals.get(ap).state == "approved"  # never consumed


# ------------------------------------------------------------------ open findings

def _attenuated(svc, tok, parent_id, how: str, **fields) -> str:
    if how == "node":
        return svc.attenuate(tok, **fields)[1]
    if "subject" in fields:
        fields["sub"] = fields.pop("subject")
    return tokens.append(tok, child(parent_id, "q", **fields))


@pytest.mark.parametrize("how", ["node", "offline"])
async def test_child_cannot_open_an_egress_its_parent_never_had(svc, how):
    g, tok = issue(svc)
    assert not g.egress_allows("evil.example")
    try:
        ctx = svc.context_for_token(_attenuated(svc, tok, g.id, how, egress=["*"]), tool="t",
                                    meta=CallMeta(session="s-1"))
    except (InvalidRequest, Denied):
        return  # fixed by refusing the child
    ctx.taint("untrusted", "private")
    await pending(ctx.authorize(Effect.DRAFT, lane_id=LANE, domain="evil.example", content={"body": "secret"}))


@pytest.mark.parametrize("fresh", ["new_mcp_session", "child_grant_without_session"])
async def test_taint_follows_the_grant_not_the_session(svc, fresh):
    _, tok = issue(svc, egress=["instagram.com"])
    first = svc.context_for_token(tok, tool="t", meta=CallMeta(session="mcp-1") if fresh == "new_mcp_session" else None)
    first.taint("untrusted", "private")
    await pending(first.authorize(Effect.DRAFT, lane_id=LANE, domain="evil.example", content={"body": "secret"}))
    if fresh == "new_mcp_session":
        later = svc.context_for_token(tok, tool="t", meta=CallMeta(session="mcp-2"))
    else:
        later = svc.context_for_token(svc.attenuate(tok, ttl_s=3600)[1], tool="t")
    await pending(later.authorize(Effect.DRAFT, lane_id=LANE, domain="evil.example", content={"body": "secret"}))


@pytest.mark.parametrize("how", ["node", "offline"])
async def test_child_cannot_take_another_identity(svc, how):
    g, tok = issue(svc)
    owner_like = {"kind": "human", "id": "owner", "owner": "owner"}
    try:
        ctx = svc.context_for_token(_attenuated(svc, tok, g.id, how, subject=owner_like), tool="t")
    except (InvalidRequest, Denied):
        return
    ap = await pending(post(ctx))
    requester = svc.approvals.get(ap).requester
    assert (ctx.principal.kind, ctx.principal.id) != ("human", "owner")
    assert (requester["kind"], requester["id"]) != ("human", "owner")


async def test_backslash_userinfo_url_is_not_read_as_the_allowed_host(svc):
    _, tok = issue(svc, domains=["allowed.example"])
    ctx = svc.context_for_token(tok, tool="t")
    with pytest.raises((Denied, InvalidRequest)):
        await ctx.authorize(Effect.READ, lane_id=LANE, domain="https://instagram.com\\@allowed.example/")


def test_pay_approve_link_is_not_published_to_a_guessable_public_topic(svc):
    try:
        svc.notifier.update_config({"ntfy": {"topic": "agentos"}, "public_base_url": "https://node.example"})
    except (ValueError, InvalidRequest):
        return  # fixed: a guessable public topic is refused when links are on
    ap = svc.approvals.create_action(
        effect="pay", tool="checkout", lane=LANE, domain="shop.example", summary="Pay 900 EUR",
        content_hash="sha256:" + "0" * 64, preview=None, requester=Principal(kind="agent", id="agent-1"),
        grant_id=None, grant_path=None, reason="always a human co-sign")
    event = {"type": "action.created", "approval_id": ap.id, "title": "biz-a/x: pay needs you",
             "body": ap.summary, "actionable": True, "expires_ts": ap.expires_at}
    labels = [a["label"] for a in svc.notifier.ntfy_payload([event])["actions"]]
    assert "Approve" not in labels


@pytest.mark.parametrize("host", ["faß.de", "https://straße.example/"])
def test_unicode_hosts_normalize_like_browsers(host):
    bare = host.removeprefix("https://").rstrip("/")
    assert globs.normalize_domain(host) == idna.encode(bare, uts46=True, transitional=False).decode()
