# Session keepalive

Long-idle sessions go stale: a site that has not seen an account for weeks asks it to confirm it
is the owner ("we noticed unusual activity") the day it comes back, usually right when an agent is
about to post. Keepalive gives each idle lane a short, human-paced, read-only visit every few days,
inside its own waking hours, and tells the owner when a lane turns out to be signed out or
challenged before a post fails on it.

Code: `src/agentos/keepalive/` (planner, visit, store, service, CLI). Runtime service `keepalive`,
built after the health service. Status: built, tested on the dev fake lanes and site
(tests/unit/test_keepalive.py) and in a real Camoufox against the fixture site
(tests/node/test_keepalive_node.py); not yet run against a real account (see "Before turning it on").

## Off until the owner turns it on

`keepalive = false` in node.toml is the default, because a visit opens a real account unattended.

    agentos lane keepalive --node on           # the node switch (read again every round, no restart)
    agentos lane keepalive biz-a/instagram     # the next planned visit, the last ones, why not now
    agentos lane keepalive biz-a/instagram --off   # this lane opts out (--on opts back in)
    agentos lane keepalive biz-a/instagram --now   # one visit now, in the foreground

A lane's opt-out is a file in the lane's own directory (`lanes/<ws>/<name>/keepalive-off`), so it
survives losing keepalive.db. `agentos doctor` says whether it is on. `--now` works while the node switch is off and on an opted-out
lane (the owner asked in person), but never past a gate. The panel's lane window shows the next and
last visit (`GET /api/lanes/{ws}/{name}/keepalive`).

## When a lane is visited (planner.py)

- **Activity** is the latest of the lane's `last_used` (stamped by the lane manager's `using()` for
  every agent call, flow and connector run and brain), the last human input in its
  live view, and the last keepalive visit that counts: one that went well (`ok`, `yielded`), one cut
  at its time limit after the feed loaded signed in, or one agentosd did not live to finish. A failed
  attempt is not activity (not even the `last_used` the lane manager stamped for it). A lane never
  used counts from its creation.
- **Due** is activity plus a cadence drawn inside the site's range (Instagram 2 to 4 days, Facebook 3
  to 5, LinkedIn and Reddit 3 to 6, Medium and a lane's own site 4 to 7, X 3 to 5, WordPress and Google
  5 to 9), from a
  generator keyed by the lane's own seed and that activity: stable between rounds, different per lane.
  When the lane check saw when the session cookie expires, a day before that at the latest.
- **When** is the first moment from due inside the lane's waking hours, in its pinned time zone (wall
  clock hours, so a DST day keeps its local window): a persona drawn from the keepalive seed
  (agentos.keepalive.persona). Each day draws its own minute and
  second (never on the minute), moved at least 20 minutes from the last visit's time of day. A missed
  slot (the lane was busy, memory was short, agentosd was down) goes at once while the lane is awake,
  else 2 to 12 minutes (drawn) after its waking hours next begin, never at the next day's random slot.
- **Retry**: an attempt that did not count (it failed, was stopped, or was cut before the feed loaded)
  is tried again 30 to 90 minutes (drawn) after it ended, inside the waking hours. A visit is written
  as `started` before it begins; one agentosd did not live to finish becomes `interrupted` at the next
  start and counts as a visit, with at least 6 hours before another.
- **Skipped**: node switch off, lane opted out, no site, quarantined, frozen, a human in control, a
  hold or a stop, an agent driving the lane (its drive ends when control leaves the agent; `--now` too),
  a claim or callers queued, the lane already open (in use or used a moment ago), a bad
  last signal (challenge, checkpoint, a login page), a hold for a wrong account, or the lane check finds
  it signed out. Those last ones tell the owner once per episode instead, with the reason that fits
  (a lane a visit found signed out is held, and the next round calls it signed out too: no second
  notice). A signed-out mark clears once the lane check sees the lane signed in again; a challenged or
  wrong-account mark only with a visit that went well, which the hold allows only after a human handed
  the lane back. A round that finds no block never clears a mark.
- **Resources**: one visit at a time on the node, at least 10 to 25 minutes (drawn) between two (kept
  in keepalive.db, so a restart keeps the gap), and only while a warm slot is free and available memory
  stays above the floor with about 1 GB to spare (docs/design/stealth.md: a warm lane on a feed takes
  0.8 to 1 GB), checked again under the one-visit lock right before the lane opens (`--now` too).
  Otherwise it waits for a later round. It never makes the lane manager close another lane.
- **keepalive.db lost**: missing or unreadable while the switch is on, a round plans nothing, opens a
  new one rebuilt from the lanes' own records (the opt-out files; the lane manager's `last_used`), and
  waits a gap before the first visit.

## What a visit does (visit.py)

Per site (sites.py): Instagram `/`, Facebook `/`, LinkedIn `/feed/`, Reddit `/`, Medium `/`,
WordPress `wordpress.com/reader`, X `/home`, Google `myaccount.google.com`; a lane on another site, or
a blog that lists its own domain, opens its own first site's front page.

Open the page, look for 4 to 12 s, scroll 3 to 8 times further down the page (the wheel, toward an
element further down, 2 to 8 s apart), with a 40% chance open one post (a link whose path is one item
on that site: `/p/<id>/` on Instagram, `/r/<sub>/comments/<id>/` on Reddit, ...) by loading its
address, look for 6 to 20 s, maybe scroll once or twice, and go back. At most 5 minutes; a visit that
reaches it is recorded `cut`. The pacing of each input is the node's own (agentos.node.human). Then the
lane goes back to sleep if the visit woke it, nobody else is on it, a human neither has it nor holds
it, and no live view of it is open or was opened during the visit. A visit stops early, as a success,
when another caller starts waiting for the lane. A human who takes the lane over (or stops agents on
it) ends the visit at once, even in the middle of a look (`stopped`); before every step the visit
checks that control is still the agents'.

Recorded: a row in `keepalive/keepalive.db` (times, outcome, host, scrolls, opened item, whether the
feed loaded, the identity check, session before and after), a `keepalive` lane event, and a trace
(`keepalive.visit`, principal `node`, effect read). Outcomes: `ok`, `yielded`, `cut`, `signed_out`,
`challenged`, `wrong_account`, `stopped`, `failed`, and `started` / `interrupted` (above). The session cookies are read from the live browser before and after (the page cannot see that
read): `refreshed` when the expiry moved later, `kept` when still signed in, `signed_out` when the
session cookie is gone.

A page that shows a challenge, a checkpoint, a block, a rate limit or a login page (the node's own
signals, agentos.node.sites) ends the visit at once: the lane is marked, the hold policy holds it as for
any agent, and the owner gets one notification with the plain reason and the fix. A page signed in to
another account than the lane's pinned one (the identity check, AUD-12) ends it as `wrong_account`:
the lane is held for a human (`needs_human:wrong_account`) and the notification names the account seen
and the one the lane expects. A held lane is left open for the human (the idle reaper closes it later).

## How it stays read-only

1. It holds the lane as the node (principal kind `node`), so holds, a human in control, claims, the
   freeze and quarantine stop it exactly as they stop an agent.
2. The lane's egress guard is driven for the whole visit by a read-only grant (ceiling `read`), under
   the guard's effect route on every lane, strict or not (`acting(effects_route=True)`), classified as
   the gate classifies a drive. Page-sent requests the gate would hold are stopped before they leave and
   recorded: writes matching a rule (like, follow, comment, post, upload), and a PUT, PATCH, DELETE or
   POST that no rule names (seen receipts included: a visit does not send them). Reads a normal browser
   sends go as they would for a person. A visit never approves anything for the node. The route exists
   only while the visit runs, so browsing outside it is unaffected (while it exists, the browser does not
   store responses in its cache: routes.py). The lane's previous driver is put back afterwards, and a
   drive an agent had is bound again if the visit ended it. A visit whose route cannot be bound does not
   run.
3. The visit can only load pages on its site's hosts, scroll, and go back. It opens a post by loading
   the address of a link to one item on that site, through the guard's URL check; it never presses
   anything, so a Like nested inside a post link cannot be hit. A button, a text box or any other link
   is refused before anything is sent. Nothing is ever typed.
4. It stops before its next step once control of the lane is not the agents' (a take-over, a hold).

## Coordination with other work on the lane

Every agent call, flow and brain uses the lane through the lane manager, so it stamps `last_used`
and resets the keepalive timer; a lane kept busy is never visited. Keepalive's own visits act on
nothing (no likes, no follows, nothing counted against caps).

## Before turning it on (with a throwaway account first)

- One `--now` visit per site on a throwaway account through its real proxy, watched in the live view:
  the feed loads signed in, scrolling looks natural, the opened post is the one the link named, and the
  trace shows no refused write or commit (if it does, an effect rule matched a read: fix the rule).
- The session line after the visit: whether each site moves its session cookie's expiry on a visit
  (`refreshed`) or not (`kept`); the cadence may be lengthened for sites that only need to see the
  account now and then.
- Item patterns: that the post links on each site's current web client match `items` in sites.py
  (Facebook and LinkedIn change their markup often). A miss only means no post is opened.
- A deliberately signed-out throwaway lane: one notification, the lane marked, no second visit.
- A lane signed in to another throwaway account: held, one notification naming both accounts.
- Memory on the production VM with 2 warm lanes: a visit must wait, not evict.
