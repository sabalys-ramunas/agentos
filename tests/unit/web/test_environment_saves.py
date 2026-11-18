"""Saved environments as the app reads them (agentos.api.web.environments): `saved` in the listing, each
environment's `keep`, and the owner forgetting a save in person. The saves are real save folders of the
node's environments service; the provider's snapshot delete is stubbed."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from starlette.testclient import TestClient

from agentos.core.models import utcnow
from agentos.environments.models import Environment
from agentos.environments.saves import SaveManifest, Snapshot, new_save_id, seal_payload

from .test_environments import ENVS, MEMBER, add_operator, as_member, as_token

KEYS = {"id", "title", "repo", "branch", "head", "saved_at", "snapshot_gb", "files_bytes", "cost_eur_month", "note",
        "agent", "prune_at"}


def put_save(mgr: Any, *, title: str = "Black Friday page", member: str | None = None, family: str = "gr_agent"
             ) -> SaveManifest:
    store = mgr.saves()
    tmp = store.tmp()
    (tmp / "files.tgz").write_bytes(b"\x1f\x8b" + b"0" * 100)
    sid = new_save_id()
    m = SaveManifest(id=sid, family=family, title=title, env_id="e7k2old0000", agent="claude-code", member=member,
                     repo="github.com/me/shop", branch="sale", head="a" * 40, saved_at=utcnow() - timedelta(hours=2),
                     snapshot=Snapshot(id="1234567", gb=2.0), checksums=seal_payload(tmp, sid, None), encrypted=False,
                     bytes=102, agent_note="the banner is next")
    store.commit(tmp, m)
    return m


@pytest.fixture
def mgr(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Any:
    m = client.app.state.api.runtime.get("environments")
    deleted: list[str] = []

    async def forget_snapshot(image_id: str) -> bool:
        deleted.append(image_id)
        return True

    monkeypatch.setattr(m, "_forget_snapshot", forget_snapshot)
    m.deleted_snapshots = deleted
    return m


def test_the_listing_has_saved_and_keep(client: TestClient, mgr: Any) -> None:
    saved = put_save(mgr)
    now = utcnow()
    mgr.store.put(Environment(id="e7k2", title="Open one", grant_id="gr_agent", binding="session:x", state="ready",
                              created_at=now, expires_at=now + timedelta(hours=1), keep="save"))
    body = client.get(ENVS).json()
    assert body["environments"][0]["keep"] == "save"
    [row] = body["saved"]
    assert set(row) == KEYS and row["id"] == saved.id and row["snapshot_gb"] == 2.0
    assert row["cost_eur_month"] == round(2.0 * mgr.settings.snapshot_eur_per_gb_month, 4)
    assert row["note"] == "the banner is next" and row["head"] == "a" * 12
    assert body["snapshots_eur_month"] == row["cost_eur_month"]


def test_the_owner_forgets_a_save_in_person_and_nobody_else_can(client: TestClient, mgr: Any) -> None:
    saved = put_save(mgr)
    assert as_token(client).delete(f"{ENVS}/saves/{saved.id}").status_code in (401, 403)  # never a token
    add_operator(client, scopes=("env",))
    assert as_member(client).delete(f"{ENVS}/saves/{saved.id}").status_code in (403, 404)
    assert client.delete(f"{ENVS}/saves/snotasave00").status_code == 404
    out = client.delete(f"{ENVS}/saves/{saved.id}")
    assert out.status_code == 200, out.text
    assert out.json() == {"forgot": [saved.id], "snapshots_deleted": 1}
    assert mgr.deleted_snapshots == ["1234567"] and mgr.saves().all() == []
    assert client.get(ENVS).json()["saved"] == []


def test_a_member_sees_only_their_own_agents_saves(client: TestClient, mgr: Any) -> None:
    put_save(mgr, title="Not theirs")
    mine = put_save(mgr, title="Theirs", member=MEMBER)
    add_operator(client, scopes=("env",))
    assert [r["id"] for r in as_member(client).get(ENVS).json()["saved"]] == [mine.id]


def test_the_owner_resumes_a_save_in_person_and_nobody_else_can(client: TestClient, mgr: Any,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """POST .../saves/{id}/resume (agentos.environments.waking): the owner in person; the rest is the
    service's own (tests/unit/test_environments_checkpoints.py resumes real saves)."""
    saved = put_save(mgr)
    now = utcnow()
    asked: list[str] = []

    async def resume_saved(ref: str, *, by: str = "owner"):
        asked.append(ref)
        env = Environment(id="e9new000000", title=saved.title, grant_id="gr_agent", binding="kept:e9new000000",
                          state="starting", created_at=now, expires_at=now + timedelta(hours=2), keep="save",
                          resumed={"save_id": saved.id, "from": "snapshot"}, resumed_by="owner")
        mgr.store.put(env)
        return env, "resumed"

    monkeypatch.setattr(mgr, "resume_saved", resume_saved)
    url = f"{ENVS}/saves/{saved.id}/resume"
    assert as_token(client).post(url).status_code in (401, 403)  # never a token
    add_operator(client, scopes=("env",))
    assert as_member(client).post(url).status_code in (403, 404)
    assert client.post(f"{ENVS}/saves/snotasave00/resume").status_code == 404
    assert asked == []
    out = client.post(url)
    assert out.status_code == 200, out.text
    body = out.json()
    assert asked == [saved.id] and body["id"] == "e9new000000" and body["opened"] == "resumed"
    assert body["state"] == "starting" and body["resumed"]["save_id"] == saved.id
    assert "/view/env/e9new000000-browser?t=" in body["links"]["browser"]


def test_one_checkpoints_diff_is_the_owners(client: TestClient, mgr: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    now = utcnow()
    mgr.store.put(Environment(id="e7k2", title="Open one", grant_id="gr_agent", binding="session:x", state="ready",
                              created_at=now, expires_at=now + timedelta(hours=1)))

    async def checkpoint_diff(env, n):
        return {"checkpoint": n, "repos": [{"dir": "/work/shop", "files": [], "patch": "", "truncated": False}]}

    monkeypatch.setattr(mgr, "checkpoint_diff", checkpoint_diff)
    assert client.get(f"{ENVS}/e7k2/checkpoints/3").json()["checkpoint"] == 3
    assert client.get(f"{ENVS}/e7k2/checkpoints/x").status_code == 400
    assert as_token(client).get(f"{ENVS}/e7k2/checkpoints/3").status_code == 200  # the owner's own token, as the CLI
    add_operator(client, scopes=("env",))
    assert as_member(client).get(f"{ENVS}/e7k2/checkpoints/3").status_code in (403, 404)
