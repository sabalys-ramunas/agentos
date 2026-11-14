"""run_checks: every measured check on one screen of a page that is already open and settled.

The page is a playwright Page (typed Any; nothing here imports playwright). Layout, text,
contrast, target and name checks run in one in-page pass (page.js); contrast over images and
gradients goes to the pixel route (pixels.js and four clipped screenshots); health reads the
page's console, requests and performance timeline; theme.leak compares two snapshots.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from agentos.studio.checks import color, theme
from agentos.studio.checks.registry import BY_ID, IDS, METRIC, selected
from agentos.studio.checks.scripts import script
from agentos.studio.contracts import (
    CheckContext,
    CheckOutput,
    ComponentRef,
    Confidence,
    CoverageItem,
    EngineName,
    Finding,
    Rect,
    Screen,
    SeenAt,
    Severity,
    Theme,
    Where,
    finding_id,
)

SEV_RANK = {Severity.INFO: 0, Severity.MINOR: 1, Severity.MAJOR: 2, Severity.BLOCKER: 3}

# Glyphs as they are, without colour, black and white: the pixel route's four screenshots.
_GLYPH_PSEUDOS = "*,*::before,*::after,*::first-letter,*::first-line,*::marker"


def _glyph_css(c: str | None) -> str | None:
    if c is None:
        return None
    return (f"{_GLYPH_PSEUDOS}{{color:{c}!important;-webkit-text-fill-color:{c}!important;"
            f"text-decoration-color:{c}!important;text-shadow:none!important;caret-color:transparent!important;"
            "transition:none!important;}")


def rule(ctx: CheckContext, check: str, key: str, default: Any) -> Any:
    """A threshold from the profile: `rules["contrast.text.min_ratio"]` or
    `rules["contrast.text"]["min_ratio"]`, else the default."""
    flat = ctx.rules.get(f"{check}.{key}")
    if flat is not None:
        return flat
    nested = ctx.rules.get(check)
    if isinstance(nested, dict) and nested.get(key) is not None:
        return nested[key]
    return default


# --- turning raw records into findings ------------------------------------------------------


def _component(comp: dict | None, area: dict | None = None) -> ComponentRef | None:
    """The component an element is in (data-component, data-slot, data-source). Without one, the
    part of the page it is in, for grouping only: the nearest landmark or titled section
    (via "none") or a class that names a part (via "class"). Only a marker's name is in an id."""
    name = (comp or {}).get("name")
    source = (comp or {}).get("source")
    if not name and source:
        stem = source.rsplit("/", 1)[-1].split(":", 1)[0]
        name = stem.rsplit(".", 1)[0] or stem
    if name:
        return ComponentRef(name=name, source=source, via="attr")
    if area and area.get("name"):
        return ComponentRef(name=str(area["name"])[:80], source=source, via="class" if area.get("how") == "class" else "none")
    return None


def _where(item: dict) -> Where:
    r = item.get("rect")
    rect = Rect(x=r["x"], y=r["y"], w=r["w"], h=r["h"]) if r else None
    return Where(selector=item.get("sel") or "html", role=item.get("role"), name=item.get("name") or None, rect=rect)


def _badness(check: str, measured: dict) -> float:
    key, higher = METRIC.get(check, ("", True))
    v = measured.get(key)
    if not isinstance(v, int | float):
        return 0.0
    return float(v) if higher else -float(v)


class _Folder:
    """Folds instances into one finding per (check, component, place) on this screen."""

    def __init__(self, screen: str, scope: str = ""):
        self.screen = screen
        self.scope = scope
        self.groups: dict[str, dict] = {}

    def add(self, check: str, item: dict, *, severity: Severity, measured: dict, expected: dict,
            fix: str | None, anchor_extra: str = "", confidence: Confidence = Confidence.MEASURED,
            why_heuristic: str | None = None) -> None:
        comp = _component(item.get("comp"), item.get("area"))
        anchor = (item.get("group") or item.get("sel") or "page") + anchor_extra
        # The page's path and hash route are part of the place: the same selector on two routes of
        # one file is two findings. A group label (via none or class) is not: a heading's text or a
        # class can change without changing the finding.
        fid = finding_id(check, comp.name if comp is not None and comp.via == "attr" else None,
                         f"{self.scope}\n{anchor}" if self.scope else anchor)
        g = self.groups.get(fid)
        cand = (SEV_RANK[severity], _badness(check, measured))
        if g is None:
            g = self.groups[fid] = {"count": 0, "rank": None}
        g["count"] += 1
        if g["rank"] is None or cand > g["rank"]:
            g["rank"] = cand
            g["finding"] = {
                "id": fid, "check": check, "severity": severity, "confidence": confidence, "where": _where(item),
                "component": comp, "measured": measured, "expected": expected, "fix": fix,
                "why_heuristic": why_heuristic,
            }

    def findings(self) -> list[Finding]:
        out = []
        for g in self.groups.values():
            f = Finding(**g["finding"], seen_at=[SeenAt(screen=self.screen, count=g["count"])])
            out.append(f)
        return out


# --- the checks that read the in-page pass --------------------------------------------------


def _page_overflow(raw: dict, fold: _Folder, ctx: CheckContext) -> None:
    for it in raw.get("items", []):
        m = dict(it.get("measured", {}))
        cause = it.get("sel") if m.get("cause") != "unknown" else None
        fix = (f"Make {cause} fit the {m.get('viewport')} px viewport (max-width: 100%, flex-wrap, or "
               "overflow-wrap: anywhere for long words)." if cause else
               "Find the element wider than the viewport; none could be named from the layout.")
        fold.add("layout.page_overflow", it, severity=Severity.MAJOR, measured=m,
                 expected={"page_width": f"<= {m.get('viewport')} px"}, fix=fix, anchor_extra="")


def _clipped(raw: dict, fold: _Folder, ctx: CheckContext) -> None:
    for it in raw.get("items", []):
        m = dict(it["measured"], clipped_by=it.get("by"))
        fold.add("layout.clipped", it, severity=Severity.MAJOR, measured=m,
                 expected={"visible_share": 1.0},
                 fix=f"Let {it.get('by')} grow or wrap so the control shows in full, or give it overflow: auto.")


def _truncated(raw: dict, fold: _Folder, ctx: CheckContext) -> None:
    min_hidden = rule(ctx, "text.truncated", "min_hidden", 0.02)
    for it in raw.get("items", []):
        m = dict(it["measured"], clipped_by=it.get("by"))
        if m.get("hidden_share", 0) < min_hidden:
            continue
        kind, reachable = m.get("kind"), m.get("reachable")
        if kind == "cut" and reachable:
            sev = Severity.MINOR
            fix = (f"The full text is in {m.get('full_text_in') or 'a title'}, but the cut shows no sign of it: add "
                   "text-overflow: ellipsis (with white-space: nowrap) to the box that cuts it, or let it wrap.")
        elif kind == "cut":
            sev = Severity.MAJOR
            fix = ("Remove the fixed height or width that cuts this text, or let it wrap; if it must be "
                   "short, end it with an ellipsis (text-overflow: ellipsis) and put the full text in a title.")
        else:
            sev = Severity.MINOR
            fix = ("The full text cannot be read: allow one more line (line-clamp), let it wrap, or add a "
                   "title or tooltip with the full text.")
        fold.add("text.truncated", it, severity=sev, measured=m,
                 expected={"rule": "text is not cut without a way to read all of it"}, fix=fix)


def _occluded(raw: dict, fold: _Folder, ctx: CheckContext) -> None:
    min_share = rule(ctx, "layout.occluded", "min_share", 0.4)
    for it in raw.get("items", []):
        m = dict(it["measured"])
        if m.get("covered_share", 0) < min_share:
            continue
        cov = it.get("cover") or {}
        fold.add("layout.occluded", it, severity=Severity.MAJOR, measured=m,
                 expected={"covered_share": 0},
                 fix=f"Move {cov.get('sel')} so it does not cover this element, or change the stacking (z-index).",
                 anchor_extra=" | under " + (cov.get("group") or cov.get("sel") or ""))


def _overlap(raw: dict, fold: _Folder, ctx: CheckContext) -> None:
    for it in raw.get("items", []):
        other = it.get("other") or {}
        fold.add("layout.text_overlap", it, severity=Severity.MAJOR, measured=dict(it["measured"]),
                 expected={"overlap_px2": 0},
                 fix="Give the two texts their own space: remove the negative margin or absolute position, or let them wrap.",
                 anchor_extra=" | over " + (other.get("group") or other.get("sel") or ""))


def _contrast_finding(fold: _Folder, it: dict, *, ratio: float, minimum: float, measured: dict,
                      fg: color.RGB | None, bg: color.RGB | None) -> None:
    sev = Severity.MAJOR
    sugg = color.suggest(fg, bg, minimum) if fg is not None and bg is not None else None
    if sugg:
        fix = f"Use a text colour of {sugg} (or further from the background) for {minimum}:1 on {color.to_hex(bg)}."
    else:
        fix = f"Change the text colour or the background to reach {minimum}:1."
    fold.add("contrast.text", it, severity=sev, measured=measured,
             expected={"min_ratio": minimum, "standard": "WCAG 2.x 1.4.3"}, fix=fix)


def _contrast_dom(raw: dict, fold: _Folder, ctx: CheckContext) -> None:
    lo = rule(ctx, "contrast.text", "min_ratio", 4.5)
    lo_large = rule(ctx, "contrast.text", "min_ratio_large", 3.0)
    for it in raw.get("items", []):
        bg = color.flatten(it.get("layers") or [])
        fg = color.over(it["fg"], bg)
        r = round(color.ratio(fg, bg), 2)
        minimum = lo_large if it.get("large") else lo
        if r >= minimum:
            continue
        m = {"ratio": r, "fg": color.to_hex(fg), "bg": color.to_hex(bg), "apca_lc": color.apca_lc(fg, bg),
             "large": bool(it.get("large")), "font": it.get("font"), "route": "dom"}
        _contrast_finding(fold, it, ratio=r, minimum=minimum, measured=m, fg=fg, bg=bg)


async def _scroll(page: Any, x: float, y: float) -> tuple[float, float]:
    return tuple(await page.evaluate(
        "([x, y]) => { window.scrollTo({left: x, top: y, behavior: 'instant'}); return [scrollX, scrollY]; }",
        [x, y]))  # type: ignore[return-value]


async def _contrast_pixels(page: Any, raw: dict, meta: dict, fold: _Folder, ctx: CheckContext) -> list[str]:
    """The pixel route. Returns notes for coverage (what was not judged and why)."""
    jobs = raw.get("jobs") or []
    if not jobs:
        return []
    lo = rule(ctx, "contrast.text", "min_ratio", 4.5)
    lo_large = rule(ctx, "contrast.text", "min_ratio_large", 3.0)
    share = rule(ctx, "contrast.text", "pixel_share", 0.1)
    cap = int(rule(ctx, "contrast.text", "pixel_max", 60))
    mismatch = float(rule(ctx, "contrast.text", "max_css_mismatch", 0.12))
    vw, vh = meta["vw"], meta["vh"]
    for i, j in enumerate(jobs):
        j["id"] = i
        j["min"] = lo_large if j.get("large") else lo
    notes: list[str] = []
    if len(jobs) > cap:
        notes.append(f"{len(jobs) - cap} text elements over images or gradients not judged (over the cap of {cap})")
        jobs = jobs[:cap]
    try:
        x0, y0 = await page.evaluate("() => [scrollX, scrollY]")
    except Exception as e:  # noqa: BLE001 - the page went away: say so in coverage
        return [f"pixel route could not run: {str(e)[:120]}"]
    # Windows: fixed text where it is now, the rest in viewport-high windows down the page.
    windows: list[tuple[float | None, list[dict]]] = []
    fixed = [j for j in jobs if j.get("fixed")]
    if fixed:
        windows.append((None, fixed))
    rest = sorted((j for j in jobs if not j.get("fixed")), key=lambda j: min(q["y"] for q in j["rects"]))
    while rest:
        top = max(0.0, min(q["y"] for q in rest[0]["rects"]) - 16)
        win = [j for j in rest if max(q["y"] + q["h"] for q in j["rects"]) <= top + vh] or [rest[0]]
        rest = [j for j in rest if j not in win]
        windows.append((top, win))
    results: dict[int, dict] = {}
    failed: str | None = None
    pixels_js = script("pixels")
    try:
        for top, win in windows:
            if top is None:
                sx, sy = x0, y0
                if (sx, sy) != tuple(await page.evaluate("() => [scrollX, scrollY]")):
                    sx, sy = await _scroll(page, x0, y0)
                off_x, off_y = 0.0, 0.0
            else:
                sx, sy = await _scroll(page, x0, top)
                off_x, off_y = sx, sy
            rects_v = []
            for j in win:
                rv = []
                for q in j["rects"]:
                    x, y = q["x"] - off_x, q["y"] - off_y
                    x1, y1 = max(0.0, x), max(0.0, y)
                    x2, y2 = min(vw, x + q["w"]), min(vh, y + q["h"])
                    if x2 - x1 >= 1 and y2 - y1 >= 1:
                        rv.append((x1, y1, x2, y2))
                rects_v.append(rv)
            allr = [r for rv in rects_v for r in rv]
            if not allr:
                continue
            cx1, cy1 = int(min(r[0] for r in allr)), int(min(r[1] for r in allr))
            cx2, cy2 = int(max(r[2] for r in allr) + 0.999), int(max(r[3] for r in allr) + 0.999)
            clip = {"x": cx1, "y": cy1, "width": max(1, cx2 - cx1), "height": max(1, cy2 - cy1)}
            shots = {}
            for name, css in (("R", None), ("T", "transparent"), ("K", "#000"), ("W", "#fff")):
                kw: dict[str, Any] = {"clip": clip, "animations": "disabled", "caret": "hide", "scale": "css",
                                      "type": "png"}
                style = _glyph_css(css)
                if style:
                    kw["style"] = style
                shots[name] = base64.b64encode(await page.screenshot(**kw)).decode()
            args = {
                **shots, "clip": {"w": clip["width"], "h": clip["height"]}, "max_mismatch": mismatch,
                "jobs": [{"id": j["id"], "min": j["min"], "fg": j.get("fg"), "opacity": j.get("opacity", 1),
                          "trust_css": bool(j.get("trust_css", True)),
                          "rects": [{"x": a - cx1, "y": b - cy1, "w": c - a, "h": d - b} for a, b, c, d in rv]}
                         for j, rv in zip(win, rects_v, strict=True) if rv],
            }
            for res in await page.evaluate(pixels_js, args):
                results[res["id"]] = res
    except Exception as e:  # noqa: BLE001 - recorded in coverage, never guessed
        failed = str(e).splitlines()[0][:160]
    finally:
        try:
            await _scroll(page, x0, y0)
        except Exception:  # noqa: BLE001,S110 - the page is gone; nothing to restore
            pass
    not_judged: dict[str, int] = {}
    for j in jobs:
        res = results.get(j["id"])
        if not res or not res.get("judged"):
            why = (res or {}).get("why") or (f"pixel route failed: {failed}" if failed else "not on screen")
            not_judged[why] = not_judged.get(why, 0) + 1
            continue
        if res["share_below"] < share:
            continue
        # The ratio is the 10th percentile of the glyph pixels: at least a tenth of the text is that
        # low. One anti-aliased pixel (worst_pixel) overstates a thin stroke over a busy background.
        m = {"ratio": res["p10"], "worst_pixel": res["worst"], "median": res["median"], "share_below": res["share_below"],
             "fg_at_worst": res["fg_at_worst"], "bg_at_worst": res["bg_at_worst"],
             "bg_range": [res["bg_darkest"], res["bg_lightest"]], "large": bool(j.get("large")), "font": j.get("font"),
             "route": "pixels", "why_pixels": j.get("route"), "fg_from": res.get("fg_from"), "css_mismatch": res.get("css_mismatch"),
             "apca_lc": color.apca_lc(_rgb(res["fg_at_worst"]), _rgb(res["bg_at_worst"]))}
        _contrast_finding(fold, j, ratio=res["p10"], minimum=j["min"], measured=m,
                          fg=_rgb(res["fg_at_worst"]), bg=_rgb(res["bg_at_worst"]))
    notes += [f"{n} text element{'s' if n > 1 else ''} not judged: {why}" for why, n in not_judged.items()]
    return notes


def _rgb(h: str) -> color.RGB:
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _target(raw: dict, fold: _Folder, ctx: CheckContext) -> None:
    min_px = rule(ctx, "target.size", "min_px", 24)
    for it in raw.get("items", []):
        fold.add("target.size", it, severity=Severity.MAJOR, measured=dict(it["measured"]),
                 expected={"min_px": min_px, "standard": "WCAG 2.2 2.5.8"},
                 fix=f"Make the target at least {min_px} by {min_px} px (padding or min-width and min-height), "
                     f"or keep {min_px} px clear of its neighbours.")
    for it in raw.get("aaa", []):
        fold.add("target.size", it, severity=Severity.INFO, measured=dict(it["measured"], level="AAA"),
                 expected={"min_px": 44, "standard": "WCAG 2.5.5 (AAA)"},
                 fix="For AAA, make the target at least 44 by 44 px.", anchor_extra=" | aaa")


def _names(raw: dict, fold: _Folder, ctx: CheckContext) -> None:
    for it in raw.get("items", []):
        role = it["measured"].get("role")
        if role == "img":
            fix = 'Add alt text that says what the image shows, or alt="" when it is decorative.'
        elif role in ("textbox", "combobox", "searchbox", "spinbutton", "checkbox", "radio", "slider", "switch"):
            fix = "Give the field a label: a <label for> element, or aria-label."
        else:
            fix = f"Give the {role} a name: visible text, or aria-label for an icon-only {role}."
        fold.add("name.missing", it, severity=Severity.MAJOR, measured=dict(it["measured"]),
                 expected={"name": "not empty"}, fix=fix)


def _images(raw: dict, perf: dict | None, fold: _Folder, ctx: CheckContext) -> list[str]:
    notes = []
    for it in raw.get("items", []):
        p = it.get("problem")
        m = dict(it["measured"], problem=p)
        if p == "no_dimensions":
            fold.add("health.images", it, severity=Severity.MINOR, measured=m, expected={"reserved": "the rendered size"},
                     fix="Add width and height attributes (or CSS aspect-ratio) so the space is kept while it loads.",
                     anchor_extra=" | no_dimensions")
        elif p == "oversized":
            w, h = m["rendered"]
            dpr = m.get("dpr", 1)
            fold.add("health.images", it, severity=Severity.MINOR, measured=m,
                     expected={"natural": f"at most {round(2 * w * dpr)} by {round(2 * h * dpr)} px",
                               "rule": "flagged from 3 times the rendered size at the screen's scale; 2 times is "
                                       "usual for sharp screens"},
                     fix=f"Serve an image near {round(2 * w * dpr)} by {round(2 * h * dpr)} px (srcset and sizes); "
                         f"this one is {m['factor']} times larger than shown.", anchor_extra=" | oversized")
    lcp = (perf or {}).get("lcp")
    if (perf or {}).get("supported", {}).get("lcp"):
        if lcp and lcp.get("lazy"):
            it = {"sel": lcp.get("sel"), "group": lcp.get("group"), "comp": lcp.get("comp"), "role": "img", "name": None}
            fold.add("health.images", it, severity=Severity.MAJOR,
                     measured={"problem": "lazy_largest", "loading": "lazy", "lcp_ms": lcp.get("ms"), "src": lcp.get("url")},
                     expected={"loading": "eager"},
                     fix='Remove loading="lazy" from the largest image on first view (and consider fetchpriority="high").',
                     anchor_extra=" | lazy_largest")
    elif raw.get("lazy_guess"):
        g = raw["lazy_guess"]
        fold.add("health.images", g, severity=Severity.MINOR, measured=dict(g["measured"], problem="lazy_largest"),
                 expected={"loading": "eager"}, confidence=Confidence.HEURISTIC,
                 why_heuristic="this browser does not report the largest paint; the largest image on first view stands in for it",
                 fix='Remove loading="lazy" from the largest image on first view.', anchor_extra=" | lazy_largest")
        notes.append("largest paint not reported by this browser: lazy check is heuristic")
    return notes


# --- health from Python ---------------------------------------------------------------------

_NUM = re.compile(r"\d+")


def _norm_msg(s: str) -> str:
    first = (s or "").strip().splitlines()[0] if (s or "").strip() else ""
    return _NUM.sub("#", first)[:200]


async def _console(page: Any, fold: _Folder) -> list[str]:
    notes = []
    msgs = await page.console_messages()
    errs = await page.page_errors()
    if len(msgs) >= 200 or len(errs) >= 200:
        notes.append("only the last 200 messages are kept by the browser")
    for e in errs:
        text = f"{getattr(e, 'name', '') or 'Error'}: {getattr(e, 'message', '') or str(e)}"
        stack = (getattr(e, "stack", "") or "").splitlines()
        it = {"sel": "html", "group": "uncaught: " + _norm_msg(text), "comp": None, "role": None, "name": text[:120]}
        fold.add("health.console", it, severity=Severity.MAJOR,
                 measured={"kind": "uncaught", "message": text[:300], "at": stack[1].strip()[:200] if len(stack) > 1 else None},
                 expected={"errors": 0}, fix="Fix the uncaught error at its source; it runs when the page loads.")
    for m in msgs:
        if m.type != "error":
            continue
        text = m.text or ""
        if text.startswith("Failed to load resource"):
            continue  # the failed request itself is reported by health.requests or health.fonts
        loc = m.location or {}
        it = {"sel": "html", "group": "console: " + _norm_msg(text), "comp": None, "role": None, "name": text[:120]}
        fold.add("health.console", it, severity=Severity.MINOR,
                 measured={"kind": "console.error", "message": text[:300],
                           "at": f"{loc.get('url', '')}:{loc.get('lineNumber', '')}" if loc.get("url") else None},
                 expected={"errors": 0}, fix="Fix the cause of this console error, or stop logging it as an error.")
    return notes


async def _requests(page: Any) -> list[dict]:
    out = []
    for r in await page.requests():
        url = r.url
        if url.startswith(("data:", "blob:")):
            continue
        failure = r.failure
        status = None
        try:
            # Never waits: a request still in flight has no response yet. A 404 stylesheet is
            # also a failure (net::ERR_ABORTED) with a response, so both are read.
            resp = r.existing_response
            status = resp.status if resp else None
        except Exception:  # noqa: BLE001,S110 - collected request: nothing more to read
            pass
        out.append({"url": url, "type": r.resource_type, "status": status, "failure": failure})
    return out


def _short_url(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}{p.path}" if p.scheme else url[:160]


def _failed(q: dict) -> bool:
    """4xx or 5xx, or no answer at all. A request the page cancelled itself (net::ERR_ABORTED with
    no response: a navigation, an image whose source changed) is not a fault."""
    if (q["status"] or 0) >= 400:
        return True
    return bool(q["failure"]) and q["status"] is None and "ERR_ABORTED" not in q["failure"]


def _requests_findings(reqs: list[dict], fold: _Folder) -> list[str]:
    notes = []
    if len(reqs) >= 100:
        notes.append("only the last 100 requests are kept by the browser")
    for q in reqs:
        if q["type"] == "font":
            continue
        if not _failed(q):
            continue
        what = f"{q['status']}" if q["status"] else q["failure"]
        url = _short_url(q["url"])
        major = q["type"] in ("document", "stylesheet", "script", "fetch", "xhr")
        it = {"sel": "html", "group": f"{q['type']} {url}", "comp": None, "role": None, "name": url[:120]}
        fold.add("health.requests", it, severity=Severity.MAJOR if major else Severity.MINOR,
                 measured={"url": url, "status": q["status"], "failure": q["failure"], "type": q["type"]},
                 expected={"status": "2xx or 3xx"},
                 fix=f"Fix or remove the {q['type']} request to {url} ({what}).")
    return notes


def _fonts(raw: dict, reqs: list[dict], fold: _Folder) -> list[str]:
    notes = []
    failed_urls = [_short_url(q["url"]) for q in reqs if q["type"] == "font" and _failed(q)]
    for f in raw.get("items", []):
        it = {"sel": "html", "group": f"font {f['family']} {f.get('weight')} {f.get('style')}", "comp": None,
              "role": None, "name": f["family"]}
        fold.add("health.fonts", it, severity=Severity.MAJOR,
                 measured={"family": f["family"], "weight": f.get("weight"), "style": f.get("style"),
                           "status": "error", "failed_urls": failed_urls[:5]},
                 expected={"status": "loaded"},
                 fix=f"Fix the font file for {f['family']} (its URL, format or CORS headers); text falls back to another font.")
    if failed_urls and not raw.get("items"):
        for u in failed_urls:
            it = {"sel": "html", "group": f"font {u}", "comp": None, "role": None, "name": u}
            fold.add("health.fonts", it, severity=Severity.MAJOR, measured={"url": u, "status": "error"},
                     expected={"status": "loaded"}, fix=f"Fix or remove the font request to {u}.")
    if raw.get("status") == "loading":
        notes.append("fonts were still loading when checked")
    return notes


def _layout_shift(perf: dict, fold: _Folder, ctx: CheckContext) -> None:
    ls = perf.get("layout_shift") or {}
    limit = rule(ctx, "health.layout_shift", "max_cls", 0.1)
    cls = ls.get("cls", 0)
    if cls <= limit:
        return
    src = (ls.get("sources") or [{}])[0]
    it = {"sel": src.get("sel") or "html", "group": "page", "comp": src.get("comp"), "role": None, "name": None}
    measured = {"cls": cls, "shifts": ls.get("shifts"), "sources": ls.get("sources")}
    if (ls.get("font_swap_share") or 0) >= 0.5:
        # The shift came right after a web font arrived: the font swapping in. Real for a visitor on
        # a cold cache, absent on a warm one, so it comes and goes between runs: said, never hidden.
        measured["cause"] = "a web font swapping in"
        fold.add("health.layout_shift", it, severity=Severity.MAJOR if cls > 0.25 else Severity.MINOR, measured=measured,
                 expected={"max_cls": limit}, confidence=Confidence.HEURISTIC,
                 why_heuristic=(f"{round(ls['font_swap_share'] * 100)}% of the shift came while web fonts loaded; it "
                                "depends on the font cache, so another run may not see it, and it may also be the "
                                "page's own work in that time"),
                 fix="Stop the swap from moving text: font-display: optional, or a fallback font sized to match "
                     "(size-adjust, ascent-override), or preload the font.")
        return
    fold.add("health.layout_shift", it, severity=Severity.MAJOR if cls > 0.25 else Severity.MINOR,
             measured=measured,
             expected={"max_cls": limit},
             fix=f"Reserve space for {src.get('sel') or 'the element that moves'} before it loads or appears "
                 "(a fixed size, aspect-ratio, or a placeholder).")


def _long_tasks(perf: dict, fold: _Folder, ctx: CheckContext) -> None:
    lt = perf.get("long_tasks") or {}
    limit = rule(ctx, "health.long_tasks", "max_blocking_ms", 300)
    blocking = lt.get("blocking_ms", 0)
    if blocking <= limit:
        return
    top = (lt.get("scripts") or [{}])[0]
    where = top.get("source") or top.get("invoker") or "a script on the page"
    it = {"sel": "html", "group": "page", "comp": None, "role": None, "name": None}
    fold.add("health.long_tasks", it, severity=Severity.MAJOR if blocking > 2 * limit else Severity.MINOR,
             measured=dict(lt), expected={"max_blocking_ms": limit},
             fix=f"Split or defer the long work in {where}; it blocks input for {lt.get('longest_ms')} ms at a time.")


MARK_CAPTURE = "() => { window.__agentosStudioCaptureAt = performance.now(); }"


async def mark_capture(page: Any) -> None:
    """Call before studio's own screenshot of a page: layout shifts and long tasks from then on
    (the full-page screenshot resizes the viewport) are studio's, not the page's."""
    try:
        await page.evaluate(MARK_CAPTURE)
    except Exception:  # noqa: BLE001,S110 - a page that cannot be marked is measured as it is
        pass


# --- theme.leak -----------------------------------------------------------------------------


async def theme_snapshot(page: Any, max_elements: int = 4000, ignore: list[str] | None = None) -> dict:
    """The page's colour custom properties and the painted colours of its visible elements, without
    pictures and the regions left out on purpose."""
    return await page.evaluate(script("theme"), {"max_elements": max_elements, "ignore": ignore or []})


async def _theme_leak(page: Any, other: Any, ctx: CheckContext, fold_for: Callable[[str], _Folder],
                      notes: list[str]) -> str | None:
    ign = ignore_selectors(ctx)
    mine = await theme_snapshot(page, ignore=ign)
    theirs = await theme_snapshot(other, ignore=ign)
    if ctx.screen.theme == Theme.DARK:
        light, dark, dark_page = theirs, mine, page
    else:
        light, dark, dark_page = mine, theirs, other
    kept: list[str] = []
    leaks, why = theme.compare(light, dark, kept)
    if why:
        return why
    left_out = light.get("left_out") or 0
    if left_out:
        notes.append(f"{left_out} elements in pictures or regions left out on purpose not compared")
    if kept:
        notes.append(f"{len(kept)} fixed colour{'s' if len(kept) != 1 else ''} kept on purpose: text fixed together "
                     "with the surface under it, or a small mark (initials on an avatar)")
    if not leaks:
        return None
    described = (await dark_page.evaluate(script("theme"), {"describe": sorted({lk.path for lk in leaks})}))["described"]
    dark_key = Screen(**{**ctx.screen.model_dump(), "theme": Theme.DARK}).key
    fold = fold_for(dark_key)
    names = {"bg": "background-color", "color": "color", "border": "border-color"}
    for lk in leaks:
        it = described.get(lk.path)
        if not it:
            continue
        prop = names[lk.prop]
        var = lk.vars[0]
        fold.add("theme.leak", it, severity=Severity.MAJOR,
                 measured={"property": prop, "value": theme.rgba_hex(lk.value), "same_in": ["light", "dark"],
                           "copies": list(lk.vars), "var_in_dark": theme.rgba_hex(lk.dark_var_value)},
                 expected={"value_in_dark": theme.rgba_hex(lk.dark_var_value)},
                 fix=f"Use var({var}) for {prop} instead of a fixed colour.", anchor_extra=f" | {prop}")
    return None


# --- the entry point ------------------------------------------------------------------------

TEXT_CHECKS = ("contrast.text", "text.truncated", "layout.occluded", "layout.text_overlap")


def page_scope(url: str) -> str:
    """The page a finding is on, for its id: the path and the hash route (`#/runs/42`), without
    the host (a dev server's port changes) or a query (`?state=empty` is a state, not a page)."""
    p = urlsplit(url)
    route = p.fragment.split("?", 1)[0] if p.fragment.startswith(("/", "!")) else ""
    path = p.path or "/"
    return path + (f"#{route}" if route else "")


def ignore_selectors(ctx: CheckContext) -> list[str]:
    """What the caller and the profile leave out of the checks (rules["ignore"]): CSS selectors
    of regions that are another product or an exhibit. data-studio="ignore" in the page adds more."""
    raw = ctx.rules.get("ignore") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(x).strip()[:200] for x in raw if str(x).strip()][:20]


async def run_checks(page: Any, ctx: CheckContext, *, other_theme: Any | None = None) -> CheckOutput:
    """Run the measured checks on one screen. `page` is a playwright Page already at the screen
    (settled). `other_theme` is, optionally, a page with the same screen in the other theme, for
    theme.leak. Every check gets a coverage line: ran, skipped (with why) or failed."""
    key = ctx.screen.key
    scope = page_scope(getattr(page, "url", "") or "")
    folds: dict[str, _Folder] = {key: _Folder(key, scope)}

    def fold_for(screen_key: str) -> _Folder:
        return folds.setdefault(screen_key, _Folder(screen_key, scope))

    fold = folds[key]
    want = [c for c in IDS if selected(c, ctx.only)]
    status: dict[str, tuple[str, str | None]] = {c: ("skipped", f"not asked for (only={ctx.only})") for c in IDS}
    notes: dict[str, list[str]] = {c: [] for c in IDS}

    def ok(check: str) -> None:
        status[check] = ("ran", None)

    def fail(check: str, why: str) -> None:
        status[check] = ("failed", why[:240])

    in_page = [c for c in want if BY_ID[c].in_page]
    raw: dict = {"checks": {}, "meta": {}}
    if in_page:
        opts = {
            "checks": in_page,
            "max_elements": int(rule(ctx, "page", "max_elements", 4000)),
            "target_min": rule(ctx, "target.size", "min_px", 24),
            "target_aaa": bool(rule(ctx, "target.size", "aaa", False)),
            "occluded_min_share": rule(ctx, "layout.occluded", "min_share", 0.4),
            "oversize": rule(ctx, "health.images", "oversize", 3.0),
            "ignore": ignore_selectors(ctx),
        }
        try:
            raw = await page.evaluate(script("page"), opts)
        except Exception as e:  # noqa: BLE001 - the whole pass failed: every in-page check says so
            why = f"the page script failed: {str(e).splitlines()[0][:160]}"
            for c in in_page:
                fail(c, why)
            in_page = []
    meta = raw.get("meta", {})
    common_notes = []
    if meta.get("capped"):
        common_notes.append(f"checked the first {meta.get('checked')} of {meta.get('elements')} elements")
    if meta.get("modal"):
        common_notes.append(f"a modal is open ({meta['modal']}): only its content was checked")
    if meta.get("shadow_roots"):
        common_notes.append(f"{meta['shadow_roots']} shadow roots not looked inside")
    if meta.get("frames"):
        common_notes.append(f"{meta['frames']} frames not looked inside")
    ign = meta.get("ignored") or {}
    if ign.get("bad"):
        common_notes.append("ignore selectors that are not valid CSS, so nothing was left out for them: "
                            + ", ".join(ign["bad"][:3]))
    if ign.get("count"):
        named = "; ".join(f"{r['sel'][:60]} ({r['how']}{', ' + r['why'] if r.get('why') else ''})"
                          for r in ign.get("regions", [])[:3])
        more = f" and {ign['count'] - 3} more" if ign["count"] > 3 else ""
        share = ign.get("area_share") or 0
        common_notes.append(f"left out on purpose: {ign['count']} region{'s' if ign['count'] != 1 else ''} "
                            f"({named}{more}), {ign.get('text', 0)} text elements and {ign.get('controls', 0)} "
                            f"controls in them, {round(share * 100)}% of the page's area; what they cover is "
                            "still checked")
    hit_notes = []
    if meta.get("windows_capped"):
        hit_notes.append("hit testing covered the first 15 viewport heights of a long page")

    perf: dict | None = None
    if any(c in want for c in ("health.layout_shift", "health.long_tasks", "health.images")):
        try:
            perf = await page.evaluate(script("perf"), {"wait_ms": 60})
        except Exception as e:  # noqa: BLE001 - recorded in coverage
            perf = None
            for c in ("health.layout_shift", "health.long_tasks"):
                if c in want:
                    fail(c, f"performance timeline not read: {str(e)[:160]}")

    handlers: dict[str, Callable[[dict, _Folder, CheckContext], None]] = {
        "layout.page_overflow": _page_overflow, "layout.clipped": _clipped, "text.truncated": _truncated,
        "layout.occluded": _occluded, "layout.text_overlap": _overlap, "contrast.text": _contrast_dom,
        "target.size": _target, "name.missing": _names,
    }
    for c in in_page:
        r = raw["checks"].get(c)
        if r is None:
            fail(c, "the page script returned nothing for this check")
            continue
        if r.get("status") == "failed":
            fail(c, f"the check threw in the page: {r.get('reason')}")
            continue
        try:
            if c in handlers:
                handlers[c](r, fold, ctx)
            elif c == "health.images":
                notes[c] += _images(r, perf, fold, ctx)
            ok(c)
        except Exception as e:  # noqa: BLE001 - one check failing never hides the others
            fail(c, f"{type(e).__name__}: {str(e)[:160]}")
            continue
        notes[c] += common_notes
        if c in TEXT_CHECKS and meta.get("decorative_text"):
            n = meta["decorative_text"]
            notes[c].append(f"{n} text element{'s' if n > 1 else ''} inside pictures (aria-hidden, inert or "
                            "role=img: decorative) not checked")
        if c in TEXT_CHECKS and meta.get("tiny_text"):
            n = meta["tiny_text"]
            notes[c].append(f"{n} text element{'s' if n > 1 else ''} drawn under 4 px tall (a miniature, not text "
                            "to read) not checked")
        if c == "layout.occluded" and r.get("stacked"):
            n = r["stacked"]
            notes[c].append(f"{n} element{'s' if n > 1 else ''} under a layer of the same size stacked on purpose "
                            "(a before and after view) not counted as covered")
        if c in ("layout.occluded", "layout.text_overlap", "contrast.text"):
            notes[c] += hit_notes
        if c == "layout.text_overlap" and r.get("unconfirmed"):
            notes[c].append(f"{r['unconfirmed']} possible overlaps off screen not confirmed")
        if c == "contrast.text":
            for why, n in (r.get("not_judged") or {}).items():
                notes[c].append(f"{n} text element{'s' if n > 1 else ''} not judged: {why}")
            notes[c] += await _contrast_pixels(page, r, meta, fold, ctx)

    reqs: list[dict] | None = None
    if "health.requests" in want or "health.fonts" in want:
        try:
            reqs = await _requests(page)
        except Exception as e:  # noqa: BLE001 - recorded in coverage
            for c in ("health.requests",):
                if c in want:
                    fail(c, f"requests not read: {str(e)[:160]}")
    if "health.requests" in want and reqs is not None:
        notes["health.requests"] += _requests_findings(reqs, fold)
        ok("health.requests")
    if "health.fonts" in want and status["health.fonts"][0] == "ran":
        notes["health.fonts"] += _fonts(raw["checks"].get("health.fonts", {}), reqs or [], fold)
    if "health.console" in want:
        try:
            notes["health.console"] += await _console(page, fold)
            ok("health.console")
        except Exception as e:  # noqa: BLE001 - recorded in coverage
            fail("health.console", f"console not read: {str(e)[:160]}")

    for c, label, fn in (("health.layout_shift", "layout shift", _layout_shift),
                         ("health.long_tasks", "long tasks", _long_tasks)):
        if c not in want or status[c][0] == "failed":
            continue
        if ctx.engine != EngineName.CHROMIUM:
            status[c] = ("skipped", f"{label} cannot be measured on {ctx.engine.value}; only Chromium reports it")
            continue
        sup = (perf or {}).get("supported", {})
        if not sup.get("layout_shift" if c == "health.layout_shift" else "long_tasks"):
            status[c] = ("skipped", f"this browser does not report {label}")
            continue
        fn(perf or {}, fold, ctx)
        ok(c)

    if "theme.leak" in want:
        if other_theme is None:
            status["theme.leak"] = ("skipped", "needs the same screen in the other theme (pass other_theme)")
        else:
            try:
                why = await _theme_leak(page, other_theme, ctx, fold_for, notes["theme.leak"])
                status["theme.leak"] = ("skipped", why) if why else ("ran", None)
            except Exception as e:  # noqa: BLE001 - recorded in coverage
                fail("theme.leak", f"{type(e).__name__}: {str(e)[:160]}")

    coverage = []
    for c in IDS:
        st, why = status[c]
        if st == "ran" and notes[c]:
            why = "; ".join(dict.fromkeys(notes[c]))
        coverage.append(CoverageItem(check=c, screen=key, status=st, reason=why))  # type: ignore[arg-type]
    findings = [f for fo in folds.values() for f in fo.findings()]
    findings.sort(key=lambda f: (-SEV_RANK[f.severity], f.check, f.id))
    return CheckOutput(findings=findings, coverage=coverage)
