"""openajar.env's side of a machine that stopped (openajar.environments.liveness answers the hook): a command on a
VM lane whose ssh could not reach the machine fails with machine_gone when the provider says it is gone, and is
handed back as before when it runs; a job on it is lost at once with that reason, and one that printed is never
said to have not started (evals t12, 2026-11-21)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from openajar.core.errors import MachineGone
from openajar.env import execs
from openajar.env.jobs import JobTable, _Follow
from openajar.env.models import ExecResult


def test_ssh_s_own_failure_is_told_from_a_commands():
    assert execs.ssh_lost(255, "ssh: connect to host 127.0.0.1 port 32771: Connection refused\n")
    assert execs.ssh_lost(255, "Connection to 127.0.0.1 closed by remote host.\n")
    assert execs.ssh_lost(255, "make: ***\nclient_loop: send disconnect: Broken pipe\n")
    assert not execs.ssh_lost(255, "my script failed\n")  # a command of the agent's that exits 255
    assert not execs.ssh_lost(1, "ssh: connect to host x port 22: Connection refused")


async def test_a_command_on_a_dead_machine_fails_with_machine_gone(tmp_path, monkeypatch):
    from openajar.env import manager as env_manager
    from openajar.env.manager import EnvManager
    from openajar.env.models import RemoteHost

    mgr = EnvManager(tmp_path, use_lock=False)
    asked: list[str] = []

    async def lost(lane_id):
        asked.append(lane_id)
        return "environment e1 (\"t\"): its machine stopped" if lane_id == "env/egone12345" else None

    mgr.machine_lost = lost
    remote = RemoteHost(vm_id="vm_1", host="127.0.0.1", key_path="/k", known_hosts="/h", host_alias="a")
    for lane in ("env/egone12345", "env/eblip12345"):
        mgr.create_remote(lane, remote)

    async def warm(lane_id, **kw):
        return SimpleNamespace(exec_sem=asyncio.Semaphore(1), lane=mgr.store.get_lane(lane_id))

    async def run(rt, argv, **kw):
        return ExecResult(exit_code=255, stdout="", stderr="ssh: connect to host 127.0.0.1 port 2: Connection refused",
                          duration_s=0.01)

    async def no_env(rt, grant_id):
        return {}

    monkeypatch.setattr(mgr, "warm", warm)
    monkeypatch.setattr(mgr, "command_env", no_env)
    monkeypatch.setattr(env_manager.execs, "run", run)
    with pytest.raises(MachineGone, match="its machine stopped"):
        await mgr.exec("env/egone12345", "true", use=False)
    res = await mgr.exec("env/eblip12345", "true", use=False)  # the machine runs: ssh's own answer, as before
    assert res.exit_code == 255 and asked == ["env/egone12345", "env/eblip12345"]


async def _judged(tmp_path: Path, lost_check, *, printed: int, waited: float) -> _Follow:
    table = JobTable(SimpleNamespace(jobs=lambda lane: tmp_path), SimpleNamespace(jobs_keep_per_lane=5))
    table.lost_check = lost_check
    job = SimpleNamespace(info=SimpleNamespace(lane="env/e1234567890"), out=SimpleNamespace(total=printed),
                          err=SimpleNamespace(total=0))

    async def pause(s):
        return None

    job.pause = pause
    st = _Follow(first=True, deadline=1e12)
    st.out_of_reach = asyncio.get_running_loop().time() - waited
    await table._judge(job, None, st, None, 0.0)
    return st


async def test_a_job_whose_machine_stopped_says_so_and_not_that_it_never_started(tmp_path):
    async def gone(lane):
        return "environment e1 (\"Release build\"): its machine stopped at 10:12 UTC"

    st = await _judged(tmp_path, gone, printed=120, waited=0.0)
    assert st.lost.startswith("the job ended with its machine, which stopped while it ran.") and "10:12" in st.lost
    assert "Nothing ran" not in st.lost  # it did run, until its machine went

    async def runs(lane):
        return None

    st = await _judged(tmp_path, runs, printed=120, waited=30.0)  # it printed: it started
    assert st.lost is None
    st = await _judged(tmp_path, runs, printed=0, waited=30.0)  # never a word, never an answer: as before
    assert st.lost == "the node could not reach the machine over ssh to start it"
