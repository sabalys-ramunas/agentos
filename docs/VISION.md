# Vision

## The one line

**agentos gives every agent its own computer in the cloud.**

You open a chat. When the agent needs a machine, or you tell it to work in agentos, it asks for
an environment: a fresh VM with a terminal, a real browser and your repository. It works there,
pushes a branch, and the VM is deleted. You watch, and take over when you want to.

## The problem

Coding agents are good at writing code and bad at having somewhere to run it. Today they run on
your laptop, as you:

- **Your machine is the test bench.** Installs, builds, test suites, dev servers and browsers
  heat it up, fill it with leftovers and stop when the lid closes.
- **Everything you are is in reach.** Your shell holds your SSH keys, your tokens and your
  logged-in browser. One bad install script or one injected web page, and it acts as you.
- **The browser is not real, or it is too real.** A headless browser cannot show you what the
  agent saw; your own browser is signed in to everything you own.
- **You cannot see or stop it.** A long run is a scrolling terminal. Nothing says what it cost,
  what it touched or what is still running.

## What agentos is

A computer that the agent you already use can ask for, over MCP: Claude Code, Codex, T3 Code, the
Claude or ChatGPT app, or any other MCP client. agentos is **not** an agent, a chat or an IDE. It
gives whichever one you use:

- **An environment per chat**: a VM rented by your node (Hetzner Cloud, or a Docker container),
  with the repository cloned, a terminal and a browser whose traffic leaves from the VM. Calls
  without a lane go there, so the agent's tools just work, and `localhost` is its dev server.
  Deleted when the work is pushed, by the agent or by the reaper.
- **A trust layer**: a grant per agent that says what it may do and expires, approvals for money
  and for pushes to protected branches, a vault that keeps keys and tokens off the VM, a git
  gateway that pushes for it, and an audit log of every call.
- **A way to watch**: the environment's commands, calls and browser, live, from a link, the web
  app or the chat, with a button to take the browser over.

Everything else is a feature on top: studio's page checks, profiles with a brand, logged-in
browser lanes for your own accounts, routines, the engagement engine.

## The building blocks

- **Node.** The machine agentos runs on: a VPS, a home box, a spare PC. It rents environments,
  keeps the vault and the log, and serves MCP and the web app. It only dials out.
- **Environment.** A cloud VM for one chat: a terminal lane (`env/<id>`) and a browser lane
  (`env/<id>-browser`) the node makes and deletes.
- **Lane.** An isolated place on a node where one thing runs: a terminal, a service, a VM, or a
  browser. An account lane is a logged-in browser with its own proxy and device, for one account.
- **Workspace.** One per business or person. It owns its lanes, logins and rules, and nothing
  crosses between workspaces. Environments live in the `env` workspace.
- **Grant.** A signed, expiring permission an agent holds and can hand down, only narrowed.
- **Vault.** Keys, tokens, logins and one-time codes, sealed on the node. The model never sees
  them ([PRINCIPLES.md](PRINCIPLES.md)).
- **Profile.** Who the work is for: a person, a business or a product with its brand. Agents
  propose changes; only the owner approves them ([PROFILES.md](PROFILES.md)).

The full technical model is in [ARCHITECTURE.md](ARCHITECTURE.md).

## How an agent sees it

```
environment_open(title, repo)     -> a VM for this chat, the workdir, links for the human
lane_exec(cmd, cwd), shell_open   -> commands on it (no `on`: this chat's environment)
browser_navigate(url)             -> its browser; localhost is the VM
ui_check(url), ui_diff(url)       -> is the page it built any good? measured, not guessed
environment()                     -> ports, git, cost, the last calls
environment_close()               -> refuses until the work is pushed, then deletes the VM
```

The MCP intro and the `agentos-environment` skill tell the agent when to take one: the human
asks for agentos or a VM, or the work needs a machine (builds, tests, a dev server, a browser).

## The features on top

- **Studio** checks a page in a real browser and answers with measurements: layout at each width
  and theme, contrast, tap targets, names, health, theme leaks, and what a change did
  ([STUDIO.md](STUDIO.md)). It works on the environment's `localhost`.
- **Profiles and brand**: the tokens, components and rules a product's pages are measured against.
- **Account lanes**: persistent, logged-in browsers on Camoufox, each with one steady proxy, a
  device of its own and a network self-check, for testing other sites and for work an agent does
  as you, on accounts you own ([PROXIES.md](PROXIES.md), [RESPONSIBLE-USE.md](RESPONSIBLE-USE.md)).
  This is where agentos started, and the hard part of it (looking like an ordinary computer) is
  in [design/stealth.md](design/stealth.md).
- **Routines**: flows recorded once and replayed with no model, workflows on a schedule, and
  brains (your own `claude -p` or `codex exec`) that keep working with your laptop closed
  ([WORKFLOWS.md](WORKFLOWS.md)).
- **The engagement engine**: a slow, capped program for your own business accounts, with
  comments you approve ([ENGAGEMENT.md](ENGAGEMENT.md)).

## What agentos is not

- **Not an agent, chat or IDE.** It gives agents a computer and never becomes the agent. Keep
  the one you like.
- **Not a CI system.** An environment runs what the agent runs, for one chat; it is not a
  pipeline or a runner fleet.
- **Not a hosting platform.** Environments are deleted when the work is done, and nodes only dial
  out. A dev server is reached through the node's links, not a public address.
- **Not a model reseller.** Brains are your own subscriptions and local models.
- **Not a flow or skill store.** Flows stay first-party and reviewed.

## Roadmap

1. **Environments everywhere**: the Docker and Hetzner providers, a `dev` image, the web app's
   environment page, then more providers (billed by the minute) and a warm pool so a VM is ready
   when the chat starts.
2. **Watching and taking over** from the phone: the environment's browser and terminal, and the
   approvals, in one place.
3. **Studio on every change**: the page checks run on the environment as part of the agent's
   loop, with the product's brand.
4. **Files and ports**: copying files in and out of the VM, and sharing a dev server link with a
   teammate for an hour.
5. **Teams**: a person's own agent gets a grant to a workspace, with roles, and the same audit.

## Where this could go as a product

An open core under Apache-2.0 (the node, environments, lanes, the vault, grants, MCP, studio) that
is fully useful to one person on their own node, plus a hosted cloud for what costs money or spans
people: VMs without your own provider account, the hosted web app and phone approvals, teams and
shared access. Comparable open-core products grew by being useful to a single self-hoster first.
agentos should earn that before it sells anything.
