"""Feature package entry: `build(runtime)` returns the "environments" service (an EnvironmentManager).

Built after env and vms (an environment is a VM lane), and after the lanes (its browser is a lane).
Its reaper starts with the node; it does nothing while no environment is open."""

from __future__ import annotations

from typing import Any

from openajar.core.config import home_from_env
from openajar.environments.manager import EnvironmentManager

SERVICE_NAME = "environments"


def build(runtime: Any) -> EnvironmentManager:
    cfg = runtime.get("config") if runtime is not None else None
    home = cfg.home if cfg is not None else home_from_env()
    mgr = EnvironmentManager(home, runtime=runtime)
    if runtime is not None:
        runtime.register(SERVICE_NAME, mgr)
    return mgr
