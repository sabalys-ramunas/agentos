"""`openajar up`: a controller and this node on one box, in one process, joined to each other.

1. Start the controller (controller.toml in the controller home; tokens are created once).
2. If node.toml is not enrolled with this controller, mint a join token in-process and run
   the normal enrollment over the real WebSocket to localhost (the same path a remote node
   takes, so nothing is special-cased). A node already joined keeps its identity; only its
   controller_url follows the controller's port. A node the owner removed enrolls again.
   This controller is the owner's own, on the same box, so joining writes the full controller
   ceiling (openajar.policy.ceiling) to node.toml; a node joined before the ceiling existed gets
   it once too. A ceiling the owner set afterwards is kept.
3. Run the node daemon as `openajar node serve` would; its channel dials the controller.
4. Print the one MCP URL. Ctrl-C (or SIGTERM) stops the node, then the controller.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Callable
from typing import Any

from openajar.controller.app import serve_controller
from openajar.controller.config import ControllerConfig
from openajar.core.config import NodeConfig, save_config
from openajar.node.channel import wire
from openajar.node.channel.enroll import enroll
from openajar.policy.ceiling import ControllerCeiling

START_TIMEOUT = 30.0


def node_name(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:31].strip("-")
    return cleaned or "node-1"


async def run_up(node_config: NodeConfig, controller_config: ControllerConfig, *, stop: asyncio.Event | None = None,
                 on_ready: Callable[[ControllerConfig, str], None] | None = None,
                 build_runtime: Callable[[NodeConfig], Any] | None = None) -> None:
    from openajar.node.daemon import serve

    if node_config.mcp_port and node_config.mcp_port == controller_config.port:
        raise RuntimeError(f"the node and the controller both want port {controller_config.port}; change "
                           "mcp_port in node.toml or port in controller.toml")
    stop = stop or asyncio.Event()
    controller_stop = asyncio.Event()
    started: asyncio.Future = asyncio.get_running_loop().create_future()

    def on_started(port: int, controller: Any) -> None:
        if not started.done():
            started.set_result((port, controller))

    controller_task = asyncio.create_task(serve_controller(controller_config, stop=controller_stop,
                                                           on_started=on_started))
    try:
        done, _ = await asyncio.wait({controller_task, started}, timeout=START_TIMEOUT,
                                     return_when=asyncio.FIRST_COMPLETED)
        if started not in done:
            if controller_task.done():
                controller_task.result()  # raises what stopped it
            raise RuntimeError("the controller did not start in time")
        port, controller = started.result()
        cfg = controller_config.model_copy(update={"port": port})
        local_url = f"{'wss' if cfg.tls else 'ws'}://localhost:{port}{wire.CONNECT_PATH}"
        known = controller.store.node(node_config.node_id) if node_config.enrolled else None
        # a node the owner removed in the panel is not joined any more: it enrolls again
        mine = node_config.controller_pubkey == controller.keys.controller_pubkey \
            and known is not None and known.get("removed_at") is None
        if not mine:
            name = node_name(node_config.name)
            _, token = controller.create_join_token(controller.org["id"], name=name, ttl_minutes=5)
            node_config = await enroll(node_config, local_url, token, name=name, ceiling=ControllerCeiling.full(),
                                       ceiling_by="self_hosted")
        else:
            update = {} if node_config.controller_url == local_url else {"controller_url": local_url}
            if ControllerCeiling.upgradable(node_config):
                update.update(ControllerCeiling.full().settings("self_hosted"))
            if update:
                node_config = node_config.model_copy(update=update)
                save_config(node_config)
        if on_ready is not None:
            on_ready(cfg, node_config.name)
        runtime = build_runtime(node_config) if build_runtime is not None else None
        await serve(node_config, runtime, stop=stop)
    finally:
        controller_stop.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(controller_task, 15)
