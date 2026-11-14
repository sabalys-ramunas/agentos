"""Where the screenshots kept after an agent's commit live, and their retention: files under
`$AGENTOS_HOME/audit/captures/` named by their SHA-256, deleted when the action log's rows go and
past a total size cap, oldest first. The capture itself (when one is taken, its settings and its
row in the action log) is agentos.audit.captures; the policy service prunes from here with the
action log's own retention.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

MAX_TOTAL_BYTES = 200 * 1024 * 1024   # all kept screenshots together; past it the oldest go
STALE_TMP_S = 3600                    # a half-written file this old was left by a write that died
FORMATS = ("png", "webp", "jpeg")
_HEX = re.compile(r"^sha256:([0-9a-f]{64})$")


def capture_dir(home: Path) -> Path:
    return Path(home) / "audit" / "captures"


def capture_file(home: Path, sha256: Any, fmt: Any) -> Path | None:
    """Where the screenshot with this hash is kept, or None when the hash or format is not one."""
    m = _HEX.match(sha256) if isinstance(sha256, str) else None
    if m is None or fmt not in FORMATS:
        return None
    return capture_dir(home) / f"{m.group(1)}.{fmt}"


def prune(home: Path, *, retention_days: float, max_bytes: int = MAX_TOTAL_BYTES, now: float | None = None) -> int:
    """Delete kept screenshots older than the action log keeps rows, then the oldest past the size
    cap, and half-written `.tmp` files older than STALE_TMP_S. Returns how many files went."""
    root = capture_dir(home)
    if not root.is_dir():
        return 0
    now = time.time() if now is None else now
    cutoff = now - retention_days * 86400
    files: list[tuple[float, int, Path]] = []
    removed = 0
    for p in root.iterdir():
        if p.suffix == ".tmp" and p.is_file():
            try:
                if p.stat().st_mtime < now - STALE_TMP_S:
                    p.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                pass
            continue
        if not p.is_file() or p.suffix.lstrip(".") not in FORMATS:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_mtime < cutoff:
            p.unlink(missing_ok=True)
            removed += 1
        else:
            files.append((st.st_mtime, st.st_size, p))
    total = sum(size for _, size, _ in files)
    for _, size, p in sorted(files):
        if total <= max_bytes:
            break
        p.unlink(missing_ok=True)
        total -= size
        removed += 1
    return removed
