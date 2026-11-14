# Design documents for the big bets

One document per bet, each with goals, the owner's stories, architecture, a security
model, a phased plan with ready-to-run builder briefs, and open questions with a
recommended default. Builders follow the defaults unless the owner says otherwise.

The Status column was checked against the code on 2026-11-12. Where a document and the code
differ, the code and the reference docs in `docs/` win.

## Building with agents (in progress)

| Document | Verdict | Status |
|---|---|---|
| [environments.md](environments.md) | agentos gives every agent its own computer in the cloud: a VM per chat with a terminal, a real browser and the repo, git through the node's gateway, deleted when the work is pushed. The plan, the owner's decisions of 2026-11-13 and the contract between its builders. | Being built ([ENVIRONMENTS.md](../ENVIRONMENTS.md) is the user guide) |
| [design-v2-polish.md](design-v2-polish.md) | The polish and UX pass on the calm dashboard prototype (design v2): what changed per screen and why, studio's measured findings before and after, open questions for the owner, and the problems studio had on its first real design job. | Done, 2026-11-12 |
| [studio.md](studio.md) | Agents write the code; agentos runs it, says whether the result is good, and remembers who it is for. Studio adds the design part: a plain review browser that is not a lane, measured page checks, token and component matching, diffs, blind panels and a bench, around a profile that also holds products, reviewers and audiences. | Plan, 2026-11-12. Nothing built yet; wave 1 builders started |
| [studio-ax.md](studio-ax.md) | The agent experience rules every studio tool follows: one call per question, nothing required that agentos knows, small results with handles, fixable findings, stable ids and deltas, honest coverage, long work as jobs. A small model must finish the loop with only the descriptions and the skill. | Requirements for the studio build |

## The other big bets

| Document | Verdict | Status |
|---|---|---|
| [always-on-agents.md](always-on-agents.md) | Build hosted agent lanes and the events layer as one feature: an `agent` lane preset that keeps the owner's own Claude Code or Codex resident (the CLI signs itself in inside the lane; agentos never holds the token), one grant per turn seeded with the event's taint, and a durable event queue with dedupe, quiet hours, rate limits, loop limits and a circuit breaker. About 6 builder-weeks for phase 1. | Events and triggers built (`automation` toolset); agent lanes not built, they need your Claude login on the node |
| [agent-identity.md](agent-identity.md) | Prototype behind a flag. Web Bot Auth is still an IETF draft, the spec and Cloudflare disagree on one header, and signing is an identity layer, not a fix for fingerprinting. Sign with a Playwright route in the engine, one key per lane, off for every host until enabled. | Phase 0 spike running (throwaway profile, test endpoints only) |
| [desktop-nodes.md](desktop-nodes.md) | A `desktop` lane kind on a "desk" (a macOS user session on a home Mac mini, a macOS VM, or a time-limited lent session on the daily Mac), the Accessibility tree as the snapshot with refs, one signed Swift helper app for the privileged parts, the Keychain holding keys rather than agent-readable secrets. Quick win: a Linux VM node on the Mac mini for the home IP. | Waiting on your answers (Mac mini, Apple Developer Program) |
| [connectors.md](connectors.md) | Connectors are signed data that compile into normal runner flows (`connector.<site>.<op>`), with a contract hash that survives locator repairs, declared typed outputs, an "acting as" check, and failure before posting, never half way. Official APIs are alternative backends behind the same operation. | Built (`connectors` toolset, one tool per site operation); canaries need your throwaway accounts |
| [workspace-memory.md](workspace-memory.md) | A node-side `agentos.memory` package, one SQLite file per workspace, append-only versions, keyword search, trust decided by the node (owner, node, agent, untrusted), labels that travel with the data so memory cannot launder untrusted text, decisions through the approval machinery. | Store, service and tools built (`memory` toolset) |
| [phones.md](phones.md) | On a daily phone, apps can no longer read SMS codes unattended (Android 17 holds them, Play limits SMS permissions, iOS gives none), so per-code consent is the default: first a code card in the panel, then a paired phone channel, an Android companion using SMS User Consent, and an iOS Shortcut per sender. agentos never taps "approve on your other device". | The code card built; the phone channel and companions not built |
| [egress-guard.md](egress-guard.md) | Enforce a per-lane site list, the driving grant's egress list and per-site effect classes on the node without a permanent route: any `context.route` stops the HTTP cache for the whole context (measured). Prevention before an agent acts (the URL, the link or form a click follows), containment of everything else from `request` events, a route only on strict lanes and only while an agent acts. Effect rules unverified. | Built; effect rules need throwaway accounts to verify |
| [browser-and-computer-agents.md](browser-and-computer-agents.md) | Do not embed a browser-agent framework: they speak Chrome's CDP and our lanes are Camoufox (Firefox), and their input teleports. Build a small node-side "pilot" that thinks with a model (subscription brain first) and acts only through agentos tools under a child grant: `repair` proposes locator patches, `resolve` finds a target for a deterministic step, `login_assist` gets past benign screens, `lane_do` runs a goal with commits behind approvals. Computer-use models (Claude, OpenAI, Gemini; UI-TARS and peers locally later) only suggest actions; `human.py` performs them. | The pilot and `lane_do` built (`worker` toolset) |
| [engagement-engine.md](engagement-engine.md) | The system engages, agents only set levers: an `agentos.engage` node service turns a program ("warm up 10 days, then 15 likes and 3 comments a day on coffee, I approve comments") into seeded per-account sessions of verified connector operations, with caps at a quarter to half of the folklore numbers, a slow ramp, per-operation holds and a backoff ladder, no engagement among the owner's own accounts, comments drafted by the brain with no tools and approved by default, and the official API for posting and own-post replies wherever it exists. | Built (`agentos.engage`, [ENGAGEMENT.md](../ENGAGEMENT.md)) |
| [stealth.md](stealth.md) | Audit of what anti-bot vendors see in a Playwright-driven Camoufox lane and of lane isolation, measured: no automation trace, workers and frames agree, a dead proxy fails closed. Fixed the real window position (#974) and per-lane CPU sharing (one slice for all lanes); new lanes claim Linux (#895), and a pin's Firefox version follows the installed browser. `agentos lane audit` and `agentos lane stealth-check` catch regressions. | Built; the owner's decisions of 2026-11-04 are in |
| [keepalive.md](keepalive.md) | Keep idle lanes signed in with a short read-only visit every few days, inside each lane's waking hours in its own time zone, planned from activity the node already records (engagement sessions included), one lane at a time and never past the warm cap or the memory floor. Read-only three ways: the lane gates, a read-only grant on the egress guard, and a visit that can only scroll and open post links. Off until the owner turns it on. | Built; one watched visit per site on a throwaway account before turning it on |
| [federation.md](federation.md) | A share is an ordinary grant signed by the host's controller, narrowed by the guest, and narrowed again per call by the host to one lane for a few minutes; calls go only through the host's controller. Prepare now inside token v2 (issuer, key id, audience, reserved share fields, refusing `peer:` owners). Ship shares between two orgs on one controller before linking two installs. | Phase 0 with token v2 |
| Teams and roles (not published) | Add the member (a human with roles per workspace, signing in with a passkey) next to grants (agents), kept apart by construction, with approval chains. | Members and invites built (`agentos members invite`, [DEVELOPING.md](../DEVELOPING.md#add-a-va)) |
| [vms.md](vms.md) | Disposable cloud VMs as terminal lanes made and deleted by the node, so the provider token never reaches an agent and a reaper deletes a VM the agent forgot. | Phase 1 built (`vm_create`, `vm_destroy`); not yet run against a real Hetzner project |
| [learn-a-site.md](learn-a-site.md) | An agent changes a profile field once in the account's lane; the node turns it into a verified connector operation and syncs that field from then on. | Built for reviewed save endpoints (Instagram bio and website); [PROFILES.md](../PROFILES.md) wins where they differ |
| [redteam.md](redteam.md) | A detector site we own, built like a commercial bot defence, and lanes that visit it the way agents drive them, asked on every change whether we would be flagged. | Built (`agentos lane redteam`) |

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
