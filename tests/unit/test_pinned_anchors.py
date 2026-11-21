"""Pins the anchor check (openajar.policy.anchors.check): a synthetic signed log, the anchors held
off the node for its checkpoints and retention marks, then one way the log or the anchors differ
per case. Each case pins the findings in order and the summary."""

from __future__ import annotations

from pinned import check, outcome
from pinned_log import KID, Log

from openajar.policy import anchors

H = "sha256:" + "9" * 64


def t(hours: float) -> str:
    """A time `hours` after 2026-11-14T00:00Z, as a mark or a receipt writes it."""
    total = int(hours * 3600)
    day, rest = divmod(total, 86400)
    return f"2026-11-{14 + day:02d}T{rest // 3600:02d}:{rest % 3600 // 60:02d}:00+00:00"


def held(log: Log, *ns: int, received: dict[int, float] | None = None, drop_kind: set[int] | None = None) -> list:
    """The anchors a controller holds for marks `ns` (all checkpoint and retention marks by default)."""
    import json

    out = []
    for m in log.marks:
        body = json.loads(m["body"])
        if body["kind"] not in anchors.KINDS or (ns and body["n"] not in ns):
            continue
        a = anchors.anchor_of(body)
        if drop_kind and body["n"] in drop_kind:
            a.pop("kind", None)
        if received and body["n"] in received:
            a["received_at"] = t(received[body["n"]])
        out.append(a)
    return out


def base() -> Log:
    """Ten rows: checkpoints at seq 4 (mark 2) and 8 (mark 3), retention through seq 3 a day and a
    half later (mark 4), then a checkpoint at seq 10 (mark 5)."""
    log = Log(n=10)
    log.checkpoint(4, at=t(0))
    log.checkpoint(8, at=t(1))
    log.prune(3, [], at=t(36))
    log.checkpoint(10, at=t(37))
    return log


def scenarios() -> dict:
    out: dict = {}

    def case(name, log, anchor_list, **kw):
        out[name] = (log, anchor_list, kw)

    log = base()
    case("all anchored", log, held(log))
    case("all anchored, trusted", log, held(log), trusted={KID})
    case("all anchored, received times", log, held(log, received={2: 0.1, 3: 1.1, 4: 36.1, 5: 37.1}))
    case("nothing held", log, [])
    case("only the first checkpoint", log, held(log, 2))
    case("checkpoints only", log, held(log, 2, 3, 5))
    case("retention not anchored, later anchored", log, held(log, 2, 3, 5))
    case("newest not anchored", log, held(log, 2, 3, 4))
    case("duplicates, earliest receipt wins", log,
         held(log, received={2: 5, 3: 5, 4: 40, 5: 40}) + held(log, received={2: 0.1, 4: 36.5}) + held(log))
    case("invalid anchors are skipped", log, [*held(log), {"chain": ""}, {"chain": "x", "n": 0}, "junk"])
    case("held beyond the log's marks", log, [*held(log), {**held(log, 5)[0], "n": 9, "seq": 12}])
    gone = base(); del gone.marks[2]
    case("an anchored mark is gone", gone, held(base()))
    case("anchor hash differs", log, [{**a, "hash": H} if a["n"] == 3 else a for a in held(log)])
    case("anchor kid differs", log, [{**a, "kid": "k_x"} if a["n"] == 3 else a for a in held(log)])
    case("anchor time differs", log, [{**a, "at": t(99)} if a["n"] == 2 else a for a in held(log)])
    case("anchor says checkpoint, mark is retention", log,
         [{**a, "kind": "checkpoint"} if a["n"] == 4 else a for a in held(log)])
    case("retention anchor with no kind, not a checkpoint", log, held(log, drop_kind={4}))
    low = Log(n=10); low.checkpoint(8, at=t(0)); low.prune(9, [], at=t(30)); low.checkpoint(10, at=t(31))
    case("retention anchor with no kind, could be a checkpoint", low, held(low, drop_kind={3}))
    case("retention anchor with no kind, earlier checkpoint held", low, held(low, drop_kind={3}))
    rewritten = base(); [r.update(hash=H) for r in rewritten.rows if r["seq"] == 8]
    case("row rewritten under an anchor", rewritten, held(base()))
    cut = base(); cut.drop(9, 10)
    case("rows cut from the end", cut, held(base()))
    hole = base(); hole.drop(8)
    case("row gone, no retention", hole, held(base()))
    at3 = Log(n=10); at3.checkpoint(3, at=t(0)); at3.prune(3, [], at=t(30)); at3.checkpoint(10, at=t(31))
    at3.edit_mark(3, through_hash=H)
    case("checkpoint at the retention head, other hash", at3, held(at3, 2, 4))
    at3b = Log(n=10); at3b.checkpoint(3, at=t(0)); at3b.prune(3, [], at=t(30)); at3b.checkpoint(10, at=t(31))
    case("checkpoint at the retention head", at3b, held(at3b))
    case("foreign chain", log, [*held(log), {**held(log, 5)[0], "chain": "ch_other"}])
    empty = Log(n=0); empty.marks.clear()
    case("no chain, foreign anchors", empty, held(base()))
    back = Log(n=10); back.checkpoint(8, at=t(0)); back.checkpoint(6, at=t(1))
    case("a later checkpoint with a lower seq", back, held(back))
    soon = Log(n=10); soon.checkpoint(4, at=t(0)); soon.prune(3, [], at=t(2)); soon.checkpoint(10, at=t(3))
    case("retention two hours after the checkpoint", soon, held(soon))
    soon1 = Log(n=10); soon1.checkpoint(4, at=t(0)); soon1.prune(3, [], at=t(0.5)); soon1.checkpoint(10, at=t(1))
    case("retention under an hour after", soon1, held(soon1))
    late = Log(n=10); late.checkpoint(4, at=t(0)); late.prune(3, [], at=t(2)); late.checkpoint(10, at=t(3))
    case("retention dated by its receipt", late, held(late, received={2: 0.0, 3: 1.0, 4: 1.5}))
    case("retention dated by a later mark's receipt", late, held(late, received={4: 30.0}))
    notime = Log(n=10); notime.checkpoint(4, at=t(0)); notime.prune(3, [], at="no time"); notime.checkpoint(10, at=t(3))
    case("retention with no readable time", notime, held(notime))
    nocover = Log(n=10); nocover.prune(3, [], at=t(30)); nocover.checkpoint(10, at=t(31))
    case("retention with no checkpoint before it", nocover, held(nocover))
    case("retention with no checkpoint before it, none held", nocover, [])
    cover = Log(n=10); cover.checkpoint(8, at=t(0)); cover.prune(3, [], at=t(30)); cover.checkpoint(10, at=t(31))
    case("retention covered by a later-seq checkpoint", cover, held(cover))
    two = Log(n=12); two.checkpoint(4, at=t(0)); two.prune(3, [], at=t(30)); two.checkpoint(8, at=t(31))
    two.prune(2, [], at=t(60)); two.checkpoint(12, at=t(61))
    case("a second retention mark that goes back", two, held(two))
    bad = base(); bad.edit_mark(4, through_seq="x")
    case("retention through_seq not a number", bad, held(bad, 2, 3, 5))
    unread = base(); unread.marks[3]["body"] = "not json"
    case("unreadable retention mark", unread, held(base()))
    nohash = base(); nohash.edit_mark(4, through_hash=None)
    case("retention of rows from before the chain", nohash, held(nohash, 2, 3, 5))
    case("marks older than every anchor held", log, held(log, 5))
    case("no traces table", log, held(log), traces=False)
    return out


def test_the_anchor_check_is_pinned():
    def run(log, anchor_list, kw):
        traces = kw.pop("traces", True)
        findings, summary = anchors.check(log.db(traces=traces), anchor_list, **kw)
        return {"findings": [f.public() for f in findings], "summary": summary}

    check("anchors_check", {name: outcome(lambda a=a: run(a[0], a[1], dict(a[2]))) for name, a in scenarios().items()})
