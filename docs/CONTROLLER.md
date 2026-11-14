# The controller

The open-source controller gives your agents one MCP URL for every node you run. Nodes
dial out to it over one WebSocket ([PROTOCOL.md](PROTOCOL.md)), so a box behind your home
router works like a cloud VM. The hosted cloud speaks the same protocol, so a node points
at either one by changing a URL and a join token.

Without a controller nothing changes: each node still serves MCP on its own localhost port
and you tunnel to it (`agentos connect <ssh-host>`).

## One box: `agentos up`

On the node:

```
agentos up                   # the first time it also writes node.toml (as node init) and controller.toml
```

`agentos up` starts the controller (settings in `$AGENTOS_HOME/controller/controller.toml`,
created on the first run), mints a join token in-process, enrolls the node over the real
WebSocket to localhost, and runs the node daemon. On a new box it first writes node.toml as
`agentos node init` does (on the next free port when something already listens on 7400). It
prints the panel, a sign-in link that works once (10 minutes; `agentos controller login` makes
another) and the line that connects your computer, for example:

```
agentos is up: controller and node box-1, joined.
  Panel:    http://localhost:7401
  Sign in:  http://localhost:7401/login#code=...
            (works once, for 10 minutes; `agentos controller login` makes another)
  Connect:  agentos connect mynode --remote-home /var/lib/agentos/controller --name controller   (on your computer)
```

The tunnel and `claude mcp add` lines, for connecting by hand, are `agentos connect --by-hand`
(run it on the box). At a terminal the log stays in `logs/agentosd.jsonl` (`agentos node logs`);
`-v` shows it as it comes.

`agentos connect` works against the controller as against a node: the controller home has a
small `node.toml` holding only `name`, `kind = "controller"`, `mcp_port` and `mcp_token` (the
agent token), which is what connect reads over SSH. Unlike a node's token, the controller's
agent token is already the agents' own credential (the controller's owner token stays in
controller.toml), so connect keeps it in the Keychain as it is and asks for no agent grant
or owner credential: you decide in the controller's panel. Run `up` again any time; the node
keeps its identity and the controller keeps its keys.

The node's own port (7400 by default) and the controller's (7401) must differ. Both bind to
127.0.0.1.

## Running them apart

```
agentos controller init --port 7401           # controller.toml with an agent token
agentos controller serve                      # agents on /mcp, nodes on /v1/node/connect
agentos controller join-token --name box-2    # single use, one hour
agentos node join --self-hosted <controller-url> <token>    # on the node, then restart agentosd
```

Other commands: `agentos controller login` (a one-time sign-in link for the panel),
`agentos controller nodes`, `agentos controller agent-token [--new]` (show or rotate the
agent token).

## Joining a second node

A node on another machine needs a controller URL it can reach, and TLS: plain `ws://` is
refused unless the controller is on the same machine. Two ways:

- **Behind TLS.** Put the controller behind a reverse proxy that terminates TLS (Caddy,
  nginx, a Tailscale serve), set `public_url = "wss://controller.example.com"` and
  `behind_tls_proxy = true` in controller.toml (or give it `tls_certfile` and `tls_keyfile`
  and bind beyond loopback). Then `agentos controller join-token` prints a line with the
  wss URL to run on the new node. The node verifies the certificate with the system CA store
  (set `SSL_CERT_FILE` for a private CA).
- **Over SSH.** On the new node keep a tunnel to the controller box
  (`ssh -N -L 7401:localhost:7401 controllerbox`) and join `ws://localhost:7401`.

Then, on the new node:

```
agentos node join --self-hosted wss://controller.example.com ajt_...
systemctl restart agentosd
```

The node makes its key (`secrets/node.key`, mode 600), enrolls with a signature by that key over
the connection's challenge (the controller refuses an enrollment without one, so a node older
than this proof must be updated before it joins), and pins the controller's key and the org's
grant key in node.toml. For your own controller add `--self-hosted`, or the node
keeps the hosted controller ceiling (see "The controller ceiling"). From then on agentosd keeps the channel up: it
authenticates with a signed challenge, sends heartbeats, and reconnects with backoff when
the controller goes away. Joining another controller later (the hosted cloud, for example)
is the same command with its URL and a token from it.

## Agents and lane ids

Agents send `Authorization: Bearer <agent token>`. The token in controller.toml is the
owner's agent (every workspace, up to `irreversible`, approvals as the nodes route them). Make
more in the panel (Agents, New agent token) or with `POST /api/grants`, scoped to workspaces,
lanes and a ceiling; revoking one reaches every node at once. The controller stores only token
hashes, apart from the owner's agent token in controller.toml, which `agentos connect` reads.

The tool list is the union of the tools of the org's online nodes, filtered by the agent's
grant. Each tool keeps the node's schema plus an optional `node` argument. Calls route like
this:

- A lane id is `workspace/name`, the id the node uses. When one online node has the lane,
  that id routes to it.
- When two nodes have the same lane id, the plain id is ambiguous and the call fails with
  `invalid`, naming the ids to use instead: `node/workspace/name`, the node's name first. The
  node receives the plain id.
- A tool without a lane routes by `node`, else by an `approval_id` the controller has seen,
  else to the only node that offers it. Otherwise the call fails and says which nodes do.
- `nodes_list` (the controller's own tool) lists the nodes and their lanes, with each lane
  shown under the id to use.

The list starts small, as on a node ([TOOLS.md](TOOLS.md#toolsets)): `nodes_list`, the `core`
toolset and the meta tools `tools_more` (lists the other toolsets, and loads one into the MCP
session) and `tools_call` (calls any tool by name). `/mcp?toolsets=files,flows` asks for more
toolsets, `/mcp?toolsets=full` for every tool, and `mcp_toolsets` in controller.toml sets the
default (`["core"]`). `agentos connect <controller-host> --toolsets ...` writes the URL for you.
A toolset changes only the list: a call through `tools_call` takes the same grant check and
routing as a direct one. Listed schemas are leaned as a node leans its own (no generated
titles, no null branch on an optional argument).

For every call the controller mints a short-lived grant token signed with the org's grant
key and sends `invoke`. The node verifies that grant itself and runs the call through the
same policy wrapper as a local call: scope, ceiling, budget, routing and approvals are
decided on the node. The controller's filtering is a convenience, never the enforcement.

## Approvals, activity and live view

Approvals start on nodes. The controller queues them (the panel's Needs you) and sends the
owner's decision back with the approval's content hash; the node refuses a decision whose
hash does not match. The agent retries its call once approved, as it would on a node.

Approving lets an action run, so the node's controller ceiling (below) applies to it: the
controller may approve only an action whose effect it could have used itself (up to
`controller_max_effect`, and a special only when `controller_specials` lists it). Above that,
the node answers `denied: controller_ceiling: ...` and the approval stays pending until the
owner decides it on the node: its panel, a push from the node, or `agentos approvals`. A denial
from the controller always passes. A decision from the controller names no grant, so the node
records it as `controller:<who>` and the grant family rule (a family never approves its own
call) has nothing to apply to.

A member acting in the cloud panel reaches the node with a per-call grant whose subject is
that person (policy/TOKENS.md, "A member's call from a controller"). On the node it is a person
acting remotely, never one at the node: the lane gates hold it like an agent (a take over, Stop
agent, a freeze, claims, egress lists), and what needs a person asks for an approval under the
member. The controller relays that approval's decision as the owner, so a member can approve
their own call from the cloud panel; the cloud intends this (a signed-in session may approve
what it started), and the node trusts it only within the controller ceiling above. Keep the
ceiling below an effect when a second person must approve a member's actions there.

Traces arrive redacted and batched, and the panel shows them as Activity. Lanes appear with
their state, signal and who has control.

Live view runs as a stream through the controller: the panel's view page plays the lane's
picture with noVNC. Watching is the default. Take control in the page and the node gives the
lane to you (agents get `human_in_control`); only then does your input reach the lane, and it
stops the moment you hand it back. Terminal lanes stream over
`/v1/terminal/{node}/{workspace}/{name}?mode=view|control` the same way: you watch a shell by
default, and what you type reaches it only in control mode after you take the lane, until you
hand it back or stop typing for the node's `control_idle_seconds` (10 minutes when it is 0: a
terminal hold always ends).

## What the controller can and cannot see

The controller sees metadata: node names, versions, load, lanes (id, kind, state, signal,
control), tool definitions, approval summaries and previews (written by the node from what
it will actually do), redacted traces, and the results of the calls it relays.

It never holds vault entries, cookies, proxy credentials, subscription tokens, browser
profiles or exit IPs: no message carries them, and node-supplied data is checked and cut
short before it is stored (an `exit_ip` is dropped whatever a node sends). It stores join
and agent tokens only as hashes.

What it can do is bounded by the nodes: a grant it signs is verified on the node, can never
carry admin rights there, and is refused once revoked; a decision must match the content
hash; `revoke`, `config_update` and `lane_control` must carry the controller's signature,
pinned at enrollment. While the controller is unreachable a node finishes in-flight work on
cached grants, buffers events, traces and approval requests on disk, and refuses grants it
has never seen.

What it is trusted with: it relays the owner's live view and terminal, and a human at the
controller can take a lane over, within the node's controller ceiling (below). A controller that
someone else controls could do what the ceiling lets it do, so run it where only you can sign in,
and keep TLS on.

## The controller ceiling

Each node's owner decides, on the node, what any controller may do there. The controller holds
the org's grant key and signs `lane_control`, so a breach of the controller must not reach past
what the owner allowed. Three settings in node.toml (`agentos config example` lists them):

| Setting | Default | What it bounds |
|---|---|---|
| `controller_max_effect` | `"commit"` | the highest effect a grant from the controller may use here |
| `controller_specials` | `[]` | which of `secret`, `exec`, `install_overlay`, `install_base`, `service`, `brain`, `memory` it may use (an allowlist: a special it does not name, including one added later, is refused) |
| `controller_streams` | `["view"]` | `view` (watch a live view; read a lane's screenshot, an approval's preview and a run's proof as `file` streams), `control` (take a lane over, type into a live view or a terminal), `terminal` (attach to a shell), `logs` (read the node's own log lines, redacted on the node; off by default) |

A grant from the controller is clamped to it: a call above it is refused with `denied:
controller_ceiling: ...` whatever the grant says. `lane_control` `take` needs `control`, and a
stream the ceiling does not allow is refused before it opens. Typing into a terminal runs
commands, so a terminal in control mode needs `terminal` and `control` in `controller_streams`
and `exec` in `controller_specials`, as the MCP shell tools need `exec`; watching a terminal's
output needs only `terminal`. The node's own grants (`agentos
grants issue`, `agentos connect`) are not affected. The node reports its ceiling when it connects
(`hello.ceiling`), and `GET /api/nodes` shows it as `ceiling`. The controller can never raise it:
edit node.toml on the node and restart agentosd.

### Standing approvals from the controller

A standing approval the controller sends (`POST /api/nodes/{node_id}/standing`, the `standing`
message in docs/PROTOCOL.md) lets an agent post without a person approving each post. It is
bounded the same way: one for an effect above the ceiling is refused with `controller_ceiling`,
and one already held stops covering calls once the ceiling no longer allows it. Only `read`,
`draft` and `commit` can be sent, never a special. This controller keeps what each node should
hold and the revokes the node has not confirmed: it signs what you send, passes it to that node
and returns which ones the node applied and which it refused, with the reason (an item refused is
dropped from what the node should hold). For a node that is offline the answer is `queued: true`,
and the node gets the whole set, and the revokes it missed, in its next welcome. Each use comes
back as a `standing` event in the activity.

**What the node trusts.** The node cannot check that you, the owner, agreed to a standing
approval the controller sends. The controller's signature is the only evidence. So a breached
controller can create standing approvals, within the ceiling. That is no wider than what it could
already do, since it can approve calls within the ceiling one by one (`commit` by default). To
bound the damage, the node's owner sets these in node.toml, and the controller cannot change them:

| Setting | Default | What it bounds |
|---|---|---|
| `controller_standing_max_count` | `50` | the uses one standing approval may cover (`over_limit`) |
| `controller_standing_max_active` | `200` | the unused uses this controller's standing approvals may hold at once in one workspace (`over_limit`) |
| `controller_standing_max_days` | `30` | how long one may run (at most 90; `too_long`) |
| `controller_standing_confirm_hours` | `24` | how long one keeps covering calls after the controller last confirmed it |

An update is checked again, so a controller cannot refill a count or push `until` past them, and
an update the node refuses withdraws the version it held. The controller confirms what it sent
by sending its whole set (every welcome does), and while it stays connected after that; a
revoke sent while the node was offline therefore has a bounded life even if it is lost. Nothing
is added while a freeze covers it (`frozen`), and a freeze revokes for good what reaches only its
scope (a node freeze: everything). A
node that joins another org forgets this org's grant key and revokes every standing approval it
sent (`agentos node join` says so).

Two things the ceiling does not gate. `lane_control` `stop_agent` and `deny_request` pass
whatever it says: they only reduce what agents do on a lane (pause the agent, refuse its request
for control back), never what the controller can do there. And while the node's policy.toml has
`accept_v1_tokens = true` (the default during the token migration), a v1 grant from the org
(`agt1_`) carries no `aud`, so it is not bound to one node: it works on every node of the org
that trusts that grant key, each clamped to its own ceiling. This controller mints v2 tokens
with an audience for every call, so turning `accept_v1_tokens` off binds every grant the node
accepts to its audience.

The defaults are for a controller someone else runs, the hosted cloud: posting up to commit, no
specials, live views to watch only. A controller you run yourself gets the full ceiling (every
effect and special, view, control and terminal):

- `agentos up` writes it to node.toml when it joins the node, since the controller runs in the
  same process on the same box, as the same owner. A node joined by `up` before the ceiling existed
  gets it at the next `up`. A ceiling you lowered afterwards stays as you set it.
- `agentos node join --self-hosted <url> <token>` writes it for your own controller on another
  machine. Without `--self-hosted`, `agentos node join` writes the hosted default, so a ceiling
  meant for one controller never carries over to the next.

node.toml is saved whole on any change, so `controller_ceiling_by` records who set the ceiling:
`default` (nobody), `join` (`node join` without `--self-hosted`), `self_hosted` (`agentos up` or
`--self-hosted`) or `owner`. `agentos up` writes the full ceiling only over `join`, or over
`default` while the values are still the hosted default (a node from before the ceiling). A
ceiling you edit by hand keeps its marker and stays as you set it; write
`controller_ceiling_by = "owner"` to keep even the hosted default through `up`.

## Files and ports

| | Where | Mode |
|---|---|---|
| Settings | `<controller home>/controller.toml` | 600 |
| Connect stub for `agentos connect` | `<controller home>/node.toml` | 600 |
| Database (orgs, join tokens, nodes, lanes, grants, approvals, traces) | `<controller home>/controller.db` | 600 |
| Panel sign-in codes and sessions | `<controller home>/panel.db` | 600 |
| Controller key, org grant keys | `<controller home>/secrets/` | 600 |
| Node key | `$AGENTOS_HOME/secrets/node.key` | 600 |
| Node outbox and call cache | `$AGENTOS_HOME/channel.db` | 600 |

The controller home is `AGENTOS_CONTROLLER_HOME`, by default `$AGENTOS_HOME/controller`.
Requests whose Host is not localhost, the host of `public_url` or one of
`extra_allowed_hosts` get 421, on every route.

## Owner API

Sign in with `agentos controller login`, or send `Authorization: Bearer <owner_token>` from
controller.toml. Cookie requests that change anything need the `X-Agentos-CSRF` header from
`GET /api/session`. Errors are `{ "error": { "code", "message" } }`.

```
GET    /api/status
GET    /api/nodes                                  DELETE /api/nodes/{node_id}
GET    /api/join-tokens                            POST   /api/join-tokens {name?, capabilities?, ttl_minutes?}
GET    /api/approvals?state=pending                POST   /api/approvals/{node_id}/{approval_id} {decision, answer?}
GET    /api/activity?limit=
GET    /api/grants                                 POST   /api/grants {subject, workspaces?, lanes?, ceiling?, ttl_days?}
DELETE /api/grants/{grant_id}                      (revokes it and its children on every node)
POST   /api/nodes/{node_id}/lanes/{ws}/{name}/control  {action: take|hand_back|stop_agent|deny_request}
POST   /api/nodes/{node_id}/config                 {config: {heartbeat_seconds}}
POST   /api/nodes/{node_id}/standing               {set | upsert, revoke}  (standing approvals; queued while offline)
GET    /api/freeze                                 POST   /api/freeze {reason}   (the mesh freeze)
POST   /api/unfreeze                               (lifts only the controller's own mesh freeze)
```

## Mesh freeze

`agentos controller freeze --reason "..."` (or `POST /api/freeze`) freezes every node of the org.
Online nodes get a signed `mesh_freeze` at once; a node that is offline gets it inside its next
`welcome`, before anything the controller sends runs there (`agentos controller freeze-status`
lists the nodes still pending). Every agent grant of the org is revoked, the owner's agent token
included (`agentos controller agent-token --new` makes another), and the controller refuses MCP
calls while the mesh is frozen. `agentos controller unfreeze` lifts the mesh freeze on every node;
a freeze made on a node itself stays until that node's owner lifts it. Nothing is restored.

### What a lane or workspace freeze reaches

A wider grant that a lane or workspace freeze keeps (one on biz-a and biz-b during a freeze of biz-a)
is refused inside the frozen scope however a tool reaches it, not only when the call names a lane
or a top-level `workspace`: an id whose workspace is its prefix (a workflow, a run, a job, a brain
task, an approval, an agenda entry), a definition's workspace (`workflow_save`), a lead list
(`leads_propose`), or a list filtered by workspace. Every scope helper a tool uses asks the freeze
(`PolicyContext.frozen_scope`, through `can_access_lane`, `covers_workspace`, `require_lanes`,
`pattern_within`, `pattern_reaches`, `frozen_off` and `refuse_frozen`). A call that acts on, or
reads one thing in, a frozen scope fails with `frozen`; a listing leaves the frozen scope's rows out
and says so in its `frozen` field (a tool whose answer is a bare list, `env_lanes_list`,
`lane_jobs_list` and `brain_tasks`, leaves them out without the note). A person in person is not
stopped. `tests/unit/test_freeze_reach.py` walks the tool registry, so a new tool that reaches a
workspace another way must say how the freeze reaches it.

Known limit: a terminal job or a service that was already running on a frozen lane when the freeze
began keeps running. The freeze refuses every new command, read and control call for it, but it
does not stop processes that started before. A person stops them in person (the panel).

## Not yet

- One org per controller in the panel (the store and every query are per org already).
- `file` streams and `run` events from the node's runner.
- MCP `tools/list_changed` notifications when a node comes or goes (agents re-list).
- A browser terminal page in the panel (the WebSocket relay is there).
