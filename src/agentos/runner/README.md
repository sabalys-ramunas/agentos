# agentos.runner: how a flow run stays as safe as a direct tool call

A flow run acts on a real account with no agent watching, so it gets the same checks a tool
call gets, every time, from every entry point (an agent's `flow_run`, a typed `flow_<name>`
tool, a schedule, a page watch, a webhook, the panel API, the CLI).

## Where the code is

- The flow document: `locator.py` (locators, conditions), `steps.py` (step types and the helpers that
  walk them), `urlpattern.py`, `names.py` (ids, lane patterns), `owner.py` (who counts as the owner),
  `flowdoc.py` (FlowDefinition, FlowVersion) and `records.py` (runs, step records, schedules).
  `models.py` re-exports what other packages import.
- The runner: `runner.py` (RunnerService: wiring, reads, re-checks, the lifecycle) with its parts in
  `submit.py` (queueing), `approvals.py`, `gate.py` (right before a run acts) and `dispatch.py`;
  `run_rules.py` holds what they share.
- Execution: `executor.py` (FlowExecutor: traversal, steps, outcomes), `connector_steps.py`,
  `execution.py` (Outcome, Journal, StepFailure, limits) and `readvalues.py` (typed values, the cap).
- `fakes.py` is the reference driver that `agentos flows test` runs flows against.

## Every run carries its grant

- `Run.grant_id` is the grant that created the run. `flow_run` records the caller's grant;
  the panel API and the CLI record the owner grant (`"owner"`); a schedule, watch or webhook
  records its creator's grant when it is added (`Schedule.grant_id`) and every fire runs
  under it.
- The grant is checked twice through `PolicyService.context_for_grant(grant_id, principal=,
  tool=)`: when the run is queued (live, covers the lane, under the ceiling) and again right
  before it runs. A run whose grant was revoked or expired fails with `grant_revoked` or
  `grant_expired` and never touches the lane; an unknown grant is `grant_unknown`.
- A run nobody authorized yet (schedules, webhooks, the panel API) is authorized right
  before it runs, under its grant, for the flow's effective effect: a commit flow runs on its
  own unless it asks first (policy/ROUTING.md, "Ask me first"), and then parks in
  `waiting_approval` until a person co-signs. A run an agent's call let through on its own reads
  the switches again right before it runs, so a switch turned on while it queued still counts.
- On a node with no policy service (unit tests, a bare CLI) no checks run; a policy service
  that cannot resolve stored grants refuses every run.

## The effective effect

A flow's effective effect is the stricter of what it declares and what its steps can do:

| Step | Counts as |
|---|---|
| navigate, wait_for, scroll_to, pause, readback, branch | read |
| click, press, type, fill, select, upload | commit |
| an input step with `"publishes": false`, in a version saved by the human owner | draft |

So an agent can never lower a flow below its steps: its `"publishes": false` marks count for
nothing, and declaring `"effect": "read"` on a flow that clicks Share still makes it commit.
Only the human owner (the panel, the CLI) can vouch that an input step publishes nothing;
re-saving an identical version as the owner records that. A connector the owner installed
counts as the owner's vouch too (see "Connector flows" below). Pay and irreversible flows keep
their declaration (steps cannot tell a payment from a post).

The effective effect is what `ctx.authorize` sees at `flow_run` and at dispatch, what a
flow's typed tool is listed under, and what `flows_list` and the panel show as `effect`
(next to `declared_effect`, with an `effect_note` when the steps raised it). It also decides
honest status: once an input step that may publish has acted, a later failure is `partial`,
never `failed`, so a re-run is not a blind repeat.

## One approval, one run

A run that waits for an approval consumes it when it resumes (`ApprovalStore.consume`). If two
identical runs were parked on the same approval, one runs and the other asks the owner again.
An approval binds to what will happen, including a stated cost and the routing facts.

## Params that are not shown

A param is shown only if the flow marks it, for example a caption the owner reads before
approving: `"caption": {"type": "string", "show": true}`. Every other value is replaced by
`${name}` in proof, the step journal, run errors, logs and the run's description, and
appears as `name=(hidden)` in approval summaries; the approval binds to a keyed digest of the
value. What a resume or a re-check needs verbatim (the page URL, saved values, the resolved
readback) is kept in the store's private `step_state`, like the run's own params.

## URLs from params

A param may fill in the path or query under a literal host (values are percent-encoded). When
a param decides the host (`"url": "${target}"`), the URL must be plain http(s) and inside the
registrable domain of the flow's `domain`; otherwise the step fails with
`url_outside_domain` before the lane loads anything.

## A human holding the lane

Take over (`human`) and stop agent (`paused`) stop a run before the step's input. The step
goes back to pending, the run defers (`error.control` says why) without counting toward
`max_deferrals`, and hand back makes it due at once (the runner registers a lane control
listener). A run is not started at all while the lane is held.

## Workflows

A workflow (docs/WORKFLOWS.md, `agentos.runner.workflows`) is a run of kind `workflow`. Its executor
walks the definition from the top each time it runs, taking finished steps from the journal, so a
restart, a wait, an answer and a retry are the same thing. An action step is a child run submitted
unauthorized, so `_gate` authorizes it right before it acts, like any other run. The child's
`trigger` names the workflow, and `bind_workflow` tells the policy so: the workflow's "Ask me first"
counts too, and the taint overlay reads what filled the step's inputs. A flow whose `operation`
block says `sends` runs only as a workflow's send step. A commit in a loop, or fed from a list, spends
the sending account's daily limit whatever it declares, unless a person marked it `not_to_people`.

## Site APIs

`agentos.runner.siteapis` (docs/SITE-APIS.md) makes a connector out of any site the owner can use.
`models` turns a definition into a Connector document, `discover` proposes one from a page's outline in
pure Python, and `SiteApiService` keeps drafts (`site_api_drafts`) and activates them through
`compile_connector` as `saved_by.kind: local`. There is no second engine: a run of an endpoint is a
connector run. A site with no row in `agentos.node.sites` has `site: web` and `acting_as: lane`. The
`/v1/sites` surface (`http`, `keys`, `cache`, `usage`, `openapi`) keeps its own database.

## Connector flows

`agentos.runner.connectors` compiles each connector operation (docs/design/connectors.md)
into an ordinary flow with the reserved id `connector.<site>.<op>`, lane `*/*`, the hidden
binding params `acts_as_id` and `account_language`, and a `connector` block holding the
operation's contract (hashed into the version like everything else).

- Only `ConnectorService` and `SiteApiService` save `connector.*` ids, passing a `NodeInstall` with
  `saved_by = {"kind": "catalog" | "local", "installed_by": <owner>, ...}`, which saves nothing else
  and which no request body, principal or export can be. `is_owner` counts it
  as the owner, because the owner chose to install a reviewed bundle.
- `flow_save` refuses `connector.*` ids from agents, and `flow_run` refuses to run them: the
  connector layer fills the binding params (who the run acts as), never the caller.
- Compiled flows set `expose_as_tool: false`; connector tools are `<site>_<op>`.

## Surfaces: who a connector run acts as

A lane is a login; a surface (`core.models.Surface`, AUD-01) is who you post as:
`biz-a/facebook-page` acts as the biz-a Page through the lane `personal/facebook`. The owner
binds surfaces (`agentos connector bind`, `POST /api/surfaces`; `SurfaceService`, runtime
service `surfaces`, kept in runner.db).

- Every connector run names its surface (`on`; `runner.submit(surface=...)`, or `lane=` from
  entry points that pass `on`): the tool, the panel API, the CLI, events targets and schedules
  all go through `RunnerService.submit`, which takes the lane from the surface's binding and
  adds `Binding.params()` (`acts_as_id`, `account_language`). A caller that passes a binding
  param is refused. `Run.surface` is set and `Run.output.source.surface` names it.
- Grants, approvals and pace see the surface, not the lane: a grant scoped to workspace biz-a
  runs `biz-a/facebook-page` but cannot touch `personal/facebook` itself. The lane is still held
  one run at a time, and a wrong identity holds the lane.
- The approval content is `{operation, contract, surface, acts_as, params}`: shown params
  verbatim, others by keyed digest, files by the sha256 of their bytes in the lane's inbox. For
  a catalog install `contract` is the contract hash, so a standing approval targeting
  `connector:<site>.<op>@<contract>` survives a repair that only changes locators.
- Pace (the operation's `min_gap_minutes`, `per_hour`, `per_day`, `per_week`) counts that operation's runs
  on that surface that may have posted, when the run is queued and again before it starts,
  ahead of any approval: no approval overrides it. A run over its pace at the start waits.
- Health per operation and surface (`connectors.health`): a run that fails on the contract
  (`connector_contract_broken`, `preflight_mismatch`, `commit_mismatch`,
  `output_contract_violation`) marks it broken and tells the owner once; while broken the tool
  answers `connector_broken`, its schedules skip their slots and its queued runs wait. A new
  implementation clears it.
- A finished run's `untrusted_fields` and `private_fields` label its grant family (`untrusted`,
  `private`) through the policy's taint port; the owner in person adds no labels.
- Connector schedules store the surface and resolve its binding at every fire.

## Connector steps

The steps and locators connectors need (docs/design/connectors.md 4.5) are ordinary runner
features, open to plain flows too. The fake driver (`runner/fakes.py`) is the reference; the
parity cases in tests/unit/test_flow_driver_parity.py run on it and on real Camoufox.

- `attr` on a locator matches a machine attribute: `{"name": "href", "equals" | "prefix" |
  "regex": "..."}`. It narrows the role (or css) candidates; alone it selects every element in
  scope that carries the attribute. Both drivers use one matcher (`AttrMatch.matches`), and
  open shadow roots are part of the scope, in tree order.
- `names` is a per-language map used in place of `name`: `{"en": "Share", "lt": "Bendrinti"}`.
  It is resolved from the `account_language` param before a driver sees the locator; a
  language with no entry is an error, never a fallback.
- `assert_identity` checks who the run acts as: the site's identity cookie (agentos.node.sites,
  no request), or an id read from the URL or an element's `href` through a one-group pattern,
  against `expect` (`${acts_as_id}`, from the binding). Another id fails the run as
  `wrong_account` (an account) or `wrong_surface` (a Page, a company page) and holds the lane
  through the lane manager's `hold` (`needs_human:wrong_account`), with one notification. An id
  that cannot be read fails as `identity_unknown`, without a hold.
- `preflight` runs right before the commit step: the composer's value equals a shown param
  (whitespace normalized), a count of matches, acting as again. Value and count checks poll up
  to `timeout_ms`. A mismatch fails the run as `preflight_mismatch` with nothing posted, then
  the connector's `discard` steps close the composer (best effort, reported in `error.discard`).
  `text_differs` wants exactly one element (even with `nth`) whose text is not the value an edit
  sets; when that is the only failure and the one element shows exactly the value, the run ends
  `already_current` instead (nothing to do, and not a contract breakage), discarded the same way.
  A preflight done before a restart runs again unless the commit step has started.
- The commit step (`connector.commit.step`) always counts as publishing: once it has acted, a
  failure is `partial`, never `failed`, whatever its marks say.
- `extract` reads up to `max_items` records into `save_as` (`{items, count, total,
  truncated}`): each field is an attribute or a text read of the item, or of the one element a
  field locator finds inside it (the item is its anchor). Values are checked against `pattern`
  and `format`, URLs are joined to the page and dropped outside `host`, text is clipped to
  `max_chars`. A value that breaks its pattern or format fails the run as
  `output_contract_violation`. `untrusted` and `private` label fields for the taint rules.
- `for_each` runs read-only steps (no input step at any depth) once per item of a saved list,
  at most `max_items` times, with the item at `${vars.<as>}`. Journal paths carry the
  iteration (`3.each1.0`). Inside a loop an extract's items are appended across iterations and
  any other saved value becomes a list.
- `Run.output` (connector flows only): the operation's outputs mapped from the saved values
  (`connectors.outputs.outputs_from_saved`), checked against the output schema, capped at
  64 KB, with `untrusted_fields` / `private_fields` as output paths (`comments[].body`),
  `truncated`, and `source` (connector, operation, version, contract). A problem fails the run
  as `output_contract_violation` (`partial` once the commit acted). A read operation whose
  output passed the contract is `succeeded_verified`.
- The connector's `discard` steps are validated as real steps when the flow is saved
  (`FlowDefinition.discard_steps`): declared params only, every input `"publishes": false`.
