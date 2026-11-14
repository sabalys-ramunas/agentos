# agentos brains: the offline brain

Your laptop sleeps, the work keeps going. A brain is a model the node can think with: your
own subscription (Claude Code, Codex), a local model on your network, or a metered API key
as the fallback. The node runs the agent loop itself (`claude -p` or `codex exec`) as a
detached task, so closing the laptop or dropping the MCP connection does not stop it, and
you reattach later to read the transcript and the result.

## The ToS boundary

Subscriptions here are **bring-your-own, for your own use, on your own node**. Running your
own Claude or ChatGPT subscription on a machine you control is your agent running somewhere
other than your lap. agentos never pools one subscription across tenants or people, never
resells tokens, and a hosted edition must give every tenant their own credential. Providers
can change their terms; brains is a plugin, so if one closes the door you disable it and the
rest of agentos is untouched (local and metered brains keep working).

## Morning setup (one time, on the node)

```bash
# 1. CLIs (Node.js 22 under /opt/agentos/node, never replacing a system Node) and the
#    unprivileged user brain processes run as
sudo src/agentos/brains/install_cli.sh --with-user

# 2. Claude subscription: create a long-lived token (about a year) and store it.
#    setup-token prints a URL; open it in any browser, approve, paste the code back.
/opt/agentos/node/bin/claude setup-token          # prints sk-ant-oat01-...
agentos brains add claude-max --kind subscription --provider anthropic --stdin
#    (paste the token, then Ctrl-D; it goes into the vault, never argv or logs)

# 3. Check it end to end with one real turn
agentos brains test claude-max --live

# Optional: ChatGPT subscription through Codex (headless device login)
mkdir -p /root/.codex-login && CODEX_HOME=/root/.codex-login /opt/agentos/node/bin/codex login --device-auth
agentos brains add codex-plus --kind subscription --provider openai --auth-file /root/.codex-login/auth.json
rm -rf /root/.codex-login                          # the vault has it now

# Optional: a local model (free, private, works with the internet down)
agentos brains add ollama --kind local --provider ollama --model qwen3:8b
# any Responses-API server on your own network works too (codex 0.166 dropped "chat"):
agentos brains add gpu-box --kind local --provider openai-compatible --base-url http://192.168.1.20:8000/v1

# Optional: a metered fallback (spends real money, only within a budget you approve)
agentos brains add anthropic-api --kind metered --provider anthropic --stdin --price-in 3 --price-out 15
# any OpenAI-compatible chat-completions endpoint, for bounded jobs (see below):
agentos brains add cheap --kind metered --provider openai-compatible --stdin --model some-model \
  --base-url https://api.example.com/v1 --price-in 0.1 --price-out 0.4 --tier 1
```

`agentos brains list` shows each brain's state (`ready`, `cooling_down`, `needs_auth`,
`down`) with the reason, its seats and how full its usage window is. `agentos brains doctor`
shows CLI versions, flag checks, isolation and where credentials live.

Credentials are sealed in the node's vault (`agentos vault init` once; a key-file vault opens
on its own, a passphrase vault asks for its passphrase). Without a vault, `brains add` and
`brains auth` refuse to store a credential unless you pass `--insecure-dev-store`; it then goes
to `$AGENTOS_HOME/secrets/brains-credentials.DEV-ONLY.json` (chmod 600, **not encrypted**, and
labelled as such everywhere) and moves into the vault automatically once one exists.

## Offloading a session from the laptop (handoff)

From Claude Code on the laptop, with the agentos MCP server attached:

1. Commit and push your work in progress to a branch (`git push origin wip/refactor`).
2. Ask the agent to hand off, for example: *"Offload this to the node and keep going."* It
   calls:

```json
brain_run({
  "prompt": "Finish the refactor of the billing module. Make the tests in tests/billing pass.",
  "workspace": "personal",
  "context_bundle": {
    "summary": "Moved invoice logic to billing/invoice.py. 3 tests in tests/billing still fail on rounding. Decided: use Decimal everywhere.",
    "git": {"remote": "git@github.com:me/app.git", "ref": "wip/refactor"},
    "files": ["billing/invoice.py", "tests/billing/test_rounding.py"]
  },
  "budget": {"max_turns": 80, "max_minutes": 120}
})
```

   It returns at once with a task id (`bt_...`) and the routing decision (which brain and
   why). The node clones the branch into the task's working directory, writes the summary
   to `AGENTOS_HANDOFF.md`, and starts the brain with the prompt plus the handoff text.
3. Close the laptop. The task runs on the node under its own supervisor.

Bundles can also carry small inline files (`{"path", "content"}`) or a `.tar.gz` of the
working tree (`archive_b64`, up to 25 MB); git is preferred. Mark private material with
`"taint": ["private"]` and it stays on local brains (or asks you first).

## Reattaching

- `brain_tasks()` lists recent tasks, newest first.
- `brain_status(id)` gives the honest state (`running`, `succeeded_unverified`, `failed`
  with the reason, `interrupted`), which brain ran each attempt, turns, tokens and cost.
- `brain_tail(id, since=N)` returns transcript events newer than `N`; keep the `next` value
  and pass it on the next call. Without `since` you get the latest events.
- `brain_cancel(id)` stops it (SIGINT, then SIGTERM, then a kill of the whole task).
- `brain_resume(id)` continues an interrupted, failed or cancelled task. The same brain
  continues its own session (`claude --resume`, `codex exec resume`); a different brain gets
  the transcript so far as context, and the working directory already has the changes.

From a shell on the node: `agentos brains tasks list`, `agentos brains tasks tail <id> -f`,
`agentos brains tasks cancel <id>`, `agentos brains tasks resume <id>`.

The working directory is `$AGENTOS_HOME/brains/tasks/<id>/work` (a link into the brain
user's home when brains run as `agentos-brain`). The brain reaches a private repository, and pushes,
only through the node's git gateway: the owner makes a deploy key on the node and declares the
remote (`agentos git key add`, `agentos git remote allow`, docs/GIT.md). The key stays in the
vault; the brain's `~/.gitconfig` points git at the gateway, and a push is a commit (a force-push
or a push to the default branch always asks). Without that, pull its work from the node.

"Succeeded" is always `succeeded_unverified`: a model saying it is done is not proof. Check
the result (or let a flow's readback check it) before you trust it.

## How a brain is chosen

Privacy decides the pool, cost decides the order (docs/ARCHITECTURE.md section 4):

1. **Privacy floor.** `agentos brains privacy <ws> local-only` means cloud brains are never
   used for that workspace; with no local brain ready the task waits in the queue.
   `redacted-cloud` runs the prompt and handoff text through the redactor first.
   Private-tainted work goes local, or asks you to co-sign sending it to the cloud.
2. **Capabilities.** Context size, tools, vision, tier, and the grant's brain list.
3. **Cost.** A subscription seat first (already paid), then a local model (free), then
   metered. A busy subscription is waited for rather than paying for metered.
   `agentos brains order a b` sets the order within a kind; `brain_use(name, workspace)`
   makes one the workspace default.
4. **Budget.** Metered brains need a dollar budget (`budget.max_usd`) inside the grant;
   beyond it agentos asks you (the same co-sign as a payment) or refuses.
   A co-sign (private data or spend) names one brain: the one the router would pick, with its
   provider, runner, model and sign-in. It covers only that brain as it is now. A retry on another
   brain, the same name signed in again (`agentos brains auth`), or another model asks again, and
   the task waits in the queue for you. A private-data co-sign kept from before this names no brain,
   so such a task routed again asks again.
   A co-sign for a task with a git bundle binds the commit its branch is at when it asks
   (`git.commit`, read with `git ls-remote`; a short commit id is refused). The clone must be at
   that commit: a branch that moved after the yes fails the task and hands nothing to the brain.
5. **Fallback.** On a 429 or usage limit the brain cools down (until the reset time the CLI
   reported, or with exponential backoff) and the task moves to the next brain. An expired
   credential marks the brain `needs_auth` and sends you a notification saying exactly what
   to run. Crashes, hangs and 5xx errors try the next brain. With nothing available the task
   waits with the reason; with the internet down only local brains run.

Every decision is recorded with a short reason (traces, and `brains/decisions.jsonl`).

## Limits and seats

Each task has limits (defaults in `$AGENTOS_HOME/brains/settings.toml`): turns (also passed
to `claude --max-turns`), wall clock, tokens, dollars (metered only), output volume, and a
stall timeout for hangs. A subscription has `seats` (default 1): concurrent tasks beyond
that wait. Usage is summed over the provider's rolling window and the window's capacity is
learned from when 429s happen, so `window_used_pct` gets better over time.

## Security model

- **The owner token never reaches a brain.** A brain that drives lanes gets a CHILD grant
  from the policy service for just those lanes (no `brain`, no `exec`, at most `commit`),
  revoked when the task ends. Without a policy service, `agentos brains child-token --stdin`
  provisions one. agentos refuses any token equal to the owner's.
- **Credentials** reach only the brain process: through the shim's stdin into its
  environment (Claude, metered Codex) or a chmod 600 `auth.json` in the task's private home
  (Codex subscription, read back afterwards because Codex refreshes it, then deleted). They
  are never in argv, launch files, logs, traces, tool results or the panel API. Transcripts
  are redacted against the exact values injected.
- **Environment** is built from an allowlist, never inherited from the daemon.
- **Brain user and scope.** Brains run as `agentos-brain`, which cannot read `$AGENTOS_HOME`
  (vault, profiles, node token). A root agentosd drops to it itself; an agentosd that runs as
  its own `agentos` user asks the agentos-runas helper (`agentos.runas`, see docs/DEVELOPING.md
  "Running agentosd as its own user instead of root"). Each attempt runs in its own systemd
  transient scope (`agentos-brain-<id>-<n>.scope`, and through the helper the CLI in
  `agentos-brain-<id>-<n>-cli.scope`) with MemoryMax, TasksMax and CPUQuota; stopping a task
  sweeps every process in the scope, even ones that escaped the group. On Linux each brain also
  starts in a PID namespace of its own (unshare, then setpriv for the drop): brains share one
  uid, and without it one brain could read another's `/proc/<pid>/environ`, which holds its
  child grant token. Killing the namespace's init kills everything in it, setsid or not, with
  or without systemd. Not covered: files. Every task's directories under the brain user's home
  belong to the same uid, so a brain could read another task's files there (including the MCP
  config, which holds the child token when Claude's subprocess scrub is on).
- **Confinement check (AUD-08).** Before a brain starts, agentosd runs a probe as the brain
  user, started exactly the way the brain is and as the same identity (one answer,
  `BrainService.brain_identity`, for the probe and the attempt; a `run_as` that names no
  account, or a helper that serves another user, refuses). The probe must have no search
  permission on AGENTOS_HOME and must fail to stat `node.toml` under it, which closes every
  path beneath the home, listed or not (`logs/`, files added later). It must also fail to list
  or read the vault and its key, `node.toml`, `proxies.json`, the channel store and the other
  databases, `logs/`, every lane profile and its cookies, every `vnc/*.xauth` cookie, the
  journal (`system.journal` and agentosd's user journal), `/root/.cache/camoufox` and
  agentosd's own Camoufox cache, and a controller home kept outside AGENTOS_HOME; to connect to
  `vnc/*.sock` and `run/*.sock`; and to open any lane display without the cookie. It must run
  as exactly the brain user's uid, never agentosd's own, and the brain user (as the probe runs,
  and in /etc/group) must not be in agentosd's group, a privileged group (root, adm, sudo,
  wheel, docker, systemd-journal, disk, shadow, lxd, libvirt, kvm) or a group owning anything
  the probe checks in the home. Anything else refuses the task with `brain_unconfined` and says
  what was reachable. What it does not show: that no other file on the machine holds a secret.
  A pass is cached per boot and layout (paths, modes, owners, inodes, live X servers, the
  identity and its groups); failures never are. `agentos brains confinement` runs it now;
  `agentos env confinement <lane>` runs the same probe inside an isolated env lane.
  `confinement = "auto"` (on for Linux, off on a Mac) in `brains/settings.toml`.
- **Links in task files.** A task's work and home dirs are the brain's, and a cloned repo or
  an archive can bring symlinks and hard links that point at host files. agentosd never follows
  one: it resolves every path there from the brain user's home (or its own tasks dir) with
  `O_NOFOLLOW` per component, writes by renaming a temp file over the name, reads back only a
  regular file with one link, and builds a bundle in its own staging dir before copying it in
  (links stay links). An inline file whose path runs through a repo link is refused. The full
  rule is in `core/fs.py` and docs/design/always-on-agents.md 4.4.1.
- **Claude's own subprocess scrub** (`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`, needs bwrap) keeps
  the OAuth token out of the brain's Bash tool. With it on, Claude does not expand `${VAR}` in
  the MCP config, so the narrow child token is written to the brain's private 600 MCP config
  instead (verified on 2.1.317). Codex tool commands get only core environment variables.
- **Settings and hooks** shipped inside a cloned repo are ignored (`--setting-sources user`,
  `--strict-mcp-config`).

Residual risks, stated plainly: a brain with a Bash tool can read files in its own home
(its child token, and for Codex the `auth.json` while it runs); the redacted-cloud level
redacts the prompt and handoff text, not files the brain reads with its tools; full
containment needs the lane network namespace and egress proxy from M5.

## Chat-completions brains

A metered brain on `--provider openai-compatible` calls any OpenAI-compatible chat-completions
endpoint: the base URL and the key are yours. It exists so a bounded job (one drafted reply, one
recovery step) can run on a cheap provider instead of a frontier subscription. Codex dropped its
"chat" wire API, so this brain runs on `chatcli.py`, a small runner of our own with the same
contract as the CLIs: prompt on stdin, key through the shim's stdin path, JSON lines on stdout.

- **One prompt, one reply.** No tools, no vision, no lanes, no files: the child grant token is never
  handed to it, and a context bundle's files are out of its sight. Tasks ask for tools by default,
  so a job for this brain sets `requires: {"tools": false}`; the router answers `no tool use`
  otherwise.
- **The router treats it as metered**: last in the ranking, behind subscriptions and local models,
  in your order among metered brains (`agentos brains order`). It needs a co-sign or a grant that
  allows metered spend, and a dollar budget. It is a cloud brain, even at a private address: a
  local-only workspace never uses it, and private data needs the usual co-sign.
- **How a job gets past a free subscription.** Cost ranking puts a paid-for seat first, so a job
  reaches this brain through a grant with a low `model_tier` (a tier 3 subscription is filtered
  out, this brain is tier 1 unless you say `--tier`), or by pinning it (`brain_run` `brain:`).
- **Prices are required** (`--price-in`, `--price-out`), because the endpoint reports tokens and not
  dollars: without them the budget gate would count nothing. When a server leaves usage out the
  runner estimates it, high. The ceiling goes into the request (`max_tokens`, at most
  `chat_max_output_tokens`, lowered to what the budget still pays for), because usage arrives only
  with the reply; a prompt the budget cannot cover is refused before any call.
- **Failures** come from the HTTP status: down, timeout, 5xx, 404 or a redirect try the next brain
  (redirects are never followed, so the key stays where you put it), 429 cools the brain down
  (honoring `Retry-After`), 401 and 403 ask you to sign in again, any other 4xx fails the task. A
  reply cut off at the ceiling ends `limit_tokens`, never as finished. The runner does not retry:
  a retried POST can bill twice.
- **Registration refuses** plain `http` to a public host (the key and every prompt would cross the
  internet in the clear) and a URL holding a user name, password or query.

Settings (`brains/settings.toml`): `chat_python`, `chat_timeout_seconds`, `chat_max_output_tokens`.
The runner uses the system Python, since the brain user cannot read agentosd's venv.

## Typed-decision brains

A decision brain is not a chat model. Jev answers typed questions about a state object and never
writes text: a yes/no probability (`noul`), one option of a set (`choice`), a level (`score`), a place
in an image (`box`). It exists so a cheap model inside the node can do the small judgments of browser
work (is the page loaded, which of these buttons) that an expensive model should not be paid to make.

```bash
agentos brains add jev --kind metered --provider typesafe --stdin       # $0.042 per million tokens in, output free
agentos brains add jev-or --kind metered --provider openrouter --stdin --price-in 0.05 --price-out 0
agentos brains test jev --live     # one harmless question: prints latency and tokens
```

Each provider pins its URL and default model (`DECISION_PROVIDERS` in `models.py`). The TypeSafe one is
verified live. The OpenRouter one comes from a community repo's source and is not verified: if it turns
out wrong, correct it there, or per brain with `--base-url` (https only, no user name or query).

It cannot be a normal brain. A task is a detached process that runs for seconds to hours; a decision is
a stateless HTTP call of about 300 ms, made hundreds of times per task. So code in agentosd calls it
directly, with no shim, no task record and no transcript:

```python
out = await brains.decide(principal, workspace="personal", state=page_state,
                          questions={"loaded": {"type": "noul", "instructions": "Has the page loaded?"}},
                          must_not_send=[password], budget=Budget(max_usd=0.50), job=task_id)
out["answers"]["loaded"]["noul"]    # plus out["usage"], out["cost_usd"], out["latency_ms"]
```

- **The two kinds never meet.** The router keeps a task off a decision brain and a decision off anything
  else, pinned or not. A decision brain cannot be a default, and the panel's chip does not count it.
- **The router treats it as metered and cloud.** Privacy comes first: a local-only workspace never
  reaches one, private data needs the usual co-sign (pass `taint=["private"]` for what is in the state),
  and a redacted-cloud workspace redacts every string, or refuses when there are images, which cannot be
  redacted. Nothing is queued: `decide` answers now or raises `decision_unavailable`, with `.state` and
  `.retry_after` on the exception. Only a call with a `ctx` can ask a person; a hot path needs a grant
  that already allows metered spend.
- **Must not send.** `must_not_send` is required. A body holding one of those strings, or the brain's own
  key, is refused before any call, and the error names its place in the list, never its value. The check
  reads the decoded strings and the body, and knows a string's URL and HTML forms. It cannot read an
  image: a screenshot with a password in it goes out unseen, so keep images away from secrets.
- **Budget.** Without `job`, `budget` limits the one call. With `job`, an id of your choosing, it limits
  everything spent under that id, across calls and restarts, so one budget covers a whole task. Calls in
  flight hold what they could cost, so parallel calls cannot all spend the same room. A call the budget
  cannot cover is not sent, with the size estimated high. The bill arrives with the reply, so the last
  call may pass the limit by what the estimate missed. Tokens and dollars reach the usage table, `spend
  today` and the grant's meters like any brain's.
- **Failures.** 429, 500, 502, 503 and 529 are tried again with a small backoff: a decision reads and
  changes nothing, so unlike a chat call a retry is safe. A `Retry-After` of more than two seconds ends
  the tries. 429 then cools the brain down (honoring `Retry-After`), 401 and 403 ask you to sign in again,
  and a timeout, a 5xx, a 404 or a redirect try the next decision brain (redirects are never followed).
  Any other 4xx means the request itself is wrong, so it raises `DecisionRejected` and goes nowhere else.
- **An answer is typed, and it is checked:** every question comes back as the type asked, a noul from 0
  to 1, a choice from the options sent. That does not make it true. It is a reading of a state that is
  usually a stranger's text, so the caller decides what to do with it.

Settings (`brains/settings.toml`): `decide_timeout_seconds`, `decide_retries`, `decide_backoff_seconds`,
`decide_max_wait_seconds`. `--seats` means nothing here: no limit is put on parallel calls but the budget
and the provider's rate limit. Every call leaves one line in `brains/decisions.jsonl` with the brain,
tokens, dollars, latency and a hash of the body, never the state or an answer.

## Claude as a helper (`pick`)

A cheap worker sometimes is not sure. Instead of stopping, it can ask a model that writes, and your
Claude subscription can be that model, with no API key:

```bash
agentos brains signin claude          # opens the sign-in link, you paste the code back
agentos brains test claude-haiku --live
```

`signin` runs `claude setup-token` on a terminal of its own, keeps the token it prints, seals it in
the vault and registers `claude-haiku` (Haiku at xhigh; `--model` and `--effort` change that). The CLI
gets a throwaway config directory, so nothing of the sign-in stays on the node. Signing in again
replaces the token. `--stdin` takes a token you already have. It is your subscription, for your own
use on your own node: the ToS boundary above applies.

`BrainService.pick` asks one typed question: the same options the worker had, so a rescue cannot
invent an action, a URL or a selector. Anything but an offered id, or a plain none, is refused.

- **It runs with no tools.** `state` is a stranger's text, so the CLI gets `--tools ""` and the prompt
  fences the page between markers holding a fresh random word. The option labels sit inside that
  fence too, because a button's name is page text. Only the question and the bare ids sit above it.
  A page can sway the pick, never add to what is on offer.
- **It is a brain task**, because a subscription token only runs its own CLI. So confinement, seats,
  the privacy floor and traces are the same, and it takes seconds, not milliseconds: it is for the
  rare step, never every step.
- **It fails fast.** A brain that is signed out, cooling down or down answers at once with
  `decision_unavailable`, and the worker stops for a person. Nothing waits out the timeout for it.
- **It has a budget and says what it cost.** `budget=Budget(max_usd=..., max_tokens=...)` caps the one
  pick, and a metered brain is refused without `max_usd`, as any task is. The result has `tokens` and
  `cost_usd`. A subscription costs $0 and still reports its tokens. Once a task ran,
  `DecisionRejected` and `DecisionUnavailable` carry the same two, because a bad answer is paid for too.
- **`must_not_send` has no default**, as in `decide`. Codex cannot promise to run with no tools, so a
  pick never runs on it.

## Pinned CLI behaviour

Verified on the node on 2026-11-03: **claude 2.1.317**, **codex-cli 0.166.0**. The flags
agentos uses are pinned in `cliprobe.PINNED`; before a run, a brain whose CLI no longer has
one of them goes `down` with a reason naming the flag, the installed version and the version
to pin (`CLAUDE_CODE_VERSION=2.1.317 install_cli.sh --upgrade`). Auto-update is disabled for
brain runs (`DISABLE_AUTOUPDATER=1`) so versions only change when you upgrade.
`tests/node/test_brains_real_cli.py` re-checks everything against the real CLIs.
