"""The still of an environment's screen for the app's pictures (openajar.environments.stills) and its routes:
taken only while someone looks, at most once a minute, never for a machine that is not awake, never while a
person has control, never counted as the agent's use of the machine, and served only to whoever may watch."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fakes import PNG_1X1
from test_environments_isolation import _record, panel  # noqa: F401  (panel is a fixture)

from openajar.core.models import ControlState
from openajar.environments import stills as stills_mod
from openajar.environments.stills import REFRESH_S, SHOWN_S, Still, Stills, stills_of

T0 = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
BEFORE = T0 - timedelta(minutes=20)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class LaneStore:
    def __init__(self) -> None:
        self.used: dict[str, datetime] = {}

    def last_used(self, lane: str) -> datetime | None:
        return self.used.get(lane)

    def touch(self, lane: str, at: datetime | None = None) -> None:
        self.used[lane] = at or datetime.now(UTC)


class Envs:
    """The env manager's exec, as the machine would answer screen_ctl.py's look."""

    def __init__(self) -> None:
        self.store = LaneStore()
        self.calls: list[dict] = []
        self.during = None  # something that happens while the machine reads its screen

    async def exec(self, lane, cmd, *, stdin=None, timeout_s=None, max_bytes=None, **_):
        self.calls.append({"lane": lane, "req": json.loads(stdin)})
        self.store.touch(lane)  # what the real exec does: it counts as use of the lane
        if self.during:
            self.during()
        out = {"ok": True, "png": base64.b64encode(PNG_1X1).decode(), "size": [1280, 720]}
        return SimpleNamespace(stdout="noise\n" + json.dumps(out) + "\n", stderr="", exit_code=0, timed_out=False)


class Lanes:
    def __init__(self) -> None:
        self.state = ControlState.AGENT
        self.since: datetime | None = None

    def control(self, lane: str):
        return SimpleNamespace(state=self.state, since=self.since)


def env(eid="escreen0001", state="ready", browser="vm"):
    return SimpleNamespace(id=eid, state=state, open=state in ("starting", "ready", "closing"), browser=browser,
                           terminal_lane=f"env/{eid}", browser_lane=f"env/{eid}-browser", created_at=T0 - timedelta(hours=1))


class Mgr:
    def __init__(self, envs: Envs, *envs_open) -> None:
        self._envs = envs
        self.by_id = {e.id: e for e in envs_open}
        self.desktops = {e.id for e in envs_open}

    def envs(self):
        return self._envs

    def get(self, eid):
        return self.by_id[eid]

    def vm_browsers(self):
        return SimpleNamespace(screen_size=lambda eid: (1280, 720) if eid in self.desktops else None)


class Runtime:
    def __init__(self, **services) -> None:
        self.services = services

    def get(self, name):
        return self.services.get(name)

    def register(self, name, service):
        self.services[name] = service


@pytest.fixture
def world():
    e = env()
    envs, lanes, clock = Envs(), Lanes(), Clock()
    envs.store.used[e.terminal_lane] = BEFORE
    mgr = Mgr(envs, e)
    return SimpleNamespace(env=e, envs=envs, lanes=lanes, clock=clock, mgr=mgr,
                           stills=Stills(Runtime(environments=mgr, lanes=lanes), clock=clock))


async def settle(stills: Stills) -> None:
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.gather(*list(stills._running.values()))


async def test_a_look_takes_a_small_still_of_the_desktop_and_it_is_shown(world):
    assert world.stills.looked_at([world.env]) == 1
    await settle(world.stills)
    [call] = world.envs.calls
    assert call["lane"] == world.env.terminal_lane
    assert call["req"]["action_kind"] == "look" and call["req"]["max_side"] == 640 and call["req"]["image"] is True
    assert "script" in call["req"]  # screen_ctl.py goes on stdin, as screen_snapshot sends it
    still = world.stills.get(world.env)
    assert still is not None and still.png == PNG_1X1 and (still.width, still.height) == (1, 1)


async def test_watching_is_not_the_agents_use_of_the_machine(world):
    """The read's exec notes the lane as used; the still puts the time back, so the idle rule and the save
    when idle see no use (the mistake sshaccess.ssh_seen was fixed for)."""
    world.stills.looked_at([world.env])
    await settle(world.stills)
    assert world.envs.store.last_used(world.env.terminal_lane) == BEFORE


async def test_a_machine_never_used_gets_its_opening_time_back(world):
    del world.envs.store.used[world.env.terminal_lane]
    world.stills.looked_at([world.env])
    await settle(world.stills)
    assert world.envs.store.last_used(world.env.terminal_lane) == world.env.created_at


async def test_at_most_once_a_minute_however_often_the_list_is_read(world):
    world.stills.looked_at([world.env])
    await settle(world.stills)
    for seconds in (5, 30, REFRESH_S - 1):
        world.clock.now = T0 + timedelta(seconds=seconds)
        assert world.stills.looked_at([world.env]) == 0
    world.clock.now = T0 + timedelta(seconds=REFRESH_S)
    assert world.stills.looked_at([world.env]) == 1
    await settle(world.stills)
    assert len(world.envs.calls) == 2


@pytest.mark.parametrize("state", ["starting", "closing", "closed", "failed"])
async def test_never_for_a_machine_that_is_not_up_and_awake(world, state):
    asleep = env("esleeping01", state=state)
    world.mgr.by_id[asleep.id] = asleep
    world.mgr.desktops.add(asleep.id)
    assert world.stills.looked_at([asleep]) == 0
    assert world.envs.calls == []


async def test_never_without_a_desktop_or_with_the_browser_on_the_node(world):
    world.mgr.desktops.clear()
    on_node = env("eonnode0001", browser="node")
    assert world.stills.looked_at([world.env, on_node]) == 0
    assert world.envs.calls == []


@pytest.mark.parametrize("state", [ControlState.HUMAN, ControlState.PAUSED])
async def test_no_still_while_a_person_has_control_and_a_held_one_is_dropped(world, state):
    world.stills.looked_at([world.env])
    await settle(world.stills)
    assert world.stills.get(world.env) is not None
    world.lanes.state, world.lanes.since = state, T0
    assert world.stills.get(world.env) is None  # dropped, not only hidden
    world.lanes.state = ControlState.AGENT
    assert world.stills.get(world.env) is None
    world.clock.now = T0 + timedelta(seconds=REFRESH_S)
    world.lanes.state = state
    assert world.stills.looked_at([world.env]) == 0
    assert len(world.envs.calls) == 1


async def test_a_take_over_while_the_screen_is_read_throws_the_still_away(world):
    def take() -> None:
        world.lanes.state, world.lanes.since = ControlState.HUMAN, T0

    world.envs.during = take
    world.stills.looked_at([world.env])
    await settle(world.stills)
    assert world.stills.get(world.env) is None


async def test_a_still_from_before_a_take_over_is_not_shown_after_it(world):
    world.stills.looked_at([world.env])
    await settle(world.stills)
    # a person took the screen and gave it back: the record's `since` moved on
    world.lanes.since = T0 + timedelta(seconds=10)
    assert world.stills.get(world.env) is None


async def test_an_old_still_is_not_shown(world):
    world.stills.looked_at([world.env])
    await settle(world.stills)
    world.clock.now = T0 + timedelta(seconds=SHOWN_S + 1)
    assert world.stills.get(world.env) is None


async def test_a_failed_read_leaves_no_still_and_no_use(world):
    async def broken(*_a, **_k):
        world.envs.store.touch(world.env.terminal_lane)
        raise RuntimeError("ssh went away")

    world.envs.exec = broken
    world.stills.looked_at([world.env])
    await settle(world.stills)
    assert world.stills.get(world.env) is None
    assert world.envs.store.last_used(world.env.terminal_lane) == BEFORE


async def test_closed_environments_are_forgotten(world):
    world.stills.looked_at([world.env])
    await settle(world.stills)
    world.env.state, world.env.open = "closed", False
    world.stills.looked_at([])
    assert world.env.id not in world.stills._held


# ------------------------------------------------------------------ the app's routes

def _still(width=640, height=360) -> Still:
    return Still(png=PNG_1X1, width=width, height=height, taken_at=datetime.now(UTC), since=None)


def test_the_list_gives_the_still_and_asks_for_new_ones_only_with_screens(panel, monkeypatch):  # noqa: F811
    from test_api_members import owner

    api_node, mgr = panel
    boss = owner(api_node)
    e = _record(mgr, "eshowsscrn1", member=None, root="gr_owner_agent")
    stills = stills_of(api_node.runtime)
    looked: list[list[str]] = []
    monkeypatch.setattr(stills, "looked_at", lambda envs: looked.append([x.id for x in envs]) or 0)
    monkeypatch.setattr(Stills, "awake", lambda self, env: True)
    base = "/api/orgs/local/environments"

    plain = boss.get(base)
    assert plain.status_code == 200, plain.text
    assert looked == []  # the rail's counts read the list too: they never ask a machine for anything
    assert plain.json()["environments"][0]["screen"] is None

    stills._held[e.id] = _still()
    listed = boss.get(base, params={"screens": "1"})
    assert looked == [[e.id]]
    screen = listed.json()["environments"][0]["screen"]
    assert screen["kind"] == "screen" and (screen["width"], screen["height"]) == (640, 360)
    assert screen["src"].startswith(f"{base}/{e.id}/screen.png?at=")
    png = boss.get(screen["src"])
    assert png.status_code == 200 and png.content == PNG_1X1 and png.headers["content-type"] == "image/png"
    assert "private" in png.headers["cache-control"]


def test_the_still_route_is_not_found_without_one_or_for_someone_outside(panel, monkeypatch):  # noqa: F811
    from test_api_members import join, owner, role

    api_node, mgr = panel
    boss = owner(api_node)
    ann = join(api_node, boss, "ann", [role("operator", scope="env")])
    e = _record(mgr, "eownersagt3", member=None, root="gr_owner_agent")
    monkeypatch.setattr(Stills, "awake", lambda self, env: True)
    route = f"/api/orgs/local/environments/{e.id}/screen.png"
    assert boss.get(route).status_code == 404  # none taken yet
    stills_of(api_node.runtime)._held[e.id] = _still()
    assert boss.get(route).status_code == 200
    assert ann.get(route).status_code == 404  # not her agent's: as if it were not there
    assert boss.get("/api/orgs/local/environments/enothere000/screen.png").status_code == 404


def test_a_sleeping_machine_has_no_still_in_the_list(panel, monkeypatch):  # noqa: F811
    from test_api_members import owner

    api_node, mgr = panel
    boss = owner(api_node)
    e = _record(mgr, "esleepsnow1", member=None, root="gr_owner_agent")
    stills = stills_of(api_node.runtime)
    stills._held[e.id] = _still()
    monkeypatch.setattr(stills_mod.Stills, "awake", lambda self, env: False)
    listed = boss.get("/api/orgs/local/environments", params={"screens": "1"}).json()["environments"]
    assert listed[0]["screen"] is None
    assert boss.get(f"/api/orgs/local/environments/{e.id}/screen.png").status_code == 404
