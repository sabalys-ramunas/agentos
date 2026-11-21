"""Connectors: sites as typed operations, compiled into runner flows (docs/design/connectors.md).

A connector is data, never code: one JSON document per site (`connectors/<site>/connector.json`)
whose operations each compile into an ordinary FlowDefinition with the reserved id
`connector.<site>.<op>`, so runs, approvals, proof, holds and schedules work unchanged.

    models.py   the format: Connector, Operation, ActingAs, Pace, Signals, Commit, Readback, Binding
    steps.py    flow steps plus the connector extensions (attr, names, assert_identity, extract,
                for_each, preflight) and which of them the runner supports yet
    lint.py     the strict rules (commit path, outputs, effects, hosts, fixtures)
    compile.py  operation -> FlowDefinition, the contract hash and the implementation hash
    outputs.py  typed outputs from a run's saved values
    signing.py  signed bundles (ed25519) and their verification
    service.py  ConnectorService: install from the repo directory or a verified bundle
    surfaces.py SurfaceService: AUD-01 surfaces, the binding of a run, its approval content
    pace.py     pace per surface and operation
    health.py   per-operation, per-surface health; one notification per breakage
    taint.py    outputs' untrusted and private fields into the grant family's taint ledger
    tools.py    the `<site>_<op>` MCP tools, connectors_list, connector_status
    api.py      /api/surfaces and /api/connectors (mounted by openajar.api.mounts)
    cli.py      `openajar connector compile|keygen|sign|install|list|bind|unbind|surfaces|run|health`
"""
