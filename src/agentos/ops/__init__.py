"""Operations: keep a node healthy, recoverable and up to date.

Runtime service name "health" (see service.build): periodic exit-IP, proxy, disk, RAM and
browser-version-drift checks, plus a local control socket (`AGENTOS_HOME/run/ops.sock`) so the
CLI can pause or drain lanes for a backup or an upgrade.

Modules: doctor (`agentos doctor`), backup (encrypted snapshots, verify, restore, retention, in
the age format of agentos.core.age; agentos.core.s3 is the minimal SigV4 client for `--to s3://`),
upgrade (`agentos upgrade`), health (pure health logic), service, control, templates
(`agentos node templates`: the node templates in deploy/templates and their variables), cli.

Moved elsewhere: the lane check, stealth findings, stealth check,
fingerprint drift and the red-team bench are agentos.stealth; workspace export and import are
agentos.workspaces.transfer; the lane directory format is agentos.node.lanefmt. Where things live
under AGENTOS_HOME (layout), every probe of the machine (system) and the versioned SQLite
migrations (migrate) are in agentos.core, since every layer uses them.

Nothing in this package prints or returns a secret: proxy credentials, the MCP token, vault
keys and backup passphrases stay out of every message, log line and JSON report.
"""
