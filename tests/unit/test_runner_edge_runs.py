"""Runner edge cases: honest status after writes, idempotency keys, a database error while a run
finishes, a malformed row, templated URLs and readbacks, fairness, and restarts at step boundaries.

Each `xfail(strict=True)` is a real bug; its reason names the BUG id used in the QA report.
The other tests are edge cases that already hold and should keep holding.
"""

import asyncio
import json
import sqlite3

import pytest

from agentos.core.errors import InvalidRequest
from agentos.core.models import RunStatus
from agentos.runner.driver import DriverTimeout
from agentos.runner.fakes import BASE, FakeCtx, Harness, SimulatedCrash, post_flow
from agentos.runner.models import TERMINAL
from agentos.runner.store import RunnerStore
from agentos.runner.tools import flow_save, tool_specs

PARAMS = {"caption": "Autumn", "image": "files/a.jpg"}


def read_flow(fid="any.social.read", lane="*/*"):
    return {"id": fid, "lane": lane, "steps": [
        {"type": "navigate", "url": f"{BASE}/"},
        {"type": "readback", "locator": {"role": "heading", "name": "Feed"}, "condition": {"kind": "exists"}}]}


def share_step(flow):
    return next(s for s in flow["steps"] if s["type"] == "click" and s["locator"].get("name") == "Share")


# ---------------------------------------------------------------- honest status


def test_a_corrupt_runner_database_is_refused_and_left_intact(tmp_path):
    path = tmp_path / "runner.db"
    garbage = b"this is not a sqlite database\n" * 100
    path.write_bytes(garbage)
    with pytest.raises(sqlite3.DatabaseError):
        RunnerStore(path)
    assert path.read_bytes() == garbage


async def test_a_failed_check_after_a_flagged_write_is_partial(tmp_path):
    flow = post_flow()
    flow["steps"][-1]["condition"]["changed_by"] = 5  # the post goes out, then the check fails
    h = Harness(tmp_path)
    run = await h.run(h.save(flow).id, PARAMS)
    assert len(h.site.state["posts"]) == 1
    assert run.status == RunStatus.PARTIAL, run.error


async def test_a_failed_check_after_an_unflagged_click_is_partial(tmp_path):
    flow = post_flow()
    share_step(flow).pop("writes")  # the click still posts; the flag is what the runner counts
    flow["steps"][-1]["condition"]["changed_by"] = 5
    h = Harness(tmp_path)
    run = await h.run(h.save(flow).id, PARAMS)
    assert len(h.site.state["posts"]) == 1
    assert run.status == RunStatus.PARTIAL, run.error


async def test_a_transient_database_error_while_saving_a_finished_run_keeps_it_succeeded(tmp_path):
    h = Harness(tmp_path)
    fv = h.save(post_flow())
    real_save = h.store.save_run
    once = {"failed": False}

    def flaky_save(run, *, expect=None):
        if run.status in TERMINAL and not once["failed"]:
            once["failed"] = True
            raise sqlite3.OperationalError("database is locked")
        return real_save(run, expect=expect)

    h.store.save_run = flaky_save
    run = h.submit(fv.id, PARAMS)
    await h.runner.run_until_idle()
    assert len(h.site.state["posts"]) == 1
    assert h.runner.get(run.id).status == RunStatus.SUCCEEDED_VERIFIED, h.runner.get(run.id).error


# ---------------------------------------------------------------- idempotency and identity


def test_an_idempotency_key_reused_for_another_flow_does_not_return_the_old_run(tmp_path):
    h = Harness(tmp_path)
    post = h.save(post_flow())
    check = h.save(read_flow("biz-a.social.check", lane="biz-a/social"))
    h.runner.submit(flow_id=post.id, params=PARAMS, idempotency_key="k1", authorized=True)
    try:
        second, created = h.runner.submit(flow_id=check.id, idempotency_key="k1", authorized=True)
    except InvalidRequest:
        return  # refusing a reused key is an acceptable answer
    assert second.flow_id == check.id and created


async def test_flow_save_names_a_tool_that_runs_that_flow(tmp_path):
    h = Harness(tmp_path)
    h.save(read_flow("biz-a.social.check", lane="biz-a/social"))
    saved = await flow_save(FakeCtx(h.runtime), definition=read_flow("biz-a.social.look", lane="biz-a/social"))
    spec = next(s for s in tool_specs(h.runtime) if s.name == saved["tool"])
    assert "biz-a.social.look" in spec.description
    # an id with the same tool name as a saved flow is refused: it would take that name from it
    with pytest.raises(InvalidRequest, match="tool name"):
        await flow_save(FakeCtx(h.runtime), definition=read_flow("biz_a.social.check", lane="biz-a/social"))
    assert h.runtime.get("flows").tool_names()["biz-a.social.check"] == "flow_biz_a_social_check"


async def test_a_flow_whose_run_was_queued_before_deletion_fails_cleanly(tmp_path):
    h = Harness(tmp_path)
    gone = h.save(read_flow("biz-a.social.gone", lane="biz-a/social"))
    kept = h.save(read_flow("biz-a.social.kept", lane="biz-a/other"))
    doomed = h.submit(gone.id, lane="biz-a/social")
    ok = h.submit(kept.id, lane="biz-a/other")
    h.store.delete_flow(gone.id)
    await h.runner.run_until_idle()
    assert h.runner.get(doomed.id).status == RunStatus.FAILED
    assert h.runner.get(doomed.id).error["code"] == "not_found"
    assert h.runner.get(ok.id).status == RunStatus.SUCCEEDED_VERIFIED


# ---------------------------------------------------------------- malformed input and malformed rows


def test_params_must_be_an_object(tmp_path):
    h = Harness(tmp_path)
    fv = h.save(post_flow())
    with pytest.raises(InvalidRequest):
        h.runner.submit(flow_id=fv.id, params=["caption", "image"], authorized=True)


def test_extra_keys_wrong_types_and_oversize_payloads_create_no_run(tmp_path):
    h = Harness(tmp_path)
    fv = h.save(post_flow())  # declares additionalProperties: false and caption maxLength 2200
    bad_inputs = [{**PARAMS, "bogus": 1}, {**PARAMS, "extra": None}, {"caption": 7, "image": "a"},
                  {"caption": "x" * 300_000, "image": "a"}]
    for bad in bad_inputs:
        with pytest.raises(InvalidRequest):
            h.runner.submit(flow_id=fv.id, params=bad, authorized=True)
    assert h.store.list_runs() == []


async def test_one_malformed_run_row_does_not_stop_dispatch(tmp_path):
    h = Harness(tmp_path)
    fv = h.save(read_flow())
    bad = h.submit(fv.id, lane="biz-a/one")
    good = h.submit(fv.id, lane="biz-a/two")
    h.store._exec("UPDATE runs SET doc=? WHERE id=?", ('{"kind": "flow"}', bad.id))
    started = await h.runner.dispatch_once()
    await asyncio.gather(*started)
    assert h.runner.get(good.id).status == RunStatus.SUCCEEDED_VERIFIED


async def test_typed_text_is_not_written_to_the_step_journal(tmp_path):
    flow = {"id": "biz-a.social.note", "lane": "biz-a/social", "effect": "draft",
            "params": {"type": "object", "required": ["note"], "properties": {"note": {"type": "string"}}},
            "steps": [{"type": "navigate", "url": f"{BASE}/compose"},
                      {"type": "type", "locator": {"role": "textbox", "name": "Write a caption..."},
                       "param": "note"}]}
    h = Harness(tmp_path)
    run = await h.run(h.save(flow).id, {"note": "meet-at-the-lake-0427"})
    assert run.status == RunStatus.SUCCEEDED_UNVERIFIED  # no readback declared, so no verified claim
    assert "meet-at-the-lake" not in json.dumps(h.runner.steps(run.id))


# ---------------------------------------------------------------- templates, readbacks and secrets


async def test_url_params_are_percent_encoded_in_a_path(tmp_path):
    flow = {"id": "biz-a.social.user", "lane": "biz-a/social", "effect": "read", "domain": "social.example",
            "params": {"type": "object", "required": ["who"], "properties": {"who": {"type": "string"}}},
            "steps": [{"type": "navigate", "url": BASE + "/u/${who}"}]}
    h = Harness(tmp_path)
    await h.run(h.save(flow).id, {"who": "a/b?c=d&e"})
    assert ("navigate", f"{BASE}/u/a%2Fb%3Fc%3Dd%26e") in h.driver.log


async def test_a_url_param_cannot_point_a_flow_at_another_site(tmp_path):
    flow = {"id": "biz-a.social.visit", "lane": "biz-a/social", "effect": "read", "domain": "social.example",
            "params": {"type": "object", "required": ["target"], "properties": {"target": {"type": "string"}}},
            "steps": [{"type": "navigate", "url": "${target}"}]}
    h = Harness(tmp_path)
    await h.run(h.save(flow).id, {"target": "https://evil.example/"})
    assert ("navigate", "https://evil.example/") not in h.driver.log


async def test_readback_expectations_do_not_copy_param_values_into_the_proof(tmp_path):
    secret = "7f3c-hidden-9d2e"
    flow = {"id": "biz-a.social.pin", "lane": "biz-a/social", "effect": "draft",
            "params": {"type": "object", "required": ["pin"], "properties": {"pin": {"type": "string"}}},
            "steps": [{"type": "navigate", "url": f"{BASE}/compose"},
                      {"type": "fill", "locator": {"role": "textbox", "name": "Write a caption..."}, "param": "pin",
                       "readback": {"condition": {"kind": "value", "equals": "${pin}"}}}]}
    h = Harness(tmp_path)
    run = await h.run(h.save(flow).id, {"pin": secret})
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    assert secret not in json.dumps({"proof": run.proof, "steps": h.runner.steps(run.id)})


async def test_an_invalid_pattern_from_a_param_is_not_reported_as_a_lane_failure(tmp_path):
    flow = {"id": "biz-a.social.match", "lane": "biz-a/social", "effect": "read",
            "params": {"type": "object", "required": ["pat"], "properties": {"pat": {"type": "string"}}},
            "steps": [{"type": "navigate", "url": f"{BASE}/"},
                      {"type": "readback", "locator": {"role": "heading", "name": "Feed"},
                       "condition": {"kind": "text", "matches": "^${pat}$"}}]}
    h = Harness(tmp_path)
    run = await h.run(h.save(flow).id, {"pat": "("})
    assert run.status == RunStatus.FAILED
    assert "could not open the lane" not in run.error["message"], run.error


# ---------------------------------------------------------------- fairness and memory


async def test_a_long_backlog_in_one_workspace_does_not_starve_another(tmp_path):
    h = Harness(tmp_path)
    fv = h.save(read_flow())
    h.runner._lanes.add("biz-a/one")  # its lane is busy, so none of its runs can start
    for _ in range(1000):
        h.submit(fv.id, lane="biz-a/one")
    other = h.submit(fv.id, lane="biz-b/one")
    started = await h.runner.dispatch_once()
    await asyncio.gather(*started)
    assert h.runner.get(other.id).status == RunStatus.SUCCEEDED_VERIFIED


async def test_finished_runs_do_not_keep_wait_events_in_memory(tmp_path):
    h = Harness(tmp_path)
    fv = h.save(read_flow())
    for i in range(5):
        await h.run(fv.id, lane=f"biz-a/l{i}")
    assert h.runner._changed == {}


# ---------------------------------------------------------------- restarts at every step boundary

CRASH_POINTS = [
    ("click", "New post", "before"),
    ("click", "New post", "after"),
    ("upload", "Select from computer", "before"),
    ("upload", "Select from computer", "after"),
    ("fill", "Write a caption...", "before"),
    ("fill", "Write a caption...", "after"),
    ("click", "Share", "before"),
    ("click", "Share", "after"),
]


@pytest.mark.parametrize("op,name,when", CRASH_POINTS, ids=lambda v: v.replace(" ", "_").replace(".", ""))
async def test_a_crash_at_any_step_boundary_never_posts_twice(tmp_path, op, name, when):
    h = Harness(tmp_path)
    fv = h.save(post_flow())
    fired: list[str] = []

    def die(o, target, d):
        if o == op and getattr(target, "name", None) == name and not fired:
            fired.append(o)
            raise SimulatedCrash(f"node died {when} {op} {name!r}")

    (h.driver.hooks if when == "before" else h.driver.after_hooks).append(die)
    run = h.submit(fv.id, PARAMS)
    results = await asyncio.gather(*await h.runner.dispatch_once(), return_exceptions=True)
    assert any(isinstance(r, SimulatedCrash) for r in results)
    assert h.store.get_run(run.id).status == RunStatus.RUNNING  # the process died mid-run

    h2 = Harness(tmp_path, site=h.site, clock=h.clock)
    h2.runner.recover()
    await h2.runner.run_until_idle()
    final = h2.runner.get(run.id)
    posts = len(h.site.state["posts"])
    assert posts <= 1
    if final.status == RunStatus.SUCCEEDED_VERIFIED:
        assert posts == 1
    else:
        assert final.status == RunStatus.PARTIAL and final.needs_decision, final.error
        await h2.runner.run_until_idle()  # nothing runs again until a human decides
        assert len(h.site.state["posts"]) == posts


async def test_a_restart_while_a_retry_waits_keeps_the_retry(tmp_path):
    h = Harness(tmp_path, max_attempts=3, backoff_base_s=30)
    fv = h.save(post_flow())
    left = {"n": 1}

    def flaky(op, target, d):
        if op == "find" and target.name == "New post" and left["n"]:
            left["n"] -= 1
            raise DriverTimeout("page slow")

    h.driver.hooks.append(flaky)
    run = await h.run(fv.id, PARAMS)
    assert run.status == RunStatus.QUEUED and run.error["retrying"]

    h2 = Harness(tmp_path, site=h.site, clock=h.clock)
    assert h2.runner.recover() == {"requeued": 0, "needs_decision": 0}  # queued, not running: nothing to repair
    h.clock.advance(120)
    await h2.runner.run_until_idle()
    final = h2.runner.get(run.id)
    assert final.status == RunStatus.SUCCEEDED_VERIFIED and final.attempts == 2
    assert len(h.site.state["posts"]) == 1
