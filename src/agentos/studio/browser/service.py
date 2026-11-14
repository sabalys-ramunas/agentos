"""The `studio` service: the review browser pool on a node, and on `agentos dev up`.

Built with the node's other feature services (agentos.node.daemon.FEATURE_SERVICES). Building it
starts nothing: the pool starts an engine on the first page asked of it, and the service's stop
closes every browser. It holds no lane, no vault and no lane profile, and asks the runtime for
none of them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentos.core.runtime import Runtime
from agentos.studio.browser.pool import ReviewPool, default_state_dir
from agentos.studio.browser.status import studio_status

SERVICE_NAME = "studio"


class StudioService:
    """runtime.studio: `browser` is the ReviewBrowser; `status()` is studio_status for this node."""

    def __init__(self, browser: ReviewPool):
        self.browser = browser

    @property
    def where(self) -> str:
        return self.browser.where

    @property
    def state_dir(self) -> Path:
        return self.browser.state_dir

    def status(self) -> dict[str, Any]:
        return studio_status(self.browser)

    async def stop(self) -> None:
        await self.browser.close()


def where_of(runtime: Runtime) -> str:
    """`local` on the dev node (a laptop), `node` on agentosd."""
    lanes = runtime.get("lanes")
    dev = lanes is not None and any(c.__module__.startswith("agentos.dev") for c in type(lanes).__mro__[:-1])
    return "local" if dev else "node"


def state_dir_of(runtime: Runtime, where: str) -> Path:
    """`~/.agentos/studio/` on a laptop; `<node home>/studio/` on a node (part 9 of the plan).
    Nothing is created here: the store (S2) makes the folder when it first writes."""
    config = runtime.get("config")
    home = getattr(config, "home", None) if config is not None else None
    return default_state_dir(where, Path(home) if home and where == "node" else None)


def build(runtime: Runtime) -> StudioService:
    where = where_of(runtime)
    service = StudioService(ReviewPool(where=where, state_dir=state_dir_of(runtime, where)))
    runtime.register(SERVICE_NAME, service)
    return service
