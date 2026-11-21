"""Feature service hook: agentosd builds this after the other feature services.

Always registers "grant_issuers" (openajar.policy.issuers.TrustedIssuers), trusting the org
grant key from node.toml once the node has joined a controller, and forgetting any other org's (one
it left: standing.leave_org). With an enrollment it also
registers "channel" (NodeChannel), which agentosd starts last and stops first, so the node
takes controller work only once everything else is up. Without one the service is dormant.
"""

from __future__ import annotations

import logging
from typing import Any

from openajar.node.channel import standing
from openajar.policy.issuers import SERVICE_NAME as ISSUERS
from openajar.policy.issuers import TrustedIssuers

log = logging.getLogger("openajar.channel")

SERVICE_NAME = "channel"


class DormantChannel:
    """This node has not joined a controller (`openajar node join`)."""

    connected = False
    degraded = False

    def status(self) -> dict:
        return {"enrolled": False, "connected": False}

    async def start(self) -> None:
        log.info("no controller configured; the node serves its own MCP only")

    async def stop(self) -> None:
        return None


def build(runtime: Any) -> Any:
    from openajar.node.channel.node import NodeChannel

    config = runtime.require("config")
    issuers = runtime.get(ISSUERS)
    if not isinstance(issuers, TrustedIssuers):
        issuers = TrustedIssuers(config.home)
        runtime.register(ISSUERS, issuers)
    current = standing.org_issuer(config) if config.enrolled else None
    for other in issuers.issuers:  # an org this node left (a join that ran while the daemon did)
        if other.startswith("org:") and other != current:
            standing.leave_org(config.home, other, issuers=issuers)
    if not config.enrolled:
        return DormantChannel()
    if config.grant_pubkey:
        issuers.trust(current, config.grant_pubkey)
    channel = NodeChannel(config, runtime, issuers=issuers)
    runtime.register(SERVICE_NAME, channel)
    return channel
