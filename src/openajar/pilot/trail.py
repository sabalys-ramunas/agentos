"""What a worker did, one step at a time, in words a person reads.

Built from the loop's rows, so it says only what was measured. A control's name came from the page,
so it is page text like any other: `Result.public` lists `trail` among the untrusted fields, and a
typed value never appears here, only where it went.
"""

from __future__ import annotations

from typing import Any

WORDS = {
    "SCROLL": "Scrolled down",
    "WAIT": "Waited for the page",
    "READ": "Read the page",
    "DONE": "Checked it was done",
    "UPLOAD": "Added the file",
    "RESUME": "Carried on after a yes",
    "TOO_MANY": "Found more controls than it could choose from",
}


def _what(row: dict[str, Any]) -> str:
    op = str(row.get("ran") or row.get("op") or "")
    element = row.get("element")
    if op == "CLICK":
        what = f"Pressed {element}" if element else "Pressed a control"
    elif op == "TYPE":
        what = f"Filled in {element}" if element else "Filled in a field"
    elif op == "WRITE":
        what = f"Wrote the words for {element}" if element else "Wrote the words"
    else:
        what = WORDS.get(op, "Looked at the page")
    if row.get("rescued"):
        what += ", after a stronger AI model helped"
    if row.get("error"):
        what += ", and it was refused"
    return what


def step(row: dict[str, Any]) -> dict[str, Any]:
    op = row.get("ran") or row.get("op")
    kind = "rescue" if row.get("rescued") else "write" if op == "WRITE" else "you" if op == "TOO_MANY" else "decide"
    return {"kind": kind, "what": _what(row), "model": row.get("model"), "ms": int(row.get("ms") or 0),
            "usd": round(float(row.get("usd") or 0.0), 6)}


def trail(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [step(row) for row in rows]
