# agentos.events: the durable queue between triggers and what they wake

Design: docs/design/always-on-agents.md, section 4.3 (briefs B0 and B4). Runtime service
`event_queue` (the name `events` is the panel's SSE bus).

```
sources                         events.db (SQLite WAL)                targets
cron, watch, webhook, --emit--> events (dedupe on source+key) --+--> flow     RunnerService.submit
mail, run.finished,             routes (owner-written)          |    brain    BrainService.submit + its own grant
lane.held/released,             deliveries (one per route)      |    agent    AgentsPort.deliver (brief B2)
approval.decided, owner.message dispatcher: the controls --------+    workflow one run per event (docs/WORKFLOWS.md)
```

## The event

Something that happened, written down by the node. `facts` are small values the node itself
observed (a slot, a seq, a URL it loaded, a run status) and are safe to show and to put in a
flow's params. `content` is what someone outside wrote; it is untrusted unless the owner
wrote it, travels only inside the fixed frame (`frame.py`), never fills a flow param, and is
purged after 7 days while the row and its facts stay for 30.

```json
{"id": "ev_3f9a21c0d4e7", "source": "watch", "kind": "watch.changed",
 "workspace": "biz-a", "lane": "biz-a/instagram",
 "dedupe_key": "watch:sch_5c1d0e2f3a4b:42",
 "facts": {"schedule_id": "sch_5c1d0e2f3a4b", "seq": 42, "url": "https://www.instagram.com/...",
           "detected_at": "2026-11-04T12:02:11+00:00"},
 "content": {"text": "maria.k commented: \"Do you ship to Riga?\"", "origin": "https://www.instagram.com/..."},
 "taint": ["untrusted"], "cause": {"depth": 0}, "priority": "normal"}
```

Taint by origin: cron, approvals, runs and lane signals carry none; the owner's words carry
`owner`; watch and webhook content carry `untrusted`; mail adds `private`; `turn.finished`
text is model output and carries `untrusted`. A source cannot label outside content `owner`.

## Routes (the panel calls them triggers)

Owner-written standing authority. `add_route`, `update_route`, `remove_route`,
`set_enabled` and `resume_route` refuse every principal but the human owner (or a call from
inside the node). Agents call `trigger_propose`, which puts the full route on an approval
card; the owner's approval creates it exactly as shown, under the owner's grant.

A proposed flow trigger runs only what its card showed:

- **The version.** A flow target that names no version is pinned, when it is proposed, to the version
  newest then, and the card shows it. A newer version runs only after a new approval: a new proposal,
  or the owner editing the trigger to name it.
- **The ceiling.** The flow's effect (the stronger of what it declares and what its steps can do) must
  be within the trigger's `grant.ceiling`. A proposal above it is refused, and a delivery above it fails
  and runs nothing.
- **Upgrade.** A trigger created from a proposal before cards named a version (`version: null` with
  `proposed_by`) runs nothing: each delivery fails and says why, until the owner edits it (an edit that
  leaves the version empty pins the one newest at that edit) or approves a new proposal. A card still waiting from before creates nothing when approved (recorded `invalid`).
- **A workflow target is never proposed.** A workflow carries its own trigger, and `workflow_propose`
  puts it on a card with the version a yes turns on (docs/WORKFLOWS.md, Agents). A route proposed beside
  it would wake whatever version is live, under no card's ceiling, so `trigger_propose` refuses it with
  `invalid_request` and says to use `workflow_propose`. Nothing is created, and no card is made.

A trigger the owner wrote is theirs as written: `version: null` there still means the newest.

```json
{"name": "IG comments biz-a",
 "match": {"source": "watch", "workspace": "biz-a", "lane": "biz-a/instagram"},
 "target": {"kind": "agent", "lane": "biz-a/claude", "session": "new"},
 "instruction": "A new comment arrived. Read it in context on the post. If it needs a reply, ...",
 "grant": {"ceiling": "draft", "lanes": ["biz-a/instagram"], "flows": ["biz-a.instagram.reply"],
           "ttl_s": 900, "budget": {"turns": 8, "minutes": 10}},
 "controls": {"batch": {"window_s": 600, "max_items": 10}}}
```

A cron, watch or webhook `Schedule` fires a route by naming `route_id` instead of
`flow_id` (`Scheduler.add(route=..., cron=...)`). The fire keeps its semantics and its key
(`cron:<id>:<slot>`, `watch:<id>:<seq>`, `hook:<id>:<signed>`), which becomes the event's
dedupe key, so a duplicate tick or a replayed webhook stores nothing new.

## Deliveries and the controls

Each matching route gets one delivery per event (or one per batch or digest). States:
`pending`, `waiting` (with the reason), `delivered`, `done`, `failed` (refused for good),
`dropped` (with the reason), `dead` (attempts exhausted). Delivery is at least once; targets
dedupe on the delivery key. The controls, in the order design section 4.3.6 gives them, with
the defaults of section 8:

| Control | Default | Effect |
|---|---|---|
| route enabled, target healthy | | disabled: dropped; paused: waits; a held lane the target acts on: waits until hand back |
| loop guard | depth 3 | deeper chains dropped; never fires on its own target's events unless `allow_self`; `turn.finished` never wakes its own lane |
| dedupe | off | the same `facts.subject` within `dedupe_window_s` is one delivery |
| quiet hours | 22:00 to 08:00 Europe/Vilnius, delay | `delay` waits for the end, `digest` merges into one delivery at the end, `drop` drops; `urgent` ignores them |
| rate limits | 6 per hour, 48 per day per agent lane | waits for the window, never drops; owner messages are not paced |
| batching | off | merges deliveries within `window_s`, up to `max_items`, oldest first |
| concurrency | 1 per route, 1 turn per agent lane | waits |
| backpressure | 50 open per target | merges into the newest open delivery; then drops only `low`, with one push |
| circuit breaker | 30 events a minute, 3 failures in a row | pauses the routes involved, one push; only the owner resumes |
| priority | | urgent (owner messages), approvals decided, normal, low |
| retries | 3 attempts | backoff from 60 s, then a dead letter and one push |

## The grant a delivery carries

- **flow**: the run is submitted under the route creator's grant, not authorized, so the
  runner authorizes it right before it runs (a commit parks for the owner's approval). Params
  come from `${facts.x}` only. Idempotency key `event:<route>:<event>:<attempt>`.
- **brain**: a grant minted for the delivery with `policy.mint_child_token` from the
  creator's grant (only the route's lanes, at most its ceiling, no specials, the route's TTL
  and budget, no further delegation), seeded with the events' taint through
  `policy.context_for_grant(grant_id).taint(...)`, and handed to `BrainService.submit` as
  `turn_grant` so the brain uses exactly that grant (revoked when the task ends).
- **workflow**: no grant of its own here. The run takes the grant of whoever saved the live version
  (docs/WORKFLOWS.md), and each step authorizes itself. Content fills `{{trigger.x}}`, marked
  untrusted, so it can fill a step and never decide one. A delivery is done once the run exists,
  because a run can wait for days. A workflow route ignores the node's default quiet hours and the
  event storm breaker: there is no general limit on a workflow, and its steps cannot wake it.
- **agent**: the narrowing (`TurnGrant`) and the taint seed travel in the `TurnRequest`; the
  agents service mints the turn grant from the lane grant (`mint_turn_token`, brief B3).

## The interface left for agent lanes (brief B2)

`agentos.agents.ports.AgentsPort`, registered as runtime service `agents`:

- `lane_state(lane) -> AgentState | None`: `None` means no such agent lane (the delivery
  fails); a state other than `cold` makes the delivery wait with that state as the reason.
- `async deliver(TurnRequest) -> TurnReceipt`: queue one turn, idempotent on `request.key`.
  The request carries the wake message (`prompt`), the delivery id, the event ids, the
  attempt, the deepest cause depth, the session choice, the grant narrowing and the taint
  seed.
- `lane_limits(lane) -> (per_hour, per_day)` (optional): the lane's own caps; the node
  defaults otherwise.
- When the turn ends: `event_queue.delivery_finished(delivery_id, "done" | "failed",
  status=..., detail=...)`, and emit `turn.finished` with `cause={"event", "turn", "lane",
  "depth": request.depth + 1}` and the final text as content.

Until B2 lands a node has no `agents` service and agent-target deliveries wait with the
reason "no agent lanes on this node yet".
