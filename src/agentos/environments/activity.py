"""What an environment is doing, for the reaper and for slow calls (a mixin of EnvironmentManager):
whether it is busy (a command, a job; with keep "save" a dev server does not count), when it was last
used, and how loaded its machine is when a browser call there was slow or stuck."""

from __future__ import annotations

import contextlib
import re
from datetime import UTC, datetime

from agentos.core.models import LaneState
from agentos.environments.models import Environment


class Activity:
    """For EnvironmentManager (its _get, envs, ports and env_of_lane)."""

    def _busy(self, env: Environment) -> bool:
        envs = self._get("envs")
        if envs is None:
            return False
        with contextlib.suppress(Exception):
            if envs.has(env.terminal_lane) and envs.state(env.terminal_lane) == LaneState.ACTIVE:
                return True
            if any(j.state == "running" for j in envs.job_list(env.terminal_lane)):
                return True
        return False

    async def _busy_for_idle(self, env: Environment) -> bool:
        """Busy, for the idle rule. With keep "save", a job that serves (a dev server: a known server
        command, one that names a port, or the one job left while a port listens) does not keep the
        machine: the save restores the services on resume. A build or a test still does."""
        if env.keep != "save":
            return self._busy(env)
        envs = self._get("envs")
        if envs is None:
            return False
        try:
            if envs.has(env.terminal_lane) and envs.state(env.terminal_lane) == LaneState.ACTIVE:
                return True
            jobs = [j for j in envs.job_list(env.terminal_lane) if j.state == "running"]
        except Exception:  # noqa: BLE001 - a lane that cannot say is not busy
            return False
        left = [j for j in jobs if not serves(j.cmd)]
        if len(left) == 1 and len(jobs) == 1 and not WORK_RE.search(left[0].cmd or "") and await self.ports(env):
            return False  # the one job, not a build or a test, and a port listens: it is the server
        return bool(left)

    def _last_used(self, env: Environment) -> datetime:
        """The last sign of use: an agent's call, a command on the terminal lane, or a person's input
        in the browser's live view (the viewer relay notes each one on the lane's control)."""
        last = env.last_activity()
        envs = self._get("envs")
        with contextlib.suppress(Exception):
            used = envs.store.last_used(env.terminal_lane) if envs is not None else None
            if used is not None:
                used = used if used.tzinfo else used.replace(tzinfo=UTC)
                last = max(last, used)
        lanes = self._get("lanes")
        with contextlib.suppress(Exception):
            human = lanes.get(env.browser_lane).control.last_human_input if lanes is not None else None
            if human is not None:
                last = max(last, human if human.tzinfo else human.replace(tzinfo=UTC))
        return last

    async def load_hint(self, lane_id: str) -> str | None:
        """For a browser call that was slow or stuck on an environment's machine: its load and memory,
        and a bigger size when it is overloaded (agentos.node.lanes slow_hints). None for other lanes."""
        from agentos.environments.models import SIZES
        from agentos.environments.probes import LOAD, load_words

        found = self.env_of_lane(lane_id)
        if found is None or not found[0].open or found[0].state != "ready":
            return None
        env = found[0]
        res = await self.envs().exec(env.terminal_lane, LOAD, timeout_s=6)
        names = list(SIZES)
        size = env.machine.size if env.machine.size in SIZES else None
        at = names.index(size) if size else 0
        bigger = names[at + 1] if at + 1 < len(names) else None
        return load_words(res.stdout, size, bigger)


SERVER_RE = re.compile(
    r"(?:^|[\s/;&|(])(?:dev|serve|server|start|preview|runserver|http\.server|uvicorn|gunicorn|nodemon|storybook|"
    r"webpack-dev-server|flask\s+run|rails\s+s(?:erver)?|hugo\s+server|jekyll\s+serve|vite(?!\s+build))(?=$|[\s;&|)])")


WORK_RE = re.compile(r"(?:^|[\s/;&|(])(?:build|test|tests|lint|check|typecheck|install|ci|pytest|jest|vitest|tsc|"
                     r"cargo\s+build|go\s+build|make)(?=$|[\s;&|)])")


def serves(cmd: str | None) -> bool:
    """A job command that runs a server (a dev server, a static server), not a build or a test: a known
    server word, or a port it names (--port 5173, PORT=3000, localhost:8080)."""
    from agentos.environments.saving import PORT_RE

    text = cmd or ""
    if WORK_RE.search(text) and not PORT_RE.search(text):
        return False
    return bool(SERVER_RE.search(text) or PORT_RE.search(text))
