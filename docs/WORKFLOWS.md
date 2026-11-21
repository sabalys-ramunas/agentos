# Workflows

The contract for workflows: what one is, how it starts, what its steps do, what a run records, and
what crosses the node channel. It is additive under `docs/PROTOCOL.md` v1. The core wins, and
The hosted edition's contract mirrors this file.

A workflow and a routine are the same thing. The panel says routine. A workflow is a trigger, then
steps, then what happens with the result. It is built on what already exists: triggers are events
routes and schedules (`src/openajar/events/`, `src/openajar/runner/schedules.py`), a run is a runner
`Run` of kind `workflow`, and an action step is an ordinary flow or connector run. There is no
second engine.

## Definition

The part a person or an agent writes, and what a developer's file holds (JSON or YAML):

```json
{
  "id": "harbor.routine.invoice-reminders",
  "name": "Invoice reminders",
  "workspace": "harbor",
  "trigger": { "kind": "schedule", "cron": "0 9 * * 1-5", "label": "every weekday at 09:00" },
  "steps": [
    { "id": "find", "kind": "action", "label": "find overdue invoices",
      "operation": "harbor.xero.overdue", "inputs": {} },
    { "id": "each", "kind": "loop", "label": "for each invoice", "item": "invoice",
      "over": "{{find.invoices}}",
      "body": [
        { "id": "remind", "kind": "action", "label": "send a reminder",
          "operation": "mail.send@harbor/mail",
          "inputs": { "subject": "Invoice {{each.number}}", "body": "It is {{each.days}} days overdue." },
          "send": { "list": "list.harbor.customers", "to": "{{each.email}}" } }
      ] }
  ]
}
```

`id` is optional when creating: the node makes `<workspace>.routine.<slug of name>`. An id is a flow
id, so the switch, the panel's list and `flow:<id>@<version>` all work, and it may not equal a saved
flow's id. A step id is `[A-Za-z0-9_-]{1,40}`, unique in the workflow, and never `trigger`.
`version` is the content hash of the definition (`sha256:...`, as for flows). `revision` is the
count of versions saved, for showing a person.

Limits on the shape: 100 steps in all, loops nested at most 2 deep, branches at most 8 deep. These
bound the definition and the run's record. They do not limit how often a workflow runs.

Saving accepts an unfinished definition (a step with no operation yet). Turning it on does not. A
safety rule (below) is refused on every save, finished or not. Errors carry
`problems: [ { target, code, words } ]`, where `target` is a step id or `trigger` and `words` is a
sentence for a person. `code` is one of the codes named in this file. A `site_change` adds `not_a_read`
(a draft or commit endpoint), `not_a_site_api`, `too_often` and `unknown_operation` on the trigger, and
`bad_trigger` for a list, key or params the endpoint does not have.

## Triggers

`RoutineTrigger`: `kind` is `schedule`, `webhook`, `email`, `page_change`, `site_change` or `manual`,
with an optional `label` (the words a person reads).

| kind | fields | how it arrives | fields it gives steps |
|---|---|---|---|
| `schedule` | `cron`, `timezone?` | a route's cron `Schedule` | `when` |
| `webhook` | none in; `hook` out | signed POST, relayed (see below) | `body`, and each top-level JSON key, all untrusted |
| `email` | `lane` (a mailbox id), `from?`, `subject?`, `unseen_only?` | the node polls IMAP, dialing out | `from`, `subject`, `body`, `id`, all untrusted, and `in_reply_to_run`, the node's own finding |
| `page_change` | `lane`, `url`, `watch?: { locator, read, every_s }` | a route's watch `Schedule` | `url`, `count?`, and `what` (untrusted) |
| `site_change` | `operation`, `params?`, `list`, `key`, `every_minutes` | an interval `Schedule` reads a Site API endpoint, and a route's event carries what changed | `added`, `removed`, `changed` (lists of rows, all untrusted), `count`, `count_added`, `count_removed`, `count_changed`, `truncated` |
| `manual` | none | a person or an agent runs it | what the run was given: a person's values are trusted, an agent's are untrusted |

`from` and `subject` filter on an exact, case-insensitive substring, and `unseen_only` leaves out mail
already marked read, all on the node. They choose what the owner wrote them to choose, and a `from` is a
sender's claim, not proof of who wrote. A filter is not a trust decision: a message that matched one is
still a stranger's text, so a forged `From` that matches gains nothing.

`id` says which message it is, as `mail_read` takes it. `in_reply_to_run` is the run a message answers, or
empty. The node works it out (see "Replies" under "Sending to people") and no field a sender wrote can set
it, so it is trusted and a branch may read it. A manual run's value for it counts as any manual value does.

**No public port on a node.** A webhook reaches a node only through the controller or the cloud,
which relays the exact bytes and headers (`hook_relay`) from `POST /hooks/{node_id}/{token}`. Mail
is polled by the node, outbound only, beside the scheduler and never in its tick: one read of a mailbox at a
time, so a slow server holds up only that mailbox. A node with no controller has no webhook trigger.

**Webhooks are signed and replay-safe.** The node makes the secret and shows it once. The sender
signs `X-Agentos-Signature: sha256=HMAC(secret, timestamp + "." + body)` and sends
`X-Agentos-Timestamp` (unix seconds, within `max_skew_s`, default 300) and optionally
`X-Agentos-Delivery`. The node verifies. The cloud never holds the secret, so it cannot forge a
delivery. The event is keyed by what was signed, so a captured request replayed, even under a new
delivery id, starts nothing. `RoutineHook { url, secret_hint, last_received_at }`: the node returns
`{ token, secret_hint }` and, once, `secret`. The cloud builds `url` from `token`.

### `site_change`

"When a new order appears, do this": a routine that starts when the answer of a Site API endpoint changes.

```json
{ "kind": "site_change", "operation": "connector.harbor-supplier.open_orders@harbor/supplier",
  "params": {}, "list": "orders", "key": "number", "every_minutes": 15 }
```

`list` is the output that holds the rows. `key` is the field that says which row is which. `params` are
the endpoint's input. All of them are literals, like every trigger's own fields.

**A check is an ordinary read.** A schedule of kind `every` runs the endpoint on its surface, under the
grant of whoever saved the version that is live, so pace, holds, a freeze, health and Ask me first all
apply, and each check is a run with its cost like any other. A watch follows the active version: it pins
none. Its read goes to the site, never to the HTTP cache, and a good read fills that cache as any run does.

**What starts a run.** The node compares each good read with the last one it kept (the baseline):

- The first read is the baseline. It starts nothing.
- A read that failed starts nothing and keeps the baseline. That is any failure: a held lane, a pace
  refusal, a freeze, a broken endpoint, a schema violation, `empty_result` under `expect.min_items`, a
  read in which no row has its key, or a key that is private now. The next good read is compared with the
  baseline as usual, so a row that arrived in the meantime starts one run, and what came and went in the
  meantime starts nothing. When that read comes depends on the failure:
  - Most failures pass by themselves, and the next check reads again. That goes for a held lane (a site's
    own 401 holds one), a rest (a site's own 429), a pace refusal and `empty_result`.
  - A failure on the contract leaves the endpoint broken on its surface: a locator that no longer resolves,
    a schema violation, or a `read_json` body of the wrong shape. With no params the first one does it. With
    params it takes a second set of params failing the same way, so until then the next check reads again.
    A site's bad moment (a 5xx, a request that did not come, a slow body) only degrades the endpoint, and
    takes three runs in a row to break it. The schedule of a broken endpoint skips its slots and the watch says it is
    waiting, so nothing is read until a person activates a repaired version or clears its health. Only
    then is the next read compared, however long ago the site came back.
- **Two reads in a row.** A read that would remove half or more of the rows known is not believed on its
  own, whatever `expect` says, because a site that broke looks just like it. Nothing starts and the
  baseline stays, and the node keeps the keys that read lost. A row is removed only if both reads say it
  went. Rows the next read lost that the first did not are not removed: they stay in the baseline, and
  go by the same rules from the read after that. If the rows come back, or that read fails, nothing is
  removed.
- **A read that may not hold every row calls nothing removed**, because a row it does not hold looks like
  one that went. That is a list cut at `max_items` (its own `total` is over its `count`), a list the output
  cap trimmed, or a read in which any row has no key. Those rows stay in the baseline, so they are not new
  when they come back. A value clipped at its `max_chars`, or another list of the endpoint being cut, says
  nothing of this one, though the run's `truncated` is true then too.
- Anything else that differs starts one run, and that read is the new baseline.

Turning the workflow off forgets the baseline, so turning it on again does not replay the time it was
off. So does a change of what is read: another endpoint, surface, list, key or params, another lane or
account behind the surface, or a new version whose rows have another shape. The next read is then a first
read, so a changed output schema never looks like every row changed. An activation that leaves the shape
alone keeps the baseline.

**Rows.** A row in `added` or `removed` holds the endpoint's declared fields and `row_key`. A row in
`changed` also holds `what_changed` (field names) and `was_<field>` for each of them. `{{each.number}}`,
`{{each.row_key}}` and `{{each.was_total}}` read them in a loop. Only declared fields are compared, and a
row's place means nothing, so a re-sorted table starts nothing. A row with no key is left out and counted
(`watch.skipped`), and it may be a known row that lost its key, so a table with such a row removes nothing
until every row has one. Of rows with one key, the one kept is the first by its values, not by its place.
That is a duplicate, and a duplicate holds nothing back. A value is clipped at 300 characters, though a
change past the clip is still seen.

A private field shows as `(hidden)` wherever a row goes, and `what_changed` still names it. A private
field cannot be the `key`, because a key is shown. That holds across versions: activating a version that
makes the key of a watch that is on private is refused, naming the routine. If one gets through, every
read of it fails (`private_key`), so none of its values reach the baseline or an event.

**Limits, checked at save.** `every_minutes` is at least 5, at least the endpoint's `cache_s`, and at most
a week. A watch alone may use at most half of the endpoint's `per_hour` and `per_day`, and may not check
inside its `min_gap_minutes`. Only one check runs at a time: one that would overlap is skipped, and a node
back from a day off checks once. An event carries at most 100 rows of each list, and what fits in one
event, sorted by key. Past that `truncated` is `true`, the `count_*` fields stay true, and the rows left
out are not sent later.

**The baseline** stays on the node, in `runner.db`, and goes with the workflow. It holds the declared
fields of at most 2000 rows and 2 MB. A longer list is a failed read that says so. So is a read that would
leave more than that with the rows it keeps from before, as a cut list that has been growing does:
nothing starts, no row is dropped to make room, because a dropped row would look new when it came back.
Turning the workflow off and on again starts over. While a drop waits for its second read the node also
keeps the keys that read lost, which are never more than the rows it knows. A private field is held as a
keyed digest of its value, never the value. None of this is in a view, report or event: `workflows_catalog`
carries metadata only, and what a run shows on the node is the rows of that one change, like any trigger's
data. Off the node a run says how many rows there were ("What leaves the node").

**A person's view.** `watch` on the workflow says how it stands, and why it stopped, in plain words. A
paused API stops the watch until it is activated, and removing an API or dropping an endpoint is refused
while a watch uses it, as for a schedule or a step. A freeze stops the schedule and the route, and the owner
starts them again.

**A watched endpoint stays a read.** Save and turning on check that it is one. Activating a version in
which it would no longer be (an input step, a declared draft or commit) is refused while a watch that is on
reads it, naming the routine. A watch that is off holds nothing back, and turning it on checks again. Right
before each check the node looks at the effect once more, whatever changed it (`pay`, say): one that is not
`read` is not fired, and the watch says it stopped and why. It carries on by itself once a version that
only reads is active.

## Steps

Every step has `id`, `kind` and `label` (a phrase that fits the sentence).

| kind | fields | does |
|---|---|---|
| `action` | `operation`, `inputs`, `send?` | runs a flow, a connector operation or `mail.send` |
| `branch` | `when { left, op, right }`, `then`, `otherwise` | takes one arm |
| `wait` | `amount`, `unit: minutes \| hours \| days` | waits, durably, holding no lane and no slot |
| `loop` | `over`, `item`, `body` | runs `body` for each item, one at a time, at most 1000 |
| `ask` | `question`, `on_no?: stop \| continue` | asks a person; gives `answer` (`yes_no`) |
| `notify` | `message` | tells the owner; reaches no one else |
| `stop_if_replied` | `after` | stops chasing whoever answered what the `mail.send` step `after` sent |

`operation` is a literal: `<flow id>`, or `<flow id>@<lane or surface>` when its lane is a pattern
or it is a connector (`connector.reddit.post_text@biz-a/reddit`, `mail.send@harbor/mail`). In a
template it may be `tag:<tag>`, resolved when the template is used. A saved workflow holds no tags.

`when.op` is `is`, `is_not`, `contains`, `more_than`, `less_than`, `is_empty` or `not_empty`.
`when.right` is a literal. `more_than` and `less_than` need numbers on both sides, or the step fails.

`on_no` defaults to `stop`: the run ends `cancelled`, and what ran before stands. An answer never
stands in for a later step's own authorization. A question nobody answers in time fails the step
(`ask_expired`), which can be retried.

A failed step stops the run. There is no failure arm yet.

## Data between steps

A reference is `{{step.field}}`, and only that. `trigger` names the trigger, and a loop's id names
its current item: `{{each.number}}`, or `{{each.value}}` for a list of plain values.
`RoutineField { key, label, type, untrusted?, rows? }`, with `type` one of `text`, `number`, `date`, `list`
or `yes_no`. An operation's `produces` lists its fields. A list's item fields are the fields keyed
`<list>_<field>`, so `invoices` has `invoices_number`, read in a loop as `{{each.number}}`.

A field that is exactly one reference keeps its type. Anything else is text. Substitution is one
pass: a value that happens to contain `{{...}}` or `${...}` is text, and is never read again.

A reference may only look back: at the trigger, at earlier steps on its path, and at the loops
around it. Anything else is refused (`bad_reference`).

**Trust.** `untrusted` follows the value, and is decided by the node, never by the definition:

- Every webhook field, and every email field a sender wrote, is untrusted. So is a page's text, every row
  of a `site_change`, a manual run's values from an agent, and `{{person.name}}` for someone who came as a
  lead.
- A connector's `untrusted_fields` are untrusted, at save and again on the value at run time.
- A plain flow's output is untrusted unless a version the human owner saved says otherwise
  (`operation.produces[].untrusted: false`). An agent's mark counts for nothing.
- An `ask` answer is a person's, and a step's status and `in_reply_to_run` are the node's: all are trusted.

Untrusted data **may fill** an action's inputs, a `notify` message and an `ask` question.
Untrusted data **may never decide**. These refuse it at save and again at run time
(`untrusted_decides`):

- `branch.when.left` and `loop.over`
- `send.to`
- an input that names a target: a param used in a step's `url` (a `navigate`, a `readback`), or a
  param the operation marks `target`

One thing a loop may go through though it is untrusted: a list the node built of a site's rows
(`RoutineField.rows`), which today means `{{trigger.added}}`, `{{trigger.removed}}` and
`{{trigger.changed}}`. That the row is new or gone is the node's own finding, so the loop follows no
instruction of the site's. Every field of every row stays untrusted, and the list stays untrusted
everywhere else: a branch cannot read it, and an input it fills counts as filled from outside. A branch
reads the node's own `count_added`. A list that merely has this name, in a manual run's values, is not one:
only an event from the node's own watch marks it, and such a run stops with `untrusted_decides`.

What a site does choose is how many rows there are, so an action inside such a loop counts as filled from
outside whatever its inputs are. See "What runs and what asks".

These are literals, with no reference allowed at all (`must_be_literal`): `operation`, `send.list`,
`when.right`, `wait.amount`, and the trigger's own fields. So nothing written outside can pick a
step, an account, a list, a person or a page.

A trusted value read through an operation stays trusted even when an untrusted value was its input
(`{{trigger.sku}}` into a stock check, then a branch on the stock). The arms were the author's, and
the number is what the owner's own account said.

## What runs and what asks

A workflow runs on its own unless Ask me first is on. It is authorized **step by step**: nothing
authorizes a run as a whole, and each action step passes `ctx.authorize` for its own effect, on its
own lane, under the run's grant, right before it acts.

| Step | Result |
|---|---|
| read, draft | runs |
| commit, every switch off | runs on its own |
| commit, the workflow's switch on | asks first |
| commit, its operation's, its account's, or its surface's lane's switch on | asks first |
| pay, irreversible, anywhere, whatever any switch says | always asks |
| commit with an untrusted value among its inputs, a lead's name included | asks first (the `[taint] downgrade` overlay), with the reason |
| commit or send inside a loop over a site's rows, however deep, whatever fills it | asks first in the same way, and the step's `why` says it is because the site decides how many times it acts |
| anything over a meter's limit | refused, as for any call |

The overlay rows are why the templates act on what they read from the owner's own accounts, and use
what arrived only to look it up. A run a trigger started is never "in person", even under the
owner's grant, so it never skips an overlay.

**Acting on a site's rows.** Rows are `{{trigger.added}}`, `{{trigger.removed}}` and
`{{trigger.changed}}`. A row's text cannot pick what happens, but a site that adds 100 rows would get 100
acts, so each act inside a loop over them asks, a branch or a loop inside it included. A read or a draft
there runs on its own, and so does a commit after the loop, because that is only one. A standing approval
covers such an act as it covers any call that follows untrusted text: it has to name the exact content the
owner saw. A commit there is a counted step and a send goes through the ledger, so both count against the
sending account's daily limit, unless a person marked the step `not_to_people`. A run does at most 100 such
acts, across all its loops and steps: an event already carries at most 100 rows of each list, and this
bounds what three lists, nested loops and several steps could multiply that to. The next one fails
`too_many_rows`, and what was done stays done.

**Switches.** `scope: "flow"` with the workflow's id, as for any routine. It follows the id across
versions and outlives a deleted workflow.

**Agents.** An agent may save a workflow, run it under its own grant, and turn Ask me first on. It
may never turn it off, and:

- A workflow runs under the grant of whoever saved the version that runs. Turning it on does not
  widen that. An agent's workflow stays inside the agent's lanes, ceiling and budget, checked at
  save and again before each step.
- Only a person turns a trigger on (`enabled`), as only a person writes a route. An agent calls
  `workflow_propose`, which puts the whole definition on a card. A yes turns on exactly the version
  on the card. A webhook workflow is the exception: its secret is shown once, so the person turns it
  on in the panel.
- A trigger runs the `live_version`: the version a person turned on, or a person saved. An agent's
  new version does not go live until a person looks at it (`needs_review: true`). So an agent cannot
  swap the steps under a person's switch.
- A new workflow an agent saves that touches a lane or operation where a person set Ask me first
  starts with its own switch on, with `ask_first_why`, so a copy does not shed it.
- A definition has no field for any of this. Unknown fields are refused.

"A person" means what it means for turning Ask me first off: the CLI, the signed-in panel, or a
controller relaying a person in a signed message. A bearer token is not a person.

## Sending to people

Anti-spam is a hard rule, and it does not rest on anything an operation says about itself.

An operation that sends to people declares `sends` (`RoutineOperation.sends`: `mail.send` always does;
a flow says so in its `operation` block, and a connector on its operation, with the param names for the
address and the message). Such an operation runs only as a send step: running it any other way is
refused, and only a person can save a version that drops the mark. A send step needs a `send` block,
and only such an operation takes one (`send_only_for_senders`).

**Undeclared commits count too.** A step whose operation commits or more, and declares no `sends`, spends
the sending account's daily limit when it runs inside a loop, or when a list feeds it, however it is
described. See "Counted steps" below.

`RoutineSend { list, to? }`:

- `list` is a list the owner supplied in this workspace. Only a person creates or changes one, and
  it cannot be filled from a step's output. An agent sees its size, never its addresses.
- Without `to`, the step sends to everyone on the list, one message each.
- With `to`, it sends to that one address, and only if it is on the list. `to` is a trusted reference.
  An address not on the list is skipped, and the step records it.
- `{{person.email}}` and `{{person.name}}` fill the message for each address. `person` is not a step id.
- A send step is authorized once for what it will send, not once per address. In a loop, once per item.
- The card shows every message filled for its person, exactly as the send fills it (`messages`, one
  `{to, inputs}` each), and the approval binds those. A message filled differently when it is sent is
  not sent: the step fails `approval_mismatch`, and nothing more goes. A long list cuts the preview,
  and the cut says so. The card then keeps every message, and `messages` on the approval says how many
  and how many pages. `GET /api/approvals/{id}/messages` serves them 25 at a time, in order (each page
  gives the next page's `cursor`), and the last page gives `seen`. Only an approve that carries `seen`
  counts (`see_whole_card` otherwise), so no path (chat, a batch, a relayed yes, a token) approves it
  from the cut alone, and a standing approval never releases it. A secret or vault value in a message
  shows redacted, as on every card, and the message is marked `withheld`.
  `openajar approvals approve` prints every message and asks first. The binding stays over the whole
  list, never the preview. A cut card made before this keeps no messages, so it cannot be approved:
  deny it and retry the run, and the new card shows them all.

Before each message, in this order:

1. **Suppression.** An address on the workspace's suppression list is skipped, whatever the list
   says. Suppression outlives a list being replaced. Anyone may add to it; only a person removes.
2. **Daily limit.** Each sending account (the mailbox or surface) has one: 200 a day by default, days
   ending at the owner's midnight. Only a person raises it, up to the node's ceiling of 2000. A step
   that reaches it waits for the next day and carries on, so the rest are never dropped or sent in
   a burst. The limit is the sending account's, across every workflow and list.
3. **The ledger.** One row per run, step and address, written *before* the message leaves.

Every message carries an **unsubscribe**: `List-Unsubscribe` (with `-Post` when it is a URL) and a
footer line, added by the node, so a workflow cannot leave it out. An operation that cannot carry
one is refused. The link holds an opaque token and needs no public port: it points at the controller
(`/u/{node_id}/{token}`), which relays the token (`unsubscribe_relay`), or is a `mailto:` to the mailbox
the message came from, which the node's own poll reads. The token says nothing about who it is for.
A list send always carries it. The one exception is a one-to-one `mail_send` reply to someone who wrote
first (docs/EMAIL.md, "Sending"), which is a conversation and not marketing. The node checks that, and
no flag an agent sets can ask for it.

**One address, one person.** Every address goes through `normal_address`: a plain addr-spec, the domain
lowercase and in IDNA form, and nothing else (no comment, display name, quoted local part, route,
whitespace or control character). What the rules checked is exactly what the server is told, and a
`+tag` is left off when two addresses are compared, so `jane+x@acme.com` gets past no suppression or
daily rule, while the mail goes to the address as given.

**No double send.** The ledger is what a retry reads. `sent` is never sent again. A row left
`sending` by a crash or a failure that may have gone out is `unknown`: it is never repeated, and the
step says how many are in doubt. A retry sends only to those with no row yet.

**Counted steps.** A commit, pay or irreversible step with no `sends` is counted when it is inside a
loop, or when any input refers to a list, at save or in the values at run time. It takes one slot per
loop item, or one per list item when a whole list feeds it, all or none, from the same daily limit as
real sends, before it acts. When the day is used up it waits for the next day and carries on. A list
bigger than the daily limit fails the step (`over_daily_limit`), since it can never fit. A step that
certainly did nothing gives its slots back; one that may have acted keeps them.

The only way out is `not_to_people: true`, and only a person can mark it. It counts only in a version
the human owner saved, or a connector the owner installed, as `publishes: false` and `untrusted: false`
do. An agent's mark counts for nothing, an agent's new version loses a person's mark, and the step
runs the very version whose mark was read. `RoutineOperation.not_to_people` says what stands.

What this does not cover: a single commit outside a loop with no list behind it is not counted, even
if many runs of it reach people one at a time. That is the owner's "no general limit" (below).

`RoutineSendLimit { step, list, list_size, daily_limit, sent_today, suppressed, unsubscribe, counted? }`
is worked out by the node. `unsubscribe` is `true` for a send step. A counted step has `counted: true`
and `unsubscribe: false`, because nothing declared where to put one. There is no general limit on how
often a workflow runs. These rules are about what may reach people, and nothing else has a cap of this kind.

### Replies

`stop_if_replied` lets a routine stop chasing someone who answered. `after` is a literal: the id of an
earlier `mail.send` step on its path (`bad_reference`, or `not_a_mail_send` for anything else, since only
the node's own mail has an id a reply can name).

Every message a mailbox sends for a routine gets a `Message-ID` nobody can guess, kept before it leaves
with its run, step, address and mailbox. A message is a reply when all of these hold:

- its `In-Reply-To` or `References` names one of those ids (the last 50 of `References` are read),
- it arrived in the mailbox that id went out on,
- its `From` is exactly one address, the one that message went to,
- and it is not an automatic answer (`Auto-Submitted`), which is nobody answering.

So `From` alone proves nothing, because it can be forged, and an id alone proves nothing, because a
forwarded thread carries it. A reply can stop mail only to the person it answers, in that thread. Someone
who answers from another address than the one written to is not matched, and gets the follow-up.

The step reads who `after` reached (`sent` or in doubt) and who answered:

- `output` is `{ replied, waiting }`, counts and never addresses, as is all of a run's record.
- From here on, a send step in this run, or in this row of a loop, goes only to those `after` reached and
  who have not answered. Each is checked as it goes, so a reply that comes while a send waits for tomorrow
  still counts. The ledger marks the rest `skipped`, with the reason.
- With nobody left to chase, the step's status is `stopped`, and the rest of the row ends, or the rest of
  the run outside a loop. That row's later steps show as `skipped` in `items`, with a `note` saying why, and
  the loop's `output` gains `stopped`, how many rows ended this way. A run that ends this way succeeded.

A follow-up is an ordinary send step, so it keeps every rule above: the list, suppression, the unsubscribe,
the ledger and the daily limit. `stop_if_replied` has no field but `after`, and it can only send less.

### Leads

An agent never adds to a list. `leads_propose(list, leads)` (docs/EMAIL.md, "Leads") puts what it found in
the list's review queue, as one record per address: `{ email, name?, company?, source, why? }`. Anything else
in a record, a record that is not one, an address that is not one, or a source that is neither a name nor
http(s) is `refused`, by its index. There is no field for a pattern, a file or a verification, and nothing
here touches a mail server.

**Bounds.** At most 50 in a batch, 200 a day for a grant family, and 500 a day for a list
(`too_many_leads`). What counts is what was received, whatever became of it.

**What never enters the queue:** a suppressed address, one on the list, one already proposed for it, and one
a person dropped, for the same family, for 30 days. A dropped lead keeps only a salted digest. Another family
can propose it, and a person reads it again.

**An agent learns nothing about who is on a list.** The answer is `{ batch, received, refused, status }` and depends
only on the records, never on who is on the list, suppressed or dropped, and so do the bounds. `RoutineList`
holds nothing of the queue. The card is the node's own question under no grant, so no agent's family reads
how many got through. What an agent does see is the list's size, in `workflow_catalog` and a workflow's
`send_limits`, so once a person has decided it can read how many were added, and never who. It never hears
what a person decided, so a size that did not grow says nothing about one address. How many on a list are
suppressed is shown only to a person, and `send_suppress` answers `{ suppressed: true }` whoever the address
is, so suppressing addresses one at a time tells an agent nothing about the list.

**The card** is written by the node from the records, and holds counts and source names only: "An agent
proposes 50 leads for Marinas: 30 from LinkedIn, 20 from Baltic Marine Directory." A card reaches the controller
and the notifier, so no person's name, company, domain or address is on it, even one typed into another field.
A source shows as a page's host, or the label it was given up to any path, as one clipped line of text, never a
link. A source that names the lead's own domain, name or company reads "their own page". At most five sources
are named, and the rest are counted. An agent's `why` is not on it. Who they are, each address and each source
are read on the node, in the panel or `openajar workflows leads`.

**Only the owner in person decides.** Take them all or Drop them on the card, through the panel's own answer,
counts only if `answered_in_person` says so, without `relayed`: a controller's relay does not count, because
nobody there has read an address. Nor does the node token, an admin grant, an agent, a chat yes or a member.
An answer that does not count decides nothing. The batch stays, and the owner hears so. To pick some, the
owner uses `POST /api/workflows/leads/{batch}` (`{ take: "all" | [addresses] }`) or the CLI, and the rest count as
dropped. What a person took joins the list as it is, a batch waits 7 days and then goes, and someone
suppressed in between stays off.

**A lead on a list** keeps its `source`, when it was added, and who took it. The owner can export that
(`GET /api/workflows/lists/{list}/leads`) and erase a lead and its source (`DELETE /api/workflows/leads`
with `{ workspace, email }`), which also forgets what was sent to them and who answered, and stops a run
already under way from reaching them. An erase keeps a suppression, the ledger (or it would give a day's
limit back) and the unsubscribe token (or a link already sent would stop working). The family that proposed
them stays out as if they had been dropped.

**The first time.** The first card says, in plain words, that the owner is the sender and that rules on
unsolicited mail differ by country, and so does `GET /api/workflows/leads` until a person has seen it. A
person's answer to that card stores it as acknowledged, with who and when, as does `POST
/api/workflows/leads/note`. It blocks nothing.

A list changes only by a person. There are no lists filled by scraping or by a step's output, and leads are
how an agent proposes, one found record at a time, for a person to read. There is no bulk import through
this path, no guessing or probing of addresses, no bulk account creation, and no feature for evading a site's
limits.

## Runs

`RoutineRun`:

```
{ id, flow, version, revision, status, cause, result, by: trigger | person | agent | retry,
  started_at, finished_at, output: RoutineOutput | null, steps: [RoutineStepRun], cost }
```

`status` is a `RunStatus`, and it is honest: `waiting_approval` while a step waits for a person,
`partial` when a step that may have acted failed, `failed` only when nothing outward happened, and
`succeeded_verified` only when every action step was checked.

`RoutineStepRun`:

```
{ step, label, short?, kind, status, started_at, finished_at, input, output, untrusted?, arrived?, why?,
  note?, attempts, approval_id?, run_id?, items?: [ { label, status, note? } ] }
```

`status` adds `skipped` (a path not taken, or a step a finished run never reached), `waiting` and `stopped` (a
`stop_if_replied` with nobody left to chase). A step a run still under way has not reached yet reads `queued`. There is one entry per step id, in order, with `items` for one in a loop: one for each row, as
`{ label, status, note? }`, numbered as the rows are. `label` is as it was when it ran. `run_id` is the child run,
for its proof. The first entry is the trigger (`kind: "trigger"`).

`why` says in plain words why an act inside a loop over a site's rows asks.
`input` and `output` show what the step had, typed. `untrusted` lists the keys that came from
outside. `arrived`, on the trigger, is the kind of what came from outside: `email`, `webhook`, `web_page` or
`agent_input`. A hidden param shows as `${name}`, a private field as `(hidden)`, and vault values are
scrubbed. Each value is clipped at 4 KB.

This is a run as the node has it, for a person there and for an agent, whose family is labelled for it.

### What leaves the node

Content stays on the node, for every trigger. The rule is one place, `runner/workflows/mirror.py`: only what
its tables name crosses, so a field added to a run stays on the node until someone adds it there. The owner has
not made this final. To send more or less, change those tables.

The mirror of a run is what these carry: the `workflow_run` event, the `run` of a `workflow_result`, what a
workflow's row says of its runs (`latest`, `stuck`, `last_run`), and a run read with a token on the node API.

| Crosses | Stays, as `withheld` |
|---|---|
| a run's `id`, `flow`, `version`, `revision`, `status`, `cause`, `by`, times, `cost`, and `result` when the node wrote it | every value of a trigger, from an email, a webhook, a site's rows, a page, or a run by hand |
| a step's `step`, `kind`, `status`, `label`, times, `attempts`, `approval_id`, `run_id`, `why` and `short` | every `input` and `output` of an action, a notify and a trigger, so a filled notify, a filled question and a value a flow shows |
| `arrived` | `untrusted`: the names of what a webhook sent are a stranger's too |
| a loop's `items`: row number, status, and the node's words | anything `run_view` does not name in `mirror.py` |
| what the node itself wrote in a step: `took`, `items`, `count`, `stopped`, `amount`, `unit`, `after`, `replied`, `waiting`, `answer` | the left side of a branch, which is a value |
| an output card's `kind`, `title` and `more` | any other field of the card |

Where a step had other values there is a mark in their place: `{ withheld: true, fields, size, rows? }`. `fields`
is how many, `size` is how many characters they were as shown, and `rows` is how many rows their lists held. A
failure's words stay when they may quote something: a child's error, a value, or a crash. `note` and `result`
then read "The details stay on the computer that ran it." The words the walk wrote itself cross.

Three things still carry a run's content off the node, and each is its own decision:

- **The approval preview.** A send or an action that asks puts what it would send on its card, and a card reaches
  a controller, so a person can decide there. That is the `mail_send` card too. It is an open decision of the owner.
- **A filled `ask`.** Its question is a card in the same way, with what filled it.
- **A filled `notify`.** Its text goes to the notifier, which is what the step is for. Where it goes is what the
  owner set up, and a member's push carries no content unless the owner turned that on.

The `run` event (`docs/PROTOCOL.md`) is not the mirror: it carries a run's `summary` and error words as it does for
any flow, so a child's words reach the controller there when a step fails.

**An agent reading a run.** `workflow_runs` and `workflow_retry` hand over what the run holds, so they label the
family as `mail_read` does (`untrusted` and `private`, with the kind of what arrived), and its next post or send
asks. A run nothing from outside reached labels nothing. `workflow_get`, `workflow_list` and `workflow_save` hold
none of a run's content, because a row is the mirror, so they label nothing. A token on the node API reads the
mirror: a route has no family to label, and an agent that wants the content uses the tools.

**Retry.** A `failed` or `partial` run can be retried: the failed step runs again in the same run,
on the same pinned version, with what earlier steps produced, and everything after it follows.
`attempts` goes up and `by` becomes `retry`. It is authorized again. A step that may already have
acted (its child run is `partial`) is refused (`retry_may_repeat`) unless a person passes
`force: true`. A send step never needs that, because of the ledger.

## Catalog and templates

`RoutineCatalog { operations, lists, templates, trigger_fields }`, from the node, scoped to the
caller. `trigger_fields.site_change` says what every watch gives: the fields of its rows depend on the
endpoint, and an operation's `produces` does not list them. `RoutineOperation { id, name, lane, workspace, effect, params, produces, sends, tags }`.
`id` is what a step's `operation` holds, with `@<lane>` already in it.
`RoutineList { id, name, workspace, size, suppressed }`. An agent, by tool or token, gets it without
`suppressed`, and the same goes for `send_limits`. A list of a workspace the caller cannot reach is not shown.

`RoutineTemplate { id, name, sentence, note, icon, developer?, trigger, steps }`. The templates are
`order`, `invoice`, `newsletter`, `outreach`, `post`, `webhook`, `new_row`, `reply` ("When someone replies,
tell me"), `follow_up` ("Follow up once if no reply in 3 days") and `to_list` ("Send this to my list"). Using one
resolves each `tag:` to what the workspace has, or leaves the step unfinished for a person to choose. `new_row`
leaves its trigger unfinished, for a person to choose the endpoint, the list and the key. `reply` takes the
workspace's mailbox if it has one, and otherwise leaves that to choose. `follow_up` and `to_list` take their
text from a manual run's values, so an agent's run of one asks first, and each asks wherever Ask me first says.

## What a saved workflow shows

These are `Flow` fields on a workflow, worked out by the node: `name`, `trigger`, `steps`, `sites`,
`enabled`, `next_at`, `version`, `revision`, `live_version`, `needs_review`, `latest`, `stuck`,
`waiting`, `always_asks_steps` (`RoutineAlwaysAsks { step, because: pay | irreversible | ask_step }`),
`send_limits`, `missing` (`[ { target, words } ]`), and the Ask me first fields a flow already has.
`lane` is `<workspace>/*` and `effect` is the strongest effect of its steps.

A `site_change` workflow also has `watch { state, words, rows, skipped, failures, last_checked_at,
last_changed_at, last_run, next_at }`, for the version that runs. `state` is `off`, `starting`, `watching`,
`confirming` (a drop waits for its second read), `failing`, `waiting` or `stopped`. `words` is the node's
own sentence. `rows` is a count: no row and nothing a site wrote is in here.

## Node API, tools and files

```
GET  /api/workflows                     GET  /api/workflows/catalog
POST /api/workflows                     GET  /api/workflows/{id}
PUT  /api/workflows/{id}                DELETE /api/workflows/{id}
POST /api/workflows/{id}/enable         POST /api/workflows/{id}/disable
POST /api/workflows/{id}/run            GET  /api/workflows/{id}/runs
GET  /api/workflows/{id}/runs/{run}     POST /api/workflows/{id}/runs/{run}/retry
GET  /api/workflows/lists               POST /api/workflows/lists
POST /api/workflows/suppressions        DELETE /api/workflows/suppressions
PUT  /api/workflows/limits/{account}    (`biz-a~mail` for `biz-a/mail`)
GET  /api/workflows/leads               DELETE /api/workflows/leads
GET  /api/workflows/leads/{batch}       POST /api/workflows/leads/{batch}
POST /api/workflows/leads/note          GET  /api/workflows/lists/{list}/leads
```

The `leads` routes are the owner's, signed in. A token is refused, whoever holds it, and so is a member.
A token reads a run as its mirror ("What leaves the node"). `POST /api/workflows/suppressions` answers
`{ suppressed: true }`, with `new` only for a person.

Tools: `workflow_save`, `workflow_list`, `workflow_get`, `workflow_run`, `workflow_runs`,
`workflow_retry`, `workflow_propose`, `workflow_discard`, `workflow_catalog`, `send_suppress`, `leads_propose`.

`workflow_discard` lets an agent take back a draft it saved, as if it never had: only one that was
never on, never proposed and never ran, every version saved by its own grant family. Nothing ran and no
person saw it, so nothing is lost and nobody has to clean up after an agent's mistake. Anything else a
person already saw or something already did, so only a person deletes it.

CLI: `openajar routines workflows apply <file>`, `export`, `list`, `run`, `runs`, `retry`, `enable`, `disable`,
`remove` (a person deletes any workflow; past runs keep their records), `watch`, `templates`, and `leads list`, `show`, `take`, `drop`, `export`, `erase`, `seen`. `apply` is idempotent, so a developer keeps the file in a repository. `openajar mail connect`
sets up a mailbox on the node, asking for its password hidden (docs/EMAIL.md).

**Mailboxes** are set up on the node only. A mailbox id looks like a lane (`harbor/mail`), so grants
and switches read it the same way. Its password is in the node's vault. Nothing about it but its id,
address and state crosses the channel. The cloud never holds it.

## Flows in git

The flows a workflow's action steps run (and the routines that post on their own) can live in a
repository, reviewed and tested like code, instead of being edited live on the node. Run these on
your computer, next to the repository. pull, diff and push go to the node through your connection
(`openajar connect`), with its agents' grant. validate and test need no node at all.

```
openajar flow pull flows/ --workspace acme     # the node's flows, one YAML file each
$EDITOR flows/acme/acme.linkedin.post.yaml
openajar flow validate flows/                  # the node's own check, offline
openajar flow test flows/                      # each flow on its recordings, offline
openajar flow diff flows/                      # what would change on the node
openajar flow push flows/                      # the diff, a yes, then flow_save
```

`openajar flow` is `openajar flows` (also `openajar routines flows`). The commands take `--node NAME`
for one connection of several, or `--url http://127.0.0.1:7450` with `$OPENAJAR_TOKEN` (a dev node,
a CI job with its own grant).

**Layout.** `flows/<workspace>/<flow id>.yaml`: the workspace is the lane's (`acme/linkedin` goes in
`acme/`), and a flow on a lane pattern (`*/instagram`) goes in `_any/`. Its tests sit next to it as
`<flow id>.test.yaml`, and their recordings as `<name>.recording.yaml`. validate refuses a file named
after another flow or in another workspace's folder, and two files of one flow.

**The file** is the definition flow_save takes, in YAML, with the values that are only defaults left
out (each step keeps its `type`; `id`, `lane` and `effect` are always there). A file reads back as the
same flow, with the same version (the content hash), and the same flow always renders to the same
bytes. So pull writes only a file whose flow differs from the node's (comments and your own layout
survive it), a pull right after a push changes nothing, and diff compares flows, not text. Where a
flow differs, the node wins, as a checkout does: run diff first when you have edits not pushed yet.
A file that does not read as a flow (half an edit) is never overwritten.
[examples/flows/acme/acme.linkedin.post.yaml](../examples/flows/acme/acme.linkedin.post.yaml) is a
whole one:

```yaml
id: acme.linkedin.post
title: Post on the Acme Bakery company page
lane: acme/linkedin
effect: commit
params:
  type: object
  required: [text]
  properties:
    text: {type: string, maxLength: 3000, show: true}
steps:
- type: navigate
  url: https://www.linkedin.example/company/104857/admin/page-posts/published/
- type: assert_identity
  acting_as: li_company
  source: url
  pattern: ^https://www\.linkedin\.example/company/([0-9]+)/admin/
  expect: '104857'
- type: click
  locator: {role: button, name: Start a post}
# ... fill the editor, press Post (writes: true), read the posts list back
```

**Push** validates every file first (one error and nothing is saved), shows the diff, asks (`--yes`
does not), then saves each new or changed flow with flow_save, one after the other. It is an agent's
save under the connection's grant and the normal policy: the grant must reach the flow's lane, a
save that waits for a person says so (`--wait SECONDS` waits for the yes, then saves), and an agent's
version never lowers a flow below its steps (`"publishes": false` counts only when a person saves
it, so a pushed flow with a click reads as commit). Push never removes a flow, and diff lists a flow
only the node has. "Ask me first" stays a switch on the node, not a line in the file.

**No secrets.** A flow never holds one: a login comes from the vault, through the lane. pull refuses
(and names) a flow that seems to hold one, and validate refuses the file: a literal typed into a
password field, a default for a param named like a credential, or a value the node masked as a vault
value (`[redacted]`).

**Tests** run the real flow executor against a recording, with no browser, account or node. A
recording is what the pages showed, one state after another: the URL, the accessibility outline
(what browser_snapshot returns, without its refs) and, for a state an action led to, that action:

```yaml
format: 1
pages:
- url: https://www.linkedin.example/company/104857/admin/page-posts/published/
  outline: |
    - main:
      - button "Start a post"
      - region "Page posts":
        - article:
          - paragraph: Open this Sunday from 8 to 14.
- url: https://www.linkedin.example/company/104857/admin/page-posts/published/
  after: click button "Start a post"
  outline: |
    - dialog "Create post modal":
      - textbox "Text editor for creating content"
      - button "Post"
```

The replay serves those states through the FlowDriver the dev node uses, so every locator resolves
under the same rules a browser lane follows (role, name, `within`, `nth`, attributes from `[...]`
and `/url:`), and a locator the page does not have fails the test as it would fail the run. A
navigate shows the next state loaded at that URL; a click or a press shows the next state recorded
after that action (before the next page load), or loads a link's URL; typed text stays in the page,
so a value readback reads it. `${name}` in an outline is the test's param, so a recorded post shows
each case's own text. A test file names the recording, the params, and what must come out:

```yaml
cases:
- name: posts the text as the company page
  recording: company-page.recording.yaml
  params: {text: Fresh rye is out of the oven. Come by before noon!}
  expect:
    status: succeeded_verified
    ran: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]          # the steps that ran to done
    published:                                   # each press while the page held text the run entered
    - pressed: button "Post"
      fields: {textbox "Text editor for creating content": Fresh rye is out of the oven. Come by before noon!}
- name: stops before posting when the Post button is gone
  recording: company-page-redesign.recording.yaml
  params: {text: Fresh rye is out of the oven. Come by before noon!}
  expect: {status: partial, failed_step: 7, error: element_not_found, published: []}
```

`openajar flow test` prints PASS or FAIL for each case with what differed (the failing step, its
locator, expected and found) and exits 1 on a failure; `--json` for CI. The example above is
[examples/flows](../examples/flows), and `tests/unit/test_flow_replay.py` keeps it passing.

**Recordings** of your own pages come from the lane itself, read only:

```
openajar flow capture flows/acme/company-page.recording.yaml --on acme/linkedin \
    --goto https://www.linkedin.com/company/104857/admin/page-posts/published/ -p text="Fresh rye"
# do the next action yourself, in the live view (openajar open acme/linkedin) or with
# openajar call browser_act --on acme/linkedin --arg ref=e12 --arg action=click
openajar flow capture flows/acme/company-page.recording.yaml --on acme/linkedin --after 'click button "Start a post"'
```

capture reads the page with browser_snapshot (and loads `--goto` first with browser_navigate),
keeps the outline without refs, layout boxes or hints, and writes `${text}` where the page shows a
`-p` value. It never clicks or types, so recording a post means posting it yourself once (or writing
the after state by hand, as the example's are). A recording holds what the page showed: read it
before you commit it. The node has already masked vault values in it.

**Workflows** keep their own files already: `openajar workflows export` and `apply` (idempotent), on
the node. Schedules are not files yet.

**openajar call.** Any tool, from a shell, as your agents would call it (the same grant, policy and
approvals): `openajar call flow_get --arg name=acme.linkedin.post`, `openajar call flows_list`,
`openajar call browser_snapshot --on acme/linkedin`. `--json '{...}'` (or `@file`) passes the
arguments whole. A call that waits for a person exits 2; `--wait SECONDS` waits for the decision
and, once approved, calls again with the same arguments.

## The node channel

All additive under v1. A node that has these says `"workflows"` in `hello.supports`. One that does
not answers `unsupported`, and the panel sets `steps_supported: false`.

**Node to controller**

- The `inventory` report: each workflow is an entry in `flows`, with the fields above.
- `workflows_catalog` report (`event`, kind `node`): the `RoutineCatalog`, on change.
- `event`, kind `workflow_run`: `{ run: RoutineRun }`, needs `ack`. Sent when a run or a step changes
  state. It is the mirror ("What leaves the node"): metadata, with a `withheld` mark where a value was,
  for every trigger.

**Controller to node**

| type | body | answer |
|---|---|---|
| `workflow` | `{ ts, nonce, action, id?, definition?, version?, run?, values?, force?, by?, sig }` | `workflow_result { re, ok, flow?, run? (the mirror), secret?, error? }` |
| `hook_relay` | `{ token, body_b64, headers }` | `hook_relay_result { re, status, body }` |
| `unsubscribe_relay` | `{ token }` | `unsubscribe_relay_result { re, ok }` |

`workflow` is a person's action, signed as `"workflow"` exactly as `ask_first` is signed, with the
same `ts` and `nonce` rules. `action` is `save`, `enable`, `disable`, `delete`, `run` or `retry`. `by`
(80 characters kept) names the person, and the node records `controller:<by>`. A controller sends
only what a signed-in person did within their role. An agent working through the controller uses the
tools over `invoke`, which carry its grant.

`hook_relay` needs no controller signature: the sender's own signature is what is checked, and it is
checked on the node. `headers` holds only `x-agentos-signature`, `x-agentos-timestamp` and
`x-agentos-delivery`. `body_b64` is the raw body, at most 512 KB of bytes. The controller answers the
sender with `status`. An offline node answers 503, and the sender retries with a fresh timestamp, so
the controller stores nothing. `hook_relay_result` may carry `retry_after` (seconds).

**Limits on the public routes.** `/hooks` and `POST /u` need no sign-in, so both ends limit them.
Over a limit is `429` with `Retry-After` and an `error` body with code `rate_limited`, at once: nothing
waits in line.

| Where | Limit, per minute | Counted |
|---|---|---|
| controller, per source (the peer address) | 60 | first, before anything is read, so made-up tokens count |
| controller, per token | 300 | after the token's shape and the node being online |
| controller, per node | 1200 | with the token |
| controller, relays in flight per node | 16 | the body is read inside this |
| node, all relays | 1200 | before anything is decoded |
| node, per token | 300 | after the signature, so a stranger cannot use up a sender's allowance |
| node, waiting and not started per workflow | 500 | after the signature, retry in 30 s |

A body over 512 KB is `413`, from its `Content-Length` before it is read, or as soon as it passes the cap.
One that takes over 10 seconds to arrive is `408`. A malformed token is `404`. The hosted cloud applies
the same limits in front of its relay, and a node does not depend on it: its own hold whoever relays.
Behind a reverse proxy every source is the proxy, so there the token and node limits are what hold.
`X-Forwarded-For` is not read, because a sender can write it.

`unsubscribe_relay` carries a token the node made. One it does not know does nothing, and the person
sees the same page. A `GET` of the link only offers a button, because mail scanners follow links; the
button, or a mail client's one click, is the `POST` that relays it.

## Billing

Billing is never per run. Nothing here counts runs for a bill.
