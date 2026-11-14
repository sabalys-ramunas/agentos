# The agenda

Every workspace has one agenda: a timeline of planned actions, what may run, what ran, and the
proof it left. An action is anything agentos can already run. A post is one kind. So are these:

- Send the November invoice reminders on the 1st at 09:00 (a saved flow on the billing lane).
- Renew the `biz-a.com` domain in the week before it lapses (a flow on the registrar lane, in a
  window: not before the 20th, not after the 27th).
- Submit the quarterly VAT form inside office hours on the 25th (a flow that fills and submits a
  form, `irreversible`, so a person always signs it).
- Export last month's sales report and file it (a read flow: nothing is published, the run's
  result is the proof).
- Post the launch photo on Instagram on Tuesday at 18:00 (a connector operation on a surface).
- On the 30th, wake the workspace's agent to draft next month's newsletter (a trigger whose
  target is an agent or a brain, with the entry's notes as what to do).

Recurring work stays a schedule (`agentos schedules`): the agenda shows its upcoming fires, but it
never stores a second copy of them.

## What is on the timeline

`agenda_list` (MCP), `GET /api/agenda` (panel), `agentos agenda list` (CLI) merge four things for
a range of days, filterable by lane, kind (`flow`, `connector`, `route`, `keepalive`) and state:

| Item | Where it comes from | State |
|---|---|---|
| entry | a one-off planned action (this page) | its lifecycle state, below |
| projected | the next fires of each enabled cron schedule in the range, computed from its cron and time zone (03:30 in a spring gap fires at 04:30; the repeated autumn hour fires once); with `paced` when the lane's pace will move it | `projected` |
| run | a run that already happened in the range (not an entry's own run, which shows as the entry), with its proof | `done`, `failed`, `running`, `cancelled` |
| managed | work another service plans on a lane on its own: keepalive's visits (below) | `planned` |

The owner sees every workspace; an agent sees the entries and runs on the lanes its grant covers.

### Other planned work

Keepalive plans timed work on accounts by itself, and the timeline shows what it plans, read only
(`type: managed`, `state: planned`, with `source` and `managed_by`). It is not stored by the agenda
or planned a second time: the service answers `planned(start, end)` from its own planner.
An item has no approval and cannot be cancelled on the agenda; `manage` names where it is managed.
`managed_by` in the timeline says, per service, what it can show and what it cannot. The panel shows
these rows muted, under "Show other planned work"; `managed=0` (API), `managed=false` (MCP) and
`--no-managed` (CLI) leave them out.

| Source | What it shows | What it cannot show |
|---|---|---|
| `keepalive` | each lane's next visit, from the plan its rounds decide on, at an approximate time (`approx`, `~` in the CLI and the panel), with `waits` when a gate holds it for now (a human has control, the lane is in use) | any visit after the next: it is drawn from how this one goes and from the lane's use since. The time moves later whenever the lane is used, and a due visit waits for a free warm slot, memory and a gap after the node's last visit. A lane it does not visit (the node switch off, the lane opted out, no site, quarantined, frozen, signed out or challenged) has no item; `skipped` says why. Managed with `agentos lane keepalive <lane>` and the Keepalive card on the lane's page |

The CLI asks agentosd for it (`op: planned` on the ops socket), since agentosd owns the lanes the
plan reads; with agentosd down, the list says so and shows the rest.

## An entry

```
agenda_draft(title="Send the November invoice reminders",
             action={"flow": "biz-a.billing.remind", "params": {"month": "2026-11"}},
             when={"at": "2026-12-01T09:00:00+02:00"}, propose=true)
```

- **action**: what runs. `{"flow": id, "params", "on"}` for a saved flow (the drafted version is
  pinned, so the approval covers the steps a person saw); `{"connector": "instagram.post_photo",
  "params", "on": surface}` for a connector operation; `{"trigger": "rt_...", "on"}` for a trigger
  that wakes on timed events, whose target is a flow, a brain or an agent (it takes no params: put
  what the agent should do in the entry's notes, which travel as the event's untrusted content).
- **when**: `{"at": t}` (an exact time; it may still start up to 30 minutes later, for a node that
  was busy or restarting), `{"not_before": t, "not_after": t}` (a window, either side optional), or
  `{}` (as soon as it is approved; add `not_after` for a deadline). Times are ISO 8601 with a time
  zone, at most a year ahead.
- **lane**: where it acts, resolved when it is drafted. The drafter's grant must cover that lane and
  the action's effect, or the draft is refused (a grant for `biz-b` cannot plan anything on `biz-a`).

## Lifecycle

```
draft ──propose──> proposed ──a person approves──> scheduled ──its time──> running ──> done
  │                    │ (or nothing asks: straight to scheduled)            │        └─> failed
  │                    └─ denied, or the request expired: back to draft      └─ the pace or a hold keeps it
  │                                                                             out of its window: skipped
  └─ its window passes before it runs (node down, nobody approved): expired
any open state ──cancel──> cancelled
```

| State | Means |
|---|---|
| `draft` | being written; nothing was asked |
| `proposed` | waiting for a person: a normal approval in Needs you |
| `scheduled` | may run at its time: a person approved it, a standing approval covered it, or the routing lets it run without asking |
| `running` | its run (or its trigger's delivery) is under way |
| `done` | finished, with the proof its run left |
| `failed` | its run failed, or it could not start (the drafter's grant expired and no person stands behind it, the lane is frozen, the flow is gone); `reason` says which |
| `skipped` | the lane's pace, its waking hours or a hold kept it out of its window; `reason` quotes it |
| `expired` | its window passed before anything ran |
| `cancelled` | someone called it off, or the grant it was drafted under was revoked; a waiting approval is withdrawn and an unfinished run cancelled |

An edit of the title or notes changes only what a person reads. An edit of the action, its inputs,
its lane or its time changes what runs: a `scheduled` or `proposed` entry is asked again (back to
`proposed`; a waiting request is withdrawn first, so a late decision on it releases nothing), and a
`failed`, `skipped` or `expired` entry goes back to `draft` to be planned again. A running, done or
cancelled entry cannot be edited.

## Approval

The agenda has no approval system of its own. Proposing asks the node's policy, under the grant
the entry was drafted with, the same question its run would ask: the action's effect on its lane or
surface, with a node-written summary ("Agenda: 'Send the November invoice reminders', at
2026-12-01T07:00+00:00. Run flow biz-a.billing.remind ...") and the same approval content a flow run
binds, so the card in Needs you shows the real caption, form or amount. Everything that routes a run
routes the entry:

- the routing table and "Ask me first": a routine that runs on its own is `scheduled` at once
  (`approval.via: auto`), and its run is authorized again when it fires, exactly like a schedule's
  slot (if "Ask me first" was turned on in between, the run asks then);
- a standing approval that covers it spends one use now (`via: standing`);
- otherwise a person decides (`via: person`): in Needs you, in the panel's Agenda, or with
  `agentos agenda approve <id>`. Who may decide, four eyes, and "a token never approves what its own
  grant family asked for" are the approvals' own rules. Approving with an edit (a caption changed on
  the card) changes the entry's inputs to what the person approved.

How long the request waits. An ordinary request in Needs you closes after the node's approval time
(a day by default). A request for an agenda entry stays open until the entry's start instead: its
exact time, or its window's not-before, plus 30 minutes (the slack an exact time already has). It
never closes sooner than an ordinary request would (an entry two hours out still waits a day), and
never later than `agenda_approval_max_days` after it was proposed (node.toml, 14 by default, 1 to 90).
An entry for "as soon as approved" has no start, so its request gets the whole cap. The entry's
`approval.expires_at` says when it closes and `approval.capped` whether the cap set it; Needs you
shows the same time on the card ("expires in 12 days"). When the request closes undecided, the entry
goes back to `draft` with the reason; one the cap closed says so ("nobody decided within the 14 days
a request for an agenda entry stays open ..., propose it again closer to its date"), and proposing it
again opens a new request with a new cap. Nothing else changes: other requests keep the node's
approval time, an edit still withdraws the request, and the run is authorized again when it fires.
An entry weeks out can outlive the grant it was drafted under (an agent's grant lasts a week unless
it was issued for longer); see "Whose authority a run carries" below.

The approval binds to a hash of the action, its inputs, its lane and its timing (`approved_hash`).
When the entry is approved, the agenda consumes the approval at once, so no other call can use it.
Agents draft and propose; approving is not an agent tool.

## Firing

There is no second scheduler. The runner's scheduler ticks the agenda on its own loop, and a due
entry goes through `RunnerService.submit`, the path every schedule slot takes, with the idempotency
key `agenda:<entry>:<hash>`. A node that restarts between submitting and recording finds the same run,
so an entry never runs twice. The run carries the drafter's grant: it is checked when the run is
queued and again right before it runs. A trigger entry emits one timed event with the same key as its
dedupe key; the trigger's own controls (quiet hours, rate) and grant decide what it wakes.

### Whose authority a run carries

| The drafter's grant at fire time | A person approved the entry | The run |
|---|---|---|
| live | either way | goes under the drafter's grant, as always |
| expired | yes (`approval.via: person`) | goes under that person's authority, for this one action (below) |
| expired | no (the routing let it run, or a standing approval covered it) | does not start: `failed`, "it could not start: ... expired" |
| revoked | either way | never runs: the entry is `cancelled` (below) |

When the drafter's grant has merely expired and a person approved the entry, the run goes under that
person's approval. At approval the entry records who decided it (`approval.approver`: the owner, or a
member with the very grant they decided with), bound to the hash they approved. At fire time the
policy checks that the approver still stands behind it: the owner always does; a member only while
that same grant is live (not revoked, not expired, the member not removed) and its `approve` still
covers the effect on the lane, so a role removed or narrowed since means the entry `failed`, with the
reason. Then the policy derives a grant for this one run from the approver's authority
(`PolicyService.approved_run_grant`): an ordinary root grant whose subject is `agenda:<entry>`, a flow
owned by the approver (removing that member revokes it with their agents' grants), covering only the
entry's lane (and surface), at most its effect, valid from the window's start until it closes (plus an
hour for a run that started inside it to finish), with no delegation (depth and fan-out 0), never
wider than the drafter's grant (its sites, egress list and lane secrets carry over, nothing more), and
pinned to the run: the flow, version, lane, inputs and idempotency key. The runner refuses that grant
for any other lane, inputs or run (`run_binding`); its pins name no tool, so it opens no MCP tool; its
token is never handed out; and it is revoked once the run is over. The entry keeps it as `authority`
(`grant_id`, `approver`, `why`), and the audit log has one `agenda_run` row naming both the drafting
agent (`principal`) and the approver (`args.approved_by`), with the one-run grant as `grant_id`.

Only a person's own decision on the entry's request counts. A standing approval belongs to a person,
but it was given ahead of time for a kind of action, never for this one, so it does not stand in for
an agent whose grant lapsed; neither does a decision made under an agent's or admin token, nor the
routing letting an action run without asking. Those entries still need the drafter's grant.

An explicit stop is never bypassed:

- A revoked grant (revoked by the owner, by removing the member who owns that agent, or by a freeze's
  sweep) cancels every entry it drafted that has not run (draft, proposed, scheduled): the agenda's
  tick sweeps them, a waiting request is withdrawn from Needs you, and `reason` names the revocation.
  A revoked grant is never read as an expired one, even after its time has passed. A run already
  going under a person's approval is cancelled too.
- A freeze on the lane, its workspace or the node fails the entry (`failed`, "... is frozen ..."),
  whoever drafted or approved it.
- A held or quarantined lane, the lane's pace and the egress guard stop the run as they stop any run:
  it waits inside the window, and the entry is `skipped` with the reason when the window closes.

The lane's posting pace and waking hours (agentos.policy.pace) apply to every commit. Before it
submits one, the agenda reads the lane's pace. When the pace says not yet and the lane may post later
inside the entry's window, the entry waits for that time (`retry_at`, `reason: waiting for the lane's
pace: ...`). When the lane may next post only after the window, the entry is `skipped` with the pace's
own words. Proposing an entry whose whole window the pace rules out is refused, with the same words.
A run the runner holds back after it was queued (a person holds the lane, a broken connector, the pace
closed in between) is cancelled when the window closes before it starts, and the entry is `skipped`
with the run's reason.

## Proof

A finished entry links to its run (`run_id`) and keeps what the run left: its honest status and
summary, the readbacks of the target's own state (expected, found, passed), the final URL and the
screenshot ref, and whether a connector returned a typed result (read it with `run_status`). A trigger
entry keeps its event and deliveries (what each started, and how it ended). An action that publishes
nothing (an export, a check) has its run's status and result as its proof. Nothing is invented: when a
run recorded no readback, page or output, the proof says so.

## Where it lives

- `src/agentos/agenda/`: the store (`agenda.db`, versioned like the other node stores), the service,
  the MCP tools (toolset `agenda`), the CLI and the panel API.
- `agentos.keepalive.service.planned`: what it plans in a range, for the timeline.
- MCP: `agenda_list`, `agenda_get`, `agenda_draft`, `agenda_update`, `agenda_propose`,
  `agenda_cancel` (docs/TOOLS.md); the `plan_action` recipe in `howto`.
- CLI: `agentos agenda list | show | add | propose | approve | cancel`.
- API: `/api/agenda*` (docs/API.md, "Agenda"). The panel has an Agenda screen per workspace.

## What it does not do

- It does not repeat: recurring work is a schedule, and the agenda shows its projected fires.
- It does not write the content. An agent (or a person) drafts the inputs; the agenda only plans,
  asks and runs them.
- It does not hold a lane's pace slot ahead of time: the run claims the pace when it fires, so two
  entries at the same minute on one lane go one after the other, inside their windows, or one is
  skipped.
- It does not keep an approval request open past `agenda_approval_max_days` (14 days by default):
  an entry planned further ahead returns to `draft` with the reason when the cap passes; propose it
  again closer to the date.
- It does not run anything the node could not run by hand. An entry is a plan for an existing flow,
  connector operation or trigger; it adds no new way to act on a site.
- It does not move approvals between nodes. A workspace export carries the entries that have not run
  yet (draft, proposed, scheduled), and the importing node brings every one in as a `draft` with the
  reason "imported from <node>: approvals do not travel, so propose it again". An approval binds to
  the source node's grants and is used up there; the import's plan and output say so. Entries that
  ran stay home as history, and an entry that fires a trigger is left out, like a trigger's schedule
  (triggers are not exported). See docs/WORKSPACES.md.
