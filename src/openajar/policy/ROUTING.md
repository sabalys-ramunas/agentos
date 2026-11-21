# Effect routing

Every tool call that does something goes through `ctx.authorize(effect, ...)`. This page
says what happens then: which calls run on their own, which wait for you, and which are
refused. In short: routines run on their own. A post, a send or a change waits for you only
where you (or the agent) turned on "Ask me first" for that routine or account; payments and
things that cannot be undone always wait; and a call that follows text a stranger wrote waits
too, with the reason in plain words. The table is data (`openajar.policy.routing.DEFAULT_RULES`); `openajar policy
routing` prints the one in force, and `$OPENAJAR_HOME/policy.toml` can replace it.

## Order of checks

1. **Known effect.** An effect the node does not know is refused (`unknown_effect`).
2. **Live grant.** Inside its `not_before` and `expires` window (with `clock_skew_s`,
   default 60 s, either side), and no block in its chain revoked. Checked on every
   authorize, so a grant revoked while a call runs stops at the next step.
3. **Scope.** The lane is inside every block's lane scope and the domain inside every
   block's domain list. A grant with no lane scope reaches no lanes (node-level reads
   such as `grant_info` still work). Calls that name no domain skip the domain check.
   A domain is read the way a browser reads it (see "Domains and URLs" below); a string
   a browser could read as another host is refused (`invalid`), never guessed.
4. **Eligibility.** Ladder effects (`read < draft < commit < pay < irreversible`) must
   be at or under every block's ceiling. Specials (`secret`, `exec`, `install_overlay`,
   `install_base`, `service`, `brain`) must be granted by every block, with their
   parameters: lanes for `secret` and `exec`, ports for `service`, provider, tier and
   credential owner for `brain`.
5. **Budget.** A spent budget refuses everything. A stated `cost={tokens, usd}` is
   reserved atomically against every block's budget (a child's spend counts against
   its parents); it is refunded if the call ends up waiting or denied. Costs are debited
   in micro-dollars rounded up (a fraction of a micro-dollar is never free); a cost that
   is not a finite number, or absurdly large, is an invalid request, and a grant with an
   Infinity or NaN budget is refused. In the same transaction the day's **meters** are
   debited (per grant block and per lane: money, tokens, actions by effect, commits per
   hour and per day, `openajar.policy.meters`): a daily or hourly limit reached refuses
   with `limit_reached` before routing, so no approval overrides it. Days end at the
   owner's local midnight; `[meters] warn_at` (0.8) warns once per threshold per day.
   A commit, pay or irreversible call on a lane with a **posting pace** (`openajar.policy.pace`,
   AUD-13) is checked in the same step: outside the lane's waking hours (read in the lane's own
   zone, its pinned fingerprint's unless the owner set one), within `min_gap_minutes` of its last
   publish, or past `commits_per_day` for the lane-local day, it is refused with `lane_paced`, whose
   message says when the lane may next post (lane time and UTC). This covers a publish the commit
   gate holds during browser_act, flows, connector and typed tools, and schedules (the runner moves
   those to the next allowed time instead of failing them). A person driving the lane by hand is
   never paced (the owner in person, a person in control of the lane); runs and agents are,
   including runs the owner starts, and so is a member. An approval never exempts a publish: an
   approved run goes when the pace allows. A call that changes only the node (a vault entry or a
   memory, `vault.*` and `memory_*` tools) posts nothing: it is neither paced nor counted.
6. **Routing table** (below): `auto`, `approval` or `deny`. A commit reads "Ask me first" (below)
   through the `runs_on_own` and `ask_first` facts; pay and irreversible never come out `auto`.
   On a lane of the caller's own agent environment (the `environment` fact) nothing asks: a rule
   that would ask comes out `auto`, whatever a replaced table says, and steps 7 and 8 do not
   apply (pay and irreversible still ask).
7. **Lethal trifecta overlay** (below) can turn `auto` into `approval`.
8. **After untrusted content** (below): once the call's context has read untrusted content, an
   effect in `[taint] downgrade` turns `auto` into `approval`, and only a standing approval bound
   to what the owner saw can cover it.
9. **Approval resolution**, when approval is needed:
   1. a standing approval covering this effect, lane, target and content is used (one
      unit), unless the trifecta triggered (see "Standing approvals"); after untrusted
      content only a bound one counts;
   2. otherwise an approved, unused one-off approval with the same content hash and
      the same grant is consumed;
   3. otherwise, if the owner denied this exact content for any grant of the same family
      (the same root grant) within `denied_cooldown_s`, the call is refused
      (`approval_denied`); after untrusted content, if the owner denied any call that
      followed untrusted content in the family within `[taint] denied_cooldown_s`, a call
      that would ask again is refused (`taint_cooldown`). Neither cooldown refuses a run a
      schedule started (cron or interval, `ctx.scheduled`, set by the runner): nobody asked
      again, so it asks a person anew. The `approval_denied` text says when the denial was
      and when the same call may ask again;
   4. otherwise the existing pending approval for this content is returned, or a new
      one is created and pushed to the owner (at most `max_pending_per_grant` pending
      per grant family). The tool returns
      `{status: "approval_pending", approval_id, summary, check_with: "approval_wait"}`;
      after approval, call the same tool again with the same arguments.

A token never decides what its own grant family asked for: approving, denying or
answering under a grant whose root also requested it is refused (`self_approval`), so an
agent cannot approve itself with its own admin grant or the node token. The owner decides
in the signed-in panel, with `openajar approvals` on the node, or from the phone (below);
a member decides what their role allows (see "Members and approvers"). How strong the yes
must be is a separate question (see "How strong a yes must be").
The preview shows at most 2000 characters of the content; when it is cut, it ends with
how many characters are hidden. The approval always binds the whole content.

## The table

First matching rule wins. An effect no rule matches needs approval (fail closed).

| Effect | When | Then | Why |
|---|---|---|---|
| any | environment | auto | a lane of the caller's own agent environment (its terminal or its browser): the VM and the browser hold no account and no key, so nothing there waits, also after untrusted text. Pay and irreversible still ask |
| read | always | auto | reading is free |
| draft | memory owner trust | approval | a memory decision, or a supersede of an owner entry: the owner sees the exact text; never auto, never standing |
| draft | always | auto | private writes |
| commit | runs on its own | auto | on one account, and "Ask me first" is off for its routine and that account |
| commit | ask first | approval | "Ask me first" is on for its routine or its account (the reason names which) |
| commit | otherwise | approval | acts on no one account (a workspace plan, node settings) |
| pay | always | approval | always a human co-sign; standing approvals cannot cover it; no replaced table makes it auto |
| irreversible | always | approval | always a human co-sign; standing approvals cannot cover it; no replaced table makes it auto |
| secret | always | auto | a password in the vault is the owner's permission to use it (see "Logins" below) |
| exec | lane not isolated | approval | workbench or shared lane, every time |
| exec | first use | approval | first use in this lane |
| exec | otherwise | auto | isolated lane, seen before |
| install_overlay | source pinned and allowlisted | auto | |
| install_overlay | otherwise | approval | unpinned or not allowlisted |
| install_base | always | approval | touches every lane on the image |
| service | TTL over the grant | approval | over `max_ttl_s`, past the grant's expiry, or unstated |
| service | otherwise | auto | |
| brain | metered | approval | any metered spend is treated like pay |
| brain | over quota | approval | past `brain_quota_threshold` of the day's quota |
| brain | otherwise | auto | within quota |

## Facts

The table reads facts the node computes or the handler passes in `facts={...}`. A
missing fact counts as false, which always lands on the safer rule.

| Fact | Source |
|---|---|
| `first_use` | no earlier approved use of this effect on this lane by this grant family (the root grant) in the period: `ever` for exec (`first_use_period`; a replaced table can give another effect one, `day` or `ever`) |
| `isolated` | `facts["isolated"]`, or `facts["isolation"] == "isolated"`, or `facts["preset"]` in account, sandbox, service, locked |
| `source_allowlisted` | `facts["source"]` is pinned (`eco:name@version`, not `latest`) and listed in the grant's `install_overlay.sources` or the node's `install_allowlist`, and in every block's list |
| `ttl_within_grant` | `facts["ttl_s"]` at most every `service.max_ttl_s` and the grant's remaining life (unstated: outside) |
| `metered` | `facts["metered"]`, or a stated cost with `usd > 0` |
| `over_quota` | today's brain tokens (or usd) plus this call's estimate pass `quota * brain_quota_threshold` |
| `memory_owner_trust` | `facts["memory_owner_trust"] is True`: the memory service is about to store a version with owner trust for a call that is not the owner in person (see "Workspace memory") |
| `internal` | `facts["internal"] is True`: the destination is the workspace's own memory on this node |
| `runs_on_own` | the node's own: the call names a lane and "Ask me first" is off for its routine and that lane; a handler's `facts` cannot set it |
| `ask_first` | the node's own: "Ask me first" is on for the call's routine (a flow run's `flow:<id>@<version>`) or its lane |
| `environment` | the node's own: the lane is the terminal (`env/<id>`) or the browser (`env/<id>-browser`) of an open agent environment (openajar.environments) of the caller's grant family (or the owner's, outside MCP). Nothing on it asks: neither the table, nor "Ask me first", nor the untrusted-content downgrade, nor the trifecta; a handler's `facts` cannot set it. Reading a page there still labels the family untrusted, so a call on an account lane afterwards asks as before |

Brain calls state `facts={"provider", "tier", "credential_owner", "metered", "tokens"}`.
Tiers are ordered `small < medium < large` (aliases haiku, sonnet, opus); an unknown tier
is refused when the grant limits the tier.

A fact that a grant limits and the call does not state is outside the limit: a service
under a grant with `ports` must state `facts["ports"]`, a brain call under a grant with a
provider, tier or credential owner must state it. `facts["sink"] = True` says the call
sends data to its `domain` whatever its effect (a navigation carries its URL there), so
the lethal-trifecta overlay applies to it.

## Ask me first

Routines run on their own. Each saved flow (a routine) has an "Ask me first" switch, and so does
each account (a lane, or a connector surface) for what an agent does there outside any routine.
Off is the default. The switches live in `policy.db` (`openajar.policy.ask_first`).

| Call | Result |
|---|---|
| read, draft | runs on its own (an owner-trust memory write still asks) |
| commit in a routine, its switch and its account's switch off | runs on its own |
| commit in a routine whose switch, or whose account's switch, is on | asks first: "Ask me first is on for routine <id>" (or "account <lane>") |
| commit outside any routine, on an account whose switch is off | runs on its own |
| commit outside any routine, on an account whose switch is on | asks first |
| commit on no account | asks first |
| pay, irreversible, anywhere, whatever the switches | always asks; a standing approval never covers it |
| a commit that would run on its own, after untrusted content or under the lethal trifecta | asks first, with the reason in plain words (below) |

- **Which switch.** A flow run's commit carries its approval content (`flow`, `version`, `lane`,
  `params`), so the node knows its routine: it asks when the flow id's switch or the lane's is on.
  A connector run reads its operation's switch (the flow id `connector.<site>.<op>`), its surface's,
  and that of the lane the surface posts through. Any other commit on a lane (a tool, a confirm
  dialog) reads that lane's switch and the switch of each surface that posts through the lane, as the
  lane's egress guard does (`node/tools.lane_asks_first`). A publish the commit gate caught says
  who it acts as (below), so it reads exactly that: as a surface, the surface's switch and its
  lane's; as the lane's own account, read from the request, the lane's switch only (another
  surface's switch does not hold the login's own posts). An account the gate only assumed (the
  request names none, only Meta's `av`, or a member urn it cannot compare) still reads every
  surface's switch: it may be a Page's post through a field the gate does not list. A flow running
  on its own reads its routine's and its lane's switches when it starts, never a surface's it
  turns out to post as (egress-guard.md 8.10). A draft reads only its lane's switch, and a run an agent queued reads them
  again right before it starts.
- **What it does not reach.** A routine's switch covers that flow id only: the same steps saved under
  a new id, or done by hand on the lane, read the account's switch instead (a new routine an agent
  saves next to one a person set to ask first starts on, below). To hold an account, turn the
  account's switch on.
- **Clicks.** `browser_act` is a draft call: what a click does is not known before it. While an
  agent drives a lane, the commit gate (below) holds each publish its clicks set off before it
  leaves and authorizes it as a commit on that lane, with what it would publish as its content. So
  the switch reads as for any commit there: with the account's switch on (the lane's, or a
  surface's that posts through it) the publish waits for a person's approval of exactly what it
  sends, or a standing approval bound to that content; with it off, it runs on its own, traced and
  counted as one commit. An act whose gate route cannot be bound (a lane with no route dispatcher, or a
  browser that refuses it) is refused before it runs, so nothing is left to the egress guard alone.
  Only an act without a route (a read, a wait) relies on the guard's own refusal of a commit rule on an
  account whose switch is on (rule `ask_first`, one card in Needs you a lane an hour), and a publish
  that left after the action ended is recorded and holds the lane. **The limit:** this covers the hosts the effect
  rules name (`EFFECT_RULES`). A publish to a host no rule names still goes through. A person in
  control of the lane is never held.
- **Versions.** The switch follows the flow id, never a version. An agent's edit (a new version)
  keeps whatever a person set: on stays on. Deleting a flow keeps its switch, so a flow saved again
  under the id still asks.
- **A copy under a new id.** A new routine an agent saves (`flow_save`, `flow_record_stop`, or
  `POST /api/flows` with a token, the owner token included) on a lane that has a routine a person set
  to ask first starts with its switch on, and its setting says why (`why`, and `ask_first_why` on the
  flow). `flow_save(ask_first=false)` is refused there. A person turns it off like any other; a save
  by a person (the CLI, signed in to the panel) is not affected. It does not cover an agent editing
  a routine that is already saved: an edit keeps that routine's own switch.
- **The browser worker.** An account's switch is also how its worker publishes. "Asks you first" holds
  every publish it makes for a person, or a standing approval bound to its exact content. "Runs on its
  own" is the only thing that lets words its `write` job wrote go without a person. An account with no
  row has no choice made, so those words still ask (`AskFirst.chosen_off`, `src/openajar/pilot/README.md`).
- **Who changes it.** On (stricter): anyone who reaches the lane, an agent included
  (`flow_save(definition, ask_first=true)`, `ask_first_on(flow=... | lane=...)`). Off: a person only:
  the owner (`openajar flows ask-first <id> off`, `--account <lane>` for an account, refused inside a
  lane or a brain task; or signed in to the panel, `POST /api/ask-first` or
  `PUT /api/lanes/{ws}/{name}/worker/ask-first`), a member signed in whose role may set standing approvals in that
  workspace (admin), or a controller relaying a person (the signed `ask_first` message,
  docs/PROTOCOL.md). An agent's off is refused (`denied`), also through `flow_save`, which then saves
  nothing. A bearer token on the HTTP API, the owner token included, only turns it on.
- **Standing approvals** still cover a commit that asks first: a person pre-approved that exact
  content (a week of posts, one flow version). They matter less now that commits run on their own.
- **The overlays stay.** The lethal trifecta and the untrusted-content downgrade run after the table,
  so an auto routine that follows a stranger's text still waits, and says why.

## Standing approvals

"20 posts to biz-a lanes until Friday" is a standing approval: effects (from
`standing_effects`: commit, secret, exec, service, brain, install_overlay; never pay,
irreversible or install_base), workspace patterns (`biz-a`, `biz-*`) or lane patterns, an
end time, an optional total `count`, an optional `per_day` cap, and optionally one grant
(and those narrowed from it). Each covered call uses one unit atomically, so two calls
racing for the last unit cannot both win.

It is bound to what the owner saw, so an injected page cannot publish arbitrary content
under it:

- `targets` name what it covers: `flow:<id>@<version>` (one saved version of a flow, run
  from any tool or schedule) or `tool:<name>`. A standing approval with targets covers
  only calls that match one.
- For a content-carrying effect (`content_effects`, default `commit`) it must name a
  target, and the call must carry content the owner approved: `contents` is a list of
  content digests (sha256 over the canonical JSON of the call's content; for a flow run:
  flow, version, lane and parameters), for example a week plan. A named flow version run
  with no parameters needs no list: the version is the template.
- A standing approval without targets is stored (the panel's older route makes them) but
  never covers a content-carrying effect. For the others (secret, exec, service, brain,
  install_overlay) it works as before, limited by `contents` when it lists any.

Created by the owner: `openajar approvals standing add --target ... --content ...` or
`POST /api/approvals/standing` with `targets` and `contents`. Both take
`--from-approval` / `from_approvals`: approvals the owner saw, whose flow version (or
tool), content digest and lane the standing approval then covers ("this post, and the
same again"). Both refuse an unbound standing approval for commit and say what to name.

### From a controller

A controller's panel can set one too ("20 posts on aster/instagram until Friday"). It arrives
signed over the channel (`standing`, docs/PROTOCOL.md v1 clarifications) and is stored in the
same table with its `issuer` (`org:<org id>`), so a matching call uses one unit exactly as above.
The node checks it with the same rules and adds its own (openajar.node.channel.standing):

| Refusal | Why |
|---|---|
| `effect_not_allowed` | anything but `read`, `draft` or `commit` (never pay, irreversible or a special, which binds no target), or an effect not in this node's `standing_effects` |
| `controller_ceiling` | an effect above `controller_max_effect` |
| `too_long` / `expired` | it would last more than `controller_standing_max_days` (node.toml, 30, at most 90), or has already ended |
| `over_limit` | more uses than `controller_standing_max_count`, or more unused uses in its workspace than `controller_standing_max_active` |
| `outside_workspace` | a lane pattern outside its one named workspace (or `*`) |
| `unbound` | commit with no targets, or a `tool:` target with no content digests |
| `revoked` | the id was revoked on this node: an id never comes back |
| `conflict` | the id is this node's own, or another issuer's |
| `frozen` | a node or mesh freeze is in force, or a workspace or lane freeze over what it covers |
| `invalid` | anything malformed (id, count, dates, targets, digests, patterns) |

It covers a call only while the node trusts its issuer (a key that has not retired), while
the controller ceiling still allows its effect, and within `controller_standing_confirm_hours`
of its controller last confirming it (docs/PROTOCOL.md), all checked at each use. Joining
another org revokes every one the old org sent. `openajar approvals
standing list` shows `from org:<id>` for it; `openajar approvals standing revoke` ends it, and
a freeze revokes it like the node's own. A refused update of one the node holds withdraws the
version held (it stops covering calls until a valid one arrives). Each use and each end goes back to the controller as a
`standing` event, so its count is the node's.

## Publishes a click sets off (the commit gate)

`browser_act` is authorized as `draft`: what a click does is not known before it. From the
agent's first act until control leaves it, the lane's egress guard holds every request that
matches a commit rule, or that it cannot tell from one (an unknown GraphQL operation, a non-GET no
rule reads: `<site>.unknown`), before it leaves and hands it to the commit gate
(`openajar.node.commitgate`, docs/design/egress-guard.md section 8), which authorizes it as a
`commit` with the content it would publish:
`{"publish": <effect rule>, "lane": <lane>, "record": <the request, normalized>}` (the text, title,
link, target, media by hash and size, visibility, the account the browser is signed into, and every
other field it sends, whole; nonces, tokens, timestamps and client ids dropped). What cannot be
bound whole (media the node never saw uploaded, a body too large or unreadable) is refused
(`publish_unbound`). Everything above applies to it as to any commit:

- "Ask me first" is off for the lane (the shipped default, above): the table says `auto` (runs on
  its own), so the publish leaves at once, traced and counted as one commit.
- It is on (the lane's switch, or that of a surface that posts through the lane): the table says
  `approval`, so a person sees a `publish` card built from that request (vault
  values scrubbed from it; the digest binds the raw content) and the tool returns
  `approval_pending`. Nothing was sent (when an earlier publish of the same click left under its
  approval, the message says so and only the later one waits). After approval the agent calls again with the same
  arguments; the click runs again and the request leaves only if it normalizes to the approved
  content (the one-off approval is consumed once).
- The same call approved for other content than it now sends: the approval is retired and the call
  refused (`publish_changed`); the next call asks about what it sends now.
- Approved, but asked for more than `publish_approval_max_age_s` (policy.toml, default 7200) ago:
  expired and refused (`approval_stale`, AUD-20); the next call asks again.
- A standing approval covers it only when bound: target `tool:browser_act`, the lane, and the
  content digest in `contents` (`--from-approval <id>` takes all three from a publish approval).
- A grant below `commit` never gets that far: the guard refuses the publish (`egress_blocked`, rule
  `commit`). A person in control of the lane is never gated. Meters count it as one commit when it
  is authorized, and a commit limit refuses it before it leaves.

**Who it posts as** (AUD-01 on the gate, `openajar.node.acting`, docs/design/egress-guard.md 8.10).
On a site where a login can post as another identity (Facebook, LinkedIn; Instagram and Medium
when the body names a business account or a publication), the gate reads who the request acts as
and resolves it. The browser's signed-in account is checked against the lane's pin first: another
account is refused (`wrong_account`), whatever was approved.

| The request acts as | Authorized on | What applies |
|---|---|---|
| the lane's own account (its pinned id, or the signed-in one when none is pinned) | the lane | as above, unchanged |
| a surface through this lane (`biz-a/facebook-page`, a Page the login manages; or an `account` surface whose id is not the lane's, for the login's own posts) | the surface: `lane` is its id | the grant must cover the surface (its workspace may differ from the lane's: a `personal` grant posting as biz-a's Page is refused `out_of_scope` unless it covers `biz-a` too); the surface's Ask me first switch and its lane's; the lane's pace and limits (a commit counts on the surface and on the lane it posts through); the approval, its card and its summary name it ("Posts as biz-a (Page)"); a standing approval covers it only when given for that surface (its lane or workspace) and that content |
| an identity openajar does not know: an id no surface names, a Page of another lane, fields that disagree, a value that is not shaped like an id (never repeated on the card), or none the gate can read on Facebook | the lane | always `approval`, whatever the table or the switches say, and never a standing approval; the card says it would post as an identity openajar does not know and names the raw id. A caller that cannot ask (a grant or principal of kind `flow`: a schedule, a trigger, a flow run) is refused (`identity_unrecognized`) |

The content binds who it acts as (`record.acts_as`, and `lane` is the surface's id), so an approval
or a standing approval given for one identity is other content for another. On Reddit, and any site
whose login only posts as itself, who it acts as is not read; the account pin above applies to
every site, so a publish there with another account signed in is refused too, where it used to
reach a person with a warning. A lane pinned by handle only cannot be compared by id: its card
still warns and asks twice.

An approval on a surface names the surface in `lane`, so it belongs to the surface's workspace
and its deciders; its card's `through` is the login, which is where the panel shows it.

## The lethal trifecta

Each grant family (a root grant and every grant narrowed from it) has one taint ledger of
data-origin labels, so a reconnect, a second MCP session or a child grant never starts
clean, and a child inherits what its parent read:

- `untrusted`: web pages, terminal and program output, model output;
- `private`: private lane data, a secret having been used;
- `owner`: input from the owner.

Handlers set labels with `ctx.trace(kind)` (kinds in `taint_kinds`: `web_content`,
`untrusted_content`, `terminal_output`, `program_output`, `model_output`,
`private_data`, `secret_used`, `owner_input`), with `ctx.trace(..., taint=[...])`, with
`ctx.taint(...)`, or automatically after a tool whose tags are in `taint_tags`. Page text
is untrusted: the browser tools label it themselves, and the default `taint_tags` is
`{"browser" = ["untrusted"]}`. A node whose lanes show the owner's private data (inboxes,
DMs) can set `{"browser" = ["untrusted", "private"]}`; then every browser sink outside the
egress allowlist asks first.

When a family holds both `untrusted` and `private`, a call that sends data out to a
destination outside the egress allowlist needs a one-off approval against its exact
content, even when the table says auto and even under a standing approval. A call sends
data out when its effect is in `sink_effects` (draft, commit, pay, irreversible, exec,
service, brain) or it states `facts["sink"]`, as `browser_navigate` does: a URL carries
its query string to the host, so a page cannot make the agent leak the inbox into
`https://evil.example/?q=...`. The egress allowlist starts from the root grant (its
`egress` list; with none, its `domains`; with neither, every destination is outside) and
every narrowed grant can only shrink it. A call with no domain is treated as outside.
Labels expire `session_ttl_s` (12 h) after they were last added. The owner in person (a
signed-in panel session) neither adds labels nor waits on them.

## After untrusted content

The trifecta needs private data too. This rule does not: once a session has read untrusted
content (a page from a site, an email, a webhook body, connector output a stranger wrote, a memory
note marked untrusted), what it does next in the same grant family cannot be steered by that
content without a person seeing it.

**The downgrade.** An effect in `[taint] downgrade` needs a person once the call's context has read
untrusted content, even where the table says auto. A login is not one of them (see "Logins"):

| Effect | Clean context | After untrusted content |
|---|---|---|
| read | auto | auto: reads stay free (a navigation is still a trifecta sink) |
| draft | auto (owner-trust memory waits) | the same |
| commit | auto unless "Ask me first" is on; then approval, or a bound standing approval | approval, or a bound standing approval (a commit standing approval is already bound) |
| pay, irreversible | approval, never standing | the same, and the card says what it follows |
| secret | auto | auto: a login never asks, and the list cannot hold it (see "Logins") |
| exec, service, install_overlay | auto when the table says so; any standing approval | approval, or a bound standing approval |
| install_base | approval | the same |
| brain | as routed | as routed (not in the default list; the trifecta and metering cover it) |

**Bound standing approvals.** After untrusted content a standing approval covers a call only when it
is bound to what the owner saw: the call's exact content is in its `contents` (a digest of the post,
of a flow run's flow, version, lane and parameters, or of a connector run's operation, contract and
parameters), or it names the flow version or connector operation the call runs and the run has no
parameters (the version is the template the owner saw). A free-form target never covers it: a
`tool:<name>` without `contents`, or no target at all. So an owner who planned a week of posts (a
flow version and its parameters per day, or exact captions) and gave a standing approval for them
keeps running unattended while the agent reads its own feed; what a page asks it to post waits.

**The approval card.** It says the call follows untrusted text, and from where: kind and host
(`a web page at evil.example`, `a webhook body`, `a memory note marked untrusted`), never the text
itself; the preview shows only the content being approved, as for every approval. The approval
carries `follows` ([{kind, host}], newest first) and `after_untrusted` (the same in words); the push
title reads `<lane>: <effect> after reading <where>` and the body starts with "This follows
untrusted text from ...". When the owner denies such a call, the whole family is quiet for
`[taint] denied_cooldown_s` (10 minutes): another call that follows untrusted content and would ask
again is refused with `taint_cooldown`, on top of the exact-content cooldown, so an injected page
cannot try variants until one slips through.

**What the agent is told.** The tool result of a downgraded call is the usual
`{status: "approval_pending", ...}` plus `why`: it follows untrusted text this grant read (from
where), so a person must see it, and the agent must not rephrase or split the call to get around
it. The MCP instructions, `howto` and the prompts say the same; `grant_info` shows `taint`
(below), and every authorize trace of such a call carries `after_untrusted` and `taint_context`.

**Taint lifetime.**

- *What carries it*: the grant family's taint key (`family:<root>`), as for the trifecta, so a
  reconnect or a second MCP session never starts clean. Each context keeps its untrusted origins
  (kind, host, first and last seen) in `taint_origins`.
- *Contexts*: the family, or a **turn**: `PolicyService.mint_turn_token(token, seed=..., origin=...)`
  narrows a grant and starts a clean context for it and every grant narrowed from it. Only node
  services call it (the agents service, once per turn); no tool or API does, so a holder cannot clear
  its own context. A turn starts with its `seed`: the taint of what woke it and of the CLI session it
  continues. What a turn reads also taints the family it runs in; the family's earlier reads never
  reach a new turn.
- *What starts a clean context*: a new turn token, a fresh grant (`issue`), or the owner in person
  (`openajar grants taint <grant> --clear` on the node), which forgets the family's live labels and
  every origin of the family and its turns. The `ever` view stays, so memory still knows what the
  family read (see "Workspace memory").
- *A narrowed grant never sheds it*: an attenuated grant, an offline block or a brain's child token
  is in its parent's family (and turn). A child of the owner token is a fresh root grant, so it is
  given the owner token family's labels and origins when it is made; it never starts clean.
- *No decay*: origins do not expire on a clock. A model keeps what it read for as long as its
  context lives, and nothing on the node can see when the text leaves it, so a timer would only
  reopen the hole for a patient injection ("wait a day, then post X"). They end with their context:
  the root grant or turn grant expires or is revoked (then `purge` drops them), or the owner clears
  them. The trifecta keeps its 12-hour labels: it also gates drafts and every navigation, the calls
  agents make all day, and those labels are family wide, so a turn does not clear them.
- *Seen*: `grant_info` returns `taint` = `{context: family | turn, labels, untrusted, since, follows,
  after_untrusted, waits_for_a_person, clean_context}`, and `explain` says in a sentence what waits;
  `openajar grants taint <grant>` prints the same.

Browser tools name the page's host; memory reads say `memory_note`, connector output
`connector_output`, an event's taint its source (`webhook_event`, `mail_event`).

```toml
# $OPENAJAR_HOME/policy.toml; the default is below. Unknown effects (or read) refuse to load, and
# secret is dropped with a warning in the log (see "Logins").
[taint]
downgrade = ["commit", "pay", "irreversible", "exec", "service", "install_overlay", "install_base"]
denied_cooldown_s = 600
```

## Git (docs/GIT.md)

A lane's or a brain task's git reaches only remotes the owner declared, through the node's git
gateway, which authorizes each request under the grant behind the command (tools `git_fetch` and
`git_push` in the action log). No rule of the table is special to git: a push is an ordinary
commit, so everything above applies to it.

| Git | Effect | Lane | So |
|---|---|---|---|
| clone, fetch, pull | read | the command's lane (a brain: the declaration's lane if the task has it, else none) | runs on its own |
| push a new branch or tag, a fast-forward | commit | the same | runs on its own unless Ask me first is on for the lane; a commit on no lane (a brain without the declaration's lane) asks; after untrusted content it waits for a person |
| force-push, delete a branch or tag, move a tag, push to a protected branch | irreversible | the same | always a person, never a standing approval |

The approval's content is `{"git_push": "<host>/<owner>/<name>", "refs": [{ref, kind, old, new,
force, protected, commits, log}]}`, so a yes covers that push of those commits only; pushed again
unchanged, it goes, and anything else asks again. The grant's ceiling must reach the effect: a
grant up to commit cannot force-push at all.

## Logins

A password in the vault is the owner's permission to use it (docs/PRINCIPLES.md, 3). `login` and
`vault_fill`, the only calls with the effect `secret`, run on their own: no first use, no downgrade
after reading a page, and `secret` is not a trifecta sink. Reading a login page is how every login
starts, and what is typed where does not depend on what the agent read: the vault checks the entry's
lane and domain, https, the frames and the field's role from the page itself, at the moment of use
(openajar.vault.policy). The call still needs the grant's `secret` special for that lane, inside its
scope: a grant without it is refused, which is no confirmation. A captcha, a code the entry has no
source for, a device approval and a checkpoint end the login with `needs` for a person. A stored TOTP
seed is the vault.

**The submit.** A login's POST is on a rule host and no rule reads it, so the commit gate would hold it
as `<site>.unknown`. While the vault fills a field the lane's guard holds a short login window for the
entry's domain, and the POST goes. Named commits and writes do not (docs/design/egress-guard.md, 8.11).

**A worker's publishes.** One carve-out from the table: on a pilot's child grant
(`PolicyService.must_ask`, src/openajar/pilot/README.md) a publish no rule reads, and every publish
after the `write` job wrote its words, wait for a person whatever the table and `[taint] downgrade`
say, and no standing approval covers them. The login window above still goes first.

**`policy.toml`.** `[taint] downgrade` can only be written whole, so a file that lists `secret` is
most likely the old default copied in, and it is not read as a choice: `secret` is dropped, the node
starts, and the log warns. A node that wants a person at every login says so in `[routing]`, where the
line can only mean that: `{effect = "secret", then = "approval"}` above the default rows (a replaced
table is read as written, so check one written before this rule for a `secret` row).

## Narrowed grants

A narrowed grant (`attenuate`, an offline block, a brain's child token) can only take
permissions away, field by field: lanes and workspaces, domains, egress (intersected with
the root's), the ceiling, specials and their parameters (secret and exec lanes, install
sources, which only the root grant or the node allowlist can make auto, service ports and
TTL, brain provider, tier, credential owner and quotas), pins, admin, budgets, not-before
and expiry, depth and fan-out (the smallest fan-out in the chain). Its subject is its
parent's, or a sub-agent of it: the parent's id plus `/label` (`claude-code/codex-worker`)
with kind agent, flow or brain. It can never be a human, the panel or another identity,
never change the owner, and never move a thread pin.

## Domains and URLs

Domains and URLs are read with one WHATWG-style parser, in the policy and in the node's
`browser_navigate` alike, so the host that is authorized is the host the browser loads.
Refused as ambiguous: backslashes, control characters or spaces, a scheme not followed by
exactly `//`, more than one `@`, percent-encoded or forbidden bytes in the host, numeric
hosts a browser reads as IPv4 (`0x7f.1`, `2130706433`), and userinfo in a domain without
a scheme (`instagram.com:@evil.example`). Unicode hosts use UTS 46 non-transitional
processing, as browsers do: `faß.de` is `xn--fa-hia.de`, never `fass.de`.

## Members and approvers

The owner can give other people a part of the node: a VA who approves biz-c posts, a
teammate who runs flows, a client who watches. Each is a **member**, a person, never an
agent. Routing (below and in the notifier) only decides
who is asked; it never decides who may decide. That is the member's grant, checked here.

**A member grant** is a grant whose subject is `kind: human` (id `m_...`, owner itself),
one per scope (a workspace, or `*` for every workspace), made from fixed roles:

| Role | Ceiling | `approve` | `control` |
|---|---|---|---|
| viewer | read | none | watch only, with `watch` |
| auditor | read | none | none |
| approver | read | `effects`, default commit (and pay when `max_usd` is set), on `lanes` | watch only, with `watch` |
| operator | commit | none | take over, each session at most `max_session_s` (1 h) |
| admin | irreversible | every effect but read, pay up to `max_usd` | take over |

Several roles in one scope combine. A workspace admin is never an admin grant (no grants,
no vault, no keys): that stays the owner's. A membership's end (`--ttl 90d`) is the grant's
expiry. A role change makes a new grant for that scope and revokes the old one; removing a
member revokes every grant they hold, and the grants of their agents (`sub.owner` is the
member), at the next call. A removed member id never gets a grant again.

**Only a person's grant carries `approve` and `control`.** Issuing or narrowing an agent
grant with either is refused, and a token whose chain adds one to an agent's grant is
`invalid_token`. A member grant is never a bearer token: MCP and the panel's bearer path
refuse it with `invalid_token` (a member signs in to the panel, which keeps the grant id),
and it is never narrowed or handed to an agent. The one exception is a controller's member
call: a person acting in a controller's panel reaches this node through the channel's `invoke`
with a per-call human grant from the controller's org, accepted there only and only while it has
at most 6 minutes left (TOKENS.md, "A member's call from a controller"). It is clamped by the
controller ceiling like any controller grant, its principal is the member, and it is not in
person: what needs a person is routed exactly like an agent's call and asks for an approval
recorded under the member, and the lane gates (a take over, Stop agent, a freeze, claims, the
grant's egress list) hold it like an agent. The controller decides it under its own rule (a
signed-in session may approve what it started); the node applies the ceiling to that decision
(docs/PROTOCOL.md).

**What this trusts.** The controller relays a decision as the owner: it names no grant, so the
node records `controller:<who>` and the family rule (`self_approval`) has nothing to check. A
member can therefore approve their own call from the cloud panel, and the node cannot tell. The
cloud means this (the hosted edition's contract: "a signed-in session may approve what it started from the
panel"), so the node trusts the controller's approval rules for its members, within the owner's
controller ceiling: above `controller_max_effect` or outside `controller_specials`, only the
owner on the node decides. An owner who wants a second person on a member's actions keeps the
ceiling below those effects.

**Deciding.** An approval is approved when the decider is the owner, an admin grant (the
owner's own delegate), or a member grant where all of this holds:

1. the grant is live (not expired, not revoked, the member not removed);
2. its lane scope covers the approval's lane, its `approve.effects` holds the effect and
   its `approve.lanes` (when set) the lane; a question needs `approve`, or `control` that
   is not watch only, on the workspace it was asked in;
3. for `pay`, the amount the node knows (`facts["amount_usd"]`, else the stated cost) is
   at most `approve.max_usd`; no `max_usd`, or no known amount, decides no pay;
4. the decider is not the requesting grant family (`self_approval`, as above);
5. four-eyes: where `policy.toml` turns it on for the workspace and effect, the person
   behind the requesting agent (its `sub.owner`) does not approve it (`four_eyes`).

Otherwise it is refused with `not_approver` (or `self_approval`, `four_eyes`, `revoked`,
`expired`). Denying needs only that the member sees the approval: denying is always safe.
An agent's grant never decides (`not_approver`).

```toml
# $OPENAJAR_HOME/policy.toml; default: no four-eyes anywhere
[approvals]
four_eyes = [{ workspace = "biz-*", effects = ["pay"] }]
```

**Seeing.** The owner and admin grants see every approval; a member sees the approvals on
the lanes their grants cover (and questions asked in their workspace); an agent sees its
own family's. A member in person in the panel co-signs their own action (no approval is
asked) only when their `approve` covers it and no four-eyes rule applies; otherwise their
action is routed exactly like an agent's.

Every approval records facts the node computed: `workspace`, `requester_owner`, `target`
(`flow:<id>@<version>` or `tool:<name>`), `amount_usd` (pay), `not_after` (when the run goes
stale), and on decision `decided_by` (the member), `decided_via` (passkey, code,
owner_token, session, push_link, chat, telegram), `decided_method` and `decided_level`
(docs/CONFIRMATIONS.md).

## Approvals on the phone

A push never carries a link that approves. When `public_base_url` is set, an actionable
push has two buttons: **Review**, a signed link that opens that approval in the panel,
where the owner approves it signed in, and **Deny**, a one-tap signed POST (denying is
always safe). Both expire after 30 minutes; the approval itself waits in the panel until
its own expiry. Without a public URL the push only links to the panel.

An ntfy topic on a public server is readable by anyone who knows its name, so an enabled
ntfy channel needs a long random topic: at least 32 characters, 16 of them different,
letters and digits. `openajar policy notify ntfy` without a topic generates one and prints
it. Better push paths, in order:

1. **Your own ntfy on the tailnet** (`server = "https://ntfy.<your-tailnet>"`), with
   access control (`auth-default-access: deny-all`) and an access token for the node.
2. **The signed webhook** to a service you own (Home Assistant, a Pushover or Signal
   bridge): every POST carries `X-Agentos-Signature: sha256=HMAC(secret, timestamp.body)`.
3. Public ntfy.sh with a generated topic, plus an access token on a reserved topic if you
   have one.

**Telegram** is the one phone path whose buttons can approve, and only what a chat prompt
can (`commit` at the `chat` level, nothing after untrusted text, never `pay`, `irreversible`
or a question): the node's own bot, paired with the owner's account from the CLI
(`openajar policy notify telegram set-token`, then `pair`). Everything else gets Deny and,
with a public URL, Review. The node only dials out (long polling). See
docs/CONFIRMATIONS.md, Telegram.

## How strong a yes must be

This page says whether a call asks. Once it asks, `docs/CONFIRMATIONS.md` says how strong the yes must be and
where it can be given. An approval's `requires` is worked out once, when it is made, and it keeps it:

| The call | `requires` |
|---|---|
| commit | `[confirmation] commit.level`, default `chat` |
| pay, irreversible | their rows, default `present`, never below `signed_in` |
| anything else that asks, a question | `signed_in` |
| follows untrusted text, or the trifecta stopped it | at least `after_untrusted.level`, default `signed_in`, whatever `[taint] downgrade` lists |
| an agent's trigger proposal | at least `signed_in` |

`commit.when` is `[routing] ask_first_default`, not a second switch: a file that sets both differently refuses
to load, and the switches of Ask me first go on working above it. A value under a floor refuses to load too, and
a change under one is refused (`floor`). `requires` never decides whether a call asks, and it binds no standing
approval: one is a person's earlier yes, and it covers what it covers.

```toml
# $OPENAJAR_HOME/policy.toml; the default is below. `openajar confirmation set` changes it on a running node.
[confirmation]
commit       = { when = "auto", level = "chat" }
pay          = { when = "ask",  level = "present" }
irreversible = { when = "ask",  level = "present" }
after_untrusted = { level = "signed_in" }
methods_off  = []
controller_max_level = "present"
```

An approve below `requires` is refused (`proof_too_weak`), and a way to confirm that is planned or off is refused
(`method_unavailable`). The windows are today's step-up: `signed_in` for a member is a passkey check from the last
15 minutes, whatever the approval requires, and `present` is a fresh check for that approval, used once. An owner's
session is `signed_in`, and so is the CLI, so what requires `present` needs a passkey, or the row lowered to
`signed_in` for new approvals. A bearer token proves no person: it records no level and reaches up to `signed_in`.

A publish the commit gate caught is the one thing after untrusted content that can take `chat`, by the owner's
decision (`[confirmation] publish_in_chat`, docs/CONFIRMATIONS.md): its prompt shows what its card shows.

An agent can offer `approval_confirm(approval_id)`: the person answers a prompt the node wrote, in their own chat
client. The agent never answers it and has no argument that could. A chat yes is `chat`: never the agent, and never
"in person", so what only a person turns on does not read it as one. `self_approval` and four-eyes are unchanged.

## Changing the table

```toml
# $OPENAJAR_HOME/policy.toml
clock_skew_s = 30

[routing]
standing_effects = ["commit"]
rules = [
  { effect = "read", then = "auto" },
  { effect = "draft", then = "approval", note = "review every draft" },
  { effect = "exec", then = "deny", note = "no shells on this node" },
]
```

Rules name an effect (or `*`), optional `when` facts and `then`. Unknown facts or
effects are rejected when the file loads.

## Denial reasons

Every refusal is `denied: <reason>: <detail>` with a stable reason: `unauthenticated`,
`invalid_token`, `unknown_issuer`, `unknown_key`, `retired_key`, `audience`, `unregistered`,
`expired`, `not_yet_valid`, `revoked`, `out_of_scope`,
`domain`, `ceiling`, `special`, `provider`, `tier`, `budget`, `depth`, `fanout`, `pins`,
`unknown_effect`, `routing`, `approval_denied`, `too_many_pending`, `admin_required`,
`self_approval`, `approve_in_panel`, `not_approver`, `four_eyes`, `step_up_required` (the
panel: decide again after a fresh passkey check), `floor` and `owner_only` (a change to the
confirmation setting), `taint_cooldown` (a person recently denied a call
that followed untrusted text in this family; the message says when to try again), `limit_reached` (a meter's daily or hourly
limit: the message names the limit, its value and when it resets), `identity_unrecognized` (a
publish would post as an identity openajar does not know, and the caller cannot ask a person: a
schedule or a flow), `wrong_account` (the commit gate: the browser is signed in to another account
than the lane's pinned one), `lane_paced` (the lane's posting
pace; its own error code, the message says when the lane may next post) (and from a controller's grants
`foreign_admin`, `degraded` and `controller_ceiling`: above what the node's owner lets a
controller do, `openajar.policy.ceiling`). The token reasons are specified in TOKENS.md, "Verifying".

## Workspace memory

Memory (docs/design/workspace-memory.md) adds a special, two facts and two ledger views. The
code is in `memory_rules.py`, `labels.py` and `memory_port.py`.

**The `memory` special.** `specials.memory = {workspaces, write, types, private,
audience_owner}`. It has no effect of its own: memory tools authorize `read`, `draft` or
`irreversible` like any tool, and the special decides which workspaces and entries they reach.

- `workspaces`: names or patterns (`biz-*`); unset means every workspace the lane scope
  reaches. A workspace is reached when one of the block's lane patterns has a workspace part
  matching it, so a grant on `biz-a/instagram` with memory for biz-a reads all of biz-a's
  memory. A workspace outside the lane scope is refused when the grant is issued or narrowed.
- `write` (read tools only when off), `types` (the entry types it may write), `private`
  (may read entries labelled private) and `audience_owner` (may read entries meant for the
  owner only; it takes effect only in an admin grant). Flags default to off.
- Narrowing: workspaces and types only shrink and flags only turn off; every block must allow
  the call on its own. The owner's grant has it all. Existing grants have no memory until they
  are issued again. A brain's child token gets memory for its task's workspace with write, the
  parent's `private`, and never `audience_owner` (`memory_rules.child_cap`).
- Out of scope is `out_of_scope`; a missing special, write, type or flag is `special`. Tools
  tagged `memory` are hidden from grants without the special, and those that write from grants
  whose memory is read-only. `grant_info` shows the special under `memory`.

**Owner-trust writes wait.** A decision, or a supersede of an owner-trust entry, by anyone but
the owner in person authorizes `draft` with `facts={"memory_owner_trust": True}` and the
canonical entry as `content`. That is always a one-off approval bound to that content: a
standing approval never covers it, and a replaced routing table that says auto is treated as
approval. Self-approval, the denied cooldown and the pending cap apply as for every approval.
Forgetting is `irreversible`, so it always waits too.

**Internal destinations.** Memory writes pass `facts={"internal": True}` and no domain: the data
stays in the workspace's memory on this node and its labels travel with it (below). For the
lethal-trifecta overlay an internal call is inside the egress allowlist. A call that states
`internal` and also names a domain is not internal.

**Labels that last.** Every label added to a family's taint ledger is also kept in the `ever`
view (`taint_ever`): what the family has seen since its root grant was issued, dropped when the
root expires or is revoked. Routing and the trifecta keep using the 12-hour labels; memory uses
`ever` to decide the trust of what a family writes. Reading memory adds the stored labels of what
was read to the reader's family.

**Context receipts.** `memory_context` records the decisions digest a family loaded for a
workspace. An `authorize` trace for a `commit` on a workspace lane then carries
`decisions_loaded: current | stale | never` (written even when the commit runs on its own). It
decides nothing.

**Evidence survives retention.** Traces cited by owner-trust and decision entries are pinned
(`trace_pins`); retention skips pinned traces. At most 10,000 pins per node: past that the
oldest pins are dropped first.
