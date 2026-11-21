"""Workspace export format 2 carries the workspace's memory (docs/design/workspace-memory.md 4.13):
the chain still verifies after an import, forgotten entries stay forgotten, --as moves keys and
scopes, an import never merges memory, and a failed import takes its memory back out."""

from __future__ import annotations

import asyncio
import gzip
from pathlib import Path

import pytest
from test_wsexport_support import LOG_N, PASSPHRASE, Node

from openajar.core import age
from openajar.memory import Caller
from openajar.policy.traces import TraceEvent
from openajar.workspaces.transfer import export as wsexport
from openajar.workspaces.transfer import import_ as wsimport
from openajar.workspaces.transfer import read as wsread
from openajar.workspaces.transfer import sources as wssources
from openajar.workspaces.transfer.import_ import ImportOptions

OWNER = Caller.owner()


class MemNode(Node):
    def __init__(self, root: Path, name: str, **kw):
        super().__init__(root, name, **kw)
        self.memory = wssources._memory_service(self.home, self.policy, self.cfg)

    def sources(self):
        src = super().sources()
        src.memory = self.memory
        return src

    def close(self):
        self.memory.close()
        super().close()


@pytest.fixture
def nodes(tmp_path):
    a, b = MemNode(tmp_path, "node-a"), MemNode(tmp_path, "node-b")
    a.build_acme()
    m = a.memory
    ev = a.policy.traces.write(TraceEvent(kind="tool_start", tool="browser_snapshot", lane="acme/instagram",
                                          workspace="acme", summary="the profile page"))
    a.decision = m.write(OWNER, "acme", {"type": "decision", "title": "No posts on Sundays",
                                         "key": "acme.posting.sunday", "evidence": [ev.id],
                                         "attrs": {"scope": ["acme/instagram"]}})
    a.fact = m.write(OWNER, "acme", {"type": "fact", "title": "4,210 followers", "body": "on 2026-11-01"})
    gone = m.write(OWNER, "acme", {"type": "contact", "title": "Jane FORGOTTENMARKER"})
    m.forget(OWNER, gone["id"], "asked to be forgotten")
    a.gone = gone
    m.write(OWNER, "other", {"type": "fact", "title": "OTHERWSMARKER"})
    yield a, b
    a.close()
    b.close()


def export(node, dest: Path, workspace: str = "acme"):
    return asyncio.run(wsexport.export_workspace(node.sources(), workspace, dest, passphrase=PASSPHRASE, log_n=LOG_N))


def do_import(node, path: Path, **options):
    contents = wsread.read_export(path, wsread.staging_dir(node.home, "import"), passphrase=PASSPHRASE)
    try:
        plan = asyncio.run(wsimport.plan_import(contents, node.sources(), ImportOptions(**options)))
        assert plan.ok, plan.blockers
        return plan, asyncio.run(wsimport.apply_import(plan, contents, node.sources()))
    finally:
        contents.discard()


def test_memory_moves_with_its_chain_intact(nodes, tmp_path):
    a, b = nodes
    out = tmp_path / "acme.openajar"
    result = export(a, out)
    assert result.counts["memory_entries"] == 3 and result.counts["memory_versions"] == 3
    with out.open("rb") as raw:
        plain = gzip.decompress(age.Decryptor(raw, PASSPHRASE).read())
    assert b"workspace/memory.jsonl" in plain and b"No posts on Sundays" in plain
    assert b"FORGOTTENMARKER" not in plain and b"OTHERWSMARKER" not in plain
    plan, done = do_import(b, out)
    assert plan.memory == {"entries": 3, "versions": 3, "forgotten": 1, "action": "import"}
    assert done.memory["ok"] and done.memory["same_hashes"]
    assert b.memory.verify("acme") == {**a.memory.verify("acme"), "workspace": "acme"}
    entry = b.memory.get(OWNER, a.decision["id"]).data
    assert entry["trust"] == "owner" and entry["version_hash"] == a.decision["version_hash"]
    assert entry["prov"]["imported_from"]["node"] == "node-a"
    assert entry["prov"]["evidence"][0]["summary"] == "the profile page"   # the snapshot travelled
    gone = b.memory.get(OWNER, a.gone["id"]).data
    assert gone["state"] == "forgotten" and gone["title"] is None and gone["tombstone"]["reason"]
    assert b.memory.decisions_for_lane("acme", "acme/instagram")


def test_as_moves_memory_keys_and_scopes(nodes, tmp_path):
    a, b = nodes
    out = tmp_path / "acme.openajar"
    export(a, out)
    plan, done = do_import(b, out, rename_to="acme2")
    assert plan.memory["action"] == "import_renamed" and done.memory["ok"] and not done.memory["same_hashes"]
    entry = b.memory.get(OWNER, a.decision["id"], workspace="acme2").data
    assert entry["key"] == "acme2.posting.sunday" and entry["attrs"]["scope"] == ["acme2/instagram"]
    assert entry["prov"]["imported_from"]["version_hash"] == a.decision["version_hash"]
    assert b.memory.verify("acme2")["ok"] and not b.memory.has_memory("acme")


def test_an_import_never_merges_memory(nodes, tmp_path):
    a, b = nodes
    out = tmp_path / "acme.openajar"
    export(a, out)
    b.memory.write(OWNER, "acme", {"type": "fact", "title": "already here"})
    contents = wsread.read_export(out, wsread.staging_dir(b.home, "import"), passphrase=PASSPHRASE)
    try:
        plan = asyncio.run(wsimport.plan_import(contents, b.sources(), ImportOptions()))
        assert any("already has memory" in x for x in plan.blockers)
    finally:
        contents.discard()


def test_a_failed_import_takes_its_memory_out_again(nodes, tmp_path, monkeypatch):
    a, b = nodes
    out = tmp_path / "acme.openajar"
    export(a, out)
    contents = wsread.read_export(out, wsread.staging_dir(b.home, "import"), passphrase=PASSPHRASE)
    try:
        plan = asyncio.run(wsimport.plan_import(contents, b.sources(), ImportOptions()))

        def adopt(s, *, enabled):
            raise RuntimeError("disk full")

        monkeypatch.setattr(b.scheduler, "adopt", adopt)
        with pytest.raises(wsimport.ImportFailed, match="removed again"):
            asyncio.run(wsimport.apply_import(plan, contents, b.sources()))
    finally:
        contents.discard()
    assert not b.memory.has_memory("acme") and "acme" not in b.memory.stores.workspaces()


def test_a_format_1_export_still_imports_without_memory(nodes, tmp_path, monkeypatch):
    a, b = nodes
    monkeypatch.setattr(wsexport, "FORMAT_VERSION", 1)
    out = tmp_path / "v1.openajar"
    export(a, out)
    monkeypatch.undo()
    plan, done = do_import(b, out)
    assert plan.memory["action"] == "none" and done.memory == {}
    assert not b.memory.has_memory("acme")


def test_a_workspace_without_memory_exports_an_empty_member(nodes, tmp_path):
    a, b = nodes
    a.workspaces.insert_workspace({"id": "empty", "name": "Empty", "color": None, "default_proxy": None,
                                   "notes": "", "created_at": "2026-11-01T00:00:00+00:00",
                                   "updated_at": "2026-11-01T00:00:00+00:00"})
    out = tmp_path / "empty.openajar"
    assert export(a, out, workspace="empty").counts["memory_entries"] == 0
    _plan, done = do_import(b, out)
    assert done.memory == {}
