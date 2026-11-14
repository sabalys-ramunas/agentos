# Changelog

All notable changes to agentos are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) (before 1.0, a minor version may break things).

## [Unreleased]

agentos is now about agent environments. Two older parts are gone.

### Removed

- **The engagement engine and the day plan.** The programs and sessions that liked, followed
  and commented on their own are removed, with their CLI commands (`agentos engage`,
  `agentos plan`), their MCP tools and their API routes. A run the engine queued on an older
  node is cancelled before it starts (`source_retired`), and nothing is sent. Old `engage_*`
  keys in `node.toml` are ignored, so the file still loads, and `engage.db` is left on disk as
  it is, never opened. The action ledger stays and keeps counting what each account does.
- **The classic panel (`panel/`).** A node serves the web app (`web/`) only. Gone with it:
  `agentos panel use`, `agentos errors --panel-json`, and two routes only it called
  (`GET /api/audit/replay.html`: `agentos audit replay --html` writes the same file;
  `GET /api/members/{id}/devices`: `GET /api/members/{id}` carries the devices).
  `AGENTOS_PANEL_DIR` still points a node at another build, and `agentos panel login` still
  prints a sign-in link.

### Changed

- The API demo runs as `python -m agentos.dev.apidemo` (it was `agentos.api.demo`).
- Old import paths of moved modules are gone. Use the new homes, for example
  `agentos.stealth`, `agentos.node.camoufox`, `agentos.workspaces.transfer` and
  `agentos.core.age`.

### For contributors

- `make layers` checks one import order between the packages, and `make complexity` fails on a
  function of complexity 25 or more or a file over 1,000 lines unless it is listed. Both run in
  `make lint` and CI, and their lists only shrink.

## [0.1.0] (unreleased)

The first public release. agentos is cloud dev environments for AI coding agents: a chat's agent
asks for a disposable VM with a terminal, a real browser and its repository, works there, pushes a
branch, and the VM is deleted.

### Added

- **Environments** (docs/ENVIRONMENTS.md): `environment_open(title, repo)` rents a VM for one
  chat, clones the repository into `/work/<name>` and opens a browser whose traffic leaves from
  the VM, so `localhost` is the agent's dev server. `environment()` shows its state, ports, git
  and cost; `environment_close()` refuses while work is not pushed, then deletes the VM and the
  browser. Terminal and browser calls without `on` (`lane_exec`, `lane_job_start`,
  `browser_navigate`, `ui_check`) go to the chat's environment.
- Two providers: **Docker** (an Ubuntu container with SSH on your own computer, walled off from
  your network, for trying it and for CI) and **Hetzner Cloud** (the cheapest server type that
  fits, or a `dev` snapshot from `agentos vm image build dev`).
- Git through the node's gateway: the VM holds no key, only a token for its own repository that
  works through its own tunnel and dies with the environment. A force push or a delete of the
  default branch is refused.
- Limits instead of questions: at most 3 open, 8 hours each and 50 EUR a month by default,
  set with `agentos environments allow`. The reaper closes an environment that was forgotten,
  and never deletes unsaved work for being idle.
- An environment belongs to the agent that opened it: other agents cannot see it or act on it.
- The live view of the environment's browser, to watch and take over, and the web app's
  Environments pages (each one's browser, commands, git, ports and cost).
- `agentos environments setup | allow | list | show | close` and the `agentos-environment`
  skill, installed for Claude Code by `agentos connect`.
- The web app (`web/`): environments, Needs you, runs, studio's checks, brand, machines and
  settings, served by the node at `/`.
- Studio's page checks for agents: `ui_check`, `ui_diff`, `system_check` and baselines, with
  brand profiles.

### Also in this release

- `agentosd`, the node daemon: browser lanes on Camoufox with a profile, proxy and pinned
  fingerprint each, a network self-check, and a live view to watch or take over.
- An MCP server with about 150 tools: browsing by accessibility snapshot, logins from the
  vault, files, terminals and services in a lane, flows, workflows, schedules, brains,
  engagement and connectors.
- Grants that narrow and expire, approvals, "Ask me first", standing approvals and an audit
  log.
- A vault with TOTP and an IMAP code catcher, and imports from Bitwarden, 1Password and Apple
  Passwords.
- A controller for several nodes, each dialling out over one WebSocket.
- `agentos connect` for a Mac: an SSH tunnel, a grant for your agents in the Keychain, and the
  Claude Code registration.
- `agentos dev up`, a fake node for trying agentos on a laptop.
- Flows in git: `agentos flow pull`, `diff`, `push`, `validate` and `test` keep a node's flows as
  YAML files, checked offline and tested on recorded pages with no browser; `agentos call` calls
  any MCP tool from a shell with the agents' grant.
- `agentos expose 5173 --lane ws/name`: your laptop's dev server in one browser lane, at a reserved
  `https://<name>.expose.agentos.invalid` address, over your SSH session and only while the command runs,
  so an app's "Sign in with LinkedIn" can be tried with a lane's real account. `agentos lane
  forward ws/name:PORT` is the other way: a terminal or service lane's port on your localhost.
- Git credentials for lanes and brains (docs/GIT.md): `agentos git key add` makes a deploy key on
  the node (only its public half is printed) or stores a token, and `agentos git remote allow`
  declares which repositories a workspace or a lane may use. Git in a lane, an `agentos run` job or
  a brain task reaches them through the node's git gateway, which holds the key: a push is a commit,
  a force-push or a push to the default branch always asks a person, and every use is traced.
- The installer, systemd units (a hardened one included), `agentos doctor`, backups and
  upgrades, and node templates for Hetzner Cloud, a home box and a Mac mini.
