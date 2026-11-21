"""Frames: the node's own look at a lane's page, for an environment's replay
(openajar.environments.replay). A mixin of the lane manager.

`frame_turn(lane_id)` hands out the lane's page only while nobody is on the lane and nobody waits for
it (no tool call, flow step, open or close), its browser is up and no dialog is open; else None, and
the caller skips its frame rather than wait. It takes no place on the lane, so no call ever waits
behind a frame: a call that comes while the frame is taken runs at once, and `still_quiet()` then
says the picture may show it half way, so the caller drops it. It never opens a cold lane, makes a
tab, moves the pointer or focuses anything: the caller only asks the page for a screenshot, its url
and its title.

`person_in_control(lane_id)`: a person took the lane over in its live view (their input goes to the
screen, not through a call), which the replay records every few seconds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openajar.core.models import LaneState
from openajar.node.interaction.dialogs import dialog_guard


@dataclass
class FrameTurn:
    """The page of a quiet lane, and whether it stayed quiet while the frame was taken."""
    page: Any
    lane: Any
    epoch: int
    last_active: float

    def still_quiet(self) -> bool:
        lane = self.lane
        return (lane.users == 0 and lane.epoch == self.epoch and lane.last_active == self.last_active
                and lane.state == LaneState.WARM and not lane.op.locked() and not lane.access.busy())


class FramesMixin:
    """For the lane manager (openajar.node.lanes.LaneManagerImpl): its lanes and their control."""

    _lanes: dict[str, Any]

    def frame_turn(self, lane_id: str) -> FrameTurn | None:
        """The lane's page when a frame may be taken of it right now without anyone noticing, else None."""
        lane = self._lanes.get(lane_id)
        if lane is None or lane.state != LaneState.WARM or lane.session is None or lane.evicting:
            return None
        if lane.users or lane.calls or lane.op.locked() or lane.access.busy() or lane.access.queue:
            return None
        session = lane.session
        try:
            if not session.alive:
                return None
            page = getattr(getattr(session, "tracker", None), "active", None)
            if page is None or page.is_closed():
                return None
        except Exception:  # noqa: BLE001 - a browser going away has no frame to give
            return None
        guard = dialog_guard(page)
        if guard is not None and guard.pending_on(page) is not None:  # a frozen page: a shot would hang
            return None
        return FrameTurn(page=page, lane=lane, epoch=lane.epoch, last_active=lane.last_active)

    def person_in_control(self, lane_id: str) -> bool:
        """Has a person taken this lane over in its live view?"""
        held = getattr(self, "_held_by_human", None)
        return bool(held(lane_id)) if callable(held) else False
