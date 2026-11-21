# Legacy names

openajar was called agentos until 2026-11-20. The product, the package, the command, the MCP server
names, the skills and the docs say openajar now. A few names still say agentos on purpose: data that is
already stored, machines that are already running, and older versions all hold them, and changing one
would strand something. This page lists them, so nobody renames them by accident.

`scripts/rename_to_openajar.py` made the rename by rules. Its `PROTECT` list is the source of truth for
what keeps the old name; a line of code that carries the word `legacy-name` keeps its old names too.

## What still answers to the old name

These are settings a person made under the old name. The new name is the default, and the old one still
works:

| Old | New | How the old one keeps working |
| --- | --- | --- |
| the `agentos` command | `openajar` | `agentos` is an alias of the same program; it says once, on stderr, that it is now `openajar` |
| `AGENTOS_HOME` and every other `AGENTOS_*` variable | `OPENAJAR_*` | read when the `OPENAJAR_*` one is not set (`OPENAJAR_*` wins) |
| `~/.agentos` | `~/.openajar` | used as it is when it holds the node (`node.toml`) and `~/.openajar` does not, or when `~/.openajar` does not exist; nothing moves it, so its environments, saves, keys, grants and the home id its cloud machines are labelled with stay as they were |
| `~/.config/agentos` (connections, tokens, the headers helper) | `~/.config/openajar` | used as it is when `~/.config/openajar` does not exist |
| the launchd job `dev.agentos.local`, the systemd user unit `agentos-local.service` | `dev.openajar.local`, `openajar-local.service` | `openajar local start` (and `stop`) stops and removes the old one, waits for its port, then installs the new one on the same home and port; the installer stops the old one before it removes the old tool, and starts the new one after |
| the MCP entries `agentos-local` (and `agentos-<name>`) in Claude Code and Codex | `openajar-local`, `openajar-<name>` | registering writes the new entry and removes the old one it wrote before; disconnecting removes both |
| the skills `agentos-environment`, `agentos-lanes`, `agentos-offload`, `agentos-studio` | `openajar-*` | installing the new skills removes the old folders openajar wrote (a folder someone else wrote is kept) |
| the agents' tokens in the macOS Keychain under the service `agentos` | the service `openajar` | read when there is no item under the new name, by openajar and by the headers helper; a new token goes under the new name |
| backups named `agentos-<node>-<time>.tar.gz.age` | `openajar-<node>-<time>.tar.gz.age` | listed, kept and restored like new ones |
| design tokens under `agentos` or `com.agentos` in a tokens file | `openajar`, `com.openajar` | both are read |
| a repository's `.agentos/environment.toml` (0.4.0's prepared machines) | `.openajar/environment.toml` | read when the new one is not there, from GitHub, the node's mirror and the checkout; the new one wins when both are there |
| `AGENTOS_NO_WEB`, `AGENTOS_WEB_SHA256`, `AGENTOS_WEB_RELEASE_URL`, `AGENTOS_PANEL_DIR` (0.4.0's web app settings) | `OPENAJAR_*` | as every `AGENTOS_*` variable, read when the new one is not set; the installer reads its own the same way |

## What keeps the old name

| Kind | Names | Why |
| --- | --- | --- |
| The environment image and its labels | `agentos-env:latest`, `agentos-env:desktop`, `org.agentos.environment`, `org.agentos.browser`, `org.agentos.desktop` | images built by 0.3.0 are recognised as they are, without a rebuild |
| Paths on an environment's machine | `/usr/local/lib/agentos-env`, `/var/lib/agentos-env/ready`, `/opt/agentos-browser`, `/opt/agentos-desktop`, `/run/agentos-browser`, `/run/agentos-ssh`, `/etc/agentos-env`, `/agentos.swap`, `~/.config/agentos/gitconfig`, apt and needrestart files named `agentos` | saved environments and running machines hold them; the ready markers say what is installed |
| Variables a machine's own scripts read | `AGENTOS_ENV_WALL`, `AGENTOS_DESK_DIR`, `AGENTOS_DESK_SOCK`, `AGENTOS_BROWSER_X`, `AGENTOS_BROWSER_Y`, `AGENTOS_AGENT_TERM_Y`, `AGENTOS_BROWSER_REQUIREMENTS`, `_AGENTOS_DRESSED` | an image's entrypoint is baked in when it is built |
| Cloud and docker labels and names | `agentos-node`, `agentos-home`, `agentos-vm`, `agentos-lane`, `agentos-image`, `agentos-save`, `agentos-env`, `agentos-family`, `agentos-title`, `agentos-server-type`; firewalls, keys, machines and their docker networks named `agentos-...` | saves, snapshots and machines are found by them |
| Saved environment files and keys | the `AGENTOS-SAVE-1` header, the vault entry `agentos/environments/saves-key`, git refs `refs/agentos-save/...` and `refs/agentos-restore` | a save made by 0.3.0 still opens |
| What 0.4.0 put on machines and in repositories | checkpoint refs `refs/agentos/checkpoints/<n>` and their author `agentos checkpoint <checkpoint@agentos.invalid>`; the proofs branch `agentos-proofs` and the pull request markers `<!-- agentos:proofs:begin -->`, `<!-- agentos:network:begin -->` and their ends; app secrets' placeholders `agentos-secret-<random>` and the relay unit `agentos-secret-relay`; the labels `agentos-network`, `agentos-prepared`, `agentos-repo`, `agentos-check`; the proxy files `95agentos-proxy` and `docker.service.d/agentos-proxy.conf` | a saved or paused machine holds the refs, placeholders and units; open pull requests hold the markers and link the branch's files; prepared machines and closed networks are found by their labels |
| Signing, sealing and key contexts | `agentos.grant.*`, `agentos.audit.*`, `agentos-view-v2`, `agentos-blob-v1`, `agentos-vault ...`, `agentos {purpose} v1`, `agentos-{purpose}`, `agentos-confirm-v1`, `agentos-send-list-v1`, `agentos-telegram-pair`, `agentos-node-hello:v1`, `agentos-node-enroll:v1`, `agentos-controller-msg:v1`, `agentos-panel-grant`, `agentos-org-key`, `agentos-cloud-bus:v1`, the test vectors' seeds | a changed byte would break every token, link, seal, audit chain and vault made before |
| HTTP headers and schema keys | `X-Agentos-*` (CSRF, webhooks' `X-Agentos-Signature` and `X-Agentos-Timestamp`, ...), `Agentos-On`, `Agentos-Wait`, `Agentos-Max-Age`, `Agentos-Stale-Ok`, `Agentos-Run`, `x-agentos-*` keys in connector and site API schemas | webhook receivers and scripts check them; connectors store them |
| Cookies and browser storage | `agentos_session`, `agentos_controller_session`, `agentos_google_state`, `agentos.*` keys in the web app's and the live view's local storage | a rename would sign everyone out and forget their settings |
| File formats and stored fields | `agentos-backup`, `agentos_version` in a backup's header, `agentos-workspace-export` and `agentos-export`, the `.agentos` export suffix, `agentos-audit-bundle`, `agentos:pin` in a lane's pin, `agentos-file`, `urn:agentos:` problem types | files made before must still import, restore and verify |
| The owner's credential in the keychain | the service `agentos-owner` | the stored credential is found by it |
| A server node's system names | the daemon and unit `agentosd`, the system user `agentos`, `agentos-runas`, `agentos-brain`, `agentos-backup.service` and `.timer`, `agentos-lanes.slice`, `journald-agentos.conf`, `agentos.logrotate`, `/var/lib/agentos`, `/opt/agentos`, `/etc/agentos`, `agentos-first-boot`, `agentos-admin`, a node template's `AGENTOS_GIT_URL` and `AGENTOS_GIT_REF`, the tunnels `dev.agentos.tunnel.*` and `agentos-tunnel-*` | nodes upgrade in place: their users, units and paths exist on the machine; renaming them needs a migration of its own |
| The hosted cloud's database | the database `agentos` and `agentos_test`, roles `agentos_app`, `agentos_owner`, `agentos_definer`, the dev stack's compose project `agentos-cloud` | a rename would strand the data |
| The repositories | `github.com/sabalys-ramunas/agentos` and the private one | until they are renamed on GitHub; then `scripts/rename_to_openajar.py --repo-urls` points the links at the new names |

## Upgrading

From 0.3.0 or 0.4.0 on a laptop: run the installer again. It sees the `agentos` tool, stops its service
(`dev.agentos.local` or `agentos-local.service`), installs `openajar` (which brings the `agentos` command as
an alias) and starts `openajar local start` on the same home (`~/.agentos`, used where it is) and port.

- **From 0.4.0** nothing else is needed: the agent grant 0.4.0's connect issued is kept, the installed web
  app is updated to the new version (`openajar web update`, which the installer runs; `OPENAJAR_NO_WEB=1`
  or `AGENTOS_NO_WEB=1` skips it), environments, saves (snapshots and paused Morph machines), checkpoints,
  replays, proofs, prepared machines and network settings are read as they are, and a repository's
  `.agentos/environment.toml` keeps working.
- **From 0.3.0** the same, and the agent grant is replaced by one with today's specials (0.3.0's lacked
  `secret` on `env/*` lanes, which `login` needs): 0.4.0 asked for `agentos connect` here; now registering
  again does it, and the installer's restart registers. Clients registered by hand keep their token until
  they are registered again.
- The machine image is not rebuilt: `agentos-env` images from 0.3.0 and 0.4.0 are used as they are (an
  image without the desktop gets it on each machine's first screen, a minute).

A server node made by 0.3.0 can upgrade to openajar in place: `pyproject.toml` still carries the line
0.3.0's upgrader looks for, the new venv has the `agentos` alias next to `openajar`, and the new
installer accepts a prefix that holds either.

## History

The changelog's released entries, dated test and research notes, and benchmark and red-team results are
records of their time and keep the name they had.
