"""Profile edits (brief B2): the program's `profile:` section, the due edits in a fixed order, the
profile_edit cap (the owner's 0 stops edits at once), the planner placing them in the day's first
session, and the executor holding every one until its rule and operation are verified, and asking
the owner for each."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import date, timedelta

import pytest
from test_engage_executor import OWNER, Rig

from agentos.engage import profile
from agentos.engage.caps import CAPS, Kind, Risk
from agentos.engage.errors import ProgramInvalid
from agentos.engage.governor import budget, ceiling_table, start_cap
from agentos.engage.models import Program
from agentos.engage.operations import DEFAULT_SPECS, PROFILE_SPECS, BadTarget, OpSpec, Registry
from agentos.engage.ports import OpResult, OpStatus
from agentos.engage.service import parse_program
from agentos.node.egress import EFFECT_RULES, EffectRule

LI = "biz-a/linkedin"
MONDAY = date(2026, 11, 2)
LI_YAML = """
program: biz-a-li
surface: biz-a/linkedin
timezone: Europe/Vilnius
phases:
  - kind: steady
profile:
  cover: in_8b1d0e44
  about: |
    Notes on the analytical engine.
    And what it could compute.
  headline: Analyst of engines
  name: {first: Ada, last: Lovelace}
  photo: in_3f9a2c71
"""
FIELDS = ["name", "headline", "about", "photo", "cover"]


def program(**profile_fields) -> Program:
    data = {"program": "biz-a-li", "surface": LI, "phases": [{"kind": "steady"}]}
    if profile_fields:
        data["profile"] = profile_fields
    return Program.model_validate(data)


def verified_registry() -> Registry:
    """The five profile operations as they would be after their canaries: rules and marks."""
    rules = [EffectRule(f"linkedin.profile.{f}", "linkedin", "www.linkedin.com", "/x", verified="test") for f in FIELDS]
    return Registry(PROFILE_SPECS, installed=lambda flow_id: True, verified=lambda flow_id: "test", rules=rules)


def first_profile_window(rig: Rig):
    for d in range(14):
        day = MONDAY + timedelta(days=d)
        for w in rig.ex.windows(LI, day):
            if profile.PROFILE_EDIT in w.kinds:
                return day, w
    raise AssertionError("no window plans a profile edit")


# ---------------------------------------------------------------- the program


def test_the_profile_section_is_optional_and_validated():
    p = parse_program(LI_YAML)
    assert p.profile is not None and p.profile.fields() == FIELDS
    assert p.profile.about == "Notes on the analytical engine.\nAnd what it could compute."
    assert program().profile is None and "profile" not in program().canonical()   # old hashes stay
    bad = [
        ({"headline": "x" * 221}, "at most 220"),
        ({"about": "x" * 2601}, "at most 2600"),
        ({"name": {"first": "A" * 51, "last": "B"}}, "at most 50"),
        ({"name": {"first": " ", "last": "B"}}, "empty"),
        ({"headline": "Analyst\u200b of engines"}, "invisible"),
        ({"headline": "Analyst\u202eenigne"}, "invisible"),
        ({"headline": "two\nlines"}, "invisible"),
        ({"about": "bell\x07"}, "invisible"),
        ({"photo": "/etc/passwd"}, "file id"),
        ({"cover": "https://evil.example/x.png"}, "file id"),
        ({"name": {"first": "Ada"}}, "last"),
        ({"bio": "x"}, "bio"),
    ]
    for fields, why in bad:
        with pytest.raises(ProgramInvalid, match=why):
            parse_program({"program": "biz-a-li", "surface": LI, "phases": [{"kind": "steady"}], "profile": fields})
    with pytest.raises(ProgramInvalid, match="no profile operations"):
        parse_program({"program": "biz-a-ig", "surface": "biz-a/instagram", "phases": [{"kind": "steady"}],
                       "profile": {"headline": "x"}})
    with pytest.raises(ProgramInvalid, match="not a daily target"):
        parse_program({"program": "biz-a-li", "surface": LI, "phases": [{"kind": "steady",
                                                                         "daily": {"profile_edit": 1}}]})


def test_a_draft_that_changes_the_profile_says_so_to_the_owner(tmp_path):
    rig = Rig(tmp_path, LI_YAML, surface=LI, registry=verified_registry())
    changed = LI_YAML.replace("Analyst of engines", "First programmer").replace("  photo: in_3f9a2c71\n", "")
    out = rig.svc.draft(LI, changed, by={"kind": "flow", "id": "drafter", "owner": "owner"})   # agents may draft it
    [note] = [w for w in out["warnings"] if "public profile" in w]
    assert "headline" in note and "photo (no longer set" in note and "approval card" in note
    assert "name" not in note.split(":", 1)[1].split(".")[0]
    assert "approve every profile edit" in out["summary"]
    assert out["state"] == "draft"                                    # approval stays the owner's


# ---------------------------------------------------------------- due edits


def test_due_edits_come_in_a_fixed_order_and_only_where_the_value_differs():
    p = parse_program(LI_YAML)
    assert profile.due(p, {}) == FIELDS
    assert [e.field for e in profile.due_edits(p, {}, 2)] == ["name", "headline"]
    assert profile.due_edits(p, {}, 0) == []
    known = {"name": profile.encode(p, "name"), "about": "Notes on the  analytical engine.\t\nAnd what it could compute. "}
    assert profile.due(p, known) == ["headline", "photo", "cover"]        # spaces normalized, line breaks kept
    one_line = {"about": "Notes on the analytical engine. And what it could compute."}
    assert "about" in profile.due(p, one_line)                            # the same words on one line differ
    assert profile.due(p, {}, blocked={("headline", "Analyst of engines")}) == ["name", "about", "photo", "cover"]
    edit = profile.due_edits(p, {}, 1)[0]
    assert edit.kind == "profile_name" and json.loads(edit.target) == {"first": "Ada", "last": "Lovelace"}
    assert profile.due(program(headline="x"), {"headline": "x"}) == [] and profile.due(None, {}) == []


def test_the_params_come_from_the_programs_value_never_from_content():
    specs = {s.kind: s for s in PROFILE_SPECS}
    p = parse_program(LI_YAML)
    built = {f: specs[f"profile_{f}"].build(None, profile.encode(p, f)) for f in FIELDS}
    assert built == {"name": {"first_name": "Ada", "last_name": "Lovelace"}, "headline": {"headline": "Analyst of engines"},
                     "about": {"about": p.profile.about}, "photo": {"image": "in_3f9a2c71"},
                     "cover": {"image": "in_8b1d0e44"}}
    for kind, target in (("profile_name", "Ada"), ("profile_photo", "../x"), ("profile_headline", "")):
        with pytest.raises(BadTarget):
            specs[kind].build(None, target)


# ---------------------------------------------------------------- the registry


def test_the_registry_holds_every_profile_edit_until_its_rule_and_its_canary():
    rules = {r.id: r for r in EFFECT_RULES}
    assert not {s.kind for s in PROFILE_SPECS} & {str(s.kind) for s in DEFAULT_SPECS}
    reg = Registry(installed=lambda flow_id: True, verified=lambda flow_id: "2026-11-06 test canary")
    for spec in PROFILE_SPECS:
        assert reg.get("linkedin", spec.kind) is spec and spec.rule_id in rules
        assert reg.problem(spec) == "rule_unverified"                 # shipped "to capture"
    # after the capture the rule is verified; the operation still waits for its own mark
    captured = [EffectRule(s.rule_id, "linkedin", "www.linkedin.com", "/x", verified="2026-11-07 test") for s in PROFILE_SPECS]
    reg = Registry(PROFILE_SPECS, installed=lambda flow_id: True, rules=captured)
    assert all(reg.problem(s) == "op_unverified" for s in PROFILE_SPECS)
    assert all(verified_registry().valid(s) for s in PROFILE_SPECS)


def _profile_only_unverified() -> Registry:
    """The engagement operations ready (their rules and marks), the profile ones not captured yet."""
    rules = [r if r.kind == "profile" else replace(r, verified=r.verified or "test") for r in EFFECT_RULES]
    return Registry(installed=lambda flow_id: True, rules=rules,
                    verified=lambda flow_id: "" if ".profile_" in flow_id else "test")


def test_an_unverified_profile_edit_holds_profile_edits_alone_and_the_rest_goes_on(tmp_path):
    liking = LI_YAML.replace("  - kind: steady\n", "  - kind: steady\n    daily: {like: 6}\n")
    rig = Rig(tmp_path, liking, surface=LI, registry=_profile_only_unverified())
    day, w = first_profile_window(rig)
    preview = rig.ex.preview(LI, day, w.idx)
    assert not preview["would_run"]
    assert {o["flow"] for o in preview["ops"] if o["kind"] == "profile_edit"} == {"connector.linkedin.profile_name"}
    rep = rig.run(day, w.idx)
    assert rep["status"] == "done" and not any(".profile_" in c.flow_id for c in rig.ops.calls)
    assert [o["reason"] for o in rep["ops"] if o["kind"] == "profile_edit"] == ["held"]
    [hold] = rig.svc.store.holds(LI)
    assert hold["reason"] == "no_valid_rule" and hold["kind"] == "profile_edit"
    for f in FIELDS:                                   # every due field's operation is named
        assert f"profile_{f} (rule_unverified)" in hold["detail"]
    assert rig.ex.active_holds(LI) == []               # the account itself is not held
    # the day's other sessions act as before, and a later day adds no second hold
    rig.run_days(day, 2)
    sessions = [x for d in (day, day + timedelta(days=1)) for x in rig.svc.store.sessions(LI, d)]
    assert sessions and all(x["status"] == "done" for x in sessions)
    likes = [o for o in rig.svc.store.ops(LI, kind="like")]
    # the likes reach their targets (the fake's are not post URLs): nothing holds them
    assert likes and all(o["reason"] not in ("held", "no_valid_rule") for o in likes)
    assert not any(".profile_" in c.flow_id for c in rig.ops.calls)
    assert len(rig.svc.store.holds(LI)) == 1


def test_a_profile_edit_cap_of_zero_checks_nothing_and_holds_nothing(tmp_path):
    rig = Rig(tmp_path, LI_YAML, surface=LI, registry=_profile_only_unverified())
    day, w = first_profile_window(rig)
    rig.svc.set_cap(LI, "profile_edit", 0, by=OWNER)
    rep = rig.run(day, w.idx)
    assert rep["status"] == "done" and rig.svc.store.holds(LI) == []
    assert [o["reason"] for o in rep["ops"] if o["kind"] == "profile_edit"] == ["cap_daily"]


# ---------------------------------------------------------------- caps and the plan


def test_the_profile_edit_cap_is_one_cap_conservative_and_linkedin_only():
    row = CAPS["linkedin"][Kind.PROFILE_EDIT]
    assert (row.start, row.careful, row.normal, row.hourly) == (1, 2, 3, 1)
    assert start_cap("linkedin", Kind.PROFILE_EDIT, Risk.CAREFUL) == 1
    assert start_cap("linkedin", Kind.PROFILE_EDIT, Risk.NORMAL) == 1
    assert not any(Kind.PROFILE_EDIT in rows for site, rows in CAPS.items() if site != "linkedin")
    [shown] = [r for r in ceiling_table("linkedin") if r["kind"] == "profile_edit"]
    assert shown["normal_start"] == 1 and shown["normal_ceiling"] == 3 and shown["hourly"] == 1
    p = parse_program(LI_YAML)
    b = budget(p, MONDAY, MONDAY, profile_due=5)
    assert b.caps[Kind.PROFILE_EDIT] == 1 and b.profile == 1 and Kind.PROFILE_EDIT not in b.targets
    assert budget(p, MONDAY, MONDAY, profile_due=5, overrides={Kind.PROFILE_EDIT: 0}).profile == 0
    assert budget(p, MONDAY, MONDAY, profile_due=0).profile == 0
    warm = Program.model_validate({**p.canonical(), "phases": [{"kind": "warm_up", "days": 5}, {"kind": "steady"}]})
    assert budget(warm, MONDAY, MONDAY, profile_due=5).profile == 0          # edits start with the steady phase


def test_a_day_with_profile_drift_edits_in_its_first_session_and_is_otherwise_drawn_as_before(tmp_path):
    rig = Rig(tmp_path, LI_YAML, surface=LI, registry=verified_registry())
    plans = rig.svc.plan(LI, days=3, start=MONDAY)
    busy = [p for p in plans if not p.quiet and p.sessions]
    first = busy[0]
    assert first.sessions[0].actions == {Kind.PROFILE_EDIT: 1}
    assert all(Kind.PROFILE_EDIT not in s.actions for s in first.sessions[1:])
    assert plans == rig.svc.plan(LI, days=3, start=MONDAY)                     # deterministic
    # the same account without a profile section: the same sessions, minus the edits
    bare = Rig(tmp_path / "bare", LI_YAML.split("profile:")[0], surface=LI)
    for a, b in zip(plans, bare.svc.plan(LI, days=3, start=MONDAY), strict=True):
        assert [s.start for s in a.sessions] == [s.start for s in b.sessions]
        assert all(Kind.PROFILE_EDIT not in s.actions for s in b.sessions)
    # a dry run spreads the five edits over the days, one a day under the careful cap
    week = rig.svc.plan(LI, days=7, start=MONDAY)
    assert sum(p.totals().get(Kind.PROFILE_EDIT, 0) for p in week) == min(5, sum(1 for p in week if p.sessions
                                                                                  and not p.quiet))


# ---------------------------------------------------------------- running


def test_a_profile_edit_runs_the_due_fields_operation_and_always_asks(tmp_path):
    rig = Rig(tmp_path, LI_YAML, surface=LI, registry=verified_registry())
    day, w = first_profile_window(rig)
    rep = rig.run(day, w.idx)
    assert rep["status"] == "done"
    [call] = rig.ops.calls
    assert call.flow_id == "connector.linkedin.profile_name" and call.ask is True
    assert call.params == {"first_name": "Ada", "last_name": "Lovelace"}
    [op] = [o for o in rep["ops"] if o["kind"] == "profile_edit"]
    assert op["status"] == "verified" and op["rule_id"] == "linkedin.profile.name"
    # the name is now known: the next edit is the headline, and the plan of a later day says so
    assert [e.field for e in profile.edits(rig.svc.store, rig.svc.store.active(LI).definition)] == FIELDS[1:]


class AttachedProfiles:
    """agentos.profiles as the engine sees it (EngageStore.profiles): the account is attached to an
    approved profile, which wants these fields."""

    def __init__(self, wanted, *, broken: bool = False):
        self.wanted, self.broken = wanted, broken

    def for_surface(self, surface):
        if self.broken:
            raise RuntimeError("profiles store unreadable")
        return object() if surface == LI else None

    def engine_profile(self, surface, site=None):
        return self.wanted


def test_an_edit_from_an_attached_profile_the_owner_saved_does_not_ask(tmp_path):
    rig = Rig(tmp_path, LI_YAML.split("profile:")[0], surface=LI, registry=verified_registry())
    rig.svc.store.profiles = AttachedProfiles(parse_program(LI_YAML).profile)
    assert profile.from_profile(rig.svc.store, LI) and not profile.from_profile(rig.svc.store, "biz-b/linkedin")
    day, w = first_profile_window(rig)
    rep = rig.run(day, w.idx)
    assert rep["status"] == "done"
    [call] = rig.ops.calls
    assert call.flow_id == "connector.linkedin.profile_name" and call.ask is False
    assert call.params == {"first_name": "Ada", "last_name": "Lovelace"}


def test_an_inline_profile_edit_asks_even_when_a_profiles_store_is_there(tmp_path):
    rig = Rig(tmp_path, LI_YAML, surface=LI, registry=verified_registry())
    rig.svc.store.profiles = AttachedProfiles(None)
    rig.svc.store.profiles.for_surface = lambda surface: None          # this account is attached to none
    day, w = first_profile_window(rig)
    rig.run(day, w.idx)
    [call] = rig.ops.calls
    assert call.ask is True


def test_an_unreadable_profiles_store_means_the_edit_asks():
    store = type("S", (), {"profiles": AttachedProfiles(None, broken=True)})()
    assert profile.from_profile(store, LI) is False
    assert profile.from_profile(type("S", (), {"profiles": None})(), LI) is False


def test_an_edit_waiting_for_the_owner_is_not_tried_again_and_already_current_counts_as_current(tmp_path):
    ops_script = {0: OpResult(OpStatus.AWAITING_APPROVAL, run_id="r0")}
    rig = Rig(tmp_path, LI_YAML, surface=LI, registry=verified_registry())
    rig.ops.script.update(ops_script)
    day, w = first_profile_window(rig)
    rig.run(day, w.idx)
    p = rig.svc.store.active(LI).definition
    assert [e.field for e in profile.edits(rig.svc.store, p)] == FIELDS[1:]     # name is in flight
    # the owner approved, and the preflight found the name already there: current, not retried
    rig.ops.later["r0"] = OpResult(OpStatus.FAILED, error="already_current", run_id="r0")
    assert asyncio.run(rig.ex.reconcile()) == 1
    known, blocked = profile.history(rig.svc.store, LI)
    assert json.loads(known["name"]) == {"first": "Ada", "last": "Lovelace"} and not blocked
    # a denied edit is not asked again for the same value
    [row] = rig.svc.store.ops(LI, kind="profile_edit", status="failed")
    rig.svc.store.set_op(row["id"], reason="denied")
    assert profile.history(rig.svc.store, LI) == ({}, {("name", row["target"])})


def _rows(rig: Rig, statuses: list[tuple[str, str | None]], *, field: str = "headline", day: date = MONDAY,
          at=None) -> list[dict]:
    """Profile rows for `field` (the program's value), one per (status, reason), in a past session."""
    p = rig.svc.store.active(LI).definition
    n = len(rig.svc.store.sessions(LI, day))
    sess = rig.svc.store.put_session(LI, day, 10 + n, planned_start=rig.ex.now(), minutes=5)
    rows = rig.svc.store.add_ops(sess["id"], LI, day, ["profile_edit"] * len(statuses))
    for r, (status, reason) in zip(rows, statuses, strict=True):
        rig.svc.store.set_op(r["id"], status=status, reason=reason, rule_id=f"linkedin.profile.{field}",
                             target=profile.encode(p, field), at=(at or rig.ex.now()).isoformat(timespec="seconds"))
    return rig.svc.store.ops(session_id=sess["id"])


@pytest.mark.parametrize("reason", ["preflight_mismatch", "element_not_found", "readback_failed", "timeout",
                                    "signal: captcha"])
def test_any_other_failure_is_a_failure_tried_again_on_a_later_day(tmp_path, reason):
    """Only already_current marks a field current: a preflight_mismatch (the element missing or
    not alone, a failed value_equals) is a failure like any other."""
    rig = Rig(tmp_path, LI_YAML, surface=LI, registry=verified_registry())
    _rows(rig, [("failed", reason)])
    known, blocked = profile.history(rig.svc.store, LI)
    assert "headline" not in known and not blocked
    p = rig.svc.store.active(LI).definition
    assert "headline" not in [e.field for e in profile.edits(rig.svc.store, p, day=MONDAY)]     # not that day
    assert "headline" in [e.field for e in profile.edits(rig.svc.store, p, day=MONDAY + timedelta(days=1))]


def test_already_current_counts_only_for_a_text_field(tmp_path):
    rig = Rig(tmp_path, LI_YAML, surface=LI, registry=verified_registry())
    _rows(rig, [("failed", "already_current")], field="photo")
    _rows(rig, [("failed", "already_current")], field="about")
    known, _ = profile.history(rig.svc.store, LI)
    assert "photo" not in known and known["about"] == profile.encode(rig.svc.store.active(LI).definition, "about")


def test_an_edit_in_flight_blocks_its_value_for_three_days(tmp_path):
    rig = Rig(tmp_path, LI_YAML, surface=LI, registry=verified_registry())
    then = rig.ex.now()
    for status in ("attempted", "awaiting_approval", "unverified"):
        rig2 = Rig(tmp_path / status, LI_YAML, surface=LI, registry=verified_registry())
        _rows(rig2, [(status, None)], at=then)
        p = rig2.svc.store.active(LI).definition
        due = [e.field for e in profile.edits(rig2.svc.store, p, now=then + timedelta(days=2))]
        assert "headline" not in due, status
        due = [e.field for e in profile.edits(rig2.svc.store, p, now=then + profile.BLOCK_TTL)]
        assert "headline" in due, status
    assert profile.BLOCK_TTL == timedelta(days=3)


def test_three_failures_in_a_row_send_one_notice(tmp_path):
    rig = Rig(tmp_path, LI_YAML, surface=LI, registry=verified_registry())
    _rows(rig, [("failed", "timeout"), ("failed", "preflight_mismatch")])
    assert profile.notify_failures(rig.svc.store, LI) == []
    _rows(rig, [("failed", "readback_failed")], day=MONDAY + timedelta(days=1))
    assert profile.failures(rig.svc.store, LI)["headline"][1] == 3
    assert profile.notify_failures(rig.svc.store, LI) == ["headline"]
    assert profile.notify_failures(rig.svc.store, LI) == []                       # once per run of failures
    [note] = [n for n in rig.svc.store.notices(LI) if n["kind"] == "profile"]
    assert "headline edit failed 3 times" in note["text"] and "profile reset" in note["text"]
    assert "\u2014" not in note["text"]
    _rows(rig, [("failed", "denied")], day=MONDAY + timedelta(days=2))
    assert "headline" not in profile.failures(rig.svc.store, LI)                  # a denial ends the run


def test_a_failed_edit_after_approval_counts_toward_the_notice_through_reconcile(tmp_path):
    rig = Rig(tmp_path, LI_YAML, surface=LI, registry=verified_registry())
    rows = _rows(rig, [("failed", "timeout"), ("failed", "timeout"), ("awaiting_approval", None)])
    rig.svc.store.set_op(rows[-1]["id"], run_id="r9")
    rig.ops.later["r9"] = OpResult(OpStatus.FAILED, error="preflight_mismatch", run_id="r9")
    assert asyncio.run(rig.ex.reconcile()) == 1
    assert any(n["kind"] == "profile" for n in rig.svc.store.notices(LI))


def test_reconcile_settles_an_attempted_row_whose_run_finished(tmp_path):
    rig = Rig(tmp_path, LI_YAML, surface=LI, registry=verified_registry())
    [row] = _rows(rig, [("attempted", None)])
    sess = rig.svc.store.session_by_id(row["session_id"])
    key = rig.ex.op_key(LI, date.fromisoformat(row["day"]), sess["idx"], row["seq"])     # tagged with the node's secret
    assert asyncio.run(rig.ex.reconcile()) == 0                     # no run for the key: it waits (and expires)
    rig.ops.by_key[key] = OpResult(OpStatus.VERIFIED, run_id="r7")
    assert asyncio.run(rig.ex.reconcile()) == 1
    [done] = rig.svc.store.ops(LI, kind="profile_edit", status="verified")
    assert done["run_id"] == "r7"
    assert profile.history(rig.svc.store, LI)[0]["headline"] == "Analyst of engines"
    # an unverified row a later check proved
    [row] = _rows(rig, [("unverified", None)], field="about")
    rig.svc.store.set_op(row["id"], run_id="r8")
    rig.ops.later["r8"] = OpResult(OpStatus.VERIFIED, run_id="r8")
    assert asyncio.run(rig.ex.reconcile()) == 1
    assert "about" in profile.history(rig.svc.store, LI)[0]


def test_the_owners_reset_forgets_a_fields_known_and_blocked_state(tmp_path):
    rig = Rig(tmp_path, LI_YAML, surface=LI, registry=verified_registry())
    _rows(rig, [("verified", None)])
    _rows(rig, [("failed", "denied")], field="about")
    _rows(rig, [("awaiting_approval", None)], field="photo")
    p = rig.svc.store.active(LI).definition
    assert [e.field for e in profile.edits(rig.svc.store, p)] == ["name", "cover"]
    with pytest.raises(Exception, match="only the owner"):
        rig.svc.reset_profile(LI, ["about"], by={"kind": "flow", "id": "x", "owner": "owner"})
    out = rig.svc.reset_profile(LI, ["about"], by=OWNER)
    assert out == {"surface": LI, "reset": ["about"]}
    assert [e.field for e in profile.edits(rig.svc.store, p)] == ["name", "about", "cover"]
    rig.svc.reset_profile(LI, None, by=OWNER)
    assert [e.field for e in profile.edits(rig.svc.store, p)] == FIELDS
    with pytest.raises(Exception, match="no profile field"):
        rig.svc.reset_profile(LI, ["bio"], by=OWNER)


def test_the_profile_reset_command(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from agentos.engage.cli import app

    rig = Rig(tmp_path, LI_YAML, surface=LI, registry=verified_registry())
    _rows(rig, [("verified", None)])
    rig.svc.store.close()
    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path))
    out = CliRunner().invoke(app, ["profile", "reset", LI, "--field", "headline"])
    assert out.exit_code == 0, out.output
    assert "headline" in out.output and "\u2014" not in out.output
    out = CliRunner().invoke(app, ["profile", "reset", LI, "--field", "bio"])
    assert out.exit_code != 0


def test_the_owners_cap_of_zero_stops_profile_edits_at_once(tmp_path):
    rig = Rig(tmp_path, LI_YAML, surface=LI, registry=verified_registry())
    day, w = first_profile_window(rig)              # the plan is drawn and saved with a cap of 1
    assert rig.svc.day_plan(LI, day).caps[Kind.PROFILE_EDIT] == 1
    out = rig.svc.set_cap(LI, "profile_edit", 0, by=OWNER)
    assert out["cap"] == 0 and out["hard_ceiling"] == 3
    rep = rig.run(day, w.idx)
    assert rig.ops.calls == []
    assert [o["reason"] for o in rep["ops"] if o["kind"] == "profile_edit"] == ["cap_daily"]
    with pytest.raises(Exception, match="only the owner"):
        rig.svc.set_cap(LI, "profile_edit", 1, by={"kind": "flow", "id": "x", "owner": "owner"})


def test_a_profile_edit_with_nothing_due_is_skipped_as_current(tmp_path):
    rig = Rig(tmp_path, LI_YAML, surface=LI, registry=verified_registry())
    day, w = first_profile_window(rig)
    p = rig.svc.store.active(LI).definition
    # the account already shows every value (an earlier run proved each of them)
    row = rig.svc.store.put_session(LI, day - timedelta(days=1), 0, planned_start=w.start - timedelta(days=1),
                                    minutes=5)
    rows = rig.svc.store.add_ops(row["id"], LI, day - timedelta(days=1), ["profile_edit"] * len(FIELDS))
    for r, f in zip(rows, FIELDS, strict=True):
        rig.svc.store.set_op(r["id"], status="verified", rule_id=f"linkedin.profile.{f}", target=profile.encode(p, f))
    assert profile.edits(rig.svc.store, p) == []
    rep = rig.run(day, w.idx)
    assert rig.ops.calls == [] and rep["status"] == "done"
    assert [o["reason"] for o in rep["ops"] if o["kind"] == "profile_edit"] == ["profile_current"]


def test_a_registered_spec_of_another_kind_is_untouched_by_the_profile_hook():
    spec = OpSpec("linkedin", "profile_headline", "connector.linkedin.profile_headline", "linkedin.profile.headline")
    assert not profile.is_profile(spec.kind) and profile.is_profile("profile_edit")


def test_the_runner_adapter_looks_a_finished_run_up_by_its_key():
    from types import SimpleNamespace as NS

    from agentos.core.runtime import Runtime
    from agentos.engage.adapters import RunnerOperations

    runs = {"engage:k1": NS(id="r1", status="failed", error={"code": "already_current"}, output=None),
            "engage:k2": NS(id="r2", status="running", error=None, output=None)}
    seen = []

    def existing_run(key, *, flow_id, lane):
        seen.append((key, flow_id, lane))
        return runs.get(key)
    port = RunnerOperations(Runtime(runner=NS(existing_run=existing_run)))
    res = asyncio.run(port.lookup("engage:k1", "connector.linkedin.profile_name", LI))
    assert res.status is OpStatus.FAILED and res.error == "already_current" and res.run_id == "r1"
    assert seen[0] == ("engage:k1", "connector.linkedin.profile_name", LI)
    assert asyncio.run(port.lookup("engage:k2", "f", LI)) is None             # still running
    assert asyncio.run(port.lookup("engage:k3", "f", LI)) is None             # never submitted
