"""The brain runner end to end with fake CLIs: real subprocesses, real shims, real files.

Covers the edge cases the brains package must handle: the owner token never reaches a
brain, runaway loops (turns, wall clock, tokens, output), two tasks contending for one seat,
rate limits and expired credentials routing onward, node restarts, detach and reattach,
cancel (graceful then kill), local-only privacy, and metered budgets.
"""

import asyncio
import json
import os
import signal
import time

import pytest
from testkit.brains import (
    CHILD_TOKEN,
    OAUTH,
    OWNER_TOKEN,
    FakeCtx,
    FakeNotifier,
    FakePolicy,
    FakeVault,
    add,
    all_task_bytes,
    dump,
    make_service,
    wait_status,
)

from openajar.brains.models import Budget, ContextBundle, GitRef, Privacy
from openajar.brains.service import BrainService
from openajar.core.errors import ApprovalPending, InvalidRequest, PolicyDenied
from openajar.core.models import Effect, Principal
from openajar.core.runtime import Runtime

P = Principal(id="claude-code", thread="T-1")
CODEX_AUTH = json.dumps({"OPENAI_API_KEY": None, "tokens": {"access_token": "at-" + "A" * 30,
                                                             "refresh_token": "rt-original-1234"}})


@pytest.fixture
def notifier():
    return FakeNotifier()


@pytest.fixture
def svc(tmp_path, notifier):
    s = make_service(tmp_path, runtime=Runtime(notifier=notifier))
    yield s
    _kill_leftovers(s)


def _kill_leftovers(s: BrainService):
    for a in s.store.q("SELECT shim_pid FROM attempts WHERE outcome IS NULL AND shim_pid IS NOT NULL"):
        try:
            os.killpg(a["shim_pid"], signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


async def run(svc, prompt, ws="personal", **kw):
    r = await svc.submit(P, prompt=prompt, workspace=ws, **kw)
    return r["id"], r


# ------------------------------------------------------------------ basics
async def test_success_end_to_end(svc, notifier):
    await add(svc, "claude-max", "subscription", "anthropic")
    tid, r = await run(svc, "List the files. FAKE:ok")
    assert r["route"]["chain"] == ["claude-max"]
    st = await wait_status(svc, tid)
    assert st["status"] == "succeeded_unverified"
    assert "not verified" in st["status_reason"]
    assert st["tokens"] == 2640 and st["turns"] == 2
    assert st["cost_usd"] == 0 and st["notional_usd"] == pytest.approx(0.0123)  # subscription: no real spend
    assert st["result_preview"].startswith("Done.")
    kinds = [e["kind"] for e in svc.tail(tid)["events"]]
    assert kinds[:2] == ["openajar", "system"] and "tool_use" in kinds and "result" in kinds
    assert any(n["kind"] == "brain.finished" for n in notifier.sent)
    # prompt reached the brain on stdin, not argv
    d = dump(svc, tid)
    assert "List the files." in d["prompt"] and not any("List the files" in a for a in d["argv"])


async def test_tail_since_returns_only_newer_events(svc):
    await add(svc, "claude-max", "subscription", "anthropic")
    tid, _ = await run(svc, "FAKE:ok")
    await wait_status(svc, tid)
    full = svc.tail(tid)
    mid = full["events"][2]["seq"]
    newer = svc.tail(tid, since=mid)
    assert [e["seq"] for e in newer["events"]] == [e["seq"] for e in full["events"] if e["seq"] > mid]
    assert svc.tail(tid, since=full["next"])["events"] == []


async def test_every_routing_decision_is_traced_with_a_reason(svc):
    await add(svc, "claude-max", "subscription", "anthropic")
    tid, _ = await run(svc, "FAKE:ok")
    await wait_status(svc, tid)
    lines = [json.loads(x) for x in (svc.dir / "decisions.jsonl").read_text().splitlines()]
    routes = [x for x in lines if x["event"] == "brain.route" and x["task"] == tid]
    assert routes and all(x["reason"] for x in routes)


# ------------------------------------------------------------------ secrets and tokens
async def test_owner_token_never_reaches_the_brain(tmp_path, monkeypatch, notifier):
    monkeypatch.setenv("OPENAJAR_TOKEN", OWNER_TOKEN)          # the daemon's own environment
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-daemon-env-key-should-not-leak")
    policy = FakePolicy()
    svc = make_service(tmp_path, runtime=Runtime(policy=policy, notifier=notifier))
    await add(svc, "claude-max", "subscription", "anthropic")
    tid, _ = await run(svc, "Upvote the post. FAKE:ok", lanes=["personal/reddit"])
    st = await wait_status(svc, tid)
    assert st["status"] == "succeeded_unverified"
    d = dump(svc, tid)
    blob = json.dumps(d)
    assert OWNER_TOKEN not in blob
    assert "daemon-env-key" not in blob
    assert d["env"]["OPENAJAR_CHILD_TOKEN"] == CHILD_TOKEN       # the brain's own child grant
    assert d["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == OAUTH
    assert "${OPENAJAR_CHILD_TOKEN}" in d["mcp_config"] and CHILD_TOKEN not in d["mcp_config"]
    assert not any(OAUTH in a or CHILD_TOKEN in a for a in d["argv"])
    # nothing openajar wrote for the task holds the credential or the child token
    written = all_task_bytes(svc, tid) + (svc.dir / "decisions.jsonl").read_bytes()
    assert OAUTH.encode() not in written and CHILD_TOKEN.encode() not in written
    assert OWNER_TOKEN.encode() not in written
    # the child grant was minted for just this lane, without brain or exec, and revoked at the end
    assert policy.minted[0]["lanes"] == ["personal/reddit"] and policy.minted[0]["specials"] == []
    assert policy.revoked == ["g_1"]
    assert st["status"] and "child" not in json.dumps(svc.status(tid))


async def test_owner_token_refused_as_a_child_grant(tmp_path):
    svc = make_service(tmp_path, runtime=Runtime(policy=FakePolicy(token=OWNER_TOKEN)))
    await add(svc, "claude-max", "subscription", "anthropic")
    with pytest.raises(PolicyDenied, match="owner"):
        await run(svc, "FAKE:ok", lanes=["personal/reddit"])
    svc2 = make_service(tmp_path / "b")  # no policy: a provisioned default child token
    await add(svc2, "claude-max", "subscription", "anthropic")
    await svc2.creds.put("brains/default-child-token", OWNER_TOKEN, kind="grant_token", label="x")
    with pytest.raises(PolicyDenied, match="owner"):
        await run(svc2, "FAKE:ok", lanes=["personal/reddit"])


async def test_a_turn_grant_from_the_caller_is_used_instead_of_minting(tmp_path):
    """openajar.events mints the delivery's grant itself (narrowed by its trigger, seeded with the
    event's taint) and hands it over; the brain gets exactly that token and it is revoked after."""
    policy = FakePolicy()
    svc = make_service(tmp_path, runtime=Runtime(policy=policy))
    await add(svc, "claude-max", "subscription", "anthropic")
    turn = {"token": "turn-token-" + "T" * 40, "grant_id": "gr_turn0001"}
    tid, _ = await run(svc, "FAKE:ok", lanes=["personal/reddit"], turn_grant=turn,
                       context_bundle=ContextBundle(taint=["untrusted"]))
    st = await wait_status(svc, tid)
    assert st["status"] == "succeeded_unverified" and st["taint"] == ["untrusted"]
    assert policy.minted == []
    assert dump(svc, tid)["env"]["OPENAJAR_CHILD_TOKEN"] == turn["token"]
    assert policy.revoked == ["gr_turn0001"]
    with pytest.raises(PolicyDenied, match="owner"):
        await run(svc, "FAKE:ok", lanes=["personal/reddit"], turn_grant={"token": OWNER_TOKEN, "grant_id": "g"})
    with pytest.raises(InvalidRequest, match="turn grant"):
        await run(svc, "FAKE:ok", turn_grant=turn)


async def test_lanes_need_a_child_grant(svc):
    await add(svc, "claude-max", "subscription", "anthropic")
    with pytest.raises(InvalidRequest, match="child grant"):
        await run(svc, "FAKE:ok", lanes=["personal/reddit"])
    with pytest.raises(InvalidRequest, match="not in workspace"):
        await run(svc, "FAKE:ok", lanes=["other/reddit"])


async def test_secrets_printed_by_the_brain_are_redacted(tmp_path):
    svc = make_service(tmp_path, runtime=Runtime(policy=FakePolicy()))
    await add(svc, "claude-max", "subscription", "anthropic")
    tid, _ = await run(svc, "FAKE:leak", lanes=["personal/reddit"])
    await wait_status(svc, tid)
    texts = " ".join(e.get("text") or "" for e in svc.tail(tid, limit=500)["events"])
    assert "CLAUDE_CODE_OAUTH_TOKEN=[redacted]" in texts and "child=[redacted]" in texts
    assert OAUTH not in texts and CHILD_TOKEN not in texts


async def test_credentials_go_through_the_vault_when_present(tmp_path):
    vault = FakeVault()
    svc = make_service(tmp_path, runtime=Runtime(vault=vault))
    await add(svc, "claude-max", "subscription", "anthropic")
    assert vault.data["brains/claude-max/credential"] == OAUTH
    assert not (tmp_path / "home" / "secrets").exists()
    tid, _ = await run(svc, "FAKE:ok")
    assert (await wait_status(svc, tid))["status"] == "succeeded_unverified"
    info = (await svc.list_brains())[0]
    assert info["credential"] == {"kind": "oauth_token", "backend": "vault"} and OAUTH not in json.dumps(info)


async def test_codex_refreshed_login_is_written_back_and_removed(tmp_path):
    vault = FakeVault()
    svc = make_service(tmp_path, runtime=Runtime(vault=vault))
    await add(svc, "codex-plus", "subscription", "openai", credential=CODEX_AUTH)
    tid, _ = await run(svc, "FAKE:refresh")
    assert (await wait_status(svc, tid))["status"] == "succeeded_unverified"
    stored = json.loads(vault.data["brains/codex-plus/credential"])
    assert stored["tokens"]["refresh_token"].startswith("rt-refreshed-")
    assert not (svc.task_dir(tid) / "home" / ".codex" / "auth.json").exists()


# ------------------------------------------------------------------ runaway loops
async def test_runaway_turns_are_stopped(svc):
    await add(svc, "claude-max", "subscription", "anthropic")
    tid, _ = await run(svc, "FAKE:loop", budget=Budget(max_turns=20))
    st = await wait_status(svc, tid)
    assert st["status"] == "failed" and "turn limit of 20" in st["status_reason"]
    assert st["attempts"][0]["outcome"] == "limit_turns"


async def test_cli_reports_its_own_turn_limit(svc):
    await add(svc, "claude-max", "subscription", "anthropic")
    tid, _ = await run(svc, "FAKE:turns:50", budget=Budget(max_turns=5))
    st = await wait_status(svc, tid)
    assert st["status"] == "failed" and st["attempts"][0]["outcome"] == "limit_turns"
    assert "--max-turns" in dump(svc, tid)["argv"]


async def test_runaway_tokens_are_stopped(svc):
    await add(svc, "claude-max", "subscription", "anthropic")
    tid, _ = await run(svc, "FAKE:tokens", budget=Budget(max_tokens=2_000_000))
    st = await wait_status(svc, tid)
    assert st["attempts"][0]["outcome"] == "limit_tokens" and st["status"] == "failed"


async def test_wall_clock_limit(svc):
    await add(svc, "claude-max", "subscription", "anthropic")
    t0 = time.time()
    tid, _ = await run(svc, "FAKE:hang", budget=Budget(max_minutes=0.03))
    st = await wait_status(svc, tid)
    assert st["attempts"][0]["outcome"] == "limit_wall" and time.time() - t0 < 12


async def test_hang_is_detected_as_a_stall_and_routes_onward(tmp_path):
    svc = make_service(tmp_path, stall_seconds=1.0)
    await add(svc, "claude-max", "subscription", "anthropic")
    await add(svc, "codex-plus", "subscription", "openai", credential=CODEX_AUTH)
    tid, _ = await run(svc, "FAKE:claude_hang")
    st = await wait_status(svc, tid)
    assert st["status"] == "succeeded_unverified"
    assert [a["outcome"] for a in st["attempts"]] == ["stalled", "succeeded"]
    assert [a["brain"] for a in st["attempts"]] == ["claude-max", "codex-plus"]


async def test_huge_output_is_rotated_and_long_lines_cut(tmp_path):
    svc = make_service(tmp_path, transcript_segment_bytes=60_000, transcript_segments=3, max_line_bytes=500_000)
    await add(svc, "claude-max", "subscription", "anthropic")
    tid, _ = await run(svc, "FAKE:huge:20")
    st = await wait_status(svc, tid, timeout=30)
    assert st["status"] == "succeeded_unverified"
    segs = list((svc.attempt_dir(tid, 1)).glob("transcript.*.jsonl"))
    assert 1 <= len(segs) <= 3
    tail = svc.tail(tid, since=0)
    assert tail["gap"] is True and tail["first_available"] > 1
    tid2, _ = await run(svc, "FAKE:giantline:2")
    await wait_status(svc, tid2)
    texts = [e.get("text") or "" for e in svc.tail(tid2, limit=500)["events"]]
    assert any("over the size cap" in t for t in texts)


async def test_runaway_output_is_stopped(tmp_path):
    svc = make_service(tmp_path, max_output_bytes=3_000_000)
    await add(svc, "claude-max", "subscription", "anthropic")
    tid, _ = await run(svc, "FAKE:huge:50")
    st = await wait_status(svc, tid, timeout=30)
    assert st["attempts"][0]["outcome"] == "limit_output"


async def test_crash_routes_onward(tmp_path):
    svc = make_service(tmp_path)
    await add(svc, "claude-max", "subscription", "anthropic")
    await add(svc, "codex-plus", "subscription", "openai", credential=CODEX_AUTH)
    tid, _ = await run(svc, "FAKE:claude_crash")
    st = await wait_status(svc, tid)
    assert [a["outcome"] for a in st["attempts"]] == ["crashed", "succeeded"]
    assert "code 3" in st["attempts"][0]["reason"]
    # the second brain got the first one's transcript as context
    prompt = dump(svc, tid)["prompt"]
    assert "Progress so far" in prompt and "[assistant] starting" in prompt


# ------------------------------------------------------------------ seats and rate limits
async def test_two_tasks_contend_for_one_seat(svc):
    await add(svc, "claude-max", "subscription", "anthropic", seats=1)
    a, _ = await run(svc, "FAKE:slow:2")
    b, rb = await run(svc, "FAKE:ok")
    assert rb["status"] == "queued" and "waiting for a free seat on claude-max" in rb["status_reason"]
    max_running = 0
    deadline = time.time() + 20
    while time.time() < deadline:
        await svc.tick()
        running = sum(1 for t in (a, b) if svc.status(t)["status"] in ("starting", "running"))
        max_running = max(max_running, running)
        if all(svc.status(t)["status"] == "succeeded_unverified" for t in (a, b)):
            break
        await asyncio.sleep(0.05)
    assert max_running == 1
    assert svc.status(b)["status"] == "succeeded_unverified"
    assert svc.status(a)["ended_at"] <= svc.status(b)["started_at"]


async def test_rate_limit_cools_down_and_routes_onward(tmp_path, notifier):
    svc = make_service(tmp_path, runtime=Runtime(notifier=notifier))
    await add(svc, "claude-max", "subscription", "anthropic")
    await add(svc, "codex-plus", "subscription", "openai", credential=CODEX_AUTH)
    tid, _ = await run(svc, "Fix the bug. FAKE:claude_rate_limit")
    st = await wait_status(svc, tid)
    assert [(a["brain"], a["outcome"]) for a in st["attempts"]] == [
        ("claude-max", "rate_limited"), ("codex-plus", "succeeded")]
    info = {b["name"]: b for b in await svc.list_brains()}
    assert info["claude-max"]["state"] == "cooling_down"
    assert info["codex-plus"]["state"] == "ready"


async def test_usage_limit_uses_the_reported_reset(svc):
    await add(svc, "claude-max", "subscription", "anthropic")
    tid, _ = await run(svc, "FAKE:usage_limit:7200")
    await wait_status(svc, tid, until=("queued",))
    until = svc.store.brain_state("claude-max")["cooldown_until"]
    assert abs(until - (time.time() + 7200)) < 30
    assert "cooling down" in svc.status(tid)["status_reason"]


async def test_queued_after_rate_limit_resumes_when_the_cooldown_ends(svc):
    await add(svc, "claude-max", "subscription", "anthropic")
    tid, _ = await run(svc, "FAKE:rate_limit")
    st = await wait_status(svc, tid, until=("queued",))
    assert "every brain is cooling down" in st["status_reason"]
    (svc.task_dir(tid) / "work" / "fake-recover").write_text("")
    svc.store.set_brain_state("claude-max", cooldown_until=time.time() - 1)  # time passes
    st = await wait_status(svc, tid)
    assert st["status"] == "succeeded_unverified" and len(st["attempts"]) == 2
    assert svc.store.brain_state("claude-max")["backoff_level"] == 0


async def test_expired_credentials_need_auth_and_notify_the_owner(svc, notifier):
    await add(svc, "claude-max", "subscription", "anthropic")
    tid, _ = await run(svc, "FAKE:auth_expired")
    st = await wait_status(svc, tid, until=("queued",))
    assert "sign in again" in st["status_reason"]
    info = (await svc.list_brains())[0]
    assert info["state"] == "needs_auth" and "401" in info["state_reason"]
    note = next(n for n in notifier.sent if n["kind"] == "brain.needs_auth")
    assert note["urgent"] and "claude setup-token" in note["body"] and OAUTH not in json.dumps(note)
    # the owner signs in again in the morning
    await svc.set_credential("claude-max", "sk-ant-oat01-" + "N" * 60)
    (svc.task_dir(tid) / "work" / "fake-recover").write_text("")
    st = await wait_status(svc, tid)
    assert st["status"] == "succeeded_unverified"
    assert dump(svc, tid)["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-" + "N" * 60


async def test_brain_without_credential_is_needs_auth_and_skipped(svc):
    spec_codex = await add(svc, "codex-plus", "subscription", "openai", credential=CODEX_AUTH)
    from openajar.brains.models import BrainSpec
    await svc.add_brain(BrainSpec.template("claude-max", "subscription", "anthropic"))  # no token yet
    info = {b["name"]: b for b in await svc.list_brains()}
    assert info["claude-max"]["state"] == "needs_auth" and "brains auth claude-max" in info["claude-max"]["state_reason"]
    _tid, r = await run(svc, "FAKE:ok")
    assert r["route"]["chain"] == [spec_codex.name]


async def test_cli_drift_marks_the_brain_down(tmp_path):
    svc = make_service(tmp_path)
    from openajar.dev import fakecli
    (tmp_path / "bin" / "claude.help").write_text(fakecli.CLAUDE_HELP.replace("--strict-mcp-config", "--mcp-strict"))
    await add(svc, "claude-max", "subscription", "anthropic")
    info = (await svc.list_brains())[0]
    assert info["state"] == "down" and "--strict-mcp-config" in info["state_reason"]
    _, r = await run(svc, "FAKE:ok")
    assert r["status"] == "queued" and "--strict-mcp-config" in r["status_reason"]


# ------------------------------------------------------------------ privacy and budgets
async def test_local_only_without_a_local_brain_queues_and_never_touches_cloud(svc):
    await add(svc, "claude-max", "subscription", "anthropic")
    svc.set_privacy("private-ws", Privacy.LOCAL_ONLY)
    tid, r = await run(svc, "Summarize my diary. FAKE:ok", ws="private-ws")
    assert r["status"] == "queued" and "local-only" in r["status_reason"]
    for _ in range(5):
        await svc.tick()
    st = svc.status(tid)
    assert st["status"] == "queued" and st["attempts"] == []
    assert not (svc.task_dir(tid) / "work" / "fake-dump.json").exists()


async def test_local_only_runs_on_the_local_brain(svc):
    await add(svc, "claude-max", "subscription", "anthropic")
    await add(svc, "ollama", "local", "ollama", model="qwen3:8b")
    svc.set_privacy("private-ws", Privacy.LOCAL_ONLY)
    tid, r = await run(svc, "FAKE:ok", ws="private-ws")
    assert r["route"]["chain"] == ["ollama"]
    st = await wait_status(svc, tid)
    assert st["status"] == "succeeded_unverified"
    argv = dump(svc, tid)["argv"]
    assert "--oss" in argv and "qwen3:8b" in argv


async def test_private_taint_needs_cosign_for_cloud(svc):
    await add(svc, "claude-max", "subscription", "anthropic")
    ctx = FakeCtx(svc.runtime, P, pending={Effect.IRREVERSIBLE})
    with pytest.raises(ApprovalPending):
        await svc.submit(P, prompt="FAKE:ok", workspace="personal",
                         context_bundle=ContextBundle(taint=["private"]), ctx=ctx)
    assert "private data" in ctx.authorized[-1]["summary"] and "cannot be unsent" in ctx.authorized[-1]["summary"]
    ok_ctx = FakeCtx(svc.runtime, P)
    r = await svc.submit(P, prompt="FAKE:ok", workspace="personal",
                         context_bundle=ContextBundle(taint=["private"]), ctx=ok_ctx)
    assert r["route"]["chain"] == ["claude-max"]


async def test_redacted_cloud_redacts_the_prompt(svc):
    await add(svc, "claude-max", "subscription", "anthropic")
    svc.set_privacy("biz", Privacy.REDACTED_CLOUD)
    leaked = "sk-ant-api03-" + "Z" * 40
    tid, _ = await run(svc, f"Use key {leaked} to call the API. FAKE:ok", ws="biz")
    await wait_status(svc, tid)
    assert leaked not in dump(svc, tid)["prompt"] and "[redacted]" in dump(svc, tid)["prompt"]
    assert leaked not in json.dumps(svc.list_tasks()) and leaked not in json.dumps(svc.status(tid))


async def test_metered_needs_a_dollar_budget_and_a_cosign(svc):
    await add(svc, "anthropic-api", "metered", "anthropic", price_in_per_mtok=3, price_out_per_mtok=15)
    with pytest.raises(InvalidRequest, match="max_usd"):
        await run(svc, "FAKE:ok", ctx=FakeCtx(svc.runtime, P))
    with pytest.raises(PolicyDenied):
        await run(svc, "FAKE:ok", budget=Budget(max_usd=2), ctx=FakeCtx(svc.runtime, P, deny={Effect.PAY}))
    ctx = FakeCtx(svc.runtime, P, pending={Effect.PAY})
    with pytest.raises(ApprovalPending):
        await run(svc, "FAKE:ok", budget=Budget(max_usd=2), ctx=ctx)
    assert "$2.00" in ctx.authorized[-1]["summary"]
    tid, _r = await run(svc, "FAKE:ok", budget=Budget(max_usd=2), ctx=FakeCtx(svc.runtime, P))
    st = await wait_status(svc, tid)
    assert st["status"] == "succeeded_unverified" and st["cost_usd"] == pytest.approx(0.0123)
    argv = dump(svc, tid)["argv"]
    assert argv[argv.index("--max-budget-usd") + 1] == "2.00"
    assert dump(svc, tid)["env"]["ANTHROPIC_API_KEY"].startswith("sk-ant-api03-")


async def test_metered_beyond_the_grants_budget_asks_for_approval(tmp_path):
    policy = FakePolicy(allowance={"allow_metered": True, "remaining_usd": 1.0})
    svc = make_service(tmp_path, runtime=Runtime(policy=policy))
    await add(svc, "anthropic-api", "metered", "anthropic")
    ctx = FakeCtx(svc.runtime, P, pending={Effect.PAY})
    with pytest.raises(ApprovalPending):
        await run(svc, "FAKE:ok", budget=Budget(max_usd=5), ctx=ctx)
    assert ctx.authorized[-1]["effect"] == Effect.PAY
    tid, r = await run(svc, "FAKE:ok", budget=Budget(max_usd=0.5))  # inside the grant: no co-sign
    assert r["route"]["chain"] == ["anthropic-api"]
    await wait_status(svc, tid)


async def test_metered_cost_limit_stops_the_run(svc):
    await add(svc, "anthropic-api", "metered", "anthropic", price_in_per_mtok=10, price_out_per_mtok=50)
    tid, _ = await run(svc, "FAKE:tokens", budget=Budget(max_usd=1.0), ctx=FakeCtx(svc.runtime, P))
    st = await wait_status(svc, tid)
    assert st["attempts"][0]["outcome"] in ("limit_cost",)
    assert st["cost_usd"] > 1.0  # the overshoot of one message is recorded honestly


# ------------------------------------------------------------------ detach, restart, cancel
async def test_task_survives_the_service_going_away(tmp_path):
    svc = make_service(tmp_path)
    await add(svc, "claude-max", "subscription", "anthropic")
    tid, _ = await run(svc, "FAKE:slow:2")
    await svc.shutdown()
    svc._procs.clear()  # the daemon is gone; the shim is not its child any more
    svc2 = BrainService(svc.runtime, home=svc.home, settings=svc.settings, online_check=lambda: True)
    st = await wait_status(svc2, tid, timeout=20)
    assert st["status"] == "succeeded_unverified" and st["turns"] == 4


async def test_node_restart_marks_interrupted_and_resume_continues(tmp_path, notifier):
    svc = make_service(tmp_path, runtime=Runtime(notifier=notifier))
    await add(svc, "claude-max", "subscription", "anthropic")
    tid, _ = await run(svc, "Long job. FAKE:slow:30")
    await wait_status(svc, tid, until=("running",))
    await asyncio.sleep(1.5)  # let it write some transcript
    attempt = svc.store.attempt(tid, 1)
    status = json.loads((svc.attempt_dir(tid, 1) / "status.json").read_text())
    os.killpg(status["child_pid"], signal.SIGKILL)   # the machine went down: everything dies
    os.kill(attempt["shim_pid"], signal.SIGKILL)
    svc._procs[(tid, 1)].wait()
    svc._procs.clear()
    svc2 = BrainService(svc.runtime, home=svc.home, settings=svc.settings, online_check=lambda: True)
    await svc2.start()
    await svc2.shutdown()
    st = svc2.status(tid)
    assert st["status"] == "interrupted" and st["resumable"]
    assert "node restarted" in st["status_reason"]
    assert any(n["kind"] == "brain.interrupted" for n in notifier.sent)
    session = st["session_id"]
    assert session
    r = await svc2.resume(tid)
    assert r["status"] in ("starting", "running")
    st = await wait_status(svc2, tid, timeout=20)
    assert st["status"] == "succeeded_unverified" and len(st["attempts"]) == 2
    d = dump(svc2, tid)
    assert d["argv"][d["argv"].index("--resume") + 1] == session   # same brain continues its session
    assert d["prompt"].startswith("Continue the task")


async def test_cancel_is_graceful_then_kills(svc):
    """Nothing here is timed: cancel's own deadline bounds it, and each step waits for what it needs,
    so a loaded machine is slower and proves the same thing."""
    await add(svc, "claude-max", "subscription", "anthropic")
    tid, _ = await run(svc, "FAKE:ignore_term")
    await wait_status(svc, tid, until=("running",), timeout=60)

    def said() -> list[str]:
        return [e.get("text") or "" for e in svc.tail(tid)["events"]]

    async def until(done) -> None:
        deadline = time.time() + 60
        while not done():
            assert time.time() < deadline, said()
            await asyncio.sleep(0.05)

    await until(lambda: any("ignoring polite signals" in t for t in said()))  # it ignores them by now
    child = json.loads((svc.attempt_dir(tid, 1) / "status.json").read_text())["child_pid"]
    os.kill(child, 0)  # alive, or the rest proves nothing

    def gone() -> bool:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            return True
        return False

    st = await svc.cancel(tid, P, wait=True)
    assert st["status"] == "cancelled"
    await until(gone)
    texts = said()
    stop = next(i for i, t in enumerate(texts) if t.startswith("stopping:"))
    kill = next(i for i, t in enumerate(texts) if "killing the process group" in t)
    assert stop < kill  # asked first, killed only after that failed


async def test_cancel_queued_task(svc):
    await add(svc, "claude-max", "subscription", "anthropic")
    svc.set_privacy("p", Privacy.LOCAL_ONLY)
    tid, _ = await run(svc, "FAKE:ok", ws="p")
    st = await svc.cancel(tid, P)
    assert st["status"] == "cancelled" and st["status_reason"] == "cancelled before it ran"


async def test_tasks_are_scoped_to_their_owner(svc):
    await add(svc, "claude-max", "subscription", "anthropic")
    tid, _ = await run(svc, "FAKE:ok")
    stranger = Principal(id="x", owner="someone-else")
    from openajar.core.errors import NotFound
    with pytest.raises(NotFound):
        svc.status(tid, stranger)
    assert svc.list_tasks(stranger) == []
    await wait_status(svc, tid)


# ------------------------------------------------------------------ handoff
def _make_repo(repo):
    import subprocess
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x"}
    subprocess.run(["git", "init", "-q", "-b", "wip"], cwd=repo, check=True, env=env)
    (repo / "app.py").write_text("print('hi')\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "wip"], cwd=repo, check=True, env=env)


async def test_git_bundle_handoff(svc, tmp_path):
    repo = tmp_path / "repo"
    _make_repo(repo)
    await add(svc, "claude-max", "subscription", "anthropic")
    bundle = ContextBundle(summary="Refactor is half done; tests in tests/ still fail.",
                           files=["app.py", {"path": "notes/next.md", "content": "1. fix tests"}],
                           git=GitRef(remote=str(repo), ref="wip"))
    tid, _r = await run(svc, "Finish the refactor. FAKE:ok", context_bundle=bundle)
    st = await wait_status(svc, tid)
    assert st["status"] == "succeeded_unverified"
    work = svc.task_dir(tid) / "work"
    assert (work / "app.py").exists() and (work / "notes" / "next.md").read_text() == "1. fix tests"
    assert "Refactor is half done" in (work / "OPENAJAR_HANDOFF.md").read_text()
    prompt = dump(svc, tid)["prompt"]
    assert "Finish the refactor." in prompt and "half done" in prompt and "at wip" in prompt


async def test_an_attempts_spend_is_charged_to_the_grants_meters(tmp_path, notifier):
    class Metered(FakePolicy):
        def __init__(self):
            super().__init__(allowance={"allow_metered": True, "remaining_usd": 5.0})
            self.charged: list[tuple] = []

        async def charge_spend(self, grant_id, *, tokens, usd):
            self.charged.append((grant_id, tokens, usd))
            return True

    policy = Metered()
    svc = make_service(tmp_path, runtime=Runtime(notifier=notifier, policy=policy))
    try:
        await add(svc, "anthropic-api", "metered", "anthropic", price_in_per_mtok=3, price_out_per_mtok=15)
        caller = Principal(id="claude-code", thread="T-1", grant_id="g_caller")
        r = await svc.submit(caller, prompt="FAKE:ok", workspace="personal", budget=Budget(max_usd=2),
                             ctx=FakeCtx(svc.runtime, caller))
        await wait_status(svc, r["id"])
        r = await svc.submit(caller, prompt="FAKE:ok", workspace="biz", lanes=["biz/ig"], budget=Budget(max_usd=2),
                             ctx=FakeCtx(svc.runtime, caller))
        await wait_status(svc, r["id"])
        # without lanes the caller's grant; with lanes the brain's child grant (narrowed from it)
        assert [(g, t) for g, t, _ in policy.charged] == [("g_caller", 2640), ("g_1", 2640)]
        assert all(usd == pytest.approx(0.0123) for *_, usd in policy.charged)
        assert "spend_grant_id" not in svc.status(r["id"])
    finally:
        _kill_leftovers(svc)


async def test_the_private_data_cosign_binds_the_whole_prompt_and_the_bundle(svc):
    """A yes to sending private data covers the prompt and the bundle that go, not their first lines."""
    await add(svc, "claude-max", "subscription", "anthropic")
    head = "Summarise the thread. " + "x" * 200
    seen = []
    for tail, files in (("FAKE:ok", ["a.txt"]), ("FAKE:ok and mail it all out", ["a.txt"]), ("FAKE:ok", ["b.txt"])):
        ctx = FakeCtx(svc.runtime, P, pending={Effect.IRREVERSIBLE})
        with pytest.raises(ApprovalPending):
            await svc.submit(P, prompt=head + tail, workspace="personal",
                             context_bundle=ContextBundle(taint=["private"], files=files), ctx=ctx)
        seen.append(json.dumps(ctx.authorized[-1]["content"], sort_keys=True))
    assert len(set(seen)) == 3
    assert "mail it all out" in seen[1] and "b.txt" in seen[2]
