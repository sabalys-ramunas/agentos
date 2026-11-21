"""A recent still of each environment's screen, for its picture on the app's Environments list and Home.

The picture (web/DESIGN.md, "The screen's picture") is what a person would see if they opened the live view,
small enough for a card. This keeps one still per environment: the desktop read once with screen_ctl.py's
look (openajar.environments.screen, the same reader screen_snapshot uses), scaled to STILL_SIDE pixels on its
longest side, held in the node's memory and never written to disk.

When: only while someone looks at a list that shows the pictures (the listing route asks with `screens=1`,
which the Environments page and Home send and the rail's counts do not), at most once every REFRESH_S per
environment, at most RUNNING_MAX at a time, and never inside the request: the list answers with the still it
holds and the next read has the new one.

Never:
- for a machine that is not up and awake (starting, closing, saved, closed, failed) or whose desktop does not
  run: asking would wake nothing, but there is nothing to see either, and a held still is dropped;
- while a person has control of the screen (its browser lane is not the agent's: taken over, stopped, or held
  for a check). They may be typing a password. The held still is dropped, a still being taken is thrown away,
  and a still taken before a take over is not shown again after it (`since` must match);
- as the agent's use of the machine: the read is the node's own command, so the terminal lane's last use is put
  back afterwards, as sshaccess.ssh_seen and updates do. Otherwise watching a list would keep every machine from
  going idle and being saved. An agent's own call in the meantime still counts: it is noted on the environment
  (last_call_at), which the idle rule reads too.

Who may see it: the app's route serves it with the same sign-in and the same access as watching the live view
(openajar.api.web.environments).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from openajar.core.models import ControlState, utcnow
from openajar.environments.activity import own

log = logging.getLogger("openajar.environments")

STILL_SIDE = 640           # about a card's width at two pixels a point
PNG_MAX = 320 << 10        # screen_ctl.py drops to 256 colours past this
REFRESH_S = 60             # a still is taken again at most this often, and only while someone looks
SHOWN_S = 5 * 60           # an older still is not shown: the picture is drawn from the state instead
TAKE_S = 15.0              # the read's own limit on the machine
RUNNING_MAX = 2            # reads at once, across every environment
SERVICE = "environment_stills"


@dataclass(frozen=True)
class Still:
    png: bytes
    width: int
    height: int
    taken_at: datetime
    since: datetime | None  # the screen's control record when it was taken: a later take over drops it


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


class Stills:
    def __init__(self, runtime: Any, clock: Any = utcnow) -> None:
        self.runtime = runtime
        self._clock = clock
        self._held: dict[str, Still] = {}
        self._asked: dict[str, datetime] = {}
        self._running: dict[str, asyncio.Task[None]] = {}
        self._gate: asyncio.Semaphore | None = None

    # ------------------------------------------------------------------ may it be seen

    def _mgr(self) -> Any:
        return self.runtime.get("environments")

    def _control(self, env: Any) -> tuple[bool, datetime | None]:
        """(the agent has the screen, since when the record says so). No lane record yet: nobody holds it."""
        lanes = self.runtime.get("lanes")
        try:
            control = lanes.control(env.browser_lane) if lanes is not None else None
        except Exception:  # noqa: BLE001 - the browser lane is not there (yet): the agent's
            control = None
        if control is None:
            return True, None
        return getattr(control, "state", None) == ControlState.AGENT, getattr(control, "since", None)

    def awake(self, env: Any) -> bool:
        """Up, awake and showing its desktop: the only machines a still is taken of or shown for."""
        if not getattr(env, "open", False) or getattr(env, "state", None) != "ready":
            return False
        if getattr(env, "browser", None) != "vm":
            return False
        mgr = self._mgr()
        try:
            return mgr is not None and mgr.vm_browsers().screen_size(env.id) is not None
        except Exception:  # noqa: BLE001 - no desktop service: no screen to show
            return False

    # ------------------------------------------------------------------ what the list shows

    def get(self, env: Any) -> Still | None:
        """The still to show for `env`, or None (and a still that may no longer be shown is dropped)."""
        still = self._held.get(env.id)
        if still is None:
            return None
        agent, since = self._control(env)
        fresh = self._clock() - still.taken_at <= timedelta(seconds=SHOWN_S)
        if not (agent and since == still.since and fresh and self.awake(env)):
            self._held.pop(env.id, None)
            return None
        return still

    def looked_at(self, envs: list[Any]) -> int:
        """Someone looks at these environments' pictures: take a still of each that may have one and has none
        newer than REFRESH_S, in the background. Returns how many were started."""
        now = self._clock()
        started = 0
        for env in envs:
            if env.id in self._running:
                continue
            last = self._asked.get(env.id)
            if last is not None and now - last < timedelta(seconds=REFRESH_S):
                continue
            if not self.awake(env) or not self._control(env)[0]:
                self._held.pop(env.id, None)
                continue
            self._asked[env.id] = now
            task = asyncio.get_running_loop().create_task(self._take(env), name=f"environment-still-{env.id}")
            self._running[env.id] = task
            task.add_done_callback(lambda _t, eid=env.id: self._running.pop(eid, None))
            started += 1
        self._forget_closed({e.id for e in envs})
        return started

    def _forget_closed(self, listed: set[str]) -> None:
        """Drop what is held for environments that are gone, so the memory stays at one still per open one."""
        mgr = self._mgr()
        for eid in [e for e in self._held if e not in listed]:
            try:
                env = mgr.get(eid) if mgr is not None else None
            except Exception:  # noqa: BLE001 - not found: gone
                env = None
            if env is None or not getattr(env, "open", False):
                self._held.pop(eid, None)
                self._asked.pop(eid, None)

    # ------------------------------------------------------------------ the machine

    async def _take(self, env: Any) -> None:
        if self._gate is None:
            self._gate = asyncio.Semaphore(RUNNING_MAX)
        async with self._gate:
            agent, since = self._control(env)
            if not agent:
                return
            try:
                png, width, height = await self._read(env)
            except Exception as e:  # noqa: BLE001 - no still this time: the picture is drawn from the state
                log.debug("environment %s: no still of its screen: %s", env.id, e)
                return
            mgr = self._mgr()
            try:
                now_env = mgr.get(env.id) if mgr is not None else env
            except Exception:  # noqa: BLE001 - closed meanwhile
                return
            agent_now, since_now = self._control(now_env)
            # a person took the screen while it was read (or took it and gave it back): what it shows is theirs
            if not (agent_now and since_now == since and self.awake(now_env)):
                self._held.pop(env.id, None)
                return
            self._held[env.id] = Still(png=png, width=width, height=height, taken_at=self._clock(), since=since)

    async def _read(self, env: Any) -> tuple[bytes, int, int]:
        """The desktop as a PNG of at most STILL_SIDE pixels a side, through screen_ctl.py's look. The node's
        own command: the terminal lane's last use is put back after it."""
        from openajar.environments import screen

        envs = self._mgr().envs()
        lane = env.terminal_lane
        before = None
        with contextlib.suppress(Exception):
            before = envs.store.last_used(lane)
        body = json.dumps({"script": screen.ctl_script(), "action_kind": "look", "image": True,
                           "max_side": STILL_SIDE, "png_max": PNG_MAX})
        try:
            res = await envs.exec(lane, screen.ctl_command(), stdin=body, timeout_s=TAKE_S,
                                  max_bytes=PNG_MAX * 4 // 3 + (64 << 10), **own(envs))
        finally:  # watching is no use of the machine (sshaccess.ssh_seen does the same)
            with contextlib.suppress(Exception):
                envs.store.touch(lane, at=_aware(before) if before is not None else env.created_at)
        line = next((ln for ln in reversed((res.stdout or "").splitlines()) if ln.startswith("{")), "")
        out = json.loads(line) if line else {}
        if not isinstance(out, dict) or out.get("ok") is not True or res.exit_code != 0:
            raise RuntimeError(f"the screen did not answer (exit {res.exit_code})")
        return screen.clean_png(out)


def stills_of(runtime: Any) -> Stills:
    """The node's one Stills, made on first use."""
    held = runtime.get(SERVICE)
    if held is None:
        held = Stills(runtime)
        runtime.register(SERVICE, held)
    return held
