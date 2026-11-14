"""Small shared helpers: ids, canonical JSON, hashing, time and durations."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import secrets
from datetime import UTC, date, datetime, time
from enum import Enum
from typing import Any

from pydantic import BaseModel

from agentos.core import clock
from agentos.core.errors import InvalidRequest


def new_id(prefix: str, nbytes: int = 10) -> str:
    """An unguessable id like `gr_k3j4...` (lowercase base32)."""
    raw = base64.b32encode(secrets.token_bytes(nbytes)).decode().rstrip("=").lower()
    return f"{prefix}_{raw}"


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def b64u_decode(text: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]*", text):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _normalize(obj: Any) -> Any:
    if obj is None or isinstance(obj, bool | int | str):
        return obj
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError("NaN and infinity cannot be hashed")
        return obj
    if isinstance(obj, Enum):
        return _normalize(obj.value)
    if isinstance(obj, BaseModel):
        return _normalize(obj.model_dump(mode="json"))
    if isinstance(obj, dict):
        return {str(k): _normalize(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_normalize(v) for v in obj]
    if isinstance(obj, set | frozenset):
        items = [_normalize(v) for v in obj]
        return sorted(items, key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(obj, bytes | bytearray):
        return {"$bytes": b64u(bytes(obj))}
    if isinstance(obj, datetime | date | time):
        return obj.isoformat()
    raise ValueError(f"cannot hash a {type(obj).__name__}")


def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON: sorted keys, no spaces, UTF-8, no NaN. Bytes become {"$bytes": b64}."""
    return json.dumps(
        _normalize(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def content_hash(obj: Any) -> str:
    """`sha256:<hex>` over the canonical JSON of obj."""
    try:
        return "sha256:" + sha256_hex(canonical_json(obj))
    except ValueError as e:
        raise InvalidRequest(f"content cannot be hashed: {e}") from None


def iso(ts: float | None) -> str | None:
    """A timestamp as policy's stores write it: UTC with Z, microseconds when not zero."""
    return clock.iso_or_none(ts, z=True)


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhdw]?)\s*$")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def parse_duration(text: str | int) -> int:
    """`90`, `30m`, `12h`, `7d`, `2w` -> seconds."""
    if isinstance(text, int):
        return text
    m = _DURATION_RE.match(text)
    if not m:
        raise InvalidRequest(f"not a duration (use 30m, 12h, 7d, 2w): {text!r}")
    return int(m.group(1)) * _UNITS[m.group(2)]


def parse_until(text: str, now: float) -> float:
    """An ISO date or datetime (UTC when no zone), or a duration from now."""
    if _DURATION_RE.match(text):
        return now + parse_duration(text)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text.strip()):
            d = date.fromisoformat(text.strip())
            dt = datetime.combine(d, time(23, 59, 59), UTC)
        else:
            dt = datetime.fromisoformat(text.strip())
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
    except ValueError:
        raise InvalidRequest(f"not a date, datetime or duration: {text!r}") from None
    return dt.timestamp()
