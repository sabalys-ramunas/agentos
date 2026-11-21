# Confirmations

The contract for how a person says yes: when openajar asks, how strong the yes must be, and where it can be
given. It is additive under `docs/PROTOCOL.md` v1 and sits on `src/openajar/policy/ROUTING.md`, which still
decides whether a call asks. The core wins, and the hosted edition's contract mirrors this file.

The goal of openajar is automation, so most things never ask. This file is about the few that do, and about
adding a new place to say yes (an iPhone, a watch, an authenticator) without touching the rules.

## Three separate questions

| Question | Answer lives in | Example |
|---|---|---|
| Does this ask at all? | routing, Ask me first, the overlays | a post runs on its own; a payment asks |
| How strong must the yes be? | the **level** the setting asks for | a payment needs a person proved just now |
| Where can it be given? | the **methods** a person has | the panel, chat, a Mac, later a watch |

A rule names a level, never a device. A method declares the level it reaches. So a new device is a new method,
and no rule changes.

Inside an agent's own environment (docs/ENVIRONMENTS.md) the first answer is always no: opening one, its
commands, its browser and its pushes never ask, before or after untrusted text (the owner's decision of
2026-11-13). The node's limits (`environment_limit`) and the git gateway's refusals (`push_refused`) bound it
instead, so nothing in this file applies there. What the same agent does on an account lane asks as usual.

## Levels

Ordered, weakest first. A yes counts when its level is at or above what the approval requires.

| Level | Means | Reached by |
|---|---|---|
| `chat` | the person answered a prompt their chat client showed them. The model did not answer it, but the proof is only as good as that client | MCP elicitation |
| `signed_in` | a person in a signed-in session | the panel, the node's CLI, a member with a recent check. Not the device credential `openajar connect` keeps on the owner's computer: an agent there can read it (docs/THREAT-MODEL.md) |
| `present` | a person proved they are here, just now, for this approval | a fresh passkey check; later a device password or Touch ID behind a device key, or an authenticator code |
| `device_key` | a key held on a device signed this exact content | later: Secure Enclave on a Mac or iPhone, Android keystore, a watch |

`device_key` covers `present`, because the device asks for the person before it signs. A device password is an
acceptable way to unlock the same key where there is no fingerprint reader: the proof the node checks is the
signature, so it stays the same level.

An agent's word is not a level. No token, no grant and no tool argument can say "the person said yes", and
`self_approval` stays exactly as it is.

## The setting

One setting, with a system default, read by the routing that already exists. It is not a second mechanism:
`commit.when` is `ask_first_default`, and the per-routine and per-account switches of Ask me first go on working
above it.

```toml
[confirmation]
commit       = { when = "auto", level = "chat" }
pay          = { when = "ask",  level = "present" }
irreversible = { when = "ask",  level = "present" }
after_untrusted = { level = "signed_in" }
methods_off  = []            # ways to confirm turned off; only "chat" and "telegram" can be
controller_max_level = "present"
```

| Row | Default | Who may change it |
|---|---|---|
| `read`, `draft` | run on their own, not in the setting | nobody |
| `commit` | `auto`; when something does ask, `chat` is enough | owner or admin: `when` and `level` |
| `pay`, `irreversible` | `ask`, at `present` | owner: `level` up or down to `signed_in`. `when` stays `ask`: it is a floor, not a default a setting can lower |
| `after_untrusted` | `signed_in`: the same for the trifecta overlay | owner: level, never below `signed_in`. Not below what `policy.toml` says: lower it there. An admin never lowers it, and a controller can only raise it, through its layer |
| anything else that asks (a command, an install, a service, metered spend, owner-trust memory, a question) | `signed_in`: what a member needs for it today | nobody |
| `methods_off` | empty | owner or admin turn `chat` or `telegram` off; only the owner turns one back on. `panel`, `cli` and `passkey` cannot be turned off, so a person can always decide |
| `controller_max_level` | `present` | owner, on the node. A controller never sets it |

- **What this replaces.** Today a member's approve needs a recent passkey check and `pay` needs a fresh one. Those
  become the `signed_in` and `present` levels with the same windows (15 minutes, and two minutes used once). The
  rules do not get weaker. The windows are for a member's session and the owner's alike: an owner's session is
  `signed_in`, so what requires `present` now asks the owner for the same fresh check, where before it asked
  nothing of them. With no passkey, the owner adds one or sets `pay` and `irreversible` to `signed_in`.
- **Nothing can lock a node out.** `policy.toml` refuses to load when it turns off a method that cannot be turned
  off, and the node refuses to start, saying which row, when a level in it is one no ready method reaches. A person's
  change to such a level is refused, and a controller's is left out of its layer.
- **`commit` needing only `chat` lowers nothing.** A member still needs their recent check to reach `signed_in` in
  the panel, whatever the approval requires. `chat` adds a place to answer; it takes nothing from the others.
- **Levels only rise down the chain.** Node, workspace, routine or account: the strictest level that applies wins.
  An overlay can raise what a row says. Nothing lowers `after_untrusted` for a routine.
- **A floor for `chat`.** An approval that follows untrusted text, or names `pay` or `irreversible`, never takes
  `chat`, whatever the setting says. Those are the cases where a hijacked agent is the threat, and a chat client
  is the same place it lives. "Follows untrusted text" reads the session, not `[taint] downgrade`, so emptying that
  list opens nothing. An agent's proposal that acts as the owner once made (a trigger) never takes `chat` either.
- **One exception to it, by the owner's decision: a publish the commit gate caught.** To publish, an agent reads the
  page, so every real publish follows untrusted text, and the floor alone would mean chat never answers one. The
  owner decided a publish can be approved on the card or in chat, whichever answers first. It holds because the
  prompt shows what the card shows: the normalized request the page was about to send, which the approval binds, so
  a person sees exactly what leaves. It is its own switch, `publish_in_chat`, **off by default** (it was on until
  2026-11-07), the owner's alone, and a controller can only turn it off. It does not reach a call that is not a
  publish the gate caught, the trifecta, a raised `commit.level`, or an `after_untrusted` an owner or a workspace
  raised. The cost is why it is off: the chat client is the agent's own, and an agent with a shell can open an MCP
  session of its own with its token and answer its own prompt (see Chat). Binding the answer to the session that
  asked does not help, because such an agent can ask from that session too. So with it on, what a page talked such
  an agent into can go out with no person. Turn it on (`openajar confirmation set publish_in_chat on`, or the panel)
  only when no agent of yours has a shell on the machine that holds its token.
- **Standing approvals hold to what a decision holds to.** Making one needs a person (`signed_in` in the panel, and
  a member's recent passkey check), never a token or the device credential. Made from approvals ("the same again"),
  each must already be approved or used, be for the standing approval's own lane, and, for a member, be one their
  role could approve itself (four-eyes included). So a standing approval is never a way round deciding.
- **Who can change the setting.** A person, signed in. An agent never, a bearer token never (the node token
  included), and the owner computer's device credential never. A controller changes none of it: it has a layer of
  its own (The node channel).

## Methods

A method is a row in a registry. It says what it is and what level it reaches. Code asks the registry; it does not
name devices.

```python
class ConfirmMethod(Protocol):
    id: str                  # "panel", "cli", "chat", "telegram", "passkey", "mac", "ios", "android", "watch", "totp"
    label: str               # what a person reads
    level: ConfirmLevel      # the highest level this method can reach
    status: Literal["ready", "planned"]
    def available(self, person: Person) -> bool: ...
    def deliver(self, approval: Approval, person: Person) -> Delivery: ...   # how it reaches them
    def verify(self, approval: Approval, proof: Proof) -> Verified: ...      # proof -> level, or refused
```

| id | Level | Status |
|---|---|---|
| `panel` | `signed_in` | ready |
| `cli` | `signed_in` | ready |
| `passkey` | `present` | ready |
| `chat` | `chat` | ready |
| `telegram` | `chat` | ready, for the owner, once a Telegram account is paired |
| `mac`, `ios`, `android`, `watch` | `device_key` | planned |
| `totp` | `present` | planned |

A `planned` method is listed and refused: `method_unavailable`. Adding one means a class, a registry row and
tests. The level table, the setting and every decide route stay as they are.

A **proof** is what a decision carries: `{method, level, at, detail?}`. The node works out `level` from how the
decision arrived. A caller never sets it. `detail` holds nothing secret and no content. `device_key` proofs also
carry `{device, signature}` over the approval's content digest, tied to the approval's id so that a signature cannot
be replayed on another approval holding the same content, and the node holds the device's public key.

**Devices.** A person's device is a row: `{id, person, method, public_key, label, added_at, last_used_at,
revoked_at}`. The table exists from the start, so a device added later needs no migration of approvals. Enrolling
one is the job of a future method. A private key never leaves a device, and neither the node nor the cloud can
sign for it.

## Chat

For people with no Mac or iPhone, and for anyone already talking to an agent. The agent gets `approval_pending`
as it does today, then calls `approval_confirm(approval_id)`.

1. **The client can show a prompt.** The node sends an MCP elicitation with the node's own words, the exact
   content and a yes and a no. The answer goes from the client to the node. The model never sees the question as
   something to answer, and has no argument it could fill in. A yes reaches the approval as `chat`.
   A publish the commit gate caught shows what its card shows instead of the raw content: warnings first (the
   account, lookalike names, invisible characters, text cut for display), where it goes, who is signed in now, the
   text, target, every link in full, media and every other field. The labels are the node's, and everything that came
   from the page is quoted, the names inside a warning too. Whichever of the card and the chat answers first wins, and
   the other goes away: a chat yes leaves the pending list, and the gate's retry with the same arguments goes through.
2. **It cannot, or the approval needs more than `chat`.** The tool returns a review link for the agent to show. It
   opens the approval in the panel, signed in, and the person decides there, with whatever level it needs. So a
   payment can still start in chat and needs no Mac, no iPhone and no app.

- A link never approves (SEC-8). It only opens the page.
- **Clients.** A client on a session protocol is asked on the open call. A client on the 2026-07-28 protocol has no way
  back on a call, so it gets the prompt as a required input and sends the call again with the answer, in state the
  node sealed. Neither passes through the model. Either way the answer is the client's word: nothing here tells a
  person from a client that answers by itself.
- **A yes is bound.** Its value is worked out from this prompt and this content, and worked out again from the stored
  approval when the answer comes, so an old yes or a yes for other content is no answer. Each prompt is spent once.
  The store checks the content again in the transaction that decides.
- **What chat will not carry.** A question (its words are the agent's), content the prompt could not show whole (cut,
  a value withheld, an image), an agent asking for another family, a session that has read untrusted text since
  the approval was made, or an approval in a workspace that has turned chat off, even one already waiting. Each gets
  the review link.
- **A send card too long for its preview** is approved only with every message read: the panel's "show all"
  pages through them (`GET /api/approvals/{id}/messages`), and the approve carries the `seen` its last page
  gave. Every other path (chat, a batch, a relayed yes, a token) is refused with `see_whole_card`
  (docs/WORKFLOWS.md, Sending).
- **Nagging has a limit.** One prompt open per approval, a pause between prompts and three in all, and ten prompts in
  an hour for a whole grant family, because new wording is a new approval. After either limit it is the review link.
  The counts live in the daemon's memory, so a restart starts them again. A yes faster than a person can read is not
  counted: that catches a client set to accept everything, and only that.
- Every approval that can be answered in chat says so in `approval_pending`, so an agent knows to offer it.
- A client that answers elicitations by itself turns `chat` into nothing. So `chat` can be turned off in the setting,
  and the record of what was decided lists each client by the name it gave itself. That name is what the client said
  about itself. It proves nothing, and it is shown that way.
- **What chat does not stop.** Chat stops a tool-only agent. It does not stop an agent with a shell on the same machine
  as its token. Such an agent can read the token, open an MCP session of its own, answer its own prompt, and call
  itself anything. Nothing here can tell that from a person. It reaches only `commit` at `chat`, because everything
  else refuses chat, and `chat` is a convenience level. An owner whose agents have a shell should turn `chat` off or
  raise `commit.level`.
- Text the agent wrote never reaches the prompt. The node writes it from the approval, as it writes a card.
- `chat` is a place to decide what a person already wants. Denying from chat is always allowed.
- A chat yes is recorded as `chat`, a person's answer through a client: never as the agent, and never as "in person",
  so what only a person turns on (a workflow, a site API) does not read it as one. The person is the one behind the
  agent, so four-eyes and a member's limits hold as they do in the panel.

## Telegram

Approve from your phone, for an owner with no Mac or iPhone app: the node's own Telegram bot sends what waits for
the owner to the owner's private chat with it, with buttons. It is a method at the `chat` level and a channel of the
owner's pushes, beside ntfy and the webhook (quiet hours, digests and the rate limit hold for it as for them).

**What it can approve: exactly what chat can, and nothing more.** The same rule decides (`chat_refusal`), so a
question, `pay`, `irreversible`, anything that needs `signed_in` or more, anything after untrusted text (a publish
card aside, under `publish_in_chat`), and content the message could not show whole never get an Approve button.
They get Deny, and Review (a signed link that opens the panel) when the node has a public URL. It is held at the
moment of the tap too, not only by leaving the button out: the node checks again, and the store holds a `telegram`
proof to the same rule and to `chat`, so a forged or replayed tap approves nothing more. Also refused:

- an approval made before Telegram was paired, or while it was off (it was not among its `methods`);
- an approval a member's agent asked for (the paired account is the owner's), and one four-eyes keeps from the owner;
- an approval whose asking grant family has read untrusted text since. A tap has no agent session, so the whole
  family is asked, which is stricter than chat;
- a message whose text would pass Telegram's 4096-character limit: Approve is left out, as for a cut preview.

**Questions** from agents get the question and a Review link, and no answer buttons.

**Pairing, on the node, as the owner:**

```sh
openajar policy notify telegram set-token      # the token from @BotFather, asked for hidden (or --token-from -)
openajar policy notify telegram pair           # prints https://t.me/<bot>?start=<code>: once, for 10 minutes
openajar policy notify telegram status
openajar policy notify telegram remove
openajar confirmation set telegram off         # sends nothing more and takes no tap; "on" turns it back on
```

The node accepts `/start <code>` only in a private chat, from the account that chat belongs to, and keeps only a
hash of the code. Every later tap must come from that account, in that chat, on a message the node sent; anything
else is ignored and logged. Anyone can message a bot, so an ignored `/start` is logged (and answered) once per
sender every 10 minutes, not once per message. Pairing again replaces the account and tells the old chat so. The
node must be running for pairing: it reads the answer from Telegram.

**How a tap holds.** A button carries an opaque random id and nothing else. Each message's buttons are spent by the
first tap, work for 30 minutes (then: the panel), and are bound to the approval's content: if it changed since the
message was sent, the tap decides nothing and the message says so. An Approve faster than a second after the
message was sent is not counted. At most three messages for one approval offer Approve. A decision goes through the
same store as every other route, recorded as `decided_method: telegram`, `decided_level: chat`, `decided_via:
telegram`, by `telegram:owner`, never "in person", so freezes, audit and standing rules apply unchanged. When an
approval is decided anywhere (the panel, the CLI, chat, Telegram) or expires, its message changes to say so and
loses its buttons. When a send went out and no answer came back (a timeout), the message may be in the chat: its
buttons keep working, the first tap from the paired account tells the node which message it was, and it counts
toward the three. The notifier tries the send again, so the chat can then hold two messages for one approval.

**Transport.** The node only dials out: it long-polls `getUpdates` and keeps the update offset in `policy.db`, and
backs off when Telegram is down. One bot per node: a second program reading the same bot takes its updates.
`set-token` removes a webhook left on the bot (with one set, Telegram refuses every `getUpdates` with 409), and
`status` says "not reading" while Telegram answers 409. The
token and the paired chat and account live in `secrets/notifier.json` (mode 600). The token is part of every Bot
API address, so the node keeps it out of traces, logs, the outbox's errors and exception text.

**What Telegram sees.** The message is plain text (no formatting, so text from a page cannot add any), and it
holds the content in full, so Telegram's servers see the post. That is the price of the convenience. Use the panel
for anything you would not send through Telegram, or turn the method off.

**What it does not stop.** Anyone with a shell as the node's user can run the pairing commands and pair their
own account, as they could read the node's secrets. The node tells the owner when a pairing happens (a push, and a
message to the chat it replaces), and Activity records it.

## Approvals carry it

Additive fields, on the node and over the channel:

- `Approval.requires` (level), `Approval.methods` (ids that could reach it for this person, ready ones only).
- On a decision: `decided_method`, `decided_level`. `decided_via` stays for what reads it today.
- Refused with `proof_too_weak` (403) when an approve is below `requires`, with `requires` in the answer, so the
  panel can step up. Denying never needs a level. A `method_unavailable` is 403 too.
- **A decision that proves no person.** The panel API takes an approve or an answer only from a person
  (docs/THREAT-MODEL.md, "A person is present"): a bearer token (a grant, an admin grant, the node token) and the
  owner computer's device credential are refused with `presence_required` (403) before any level is worked out. They
  can still deny. A controller's relayed decision with no proof is no method and no level, so `decided_method` and
  `decided_level` stay empty and `decided_via` says what it was; it reaches at most `signed_in`, and never `present`.
  `self_approval` comes first, as ever.
- An approval made before this keeps what it needed then, and never takes `chat`.
- **A later change.** `requires` is fixed when an approval is made, so a setting changed later cannot lower one that
  waits. There is one exception: a person, signed in, on purpose. `openajar confirmation set ... --apply-to-waiting`, or
  `apply_to_waiting: true` on `PUT /api/confirmation`, works out `requires` and `methods` again for what waits, from
  the setting then in force. It never goes below a floor: a `pay` never takes `chat`, and what followed untrusted text
  or was held up by its creator stays held up, because the approval keeps those facts. An approval from before they
  were kept counts as following untrusted text. Activity says what moved. A controller can never do this.

## The node channel

Additive under v1, gated by `"confirmations"` in `hello.supports`. A node without it behaves as it does today.

- `approval` events carry `requires` and `methods`.
- A decision from the controller carries `proof: {method, level}`. The node accepts it up to a limit the owner set
  at the node (`[confirmation] controller_max_level`, default `present`), so a compromised cloud cannot claim a
  device signature. A `device_key` proof carries its signature, and the node checks it against its own record, for
  a person who may decide the approval.
- With no proof, a decision proves no person and reaches at most `signed_in`, so leaving it out gets past nothing. A
  controller cannot report a `chat` or `telegram` answer: those go to the node directly.
- `config_update` may carry `confirmation`: the controller's own layer, stored apart from the node's setting and
  replaced whole by each one. What is in force is the stricter of the two, row by row: `commit.when` is `ask` if
  either says `ask`, a level is the higher of the two, `methods_off` is the union, and `workspaces` adds to what
  the node holds for each. So a controller can only ever ask for more. Nothing it sends can lower or undo what the
  node's owner set, or what `policy.toml` says, and a lower value is not refused: it simply has no effect. It can
  take back only what it asked for itself, because the next layer replaces the last.
- What cannot be read is left out of the layer and logged, and so is a level no ready method reaches, which would
  leave every approval waiting. `controller_max_level` is no part of the layer.
- `confirmation: null` drops the layer, and so does the node leaving that controller. A `config_update` without the
  key leaves it as it is.
- The cloud stores the setting, the methods and the device rows (public keys only), and no content beyond what it
  holds today.

## Not in this round

The seam only, by the owner's decision: a Mac, iPhone, Android, watch or authenticator method. No enrollment
routes, no push to a device, no signature verification beyond a tested interface with a fake device.
