"""Restarts never close another session's lane silently (openajar.ops.restart): agentosd reports
who is on each open lane, `openajar node restart`, `openajar upgrade` and the installer name them
and ask first, and a restart that happens anyway leaves a lane event and tells claim holders."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from edge_shell import REPO
from test_lanes_claims import LANE, A, B, act, lane
from test_lanes_holds import again
from typer.testing import CliRunner

from openajar.cli import app
from openajar.core.errors import ClaimLost
from openajar.core.models import LaneInfo, LaneSpec, LaneState
from openajar.node.claims import holder_text
from openajar.ops import control, restart
from openajar.ops.service import HealthService

OTHER = "biz-a/other"


# ---- what agentosd reports -----------------------------------------------------------------


async def test_holders_name_claims_calls_takeovers_and_idle_lanes(tmp_path):
    t = await lane(tmp_path, lanes=(LANE, OTHER, "biz-a/cold"))
    await t.mgr.claim(LANE, A, purpose="weekly post", ttl_s=900)
    await t.mgr.open(OTHER)
    inside = asyncio.Event()
    release = asyncio.Event()

    async def call():
        async with t.mgr.using(LANE, holder=A):
            inside.set()
            await release.wait()

    task = asyncio.create_task(call())
    await inside.wait()
    rows = {row["id"]: row for row in t.mgr.holders()}
    assert set(rows) == {LANE, OTHER}  # a cold lane is not closed by a restart
    kinds = [(h["kind"], h["who"]) for h in rows[LANE]["holders"]]
    assert ("claim", "agent claude-code") in kinds and ("call", "agent claude-code") in kinds
    assert rows[LANE]["holders"][0]["purpose"] == "weekly post"
    assert rows[OTHER]["holders"] == []
    release.set()
    await task
    await t.mgr.take_control(OTHER, by="owner")
    rows = {row["id"]: row for row in t.mgr.holders()}
    assert {"kind": "takeover", "who": "owner"} in rows[OTHER]["holders"]
    assert holder_text(rows[LANE]["holders"][0]) == "claimed by agent claude-code (weekly post)"


async def test_a_restart_anyway_leaves_an_event_and_tells_the_claim_holder(tmp_path, caplog):
    t = await lane(tmp_path, lanes=(LANE, OTHER))
    await t.mgr.start()
    await t.mgr.claim(LANE, A, purpose="job", ttl_s=900)
    await t.mgr.open(OTHER)
    with caplog.at_level("WARNING"):
        await t.mgr.shutdown()
    assert "closed because agentosd is stopping (claimed by agent claude-code (job))" in caplog.text
    [event] = [e for e in t.mgr.events(LANE) if e.kind == "closed_by_shutdown"]
    assert event.detail["holders"][0]["kind"] == "claim"
    assert next(e for e in t.mgr.events(OTHER) if e.kind == "closed_by_shutdown").detail["holders"] == []
    mgr = again(t, queue_timeout=0.4)
    await mgr.start()
    with pytest.raises(ClaimLost, match="the node restarted"):
        await act(mgr, A)
    await act(mgr, B)
    await mgr.shutdown()


async def test_the_ops_socket_reports_holders_and_falls_back_for_simple_lane_managers(tmp_path):
    t = await lane(tmp_path)
    await t.mgr.claim(LANE, A, purpose="job", ttl_s=900)
    service = SimpleNamespace(holders=lambda: t.mgr.holders())
    resp = await control.dispatch(service, {"op": "holders"})
    assert resp["ok"] and resp["lanes"][0]["holders"][0]["kind"] == "claim"

    class Plain:  # a lane manager without holders(): the dev fakes
        def list(self):
            warm = LaneInfo(spec=LaneSpec(id=LANE), state=LaneState.WARM,
                            claim={"holder": "agent codex", "implicit": False, "purpose": "x"})
            return [warm, LaneInfo(spec=LaneSpec(id=OTHER), state=LaneState.COLD)]

    health = HealthService.__new__(HealthService)
    health._lanes = lambda: Plain()
    assert health.holders() == [{"id": LANE, "state": "warm",
                                 "holders": [{"kind": "claim", "who": "agent codex", "purpose": "x"}]}]


# ---- the CLI and the restart paths ---------------------------------------------------------


class FakeHost:
    def __init__(self, holders=None, *, older=False, systemd=True):
        self.holders = holders
        self.older = older
        self.systemd = systemd
        self.ran: list[list[str]] = []

    def unix_request(self, path, payload, timeout=10.0):
        if self.holders is None:
            return None
        if payload["op"] == "holders":
            return {"ok": False, "error": {"code": "invalid", "message": "unknown op"}} if self.older else \
                {"ok": True, "lanes": self.holders}
        return {"ok": True, "lanes": [{"id": r["id"], "state": r["state"]} for r in self.holders]}

    def which(self, name):
        return "/usr/bin/systemctl" if self.systemd else None

    def run(self, argv, timeout=10.0, env=None):
        self.ran.append(argv)
        return (0, "active\n") if argv[1] == "is-active" else (0, "")


HELD = [{"id": "test/probe", "state": "warm", "holders": [{"kind": "claim", "who": "agent codex", "purpose": "probe"}]},
        {"id": "test/reddit", "state": "warm", "holders": []}]


def test_open_lanes_and_what_counts_as_held(tmp_path):
    report = restart.open_lanes(tmp_path, FakeHost(HELD))
    assert [lane["id"] for lane in report.held] == ["test/probe"]
    lines = restart.describe(report)
    assert lines[0] == "  test/probe (warm): claimed by agent codex (probe)"
    assert "idle (it only loses its page" in lines[1]
    assert "test/probe" in restart.confirm_line(report) and "other sessions" in restart.confirm_line(report)
    older = restart.open_lanes(tmp_path, FakeHost(HELD, older=True))  # cannot say who: counts as held
    assert len(older.held) == 2 and "unknown" in restart.describe(older)[0]
    assert not restart.open_lanes(tmp_path, FakeHost(None)).running
    assert restart.confirm_line(restart.open_lanes(tmp_path, FakeHost([HELD[1]]))) == ""


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path))

    def run(host, *args, input=None):
        monkeypatch.setattr(restart, "Host", lambda: host)
        return CliRunner().invoke(app, ["node", "restart", *args], input=input)

    return run


def test_restart_check_exits_3_while_another_session_holds_a_lane(cli):
    result = cli(FakeHost(HELD), "--check")
    assert result.exit_code == restart.HELD_EXIT and "test/probe (warm): claimed by agent codex" in result.output
    assert cli(FakeHost([HELD[1]]), "--check").exit_code == 0
    assert cli(FakeHost(None), "--check").exit_code == 0


def test_restart_asks_before_closing_held_lanes_and_yes_goes_ahead(cli):
    host = FakeHost(HELD)
    result = cli(host, input="n\n")
    assert result.exit_code != 0 and "It closes 1 lane other sessions are using (test/probe)" in result.output
    assert ["systemctl", "restart", "agentosd"] not in host.ran
    host = FakeHost(HELD)
    result = cli(host, "--yes")
    assert result.exit_code == 0 and ["systemctl", "restart", "agentosd"] in host.ran
    assert "2 lanes closed" in result.output and "claim_lost" in result.output
    idle = FakeHost([HELD[1]])  # nobody holds anything: no question
    assert cli(idle).exit_code == 0 and ["systemctl", "restart", "agentosd"] in idle.ran
    assert "Ctrl-C" in cli(FakeHost(HELD, systemd=False), "--yes").output


def test_the_installer_does_not_restart_over_held_lanes_without_yes():
    script = (REPO / "deploy" / "install-node.sh").read_text()
    assert 'openajar" node restart --check' in script and "--yes|-y) YES=1" in script
    guard = script.index("node restart --check")
    assert guard < script.index("systemctl restart agentosd.service")
    assert '[ "$RESTART_HELD" = 0 ] || exit 3' in script


def test_upgrade_names_held_lanes_in_its_confirmation(tmp_path, monkeypatch):
    (tmp_path / "node.toml").write_text('name = "n"\n')
    monkeypatch.setenv("OPENAJAR_HOME", str(tmp_path))
    monkeypatch.setattr(restart, "Host", lambda: FakeHost(HELD))
    result = CliRunner().invoke(app, ["upgrade", "--source", str(tmp_path), "--no-service"], input="n\n")
    assert "test/probe (warm): claimed by agent codex (probe)" in result.output
    assert "other sessions are using (test/probe)" in result.output and result.exit_code != 0
