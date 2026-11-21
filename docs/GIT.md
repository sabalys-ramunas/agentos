# Git from lanes and brains

A brain on the node, an `openajar run` job or a command in a terminal lane can clone a private
repository and push a branch overnight, without the deploy key or the token ever being in the lane.
The owner keeps the credential in the node's vault and declares which repositories each workspace
(or lane) may use; the node does the git traffic with the real remote itself, and every push goes
through the same policy as every other act: "Ask me first", approvals, the untrusted-content rule,
freezes and the action log.

## Set it up (on the node, as the owner)

```
$ openajar git key add --host github.com --repo me/app
Made deploy key github-me-app-ssh (SHA256:3q2+7w...) for github.com (me/app).
Add this public key as a deploy key, with write access if lanes should push:
  https://github.com/me/app/settings/keys/new

ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI... openajar-github-me-app-ssh

The private key stays sealed in the vault; nothing can export it.
Then declare where it may be used: openajar git remote allow <workspace or lane> github.com/me/app

$ openajar git remote allow personal github.com/me/app
Declared github.com/me/app for personal: fetch and push, default branch asks.
```

The key is made on the node, in memory, and goes straight into the vault (a machine secret,
`git:<name>`, bound to the host and the repositories). Only the public half is ever printed.
There is no command that prints or exports the private key.

| Command | What it does |
|---|---|
| `openajar git key add --host H --repo O/N [--repo ...]` | make an Ed25519 deploy key; print its public key |
| `openajar git key add --token --host H --repo O/* [--stdin]` | store a token (a fine-grained PAT), read from a prompt or stdin, never argv |
| `openajar git key list`, `show NAME`, `remove NAME` | names, hosts, repositories, public keys, use counts; never a secret |
| `openajar git remote allow SCOPE REMOTE` | let a workspace (`personal`) or a lane (`personal/code`) fetch and push `REMOTE` |
| `... --read-only` | fetch only |
| `... --protect 'release/*'` | more branches where a push always asks (the default branch always does) |
| `... --allow-default-branch` | a push to the default branch is a plain commit |
| `... --url ssh://git@host:2222/o/n.git` | the real remote, when it is not the host's usual address |
| `openajar git remote list [SCOPE]`, `deny SCOPE REMOTE` | what is declared; take a declaration back |

The same commands are under `openajar vault git ...`. A remote is `host/owner/name`, `host/owner/*`
or any git URL; a URL with a password or a token in it is refused.

SSH host keys: the node accepts github.com, gitlab.com and bitbucket.org only with their published
keys (pinned in `openajar.git.keys`). For another host, `openajar git key add --host` reads its key,
prints the fingerprint for you to compare with the host's documentation, and records it in
`OPENAJAR_HOME/git/known_hosts` (or pass the key with `--host-key`). An unknown host key stops the
connection; the node never trusts a key on first sight during a push.

## In a lane or a brain task

Nothing to configure. A command a grant starts in a lane (`terminal_exec`, `terminal_job_start` and so
`openajar run`, `shell_open`) gets `GIT_CONFIG_*` variables, and a brain task gets a private
`~/.gitconfig`, that send every declared host to the node's git gateway:

```
url.http://127.0.0.1:3129/github.com/.insteadOf = https://github.com/
url.http://127.0.0.1:3129/github.com/.insteadOf = git@github.com:
url.http://127.0.0.1:3129/github.com/.insteadOf = ssh://git@github.com/
http.http://127.0.0.1:3129/.extraHeader = x-agentos-git: agtg_...
```

So `git clone git@github.com:me/app.git` and `git push origin feature` work as usual, and the clone
keeps the real remote URL. The bundle of an offloaded session is cloned the same way. What git
prints when the node says no is a sentence that says what to do:

```
$ git push origin main
remote: openajar: this push waits for a person (approval ap_e6bd...): main is a protected branch of
github.com/me/app, so a person approves it. Tell the user; once it is approved, push again with the
same commits. Do not change or split the push to get around it
 ! [remote rejected] main -> main (openajar: this push waits for a person ...)

$ git clone https://github.com/me/other.git
fatal: remote error: openajar: github.com/me/other is not declared for personal/code; the owner runs
`openajar git remote allow personal github.com/me/other` on the node
```

An agent can ask what the node reaches with the `git_remotes` tool (in the `brains` toolset): the
declarations for a workspace, whether a credential exists (its kind, a deploy key's public key and
fingerprint, never the secret) and, for one remote, what the owner still has to run.

## How it works

```
 lane (its own network namespace)          agentosd (root, outside every lane)            the remote
 git ── http://127.0.0.1:3129 ──────────▶  gateway listener (in the lane's namespace)
         x-agentos-git: agtg_...            1. token -> lane + grant
                                            2. declared? (remotes.json)
                                            3. policy: read / commit / irreversible
                                            4. mirror (OPENAJAR_HOME/git/mirrors)
                                               fetch / push with the credential  ───────▶  github.com
                                                 ssh: IdentityAgent = a one-op agent socket
                                                 https: Authorization in GIT_CONFIG_* env
```

- **The listener.** An isolated lane's network namespace has only loopback and the egress
  forwarder (127.0.0.1:3128). The node opens the gateway's listener there too (127.0.0.1:3129)
  the first time a command needs git, and closes it when the lane goes cold, so a connection on it
  can only come from that lane. Brains (which run on the node's network as their own user) and
  lanes on the node's network use the node's loopback listener (a port picked at start, or
  `OPENAJAR_GIT_PORT`). A lane with network `off` (an `openajar run` lane, by default) gets the
  listener too: a declared remote is the owner's yes to reach that repository through the node,
  and the lane still reaches nothing else.
- **The token** (`agtg_...`) names the caller: the lane and the grant that started the command, or
  the brain task and its grant. It is no credential for the remote; it only lets the gateway tell
  callers apart. It ends when the lane goes cold or the task ends, and a lane's token is refused
  on another lane's listener. It is redacted from traces and transcripts.
- **Declared.** The remote must be declared for the caller's lane or workspace; a push needs a
  declaration that allows pushes. A refusal of a known caller is traced (`git_refused`).
- **The mirror.** The gateway keeps a bare mirror per repository. A clone or fetch first brings the
  mirror up to date from the remote (with the credential), then git's own `upload-pack` serves it.
  Requests that wait for the mirror while a fetch runs are served by that fetch (one that began at
  most 5 s before them), not a second one. The mirror serves partial clones (`--filter`) and a later
  fetch of one object by its id, for objects a branch or tag reaches only: an environment's VM clones
  blobless, and the environments service warms the mirror while the VM boots (`GitService.warm`).
  A push goes into the mirror with `receive-pack` (objects checked with fsck), then the node
  classifies and authorizes each ref, and only then pushes to the real remote, each ref with
  `--force-with-lease` on the value the lane saw (several refs atomically). The lease is what lets a
  force push through, so the refspec has no `+`, which would make git skip it: a commit someone else
  pushed meanwhile is never lost. A refused, waiting or failed push puts every ref of the push back in
  the mirror; the remote never saw it. A push that asks for no report of what it did (git always asks
  for `report-status`; only a hand-made request leaves it out) is refused before the mirror takes it.
- **SSH keys** are signed for by a small SSH agent inside agentosd (`openajar.git.agent`) on a fresh
  socket in a 0700 directory of its own, for one operation: it holds the key in memory, offers only
  it, signs only an SSH login as `git` (never a commit or file signature), at most four times, and
  is gone when the operation ends. The node's ssh is pointed at it with `IdentityAgent`, an
  `IdentityFile` of the public half (so root's own keys are never offered), `-F /dev/null`, batch
  mode and strict host key checking against the pinned and recorded keys.
- **Tokens** go in `http.extraHeader` (Basic, the token as the password) through the git process's
  `GIT_CONFIG_*` environment: in no file and no command line.

## What a fetch and a push are

| Git | Effect | So |
|---|---|---|
| clone, fetch, pull, ls-remote | `read` | runs on its own (a revoked or frozen grant is refused) |
| push a new branch or tag, or a fast-forward | `commit` on the lane | runs on its own unless "Ask me first" is on for the lane; after untrusted content it waits for a person |
| force-push (the old commit is not an ancestor of the new one, checked on the mirror) | `irreversible` | always a person, never a standing approval; the grant's ceiling must reach it |
| delete a branch or a tag, move a tag | `irreversible` | the same |
| push to a protected branch (the default branch unless `--allow-default-branch`, and `--protect` globs) | `irreversible` | the same |
| push to anything but `refs/heads/*` and `refs/tags/*`, a signed push | refused | |

**From an agent environment's VM** (docs/ENVIRONMENTS.md) nothing asks a person, after untrusted
content or with "Ask me first" on: a new branch, a fast-forward of any branch (the default branch
included), and a force push or a delete of a branch the environment created itself, is a `commit`
on the environment's lane that runs on its own. Refused instead, asking nobody (`push_refused`:
push a new branch and open a pull request; nothing is pushed and the mirror is put back):

- a force push or a delete of a protected branch (the default branch unless
  `--allow-default-branch`, and `--protect` globs);
- a force push or a delete of any other branch the environment did not create: one that was on the
  remote before its first push, such as a colleague's feature branch;
- moving or deleting a tag.

A branch the environment created is one it pushed first (a push that creates the ref). The
environments service keeps that list on the environment's record, so a restart of the node keeps
it; a branch it deletes is no longer its own. The VM's token reaches its own repository only.
Pushes from anywhere else (account lanes, brains, the owner) keep the rules in the table above.

The lane a push is a commit on: the lane the command runs in. A brain task has no lane of its own,
so its push is a commit on the declaration's lane when the task was given that lane
(`openajar git remote allow personal/code ...` and `brain_run(..., lanes=["personal/code"])`), and
otherwise a commit on no account, which always asks. The approval card shows the remote, every
ref with its old and new commit, whether it is forced or protected, and the first 20 commit
subjects; the approval binds all of it, so the same push after the person's yes goes, and a
different one asks again.

Every fetch is traced (`git_fetch`) and every push (`git_push`, with `result` pushed,
approval_pending, denied or failed), each with the remote, the refs, the caller, the grant and
the vault reference of the credential used; the vault records each read of the credential with
its purpose (`openajar vault status`, `openajar git key list` show use counts).

## Guarantees and limits

What holds, for an isolated lane and for a brain the confinement probe passed:

- The private key and the token are never in the lane's or the brain's environment, files or
  network traffic. Only agentosd opens them, only for the operation, and the key never leaves its
  memory (no key file is written, ever).
- Git reaches only declared repositories through the node; an undeclared host, repository or a
  fetch-only one is refused at the gateway, whatever the vault holds for its host.
- Pushes go through the policy as commit or irreversible, under the caller's own grant, and are
  traced with the remote, the refs and the grant.
- Messages the lane sees are the gateway's own sentences, scrubbed of vault values; the remote's
  raw output is never passed through.

What it does not do:

- **The lane's own network.** The gateway does not need the git host on a lane's egress allowlist,
  and a lane should not have it there. A lane whose allowlist does list the host can reach it
  directly, with whatever credential the agent brings itself; the node's credential is never
  available to that path.
- **A workbench (env0) lane, or a lane on the node's network or filesystem**, runs as the node's
  user without a sandbox: it can read OPENAJAR_HOME and reach the agent socket while a push runs.
  These lanes are the owner's own shell; the guarantees above are for isolated lanes and brains.
- **Commands in one lane** share it: a command of another grant in the same lane, with shared
  processes, can read the first command's token and push as that grant (within what the
  declaration and the policy allow). Give agents with different grants different lanes.
- **What was pushed.** The node checks where a push goes and how (force, protected), not what the
  commits contain. A model's commit can contain anything the model wrote; protect the branches
  that deploy, and review before you merge.
- **The remote's own rules** (branch protection, required reviews) are the remote's; the gateway's
  protected branches are in addition to them.
- **A deploy key with write access** can push to any branch the remote allows. The node narrows that
  to what is declared and routed, but someone who steals the vault and its key file has the key.
- **Fetches are not taint.** Code fetched from a declared repository is not labelled untrusted;
  reading it later through a lane tool follows that tool's own labels.
- **Pushes are held in memory** (up to 1 GB a push), and the mirror keeps what was pushed into it,
  also when the push was refused.
- **The co-sign of a brain task** (`brains/README.md`) reads the bundle's commit with `git ls-remote`
  straight from the remote, so a private repository's commit cannot be bound yet; give such a task
  the commit id.

## For builders

`src/openajar/git/`: `wire.py` (URLs, pkt-lines), `remotes.py`, `keys.py`, `agent.py`,
`upstream.py`, `gateway.py`, `service.py` (the runtime service `git`, built after the vault),
`cli.py`, `tools.py`. Tests: `tests/unit/test_git_*.py` run a real git client through the gateway
against a local bare repository with a real policy and vault; `tests/node/test_git_ssh_node.py` pushes
over real SSH to a local sshd with a key made by `openajar git key add` (run it with
`scripts/node-test.sh git tests/node/test_git_ssh_node.py`).
