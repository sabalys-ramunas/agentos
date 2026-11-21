# Passkeys in the vault

Research note, 2026-11-04. Research only: nothing ran on the node and no code changed. Items marked **(secondary)** come from a blog, an aggregator or a search summary and need a primary check before anyone relies on them.

## Recommendation

The owner's idea is right about the sites and overstated about custody.

- **Why it is worth doing:** a passkey is origin-bound and leaves no reusable secret on the server. Google, Facebook, LinkedIn and Reddit accept one today. Instagram and Medium show no passkey support in the sources found.
- **Where it overstates:** a passkey held on the node is a copy of a private key that can sign in alone. The vault already holds a password and a TOTP seed for the same accounts, and those are copies too. Phone consent stops the node from signing without asking, but it does not stop someone who copies the sealed store. Only a key that never leaves the phone or a hardware key removes that custody risk.

**Build now (small, reversible, needs the owner's go-ahead for the node):**

1. A time-boxed spike on a throwaway account. It answers three questions: does Playwright 1.62's `browser_context.credentials` complete `navigator.credentials.create()` and `get()` inside Camoufox; does the Firefox under Camoufox also take the Marionette WebAuthn route; and what lands on disk in the lane profile when a credential exists. No root, no kernel changes.
2. Two owner rules, no code. Keep the password as the first factor on every account where a node-held passkey could exist (on Google, turn off "Skip password when possible"). Treat the mailbox as the real root: email is the recovery path for several sites and the code catcher reads it.

**Build later (only if the spike passes):** a sealed `passkey` vault entry, signing gated by a one-off phone approval, with the authenticator window open only while that approval is live. Details in the design section below.

**Later, optional:** a hardware FIDO2 key (YubiKey or similar) as a second factor on the owner's highest-value accounts, touched by the owner. A cloud lane cannot use a key plugged into the owner's home. That needs USB/IP, which is unverified.

**Avoid:**

- `security.webauth.webauthn_enable_softtoken`. Mozilla's bug describes it as an internal soft token "mostly for testing".
- A software CTAP2 authenticator over `/dev/uhid` on the node, for now. It needs root and a kernel HID device, rests on community code, and is untested with Camoufox.
- A browser extension that wraps `navigator.credentials`. Keys sit in the profile, and the wrapper is easy for a page to detect.
- Hybrid (QR) passkeys on Linux lanes. Firefox does not implement caBLE there.
- Persisting Playwright storage state that includes credentials. The keys would land in the lane profile as a JSON file.
- Passkey-only sign-in on any account.

## 1. How a Playwright-driven browser presents a passkey

**Premise check.** The Chrome DevTools WebAuthn domain is Chromium-only and marked experimental ([CDP WebAuthn](https://chromedevtools.github.io/devtools-protocol/tot/WebAuthn)). That part of the owner's note holds. It is out of date for Playwright: release 1.61 added a browser-context virtual authenticator, `browserContext.credentials`, and storage state can carry the credentials ([Playwright 1.61.0 release](https://github.com/microsoft/playwright/releases/tag/v1.61.0)). That release says the API "works in all browsers". The BrowserContext docs do not state browser support, so whether it runs in Camoufox's Firefox is the open point.

| Route | Status in Firefox 158 / Camoufox | What the node needs | Detectability and risk |
|---|---|---|---|
| Playwright `browser_context.credentials` (1.61+, repo pins 1.62) | The 1.61 release notes say it "works in all browsers"; the BrowserContext docs do not say. Untested on Camoufox. | Nothing new, no root. | Keys live in the browser context. Storage state can write them to disk, and restoring a state that holds credentials installs the authenticator and blocks real ones. |
| Firefox Marionette WebAuthn virtual authenticator | Shipped in Firefox 118 per [Bug 1846574](https://bugzilla.mozilla.org/show_bug.cgi?id=1846574) ("firefox118 fixed"). The bug and the [Perl Marionette module](https://metacpan.org/pod/Firefox::Marionette::WebAuthn::Authenticator) do not say whether release builds enable it without a pref. | A Marionette port on localhost and a second client next to the Playwright connection. Camoufox's README does not mention Marionette. | Marionette is the WebDriver stack, and WebDriver state can be visible to pages. Untested on Camoufox. |
| `security.webauth.webauthn_enable_softtoken` | An internal U2F soft token, "mostly for testing" ([Bug 1529973](https://bugzilla.mozilla.org/show_bug.cgi?id=1529973)). Current default not stated. | None. | Not a supported product. Do not use. |
| CTAP2 software authenticator over uhid | The [fido2-hid-bridge README](https://github.com/BryanJacobs/fido2-hid-bridge) names Firefox as a USB-HID application, so USB HID keys reach it on Linux (not checked against Mozilla's docs). Software options are community projects: [soft-fido2](https://lib.rs/crates/soft-fido2) (pure software; license unclear across sources) and [fidorium](https://docs.rs/crate/fidorium/latest/source/README.md) (TPM 2.0 backed). fido2-hid-bridge is not software: it relays to a physical PC/SC key. Firefox testing not stated. | Root or a udev group on `/dev/uhid`, the uhid module, and a VM that exposes it. | Looks like a USB HID device. Kernel-level and fragile. Untested. |
| Browser extension wrapping `navigator.credentials` | Not tested. | Extension install by policy. | Keys in the profile. My assessment, not tested: page-side checks on native functions can expose a wrapper. |

**Camoufox specifics.** Camoufox's [Python README](https://github.com/daijro/camoufox/blob/main/python/README.md) documents a `firefox_user_prefs` launch option, so WebAuthn prefs can be set at launch. Its README does not name the Firefox base it is built on. The repo's fingerprint code targets Firefox 158 (`src/openajar/node/fingerprint.py`, `tests/unit/test_ops_fingerprint.py`), which is the expected base. I did not check the build the node has installed.

## 2. Storage and consent

**Storage.** The private key lives only in the sealed vault, as ARCHITECTURE section 6 describes for every secret: a sealed store (libsodium/age), unsealed by a node key that is Touch ID or hardware backed. For each approved use, the node loads the key into the lane's authenticator for one window and removes it after. It never goes into the lane profile or any storage state, never reaches the model, and the typist never types it, because a passkey is not a form field.

**Signing.** A page calls `navigator.credentials.get()`. The authenticator in the lane signs the challenge with the loaded key. The node records the rp_id, lane, approval id and time. It records neither the key nor the challenge body.

**Consent, compared:**

| Option | How it works | Key custody | Unattended? | On the node's Linux lanes? |
|---|---|---|---|---|
| A. Phone approval before the window (recommended) | The existing approvals queue issues a one-off or standing approval (count 1, 5 minutes, bound to lane and rp_id). The notifier pushes it; the phone approves through a signed link; the window opens. | Node, sealed | Yes | Yes |
| B. Phone as the authenticator via hybrid QR, human watching the live view | The site shows a QR code; the phone's own passkey signs after its biometric check. | Phone secure hardware | No: a human at every sign-in | No. Firefox on Linux has no hybrid transport ([Bug 1543674](https://bugzilla.mozilla.org/show_bug.cgi?id=1543674), still open). Mozilla says hybrid works on recent Windows and macOS. |
| C. Approval in the panel | As A, answered in the panel. | Node, sealed | Only when someone is at a desk | Yes |

The real choice is A against B. B gives the owner what the idea wants: the key never sits on the node, and consent is biometric on the phone. It needs a human at the live view for each sign-in and a Mac or Windows lane host. A keeps lanes unattended, at the price of node custody.

Under A, the approval cannot bind to the challenge. The challenge only exists once the page calls `get()`, which happens after the approval is asked for. So A binds to the lane, the site and a short window, and the node records the challenge outside the approval. Whether the window can hold `get()` open until the phone answers decides whether the approval can bind to the challenge too. That is an open question below.

## 3. Site reality

| Site | Passkeys, 2026 | Add one without removing the password? | What matters for the node |
|---|---|---|---|
| Google | Yes ([Google Account help](https://support.google.com/accounts/answer/13548313?hl=en)). | Yes. Adding a passkey does not change or remove other sign-in or recovery factors. | Creating a passkey opts into passkey-first sign-in, with "Skip password when possible" on by default. Turn that off to keep the password first. Workspace accounts may not allow passkey-only sign-in. |
| Facebook (Meta) | Yes on the Facebook app since June 2025 (Messenger was promised; status not checked). Create it in Accounts Center under Password and security ([Facebook help](https://www.facebook.com/help/1181045243159511)). | Yes. Passwords and 2FA still work. | The help page lists setup on Windows 10, macOS Ventura, ChromeOS 109 or later, or a FIDO2 hardware key. Linux is not listed, so a Linux lane may not be offered the option. The passkey takes the owner's email as its name. Removing it returns the account to other methods. |
| Instagram | No passkey article in Instagram's help center results. 2025 coverage says Instagram has no passkey option ([search summary](https://mezha.net/eng/news/bf2895e9_security_site_shames/)). | n/a | Treat as no passkey. 2FA is an authenticator app or SMS. |
| LinkedIn | Yes. Official help: "Set up and use a passkey for signing in" ([LinkedIn help](https://www.linkedin.com/help/linkedin/answer/a1621596)). Up to five passkeys per account, created under Settings, Sign in and security, Passkeys. Cross-device setup uses a QR code. | Yes (per the help page). | A vendor post (Corbado, March 2026) describes the rollout as partial. Check the account's own settings page. |
| Reddit | Yes, optional ([Reddit help](https://support.reddithelp.com/hc/en-us/articles/49063461551252-What-are-passkeys-and-how-do-I-set-it-up)). | Yes. Reddit does not allow passkey-only accounts. | Passkeys can sync through iCloud Keychain or Google Password Manager. Reddit offers one at sign-up and when it asks for human verification. Removable from settings. |
| Medium | No passkey in the help pages found. Sign-in is an email link or code, or a linked Google or Apple account, and there is no password ([Medium help](https://help.medium.com/hc/en-us/articles/115004914788-About-passwords)). The pages are old. | n/a | Email is the factor. The node's code catcher reads mailboxes, so the mailbox is the real root for this account. |

**Account recovery.** On every site above, a passkey does not replace the password or 2FA. A lost node-held passkey therefore matters only where the password is gone, which is exactly the setting to avoid. Recover through the password, TOTP or backup codes, and keep backup codes in the sealed vault.

**Sync and export.** A passkey created on the node stays on the node unless it is exported. The FIDO Credential Exchange Format 1.0 was published as a Proposed Standard in August 2025, with an errata in March 2026 ([CXF 1.0 errata](https://fidoalliance.org/specs/cx/cxf-v1.0-ps-errata-20260309.pdf)). Shipping support is uneven. Apple's iOS 26 implements the transfer protocol and 1Password exports on iOS (**secondary**, from vendor and employee posts). Android status is contested: Bitwarden says it shipped ([Bitwarden](https://bitwarden.com/blog/security-vendors-join-forces-to-make-passkeys-more-portable-for-everyone/)), while a June 2026 Medium post says Google has not implemented it (**secondary**). Bitwarden also says passkeys that use a signature counter cannot transfer through that protocol (**secondary**). Plan: the vault exports CXF so the owner can move credentials, and the password and TOTP stay as the fallback, so the owner is not locked in.

## 4. Hardware-backed 2FA, TOTP and passkeys

- **YubiKey at the lane host.** The key needs a touch per sign-in, which is a strong consent. A cloud VM cannot take a USB key, and USB/IP over the mesh is possible in principle but unverified. A key at the owner's home works only if the lane runs there.
- **Hardware key support on the sites.** Facebook documents FIDO2 security keys ([security keys on Facebook](https://www.facebook.com/help/401566786855239)). Google accepts a security key for 2-Step Verification ([Google help](https://support.google.com/accounts/answer/6103523?hl=en&co=GENIE.Platform%3DDesktop)).
- **TOTP is already built** (`src/openajar/vault/totp.py`). A seed on the node is a second factor only, and a real-time reverse proxy can phish the code. It works on Instagram, which has no passkey.
- **Verdict for the owner's case.** Passkeys beat TOTP against phishing and against a site breach. A node-held passkey does not beat a node-held TOTP seed on custody, and on a site that allows passkey-only sign-in it is a single factor. So: TOTP for sites without passkeys (Instagram), passkeys only as a second factor on the others, and a touched hardware key for the highest-value accounts if the owner can reach one.

## Proposed design

**Vault entry.** A new `EntryKind.PASSKEY` in `src/openajar/vault/models.py`, next to `PASSWORD`, `TOTP`, `COOKIE`, `API_KEY` and `NOTE`. Fields: `rp_id`, `credential_id`, `user_handle`, a display name, `sign_count`, the lane binding, `created_via` (node or phone), `last_used`, and the private key (PKCS#8) in the sealed store. `EntryMeta` exposes only `rp_id`, the lane and dates, never the key.

**Policy.** A new `Sink.WEBAUTHN` in `src/openajar/vault/policy.py`, with reasons `RP_MISMATCH` and `APPROVAL_REQUIRED`. The rp_id must match the registrable domain of the top origin, the same check `check_origin` already makes for passwords, and the lane must be bound.

**Signing path.** The lane reaches its login step. The node asks for a standing approval (count 1, 5 minutes, lane and rp_id). The phone approves. The node loads the credential into the lane's authenticator (Playwright's `credentials` API, or Marionette's add-credential command if the spike favours it). Playwright's `credentials.create()` takes the private key directly (1.61 release notes), so the seed comes from the sealed entry and never from a profile file; the W3C automation extension defines the same add and get commands ([WebAuthn L3, User Agent Automation](https://www.w3.org/TR/webauthn-3/)). The page calls `get()`, the browser signs, and the node removes the credential, records the sign count and writes a trace line with rp_id, lane, approval id and time. The window closes after one use or at the TTL. A denial or timeout fails the login with a clear reason.

**Components that change:**

- **vault**: `models.py` (kind), the sealed store (private key), a CXF exporter, `policy.py` (sink, reasons, rp_id check), `api.py` (add and remove routes; never return the key).
- **node**: the Camoufox engine (`src/openajar/node/camoufox/engine.py`) opens and closes the authenticator window and sets WebAuthn prefs through `firefox_user_prefs`. The typist does not change: a passkey is not a field.
- **approvals**: a new kind `passkey_sign_in` in `src/openajar/policy/approvals.py`, with a content hash over lane, rp_id, credential reference and TTL.
- **notifier**: `src/openajar/policy/notifier.py` pushes the site and lane name with the signed approve link, never key material.
- **panel**: passkeys per lane (rp_id, created, last used, approvals), with revoke. Revoke removes the entry and reminds the owner to remove it on the site.
- **fingerprint**: a virtual authenticator can change what `PublicKeyCredential` reports. Re-test the pinned fingerprint before enabling the window (`src/openajar/node/fingerprint.py`).

## Risks to the owner's accounts

1. **Node theft or a leaked sealed store is a passkey theft.** On Google with "Skip password when possible" on, the passkey alone can sign in. Keep that setting off, keep the passkey as a second factor only, keep the store sealed with hardware unseal.
2. **A new sign-in method is a security event.** Sites may email the owner, lock the account or ask for re-verification. Meta, LinkedIn and Reddit are strict about automated accounts, and a passkey does not change that. Have the owner add any passkey while watching the live view, not from a scheduled job, and verify the real effect afterwards.
3. **Lost vault, lost passkey.** Covered by the backup in `deploy/README.md`. Keep backup codes sealed, and never remove the password on a passkey site.
4. **Software authenticators are visible.** There is no hardware attestation, and the authenticator's identity can show in the attestation data. Consumer sites usually accept an unattested credential, but this is unverified per site. Test on a throwaway account first.
5. **The mailbox is the root.** Medium signs in by email, and the code catcher reads mail. A passkey does not fix a single-factor mailbox.
6. **Terms.** Meta, LinkedIn and Reddit restrict automated accounts. That is the owner's call, and this design does not change it.
7. **API churn.** `browserContext.credentials` is new (1.61). The sign count is exposed in the 1.64 notes, after the repo's 1.62 pin, so the pin may have to move. Pin it and test each bump.

## Open questions

1. Does Playwright's `browser_context.credentials` run inside Camoufox? The release notes say all browsers, the docs are silent. Settle it in the spike.
1a. Does the pinned 1.62 return the sign count and private keys from `credentials`? The sign count is in the 1.64 notes, and the repo pins `playwright>=1.62,<1.63`.
2. Can a `get()` be held open until the phone answers? If yes, consent can bind to the challenge.
3. Does the node's Camoufox expose Marionette? Does attaching a second client change `navigator.webdriver` or the pinned fingerprint?
4. Does Facebook offer passkey creation on a Linux lane? Its help page lists other platforms.
5. Which Firefox base does the node's Camoufox build use? The repo targets 158; the README does not say.
6. Which accounts go first, and does the owner accept a node-held credential for any of them, or only a phone or hardware key?

## Sources

- Playwright 1.61.0 release: https://github.com/microsoft/playwright/releases/tag/v1.61.0
- Chrome DevTools WebAuthn domain: https://chromedevtools.github.io/devtools-protocol/tot/WebAuthn
- W3C WebAuthn Level 3, User Agent Automation (Add Credential, Get Credentials): https://www.w3.org/TR/webauthn-3/
- Firefox Marionette virtual authenticator, Bug 1846574: https://bugzilla.mozilla.org/show_bug.cgi?id=1846574
- Firefox soft token, Bug 1529973: https://bugzilla.mozilla.org/show_bug.cgi?id=1529973
- Firefox hybrid transport on Linux, Bug 1543674: https://bugzilla.mozilla.org/show_bug.cgi?id=1543674
- Perl Marionette WebAuthn module: https://metacpan.org/pod/Firefox::Marionette::WebAuthn::Authenticator
- Camoufox Python README: https://github.com/daijro/camoufox/blob/main/python/README.md
- fido2-hid-bridge: https://github.com/BryanJacobs/fido2-hid-bridge
- soft-fido2: https://lib.rs/crates/soft-fido2
- fidorium: https://docs.rs/crate/fidorium/latest/source/README.md
- Google, passkeys: https://support.google.com/accounts/answer/13548313?hl=en
- Facebook, create a passkey: https://www.facebook.com/help/1181045243159511
- LinkedIn, passkeys: https://www.linkedin.com/help/linkedin/answer/a1621596
- Reddit, passkeys: https://support.reddithelp.com/hc/en-us/articles/49063461551252-What-are-passkeys-and-how-do-I-set-it-up
- Medium, passwords: https://help.medium.com/hc/en-us/articles/115004914788-About-passwords
- FIDO CXF 1.0 errata: https://fidoalliance.org/specs/cx/cxf-v1.0-ps-errata-20260309.pdf
- Bitwarden on passkey portability: https://bitwarden.com/blog/security-vendors-join-forces-to-make-passkeys-more-portable-for-everyone/
