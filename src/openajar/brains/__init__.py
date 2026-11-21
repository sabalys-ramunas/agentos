"""openajar brains: keep the agent thinking on the node when the laptop sleeps.

An optional plugin (docs/ARCHITECTURE.md section 4). It runs `claude -p` and `codex exec` on
the node under the owner's own subscription, routes each task privacy-first and then
subscription > local > metered, models each subscription as a limited seat, and keeps tasks
alive across the caller disconnecting and the daemon restarting.

ToS boundary, stated plainly: subscriptions are bring-your-own, for the owner's own use on the
owner's node. openajar never pools one subscription across tenants or people, never resells
tokens, and a hosted edition must give each tenant its own credential. If a provider closes
the door on headless use, disable this plugin; the rest of openajar is untouched.

Integration (feature package contract):
  service   openajar.brains.service.build(runtime) -> BrainService, runtime name "brains"
  tools     openajar.brains.tools.tool_specs(runtime)
  cli       openajar.brains.cli.app  (mount as `openajar brains`)
  api       openajar.brains.api.routes(runtime)  (mount under /api)
"""

from openajar.brains.service import BrainService, build
from openajar.brains.tools import tool_specs

__all__ = ["BrainService", "build", "tool_specs"]
