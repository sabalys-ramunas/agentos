"""Signed agent identity for browser lanes: Web Bot Auth (HTTP Message Signatures, RFC 9421, Ed25519).

Phase 1 of docs/design/agent-identity.md: the signing core, off by default, nothing published.

- `sf`        the RFC 8941 structured fields these headers use
- `base`      signature bases, `Signature-Input`, JWK thumbprints (RFC 7638, RFC 8037), the signer
- `verify`    a strict verifier (protocol draft section 5.2), for tests and `openajar identity test-sign`
- `keys`      one Ed25519 key per lane under `secrets/webbotauth/` (mode 600, refused when readable)
- `settings`  the lane's `sign_hosts` list (empty by default, owner-only) and its signature agent URL
- `route`     the engine's signing route, installed only when `sign_hosts` is non-empty, and the
              redirect guard
- `trace`     the per-lane identity trace (never the `Signature` header)
- `cli`       `openajar identity init | show | hosts | test-sign`

Not named `identity` so it is not confused with `node/channel/identity.py` (the node's own key).
Playwright and Camoufox are never imported here: the route receives Playwright objects.
"""
