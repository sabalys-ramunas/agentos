# New hooks: Runs, Checks, Brand, Needs you previews, Machines

The data layer for the Mono screens (builder J3). Types are in `types.ts`, calls in `client.ts`
(`jobs`, `studio`, `brand`, `vms`), keys in `keys.ts`, hooks at the end of `hooks.ts`. Mock data
lives in `src/mocks/jobs.ts`, `studio.ts` and `brand.ts`, in the `northwind-group` org.

Every hook takes `org` first and an optional `enabled` last. Live mode shows only what the node
serves: a field marked optional below is absent when the node cannot know it, and the screen says
so in plain words.

## Runs (`agentos run` jobs)

Not the routine runs of History (`Run`). Routes: `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/cancel`.

| Hook | Returns | Notes |
|---|---|---|
| `useJobs(org, { lane?, state?, limit? })` | `JobList` `{ available, reason?, jobs: Job[] }` | newest first; polls every 2 s while one is running. `available: false` when the computer runs no terminal lanes |
| `useJob(org, id)` | `JobDetail` (a `Job` plus `output`, the first 64 KB) | polls while running |
| `useJobFollow(org, id)` | `{ job, stdout, stderr, done, error, loading, trimmed }` | follows the output from offset 0 to the end; keeps the last 2 MB of each stream (`trimmed`) |
| `useCancelJob(org)` | mutation `(id) => Job` | needs `operate`; refreshes the list and the run |

`Job.state`: `running`, `passed` (exit 0), `failed` (other code or a signal), `cancelled`,
`timed_out`, `lost` (the computer stopped while it ran). Optional, absent when unknown:
`source` (`{ branch, commit, dirty, told_by: 'client' }`, as the sending computer said) and
`tests` (`{ runner, passed, failed, skipped, total, source: 'output' }`, read from the runner's
own summary line: pytest, vitest, jest, swift test, go test).

There are no run events on the live stream yet: the hooks poll.

Who may: a member whose role has `see` (viewer, approver, operator, admin) reads the runs in lanes of their
workspaces only (another workspace's run is 403); a cancel needs `operate` there (operator, admin).

Mock scenarios (`src/mocks/jobs.ts`): a running vitest run with output still coming, a passed
pytest run with branch and commit, a failed vitest run with 2 failures, a cancelled run, a
timed out run, a lost run, and a run with no test summary and no source (an older client). The
running one finishes after a few reads, so a follow ends on its own.

## Checks (studio)

Routes under `/studio`: `runs`, `runs/{id}`, `baseline?url=&profile=`, `diff?before=&after=`,
`profiles/{ws}/{id}/components`, and two images for an `<img>`:
`studioShotUrl(org, capture, screen, maxWidth?)` and `studioCropUrl(org, run, finding, screen?)`.

| Hook | Returns | Notes |
|---|---|---|
| `useStudioRuns(org, { url?, profile?, limit? })` | `StudioRuns` or `StudioMissing` | test with `isStudioMissing(data)`: `{ available: false, reason, command }` |
| `useStudioRun(org, id)` | `StudioRun`: the row plus `verdict`, `groups` (findings by component), `screen_list`, `problems`, `system`, `images_gone` | |
| `useStudioBaseline(org, url, profile?)` | `StudioBaseline` (`capture: null` when none is kept) | |
| `useStudioDiff(org, before, after)` | `StudioDiff`: per screen the cause sentences and changes | two capture ids; no browser runs |
| `useBrandComponents(org, 'ws/profile')` | `BrandComponents` or `StudioMissing` | component use and look-alikes from the profile's newest system check; `run: null` when none is stored |

Who may: `see` (viewer, approver, operator, admin). A check belongs to the workspace of the profile it was
checked with; a member sees only those of their workspaces, and a check with no profile (with its images,
baseline and diffs) is the owner's alone: 403 for a member.

A finding carries `rects` (optional): its place on each screen in CSS px of the page, so the check page
outlines it on the whole screenshot (scale by the image's width over the screen's width).

Without studio, one run, the baseline, a diff and an image answer the error `unavailable`:
test with `isUnavailable(error)`.

Mock scenarios (`src/mocks/studio.ts`): a check of the Harbor shop with 2 blockers, 3 majors and
minors on 4 screens, one failed screen; a clean check of the same page; a system check of the
`harbor/shop` profile with component use and look-alikes; a baseline for the shop's url and a
diff between the two checks. Studio missing: set `studioMissing` on the org in the mock
(`setStudioMissing(true)` from `src/mocks/studio.ts` in a test).

## Brand (product and business profiles)

Routes under `/brand`: list, `{ws}/{id}`, `proposals`, `{ws}/{id}/approve`, `{ws}/{id}/reject`.
The owner in person reads and decides; the node refuses anyone else (403), `proposals` included.

| Hook | Returns | Notes |
|---|---|---|
| `useBrandProfiles(org, workspace?)` | `BrandProfiles` `{ available, profiles: BrandProfileRow[] }` | product and business profiles only |
| `useBrandProfile(org, 'ws/profile')` | `BrandProfile`: brand (tokens flattened), guidelines, references, environments, and `draft` with the agent's `reason` and a readable `diff` | |
| (in `BrandProfile.draft`) | `tokens` and `guidelines` (optional): the draft's own, so each change is shown beside today's value | |
| `useBrandProposals(org)` | `BrandProposal[]` (`kind: 'brand'`, id `brand:ws/profile@version`) | for Needs you, beside approvals; decide them with `useDecideBrand`, never the approval routes. A member's 403 is read as `[]`, so Needs you shows nothing and no error |
| `useDecideBrand(org)` | mutation `({ ref, version, approve, note? }) => BrandDecision` | always the version the owner read |

Mock scenarios (`src/mocks/brand.ts`): `harbor/shop`, a product profile for Harbor Supply's web
shop with tokens, fonts, components, guidelines and references, and a waiting draft (new accent
colour and a guideline) with the agent's reason; `aster/cafe`, a business profile with no draft.

## Needs you previews

New `PreviewKind`s from the node: `push` (`remote`, `push_refs`: each ref with `kind`, `force`,
`protected`, `commits` and the one-line `log`), `vm` (`server_type`, `vm_profile`, `ttl_minutes`,
`price_hourly_eur`, `max_cost_eur`) and `lane` (a command in a lane: `computer` is the lane,
`command`, `cwd`, `timeout_s`, `synced_files`, `sync_dest`). Lines changed by a push are not
known to the node, so there is no field for them. `Effect` gains `agent` and `vm`. The existing
kinds and how approvals are decided are unchanged.

Mock approvals: a push to a protected branch with force, a VM rental, and a command in a new lane.

## Machines

`FleetNode` (from `useNodes` and `useNode`) gains optional `jobs_running`, `lanes_by_kind`,
`lanes_active` and `approvals_waiting`. Disposable VMs, read only:

| Hook | Returns | Notes |
|---|---|---|
| `useVms(org)` | `VmList` `{ available, reason?, vms: Vm[] }` | `available: false` when no VM provider is set up. The owner's only, as Computers are: a member gets 403 |

Mock scenarios: `hetzner-1` with a run going and an approval waiting; one VM ready with 40
minutes left, one booting.

## Environments

The cloud computers agents rent for one chat (docs/design/environments.md, "Web routes"). Routes:
`GET /environments?state=open|all`, `GET /environments/{id}`, `POST /environments/{id}/close`
(`{discard}`), `POST /environments/{id}/extend` (`{minutes}`). No events carry them: the hooks poll.

| Hook | Returns | Notes |
|---|---|---|
| `useEnvironments(org, state = 'all')` | `EnvList` `{ environments: EnvSummary[] }` | every 3 s while one is starting or closing, else every 15 s; Home, the rail and the page share the `all` list |
| `useEnvironment(org, id)` | `EnvDetail` | every 3 s while it has a machine, not after it is closed or failed |
| `useCloseEnvironment(org)` | mutation `({ id, discard? }) => EnvSummary` | needs `operate`; 409 `unsaved_work` with `dirty_files` and `unpushed` unless `discard` |
| `useExtendEnvironment(org)` | mutation `({ id, minutes }) => EnvSummary` | needs `operate` |

`isEnvOpen(state)` is true for every state but `closed` and `failed`. A command whose id starts with
`job-` (the node's job id) links to its run page; any other is an exec, shown without a link.

Mock (`src/mocks/environments.ts`): three open in northwind-group (`e7k2` working on a dev server on
port 3000, `q3mn` starting and ready after 25 s, `w8tp` idle with unsaved work, so its close answers 409)
and six closed; `?mock=no-envs` has none. Each open one's browser is a lane `env/<id>-browser` among the
org's lanes, so the live view and take over work; the jobs its terminal ran answer `/jobs/{id}`.
