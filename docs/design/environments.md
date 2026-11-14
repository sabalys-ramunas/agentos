# Environments: a cloud computer for each agent

Status: being built (branch t3/environments). This page is the plan and the contract between the
builders; docs/ENVIRONMENTS.md is the user guide.

## What agentos is

agentos gives every agent its own computer in the cloud. You open a chat, and when the agent
decides it needs one (or you tell it to work in agentos), it asks for an **environment**: a fresh
VM with a terminal, a real browser and your repository on it. It works there, not on your laptop,
commits and pushes a branch, and when it is done the VM is deleted. You can watch its browser and
its commands live, from the web app or from the chat (T3 Code, Claude Code, Codex), and take over.

The trust layer is what makes that safe to leave running: grants say what an agent may do, the
node's limits bound what environments cost, approvals ask you before payments and before acting
on your own accounts (never inside an environment), the vault keeps keys and tokens off the VM,
and the audit log records every call.

Everything else is a feature on top: studio's page checks (`ui_check`, `ui_diff`), profiles with a
brand, logged-in browser lanes for your own accounts and routines.

## The agent's story

1. A chat starts. The MCP intro tells the agent, in its first lines, that it can have its own cloud
   computer: `environment_open(title=..., repo=...)`.
2. `environment_open` rents a VM (the cheapest that fits, unless the owner set a size), clones the
   repo into `/work/<name>` through the node's git gateway, and opens a browser whose traffic goes
   through the VM. It returns the environment, its workdir, and a link for the human: the
   browser's live view, served by the node (no web app page: the public repository has none).
3. Every terminal and browser call **without `on`** goes to the environment of this chat:
   `lane_exec(cmd="npm run dev", cwd=...)`, `lane_job_start`, `shell_open`, `browser_navigate(url=
   "http://localhost:3000")`, `browser_snapshot`, `ui_check(url="http://localhost:3000")`.
   `localhost` in the browser is the VM's localhost.
4. The agent commits and runs `git push origin <branch>` on the VM. The push goes through the
   node's git gateway (the key never reaches the VM; its one gateway token reaches only this
   repository). It never asks a person, the default branch included; a force push or a delete of
   the default branch is refused (`push_refused`).
5. `environment_close()` checks for unsaved work (uncommitted changes or commits not pushed),
   refuses with `unsaved_work` unless `discard=true`, then deletes the VM and the browser.
   If the agent forgets, the node closes it: idle for `idle_minutes` (default 60) or past its time
   limit. A VM with unsaved work is never deleted for idleness; it waits to the time limit and the
   owner gets a notification first.

## Decisions (owner, 2026-11-13)

- An environment is a **cloud VM**, rented by the node (Hetzner first, through `agentos.vms`).
- **One per chat** (MCP session). A new chat starts clean. `environment_open` with the title and the repo of an
  environment that is still starting or ready, from the same grant family, reattaches to it (a
  resumed chat; the same title with another repo is `environment_in_use`, changed 2026-11-13 after a
  real chat got a starting environment back for an open without its repo), but only when the chat
  that has it made no call for 10 minutes; else `environment_in_use`, naming
  that chat's last call. The chat that lost it gets `no_environment` saying so. A chat with none
  hears its family's open ones (id, title, last call). Concurrent opens in one chat get one.
- **Whose it is** (2026-11-13). An environment belongs to the grant family that opened it (its
  root grant, and every grant narrowed from it) and records its opener: grant root, subject, and
  the member whose agent it is, if any. No other grant acts on its lanes, reads it or closes it,
  and to it the environment does not exist (`lane_not_found`, `no environment`); over MCP that
  holds for the node's token too. The owner sees and closes every one from the web app and the
  CLI, a member those of their own agents. Ids are `e` and 10 characters (31^10), not guessable.
- **Deleted when finished**, by the agent or by the reaper.
- **No asking inside** (widened 2026-11-13: "No questions ever if it runs in agents enviroment").
  Nothing in an environment waits for a person, before or after untrusted text: not opening it,
  not commands on the VM, not its browser (`env/<id>`, `env/<id>-browser`: exec, installs, jobs,
  shells, uploads, files, a click the commit gate reads as a publish, studio checks through its
  proxy), not a push. The VM holds only what the work needs. No account, no key; its browser no
  login and no vault entry; one gateway token that fetches and pushes `env.repo` only, good only
  through this VM's own tunnel, revoked when the environment closes or its lane goes down; no
  token at all without a declared repo. It reaches the VM's git config through stdin, never a
  command line on the node. The real guards stay, as limits and refusals (an error with a fix,
  never an approval):
  - Opening one costs money, so the node's limits bound it, and they hold without any command:
    at most `max_live` open at once (3), the longest life (8 hours, `extend` included), and a
    monthly budget that counts open environments to their time limits (EUR 50). Past one:
    `environment_limit`, naming the limit and the command that raises it. `agentos environments
    allow` changes the limits; it grants nothing. The grant still needs the `vm` special for the
    lane. Untrusted text never changes this.
  - A push never asks, on any branch, the default branch included. A force push or a delete of
    the default branch (or a `protect` glob), or moving or deleting a tag, is refused
    (`push_refused`: push a branch and open a pull request).
  - Reading a page in the environment's browser still labels the grant family untrusted, so a
    call on an account lane afterwards (a post on `personal/x`) asks as before. Pay and
    irreversible effects still ask anywhere. `environment_close` with unsaved work stays the
    error `unsaved_work`.
- Socials, browser lanes for accounts and studio stay, as features. The README leads with
  environments.

## How it is built

- **Machine**: a VM from `agentos.vms` (`VmManager.plan/create/destroy`), lane `env/<id>` (an SSH
  terminal lane, so `lane_exec`, `lane_job_*`, `shell_*`, services work unchanged). Image: the
  `dev` snapshot when the node has one, else Ubuntu 24.04 with git, curl, build tools, Node 22 and
  Python with uv installed by cloud-init. Working folder `/work`.
- **Git**: the VM's SSH connection carries a reverse tunnel to a gateway listener of its own on the
  node (127.0.0.1:3129 on the VM goes to it), and the VM's git config sends the environment's repo
  there (`url.<gateway>/github.com/me/shop.insteadOf`) with its token. Clone, fetch and push of
  that repo go through it, declared in workspace `env` (the owner declares `agentos git remote
  allow env github.com/me/*`). Every other repo, and a public repo that is not declared, is fetched
  directly on the VM.
- **Browser**: a browser lane on the node, `env/<id>-browser`, with no account, no vault entry and
  no keepalive, whose proxy is a SOCKS tunnel over the VM's SSH (`ssh -D`). Every request, DNS
  included, leaves from the VM: `localhost` is the VM, and sites see the VM's IP. The browser tools,
  the live view and human takeover work as on any lane. Studio's review browser takes the same
  proxy, so `ui_check` on `http://localhost:3000` checks the dev server on the VM.
- **Ports**: `environment()` lists listening TCP ports on the VM (`ss -ltnp`). For each the node can
  forward, it gives a link the human can open (the node forwards it over SSH, behind the app's sign-in).
- **Binding**: the environment is bound to the MCP session id (header `mcp-session-id`); without
  one (stdio), to the grant, and is found only by its own grant family. A call without `on`
  resolves to the bound environment, by terminal or browser lane depending on the tool. With none
  bound, the call fails with `no_environment` and the fix `environment_open(title=...)`. A call
  with `on` works as before (account lanes); on another family's environment lane it is refused
  as not found.

## Contract

### MCP tools (everyday list, not in a toolset)

`environment_open(title: str, repo: str | None = None, ref: str | None = None, size: str | None = None,
hours: float | None = None, wait_s: float = 240) -> dict`

Returns:

```json
{"id": "e7k2", "title": "Black Friday page", "state": "ready",
 "workdir": "/work/shop", "repo": "github.com/me/shop", "branch": "main",
 "machine": {"provider": "hetzner", "server_type": "cpx22", "cpu": 2, "memory_gb": 4,
             "location": "nbg1", "price_hourly_eur": 0.012},
 "expires_at": "2026-11-13T18:00:00Z",
 "lanes": {"terminal": "env/e7k2", "browser": "env/e7k2-browser"},
 "links": {"browser": "http://localhost:7450/...the node's live view, signed..."},
 "next": "Give the human links.browser. Then lane_exec(cmd=\"ls\", cwd=\"/work/shop\") ..."}
```

`state` is `starting` (still booting: call `environment()` again) or `ready`. A second call in the
same chat returns the same environment. `links.browser` is the node's own live view (a signed
take-over link), null until the browser lane exists; there is no `links.environment` (changed
2026-11-13 by F2: it pointed at a web app page the public repository does not have).
`lane_exec`, `lane_job_start`, `lane_job_read` and `lane_job_cancel` are in the everyday list with
the three environment tools; the rest of the terminal tools are `tools_more("terminals")`.

`environment(id: str | None = None) -> dict`: the bound (or named) environment: state, machine, time
left, cost so far, `ports` (`[{"port": 3000, "process": "node", "link": "..."}]`), `git` (`{"branch",
"ahead", "dirty": n, "pushed": [{"branch", "commit", "at"}]}`), the last 10 `calls`. With no
environment bound and no id: `{"environment": null, "open": [...open ones of this grant, by title...],
"next": "environment_open(title=...)"}`.

`environment_close(id: str | None = None, discard: bool = False) -> dict`: `unsaved_work` error
(listing dirty files, unpushed commits and stashes, fix: commit and push, or discard=true) unless
clean or discard; then deletes the VM and the browser lane. Unsaved is any repository under
`/work`: changed or untracked files, commits on any local branch no remote has, stashes. Returns
`{"id", "state": "closed", "cost_eur", "minutes", "pushed": [...]}`.

Errors (in docs/ERRORS.md, generated): `no_environment`, `unsaved_work`, `environment_limit` (the
node's `max_live`, the longest life, or the budget, which counts open environments to their time
limits), `environment_failed` (boot failed; the VM is deleted), `environment_in_use` (the title
names one another chat used in the last 10 minutes), `environment_not_ready` (a studio check in a
chat whose environment is starting or whose tunnel is down; it never checks the node instead).

### Web routes (the app polls; no live events needed)

```
GET  /api/orgs/{org}/environments?state=open|all       {"environments": [EnvSummary]}
GET  /api/orgs/{org}/environments/{id}                 EnvDetail
POST /api/orgs/{org}/environments/{id}/close {"discard": false}   EnvSummary  (409 unsaved_work)
POST /api/orgs/{org}/environments/{id}/extend {"minutes": 60}     EnvSummary
```

EnvSummary: `id, title, state ("starting" | "ready" | "working" | "idle" | "closing" | "closed" |
"failed"), repo?, branch?, created_at, last_call_at?, closed_at?, expires_at, machine {provider,
server_type, cpu, memory_gb, location}, price_hourly_eur, cost_eur, agent {name, client?},
lanes {terminal, browser}`. "working" means a call in the last 2 minutes, "idle" none for 10.

EnvDetail: EnvSummary plus `ports [{port, process?, link?}]`, `git {branch, ahead, dirty,
pushed [{branch, commit, at}]}`, `calls [{at, tool, summary, ok}]` (newest first, 50),
`commands [{id, cmd, state, started_at, exit_code?}]` (the terminal lane's jobs and execs),
`viewer {path} | null` (the browser lane's live view, as accounts' `lane_viewer_path`),
`closed_reason?`, `unsaved {dirty_files: [..], unpushed: [..], stashes: [..], unreadable?: [..]} | null`
(`stashes` and `unreadable` added by F2).

Reading needs `see` in workspace `env`, close and extend `operate`; every route checks the caller's
grant as the jobs routes do. The owner sees every environment, a member only those their own agents
opened; any other is not found. `calls[].summary` is scrubbed as the audit log's traces are.

### CLI

`agentos environments` (alias `agentos envs`): `list`, `show ID`, `close ID [--discard]`, `allow`
(the limits that bound `environment_open`: `--max-live`, `--max-hours`, `--monthly-eur`; since
2026-11-13 it grants nothing, as opening never asks), and
`setup` (checks the provider token, the image, the git declarations, and says what is missing).

## Builders and files

| Builder | Owns |
|---|---|
| E1 core | `src/agentos/environments/` (new), the `on` default routing, `src/agentos/vms/` changes, `src/agentos/api/web/environments.py`, CLI, tests under `tests/unit/test_environments*.py` |
| E2 web | `web/src/features/environments/`, the rail and Home entry, mocks and hooks for the routes above |
| E3 front door | README.md, docs/ENVIRONMENTS.md, docs/VISION.md, docs/ARCHITECTURE.md, the MCP intro (`INSTRUCTIONS` in src/agentos/node/tools/recipes.py), `whoami` text, the skill `src/agentos/client/skills/agentos-environment/`, CLI root help, the public export |
