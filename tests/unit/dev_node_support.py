"""`agentos dev up` in a thread of its own, for tests that call it the way a shell on this computer
does: over real HTTP, with the agent grant it prints, from synchronous code (agentos.client.call)."""

from __future__ import annotations

import asyncio
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from agentos.dev.node import DevNode, run


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def dev_node_thread(home: Path) -> Iterator[DevNode]:
    """The seeded dev node on 127.0.0.1, until the block ends."""
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    box: dict = {}

    def main() -> None:
        asyncio.set_event_loop(loop)

        async def serve() -> None:
            box["stop"] = asyncio.Event()

            def announce(node: DevNode) -> None:
                box["node"] = node
                ready.set()

            await run(home=home, port=free_port(), seed_lanes=True, announce=announce, stop=box["stop"])

        try:
            loop.run_until_complete(serve())
        except BaseException as exc:  # noqa: BLE001 - shown by the assert below
            box["error"] = exc
            ready.set()
        finally:
            left = asyncio.all_tasks(loop)  # the SSE shutdown watchers outlive the server
            for task in left:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*left, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    thread = threading.Thread(target=main, name="dev-node", daemon=True)
    thread.start()
    assert ready.wait(60) and "node" in box, f"the dev node did not start: {box.get('error')!r}"
    try:
        yield box["node"]
    finally:
        loop.call_soon_threadsafe(box["stop"].set)
        thread.join(30)
