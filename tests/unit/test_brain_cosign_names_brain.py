"""A co-sign for a brain task names one brain (provider, model, sign-in), and covers only it: a
retry on another brain, or the same name signed in again or set to another model, asks again."""

from __future__ import annotations

import os
import signal

import pytest
from testkit.brains import OAUTH, FakeCtx, add, make_service

from openajar.brains.models import (
    BrainKind,
    BrainState,
    BrainView,
    Budget,
    Capabilities,
    ContextBundle,
    GrantView,
    Locality,
    Privacy,
    Requirements,
)
from openajar.brains.router import route
from openajar.core.errors import ApprovalPending
from openajar.core.models import Effect, Principal
from openajar.core.runtime import Runtime

P = Principal(id="claude-code", thread="T-1")
PRIVATE = ContextBundle(taint=["private"])


@pytest.fixture
def svc(tmp_path):
    s = make_service(tmp_path, runtime=Runtime())
    yield s
    for a in s.store.q("SELECT shim_pid FROM attempts WHERE outcome IS NULL AND shim_pid IS NOT NULL"):
        try:
            os.killpg(a["shim_pid"], signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


async def two_clouds(svc):
    await add(svc, "claude-max", "subscription", "anthropic")
    await add(svc, "claude-team", "subscription", "anthropic", order=200)


async def cosigned(svc):
    ctx = FakeCtx(svc.runtime, P)
    r = await svc.submit(P, prompt="FAKE:ok", workspace="personal", context_bundle=PRIVATE, ctx=ctx)
    return ctx.authorized[-1], svc.store.get_task(r["id"])


async def test_the_card_names_one_brain_with_its_provider_and_sign_in(svc):
    await two_clouds(svc)
    ctx = FakeCtx(svc.runtime, P, pending={Effect.IRREVERSIBLE})
    with pytest.raises(ApprovalPending):
        await svc.submit(P, prompt="FAKE:ok", workspace="personal", context_bundle=PRIVATE, ctx=ctx)
    card = ctx.authorized[-1]
    assert card["content"]["brains"] == ["claude-max"]
    brain = card["content"]["brain"]
    assert brain["name"] == "claude-max" and brain["provider"] == "anthropic" and brain["identity"]
    assert "claude-max (anthropic" in card["summary"]


async def test_a_retry_on_another_brain_asks_again(svc):
    await two_clouds(svc)
    card, rec = await cosigned(svc)
    assert rec.grant.private_cloud_brains == [card["content"]["brain"]["identity"]]
    spec = svc.get_spec("claude-max")
    svc.store.put_brain(spec.model_copy(update={"enabled": False}))  # the named brain is gone for now
    again = await svc.route_for(rec)
    assert again.action == "needs_approval" and not again.candidates
    assert any(r.name == "claude-team" and "named another brain" in r.reason for r in again.rejected)


async def test_the_same_name_signed_in_again_asks_again(svc):
    await two_clouds(svc)
    _, rec = await cosigned(svc)
    assert (await svc.route_for(rec)).candidates[0].name == "claude-max"
    await svc.set_credential("claude-max", OAUTH)  # perhaps another subscription
    assert (await svc.route_for(rec)).action == "needs_approval"


async def test_the_same_name_on_another_model_asks_again(svc):
    await two_clouds(svc)
    _, rec = await cosigned(svc)
    spec = svc.get_spec("claude-max")
    svc.store.put_brain(spec.model_copy(update={"model": "another-model"}))
    assert (await svc.route_for(rec)).action == "needs_approval"


def view(name, identity, kind=BrainKind.METERED):
    return BrainView(name=name, kind=kind, locality=Locality.CLOUD, state=BrainState.READY, caps=Capabilities(),
                     identity=identity)


def test_a_metered_co_sign_covers_only_the_brain_it_named():
    grant = GrantView(allow_metered=True, metered_brains=["a:1"])
    brains = [view("a", "a:1"), view("b", "b:1")]
    d = route(Requirements(), privacy=Privacy.FULL_CLOUD, grant=grant, brains=brains, budget=Budget(max_usd=2))
    assert [c.name for c in d.candidates] == ["a"]
    d = route(Requirements(), privacy=Privacy.FULL_CLOUD, grant=grant, brains=brains, budget=Budget(max_usd=2),
              exclude={"a"})
    assert d.action == "needs_approval"


def test_a_grant_that_allows_metered_on_its_own_covers_every_metered_brain():
    brains = [view("a", "a:1"), view("b", "b:1")]
    d = route(Requirements(), privacy=Privacy.FULL_CLOUD, grant=GrantView(allow_metered=True), brains=brains,
              budget=Budget(max_usd=2))
    assert sorted(c.name for c in d.candidates) == ["a", "b"]


def test_a_private_data_co_sign_kept_from_before_names_no_brain():
    """A task co-signed before co-signs named their brain asks again when it is routed again."""
    grant = GrantView(allow_private_cloud=True)
    d = route(Requirements(), privacy=Privacy.FULL_CLOUD, taint={"private"}, grant=grant,
              brains=[view("a", "a:1", kind=BrainKind.SUBSCRIPTION)])
    assert d.action == "needs_approval"


# ---- found by attacking it ----

async def test_the_card_names_the_brain_the_router_would_pick(svc):
    """The first brain listed needs a sign-in, so the yes would run on the second: the card names it."""
    from openajar.brains.credentials import brain_ref

    await add(svc, "claude-max", "subscription", "anthropic")
    await add(svc, "claude-team", "subscription", "anthropic", order=200)
    await svc.creds.delete(brain_ref("claude-max"))
    svc._cred_cache.clear()
    ctx = FakeCtx(svc.runtime, P, pending={Effect.IRREVERSIBLE})
    with pytest.raises(ApprovalPending):
        await svc.submit(P, prompt="FAKE:ok", workspace="personal", context_bundle=PRIVATE, ctx=ctx)
    assert ctx.authorized[-1]["content"]["brain"]["name"] == "claude-team"


async def test_a_sign_in_after_the_route_is_caught_where_the_credential_is_read(svc):
    await two_clouds(svc)
    _, rec = await cosigned(svc)
    spec = svc.get_spec("claude-max")
    assert svc._cosign_problem(rec.grant, rec.taint, spec) is None
    await svc.set_credential("claude-max", OAUTH)
    assert "changed since the co-sign" in svc._cosign_problem(rec.grant, rec.taint, spec)


async def test_a_codex_refresh_never_overwrites_a_newer_sign_in(tmp_path):
    import json

    from testkit.brains import FakeVault

    from openajar.brains.credentials import brain_ref

    s = make_service(tmp_path, runtime=Runtime(vault=FakeVault()))
    auth = json.dumps({"tokens": {"access_token": "at-" + "A" * 30, "refresh_token": "rt-a"}})
    await add(s, "codex-plus", "subscription", "openai", credential=auth)
    spec = s.get_spec("codex-plus")
    rec = type("Rec", (), {"id": "t1"})()
    s.store.set_kv("attempt_signin:t1", s._signin("codex-plus"))
    newer = json.dumps({"tokens": {"access_token": "at-" + "B" * 30, "refresh_token": "rt-b"}})
    await s.set_credential("codex-plus", newer)  # the owner signs in again while the attempt runs
    home = s.task_dir("t1") / "home" / ".codex"
    home.mkdir(parents=True)
    (home / "auth.json").write_text(json.dumps({"tokens": {"access_token": "at-" + "C" * 30, "refresh_token": "rt-a2"}}))
    await s._after_attempt_files(rec, spec)
    assert json.loads(await s.creds.get(brain_ref("codex-plus")))["tokens"]["refresh_token"] == "rt-b"
