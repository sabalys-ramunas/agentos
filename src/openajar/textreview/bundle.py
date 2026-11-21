"""Review runs on disk: `$OPENAJAR_HOME/text-review/<run id>/`, one folder each, private to the node.

    evidence.md       exactly what the reviewer saw of this week
    meta.json         the window, the counts, the brain, the cost and the status
    proposal.json     the reviewer's edit, as it replied (ready or rejected)
    validation.json   the safety lock's verdict on each reply
    variant/          instructions.md, tools.json, recipes.json, errors.json: the current text with
                      the proposal merged (ready only), for `openajar lanebench run --variant DIR`
    CHANGES.md        what a person reads first
    reports/          the lanebench reports this run read, moved out of the inbox

A status is one of: ready (a proposal passed the lock), rejected (it did not, after one retry),
skipped (too little evidence, so no model was asked) and failed (the brain gave no usable reply).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import secrets
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openajar.core.errors import Conflict, NotFound
from openajar.textreview.settings import review_dir

RUN_ID_RE = re.compile(r"^rv-\d{8}-\d{6}-[0-9a-f]{4}$")
STATUSES = ("ready", "rejected", "skipped", "failed")


class Runs:
    def __init__(self, home: Path):
        self.root = review_dir(home)

    def new_id(self, at: float) -> str:
        return f"rv-{datetime.fromtimestamp(at, UTC).strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"

    def path(self, run_id: str) -> Path:
        if not RUN_ID_RE.match(run_id or ""):
            raise NotFound(f"{run_id!r} is not a review id. `openajar text-review list` shows them")
        found = self.root / run_id
        if not (found / "meta.json").is_file():
            raise NotFound(f"no review {run_id}. `openajar text-review list` shows them")
        return found

    def _mkdir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)

    def write(self, run_id: str, files: dict[str, str]) -> Path:
        if not RUN_ID_RE.match(run_id):
            raise ValueError(f"bad run id {run_id!r}")
        self._mkdir(self.root)
        folder = self.root / run_id
        self._mkdir(folder)
        for name, text in files.items():
            target = folder / name
            if target.parent != folder:
                self._mkdir(target.parent)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
        return folder

    def keep_reports(self, run_id: str, reports: list[Path]) -> None:
        """The inbox files a run read move into it, so the next run does not read them again."""
        if not reports:
            return
        folder = self.root / run_id / "reports"
        self._mkdir(folder)
        for report in reports:
            with contextlib.suppress(OSError):
                shutil.move(str(report), folder / report.name)

    def meta(self, run_id: str) -> dict[str, Any]:
        return json.loads((self.path(run_id) / "meta.json").read_text())

    def list(self) -> list[dict[str, Any]]:
        """Every run, newest first. A folder that cannot be read is left out."""
        out = []
        if not self.root.is_dir():
            return out
        for folder in self.root.iterdir():
            if RUN_ID_RE.match(folder.name):
                with contextlib.suppress(OSError, ValueError):
                    out.append(json.loads((folder / "meta.json").read_text()))
        return sorted(out, key=lambda m: m.get("started_at") or 0, reverse=True)

    def last_started(self) -> float | None:
        runs = self.list()
        return float(runs[0]["started_at"]) if runs else None

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """One run at a time on a node, whether the daemon or a person at the shell started it."""
        self._mkdir(self.root)
        fd = os.open(self.root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Conflict("a text review is already running on this node. `openajar text-review list` shows "
                               "it when it ends") from None
            yield
        finally:
            os.close(fd)


def changes_md(meta: dict[str, Any], proposal: dict[str, Any] | None, problems: list[str], folder: Path) -> str:
    """What a person reads first: the status, each change with its evidence, and what to do next."""
    status = meta["status"]
    counts = meta.get("counts") or {}
    head = {
        "ready": f"Ready: {len((proposal or {}).get('changes') or [])} changes proposed. Nothing on the node changed.",
        "rejected": "Rejected: the proposal failed the safety lock twice, so there is no variant to probe.",
        "skipped": "Skipped: too little evidence, so no model was asked.",
        "failed": "Failed: the brain gave no usable reply.",
    }[status]
    lines = [f"# Agent text review {meta['id']}", "", head, ""]
    if meta.get("reason"):
        lines += [f"Why: {meta['reason']}", ""]
    lines += [f"- Window: {meta['window']['from']} to {meta['window']['to']}",
              (f"- Evidence: {counts.get('calls', 0)} calls in {counts.get('sessions', 0)} sessions, "
              f"{counts.get('probe_tasks', 0)} probe tasks ({counts.get('probe_failed', 0)} failed)"),
              (f"- Brain: {meta.get('brain') or 'none'}; {len(meta.get('attempts') or [])} answers asked for, "
              f"${meta.get('cost_usd', 0):.2f} spent (${meta.get('notional_usd', 0):.2f} at API prices)"), ""]
    changes = (proposal or {}).get("changes") or []
    if changes:
        lines += ["## Changes", ""]
        for i, change in enumerate(changes, 1):
            lines += [f"### {i}. {change['what']}", f"- Evidence: {change['evidence']}", f"- Fixes: {change['fixes']}",
                      ""]
        edited = [f"{key}: {', '.join(sorted(proposal[key]))}" for key in ("tools", "recipes", "errors")
                  if proposal and proposal.get(key)]
        if proposal and "instructions" in proposal:
            edited.insert(0, "instructions")
        if edited:
            lines += ["## What the variant edits", "", *[f"- {e}" for e in edited], ""]
    elif status == "ready":
        lines += ["The reviewer found nothing worth changing this week.", ""]
    if problems:
        lines += ["## Why the proposal was refused", "", *[f"- {p}" for p in problems], ""]
    lines += ["## Next", "",
              "1. Read evidence.md: exactly what the reviewer saw. It holds no page text and no argument values."]
    if status == "ready" and changes:
        lines += [(f"2. Probe the variant: `openajar lanebench run --variant {folder / 'variant'}`, and compare it with a run "
                  "without it on the same tasks and seed. Weaker models show the difference first."),
                  ("3. Turn what holds up into a code change: the instructions and recipes in src/openajar/node/tools/recipes.py, "
                  "the tool docstrings, and the `agent` lines in src/openajar/core/error_catalog.toml. Nothing here is "
                  "applied on its own.")]
    return "\n".join(lines).rstrip() + "\n"
