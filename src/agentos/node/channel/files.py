"""`file` streams: the three images a controller may read from a node (docs/PROTOCOL.md v1).

- `screenshot.png`: what a warm browser lane shows now (the stream's `lane`);
- `approvals/{id}/preview.png`: the image an approval's card shows (a post's photo);
- `runs/{id}/proof.png`: the screenshot a run kept as proof (or of where it failed).

Nothing else is served: `params.path` must match one of these patterns exactly, so a path can
never name a file of its own. The bytes come from the services that already hold them (the lane
manager, the approval store, the runner's shots folder), never from a path the controller wrote.
When the stream names a lane, an approval or run on another lane reads as not found. A file is
read whole before `stream_ready` goes out, so every refusal is an `error` and never half a file.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from agentos.core.errors import AgentosError, InvalidRequest, NotFound, ServiceUnavailable
from agentos.core.models import LANE_ID_RE, LaneKind
from agentos.node.channel.streams import StreamPipe

MAX_FILE_BYTES = 8 * 1024 * 1024
APPROVAL_FILE = re.compile(r"approvals/(ap_[0-9A-Za-z]{1,64})/preview\.png")
RUN_FILE = re.compile(r"runs/(run_[0-9A-Za-z]{1,64})/proof\.png")
SCREENSHOT = "screenshot.png"
WARM = ("warm", "active")

Worker = Callable[[StreamPipe], Awaitable[str]]


class LaneCold(AgentosError):
    """A screenshot of a lane that is not open (the cloud's `lane_cold`)."""
    code = "lane_cold"


async def read_file(runtime: Any, home: Path, lane: Any, path: Any) -> bytes:
    """The bytes behind `path`, or an AgentosError that says why there are none."""
    if not isinstance(path, str) or not path:
        raise InvalidRequest("a file stream needs params.path")
    if lane is not None and not (isinstance(lane, str) and LANE_ID_RE.fullmatch(lane)):
        raise InvalidRequest("lane must be a lane id")
    if path == SCREENSHOT:
        data = await _screenshot(runtime, lane)
    elif match := APPROVAL_FILE.fullmatch(path):
        data = _approval_image(runtime, lane, match.group(1))
    elif match := RUN_FILE.fullmatch(path):
        data = _run_proof(runtime, home, lane, match.group(1))
    else:
        raise InvalidRequest("this node serves screenshot.png, approvals/{id}/preview.png and runs/{id}/proof.png")
    if not data:
        raise NotFound(f"no file {path} on this node")
    if len(data) > MAX_FILE_BYTES:
        raise InvalidRequest(f"{path} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MiB")
    return data


async def prepare_file(runtime: Any, home: Path, lane: Any, params: dict) -> Worker:
    data = await read_file(runtime, home, lane, params.get("path"))

    async def run(pipe: StreamPipe) -> str:
        await pipe.write(data)
        return "eof"

    return run


async def _screenshot(runtime: Any, lane_id: Any) -> bytes:
    if lane_id is None:
        raise InvalidRequest("screenshot.png needs the stream's lane")
    lanes = runtime.get("lanes")
    if lanes is None:
        raise ServiceUnavailable("this node has no browser lanes")
    try:
        info = lanes.get(lane_id)
    except AgentosError:
        envs = runtime.get("envs")
        if envs is not None and callable(getattr(envs, "has", None)) and envs.has(lane_id):
            raise LaneCold(f"lane {lane_id} is a terminal or service lane; it has no screen") from None
        raise
    if info.spec.kind not in (LaneKind.BROWSER, LaneKind.ANDROID):
        raise LaneCold(f"lane {lane_id} has no screen")
    if info.state.value not in WARM:
        raise LaneCold(f"lane {lane_id} is {info.state.value}; open it first")
    if callable(getattr(lanes, "screenshot", None)):
        return await _maybe_await(lanes.screenshot(lane_id))
    from agentos.node.claims import READ
    from agentos.node.tools.session import lane_session

    interaction = runtime.get("interaction")
    if interaction is None:
        raise ServiceUnavailable("this node cannot take screenshots")
    async with lane_session(lanes, lane_id, "agent", mode=READ) as page:  # a read: it passes a claim
        return await interaction.screenshot(page)


def _approval_image(runtime: Any, lane: str | None, approval_id: str) -> bytes | None:
    """The first image the approval's card shows, read through the store (it checks the bytes
    still match what the approval binds)."""
    store = runtime.get("approvals")
    if store is None:
        raise ServiceUnavailable("this node has no approvals")
    try:
        approval = store.get(approval_id)
    except AgentosError:
        raise NotFound(f"no approval {approval_id} on this node") from None
    if lane is not None and getattr(approval, "lane", None) != lane:
        raise NotFound(f"no approval {approval_id} on lane {lane}")
    name = preview_param(getattr(approval, "card", None))
    files = getattr(store, "card_files", None)
    if name is None or not callable(files):
        raise NotFound(f"approval {approval_id} has no image")
    data, _mime = files(approval, name)
    return data


def preview_param(card: Any) -> str | None:
    """The param whose image is the approval's preview: the card's first image."""
    images = card.get("images") if isinstance(card, dict) else None
    for image in images or []:
        if isinstance(image, dict) and isinstance(image.get("name"), str):
            return image["name"]
    return None


def _run_proof(runtime: Any, home: Path, lane: str | None, run_id: str) -> bytes | None:
    runner = runtime.get("runner")
    if runner is None:
        raise ServiceUnavailable("this node has no runner")
    try:
        run = runner.get(run_id)
    except AgentosError:
        raise NotFound(f"no run {run_id} on this node") from None
    if lane is not None and lane not in (getattr(run, "lane", None), getattr(run, "surface", None)):
        raise NotFound(f"no run {run_id} on lane {lane}")
    path = proof_path(home, getattr(run, "proof", None), getattr(run, "error", None))
    if path is None:
        raise NotFound(f"run {run_id} kept no screenshot")
    with path.open("rb") as handle:  # at most the cap and one byte: a file that grows is not read whole
        data = handle.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise InvalidRequest(f"the proof of {run_id} is too large to send")
    return data


def proof_path(home: Path, proof: Any, error: Any = None) -> Path | None:
    """The file behind a run's proof (or failure) screenshot, only while it is still on the node
    and inside the runner's own shots folder (as agentos.audit reads it)."""
    rel = (proof or {}).get("screenshot") if isinstance(proof, dict) else None
    if not rel and isinstance(error, dict):
        rel = error.get("screenshot")
    if not isinstance(rel, str) or not rel or rel.startswith("/api/") or "\x00" in rel:
        return None
    root = (home / "runner" / "shots").resolve()
    path = (Path(rel) if Path(rel).is_absolute() else home / rel).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value
