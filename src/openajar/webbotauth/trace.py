"""The identity trace: one JSON line per signed (or deliberately unsigned) request and per
redirect hop off a signed host, in `lanes/<workspace>/<name>/identity-trace.jsonl` (mode 600).

A row says that a request was signed, with what key and for what window, and the SHA-256 of the
signature base (design section 5.8). It never holds the `Signature` or `Signature-Input` header:
a signature is a replayable bearer token until it expires (protocol section 6.9). Rows carry the
origin and path but not the query, which can hold tokens. Writing never fails a request: errors
are logged, and the file rolls over to `.1` past a size bound.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("openajar.webbotauth.trace")

MAX_BYTES = 4 * 1024 * 1024
FORBIDDEN = frozenset({"signature", "signature-input", "headers", "query", "url"})


class IdentityTrace:
    def __init__(self, path: Path, *, max_bytes: int = MAX_BYTES):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def write(self, row: dict[str, Any]) -> None:
        clean = {k: v for k, v in row.items() if k.lower() not in FORBIDDEN}
        clean.setdefault("at", datetime.now(UTC).isoformat(timespec="milliseconds"))
        line = json.dumps(clean, sort_keys=True, default=str) + "\n"
        try:
            with self._lock:
                self._roll()
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a") as f:
                    f.write(line)
        except OSError as e:
            log.warning("could not write the identity trace %s: %s", self.path, e)

    def _roll(self) -> None:
        try:
            if self.path.stat().st_size >= self.max_bytes:
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
        except FileNotFoundError:
            pass

    def read(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text().splitlines()
        except FileNotFoundError:
            return []
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out
