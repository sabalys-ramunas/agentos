# agentos on your computer

`agentos local` runs a small agentos service on your laptop (macOS or Linux). It serves MCP to the
agents on that laptop (Claude Code, Codex). When an agent opens an environment, agentos rents a
cloud VM for it (Hetzner, or a Docker container). A single-use one is deleted when the agent closes
it or its time runs out. A kept one is saved and its VM deleted when it goes idle, and comes back
when an agent opens it again with the same title
([ENVIRONMENTS.md](ENVIRONMENTS.md#keeping-an-environment)).
Nothing costs money while no environment is open, except saves (cents a month) and any VM you
chose to keep running.

The agent itself runs on the laptop too. So the laptop must be awake while a VM works: no agent
works while the lid is closed in this version.

`agentos setup` walks you through all of it. This page is what happens underneath, and how to do
it by hand.

## What runs

- MCP at `http://127.0.0.1:7410/mcp`, and the web app at `http://127.0.0.1:7410/`.
- Grants and approvals, the action log, the vault, the git gateway.
- VMs and environments, with their limits and the reaper that deletes them.

It listens on 127.0.0.1 only. There are no account browser lanes on a laptop (those need a Linux
server: see `agentos connect`). An environment has a terminal, your repo and a real browser.

## The environment's browser

The browser runs on the environment's VM, not on your laptop: Camoufox on the VM's own screen
(Xvfb). agentos on your laptop drives it over the SSH connection it already holds for the terminal,
so the browser tools work as on a server (`browser_navigate`, `browser_snapshot`, `browser_act`,
`browser_wait`, `browser_screenshot` with `proof`, `width` and `full_page`). In it, `localhost` is the
VM. The agent gives you `links.browser`, the live view, at `http://localhost:7410/view/...`: watch
it, take over, and hand it back.

Nothing browser-related is installed on your laptop except Playwright's client (the installer's
`agentos[local]`). No browser is downloaded here, and no Xvfb is needed.

Where the browser comes from on the VM:

- **Docker**: the machine image has it (`agentos environments setup --build` builds it once).
- **Hetzner with the `dev` snapshot**: it is in the snapshot, so each environment's browser starts
  at once. `agentos setup vms` offers to build it, or run
  `AGENTOS_HOME=~/.agentos agentos vm image build dev`. It takes a few minutes and costs a cent or
  two, and the snapshot (about 4 GB) is billed as Hetzner snapshot storage while you keep it.
- **Hetzner without a snapshot**: each environment installs the browser when it opens. That adds 2
  to 4 minutes to every `environment_open`.

`environments.toml` has `browser = "vm"` here, also when it does not say so. `browser = "node"` (a
server's setting) stops the service with a message, because a laptop cannot run that browser.

## Your own shell: ssh agentos-<id>

In local mode an agent can also use its own shell on the VM: `ssh agentos-<id>`, `scp`, `rsync` and
git over ssh, the way it works on the laptop. `environment_open` and `environment()` give the commands in `ssh` (`host`, `command`, `examples`).

- When an environment is ready, agentos makes a key for it and writes a host entry to
  `~/.agentos/ssh/config`: `Host agentos-<id>`, the VM's address, user root, that key with
  `IdentitiesOnly yes`, and the VM's host key pinned (`StrictHostKeyChecking yes` against
  `~/.agentos/ssh/<id>/known_hosts`; agentos made that host key and gave it to the VM, so the first
  connection is already checked). The entry and the key go when the environment closes, fails, or
  is saved and its VM deleted. A resume writes a new one.
- `agentos setup` asks once to put `Include ~/.agentos/ssh/config` at the top of `~/.ssh/config`
  (it makes the file, 0600, if there is none, and leaves the rest as it is). Then plain
  `ssh agentos-<id>` works. `agentos setup ssh` asks again. Without that line the command is
  `ssh -F ~/.agentos/ssh/config agentos-<id>`, and the results say so.
- The key is the environment's own, not the node's. On the VM it may open a terminal and run
  commands, and nothing else: no port forwarding either way, no ssh agent (your own keys never
  reach the VM), no X11. Use `environment_port` to show a dev server.
- The VM firewall lets in your laptop's address (below), which is where both agentos and your
  shell connect from, so nothing more is opened. With Docker, SSH is on 127.0.0.1.
- A machine is idle when nothing used it for `idle_minutes`. An ssh session running a command, an
  interactive one typed in lately, or one that ended within that time counts as use, so an agent
  working only over ssh keeps its machine.
- **Not in the call log.** agentos logs every call made through its tools. What runs over the
  agent's own ssh is not in that log: agentos sees that a session ran, not what it ran. This does
  not widen what the agent can reach. The VM holds no key or token of yours: its one credential is
  the git gateway token for its own repository, which works only through that VM's own tunnel and
  ends when the environment closes. The agent could already read it with `lane_exec`.

A server node (`agentos connect`) gives no `ssh`: its agent runs on another computer and works
through the tools only.

## Setup, one step at a time

`agentos setup` runs every step. Run one again to change one thing:

| Step | What it does |
|---|---|
| `agentos setup vms` | where VMs come from: a Hetzner token (checked at once), a location, a default size and the `dev` snapshot, or Docker and its image |
| `agentos setup github` | your `gh` login, `gh auth login`, a pasted token, or none; and whether environments may use any repository the login reaches |
| `agentos setup limits` | the monthly cap (10 EUR by default here), hours per VM, VMs at once |
| `agentos setup agents` | starts the service, connects Claude Code and Codex, adds the skills |
| `agentos setup ssh` | adds `Include ~/.agentos/ssh/config` at the top of `~/.ssh/config`, so `ssh agentos-<id>` works |
| `agentos setup check` | each piece, in plain words |
| `agentos setup test-vm` | opens a VM, runs `uname -a`, closes it (about 0.01 EUR on Hetzner) |

For scripts: `agentos setup --yes` takes every default and asks nothing, with
`--hetzner-token-file FILE`, `--github-token-file FILE` or `--docker`. A test VM never opens
without a yes.

## By hand, step by step

```sh
agentos local start              # sets up ~/.agentos, starts the service, connects Claude Code and Codex
AGENTOS_HOME=~/.agentos agentos vm setup        # your Hetzner token (or the docker provider)
agentos local status             # running? which port? which environments are open?
```

Then start a new Claude Code or Codex session and ask: "open an agentos environment and run
uname -a".

`agentos local start`:

1. makes `~/.agentos` (chmod 700) with `node.toml` and a vault key file in `secrets/`;
2. picks port 7410, or the next free one if 7410 is taken, and remembers it in `node.toml` (it
   never takes 7400, which `agentos connect` tunnels use, or 7450, which `agentos dev up` uses);
3. installs and loads the service: a launchd agent (`~/Library/LaunchAgents/dev.agentos.local.plist`)
   on macOS, a systemd user unit (`~/.config/systemd/user/agentos-local.service`) on Linux. It
   runs `agentos local serve`, starts at login, and starts again if it stops. On macOS it is an
   Interactive job: launchd throttles a Background one (efficiency cores, slow disk and network),
   and every git and ssh it starts with it (a 13 MB clone took 212 s instead of 2 s). A node still
   running from an older plist logs a warning, and the next `agentos local start` loads it again;
4. issues the agent grant `agentos connect` gives by default (environments and memory_read: the
   agent may open its own environment and run commands there, and nowhere else), stores its
   token in the macOS Keychain (Linux: a chmod 600 file in `~/.config/agentos/tokens/`), and
   registers `agentos-local` with Claude Code (user scope) and Codex, and copies the skills. No
   token goes into their config: they read it through `~/.config/agentos/bin/agentos-mcp-headers`.

Run it again at any time: it keeps what works.

## Commands

| Command | What it does |
| --- | --- |
| `agentos local start` | Set up, start the service, connect Claude Code and Codex. `--port`, `--no-register`. |
| `agentos local stop` | Stop it. It stays stopped, also after a restart, until `agentos local start`. |
| `agentos local status` | Running or not, the port, open environments, what it closed after a sleep. `--json`. |
| `agentos local logs` | The end of the log (`~/.agentos/logs/local.log`). `-f` keeps printing. |
| `agentos local serve` | The service itself, in the foreground. Ctrl-C stops it. |

Every command takes `--home` for another folder than `~/.agentos`.

## GitHub: any repository, or one by one

With a GitHub login saved, setup asks once: "Let environments clone and push any repository this
GitHub login can reach?" Yes (the default here) means an agent can open an environment on any
repository the login reaches, with nothing more to connect. No means you connect repositories one
by one (`agentos environments repo add github.com/<owner>/<name>`), as on a server.

Either way the push rules hold: no force push or delete of a branch the environment did not
create, and no tags. The setting is `~/.agentos/git/login.json` (`any_repo`).

With `gh`, agentos stores the token `gh auth token` gives and does not call `gh` again. When gh
signs in again or the token changes, run `agentos setup github` again.

## Where your tokens are

They stay on your laptop, in `~/.agentos`: the Hetzner token in `secrets/hetzner.token` (only you
can read it), and the GitHub token sealed in the agentos vault (`vault.db`, its key in
`secrets/vault.key`). A VM never holds either. A push from the VM goes through agentos's git gateway
with a token for that one repository, which ends when the environment closes.

## The VM firewall

A VM lets SSH in only from your laptop. Your laptop's public address changes when you move to
another network, so agentos reads it again (IPv4, and IPv6 when you have it) every time an
environment opens, and after every wake. If it cannot find it (you are offline), the environment
does not open and says so. Set `ssh_from` in `~/.agentos/vms.toml` to fixed addresses if you would
rather.

## Sleep

When the laptop is about to sleep, agentos acts on each open environment by its mode:

- **Saves when idle** (`keep="save"`): it is saved (the disk snapshot and the files) and its VM
  deleted. Nothing bills while the laptop sleeps but the save. When you are back, say "continue
  <title> in agentos" in any chat, and it comes back.
- **Single use** (`keep="none"`): one with nothing to lose is closed. One with unsaved work gets a
  rescue save (its files and commits), and you are told; an agent that opens the same title gets
  that work back.
- **Kept running** (`keep="live"`): it stays on and keeps billing, as you chose.

A sleep that comes too fast for this (the lid closed on a battery about to die) leaves the VMs
running and billing, and nothing on the laptop can delete them. When it wakes, or when the service
starts again, agentos at once deals with every environment past its time limit the same way, and
`agentos local status` says what it closed or saved. `agentos local stop` leaves open environments
running: nothing closes them until the next start.

## Remove it

```sh
agentos local stop
agentos connect --remove local   # the Claude Code and Codex entries, the token
rm -rf ~/.agentos                # everything else, the vault too
```
