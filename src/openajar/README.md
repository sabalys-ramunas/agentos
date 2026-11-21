# Where things live in `openajar`

openajar is a node daemon (`agentosd`) that rents agents a computer of their own (an environment:
a VM with a terminal, a browser and the repo) and runs logged-in browser lanes, routines,
profiles and brains beside it. Its packages sit on six layers, top to bottom. Code imports
down, never up (`make layers`; the order is in `scripts/layers.toml`, the why in
[docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md), section 13).

| Layer | Packages | What is there |
|---|---|---|
| Entry points | `cli.py`, `commands/`, `cli_groups.py`, `tui/`, `client/`, `dev/`, `controller/`, every `*/cli.py`, `node/daemon.py` | The `openajar` command (a thin root), its own groups and the helpers every CLI shares (`commands/base.py`), the dashboard, the laptop side (`openajar connect`), the fake laptop node, the controller, and the daemon's composition root. |
| Surfaces | `api/`, `mcp/`, every `*/api.py`, `*/form.py`, `*/forms.py`, `node/channel/` | The panel and app HTTP API, the MCP server and its tool registry, the pages a node serves, and the controller's channel into the node. |
| Features | `environments/`, `studio/`, `profiles/`, `brains/`, `memory/`, `runner/`, `agenda/`, `pilot/`, `keepalive/`, `workspaces/`, `textreview/`, `plugins/`, `webhooks/`, `events/`, `agents/`, `ops/`, `freeze/`, `audit/` | What openajar does: environments per chat, page checks, profiles, brains, workspace memory, flows and routines, the agenda, the cheap pilot, keepalive visits, workspaces, operations (doctor, backup, upgrade), the freeze and the audit tools. |
| Node runtime | `node/`, `env/`, `vms/`, `git/`, `stealth/`, `vault/`, `webbotauth/`, `runas.py` | Lanes and the browser engine, terminal and service lanes, cloud VMs, the git gateway, the stealth checks, the vault, signed agent identity. |
| Safety | `policy/` | Grants, authorization and effect routing, approvals, traces, scrubbing secrets out of results. |
| Core | `core/` | Contracts every layer builds on: config, errors, models, `ToolSpec`, the runtime, the node's layout under OPENAJAR_HOME, SQLite and its migrations, `clock` (time and the ISO formats the stores write), `aio`, `fs`, `proc`. |

Starting points:

- A tool an agent calls: the package's `tools.py` (a `ToolSpec` list); `mcp/registry.py` wraps
  every call in the grant check, approvals and redaction (`policy/wrap.py`).
- A panel route: `api/routes_*.py`, mounted in `api/app.py`.
- What the daemon builds at start: `node/daemon.py`.
- A small helper you need: look in `core/` first (`clock`, `aio`, `text`, `fs`, `proc`), and put a
  shared one there rather than a private copy.
- Size limits: no new function of complexity 25 or more and no new file over 1,000 lines
  (`make complexity`, [docs/DEVELOPING.md](../../docs/DEVELOPING.md)).
