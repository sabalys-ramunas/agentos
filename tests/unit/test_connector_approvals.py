"""Connector approvals, pace, health and taint (docs/design/connectors.md 4.7, 4.8, 4.10 and
4.12, Brief C): exact content down to the image's bytes, a standing approval bound to the
contract hash, pace that no approval overrides, one notification per breakage, and outputs
that label the grant family."""

import hashlib
from datetime import timedelta

import pytest
from connector_samples import (
    POST_PARAMS as PARAMS,
)
from connector_samples import (
    SAMPLE_POSTS,
    SITES,
    Inbox,
    bind,
    comments_op,
    commit_op,
    connector,
    connector_site,
    live,
)
from test_policy_support import Clock

from agentos.core.models import Effect, RunStatus
from agentos.policy.approvals import call_targets, content_digest
from agentos.policy.service import PolicyService, family_key
from agentos.policy.util import content_hash
from agentos.runner import tools as runner_tools
from agentos.runner.connectors.compile import compile_connector
from agentos.runner.connectors.service import ConnectorService
from agentos.runner.connectors.surfaces import APPROVAL_KEYS, ConnectorBroken, PaceLimited
from agentos.runner.connectors.taint import output_labels
from agentos.runner.fakes import OWNER, FakeApprovals, FakeCtx, FakeLanes, FakeNotifier, FakePolicy, Harness

FID = "connector.social.post_photo"
LANE = "biz-a/social"
PHOTO = b"\xff\xd8\xff\xe0 a small photo"


def _no_pace() -> dict:
    """commit_op with a pace that never binds in a test (a commit operation must declare one)."""
    op = commit_op()
    op["pace"] = {"min_gap_minutes": 0, "per_day": 1000}
    return op


def _harness(tmp_path, *, ops=None, site=None, version="1.0.0"):
    inbox = Inbox(tmp_path / "inbox")
    h = Harness(tmp_path, site=site or connector_site(),
                services={"lanes": FakeLanes([LANE]), "notifier": FakeNotifier(), "files": inbox})
    live(h.driver)
    install(h, ops if ops is not None else {"post_photo": commit_op()}, version)
    bind(h)
    inbox.put(LANE, "photo.jpg", PHOTO)
    return h, inbox


def install(h, ops, version):
    doc = connector(ops, version=version)
    ConnectorService(h.flows, sites=SITES).install(compile_connector(doc, sites=SITES), by=OWNER)


def _post(h):
    return next(s for s in runner_tools.tool_specs(h.runtime) if s.name == "social_post_photo").handler


def _policy(h, tmp_path):
    svc = PolicyService(tmp_path / "policy", runtime=h.runtime, clock=Clock())
    h.runtime.register("policy", svc)
    _, tok = svc.issue(subject={"id": "agent-a"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    return svc, tok


def _standing(svc, h, *, count=10):
    fv = h.flows.get(FID)
    _, content = h.runner.surfaces.approval(fv, h.runner.connector_target(fv, LANE, PARAMS))
    target = f"connector:social.post_photo@{fv.definition.connector['contract']}"
    return svc.approvals.add_standing(effects=["commit"], lanes=[LANE], until=svc.clock() + 86_400, count=count,
                                      targets=[target], contents=[content_digest(content)], by="owner")


def _uses(svc, sa_id):
    return next(s for s in svc.approvals.list_standing() if s.id == sa_id).uses


# ---------------------------------------------------------------- the approval content


async def test_the_approval_binds_the_exact_content_down_to_one_byte_of_the_image(tmp_path):
    h, inbox = _harness(tmp_path)
    fv = h.flows.get(FID)
    target = h.runner.connector_target(fv, LANE, PARAMS)
    summary, content = h.runner.surfaces.approval(fv, target)
    assert set(content) == APPROVAL_KEYS
    assert content["operation"] == "social.post_photo" and content["surface"] == LANE
    assert content["contract"] == fv.definition.connector["contract"] != fv.version
    assert content["acts_as"] == {"kind": "account", "id": "biz-a"}
    assert content["params"] == {"caption": PARAMS["caption"], "image": "sha256:" + hashlib.sha256(PHOTO).hexdigest()}
    assert summary.startswith("Post a photo with a caption (social.post_photo 1.0.0) as biz-a (account) on "
                              "biz-a/social, through biz-a/social: caption 'Autumn at the lake', image 1 file (sha256 ")
    assert "\u2014" not in summary
    assert f"connector:social.post_photo@{content['contract']}" in call_targets("social_post_photo", content, LANE)
    assert not any(t.startswith("connector:") for t in call_targets("social_post_photo", content, "biz-b/social"))

    inbox.put(LANE, "photo.jpg", PHOTO[:-1] + b"t")  # one byte
    _, changed = h.runner.surfaces.approval(fv, target)
    assert changed["params"]["image"] != content["params"]["image"]
    assert content_hash(changed) != content_hash(content)

    # the tool asks the owner with exactly that content and summary, on the surface
    ctx = FakeCtx(h.runtime, policy=FakePolicy(FakeApprovals()))
    out = await _post(h)(ctx, on=LANE, caption=PARAMS["caption"], image="photo.jpg", wait_seconds=0)
    assert out["status"] == "waiting_approval" and out["surface"] == LANE
    asked = ctx.authorized[-1]
    assert asked["effect"] == Effect.COMMIT and asked["lane_id"] == LANE and asked["content"] == changed
    assert asked["domain"] == "social.example" and "caption 'Autumn at the lake'" in asked["summary"]
    assert h.site.state["posts"] == []


async def test_a_file_that_is_not_in_the_lanes_inbox_is_refused_before_anyone_is_asked(tmp_path):
    h, _ = _harness(tmp_path)
    ctx = FakeCtx(h.runtime, policy=FakePolicy(FakeApprovals()))
    with pytest.raises(Exception, match="put it in the inbox of biz-a/social"):
        await _post(h)(ctx, on=LANE, caption="x", image="elsewhere.jpg", wait_seconds=0)
    assert ctx.authorized == [] and h.store.list_runs() == []


async def test_a_standing_approval_on_the_contract_survives_a_locator_repair(tmp_path):
    h, _ = _harness(tmp_path, ops={"post_photo": _no_pace()})
    svc, tok = _policy(h, tmp_path)
    keys = iter(range(100))

    async def post():  # the same post again on purpose: a new key each time (a keyless retry is the same run)
        return await _post(h)(svc.context_for_token(tok, tool="social_post_photo"), on=LANE,
                              caption=PARAMS["caption"], image="photo.jpg", wait_seconds=0,
                              idempotency_key=f"post-{next(keys)}")

    first = await post()
    assert first["status"] == "waiting_approval"
    svc.approvals.decide(first["approval_id"], "approve", by="owner")
    contract = h.flows.get(FID).definition.connector["contract"]
    targets, contents, lanes = svc.approvals.binding_from([first["approval_id"]])
    assert targets == [f"connector:social.post_photo@{contract}"] and lanes == [LANE]
    sa = svc.approvals.add_standing(effects=["commit"], lanes=lanes, until=svc.clock() + 86_400, count=10,
                                    targets=targets, contents=contents, by="owner")
    assert (await post())["status"] == "queued" and _uses(svc, sa.id) == 1

    # a repair that only changes locators keeps the contract hash, so the standing approval holds
    old = h.flows.get(FID).version
    repaired = _no_pace()
    repaired["steps"][4]["locator"] = {"role": "textbox", "name": "Write a caption...", "within": {"role": "dialog"}}
    install(h, {"post_photo": repaired}, "1.0.1")
    fv = h.flows.get(FID)
    assert fv.version != old and fv.definition.connector["contract"] == contract
    again = await post()
    assert again["status"] == "queued" and again["version"] == fv.version and _uses(svc, sa.id) == 2

    # a contract change (the caption's schema) is a different operation to approve
    changed = _no_pace()
    changed["input"]["properties"]["caption"]["maxLength"] = 2000
    install(h, {"post_photo": changed}, "1.1.0")
    assert h.flows.get(FID).definition.connector["contract"] != contract
    assert (await post())["status"] == "waiting_approval" and _uses(svc, sa.id) == 2


def test_repointing_a_surface_to_another_login_lapses_its_standing_approvals(tmp_path):
    h, _ = _harness(tmp_path, ops={"post_photo": _no_pace()})
    svc, _ = _policy(h, tmp_path)
    h.runtime.register("approvals", svc.approvals)
    sa = _standing(svc, h)
    # a change that keeps the login and who it posts as keeps the standing approval
    bind(h, acts_as={"kind": "account", "user_id": "biz-a", "name": "Biz A"})
    assert [s.id for s in svc.approvals.list_standing()] == [sa.id]
    # the same surface through another lane is not what the owner approved
    h.runtime.get("lanes").add("biz-a/social-2")
    bind(h, lane="biz-a/social-2")
    assert svc.approvals.list_standing() == []


# ---------------------------------------------------------------- pace


async def test_pace_refuses_the_second_post_inside_the_gap_even_under_a_standing_approval(tmp_path):
    h, _ = _harness(tmp_path)  # pace: min_gap_minutes 90, per_day 3
    svc, tok = _policy(h, tmp_path)
    sa = _standing(svc, h)

    async def post():
        out = await _post(h)(svc.context_for_token(tok, tool="social_post_photo"), on=LANE,
                             caption=PARAMS["caption"], image="photo.jpg", wait_seconds=0)
        await h.runner.run_until_idle()
        return h.runner.get(out["id"])

    assert (await post()).status == RunStatus.SUCCEEDED_VERIFIED and _uses(svc, sa.id) == 1
    with pytest.raises(PaceLimited, match="at most once every 90 minutes on biz-a/social") as e:
        await post()
    assert e.value.code == "pace_limited" and _uses(svc, sa.id) == 1  # refused before any approval
    with pytest.raises(PaceLimited):  # every other entry point too
        h.runner.submit(flow_id=FID, params=PARAMS, surface=LANE, principal=OWNER, authorized=True)
    for _ in range(2):
        h.clock.advance(91 * 60)
        assert (await post()).status == RunStatus.SUCCEEDED_VERIFIED
    h.clock.advance(91 * 60)
    with pytest.raises(PaceLimited, match="at most 3 times in a day"):
        await post()
    assert len(h.site.state["posts"]) == 3 and _uses(svc, sa.id) == 3


async def test_a_run_over_its_pace_waits_at_dispatch(tmp_path):
    h, _ = _harness(tmp_path)
    first = h.submit(FID, PARAMS, lane=LANE)
    await h.runner.run_until_idle()
    first = h.runner.get(first.id)
    assert first.status == RunStatus.SUCCEEDED_VERIFIED
    # a run queued around the pace (a race, another entry point): it waits for the gap, held
    late = first.model_copy(update={"id": "run_late", "status": RunStatus.QUEUED, "started_at": None,
                                    "finished_at": None, "attempts": 0, "output": None, "proof": None, "error": None,
                                    "created_at": h.clock.now(), "updated_at": h.clock.now()})
    h.store.insert_run(late)
    for task in await h.runner.dispatch_once():
        await task
    late = h.runner.get("run_late")
    assert late.status == RunStatus.QUEUED and late.error["control"] == "pace"
    assert late.not_before == first.started_at + timedelta(minutes=90)
    assert late.held == 1 and len(h.site.state["posts"]) == 1


# ---------------------------------------------------------------- health


async def test_a_broken_operation_tells_the_owner_once_and_holds_its_schedules(tmp_path):
    # the site's Share button now reads "Bendrinti" for an English account: the commit locator finds nothing
    h, _ = _harness(tmp_path, ops={"post_photo": _no_pace()}, site=connector_site(lang="lt"))
    bind(h, id="biz-a/social-two")  # a second surface on the same login
    for surface in (LANE, "biz-a/social-two"):
        run = await h.run(FID, PARAMS, lane=surface)
        assert run.status == RunStatus.FAILED
        assert run.error["code"] == "connector_contract_broken" and run.error["cause"] == "element_not_found"
    assert h.site.state["posts"] == []
    sent = h.runtime.get("notifier").sent
    assert [m["title"] for m in sent] == ["A connector operation changed"]
    assert "social.post_photo stopped on biz-a/social at step 6" in sent[0]["body"]
    assert "Nothing was posted." in sent[0]["body"] and "\u2014" not in sent[0]["body"]
    health = h.runner.health
    for surface in (LANE, "biz-a/social-two"):
        hs = health.get("social.post_photo", surface)
        assert hs["state"] == "broken" and hs["cause"] == "element_not_found" and hs["step"] == "6"

    # while broken: the tool answers at once, schedules skip their slots, queued runs wait
    with pytest.raises(ConnectorBroken, match="broken on biz-a/social") as e:
        await _post(h)(FakeCtx(h.runtime), on=LANE, caption=PARAMS["caption"], image="photo.jpg")
    assert e.value.code == "connector_broken"
    s, _ = h.scheduler.add(flow=FID, on=LANE, params=PARAMS, cron="*/5 * * * *", principal=OWNER)
    h.clock.advance(600)
    assert (await h.scheduler.tick())["fired"] == 0
    assert h.scheduler.get(s.id).state["held"]["reason"] == "connector_broken"
    last = h.runner.list(flow_id=FID, limit=1)[0]
    queued = last.model_copy(update={"id": "run_queued", "status": RunStatus.QUEUED, "started_at": None,
                                     "finished_at": None, "attempts": 0, "error": None, "proof": None})
    h.store.insert_run(queued)
    for task in await h.runner.dispatch_once():
        await task
    queued = h.runner.get("run_queued")
    assert queued.status == RunStatus.QUEUED and queued.error["control"] == "connector_broken"
    await h.runner.cancel("run_queued")
    assert len(h.runtime.get("notifier").sent) == 1

    # a repaired version clears it: the schedule fires again and the post goes out
    repaired = _no_pace()
    repaired["steps"][5]["locator"]["names"] = {"en": "Bendrinti", "lt": "Bendrinti"}
    install(h, {"post_photo": repaired}, "1.0.1")
    assert health.get("social.post_photo", LANE)["state"] == "unknown"
    h.clock.advance(600)
    assert (await h.scheduler.tick())["fired"] == 1
    await h.runner.run_until_idle()
    assert len(h.site.state["posts"]) == 1 and health.get("social.post_photo", LANE)["state"] == "healthy"
    assert "held" not in h.scheduler.get(s.id).state


# ---------------------------------------------------------------- taint


async def test_untrusted_output_labels_the_grant_family(tmp_path):
    h, _ = _harness(tmp_path, ops={"list_comments": comments_op()}, site=connector_site(posts=SAMPLE_POSTS))
    svc, tok = _policy(h, tmp_path)
    g = svc.resolve(tok)[1]
    family = family_key(g)
    tool = next(s for s in runner_tools.tool_specs(h.runtime) if s.name == "social_list_comments").handler
    out = await tool(svc.context_for_token(tok, tool="social_list_comments"), on=LANE, wait_seconds=0)
    assert svc.taint.get(family) == set()
    await h.runner.run_until_idle()
    run = h.runner.get(out["id"])
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    assert "comments[].body" in run.output["untrusted_fields"] and run.output["source"]["surface"] == LANE
    assert svc.taint.get(family) == {"untrusted"}
    # the owner in person (the CLI, the panel) neither adds nor receives labels
    await h.run("connector.social.list_comments", {}, lane=LANE, principal=OWNER)
    assert svc.taint.get(family_key(svc.owner_grant())) == set()


def test_output_labels_name_untrusted_and_private():
    assert output_labels(None) == set()
    assert output_labels({"untrusted_fields": [], "private_fields": []}) == set()
    assert output_labels({"untrusted_fields": ["items[].body"]}) == {"untrusted"}
    assert output_labels({"untrusted_fields": ["a"], "private_fields": ["items[].customer"]}) == {"untrusted", "private"}


# ---------------------------------------------------------------- staging a file for a surface


async def test_files_put_on_a_surface_stages_the_photo_where_the_run_reads_it(tmp_path):
    import base64

    from agentos.core.config import NodeConfig
    from agentos.node import io_tools

    png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                        "0000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")
    config = NodeConfig(home=tmp_path / "home")
    config.lane_dir("personal/social").mkdir(parents=True, mode=0o700)  # the lane manager makes it
    h = Harness(tmp_path, site=connector_site(), services={"config": config, "lanes": FakeLanes(["personal/social"]),
                                                           "notifier": FakeNotifier()})
    install(h, {"post_photo": commit_op()}, "1.0.0")
    bind(h, id="biz-a/social-brand", lane="personal/social")  # biz-a's account, through a personal login
    ctx = FakeCtx(h.runtime)
    out = await io_tools.files_put(ctx, on="biz-a/social-brand", name="photo", content_base64=base64.b64encode(png).decode())
    file_id = out["file"]["id"]
    assert ctx.authorized[-1]["lane_id"] == "biz-a/social-brand"  # checked against the surface, not the login
    assert "connector tool with on='biz-a/social-brand'" in out["next"]
    assert (config.lane_dir("personal/social") / "files" / "inbox" / file_id).is_dir()
    fv = h.flows.get(FID)
    target = h.runner.connector_target(fv, "biz-a/social-brand", {"caption": "Hi", "image": file_id})
    _, content = h.runner.surfaces.approval(fv, target)
    assert content["params"]["image"] == "sha256:" + hashlib.sha256(png).hexdigest()
    with pytest.raises(Exception, match="no file in_000000000000"):
        h.runner.surfaces.approval(fv, h.runner.connector_target(fv, "biz-a/social-brand",
                                                                 {"caption": "Hi", "image": "in_000000000000"}))


async def test_a_connector_post_outside_the_lanes_waking_hours_is_refused_with_when_it_may_post(tmp_path):
    """AUD-13: the lane's posting pace holds for a typed connector tool too, before any approval."""
    from agentos.policy.pace import LanePaced

    h, _ = _harness(tmp_path, ops={"post_photo": _no_pace()})
    svc, tok = _policy(h, tmp_path)
    sa = _standing(svc, h)
    svc.set_lane_pace(LANE, {"active_hours": ["09:00-17:00"], "timezone": "UTC"})  # the clock reads 08:00 UTC
    with pytest.raises(LanePaced) as e:
        await _post(h)(svc.context_for_token(tok, tool="social_post_photo"), on=LANE,
                       caption=PARAMS["caption"], image="photo.jpg", wait_seconds=0)
    assert e.value.hit["rule"] == "active_hours" and "it may next post at 09:00 today lane time (UTC)" in e.value.message
    assert _uses(svc, sa.id) == 0 and h.store.list_runs() == [] and h.site.state["posts"] == []
