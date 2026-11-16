"""The agent's own ssh across a save (agentos.environments.sshaccess): the entry goes when the environment
is saved and its VM deleted, and a resume (a new machine) writes a new one with a new key. HOME is a
temporary folder: nothing reaches this computer's ~/.ssh."""

from __future__ import annotations

import pytest
from test_environments_saves import TITLE, node, opened  # noqa: F401 - node is the fixture

from agentos.environments import sshaccess


@pytest.fixture(autouse=True)
def user_home(tmp_path, monkeypatch):
    user = tmp_path / "user"
    user.mkdir()
    monkeypatch.setenv("HOME", str(user))


async def test_a_save_removes_the_entry_and_the_resume_writes_a_new_one(node):  # noqa: F811
    mgr = node["mgr"]
    mgr.on_laptop = True
    out = await opened(node, keep="save")
    cfg = sshaccess.config_path(mgr.home)
    assert f"Host agentos-{out['id']}" in cfg.read_text() and out["ssh"]["host"] == f"agentos-{out['id']}"
    old_key = (mgr.home / "ssh" / out["id"] / "id_ed25519.pub").read_text()

    closed = await node["call"]("environment_close")
    assert closed["machine"] == "deleted" and closed["saved"]["save_id"]
    assert "agentos-" not in cfg.read_text() and not (mgr.home / "ssh" / out["id"]).exists()

    again = await node["call"]("environment_open", title=TITLE, session="chat-2")
    assert again["state"] == "ready" and again["resumed"] and again["id"] != out["id"]
    rec = node["vms"].store.get(mgr.get(again["id"]).vm_id)
    text = cfg.read_text()
    assert f"Host agentos-{again['id']}" in text and f"HostName {rec.host}" in text
    assert again["ssh"]["command"].endswith(f"agentos-{again['id']}")
    assert (mgr.home / "ssh" / again["id"] / "id_ed25519.pub").read_text() != old_key
