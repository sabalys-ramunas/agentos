# Architecture

How agentos is built. This is the technical companion to [VISION.md](VISION.md); read that first
for what agentos is and why. agentos gives every agent its own computer in the cloud: an
**environment**, a VM the node rents for one chat. Everything below (lanes, grants, the vault, the
git gateway, the audit log) is what that rests on, and the older pieces (logged-in browser lanes
for accounts, brains, flows) are features beside it.

**Status.** This document describes what is built, and marks what is not. Sections 0 to 2 and 7
describe the code as it is today (section 0's environments are being built: the contract is
[design/environments.md](design/environments.md)). Anything designed but not written yet is listed
in [Not built yet](#not-built-yet) at the end.

Guiding invariants, true everywhere below:

- **The agent's machine holds nothing of yours.** An environment's VM has no account, no key and
  no part of agentos. Its one token reaches the git gateway for its own repository, through its
  own tunnel, until it closes. Git credentials and the browser's identity stay on the node.
- **Two planes.** A control plane (the controller) decides and renders the UI. A data plane (the
  node daemon) holds everything sensitive and does the work.
- **The controller never holds secret plaintext, profile bytes, or an exit IP.** It moves
  envelopes and decisions.
- **Nodes dial out only.** No inbound ports, so a box behind your home router works like a
  cloud VM.
- **Grants are enforced at the node**, checked again after the gateway, so a bug or a hijacked
  agent is still boxed by what the node will actually do.
- **One account is one lane** for account browsers. No cross-account cookie or session bleed, by
  construction.
- **A lane's signed identity stays on its node and off by default** (Web Bot Auth,
  [design/agent-identity.md](design/agent-identity.md)).

## 0. The one model: an environment per task, named by its title

```
  your agent (Claude Code, Codex, T3 Code)               you (web app, live view, chat)
        |  MCP: environment_open, terminal_exec, browser_*, ui_check     |
        v                                                            v
  +------------------------------- node (agentosd) ---------------------------------+
  |  grants, approvals, vault, audit log, git gateway (127.0.0.1:3129)               |
  |  environment e7k2:  terminal lane env/e7k2 (SSH)   browser lane env/e7k2-browser |
  +----------+-------------------------------------------------+---------------------+
             | SSH: commands, -R tunnel to the git gateway     | SOCKS over the same SSH (-D)
             v                                                 v
  +--------------------------- VM (Hetzner or Docker) -------------------------------+
  |  /work/shop (the repo), the dev server on :3000, git -> 127.0.0.1:3129 -> node    |
  |  no agentos, no key, no account; one gateway token for /work/shop's repo only    |
  +----------------------------------------------------------------------------------+
```

- **Machine.** `environment_open` plans and creates a VM through `agentos.vms` (the cheapest
  server type that fits, unless a size is set; Hetzner Cloud or a Docker container). It never
  asks a person: the grant needs the `vm` special, and the node's limits bound it (open at once,
  the longest life, a monthly budget; `environment_limit` past one). The image is
  the `dev` snapshot when the node has one, else Ubuntu 24.04 with the tools installed by
  cloud-init. The VM is a terminal lane whose commands run over SSH (`env/<id>`), so `terminal_exec`,
  `terminal_job_*`, `shell_*` and services work on it unchanged ([design/vms.md](design/vms.md)).
- **Git.** The VM's SSH connection carries a reverse tunnel to the node's git gateway, and the
  VM's git config sends declared hosts there. Clone, fetch and push run under workspace `env`'s
  declarations: a push never asks, a force push or a delete of the default branch is refused
  (`push_refused`), the key stays in the vault ([GIT.md](GIT.md)).
- **Browser.** A browser lane on the node (`env/<id>-browser`) with no account, no keepalive and no
  vault entry but the logins the owner bound to it (agentos.environments.logins), whose proxy is a
  SOCKS tunnel over the VM's SSH. Every request, DNS included, leaves from the VM: `localhost` is the VM. Studio's review browser takes the same proxy, so
  `ui_check` on `http://localhost:3000` checks the VM's dev server. The live view and take over
  work as on any lane.
- **Binding.** The environment is bound to the MCP session (`mcp-session-id`), or to the grant
  without one. A terminal or browser call without `on` resolves to the bound environment; with
  none bound it fails with `no_environment`. A call with `on` goes to that lane as always.
- **No asking inside.** Nothing in an environment waits for a person, even after untrusted text:
  not opening it, not its commands or its browser, not a push. There is nothing on it to
  protect. What stays are refusals: the node's limits at the open, the gateway's at a push.
- **End.** `environment_close` refuses with `unsaved_work` while changes are not committed or
  commits not pushed, then deletes the VM and the browser lane. The reaper closes one that is
  idle for `idle_minutes` or past its time limit, never for idleness while work is unsaved.

Around it, the nouns: a **Node** (the machine agentos runs on, which only dials out), a
**Workspace** (one per business or person, plus `env` for environments), **Lanes** (isolated
places where one thing runs: a terminal, a service, a VM, a logged-in browser), a **Vault** that
fills logins and holds keys the model never sees, **Grants** that say what an agent may do and
expire, and, as features, **Brains** (a model subscription you bring, hosted on the node) and
**Flows** (automations that replay with no model). Everything lands in the audit log, and
anything hard to undo waits for you in Needs you.

## 1. The two planes

```
     your agent (Claude Code, Codex, any MCP client)           the panel (browser)
              |  MCP over streamable HTTP at /mcp                      |
              |  (localhost, reached over an SSH tunnel)               |
              v                                                        v
 +-------------------- CONTROLLER (optional, `agentos controller serve`) ---------+
 |  MCP gateway (/mcp, port 7401) | grants | approvals | traces (controller.db)   |
 |  owner pages and the live view relay                                           |
 +--------------+----------------------------------------------+-----------------+
                ^  one outbound WebSocket per node: /v1/node/connect            ^
                |  (signed JSON envelopes, docs/PROTOCOL.md)                    |
  +-------------+--+                +----------------+                +---------+------+
  | node A         |                | node B         |                | a Mac          |
  | agentosd       |                | agentosd       |                | agentos connect|
  | lanes, vault,  |                | lanes, brains  |                | (SSH tunnel to |
  | brains, flows  |                | + proxies      |                |  one node)     |
  +----------------+                +----------------+                +----------------+
  profiles, vault, subscription sessions and schedules live on the NODE, never on the controller
```

There are two ways to run agentos, and both are built:

- **One node, no controller.** `agentos node serve` (the `agentosd` systemd unit) runs the
  MCP server on `127.0.0.1:7400/mcp`, the panel API and the live view. From a Mac,
  `agentos connect <ssh-host>` keeps an SSH tunnel up, mints a grant for your agents, keeps
  the token in the Keychain and registers the server with Claude Code. This is the main path
  today.
- **Several nodes behind a controller.** `agentos controller serve` (or `agentos up`, which
  starts a controller and a node together) is the control plane: it authenticates agents on
  its own `/mcp`, relays each tool call to the right node, keeps the redacted traces and
  shows the approvals. Each node dials out to it ([CONTROLLER.md](CONTROLLER.md)).

**Controller:** authenticate the agent, resolve the lane to its node, relay the call over the
node's WebSocket, record the redacted trace, relay human decisions (approvals, standing
approvals, Ask me first, Take over) as signed messages. It never holds vault plaintext,
profile bytes or an exit IP.

**Node daemon (`agentosd`):** run lanes and their programs, enforce grants on every call
(again, after the controller), resolve vault secrets locally, redact before returning, run
its own schedules, flows and brains (the model router and the seat accounting run here),
store artifacts on local disk. The node is the source of truth for execution facts; the
controller is the source of truth for config and authority.

Where data lives on a node: everything is under `AGENTOS_HOME` (default `/var/lib/agentos`):
`node.toml`, SQLite databases in WAL mode (lanes, runs, brains, memory, traces, policy,
plugins, vault), `secrets/` (the vault key, chmod 600) and `lanes/<workspace>/<name>/`
(each lane's browser profile and files).

## 2. Nodes, joining, and degraded mode

A node joins a controller with one command and a short-lived join token from
`agentos controller join-token` (or the hosted cloud):

```
agentos node join [--self-hosted] [--name NAME] <controller-url> <join-token>
```

The URL is `wss://...` (plain `ws://` only to localhost). On first connect the node sends
`enroll` with its Ed25519 public key (generated on the node, never sent anywhere else) and
its capabilities (`browser`, `terminal`, `service`, and more when present). From then on
every connection starts with a challenge the node signs (`challenge`, `hello`, `welcome`).
`--self-hosted` says the controller is your own, so it may do everything the node allows.

Everything after the handshake travels on that one outbound WebSocket at
`/v1/node/connect`: `heartbeat` every 15 seconds with load and lane states, `invoke` and
`result` for tool calls, `event` and `traces` with acknowledged, replayable delivery, signed
pushes from the controller (`revoke`, `config_update`, `standing`, `ask_first`,
`lane_control`, `mesh_freeze`) and multiplexed streams for the live view, terminals, files
and logs. The message tables are in [PROTOCOL.md](PROTOCOL.md). Nodes need no inbound port,
so a box behind a home router works like a cloud VM. Between nodes there is no overlay
network: nodes only ever talk to the controller.

**Degraded mode** (controller unreachable): in-flight lanes and already scheduled flows keep
running on the grants the node holds until they expire; events and traces buffer in the
node's outbox and are sent again on reconnect (idempotent); human approvals that need the
controller wait. Fail closed on new capability, fail open on in-flight work. Section 4
extends this to brains.

## 3. Lanes: the general model

A lane is the one place work runs, so an environment's terminal and browser are lanes too, and
generalizing it costs no new plane and no new daemon. A lane has a **kind**
(`browser`, `terminal`, `service`, `android`), an **isolation profile** (four dials), and
runs on exactly one node.

### env0 and isolated lanes

A node hosts one **base** (`env0`, the node itself: real root filesystem, node
IP, shared processes, the baked base image) plus any number of **isolated lanes** that
overlay on it. `env0` is not a container, so a shared terminal has zero overhead and writes
land on the real root. Isolated lanes reuse the netns + cgroup + PID-ns + copy-on-write
overlay machinery.

```
+--------------------------- node (agentosd) ------------------------------+
|  env0 = node root fs + node IP + shared processes + baked base image      |
|    workloads: shared terminal, host services                              |
|    install here -> SHARED programs (in the base image, seen by all lanes) |
|  - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -  |
|  isolated lanes (overlay on env0), each with its own dials:               |
|   +-------------------+ +------------------+ +----------------------+      |
|   | browser (Account) | | service          | | terminal (Sandbox)   |      |
|   | net:iso fs:overlay| | net:iso fs:over  | | net:allowlist        |      |
|   | proc:iso prog:base| | proc:iso prog:ov | | fs:ephemeral prog:ov |      |
|   | [browser] [term]  | | [service] warm   | | [shell]              |      |
|   +-------------------+ +------------------+ +----------------------+      |
|  cgroup v2 per lane: memory.max cpu.max pids.max io.max                    |
|  egress: every isolated-net lane runs its own forwarder; env0 uses node IP |
+--------------------------------------------------------------------------+
```

### Isolation dials and presets

Four independent dials, each defaulting to the cheapest safe value:

```ts
interface IsolationProfile {
  network:    "shared" | "isolated"      // isolated -> own netns + egress forwarder
  exit?:      { kind: "node-ip" | "proxy"; proxyRef?: string }
  filesystem: "shared" | "overlay" | "ephemeral"   // overlay = CoW on the base image
  processes:  "shared" | "isolated"      // isolated -> own PID + pids namespace
  programs:   "base"   | "overlay"       // overlay -> per-lane installs above the base
  volumes:    VolumeMount[]              // named shared rw volumes (e.g. a brand kit)
  baseImage:  string                     // template ref, e.g. "browser@3"
  display?:   "none" | "xvfb" | "wayland"
  limits:     { memMax; cpuMax; pidsMax; diskMax }
}
```

A beginner never sees a dial. They pick a lane kind and get a **preset**:

| Preset | For | Network | Filesystem | Processes | Programs |
|---|---|---|---|---|---|
| **Account** | browser, android | identity proxy or node IP, per-site allowlist | private profile overlay | isolated | browser only, no shell |
| **Workbench** | your trusted terminal | shared (node) | shared node work dir | shared | install freely, shell on |
| **Sandbox** | throwaway exec | off or allowlist | private, ephemeral (wiped) | isolated | install into sandbox only |
| **Service** | service, brain | allowlist | private, persistent | isolated | pinned set, shell off |
| **Locked** | hosted / least trust | deny-all + explicit allowlist | private, encrypted | microVM (own kernel) | signed only |

**Account** is the wedge preset and honors "one account, one lane." A "shared terminal" is
a Workbench lane; an "isolated terminal" is a Sandbox. Full isolation is the default; the
weaker presets are opt-in (section 5 covers the risk tiers).

### Programs, base images, terminals, services

**Programs.** Shared programs are *baked*, not installed live: a base image is built from a
declarative, hashed template (packages, files, env vars), versioned (`browser@3`), stored
node-local, and pinned in grants the way flow versions are. Isolated programs are
*overlaid* into one lane and gone when it is destroyed. One verb, two targets:

```
lane.install(on, pkg, { scope: "overlay" | "shared" })
  scope:"overlay" -> writes the lane overlay           (needs install:overlay)
  scope:"shared"  -> re-bakes the base image, new ver  (needs install:base, high bar)
```

The catalog ships four curated templates, deliberately not a marketplace: **browser**
(engines, fonts), **dev-box** (shell, toolchains), **render** (headless GPU, ffmpeg), and
**brain** (an agent/LLM runtime, so an agent can be offloaded wholesale). Curated and small
is the anti-sprawl guardrail.

**Terminals** are a `shell` workload: a PTY opened inside a lane's namespaces, so egress
policy, vault and grants apply exactly as for a browser. Agents see `lane.exec(on, cmd)`
and interactive `shell.open/write/read/close`. Detached sessions survive tmux-style so a
long build keeps running after the agent disconnects, and stream to the panel as a web
terminal over the same live-view channel as the browser.

**Services** are a workload flagged `keep_warm`, supervised by the node-local durable
runner with a health check and restart policy. They sit in a separate always-warm RAM
budget (so a service never evicts a posting lane), have no inbound (nodes dial out only),
and are bound to a grant TTL with a dead-man.

### Filesystem

```
  base image (lower, read-only)          named volumes (shared, rw)
  /usr /opt baked shared programs        vol:brandkit   vol:models
        |                                      |              |
   +----+----------- overlayfs ------+         | mount        | mount
   |  lane upper (CoW, rw)           | <-------+--------------+
   |  /home  installs  artifacts     |   (mountable into several lanes)
   |  profile dir  (THE cold state)  |
   +--------------------------------+
   env0: no overlay, writes hit the node root directly
```

All of it is node-local, the crown jewels, never on the controller (which holds only a
`profile_dir_ref` plus checksum). On sleep the overlay upper and volumes persist; the
read-only base image is never duplicated; ephemeral scratch does not persist. Named volumes
are the shared-asset answer: one `brandkit` volume mounts read-write into several lanes, so
four companies share one logo set without copies.

### One lifecycle for everything

Browser, terminal, service and brain share four states, owned by the node-local durable
runner pinned to the lane's node:

| State | Browser | Terminal | Service | Brain |
|---|---|---|---|---|
| **Cold** | profile on disk, 0 RAM | work dir on disk | volume on disk | weights/config on disk |
| **Warm** | logged in, cgroup-frozen, instant | shell ready | loaded, idle | router + session warm |
| **Active** | serving under a lease | running a command | handling requests | answering the thread |
| **Sleeping** | node offline: pinned, alerts, no silent failover | same | same | same |

Plus **Quarantined** on a failed self-check. A per-node scheduler caps warm lanes by RAM
(roughly 500 MB per warm Chrome, about 8 on a 16 GB box), queues the rest, and LRU-evicts
idle warm lanes back to cold. Each lane is its own cgroup and PID namespace, so an
out-of-memory or crash kills that one lane, never the node, and an orphan sweep reaps stray
processes. This is the exact failure that crashes the Mac under Hermes today, designed out.

### Leases, self-check, state signal, engine interface

One account per browser lane makes cross-account collisions impossible, so leases only
govern several threads touching the same lane (shared gives each a tab, exclusive locks the
whole profile). Before a browser lane serves it must pass a **self-check**: exit IP matches
the expected network, STUN returns only the proxy IP, DNS resolves through the proxy,
timezone and locale match the IP, the engine answers, and a logged-in marker is present
(detected by a flow, not a model). Any failure sends it to Quarantined with a reason.

Every response carries a **state signal** read from deterministic sources, never inferred
from page text: `ok | captcha | rate_limited | blocked | logged_out | error`. The model
never reads the page to decide this, so it cannot be fooled or see secrets.

Engines sit behind one interface (`Engine` in `src/agentos/node/interfaces.py`: launch a
session for a lane, sweep orphans after a restart). Camoufox (a hardened Firefox fork, driven
through Playwright) is the one engine built today. Chrome and Android engines are planned
([Not built yet](#not-built-yet)).

### Home node vs cloud node

A home node has a real residential IP and a real GPU: pass the GPU through for genuine
WebGL, no proxy needed, best for high-scrutiny sites (Instagram, Facebook). A cloud node has
a datacenter IP (needs a residential or mobile proxy) and software WebGL (a mild tell), so
prefer it for lower-scrutiny sites (Medium, a blog, LinkedIn). One Linux caveat: overlayfs
and namespaces are Linux only, so a Mac node can host `env0`, a shared terminal and
browsers, but fully isolated lanes need a Linux node or a Linux VM. This matters for the
offload story and ships with an explicit degraded profile.

## 4. Brains, the model router, and offline continuity

A brain keeps an agent working on the node when your laptop sleeps. It is an optional plugin, a
`service` lane that serves a model, so it reuses the lane lifecycle.

- **Brain runner**: a node-side worker running `claude -p`, `codex exec` or an MCP-client agent
  inside a lane's network namespace, so its own tools reach the model only through the egress
  forwarder ([src/agentos/brains/README.md](../src/agentos/brains/README.md)).
- **Model router** (`brains/router.py`): privacy decides the pool (a local-only workspace never
  touches the cloud, tainted steps go local or ask), cost decides the order (your flat-rate
  subscription first, then a local model, then a metered API), a budget gate drops what the grant
  cannot pay, and a fallback chain tries the next on 429, quota or timeout, else queues and alerts.
- **Seats** (`brains/seats.py`): a subscription is a seat with a concurrency limit and an estimate
  of the provider's rolling window, learned from observed 429s, so the node queues rather than
  over-drives your real account.
- **Offline**: with the laptop off, a brain on the node keeps going; with the internet down, the
  router drops to local models and cloud steps wait.
- **The terms boundary**: bring your own subscription, for your own use, on your own node. The
  hosted edition never pools one subscription across people. If a provider closes the door, the
  plugin is turned off and the rest of agentos stands.

## 5. Grants and security

### Grants

A grant is a signed token of agentos's own: a chain of Ed25519-signed blocks, each one only
narrowing the one before (`src/agentos/policy/tokens.py`). Nodes hold only the public key and
verify offline, a child is provably a subset of its parent, and every block carries a
revocation id. The checks (effect class, taint, egress) are plain Python, not a Datalog
authorizer. The grant is the MCP bearer token. (Biscuit and an OAuth 2.1 resource server were
the original plan: see [Not built yet](#not-built-yet).)

A grant encodes: issuer, subject (agent, thread, human owner), workspace, lane, domains, an
effect ceiling, a budget (money and tokens with a counter), not-before and expiry, spawn
depth and fan-out, pinned tool and program manifest hashes, an egress allowlist, taint
policy, and a revocation id. The panel shows a readable projection.

**Effect classes**, lowest to highest: `read`, `draft` (private writes), `commit` (post or
publish on the bound site), `pay`, `irreversible`. Specials for the generalized surface:
`secret` (login and vault fill, URL-mode elicitation), `exec` (terminal and commands, off by
default, generalizes the old shell class), `install:overlay` and `install:base` (add a
program to one lane vs re-bake a shared image), `service` (run a long-running service, with
`{max-ttl, ports}`), and `brain` (model use, with `{provider, tier, credentialOwner,
quotaCeiling}`, so a grant names which subscription, whose credential, and a ceiling).

Attenuation on spawn, each block only narrowing:

```
root (owner, commit, brain{claude-max, 2M tok/day}, exec, install:overlay{npm@pinned})
  |- child: Codex worker   draft, brain{claude-max, 200k tok/day}, exec dropped, install dropped
  |    |- grandchild       read, no brain, no exec
```

Each level embeds its ancestors' revocation ids, so revoking the root cascades for free; the
controller pushes revocations to every node (`revoke`) and short TTLs kill stale grants even
offline.

**Narrowing is checked field by field** (policy/ROUTING.md, "Narrowed grants"), and a
property test holds every field to it. Two fields need care because a list is a permission,
not a restriction: the egress allowlist starts from the root grant (its egress, else its
domains, else nothing) and a child can only shrink it, and only the root (or the node's
allowlist) can make an install source auto. The subject belongs to the root: a child keeps
it or names a sub-agent of it (`claude-code/codex-worker`, kind agent, flow or brain), and
can never appear as a human, the panel or another identity. Taint is kept per grant family
(the root and everything narrowed from it), so delegating or reconnecting never launders
what the agent has read.

### Approvals without fatigue

Effect class drives routing. `read` and `draft` resolve automatically. `commit` runs on its own
unless "Ask me first" is on for its routine or account, it acts on no one account, or an overlay
stops it (policy/ROUTING.md). A commit that asks can still run under a standing approval you gave
once ("20 posts on this lane until Friday"), bound to what you saw: it names the flow version or
tool it covers, and the posts you approved (a week plan) or a flow version with no parameters (a
template), so an injected page cannot publish its own text under it. `pay` and `irreversible`
always need a human co-sign. New cases: `exec` in an isolated lane under an existing grant is
auto, but first use or any shared lane is co-signed; `install:overlay` from a signed
allowlisted source is auto, anything else co-signed; `install:base` is always co-signed
(it touches many lanes); `brain` within quota is auto, crossing a quota threshold or any
metered spend is treated like `pay`. The push shows what the node parsed from the real
outbound request, never text the model wrote, so a hijacked model cannot talk you into
approving it. No push link approves anything: Review opens the approval in the panel, where
you approve it signed in, and Deny is one tap. A token never approves what its own grant
family asked for.

**Flow runs get the same checks.** A saved flow's effective effect is the stricter of what it
declares and what its steps do: navigation, waits and reads are `read`; any input step
(click, press, type, fill, select, upload) is `commit`, unless the human owner marked it
`"publishes": false` (then `draft`). An agent can never lower a flow below its steps. Every
run carries the grant that created it (the caller's, the schedule creator's, or the owner's
for the panel and CLI), checked when it is queued and again right before it runs; a revoked
or expired grant fails the run before it acts. One approval releases exactly one run.
Details: [src/agentos/runner/README.md](../src/agentos/runner/README.md).

### Threat model (expanded surface)

| Capability | Threat | Blast radius | Mitigation |
|---|---|---|---|
| Terminal (`exec`) | arbitrary command, exfil, lateral move | the lane; a shared lane means env0 | off by default, netns egress-only, no vault plaintext, cgroup caps, full session traced |
| Install (`install`) | malicious/typosquatted package, post-install scripts | overlay: one lane. base: every lane on that image | pinned source + signer allowlist, digest verify, pre-activation scan, installed set is a pinned manifest hash |
| Service | persistent C2 or miner, resource drain | continuous compute and egress | netns-boxed, no inbound, grant TTL + dead-man, reaped on expiry, caps |
| Shared / weak-isolation lane | cross-workload bleed via shared kernel/FS/net | every workload in the lane, up to the node | opt-in with a loud warning, the trifecta invariant below, no bound secret may attach |
| Subscription credential (brain) | full AI-account takeover, quota drain, ban | the user's whole AI account, plus what the brain can do | crown-jewel tier: hardware unseal, RAM-only TTL, resolved to the provider auth header only, quota ceiling, phone re-auth |
| Local model endpoint | unauthenticated server; its output is untrusted | any workload on the netns; injected steering | bind to loopback/netns, tag completions as tool-untrusted |

### Isolation tiers and the trifecta invariant

| Tier | Dials | May hold | Approval |
|---|---|---|---|
| 0 (default) | full isolation | private data + network + untrusted input (contained by taint + no-exfil) | none |
| 1 | shared FS or programs, net still isolated | no bound secret | opt-in |
| 2 (shared) | node-shared, weak isolation | at most one of {private data, network, untrusted input} | opt-in, loud warning |

The hard invariant, a rule generalizing the lethal trifecta from the browser to any
lane: a lane may not simultaneously reach untrusted input, network egress, and private data
or a bound secret, without a human co-sign. Tier 0 is exempt only because tainting and the
no-exfil sink rule already contain it. The forced per-lane egress proxy (drop non-proxied
UDP, remote DNS, host allowlist) now applies to terminals and services too, not just
browsers.

### The advisory-grant fix, secrets, and injection

An attached Claude Code or Codex keeps its own shell and `curl`, so grants would be advisory
if it ran unconfined. The fix is to run the attached agent **inside its lane's network
namespace with no route out except the egress proxy** (default-deny, DNS only through the
proxy, seccomp), which is also the "offload to a node" action. A bare un-offloaded Mac agent
is a weaker, clearly-labelled mode: there, `exec`, `install` and `brain` protections are
advisory, not enforced.

Vault secrets resolve at the node into exactly two sinks, an auth header or a login-form
field on the bound domain, and nowhere else (the **No-Body-Injection rule**, since an allowed
host could echo a request body back into a response the model reads). Cookies and subscription
sessions are secrets too. A subscription session sits at the crown-jewel tier with profile
dirs: sealed, bound to `{node, lane, provider}`, decrypted in RAM with a hard TTL, unsealed by
a hardware or Touch-ID key, resolved only into the provider auth header, never into the model.

New injection vectors (terminal output, installed-program output, and model responses
themselves) are all tagged tool-untrusted and fed to the taint engine, so a package banner or
a poisoned completion is treated like scraped web text. Browser page text is tagged untrusted
the same way, and a navigation is a sink: once untrusted content and private data are both in
the grant family, opening a URL outside the egress allowlist waits for a co-sign, because the
URL itself can carry the data out. Every URL check (policy and node) uses one WHATWG-style
parser and refuses strings a browser could read as another host. A hijacked brain is still contained by
its grant: it cannot exceed `read` on an off-list host, spend past budget, touch an unbound
secret, or exfiltrate its own session token, because the token was never in its context.

## 6. Vault and secrets

A sealed store on the node: items are encrypted with ChaCha20-Poly1305 under a node key in
`secrets/vault.key` (`src/agentos/vault/crypto.py`; `age` is used for backups only). It
imports Bitwarden JSON, 1Password and Apple Passwords CSV exports. Secrets are bound to a
domain and a lane. Per-lane keys, a Touch ID or hardware unseal and the hosted
zero-knowledge sync are planned ([Not built yet](#not-built-yet)).

```
agent: "post to instagram.com"   (no secret in the call)
  -> node enforcer: grant check (offline: pubkey + revocation set)
  -> egress forwarder: host allowed? taint ok?
  -> secret broker: match {domain, lane} -> inject into Cookie / form field ONLY
  -> instagram.com         response body -> scrubbed -> model
```

One-time codes: TOTP seeds live in the vault and the node computes the code locally; SMS and
email codes are caught by a code-catcher (an IMAP mailbox the node reads) and injected into
the field. The model sees "code entered", never the code.

## 7. The MCP surface

An agent adds one URL and gets a computer. Every tool is snake_case. Terminal and browser tools
called without `on` go to the caller's environment (section 0: a session's own, else the agent's
only open one); every acting tool also takes an `on` selector, an environment's title or `env/<id>`,
or the lane identity `workspace/lane` (a browser lane's name encodes its
account, for example `biz-a/instagram`; a second account is `biz-a/instagram-2`, and the
handle lives in `LaneSpec.account`).

[TOOLS.md](TOOLS.md) is the generated reference for every tool the node exposes (run
`agentos tools` for the list on your build). The main groups:

- **Environments:** `environment_open` (the title names the computer), `environment`,
  `environment_close`, `environment_save`, `environment_upload`, `environment_download`,
  `environment_forget` (section 0); terminal and browser calls without `on` go to the caller's
  environment.
- **Lanes:** `whoami`, `browsers_list`, `browser_open`, `browser_close`, `browser_view` (a signed link
  to watch or take over), `browser_claim` and `browser_release`, `browser_check`, `terminal_exec`,
  `terminal_install`.
- **Browser:** `browser_navigate`, `browser_snapshot`, `browser_act`, `browser_wait`,
  `browser_screenshot`, `browser_scroll`, `browser_tabs`, `browser_upload`, dialogs.
- **Devtools:** `browser_console` and `browser_network`, a page's console errors and requests,
  for an agent testing a web app (secrets in headers, URLs and bodies masked; docs/DEVELOPING.md).
- **Logins and secrets:** `login`, `vault_fill`, `vault_list`, `browser_login_request`.
- **Approvals and people:** `approval_wait`, `human_ask`,
  `browser_wait_handback`.
- **Automation:** flows (`flow_record_start`, `flow_save`, `flow_run`, one `flow_<name>`
  tool per saved flow), workflows, triggers, schedules, brains (`brain_run`, `brain_use`),
  connectors, memory, mail, files, traces.

Key choices:

- **Snapshots return an accessibility outline with refs** (`[ref=e12]`), and an action can
  return only the lines it changed (`snapshot="delta"`); `browser_screenshot` is the
  explicit escape hatch.
- **The grant projects the surface.** A tool a grant cannot use (for example `terminal_exec`
  with no `exec` grant) is left out of the list.
- **Gated actions return `approval_pending`, not an error:** the agent tells the human, waits
  with `approval_wait`, then calls again with the same arguments.

Local agents attach over localhost (an SSH tunnel from another computer) or through a
controller's `/mcp`. The gateway holds no secret values, only grants and secret references
the node resolves locally.

## 8. Data model and execution

**Entities:** Node, Capability, Workspace, Account/Identity (1:1 with a browser Lane), Lane
(with kind + IsolationProfile), Program (and base-image versions), Service, Proxy,
Engine/FingerprintProfile, VaultItem, Brain (and SubscriptionSeat), Grant, Worker, Flow (with
immutable FlowVersions carrying a readback spec and hash), Run, Task (the durable journaled
step), TraceEvent (append-only, redacted at write), Approval, Schedule/Trigger (cron,
page-watch, webhook, with a dead-man cadence).

**Where state lives:** config and authority on the controller; profile dirs, cookies, live
browser state, vault plaintext (RAM only), and subscription sessions on the node and never on
the controller (which holds only references and checksums); the durable run queue and task
journal node-authoritative (SQLite WAL); runs and redacted traces written locally first and
pushed on reconnect.

**Run lifecycle with proof-of-done:**

```
trigger (cron@node | page-watch diff | webhook)   fires LOCALLY, even if controller offline
  -> Run created in the durable queue (idempotency key set)
  -> wake lane, resolve grant (cached, hard-expiry, fail-closed)
  -> steps: vault resolves AT NODE -> redact -> TraceEvent (local)
  -> READBACK (mandatory): re-fetch the live post URL / order id / visible state
       ok -> succeeded-verified + proof | missing -> succeeded-unverified (re-check later)
       fails -> failed | partial
  -> push Run + redacted traces on reconnect; dead-man alerts the phone if a run goes missing
```

A run cannot claim verified success without a satisfied readback, the direct answer to
agents that falsely report "done". Status is honest everywhere: `succeeded-verified`,
`succeeded-unverified`, `partial`, `failed`, never a bare "done".

**Scheduling** is node-local: each schedule is pinned to the lane's node and runs on a
SQLite-journaled durable runner (deterministic replay, durable timers, idempotency keys), so
posting and reply-watching survive controller and laptop downtime and a crash resumes
mid-flow without double-posting. The controller coordinates only unpinned work and mirrors
for the panel. Profile dirs are backed up as encrypted snapshots to a destination you
control; migrating self-hosted to hosted is a controller swap, not a data move.

## 9. UX surfaces

- **MCP** is the main surface: the intro an agent reads first leads with the environment, and the
  Claude Code skills (`agentos-environment`, `agentos-studio`, `agentos-lanes`) teach the loops.
- **The web app** (`web/`): Home, Needs you, Runs, Checks, Brand, Machines and the environment
  pages (commands, calls, ports, git, cost, extend and close), in light and dark
  ([WEB-NODE.md](WEB-NODE.md)). Not in the public repository yet.
- **The live view**: a signed link to watch a lane's browser and take control, served by the node
  (`src/agentos/node/viewer/`).
- **The CLI and the terminal dashboard** (`agentos open --terminal`): the same facts without a
  browser.

## 10. Open source vs hosted

Open source is Apache-2.0 and fully useful to one self-hoster: the node, environments on your own
provider account or Docker, lanes, the vault, grants, MCP, studio, brains on your own
subscription, no feature gates. A hosted edition (separate, compiled out of the open-source build)
adds only what is hosted, multi-party or costs money: VMs without your own provider account, a
multi-tenant control plane, hosted push and phone approvals, teams with SSO, managed proxies for
account lanes. Pricing never counts runs or executions.

## 11. What agentos is not

The spine is general; the product stays sharp. Five lines agentos will not cross, each of
which also protects a core invariant:

1. **Not a general PaaS.** Services keep your agent's own work alive, not arbitrary public
   apps. Nodes dial out only, so there is no public ingress, and adding one breaks the core.
2. **Not a CI/CD system.** An environment runs what the agent runs, for one chat; it is not
   pipelines or a runner fleet, and it is deleted when the work is pushed.
3. **Not a model marketplace or reseller.** Brains are BYO subscriptions and local models;
   agentos never resells tokens, which would break the BYO constraint and provider terms.
4. **Not a flow or skill marketplace.** Flows stay first-party and reviewed. An open store is
   a supply-chain attack surface for no gain to a single self-hoster.
5. **Not an agent, chat, or IDE.** agentos gives agents a computer; it never becomes the
   agent. Blurring that makes it one more agent to choose instead of the body they all share.

## 12. Key decisions and open questions

1. Nodes dial out over one WebSocket to the controller; no overlay network, no inbound port.
   (An embedded mesh and relay were the original plan; see Not built yet.)
2. The controller relays MCP but stays off the bulk-data path (artifacts stay on the node and
   travel on streams), one audit and quota chokepoint without a bandwidth bottleneck.
3. Signed, attenuable grants of agentos's own (Ed25519 block chains), for offline public-key
   verification and provable attenuation.
4. Generalize `Lane` to carry a kind and a four-dial isolation profile; the browser account
   is the all-on preset, and existing rows migrate in place. Keep the word "Lane".
5. `env0` is the node itself; isolated lanes overlay on it. Shared programs are baked,
   hashed, versioned images; isolated programs are overlay installs.
6. Terminals and services are workloads reusing leases, the live-view channel and the durable
   runner. Services are a first-class always-warm class outside LRU.
7. A brain is a `service` lane and a Worker; a subscription is a leased seat; the router is
   privacy-floor then subscription-first then local then metered. Brains are an optional
   plugin, self-host BYO only, never pooled in hosted.
8. Isolation by trust: process on a home node, container on a cloud node, microVM for hosted
   multi-tenant. Full isolation is the default; weaker presets are opt-in behind the trifecta
   invariant.
9. Node-local durable runner, schedules pinned to the lane's node, so work survives downtime.
10. Proof-of-done readback is mandatory and defaults to unverified, so success is honest.
11. Attached agents run inside the lane's netns so grants are enforced, not advisory.
12. The No-Body-Injection rule and node-authored approval text, so a compromised gateway leaks
    nothing and a hijacked model cannot social-engineer approval.
13. An environment is a cloud VM per chat, deleted when the work is pushed. Nothing of agentos
    runs on it and it holds no account and no key, only a gateway token for its own repository, so
    commands on it never ask; git goes through the node's gateway and the browser's traffic through
    the VM. Only the agent that opened it (its grant family) reaches it (owner, 2026-11-13).

Open questions still to settle:

- Agent-to-controller auth: lean OIDC at the gateway minting the root grant downstream.
- Lane identity across account renames needs a migration story (immutable ids, display
  aliases); a `MigrateLane` verb is reserved.
- A home node going offline takes its lanes with it; lanes pin and alert rather than silently
  failing over (which would change IP and fingerprint).
- Subscription concurrency and rate windows are opaque; the seat model is tuned from observed
  throttling and over-driving risks a ban on the real account.
- Taint labels decay once a brain summarizes tainted input, and model completions are
  themselves tainted, so over-blocking compounds.
- Shared (tier 2) lanes and a bare un-offloaded Mac agent are weaker by construction; the UI
  must say so, because a user can still opt in and footgun.
- A rooted node loses its subscription session; hardware unseal and quota caps cap the loss
  but do not prevent it. The code-catcher (OTP) must live on the node, never the controller.
- Providers may block node-hosted subscription use; the plugin boundary limits the blast
  radius to that layer.
- macOS nodes cannot run fully isolated Linux lanes; the degraded profile needs to be explicit.

## 13. Layers

The packages of `src/agentos` sit on six layers. A module imports its own layer and the ones
below it, never one above, and packages on one layer never import each other in a circle.
`make layers` checks it (scripts/layers.py, the order in scripts/layers.toml); the imports that
still break it are listed there with a reason, and that list only shrinks. Top to bottom:

1. **Entry points**: `cli`, `commands`, `cli_groups`, `tui`, `client`, `dev`, `controller`, and every
   package's `cli.py`; `node/daemon.py` (the composition root).
2. **Surfaces**: `api`, `mcp`, every package's HTTP routes and pages (`api.py`, `form.py`,
   `forms.py`), and `node/channel/`, the controller's API into the node (it relays workflow runs,
   logs and the doctor's checks, so it sits above the features, like the panel's API).
3. **Features**: `environments`, `studio`, `profiles`, `brains`, `memory`, `runner`, `agenda`,
   `pilot`, `keepalive`, `workspaces`, `textreview`, `plugins`, `webhooks`, `events`, `agents`,
   and the services that act across them: `ops`, `freeze`, `audit`.
4. **Node runtime**: `node`, `env`, `vms`, `git`, `stealth`, `vault`, `webbotauth`, `runas`.
5. **Safety**: `policy` (grants, approvals, traces, scrubbing).
6. **Core**: `core`, the contracts and small shared pieces (config, errors, models, tools,
   layout, migrations, clock). It imports nothing else of agentos.

[src/agentos/README.md](../src/agentos/README.md) says where things live.

## Not built yet

Designed, written about above or in [design/](design/), and **not in the code today**. Each
one is a plan, not a promise.

- **Mesh networking:** a WireGuard overlay between nodes, an embedded Headscale-style
  coordinator and a NAT relay. Nodes talk only to the controller, over one WebSocket.
- **gRPC control channel** (`Enroll`, `Heartbeat -> {assignments, grantUpdates, leasedJobs}`,
  `InvokeTool`). The WebSocket messages in PROTOCOL.md do this job; a heartbeat gets no
  assignments back.
- **Controller-side scheduling and model routing:** the controller has no scheduler, no
  coordinator for unpinned work, no model router and no vault custodian. Schedules, the
  router and the vault are all node-side.
- **Biscuit grants with a Datalog authorizer, and an OAuth 2.1 resource server** (with CIMD
  for phone apps). Grants are agentos's own signed tokens, sent as a bearer token.
- **Typed flow tools listed lazily** (`flow:bizA.instagram.post(image, caption)`). Today each
  saved flow is one `flow_<name>` tool, and every tool is listed.
- **Engines other than Camoufox:** Chrome (Patchright) and Android (redroid) lanes, WebDriver
  BiDi.
- **Isolation:** microVM-backed `locked` lanes, `io.max` cgroup limits, always-warm
  `keep_warm` services outside the LRU.
- **Vault:** per-lane encryption keys, a Touch ID or hardware unseal, Apple Credential
  Exchange import, SMS code forwarding from a phone node, zero-knowledge sync.
- **Seats:** a token bucket per subscription window (today a slot count and a rolling
  estimate).
- **Data:** a Postgres option for self-hosters, and Worker and SubscriptionSeat as stored
  entities.
- **Surfaces:** the Mac menu-bar app (Touch ID approvals, "offload this session").
- **Environments:** files in and out other than through commands and git, a warm pool of VMs,
  providers billed by the minute ([design/environments.md](design/environments.md)).
