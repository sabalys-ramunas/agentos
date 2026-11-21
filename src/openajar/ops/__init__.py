"""Operations: keep a node healthy, recoverable and up to date.

Runtime service name "health" (see service.build): periodic exit-IP, proxy, disk, RAM and
browser-version-drift checks, plus a local control socket (`OPENAJAR_HOME/run/ops.sock`) so the
CLI can pause or drain lanes for a backup or an upgrade.

Modules: doctor (`openajar doctor`), backup (encrypted snapshots, verify, restore, retention, in
the age format of openajar.core.age; openajar.core.s3 is the minimal SigV4 client for `--to s3://`),
upgrade (`openajar upgrade`), health (pure health logic), service, control, templates
(`openajar node templates`: the node templates in deploy/templates and their variables), cli.

Moved elsewhere: the lane check, stealth findings, stealth check,
fingerprint drift and the red-team bench are openajar.stealth; workspace export and import are
openajar.workspaces.transfer; the lane directory format is openajar.node.lanefmt. Where things live
under OPENAJAR_HOME (layout), every probe of the machine (system) and the versioned SQLite
migrations (migrate) are in openajar.core, since every layer uses them.

Nothing in this package prints or returns a secret: proxy credentials, the MCP token, vault
keys and backup passphrases stay out of every message, log line and JSON report.
"""
