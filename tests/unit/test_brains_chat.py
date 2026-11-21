"""The chat-completions brain: the spec's rules, what reaches the process, the real runner against a
local HTTP server, and a task falling back through the real shim when the endpoint is down."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from testkit.brains import (
    API_KEY,
    CHILD_TOKEN,
    FakePolicy,
    add,
    all_task_bytes,
    make_service,
    wait_status,
)
from typer.testing import CliRunner

from openajar.brains import adapters, chatcli, cliprobe
from openajar.brains import cli as brains_cli
from openajar.brains.models import BrainKind, BrainSpec, Budget, Locality, Outcome, Requirements, Runner
from openajar.brains.parsers import make_parser
from openajar.brains.settings import BrainsSettings
from openajar.core.models import Principal
from openajar.core.runtime import Runtime

P = Principal(id="claude-code", thread="T-1")
KEY = "sk-chat-" + "k" * 24
BOUNDED = Requirements(tools=False)


def spec(**over):
    args = {"model": "cheap-1", "base_url": "https://api.example.com/v1", "price_in_per_mtok": 0.1,
            "price_out_per_mtok": 0.4}
    return BrainSpec.template("chat", "metered", "openai-compatible", **{**args, **over})


# ---- the spec ---------------------------------------------------------------------------------


def test_the_template_is_a_metered_cloud_brain_on_the_chat_runner():
    s = spec()
    assert (s.kind, s.runner, s.credential, s.locality) == (BrainKind.METERED, Runner.CHAT, "api_key", Locality.CLOUD)
    assert (s.caps.tools, s.caps.vision, s.caps.tier, s.window_hours) == (False, False, 1, 0)
    assert spec(caps={"tier": 2, "context_tokens": 128_000}).caps.context_tokens == 128_000
    assert BrainSpec.model_validate_json(s.model_dump_json()) == s  # the store round trip


def test_a_private_address_is_still_cloud_for_privacy_and_may_use_http():
    s = spec(base_url="http://192.168.1.9:8000/v1")
    assert s.kind == BrainKind.METERED and s.locality == Locality.CLOUD


@pytest.mark.parametrize(("over", "message"), [
    ({"base_url": None}, "needs --base-url"),
    ({"base_url": "ftp://api.example.com/v1"}, "needs --base-url"),
    ({"base_url": "http://api.example.com/v1"}, "must be https"),
    ({"base_url": "https://user:secret@api.example.com/v1"}, "may not hold"),
    ({"base_url": "https://api.example.com/v1?key=secret"}, "may not hold"),
    ({"model": None}, "needs --model"),
    ({"price_in_per_mtok": None}, "--price-in"),
    ({"price_out_per_mtok": None}, "--price-in"),
    ({"price_in_per_mtok": -1}, "negative"),
    ({"caps": {"vision": True}}, "no tools and no vision"),
    ({"caps": {"tools": True}}, "no tools and no vision"),
])
def test_what_a_chat_brain_refuses(over, message):
    args = {"model": "cheap-1", "base_url": "https://api.example.com/v1", "price_in_per_mtok": 0.1,
            "price_out_per_mtok": 0.4, **over}
    with pytest.raises(ValueError, match=message):
        BrainSpec(name="chat", kind="metered", provider="openai-compatible", runner="chat", credential="api_key",
                  **{**args, "caps": {"tools": False, **args.get("caps", {})}})


def test_chat_is_metered_only_and_existing_openai_compatible_brains_are_unchanged():
    with pytest.raises(ValueError, match="unknown provider"):
        BrainSpec.template("x", "subscription", "openai-compatible", base_url="https://api.example.com/v1")
    local = BrainSpec.template("x", "local", "openai-compatible", base_url="http://192.168.1.9:8000/v1")
    assert local.runner == Runner.CODEX
    with pytest.raises(ValueError, match="is metered"):
        BrainSpec(name="x", kind="subscription", provider="openai-compatible", runner="chat")


def test_the_cli_says_what_is_wrong_without_echoing_the_url():
    result = CliRunner().invoke(brains_cli.app, [
        "add", "chat", "--kind", "metered", "--provider", "openai-compatible", "--model", "m",
        "--base-url", "https://user:hunter2@api.example.com/v1", "--price-in", "1", "--price-out", "1"])
    assert result.exit_code == 1 and "may not hold" in result.output and "hunter2" not in result.output


# ---- what reaches the process -----------------------------------------------------------------


def attempt(tmp_path, s=None, settings=None, **kw):
    args = {"bin_path": "/usr/bin/python3", "task_id": "bt_000000000001", "work": tmp_path / "work",
            "home": tmp_path / "home", "user": None, "credential": KEY, "mcp_url": "http://127.0.0.1:7400/mcp",
            "child_token": CHILD_TOKEN, "max_turns": 7, "max_usd": 0.5, "resume_session": None}
    return adapters.plan(s or spec(), settings or BrainsSettings(bin_dirs=[]), **{**args, **kw})


def test_the_key_goes_only_through_the_secret_path_and_the_grant_token_nowhere(tmp_path):
    p = attempt(tmp_path)
    assert p.secret_env == {chatcli.API_KEY_ENV: KEY}
    blob = json.dumps([p.argv, p.env, p.files])
    assert KEY not in blob and CHILD_TOKEN not in blob
    assert CHILD_TOKEN not in json.dumps(p.secret_env)
    assert not p.reports_cost and not p.files  # dollars come from the spec's prices


def test_the_environment_is_the_allowlist_not_the_daemons(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAJAR_MCP_TOKEN", "owner-token")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-the-daemon")
    p = attempt(tmp_path)
    assert set(p.env) == set(adapters.base_env(bin_path="/usr/bin/python3", settings=BrainsSettings(bin_dirs=[]),
                                               home=tmp_path / "home", user=None, task_id="t"))
    assert "owner-token" not in json.dumps(p.env) and "sk-from-the-daemon" not in json.dumps(p.env)


def test_the_settings_reach_the_runner(tmp_path):
    p = attempt(tmp_path, settings=BrainsSettings(bin_dirs=[], chat_max_output_tokens=256, chat_timeout_seconds=9))
    argv = p.argv
    assert argv[:4] == ["/usr/bin/python3", *cliprobe.CHAT_ARGS]
    assert argv[argv.index("--max-output-tokens") + 1] == "256" and argv[argv.index("--timeout") + 1] == "9.0"
    assert argv[argv.index("--max-usd") + 1] == "0.500000" and argv[argv.index("--price-out") + 1] == "0.4"
    assert "--max-usd" not in attempt(tmp_path, max_usd=None).argv


# ---- the runner against a real socket ---------------------------------------------------------


class Endpoint:
    """A chat-completions server that answers from a queue and keeps every request."""

    def __init__(self):
        outer = self
        self.requests: list[dict] = []
        self.answers: list[tuple[int, dict, object]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                       "body": json.loads(body) if body else None})
                code, headers, payload = outer.answers.pop(0) if outer.answers else (200, {}, reply("ok"))
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(code)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def reply(text, *, usage=None, finish="stop"):
    out = {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": finish}]}
    if usage is not False:
        out["usage"] = usage or {"prompt_tokens": 120, "completion_tokens": 30}
    return out


@pytest.fixture
def endpoint():
    e = Endpoint()
    yield e
    e.close()


def closed_port_url() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}/v1"


def run_runner(url, prompt="Draft one reply.", *args, key=KEY):
    env = {"PATH": os.environ["PATH"]} | ({chatcli.API_KEY_ENV: key} if key else {})
    proc = subprocess.run([sys.executable, *cliprobe.CHAT_ARGS, "--base-url", url, "--model", "cheap-1",
                           "--timeout", "5", *args], input=prompt, env=env, capture_output=True, text=True,
                          timeout=30, check=False)
    return proc.returncode, [json.loads(line) for line in proc.stdout.splitlines()]


def outcome_of(events, code):
    parser = make_parser("chat")
    for ev in events:
        parser.feed(json.dumps(ev))
    return parser, parser.outcome(code)


def test_a_reply_is_a_finished_turn_with_its_usage(endpoint):
    endpoint.answers.append((200, {}, reply("Thanks for asking!")))
    code, events = run_runner(endpoint.url, "Draft one reply.", "--system", "Be brief.")
    assert code == 0 and [e["type"] for e in events] == ["chat.started", "chat.completed"]
    req = endpoint.requests[0]
    assert req["path"] == "/v1/chat/completions" and req["auth"] == f"Bearer {KEY}"
    assert req["body"]["messages"] == [{"role": "system", "content": "Be brief."},
                                       {"role": "user", "content": "Draft one reply."}]
    assert req["body"]["model"] == "cheap-1" and req["body"]["stream"] is False
    parser, outcome = outcome_of(events, code)
    assert outcome == ("succeeded", "finished") and parser.result_text == "Thanks for asking!"
    assert (parser.input_tokens, parser.output_tokens, parser.turns) == (120, 30, 1)


def test_a_server_that_reports_no_usage_still_moves_the_budget(endpoint):
    endpoint.answers.append((200, {}, reply("x" * 300, usage=False)))
    code, events = run_runner(endpoint.url, "y" * 600)
    assert events[-1]["usage_estimated"] is True
    parser, _ = outcome_of(events, code)
    assert parser.input_tokens == 200 and parser.output_tokens == 100


def test_the_budget_goes_into_the_request_because_the_usage_comes_too_late(endpoint):
    run_runner(endpoint.url, "y" * 3000, "--max-usd", "0.001", "--price-in", "0.1", "--price-out", "1",
               "--max-output-tokens", "4096")
    assert endpoint.requests[0]["body"]["max_tokens"] == 900  # $0.0009 left after 1000 prompt tokens
    run_runner(endpoint.url, "hi", "--max-usd", "5", "--price-in", "0.1", "--price-out", "1",
               "--max-output-tokens", "64")
    assert endpoint.requests[1]["body"]["max_tokens"] == 64


def test_a_prompt_the_budget_cannot_pay_for_is_never_sent(endpoint):
    code, events = run_runner(endpoint.url, "y" * 30_000, "--max-usd", "0.0001", "--price-in", "10")
    assert code == 1 and not endpoint.requests
    assert outcome_of(events, code)[1][0] == "limit_cost"


@pytest.mark.parametrize(("status", "body", "expected"), [
    (401, {"error": {"message": "bad key"}}, "auth_failed"),
    (403, {"error": {"message": "no access"}}, "auth_failed"),
    (429, {"error": {"message": "slow down"}}, "rate_limited"),
    (500, {"error": {"message": "boom"}}, "server_error"),
    (503, b"<html>down</html>", "server_error"),
    (404, {"error": {"message": "no such model"}}, "server_error"),
    (400, {"error": {"message": "the model x-429 refused this input"}}, "failed"),
])
def test_the_outcome_comes_from_the_status_not_the_wording(endpoint, status, body, expected):
    endpoint.answers.append((status, {}, body))
    code, events = run_runner(endpoint.url)
    assert code == 1 and outcome_of(events, code)[1][0] == expected


def test_a_rate_limit_keeps_the_endpoints_retry_after(endpoint):
    endpoint.answers.append((429, {"Retry-After": "30"}, {"error": {"message": "slow down"}}))
    code, events = run_runner(endpoint.url)
    parser, _ = outcome_of(events, code)
    assert parser.resets_at is not None


def test_a_down_endpoint_is_a_server_error_so_the_chain_moves_on():
    code, events = run_runner(closed_port_url())
    _, (outcome, reason) = outcome_of(events, code)
    assert code == 1 and outcome == "server_error" and "did not answer" in reason
    assert Outcome(outcome).retry_elsewhere


def test_a_reply_that_is_not_a_completion_is_a_server_error(endpoint):
    for body in (b"<html>", {"choices": []}, {"choices": [{"message": None}]}):
        endpoint.answers.append((200, {}, body))
        code, events = run_runner(endpoint.url)
        assert outcome_of(events, code)[1][0] == "server_error"


def test_no_text_is_not_a_success(endpoint):
    endpoint.answers.append((200, {}, reply(None, finish="content_filter")))
    code, events = run_runner(endpoint.url)
    _, (outcome, reason) = outcome_of(events, code)
    assert outcome == "failed" and "content_filter" in reason


def test_a_reply_cut_off_at_the_ceiling_is_never_reported_as_finished(endpoint):
    endpoint.answers.append((200, {}, reply("half a sent", finish="length")))
    code, events = run_runner(endpoint.url)
    parser, (outcome, _) = outcome_of(events, code)
    assert outcome == "limit_tokens" and parser.result_text == "half a sent"


def test_a_redirect_is_not_followed_so_the_key_cannot_leave_with_it(endpoint):
    other = Endpoint()
    try:
        endpoint.answers.append((302, {"Location": other.url + "/chat/completions"}, b""))
        code, events = run_runner(endpoint.url)
        assert not other.requests  # the key stayed where the owner put it
        assert outcome_of(events, code)[1][0] == "server_error"
    finally:
        other.close()


def test_no_key_never_calls_out(endpoint):
    code, events = run_runner(endpoint.url, key=None)
    assert code == 1 and not endpoint.requests and outcome_of(events, code)[1][0] == "auth_failed"


# ---- a task through the real shim and the fallback chain --------------------------------------


@pytest.fixture
def svc(tmp_path):
    policy = FakePolicy(allowance={"allow_metered": True, "remaining_usd": 5.0})
    return make_service(tmp_path, runtime=Runtime(policy=policy))


async def add_chat(svc, url, name="chat-cheap", order=1):
    return await add(svc, name, "metered", "openai-compatible", credential=KEY, model="cheap-1", base_url=url,
                     price_in_per_mtok=1.0, price_out_per_mtok=2.0, order=order)


async def test_a_task_runs_on_the_chat_brain_and_is_charged_at_the_specs_prices(svc, endpoint):
    await add_chat(svc, endpoint.url)
    endpoint.answers.append((200, {}, reply("Done.", usage={"prompt_tokens": 1000, "completion_tokens": 500})))
    r = await svc.submit(P, prompt="Draft one reply.", workspace="personal", budget=Budget(max_usd=1), requires=BOUNDED)
    st = await wait_status(svc, r["id"])
    assert st["status"] == "succeeded_unverified" and st["brain"] == "chat-cheap"
    assert st["cost_usd"] == pytest.approx(0.002) and st["tokens"] == 1500
    assert endpoint.requests[0]["auth"] == f"Bearer {KEY}"
    assert KEY.encode() not in all_task_bytes(svc, r["id"])


async def test_the_key_never_reaches_the_transcript_even_when_the_endpoint_echoes_it(svc, endpoint):
    await add_chat(svc, endpoint.url)
    endpoint.answers.append((400, {}, {"error": {"message": f"bad key {KEY}"}}))
    r = await svc.submit(P, prompt="Draft.", workspace="personal", budget=Budget(max_usd=1), requires=BOUNDED)
    st = await wait_status(svc, r["id"])
    assert st["status"] == "failed"
    assert KEY.encode() not in all_task_bytes(svc, r["id"])


async def test_an_endpoint_that_is_down_falls_back_to_the_next_brain(svc):
    await add_chat(svc, closed_port_url())
    await add(svc, "anthropic-api", "metered", "anthropic", credential=API_KEY, order=2)
    r = await svc.submit(P, prompt="FAKE:ok", workspace="personal", budget=Budget(max_usd=1), requires=BOUNDED)
    st = await wait_status(svc, r["id"])
    assert st["status"] == "succeeded_unverified" and st["brain"] == "anthropic-api"
    attempts = svc.store.attempts(r["id"])
    assert [(a["brain"], a["outcome"]) for a in attempts] == [("chat-cheap", "server_error"),
                                                              ("anthropic-api", "succeeded")]


async def test_with_nothing_to_fall_back_to_the_task_queues_with_the_reason(svc):
    await add_chat(svc, closed_port_url())
    r = await svc.submit(P, prompt="Draft.", workspace="personal", budget=Budget(max_usd=1), requires=BOUNDED)
    for _ in range(50):
        await svc.tick()
        st = svc.status(r["id"])
        if st["status"] == "queued" and svc.store.attempts(r["id"]):
            break
        await asyncio.sleep(0.1)
    assert st["status"] == "queued" and "already failed" in st["status_reason"]


async def test_a_rejected_key_asks_for_a_new_one_and_routes_onward(svc, endpoint):
    await add_chat(svc, endpoint.url)
    await add(svc, "anthropic-api", "metered", "anthropic", credential=API_KEY, order=2)
    endpoint.answers.append((401, {}, {"error": {"message": "bad key"}}))
    r = await svc.submit(P, prompt="FAKE:ok", workspace="personal", budget=Budget(max_usd=1), requires=BOUNDED)
    st = await wait_status(svc, r["id"])
    assert st["brain"] == "anthropic-api"
    state, _ = await svc.effective_state(svc.get_spec("chat-cheap"))
    assert state.value == "needs_auth"


async def test_the_brain_shows_up_like_any_other(svc, endpoint):
    await add_chat(svc, endpoint.url)
    info = (await svc.list_brains())[0]
    assert (info["kind"], info["provider"], info["runner"]) == ("metered", "openai-compatible", "chat")
    assert info["state"] == "ready" and info["cli"]["ok"] and info["cli"]["version"] == chatcli.VERSION
    assert info["credential"]["kind"] == "api_key" and KEY not in json.dumps(info)
