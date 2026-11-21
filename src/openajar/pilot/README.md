# openajar pilot: a cheap worker on one lane

Browser work is expensive when a frontier model does every step. So the expensive model calls
`browser_task(on, goal, values)` and gets back one honest result. Code owns the loop, a cheap decider
picks from what code offers, and the node's own tools act. The design is
`docs/design/browser-and-computer-agents.md` sections 5 and 6.

## The three jobs

| Job | When | Who |
|---|---|---|
| `decide` | Every step | A decision brain (Jev), through `BrainService.decide` |
| `rescue` | A pick is too unsure to act on | A model that writes, through `BrainService.pick` |
| `write` | A step needs words and no value was supplied | A model that writes, as a bounded task with no tools |

The worker is the `browser-worker` plugin, off until a person turns it on. Which brain does each job
is a person's choice there. Assign first, because `enable` checks the assignments:

```bash
openajar plugins assign browser-worker decide jev
openajar plugins assign browser-worker rescue claude-haiku
openajar plugins assign browser-worker write claude-haiku   # only if a run should write its own words
openajar plugins enable browser-worker
```

A run with no `write` brain ends `needs_help` at any step that needs words, so pass them in `values`.
A run that uses one waits for a person at every publish after it, until a person chooses otherwise
for that lane (below). The limits of a run live in `$OPENAJAR_HOME/pilot/settings.toml`, and
`openajar pilot settings` shows them:

```toml
max_steps = 25
wall_s = 300
max_usd = 0.25
```

An old file that still says `written_text_asks` keeps its limits, and that key does nothing now.

No model is assumed to win. `Decider`, `Rescue` and `Writer` are small protocols in `deciders.py`, and
`tests/lanebench/executors/pilot.py` scores them on the same bench.

## Each lane is a person's to set

Two switches, both per lane, neither of them in this package's settings.

**How a publish goes** is the lane's Ask me first (`openajar.policy.ask_first`, `src/openajar/policy/ROUTING.md`).
There is no second setting:

```bash
openajar flows ask-first --account personal/reddit on    # Asks you first
openajar flows ask-first --account personal/reddit off   # Runs on its own
openajar flows ask-first --account personal/reddit       # what it is now
```

| The lane | Words a caller supplied | Words the `write` job wrote |
|---|---|---|
| Asks you first | Waits for a person, with the exact content | Waits for a person, with the exact content |
| Runs on its own | Goes on its own, within the rules below | Goes on its own, within the rules below |
| No choice made yet | The node's policy | Waits for a person |

Only a person's off counts as "Runs on its own": a row with an agent's name never reads that way, and an
agent's off stores nothing. So a lane where nobody chose keeps asking for written words, and the
reason is the same one as ever: a page can talk a model into them.

On a lane that asks, the one thing that stands in for waiting is a standing approval bound to that
exact content, which a person has read. A free-form one covers no commit.
What no switch reaches, in any row of that table:

- **Money and what cannot be undone** always ask, and `<site>.unknown` always asks on a pilot grant.
- **`[taint] downgrade`** (`commit` is in it by default) still applies, and the worker always reads a
  page first. So on a node with `commit` in that list, every publish asks, "Runs on its own" included.
  The panel gets this as `after_a_page_asks`. `openajar policy routing` prints the list.
- **A login** never asks when its password is in the vault, on any row.

**Whether the worker runs there at all** is the plugin's switch for that lane, on by default:

```bash
openajar plugins lane personal/reddit off
openajar plugins lane personal/reddit on      # a person only
openajar plugins lane personal/reddit
```

Off is about the worker and nothing else. `browser_task` is refused with `pilot_lane_off` before anything
runs, a queued run ends with it when it comes up, and a run that is going stops at its next step
with nothing further done. A run parked on an approval stays parked, and stops without sending when
it wakes. Agents keep the browser tools, and a person can still take over.

## How a step decides

One request asks every question at once, and code adds the answers up, in this order:

1. `needs_owner` at 0.7 or more stops for a person. Nothing else does: `operation` has no HOLD.
2. `done` and an operation of DONE end the run, verified only when both say so.
3. An effect is judged by the head that picked its element. It needs a chance of at least 0.7 and at
   least three times the runner-up's. A click also needs `operation` at 0.5 or more.
4. A WAIT needs 0.8, because the page has already gone quiet. Anything less scrolls instead.

Thresholds read `probabilities[choice]`, not `confidence`, which is rescaled by the option count.
Code narrows the options first: no off-site link, nothing disabled, nothing behind an open dialog
(and no SCROLL or WAIT while one is open), no toggle already flipped, and, until something is
pressed, no twin of a control in the form just filled. All of it was measured in
`tests/lanebench/lab/`, whose README has the numbers.

## Escalating

A low-confidence pick with an effect is the normal case that goes up a level, not a failure. A
shaky WAIT or SCROLL costs nothing and never escalates. With a rescue assigned, the loop asks it the
SAME question over the SAME options and state, one question at a time: an operation first, then its
target only if that was unsure too. `BrainService.pick` runs with no tools and refuses anything but
an offered id or a plain none, and the loop checks again, so a rescue cannot invent an action, a URL
or a selector. A sure pick then passes the same gates as a decider's. Without a rescue, or when it
declines or stays unsure, the run ends `needs_help` with `needs` saying what would carry it on.
Every escalation is in `output.escalations` with its reason, outcome and cost. A rescue takes
seconds where a decision takes 200 ms, and it needs the caller's own `brain` right, as `brain_run` does.

## How a run ends

| `pilot_status` | Run status | Means |
|---|---|---|
| `done_verified` | `succeeded_verified` | Both done answers agree, everything supplied was used, the last action's target changed, the post or comment it sat in holds what the request named (`target`, or an @handle, link or quoted words in the goal) in a form that pins one item (a display name alone, or a quote that is only part of a line, does not), and nothing was refused or blocked since. Otherwise `done_unverified`, reason `target_not_in_goal` |
| `done_unverified` | `succeeded_unverified` | It looks done, but `reason` says what does not line up. The two done answers disagreeing lands here, and so does a page that merely changed, because a clock ticks too |
| `held` | `partial` or `failed`, `pilot_held` | A captcha, a checkpoint, a block, a signed-out lane on a run not asked to sign in (`login`), or a person took the lane |
| `needs_help` | `partial` or `failed`, `pilot_needs_help` | It stopped rather than guess |
| `gave_up` | `partial` or `failed`, `pilot_gave_up` | A budget ran out, or nothing moved in 4 steps |
| `refused` | `partial` or `failed`, `pilot_refused` | Policy or a safety rule ended it. The wrong account, a limit or the lane's pace ends it at once |
| `refused` | `partial` or `failed`, `pilot_lane_off` | A person turned the worker off on this lane, and it stopped at its next step (`reason` is `pilot_lane_off`) |

`partial` means an action landed or may have, so the page may not be as it was: do not simply retry.
`waiting_approval` is not an end: the run parks, gives the lane's claim back, and carries on by itself
once a person approves.

**A person taking the lane** stops the worker at its next tool call. Work already inside a tool call
finishes that one step. The run ends `held`, `pilot_held`, and an action that landed just before the
take over counts, so it reads `partial` and not `failed`. A run that had not started, or one parked
on an approval that a person then approves, waits in the queue (`error.code` `deferred`,
`error.control` `human`) and goes after the hand back.

## What holds it in

- **Input** stays in `openajar.node.human`: every action is a node tool through the policy wrapper.
  The pilot adds no input layer, no page script and no CDP, and it never uses the flow driver, whose
  `guard.flow` would lift the agent grant. A fresh page gets `human.dwell` at the flow driver's
  `DWELL_MS`.
- **The child grant** is the caller's, narrowed: one lane, the mode's tools as pins, commit at most, a
  budget, no delegation. Its life is the wall-time budget plus the approval window, because an
  approval binds to the grant that asked. It is revoked the moment the run ends.
- **Money and what cannot be undone** always need a person (docs/PRINCIPLES.md, 5), on every config,
  and no effect rule names either. So `browser_task` refuses a lane unless every entry of its site list is a
  host the commit gate reads, or a platform's own reviewed CDN or sign-in host from `SUGGESTED_SITES`
  (`pilot_lane_uncovered`): on any other host a checkout would run as a plain draft. On them, a publish no rule reads (`<site>.unknown`) always asks a person for a pilot grant,
  whatever `[taint] downgrade` says. The one exception is the login window below.
- **A commit** is the node's to catch. The commit gate holds a publish, and the node's policy says who
  decides. The pilot's context is tainted from its first snapshot, so with `commit` in `[taint]
  downgrade` (the default) a person approves the exact content. With it out, the lane's Ask me first
  decides (above).
- **Words a model wrote** reach a person unless a person chose "Runs on its own" for that lane. A page
  can talk a model into them, so once the `write` job has written, every publish of that run asks,
  on every config, and the approval shows the exact text. The choice is read when the words are
  written, so choosing it after that does not release the run that wrote them. The writer reads the
  page inside a random fence, as a rescue does.
- **Secrets**: `values` is for plain strings, and a name that looks like a secret is refused. A
  password is `{"vault": "<entry>"}`: the vault fills it, and a model reads only a label. The goal, the
  labels, results and tool errors pass through the same masking an MCP call does, and no images are
  sent. `must_not_send` is empty on a node, because a run holds no secret's text to compare with.
- **A login** waits for nobody when its password is in the vault: storing it is the owner's permission,
  so `vault_fill` runs on its own even though the context is tainted, and the click that follows goes
  through the guard's login window (`docs/design/egress-guard.md`, 8.11). The caller's grant must hold
  `secret` to pass it on. A captcha, a code the entry has no source for, a device approval and a
  checkpoint still end the run for a person.
- **A restart** never starts a goal again. A run journals its start before its first action, so one
  killed at any point after that ends `pilot_interrupted`, reads `partial`, and its child grant is
  revoked.

## Not built

`repair`, `resolve` and `login_assist` modes, `browser_act_point`, navigating (start the lane on the
right page first), and asking in two stages on a page with more than 254 options (the run ends
`needs_help`, because what is cut could be the right control).
