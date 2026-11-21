"""Feature package entry: `build(runtime)` returns the "vms" service (a VmManager)."""

from __future__ import annotations

from typing import Any

from openajar.core.config import home_from_env
from openajar.vms.manager import VmManager

SERVICE_NAME = "vms"


def build(runtime: Any) -> VmManager:
    """The VM manager. Its reaper starts with the node and runs whether or not VMs are set up yet
    (it does nothing until `openajar vm setup` has stored a token)."""
    cfg = runtime.get("config") if runtime is not None else None
    home = cfg.home if cfg is not None else home_from_env()
    return VmManager(home, runtime=runtime)
