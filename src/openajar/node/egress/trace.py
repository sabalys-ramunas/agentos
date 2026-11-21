"""The egress trace: one JSON line per refused or recorded request, host and path only, rotated by size."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openajar.node.egress.constants import TRACE_MAX_BYTES

log = logging.getLogger("openajar.node.egress")


# ---- the trace ---------------------------------------------------------------------------

class EgressTrace:
    """One JSON line per block and per observed commit, mode 600, rolled to `.1` past a bound.
    Rows carry host, path and method: never a query, headers or a body."""

    FORBIDDEN = frozenset({"url", "query", "headers", "body", "post_data"})

    def __init__(self, path: Path, *, max_bytes: int = TRACE_MAX_BYTES):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def write(self, row: dict[str, Any]) -> None:
        clean = {k: v for k, v in row.items() if k not in self.FORBIDDEN}
        clean.setdefault("at", datetime.now(UTC).isoformat(timespec="milliseconds"))
        line = json.dumps(clean, sort_keys=True, default=str) + "\n"
        try:
            with self._lock:
                with contextlib.suppress(FileNotFoundError):
                    if self.path.stat().st_size >= self.max_bytes:
                        os.replace(self.path, self.path.with_name(self.path.name + ".1"))
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a") as f:
                    f.write(line)
        except OSError as e:
            log.warning("could not write the egress trace %s: %s", self.path, e)

    def read(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text().splitlines()
        except FileNotFoundError:
            return []
        out = []
        for line in lines[-limit:]:
            with contextlib.suppress(ValueError):
                out.append(json.loads(line))
        return out
