"""Importers: Apple Passwords CSV, Bitwarden unencrypted JSON, 1Password CSV, generic CSV.

Parsing produces ImportRecords whose sensitive fields are `Secret`s. A plan summary
shows counts and domains only, never usernames, passwords, URLs or notes. Applying a
plan inserts everything in one transaction.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openajar.vault.domains import host_of, registrable_domain
from openajar.vault.errors import EntryInvalid
from openajar.vault.models import (
    NAME_MAX,
    EntryKind,
    NewEntry,
    validate_domain,
    validate_lane_pattern,
    validate_new_entry,
)
from openajar.vault.secret import Secret

MAX_FILE_BYTES = 50 * 1024 * 1024
FORMATS = ("auto", "apple", "bitwarden", "1password", "csv")

_ALIASES = {
    "name": ("title", "name", "item name", "login name"),
    "url": ("url", "website", "login_uri", "login uri", "uri", "urls", "web site", "login url", "hostname",
            "origin_url"),
    "username": ("username", "user name", "login_username", "login username", "login", "email", "user"),
    "password": ("password", "login_password", "login password", "pass"),
    "totp": ("otpauth", "totp", "login_totp", "otp", "one-time password", "one time password", "otp auth", "2fa"),
    "notes": ("notes", "note", "comments", "extra"),
    "type": ("type",),
}


@dataclass(repr=False)
class ImportRecord:
    name: str
    kind: EntryKind
    domain: str | None
    username: Secret | None = None
    secret: Secret | None = None
    totp: Secret | None = None

    def __repr__(self) -> str:
        return f"ImportRecord(name={self.name!r}, kind={self.kind.value!r}, domain={self.domain!r})"


@dataclass
class ImportPlan:
    records: list[ImportRecord] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)   # reason -> count
    dropped_notes: int = 0

    def entries(self, bindings: dict[str, list[str]], *, only_bound: bool = False) -> tuple[list[NewEntry], Counter]:
        """Validated NewEntries with lanes from the binding rules, plus skip reasons."""
        out: list[NewEntry] = []
        skipped: Counter = Counter()
        seen: Counter = Counter()
        for r in self.records:
            lanes = bindings.get(r.domain or "", [])
            if only_bound and not lanes:
                skipped["unbound (--only-bound)"] += 1
                continue
            seen[r.name] += 1
            name = r.name if seen[r.name] == 1 else _suffixed(r.name, seen[r.name])
            try:
                out.append(validate_new_entry(NewEntry(
                    name=name, kind=r.kind, domain=r.domain, lanes=lanes,
                    username=r.username, secret=r.secret, totp=r.totp,
                )))
            except EntryInvalid as e:
                skipped[f"invalid ({e.message})"] += 1
        return out, skipped

    def summary(self, bindings: dict[str, list[str]], *, only_bound: bool = False,
                existing: set[str] | None = None) -> dict[str, Any]:
        entries, invalid = self.entries(bindings, only_bound=only_bound)
        domains: dict[str, dict[str, Any]] = {}
        for e in entries:
            d = domains.setdefault(e.domain or "(none)", {"count": 0, "lanes": bindings.get(e.domain or "", [])})
            d["count"] += 1
        skipped = self.skipped + invalid
        return {
            "importable": len(entries),
            "kinds": dict(Counter(e.kind.value for e in entries)),
            "with_totp": sum(1 for e in entries if e.totp is not None),
            "bound": sum(1 for e in entries if e.lanes),
            "unbound": sum(1 for e in entries if not e.lanes),
            "conflicts": sum(1 for e in entries if existing and e.name in existing),
            "skipped": dict(skipped),
            "notes_not_imported": self.dropped_notes,
            "domains": dict(sorted(domains.items())),
        }


def _suffixed(name: str, n: int) -> str:
    suffix = f" ({n})"
    return name[: NAME_MAX - len(suffix)] + suffix


def parse_bindings(rules: list[str]) -> dict[str, list[str]]:
    """`instagram.com=biz-a/instagram` (repeatable; several lanes per domain allowed)."""
    out: dict[str, list[str]] = {}
    for rule in rules:
        domain, sep, lane = rule.partition("=")
        if not sep or not domain.strip() or not lane.strip():
            raise EntryInvalid(f"binding {rule!r} must look like domain=workspace/lane")
        d = validate_domain(domain.strip())
        out.setdefault(d, [])
        lane_p = validate_lane_pattern(lane.strip())
        if lane_p not in out[d]:
            out[d].append(lane_p)
    return out


def _domain_of(url: str) -> str | None:
    url = (url or "").strip()
    if not url:
        return None
    if "://" in url:
        scheme = url.split("://", 1)[0].lower()
        if scheme not in ("http", "https"):
            return None
    try:
        return registrable_domain(host_of(url))
    except ValueError:
        return None


def _s(value: Any) -> Secret | None:
    if value is None:
        return None
    value = str(value)
    return Secret(value) if value.strip() else None


def _clean_name(name: str, domain: str | None, username: Secret | None = None) -> str:
    """Entry names are public (agents see them), so a username embedded in the title (Apple
    exports titles like "instagram.com (me@example.org)") is taken out."""
    name = " ".join((name or "").split())
    user = username.reveal().strip() if username else ""
    if user and user.lower() in name.lower():
        i = name.lower().index(user.lower())
        name = name[:i] + name[i + len(user):]
        name = re.sub(r"\(\s*\)|\[\s*\]", "", name)
        name = " ".join(name.split()).strip(" -:|,")
    return (name or domain or "imported")[:NAME_MAX]


def _record(plan: ImportPlan, *, name: str, url: str, username: Any, password: Any, totp: Any,
            notes: Any, note_only: bool = False) -> None:
    domain = _domain_of(url)
    pw, seed, note = _s(password), _s(totp), _s(notes)
    if note_only:
        if note is None:
            plan.skipped["empty note"] += 1
            return
        plan.records.append(ImportRecord(_clean_name(name, domain), EntryKind.NOTE, domain, secret=note))
        return
    if pw is None and seed is None:
        plan.skipped["no password or TOTP"] += 1
        return
    if domain is None:
        plan.skipped["no web domain"] += 1
        return
    if note is not None:
        plan.dropped_notes += 1
    user = _s(username)
    if pw is None:
        plan.records.append(ImportRecord(_clean_name(name, domain, user), EntryKind.TOTP, domain, totp=seed))
        return
    plan.records.append(ImportRecord(_clean_name(name, domain, user), EntryKind.PASSWORD, domain,
                                     username=user, secret=pw, totp=seed))


def parse_csv(text: str) -> ImportPlan:
    """Any CSV with a header row: Apple Passwords, 1Password, Chrome, Firefox,
    Bitwarden CSV and hand-made files (columns matched by name, case-insensitive)."""
    csv.field_size_limit(1024 * 1024)
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return ImportPlan()
    except csv.Error:
        raise EntryInvalid("the file is not a readable CSV") from None
    cols = [h.strip().lower() for h in header]
    index: dict[str, int] = {}
    for key, aliases in _ALIASES.items():
        for alias in aliases:
            if alias in cols:
                index[key] = cols.index(alias)
                break
    if "password" not in index and "totp" not in index:
        raise EntryInvalid("the CSV has no password or TOTP column")
    plan = ImportPlan()
    while True:
        try:
            row = next(reader)
        except StopIteration:
            break
        except csv.Error:
            plan.skipped["unreadable row"] += 1
            continue
        if not any(c.strip() for c in row):
            continue

        def get(key: str, row: list[str] = row) -> str:
            i = index.get(key)
            return row[i] if i is not None and i < len(row) else ""

        kind = get("type").strip().lower()
        if kind in ("card", "identity", "3", "4"):
            plan.skipped["card or identity"] += 1
            continue
        _record(plan, name=get("name"), url=get("url"), username=get("username"), password=get("password"),
                totp=get("totp"), notes=get("notes"), note_only=kind in ("note", "securenote", "2"))
    return plan


def parse_bitwarden_json(text: str) -> ImportPlan:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise EntryInvalid("the file is not valid JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise EntryInvalid("not a Bitwarden JSON export (no items list)")
    if data.get("encrypted"):
        raise EntryInvalid("this Bitwarden export is encrypted; export it as unencrypted JSON")
    plan = ImportPlan()
    for item in data["items"]:
        if not isinstance(item, dict):
            plan.skipped["unreadable item"] += 1
            continue
        t = item.get("type")
        if t == 2:
            _record(plan, name=str(item.get("name") or ""), url="", username=None, password=None, totp=None,
                    notes=item.get("notes"), note_only=True)
            continue
        if t != 1 or not isinstance(item.get("login"), dict):
            plan.skipped["card or identity"] += 1
            continue
        login = item["login"]
        uris = [u.get("uri") for u in login.get("uris") or [] if isinstance(u, dict) and u.get("uri")]
        url = next((u for u in uris if _domain_of(u)), uris[0] if uris else "")
        _record(plan, name=str(item.get("name") or ""), url=url, username=login.get("username"),
                password=login.get("password"), totp=login.get("totp"), notes=item.get("notes"))
    return plan


def detect_format(path: Path, text: str) -> str:
    stripped = text.lstrip("﻿ \r\n\t")
    if stripped.startswith("{"):
        return "bitwarden"
    first = stripped.splitlines()[0].lower() if stripped else ""
    if "otpauth" in first and "title" in first and "favorite" not in first:
        return "apple"
    if "favorite" in first or "archived" in first:
        return "1password"
    return "csv"


def read_source(path: Path) -> str:
    path = Path(path)
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise EntryInvalid(f"{path} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise EntryInvalid(f"{path} is not UTF-8 text") from None


def parse_file(path: Path, fmt: str = "auto") -> tuple[str, ImportPlan]:
    if fmt not in FORMATS:
        raise EntryInvalid(f"format must be one of {', '.join(FORMATS)}")
    text = read_source(path)
    if fmt == "auto":
        fmt = detect_format(path, text)
    plan = parse_bitwarden_json(text) if fmt == "bitwarden" else parse_csv(text)
    return fmt, plan


def shred(path: Path) -> None:
    """Overwrite the file with random bytes, fsync, then delete it. Best effort: SSDs,
    copy-on-write filesystems, snapshots and backups may keep old copies."""
    path = Path(path)
    size = path.stat().st_size
    fd = os.open(path, os.O_WRONLY)
    try:
        remaining = size
        while remaining > 0:
            n = min(remaining, 1024 * 1024)
            os.write(fd, os.urandom(n))
            remaining -= n
        os.fsync(fd)
        os.ftruncate(fd, 0)
        os.fsync(fd)
    finally:
        os.close(fd)
    path.unlink()
