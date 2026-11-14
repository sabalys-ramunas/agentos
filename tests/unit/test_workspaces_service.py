"""The workspace service: records, templates, readiness and the queue, plan and apply, races and
partial failures. Runs against an in-memory lane manager and a temporary home. No browser, no proxy."""

from __future__ import annotations

import pytest

from agentos.core.errors import InvalidRequest, LaneNotFound, NotFound, ServiceUnavailable
from agentos.workspaces.errors import WorkspaceConflict
from agentos.workspaces.fakes import Harness
from agentos.workspaces.models import PLAN_MAX_LANES
from agentos.workspaces.service import build, derived_name

SOCIAL_LANES = ["instagram", "facebook", "reddit", "medium", "blog", "linkedin"]


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path / "home")
    yield harness
    harness.close()


def write_template(h: Harness, name: str, text: str) -> None:
    (h.home / "templates").mkdir(parents=True, exist_ok=True)
    (h.home / "templates" / f"{name}.toml").write_text(text, encoding="utf-8")


# ---- workspace records ----------------------------------------------------------

def test_create_uses_the_id_as_the_name_when_none_is_given(h):
    ws = h.service.create_workspace("biz-a")
    assert (ws.name, ws.derived, ws.summary, ws.health) == ("Biz A", False, "no lanes yet", "idle")
    assert derived_name("acme") == "Acme"


def test_create_keeps_the_fields_it_is_given_and_lowercases_the_color(h):
    ws = h.service.create_workspace("acme", name="  Acme Ltd ", color="#4F7CFF", default_proxy="lt1",
                                    notes="main brand")
    assert (ws.name, ws.color, ws.default_proxy, ws.notes) == ("Acme Ltd", "#4f7cff", "lt1", "main brand")
    assert ws.created_at and ws.updated_at


def test_create_refuses_a_duplicate_and_points_at_update(h):
    h.service.create_workspace("acme")
    with pytest.raises(WorkspaceConflict, match="already exists; change it with: agentos workspace update acme"):
        h.service.create_workspace("acme")


def test_workspace_id_limits_are_the_lane_id_limits(h):
    assert h.service.create_workspace("a" * 31).id == "a" * 31
    with pytest.raises(InvalidRequest, match="workspace id must be 1 to 31"):
        h.service.create_workspace("a" * 32)
    with pytest.raises(InvalidRequest, match="workspace id must be"):
        h.service.create_workspace("Acme Ltd")


def test_unknown_default_proxy_is_refused_and_no_proxies_is_its_own_message(h):
    with pytest.raises(InvalidRequest, match="proxy 'lt9' is not configured on this node. The owner sees"):
        h.service.create_workspace("acme", default_proxy="lt9")
    empty = Harness(h.home / "empty", proxies=())
    try:
        with pytest.raises(InvalidRequest, match="no proxies are set up on this node yet"):
            empty.service.create_workspace("acme", default_proxy="lt1")
    finally:
        empty.close()


def test_color_and_notes_are_checked(h):
    with pytest.raises(InvalidRequest, match="color must look like #rrggbb"):
        h.service.create_workspace("acme", color="blue")
    with pytest.raises(InvalidRequest, match="at most 2000 characters"):
        h.service.create_workspace("acme", notes="x" * 2001)
    with pytest.raises(InvalidRequest, match="control characters"):
        h.service.create_workspace("acme", name="bad\x07name")


def test_update_changes_only_the_fields_it_is_given(h):
    h.service.create_workspace("acme", name="Acme", color="#111111", default_proxy="lt1", notes="n")
    ws = h.service.update_workspace("acme", {"name": "Acme Two"})
    assert (ws.name, ws.color, ws.default_proxy, ws.notes) == ("Acme Two", "#111111", "lt1", "n")
    cleared = h.service.update_workspace("acme", {"color": None, "default_proxy": None})
    assert cleared.color is None and cleared.default_proxy is None


def test_update_refuses_clearing_the_name_and_unknown_fields(h):
    h.service.create_workspace("acme")
    with pytest.raises(InvalidRequest, match="name cannot be cleared"):
        h.service.update_workspace("acme", {"name": None})
    with pytest.raises(InvalidRequest, match="a workspace can change: name, color, default_proxy, notes"):
        h.service.update_workspace("acme", {"id": "other"})


def test_update_on_a_derived_workspace_makes_it_a_record(h):
    h.lanes.add("legacy/instagram")
    before = h.service.get_workspace("legacy")
    assert before.derived and before.name == "Legacy"
    after = h.service.update_workspace("legacy", {"color": "#00ff00"})
    assert after.derived is False and after.color == "#00ff00" and after.name == "Legacy"


def test_a_workspace_that_only_exists_as_a_lane_prefix_is_listed_with_a_derived_name(h):
    h.lanes.add("legacy-co/reddit", proxy="lt1")
    h.service.create_workspace("acme")
    listed = {ws.id: ws for ws in h.service.list_workspaces()}
    assert set(listed) == {"acme", "legacy-co"}
    assert listed["legacy-co"].derived and listed["legacy-co"].name == "Legacy Co"
    assert [lane.id for lane in listed["legacy-co"].lanes] == ["legacy-co/reddit"]
    assert listed["legacy-co"].lanes[0].proxy == "lt1"


def test_unknown_workspace_is_not_found(h):
    with pytest.raises(NotFound, match="no workspace 'ghost'"):
        h.service.get_workspace("ghost")
    with pytest.raises(NotFound):
        h.service.update_workspace("ghost", {"name": "x"})
    with pytest.raises(NotFound):
        h.service.delete_workspace("ghost")


def test_delete_is_refused_while_the_workspace_has_lanes(h):
    h.service.create_workspace("acme")
    h.lanes.add("acme/instagram")
    h.lanes.add("acme/reddit")
    with pytest.raises(WorkspaceConflict, match="still has 2 lanes; a workspace can only be deleted once it has none"):
        h.service.delete_workspace("acme")
    assert h.service.get_workspace("acme").total == 2


def test_delete_an_empty_workspace_removes_its_record(h):
    h.service.create_workspace("acme")
    h.service.delete_workspace("acme")
    with pytest.raises(NotFound):
        h.service.get_workspace("acme")


def test_list_for_a_restricted_caller_leaves_out_lanes_outside_the_grant(h):
    h.lanes.add("acme/instagram")
    h.lanes.add("beta/instagram")
    h.service.create_workspace("acme")
    h.service.create_workspace("beta")
    listed = h.service.list_workspaces(allowed=lambda lane_id: lane_id.startswith("acme/"))
    assert [ws.id for ws in listed] == ["acme"]
    assert [lane.id for lane in listed[0].lanes] == ["acme/instagram"]


# ---- templates ----------------------------------------------------------------------

def test_templates_list_builtins_first_and_report_a_bad_file_without_crashing(h):
    write_template(h, "shop", 'title = "Shop"\n[[surfaces]]\nname = "store"\nplatform = "shopify"\n')
    write_template(h, "broken", "[[surfaces\n")
    templates, problems = h.service.templates()
    assert [t.id for t in templates][:2] == ["personal", "social-6"]
    assert "shop" in [t.id for t in templates] and "broken" not in [t.id for t in templates]
    assert [(p.file, p.template) for p in problems] == [("broken.toml", "broken")]


def test_a_broken_template_asked_for_by_name_says_why(h):
    write_template(h, "broken", "[[surfaces\n")
    with pytest.raises(InvalidRequest, match="template 'broken' cannot be used: not valid TOML"):
        h.service.get_template("broken")
    with pytest.raises(NotFound, match="no template 'ghost'"):
        h.service.get_template("ghost")
    with pytest.raises(InvalidRequest, match="template id must be"):
        h.service.get_template("../etc")


# ---- readiness and the queue ----------------------------------------------------------

def test_a_lane_with_no_record_is_needs_login_and_queued(h):
    h.lanes.add("acme/instagram")
    queue = h.service.queue()
    assert [lane.id for lane in queue.lanes] == ["acme/instagram"]
    assert queue.lanes[0].readiness == "needs_login" and queue.summary == "0 of 1 ready"


def test_readiness_moves_a_lane_out_of_the_queue_and_counts_it(h):
    h.lanes.add("acme/instagram")
    h.lanes.add("acme/reddit")
    view = h.service.set_readiness("acme/instagram", "ready", by="owner")
    assert view.readiness == "ready" and view.updated_by == "owner" and view.updated_at
    queue = h.service.queue()
    assert [lane.id for lane in queue.lanes] == ["acme/reddit"]
    assert (queue.ready, queue.total, queue.summary) == (1, 2, "1 of 2 ready")
    assert queue.workspaces[0].summary == "1 of 2 ready"


def test_needs_attention_needs_a_note_and_sorts_first(h):
    h.lanes.add("acme/instagram")
    h.lanes.add("acme/reddit")
    with pytest.raises(InvalidRequest, match="needs_attention needs a note"):
        h.service.set_readiness("acme/reddit", "needs_attention", by="owner")
    h.service.set_readiness("acme/reddit", "needs_attention", note="phone code asked", by="owner")
    queue = h.service.queue()
    assert [lane.id for lane in queue.lanes] == ["acme/reddit", "acme/instagram"]
    assert queue.lanes[0].note == "phone code asked" and queue.needs_attention == 1 and queue.needs_login == 1


def test_readiness_states_and_their_wording_are_checked(h):
    h.lanes.add("acme/instagram")
    with pytest.raises(InvalidRequest, match="state must be one of: needs_login, ready, needs_attention"):
        h.service.set_readiness("acme/instagram", "logged_in", by="owner")
    with pytest.raises(InvalidRequest, match="lane id must look like"):
        h.service.set_readiness("Acme/Instagram", "ready", by="owner")


def test_readiness_of_a_lane_that_does_not_exist_is_lane_not_found(h):
    with pytest.raises(LaneNotFound) as info:
        h.service.set_readiness("acme/ghost", "ready", by="owner")
    assert info.value.code == "lane_not_found"
    assert h.service.store.lane_row("acme/ghost") is None  # nothing was written for it


def test_a_queue_for_one_workspace_only_counts_that_workspace(h):
    h.lanes.add("acme/instagram")
    h.lanes.add("beta/instagram")
    queue = h.service.queue("acme")
    assert [lane.id for lane in queue.lanes] == ["acme/instagram"] and queue.total == 1
    with pytest.raises(NotFound):
        h.service.queue("ghost")


def test_the_queue_respects_a_grant(h):
    h.lanes.add("acme/instagram")
    h.lanes.add("beta/instagram")
    queue = h.service.queue(allowed=lambda lane_id: lane_id.startswith("beta/"))
    assert [lane.id for lane in queue.lanes] == ["beta/instagram"] and queue.total == 1


# ---- plan --------------------------------------------------------------------------

def test_plan_creates_nothing(h):
    h.service.create_workspace("acme")
    plan = h.service.plan("social-6", ["acme"], proxy_per_workspace={"acme": "lt1"})
    assert plan.public()["ok"] and plan.count("create") == 6
    assert h.lanes.created == [] and h.service.store.lane_rows() == {}


def test_plan_needs_known_workspaces_known_proxies_and_a_sane_shape(h):
    h.service.create_workspace("acme")
    with pytest.raises(NotFound, match="no workspace 'beta'; create it first"):
        h.service.plan("social-6", ["acme", "beta"])
    with pytest.raises(InvalidRequest, match="name at least one workspace"):
        h.service.plan("social-6", [])
    with pytest.raises(InvalidRequest, match="workspace listed more than once: acme"):
        h.service.plan("social-6", ["acme", "acme"])
    with pytest.raises(InvalidRequest, match="proxy 'lt9' is not configured"):
        h.service.plan("social-6", ["acme"], proxy_per_workspace={"acme": "lt9"})
    with pytest.raises(InvalidRequest, match="proxy_per_workspace names 'beta'"):
        h.service.plan("social-6", ["acme"], proxy_per_workspace={"beta": "lt1"})
    with pytest.raises(InvalidRequest, match="proxy_per_lane names 'acme/mastodon'"):
        h.service.plan("social-6", ["acme"], proxy_per_lane={"acme/mastodon": "lt1"})
    with pytest.raises(InvalidRequest, match="proxy_per_workspace must map names to proxy labels"):
        h.service.plan("social-6", ["acme"], proxy_per_workspace=["lt1"])


def test_an_empty_proxy_map_still_plans_and_warns_about_the_node_ip(h):
    empty = Harness(h.home / "empty", proxies=())
    try:
        empty.service.create_workspace("acme")
        plan = empty.service.plan("social-6", ["acme"])
        assert plan.count("create") == 6 and all(step.proxy is None for step in plan.steps)
        assert all("exits on the node's own IP" in step.warnings[0] for step in plan.steps)
        with pytest.raises(InvalidRequest, match="no proxies are set up on this node yet"):
            empty.service.plan("social-6", ["acme"], proxy_per_workspace={"acme": "lt1"})
    finally:
        empty.close()


def test_override_needs_a_reason(h):
    h.service.create_workspace("acme")
    with pytest.raises(InvalidRequest, match="overriding the proxy rules needs a reason"):
        h.service.plan("social-6", ["acme"], override_proxy_rules=True, reason="  ")


def test_proxy_assignment_is_per_lane_then_per_workspace_then_default(h):
    h.service.create_workspace("acme", default_proxy="lt1")
    plan = h.service.plan("social-6", ["acme"], proxy_per_lane={"acme/linkedin": "lt2"})
    proxies = {step.lane: step.proxy for step in plan.steps}
    assert proxies["acme/instagram"] == "lt1" and proxies["acme/linkedin"] == "lt2"
    # Per-workspace beats the stored default.
    by_ws = h.service.plan("social-6", ["acme"], proxy_per_workspace={"acme": "lt2"})
    assert {step.proxy for step in by_ws.steps} == {"lt2"}


def test_a_plan_over_the_lane_limit_is_refused(h):
    surfaces = "\n".join(f'[[surfaces]]\nname = "s{i}"\nplatform = "p{i}"\n' for i in range(50))
    write_template(h, "big", surfaces)
    names = [f"ws{i}" for i in range(5)]
    for name in names:
        h.service.create_workspace(name)
    assert len(names) * 50 > PLAN_MAX_LANES
    with pytest.raises(InvalidRequest, match="at most 200 lanes; this one would create 250"):
        h.service.plan("big", names)


# ---- apply -------------------------------------------------------------------------

async def test_apply_creates_the_lanes_in_order_with_their_proxy_and_login_urls(h):
    h.service.create_workspace("acme")
    result = await h.service.apply("social-6", ["acme"], by="owner", proxy_per_workspace={"acme": "lt1"})
    assert result.status == "complete" and result.failed is None and result.remaining == []
    assert result.created == [f"acme/{name}" for name in SOCIAL_LANES]
    info = h.lanes.lanes["acme/instagram"]
    assert (info.spec.proxy_label, info.spec.engine, info.spec.workspace) == ("lt1", "camoufox", "acme")
    queue = h.service.queue("acme")
    assert queue.total == 6 and queue.ready == 0 and queue.needs_login == 6
    by_name = {lane.name: lane for lane in queue.lanes}
    assert by_name["instagram"].login_url == "https://www.instagram.com/accounts/login/"
    assert by_name["instagram"].surface == "instagram" and by_name["instagram"].platform == "instagram"
    assert by_name["blog"].login_url is None  # the company's blog address is not known yet


async def test_apply_twice_is_idempotent_and_keeps_readiness(h):
    h.service.create_workspace("acme")
    await h.service.apply("social-6", ["acme"], by="owner", proxy_per_workspace={"acme": "lt1"})
    h.service.set_readiness("acme/instagram", "ready", by="owner")
    again = await h.service.apply("social-6", ["acme"], by="owner", proxy_per_workspace={"acme": "lt1"})
    assert again.status == "complete" and again.created == [] and len(again.existing) == 6
    assert h.lanes.created == [f"acme/{name}" for name in SOCIAL_LANES]  # no second create
    assert h.service.get_workspace("acme").ready == 1


async def test_apply_refuses_on_a_conflict_and_creates_nothing(h):
    h.service.create_workspace("acme")
    h.service.create_workspace("beta")
    await h.service.apply("social-6", ["acme"], by="owner", proxy_per_workspace={"acme": "lt1"})
    before = list(h.lanes.created)
    with pytest.raises(WorkspaceConflict, match=r"^6 conflicts, nothing was created: beta/instagram: proxy lt1 is already "
                                       r"used by acme/blog in workspace acme: .* \(and 6 more\) \| beta/facebook"):
        await h.service.apply("social-6", ["beta"], by="owner", proxy_per_workspace={"beta": "lt1"})
    assert h.lanes.created == before and h.service.get_workspace("beta").total == 0


async def test_an_override_creates_the_lane_and_is_recorded_with_its_reason(h):
    h.service.create_workspace("acme")
    h.service.create_workspace("beta")
    await h.service.apply("social-6", ["acme"], by="owner", proxy_per_workspace={"acme": "lt1"})
    result = await h.service.apply("social-6", ["beta"], by="owner", proxy_per_workspace={"beta": "lt1"},
                                   override_proxy_rules=True, reason="same owner, two brands")
    assert result.status == "complete" and "beta/instagram" in result.created
    assert {item["lane"] for item in result.overridden} == {f"beta/{name}" for name in SOCIAL_LANES}
    recorded = h.service.overrides()
    assert recorded and recorded[0].reason == "same owner, two brands" and recorded[0].by == "owner"
    assert recorded[0].lane == "beta/linkedin" and recorded[0].proxy == "lt1"  # newest first
    assert any("proxy lt1 is already used by acme/instagram" in rule for rule in recorded[-1].rules)


async def test_an_existing_lane_on_another_proxy_is_a_conflict_and_is_never_moved(h):
    h.lanes.add("acme/instagram", proxy="lt2")
    h.service.create_workspace("acme")
    with pytest.raises(WorkspaceConflict, match="already exists on proxy lt2, the plan wants proxy lt1"):
        await h.service.apply("social-6", ["acme"], by="owner", proxy_per_workspace={"acme": "lt1"})
    assert h.lanes.lanes["acme/instagram"].spec.proxy_label == "lt2"
    assert h.lanes.created == []


async def test_a_matching_lane_made_by_hand_is_adopted_with_its_login_url(h):
    h.lanes.add("acme/instagram", proxy="lt1")
    h.service.create_workspace("acme")
    result = await h.service.apply("social-6", ["acme"], by="owner", proxy_per_workspace={"acme": "lt1"})
    assert "acme/instagram" in result.existing and "acme/instagram" not in result.created
    queue = h.service.queue("acme")
    assert next(lane for lane in queue.lanes if lane.name == "instagram").login_url == \
        "https://www.instagram.com/accounts/login/"


async def test_a_lane_made_by_hand_with_another_platform_name_is_a_conflict(h):
    h.service.store.record_lane({"id": "acme/main", "workspace": "acme", "surface": None, "platform": "facebook",
                                 "template": None, "home_url": None, "login_url": None,
                                 "created_at": "2026-11-04T00:00:00+00:00"})
    h.lanes.add("acme/main", proxy="lt1")
    h.service.create_workspace("acme")
    write_template(h, "one", '[[surfaces]]\nname = "main"\nplatform = "instagram"\n')
    with pytest.raises(WorkspaceConflict, match="already exists with platform facebook, the template says instagram"):
        await h.service.apply("one", ["acme"], by="owner", proxy_per_workspace={"acme": "lt1"})


async def test_a_lane_created_between_plan_and_apply_on_the_same_proxy_stops_the_apply(h):
    h.service.create_workspace("acme")
    h.service.create_workspace("beta")
    plan = h.service.plan("social-6", ["acme"], proxy_per_workspace={"acme": "lt1"})
    h.lanes.add("beta/instagram", proxy="lt1")  # appears after the plan was made
    result = await h.service.execute(plan, by="owner")
    assert result.status == "partial" and result.created == []
    assert result.failed["lane"] == "acme/instagram" and result.failed["code"] == "conflict"
    assert "proxy lt1 is already used by beta/instagram" in result.failed["message"]
    assert result.remaining == [f"acme/{name}" for name in SOCIAL_LANES]
    assert h.lanes.created == []


async def test_a_lane_created_between_plan_and_apply_with_the_same_details_is_accepted(h):
    h.service.create_workspace("acme")
    plan = h.service.plan("social-6", ["acme"], proxy_per_workspace={"acme": "lt1"})
    h.lanes.add("acme/instagram", proxy="lt1")
    result = await h.service.execute(plan, by="owner")
    assert result.status == "complete" and "acme/instagram" in result.existing
    assert result.created == [f"acme/{name}" for name in SOCIAL_LANES[1:]]


async def test_a_lane_that_appears_during_the_create_call_is_judged_by_its_details(h):
    h.service.create_workspace("acme")

    def race(spec):
        if spec.id == "acme/facebook":
            h.lanes.add("acme/facebook", proxy="lt2")  # someone else put it on a different proxy

    h.lanes.before_create = race
    plan = h.service.plan("social-6", ["acme"], proxy_per_workspace={"acme": "lt1"})
    result = await h.service.execute(plan, by="owner")
    assert result.status == "partial" and result.created == ["acme/instagram"]
    assert result.failed["lane"] == "acme/facebook" and result.failed["code"] == "conflict"
    assert "already exists on proxy lt2, the plan wants proxy lt1" in result.failed["message"]
    assert h.lanes.lanes["acme/facebook"].spec.proxy_label == "lt2"  # untouched


async def test_a_failed_create_stops_and_a_second_run_finishes_the_rest(h):
    h.service.create_workspace("acme")
    h.lanes.fail_on = {"acme/reddit"}
    result = await h.service.apply("social-6", ["acme"], by="owner", proxy_per_workspace={"acme": "lt1"})
    assert result.status == "partial" and result.created == ["acme/instagram", "acme/facebook"]
    assert result.failed == {"lane": "acme/reddit", "code": "engine_error",
                             "message": "the browser for acme/reddit did not start"}
    assert result.remaining == ["acme/reddit", "acme/medium", "acme/blog", "acme/linkedin"]
    h.lanes.fail_on = set()
    second = await h.service.apply("social-6", ["acme"], by="owner", proxy_per_workspace={"acme": "lt1"})
    assert second.status == "complete" and second.failed is None
    assert second.created == ["acme/reddit", "acme/medium", "acme/blog", "acme/linkedin"]
    assert len(second.existing) == 2


async def test_a_lane_the_node_refuses_is_reported_with_its_own_code(h):
    h.service.create_workspace("acme")
    plan = h.service.plan("social-6", ["acme"], proxy_per_workspace={"acme": "lt1"})
    h.lanes.proxies = set()  # the proxy disappears between plan and apply
    result = await h.service.execute(plan, by="owner")
    assert result.status == "partial" and result.failed["code"] == "invalid"
    assert "unknown proxy 'lt1'" in result.failed["message"]


async def test_lanes_without_a_proxy_are_created_with_their_warnings_reported(h):
    empty = Harness(h.home / "empty", proxies=())
    try:
        empty.service.create_workspace("acme")
        result = await empty.service.apply("social-6", ["acme"], by="owner")
        assert result.status == "complete" and len(result.created) == 6
        assert len(result.warnings) == 6 and "node's own IP" in result.warnings[0]
    finally:
        empty.close()


async def test_a_user_template_with_a_duplicate_platform_needs_per_lane_proxies(h):
    write_template(h, "twins", '[[surfaces]]\nname = "reddit"\nplatform = "reddit"\n'
                               '[[surfaces]]\nname = "reddit-alt"\nplatform = "reddit"\n')
    h.service.create_workspace("acme")
    plan = h.service.plan("twins", ["acme"], proxy_per_workspace={"acme": "lt1"})
    assert [step.action for step in plan.steps] == ["create", "conflict"]
    fixed = h.service.plan("twins", ["acme"], proxy_per_lane={"acme/reddit-alt": "lt2"},
                           proxy_per_workspace={"acme": "lt1"})
    assert [step.action for step in fixed.steps] == ["create", "create"]


async def test_apply_with_a_bad_template_file_says_why(h):
    write_template(h, "broken", "[[surfaces]]\nname = \"Store\"\nplatform = \"x\"\n")
    h.service.create_workspace("acme")
    with pytest.raises(InvalidRequest, match="template 'broken' cannot be used: surfaces #1: name must be"):
        await h.service.apply("broken", ["acme"], by="owner")


async def test_the_apply_lock_serialises_concurrent_applies_on_one_service(h):
    import asyncio

    h.service.create_workspace("acme")
    h.service.create_workspace("beta")
    a = h.service.apply("social-6", ["acme"], by="owner", proxy_per_workspace={"acme": "lt1"})
    b = h.service.apply("social-6", ["beta"], by="owner", proxy_per_workspace={"beta": "lt2"})
    results = await asyncio.gather(a, b)
    assert [r.status for r in results] == ["complete", "complete"]
    assert len(h.lanes.created) == 12


def test_a_damaged_database_makes_the_service_unavailable_not_a_traceback(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "workspaces.db").write_bytes(b"garbage" * 300)
    from agentos.core.runtime import Runtime

    with pytest.raises(ServiceUnavailable, match="Lanes are not affected"):
        build(Runtime(), home=home, labels=lambda: set())


async def test_an_unexpected_error_from_the_lane_manager_stops_and_is_reported_as_error(h, monkeypatch):
    h.service.create_workspace("acme")
    plan = h.service.plan("social-6", ["acme"], proxy_per_workspace={"acme": "lt1"})

    async def broken(spec):
        raise RuntimeError("disk full at /var/lib/agentos/lanes")

    monkeypatch.setattr(h.lanes, "create", broken)
    result = await h.service.execute(plan, by="owner")
    assert result.status == "partial" and result.created == []
    assert result.failed == {"lane": "acme/instagram", "code": "error",
                             "message": "unexpected error creating the lane; see the node log"}
    assert "disk full" not in result.failed["message"]  # internals stay in the node log


async def test_a_lane_created_but_not_recorded_is_still_listed_as_created(h, monkeypatch):
    h.service.create_workspace("acme")
    plan = h.service.plan("social-6", ["acme"], proxy_per_workspace={"acme": "lt1"})

    def no_db(row):
        raise ServiceUnavailable("workspaces.db could not be read or written (disk I/O error).")

    monkeypatch.setattr(h.service.store, "record_lane", no_db)
    result = await h.service.execute(plan, by="owner")
    assert result.status == "partial" and result.created == ["acme/instagram"]
    assert result.failed["lane"] == "acme/instagram" and result.failed["code"] == "unavailable"
    assert "run apply again to finish" in result.failed["message"]
    assert h.lanes.created == ["acme/instagram"]  # the lane is real; a second run adopts it


def test_delete_is_refused_while_the_workspace_has_memory(h):
    from agentos.memory import Caller
    from agentos.memory.fakes import fake_memory

    h.service.create_workspace("acme")
    m = fake_memory(h.home)
    m.service.write(Caller.owner(), "acme", {"type": "fact", "title": "a fact"})
    m.service.close()
    with pytest.raises(WorkspaceConflict, match="has memory. Export it first"):
        h.service.delete_workspace("acme")
    assert (h.home / "memory" / "acme.db").exists()
    h.service.delete_workspace("acme", with_memory=True)
    assert not (h.home / "memory" / "acme.db").exists()
    with pytest.raises(NotFound):
        h.service.get_workspace("acme")


def test_delete_with_memory_through_the_memory_service(h):
    from agentos.memory import Caller
    from agentos.memory.fakes import fake_memory

    h.service.create_workspace("acme")
    m = fake_memory(h.home)
    h.runtime.register("memory", m.service)
    m.service.write(Caller.owner(), "acme", {"type": "fact", "title": "a fact"})
    with pytest.raises(WorkspaceConflict):
        h.service.delete_workspace("acme")
    h.service.delete_workspace("acme", with_memory=True)
    assert not m.service.has_memory("acme")
    m.service.close()
