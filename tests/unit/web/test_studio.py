"""Checks: studio runs as the app's Checks and Brand pages read them (agentos.api.web.studio).

The runs and captures are kept in a real studio store, made here with Pillow as the diff tests make
them: no browser starts, as none starts behind these routes.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from agentos.studio.contracts import (
    Adherence,
    CheckRun,
    ComponentRef,
    CoverageItem,
    Element,
    Finding,
    Rect,
    Screen,
    SeenAt,
    Severity,
    Shot,
    Target,
    Theme,
    Where,
)
from agentos.studio.runs import RunRecord, Runs, new_run_id
from agentos.studio.store import Manifest, iso_now, new_capture_id, store_for
from agentos.studio.tools import store_home

pil = pytest.importorskip("PIL.Image")

STUDIO = "/api/orgs/local/studio"
URL = "https://shop.harbor.example/"
LIGHT, DARK = "default@400-light", "default@400-dark"


class Studio:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir


def image(color: tuple[int, int, int], box: tuple[int, int, int, int] | None = None) -> bytes:
    from PIL import ImageDraw

    im = pil.new("RGB", (400, 600), (255, 255, 255))
    if box:
        ImageDraw.Draw(im).rectangle(box, fill=color)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def element(key: str, y: float, h: float, component: str | None = None) -> Element:
    return Element(key=key, tag="div", rect=Rect(x=10, y=y, w=200, h=h),
                   component=ComponentRef(name=component, via="attr") if component else None)


@pytest.fixture
def studio(client: TestClient, tmp_path: Path) -> Iterator[Any]:
    svc = Studio(tmp_path / "studio")
    svc.state_dir.mkdir()
    client.app.state.api.runtime.register("studio", svc)
    store = store_for(store_home(svc.state_dir))
    yield svc, store, Runs(svc.state_dir)


def capture(store: Any, heading: float) -> Any:
    shots = []
    for theme in (Theme.LIGHT, Theme.DARK):
        els = [element("main", 0, 600), element("button", 40, heading, "Button"), element("card", 200, 100, "Card"),
               element("card2", 320, 100, "Card")]
        m = Manifest(elements=els, total=len(els), cap=2000, page_width=400, page_height=600)
        shots.append(Shot(screen=Screen(width=400, theme=theme), image=store.put_blob(
            image((37, 99, 235), (10, 40, 210, 40 + int(heading))), "image/png"),
            manifest=store.put_blob(m.model_dump_json().encode(), "application/json")))
    from agentos.studio.contracts import Capture

    return store.save(Capture(id=new_capture_id(), target=Target(url=URL), shots=shots, created_at=iso_now()))


def finding(fid: str, check: str, severity: Severity, component: str | None = None) -> Finding:
    return Finding(id=fid, check=check, severity=severity, where=Where(selector=".buy", name="Buy now"),
                   component=ComponentRef(name=component, via="attr") if component else None,
                   seen_at=[SeenAt(screen=LIGHT)], measured={"ratio": 2.1}, expected={"min_ratio": 4.5},
                   fix="Darken the text.")


def keep_run(runs: Runs, cap: Any, findings: list[Finding], *, kind: str = "check", profile: str | None = None,
             adherence: bool = False) -> RunRecord:
    rid = new_run_id()
    record = RunRecord(
        id=rid, kind=kind, url=URL, profile=profile, capture=cap.id, created_at=iso_now(),
        run=CheckRun(id=rid, findings=findings,
                     coverage=[CoverageItem(check="contrast.text", screen=LIGHT, status="ran"),
                               CoverageItem(check="contrast.text", screen=DARK, status="ran")]),
        adherence={LIGHT: Adherence(by_category={"color": 0.9, "space": 0.7}, hand_rolled=2)} if adherence else {},
        provenance={"token": 30, "derived": 4, "fixed": 3} if adherence else {},
        rects={f.id: {LIGHT: Rect(x=10, y=40, w=200, h=40)} for f in findings})
    return runs.save(record)


def test_without_studio_a_list_says_what_adds_it(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentos.api.web import studio as routes

    monkeypatch.setattr(routes, "service", lambda api: None)
    body = client.get(f"{STUDIO}/runs").json()
    assert body == {"available": False, "reason": body["reason"], "command": "uv sync --extra studio"}
    refused = client.get(f"{STUDIO}/runs/run_abcdefghij")
    assert refused.status_code == 503 and refused.json()["error"]["code"] == "unavailable"
    assert client.get(f"{STUDIO}/profiles/harbor/shop/components").json()["available"] is False


def test_runs_are_listed_with_their_counts_and_share(client: TestClient, studio: Any) -> None:
    _svc, store, runs = studio
    cap = capture(store, 40)
    blocked = keep_run(runs, cap, [finding("f_0000000001", "contrast.text", Severity.BLOCKER, "Button"),
                                   finding("f_0000000002", "target.size", Severity.MINOR)])
    clean = keep_run(runs, capture(store, 60), [], kind="system", profile="harbor/shop", adherence=True)
    body = client.get(f"{STUDIO}/runs").json()
    assert body["available"] is True
    ids = [r["id"] for r in body["runs"]]
    assert set(ids) == {blocked.id, clean.id}
    row = next(r for r in body["runs"] if r["id"] == blocked.id)
    assert row["counts"] == {"blocker": 1, "major": 0, "minor": 1, "info": 0, "total": 2}
    assert row["screens"] == 2 and row["screens_failed"] == 0 and row["system_share"] is None
    sys_row = next(r for r in body["runs"] if r["id"] == clean.id)
    assert sys_row["kind"] == "system" and sys_row["system_share"] == 0.8 and sys_row["profile"] == "harbor/shop"
    only = client.get(f"{STUDIO}/runs", params={"profile": "harbor/shop"}).json()["runs"]
    assert [r["id"] for r in only] == [clean.id]


def test_one_run_has_its_findings_by_component_and_its_screens(client: TestClient, studio: Any) -> None:
    _svc, store, runs = studio
    cap = capture(store, 40)
    record = keep_run(runs, cap, [finding("f_0000000001", "contrast.text", Severity.BLOCKER, "Button"),
                                  finding("f_0000000002", "target.size", Severity.MINOR)])
    body = client.get(f"{STUDIO}/runs/{record.id}").json()
    assert body["verdict"].startswith("Not clean: 2 findings")
    assert [g["component"] for g in body["groups"]] == ["Button", "page"]
    first = body["groups"][0]["findings"][0]
    assert first["id"] == "f_0000000001" and first["where"]["name"] == "Buy now" and first["crop_screens"] == [LIGHT]
    assert set(first["rects"]) == {LIGHT} and set(first["rects"][LIGHT]) == {"x", "y", "w", "h"}
    assert [s["key"] for s in body["screen_list"]] == [LIGHT, DARK]
    assert all(s["image"] for s in body["screen_list"]) and body["images_gone"] is False


def test_images_are_pngs_cached_by_content(client: TestClient, studio: Any) -> None:
    _svc, store, runs = studio
    cap = capture(store, 40)
    record = keep_run(runs, cap, [finding("f_0000000001", "contrast.text", Severity.BLOCKER, "Button")])
    shot = client.get(f"{STUDIO}/captures/{cap.id}/screens/{LIGHT}", params={"max_width": 200})
    assert shot.status_code == 200 and shot.headers["content-type"] == "image/png"
    assert pil.open(io.BytesIO(shot.content)).size[0] == 200
    again = client.get(f"{STUDIO}/captures/{cap.id}/screens/{LIGHT}", params={"max_width": 200},
                       headers={"if-none-match": shot.headers["etag"]})
    assert again.status_code == 304
    crop = client.get(f"{STUDIO}/runs/{record.id}/findings/f_0000000001/crop")
    assert crop.status_code == 200 and crop.content[:4] == b"\x89PNG"
    assert client.get(f"{STUDIO}/runs/{record.id}/findings/f_000000000f/crop").status_code == 404
    assert client.get(f"{STUDIO}/captures/nope/screens/{LIGHT}").status_code == 400


def test_a_baseline_and_a_diff_of_two_stored_captures(client: TestClient, studio: Any) -> None:
    _svc, store, runs = studio
    before, after = capture(store, 40), capture(store, 80)
    keep_run(runs, before, [])
    keep_run(runs, after, [])
    assert client.get(f"{STUDIO}/baseline", params={"url": URL}).json()["capture"] is None
    store.set_baseline(URL, None, before.id)
    base = client.get(f"{STUDIO}/baseline", params={"url": URL}).json()
    assert base["capture"] == before.id and [s["key"] for s in base["screens"]] == [LIGHT, DARK]
    diff = client.get(f"{STUDIO}/diff", params={"before": before.id, "after": after.id})
    assert diff.status_code == 200, diff.text
    body = diff.json()
    assert body["before"] == before.id and body["after"] == after.id
    changed = [s for s in body["screens"] if s["status"] == "changed"]
    assert changed and changed[0]["lines"] and body["verdict"].startswith(f"{len(changed)} of 2 screens changed")
    assert client.get(f"{STUDIO}/diff", params={"before": "x", "after": after.id}).status_code == 400


def test_component_use_and_look_alikes_of_a_profile(client: TestClient, studio: Any) -> None:
    _svc, store, runs = studio
    empty = client.get(f"{STUDIO}/profiles/harbor/shop/components").json()
    assert empty["run"] is None and empty["components"] == [] and empty["look_alikes"] is None
    record = keep_run(runs, capture(store, 40), [finding("f_0000000003", "token.hardcoded", Severity.MINOR, "Card")],
                      kind="system", profile="harbor/shop", adherence=True)
    body = client.get(f"{STUDIO}/profiles/harbor/shop/components").json()
    assert body["run"] == record.id and body["hand_rolled"] == 2
    assert body["look_alikes"] == {"token": 30, "derived": 4, "look_alike": 3}
    uses = {c["name"]: c for c in body["components"]}
    assert uses["Card"]["uses"] == 4 and uses["Card"]["screens"] == 2 and uses["Card"]["findings"] == 1
    assert uses["Button"]["uses"] == 2
    assert [f["id"] for f in body["look_alike_findings"]] == ["f_0000000003"]
