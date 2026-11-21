"""The signed-out check (AUD-22, openajar.runner.signed_out): an operation declaring
`readback.public: cookieless` is verified only when a signed-out visitor sees what the author
saw; otherwise `succeeded_unverified` with `visible_only_to_author`, re-checked later."""

from __future__ import annotations

import pytest
from connector_samples import LANE, SITES, bind, commit_op, connector, connector_site, live
from pydantic import ValidationError

from openajar.core.models import RunStatus
from openajar.runner import signed_out
from openajar.runner.connectors.compile import compile_connector
from openajar.runner.connectors.models import ReadbackContract
from openajar.runner.connectors.service import ConnectorService
from openajar.runner.fakes import OWNER, FakeLanes, FakeNotifier, Harness

CAPTION = "Autumn at the lake"
PARAMS = {"caption": CAPTION, "image": "photo.jpg"}
POST_URL = "https://social.example/u/biz-a"


def _harness(tmp_path, op=None, *, driver=None):
    h = Harness(tmp_path, site=connector_site(), services={"lanes": FakeLanes([LANE]), "notifier": FakeNotifier()},
                driver=driver)
    live(h.driver)
    doc = connector({"post_photo": op or commit_op()})
    ConnectorService(h.flows, sites=SITES).install(compile_connector(doc, sites=SITES), by=OWNER)
    bind(h)
    return h


async def _post(h):
    return await h.run("connector.social.post_photo", PARAMS, lane=LANE)


async def _recheck(h, seconds: float):
    h.clock.advance(seconds)
    for task in await h.runner.recheck_due():
        await task
    return h.runner.get


async def test_a_post_a_signed_out_visitor_sees_is_verified(tmp_path):
    h = _harness(tmp_path)
    run = await _post(h)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    public = run.proof["public"]
    assert public["check"] == "cookieless" and public["visible"] is True and public["url"] == POST_URL
    # the default check: the live readback's saved url, and the text the author's view showed there
    assert h.site.signed_out_reads == [(POST_URL, CAPTION)]
    assert h.store.step_states(run.id)[signed_out.CHECK_PATH] == {"url": POST_URL, "text": CAPTION}


async def test_a_post_only_its_author_sees_is_unverified_then_rechecked(tmp_path):
    h = _harness(tmp_path)
    h.site.signed_out[POST_URL] = False  # held by the site's filters
    run = await _post(h)
    assert run.status == RunStatus.SUCCEEDED_UNVERIFIED, run.proof
    assert run.proof["note"].startswith("visible_only_to_author:") and run.proof["public"]["visible"] is False
    assert all(rb["passed"] for rb in run.proof["readbacks"])  # the author's own readback passed
    assert run.recheck_at is not None and run.error is None
    await _recheck(h, 301)  # still hidden: the next re-check is scheduled, the note kept
    run = h.runner.get(run.id)
    assert run.status == RunStatus.SUCCEEDED_UNVERIFIED and run.proof["public"]["rechecked"] is True
    assert run.recheck_at is not None and len(h.site.signed_out_reads) == 2
    h.site.signed_out[POST_URL] = True  # the site released it
    await _recheck(h, 1801)
    run = h.runner.get(run.id)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED and run.recheck_at is None
    assert run.proof["note"].startswith("verified by a re-check")


async def test_still_hidden_after_every_recheck_says_why(tmp_path):
    h = _harness(tmp_path)
    h.site.signed_out[POST_URL] = False
    run = await _post(h)
    for wait in (301, 1801, 7201):
        await _recheck(h, wait)
    run = h.runner.get(run.id)
    assert run.status == RunStatus.SUCCEEDED_UNVERIFIED and run.recheck_at is None
    assert "still unverified after 3 re-checks: visible_only_to_author" in run.proof["note"]


async def test_a_driver_that_cannot_read_signed_out_leaves_the_run_unverified(tmp_path):
    class NoSignedOut:
        def __getattr__(self, name):
            if name == "read_signed_out":
                raise AttributeError(name)
            return getattr(inner, name)

    h = _harness(tmp_path)
    inner = h.driver
    from openajar.runner.fakes import driver_factory
    h.runner.set_driver_factory(driver_factory(NoSignedOut()))
    run = await _post(h)
    assert run.status == RunStatus.SUCCEEDED_UNVERIFIED
    assert run.proof["note"] == "the signed-out check could not run: this node cannot open a page signed out"
    assert run.recheck_at is not None


async def test_a_failed_signed_out_read_is_could_not_check(tmp_path):
    h = _harness(tmp_path)

    def broken(url, text):
        raise RuntimeError("the throwaway browser did not start")

    h.site.signed_out = broken
    run = await _post(h)
    assert run.status == RunStatus.SUCCEEDED_UNVERIFIED and run.proof["public"]["visible"] is None
    assert "the throwaway browser did not start" in run.proof["note"]


async def test_public_check_names_the_url_and_a_param_as_the_text(tmp_path):
    op = commit_op()
    op["readback"]["public_check"] = {"url": "post.url", "text": "param:caption"}
    h = _harness(tmp_path, op)
    run = await _post(h)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.proof
    assert h.site.signed_out_reads == [(POST_URL, CAPTION)]


async def test_a_permalink_outside_the_domain_is_never_opened(tmp_path):
    op = commit_op()
    op["readback"]["public_check"] = {"url": "post.attributes.href", "text": "post.text"}  # nothing saved there
    h = _harness(tmp_path, op)
    run = await _post(h)
    assert run.status == RunStatus.SUCCEEDED_UNVERIFIED and h.site.signed_out_reads == []
    assert "saved no permalink at 'post.attributes.href'" in run.proof["note"]
    found, why = signed_out.resolve({"url": "post.url", "text": "post.text"},
                                    {"vars": {"post": {"url": "https://evil.example/p/1", "text": "x"}}},
                                    "social.example")
    assert found is None and "not under the operation's domain" in why
    found, why = signed_out.resolve({"url": "post.url", "text": "post.text"},
                                    {"vars": {"post": {"url": "javascript:alert(1)", "text": "x"}}}, "social.example")
    assert found is None and "plain http(s)" in why


async def test_a_failed_live_readback_never_runs_the_signed_out_check(tmp_path):
    from openajar.runner.fakes import El

    h = _harness(tmp_path)
    h.site.route("https://social.example/u/*", lambda s, d: El("document", "biz-a").add(El("main", "biz-a")))
    run = await _post(h)  # posted, but the author's profile shows no post: the live readback fails
    assert run.status == RunStatus.PARTIAL, run.status
    assert h.site.signed_out_reads == [] and "public" not in (run.proof or {})


def test_the_contract_needs_a_saved_value_or_a_public_check():
    with pytest.raises(ValidationError, match="needs readback.live.save_as"):
        ReadbackContract.model_validate({"public": "cookieless"})
    with pytest.raises(ValidationError, match="public_check needs public"):
        ReadbackContract.model_validate({"public_check": {"url": "post.url", "text": "post.text"}})
    with pytest.raises(ValidationError):
        ReadbackContract.model_validate({"public": "cookieless", "public_check": {"url": "post.url", "text": "p:x"}})
    ok = ReadbackContract.model_validate({"public": "cookieless", "public_check": {"url": "post.url",
                                                                                     "text": "param:caption"}})
    assert ok.signed_out_check() == {"url": "post.url", "text": "param:caption"}
    assert ReadbackContract.model_validate({"live": {"save_as": "post"}, "public": "cookieless"}
                                           ).signed_out_check() == {"url": "post.url", "text": "post.text"}
    assert ReadbackContract.model_validate({"live": {"save_as": "post"}}).signed_out_check() is None


def test_the_proof_never_carries_the_text_or_a_query():
    rec = signed_out.record(False, url="https://social.example/p/1?token=abc#x", status=200, note="n")
    assert rec == {"check": "cookieless", "visible": False, "url": "https://social.example/p/1?...", "status": 200,
                   "note": "n"}


# ---- the lane manager's part (openajar.node.lanes.read_signed_out) ----


async def test_the_lane_lends_its_browser_on_a_display_of_its_own(tmp_path):
    from test_lanes import make

    from openajar.core.errors import ServiceUnavailable
    from openajar.core.models import LaneSpec
    from openajar.node.lanes import ResourcesLow

    mgr, engine, displays, _ = make(tmp_path, max_slots=4)
    await mgr.create(LaneSpec(id=LANE, sites=["social.example"]))
    with pytest.raises(ServiceUnavailable, match="not open"):  # cold: no browser to lend its options
        await mgr.read_signed_out(LANE, POST_URL, CAPTION)
    calls = []
    async with mgr.using(LANE):
        session = engine.sessions[-1]

        async def read(url, text, **kw):
            calls.append((url, text, kw, dict(displays.slots)))
            return {"visible": True}

        session.read_signed_out = read
        assert await mgr.read_signed_out(LANE, POST_URL, CAPTION, timeout_ms=5000) == {"visible": True}
        url, text, kw, slots = calls[0]
        assert (url, text) == (POST_URL, CAPTION) and kw["timeout_ms"] == 5000 and kw["lane_id"] == LANE
        assert kw["sites"] == ("social.example",) and kw["strict"] is False
        key = f"{LANE}+signed-out"
        assert key in slots and slots[key] != slots[LANE]  # never the lane's display
        assert kw["display"] == f":{displays.config.display_base + slots[key]}"
        assert key not in displays.slots and key in displays.released  # given back
        mgr.memory_floor_mb, mgr.memory_probe = 800, lambda: 500
        with pytest.raises(ResourcesLow, match="signed-out browser was not started"):
            await mgr.read_signed_out(LANE, POST_URL, CAPTION)
        assert len(calls) == 1
    await mgr.shutdown()


async def test_the_flow_driver_reads_signed_out_through_its_lane():
    from contextlib import asynccontextmanager

    from openajar.core.errors import ServiceUnavailable
    from openajar.core.runtime import Runtime
    from openajar.node import flow_driver as fd

    asked = []

    class Lanes:
        def control(self, lane_id):
            return None

        @asynccontextmanager
        async def using(self, lane_id, **kw):
            yield object()

        async def read_signed_out(self, lane_id, url, text, *, timeout_ms=20_000):
            asked.append((lane_id, url, text, timeout_ms))
            return {"visible": False}

    async with fd.driver_factory(Runtime(lanes=Lanes()))(LANE) as driver:
        assert await driver.read_signed_out(POST_URL, CAPTION, timeout_ms=3000) == {"visible": False}
    assert asked == [(LANE, POST_URL, CAPTION, 3000)]
    bare = fd.PlaywrightFlowDriver(object(), lane_id=LANE)
    with pytest.raises(ServiceUnavailable):
        await bare.read_signed_out(POST_URL, CAPTION)
