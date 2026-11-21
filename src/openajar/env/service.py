"""Feature package entry: `build(runtime)` returns the "envs" service (an EnvManager)."""

from __future__ import annotations

from typing import Any

from openajar.core.config import home_from_env
from openajar.env.manager import EnvManager

SERVICE_NAME = "envs"


def build(runtime: Any) -> EnvManager:
    """Build the env manager for this node. The daemon should `await svc.start()` at boot and
    `await svc.shutdown()` on exit; tools also start it lazily on first use."""
    cfg = runtime.get("config") if runtime is not None else None
    home = cfg.home if cfg is not None else home_from_env()
    return EnvManager(home, runtime=runtime)
