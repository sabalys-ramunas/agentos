"""The owner resumes a saved environment without a chat. A mixin of EnvironmentManager.

From the web app's saved list (POST /api/orgs/{org}/environments/saves/{save_id}/resume), the CLI
(`openajar environments resume <title or save id>`) and the Resume button on a saved environment's
live-view page (openajar.node.viewer_saved). It resumes the save as environment_open(title=...) does
(a snapshot on Hetzner or Docker, the paused machine on Morph: seconds), within the same limits (an
environment_limit or environment_full names the fix), but no chat holds it: like a keep "live"
machine its chat let go (binding "kept:<id>", openajar.environments.picking), so the owner can watch
it at once, and the next environment_open of that title by an agent of the family takes it with its
`resumed` record (`opened: "resumed"`). Its idle rule and time limit are any open environment's: one
watched stays open (openajar.environments.watching), an unwatched, unused one is saved again after
idle_minutes. It is never resumed on an agent's behalf: an agent's call with the handle of the closed
one hears how to come back (openajar.environments.comeback.closed_words).
"""

from __future__ import annotations

import logging
from typing import Any

from openajar.core.errors import InvalidRequest, NotFound
from openajar.environments.models import KEEPS, LIVE_STATES, Environment
from openajar.environments.saves import SaveManifest, check_save_id

log = logging.getLogger("openajar.environments")

OWNER = "owner"
SAVE_ID_LEN = 11  # s and 10 letters or digits (openajar.environments.saves.new_save_id)


def is_owner_woken(env: Environment) -> bool:
    """Resumed by the owner and not taken by any chat yet: the next open of its title is a resume."""
    return env.resumed_by == OWNER and env.binding.startswith("kept:") and not env.calls


class Waking:
    """For EnvironmentManager (its saves, store, settings, planning and create)."""

    def save_for_owner(self, ref: str) -> SaveManifest:
        """The save `ref` names: a save id, else the newest save with that title (of any family; two
        families with the title are a choice, by save id)."""
        ref = " ".join(str(ref or "").split())
        if not ref:
            raise InvalidRequest("name the save: its title, or its id (s and 10 letters or digits)")
        saves = self.saves()
        if len(ref) == SAVE_ID_LEN and ref.startswith("s"):
            try:
                return saves.find(check_save_id(ref))[0]
            except (NotFound, InvalidRequest):
                pass
        key = ref.lower()
        found = [m for m in saves.all() if m.title.lower() == key]
        if not found:
            raise NotFound(f"no save is named \"{ref[:80]}\": `openajar environments saves` lists them")
        newest: dict[str, SaveManifest] = {}
        for m in found:  # newest first: the first of each family is its newest
            newest.setdefault(m.family, m)
        if len(newest) > 1:
            listed = ", ".join(f"{m.id} ({m.agent}, saved {m.saved_at:%Y-%m-%d %H:%M} UTC)" for m in newest.values())
            raise InvalidRequest(f"\"{ref[:80]}\" is the title of saves of {len(newest)} agents: {listed}. Name one "
                                 "by its save id")
        return found[0]

    async def resume_saved(self, ref: str, *, by: str = OWNER) -> tuple[Environment, str]:
        """Resume the save `ref` names, held by no chat. Returns the environment and how: "resumed", or
        "already_open" when one with its title is open (it is left as it is)."""
        m = self.save_for_owner(ref)
        live = [e for e in self.store.open()
                if e.family == m.family and e.title.lower() == m.title.lower() and e.state in LIVE_STATES]
        if live:
            return live[0], "already_open"
        from openajar.environments.manager import new_id

        env_id = new_id()
        keep = self.check_keep(m.keep if m.keep in KEEPS and m.keep != "live" else "save")
        async with self.opening(m.family, m.title):
            plan = await self.plan_restore(env_id, m, hours=None, keep=keep)
            self.check_network_plan(self.network_for_open(None, m.repo), plan)
            self.check_limits(plan)
            env = await self.create(env_id, plan, binding=f"kept:{env_id}", title=m.title,
                                    grant_id=m.grant_id or m.family, grant_root=m.family, agent=m.agent, client=None,
                                    repo=m.repo, ref=m.ref, member=m.member, keep=keep,
                                    resumed=self.resuming(m, plan))

        def mark(e: Environment) -> None:
            e.resumed_by = by
            e.keep_signins = m.keep_signins

        env = self.store.update(env.id, mark)
        log.info("environment %s (%s) resumed from save %s by the %s", env.id, env.title, m.id, by)
        return env, "resumed"

    def owner_view(self, env: Environment, how: str) -> dict[str, Any]:
        """What the owner's resume answers: the environment's summary, how, where it came from and its
        live view's link (a page that shows the machine once its browser is up)."""
        out = {**self.summary(env), "opened": how, "resumed": env.resumed, "links": {"browser": None}}
        try:
            from openajar.node.control_tools import take_over_link

            link = take_over_link(self.runtime, env.browser_lane, control=True)
            out["links"] = {"browser": link["url"], "browser_expires_at": link["expires_at"]}
            if not getattr(self, "on_laptop", False):
                out["links"]["tunnel"] = link["tunnel"]
        except Exception:  # the resume stands without a link; the web app has its own view
            log.warning("environment %s: no live-view link for the owner", env.id, exc_info=True)
        return out

    def browser_story(self, lane_id: str) -> dict[str, Any] | None:
        """What a live-view link of an environment's browser shows while that browser is not there
        (openajar.node.viewer_saved): `state` starting (its machine comes up), open_again (its title is
        open as another environment: `id`, `lane`), saved (`save_id`, `saved_at`, `note`, `paused`), or
        closed; with `title`. None: not an environment's browser lane."""
        from openajar.core import clock
        from openajar.environments.watching import env_id_of

        eid = env_id_of(lane_id)
        if eid is None or not str(lane_id).endswith("-browser"):
            return None
        try:
            env = self.store.get(eid)
        except (NotFound, ValueError):
            return None
        out: dict[str, Any] = {"id": env.id, "title": env.title}
        if env.open:
            return {**out, "state": "starting" if env.state in LIVE_STATES else env.state}
        again = next((e for e in self.store.open() if e.family == env.family and e.title.lower() == env.title.lower()
                      and e.state in LIVE_STATES), None)
        if again is not None:
            return {**out, "state": "open_again", "again": again.id, "lane": again.browser_lane}
        m = None
        try:
            m = self.saves().latest(env.family, env.title)
        except Exception:  # no saves folder: it is closed
            log.warning("could not read the saves of %s", env.id, exc_info=True)
        if m is None:
            return {**out, "state": "closed", "closed_at": clock.iso_or_none(env.closed_at, z=True),
                    "reason": env.closed_reason}
        return {**out, "state": "saved", "save_id": m.id, "saved_at": clock.iso_or_none(m.saved_at, z=True),
                "note": m.agent_note, "paused": m.paused is not None, "reason": env.closed_reason}
