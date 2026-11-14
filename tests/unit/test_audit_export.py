"""`agentos audit export`: a self-contained bundle for one day (and lane) that verifies offline,
with no node, and catches an edited row, a hidden lane row, an edited file or manifest."""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest
from test_policy_support import T0, Clock
from typer.testing import CliRunner

from agentos.audit.errors import BundleInvalid
from agentos.audit.service import AuditService, verify_bundle
from agentos.policy.auditlog import EXIT_OK, EXIT_TAMPERED
from agentos.policy.traces import TraceEvent, TraceStore

DAY = "2027-01-15"  # T0 is 2027-01-15T08:00Z
LANE = "biz-a/instagram"


class FakeRunner:
    def __init__(self, runs: dict):
        self.runs = runs

    def get(self, run_id):
        if run_id not in self.runs:
            raise KeyError(run_id)
        return self.runs[run_id]

    def describe(self, run):
        return run


@pytest.fixture
def node(tmp_path):
    clock = Clock()
    store = TraceStore(tmp_path / "traces.db", clock=clock)
    shot = tmp_path / "runner" / "shots" / "run_1" / "step-final-a1.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(b"\x89PNG proof of the post")
    runner = FakeRunner({"run_1": {"id": "run_1", "flow": "instagram-post", "status": "succeeded", "lane": LANE,
                                   "proof": {"url": "https://www.instagram.com/p/abc/", "readbacks": [],
                                             "screenshot": "runner/shots/run_1/step-final-a1.png"}}})
    svc = AuditService(store, runner=runner, home=tmp_path, clock=clock, node="node-1")
    clock.t = T0 - 86400  # the day before
    store.write(TraceEvent(kind="e", lane=LANE, summary="yesterday"))
    clock.t = T0
    start = store.write(TraceEvent(kind="tool_start", tool="flow_run", lane=LANE, status="started", run_id="run_1",
                                   args={"caption": "hello"}))
    store.write(TraceEvent(kind="tool_end", tool="flow_run", lane=LANE, status="ok", run_id="run_1",
                           parent_id=start.id, summary="Posted to instagram.com as biz-a"))
    store.write(TraceEvent(kind="tool_end", tool="browser_read", lane="biz-b/linkedin", summary="other company"))
    clock.advance(60)
    store.write(TraceEvent(kind="tool_end", tool="browser_act", lane=LANE, status="ok", summary="Liked a post"))
    yield svc, store, tmp_path
    store.close()


def test_a_day_of_one_lane_verifies_offline(node, tmp_path):
    svc, store, home = node
    out = svc.export(tmp_path / "out" / "day.zip", day=DAY, lane=LANE, tz="UTC")
    assert out["rows"] == 3 and out["stubs"] == 1 and out["files"] == 1
    store.close()
    offline = tmp_path / "auditor"
    offline.mkdir()
    bundle = shutil.copy(out["path"], offline / "day.zip")
    shutil.rmtree(home / "secrets")  # no node, no keys on disk: only the bundle
    (home / "traces.db").unlink()
    report = verify_bundle(Path(bundle))
    assert report.status == "ok", report.public()
    assert report.in_scope == 3 and report.proven_through is not None
    assert [f.level for f in report.findings] == ["note"]  # it trusted the bundle's own genesis key, and says so
    with zipfile.ZipFile(bundle) as z:
        rows = [json.loads(line) for line in z.read("rows.jsonl").splitlines()]
        assert "other company" not in z.read("rows.jsonl").decode()  # the other lane is a stub
        assert z.read("files/run_1/step-final-a1.png") == b"\x89PNG proof of the post"
    assert [r.get("stub", False) for r in rows] == [False, False, True, False]
    kid = report.keys[0]["kid"]
    assert verify_bundle(Path(bundle), trusted={kid}).findings == []


def _rewrite(bundle: Path, name: str, edit) -> None:
    with zipfile.ZipFile(bundle) as z:
        items = {n: z.read(n) for n in z.namelist()}
    items[name] = edit(items[name])
    with zipfile.ZipFile(bundle, "w") as z:
        for n, data in items.items():
            z.writestr(n, data)


def test_an_edited_row_in_a_bundle(node, tmp_path):
    svc, *_ = node
    path = Path(svc.export(tmp_path / "day.zip", day=DAY, lane=LANE, tz="UTC")["path"])
    _rewrite(path, "rows.jsonl", lambda b: b.replace(b"Liked a post", b"Read a post"))
    texts = [f.text for f in verify_bundle(path).findings if f.level == "tampered"]
    assert "row seq 5 was edited after it was written: its contents no longer match its hash" in texts
    assert "rows.jsonl does not match the hash the export signed" in texts


def test_a_lane_row_hidden_as_a_stub(node, tmp_path):
    svc, store, _ = node
    path = Path(svc.export(tmp_path / "day.zip", day=DAY, lane=LANE, tz="UTC")["path"])
    from agentos.policy.auditlog import stub

    row5 = next(r for r in store.chain_rows() if r["seq"] == 5)

    def hide(blob: bytes) -> bytes:
        lines = blob.splitlines()
        return b"\n".join(json.dumps(stub(row5), sort_keys=True).encode() if json.loads(x)["seq"] == 5 else x
                          for x in lines) + b"\n"
    _rewrite(path, "rows.jsonl", hide)
    texts = [f.text for f in verify_bundle(path).findings]
    assert f"row seq 5 belongs to lane {LANE}, but the export left it out" in texts


def test_an_edited_screenshot_or_manifest(node, tmp_path):
    svc, *_ = node
    path = Path(svc.export(tmp_path / "day.zip", day=DAY, lane=LANE, tz="UTC")["path"])
    _rewrite(path, "files/run_1/step-final-a1.png", lambda b: b + b"edited")
    assert "files/run_1/step-final-a1.png does not match the hash the export signed" in \
        [f.text for f in verify_bundle(path).findings]
    _rewrite(path, "manifest.json", lambda b: b.replace(b'"node":"node-1"', b'"node":"node-2"'))
    assert any("manifest signature does not verify" in f.text for f in verify_bundle(path).findings)


def test_not_a_bundle(tmp_path):
    (tmp_path / "x.zip").write_bytes(b"nope")
    with pytest.raises(BundleInvalid):
        verify_bundle(tmp_path / "x.zip")


def test_cli_export_then_verify_bundle(node, tmp_path, monkeypatch):
    from agentos.audit.cli import app

    _svc, store, home = node
    store.close()
    monkeypatch.setenv("AGENTOS_HOME", str(home))
    runner = CliRunner()
    out = tmp_path / "b.zip"
    r = runner.invoke(app, ["export", "--day", DAY, "--lane", LANE, "--tz", "UTC", "-o", str(out)])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["verify", "--bundle", str(out)])
    assert r.exit_code == EXIT_OK, r.output
    _rewrite(out, "rows.jsonl", lambda b: b.replace(b"Liked a post", b"Read a post"))
    assert runner.invoke(app, ["verify", "--bundle", str(out)]).exit_code == EXIT_TAMPERED


def test_cli_replay(node, monkeypatch):
    from agentos.audit.cli import app

    _svc, store, home = node
    store.checkpoint()
    store.close()
    monkeypatch.setenv("AGENTOS_HOME", str(home))
    r = CliRunner().invoke(app, ["replay", "--lane", LANE, "--day", DAY, "--tz", "UTC"])
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    assert lines[0].startswith(f"{LANE} on {DAY} (UTC): 2 steps, chain ok (proven through checkpoint")
    assert "flow_run" in lines[1] and "[chain ok]" in lines[1] and "Posted to instagram.com as biz-a" in lines[1]
    assert "Liked a post" in r.output and "other company" not in r.output and "—" not in r.output


def test_cli_replay_says_rows_after_the_last_checkpoint_are_not_sealed_yet(node, monkeypatch):
    """QA 2026-11-10 finding 14: rows the next checkpoint will seal are not "[chain unprovable]"."""
    from agentos.audit.cli import app

    _svc, store, home = node
    monkeypatch.setattr(store, "checkpoint", lambda *_a, **_k: None)  # no checkpoint since the rows were written
    store.close()
    monkeypatch.setenv("AGENTOS_HOME", str(home))
    r = CliRunner().invoke(app, ["replay", "--lane", LANE, "--day", DAY, "--tz", "UTC"])
    assert r.exit_code == 0, r.output
    assert "[chain unprovable]" not in r.output and "unprovable:" not in r.output
    assert r.output.count("[not sealed yet]") == 2, r.output
    assert "not sealed yet: 2 step(s) after the last checkpoint. The next checkpoint seals them" in r.output
    assert "agentos access audit checkpoint" in r.output and "—" not in r.output
