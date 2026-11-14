"""The spill file after a crash: a torn tail or line is set aside in a `.torn` file and named by the
health monitor, every complete row is restored, and a new spill never starts on a torn line
(agentos.policy.traces)."""

from __future__ import annotations

import json
import re
import types
from pathlib import Path

import pytest
from test_policy_support import Clock

from agentos.core.config import NodeConfig
from agentos.ops.health import HealthMonitor
from agentos.policy.traces import TraceEvent, TraceStore


def ev(i: int = 0, **kw) -> TraceEvent:
    return TraceEvent(**{"kind": "tool_end", "tool": "browser_act", "lane": "biz-a/instagram", "status": "ok",
                         "summary": f"step {i}", "args": {"i": i}, **kw})


def make_rows(path: Path, clock: Clock, ids) -> list[bytes]:
    """Spill lines for events `ids`, built the way the store builds them. Call this before the spill
    file exists: a store opened on a path with a spill file replays it when it closes."""
    s = TraceStore(path, clock=clock)
    try:
        return [json.dumps(s._row(ev(i))).encode() for i in ids]
    finally:
        s.close()


def half(line: bytes) -> bytes:
    """A row that broke off mid-write: the first half of its JSON, no newline."""
    return line[: len(line) // 2]


def arg_i(row: dict) -> int:
    return json.loads(row["args"])["i"]


TORN_NAME = re.compile(r"traces\.db\.spill\.jsonl\.\d{20}\.torn")


def torn_files(path: Path) -> list[Path]:
    return sorted(p for p in path.parent.iterdir() if TORN_NAME.fullmatch(p.name))


def restoring_files(path: Path) -> list[Path]:
    return sorted(path.parent.glob("traces.db.spill.jsonl.*.restoring"))


@pytest.fixture
def clock():
    return Clock()


def test_a_torn_tail_is_set_aside_and_the_rows_before_it_are_restored(tmp_path, clock):
    path = tmp_path / "traces.db"
    rows = make_rows(path, clock, range(4))
    torn = half(rows[3])
    spill = path.with_name("traces.db.spill.jsonl")
    spill.write_bytes(b"".join(r + b"\n" for r in rows[:3]) + torn)  # the process died mid-line

    s = TraceStore(path, clock=clock)
    try:
        s.flush()
        assert [arg_i(r) for r in s.chain_rows()] == [0, 1, 2]
        assert s.verify().status != "tampered"
        [kept] = torn_files(path)
        assert kept.read_bytes() == torn + b"\n"
        assert s.spill_torn == {"lines": 1, "file": str(kept), "at": clock.t}
        assert not spill.exists() and restoring_files(path) == []
    finally:
        s.close()


def test_a_torn_line_in_the_middle_is_set_aside_and_the_rows_around_it_are_restored(tmp_path, clock):
    path = tmp_path / "traces.db"
    rows = make_rows(path, clock, range(3))
    torn = half(rows[1])
    spill = path.with_name("traces.db.spill.jsonl")
    spill.write_bytes(rows[0] + b"\n" + torn + b"\n" + rows[2] + b"\n")

    s = TraceStore(path, clock=clock)
    try:
        s.flush()
        assert [arg_i(r) for r in s.chain_rows()] == [0, 2]
        [kept] = torn_files(path)
        assert kept.read_bytes() == torn + b"\n"
        assert s.spill_torn["lines"] == 1
        assert not spill.exists() and restoring_files(path) == []
    finally:
        s.close()


def test_a_last_complete_row_without_its_newline_is_recovered_and_nothing_is_set_aside(tmp_path, clock):
    path = tmp_path / "traces.db"
    rows = make_rows(path, clock, range(3))
    spill = path.with_name("traces.db.spill.jsonl")
    spill.write_bytes(b"\n".join(rows))  # the last row lost only its newline

    s = TraceStore(path, clock=clock)
    try:
        s.flush()
        assert [arg_i(r) for r in s.chain_rows()] == [0, 1, 2]
        assert torn_files(path) == []
        assert s.spill_torn is None
        assert not spill.exists() and restoring_files(path) == []
    finally:
        s.close()


def test_a_line_that_is_not_utf8_is_set_aside_and_the_others_are_restored(tmp_path, clock):
    path = tmp_path / "traces.db"
    rows = make_rows(path, clock, range(3))
    assert rows[1].count(b"step 1") == 1
    bad = rows[1].replace(b"step 1", b"step \xff1")
    with pytest.raises(ValueError):  # the precondition: this line is not decodable
        json.loads(bad)
    spill = path.with_name("traces.db.spill.jsonl")
    spill.write_bytes(rows[0] + b"\n" + bad + b"\n" + rows[2] + b"\n")

    s = TraceStore(path, clock=clock)
    try:
        s.flush()
        assert [arg_i(r) for r in s.chain_rows()] == [0, 2]
        [kept] = torn_files(path)
        assert kept.read_bytes() == bad + b"\n"
        assert s.spill_torn["lines"] == 1
        assert not spill.exists() and restoring_files(path) == []
    finally:
        s.close()


def test_a_new_spill_starts_on_its_own_line_after_a_torn_tail(tmp_path, clock):
    path = tmp_path / "traces.db"
    rows = make_rows(path, clock, range(3))
    torn = half(rows[2])
    spill = path.with_name("traces.db.spill.jsonl")
    spill.write_bytes(rows[0] + b"\n" + rows[1] + b"\n" + torn)  # ends mid-line, no newline

    s = TraceStore(path, clock=clock)
    try:
        s._pending.extend(s._row(ev(i)) for i in (3, 4))
        s._spill()
        lines = spill.read_bytes().split(b"\n")
        assert len(lines) == 6 and lines[5] == b""  # the file now ends with a newline
        assert lines[:2] == rows[:2]
        assert lines[2] == torn  # the torn tail stays alone on its line
        assert [arg_i(json.loads(x)) for x in lines[3:5]] == [3, 4]
        assert s.pending_count == 0
        s.flush()
        assert [arg_i(r) for r in s.chain_rows()] == [0, 1, 3, 4]
        [kept] = torn_files(path)
        assert kept.read_bytes() == torn + b"\n"
        assert s.spill_torn["lines"] == 1
        assert not spill.exists() and restoring_files(path) == []
    finally:
        s.close()


def test_the_health_monitor_names_a_torn_spill_line(tmp_path, clock):
    path = tmp_path / "traces.db"
    rows = make_rows(path, clock, range(2))
    spill = path.with_name("traces.db.spill.jsonl")
    spill.write_bytes(rows[0] + b"\n" + half(rows[1]))  # torn tail

    s = TraceStore(path, clock=clock)
    try:
        s.flush()
        monitor = HealthMonitor(NodeConfig(home=tmp_path))
        monitor.policy = types.SimpleNamespace(traces=s)
        found = {f.key: f for f in monitor.check_audit()}
        assert found["audit.write"].level == "ok"
        torn = found["audit.spill_torn"]
        assert (torn.kind, torn.level) == ("audit", "warn")
        assert "1 line(s)" in torn.detail and str(torn_files(path)[0]) in torn.detail
        assert {k: torn.data[k] for k in s.spill_torn} == s.spill_torn
        assert torn.data["files"] == [str(p) for p in torn_files(path)]
        assert "\u2014" not in torn.title + torn.detail
    finally:
        s.close()


def test_no_torn_finding_while_no_line_was_set_aside(tmp_path, clock):
    s = TraceStore(tmp_path / "traces.db", clock=clock)
    try:
        monitor = HealthMonitor(NodeConfig(home=tmp_path))
        monitor.policy = types.SimpleNamespace(traces=s)
        assert [f.key for f in monitor.check_audit()] == ["audit.write"]
    finally:
        s.close()


def test_a_row_an_older_spill_glued_onto_a_torn_one_is_recovered(tmp_path, clock):
    # the spill writer before this fix appended the next row straight after a torn tail
    path = tmp_path / "traces.db"
    rows = make_rows(path, clock, range(3))
    path.with_name("traces.db.spill.jsonl").write_bytes(rows[0] + b"\n" + half(rows[1]) + rows[2] + b"\n")
    s = TraceStore(path, clock=clock)
    try:
        s.flush()
        got = sorted(arg_i(dict(r)) for r in s.db.all("SELECT args FROM traces"))
        assert got == [0, 2]
        assert s.spill_torn["lines"] == 1 and torn_files(path)[0].read_bytes() == half(rows[1]) + b"\n"
    finally:
        s.close()


def test_torn_files_left_from_before_a_restart_are_still_named(tmp_path, clock):
    path = tmp_path / "traces.db"
    s = TraceStore(path, clock=clock)
    try:
        path.with_name("traces.db.spill.jsonl.00000000000000000001.torn").write_bytes(b'{"id": "x\n')
        monitor = HealthMonitor(NodeConfig(home=tmp_path))
        monitor.policy = types.SimpleNamespace(traces=s)
        found = {f.key: f for f in monitor.check_audit()}
        assert found["audit.spill_torn"].level == "warn" and "1 file(s)" in found["audit.spill_torn"].detail
    finally:
        s.close()
