# Email

The contract for email as a built-in: connect a mailbox in a minute, send, see what is new, and use all of it in
routines. It sits on `docs/WORKFLOWS.md` ("Sending to people", the `email` trigger), which still holds every
anti-spam rule. The core wins, and the hosted edition's contract mirrors this file.

Mailboxes, `mail.send`, the `email` trigger, unsubscribe, suppression and the daily limit exist. This adds easy
setup, reading, a way for leads to reach a list, and tests against a real mail server.

## Connecting a mailbox

A person gives an address and a password. openajar does the rest.

```
openajar mail connect you@example.com          # asks for the password, hidden
```

1. **Find the servers** from the address (below). Nothing secret leaves yet.
2. **Show what it found**: `imap.example.com:993`, `smtp.example.com:465`, and where that came from. A person can
   correct it. The password is not sent anywhere until then.
3. **Sign in** to both, over verified TLS: IMAP login and a SMTP login. Nothing is sent and nothing is read.
4. **Save**: the password goes to the vault, bound to those two hosts, and the mailbox to the store. A failure says
   which of the two failed and why, in plain words, and saves nothing.

**Where the password goes.** Straight to the node: the node's CLI, the node's own panel, or a short-lived form the
node serves for this one job. An agent never sees the password, and there is no tool argument for one. The hosted
panel collects no third-party password. It shows how to connect, and shows the result.

**An agent asks, and a person connects.** `mail_connect` puts a card in Needs you that names the address, and returns
no link and no token: its answer says a person must connect it. It cannot name a mailbox that exists. The link exists
only for a person: the owner signed in to the node's own panel opens the card there (`POST
/api/mail/requests/{id}/link`), or runs `openajar mail connect`. A card reaches the cloud too, so it holds no token and
no link, only the one line to run at the computer.

**Only a person changes a mailbox.** A token proves someone holds a link, not that a person is there, so the form can
add a mailbox and never change or replace one. That takes the owner in person, in the node's panel or at the CLI, and
a different address on the same id still takes removing it first.

**A yes is to what was shown.** `POST /api/mail/discover` gives a `digest` of exactly the hosts, ports and TLS it found,
and connecting takes that digest and uses those hosts. It never looks again, so a yes to one answer cannot cover
another, and anything else is refused with `mail_confirm_hosts`. That goes for every result, https ones too, and a
person is shown each one. A job holds the hosts it showed in the same way.

**Gmail comes first.** It connects with an app password in place of the normal one. Discovery knows this and asks for
the right thing up front, with a link to the provider's own page, instead of failing after. iCloud is a later job:

- **Gmail:** an app password from `myaccount.google.com/apppasswords`. The account needs 2-Step Verification on, and
  Advanced Protection blocks app passwords. Workspace admins can block IMAP. `imap.gmail.com:993`, `smtp.gmail.com`.
- **iCloud (to do):** one app-specific password from `account.apple.com`, under Sign-In and Security, which needs two-factor
  authentication. `imap.mail.me.com:993`, `smtp.mail.me.com:587` with required STARTTLS. The IMAP username can be
  the part before the `@`, so connecting tries that, then the full address. Changing the Apple Account password
  revokes app-specific passwords, so that mailbox then says plainly that it needs a new one.
- **Yahoo** also wants an app password. **Microsoft 365** often turns password sign-in off, which needs OAuth. OAuth is
  later, and Gmail's app password does not wait for it.

## Finding the servers

In order, stopping at the first that gives both IMAP and SMTP:

| Step | Source | Trust |
|---|---|---|
| 1 | a built-in table of large providers, by domain and by MX | ours |
| 2 | Thunderbird's ISPDB, `https://autoconfig.thunderbird.net/v1.1/<domain>` | https |
| 3 | the domain's own `https://autoconfig.<domain>/mail/config-v1.1.xml` and `https://<domain>/.well-known/autoconfig/mail/config-v1.1.xml` | https, verified |
| 4 | the ISPDB for the domain of the MX host (a domain on Google or Microsoft mail) | https |
| 5 | SRV records: `_imaps._tcp`, `_submissions._tcp`, `_submission._tcp` (RFC 6186, RFC 8314) | low |
| 6 | trying `imap.`, `mail.`, `smtp.` on the domain, for a TLS answer | low |

- **The password never goes to a host only DNS named until a person saw it.** A low trust result is always shown,
  and the login waits for a yes. A spoofed record must not be able to collect a password.
- Plain `http` is never fetched, and a config asking for no TLS or STARTTLS-optional is refused. Always TLS,
  always a verified certificate, on implicit TLS or required STARTTLS.
- Hosts that resolve to private, loopback or link-local addresses are refused, unless a person typed them.
- Fetches have a timeout, a size cap, and follow no redirect off https. XML is parsed with entity expansion off.
- The domain is the only thing sent to a third party (ISPDB). The address and password are not.
- A step that goes wrong is skipped and the next one runs, whatever a hostile answer made it raise. The fetch tries
  every address a name gave. Each read has a deadline of its own, so nothing stays running after a step gave up.
- A config picks only which form of the address is the username (`%EMAILADDRESS%` or `%EMAILLOCALPART%`). Anything else
  it writes there is ignored, and what a config or DNS said is labelled `untrusted_fields` for whoever reads it.
- IMAP and SMTP are not pinned to the address that was checked: the verified certificate is what says it is the right
  server. Only the https fetch pins, because it has no name a person chose.

## Polling

A poll has a hard deadline of 60 seconds, checked between operations and by socket deadlines that do not start again
on each byte, so a server that trickles cannot hold one open. IMAP literals and response lines, untagged lines, and
multi-line SMTP replies are capped. A server past a cap or the deadline fails that one poll, says so in the mailbox's
health, and the next poll tries again. Past half the deadline a poll starts no new message and keeps what it read, so a
slow server still moves forward. One poll runs at a time per mailbox, beside the scheduler and never in its tick.

## Reading

| Tool | Effect | What |
|---|---|---|
| `mail_list(mailbox, unread?, since?, limit?)` | read | headers and a short clip, newest first |
| `mail_read(mailbox, id)` | read | one message, as text |
| `mail_mark(mailbox, id, read)` | draft | marks it read or unread on the server |

- Every field that came from a stranger is untrusted: sender, subject, body, attachment names. Reading labels the
  family, so what an agent does next asks first, by the rules in ROUTING.md.
- HTML is reduced to text on the node. Nothing is fetched from a message, no remote image, no link.
- Attachments are listed and not opened. A person or an agent can save one to the lane's inbox, where it is a
  file like any other.
- Nothing here reaches the cloud. A mailbox's content stays on the node, as `docs/WORKFLOWS.md` says of a trigger's.
- **No single message stops a mailbox.** One that cannot be read (too deeply nested, too slow to parse on purpose, or
  anything else a parser cannot get through) is a stub marked `unreadable`, with what its headers said. The poll moves
  past it and starts nothing from it, and it counts once in the mailbox's health as `unreadable`.

## Sending

`mail_send(mailbox, to, subject, body, reply_to_id?)` and the `mail.send` step are one operation.

- A reply to a message in the mailbox, or a message to one person who is not on a list, is a `commit` like any post,
  so it runs on its own unless Ask me first says otherwise, and after untrusted text it asks.
- **Sending to many goes through a list**, with everything in "Sending to people": suppression, the ledger, the
  daily limit, and the unsubscribe the node adds. Sending to the same address twice in a day, or to more than 10
  addresses in a day outside a list, counts as sending to many and is refused with `use_a_list`, so the list rules
  cannot be walked around one message at a time.
- **A reply is a conversation.** Answering someone in a real thread, twice in a day or twenty times, does not count
  toward `use_a_list`, and it carries no unsubscribe footer. A reply here means `reply_to_id` names a message that
  really is in this mailbox, from this exact recipient, written by a person (not an automatic answer, and not one that
  could not be read), and the send keeps its thread headers. The node checks all of it from the message itself, and
  no flag an agent sets can ask for it. Anything else counts as before and keeps the footer, and a list send always
  does. Suppression still applies to a reply, and a reply still counts toward the daily limit. A `From` can be forged,
  so the daily limit is what bounds a reply to someone who never wrote.
- A send from a mailbox counts against that mailbox's limit, whichever path it took.
- **One canonical address** is looked up, counted, compared, stored and sent: a plain addr-spec only (no comment,
  display name, quoted local part, route, whitespace or control character), lowercase, with an IDNA domain. The server
  is told that address outright and no header is read back. A `+tag` is left off for suppression, the daily rules and
  the unsubscribe token, so `jane+x@acme.com` is Jane to every rule, and the mail goes to the address as given.
- **What certainly did not leave is never in doubt.** Any failure before the message is handed to the server is
  `mail_not_sent`, safe to try again. A sealed vault, a password that was rejected, or a server the node cannot sign in
  to stops a send step at once, says so plainly (`vault_sealed`: unlock the vault) and spends nothing. A retry reaches
  everyone it did not reach.
- Sent mail goes in the mailbox's Sent folder, so the person's own mail client shows it. Gmail files what its SMTP
  sent by itself, so no copy is added there, and elsewhere a copy with that Message-ID already in Sent is not doubled.
  A copy that fails changes nothing: by then the message has left.

## Leads

A lead is a person someone might write to, found on a site or given by the owner. A list stays what
`docs/WORKFLOWS.md` says: only a person adds to it. Leads are how an agent proposes.

`leads_propose(list, leads: [{email, name?, company?, source, why?}])` puts them in a list's review queue. A card
says how many and from where, and a person takes them all, or picks, or drops them. Then they are on the
list and the routine sends.

- **A person says yes to each batch**, never each address, so finding fifty leads costs one look. An address that
  came from a page is text a stranger wrote, and a person reading the batch is what stops a page from adding one.
- Each lead keeps its `source`: the URL or name it came from, and when, so the owner can say where an address came
  from, which the law may ask of them. Suppressed addresses never enter the queue.
- A lead with no public business address is not guessed. No pattern guessing, no verification by probing a mail
  server, no buying or importing of bulk lists through this path.
- `send` keeps every limit: one sending account, 200 a day by default, a real unsubscribe, a real sender.

These are outside it, in the same way as in `docs/WORKFLOWS.md`: rotating mailboxes or senders to get past a limit
or a spam filter, hiding who sent it, and reading a site harder than a person would. The lane's pace and holds apply
on whichever site a lead came from. Getting a lead from LinkedIn runs on the owner's own account, at the account's
pace, and a challenge stops it.

The owner is the sender and answers for it. Rules on unsolicited mail differ by country. In the EU, writing to a
named person can need their consent, and to a business less so. The panel says this once, plainly, when a first list
of leads is made. openajar gives the tools to do it properly and does not decide it.

## New mail in routines

The `email` trigger exists. It gains `from?`, `subject?` and `unseen_only?` filters, all checked on the node, and
fields `from`, `subject`, `body`, `id`, all untrusted, as a `webhook`'s are. A reply to something a routine sent
carries `in_reply_to_run`, so a routine can stop chasing someone who answered: a built-in step, `stop_if_replied`.

Templates: "When someone replies, tell me", "Follow up once if no reply in 3 days", "Send this to my list".

## Owner API, CLI and tools

`POST /api/mail/discover {address}`, `POST /api/mail/connect {id, address, password, digest?, imap?, smtp?, confirmed?}`
(node only: the owner signed in to the node's own panel, and any token is refused, so a controller has no way in. It
takes a `digest`, or both servers typed), `GET /api/mail/requests` and `POST /api/mail/requests/{id}/link` (what agents
asked for, and the link for one card, also node only), `GET /api/mail`, `DELETE /api/mail/{id}`, `GET
/api/mail/{id}/messages`, `GET` and `POST /api/mail/form/{token}` (the short-lived form: its token is its auth, it
ends when it is used, it serves only https or this computer, and it never changes a mailbox that exists), and `openajar
mail connect|list|test|remove`. Connecting is the only way in: there is no `mail add`. An `{id}` here is written
`biz-a~mail` for `biz-a/mail`.
Agent tools: `mail_connect`, `mail_list`, `mail_read`, `mail_mark`, `mail_send`, `leads_propose`.

## The cloud

Metadata only, as today: a mailbox's address and health, list sizes, send counts. No message, no password, no lead's
address. A hosted person sees a mailbox and its health, and connects it on the node.
