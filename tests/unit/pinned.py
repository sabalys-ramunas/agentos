"""Characterization pins: a table of inputs, and the exact output the code gave for each, kept in
tests/fixtures/pinned/<name>.json. A refactor of safety code (policy, audit, vault, freeze) must
leave every pinned output as it was, byte for byte: card text, error codes and messages, booleans.

Write the file once, from the code before the change:

    AGENTOS_PIN_WRITE=1 uv run pytest tests/unit/test_pinned_<name>.py

Never rewrite one to make a refactor pass. A deliberate change of behaviour rewrites it in its own
commit, which says why.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

DIR = Path(__file__).resolve().parents[1] / "fixtures" / "pinned"


def outcome(fn: Callable[[], Any]) -> Any:
    """What fn returned, or the error it raised (its type, code and message), as plain JSON."""
    try:
        return {"ok": plain(fn())}
    except Exception as e:  # noqa: BLE001 - the error is the outcome being pinned
        return {"raised": type(e).__name__, "code": getattr(e, "code", None), "message": str(e)}


def plain(value: Any) -> Any:
    """A value as JSON can hold it, keeping what a comparison needs (dataclasses and models by field)."""
    if hasattr(value, "model_dump"):
        return plain(value.model_dump(mode="json"))
    if hasattr(value, "__dataclass_fields__"):
        return {k: plain(getattr(value, k)) for k in value.__dataclass_fields__}
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(plain(v) for v in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "value") and type(value).__module__.startswith("agentos"):  # an Enum
        return value.value
    return repr(value)


def check(name: str, results: dict[str, Any]) -> None:
    """Compare every case's outcome with the pinned one, keys in the same order (or write them, with
    AGENTOS_PIN_WRITE=1)."""
    path = DIR / f"{name}.json"
    text = json.dumps(results, indent=1) + "\n"  # key order is pinned too (stored JSON)
    if os.environ.get("AGENTOS_PIN_WRITE") == "1":
        DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return
    pinned = json.loads(path.read_text())
    assert sorted(results) == sorted(pinned), "the cases changed: the pin no longer covers the same inputs"
    for case, got in results.items():
        assert json.dumps(got) == json.dumps(pinned[case]), f"case {case!r} changed"
