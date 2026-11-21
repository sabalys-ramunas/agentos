"""The proxy reputation rules and the planner, with no store, no lane manager and no disk.

The rules are the part that protects accounts from linking by IP, so each boundary is pinned here:
same platform or not, same workspace or not, planned or already existing, with and without override.
"""

from __future__ import annotations

from openajar.workspaces.models import Template, TemplateLane
from openajar.workspaces.planner import approval_summary, make_plan, refusal
from openajar.workspaces.rules import LaneFact, proxy_conflicts


def fact(lane: str, proxy: str | None, platform: str | None = None) -> LaneFact:
    workspace, name = lane.split("/")
    return LaneFact(lane=lane, workspace=workspace, platform=platform or name, proxy=proxy)


def template(*surfaces: tuple[str, str]) -> Template:
    return Template(id="t", title="t", surfaces=[TemplateLane(name=n, platform=p) for n, p in surfaces])


SOCIAL = template(("instagram", "instagram"), ("facebook", "facebook"), ("reddit", "reddit"))


# ---- the rules -------------------------------------------------------------

def test_no_proxy_is_never_refused_by_the_rules():
    assert proxy_conflicts(fact("a/instagram", None), [fact("b/instagram", None)]) == []


def test_two_workspaces_on_one_proxy_are_refused_as_linked_businesses():
    reasons = proxy_conflicts(fact("b/facebook", "lt1"), [fact("a/instagram", "lt1")])
    assert reasons == ["proxy lt1 is already used by a/instagram in workspace a: two businesses must not share an IP"]


def test_same_platform_on_one_proxy_is_refused_even_inside_one_workspace():
    reasons = proxy_conflicts(fact("a/instagram-2", "lt1", "instagram"), [fact("a/instagram", "lt1")])
    assert reasons == ["a/instagram already uses instagram on proxy lt1: one account per platform per IP"]


def test_same_platform_across_workspaces_reports_both_rules():
    reasons = proxy_conflicts(fact("b/instagram", "lt1"), [fact("a/instagram", "lt1")])
    assert len(reasons) == 2
    assert "two businesses" in reasons[0] and "one account per platform" in reasons[1]


def test_one_workspace_may_use_different_platforms_on_one_proxy():
    # One business can have an Instagram and a Facebook behind one static IP.
    assert proxy_conflicts(fact("a/facebook", "lt1"), [fact("a/instagram", "lt1")]) == []


def test_different_proxies_never_conflict():
    assert proxy_conflicts(fact("b/instagram", "lt2"), [fact("a/instagram", "lt1")]) == []


def test_a_lane_is_not_compared_with_itself():
    assert proxy_conflicts(fact("a/instagram", "lt1"), [fact("a/instagram", "lt1")]) == []


def test_a_platform_recorded_for_a_lane_overrides_its_name():
    # A lane named "main" that is really an Instagram account still counts as Instagram.
    others = [LaneFact(lane="a/main", workspace="a", platform="instagram", proxy="lt1")]
    assert proxy_conflicts(fact("a/instagram", "lt1"), others) == [
        "a/main already uses instagram on proxy lt1: one account per platform per IP"]


def test_reasons_are_in_a_stable_order_by_lane():
    others = [fact("z/reddit", "lt1"), fact("m/reddit", "lt1")]
    reasons = proxy_conflicts(fact("a/reddit", "lt1"), others)
    assert len(reasons) == 4
    assert reasons[0].startswith("proxy lt1 is already used by m/reddit")
    assert reasons[1].startswith("m/reddit already uses reddit")
    assert reasons[2].startswith("proxy lt1 is already used by z/reddit")
    assert reasons[3].startswith("z/reddit already uses reddit")


# ---- the planner -----------------------------------------------------------

def plan(tpl, workspaces, proxies, existing=None, override=False, reason=None):
    return make_plan(tpl, workspaces, proxies=proxies, existing=existing or {}, override=override, reason=reason)


def test_a_clean_plan_creates_every_surface_in_order():
    p = plan(SOCIAL, ["acme"], {"acme/instagram": "lt1", "acme/facebook": "lt1", "acme/reddit": "lt1"})
    assert [(s.lane, s.action) for s in p.steps] == [
        ("acme/instagram", "create"), ("acme/facebook", "create"), ("acme/reddit", "create")]
    assert p.public()["ok"] is True and p.public()["counts"] == {"create": 3, "exists": 0, "conflict": 0}


def test_a_lane_without_a_proxy_is_created_with_a_warning_not_refused():
    p = plan(SOCIAL, ["acme"], {"acme/instagram": None, "acme/facebook": None, "acme/reddit": None})
    assert all(s.action == "create" for s in p.steps)
    assert all("exits on the node's own IP" in s.warnings[0] for s in p.steps)


def test_a_new_workspace_cannot_reuse_another_workspaces_proxy():
    existing = {"acme/instagram": fact("acme/instagram", "lt1")}
    p = plan(template(("instagram", "instagram")), ["beta"], {"beta/instagram": "lt1"}, existing)
    assert p.steps[0].action == "conflict"
    assert p.steps[0].reasons[0].startswith("proxy lt1 is already used by acme/instagram in workspace acme")


def test_a_planned_duplicate_platform_on_one_proxy_is_refused_within_the_plan():
    tpl = template(("reddit", "reddit"), ("reddit-alt", "reddit"))
    p = plan(tpl, ["acme"], {"acme/reddit": "lt1", "acme/reddit-alt": "lt1"})
    assert [s.action for s in p.steps] == ["create", "conflict"]
    assert p.steps[1].reasons == ["acme/reddit already uses reddit on proxy lt1: one account per platform per IP"]


def test_planned_lanes_on_different_proxies_do_not_conflict():
    tpl = template(("reddit", "reddit"), ("reddit-alt", "reddit"))
    p = plan(tpl, ["acme"], {"acme/reddit": "lt1", "acme/reddit-alt": "lt2"})
    assert all(s.action == "create" for s in p.steps)


def test_override_creates_the_refused_lane_and_names_the_rules_it_bypasses():
    existing = {"acme/instagram": fact("acme/instagram", "lt1")}
    p = plan(template(("instagram", "instagram")), ["beta"], {"beta/instagram": "lt1"}, existing,
             override=True, reason="same owner, two brands")
    step = p.steps[0]
    assert step.action == "create" and len(step.overridden) == 2
    assert p.override_proxy_rules and p.reason == "same owner, two brands"
    assert "OVERRIDES the proxy rules for 1 lane" in approval_summary(p)


def test_override_only_bypasses_the_rules_and_never_moves_an_existing_lane():
    existing = {"acme/instagram": fact("acme/instagram", "lt2")}
    p = plan(template(("instagram", "instagram")), ["acme"], {"acme/instagram": "lt1"}, existing, override=True,
             reason="try")
    assert p.steps[0].action == "conflict"
    assert p.steps[0].reasons == ["already exists on proxy lt2, the plan wants proxy lt1: existing lanes are never moved"]


def test_a_matching_existing_lane_is_exists_and_counts_for_later_rules():
    existing = {"acme/instagram": fact("acme/instagram", "lt1")}
    p = plan(SOCIAL, ["acme"], {"acme/instagram": "lt1", "acme/facebook": "lt1", "acme/reddit": "lt1"}, existing)
    # facebook and reddit share lt1 with the existing instagram, but they are other platforms in the
    # same workspace, which the rules allow.
    assert [s.action for s in p.steps] == ["exists", "create", "create"]


def test_an_existing_lane_with_another_platform_is_a_conflict():
    existing = {"acme/instagram": LaneFact("acme/instagram", "acme", "facebook", "lt1")}
    p = plan(template(("instagram", "instagram")), ["acme"], {"acme/instagram": "lt1"}, existing)
    assert p.steps[0].action == "conflict"
    assert p.steps[0].reasons == ["already exists with platform facebook, the template says instagram"]


def test_an_existing_lane_with_no_proxy_is_a_conflict_when_the_plan_wants_one():
    existing = {"acme/instagram": fact("acme/instagram", None)}
    p = plan(template(("instagram", "instagram")), ["acme"], {"acme/instagram": "lt1"}, existing)
    assert p.steps[0].reasons[0] == "already exists on no proxy, the plan wants proxy lt1: existing lanes are never moved"


def test_refusal_and_summary_read_plainly():
    existing = {"acme/instagram": fact("acme/instagram", "lt1")}
    p = plan(template(("instagram", "instagram")), ["beta"], {"beta/instagram": "lt1"}, existing)
    assert refusal(p).startswith("1 conflict, nothing was created: beta/instagram: proxy lt1 is already used")
    ok = plan(SOCIAL, ["acme"], {"acme/instagram": "lt1", "acme/facebook": "lt1", "acme/reddit": "lt1"})
    assert approval_summary(ok) == ("Create 3 lanes from template t: acme/instagram (proxy lt1), "
                                    "acme/facebook (proxy lt1), acme/reddit (proxy lt1)")
    no_proxy = plan(template(("instagram", "instagram")), ["acme"], {"acme/instagram": None})
    assert approval_summary(no_proxy) == "Create 1 lane from template t: acme/instagram (no proxy)"


def test_approval_content_is_deterministic_and_has_no_timestamps():
    proxies = {"acme/instagram": "lt1", "acme/facebook": "lt1", "acme/reddit": "lt1"}
    first = plan(SOCIAL, ["acme"], proxies).approval_content()
    second = plan(SOCIAL, ["acme"], proxies).approval_content()
    assert first == second
    assert "created_at" not in str(first) and "home_url" not in str(first)
