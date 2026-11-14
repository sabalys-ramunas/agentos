"""The sealed store: `AGENTOS_HOME/vault.db` (SQLite, WAL, chmod 600).

Synchronous and connection-per-call, so the daemon (through asyncio.to_thread) and the
CLI can use the same file at once; SQLite's locking serializes writers. Every
operation that needs keys unwraps the data key inside its own transaction, so a key
rotation from another process is never seen half done.

Layout:
  meta(key, value)      schema_version, vault_id, mode, kdf, key_version, wrapped_key
  entries(...)          plaintext metadata + sealed username/secret/totp + metadata MAC
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets as pysecrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentos.vault import crypto
from agentos.vault.errors import (
    EntryExists,
    EntryNotFound,
    VaultAlreadyInitialized,
    VaultBusy,
    VaultCorrupt,
    VaultError,
    VaultNotInitialized,
    VaultTampered,
)
from agentos.vault.models import EntryKind, EntryMeta, NewEntry, validate_config, validate_lanes
from agentos.vault.secret import Secret

SCHEMA_VERSION = 1
SEALED_FIELDS = ("username", "secret", "totp")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entries (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  domain TEXT,
  hosts TEXT NOT NULL DEFAULT '[]',
  lanes TEXT NOT NULL DEFAULT '[]',
  config TEXT NOT NULL DEFAULT '{}',
  username BLOB,
  secret BLOB,
  totp BLOB,
  key_version INTEGER NOT NULL,
  mac TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_used_at TEXT,
  use_count INTEGER NOT NULL DEFAULT 0
);
"""

# Opaque machine secrets (a brain's subscription token, a child grant token): no domain, no lane,
# no sink. Sealed with the same data key as entries, under their own AAD and MAC; added to older
# vaults on first write.
_MACHINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS machine_secrets (
  ref TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  label TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT '{}',
  value BLOB NOT NULL,
  key_version INTEGER NOT NULL,
  mac TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_used_at TEXT,
  last_purpose TEXT,
  use_count INTEGER NOT NULL DEFAULT 0
);
"""


class WrongKey(Exception):
    """The master key does not unwrap the data key. Carries no data."""


@dataclass(frozen=True, repr=False)
class Header:
    vault_id: str
    mode: str                 # "keyfile" | "passphrase"
    kdf: dict | None
    key_version: int
    wrapped: bytes
    created_at: str

    def __repr__(self) -> str:
        return f"Header(vault_id={self.vault_id!r}, mode={self.mode!r}, key_version={self.key_version})"


class _Keys:
    __slots__ = ("enc", "mac", "vault_id", "version")

    def __init__(self, version: int, enc: bytes, mac: bytes, vault_id: str):
        self.version, self.enc, self.mac, self.vault_id = version, enc, mac, vault_id

    def __repr__(self) -> str:
        return f"_Keys(version={self.version})"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _wrap_aad(vault_id: str, version: int) -> bytes:
    return f"agentos-vault|{vault_id}|data-key|{version}".encode()


def _field_aad(vault_id: str, entry_id: str, field: str, version: int) -> bytes:
    return f"agentos-vault|{vault_id}|{entry_id}|{field}|{version}".encode()


def _machine_aad(vault_id: str, ref: str, version: int) -> bytes:
    return f"agentos-vault|{vault_id}|machine|{ref}|{version}".encode()


def _machine_mac_payload(vault_id: str, r: dict[str, Any]) -> bytes:
    body = {"v": 1, "vault": vault_id, "machine": r["ref"], "kind": r["kind"], "label": r["label"],
            "scope": json.loads(r["scope"]), "key_version": r["key_version"], "value": _digest(r["value"])}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _digest(blob: bytes | None) -> str | None:
    return hashlib.sha256(blob).hexdigest() if blob is not None else None


def _mac_payload(vault_id: str, r: dict[str, Any]) -> bytes:
    body = {
        "v": 1, "vault": vault_id, "id": r["id"], "name": r["name"], "kind": r["kind"],
        "domain": r["domain"], "hosts": json.loads(r["hosts"]), "lanes": json.loads(r["lanes"]),
        "config": json.loads(r["config"]), "key_version": r["key_version"],
        "fields": {f: _digest(r[f]) for f in SEALED_FIELDS},
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _map_sqlite(path: Path, e: sqlite3.Error) -> VaultError:
    text = str(e)
    if isinstance(e, sqlite3.OperationalError) and ("locked" in text or "busy" in text):
        return VaultBusy("vault.db is busy (another agentos process is writing); try again")
    if isinstance(e, sqlite3.OperationalError) and ("unable to open" in text or "readonly" in text):
        return VaultError(f"cannot open {path}: {text}")
    return VaultCorrupt(f"{path} is damaged or is not an agentos vault ({text}); restore it from a backup")


class VaultStore:
    def __init__(self, path: Path, *, timeout: float = 5.0):
        self.path = Path(path)
        self.timeout = timeout

    # ---- connections ------------------------------------------------------------

    def exists(self) -> bool:
        return self.path.exists()

    @contextmanager
    def _conn(self, *, write: bool = False, create: bool = False) -> Iterator[sqlite3.Connection]:
        if not self.path.exists():
            if not create:
                raise VaultNotInitialized(f"no vault at {self.path}; run: agentos vault init")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600))
        try:
            conn = sqlite3.connect(str(self.path), timeout=self.timeout, isolation_level=None,
                                   check_same_thread=False)
        except sqlite3.Error as e:
            raise _map_sqlite(self.path, e) from None
        conn.row_factory = sqlite3.Row
        try:
            try:
                conn.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = FULL")
                if create:
                    conn.executescript(_SCHEMA)
                elif not conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
                ).fetchone():
                    raise VaultNotInitialized(f"{self.path} holds no vault; run: agentos vault init")
                conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            except sqlite3.Error as e:
                raise _map_sqlite(self.path, e) from None
            try:
                yield conn
                conn.execute("COMMIT")
            except sqlite3.Error as e:
                self._rollback(conn)
                raise _map_sqlite(self.path, e) from None
            except BaseException:
                self._rollback(conn)
                raise
        finally:
            conn.close()

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def quick_check(self) -> None:
        with self._conn() as c:
            result = c.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise VaultCorrupt(f"{self.path} failed its integrity check; restore it from a backup")

    # ---- header and keys --------------------------------------------------------

    def _header(self, c: sqlite3.Connection) -> Header | None:
        rows = {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM meta")}
        if "wrapped_key" not in rows:
            return None
        try:
            version = int(rows.get("schema_version", "0"))
            if version > SCHEMA_VERSION:
                raise VaultCorrupt(f"{self.path} was written by a newer agentos (schema {version})")
            kdf = json.loads(rows["kdf"]) if rows.get("kdf") else None
            return Header(
                vault_id=rows["vault_id"], mode=rows["mode"], kdf=kdf,
                key_version=int(rows["key_version"]), wrapped=crypto.b64d(rows["wrapped_key"]),
                created_at=rows.get("created_at", ""),
            )
        except (KeyError, ValueError, TypeError):
            raise VaultCorrupt(f"{self.path} has a malformed header; restore it from a backup") from None

    def header(self) -> Header | None:
        if not self.exists():
            return None
        try:
            with self._conn() as c:
                return self._header(c)
        except VaultNotInitialized:
            return None

    def _keys(self, c: sqlite3.Connection, master: bytes) -> _Keys:
        h = self._header(c)
        if h is None:
            raise VaultNotInitialized(f"no vault at {self.path}; run: agentos vault init")
        try:
            dk = crypto.unseal(master, h.wrapped, _wrap_aad(h.vault_id, h.key_version))
        except crypto.DecryptError:
            raise WrongKey() from None
        enc, mk = crypto.subkeys(dk)
        return _Keys(h.key_version, enc, mk, h.vault_id)

    def check_master(self, master: bytes) -> None:
        """Raises WrongKey if `master` does not open this vault."""
        with self._conn() as c:
            self._keys(c, master)

    def initialize(self, *, mode: str, master: bytes, kdf: dict | None) -> Header:
        if mode not in ("keyfile", "passphrase"):
            raise ValueError("mode must be keyfile or passphrase")
        with self._conn(write=True, create=True) as c:
            if self._header(c) is not None:
                raise VaultAlreadyInitialized(f"a vault already exists at {self.path}")
            vault_id = pysecrets.token_hex(8)
            wrapped = crypto.seal(master, os.urandom(crypto.KEY_LEN), _wrap_aad(vault_id, 1))
            values = {
                "schema_version": str(SCHEMA_VERSION), "vault_id": vault_id, "mode": mode,
                "kdf": json.dumps(kdf) if kdf else "", "key_version": "1",
                "wrapped_key": crypto.b64e(wrapped), "created_at": _now(),
            }
            c.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", list(values.items()))
            header = self._header(c)
        os.chmod(self.path, 0o600)
        assert header is not None
        return header

    # ---- rows -------------------------------------------------------------------

    @staticmethod
    def _meta(r: sqlite3.Row | dict) -> EntryMeta:
        return EntryMeta(
            id=r["id"], name=r["name"], kind=EntryKind(r["kind"]), domain=r["domain"],
            hosts=json.loads(r["hosts"]), lanes=json.loads(r["lanes"]), config=json.loads(r["config"]),
            has_username=r["username"] is not None, has_secret=r["secret"] is not None,
            has_totp=r["totp"] is not None, created_at=datetime.fromisoformat(r["created_at"]),
            updated_at=datetime.fromisoformat(r["updated_at"]),
            last_used=datetime.fromisoformat(r["last_used_at"]) if r["last_used_at"] else None,
            use_count=r["use_count"], key_version=r["key_version"],
        )

    def _row(self, c: sqlite3.Connection, ref: str) -> sqlite3.Row:
        row = c.execute("SELECT * FROM entries WHERE id = ? OR name = ? ORDER BY id = ? DESC LIMIT 1",
                        (ref, ref, ref)).fetchone()
        if row is None:
            raise EntryNotFound(f"no vault entry named {ref!r}")
        return row

    def _verify(self, keys: _Keys, row: sqlite3.Row | dict) -> None:
        if row["key_version"] != keys.version:
            raise VaultCorrupt(f"entry {row['name']!r} is sealed under an unknown key version")
        if not crypto.mac_ok(keys.mac, _mac_payload(keys.vault_id, dict(row)), row["mac"]):
            raise VaultTampered(f"entry {row['name']!r} failed its integrity check; refusing to use it")

    def _seal_fields(self, keys: _Keys, entry_id: str, values: dict[str, Secret | None]) -> dict[str, bytes | None]:
        out: dict[str, bytes | None] = {}
        for f in SEALED_FIELDS:
            v = values.get(f)
            out[f] = None if v is None else crypto.seal(
                keys.enc, v.reveal().encode("utf-8"), _field_aad(keys.vault_id, entry_id, f, keys.version))
        return out

    def _open_field(self, keys: _Keys, row: sqlite3.Row | dict, field: str) -> Secret | None:
        blob = row[field]
        if blob is None:
            return None
        try:
            raw = crypto.unseal(keys.enc, blob, _field_aad(keys.vault_id, row["id"], field, row["key_version"]))
        except crypto.DecryptError:
            raise VaultTampered(f"entry {row['name']!r} has a damaged {field} field") from None
        return Secret(raw.decode("utf-8"))

    def _write_row(self, c: sqlite3.Connection, keys: _Keys, r: dict[str, Any], *, insert: bool) -> None:
        r["key_version"] = keys.version
        r["mac"] = crypto.mac(keys.mac, _mac_payload(keys.vault_id, r))
        cols = ["id", "name", "kind", "domain", "hosts", "lanes", "config", "username", "secret", "totp",
                "key_version", "mac", "created_at", "updated_at", "last_used_at", "use_count"]
        if insert:
            c.execute(f"INSERT INTO entries({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                      [r.get(k) for k in cols])
        else:
            sets = ", ".join(f"{k} = ?" for k in cols[1:])
            c.execute(f"UPDATE entries SET {sets} WHERE id = ?", [r.get(k) for k in cols[1:]] + [r["id"]])

    def _new_row(self, keys: _Keys, e: NewEntry) -> dict[str, Any]:
        entry_id = "v_" + pysecrets.token_hex(6)
        now = _now()
        r: dict[str, Any] = {
            "id": entry_id, "name": e.name, "kind": e.kind.value, "domain": e.domain,
            "hosts": json.dumps(sorted(e.hosts)), "lanes": json.dumps(sorted(e.lanes)),
            "config": json.dumps(e.config, sort_keys=True), "created_at": now, "updated_at": now,
            "last_used_at": None, "use_count": 0,
        }
        r.update(self._seal_fields(keys, entry_id, {"username": e.username, "secret": e.secret, "totp": e.totp}))
        return r

    # ---- public operations ------------------------------------------------------

    def list(self) -> list[EntryMeta]:
        with self._conn() as c:
            return [self._meta(r) for r in c.execute("SELECT * FROM entries ORDER BY name")]

    def count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

    def get(self, ref: str) -> EntryMeta:
        with self._conn() as c:
            return self._meta(self._row(c, ref))

    def add(self, master: bytes, entry: NewEntry, *, replace: bool = False) -> EntryMeta:
        """Insert a validated entry (see models.validate_new_entry)."""
        return self.add_many(master, [entry], on_conflict="replace" if replace else "error")["entries"][0]

    def add_many(self, master: bytes, entries: list[NewEntry], *, on_conflict: str = "error") -> dict[str, Any]:
        """Insert many entries in one transaction (all or nothing).
        on_conflict: error | skip | rename | replace (by name)."""
        if on_conflict not in ("error", "skip", "rename", "replace"):
            raise ValueError("on_conflict must be error, skip, rename or replace")
        result: dict[str, Any] = {"added": 0, "skipped": 0, "renamed": 0, "replaced": 0, "entries": []}
        with self._conn(write=True) as c:
            keys = self._keys(c, master)
            for e in entries:
                name = e.name
                existing = c.execute("SELECT id FROM entries WHERE name = ?", (name,)).fetchone()
                if existing is not None:
                    if on_conflict == "error":
                        raise EntryExists(f"a vault entry named {name!r} already exists")
                    if on_conflict == "skip":
                        result["skipped"] += 1
                        continue
                    if on_conflict == "replace":
                        c.execute("DELETE FROM entries WHERE id = ?", (existing["id"],))
                        result["replaced"] += 1
                    else:
                        n = 2
                        while c.execute("SELECT 1 FROM entries WHERE name = ?", (name,)).fetchone():
                            suffix = f" ({n})"
                            name = e.name[: 80 - len(suffix)] + suffix
                            n += 1
                        result["renamed"] += 1
                r = self._new_row(keys, e)
                r["name"] = name
                try:
                    self._write_row(c, keys, r, insert=True)
                except sqlite3.IntegrityError:
                    raise EntryExists(f"a vault entry named {name!r} already exists") from None
                result["added"] += 1
                result["entries"].append(self._meta(r))
        return result

    def update(self, master: bytes, ref: str, *, lanes: list[str] | None = None,
               config: dict[str, Any] | None = None) -> EntryMeta:
        """Change an entry's lane bindings or config (re-authenticates its metadata)."""
        with self._conn(write=True) as c:
            keys = self._keys(c, master)
            row = self._row(c, ref)
            self._verify(keys, row)
            r = dict(row)
            if lanes is not None:
                r["lanes"] = json.dumps(validate_lanes(lanes))
            if config is not None:
                r["config"] = json.dumps(validate_config(EntryKind(r["kind"]), config), sort_keys=True)
            r["updated_at"] = _now()
            self._write_row(c, keys, r, insert=False)
            return self._meta(r)

    def remove(self, ref: str) -> EntryMeta:
        with self._conn(write=True) as c:
            row = self._row(c, ref)
            c.execute("DELETE FROM entries WHERE id = ?", (row["id"],))
            return self._meta(row)

    def open(self, master: bytes, ref: str, fields: tuple[str, ...] = ()) -> tuple[EntryMeta, dict[str, Secret | None]]:
        """Integrity-checked metadata plus the requested sealed fields."""
        bad = set(fields) - set(SEALED_FIELDS)
        if bad:
            raise ValueError(f"unknown sealed fields: {sorted(bad)}")
        with self._conn() as c:
            keys = self._keys(c, master)
            row = self._row(c, ref)
            self._verify(keys, row)
            return self._meta(row), {f: self._open_field(keys, row, f) for f in fields}

    def open_all(self, master: bytes, fields: tuple[str, ...], *,
                 skip_bad: bool = False) -> list[tuple[EntryMeta, dict[str, Secret | None]]]:
        """Every entry, integrity-checked. With skip_bad, entries that fail their check
        are left out instead of failing the whole call."""
        with self._conn() as c:
            keys = self._keys(c, master)
            out = []
            for row in c.execute("SELECT * FROM entries ORDER BY name"):
                try:
                    self._verify(keys, row)
                    out.append((self._meta(row), {f: self._open_field(keys, row, f) for f in fields}))
                except VaultCorrupt:
                    if not skip_bad:
                        raise
            return out

    def record_use(self, entry_id: str) -> None:
        with self._conn(write=True) as c:
            c.execute("UPDATE entries SET use_count = use_count + 1, last_used_at = ? WHERE id = ?",
                      (_now(), entry_id))

    # ---- machine secrets ----------------------------------------------------------

    @staticmethod
    def _has_machine_table(c: sqlite3.Connection) -> bool:
        return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='machine_secrets'").fetchone() \
            is not None

    @staticmethod
    def _machine_meta(r: sqlite3.Row | dict) -> dict[str, Any]:
        """Everything about a machine secret except its value."""
        return {"ref": r["ref"], "kind": r["kind"], "label": r["label"], "scope": json.loads(r["scope"]),
                "created_at": r["created_at"], "updated_at": r["updated_at"], "last_used_at": r["last_used_at"],
                "last_purpose": r["last_purpose"], "use_count": r["use_count"]}

    def _verify_machine(self, keys: _Keys, row: sqlite3.Row | dict) -> None:
        if row["key_version"] != keys.version:
            raise VaultCorrupt(f"machine secret {row['ref']!r} is sealed under an unknown key version")
        if not crypto.mac_ok(keys.mac, _machine_mac_payload(keys.vault_id, dict(row)), row["mac"]):
            raise VaultTampered(f"machine secret {row['ref']!r} failed its integrity check; refusing to use it")

    def _seal_machine(self, keys: _Keys, r: dict[str, Any], value: bytes) -> None:
        r["value"] = crypto.seal(keys.enc, value, _machine_aad(keys.vault_id, r["ref"], keys.version))
        r["key_version"] = keys.version
        r["mac"] = crypto.mac(keys.mac, _machine_mac_payload(keys.vault_id, r))

    def _open_machine_value(self, keys: _Keys, row: sqlite3.Row | dict) -> bytes:
        try:
            return crypto.unseal(keys.enc, row["value"], _machine_aad(keys.vault_id, row["ref"], row["key_version"]))
        except crypto.DecryptError:
            raise VaultTampered(f"machine secret {row['ref']!r} is damaged") from None

    def put_machine(self, master: bytes, ref: str, value: Secret, *, kind: str, label: str,
                    scope: dict[str, Any]) -> dict[str, Any]:
        """Insert or replace a machine secret (validated by the service)."""
        with self._conn(write=True) as c:
            keys = self._keys(c, master)
            c.execute(_MACHINE_SCHEMA)
            old = c.execute("SELECT created_at FROM machine_secrets WHERE ref = ?", (ref,)).fetchone()
            now = _now()
            r: dict[str, Any] = {"ref": ref, "kind": kind, "label": label, "scope": json.dumps(scope, sort_keys=True),
                                 "created_at": old["created_at"] if old else now, "updated_at": now,
                                 "last_used_at": None, "last_purpose": None, "use_count": 0}
            self._seal_machine(keys, r, value.reveal().encode("utf-8"))
            cols = ["ref", "kind", "label", "scope", "value", "key_version", "mac", "created_at", "updated_at",
                    "last_used_at", "last_purpose", "use_count"]
            c.execute(f"INSERT OR REPLACE INTO machine_secrets({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                      [r[k] for k in cols])
            return self._machine_meta(r)

    def open_machine(self, master: bytes, ref: str, *, purpose: str) -> Secret | None:
        """The value, integrity-checked, with the purpose and time of the read recorded. None when
        there is no such secret."""
        with self._conn(write=True) as c:
            if not self._has_machine_table(c):
                return None
            keys = self._keys(c, master)
            row = c.execute("SELECT * FROM machine_secrets WHERE ref = ?", (ref,)).fetchone()
            if row is None:
                return None
            self._verify_machine(keys, row)
            value = self._open_machine_value(keys, row)
            c.execute("UPDATE machine_secrets SET use_count = use_count + 1, last_used_at = ?, last_purpose = ? "
                      "WHERE ref = ?", (_now(), purpose, ref))
            return Secret(value.decode("utf-8"))

    def machine_values(self, master: bytes) -> list[Secret]:
        """Every machine secret's value, for scrubbing only: not recorded as a read. Damaged ones
        are left out."""
        with self._conn() as c:
            if not self._has_machine_table(c):
                return []
            keys = self._keys(c, master)
            out = []
            for row in c.execute("SELECT * FROM machine_secrets"):
                try:
                    self._verify_machine(keys, row)
                    out.append(Secret(self._open_machine_value(keys, row).decode("utf-8")))
                except VaultCorrupt:
                    continue
            return out

    def has_machine(self, ref: str) -> bool:
        with self._conn() as c:
            return self._has_machine_table(c) and c.execute(
                "SELECT 1 FROM machine_secrets WHERE ref = ?", (ref,)).fetchone() is not None

    def list_machine(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            if not self._has_machine_table(c):
                return []
            return [self._machine_meta(r) for r in c.execute("SELECT * FROM machine_secrets ORDER BY ref")]

    def delete_machine(self, ref: str) -> bool:
        with self._conn(write=True) as c:
            if not self._has_machine_table(c):
                return False
            return c.execute("DELETE FROM machine_secrets WHERE ref = ?", (ref,)).rowcount == 1

    def rotate_data_key(self, master: bytes) -> int:
        """New data key; every field re-sealed and every MAC recomputed, in one transaction.
        Refuses (and changes nothing) if any entry or machine secret fails its integrity check."""
        with self._conn(write=True) as c:
            old = self._keys(c, master)
            h = self._header(c)
            assert h is not None
            rows = [dict(r) for r in c.execute("SELECT * FROM entries")]
            plain = []
            for r in rows:
                self._verify(old, r)
                plain.append((r, {f: self._open_field(old, r, f) for f in SEALED_FIELDS}))
            machine = []
            if self._has_machine_table(c):
                for r in [dict(m) for m in c.execute("SELECT * FROM machine_secrets")]:
                    self._verify_machine(old, r)
                    machine.append((r, self._open_machine_value(old, r)))
            version = h.key_version + 1
            dk = os.urandom(crypto.KEY_LEN)
            enc, mk = crypto.subkeys(dk)
            new = _Keys(version, enc, mk, h.vault_id)
            for r, values in plain:
                r.update(self._seal_fields(new, r["id"], values))
                r["updated_at"] = _now()
                self._write_row(c, new, r, insert=False)
            for r, value in machine:
                self._seal_machine(new, r, value)
                c.execute("UPDATE machine_secrets SET value = ?, key_version = ?, mac = ? WHERE ref = ?",
                          (r["value"], r["key_version"], r["mac"], r["ref"]))
            wrapped = crypto.seal(master, dk, _wrap_aad(h.vault_id, version))
            c.executemany("UPDATE meta SET value = ? WHERE key = ?",
                          [(crypto.b64e(wrapped), "wrapped_key"), (str(version), "key_version")])
            return version

    def rewrap(self, old_master: bytes, new_master: bytes, *, mode: str, kdf: dict | None) -> None:
        """Wrap the data key under a new master key (new passphrase or key file)."""
        if mode not in ("keyfile", "passphrase"):
            raise ValueError("mode must be keyfile or passphrase")
        with self._conn(write=True) as c:
            h = self._header(c)
            if h is None:
                raise VaultNotInitialized(f"no vault at {self.path}; run: agentos vault init")
            try:
                dk = crypto.unseal(old_master, h.wrapped, _wrap_aad(h.vault_id, h.key_version))
            except crypto.DecryptError:
                raise WrongKey() from None
            wrapped = crypto.seal(new_master, dk, _wrap_aad(h.vault_id, h.key_version))
            c.executemany("UPDATE meta SET value = ? WHERE key = ?", [
                (crypto.b64e(wrapped), "wrapped_key"), (mode, "mode"), (json.dumps(kdf) if kdf else "", "kdf"),
            ])
