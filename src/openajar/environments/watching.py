"""A person watching an environment counts as use, so the reaper never closes or saves it under their
eyes. A mixin of EnvironmentManager.

Watching is an open live view of its browser (the node's viewer relay, openajar.node.viewer_app, and
the panel's, openajar.api.realtime) or a connection through an environment_port link (a dev server's
page open on the person's computer, openajar.environments.ports). Each one calls `watch_lane` when it
opens and the function it returns when it closes. While one is open the reaper's idle rule
(Activity._last_used) reads the environment as used now; when the last one closes, that moment is
the last use. The record keeps `watched_at`, noted at most once a minute while it is watched, so a
restart of the node still knows. The web app's polling of the environment page is not watching (it
looks at the record, not at the machine) and never counts.

environment() and the owner's detail say `watched`: since when, and by how many (live views and
port connections), or None.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from openajar.core import clock
from openajar.environments.models import ID_RE, WORKSPACE, Environment

log = logging.getLogger("openajar.environments")

NOTE_EVERY_S = 60.0
KINDS = ("live_view", "port")


def env_id_of(lane_id: str) -> str | None:
    """The environment a lane is (env/<id> or env/<id>-browser), by its name alone, else None."""
    workspace, _, name = str(lane_id or "").partition("/")
    eid = name.removesuffix("-browser")
    return eid if workspace == WORKSPACE and ID_RE.match(eid) else None


class Watching:
    """For EnvironmentManager (its store and clock)."""

    def _watches(self) -> dict[str, dict]:
        return self.__dict__.setdefault("_watching", {})

    def watch_lane(self, lane_id: str, kind: str = "live_view") -> Callable[[], None] | None:
        """Someone began to watch the open environment `lane_id` belongs to (`kind`: live_view or port).
        Returns the function to call once when they stop; None for a lane that is no open environment's."""
        eid = env_id_of(lane_id)
        if eid is None or kind not in KINDS:
            return None
        try:
            env = self.store.get(eid)
        except Exception:  # noqa: BLE001 - no such environment: nothing to keep alive
            return None
        if not env.open:
            return None
        w = self._watches().setdefault(eid, {"live_view": 0, "port": 0, "since": self._clock(), "noted": 0.0})
        w[kind] += 1
        self._note_watched(eid, w)
        ended = False

        def stop() -> None:
            nonlocal ended
            if ended:
                return
            ended = True
            w[kind] = max(0, w[kind] - 1)
            if not (w["live_view"] or w["port"]):
                self._watches().pop(eid, None)
                self._note_watched(eid, w, force=True)

        return stop

    def _note_watched(self, eid: str, w: dict, *, force: bool = False) -> None:
        """Keep the time on the record, at most every NOTE_EVERY_S (and always when the last one stops)."""
        import time

        now = time.monotonic()
        if not force and now - w["noted"] < NOTE_EVERY_S:
            return
        w["noted"] = now
        at = self._clock()

        def note(e: Environment) -> bool:
            if not e.open:
                return False
            e.watched_at = at
            return True

        try:
            self.store.update(eid, note)
        except Exception:  # the in-memory count still holds while the node runs
            log.warning("environment %s: could not note that it is watched", eid, exc_info=True)

    def watching_since(self, env: Environment) -> datetime | None:
        """When someone began watching it, while someone does; else None. Noted on the record once a
        minute, so the idle rule reads now."""
        w = self._watches().get(env.id)
        if w is None or not (w["live_view"] or w["port"]):
            return None
        self._note_watched(env.id, w)
        return w["since"]

    def watched(self, env: Environment) -> dict | None:
        """environment()'s `watched`: {since, viewers, live_views, port_connections}, or None."""
        w = self._watches().get(env.id)
        if w is None or not (w["live_view"] or w["port"]):
            return None
        return {"since": clock.iso_or_none(w["since"], z=True), "viewers": w["live_view"] + w["port"],
                "live_views": w["live_view"], "port_connections": w["port"]}
