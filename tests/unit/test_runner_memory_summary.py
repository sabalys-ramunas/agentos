"""Every finished run leaves one run summary in its workspace's memory, from the runner's
structured record only (docs/design/workspace-memory.md 3.2)."""

from __future__ import annotations

import json

import pytest
from testkit.memory import fake_memory

from agentos.core.models import RunStatus
from agentos.memory import Caller
from agentos.runner.fakes import Harness, post_flow
from agentos.runner.memory_summary import summary_record

PARAMS = {"caption": "Autumn menu is here", "image": "files/a.jpg"}
OWNER = Caller.owner()


@pytest.fixture
def mem(tmp_path):
    m = fake_memory(tmp_path / "mem")
    yield m
    m.service.close()


def _summaries(mem, ws="biz-a"):
    return mem.service.list_entries(OWNER, ws, types=["run_summary"]).data["entries"]


async def test_a_finished_run_writes_one_node_summary(tmp_path, mem):
    h = Harness(tmp_path, services={"memory": mem.service})
    run = await h.run(h.save(post_flow()).id, PARAMS)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    [row] = _summaries(mem)
    entry = mem.service.get(OWNER, row["id"]).data
    assert entry["trust"] == "node" and entry["key"] == f"run.{run.id}"
    attrs = entry["attrs"]
    assert attrs["status"] == "succeeded-verified" and attrs["flow"] == "biz-a.social.post"
    assert attrs["lane"] == "biz-a/social" and attrs["content_digest"].startswith("sha256:")
    assert any(p.get("readback") == "passed" for p in attrs["proof"])
    assert "approved_text" not in attrs           # nobody approved this run
    # never page text: the readback's found value and the page's own words stay out
    text = json.dumps(entry)
    assert "found" not in json.dumps(attrs["proof"]) and "Feed" not in text


async def test_a_failed_run_is_summarized_honestly(tmp_path, mem):
    flow = post_flow()
    flow["steps"][-1]["condition"]["changed_by"] = 5
    h = Harness(tmp_path, services={"memory": mem.service})
    run = await h.run(h.save(flow).id, PARAMS)
    assert run.status == RunStatus.PARTIAL
    [row] = _summaries(mem)
    assert mem.service.get(OWNER, row["id"]).data["attrs"]["status"] == "partial"


async def test_a_memory_failure_never_fails_the_run(tmp_path):
    class Broken:
        def run_summary(self, record):
            raise RuntimeError("disk full")

    class Traces:
        def __init__(self):
            self.events = []

        def write(self, ev, **_kw):
            self.events.append(ev)

    traces = Traces()
    h = Harness(tmp_path, services={"memory": Broken(), "traces": traces})
    run = await h.run(h.save(post_flow()).id, PARAMS)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED
    assert [e.kind for e in traces.events] == ["memory_run_summary"] and traces.events[0].status == "error"


async def test_no_memory_service_is_fine(tmp_path):
    h = Harness(tmp_path)
    run = await h.run(h.save(post_flow()).id, PARAMS)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED


async def test_an_approved_run_carries_the_text_the_owner_read(tmp_path):
    h = Harness(tmp_path)
    run = await h.run(h.save(post_flow()).id, PARAMS)
    run = run.model_copy(update={"approval_id": "apv_1", "authorized": True, "schedule_id": "sch_9",
                                 "proof": {**(run.proof or {}), "readbacks": [
                                     {"path": "8", "passed": True, "found": "PAGE TEXT", "check": {"url": "https://x/u"}}]}})
    record = summary_record(run, h.flows)
    assert record["approved_text"] == "caption: Autumn menu is here\nimage: files/a.jpg"
    assert record["caller"] == "schedule:sch_9"
    assert "PAGE TEXT" not in json.dumps(record)
    assert {"url": "https://x/u", "path": "8", "readback": "passed"} in record["proof"]


def test_a_run_outside_a_workspace_has_no_summary():
    from agentos.runner.models import Run

    assert summary_record(Run(id="run_x", status=RunStatus.FAILED)) is None
    assert summary_record(Run(id="run_y", lane="biz-a/x", status=RunStatus.RUNNING)) is None
