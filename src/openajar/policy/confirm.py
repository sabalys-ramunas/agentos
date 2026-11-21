"""How strong a yes is, and where it can be given (docs/CONFIRMATIONS.md).

A rule names a level, never a device. A method says what it is and the level it reaches, so a new
place to say yes is a new row in the registry and no rule or decide route changes. Nothing outside
`default_registry` names a device.

The level of a decision is worked out by the node from how it arrived (`arrival`, a panel route, the
chat tool, the channel). A caller never sets it. `MethodRegistry.verify` then holds every proof to
what its method can reach, so a proof cannot say more than its method does.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Literal, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from openajar.core.errors import InvalidRequest
from openajar.policy.errors import MethodUnavailable, ProofTooWeak
from openajar.policy.store import DB
from openajar.policy.util import iso, new_id


class ConfirmLevel(IntEnum):
    """Ordered, weakest first. A yes counts when its level is at or above what the approval requires."""
    CHAT = 1        # a person answered a prompt their chat client showed them
    SIGNED_IN = 2   # a person in a signed-in session
    PRESENT = 3     # a person proved they are here, just now, for this approval
    DEVICE_KEY = 4  # a key held on a device signed this exact content

    @property
    def word(self) -> str:
        return self.name.lower()

    @classmethod
    def of(cls, word: Any) -> ConfirmLevel:
        for level in cls:
            if level.word == word:
                return level
        raise ValueError(f"{str(word)[:40]!r} is not a level ({', '.join(x.word for x in cls)})")


LEVEL_WORDS = tuple(level.word for level in ConfirmLevel)
ALWAYS_ASK = ("pay", "irreversible")  # a person every time, and never through chat
# what a decision reaches when it proves no person: a bearer token, or a controller that sends no
# proof. It reaches what it reached before levels existed, and no further
UNPROVEN_REACH = ConfirmLevel.SIGNED_IN
# how a signed-in session came to be. Not "device": the owner computer's saved credential sits where an
# agent on that computer can read it, so it proves no person (docs/THREAT-MODEL.md)
SIGNED_IN_VIA = frozenset({"session", "passkey", "code", "cli"})
TOKEN_VIA = frozenset({"owner_token", "token"})


@dataclass(frozen=True)
class Proof:
    """What a decision carries: `{method, level, at, detail?}`. `detail` holds nothing secret and no
    content. `method` and `level` are None when the decision proves no person: then `reach`, not
    `level`, says what it may still decide, and what is recorded says so."""
    method: str | None
    level: ConfirmLevel | None
    at: float = 0.0
    detail: dict[str, Any] | None = None
    reach: ConfirmLevel | None = None
    person: str | None = None  # whose device signed: they must be able to decide this themselves

    @property
    def counts(self) -> ConfirmLevel | None:
        """What this decision counts as against what an approval requires."""
        return self.level if self.level is not None else self.reach


@dataclass(frozen=True)
class Person:
    """Who a method is asked about: the facts a method needs, none of them a name of a device."""
    id: str
    passkey: bool = False  # holds a passkey (panel.db), so a fresh check can be asked of them


@dataclass(frozen=True)
class Delivery:
    """How a method reaches a person for one approval."""
    method: str
    how: str


@dataclass(frozen=True)
class Verified:
    """A proof checked by its method: the level it reached, or why it was refused."""
    level: ConfirmLevel | None
    refused: str | None = None
    person: str | None = None


class ConfirmMethod(Protocol):
    id: str
    label: str
    level: ConfirmLevel  # the highest level this method can reach
    status: Literal["ready", "planned"]

    def available(self, person: Person) -> bool:
        """Can this person use it at all?"""

    def deliver(self, approval: Any, person: Person) -> Delivery | None:
        """How it reaches them for this approval; None when it cannot carry this approval."""

    def verify(self, approval: Any, proof: Proof) -> Verified:
        """Proof to level, or refused."""


class Method:
    """A method that reaches its level by arriving through a route the node itself controls."""
    optional = False  # the owner's setting can turn it off
    relayed = True    # a controller may report a decision made this way

    def __init__(self, id: str, label: str, level: ConfirmLevel, *,
                 status: Literal["ready", "planned"] = "ready", how: str = ""):
        self.id, self.label, self.level, self.status, self.how = id, label, level, status, how

    def available(self, person: Person) -> bool:
        return True

    def deliver(self, approval: Any, person: Person) -> Delivery | None:
        return Delivery(self.id, self.how)

    def verify(self, approval: Any, proof: Proof) -> Verified:
        if proof.level is None:
            return Verified(None, f"a {self.id} proof names no level")
        return Verified(min(proof.level, self.level))


class PasskeyMethod(Method):
    def available(self, person: Person) -> bool:
        return person.passkey


def chat_refusal(approval: Any) -> str | None:
    """Why this approval can never be answered in chat, whatever the setting says, or None. Chat is
    for what a person already wants: a hijacked agent lives in the same place as the chat client, and
    a person can only say yes to what the prompt shows them."""
    if approval.kind != "action":
        return "a question is answered in the panel: its words are the agent's"
    if approval.effect in ALWAYS_ASK:
        return f"{approval.effect} never takes a chat answer"
    card = getattr(approval, "card", None)  # a publish's prompt shows its card, with what it cannot show said in it
    publish = isinstance(card, dict) and card.get("type") == "publish"
    if approval.follows and not publish:
        return "it follows untrusted text"
    if approval.requires != ConfirmLevel.CHAT.word:
        return f"it needs {approval.requires or 'more than chat'}"
    if not publish and (not approval.preview or approval.preview_cut()):
        return "the prompt could not show the whole content"
    return None


class ChatMethod(Method):
    optional = True
    relayed = False  # the answer goes from the client to the node, never through anyone's word for it

    def deliver(self, approval: Any, person: Person) -> Delivery | None:
        return None if chat_refusal(approval) else Delivery(self.id, self.how)

    def verify(self, approval: Any, proof: Proof) -> Verified:
        why = chat_refusal(approval)
        return Verified(None, why) if why else Verified(ConfirmLevel.CHAT)


class TelegramMethod(Method):
    """Buttons in the owner's own Telegram chat with this node's bot (openajar.policy.telegram). It reaches
    `chat` and carries exactly what chat may carry: `chat_refusal` decides, here at decision time and not
    only by leaving a button out. Only the owner pairs a Telegram account, so it reaches the owner's own
    approvals only, and only while one is paired."""
    optional = True
    relayed = False  # the tap goes from Telegram to the node, never through a controller's word for it

    def __init__(self, id: str, label: str, *, paired: Callable[[], bool] | None = None, how: str = ""):
        super().__init__(id, label, ConfirmLevel.CHAT, how=how)
        self.paired = paired

    def available(self, person: Person) -> bool:
        return person.id == "owner" and self.paired is not None and bool(self.paired())

    def deliver(self, approval: Any, person: Person) -> Delivery | None:
        return None if chat_refusal(approval) else Delivery(self.id, self.how)

    def verify(self, approval: Any, proof: Proof) -> Verified:
        why = chat_refusal(approval)
        if why:
            return Verified(None, why)
        if self.id not in (getattr(approval, "methods", None) or []):
            return Verified(None, "Telegram was not offered for this approval when it was made")
        return Verified(ConfirmLevel.CHAT)


def signed_message(approval: Any) -> bytes:
    """What a device signs: the approval's content digest, tied to this approval and this purpose, so
    a signature made for one approval cannot be replayed on another that holds the same content."""
    return f"agentos-confirm-v1|{approval.id}|{approval.content_hash}|{approval.content_digest}".encode()


class DeviceKeyMethod(Method):
    """The seam for a device: the node checks a signature over the content digest against the public
    key it holds. The private key never leaves the device, so neither the node nor the cloud signs."""

    def __init__(self, id: str, label: str, devices: DeviceStore | None, *,
                 status: Literal["ready", "planned"] = "planned", how: str = ""):
        super().__init__(id, label, ConfirmLevel.DEVICE_KEY, status=status, how=how)
        self.devices = devices

    def available(self, person: Person) -> bool:
        return self.devices is not None and bool(self.devices.live(person.id, self.id))

    def verify(self, approval: Any, proof: Proof) -> Verified:
        detail = proof.detail or {}
        device_id, signature = detail.get("device"), detail.get("signature")
        device = self.devices.get(device_id) if self.devices is not None and isinstance(device_id, str) else None
        if device is None or device.method != self.id or device.revoked_at is not None:
            return Verified(None, "this device is not one the node holds for this method")
        if not approval.content_digest or not check_signature(device.public_key, signature, signed_message(approval)):
            return Verified(None, "the device's signature does not match this content")
        self.devices.touch(device.id)
        return Verified(ConfirmLevel.DEVICE_KEY, person=device.person)


def check_signature(public_key: str, signature: Any, message: bytes) -> bool:
    """An Ed25519 signature, both base64url. False for anything malformed."""
    try:
        key = Ed25519PublicKey.from_public_bytes(_unb64(public_key))
        key.verify(_unb64(signature), message)
    except (InvalidSignature, ValueError, TypeError, binascii.Error):
        return False
    return True


def _unb64(text: Any) -> bytes:
    if not isinstance(text, str):
        raise TypeError("not base64url text")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class MethodRegistry:
    """Every method the node knows. Code asks the registry; it does not name devices."""

    def __init__(self, methods: Iterable[ConfirmMethod] = ()):
        self._methods: dict[str, ConfirmMethod] = {}
        for m in methods:
            self.register(m)

    def register(self, method: ConfirmMethod) -> None:
        self._methods[method.id] = method

    def all(self) -> list[ConfirmMethod]:
        return list(self._methods.values())

    def get(self, method_id: Any) -> ConfirmMethod:
        """The method, if it can be used now. A planned or unknown method is `method_unavailable`."""
        m = self._methods.get(method_id) if isinstance(method_id, str) else None
        if m is None:
            raise MethodUnavailable(f"{str(method_id)[:40]!r} is not a way to confirm on this node")
        if m.status != "ready":
            raise MethodUnavailable(f"{m.label} is planned and cannot be used yet")
        return m

    def highest_ready(self) -> ConfirmLevel:
        return max(m.level for m in self._methods.values() if m.status == "ready")

    def verify(self, approval: Any, proof: Proof, *, off: Iterable[str] = ()) -> Proof:
        """Hold a proof to its method: the level it counts as, never more than the method reaches.
        A proof with no method (no person proved) passes unchanged: only its `reach` counts."""
        if proof.method is None:
            return proof
        method = self.get(proof.method)
        if method.id in set(off):
            raise MethodUnavailable(f"{method.label} is turned off on this node")
        verified = method.verify(approval, proof)
        if verified.level is None:
            raise ProofTooWeak(requires_of(approval).word, verified.refused or "this proof was not accepted", approval.id)
        return Proof(method.id, min(verified.level, method.level), proof.at, proof.detail, person=verified.person)

    def reaching(self, approval: Any, person: Person, *, off: Iterable[str] = ()) -> list[str]:
        """`Approval.methods`: the ready methods that could reach this approval for this person."""
        need = requires_of(approval)
        off = set(off)
        return [m.id for m in self._methods.values()
                if m.status == "ready" and m.id not in off and m.level >= need and m.available(person)
                and m.deliver(approval, person) is not None]


def default_registry(devices: DeviceStore | None = None, *,
                     telegram_paired: Callable[[], bool] | None = None) -> MethodRegistry:
    """The methods of docs/CONFIRMATIONS.md. This table is the only place that names a device.
    `telegram_paired`: is a Telegram account paired now (without it, Telegram reaches nobody)."""
    return MethodRegistry([
        Method("panel", "The panel", ConfirmLevel.SIGNED_IN, how="open Needs you in the panel, signed in"),
        Method("cli", "The node's command line", ConfirmLevel.SIGNED_IN, how="openajar approvals, on the node"),
        PasskeyMethod("passkey", "A passkey", ConfirmLevel.PRESENT, how="a fresh passkey check in the panel"),
        ChatMethod("chat", "A prompt in your chat client", ConfirmLevel.CHAT, how="approval_confirm"),
        TelegramMethod("telegram", "Buttons in your Telegram chat with the node's bot", paired=telegram_paired,
                       how="a message from the node's Telegram bot"),
        DeviceKeyMethod("mac", "A Mac", devices),
        DeviceKeyMethod("ios", "An iPhone", devices),
        DeviceKeyMethod("android", "An Android phone", devices),
        DeviceKeyMethod("watch", "A watch", devices),
        Method("totp", "An authenticator code", ConfirmLevel.PRESENT, status="planned"),
    ])


def legacy_requires(effect: str | None) -> ConfirmLevel:
    """For an approval with no stored level (made before levels existed): never less than it needed
    then, and never chat."""
    return ConfirmLevel.PRESENT if effect in ALWAYS_ASK else ConfirmLevel.SIGNED_IN


def requires_of(approval: Any) -> ConfirmLevel:
    if approval.requires:
        return ConfirmLevel.of(approval.requires)
    return legacy_requires(approval.effect if approval.kind == "action" else None)


def arrival(*, via: str | None, decider: Any = None, in_person: bool = False, at: float = 0.0) -> Proof:
    """The proof of a decision from how it arrived, for a caller that has none to add: the node's CLI
    (no decider, no via), a signed-in panel session, a bearer token. A token proves no person, so it
    names no method and no level, however it reads. Anything else proves nothing."""
    if via in TOKEN_VIA or (decider is not None and not in_person):
        return Proof(None, None, at, reach=UNPROVEN_REACH)
    if via is None and decider is None:
        return Proof("cli", ConfirmLevel.SIGNED_IN, at)
    if via is None or via in SIGNED_IN_VIA:
        return Proof("panel", ConfirmLevel.SIGNED_IN, at)
    return Proof(None, None, at)


def controller_proof(claim: Any, methods: MethodRegistry, limit: ConfirmLevel, *, at: float = 0.0) -> Proof:
    """A decision a controller relays. Its word counts up to `limit`, the owner's own setting on this
    node, so a compromised cloud can claim no more than that. Leaving the proof out gains nothing: it
    then proves no person, and reaches no further than a signed-in session. A device's proof is not its
    word: `verify` checks the signature against the key the node holds, so the limit does not bind it."""
    if claim is None:
        return Proof(None, None, at, reach=min(UNPROVEN_REACH, limit))
    if not isinstance(claim, dict):
        raise InvalidRequest("proof is {method, level}")
    method = methods.get(claim.get("method"))
    if not getattr(method, "relayed", True):
        raise MethodUnavailable(f"{method.label} answers the node directly, so a controller cannot report it")
    try:
        level = ConfirmLevel.of(claim.get("level"))
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from None
    if method.level == ConfirmLevel.DEVICE_KEY:
        return Proof(method.id, level, at, {k: claim[k] for k in ("device", "signature") if isinstance(claim.get(k), str)})
    return Proof(method.id, min(level, method.level, limit), at)


# ---- a person's devices: the table a future method fills ----

SCHEMA = """
CREATE TABLE IF NOT EXISTS confirm_devices (
  id TEXT PRIMARY KEY, person TEXT NOT NULL, method TEXT NOT NULL, public_key TEXT NOT NULL,
  label TEXT NOT NULL, added_at REAL NOT NULL, last_used_at REAL, revoked_at REAL
);
CREATE INDEX IF NOT EXISTS confirm_devices_person ON confirm_devices(person, method);
"""


@dataclass(frozen=True)
class Device:
    """A person's device. The node holds its public key only."""
    id: str
    person: str
    method: str
    public_key: str
    label: str
    added_at: float
    last_used_at: float | None = None
    revoked_at: float | None = None

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "person": self.person, "method": self.method, "public_key": self.public_key,
                "label": self.label, "added_at": iso(self.added_at), "last_used_at": iso(self.last_used_at),
                "revoked_at": iso(self.revoked_at)}


class DeviceStore:
    """Rows in `policy.db`. Enrolling is the job of a future method: no route reaches `add`."""

    def __init__(self, db: DB, *, clock: Callable[[], float]):
        self.db = db
        self.clock = clock
        with db.lock:
            db.conn.executescript(SCHEMA)

    def add(self, *, person: str, method: str, public_key: str, label: str) -> Device:
        device = Device(new_id("dev"), person, method, public_key, label, self.clock())
        self.db.execute("INSERT INTO confirm_devices (id, person, method, public_key, label, added_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (device.id, person, method, public_key, label, device.added_at))
        return device

    def get(self, device_id: str) -> Device | None:
        row = self.db.one("SELECT * FROM confirm_devices WHERE id = ?", (device_id,))
        return Device(**dict(row)) if row is not None else None

    def live(self, person: str, method: str) -> list[Device]:
        rows = self.db.all("SELECT * FROM confirm_devices WHERE person = ? AND method = ? AND revoked_at IS NULL "
                           "ORDER BY added_at", (person, method))
        return [Device(**dict(r)) for r in rows]

    def touch(self, device_id: str) -> None:
        self.db.execute("UPDATE confirm_devices SET last_used_at = ? WHERE id = ?", (self.clock(), device_id))

    def revoke(self, device_id: str) -> None:
        self.db.execute("UPDATE confirm_devices SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                        (self.clock(), device_id))
