# agentos client: connect this computer to a node

```
agentos connect mynode             # tunnel, the agents' grant, the owner credential, Claude Code, Codex, skills
agentos connect root@new-box       # no node there yet: asks once, installs one, then connects (--install, --dry-run)
agentos connect --check            # tunnel, grant, and whether Claude Code and Codex see the server
agentos nodes                      # every connection: tunnel and MCP health, and which grant its agents use
agentos status [mynode]            # whoami and lanes, through the tunnel
agentos dash                       # the dashboard, as you (the owner), through the tunnel
agentos approvals list             # Needs you, from this computer (approve, deny: --node to choose)
agentos open biz-a/instagram       # the lane's live view in your browser (--wake if asleep)
agentos run -- make check          # this working tree into a terminal lane on the node, the command there
agentos expose 5173 --lane personal/linkedin   # your dev server in that one lane, while this runs
agentos lane forward dev/web:3000  # a service lane's port on this computer's localhost, while this runs
agentos call browsers_list         # any MCP tool, with the agents' grant (--wait for an approval)
agentos flow pull flows/           # the node's flows as files; diff, push (docs/WORKFLOWS.md, Flows in git)
agentos disconnect mynode          # undo all of it, the grant on the node included
```

The main CLI mounts these at the top level. `uv run python -m agentos.client ...` runs the same commands.

## Two credentials, never the node token

The node's own token (`mcp_token` in node.toml) is the owner: whoever holds it can do anything,
and a token never decides what its own grant family asked for. So it stays on the node.
`connect` gives this computer two credentials instead, in two separate Keychain items:

| | The agents' grant | The owner credential |
|---|---|---|
| What | a grant token, subject `claude-code@<this computer>` (kind agent) | a device credential: an owner panel session labelled with this computer's name |
| Used by | Claude Code (and Codex, T3 Code) for MCP, and `agentos call` and `agentos flow pull/diff/push` | `agentos dash`, `agentos approvals`, `agentos open` |
| Keychain | service `agentos`, account `<name>` | service `agentos-owner`, account `<name>` |
| Can | every lane, up to `--ceiling` (default commit), no specials unless `--special` | list and deny approvals, control lanes, view links (not owner links) |
| Cannot | decide any approval, or make itself an owner credential | approve or answer, or anything else that needs a person (docs/THREAT-MODEL.md); call an MCP tool (the node refuses it on /mcp) |
| Lasts | `--agent-ttl` (default 90d; `agentos doctor` warns two weeks before) | 90 days |
| Ends | `agentos disconnect`, `--new-grant`, or `agentos grants revoke <id>` on the node | `agentos disconnect`, or removing the device in the panel |

The headers helper reads only the `agentos` item, so Claude Code never sees the owner credential. An agent with a
shell as you could still read the `agentos-owner` item from the login Keychain, so the node treats it as your
computer, not as you in person: approving or answering from here is refused, and `agentos approvals approve` says
to open Needs you at `http://localhost:<tunnel port>/needs-you` and approve there, signed in with your passkey (or
a link from `ssh <host> agentos panel login`). Denying works from here.
policy/TOKENS.md ("Who holds which credential") says why the owner credential is a device
session and not a human grant.

## What `connect` does

1. **Reads the node over SSH.** `ssh -o BatchMode=yes <host> sh -s` with a short script
   on stdin. It prints only `name`, `kind`, `mcp_port` and `mcp_token` from
   `/var/lib/agentos/node.toml` (`--remote-home` for another `AGENTOS_HOME`), using
   `sudo -n` if the login user cannot read it. The token stays in memory and is never
   printed or stored.
2. **Installs the tunnel**: `~/Library/LaunchAgents/dev.agentos.tunnel.<name>.plist`
   runs `ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30
   -o ServerAliveCountMax=3 -o ConnectTimeout=15 -o ControlMaster=no -o ControlPath=none
   -L <local>:localhost:<port> <host>` with `KeepAlive` and `ThrottleInterval` 30 (launchd
   restarts it at most every 30 seconds). Its output goes to
   `~/Library/Logs/agentos/tunnel-<name>.log`. The local port is 7400, or the next free
   one, or `--local-port`.
3. **Checks it**: an MCP `initialize` over HTTP through the tunnel.
4. **Gets the two credentials** through the tunnel, with the node token, once:
   `POST /api/session/device` (the owner credential) and `POST /api/grants` (the agents'
   grant: `--client` (default claude-code) `@` this computer, workspaces `*`, `--ceiling`,
   `--special`, `--agent-ttl`). Then it checks the grant with an MCP `initialize`.
5. **Stores them** in the login Keychain, written with `security -i` so they go over stdin
   and never appear in `ps`.
6. **Writes `~/.config/agentos/bin/agentos-mcp-headers`**, a small sh script that prints
   `{"Authorization": "Bearer <grant token>"}` from the `agentos` item.
7. **Registers Claude Code**: `claude mcp add-json --scope user agentos-<name>
   '{"type": "http", "url": "http://localhost:<local>/mcp", "headersHelper":
   "~/.config/agentos/bin/agentos-mcp-headers <name>"}'`, replacing an entry with the
   same name. Without `claude` on PATH it prints that command instead.
8. **Registers Codex**, when it is installed (`codex` on PATH, or a `~/.codex` folder): a
   `[mcp_servers.agentos-<name>]` table in `$CODEX_HOME/config.toml` (default
   `~/.codex/config.toml`) with `command = "<this computer's agentos>"` and
   `args = ["mcp-bridge", "<name>"]`, and no token. The file is edited as text: comments, order and
   every other table stay as they were, and the previous file is copied to
   `config.toml.agentos-backup` first.
9. **Copies the Claude Code skills** (`agentos-lanes`, `agentos-offload`, shipped inside the
   package) into `$CLAUDE_CONFIG_DIR/skills` or `~/.claude/skills`, when Claude Code is here.
10. Prints one line per client (registered, already registered, not installed, or skipped) and the
    sentence to try first. T3 Code needs nothing more: its Claude threads read Claude Code's
    settings and its Codex threads read Codex's.

`--no-claude`, `--no-codex` and `--no-skills` leave those alone. `agentos connect --remove NAME`
removes the Claude Code entry and the Codex table, and the skills with the last connection that
copied them.

### The stdio bridge (Codex)

Codex, and T3 Code's Codex threads, start MCP servers as commands without our environment, so an
env var with the token cannot reach them. `agentos mcp-bridge <name>` is a stdio MCP server that
reads the token from the same Keychain item (or file) as the headers helper, and forwards each
message to the connection's HTTP MCP URL. Only MCP messages go to stdout; its own notes go to
stderr (Codex keeps them in its log). It exits when stdin closes. When nothing answers on the
local port it restarts the tunnel once; after a 401 it reads the token again (a new grant); after
agentosd restarts it opens a new session by itself. A request the node does not answer in 570
seconds gets an error that says so (the Codex entry sets `tool_timeout_sec = 600`), and an error
from the node carries the node's own text.

### A host with no node

`agentos connect root@my-server` on a host with no `/var/lib/agentos/node.toml` first checks it
over SSH (read only): Linux with systemd, Ubuntu 22.04 or later or Debian 12, x86_64 or arm64,
root or passwordless `sudo -n`, and no sign of agentos (no node.toml, no agentosd unit, no
`/opt/agentos`). It then says what it will install and asks once; `--install` skips the question.
The code goes to `/opt/agentos-src`: with `AGENTOS_GIT_URL` set the host clones that repository
(`AGENTOS_GIT_REF`, default main); otherwise, when this agentos runs from a checkout, connect sends
that checkout with rsync over the same SSH login (as `scripts/sync-node.sh` does: no `.git`, no
`.venv`, no `node_modules`; `web/dist` goes along, so a node without Node.js still gets
the web app built here), which works for a private repository. With neither, it refuses: the public URL is
a placeholder until it is decided. Then it runs `deploy/install-node.sh --systemd --user agentos
--hardened`, which writes node.toml and starts agentosd; then connect goes on as for any node and
prints a one-time panel sign-in link through the tunnel. `--dry-run` prints every remote command
and runs only the read-only check. A host that has agentos is never installed on.

### This computer as the node

On Linux, `agentos up --here` (the default when Claude Code or Codex is installed there) connects
this machine's clients to `http://127.0.0.1:<port>/mcp` with their own agent grant, as the
connection `local`, with no SSH and no tunnel. A Mac cannot be a node yet (browser lanes need
Linux; docs/design/desktop-nodes.md is the plan): try agentos on a Mac with `agentos dev up --seed`,
which registers Claude Code and Codex with the fake node the same way, as `dev`.

The connection record (no secrets; the grant's id, subject, ceiling and expiry, and the
device credential's id) is `~/.config/agentos/connections/<name>.json`.

A node too old to issue grants (or without its policy service) answers 404 or 503: then the
connection keeps the node token, as before, and says so. Update agentos there and run
`agentos connect --upgrade`.

A controller (`--remote-home <controller home>`) is different: its connect stub says
`kind = "controller"` and its token is already the agents' own (not the controller's owner
token), so `connect` keeps it as it is and the owner decides in the controller's panel.

### Why a headersHelper and not `--header`

`claude mcp add --header "Authorization: Bearer <token>"` puts the token on Claude
Code's command line, where any local process can read it with `ps`, and stores it in
plain text in `~/.claude.json`. With `headersHelper`, Claude Code runs the helper when
it connects (and again after a 401), so the token stays in the Keychain and a new grant
needs only `agentos connect` again, with no change to Claude Code. If your Claude
Code predates `headersHelper`, add the server by hand with `--header` and accept that
trade-off.

## Running it again

`connect` is safe to repeat. An unchanged, healthy tunnel is left running; a working grant
and owner credential are kept; the Claude Code entry is replaced, not duplicated. A
different `--ceiling`, `--client`, `--special` or `--agent-ttl`, or `--new-grant`, issues a
new grant and revokes the previous one on the node. Rotating the node token does not
break a connection that has its own grant.

## Moving an old connection off the node token

A connection made before the split gave its agents the node token. It keeps working, but you
cannot approve from this computer what those agents ask, and a copy of the token opens the
whole node. `agentos doctor` (on this computer) and `agentos nodes` warn about it.

1. On this computer: `agentos connect --upgrade` (every old connection), or
   `agentos connect <ssh-host>` for one. Each gets its own grant and an owner credential; the
   node token is replaced in the Keychain.
2. Check: `agentos doctor` shows no warning, and `agentos nodes` shows `grant gr_...` for each.
3. On the node, once every computer is moved: `agentos node rotate-token`, then
   `systemctl restart agentosd`. Any copy of the old node token, wherever it was pasted
   (an old `claude mcp add --header`, a script), stops working; grants keep working.

## When something is wrong

| Message | What to do |
|---|---|
| SSH does not know the host | check the name, or add a `Host` entry to `~/.ssh/config` |
| cannot reach `<host>` | the node is down or the network is; run connect again later |
| refused the SSH login | the tunnel cannot answer prompts: load the key with `ssh-add` (`UseKeychain yes` keeps a passphrase in the Keychain) |
| SSH has not accepted the host key | run `ssh <host>` once and accept it |
| local port N is already in use | stop the old `ssh -L` tunnel, or pick `--local-port` |
| the macOS Keychain refused | unlock it (`security unlock-keychain`), or `--token-store file` |
| cannot issue agent grants yet | the node runs an older agentos: update it, then `agentos connect --upgrade` |
| no longer accepts the owner credential | it expired (90 days) or was removed: `agentos connect <ssh-host>` again |
| could not revoke agent grant ... on the node | disconnect ran while the node was down: run the printed `agentos grants revoke` there |
| the tunnel is up, but nothing answers | agentosd is not running on the node (`systemctl status agentosd`) |
| the node refused the token from its own node.toml | the daemon runs with an older token: `systemctl restart agentosd` on the node |

A failed check leaves the tunnel installed (it keeps retrying), so fixing the node and
running `connect` again is enough. `agentos disconnect <name>` removes everything.

macOS shows a "Background Items Added" notice the first time; that is the tunnel's
LaunchAgent. If your `~/.ssh/config` adds `LocalForward` lines for the same host, the
tunnel tries to open those too and exits when one of their ports is busy.

## Linux

There is no launchd or Keychain, so:

- the tunnel is a systemd user unit, `~/.config/systemd/user/agentos-tunnel-<name>.service`,
  with `Restart=always`, `RestartSec=10` and an exponential backoff up to 5 minutes
  (`RestartSteps`, `RestartMaxDelaySec`; older systemd ignores those two lines). It is
  enabled with `systemctl --user enable` and started with `systemctl --user restart`.
  To keep it running while you are logged out: `loginctl enable-linger $USER`.
- the agents' grant is `~/.config/agentos/tokens/<name>` and the owner credential
  `~/.config/agentos/owner/<name>`, chmod 600 in chmod 700 directories. `--token-store
  file` does the same on a Mac.

Without systemd, keep the tunnel up yourself (`autossh -M 0 -N -L 7400:localhost:7400
<host>`) and register the server by hand.
