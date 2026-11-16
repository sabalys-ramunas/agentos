"""Security updates of a keep "live" machine, at a quiet moment (a mixin of EnvironmentManager).

Environment machines never update on their own in a session (agentos.environments.machine.UPDATES_OFF
says why and what the policy is). A keep "live" machine lives for weeks, so the reaper looks after it:
when nobody used it for EnvSettings.upgrade_quiet_minutes and no job runs on it, the security updates
start there (machine.upgrade_script, at most once a day, in a session of their own, restarting nothing);
else it only asks how they stand. Either way the answer goes in the machine's notes (Machine.updates),
which environment() and the panel show: when they were applied, that they wait for a quiet moment, and
when a new kernel waits for a restart.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime, timedelta

from agentos.core.errors import AgentosError
from agentos.environments import machine
from agentos.environments.models import Environment

log = logging.getLogger("agentos.environments")

ASK_EVERY = timedelta(minutes=60)  # how often the reaper asks a live machine about its updates


class Updates:
    """For EnvironmentManager (its _get, store, settings, _busy and _last_used)."""

    async def quiet_upgrade(self, env: Environment, now: datetime) -> str | None:
        """For the reaper, on a ready keep "live" machine: start the security updates when it is quiet and
        they are due, else ask how they stand; note the answer. Returns the note (None: not asked)."""
        asked: dict[str, datetime] = self.__dict__.setdefault("_upgrades_asked", {})
        if env.keep != "live" or env.state != "ready":
            return None
        last = asked.get(env.id)
        if last is not None and now - last < ASK_EVERY:
            return None
        envs = self._get("envs")
        if envs is None:
            return None
        asked[env.id] = now
        quiet = (not self._busy(env)
                 and now - self._last_used(env) >= timedelta(minutes=self.settings.upgrade_quiet_minutes))
        lane = env.terminal_lane
        before = None
        with contextlib.suppress(Exception):
            before = envs.store.last_used(lane)
        try:
            res = await envs.exec(lane, machine.upgrade_script(start=quiet), timeout_s=machine.UPGRADE_S)
        except AgentosError as e:
            log.debug("environment %s: could not ask about its updates: %s", env.id, e)
            return None
        finally:  # the node's own command is no use of the machine (sshaccess.ssh_seen does the same)
            if before is not None:
                with contextlib.suppress(Exception):
                    envs.store.touch(lane, at=before)
        words = machine.updates_words(res.stdout or "", now)
        if "upgrade: started" in (res.stdout or ""):
            log.info("environment %s: security updates started at a quiet moment", env.id)
        if words and words != env.machine.updates:
            def noted(e: Environment) -> None:
                e.machine.updates = words

            with contextlib.suppress(Exception):
                self.store.update(env.id, noted)
        return words
