# The tamper-evident action log (format 1)

The action log (`$OPENAJAR_HOME/traces.db`, table `traces`) is append-only and redacted at write
time. Format 1 makes it tamper-evident: every row is hashed exactly as stored and linked to the
row before it, and signed marks vouch for the chain head at regular points. Code:
`openajar.policy.auditlog` (format, verifier) and `openajar.policy.traces` (write path). Test
vectors: `tests/fixtures/audit/vectors.json` (regenerate with `make_vectors.py` next to it only
when the format changes).

Revision 2026-11-04 (still format 1): values of a type a column never holds are typed objects
(see "The row record"), the write path gives every value a storable form ("Write path"),
retention marks count cumulatively, and verify, export and share links were corrected. An
honest record's canonical bytes did not change: every row and mark a node wrote before verifies
as it did, and the earlier vectors are unchanged byte for byte.

## What it proves, and what it does not

- **Proves:** a row covered by a checkpoint was not edited, deleted, inserted or moved since
  that checkpoint was signed; the rows between two checkpoints are complete; a gap made by
  retention is lawful (a signed "pruned through" mark) and a gap made any other way is not.
- **Reports, but cannot prove:** rows written after the latest checkpoint. Someone with write
  access to `traces.db` can remove them without a trace; verify names them "unprovable since
  checkpoint X". Checkpoints are written every 1,000 rows, every 10 minutes while rows keep
  coming, at shutdown, on every export, and on `openajar audit checkpoint`.
- **Does not protect against** someone holding the node's audit private key: they can sign a
  rewritten history. What protects against that is a copy of a checkpoint that left the node:
  an export bundle an auditor kept, the anchors the controller and an Object Lock bucket hold
  (`openajar audit verify --anchors`, `openajar.policy.anchors`), and
  pinning the genesis key id (`openajar audit verify --trust-key`).
- **Never hashes anything new.** The row record is the stored, redacted row. No secret, cookie
  or page text that was not already in the row enters a hash, and verifying needs no secret.

## The row record

A row's record is a JSON object of its stored columns plus the format version:

| key | from column | JSON value |
|---|---|---|
| `v` | (none) | `1` |
| `seq`, `cost_tokens`, `duration_ms` | INTEGER | number, or `null` |
| `at`, `cost_usd` | REAL | string, see "REAL values" |
| `id`, `kind`, `principal`, `principal_kind`, `grant_id`, `grant_path`, `tool`, `lane`, `workspace`, `effect`, `status`, `summary`, `args`, `error`, `run_id`, `parent_id` | TEXT | string, or `null` |
| any column holding a value of another type | (any) | a typed object, see "Typed values" |

`args` is hashed as the stored text (the JSON the store wrote), never re-parsed. `prev` and
`hash` are not part of the record.

**Canonical bytes**: the record serialized as JSON with keys sorted by code point, no
whitespace, UTF-8, strings escaped as RFC 8785 does (`\"`, `\\`, `\b`, `\f`, `\n`, `\r`, `\t`,
every other control character below U+0020 as `\u00xx` in lowercase hex, everything else
literal, including non-ASCII).

**REAL values** are written as a JSON string holding the shortest decimal that reads back as the
same IEEE-754 double, in positional notation (never an exponent), with at least one digit on
each side of the point: `1793750400.25`, `0.0`, `0.00001`, `150000000000000000000.0`. NaN and the
infinities, which the store never writes, are `"NaN"`, `"Infinity"`, `"-Infinity"`. A value in a
REAL column that SQLite returns as an integer is written the same way (`42` becomes `"42.0"`).
`-0.0` is written `"-0.0"`; the store never writes it (see "Write path").

**Typed values.** openajar stores only text in TEXT columns, integers in INTEGER columns and
numbers in REAL columns, and a bundle carries them as JSON strings and numbers. Any other value
(a BLOB, a JSON boolean, text in a REAL or INTEGER column, a REAL in an INTEGER column, a number
in a TEXT column) is written as a JSON object with one key naming its type, so it can never
equal an honest value, which is always a string, a number or `null`:

| value | written as |
|---|---|
| a boolean (JSON `true`, `false`) | `{"bool":true}` |
| an integer where none belongs | `{"int":"7"}` (decimal text) |
| a REAL where none belongs | `{"real":"1.0"}` (as in "REAL values") |
| text where none belongs (`'Infinity'` in a REAL column) | `{"text":"Infinity"}` |
| a BLOB | `{"blob":"41"}` (lowercase hex) |
| anything else | `{"other":"<type>:<text>"}` |

So REAL `inf` (`"Infinity"`) and TEXT `'Infinity'` in the same column, `1e-05` and the string
`"0.00001"`, `1` and `true`, and the text `"\u0000blob:41"` and the BLOB `x'41'` all have different
digests. Vectors: `typed` in vectors.json.
## Hashes

All hashes are SHA-256, written as lowercase hex. Each hash input starts with a context string
ending in a NUL byte:

```
digest   = hex(SHA256("agentos.audit.row.v1\0"  || canonical(record)))
lane_tag = hex(SHA256("agentos.audit.lane.v1\0" || utf8(lane or "") || "\0" || utf8(id)))
hash     = "sha256:" || hex(SHA256("agentos.audit.link.v1\0" || ascii(prev) || "\0"
                                   || ascii(digest) || "\0" || ascii(lane_tag)))
```

`prev` is the previous row's `hash` (the full `sha256:...` string); the first row after the
genesis uses the genesis mark's hash. Rows are chained in `seq` order, and `seq` is part of the
record, so moving a row changes its digest.

`lane_tag` lets an export leave a row out (as a stub: seq, id, prev, digest, lane_tag, hash)
while still proving the row was not from the exported lane: the verifier recomputes the tag of
the exported lane for the stub's id and refuses a match. The id is random, so the tag says
nothing about which lane the row belongs to.

## Marks

Marks live in `audit_marks (n, kind, body, hash, sig)`, append-only (triggers refuse UPDATE and
DELETE). `body` is the canonical JSON of the mark; `hash` is
`"sha256:" || hex(SHA256("agentos.audit.mark.v1\0" || body))`; `sig` is base64url (no
padding) of the Ed25519 signature over `"agentos.audit.sig.v1\0" || body`. Every body has:

| key | meaning |
|---|---|
| `v` | 1 |
| `kind` | `genesis`, `checkpoint`, `pruned` or `key` |
| `chain` | the chain id, chosen at genesis (`ch_...`) |
| `n` | the mark's number: 1, 2, 3, ... with no gaps |
| `prev` | the previous mark's hash (`null` for the genesis) |
| `at` | when it was written (ISO 8601, UTC) |
| `kid` | the key that signs it: `ed25519:` and the first 16 bytes of SHA-256 over the raw public key, in hex |

By kind:

- **genesis** (always mark 1): `pub` (the first audit public key, raw 32 bytes in base64url),
  `after_seq` and `unchained`. A log made before format 1 keeps its rows: they get no hash, and
  the genesis says they end at `after_seq` (the highest seq used, from the table or SQLite's
  counter) and how many there were. They are reported as "written before the chain began", never
  chained after the fact. Self-signed.
- **checkpoint**: `seq` and `hash` (the chain head), `count` (rows chained since the genesis:
  `seq - after_seq`), `reason` (`rows`, `time`, `shutdown`, `export`, `manual`).
- **pruned**: written by retention in the same transaction as the delete. `through_seq` and
  `through_hash` (the last row removed and its hash), `deleted`, `cutoff`, and `kept`: every row
  at or below `through_seq` that is still there (pinned as memory evidence), as `[seq, hash]`.
  Retention removes a prefix of the log by seq; the next row's `prev` is `through_hash`.
  `through_seq` never moves back: a purge that only removes rows an earlier mark kept (their pin
  was released) repeats that mark's `through_seq` and `through_hash` with the shorter `kept`.
  Marks count cumulatively: a verifier takes the highest `through_seq` of all of them (with its
  hash), and a mark below the one before it (written by a node before 2026-11-04) keeps the
  earlier mark's kept rows above its own `through_seq`.
- **key**: `next_kid`, `next_pub` (the new audit key), `reset`. Signed by the key it replaces
  (`kid` is the old key), so the chain of keys is endorsed key by key back to the genesis. With
  `reset: true` (the old key was lost) it is signed by the new key only, and verify reports
  every later checkpoint as only as good as that reset, unless the new key is trusted.

The active key at mark n is the genesis key, then each key mark's `next_kid` in turn. Every mark
must be signed by the key active at that point; a mark signed by any other key is "the key was
swapped".

## Keys and trust

The audit key is its own Ed25519 key in `secrets/audit-<kid hex>.key` (mode 600), never the
grant signing key. Only the active private key is kept; after a rotation the old private key is
deleted and its public half stays in the log. `secrets/audit-trust.json` records the chain id and
every audit public key this node introduced; the node's own verify refuses a log whose genesis
key is not in it. Rotation: `openajar audit rotate-key` (or `--reset`).

## Verifying

Rows are read in seq order. For each row: recompute `hash` from its record and stored `prev`;
check `prev` equals the previous row's stored hash; check `seq` follows on. Findings, one
sentence each, with seq numbers:

| case | finding | level |
|---|---|---|
| content changed, hash not recomputed | row seq N was edited after it was written | tampered |
| content changed, hash recomputed | row seq N+1 does not link to row seq N: row N was rewritten or replaced | tampered |
| a row deleted from the middle | row seq N is missing from the middle of the log | tampered |
| rows swapped | rows seq A and B are out of chain order | tampered |
| rows deleted from the end | the log has used seq up to M (SQLite's counter), or a checkpoint covers seq M, but it now ends at L | tampered |
| a checkpoint whose head differs from the row | checkpoint n vouches for row seq S with another hash | tampered |
| an edited, missing or replaced mark | mark n was edited / marks a-b are missing / mark n does not follow mark n-1 | tampered |
| a forged signature | mark n has a signature that does not verify | tampered |
| a key not introduced by the chain | mark n is signed by key X, but the audit key at that point was Y: the key was swapped | tampered |
| a genesis key this node never trusted | the log starts from audit key X, which this node never trusted | tampered |
| a public key that does not decode | mark n carries an unreadable public key: it was edited | tampered |
| a row below a retention mark it did not keep (chained or not), or a kept row changed or missing | ... retention mark n ... | tampered |
| more rows from before the chain than the genesis counted | the genesis counted N rows written before the chain began, but M are there | tampered |
| rows after the latest checkpoint | rows seq A-B are unprovable since checkpoint n | unprovable |
| rows from before the chain | rows seq A-B were written before the chain began | unprovable |
| an unsigned mark, a key reset | mark n is unsigned / mark n reset the audit key | unprovable |
| rows below a retention mark that cannot be trusted (unsigned) | retention mark n cannot be trusted, so rows before seq S are not checked | unprovable |

A retention mark that cannot be trusted says nothing about the rows below it, so they are
unprovable, never tampered for its sake: a kept row is not missing, and a row it did not list is
not "added". What a signed mark before it proved still holds: a row below that mark's
`through_seq` that it did not keep is tampered, and a row whose contents no longer match its own
hash is tampered either way.

`openajar audit verify` exits 0 when everything in scope is proven, 3 when anything was tampered
with, 4 when something in scope cannot be proven, 1 when it could not run. `--since` starts the
walk at the last row before that time, so rows deleted at the start of the window (and a window
with no rows at all) are judged against the row the window follows; with no row before it the
walk starts at the genesis. The walk always runs to the end of the log. `--lane` limits which
rows count as in scope for "unprovable"; tampering in the rows walked is always reported.

## Export bundles

`openajar audit export --day D [--lane L] -o file` writes a zip:

- `rows.jsonl`: every row from the day's first seq to the checkpoint that covers its last one
  (export writes one first). Rows of the day and lane in full (plus the events traced inside
  those calls); rows of the lane from outside the day in that range in full as well, out of the
  bundle's scope (`rows.context` in the manifest), because a stub of the lane could not be told
  from a row of the day that was left out; every other row as a stub. A stub whose `lane_tag` is
  the exported lane's is always tampered.
- `marks.jsonl`: every mark, with its body, hash and signature.
- `files/<run>/<name>.png`: proof screenshots of the runs those rows belong to.
- `manifest.json`: scope (day, timezone, lane, Unix since and until), row and mark counts with
  the SHA-256 of `rows.jsonl` and `marks.jsonl`, the audit public keys, the runs with their proof
  of done, and every file with its SHA-256.
- `manifest.sig`: `{"kid", "sig"}`, Ed25519 by the active audit key over
  `"agentos.audit.bundle.v1\0" || manifest.json`.

`openajar audit verify --bundle file` needs nothing but the file. Rows are proven by the chain
and the checkpoint that covers them; the screenshots and run proofs are vouched for by the
export's signature only (they are not in the chain). Without `--trust-key`, the bundle's own
genesis key is trusted and the report says so, so the auditor compares that key id with the
owner's record.

## Share links

A share link names one trace or one lane-day. Its token is
`ash_<base64url(payload)>.<kid hex>.<base64url(sig)>` where the payload is the canonical JSON of
`{v, sid, kind, target, exp, screenshots}` and the signature is the audit key's over
`"agentos.audit.share.v1\0" || payload`. The share's row in `audit.db` makes it revocable;
expiry is the earlier of the payload's `exp` and the row's. A lane-day share is refused when it
is made for a lane the replay would refuse (`workspace/name`). Every view that shows something
is written to the action log (kind `share_view`) and counted on the share.

## Write path

A row gets its seq, prev and hash only when it is inserted, inside one `BEGIN IMMEDIATE`
transaction that reads the chain head and inserts the waiting rows in order, so two writers
(the daemon and a CLI) never fork the chain. Rows that wait (a locked database) stay unchained
in memory and, past 10,000, in the spill file. The spill file is deleted only after its rows are
committed (then renamed and read once more, for rows another process appended meanwhile), and a
replay skips rows already in the log by id, so a crash between spill and flush neither loses nor
repeats a row and never forks the chain (a row skipped that way takes no seq, so SQLite's counter
stays at the head). A line of the spill file that broke off mid-write is set aside on its own in a
`.torn` file; every complete line is still restored.
Writing still never fails a tool call, and the log never stops recording:

- **Storable values.** Before a row is chained, each TEXT value has every lone surrogate
  (U+D800 to U+DFFF, which UTF-8 cannot encode) replaced by U+FFFD; each INTEGER value is
  clamped to the signed 64-bit range (`2^63` is stored as `9223372036854775807`); a REAL that
  is not finite is stored as `0.0` (`at`: the write time), and `-0.0` as `0.0`. `args` is the
  JSON text the store wrote (non-ASCII escaped), so it is always storable. The row is hashed as
  it is then stored. Vectors: `stored` in vectors.json.
- **One row never blocks the rest.** A row SQLite still refuses is stored as a stub in its place
  (its id, time and kind, status `error`, summary `[trace could not be stored]`), chained like
  any row, so the rows behind it are written.
- **A seq is never given twice.** The next row's seq is above both the chain head and SQLite's
  counter (`sqlite_sequence`), even after retention emptied the table and the store was opened
  again; a gap this leaves is reported by verify, never filled.
