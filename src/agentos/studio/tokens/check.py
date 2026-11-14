"""`system_check` and `adherence`: is a rendered page on the design system?

Reads only values that originate on an element (an inherited property that differs from its
parent, a non-inherited one that is not the initial value), only authored properties, with
::before and ::after, through open shadow roots. Never widths or heights layout produced.
Each value is matched to the token set; the provenance probe then says whether a value that
matches a token really came from it.
"""

from __future__ import annotations

import contextlib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ..contracts import (
    Adherence,
    CheckContext,
    CheckOutput,
    ComponentRef,
    ComponentRule,
    Confidence,
    CoverageItem,
    Finding,
    Match,
    Rect,
    SeenAt,
    Severity,
    Theme,
    TokenCategory,
    TokenSet,
    Where,
    finding_id,
)
from . import page_js
from .color import parse_color
from .match import match, radius_shape, scale_px
from .probe import PROPS_BY_CAT, Prov, run_probe
from .units import fmt_px, parse_length, parse_number, parse_shadow, shadow_css

C = TokenCategory

CHECK_OF = {
    "color": "token.color", "space": "token.space", "radius": "token.radius",
    "font_size": "token.font", "font_family": "token.font", "font_weight": "token.font",
    "line_height": "token.font", "shadow": "token.shadow", "border_width": "token.border",
}
TOKEN_CHECKS = ["token.color", "token.space", "token.radius", "token.font", "token.shadow", "token.border"]
ALL_CHECKS = [*TOKEN_CHECKS, "token.hardcoded", "component.hand_rolled"]
CATEGORY_WORDS = {
    "color": "colour", "space": "spacing", "radius": "radius", "font_size": "font size",
    "font_family": "font family", "font_weight": "font weight", "line_height": "line height",
    "shadow": "shadow", "border_width": "border width",
}
_FAMILY = {
    "padding": "padding", "margin": "margin", "border-width": "border-width", "border-color": "border-color",
    "radius": "border-radius", "gap": "gap",
}
MAX_SELECTORS = 8


def _family(prop: str) -> str:
    if prop.startswith("padding-"):
        return "padding"
    if prop.startswith("margin-"):
        return "margin"
    if prop.startswith("border-") and prop.endswith("-radius"):
        return "border-radius"
    if prop.startswith("border-") and prop.endswith("-width"):
        return "border-width"
    if prop.startswith("border-") and prop.endswith("-color"):
        return "border-color"
    if prop.endswith("-gap"):
        return "gap"
    return prop


@dataclass
class Value:
    rec: dict
    props: list[str]
    category: str
    value: str
    ua: bool
    fs: float
    prov: Prov | None = None
    match: Match | None = None
    method: str = "value"

    @property
    def on_system(self) -> bool:
        if self.category == "radius" and radius_shape(self.value) is not None:
            return True   # a circle or a pill is a shape, whatever wrote it
        if self.match is not None and self.match.kind == "exact" and (self.match.token or "").endswith(".scale.px"):
            return True   # a 1px hairline (Tailwind's p-px) is fixed on purpose
        if self.prov is not None and self.method == "probe":
            return self.prov.kind in ("token", "derived")
        return self.match is not None and self.match.kind == "exact"


@dataclass
class Analysis:
    records: list[dict]
    values: list[Value]
    tokens: TokenSet
    theme: Theme | None
    components: list[ComponentRule]
    method: dict[str, str] = field(default_factory=dict)       # category -> probe | value: why
    notes: list[str] = field(default_factory=list)
    area: float | None = None
    truncated: bool = False
    typed: bool = True


def _categories(tokens: TokenSet) -> set[str]:
    cats = {t.category.value for t in tokens.tokens}
    cats |= {s.category.value for s in tokens.scales}
    return cats


async def analyse(page, tokens: TokenSet, components: list[ComponentRule] | None = None, *,
                  theme: Theme | str | None = None, rules: dict[str, Any] | None = None,
                  categories: list[str] | None = None) -> Analysis:
    """Read the page once, probe, match. system_check and adherence both start here."""
    rules = rules or {}
    components = list(components or [])
    theme = Theme(theme) if theme else None
    await page.evaluate(page_js.TRANSITIONS_OFF, True)
    try:
        data = await page.evaluate(page_js.COLLECT, {
            "max": int(rules.get("max_elements", 4000)),
            "components": [c.model_dump() for c in components],
            "ignore": list(rules.get("ignore") or []),
        })
        if theme is None:
            theme = Theme.DARK if data.get("themeHint") == "dark" else Theme.LIGHT
        records = data["records"]
        have = _categories(tokens)
        cats = [c for c in (categories or list(PROPS_BY_CAT)) if c in have]
        a = Analysis(records=records, values=[], tokens=tokens, theme=theme, components=components,
                     area=data.get("area"), truncated=bool(data.get("truncated")), typed=bool(data.get("typed")))
        a.notes += _read_notes(a, data)
        probe = None
        if rules.get("probe", True) and cats:
            probe = await run_probe(page, records, tokens, theme, cats,
                                    timeout_s=float(rules.get("probe_timeout_s", 20)))
            a.notes += probe.notes
        grouped = _grouped_values(records, probe)
        for cat in cats:
            if probe is None:
                a.method[cat] = "value: the probe was turned off" if not rules.get("probe", True) else "value"
            else:
                why = probe.ran.get(cat, "value")
                a.method[cat] = "probe" if why == "probe" else f"value: {why}"
        root_px = data.get("rootFontSize") or 16.0
        for val in grouped.values():
            if val.category not in cats:
                continue
            val.match = match(val.value, val.category, tokens, theme=theme, prop=val.props[0],
                              font_size_px=val.fs, root_px=root_px, rules=rules)
            if a.method.get(val.category) == "probe" and probe is not None:
                _take_provenance(val, probe)
            a.values.append(val)
        if probe:
            _distrust_still_categories(a, cats, probe)
        return a
    finally:
        with contextlib.suppress(Exception):   # the page may have been reloaded by the probe
            await page.evaluate(page_js.CLEANUP)


def _read_notes(a: Analysis, data: dict) -> list[str]:
    """What the read of the page left out, for the coverage line."""
    notes = []
    if a.truncated:
        notes.append(f"only the first {len(a.records)} of {data.get('total')} elements were read")
    if data.get("leftOut"):
        n = data["leftOut"]
        notes.append(f"{n} region{'s' if n != 1 else ''} left out: pictures (role=img, inert) and regions marked "
                     "as another product (data-studio=\"ignore\", ignore)")
    if not a.typed:
        notes.append("this engine has no typed computed values, so equal left and right margins "
                     "were taken as centring and not checked")
    return notes


def _grouped_values(records: list[dict], probe: Any) -> dict[tuple, Value]:
    """One value per element, property family and value; with the probe, also the values a
    hidden state showed only to it."""
    grouped: dict[tuple, Value] = {}
    for rec in records:
        for prop, cat, v, ua, fs in rec["v"]:
            if cat == "shadow":
                # a ring or an inset edge is a line drawn as a shadow, not elevation: left out
                # (its colour is often a translucent tint no token names, so it is not judged)
                v = elevation(v)
                if v is None:
                    continue   # only rings, edges and empty layers: borders drawn as a shadow, not one
            key = (rec["i"], _family(prop), cat, v)
            if key in grouped:
                grouped[key].props.append(prop)
            else:
                grouped[key] = Value(rec, [prop], cat, v, bool(ua), fs)
    if probe:
        by_i = {r["i"]: r for r in records}
        for i, prop, base in probe.hidden:
            cat = next(c for p, c, _ in page_js.PROPS if p == prop)
            fs = parse_length((probe.base_rows[i] if probe.base_rows else {}).get("font-size") or "16px") or 16.0
            key = (i, _family(prop), cat, base)
            if key not in grouped:
                grouped[key] = Value(by_i[i], [prop], cat, base, False, fs)
    return grouped


def _take_provenance(val: Value, probe: Any) -> None:
    """Where the probe saw this value come from: a token first, then a derived value."""
    provs = [probe.prov.get((val.rec["i"], p)) for p in val.props]
    provs = [p for p in provs if p is not None]
    if provs:
        val.prov = next((p for p in provs if p.kind == "token"), None) or \
            next((p for p in provs if p.kind == "derived"), None) or provs[0]
        val.method = "probe"


def _distrust_still_categories(a: Analysis, cats: list[str], probe: Any) -> None:
    """A category where nothing moved but values match tokens exactly: the page most likely
    writes the values in at build time, so a still value proves nothing."""
    for cat in cats:
        if a.method.get(cat) != "probe":
            continue
        vals = [v for v in a.values if v.category == cat]
        exact = sum(1 for v in vals if v.match and v.match.kind == "exact")
        if probe.moved.get(cat, 0) == 0 and exact >= 1:
            a.method[cat] = ("value: no value moved when its tokens changed, so the page likely writes "
                             "token values in at build time; look-alikes cannot be told apart here")
            for v in vals:
                v.method = "value"
                v.prov = None


def _line(la: Any) -> bool:
    """A layer with no blur that draws a line: a ring (no offset, a spread) or an inset edge."""
    return la.blur == 0 and ((la.x == 0 and la.y == 0) or la.inset)


def elevation(value: str) -> str | None:
    """A box-shadow as the shadow it draws: transparent layers dropped (Tailwind pads every
    shadow with empty ring and shadow layers), and lines dropped (rings with no offset and no
    blur, inset edges with no blur: ring-1, a focus ring, a 2 px bar), which are borders rather
    than elevation. None when nothing else is left."""
    layers = parse_shadow(value)
    if layers is None:
        return value
    kept = [la for la in layers if la.color is None or la.color.alpha > 0.001]
    rest = [la for la in kept if not _line(la)]
    if not rest:
        return None
    return value if len(rest) == len(layers) else shadow_css(rest)


# --- findings -----------------------------------------------------------------------------

def _display(category: str, value: str, fs: float | None = None) -> str:
    if category == "color":
        c = parse_color(value)
        return c.hex() if c else value
    if category == "line_height":
        n = parse_number(value)
        if n is not None:
            return f"{value} ({fmt_px(n * (fs or 16))})"
    return value


def _token_display(tokens: TokenSet, name: str | None, theme: Theme | None) -> tuple[str | None, str | None]:
    if not name:
        return None, None
    for t in tokens.tokens:
        if t.name == name and (theme is None or theme in t.themes):
            if t.category == C.COLOR:
                c = parse_color(t.value)
                return (c.hex() if c else t.value), t.css_var
            return t.value, t.css_var
    px = scale_px(name, tokens)
    if px is not None:
        return fmt_px(px), None
    return None, None


def _value_key(v: Value) -> str:
    if v.category == "color":
        c = parse_color(v.value)
        return c.hex() if c else v.value
    return v.value


def _component(rec: dict) -> ComponentRef | None:
    c = rec.get("comp")
    if not c:
        return None
    return ComponentRef(name=c["name"], source=c.get("source"), via=c.get("via") or "none")


def _rect(rec: dict) -> Rect | None:
    r = rec.get("rect")
    return Rect(x=r[0], y=r[1], w=r[2], h=r[3]) if r else None


def _fix(kind: str, disp: str, tok: str | None, tok_disp: str | None, var: str | None, dist: str | None,
         ua: bool, tag: str, props: list[str]) -> str:
    use = f"{tok} (var({var}))" if tok and var else (tok or "a token")
    prop = props[0]
    if kind == "hardcoded":
        lead = f"This is the browser's default for <{tag}>. " if ua else ""
        return f"{lead}Write {prop}: var({var}) instead of the hard-coded {disp}; it has the same value." if var \
            else f"{lead}Use {use} instead of the hard-coded {disp}; it has the same value."
    lead = f"{disp} is the browser's default for <{tag}>. " if ua else ""
    if kind == "near":
        return f"{lead}Use {use} ({tok_disp}) instead of {disp}: it is {dist} away, almost certainly the one meant."
    if tok:
        return (f"{lead}{disp} is not on the system. The nearest token is {use} ({tok_disp}, {dist} away): "
                "use it, or add a token if this value is intended.")
    return f"{lead}{disp} is not on the system and no token is close. Use a token, or add one if this value is intended."


def _distance(category: str, m: Match | None) -> tuple[str, dict]:
    if m is None or m.distance is None:
        return "", {}
    if category == "color":
        return f"delta E {m.distance:.3f}", {"delta_eok": m.distance}
    if category in ("font_weight",):
        return f"{m.distance:g}", {"delta": m.distance}
    if category == "font_family":
        return "a different family", {}
    return fmt_px(m.distance), {"delta_px": m.distance}


def findings_from(a: Analysis, ctx: CheckContext) -> CheckOutput:
    screen = ctx.screen.key
    want = _wanted(ctx.only)
    findings = [_value_finding(a, key, vals, screen) for key, vals in _off_system(a).items() if key[0] in want]
    if "component.hand_rolled" in want and a.components:
        findings += _hand_rolled(a, screen)
    order = {Severity.BLOCKER: 0, Severity.MAJOR: 1, Severity.MINOR: 2, Severity.INFO: 3}
    findings.sort(key=lambda f: (order[f.severity], -(f.seen_at[0].count if f.seen_at else 0), f.check, f.id))
    return CheckOutput(findings=findings, coverage=_coverage(a, screen, want))


def _off_system(a: Analysis) -> dict[tuple, list[Value]]:
    """The values that are not on the system, grouped by (check, component, category, value,
    browser default, match kind): one finding each. A look-alike (exact by value, but fixed
    when the tokens moved) is a hard-coded value."""
    groups: dict[tuple, list[Value]] = defaultdict(list)
    for v in a.values:
        if v.on_system or v.match is None:
            continue
        lookalike = v.method == "probe" and v.prov is not None and v.prov.kind == "fixed" and v.match.kind == "exact"
        if not lookalike and v.match.kind == "exact":
            continue
        check = "token.hardcoded" if lookalike else CHECK_OF[v.category]
        comp = (v.rec.get("comp") or {}).get("name")
        groups[(check, comp, v.category, _value_key(v), v.ua, v.match.kind)].append(v)
    return groups


def _value_finding(a: Analysis, key: tuple, vals: list[Value], screen: str) -> Finding:
    check, comp, cat, vkey, ua, kind = key
    first = vals[0]
    m = first.match
    tok_disp, var = _token_display(a.tokens, m.token if m else None, a.theme)
    disp = _display(cat, first.value, first.fs)
    dist_text, dist = _distance(cat, m)
    props = sorted({p for v in vals for p in v.props})
    fam = sorted({_family(p) for p in props})
    sels = []
    for v in vals:
        if v.rec["sel"] not in sels:
            sels.append(v.rec["sel"])
    measured = {"value": disp, "category": cat, "properties": fam, "count": len(sels),
                "selectors": sels[:MAX_SELECTORS], "method": first.method, **dist}
    if first.value != disp:
        measured["computed"] = first.value
    if first.method == "probe":
        measured["provenance"] = "hard-coded: it did not move when the tokens changed"
    if ua:
        measured["origin"] = "browser default"
    expected = {"token": m.token if m else None, "value": tok_disp, "css_var": var,
                "candidates": (m.candidates if m else [])[:6]}
    if check == "token.hardcoded":
        severity, fkind = Severity.MINOR, "hardcoded"
    elif kind == "near":
        severity, fkind = Severity.MINOR, "near"
    else:
        severity, fkind = (Severity.MAJOR if cat == "color" else Severity.MINOR), "off"
    measured["match"] = kind
    fix = _fix(fkind, disp, m.token if m else None, tok_disp, var, dist_text, ua, first.rec["tag"], fam)
    anchor = f"{cat}:{vkey}" + (":ua" if ua else "") + (":near" if kind == "near" and check != "token.hardcoded" else "")
    return Finding(
        id=finding_id(check, comp, anchor), check=check, severity=severity, confidence=Confidence.MEASURED,
        where=Where(selector=first.rec["sel"], role=first.rec.get("role"), rect=_rect(first.rec)),
        component=_component(first.rec), seen_at=[SeenAt(screen=screen, count=len(sels))],
        measured=measured, expected=expected, fix=fix)


def _hand_rolled(a: Analysis, screen: str) -> list[Finding]:
    """Controls outside every known component."""
    out: list[Finding] = []
    hand: dict[tuple, list[dict]] = defaultdict(list)
    for rec in a.records:
        if rec.get("inter") and not rec.get("inComp"):
            hand[(rec["tag"], rec.get("role") or "")].append(rec)
    names = ", ".join(sorted({c.name for c in a.components if c.name != "*"})[:5]) or "a known component"
    for (tag, role), recs in hand.items():
        what = f"<{tag}" + (f" role={role}" if role else "") + ">"
        out.append(Finding(
            id=finding_id("component.hand_rolled", None, f"{tag}|{role}"), check="component.hand_rolled",
            severity=Severity.MINOR, confidence=Confidence.HEURISTIC,
            where=Where(selector=recs[0]["sel"], role=recs[0].get("role"), rect=_rect(recs[0])),
            seen_at=[SeenAt(screen=screen, count=len(recs))],
            measured={"control": what, "count": len(recs), "selectors": [r["sel"] for r in recs][:MAX_SELECTORS]},
            expected={"rule": "every control comes from a known component", "components": names},
            fix=f"Use one of the design system's components ({names}) instead of a hand-built {what}.",
            why_heuristic=("a control outside every known component is taken as hand-built; a component "
                           "the rules do not list looks the same")))
    return out


def _wanted(only: list[str] | None) -> set[str]:
    if not only:
        return set(ALL_CHECKS)
    return {c for c in ALL_CHECKS if any(c == o or c.startswith((o.rstrip(".*") + ".", o)) for o in only)}


def _how(cats: list[str], method: dict[str, str]) -> str:
    """'colour and spacing by probe; radius by value (why)', with each reason said once."""
    by: dict[str, list[str]] = defaultdict(list)
    for c in cats:
        by[method.get(c, "value")].append(CATEGORY_WORDS[c])
    parts = []
    for m, words in by.items():
        if m == "probe":
            parts.append(f"{' and '.join(words)} by the provenance probe")
        else:
            why = m.removeprefix("value").removeprefix(": ")
            parts.append(f"{' and '.join(words)} by value only" + (f" ({why})" if why else ""))
    return "; ".join(parts)


def _coverage(a: Analysis, screen: str, want: set[str]) -> list[CoverageItem]:
    have = _categories(a.tokens)
    items = []
    extra = "; ".join(a.notes)
    for check in TOKEN_CHECKS:
        if check not in want:
            continue
        cats = [c for c, ch in CHECK_OF.items() if ch == check]
        present = [c for c in cats if c in have]
        if not present:
            words = " or ".join(CATEGORY_WORDS[c] for c in cats)
            items.append(CoverageItem(check=check, screen=screen, status="skipped",
                                      reason=f"the token set has no {words} tokens"))
            continue
        counted = sum(1 for v in a.values if v.category in present)
        reason = f"{counted} value(s) read; {_how(present, a.method)}" + (f"; {extra}" if extra else "")
        items.append(CoverageItem(check=check, screen=screen, status="ran", reason=reason))
    if "token.hardcoded" in want:
        probed = [c for c, m in a.method.items() if m == "probe"]
        not_probed = [c for c, m in a.method.items() if m != "probe"]
        if probed:
            reason = _how(probed, a.method)
            if not_probed:
                reason += "; look-alikes cannot be found for " + _how(not_probed, a.method)
            items.append(CoverageItem(check="token.hardcoded", screen=screen, status="ran", reason=reason))
        else:
            reason = ("look-alikes cannot be found: " + _how(not_probed, a.method)) if not_probed \
                else "the token set has nothing to probe"
            items.append(CoverageItem(check="token.hardcoded", screen=screen, status="skipped", reason=reason))
    if "component.hand_rolled" in want:
        if a.components:
            items.append(CoverageItem(check="component.hand_rolled", screen=screen, status="ran",
                                      reason=f"{sum(1 for r in a.records if r.get('inter'))} control(s) read"))
        else:
            items.append(CoverageItem(check="component.hand_rolled", screen=screen, status="skipped",
                                      reason="no component rules were given"))
    return items


async def system_check(page, tokens: TokenSet, components: list[ComponentRule] | None,
                       ctx: CheckContext) -> CheckOutput:
    """Findings for one screen: token.color, token.space, token.radius, token.font,
    token.shadow, token.border (off or near the system), token.hardcoded (a look-alike the
    probe saw not move) and component.hand_rolled (a control outside any known component)."""
    want = _wanted(ctx.only)
    cats = [c for c, ch in CHECK_OF.items() if ch in want or "token.hardcoded" in want]
    a = await analyse(page, tokens, components, theme=ctx.screen.theme, rules=ctx.rules, categories=cats)
    return findings_from(a, ctx)


def adherence_from(a: Analysis) -> Adherence:
    by: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for v in a.values:
        by[v.category][1] += 1
        if v.on_system:
            by[v.category][0] += 1
    controls = [r for r in a.records if r.get("inter")]
    inside = sum(1 for r in controls if r.get("inComp"))
    return Adherence(
        by_category={c: round(on / n, 4) for c, (on, n) in sorted(by.items()) if n},
        area_coverage=round(a.area, 4) if a.components and a.area is not None else None,
        interactive_coverage=round(inside / len(controls), 4) if a.components and controls else None,
        hand_rolled=(len(controls) - inside) if a.components else 0,
    )


async def adherence(page, tokens: TokenSet, components: list[ComponentRule] | None = None, *,
                    theme: Theme | str | None = None, rules: dict[str, Any] | None = None) -> Adherence:
    """How much of the page is on the system: the share of values on a token per category,
    the share of painted area inside known components and the share of controls inside one."""
    a = await analyse(page, tokens, components, theme=theme, rules=rules)
    return adherence_from(a)


async def check_and_adherence(page, tokens: TokenSet, components: list[ComponentRule] | None,
                              ctx: CheckContext) -> tuple[CheckOutput, Adherence]:
    """Both from one read of the page (one probe)."""
    a = await analyse(page, tokens, components, theme=ctx.screen.theme, rules=ctx.rules)
    return findings_from(a, ctx), adherence_from(a)


__all__ = ["Analysis", "adherence", "adherence_from", "analyse", "check_and_adherence", "findings_from",
           "system_check"]
