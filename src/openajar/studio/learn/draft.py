"""From what was read to a brand draft: declared tokens where the site has them, clusters where it
has none, the coverage of each, and the profile's Brand form (DTCG tokens, fonts, components,
theme). Pure: `build_draft(reads)` needs no browser."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from ..contracts import Scale, Theme, ThemeSwitch, Token, TokenCategory, TokenSet
from ..tokens.color import EXACT_DE, delta_e, parse_color
from ..tokens.export import to_dtcg
from ..tokens.match import match
from ..tokens.units import ROOT_PX, fmt_px, font_stack, parse_length, parse_weight
from . import cluster as cl
from .read import DRAFTED, ScreenRead, class_roots

C = TokenCategory
FOUND, WEAK = 0.75, 0.5          # coverage from which a clustered category is a system, or a weak one
PALETTE_SHOWN = 16
NAME_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_COMPONENTS = 30
WORDS = {"color": "colour", "space": "spacing", "radius": "radius", "font_family": "font family",
         "font_size": "font size", "font_weight": "font weight"}
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CategorySummary(_M):
    """One category. `source` says where the tokens came from; `system` whether the page holds
    to one (declared: the site says so itself; found or weak or none: clustered, a heuristic)."""

    source: Literal["declared", "clustered"]
    system: Literal["declared", "found", "weak", "none"]
    tokens: int = 0
    values: int = 0                   # places a value is written (origins), light theme
    on_tokens: int = 0
    coverage: float | None = None     # on_tokens / values
    one_offs: int = 0                 # distinct values that fit no token
    one_off_examples: list[str] = Field(default_factory=list)
    note: str | None = None


class BrandDraft(_M):
    url: str
    pages: list[str]
    screens: list[str]
    failed: list[str] = Field(default_factory=list)
    categories: dict[str, CategorySummary] = Field(default_factory=dict)
    palette: list[dict[str, Any]] = Field(default_factory=list)
    space_step_px: float | None = None
    type_scale: dict[str, float] = Field(default_factory=dict)
    type_ratio: float | None = None
    theme: dict[str, Any] | None = None
    theme_note: str | None = None
    components: list[dict[str, Any]] = Field(default_factory=list)
    fonts: list[dict[str, Any]] = Field(default_factory=list)
    uncertain: list[str] = Field(default_factory=list)
    tokens: TokenSet = Field(default_factory=TokenSet)
    brand: dict[str, Any] = Field(default_factory=dict)

    @property
    def has_system(self) -> bool:
        return any(c.system != "none" for c in self.categories.values())

    def counts(self) -> dict[str, int]:
        return {k: c.tokens for k, c in self.categories.items()}

    def reason(self) -> str:
        """For the owner's approval card: what was read and from where, in at most 500 characters."""
        host = urlsplit(self.url).netloc
        themes = [t for t in ("light", "dark") if any(s.endswith("-" + t) for s in self.screens)]
        widths = sorted({int(s.split("@", 1)[1].rsplit("-", 1)[0]) for s in self.screens})
        parts = [(f"Learned from {host}: {len(self.pages)} page(s), {' and '.join(themes)}, at "
                  f"{' and '.join(str(w) for w in widths)} px.")]
        declared = [WORDS[k] for k, c in self.categories.items() if c.source == "declared"]
        clustered = [f"{WORDS[k]} ({round((c.coverage or 0) * 100)}% fit)" for k, c in self.categories.items()
                     if c.source == "clustered" and c.system in ("found", "weak")]
        if declared:
            parts.append("Declared by the site's custom properties: " + ", ".join(declared) + ".")
        if clustered:
            parts.append("Guessed from the values the pages use (heuristic, check the names): "
                         + ", ".join(clustered) + ".")
        if self.theme:
            parts.append(f"Dark theme by {self.theme['how']}.")
        if self.components:
            parts.append(f"{len(self.components)} component hint(s).")
        text = " ".join(parts)
        return text if len(text) <= 500 else text[:497].rsplit(" ", 1)[0] + "..."

    def summary(self) -> dict[str, Any]:
        """What an agent gets: small (counts, the palette, what is uncertain)."""
        cats = {}
        for k, c in self.categories.items():
            row: dict[str, Any] = {"source": c.source, "system": c.system, "tokens": c.tokens,
                                   "coverage": c.coverage, "one_offs": c.one_offs}
            if c.one_off_examples:
                row["one_off_examples"] = c.one_off_examples[:4]
            if c.note:
                row["note"] = c.note
            cats[k] = row
        out: dict[str, Any] = {
            "pages": self.pages, "screens": len(self.screens), "categories": cats,
            "palette": self.palette[:PALETTE_SHOWN],
        }
        if self.space_step_px:
            out["space_step_px"] = self.space_step_px
        if self.type_scale:
            out["type_scale"] = self.type_scale
        if self.type_ratio:
            out["type_ratio"] = self.type_ratio
        out["theme"] = self.theme or {"how": None, "note": self.theme_note}
        if self.components:
            out["components"] = [c["name"] for c in self.components]
        if self.fonts:
            out["fonts"] = [f["family"] for f in self.fonts]
        if self.failed:
            out["failed"] = self.failed
        out["uncertain"] = self.uncertain
        out["heuristic"] = ("Clustered categories, colour roles, component hints and the type scale names are "
                            "guesses (labelled heuristic in the draft); declared tokens are the site's own.")
        return out


@dataclass
class Reads:
    url: str
    pages: list[str]
    light: list[ScreenRead] = field(default_factory=list)
    dark: list[ScreenRead] = field(default_factory=list)
    declared: TokenSet | None = None
    usage: dict[str, dict[str, int]] = field(default_factory=dict)     # --var -> {rules, elements}
    hints: dict[str, dict[str, int]] = field(default_factory=lambda: {"comp": {}, "slot": {}, "cls": {}, "svg": {},
                                                                      "util": {}})
    theme: ThemeSwitch | None = None
    theme_note: str | None = None
    failed: list[str] = field(default_factory=list)
    screens: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class _Tok:
    token: Token
    uses: int
    how: str                     # declared | clustered
    role: str | None = None
    note: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    rank: int | None = None      # how the palette is ordered: places written plus elements reached


def _dedupe(reads: list[ScreenRead]) -> dict[str, list[cl.Origin]]:
    """Origins of every screen of a theme, one per (place, value); the widest reach kept."""
    best: dict[tuple, cl.Origin] = {}
    for r in reads:
        for o in r.origins:
            k = (o.loc, o.value)
            if k not in best or o.reach > best[k].reach:
                best[k] = o
    out: dict[str, list[cl.Origin]] = defaultdict(list)
    for o in best.values():
        out[o.category].append(o)
    return out


def _verdict(on: int, total: int) -> tuple[str, float | None]:
    if total == 0:
        return "none", None
    share = round(on / total, 3)
    return ("found" if share >= FOUND else "weak" if share >= WEAK else "none"), share


def _hex(value: str) -> str:
    c = parse_color(value)
    return c.hex() if c else value


def _px(value: str) -> float | None:
    v = (value or "").strip()
    if v.endswith("%"):
        try:
            return cl.FULL_RADIUS_PX if float(v[:-1]) >= 50 else None
        except ValueError:
            return None
    px = parse_length(v)
    return abs(px) if px is not None else None


# ---- declared ------------------------------------------------------------------------------


def _declared(cat: str, origins: list[cl.Origin], ts: TokenSet, usage: dict) -> tuple[list[_Tok], CategorySummary]:
    toks = [t for t in ts.tokens if t.category.value == cat]
    sub = TokenSet(tokens=toks, scales=[s for s in ts.scales if s.category.value == cat])
    per_token: Counter = Counter()
    off: Counter = Counter()
    on = 0
    for o in origins:
        m = match(o.value, cat, sub, theme=Theme.LIGHT, prop=o.prop)
        if m.kind == "exact":
            on += 1
            if m.token:
                per_token[m.token] += 1
        else:
            off[_hex(o.value) if cat == "color" else o.value] += 1
    out = []
    for t in toks:
        u = usage.get(t.css_var or "", {})
        refs = int(u.get("rules", 0))
        out.append(_Tok(t, uses=per_token.get(t.name, 0), how="declared",
                        note=f"declared by the site as {t.css_var}; used in {refs} CSS rule(s)" if t.css_var else None,
                        extra={"css_rules": refs, "css_elements": int(u.get("elements", 0))}))
    _, share = _verdict(on, len(origins))
    names = {t.name for t in toks}
    summary = CategorySummary(source="declared", system="declared", tokens=len(names), values=len(origins),
                              on_tokens=on, coverage=share, one_offs=len(off),
                              one_off_examples=[v for v, _ in off.most_common(4)])
    unused = [t.name for t in toks if not usage.get(t.css_var or "", {}).get("rules")]
    if unused:
        summary.note = f"{len(set(unused))} declared token(s) no rule uses"
    return out, summary


# ---- clustered -----------------------------------------------------------------------------


def _colors(origins: list[cl.Origin], dark: list[cl.Origin], have_dark: bool) -> tuple[list[_Tok], CategorySummary, list[str]]:
    clusters = cl.cluster_colors(origins)
    names = cl.name_colors(clusters)
    dark_at: dict[tuple, Counter] = defaultdict(Counter)
    for o in dark:
        c = parse_color(o.value)
        if c is not None:
            dark_at[o.loc][c.hex()] += 1
    toks: list[_Tok] = []
    uncertain: list[str] = []
    on = sum(c.uses for c in clusters if c.token)
    for i, c in enumerate(clusters):
        name = names.get(i)
        if name is None:
            continue
        light = c.center.css()
        why = (f"heuristic: {c.uses} place(s) on the page"
               + (f", inherited by {c.reach} element(s)" if c.reach else "") + f"; role {c.role} guessed from use")
        if not have_dark:
            toks.append(_Tok(Token(name=name, category=C.COLOR, value=light), c.uses, "clustered", c.role, why, rank=c.uses + c.reach))
            continue
        seen: Counter = Counter()
        for loc in c.locs:
            seen.update(dark_at.get(loc, {}))
        if not seen:
            toks.append(_Tok(Token(name=name, category=C.COLOR, value=light), c.uses, "clustered", c.role,
                             why + "; no dark value was read at the same places", rank=c.uses + c.reach))
            uncertain.append(f"{name}: no dark value read where it is used; kept for both themes")
            continue
        top, n = seen.most_common(1)[0]
        dc = parse_color(top)
        if n / sum(seen.values()) < 0.6:
            uncertain.append(f"{name}: its dark value varies ({len(seen)} values); the most used was taken")
        if dc is None or delta_e(dc, c.center) < EXACT_DE and abs(dc.alpha - c.center.alpha) < 0.01:
            toks.append(_Tok(Token(name=name, category=C.COLOR, value=light), c.uses, "clustered", c.role, why, rank=c.uses + c.reach))
        else:
            toks.append(_Tok(Token(name=name, category=C.COLOR, value=light, themes=[Theme.LIGHT]), c.uses,
                             "clustered", c.role, why, rank=c.uses + c.reach))
            toks.append(_Tok(Token(name=name, category=C.COLOR, value=dc.css(), themes=[Theme.DARK]), c.uses,
                             "clustered", c.role, why, rank=c.uses + c.reach))
    off = [c for c in clusters if not c.token]
    system, share = _verdict(on, len(origins))
    s = CategorySummary(source="clustered", system=system, tokens=len(names), values=len(origins), on_tokens=on,
                        coverage=share, one_offs=len(off), one_off_examples=[c.center.hex() for c in off[:4]])
    return toks, s, uncertain


def _space(origins: list[cl.Origin]) -> tuple[list[_Tok], list[Scale], CategorySummary, float | None]:
    vals = [(px, o.reach) for o in origins if (px := _px(o.value))]
    step, fit = cl.detect_step([v for v, _ in vals])
    clusters = cl.cluster_lengths(vals)
    toks: list[_Tok] = []
    scales: list[Scale] = []
    if step:
        on_step = [c for c in clusters if cl._on_step(c.px, step)]
        for c in on_step:
            n = round(c.px / step)
            toks.append(_Tok(Token(name=f"space.{n}", category=C.SPACE, value=fmt_px(n * step)), c.uses, "clustered",
                             note=f"heuristic: {n} x the {fmt_px(step)} step, {c.uses} place(s)"))
        scales.append(Scale(category=C.SPACE, step_px=step))
        off = [c for c in clusters if c not in on_step]
        on = sum(c.uses for c in on_step)
        note = f"step {fmt_px(step)}: {round(fit * 100)}% of spacing values are multiples of it (heuristic)"
    else:
        keep = [c for c in clusters if c.token]
        for c in keep:
            toks.append(_Tok(Token(name=f"space.{_seg(fmt_px(c.px))}", category=C.SPACE, value=fmt_px(c.px)), c.uses,
                             "clustered", note=f"heuristic: {c.uses} place(s); no common step"))
        off = [c for c in clusters if not c.token]
        on = sum(c.uses for c in keep)
        note = "no common step: no step of 2 px or more fits most spacing values"
    system, share = _verdict(on, len(vals))
    if not step and system == "found":
        system = "weak"
    s = CategorySummary(source="clustered", system=system, tokens=len(toks), values=len(vals), on_tokens=on,
                        coverage=share, one_offs=len(off), one_off_examples=[fmt_px(c.px) for c in off[:4]], note=note)
    return toks, scales, s, step


def _seg(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", text.replace(".", "_"))


def _radius(origins: list[cl.Origin]) -> tuple[list[_Tok], CategorySummary]:
    vals = [(px, o.reach) for o in origins if (px := _px(o.value))]
    clusters = cl.cluster_lengths(vals)
    keep = [c for c in clusters if c.token]
    names = cl.radius_names([c.px for c in keep])
    toks = [_Tok(Token(name=f"radius.{names[c.px]}", category=C.RADIUS,
                       value="9999px" if names[c.px] == "full" else fmt_px(c.px)), c.uses, "clustered",
                 note=f"heuristic: {c.uses} place(s)") for c in keep]
    off = [c for c in clusters if not c.token]
    system, share = _verdict(sum(c.uses for c in keep), len(vals))
    return toks, CategorySummary(source="clustered", system=system, tokens=len(toks), values=len(vals),
                                 on_tokens=sum(c.uses for c in keep), coverage=share, one_offs=len(off),
                                 one_off_examples=[fmt_px(c.px) for c in off[:4]])


def _families(origins: list[cl.Origin]) -> tuple[list[_Tok], CategorySummary, list[dict]]:
    groups: dict[str, list[cl.Origin]] = defaultdict(list)
    for o in origins:
        groups[", ".join(font_stack(o.value))].append(o)
    order = sorted(groups, key=lambda k: (-max(o.reach for o in groups[k]), -len(groups[k]), k))
    toks: list[_Tok] = []
    fonts: list[dict] = []
    used_names: Counter = Counter()
    on = 0
    off = []
    for key in order:
        os_ = groups[key]
        if not cl.counts_as_system(len(os_), max(o.reach for o in os_)):
            off.append(key)
            continue
        on += len(os_)
        stack = font_stack(key)
        tags = Counter(o.tag for o in os_)
        if "monospace" in stack or "ui-monospace" in stack or tags["code"] + tags["pre"] > len(os_) / 2:
            role = "mono"
        elif sum(n for t, n in tags.items() if t in HEADINGS) > len(os_) / 2:
            role = "heading"
        else:
            role = "body"
        used_names[role] += 1
        name = f"font.family.{role}" + (f"-{used_names[role]}" if used_names[role] > 1 else "")
        toks.append(_Tok(Token(name=name, category=C.FONT_FAMILY, value=os_[0].value), len(os_), "clustered", role,
                         note=f"heuristic: {len(os_)} place(s); role {role} guessed from the elements"))
        fam = _family_name(os_[0].value)
        if fam and len(fonts) < 12 and fam.lower() not in {f["family"].lower() for f in fonts}:
            fonts.append({"family": fam, "role": role})
    system, share = _verdict(on, len(origins))
    return toks, CategorySummary(source="clustered", system=system, tokens=len(toks), values=len(origins),
                                 on_tokens=on, coverage=share, one_offs=len(off),
                                 one_off_examples=[k[:40] for k in off[:4]]), fonts


def _family_name(value: str) -> str | None:
    for part in re.findall(r'"[^"]*"|\'[^\']*\'|[^,]+', value or ""):
        p = part.strip().strip("'\"").strip()
        if p and p.lower() not in cl.GENERIC_FAMILIES:
            return p[:100]
    return None


def _sizes(origins: list[cl.Origin]) -> tuple[list[_Tok], CategorySummary, dict[str, float], float | None]:
    vals = [(px, o.reach) for o in origins if (px := _px(o.value))]
    clusters = cl.cluster_lengths(vals)
    keep = [c for c in clusters if c.token]
    based = [c for c in keep if c.reach >= cl.BASE_REACH]
    implicit = None
    if based:
        base = max(based, key=lambda c: (c.reach, c.uses)).px
    else:
        # no size is inherited widely: the page keeps the root size, which is its base
        base = implicit = ROOT_PX
    names, ratio = cl.type_scale([c.px for c in keep] + ([implicit] if implicit else []), base)
    toks = [_Tok(Token(name=f"font.size.{names[c.px]}", category=C.FONT_SIZE, value=fmt_px(c.px)), c.uses, "clustered",
                 note=f"heuristic: {c.uses} place(s); the name is its place on the scale around the base")
            for c in keep]
    if implicit is not None and keep and all(abs(c.px - implicit) > cl.JOIN_PX for c in keep):
        toks.append(_Tok(Token(name="font.size.base", category=C.FONT_SIZE, value=fmt_px(implicit)), 0, "clustered",
                         note="heuristic: the root size, which the page keeps for its body text and never writes"))
        keep_px = [c.px for c in keep] + [implicit]
    else:
        keep_px = [c.px for c in keep]
    off = [c for c in clusters if not c.token]
    system, share = _verdict(sum(c.uses for c in keep), len(vals))
    scale = {names[px]: px for px in sorted(keep_px)}
    s = CategorySummary(source="clustered", system=system, tokens=len(toks), values=len(vals),
                        on_tokens=sum(c.uses for c in keep), coverage=share, one_offs=len(off),
                        one_off_examples=[fmt_px(c.px) for c in off[:4]],
                        note=f"ratio about {ratio} between steps above the base (heuristic)" if ratio else None)
    return toks, s, scale, ratio


def _weights(origins: list[cl.Origin]) -> tuple[list[_Tok], CategorySummary]:
    groups: dict[float, list[cl.Origin]] = defaultdict(list)
    for o in origins:
        w = parse_weight(o.value)
        if w is not None:
            groups[w].append(o)
    toks, off, on = [], [], 0
    for w in sorted(groups):
        os_ = groups[w]
        if cl.counts_as_system(len(os_), max(o.reach for o in os_)):
            on += len(os_)
            toks.append(_Tok(Token(name=f"font.weight.{cl.weight_name(w)}", category=C.FONT_WEIGHT, value=str(int(w))),
                             len(os_), "clustered", note=f"heuristic: {len(os_)} place(s)"))
        else:
            off.append(str(int(w)))
    total = sum(len(v) for v in groups.values())
    system, share = _verdict(on, total)
    return toks, CategorySummary(source="clustered", system=system, tokens=len(toks), values=total, on_tokens=on,
                                 coverage=share, one_offs=len(off), one_off_examples=off[:4])


# ---- the whole draft -----------------------------------------------------------------------


def _declares(ts: TokenSet | None, cat: str) -> bool:
    if ts is None:
        return False
    names = {t.name for t in ts.tokens if t.category.value == cat}
    return len(names) >= 2 or (cat == "space" and any(s.category == C.SPACE for s in ts.scales))


def _components(hints: dict[str, dict[str, int]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(row: dict[str, Any]) -> None:
        key = row["name"].lower()
        if key in seen or len(out) >= MAX_COMPONENTS:
            return
        seen.add(key)
        out.append(row)

    for v, n in sorted(hints.get("comp", {}).items(), key=lambda kv: (-kv[1], kv[0])):
        add({"name": v[:60], "attr": f"data-component={v}", "count": n, "via": "data-component"})
    for v, n in sorted(hints.get("slot", {}).items(), key=lambda kv: (-kv[1], kv[0])):
        add({"name": re.sub(r"(^|[-_])(\w)", lambda m: m.group(2).upper(), v)[:60], "attr": f"data-slot={v}",
             "count": n, "via": "data-slot"})
    for row in class_roots(hints.get("cls", {}), svg=hints.get("svg"), utility=hints.get("util")):
        add({**row, "via": "repeated class (heuristic)"})
    return out


def build_draft(reads: Reads) -> BrandDraft:
    light = _dedupe(reads.light)
    dark = _dedupe(reads.dark)
    have_dark = bool(reads.dark)
    toks: list[_Tok] = []
    scales: list[Scale] = []
    cats: dict[str, CategorySummary] = {}
    uncertain: list[str] = list(reads.notes)
    draft = BrandDraft(url=reads.url, pages=reads.pages, screens=reads.screens, failed=reads.failed,
                       theme=None, theme_note=reads.theme_note)
    for cat in DRAFTED:
        origins = light.get(cat, [])
        if _declares(reads.declared, cat):
            t, s = _declared(cat, origins, reads.declared, reads.usage)
            toks += t
            scales += [x for x in reads.declared.scales if x.category.value == cat]
            cats[cat] = s
            continue
        t, s, sc = _heuristic(cat, origins, dark, have_dark, draft, uncertain)
        scales += sc
        cats[cat] = s
        if s.system in ("found", "weak"):
            toks += t
            if s.system == "weak":
                uncertain.append(f"{WORDS[cat]}: a weak system, {round((s.coverage or 0) * 100)}% of values fit; "
                                 "check before approving")
        else:
            s.tokens = 0
            if s.values:
                uncertain.append(f"{WORDS[cat]}: no consistent system ({s.on_tokens} of {s.values} values would fit, "
                                 f"{s.one_offs} one-off values); nothing drafted for it")
    toks = _writable(_one_per_name(toks), uncertain)
    for cat in DRAFTED:
        if cat in cats and cats[cat].source == "declared":
            cats[cat].tokens = len({t.token.name for t in toks if t.token.category.value == cat})
    draft.categories = cats
    draft.tokens = TokenSet(tokens=[t.token for t in toks], scales=scales, source=f"learned:{reads.url}")
    draft.palette = _palette(toks)
    if reads.theme is not None:
        draft.theme = reads.theme.model_dump(exclude_none=True)
    if reads.dark == [] and reads.theme is None:
        uncertain.append("no dark theme found: media, data-theme, the dark class, data-mode and stored theme "
                         "were tried, and none changed the page's colours")
    draft.components = _components(reads.hints)
    draft.uncertain = uncertain
    draft.brand = _brand(draft, toks)
    return draft


def _heuristic(cat: str, origins: list, dark: dict, have_dark: bool, draft: BrandDraft,
               uncertain: list[str]) -> tuple[list[_Tok], CategorySummary, list[Scale]]:
    """A category the page does not declare, learned from the values it uses. A found or weak
    system also sets the draft's step, fonts or type scale."""
    found = ("found", "weak")
    scales: list[Scale] = []
    if cat == "color":
        t, s, u = _colors(origins, dark.get("color", []), have_dark)
        uncertain += u
    elif cat == "space":
        t, sc, s, step = _space(origins)
        if s.system in found:
            scales += sc
            draft.space_step_px = step
    elif cat == "radius":
        t, s = _radius(origins)
    elif cat == "font_family":
        t, s, fonts = _families(origins)
        if s.system in found:
            draft.fonts = fonts
    elif cat == "font_size":
        t, s, scale, ratio = _sizes(origins)
        if s.system in found:
            draft.type_scale, draft.type_ratio = scale, ratio
    else:
        t, s = _weights(origins)
    return t, s, scales


def _one_per_name(toks: list[_Tok]) -> list[_Tok]:
    """One token per name and theme set (two clusters can round to one step); the uses add up."""
    kept: dict[tuple, _Tok] = {}
    for t in toks:
        k = (t.token.name, tuple(t.token.themes))
        if k in kept:
            kept[k].uses += t.uses
        else:
            kept[k] = t
    return list(kept.values())


def _writable(toks: list[_Tok], uncertain: list[str]) -> list[_Tok]:
    """Names a DTCG file cannot hold are left out, and said."""
    bad = sorted({t.token.name for t in toks if not all(NAME_SEGMENT.match(p) for p in t.token.name.split("."))})
    if not bad:
        return toks
    uncertain.append(f"{len(bad)} declared token name(s) cannot be written as DTCG and were left out")
    return [t for t in toks if t.token.name not in set(bad)]


def _palette(toks: list[_Tok]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for t in toks:
        if t.token.category != C.COLOR:
            continue
        row = rows.setdefault(t.token.name, {"name": t.token.name, "uses": t.uses,
                                             "how": "declared" if t.how == "declared" else "heuristic",
                                             "_rank": t.uses if t.rank is None else t.rank})
        if t.role and t.how != "declared":
            row["role_guess"] = t.role
        hexv = _hex(t.token.value)
        if Theme.LIGHT in t.token.themes:
            row["light"] = hexv
        if Theme.DARK in t.token.themes:
            row["dark"] = hexv
    out = sorted(rows.values(), key=lambda r: (-r["_rank"], r["name"]))
    for r in out:
        del r["_rank"]
    return out


def _annotate(tree: dict, toks: list[_Tok], themed: bool) -> None:
    for t in toks:
        tok = t.token
        if themed:
            group = "base" if {Theme.LIGHT, Theme.DARK} <= set(tok.themes) else tok.themes[0].value
            node: Any = tree.get(group, {})
        else:
            node = tree
        for part in tok.name.split("."):
            node = node.get(part) if isinstance(node, dict) else None
            if node is None:
                break
        if not isinstance(node, dict) or "$value" not in node:
            continue
        if t.note:
            node["$description"] = t.note[:500]
        ext = node.setdefault("$extensions", {}).setdefault("openajar", {})
        ext.update({"learned": t.how, "heuristic": t.how != "declared", "uses": t.uses, **t.extra})
        if t.role and t.how != "declared":
            ext["role_guess"] = t.role


def _brand(draft: BrandDraft, toks: list[_Tok]) -> dict[str, Any]:
    brand: dict[str, Any] = {}
    if draft.tokens.tokens:
        tree = to_dtcg(draft.tokens)
        themed = any(set(t.themes) != {Theme.LIGHT, Theme.DARK} for t in draft.tokens.tokens)
        _annotate(tree, toks, themed)
        brand["tokens"] = tree
    if draft.fonts:
        brand["fonts"] = draft.fonts
    if draft.components:
        brand["components"] = [{k: v for k, v in c.items() if k in ("name", "attr", "class_pattern")}
                               for c in draft.components]
    if draft.theme:
        brand["theme"] = draft.theme
    return brand
