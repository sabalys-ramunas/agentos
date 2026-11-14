# Signed agent identity for agentos (Web Bot Auth)

Status: research and proposal, 2026-11-04. The Phase 0 spike ran the same day (section 12), and
Phase 1, the signing core, was built after it (section 13). Nothing is published or registered. This document answers
the "signed agent identity" big bet in the backlog. It relies on
[PRINCIPLES.md](../PRINCIPLES.md) (especially 1, 2, 3, 4, 6 and 7) and the trust model in
[ARCHITECTURE.md](../ARCHITECTURE.md). Sources are listed in section 11. Claims are marked
by how they were checked: "read" means the primary text was fetched and read this session;
"reported" means it comes from a search summary or a vendor blog and was not opened.

## 1. Recommendation

**Prototype now, behind a flag. Do not build directory publication, registration or the
panel yet.**

Phase 0 is a short spike on one throwaway lane with no owner accounts. It answers two
questions: can a Camoufox lane sign every request to an opted-in host through Playwright
without moving its pinned fingerprint, and which `Signature-Agent` header form do the large
verifiers accept? Phase 1 (the signing core, off by default) starts only if the spike
passes. Anything that publishes or registers an identity waits for the four gates below.

Why, in four points:

1. **The standard is not finished, and its own timetable has slipped.** Web Bot Auth is an
   active IETF working group. Its document is `draft-ietf-webbotauth-httpsig-protocol-01`
   (Standards Track, last updated 2026-10-29, still "I-D Exists"). The charter milestones
   were standards-track specs to the IESG by 2026-04-30 and a best-current-practice
   document by 2026-08-31. Datatracker shows no IESG submission (read). The directory and
   architecture drafts were folded into the protocol draft. The spec and the largest
   verifier also disagree on one header form (section 2.4).
2. **The payoff is narrower than the bet.** The adopters we could confirm are bot-management
   and CDN products and their customers, plus Google and OpenAI for their own agents
   (section 3). We found no primary document saying that a consumer platform the owner's
   lanes use (social networks, marketplaces, email, banks) accepts signatures. No vendor page
   we read says signed traffic skips bot scoring. Amazon's own page says "Not all websites
   will recognize or allow signed agent traffic" and that domain owners "may block, monitor,
   or rate-limit agent traffic regardless of cryptographic signatures" (read).
3. **A signature proves a key, not behaviour.** The protocol says a valid signature "says
   nothing about who operates the Agent, whether the Agent is benign, or whether..." (draft
   section 4, read). Accountability therefore comes from something the site already trusts
   (a registration, a contract, a vendor's listing), not from the signature. This is the
   widest gap between the bet and what the standard gives.
4. **The position is partly taken already, and the test is cheap.** Cloudflare publishes
   reference libraries in TypeScript and Rust with a Chrome extension example (read).
   Amazon's managed browser signs each request, in preview (read). OpenAI publishes a key
   directory for its cloud browser (read). What is still open is a self-hosted platform that
   gives each lane its own key, ties signing to grants and the audit log, and keeps keys off
   the controller. That is a real niche. The protocol is not one. The spike needs only
   Playwright, which the engine already uses, and Ed25519, which `cryptography` already
   provides.

**Correction to the bet.** Signing is an identity layer, not a fix for fingerprinting.
Fingerprint scoring still runs on signed traffic. Signing gives sites that opt in a
truthful, machine-checkable statement that this is an automated agent acting for a named
workspace. That is principle 7 made stronger. It does nothing for the other sites.

**Gates before Phase 2 (publication, panel, grants):**

- (a) The Phase 0 spike passes.
- (b) At least one major verifier accepts the `Signature-Agent` form we send.
- (c) The owner picks the operator origin and the first sites.
- (d) The standard reaches IESG approval, or the sites the owner cares about say in writing
  that they accept the draft.

## 2. The standard

### 2.1 Documents and dates (as of 2026-11-04)

| Item | State | Source |
| --- | --- | --- |
| IETF working group "Web Bot Auth" (`webbotauth`) | Active. Charter 01 approved. Chairs David Schinazi and Rifaat Shekh-Yusef, area director Mike Bishop (read) | [WG about page](https://datatracker.ietf.org/group/webbotauth/about/) |
| `draft-ietf-webbotauth-httpsig-protocol-01`, "HTTP Message Signatures for automated traffic" | WG document, Standards Track, "I-D Exists", last updated 2026-10-29, expires 2027-05-02. Authors Thibault Meunier (Cloudflare) and Sandor Major (Google) (read) | [Datatracker](https://datatracker.ietf.org/doc/draft-ietf-webbotauth-httpsig-protocol/) |
| Charter milestones | Standards-track specs to IESG by 2026-04-30 (two items). Best-current-practice operational spec to IESG by 2026-08-31. No submission shown (read) | [WG about page](https://datatracker.ietf.org/group/webbotauth/about/) |
| Predecessors | `draft-meunier-web-bot-auth-architecture-05` and `draft-meunier-http-message-signatures-directory` are replaced by the protocol draft. The directory format is now section 5.5 and the directory signature is Appendix B (read) | [directory draft](https://datatracker.ietf.org/doc/draft-meunier-webbotauth-httpsig-directory/), [architecture -05](https://www.ietf.org/archive/id/draft-meunier-web-bot-auth-architecture-05.html) |
| Related individual drafts | Registry (`webbotauth-registry-04`, 2026-10-27), hosted directories (`webbotauth-hosted-directories-00`, 2026-07-19), signed-agent metadata (`webbotauth-signed-agent-metadata-00`, 2026-11-02), anonymous credentials (`anonymous-webbotauth-01`, 2026-07-19). Several more are listed, none adopted (read) | Datatracker related list |
| RFC 9421, HTTP Message Signatures | Published RFC. Defines `created`, `expires`, `nonce`, `keyid`, `alg` and derived components (read) | [RFC 9421](https://www.rfc-editor.org/rfc/rfc9421) |
| RFC 9530, Digest Fields | Defines `Content-Digest`, which the protocol uses only when a body must be covered (read) | cited by the protocol draft |
| RFC 7638 and RFC 8037 | JWK thumbprint, and its OKP (Ed25519) form. The protocol's `keyid` is a base64url SHA-256 thumbprint (read in the draft) | cited by the protocol draft |
| Cloudflare reference code | `cloudflare/web-bot-auth`: TypeScript packages (`http-message-sig`, `web-bot-auth`, `jsonwebkey-thumbprint`), Rust crates, a Chrome MV3 browser extension example, a Workers signing example, and a live test deployment (read) | [GitHub README](https://github.com/cloudflareresearch/web-bot-auth) |

There is no Python reference implementation in that list. Cloudflare's own docs point at its
TypeScript and Rust packages (read).

### 2.2 What a signed request must carry (protocol draft -01, read)

- **Signature-Input** parameters: `created` and `expires` (both MUST); `keyid` (MUST, a
  base64url JWK SHA-256 thumbprint, RFC 7638, or the OKP form for Ed25519); `tag`
  (MUST be `web-bot-auth`). `expires` is "RECOMMENDED ... no more than 24 hours after
  created."
- **Covered components:** at least `@authority` or `@target-uri` (MUST). `@method`, `@path`,
  `@target-uri` and `@query-param` are SHOULD. "No component covers the body." A body is
  covered only if the agent sends and covers `Content-Digest`.
- **Signature-Agent:** the URL where the key directory lives. It is a Dictionary Structured
  Header with https member values. "Signers MUST send the dictionary form." Verifiers MAY
  accept the earlier bare-string form (Appendix E.1.2 has a legacy test vector).
- **Replay:** the draft defines no additional nonce requirements (section 5.2.3), and
  appendix C.6 leaves replay checks to verifier policy: a verifier that does not keep state
  "should not treat signature validation as evidence that replay was detected or prevented".
  A nonce is cheap and we would send one, but no verifier is required to check it.
- **Static signatures:** "Deployments MUST NOT treat a precomputed Web Bot Auth signature as
  a long-lived access credential" (section 6.9). A signature is a bearer token until it
  expires, so it must be produced at send time.
- **Origin-requested signing:** an origin can ask for a signature with `Accept-Signature`
  (section 5.3). It can refuse unsigned requests with 403 and `Accept-Signature` in the
  response. "A valid signature does not guarantee that the request will be accepted."

### 2.3 Directory, rotation, revocation (read)

- The key directory is a JWKS at `/.well-known/http-message-signatures-directory` on the
  `Signature-Agent` origin. The fetch must return 200, and verifiers "MUST NOT automatically
  follow HTTP redirects" (section 5.5).
- Directory responses should be signed. Each key SHOULD sign once, covering `@authority`
  (with the `req` flag), `content-digest`, `created`, `expires`, `keyid`, and
  `tag="http-message-signatures-directory"` (Appendix B.1). The signature proves the key
  holder assembled the set, and it stops a copied set from being re-served under another
  authority. A verifier MAY still use a key from an unsigned directory fetched directly.
- Rotation: "publishes the new key alongside the old one, then drops the old one"
  (section 4.2). Operators "SHOULD rotate keys by publishing the old and the new key
  together" (section 5.5.2).
- Revocation: "It is not a revocation mechanism, and this document does not define any"
  (section 5.5.2). Removing a key stops new verification only after verifiers' cached copies
  expire. Appendix C.8 says the lever for faster revocation is "publishing more often, not
  signing shorter". In practice, revoking means stopping signing, removing the key, and
  accepting that signatures already sent stay valid until their `expires`.
- Key reuse: implementations SHOULD NOT reuse a signing key "for different purposes". For
  crawlers versus user-directed agent platforms, distinct `Signature-Agent` URLs, keys and
  directories are recommended (section 6.5). This matters for the design (section 5.1).
- Identity: "the identifier is the URL and not the key" (section 4.2). The `keyid` is derived
  from key material and cannot carry continuity across a rotation. The operator origin is
  what sites see.

### 2.4 Conflicts to settle in Phase 0

- **`Signature-Agent` form.** The protocol draft says signers MUST send the dictionary form
  (`sig1="https://..."`). Cloudflare's reference page, updated 2026-10-08, says verification
  fails for the dictionary form and expects a quoted bare URL (read). A third-party report
  says OpenAI's production traffic uses the bare form, which we did not confirm. This is a
  real interop conflict, not a wording issue. We cannot send both forms in one request.
- **Directory signing versus static hosting.** A directory signature needs the private key,
  and the node cannot serve HTTP (section 5.3). The publisher must sign, then upload to a host
  that can set headers.
- **Label names.** One source says AWS WAF labels are `verified`, `invalid` and `unknown_bot`.
  Another says `failed`. Reported, not checked against AWS's own page.

## 3. Adopters and registration

### 3.1 Who signs and who checks

| Party | What is documented | Confidence |
| --- | --- | --- |
| Cloudflare | Verified bots now include signed agents, as of 2026-07-01 with a metadata field "Direct versus Intermediary access". Verification is at the edge. Operators apply with a Bot Submission Form, verification method "Request Signature" (read) | High |
| Amazon Bedrock AgentCore browser | Signs every request in a managed browser, in preview, disabled by default. Vendors it works with: Cloudflare, HUMAN, Akamai, DataDome, F5. The mechanism is not stated (read) | High for the description, low for the mechanism |
| AWS WAF Bot Control | Web Bot Auth verification on CloudFront from 2025-11-21. Needs Bot Control 4.0 or later. Verified bots allowed by default in the AI category (reported) | Medium |
| Akamai | Bot and abuse products verify Web Bot Auth. Site operators choose allow, challenge, delay or block for verified traffic (reported, from an Akamai blog from November 2025) | Medium |
| HUMAN, F5, DataDome | HUMAN and F5 are named as vendors in the AgentCore docs (read). DataDome's changelog says verification is live for all customers, while its docs describe a form path (reported) | Low to medium |
| Fastly | One blog post says early support in April 2026 (reported) | Low |
| Google | `Google-Agent` signs a subset of its requests from `agent.bot.goog`. The docs call it "experimental" and say "Not all Google user agents are using Web Bot Auth". User-triggered fetchers "generally ignore robots.txt rules". Google recommends keeping IP, reverse DNS and user-agent checks as fallbacks (read) | High |
| OpenAI | A key directory exists at `chatgpt.com/.well-known/...`, with `signature_agent` set to `https://chatgpt.com` (read). OpenAI's help article returned 403 to us, so the signing sentence is reported only | Medium |
| Fingerprint (verifier and signer guide) | Operators register with Fingerprint in "1-2 weeks". Registered bots are treated as authorised and "will not flag it as malicious". Its guide says every request, including fetches and static files, must be signed (read) | Medium |

One vendor report claims that only about 2% of agent sessions carry a verified signature.
We could not check it and do not use it here.

### 3.2 How registration works

- **Protocol level.** Nothing vouches for an operator. The architecture draft's discovery
  options are an out-of-band list, a public list, or the header itself (read). The registry
  draft says that "A valid card shows that whoever controls the client_id URL published it",
  and that registry values "MUST NOT" be treated as verified behaviour (read). The hosted
  directories draft says a host "MUST NOT generate, receive, store, or otherwise possess
  tenant private keys" and that a verifier "extending trust to a hosted directory is extending trust to the host's operational practices"
  (read).
- **Cloudflare.** Verified or signed-agent status comes from a Cloudflare application, tracked
  in BotBase as Waiting For Review, Accepted, Rejected or Cancelled. Eligibility includes
  honest identification, non-abusive behaviour, respecting robots.txt, and reasonable request
  rates. Signed agents are "generally directed by an end user". Repeated violations can
  "revoke status for all bots of an organization" (read). Whether an individual can apply,
  and how long review takes, is not stated.
- **Fingerprint.** Registration takes "1-2 weeks" and is required to be treated as an
  authorised bot (read).
- **Anonymous credentials draft.** A site learns only that a bot is "part of some set of
  authorized bots", and abuse controls that need the abuser's identity are "incompatible with
  anonymous authentication" (read). Its anchors may require "a registered corporation, holds
  a domain name or an IP address block" (read).

**What this means for a self-hoster.** A node can always make a valid signature from a domain
it controls. That proves control of the key and the domain, nothing more. Sites decide what an
unlisted valid signature means. AWS's labels include `unknown_bot` (reported), which suggests
an unlisted signer is classed as unknown, not trusted. Being trusted by a site means applying
for a programme or a site-specific allowlist. No programme we read documents individuals as
eligible.

## 4. Signing a browser lane

### 4.1 The options

The lane's browser makes the requests, so signing has to happen where those requests are
built or sent. The options, against the engine as it is today (`node/camoufox/` launches
`AsyncCamoufox` through Playwright, and `env/netproxy.py` is a CONNECT forwarder that does not
inspect TLS):

| Option | How it works | For | Against | Verdict |
| --- | --- | --- | --- | --- |
| **A. Playwright route in the node daemon** | The engine registers a route on the context for the hosts in the lane's `sign_hosts` list. The handler computes the Signature headers with the lane's key and calls `route.continue` with the new headers | Key stays in the node process, never in the browser or profile. Playwright routes "any requests that a page does, including XHRs and fetch requests" (read). Only matched hosts are touched. No change to the TLS connection. Reuses the engine's Playwright | Camoufox's route support is not verified for our build. Playwright says "Chromium and Firefox follow the redirect without calling your handler" (read), so redirect hops are unsigned unless handled. Network-owned headers (`Cookie`, `Host`, `Content-Length`, `Accept-Encoding`, `Sec-Fetch-*`) cannot be set, which the protocol does not need. Interception may be a detectable side channel: not measured. Service workers hide requests from routes, so they must be blocked for signed hosts (read) | **Best candidate. Spike first.** |
| **B. Firefox WebExtension** (`webRequest.onBeforeSendHeaders`, blocking) | An extension adds the headers on every request before it is sent | Can see and change headers without Playwright. The body is not needed, because the protocol does not cover it | The `onBeforeSendHeaders` page says it is headers-only and needs a blocking listener, which requires `webRequestBlocking` (read). Cloudflare's Chrome example needs an enterprise policy for that permission (read). Firefox's manifest V3 status is not verified. Ed25519 WebCrypto exists from Firefox 129 but only in a secure context (read). Extension contexts were not verified. The key would sit in extension storage on the lane's disk, or cross to the node on each request through native messaging. An unpacked extension in Camoufox was not verified, and any extension is a new fingerprint surface | **Fallback only, if A fails** |
| **C. Egress forwarder on the node** | The existing forwarder adds the headers | For plain HTTP only, it is trivial | For HTTPS it sees only `CONNECT host:port`. Signing would need TLS interception with a lane CA. The site would then see a TLS connection from the node's proxy process, not from Firefox. That breaks the pinned TLS parity that principle 1 depends on, and it breaks pinned certificates. This is reasoning, not a measured result | **Rejected** |
| **D. Page script wrapping fetch and XHR** | Injected JavaScript signs page-initiated requests | None that A lacks | Navigations and subresources the browser makes bypass it. The signing key or signer is reachable from page code. Detectable | **Rejected** |
| **E. A managed browser signs for us** | A vendor's browser signs with keys it holds (AgentCore-style) | Easy | The vendor holds the key. In a hosted product that means the cloud holds identity keys, which breaks the rule that the controller never holds secret plaintext (ARCHITECTURE, guiding invariants). Not self-hosted | **Not for the core. Useful as a comparison** |

### 4.2 What Chrome-based agents do today

Cloudflare's reference extension adds a `Signature` on every outgoing request, but only in
Chrome, and only with `webRequestBlocking` under an enterprise policy (read). Fingerprint's
guide suggests either Playwright's `Page.route` or a browser extension (read). Amazon's
managed browser signs on the service side (read). We found no evidence about Browserbase or
Anchor Browser, so we do not describe them.

### 4.3 Per request or per session

**Per request.** Each request gets its own `created`, `expires` and nonce, and covers its
authority, method and path. A signature for one URL cannot be replayed against another path,
which is one reason the protocol recommends covering `@method` and `@path`. Keep `expires`
short. Cloudflare says a short `expires` (about a minute) is its replay defence and that it does
not validate nonces (read). Fingerprint recommends one hour and a nonce of at least 16 bytes
(read). We would take the short end, because a stolen signature is only useful for the window
it covers. The protocol lets agents "sign every request or sign only when an origin requests a
signature" (Appendix C.9).

"Session" is not a signature concept. If a verifier needs to see a task or workspace, the
signed-agent metadata draft carries opaque `session` and `project` values, and says platforms
"MUST NOT place personally identifiable information" in them (read). That draft is individual,
not adopted, so it is an option for later.

## 5. Proposed design

### 5.1 Identity model

- **One operator origin per workspace.** `agents.<owner's domain>` or similar, chosen by the
  owner. The origin is what sites see, so it is also the blast radius: a site that blocks the
  origin blocks every lane under it, and the protocol says keys sharing an origin look like one
  operator. Start with one origin per workspace.
- **One key per lane.** A lane is one identity (PRINCIPLES 1, "one lane is one identity"). Its
  key is an Ed25519 keypair. Grants do not get keys (section 5.7).
- **Two classes when needed.** A lane driven live for a person (`user`) and a lane acting on a
  schedule or a brain with no person present (`scheduled`) differ in purpose. Section 6.5
  recommends separate keys, and separate origins if sites should tell them apart. Start with
  `user`. Give `scheduled` its own key and origin before flows sign anything, and label it
  truthfully in the signed card.
- **No identity by default.** A lane signs nothing until the owner enables a host for it.

### 5.2 Keys

- Stored at `AGENTOS_HOME/secrets/webbotauth/<lane-id>.key`, mode 600, PEM, created with the
  hard-link pattern from `policy/keys.py` and `node/channel/identity.py`. Refuse to load a key
  that other users can read.
- Never in the lane's profile directory. Profiles are copied for snapshots, forks and
  workspace export, and a copied key would make two lanes look like one.
- Domain separation: identity keys sign only the RFC 9421 signature base for this protocol. They
  are distinct from `policy.key` (grant signing) and `node.key` (node identity), which already
  exist. Section 6.5 asks for this, and the grant tokens already use context prefixes for the
  same reason.
- Rotation: a new key is published next to the old one, used from a set date, and the old one is
  removed after its last possible use (section 5.8).

### 5.3 Directory

- The node cannot serve the directory, because nodes dial out only and have no public ingress
  (ARCHITECTURE, guiding invariants). The node builds the directory, signs it, and publishes it to a
  static host the owner controls, using an outbound write credential held in the vault and
  resolved on the node (PRINCIPLES 3).
- Contents: a JWKS of the active lane keys (public parts only), with `Content-Type:
  application/http-message-signatures-directory+json`, plus a directory signature per key
  (Appendix B.1). The `expires` on those signatures should be well past the republish interval,
  since Appendix C.8 says a short expiry "buys nothing and costs availability". Republish
  every few hours.
- Host requirements to check before publishing: returns 200 with the right media type, sets the
  `Signature`, `Signature-Input` and `Content-Digest` headers, and does not redirect. Some static
  hosts cannot set custom headers (per host, to check). If the host cannot set them, verifiers
  that fetch the directory directly can still use the keys (Appendix B.1 allows this), but
  redistributed copies carry no proof of attribution.
- Hosted edition: the tenant's node signs the directory, and the cloud only serves it. The cloud
  never holds a tenant private key (hosted-directories draft, read; ARCHITECTURE invariants).

### 5.4 Which requests are signed

- Every request from the lane to a host in `sign_hosts`: navigations, subresources, fetch and
  XHR. The protocol's introduction says "every request from bots be signed" (read). Fingerprint
  says the same (read).
- Requests to any other host are never signed, so identity never leaks to hosts the owner did
  not choose.
- Redirects: sign each hop the handler sees. If Playwright does not call the handler for a hop
  (Chromium and Firefox per the docs), that hop is unsigned. The spike measures this, and the
  design records it as a known gap.
- Covered components: `@authority`, `@method`, `@path`, `@query` when present (not
  `@query-params`, which Cloudflare rejects, read), and `signature-agent`. `created` is now,
  `expires` is now plus 120 seconds by default, and each request gets a fresh 16-byte nonce.
- The `Signature-Agent` form is whichever Phase 0 proves accepted, per origin. Default is the
  dictionary form the spec requires.
- Never store the `Signature` header in traces or logs (see 5.8).

### 5.5 Per-site opt-in

- Each lane has `sign_hosts`, a list of hosts it may sign for. Default is empty, so a lane is
  unsigned everywhere, which matches how it behaves today.
- Only the owner can change `sign_hosts`, in the panel or the CLI. An agent cannot change it.
  This is an effect class `commit` change: approving it once is a policy decision, not an
  action on a site.
- Each entry carries a short reason the owner wrote ("read-only research on X, site accepts
  signed agents"), shown in the panel and in grants.

### 5.6 Panel

- Lane page, "Identity" card: origin, short key id (the thumbprint, truncated), `sign_hosts`
  with a state per host (off, signing), time of the last signed request, directory state
  (published, directory signature valid, expires in N days), and a plain statement that the lane
  "identifies itself as an automated agent" to listed hosts.
- Stop signing is one click and takes effect at once. Rotate and enable-host are approval
  actions.
- Activity rows for signed requests show "signed as <origin>, key <id>", so an owner can see
  which lane spoke as which identity.
- Default state is "off", and the card says so.

### 5.7 Grants

- A grant gets an `identity` field: the hosts it may sign for, a subset of the lane's
  `sign_hosts`. The owner's root grant sets it. Child grants can only narrow it (PRINCIPLES 6).
  This follows the same narrowing rule as domains and egress in `policy/`.
- A child grant can never turn signing on for a host the lane does not already list.
- Revoking a grant stops signing under its session metadata. The lane's key stays.
- Per-grant keys were considered and rejected. Grants are many and short-lived, and per-grant
  keys would create a new "bot" for each sub-agent and grow the directory without bound. Per-grant
  information belongs in the opaque `session` value, when a verifier wants it.
- "Explain this grant" gets one more sentence: "This grant may sign requests as <origin> to
  <hosts>."

### 5.8 Rotation, revocation, logging

- **Rotation.** Default every 90 days, with at least 24 hours of overlap: publish the new key,
  sign with it from a set date, remove the old one after its last `expires` plus the directory
  cache time (verifiers stop accepting a removed key only when their cache expires, read).
- **Urgent revocation.** Stop signing for the lane, remove the key from the directory, and
  shorten the directory `Cache-Control` so verifiers refresh sooner. Verifiers may ignore short
  cache times. We cannot recall signatures already sent, so their 120-second window is the most
  that can still be used.
- **Logging.** Each signed request writes a trace row with the lane, `keyid`, origin, covered
  components, `created`, `expires`, the grant session hash, and a hash of the signature base.
  The `Signature` header itself is never stored, because it is a replayable bearer token until
  it expires (section 6.9, read). Rotation, enable, disable and revoke are audited with who did
  them and which approval covered them. This feeds the signed audit log in the backlog.

### 5.9 Hosted edition

- Each tenant gets its own origin. The hosted directory service only serves signed directories
  that tenant nodes produce. No tenant private key reaches the cloud.
- Registration with a programme (section 3.2) is per tenant and per site, and only with the
  tenant's consent. The cloud does not apply on a tenant's behalf.
- Federation (sharing a lane across meshes) is out of scope until the metadata and anonymous
  credential drafts settle.

## 6. Where it helps and where it hurts

**It helps where:**

- A site runs a bot-management product that already verifies signatures, and the owner's
  read-only research or monitoring would otherwise meet a challenge or block. The benefit is
  "a declared, accountable agent instead of a suspicious human" only at those sites.
- The owner runs or partners on a site that wants to know which agent it is dealing with
  (principle 7 satisfied in a form the site can check).
- An API-like flow runs under a grant the site already accepts, and the site wants per-agent
  attribution.

**It hurts or does nothing where:**

- **The owner's consumer accounts.** Social networks, marketplaces, email and banks. We found no
  primary document that they accept signatures. Declaring "automated agent" on an account whose
  terms ban automation puts the declaration in the site's logs as evidence. That is a risk, not
  a finding, and it is the main reason signing stays opt-in per site.
- **Allow-only-signed modes.** AgentCore's docs describe an owner option to allow only signed
  bots (read). A lane that works unsigned today could be blocked on such a site. Signing must be
  additive, and the spike must check each site before it is enabled.
- **Scoring.** A signed request with a browser fingerprint that looks like a home laptop can look
  like a spoofed claim to a bot manager. We have not measured this. The spike should record how
  the scorer reacts on the Cloudflare test endpoints, and an owner's real site should only be
  tested after that.
- **High-scrutiny sites** from the repo's own notes (Instagram and Facebook). No evidence either
  way. Keep them off.

**Tension with the principles.** Principle 1 says a lane should look like the owner's own
computer, and principle 7 says say when it is an agent. These are reconciled per site. Unsigned is
the default, and the owner turns signing on where declaring is welcome. Principle 7 is still met
by the existing labelling in the UI and by the agent's own declarations. Signing is the stronger
form, used where it helps.

## 7. What changes in which package

| Package or file | Change | Phase |
| --- | --- | --- |
| `src/agentos/webbotauth/` (new) | Keys per lane, RFC 9421 signature base, `Signature-Input` builder, JWK thumbprint, directory JSON and signing, a strict verifier for tests and `agentos identity check` | 1 |
| `src/agentos/node/camoufox/` | Install the signing route only for `sign_hosts`. No route when the list is empty. Block service workers for signed hosts | 1 |
| `src/agentos/node/lanes/` | Create the key at lane creation. Rotation command. `sign_hosts` in the lane site policy. Export excludes keys | 1 to 2 |
| `src/agentos/node/` daemon and runner | Directory refresh timer, publish with a vault-held outbound credential | 2 |
| `src/agentos/policy/grants.py`, `routing.py`, `service.py` | `identity` caveat with narrowing only. Enabling a host is an approval-class change | 2 |
| `src/agentos/policy/traces.py`, `runner/` | Identity fields on action traces (no `Signature` header) | 2 |
| `src/agentos/ops/doctor.py` | Key file permissions, directory fetch and signature check, expiry headroom | 2 |
| `src/agentos/api/`, `web/src` | Identity card, per-host toggle, read routes scoped by grant | 2 |
| `src/agentos/mcp/`, `core/tools.py` | `lane_identity` read tool. Set-identity tool is owner-only, effect class `commit` | 2 |
| `src/agentos/controller/`, the hosted edition | Hosted tenant directories, no tenant keys in the cloud | 3 |
| `docs/` | ARCHITECTURE gets an identity invariant. A tracking row for each phase. PRINCIPLES 7 wording stays | 1 |

Note on naming: `src/agentos/node/channel/identity.py` already holds the node's own key. The new
package is `webbotauth` so the two are not confused. `policy/keys.py` and `node/channel/identity.py`
are reused as patterns, not changed.

## 8. Phased plan and the Phase 1 brief

### Phase 0: spike (a few days, no product code, no owner accounts)

Run on a throwaway Camoufox profile with a test host the team controls, plus Cloudflare's published
test deployments. Do not run on any node. Questions and pass criteria:

1. **Route signing works on Camoufox.** The route fires for the document, a subresource, a fetch and
   an XHR, and the verifier sees the headers. Pass: all four verify. Record what happens to
   redirects.
2. **Fingerprint does not move.** Run the pinned-fingerprint self-check with the route installed and
   without it, and compare navigator values and timing for a navigation. Pass: no key changes, and
   no timing change large enough to notice on a normal navigation. Record numbers, not impressions.
3. **Interop.** Send both `Signature-Agent` forms to Cloudflare's test endpoints (the live deployment
   in the README, and `crawltest.com/cdn-cgi/web-bot-auth`, which Cloudflare documents). Record which
   the verifier accepts. Pass: at least one form is accepted. Cloudflare's published test key is used
   for the spike only and never in production.
4. **Clock and expiry.** Sign with a 120-second window and confirm it verifies with the node clock.

Stop and report if (1) fails for subresources on the Camoufox build, or (2) moves a pinned value. Then
the fallback is option B (extension), and the owner should decide whether that is worth it.

### Phase 1: signing core, off by default, no publication

Starts only after Phase 0 passes. Scope in section 8.1 below.

### Phase 2: publication, panel, grants, audit

Directory publish with a vault-held credential, rotation and revocation commands, per-host opt-in, the
grant `identity` caveat, the panel card, trace fields, the doctor check. Gate: the owner picks the
origin and the first two sites.

### Phase 3: registration and hosted

Programme applications (only per site, only with owner consent), hosted tenant directories, the
signed-agent metadata once a verifier covers it. Federation waits.

### Watch list

WG milestones and the IESG state. Cloudflare's verified-bots form and its treatment of the
`Signature-Agent` form. OpenAI's cloud browser help article (we got 403). Google's Web Bot Auth page
(it changes; it was last updated 2026-10-13 per the fetch). AWS's label names.

### 8.1 Phase 1 builder brief

**Goal.** One lane signs requests to one opted-in test host. Signing is off by default. Nothing is
published, and nothing shows in the panel.

**In scope.**

- `src/agentos/webbotauth/keys.py`: per-lane Ed25519 keys, the same file rules as `policy/keys.py`
  (mode 600, refuse readable files, hard-link creation).
- `src/agentos/webbotauth/base.py`: the RFC 9421 signature base, `Signature-Input` with `tag`, `keyid`,
  `created`, `expires` and `nonce`, JWK thumbprint per RFC 7638 and the OKP form per RFC 8037.
- `src/agentos/webbotauth/verify.py`: a strict verifier that follows protocol section 5.2. Used by
  tests and by `agentos identity check`.
- Tests against the protocol's Appendix E test vectors, plus golden output from Cloudflare's TypeScript
  `web-bot-auth` package generated once and committed. The spike's test key is not the production key.
- `src/agentos/node/camoufox/`: install the route only for hosts in `sign_hosts`. An empty list
  installs nothing.
- A CLI subcommand `agentos identity init | show | test-sign <url>`, following the existing CLI pattern.

**Out of scope.** Directory publication, the panel, grants, the controller, hosted work, registration,
the Firefox extension, any inbound port, changes to `node/channel/identity.py` or `policy/keys.py`.

**Hard rules.**

- The private key never leaves the node process and never enters the profile directory.
- Never sign a request to a host outside `sign_hosts`.
- Never write the `Signature` header to logs or traces. Log `keyid` and a hash.
- Identity keys sign only the RFC 9421 base for this protocol.
- Tests use no owner accounts and no production hosts.
- Any user-visible string follows the repo's no-em-dash rule.

**Acceptance.**

- Unit tests pass, including the Appendix E vectors.
- The route test on a local test server shows `Signature` on the document, a subresource, a fetch and
  an XHR, and none on a host outside the list.
- The pinned-fingerprint self-check passes with the route on and off.
- The repo's full test target passes. `agentos doctor` output is unchanged.

**Stop and report if** the route does not fire for subresources on the Camoufox build, or if a pinned
fingerprint value moves with the route on.

## 9. Risks to the owner's accounts

| Risk | Why it matters | Mitigation |
| --- | --- | --- |
| Declaring "automated agent" on an account whose terms ban automation | The declaration is evidence in the site's logs | Opt-in per host, off by default, the owner writes the reason |
| One bad lane blocks every lane under the shared origin | Cloudflare says repeated violations can revoke status for all bots of an organisation (read). Akamai and AWS let sites block verified traffic | One origin per workspace. Owner can stop signing for one lane at once |
| Allow-only-signed modes block unsigned lanes that work today | AgentCore documents the option (read) | Signing is additive. Each site is checked before it is enabled |
| Key leak lets someone impersonate the lane or the workspace | A signature is a bearer token until `expires` | Keys in `secrets/`, never in profiles, excluded from exports, rotation on import, short signature lifetime |
| Revocation is slow | The protocol defines no revocation (section 5.5.2, read). Verifiers cache directories | Stop signing and remove the key at once. Accept that signatures already sent last until `expires` |
| Replay of a captured signature | A signature can be replayed to the same origin until it expires (read) | Short `expires`, a nonce, and no header stored in logs |
| Directory reveals lane count and operator domain | Anyone can fetch the directory | Public parts only. Only keys for lanes that sign are listed. The owner picks the domain |
| Declared identity conflicts with a human-looking fingerprint | A bot manager may read it as spoofing | Measured in Phase 0 before any real site is enabled |
| Redirect hops unsigned | A site may read them as a different client | Recorded as a known gap, measured in Phase 0 |
| Legal and disclosure duties | Principle 7 cites the EU AI Act disclosure rule. We did not assess it | Legal review before any signed host is enabled for a consumer service |
| Programme registration exposes the operator | A listing names the operator and may need an organisation (not stated for individuals) | No registration in Phase 1 or 2. Owner decides per site |

## 10. Open questions and recommended defaults

| # | Question | Recommended default |
| --- | --- | --- |
| 1 | Which `Signature-Agent` form? | Dictionary form (spec). Bare form only per origin, if a verifier that matters demands it. Phase 0 decides |
| 2 | Origin granularity | One origin per workspace. Split off `scheduled` when flows start signing |
| 3 | Keys per lane or per grant? | Per lane. Per-grant detail goes in opaque `session` metadata only |
| 4 | Signature lifetime | 120 seconds, 16-byte nonce. Check clock skew in Phase 0 |
| 5 | What to cover? | `@authority`, `@method`, `@path`, `@query` when present, `signature-agent`. Never `@query-params` |
| 6 | Key rotation interval | 90 days, 24 hours of overlap, plus an urgent path |
| 7 | Directory host | A static host the owner controls, with headers. Unsigned fallback accepted for direct fetches only |
| 8 | Default per-host state | Off everywhere. First pilot: Cloudflare's test endpoint and one public read-only site the owner picks |
| 9 | Registration | None in Phases 1 and 2. Per site, only at the owner's decision. Ask whether individuals can apply (not stated) |
| 10 | Forks and clones | No key copied. A fork starts with signing off |
| 11 | Workspace export | Keys excluded. Import mints new keys |
| 12 | What is logged? | `keyid`, origin, covered components, times, grant session hash, hash of the signature base. Never the `Signature` header |
| 13 | Hosted keys | Tenant keys stay on the tenant's node. The cloud serves only public directories |
| 14 | Disclosure wording | Keep principle 7 as written. Legal review before consumer use |

## 11. Sources and method

### Primary (read this session)

- IETF Datatracker, Web Bot Auth group: https://datatracker.ietf.org/group/webbotauth/about/ and https://datatracker.ietf.org/group/webbotauth/documents/
- `draft-ietf-webbotauth-httpsig-protocol-01`: https://datatracker.ietf.org/doc/draft-ietf-webbotauth-httpsig-protocol/ (full text at https://www.ietf.org/archive/id/draft-ietf-webbotauth-httpsig-protocol-01.txt)
- `draft-meunier-webbotauth-httpsig-directory` (replaced by the protocol draft): https://datatracker.ietf.org/doc/draft-meunier-webbotauth-httpsig-directory/
- `draft-meunier-web-bot-auth-architecture-05`: https://www.ietf.org/archive/id/draft-meunier-web-bot-auth-architecture-05.html
- Registry draft, `draft-meunier-webbotauth-registry-04`, and the hosted-directories, signed-agent-metadata and anonymous-credentials drafts (read from https://www.ietf.org/archive/id/)
- RFC 9421: https://www.rfc-editor.org/rfc/rfc9421
- Cloudflare, Web Bot Auth reference: https://developers.cloudflare.com/bots/reference/bot-verification/web-bot-auth/
- Cloudflare, Verified bots (incl. signed agents): https://developers.cloudflare.com/bots/concepts/bot/verified-bots/
- Cloudflare, announcement: https://blog.cloudflare.com/signed-agents/
- Cloudflare reference code: https://github.com/cloudflareresearch/web-bot-auth (README and `examples/browser-extension`)
- Amazon Bedrock AgentCore, browser Web Bot Auth (preview): https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/browser-web-bot-auth.html
- Google, Web Bot Auth and Google-Agent: https://developers.google.com/crawling/docs/crawlers-fetchers/web-bot-auth and https://developers.google.com/crawling/docs/crawlers-fetchers/google-agent
- OpenAI cloud browser key directory: https://chatgpt.com/.well-known/http-message-signatures-directory (help article returned 403)
- Fingerprint, implementation guide: https://docs.fingerprint.com/docs/bot-detection/web-bot-auth-implementation
- MDN, SubtleCrypto (Ed25519 from Firefox 129; secure context), `webRequest.onBeforeSendHeaders` and `onBeforeRequest`: https://developer.mozilla.org/en-US/docs/Web/API/SubtleCrypto/sign
- Playwright, Route and Network: https://playwright.dev/docs/api/class-route and https://playwright.dev/docs/network

### Reported (search summaries, not opened)

AWS WAF Bot Control announcement (2025-11-21), Akamai blog (November 2025), DataDome changelog and docs,
the Stytch and Vercel mentions, the Fastly claim, and the third-party OpenAI signing claim. Treat those
as leads to check, not facts.

### Not used

The vendor-reported adoption share and a Google start-date claim that conflicted with another source.

### Not verified

Camoufox's Firefox base version and Ed25519 in its build (sources name 135, 146 and 156; all are above
Firefox 129, so Ed25519 WebCrypto should be present, but confirm on the pinned build). Firefox manifest
V3 support for blocking `webRequest`. Extension contexts and Ed25519. Whether Camoufox supports route
interception as Playwright does. Browserbase and Anchor Browser signing.

### Method

Parallel fetches of the primary texts, grepped for the normative passages quoted above, followed by a
read of the verbatim sections. Nothing was run on any node. No code changed. Branch
`t3/agent-identity-design` at cf2cfe0.

## 12. Phase 0 results (2026-11-04)

**Verdict: go with route signing for Phase 1, with the changes in 12.6.** Every pass criterion
in section 8 was met, and neither stop condition fired: subresources verify, and no pinned value
moved. The spike also found three things the design did not expect. Redirects are broken, not
just unsigned (12.2). The dictionary `Signature-Agent` form fails at Cloudflare's verifier even
when it is byte-exact to the draft's own test vector (12.4). Any installed route slows every host
in the lane, not only the signed ones (12.3).

| # | Question | Result | Key numbers |
| --- | --- | --- | --- |
| 1 | Route signing on Camoufox | **Pass** for document, subresource, fetch and XHR. **Fail** for redirects | 7 of 7 request kinds verified locally (document, image, stylesheet, script, fetch GET, fetch POST, XHR). Document, image, fetch and XHR returned `valid` from Cloudflare's research verifier. The route handler fired 0 times for 6 redirect hops |
| 2 | Pinned fingerprint does not move | **Pass** for navigator and pinned values. The HTTP header order changes on signed hosts only | 15 of 15 readable pinned keys equal the pin in all 22 launches. Header block unchanged on unsigned hosts in 4 of 4 route launches. Median navigation +10 to +16 ms with any route installed |
| 3 | Interop, `Signature-Agent` form | **Pass** (one form accepted): the bare sf-string. The dictionary form is rejected | Research deployment: bare 6 of 6 `valid`, dictionary 0 of 12. crawltest.com: 401 "unknown public key" for every signed request, so it cannot confirm a final verification |
| 4 | 120 s window against the node clock | **Pass** | Node clock -0.2 ms against time.cloudflare.com. One signature verified from 0.7 s to 118 s and was rejected as expired at 125 s. The verifier allows no future skew: `created` +1 s is rejected |

### 12.1 Setup and method

- Where: the dev node, throwaway dev copy `$AGENTOS_DEV_ROOT/identity` (slot 33: Xvfb `:330`,
  loopback test servers on `127.0.0.1:6331`, the signed host, and `127.0.0.1:6332`, unsigned).
  The owner directed the spike to this slot, which overrides the "do not run on any node" line in
  section 8. Nothing touched `/root/agentos`, `/var/lib/agentos`, `/opt/agentos`, the service, or
  the reserved displays and ports. Every process the script started was stopped, and the throwaway
  profile was deleted afterwards.
- Stack: Camoufox 0.5.9 with browser v158.0.1-beta.41 (Firefox 158), Playwright 1.62.0,
  `cryptography` 50.0.2, Python 3.12. Lanes were launched through `CamoufoxEngine.launch`, so they
  got the product's launch options and fingerprint pinning, with a fresh profile, no proxy, and the
  private-network PAC lifted only so that the loopback servers were reachable.
- Key: only Cloudflare's published RFC 9421 test key (B.1.4), keyid
  `poqkLGiymh_W0uP6PZFw-dvez3QJT5SolqXBCW38r0U`.
- Hosts contacted: `http-message-signatures-example.research.cloudflare.com` (its plain-text
  `/v0/api/verify` endpoint returns `valid`, `invalid: <reason>` or `neutral`), `crawltest.com/cdn-cgi/web-bot-auth`,
  `time.cloudflare.com` (SNTP), and the two loopback servers.
- Signer check: Ed25519 signatures are deterministic, so the spike signer was checked byte for
  byte against RFC 9421 B.2.6 and the protocol draft's E.2.1 (dictionary form) and E.2.2 (legacy
  form). All three match. The loopback servers verify each request with a strict verifier that
  uses the node clock and no skew.
- Script: [`scripts/spikes/web_bot_auth.py`](../../scripts/spikes/web_bot_auth.py) (subcommands
  `selftest`, `interop`, `edges`, `browser`, `repeat`, `clock`). It is a spike and not product
  code.

### 12.2 Question 1: route signing on Camoufox

The route was `context.route("<scheme>://<host>/**", handler)`. The handler signs and calls
`route.continue_(headers=...)` with the request's full header set plus the three signature headers.

| Request | Local strict verifier | Cloudflare research verifier |
| --- | --- | --- |
| Document (`Sec-Fetch-Dest: document`) | valid | `valid` |
| Subresources: image, stylesheet, script | valid (3 of 3) | image `valid`. The script was signed, but Playwright cannot read a script body after navigation |
| `fetch` GET, `fetch` POST (10-byte body arrived intact) | valid (2 of 2) | fetch `valid` |
| XHR | valid | `valid` |
| Cross-origin image to the unsigned host | unsigned, as intended | not applicable |

Cookies were present on every routed request. The handler's own cost (signing in Python) was a
median of 0.32 ms (p90 0.85 ms, n = 70).

**Redirects (the gap is worse than section 5.4 assumed).** Playwright never called the handler
for a redirect hop: 0 calls for 6 hops, as its docs say for Firefox. But Firefox does not send the
hop "unsigned". It reuses the **entire overridden header set** of the first hop, including `Host`:

| Redirect | `@path` covered | Only `@authority` and `signature-agent` covered | `Host` on the hop |
| --- | --- | --- | --- |
| Signed host to the same host (fetch and navigation) | stale signature, **invalid** (2 of 2) | stale signature, **valid** (2 of 2) | correct |
| Signed host to the unsigned host (fetch and navigation) | stale signature sent to the other host, **invalid** | stale signature sent to the other host | **`127.0.0.1:6331`, the old host** (2 of 2) |
| Unsigned host to the signed host | unsigned | unsigned | correct |

Controls, run in the same launch: with no route at all, or with a route that calls
`route.continue_()` without headers, every hop carried the correct `Host`. Leaving `Host` out of
the override did not help, and an override with only the three signature headers did not help
either. Playwright's `headers` replaces the whole set, so that variant also dropped `User-Agent`,
`Accept` and `Accept-Language` from every request. So any header override has two effects:

1. A cross-host redirect from a signed host reaches the next host with the previous host's `Host`
   value. A CDN routes by `Host` (or `:authority`), so such redirects (login bounces, `www` to
   apex, link shorteners) will likely fail. This was measured on loopback HTTP/1.1. The permitted
   test hosts have no cross-host redirect, so HTTPS was not measured.
2. The same hop hands a live signature for the signed host to a third party. With `@path` covered
   it can be replayed against that one path for the rest of the 120 s window. With only
   `@authority` covered it can be replayed against any path.

### 12.3 Question 2: the pinned fingerprint

**Navigator and page values.** The lane was launched 22 times from one pin: 6 in the main plan (no
route, glob route, glob route with the original header case kept, no route, callable route, glob
route with `service_workers="block"`), then 16 more alternating no route and route. In every launch
the 15 pinned keys a page can read (`navigator.userAgent`, `appVersion`, `platform`, `oscpu`,
`hardwareConcurrency`, five `screen.*` sizes and depths, and `window.outer*` and `screenX/Y`) equal
the pin byte for byte. Of the 56 values the probe reads (navigator, screen, window, timezone, locale, WebGL
vendor, renderer and limits, fonts, audio sample rate, plugins, Ed25519 WebCrypto), three differed
between launches, and none of them because of the route:

- `canvas.sha256` differs between two reads in the same launch, so this is Camoufox's per-read
  canvas noise.
- `speech.voices` read 0 the first time and 5 later, because the voices load asynchronously.
- `navigator.maxTouchPoints` is **not in the pin** (the pin has 47 keys and none is about touch).
  It read 0 in 13 of 16 launches, 5 twice and 1 once, and two of those three were launches without
  the route. This is an existing instability in the engine, not caused by signing. It is worth a
  backlog row to pin it.

`service_workers="block"` changed no probe value (`'serviceWorker' in navigator` stays true).
Ed25519 WebCrypto works in a Camoufox 158 page (secure context), which settles one unknown from
section 11 for the extension fallback.

**HTTP headers.** On the unsigned host, the header block was identical, in order and in value, in
all 4 launches with a route installed. On the signed host it changes. Stock Camoufox, document
request:

```
Host, User-Agent, Accept, Accept-Language, Accept-Encoding, Connection, Cookie,
Upgrade-Insecure-Requests, Sec-Fetch-Dest, Sec-Fetch-Mode, Sec-Fetch-Site, Sec-Fetch-User, Priority
```

With the route (the documented `{**request.headers, ...}` merge):

```
Host, Cookie, user-agent, accept, accept-language, upgrade-insecure-requests, signature-agent,
signature-input, signature, Accept-Encoding, Sec-Fetch-Dest, Sec-Fetch-Mode, Sec-Fetch-Site,
Sec-Fetch-User, Connection, Priority
```

Building the override from `request.headers_array()` keeps the names' case (`User-Agent`), but
not the order. Firefox re-adds the headers it owns (`Accept-Encoding`, `Sec-Fetch-*`,
`Connection`, `Priority`, and `Cookie` near the top) around whatever the override sets. The route
API cannot restore stock order. The only place this shows is a host where the lane already
declares itself an automated agent. That makes it a cost to record, not a reason to stop.

**Timing.** Medians of `page.goto` to a small loopback page, n = 30 per cell, modes interleaved,
two separate runs:

| Route state | Signed host (ms) | Unsigned host (ms) |
| --- | --- | --- |
| No route | 71.9 / 79.5 | 71.4 / 79.1 |
| Glob route for the signed host | 82.3 / 95.5 | 84.5 / 88.7 |
| Callable matcher | 83.0 / 94.3 | 82.3 / 93.9 |

Navigation Timing `duration` on the signed host went from 50.5 / 55.5 ms to 63.0 / 72.0 ms. TTFB
(`responseStart` minus `requestStart`) stayed at 1 to 2 ms, so the added time falls outside the
server round trip. To Cloudflare's research
page (n = 10), unsigned against signed: 91.3 against 101.3 ms, and 84.9 against 105.4 ms. On a page
with 70 same-host subresources (n = 10) the medians were 677 ms with no route and 850 ms with the
route on the signed host (+25%). On the unsigned host they were 699 ms and 798 ms (+14%).
**Firefox pauses every request once any route exists**, so the slowdown hits hosts that are not
signed. A person would not notice 10 to 20 ms. A timing-based detector might, which is one more
reason to install no route when `sign_hosts` is empty, as section 7 already says.

### 12.4 Question 3: interop

Matrix: four `Signature-Agent` forms (bare sf-string, dictionary `sig1="..."`, dictionary with
`;type=directory`, and an unquoted URL as a negative control), three nonce sizes (none, 16 and 64
bytes), and two component sets (`@authority` and `signature-agent`, and the full set with
`@method`, `@path` and `@query`). Plus 16 malformed variants, sent over plain HTTPS from the node.
The browser route then repeated the bare and dictionary forms.

**Research deployment** (`/v0/api/verify`):

- Bare form: `valid` for all 6 nonce and component combinations, and from the browser for document,
  image, fetch and XHR.
- Dictionary form: `invalid: invalid signature` for all 12 combinations (with and without
  `;type=directory`), and from the browser for all four request kinds. The signature that fails is
  byte-exact to the draft's E.2.1 construction. The deployment accepts a dictionary header only
  when the signature base carries the **raw header value** (`sig1="https://..."`) for
  `"signature-agent";key="sig1"`. That is not what RFC 9421 section 2.1.2 or the draft's vector
  specify, so a conforming dictionary signer fails here.
- It is lenient elsewhere. It accepts a request with no `Signature-Agent`, with `signature-agent`
  not covered, and with an unquoted URL. It rejects a tampered signature, `@query-param`, a wrong
  `tag`, an `http://` agent, an expired signature, and a `created` in the future.
- Nonce: no nonce, 16 bytes and 64 bytes were all accepted. The reference library on GitHub `main`
  (`web-bot-auth`, `validateNonce`) rejects any nonce that is not exactly 64 bytes, and its own
  example signers send the dictionary form with `;type=directory`. So the deployed verifier and
  the published library disagree. Expect the deployment to change.

**crawltest.com** (`/cdn-cgi/web-bot-auth`, Cloudflare's production verifier):

- Every signed request, including tampered and expired ones, got 401 `unknown public key or
  unknown verified bot ID for keyid`. **The published test key is not a known key there.** So
  crawltest cannot show a verified 200 for any form without a registered key, and registering one
  is outside Phase 0.
- What it does show is its format gate, which runs before the key lookup. It returned 400
  `signature, signature-input, or signature-agent is malformed` when `signature-agent` was not
  covered, when a dictionary header was covered without `;key=`, for `@query-param`, and for a
  wrong `tag`. It returned 400 `missing signature / signature-input / signature-agent headers` with
  no `Signature-Agent`. Both the bare form and the dictionary form with `;key="sig1"` passed the
  gate (401). Expiry is checked only after the key lookup.

So gate (b) in section 1 is only partly met. One Cloudflare verifier accepts the bare form, and
the production edge cannot be observed without registration.

**Headers that verified** (research deployment, `GET /v0/api/verify?q=1`):

```
Signature-Agent: "https://http-message-signatures-example.research.cloudflare.com"
Signature-Input: sig1=("@authority" "@method" "@path" "@query" "signature-agent");created=1793768938;expires=1793769058;keyid="poqkLGiymh_W0uP6PZFw-dvez3QJT5SolqXBCW38r0U";alg="ed25519";nonce="f0wYUTctfClla1DllORminLs43kccNPMdLQloVCm3g0hDN/u9smt8b/mb8gL9uOMcgBXie/dPwT9bDMzowav1w==";tag="web-bot-auth"
Signature: sig1=:xZw9GTucYarmKPg7BpfzDCCsNTkcG35CS0Etdf8MxfihC87D9POeayUTPoXTOf+Yxjee7Iu3tsfYHmWurt2LAA==:
```

with the signature base:

```
"@authority": http-message-signatures-example.research.cloudflare.com
"@method": GET
"@path": /v0/api/verify
"@query": ?q=1
"signature-agent": "https://http-message-signatures-example.research.cloudflare.com"
"@signature-params": ("@authority" "@method" "@path" "@query" "signature-agent");created=...;tag="web-bot-auth"
```

The dictionary form that failed sent `Signature-Agent: sig1="https://http-message-signatures-example.research.cloudflare.com"`,
covered `"signature-agent";key="sig1"`, and used the base line
`"signature-agent";key="sig1": "https://http-message-signatures-example.research.cloudflare.com"`
(the draft's form). These signatures were made with the public test key and have expired, so they
are safe to publish.

### 12.5 Question 4: clock and a 120-second window

- Node clock: `systemd-timesyncd` synchronised to `ntp.hetzner.com` (offset -4.2 ms, jitter
  5.5 ms). SNTP against `time.cloudflare.com` gave -0.21 ms before and -0.37 ms after the run
  (2.1 ms round trip).
- One signature (`created` = node time, `expires` = `created` + 120) was sent again and again to
  the research verifier: `valid` at 0.7, 30, 60, 90, 110 and 118 s, and `invalid: Signature expired`
  at 125 and 135 s. The local strict verifier agreed on every sample. The same nonce was accepted
  six times, which confirms Cloudflare's note that it does not check nonces for replay.
- Boundary map (fresh signatures with a 120 s window): `created` -60, -115 and -119 s verified, and
  -121, -125 and -180 s were expired. **`created` +1 s was already rejected** ("created in the
  future"), and so were +2, +5, +10, +30 and +60 s. The research verifier allows no future skew, as
  `clockSkew: 0` in the reference code suggests. A node clock running one second ahead of
  Cloudflare's would break every signature.
- crawltest.com answered 401 (unknown key) at every offset, so it says nothing about time.

### 12.6 Recommendation for Phase 1

**Go with route signing.** Do not fall back to the extension now. The route met every criterion
section 8 set, and the extension still carries every unknown in section 4.1 and is a new
fingerprint surface. Do not stop: the redirect break is real but bounded, and Phase 1 is off by
default and limited to one test host. Change the Phase 1 brief (8.1) and the defaults as follows:

1. **`Signature-Agent` defaults to the bare sf-string form**, per origin. This reverses the
   default in 5.4 and open question 1. Offer the dictionary form per origin only once a verifier
   is shown to accept a conforming one. Keep both forms in the Phase 1 unit tests (E.2.1 and E.2.2
   already pass in the spike signer).
2. **Nonce: 64 random bytes, standard base64** (not 16). The deployed verifier takes either, but
   the reference library requires exactly 64.
3. **`created` = now minus 2 s, `expires` = `created` + 120.** Cloudflare allows no future skew.
   `agentos doctor` should warn when the node clock is not NTP-synchronised.
4. **Covered components: `@authority`, `@method` and `signature-agent`. Do not cover `@path` or
   `@query` until redirects are fixed.** With `@path` covered, every same-host redirect hop sends
   an invalid signature (2 of 2), and an invalid signature is worse than none. The cost is that a
   leaked signature can be replayed against any path on that host for up to 120 s. Owner's call;
   the spike measured both.
5. **Pass the full header set**, built from `request.headers_array()` (it keeps case), with the
   three signature headers appended. Never pass only the new headers: Playwright then drops
   `User-Agent`, `Accept` and `Accept-Language`. Record the header reorder on signed hosts as a
   known cost.
6. **Install routes only when `sign_hosts` is non-empty**, one glob per host, never a callable
   matcher. Any route slows every request in the lane by about 10 to 16 ms per navigation, and by
   about 14% to 25% on a page with 70 subresources.
7. **Redirect guard, required in Phase 1 acceptance.** Add a test that pins today's behaviour (the
   stale `Host` and signature on a cross-host hop). Add a `request` listener that detects a
   redirect hop leaving a signed host and writes an "identity carried across redirect" trace row,
   without storing the `Signature` header. Investigate a fix in Camoufox's Juggler (drop the
   override, or re-intercept, on redirect) and report the bug upstream to Playwright. **Phase 2
   must not enable signing for any real site until cross-host redirect hops are clean**, either
   through that fix or by using the extension only for redirect hops.
8. `service_workers="block"` for signed lanes is safe for the fingerprint (no probe value moved),
   but it applies to the whole context, so it also blocks service workers on unsigned sites.
   Note this in the lane docs.
9. Separate from signing: pin `navigator.maxTouchPoints` in the engine (backlog). It is unpinned
   today and varied in 3 of 16 launches.

Gates (b) and (d) in section 1 stay open. Cloudflare's production verifier could not be observed
because the test key is unregistered, and the research verifier is ahead of or behind the
published library in two ways (dictionary base, nonce length).

## 13. Phase 1 as built (2026-11-04)

Phase 1 is in `src/agentos/webbotauth/` with the engine hook in `node/camoufox/engine.py`
(`_install_identity`), the CLI `agentos identity`, and the doctor's clock check. It follows the
brief in 8.1 with the changes of 12.6, and it changes one thing the spike did not settle: **how a
signed request is sent.**

### 13.1 The redirect decision: the driver sends signed requests, the browser follows redirects

The spike showed that `route.continue_(headers=...)` cannot be made safe: Firefox follows the
redirect without calling the handler and reuses the whole overridden header set, a live signature
and a stale `Host` included. Neither of the two remedies the brief named works for every request:
an extension that strips the headers on the next hop adds a fingerprint surface and every unknown
in 4.1, and "not signing navigations that may redirect" leaves fetches, XHRs and subresources,
which redirect too (the spike's own fetch redirect leaked). So Phase 1 never overrides a header
on a request the browser sends. For a request to a listed origin, the route:

1. takes the browser's header set from `request.headers_array()` (names keep their case), drops any
   `Signature*` header a page set, keeps only the content codings the driver decodes (Firefox also
   offers `zstd`), and appends the three Web Bot Auth headers;
2. sends exactly that request from the Playwright driver, `route.fetch(max_redirects=0)`;
3. fulfills the browser's request with the response, a 3xx included.

The browser then follows a redirect as a new request with only its own headers. A signature can
reach only the URL it was made for, by construction. Evidence, on the dev node, slot 35,
Camoufox 0.5.9 (Firefox 158), Playwright 1.62.0 (`tests/node/test_identity_node.py`, 6 of 6 passed):

| Check | Result |
| --- | --- |
| Document, stylesheet, image, script, `fetch` GET, `fetch` POST (body intact), XHR, a gzip response | all signed and `valid` at the local strict verifier with the lane's own key; the script ran; two `Set-Cookie` headers on one response both landed in the lane's cookie jar, `HttpOnly` kept, and later requests carried them |
| Requests to a host not in `sign_hosts` | no signature header on any of them |
| Signed host to another host, `fetch` and navigation | the next host gets its own `Host` and **no signature** (2 of 2); the detector writes a `redirect` row (`left_host`, `carried_signature: false`) and one `identity_redirect_off_host` lane event |
| Signed host to the same host, and another host into the signed host | the hop is not routed, so it is sent **unsigned**, never with a stale signature (3 of 3) |
| The same lane with `continue_(headers=)` (kept as a pin of today's Playwright behaviour) | the hop to the other host still carries the signature and `Host: 127.0.0.1:<signed port>`; the product's detector saw it (`carried_signature: true`), wrote `identity_redirect_leak`, and stopped signing |
| A page on another site embedding the signed host | unsigned, and the browser's cookie rules held (a `SameSite=Strict` cookie stayed home) |
| Pinned fingerprint with no route, with the route, with no route again | the page's whole dump is the same in all three; the request headers a page and a site read as the device (`User-Agent`, `Accept`, `Accept-Language`, `Sec-Fetch-*`, `Referer`) are the same |
| Navigation medians (n = 8, small loopback page) | signed host 94.5, then 105.9 with the route, then 92.5 ms; unsigned host 84.7, 82.0, 77.2 ms |
| Cloudflare research deployment, published test key, through the product route | `valid` for a document and for a `fetch` |

The route goes through the lane's proxy too: a scratch run on the same slot with a local proxy saw
every signed request arrive as a `CONNECT` tunnel through it, signatures inside. Playwright's
request client uses the browser's proxy settings.

### 13.2 What the driver changes, and the guards for it

Signed requests no longer come from Firefox's network stack. On listed origins only:

- **TLS and HTTP are the driver's (Node), not Firefox's.** A bot manager may notice a Firefox
  `User-Agent` over a Node TLS handshake. This is the main cost, and the reason the redirect pin
  test exists: once Playwright or Camoufox stops reusing overridden headers on redirects, signing
  can move back into the browser (`continue_`) and the TLS fingerprint returns to Firefox's.
- **Headers Firefox adds after the route sees a request are missing:** `Priority`, and `Pragma`
  and `Cache-Control` on a no-store fetch. The order is the driver's, as the spike already found
  with header overrides. The node test pins this exact set.
- **Bodies are buffered and not cached** by the browser. Streamed types (`media`, `eventsource`,
  `websocket`, and types Playwright does not name) stay with the browser, unsigned.

Guards, each a fall back to the browser, unsigned, with a trace row giving the reason:

- **First-party contexts only.** The driver keeps no partitioned cookie jar and no SameSite rules
  (a scratch run showed it sending every cookie of the host on a cross-site embed). A request is
  signed only when it is a top-level navigation or every frame above it belongs to the signed
  origin. Service worker requests are not seen by routes and go unsigned.
- **The browser's cookies or none.** If the browser sent no `Cookie` header but the lane holds
  cookies for the URL, the browser withheld them on purpose, and the request goes unsigned.
- **Public addresses only on a lane without a proxy.** The driver skips the lane's private-network
  PAC, so a listed host must resolve to global addresses (cached 60 s). Proxied lanes leave that to
  the proxy, as the browser does.
- **`Host` must match the URL**, or the request is not signed.
- **The detector** watches every redirect hop whose previous hop was on a signed origin, and stops
  signing for the lane at once if a hop off the host ever carries a signature.

### 13.3 What is built, against the brief

- `webbotauth/sf.py` and `base.py`: RFC 8941 fields, the RFC 9421 base, the signer (bare
  `Signature-Agent` by default, the dictionary form as an option; 64-byte nonce; `created` = now
  minus 2 s; 120 s window; `@authority`, `@method` and `signature-agent` covered), RFC 7638 and
  RFC 8037 thumbprints. Unit tests: RFC 9421 B.2.6, draft E.2.1 and E.2.2, and the golden request of
  12.4 (the headers Cloudflare answered `valid`), all byte-exact. The 8.1 plan for golden output from
  Cloudflare's TypeScript package was replaced by that golden request, which a Cloudflare verifier
  accepted.
- `webbotauth/verify.py`: the strict verifier (tag, times with no skew, window at most 24 hours
  unless the caller lifts it, `alg`, keyid, coverage of `@authority` and of `Signature-Agent` when
  sent, https agent URL, no `@query-param`, optional nonce length).
- `webbotauth/keys.py`: one key per lane in `secrets/webbotauth/<workspace>/<name>.key`, the
  `policy/keys.py` rules. The node's secrets were chosen over the vault: a passphrase vault is sealed
  after a restart, and lanes must still open (unsigned is the safe default, but a sealed vault would
  silently turn signing off).
- `webbotauth/settings.py`: `sign_hosts` (empty by default, each with the owner's reason, origins
  only, https except loopback for tests) and the signature agent URL, in a mode 600 file next to the
  key. Only the CLI writes it; no MCP tool or API route exists. The file is bound to the lane's
  profile directory, so a recreated or imported lane starts with signing off and needs
  `agentos identity init --force` (a new key).
- `webbotauth/route.py`: the route, the guards and the detector, and `IdentityProvider`, which the
  engine asks at launch. No hosts means no route and no listener. A broken identity (bad mode, key
  mismatch, another profile) means the lane runs unsigned with an `identity_error` lane event.
- `webbotauth/trace.py`: `lanes/<workspace>/<name>/identity-trace.jsonl` (mode 600, rolls over at
  4 MB) with `signed`, `unsigned` and `redirect` rows: lane, method, origin, path (no query), type,
  status, keyid, covered, created, expires, SHA-256 of the base. Never the `Signature` or
  `Signature-Input` header. Lane events: `identity_signing` at launch, `identity_redirect_off_host`,
  `identity_redirect_leak`, `identity_signing_stopped`, `identity_error`.
- `agentos identity init | show | hosts add | hosts remove | test-sign`. `test-sign` signs one URL
  on a listed host, checks it locally, hides the `Signature` unless `--reveal`, and can `--send`
  it through the lane's proxy.
- `agentos doctor`: the clock check names the lanes that sign when NTP is off or not synchronized.

Differences from 8.1: keys are made by `agentos identity init`, not at lane creation, so
`lanes.py` changes only in its wiring; `service_workers="block"` is not set (it would change every
site the lane visits, and service worker requests are already unsigned rather than leaked); hosts
added to `sign_hosts` take effect at the lane's next launch (that is when its route is
installed); a removal takes effect at once, because an open lane's signer re-reads its settings
file when it changes, and a missing or broken file stops all signing (QA-identity-1).

### 13.4 What Phase 2 needs

- **Publication.** The directory JSON and its Appendix B signature (the key can already produce
  the public JWK), a static host the owner controls with a vault-held write credential, and a
  refresh timer. Then gate (b) can be tested for real at crawltest.com, which needs a known key.
- **Panel and grants.** The identity card, a stop-signing control that unroutes the running lane at
  once (`LaneSigner.stop` exists; the panel needs a daemon route to it), approval-class host changes,
  the grant `identity` caveat, and identity fields on action traces in `policy/traces.py` and the
  runner (untouched here).
- **Rotation and revocation commands** with overlap, and a lane-delete hook that removes the key
  (today a deleted lane's key stays in `secrets/` until `init --force`; it cannot sign for a new lane
  because of the profile binding).
- **Before any real site:** decide whether the Node TLS handshake on signed hosts is acceptable, or
  move signing back into the browser once redirect hops stop reusing overridden headers (report it
  upstream to Playwright and Camoufox's Juggler; the pin test says when it is fixed). Measure how a
  bot manager scores a signed lane, as section 6 already asks.
- Pin `navigator.maxTouchPoints` (12.6, item 9) is already on the backlog's fingerprint row.
