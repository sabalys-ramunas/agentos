# Vision

## The one line

**Every agent gets its own computer, and it stays.**

A computer for your agent, with a browser, a terminal and a disk that stay. You open a chat. When
the agent needs a machine, or you tell it to work in agentos, it asks for its computer: a VM with
a terminal, a real browser and your repository. It works there and pushes a branch. You watch, and
take over when you want to.

When the work goes idle, the whole machine is saved and the VM deleted, so it costs cents a month.
Tomorrow, in any chat, "continue the shop in agentos" brings it back as it was: the installs, the
branch, the dev server and the browser still signed in. A one-off fix can use a machine once
(`keep="none"`), and a machine can stay on when you ask for it. The agent's thinking stays on your
laptop, in the client you already use (Claude Code, Codex, T3 Code). agentos runs next to it, on
the laptop first or on a server, and rents the machines in your own account.

## The problem

Coding agents are good at writing code and bad at having somewhere to run it. Today they run on
your laptop, as you:

- **Your machine is the test bench.** Installs, builds, test suites, dev servers and browsers
  heat it up and fill it with leftovers.
- **Somewhere else means starting over.** A sandbox for one session forgets everything: the next
  chat installs the tools again, rebuilds the cache, starts the dev server from nothing and signs
  in to every site again.
- **Everything you are is in reach.** Your shell holds your SSH keys, your tokens and your
  logged-in browser. One bad install script or one injected web page, and it acts as you.
- **The browser is not real, or it is too real.** A headless browser cannot show you what the
  agent saw; your own browser is signed in to everything you own.
- **You cannot see or stop it.** A long run is a scrolling terminal. Nothing says what it cost,
  what it touched or what is still running.

Hosted agent computers (xAI's Grok Bot, OpenAI's Dots) answer part of this with an always-on
machine for their own agent, on their own computers. agentos is the same idea for the agent you
already use, on machines you own the account for, one per agent or chat.

## What agentos is

A computer that the agent you already use can ask for, over MCP: Claude Code, Codex, T3 Code, the
Claude or ChatGPT app, or any other MCP client. agentos is **not** an agent, a chat or an IDE. It
gives whichever one you use:

- **A computer of its own**: a VM rented by agentos in your account (Hetzner Cloud, Morph Cloud, or
  a Docker container), with the repository cloned, a terminal and a browser with tabs whose
  traffic leaves from the VM. The agent's tools reach it without naming it, and `localhost` is its
  dev server.
- **That stays**: saves when idle by default (the whole machine is saved as a disk snapshot plus its
  files, or paused with its memory on Morph, the VM deleted, and the same title brings it back from
  any chat), single use for a one-off fix (deleted when the work is pushed), or kept running
  (always on, billed every hour, only when the owner allows it).
- **A browser that can stay signed in**: the owner binds logins from the vault to a computer
  (`agentos environments login add <site>`); the agent calls the login tool on a sign-in page, the
  node types the stored login into that computer's browser, and signs it in again after a logout
  without asking. The model never sees the password. This is for opening the accounts the work
  needs when it needs them, not for automating them.
- **A trust layer**: a grant per agent that says what it may do and expires, a vault that keeps
  keys and tokens off the machine (only the logins you bind are typed into its browser), a git
  gateway that pushes for it, app secrets swapped in on the way out, a network policy, limits on
  spend, and an audit log of every call. Nothing inside the computer waits for a person: a guard
  is a limit or a refusal that names the fix.
- **A way to watch**: the computer's commands, calls and browser, live, from a link, the web app
  or the chat, with a button to take the browser over, and a replay after it is gone.

Everything else is a feature on top: studio's page checks, profiles with a brand, account
automation and routines.

## The building blocks

- **Node.** The machine agentos runs on: your laptop, a VPS, a home box. It rents environments,
  keeps the vault and the log, and serves MCP and the web app. It only dials out.
- **Environment.** The agent's computer: a VM with a title, its terminal and its browser, which the
  node makes, saves and deletes. Its `keep` says how it stays (`save` by default). In the technical
  model its terminal and its browser are two lanes (`env/<id>` and `env/<id>-browser`).
- **Save.** A kept environment's disk snapshot (or paused machine) plus its files, with the agent's
  note for the next one. Forgotten by the agent or the owner when the work is done.
- **Prepared machine.** An image with a repository's setup already run, so the next open boots with
  the app running; a warm pool can keep one booted ahead of the chat.
- **Lane.** The technical unit under all of this: an isolated place on a node where one thing runs
  (a terminal, a service, a VM, or a browser). An account lane is a logged-in browser with its own
  proxy and device, for one account.
- **Workspace.** One per business or person. It owns its lanes, logins and rules, and nothing
  crosses between workspaces. Environments live in the `env` workspace.
- **Grant.** A signed, expiring permission an agent holds and can hand down, only narrowed.
- **Vault.** Keys, tokens, logins and one-time codes, sealed on the node. A login the owner binds
  to a computer is the permission to use it there. The model never sees them
  ([PRINCIPLES.md](PRINCIPLES.md)).
- **Profile.** Who the work is for: a person, a business or a product with its brand. Agents
  propose changes; only the owner approves them ([PROFILES.md](PROFILES.md)).

The full technical model is in [ARCHITECTURE.md](ARCHITECTURE.md).

## How an agent sees it

```
environment_open(title, repo, keep) -> its computer, or the saved one with that title back (resumed)
terminal_exec(cmd, cwd, on), shell_open -> commands in its terminal (`on`: the handle the open gave)
browser_navigate(url), browser_tabs -> its browser; localhost is the VM
login                               -> signed in with a login the owner bound; the model never sees it
ui_check(url), ui_diff(url)         -> is the page it built any good? measured, not guessed
environment()                       -> ports, git, cost, the last calls; or the open and saved ones
environment_save(note)              -> the whole machine saved now, with a note for the next agent
environment_close(note)             -> saved for the next chat; single use: deleted once pushed
environment_forget(title)           -> the saves of finished work deleted
```

The MCP intro and the `agentos-environment` skill tell the agent when to take one: the human
asks for agentos or a VM, or the work needs a machine (builds, tests, a dev server, a browser).
They also tell it how to keep it: the default saves when idle, `keep="none"` only for a one-off
fix, and kept running only when the person asks for an always-on machine.

## The honest limits

- **The laptop runs the agent.** In local mode the laptop must be awake while a VM works: the
  agent, the commands, the browser and git all go through it. No agent works while the lid is
  closed in this version.
- **Sleep saves, it does not continue.** When the laptop sleeps, each computer that saves when
  idle is saved and its VM deleted; a single-use one with unsaved work gets a rescue save. The
  work continues when the laptop wakes and an agent opens it again.
- **A bound login lives in that browser.** The node types it in, and the machine's browser then
  holds that session (a save keeps it). A computer that runs code you do not trust should get no
  login bound ([THREAT-MODEL.md](THREAT-MODEL.md)).
- **Coming back is not instant everywhere.** On Morph a paused machine is back in seconds with its
  dev server running; on Hetzner a save comes back in about a minute.
- **A server keeps going.** On a server node, environments do not depend on your laptop, but the
  agent still runs where you run it.

## The features on top

Short, because they are not the product's front door:

- **Studio** checks a page in a real browser and answers with measurements: layout at each width
  and theme, contrast, tap targets, names, and what a change did ([STUDIO.md](STUDIO.md)).
- **Profiles and brand**: the tokens, components and rules a product's pages are measured against.
- **Account automation**: persistent, logged-in browsers on a server, each with one steady proxy
  and a device of its own, for work an agent does as you on accounts you own
  ([PROXIES.md](PROXIES.md), [RESPONSIBLE-USE.md](RESPONSIBLE-USE.md),
  [design/stealth.md](design/stealth.md)).
- **Routines**: flows recorded once and replayed with no model, on a schedule or a trigger, and
  brains (your own `claude -p` or `codex exec`) that keep working with your laptop closed
  ([WORKFLOWS.md](WORKFLOWS.md)).

## What agentos is not

- **Not an agent, chat or IDE.** It gives agents a computer and never becomes the agent. Keep
  the one you like.
- **Not a CI system.** An environment runs what the agent runs, for one chat; it is not a
  pipeline or a runner fleet.
- **Not a hosting platform.** A kept environment is a workbench, not a server for the public:
  nodes only dial out, and a dev server is reached through the node's links, not a public address.
- **Not a model reseller.** Brains are your own subscriptions and local models.
- **Not a flow or skill store.** Flows stay first-party and reviewed.

## Roadmap

1. **Computers that stay** (built): saves when idle by default, single use and kept running,
   resume by title from any chat or by the owner, Hetzner, Morph (a save pauses the machine) and
   Docker, prepared machines that boot with the app running, and a warm pool so a machine is
   ready when the chat starts.
2. **Signed in**: logins from the owner's vault bound to a computer, typed into its browser by the
   node, and signed in again after a logout.
3. **Watching and taking over** from the phone: the computer's browser and terminal, the replay and
   the proofs in one place (the web app has most of it; video proofs go into the pull request).
4. **Files and ports**: files in and out of the machine (upload and download are built), and
   sharing a dev server link with a teammate for an hour (still open).
5. **An agent that keeps working with the laptop closed**: the agent's loop on the node, not only
   its machine.
6. **Teams**: a person's own agent gets a grant to a workspace, with roles, and the same audit.

## Where this could go as a product

An open core under Apache-2.0 (the node, environments, lanes, the vault, grants, MCP, studio) that
is fully useful to one person on their own node, plus a hosted cloud for what costs money or spans
people: VMs without your own provider account, the hosted web app and phone approvals, teams and
shared access. Comparable open-core products grew by being useful to a single self-hoster first.
agentos should earn that before it sells anything.
