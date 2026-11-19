"""Review repros for the owner's new routes and the web app install (findings R1 to R3).

Each test states what the code should do and is xfail(strict=True) until the fix lands.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
from types import SimpleNamespace

import pytest
from test_environments import node  # noqa: F401  (a fixture)
from test_environments_logins import LOGIN_URL, PASSWORD, TITLE, USER, agent, vault  # noqa: F401

from agentos.environments import logins

# ------------------------------------------------------------------ R1


@pytest.mark.xfail(strict=True, reason="R1: /api/environments/login takes the bare node token (NODE_TOKEN), "
                   "though environments/api.py says every route needs Presence.PERSON")
async def test_r1_the_sign_in_route_needs_a_person_not_the_bare_node_token(agent, tmp_path):  # noqa: F811
    import httpx

    from agentos.api import ApiSettings, build_api

    await logins.add(agent.vault, LOGIN_URL, user=USER, password=PASSWORD, scope={"title": TITLE})
    await agent.call("environment_open", title=TITLE)
    api = build_api(agent.rt, ApiSettings(lane_pump_seconds=0, panel_dir=tmp_path / "no-panel"))
    node_token = agent.rt.get("config").mcp_token  # what an agent with the node's token holds: never a person
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://localhost") as c:
        r = await c.post("/api/environments/login", json={"title": TITLE, "site": "localhost"},
                         headers={"Authorization": f"Bearer {node_token}"})
    assert r.status_code == 403 and "presence_required" in r.text, r.text
    assert agent.site.typed == []


# ------------------------------------------------------------------ R2


def _archive(files: dict[str, bytes]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue())


@pytest.mark.xfail(strict=True, reason="R2: the sha256 comes from the same release as the archive, so a "
                   "replaced asset (gh release upload --clobber) installs; nothing pinned in the package")
def test_r2_an_archive_replaced_together_with_its_sha256_is_refused(tmp_path):
    from agentos.setup import webapp

    evil = _archive({"index.html": b"<script>fetch('/api/...')</script>"})
    served = {}

    def get(url: str, limit: int) -> bytes:
        name = url.rsplit("/", 1)[-1]
        if url.endswith(".sha256"):
            return f"{hashlib.sha256(evil).hexdigest()}  {name.removesuffix('.sha256')}\n".encode()
        served["archive"] = url
        return evil

    with pytest.raises(webapp.WebAppRefused):
        webapp.install(tmp_path, "9.9.9", base="https://example.invalid/releases/download", get=get)


# ------------------------------------------------------------------ R3


def test_r3_paging_the_replay_loses_no_item_at_a_page_edge():
    from agentos.environments.replay import Replay

    at = "2026-11-19T10:00:00.123Z"
    frames = [{"id": "f1", "at": at}]
    calls = [{"at": "2026-11-19T10:00:00.000Z", "tool": "a"}, {"at": at, "tool": "b"}]
    store = SimpleNamespace(frames=lambda _e: frames, calls=lambda _e: calls)
    fake = SimpleNamespace(replay_store=lambda: store, _replay_link=lambda _e: None,
                           replay_kept_until=lambda _e: None, settings=SimpleNamespace(replay=True))
    env = SimpleNamespace(id="eabc123", title="t", state="ready", browser="node", created_at=None,
                          closed_at=None, calls=[])
    seen, after = [], None
    for _ in range(5):
        page = Replay.replay_timeline(fake, env, after=after, limit=2)
        seen += page["timeline"]
        after = page["next"]
        if after is None:
            break
    assert len(seen) == 3, seen  # two calls and one frame


def test_r3_many_items_of_one_millisecond_page_through_once_each():
    from agentos.environments.replay import Replay

    at = "2026-11-19T10:00:00.123Z"
    calls = [{"at": at, "tool": f"t{i}"} for i in range(7)] + [{"at": "2026-11-19T10:00:01.000Z", "tool": "last"}]
    store = SimpleNamespace(frames=lambda _e: [], calls=lambda _e: calls)
    fake = SimpleNamespace(replay_store=lambda: store, _replay_link=lambda _e: None,
                           replay_kept_until=lambda _e: None, settings=SimpleNamespace(replay=True))
    env = SimpleNamespace(id="eabc123", title="t", state="ready", browser="node", created_at=None,
                          closed_at=None, calls=[])
    seen, after = [], None
    for _ in range(10):
        page = Replay.replay_timeline(fake, env, after=after, limit=3)
        seen += [i["tool"] for i in page["timeline"]]
        after = page["next"]
        if after is None:
            break
    assert seen == [f"t{i}" for i in range(7)] + ["last"]
    # a bare time, as the old cursor was, is still read: strictly after it
    assert [i["tool"] for i in Replay.replay_timeline(fake, env, after=at, limit=5)["timeline"]] == ["last"]
