# Engagement: posting, likes, comments and follows as a system

agentos has its own engine for engagement. You (or your agent) write a **program** per account:
what to do and how much. The engine plans short sessions at random times in that account's waking
hours, keeps every action under hard caps that ramp up slowly, and reports what it planned. No
agent clicks anything. The full design is in
[design/engagement-engine.md](design/engagement-engine.md).

**Sessions are off until you turn them on.** By default the engine only draws plans and shows
them to you: it opens no browser, claims no lane and acts on no site. With `engage_sessions = true`
in node.toml, approved programs run their sessions at the planned times (see "How a session runs"
below). Today no operation is verified on a node: they exist but wait for a canary on a
throwaway account (see "Operations" below), so a session that plans one of them stops before
acting and holds the account until you clear it.

## A program

```yaml
program: biz-a-ig-coffee
surface: biz-a/instagram
risk: careful                 # careful (default), normal (only you can pick it), or off
timezone: Europe/Vilnius      # optional: the lane's own time zone is used when left out
phases:
  - kind: warm_up             # for new accounts; leave it out for accounts you have used for years
    days: 10
  - kind: steady
    daily: {like: 15, comment: 3, follow: 2, story_view: 10}
    targets:
      - {source: hashtags, values: [specialtycoffee, vilniuscoffee, latteart]}
      - {source: own_engagers, lookback_days: 14}
      - {source: own_feed}
    filters: {language: [lt, en], min_account_age_days: 90, max_following_ratio: 3}
comments: {mode: approve, topics: [coffee, cafe life], max_chars: 140}
community: {reply_to_comments: {mode: approve, within_hours: 6}}
```

Daily numbers are targets, not promises. Each day the engine draws a number around the target
(so "15 likes" is 9 one day and 19 another) and never goes over the cap. About one day in seven
is a quiet day, with nothing but replies on your own posts.

## Commands (on the node)

| Command | What it does |
|---|---|
| `agentos engage program draft <surface> <file>` | Check a program and save it as a draft. Nothing runs. |
| `agentos engage program show [<name or surface>]` | List programs, or show one program's versions and its YAML. |
| `agentos engage program approve <name> [--version N]` | Approve it. Only you can do this. |
| `agentos engage plan <surface> --dry-run [--days 7]` | Print the planned sessions and actions per day. |
| `agentos engage status [<surface>]` | Today's plan, the phase, caps and notices. |
| `agentos engage caps [<site>]` | The caps tables below. |
| `agentos engage cap <surface> <kind> <n>` | Raise or lower one daily cap, never above the hard ceiling. `--clear` goes back to the ramp. `profile_edit 0` stops profile edits at once. |
| `agentos engage run <surface> --dry-run [--day D] [--session N]` | Show what a day's sessions would do: each operation, the connector it uses, and why it could not run. Nothing is clicked. |
| `agentos engage run <surface> --now [--session N]` | Move the next session (or session N) to now. The node runs it within a minute while sessions are on. |
| `agentos engage holds <surface> [--all]` | What stopped the account, since when and until when. |
| `agentos engage hold clear <surface> [--kind K] [--id N]` | Clear the holds once you have checked the account. Only you can do this. |
| `agentos engage items <surface> [--status S]` | The account's items: waiting, in a session, waiting for your approval, used (with the run), failed, expired or withdrawn. |
| `agentos engage item withdraw <id>` | Take a waiting item back, so no session uses it. |
| `agentos engage drafts <surface> [--status S]` | The account's drafts: what the provider wrote, which one a session used, and the alternatives. |
| `agentos engage provider test [--surface S] [--kind K]` | Send the draft provider one request and print how many candidates came back and how long it took (never the text or the token). |
| `agentos engage provider token [--stdin]` | Store the draft provider's token in the vault. |
| `agentos engage digest [<surface>] [--day D] [--json]` | One day per account (yesterday by default): sessions, operations, drafts waiting for you, holds, signals and caps. See "Daily digest". |
| `agentos engage profile reset <surface> [--field F]` | Forget what the engine knows about profile fields (every field, or each `--field`): the value it last read back, an edit still waiting or not proved, one you denied. Each is edited again, after your approval, if the program asks for a value. |

Every change to a program is a new version, and a new version needs your approval again.

## The day's view

Two commands read across every lane and program. They only read: they change no file and no row,
need no running daemon, and queue nothing.

| Command | What it does |
|---|---|
| `agentos plan status [--workspace biz-a] [--json]` | A table of lanes: readiness, last signal, program phase, pending items. |
| `agentos plan draft [--workspace biz-a] [--json]` | A day-plan draft: what is blocked, what is queued, what is due, and upkeep. |

Readiness says who may act on a lane, from the stores: `frozen`, `quarantined`, `needs human`,
`paused`, `human`, `backoff`, `connector broken`, `no lane` or `ready`. It does not say warm or cold,
which only agentosd knows. Due counts come from the plan the nightly job saved, minus what is done.
A lane that cannot act gets no due work. A signal older than three days counts as stale.

The draft holds no written content yet. `no_content` in `src/agentos/plan/draft.py` is where a brain
task will produce that batch, and what it writes stays a draft: it goes to you on the commit gate
like anything else, and never leaves through this command.

## What agents can and cannot do

Agents can draft a program (`engage_program_draft`), its `profile:` section included, read the
status (`engage_status`), ask to add an item (`engage_item_submit`, which waits for your approval,
see "Items"), read items (`engage_items`), withdraw the items they asked for
(`engage_item_withdraw`) and read the daily digest (`engage_digest`, see "Daily digest"). They
cannot approve a program, change a cap, choose `normal` risk, turn on `auto` comments or replies,
use the `followers_of` source, start a session, reset the profile's state or clear a hold. Those
are yours. See "Holds and approvals" for what waits for you.

## How a session runs

When sessions are on, the node checks every 30 seconds for a session whose time has come (in the
account's time zone, inside its waking hours, never on a quiet day). A session the node could not
start within 30 minutes (it was down) is marked missed, never run late, even across midnight. A
session cut short because the node stopped is closed as aborted, what it had not done yet is
skipped as interrupted, and you get a notice. A session that loses its lane midway ends early. An
account whose program and lane name no time zone runs on UTC hours; the status and the digest say
so.

1. The account must have an approved program and no hold on the whole account (a hold on one kind
   skips only that kind, and a draft provider pause only the provider's text). The session takes
   the account's lane for its length, so no agent acts there meanwhile, and gives it back at the
   end whatever happened.
2. Every operation it plans must have an installed connector that a canary verified (see
   "Operations" below) and an egress rule that was checked on real traffic. If one does not, the
   whole session stops before acting and the account is held (`no_valid_rule`, with
   `op_unverified` or `no_flow` in the detail), so you hear about it once instead of every
   session failing quietly. Profile edits are the exception: they hold only themselves (see
   "Profile").
3. The operations run in a fixed order drawn from the account's habits, spread over the session.
   Each one is checked again against today's cap and the hourly cap (a cap you lowered today
   counts at once). A comment or a post needs words (from items you add, see "Items", then from
   the draft provider, see "Drafts") and something to act on (see "Targets"); without them it is
   skipped (`no_content`, `no_target`), and a target the operation cannot use is skipped as
   `bad_target` and its words go back. A profile edit takes its value from the program (see
   "Profile"). What waits for your approval is listed in "Holds and approvals"; the session goes on
   with the rest. An operation whose key another run already holds is not done, and you get a
   notice (the engine never adopts a run it did not start).
4. If the site shows a captcha, a checkpoint, a logged-out page, a block, the wrong account or a
   rate limit (while acting or while reading targets), the session stops at once and the account
   is held. A rate limit rests it for 6 hours; anything else holds it until you clear it with
   `agentos engage hold clear`. What the session had left moves to new sessions later the same
   week (Monday to Sunday), in waking hours and under the caps. What does not fit by Sunday is
   dropped, and you get a notice.

A session that cannot start because the account is held does not move: its operations are
dropped, and the history and a notice say so.

A session the node ends early because someone else waited for the lane (a claim may be held at
most 900 s while others wait) moves what it had left the same way, to later sessions that week at
least 45 minutes on, in waking hours and under the caps, so the waiting caller gets the lane.

A comment you approved goes inside one of the account's sessions, never the moment you approve
it. If its session passes while the node is down or you have the lane, it moves to the next
session instead of going late. While the account is held it waits; a rate limit moves it past the
rest, a hold you clear lets it take the next session after you clear it, and after 7 days held it
is not sent (the run ends as cancelled, `engage_held`, and you get a notice). While sessions are
switched off or the program is not approved it waits, however long. It also waits for room under
the day's and the hour's caps, counted on every path.

Some outcomes come after the session, and the node settles them every 30 seconds while sessions
are on: an operation
that waited for your approval takes what came of its run once you decide (or the card expires);
one that ran but could not be proved yet is checked again for two days (a profile edit for six)
and becomes verified when
a later check proves it (otherwise it stays unverified, because it may have acted); and one whose
session stopped while it ran takes the outcome of its run once that finishes. An operation counts
as done once, when it turns verified.

Every operation, hold, moved session and dropped operation is kept in the account's history on
the node.

## Holds and approvals

**Holds.** A hold stops engagement until it ends or you clear it. `agentos engage holds <surface>`
lists them, and only you clear them (`agentos engage hold clear`). A hold covers one of three
things:

- **The whole account.** The site showed a signal (a captcha, a checkpoint, a logged-out page, a
  block or the wrong account: until you clear it; a rate limit: 6 hours), or a session planned an
  operation that may not run (`no_valid_rule`, with each kind's problem in the detail: `no_flow`,
  `rule_unverified`, `op_unverified`, or `op_unverified: verified_stale` when the operation changed
  since its canary). A session that starts while the account is held does not run, and its
  operations are dropped.
- **One kind.** A connector broke for that kind (`connector_broken`: until a repaired version is
  installed and you clear it), or a profile edit's operation may not run yet (`no_valid_rule` with
  kind `profile_edit`). Only that kind is skipped (`held`); the rest of the session goes on.
- **The draft provider.** It could not be reached (kind `draft_provider`, until the account's next
  session starts). Only the provider's text waits: nothing else is held, and it ends on its own.

When the site challenged the account, the lane has its own hold as well; hand the lane back to
clear that one.

**Approvals.** These wait for your approval card in Needs you, and nothing is sent until you
approve it:

- comments and replies in approve mode (always on LinkedIn and Reddit: `auto` is refused there,
  even for you);
- every post whose words came from the draft provider, and comments and replies from the provider
  unless the program runs them in auto mode (Instagram and Facebook only);
- test text from the passthrough provider, in every mode;
- an item an agent asks to add (the card comes before anything is queued).

Your own items do not ask again. Approving a program, changing a cap, clearing a hold and
resetting the profile's state are yours alone, on the node, and never come as a card.

## Operations

Each kind of action is one connector operation. Until a canary on a throwaway account runs it on
this node and reads the effect back, it is **unverified**, and sessions hold instead of running it.

| Site | Kind | Operation | Verified |
|---|---|---|---|
| Reddit | post | `reddit.post_text` | no: it needs a fresh canary (the 2026-11-04 one ran an earlier version, so its mark is stale) |
| Reddit | comment | `reddit.comment` | no |
| Reddit | join | `reddit.join` | no |
| Reddit | (targets) | `reddit.browse_subreddit` | no (a read) |
| LinkedIn | post | `linkedin.post` (text, optional photo, to Anyone only) | no |
| LinkedIn | comment | `linkedin.comment` | no |
| LinkedIn | like | `linkedin.react` | no |
| LinkedIn | follow | `linkedin.follow` (a member or a company; never a connection request) | no |
| LinkedIn | (targets) | `linkedin.feed` | no (a read) |
| LinkedIn | profile edit | `linkedin.profile_name`, `linkedin.profile_headline`, `linkedin.profile_about`, `linkedin.profile_photo`, `linkedin.profile_cover` | no, and their egress rules are not captured yet (see "Profile" below) |

**A mark belongs to this node and to one version of the operation.** `agentos connector verify`
writes it into the node's own ledger (`connector-marks.json` in AGENTOS_HOME), with a hash of
everything the operation does. Change any step, locator, readback, preflight, commit request or
pace, and the mark is stale: sessions hold the operation again (`op_unverified`, reason
`verified_stale`) until a new canary. A `verified` line inside a connector file or a bundle is
only a note for readers; it never makes an operation run. Only you write marks, from a shell on
the node. `agentos connector list` shows which operations are verified here.

**To verify one**, on a throwaway account (never a real one):

1. `agentos lane sites <lane> observe on 60`, so the lane's trace records each request's shape.
2. Run the operation through its tool or `agentos connector run <site>.<op> --on <surface>`, as
   the checklist below says.
3. Check the proof yourself on the site, that the trace shows the request under the rule, and
   that it shows no `flow_commit` block (the run sent nothing but its own request).
4. `agentos connector verify <site>.<op> --note "<the account, what it made, how you read it back>"`.
   This marks the copy installed on this node. With `--dir connectors/<site>` it also records the
   mark in that connector file with a new version and installs it. If a later canary fails,
   `--revoke` takes the mark back.

| Operation | Do | It passed when |
|---|---|---|
| `reddit.post_text` | post a text post to r/test, with the version installed now (its 2026-11-04 mark is stale) | the run is verified with a `post_url`; the post page names the account as its author; a signed-out visitor sees it; the trace has `CreatePost` |
| `reddit.comment` | comment on a post in r/test by another throwaway account, then again on a post the account already commented on | each run is verified with the `comment_url` of the new comment (not the older one); your comments page lists the newest first with its text; the trace has `create-comment` under `reddit.comment` |
| `reddit.join` | join a small subreddit, then run it again | the subreddit shows Joined after a reload; the trace has `UpdateSubredditSubscriptions` with `subscribeState` SUBSCRIBED; the second run stops with `preflight_mismatch` and the account is still in |
| `reddit.browse_subreddit` | read r/test | it returns the newest posts (links, ids, authors) in page order |
| `linkedin.post` | post a short text, then one with a photo, then try once with the audience set to Connections only | each run is verified with a new `post_urn` whose text on your activity page is exactly the text; the post shows to Anyone; the trace has `CreatePost`; the Connections only try stops with `preflight_mismatch` and posts nothing |
| `linkedin.comment` | comment on a post by a creator the account follows | your comments activity's newest entry for that post holds your comment with exactly the text; the trace has `createComment` |
| `linkedin.react` | like a post that has comments, run it again on the same post, then on a post you reacted to with Celebrate | the post's own Like is pressed after a reload and the post is on your reactions activity; no comment was liked; the trace has `reactions.create`; the second and third runs stop with `preflight_mismatch` and the reactions stay |
| `linkedin.follow` | follow a company page, then a creator who offers Follow, then a profile that offers only Connect | the page's top card shows Following after a reload; the trace has `addaUpdateFollowState`; the Connect-only profile stops with `preflight_mismatch`; no invitation was sent and no suggested person was followed |
| `linkedin.feed` | read the home feed | it returns post ids and author links |
| `linkedin.profile_name` | change the first and last name once, then run it again with the same name | the name heading on the profile page reloaded as the account is exactly the new name; the trace shows the request's name (write it into the rule `linkedin.profile.name`, see below); the second run stops with `already_current` and saves nothing; once, open the profile signed out (a private window) and check that it shows the new name too |
| `linkedin.profile_headline` | change the headline once, then again with the same text | the headline under the name on a fresh load is exactly the text; the trace shows the request's name (rule `linkedin.profile.headline`); the second run stops with `already_current` |
| `linkedin.profile_about` | change the About text once (a profile that has an About section) to two lines, then again with the same text | the About text on a fresh load is exactly the text, with its line break (spaces aside); the trace shows the request's name (rule `linkedin.profile.about`); the second run stops with `already_current` |
| `linkedin.profile_photo` | upload a new profile photo once (put it in the lane's inbox with `files_put`), on an account that already has a photo | on a fresh load the photo's `src` names a new asset on `media.licdn.com`; the trace shows the upload's registration (`linkedin.draft.register`), its `/dms-uploads/` PUT and the save's request name (rule `linkedin.profile.photo`) |
| `linkedin.profile_cover` | upload a new cover image once, on an account that already has one | on a fresh load the cover's `src` names a new asset on `media.licdn.com`; registration, PUT and the save's request name (rule `linkedin.profile.cover`) |

For the five profile operations the egress rule comes first, because nobody has captured
LinkedIn's profile edits yet: the rules `linkedin.profile.*` in `src/agentos/node/egress.py` ship
with a guess at the request name and an empty `verified`. On the throwaway account, with observe
mode on, edit each field once (through `agentos connector run linkedin.profile_<field> --on
<surface>`, or by hand in the live view if the guessed locators miss), find the request in the
lane's egress trace (`lanes/<ws>/<name>/egress-trace.jsonl` in AGENTOS_HOME: an `observed_request`
row with the request's `op`, or an `observed_commit` row when the guessed rule already matched),
write that name and path into the rule's `op` and `path` and the date into its `verified`
(`"2026-11-.. linkedin.com web"`), and change the operation's commit request (`op`) in
`connectors/linkedin/connector.json` to the same name. Install it, run the operation as the table
says, and mark it: `agentos connector verify linkedin.profile_headline --note "<the account, the
old and new headline, read back on a fresh load>"`. If the intro dialog saves the name and the
headline in one request, the two rules become one.

The design has the same list with more detail: [design/connectors.md](design/connectors.md)
section 4.13.

## Targets

What to act on comes from the program's targets, read during the session itself, inside its claim
on the lane:

- on Reddit, the newest posts of the `communities` you list (for comments), and those communities
  themselves (to join);
- on LinkedIn, posts and their authors from the account's own feed (`own_feed`), for likes,
  comments and follows.

Only links and ids are read, never the words on the page. The account's own posts, your other
accounts on the site, anything it already acted on for the same kind, and more than one post per
author in one read are never picked; a LinkedIn update with no author link is dropped. Candidates
are kept for 3 days. A post takes its target from its item. Other sources (`hashtags`,
`own_engagers`, ...) and the language and account filters are not read yet.

Bind a LinkedIn surface with the account's public profile name (the part after `/in/` in its
profile URL) as `--id`, and a Reddit surface with the username as `--name`, so the engine can
tell the account's own posts apart.

## Items

An item is a post or a comment you made outside agentOS (the text, an optional title, where it
goes, pictures or videos) that you want sent from an account. You add it, and the account's
sessions send it.

- **It fills what the plan already has.** A session that plans a post takes the oldest waiting
  post item for that account (a comment takes a comment item). Items never add posts or comments:
  with five posts waiting and a program of one post a day, one goes out a day, under the same caps.
  If the program plans none of that kind, the item waits (you get a warning when you add it).
- **It is yours, so it does not ask again.** Every item is recorded as added by you and goes out
  without a second approval. When an agent adds one with `engage_item_submit`, you get an approval
  card with the exact item first, and nothing is queued until you approve it (the agent then calls
  again). That holds for every token, the node's own token included: only you, signed in to the
  panel, add an item without a card. The card names each attachment by its file name, type, size
  and sha256, and the approval covers exactly those files. Your history notes which agent asked.
  A standing approval never covers an item, and one approval queues one item.
- **What is checked when it is added.** The account has an approved program; the site has that
  kind (a post or a comment on Reddit or LinkedIn today); the text is within the site's limit and,
  for a comment, the program's `comments.max_chars`; there is at most one target, in the site's
  form (a subreddit such as `r/coffee` for a Reddit post, a post link for a comment);
  attachments are complete pictures or videos already on the account's lane (put there with
  `files_put`), on sites that take them; it expires in the future and at most 30 days away
  (7 days when you do not say), given as a time with a time zone (`2026-11-12T18:00:00+02:00`
  or `...Z`; a date alone or a time without a zone is refused).
- **What happens to it.** `queued` until a session takes it (`reserved`), then `used` with the
  run that sent it. It goes back to the queue only when the session provably sent nothing: no
  target was found, the run never started, or it ended failed with a record that nothing was
  typed or clicked. Anything else that failed counts as maybe sent, and the item becomes `failed`
  and stays with you: a run that failed after it may have posted, a session that stopped while
  the run was going, and a run the site stopped with a signal (a captcha, a rate limit, ...) that
  the runner put back to try later. Check the run, then withdraw the item or add it again (it is
  never sent twice on its own). If the run waits for an
  approval card, so does the item (`awaiting`): it is `used` once the run goes out, and back in
  the queue if you deny the card or it expires. A session that stopped (agentosd restarted or
  crashed) hands back the items it had not started to send on the next sweep, at the latest two
  hours after the session's planned end. An item still waiting at its expiry becomes `expired`. You can withdraw a waiting
  item, and an agent can withdraw the ones it asked for. Every step is in the account's history.

## Drafts

Comments and posts nobody wrote as an item can get their words from a **draft provider**: a
service you name in node.toml that writes a few candidate texts on request. It is off by default,
so such comments and posts are skipped until you add items or turn a provider on.

```toml
engage_draft_provider = "http"                      # off (default), passthrough or http
engage_draft_endpoint = "https://drafts.example.com/v1/candidates"
engage_draft_secret_ref = "engage/draft-provider/token"
engage_draft_timeout_s = 20
```

- **What it is asked.** One request per comment or post a session needs words for, after any
  waiting items: the kind of text, the program's topics (`comments.topics`), its languages
  (`filters.language`), the longest text the site and the program take (`comments.max_chars` for
  comments) and how many candidates to send (3), plus the program's `comments.voice`. The token
  goes only in the request's header, and the account itself is not named.
- **What it answers.** `{"candidates": ["...", "..."]}`, at most as many as were asked for, in at
  most 256 KB and within `engage_draft_timeout_s` for the whole request. Each candidate is
  normalized (Unicode NFC) and trimmed; one that is empty, longer than the limit (counted in
  characters, that is Unicode code points) or has a control character or a hidden character
  (zero width spaces and joiners, direction marks and overrides, the byte order mark, tag
  characters, stray variation selectors) is dropped, never fixed. An emoji's own joiners and
  selectors are fine. An answer that is not that shape counts as no answer.
- **The token** stays in the vault and goes only in the request's `Authorization: Bearer`
  header, never through a proxy from the environment. `agentos engage provider token` stores it
  under `engage_draft_secret_ref`, or `engage/draft-provider/token` when that is empty, which is
  where requests look when no ref is set. The endpoint must be https (plain http only to this
  machine).
- **When a draft waits for you.** The session uses the first candidate and keeps the others as
  alternatives you can see with `agentos engage drafts`. A draft is never treated as approved
  content the way your items are: posts, and comments and replies in approve mode, become an
  approval card in Needs you (see "Holds and approvals"). Comments and replies in auto mode
  (allowed only on Instagram and Facebook) go out without a card, because auto means you chose
  not to review them; test text from the passthrough provider waits in every mode.
- **When the provider cannot be reached** (it is down, slow, refuses the token or answers
  nonsense), the session skips its comments and posts that needed a draft and runs the rest
  (likes, follows, items). The account's history notes it, you get a notice (at most one every 12
  hours), and a hold of kind `draft_provider` shows in the status until the account's next
  session starts (an hour when there is none today). That hold stops only text from the provider:
  the rest of the session, and your items, still go. The next session asks again.
- **What happens to a draft.** `pending` while a session has it, then `used` (with the run) or
  `returned`. A draft goes back only when its run provably sent nothing (no target was found, the
  run never started, or it ended failed with a record that nothing was typed or clicked); a later
  session can offer it again once, within a day of its return. When a run fails after it may have
  posted, the site stopped it with a signal and the runner put it back to try later, or the
  session stopped while the run was going (agentosd stopped or crashed), the draft becomes
  `failed` and is never offered again: check the run, and nothing is posted twice. Once its run is decided a used draft becomes
  `approved` (it went out and was verified), `denied` (you refused the card), `expired` (the card
  expired) or `failed`. Alternatives expire a day after they were written, returned drafts a day
  after they came back.
- `engage_draft_provider = "passthrough"` writes fixed test candidates ("Test comment 1 about
  coffee") without any network, for trying the flow out. It does not start while an approved
  program runs comments or replies in auto mode, and such a program cannot be approved while it
  is on. Do not leave it on for real accounts.
- `agentos engage provider test` sends one request and prints the number of candidates and the
  time it took, and whether a token went with it, so you can check the endpoint and the token
  without starting a session.

## Profile

A LinkedIn program can say what the account's public profile should show. Every field is
optional; one you leave out is never touched.

An account attached to a profile (docs/PROFILES.md) takes these values from the profile instead,
and the program's own `profile:` is ignored: write the profile once for every account there.

```yaml
profile:
  name: {first: Ada, last: Lovelace}        # 1 to 50 characters each
  headline: Analyst of engines              # at most 220 characters
  about: |                                  # at most 2600 characters, line breaks kept
    Notes on the analytical engine.
  photo: in_3f9a2c71                        # a file id from files_put, in the lane's inbox
  cover: in_8b1d0e44
```

These limits are the brief's (no LinkedIn figure is written down in this repository); control
and invisible characters (zero-width spaces, direction marks) are refused, because they hide text
from you when you approve it. A draft that changes the profile says which fields it changes.

The engine compares the program with what the account last showed: a value an earlier edit read
back on the profile page, or a text the edit found already there (`already_current`). Each field that differs is one
edit, made in a fixed order (name, headline, About, photo, cover), at most the day's
`profile_edit` cap (1 a day at the start, 1 an hour), in the day's first session; the rest follow
on later days. Profile edits start with the steady phase (none while warming up) and never run
with `risk: off`. Each edit:

- does not wait for an approval card: you approved its value already (the program's `profile:`,
  or the profile the account is attached to). "Ask me first" on the lane still makes it wait;
- stops with `already_current`, saving nothing, when the page shows exactly one such element
  and it holds exactly the value; the engine then counts the field as current. Any other stop
  (`preflight_mismatch` when the element is missing or not alone, a timeout, a failed proof) is
  a failure: the field is tried again on a later day, under the cap, and after 3 failures in a
  row for the same value you get a notice;
- is proved only on the profile page reloaded as the account (the view the account itself gets,
  not a signed-out visitor's; the canary checks a signed-out view once): the exact text for the
  name and headline, the About text with its line breaks (spaces normalized), and for the photo
  and cover a new image on `media.licdn.com`, compared by its asset id, not its size or
  signature. The photo and cover first note the image already there; with none to note, nothing
  is uploaded;
- takes its value only from the program or the attached profile, never from the content queue;
- sends only its own request: while it runs, the egress guard refuses any other publish or write
  from its page (the photo's upload and its registration aside).

An edit that waits for you, or that ran and could not be proved, is not tried again for the same
value for 3 days, or until the engine learns what came of it; one you deny is not asked again for
that value. A new value in the program is due again, and `agentos engage profile reset` forgets
all of this for a field. `agentos engage cap <surface> profile_edit 0` stops profile edits at
once, including those already planned for today. Until the five operations and their rules pass
the canary (see "Operations" above), a session that plans a profile edit holds profile edits
alone (`no_valid_rule` with kind `profile_edit`, the detail naming `rule_unverified` or
`op_unverified`): its other operations, and the account's other sessions, go on. With the day's
profile_edit cap at 0 nothing is checked and nothing is held. Clear the hold with `agentos engage
hold clear <surface> --kind profile_edit` once the operations are ready.

## Caps

Every account starts at the **careful start**. After each week with no warning from the site the
cap grows by 15 percent, up to the ceiling of its risk level. A warning stops the growth for two
weeks. The **normal ceiling is the hard ceiling**: you can raise a cap up to it, never above.
These are a quarter to a half of the numbers growth tools quote (no site publishes a safe rate).

| Site | Kind | Careful start | Careful ceiling | Normal start | Normal ceiling | Per hour |
|---|---|---|---|---|---|---|
| Instagram | like | 10 | 40 | 20 | 80 | 15 |
| Instagram | follow | 2 | 10 | 4 | 20 | 5 |
| Instagram | comment | 1 | 5 | 2 | 10 | 3 |
| Instagram | story view | 10 | 40 | 20 | 80 | 30 |
| Instagram | post | 1 | 2 | 2 | 3 | 1 |
| Facebook (profile) | like | 5 | 25 | 10 | 50 | 10 |
| Facebook (profile) | comment | 1 | 3 | 2 | 6 | 2 |
| LinkedIn | like (reaction) | 5 | 20 | 10 | 30 | 8 |
| LinkedIn | comment | 1 | 3 | 2 | 5 | 2 |
| LinkedIn | follow (connection request) | 2 a day, 10 a week | 5 a day, 25 a week | 4 a day, 20 a week | 10 a day, 50 a week | 3 |
| LinkedIn | post | 1 | 2 | 2 | 3 | 1 |
| LinkedIn | profile edit (name, headline, About, photo and cover together) | 1 | 2 | 1 | 3 | 1 |
| Reddit | comment | 1 | 4 | 2 | 8 | 2 |
| Reddit | join | 1 | 2 | 2 | 3 | 1 |
| Reddit | post | 1 | 1 | 1 | 1 | 1 |
| Medium | like (clap), follow | 3 | 10 | 6 | 20 | 5 |
| Blog, any feed | post | 1 | 2 | 2 | 3 | 1 |

Reddit has no likes: Reddit's rules say software may not choose what to vote on.

**Warm-up** (new accounts): days 1 to 3 only look around (browse, open posts, a few story views);
days 4 to 7 add up to 5 likes, 1 follow and 1 join a day; from day 8, up to 10 likes, 3 follows
and a first comment every other day. Then the steady phase starts at the careful start.

## Daily digest

One place to see how yesterday went, per account. The panel's Home shows an **Engagement** card
for each account you can see (sessions run of planned, operations verified of attempted, approvals
waiting with a link to Needs you, holds, and how much of each cap was used); the card opens
`/engage/<workspace>/<name>`, with a day picker for the last seven days, every session, a table per
kind, holds and signals, and the drafts waiting for you with their ids. `agentos engage digest`
prints the same on the node, and agents read it with `engage_digest` (JSON, or `format="text"`).

- **The day** is a calendar day in the account's time zone, the one its sessions run in (the
  program's `timezone`, else the lane's, else UTC, and then the day says why). Without `--day` it
  is yesterday there, so two accounts in different zones can show different dates. A day is
  YYYY-MM-DD within the last 400 days.
- **Sessions**: every window of the day (the plan's, then the ones a signal moved there), when it
  started and ended and how (`done`, `ended_early`, `aborted`, `refused`, `missed`, or still
  `scheduled`). "Planned" counts the plan's windows, "rescheduled" the ones a signal added, and
  "run" the ones that started.
- **Operations per kind** (every kind, profile edits included): planned (by the day's plan),
  moved in from another session, attempted (every one that reached the site), and of those
  verified, unverified, failed or waiting for your approval, with why runs failed (the site's
  signal, a timeout, another run holding the key, the page already showing a profile value, your
  denial, ...) or were held by the node (paced, a broken connector); skipped ones with the reason
  (the day's cap, the hour's cap, a hold, turned off, no words, the draft provider down, nothing
  to act on, a target it cannot act on, the profile already current, the lane busy or lost, no
  checked rule, the node stopped, the session ended early, moved, no room left that week,
  missed).
- **Caps**: used against the day's cap and the hard ceiling, with the hour's cap, for every kind
  of the site (profile edits included). "Used" counts
  exactly what the session runner counts against the cap (every operation that reached the site,
  whatever came of it), and the caps are the ones it applies, worked out by the same rule: the
  day's plan, never above your current cap or the hard ceiling, and the hour's cap never above the
  day's. A day with no saved plan shows no cap. Next to each cap, `account` shows that kind's
  actions on the account that day on every path (the action ledger), by who drove it: agents
  (an MCP agent or a pilot run), flows (a flow, a workflow, engagement itself) and you. `counted`
  is what the cap holds against: the larger of "used" and agents plus flows. Your own clicks are
  shown but never count.
- **Drafts waiting for you**: the operations of that day still waiting for your approval whose words
  came from the draft provider, with the draft id and the approval id; how many drafts the
  provider wrote that day, and how many failed (a run may have posted them) or expired that day.
  Home's card counts every approval waiting now, whatever its day.
- **Items** used, failed, expired, withdrawn and added that day, and how many wait in the queue and
  for your approval now.
- **Holds** in force at any time that day (whether each began that day and whether it still
  holds now), the **signals** the site showed (captcha, rate limit, ...) and the day's **notices**. A
  notice that names an account you cannot see says "another account". A hold on one kind (a
  broken connector, profile edits) says that only that kind waits, and the panel shows the account
  as partly held, not held; a `no_valid_rule` hold says in words why each kind may not run (no
  canary verified it, changed since its canary, ...). A draft provider pause (`draft_provider`)
  shows apart: it stops only the provider's text, so it never marks the account as held.

With sessions off the digest shows what was planned and nothing run. It only reads: it never draws
a plan, starts a session or changes anything.

**Draft text stays out of it.** A draft's words come from a provider, so they are untrusted and can
be sensitive. The tool, the CLI and members' panels show draft ids only; the owner's panel shows
the first 80 characters, and the whole text is in Needs you (and `agentos engage drafts`).

## Timing

Each account has its own habits, drawn once and kept: when it wakes and sleeps (in the lane's
time zone), how much later it starts at weekends, its busiest hours, how many sessions it likes
(2 to 4 a day), how long they last (about 9 minutes, rarely over 25) and how often it acts on
what it sees (about 1 post in 4 to 8). The habits drift a little every week.

Session keepalive (docs/design/keepalive.md) is separate: a read-only visit to an idle lane every
few days that never likes, follows or counts against a cap. An engagement session uses the lane like
any agent, so it resets the keepalive timer; keepalive reads the habits drawn here for its waking
hours and changes nothing in the engine.

Sessions start at least 45 minutes apart, never on a round minute, and never at yesterday's
time. Accounts of one company never start within 20 minutes of each other. Every night the engine
checks the last 14 days: if two accounts were active at the same hours, one gets new habits and
you get a notice.
