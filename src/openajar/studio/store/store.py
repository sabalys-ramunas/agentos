"""The studio artifact store: images and manifests addressed by their content, and the captures
that name them.

`OPENAJAR_HOME/studio/studio.db` (WAL, created and migrated through openajar.core.migrate) indexes
`OPENAJAR_HOME/studio/blobs/<2 hex>/<sha256>.<ext>` (files 600 in 700 directories). A blob id is
`sha256:<hex>` of its bytes, as audit screenshots are named (openajar.audit.captures), so the same
image kept twice is one file and a reader can check the file against its id.

Past a total size cap the oldest captures go first, with the blobs only they used. A pinned
capture (a frozen reference) and a capture named as a baseline are never pruned. A blob written
but not yet linked to a capture (a capture still running, a crop on its own) is kept for
ORPHAN_GRACE_S before it counts as litter.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from openajar.core import clock, migrate
from openajar.core.errors import InvalidRequest, NotFound, ServiceUnavailable
from openajar.studio.contracts import Capture, Rect, Shot
from openajar.studio.store.migrations import DB, MIGRATIONS
from openajar.studio.store.models import CaptureInfo, Manifest

log = logging.getLogger("openajar.studio")

DEFAULT_MAX_BYTES = 1024 * 1024 * 1024   # every blob together; past it the oldest unpinned captures go
MAX_BLOB_BYTES = 64 * 1024 * 1024        # one blob bigger than this is refused
ORPHAN_GRACE_S = 3600                    # an unlinked blob younger than this belongs to work in progress
MEDIA = {"image/png": "png", "image/webp": "webp", "image/jpeg": "jpg", "application/json": "json"}
_ID = re.compile(r"^sha256:([0-9a-f]{64})$")
_CAP = re.compile(r"^cap_[0-9a-z]{8,40}$")
PILLOW_MISSING = ("Pillow is not installed here, so images cannot be cropped or resized. "
                  "Install the studio extra (uv sync --extra studio), then call again.")


def blob_id(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def new_capture_id() -> str:
    """Sortable by time: milliseconds in hex, then randomness."""
    return f"cap_{int(time.time() * 1000):011x}{secrets.token_hex(4)}"


def _iso(ts: float) -> str:
    return clock.iso(ts, timespec="seconds")


def _parse_iso(value: str) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return time.time()


def _pil() -> Any:
    try:
        from PIL import Image
    except ImportError:
        raise ServiceUnavailable(PILLOW_MISSING) from None
    return Image


OUTLINE = (225, 29, 72)     # the box drawn around a finding in its crop


def crop_png(data: bytes, rect: Rect, pad: float = 8, *, scale: float = 1.0, outline: bool = False,
             min_size: tuple[float, float] = (0, 0)) -> bytes:
    """The part of a PNG inside `rect` (CSS px) grown by `pad` on each side, clamped to the image.
    `scale` is the device pixel ratio the image was taken at. `min_size` (CSS px) grows the crop
    around the rect's centre to at least that size, for context; `outline` draws a 2 px box just
    outside the rect, so a small element can be found in its surroundings."""
    image_mod = _pil()
    from PIL import ImageDraw

    with image_mod.open(io.BytesIO(data)) as im:
        x1, y1, x2, y2 = rect.x - pad, rect.y - pad, rect.x + rect.w + pad, rect.y + rect.h + pad
        mw, mh = min_size
        if x2 - x1 < mw:
            grow = (mw - (x2 - x1)) / 2
            x1, x2 = x1 - grow, x2 + grow
        if y2 - y1 < mh:
            grow = (mh - (y2 - y1)) / 2
            y1, y2 = y1 - grow, y2 + grow
        left, top = max(0, int(x1 * scale)), max(0, int(y1 * scale))
        right, bottom = min(im.width, round(x2 * scale)), min(im.height, round(y2 * scale))
        if right <= left or bottom <= top:
            raise InvalidRequest(f"the area {rect.x:g},{rect.y:g} {rect.w:g}x{rect.h:g} is outside the "
                                 f"image ({im.width}x{im.height} px): crop inside it")
        part = im.crop((left, top, right, bottom))
        if outline:
            part = part.convert("RGB")
            w = max(2, round(2 * scale))
            box = (round(rect.x * scale) - left - w, round(rect.y * scale) - top - w,
                   round((rect.x + rect.w) * scale) - left + w - 1, round((rect.y + rect.h) * scale) - top + w - 1)
            ImageDraw.Draw(part).rectangle(box, outline=OUTLINE, width=w)
        out = io.BytesIO()
        part.save(out, format="PNG", optimize=True)
        return out.getvalue()


def resize_png(data: bytes, *, max_width: int | None = None, max_height: int | None = None) -> bytes:
    """The PNG made smaller to fit both limits, keeping its shape. Never made larger."""
    if not max_width and not max_height:
        return data
    if (max_width is not None and max_width < 16) or (max_height is not None and max_height < 16):
        raise InvalidRequest("ask for an image at least 16 px wide and high")
    image_mod = _pil()
    with image_mod.open(io.BytesIO(data)) as im:
        ratio = min((max_width or im.width) / im.width, (max_height or im.height) / im.height, 1.0)
        if ratio >= 1.0:
            return data
        size = (max(1, round(im.width * ratio)), max(1, round(im.height * ratio)))
        out = io.BytesIO()
        im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB").resize(
            size, image_mod.Resampling.LANCZOS).save(out, format="PNG", optimize=True)
        return out.getvalue()


class StudioStore:
    """One per OPENAJAR_HOME. Thread-safe; every method is short and synchronous (call it through
    asyncio.to_thread from the event loop when the blobs are large)."""

    def __init__(self, home: Path, *, max_bytes: int = DEFAULT_MAX_BYTES):
        self.home = Path(home)
        self.path = self.home / DB
        self.root = self.path.parent
        self.blobs_dir = self.root / "blobs"
        self.max_bytes = int(max_bytes)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.root.chmod(0o700)
            migrate.migrate(self.home, [MIGRATIONS], backup=self.path.exists())
            self.path.chmod(0o600)
            self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        except (OSError, sqlite3.Error, migrate.MigrationError) as e:
            raise ServiceUnavailable(f"cannot open the studio store at {self.path}: {e}") from None
        self._lock = threading.RLock()
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _rows(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    # ---- blobs ----

    def _file(self, bid: str, media: str) -> Path:
        m = _ID.match(bid)
        if m is None:
            raise InvalidRequest(f"{bid[:80]!r} is not a blob id: one looks like sha256:<64 hex>")
        return self.blobs_dir / m.group(1)[:2] / f"{m.group(1)}.{MEDIA.get(media, 'bin')}"

    def put_blob(self, data: bytes, media: str) -> str:
        """Keep `data` and return its id. The same bytes again are the same blob."""
        if media not in MEDIA:
            raise InvalidRequest(f"the store keeps {', '.join(MEDIA)}, not {media[:60]}")
        data = bytes(data)
        if not data:
            raise InvalidRequest("an empty blob is not kept")
        if len(data) > MAX_BLOB_BYTES:
            raise InvalidRequest(f"a blob of {len(data)} bytes is over the {MAX_BLOB_BYTES} byte limit")
        bid = blob_id(data)
        path = self._file(bid, media)
        for d in (self.blobs_dir, path.parent):
            d.mkdir(mode=0o700, exist_ok=True)
        if not path.exists():
            tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        with self._tx() as conn:
            # created_at moves to now: a blob written again belongs to the work now in progress
            conn.execute("INSERT INTO blobs (id, media, bytes, created_at) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(id) DO UPDATE SET created_at = excluded.created_at",
                         (bid, media, len(data), time.time()))
        return bid

    def blob_media(self, bid: str) -> str:
        rows = self._rows("SELECT media FROM blobs WHERE id = ?", (bid,))
        if not rows:
            raise NotFound(f"no blob {bid[:80]} in the studio store: it was pruned, or never kept")
        return rows[0]["media"]

    def blob(self, bid: str) -> bytes:
        """The blob's bytes, checked against its id."""
        media = self.blob_media(bid)
        try:
            data = self._file(bid, media).read_bytes()
        except FileNotFoundError:
            raise NotFound(f"the file of blob {bid} is gone from the studio store; capture again") from None
        if blob_id(data) != bid:
            raise NotFound(f"the file of blob {bid} no longer matches its hash; capture again")
        return data

    def crop(self, bid: str, rect: Rect, pad: float = 8, *, scale: float = 1.0,
             capture_id: str | None = None, outline: bool = False, min_size: tuple[float, float] = (0, 0)) -> str:
        """A new PNG blob of `rect` (CSS px) in image blob `bid`, `pad` px around it (see
        crop_png for `outline` and `min_size`). With `capture_id` the crop belongs to that capture
        and goes when it does."""
        if self.blob_media(bid) != "image/png":
            raise InvalidRequest(f"{bid} is not an image; crop a shot's image")
        out = self.put_blob(crop_png(self.blob(bid), rect, pad, scale=scale, outline=outline, min_size=min_size),
                            "image/png")
        if capture_id is not None:
            self.attach(capture_id, out)
        return out

    def attach(self, capture_id: str, bid: str) -> None:
        """Make a kept blob (a crop, a composed image) belong to a capture: it is kept while the
        capture is and goes when it does."""
        self._capture_row(capture_id)
        self.blob_media(bid)
        with self._tx() as conn:
            conn.execute("INSERT OR IGNORE INTO capture_blobs (capture_id, blob_id) VALUES (?, ?)",
                         (capture_id, bid))

    # ---- captures ----

    def save(self, capture: Capture, *, extra_blobs: list[str] | tuple[str, ...] = ()) -> Capture:
        """Index a capture whose blobs are already kept, then prune past the cap (never this one)."""
        if not _CAP.match(capture.id):
            raise InvalidRequest(f"{capture.id[:60]!r} is not a capture id")
        blobs = {b for s in capture.shots for b in (s.image, s.manifest) if b} | set(extra_blobs)
        with self._tx() as conn:
            missing = [b for b in blobs if not conn.execute("SELECT 1 FROM blobs WHERE id = ?", (b,)).fetchone()]
            if missing:
                raise InvalidRequest(f"the capture names blobs the store does not have: {missing[:3]}")
            conn.execute("INSERT INTO captures (id, url, body, pinned, created_at) VALUES (?, ?, ?, NULL, ?)",
                         (capture.id, capture.target.url, capture.model_dump_json(), _parse_iso(capture.created_at)))
            conn.executemany("INSERT OR IGNORE INTO capture_blobs (capture_id, blob_id) VALUES (?, ?)",
                             [(capture.id, b) for b in sorted(blobs)])
        self.prune(keep=capture.id)
        return capture

    def _capture_row(self, capture_id: str) -> sqlite3.Row:
        rows = self._rows("SELECT * FROM captures WHERE id = ?", (capture_id,))
        if not rows:
            raise NotFound(f"no capture {capture_id[:60]}: it was pruned, or the id is wrong. "
                           "List captures, or capture again")
        return rows[0]

    def get(self, capture_id: str) -> Capture:
        return Capture.model_validate_json(self._capture_row(capture_id)["body"])

    def captures(self, url: str | None = None, *, limit: int = 20) -> list[CaptureInfo]:
        """Newest first."""
        limit = max(1, min(int(limit), 200))
        rows = (self._rows("SELECT * FROM captures WHERE url = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                           (url, limit)) if url is not None else
                self._rows("SELECT * FROM captures ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)))
        out = []
        for r in rows:
            cap = Capture.model_validate_json(r["body"])
            profiles = [b["profile"] for b in self._rows(
                "SELECT profile FROM baselines WHERE capture_id = ? ORDER BY profile", (r["id"],))]
            out.append(CaptureInfo(id=r["id"], url=r["url"], created_at=cap.created_at, screens=len(cap.shots),
                                   failed=sum(not s.ok for s in cap.shots), pinned=r["pinned"],
                                   baseline_for=profiles))
        return out

    def _shot(self, capture_id: str, screen_key: str) -> Shot:
        cap = self.get(capture_id)
        for s in cap.shots:
            if s.screen.key == screen_key:
                return s
        keys = ", ".join(s.screen.key for s in cap.shots)
        raise NotFound(f"capture {capture_id} has no screen {screen_key[:60]!r}; it has: {keys}")

    def shot_image(self, capture_id: str, screen_key: str, *, max_width: int | None = None,
                   max_height: int | None = None) -> bytes:
        """The PNG of one screen, made smaller to fit `max_width` and `max_height` when given."""
        shot = self._shot(capture_id, screen_key)
        if not shot.ok or not shot.image:
            raise NotFound(f"screen {screen_key} of {capture_id} has no image: {shot.error or 'it was not taken'}")
        return resize_png(self.blob(shot.image), max_width=max_width, max_height=max_height)

    def manifest(self, capture_id: str, screen_key: str) -> Manifest:
        shot = self._shot(capture_id, screen_key)
        if not shot.ok or not shot.manifest:
            raise NotFound(f"screen {screen_key} of {capture_id} has no manifest: {shot.error or 'none was built'}")
        return Manifest.model_validate(json.loads(self.blob(shot.manifest)))

    # ---- pins and baselines ----

    def pin(self, capture_id: str, why: str = "pinned") -> None:
        """Keep this capture through every prune (a frozen reference)."""
        self._capture_row(capture_id)
        why = (why or "pinned").strip()[:120] or "pinned"
        with self._tx() as conn:
            conn.execute("UPDATE captures SET pinned = ? WHERE id = ?", (why, capture_id))

    def unpin(self, capture_id: str) -> None:
        """Let this capture be pruned again. A baseline stays kept while it is a baseline."""
        self._capture_row(capture_id)
        with self._tx() as conn:
            conn.execute("UPDATE captures SET pinned = NULL WHERE id = ?", (capture_id,))

    def set_baseline(self, url: str, profile: str | None, capture_id: str, *, by: str | None = None) -> None:
        """Name `capture_id` the baseline for (url, profile). It is kept while it is one."""
        row = self._capture_row(capture_id)
        if row["url"] != url:
            raise InvalidRequest(f"capture {capture_id} is of {row['url'][:200]}, not {url[:200]}")
        with self._tx() as conn:
            conn.execute("INSERT INTO baselines (url, profile, capture_id, set_by, set_at) VALUES (?, ?, ?, ?, ?) "
                         "ON CONFLICT(url, profile) DO UPDATE SET capture_id = excluded.capture_id, "
                         "set_by = excluded.set_by, set_at = excluded.set_at",
                         (url, profile or "", capture_id, by, time.time()))

    def baseline(self, url: str, profile: str | None = None) -> Capture | None:
        rows = self._rows("SELECT capture_id FROM baselines WHERE url = ? AND profile = ?", (url, profile or ""))
        return self.get(rows[0]["capture_id"]) if rows else None

    # ---- size ----

    def usage(self) -> dict[str, int]:
        with self._lock:
            b = self._conn.execute("SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM blobs").fetchone()
            c = self._conn.execute("SELECT COUNT(*) FROM captures").fetchone()
            p = self._conn.execute("SELECT COUNT(*) FROM captures WHERE pinned IS NOT NULL "
                                   "OR id IN (SELECT capture_id FROM baselines)").fetchone()
        return {"blobs": b[0], "bytes": b[1], "captures": c[0], "kept": p[0], "max_bytes": self.max_bytes}

    def prune(self, *, keep: str | None = None, now: float | None = None) -> int:
        """Delete litter (unlinked blobs past the grace), then the oldest unpinned, non-baseline
        captures (never `keep`) until the blobs fit the cap. Returns how many captures went."""
        now = time.time() if now is None else now
        gone_files: list[Path] = []
        removed = 0
        with self._tx() as conn:
            for r in conn.execute("SELECT id, media FROM blobs WHERE created_at < ? AND id NOT IN "
                                  "(SELECT blob_id FROM capture_blobs)", (now - ORPHAN_GRACE_S,)).fetchall():
                conn.execute("DELETE FROM blobs WHERE id = ?", (r["id"],))
                gone_files.append(self._file(r["id"], r["media"]))
            total = conn.execute("SELECT COALESCE(SUM(bytes), 0) FROM blobs").fetchone()[0]
            if total > self.max_bytes:
                candidates = conn.execute(
                    "SELECT id FROM captures WHERE pinned IS NULL AND id != ? AND id NOT IN "
                    "(SELECT capture_id FROM baselines) ORDER BY created_at, id", (keep or "",)).fetchall()
                for c in candidates:
                    if total <= self.max_bytes:
                        break
                    own = conn.execute(
                        "SELECT b.id, b.media, b.bytes FROM capture_blobs cb JOIN blobs b ON b.id = cb.blob_id "
                        "WHERE cb.capture_id = ? AND NOT EXISTS (SELECT 1 FROM capture_blobs o "
                        "WHERE o.blob_id = cb.blob_id AND o.capture_id != cb.capture_id)", (c["id"],)).fetchall()
                    conn.execute("DELETE FROM capture_blobs WHERE capture_id = ?", (c["id"],))
                    conn.execute("DELETE FROM captures WHERE id = ?", (c["id"],))
                    for b in own:
                        conn.execute("DELETE FROM blobs WHERE id = ?", (b["id"],))
                        gone_files.append(self._file(b["id"], b["media"]))
                        total -= b["bytes"]
                    removed += 1
        for p in gone_files:   # after the commit: a crash between leaves a stray file, never a dangling row
            p.unlink(missing_ok=True)
        if removed:
            log.info("studio store: pruned %d captures to fit %d bytes", removed, self.max_bytes)
        return removed


_stores: dict[Path, StudioStore] = {}
_stores_lock = threading.Lock()


def store_for(home: Path) -> StudioStore:
    """The one store of this OPENAJAR_HOME."""
    key = Path(home).resolve()
    with _stores_lock:
        if key not in _stores:
            _stores[key] = StudioStore(key)
        return _stores[key]


def iso_now() -> str:
    return clock.iso(time.time(), timespec="seconds")
