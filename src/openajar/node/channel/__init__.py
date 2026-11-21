"""The node end of the controller channel (docs/PROTOCOL.md v1, docs/CONTROLLER.md).

  wire.py          envelope, limits, ULIDs, encodings, signature strings (shared with the controller)
  identity.py      the node key (secrets/node.key) and controller signature checks
  store.py         channel.db: the persistent outbox, the at-most-once call cache, approvals sent
  streams.py       binary stream frames with credit flow control (shared with the controller)
  node_streams.py  live view (vnc, through openajar.node.vnc_relay) and terminal PTYs
  invoke.py        one invoke through the node's own MCP wrapper, grant token bound
  bridge.py        lanes, load, tools, traces and approvals shaped for the wire
  node.py          NodeChannel: connect, authenticate, heartbeats, outbox, handlers, degraded mode
  enroll.py        `openajar node join`
  service.py       the feature service agentosd starts when node.toml has an enrollment
"""
