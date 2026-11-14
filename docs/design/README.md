# Design documents for the big bets

One document per bet, each with goals, the owner's stories, architecture, a security
model, a phased plan with ready-to-run builder briefs, and open questions with a
recommended default. Builders follow the defaults unless the owner says otherwise.

The Status column was checked against the code on 2026-11-14. It uses four words:

- **Built**: the design is in the code. Notes say what still needs the owner.
- **Partly built**: some phases are in the code, others are not.
- **Not built**: a plan or research only.
- **Removed**: it was built and has since been taken out. It stays readable in git history.

Where a document and the code differ, the code and the reference docs in `docs/` win.

## Agent environments (the product)

| Document | Verdict | Status |
|---|---|---|
| [environments.md](environments.md) | agentos gives every agent its own computer in the cloud: a VM per chat with a terminal, a real browser and the repo, git through the node's gateway, deleted when the work is pushed. The plan, the owner's decisions of 2026-11-13 and the contract between its builders. | Built ([ENVIRONMENTS.md](../ENVIRONMENTS.md) is the user guide); later phases still open |
| [vms.md](vms.md) | Disposable cloud VMs as terminal lanes made and deleted by the node, so the provider token never reaches an agent and a reaper deletes a VM the agent forgot. | Built (phase 1, `vm_create`, `vm_destroy`); not yet run against a real Hetzner project |
| [studio.md](studio.md) | Agents write the code; agentos runs it, says whether the result is good, and remembers who it is for. Studio adds the design part: a plain review browser that is not a lane, measured page checks, token and component matching, diffs, blind panels and a bench, around a profile that also holds products, reviewers and audiences. | Partly built: waves 1 and 2 (checks, tokens, brand, diff, MCP tools, the skill; [STUDIO.md](../STUDIO.md)); waves 3 and 4 not built |
| [studio-ax.md](studio-ax.md) | The agent experience rules every studio tool follows: one call per question, nothing required that agentos knows, small results with handles, fixable findings, stable ids and deltas, honest coverage, long work as jobs. A small model must finish the loop with only the descriptions and the skill. | Built: the rules the studio tools follow |
| The end game (not published) | The end game, against xAI's Grok Bot and OpenAI's dots: agentos stays a body, not an agent, and gets kept environments, one take-over screen for laptop and phone, files in and out, the owner's own agent resident in its environment, and a desktop on request. | Not built: research and plan, 2026-11-14. Private until the owner reviews it |
| The unification plan (not published) | The staged plan of 2026-11-07 to make one of each thing (helpers, signals, fakes, splits, one approval path, CLI groups). It opens with a status table per stage. | Partly built: see its status table |

## The web app's design

The classic panel and its prototypes under `panel/prototypes/` were removed on 2026-11-14. `web/` is
the only web app. The prototypes these documents describe are in git history (commit `59634500`
and earlier); the documents stay as the record of why the app looks as it does.

| Document | Verdict | Status |
|---|---|---|
| [app-design.md](app-design.md) | The chosen app design: Workbench as the base, a calmer Home with running jobs, Machines on one screen. | Built in `web/`; its prototype removed |
| [style-style-mono.md](style-style-mono.md) | A style take: precise and monochrome, a colour per kind of work. | Built: Mono is the web app's token set ([web/DESIGN.md](../../web/DESIGN.md)); its prototype removed |
| [style-style-glow.md](style-style-glow.md) | A style take: dark first, light that explains, a hue per kind of work. | Not built: the owner chose Mono; its prototype removed |
| [foundations.md](foundations.md) | Tokens, type roles, tables and every base component. | Built in `web/`; its prototype removed |
| [direction-workbench.md](direction-workbench.md) | A design direction: a precise, keyboard-first tool. | Built as the base of the app design; its prototype removed |
| [direction-canvas.md](direction-canvas.md) | A design direction: the work shown as what it looks like. | Not built: not chosen; its prototype removed |
| [design-v2-polish.md](design-v2-polish.md) | The polish and UX pass on the calm dashboard prototype (design v2), measured with studio. | Removed with the classic panel |

## Accounts, safety and the node

| Document | Verdict | Status |
|---|---|---|
| [egress-guard.md](egress-guard.md) | Enforce a per-lane site list, the driving grant's egress list and per-site effect classes on the node without a permanent route. Prevention before an agent acts, containment of everything else from `request` events, a route only on strict lanes and only while an agent acts. | Built; effect rules need throwaway accounts to verify |
| [stealth.md](stealth.md) | Audit of what anti-bot vendors see in a Playwright-driven Camoufox lane and of lane isolation, measured: no automation trace, workers and frames agree, a dead proxy fails closed. `agentos lane audit` and `agentos lane stealth-check` catch regressions. | Built |
| [redteam.md](redteam.md) | A detector site we own, built like a commercial bot defence, and lanes that visit it the way agents drive them, asked on every change whether we would be flagged. | Built (`agentos lane redteam`) |
| [keepalive.md](keepalive.md) | Keep idle lanes signed in with a short read-only visit every few days, inside each lane's waking hours, one lane at a time and never past the warm cap or the memory floor. Off until the owner turns it on. | Built; one watched visit per site on a throwaway account before turning it on |
| [connectors.md](connectors.md) | Connectors are signed data that compile into normal runner flows (`connector.<site>.<op>`), with a contract hash that survives locator repairs, typed outputs, an "acting as" check, and failure before posting, never half way. | Built (`connectors` toolset); canaries need throwaway accounts |
| [learn-a-site.md](learn-a-site.md) | An agent changes a profile field once in the account's lane; the node turns it into a verified connector operation and syncs that field from then on. | Built for reviewed save endpoints (Instagram bio and website); [PROFILES.md](../PROFILES.md) wins where they differ |
| [browser-and-computer-agents.md](browser-and-computer-agents.md) | Do not embed a browser-agent framework. Build a small node-side "pilot" that thinks with a model and acts only through agentos tools under a child grant; `human.py` performs every action. | Partly built: the pilot and `lane_do` (`worker` toolset); computer-use models not built |
| [workspace-memory.md](workspace-memory.md) | One SQLite file per workspace, append-only versions, keyword search, trust decided by the node, labels that travel with the data so memory cannot launder untrusted text. | Built (`memory` toolset) |
| [always-on-agents.md](always-on-agents.md) | Hosted agent lanes and the events layer as one feature: the owner's own Claude Code or Codex resident in a lane, one grant per turn seeded with the event's taint, and a durable event queue with dedupe, quiet hours, rate limits, loop limits and a circuit breaker. | Partly built: events and triggers (`automation` toolset); agent lanes not built |
| [agent-identity.md](agent-identity.md) | Web Bot Auth behind a flag: sign with a Playwright route in the engine, one key per lane, off for every host until enabled. | Partly built: `agentos.webbotauth`, off by default; the phase 0 spike ran on test endpoints only |
| [phones.md](phones.md) | Per-code consent on a daily phone: first a code card in the panel, then a paired phone channel, an Android companion, and an iOS Shortcut per sender. | Partly built: the code card; the phone channel and companions not built |
| [desktop-nodes.md](desktop-nodes.md) | A `desktop` lane kind on a macOS session, the Accessibility tree as the snapshot, one signed helper app for the privileged parts. | Not built: waiting on the owner's answers (Mac mini, Apple Developer Program) |
| [federation.md](federation.md) | A share is an ordinary grant signed by the host's controller, narrowed by the guest and again per call by the host; calls go only through the host's controller. | Not built, beyond token v2's issuer and key id |
| Teams and roles (not published) | The member (a human with roles per workspace, signing in with a passkey) next to grants (agents), kept apart by construction, with approval chains. | Partly built: members and invites (`agentos members invite`, [DEVELOPING.md](../DEVELOPING.md#add-a-va)); approval chains not built |

## Removed

- **The engagement engine** (programs, sessions and the day plan that liked, followed and
  commented on its own) was removed on 2026-11-14, with its CLI commands, MCP tools and routes.
  The action ledger it read (`policy/ledger.py`) stays and counts every account's actions.
- **The classic panel** (`panel/`) and its prototypes were removed on 2026-11-14. A node serves
  `web/` only.

## What only the owner can provide

- Throwaway accounts on each platform for connector canaries and spikes, including a test
  Facebook Page and a test LinkedIn company page, and an Instagram account with SMS 2FA.
- For desktop nodes: whether there is a Mac mini (and which), and joining the Apple
  Developer Program ($99 a year) so the helper app keeps its macOS permissions.
- For the phone spikes: a phone on Android 16, one on Android 17, and an iPhone on iOS 26;
  a spare phone with a second SIM only if unattended code reading is wanted.
- Signing the node's brain in to the Claude subscription (also needed for agent lanes).

## Defaults builders use unless the owner decides otherwise

- Agent lanes use the CLI's own login; event turns start a fresh session; agent lanes and
  brain tasks share one seat per subscription; Instagram, Facebook and LinkedIn watches at
  most every 20 minutes between 08:00 and 22:00.
- Signed identity is off for every host until enabled per host; one key per lane.
- An agent using the owner token never writes owner-trust memory; memory of a local-only
  workspace is refused to attached cloud agents unless the workspace opts in.
- Four-eyes approval is off per workspace by default (a solo owner keeps today's flow).
- In federation the guest dials the host, so a host needs a public URL (or the hosted
  cloud as a meeting point).
