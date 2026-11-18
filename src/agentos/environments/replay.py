"""Replay: what happened in an environment's browser, as a timeline of frames kept on the node after
the VM is gone. A mixin of EnvironmentManager; the files are agentos.environments.replay_store.

A frame is a small JPEG of the browser's viewport (FRAME_WIDTH wide when Pillow is there to shrink it,
JPEG quality FRAME_QUALITY), taken by the node:

- after each agent call on the environment's browser lane that can change the page (PAGE_TOOLS),
  with the tool and the summary record_call keeps for it (scrubbed as the audit log's are);
- every EVERY_S while a person has taken the browser over in the live view (`person`), and while the
  page keeps changing after an agent call (`change`: an animation, a dev server rebuilding), at most
  CHANGE_FRAMES_MAX in a row, stopping at the first one that shows no change;
- when the environment is saved and when it closes (`save`, `close`: its last state).

A frame identical to the one before is dropped. Taking one never gets in the agent's way: the lane
manager hands out the page only while nobody is on the lane or waits for it (frame_turn), the frame
takes no place on the lane, so no call waits behind it, and a call that comes while it is taken makes
the frame go (it could show that call half way). It is skipped, never waited for. It is a screenshot
of the viewport only: no pointer, focus, scroll or tab changes (caret="initial" injects no style).
Only an environment's own browser `env/<id>-browser` is ever pictured: it holds no account and no
vault entry. An account lane never is.

The owner reads the timeline (frames with links signed like the proofs' and revoked with the viewer
key, the calls interleaved by time) through the web API and `agentos environments replay`; an agent
sees only environment()'s `replay` summary. environments.toml: `replay` (default true) and
`replay_days` (default 14, how long a closed environment's replay stays).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import logging
import struct
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from agentos.core import clock
from agentos.core.errors import NotFound
from agentos.environments.models import WORKSPACE, Environment
from agentos.environments.replay_store import ReplayStore, is_frame_id, iso_ms, public

log = logging.getLogger("agentos.environments")

FRAME_WIDTH = 1280        # a frame's width when Pillow can shrink the browser's own (1920 on the node)
FRAME_QUALITY = 60        # JPEG quality
EVERY_S = 3.0             # how often a person's take-over and a changing page are pictured
SETTLE_S = 0.4            # after an agent's call, before its frame: let the page paint
CHANGE_FRAMES_MAX = 20    # change frames after one agent call (an animation that never settles)
SHOT_TIMEOUT_S = 5.0
REPLAY_LINK_S = 900       # a signed link to a frame lives this long (the timeline gives fresh ones)
REPLAY_PURPOSE = "replay"
TIMELINE_LIMIT, TIMELINE_MAX = 500, 2000
# the agent calls on an environment's browser that can change what it shows
PAGE_TOOLS = frozenset({"browser_navigate", "browser_act", "browser_scroll", "browser_upload",
                        "browser_dialog_answer", "browser_tab", "browser_wait"})


@dataclass
class _Track:
    """One environment's replay while it is open: the last kept frame and the change it follows."""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    exact: str | None = None    # the last kept frame's pixels, hashed
    coarse: str | None = None   # and a coarse print of it (a blinking caret is no change)
    follow: int = 0             # change frames still to look for after an agent call


def frame_lane(env: Environment) -> str | None:
    """The one lane a frame of this environment may picture: its own browser, env/<id>-browser."""
    lane = env.browser_lane
    return lane if lane == f"{WORKSPACE}/{env.id}-browser" else None


class Replay:
    """For EnvironmentManager (its home, settings, store, runtime, clock, _scrub, _tokens and _closed)."""

    # ================================================================ state
    def _replay(self) -> dict[str, Any]:
        state = self.__dict__.get("_replay_state")
        if state is None:
            state = self.__dict__["_replay_state"] = {"store": ReplayStore(self.home), "tracks": {},
                                                      "tasks": set(), "loop": None}
        return state

    def replay_store(self) -> ReplayStore:
        return self._replay()["store"]

    def _track(self, env_id: str) -> _Track:
        tracks = self._replay()["tracks"]
        if env_id not in tracks:
            tracks[env_id] = _Track()
        return tracks[env_id]

    def _spawn_replay(self, coro: Any) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:  # no loop (a CLI): nothing to picture
            coro.close()
            return
        tasks = self._replay()["tasks"]
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    def replay_start(self) -> None:
        state = self._replay()
        if state["loop"] is None:
            state["loop"] = asyncio.create_task(self._replay_loop(), name="environment-replay")

    async def replay_stop(self) -> None:
        state = self._replay()
        tasks = [t for t in (state["loop"], *state["tasks"]) if t is not None]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        state["loop"] = None
        state["tasks"].clear()

    # ================================================================ triggers
    def replay_noted(self, env: Environment, lane_id: str, call: Any) -> None:
        """record_call's call: into the replay's call log, and after a call that can change the page, a
        frame of it (taken in the background, never in the call's way)."""
        if not self.settings.replay or not env.open:
            return
        browser = lane_id == env.browser_lane
        try:
            self.replay_store().add_call(env.id, {
                "at": iso_ms(call.at), "tool": call.tool, "summary": call.summary, "ok": call.ok,
                "error": call.error, "exit_code": call.exit_code, "lane": "browser" if browser else "terminal"})
        except (OSError, ValueError):
            log.warning("replay: could not log a call of environment %s", env.id, exc_info=True)
        if browser and call.tool in PAGE_TOOLS:
            self._spawn_replay(self._frame_after(env.id, call.tool, call.summary))

    async def _frame_after(self, env_id: str, tool: str, summary: str) -> None:
        await asyncio.sleep(SETTLE_S)
        try:
            env = self.store.get(env_id)
        except (NotFound, ValueError, KeyError):
            return
        if env.open:
            await self.take_frame(env, "agent", tool=tool, summary=summary)
            self._track(env_id).follow = CHANGE_FRAMES_MAX

    async def _replay_loop(self) -> None:
        while True:
            await asyncio.sleep(EVERY_S)
            try:
                await self._replay_tick()
            except Exception:
                log.warning("replay: a tick failed", exc_info=True)

    async def _replay_tick(self) -> None:
        """A person's take-over, and a page still changing after an agent call: a frame of each."""
        if not self.settings.replay:
            return
        lanes = self._get("lanes")
        person = getattr(lanes, "person_in_control", None)
        tracks = self._replay()["tracks"]
        for env in self.store.open():
            if env.state != "ready":
                continue
            if callable(person) and person(env.browser_lane):
                await self.take_frame(env, "person")
                continue
            track = tracks.get(env.id)
            if track is not None and track.follow > 0:
                got = await self.take_frame(env, "change")
                track.follow = 0 if got == "same" else track.follow - 1

    async def replay_last(self, env: Environment, cause: str) -> None:
        """The browser's last state before a save or a close. Never raises, never waits long."""
        try:
            await asyncio.wait_for(self.take_frame(env, cause), SHOT_TIMEOUT_S + 3)
        except Exception:
            log.info("replay: no %s frame of environment %s", cause, env.id, exc_info=True)
        if cause == "close":
            self._replay()["tracks"].pop(env.id, None)

    # ================================================================ a frame
    async def take_frame(self, env: Environment, cause: str, *, tool: str | None = None,
                         summary: str | None = None) -> str:
        """Picture the environment's browser now: "kept", "same" (no change: dropped), "skipped" (the
        lane is busy, closed or not there) or "off"."""
        if not self.settings.replay:
            return "off"
        lane = frame_lane(env)
        turn_of = getattr(self._get("lanes"), "frame_turn", None)
        if lane is None or not callable(turn_of) or self.browser_environment(lane) is None:
            return "skipped"  # not an open environment's own browser
        track = self._track(env.id)
        if track.lock.locked() and cause in ("change", "person"):
            return "skipped"
        async with track.lock:
            turn = turn_of(lane)
            if turn is None:
                return "skipped"
            shot = await _shoot(turn.page)
            if shot is None or not turn.still_quiet():
                return "skipped"  # a call came while it was taken: it could show that call half way
            raw, url, title, ms = shot
            jpeg, width, height, exact, coarse = await asyncio.to_thread(prepare, raw)
            if exact == track.exact or (cause in ("change", "person") and coarse == track.coarse):
                return "same"
            rec = self.replay_store().add_frame(env.id, jpeg, {
                "at": iso_ms(self._clock()), "cause": cause, "tool": tool, "summary": summary,
                "page_url": self._scrub(url, max_len=300) or None, "page_title": self._scrub(title, max_len=300) or None,
                "width": width, "height": height, "fp": coarse, "ms": ms}, closed=self._closed)
            if rec is None:
                return "skipped"
            track.exact, track.coarse = exact, coarse
            log.debug("replay: %s frame of %s, %d bytes, %d ms", cause, env.id, len(jpeg), ms)
            return "kept"

    # ================================================================ reads
    def replay_summary(self, env: Environment) -> dict | None:
        """environment()'s `replay`: {frames, since, bytes}, or None when the node keeps none."""
        if not self.settings.replay:
            return None
        try:
            return self.replay_store().summary(env.id)
        except Exception:  # noqa: BLE001 - the environment's state without it rather than none
            return None

    def replay_frame(self, env_id: str, frame_id: str) -> bytes | None:
        return self.replay_store().frame_bytes(env_id, frame_id)

    def _replay_link(self, env_id: str) -> tuple[str, str, str] | None:
        """(the link's start, its token, when it ends) for the frames of one environment, or None."""
        tokens, config = self._tokens(), self._get("config")
        if tokens is None or config is None:
            return None
        try:
            token, expires = tokens.sign_blob(REPLAY_PURPOSE, env_id, REPLAY_LINK_S)
        except Exception:
            log.warning("could not sign a link to the replay of %s", env_id, exc_info=True)
            return None
        base = f"http://localhost:{config.mcp_port}/view/{WORKSPACE}/{env_id}-browser/replay"
        return base, token, clock.iso(datetime.fromtimestamp(expires, UTC), z=True)

    def signed_frame(self, lane_workspace: str, lane_name: str, frame_id: str, token: str | None) -> bytes | None:
        """The JPEG a signed link names (the viewer's GET /view/env/<id>-browser/replay/<frame>.jpg), or
        None when the link is not valid, expired, or for another environment."""
        if lane_workspace != WORKSPACE or not lane_name.endswith("-browser") or not is_frame_id(frame_id):
            return None
        env_id = lane_name.removesuffix("-browser")
        tokens = self._tokens()
        if tokens is None or tokens.open_blob(REPLAY_PURPOSE, token) != env_id:
            return None
        return self.replay_frame(env_id, frame_id)

    def replay_timeline(self, env: Environment, *, src: str | None = None, after: str | None = None,
                        limit: int = TIMELINE_LIMIT) -> dict:
        """The owner's timeline of one environment: its frames and its calls, oldest first (see
        docs/ENVIRONMENTS.md, "Replay"). `src`: the app's own route for a frame's JPEG. `after`: only
        what came after this time (the last page's `next`)."""
        store = self.replay_store()
        frames = store.frames(env.id)
        calls = store.calls(env.id) or [_call_item(c) for c in env.calls]  # one from before the replay
        link = self._replay_link(env.id)
        items: list[dict] = []
        for r in frames:
            item = {"kind": "frame", **public(r)}
            if link is not None:
                item["url"] = f"{link[0]}/{r['id']}.jpg?t={link[1]}"
            if src:
                item["src"] = f"{src}/{r['id']}.jpg"
            items.append(item)
        items.extend({"kind": "call", **c} for c in calls)
        items.sort(key=lambda i: (str(i.get("at") or ""), i["kind"] == "frame"))
        if after:
            items = [i for i in items if str(i.get("at") or "") > after]
        limit = max(1, min(int(limit), TIMELINE_MAX))
        kept = (env.closed_at + timedelta(days=self.settings.replay_days)) if env.closed_at and not env.open else None
        return {
            "environment": {"id": env.id, "title": env.title, "state": env.state, "browser": env.browser,
                            "created_at": clock.iso_or_none(env.created_at, z=True),
                            "closed_at": clock.iso_or_none(env.closed_at, z=True)},
            "replay": {"enabled": bool(self.settings.replay), "frames": len(frames),
                       "since": frames[0].get("at") if frames else None,
                       "until": frames[-1].get("at") if frames else None,
                       "bytes": sum(int(r.get("bytes") or 0) for r in frames),
                       "kept_until": clock.iso_or_none(kept, z=True)},
            "links_expire_at": link[2] if link is not None else None,
            "timeline": items[:limit],
            "next": items[limit - 1]["at"] if len(items) > limit else None,
        }

    def prune_replays(self, now: datetime | None = None) -> int:
        """Remove the replays of environments closed more than replay_days ago (and of ones the node no
        longer knows, by the folder's age). Returns how many went."""
        def ended(env_id: str) -> datetime | None | bool:
            try:
                env = self.store.get(env_id)
            except (NotFound, ValueError, KeyError):
                return False
            return None if env.open else (env.closed_at or env.created_at)

        return self.replay_store().prune(now or self._clock(), self.settings.replay_days, ended)


def _call_item(call: Any) -> dict:
    return {"at": iso_ms(call.at), "tool": call.tool, "summary": call.summary, "ok": call.ok, "error": call.error,
            "exit_code": call.exit_code, "lane": None}


async def _shoot(page: Any) -> tuple[bytes, str, str, int] | None:
    """The viewport as a JPEG, the page's url and title, and how long the shot took (ms); None when the
    page did not give one in time."""
    started = time.monotonic()
    try:
        raw = await asyncio.wait_for(page.screenshot(type="jpeg", quality=FRAME_QUALITY, caret="initial",
                                                     timeout=SHOT_TIMEOUT_S * 1000), SHOT_TIMEOUT_S + 1)
        ms = int((time.monotonic() - started) * 1000)
        title = await asyncio.wait_for(page.title(), 2.0)
    except Exception:  # noqa: BLE001 - a page navigating, closing or stuck: no frame this time
        return None
    if not isinstance(raw, bytes | bytearray):
        return None
    return bytes(raw), str(getattr(page, "url", "") or ""), str(title or ""), ms


def prepare(raw: bytes) -> tuple[bytes, int | None, int | None, str, str]:
    """(the frame's JPEG, its width, its height, a hash of its pixels, a coarse print of them). Shrunk to
    FRAME_WIDTH with Pillow when it is installed; the browser's own JPEG as it is without it."""
    try:
        from PIL import Image
    except ImportError:
        width, height = jpeg_size(raw)
        digest = hashlib.sha256(raw).hexdigest()[:24]
        return raw, width, height, digest, digest
    try:
        with Image.open(io.BytesIO(raw)) as im:
            rgb = im.convert("RGB")
    except Exception:  # noqa: BLE001 - not an image Pillow reads: keep it as it came
        width, height = jpeg_size(raw)
        digest = hashlib.sha256(raw).hexdigest()[:24]
        return raw, width, height, digest, digest
    exact = hashlib.sha256(rgb.tobytes()).hexdigest()[:24]
    small = rgb.convert("L").resize((48, 27), Image.Resampling.BOX)
    coarse = hashlib.sha256(bytes(v >> 4 for v in small.tobytes())).hexdigest()[:24]
    width, height = rgb.size
    if width <= FRAME_WIDTH:
        return raw, width, height, exact, coarse
    height = max(1, round(height * FRAME_WIDTH / width))
    out = io.BytesIO()
    rgb.resize((FRAME_WIDTH, height), Image.Resampling.BILINEAR).save(out, "JPEG", quality=FRAME_QUALITY)
    return out.getvalue(), FRAME_WIDTH, height, exact, coarse


def jpeg_size(data: bytes) -> tuple[int | None, int | None]:
    """A JPEG's width and height from its start-of-frame marker."""
    i = 2
    while i + 9 < len(data) and data[i] == 0xFF:
        marker, length = data[i + 1], struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return int(width), int(height)
        i += 2 + length
    return None, None
