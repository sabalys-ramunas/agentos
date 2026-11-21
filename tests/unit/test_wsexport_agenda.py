"""Workspace export format 3 carries the agenda's entries that have not run yet, without their
approvals: the importing node brings every one in as a draft to propose again (docs/AGENDA.md)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_wsexport_cli import on, secret_file
from test_wsexport_support import LOG_N, PASSPHRASE, Node

from openajar.agenda.models import Action, ActionKind, Entry, EntryState, When
from openajar.agenda.service import AgendaService
from openajar.agenda.store import DB_NAME, AgendaStore
from openajar.core.runtime import Runtime
from openajar.workspaces import cli
from openajar.workspaces.transfer import export as wsexport
from openajar.workspaces.transfer import import_ as wsimport
from openajar.workspaces.transfer import read as wsread
from openajar.workspaces.transfer.import_ import ImportOptions

SOON = datetime.now(UTC).replace(microsecond=0) + timedelta(days=3)


class AgendaNode(Node):
    def __init__(self, root: Path, name: str, **kw):
        super().__init__(root, name, **kw)
        self.store = AgendaStore(self.home / DB_NAME)
        runtime = Runtime()
        runtime.register("runner", self.scheduler.runner)  # no policy service: everything is the owner's
        self.agenda = AgendaService(self.store, runtime)

    def sources(self):
        src = super().sources()
        src.agenda = self.store
        return src

    def close(self):
        self.store.close()
        super().close()

    def plan_acme(self) -> dict[str, Entry]:
        """A draft, a scheduled entry with its approval, an agent's draft, and two that stay home:
        one that ran, one whose window closed."""
        flow = {"flow": "acme.instagram.post"}
        out = {"draft": self.agenda.draft(title="Renew the domain", action=flow, when={"at": SOON.isoformat()},
                                          notes="two years")}
        sched = self.agenda.draft(title="Send the reminders", action=flow,
                                  when={"not_before": SOON.isoformat(), "not_after": (SOON + timedelta(hours=4)).isoformat()})
        out["scheduled"] = asyncio.run(self.agenda.propose(sched.id))
        out["agent"] = self.agenda.draft(title="Agent's post", action=flow, when={"at": SOON.isoformat()},
                                         principal={"kind": "agent", "id": "acme-agent", "grant_id": self.grant.id})
        ran = self.agenda.draft(title="Export the orders", action=flow, when={"at": SOON.isoformat()})
        ran.state, ran.run_id = EntryState.DONE, "run_x"
        self.store.put(ran)
        closed = self.agenda.draft(title="Old window", action=flow, when={"at": SOON.isoformat()})
        closed.when = When(kind="at", not_before=SOON - timedelta(days=10), not_after=SOON - timedelta(days=9))
        self.store.put(closed)
        now = datetime.now(UTC)
        self.store.insert(Entry(id="ag_00000000c0de", workspace="acme", title="A connector post", created_at=now,
                                updated_at=now, when=When(kind="asap"), state=EntryState.DRAFT,
                                action=Action(kind=ActionKind.CONNECTOR, target="connector.instagram.post_photo",
                                              version="c1", on="acme/brand", lane="acme/instagram",
                                              surface="acme/brand", effect="commit")))
        return out


@pytest.fixture
def nodes(tmp_path):
    a, b = AgendaNode(tmp_path, "node-a"), AgendaNode(tmp_path, "node-b")
    a.build_acme()
    a.entries = a.plan_acme()
    yield a, b
    a.close()
    b.close()


def export(node, dest: Path):
    return asyncio.run(wsexport.export_workspace(node.sources(), "acme", dest, passphrase=PASSPHRASE, log_n=LOG_N))


def do_import(node, path: Path, **options):
    contents = wsread.read_export(path, wsread.staging_dir(node.home, "import"), passphrase=PASSPHRASE)
    try:
        plan = asyncio.run(wsimport.plan_import(contents, node.sources(), ImportOptions(**options)))
        assert plan.ok, plan.blockers
        return plan, asyncio.run(wsimport.apply_import(plan, contents, node.sources()))
    finally:
        contents.discard()


def test_open_entries_travel_without_approvals_and_come_in_as_drafts(nodes, tmp_path):
    a, b = nodes
    assert a.entries["scheduled"].state == EntryState.SCHEDULED and a.entries["scheduled"].approval
    out = tmp_path / "acme.openajar"
    result = export(a, out)
    assert result.counts["agenda_entries"] == 4  # the done entry and the closed window stay home
    assert any("Old window" in n and "window has closed" in n for n in result.notes)
    assert any("travel without their approvals" in n for n in result.notes)
    plan, done = do_import(b, out, reissue_grants=True)
    assert any("come in as drafts: approvals do not travel" in n for n in plan.notes)
    assert any("connector.instagram.post_photo" in n and "not installed" in n for n in plan.notes)
    assert {row["title"] for row in done.agenda} == {"Renew the domain", "Send the reminders", "Agent's post"}
    here = {e.title: e for e in b.store.list(workspace="acme")}
    assert set(here) == {"Renew the domain", "Send the reminders", "Agent's post"}
    for e in here.values():
        assert e.state == EntryState.DRAFT and e.approval is None and e.approved_hash is None and e.run_id is None
        assert "approvals do not travel" in e.reason and e.history[-1]["what"].startswith("imported as a draft")
    assert here["Renew the domain"].notes == "two years"
    assert here["Send the reminders"].when == a.entries["scheduled"].when
    regranted = {g["from"]: g["id"] for g in done.grants}
    agents = here["Agent's post"]
    assert agents.grant_id == regranted[a.grant.id] and agents.principal["grant_id"] == agents.grant_id
    proposed = asyncio.run(b.agenda.propose(here["Renew the domain"].id))  # proposed again on the new node
    assert proposed.state == EntryState.SCHEDULED


def test_under_a_new_id_the_entries_follow_the_workspace(nodes, tmp_path):
    a, b = nodes
    out = tmp_path / "acme.openajar"
    export(a, out)
    _plan, done = do_import(b, out, rename_to="acme2")
    moved = b.store.list(workspace="acme2")
    assert len(moved) == 3 == len(done.agenda)
    assert {e.action.target for e in moved} == {"acme2.instagram.post"}
    assert {e.action.lane for e in moved} == {"acme2/instagram"}
    agents = next(e for e in moved if e.title == "Agent's post")
    assert "not re-issued here" in agents.reason  # its grant stayed behind


def test_a_failed_import_takes_its_entries_back_out(nodes, tmp_path, monkeypatch):
    a, b = nodes
    out = tmp_path / "acme.openajar"
    export(a, out)
    real = wsimport._apply_agenda_entry
    calls = []

    def flaky(row, *args, **kw):
        calls.append(row)
        if len(calls) == 2:
            raise RuntimeError("disk full")
        return real(row, *args, **kw)

    monkeypatch.setattr(wsimport, "_apply_agenda_entry", flaky)
    contents = wsread.read_export(out, wsread.staging_dir(b.home, "import"), passphrase=PASSPHRASE)
    try:
        plan = asyncio.run(wsimport.plan_import(contents, b.sources(), ImportOptions()))
        with pytest.raises(wsimport.ImportFailed, match="agenda entries"):
            asyncio.run(wsimport.apply_import(plan, contents, b.sources()))
    finally:
        contents.discard()
    assert b.store.list() == []


def test_a_format_2_export_imports_with_no_agenda(nodes, tmp_path, monkeypatch):
    a, b = nodes
    monkeypatch.setattr(wsexport, "FORMAT_VERSION", 2)
    out = tmp_path / "v2.openajar"
    assert "agenda_entries" not in export(a, out).counts
    monkeypatch.undo()
    plan, done = do_import(b, out)
    assert plan.agenda == [] and done.agenda == [] and b.store.list() == []


def test_the_import_output_says_entries_come_in_as_drafts(nodes, tmp_path, monkeypatch):
    a, b = nodes
    real = wsexport.export_workspace

    async def quick(*args, **kw):
        return await real(*args, **{**kw, "log_n": LOG_N})

    monkeypatch.setattr(wsexport, "export_workspace", quick)
    managers = {a.home: a.lanes, b.home: b.lanes}
    monkeypatch.setattr(cli, "_lane_manager", lambda cfg: managers[cfg.home])
    a.store.close()
    b.store.close()
    pw = secret_file(tmp_path, "pw", PASSPHRASE)
    out = tmp_path / "acme.openajar"
    done = on(a, monkeypatch, "export", "acme", "--to", str(out), "--passphrase-file", str(pw))
    assert done.exit_code == 0, done.output
    assert "4 agenda entries" in done.output
    dry = on(b, monkeypatch, "import", str(out), "--passphrase-file", str(pw), "--dry-run")
    assert "agenda: 3 entries, in as drafts (approvals do not travel; propose them again)" in dry.output
    real_import = on(b, monkeypatch, "import", str(out), "--passphrase-file", str(pw), "--yes")
    assert real_import.exit_code == 0, real_import.output
    assert "agenda: 3 entries in as drafts. Approvals do not travel between nodes" in real_import.output
    assert "—" not in real_import.output + dry.output + done.output
    a.store = AgendaStore(a.home / DB_NAME)  # so close() has something to close
    b.store = AgendaStore(b.home / DB_NAME)
    assert {e.state for e in b.store.list()} == {EntryState.DRAFT}
