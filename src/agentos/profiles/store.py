"""SQLite storage for profiles: numbered versions, photos, and the copies of photos in lanes.

`AGENTOS_HOME/profiles/profiles.db` (WAL, created and migrated through agentos.core.migrate) and
`AGENTOS_HOME/profiles/photos/<sha256>.<ext>` (600, in a 700 directory). A version is never
changed after it is saved, only its state (draft, approved, superseded, rejected). A photo is
kept once, by the hash of its bytes, so the same picture saved twice is one file. A brand's
files (a logo as SVG, its fonts) are kept the same way, next to the photos.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from agentos.core import clock, migrate
from agentos.core.errors import InvalidRequest, NotFound, ServiceUnavailable
from agentos.core.sqlite import SqliteStore
from agentos.profiles.migrations import DB, MIGRATIONS
from agentos.profiles.models import Profile, ProfileState, ProfileVersion

log = logging.getLogger("agentos.profiles")
MAX_PHOTO_BYTES = 10 * 1024 * 1024
PHOTO_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}
FONT_TYPES = {"font/woff2": "woff2", "font/woff": "woff", "font/ttf": "ttf", "font/otf": "otf"}
SVG = "image/svg+xml"
FILE_TYPES = {**PHOTO_TYPES, SVG: "svg", **FONT_TYPES}
_SVG_ACTIVE = re.compile(rb"<script|<foreignobject|javascript:|\son[a-z]+\s*=|<iframe|<embed|<object", re.IGNORECASE)


def brand_type(data: bytes) -> str | None:
    """A brand file's type from its bytes: an SVG image or a font. None for anything else."""
    head = data[:8]
    if head[:4] == b"wOF2":
        return "font/woff2"
    if head[:4] == b"wOFF":
        return "font/woff"
    if head[:4] in (b"\x00\x01\x00\x00", b"true"):
        return "font/ttf"
    if head[:4] == b"OTTO":
        return "font/otf"
    text = data[:2048].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if text.startswith(b"<svg") or (text.startswith(b"<?xml") and b"<svg" in text):
        return SVG
    return None


class ProfileStore(SqliteStore):
    """The profiles database (agentos.core.sqlite: a connection per thread, WAL). Its schema is
    agentos.profiles.migrations'; photos are files beside it, by content hash."""

    row_factory = sqlite3.Row

    def __init__(self, home: Path):
        self.home = Path(home)
        path = self.home / DB
        self.photos_dir = path.parent / "photos"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.parent.chmod(0o700)
            migrate.migrate(self.home, [MIGRATIONS], backup=path.exists())
            path.chmod(0o600)
            super().__init__(path, timeout=30)
        except (OSError, sqlite3.Error, migrate.MigrationError) as e:
            raise ServiceUnavailable(f"cannot open {path}: {e}") from None

    def _rows(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self._q(sql, args)

    # ---- versions ----
    @staticmethod
    def _version(row: sqlite3.Row) -> ProfileVersion:
        return ProfileVersion(
            definition=Profile.model_validate(json.loads(row["body"])), version=row["version"], hash=row["hash"],
            state=ProfileState(row["state"]), created_at=row["created_at"],
            created_by=json.loads(row["created_by"]) if row["created_by"] else None,
            decided_at=row["decided_at"], decided_by=row["decided_by"], note=row["note"],
            reason=row["reason"])

    def put(self, profile: Profile, *, created_by: dict[str, Any] | None,
            reason: str | None = None) -> tuple[ProfileVersion, bool]:
        """Save `profile` as the next draft version of its id. A profile equal to the approved
        version or a waiting draft returns that version (an old, superseded or rejected one is
        saved again as a new draft). `reason` is why an agent proposes it, for the owner.
        Returns (version, created)."""
        h = profile.content_hash()
        ws, pid = profile.workspace, profile.id
        with self._tx() as conn:
            same = conn.execute("SELECT * FROM profiles WHERE workspace = ? AND id = ? AND hash = ? AND state IN (?, ?) "
                                "ORDER BY version DESC LIMIT 1",
                                (ws, pid, h, ProfileState.DRAFT.value, ProfileState.APPROVED.value)).fetchone()
            if same is not None:
                return self._version(same), False
            n = conn.execute("SELECT COALESCE(MAX(version), 0) FROM profiles WHERE workspace = ? AND id = ?",
                             (ws, pid)).fetchone()[0]
            conn.execute(
                "INSERT INTO profiles (workspace, id, version, hash, body, state, created_at, created_by, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ws, pid, n + 1, h, json.dumps(profile.canonical(), sort_keys=True), ProfileState.DRAFT.value,
                 clock.iso(timespec="seconds"), json.dumps(created_by) if created_by else None, reason))
            row = conn.execute("SELECT * FROM profiles WHERE workspace = ? AND id = ? AND version = ?",
                               (ws, pid, n + 1)).fetchone()
        return self._version(row), True

    def versions(self, workspace: str, profile_id: str) -> list[ProfileVersion]:
        return [self._version(r) for r in self._rows(
            "SELECT * FROM profiles WHERE workspace = ? AND id = ? ORDER BY version", (workspace, profile_id))]

    def version(self, workspace: str, profile_id: str, version: int | None = None) -> ProfileVersion:
        found = self.versions(workspace, profile_id)
        if not found:
            raise NotFound(f"no profile {workspace}/{profile_id} (list them with agentos profile list)")
        if version is None:
            return found[-1]
        for v in found:
            if v.version == version:
                return v
        raise NotFound(f"profile {workspace}/{profile_id} has no version {version}")

    def active(self, workspace: str, profile_id: str) -> ProfileVersion | None:
        rows = self._rows("SELECT * FROM profiles WHERE workspace = ? AND id = ? AND state = ? "
                          "ORDER BY version DESC LIMIT 1", (workspace, profile_id, ProfileState.APPROVED.value))
        return self._version(rows[0]) if rows else None

    def approved(self) -> list[ProfileVersion]:
        """Every approved profile. One whose stored body no longer validates (a later, stricter
        check) is left out and logged, so it cannot stop every other account's lookups."""
        out = []
        for r in self._rows("SELECT * FROM profiles WHERE state = ? ORDER BY workspace, id",
                            (ProfileState.APPROVED.value,)):
            try:
                out.append(self._version(r))
            except ValueError:
                log.warning("profiles: %s/%s version %s no longer validates; skipped", r["workspace"], r["id"],
                            r["version"])
        return out

    def latest(self, workspace: str | None = None) -> list[ProfileVersion]:
        """The latest version of every profile (of one workspace)."""
        sql = ("SELECT p.* FROM profiles p JOIN (SELECT workspace, id, MAX(version) v FROM profiles "
               "GROUP BY workspace, id) m ON p.workspace = m.workspace AND p.id = m.id AND p.version = m.v")
        rows = self._rows(sql + (" WHERE p.workspace = ?" if workspace else "") + " ORDER BY p.workspace, p.id",
                          (workspace,) if workspace else ())
        return [self._version(r) for r in rows]

    def drafts(self, workspace: str | None = None) -> list[ProfileVersion]:
        sql = "SELECT * FROM profiles WHERE state = ?" + (" AND workspace = ?" if workspace else "")
        args = (ProfileState.DRAFT.value, workspace) if workspace else (ProfileState.DRAFT.value,)
        return [self._version(r) for r in self._rows(sql + " ORDER BY workspace, id, version", args)]

    def decide(self, workspace: str, profile_id: str, version: int, state: ProfileState, *, by: str,
               note: str | None = None) -> ProfileVersion:
        """Approve or reject one draft. Approving supersedes the profile's approved version, and
        every older draft stays a draft (the owner may still pick one)."""
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM profiles WHERE workspace = ? AND id = ? AND version = ?",
                               (workspace, profile_id, version)).fetchone()
            if row is None:
                raise NotFound(f"profile {workspace}/{profile_id} has no version {version}")
            if row["state"] != ProfileState.DRAFT.value:
                raise InvalidRequest(f"version {version} of {workspace}/{profile_id} is {row['state']}, not a draft")
            if state is ProfileState.APPROVED:
                conn.execute("UPDATE profiles SET state = ? WHERE workspace = ? AND id = ? AND state = ?",
                             (ProfileState.SUPERSEDED.value, workspace, profile_id, ProfileState.APPROVED.value))
            conn.execute("UPDATE profiles SET state = ?, decided_at = ?, decided_by = ?, note = ? "
                         "WHERE workspace = ? AND id = ? AND version = ?",
                         (state.value, clock.iso(timespec="seconds"), by, note, workspace, profile_id, version))
            row = conn.execute("SELECT * FROM profiles WHERE workspace = ? AND id = ? AND version = ?",
                               (workspace, profile_id, version)).fetchone()
        return self._version(row)

    # ---- photos ----
    def put_photo(self, workspace: str, name: str, data: bytes) -> dict[str, Any]:
        """Keep a photo (JPEG, PNG, WebP, GIF) or a brand file (SVG, WOFF2, WOFF, TTF, OTF). Which
        field may name which type is checked when a profile is saved (ProfilesService._check)."""
        from agentos.node.files import sniff

        if len(data) > MAX_PHOTO_BYTES:
            raise InvalidRequest(f"the file is {len(data)} bytes; at most {MAX_PHOTO_BYTES // (1024 * 1024)} MB")
        ftype = sniff(data[:4096], name)
        mime = ftype.mime if ftype is not None and ftype.mime in PHOTO_TYPES else brand_type(data)
        if mime is None:
            raise InvalidRequest("a profile photo is a JPEG, PNG, WebP or GIF image; a brand file may also be an "
                                 "SVG or a font (WOFF2, WOFF, TTF, OTF)")
        if mime == SVG and _SVG_ACTIVE.search(data):
            raise InvalidRequest("the SVG runs code (a script, an event handler, a javascript: link or embedded "
                                 "HTML); save a plain drawing")
        sha = hashlib.sha256(data).hexdigest()
        self.photos_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.photos_dir / f"{sha}.{FILE_TYPES[mime]}"
        if not path.exists():
            tmp = path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            tmp.replace(path)
        with self._tx() as conn:
            conn.execute("INSERT OR IGNORE INTO photos (sha, workspace, name, type, size, created_at) "
                         "VALUES (?, ?, ?, ?, ?, ?)", (sha, workspace, name[:200], mime, len(data), clock.iso(timespec="seconds")))
        return {"photo": f"sha256:{sha}", "type": mime, "size": len(data), "name": name[:200]}

    def photo(self, ref: str) -> tuple[Path, dict[str, Any]]:
        sha = ref.removeprefix("sha256:")
        rows = self._rows("SELECT * FROM photos WHERE sha = ?", (sha,))
        if not rows:
            raise NotFound(f"no profile photo {ref}")
        meta = dict(rows[0])
        path = self.photos_dir / f"{sha}.{FILE_TYPES[meta['type']]}"
        if not path.is_file() or path.is_symlink():
            raise NotFound(f"profile photo {ref} is missing on disk")
        return path, meta

    # ---- fields changed by hand ----
    def mark(self, surface: str, field: str, value_hash: str, *, by: str | None) -> None:
        with self._tx() as conn:
            conn.execute("INSERT OR REPLACE INTO marks (surface, field, value_hash, by, at) VALUES (?, ?, ?, ?, ?)",
                         (surface, field, value_hash, by, clock.iso(timespec="seconds")))

    def marks(self, surface: str) -> dict[str, dict[str, Any]]:
        return {r["field"]: dict(r) for r in self._rows("SELECT * FROM marks WHERE surface = ?", (surface,))}

    # ---- what the node synced (agentos.profiles.sync, agentos.profiles.learner) ----
    def set_sync(self, surface: str, field: str, status: str, *, value_hash: str | None, run_id: str | None = None,
                 error: str | None = None, relearn: bool = False) -> None:
        with self._tx() as conn:
            row = conn.execute("SELECT relearns FROM sync WHERE surface = ? AND field = ?", (surface, field)).fetchone()
            relearns = json.loads(row["relearns"]) if row is not None else []
            if relearn:
                relearns = [*relearns[-9:], clock.iso(timespec="seconds")]
            conn.execute("INSERT OR REPLACE INTO sync (surface, field, status, value_hash, run_id, error, relearns, at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (surface, field, status, value_hash, run_id, error, json.dumps(relearns), clock.iso(timespec="seconds")))

    def sync(self, surface: str) -> dict[str, dict[str, Any]]:
        out = {}
        for r in self._rows("SELECT * FROM sync WHERE surface = ?", (surface,)):
            d = dict(r)
            d["relearns"] = json.loads(d["relearns"] or "[]")
            out[d["field"]] = d
        return out

    # ---- lessons (agentos.profiles.learner) ----
    def add_lesson(self, *, site: str, field: str, surface: str, lane: str, by: str | None,
                   grant_id: str | None) -> int:
        with self._tx() as conn:
            cur = conn.execute("INSERT INTO lessons (site, field, surface, lane, by, grant_id, status, at) "
                               "VALUES (?, ?, ?, ?, ?, ?, 'started', ?)", (site, field, surface, lane, by, grant_id, clock.iso(timespec="seconds")))
            return int(cur.lastrowid)

    def set_lesson(self, lesson_id: int, status: str, *, error: str | None = None) -> None:
        with self._tx() as conn:
            conn.execute("UPDATE lessons SET status = ?, error = ? WHERE id = ?", (status, error, lesson_id))

    def lessons(self, *, lane: str | None = None, site: str | None = None, field: str | None = None,
                since: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM lessons WHERE 1 = 1", []
        for col, val in (("lane", lane), ("site", site), ("field", field)):
            if val is not None:
                sql += f" AND {col} = ?"
                args.append(val)
        if since is not None:
            sql += " AND at >= ?"
            args.append(since)
        return [dict(r) for r in self._rows(sql + " ORDER BY id", tuple(args))]
