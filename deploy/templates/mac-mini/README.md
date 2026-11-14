# Mac mini: an agentos controller on macOS

A Mac mini at home can be the controller: the one MCP URL your agents connect to, and the place
Linux nodes dial into. This template sets it up as a LaunchAgent, so it restarts after a crash,
and sets the power so it stays on.

## What runs on macOS today

Works:

- the controller, `agentos controller serve`, under launchd (`setup.sh`)
- the controller commands: `join-token`, `nodes`, `agent-token`, `login` (the panel's sign-in link)
- `agentos connect`, from this Mac to a node or controller elsewhere (the token goes in the Keychain)
- the power settings (`power.sh`)

Does not run on macOS yet:

- **Browser lanes.** Camoufox and Xvfb are Linux-only in this build. Run lanes on a Linux node
  (the Hetzner or home-box template) and join that node to this controller.
- `agentos node serve` and `agentos up`. Both start a node, and a node needs the lane stack.
- `deploy/install-node.sh`. It targets Ubuntu 24.04.
- `agentos doctor` as a node check. On a Mac it reports the Linux packages, the browser, the
  service and the displays as missing. That is expected, so `setup.sh` runs its own check instead.

A macOS node (computer use on the real desktop, the Keychain as a vault) is a planned big bet, in
the design stage (docs/design/desktop-nodes.md).

## Set up

1. Get the checkout onto the Mac (`git clone <the agentos repository URL>`) and
   `cd` into it.
2. Run `deploy/templates/mac-mini/setup.sh`. It installs `uv` with Homebrew (if Homebrew is
   there), runs `uv sync --frozen --no-dev` in the checkout, creates `~/.agentos`, writes the
   controller settings, renders the LaunchAgent to `~/Library/LaunchAgents/dev.agentos.controller.plist`,
   loads it, and checks three things: the agent is loaded, the MCP endpoint answers 401 without
   the token, and the token opens it.
3. Run `deploy/templates/mac-mini/power.sh` once. It asks for your password and reports the
   settings it could not apply.
4. Keep it running after a restart. A LaunchAgent starts when you log in. On a headless Mac mini,
   turn on automatic login (System Settings, Users & Groups). macOS offers automatic login only
   with FileVault off. With FileVault on, the controller comes back after a restart once someone
   logs in.
5. Connect Claude Code on this Mac. `AGENTOS_HOME=~/.agentos .venv/bin/agentos controller agent-token`
   prints the MCP line with the agent token. Treat that token like a password. Setting
   `AGENTOS_HOME=~/.agentos` in your shell profile saves typing it.

`setup.sh --check` runs only the self-check. It is also what to run after an update: `git pull`,
then `setup.sh` again (it re-syncs the environment and reloads the LaunchAgent).

## Nodes on other machines

A node elsewhere needs a URL it can reach, and TLS unless it is on this Mac. The controller listens
on 127.0.0.1 only. Two ways, as docs/CONTROLLER.md describes under "Joining a second node":

- Behind TLS: a reverse proxy that terminates TLS (Caddy, nginx, or Tailscale serve), with
  `public_url` and `behind_tls_proxy = true` in `~/.agentos/controller/controller.toml`.
- Over SSH: the node keeps a tunnel to this Mac, `ssh -N -L 7401:localhost:7401 mac`, and joins
  `ws://localhost:7401`.

Make a join token on this Mac with `.venv/bin/agentos controller join-token --name NODE` (with
`AGENTOS_HOME=~/.agentos` set). A token is single use and valid for one hour.

## Where things are

| What | Where |
|---|---|
| Controller settings (mode 600, the agent and owner tokens) | `~/.agentos/controller/controller.toml` |
| Controller database (orgs, nodes, grants, approvals, traces) | `~/.agentos/controller/controller.db` |
| Controller secrets (keys) | `~/.agentos/controller/secrets/` |
| The LaunchAgent | `~/Library/LaunchAgents/dev.agentos.controller.plist` |
| The controller log | `~/Library/Logs/agentos/controller.log` |

The LaunchAgent runs the virtual environment in this checkout. If you move the checkout, run
`setup.sh` again.

## Backups

This template does not back the controller up. Copy `~/.agentos/controller` to another disk or
machine on a schedule you can keep, and check that the copy opens. The controller database holds
the org, the nodes and the grants.
