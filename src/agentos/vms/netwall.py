"""A machine's network wall, set outside the machine: what lets an environment's `network` setting
(agentos.environments.network: full, limited, none) hold even though the agent is root on its VM.

- `full`: today's machine. The provider's usual firewall (Hetzner: SSH in from the node only) and,
  on docker, the entrypoint's wall against private addresses.
- `limited` and `none`: the machine opens no connection out at all. Its only ways out are the
  ports the node forwards into it over its SSH connection (the git gateway, the node's allowlist
  proxy); the node decides what those reach.

How each provider closes it:

- Hetzner: a second firewall for closed machines (`<node firewall>-closed`), used in place of the
  node's own at create. Its inbound rules are the same (SSH and ping from the node), and it has one
  outbound rule, ICMP to the node's own addresses. Hetzner drops every other outbound packet once a
  firewall has any outbound rule (docs.hetzner.com/cloud/firewalls/faq), and its firewalls are
  stateful, so the node's SSH connection in keeps working. Capability: `ensure_closed_firewall`.
- Docker: the machine's entrypoint refuses every new outbound connection but loopback (DNS
  included) before sshd starts, with no NET_ADMIN left after (agentos.vms.docker). Capability:
  `closes_network = True`.
- Any other provider (one that has neither) refuses limited and none: `cannot_close` says why.

The setting reaches the provider as the server label `agentos-network`, so the provider protocol
(agentos.vms.provider) does not change, and a listing of the servers shows it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

LABEL = "agentos-network"
CLOSED = ("limited", "none")
CLOSED_SUFFIX = "-closed"


def closed(network: str | None) -> bool:
    return network in CLOSED


def labels(network: str | None) -> dict[str, str]:
    """The label a closed machine carries (a full one carries none, as before)."""
    return {LABEL: str(network)} if closed(network) else {}


def cannot_close(prov: Any, *, firewall_on: bool = True) -> str | None:
    """Why this provider cannot hold a closed network for its machines, or None when it can."""
    if getattr(prov, "closes_network", False):
        return None
    if callable(getattr(prov, "ensure_closed_firewall", None)):
        if not firewall_on:
            return ("this node's VM firewall is off (vms.toml firewall = false), and the closed network is a "
                    "firewall rule. Set firewall = true in vms.toml")
        return None
    name = getattr(prov, "name", "this provider")
    return (f"the {name} provider cannot close a machine's network from outside it, so network limited or none "
            "cannot hold there. Use network full, or the hetzner or docker provider")


async def firewall_for(prov: Any, network: str | None, name: str, labels_: dict[str, str],
                       ssh_from: Callable[[], Awaitable[list[str]]], *, on: bool = True) -> str:
    """The firewall a new machine gets ("" for none): the node's own (when the provider has firewalls and
    vms.toml's `firewall` is on), or for a closed network its closed twin, whatever that setting says."""
    if not closed(network):
        if on and getattr(prov, "firewall", True):
            return str(await prov.ensure_firewall(name, labels_, await ssh_from()))
        return ""
    if getattr(prov, "closes_network", False):
        return ""  # the docker entrypoint closes it (the server label says so)
    make = getattr(prov, "ensure_closed_firewall", None)
    if callable(make):
        return str(await make(name + CLOSED_SUFFIX, labels_, await ssh_from()))
    from agentos.vms.models import VmError

    raise VmError(cannot_close(prov) or "this provider cannot close a machine's network")


async def refresh(prov: Any, name: str, labels_: dict[str, str], nets: list[str]) -> None:
    """With follow_ip: the node's firewall, and its closed twin when the provider has one, let SSH in
    from the node's addresses now."""
    await prov.ensure_firewall(name, labels_, nets)
    make = getattr(prov, "ensure_closed_firewall", None)
    if callable(make):
        await make(name + CLOSED_SUFFIX, labels_, nets)
