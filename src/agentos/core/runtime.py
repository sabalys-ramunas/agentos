"""The runtime container: every service on a node (or controller) registered by name.

Feature packages never import each other's implementations; they ask the runtime.
The composition root (agentos.node.daemon) builds one Runtime and registers what is
configured. A tool that needs a missing service raises ServiceUnavailable with a clear
message instead of crashing.

Well-known service names (keep this list in sync with the packages that provide them):
  config        NodeConfig
  events        agentos.api.events.EventBus (live updates for the panel; registered first)
  lanes         agentos.node.interfaces.LaneManager
  interaction   agentos.node.interfaces.Interaction
  after_commit  agentos.audit.captures.after_commit (handed each agent commit on a lane, by browser_act)
  tool_catalog  agentos.mcp.providers.ProviderCatalog (the tools agents see, for the text review)
  card_facts    agentos.node.cardfacts (what an approval card shows that only the node knows: core.cardfacts)
  policy        agentos.policy  (grants, authorization, effect routing)
  freeze        agentos.freeze  (break-glass: frozen lanes, workspaces, the node; checked before every agent call)
  approvals     agentos.policy  (approval queue, notifier)
  traces        agentos.policy  (append-only, redacted action log)
  ledger        agentos.policy.ledger (per account and kind: every like, follow, post that left, whoever sent it)
  vault         agentos.vault
  runner        agentos.runner  (durable runs, schedules, triggers)
  flows         agentos.runner  (recorded flows, replay, typed flow tools)
  brains        agentos.brains  (brain runner, router, subscription seats)
  plugins       agentos.plugins (which workers this node has, what is on, and the brain on each job)
  envs          agentos.env     (terminal and service lanes, isolation, programs)
  android       agentos.android
  notifier      agentos.policy  (phone push, webhooks)
  typist        agentos.node.typist (factory page -> Typist the vault types secrets through)
  flow_driver   agentos.node.flow_driver (FlowDriver factory the runner replays flows through)
  health        agentos.ops     (periodic proxy, session and resource checks)
  files         agentos.node.files (per-lane inbox and outbox)
  workspaces    agentos.workspaces (workspace names and colors, templates, bulk lanes, login queue)
  memory        agentos.memory  (workspace memory with provenance, docs/design/workspace-memory.md)
  expose        agentos.node.expose (the owner's dev servers in a browser lane, a lane's port on the laptop)
  pilot         agentos.pilot   (lane_do: a cheap worker drives one lane toward one goal, docs/design/browser-and-computer-agents.md)
"""

from __future__ import annotations

from typing import Any

from agentos.core.errors import ServiceUnavailable


class Runtime:
    def __init__(self, **services: Any):
        self._services: dict[str, Any] = {}
        for name, svc in services.items():
            self.register(name, svc)

    def register(self, name: str, service: Any) -> None:
        self._services[name] = service

    def has(self, name: str) -> bool:
        return name in self._services

    def get(self, name: str) -> Any | None:
        return self._services.get(name)

    def require(self, name: str) -> Any:
        svc = self._services.get(name)
        if svc is None:
            raise ServiceUnavailable(f"{name} is not configured on this node")
        return svc

    def names(self) -> list[str]:
        return sorted(self._services)

    def __getattr__(self, name: str) -> Any:
        # runtime.lanes, runtime.vault, ... ; missing services raise a clear error
        if name.startswith("_"):
            raise AttributeError(name)
        return self.require(name)
