"""The open-source controller (docs/CONTROLLER.md): nodes dial in, agents attach to one MCP URL.

  config.py   controller.toml, and the node.toml stub `agentos connect` reads
  store.py    SQLite: orgs, join tokens, nodes, nonces, lanes, grants, approvals, traces, events
  grants.py   the controller key, org grant keys, agent grants minted per call
  hub.py      live node sessions: invoke routing, results, approvals, streams, signed pushes
  connect.py  /v1/node/connect: challenge, enroll or hello, welcome, the session loop
  routing.py  one tool list across nodes, and which node a call goes to
  mcp.py      the agents' MCP endpoint
  web.py      the owner's sign-in, JSON API, pages, live view and terminal relays
  app.py      composition, the ASGI app, serving it
  cli.py      `agentos controller ...` and `agentos up`
  up.py       a controller and a local node in one process, joined
"""
