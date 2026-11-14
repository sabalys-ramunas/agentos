# Phones as nodes

Design, 2026-11-04. Branch based on `m1-node-lanes-mcp` at `cf2cfe0`. No code. Inputs:
docs/ANDROID.md (Android lanes research), docs/PASSKEYS.md, docs/VISION.md,
docs/ARCHITECTURE.md (sections 5 and 6), docs/PRINCIPLES.md, docs/PROTOCOL.md,
`src/agentos/vault/` (`catcher.py`, `login.py`,
`service.py`, `policy.py`, `totp.py`, `typist.py`), `src/agentos/policy/` (`approvals.py`,
`notifier.py`, `ROUTING.md`) and `src/agentos/node/interfaces.py`.

Items marked **(secondary)** come from news or community sources and need a primary
check before anyone relies on them. Items marked **(spike)** are open until a spike on a
real device answers them.

## 1. Summary and recommendation

The owner's bet has two halves that differ in cost, value and risk. Build them in that
order.

**Half one: any phone as a code source.** This is the high-value, low-risk half. Most of
the owner's accounts are blocked today by one thing: "we sent a code to your phone". The
vault already types TOTP codes and email codes without the model seeing them
(`vault.one_time_code`, `ImapCodeCatcher`). An SMS code is the same problem with a
different transport, so the phone becomes a third code source next to the TOTP seed and
the mailbox, behind the same `FORM_OTP` sink and the same typist.

The platform facts force the shape of the design:

- **Android 17 holds OTP SMS for three hours** from most apps that read SMS, and it
  tells those apps to use the SMS Retriever or SMS User Consent APIs instead. The default
  SMS app and connected-device companion apps are exempt
  ([Android 17 behavior changes](https://developer.android.com/about/versions/17/behavior-changes-all)).
- **Android 15 hides OTP notifications from notification listeners** unless the listener
  holds `RECEIVE_SENSITIVE_NOTIFICATIONS`, which only system apps and some roles get
  **(secondary)** ([Android Authority](https://www.androidauthority.com/android-15-sensitive-notifications-3416414),
  [Android Authority](https://androidauthority.com/android-15-two-factor-authentication-codes-3492585)).
- **Google Play restricts the SMS permission group** to the default SMS handler and a
  short list of exceptions, with a declaration and review
  ([Play Console help](https://support.google.com/googleplay/android-developer/answer/10208820),
  [Play policy](https://support.google.com/googleplay/android-developer/answer/9047303)).
- **The SMS User Consent API needs no SMS permission.** The app asks Play services to
  watch for five minutes, optionally for one sender. When a message with a 4 to 10
  character code arrives, Android shows the user a prompt to let the app read that one
  message ([Google developers](https://developers.google.com/identity/sms-retriever/user-consent/request)).
- **iOS gives apps no SMS access.** The only unattended route is a Shortcuts personal
  automation with the Message trigger (sender and "message contains" filters), which can
  run immediately since iOS 17 but always notifies when it runs
  ([Apple Shortcuts guide](https://support.apple.com/guide/shortcuts/apdd711f9dff/ios),
  [Cassinelli](https://matthewcassinelli.com/automations-run-immediately-shortcuts-notifications) **(secondary)**).

So the recommendation is:

1. **Phase 1a, the code card (smallest, works on any phone today).** When a lane reaches
   a code step and the entry's code source is "relay", the owner gets a push, opens a
   code card in the panel on their phone, and types the code into a sealed field. The
   node types it into the lane. The model never sees it, the VA never sees it, and no
   app is needed. This is also the fallback for every other source.
2. **Phase 1b, the phone channel on the node.** Phones pair with the node (later the
   controller) with their own key, dial out like a node, and answer grant-scoped,
   node-signed code requests with a code sealed to the node's key.
3. **Phase 1c, the Android companion app, with SMS User Consent as the only SMS path.**
   Per-request consent on the phone, no SMS permission, Play-compliant, not affected by
   the Android 17 hold. The app reads one message, extracts the code on the phone, seals
   it, sends it, forgets it.
4. **Phase 1d, the iOS Shortcut.** A Shortcuts automation per sender posts the code to
   the node over the tailnet. It is a standing rule by nature (iOS cannot ask per
   request), so it is opt-in per sender and the node accepts a post only while it has a
   matching open request.
5. **Standing, unattended Android reading only on a dedicated phone** that the owner
   owns, where the companion holds the default SMS role. Spike first. Never on the
   owner's daily phone.

**Half two: phones as lanes.** A real Android phone on a home node, driven over adb, is
the only honest way to reach app-only features in apps that check Play Integrity's
device verdict (docs/ANDROID.md, section 2). It is worth building, but after the redroid
engine work in ANDROID.md, because both share `AndroidInteraction`. Two limits have to
be said plainly:

- **Bank-style apps will mostly refuse a phone lane.** Banks commonly block the app
  while developer options or USB debugging are on, and some block when any accessibility
  service is active **(secondary)**
  ([GX Bank help](https://help.gxbank.my/Personal%2F%2Fsecurity-and-privacy/Fraud_Prevention_and_Security/Why_can't_I_access_the_GX_Bank_app%3F_I_see_a_%22Your_device_may_have_been_modified%22_screen.),
  [Bank Muamalat](https://www.muamalat.com.my/services/business/app-restricted/)).
  Their screens also set `FLAG_SECURE`, which scrcpy shows as black
  ([scrcpy FAQ commit](https://github.com/Genymobile/scrcpy/commit/1fdde490fd2a0b89680a2b5da5e5274192398023)).
  For those, agentos parks the run and asks the owner to do the step on their own phone.
  We do not patch apps or hide debugging.
- **A phone lane belongs to one workspace and uses its own network.** Its mobile or home
  IP is a strong, real identity. That is wanted for the owner's own accounts that already
  live on that phone. It is never lent to browser lanes as a proxy.

**Redroid lanes** stay as ANDROID.md says: not on a small cloud VM, a spike on a throwaway
8 GB VM, then the engine on a 16 GB node in wave 3. Redroid has no SIM, so its codes come
from the phone code source too.

## 2. Goals and non-goals

### Goals

- A login step that needs an SMS code, an approval on another device, or an app-only
  action completes without the model ever seeing the code, and with the owner in control
  of every code that leaves their phone.
- A code request is scoped like a secret: one lane, one site (the entry's registrable
  domain), one sender rule, one time window, one use, tied to the grant that caused it.
- Consent happens on the phone: per request by default, or under a standing rule the
  owner created and confirmed on the phone.
- Every code produces one audit entry (on the node and on the phone), never containing
  the code.
- Works for the owner's own iPhone or Android phone, with no new hardware, from phase 1a.
- A dedicated Android phone on a home node can run as a lane for app-only features of
  apps that allow it.

### Non-goals

- **Other people's phones or numbers.** Only phones and numbers the owner owns, or a
  teammate's own phone that the teammate pairs and consents with (principle 2).
- **SIM farms, virtual numbers for sign-ups, number rental, or SMS receiving services.**
  agentos never creates accounts and never holds numbers it does not own (principle 2,
  "mass or fake account creation: no").
- **Bypassing a site's 2FA.** agentos completes the site's own 2FA with the owner's real
  factor. It does not intercept codes meant for anyone else, downgrade a factor, or
  approve a login prompt by itself.
- **Faking device integrity.** No spoofed Play Integrity verdicts, no hidden root, no
  patching `FLAG_SECURE` away, no hiding developer options from banking apps
  (docs/ANDROID.md, "We do not try to fake it").
- **Mobile proxies.** A phone's connection is never used to route browser lanes.
- **Reading the owner's messages.** Only one message per request, matched by sender and
  pattern on the phone; message bodies never leave the phone.
- **A native iOS app in phase 1.** iOS gets the code card and the Shortcut.

### A rule that comes before all of this

**Prefer TOTP.** Where a site offers an authenticator app (Instagram, Facebook, Google,
LinkedIn, Reddit do), the owner moves that account to TOTP in the vault and the phone is
not involved at all. The phone code source is for sites that only offer SMS, for
suspicious-login checks that send an SMS even when TOTP is on, and for numbers that are
the account's recovery factor.

## 3. Stories

### 3.1 An Instagram login that sends an SMS code to the owner's phone

Lane `biz-a/instagram`, entry `biz-a instagram` with a password and a code source
`{kind: "phone", phone: "owner-pixel", senders: ["32665"], marker: "Instagram"}` (the short code is an example; senders differ by country, and spike A records the real ones).

1. The agent calls `login` under a grant with `secret` on that lane and domain.
2. `login_flow` picks the entry, checks the policy, calls `ctx.authorize(SECRET, ...)`,
   types the username and the password.
3. **Before pressing Enter**, the vault arms the code source: it creates a code request,
   signed by the node key, and sends it to `owner-pixel`. The phone shows: "biz-a /
   instagram on node home-1 wants an Instagram login code. Allow / Deny." The owner taps
   Allow, which starts SMS User Consent for sender `32665`.
4. The node presses Enter. Instagram sends the SMS. Android shows its own "allow agentos
   to read this message" sheet; the owner taps Allow.
5. The app extracts the code with the request's pattern, checks the marker, seals the
   code to the node's key with the request id as associated data, sends it, and drops it
   from memory.
6. The node opens it, checks it against the open request, and hands it to
   `one_time_code`, which runs `decide(..., sink=FORM_OTP)` on the current page and types
   it. The agent gets `{ok: true, step: "code"}`.
7. The trace gets `vault.code_used {source: "phone_sms", phone: "owner-pixel",
   request: "cr_...", consent: "per_request", lane, host}`. The phone's own log gets the
   same, without the code.

If the owner already sees the SMS on an iPhone, the Shortcut path replaces steps 3 to 5:
the automation for sender `32665` matches the code and posts it to the node; the node
accepts it because a matching request is open. If neither is set up, the code card
(phase 1a) asks the owner to type it.

### 3.2 A Facebook "approve on your other device" prompt

Facebook can ask the owner to approve the login on a device where they are already
signed in, instead of a code
([Facebook engineering, 2011](https://engineering.fb.com/2011/05/12/web/introducing-login-approvals/), dated;
current UI per **(secondary)** guides).

1. The lane reaches the "check notifications on another device" page. The site signals
   detect it deterministically (a new signal, `awaiting_device_approval`, from the sites
   and signals package), and `login` returns `{ok: false, needs: "device_approval"}`.
2. The node sends one push: "Facebook asks you to approve a login for biz-a / facebook on
   node home-1. If that was agentos, approve it in the Facebook app on your phone." The
   push never carries an approve link (SEC-8 still holds; this approves nothing in
   agentos).
3. The lane holds, waiting for the page to move on, for up to 5 minutes, with no reload
   and no retry.
4. The owner approves in the Facebook app. The page advances; the next signal check
   reads `ok` and the flow continues. Timeout: `needs: "human"`, the lane is held
   (AUD-07).

**Never automated**, even if the "other device" is a phone lane agentos drives.
Approving a login is the site's own consent step, and an agent that can approve its own
login removes the factor. Where Facebook offers "try another way", a flow may choose the
authenticator code instead, which the vault resolves from TOTP.

### 3.3 A bank-style app-only feature

A supplier portal or bank offers a feature only in its app (approve a payment, download
a statement).

- If the app refuses a debug-enabled phone or shows secure screens (most banks), the
  honest answer is a **human step**: the run parks with a card "Open the bank app on your
  phone and approve the payment of 240 EUR to X. Tap Done when finished." The run
  resumes on Done and checks the real effect where it can (a statement download in the
  browser lane, a balance read). The phone is the owner's own; agentos does not drive it.
- If the app tolerates a phone lane (many business and social apps do), a dedicated
  phone on the home node runs it as an Android lane (section 4b), under the same effect
  classes: `pay` and `irreversible` always need a co-sign.

### 3.4 A VA who needs a code but must never see it

The VA holds a human-lane grant with control of
`biz-a/linkedin`. LinkedIn asks for a code sent to the owner's phone.

1. The VA presses "Get code" in the viewer (or their agent calls `vault_fill` on the
   code field). That makes a code request under the VA's grant; the phone card says "for
   VA Dana on biz-a / linkedin".
2. The owner allows on the phone (or types it into the code card).
3. Before the typist types, the node **freezes the live view for every non-owner
   viewer** and suspends their input. It types the code, presses Enter, waits for
   `settle()`, and only then unfreezes. A one-time code field is usually not masked, so
   without the freeze the VA would read it off the screen.
4. Snapshots never show field values (already done), and `browser_screenshot` is refused
   on that lane while the freeze is on.
5. The VA sees "code entered" in the viewer. The SMS went to the owner's phone, so the VA
   never had it.

## 4. Architecture

### 4a. The phone as a code source

#### Pieces

```
 lane (browser or android)      node                                  phone
 +--------------------+   +-------------------------------+   +---------------------------+
 | login page         |<--| typist <- vault.one_time_code |   | companion app / Shortcut  |
 | code field         |   |             ^                 |   |  request card (consent)   |
 +--------------------+   |   CodeSource "phone"          |   |  SMS User Consent         |
                          |   phone broker (requests,     |<->|  extract code, seal, send |
                          |   rules, phone registry)      |   |  Keystore key             |
                          +-------------------------------+   +---------------------------+
                                       ^  wss (sealed payloads only)  ^
                                       +------ controller (phase 2) --+
```

- **Phone broker** (new, on the node): the phone registry, open code requests, standing
  rules, the phone channel endpoint, and audit. It is a node service like `vault` and
  `approvals`, and it lives in a new package, `agentos.phones`.
- **CodeSource** (vault): a generalization of `CodeCatcher` with three kinds: `imap`
  (exists), `phone` (new), `relay` (the code card, new).
- **Companion app** (Android, new, Kotlin): pairing, request cards, SMS User Consent,
  sealing.
- **Shortcut template** (iOS): a Message automation per sender plus a post token.
- **Code card** (panel): a sealed input for a pending code request.

#### Pairing: the phone dials out like a node, with its own key

Pairing reuses the enrollment shape in docs/PROTOCOL.md, on a separate endpoint so a
phone can never be mistaken for a node.

1. The owner opens Panel, Phones, Pair. The panel shows a QR with a single-use **phone
   join token** (10 minutes), the endpoint URL (`wss://<node tailnet name>/v1/phone/connect`
   in phase 1, the controller in phase 2), and the fingerprint of the node's signing key
   and its **sealing key** (a new X25519 key, `AGENTOS_HOME/secrets/seal.key`, 600).
2. The app generates a P-256 signing key in the Android Keystore (StrongBox when
   present), non-exportable, with user authentication required for signing consents
   (biometric or device credential). P-256 because it is what StrongBox and the iOS
   Secure Enclave support everywhere; Ed25519 in the Keystore needs newer devices.
3. It connects and sends `phone_enroll { join_token, phone_pubkey, name, platform,
   os_version, capabilities }`. The node answers `phone_enrolled { phone_id,
   node_signing_pubkey, node_seal_pubkey }`; the app pins both.
4. Every later connection: `challenge { nonce }` and `phone_hello { phone_id, nonce,
   sig }`, with `sig = p256(phone_key, "agentos-phone-hello:v1:" + nonce + ":" + phone_id)`.
5. **Wake.** Android kills idle sockets, so the app does not hold the socket open. The
   node wakes it with a push that carries only "you have a request" and a request id:
   UnifiedPush through the owner's own ntfy (already in the stack, see ROUTING.md), or
   FCM for the hosted edition. The app then dials in and fetches the request.

Capabilities a phone advertises: `relay` (can show a code card), `sms_consent` (SMS User
Consent), `sms_role` (default SMS app on a dedicated phone), `shortcut` (iOS post token),
later `approver` (section 6, phase 4).

The **iOS Shortcut** has no key store it can use from Shortcuts. It gets a **post
token**: random, 32 bytes, scoped to `phone_id` and to "submit a code", shown once in the
panel as part of a Shortcut install link. It can do nothing but offer a code to an open
request on that one node, it only works on the tailnet address, and it is revocable.
That is weaker than a Keystore key, which is why the iOS path is standing-rule only and
why every post must still match an open request (below).

#### The code request

The vault creates one when a login or fill reaches a point where the entry's code source
is `phone`. It is signed by the node key, so a phone only acts on requests from its own
paired node.

```python
@dataclass(frozen=True)
class CodeRequest:
    id: str                    # "cr_" + ULID, single use
    phone_id: str
    node_id: str
    lane: str                  # "biz-a/instagram"
    site: str                  # registrable domain of the entry, "instagram.com"
    entry_id: str              # opaque id, never the entry's secret fields
    senders: tuple[str, ...]   # normalized: E.164 numbers, short codes, alphanumeric ids
    marker: str | None         # text the message must contain, e.g. "Instagram"
    pattern: str               # code regex, like the IMAP catcher's
    not_before: datetime       # the SMS must arrive at or after this
    expires_at: datetime       # not_before + window (default 5 min, the User Consent window)
    requester: str             # principal id: agent, flow, VA; for the card and the audit
    grant_id: str              # for the audit; the phone shows nothing from the grant
    summary: str               # node-written, never model text
    consent: Literal["per_request", "rule"]
    rule_id: str | None
    sig: str                   # node key over canonical(body without sig)
```

Rules for requests:

- **Grant-scoped.** The request is only created inside `one_time_code`, after
  `ctx.authorize(Effect.SECRET, lane, domain)` passed for that lane and domain, so the
  same grant check that allows typing a password allows asking for a code. No new effect
  class. The phone's consent is an extra gate on top, not a replacement.
- **One open request per (phone, sender set).** Two Instagram accounts on one number
  would otherwise race for one SMS, and Instagram's SMS does not name the account. The
  broker queues the second request until the first closes.
- **Bounded rate.** At most 3 requests per entry per hour and 10 per phone per hour by
  default, so a looping agent cannot spam the owner or the site.
- **No resend.** agentos never presses "resend code" by itself. A resend is a human step
  or a second explicit tool call that makes a new request.

#### Delivery: the code reaches the vault and the typist, never a model

```python
@dataclass(frozen=True)
class CodeOffer:
    request_id: str
    phone_id: str
    received_at: datetime      # when the phone got the SMS
    sender: str                # normalized sender, for the audit
    sealed: bytes              # crypto_box_seal(node_seal_pubkey, code) with AAD below
    sig: str                   # phone key over (request_id, received_at, sender, sha256(sealed))
```

- The phone extracts the code on the phone with the request's pattern and marker. The
  message body never leaves it.
- The code is sealed to the node's X25519 key (libsodium sealed box). The associated
  data is `request_id | lane | site | expires_at`, so a sealed code cannot be replayed
  into another request. In phase 2 the controller routes `CodeOffer` bytes it cannot
  open, which keeps the protocol rule "the controller never holds secrets".
- The node accepts an offer only if: the request is open; the phone is the one it was
  sent to; the signature verifies; `received_at` is within `[not_before - 60 s,
  expires_at]` (the same skew as `extract_code`); the sender is in the request's set.
  Then it closes the request, so a second offer is refused.
- The opened code becomes a `Secret`, goes through `one_time_code`, which runs
  `decide(entry, sink=FORM_OTP, url, field)` against the page as it is now and types it.
  `_remember_code` keeps it for the scrubber, as today.
- The app keeps the code only in memory, never writes it to disk, never puts it in a
  notification (a notification would be readable on the lock screen and by listeners),
  and zeroes its buffer after send as far as the runtime allows.

#### Consent on the phone

| Mode | Where it works | What the owner does | Default |
|---|---|---|---|
| Per request | Android `sms_consent`, the code card on any phone | Taps Allow on the request card (biometric), then Android's own sheet for the one SMS | Yes, for every entry |
| Standing rule | iOS Shortcut, Android `sms_role` (dedicated phone) | Creates a rule in the panel ("codes from 32665 for biz-a/instagram, until 2026-12-31, at most 5 per day"), then confirms it on the phone with biometric | Opt-in per entry |
| Manual (code card) | Any phone or desk | Reads the SMS and types the code into the sealed field | Fallback everywhere |

Standing rules are stored on the node and on the phone, and both check them. The node
refuses a rule the phone never confirmed. A rule names a lane (not a wildcard), a site,
a sender set, an expiry (at most 90 days) and a daily count. Revoking a rule on either
side revokes it on both at the next contact; the node side wins immediately.

**Android per-request details (spike).** SMS User Consent is started by the app, and it
only sees messages that arrive in the next five minutes. That is why the vault arms the
request before it presses Enter on the password, not after the code page appears. Open
points: whether starting consent from a notification tap is reliable under Android's
background activity limits, whether WebOTP-format messages (Android 17 holds them for
any app that is not the domain's) still reach the User Consent prompt, and whether
messages from a sender in the owner's contacts are filtered out (a tutorial says so
**(secondary)**, Google's page does not).

**Android standing details (spike).** On the owner's daily phone there is no stock-OS
route left for unattended reading: listeners lose OTP notifications on Android 15, and
`RECEIVE_SMS`/`READ_SMS` reading is held three hours on Android 17. The exemptions are
the default SMS app and companion-device apps. The companion-device route
(`CompanionDeviceManager` profiles) looks gated by privileged permissions for the
"computer" profile, and the docs disagree on the details
([AssociationRequest](https://developer.android.com/reference/android/companion/AssociationRequest)),
so we do not plan on it. The default-SMS route works only if the companion is a full SMS
app, so it is limited to a **dedicated phone** with the owner's second SIM, where the
companion is installed outside Play and holds the SMS role. Sideloaded apps also face
Android's restricted settings and, on Android 15, enhanced confirmation for sensitive
permissions **(secondary)**
([Android Authority](https://androidauthority.com/android-15-restricted-settings-sideloading-3481098),
[Android Authority](https://androidauthority.com/android-15-enhanced-confirmation-mode-3436697)).

#### iOS: what is possible and what is not

| Route | Possible? | Notes |
|---|---|---|
| An app reading SMS | No | iOS has no SMS read API for third-party apps. |
| Shortcuts personal automation, Message trigger | Yes | Filters on sender (contacts) and "message contains"; all criteria must match. "Run Immediately" since iOS 17, with a notification every time it runs ([Apple](https://support.apple.com/guide/shortcuts/apdd711f9dff/ios), **(secondary)** [Cassinelli](https://matthewcassinelli.com/automations-run-immediately-shortcuts-notifications)). The sender filter takes contacts, so a short code may need saving as a contact, or use "message contains" with the marker (spike). The Shortcut matches the code with a regex action and posts it to the node over HTTPS on the tailnet (Tailscale on the phone). |
| Text Message Forwarding to a Mac | Yes | SMS from the iPhone also appear on a Mac signed into the same Apple ID ([Apple](https://support.apple.com/102545)). A home Mac running the agentos node could read new codes locally. This belongs to the "macOS as a node" bet; reading the Messages store needs Full Disk Access and is not verified here. |
| Message Filter extension | Not used | It only sees messages from unknown senders and is meant for spam filtering; using it to forward codes is outside its purpose and App Review would decide. |
| The code card | Yes | Works today, no setup beyond phone access to the panel (AUD-05). |

The Shortcut cannot do public-key crypto, so its post is protected by TLS on the tailnet
and the post token, not by sealing. It may only post to the node's tailnet address,
never to a public controller URL.

#### Audit

One trace event per code, on the node: `vault.code_requested`, then `vault.code_used`
or `vault.code_declined` or `vault.code_timeout`, each with `request`, `phone`, `lane`,
`host`, `entry`, `requester`, `consent` (`per_request`, `rule:<id>`, `manual`), the
normalized sender and the latency. Never the code, never the message body. The phone
keeps the same entries locally (last 500) and shows them under Activity. The panel shows
codes per lane and per phone. When the signed audit log lands, these
events are chained like every other.

#### Security model

| Threat | What it can do | Mitigation |
|---|---|---|
| Lost or stolen phone (locked) | Nothing new. It receives the site's SMS anyway, which is the site's own factor. | Consents need biometric. Revoke the phone in the panel: its key and rules die on the node at once. Recommend a SIM PIN and, where possible, moving the account to TOTP. Recovery codes stay sealed in the vault. |
| Stolen unlocked phone | Allow a pending request | Requests are node-initiated and short; it cannot create one. A card shows lane and site, and the owner can deny from the panel. |
| Malicious app on the phone reading SMS | Read the same code the site sent | Outside our control; Android 17's hold reduces it. We do not widen it: no code in notifications, logs, clipboard or disk. |
| An app impersonating the companion | Offer fake codes | It does not hold the Keystore key. Offers must be signed by the paired key and match an open request. A wrong code only fails on the real site. |
| Spoofed SMS sender (alphanumeric ids can be spoofed in many countries) | Get a wrong code typed during the window | The wrong code is typed into the real site's field, which rejects it. Nothing leaks. One failure, no retry. |
| Code for the wrong site (a prompt-injected agent tries to use a bank's SMS code on an attacker page) | Phish a code | The request is bound to the entry's site and sender set, so the bank's SMS does not match an Instagram request. The typist refuses any origin that is not the entry's domain (`check_origin`, cross-origin frame rule). The card names the site. |
| Replay of a sealed code or an old SMS | Reuse a code | Single-use requests, AAD binds the ciphertext to one request, `not_before` excludes messages from before the request, offers after close are refused. |
| Compromised controller (phase 2) | Route or drop messages | It sees only sealed bytes and metadata. Requests are signed by the node, offers by the phone; it can forge neither. |
| Compromised node | Ask for codes for its own entries | Consent on the phone shows every request; standing rules are per lane and capped per day. This is the reason per-request is the default. The node already holds passwords and TOTP seeds, so a node compromise is already serious (PASSKEYS.md, custody). |
| Leaked iOS post token | Offer codes to open requests on the tailnet | Needs tailnet access, can only fill an open request, revocable, never reads anything. |
| A VA or viewer reads the code off the screen | See the code | Live view frozen for non-owner viewers during secret typing; screenshots refused during the freeze; snapshot values hidden. |
| SIM swap | Take over the number | Out of scope for agentos; it is why TOTP is preferred and SMS is the fallback. |

#### Failure modes

| Failure | Behavior |
|---|---|
| Phone offline or the wake push fails | After 30 s with no `phone_hello`, the request falls back to the code card if the entry allows it; otherwise `needs: "code"`. No retry against the site. |
| The owner denies | `vault.code_declined`; `login` returns `needs: "human"`, the lane is held. |
| The SMS never arrives | Request expires; `needs: "code"`. No automatic resend. |
| Several SMS in the window | The newest matching message after `not_before` wins; Android's consent sheet is per message, so the owner sees which one. |
| The site rejects the code | One failure is reported as `needs: "human"` with signal; no second attempt with a new request in the same call. |
| Two lanes need codes from one number | Queued per (phone, sender set); the second waits or times out. |
| Phone clock wrong | The node checks `received_at` with 60 s skew and logs the skew; the phone app warns when its clock is off by more than 30 s against `server_time`. |
| Node restarts mid-request | Open requests are in memory only; they die with the process, and late offers are refused. |

### 4b. A real phone as an Android lane

A dedicated Android phone, owned by the owner, plugged into a home node.

- **Transport: USB first.** adb over USB to the home node (a mini PC, a Pi, a Mac). The
  node's adb key is authorized once on the phone. Wireless debugging (Android 11 and
  later) pairs with a six-digit code but needs the same Wi-Fi and turns off when the
  phone changes or drops networks
  ([adb docs](https://developer.android.com/tools/adb)), so it is a fallback for a phone
  that stays on home Wi-Fi, not the default. `adb tcpip` is not used: it resets on reboot.
- **Engine.** A new engine `adb-device` behind the same `AndroidEngine` and
  `AndroidSession` protocols ANDROID.md proposes for redroid. `launch` does not start a
  container; it attaches to the device by serial, runs the self-check, and fails closed
  if the serial or the device's build fingerprint changed (a swapped phone is a new
  identity).
- **Snapshot.** The accessibility tree through uiautomator2, as ANDROID.md section 3,
  with refs and password values redacted. The same `AndroidInteraction` for both engines.
- **Viewer and take over.** scrcpy in an Xvfb slot through `DisplayManager` and the
  existing noVNC relay, the same as redroid. Secure screens show black; the viewer says
  "this screen is protected by the app" instead of a black box, and the human uses the
  phone itself.
- **Secrets.** Typist contract only, never `adb shell input text` (ANDROID.md, secrets).
  The phone lane gets its codes from the same code source: if the SMS arrives on this
  very phone, the companion is installed on it and paired like any phone.
- **Per-app isolation limits.** A phone is one device identity: one ANDROID_ID per
  signing key, one Google account, one IP, one set of sensors. There is no per-lane
  netns or data volume. So: **one phone lane per physical phone, one workspace per phone**,
  and the apps on it are that workspace's apps. A work profile could separate app data,
  but not the device or the IP, so it does not make two brands safe on one phone. Locked
  and Shared presets are refused; only Account.
- **Network path.** The phone uses its own connection, by decision:

  | Option | When it is wanted | Default |
  |---|---|---|
  | The phone's own mobile data | The accounts already live on this phone and SIM (the owner's personal Instagram). A carrier IP is a strong, ordinary identity. | Yes, for a phone with a SIM |
  | Home Wi-Fi | A phone without a SIM, or the owner's home is where those accounts are used | Yes, for a phone without a SIM |
  | The workspace's static proxy through an always-on VPN app | Only if the workspace's accounts have always been used from that proxy's IP and country | No; owner opt-in per phone |

  Never: a phone used as an exit for browser lanes; a phone of one workspace on another
  workspace's proxy. Country, timezone and locale on the phone must agree with its
  network, the same rule as browser lanes.
- **Self-check.** adb reachable, serial and build fingerprint unchanged, screen lock
  state known, target apps at pinned versions, battery above 20 percent and charging,
  network path as configured, logged-in markers from flows.
- **Owner's daily phone: no.** A phone lane needs developer options and USB debugging,
  which breaks banking apps and widens the attack surface. The design assumes a spare
  phone. The owner's daily phone stays a code source only.

### 4c. Redroid lanes, briefly

As docs/ANDROID.md decides: one privileged container per lane in its own network
namespace through the lane's static ISP proxy, on a 16 GB home or server node, 4 to 6
warm. No device verdict, so apps that require it go to a real phone (4b). Redroid has no
SIM: its SMS codes come from the code source in 4a. It shares `AndroidInteraction`, the
viewer path and the lane lifecycle with the `adb-device` engine, so building either one
does most of the other.

## 5. Fit: what changes where

| Package | Change |
|---|---|
| `agentos.vault` | `catcher.py`: `CodeCatcher` becomes `CodeSource` with `arm()` and `wait()` (sketch below). `validate_catcher_config` accepts `kind` in `imap`, `phone`, `relay`, with `phone`, `senders`, `marker`, `pattern`, `window_seconds`, `timeout_seconds`, `fallback` (`relay` or none). `service.py`: `one_time_code` dispatches by kind; `source` in the trace becomes `totp`, `email`, `phone_sms`, `shortcut`, `manual`. `login.py`: arm the code source before pressing Enter on the password (and before the username step for username-only first pages), wait after `settle()`. No new sink: codes still resolve into `FORM_OTP`. |
| `agentos.phones` (new) | Phone registry (SQLite, next to the vault store), pairing and join tokens, the `/v1/phone/connect` endpoint, the request broker (open requests, per-sender queue, rate limits), standing rules, offer verification and unsealing, wake pushes through the notifier, audit events. The node's sealing key. |
| `agentos.policy` | `notifier.py`: event types `code.requested` (urgent: ignores quiet hours, TTL equals the request), `device_approval.waiting`; never a code. Traces: the `vault.code_*` events above. No change to grants or effects. The code card is **not** an approval row: the approvals table stores answers in plaintext, so the code must never go there. |
| `agentos.node` | The viewer relay gets `freeze(lane, reason)` for non-owner viewers, called by the typist adapter around every `type_secret`; `browser_screenshot` refuses during a freeze. Phase 3: `adb-device` engine and `AndroidInteraction` (shared with redroid); `interfaces.py` generalizes `LaneManager.using()` to a handle (lead's sign-off, as ANDROID.md says). Sites and signals: `awaiting_device_approval`. |
| `agentos.core` | `Signal.AWAITING_DEVICE_APPROVAL` (or a `needs` value only; decide with the signals owner). `needs: "code"` and `needs: "device_approval"` in login results. |
| Node protocol | Additive v1 messages for phase 2: node to controller `code_request { request }` and `code_cancel { request_id }`; controller to node `code_offer { offer }` and `code_declined { request_id, reason }`. Capabilities gain nothing (phones are not nodes). The controller never sees a code: offers are sealed to the node. |
| Panel | Phones page (pair by QR, list with last seen and capabilities, revoke), standing rules per lane (create, then confirm on phone), the code card (sealed input, one use, auto-closes), code activity per lane and phone, the frozen-view notice in the viewer, the human-step card for app-only actions (3.3). |
| Controller (phase 2) | Phone enrollment per org, the phone endpoint, routing of requests and sealed offers between a node and the phones its owner paired, wake push relay (FCM for hosted), revocation fan-out. |
| Companion app (new repo or `apps/android`) | Kotlin, minSdk 29. Pairing, Keystore key, UnifiedPush and FCM wake, request cards with biometric, SMS User Consent, sealed send, local activity log. No SMS permissions in the Play build. |

Interface sketches:

```python
class PendingCode(Protocol):
    async def wait(self) -> Secret | None:
        """The code, or None at timeout or denial. Raises CatcherError on transport faults."""
    async def cancel(self) -> None: ...


class CodeSource(Protocol):
    kind: str  # "imap" | "phone" | "relay"

    async def arm(self, ctx: Any, entry: EntryMeta, *, lane_id: str, site: str,
                  not_before: datetime) -> PendingCode:
        """Get ready before the step that makes the site send a code. For imap this only
        records not_before; for phone it creates and sends the signed CodeRequest; for
        relay it opens a code card and pushes the owner."""


class PhoneBroker(Protocol):
    async def request(self, req: CodeRequest) -> PendingCode: ...
    async def offer(self, offer: CodeOffer) -> None:          # from the phone channel
        """Verify, unseal, close the request, resolve its PendingCode. Refuse otherwise."""
    async def manual(self, request_id: str, code: Secret, *, by: Principal) -> None:  # code card
        ...
    def phones(self) -> list[PhoneInfo]: ...
    async def revoke(self, phone_id: str, *, by: str) -> None: ...
```

## 6. Phased plan

Sizes as in ANDROID.md: S under 3 days, M 3 to 7 days, L over 7. Estimates.

| # | Step | Size | Depends on | Acceptance |
|---|---|---|---|---|
| 0 | Spikes on the owner's own test account and phones: (A) SMS User Consent on an Android 16 and an Android 17 phone with a real Instagram SMS: sender match, contact filtering, WebOTP format, start from a notification tap, latency; (B) an iOS 26 Shortcut on a short-code sender: does the trigger fire, sender as contact vs "contains", Run Immediately, post over Tailscale; (C) on a spare Android phone: companion as default SMS app reads an OTP on Android 17 without the hold. | S each | owner's OK and test account | Findings written into this doc; go or no-go per path |
| 1a | Code card (`relay` source) | M | AUD-05 phone access | A login on a fake site with an SMS step completes with a code typed on the phone; the code is in no trace, log, approval row, SSE event or snapshot |
| 1b | Phone channel, registry and broker on the node; `phone` source; arm-before-submit in `login_flow`; audit | M | 1a | Fake-phone tests: pairing, signed requests, sealed offers, replay refused, wrong sender refused, queue per sender, rate limits, revocation |
| 1c | Android companion app with SMS User Consent | L | 1b, spike A | Story 3.1 end to end on a real phone and a test account |
| 1d | iOS Shortcut template and post tokens | S | 1b, spike B | Story 3.1 with an iPhone |
| 1e | Viewer freeze during secret typing for non-owner viewers | S | none (needs the viewer relay) | Story 3.4 on a test lane; a VA viewer never receives a frame with the code |
| 2 | Controller routing for phones, standing rules with phone confirmation, `awaiting_device_approval` and its push, human-step cards | M | controller, 1b | Story 3.2 on a test account; a phone paired to the hosted controller fills a code on a home node |
| 3 | Real phone lane (`adb-device`), shared with the redroid `AndroidInteraction` | L | ANDROID.md steps 1 to 4 (or the shared parts first) | A dedicated phone runs one app-only flow under the viewer, with take over and hand back |
| 4 | Optional: the phone app as an approver with its Keystore key (replaces Review links for owners who pair a phone), and the passkey consent of PASSKEYS.md option A | M | 2 | Approvals signed on the phone; ROUTING.md updated |

### Phase 1 builder briefs

**Brief 1a: the code card.**
> Add a `relay` code source to the vault. Read `src/agentos/vault/catcher.py`,
> `service.py` (`one_time_code`, `_catch`), `login.py` (`login_flow`, `_fill_code`) and
> this doc, sections 4a and 5. Generalize `CodeCatcher` into `CodeSource` with
> `arm()`/`wait()`; keep IMAP behavior identical (its tests must pass unchanged). A
> `relay` entry config `{kind: "relay", window_seconds, timeout_seconds}` makes
> `arm()` create an in-memory pending code (id `cr_<ulid>`, lane, site, requester,
> expiry) and send a notifier event `code.requested` with lane, site and a panel link,
> no code. Add `POST /api/codes/{id}` (owner or a principal with an approver role for
> that workspace, never the requester's own grant family) that accepts the code once,
> as a `Secret`, and resolves the pending wait. The code must never be stored in SQLite,
> traces, SSE, logs or responses; add a test that scans all of them after a run. Panel:
> a code card with a one-time-code input (`autocomplete="one-time-code"`,
> `inputmode="numeric"`), that closes on submit or expiry. Trace `vault.code_requested`
> and `vault.code_used {source: "manual"}`. Update docs/TOOLS.md through `make tools-doc`
> if any tool text changes. `make check` passes. No em dash in any UI string.

**Brief 1b: the phone channel on the node.**
> Create `agentos.phones`: a registry (SQLite under AGENTOS_HOME), a sealing key
> (`secrets/seal.key`, X25519, 600), single-use phone join tokens, and a WebSocket
> endpoint `/v1/phone/connect` on the node's tailnet listener, with enrollment and
> challenge-hello as in this doc, section 4a, mirroring docs/PROTOCOL.md (P-256
> signatures for phones). Implement `CodeRequest`, `CodeOffer` and `PhoneBroker` as
> sketched: node-signed requests, sealed offers with the AAD in section 4a, the
> acceptance checks (open, right phone, signature, time window, sender), single use, one
> open request per (phone, sender set), rate limits, revocation. Add the `phone` code
> source to the vault, and in `login_flow` arm the source before pressing Enter on the
> password. Wake through the notifier (UnifiedPush endpoint stored per phone; carries
> only the request id). Panel: Phones page with a pairing QR and revoke. Tests with a
> fake phone client cover every row of the security table that is testable in process,
> including replay, wrong sender, wrong phone, expired, after close, and revoked phone.
> No real SMS in tests.

**Brief 1c: the Android companion.**
> A Kotlin app (minSdk 29, target the current SDK), no SMS permissions. Pair by scanning
> the panel QR; P-256 key in the Keystore (StrongBox if present), user auth required.
> Wake by UnifiedPush (ntfy) with FCM optional. On a request: verify the node signature
> against the pinned key, show a card (site, lane, requester, expiry), on Allow with
> biometric start SMS User Consent with the request's sender, on the consent result
> extract the code with the pattern and marker, seal it with libsodium to the pinned node
> sealing key with the request AAD, sign and send the offer, then clear it. Never write
> the code or message to disk, logs, clipboard or notifications. Activity screen from a
> local log without codes. Instrumented test with a fake node; a manual test script for
> spike A's phones.

**Brief 1e: freeze the view while a secret is typed.**
> In the viewer relay, add a freeze per lane: while frozen, non-owner viewers get the
> last frame before the freeze with a "a secret is being entered" banner, and their
> input is dropped. The typist adapter freezes before `focus()` of any secret field and
> unfreezes after `settle()` or on abort. `browser_screenshot` is refused for the lane
> while frozen. Test with a fake display that no frame produced during the freeze reaches
> a non-owner stream.

## 7. Risks

| Risk | Why it matters | Mitigation |
|---|---|---|
| Play SMS permission policy | The SMS permission group needs the default handler role or a granted exception, with review ([Play help](https://support.google.com/googleplay/android-developer/answer/10208820)). Cross-device sync is a listed use case, but Google reviews case by case. | The Play build has no SMS permissions and uses SMS User Consent. The SMS-role build for dedicated phones is not on Play. |
| Android 17 OTP hold, Android 15 sensitive notifications | They close unattended reading on the owner's daily phone | Designed around them: per-request consent on the daily phone, standing rules only on a dedicated phone or iOS Shortcut. Re-check with each Android release. |
| Developer verification for sideloaded apps | Mandatory from 30 September 2026 in Brazil, Indonesia, Singapore and Thailand, global from 2027; adb installs stay exempt per Google's FAQ, long term not confirmed **(secondary)** ([Android Authority](https://www.androidauthority.com/android-sideloading-changes-timeline-3679204/), [Android Authority](https://www.androidauthority.com/android-developer-verification-rollout-sideloading-flow-3653395)) | Register the publisher with developer verification before shipping the sideloaded build; adb install for dedicated phones on a home node. |
| Restricted settings for sideloaded apps | Notification listener and accessibility need an extra user step, stricter on Android 15 | We use neither in the companion. |
| iOS Shortcuts behavior changes | The Message trigger, Run Immediately and its notification have changed across releases | The code card is always the fallback. Re-test on each iOS major release. |
| Apple App Review for a future native iOS app | An app forwarding message content would face review | Not in scope; the Shortcut is the owner's own automation. |
| Carrier terms | Some carriers restrict SIMs in automated or machine-to-machine devices. I did not research specific carriers. | The dedicated phone is a normal phone used by its owner; check the carrier's acceptable use terms before putting a SIM in a phone lane. |
| Platform terms | Instagram, Meta, LinkedIn and Reddit restrict automated access (ANDROID.md, risks) | Unchanged by this design. Own accounts, human pace, the owner decides per platform. |
| Concentration | A node that holds passwords, TOTP seeds and a standing SMS rule can complete every factor alone | Per-request consent by default; standing rules capped per day and per lane; TOTP preferred so fewer accounts depend on the phone; break-glass revokes phones too. |
| Banking and secure apps | They refuse debug-enabled phones and block capture | Human-step cards; no workarounds. |
| Wrong account on a shared number | One SMS, two lanes | One open request per sender set, queued. |

## 8. Open questions and recommended defaults

| Question | Recommended default |
|---|---|
| Per-request or standing consent by default? | Per request. Standing only by the owner's choice per lane, confirmed on the phone, at most 90 days. |
| Does the phone send the message or only the code? | Only the code, extracted on the phone. Bodies never leave the phone. |
| Phone to node directly, or through the controller? | Directly on the tailnet in phase 1; through the controller with sealed offers in phase 2. The iOS Shortcut stays tailnet-only. |
| Wake channel? | UnifiedPush through the owner's own ntfy; FCM only for the hosted edition. Payload: request id only. |
| Fallback when the phone is silent? | The code card after 30 s, if the entry allows it (default yes). |
| Who may type into a code card? | The owner and workspace approvers (AUD-11). Never a principal in the requester's grant family, so a VA cannot type a code for themselves after reading it elsewhere. |
| Should agentos ever approve a "log in on another device" prompt? | No, not even on a phone lane it drives. |
| A phone lane's network? | Its own connection (SIM or home Wi-Fi). A workspace proxy only by owner opt-in per phone. Never an exit for browser lanes. |
| Which phone for a phone lane? | A spare phone with the owner's second SIM or none, never the daily phone. |
| Freeze the view for all secrets or only codes? | All secret fields, for non-owner viewers. The owner's own view is not frozen. |
| Rate limits? | 3 code requests per entry per hour, 10 per phone per hour, no automatic resend. |
| Where does the companion live? | `apps/android` in this repo, so its protocol tests run in CI with the node's. |
| First accounts? | A throwaway Instagram test account with SMS 2FA for the spikes; then the owner's own account that has no TOTP option, after the owner's go-ahead. |

## Sources

Android and Google Play
- Android 17 behavior changes, all apps (OTP SMS hold, exemptions, SMS Retriever and User Consent guidance): https://developer.android.com/about/versions/17/behavior-changes-all
- SMS User Consent API: https://developers.google.com/identity/sms-retriever/user-consent/request
- Play Console help, SMS and Call Log permissions and exceptions: https://support.google.com/googleplay/android-developer/answer/10208820
- Play policy, use of SMS or Call Log permission groups: https://support.google.com/googleplay/android-developer/answer/9047303
- CompanionDeviceManager AssociationRequest: https://developer.android.com/reference/android/companion/AssociationRequest
- adb and wireless debugging: https://developer.android.com/tools/adb
- Play Integrity verdicts: https://developer.android.com/google/play/integrity/verdicts
- scrcpy and FLAG_SECURE: https://github.com/Genymobile/scrcpy/commit/1fdde490fd2a0b89680a2b5da5e5274192398023

Apple
- Shortcuts, communication (Message) triggers: https://support.apple.com/guide/shortcuts/apdd711f9dff/ios
- Text Message Forwarding: https://support.apple.com/102545

Sites
- Facebook login approvals (2011 design): https://engineering.fb.com/2011/05/12/web/introducing-login-approvals/

Secondary sources
- Android 15 sensitive notifications: https://www.androidauthority.com/android-15-sensitive-notifications-3416414 ; https://androidauthority.com/android-15-two-factor-authentication-codes-3492585
- Android restricted settings and enhanced confirmation: https://androidauthority.com/android-15-restricted-settings-sideloading-3481098 ; https://androidauthority.com/android-15-enhanced-confirmation-mode-3436697 ; https://discuss.grapheneos.org/d/825-restricted-setting
- Android developer verification timeline: https://www.androidauthority.com/android-sideloading-changes-timeline-3679204/ ; https://www.androidauthority.com/android-developer-verification-rollout-sideloading-flow-3653395
- Shortcuts Run Immediately in iOS 17: https://matthewcassinelli.com/automations-run-immediately-shortcuts-notifications
- Banking apps blocking developer options and accessibility: https://help.gxbank.my/Personal%2F%2Fsecurity-and-privacy/Fraud_Prevention_and_Security/Why_can't_I_access_the_GX_Bank_app%3F_I_see_a_%22Your_device_may_have_been_modified%22_screen. ; https://www.muamalat.com.my/services/business/app-restricted/
