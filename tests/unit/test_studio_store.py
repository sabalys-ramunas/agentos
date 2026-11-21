"""The studio artifact store: blobs by content hash, captures, pruning past the cap with pins and
baselines kept, crops and smaller images, and screen sets. No browser."""

from __future__ import annotations

import io
import struct
import sys
import zlib

import pytest

from openajar.core import migrate
from openajar.core.errors import InvalidRequest, NotFound, ServiceUnavailable
from openajar.studio.capture import screens
from openajar.studio.contracts import DEFAULT_WIDTHS, Capture, Rect, Screen, Shot, Target, Theme
from openajar.studio.store import StudioStore, blob_id, iso_now, new_capture_id
from openajar.studio.store import store as store_mod


def tiny_png(seed: int = 0, w: int = 4, h: int = 4) -> bytes:
    """A valid RGB PNG without Pillow; `seed` makes the bytes differ."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + bytes([seed % 256, 0, 0]) * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


@pytest.fixture
def store(tmp_path):
    s = StudioStore(tmp_path)
    yield s
    s.close()


def _capture(store: StudioStore, url: str = "https://app.example/", seed: int = 0, *, pad: int = 0) -> Capture:
    image = store.put_blob(tiny_png(seed) + b"\x00" * pad, "image/png")
    manifest = store.put_blob(b'{"elements": [], "total": 0}', "application/json")
    shot = Shot(screen=Screen(width=360), image=image, manifest=manifest)
    failed = Shot(screen=Screen(width=1280, theme=Theme.DARK), ok=False, error="blank: the page came back blank")
    return store.save(Capture(id=new_capture_id(), target=Target(url=url), shots=[shot, failed], created_at=iso_now()))


def test_a_blob_round_trips_by_its_hash_and_is_kept_once(store, tmp_path):
    data = tiny_png(7)
    bid = store.put_blob(data, "image/png")
    assert bid == blob_id(data) and bid.startswith("sha256:")
    assert store.put_blob(data, "image/png") == bid
    assert store.blob(bid) == data
    files = list((tmp_path / "studio" / "blobs").rglob("*.png"))
    assert len(files) == 1 and files[0].stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "studio").stat().st_mode & 0o777 == 0o700
    assert store.usage()["blobs"] == 1


def test_a_capture_round_trips_with_its_failed_screen(store):
    cap = _capture(store)
    got = store.get(cap.id)
    assert got == cap
    assert [s.ok for s in got.shots] == [True, False] and "blank" in got.shots[1].error
    assert store.shot_image(cap.id, "default@360-light") == store.blob(cap.shots[0].image)
    with pytest.raises(NotFound, match="no image: blank"):
        store.shot_image(cap.id, "default@1280-dark")
    with pytest.raises(NotFound, match="it has: default@360-light, default@1280-dark"):
        store.shot_image(cap.id, "default@768-light")
    [info] = store.captures("https://app.example/")
    assert info.id == cap.id and info.screens == 2 and info.failed == 1
    with pytest.raises(NotFound, match="pruned"):
        store.get("cap_00000000000nothere")


def test_a_damaged_blob_is_refused_not_served(store):
    bid = store.put_blob(tiny_png(3), "image/png")
    path = store._file(bid, "image/png")
    path.write_bytes(tiny_png(4))
    with pytest.raises(NotFound, match="no longer matches"):
        store.blob(bid)


def test_the_store_refuses_what_it_does_not_keep(store):
    with pytest.raises(InvalidRequest):
        store.put_blob(b"x", "text/html")
    with pytest.raises(InvalidRequest):
        store.put_blob(b"", "image/png")
    with pytest.raises(InvalidRequest, match="does not have"):
        store.save(Capture(id=new_capture_id(), target=Target(url="https://a.example/"), created_at=iso_now(),
                           shots=[Shot(screen=Screen(width=360), image="sha256:" + "0" * 64)]))


def test_baselines_are_named_per_url_and_profile(store):
    a = _capture(store, seed=1)
    b = _capture(store, seed=2)
    assert store.baseline("https://app.example/", "shop") is None
    store.set_baseline("https://app.example/", "shop", a.id)
    store.set_baseline("https://app.example/", None, b.id)
    assert store.baseline("https://app.example/", "shop").id == a.id
    assert store.baseline("https://app.example/").id == b.id
    store.set_baseline("https://app.example/", "shop", b.id)
    assert store.baseline("https://app.example/", "shop").id == b.id
    with pytest.raises(InvalidRequest, match="is of https://app.example/"):
        store.set_baseline("https://other.example/", "shop", a.id)


def test_pruning_takes_the_oldest_and_never_a_pin_or_a_baseline(tmp_path):
    store = StudioStore(tmp_path, max_bytes=10**9)
    pinned = _capture(store, seed=1, pad=4000)
    based = _capture(store, seed=2, pad=4000)
    old = _capture(store, seed=3, pad=4000)
    store.pin(pinned.id, "frozen reference")
    store.set_baseline("https://app.example/", "shop", based.id)
    store.max_bytes = 9000   # three images of ~4 kB and one shared manifest: over the cap
    newest = _capture(store, seed=4, pad=4000)
    kept = {c.id for c in store.captures(limit=50)}
    assert kept == {pinned.id, based.id, newest.id}
    assert old.id not in kept
    with pytest.raises(NotFound):
        store.blob(old.shots[0].image)
    # the manifest blob the pruned capture shared with the others stays
    assert store.blob(newest.shots[0].manifest)
    # nothing left to prune but kept ones and the newest: the cap is exceeded, not broken
    assert store.prune(keep=newest.id) == 0 and store.usage()["bytes"] > store.max_bytes
    store.unpin(pinned.id)
    assert store.prune(keep=newest.id) == 1 and {c.id for c in store.captures()} == {based.id, newest.id}
    store.close()


def test_an_unlinked_blob_is_litter_only_after_the_grace(store):
    bid = store.put_blob(tiny_png(9), "image/png")
    import time
    store.prune(now=time.time())
    assert store.blob(bid)
    store.prune(now=time.time() + store_mod.ORPHAN_GRACE_S + 5)
    with pytest.raises(NotFound):
        store.blob(bid)


def test_the_store_is_a_migrated_database_found_by_discover(tmp_path):
    StudioStore(tmp_path).close()
    assert "studio" in [s.package for s in migrate.discover()]
    [st] = migrate.status(tmp_path, [s for s in migrate.discover() if s.package == "studio"])
    assert st.current == st.latest == 1 and not st.pending


# ---- crops and smaller images (Pillow) ----


def _solid(w: int, h: int, box: tuple[int, int, int, int]) -> bytes:
    from PIL import Image

    im = Image.new("RGB", (w, h), (255, 255, 255))
    im.paste((255, 0, 0), box)
    out = io.BytesIO()
    im.save(out, format="PNG")
    return out.getvalue()


def test_a_crop_is_a_new_blob_of_the_area_with_padding(store):
    pil = pytest.importorskip("PIL.Image")
    src = store.put_blob(_solid(200, 100, (50, 20, 90, 40)), "image/png")
    out = store.crop(src, Rect(x=50, y=20, w=40, h=20), pad=0)
    assert out != src
    with pil.open(io.BytesIO(store.blob(out))) as im:
        assert im.size == (40, 20) and im.getpixel((20, 10))[:3] == (255, 0, 0)
    padded = store.crop(src, Rect(x=50, y=20, w=40, h=20), pad=10)
    with pil.open(io.BytesIO(store.blob(padded))) as im:
        assert im.size == (60, 40) and im.getpixel((0, 0))[:3] == (255, 255, 255)
    edge = store.crop(src, Rect(x=190, y=-5, w=40, h=20), pad=8)
    with pil.open(io.BytesIO(store.blob(edge))) as im:
        assert im.size == (18, 23)   # clamped to the image
    with pytest.raises(InvalidRequest, match="outside the image"):
        store.crop(src, Rect(x=500, y=500, w=10, h=10))


def test_a_crop_honours_the_device_scale_and_can_belong_to_a_capture(store):
    pil = pytest.importorskip("PIL.Image")
    cap = _capture(store)
    src = store.put_blob(_solid(400, 200, (100, 40, 180, 80)), "image/png")
    out = store.crop(src, Rect(x=50, y=20, w=40, h=20), pad=0, scale=2.0, capture_id=cap.id)
    with pil.open(io.BytesIO(store.blob(out))) as im:
        assert im.size == (80, 40) and im.getpixel((40, 20))[:3] == (255, 0, 0)
    import time
    store.prune(now=time.time() + store_mod.ORPHAN_GRACE_S + 5)
    assert store.blob(out)   # linked to the capture: not litter
    with pytest.raises(InvalidRequest, match="not an image"):
        store.crop(cap.shots[0].manifest, Rect(x=0, y=0, w=1, h=1))


def test_a_finding_crop_shows_the_page_around_it_with_the_element_outlined(store):
    pil = pytest.importorskip("PIL.Image")
    src = store.put_blob(_solid(800, 600, (400, 300, 416, 316)), "image/png")   # a 16 px checkbox
    out = store.crop(src, Rect(x=400, y=300, w=16, h=16), pad=24, outline=True, min_size=(360, 200))
    with pil.open(io.BytesIO(store.blob(out))) as im:
        assert im.size == (360, 200)                       # context, not a 16 px square
        cx, cy = 180, 100                                   # the element sits in the middle
        assert im.getpixel((cx, cy))[:3] == (255, 0, 0)
        assert im.getpixel((cx - 9, cy))[:3] == store_mod.OUTLINE   # the box just outside it
        assert im.getpixel((5, 5))[:3] == (255, 255, 255)


def test_a_shot_image_can_be_asked_for_small(store):
    pil = pytest.importorskip("PIL.Image")
    image = store.put_blob(_solid(1280, 3000, (0, 0, 10, 10)), "image/png")
    cap = store.save(Capture(id=new_capture_id(), target=Target(url="https://a.example/"), created_at=iso_now(),
                             shots=[Shot(screen=Screen(width=1280), image=image)]))
    with pil.open(io.BytesIO(store.shot_image(cap.id, "default@1280-light", max_width=320))) as im:
        assert im.size == (320, 750)
    with pil.open(io.BytesIO(store.shot_image(cap.id, "default@1280-light", max_width=640, max_height=300))) as im:
        assert im.size == (128, 300)
    assert store.shot_image(cap.id, "default@1280-light", max_width=4000) == store.blob(image)   # never larger


def test_without_pillow_a_crop_says_how_to_add_it(store, monkeypatch):
    monkeypatch.setitem(sys.modules, "PIL", None)
    src = store.put_blob(tiny_png(), "image/png")
    with pytest.raises(ServiceUnavailable, match="uv sync --extra studio"):
        store.crop(src, Rect(x=0, y=0, w=2, h=2))


# ---- screen sets ----


def test_screens_default_to_the_widths_in_both_themes():
    got = screens()
    assert [s.key for s in got] == [f"default@{w}-{t}" for w in DEFAULT_WIDTHS for t in ("light", "dark")]


def test_screens_expand_states_first_and_drop_repeats():
    got = screens(widths=[768, 360, 768], themes=["dark"], states=["default", "menu-open"])
    assert [s.key for s in got] == ["default@768-dark", "default@360-dark", "menu-open@768-dark", "menu-open@360-dark"]


@pytest.mark.parametrize("kw", [{"widths": [100]}, {"themes": ["sepia"]}, {"widths": []}, {"states": [" "]},
                                {"widths": list(range(300, 1300, 10))}])
def test_a_bad_screen_set_says_what_is_allowed(kw):
    with pytest.raises(InvalidRequest):
        screens(**kw)
