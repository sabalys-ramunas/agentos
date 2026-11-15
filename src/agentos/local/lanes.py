"""The lane manager of agentos local: the node's own (agentos.node.lanes), with no browser on this computer.

The one lane it opens is an environment's browser (`env/<id>-browser`), and that browser runs on the
environment's VM: agentos.environments.vmbrowser puts its routers in front of this manager's engine
and displays (agentos.local.node does it when the node is built), so the lane's screen is the VM's
Xvfb and its browser the VM's Camoufox, driven over the SSH connection this computer holds. Every
browser tool, the egress guard, the live view, take over and proofs are the node's own code.

Behind the routers are NoLocalEngine and NoLocalDisplays: a lane that is not routed to a VM would need
a browser on this computer (Camoufox on Xvfb, Linux only), and they refuse it with words that say why.
Nothing here imports Camoufox, Playwright or Xvfb; the routed engine imports Playwright when the first
browser opens. Account lanes are refused at create: a laptop has none.

`makes_browsers = False` tells agentos.environments not to make an environment's browser as a lane on
this computer: an environment with browser = "node" (made before, or by hand) gets a terminal only
(agentos.environments.bringup._make_browser).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentos.core.errors import ServiceUnavailable
from agentos.core.models import LaneSpec, ViewerInfo
from agentos.node.lanes.manager import LaneManagerImpl
from agentos.node.store import LaneStore

NO_LOCAL_BROWSER = ("this agentos runs on your computer, so a browser runs only on an environment's VM. There "
                    "are no account lanes here, and nothing starts a browser on this computer. Open an "
                    "environment (environment_open) and use its browser.")
WORKSPACE = "env"  # agentos.environments.models.WORKSPACE: the environments' lanes


def is_environment_browser(lane_id: str) -> bool:
    """`env/<id>-browser`: the one kind of lane this computer has."""
    workspace, _, name = str(lane_id).partition("/")
    return workspace == WORKSPACE and name.endswith("-browser") and len(name) > len("-browser")


class NoLocalEngine:
    """The engine behind the router: a lane the router does not send to a VM has no browser here."""

    name = "camoufox"  # what a LaneSpec names by default: an environment's browser is Camoufox on the VM
    identity_os: list[str] | None = None
    close_timeout = 15.0
    files = None

    async def launch(self, spec: LaneSpec, **_: Any) -> Any:
        raise ServiceUnavailable(f"{spec.id}: {NO_LOCAL_BROWSER}")


class NoLocalDisplays:
    """The displays behind the router: no Xvfb on this computer, so no screen for a lane that is not routed."""

    async def acquire(self, lane_id: str, *, size: tuple[int, int] | None = None) -> ViewerInfo:
        raise ServiceUnavailable(f"{lane_id}: {NO_LOCAL_BROWSER}")

    async def release(self, lane_id: str) -> None:
        return None

    def get(self, lane_id: str) -> ViewerInfo | None:
        return None

    def healthy(self, lane_id: str) -> bool:
        return True

    async def release_all(self) -> None:
        return None


class LocalLanes(LaneManagerImpl):
    """LaneManagerImpl that makes environments' browser lanes only."""

    makes_browsers = False  # no browser on this computer: only a "vm" environment's lane gets one

    async def create(self, spec: LaneSpec) -> Any:
        if not is_environment_browser(spec.id):
            raise ServiceUnavailable(f"lane {spec.id} was not made: {NO_LOCAL_BROWSER}")
        return await super().create(spec)

    async def import_profile(self, spec: LaneSpec, profile_src: Path, fingerprint_src: Path | None) -> Any:
        raise ServiceUnavailable(f"lane {spec.id} was not made: {NO_LOCAL_BROWSER}")


def build_local_lanes(config: Any, **kwargs: Any) -> LocalLanes:
    """The local node's lane manager: its store in the home's database, nothing on this computer behind
    the routers. The browser runs on the VM, so this computer's memory is no limit (no memory floor)
    and its own public address is never compared with a lane's (no node IP probe)."""
    kwargs.setdefault("node_ip_probe", None)
    kwargs.setdefault("memory_probe", lambda: None)
    return LocalLanes(config, LaneStore(config.db_path), NoLocalEngine(), NoLocalDisplays(), **kwargs)
