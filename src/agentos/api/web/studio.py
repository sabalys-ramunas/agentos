"""Checks: what studio found on a page (ui_check and system_check runs), as the app's Checks and Brand pages read it.

    GET /api/orgs/{org}/studio/runs?url=&profile=&limit=           the kept runs, newest first
    GET /api/orgs/{org}/studio/runs/{id}                           one run: findings by component, screens, coverage
    GET /api/orgs/{org}/studio/runs/{id}/findings/{fid}/crop?screen=   a finding's place, outlined (PNG)
    GET /api/orgs/{org}/studio/captures/{id}/screens/{screen}?max_width=  one screen's screenshot (PNG)
    GET /api/orgs/{org}/studio/baseline?url=&profile=              the capture a url is compared with
    GET /api/orgs/{org}/studio/diff?before=&after=                 two stored captures compared, no browser
    GET /api/orgs/{org}/studio/profiles/{ws}/{id}/components       component use and look-alikes of a profile

The node has no HTTP route for studio, so these read studio's own store (agentos.studio.runs and .store) as
finding_get, ui_image and ui_diff read it, and start no browser. Who may: `see`. A run checked with a profile
belongs to that profile's workspace and is shown to whoever reaches it; a run with no profile is the owner's,
since nothing says whose page it was. Without studio, a list answers `available: false` with the command that
adds it, and anything else answers `unavailable` with it.

Page text in a finding (a name, a value, a cause line) is data: returned as text, capped. Images are PNGs of
stored captures, which never change, so they are cached by their content.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections import Counter
from typing import Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute

from agentos.api.app import Api, access_of
from agentos.api.errors import ApiError
from agentos.api.web.base import org_route
from agentos.core.errors import InvalidRequest, NotFound, ServiceUnavailable

SERVICE = "studio"
INSTALL = "uv sync --extra studio"
MISSING = ("Studio is not installed on this computer, so it has no checks. Add it with the command below and "
           "restart agentos, or run `agentos studio up` on a laptop.")
SEVERITIES = ("blocker", "major", "minor", "info")
TEXT_CAP = 200
LINE_CAP = 240
LIST_LIMIT = 200
SHOT_WIDTH = 1000
SHOT_HEIGHT = 4000
CROP_WIDTH = 800
FINDING_VIEW = (360.0, 200.0)
CAPTURE_RE = re.compile(r"^cap_[0-9a-z]{6,40}$")
FINDING_RE = re.compile(r"^f_[0-9a-f]{10}$")
KEY_RE = re.compile(r"^(?P<state>[^@]+)@(?P<width>\d+)-(?P<theme>light|dark)$")
IMAGE_HEADERS = {"x-content-type-options": "nosniff", "cache-control": "private, max-age=86400, immutable",
                 "content-security-policy": "default-src 'none'; sandbox"}
NOT_YOURS = "This check is not in a workspace you can see."
LOOK_ALIKE_CHECKS = ("token.hardcoded",)


def service(api: Api) -> Any | None:
    svc = api.optional(SERVICE)
    return svc if svc is not None and getattr(svc, "state_dir", None) is not None else None


def require(api: Api) -> Any:
    svc = service(api)
    if svc is None:
        raise ServiceUnavailable(f"Studio is not installed on this computer. Add it with `{INSTALL}` and restart "
                                 "agentos.")
    return svc


def missing() -> dict[str, Any]:
    return {"available": False, "reason": MISSING, "command": INSTALL}


def cap(text: Any, n: int = TEXT_CAP) -> str | None:
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def _small(value: Any, depth: int = 0) -> Any:
    """A measured or expected value as the page reads it: text capped, lists and records kept short."""
    if isinstance(value, str):
        return cap(value, TEXT_CAP)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if depth >= 2:
        return cap(str(value), 80)
    if isinstance(value, dict):
        return {str(k)[:60]: _small(v, depth + 1) for k, v in list(value.items())[:12]}
    if isinstance(value, (list, tuple)):
        return [_small(v, depth + 1) for v in list(value)[:12]]
    return cap(str(value), 80)


def runs_of(svc: Any) -> Any:
    from agentos.studio.runs import Runs

    return Runs(svc.state_dir)


def store_of(svc: Any) -> Any:
    from agentos.studio.store import store_for
    from agentos.studio.tools import store_home

    return store_for(store_home(svc.state_dir))


def workspace_of(profile: str | None) -> str | None:
    return profile.split("/", 1)[0] if profile and "/" in profile else None


def sees(request: Request, profile: str | None) -> bool:
    access = access_of(request)
    if access.owner:
        return True
    ws = workspace_of(profile)
    return ws is not None and access.workspace_allowed(ws)


def check_sees(request: Request, profile: str | None, action: str) -> None:
    if not sees(request, profile):
        access_of(request).trace(action, status="denied", summary="studio run outside the caller's workspaces")
        raise ApiError("denied", NOT_YOURS)


def counts(findings: list[Any]) -> dict[str, int]:
    c = Counter(f.severity.value for f in findings)
    out = {s: c.get(s, 0) for s in SEVERITIES}
    out["total"] = len(findings)
    return out


def system_share(record: Any) -> float | None:
    from agentos.studio import shaping

    summary = shaping.adherence_summary(record.adherence, record.provenance)
    shares = list((summary.get("on_tokens") or {}).values())
    return round(sum(shares) / len(shares), 3) if shares else None


def screen_keys(record: Any) -> list[str]:
    return list(dict.fromkeys([*(c.screen for c in record.run.coverage), *record.failed]))


def row(record: Any) -> dict[str, Any]:
    keys = screen_keys(record)
    return {"id": record.id, "kind": "system" if record.kind == "system" else "check", "url": cap(record.url, 2000),
            "profile": record.profile, "created_at": record.created_at, "capture": record.capture,
            "counts": counts(record.run.findings), "system_share": system_share(record), "screens": len(keys),
            "screens_failed": len(record.failed)}


def finding_out(f: Any, record: Any) -> dict[str, Any]:
    return {"id": f.id, "check": f.check, "severity": f.severity.value, "confidence": f.confidence.value,
            "where": {"selector": cap(f.where.selector, 300), "role": cap(f.where.role, 60),
                      "name": cap(f.where.name, TEXT_CAP)},
            "component": cap(f.component.name, 80) if f.component is not None else None,
            "seen_at": [{"screen": s.screen, "count": s.count} for s in f.seen_at],
            "measured": _small(f.measured), "expected": _small(f.expected),
            "fix": cap(f.fix, 400), "why_heuristic": cap(f.why_heuristic, 300),
            "crop_screens": [k for k, r in (record.rects.get(f.id) or {}).items() if r.w > 0 and r.h > 0],
            # its place on each screen in CSS px of the page, so the app outlines it on the whole screenshot
            "rects": {k: {"x": r.x, "y": r.y, "w": r.w, "h": r.h}
                      for k, r in (record.rects.get(f.id) or {}).items() if r.w > 0 and r.h > 0}}


def screen_out(key: str, shot: Any | None, failed: dict[str, str]) -> dict[str, Any]:
    if shot is not None:
        return {"key": key, "width": shot.screen.width, "theme": shot.screen.theme.value, "state": shot.screen.state,
                "ok": bool(shot.ok), "error": cap(shot.error, 300), "image": bool(shot.ok and shot.image)}
    m = KEY_RE.match(key)
    return {"key": key, "width": int(m["width"]) if m else 0, "theme": m["theme"] if m else "light",
            "state": m["state"] if m else key, "ok": key not in failed, "error": cap(failed.get(key), 300),
            "image": False}


def _capture(store: Any, capture_id: str) -> Any | None:
    try:
        return store.get(capture_id)
    except NotFound:
        return None


def png(request: Request, data: bytes) -> Response:
    tag = '"' + hashlib.sha256(data).hexdigest()[:32] + '"'
    headers = {**IMAGE_HEADERS, "etag": tag}
    if request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers=headers)
    return Response(data, media_type="image/png", headers=headers)


def _width(raw: str | None, default: int) -> int:
    if not raw:
        return default
    if not raw.isdigit() or not 100 <= int(raw) <= 2000:
        raise InvalidRequest("max_width is a number from 100 to 2000.")
    return int(raw)


def components_of(store: Any, record: Any) -> list[dict[str, Any]]:
    """Elements per known component on the run's screens, counted from the stored layout manifests."""
    cap_ = _capture(store, record.capture)
    uses: Counter[str] = Counter()
    screens: dict[str, set[str]] = {}
    for shot in cap_.shots if cap_ is not None else []:
        if not shot.ok or not shot.manifest:
            continue
        try:
            manifest = store.manifest(record.capture, shot.screen.key)
        except (NotFound, ValueError):
            continue
        for el in manifest.elements:
            if el.component is not None and el.component.via != "none" and el.component.name:
                name = cap(el.component.name, 80) or ""
                uses[name] += 1
                screens.setdefault(name, set()).add(shot.screen.key)
    found = Counter(cap(f.component.name, 80) for f in record.run.findings if f.component is not None)
    names = sorted(set(uses) | {n for n in found if n}, key=lambda n: (-uses.get(n, 0), n))
    return [{"name": n, "uses": uses.get(n, 0), "screens": len(screens.get(n, ())), "findings": found.get(n, 0)}
            for n in names]


class Handlers:
    """The handlers of the routes below, sharing one Api (and what they keep between calls)."""

    def __init__(self, api: Api) -> None:
        self.api = api

    async def listing(self, request: Request) -> dict[str, Any]:
        svc = service(self.api)
        if svc is None:
            return missing()
        q = request.query_params
        url, profile = q.get("url") or None, q.get("profile") or None
        raw = q.get("limit") or "50"
        if not raw.isdigit():
            raise InvalidRequest("limit is a number.")
        limit = max(1, min(int(raw), LIST_LIMIT))
        records = await asyncio.to_thread(runs_of(svc).records)
        out = []
        for r in records:
            if (url and r.url != url) or (profile and r.profile != profile) or not sees(request, r.profile):
                continue
            out.append(row(r))
            if len(out) >= limit:
                break
        return {"available": True, "runs": out}

    async def run(self, request: Request) -> dict[str, Any]:
        from agentos.studio import shaping

        svc = require(self.api)
        record = await asyncio.to_thread(runs_of(svc).get, request.path_params["id"])
        check_sees(request, record.profile, "studio.run")
        cap_ = await asyncio.to_thread(_capture, store_of(svc), record.capture)
        shots = {s.screen.key: s for s in cap_.shots} if cap_ is not None else {}
        keys = list(dict.fromkeys([*shots, *screen_keys(record)]))
        findings = record.run.findings
        by_id = {f.id: f for f in findings}
        groups = []
        for g in shaping.grouped(findings, limit=None, full=False):
            out: dict[str, Any] = {"component": cap(g["component"], 120),
                                   "findings": [finding_out(by_id[f["id"]], record) for f in g["findings"]]}
            if g.get("grouped_by"):
                out["grouped_by"] = g["grouped_by"]
            groups.append(out)
        cov = shaping.coverage(screens=screen_keys(record), failed=record.failed, items=record.run.coverage,
                               fonts_pinned=bool(cap_.fonts_pinned) if cap_ is not None else True,
                               reports=record.reports, notes=record.notes, extra=[], full=True)
        problems = [cap(p, LINE_CAP) for p in cov.get("problems", [])]
        system = shaping.adherence_summary(record.adherence, record.provenance) or None
        n = len(screen_keys(record))
        return {**row(record),
                "verdict": shaping.verdict(findings, screens=n, rendered=n - len(record.failed),
                                           problems=len(problems), system=None),
                "groups": groups,
                "screen_list": [screen_out(k, shots.get(k), record.failed) for k in keys],
                "problems": problems, "system": system, "images_gone": cap_ is None}

    async def crop(self, request: Request) -> Response:
        from agentos.studio.store import resize_png

        svc = require(self.api)
        record = await asyncio.to_thread(runs_of(svc).get, request.path_params["id"])
        check_sees(request, record.profile, "studio.image")
        fid = request.path_params["fid"]
        if not FINDING_RE.match(fid):
            raise InvalidRequest("That is not a finding id.")
        rects = record.rects.get(fid) or {}
        screen = request.query_params.get("screen") or next(iter(rects), None)
        rect = rects.get(screen) if screen else None
        if rect is None or rect.w <= 0 or rect.h <= 0:
            raise NotFound("This finding has no place on that screen to show.")
        store = store_of(svc)
        cap_ = await asyncio.to_thread(_capture, store, record.capture)
        shot = next((s for s in cap_.shots if s.screen.key == screen), None) if cap_ is not None else None
        if shot is None or not shot.ok or not shot.image:
            raise NotFound("The picture of this screen is no longer kept.")
        blob = await asyncio.to_thread(store.crop, shot.image, rect, 24, scale=shot.screen.scale,
                                       capture_id=record.capture, outline=True, min_size=FINDING_VIEW)
        data = await asyncio.to_thread(resize_png, store.blob(blob), max_width=CROP_WIDTH)
        return png(request, data)

    async def shot(self, request: Request) -> Response:
        svc = require(self.api)
        cid = request.path_params["id"]
        if not CAPTURE_RE.match(cid):
            raise InvalidRequest("That is not a capture id.")
        width = _width(request.query_params.get("max_width"), SHOT_WIDTH)
        runs = runs_of(svc)
        record = await asyncio.to_thread(runs.for_capture, cid)
        check_sees(request, record.profile if record is not None else None, "studio.image")
        store = store_of(svc)
        data = await asyncio.to_thread(store.shot_image, cid, request.path_params["screen"], max_width=width,
                                       max_height=SHOT_HEIGHT)
        return png(request, data)

    async def baseline(self, request: Request) -> dict[str, Any]:
        svc = require(self.api)
        url = (request.query_params.get("url") or "").strip()
        if not url:
            raise InvalidRequest("Name the page: url.")
        profile = request.query_params.get("profile") or None
        check_sees(request, profile, "studio.baseline")
        base = await asyncio.to_thread(store_of(svc).baseline, url, profile)
        if base is None:
            return {"url": url, "profile": profile, "capture": None, "created_at": None, "screens": []}
        return {"url": url, "profile": profile, "capture": base.id, "created_at": base.created_at,
                "screens": [screen_out(s.screen.key, s, {}) for s in base.shots]}

    async def diff(self, request: Request) -> dict[str, Any]:
        svc = require(self.api)
        try:
            from agentos.studio.diff import diff_captures
        except ImportError:
            raise ServiceUnavailable("This build of studio has no diff engine, so two captures cannot be "
                                     "compared here.") from None
        before = request.query_params.get("before") or ""
        after = request.query_params.get("after") or ""
        if not (CAPTURE_RE.match(before) and CAPTURE_RE.match(after)):
            raise InvalidRequest("before and after are capture ids, like cap_k3x9q2m4ab.")
        runs = runs_of(svc)
        for cid in (before, after):
            record = await asyncio.to_thread(runs.for_capture, cid)
            check_sees(request, record.profile if record is not None else None, "studio.diff")
        result = await asyncio.to_thread(diff_captures, store_of(svc), before, after)
        one_side = [s for s in result.screens if s.status == "skipped" and (s.reason or "").startswith("the ")
                    and " capture has no screen " in (s.reason or "")]
        screens = [s for s in result.screens if s not in one_side]
        changed = [s for s in screens if s.status == "changed"]
        causes = sum(len(s.lines) or len(s.changes) for s in changed)
        if changed:
            verdict = f"{len(changed)} of {len(screens)} screens changed: {causes} cause{'s' if causes != 1 else ''}."
        else:
            done = sum(1 for s in screens if s.status != "skipped")
            verdict = f"No change on {done} of {len(screens)} screens."
        out: dict[str, Any] = {
            "before": result.before, "after": result.after, "verdict": verdict,
            "screens": [{"screen": s.screen, "status": s.status,
                         **({"reason": cap(s.reason, 300)} if s.reason else {}),
                         "lines": [cap(line, LINE_CAP) for line in s.lines[:60]],
                         "pixels_changed": round(s.pixel_share, 4),
                         "unexplained": [{"rect": r.rect.model_dump(), "share": round(r.share, 4)}
                                         for r in s.unexplained[:10]],
                         "changes": [{"kind": c.kind, "key": cap(c.key, 200),
                                      "component": cap(c.component.name, 80) if c.component else None}
                                     for c in s.changes[:100]]}
                        for s in screens]}
        if one_side:
            out["not_compared"] = (f"{len(one_side)} screen{'s' if len(one_side) != 1 else ''} only one capture "
                                   "has, so they were not compared.")
        return out

    async def components(self, request: Request) -> dict[str, Any]:
        svc = service(self.api)
        if svc is None:
            return missing()
        ref = f"{request.path_params['ws']}/{request.path_params['id']}"
        check_sees(request, ref, "studio.components")
        records = await asyncio.to_thread(runs_of(svc).records)
        mine = [r for r in records if r.profile == ref]
        record = next((r for r in mine if r.kind == "system"), None) or next((r for r in mine if r.adherence), None)
        if record is None:
            return {"profile": ref, "run": None, "url": None, "checked_at": None, "components": [], "hand_rolled": 0,
                    "look_alikes": None, "look_alike_findings": [], "on_tokens": {}}
        from agentos.studio import shaping

        summary = shaping.adherence_summary(record.adherence, record.provenance)
        used = await asyncio.to_thread(components_of, store_of(svc), record)
        look = [finding_out(f, record) for f in record.run.findings if f.check in LOOK_ALIKE_CHECKS][:50]
        return {"profile": ref, "run": record.id, "url": cap(record.url, 2000), "checked_at": record.created_at,
                "components": used, "hand_rolled": int(summary.get("hand_rolled_controls") or 0),
                "look_alikes": summary.get("probe"), "look_alike_findings": look,
                "on_tokens": summary.get("on_tokens") or {}}


def routes(api: Api) -> list[BaseRoute]:
    h = Handlers(api)

    return [
        org_route(api, "/studio/runs", h.listing, ["GET"], cap="see"),
        org_route(api, "/studio/runs/{id}", h.run, ["GET"], cap="see"),
        org_route(api, "/studio/runs/{id}/findings/{fid}/crop", h.crop, ["GET"], cap="see"),
        org_route(api, "/studio/captures/{id}/screens/{screen}", h.shot, ["GET"], cap="see"),
        org_route(api, "/studio/baseline", h.baseline, ["GET"], cap="see"),
        org_route(api, "/studio/diff", h.diff, ["GET"], cap="see"),
        org_route(api, "/studio/profiles/{ws}/{id}/components", h.components, ["GET"], cap="see"),
    ]
