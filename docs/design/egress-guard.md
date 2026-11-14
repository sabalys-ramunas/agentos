# The lane egress guard: site lists and effect classes, enforced on the node

Status: built 2026-11-04 (`agentos.node.egress`, `agentos.node.routes`); the pre-check was rebuilt
the same day after an internal QA pass. The same day, after
the owner's first real sign-in through the live view (a throwaway Reddit account, Google sign-in),
the identity group, `frame:` and `@cc` entries, block notices in the live view and the daemon's
site-list route were added (sections 4.2 and 5). The commit gate (AUD-03) was built on it the same
day (section 8). Backlog: "Site allowlist and effect classes per lane" (Trust and security), AUD-03,
AUD-09 and AUD-31.

## 1. Goal

An injected instruction must not be able to take a logged-in lane to a host, or to an effect,
that the lane and the grant driving it do not allow. Before this, only `browser_navigate` checked
the host. A click on an injected link, a form that posts off-site, a popup or a page-driven
redirect went through.

The hard constraint: lanes are real, logged-in accounts, and a lane must not look different from
a normal Firefox. So the mechanism was chosen by measuring what each candidate does to the
browser (section 3) before writing any of it.

## 2. What is allowed

- A **document navigation** (top-level or a subframe; from a click, a form submit,
  `window.open`, a meta refresh, a script, or any server redirect hop) may go to a host that is
  inside the lane's site list (when it has one) **and** inside every egress list of the grant
  driving the lane (when an agent drives it and the grant sets one).
- A `frame:` entry admits its host only as a **subframe** document (a redirect hop inside a frame
  included), never as a page, a popup or a top-level redirect. Captcha widgets and sign-in helper
  frames are listed this way (section 5).
- While a **human has control** (take over), nothing is refused. Off-list documents are noted in the
  lane's egress trace instead (`kind: noted`), so the owner can add the hosts for agents later. Changed
  after the first real logins (2026-11-04): Google sign-in went through `accounts.google.lt`,
  `gds.google.com` and `accounts.youtube.com`, and Meta's captcha came from `www.fbsbx.com`. No list
  predicts these, and blocking a person who took control protects nothing (the guard exists to stop
  an agent being steered off-site by text it read).
- **Subresources** (images, scripts, fetch and XHR GETs) are never blocked.
- A **non-GET request** (a form post, a fetch POST) while an agent drives the lane may go only
  where a document may go, a `frame:` host included (a captcha frame posts its answer to its own
  host).
- A request matching a **commit rule** needs a grant that allows `commit`; a **write rule** needs
  `draft`. Unknown requests are never blocked.

An empty site list means "no lane restriction", and a grant that sets neither `egress` nor
`domains` sets no list. A lane made before this, driven by such a grant, behaves exactly as it
did: nothing is judged differently and no route is installed.

"The grant driving the lane" is the grant of the last agent call that entered the lane (every
browser tool sets it). It stays the driver between that agent's calls, so a page that navigates on
its own later is judged by the same grant. A take over switches to the lane's list alone. A
grant's lists are one per block that sets one (its `egress`, else its `domains`), and a host must
be inside all of them, the way `Grant.egress_allows` reads them, except that a grant with no list
at all is not restricted here (its trifecta check in policy still applies to `browser_navigate`).

### The rules, and the threat each one covers

| Rule | Blocks | Threat it covers |
|---|---|---|
| `lane_sites` | a document outside the lane's site list while an agent drives (a human in control is never blocked) | an injected link, redirect or popup taking the account's session (and the agent) to another site |
| `grant_egress` | a document outside the driving grant's egress list, while an agent drives | an injected instruction making the agent carry data off in a URL (AUD-09) |
| `non_get` | a non-GET request outside the lane's list or the grant's list, while an agent drives | exfiltration by form: the agent fills a form that posts its data off-site |
| `commit` | a request matching a commit rule under a grant below `commit` | a plain click on "Share" publishing under a grant that may only draft (AUD-03) |
| `commit_gate`, `publish_changed`, `publish_unbound` | a request that matches a commit rule, or that the gate cannot tell from one, while an agent drives the lane, until a person approved exactly what it publishes; what cannot be bound whole is refused (section 8) | a plain click on "Share" publishing under a grant that may commit, with nobody seeing what (AUD-03) |
| `write` | a request matching a write rule under a `read` grant | a like or an upload under a read-only grant |

## 3. Measurements (node, 2026-11-04)

Camoufox 0.5.9 (Firefox 158.0.1, beta.41), Playwright 1.62.0. A throwaway profile per scenario,
`enable_cache` on (as the engine launches lanes), no proxy, and two loopback servers on the node:
127.0.0.1 stands in for the lane's site and 127.0.0.2 for a foreign host. No real site was
visited. The spike is `scripts/spikes/egress_guard.py`.

### 3.1 Source reading

- Playwright's Firefox driver (`doUpdateRequestInterception`) sends
  `Browser.setCacheDisabled(cacheDisabled: requestInterceptors.length > 0)` for the context. So the
  cache is off whenever **any** route exists, whatever its pattern. A narrow glob does not help.
- Camoufox's Juggler (`TargetRegistry.updateCacheDisabled`) implements that flag as
  `INHIBIT_CACHING` on every page's default load flags (Camoufox dropped upstream's
  `LOAD_BYPASS_CACHE` so no `Pragma: no-cache` is sent). Responses are never stored; entries
  already stored are still read.
- Juggler never intercepts a redirect hop (`_shouldIntercept` returns false for a request with a
  `redirectedFrom`), so a route cannot judge server redirects. The identity Phase 0 spike measured
  the same thing (0 handler calls for 6 hops).

### 3.2 What a route does to the cache

A page with a no-cache HTML document, a cacheable stylesheet, two scripts (one no-cache with an
ETag) and five 20 kB images (`max-age=3600`). Visit `p1`, then `p2` (the same assets), reload, then
`p1` again. Server-side counts for the second and later visits:

| Route state | `p2` after `p1` | Reload | `p1` again | Page load median (ms) |
|---|---|---|---|---|
| No route | 2 requests, 590 B (one 304) | 2 requests, 0 B | 2 requests, 0 B | 121 |
| `context.route("http://route-never.invalid/**")` (never matches) | 10 requests, 131 kB, no 304 | 10 requests, 131 kB | 10 requests, 131 kB | 228 |
| `context.route("**/*")` that falls back for everything but documents | 10 requests, 131 kB, no 304 | 10 requests, 131 kB | 10 requests, 131 kB | 225 |
| `request` and `framenavigated` listeners only | 2 requests, 590 B | 2 requests, 0 B | 2 requests, 0 B | 118 |

With any route, every visit downloads everything again and sends no conditional request, which a
server can see, and page loads take about twice as long on loopback. Listeners change nothing.

A route held only for a moment (an agent's action): visit `p1` with no route, add the narrow route,
visit `p2` (`p1`'s assets), visit `p3` (five new images), remove the route, visit `p4` (`p3`'s
images):

| Step | Requests | Static 200s | Bytes |
|---|---|---|---|
| `p2` while the route exists | 2 | 0 | 590 |
| `p3` while the route exists | 7 | 5 | 103 kB |
| `p4` after the route is gone | 8 | 5 | 103 kB |
| `p4` again | 2 | 0 | 0 |

So entries stored earlier are still read while a route exists, but whatever arrives during the
window is never stored and is fetched again in full afterwards.

### 3.3 What a `request` listener sees, and how late

One trigger per row, each to the foreign host (which answers after 2 s). "Received" is when the
foreign server logged the request arriving, relative to the trigger; "event" is when the driver's
`request` listener heard of it.

| Source | Event (ms) | Received (ms) | Navigation | Frame | With `window.stop()` on the event |
|---|---|---|---|---|---|
| JS `location.href` | 29 | 30 | yes | main | page stayed |
| Link click | 184 | 185 | yes | main | page stayed |
| Form POST | 175 | 177 | yes | main | page stayed |
| Meta refresh | 107 | 104 | yes | main | page stayed |
| Server redirect hop | 41 | 46 | yes (`redirected_from` set) | main | page stayed |
| iframe | 116 | 99 | yes | sub | frame stopped |
| `window.open` popup | 87 | 88 | yes | not yet created | popup opened (it must be closed) |

The listener sees every kind of document navigation, every redirect hop included. But it hears of
a request at the moment it reaches the host, never before. Stopping it keeps the page where it was
(containment); it cannot keep the URL from leaving (prevention).

## 4. The mechanism chosen

No lane gets a permanent route: that would change how every lane talks to every site. Instead:

1. **Prevention before an agent acts.** `browser_navigate` checks the URL (`LaneGuard.check_url`),
   and `browser_act` resolves from the DOM what the action would follow or submit
   (`check_action`, section 4.1) and refuses before acting. Nothing is sent.
2. **Containment for everything.** A context `request` listener judges every navigation and every
   non-GET request. A refused navigation is stopped with `window.stop()` in its frame, a refused
   popup is closed, and a refused document that commits anyway (the stop lost the race) is left
   with `go_back`. A refused non-GET request cannot be stopped this way: it is recorded, and a
   refused commit holds the lane (`egress:commit <host>`), so the agent stops until a human looks.
3. **Strict lanes, opt in.** `agentos lane sites <lane> strict on` adds a catch-all route through
   the lane's `RouteDispatcher` only while an agent's action runs (navigate and act, not waits).
   It aborts refused requests before they leave. Its costs, measured above: requests pause for the
   driver, and responses that arrive during the action are not stored in the cache. Redirect hops
   are still only contained.
4. **One route dispatcher per context.** `agentos.node.routes.RouteDispatcher` owns every
   `context.route` of a lane. The Web Bot Auth `LaneSigner` installs its per-origin routes through
   it, exactly as it did on the context; the strict guard is a dispatcher guard and runs first, so
   a refused request is never signed. A routed request is dispatched once, and with no signer and
   no strict window the context has no route at all. Both fail closed: a guard that raises aborts
   the request (no owner sees it), and the strict guard aborts a request it cannot read.

### 4.1 What `check_action` reads

It runs for a click, a press of Enter or Space, a `type` or `fill` whose text holds a newline (a
newline is an Enter), and a `select` (it ends with Enter on a dropdown). It runs only on a lane
that limits its hosts: a site list, or a driving agent grant with an egress list. Elsewhere it
reads nothing, so a lane with no list under a grant with no list behaves exactly as before (its
commits are still judged by the listener, after the fact). It resolves the target the way the
browser does (`TARGET_JS`, in Camoufox's isolated world, so page scripts cannot patch what it
calls):

- **The composed path.** From the element up through slots, shadow roots and their hosts, every
  link (`a` or `area`, HTML or SVG, with `href` or `xlink:href`) and every submit button. An SVG
  link's address is its `href.baseVal` resolved against the document's base.
- **Labels.** A label on the path, or one of the element's labels (the pointer can land on it),
  activates its control: the control's path counts too.
- **Under the pointer, for a click.** The press lands at a random point in the middle of the
  element, which can be any rendered descendant. So every rendered link, submit button, label and
  image map area inside the element counts, through open shadow roots and same-origin frames. A
  cross-origin frame inside the element cannot be read: refused.
- **Frames.** For a target inside a frame, the target's press points are hit-tested in every parent
  document on the way down; an element laid over the frame (a parent link) takes the press, and
  what it leads to counts. `human.click` does the same cover check through frames, so it never
  presses on a cover (before, a frame target skipped the cover check).
- **Forms.** A submit button goes to its form owner (the `form` attribute included) with its
  `formaction`, `formmethod` and `formtarget`. Enter in a field (an `input` or a `select`) is
  implicit submission: the form's default button (its first submit button in tree order) with its
  overrides, and the form's own action. Form attributes are read through `HTMLFormElement`'s
  prototype, so a control named `action` or `method` cannot stand in for them.
- **Effects.** A non-GET form is classified against `EFFECT_RULES`, so a form whose action is a
  commit endpoint is refused under a grant below `commit` before the click, not held after it.
- **What cannot be judged is refused:** a `javascript:`, `data:`, `blob:` or other non-web link or
  action (only `about:` passes), a link whose address cannot be read, a frame whose position cannot
  be read, an element too large to walk (20 000 elements) or with more than 500 distinct targets,
  and a `type` or `fill` with a tab before its newline (the Enter lands in a field the check did
  not read). The refusal's rule is `scheme` or `unreadable`.

### What a refusal looks like

- The tool call fails with `egress_refused: <host> is not on this lane's site list; the page stays
  where it was (nothing was sent)`, or "(the request reached the host and was stopped)" for a
  redirect hop the listener stopped.
- Anything else refused during the call (a frame, a popup, a POST) is listed in the result as
  `egress_blocked: [{host, rule, reason, kind, method, prevented}]`.
- Each block goes to the call's action log (`egress_blocked`), to the lane's egress trace
  (`lanes/<ws>/<name>/egress-trace.jsonl`, mode 600, host, path and method, never a query, headers
  or a body), to the lane events (`egress_blocked`) and to the log. Repeats of one (rule, host,
  kind) are limited to one log line and one lane event a minute and one trace row a second, each
  with a count of what was folded in. Every block still reaches the tool result.
- A commit the grant does allow is traced as `observed_commit` (host, path, method, body size,
  the rule and whether it is verified). During an agent's act the commit gate holds it first
  (section 8).
- **Observing a lane** (added 2026-11-04 for the connectors canary): `agentos lane sites <lane> observe
  on [minutes]` (default 60, at most 240; `off` ends it) writes an expiry to
  `lanes/<ws>/<name>/egress-observe`, which a running guard rereads every two seconds. Until then every
  non-GET request the guard lets through is traced as `observed_request`, and every observed commit gets
  a `shape` too: the content type, an operation name (from `x-fb-friendly-name` and similar headers, or
  an `operationName`, `operation` or `fb_api_req_friendly_name` key) and the names of the body's fields
  (JSON paths, form and multipart names, the fields inside a form field holding JSON such as Meta's
  `variables`). Never a value, a query or a header other than the operation-name ones. Without it a
  publish request that no rule matches left no trace at all, so a wrong rule could not be seen.

### 4.2 Block notices in the live view

When the guard refuses a document while a human has control, the human sees it in the live view
instead of a blank captcha or a page that does not move. The guard sends a notice through the
lane manager (`add_notice_listener`) to every open viewer event stream of the lane (`egress` events
on `/view/{ws}/{name}/events`). A notice has the host, the path (never the query), the kind
(subframe, redirect, navigation, popup, committed; `frame` says whether it was inside a frame), the
rule, the reason, the entry that would admit it (`frame:` for a frame, `.@cc` for a Google
country domain) and the exact command (`agentos lane sites <lane> add <entry>`). One notice per
(rule, host, kind) every 30 s, with a count of what was folded in, and at most five a lane in any
30 s. Nothing is sent while an agent drives: its blocks are in its tool result.

The page shows each notice above the live view (never over it), with Copy for the command. An
owner link (a control link the owner asked for with the node token or the owner's panel session:
`agentos lane view --control`) also shows "Allow <entry>", which posts to
`/view/{ws}/{name}/sites` and goes through the same change as `POST /api/lanes/{ws}/{name}/sites`.
Links from `lane_view` (the agent's) and from grants never carry the owner flag (it is in the
signed token), so an agent cannot widen a lane from the page.

## 5. Site lists and effect rules

- `LaneSpec.sites` (domain patterns in the policy's globs syntax: `instagram.com` is that host
  only, `*.instagram.com` any subdomain, `*` refused) and `LaneSpec.egress_strict`. Old lanes read
  as an empty list and strict off. Two narrow forms on top, validated in `core.models.validate_sites`:
  - `frame:<pattern>`: the host only as a subframe (section 2).
  - `<host>.google.@cc` (or `*.google.@cc`, `google.@cc`): that host on any of Google's country
    domains, `core.models.GOOGLE_COUNTRY_SUFFIXES` (the 187 entries of
    https://www.google.com/supported_domains, read 2026-11-04). `@cc` is refused anywhere but right
    after `google.`, so a bare TLD wildcard cannot be written.
- Set by `agentos lane sites <lane> show|add|remove|clear|suggest [platform]|strict on|off`, by a
  workspace template surface (`sites = [...]`), or at creation. While agentosd runs, the CLI calls
  `POST /api/lanes/{ws}/{name}/sites` (the owner only, never a grant), which changes the list on the
  daemon's own lane manager, so a warm lane's guard has it at once. When agentosd is not running (or
  predates the route) the CLI writes the store, which the next lane start reads (a running guard
  also rereads the store every two seconds, but the owner's first sign-in showed a warm lane keeping
  its old list, and a restart in the middle of a login is what the route avoids).
- `egress.SUGGESTED_SITES` has lists for instagram, facebook, reddit, linkedin and medium: each
  platform's domains and CDNs (`PLATFORM_SITES`) plus the shared `IDENTITY_SITES`. `suggest` without a
  platform prints them; nothing applies them on its own.

### 5.1 The identity and challenge group

Every suggestion includes it, whatever sign-in the platform offers, because a captcha or a sign-in
provider can show up on any of them. From the providers' public docs (nobody logged in anywhere):

| Entries | Why | Source |
|---|---|---|
| `accounts.google.com`, `accounts.google.@cc` | Sign in with Google: the GIS button and One Tap frames (`/gsi/`), the popup or redirect (`/o/oauth2/v2/auth`, `/v3/signin`), and after the sign-in the `SetSID` redirects through `accounts.google.com` and the country domain of the exit IP (`accounts.google.lt` here) | developers.google.com/identity/gsi/web (CSP section), the OAuth 2.0 web-server guide; the SetSID hop is not in Google's docs, it is what the owner's sign-in hit and what other embedders report |
| `consent.google.com` | the cookie consent page Google can put in front of a sign-in from the EU | low confidence; harmless (a Google page) |
| `accounts.youtube.com` | the `CheckConnection` frame the Google sign-in page embeds, and a `SetSID` hop for YouTube's cookies (both seen in public reports), so a plain entry | Google's password-alert extension source, Mozilla bug 1319728 |
| `appleid.apple.com`, `frame:idmsa.apple.com` | Sign in with Apple: the popup or redirect (`/auth/authorize`), and the frame its sign-in form runs in | developer.apple.com, Sign in with Apple JS |
| `frame:www.google.com`, `frame:recaptcha.google.com`, `frame:www.recaptcha.net`, `frame:recaptcha.net` | reCAPTCHA v2, v3 and Enterprise frames (`/recaptcha/api2/anchor`, `bframe`), and recaptcha.net where google.com is not reachable | developers.google.com/recaptcha/docs/faq |
| `frame:hcaptcha.com`, `frame:*.hcaptcha.com` | hCaptcha's checkbox and challenge frames (its docs ask not to hard-code a subdomain) | docs.hcaptcha.com, CSP section |
| `frame:challenges.cloudflare.com` | Cloudflare Turnstile and the managed challenge's widget | developers.cloudflare.com/turnstile, CSP reference |
| `frame:*.arkoselabs.com`, `frame:*.funcaptcha.com` | Arkose Labs: `client-api`, `<company>-api` and `iframe.arkoselabs.com` (LinkedIn's checkpoint, Meta's Arkose frame); funcaptcha.com for old keys | developer.arkoselabs.com, domain policy |

Per platform, from public help pages and write-ups: Reddit offers Google and Apple and uses
reCAPTCHA; LinkedIn offers Google and Apple and uses Arkose; Medium offers Google, Apple, Facebook
and X and sits behind Cloudflare; Instagram and Facebook wrap reCAPTCHA and Arkose in Meta's own
frame on `www.fbsbx.com` (now in the instagram list too). Vanity Arkose hosts some sites use
(`arkoselabs.<site>`) are not listed: a notice names them when they appear.

**Country domains: the syntax, not the exit country.** The alternative was to derive the country
domain from the exit country the lane check's geo cache knows. The syntax was chosen because:
Google picks the domain with its own geolocation, which a GeoIP database can disagree with; the
geo cache is empty until the first lane check and goes stale when the proxy changes
(`agentos lane proxy`), so the list would need rewriting behind the owner's back; a stored list
that says `accounts.google.@cc` reads the same on every node and in every review, while a derived
entry is invisible. The form is narrow: it matches only Google's published country domains, and
only for the host it names.

**Frame-only hosts.** The guard already knows a document's frame (Playwright gives the request's
frame and its parent), so `frame:` is enforced, not documented: an agent's `browser_navigate` to
`www.google.com`, a click on a link to it, a popup or a top-level redirect to it are refused
(`lane_sites`, "only as a frame inside a page"). The widening that remains: while an agent drives,
a non-GET request to a `frame:` host is let through (`non_get`), because a captcha frame posts its
answer there. And Google sign-in hosts are plain entries, so an agent may open
`accounts.google.com` as a page; the sign-in flow needs it as a popup and a redirect.
- `egress.EFFECT_RULES` is the per-site table: host pattern, path regex, method, optional header,
  an optional operation name (`op`, a regex) and the effect. Rule ids are
  `<site>.<kind>[.<detail>]`, kind one of post, comment, like, follow, join (what other people see
  the account do), profile (a change to the account's own public profile, a commit; LinkedIn's,
  still to capture, 8.6), `read` (a read through POST) or `draft` (an upload before a publish). Instagram,
  Facebook, Reddit and LinkedIn each send many actions to one URL, so their rules match the
  operation name: `x-fb-friendly-name` or the form's `fb_api_req_friendly_name` (Meta), the JSON
  body's `operation` (Reddit's shreddit GraphQL) or a dotted `requestId` (LinkedIn's
  `/flagship-web/rsc-action/actions/server-request`). The body is read only when a rule with
  `op` matches host and path. Each operation rule comes before its endpoint's `read` catch-all.

**Verified rules.** On 2026-11-04 the four throwaway accounts made every kind of action with
observe mode on (each request's shape was recorded), and every rule that
matched one carries `verified="2026-11-04 <site> web"`: posts, comments, likes, follows and joins
on Reddit; likes, follows and comments on Instagram; posts, comments, likes, Page follows and
photo uploads on Facebook; posts, comments, reactions, follows and image uploads on LinkedIn.
Before that, an Instagram comment and every like or follow on Instagram and Facebook passed as a
read, Reddit's real publish (`CreatePost`) matched no rule, and a Facebook comment
(`useCometUFICreateCommentMutation`) was taken for a read. The rest (Instagram's photo publish,
the older REST and old.reddit clients, LinkedIn's Voyager client, Medium) are still unverified:
a wrong one either misses a publish or treats one more request as a write than it should.

## 6. What remains unguarded

- **Page-driven requests that are not navigations are only recorded, never stopped, on a lane that
  is not strict.** A page script can send anything with a GET (an image URL with data in the query)
  at any time; that is out of scope by design, since the page already holds its own data. A script
  POST off-site while an agent drives is recorded, and the lane is held only when it is a commit.
- **Page-driven navigations are contained, not prevented.** The URL of a refused redirect hop,
  meta refresh, script navigation or popup reaches its host before it is stopped (measured, 3.3).
  Prevention covers what the agent itself does: `browser_navigate`, and clicks and key presses on
  links and forms whose target is in the DOM.
- **A click whose handler navigates by script** (a button with an `onclick` that sets
  `location`, a link whose `href` is rewritten on `mousedown`, a textarea that submits on Enter) is
  not prevented; the listener contains it.
- **Closed shadow roots.** A link inside a closed shadow root, around the slot that shows the
  clicked light child, is on the event path but out of the check's reach: Camoufox's isolated world
  has no `openOrClosedShadowRoot` (measured), and Playwright cannot target closed roots either. The
  listener contains it; a strict lane prevents it.
- **The press point is chosen after the check.** The check reads the element as it is before the
  wheel scroll. A cover that appears after it is refused by `human.click` (`target_covered`), not
  judged as egress.
- **Strict lanes still cannot route redirect hops** (Juggler), and their route exists only while
  an agent's action runs. The commit gate's route stays on for the agent's whole drive (section
  8.1), so a publish the page sends after an act is held; a publish that arrives by a redirect hop
  is not routed, and during a drive it holds the lane (its first hop on a rule host is routed and,
  being a non-GET no rule reads, held).
- **Flows and the vault** enter lanes without a tool context, so they do not set a driver: the last
  agent driver stays in force (stricter, never looser), or none on a fresh lane, where only the
  lane's list applies. `io_tools` (uploads, downloads) does not set a driver either.
- **Service workers, WebSockets and WebRTC data channels are not guarded.** A WebSocket's
  handshake is a GET (`request` events show it, the route lets it go) and its frames are never
  seen; a WebRTC data channel goes peer to peer over UDP with no request at all; a service worker's
  own fetches are not the page's. A site that publishes over any of them (a chat over a WebSocket,
  a live comment over a data channel) cannot be held by the commit gate or recorded by the guard.
- **Some effect rules are still unverified** (section 5): Instagram's photo publish, the older clients, Medium.
- **`frame:` hosts take non-GET requests** while an agent drives (a captcha posts its answer to its
  own host), and the Google and Apple sign-in hosts are plain entries an agent may open as pages
  (section 5.1).
- **An exposed dev server is a route too** (`agentos expose`, `agentos.node.expose`): while one is
  on, its lane's browser has a route for `*.expose.agentos.invalid`, so it stores no responses either.
  Its host is inside the lane's site list and every grant's egress list only while it lasts
  (`LaneGuard.exposed`, read on every judgement); the effect rules judge what goes there as anywhere.
- **Signing lanes already have routes.** A lane with Web Bot Auth hosts keeps per-origin routes for
  as long as it signs, so on that lane the browser stores no responses at all (3.1). The identity
  design described the loss as limited to signed responses; it is the whole context.

## 7. Code

- `src/agentos/node/egress/` (one file until the split): rules, `judge`, `Driver`, `LaneGuard`, the trace, the tables,
  `gate_pattern` and the gate's route (`acting(gate=...)`, `settle`).
- `src/agentos/node/commitgate.py` (`ActGate`) and `src/agentos/node/publish.py` (normalization):
  the commit gate (section 8; the drive's route, `_gate_rule`, `gql_verdict`, `end_drive`). `src/agentos/policy/cards.py` `publish_card`,
  `ApprovalStore.consume_approved(max_age_s=...)` and `retire`.
- `src/agentos/node/routes.py`: `RouteDispatcher`.
- `src/agentos/node/expose/`: the owner's dev server in a lane (its route, `LaneGuard.exposed`).
- `src/agentos/node/camoufox/session.py` and `engine.py`: every session gets one dispatcher and one guard.
- `src/agentos/node/lanes/` (`specs.py`, `launch.py`, `control.py`): `set_sites`, and the guard's bindings (control, events, store, hold).
- `src/agentos/node/tools/`: `session.driven_session`, `guarding.guarded`, the pre-checks (`form_check.py`).
- `src/agentos/node/human.py`: the hit test `human.click` uses, through frames.
- `src/agentos/cli.py`: `agentos lane sites` (through the daemon while it runs).
- `src/agentos/node/viewer_app.py`: `change_sites`, `POST /api/lanes/{ws}/{name}/sites`, owner links,
  the notice stream and the one-click allow; `src/agentos/node/viewer/`: the notices on the page.
- Tests: `tests/unit/test_egress.py`, `tests/unit/test_egress_wiring.py`, `tests/unit/test_cli.py`,
  `tests/unit/test_qa_egress.py`, `tests/unit/test_egress_identity.py`, `tests/unit/test_viewer_sites.py`,
  `tests/node/test_egress_node.py`, `tests/node/test_qa_egress_node.py`, `tests/node/test_egressux_node.py`
  (the identity group, a blocked frame's notice and a live change on a real lane, behind a loopback
  stand-in for every host). The commit gate: `tests/unit/test_publish_normalize.py`,
  `tests/unit/test_commit_gate_guard.py`, `tests/unit/test_commit_gate_drive.py` (the drive and the
  lane manager's hooks), `tests/unit/test_commit_gate_classify.py` (fail-closed classification and a
  feed session that meets no gate), `tests/unit/test_commit_gate_digest.py`,
  `tests/unit/test_commit_gate_cards.py`, `tests/unit/test_commit_gate.py` (the dev lanes, end to
  end), and `scripts/spikes/commit_gate.py` (a real Camoufox).

## 8. The commit gate (AUD-03)

Built 2026-11-04. Before it, `browser_act` was authorized as a draft, the guard recognised the real
publish request by its commit rule, and under a grant that allows commit it only traced
`observed_commit`: a click on "Share" published with no person seeing it.

### 8.1 The flow

The principle: while an agent drives a lane, nothing that may publish leaves without a person's
approval of what it sends, whatever its timing or shape; what the gate cannot judge, it refuses.

1. `browser_act` builds an `ActGate` (`agentos.node.commitgate`) and runs the act under
   `LaneGuard.acting(gate=...)` (every other acting tool gets a gate too, through `guarded`). When an
   agent drives the lane (never a person in control), the first such act starts the **drive**: the
   guard installs one route through the lane's `RouteDispatcher`, a regex over the rule hosts,
   every path (`gate_pattern`, from `EFFECT_RULES`, plus the dev site's rule on the dev node). The
   route stays until control leaves the agent (`LaneGuard.end_drive`): a person takes over, a pause
   or a hold, the agent's claim ends (released, expired, frozen), the lane sleeps or closes, or a
   person drives through a tool. Each later act binds its own gate and meter to the same route;
   between acts the last act's gate judges. GETs on the route go on at once. An act whose route cannot
   be bound (no dispatcher, or the browser refuses it) is refused before it runs (EngineError), so no
   act is left to `check_action` and the listener alone. A keepalive visit never starts on a driven
   lane (the planner and `--now` skip it); if a visit ends a drive anyway, the drive is bound again
   when the visit ends (`restore_drive`), unless a person or a hold has control.
2. A non-GET on that route is classified fail closed (`_gate_rule`, 8.7): a commit rule's request;
   a request a GraphQL endpoint's read catch-all matched, unless every operation in it is a known
   read; and a non-GET to a rule host that no rule reads (`<site>.unknown`, a possible publish).
   Each is first judged as before (a grant below commit is refused there, prevented since it is
   routed), then goes to the gate before it leaves.
3. The gate normalizes it (`agentos.node.publish.capture`, 8.2) into
   `{"publish": <rule>, "lane": <lane>, "record": {...}}`, with the account the browser is signed
   into on that site (from its cookies, AUD-12) when it can be read. A signed-in account that is not
   the lane's pinned one is refused (`wrong_account`). Then who the body acts as is read and
   resolved (8.10): as a surface through the lane, `lane` is the surface's id. It authorizes that as
   a commit (`ctx.authorize(Effect.COMMIT, content=...)`), the path every approval takes (ROUTING.md):
   - a standing approval with `tool:browser_act` in its targets and this content's digest in its
     `contents` lets it go and uses one unit;
   - an approved, unused approval of exactly this content and grant is consumed (atomically, once)
     and lets it go, unless it was asked for more than `publish_approval_max_age_s` ago (policy.toml,
     default 2 hours, AUD-20): then it is expired and the call refused (`approval_stale`);
   - a denial of this content within `denied_cooldown_s` refuses (`approval_denied`);
   - otherwise a pending approval is created (or the pending one for this content found) with a
     `publish` card, and the request is aborted.
   What cannot be bound whole is refused before that (`publish_unbound`): a body over 2 MB of text,
   in a format the node cannot read, with more than 5000 fields or nested deeper than 32, or one
   that names media the node never saw uploaded in this lane (or saw but could not hash).
4. After a press that sent a non-GET, the act waits for every non-GET it started to finish and then
   for 2 s of quiet, at most 10 s (`settle`): Reddit runs its invisible reCAPTCHA first and
   publishes seconds after the click, and a post can follow its upload. A press whose requests
   have not reached the driver yet waits 0.25 s for them, or, on a browser's first press while it
   is younger than 5 s and has told the driver of no fetch since it launched, until they come, at
   most 2.5 s (`SETTLE_COLD_S`, 8.5; paid once a browser). Settling
   decides only what this act reports; a publish that comes later meets the route all the same.
5. `browser_act` then raises what the gate decided: the tool returns `approval_pending` with the
   approval id ("call browser_act again with the same arguments once approved"), or fails with
   `denied` (`approval_denied`, `approval_stale`, `limit_reached`, `too_many_pending`),
   `publish_unbound` or `publish_changed`, or with `lane_paced` (the lane's posting pace, AUD-13:
   outside its waking hours, within its gap or past its posts for the day, whatever was approved;
   the message says when it may next post). When an earlier publish of the same click left under its
   approval, the message says what was sent and what waits. The result's `egress_blocked` lists the
   held requests (rule `commit_gate`); one held between the agent's calls is listed in its next act.
6. After approval the agent calls again with the same arguments. The act runs again, the page sends
   the request again, and it is normalized again: it leaves only when its digest is the approved
   one. When this same call (grant, lane, ref, action and text) was approved and the click now
   sends other content, the old approval is retired (expired) and the call refused
   (`publish_changed`, "nothing was sent"): the next call asks about what it sends now. Which
   approval a call asked for is kept in memory; after a daemon restart a changed click simply asks
   again (the old approval can still only publish its own content).
7. One publish per approval: a second request with a digest this act already let go is aborted (a
   page that submits twice or retries does not post twice under one approval), and once a publish
   of the act is held, every later one of the act is held too. The approval of one that left is
   used, never retired.

It is metered and paced by the authorize (one commit action; a limit or the lane's pace refuses before
anything leaves, and an approval does not get past either), so the
guard does not charge a request the gate judged. The trace has a `commit_gate` row per decision
(rule, verified, digest, status), never the text, and a `gate_classified` row per shape the gate
classified without a rule (decision, why, field names).

### 8.2 Normalization, per rule

`decode` reads form-encoded, JSON and multipart bodies, and JSON a field carries (Meta's
`variables`); a body over 2 MB (its text parts), or in another format, is refused (a person could
not read it, and the digest never binds a prefix). `flatten`
turns it into `{dotted path: value}` and drops what changes between two clicks of the same post
or is not content, named exactly and never by a part of a name: VOLATILE_NAMES anywhere (CSRF
and captcha tokens, `fb_dtsg`, `jazoest`, `lsd`, client mutation and correlation ids, LinkedIn's
tracking and client request ids), the operation's name at the top (`operation`, `operationName`,
`query`, `doc_id`...), and RULE_VOLATILE's exact paths per rule (Meta's per-request parameters such
as `__req` and `__spin_t`, a composer's logging, tracking and navigation blocks). A scheduled time,
an audience or a target is never volatile. Everything else stays in the
record: the digest binds all the page sends, not only what the card shows, so an injected link
attachment is other content. A field nobody listed as volatile that changes per click makes two
clicks differ, and the gate then refuses after an approval (`publish_changed`): it fails closed.

What the card shows is picked from the fields per rule (DISPLAY):

| Rule | Text | Also |
|---|---|---|
| `reddit.post` (verified) | `variables.input.content.richText` as plain text (the raw rich text stays bound) | title, `subredditName` as target, NSFW and spoiler flags, a link post's url |
| `reddit.comment` (verified) | `content` rich text | the post or comment it answers, from the URL (`t3_...`, `t1_...`) |
| `facebook.post` (verified) | `variables.input.message.text` | `actor_id` (who the body posts as: a profile or a Page), audience `base_state` (FRIENDS, EVERYONE...), photo ids |
| `facebook.comment` (verified) | `variables.input.message.text` | `feedback_id` (what it answers), `actor_id` |
| `instagram.comment` (verified) | `variables.data.comment_text` | `media_id`, a reply's comment id |
| `linkedin.post`, `linkedin.comment` (verified) | the first `commentary`, `text`, ... by name | visibility, `urn:li:digitalmediaAsset`/`image` urns as media, a thread urn as target |
| `instagram.post` (not verified) | `caption` | `upload_id` and `children_metadata[].upload_id` as media |
| old.reddit, Voyager, Medium, Instagram REST (not verified) | by name (`text`, `title`, `url`, `sr`, `thing_id`...) | |
| `linkedin.profile.headline`, `.about` (to capture) | by name (`headline`; `summary`, `about`) | every other field (a name's first and last parts among them), `requestId` volatile as for a post; the photo and cover by their urns as media |

LinkedIn's field names were not all recorded by the canary (it recorded the operation names), so
its shapes in the tests are the server-request envelope with plausible payload names; the first
real LinkedIn publish through the gate should be checked against them (observe mode records the
names).

Media: an upload (a `draft` rule: Facebook's photo, video and resumable uploads, LinkedIn's
`/dms-uploads/` PUT and its media registration, Instagram's `rupload` photos and videos) is
remembered per lane (the last 20) on every path (a strict or capped act too) by the sha256 and size
of its file and the ids it names (URL segments, Instagram's `x-instagram-rupload-params`) or the
site answers with (Meta's `photoID`). A publish that names one of those ids is bound by the file of
the latest upload that made it, so a client-chosen id reused for another file binds the other file.
One that names an id no upload made, or one the guard saw but could not hash (over 64 MB, an
unreadable body), is refused (`publish_unbound`, "media not seen by the node: id only"): no
approval could bind the file, and the approval API has no "I accept an unseen file" tick, so
refusing is the fail-closed default. The agent uploads the file again in the lane and publishes.
A file inside the publish body itself is bound by its hash.

Some media is named by an id the upload never carries; a registration names it first
(`<site>.draft.register...` rules, `publish.registrations`). The node reads the registration's
answer (never its own upload: it carries no file) for the ids of the media it makes and how the
upload of its file is named: an upload URL (host and path), an S3 form's `key`, or an id the
registration request named. Pairs are taken per object of the answer: the smallest object that
holds both, and only when it holds exactly one of each. An object with two uploads and two urns,
or one upload and two urns, cannot say which file is which, so it binds nothing (and no larger
object pairs its parts): a publish naming those urns is refused as unseen, never bound to another
file. A server component stream is read line by line, and as a last resort by its upload URLs and
urns, only when it names exactly one of each; an image registration that names its asset binds
only when its answer gives exactly one other id. The file the guard saw at that upload binds those ids, whichever comes first. None of
these shapes is verified on a real account (the rules' `verified` is empty; the tests use the
public shapes with made-up values):

- **Reddit**: the client leases an upload (`CreateMediaUploadLease`, or `/api/media/asset.json` in
  the older client): the answer names the media id and the S3 form's key; the file goes as a
  multipart POST to `reddit-uploaded-media.s3-accelerate.amazonaws.com` (videos:
  `reddit-uploaded-video...`, both `frame:` entries of the suggested site list, so a POST there is
  allowed but never a page), and the post names the media id in its rich text (`{"e": "img",
  "id": ...}`) or in a `mediaId` field. The key's last segment is an id too, so a post binds even
  when the lease's answer was not read.
- **LinkedIn**: `registerMediaUpload` (the canary saw it before the `/dms-uploads/` PUT), the
  API's `registerUpload` and `initializeUpload`, and the older client's voyager registration
  answer with a urn and an upload URL; `RegisterImageMedia` names the asset it is for, so the image
  urn its answer gives binds the file of that asset's PUT. An asset urn also binds by the id its
  upload URL carries (`urn:li:digitalmediaAsset:<id>`, `/dms-uploads/.../<id>/...`). A profile
  photo or cover (brief B2) goes the same way, with the same rules: its edit names the urn its
  own registration paired with its own upload, so it binds that file. A post's photo uploaded
  earlier on the lane has its own pair and is never bound to the profile edit, nor the profile
  file to a post (`test_a_profile_photo_binds_its_own_file_and_never_a_posts`). Whether
  LinkedIn's profile client registers its upload under a name the register rule knows
  (`...registerMediaUpload`, voyager's `voyagerMediaUploadMetadata`) is what the profile canary
  checks first; while an agent drives, a registration under another name is gated as unknown,
  and an edit naming a urn no registration paired is refused as unseen media.

A sticker or a GIF of the site's own catalog (Facebook's stickers, a GIF from a site's picker:
Reddit names those `giphy|<id>|<size>`) is never a file a user uploads, so it is bound by its id
and shown with a plain label ("a sticker from Facebook's own catalog, not a file (id ...)")
instead of being refused as unseen media. The node tells one by the field that names it
(`attachments[0].sticker.id`, `sticker_id`, `gif.id`, `gif_id`) or by the type its object gives
it (`{"e": "gif"}`, `__typename: "Sticker"`); anything else named by id only (a photo's id under
`media.id`) is still refused, and an id an upload the node saw made is bound by that file all the
same. The site's server decides what an id renders as; the card shows the kind the request gives
it, and the digest binds the id. Every value is bound whole (no text is
cut), and a form or multipart key sent more than once binds all its values in order.

The vault's scrub runs on the summary, the preview and the card (ApprovalStore.scrub, and the
channel's payload scrubs again); the digest binds the raw record.

### 8.3 What a person and an agent see

- The approval: effect commit, tool browser_act, the lane, a node-written summary ("Publish a post
  to test on Reddit through test/reddit: \"Title\""), and a `publish` card: rule, whether the
  rule was verified on a real account, kind, text, title, link, every link URL the request sends in
  full (`links`, from the text, the fields and the rich text), target, the account the body acts
  as, visibility, flags, media (hash and size; an id only is marked "media not seen by the node: id
  only"), each operation of a batch, the other fields (40 shown, `fields_note` counts the rest,
  "the approval binds them too"), the account the browser is signed into now (`signed_in_as`, from
  the session's cookies) with `account_warning` when it is not the lane's expected account, the
  line ("as @handle on Reddit, through test/reddit"; the live account when it differs or none is
  set), who it posts as when the gate resolved it (8.10: `acting`, `identity_line` such as "Posts as
  biz-a (Page)", which a chat confirmation shows as it is, and `identity_warning` for an identity
  agentos does not know, with its raw id), and the digest. A value the card cannot show whole says so: a vault value ("a vault value
  is here; it will be sent"), a secret shape the card hides, characters past the display limit
  (counted; "the approval binds all of them"). An unrecognised request (`<site>.unknown`) has a
  `headline`: "Unrecognised request to Reddit, shape: POST /path with field, field...". It cannot
  be edited ("deny it and tell the agent what to change"). The panel shows its summary and preview
  until it renders the card type; the controller gets `card` and `content_digest` in
  `approval_request` exactly as for other approvals.
- A standing approval for "this post, and the same again": `agentos approvals standing add
  --from-approval <id>` binds `tool:browser_act`, the lane and the digest.
- The agent: `approval_pending` with the id, then `approval_wait`, then the same call. Refusals say
  "nothing was sent".

### 8.4 What the route costs (measured)

`scripts/spikes/commit_gate.py` in the `agentos-stealth-node` image (Camoufox 0.5.9, Firefox 158,
Playwright 1.62.0, loopback pages as in section 3), 2026-11-04:

| Route held for a whole visit | `p2` after `p1` | Reload | Page load median |
|---|---|---|---|
| none | 2 requests, 590 B (one 304) | 2 requests, 0 B | 35.0 ms |
| the gate's regex (848 characters, matches no loopback URL) | 10 requests, 131 kB, no 304 | 10 requests, 131 kB | 89.8 ms |
| `**/*` falling back | 10 requests, 131 kB, no 304 | 10 requests, 131 kB | 95.5 ms |

A narrow pattern does not keep the cache: Playwright turns caching off for the context while any
route exists (3.1), whatever it matches. It keeps requests to other hosts from waiting for Python
(the regex is matched in the driver). Installing took 1.2 ms and removing 1.2 ms; once removed, the
cache works again (the second visit after removal: 0 B).

**A typical feed session** (`feed` in the spike: a feed page with 20 images, three posts opened
from it with two images of their own, back to the feed after each, nine page loads, 26 distinct
images):

| | Requests | Bytes | Image fetches | Time |
|---|---|---|---|---|
| no route | 40 | 555 kB | 26 | 2.10 s |
| the drive's route on the site's host (the real LaneGuard: every request a round trip to Python) | 160 | 2.76 MB | 124 | 2.88 s |
| a route that covers another host (images on a CDN the route does not cover) | 160 | 2.76 MB | 124 | 3.09 s |

So the drive costs the cache, not the Python round trip: every image is fetched again on every page
that shows it (4 times the requests, 5 times the bytes in this session), for as long as the agent
drives the lane. That is the price of the principle (a publish can leave at any time while an agent
drives). It ends when control leaves the agent: a person takes over, the agent releases its claim
(`lane_release`), or the lane sleeps. An agent that reads a feed for a long while and posts once
pays it for the whole visit; the narrower variant (`gate_unknown_posts = False`, 8.6) does not
change it, since any route costs the cache. Requests to hosts the route does not cover do not wait
for Python.

### 8.5 Measured on a real browser, and what that changed

The spike also runs the real `LaneGuard`, `RouteDispatcher` and `ActGate` on a page that copies the
dev composer (a caption and a Share that POSTs a CreatePost body with a fresh client id; the server
log says what left the browser):

| Scenario | Reached the server | What the act reported |
|---|---|---|
| first click | nothing (the page shows its own "Couldn't share") | approval_pending (12 cold first clicks of 12, each on a fresh browser; 3 of 12 before the cold wait below) |
| the same click after approval | exactly one post | ok |
| the caption changed | nothing | approval_pending (a new approval) |
| the publish 1.5 s after the click, behind a captcha POST | nothing | approval_pending |
| the press sends nothing; the page's timer publishes 3 s after the act returned | nothing | ok (the hold is listed in the next act) |
| an operation no rule names (`CreatePostV2`) | nothing | approval_pending (`loop.unknown`) |
| a PUT to another path of the site | nothing | approval_pending (`loop.unknown`) |
| a read named like a query (`FeedPaginationQuery`) | the read | ok, nothing held |
| one click sends two posts, nothing approved | nothing | approval_pending, both held |
| the same, the first approved | the first post only | approval_pending: "1 publish of this click already left ... and was sent (...). A later one did not leave" |
| a person takes over while the drive's route is on, and posts | their post | nothing charged, nothing held |
| after `end_drive` | | no route left |
| the press arms a timer (3 s) on the page a flow then takes, and one (2 s) in another tab (`flow` in the spike, 2026-11-05) | nothing while the flow held the lane; then the flow's own post | the flow's reload (36 ms) killed its page's timer; the other tab's was held (`loop.post`); the flow's post from its page went |

**The cold first click** (`first` in the spike, 2026-11-04). On a browser that has just launched, the
request a first click sent reached the driver 1.1 to 1.4 s after the click returned, though the
page called `fetch` before it returned; the route and the `request` event heard of it at the same
moment, so the act (0.25 s of grace) said ok in 9 runs of 12 and the hold showed in the next act
(nothing left either way). The cause is Camoufox's bundled content blocker (uBlock Origin): it
holds a page's requests until its filter lists load, about 1.5 s after launch (with it excluded: 6
of 6 on time; with the page idle 3 s first: 6 of 6; 1 s: 0 of 6, 250 to 500 ms late). The document
itself is not held (22 ms), so its load says nothing; a round trip to the browser (`cookies()`,
`evaluate`) is no barrier either (it returns in 1 to 6 ms, before the held request). So until the
browser has told the driver of a fetch, XHR or beacon since it launched (`_live_at`), a press waits
for its requests up to 2.5 s (`SETTLE_COLD_S`) and stops 0.25 s after the first one comes. Once the
blocker lets requests go, a lane's pages fetch at once, so a later press waits the usual 0.25 s.
The cold wait is paid once a browser: by its first press, and only while the browser is younger
than 5 s (`SETTLE_COLD_AGE_S`), so a page that never fetches does not make every act wait 2.5 s.
After: 12 of 12 reported by the act that made them, at 3.2 to 3.4 s an act when the request was
held (2.0 s when not), nothing sent. The page is not instrumented: wrapping its `fetch` would be
a change a site can see.

The first version of the gate (a route per act, lingering 1 s) leaked: a request the browser paused
during the act reached the driver after the route was gone, and Playwright let it go; and a publish
later than the act's settling left unheld. Both are why the route now lasts for the drive.

### 8.6 What the gate does not cover

- **What an act reports can lag what the gate held** when a press's request reaches the driver
  after the press's grace (0.25 s) on a browser that has already told of a fetch (8.5 covers the
  cold browser): the act returns before the hold, so the tool says ok and the hold is listed in the
  agent's next act (`egress_blocked`). Nothing leaves: the drive's route holds it either way.
- **Unknown POSTs can be let go instead** (`LaneGuard.gate_unknown_posts = False`): the narrower
  variant. A PUT, PATCH or DELETE no rule reads and an unknown GraphQL operation are still held;
  a POST to a rule host's other paths goes, traced (`gate_classified`, "let go"). It is not the
  default because a 307 from such a POST into a commit endpoint, or a publish endpoint no rule
  knows yet, would then leave unapproved.
- **The telemetry and plumbing read rules are unverified** (`*.read.plumbing`, from the sites' web
  clients' public behaviour; the 2026-11-10 ones from the real node's shapes, 8.13): a POST of a
  site's own logging that no rule lists is held as unknown and not sent. Since 2026-11-10 it makes
  no card unless it carries what the agent typed on the act's press (8.13), but the site still sees
  a client whose call never arrives. Observe mode
  and the gate log (8.9: the `gate_classified` rows by shape) show which shapes to add, and the
  owner can promote one there without a code change. Reddit's GraphQL reads need names
  (READ_OPS): a new one is held until it is listed. Meta and Medium reads named like a query pass.
- **Media from outside the lane** is refused (8.2): a photo uploaded before a daemon restart, or
  in another lane, needs an upload the node sees (catalog stickers and GIFs aside). Reddit's lease
  and LinkedIn's registration answers were not recorded by the canary (it recorded the names):
  the first real Reddit and LinkedIn photo posts through the gate will tell whether the shapes
  (8.2) hold; until then a photo post whose answer the node cannot pair is refused as unseen
  media, never let go. Which Facebook comment fields carry a sticker or a GIF is not recorded
  either: one the node cannot tell from a file is refused as unseen.
- **Requests the route never sees**: redirect hops, service workers, WebSockets, WebRTC data channels
  (section 6). A site that publishes over a WebSocket cannot be held.
- **A redirect hop is judged by the listener only.** A hop is never routed, so a commit that arrives
  by one has left before the gate is asked. During a drive the listener records it (`commit_gate`,
  not prevented), stops the navigation it carried (`window.stop()`, as for any refused navigation),
  and holds the lane. A hop that matches no commit rule (a PUT, or an unnamed POST that a 307 sends
  on) is never gated: the listener applies the grant's checks and lets it go, traced only while the
  owner observes the lane. A publish that arrives by a hop reaches the site once before the lane is
  held, and a hop into a publish no rule names is not held at all.
- **A host no rule names** is not routed: a lane on a site agentos has no rules for publishes as
  before (with "Ask me first" on, a click that submits a form by POST there waits for a person
  before it runs, 8.13; a script's own request does not) (the listener records `unmatched_publish` for an agent's press, and `unruled_request` for
  any other non-GET to a host of the lane's site list; 8.12).
- **Unverified rules** are gated too (a needless approval is better than an unapproved post), but a
  wrong one either misses a publish or asks for an approval that is not one. The LinkedIn profile
  edits (`linkedin.profile.name`, `.headline`, `.about`, `.photo`, `.cover`, kind `profile`, brief
  B2) are all unverified and their operation names are guesses (`com.linkedin.sdui.requests.profile.`
  plus a verb that writes, update or save, then the field and nothing after it), to be replaced at
  their canary: until then a real profile edit may be classified as the read catch-all's unknown
  operation (gated as unknown while an agent drives, refused for a connector run, which declares
  the guessed name), never let go as a read. A name around a field that is not its save
  (`editProfileNameForm`, `saveDraftHeadlineSuggestions`, `updateBackgroundImageUploadPrompt`) is
  no profile edit either: it is gated as unknown the same way. The patterns come after the media
  registrations, so a registration of the profile photo's upload stays a draft.
- **Flows and connectors** are authorized at their own commit effect (their approvals bind the flow
  version and parameters); the gate runs for agents' browser tools. Only who a flow's publish acts
  as is checked (8.10), on lanes whose login can post as another identity. A flow run that takes the lane
  during an agent's drive (`LaneGuard.flow(page)`, entered by the flow driver) first reloads its
  page while the drive's route still gates (its unload's beacons included), so a timer the agent's
  presses armed on that page dies with its document before the flow's window opens. Only then are
  that page's requests the flow's (`_flow_owns`, by the request's page): the drive's route lets
  them go to the listener, which judges them as it did before the gate (the lane's site list, for
  a non-GET too; never the agent's grant, never charged to the agent's act). A request from any
  other page (another tab, a popup the flow opened, a service worker) or from a page that did not
  reload within 10 s stays gated as the agent's, so a flow that publishes from a popup is held for
  a person too (fail closed). The route stays on, and the drive gates everything again once the
  flow lets go. With no drive on, nothing is gated and a flow's page is not reloaded.
- **A connector run is held to the request it declares.** An egress rule is wider than one
  operation: `linkedin.like` takes `reactions.create`, `delete` and `update`, and `reddit.join`
  takes a leave too (`UpdateSubredditSubscriptions` with another `subscribeState`). So when a
  connector run takes the lane, the runner tells the flow driver the operation's commit request
  (`CommitRequest`: method, URL, `op`, and `match` fields such as
  `json:variables.input.inputs[].subscribeState: ^SUBSCRIBED$`; none for a read), and the guard
  binds the rule hosts' route for the run (`LaneGuard.scope_flow`, `FlowCommit`). A non-GET of the
  run's page that a commit or write rule matches leaves only when it is that request; anything
  else (the like's `delete`, the join's leave, a read operation's any write) is aborted before it
  leaves and recorded as `flow_commit`. Uploads (`draft` rules), reads and requests no rule names
  are not the scope's business; a profile edit's run (`linkedin.profile_*`) may send its photo's
  registration and upload and its own edit, never another profile field's edit, a post, a like or
  a follow. The route goes when the run lets go of the lane. A run whose
  route cannot be bound does not start. An agent's own browsing is not touched: only the run's
  page is judged (`_flow_owns`).
- **The approval binds the request, not the outcome**: a site that publishes something other than
  what its request says (server-side templates) is out of reach.

### 8.7 How the gate classifies a request

While a drive is on, every non-GET on a rule host is classified before it leaves:

| Request | Classified as |
|---|---|
| matches a commit rule (by host, path, method, operation) | that rule: gated |
| matches a commit or write rule but sent with another unsafe method (a CreatePost as a PUT) | that rule |
| a GraphQL endpoint's read catch-all (`strict_ops`): every operation a known read (READ_OPS, READ_DOC_IDS), named like a query (`...Query`, `...Query_x`) or a GraphQL `query` document | read: goes |
| the same, with a `mutation` keyword, an unknown or missing name, an unknown `doc_id` and no name, a batch with any non-read, a body it cannot read, or a known publish's name without its header (Medium) | `<site>.unknown`: gated as a possible publish |
| a write rule (like, follow, join, upload) | write: needs draft, goes |
| a read rule (the plumbing list) | read: goes |
| PUT, PATCH or DELETE no rule names | `<site>.unknown`: gated |
| POST no rule names | `<site>.unknown`: gated (`gate_unknown_posts`, the default); asks a person only when it carries what the agent typed and the act's own press sent it, else held without a card (8.13) |
| a telemetry or plumbing read rule (`untyped`, 8.13) whose body carries what the agent typed | `<site>.unknown`: gated |
| a POST that would be `<site>.unknown` only for the reasons above, inside the vault's login window (8.11) | goes, traced as `login` |
| any GET, HEAD or OPTIONS | goes at once |

The grant's ceiling (`judge`) and the listener still read a catch-all as a read, as before: the
fail-closed reading is the gate's, during a drive. Host and path are normalized the same way in the
route and the classifier: case, userinfo, a trailing dot, any port, IDNA and percent-encoded hosts
(the route also takes any host with a non-ASCII or `%` character, and the classifier decides), dot
segments and percent-encoded unreserved characters in the path.

### 8.8 "Ask me first" and keepalive visits

The gate authorizes a publish as a commit on its lane, so the routing table decides it the way it
decides any commit there (ROUTING.md, "Ask me first"). With the account's switch off (the shipped
default) a commit runs on its own: the publish leaves at once, traced (`commit_gate` with status
ok, and the `authorize` row every commit gets) and counted as one commit. With it on (the lane's
switch, or that of a connector surface posting through the lane) it waits for a person: the
`publish` card above, a one-off approval of exactly what it sends, or a standing approval bound to
that content. The switch was first held on clicks by refusing them (rule `ask_first`, "run a
routine or ask the person"), because a click could not then wait for an approval of what it sends;
the gate is that approval, so during a drive the guard no longer refuses for `ask_first`, before
the press or on the route. The refusal stays where no gate can hold the act (an act without a route),
and at the listener, where a publish that left outside a drive on such a lane is recorded and holds
the lane. A lane with no route dispatcher refuses the act instead (section 8.1).

A keepalive visit (docs/design/keepalive.md) drives the lane as the node under a read-only grant, and
never on a lane an agent drives. It binds the same route handler on the gate's pattern in its effects
mode (`acting(effects_route=True)`): a request the gate would hold is aborted before it leaves. That
is a write or commit rule the grant's ceiling refuses, and a PUT, PATCH, DELETE or POST that no rule
names (`_gate_rule`, 8.7). That mode never gates, so a visit never asks for an approval; reads go to
the listener as usual, and the route goes when the visit ends. A visit whose route cannot be bound
fails. A visit that ends an agent's drive binds the drive again before control returns
(`restore_drive`).

### 8.9 The gate log: the owner's own allowlist

`agentos lane gate-log <lane> [--since 24h]` (and `GET /api/lanes/{ws}/{name}/gate-log?since=`) lists
what the gate classified without a rule while agents drove the lane: the lane's `gate_classified`
rows grouped by shape (host, path, method and operation name; `agentos.node.gateshapes`), with the
field names every row had, how many field sets, how each was classified (`gated`, `read`, `let go`)
and why, the GraphQL catch-all that matched, first and last seen, and a count (rows are traced once
per shape a second, so it counts seconds with that shape). Never a value: the rows hold names only.

The owner promotes a shape from it (`--allow-read <shape>`, `--as-commit <name> --shape <shape>`,
`--remove <shape>`; `POST` with `{"allow_read"}`, `{"as_commit", "shape"}` or `{"remove"}`). A
promotion holds for exactly the host, path and method the shape was seen on, never on the site's
other endpoints, and on the lane it was promoted from (`lanes` in the entry; promoting it again
from another lane adds that lane); `--all-lanes` (`"all_lanes": true`) widens it to every lane of
the node. The gate log and the action log say where each promotion holds:

| Promoted as | What the gate does from then on |
|---|---|
| read, on a GraphQL endpoint (a strict_ops catch-all's) | its operation is a known read on that endpoint (`gql_verdict(extra=...)`) with the doc_ids the shape was seen with, and only with those (a shape is its host, path, method, operation name and doc_ids: the same name with another doc_id is another shape, still held); a batch is still a read only when every operation in it is one. A call with no operation name cannot be promoted (it cannot be told from a publish) |
| read, anywhere else, naming an operation | a read rule for exactly that host, path and method and that operation, checked whole like a GraphQL one (doc_ids included), after the built-in rules |
| read, anywhere else, naming no operation | a read rule for exactly that host, path and method, bound by the shape's field names (`EffectRule.fields`, `body_covered`): a body goes only when every field name it has is one of them (optional ones may be missing) and it names no operation (no operation header, no `operationName`, `doc_id`, `query` or other operation key). One with another field, a GraphQL document, an operation name or a body the guard cannot list whole is held as `<site>.unknown`. Refused on a host the rules know publishes on (any commit rule's host pattern matches it: with the built-in rules, every host of Reddit, Facebook, Instagram, LinkedIn and Medium), for a shape that mixes several bodies on one path (the gate log marks it `mixed`), and for one whose field names are not all of its body's (`partial`) |
| `--as-commit <name>` | a commit rule `<site>.<kind>[.<detail>]` (a kind of `post`, `comment`, `like`, `follow`, `join`; not a built-in id) for that host, path, method and operation, before the built-in rules: the gate holds it under that name, and its card says which rule (unverified) |

Field names are shown so the owner can judge a shape; they are not matched (optional fields come
and go). Promotions are the node's own, stored in `AGENTOS_HOME/gate-shapes.json` (mode 600, at
most 500), each with who promoted it and when, never in code; every change writes a `gate_shape`
row (who, which shape, as what) to the action log. A change takes a lock (`gate-shapes.json.lock`,
so the daemon and the CLI never lose each other's update), writes a unique 600 temp file, fsyncs
it and renames it into place, keeping the file it replaced as `gate-shapes.json.bak`; a file that
cannot be parsed (a torn or hand-broken write) is read from that copy instead. Every lane's guard reads the file when it starts
and again when it changes (its mtime and size, checked at most every 2 s, `LaneGuard.rules`); a
file it cannot read is no promotion (fail closed). Every entry is validated strictly
(`gateshapes.invalid`: each field present with its type, the id its own shape's, the site the one
the rules give its host, a commit rule's name one the CLI would accept); one that is not valid is
skipped with the reason logged. The guard compiles the promotions before it records them as seen,
and when reading or compiling fails it uses the built-in rules alone (no promotion) and tries
again on its next read. Only the owner promotes or reads the log: the CLI
on the node, the node token, or the owner's own panel session; a member's session or any grant, an
admin grant included, gets `denied` (403, traced). A wrong read lets a publish go without a person;
a wrong commit rule only asks once too often, so promote a read only for what is known to publish
nothing.

### 8.10 Who a publish acts as (AUD-01 on the gate)

Built 2026-11-05 (owner decision, option 2: automation, standing approvals and schedules run on
logins that manage Pages). A lane is a login; a surface is who you post as
(`agentos.core.models.Surface`). Connector tools target surfaces and check the identity on the
page (`assert_identity`); an agent that drives the raw lane went through the gate with nothing
checking whether the post goes out as the profile, as the Page it meant, or as another Page the
login manages. `agentos.node.acting` reads it from the normalized request and resolves it.

**Read, per site** (`ACTING`; the first acting field present decides, a later one that names
another identity makes it unrecognized; the fallback `av` decides when no other is present, and next
to one it may name the login, while anything else it names disagrees):

| Site | Fields | Verified on a real account | When nothing is named |
|---|---|---|---|
| Facebook (`facebook.post`, `.comment`, any `facebook.unknown`) | `variables.input.actor_id`, `variables.actor_id`, `variables.input.page_id`, `variables.pageID`, `variables.input.target_page_id`; the form's `av` as a fallback | `actor_id` and `av` (the canary's personal post and comment: both the profile's id). No Page post has been recorded, so which of these a Page post sends is not known | unrecognized (a login can post as a Page) |
| LinkedIn | by name anywhere: `author`, `authorUrn`, `actor`, `actorUrn`, `nonMemberActorUrn`, `organizationActor`, `organizationUrn`, `organizationId`, `organization`, `companyUrn`, `companyId`, `company`, `postAs`, `postingAs`, `actingEntityUrn`; a member urn is the member; an organization urn (`urn:li:organization:`, `fsd_company`, `company`) anywhere else in the body is unrecognized (the gate cannot tell whether it posts as it) | none (the canary recorded operation names, not fields) | the member's own (the recorded `CreatePost` names no author) |
| Instagram | `variables.input.actor_id`, `variables.actor_id`, `variables.data.actor_id`, `business_account_id` | none | the login itself, unchanged |
| Medium | `publicationId`, `publication_id`: always unrecognized (no surface kind names a publication yet) | none | the login itself, unchanged |
| Reddit, the rest | nothing is read | | the login itself, unchanged |

**Resolve** (`acting.resolve`): an id equal to the lane's pinned user id, or the signed-in
account's when none is pinned, is the lane's own (`assumed` when it was inferred: nothing named on
LinkedIn, only `av`, a member urn that cannot be compared, since LinkedIn's cookies carry no member
id; every surface's switch on the lane then still holds it); an `account` surface's id through the
lane, when that surface's id is not the lane's, is that surface; an
id that a surface through this lane names (its `acts_as.id`, of the site's asset kind: `fb_page`,
`li_company`, `ig_business`) is that surface; anything else is unrecognized, with the reason (no
surface names it; it is the Page of `biz-b/facebook-page`, which posts through another lane; the
fields disagree; the body names none; the value is not shaped like an id, which is then not
repeated anywhere in the node's words). The record keeps it (`record.acts_as`: status, raw id,
field, whether that field is verified, the surface, its label, the lane it goes through), so the
digest binds it.

**Then** (ROUTING.md, "Who it posts as"): a surface's publish is authorized on the surface (the
grant must cover its workspace; its Ask me first switch and its lane's; the lane's pace and limits,
since a commit counts on the surface and on the lane it posts through; the card
and summary name it, and the `commit_gate` row in the action log carries `acts_as`); the lane's own
is as before (and only the lane's switch holds it); an unknown identity always asks, is never
covered by a standing approval, and is refused (`identity_unrecognized`) for a grant or principal
of kind `flow`. The gate refuses a surface the grant does not cover before it asks
(`out_of_scope`, nothing sent).

**Flows running on their own.** A flow run is authorized with its approval (flow version and
parameters), and its publishes went to the listener only. On a lane whose site is in `ACTING`
(its pinned account's site, its name, its site list or a surface through it), or that none of them
describes, the flow driver now binds the gate's
route for the run (`LaneGuard.flow_identity`, `commitgate.flow_identity_check`): a commit rule's
request its page sends is read and resolved, and one that acts as an identity agentos does not
know, or whose body the gate cannot read, is aborted (block rule `identity_unrecognized`), since
nobody is there to ask. The lane's own account and its surfaces go as before. The route has one
owner: a drive that ends under a flow hands it to the flow, a drive that begins takes it over, and
the last flow out removes it. The cost is the route's (8.4): no HTTP cache while such a
flow runs. Whether the run's grant covers the surface a flow posts as, and that surface's Ask me
first switch, are not checked here (the run was authorized on its lane, and reads its routine's
and its lane's switches when it starts).

**The gate log** (8.9) lists the identities agentos did not know (`gate_identity` rows in the
lane's egress trace: site, raw id, field, rule, whether a flow sent it, why, first and last seen;
never a value of the post), with the hint to add a surface for a Page the login manages.

**Measured on a real browser** (`acting` in `scripts/spikes/commit_gate.py`, the
`agentos-stealth-node` image, 2026-11-05: the real LaneGuard, route and ActGate, the loopback
composer sending `actor_id`, read as a site whose login can post as a Page):

| Click | Authorized on | Reached the server |
|---|---|---|
| as the lane's pinned account | the lane (`self`) | the post |
| as the Page a surface through the lane names | `biz-a/loop-page` (`surface`) | the post |
| as a Page no surface names | the lane (`unrecognized`): approval_pending | nothing |
| as the Page, under a grant without its workspace | refused before authorize (`out_of_scope`) | nothing |
| a flow on its own (`flow_identity`, one route while it ran, none after): a Page no surface names | aborted (`identity_unrecognized`) | nothing |
| the same flow, as the surface's Page | goes | the post |

The `gate` scenarios of 8.5 gave the same results as before.

**What it does not cover.** The field names that name a Page, a company page or a business account
are not verified: the first real Page and company posts through the gate will tell. A field this
module does not list that a site uses to name a Page would make a Page post read as the login's
own on LinkedIn and Instagram (where nothing named means the login); on Facebook it reads as
unrecognized unless `av` names the login. Such a post is `assumed` on LinkedIn and through `av`, so
a surface's Ask me first switch still holds it, but it is authorized on the lane. A connector run keeps its own identity check on the page.

### 8.11 A login is not a publish: the vault's login window

A password in the vault is the owner's permission to use it (docs/PRINCIPLES.md, 3), so a login
never asks. The policy side is one row (ROUTING.md, "Logins"). This is the other side: a login's POST
is on a rule host and no rule reads it, so during a drive the gate classifies it as `<site>.unknown`.
Under a commit grant that is a publish card for a login; under a draft grant it is refused outright
(`rule: commit`), which is what the benchmark hit as `lanebench.unknown`.

**The window.** While the vault fills a login field (`login`, `vault_fill`), the typist calls
`LaneGuard.open_login(domain)` with the stored entry's registrable domain. For `LOGIN_WINDOW_S` (30
seconds, renewed by each fill, so a username step, a password step and a code step each carry the next)
a POST to that domain that the gate would hold only because it cannot read it goes on. That covers a
POST no rule names, a `strict_ops` read that fell to unknown, and a promoted read whose body it does not
cover. Each is traced as `gate_classified` with the decision `login`. Nothing reads the password in the
body: Facebook and Instagram encrypt it in the browser.

It opens before the first key, because a code field can submit itself on its last digit, and a fill
that fails closes it, an earlier fill's window included.

| Inside a window | What happens |
|---|---|
| a POST that would be `<site>.unknown`, to the entry's domain or a host under it | goes, `login` |
| a request a commit or write rule names | judged as always: gated, or refused by the ceiling |
| a batch with any operation such a rule names (`classify` reads one; `_in_login` reads them all) | gated |
| a body that cannot be read as operations, on a `strict_ops` endpoint | gated: it could carry anything |
| PUT, PATCH, DELETE | gated |
| another domain, another lane | gated: the window is one domain on one lane's guard |
| a host outside the lane's site list or the grant's egress list | refused, as always (`judge`) |

**Who can open one.** The vault, after the entry passed `decide` for this lane, this page and this
field, and `ctx.authorize` passed: `open_login` has one caller, the typist, which has one caller,
`agentos.vault.login`. The domain is the stored entry's, never an argument, so a page, a tool argument
or a model cannot reach it. `agentos.vault` does not import `agentos.node`: the call goes through the
`Typist` interface, and an adapter without it opens no window.

**How it ends.** On expiry, and with the drive (`end_drive`): a person taking over, a pause or a hold,
the end of a claim, the lane closing, and a keepalive visit, which sees no window. It does not need a
drive to open: on a lane no act has driven yet, the fill comes first and the click starts the drive.

**What it costs.** For 30 seconds after a fill, what the page sends to that domain and no rule names
goes unheld, whoever on the lane set it off. A model that fills a real login field and then clicks a
form no rule knows gets that form through. It cannot use this to reach a named publish, and the
fill needs a stored entry for that domain on that lane. `gate_classified` rows with the decision `login` show
everything that went this way.

### 8.12 When a site changes its web client: rule health and drift

Built 2026-11-06. The rules in `EFFECT_RULES` are hand-written from what each site's web client sent on
2026-11-04. Before that day, every platform's rules were wrong. Sites ship
new frontend code all the time. What happens when one renames a publish, and what the owner has:

**What stays safe on its own.** While an agent drives, the gate classifies fail closed (8.7): a renamed
mutation on a GraphQL endpoint is `<site>.unknown`, and so is a new POST path on a rule host, so the
publish waits for a person (as an unknown shape, its card unnamed) and is traced `gate_classified`.
What it does not catch by itself: a new publish named like a query (`...Query`) or under a name in
READ_OPS (taken for a read), a new host the rules do not name (not routed), and anything sent while no
agent drives (below).

**Rule health** (`agentos.node.rulehealth`). Read from the lanes' egress traces, names and shapes only:

- When each commit and write rule last matched, how often and on which lanes. Commit rules leave
  `observed_commit` and `blocked` rows; write rules now leave a `rule_hit` row (one per rule a second).
  The window is what the traces keep (4 MB, rolled once per lane), and the report says from when.
- The shapes no rule names, per site: `gate_classified` rows the gate held or let go,
  `unmatched_publish` (an agent's press to a host no rule names), `unruled_request` (below) and
  `observed_request` rows no commit or write rule matched (an operation a GraphQL catch-all took
  whose name is not a known read). Each with counts, how it was decided, and `write_like` and `kind`:
  a heuristic over the words of its operation name (or path: `create-comment`), for the owner's eyes
  only. It never changes what the gate does.
- Flags: `stale_rule`, a verified or owner promoted commit or write rule that has not matched since a
  shape of its own kind appeared on its site (`CreateCommentV2` after `reddit.comment` went quiet), or
  that has been quiet for 14 days (`--stale-days`) while its site sends unknown shapes that look like
  writes. Rules flagged by the same shapes are one flag. Unverified rules (older clients) are never
  flagged. `new_unknowns`, three or more new write-like shapes on one site within 24 hours.
  `no_rules`, below.

Where the owner sees it: `agentos lane rules [lane] [--stale-days N] [--all] [--json]`, the doctor's
`effect_rules` check (a warning per flag, never a failure), and `GET /api/rules/health` (the owner
only). A stale flag ends with the command that fixes it.

**The fix is data, not code.** The owner names the new shape a commit rule: `agentos lane gate-log
<lane> --as-commit comment.v2 --shape <id>` (8.9). It is stored on the node (`gate-shapes.json`),
written to the action log, and every running guard reads it within 2 s: no deploy, no restart.
`--observed` lists the shapes observe mode saw too (`observed_request` rows no commit or write rule
matched), so a publish the gate takes for a read, or one the owner made by hand while observing the
lane (the canary's way to find it), can be named; such a shape is promoted to a commit rule only, never
to a read. A read promotion cannot downgrade a built-in commit or write rule: promoted reads come after
the built-in rules, and a promoted operation name is a known read only on the catch-all it was seen
on, which that endpoint's commit and write rules come before (`test_gate_log.py` pins it with a stored
read for `CreatePost`). Removing a built-in rule, or turning one into a read, still takes a code change.

**Outside a drive** (checked 2026-11-06, in code):

| Who sends the publish | Rule host, a commit rule names it | Rule host, no rule names it | A host no rule names |
|---|---|---|---|
| an agent's act or its drive | gated (8.1) | gated as `<site>.unknown` | goes; `unmatched_publish` for a press, else `unruled_request` |
| a flow run on its own | goes under the flow's own approval, `observed_commit`; who it acts as is checked on ACTING sites (8.10) | goes; traced only in observe mode | goes; `unruled_request` |
| a page timer, no agent driving | goes, `observed_commit`, charged nowhere (when the last agent's grant does not allow commit, the listener records it and holds the lane, after it left) | goes; traced only in observe mode | goes; `unruled_request` |
| a person in control | goes, `observed_commit`, on the lane's pace ledger | goes; traced only in observe mode | goes, not traced |

`unruled_request` is new: a non-GET to a plain entry of the lane's own site list (not a `frame:` entry,
not a sign-in or captcha host) that no effect rule names, sent while no person has control, is traced
by shape, one row per shape a minute. Nothing is blocked: many sites read through POST. A lane with
no site list traces nothing there (only rule hosts are its own). The `no_rules` flag names every lane
whose site list, or the site it is expected to be signed into (its account, or its name:
`biz-a/x`, `biz-b/twitter`), has hosts no effect rule names: x.com, threads.net, tiktok.com,
youtube.com, bsky.app have none today, so publishes there are never gated. `agentos lane sites` says so
when such a host is on the list.

**Not done.** No Needs you card for a flag: a question needs a grant, and drift has none; the doctor
and `agentos lane rules` are where the owner looks. No effect rules for a new site from the owner: a
commit rule on a host no rule names would put that host on the gate's route, and every unknown POST
there would then be held while an agent drives (reads through POST included), so it stays a code
change with its plumbing reads. Rule hits are not kept past the trace's two files.

### 8.13 The gate on real sites (the full test of 2026-11-10)

The full test of 2026-11-10 (findings 10, 13, 16 to 21, 27) ran the gate on the real
node's LinkedIn, Facebook, Instagram and Reddit lanes. Since unknown POSTs on a rule host are gated
(`gate_unknown_posts`), the sites' own background traffic was held while an agent acted: every
LinkedIn click returned `publish_unbound ... nothing was sent`, also when its like had left and could
be read back; Facebook and Instagram background calls became approvals nobody could judge; Reddit's
as-you-type validation and token refresh never arrived. What changed:

**Read rules for the recorded shapes** (`rules.py`, `READ_OPS`; each from
the real node's gate log, names and shapes only, `verified` "2026-11-10 ... (shape only)"):

| Site | What now goes as a read | Bound by |
|---|---|---|
| LinkedIn | `/flagship-web/rsc-action/actions/pagination` and `app-config` | path; untyped |
| LinkedIn | `/voyager/api/messaging/dash/presenceStatuses` | its one field (`ids`); untyped |
| LinkedIn | the server render of the feed and a profile (`/flagship-web/feed/`, `/flagship-web/in/<name>/`) and `actions/navigation` | every field name recorded; untyped |
| LinkedIn | the event beacons on a path that changes with each build (`/to11y...`, `/tapi...`, `/E1J9...`) | one segment of 12 to 24 `[A-Za-z0-9_-]` with both cases, and either the tracking envelope (every field name recorded, `clientContext.eventHeaderContext` among them) or a `text/plain` body; untyped |
| LinkedIn | server requests `update.incrementCounterMetric`, `impl.homenav.requests.getThirdPartyTrackingPixels`, `requests.sharing.LaunchShareboxTracking`, `requests.feed.actorPreviewCard`, `comments.fetchFeedUpdateActionPrompt`, `requests.comments.deferredCommentBox` | READ_OPS (every operation of a batch must be one) |
| Reddit | `ValidateCreatePostInput` (it validates, it creates nothing), `TrophyCategories`, `DeleteAchievementNotifications` (the account's own notices), `GetIsCommentGuidanceAvailableFromPostId` | READ_OPS |
| Reddit | `POST /svc/shreddit/token` | its one field (`csrf_token`); untyped |
| Meta | Facebook's `/ajax/relay-ef/`, `/static_resources/webworker/rsrc/`, `/ajax/bnzai`; the login sync `www.facebook.com/instagram/login_sync/update/` and Instagram's `/sync/instagram/` and `/api/v1/web/fxcal/ig_sso_users/` | path; untyped |
| Meta | the `...Query` operations | by name, as before (they were reads already) |

`untyped` is new (`EffectRule.untyped`): such a read lets a body go only while it carries no text an
agent typed on the lane (`LaneGuard.carries_typed`, below); one that does is gated as `<site>.unknown`.
Left gated on purpose: `register.cooloff.activity`, `restoreTagPills`, Facebook's
`/shared/user_preferences/` and the `...Mutation` operations no rule names (they write something,
even if not a post), and every shape that only looks like a recorded one (another field, an
operation name, a word path, one case only).

**What the agent typed** is a stream check (`egress.TypedStream`, review of the fix wave, finding 3).
`browser_act` type and fill tell the lane's guard their text (`note_typed`), and press tells it each
printable key ("a", "Shift+A", "Space"; not Enter, Tab or a shortcut, `note_key`). The guard keeps,
for as long as the lane's browser lives, the running text of all of it in order, normalised and capped
at the newest 16,384 characters, and the last 20 whole type and fill pieces. In memory only: never
traced or logged, and its repr names only its size; a vault fill is never there.

Both sides are normalised the same way (`normalise_text`): JSON and URL escapes and HTML entities
decoded, a few layers deep; tags stripped; case folded; everything that is not a letter or a digit
dropped. A body carries typed text when it shares a run of at least 10 normalised characters with the
stream, so text typed in chunks or key by key, split by markup, escaped, case-changed or spaced
differently is found however long ago it was typed. Base64-looking runs of the body (standard and
URL-safe, also once form-decoded) are decoded and checked the same way. A short piece (fewer than 10
normalised characters) counts as a whole JSON string or form value, and for 30 minutes after it was
typed also inside a value once it has 4 characters (the check before the stream). A long piece found
whole is named whole on the card.

The false-positive cost is a quiet one: a background read that shares a 10-character run with the
stream is held as unknown, without a card, and listed in `egress_blocked` and the gate log. The site's
recorded background traffic carries none of it (tests/unit/test_typed_stream.py checks both ways).

What it still cannot see: text that never went through `browser_act` type, fill or press (a paste, an
upload's contents, a file or image, a flow's own typing, text a person typed in the live view); a
draft the site restored by itself and sends again (it was typed in another browser life, or by a
person); text the site rewrote past these decodings (encrypted, compressed, hashed, translated,
shortened below 10 letters in a row, or put in an encoding the check does not know, such as protobuf
varints with the text split, or UTF-16); and the part of a body past the first 2 MB. A held unknown
request whose body showed none of the typed text says so: "no text the agent typed was found in it",
which is all that is known.

**A card only for what a person can judge** (`ActGate.hold`). A request the gate holds as
`<site>.unknown` gets an approval card only when it carries what the agent typed and this act's own
click or Enter sent it (`submit_like`, while the act runs). Anything else no rule reads (telemetry, a
sync, a request sent while typing, during a navigation or between acts) is held and not sent, and
nobody is asked (`held_unknown`): the act's `egress_blocked` lists it with rule `unrecognised` and
what is known ("agentos does not recognise this request (POST host/path) ... It was held and not
sent, and nobody was asked"), the gate log and `agentos lane rules` show its shape as before
(`gate_classified`, decision `gated`), and the owner can promote it there. A body the node cannot
read is held the same way (no more `publish_unbound` for a telemetry beacon). The card of an unknown
request that remains says in plain words what is known: `headline` ("A request agentos does not
recognise, to Facebook: POST /path"), `explain` (it cannot tell whether it posts; whether it carries
the agent's words and where; that denying is safe), `deny_safe`, and `shape` (method, path, field
names); the summary reads "Send a request agentos does not recognise to ... It carries text the agent
typed: ...".

**A true result.** Such a hold never fails the act: `browser_act` returns ok with what its read-back
saw, and `egress_blocked` lists what was held. An act that fails for its own reason (a ref gone, a
timeout) fails with that reason, never as `egress_refused` because of a held background request.
`approval_pending`, `publish_unbound` and the other refusals come only from a request the gate can
describe as a publish: a commit rule's (always, whatever the read-back says: a click whose button
changed while its post waits must not read "ok"), or an unknown one that carries the agent's words on
its press. When a like, a follow or another write of the act left before a publish was held, the
message says so ("This click also sent a like (linkedin.like) ... A later one did not leave: ... that
request was not sent"), never "nothing was sent".

What a real-site run still has to confirm: that the recorded field sets hold across LinkedIn's builds
(a beacon with one more field is held, without a card, and shows in the gate log), that LinkedIn's
text/plain beacons and Meta's sync blobs never carry typed text in clear (if one does, it is held
while the agent's text is remembered), and that nothing LinkedIn needs for a post is left among the
held shapes.

**LinkedIn's bot-defence frame** (`li.protechts.net`, HUMAN Security) is a `frame:` entry of the
suggested LinkedIn list: blocking it makes the account look like a bot. A lane built before this
needs `agentos lane sites <lane> suggest linkedin` again. No other suggested list had a frame refused
in the test.

**Ask me first on a site without rules** (finding 10). The gate never sees a host no rule names, so
the switch held nothing there. Now `browser_act` reads the form an act that can submit one would
submit (the form approval path, `form_before_submit`). Which acts can is the commit gate's own
definition (`_press_mode`, which `submit_like` reads too): a click; a press of Enter, NumpadEnter or
Space, also with a modifier (Shift+Enter, Control+Enter); a type or fill whose text holds a newline;
and a select. For a key the form of the field or button it is on is read, or the form around it
(review of the fix wave, finding 2). When it posts (method POST) to a host no effect rule names
and the lane asks first, the act is authorized as a commit, so it waits for a person with the form
card (where it goes, every field it sends) before anything is clicked; after approval the same call
runs once (`check_form_unchanged` binds the form). A draft-only grant is refused there, as for any
publish. Not held: a GET form; a host the vault's login window is open for (8.11); a request a
script sends on its own (fetch or XHR to a host no rule names, a form a script submits that the click
does not resolve to), which goes as before and is traced (`unmatched_publish`, `unruled_request`).
The "after reading untrusted text" rule does not hold a form post there either: it reads commits, and
a form post on a host without rules is a draft unless Ask me first is on.

**`account` on LinkedIn results** (finding 19) is not added: no LinkedIn cookie carries the member
id (`li_at` is opaque, sites.py), and the id needs a request (`/voyager/api/me`) or the page's own
markup. A request would be traffic the site sees from the node, and an id read from the page can be
written by what the page shows (a post's text), so it could not prove which account is signed in. The
card says the account is not known (`account_status: unknown`) and the lane's pin is not checked on
LinkedIn.

