# Environments: a computer in the cloud for each agent

An environment is a VM that the node rents for an agent. It has a terminal, a real browser whose
traffic leaves from the VM, and your repository in `/work/<name>`. The agent works there, commits
and pushes a branch. A single-use one is deleted when it is done; a kept one is saved or stays
running, and comes back when an agent opens it again with the same title
([Keeping an environment](#keeping-an-environment)). You can watch its browser and its commands
live and take over.

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

The node uses the `dev` snapshot when it has one. The build applies the security updates of that day
(machines never update themselves mid-session, see [What is on the VM](#what-is-on-the-vm-and-what-is-not)),
so rebuild it now and then. The build runs the environment's own setup script
too, so a VM from the snapshot has the ready marker and pnpm, and its setup check is one SSH call. A
snapshot built before that (no marker) still works: each open waits for cloud-init in one call and
runs the setup, which skips apt when every package is there and installs pnpm.
Rebuild it to drop that. It also holds the browser for `browser = "vm"`
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

How the clone travels. A private repository comes through the node's mirror, so its pack crosses the
upload of the computer agentos runs on: the VM clones it blobless (every commit and tree, and only the
files the checkout needs; git fetches any other file through the gateway when a command needs it). A
241 MiB repository is about 57 MiB that way. A declared repository the VM can read with no credential
(a public one) is cloned and fetched straight from GitHub, at the cloud's own speed, and only its
pushes go through the gateway, with the rules above. Either way the node starts fetching the mirror
when the open starts, so a cold mirror fills while the VM boots.

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

How environments stay is set in the same file: `default_keep` (`none`, the default, or `save`),
`allow_keep_live` (off until you turn it on) and `max_live_kept`. `agentos setup` asks the first
one, and turns the second on only when you choose it. See
[Keeping an environment](#keeping-an-environment).

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
browser, keep a screenshot as proof and give you its link, push a branch and open the pull
request (you get its link), close (the VM is deleted).

What the agent does:

| Call | What it does |
|---|---|
| `environment_open(title, repo, ref, size, hours, keep, signins)` | rents the VM, clones `repo` into `/work/<name>`, opens the browser; returns the workdir and the browser's live view link for you. With `repo` and no `title` it finds that repository's one open or saved environment (several: `environment_choice`, with each title to pick from; none: a new one named after the repository) |
| `lane_file_read(path, offset, limit)`, `lane_file_edit(path, old, new)`, `lane_file_write(path, content)` | the repository's files on the VM, with line numbers; an edit replaces exact text that occurs once (a relative path starts in the repository) |
| `lane_exec(cmd, cwd, max_bytes)`, `lane_job_start`, `lane_job_read(until_exit, until_port)`, `lane_job_cancel` | commands and dev servers on the VM (`on`: the handle your environment_open gave; the job tools take it too and check it names the job's machine, though the job id alone is enough); in every agent's tool list. A command's output comes back within 16 KB by default (the middle is cut, with the full sizes). `shell_open` and the rest are `tools_more("terminals")`. See [Waiting and memory](#waiting-and-memory) |
| `browser_navigate(url)`, `browser_snapshot`, `browser_act` | the environment's browser, a desktop window; `localhost` is the VM |
| `browser_screenshot(proof="checkout after the fix", full_page, width)` | keeps the shot as proof (see below); `width=390` shows the phone layout |
| `ui_check(url="http://localhost:3000")` | studio's page checks (`tools_more("ui_check")` loads them), on the VM's dev server; while the environment cannot be reached they fail with `environment_not_ready` and never check the node instead |
| `environment()` | state, time left, cost so far, listening ports (a shared one with its `link`), git (branch, ahead, dirty, `pushed`: each branch at its commit on the remote now, `deleted`: branches it deleted, `compare`: GitHub's form for the pushed branch, `pull_requests`), proofs, the agent's `note`, the last calls (a command's `exit_code`, a refusal's `error`), and always the agent's `open` and `saved` environments by title |
| `environment_pull_request(title, body, base, draft)` | opens the pull request for a branch this environment pushed, with the node's GitHub token, and returns its `url` (see below) |
| `environment_port(port)` | a link on your own computer to a server on the VM (see [Ports](#ports)) |
| `environment_close(discard, final, note, forget)` | refuses with `unsaved_work` while any repository under `/work` has work that is not on a remote (changed or new files, commits on any branch, stashes); then deletes the VM, the browser and both lanes; `machine` says `deleted` only once the provider has no such server (`deleting` while the node still deletes it). `saves` says what became of the saves; `note` and `forget` are in [Keeping an environment](#keeping-an-environment) |

Work that is only on your computer (changes not committed yet, or a project with no repository the
node can clone) gets there two ways. The agent commits it to a branch, pushes it, and opens with
`ref=<branch>`; or, without a commit, it uploads its checkout:

1. `environment_upload(dest)` (in `tools_more("terminals")`, or `tools_call`) gives a signed link on
   the node's own address (for an agent on your Mac, `http://localhost:7400` through its tunnel),
   good once and for 15 minutes, and two commands.
2. The agent runs them in its own shell, in the folder with its code. In a git checkout `pack`
   takes the tracked and new files and leaves out what git ignores; in a plain folder it takes all
   of it but `node_modules`, `.env`, `.git`, `.venv` and `__pycache__`. The archive goes to the temp
   folder, never into the folder it packs. `send` puts it on the node with curl, so the bytes never
   go through tool calls. An archive with no files is refused:

   ```
   A="${TMPDIR:-/tmp}/agentos-work.tgz"; if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
     git ls-files -co --exclude-standard -z | COPYFILE_DISABLE=1 tar --null -T - -czf "$A"
   else COPYFILE_DISABLE=1 tar --exclude node_modules --exclude .env ... -czf "$A" .; fi
   curl --fail-with-body -T "${TMPDIR:-/tmp}/agentos-work.tgz" "<url>"
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

One chat holds a live machine (the owner's rule of 2026-11-16: "only one agent chat should have
access to the same machine at the same time, even if it is the same project"). `environment_open`
with a new title opens a new environment, even while others of the same agent are open (the node's
limits still hold), and gives the calling chat the machine's handle, `on` (its id, a dot and 16
random letters and digits). The node keeps only the handle's sha256, so no record, listing or log
shows it. Only calls that carry it reach the machine: the agent keeps the `on` from its
`environment_open` and passes it on every call.

- A client that sends no MCP session id (MCP 2026-07-28, as Claude Code and T3 Code use it, and
  stdio) looks like every other chat of the agent. A terminal, browser or environment call without
  `on` is refused with `which_environment`, even with one machine open, and so is a title or an id
  in `on`: another chat's machine must never be reached by default.
- A client that sends a session id (`mcp-session-id`, the older protocols) holds its machines by the
  session too: its calls without `on` go to the one it opened last, and its own id or `env/<id>`
  works as `on`.

The title of a live machine another chat holds is `environment_in_use`, with the fix: open your own
with another title, like "<title> 2". Once the holder has made no call for `take_over_minutes` (10,
in `environments.toml`), `environment_open(title, take_over=true)` gives the machine to the new chat
with a fresh handle (`opened: "taken_over"`), and says the earlier chat's work is on it. The old
handle stops working (`no_environment`, saying it was taken over), and the move counts as a call, so
the earlier chat cannot take it straight back. Without `take_over` it is refused, so taking a machine
is always deliberate. A machine kept live and let go (`environment_close` with keep "live") is
nobody's: its title gets it back at once, with a new handle. The repository may be left out; the
same title with another repository is `environment_already_open`, whose fix is another title. What
an attach could not give as asked (keep, hours, size, ref) is in `differs`, with the values it has in
`applied`. With the title of a saved one it resumes it into a new machine leased to the resumer
(`opened: "resumed"`); of two chats resuming at once, one wins and the other gets
`environment_in_use`.

`environment()` and every listing show a live machine the caller does not hold without its handle
or id: its title, state and `use` ("in use by another chat", "quiet for N min" with the take-over
call, or "let go"). `environment(on=<handle>)` shows that machine in full. `lane_jobs_list` without
`on` lists no environment's jobs over MCP. Studio's checks take `on` for a page on the machine
(`ui_check(url="http://localhost:3000", on=...)`). Every result of a call that went to an
environment carries `environment: {id, title}`. Calls with `on="workspace/name"` go to that account
lane, as before: account lanes and environments live side by side.

An environment belongs to the agent that opened it: its grant, and the grants narrowed from it.
Other people's agents cannot
run commands there, drive its browser, read it or close it, whatever their grant says; to them the
environment does not exist. You see and close every one from the web app and
the CLI; a member sees and closes the ones their own agents opened.

Nothing in the environment waits for a person, even after the agent read a web page: not opening
it, not its commands, not its browser (a click that publishes, an upload), not a push. The VM holds
no account and no key, its browser no login and no vault entry, and its one token pushes only its
own repository. What stays are refusals, never questions: the limits above, a push that would
rewrite or delete the default branch or a branch the environment did not create, a repository the
token does not reach. Reading a page in the
environment's browser still marks the agent as having read untrusted text, so what it does next on
your account lanes (a post on `personal/x`, say) asks you as before.

### Waiting and memory

- **Waiting for a dev server.** `lane_job_read(job, until_port=3000, wait_ms=120000)` returns as soon
  as something listens on port 3000 on the VM (`listening: true`; a server on `0.0.0.0`,
  `127.0.0.1` or `::1` counts), or when the job ends first (`listening: false`, its exit code, and
  stdout and stderr are the end of its output), or at `wait_ms` (at most 120000; call again to wait
  longer). The waiting is one command on the VM that reads its listening sockets
  (`/proc/net/tcp`), not a loop of calls, and the call sends progress while it waits. No `sleep`
  loops in `lane_exec`.
- **Waiting for a job to end.** `lane_job_read(job, until_exit=true, wait_ms=120000)` returns when
  the job ends. When more is left after your offset than `max_bytes` (16 KB by default), stdout and
  stderr are each its start and its end, cut at line ends, as `lane_exec` keeps them, with a marker
  between them (`[agentos: 6452946 bytes skipped here, of 6468895 in all; read them with
  stdout_offset=3992]`) and the same words in `note`. `next_stdout_offset` is then the end; a read
  at the offset the marker names pages through what was skipped. A read without `until_exit`, and
  one with `base64_output`, always gives the bytes right after your offset.
- **Out of memory.** When the VM's kernel kills a process for memory, `lane_job_read` and
  `lane_exec` say so: `oom` lists each kill since the job or command started (`pid`, `name`,
  `rss_mb`, `at`, `machine_mb`: the machine's memory, and `in_job`: whether it was one of this job's
  processes, known from the job's own login session on the VM; `null` when the VM cannot tell),
  `oom_killed` is true for a kill of its own, and `note` says it in words ("The kernel killed pid
  4242 (node, 4.1 GB) of this job at 06:52:02 UTC: out of memory. The job runs on without it and
  may hang: ..."). When the killed process used half the machine's memory or more, the note names
  the machine's memory and the next size ("This machine has 3.7 GB of memory, and python3 used 3.6
  GB when it was killed: run less at once, or push your work and open a bigger machine,
  environment_open(title=..., size="medium")"); resuming a saved title keeps the size it was saved
  with. A job whose child was killed often keeps running and prints nothing; a read that waits
  (`until_exit`, `until_port`, or for more output) returns at once when such a kill appears. The
  node reads the VM's kernel log (`dmesg`) with one SSH call: at most every 10 seconds while a job
  is read, when a read's new output has the shell's "Killed" line, and once when a job or command
  ends with a sign of a kill: an exit code that is not 0, a "Killed" line, or an exit code 137 in
  its output (`child exit=137`, `exit status 137`, `SIGKILL`). That covers the common case of a
  parent that printed its child's 137 and exited 0. For an ended job this happens before the read
  that reports the end returns. A clean end costs no call. Every machine has swap (see [What is on
  the VM](#what-is-on-the-vm-and-what-is-not)), so a heavy build slows down before it runs out.
- **Jobs outlive a restart of agentos.** A `lane_job_start` job runs on the VM on its own (its own
  session, `setsid`), with its stdout and stderr in files there (`/tmp/agentos-wl/<id>.out` and
  `.err`), and the node follows those files over SSH. A restart or an upgrade of the agentos service,
  a laptop that sleeps, or a dropped connection no longer ends a dev server: when the service starts
  again it finds each job still running on its machine and follows it again, and `lane_job_read` goes
  on with the same job id and offsets. `lane_job_cancel`, the job's timeout (also one that passed
  while the service was down) and closing the environment still end its whole process tree. If the
  machine restarted meanwhile, the job is `lost` and its `note` says so. A job whose node never comes
  back ends a minute past its timeout by itself.
- **`pkill -f` killed its own shell.** `pkill -f vite` matches the shell that runs it, whose
  command line holds the pattern too, so the shell dies before the rest of the command runs (exit
  255 over SSH, or 143, 137, a signal), even after it printed something. When a `lane_exec` or an
  ended job exits that way and its command holds `pkill`, `pgrep` or `killall` with `-f` or
  `--full`, its `note` says the shell running it was killed and why: use `pkill -f '[v]ite'` (the
  brackets keep the pattern from matching itself), kill a pid, or `lane_job_cancel` for a job.

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

How long a link works: it opens until its expiry (`links.browser_expires_at`, 15 minutes after it was
made by default, `view_token_seconds` in node.toml), and a page that is open renews itself until 30
minutes after its link was made. `environment()` hands out the same link on every call until shortly
before it expires, then a new one; a link given earlier keeps working until its own expiry
(`links.browser_note` says this to the agent). Replacing the viewer key ends them all.

### Proof of the work

`browser_screenshot(proof="checkout page after the fix")` keeps that screenshot as proof. It is
stored on the node with the environment (`$AGENTOS_HOME/environments/<id>/proofs/`), recorded in the
audit log, and kept for 30 days after the environment closes (at most 30 for one environment). The
agent gets a link to it on the node's own address, good for 15 minutes (`environment()` and
`environment_close` list every proof with a fresh one), which it gives you or saves with
`curl -o proof.png "<url>"`. The web app's environment page shows them too
(`GET /api/orgs/{org}/environments/{id}` lists `proofs`, and each `src` is its PNG).

### Who the commits are by

With a GitHub login saved (`agentos setup github`), an environment's git commits as you: your GitHub
display name and your GitHub noreply address, `<id>+<login>@users.noreply.github.com`, so the commits
link to your account without showing your email. agentos asks GitHub for the account's number once,
with the saved login, and keeps it in `git/login.json`; only the name and the address reach the VM,
never a token. Without a login (or when GitHub cannot be asked), commits are by the agent: its name and
`agent@agentos.invalid`. An agent may still set its own with `git config` on the machine.

### Pull requests

After `git push origin <branch>`, `environment_pull_request(title, body=None, base=None,
draft=False)` opens the pull request on GitHub and returns its `url`, which the agent gives you.
The VM has no `gh` and no credential: the node opens it through GitHub's API with the token the
git gateway pushes with (a token declared for the repository or its owner first, else the GitHub
login of agentos local, `agentos setup github`; a deploy key cannot open one). It works only for
a branch this environment created on the remote, into `base` or the repository's default branch;
if one is open for that branch already, it returns that one. It never merges, closes or changes
anything else, and the token never leaves the node. With no token it fails with
`pull_request_failed`, and the agent gives you `git.compare` instead: GitHub's own form for the
branch.

### Ports

`environment()` lists the ports listening on the machine, for example the dev server on port 3000.
`environment_port(3000)` shares one with you, as E2B and Daytona give a preview link: the node
listens on a free `127.0.0.1` port and sends each connection through the environment's own SSH
connection (its SOCKS proxy) to that port on the VM, so no new SSH session, key or firewall rule is
needed, and only the node's own user may use it. It closes with the environment.

- On agentos local the node is your computer: `url` is `http://127.0.0.1:<p>/`, open it as is.
- On a server node the port is on the server: run the `tunnel` line it gives
  (`ssh -N -L <p>:127.0.0.1:<p> <your node>`, the same as for the live view), then open `url`.

The web app's Ports card shows the link (and the ssh line on a server node) for each shared port;
the others say "On the machine only".

From the CLI:

```
agentos environments list           # the open ones (alias: agentos envs); --all: closed and failed too
agentos environments show e7k2      # one in full: machine, cost, ports, git, last calls
agentos environments close e7k2     # --discard to close with unsaved work
```

The web app's Environments page shows the same, with each one's live browser, commands and git on
its own page ([web/README.md](../web/README.md) says how to build and open it). Without a build, the
live view links, the CLI and the terminal dashboard (`agentos open --terminal`) show the same.

## Keeping an environment

An environment has a `keep` mode, chosen by the agent when it opens it (`environment_open(...,
keep=...)`), else `default_keep` in `environments.toml`:

| Mode | In the web app | What happens | While nobody uses it |
|---|---|---|---|
| `none` | Single use | work, push, close; the VM is deleted | nothing |
| `save` | Saves when idle | on idle, close, its time limit or laptop sleep, the whole machine is saved (a disk snapshot plus the files) and the VM deleted; autosave every 15 minutes | the snapshot, about EUR 0.012 per GB a month (cents) |
| `live` | Kept running | the VM stays on; deleted only on a final close or by you | every hour, about EUR 7.80 a month for a `cx23` |

The skill and the MCP intro tell the agent to choose single use for a one-off fix, `save` when the
work will span sessions or you say "keep it" or "continue tomorrow", and `live` only when you ask
for an always-on machine. `live` is refused (`keep_live_not_allowed`) until you allow it:

```
agentos environments allow --keep-live
```

**Coming back.** `environment_open` with the same title, from any chat, recreates the VM from the
save, and the job continues: the tools, the Docker volumes, `node_modules`, the browser's sign-ins
and the dev server come back. `environment_open(repo=...)` without a title finds the one
environment of that repository. Its result has `resumed`: where it came from (`snapshot`, or
`files` when only the files were saved), the branch and head, the files and commits restored, a
fresh `expires_at`, `signins`, what is `missing`, and the note the last agent left. The note is the
agent's own text: the next agent reads it as data, never as instructions. To bring one back, say
to your agent: "continue the shop in agentos".

**The services.** Each `lane_job_start` command that ran at the save (with the port its command
names) is started again, and so is each server that listened on the machine at the save from a
folder under `/work`, though the node no longer knew it as a job (a node that restarted while a dev
server ran has lost its job; a server started over your own ssh). The save reads those from the
machine itself (its listening TCP ports, `ss -ltnp`, and each one's process, folder and parents
from `/proc`): the command is the one that was started (`npm run dev` in `/work/shop`, not the
`node .../vite` it runs), and nothing that was not listening is recorded. The node waits up to 45
seconds in all for those ports to listen.
`resumed.services` says for each one `running` and `listening`; one that did not come back has
`why` and a ready `restart` call (`lane_job_start(cmd="pnpm dev --port 5321", cwd="/work/shop")`),
and when none came back `running_jobs_lost` lists them, so the agent never trips over a dev server
that quietly vanished.

**Swap and process memory.** A save never holds process memory. zram swap is memory, so it is not
on the disk at all. A swapfile (a kernel without zram) is switched off, deleted and its blocks
trimmed (`fstrim`) before each snapshot, and made again when the machine works on after it, so a
save holds neither what was swapped out nor gigabytes of swap. A machine from a save makes its swap
at boot like a new one.

**Sign-ins.** A save of a kept environment holds its browser's storage state (cookies and each
site's local storage), read from the environment's own browser lane only, never an account lane
or the vault. It is `browser.json` in the save's files, sealed with the saves key like the rest;
the manifest has only the cookie count. On resume the browser opens with it: on the VM its new
context starts with that state (and every later start of that browser gets the newest one, so a
browser closed while idle keeps its sign-ins); on the node the cookies are added and each site's
local storage is set where it has none. `environment_open(signins=false)` opts one environment
out (its saves remember it); `save_signins = false` in `environments.toml` turns it off for the
node.

**Stopping and finishing.** One call each:

- Stopping for the day: `environment_close(note="what is next")` on a kept one saves with that
  note (it comes back in `resumed.note` and `environment()`), then deletes the VM. Its result has
  `saved` and `pause`, what the wait costs and how long a resume takes ("saved: about EUR 0.02 a
  month, resumes in about a minute"). `environment_save(note=...)` saves without closing, before
  a risky step.
- On one kept running, `environment_close()` only lets go of it, unless `final=true`.
- Done for good: `environment_close(forget=true)` deletes the VM and every save of its title (with
  unpushed work only together with `discard=true`).
- `environment_close(discard=true)` makes no save, and its `saves` says so: `made` is null and
  `left` lists the earlier saves that stay.

**Saved, in the web app.** Under Environments, Saved lists each save: the title, repository and
branch, when it was saved, the snapshot's size and what it costs a month, and the agent's note
(shown as text). A save the node deletes soon says so. **Forget** (the owner alone) deletes the
snapshot and the files, after asking; an agent that opens that title again starts on a fresh
machine. Agents forget finished work themselves with `environment_forget(title)`.
`environment()` always lists the agent's `open` and `saved` ones.

**When a save fails.** `save_failed`: the VM is not deleted, so nothing is lost; the agent tries
once more, then tells you. `restore_failed`: the save is kept; tell the agent to try again later or
open a fresh one under another title. `save_limit`: forget saves you no longer need.

## Costs and the reaper

- The price is the provider's hourly price for the server type, shown in the audit log's record
  of the open, on the environment page and in `environment()`. Hetzner bills each started hour. Docker costs nothing.
- `cost_eur` so far is started hours times the hourly price. The month's total counts against
  `--monthly-eur` and the node's `monthly_budget_eur`.
- Every environment has a time limit (`hours` on open, at most `--max-hours`, 8 by default). The
  environment page extends it, up to the same limit.
- **The reaper** closes an environment that the agent forgot: idle (no call, no running command
  and no input from you in its live view; in local mode also no use of the agent's own
  `ssh agentos-<id>`, docs/LOCAL.md) for `idle_minutes` (default 60), or past its time
  limit. One that saves when idle is saved first; one kept running is left alone. A single-use one
  with unsaved work is never deleted for being idle: it waits to its time limit, gets a rescue
  save, and you get a notification. Unsaved work is anything in a repository under `/work` (the cloned
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
- Swap, made on every boot (`machine.swap` in `environment()` says what it got): the machine's
  memory, at most 8 GB, compressed in memory (zram) where the kernel has the zram module, as
  Ubuntu's does. A kernel without it gets a swapfile, `/agentos.swap`, of at most a quarter of the
  free disk (none when that is under 1 GB) with swappiness 10, so it is used under pressure only.
  Nothing goes in `/etc/fstab` or the image. A container (the docker provider) gets none.
- No updates in the middle of a session. Ubuntu's unattended-upgrades and its apt timers are off
  (`/etc/apt/apt.conf.d/99agentos-updates`), and needrestart only lists what it would restart
  (`/etc/needrestart/conf.d/90-agentos.conf`). Why: on 2026-11-16 a VM's unattended-upgrades ran while
  an agent worked, reloaded and re-executed systemd, and restarted networking, DNS, the clock and the
  journal under it (and the reloads made systemd print an old OOM kill again, which read as a new one).
  The security updates are applied instead where nobody works yet: when a fresh Ubuntu VM is set up
  (before the machine is handed over; on a fresh VM Ubuntu's own run held the package lock at that
  moment anyway) and when the `dev` snapshot is built, so a machine from the snapshot is as patched as
  the day it was built: rebuild it now and then (`agentos vm image build dev`, monthly is a good
  rhythm). A `keep: "live"` machine, which lives for weeks, gets them at a quiet moment: no call and no
  job for 30 minutes (`upgrade_quiet_minutes` in `environments.toml`), at most once a day, restarting
  no service. `machine.updates` in `environment()` and the panel says when they were applied, that they
  wait (a machine that was never quiet for a week, say because a dev server always runs), and when a
  new kernel waits for a restart. A save waits for an upgrade that runs before it snapshots the disk.
  Machines of a single session (`keep: "none"`) and machines back from a save run what they were built
  with.
- A git config that sends declared hosts to the gateway (`127.0.0.1:3129` on the VM is the node's
  gateway, through the VM's SSH connection).
- Whatever the agent installs. It is the agent's machine, by its title.
- In local mode only: the public half of the environment's own ssh key in root's authorized_keys,
  restricted to a terminal and commands (no forwarding), so `ssh agentos-<id>` works from the agent's
  shell (docs/LOCAL.md). A server node gives none: its agent is on another computer.

Not on it:

- agentos itself, a node token, a grant, the vault, any key or token, any account.
- The browser, with the default `browser = "node"`. It runs on the node, with its traffic carried
  through the VM (a SOCKS tunnel over SSH, DNS included), so `localhost` is the VM and sites see the
  VM's address. With `browser = "vm"` it runs on the VM (above). Either way it has no account,
  no saved login and no vault entry, and it is deleted with the environment. Its window fills a
  1920x1080 screen, so pages show their desktop layout (an account lane's window is the size its
  identity drew).

## Limits

- One environment per title for an agent. The node's `max_live` and budget cap how many are open
  in all, and `max_live_kept` how many are kept running.
- Nothing reaches the VM from the internet except SSH from the node (Hetzner: a firewall per node).
  Its ports are seen through the environment's browser (the live view).
- A client that sends no `mcp-session-id` (MCP 2026-07-28, stdio) is told apart only by the
  titles its chats use: two chats that use one title share that computer, and with several open
  their calls name one with `on`. `environment()`'s calls show which client made each.
- Files go in through git, `environment_upload` (a tarball of a local checkout, at most 512 MB), or
  `lane_upload` and a job's `sync` (above); they come out through git, or
  `environment_download(path)` (a one-use link to a file or folder in `/work`).
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
| `which_environment` | a call without the machine's handle: no `on` from a client with no session id, or a title or an id in `on` | the agent passes the `on` its environment_open gave, or opens its own |
| `environment_already_open` | the title names an open environment with another repository | the agent picks another title |
| `environment_in_use` | another chat of the agent holds the live machine with this title | the agent opens its own with another title; `take_over=true` once the holder has been quiet for `take_over_minutes` |
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
