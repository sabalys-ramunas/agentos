"""agentos brains: keep the agent thinking on the node when the laptop sleeps.

An optional plugin (docs/ARCHITECTURE.md section 4). It runs `claude -p` and `codex exec` on
the node under the owner's own subscription, routes each task privacy-first and then
subscription > local > metered, models each subscription as a limited seat, and keeps tasks
alive across the caller disconnecting and the daemon restarting.

ToS boundary, stated plainly: subscriptions are bring-your-own, for the owner's own use on the
owner's node. agentos never pools one subscription across tenants or people, never resells
tokens, and a hosted edition must give each tenant its own credential. If a provider closes
the door on headless use, disable this plugin; the rest of agentos is untouched.

Integration (feature package contract):
  service   agentos.brains.service.build(runtime) -> BrainService, runtime name "brains"
  tools     agentos.brains.tools.tool_specs(runtime)
  cli       agentos.brains.cli.app  (mount as `agentos brains`)
  api       agentos.brains.api.routes(runtime)  (mount under /api)
"""

from agentos.brains.service import BrainService, build
from agentos.brains.tools import tool_specs

__all__ = ["BrainService", "build", "tool_specs"]
