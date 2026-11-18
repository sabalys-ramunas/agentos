# Evals: is agentos getting better for the agents that use it?

The environments eval runs the loop an agent really does in agentos (open a machine, change code,
run tests, start a dev server, look at the page, push, open the pull request, close or save and
come back) against a node on this computer, and measures three things: did the work get done (the
experience), how long it took (speed), and how many tokens of tool output the agent had to read
(token efficiency). Run it before and after a change to a tool, a result's shape, a description,
the skill or the machine's setup, and compare.

It lives in `tests/env_eval/`. It needs Docker and the environment image
(`uv run agentos environments setup --pull`, once). Nothing in a run reaches a real repository or
a cloud: the repositories are local bare repositories, GitHub's pull request API is a stand-in, and
every machine is a container. The one thing that leaves this computer is task 5's `npm install`
of one small public package. The free suite costs nothing; a model run costs what its model
reads and writes (see "Paid runs").

The studio has its own eval of the same kind (`tests/studio_eval/`, for `ui_check` and friends),
and the lanes have their benchmarks ([BENCH.md](BENCH.md)).

## Run it

```sh
make eval                                                     # the free suite: the perfect and careless scripts
uv run python tests/env_eval/run.py                           # the same
uv run python tests/env_eval/run.py --task t01 t03            # some tasks (ids or prefixes)
uv run python tests/env_eval/run.py --agent claude --model haiku --task t01 --repeat 3
uv run python tests/env_eval/run.py compare                   # the newest run against the one before
uv run python tests/env_eval/run.py compare --against 5693c8e # ... or against a commit's run
uv run python tests/env_eval/run.py list                      # the tasks
uv run python tests/env_eval/run.py clean                     # what a killed run left in Docker
```

A run prints each task as it goes, then a table, then what changed against the previous run of the
same kind. `--out DIR` keeps the results (`results.json`), every call with its whole result
(`<agent>/<task>-r<n>/calls.jsonl`), each node's log and a model's transcripts
(`session<n>.jsonl`); without it a new temporary folder is used and printed. `--keep` also keeps
each node's home and repositories there. Ports: the node on 7467, the proxy on 7468, the GitHub
stand-in on 7469 (`--port` moves all three).

The free suite takes about six minutes on a Mac with Docker Desktop. It exits 1 unless `perfect`
finishes every task and `careless` none (that proves the checkers), or when a model agent errored.

## What a run does

For each task (and each repeat) the harness, `tests/env_eval/node.py`:

1. Makes a scratch home with agentos's local node (`agentos local serve`'s composition, with the
   environment's browser on the machine), the docker provider, the task's `environments.toml`, and
   an agent grant as `agentos local` gives Claude Code. It is never your `~/.agentos`, and it never
   registers anything with your Claude Code or Codex.
2. Commits the task's fixture (`tests/env_eval/repos/<name>/`) into a new bare repository and
   declares it for environments as `github.com/evals/<name>` through the git gateway
   (`agentos git remote allow env ... --url <path>`, as an owner does), with a made-up token in the
   vault. Pushes go to that bare repository; `environment_pull_request` goes to the stand-in
   (`ghstub.py`), which checks the branch is really there.
3. Starts the node and runs the agent, one session per prompt, each in a fresh empty folder with a
   new MCP session. Every agent reaches the node through the recording proxy (`proxy.py`), so every
   agent is measured the same way, from the MCP side.
4. Runs the task's checker over the real end state, then stops the node and removes every container,
   network and image it made (also after an error or Ctrl-C), and the scratch home.

The docker provider's image is tagged `agentos-eval-<id>:base` for each node, so every save, snapshot
and prepared machine of a run is an image of that repository alone and goes with it.

## The agents

| Agent | What it is | Costs |
|---|---|---|
| `perfect` | A script that does what the `agentos-environment` skill teaches. It must finish every task, and it is a repeatable benchmark of the canonical loop: its time and the tokens of tool output it reads. | nothing |
| `careless` | A script that takes a plausible wrong turn per task (leaves the machine running, sleeps instead of waiting on the port, retries a refused host, opens a new machine instead of resuming). It must finish none. | nothing |
| `claude` | `claude -p` with only the agentos MCP server (`--strict-mcp-config`), no built-in tools (`--tools ""`), the `agentos-environment` skill appended to its system prompt, no user settings (`--setting-sources project` in an empty folder), `--model`, `--output-format stream-json`, and `--max-budget-usd` as each session's hard cap. | the model's tokens |
| `codex` | `codex exec --json` with a scratch `CODEX_HOME` whose `config.toml` names only the agentos server (the token in `AGENTOS_EVAL_TOKEN`, `bearer_token_env_var`), the skill as `AGENTS.md` in its folder, `--sandbox read-only`, and your Codex sign-in linked in. Built to Codex's documented CLI and not yet run against a real Codex. | the model's tokens |
| `cmd` | Any other command, with `--agent-cmd 'mytool --mcp {mcp_config} {prompt}'`. It gets {prompt}, {mcp_config} (a Claude-style MCP JSON file), {url}, {token_file}, {skill}, {model} and {transcript}; stdout is read as stream-json when it is that. | yours |

A model agent's prompt is the task's prompt as a human would type it, and one line asking it to end
with `{"status": ..., "summary": ...}`.

## The tasks

One Python file each in `tests/env_eval/tasks/` (`tNN_<what>.py`, with `TASK = Task(...)`): the
prompt of each session, the fixture repository, the node's settings, the checker, and the two
scripts.

| Task | The human asks | Finished when (the checker reads the end state) |
|---|---|---|
| `t01_fix_push_pr` | fix the failing tests, push the fix on a branch, open the pull request | a new branch is in the bare repository; its tests pass in a clean checkout here; the stand-in has a pull request for it; main is unchanged; every environment is closed and its container gone |
| `t02_dev_server_proof` | start the dev server, check the home page in the browser, keep a screenshot as proof | the node keeps a proof with a title, of `localhost:3000`; the environment's browser showed the app's page; closed and gone |
| `t03_continue_tomorrow` | day one: start, keep the machine, leave a note; day two: "continue Todo export in agentos", push, open the pull request | day two's open resumed the save (no second machine); the note came back naming what was next (the second prompt does not say it); every test passes at the pushed branch; a pull request; closed and gone |
| `t04_prepared_warm` | three times: open the storefront (it has `.agentos/environment.toml`) and say its first price | the app listened at each open; the second booted from the prepared machine (the harness waits for the build); the third took the warm machine (the harness turns `warm = 1` on and waits for one); every answer names the price; closed and gone. The three open times are the point: `open1_s`, `open2_s`, `open3_s` |
| `t05_blocked_host` | on a `limited` network: install the dependencies (npm is allowed) and run a script that calls a host that is not | the script got past its dependency; the node refused the host; the script ran at most twice (no retry loop); the answer names the host and the owner's command (`agentos environments allow --host ...`); closed and gone |
| `t06_checkpoint_rollback` | a risky change: checkpoint first, roll back if the tests fail | a checkpoint before the first edit; a rollback; the tests ran and passed after it; nothing broken pushed; the answer says the tests failed. Needs `environment_checkpoint`: skipped on an agentos without it |
| `t07_video_proof` | on a `limited` network: change the heading, record the page in the browser, push and open the pull request with the recording | a recording kept as proof with a title; the browser showed the new heading; a branch with it; a pull request for it; the recording's files reached the stand-in's `agentos-proofs` branch and the body shows its GIF; closed and gone. Needs `browser_record`. A machine from an image without ffmpeg installs it on the first recording, through the proxy |

A task can name tools it needs (`requires`); on a node without them it is skipped and says so. The
stand-in answers the Git Data API calls the proofs make (blobs, trees, commits, the ref) in memory.

To add a task: a fixture under `repos/` if none fits, then one file in `tasks/`. Run the scripts
(`--task tNN`) and see `perfect` finish it and `careless` fail it before a model runs it. Every wrong
turn seen in real use should become a task or a rule here.

## The metrics

Per task run (`results.json`), and per task in the history as the median over the repeats:

| Metric | What |
|---|---|
| `finished` | the checker passed and no session errored (in the history: the rate over the repeats) |
| `seconds` | the task's wall time, every session, from the first prompt to the checker (the harness's own waits between sessions in t04 are inside it) |
| `ready_s` | from the agent's start to the first result that said its environment is `ready` |
| `first_navigate_s` | the first `browser_navigate`'s own time: on a fresh machine it waited for the browser to start |
| `opens` | each `environment_open`: the time until ready, `new` or `resumed`, the app listening, prepared, warm |
| `calls`, `calls_by_tool`, `seconds_by_tool` | the agent's tool calls (a `tools_call` counts as the tool it ran), and where the time went |
| `tool_errors`, `errors_by_code` | calls that came back as errors, by their code |
| `tokens_read` | the text of every tool result the agent got back, in tokens (characters / 4, the estimate the toolset budget test uses). The same measure for a script and a model |
| `surface_tokens`, `surface_tools` | the tool list the agent's session starts with, estimated the same way (`tests/unit/test_toolsets.py`'s budget measures the same list) |
| `instructions_tokens` | the server's instructions at `initialize` |
| `wrong_turns` | what an agent following the skill would not have done, one line each (below) |
| model: `turns`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `cost_usd` | from Claude Code's stream-json `result` event (Codex: its `turn.completed` events), summed over the sessions |

The wrong turns (`metrics.wrong_turns`), each one a rule the skill states:

- a call refused for want of the `on` its `environment_open` gave (`which_environment`,
  `no_environment`), whether it passed none or another value;
- a lane's name as `on` (`env/<id>`, `env/<id>-browser`, `<id>-browser`), refused or not (Haiku passed
  the open's `lanes.browser` once; a chat whose session holds the machine now reaches it so);
- a file read or written through `lane_exec` (`cat`, `head`, `sed -n`, `sed -i`, a heredoc, `echo >`)
  instead of `lane_file_read`, `lane_file_edit` or `lane_file_write` (a `tail` at the end of a pipe
  reads no file and is fine);
- a `sleep` of 2 s or more in a command (in a loop it counts ten times), or the same job read three
  times without `until_exit` or `until_port`: waiting by hand where the machine waits for you;
- the same call repeated unchanged right after it failed (an error, or a command's non-zero exit), in
  the same chat;
- a new environment opened past what the task needs.

## Reading the compare

Every run appends one line per agent to `bench/evals.jsonl`: when, the commit (and whether the tree
was dirty), a hash of the host's name, the agent and model, the repeats, the tasks run, and per
task the medians. `run.py` then compares it with the latest earlier line of the same agent, model,
host and task set, and `run.py compare` does the same for the newest line (or `--against <commit>`):

```
perfect: 0bd3c1a (dirty) against 5693c8e: 2 better, 1 worse, regressed: t02_dev_server_proof.tokens_read
  t02_dev_server_proof.tokens_read           2216 -> 2790       +25.9%  REGRESSION
  t04_prepared_warm.open2_s                   5.21 -> 3.9        -25.1%  better
```

A metric regresses when it is worse by more than its threshold and by more than its noise floor,
so a run that is one second or a hundred tokens off is not flagged:

| Metric | Threshold | Noise floor |
|---|---|---|
| `finished` | any drop | |
| `wrong_turns`, `tool_errors` | any rise | 0.5 (a median) |
| `*_s` (seconds) | 25% | 3 s |
| `tokens_read` | 10% | 300 tokens |
| `surface_tokens` | 3% | 60 tokens |
| `instructions_tokens` | 5% | 40 tokens |
| `calls` | 20% | 1.5 calls |
| model `input_tokens` / `output_tokens` / `cache_read_tokens` / `cache_write_tokens` | 15% / 20% / 20% / 20% | 2,000 / 500 / 20,000 / 5,000 |
| `turns` | 20% | 2 |
| `cost_usd` | 20% | $0.02 |

`compare` exits 1 on a regression; `run.py --fail-on-regression` does the same for a run. Times on
Docker move with what else the computer does: for a speed claim, `--repeat 3` both sides.

## A/B between two commits

`--ref <commit>` runs the suite on another commit's agentos under this harness: a temporary
worktree of that commit (removed afterwards), whose `src/` the node, the owner commands and the
skill come from. The tasks, checkers and metrics stay this checkout's, so both sides are measured
the same way:

```sh
uv run python tests/env_eval/run.py --ref main --repeat 3       # before
uv run python tests/env_eval/run.py --repeat 3                  # after (this checkout)
uv run python tests/env_eval/run.py compare --against main      # what changed
```

A ref needs the same Python dependencies as this checkout (`uv sync` here first when they differ).

## Paid runs

A model run prints its estimate before it starts, refuses to start when the estimate is above
`--max-usd` (default $2), and gives each session `--max-budget-usd` of `--max-usd` divided by the
sessions, so the whole run cannot spend more. The estimate is a long session of these tasks
(about 400,000 tokens read from the cache, 50,000 written to it, 10,000 more input and 6,000 output)
priced at the dearest model an alias may mean: about $0.14 a session for `haiku` at Haiku 4.5's
prices, $0.43 for `sonnet`, $0.71 for `opus`. The suite is ten sessions, so `--model haiku` on all
of it estimates about $1.40. The real cost comes from the transcript (`cost_usd` in the table), and
is far lower: on 2026-11-18 `haiku` meant Haiku 5.5, and t01 and t02 cost $0.015 together.
For a model the table does not know (a Codex model), give your own estimate of one session with
`--session-usd`; Codex has no hard cap of its own, so keep its runs small.

Model runs use your Claude Code or Codex account and its usage limits: one or two tasks, once, is
enough to see a change; a whole-suite `--repeat 3` is for a release.

## The baseline

The free suite on a Mac with Docker Desktop, 2026-11-18 (`perfect`; `careless` finished none):

| Task | Seconds | Ready s | Opens s | Calls | Tokens read |
|---|---|---|---|---|---|
| t01_fix_push_pr | 18.8 | 6.2 | 5.7 | 8 | 2,161 |
| t02_dev_server_proof | 29.8 | 4.8 | 4.8 | 8 | 2,220 |
| t03_continue_tomorrow | 26.1 | 5.1 | 5.1 new, 4.6 resumed | 11 | 4,030 |
| t04_prepared_warm | 86.5 | 17.9 | 17.9 cold, 5.3 prepared, 3.3 warm | 12 | 4,687 |
| t05_blocked_host | 17.2 | 5.5 | 5.4 | 5 | 2,678 |
| t06_checkpoint_rollback | skipped (no checkpoints on this branch) | | | | |

Each session starts with 38 tools of about 9,480 tokens and instructions of about 900. The first
`browser_navigate` of a machine takes 10 to 14 s (its browser starting), the largest single wait
after a cold open. `--agent claude --model haiku` (Haiku 5.5) finished t01 and t02 in 32 s and 47 s
for $0.015, with 5 wrong turns between them: files read with `cat` through `lane_exec`, and a
`browser_navigate` with `on` set to the environment's `lanes.browser` instead of its `on`.

After the fixes for those (2026-11-18, `envs/axfixes`): `perfect` and `careless` still finish all and
none; t05 reads 1,668 tokens instead of 2,992 and makes 4 calls instead of 5 (the refused host is in
the command's own result, no `environment()` needed); every other metric within its noise. The
browser starts during the open, so it is ready by the time an agent that thinks between calls
navigates: Haiku's first `browser_navigate` took 3.3 s instead of 15.6 s, and t01 and t02 took 30 s
and 30 s for the same $0.015, with 3 wrong turns (the `cat`s, before the hint) and no tool error. A
script that navigates a second after the open still waits for most of the start (t02: 6.8 s, was
7.7 s). `t07_video_proof` finishes in 45 s, 25 of them installing ffmpeg on this image.
