# Design: federation between agentos installations

Status: design, far out. Date: 2026-11-04. Base: `m1-node-lanes-mcp` at `cf2cfe0`.
Owner's bet: share a lane or a workspace with another
person's agentos, with grants crossing the boundary and expiring on your side.
Collaboration without moving passwords, across companies.

Read with: docs/PROTOCOL.md (node channel v1), docs/CONTROLLER.md,
`src/agentos/policy/issuers.py`, `src/agentos/controller/grants.py`.

## 1. Summary and recommendation

A **share** is a grant that one install (the **host**, who owns the lane and its login)
issues to another install (the **guest**). The host's controller is the only door: guest
calls travel guest agent, guest controller, peer link, host controller, host node. The
host's node verifies every call against a token chain whose root the host signed, so the
host's scope, expiry, revocation and approvals bind no matter what the guest's side does.
Nothing secret crosses: the vault types the password on the host's node, and the guest only
ever sees tool results, approval status and (if shared) the live picture.

The model is Tailscale node sharing applied to agent hands: the sharer picks one thing,
the recipient sees only that thing, the shared thing never reaches back into the
recipient's network, and the sharer revokes from their side. Unlike Tailscale there is no
common coordination server, so two controllers pair explicitly (an invitation, an
out-of-band check of a short code, then pinned keys), much as SPIFFE federation and Matrix
servers exchange and pin keys.

**Build later** (phases 1 to 3, about 4 to 6 weeks in total once the multi-org controller
exists): shares between two orgs on one controller first (which is also the "workspace
handoff for agencies" backlog item and the hosted cloud case), then a peer link between two
controllers, then the live view and files across it.

**Prepare now** (about one day on top of the token v2 work, section 5): the token v2
authority block carries a globally unique `iss`, a `kid`, a required `aud` and an optional
holder binding `hld`; revocation ids come from signatures; blocks are canonical; the header
has a reserved flags byte; owners in the `peer:` namespace are reserved and refused; and
PROTOCOL.md reserves `invoke.via`, a `share` field on traces and approvals, the error codes,
and the `peer_*` message family on a separate endpoint. Every reservation fails closed on a
node that does not implement federation, so nothing here can be used before it is built.

**No Biscuit switch is needed.** The guest is authenticated at the link, not inside the
token, and the host's controller re-attenuates every call, so third-party block signatures
(which would be the trigger for moving to Biscuit) are not required. The
reserved flags byte keeps that door open.

## 2. Goals and non-goals

Goals:

- An owner shares one lane, a set of lanes, or a workspace with a named peer install, with a
  scope (lanes, tools, ceiling, domains, budget), an expiry and an approval policy.
- The host's side enforces everything: scope and expiry on the host's node clock, revocation
  pushed by the host's controller, approvals decided by the host's people.
- The guest narrows a share for each of its own agents, offline, and revokes its own agents
  without asking the host.
- Both sides keep an audit record of every call under a share; the host's is authoritative.
- No password, cookie, profile, proxy credential, vault entry, exit IP or node address ever
  leaves the host. The guest's own credentials (brain subscriptions, its agent tokens) never
  reach the host.
- Works between two self-hosted installs, between self-hosted and the hosted cloud, and
  between two orgs on one cloud, with one model and one token format.

Non-goals:

- Re-sharing: a guest cannot pass a share to a third install (refused by holder binding).
- Node-to-node federation, or a guest reaching the host's nodes directly.
- Moving or syncing lanes, profiles or vault entries between installs (that is workspace
  export, already done).
- A shared global directory of installs or a global namespace of names.
- Running the guest's brain on the host's node under a share (hosted agent lanes is its own
  design; a guest's brain stays on the guest's side).
- Merged policy: the guest does not get to change the host's approval routing, and the host
  does not get authority in the guest's mesh by sharing to it.
- Replacing official delegation. Where a platform offers a role (a Facebook Page role, a
  seller user invite), VISION.md's advice stands: prefer it. Federation is for the accounts
  and sites that do not.

## 3. Stories

### 3.1 An agency works in a client's workspace

Dana runs Client Co's agentos: a controller and a node holding `biz-a/instagram`,
`biz-a/facebook` and `biz-a/shopify`, logged in. Northwind (an agency) runs its own agentos
with its agents (Claude Code for copy, a scheduler flow).

1. Dana opens Shares, New share: workspace `biz-a`, lanes `instagram` and `facebook`,
   tools browser, files in, flows; ceiling `commit`; domains instagram.com and facebook.com;
   every commit needs Dana's approval; live view: watch only; expires 2026-12-31. The panel
   makes an invitation for "Northwind" and shows it once.
2. Dana sends the invitation by email. Northwind's owner pastes it into their controller,
   which dials Dana's controller. Both panels show the same code (`tulip-orbit-58`); they
   read it to each other on a call and both press Confirm. The keys are now pinned on both
   sides.
3. Northwind's controller lists the share as `@client-co` (Northwind's own name for the
   peer). Northwind's owner attaches it to the copy agent, narrowed to
   `@client-co/biz-a/instagram`, for two weeks. The agent's tool list now shows
   that lane next to Northwind's own.
4. The agent calls `browser_snapshot` on `@client-co/biz-a/instagram`. The call runs on
   Dana's node under a token chain rooted in Dana's org key. A post fails with
   `approval_pending`; Dana's phone shows the post exactly as it will appear, marked
   "from Northwind / copywriter". Dana approves, the agent retries, the post goes out.
5. Dana's Activity, filtered to the share, lists every call: who (Northwind's label),
   which lane, which tool, effect, outcome, approval. Northwind's panel shows the same calls
   from its side, plus what its own agents asked for.
6. Dana presses Revoke. Within a second her nodes refuse the share, cancel the call in
   flight, close Northwind's live view, and Northwind's agents get `share_revoked`. Had Dana
   done nothing, the node would have refused the share on 2026-12-31 by its own clock, even
   with Dana's controller down and Northwind trying.

### 3.2 Two companies co-run one campaign lane

Alder (company A) owns `launch/x` (the campaign's X account) on its node. Birch (company B)
has the copy team and the community managers. Both want agents and people on it.

1. Alder shares `launch/x` with Birch: ceiling `commit`, replies allowed, posts need
   approval, live view with control for Birch's humans, budget 200 actions a day.
2. Birch adds a guest-side gate: every post by a Birch agent first needs Birch's brand
   lead in Birch's own panel; only then does Birch's controller forward it. Alder's approver
   still decides on Alder's side. Two keys, each side holding its own.
3. Birch shares its `launch/linkedin` lane with Alder in the other direction. One peer link
   carries both shares; each owner is authoritative for its own lane and neither gets
   anything else.
4. When the campaign ends, either share expires on its owner's side. The audit stays with
   both.

## 4. Architecture

### 4.1 Roles and objects

| Term | Meaning |
|---|---|
| Host | the install that owns the lane (its node runs it, its vault logs in) |
| Guest | the install whose agents and people use the lane |
| Peer | the other controller, as one controller sees it; a pinned key plus a local petname |
| Peer link | one authenticated WebSocket between two controllers, carrying every share in both directions |
| Share | on the host: a grant definition (scope, tools, ceiling, domains, budget, approvals, live view, expiry) bound to one peer |
| Share token | the share's root grant token, signed by the host's org grant key, held by the guest's controller and never given to an agent |

Federation is controller to controller only. A node never learns that a peer exists beyond
what arrives in `invoke` (section 4.4). An install without a controller runs `agentos up`
first.

### 4.2 Trust establishment

**Identity.** Each controller gets a stable `controller_id` (`ctl_` + ULID, minted at init)
and, once federation is enabled, a federation key (Ed25519, `secrets/federation.key`, 600),
separate from `controller.key` (which nodes pin) and the org grant keys. A peer is
identified by the fingerprint of its federation key; its name is a petname each side picks.
There is no global namespace.

**Invitation.** The host creates an invitation for one share offer: single use, 24 hours,
shown once. It carries the host's peer endpoint, an invitation id, a 128-bit secret and the
host's federation key fingerprint:

```
afi1_<base64url(canonical json)>
{ "u": "wss://ctl.client.example/v1/peer/connect", "i": "inv_01J...",
  "s": "<secret>", "fp": "fp_9c1e...", "n": "Client Co" }
```

**Pairing.** The guest's controller dials `u`, checks that the host's key matches `fp`,
and presents its own key with the invitation id and an HMAC of the secret. Both sides then
derive a short authentication string from both public keys and the invitation id (a few
words and digits, like Signal safety numbers) and show it. Each owner confirms the code out
of band. Until both have confirmed, the share is `pending` and carries no calls. The
invitation secret proves the guest got the link; the code defeats someone who intercepted
the link and accepted it first (the real guest's code would not match, and the host sees
the stray attempt).

**Pinning and later connections.** After pairing both sides pin each other's key. Every
later connection runs a mutual challenge like the node hello, over TLS:
`sig = ed25519(federation_key, "agentos-peer-hello:v1:" + nonce + ":" + own_id + ":" + peer_id)`.
A key rotation is a `peer_key_rotate` signed by the old key and the new key; a lost key
means pairing again. Either side can dial: the link is symmetric once up. Default: the
guest dials the host, since the host already needs a reachable URL for its own nodes.

**Signed state changes.** Messages that change a share (`share_offer`, `share_accept`,
`share_update`, `share_revoked`, `peer_revoke`, `peer_key_rotate`) carry a signature with the
sender's federation key, as controller-signed node messages do, so each side's log holds
proof of what the other side asked. Calls rely on the authenticated link.

### 4.3 Cross-boundary grants

The share is an ordinary grant, issued by the host and narrowed by everyone after it:

```
block 0  host org key   iss=ctl_H/org_H  kid  aud=[node:N1]  hld=peer:fp_G  share=sh_..
                        sub={kind:agent, id:"peer:northwind", owner:"peer:fp_G"}
                        workspaces/lanes, ceiling, domains, budget, tools, exp=2026-12-31
block 1  guest ctl      label="copywriter"  lanes=[biz-a/instagram]  exp=+14d
block 2  host ctl       aud=[node:N1]  lanes=[biz-a/instagram]  exp=deadline+120s
```

- **Issued by the resource owner's side.** Block 0 is minted by the host's controller with
  the host's org grant key, the key the host's nodes already trust (`issuers.py`). The share
  token goes to the guest's controller over the peer link once both owners confirm.
- **Attenuated for the guest.** The guest's controller appends a block per agent (offline
  attenuation, no key needed) and may append further blocks per call. Blocks can only
  narrow; the v2 rule that attenuation carries a `label`, never a new subject (SEC-5), gives
  the principal `peer:northwind/copywriter` with owner `peer:fp_G`. Labels are the guest's
  claim and are shown as such; the host authorizes on the share's scope, never on a label.
- **Audience-bound.** `aud` names the host nodes that hold the shared lanes. The host's
  controller appends block 2 per call, narrowing `aud` to the one node and the lane to the
  one lane, with a short `exp`, so the token a node sees is single-node and lives minutes.
  This is the same per-call minting the controller does today, done by appending instead
  of re-minting so the guest's narrowing still binds (a token exchange in the sense of
  RFC 8693, without a new root).
- **Holder-bound.** `hld=peer:fp_G` says the chain is only valid when it arrives over the
  peer link of that key. The host's controller checks it before routing, and the node checks
  it against `invoke.via` (section 4.4). A share token stolen from the guest is useless
  except through the guest's own pinned link, and a guest cannot hand it to a third party.
  This is the confused-deputy guard AWS gets from an external id.
- **Short-lived where it travels.** The share token can live as long as the share; it
  never leaves the guest's controller. The guest's agents hold only the guest's own bearer
  tokens to their controller.
- **Revocable on the owner's side.** Revoking the share pushes `revoke` with the share's
  revocation id to the host's nodes (the existing path; every token narrowed from it dies
  with it). The guest is told with `share_revoked`, which is a courtesy: enforcement does not
  depend on the guest hearing it.
- **Expiring on the owner's side.** Block 0's `exp` is checked on the host's node, on the
  host's clock. Nothing the guest does can extend it; a longer share needs a new block 0
  from the host (`share_update` delivers it).
- **Guest revocation.** The guest stops forwarding for an agent it revokes, and may also
  send `peer_revoke { revocation_ids }` so the host's nodes refuse those chains too. The
  host accepts only ids it has seen in chains from that peer's shares, so a guest cannot
  revoke the host's own grants by guessing ids (and with signature-derived ids, SEC-6,
  cannot craft a colliding one).

**What a share can never carry**, enforced on the host's node for any grant whose owner is
in the `peer:` namespace: `admin`, `secret` (vault reveal), `install_base`,
`install_overlay`, `brain` (the host would pay for the guest's model), and standing approvals
created by the guest. `exec` and `service` stay off unless a later phase adds them per share.
Vault fill (the node types a login the guest never sees) is allowed, because nothing
crosses.

**Taint.** The family key stays `family:<root id>`, so every guest agent under one share
shares one taint ledger and one pending-approval cap. That is conservative (one agent's
untrusted read makes the family's next commit go to approval) and it is the right default
for a boundary.

### 4.4 Routing calls across meshes

```
guest agent --MCP--> guest controller --peer_invoke--> host controller --invoke--> host node
            <-------                  <--peer_result--                 <--result--
```

- The host sends `share_tools` and `share_lanes` for each share: the tool definitions the
  share allows and safe lane metadata (id, kind, state, signal, control). No node names, no
  proxy labels, no other lanes or workspaces.
- The guest's controller lists shared lanes under its petname: `@client-co/biz-a/instagram`.
  The `@` prefix cannot collide with node-qualified ids (`node/workspace/name`), because node
  names are lowercase letters, digits and dashes.
- A call on a shared lane becomes `peer_invoke` with the chain the guest narrowed, and an
  `xcall_id` both sides log. The host's controller verifies the chain against its own key,
  checks the share is live and bound to this link, applies the share's rate limits and tool
  allowlist, picks the node, appends block 2, and sends a normal `invoke` with `via`.
- Calls go **through the host's controller only**. The guest never reaches a host node,
  and the host's topology stays private. If the host's controller is down, every share is
  down: that is the fail-closed choice.

```json
{ "v": 1, "type": "peer_invoke", "id": "m_01J...", "body": {
    "share_id": "sh_01J...", "xcall_id": "xc_01J...", "tool": "browser_snapshot",
    "args": { "lane": "biz-a/instagram" }, "grant_token": "agt2_...",
    "principal_label": "copywriter", "deadline": "2026-11-20T10:00:30Z" } }

{ "v": 1, "type": "invoke", "body": {
    "call_id": "c_01J...", "tool": "browser_snapshot", "args": { "lane": "biz-a/instagram" },
    "principal": { "kind": "agent", "id": "peer:northwind/copywriter", "owner": "peer:fp_G" },
    "grant_token": "agt2_...", "deadline": "...",
    "via": { "peer": "fp_G", "share": "sh_01J...", "xcall_id": "xc_01J..." } } }
```

Results return the same way. The host's controller keeps a result for 5 minutes by
`xcall_id`, so a guest retry after a dropped link gets the cached answer (the node's
at-most-once rule, one hop out). On the guest's side, everything from a peer is untrusted
input: sizes and types checked as the hub checks node data, and results tagged untrusted in
the guest's own taint ledger, because a host (or a page on the host's lane) can try to
inject into the guest's agents too.

### 4.5 Approvals

- Approvals start on the host's node exactly as for a local call and are decided on the
  host's side by the host's people, bound to the content hash. The share's approval policy
  can only add approvals (for example, every commit), never remove one the host's routing
  would ask for.
- The guest's agent gets `approval_pending` with the approval id, summary and preview: the
  guest wrote the content, so seeing it is fine. The host pushes `peer_approval { approval_id,
  state, decided_at, note? }` on every change; the guest's panel shows status and the agent's
  `approval_wait` works through its own controller. The guest never decides, and the host
  never shows guest-side approvers as having decided.
- **Guest gate (optional).** The guest may require its own approval before forwarding a
  call. That is guest policy on the guest's controller and adds a key; it never stands in
  for the host's decision.
- Questions (`human_ask`) from a guest agent go to the guest's own people, not the host's.

### 4.6 Audit

- The host's node traces every call as today, with `principal` = the peer principal and
  `share` = the share id. The host's controller logs every `peer_invoke` (peer, share,
  `xcall_id`, tool, lane, outcome, approval). When the signed, hash-chained audit log lands,
  share calls are in it like any other: **the host's record is authoritative**.
- The guest keeps its own record: which of its agents asked for what, the chain it sent,
  what came back.
- The host sends `peer_audit` (its redacted traces for calls under that share, batched,
  acked like `traces`), so the guest holds a copy of the host's view. A guest never receives
  traces of anything outside its shares.
- Later (phase 3): `peer_result` carries a receipt signed with the host's federation key
  over `xcall_id`, tool, lane, a hash of the arguments, a hash of the result and the time. An
  agency can then prove what the client's side ran and returned, and a client can prove what
  the agency asked for (the guest's signed request is in the host's log).

### 4.7 The live view across meshes

- `peer_stream_open { share_id, lane, kind: "vnc", mode }` from the guest; the host's
  controller opens the stream to its node and relays bytes on the peer link. Each hop keeps
  its own credit window (256 KiB, as PROTOCOL.md), and each controller maps stream ids per
  hop, so the node channel is unchanged.
- Watching is the share's default if live view is shared at all. Control needs a share flag
  and a guest human principal (`peer:fp_G` owner, kind `human`, authenticated by the guest's
  controller); the host's owner can always take the lane back, and the host's panel shows who
  is watching.
- Terminal and file streams follow the same path in phase 3: files into a lane's inbox
  (size-capped), files out only with a share flag.
- What a picture shows is shared: vault fill masks password fields, but a page that prints
  an API key shows it. The share's domain list is the host's control here.

### 4.8 Secrets never cross

Never sent to a peer, by construction (no message type carries them): vault entries,
cookies, browser profiles, proxy credentials and labels, exit IPs, node names and addresses,
the host's other lanes and workspaces, the host's grant tokens for its own agents, the
controller's keys. Never sent to a host: the guest's brain credentials, the guest's agent
bearer tokens, anything about the guest's own lanes. What does cross: tool definitions and
lane metadata for shared lanes, call arguments and results, approval summaries and
previews, redacted traces, and stream bytes for shared streams. Each side should assume the
other sees everything it sends.

### 4.9 How this relates to Tailscale node sharing

| Tailscale node sharing | agentos federation |
|---|---|
| Share one machine with another user's tailnet | Share one lane or workspace with another install |
| The recipient sees only the shared machine | The guest sees only the shared lanes and tools |
| Shared machines are quarantined: they answer connections but cannot start them | The host never gains authority in the guest's mesh by sharing; a share is one-way |
| Both tailnets' ACLs must allow a connection | Host scope intersected with the guest's narrowing |
| The sharer removes the share at any time | The host revokes; expiry is on the host's clock |
| One coordination server vouches for both sides | No common server: invitation, out-of-band code, pinned keys |
| Shares network reachability | Shares capabilities: per lane, tool, effect and approval |

Two installs can still use Tailscale underneath (a host's controller reachable on a shared
tailnet address); the peer link's own key pinning holds either way. Two orgs on the hosted
cloud are the Tailscale case: the cloud vouches for both, so pairing can skip the code, but
the share, the tokens and the enforcement are the same.

### 4.10 Threat model

| Adversary | Tries | Bound |
|---|---|---|
| Malicious guest | widen scope, reach other lanes, outlive expiry | node verifies the host-rooted chain: blocks only narrow, `aud`, scope, `exp` on the host clock |
| | read secrets | none cross; `secret` refused for `peer:` owners; snapshots hide field values |
| | publish without consent | host approvals bound to content; guest cannot create standing approvals |
| | approval spam, load, cost | per-share rate limits and concurrency, the family pending cap, budget, no `brain` |
| | revoke the host's own grants | `peer_revoke` limited to ids seen in that peer's chains; signature-derived ids |
| | re-share to a third party | `hld` binds the chain to the guest's link |
| Stolen share token | use it elsewhere | only valid over the guest's pinned link; node refuses `hld` grants without matching `via` |
| Compromised guest controller | everything the guest may do | bounded by the share; host sees it in its audit and revokes |
| Malicious host | lie about results, inject into the guest's agents | guest treats peer results as untrusted; receipts (phase 3); guest keeps its own log |
| | act and blame the guest | guest-signed requests in the host's log cut both ways; guest's own record |
| Compromised host controller | abuse the host's own lanes | already in full control of the host mesh; gains nothing in the guest's mesh |
| Invitation interception | pair as the guest | single use, 24 hours, out-of-band code, host sees the attempt |
| Host org key compromise | mint shares | `kid` rotation through a signed key set; revocation feed |

### 4.11 Failure modes

- **Peer link down.** Guest calls fail fast with `peer_unavailable`. Calls already on the
  host's node finish to their deadline; the guest retries with the same `xcall_id` once the
  link is back and gets the cached result. Approvals keep their own expiry.
- **Host controller down.** All shares stop (fail closed). Host nodes keep running local
  work; per-call tokens die within minutes.
- **Host node offline.** As for a local agent: `unavailable`. A degraded node refuses grants
  it has never seen, which includes any per-call block 2 minted while it was away.
- **Guest controller down.** The host is unaffected. The guest's agents lose the shared
  lanes until it is back.
- **Revocation during a call.** The node cancels calls under the revoked grant (existing),
  closes streams, and the guest gets `share_revoked`.
- **Expiry during a pending approval.** The retry is refused because the chain has expired;
  the approval itself expires on the host.
- **Clock skew between installs.** Irrelevant to enforcement: only the host node's clock
  decides. The guest's panel shows the host's `exp` as the host stated it.
- **Key rotation mid-share.** Org grant key: the node holds several keys by `kid`, so old
  shares keep verifying until reissued. Federation key: `peer_key_rotate` signed by both
  keys; a peer that misses it pairs again.
- **Petname collision.** Petnames are local; a guest renames.

## 5. Fit: what token v2 and PROTOCOL.md must say now

Federation is far out, but three things are expensive to change after token v2 and the
node channel are frozen: the authority block's naming of who issued it and for whom, how
revocation ids are made, and which fields and message names mean what. Each item below is
either needed by token v2 anyway (SEC-6, SEC-9, SEC-10, SEC-5) or a reservation that fails
closed.

### 5.1 Token v2 requirements

1. **Version byte 2 and a reserved flags byte** in the header (`AG | 2 | flags | nblocks`).
   `flags` must be 0; a non-zero value is `invalid_token`. Reserved for third-party block
   signatures, so a later move to them (or to Biscuit) is not another format break.
2. **`iss` (required in block 0)**, globally unique and stable across key rotation:
   `<controller_id>/<org_id>` for a controller-issued grant (`ctl_01J.../org_01J...`),
   `node:<node_id>` for a node-local grant. It names the issuer; it does not authenticate it.
3. **`kid` (required in block 0)**: `k_` + base64url of the first 12 bytes of sha256 of the
   raw public key. `TrustedIssuers` looks keys up by `(iss, kid)` instead of trying each key,
   and holds several keys per issuer for rotation.
4. **`aud` (required in block 0, narrowable later)**: a list of `node:<node_id>` or
   `org:<org_id>` for org-wide. A node refuses a chain unless every block's `aud` (where
   present) names it or its org. A later block may only narrow `aud`.
5. **`hld` (optional, block 0 only)**: `peer:<fingerprint>`. Defined now in the schema so a
   v2 node understands it, and refused by every node until federation ships (denial reason
   `federation_unsupported`). Once built: valid only when `invoke.via.peer` equals it, and
   never on a local MCP call.
6. **`share` (optional, block 0 only)**: a share id (`sh_` + ULID), for audit and display.
   A v2 node refuses it, as it refuses `hld`, until federation ships.
7. **Revocation ids derived from block signatures** (SEC-6), never read from block data,
   UNIQUE in the registry. Federation depends on this: guest-appended blocks must not be able
   to collide with host ids.
8. **Canonical block bytes** (SEC-10): a duplicate-refusing, NaN-refusing parse whose
   canonical re-encoding must equal the signed bytes. Two installs attenuate the same chain,
   so both must read it the same way.
9. **Restriction-only attenuation** (SEC-5): `label` instead of `sub`, the node derives the
   principal id, the owner never changes.
10. **Reserved owner namespace**: a subject `owner` beginning with `peer:` is refused unless
    the chain has a valid `hld`. With `hld`, the node also refuses `admin`, `secret`,
    `install_base`, `install_overlay` and `brain` in any block. Write this rule now; it costs
    a few lines and makes the federation phase a pure addition.
11. **Family key unchanged** (`family:<root id>`), so a share's root is one taint family.

### 5.2 PROTOCOL.md now (additive v1 clarifications)

- **`enrolled.grant_issuer` and `enrolled.grant_kid`** (needed by v2 for any controller
  grant): the node stores the `iss` and `kid` it trusts. A signed **`grant_keys { issuer,
  keys: [ { kid, pubkey, not_after? } ], sig }`** message, controller to node, adds or retires
  keys (rotation without re-enrolling). Reserve both now; build with v2.
- **`invoke.via`** (optional): absent means a local agent of this org. Reserved shape
  `{ peer, share, xcall_id }`. A node that does not implement federation must refuse any
  grant with `hld` whatever `via` says.
- **`principal.owner`** values beginning with `peer:` are reserved for federated
  principals.
- **`share`** (optional string) on trace events, `approval_request` approvals and `run`
  events, so the host's controller filters a share's audit without parsing principal ids.
  Nodes copy it from block 0.
- **Error codes** reserved in `agentos.core.errors`: `peer_unavailable`, `share_revoked`,
  `share_expired`, `holder_mismatch`, `federation_unsupported`.
- **A separate endpoint and namespace**: `wss://<controller>/v1/peer/connect`, its own
  envelope `v: 1`, never mixed with the node channel. Reserved types (the `peer_` and
  `share_` prefixes are not used on the node channel):

| Type | Direction | Purpose |
|---|---|---|
| `peer_challenge`, `peer_hello`, `peer_welcome` | both | mutual key challenge on every connection |
| `peer_pair`, `peer_pair_confirm` | guest to host, both | invitation use; both owners confirm the code |
| `peer_key_rotate` | both | signed by old and new key |
| `share_offer`, `share_accept`, `share_update` | host to guest, guest to host, host to guest | share terms and block 0 (signed) |
| `share_revoked` | either (host revokes, guest gives up) | signed |
| `share_tools`, `share_lanes` | host to guest | allowed tool definitions and safe lane metadata |
| `peer_invoke`, `peer_cancel` | guest to host | a call under a share |
| `peer_result`, `peer_progress` | host to guest | answers, with `re` and `xcall_id` |
| `peer_approval` | host to guest | approval status changes |
| `peer_revoke` | guest to host | revocation ids from the guest's own blocks (signed) |
| `peer_audit` | host to guest | redacted traces for the share, acked |
| `peer_stream_open`, `peer_stream_ready` | guest to host, host to guest | streams; `stream_credit`, `stream_close` and binary frames as on the node channel |
| `ack`, `error` | both | as on the node channel |

Sketches of the two state messages (section 4.4 has the call):

```json
{ "v": 1, "type": "share_offer", "body": {
    "share_id": "sh_01J...", "name": "biz-a for Northwind",
    "lanes": ["biz-a/instagram", "biz-a/facebook"], "tools": ["browser_*", "files_put", "flow_*"],
    "ceiling": "commit", "domains": ["instagram.com", "facebook.com"],
    "approvals": { "commit": "always" }, "live_view": "watch",
    "expires_at": "2026-12-31T23:59:59Z", "grant_token": "agt2_...", "sig": "..." } }

{ "v": 1, "type": "share_revoked", "body": {
    "share_id": "sh_01J...", "revocation_ids": ["rv_..."], "by": "host",
    "at": "2026-11-20T10:02:11Z", "sig": "..." } }
```

## 6. Phased plan

| Phase | What | Size | Depends on |
|---|---|---|---|
| 0. Prepare now | section 5: token v2 fields and refusals, PROTOCOL.md reservations, tests | S (about 1 day on top of token v2) | the token v2 builder |
| 1. Shares inside one controller | a share between two orgs of one controller (the peer is a local org): share table, `@org/...` lanes in the guest org's tool list, host-side approvals and Activity per share, revoke and expiry, rate limits, panel screens | M (4 to 6 days) | multi-org controller; also delivers "workspace handoff for agencies" and the hosted cloud case |
| 2. Peer link | federation key, invitations, pairing code, pinning, `/v1/peer/connect`, `peer_invoke` routing with block 2 and `via`, node `hld` checks, `peer_approval`, `peer_audit`, `peer_revoke`, untrusted tagging on the guest side | L (8 to 12 days) plus a security review | phase 1 |
| 3. Live view, files, receipts | `peer_stream_*` for vnc (watch, then control for guest humans), files in and out, signed receipts, guest gate, share audit export | M to L (5 to 8 days) | phase 2, human lanes (non-owner human principals) |
| 4. Maybe | third-party block signatures in the flags slot, re-sharing with explicit consent, `exec` and `service` per share | unsized | a real need |

### Builder brief: phase 0 (with token v2)

You are extending the token v2 work in `src/agentos/policy/` (tokens.py, grants.py,
issuers.py, service.py) so federation stays possible later. Do not build federation.

1. Header `AG | 2 | flags | nblocks`; refuse `flags != 0` with `invalid_token`.
2. Add to `GrantBlock` (v2 only): `iss`, `kid`, `aud` (required in block 0), `hld`, `share`
   (block 0 only), with the formats in docs/design/federation.md section 5.1. A later block
   may carry only a narrower `aud`; `iss`, `kid`, `hld`, `share` in a later block are
   `invalid_token`.
3. `TrustedIssuers`: keys by `(iss, kid)`, several per issuer; verification picks the key
   by `kid` and checks `iss` matches the issuer it is registered under.
4. Node checks: `aud` names this node or its org in every block that has it; any `hld` is
   refused with `federation_unsupported`, and so is `share`; an owner starting with `peer:` without `hld` is
   refused; for grants with `hld`, `admin`, `secret`, `install_base`, `install_overlay` and
   `brain` are refused (write the check even though `hld` cannot pass yet).
5. Copy block 0's `share` into trace events and approvals as an optional `share` field.
6. PROTOCOL.md: add the reservations in section 5.2 as a "Reserved for federation"
   clarification, plus `enrolled.grant_issuer`, `enrolled.grant_kid` and `grant_keys`.
   Add the five error codes to `agentos.core.errors`.
7. Tests: a non-zero flags byte; a token for node A refused on node B of the same org by
   `aud`; an `aud` widened in a child refused; `hld` refused with `federation_unsupported`;
   a `peer:` owner without `hld` refused; key rotation (two kids, both verify; a retired kid
   refused); `share` reaches the trace.

Done when `make check` passes and the SEC-6, SEC-9 and SEC-10 xfails that v2 targets are
green. No new endpoints, no controller changes beyond minting with `iss`, `kid` and `aud`.

## 7. Risks

- **Platform terms.** Letting another company act in your account may break a platform's
  rules even without sharing a password. It is the host's account and the host's call; the
  docs should say so and point to official roles where they exist.
- **People skip the code.** Out-of-band checks are routinely clicked through. Mitigation:
  the invitation already pins the host's key, so skipping the code only matters if the
  invitation itself was intercepted; make the code short and the stray-attempt warning loud.
- **Injection both ways.** A shared lane is a page the guest's agents read and a channel the
  guest's prompts reach. Approvals cover publishing on the host; untrusted tagging covers the
  guest. Neither covers reads that leak through results; the share's domain list does.
- **The host sees everything the guest sends.** Agencies may not expect their prompts and
  drafts to sit in a client's log. Say it in the share screen.
- **Two hops for live view.** Latency and bandwidth double; a 2 vCPU host relaying several
  guest viewers may struggle. Per-share stream caps.
- **Reservation mistakes.** A field reserved wrongly is a format change later. Every
  reservation fails closed, so the cost of a mistake is a v3 field, not a hole.
- **Scope creep.** Federation touches tokens, both controllers, the panel and streams. Keep
  phase 1 inside one controller and ship it as the agency feature before any peer link.

## 8. Open questions (recommended defaults)

1. Who dials? **The guest dials the host**; the link is symmetric once up. Revisit with a
   relay for hosts without a public URL (the hosted cloud as a rendezvous).
2. Unit of federation? **Controller to controller only**; never node to node.
3. Re-sharing? **No.** `hld` makes it impossible; phase 4 at the earliest, with the host's
   explicit consent.
4. Guest humans in control of the live view? **Watch only by default**; control per share,
   off.
5. Default and maximum share lifetime? **30 days, at most 1 year**, with a reminder to the
   host a week before expiry; renewal is a new block 0.
6. Guest use of `brain`, `exec`, `service`? **Refused** until a share flag exists for them.
7. Taint per share or per guest agent? **Per share** (one family); revisit if one noisy
   agent blocks the rest.
8. Does the guest receive the host's traces? **Yes, redacted, for its shares only.**
9. Peer names? **Local petnames plus the key fingerprint**; no global names.
10. Who may create a share on the host? **Owner and admin roles only**; members may request
    one.
11. Rate limits per share? **60 calls a minute, 2 concurrent calls per lane, 2 viewers.**
12. Move to Biscuit for third-party blocks? **No**; the flags byte keeps the option.

## Sources

- Tailscale, "Share your machines with other users" (quarantine, revocation, both sides'
  access rules): https://tailscale.com/kb/1084/sharing
- SPIFFE Federation (trust bundles exchanged between trust domains, bootstrapped out of
  band, then refreshed from a bundle endpoint):
  https://github.com/spiffe/spiffe/blob/main/standards/SPIFFE_Federation.md
- Biscuit specification (attenuation, signature-derived revocation ids, third-party
  blocks): https://doc.biscuitsec.org/reference/specifications ; its third-party block
  request flow needed fixes in spec v2 and v4 (CVE-2022-31053, CVE-2024-41949), one reason
  to keep guests out of the token's signatures:
  https://docs.rs/biscuit-auth/latest/biscuit_auth/
- UCAN (issuer, audience and delegation chains for capabilities):
  https://github.com/ucan-wg/spec
- Macaroons (contextual caveats, attenuation by any holder):
  https://research.google/pubs/macaroons-cookies-with-contextual-caveats-for-decentralized-authorization-in-the-cloud/
- AWS IAM, the confused deputy problem and external ids:
  https://docs.aws.amazon.com/IAM/latest/UserGuide/confused-deputy.html
- Matrix server-server API, server signing keys and their retrieval:
  https://spec.matrix.org/latest/server-server-api/#retrieving-server-keys
- Signal, safety numbers (out-of-band key verification):
  https://signal.org/blog/safety-number-updates/
- RFC 8693, OAuth 2.0 Token Exchange: https://www.rfc-editor.org/rfc/rfc8693
