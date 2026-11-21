# Site APIs

The contract for turning a site you can use in a browser into a typed HTTP API. It is additive under
`docs/PROTOCOL.md` v1 and builds on `docs/design/connectors.md`. The core wins, and
The hosted edition's contract mirrors this file.

A **site API** is a connector the owner made for their own use. An **endpoint** is one of its
operations. It is built on what exists: an endpoint compiles to an ordinary flow with the id
`connector.<api>.<endpoint>`, so runs, approvals, Ask me first, holds, health, pace, schedules and
workflow steps work unchanged. There is no second engine. What this adds is a way to make one for any
site, a stable HTTP surface with keys, and an OpenAPI document.

## The lines it stays inside

"Any site" means any site the owner can already use, from their own lane. These hold for every part
of this file and each has a test:

- **One lane is one identity.** An endpoint runs on the surface it is bound to. An API never spreads
  calls over several lanes, never rotates lanes or proxies, and never adds a lane to get past a site's
  limit. A second surface is a second person's account the owner bound by hand.
- **A challenge stops the work.** `captcha`, `checkpoint`, `logged_out`, `blocked` and `rate_limited`
  hold the lane exactly as they do for a flow. The caller gets `needs_human` or `rate_limited`. Nothing
  retries, solves or logs in.
- **No secret leaves.** A site's cookies, session and tokens are never in an output, a cache, a usage
  row, a draft or a log. Vault fields stay on the node. A step that types into a sensitive field is
  dropped from a draft, as the recorder does today.
- **No raw page.** An endpoint returns declared fields only, never HTML, a snapshot or page text.
- **Page text is data.** Text read from a page is untrusted by default, and a run labels the caller's
  grant family. A page can never change what a draft does: discovery reads structure and proposes, and
  a person activates.
- **Only a person activates.** An agent can discover, try and propose. An agent cannot activate, widen
  or re-point an endpoint (`connector.*` stays closed to agents).
- **`pay` and `irreversible` are not endpoints.** They are refused at save.

## Definition

What a person, an agent or a developer's file holds (JSON or YAML):

```json
{
  "api": "harbor-supplier",
  "title": "Harbor supplier portal",
  "domain": "portal.northsupply.example",
  "version": "1.0.0",
  "surfaces": ["harbor/supplier"],
  "endpoints": {
    "open_orders": {
      "title": "Open orders",
      "description": "Orders that have not shipped yet.",
      "effect": "read",
      "input": { "type": "object", "properties": {
        "status": { "type": "string", "enum": ["open", "late"], "default": "open" } } },
      "output": { "type": "object", "required": ["orders"], "properties": {
        "orders": { "type": "array", "items": { "type": "object", "properties": {
          "number": { "type": "string" }, "total": { "type": "string" },
          "url": { "type": "string", "format": "uri", "x-agentos-host": "portal.northsupply.example" } } } } } },
      "steps": [
        { "type": "navigate", "url": "https://portal.northsupply.example/orders?status=${status}" },
        { "type": "extract", "save_as": "orders", "max_items": 100,
          "items": { "role": "row", "within": { "role": "table", "name": "Orders" } },
          "fields": { "number": { "locator": { "role": "cell", "nth": 0 }, "read": "text", "max_chars": 40 },
                      "total": { "locator": { "role": "cell", "nth": 3 }, "read": "text", "max_chars": 40 },
                      "url": { "locator": { "role": "link" }, "attr": "href", "format": "uri",
                               "host": "portal.northsupply.example" } } }
      ],
      "outputs": { "orders": "orders" },
      "cache_s": 300,
      "expect": { "min_items": { "orders": 1 } },
      "pace": { "per_hour": 60 }
    }
  }
}
```

- `api` is a connector key (`CONNECTOR_KEY_RE`, which allows inner hyphens; the tool name turns them
  into underscores, `harbor_supplier_open_orders`), not a reserved one, and not a key the catalog
  installed. `version` is semver; each save of changed content needs a higher one, and the CLI and the
  panel bump the patch for you.
- `outputs` name what an `extract` or `read_json` step saved (`orders`, for its list) or that value's
  `count`, `total` or `truncated`: nothing else, so the labels cover everything an endpoint returns. A
  field reads at most 2000 characters. `output` may be left out; the step's fields are its schema, and a
  field in `output` keeps what it says.
- An `extract` step may set `pick` (`outermost` drops a match inside another, such as a reply inside a
  post; `card` makes each match stand for the widest element around it holding no other match, for cards
  of plain boxes no role can name) and `drop_empty` (a table's header row reads as no values at all).
  An extract looks at the first 5000 matches at most, so a page cannot make a read expensive for the node;
  past that `total` stops counting.
  A locator may set `level` (a heading's level). A field may set `capture`, a regex with one group,
  for a value inside loose text (a price in a list item). No match, or a group that matched nothing, is
  null, and the output schema decides whether that fits.
- Compilation goes through the same `compile_connector`, the same lint and the same hashes.
  `SiteApiService` turns the definition into a `Connector` document, so a site API cannot do anything a
  connector cannot.
- **Any domain.** `site` is `"web"`, a generic key whose hosts are `domain` and its subdomains: every
  `navigate` must sit under `domain`, as lint checks today. If `domain` belongs to a site in
  `openajar.node.sites.SITES`, the compiler uses that site key and `acting_as: account`, so the wrong
  account check still applies. A site with no table row uses the new `acting_as: {"kind": "lane"}`: the
  lane's own session, no identity cookie to check. `kind: lane` is only legal on `saved_by.kind: local`.
  On a site with a row, the compiler adds an `assert_identity` step after the first `navigate`; a lane
  API has no identity to check and may not have one.
- **URLs.** A host is read as a browser reads it (no user part, no backslash). `domain` is not a private
  name or a shared suffix. A URL takes the caller's params only, never `${vars...}`, so what a page said
  cannot steer the lane. A param value is percent-encoded, and one that would make a path segment `.` or
  `..` is refused when the run fills it in. Whether a host resolves to a private address is the lane's
  guard's to refuse.
- **Params.** A param reaches a page only through a URL, a `fill` or `type` value, or a `select` whose
  param has an `enum` in `input`. No `${...}` appears anywhere in a locator (`css`, `name`, `names`,
  `attr`, at any depth of `within`), in an extract's `items` or fields, in a `read_json`'s `url`, `items`
  or paths, or in a condition's `matches`, and a site API has no `upload` step, since a param would pick the file. A value in a selector would
  be structure, so a caller could point a click at another element. These are refused at save and in
  lint. A param may still be compared with what a page says (a condition's `equals`, `value_equals`) and
  pick a branch the definition wrote out. The catalog is reviewed and is not held to this.
- **Provenance.** The compiled flow is saved with `saved_by = {"kind": "local", "id", "installed_by",
  "owner"}`. `is_owner` counts it only when a person saved it, `FlowService.save` lets `connector.*`
  through for `catalog` and for `local` from `SiteApiService`, and `binds_to` already uses the
  implementation hash for anything that is not catalog, so an edited endpoint loses its standing
  approvals.
- **Effects.** `read`: no input steps. `draft`: input steps every one of which is `publishes: false`,
  like a search box or a date filter; that vouch only counts when a person saved it. `commit`: must meet
  the connector commit rules in full (one labelled commit step, preflight, a readback). Effect is
  checked against the steps, never trusted. An agent's proposal that has an input step shows as
  `commit` until a person vouches.
  A person vouches by saving that content themselves, or with `vouch` at activation.
- **Labels.** In what an agent wrote, every field is untrusted unless its `format` is `date-time`,
  `integer` or `number`, or `uri` with a `host`, or its `type` is `number`, `integer` or `boolean`,
  because only those hold a value to a shape; a `pattern` can be `.*`, and `untrusted: false` is ignored.
  A `vouch` keeps these labels. A person who saves the definition themselves answers for it: a text
  read, a JSON string or a free attribute is untrusted unless they say otherwise, and a `pattern` or a
  `format` holds an attribute.
- `cache_s` (0 to 86400, default 300 for `read`, always 0 otherwise) and `expect` are read by the HTTP
  layer and health. They are part of the implementation hash, not the contract hash.
- `expect.min_items`: fewer rows than this means the site probably changed. The call answers
  `empty_result` and the endpoint goes `degraded`, instead of returning an empty list that looks true.
- A call can land on a page the contract never meant: an id that does not exist has no anchor to read,
  and a value can fail a field's pattern. So with params, a contract failure (`connector_contract_broken`,
  `output_contract_violation`, a preflight or commit mismatch) marks the endpoint `broken` only once a
  second param set has failed the same way on that version. Until then it is `degraded`: that call fails
  on its own and keys, schedules and workflows carry on. With no params it is `broken` at once. Health
  keeps a keyed digest of the params, never their values, and a success forgets it.
- `pace` defaults to `{ "per_hour": 60 }` per endpoint and surface. It protects the owner's account from
  their own code; it is the owner's to change. Every run that reached the lane counts, whatever came of
  it, because a failed run loaded the site too; a run cancelled in the queue or refused at its gate does
  not. It is separate from the key's rate limit, and it is the only limit on how often an endpoint runs:
  the lane's `LaneSpec.limits` are not read yet, for a site API or any connector. A commit endpoint is
  also counted by the policy's meters when it is authorized, as any commit is.

## Making one

States: `draft` -> `proposed` (an agent asks) -> `active` -> `paused`. An `active` API that a person
pauses answers `paused`. Drafts live in the runner store (`site_api_drafts`); only an activated API has
flows.

1. **Discover.** `discover(on, url?)` looks at the page in the lane and returns candidates. It works
   from the same accessibility outline as `browser_snapshot`, in pure Python
   (`openajar.runner.siteapis.discover`), so it adds no JavaScript of its own to the page (the outline is
   taken as `browser_snapshot` takes it) and can be tested from stored outlines. It finds repeated shapes (table rows, list items, repeated articles, cards of
   siblings with the same role shape), and proposes `{name, items, fields, sample, count, confidence}`
   with field names from table headers or accessible names. Sample values are clipped and labelled
   untrusted. When it opens `url` itself it also lists, under `json`, what the page's own code fetched
   as JSON from its own site, to read with a `read_json` step (below).
2. **Record**, for anything that needs input first. `flow_record_start` and `flow_record_stop` make the
   steps and params; `from_recording` turns that draft into an endpoint.
3. **Try.** `try(draft, endpoint, params)` runs it once on the lane and returns the typed result with
   `problems`. A try is allowed only for `read`, or `draft` when a person calls it; it never runs a
   commit step. It counts against pace. A paused API answers an agent's try with `paused`; a person may
   try it, because a person fixing a paused API must be able to test the fix.
4. **Activate.** The owner only: compiles, installs, binds to `surfaces`. It is refused when a tool name
   would match another connector's, because the hyphens in a key read as underscores (`instagram-post` with
   `photo` is `instagram` with `post_photo`) and one of the two tools would be dropped. A `web` surface that does not
   exist yet is made on the lane of the same name; a site with a row is bound by hand, for its account.
   An edit changes the draft, and what is live moves only here. An endpoint dropped by an edit, like a
   removed API, is refused while a schedule or workflow uses it, and a removed API's runs stay in history.

A try has no route of its own: it stages the compiled version without a head (`FlowService.stage`), so
the run is an ordinary connector run with a pinned version, and no tool, schedule or list can reach it.
Naming a version does not reach one either: a site API runs only its active version, so `POST
/api/connectors/{op}/run`, `flow_run`, a schedule and an event route answer 403 to any other, an older
active one too, and a schedule pinned to the version that was active stops firing at the next activation.
What a try finds says nothing of the health of what is live. A web API can be tried on a lane no surface
names yet; a site with a row needs its surface, because that pins the account.

## HTTP

Served by the node beside the panel API, under `/v1/sites`. This namespace is the stable developer
surface: additive changes only. The node's host check still applies (421), so a self-hosted node
reaches callers over the mesh or `extra_allowed_hosts`. A hosted node is reached through the cloud
relay (below). No public port is opened on a node.

| Route | What |
|---|---|
| `GET /v1/sites` | the site APIs this key can see |
| `GET /v1/sites/{api}/openapi.json` | OpenAPI 3.1, for this key's endpoints and surfaces |
| `GET /v1/sites/{api}/{endpoint}` | call a `read` endpoint, params in the query |
| `POST /v1/sites/{api}/{endpoint}` | call any endpoint, params as a JSON object |
| `GET /v1/sites/{api}/runs/{run}` | a call that answered 202 |

Query or header controls, none of which are params: `on` (the surface; optional when the key sees
one), `wait` (seconds, 0 to 60, default 25), `max_age` (seconds; lowers `cache_s`, 0 forces a fresh
read), `stale_ok` (`1`: on `needs_human`, `pace_limited` or `rate_limited`, return the last good result
with `stale: true`, when it is at most 7 days old). As headers they are `Agentos-On`, `Agentos-Wait`, `Agentos-Max-Age` and
`Agentos-Stale-Ok`. `Idempotency-Key` on POST, required for `commit`, and ignored on a `read`.

### Keys

`Authorization: Bearer aos_<43 base64url chars>`, 256 random bits. Shown once; only the SHA-256 is
stored (`site_api_keys`, with a short `aos_ab12` prefix for showing). A key has a `label`, `apis`,
optional `endpoints` and `surfaces`, `ceiling` (`read` by default; `draft`, `commit` by a person's
choice; never above), `rate_per_min` (default 60), `expires_at` (optional), `last_used_at`.

- A key is backed by a grant from `policy.issue` (`subject` kind `agent`, id `site-key:<id>`, lanes,
  domains and ceiling narrowed to the key; its lanes are the key's surfaces and the lanes behind them,
  so a freeze of either reaches it), so the policy, Ask me first, meters, freeze, taint and
  Activity treat a call like any agent's call, with the key's label as who did it. The grant's token is
  dropped at creation. Revoking the key revokes every grant of it in the same step.
- A key with an `expires_at` gets a grant that outlives it, so that grant is never replaced. A key with
  no end, or one past the longest grant the node issues, gets a new grant on a call within 30 days of
  the old one's end (half way through it, on a node whose grants are short). The old grant stays live
  until its own end, so a run queued or waiting for a person under it finishes, and the key remembers
  it, so revoking or editing the key ends them all. What the key read goes to the new grant, so a
  renewal never clears the labels. A key unused past its grant's end gets a new grant on its next call.
  A grant a person revoked or a freeze swept is never replaced, and none is issued during a freeze.
- A key opens `/v1/sites` and nothing else: `/mcp`, `/api` and the panel answer 401, and a grant or
  node token is not accepted on `/v1/sites`. An owner session may call `/v1/sites` from the panel's try
  box with its CSRF header.
- Only a person makes, edits or revokes a key. Comparison is constant time on the hash.
- Failed key lookups are limited per source (`hook_limits` style) so keys cannot be guessed at speed.
- A key in a query string is refused, even beside a good header.

### Answers

```json
{
  "data": { "orders": [ { "number": "A-1042", "total": "1,204.00", "url": "https://..." } ] },
  "status": "succeeded_verified",
  "untrusted_fields": ["orders[].number", "orders[].total"],
  "private_fields": [],
  "truncated": false,
  "stale": false,
  "cache": { "hit": true, "age_s": 41 },
  "source": { "api": "harbor-supplier", "endpoint": "open_orders", "version": "1.0.0",
              "surface": "harbor/supplier", "read_at": "2026-11-05T09:00:00Z", "run": "run_..." }
}
```

Headers: `Age`, `ETag` (of `data`; `If-None-Match` answers 304), `Agentos-Run`, and `Cache-Control:
no-store` when the endpoint has a private field. `RateLimit-Limit`, `RateLimit-Remaining`,
`RateLimit-Reset` for the key.

Errors are `application/problem+json` with `{type, title, status, code, detail}` plus `retry_at` or
`run` when they apply. `detail` is a sentence for a person. No site text goes in `detail`.

| Status | `code` | Meaning |
|---|---|---|
| 202 | `pending`, `approval_pending` | still running, or a person must approve: poll `run` (not an error body; carries `run`, `why`) |
| 400 | `invalid_params` | fails the input schema, or the body is not a JSON object the node can keep (a lone surrogate, or nested over 100 levels) |
| 401 | `unauthorized` | no key, a bad key, an expired key |
| 403 | `denied`, `ceiling` | outside the key's scope, or above its ceiling |
| 404 | `not_found` | no such API or endpoint for this key (the same answer for both) |
| 405 | `method_not_allowed` | a route that does not take the method, such as GET on a `commit`; `Allow` says which |
| 409 | `paused` | a person paused it |
| 413 | `too_large` | the request body is over the limit |
| 421 | `misdirected` | the node's host check |
| 422 | `empty_result` | below `expect.min_items` |
| 429 | `rate_limited_key`, `pace_limited` | the key's limit, or the account's pace; `Retry-After` always |
| 500 | `internal`, `run_failed` | the node failed, or the run ended with no result and no row above fits |
| 502 | `output_contract_violation` | the site changed shape; the value is not passed on |
| 503 | `needs_human`, `rate_limited`, `connector_broken`, `frozen`, `lane_busy`, `node_offline` | see below |

`needs_human` carries no retry header: do not retry. `lane_busy` carries `Retry-After`. `rate_limited`
is the site's own limit: `retry_at` is when the lane's rest ends.

### Cache

A successful `read` is cached on the node per `(flow id, implementation hash, surface, the surface's
binding, canonical params)` for `cache_s`. The binding is who the read ran as: the lane, what it acts as
and the account. A hit starts no run and does not touch the lane or use pace. Concurrent misses
for one cache key share one run. When the grant of the key that started it ends first, a caller still
waiting starts its own run, and another key that polls it answers `run_failed`, never 401: that key is
fine. `max_age=0` still pays pace. A changed version misses, and so does a
surface bound to another lane or account, so a caller never gets the old account's data. Nothing with a
`private` field leaves the node in a shared relay cache; the cache is only ever on the node. Entries
are deleted when the API is paused or removed. The cache keeps the last good result for `stale_ok`,
except for an endpoint with `cache_s` 0, which keeps nothing. A try never touches the cache.

Auth comes first and the cache second: a hit checks the key, its limit, its scope, a pause, the grant
and a freeze like any call. A hit on a held lane still answers, because it does nothing on the lane and
the value was read in good standing inside `cache_s`; a miss answers the hold.

### Usage

One row per call in `site_api_usage`: key id, API, endpoint, surface, status, code, duration, cached,
time. Never params, never data, never the source address. Kept for 30 days, with an hourly rollup that
feeds `GET /api/site-apis/{api}/usage`.

## OpenAPI

Generated, never hand-written: `openapi: 3.1.0`, because draft 2020-12 is what operations already use,
so `input` and `output` go in unchanged, less `x-agentos-*` marks that have no meaning outside. Each
endpoint is an `operationId` of `<api>_<endpoint>`, with GET and POST for `read` (its POST is
`<api>_<endpoint>_post`, because an `operationId` is unique in a document), the problem schema, the 202
shape, bearer security, and `x-agentos-effect`, `x-agentos-untrusted-fields`, `x-agentos-cache-s`. A key
gets only what it can call: an endpoint outside its scope or above its ceiling is left out. An input
param named `on`, `wait`, `max_age` or `stale_ok` is refused here, not renamed. The last good result, when it has no private field, is **not** used as an example:
examples come from the input schema only, so the document holds no site data and can be shared.
`servers` is the node URL or the relay URL. The document must pass an OpenAPI 3.1 validator in tests,
and an import into n8n's HTTP node needs nothing else.

## Agent tools

`site_api_discover(on, url?)`, `site_api_try(draft, endpoint, params)`, `site_api_propose(draft)`,
`site_apis_list()`. All on grants as usual. Proposing makes a card in Needs you for a person to
activate; the card shows everything that runs (each step with its locators and css, every field with its
`capture` and `pattern`, `cache_s`, `pace`, `expect` and any commit or discard) in the node's words, never
the agent's title or description. Nothing on it is cut. A proposal too long for one card is refused with a
message to split it up, and so is one with a locator name that is not a plain label, since that name would
have to go on the card or stay hidden. It is a question (`Activate`, `Leave it`), and the owner's `Activate`
activates exactly the content on the card; a changed draft voids it. Only the owner in person activates there: signed in to the panel,
or at the node's CLI. A bearer token never does, because an agent may hold one: the node token and an
admin grant can answer the card and it stays a draft, and the agent that proposed it (or a grant narrowed
from the same root) cannot answer it at all. With an agent's input steps on it the card offers no `Activate`:
those need a vouch, in the panel or with `--vouch`. Once active an endpoint is the ordinary
`<api>_<endpoint>` connector tool, listed when the MCP server next starts, like any connector's.

## Owner API and CLI

`GET|POST /api/site-apis`, `GET|PATCH|DELETE /api/site-apis/{api}`, `POST
/api/site-apis/{api}/activate|pause|try`, `POST /api/site-apis/discover`, `GET
/api/site-apis/{api}/usage`, `GET|POST /api/site-keys`, `PATCH|DELETE /api/site-keys/{id}`. Owner or
admin as in `openajar.api.mounts`; activate, keys and widening need the owner.

`openajar site-api discover|new|try|activate|pause|list|show|openapi|remove` and `openajar site-api keys
add|list|revoke`.

A pause is a state on the draft: its runs wait and every entry point answers `paused`
until the owner activates it again, except a person's try. A step that types into a sensitive field is dropped when a draft is
saved, and the answer lists it in `dropped`.

## Reading the site's own JSON

Not run on Camoufox yet, the node's production engine: only in real Chrome. `tests/node/test_read_json_node.py`
covers it and needs Linux, so treat this as unproven in production until that passes.

Many pages load their data as JSON, and that survives a redesign far better than the page does. A
`read_json` step takes an endpoint's values from a response the page itself made:

```json
{ "type": "navigate", "url": "https://portal.northsupply.example/orders" },
{ "type": "read_json", "save_as": "orders", "url": "^https://portal\\.northsupply\\.example/api/orders",
  "items": "data.orders", "max_items": 100, "timeout_ms": 10000,
  "fields": { "number": { "path": "id", "type": "string", "max_chars": 40 },
              "total": { "path": "amount.value", "type": "number" } } }
```

It saves `{items, count, total, truncated}` as an `extract` does, so `outputs`, `expect.min_items`, the
output schema, OpenAPI, a `try`, a workflow step and health treat it the same. It is a read. A runner
without it marks the step pending and refuses the install.

- **Passive.** The step comes right after the `navigate` it watches (past other `read_json` steps and the
  compiler's `assert_identity`), and listening starts before that page loads. It listens to responses of
  the `fetch` and XHR requests the page's own code makes. It never makes, repeats, alters or delays a
  request, sends no header of its own and adds no route, so the site sees a normal visit. A response
  the browser preloaded for the page (`<link rel=preload>`) or a service worker answered is not seen.
  A body is read only from what the browser kept. On Chromium that goes straight to the browser's own
  copy over a session of the step's own, because Playwright's usual call asks the network again when
  the copy is empty under a Content-Length. So a body it cannot read that way fails as `json_unreadable`.
- **Which response.** The first that matches after the page commits, whatever its status. Later matches
  are ignored and never read, and a response of the page being left never counts. A redirect is a hop:
  what it leads to is the response, if that matches too. `timeout_ms` (100 to 60000, default 10000) is
  how long to wait for it once the page has loaded.
- **`url`** is `^https://`, a literal host, then a path. It must sit under `domain`, checked as any URL is
  (a port is allowed, a user part is not), and it matches from the start of a URL and to the end only
  with `$`. It is at most 200 characters, and it is not free regex, because it runs against every
  response a page makes, on the node's event loop: a dot and a `?` are written `\.` and `\?`, and only
  `[^/]+`, `[^&]+`, `[0-9]+`, `\d+`, `[a-z0-9_-]+` and `[A-Za-z0-9_-]+` may repeat, each followed by a
  character it cannot match. So each has one place to end and one pass over the URL decides, whatever
  the pattern. A URL over 2048 characters is never tested. At run time a response must also be https
  under `domain` itself. No `${param}`.
- **Paths.** `items` is a plain dotted path to the list (`.` when the body is the list) and a field's
  `path` a plain dotted path under one item: keys of letters, digits, `_` and `-`. The one list is the
  only level that repeats: no wildcard, no index, no expression, nothing evaluated. A path with any
  part that names a token, password, secret, session, cookie or authorization is refused (`session.id`,
  `token.value` and `credentials.key` too), because a site's credentials must never reach an output.
  That is a net for a mistake, not the defence: a key can be
  called anything, and what keeps a secret in is that only declared fields leave and a person activates.
- **Fields.** `type` is `string`, `number`, `integer` or `boolean`. A value of another type is an
  `output_contract_violation`, never converted (a `"12"` is not a number and `1.0` is not an integer). A
  key that is missing or null reads as null, and the output schema decides whether that fits. A string
  may set `max_chars` (clipped, `truncated` says so), `format` (`date-time`, `uri`), `host`, `private` and
  `untrusted`; a string is untrusted by the rules under **Labels**, a `number`, `integer` or `boolean`
  is not.
- **The body stays in memory.** A body is held only for the step, at most 5 MB, and is never written
  down. Only declared fields leave: not to outputs, the step journal, traces, proof, Activity, the
  cache or a log, and screenshots do not gain it. An error names the step's own pattern, a status or
  what kind of value came, never the response's URL or a value.
- **No value in a problem.** This holds for every connector and every read, not only this step: a run's
  error, a readback's `found`, a preflight, an identity check, notifications, health and what a caller
  gets say which field and what kind of value came, in the schema's words. jsonschema's own text is not
  used, because it quotes the value. A wrong account reads `another account`. Saved values stay in the
  run's private step state, out of the step journal, and one a template carried into a URL, a locator
  or a check reads `${name}` in a failure if it is at least 8 characters.
- **When the site says stop.** A 429 is the site's own limit and a 401 is a session that ended, so
  neither is a broken contract. The lane hears them as it hears a page's signal: `rate_limited` rests
  it and `logged_out` holds it, for every agent, flow and schedule, the run ends without a retry, and
  the caller gets `rate_limited` or `needs_human`. A 403 stays `json_status`: it can be one caller's
  params, and a key must not be able to hold the lane with them. So is a 401 when a param a caller gave
  fills the `navigate` URL, and a default does not count as given: with no such param (a schedule or a
  watch on a param-free endpoint) it holds as above.
- **A try and the lane.** What a response said holds nothing in a try, because a draft can point at any
  response on the domain, so an agent's draft cannot hold or rest a lane with a 401 or a 429. A try stops
  and says why, and does not go back after a `rate_limited`. A page's challenge is another matter: a
  captcha, a checkpoint, a logged out page or the wrong account is a fact about the lane, so it holds the
  lane in a try as anywhere, and an agent cannot keep trying a challenged account.
- **When the answer is wrong.** A status outside 2xx that is not a 5xx (`json_status`), a body that is
  not JSON (`json_invalid`) or one over the cap (`json_too_large`) fail the run as
  `connector_contract_broken` with that `cause`, and a body of another shape as
  `output_contract_violation`, so health treats them as it treats a broken extract, params rule
  included. A signal comes first: the page's signal is read before any of these, and a captcha, a
  checkpoint or a logged out page stops the run as usual.
- **A bad moment.** A 5xx (`json_status`), no match in time (`json_not_seen`) and a body that did not
  finish (`json_slow`) say nothing of the contract on their own, and with no params a broken endpoint
  does not mend itself. The run goes back as any transient failure does, at most 3 attempts in all, and
  then fails under that code. A site API's retry loads the page again, so its own first visit counts:
  a retry `pace` does not allow now ends the run with what it met. Health marks the endpoint `degraded`
  and only `broken` after 3 such runs in a row on one version, and any success clears the count. The
  params rule holds here too: one param set alone stays `degraded`, however many times. No body on a 2xx
  (`json_empty`, a 204 most of all) fails the run at once and is not retried, and it counts as a wrong
  shape once it repeats: two in a row make it `broken`.
- **The wait.** `timeout_ms` is for the response to come. A response that came and whose body is still
  being read is waited for up to 20 seconds more, and then it is `json_slow`, never `json_not_seen`.
- **A body lost.** A body the browser no longer holds (`json_unreadable`) is retried and says nothing of
  the contract.
- **A 304.** It is never a contract failure: the page got its data. When Chrome revalidates a response
  itself, the server's 304 reaches the page and the node as a 200 with the cached body, so a site with
  ETags reads as usual, and a response Chrome serves from its cache reads without asking the site. A
  real 304 comes only when the page sends its own `If-None-Match` and keeps its own copy, and then no
  body reaches the browser. A body if there is one is read. Otherwise the run fails as `json_unreadable`
  and is not retried, because the same page would answer the same. Health does not change, so the
  endpoint's last good answer stands. Camoufox is not checked yet: see `tests/node/test_read_json_node.py`.
- **On resume** the watch is gone, because it lives in memory, so a read that did not finish loads its
  page again with its identity check. That is a new visit of the run, never a replay of a request.
- **Discovery.** `discover` with a `url` adds `json`: for up to 6 responses from the page's own site with
  a JSON content type, `{name, path, shape, untrusted: true}`, and where it finds a list of records also
  `{url, items, count, fields}` to start a step from. `path` never has a query, and a part that looks
  like an id or a token (a number, hex, a uuid, base64, anything a plain word never holds) reads `{id}`.
  `name` comes from a part that does not, and `url` has `[^/]+` there. It is a guess that errs toward
  hiding, because a URL can hold a token. `shape` holds keys and types, clipped, never a value. A key
  that is not a plain path part is left out, and one the path rule refuses reads `hidden` and is never
  proposed. A map keyed by ids holds values in its keys, so it reads `map of N`, its keys are not
  listed, and nothing under it is proposed. The bodies are dropped there.
- It belongs to a connector or a site API: a plain flow has no domain it must hold to, no output
  contract and no labels, so `flow_save` refuses it.

## Watching for changes

A `site_change` workflow trigger runs a `read` endpoint on a schedule and starts the routine with
`added`, `removed` and `changed` rows, keyed by a field the trigger names. `docs/WORKFLOWS.md` has the
whole rule. What it means for an API:

- A check is an ordinary run of the endpoint on its surface, so the lines above hold, and every check
  counts against pace. A trigger that alone would use more than half of `per_hour` is refused at save, and
  so is one faster than `cache_s` or 5 minutes. Only a `read` endpoint can be watched, and that stays true
  of what is active: `activate` refuses a version in which an endpoint a watch that is on reads would stop
  being a read, naming the routine, and the effect is checked again before each check, which stops the
  watch with the reason on the routine.
- A check is always a fresh read, and a good one fills the cache as any run does. It writes no usage row,
  because it is no HTTP call: it shows as a run.
- A failed read keeps the last good rows, and that covers `empty_result`. A read that would remove half
  or more of them needs a second read in a row, whatever `expect` says, and only the rows both lost are
  removed. An endpoint a contract failure left broken is not read at all until a repaired version is active.
- A read that may not hold every row (cut at `max_items`, trimmed by the output cap, or with a row that has
  no key) calls nothing removed. A clipped value does not count: a watch reads the list's own `count` and
  `total`, not the run's `truncated`.
- Text in the rows is untrusted, as for any other trigger. A commit or send in a loop over the rows asks
  first even with literal inputs, because the site decides how many there are. The rows the node keeps to
  compare stay on the node, and a `private` field is kept only as a digest. A watch's key cannot be
  private: `activate` refuses a version that makes it so, and a read of one fails.
- A pause stops the watch, and removing an API or dropping an endpoint is refused while one uses it. A new
  version with rows of another shape starts the watch over from its next read.

## The cloud (phase 3)

`hello.supports` gains `"site_apis"`. The node sends a `site_apis_catalog` report with metadata only:
names, endpoints, schemas, health, usage counts. A hosted key is checked in the cloud and relayed as a
signed `site_api_call` over the channel, with the same rules as the webhook relay, and the answer
passes through in memory: the cloud stores no params and no data, and logs neither.
