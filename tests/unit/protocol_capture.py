"""Record the node channel protocol from a real session: the open-source controller and a node
with fake lanes and the real policy and channel services, over a real WebSocket.

Every text frame either side reads goes through `wire.decode`; `capture()` wraps it and keeps one
example of each message (per direction, type, and event kind or report), normalized so a recording
is the same every run: ids, keys, signatures, tokens and times become fixed placeholders, long
strings and lists are cut. tests/fixtures/protocol/messages.json holds the examples, and
docs/PROTOCOL.md shows them (scripts/protocol_doc.py, `make protocol-doc`).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
import json
import re
from pathlib import Path
from typing import Any

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "protocol" / "messages.json"

NODE_TO_CONTROLLER = "node_to_controller"
CONTROLLER_TO_NODE = "controller_to_node"

_ULID = re.compile(r"\b([A-Za-z]+_)?([0-9A-HJKMNP-TV-Za-hjkmnp-tv-z]{26})\b")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
SECRET_KEYS = {
    "sig": "<base64url Ed25519 signature>", "proof": "<base64url Ed25519 signature>",
    "node_pubkey": "<base64url Ed25519 public key>", "controller_pubkey": "<base64url Ed25519 public key>",
    "grant_pubkey": "<base64url Ed25519 public key>", "nonce": "<nonce>", "join_token": "<join token>",
    "grant_token": "agt2_<per-call grant token>", "epoch": "<outbox epoch>",
}
VOLATILE_NUMBERS = {"cpu_pct", "mem_used_mb", "mem_total_mb", "disk_free_mb", "mem_mb", "uptime_s", "deadline",
                    "duration_ms", "ms", "cores", "disk_total_mb", "bytes"}
MAX_STRING = 160
MAX_LIST = 2


class Normalizer:
    def __init__(self) -> None:
        self.ulids: dict[str, str] = {}
        self.ids: dict[str, str] = {}
        self.times = 0

    def ulid(self, match: re.Match) -> str:
        prefix, raw = match.group(1) or "", match.group(2).upper()
        if raw not in self.ulids:
            self.ulids[raw] = f"01JC{len(self.ulids) + 1:022d}"
        return prefix + self.ulids[raw]

    def random_id(self, match: re.Match) -> str:
        raw = match.group(0)
        if raw not in self.ids:
            self.ids[raw] = f"{match.group(1)}_{len(self.ids) + 1:016d}"
        return self.ids[raw]

    def time(self) -> str:
        self.times += 1
        return f"2026-11-04T03:{self.times // 60:02d}:{self.times % 60:02d}Z"

    def value(self, key: str | None, value: Any) -> Any:
        if key in SECRET_KEYS and isinstance(value, str):
            return SECRET_KEYS[key]
        if isinstance(value, dict):
            return {k: self.value(k, v) for k, v in value.items()}
        if isinstance(value, list):
            items = [self.value(None, v) for v in value[:MAX_LIST]]
            return items
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, int | float):
            return 0 if key in VOLATILE_NUMBERS else value
        if isinstance(value, str):
            if _ISO.match(value):
                return self.time()
            if _HASH.match(value):
                return "sha256:<hex digest>"
            if _HEX_ID.match(value):
                return "<thread id>"
            text = _ULID.sub(self.ulid, value)
            text = _RANDOM_ID.sub(self.random_id, text)
            text = re.sub(r"\b\d+\.\d+\.\d+(\.dev\d+\S*)?\b", "<version>", text) if key == "version" else text
            return text if len(text) <= MAX_STRING else text[:MAX_STRING] + "..."
        return value


def example_key(direction: str, data: dict) -> str:
    kind = data.get("type", "?")
    body = data.get("body") if isinstance(data.get("body"), dict) else {}
    if kind == "event":
        sub = body.get("kind")
        payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
        if sub == "node":
            sub = "node." + ("+".join(sorted(k for k in payload if k != "tools_part")) or "?")
        return f"{direction} event {sub}"
    if kind == "error":
        return f"{direction} error {body.get('code')}"
    if kind == "result":
        error = body.get("error") if isinstance(body.get("error"), dict) else {}
        if body.get("ok") is False:
            return f"{direction} result {error.get('code')}"
        images = [c for c in body.get("content") or [] if isinstance(c, dict) and c.get("type") == "image"]
        return f"{direction} result image" if images else f"{direction} result"
    if kind == "mesh_freeze":
        return f"{direction} mesh_freeze {'on' if body.get('frozen') else 'off'}"
    return f"{direction} {kind}"


def _direction() -> str | None:
    for frame in inspect.stack()[2:8]:
        module = frame.frame.f_globals.get("__name__", "")
        if module.startswith("agentos.controller"):
            return NODE_TO_CONTROLLER
        if module.startswith("agentos.node.channel"):
            return CONTROLLER_TO_NODE
    return None


@contextlib.contextmanager
def recording():
    """Collect (direction, decoded JSON) for every text frame read through wire.decode."""
    from agentos.node.channel import wire

    seen: list[tuple[str, dict]] = []
    real = wire.decode

    def decode(frame):
        envelope = real(frame)
        direction = _direction()
        if direction is not None:
            with contextlib.suppress(ValueError, TypeError):
                seen.append((direction, json.loads(frame)))
        return envelope

    wire.decode = decode
    try:
        yield seen
    finally:
        wire.decode = real


# The order the examples are shown in: the life of a connection, then what each side sends.
ORDER = [
    "controller_to_node challenge", "node_to_controller enroll", "controller_to_node enrolled",
    "node_to_controller hello", "controller_to_node welcome", "node_to_controller heartbeat",
    "node_to_controller event lane", "node_to_controller event node.tools", "node_to_controller event node.system",
    "node_to_controller event node.programs", "node_to_controller event node.services",
    "node_to_controller event node.inventory", "node_to_controller event node.doctor",
    "controller_to_node ack", "controller_to_node invoke", "node_to_controller result",
    "node_to_controller result image", "node_to_controller result approval_pending", "node_to_controller traces",
    "node_to_controller approval_request", "controller_to_node approval_decision",
    "node_to_controller event approval", "controller_to_node revoke", "controller_to_node lane_control",
    "controller_to_node ask_first", "node_to_controller ask_first_result",
    "controller_to_node config_update", "controller_to_node mesh_freeze on", "controller_to_node mesh_freeze off",
    "controller_to_node stream_open", "node_to_controller stream_ready", "controller_to_node stream_credit",
    "node_to_controller stream_close", "node_to_controller error invalid",
]

# Values that differ by machine, shown as a fixed example instead.
SYSTEM_EXAMPLE = {"os": "linux", "arch": "x86_64", "kernel": "6.8.0-48-generic", "cpu_model": "AMD EPYC 7763",
                  "cores": 4, "mem_total_mb": 15990, "disk_total_mb": 154000, "gpu": None, "timezone": "UTC",
                  "uptime_s": 86400, "python": "3.12.7"}
_RANDOM_ID = re.compile(r"\b([a-z]{2,6})_([a-z0-9]{12,16})\b")
_HEX_ID = re.compile(r"^[0-9a-f]{32}$")


def _stable(data: dict) -> dict:
    body = data.get("body") or {}
    if data.get("type") == "enroll":
        body.update({k: SYSTEM_EXAMPLE[k] for k in ("os", "arch") if k in body})
    payload = (data.get("body") or {}).get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("system"), dict):
        payload["system"] = {**payload["system"], **{k: v for k, v in SYSTEM_EXAMPLE.items()
                                                       if k in payload["system"]}}
    return data


def select(seen: list[tuple[str, dict]]) -> list[dict]:
    """One normalized example per key, in ORDER (others after, by key)."""
    first: dict[str, tuple[str, dict]] = {}
    for direction, data in seen:
        first.setdefault(example_key(direction, data), (direction, data))
    rank = {key: i for i, key in enumerate(ORDER)}
    norm = Normalizer()
    out = []
    for key in sorted(first, key=lambda k: (rank.get(k, len(ORDER)), k)):
        direction, data = first[key]
        envelope = norm.value(None, _stable(json.loads(json.dumps(data))))
        if "ts" in envelope:
            envelope["ts"] = norm.time()
        out.append({"key": key, "direction": direction, "type": data.get("type"), "envelope": envelope})
    return out


async def _start_node(home: Path, name: str, ctl: Any, lanes: list[str]) -> Any:
    from channel_fake_controller import ControlLanes
    from channel_support import FAST, RunningNode, extra_tools, join_node
    from fakes import FakeInteraction

    from agentos.core.models import LaneSpec
    from agentos.mcp import providers as mcp_providers
    from agentos.node.daemon import build_services, node_runtime, serve

    config = await join_node(home, name, ctl)
    manager = ControlLanes(config)
    for lane in lanes:
        await manager.create(LaneSpec(id=lane))
    interaction = FakeInteraction()
    runtime = node_runtime(config, manager, interaction)
    build_services(runtime, ["agentos.policy.service", "agentos.freeze.service", "agentos.node.channel.service"])
    channel = runtime.get("channel")
    channel.timings = dataclasses.replace(FAST, doctor_after=600.0)  # no doctor report mid-session: it is timed
    channel._backoff.low, channel._backoff.high = FAST.backoff_min, FAST.backoff_max
    stop = asyncio.Event()
    task = asyncio.create_task(serve(config, runtime, stop=stop, providers=[*mcp_providers.tool_providers(), extra_tools]))
    await asyncio.wait_for(channel.welcomed.wait(), 15)
    return RunningNode(name, config, runtime, manager, interaction, stop, task)


async def session(home: Path) -> list[tuple[str, dict]]:
    """The scripted session: enroll, connect, tools and lanes, a call, an image, an approval, a
    revocation, a take over, a config change, a file stream and a refusal."""
    from channel_support import agent, owner, start_controller, wait_for

    with recording() as seen:
        ctl = await start_controller(home / "controller")
        node = await _start_node(home / "node", "node-a", ctl, ["personal/reddit"])
        hub = ctl.controller.hub
        node_id = node.config.node_id
        try:
            await wait_for(lambda: all(s.tools and s.heartbeat for s in hub.sessions.values()) and hub.sessions)
            await hub.push_ask_first(ctl.org, node_id, [{"scope": "lane", "id": "personal/reddit", "ask_first": True,
                                                         "by": "usr_owner"}])  # the post below waits for a person
            async with agent(ctl) as client, owner(ctl) as api:
                await client.call_tool("browser_snapshot", {"on": "personal/reddit"})
                await client.call_tool("browser_screenshot", {"on": "personal/reddit"})
                first = await client.call_tool("post_note", {"on": "personal/reddit", "text": "hello"})
                approval_id = first.structured_content["approval_id"]
                await wait_for(lambda: ctl.controller.store.approval(ctl.org, node_id, approval_id) is not None)
                await api.post(f"/api/approvals/{node_id}/{approval_id}", json={"decision": "approved"})
                await client.call_tool("post_note", {"on": "personal/reddit", "text": "hello"})
                await wait_for(lambda: ctl.controller.store.approval(ctl.org, node_id, approval_id)["state"]
                               == "consumed")
                with contextlib.suppress(Exception):  # the node refuses a second decision
                    await hub.decide(ctl.org, node_id, approval_id, "denied", decided_by="owner")
                made = (await api.post("/api/grants", json={"subject": "helper", "workspaces": ["personal"],
                                                            "ceiling": "draft"})).json()
                await api.delete(f"/api/grants/{made['id']}")
                await wait_for(lambda: node.runtime.get("grant_issuers").is_revoked([made["id"]]))
            await hub.lane_control(ctl.org, node_id, "personal/reddit", "take", by="owner")
            await hub.lane_control(ctl.org, node_id, "personal/reddit", "hand_back", by="owner")
            await hub.config_update(ctl.org, node_id, {"heartbeat_seconds": 15})
            async with owner(ctl) as api:
                await api.post("/api/freeze", json={"reason": "checking a leak"})
                await api.post("/api/unfreeze")
            pipe = await hub.open_stream(ctl.org, node_id, "file", "personal/reddit", {"path": "screenshot.png"})
            with contextlib.suppress(Exception):
                async with asyncio.timeout(5):
                    while await pipe.read() is not None:
                        pass
            with contextlib.suppress(Exception):
                await hub.open_stream(ctl.org, node_id, "file", "personal/reddit", {"path": "../etc/passwd"})
            await wait_for(lambda: node.channel.store.depth() == 0)
            await asyncio.sleep(0.3)
        finally:
            await node.close()
            await ctl.close()
    return seen


def shape(value: Any) -> Any:
    """The structure of a message: keys and value types, lists by their first item."""
    if isinstance(value, dict):
        return {k: shape(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [shape(value[0])] if value else []
    return "null" if value is None else type(value).__name__


def shape_problems(want: Any, got: Any, path: str = "") -> list[str]:
    """Where `got` (a new recording) no longer has `want`'s (the fixture's) structure. A null on
    either side matches anything (a value the node could not read, or that varies by machine)."""
    if want == "null" or got == "null":
        return []
    if isinstance(want, dict) and isinstance(got, dict):
        out = [f"{path}.{k}: gone" for k in want if k not in got]
        out += [f"{path}.{k}: new (re-record: make protocol-doc)" for k in got if k not in want]
        for k in want:
            if k in got:
                out += shape_problems(want[k], got[k], f"{path}.{k}")
        return out
    if isinstance(want, list) and isinstance(got, list):
        return shape_problems(want[0], got[0], f"{path}[0]") if want and got else []
    if {want, got} <= {"int", "float"} if isinstance(want, str) and isinstance(got, str) else False:
        return []
    return [] if want == got else [f"{path}: {want} is now {got}"]


def load_fixture() -> list[dict]:
    return json.loads(FIXTURE.read_text())["messages"]


def write_fixture(examples: list[dict]) -> None:
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps({
        "note": "Recorded by scripts/protocol_doc.py --record from tests/unit/protocol_capture.py; "
                "rendered into docs/PROTOCOL.md by `make protocol-doc`. Do not edit by hand.",
        "messages": examples}, indent=1, ensure_ascii=False) + "\n")
