"""Regenerate vectors.json (only when the format changes): uv run python tests/fixtures/audit/make_vectors.py

The key is fixed (32 bytes of 0x01..0x20) so signatures are reproducible; Ed25519 is deterministic.
The 2026-11-04 additions (`typed`, `stored`) left every earlier vector byte for byte as it was:
an honest record's canonical bytes did not change.
"""

from __future__ import annotations

import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentos.policy.auditlog import (
    LANE_CTX,
    LINK_CTX,
    MARK_CTX,
    ROW_CTX,
    SIG_CTX,
    canonical,
    lane_tag,
    mark_hash,
    pub_text,
    real,
    row_digest,
    row_hash,
    row_record,
    sign,
)
from agentos.policy.tokens import key_id
from agentos.policy.trace_writes import INT64_MAX
from agentos.policy.traces import TraceEvent, TraceStore

KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
KID = key_id(KEY)


def main() -> None:
    genesis = {"v": 1, "kind": "genesis", "chain": "ch_testvectorchain00", "n": 1, "prev": None,
               "at": "2026-11-03T00:00:00Z", "kid": KID, "pub": pub_text(KEY), "after_seq": 2, "unchained": 2}
    g_hash = mark_hash(genesis)
    base = {"principal": "claude-code", "principal_kind": "agent", "grant_id": "gr_abc", "grant_path": "/gr_root/gr_abc/",
            "workspace": "biz-a", "cost_tokens": 0, "cost_usd": 0.0, "duration_ms": None, "run_id": None,
            "parent_id": None, "error": None}
    rows = [
        {**base, "seq": 3, "id": "tr_aaaaaaaaaaaaaaaa", "at": 1793750400.25, "kind": "tool_start", "tool": "browser_act",
         "lane": "biz-a/instagram", "effect": "commit", "status": "started", "summary": None,
         "args": '{"on": "biz-a/instagram", "ref": "e12"}'},
        {**base, "seq": 4, "id": "tr_bbbbbbbbbbbbbbbb", "at": 1793750401.0, "kind": "tool_end", "tool": "browser_act",
         "lane": "biz-a/instagram", "effect": "commit", "status": "ok",
         "summary": "Posted “hello” à la page \"biz-a\"\n\ttab\\slash \u0001ctl \U0001F600",
         "args": None, "cost_tokens": 1200, "cost_usd": 1e-05, "duration_ms": 750, "parent_id": "tr_aaaaaaaaaaaaaaaa"},
        {**base, "seq": 5, "id": "tr_cccccccccccccccc", "at": 1793750402.5, "kind": "panel", "tool": "panel.approve",
         "lane": None, "workspace": None, "effect": None, "status": "denied", "summary": "[redacted]",
         "grant_id": "owner", "grant_path": "/owner/", "principal": "owner", "principal_kind": "human",
         "args": '{"password": "[redacted]"}'},
    ]
    prev = g_hash
    out_rows = []
    for r in rows:
        h = row_hash(prev, r)
        out_rows.append({"row": r, "canonical": canonical(row_record(r)).decode(), "digest": row_digest(r),
                         "lane_tag": lane_tag(r["lane"], r["id"]), "prev": prev, "hash": h})
        prev = h
    checkpoint = {"v": 1, "kind": "checkpoint", "chain": genesis["chain"], "n": 2, "prev": g_hash,
                  "at": "2026-11-03T00:10:00Z", "kid": KID, "seq": 5, "hash": prev, "count": 3, "reason": "rows"}
    marks = []
    for body in (genesis, checkpoint):
        data = canonical(body)
        marks.append({"body": body, "canonical": data.decode(), "hash": mark_hash(body), "sig": sign(KEY, SIG_CTX, data)})
    # values of a type their column never holds when agentos wrote them: a one-key object
    typed_cases = [("cost_usd", "Infinity", "text"), ("cost_usd", "0.00001", "text"), ("cost_tokens", True, "bool"),
                   ("cost_tokens", 1.0, "real"), ("summary", 7, "int"), ("summary", b"A", "blob")]
    typed = []
    for col, value, kind in typed_cases:
        r = {**rows[1], col: value}
        typed.append({"column": col, "type": kind, "value": value.hex() if isinstance(value, bytes) else value,
                      "record_value": row_record(r)[col], "digest": row_digest(r)})
    # what the write path stores for values SQLite or UTF-8 cannot hold (AUDIT-LOG.md "Write path")
    stored = []
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        store = TraceStore(Path(tmp) / "traces.db", clock=lambda: 1793750400.0)
        for i, (given, fields) in enumerate([
            ('{"lane": "biz-a/x\\ud800", "summary": "a\\udc00b"}', {"lane": "biz-a/x\ufffd", "summary": "a\ufffdb"}),
            ('{"cost": {"tokens": 9223372036854775808}, "duration_ms": -9223372036854775809}',
             {"cost_tokens": INT64_MAX, "duration_ms": -INT64_MAX - 1}),
            ('{"cost": {"usd": -0.0}}', {"cost_usd": 0.0}),
            ('{"cost": {"usd": NaN}}', {"cost_usd": 0.0}),
        ]):
            ev = TraceEvent(**{"id": f"tr_stored{i:011d}", "at": 1793750400.5, "kind": "tool_end",
                               "tool": "browser_act", "status": "ok", **json.loads(given)})
            store.write(ev)
            row = store.row(ev.id)
            row = {k: v for k, v in row.items() if k not in ("prev", "hash")}
            assert all(row[k] == v for k, v in fields.items()), (row, fields)
            stored.append({"given": given, "stored": fields, "row": {**row, "seq": 1},
                           "digest": row_digest({**row, "seq": 1})})
        store.close()
    vectors = {
        "contexts": {k: v.decode() for k, v in (("row", ROW_CTX), ("lane", LANE_CTX), ("link", LINK_CTX),
                                                 ("mark", MARK_CTX), ("sig", SIG_CTX))},
        "key": {"seed_hex": bytes(range(1, 33)).hex(), "kid": KID, "pub": pub_text(KEY)},
        "reals": [[x, real(x)] for x in (1793750400.25, 0.0, 1e-05, 1.5e20, 0.1, 42.0, -3.75)],
        "rows": out_rows,
        "marks": marks,
        "typed": typed,
        "stored": stored,
    }
    path = Path(__file__).with_name("vectors.json")
    path.write_text(json.dumps(vectors, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
