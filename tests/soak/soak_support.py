"""What the soak and chaos tests share: calls that time out loudly, and refs from the fixture pages."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path
from typing import Any

from agentos.dev.slotnode import SITE, json_of, lanes_by_id, text_of
from agentos.node.snapshot import find_refs

FEED = f"{SITE}/feed.html"
FORM = f"{SITE}/form.html"
CAPTCHA = f"{SITE}/captcha.html"
POST = "Post by Ada Lovelace"
LONG_TEXT = "The quick brown fox jumps over the lazy dog while the soak test keeps typing."


async def call(client: Any, tool: str, args: dict[str, Any], timeout: float = 120) -> Any:
    return await asyncio.wait_for(client.call_tool(tool, args), timeout)


async def ok(client: Any, tool: str, args: dict[str, Any], timeout: float = 120) -> Any:
    result = await call(client, tool, args, timeout)
    assert not result.is_error, f"{tool} {args}: {text_of(result)[:800]}"
    return result


async def lanes(client: Any) -> dict[str, dict[str, Any]]:
    return lanes_by_id(await ok(client, "lanes_list", {}, timeout=15))


async def control(client: Any, lane: str) -> dict[str, Any]:
    return json_of(await ok(client, "lane_control", {"on": lane}, timeout=15))


def one_ref(outline: str, role: str, name: str, within: str | None = None) -> str:
    refs = find_refs(outline, role=role, name=name, within=within)
    assert len(refs) == 1, f"expected one {role} {name!r}, got {refs}\n{outline[:2000]}"
    return refs[0]


async def name_field(client: Any, lane: str) -> str:
    await ok(client, "browser_navigate", {"on": lane, "url": FORM})
    return one_ref(text_of(await ok(client, "browser_snapshot", {"on": lane})), "textbox", "Name")


def kill_our_process(pid: int, home: Path) -> None:
    """SIGKILL a process only after checking its command line is one this test's daemon started."""
    argv = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
    assert str(home) in argv, f"refusing to kill {pid}: not ours ({argv[:200]!r})"
    os.kill(pid, signal.SIGKILL)


async def until(check, timeout: float, what: str, interval: float = 0.5) -> Any:
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        last = await check() if asyncio.iscoroutinefunction(check) else check()
        if last:
            return last
        await asyncio.sleep(interval)
    raise AssertionError(f"{what}: not within {timeout}s (last: {last!r})")


def reported_failure(outcome: Any) -> bool:
    """A call the agent sees fail: an exception on the connection, or an error result."""
    if isinstance(outcome, BaseException):
        return True
    if outcome.is_error:
        return True
    body = json_of(outcome)
    return isinstance(body, dict) and body.get("ok") is False
