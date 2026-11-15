"""The catch-up after a sleep or a restart: on a laptop, the reapers must not wait.

While a laptop sleeps, its VMs keep running and billing, and nothing on the laptop runs. Time
limits are wall-clock times, so the first reaper pass after the wake closes every environment past
its limit. But the reaper's own wait counts awake time only (asyncio's clock stops while the
computer sleeps on macOS and Linux), so that pass could come up to a minute late. CatchUp
runs it at once:

- when the service starts (a restart after a crash, a reboot, `agentos local start`);
- when the wall clock jumped ahead of the awake clock by WAKE_JUMP_S or more: the computer slept.

Each catch-up first sets the VM firewall to this computer's address now (it may have woken on
another network), then runs the environments' reaper and the VMs' reaper, and writes what they
closed to local-catch-up.json in the home, which `agentos local status` reads.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentos.core import fs
from agentos.local.paths import catch_up_path

log = logging.getLogger("agentos.local")

SERVICE_NAME = "local_catch_up"
TICK_S = 5.0
WAKE_JUMP_S = 30.0


class CatchUp:
    def __init__(self, runtime: Any, home: Path, *, tick_s: float = TICK_S, jump_s: float = WAKE_JUMP_S,
                 wall: Callable[[], float] = time.time, awake: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.runtime = runtime
        self.home = Path(home)
        self.tick_s, self.jump_s = tick_s, jump_s
        self._wall, self._awake, self._sleep = wall, awake, sleep
        self._task: asyncio.Task | None = None
        self.last: dict | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="local-catch-up")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _loop(self) -> None:
        await self.run("started")
        wall, awake = self._wall(), self._awake()
        while True:
            await self._sleep(self.tick_s)
            now_wall, now_awake = self._wall(), self._awake()
            slept = (now_wall - wall) - (now_awake - awake)
            wall, awake = now_wall, now_awake
            if slept >= self.jump_s:
                await self.run("woke", slept_s=slept)

    async def run(self, reason: str, *, slept_s: float | None = None) -> dict:
        """One catch-up: the firewall, then both reapers. Never raises: what failed is recorded."""
        record: dict[str, Any] = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "reason": reason,
                                  "slept_s": round(slept_s) if slept_s is not None else None,
                                  "closed": [], "vms_deleted": [], "errors": []}
        vms = self.runtime.get("vms")
        if vms is not None and vms.store.live():
            try:
                await vms.refresh_firewall()
            except Exception as e:  # noqa: BLE001 - the reapers still run; the tunnels retry on their own
                record["errors"].append(f"firewall: {getattr(e, 'message', e)}")
        envs = self.runtime.get("environments")
        if envs is not None:
            try:
                out = await envs.reap_once()
                for env_id in [*out.get("closed", []), *out.get("gone", [])]:
                    env = envs.get(env_id)
                    record["closed"].append({"id": env.id, "title": env.title, "reason": env.closed_reason or ""})
            except Exception as e:  # noqa: BLE001
                record["errors"].append(f"environments: {getattr(e, 'message', e)}")
        if vms is not None and vms.configured:
            try:
                record["vms_deleted"] = list((await vms.reap_once()).get("deleted", []))
            except Exception as e:  # noqa: BLE001
                record["errors"].append(f"vms: {getattr(e, 'message', e)}")
        self.last = record
        if record["closed"] or record["vms_deleted"] or record["errors"] or reason == "woke":
            log.info("catch-up after %s: %s", "a wake" if reason == "woke" else "the start", note(record))
        try:
            fs.write_private(catch_up_path(self.home), json.dumps(record, indent=2) + "\n")
        except OSError as e:
            log.warning("could not write %s: %s", catch_up_path(self.home), e)
        return record


def read_last(home: Path) -> dict | None:
    try:
        data = json.loads(catch_up_path(home).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _span(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{max(1, minutes)} min"
    return f"{minutes // 60} h {minutes % 60} min"


def _when(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def note(record: dict | None) -> str:
    """The catch-up in one or two plain sentences, for `agentos local status`."""
    if not record:
        return ""
    when = _when(str(record.get("at") or ""))
    if record.get("reason") == "woke":
        slept = record.get("slept_s")
        head = f"The computer woke at {when}" + (f" after {_span(slept)} asleep" if slept else "")
    else:
        head = f"The service started at {when}"
    closed = record.get("closed") or []
    if closed:
        names = ", ".join(f"{c['id']} ({c.get('title') or 'no title'}: {c.get('reason') or 'closed'})" for c in closed)
        body = f"{head}, and it closed {len(closed)} environment{'s' if len(closed) != 1 else ''} at once: {names}."
    else:
        body = f"{head}. No environment was past its time limit."
    errors = record.get("errors") or []
    if errors:
        body += f" Some of the check failed: {'; '.join(errors)}."
    return body


def build(runtime: Any) -> CatchUp:
    cfg = runtime.get("config")
    svc = CatchUp(runtime, cfg.home)
    runtime.register(SERVICE_NAME, svc)
    return svc
