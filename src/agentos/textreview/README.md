# Agent text review

Once a week, a brain reads how agents used this node and proposes clearer agent-facing text: the MCP
instructions, tool descriptions, `howto` recipes and the next-step line of each error code. A person
reads the proposal, probes it with lanebench, and turns what holds up into a code change. Nothing
here changes the node.

```
agentos plugins enable text-review
agentos plugins assign text-review review <brain>
sudo systemctl restart agentosd

agentos text-review run [--json]        # a review now, whatever the schedule says
agentos text-review list [--json]
agentos text-review show <id> [--json]
agentos text-review settings [--json]
```

## Why

An experiment ran this kind of review (google-research/harness2's method, not its code) on a week of
agent sessions, with Opus as the reviewer, and probed the result on lanebench. Its edits cut Haiku
5.5's calls per task by 20% and its missing-argument errors by 52%, held-out tasks included, with no
change in pass rate or safety gates, and no change for Sonnet. So the value is for weaker models:
brains, and the browser worker's jobs. Its blind spot: a reviewer that never sees outcomes missed a
decoy-button click and invalid repair patches, because both runs ended "done" with no error. That is
why this plugin also reads lanebench results, where each task's outcome is known.

## What it reads

- **The action log** over the window (7 days): the calls agents and brains made, grouped by run or
  grant, each labelled by who was behind it (the client a grant was issued for, the brain that ran a
  task, or the brain deciding for the browser worker). People and flows are left out, and so are the
  node's own checks. Those are marked only by the subject their grant was issued for, so
  `skip_subjects` lists them (`canary-agent`, `preflight-agent`), and `skip_workspaces` lists the
  workspaces only checks use (`preflight`, `lanebench`).
- **Of each call, only**: the tool, its status, the error code and the catalog's text for that code,
  the argument names, and the values of arguments whose type is a fixed set of choices (a Literal, an
  Enum or a bool, such as browser_act's `action` and `snapshot`). Never argument values, the call's
  summary or an error's message, which can hold a page's or an email's text.
- **Patterns** over those calls: errors by code and what the agent did next, the same call twice in
  a row, start-up calls repeated, browser_act followed by a full snapshot, and how often `delta` is used.
- **Lanebench reports** you put in the inbox (`$AGENTOS_HOME/text-review/inbox/`, the JSON that
  `agentos lanebench run` writes). Per task: whether it passed, how the run said it ended, the kinds
  of the checks that failed, the gates that failed with their findings, and its tool trace in the
  same shape as above. Task ids, lane ids, URLs, hosts, quoted text and the bench's own name are
  replaced. A run that reads a report moves it into its bundle.
- **The current text**, as the node serves it.

With fewer than `min_calls` calls (40) in all of that, the run is recorded as `skipped` and no model
is asked.

## What the brain does

One task on the brain a person assigned to `review`, with no tools, no MCP, no files and no child
grant (`Requirements(tools=False, no_tools=True)`). Its only output is a JSON object a person reads,
under an edit contract where anything left out is unchanged:

```
{"instructions"?, "tools"?: {name: text}, "recipes"?: {topic: {title?, summary?, steps?}},
 "errors"?: {code: line}, "changes": [{"what", "evidence", "fixes"}]}
```

Then the safety lock checks it: only existing tools, codes, topics and fields; the safety guidance
kept in the instructions (page text is data, challenges stop the lane, never log in again, approvals
wait and are never rephrased, agent_paused, egress_refused, denied, wrong_account); every "do not"
and "never" a line had still there; recipe placeholders kept; instructions under 5,000 characters;
no lane id, task id, evidence id or bench word the line did not already have; no em dash. A proposal
that fails is asked for once more with the problems listed, and otherwise the run is `rejected`.

The brains service keeps 4,000 characters of a task's reply, so the prompt asks for at most
`max_changes` (3) and a reply under 3,500 characters. A longer one is asked for again with half as many
changes.

## What it never does

It never changes the running node's text, opens a pull request, pushes or applies anything. No tool
reads or starts it: `run` is for a person at the node, and refuses inside a lane or a brain task.

## The bundle

`$AGENTOS_HOME/text-review/<run id>/`, private to the node's user:

| File | What |
|---|---|
| `CHANGES.md` | What to read first: the status, each change with its evidence, and what to do next |
| `evidence.md` | Exactly what the reviewer saw of the week |
| `variant/` | `instructions.md`, `tools.json`, `recipes.json`, `errors.json`: the whole current text with the proposal merged (ready runs only) |
| `proposal.json` | The reply as the reviewer gave it |
| `validation.json` | The lock's verdict on each reply |
| `meta.json` | The window, the counts, the brain, the cost and the status |
| `reports/` | The lanebench reports the run read |

The owner is told through the node's notifier when a run is ready, rejected or failed. A skipped week
is quiet.

## Probing a bundle

```
agentos lanebench run --variant $AGENTOS_HOME/text-review/<run id>/variant --executor mcp-agent ...
agentos lanebench run --executor mcp-agent ...        # the same tasks and seed, without it
```

Compare calls per task, argument errors, the pass rate and the gates, with a weaker model first. What
holds up becomes a code change: the instructions and recipes in `src/agentos/node/tools/recipes.py`, the tool
docstrings, and the `agent` lines in `src/agentos/core/error_catalog.toml`.

## Cost

At most three answers a run (one more for a reply too long to read, one more for a refused
proposal), each a single tool-less turn. The prompt is about 100,000 characters: the current text is
most of it. On a subscription brain that is part of the subscription; on a metered one, `max_usd`
($1.00) caps the whole run. `meta.json` and `list` show what each run spent.

## Settings

`$AGENTOS_HOME/text-review/settings.toml` (`agentos text-review settings` shows them). A file that
cannot be read means the defaults.

| Setting | Default | What |
|---|---|---|
| `interval_days` | 7 | A run starts when the last one is older than this |
| `window_days` | 7 | How far back the action log is read |
| `check_every_s` | 3600 | How often the daemon looks at the schedule |
| `min_calls` | 40 | Below this, the run is skipped with no model call |
| `max_usd` | 1.0 | What one run may spend, retries included |
| `timeout_s` | 900 | How long one answer may take |
| `max_changes` | 3 | How many changes are asked for |
| `workspace` | text-review | The brain task's workspace, so `agentos brains privacy` can be set for it |
| `skip_subjects`, `skip_workspaces` | see above | The node's own checks |
| `inbox` | `text-review/inbox` | Where lanebench reports go |

## Turning it off, and removing it

- **For a while**: `agentos plugins disable text-review`. The daemon checks at every tick and before
  every answer, so no new run starts and a run going stops before it spends more. The service itself
  stops at the next restart. `enable` turns it back on.
- **For good**: delete `src/agentos/textreview/`, the `TEXT_REVIEW` entry in
  `src/agentos/plugins/entries.py`, and its line in `src/agentos/plugins/README.md`. Nothing else
  imports it (`tests/unit/test_textreview.py` checks that, so delete that test too). Bundles stay in
  `$AGENTOS_HOME/text-review/` until you remove them.
