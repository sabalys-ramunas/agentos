"""Small asyncio helpers shared by the node and its tools."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import Any

log = logging.getLogger("agentos.aio")


async def run_to_end(aw: Awaitable[Any]) -> Any:
    """Await `aw` to completion even if the caller is cancelled meanwhile, then re-raise the
    cancellation (also when `aw` failed: the cancellation wins, so it is never lost). Used for
    teardown: a browser must never be left half closed."""
    task = asyncio.ensure_future(aw)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.done():
                raise
            cancelled = True
        except BaseException:
            if cancelled:  # the caller was cancelled meanwhile: that wins over the teardown's error
                log.warning("a teardown failed after its caller was cancelled", exc_info=True)
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        raise asyncio.CancelledError
    return result
