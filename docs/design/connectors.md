# Design: sites as typed connectors

Status: design, 2026-11-04. A big bet. Builds on the runner
(`src/agentos/runner/`), the flow driver (`src/agentos/node/flow_driver.py`) and the site
signals table (`src/agentos/node/sites.py`). Related audit items: AUD-01 (surfaces), AUD-03
(commit gate), AUD-04 (first-party post flows), AUD-12 (expected account), AUD-13 (pace),
AUD-22 (public visibility), AUD-39 (account languages), AUD-46 (partial runs).

## 1. Summary and recommendation

A **connector** is a first-party, reviewed, versioned package of data that describes one
site as a small set of typed operations: `instagram.post_photo`, `facebook.page_post`,
`linkedin.company_post`, `reddit.post_text`, `reddit.list_comments`, `shopify.list_orders`.
Each operation has an input schema, an output schema, an effect class, an "acting as" check,
structural locators, a commit contract, a readback contract, the signals it expects, and
pace limits. An agent calls `instagram_post_photo(on="biz-a/instagram", image=..., caption=...)`
and the node runs it against the owner's own logged-in lane with no model in the loop.

Recommendation, in five decisions:

1. **Compile connectors into the runner's flows; do not build a second engine.** A connector
   operation compiles to an ordinary `FlowDefinition` (reserved id `connector.<site>.<op>`).
   Runs, approvals, the step journal, proof of done, honest statuses, holds, schedules,
   webhooks and grant re-checks all come for free, and the 35 locator parity cases keep
   meaning what they mean today. The connector layer adds what flows lack: a contract
   (outputs, acting as, commit and readback shape), a binding to surfaces, catalog
   provenance, and health.
2. **Connectors are data, never code.** No Python, no page JavaScript, no regex that runs
   anywhere but the node's own matcher. A connector cannot do anything a flow cannot do, so
   the catalog is not a code supply chain, and review is review of a diff of steps.
3. **Split contract from implementation.** The contract (inputs, outputs, effect, acting-as
   kind, commit request shape, readback kind) has its own hash. A repair that only changes
   locators keeps the contract hash, so standing approvals and schedules survive a repair; a
   contract change invalidates them and waits for the owner.
4. **Break loudly, before the commit, never half way.** Every commit operation runs a
   preflight (every locator up to the commit resolves exactly once, the composer holds exactly
   the approved text and media, acting as is right) and, once AUD-03 lands, the commit gate
   checks the real publish request against the approved content before it leaves. Any
   mismatch stops the run with nothing posted, holds the surface and sends one notification.
   No model improvises a commit step at run time, ever.
5. **Start with the four posting connectors from AUD-04**, verified only with the owner on
   throwaway accounts, then health and canaries, then reads with safe extraction, then the
   signed catalog channel, then official API backends behind the same operations.

This is the "self-hosted Zapier whose integrations are your logged-in browsers" from the
owner's bet, kept inside principle 8: first-party, reviewed, signed, no open store.

## 2. Goals and non-goals

### Goals

- An agent (or a schedule, a webhook, the panel) calls a typed operation on a surface and
  gets a typed, verified result, with no model tokens spent on execution.
- The owner's own logins only: every operation runs on a lane the owner set up and bound to a
  surface they own or were delegated (principles 1 and 2).
- Commit operations carry exact-content approvals, an acting-as check and a readback of the
  live object; they can never post a partial or wrong thing.
- A site change is detected before a real post when possible (canaries, preflight), and
  always surfaces as a clear failure with the failing step, expected versus found, and a
  screenshot: never a silent success, never a retry storm.
- Each repair makes the connector better for everyone running it, through a reviewed,
  tested, signed update, without the owner's data leaving their node.
- When the site has an official API the owner can use, the same operation runs over that API
  instead, with the same contract.

### Non-goals

- **Not scraping at scale.** Operations read the owner's own objects (their posts, comments
  on their posts, their orders, their notifications). No "search users", "list followers of
  X", "export a competitor's catalog". Reads are capped per call and paced per day. This is a
  catalog review rule, not only a default.
- **Not someone else's accounts.** No operation logs into, creates or acts on an account the
  owner does not control. No account creation operations at all.
- **Not an open marketplace.** No third-party connectors in the catalog. The owner's local
  overrides are their own and never published by agentos.
- **Not a replacement for an API that exists.** Where an official API is usable by a single
  owner (Shopify Admin API, Graph API for Pages, Reddit API), the catalog recommends it and
  the browser backend is the fallback.
- **Not CAPTCHA solving or detection evasion.** A challenge is a hold for a human (AUD-07),
  exactly as today.
- **Not DMs or messaging in v1.** Sending a message is close to irreversible and reading
  inboxes is the heaviest private-data and injection surface. Revisit after the trifecta
  rules have run in production.

## 3. Stories

### 3.1 biz-a posts a photo with a caption every Tuesday to three surfaces

Surfaces (AUD-01): `biz-a/instagram` (its own login), `biz-a/facebook-page` (acts as the
biz-a Page through `personal/facebook`), `biz-a/linkedin-company` (acts as the biz-a company
page through `personal/linkedin`).

1. Monday, in Claude Code: "Draft Tuesday's post for biz-a and schedule it everywhere." The
   agent writes a caption and puts the photo on the node with `files_put`. In phase 1 it
   adds three cron schedules (one per surface) with the same params; from phase 2 it makes
   one `post_bundle` call with `at="2026-11-10T10:00"`. Either way the result is
   `approval_pending`.
2. The owner's phone shows one card (three in phase 1, one per schedule): the photo, the caption as it will appear, and three
   rows "Instagram as @biza.coffee", "Facebook as biz-a (Page)", "LinkedIn as biz-a
   (company)". The text is node-written from the params, never model prose. Approve.
3. Tuesday 09:40 (phase 2): a rehearsal canary runs for any operation that has not succeeded
   in seven days or whose version changed: it opens the composer, resolves every locator up
   to the commit step, checks acting as, and discards the draft. All green.
4. 10:00, spread by schedule jitter and each lane's pace limits: each run wakes its lane,
   checks the signal and the account identity (AUD-12), switches to the Page or company
   page, uploads the photo, types the caption, runs the preflight (composer text equals the
   approved caption, one image attached, acting as biz-a), clicks Share. The commit gate
   (AUD-03) sees the publish request, parses caption and media, and matches them to the
   approval.
5. Readback: the live post URL is read from the site's own state; for Instagram a second,
   cookieless read through the same proxy (AUD-22) confirms it is publicly visible. Each run
   ends `succeeded_verified` with `{post_url, post_id}`.
6. If Facebook redesigned its composer overnight, step 4 stops at the first locator that no
   longer resolves, before any input that can publish: the run is `failed` with
   `connector_contract_broken`, the surface `biz-a/facebook-page` is held for that operation,
   one push says "Facebook Page posting changed; nothing was posted; Instagram and LinkedIn
   went out", and the operation shows `broken` in the panel until a repaired version is
   installed.

### 3.2 List new Reddit comments on our posts

1. An agent (or a page watch from the events layer) calls
   `reddit_list_comments(on="biz-b/reddit", since="2026-11-03T00:00Z", max_items=50)`.
2. The run is `read`: no approval. The connector opens the account's own submitted posts
   (bounded to the newest N posts) and extracts comments structurally: comment id, post id,
   author handle, created time from a machine attribute, permalink, and the body.
3. The result is typed and capped: ids and times are validated against patterns, permalinks
   must sit under reddit.com, the body is clipped and listed in `untrusted_fields`. The taint
   ledger records `untrusted_content` for the grant family, so if the agent later tries to
   send data outside its egress allowlist, policy asks first.
4. The agent drafts replies; a reply is a separate commit operation with its own approval
   (phase 3).

### 3.3 Read today's orders

1. `shopify_list_orders(on="biz-c/shopify", created_since="today")`.
2. The binding for `biz-c/shopify` uses the **api** backend: a Shopify custom app token in
   the vault, resolved by the node into the auth header only. The browser backend (the
   logged-in admin orders table) is the fallback for a store where the owner cannot create a
   custom app.
3. Output: order number, created time, total and currency, financial and fulfillment status,
   item count; customer name and note are marked `private` and `untrusted`. The run labels
   the family `private_data` and `untrusted_content`, so the lethal-trifecta rule applies to
   whatever the agent does next.

## 4. Architecture

### 4.1 Pieces

```
connectors/<site>/connector.json   catalog source in the repo (data only), with fixtures/
      |  agentos connector compile (lint strict, contract hash, implementation hash)
      v
signed bundle  ->  node: ConnectorService (verify, install, bindings, health)
      |                 |  compiles each operation to a FlowVersion "connector.<site>.<op>"
      |                 v
      |            runner (FlowService, executor, proof, schedules)  ->  FlowDriver (Camoufox)
      |                 ^                                               ->  ApiBackend (vault auth header)
      |                 |  binding: surface -> lane, acts_as, backend
MCP tools <site>_<op>(on=surface, ...)   policy: effect, content-bound approval, pace, taint
```

### 4.2 The connector

A connector is one JSON document per site (shown as YAML here for reading), validated by a
pydantic model the way `FlowDefinition` is, and hashed canonically.

```yaml
connector: instagram
version: 1.4.0            # semver of the implementation
site: instagram           # agentos.node.sites key: hosts, session and identity cookies, URL rules
domain: instagram.com
requires: {agentos: ">=0.9", engines: [camoufox]}
backends: [browser]       # later: graph_api
account_languages: [en, lt]   # languages the fixtures and canaries cover (AUD-39)
operations:
  post_photo:
    title: Post a photo with a caption
    effect: commit
    input:
      type: object
      required: [image, caption]
      properties:
        image:   {type: string, format: agentos-file, media: [image/jpeg, image/png], max_bytes: 8000000}
        caption: {type: string, maxLength: 2200, show: true}
    output:
      type: object
      required: [post_url, post_id]
      properties:
        post_url: {type: string, format: uri, x-agentos-host: instagram.com}
        post_id:  {type: string, pattern: "^[A-Za-z0-9_-]{5,40}$"}
    acting_as: {kind: account}             # the lane's own account, from the identity cookie
    pace: {min_gap_minutes: 90, per_day: 3, active_hours: true}
    signals:
      benign: [save_login_info, notifications_prompt]   # each with a reviewed, structural dismiss recipe
      hold:   [captcha, checkpoint, logged_out, blocked, wrong_account]
    commit:
      step: share                          # label of the only step that may publish
      preflight: [composer_text_equals_caption, media_count_1, acting_as]
      request:                             # the AUD-03 commit gate entry (patterns illustrative)
        method: POST
        url: "^https://www\\.instagram\\.com/api/v1/media/configure/"
        text: "form:caption"
        media_count: "count:upload_id"
    readback:
      live: {save_as: post, outputs: {post_url: "post.url", post_id: "post.id"}}
      public: cookieless                   # AUD-22: visible to a signed-out visitor
    rehearsal: until_commit                # canaries may run up to the commit, then discard
    discard: [ ... steps that close the composer without saving a draft ... ]
    steps:   [ ... flow steps with structural locators ... ]
```

The parts, and the rule each one carries:

- **Site and version.** `site` must be a key in `agentos.node.sites.SITES`, so identity and
  signals come from the one table. `version` is semver for the implementation; the compiler
  computes two hashes: the **contract hash** over operation name, input and output schemas,
  effect, acting-as kind, commit request shape and readback kinds, and the **implementation
  hash** over everything (it equals the compiled `FlowVersion.version`).
- **Typed operations.** `input` is a JSON schema (draft 2020-12, as flow params are today)
  with the same `show` marks: a caption is shown and bound verbatim, anything not shown is
  bound by a keyed digest. File params use `format: agentos-file` and are checked on the
  node (type sniffed from bytes, size, EXIF GPS stripped per AUD-40) before the run starts.
  `output` is a JSON schema the result must validate against (section 4.10).
- **Effect class per operation.** Declared, and checked at compile time against the steps'
  effective effect (`steps_effect`): a declared `read` whose steps click is a compile error,
  not a silent raise. `pay` and `irreversible` operations (delete a post, refund an order)
  are out of the v1 catalog.
- **Acting as.** `kind: account` checks the lane's identity cookie against the surface's
  pinned `AccountRef.user_id` (sites.py `check_account`, AUD-12). `kind: fb_page`,
  `li_company`, `ig_business` check the identity the composer will post as, from structure
  only: a URL that carries the asset id (`asset_id=<page id>`, `/company/<id>/admin/`), or the
  `href` of the composer's actor link. The id comes from the **binding**, never from the
  caller, so an agent cannot redirect a post to another page by passing a different id.
- **Locators: structural and language-independent.** Role plus anchors (`within`, `own`)
  stays the base. Connectors add one matcher the driver does not have yet, `attr`, which
  matches a machine attribute (`href`, `type`, `data-*`, `datetime`, a custom element's
  attribute) by equals, prefix or regex, and they treat accessible names as the last resort.
  When a name is unavoidable, it is a per-language map (`names: {en: "Share", lt:
  "Bendrinti"}`) picked by the surface's recorded account language (AUD-39). The strict lint
  rejects a commit-path locator that has only a name and no structural part.
- **Commit contract.** Exactly one labelled step may publish. Every input step before it must
  be owner-vouched `publishes: false` (the catalog signature plus the owner's install counts
  as the owner's vouch, section 4.4). The preflight runs right before it. The `request` block
  is the site's row in the AUD-03 commit gate table: how to recognize the publish request and
  pull text and media out of its body.
- **Readback contract.** What proves the object exists, read from the target's own state
  (the post's own permalink and author link, not a toast), and which outputs it yields. For
  Reddit and Instagram, `public: cookieless` adds the AUD-22 signed-out check: after the live
  readback passes, the permalink is opened in a throwaway browser with no cookies, the lane's
  fingerprint and its proxy, and the object's text must show there (else `succeeded_unverified`,
  `visible_only_to_author`, re-checked). `public_check: {url, text}` names the saved permalink and
  the text (`param:<name>` or a saved value); by default the live readback's saved `url` and `text`
  (agentos/runner/signed_out.py, agentos/node/signed_out.py).
- **Signals expected.** `benign` names interstitials the operation knows (Instagram's "save
  login info", a cookie banner) with a reviewed structural dismiss recipe; `hold` names
  signals that stop the run and hold the lane (AUD-07). Anything not listed is treated as
  `hold`. New URL rules a connector needs go into `sites.py` through the same review, so there
  is still one signal table.
- **Pace limits.** Per-operation defaults (posts per day, minimum gap, active hours in the
  workspace timezone, reads per hour and items per call). Policy enforces the stricter of the
  connector's pace and the lane's `LaneSpec.limits` (AUD-13) before approval routing; an
  approval cannot override pace.

### 4.3 Bindings: where an operation runs

A binding ties a connector to a surface. It is the AUD-01 surface plus the backend choice.

```yaml
- surface: biz-a/instagram
  connector: instagram
  lane: biz-a/instagram
  acts_as: {kind: account, user_id: "51234567"}
  backend: browser
  account_language: en
- surface: biz-a/facebook-page
  connector: facebook
  lane: personal/facebook
  acts_as: {kind: fb_page, id: "100089000000001", name: "biz-a"}
  backend: browser
- surface: biz-c/shopify
  connector: shopify
  backend: api
  credential: vault:biz-c/shopify-admin-token     # a vault reference, never the value
  fallback: {backend: browser, lane: biz-c/shopify}
```

Grants, approvals and schedules target the surface; the binding decides the lane. Workspace
templates (`social-6`) create bindings along with lanes. A grant scoped to workspace `biz-a`
sees `biz-a/facebook-page` even though its lane lives in `personal`, which is the point of
AUD-01.

**On the commit gate too** (built 2026-11-05, docs/design/egress-guard.md 8.10). An agent that
drives the raw lane with `browser_act` instead of a connector tool goes through the commit gate,
which reads who each publish acts as from the request (`agentos.node.acting`: Facebook's
`actor_id`, a LinkedIn author or organization urn, an Instagram business account id, a Medium
publication) and resolves it against the surfaces: a Page whose surface posts through this lane
is authorized on that surface (its workspace must be in the grant, its Ask me first switch and
its lane's pace apply, and the card says "Posts as biz-a (Page)"); the lane's own account is the
lane's, as before; anything else (an id no surface names, a Page of another lane, an identity the
gate cannot read on Facebook) always asks a person, and a schedule, a flow grant or a flow running
on its own is refused it. So the surfaces are the list of identities agentos knows: a Page a
login manages that has no surface is asked about every time, and listed in the lane's gate log.

### 4.4 Compilation from flows

The authoring path reuses what exists:

1. A maintainer records the job on a throwaway account (`flow_record_start` and
   `flow_record_stop`), or writes steps by hand. The recorder already picks durable names
   (`name_variants`) and refuses snapshot refs.
2. They turn the draft into an operation: replace names with structural locators where
   possible, label the commit step, add the preflight and discard steps, map readback
   `save_as` values to outputs, write the input and output schemas.
3. `agentos connector compile connectors/instagram` validates the document, runs the strict
   lint, computes both hashes and emits one `FlowDefinition` per operation:
   `id = connector.instagram.post_photo`, `lane = */*` (the binding resolves the real lane),
   `domain` from the connector, params = the input schema plus hidden binding params
   (`acts_as_id`, `account_language`) that only the connector layer fills.
4. The strict lint, beyond today's `lint()`: every commit operation has a preflight, a
   labelled commit step, a readback with outputs, and a request block (warning until AUD-03
   lands, error after); no css-only or name-only locator on the commit path; `nth` only with
   an anchor; no param decides a host; no input step inside a `for_each`; output schema
   covers every saved value it exports; fixtures exist for every operation and account
   language listed.
5. On the node, `ConnectorService` installs a verified bundle by saving each compiled
   definition with `saved_by = {kind: "catalog", id: "agentos", signer: <key id>,
   installed_by: "owner"}`. The runner treats this as owner-saved for `publishes: false`
   marks, because the owner chose to install a signed, reviewed bundle. An agent's
   `flow_save` can never write a `connector.*` id (it would land as a proposed override,
   section 4.6).

An owner's existing flow can be promoted into a local connector operation the same way
(`agentos connector adopt <flow id>`), so nothing recorded so far is wasted.

### 4.5 New runner and driver capabilities

Connectors need four things flows do not have today. Each is generic, useful to plain flows
too, and kept inside the existing safety model.

| Capability | What | Rule |
|---|---|---|
| `attr` locator matcher | match an element by a machine attribute (`href` regex, `type`, `data-*`, `datetime`, custom element attributes) | implemented in the fake and the Playwright driver with new parity cases; read only, never marks the page |
| `assert_identity` step | check acting as: the identity cookie (sites.py) or a URL or `href` carrying the bound asset id | fails with `wrong_account` or `wrong_surface` and holds the lane; a read step |
| commit marker and preflight | a step labelled `commit` plus checks run right before it (value of the composer equals the shown param after whitespace normalization, media count, acting as) | a failed preflight is `failed` with nothing published; the step before the commit is the last point a run can stop as `failed`, after it only `partial` or success |
| `extract` and bounded `for_each` | read N items under an anchor into typed records; iterate over a saved list | read only: the compiler rejects input steps inside `for_each`; `max_items` required; output validated (section 4.10) |

Sketch of an extract step (Reddit comments, attribute names illustrative):

```yaml
- type: extract
  save_as: comments
  items: {css: "shreddit-comment", within: {role: main}}   # every match is an item, no ambiguity error
  max_items: 50
  fields:
    id:         {attr: thingid, pattern: "^t1_[a-z0-9]+$"}
    post_id:    {attr: postid,  pattern: "^t3_[a-z0-9]+$"}
    author:     {attr: author,  pattern: "^[A-Za-z0-9_-]{3,20}$"}
    created_at: {locator: {role: time}, attr: datetime, format: date-time}
    permalink:  {attr: permalink, host: reddit.com}
    body:       {locator: {css: "[slot=comment]"}, read: own_text, max_chars: 2000, untrusted: true}
```

A rehearsal mode in the executor runs an operation up to (not including) its commit step and
then its `discard` steps. It is how canaries test commit operations without posting
(section 4.7).

### 4.6 The catalog

- **First-party.** Source lives in the repo under `connectors/<site>/` with fixtures and
  contract tests. Anyone can read it; only maintainers merge it.
- **Reviewed.** Every change is a pull request with: the step diff, the contract hash before
  and after (a change is called out at the top), fixture runs on the fake and on real
  Camoufox with route interception, and, for commit operations, a canary
  run on a throwaway account with the run id and proof attached. Two reviewers for a commit
  operation change.
- **Signed.** A release builds `connectors-<date>.bundle`: the compiled documents plus an
  index of `{site, version, contract hash, implementation hash}`, signed with an ed25519
  catalog key whose public half ships in agentos. The node verifies before install and
  refuses unsigned or downgraded bundles (a lower version of the same contract, unless the
  owner forces a rollback).
- **Versioned.** Nodes keep every installed version (flow versions are immutable already), so
  a schedule can pin one and a rollback is a pointer move. Each operation shows its version,
  contract hash and health.
- **Local overrides.** The owner can override an operation locally (`agentos connector
  override instagram.post_photo --from-flow ...` or in the panel). An override is
  `saved_by` the owner, shows `local` next to the tool, is never replaced by a catalog
  update (the panel shows "catalog has 1.5.0; your override is based on 1.4.0" with a diff),
  and binds approvals to its full implementation hash, not the contract hash.
- **Agent-proposed repairs.** When an operation breaks, the owner's agent may record a fix on
  the lane and `flow_save` it under the connector's id. That lands as a *proposed* override:
  inactive, listed under Needs you with the step diff and the failure it addresses. The owner
  activates it in the panel. It is never auto-activated, and never for a commit operation
  without a rehearsal passing first.
- **How a repair ships.** (1) A failure produces a repair report on the node (below). (2) A
  maintainer reproduces it on the throwaway account, fixes the locators, refreshes fixtures.
  (3) CI runs fixture tests on the fake and on Camoufox. (4) A canary run on the throwaway
  account (rehearsal for read, full post and readback for commit, then delete by hand).
  (5) Review, merge, signed bundle. (6) Nodes with update checks on pick it up; a repair that
  keeps the contract hash installs automatically and clears `broken`; a contract change waits
  for the owner.
- **Telemetry-free self-host.** By default a node sends nothing anywhere. Update checks are
  off until the owner turns them on (they reveal the node's IP to the release host; the
  panel says so); `agentos connectors update --from <file>` works fully offline.
- **Opt-in repair sharing.** A repair report contains: connector, operation, version,
  failing step and locator, the signal, engine and Camoufox versions, account language, and a
  text-free structural skeleton of the region around the failing locator (tags, roles,
  allowlisted attribute names, no attribute values except allowlisted structural ones, no
  text nodes, no cookies, no URLs beyond the path pattern). A screenshot is never included
  unless the owner views it and attaches it. Phase 4 exports the report as a file the owner
  can send; an upload channel comes only if owners ask for it, and stays opt-in per report.

### 4.7 Contract tests and canaries

Three layers, cheapest first:

1. **Fixture tests (CI, every change).** Each operation ships captured page fixtures per
   account language (DOM saved on a throwaway account, text replaced, served under the real
   hostname by route interception). The compiled flow runs against the fake driver and real
   Camoufox; the preflight, readback and extract outputs must match expected values. This
   catches our own regressions, not site changes.
2. **Read-only probes on the owner's node (scheduled, paced).** For read operations, a probe
   is a normal small run (`max_items: 1`) that must validate against the output schema. For
   commit operations, a probe is a **rehearsal**: run to the commit step, resolve every
   locator, check acting as, run `discard`. Probes are counted against the lane's pace and
   run at human hours, because opening a composer is account activity. Default triggers: a
   new version installed, no success in seven days, or 20 to 40 minutes before a scheduled
   commit when either of those holds. Never more than one probe per surface per day.
3. **Throwaway-account canaries (project side, and optionally the owner's).** The full
   commit, readback and a manual delete on dedicated throwaway accounts in a separate
   `canary` workspace on its own device and proxy, never linked to real accounts (different
   phone and recovery email; AUD-02 device plan). The project runs these weekly per commit
   operation and on every release candidate. An owner may run them too; nothing requires it.

Each operation on each surface has a health: `healthy`, `degraded` (passed, but through a
fallback such as a css fallback or a second-language name; a repair is due), `broken` (a
probe or real run failed on the contract), `held` (a bad signal held the lane; not the
connector's fault). Health is shown everywhere status glyphs are shown.

### 4.8 How a breaking change surfaces

- **Before any input that can publish:** a missing or ambiguous locator, an unexpected
  signal, or a failed acting-as check stops the run as `failed` with
  `connector_contract_broken` (or the signal's code), the failing step, the locator,
  expected versus found and a screenshot ref, as runs already report.
- **At the preflight:** the composer does not hold exactly the approved text or media, so
  the run stops as `failed`, `preflight_mismatch`, nothing posted, and the draft is
  discarded.
- **At the commit gate (after AUD-03):** the publish request does not match the operation's
  `request` shape (the site changed its endpoint) or its parsed text and media differ from
  the approval. The gate aborts the request before it leaves: `failed`, `commit_mismatch`,
  nothing posted.
- **After the commit:** the readback decides (AUD-46). Live means verified; absent means
  `partial` and the owner is asked; a commit step is never retried without that readback.
- **Then, always:** the operation is marked `broken` for that site (or that surface when it
  looks surface-specific, such as a per-account A/B test), scheduled runs of it are held
  (they defer, they do not fail one by one), and the owner gets **one** notification per
  operation per breakage, not one per run. Other operations and other surfaces keep working.
  A repair report is written locally.
- **Never:** a model asked to find the button, a click on "the first match", a retry of the
  commit, or a success without a readback.

### 4.9 How agents see connectors

- **Tool names `<site>_<operation>`**: `instagram_post_photo`, `facebook_page_post`,
  `linkedin_company_post`, `reddit_post_text`, `reddit_list_comments`,
  `shopify_list_orders`. Underscores, because several MCP clients reject dots; the canonical
  dotted name (`instagram.post_photo`) appears in descriptions, docs and the panel. Flows
  keep `flow_<id>`. Site keys from `sites.py` are reserved prefixes, so a flow or static tool
  can never take a connector's name.
- **One tool per operation, not per surface.** `on` is required and its schema `enum` lists
  only the surfaces the caller's grant can see that are bound to this connector. With 29
  lanes that is about ten tools, not hundreds. The grant projects the list: a thread scoped
  to biz-a sees `on: ["biz-a/instagram"]`, never biz-b.
- **Descriptions are node-written** from the manifest: what it does, effect, the surfaces it
  can act as, health, and "Runs with no model; returns a verified result or an honest
  status". A `broken` operation stays listed with health in its description and returns
  `connector_broken` immediately, so an agent does not fall back to clicking.
- **`connectors_list`** (lazy, like `flows_list`) shows connectors, operations, bound
  surfaces, health and versions. `connector_status(name, on)` gives the last probe and the
  last real run.
- Results are the runner's run description plus `data` (section 4.10), so `run_status`,
  `run_cancel` and `idempotency_key` work unchanged.

### 4.10 Safe outputs

```json
{
  "status": "succeeded_verified",
  "data": {"items": [{"id": "t1_abc", "author": "someone", "created_at": "2026-11-04T08:12:00Z",
                      "permalink": "https://www.reddit.com/r/x/comments/...", "body": "..."}]},
  "untrusted_fields": ["items[].body", "items[].author"],
  "private_fields": [],
  "truncated": false,
  "source": {"connector": "reddit", "operation": "list_comments", "version": "1.0.2",
             "contract": "sha256:...", "surface": "biz-b/reddit", "read_at": "2026-11-04T09:00:00Z"}
}
```

- **Typed.** Every value is validated against the output schema. A value that fails its
  pattern or format is a contract violation (the site changed shape), not a value passed
  through: the run fails with `output_contract_violation` and the operation is `degraded` or
  `broken`.
- **Size-capped.** Per field `max_chars`, per list `max_items`, and a 64 KB cap on `data`;
  hitting a cap sets `truncated: true`. Values are clipped by the node, never by the model.
- **Structured fields cannot carry prose.** Ids, counts, times, money and enums are parsed
  from machine attributes and must match tight patterns. URLs must parse with the policy's
  single URL parser and sit inside the connector's registrable domain, or they are dropped.
- **Untrusted text is labelled.** Fields marked `untrusted` (comment bodies, author names,
  order notes) are listed in `untrusted_fields` and the run records `untrusted_content` in the
  grant family's taint ledger. Fields marked `private` (customer names, emails, order notes)
  record `private_data`. The lethal-trifecta rule then gates what the agent can send out next.
- **No raw page.** An operation never returns HTML, a snapshot or page text outside declared
  fields.

### 4.11 Official APIs as alternative backends

An operation's contract does not name a backend. The binding does:

| Backend | What runs | Auth | Readback |
|---|---|---|---|
| `browser` | the compiled flow on the lane | the lane's session | the live object in the lane, plus cookieless where declared |
| `api` | a declarative HTTP recipe per operation (method, URL template under the site's API host, body mapping from params, response mapping to outputs) | vault reference resolved by the node into the auth header only (No-Body-Injection) | a GET of the created object by id |
| `postiz` | the owner's self-hosted Postiz as a relay for posting | Postiz API key from the vault | Postiz's post status, then a browser or API readback of the live post |

- The API recipe is data like the steps: no code, host fixed by the connector, response
  mapping through the same output validation. Effects, approvals, pace and holds are
  identical, because they sit above the backend.
- Recommended defaults: Shopify `api` (a custom app with read scopes is easy for an owner);
  Facebook Pages and Instagram business accounts `api` once the owner has a Meta app, else
  `browser`; LinkedIn company pages `browser` (the Community Management API needs partner
  approval); Reddit `browser` for posting from the owner's account, `api` for reads if the
  owner registers a script app.
- A binding may name a `fallback`. A fallback never runs a commit that the primary already
  attempted: if the API post returned an error after sending, the readback decides first,
  exactly as for the browser.

### 4.12 Security

- **Effects.** Declared per operation, checked against the steps at compile time, enforced by
  the runner's effective effect at run time. A connector cannot be lower than its steps.
- **Approvals bound to content.** The approval content is `{operation, contract hash (or
  implementation hash for local overrides), surface, acts_as id, params}`, with shown params
  verbatim and files bound by sha256 of the bytes on the node. The phone card renders the
  post from those params (text, image, "as biz-a (Page)"); it never shows model prose.
  Standing approvals name `connector:<site>.<op>@<contract>` plus content digests (the week
  plan), as `flow:<id>@<version>` targets do today.
- **The request is checked, not only the click.** With AUD-03, the commit gate compares the
  real publish request with the approved content; a mismatch is aborted on the node.
- **No model in the loop for execution.** Runs are deterministic replay through the runner.
  A model is involved only out of band: an agent may propose a repair, which is inactive
  until the owner activates it, and which never applies to a running commit.
- **Acting-as identity from the binding.** The page or company id is a hidden binding param,
  so an injected instruction cannot point a post at another page; `assert_identity` checks it
  before the commit. A raw `browser_act` publish is checked by the commit gate instead (4.3: who
  the request acts as, resolved against the surfaces).
- **Supply chain.** Data-only bundles, signed, version-pinned, verified on install;
  downgrade refused; the catalog key is held offline by the maintainer, and a key rotation
  ships in an agentos release. An agent cannot write `connector.*` ids; local overrides need
  the owner (panel or CLI).
- **Pace and holds** are enforced by policy and the lane gate, below approvals: a hijacked
  agent with a standing approval still cannot post more often than the pace allows, or on a
  held lane.
- **Taint.** Outputs label the family (`untrusted_content`, `private_data`); connector runs
  are browser reads in the taint sense even when no browser tool was called.

### 4.13 Engagement operations and the verified mark

The engagement engine (`docs/design/engagement-engine.md`) acts only through connector
operations. These exist (connectors/reddit 0.3.0, connectors/linkedin 0.3.0):

| Operation | Effect | Inputs | Output | Egress rule | Verified |
|---|---|---|---|---|---|
| `reddit.post_text` | commit | subreddit, title, body | `post_url` | `reddit.post` (`CreatePost`) | no (stale: the 2026-11-04 canary ran 0.1.0) |
| `reddit.comment` | commit | subreddit, post_id, text | `comment_url` | `reddit.comment` (`/svc/shreddit/t3_<id>/create-comment`) | no |
| `reddit.join` | commit | subreddit | `subreddit_url` | `reddit.join` (`UpdateSubredditSubscriptions`, `subscribeState` SUBSCRIBED only) | no |
| `reddit.browse_subreddit` | read | subreddit | `posts[]`: url, id, author | none (a read) | no |
| `linkedin.post` | commit | text, image (optional) | `post_urn` | `linkedin.post` (`...sharing.CreatePost`) | no |
| `linkedin.comment` | commit | post_urn, text | `proof_url` | `linkedin.comment` (`...comments.createComment`) | no |
| `linkedin.react` | commit | post_urn | `proof_url` | `linkedin.like` (`...reactions.create` only) | no |
| `linkedin.follow` | commit | profile_kind (in or company), name | `proof_url` | `linkedin.follow` (`...mynetwork.addaUpdateFollowState`) | no |
| `linkedin.feed` | read | none | `posts[]`: urn, author_url | none (a read) | no |
| `linkedin.profile_name` | commit | first_name, last_name (1 to 50 each) | `proof_url` | `linkedin.profile.name` (to capture) | no |
| `linkedin.profile_headline` | commit | headline (up to 220) | `proof_url` | `linkedin.profile.headline` (to capture) | no |
| `linkedin.profile_about` | commit | about (up to 2600) | `proof_url` | `linkedin.profile.about` (to capture) | no |
| `linkedin.profile_photo` | commit | image (a file) | `proof_url` | `linkedin.profile.photo` (to capture), upload as `linkedin.draft.register` and `linkedin.draft.upload` | no |
| `linkedin.profile_cover` | commit | image (a file) | `proof_url` | `linkedin.profile.cover` (to capture), upload as for the photo | no |

Read operations return ids and links only, never page text. Every write operation refuses its
own undo in the preflight: `reddit.join` when the subreddit already shows Joined, `linkedin.react`
when the post already carries any reaction of the member's (Like, Celebrate, ...),
`linkedin.follow` when the top card shows Following (pressing again would leave, take the
reaction back or unfollow). The commit request block names the operation (`op`) where a site
sends every action to one URL, and it is narrower than its egress rule where the rule also covers
the undo (`reactions.create`, not `delete`; a join's `subscribeState` through `match`). While a
connector run holds the lane, the egress guard refuses any other commit or write of the run's
page (`FlowCommit`, docs/design/egress-guard.md 8.6), so the narrowing holds at runtime too.

What each one acts on and what proves it, fail closed:

- `linkedin.react`, `linkedin.comment`: every locator is inside the post's own container (the
  article whose `data-urn` is the post's urn), never a comment's Like or reply box. React proves
  the post's own toggle pressed after a reload, then the post on the reactions activity; comment
  proves that, in the newest entry for that post on the comments activity, the member's comment
  holds exactly the text.
- `linkedin.post`: notes the newest update on the activity page before the run; the preflight
  wants the editor to hold exactly the text and the audience to read Post to Anyone; proved when
  the newest update is not the one noted (`changed_from`, with `empty_before_ok`: an activity
  page with no update yet is a normal state) and its text is exactly the text.
- `linkedin.follow`: only the Follow of the page's own top card, never Connect or a suggestion's
  Follow; a top card with no Follow (only Connect) ends `preflight_mismatch`.
- `reddit.comment`: notes the account's newest comment on the post first; proved when the
  newest comment on that post is a new one (`empty_before_ok`: no comment there yet is normal)
  and its text is exactly the text.
- `changed_from` never passes on a missing baseline (the element saved before was not there,
  had no such attribute, or was not alone). `empty_before_ok` lets an empty list pass, and only
  when the saved read found no element at all; `identity` (a regex with one group) compares the
  part of both values that names the thing, and the new value must match it.
- `reddit.post_text`: the post page must name the account as its author (`author-id`).
- `linkedin.profile_*` (brief B2): each starts at `/in/me/` for the identity check, then loads
  `/in/<acts_as_id>/`, the page every read before the edit and the proof use, and opens its edit
  dialog there. No profile locator sets `nth`: each is scoped to the dialog or the top card so
  that exactly one element matches. The text edits (name, headline, About) fill the dialog's
  box, and the preflight wants the box to hold exactly the value and the value on the page behind
  the dialog to differ from it (`text_differs`, a preflight kind added for this: exactly one
  element, even when a locator sets `nth`, whose own text, whitespace normalized, is not the
  template; with `lines`, as for the About, line breaks are kept). When that is the only failure
  and the one element shows exactly the value, the run ends `already_current` with nothing saved;
  a missing or doubled element is `preflight_mismatch`. The photo and cover first wait for the
  one image there with a non-empty `src` and note it (none: the run stops before any upload),
  upload the file through the dialog (the same registration and `/dms-uploads/` PUT as a post's
  photo, so the commit gate binds the edit to the file the node saw; 8.2), and the preflight
  wants exactly one image in the dialog. Every one is proved on `/in/<acts_as_id>/` reloaded as
  the account (a signed-in view; the canary checks a signed-out view once): the exact text for
  the name heading and the headline, the About text with its line breaks; for an image, a `src`
  on `media.licdn.com` whose asset id (`identity`) is not the one noted before the run, so the
  same image at another size or signature does not count. Their inputs are `readOnly`: the
  approval card shows the value and the owner approves or denies it, never edits it, because the
  engagement program keeps the value it asked for as the account's state. Nobody has captured these
  pages or requests: the locators, the fixtures (SYNTHESIZED, NOT CAPTURED) and the commit
  request names are guesses, and the egress rules `linkedin.profile.*` ship with an empty
  `verified`, so the engine holds them as `rule_unverified` before it even looks for a mark.

LinkedIn sets no cookie that names the member, so its operations read who they act as from the
URL: each starts at `/in/me/`, which LinkedIn sends to the member's own profile, and the profile
name in that URL must equal the surface's id. The runner waits for that redirect: `me` is never
read as an id (it is unknown until it resolves, never a wrong account). Bind a LinkedIn surface
with the public profile name: `agentos connector bind test/linkedin --site linkedin --lane
test/linkedin --acts-as account --id arlo-fenwick-demo`.

**The verified mark.** A canary on a throwaway account that ran an operation end to end and
read the effect back is recorded as a mark in the node's own ledger
(`$AGENTOS_HOME/connector-marks.json`, agentos.runner.connectors.marks), keyed by `site.op` and
the operation's **proof hash**: the compiled operation (steps, locators, readbacks, preflights,
commit request, pace, outputs) without its title, description, version and mark. A mark is
honoured only while the installed operation has that proof hash: any later edit makes it stale,
and the engine holds the operation again (`op_unverified`, reason `verified_stale`). With no
mark the engine holds too. Everything here was written from the 2026-11-04 notes without loading
the pages, and its fixtures say so in their first line: the locators are a hypothesis until a
canary proves them. `reddit.post_text`'s 2026-11-04 canary ran 0.1.0; its public check and
author check came later, so its old mark is stale and it needs a canary again.

Provenance: only the ledger counts. A connector file or a signed bundle may carry `verified` and
`verified_hash` (`2026-11-04 reddit.com canary: ...`), as information for a reader; a mark that
arrives in a manifest never makes an operation run, and one whose hash differs compiles as stale
(`agentos connector compile` says so). Only the owner writes the ledger, from a shell on the node:
no MCP tool, API route or agent can.

```
agentos connector verify reddit.comment --note "test/reddit: comment 19s4... on 1demo02, read back on its permalink"
agentos connector verify reddit.comment --revoke      # a later canary failed
agentos connector verify reddit.comment --dir connectors/reddit --note "..."   # also record it in the repo
```

Without `--dir` it marks the copy installed on this node (it must be installed); with `--dir`
it also writes the canary's words and the proof hash into that directory's connector.json with a
new patch version and installs it (`--no-install` to only write it). It never picks up a
`connectors/` directory from the shell's working directory. The contract hash does not change.
`agentos connector list` shows each installed operation as verified, verified_stale or not verified.

**Canary checklist** (on a throwaway account, through the operation's own MCP tool or `agentos
connector run`, with observe mode on for the lane so the request shape lands in the trace). An
operation is marked verified only when every point holds; when a locator misses, fix the
connector (and its fixture, from the captured page with the text replaced), then run it again.
Every run also checks that the trace has no `flow_commit` block (the run sent only its own
request).

| Operation | Do | Proves it |
|---|---|---|
| `reddit.post_text` | post a text post to r/test (its 2026-11-04 mark is stale) | verified with a `post_url`; the permalink shows the title as the account's (`author-id` is the account's `t2_` id); the signed-out check passes; `CreatePost` under `reddit.post`; no leave-page dialog |
| `reddit.comment` | comment on a post in r/test made by another throwaway account, then again on a post the account already commented on | each run `succeeded_verified` with a `comment_url` of the new comment, not the older one; `/user/me/comments/` lists the newest comment first and its link's text is the comment's text; `create-comment` under `reddit.comment`; the run did not leave a leave-page dialog |
| `reddit.join` | join a small subreddit the account is not in, then run it again | first run verified and r/<name> shows Joined after a reload; `UpdateSubredditSubscriptions` under `reddit.join` with `subscribeState` SUBSCRIBED; the second run ends `preflight_mismatch` and the account is still joined |
| `reddit.browse_subreddit` | read r/test | `posts` lists the newest posts with permalinks, `t3_` ids and authors, in page order |
| `linkedin.post` | post a short text, then a text with a photo; then try once with the audience set to Connections only | each run verified with a `post_urn` that is new on the activity page, whose `.update-components-text` is exactly the text; the post shows to Anyone; the trace has `CreatePost` (and `registerMediaUpload`, a `/dms-uploads/` PUT for the photo); the Connections only run ends `preflight_mismatch` with nothing posted |
| `linkedin.comment` | comment on a post by a creator the account follows | verified with a `proof_url`; the comments activity's newest entry for that post (`data-urn` the post's urn) holds the member's comment with exactly the text in `.comments-comment-item__main-content`; `createComment` under `linkedin.comment` |
| `linkedin.react` | like a post that has comments, then run it again on the same post; then on a post the account reacted to with Celebrate | first run verified: the post's own container (role article, `data-urn` the post's urn) has its Like pressed after a reload and the post is on the reactions activity; no comment was liked; `reactions.create` under `linkedin.like`; the second and third runs end `preflight_mismatch` and the reactions stay |
| `linkedin.follow` | follow a company page, then a creator's profile that offers Follow, then a profile that offers only Connect | the first two verified, the top card (`.org-top-card`, `.pv-top-card`) shows Following after a reload (if it shows the button pressed instead, change the readback); `addaUpdateFollowState` under `linkedin.follow`; the Connect-only profile ends `preflight_mismatch`; no invitation was sent and no suggested person was followed |
| `linkedin.feed` | read the home feed | `posts` lists activity urns and author links; none is a promoted item without an author |
| `linkedin.profile_name` | observe mode on; change the first and last name once, then run it again with the same name | the trace's request name goes into the rule `linkedin.profile.name` (`op`, `path`, `verified`) and the operation's commit request; verified with the name heading exactly the new name on a fresh load; the second run ends `already_current` and saves nothing; no `flow_commit` block; once, open the profile signed out (a private window) and see the new name there too |
| `linkedin.profile_headline` | the same with the headline | rule `linkedin.profile.headline` from the trace; the headline under the name is exactly the text on a fresh load; the repeat ends `already_current` (if the intro dialog saves name and headline in one request, merge the two rules) |
| `linkedin.profile_about` | the same with the About text, on a profile that has an About section | rule `linkedin.profile.about` from the trace; the About text is exactly the text (spaces normalized, line breaks kept) on a fresh load; the repeat ends `already_current` |
| `linkedin.profile_photo` | upload a new photo (`files_put` first) | rule `linkedin.profile.photo` from the trace, after the registration (`linkedin.draft.register`) and the `/dms-uploads/` PUT; the photo's `src` is new and on `media.licdn.com` on a fresh load; the commit gate's card shows the file's hash |
| `linkedin.profile_cover` | upload a new cover image | rule `linkedin.profile.cover` from the trace; the cover's `src` is new and on `media.licdn.com` on a fresh load |

## 5. Fit: what changes where

| Package | Changes |
|---|---|
| `runner` | `connectors/` subpackage: connector and operation models, compiler, strict lint, contract and implementation hashes, install and override store. Executor: `assert_identity`, commit marker with preflight, `extract`, bounded read-only `for_each`, rehearsal mode with `discard`, outputs mapped from `save_as` into `Run.output` (new field) validated against the output schema. `saved_by` kind `catalog` counted as owner-vouched when installed by the owner. `flow_save` refuses `connector.*` ids except as proposed overrides. New error codes: `connector_contract_broken`, `preflight_mismatch`, `commit_mismatch`, `output_contract_violation`, `connector_broken`, `wrong_surface`. Health records per operation and surface. |
| `node/flow_driver.py` | `attr` matcher in `find` and `read_state`; `extract_items(locator, fields, max_items)` in the `FlowDriver` protocol; a text-free structural skeleton reader for repair reports. Fake driver parity for each, with new parity cases (attributes, custom elements, shadow roots, items under anchors, own-text extraction). |
| `node/sites.py` | URL rules and benign interstitial markers that connectors need (reviewed with the connector); acting-as probes that read an asset id from URL or `href`; site keys exported as reserved tool prefixes. Still the single signal table. |
| `node` (new `commitgate.py`, AUD-03) | consumes each operation's `request` block as its per-site table; during a connector run it matches the publish request to the run's approval. |
| `policy` | targets `connector:<site>.<op>@<contract>`; content digests with file sha256; pace limits (AUD-13) per surface and operation, enforced before routing; outputs feeding the taint ledger; standing approvals bound to contract hashes for signed catalog versions and to implementation hashes for overrides. |
| `mcp` | registers `<site>_<op>` tools with the grant-projected `on` enum; `connectors_list`, `connector_status`; refreshes the tool list when bindings, health or installs change. |
| `api` | `GET /api/connectors`, `GET/PUT /api/connectors/bindings`, `POST /api/connectors/{op}/probe`, `POST /api/connectors/install` (bundle upload), `POST /api/connectors/overrides/{id}/activate`, `GET /api/connectors/reports/{id}` (repair report export); SSE `connector_health` events. |
| `panel` | a Connectors view under Flows (the rail stays six): per workspace, each surface with its connectors, health glyph, last probe, last run, version, and override diff; Needs you gets "operation broken" and "proposed repair" items; approval cards render the post per surface. |
| `workspaces` | templates create bindings with lanes (`social-6`: which surfaces are logins and which act through another lane). Note the name clash: `workspaces.models.Surface` is a template lane today; AUD-01's surface is the thing posts target (open question 9). |
| `controller` | merges connector tools across nodes (one `instagram_post_photo` whose `on` lists surfaces on every node, routed to the node holding the lane); mirrors health; flags version skew between nodes. It never holds bundles' secrets because there are none, and never holds API tokens. |
| `cli` | `agentos connector` with `compile`, `install`, `list`, `probe`, `override`, `adopt` and `report`. |
| docs | `docs/CONNECTORS.md` generated from the manifests (operations, schemas, effects, health meaning), like `docs/TOOLS.md`. |

## 6. Phased plan

Sizes: **S** is one to two builder days, **M** three to five, **L** one to two weeks.

Dependencies outside this bet: AUD-01 surfaces (bindings are surfaces), AUD-12 identity
(built in sites.py), AUD-13 pace, AUD-03 commit gate. Phase 1 can start before AUD-03; commit
connectors may run on real accounts only under exact-content approvals until the gate lands,
and the strict lint turns the missing `request` block into an error once it does.

| Phase | Scope | Size |
|---|---|---|
| 1. Posting connectors | connector format, compiler and strict lint; runner `assert_identity`, commit marker and preflight, outputs; `attr` matcher with parity; bindings on AUD-01 surfaces; `<site>_<op>` tools; the four posting connectors verified with the owner on throwaway accounts | L |
| 2. Health | rehearsal mode and `discard`; scheduled probes and their triggers; health states and glyphs; one notification per breakage; held schedules; fixture tests in CI on fake and Camoufox; `post_bundle` (one approval, several surfaces) and `at` on commit tools | M |
| 3. Reads | `extract` and read-only `for_each`; output validation, caps, untrusted and private labels into taint; `reddit.list_comments`, `instagram.list_comments`, `facebook.page_comments`, `shopify.list_orders` (browser); reply operations as commit | M |
| 4. Catalog channel | signed bundles, install and verify, downgrade refusal, update checks (off by default), local overrides with diffs, agent-proposed repairs, repair reports (export only), `docs/CONNECTORS.md` generation | M |
| 5. API backends | declarative API recipes, vault auth header, readback by GET; Shopify Admin API, Graph API for Pages and Instagram business, Reddit script app reads, Postiz relay; fallback rules | L |
| 6. More sites | Medium, WordPress (own blog), X, Google Business Profile, each only after the owner asks for it and an API path has been ruled out | S each |

### Phase 1 builder briefs

Each brief is self-contained. Builders read this document and `src/agentos/runner/README.md`
first. Nothing in phase 1 touches a real account except brief D, and brief D only
with the owner present.

**Brief A: connector format and compiler (M).**
- Goal: `connectors/<site>/connector.json` validates, lints strictly and compiles into
  `FlowDefinition`s that the existing runner accepts.
- Build: `src/agentos/runner/connectors/models.py` (Connector, Operation, Commit, Readback
  contract, Pace, Signals, Binding schema), `compile.py` (operation to FlowDefinition with
  id `connector.<site>.<op>`, lane `*/*`, hidden binding params), `lint.py` (the strict
  rules in section 4.4), contract and implementation hashes, `agentos connector compile`.
  Install from the repo directory (no signing yet) with `saved_by = {kind: "catalog", ...}`
  and make `is_owner` count it when installed by the owner. Reserve `connector.*` ids in
  `flow_save` (MCP saves are refused with a clear message in phase 1; proposed overrides come
  in phase 4).
- Tests: model validation table; compile of a sample connector against the fake driver;
  effect mismatch is a compile error; contract hash unchanged by a locator-only edit and
  changed by a schema edit; `flow_save` cannot write a connector id.
- Done: `make check` green; a sample read connector runs end to end on the fake.

**Brief B: runner and driver capabilities (M).**
- Goal: the four capabilities connectors need on the commit path.
- Build: `attr` matcher on `Locator` (equals, prefix, regex on a named attribute) in the
  fake (`runner/fakes.py`) and `PlaywrightFlowDriver`, with new parity cases (at least 8,
  including shadow roots and custom elements); per-language `names` resolved from a hidden
  `account_language` param; `assert_identity` step (identity cookie through `sites.py`, or
  an asset id read from the URL or an `href`) failing as `wrong_account` or `wrong_surface`
  and holding the lane; the commit marker with preflight checks (value equals a shown param,
  count, acting as); `Run.output` filled from readback `save_as` mappings and validated
  against the output schema.
- Rules: a failed preflight is `failed` and nothing after it runs; after the commit step only
  `partial` or success (keep AUD-46's readback-first resolution); no page marking.
- Tests: parity cases on fake and Camoufox; executor tests for preflight pass, fail and
  partial; identity mismatch holds the lane; output schema violation fails the run.

**Brief C: surfaces, tools and approvals (M).**
- Goal: an agent calls `instagram_post_photo(on="biz-a/instagram", ...)` and the right lane,
  identity, approval and pace apply.
- Build: bindings on the AUD-01 Surface model (if AUD-01 has not landed, build `Surface`
  per its proposal in `core/models` and coordinate with the workspaces owner on the name
  clash); binding store and `agentos connector bind`; MCP registration of `<site>_<op>` with
  the grant-projected `on` enum; `connectors_list`; approval content `{operation, contract,
  surface, acts_as, params}` with file sha256; node-written summary ("Post to Facebook as
  biz-a (Page): caption '...', 1 image"); policy target `connector:<site>.<op>@<contract>`;
  pace enforcement through AUD-13 limits when present, else the connector's defaults.
- Tests: a grant scoped to biz-a sees only biz-a surfaces in `on`; a Page surface whose
  lane is `personal/facebook` is reachable from a biz-a grant and not from biz-b; approval
  digest changes with one byte of the image; pace refuses the second post inside the gap
  even under a standing approval.

**Brief D: the four posting connectors (L, with the owner).**
- Goal: AUD-04's v1 set as catalog connectors: `instagram.post_photo` (photo plus caption),
  `facebook.page_post` (photo plus text, as a Page through the personal login),
  `linkedin.company_post` (photo plus text, as a company page), `reddit.post_text`
  (subreddit, title, body).
- Precondition: the owner provides one throwaway account per platform (a throwaway Facebook
  profile with a test Page, a throwaway LinkedIn profile administering a test company page),
  on a separate `canary` workspace with its own device and proxy, never linked to real
  accounts. The owner is present for every live session; the builder never logs in alone and
  never touches a real account.
- Build per connector: record on the throwaway account, convert to structural locators
  (`attr` on `href`, `type=file`, machine attributes; names only as per-language maps for
  English and Lithuanian), acting-as check (Instagram and Reddit by identity cookie; Facebook
  Page by asset id in the URL or the composer's actor link; LinkedIn by `/company/<id>/`),
  preflight, commit step, readback of the live permalink with `post_url` and `post_id`
  outputs, `public: cookieless` for Instagram and Reddit (AUD-22: `visible_only_to_author`
  is `succeeded_unverified`), benign interstitials with structural dismiss recipes,
  conservative pace defaults, the commit `request` block captured from the real publish
  request (kept as a warning-level field until AUD-03 lands), fixtures in English and
  Lithuanian.
- Verification: three successful live posts per connector on the throwaway accounts, each
  `succeeded_verified`, plus one deliberate break per connector (a fixture with the commit
  button removed) showing `connector_contract_broken` with nothing posted. Then freeze the
  version and record the contract and implementation hashes in the PR.
- Never: run on the owner's real accounts in this phase, retry a commit, or post more than the
  pace defaults allow, even on throwaway accounts.

## 7. Risks

- **Site terms.** Most platforms' terms restrict automated access, even to your own account.
  Mitigations: own accounts only, human pace and active hours, low volume, no scraping of
  others, prefer the official API wherever the owner can use it, label actions as automated
  where a site or law asks (principle 7), and say plainly in the docs that the owner accepts
  the platform risk for browser backends. The catalog never ships an operation whose purpose
  is evading a platform's limits.
- **Account safety.** A broken connector that retries, a canary that adds odd activity, or a
  device change can trigger checkpoints. Mitigations: holds on any bad signal (AUD-07), one
  probe per surface per day at most and counted in pace, rehearsals that discard instead of
  saving drafts, the AUD-02 device plan, and throwaway canaries on their own device.
- **Maintenance load.** Social sites change often and A/B test per account; Meta especially.
  Each connector is a standing cost. Mitigations: a small catalog (four, then a handful),
  structural locators over names, health that tells us which operation broke and where,
  repair reports that are actionable without the owner's data, API backends that retire the
  browser path where possible. Track repairs per connector per month; a connector needing
  more than two repairs a month gets an API-first review or is dropped.
- **Per-account divergence.** One account sees a new composer while another sees the old.
  Mitigations: operations may carry structural branches (`branch` on a structural check, not
  text), health per surface, and fixtures for both variants while a test runs.
- **False health.** A rehearsal can pass while the publish request changed. Mitigations: the
  commit gate's request check, readback as the only proof, and throwaway full-post canaries
  weekly.
- **Supply chain.** A stolen catalog key could ship a malicious step list. Mitigations:
  data-only connectors that cannot exceed what a flow can do, content-bound approvals and the
  commit gate (a malicious step cannot change what the owner approved without the gate
  noticing), two-reviewer rule for commit operations, offline key, rotation path.
- **Output injection.** Comment text is attacker-controlled. Mitigations: section 4.10 typing,
  caps, labels, and the trifecta rule.
- **Scope creep toward a Zapier clone.** Many sites, many operations, an ecosystem ask.
  Mitigation: the non-goals and principle 8; new sites only when the owner needs them.

## 8. Open questions with recommended defaults

1. **Tool naming: `instagram_post_photo`, `connector_instagram_post_photo` or
   `instagram.post_photo`?** Default: `<site>_<op>` with reserved site prefixes; dotted names
   in descriptions and docs.
2. **One tool per operation with `on`, or one per surface?** Default: per operation, with a
   grant-projected `on` enum.
3. **Do catalog updates install by themselves?** Default: update checks off. When on, an
   update that keeps the contract hash installs automatically (it is a repair) and an update
   that changes it waits for the owner. Local overrides are never replaced.
4. **What do approvals and schedules bind to?** Default: the contract hash for signed catalog
   versions, the implementation hash for local overrides, so repairs do not invalidate a
   week's approved plan but a local edit does.
5. **Probe before every scheduled commit?** Default: no; the preflight inside the run covers
   most breaks. Probe only when a new version is installed or the operation has not
   succeeded on that surface in seven days, 20 to 40 minutes before the slot.
6. **Who owns the throwaway accounts, and where do they live?** Default: the owner, one per
   platform, in a `canary` workspace with its own device and proxy and no shared phone or
   recovery email with real accounts. The project keeps its own set for release canaries.
7. **Can an agent-proposed repair ever go live without the owner?** Default: never. It is
   listed under Needs you with the diff; commit operations also need a passing rehearsal.
8. **Repair sharing.** Default: off. Phase 4 adds export of a text-free report as a file; an
   upload channel only if owners ask, opt-in per report.
9. **The name "surface".** `workspaces.models.Surface` means "a lane a template creates";
   AUD-01's surface means "who you post as". Default: keep "surface" for AUD-01 (the
   owner-facing word, used by grants, approvals and connectors) and rename the template class
   to `TemplateLane` when AUD-01 lands.
10. **Account language.** Default: structural locators first, per-language names for English
    and Lithuanian as a last resort, the account language recorded on the binding at
    onboarding (AUD-39, AUD-45). An account in another language runs only operations whose
    fixtures cover it; others show `degraded` with the reason.
11. **Messaging and DMs.** Default: out of the catalog until the trifecta rules and the
    commit gate have run in production for a month.
12. **Where the catalog lives.** Default: in this repo under `connectors/` now; a separate
    signed bundle channel in phase 4 so a repair can ship faster than an agentos release.
13. **Instagram business accounts posted through Meta Business Suite.** Default: phase 1 uses
    the Instagram web composer on the account's own login; a Business Suite binding (acting
    as an `ig_business` asset through the Facebook login) comes with the Graph API backend in
    phase 5, which is the better path for those accounts anyway.
