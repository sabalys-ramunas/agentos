"""QA bug hunt on workspace memory Brief C (tools, export and import, hash chain, forgetting).

Each xfail pins one defect found in the recent merges.
"""

from __future__ import annotations

import sqlite3

import pytest
from testkit.memory import fake_memory

from agentos.memory.store import WorkspaceDB


@pytest.fixture
def mem(tmp_path):
    m = fake_memory(tmp_path)
    yield m
    m.service.close()


def test_qa_memory_1_verify_catches_a_trust_edited_in_the_entry_index(mem):
    out = mem.service.write(mem.agent(), "biz-a", {"type": "fact", "title": "IG biz-a had 4,210 followers"})
    assert out["trust"] == "agent"
    path = mem.service.stores.path("biz-a")
    mem.service.close()
    # an edit on disk to the table the chain does not cover (no trigger guards it)
    con = sqlite3.connect(str(path))
    con.execute("UPDATE entries SET trust = 'owner' WHERE id = ?", (out["id"],))
    con.commit()
    con.close()
    check = WorkspaceDB(path, "biz-a").verify()
    assert check["ok"] is False, check
