# Disposable cloud VMs as lanes

Status: phase 1 built (agentos.vms), not yet run against a real Hetzner project.

## The story

"In T3 (or Claude Code), I say: test this on a fresh VM." The agent calls `vm_create`, gets a lane,
runs the work with `lane_exec` or a shell, copies out what it needs, and calls `vm_destroy`. If it
forgets, crashes, or the laptop sleeps, the node deletes the VM anyway. Nothing bills for long.

## Why the node does it, not a Hetzner MCP in the agent

- **The token stays on the node.** A Hetzner token is per project and either read or read-write.
  In an agent it could delete every server in the project (including the node itself) or make fifty.
  The node exposes only "a VM of a profile the owner set up, for at most this long" and "delete a VM
  this node made".
- **The reaper outlives the agent.** Deletion must not depend on the session that asked.
- **One policy.** The `vm` effect goes through grants, approvals, standing approvals, freezes,
  taint and the audit log like everything else.
- **Every agent gets it.** Claude Code, Codex, T3, Hermes, the node's brain.
- **Keys never reach the model.** SSH keys are per VM, made and kept on the node.

## Shape

A VM is a **terminal lane whose commands run over SSH** (`EnvLane.remote`). The env manager picks
`SshLauncher` for such lanes, so `lane_exec`, `shell_*`, `lane_install` and service lanes work on it
unchanged. Nothing of agentos runs on the VM: no agentosd, no token, no vault. Dev work runs code
that agentos should not trust (install scripts, a PR's tests), so agentos stays off that box.

The other option, a VM that joins the controller as a node, fits long-lived capacity (more browser
lanes, a company's lanes on their own IP) and already exists as node templates plus `node join`.
Both share the provider client and the reaper's rules; the agent only ever sees a lane id.

## Size: the cheapest that fits

Unless the owner says what they need, the node rents the cheapest server type that fits. A profile
in vms.toml is a set of minimums (`min_cpu`, `min_memory_gb`, `min_disk_gb`, `arch`, `cpu_type`);
`small` has none, so it is the cheapest x86 type of the day. At create time the node reads the
provider's types and prices (cached 30 minutes), keeps those orderable in the location now (and
matching a snapshot's architecture and disk), and takes the cheapest. Sold out: the next two
cheapest, never more than twice the price, all named in the approval. A profile with
`server_type` pins one type, no picking.

An agent can state its own size on `vm_create`: `cpu`, `memory_gb`, `disk_gb`, `arch` (minimums,
picked again) or `server_type` (pinned). Its tool text tells it to leave the size alone unless the
owner asked or the task needs it. Such a VM's profile is `custom`, which a grant limited to
profiles refuses. `agentos vm types` lists what can be ordered and what each profile rents today.

## Creating one

1. Policy: `vm` effect on the lane, with the summary the node writes (server type and its specs,
   location, image, time limit, hourly price, the most it can cost, the sold-out fallbacks).
   Default routing: a person approves; a standing approval may cover it. A grant's `vm` cap can
   limit lanes (`dev/*`), profiles and repos.
2. Node limits (vms.toml, enforced whatever the grant says): `max_live`, `max_ttl_minutes`,
   `monthly_budget_eur` (counted as started hours times the hourly price).
3. Keys: an ed25519 client key and an ed25519 **host key** made on the node. The host key goes to
   the VM through cloud-init (`ssh_keys`), so the first connection is already pinned
   (StrictHostKeyChecking=yes, a one-line known_hosts under a HostKeyAlias).
4. Provider: a firewall per node (SSH and ping only from this node's public IP), a throwaway SSH key
   resource (deleted right after the server exists), the server with labels `agentos-node`,
   `agentos-vm`, `agentos-lane`.
5. Boot: wait for SSH with the pinned key (default up to 300 s), then register the VM lane.
   A VM that never answers is deleted and marked failed.

## Deleting one (the reaper, every minute)

- past its time limit;
- idle (no command, no open shell or service for `idle_minutes`) in the last
  `hour_margin_minutes` of a started hour: Hetzner bills by the started hour, so deleting earlier
  saves nothing, and the next task of the same agent can still use it;
- `vm_destroy`, `agentos vm destroy`;
- a server labelled with this node and no live record: an orphan, deleted (after 10 minutes'
  grace while a create is in flight);
- a record whose server is gone: marked deleted, its lane closed.

Deletes retry with backoff until the provider confirms. A VM still alive 10 minutes past its limit
sends one urgent notification. A daemon restart never deletes VMs; the reaper resumes.

## Private repos

`vm_clone(on, repo, ref, dest, depth)` puts a private repo on a VM without the token ever reaching
it. The owner stores a read-only token per repo or per owner with `agentos vm repo add
github.com/acme/*` (GitHub: a fine-grained token, only those repos, "Contents: read"). It is a
vault machine secret; listing shows patterns, never tokens.

The node clones into a private temp dir with the token in git's environment (GIT_ASKPASS), never in
the URL, argv or .git/config, then streams the checkout (with its .git) to the VM as a tar over the
VM's SSH and deletes its copy. `origin` on the VM is the plain https URL. Only https remotes on
public host names (no IPs, localhost, file://, ext:: or submodules), so an agent cannot point the
node at its own network. Limits: `clone_max_mb` (2048), `clone_timeout_s` (600).

Policy: the `vm` effect again, with the repo as a fact; a grant's `repos` (`--vm-repo
github.com/acme/*`) limits which. The approval says the code lands on a box with internet access.
A public repo needs none of this: the agent runs git clone on the VM.

## Images

`agentos vm image build dev` makes a snapshot (git, build tools, Python with uv, Node 22, Docker),
cleaned of keys and cloud-init state. A profile with `image = "snapshot:dev"` then boots ready
in under a minute instead of installing on every VM.

## Settings (`$AGENTOS_HOME/vms.toml`)

```toml
location = "nbg1"
default_profile = "small"
max_live = 3
default_ttl_minutes = 60
max_ttl_minutes = 1440
idle_minutes = 15
hour_margin_minutes = 5
monthly_budget_eur = 50
ssh_from = []          # empty: this node's public IP, looked up

[profiles.small]          # no minimums: the cheapest x86 type of the day
image = "ubuntu-24.04"

[profiles.build]
min_cpu = 8
min_memory_gb = 16
image = "snapshot:dev"

[profiles.arm]
arch = "arm"
min_memory_gb = 8

[profiles.pinned]
server_type = "ccx23"     # exactly this type (`agentos vm types` lists them)
```

The token lives in `$AGENTOS_HOME/secrets/hetzner.token` (0600), written by `agentos vm setup`. Use
a Hetzner project that holds nothing but these VMs: the token can delete any server in it.

## Not in phase 1

- Copying files in and out other than through commands (`lane_exec` with stdin, `git`): a
  `files_put`/`files_get` for VM lanes over SFTP.
- Reusing an idle VM for another task of the same grant family, and a warm pool.
- A panel tile and a doctor line for live VMs and the month's spend; the meters do not count VM
  spend yet (it is in EUR and has its own monthly budget).
- Other providers (Scaleway bills by the minute), and VMs as controller nodes for capacity.
- Killing a remote command when its ssh dies without a TTY (the VM is disposable, so for now the
  process lives until the VM goes).
