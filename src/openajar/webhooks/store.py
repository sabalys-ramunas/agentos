"""The webhooks' own SQLite file, `$OPENAJAR_HOME/webhooks.db`, and the key that seals what it holds.

A webhook's address and signing secret are sealed (ChaCha20-Poly1305 under a key of this node,
`secrets/webhooks.key`, the same primitive the vault uses), with the webhook's id and field as the
associated data, so a sealed value cannot be moved to another webhook. The secret is needed to sign
every delivery, so it is kept sealed rather than hashed; it is only ever shown once, when made.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openajar.policy.store import DB
from openajar.vault.crypto import DecryptError, ensure_private_dir, read_keyfile, seal, unseal, write_keyfile
from openajar.vault.errors import VaultKeyMissing

log = logging.getLogger("openajar.webhooks")

SCHEMA = """
CREATE TABLE IF NOT EXISTS webhooks (
  id TEXT PRIMARY KEY,
  origin TEXT NOT NULL,
  url_sealed BLOB NOT NULL,
  secret_sealed BLOB NOT NULL,
  events TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
  id TEXT PRIMARY KEY,
  webhook_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  body TEXT NOT NULL,
  state TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_status INTEGER,
  last_error TEXT,
  next_attempt_at REAL,
  created_at REAL NOT NULL,
  delivered_at REAL,
  UNIQUE (webhook_id, event_id)
);
CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries (state, next_attempt_at);
CREATE INDEX IF NOT EXISTS deliveries_hook ON deliveries (webhook_id, created_at);
"""


@dataclass(frozen=True, slots=True)
class Hook:
    id: str
    origin: str
    url: str
    secret: str
    events: tuple[str, ...]
    enabled: bool
    created_at: float


def load_key(home: Path) -> bytes:
    """This node's key for sealing webhook fields. Made on first use, owner-readable only."""
    path = home / "secrets" / "webhooks.key"
    try:
        return read_keyfile(path)
    except VaultKeyMissing:
        ensure_private_dir(path.parent)
        key = secrets.token_bytes(32)
        write_keyfile(path, key)
        return key


class Store:
    def __init__(self, home: Path):
        self.db = DB(home / "webhooks.db", SCHEMA)
        self._key = load_key(home)
        os.chmod(home / "webhooks.db", 0o600)

    def _aad(self, hook_id: str, field: str) -> bytes:
        return f"webhook:{field}:{hook_id}".encode()

    def seal(self, hook_id: str, field: str, value: str) -> bytes:
        return seal(self._key, value.encode(), self._aad(hook_id, field))

    def _open(self, hook_id: str, field: str, blob: bytes) -> str:
        return unseal(self._key, bytes(blob), self._aad(hook_id, field)).decode()

    def add_hook(self, hook_id: str, origin: str, url: str, secret: str, events: list[str], now: float) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO webhooks (id, origin, url_sealed, secret_sealed, events, enabled, created_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?)",
                (hook_id, origin, self.seal(hook_id, "url", url), self.seal(hook_id, "secret", secret),
                 json.dumps(list(events)), now),
            )

    def _hook(self, row: Any) -> Hook | None:
        """A webhook whose sealed fields do not open for it (a row edited by hand, or a field moved to
        another webhook) is not read at all: it is never sent to, and the log says why."""
        try:
            return Hook(
                id=row["id"], origin=row["origin"],
                url=self._open(row["id"], "url", row["url_sealed"]),
                secret=self._open(row["id"], "secret", row["secret_sealed"]),
                events=tuple(json.loads(row["events"])), enabled=bool(row["enabled"]),
                created_at=float(row["created_at"]),
            )
        except DecryptError:
            log.warning("a webhook could not be read: its sealed fields do not belong to it")
            return None

    def hook(self, hook_id: str) -> Hook | None:
        row = self.db.one("SELECT * FROM webhooks WHERE id = ?", (hook_id,))
        return None if row is None else self._hook(row)

    def hooks(self) -> list[Hook]:
        rows = self.db.all("SELECT * FROM webhooks ORDER BY created_at DESC, id")
        return [hook for hook in (self._hook(row) for row in rows) if hook is not None]

    def exists(self, hook_id: str) -> bool:
        return self.db.one("SELECT 1 FROM webhooks WHERE id = ?", (hook_id,)) is not None

    def wanting(self, kind: str) -> list[str]:
        """The ids of the enabled webhooks that asked for `kind`."""
        rows = self.db.all("SELECT id, events FROM webhooks WHERE enabled = 1")
        return [row["id"] for row in rows if kind in json.loads(row["events"])]

    def set_hook(self, hook_id: str, *, enabled: bool | None = None, events: list[str] | None = None) -> None:
        with self.db.tx() as conn:
            if enabled is not None:
                conn.execute("UPDATE webhooks SET enabled = ? WHERE id = ?", (int(enabled), hook_id))
            if events is not None:
                conn.execute("UPDATE webhooks SET events = ? WHERE id = ?", (json.dumps(list(events)), hook_id))

    def set_secret(self, hook_id: str, secret: str) -> None:
        with self.db.tx() as conn:
            conn.execute("UPDATE webhooks SET secret_sealed = ? WHERE id = ?",
                         (self.seal(hook_id, "secret", secret), hook_id))

    def remove_hook(self, hook_id: str) -> bool:
        with self.db.tx() as conn:
            conn.execute("DELETE FROM deliveries WHERE webhook_id = ?", (hook_id,))
            return conn.execute("DELETE FROM webhooks WHERE id = ?", (hook_id,)).rowcount > 0

    def add_delivery(self, delivery_id: str, hook_id: str, event_id: str, kind: str, body: str, now: float) -> bool:
        """One row per (webhook, event): the same event twice is recorded once. True if it was new."""
        with self.db.tx() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO deliveries (id, webhook_id, event_id, kind, body, state, attempts, "
                "next_attempt_at, created_at) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?)",
                (delivery_id, hook_id, event_id, kind, body, now, now),
            )
            return cursor.rowcount > 0

    def delivery(self, delivery_id: str) -> Any | None:
        return self.db.one("SELECT * FROM deliveries WHERE id = ?", (delivery_id,))

    def due(self, now: float, limit: int = 20) -> list[Any]:
        return self.db.all("SELECT * FROM deliveries WHERE state = 'pending' AND next_attempt_at <= ? "
                           "ORDER BY next_attempt_at, created_at LIMIT ?", (now, limit))

    def update_delivery(self, delivery_id: str, **fields: Any) -> None:
        names = ", ".join(f"{name} = ?" for name in fields)
        self.db.execute(f"UPDATE deliveries SET {names} WHERE id = ?", (*fields.values(), delivery_id))

    def recent(self, hook_id: str, limit: int) -> list[Any]:
        return self.db.all("SELECT * FROM deliveries WHERE webhook_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                           (hook_id, limit))

    def last(self, hook_id: str) -> Any | None:
        return self.db.one("SELECT * FROM deliveries WHERE webhook_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                           (hook_id,))

    def close(self) -> None:
        self.db.close()
