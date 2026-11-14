"""Golden traces: a lane session recorded once, replayed in CI against the engine abstraction.

A trace is three files in tests/golden/<name>/:

    session.json   the tool calls an agent made (MCP tools, through the node's real tool layer)
                   and what each returned: outlines with refs, signals, page results, errors
    engine.json    every call the tool layer made on the Interaction (agentos.node.interfaces):
                   navigate, snapshot, act, screenshot, signal, with their results
    page.har       HAR 1.2 of the documents the tab loaded, in order (the dev web has no network:
                   each entry is a page the tab showed, with its outline as the response body)

`record()` runs SESSION on the dev web (agentos.dev.web, a fake site with a feed, a composer, a
like button and a captcha page: never a real site, so the repo holds no one else's content), with
a RecordingInteraction between the tool layer and the engine. `replay()` runs the recorded calls
again with a ReplayInteraction fed by engine.json in place of the engine, so what is under test is
everything above the engine: how the tools turn engine results into what an agent reads (snapshot
headers and deltas, refs, signals and the holds they make, page results, errors). A replay that
asks the engine for something the recording did not is a mismatch too. scripts/record_golden.py
writes a trace; tests/unit/test_golden.py replays every trace.
"""

from __future__ import annotations

import base64
import enum
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from agentos.core.config import NodeConfig
from agentos.core.errors import AgentosError
from agentos.core.models import LaneSpec
from agentos.core.runtime import Runtime
from agentos.dev.lanes import DevLaneManager
from agentos.dev.web import CHALLENGE_URL, DevInteraction, frame_of_page, new_tab, outline
from agentos.runner.fakes import BASE

LANE = "biz-a/social"
FORMAT = 1
ENGINE_CALLS = ("navigate", "snapshot", "act", "screenshot", "signal")


@dataclass(frozen=True)
class Ref:
    """A ref the agent reads off the latest outline: the element on the line that names `name`."""
    name: str


# What the agent does: look around, like a post, write a caption, look at the page, hit a captcha.
SESSION: list[tuple[str, dict[str, Any]]] = [
    ("lanes_list", {}),
    ("browser_navigate", {"on": LANE, "url": f"{BASE}/"}),
    ("browser_snapshot", {"on": LANE}),
    ("browser_act", {"on": LANE, "ref": Ref("New post"), "action": "click", "snapshot": "full"}),
    ("browser_navigate", {"on": LANE, "url": f"{BASE}/p/1", "snapshot": "full"}),
    ("browser_act", {"on": LANE, "ref": Ref("Like"), "action": "click", "snapshot": "delta"}),
    ("browser_act", {"on": LANE, "ref": "e99", "action": "click"}),
    ("browser_navigate", {"on": LANE, "url": f"{BASE}/compose"}),
    ("browser_snapshot", {"on": LANE}),
    ("browser_act", {"on": LANE, "ref": Ref("Write a caption..."), "action": "type", "text": "Fresh rye",
                     "snapshot": "delta"}),
    ("browser_screenshot", {"on": LANE}),
    ("browser_navigate", {"on": LANE, "url": CHALLENGE_URL}),
    ("browser_snapshot", {"on": LANE}),
]


class GoldenMismatch(AssertionError):
    """The replay asked the engine for something the recording did not have."""


# ---- serializing ------------------------------------------------------------------------

def to_json(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, bytes):
        return {"bytes_b64": base64.b64encode(value).decode(), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, dict):
        return {str(k): to_json(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [to_json(v) for v in value]
    return value


def _error(exc: BaseException) -> dict[str, Any]:
    return {"type": type(exc).__name__, "module": type(exc).__module__, "message": str(exc),
            "code": getattr(exc, "code", None)}


def _raise(error: dict[str, Any]) -> None:
    import importlib

    cls: Any = getattr(importlib.import_module(error["module"]), error["type"], None)
    if isinstance(cls, type) and issubclass(cls, AgentosError):
        raise cls(error["message"])
    raise GoldenMismatch(f"the recording raised {error['type']}, which a replay cannot rebuild")


# ---- the engine side ------------------------------------------------------------------

class RecordingInteraction:
    """Passes every call to the real interaction and writes down what it returned."""

    def __init__(self, inner: Any):
        self.inner = inner
        self.calls: list[dict[str, Any]] = []
        self.har: list[dict[str, Any]] = []

    def _wrap(self, name: str) -> Callable[..., Awaitable[Any]]:
        method = getattr(self.inner, name)

        async def call(page: Any, *args: Any, **kwargs: Any) -> Any:
            entry: dict[str, Any] = {"call": name, "lane": getattr(page, "lane_id", None),
                                     "args": to_json(list(args)), "kwargs": to_json(kwargs)}
            before = getattr(getattr(page, "driver", None), "url", None)
            try:
                result = await method(page, *args, **kwargs)
            except Exception as exc:
                entry["error"] = _error(exc)
                self.calls.append(entry)
                raise
            entry["result"] = to_json(result)
            self.calls.append(entry)
            self._loaded(page, before)
            return result

        return call

    def _loaded(self, page: Any, before: str | None) -> None:
        driver = getattr(page, "driver", None)
        if driver is None or (driver.url == before and self.har):
            return
        if self.har and self.har[-1]["request"]["url"] == driver.url:
            return
        text, _ = outline(driver)
        self.har.append(har_entry(len(self.har), driver.url, driver.root.name, text))

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        getattr(self.inner, name)  # AttributeError when the engine does not have it
        return self._wrap(name)


class ReplayInteraction:
    """The engine, played back from engine.json: each call must be the next recorded one."""

    def __init__(self, calls: list[dict[str, Any]]):
        self.calls = list(calls)
        self.index = 0
        self._methods = {c["call"] for c in calls} | set(ENGINE_CALLS)

    def _wrap(self, name: str) -> Callable[..., Awaitable[Any]]:
        async def call(page: Any, *args: Any, **kwargs: Any) -> Any:
            if self.index >= len(self.calls):
                raise GoldenMismatch(f"the replay called {name} after the recording ended")
            want = self.calls[self.index]
            got = {"call": name, "lane": getattr(page, "lane_id", None), "args": to_json(list(args)),
                   "kwargs": to_json(kwargs)}
            if {k: want.get(k) for k in got} != got:
                raise GoldenMismatch(f"engine call {self.index}: the recording has "
                                     f"{_short(want)}, the replay asked for {_short(got)}")
            self.index += 1
            if "error" in want:
                _raise(want["error"])
            return _result(name, want.get("result"))

        return call

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or name not in self._methods:
            raise AttributeError(name)
        return self._wrap(name)

    @property
    def done(self) -> bool:
        return self.index == len(self.calls)


def _short(entry: dict[str, Any]) -> str:
    return json.dumps({k: entry.get(k) for k in ("call", "args", "kwargs")})[:300]


def _result(name: str, data: Any) -> Any:
    from agentos.core.models import ActionResult, NavigateResult, Signal, SnapshotResult

    if name == "navigate":
        return NavigateResult.model_validate(data)
    if name == "snapshot":
        return SnapshotResult.model_validate(data)
    if name == "act":
        return ActionResult.model_validate(data)
    if name == "signal":
        return Signal(data)
    if isinstance(data, dict) and "bytes_b64" in data:
        return base64.b64decode(data["bytes_b64"])
    return data


def har_entry(index: int, url: str, title: str, text: str) -> dict[str, Any]:
    body = text.encode()
    return {
        "pageref": f"page_{index + 1}",
        "startedDateTime": f"2026-11-04T03:00:{index:02d}.000Z",
        "time": 0,
        "request": {"method": "GET", "url": url, "httpVersion": "HTTP/1.1", "cookies": [], "headers": [],
                    "queryString": [], "headersSize": -1, "bodySize": 0},
        "response": {"status": 200, "statusText": "OK", "httpVersion": "HTTP/1.1", "cookies": [], "headers": [],
                     "content": {"size": len(body), "mimeType": "text/x-agentos-outline", "text": text},
                     "redirectURL": "", "headersSize": -1, "bodySize": len(body), "_title": title},
        "cache": {}, "timings": {"send": 0, "wait": 0, "receive": 0},
    }


def har(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"log": {"version": "1.2", "creator": {"name": "agentos golden traces", "version": str(FORMAT)},
                    "pages": [{"id": e["pageref"], "startedDateTime": e["startedDateTime"],
                               "title": e["response"]["_title"], "pageTimings": {}} for e in entries],
                    "entries": entries}}


# ---- the tool side ----------------------------------------------------------------------

_TIMES = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?")


def tool_result(result: Any) -> dict[str, Any]:
    """What the agent got back, with times made stable (they are the only thing that varies)."""
    content = []
    for block in result.content:
        if block.type == "text":
            content.append({"type": "text", "text": _TIMES.sub("<time>", block.text)})
        elif block.type == "image":
            raw = base64.b64decode(block.data)
            content.append({"type": "image", "mime_type": block.mime_type, "bytes": len(raw),
                            "sha256": hashlib.sha256(raw).hexdigest()})
        else:
            content.append({"type": block.type})
    structured = json.loads(_TIMES.sub("<time>", json.dumps(result.structured_content))) \
        if result.structured_content is not None else None
    return {"is_error": bool(result.is_error), "content": content, "structured": structured}


def _resolve(args: dict[str, Any], last_outline: str) -> dict[str, Any]:
    out = {}
    for key, value in args.items():
        if isinstance(value, Ref):
            line = next((line for line in last_outline.splitlines()
                         if f'"{value.name}"' in line and "[ref=" in line), None)
            if line is None:
                raise GoldenMismatch(f"no element named {value.name!r} in the last outline")
            value = line.split("[ref=")[1].split("]")[0]
        out[key] = value
    return out


def _texts(result: dict[str, Any]) -> str:
    return "\n".join(block["text"] for block in result["content"] if block["type"] == "text")


def _runtime(home: Path, interaction_for: Callable[[], Any]) -> tuple[Runtime, DevLaneManager, Any]:
    config = NodeConfig(home=home, name="golden", mcp_token="golden-token")
    lanes = DevLaneManager(config, make_page=new_tab, frame=frame_of_page)
    engine = interaction_for()
    runtime = Runtime(config=config, lanes=lanes, interaction=lanes.watch(engine))
    return runtime, lanes, engine


async def _run(home: Path, engine_for: Callable[[], Any], calls: list[tuple[str, dict[str, Any]]],
               *, resolve: bool) -> tuple[list[dict[str, Any]], Any]:
    from mcp import Client

    from agentos.mcp.registry import build_mcp
    from agentos.node.tools import INSTRUCTIONS, tool_specs

    runtime, lanes, engine = _runtime(home, engine_for)
    await lanes.create(LaneSpec(id=LANE))
    out: list[dict[str, Any]] = []
    last_outline = ""
    try:
        async with Client(build_mcp(runtime, [tool_specs], instructions=INSTRUCTIONS)) as client:
            for tool, args in calls:
                args = _resolve(args, last_outline) if resolve else args
                result = tool_result(await client.call_tool(tool, args))
                out.append({"tool": tool, "args": args, "result": result})
                if "[ref=" in _texts(result):
                    last_outline = _texts(result)
    finally:
        await lanes.shutdown()
    return out, engine


async def record(home: Path) -> dict[str, Any]:
    """Run SESSION on the dev web; returns the three files' contents."""
    session, engine = await _run(home, lambda: RecordingInteraction(DevInteraction()), SESSION, resolve=True)
    return {"session": {"format": FORMAT, "lane": LANE, "engine": "agentos.dev.web", "calls": session},
            "engine": {"format": FORMAT, "calls": engine.calls},
            "har": har(engine.har)}


async def replay(home: Path, trace: dict[str, Any]) -> tuple[list[dict[str, Any]], ReplayInteraction]:
    """The recorded tool calls again, with the engine played back from the recording."""
    calls = [(c["tool"], c["args"]) for c in trace["session"]["calls"]]
    return await _run(home, lambda: ReplayInteraction(trace["engine"]["calls"]), calls, resolve=False)


def _pixels(png: bytes) -> str:
    """A digest of a PNG's pixels (its inflated image data), the same whichever zlib compressed it."""
    import struct
    import zlib

    data, pos = b"", 8
    while pos < len(png):
        length, kind = struct.unpack(">I4s", png[pos:pos + 8])
        if kind == b"IDAT":
            data += png[pos + 8:pos + 8 + length]
        pos += 12 + length
    return hashlib.sha256(png[16:24] + zlib.decompress(data)).hexdigest()


def comparable(trace: dict[str, Any]) -> dict[str, Any]:
    """A trace with each picture as its pixels, for comparing two recordings: PNG bytes depend on
    the zlib that wrote them (macOS and Linux differ), the pixels do not."""
    out = json.loads(json.dumps(trace))
    for call in out["engine"]["calls"]:
        result = call.get("result")
        if isinstance(result, dict) and "bytes_b64" in result:
            call["result"] = {"pixels": _pixels(base64.b64decode(result["bytes_b64"]))}
    for call in out["session"]["calls"]:
        for block in call["result"]["content"]:
            if block.get("type") == "image":
                block.pop("sha256", None)
                block.pop("bytes", None)
    return out


def load(directory: Path) -> dict[str, Any]:
    directory = Path(directory)
    return {"session": json.loads((directory / "session.json").read_text()),
            "engine": json.loads((directory / "engine.json").read_text()),
            "har": json.loads((directory / "page.har").read_text())}


def write(directory: Path, trace: dict[str, Any]) -> None:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, key in (("session.json", "session"), ("engine.json", "engine"), ("page.har", "har")):
        (directory / name).write_text(json.dumps(trace[key], indent=1, ensure_ascii=False) + "\n")
