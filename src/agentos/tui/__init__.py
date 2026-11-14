"""`agentos dash`: the terminal dashboard (docs/ARCHITECTURE.md section 9, docs/API.md).

Layers, each importable without the ones above it:
  connect   where the node is and which token to use (flags, env, node.toml)
  client    async HTTP client for /api plus the SSE stream
  models    lenient views of the API objects
  state     everything the dashboard knows, plus derived views (health, Needs you)
  sync      loads, polls and live updates with reconnect and degraded mode
  app       the Textual app and its screens
"""
