---
name: agentos-offload
description: Hand this session's work to an agentos node so it keeps going while the laptop sleeps. Packages the goal, a summary, the git remote and ref and the touched files, checks the node can clone and push the remote (git_remotes), starts it with the node's brain_run tool, and tells the user how to reattach.
argument-hint: "[connection name] [what the node should focus on]"
disable-model-invocation: true
allowed-tools: Bash(git status:*), Bash(git rev-parse:*), Bash(git remote get-url:*), Bash(git log:*), Bash(git diff:*), Bash(git branch:*), Bash(git ls-remote:*)
---

# Offload this session to an agentos node

The node runs a brain of its own (the owner's subscription first, then a local model, a
metered API only within a budget) and keeps working after this laptop closes. You hand
the work over once. Afterwards the node is the only writer of the branch, so you stop
editing it here.

Arguments: `$ARGUMENTS` (optional: a connection name such as `mynode`, and a focus).

## 1. Find the node

`agentos connect <ssh-host>` registers each node as the MCP server `agentos-<name>`, so
its tools appear as `mcp__agentos-<name>__brain_run` and so on.

- No `agentos-*` server: stop. Tell the user to run `agentos connect <ssh-host>` in a
  terminal, then start a new Claude Code session (or run `/mcp`).
- The brain tools are in the node's `brains` toolset, which a default connection does not
  list. If `brain_run` is not among your tools, call `tools_more` with toolset `brains`:
  it returns the brain tools with their arguments and adds them to your list. If your list
  does not refresh, call each one through `tools_call(name, arguments)`; it is the same call.
- `tools_more` lists no `brains` toolset: the node has no brains plugin yet. Say so and stop.
- Several nodes: use the one named in the arguments, otherwise ask which.

Call `brains_list`. At least one brain must be `ready`. If none is, show each brain's
state and reason and stop (`needs_auth` means the owner has to sign the subscription in
on the node).

## 2. Make the work reachable from the node

The node clones a git remote. It cannot see this disk. Look, without changing anything:

- `git rev-parse --show-toplevel`, `git branch --show-current`, `git rev-parse HEAD`
- `git remote get-url origin` (or the remote of the branch's upstream)
- `git status --porcelain` and `git log --oneline @{upstream}..HEAD`

If there are uncommitted changes or unpushed commits, ask the user what to do before
anything else: commit and push the branch (suggest a new branch if this is the default
branch), or offload without those changes. Never commit or push without a clear yes.
Never put secrets (`.env` files, keys, tokens) into a commit or the bundle.

The remote must be `https://...`, `ssh://...` or `user@host:path`. A local path does
not work. The ref is the pushed branch name.

Then ask the node whether it can clone and push that remote: call `git_remotes` (in the
same `brains` toolset) with the workspace and `remote`. It never returns a secret.

- `check.can_fetch` false or `check.can_push` false while the task should push: the node
  holds no deploy key for the repository, or the remote is not declared for the workspace.
  Tell the user to run the lines in `check.owner_runs_on_the_node` on the node (in a
  terminal there, as themselves; you cannot), and to add the printed public key to the
  repository's deploy keys with write access. A public repository clones without a key,
  but the node still pushes only with one. Offload anyway only if the user says so: the
  node then cannot push, and its work stays on the node.
- Pushes from the node go through it, never with a key in the brain: a new branch or a
  fast-forward is a commit, a force-push, a deleted branch or a push to the default
  branch always waits for a person. So have the node work on a branch of its own, never
  the default branch.
- `declared_for` names a lane (`personal/code`) when the owner declared the remote on one.
  Put that lane in `lanes` below, so the node's pushes are commits on it and run on their
  own (its "Ask me first" decides). Without it every push waits for a person, which is
  safe but slow overnight; say so to the user.

## 3. Write the handoff

- **goal**: what the user asked for, in their words, and what "done" means.
- **summary** (at most 20,000 characters): what is done; what is left, in order;
  decisions and why; rules from this session (style, files not to touch, things the user
  said no to); how to check the work (test and lint commands); open questions.
- **files**: every file this session created or changed, as paths relative to the repo
  root (`git diff --name-only <base>...HEAD` plus whatever you touched).
- **taint**: add `"untrusted"` if the summary draws on web pages or other text from
  outside the user; add `"private"` if the work is private. Both keep routing on the
  safe side (local models first); never leave one out to reach a stronger model.

## 4. Start it

Call `brain_run` on that node:

```json
{
  "prompt": "<goal>\n\nYou are continuing work handed off from a laptop session. Read AGENTOS_HANDOFF.md in your work dir first. Work on branch <ref> of <remote>, commit as you go, push the branch when done, and finish with a short report of what changed and what is left.",
  "workspace": "<agentos workspace, for example personal>",
  "lanes": ["<workspace>/<lane>"],
  "context_bundle": {
    "summary": "<summary>",
    "files": ["src/app.py", "docs/notes.md"],
    "git": {"remote": "<remote url>", "ref": "<branch>"},
    "taint": []
  }
}
```

- `workspace`: the agentos workspace the work belongs to; it sets the privacy rules.
  `lanes_list` shows the workspaces. Ask if it is not clear.
- `lanes`: only the browser lanes the work needs, all in that workspace, plus the lane
  `git_remotes` named in `declared_for` (if any). Leave it out for code-only work with no
  such lane.
- `budget` (optional): `{"max_turns", "max_minutes", "max_usd"}`. Set `max_usd` only if
  the user agreed to metered spending.
- If the answer is an approval request, tell the user to approve it on their phone or in
  the panel, and wait. Do not call `brain_run` again.
- The node's git push may later wait for a person too (a push to the default branch, a
  force-push, or any push after the task read untrusted text). `brain_status` and the
  transcript show `waits for a person (approval ...)`; the user approves it in Needs you.

## 5. Tell the user how to come back

Report the task id and the brain that took it, then:

- `brain_status(id)`: state and reason, which brain ran, turns, cost, and
  `transcript_last_seq`.
- `brain_tail(id, since=N)`: the transcript after event N (leave out `since` for the
  latest events).
- `brain_tasks()`: finds the task again from any session, on any computer.
- `brain_cancel(id)` stops it; `brain_resume(id)` continues an interrupted one.

Close with: do not edit this branch here until the node reports done; then pull it.
