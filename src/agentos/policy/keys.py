"""The policy root key: an Ed25519 keypair in `$AGENTOS_HOME/secrets/policy.key` (mode 600),
and the key ring that rotates it (KeyRing).

Grants are signed by the private half; anything holding `policy.pub` can verify them
offline. Like ssh, a key file readable by group or others is refused rather than used.
Creation is race-free: the key is written to a temp file and hard-linked into place, so
two processes starting at once end up with the same key.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import stat
import time
from collections.abc import Callable
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from agentos.core import fs
from agentos.policy.errors import Denied, PolicyKeyError
from agentos.policy.tokens import key_id
from agentos.policy.util import new_id

log = logging.getLogger("agentos.policy.keys")

KEY_NAME = "policy.key"
PUB_NAME = "policy.pub"


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _load(path: Path) -> Ed25519PrivateKey:
    for _ in range(50):  # another process may be between link() and fsync on a slow disk
        st = path.stat()
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise PolicyKeyError(
                f"{path} is accessible by other users (mode {oct(st.st_mode & 0o777)}); run chmod 600 on it"
            )
        data = path.read_bytes()
        if data:
            break
        time.sleep(0.02)
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError):
        raise PolicyKeyError(f"{path} is not a valid policy key") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise PolicyKeyError(f"{path} is not an Ed25519 key")
    return key


def load_or_create_root_key(secrets_dir: Path) -> Ed25519PrivateKey:
    _ensure_dir(secrets_dir)
    path = secrets_dir / KEY_NAME
    if path.exists():
        return _load(path)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    tmp = secrets_dir / f".{KEY_NAME}.{new_id('tmp')}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            return _load(path)
    finally:
        tmp.unlink(missing_ok=True)
    write_public_key(secrets_dir, key.public_key())
    return key


def write_public_key(secrets_dir: Path, pub: Ed25519PublicKey) -> Path:
    path = secrets_dir / PUB_NAME
    raw = pub.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    path.write_bytes(raw)
    os.chmod(path, 0o644)
    return path


def load_public_key(path: Path) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(path.read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise PolicyKeyError(f"{path} is not an Ed25519 public key")
    return key


def derive_key(root: Ed25519PrivateKey, purpose: str) -> bytes:
    """A 32-byte symmetric key for one purpose (approval action links), derived with HKDF."""
    seed = root.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=f"agentos {purpose} v1".encode()).derive(seed)


# ---------------------------------------------------------------- the key ring (rotation)

RING_NAME = "policy-ring.json"
LINK_KEY_NAME = "approval-links.key"
DEFAULT_OVERLAP_S = 7 * 86400


def _write_private(path: Path, data: bytes) -> None:
    """Write a secret file atomically with mode 600 (agentos.core.fs: temp file, fsync, rename)."""
    fs.write_private(path, data)


def _pem(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


class KeyRing:
    """The node's grant signing keys. `secrets/policy.key` is the active key: new grants are
    signed with it. `agentos grants rotate-key` moves it to `secrets/policy-<kid hex>.key` and
    lists it in `secrets/policy-ring.json` with a retirement time: until then grants signed with
    it keep verifying (and the node can still re-sign their chains for brain child grants);
    after it, they are refused with `retired_key`, and the old private key is deleted.

    Version 1 tokens carry no key id. They verify only against the active key and only while
    the ring has never rotated: rotating the key is how the owner ends the v1 migration window.
    A running daemon notices a rotation made by the CLI (`refresh` compares file times)."""

    def __init__(self, secrets_dir: Path, *, clock: Callable[[], float] = time.time):
        self.dir = Path(secrets_dir)
        self.clock = clock
        self.active = load_or_create_root_key(self.dir)
        self._stamp: tuple = ()
        self._ring: list[dict] = []
        self.refresh(force=True)

    # ---- state ----
    def _files_stamp(self) -> tuple:
        out: list[tuple[int, int, int] | None] = []
        for name in (KEY_NAME, RING_NAME):
            try:
                st = (self.dir / name).stat()
                out.append((st.st_mtime_ns, st.st_size, st.st_ino))
            except FileNotFoundError:
                out.append(None)
        return tuple(out)

    def refresh(self, *, force: bool = False) -> None:
        stamp = self._files_stamp()
        if not force and stamp == self._stamp:
            return
        self.active = _load(self.dir / KEY_NAME)
        path = self.dir / RING_NAME
        ring = []
        if path.exists():
            try:
                ring = [e for e in json.loads(path.read_text()).get("keys", []) if isinstance(e, dict)]
            except (ValueError, AttributeError):
                log.error("%s is not valid JSON; only the active grant key is trusted", path)
        self._ring = ring
        self._stamp = stamp
        self._purge()

    @property
    def active_kid(self) -> str:
        return kid_of(self.active)

    @property
    def rotated(self) -> bool:
        """True once the key was ever rotated (version 1 tokens are refused from then on)."""
        return bool(self._ring)

    def entries(self) -> list[dict]:
        """Every key the node has had: kid, state (active, retiring, retired) and times."""
        now = self.clock()
        out = [{"kid": self.active_kid, "state": "active", "retire_at": None}]
        for e in self._ring:
            if e.get("kid") == self.active_kid:
                continue
            retire_at = e.get("retire_at")
            state = "retired" if retire_at is not None and retire_at <= now else "retiring"
            out.append({"kid": e.get("kid"), "state": state, "retire_at": retire_at,
                        "rotated_at": e.get("rotated_at")})
        return out

    # ---- lookups ----
    def _entry(self, kid: str) -> dict | None:
        return next((e for e in self._ring if e.get("kid") == kid), None)

    def _live(self, entry: dict) -> bool:
        retire_at = entry.get("retire_at")
        return retire_at is None or retire_at > self.clock()

    def check(self, kid: str | None) -> None:
        """Denied unless `kid` is the active key or a rotated key inside its overlap."""
        if kid is not None and kid == self.active_kid:
            return
        entry = self._entry(kid) if kid else None
        if entry is None:
            raise Denied("unknown_key", "this grant was signed with a key this node does not know")
        if not self._live(entry):
            self._purge()
            raise Denied("retired_key", "this grant was signed with a key the owner has retired; ask for a new grant")

    def public_for(self, kid: str | None) -> Ed25519PublicKey:
        self.check(kid)
        if kid == self.active_kid:
            return self.active.public_key()
        return self._private(kid).public_key()

    def private_for(self, kid: str | None) -> Ed25519PrivateKey:
        """The signing key for `kid` (to re-sign a registered chain); Denied when retired."""
        self.check(kid)
        return self.active if kid == self.active_kid else self._private(kid)

    def _private(self, kid: str | None) -> Ed25519PrivateKey:
        path = self.dir / _ring_file(kid or "")
        try:
            return _load(path)
        except (FileNotFoundError, PolicyKeyError):
            raise Denied("retired_key", "the key this grant was signed with is no longer on this node") from None

    # ---- changes ----
    def rotate(self, *, overlap_s: int = DEFAULT_OVERLAP_S) -> dict:
        """Make a fresh key active; the old one keeps verifying for `overlap_s` seconds."""
        if overlap_s < 0:
            raise ValueError("the overlap cannot be negative")
        self.refresh()
        now = self.clock()
        old = self.active
        old_kid = kid_of(old)
        link_key = self.dir / LINK_KEY_NAME
        if not link_key.exists():  # pending approval links stay valid across the rotation
            _write_private(link_key, derive_key(old, "approval links"))
        _write_private(self.dir / _ring_file(old_kid), _pem(old))
        ring = [e for e in self._ring if e.get("kid") != old_kid]
        ring.append({"kid": old_kid, "rotated_at": now, "retire_at": now + overlap_s})
        self._write_ring(ring)
        new = Ed25519PrivateKey.generate()
        _write_private(self.dir / KEY_NAME, _pem(new))
        write_public_key(self.dir, new.public_key())
        self.refresh(force=True)
        return {"kid": kid_of(new), "previous": old_kid, "retire_at": now + overlap_s}

    def retire(self, kid: str, *, at: float | None = None) -> dict:
        """Retire a rotated key at `at` (now by default). The active key cannot be retired."""
        self.refresh()
        if kid == self.active_kid:
            raise ValueError("the active key cannot be retired; rotate first")
        entry = self._entry(kid)
        if entry is None:
            raise ValueError(f"no rotated key {kid}")
        when = self.clock() if at is None else at
        self._write_ring([{**e, "retire_at": when} if e.get("kid") == kid else e for e in self._ring])
        self.refresh(force=True)
        return {"kid": kid, "retire_at": when}

    def _write_ring(self, ring: list[dict]) -> None:
        path = self.dir / RING_NAME
        tmp = path.with_name(f".{path.name}.{new_id('tmp')}")
        tmp.write_text(json.dumps({"keys": ring}, indent=1))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def _purge(self) -> None:
        """Delete the private half of retired keys (the ring keeps their kid, for the reason)."""
        for e in self._ring:
            if not self._live(e):
                with contextlib.suppress(FileNotFoundError, OSError):
                    (self.dir / _ring_file(e.get("kid") or "")).unlink()


def kid_of(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    return key_id(key)


def _ring_file(kid: str) -> str:
    return "policy-" + kid.removeprefix("ed25519:") + ".key"


def link_key(secrets_dir: Path, active: Ed25519PrivateKey) -> bytes:
    """The approval-link key: saved at the first rotation, else derived from the active key."""
    path = Path(secrets_dir) / LINK_KEY_NAME
    if path.exists():
        data = path.read_bytes()
        if len(data) == 32:
            return data
        log.error("%s is not a 32-byte key; deriving the link key from the grant key", path)
    return derive_key(active, "approval links")
