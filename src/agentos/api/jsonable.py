"""Turn service results (pydantic models, dataclasses, dicts, datetimes) into JSON data."""

from __future__ import annotations

import inspect
from typing import Any

from pydantic_core import to_jsonable_python


def jsonable(value: Any) -> Any:
    return to_jsonable_python(value, fallback=str)


async def maybe_await(value: Any) -> Any:
    """Services may be sync or async; the API accepts both."""
    if inspect.isawaitable(value):
        return await value
    return value


# Keys that never leave the node, whatever a service returns by mistake.
SENSITIVE_KEYS = frozenset({
    "token", "secret", "password", "passphrase", "private_key", "key_material", "biscuit",
    "api_key", "cookie", "cookies", "totp_seed", "seed", "credential", "credentials",
})


SENSITIVE_SUFFIXES = ("_secret", "_token", "_password", "_passphrase", "_api_key", "_private_key", "_cookie")


def is_sensitive(key: Any) -> bool:
    k = str(key).lower()
    return k in SENSITIVE_KEYS or k.endswith(SENSITIVE_SUFFIXES)


def strip_sensitive(value: Any) -> Any:
    """Drop sensitive keys at any depth (defense in depth for grants, notifier settings, nodes)."""
    if isinstance(value, dict):
        return {k: strip_sensitive(v) for k, v in value.items() if not is_sensitive(k)}
    if isinstance(value, list):
        return [strip_sensitive(v) for v in value]
    return value


def pick(value: dict, keys: tuple[str, ...]) -> dict:
    """Strict whitelist (vault entries: names only)."""
    return {k: value.get(k) for k in keys}
