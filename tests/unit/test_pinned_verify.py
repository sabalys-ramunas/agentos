"""Pins the audit verifier's whole verdict (agentos.policy.auditlog.verify): a synthetic log signed
with a fixed key, then one kind of tampering, damage or retention per case, and the options a
walk takes (partial, mid, to_seq, max_seq_used, scope, trust). Each case pins the report, the bad
and unproven seqs, the exit code, and each row's status."""

from __future__ import annotations

import pytest
from pinned import check, outcome
from pinned_log import KEY2, KID, KID2, PUB, Log, row

from agentos.policy import auditlog
from agentos.policy.auditlog import SIG_CTX, Scope, sign, verify


def honest() -> Log:
    log = Log()
    log.checkpoint()
    return log


def scenarios() -> dict:
    out: dict = {}

    def case(name: str, log: Log, **kw):
        out[name] = (log, kw)

    case("honest", honest())
    case("honest, trusted key", honest(), trusted={KID})
    case("honest, untrusted key", honest(), trusted={"k_other"})
    case("honest, no trust note", honest(), trust_note=False)
    case("no checkpoint", Log())
    log = honest(); log.rows += [row(7)]; log.relink(6)
    case("rows after the checkpoint", log)
    log = honest(); log.row_at(3)["summary"] = "edited"
    case("edited row", log)
    log = honest(); log.row_at(3)["summary"] = "edited"; log.relink(2)
    case("edited row, hashes recomputed", log)
    log = honest(); log.drop(3)
    case("deleted middle row", log)
    log = honest(); log.drop(3); log.row_at(4)["summary"] = "x"
    case("deleted row and edited next", log)
    log = honest(); log.drop(5, 6)
    case("deleted tail", log)
    log = honest(); log.drop(6)
    case("deleted tail, max seq used", log, max_seq_used=8)
    case("honest, max seq used beyond", honest(), max_seq_used=9)
    log = honest(); log.rows[2], log.rows[3] = log.rows[3], log.rows[2]
    case("reordered rows", log)
    log = honest(); r = log.row_at(4); r["hash"] = None
    case("row with no hash after the chain began", log)
    log = honest(); log.row_at(4)["prev"] = "sha256:" + "0" * 64
    case("row links to nothing", log)
    log = Log(n=8); log.checkpoint(4); log.checkpoint(8)
    case("two checkpoints", log)
    log = Log(n=8); log.checkpoint(4); log.checkpoint(8); log.drop(7, 8)
    case("tail deleted under the second checkpoint", log)
    log = Log(n=6); log.checkpoint(6); log.checkpoint(6)
    case("two checkpoints at one seq", log)
    log = honest(); log.edit_mark(2, hash="sha256:" + "1" * 64)
    case("checkpoint vouches for another hash", log)
    log = honest(); log.edit_mark(2, count=99)
    case("checkpoint count wrong", log)
    log = honest(); log.marks[1]["body"] = "not json"
    case("unreadable mark", log)
    log = honest(); log.marks[1]["body"] = "[1, 2]"
    case("mark body not an object", log)
    log = honest(); log.edit_mark(2, resign=False, reason="edited")
    case("mark edited, not resigned", log)
    log = honest(); log.marks[1]["sig"] = None
    case("unsigned checkpoint", log)
    log = honest(); log.marks[1]["sig"] = sign(KEY2, SIG_CTX, log.marks[1]["body"].encode())
    case("checkpoint forged", log)
    log = honest(); log.edit_mark(2, kid=KID2)
    case("checkpoint names another key", log)
    log = Log(n=8); log.checkpoint(4); log.checkpoint(8); del log.marks[1]
    case("mark deleted", log)
    log = Log(n=8); log.checkpoint(4); log.checkpoint(8); log.edit_mark(3, prev="sha256:" + "2" * 64)
    case("mark replaced", log)
    log = honest(); del log.marks[0]
    case("genesis deleted", log)
    log = honest(); del log.marks[0]
    case("genesis deleted, partial", log, partial=True)
    log = honest(); log.mark("genesis", pub=PUB, after_seq=0, unchained=0)
    case("second genesis", log)
    log = honest(); log.edit_mark(1, kid=KID2)
    case("genesis names another key", log)
    log = honest(); log.edit_mark(1, pub="garbage")
    case("genesis unreadable key", log)
    log = honest(); log.rotate(); log.rows += [row(7), row(8)]; log.relink(6); log.checkpoint()
    case("key rotation", log)
    log = honest(); log.rotate(); log.rows += [row(7)]; log.relink(6); log.checkpoint()
    case("key rotation, trusted both", log, trusted={KID, KID2})
    log = honest(); log.rotate(reset=True); log.rows += [row(7)]; log.relink(6); log.checkpoint()
    case("key reset, untrusted", log)
    log = honest(); log.rotate(reset=True); log.rows += [row(7)]; log.relink(6); log.checkpoint()
    case("key reset, trusted", log, trusted={KID, KID2})
    log = honest(); log.rotate(reset=True, as_node=True); log.rows += [row(7)]; log.relink(6); log.checkpoint()
    case("key reset as the node writes it", log)
    log = honest(); log.rotate(reset=True, as_node=True); log.rows += [row(7)]; log.relink(6); log.checkpoint()
    case("key reset as the node writes it, trusted", log, trusted={KID, KID2})
    log = honest(); log.rotate(next_pub="garbage")
    case("key mark unreadable key", log)
    log = honest(); log.rotate(next_kid="k_wrong")
    case("key mark id mismatch", log)
    log = honest(); log.rotate(); log.edit_mark(3, kid=KID)
    case("key mark signed by the old kid", log)
    log = Log(n=8); log.checkpoint(4); log.prune(3, [2]); log.checkpoint(8)
    case("retention, honest", log)
    log = Log(n=8); log.checkpoint(4); log.prune(3, [2]); log.checkpoint(8); log.row_at(2)["summary"] = "x"
    case("retention, kept row edited", log)
    log = Log(n=8); log.checkpoint(4); log.prune(3, [2]); log.checkpoint(8); log.rows.insert(0, row(1))
    case("retention, row added below", log)
    log = Log(n=8); log.checkpoint(4); log.prune(3, [2]); log.checkpoint(8); log.drop(2)
    case("retention, kept row missing", log)
    log = Log(n=8); log.checkpoint(4); log.prune(3, [2]); log.checkpoint(8); log.drop(2)
    case("retention, kept row missing, partial", log, partial=True)
    log = Log(n=8); log.checkpoint(4); log.prune(3, [2]); log.checkpoint(8); log.drop(2)
    case("retention, kept row missing, mid", log, mid=True)
    log = Log(n=8); log.checkpoint(4); log.prune(3, [], kept_raw=[["x"]]); log.checkpoint(8)
    case("retention, kept list unreadable", log)
    log = Log(n=8); log.checkpoint(4); log.prune(3, [2], signed=False); log.checkpoint(8)
    case("retention unsigned", log)
    log = Log(n=8); log.checkpoint(4); log.prune(3, [2], signed=False); log.checkpoint(8); log.row_at(2)["summary"] = "x"
    case("retention unsigned, row below edited", log)
    log = Log(n=8); log.checkpoint(4); log.prune(3, [2], signed=False); log.checkpoint(8); log.drop(2)
    case("retention unsigned, kept row gone", log)
    log = Log(n=10); log.checkpoint(4); log.prune(3, [2], signed=False); log.prune(5, [2]); log.checkpoint(10)
    case("retention unsigned then signed", log)
    log = Log(n=10); log.checkpoint(4); log.prune(5, [2, 4]); log.prune(3, [2]); log.checkpoint(10)
    case("retention lower through after higher", log)
    log = Log(n=10); log.checkpoint(4); log.prune(5, [4], signed=False); log.prune(3, [2]); log.checkpoint(10)
    case("retention signed below unsigned", log)
    log = Log(n=8, after=2); log.checkpoint(8)
    case("unchained rows before the chain", log)
    log = Log(n=8, after=2); log.checkpoint(8)
    case("unchained rows, scoped", log, scope=Scope(lane="biz-a/x"))
    log = Log(n=8, after=2, unchained=1); log.checkpoint(8)
    case("more unchained rows than counted", log)
    log = Log(n=8, after=2); log.checkpoint(2); log.checkpoint(8)
    case("checkpoint at the genesis", log)
    log = Log(n=8, after=2); log.checkpoint(8); log.prune(4, [1])
    case("retention keeps an unchained row", log)
    log = Log(n=8, after=2); log.checkpoint(8); log.prune(4, [1]); log.row_at(1)["summary"] = "x"
    case("retention keeps an unchained row, edited", log)
    log = Log(n=8, lanes=("biz-a/x", "biz-b/y")); log.checkpoint()
    case("scope by lane", log, scope=Scope(lane="biz-a/x"))
    case("scope by time", Log(n=8), scope=Scope(since=1003.0, until=1006.0))
    log = Log(n=8, lanes=("biz-a/x", "biz-b/y")); log.checkpoint(); log.stub(3); log.stub(5)
    case("export, honest", log, partial=True, scope=Scope(lane="biz-a/x"))
    log = Log(n=8, lanes=("biz-a/x", "biz-b/y")); log.checkpoint(); log.stub(3); log.stub(4)
    case("export stubs", log, partial=True, scope=Scope(lane="biz-a/x"))
    log = Log(n=8, lanes=("biz-a/x", "biz-b/y")); log.checkpoint(); log.stub(4); log.rows[3]["digest"] = "0" * 64
    case("export stub edited", log, partial=True, scope=Scope(lane="biz-a/x"))
    log = Log(n=8, lanes=("biz-a/x", "biz-b/y")); log.checkpoint(); log.stub(4)
    del log.rows[3]["digest"]
    case("export stub unreadable", log, partial=True, scope=Scope(lane="biz-a/x"))
    log = Log(n=8, lanes=("biz-a/x", "biz-b/y")); log.checkpoint(); log.stub(3)
    case("export left out a row of its lane", log, partial=True, scope=Scope(lane="biz-b/y"))
    log = Log(n=10); log.checkpoint(5); log.checkpoint(10); log.rows = log.rows[4:]
    case("partial bundle", log, partial=True)
    log = Log(n=10); log.checkpoint(5); log.checkpoint(10); log.rows = log.rows[4:]
    case("mid walk", log, mid=True)
    log = Log(n=10); log.checkpoint(5); log.checkpoint(10); log.rows = []
    case("mid walk, no rows", log, mid=True)
    log = Log(n=10); log.checkpoint(5); log.checkpoint(10); log.rows = log.rows[:7]
    case("to_seq", log, to_seq=7)
    log = Log(n=10); log.checkpoint(5); log.checkpoint(10); log.rows = []
    case("no rows at all", log)
    log = Log(n=6); log.checkpoint(); log.row_at(2)["seq"] = "x"
    case("row seq not a number", log)
    return out


def verdict(log: Log, kw: dict) -> dict:
    report = verify([dict(r) for r in log.rows], [dict(m) for m in log.marks], **kw)
    statuses = {}
    for r in log.rows:
        try:
            statuses[str(r["seq"])] = report.row_status(r)
        except Exception as e:  # noqa: BLE001
            statuses[str(r["seq"])] = f"raised {type(e).__name__}"
    return {**report.public(), "bad": sorted(report.bad), "unproven": sorted(report.unproven),
            "exit": report.exit_code, "rows": statuses}


def test_the_verifier_is_pinned(monkeypatch):
    check("verify", {name: outcome(lambda a=a: verdict(*a)) for name, a in scenarios().items()})


@pytest.mark.parametrize("window", [2])
def test_reordering_far_apart_is_pinned(monkeypatch, window):
    monkeypatch.setattr(auditlog.Verifier, "WINDOW", window)
    log = Log(n=10)
    log.checkpoint()
    log.rows[1], log.rows[7] = log.rows[7], log.rows[1]
    near = Log(n=10)
    near.checkpoint()
    near.rows[3], near.rows[4] = near.rows[4], near.rows[3]
    check("verify_window", {"far swap": outcome(lambda: verdict(log, {})), "near swap": outcome(lambda: verdict(near, {}))})
