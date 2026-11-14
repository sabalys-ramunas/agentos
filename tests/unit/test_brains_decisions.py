"""Typed decisions: the spec's rules, what `decide` sends and refuses, how failures map to brain
states, the money, and the wall between a decision brain and a task. The endpoint is an
httpx.MockTransport throughout: nothing here reaches a network."""

from __future__ import annotations

import asyncio
import json
from email.utils import formatdate

import httpx
import pytest
from testkit.brains import API_KEY, FakeCtx, FakeNotifier, FakePolicy, add, make_service
from typer.testing import CliRunner

from agentos.brains import adapters, cliprobe, decisions
from agentos.brains import cli as brains_cli
from agentos.brains.decisions import (
    DecisionBlocked,
    DecisionOverBudget,
    DecisionRejected,
    DecisionUnavailable,
)
from agentos.brains.models import (
    BrainKind,
    BrainSpec,
    BrainState,
    Budget,
    Candidate,
    Locality,
    Privacy,
    Requirements,
    Runner,
)
from agentos.brains.service import BrainService
from agentos.brains.settings import BrainsSettings
from agentos.core.errors import ApprovalPending, InvalidRequest, NotFound, PolicyDenied
from agentos.core.models import Effect, Principal
from agentos.core.runtime import Runtime

P = Principal(id="pilot", thread="T-1", grant_id="g_pilot")
KEY = "ts-key-" + "k" * 32
WS = "personal"
TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
OPENROUTER_URL = "https://openrouter.ai/api/v1/systemone"
NOUL = {"ok": {"type": "noul", "instructions": "Is the page loaded?"}}
ROOMY = Budget(max_usd=1.0)


def reply(*, noul=0.9, usage=(100, 0), model="jev-1.14.0", status=200, headers=None) -> httpx.Response:
    return httpx.Response(status, headers=headers, json={
        "model": model, "answers": {"ok": {"type": "noul", "noul": noul}},
        "usage": {"input_tokens": usage[0], "output_tokens": usage[1]}})


class Endpoint:
    """An httpx transport that answers from a script (the last entry repeats) and keeps every request."""

    def __init__(self, *script):
        self.script = list(script) or [reply()]
        self.requests: list[httpx.Request] = []
        self.mock = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        out = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(out, Exception):
            raise out
        return out() if callable(out) else out

    @property
    def calls(self) -> int:
        return len(self.requests)

    def body(self, n: int = -1) -> dict:
        return json.loads(self.requests[n].content)


class ChargingPolicy(FakePolicy):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.charges: list[tuple] = []

    async def charge_spend(self, grant_id, *, tokens, usd):
        self.charges.append((grant_id, tokens, usd))
        return True


ALLOW = {"allow_metered": True, "remaining_usd": None, "remaining_tokens": None, "max_tier": None, "brains": None}


def service(tmp_path, endpoint=None, *, allowance=ALLOW, taint=None, **settings) -> BrainService:
    rt = Runtime()
    rt.register("policy", ChargingPolicy(allowance=allowance, taint=taint))
    rt.register("notifier", FakeNotifier())
    defaults = {"decide_backoff_seconds": 0.0}
    return make_service(tmp_path, runtime=rt, decide_transport=endpoint.mock if endpoint else None,
                        **{**defaults, **settings})


async def jev(svc, name="jev", provider="typesafe", **over):
    if provider == "openrouter":
        over = {"price_in_per_mtok": 0.05, "price_out_per_mtok": 0.0, **over}
    return await add(svc, name, "metered", provider, credential=KEY, **over)


async def ask(svc, **over):
    args = {"workspace": WS, "state": {"title": "Inbox"}, "questions": NOUL, "must_not_send": (), "budget": ROOMY}
    return await svc.decide(P, **{**args, **over})


def trace_lines(svc) -> str:
    path = svc.dir / "decisions.jsonl"
    return path.read_text() if path.exists() else ""


# ---- the spec ---------------------------------------------------------------------------------


def test_a_typesafe_brain_is_a_metered_cloud_decision_brain_with_default_prices():
    s = BrainSpec.template("jev", "metered", "typesafe")
    assert (s.kind, s.runner, s.credential, s.locality) == (BrainKind.METERED, Runner.SYSTEMONE, "api_key",
                                                            Locality.CLOUD)
    assert (s.caps.decisions, s.caps.tools, s.caps.vision, s.caps.context_tokens) == (True, False, False, 64_000)
    assert (s.base_url, s.model) == ("https://api.typesafe.ai/v1", "jev-latest")
    assert (s.price_in_per_mtok, s.price_out_per_mtok) == (0.042, 0.0)
    assert BrainSpec.model_validate_json(s.model_dump_json()) == s  # the store round trip


def test_an_openrouter_brain_pins_its_own_url_and_model_and_requires_prices():
    s = BrainSpec.template("jev", "metered", "openrouter", price_in_per_mtok=0.05, price_out_per_mtok=0)
    assert (s.base_url, s.model) == ("https://openrouter.ai/api/v1", "~typesafe/jev-latest")
    assert s.price_out_per_mtok == 0  # a zero is a price, not a missing one
    with pytest.raises(ValueError, match="--provider openrouter needs --price-in"):
        BrainSpec.template("jev", "metered", "openrouter")


def test_existing_brains_keep_their_capabilities():
    assert BrainSpec.template("a", "metered", "anthropic").caps.decisions is False
    assert Requirements().decisions is False


@pytest.mark.parametrize(("over", "message"), [
    ({"base_url": "http://api.typesafe.ai/v1"}, "must be https"),
    ({"base_url": "http://192.168.1.9:8000/v1"}, "must be https"),  # stricter than a chat brain
    ({"base_url": "https://user:secret@api.typesafe.ai/v1"}, "may not hold"),
    ({"base_url": "https://api.typesafe.ai/v1?key=secret"}, "may not hold"),
    ({"price_in_per_mtok": -1}, "negative"),
    ({"caps": {"vision": True}}, "no tools and no vision"),
    ({"caps": {"tools": True}}, "no tools and no vision"),
])
def test_what_a_decision_brain_refuses(over, message):
    with pytest.raises(ValueError, match=message):
        BrainSpec.template("jev", "metered", "typesafe", **over)


def test_the_url_can_be_corrected_for_one_brain_within_the_rules():
    s = BrainSpec.template("jev", "metered", "openrouter", base_url="https://openrouter.ai/api/v2",
                           price_in_per_mtok=0.05, price_out_per_mtok=0)
    assert s.base_url == "https://openrouter.ai/api/v2"


def test_decisions_and_the_systemone_runner_only_come_together():
    with pytest.raises(ValueError, match="together"):
        BrainSpec.template("a", "metered", "anthropic", caps={"decisions": True})
    with pytest.raises(ValueError, match="together"):
        BrainSpec(name="x", kind="metered", provider="typesafe", runner="systemone", credential="api_key",
                  base_url="https://api.typesafe.ai/v1", model="m", price_in_per_mtok=1, price_out_per_mtok=0)


def test_a_decision_provider_is_metered_only():
    with pytest.raises(ValueError, match="unknown provider"):
        BrainSpec.template("x", "subscription", "typesafe")


# ---- the call ---------------------------------------------------------------------------------


async def test_a_decision_sends_the_documented_request_and_returns_answers_and_usage(tmp_path):
    ep = Endpoint(reply(noul=0.25, usage=(1000, 0)))
    svc = service(tmp_path, ep)
    await jev(svc)
    out = await ask(svc, state={"title": "Inbox"})
    req = ep.requests[0]
    assert str(req.url) == TYPESAFE_URL and req.method == "POST"
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert ep.body() == {"model": "jev-latest", "state": {"title": "Inbox"}, "questions": NOUL}
    assert decisions.IMAGES_HEADER.lower() not in req.headers
    assert out["answers"] == {"ok": {"type": "noul", "noul": 0.25}}
    assert out["usage"] == {"input_tokens": 1000, "output_tokens": 0, "estimated": False}
    assert out["brain"] == "jev" and out["model"] == "jev-1.14.0" and out["retries"] == 0
    assert out["cost_usd"] == pytest.approx(0.000042)


async def test_openrouter_takes_the_same_request_at_its_own_url_and_model(tmp_path):
    ep = Endpoint()
    svc = service(tmp_path, ep)
    await jev(svc, provider="openrouter")
    await ask(svc)
    assert str(ep.requests[0].url) == OPENROUTER_URL and ep.body()["model"] == "~typesafe/jev-latest"


async def test_a_decision_leaves_no_task_transcript_or_process_behind(tmp_path):
    svc = service(tmp_path, Endpoint())
    await jev(svc)
    await ask(svc)
    assert svc.list_tasks() == [] and list(svc.tasks_dir.iterdir()) == [] and svc._procs == {}


async def test_the_client_ignores_the_environment_and_redirects():
    wire = decisions.Wire(5.0)
    client = wire.client()
    assert client.trust_env is False and client.follow_redirects is False
    await wire.aclose()


async def test_assets_and_box_questions_carry_the_header_the_api_needs(tmp_path):
    box = httpx.Response(200, json={"answers": {"where": {"type": "box", "left": 0, "top": 0, "right": 1, "bottom": 1}},
                                    "usage": {"input_tokens": 9, "output_tokens": 0}})
    ep = Endpoint(box)
    svc = service(tmp_path, ep)
    await jev(svc)
    assets = {"a1": {"type": "image", "format": "png", "data_base64": "iVBORw0KGgo="}}
    await ask(svc, state="a1", questions={"where": {"type": "box", "instructions": "the login button"}}, assets=assets)
    assert ep.requests[0].headers[decisions.IMAGES_HEADER] == "true" and ep.body()["assets"] == assets


@pytest.mark.parametrize(("questions", "message"), [
    ({}, "at least one question"),
    ({"a": {"type": "essay"}}, "has type"),
    ({"a": "noul"}, "must be an object"),
    ({"a": {"type": "choice"}}, "needs criteria"),
    ({"a": {"type": "choice", "criteria": {str(i): "" for i in range(256)}}}, "1 to 255"),
    ({"a": {"type": "score", "criteria": ["x"] * 11}}, "1 to 10"),
])
async def test_a_bad_question_is_refused_before_anything_is_sent(tmp_path, questions, message):
    ep = Endpoint()
    svc = service(tmp_path, ep)
    await jev(svc)
    with pytest.raises(InvalidRequest, match=message):
        await ask(svc, questions=questions)
    assert ep.calls == 0


async def test_a_choice_of_255_options_is_allowed(tmp_path):
    options = {f"o{i}": "" for i in range(255)}
    ep = Endpoint(httpx.Response(200, json={"answers": {"a": {"type": "choice", "choice": "o7"}},
                                            "usage": {"input_tokens": 5, "output_tokens": 0}}))
    svc = service(tmp_path, ep)
    await jev(svc)
    out = await ask(svc, questions={"a": {"type": "choice", "instructions": "which?", "criteria": options}})
    assert out["answers"]["a"]["choice"] == "o7"


async def test_a_state_that_is_not_json_is_refused(tmp_path):
    svc = service(tmp_path, Endpoint())
    await jev(svc)
    with pytest.raises(InvalidRequest, match="not plain JSON"):
        await ask(svc, state={"x": float("nan")})
    with pytest.raises(InvalidRequest, match="must be a string"):
        await ask(svc, state=7)


# ---- privacy ----------------------------------------------------------------------------------


async def test_a_local_only_workspace_never_reaches_a_decision_brain(tmp_path):
    ep = Endpoint()
    svc = service(tmp_path, ep)
    await jev(svc)
    svc.set_privacy(WS, Privacy.LOCAL_ONLY)
    ctx = FakeCtx(svc.runtime, P)
    with pytest.raises(DecisionUnavailable, match="local-only"):
        await ask(svc, ctx=ctx)
    with pytest.raises(DecisionUnavailable, match="local-only"):
        await ask(svc, brain="jev")
    assert ep.calls == 0 and ctx.authorized == []  # not even a question to the owner


async def test_private_data_needs_the_same_cosign_as_any_cloud_brain(tmp_path):
    ep = Endpoint()
    svc = service(tmp_path, ep)
    await jev(svc)
    with pytest.raises(PolicyDenied, match="private"):
        await ask(svc, taint=["private"])
    assert ep.calls == 0
    pending = FakeCtx(svc.runtime, P, pending={Effect.IRREVERSIBLE})
    with pytest.raises(ApprovalPending):
        await ask(svc, taint=["Private"], ctx=pending)
    assert ep.calls == 0
    signed = FakeCtx(svc.runtime, P)
    await ask(svc, taint=["private"], ctx=signed, state="the secret plan")
    assert ep.calls == 1 and signed.authorized[0]["effect"] == Effect.IRREVERSIBLE
    assert "the secret plan" not in json.dumps(signed.authorized[0]["content"])


async def test_the_grants_own_taint_counts(tmp_path):
    ep = Endpoint()
    svc = service(tmp_path, ep, taint={"private"})
    await jev(svc)
    with pytest.raises(PolicyDenied):
        await ask(svc)
    assert ep.calls == 0


async def test_metered_spend_needs_a_grant_and_a_dollar_budget(tmp_path):
    ep = Endpoint()
    svc = service(tmp_path, ep, allowance={**ALLOW, "allow_metered": False})
    await jev(svc)
    with pytest.raises(PolicyDenied, match="co-sign"):
        await ask(svc)
    with pytest.raises(InvalidRequest, match="budget.max_usd"):
        await ask(svc, budget=None)
    assert ep.calls == 0
    signed = FakeCtx(svc.runtime, P)
    await ask(svc, ctx=signed)
    assert ep.calls == 1 and signed.authorized[0]["effect"] == Effect.PAY


async def test_a_redacted_cloud_workspace_redacts_what_it_sends_and_refuses_images(tmp_path):
    token = "ghp_" + "a" * 30
    ep = Endpoint()
    svc = service(tmp_path, ep)
    await jev(svc)
    svc.set_privacy(WS, Privacy.REDACTED_CLOUD)
    await ask(svc, state={"url": f"https://x.test/?t={token}", "list": [token]},
              questions={"ok": {"type": "noul", "instructions": f"Is {token} here?"}})
    assert token not in ep.requests[0].content.decode() and "[redacted]" in ep.body()["state"]["list"][0]
    with pytest.raises(PolicyDenied, match="image cannot be redacted"):
        await ask(svc, assets={"a1": {"type": "image", "format": "png", "data_base64": "AAAA"}})
    assert ep.calls == 1


async def test_nothing_is_sent_when_the_internet_is_down(tmp_path):
    ep = Endpoint()
    svc = service(tmp_path, ep)
    await jev(svc)
    svc._online_check = lambda: False
    with pytest.raises(DecisionUnavailable, match="internet is down"):
        await ask(svc)
    assert ep.calls == 0


# ---- must not send ----------------------------------------------------------------------------


@pytest.mark.parametrize(("state", "secret"), [
    ({"field": "hunter2-value"}, "hunter2-value"),
    ("the code is hunter2-value!", "hunter2-value"),
    ({"hunter2-value": "as a key"}, "hunter2-value"),
    (["deep", {"er": ["hunter2-value"]}], "hunter2-value"),
    ({"f": 'pa"ss\\word'}, 'pa"ss\\word'),            # JSON escapes it in the body
    ({"f": "pässwörd-é"}, "pässwörd-é"),
    ({"otp": 481516}, "481516"),                       # a number is no string at all
    ({"url": "https://x.test/?p=a%40b%2Fc"}, "a@b/c"),  # as a page's URL carries it
    ({"url": "https://x.test/?p=a+b"}, "a b"),
    ({"html": "<b>a&amp;b</b>"}, "a&b"),
])
async def test_a_body_holding_a_must_not_send_string_never_leaves(tmp_path, state, secret):
    ep = Endpoint()
    svc = service(tmp_path, ep)
    await jev(svc)
    with pytest.raises(DecisionBlocked, match=r"must_not_send\[1\]") as err:
        await ask(svc, state=state, must_not_send=["not-here-at-all", secret])
    assert ep.calls == 0
    assert secret not in err.value.message and secret not in trace_lines(svc)
    assert '"outcome": "blocked"' in trace_lines(svc)


async def test_the_check_covers_questions_and_assets_too(tmp_path):
    ep = Endpoint()
    svc = service(tmp_path, ep)
    await jev(svc)
    with pytest.raises(DecisionBlocked):
        await ask(svc, questions={"ok": {"type": "noul", "instructions": "Is s3cret-value right?"}},
                  must_not_send=["s3cret-value"])
    with pytest.raises(DecisionBlocked):
        await ask(svc, assets={"a": {"type": "image", "format": "png", "data_base64": "s3cret-value"}},
                  must_not_send=["s3cret-value"])
    assert ep.calls == 0


async def test_the_brains_own_key_is_never_sent_even_when_nobody_listed_it(tmp_path):
    ep = Endpoint()
    svc = service(tmp_path, ep)
    await jev(svc)
    with pytest.raises(DecisionBlocked, match="the API key of jev") as err:
        await ask(svc, state={"oops": KEY})
    assert ep.calls == 0 and KEY not in err.value.message


async def test_a_string_redaction_removed_is_no_longer_in_the_body(tmp_path):
    token = "ghp_" + "a" * 30
    ep = Endpoint()
    svc = service(tmp_path, ep)
    await jev(svc)
    svc.set_privacy(WS, Privacy.REDACTED_CLOUD)
    await ask(svc, state=token, must_not_send=[token])
    assert ep.calls == 1 and token not in ep.requests[0].content.decode()


async def test_must_not_send_is_required_and_a_bare_string_is_refused(tmp_path):
    svc = service(tmp_path, Endpoint())
    await jev(svc)
    with pytest.raises(TypeError):
        await svc.decide(P, workspace=WS, state="x", questions=NOUL)
    with pytest.raises(InvalidRequest, match="list of strings"):
        await ask(svc, must_not_send="hunter2")
    with pytest.raises(InvalidRequest, match="not a string"):
        await ask(svc, must_not_send=[481516])


async def test_an_empty_entry_blocks_nothing(tmp_path):
    ep = Endpoint()
    svc = service(tmp_path, ep)
    await jev(svc)
    await ask(svc, must_not_send=["", ""])
    assert ep.calls == 1


async def test_neither_the_key_nor_the_state_nor_an_answer_reaches_the_trace(tmp_path):
    ep = Endpoint(reply(noul=0.123456))
    svc = service(tmp_path, ep)
    await jev(svc)
    await ask(svc, state="a very particular sentence")
    lines = trace_lines(svc)
    assert '"outcome": "answered"' in lines
    assert KEY not in lines and "a very particular sentence" not in lines and "0.123456" not in lines


# ---- redirects, retries and brain states ------------------------------------------------------


@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_a_redirect_is_never_followed(tmp_path, status):
    ep = Endpoint(httpx.Response(status, headers={"Location": "https://evil.test/steal"}))
    svc = service(tmp_path, ep)
    await jev(svc)
    with pytest.raises(DecisionUnavailable, match=f"HTTP {status}"):
        await ask(svc)
    assert ep.calls == 1 and ep.requests[0].url.host == "api.typesafe.ai"


@pytest.mark.parametrize("status", [429, 500, 502, 503, 529])
async def test_the_statuses_worth_another_try_get_one(tmp_path, status):
    ep = Endpoint(httpx.Response(status), httpx.Response(status), reply())
    svc = service(tmp_path, ep)
    await jev(svc)
    out = await ask(svc)
    assert ep.calls == 3 and out["retries"] == 2
    assert (await svc.effective_state(svc.get_spec("jev")))[0] == BrainState.READY


@pytest.mark.parametrize("status", [400, 404, 408, 413, 422, 501, 504])
async def test_nothing_else_is_retried(tmp_path, status):
    ep = Endpoint(httpx.Response(status, json={"detail": "no"}), reply())
    svc = service(tmp_path, ep)
    await jev(svc)
    with pytest.raises((DecisionRejected, DecisionUnavailable)):
        await ask(svc)
    assert ep.calls == 1


@pytest.mark.parametrize("error", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
async def test_a_failed_connection_is_not_retried_and_keeps_the_url_out_of_the_message(tmp_path, error):
    ep = Endpoint(error, reply())
    svc = service(tmp_path, ep)
    await jev(svc)
    with pytest.raises(DecisionUnavailable, match=type(error).__name__):
        await ask(svc)
    assert ep.calls == 1


async def test_a_key_httpx_cannot_put_in_a_header_is_a_failed_call_not_a_crash(tmp_path):
    ep = Endpoint()
    svc = service(tmp_path, ep)
    await add(svc, "jev", "metered", "typesafe", credential="ts-kéy-" + "k" * 20)
    with pytest.raises(DecisionUnavailable, match="did not answer") as err:
        await ask(svc)
    assert ep.calls == 0 and "kéy" not in err.value.message


async def test_retries_stop_at_the_setting(tmp_path):
    ep = Endpoint(httpx.Response(503))
    svc = service(tmp_path, ep, decide_retries=1)
    await jev(svc)
    with pytest.raises(DecisionUnavailable, match="HTTP 503"):
        await ask(svc)
    assert ep.calls == 2
    assert (await svc.effective_state(svc.get_spec("jev")))[0] == BrainState.READY  # a 5xx is not a state


async def test_backoff_doubles_with_a_little_spread(tmp_path, monkeypatch):
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr("agentos.brains.service.asyncio.sleep", sleep)
    ep = Endpoint(httpx.Response(503), httpx.Response(503), reply())
    svc = service(tmp_path, ep, decide_backoff_seconds=0.25)
    await jev(svc)
    await ask(svc)
    assert len(waits) == 2 and 0.25 <= waits[0] <= 0.3125 and 0.5 <= waits[1] <= 0.625


@pytest.mark.parametrize("status", [401, 403])
async def test_a_rejected_key_means_needs_auth_and_one_notification(tmp_path, status):
    ep = Endpoint(httpx.Response(status))
    svc = service(tmp_path, ep)
    await jev(svc)
    with pytest.raises(DecisionUnavailable) as err:
        await ask(svc)
    assert err.value.state == "needs_auth" and ep.calls == 1
    state, _ = await svc.effective_state(svc.get_spec("jev"))
    assert state == BrainState.NEEDS_AUTH
    sent = svc.runtime.get("notifier").sent
    assert len(sent) == 1 and sent[0]["kind"] == "brain.needs_auth" and "agentos brains auth jev" in sent[0]["body"]
    with pytest.raises(DecisionUnavailable) as err:  # the router now keeps calls away from it
        await ask(svc)
    assert err.value.state == "needs_auth" and ep.calls == 1 and len(sent) == 1


async def test_parallel_rejections_tell_the_owner_once(tmp_path):
    release = asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(request)
        await release.wait()
        return httpx.Response(401)

    svc = service(tmp_path)
    svc.wire.transport = httpx.MockTransport(handler)
    await jev(svc)
    tasks = [asyncio.create_task(ask(svc)) for _ in range(5)]
    while len(calls) < 5:  # every call is past the router before the first answer lands
        await asyncio.sleep(0.01)
    release.set()
    out = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(e, DecisionUnavailable) and e.state == "needs_auth" for e in out)
    assert len(svc.runtime.get("notifier").sent) == 1


async def test_a_new_key_brings_it_back(tmp_path):
    ep = Endpoint(httpx.Response(401), reply())
    svc = service(tmp_path, ep)
    await jev(svc)
    with pytest.raises(DecisionUnavailable):
        await ask(svc)
    await svc.set_credential("jev", KEY + "x")
    await ask(svc)
    assert ep.requests[-1].headers["authorization"] == f"Bearer {KEY}x"


async def test_a_long_retry_after_cools_the_brain_down_at_once_for_that_long(tmp_path):
    ep = Endpoint(httpx.Response(429, headers={"Retry-After": "600"}))
    now = [1_000_000.0]
    svc = service(tmp_path, ep)
    svc.clock = svc.seats.clock = lambda: now[0]
    await jev(svc)
    with pytest.raises(DecisionUnavailable) as err:
        await ask(svc)
    assert err.value.state == "cooling_down" and err.value.retry_after == 600 and ep.calls == 1
    assert (await svc.effective_state(svc.get_spec("jev")))[0] == BrainState.COOLING_DOWN
    now[0] += 300
    with pytest.raises(DecisionUnavailable) as err:  # no call while it cools
        await ask(svc)
    assert err.value.state == "cooling_down" and err.value.retry_after == pytest.approx(300) and ep.calls == 1
    now[0] += 301
    ep.script = [reply()]
    await ask(svc)
    assert ep.calls == 2


async def test_an_http_date_retry_after_works_too(tmp_path):
    ep = Endpoint(httpx.Response(429, headers={"Retry-After": formatdate(2_000_600, usegmt=True)}))
    svc = service(tmp_path, ep)
    svc.clock = svc.seats.clock = lambda: 2_000_000.0
    await jev(svc)
    with pytest.raises(DecisionUnavailable) as err:
        await ask(svc)
    assert err.value.retry_after == pytest.approx(600)


async def test_a_short_retry_after_is_waited_for_and_never_cools_the_brain(tmp_path, monkeypatch):
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr("agentos.brains.service.asyncio.sleep", sleep)
    ep = Endpoint(httpx.Response(429, headers={"Retry-After": "1"}), reply())
    svc = service(tmp_path, ep)
    await jev(svc)
    await ask(svc)
    assert waits == [1.0] and ep.calls == 2
    assert (await svc.effective_state(svc.get_spec("jev")))[0] == BrainState.READY


async def test_429_after_every_try_cools_down_with_the_usual_backoff(tmp_path):
    ep = Endpoint(httpx.Response(429))
    svc = service(tmp_path, ep, cooldown_base_seconds=60)
    svc.clock = svc.seats.clock = lambda: 3_000_000.0
    await jev(svc)
    with pytest.raises(DecisionUnavailable) as err:
        await ask(svc)
    assert err.value.state == "cooling_down" and err.value.retry_after == 60 and ep.calls == 3


async def test_parallel_rate_limits_count_as_one_and_a_late_success_does_not_end_the_cooldown(tmp_path):
    """Calls in flight when the limit lands: ten 429s must not raise the backoff ten levels."""
    ep = Endpoint(httpx.Response(429))
    svc = service(tmp_path, ep, decide_retries=0)
    await jev(svc)
    out = await asyncio.gather(*(ask(svc) for _ in range(10)), return_exceptions=True)
    assert all(isinstance(e, DecisionUnavailable) for e in out)
    assert svc.store.brain_state("jev")["backoff_level"] == 1
    data = {"answers": {"ok": {"type": "noul", "noul": 1}}, "usage": {"input_tokens": 1, "output_tokens": 0}}
    svc.seats.mark_success = lambda name: pytest.fail("a success must not clear a live cooldown")
    # the call below is one that was already past the router when the limit landed
    ep.script = [httpx.Response(200, json=data)]
    spec = svc.get_spec("jev")
    candidate = Candidate(name=spec.name, kind=spec.kind, reason="in flight")
    ask_ = decisions.Ask.build("x", NOUL, None, ())
    await svc._decide_on(spec, candidate, ask_, P, await svc._grant_view(P), ROOMY, None, WS, None)
    assert svc.seats.override("jev")[0] == BrainState.COOLING_DOWN


async def test_a_decision_falls_through_the_chain_to_the_next_brain(tmp_path):
    def answer(request: httpx.Request) -> httpx.Response:
        return reply() if request.url.host == "openrouter.ai" else httpx.Response(503)

    calls = []

    def handler(request):
        calls.append(request.url.host)
        return answer(request)

    svc = service(tmp_path, decide_retries=0)
    svc.wire.transport = httpx.MockTransport(handler)
    await jev(svc, "jev-a", order=1)
    await jev(svc, "jev-b", provider="openrouter", order=2)
    out = await ask(svc)
    assert calls == ["api.typesafe.ai", "openrouter.ai"] and out["brain"] == "jev-b"


async def test_a_rejected_request_does_not_go_to_the_next_brain_and_keeps_the_input_out(tmp_path):
    detail = [{"loc": ["body", "questions", "a"], "msg": "bad", "input": {"state": "private page text"}}]
    ep = Endpoint(httpx.Response(422, json={"detail": detail}))
    svc = service(tmp_path, ep)
    await jev(svc, "jev-a", order=1)
    await jev(svc, "jev-b", provider="openrouter", order=2)
    with pytest.raises(DecisionRejected, match=r"HTTP 422\): body.questions.a: bad") as err:
        await ask(svc)
    assert ep.calls == 1 and "private page text" not in err.value.message


async def test_a_long_state_comes_back_as_the_endpoints_own_refusal(tmp_path):
    ep = Endpoint(httpx.Response(400, json={"detail": "max_tokens_exceeded"}))
    svc = service(tmp_path, ep)
    await jev(svc)
    with pytest.raises(DecisionRejected, match="max_tokens_exceeded"):
        await ask(svc)


async def test_a_state_past_any_brains_context_is_not_sent(tmp_path):
    ep = Endpoint()
    svc = service(tmp_path, ep)
    await jev(svc)
    with pytest.raises(DecisionUnavailable, match="context"):
        await ask(svc, state="x" * 300_000)
    assert ep.calls == 0


async def test_an_error_body_that_echoes_a_secret_is_scrubbed(tmp_path):
    ep = Endpoint(httpx.Response(400, json={"detail": f"bad key {KEY} and sk-ant-api03-{'z' * 20}"}))
    svc = service(tmp_path, ep)
    await jev(svc)
    with pytest.raises(DecisionRejected) as err:
        await ask(svc)
    assert KEY not in err.value.message and "sk-ant" not in err.value.message


async def test_a_missing_credential_is_needs_auth_and_no_call(tmp_path):
    ep = Endpoint()
    svc = service(tmp_path, ep)
    await svc.add_brain(BrainSpec.template("jev", "metered", "typesafe"))
    with pytest.raises(DecisionUnavailable) as err:
        await ask(svc)
    assert err.value.state == "needs_auth" and ep.calls == 0


async def test_no_decision_brain_is_a_plain_answer_not_a_crash(tmp_path):
    svc = service(tmp_path, Endpoint())
    await add(svc, "api", "metered", "anthropic", credential=API_KEY)
    with pytest.raises(DecisionUnavailable, match="no decision brain"):
        await ask(svc)


# ---- bad replies ------------------------------------------------------------------------------


@pytest.mark.parametrize(("body", "why"), [
    ({"answers": {}, "usage": {"input_tokens": 3, "output_tokens": 0}}, "no answer to ok"),
    ({"usage": {"input_tokens": 3, "output_tokens": 0}}, "no answers"),
    ({"answers": {"ok": {"type": "choice", "choice": "x"}}, "usage": {"input_tokens": 3, "output_tokens": 0}},
     "not a noul"),
    ({"answers": {"ok": {"type": "noul", "noul": 7}}, "usage": {"input_tokens": 3, "output_tokens": 0}}, "0 to 1"),
    ({"answers": {"ok": {"type": "noul", "noul": True}}, "usage": {"input_tokens": 3, "output_tokens": 0}}, "0 to 1"),
])
async def test_an_answer_that_is_not_what_was_asked_is_not_handed_on(tmp_path, body, why):
    ep = Endpoint(httpx.Response(200, json=body))
    svc = service(tmp_path, ep)
    await jev(svc)
    with pytest.raises(DecisionUnavailable, match=why):
        await ask(svc, job="j")
    assert ep.calls == 1 and svc.store.usage_for_task("j")[0] == 3  # it answered, so it counts


async def test_a_choice_outside_the_options_is_not_handed_on(tmp_path):
    ep = Endpoint(httpx.Response(200, json={"answers": {"a": {"type": "choice", "choice": "delete_everything"}},
                                            "usage": {"input_tokens": 3, "output_tokens": 0}}))
    svc = service(tmp_path, ep)
    await jev(svc)
    with pytest.raises(DecisionUnavailable, match="not one of the options"):
        await ask(svc, questions={"a": {"type": "choice", "criteria": {"keep": "", "archive": ""}}})


async def test_a_reply_that_is_not_json_still_moves_the_budget(tmp_path):
    ep = Endpoint(httpx.Response(200, content=b"<html>hello</html>"))
    svc = service(tmp_path, ep)
    await jev(svc)
    with pytest.raises(DecisionUnavailable, match="cannot be used"):
        await ask(svc, job="j")
    assert svc.store.usage_for_task("j")[0] > 0


async def test_a_reply_past_the_size_limit_is_dropped_unread(tmp_path, monkeypatch):
    monkeypatch.setattr(decisions, "MAX_REPLY_BYTES", 10)
    ep = Endpoint(reply())
    svc = service(tmp_path, ep)
    await jev(svc)
    with pytest.raises(DecisionUnavailable, match="cannot be used"):
        await ask(svc)


async def test_usage_left_out_is_estimated_high_and_says_so(tmp_path):
    ep = Endpoint(httpx.Response(200, json={"answers": {"ok": {"type": "noul", "noul": 0.5}}}))
    svc = service(tmp_path, ep)
    await jev(svc)
    out = await ask(svc, state="x" * 3000)
    assert out["usage"]["estimated"] is True and out["usage"]["input_tokens"] >= 1000 and out["cost_usd"] > 0


# ---- money ------------------------------------------------------------------------------------


async def test_spend_reaches_the_usage_table_the_grant_and_the_spend_today_figure(tmp_path):
    ep = Endpoint(reply(usage=(1_000_000, 500)))
    svc = service(tmp_path, ep)
    await jev(svc, price_out_per_mtok=2.0)
    out = await ask(svc, budget=Budget(max_usd=5))
    assert out["cost_usd"] == pytest.approx(0.042 + 0.001)
    assert svc.spend_today() == {"tokens": 1_000_500, "usd": pytest.approx(0.043)}
    assert svc.runtime.get("policy").charges == [("g_pilot", 1_000_500, pytest.approx(0.043))]


async def test_a_failed_call_costs_nothing(tmp_path):
    svc = service(tmp_path, Endpoint(httpx.Response(503)))
    await jev(svc)
    with pytest.raises(DecisionUnavailable):
        await ask(svc)
    assert svc.spend_today() == {"tokens": 0, "usd": 0}


async def test_one_budget_covers_every_call_of_a_job(tmp_path):
    ep = Endpoint(reply(usage=(1_000_000, 0)))  # $0.042 a call
    svc = service(tmp_path, ep)
    await jev(svc)
    budget = Budget(max_usd=0.1)
    await ask(svc, budget=budget, job="task-1")
    await ask(svc, budget=budget, job="task-1")
    await ask(svc, budget=budget, job="task-1")  # 0.126 now: the bill arrives with the reply
    with pytest.raises(DecisionOverBudget, match="^budget: "):
        await ask(svc, budget=budget, job="task-1")
    assert ep.calls == 3
    await ask(svc, budget=budget, job="task-2")  # another job has its own
    assert ep.calls == 4


async def test_a_call_the_budget_cannot_cover_is_not_sent(tmp_path):
    ep = Endpoint()
    svc = service(tmp_path, ep)
    await jev(svc)
    with pytest.raises(DecisionOverBudget, match="could cost"):
        await ask(svc, state="x" * 30_000, budget=Budget(max_usd=0.0000001))
    with pytest.raises(DecisionOverBudget, match="could use"):
        await ask(svc, state="x" * 30_000, budget=Budget(max_usd=1, max_tokens=100))
    assert ep.calls == 0


async def test_the_token_budget_counts_across_a_job_too(tmp_path):
    ep = Endpoint(reply(usage=(600, 0)))
    svc = service(tmp_path, ep)
    await jev(svc)
    budget = Budget(max_usd=1, max_tokens=1000)
    await ask(svc, budget=budget, job="j")
    await ask(svc, budget=budget, job="j")
    with pytest.raises(DecisionOverBudget, match="tokens are spent"):
        await ask(svc, budget=budget, job="j")
    assert ep.calls == 2


async def test_a_jobs_spend_survives_a_restart(tmp_path):
    svc = service(tmp_path, Endpoint(reply(usage=(1_000_000, 0))))
    await jev(svc)
    await ask(svc, budget=Budget(max_usd=0.04), job="task-1")
    restarted = service(tmp_path, Endpoint())
    with pytest.raises(DecisionOverBudget):
        await ask(restarted, budget=Budget(max_usd=0.04), job="task-1")


async def test_parallel_calls_under_one_job_cannot_all_spend_the_same_room(tmp_path):
    release = asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(request)
        await release.wait()
        return reply(usage=(10, 0))

    svc = service(tmp_path)
    svc.wire.transport = httpx.MockTransport(handler)
    await jev(svc)
    state = "x" * 3000  # about 1000 tokens each by the high estimate
    budget = Budget(max_usd=1, max_tokens=2500)
    tasks = [asyncio.create_task(ask(svc, state=state, budget=budget, job="j")) for _ in range(5)]
    await asyncio.sleep(0.05)
    release.set()
    out = await asyncio.gather(*tasks, return_exceptions=True)
    assert len(calls) == 2
    assert sum(isinstance(o, DecisionOverBudget) for o in out) == 3
    assert svc._holds.held == {}  # every hold let go


async def test_a_failed_call_lets_go_of_what_it_held(tmp_path):
    svc = service(tmp_path, Endpoint(httpx.Response(503)))
    await jev(svc)
    with pytest.raises(DecisionUnavailable):
        await ask(svc, job="j")
    assert svc._holds.held == {}


async def test_a_job_budget_the_size_of_the_grant_keeps_working_as_the_grant_shrinks(tmp_path):
    """The router reads a budget over the grant's remainder as a request for more money. A job set
    to the grant's size is exactly that after one call, unless it sees what is left."""
    ep = Endpoint(reply(usage=(1_000_000, 0)))
    svc = service(tmp_path, ep, allowance={**ALLOW, "remaining_usd": 1.0})
    await jev(svc)
    policy = svc.runtime.get("policy")
    budget = Budget(max_usd=1.0)
    await ask(svc, budget=budget, job="j")
    policy.allowance = {**ALLOW, "remaining_usd": 1.0 - 0.042}
    await ask(svc, budget=budget, job="j")
    assert ep.calls == 2


async def test_the_grants_remainder_caps_a_call_whatever_the_budget_says(tmp_path):
    ep = Endpoint()
    svc = service(tmp_path, ep, allowance={**ALLOW, "remaining_usd": 0.0000001})
    await jev(svc)
    with pytest.raises(DecisionOverBudget, match="could cost"):
        await ask(svc, state="x" * 30_000, budget=Budget(max_usd=5))
    assert ep.calls == 0


@pytest.mark.parametrize("job", ["", "x" * 81, "line\nbreak"])
async def test_a_job_id_is_short_and_printable(tmp_path, job):
    svc = service(tmp_path, Endpoint())
    await jev(svc)
    with pytest.raises(InvalidRequest, match="printable"):
        await ask(svc, job=job)


# ---- the two kinds stay apart -----------------------------------------------------------------


async def test_a_task_cannot_name_or_ask_for_a_decision_brain(tmp_path):
    svc = service(tmp_path, Endpoint())
    await jev(svc)
    with pytest.raises(InvalidRequest, match="cannot run a task"):
        await svc.submit(P, prompt="hello", workspace=WS, brain="jev", budget=ROOMY)
    with pytest.raises(InvalidRequest, match="decision brain"):
        await svc.submit(P, prompt="hello", workspace=WS, requires=Requirements(tools=False, decisions=True),
                         budget=ROOMY)
    assert svc.list_tasks() == []


async def test_a_bounded_task_with_only_a_decision_brain_waits_and_never_reaches_it(tmp_path):
    ep = Endpoint()
    svc = service(tmp_path, ep)
    await jev(svc)
    out = await svc.submit(P, prompt="hello", workspace=WS, requires=Requirements(tools=False), budget=ROOMY)
    assert out["status"] == "queued" and "typed questions" in out["status_reason"] and out["attempts"] == []
    assert ep.calls == 0


async def test_a_decision_cannot_be_pinned_to_a_task_brain(tmp_path):
    ep = Endpoint()
    svc = service(tmp_path, ep)
    await jev(svc)
    await add(svc, "api", "metered", "anthropic", credential=API_KEY)
    with pytest.raises(InvalidRequest, match="cannot answer typed questions"):
        await ask(svc, brain="api")
    with pytest.raises(NotFound):
        await ask(svc, brain="ghost")
    assert ep.calls == 0


async def test_a_task_cannot_resume_onto_a_decision_brain(tmp_path):
    svc = service(tmp_path, Endpoint())
    await add(svc, "api", "metered", "anthropic", credential=API_KEY)
    await jev(svc)
    out = await svc.submit(P, prompt="hello", workspace=WS, requires=Requirements(tools=False), budget=ROOMY)
    await svc.cancel(out["id"], wait=True)
    with pytest.raises(InvalidRequest, match="cannot run a task"):
        await svc.resume(out["id"], brain="jev")


async def test_a_decision_brain_cannot_be_a_default(tmp_path):
    svc = service(tmp_path, Endpoint())
    await jev(svc)
    with pytest.raises(InvalidRequest, match="cannot run a task"):
        svc.use("jev", WS)
    with pytest.raises(InvalidRequest, match="cannot run a task"):
        svc.use_node_default("jev")


async def test_the_chip_does_not_call_a_decision_brain_ready_for_offline_work(tmp_path):
    svc = service(tmp_path, Endpoint())
    await jev(svc)
    assert await svc.chip() == {"name": None, "state": None, "offline_ready": False, "local_ready": False}


def test_an_attempt_could_not_start_one_anyway(tmp_path):
    with pytest.raises(ValueError, match="no process to start"):
        adapters.plan(BrainSpec.template("jev", "metered", "typesafe"), BrainsSettings(bin_dirs=[]))


async def test_a_decision_brain_needs_no_cli_and_shows_up_in_the_list(tmp_path):
    svc = service(tmp_path, Endpoint())
    await jev(svc)
    info = (await svc.list_brains())[0]
    assert (info["runner"], info["state"], info["locality"]) == ("systemone", "ready", "cloud")
    assert info["caps"]["decisions"] is True and info["cli"]["ok"] is True and KEY not in json.dumps(info)
    probe = cliprobe.probe("systemone")
    assert probe.ok and probe.version == decisions.VERSION


async def test_remove_deletes_the_key(tmp_path):
    svc = service(tmp_path, Endpoint())
    await jev(svc)
    await svc.remove_brain("jev")
    assert not await svc.creds.has("brains/jev/credential")


async def test_shutdown_closes_the_pool_and_a_later_call_opens_a_new_one(tmp_path):
    ep = Endpoint()
    svc = service(tmp_path, ep)
    await jev(svc)
    await ask(svc)
    client = svc.wire.client()
    await svc.shutdown()
    assert client.is_closed
    await ask(svc)
    assert ep.calls == 2


# ---- the CLI ----------------------------------------------------------------------------------


runner = CliRunner()


@pytest.fixture
def cli(tmp_path, monkeypatch):
    ep = Endpoint(httpx.Response(200, json={"model": "jev-1.14.0", "usage": {"input_tokens": 31, "output_tokens": 0},
                                            "answers": {"mentions_sky": {"type": "noul", "noul": 0.99}}}))
    svc = make_service(tmp_path, decide_transport=ep.mock)
    monkeypatch.setattr(brains_cli, "runtime_factory", lambda: svc.runtime)
    svc.endpoint = ep
    return svc


def invoke(*args, input=None):
    r = runner.invoke(brains_cli.app, list(args), input=input)
    assert "\u2014" not in r.output
    return r.exit_code, r.output


def add_jev(*extra):
    return invoke("add", "jev", "--kind", "metered", "--provider", "typesafe", "--stdin", "--insecure-dev-store",
                  *extra, input=KEY + "\n")


def test_add_takes_the_key_on_stdin_and_never_echoes_it(cli):
    code, out = add_jev()
    assert code == 0, out
    assert "added jev: metered via systemone, state ready" in out and KEY not in out
    code, out = invoke("list")
    assert code == 0 and "jev" in out and KEY not in out


def test_openrouter_needs_prices_at_the_command_line(cli):
    args = ("add", "jev", "--kind", "metered", "--provider", "openrouter", "--stdin", "--insecure-dev-store")
    code, out = invoke(*args, input=KEY + "\n")
    assert code == 1 and "--price-in" in out
    code, out = invoke(*args, "--price-in", "0.05", "--price-out", "0", input=KEY + "\n")
    assert code == 0, out


def test_add_refuses_a_bad_url_without_quoting_it(cli):
    code, out = add_jev("--base-url", "https://me:hunter2@api.typesafe.ai/v1")
    assert code == 1 and "hunter2" not in out and "may not hold" in out


def test_without_a_vault_the_key_is_not_stored_in_plain_text_by_default(cli):
    code, out = invoke("add", "jev", "--kind", "metered", "--provider", "typesafe", "--stdin", input=KEY + "\n")
    assert code == 1 and "no vault" in out


def test_brains_test_without_live_makes_no_call(cli):
    add_jev()
    code, out = invoke("test", "jev")
    assert code == 0, out
    assert "https://api.typesafe.ai/v1/systemone" in out and "state: ready" in out and cli.endpoint.calls == 0


def test_brains_test_live_asks_one_harmless_noul_and_prints_latency_and_tokens(cli):
    add_jev()
    code, out = invoke("test", "jev", "--live")
    assert code == 0, out
    assert " ms, 31 tokens in, 0 out" in out and "jev-1.14.0" in out and KEY not in out
    body = cli.endpoint.body()
    assert cli.endpoint.calls == 1 and next(iter(body["questions"].values()))["type"] == "noul"


def test_brains_test_live_needs_no_grant_for_its_own_cent_but_keeps_the_privacy_floor(cli):
    add_jev()
    cli.set_privacy("agentos-test", Privacy.LOCAL_ONLY)
    code, out = invoke("test", "jev", "--live")
    assert code == 1 and "decision_unavailable" in out and "local-only" in out and cli.endpoint.calls == 0


def test_a_failed_live_test_exits_one_with_the_fix(cli):
    add_jev()
    cli.endpoint.script = [httpx.Response(401)]
    code, out = invoke("test", "jev", "--live")
    assert code == 1 and "rejected its API key" in out and "agentos brains list" in out and KEY not in out


def test_doctor_lists_the_runner_without_a_binary(cli):
    code, out = invoke("doctor")
    assert code == 0, out
    assert f"systemone: {decisions.VERSION} (ok)" in out
