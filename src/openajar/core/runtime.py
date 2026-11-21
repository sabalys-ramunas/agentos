"""The runtime container: every service on a node (or controller) registered by name.

Feature packages never import each other's implementations; they ask the runtime.
The composition root (openajar.node.daemon) builds one Runtime and registers what is
configured. A tool that needs a missing service raises ServiceUnavailable with a clear
message instead of crashing.

Well-known service names (keep this list in sync with the packages that provide them):
  config        NodeConfig
  events        openajar.api.events.EventBus (live updates for the panel; registered first)
  lanes         openajar.node.interfaces.LaneManager
  interaction   openajar.node.interfaces.Interaction
  after_commit  openajar.audit.captures.after_commit (handed each agent commit on a lane, by browser_act)
  tool_catalog  openajar.mcp.providers.ProviderCatalog (the tools agents see, for the text review)
  card_facts    openajar.node.cardfacts (what an approval card shows that only the node knows: core.cardfacts)
  policy        openajar.policy  (grants, authorization, effect routing)
  freeze        openajar.freeze  (break-glass: frozen lanes, workspaces, the node; checked before every agent call)
  approvals     openajar.policy  (approval queue, notifier)
  traces        openajar.policy  (append-only, redacted action log)
  ledger        openajar.policy.ledger (per account and kind: every like, follow, post that left, whoever sent it)
  vault         openajar.vault
  runner        openajar.runner  (durable runs, schedules, triggers)
  flows         openajar.runner  (recorded flows, replay, typed flow tools)
  brains        openajar.brains  (brain runner, router, subscription seats)
  plugins       openajar.plugins (which workers this node has, what is on, and the brain on each job)
  envs          openajar.env     (terminal and service lanes, isolation, programs)
  environments  openajar.environments.manager.EnvironmentManager (a cloud computer per chat; node code asks it by
                method, e.g. bound_to_call(ctx) for whoami, browser_environment(lane_id), workdir_of(lane_id))
  android       openajar.android
  notifier      openajar.policy  (phone push, webhooks)
  typist        openajar.node.typist (factory page -> Typist the vault types secrets through)
  flow_driver   openajar.node.flow_driver (FlowDriver factory the runner replays flows through)
  health        openajar.ops     (periodic proxy, session and resource checks)
  files         openajar.node.files (per-lane inbox and outbox)
  workspaces    openajar.workspaces (workspace names and colors, templates, bulk lanes, login queue)
  memory        openajar.memory  (workspace memory with provenance, docs/design/workspace-memory.md)
  expose        openajar.node.expose (the owner's dev servers in a browser lane, a lane's port on the laptop)
  pilot         openajar.pilot   (browser_task: a cheap worker drives one lane toward one goal, docs/design/browser-and-computer-agents.md)
"""

from __future__ import annotations

from typing import Any

from openajar.core.errors import ServiceUnavailable


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
