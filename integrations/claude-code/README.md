# openajar for Claude Code

Skills for Claude Code sessions that are connected to an openajar node (run
`openajar connect <ssh-host>` first; it registers the node as the MCP server
`openajar-<name>`).

| Skill | How it starts | What it does |
|---|---|---|
| `openajar-offload` | you type `/openajar-offload [node] [focus]` | packages the session (goal, summary, git remote and ref, touched files) and starts it on the node with `brain_run`, then says how to reattach |
| `openajar-environment` | Claude loads it when the human says "work in openajar" or the task needs a machine | open a cloud VM for the chat, work there, check the page, commit, push, close |
| `openajar-lanes` | Claude loads it when a task uses openajar lanes | the snapshot, act by ref, read-back loop, signals, and take-over etiquette |
| `openajar-studio` | Claude loads it when a task changes a web page | `ui_check`, `since`, `ui_diff` and profiles |

In Claude Code 2.x a skill is also a slash command, so `/openajar-offload` needs no
separate file in `commands/`. The offload skill has `disable-model-invocation: true`:
handing work to a node spends the owner's subscription, so only the owner starts it.

## Tool profiles

A node lists a small set of tools by default (the `core` toolset: the lane loop, 25
tools) plus `tools_more`, which loads any other toolset into the session, and
`tools_call`, which calls any tool by name. The skills know this. To change what a
connection starts with, connect again with `--toolsets`:

```
openajar connect mynode --toolsets brains,files   # core, brains and files
openajar connect mynode --toolsets full           # every tool, as before toolsets
openajar connect mynode --toolsets default        # whatever the node lists by default
```

It puts `?toolsets=` on the MCP URL it registers with Claude Code (and prints for Codex), and
later runs of `openajar connect` keep it. By hand, the same is
`claude mcp add ... "http://localhost:7400/mcp?toolsets=brains,files"`. A controller's combined
URL takes the same toolsets. `mcp_toolsets` in node.toml sets the
default for every connection. The toolsets and their tools are in
[docs/TOOLS.md](../../docs/TOOLS.md#toolsets). A toolset never widens what a grant allows.

## Install

```
integrations/claude-code/install.sh --dry-run   # what it would copy
integrations/claude-code/install.sh             # copy into ~/.claude/skills
integrations/claude-code/install.sh --uninstall
```

It honours `CLAUDE_CONFIG_DIR`. `openajar connect` installs or updates the same skills when Claude
Code is on this computer (`--no-skills` leaves them out, `openajar connect --remove NAME` removes them
with the last connection). The files live in the package, in `src/openajar/client/skills/`, so an
installed `openajar` (no checkout) carries them.

## The brain_run contract the offload skill relies on

From the brains package (`openajar.brains.tools`), registered on the node's MCP server:

```
brain_run(prompt: str, workspace: str, lanes: list[str] | None = None,
          brain: str | None = None, budget: dict | None = None,
          context_bundle: dict | None = None, requires: dict | None = None) -> {id, ...}

context_bundle = {
  "summary": str,                          # at most 20,000 characters
  "files": [path | {"path", "content"}],   # paths relative to the repo; inline files at most 1 MB each, 5 MB total
  "git": {"remote": "https://... | ssh://... | user@host:path", "ref": "branch"},
  "archive_b64": str,                      # optional .tar.gz of the tree, at most 25 MB
  "taint": ["private", "untrusted"]        # can only tighten routing
}
```

The node writes the summary to `OPENAJAR_HANDOFF.md` in the task's work dir, clones the
remote at the ref, and returns at once with a task id. Reattach with
`brain_status(id)`, `brain_tail(id, since=N)` and `brain_tasks()`; stop or continue with
`brain_cancel(id)` and `brain_resume(id)`. Lanes in `lanes` must belong to `workspace`.
They are in the `brains` toolset: the offload skill loads it with `tools_more` when the
connection does not list them.

The lanes skill names the control tools from the take-over contract:
`browser_control(on)`, `browser_request_control(on, reason)` (both in the `control` toolset),
`browser_wait_handback(on, wait_s)` (in `core`), and the errors `human_in_control` and
`agent_paused`.
