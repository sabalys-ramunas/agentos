"""The replay of an environment's browser (agentos.environments.replay and replay_store): what takes a
frame and what drops one, that a frame never waits for or acts on a busy lane, the index and its
limits, retention, the signed links, the owner-only routes, environment()'s summary and the CLI export."""

from __future__ import annotations

import asyncio
import io
import json
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image
from starlette.testclient import TestClient
from test_environments import node, opened  # noqa: F401  (node is a fixture)
from test_environments_isolation import _record, panel  # noqa: F401  (panel is a fixture)
from typer.testing import CliRunner

from agentos.core.models import LaneState, utcnow
from agentos.environments import replay as replay_mod
from agentos.environments import replay_store as store_mod
from agentos.environments import routing
from agentos.environments.replay_cli import export
from agentos.node.lanes.base import _Lane
from agentos.node.lanes.frames import FramesMixin
from agentos.node.viewer_tokens import ViewTokens


def jpeg(color=(255, 255, 255), size=(1920, 1080)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "JPEG", quality=60)
    return out.getvalue()


class ShotPage:
    """The environment's browser page as a frame sees it: a screenshot, its url and its title."""

    def __init__(self):
        self.color = (255, 255, 255)
        self.url = "http://localhost:3000/"
        self.page_title = "Shop"
        self.shots: list[dict] = []
        self.during = None  # runs while the shot is taken (a call arriving)

    async def screenshot(self, **kw):
        self.shots.append(kw)
        if self.during is not None:
            self.during()
        return jpeg(self.color)

    async def title(self):
        return self.page_title


class Shots:
    """The lane manager's side: frame_turn and person_in_control, on the node fixture's fake lanes."""

    def __init__(self):
        self.page = ShotPage()
        self.busy = False
        self.quiet_after = True
        self.person = False
        self.asked: list[str] = []

    def frame_turn(self, lane_id):
        self.asked.append(lane_id)
        if self.busy:
            return None
        return SimpleNamespace(page=self.page, still_quiet=lambda: self.quiet_after)

    def person_in_control(self, lane_id):
        return self.person


@pytest.fixture
def shots(node, monkeypatch):  # noqa: F811
    s = Shots()
    monkeypatch.setattr(node["lanes"], "frame_turn", s.frame_turn, raising=False)
    monkeypatch.setattr(node["lanes"], "person_in_control", s.person_in_control, raising=False)
    monkeypatch.setattr(replay_mod, "SETTLE_S", 0)
    monkeypatch.setattr(replay_mod, "RETRY_S", 0.01)
    monkeypatch.setattr(replay_mod, "AGENT_WAIT_S", 0.3)
    return s


async def settled(mgr):
    while mgr._replay()["tasks"]:
        await asyncio.gather(*list(mgr._replay()["tasks"]))


def frames(mgr, env):
    return mgr.replay_store().frames(env.id)


def token_of(url: str) -> str:
    return parse_qs(urlsplit(url).query)["t"][0]


# ------------------------------------------------------------------ what takes a frame


async def test_a_page_call_on_the_environments_browser_takes_a_captioned_frame(node, shots):  # noqa: F811
    mgr = node["mgr"]
    env = mgr.get((await opened(node))["id"])
    secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    mgr.record_call(env.browser_lane, "browser_navigate", ok=True, summary=f"http://localhost:3000/cart?t={secret}")
    await settled(mgr)
    [frame] = frames(mgr, env)
    assert frame["cause"] == "agent" and frame["tool"] == "browser_navigate"
    assert frame["summary"].startswith("http://localhost:3000/cart") and secret not in frame["summary"]
    assert (frame["page_url"], frame["page_title"]) == ("http://localhost:3000/", "Shop")
    assert frame["width"] == replay_mod.FRAME_WIDTH and frame["height"] == 720  # shrunk from 1920 x 1080
    jpg = mgr.replay_frame(env.id, frame["id"])
    assert jpg.startswith(b"\xff\xd8") and len(jpg) == frame["bytes"]
    assert shots.page.shots == [{"type": "jpeg", "quality": replay_mod.FRAME_QUALITY, "caret": "initial",
                                 "timeout": replay_mod.SHOT_TIMEOUT_S * 1000}]  # the viewport only, no style injected
    mgr.record_call(env.browser_lane, "browser_snapshot", ok=True)  # a read changes nothing: no frame
    mgr.record_call(env.terminal_lane, "terminal_exec", ok=True, summary="npm test", exit_code=0)
    await settled(mgr)
    assert len(frames(mgr, env)) == 1
    calls = mgr.replay_store().calls(env.id)
    assert [(c["tool"], c["lane"]) for c in calls][-3:] == [("browser_navigate", "browser"),
                                                            ("browser_snapshot", "browser"), ("terminal_exec", "terminal")]
    assert calls[-1]["exit_code"] == 0 and secret not in json.dumps(calls)


async def test_a_frame_identical_to_the_one_before_is_dropped(node, shots):  # noqa: F811
    mgr = node["mgr"]
    env = mgr.get((await opened(node))["id"])
    for _ in range(2):
        mgr.record_call(env.browser_lane, "browser_act", ok=True, summary="click e12")
        await settled(mgr)
    assert len(frames(mgr, env)) == 1
    shots.page.color = (10, 120, 200)
    mgr.record_call(env.browser_lane, "browser_act", ok=True, summary="click e13")
    await settled(mgr)
    assert [f["summary"] for f in frames(mgr, env)] == ["click e12", "click e13"]


async def test_a_busy_lane_is_skipped_never_waited_for(node, shots):  # noqa: F811
    mgr = node["mgr"]
    env = mgr.get((await opened(node))["id"])
    shots.busy = True  # another call is on the lane: frame_turn gives no page
    mgr.record_call(env.browser_lane, "browser_navigate", ok=True, summary="http://localhost:3000/")
    await asyncio.wait_for(settled(mgr), 1.0)
    assert frames(mgr, env) == [] and shots.page.shots == []  # nothing asked of the page
    shots.busy = False
    shots.quiet_after = False  # a call came while the shot was taken: it could show that call half way
    mgr.record_call(env.browser_lane, "browser_navigate", ok=True, summary="http://localhost:3000/")
    await settled(mgr)
    assert frames(mgr, env) == [] and shots.page.shots  # taken, then dropped (each time it looked again)


async def test_an_agent_frame_looks_again_while_the_next_call_reads_and_gives_way_to_a_newer_one(node, shots):  # noqa: F811
    mgr = node["mgr"]
    env = mgr.get((await opened(node))["id"])
    shots.busy = True  # the agent's snapshot right after its navigate
    mgr.record_call(env.browser_lane, "browser_navigate", ok=True, summary="http://localhost:3000/")
    await asyncio.sleep(0.05)
    shots.busy = False
    await settled(mgr)
    assert [f["summary"] for f in frames(mgr, env)] == ["http://localhost:3000/"]
    shots.busy = True
    mgr.record_call(env.browser_lane, "browser_act", ok=True, summary="click e7")
    shots.page.color = (90, 90, 90)
    mgr.record_call(env.browser_lane, "browser_act", ok=True, summary="click e8")  # before e7's frame was taken
    await asyncio.sleep(0.05)
    shots.busy = False
    await settled(mgr)
    assert [f["summary"] for f in frames(mgr, env)] == ["http://localhost:3000/", "click e8"]
    assert mgr._track(env.id).pending == 0


async def test_only_the_environments_own_browser_is_ever_pictured(node, shots):  # noqa: F811
    mgr = node["mgr"]
    env = mgr.get((await opened(node))["id"])
    mgr.record_call("personal/reddit", "browser_navigate", ok=True, summary="https://www.reddit.com/")
    mgr.record_call(env.terminal_lane, "browser_navigate", ok=True, summary="http://x.test/")  # not its browser
    mgr.record_call(env.browser_lane, "browser_navigate", ok=True, summary="http://localhost:3000/")
    await settled(mgr)
    await mgr.take_frame(env, "person")
    assert set(shots.asked) == {f"env/{env.id}-browser"}
    assert replay_mod.frame_lane(env) == env.browser_lane


async def test_a_person_in_control_and_a_changing_page_are_pictured_every_few_seconds(node, shots):  # noqa: F811
    mgr = node["mgr"]
    env = mgr.get((await opened(node))["id"])
    shots.person = True
    await mgr._replay_tick()
    await mgr._replay_tick()  # nothing changed: dropped
    shots.page.color = (200, 30, 30)
    await mgr._replay_tick()
    shots.page.color = (255, 255, 255)  # back to the frame before: a caret blinking is no change
    await mgr._replay_tick()
    assert [f["cause"] for f in frames(mgr, env)] == ["person", "person"]
    shots.person = False
    shots.page.color = (30, 200, 30)
    mgr.record_call(env.browser_lane, "browser_act", ok=True, summary="click e4")
    await settled(mgr)
    assert mgr._track(env.id).follow == replay_mod.CHANGE_FRAMES_MAX
    await mgr._replay_tick()  # no change yet: it looks again (a rebuild takes a moment)
    shots.page.color = (30, 30, 200)  # the page changes after the call (a dev server rebuilt)
    await mgr._replay_tick()
    await mgr._replay_tick()
    assert mgr._track(env.id).follow > 0
    await mgr._replay_tick()  # and then it stays still: the change frames stop
    assert mgr._track(env.id).follow == 0
    await mgr._replay_tick()
    assert [f["cause"] for f in frames(mgr, env)] == ["person", "person", "agent", "change"]


async def test_close_takes_the_last_frame_and_the_replay_outlives_the_machine(node, shots):  # noqa: F811
    mgr = node["mgr"]
    env = mgr.get((await opened(node))["id"])
    mgr.record_call(env.browser_lane, "browser_navigate", ok=True, summary="http://localhost:3000/")
    await settled(mgr)
    shots.page.color = (0, 0, 0)
    await node["call"]("environment_close", discard=True)
    assert [f["cause"] for f in frames(mgr, env)] == ["agent", "close"]
    assert mgr.get(env.id).state == "closed" and env.id not in mgr._replay()["tracks"]
    out = mgr.replay_timeline(mgr.get(env.id))
    assert out["replay"]["frames"] == 2 and out["replay"]["kept_until"]
    await mgr.take_frame(mgr.get(env.id), "agent")  # closed: its lane is no environment's any more
    assert len(frames(mgr, env)) == 2


async def test_a_save_takes_a_frame(node, shots):  # noqa: F811
    mgr = node["mgr"]
    env = mgr.get((await opened(node))["id"])
    await mgr.replay_last(env, "save")
    assert [f["cause"] for f in frames(mgr, env)] == ["save"]


async def test_replay_off_keeps_nothing_and_says_nothing(node, shots):  # noqa: F811
    mgr = node["mgr"]
    mgr.settings = mgr.settings.model_copy(update={"replay": False})
    env = mgr.get((await opened(node))["id"])
    mgr.record_call(env.browser_lane, "browser_navigate", ok=True, summary="http://localhost:3000/")
    await settled(mgr)
    assert await mgr.take_frame(env, "agent") == "off"
    assert frames(mgr, env) == [] and mgr.replay_store().calls(env.id) == []
    assert (await node["call"]("environment"))["replay"] is None


async def test_environment_gives_the_agent_only_a_short_summary(node, shots):  # noqa: F811
    mgr = node["mgr"]
    env = mgr.get((await opened(node))["id"])
    mgr.record_call(env.browser_lane, "browser_navigate", ok=True, summary="http://localhost:3000/")
    await settled(mgr)
    seen = (await node["call"]("environment"))["replay"]
    assert set(seen) == {"frames", "since", "bytes"} and seen["frames"] == 1 and seen["bytes"] > 0


def test_an_acts_summary_names_the_action_and_its_ref():
    assert routing._summary("browser_act", {"ref": "e12", "action": "click", "text": "secret"}) == "click e12"
    assert routing._summary("browser_tab", {"index": 2}) == "tab 2"
    assert routing._summary("browser_navigate", {"url": "http://x.test/"}) == "http://x.test/"


# ------------------------------------------------------------------ the lane manager's side


def _quiet_lane():
    page = SimpleNamespace(is_closed=lambda: False, url="http://localhost:3000/")
    lane = _Lane(state=LaneState.WARM)
    lane.session = SimpleNamespace(alive=True, tracker=SimpleNamespace(active=page))
    return lane, page


def test_frame_turn_gives_the_page_only_while_nobody_is_on_the_lane():
    lanes = FramesMixin()
    lane, page = _quiet_lane()
    lanes._lanes = {"env/eabc1234567-browser": lane}
    turn = lanes.frame_turn("env/eabc1234567-browser")
    assert turn is not None and turn.page is page and turn.still_quiet()
    lane.users = 1  # a call came in meanwhile (inside, or waiting for its turn)
    assert not turn.still_quiet() and lanes.frame_turn("env/eabc1234567-browser") is None
    lane.users = 0
    lane.last_active += 1  # a call came and went meanwhile
    assert not turn.still_quiet()
    lane.state = LaneState.ACTIVE
    assert lanes.frame_turn("env/eabc1234567-browser") is None
    lane.state = LaneState.COLD  # never opens a lane to picture it
    assert lanes.frame_turn("env/eabc1234567-browser") is None
    assert lanes.frame_turn("env/enothere000-browser") is None


async def test_frame_turn_refuses_a_lane_that_opens_or_whose_browser_is_gone():
    lanes = FramesMixin()
    lane, _ = _quiet_lane()
    lanes._lanes = {"x/y": lane}
    async with lane.op:  # opening or closing
        assert lanes.frame_turn("x/y") is None
    lane.session.alive = False
    assert lanes.frame_turn("x/y") is None


# ------------------------------------------------------------------ the index and its limits


def _add(store, env_id, cause, color=(0, 0, 0), *, closed=lambda _e: False, at=None):
    rec = {"at": at or store_mod.iso_ms(utcnow()), "cause": cause, "tool": "browser_act" if cause == "agent" else None,
           "summary": "click e1" if cause == "agent" else None, "page_url": "http://localhost:3000/",
           "page_title": "Shop", "width": 64, "height": 36, "fp": "x", "ms": 40}
    return store.add_frame(env_id, jpeg(color, (64, 36)), rec, closed=closed)


def test_the_index_is_json_lines_of_frames(tmp_path):
    store = store_mod.ReplayStore(tmp_path)
    rec = _add(store, "eabcdefghjk", "agent")
    lines = (tmp_path / "environments" / "eabcdefghjk" / "replay" / "index.jsonl").read_text().splitlines()
    assert [json.loads(line)["id"] for line in lines] == [rec["id"]]
    assert set(store_mod.public(rec)) == {"id", "at", "cause", "tool", "summary", "page_url", "page_title", "width",
                                          "height", "bytes"}
    assert store.summary("eabcdefghjk") == {"frames": 1, "since": rec["at"], "bytes": rec["bytes"]}
    assert store.add_frame("eabcdefghjk", b"GIF89a", {"cause": "agent"}, closed=lambda _e: False) is None
    with pytest.raises(ValueError):
        store.add_frame("eabcdefghjk", jpeg(), {"cause": "sometime"}, closed=lambda _e: False)
    assert store.frame_bytes("eabcdefghjk", "../../secrets") is None


def test_change_frames_go_first_and_agent_and_person_frames_stay(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "FRAMES_MAX", 3)
    store = store_mod.ReplayStore(tmp_path)
    e = "eabcdefghjk"
    a1 = _add(store, e, "agent")
    c1 = _add(store, e, "change")
    p1 = _add(store, e, "person")
    a2 = _add(store, e, "agent")  # full: the change frame goes
    assert [f["id"] for f in store.frames(e)] == [a1["id"], p1["id"], a2["id"]]
    assert store.frame_bytes(e, c1["id"]) is None
    assert _add(store, e, "change") is None  # a change frame never pushes out another kind
    close = _add(store, e, "close")  # the end of the replay: the oldest other frame makes room
    assert [f["id"] for f in store.frames(e)] == [p1["id"], a2["id"], close["id"]]
    _add(store, e, "agent")
    _add(store, e, "agent")
    assert store.frames(e)[0]["id"] == close["id"]  # a close frame is never dropped


def test_every_replay_together_stays_under_the_nodes_cap(tmp_path, monkeypatch):
    store = store_mod.ReplayStore(tmp_path)
    one = len(jpeg((0, 0, 0), (64, 36)))
    monkeypatch.setattr(store_mod, "NODE_BYTES_MAX", one * 2 + 10)
    old = _add(store, "eoldoldoldo", "agent")
    _add(store, "enewnewnewn", "agent", closed=lambda e: e == "eoldoldoldo")
    third = _add(store, "enewnewnewn", "agent", (9, 9, 9), closed=lambda e: e == "eoldoldoldo")
    assert third is not None and store.frames("eoldoldoldo") == []  # the closed one's frames went
    assert store.frame_bytes("eoldoldoldo", old["id"]) is None
    assert _add(store, "enewnewnewn", "agent", closed=lambda e: False) is None  # only open ones: skipped
    assert len(store.frames("enewnewnewn")) == 2


def test_the_calls_log_keeps_the_newest(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "CALLS_MAX", 4)
    store = store_mod.ReplayStore(tmp_path)
    for i in range(12):
        store.add_call("eabcdefghjk", {"at": f"2026-11-18T10:00:{i:02d}.000Z", "tool": "terminal_exec", "summary": str(i)})
    kept = [c["summary"] for c in store.calls("eabcdefghjk")]
    assert kept[-1] == "11" and len(kept) <= 5


async def test_replays_are_pruned_replay_days_after_the_close(node, shots):  # noqa: F811
    mgr = node["mgr"]
    env = mgr.get((await opened(node))["id"])
    await mgr.take_frame(env, "agent")
    assert mgr.prune_replays() == 0  # open: kept
    await node["call"]("environment_close", discard=True)
    days = mgr.settings.replay_days
    assert mgr.prune_replays(utcnow() + timedelta(days=days - 1)) == 0
    assert mgr.prune_replays(utcnow() + timedelta(days=days + 1)) == 1
    assert frames(mgr, env) == [] and not (mgr.home / "environments" / env.id / "replay").exists()


# ------------------------------------------------------------------ links and access


@pytest.fixture
def tokens(node, tmp_path):  # noqa: F811
    clock = SimpleNamespace(now=1_800_000_000.0)
    t = ViewTokens(tmp_path / "viewer.key", clock=lambda: clock.now)
    node["rt"].register("viewer_links", t)
    return t, clock


async def test_a_timeline_interleaves_calls_and_frames_with_signed_links(node, shots, tokens):  # noqa: F811
    t, clock = tokens
    mgr = node["mgr"]
    env = mgr.get((await opened(node))["id"])
    mgr.record_call(env.browser_lane, "browser_navigate", ok=True, summary="http://localhost:3000/")
    await settled(mgr)
    mgr.record_call(env.terminal_lane, "terminal_exec", ok=True, summary="npm run build", exit_code=0)
    out = mgr.replay_timeline(env, src="/api/orgs/local/environments/x/replay")
    kinds = [(i["kind"], i["tool"]) for i in out["timeline"]]
    assert kinds[-3:] == [("call", "browser_navigate"), ("frame", "browser_navigate"), ("call", "terminal_exec")]
    frame = next(i for i in out["timeline"] if i["kind"] == "frame")
    port = node["rt"].get("config").mcp_port
    assert frame["url"].startswith(f"http://localhost:{port}/view/env/{env.id}-browser/replay/{frame['id']}.jpg?t=")
    assert frame["src"] == f"/api/orgs/local/environments/x/replay/{frame['id']}.jpg"
    tok = token_of(frame["url"])
    jpg = mgr.replay_frame(env.id, frame["id"])
    assert mgr.signed_frame("env", f"{env.id}-browser", frame["id"], tok) == jpg
    assert mgr.signed_frame("env", "eotherother-browser", frame["id"], tok) is None  # another environment
    assert mgr.signed_frame("personal", f"{env.id}-browser", frame["id"], tok) is None
    assert mgr.signed_frame("env", f"{env.id}-browser", frame["id"], tok + "x") is None
    proof_tok, _ = t.sign_blob("proof", f"{env.id}/{frame['id']}", 60)  # a proof link is no replay link
    assert mgr.signed_frame("env", f"{env.id}-browser", frame["id"], proof_tok) is None
    clock.now += replay_mod.REPLAY_LINK_S + 1
    assert mgr.signed_frame("env", f"{env.id}-browser", frame["id"], tok) is None
    t.revoke()
    fresh = token_of(mgr.replay_timeline(env)["timeline"][-2]["url"])
    assert mgr.signed_frame("env", f"{env.id}-browser", frame["id"], fresh) == jpg
    page = mgr.replay_timeline(env, limit=1)
    assert len(page["timeline"]) == 1 and page["next"] == page["timeline"][0]["at"]
    rest = mgr.replay_timeline(env, after=page["next"])["timeline"]
    assert [i["at"] for i in rest] == [i["at"] for i in out["timeline"][1:]]


def test_the_replay_routes_answer_the_owner_only(panel):  # noqa: F811
    from test_api_members import ORIGIN, join, owner, role
    from test_node_panel import bearer

    api_node, mgr = panel
    boss = owner(api_node)
    env = _record(mgr, "ereplayagnt", member=None, root="gr_owner_agent")
    rec = mgr.replay_store().add_frame(env.id, jpeg((1, 2, 3), (64, 36)), {
        "at": store_mod.iso_ms(utcnow()), "cause": "agent", "tool": "browser_act", "summary": "click e5"},
        closed=lambda _e: False)
    base = f"/api/orgs/local/environments/{env.id}/replay"
    seen = boss.get(base)
    assert seen.status_code == 200, seen.text
    body = seen.json()
    assert set(body) == {"environment", "replay", "links_expire_at", "timeline", "next"}
    [item] = [i for i in body["timeline"] if i["kind"] == "frame"]
    assert item["summary"] == "click e5" and item["src"] == f"{base}/{rec['id']}.jpg"
    jpg = boss.get(item["src"])
    assert jpg.status_code == 200 and jpg.headers["content-type"] == "image/jpeg" and jpg.content.startswith(b"\xff\xd8")
    assert boss.get(f"{base}/fnotthere00.jpg").status_code == 404
    assert boss.get(f"{base}?limit=0").status_code == 400
    _, agent = api_node.policy.issue(subject={"id": "agent-x"}, workspaces=["*"], ceiling="commit",
                                     specials={"vm": {}})
    with TestClient(api_node.app, base_url=ORIGIN) as client:
        for path in (base, item["src"]):
            r = client.get(path, headers=bearer(agent))
            assert r.status_code == 403, r.text
            assert b"\xff\xd8" not in r.content and "click e5" not in r.text
    ann = join(api_node, boss, "ann", [role("operator", scope="env")])
    assert ann.get(base).status_code == 403 and ann.get(item["src"]).status_code == 403


# ------------------------------------------------------------------ the CLI


def test_the_cli_writes_the_frames_and_a_contact_sheet(panel, tmp_path):  # noqa: F811
    from agentos.cli import app

    _, mgr = panel
    env = _record(mgr, "eexportagnt", member=None, root="gr_owner_agent")
    store = mgr.replay_store()
    a = _add(store, env.id, "agent")
    store.add_call(env.id, {"at": store_mod.iso_ms(utcnow()), "tool": "terminal_exec", "summary": "npm test", "ok": True,
                            "lane": "terminal"})
    c = _add(store, env.id, "close", (200, 200, 200))
    out = tmp_path / "out"
    done = export(mgr.home, env.id, out)
    assert done["frames"] == 2 and done["calls"] == 1
    assert (out / "frames" / f"{a['id']}.jpg").read_bytes() == store.frame_bytes(env.id, a["id"])
    assert (out / "frames" / f"{c['id']}.jpg").is_file()
    page = (out / "index.html").read_text()
    assert "click e1" in page and "npm test" in page and f"Replay of {env.title}" in page
    assert "<script src" not in page and "<link" not in page and "—" not in page  # no external assets
    timeline = json.loads((out / "timeline.json").read_text())
    assert [i["kind"] for i in timeline["timeline"]] == ["frame", "call", "frame"]
    ran = CliRunner().invoke(app, ["environments", "--home", str(mgr.home), "replay", env.id, "--out",
                                   str(tmp_path / "again"), "--json"])
    assert ran.exit_code == 0, ran.output
    assert json.loads(ran.output)["frames"] == 2
    missing = CliRunner().invoke(app, ["environments", "--home", str(mgr.home), "replay", "enotherenot"])
    assert missing.exit_code == 1 and "no environment enotherenot" in missing.output
