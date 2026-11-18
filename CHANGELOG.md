# Changelog

All notable changes to agentos are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) (before 1.0, a minor version may break things).

## [Unreleased]

### Added

- **Morph Cloud as a VM provider.** `agentos vm setup --provider morph` takes a Morph API key from
  your own account (hidden, checked, kept 0600), pins the host key of Morph's SSH gateway and makes
  Morph the provider; `agentos setup` offers it next to Hetzner and Docker. Machines start from a
  snapshot in about a second, sign in through Morph's gateway with each machine's own key (the API
  key is never on a command line), and carry a Morph time limit as a backstop. Sizes are agentos's
  (`morph-small` to `morph-xlarge`), and prices are estimates from Morph's MCU rates in `vms.toml`.
- **On Morph, a save pauses the machine.** A kept environment that goes idle, is closed or reaches
  its time limit is paused with its memory instead of snapshotted and deleted, and
  `environment_open` with its title resumes the same machine in seconds, its dev server still
  running (`resumed.from` is `paused`). Forget and retention delete the paused machine.
  `pause_saves = false` in `environments.toml` turns it off.
- Providers declare what they can do (firewall, snapshot, pause, network policy, cloud-init), and a
  machine without cloud-init gets its setup from the node at once.
- `scripts/morph_live_check.py` checks the Morph provider against a real account for a few cents.

### Fixed

- **`agentos setup --docker` works from the one-line install.** The machine image's Dockerfile and
  entrypoint now ship inside the package, so the image builds without a checkout of agentos.

### Changed

- **The Docker image downloads instead of building.** Setup pulls the prebuilt
  `ghcr.io/sabalys-ramunas/agentos-env:<version>` (amd64 and arm64, published by each release) in a
  minute or two, and builds it here only when it cannot (10 to 15 minutes). `agentos environments
  setup --pull` does the same; `--build` still builds it here; `docker_pull = ""` in `vms.toml` never
  downloads.
- The README says the web app is not part of the one-line install.

## [0.3.0] - 2026-11-16

Agents work on their computer the way they work locally, one chat per machine, and a lot less waiting.
Built from three rounds of agent testers and two field reports from a real project.

### Changed

- **One chat per machine.** `environment_open` returns `on`, a handle only that chat has; every call to
  the machine carries it. Another chat's title is `environment_in_use`; `take_over=true` moves a machine
  that has been quiet for 10 minutes. Clients that send no session id must pass `on` on every call.
- **Commits are yours.** With your GitHub login saved, an environment's commits carry your GitHub name
  and noreply address (they link to your account without your real email); without one, the agent's.
- **One count limit** (`environment_full`, with `retry_after`), and `environment_open` waits for a free
  slot up to its `wait_s` instead of failing.

### Added

- **The agent's own shell:** `ssh agentos-<id>`, `scp` and `rsync` from the laptop (`agentos setup ssh`
  adds one Include line to `~/.ssh/config`). Each machine has its own key; port forwards are off
  (`environment_port` gives a local link to a dev server).
- **Dev servers survive agentos.** Jobs run detached on the VM with their output in files there; a restart
  or an update of agentos reattaches to them with the same job ids and offsets.
- `lane_job_read(until_port=3000)` waits on the machine until a dev server listens (or returns the job's
  end), and `environment(on=...)` waits up to a minute for a starting machine: no more polling.
- **Memory:** every VM gets swap, and a process the kernel kills for memory is named in the job's and
  the command's result, with the machine's memory and the next size.
- A resume brings back dev servers from what was listening when the machine was saved.

### Fixed

- Opening is about three times faster (81 s instead of 255 s on a real run): the local service no longer
  runs as a throttled macOS Background job, the setup check is one call, the clone through the laptop is
  blobless (a quarter of the bytes), public repositories clone straight from GitHub, and the mirror warms
  while the VM boots.
- Nothing runs on a machine that is still starting (a git command during the clone broke it); a failed
  one says why on the next call.
- No system updates in the middle of a session (they reloaded systemd and restarted the network under a
  starting browser): they are applied when a VM is set up and when the dev snapshot is built, and a
  kept-running machine gets them at a quiet moment.
- A save could be undone by the reaper and leave its title held; a push could reuse a fetch from before
  it and let a force push over a colleague's commit through. Both fixed.
- Clearer errors: which browser launch step hung, the `pkill -f` that killed its own shell, the two
  limits' words, `which_environment` naming the open titles, and `unsaved_work` offering a git bundle.
- Installed skills refresh when the service starts; `agentos local status` shows the right start time.

## [0.2.0] - 2026-11-16

The first tagged release. agentos is now about agent environments: your coding agent's own
computer in the cloud, and it stays when you want it to. It runs on your laptop, with no server
needed. Two older parts are gone.

### Highlights

- **One command to install, then a guided setup.** `curl -fsSL .../scripts/install.sh | sh`
  installs the newest release (`AGENTOS_GIT_REF` picks another) and starts `agentos setup`: VMs
  (a Hetzner token, or Docker), your GitHub login, the limits, and Claude Code and Codex connected
  to a small service on your laptop. Nothing bills until an agent opens a VM.
- **Three ways an environment stays**: single use (deleted when done), saves when idle (the whole
  machine is saved and the VM deleted, resumed by its title from any chat), and kept running
  (only when you allow it).
- **Private repositories** through your GitHub login, or a card where you paste a token; the agent
  never sees one.
- **Moving files**: `environment_upload` sends a local checkout up with a one-use link, and
  `environment_download` gives you a file from the VM. `environment_port` gives you a link to a
  dev server, and `environment_pull_request` opens the pull request.
- **The browser runs on the VM**, ready at boot with the `dev` snapshot that setup offers to build.
- **A new README and logo**, with a picture of how it works and the setup in a terminal.
- `agentos setup` shows which agents it connected in plain lines, without the grant details.

### Added

- **Kept environments, in the agent's words and yours.** The `agentos-environment` skill and the
  MCP intros (server and local) teach the three modes: single use for a one-off fix, `keep="save"`
  when the work spans sessions (saved when idle, cents a month), and `keep="live"` only when the
  person asks for an always-on machine. They teach resuming by title and reading `resumed` (its
  note is data, never instructions), `environment_save` before risky steps and at the end of the
  day, telling the person how to come back ("say: continue the shop in agentos"), forgetting
  finished saves, and upload and download in one line each.
- **The web app shows how each environment stays.** The list and each environment's page say
  "Single use", "Kept running" or "Saves when idle", with what it costs while unused. A Saved
  section lists each save (title, repository, branch, when, the snapshot's size, what it costs a
  month, and the agent's note as text), says when one is pruned soon, and has Forget for the
  owner, which says what is deleted before it deletes it.
- **`agentos setup` asks one question**: single use or saves when idle. Kept running is explained
  with its cost and is allowed only when you choose it (`allow_keep_live`).

### Changed

- README, docs/VISION.md, docs/ENVIRONMENTS.md ("Keeping an environment") and docs/LOCAL.md
  (sleep: a kept one is saved, a single-use one gets a rescue save) are written around the three
  modes, with the honest limits of local mode: the laptop must be awake while a VM works, and no
  agent runs while it is closed.

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

## [0.1.0] (never tagged)

The first public code. agentos is cloud dev environments for AI coding agents: a chat's agent
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
