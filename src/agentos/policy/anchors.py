"""Audit anchors: checkpoint and retention heads kept somewhere the node cannot rewrite, and the
check that the node's log still matches every one of them.

The node signs its own checkpoints with a key under its own user, so whoever controls that
user can rewrite history and sign it again: `verify` alone then says "ok". An anchor is the
head a mark signed (chain id, mark number, seq, the 32-byte row hash at that seq, key id),
and nothing else: no row content ever leaves the node. Two kinds of mark are anchored:

- a checkpoint: the chain head it signed;
- a retention mark ("pruned", kind "pruned" in the anchor): the "pruned through seq N, hash
  H" it signed. The controller stamps the time it received it, so a retention mark dated later
  than it was written, or one held back, no longer looks like lawful retention.

Once the controller (or an S3 bucket with object lock) holds an anchor, a rewritten, truncated
or restarted log no longer matches it, and `agentos audit verify --anchors` says so. See
docs/PROTOCOL.md, `audit_anchor`.
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from agentos.policy.auditlog import Finding, Verifier, read_marks

ANCHOR_V = 1
RECEIPT_KIND = "audit_anchor_receipt"  # what a controller signs each stored anchor as (wire.sign_controller)
KINDS = ("checkpoint", "pruned")  # the marks that are anchored: what the log covers, and what it let go
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
RETENTION_FLOOR_H = 23  # retention keeps rows at least a day (trace_retention_days >= 1), less an hour for clocks


def kind_of(a: Mapping[str, Any]) -> str:
    """An anchor's mark kind. Anchors from before retention anchors carry none: checkpoints."""
    return str(a.get("kind") or "checkpoint")


def anchor_of(body: Mapping[str, Any]) -> dict:
    """The anchor for one checkpoint or retention mark body: the head it signed, no content.
    A checkpoint's anchor has no kind field, so it is the same object it always was."""
    if body.get("kind") == "pruned":
        return {"v": ANCHOR_V, "kind": "pruned", "chain": str(body.get("chain") or ""), "n": int(body["n"]),
                "seq": int(body["through_seq"]), "hash": str(body["through_hash"]), "kid": body.get("kid"),
                "at": body.get("at")}
    return {"v": ANCHOR_V, "chain": str(body.get("chain") or ""), "n": int(body["n"]), "seq": int(body["seq"]),
            "hash": str(body["hash"]), "kid": body.get("kid"), "at": body.get("at")}


def valid(a: Any) -> str | None:
    """Why `a` is not a well-formed anchor, or None when it is."""
    if not isinstance(a, Mapping):
        return "an anchor is an object"
    if not isinstance(a.get("chain"), str) or not a["chain"] or len(a["chain"]) > 128:
        return "chain is a non-empty string"
    for k in ("n", "seq"):
        if not isinstance(a.get(k), int) or isinstance(a.get(k), bool) or a[k] < 1:
            return f"{k} is a positive integer"
    if not isinstance(a.get("hash"), str) or not _HASH.match(a["hash"]):
        return "hash is sha256: and 64 lowercase hex characters"
    if a.get("kid") is not None and (not isinstance(a["kid"], str) or len(a["kid"]) > 80):
        return "kid is a short string"
    if a.get("at") is not None and (not isinstance(a["at"], str) or len(a["at"]) > 40):
        return "at is an ISO time"
    if a.get("kind") is not None and a["kind"] not in KINDS:
        return f"kind is one of {', '.join(KINDS)}"
    return None


def core(a: Mapping[str, Any]) -> dict:
    """The fields of an anchor a receipt vouches for. A checkpoint's has no kind, so receipts
    signed before retention anchors still verify."""
    out = {k: a.get(k) for k in ("v", "chain", "n", "seq", "hash", "kid", "at")}
    if kind_of(a) != "checkpoint":
        out["kind"] = kind_of(a)
    return out


def _mark_bodies(conn: sqlite3.Connection) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for m in read_marks(conn):
        try:
            out[int(m["n"])] = {**json.loads(m["body"]), "_kind": m["kind"]}
        except (ValueError, TypeError):
            out[int(m["n"])] = {"_kind": m["kind"], "_unreadable": True}
    return out


def _ts(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _head_of(m: Mapping[str, Any]) -> tuple[Any, Any]:
    """The (seq, hash) a mark body signed: a checkpoint's head, a retention mark's "through"."""
    if m.get("_kind") == "pruned":
        return m.get("through_seq"), m.get("through_hash")
    return m.get("seq"), m.get("hash")


def _matches(a: Mapping[str, Any], m: Mapping[str, Any], kind: str) -> bool:
    seq, h = _head_of(m)
    return m.get("_kind") == kind and seq == a["seq"] and h == a["hash"] and m.get("chain") == a["chain"] \
        and (a.get("kid") is None or m.get("kid") == a.get("kid")) and (a.get("at") is None or m.get("at") == a.get("at"))


def _dedupe(anchors: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Each head once (the controller and S3 may both hold it), keeping the copy with the earliest
    time a target received it: that is the proof of when the mark existed."""
    unique: dict[tuple, Mapping[str, Any]] = {}
    for a in anchors:
        if valid(a) is not None:
            continue
        key = (a["chain"], a["n"], kind_of(a), a["seq"], a["hash"], a.get("kid"), a.get("at"))
        held = unique.get(key)
        t, t_held = _ts(a.get("received_at")), _ts(held.get("received_at")) if held is not None else None
        if held is None or (t is not None and (t_held is None or t < t_held)):
            unique[key] = a
    return sorted(unique.values(), key=lambda a: (a["chain"], a["n"]))


def check(conn: sqlite3.Connection, anchors: Iterable[Mapping[str, Any]], *, trusted: set[str] | None = None,
          lock: Any = None) -> tuple[list[Finding], dict]:
    """Compare the node's log with anchors held elsewhere. Returns (findings, summary): one
    "tampered" finding per way the log no longer matches, "unprovable" when the newest marks are
    not anchored yet or a retention mark cannot show it removed only old rows, and a summary with
    how many anchors matched."""
    anchors = _dedupe(anchors)
    with lock or contextlib.nullcontext():
        marks = _mark_bodies(conn)
        try:
            last_seq = conn.execute("SELECT MAX(seq) FROM traces").fetchone()[0]
        except sqlite3.OperationalError:
            last_seq = None

        def row_hash_at(seq: int) -> tuple[bool, str | None]:
            try:
                r = conn.execute("SELECT hash FROM traces WHERE seq = ?", (seq,)).fetchone()
            except sqlite3.OperationalError:
                return False, None
            return (r is not None), (r[0] if r else None)

        raw = read_marks(conn)
    v = Verifier(raw, trusted=trusted, trust_note=False)
    chain = v.chain or (marks.get(1) or {}).get("chain")
    through = int(v.prune["through_seq"]) if v.prune else 0
    through_hash = v.prune.get("through_hash") if v.prune else None
    findings: list[Finding] = []
    matched = 0
    newest: dict | None = None
    foreign: dict[str, Mapping[str, Any]] = {}
    held_cps: list[Mapping[str, Any]] = []        # checkpoint anchors that match the log
    held_prunes: dict[int, Mapping[str, Any]] = {}  # retention anchors that match the log, by mark
    broken: set[int] = set()                      # marks an anchor already calls rewritten or gone
    for a in anchors:
        if a["chain"] != chain:
            foreign[a["chain"]] = a  # sorted by n, so the newest anchor of that chain wins
            continue
        kind = kind_of(a)
        n, seq, h = a["n"], a["seq"], a["hash"]
        m = marks.get(n)
        what = "checkpoint" if kind == "checkpoint" else "retention mark"
        where = f"anchor of {what} {n} (seq {seq}, {a.get('at') or 'time unknown'})"
        if m is None:
            broken.add(n)
            top = max(marks) if marks else 0
            if n > top:
                findings.append(Finding("tampered", f"the log's marks end at mark {top}, but the {where} is held "
                                                    "off the node: the log was cut back after it was anchored",
                                        mark=n))
            else:
                findings.append(Finding("tampered", f"mark {n} is gone, but the {where} is held off the node: "
                                                    "checkpoints were deleted", mark=n))
            continue
        if "kind" not in a and m.get("_kind") == "pruned" and _matches(a, m, "pruned") \
                and _not_a_checkpoint(a, anchors, marks):
            # a retention anchor stored by a controller older than retention anchors: it kept the head
            # and its time, not the kind. It cannot have been a checkpoint: an earlier checkpoint was
            # already at or past its seq, and each checkpoint signs a newer head than the last
            held_prunes[n] = a
            matched += 1
            findings.append(Finding("note", f"the anchor of retention mark {n} does not say it is a retention mark "
                                            "(the controller that took it was older than retention anchors); it "
                                            "cannot be a checkpoint, since an earlier checkpoint already reached "
                                            f"seq {seq}", mark=n))
            continue
        if not _matches(a, m, kind):
            broken.add(n)
            now = "a retention mark" if m.get("_kind") == "pruned" else f"a {m.get('_kind')} mark"
            mseq, _ = _head_of(m)
            text = (f"mark {n} no longer matches the {where}: it is now {now} for seq {mseq} with another hash, "
                    "key or time, so history was rewritten and signed again")
            findings.append(Finding("tampered", text, first=seq, mark=n))
            continue
        if kind == "pruned":
            held_prunes[n] = a
            matched += 1
            continue
        present, stored = row_hash_at(seq)
        if present:
            if stored != h:
                findings.append(Finding("tampered", f"row seq {seq} no longer has the hash the {where} holds: the "
                                                    "log was rewritten after it was anchored", first=seq, mark=n))
                continue
        elif seq > max(last_seq or 0, through):
            end = max(last_seq or 0, through)
            findings.append(Finding("tampered", f"the log ends at seq {end}, but the {where} covers rows through "
                                                f"seq {seq}: rows were deleted from the end after it was anchored",
                                    first=end + 1, last=seq, mark=n))
            continue
        elif seq > through:
            findings.append(Finding("tampered", f"row seq {seq} is gone, though no signed retention mark removed it "
                                                f"and the {where} is held off the node", first=seq, mark=n))
            continue
        elif seq == through and through_hash is not None and through_hash != h:
            findings.append(Finding("tampered", f"the retention mark says the log was pruned through seq {seq} "
                                                f"with another hash than the {where} holds", first=seq, mark=n))
            continue
        held_cps.append(a)
        matched += 1
        if newest is None or seq > newest["seq"]:
            newest = {"mark": n, "seq": seq, "at": a.get("at")}
    findings += _retention(marks, chain, anchors, held_cps, held_prunes, broken)
    for other, a in foreign.items():
        if chain is None:
            text = (f"this log has no chain, but chain {other} was anchored through seq {a['seq']} "
                    f"(checkpoint {a['n']}, {a.get('at') or 'time unknown'}): the log was deleted or reset")
        else:
            text = (f"this log is chain {chain}, but chain {other} was anchored through seq {a['seq']} "
                    f"(checkpoint {a['n']}, {a.get('at') or 'time unknown'}): the log was reset and started again")
        findings.append(Finding("tampered", text, mark=a["n"]))
    cps = [b for b in marks.values() if b.get("_kind") == "checkpoint" and isinstance(b.get("seq"), int)]
    later = [b for b in cps if newest is None or b["seq"] > newest["seq"]]
    if later and not any(f.level == "tampered" for f in findings):
        lo = min(int(b["n"]) for b in later)
        hi = max(int(b["n"]) for b in later)
        span = f"checkpoint {lo}" if lo == hi else f"checkpoints {lo}-{hi}"
        findings.append(Finding("unprovable", f"{span} {'is' if lo == hi else 'are'} not anchored off the node yet, "
                                              "so rows after the last anchor are only as safe as the node's own key",
                                mark=lo))
    summary = {"anchors": len(anchors), "matched": matched, "anchored_through": newest, "chain": chain,
               "retention_anchored": len(held_prunes)}
    return findings, summary


def _not_a_checkpoint(a: Mapping[str, Any], anchors: list[Mapping[str, Any]],
                      marks: Mapping[int, Mapping[str, Any]]) -> bool:
    """Each checkpoint signs a newer head than the one before it, so a head at or below an earlier
    checkpoint's seq (held off the node, or in the log) cannot be a checkpoint's."""
    if any(b["chain"] == a["chain"] and b["n"] < a["n"] and kind_of(b) == "checkpoint" and b["seq"] >= a["seq"]
           for b in anchors):
        return True
    return any(k < a["n"] and m.get("_kind") == "checkpoint" and isinstance(m.get("seq"), int) and m["seq"] >= a["seq"]
               for k, m in marks.items())


def _retention(marks: Mapping[int, Mapping[str, Any]], chain: str | None, anchors: list[Mapping[str, Any]],
               held_cps: list[Mapping[str, Any]], held_prunes: Mapping[int, Mapping[str, Any]],
               broken: set[int]) -> list[Finding]:
    """Every retention mark in the log must be anchored, and must have come at least a day after
    the youngest row it removed was written, by the earliest time anything held off the node
    shows the mark existed.

    A mark existed by the time a target received its own anchor, or the anchor of any later
    mark (marks are numbered in order). The youngest row it removed (seq N) was written after
    the newest anchored checkpoint before N was signed; with no such checkpoint, the anchored
    checkpoint that covers N stands in for it (an honest node checkpoints within minutes). Only
    checkpoints written before the mark count. Marks older than every anchor held (an older
    controller that hands back only its newest heads) are not judged: nothing held reaches them."""
    mine = [a for a in anchors if a["chain"] == chain]
    received = sorted((int(a["n"]), t) for a in mine if (t := _ts(a.get("received_at"))) is not None)
    cps = sorted(held_cps, key=lambda a: a["seq"])
    top_held = max((int(a["n"]) for a in mine), default=0)
    low_held = min((int(a["n"]) for a in mine), default=0)
    out: list[Finding] = []
    floor = 0
    for n in sorted(marks):
        m = marks[n]
        if m.get("_kind") != "pruned" or m.get("_unreadable"):
            continue
        try:
            through = int(m.get("through_seq") or 0)
        except (TypeError, ValueError):
            continue
        forward, floor = through > floor, max(floor, through)
        if n in broken or n < low_held or m.get("through_hash") is None:
            continue  # called out already, older than what is held, or only rows from before the chain
        by = [t for k, t in received if k >= n]
        node_t = _ts(m.get("at"))
        times = [t for t in (node_t, min(by) if by else None) if t is not None]
        when = min(times) if times else None
        before = [c for c in cps if c["seq"] < through and c["n"] < n]
        covers = [c for c in cps if c["seq"] >= through and c["n"] < n]
        ref = before[-1] if before else (covers[0] if covers else None)
        ref_t = (_ts(ref.get("at")) or _ts(ref.get("received_at"))) if ref is not None else None
        text = None
        if forward and ref is None and mine:
            text = (f"retention mark {n} removed rows through seq {through}, but no anchored checkpoint before it "
                    "covers those rows, so nothing held off the node shows they were old: they may have been "
                    "removed to hide them")
        elif forward and ref_t is not None and (when is None or (when - ref_t) / 3600 < RETENTION_FLOOR_H):
            if when is None:
                said = "it carries no time a verifier can read, and nothing held off the node dates it"
            else:
                src = "by when it was received off the node" if by and when == min(by) and when != node_t \
                    else "by its own time"
                gap = (when - ref_t) / 3600
                after = "less than an hour" if gap < 1.5 else f"{gap:.0f} hours"
                said = f"it came {after} after checkpoint {ref['n']} (seq {ref['seq']}) {src}"
            text = (f"retention mark {n} removed rows through seq {through}, but {said}, and retention keeps rows "
                    "at least a day: they may have been removed to hide them")
        elif n not in held_prunes:
            if n > top_held:
                text = (f"retention mark {n} (rows through seq {through}) is not anchored off the node yet, so "
                        "nothing held elsewhere shows when it removed those rows")
            else:
                text = (f"retention mark {n} (rows through seq {through}) was never anchored, though later marks "
                        "were: nothing held elsewhere shows when it removed those rows, so they may have been "
                        "removed to hide them")
        if text:
            out.append(Finding("unprovable", text, first=through, mark=n))
    return out


def receipt_ok(receipt: Any, *, controller_pubkey: str, node_id: str) -> bool:
    """A controller's receipt for one anchor, checked against the key this node pinned."""
    from agentos.node.channel.identity import verify_controller

    return isinstance(receipt, Mapping) and isinstance(receipt.get("anchor"), Mapping) and verify_controller(
        controller_pubkey, RECEIPT_KIND, node_id, dict(receipt))


def from_document(doc: Any, *, controller_pubkey: str = "", node_id: str = "", source: str = "") -> tuple[list[dict], int]:
    """Anchors from a file: a controller export (`agentos-controller anchors NODE`), a list of
    receipts, or a list of bare anchors (an S3 copy). Receipts are kept only when their signature
    verifies with the controller key (with no key, none is). Bare anchors carry no signature: each
    one kept is marked `unsigned`, and the caller says so. Returns (anchors, how many were left out)."""
    items = doc.get("receipts", doc.get("anchors")) if isinstance(doc, Mapping) else doc
    out: list[dict] = []
    dropped = 0
    for item in items if isinstance(items, list) else []:
        if isinstance(item, Mapping) and "anchor" in item:
            if not controller_pubkey or not receipt_ok(item, controller_pubkey=controller_pubkey, node_id=node_id):
                dropped += 1
                continue
            item = {**item["anchor"], "received_at": item.get("received_at")}
        elif isinstance(item, Mapping):
            item = {**{k: v for k, v in item.items() if k != "received_at"}, "unsigned": True}  # no one vouches for a time
        if valid(item) is None:
            out.append({**item, "source": source})
        else:
            dropped += 1
    return out, dropped


def fold(report: Any, findings: list[Finding], summary: dict) -> None:
    """Add the anchor findings to a verify Report, and let them decide its status too."""
    report.findings.extend(findings)
    if any(f.level == "tampered" for f in report.findings):
        report.status = "tampered"
    elif any(f.level == "unprovable" for f in report.findings):
        report.status = "unverifiable"
    report.anchors = summary
