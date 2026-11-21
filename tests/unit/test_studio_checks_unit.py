"""The measured checks without a browser: colour maths, theme comparison, check selection,
thresholds, merging across screens and the delta between runs."""

from __future__ import annotations

import pytest

from openajar.studio.checks import IDS, color, delta, merge, theme, unchecked
from openajar.studio.checks.registry import selected
from openajar.studio.checks.run import rule
from openajar.studio.checks.scripts import script
from openajar.studio.contracts import (
    CheckContext,
    CheckOutput,
    CoverageItem,
    Finding,
    Screen,
    SeenAt,
    Severity,
    Where,
    finding_id,
)

EM_DASH = chr(0x2014)

# --- colour ---------------------------------------------------------------------------------


def test_wcag_ratios_match_the_known_values():
    assert color.ratio((0, 0, 0), (255, 255, 255)) == pytest.approx(21.0)
    assert color.ratio((255, 255, 255), (255, 255, 255)) == pytest.approx(1.0)
    assert color.ratio((0x77, 0x77, 0x77), (255, 255, 255)) == pytest.approx(4.48, abs=0.01)
    assert color.ratio((0x76, 0x76, 0x76), (255, 255, 255)) == pytest.approx(4.54, abs=0.01)
    assert color.ratio((0xAA, 0xAA, 0xAA), (255, 255, 255)) == pytest.approx(2.32, abs=0.01)


def test_apca_is_signed_and_matches_the_reference_extremes():
    assert color.apca_lc((0, 0, 0), (255, 255, 255)) == pytest.approx(106.0, abs=0.1)
    assert color.apca_lc((255, 255, 255), (0, 0, 0)) == pytest.approx(-107.9, abs=0.1)
    assert color.apca_lc((128, 128, 128), (128, 128, 128)) == 0.0


def test_layers_flatten_from_the_farthest_up():
    # 50% black over 50% white over the white canvas
    bg = color.flatten([[0, 0, 0, 0.5], [255, 255, 255, 0.5]])
    assert bg == pytest.approx((127.5, 127.5, 127.5))
    assert color.flatten([]) == color.WHITE
    assert color.over([255, 0, 0, 0.25], (0, 0, 0)) == pytest.approx((63.75, 0, 0))


def test_the_suggested_colour_passes_and_stays_close():
    s = color.suggest((0xAA, 0xAA, 0xAA), color.WHITE, 4.5)
    rgb = tuple(int(s[i:i + 2], 16) for i in (1, 3, 5))
    assert color.ratio(rgb, color.WHITE) >= 4.5
    assert color.ratio(rgb, color.WHITE) < 5.0  # the nearest, not black
    light = color.suggest((0x55, 0x55, 0x55), (0x22, 0x22, 0x22), 4.5)
    assert color.ratio(tuple(int(light[i:i + 2], 16) for i in (1, 3, 5)), (0x22, 0x22, 0x22)) >= 4.5


# --- theme ----------------------------------------------------------------------------------

LIGHT = {"vars": {"--bg": "255,255,255,1", "--surface": "244,244,245,1", "--white": "255,255,255,1",
                  "--brand": "29,78,216,1"},
         "els": {"a": {"bg": "244,244,245,1"}, "b": {"bg": "255,255,255,1"}, "c": {"bg": "29,78,216,1"},
                 "d": {"color": "244,244,245,1", "inherits": True}, "e": {"bg": "244,244,245,1"}}}
DARK = {"vars": {"--bg": "17,17,17,1", "--surface": "34,34,34,1", "--white": "255,255,255,1",
                 "--brand": "29,78,216,1"},
        "els": {"a": {"bg": "244,244,245,1"}, "b": {"bg": "255,255,255,1"}, "c": {"bg": "29,78,216,1"},
                "d": {"color": "244,244,245,1", "inherits": True}, "e": {"bg": "34,34,34,1"}}}


def test_a_colour_left_at_a_changed_variable_light_value_is_a_leak():
    leaks, why = theme.compare(LIGHT, DARK)
    assert why is None
    assert [(lk.path, lk.prop, lk.vars) for lk in leaks] == [("a", "bg", ("--surface",))]
    assert theme.rgba_hex(leaks[0].dark_var_value) == "#222222"


def test_near_misses_are_not_leaks():
    # b: white is also an unchanged variable (--white); c: the brand colour does not change;
    # d: inherited text colour; e: followed the theme.
    leaks, _ = theme.compare(LIGHT, DARK)
    assert {lk.path for lk in leaks} == {"a"}


def test_no_changed_variable_means_nothing_to_compare():
    leaks, why = theme.compare(LIGHT, LIGHT)
    assert leaks == [] and "no colour custom property changed" in why


# --- selection and thresholds -----------------------------------------------------------------


def test_only_takes_ids_and_prefixes():
    assert selected("contrast.text", None)
    assert selected("contrast.text", ["contrast"]) and selected("contrast.text", ["contrast."])
    assert selected("layout.clipped", ["layout"]) and not selected("layout.clipped", ["lay"])
    assert not selected("health.fonts", ["contrast.text"])


def test_rules_read_flat_or_nested_keys():
    ctx = CheckContext(screen=Screen(width=360), rules={"contrast.text": {"min_ratio": 7},
                                                        "target.size.min_px": 44})
    assert rule(ctx, "contrast.text", "min_ratio", 4.5) == 7
    assert rule(ctx, "target.size", "min_px", 24) == 44
    assert rule(ctx, "health.long_tasks", "max_blocking_ms", 300) == 300


def test_the_page_scripts_carry_the_shared_helpers_and_no_em_dash():
    for name in ("page", "perf", "theme"):
        body = script(name)
        assert "/*COMMON*/" not in body and "const groupKey" in body
        assert EM_DASH not in body
    assert EM_DASH not in script("pixels")


# --- merge and delta --------------------------------------------------------------------------


def _f(check, anchor, screen, *, sev=Severity.MAJOR, count=1, **measured) -> Finding:
    return Finding(id=finding_id(check, "Card", anchor), check=check, severity=sev, where=Where(selector=anchor),
                   seen_at=[SeenAt(screen=screen, count=count)], measured=measured)


def _out(screen, *findings, skipped=()) -> CheckOutput:
    cov = [CoverageItem(check=c, screen=screen, status="skipped" if c in skipped else "ran") for c in IDS]
    return CheckOutput(findings=list(findings), coverage=cov)


def test_merge_folds_screens_and_keeps_the_worst_instance():
    a = _out("default@360-light", _f("contrast.text", "p", "default@360-light", count=3, ratio=3.1))
    b = _out("default@360-dark", _f("contrast.text", "p", "default@360-dark", count=2, ratio=1.9))
    run = merge({"x": a, "y": b}, used={"widths": [360]})
    [f] = run.findings
    assert f.measured["ratio"] == 1.9
    assert {(s.screen, s.count) for s in f.seen_at} == {("default@360-light", 3), ("default@360-dark", 2)}
    assert run.used == {"widths": [360]} and len(run.coverage) == 2 * len(IDS)
    assert merge({"x": a, "y": b}).id == run.id  # the same input gives the same run id


def test_merge_orders_by_severity_then_reach():
    s = "default@360-light"
    run = merge([_out(s, _f("health.images", "img", s, sev=Severity.MINOR), _f("name.missing", "button", s),
                      _f("layout.clipped", "a", s, sev=Severity.BLOCKER))])
    assert [f.check for f in run.findings] == ["layout.clipped", "name.missing", "health.images"]


def test_delta_sorts_fixed_new_still_and_worse():
    s = "default@360-light"
    before = merge([_out(s, _f("contrast.text", "p", s, ratio=3.0), _f("target.size", "b", s, spacing_short_px=4),
                         _f("text.truncated", "h", s, hidden_share=0.3))])
    after = merge([_out(s, _f("target.size", "b", s, spacing_short_px=4), _f("text.truncated", "h", s, hidden_share=0.6),
                        _f("name.missing", "i", s))])
    d = delta(before, after)
    ids = {f.check: f.id for f in before.findings + after.findings}
    assert d.fixed == [ids["contrast.text"]] and d.new == [ids["name.missing"]]
    assert d.still == [ids["target.size"]] and d.worse == [ids["text.truncated"]]


def test_a_finding_whose_check_did_not_run_again_is_not_fixed():
    s = "default@360-light"
    before = merge([_out(s, _f("contrast.text", "p", s, ratio=3.0))])
    after = merge([_out(s, skipped=("contrast.text",))])
    assert delta(before, after).fixed == []
    assert unchecked(before, after) == [before.findings[0].id]


def test_more_screens_or_a_higher_severity_is_worse():
    s1, s2 = "default@360-light", "default@768-light"
    before = merge([_out(s1, _f("target.size", "b", s1))])
    after = merge([_out(s1, _f("target.size", "b", s1)), _out(s2, _f("target.size", "b", s2))])
    assert delta(before, after).worse == [before.findings[0].id]
    up = merge([_out(s1, _f("target.size", "b", s1, sev=Severity.BLOCKER))])
    assert delta(before, up).worse == [before.findings[0].id]
