# Environments: a computer in the cloud for each agent

An environment is a fresh VM that the node rents for one chat. It has a terminal, a real browser
whose traffic leaves from the VM, and your repository in `/work/<name>`. The agent works there,
commits and pushes a branch, and the VM is deleted when it is done. You can watch its browser and
its commands live and take over.

This page is the user guide. The design, the decisions and the exact tool shapes are in
[design/environments.md](design/environments.md).

## Set it up

Everything here runs on the node, as the owner (`sudo agentos ...` on a node installed with
`deploy/install-node.sh`, or with `AGENTOS_HOME` pointing at a dev node's home).

### Connect a repository

A public repository needs nothing: the VM clones it directly. A private one is connected once, by
you. An agent cannot do it, and never sees the token: `environment_open` with a repository the node
cannot read rents nothing and gives the agent a card instead (`status: "approval_pending"`, `kind:
"repo_connect"`). The agent hands you its `review_url`. In T3 Code it opens beside the chat (the
preview pane), and that is the in-chat way to connect it.

On the card, in Needs you:

- **Paste a token** (the default). It needs no admin rights on the repository, so a collaborator can
  connect one. A classic token needs the `repo` scope; a fine-grained one needs this repository with
  Contents: Read (Read and write, so agents can push). The button opens a small page the node serves
  itself, in a new tab, and you paste the token there: the web app never holds it. The node checks it
  with GitHub, then with `git ls-remote` the way the git gateway fetches, and keeps it in the vault for
  this one repository only. The page says connected, the card moves on by itself, and the agent's
  `approval_wait` returns, so it opens again. When GitHub says when the token expires, the page and
  the card show it. The page's link works once, for 15 minutes; a token that did not work can be
  pasted again on the same page until then.
- **Use a deploy key instead (repo admins)**: the card shows the node's public key and the
  repository's deploy keys page. An admin adds it with Allow write access, then you press "I added
  it, check".

The token goes only to GitHub, to git and into the vault: never into the chat, a tool result, an
error, the card, the web app, an approval, the action log or a log file. Only the owner signed in to
the panel can open the token page (a person present, with a fresh passkey check when you have a
passkey): no agent's token, grant or link can. Never paste a token into the chat. No tool takes one, because anything in a tool
argument is in the transcript, and the node cannot read a chat client's secret references (T3 Code's
`request_secret`) either.

What the token page says when a token does not work:

| It says | What to do |
|---|---|
| wrong, expired or revoked (GitHub's 401) | make a new token and paste it |
| this token cannot see the repository (404) | check you are a collaborator; a fine-grained token needs the right resource owner and this repository in its repository access |
| must be authorized for the organization's single sign-on | open the link it gives, authorize the token, paste it again |
| waits for the organization's approval | an organization admin approves it (Personal access tokens, Pending requests) |
| the organization does not allow this kind of token | make the kind it allows (often fine-grained), or ask an admin |
| GitHub accepts it, but git cannot read the code | give it Contents: Read (classic: the `repo` scope) |

At the node, the same in one command, and the card closes on its own within a minute:

```
agentos environments repo add github.com/me/shop --token   # a token, read from a prompt (no admin rights needed)
agentos environments repo add github.com/me/shop           # a deploy key: it prints the key for a repo admin to add
agentos environments repo list                             # what is connected, with which key or token
agentos environments repo remove github.com/me/shop        # disconnect it, and delete the key made for it
```

`repo add` is the two steps of section 3 in one: `agentos vault git key add --host github.com --repo
me/shop` (or `--token`) and `agentos vault git remote allow env github.com/me/shop`. Add the deploy
key it prints with write access, so agents can push. When a key or token stops working later (the
deploy key was removed, the token expired), the agent's next open gives the same card, to give a new
one (at the node: `repo add ... --token --replace`).

### 1. Where the VMs come from

**Docker** (no cloud account): each environment is an Ubuntu container with SSH on the node's own
Docker. Good for trying it, for CI and for a node at home. Put this in `$AGENTOS_HOME/vms.toml`:

```toml
provider = "docker"
```

Then build its image once (a few minutes): `agentos environments setup --build` (build it again
after an upgrade when setup says the image is older than agentos).

What a docker machine is kept from, and what it is not:

- Each machine has a docker network of its own, so machines never reach each other, and limits
  from `vms.toml`: `docker_cpus` (default 2), `docker_memory_gb` (4, no swap on top) and
  `docker_pids` (512 processes and threads). An agent that asks for a size (below) gets that size's
  CPUs and memory instead, never more than this computer has.
- It has only the capabilities a dev box and sshd need. A firewall inside it, set before sshd
  starts and out of reach after (the machine has no `NET_ADMIN`, root included), refuses new
  connections to private, shared and link-local addresses: the machine reaches the internet but
  not this computer and not your network. That matters most on Docker Desktop for Mac, which
  forwards `host.docker.internal` to the Mac's own loopback, where the node's git gateway and the
  other environments' browser tunnels listen. On Linux, the docker gateway is the computer itself.
  DNS (port 53) is the one exception.
- It still shares a kernel: with this computer on Linux, with Docker Desktop's Linux VM on a Mac
  (and that VM can read the folders you share with Docker, your home folder by default). A machine
  that breaks out of its container is on this computer. Use the docker provider for your own code;
  the cloud provider is the isolated one.
- The browser's tunnel to a machine is a port on this computer's loopback. On Linux the node
  serves it to its own user only. macOS cannot tell which account opened a local connection, so
  another account on the same Mac could use it.

`agentos environments setup` says the same.

**Hetzner Cloud**: make a project that holds nothing but these VMs (the token can delete any
server in it), make a read-write API token in it, and give it to the node:

```
agentos vm setup              # asks for the token, keeps it in $AGENTOS_HOME/secrets/hetzner.token (0600)
agentos vm types              # what can be ordered today, and what each profile rents
```

The node rents the cheapest server type that fits, unless you or the agent set a size. The
location, your own profiles and the node's VM limits are in `vms.toml`
([design/vms.md](design/vms.md#settings-agentos_homevmstoml)).

An agent asks for a size by name, and the open's `machine` says what it got and what each bigger
size would rent and cost (`machine.bigger`):

| `size` | At least | On Hetzner | On docker |
|---|---|---|---|
| left out | the node's default | the cheapest type that fits (`profile` in `environments.toml`, else vms.toml's default) | `docker_cpus`, `docker_memory_gb` |
| `small` | 2 CPU, 4 GB | the cheapest type in the location with at least that | 2 CPU, 4 GB |
| `medium` | 4 CPU, 8 GB | the same | 4 CPU, 8 GB |
| `large` | 8 CPU, 16 GB | the same | 8 CPU, 16 GB |
| `xlarge` | 16 CPU, 32 GB | the same | 16 CPU, 32 GB, at most this computer's |

It may also name a server type (`cpx42`) or one of your `vms.toml` profiles. The biggest an agent
may ask for is `max_size` (default `xlarge`; `agentos environments allow --max-size large`); past
it the open is refused with `environment_limit`.

### 2. The image

Without one, each VM starts from Ubuntu 24.04 and installs git, curl, build tools, Node 22 and
Python with uv on first boot, which takes a few minutes. A snapshot boots ready in under a minute:

```
agentos vm image build dev    # Hetzner: a snapshot with git, build tools, Python with uv, Node 22, Docker
```

The node uses the `dev` snapshot when it has one. It also holds the browser for `browser = "vm"`
(below), as does the docker image: Xvfb, x11vnc, fonts and Camoufox at the node's own versions, about
3.3 GB more (Camoufox's build is 2.4 GB, most of it the fonts of the systems it can look like) and
about two minutes more to build.

### Where the browser runs

`browser` in `$AGENTOS_HOME/environments.toml` says where each new environment's browser runs:

```toml
browser = "node"   # the default: on the node, with its traffic through the VM
browser = "vm"     # on the VM itself: for a node with no browser of its own, such as agentos on a Mac
```

With `"vm"` the browser, its screen and its live view run on the VM, bound to the VM's loopback only.
The node starts them over the SSH connection it already holds and drives the browser through that
connection. Every browser tool works the same way, `browser_screenshot(proof=..., width=...)`
included (proofs stay on the node), and so do the live view and take over. On the VM, `localhost` is
the VM, so no proxy is needed. It is a fresh browser each time it opens. A VM booted from an image
without the browser installs it while the environment starts (a few minutes). `ui_check` still runs
studio's browser on the node, through the tunnel. An environment keeps the setting it opened with.

A VM with the browser and one page open uses about 1 GB of memory (measured on the docker provider).
The smallest size (`small`, 2 CPU and 4 GB, a Hetzner cx23) leaves room for a typical dev server; a
heavy build next to a big dev server wants `size="medium"`.

### 3. Git: keys and declared repositories

The VM never holds a git key or your token. Clone, fetch and push go from the VM through an SSH
tunnel to the node's git gateway, which uses the credential in the vault, only for the repositories
you declared for the `env` workspace ([GIT.md](GIT.md)). The VM holds one gateway token, in its git
config: it fetches and pushes the repository the environment was opened with and nothing else (not
the other declared ones), works only through that VM's own tunnel, and is revoked when the
environment closes. An environment with no declared repository gets no token at all.

```
agentos vault git key add --token --host github.com --repo 'me/*'   # a fine-grained token, read from a prompt
agentos vault git remote allow env 'github.com/me/*'                # environments may clone and push these
```

Or a deploy key per repository: `agentos vault git key add --host github.com --repo me/shop` prints the
public key to add on GitHub, with write access if agents should push. A push from an environment
never asks you, the default branch included. Refused instead (`push_refused`: the agent pushes a
new branch and opens a pull request): a force push or a delete of the default branch (or of a
branch you protect with `--protect`), a force push or a delete of any branch the environment did
not create itself (a colleague's branch that was there before), and moving or deleting a tag. Its
own branches (the ones it pushed first) it may rewrite and delete. A push from anywhere else, an account
lane or a brain, still asks you for the default branch (`--allow-default-branch` on `remote allow`
turns that off for a repository). A public repository that is not declared is cloned directly on
the VM, read only.

### 4. The limits

Nothing in an environment asks you (the owner's decision of 2026-11-13): not opening one, not its
commands or its browser, not a push, also after the agent read a web page. Opening one costs
money, so the node's limits bound it instead. They hold from the start, without any command:

| Limit | Default | Changed with |
|---|---|---|
| open at once | 3 | `agentos environments allow --max-live N` |
| the longest life, `extend` included | 8 hours | `agentos environments allow --max-hours H` |
| booked in a month | EUR 50 | `agentos environments allow --monthly-eur E` |
| the biggest machine (`size`) | `xlarge` (16 CPU, 32 GB) | `agentos environments allow --max-size large` |

```
agentos environments allow --max-live 2 --max-hours 4 --monthly-eur 20
```

Past a limit the open (or an extend) is refused with `environment_limit`, whose message names the
limit and the command that raises it: the agent closes one, asks for fewer hours, or tells you.
What the agent read never changes this. The budget counts what open environments have booked: each
one to its time limit, not only what it has cost so far. The node's own limits in `vms.toml`
(`max_live`, `max_ttl_minutes`, `monthly_budget_eur`) hold too, and the tighter one wins.
`allow` grants nothing; it only writes the limits to `environments.toml`.

### 5. Check it

```
agentos environments setup
```

It checks the provider (Docker running, or the Hetzner token), the image and the git
declarations, and says what is missing and the command that fixes it.

## Use it from an agent

Connect your agent first (`agentos connect`, see the [README](../README.md#connect-claude-code-codex-and-t3-code)).
The MCP intro tells every agent it can have an environment, and the `agentos-environment` skill
(installed by `agentos connect` for Claude Code) teaches it the loop. You only have to ask:

> Work on this in agentos: fix the failing checkout test, check the page on a phone, push a branch.

The loop: open, read and edit files, run the tests, start the dev server and look at it in the
browser, keep a screenshot as proof and give you its link, push a branch and give you the pull
request link, close (the VM is deleted).

What the agent does:

| Call | What it does |
|---|---|
| `environment_open(title, repo, ref, size, hours)` | rents the VM, clones `repo` into `/work/<name>`, opens the browser; returns the workdir and the browser's live view link for you |
| `lane_file_read(path, offset, limit)`, `lane_file_edit(path, old, new)`, `lane_file_write(path, content)` | the repository's files on the VM, with line numbers; an edit replaces exact text that occurs once (a relative path starts in the repository) |
| `lane_exec(cmd, cwd, max_bytes)`, `lane_job_start`, `lane_job_read(until_exit)`, `lane_job_cancel` | commands and dev servers on the VM (no `on`: the environment of this chat); in every agent's tool list. A command's output comes back within 16 KB by default (the middle is cut, with the full sizes). `shell_open` and the rest are `tools_more("terminals")` |
| `browser_navigate(url)`, `browser_snapshot`, `browser_act` | the environment's browser, a desktop window; `localhost` is the VM |
| `browser_screenshot(proof="checkout after the fix", full_page, width)` | keeps the shot as proof (see below); `width=390` shows the phone layout |
| `ui_check(url="http://localhost:3000")` | studio's page checks (`tools_more("studio")`), on the VM's dev server; while the environment cannot be reached they fail with `environment_not_ready` and never check the node instead |
| `environment()` | state, time left, cost so far, listening ports, git (branch, ahead, dirty, `pushed`: each branch at its commit on the remote now, `deleted`: branches it deleted, and `compare`: the GitHub link that opens a pull request for the pushed branch), proofs, the last calls (a command's `exit_code`, a refusal's `error`) |
| `environment_close(discard=False)` | refuses with `unsaved_work` while any repository under `/work` has work that is not on a remote (changed or new files, commits on any branch, stashes); then deletes the VM, the browser and both lanes; `machine` says `deleted` only once the provider has no such server (`deleting` while the node still deletes it) |

Work that is only on your computer (changes not committed yet, or a project with no repository the
node can clone) gets there two ways. The agent commits it to a branch, pushes it, and opens with
`ref=<branch>`; or, without a commit, it uploads its checkout:

1. `environment_upload(dest)` (in `tools_more("terminals")`, or `tools_call`) gives a signed link on
   the node's own address (for an agent on your Mac, `http://localhost:7400` through its tunnel),
   good once and for 15 minutes, and two commands.
2. The agent runs them in its own shell, in the checkout. `pack` takes the tracked and new files
   and leaves out what git ignores (`node_modules`, `.env`); `send` puts the archive on the node
   with curl, so the bytes never go through tool calls:

   ```
   git ls-files -co --exclude-standard -z | COPYFILE_DISABLE=1 tar --null -T - -czf work.tgz
   curl --fail-with-body -T work.tgz "<url>"
   ```

3. The node checks every file in the archive, then extracts it into the workdir (`/work/<name>`,
   or `/work` for an environment opened without a repository; `dest` may name a folder under
   `/work`). Files in it replace the same paths, and nothing else is deleted. The reply lists the
   files; the upload is in the audit log and in `environment()`'s last calls.

The checks: at most 512 MB sent and 4 GB unpacked, 200,000 files, and no absolute path, no `..`,
no link that leaves the folder, no device or fifo (Python's tarfile data filter, and more). One bad
file refuses the whole archive before the VM sees any of it. The link ends when the environment
closes or the node restarts, and a used, expired or changed one gets a 403. For a few small files
without a shell of its own, the agent still has `lane_upload` and a job's sync.

One environment per chat. A new chat starts clean. A resumed or reconnected chat that opens the
same title again, with the same grant, gets the one still open (only one that is starting or
ready, never one that failed or closed; the repository may be left out; a title held by one with
another repository than the one named is `environment_in_use`), once the chat that had it has made
no call for 10 minutes (before that it is refused with `environment_in_use`, and the chat that lost
it is told so on its next call). The takeover counts as a call, so the chat that lost it cannot
take it straight back. A chat whose call finds no environment hears the agent's open
ones by title. Calls with `on="workspace/name"` go to that lane, as before: account lanes and
environments live side by side.

An environment belongs to the agent that opened it: its grant, and the grants narrowed from it.
Another agent cannot run commands there, drive its browser, read it or close it, whatever its
grant says; to it the environment does not exist. You see and close every one from the web app and
the CLI; a member sees and closes the ones their own agents opened.

Nothing in the environment waits for a person, even after the agent read a web page: not opening
it, not its commands, not its browser (a click that publishes, an upload), not a push. The VM holds
no account and no key, its browser no login and no vault entry, and its one token pushes only its
own repository. What stays are refusals, never questions: the limits above, a push that would
rewrite or delete the default branch or a branch the environment did not create, a repository the
token does not reach. Reading a page in the
environment's browser still marks the agent as having read untrusted text, so what it does next on
your account lanes (a post on `personal/x`, say) asks you as before.

## Watch and take over

`environment_open` returns `links.browser`, and agents are told to give it to you: **the
browser's live view**, served by the node itself, the environment's browser as it is now. Take
control to click or type yourself, then hand it back, from that link alone: the environment's
browser holds no account, so no sign-in is needed (an account lane's link still needs your panel
sign-in for that, [THREAT-MODEL.md](THREAT-MODEL.md#viewer-links)). The agent waits until you hand
it back, your input counts as use, so the reaper does not close it under you, and the trail records
what you did as `link holder`. The link is on the node's own address as the agent reaches it (for an
agent on your Mac, `http://localhost:7400` through its tunnel to the node), so it opens on that computer. In T3 Code the agent opens it in the preview pane
beside the chat (`preview_open`), so you watch the browser while you talk to the agent.

### Proof of the work

`browser_screenshot(proof="checkout page after the fix")` keeps that screenshot as proof. It is
stored on the node with the environment (`$AGENTOS_HOME/environments/<id>/proofs/`), recorded in the
audit log, and kept for 30 days after the environment closes (at most 30 for one environment). The
agent gets a link to it on the node's own address, good for 15 minutes (`environment()` and
`environment_close` list every proof with a fresh one), which it gives you or saves with
`curl -o proof.png "<url>"`. The web app's environment page shows them too
(`GET /api/orgs/{org}/environments/{id}` lists `proofs`, and each `src` is its PNG).

After `git push origin <branch>`, `environment()` gives `git.compare`, the GitHub page that opens a
pull request for that branch, and the agent gives it to you.

### Ports

`environment()` lists the ports listening on the machine, for example the dev server on port 3000.
There is no link that opens a port on your own computer (`link` is null): look at the dev server
through the environment's browser, in the live view.

From the CLI:

```
agentos environments list           # the open ones (alias: agentos envs); --all: closed and failed too
agentos environments show e7k2      # one in full: machine, cost, ports, git, last calls
agentos environments close e7k2     # --discard to close with unsaved work
```

The web app's Environments page shows the same, with each one's live browser, commands and git on
its own page ([web/README.md](../web/README.md) says how to build and open it). Without a build, the
live view links, the CLI and the terminal dashboard (`agentos open --terminal`) show the same.

## Costs and the reaper

- The price is the provider's hourly price for the server type, shown in the audit log's record
  of the open, on the environment page and in `environment()`. Hetzner bills each started hour. Docker costs nothing.
- `cost_eur` so far is started hours times the hourly price. The month's total counts against
  `--monthly-eur` and the node's `monthly_budget_eur`.
- Every environment has a time limit (`hours` on open, at most `--max-hours`, 8 by default). The
  environment page extends it, up to the same limit.
- **The reaper** closes an environment that the agent forgot: idle (no call, no running command
  and no input from you in its live view) for `idle_minutes` (default 60), or past its time
  limit. One with unsaved work is never deleted for being idle: it waits to its time limit, and
  you get a notification first. Unsaved work is anything in a repository under `/work` (the cloned
  one or any other) that is not on a remote: changed or new files, commits on any local branch
  that no remote has, and stashes. `environment_close` refuses for the same reasons.
- A daemon restart never deletes environments; the reaper picks up where it was. A server the node
  made but no longer knows about is deleted as an orphan.

## What is on the VM, and what is not

On it:

- Ubuntu 24.04 with git, curl, build tools, Node 22 with npm and pnpm (through corepack, with
  `COREPACK_ENABLE_DOWNLOAD_PROMPT=0`, so a repository's `packageManager` version downloads without a
  question), and Python with uv (or the `dev` snapshot, which also has Docker). A snapshot made
  before pnpm was set up gets it when an environment comes up.
- `/work/<name>`: the repository, on the branch or `ref` asked for.
- A git config that sends declared hosts to the gateway (`127.0.0.1:3129` on the VM is the node's
  gateway, through the VM's SSH connection).
- Whatever the agent installs. It is the agent's machine for this chat.

Not on it:

- agentos itself, a node token, a grant, the vault, any key or token, any account.
- The browser, with the default `browser = "node"`. It runs on the node, with its traffic carried
  through the VM (a SOCKS tunnel over SSH, DNS included), so `localhost` is the VM and sites see the
  VM's address. With `browser = "vm"` it runs on the VM (above). Either way it has no account,
  no saved login and no vault entry, and it is deleted with the environment. Its window fills a
  1920x1080 screen, so pages show their desktop layout (an account lane's window is the size its
  identity drew).

## Limits

- One environment per chat. The node's `max_live` and budget cap how many are open in all.
- Nothing reaches the VM from the internet except SSH from the node (Hetzner: a firewall per node).
  Its ports are seen through the environment's browser (the live view).
- A chat that talks to the node over stdio (no `mcp-session-id`) is bound by its grant, so two
  such chats with one grant share an environment.
- Files go in through git, `environment_upload` (a tarball of a local checkout, at most 512 MB), or
  `lane_upload` and a job's `sync` (above); they come out through git.
- The node checks where a push goes (declared repository, protected branch), not what the commits
  contain. Review a branch before you merge it.
- On the dev node (`agentos dev up`), account lanes are simulated. The environment's machine,
  terminal, git and studio's checks are real.

## Troubleshooting

| You see | What it means | What to do |
|---|---|---|
| `environment_limit` | `max_live`, the longest life or the month's budget is reached; the message says which | close one (`agentos environments list`), or raise that limit with `agentos environments allow` |
| `environment_failed` | the VM did not boot or answer SSH in time; it was deleted | `agentos environments setup`; on Hetzner, `agentos vm types` (sold out in the location?) |
| `state: "starting"` for minutes | first boot without the `dev` image installs the tools | wait, or build the image (step 2) |
| `no_environment` | a call without `on` in a chat with no environment | the agent calls `environment_open(title=...)` first, or names an account lane with `on` |
| `environment_not_ready` | the environment is starting, or its tunnel is down after a restart | wait a minute; `agentos environments show ID` |
| `unsaved_work` on close | uncommitted changes or commits not pushed | commit and push, or close with `discard=true` if the work may go |
| `approval_pending` with `kind: "repo_connect"` | the repository is private and not connected, or its key or token was refused; nothing was rented | open the card's `review_url` and paste a token (see "Connect a repository"), or at the node `agentos environments repo add github.com/me/shop --token` |
| the token page says it did not work | the token did not work, and nothing was kept; the page says why | do what the page says (table in "Connect a repository"), then paste it again on the same page |
| `repo_not_connected` | a node without approvals, or the clone on the machine still failed (its machine was deleted) | `agentos environments repo add github.com/me/shop` (the message names the repository) |
| a push is refused | the repository is not declared for `env`, or it is fetch only | `agentos environments repo add github.com/me/shop` |
| a push is refused with `push_refused` | it would force-push or delete the default branch (or a protected one), force-push or delete a branch the environment did not create, or move a tag | push a new branch and open a pull request |
| `localhost:3000` does not load in the browser | nothing listens on that port on the VM | `environment()` lists the ports that listen; start the dev server with `lane_job_start` |
| `studio_blank_page` on a dev server | nothing rendered within the wait (15 s on a dev server) | a slow app: `ui_check(..., wait_for="main h1")`; else the app failed to render (the message quotes its console) |
| a proof link answers "not valid or has expired" | the link is 15 minutes old | `environment()` lists the proof with a fresh link; the web app's page shows it too |

Every error code and its fix is in [ERRORS.md](ERRORS.md).
