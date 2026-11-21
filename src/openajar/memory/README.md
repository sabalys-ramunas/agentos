# openajar.memory

Workspace memory with provenance: one store per workspace of facts, documents, decisions,
preferences, contacts and run summaries. Every version says who wrote it, under which grant,
from which traces, and how far it can be trusted. The design is
`docs/design/workspace-memory.md`; this package holds its Brief A (store and service) and
Brief C (MCP tools, the owner's CLI, run summaries, export and import). HTTP routes and the panel
are phase 2.

The one rule: **trust is computed by the node, never claimed by the writer, and it travels
with the data.** Reading an entry adds its labels to the reader's grant family.

## Files

| File | What |
|---|---|
| `models.py` | entry types and their attributes, `Trust`, `Caller`, `MemoryScope`, `WriteRequest`, `Limits` (from `$OPENAJAR_HOME/memory.toml`) |
| `store.py` | one SQLite file per workspace (`$OPENAJAR_HOME/memory/<ws>.db`, WAL, 600): append-only versions, the hash chain, blobs, FTS5 |
| `service.py` | `MemoryService` (runtime service `memory`), `build(runtime)`, `SERVICE_NAME` |
| `ports.py` | what the service needs from policy, traces, vault, brains and workspaces, and adapters over the runtime |
| `scan.py` | secrets refused on write and scrubbed on read; the suspicious heuristic |
| `errors.py` | refusals with stable codes |
| `fakes.py` | `fake_memory(home)`: the service over a fake policy, vault and privacy, for tests and `openajar dev up` |
| `tools.py` | the MCP tools `memory_context`, `memory_search`, `memory_get`, `memory_write`, `memory_forget` (tag `memory`) |
| `cli.py` | `openajar memory`: list, show, search, write, confirm, review, forget, verify, stats, export, import (the owner in person) |

## Trust and labels

| Trust | When |
|---|---|
| `owner` | `Caller.owner()` (the owner in person: signed-in panel or node CLI), or a caller carrying the owner's approval for this exact write |
| `node` | `Caller.node()` writing structured records (`run_summary`) |
| `agent` | any other caller whose family has never seen `untrusted` (the `ever` ledger), with no untrusted evidence or `derived_from` source |
| `untrusted` | everything else |

A version's labels (`untrusted`, `private`) are the writer family's `ever` labels plus those of
its evidence and `derived_from` sources, plus `private` for contacts and for every entry of a
`local-only` workspace. Owner-trust versions drop `untrusted` and keep `private`. Reads return
`MemoryRead(data, taint)`; the service has already added `taint` to the reader's family through
the policy port. Title-only results (suspicious notes, private entries the reader may not see
whole) add nothing until the body is fetched.

## Refusals

`secret_in_memory`, `bad_evidence`, `conflict` (with the current `id@version`), `too_large`,
`rate_limited`, `trust_too_low`, `type_not_allowed`, `privacy`, `out_of_scope`, `not_found`,
`invalid`, `unavailable` (a workspace file that cannot be read; never silent), and
`owner_trust_required` (internal: the tools layer turns it into an approval).

## For the tools, API and CLI (Brief C)

```python
from openajar.memory import Caller, MemoryScope, SERVICE_NAME
from openajar.memory.errors import OwnerTrustRequired

memory = ctx.runtime.require(SERVICE_NAME)
scope = MemoryScope.from_cap(grant.specials.memory, lanes=grant_lane_patterns)   # Brief B's MemoryCap
caller = Caller.from_context(ctx, scope, cloud=True)       # the owner in person becomes Caller.owner()

# reads: the result, then the labels (already added; ctx.taint is idempotent)
read = memory.context(caller, ws, task=task, budget_tokens=budget)
ctx.taint(*read.taint)
return read.data

# memory_write: plan, authorize, store
plan = memory.plan_write(caller, ws, args)
facts = {"internal": True}
if plan.needs_owner_trust:
    facts["memory_owner_trust"] = True
d = await ctx.authorize(Effect.DRAFT, content=plan.canonical if plan.needs_owner_trust else None,
                        summary=plan.summary, facts=facts)
if plan.needs_owner_trust:
    caller = caller.with_approval(d.via, plan.digest)
result = memory.write(caller, ws, args)

# memory_forget: always the owner's co-sign
p = memory.plan_forget(caller, entry_id, reason)
d = await ctx.authorize(Effect.IRREVERSIBLE, content=p["canonical"], summary=p["summary"], facts={"internal": True})
memory.forget(caller.with_approval(d.via, p["digest"]), entry_id, reason)
```

Other methods: `get(caller, id, version=, history=, as_of=, workspace=)`, `search(caller, ws,
query, types=, trust_min=, since=, until=, subject=, writer=, lane=, limit=,
include_expired=)`, `list_entries(caller, ws, types=, trust_min=, states=, since=,
unreviewed=, limit=)`, `confirm(owner, id, version, edit=)`, `review(owner, id)`,
`run_summary(record)` (runner and brains), `verify(ws)`, `expire()`, `stats(ws)`,
`decisions_for_lane(ws, lane)`. `ports.on_event(kind, payload)` receives `memory_write`,
`memory_confirm`, `memory_forget` and `memory_flagged` with ids and hashes, never content.

## Ports (Brief B fills the policy side)

`PolicyPort` in `ports.py`: `taint_ever(family)`, `taint_add(family, labels)`,
`trace_visible(trace_id, family, workspace)`, `trace_snapshot(trace_id)`,
`lane_url_at(trace_id)`, and optionally `record_receipt(family, workspace, digest)`.
`RuntimePolicyPort` calls `policy.taint_ever`, `policy.add_labels`, `policy.record_receipt`,
`traces.visible` and `traces.lane_url_at` when they exist, and falls back to today's
`policy.taint` ledger and `traces.get` until they do. `decisions_digest(ws)` is the getter
for `decisions_loaded` on commit traces.

## Tools, CLI, runner and export (Brief C)

- **MCP tools** (`tools.py`): tagged `memory`, so policy hides them from grants without the
  `memory` special and hides `memory_write` and `memory_forget` from read-only memory. The
  caller's scope comes from `policy.memory_rules.caller(ctx, ws, ...)`; the owner in person is
  `Caller.owner()`. Reads call `ctx.taint(*read.taint)`. `memory_write` authorizes `draft` with
  `{"internal": True}` and the canonical entry as content; a decision or a supersede of an owner
  entry adds `memory_owner_trust`, and only an `approved` route stores owner trust
  (`confirmed_by` the approval id). `memory_forget` authorizes `irreversible` and forgets only
  with an approval. Writes and forgets leave a `memory_write` or `memory_forget` trace.
- **Pins**: an owner-trust or decision version pins every evidence trace any version of the
  entry cites (`policy.memory_rules.pin`); forget and retention unpin.
- **Context receipts**: `context` records the decisions digest through the port; `build` wires
  `memory_rules.set_decisions_digest(service.loaded_digest)`, so commit traces carry
  `decisions_loaded` (None when the workspace has no decisions).
- **CLI** (`cli.py`, mounted as `openajar memory`): `Caller.owner(session="cli")` over the node's
  files, with the policy (evidence checks, pins) and the vault (scrub) when the node has them.
  `export --md DIR` writes one Markdown file per type with provenance footnotes; `--jsonl FILE`
  every record. Both are plaintext, mode 600. `import` takes Markdown (one owner document) or
  jsonl (one entry a line).
- **Run summaries**: `openajar.runner.memory_summary` builds the record from the run (status,
  node-observed proof URLs and readback pass or fail, cost, content digest, the shown params of
  an approved run) and the runner calls `run_summary` whenever a run reaches a final state. A
  later status (a re-check that verifies the run) is a new version of the same entry.
- **Export and import** (`export_records`, `import_records`, `drop_workspace`): the records of
  `workspace/memory.jsonl` in workspace export format 2 (`openajar.workspaces.transfer.export`). The incoming
  chain is checked before anything is written; under the same id every row is copied as it was
  (the hashes are the source's, `imported_from` comes from the file's import record); under
  `--as` keys, decision scopes, lanes and lane subjects move to the new id and the chain is
  rebuilt, each version keeping `imported_from.version_hash`. An import never merges and an
  import that does not verify is removed again.
