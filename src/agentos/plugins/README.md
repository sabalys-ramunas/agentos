# Plugins

Providers are who thinks (`agentos.brains`). Plugins are what you turn on. This package is the
node-side registry: which workers this build has, why one cannot run on this node, whether it is
on, and which brain does each of its jobs.

```
agentos plugins list [--json]
agentos plugins show <id> [--json]
agentos plugins enable <id>
agentos plugins disable <id>
agentos plugins assign <id> <job> <brain>
agentos plugins unassign <id> <job>
agentos plugins lane <lane> [on|off] [--json]
```

## What a plugin is

A manifest in `entries.py`: reviewed, first-party code in this repo. There are no entry points and
nothing loads from outside the repo (principle 8). Adding a plugin is a code review.

| Field | Meaning |
|---|---|
| `id`, `name`, `description` | What people read |
| `stage` | `works`, `declared` (no engine, cannot be enabled) or `later` |
| `lane_kinds` | Lane kinds it works on. Each one is checked on this node |
| `services`, `tools` | Feature modules it adds, loaded only when it is on |
| `cli` | A module whose Typer `app` is mounted as `agentos <id>`, on or off; its commands refuse while it is off |
| `needs` | Requirements (`requirements.py`). Each returns a plain reason, pass or fail |
| `jobs` | What a model does for it, what that model must be able to do, and whether the job is optional |

## Jobs

`enable` needs a brain on every required job. An optional job can stay empty, and the job says what
the worker does then (`show` prints it). For the workers here, only `decide` is required:

| Job | Required | Without it |
|---|---|---|
| `decide` | Yes | The worker does not load |
| `write` | No | The worker cannot write text, so a run that needs new words stops and says so |
| `rescue` | No | When the worker is unsure, it stops for a person |

`text-review` is not a worker: it drives no lane, and its one job, `review`, is required. Once a week it
proposes clearer agent-facing text for a person to review (`src/agentos/textreview/README.md`).

`unassign` clears a job. A required job left empty makes an enabled worker `blocked`. A brain on an
optional job must still fit it: a person chose that brain, so one that no longer fits blocks the
worker until it is replaced or cleared.

## On one lane

`browser-worker` also has a switch per lane, on by default, in `plugins.db` next to the rest
(`lane_switches`, added to version 1 of the file: a file without the table has every lane on, and the
next write creates it). Off stops the worker on that lane and nothing else: the browser tools and a
person's take over do not read it. `src/agentos/pilot/README.md` says what a run does. `show` lists the
lanes it is off on, and `plugins_list` gives them as `lanes_off`, limited to the lanes the caller reaches.

The panel reads and sets it through `/api/lanes/{ws}/{name}/worker` (docs/API.md), for a person signed in.

## Rules

- **Off by default.** State is `$AGENTOS_HOME/plugins.db`. A missing file means everything is off. A
  file that is broken, has another schema version or holds a value it should not also reads as
  everything off, and writes refuse it instead of replacing it.
- **Only a person turns a plugin on.** Writes live in `admin.py`, which only `cli.py` and the panel route
  `agentos.api.routes_worker` import. That route refuses every bearer token, the owner's included. The
  one tool, `plugins_list`, reads. Naming a disabled worker there gets `plugin_disabled`.
- **Checked, not assumed.** `enable` refuses with each unmet reason, and the daemon checks again at
  every start, so a state file cannot turn on something that cannot run.
- **Capability is checked here, privacy is not.** A job's brain must be able to do the job. Where a
  workspace's prompts may go stays the brains router's job.

## Who can change this

No tool can. The commands below are the other way in, and two kinds of process on a node can run
them: a workbench lane (commands run as root, each asked of a person) and a brain with
`confinement = "off"`.

So `enable`, `assign`, `unassign` and `lane <lane> on` refuse with `plugin_person_only` when `AGENTOS_LANE` or
`AGENTOS_BRAIN_TASK` is set. The lane runner sets the first on everything a lane runs, a person's
shell in that lane included. The brain runner sets the second on every attempt, and for Codex it is
set again in `shell_environment_policy`, which drops `AGENTOS_*` from tool commands otherwise.
`disable`, `lane <lane> off`, `list` and `show` still work: turning a worker off is the safe way, and must
always work.

**What this stops:** an agent that runs `agentos plugins enable`, in a lane or as a brain, whether a
page told it to or it decided to, and a person who approved a lane command that does that.

**What this does not stop.** It is a variable, not a boundary. It does not stop:

- A process that clears the variable first: `env -u AGENTOS_LANE agentos plugins enable ...`, or
  `sudo`, which resets the environment by default.
- A process that writes `plugins.db` itself. Root can, in a workbench lane, and so can a brain that
  runs as the daemon's user.

What does hold is outside this package. A confined brain cannot reach `AGENTOS_HOME` at all
(`agentos brains confinement`), so keep confinement on anywhere that holds real accounts. Each
workbench lane command is asked of a person, so read it before you approve it. And a change made
through these commands is in the action log, while one made by writing the file is not.

## Audit trail

Turning a plugin on or off, on the node or on a lane, and changing who does a job each write a
`plugin_change` row to the node's action log: what changed, when, the brains before and after, the
lane, and who (`cli:<name>`, from `SUDO_USER` or the login, so it is what the shell says and no proof,
or `panel:owner` and `panel:<member>` from a signed-in session). `agentos plugins show <id>` prints the
newest 20, and `--json` gives them as `history`. The rows are in the same hash chain as everything
else, so `agentos audit verify` covers them.

- The row is written inside the `plugins.db` transaction. If the log cannot take it, the change is
  undone and the command fails with `plugin_change_not_recorded`. A crash between the two commits
  leaves a record of a change that did not stand, which is the safe way round.
- `disable` and `lane <lane> off` are the exception. They go through, and fail to say nothing recorded it.
- A command that changes nothing writes nothing.
- Under `sudo` on a node where agentosd has never started, there is no log yet, and the command says
  to start it once. Root must not create a file the daemon then cannot write.
- Rows follow the log's retention (`trace_retention_days`, 90 by default), so older changes leave
  `show` too. The anchors in the chain stay.

`updated_at` in `plugins.db` still says when a row last changed, and nothing more.

## When a change takes effect

`enable` counts at the next `agentosd` start. `disable` stops the plugin's tools at once, because
each call checks the state, and its services stop at the next start. A lane's switch counts at once,
in both directions, because a run reads it at every step.

## How a plugin loads

`agentos.node.daemon.build_plugins` runs after the feature services, and `_plugin_tools` adds the
tools. A plugin's modules must **not** also be in `FEATURE_SERVICES` or `FEATURE_TOOLS`: those
always load, so the plugin would gate nothing. A plugin that fails to build, start or serve its
tools is marked `failed`, and the node carries on.

A worker asks the runtime, never this package's internals:

```python
plugins = runtime.require("plugins")
plugins.require("browser-worker")                    # raises plugin_disabled when it is off
brain = plugins.brain_for("browser-worker", "decide")  # the name to hand the brains service
```
