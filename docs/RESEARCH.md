# Research

The evidence behind agentos: what people struggle with, and what they ask for. Gathered
November 2026 from GitHub issues, Hacker News, vendor and security write-ups, and
platform docs. Reaction counts are as of then. Items marked (weak) are single-source,
vendor, or self-reported, and should be treated with caution.

## Pain points agentos targets

### Running many agents overloads the local machine

- Claude Code [#85015](https://github.com/anthropics/claude-code/issues/85015): two
  background subagents grew to 26 GB and 20 GB on a 16 GB Mac and froze it.
- [#102597](https://github.com/anthropics/claude-code/issues/102597): a laptop left
  asleep overnight burned about 7.1M cache tokens for 7K of output.
- [#101376](https://github.com/anthropics/claude-code/issues/101376): idle sessions kept
  about 121 node processes and 10 GB of MCP servers alive.

Workarounds (restart, cap concurrency, buy a spare Mac mini) treat the symptom. agentos
moves the browsers, and later the agent processes, onto nodes with memory caps.

### Cloud and remote agents have no real browser

- Claude Code [#75632](https://github.com/anthropics/claude-code/issues/75632): cloud
  sessions run in "headless VMs with no browser... I end up reviewing the PR blind," and
  the sandbox blocks Playwright.
- Codex [#22844](https://github.com/openai/codex/issues/22844): the browser tool is
  unavailable in remote threads.
- Hermes [#18715](https://github.com/NousResearch/hermes-agent/issues/18715): "remote
  brain, local hands."

This is agentos's core use case.

### Agents collide when they share a browser, and lose their logins

- Playwright MCP [#1804](https://github.com/microsoft/playwright-mcp/issues/1804): eight
  agents "navigate over each other, corrupt form state." Closed as not planned.
- [#1530](https://github.com/microsoft/playwright-mcp/issues/1530): each connection gets
  an ephemeral context, so agents "re-login on every session start." Closed as not
  planned.
- Skyvern [#6712](https://github.com/Skyvern-AI/skyvern/issues/6712): abandoned sessions
  left about 36 Chrome processes using 14 GB until the pod was killed.

Lanes (persistent, shared, sleeping profiles with per-tab locks and an idle reaper) are
the direct answer.

### A cloud VM gets blocked where a home machine does not

- Grok Bot in Cursor was blocked by Cloudflare on Suno and Shopify even with a human
  driving ([forum, Aug 2026](https://forum.cursor.com/t/grok-bot-agent-computer-blocked-by-cloudflare-hcaptcha-on-suno-create-often-silent-fails/168082)).
- Anti-bot systems weigh IP reputation, the network handshake, the browser fingerprint,
  automation leaks and behavior together, and they have to agree. A datacenter IP with a
  generic, GPU-less fingerprint fails that test; a persistent profile on a home-like IP
  with a coherent fingerprint passes it for ordinary use.
- cua [#3136](https://github.com/trycua/cua/issues/3136): an agent "had to infer both
  conditions from visible page text," i.e. there was no structured blocked signal.

agentos's answer is parity for your own accounts (persistent profile, one steady proxy,
a coherent engine), a startup self-check, and a structured blocked state with backoff.
Not evasion at scale (see PRINCIPLES.md).

### Posting to the wrong account

- Hermes [#111151](https://github.com/NousResearch/hermes-agent/issues/111151): one
  profile's session used another profile's credentials, so "a scheduled post can be
  created on the wrong account with no error anywhere."
- Hermes [#63902](https://github.com/NousResearch/hermes-agent/issues/63902): email sent
  from the wrong account.

Workspaces with hard isolation, plus a check that the logged-in account matches the
workspace before any write, address this.

### Agents report success when nothing happened

- browser-use [#6068](https://github.com/browser-use/browser-use/issues/6068): the agent
  enabled a disabled button, placed the order, and reported plain success; a prompt rule
  against it changed nothing across 8 of 8 runs.
- [#6031](https://github.com/browser-use/browser-use/issues/6031): on a 403 page, it
  reported success in 3 of 3 runs.

Proof-of-done (confirm the result by reading it back, e.g. the live post URL or an order
id) is needed, checked outside the agent.

### Secrets leak into context, logs and config

- Claude Code [#32733](https://github.com/anthropics/claude-code/issues/32733): the env
  UI warns you not to add secrets. [#59094](https://github.com/anthropics/claude-code/issues/59094):
  an agent read live brokerage keys from `.env` into the transcript.
- GitGuardian (Mar 2026) found about 24,000 secrets in MCP config files on public GitHub
  ([report](https://blog.gitguardian.com/the-state-of-secrets-sprawl-2026-pr)).
- Skyvern [#8621](https://github.com/Skyvern-AI/skyvern/issues/8621): network recordings
  were written with plaintext cookies and passwords.

A vault that resolves secrets only at the node, into auth fields, and redacts them
everywhere else, is the fix. Note the holes it must respect: do not resolve a placeholder
into a request body an allowed host could echo back; cookies are secrets too.

### Permissions, cost and runaway sub-agents

- Claude Code [#68619](https://github.com/anthropics/claude-code/issues/68619):
  sub-agents spawned 50+ levels deep, ignored the cap, and "permission denials trigger
  further agent spawning"; one case used 4M tokens in under 5 minutes.
- [#73438](https://github.com/anthropics/claude-code/issues/73438): "spawned 142 agents
  that ate through my 5-hour usage limit in 20 minutes."
- Scheduled routines fail on every MCP call with "requires approval"
  ([#61015](https://github.com/anthropics/claude-code/issues/61015)).

Grants that carry a budget (money, tokens, spawn depth and fan-out), where running out
stops the whole sub-tree instead of spawning more, address both the cost and the
pre-approval problem.

### Security: prompt injection and poisoned tools

- OpenAI said prompt injection is "unlikely to ever be fully solved"
  ([Fortune, Dec 2025](https://www.fortune.com/2025/12/23/openai-ai-browser-prompt-injections-cybersecurity-hackers)).
  Anthropic reported strong attacks still succeeding 3.8% of the time on its newer model
  before safeguards ([blog](https://claude.com/blog/claude-in-chrome-generally-available)).
- A security audit found 341 malicious skills in a popular agent's marketplace, most
  delivering a macOS stealer through fake "prerequisites"
  ([The Hacker News, Feb 2026](https://thehackernews.com/2026/02/researchers-find-341-malicious-clawhub.html)).
  Follow-up research showed evasion still working months later
  ([Unit 42](https://unit42.paloaltonetworks.com/openclaw-ai-supply-chain-risk/)).
- A one-click RCE in a widely-run agent (CVE-2026-25253) came from unchecked WebSocket
  origins, with tens of thousands of instances exposed.

This is why agentos is closed by default, why grants are enforced at the node (so a
hijacked agent is boxed by what it holds), and why flows are curated rather than an open
marketplace. A real hole to design around: attached agents (Claude Code, Codex) keep
their own shell and network tools, so grants are advisory unless the attached agent is
locked down to route side effects through agentos.

## What people are asking for (demand signals)

- **Several accounts on one site.** The largest single cluster: Claude Code
  [#27302](https://github.com/anthropics/claude-code/issues/27302) (446 upvotes),
  [#44243](https://github.com/anthropics/claude-code/issues/44243) (multiple Slack
  workspaces), Codex [#20500](https://github.com/openai/codex/issues/20500).
- **Logins the agent can complete, where current integrations cannot reach:** Claude Code
  [#83380](https://github.com/anthropics/claude-code/issues/83380) (admin panels with no
  API, worst from a phone), [#96762](https://github.com/anthropics/claude-code/issues/96762),
  [#102231](https://github.com/anthropics/claude-code/issues/102231).
- **The session (and its browser) following you to a remote host or phone:** Codex Remote
  Development (694 upvotes) and Remote Control (411), both popular, plus
  [#23200](https://github.com/openai/codex/issues/23200).
- **Pre-approved tools and scopes for agents:** Claude Code
  [#6915](https://github.com/anthropics/claude-code/issues/6915) (270), tool filtering
  (225), agent-scoped config (183).
- **Draft-vs-send and phone approvals:** push on pending approvals
  ([#29438](https://github.com/anthropics/claude-code/issues/29438), t3code
  [#780](https://github.com/pingdotgg/t3code/issues/780)); a Tell HN about an agent that
  "accepted and signed a contract" because signing looked like saving a draft.
- **Logged-in sites as deterministic tools:** a Show HN on reverse-engineering web apps
  into agent tools drew strong interest, with the main objection being "how do I know it
  will not delete my account." This argues for open, deterministic, effect-classed flows.
- **Watchers behind a login that trigger runs**, and **visual proof of UI work** (PR
  videos, including mobile).

## Platform shifts worth riding

- **MCP from phone apps.** The MCP spec (2026-07-28) and custom connectors in the Claude
  and ChatGPT apps mean a self-hosted MCP server with a public HTTPS endpoint and OAuth
  can be used from a phone. This is agentos's distribution channel.
  ([spec changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog))
- **Nodes as the environment for computer-use models.** Gemini, Claude and OpenAI shipped
  computer-use models; none host the machine they run on. Nodes fill that.
- **Scoped agent payments.** Stripe Issuing for agents offers per-agent virtual cards with
  limits and a real-time authorization webhook
  ([docs](https://docs.stripe.com/issuing/agents.md)). A grant can answer that webhook
  and approve only a matching charge. Bring-your-own issuer keeps it self-hostable.
- **Official Apple Passwords import.** iOS/macOS 26 ship a user-initiated Credential
  Exchange export API, the sanctioned way to move Apple Passwords into a vault
  ([WWDC25 passkeys/OS26](https://www.corbado.com/blog/wwdc25-passkeys-os26.md)). Prefer
  this over reverse-engineered helpers.
- **Agent identity / signed agents.** Cloudflare's Web Bot Auth (RFC 9421 signatures)
  lets a registered agent identify itself and be allowed in
  ([docs](https://developers.cloudflare.com/bots/concepts/bot/signed-agents/)). A future
  per-lane "signed" mode fits sites that reward it.
- **Disclosure.** The EU AI Act's transparency duty (Article 50) applies from Aug 2026, so
  labeling agent actions as automated is both polite and required in places.

## Collaboration: mechanics worth copying

For sharing access without sharing passwords, the trusted patterns are: Tailscale's
share-a-device (a shared node answers you but cannot reach into your network), Plaid's
"manage my access" page with one-click revoke, 1Password's email-verified share links,
and above all the platforms' own delegation (Facebook Page roles, OAuth grants, seller
user invites), which keep the account with its owner. agentos adopts the share-a-lane and
official-delegation routes, and drops login-capture pages (PRINCIPLES.md #9).
