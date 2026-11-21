# The studio agent experience eval

Rule 15 of [docs/design/studio-ax.md](../../docs/design/studio-ax.md): a fixed set of small tasks,
run by a model that has only the studio MCP tools and the `openajar-studio` skill, recording per
task whether it finished, its calls, the tokens it read from tool results and its wrong turns. A
change to a tool, a result shape or a description that makes these worse does not merge.

It is not part of the unit suite (no file here is named `test_*`), and it needs Chrome.

## Run it

```
uv run python tests/studio_eval/run.py                                # scripted agents: proves the checkers
uv run python tests/studio_eval/run.py --agent claude --model haiku   # Claude Code headless, Haiku
uv run python tests/studio_eval/run.py --agent claude --model sonnet --task t04_why_moved
```

`--out <folder>` keeps results (`results.json`), the transcripts (`<agent>/<task>.log`) and each
agent's scratch HOME. Without it, a new temporary folder is used and printed.

## What a run does

For each agent kind it starts `openajar studio up` in a scratch HOME (never your `~/.openajar`)
and serves the task pages on 127.0.0.1 with a plain http server, a stand-in for a dev server.
For each task it lays the pages out fresh, runs the task's setup (a baseline, a product profile
the owner approved through `openajar profile approve`), runs the agent, then the task's checker.

Agents:

- `perfect`: a script that does what the skill teaches. It must finish every task.
- `careless`: a script that takes a plausible wrong turn (reports clean without fixing, ignores
  the dark screens, never proposes). It must finish none. Together they prove the checkers.
- `claude`: `claude -p` with only the studio server (`--strict-mcp-config`), the file tools
  Read, Edit, Write and Glob in the task's folder, and the skill appended to its system prompt.
  Its stream-json transcript is read back into the same trace.

## The tasks

| Task | What it asks | Finished when |
|---|---|---|
| t01_fix_clipped | fix a button cut off by its bar | a fresh ui_check has no layout.clipped, and the fix was confirmed with since |
| t02_two_faults | make a page pass: faint text and small tap targets | no contrast.text and no target.size (in both themes) |
| t03_since | the user names the run before their fix | ui_check with since=<that run>, and the answer says it is fixed |
| t04_why_moved | why did the cards move (a banner was added) | ui_diff was used, the banner is named, no file changed |
| t05_not_running | check a page whose server is down | the answer says it did not load and never calls it clean |
| t06_blank_page | a white screen from a script error | the page renders after the fix |
| t07_brand_color | check against a product profile's brand | no token.color with the profile |
| t08_propose_guideline | a stricter contrast rule from now on | a draft with contrast.text at 7 waits; the approved profile is unchanged; the user is told |
| t09_no_dark_theme | a site with no dark theme | the fault is fixed, and light was checked alone or dark was said to be unchecked |
| t10_theme_switch | dark mode set by an attribute, a fault in dark only | theme_switch was used and the dark fault is fixed |
| t11_image_when_needed | only an image changed | ui_diff was used, the photo is named, no whole-screen image |

Wrong turns are read from the trace (`harness.wrong_turns`): a tool error other than a page that
did not load or render, a lane tool, the same call twice in a row, a second check of the same
screens without `since`, `detail="full"`, an image where the task needs none, a whole screen
where a crop would do. Every wrong turn found in real use should become a task here.

## Adding a task

One file in `tasks/`, named `tNN_<what>.py`, with `TASK = Task(...)`: the prompt as a user would
write it (`{url}` and setup's saved values are filled in), the pages, an optional `setup`, a
`check` that reads the final state, and `perfect` and `careless` scripts. Run the scripted agents
and see perfect finish it and careless fail it before you run a model on it.
