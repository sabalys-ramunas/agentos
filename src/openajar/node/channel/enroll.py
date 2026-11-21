"""`openajar node join <controller-url> <join-token>`: enroll this node with a controller.

The node makes (or reuses) its Ed25519 key at secrets/node.key, dials the controller, reads the
`challenge` the controller sends first, and sends `enroll` with `proof`: the node key's signature
over `agentos-node-enroll:v1:{nonce}:{node_pubkey}`, so nobody can enroll a key they do not hold.
A controller that sends no challenge in time gets the enroll without a proof. The answer `enrolled` carries
its node id, the org's grant key and the controller key, which are pinned in node.toml. The
join token is consumed. A refusal (`already_used`, `expired`, `denied`, `conflict`) is final
for that token. The daemon dials again for `challenge` and `hello` once it runs.

Joining another controller (the hosted cloud, say) is the same command with its URL and a
token from it: the new enrollment replaces the old one and the outbox meant for the old
controller is dropped. The seq counter is never reset. A `ceiling` given here is written with
the enrollment (openajar.policy.ceiling): `openajar node join` writes the hosted default, or the
full ceiling with `--self-hosted`, so a ceiling meant for one controller never carries over to
another. Joining another org leaves the old one (standing.leave_org): its grant key is no longer
trusted, so its grants stop verifying, and the standing approvals it sent are revoked.
"""

from __future__ import annotations

import asyncio
import platform
from typing import Any

from websockets.asyncio.client import connect

from openajar import __version__
from openajar.core import signing
from openajar.core.config import NodeConfig, save_config
from openajar.node.channel import standing, wire
from openajar.node.channel.identity import NodeKey
from openajar.node.channel.store import ChannelStore
from openajar.policy.ceiling import ControllerCeiling

DEFAULT_CAPABILITIES = ("browser", "terminal", "service")
REQUIRED = ("node_id", "org_id", "grant_pubkey", "controller_pubkey")
CHALLENGE_WAIT = 5.0  # how long the node waits for the challenge before it enrolls without a proof


class EnrollmentRefused(Exception):
    """The controller said no. Retrying with the same token cannot help."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class EnrollmentFailed(Exception):
    """The controller could not be reached, or answered something unusable."""


def _parse_enrolled(body: dict) -> dict[str, Any]:
    for name in REQUIRED:
        value = body.get(name)
        if not isinstance(value, str) or not value or len(value) > 200:
            raise EnrollmentFailed(f"the controller's enrolled message has no usable {name}")
    for name in ("grant_pubkey", "controller_pubkey"):
        try:
            signing.public_key(body[name])
        except ValueError:
            raise EnrollmentFailed(f"{name} is not a 32 byte Ed25519 key") from None
    config = body.get("config")
    return {**{name: body[name] for name in REQUIRED}, "config": config if isinstance(config, dict) else {}}


async def _challenge(ws: Any, wait: float) -> str | None:
    """The nonce of the controller's first `challenge`, or None when none comes in time."""
    try:
        async with asyncio.timeout(wait):
            while True:
                frame = await ws.recv()
                if isinstance(frame, bytes):
                    continue
                message = wire.decode(frame)
                nonce = message.body.get("nonce")
                if message.type == "challenge" and isinstance(nonce, str) and 8 <= len(nonce) <= 200:
                    return nonce
                if message.type == "error":
                    raise EnrollmentRefused(str(message.body.get("code", "error"))[:40],
                                            str(message.body.get("message", ""))[:300])
    except TimeoutError:
        return None


async def enroll(config: NodeConfig, url: str, join_token: str, *, name: str | None = None,
                 capabilities: list[str] | None = None, timeout: float = wire.HANDSHAKE_TIMEOUT,
                 ceiling: ControllerCeiling | None = None, ceiling_by: str = "join",
                 left: dict | None = None) -> NodeConfig:
    """Enroll and save the enrollment in node.toml. Returns the updated config. `ceiling` is
    written with `ceiling_by` as who set it ("join", or "self_hosted" for the owner's own). When
    the node leaves another org, `left` (if given) gets `issuer` and the `standing` ids revoked."""
    url = wire.connect_url(url)
    if not join_token or len(join_token) > 200:
        raise EnrollmentFailed("the join token is empty or too long")
    caps = [c for c in (capabilities or DEFAULT_CAPABILITIES) if c in wire.CAPABILITIES]
    key = NodeKey.load_or_create(config.secrets_dir)
    body = {"join_token": join_token, "node_pubkey": key.public, "name": name or config.name,
            "version": __version__, "os": platform.system().lower(), "arch": platform.machine().lower(),
            "capabilities": caps, "max_warm_lanes": config.max_warm_lanes}
    try:
        async with connect(url, max_size=wire.HARD_FRAME_BYTES, open_timeout=timeout, close_timeout=2,
                           proxy=None, user_agent_header=f"openajar-node/{__version__}") as ws:
            nonce = await _challenge(ws, min(timeout, CHALLENGE_WAIT))
            if nonce is not None:
                body["proof"] = key.sign_enroll(nonce)
            await ws.send(wire.Envelope("enroll", body).encode())
            async with asyncio.timeout(timeout):
                while True:
                    frame = await ws.recv()
                    if isinstance(frame, bytes):
                        continue
                    message = wire.decode(frame)
                    if message.type == "enrolled":
                        enrolled = _parse_enrolled(message.body)
                        break
                    if message.type == "error":
                        raise EnrollmentRefused(str(message.body.get("code", "error"))[:40],
                                                str(message.body.get("message", ""))[:300])
                    # anything else (another challenge) means nothing before enrollment
    except (OSError, TimeoutError) as exc:
        raise EnrollmentFailed(f"could not reach the controller at {url}: {exc or type(exc).__name__}") from None
    except wire.WireError as exc:
        raise EnrollmentFailed(f"the controller sent something unusable: {exc.message}") from None
    except Exception as exc:
        if isinstance(exc, EnrollmentRefused | EnrollmentFailed):
            raise
        raise EnrollmentFailed(f"could not enroll with {url}: {type(exc).__name__}: {exc}"[:300]) from None

    store = ChannelStore.for_home(config.home)
    try:
        if config.node_id and config.node_id != enrolled["node_id"]:
            store.trim(store.high_seq)  # the old controller's backlog is not the new one's business
        store.set_meta("controller_config", enrolled["config"])
        store.set_meta("degraded_since", None)
    finally:
        store.close()
    old_issuer, new_issuer = standing.org_issuer(config), f"org:{enrolled['org_id']}"
    if config.enrolled and old_issuer != new_issuer:
        revoked = standing.leave_org(config.home, old_issuer)
        if left is not None:
            left.update({"issuer": old_issuer, "standing": revoked})
    updated = config.model_copy(update={"controller_url": url, **{k: enrolled[k] for k in REQUIRED},
                                        **(ceiling.settings(ceiling_by) if ceiling is not None else {})})
    save_config(updated)
    return updated
