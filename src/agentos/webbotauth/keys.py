"""Per-lane Web Bot Auth keys: `$AGENTOS_HOME/secrets/webbotauth/<workspace>/<name>.key`.

One Ed25519 keypair per lane (a lane is one identity), PEM PKCS#8, mode 600, in directories of
mode 700. The file rules are those of `policy/keys.py`: a key file or directory that group or
others can reach is refused rather than used, and creation is race-free (a 600 temp file,
fsync, then a hard link into place, so two processes creating at once end up with one key).

Keys live under `secrets/`, never in the lane's directory: profiles are copied for snapshots,
forks and workspace export, and a copied key would make two lanes look like one (design
section 5.2). The key never leaves the node process: only `LaneKey.sign_request` uses it, and it
signs nothing but a Web Bot Auth signature base.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import stat
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from agentos.core.errors import AgentosError
from agentos.core.models import parse_lane_id
from agentos.webbotauth import base

DIR_NAME = "webbotauth"


class IdentityError(AgentosError):
    code = "identity_error"


def identity_dir(secrets_dir: Path) -> Path:
    return Path(secrets_dir) / DIR_NAME


def lane_path(secrets_dir: Path, lane_id: str, suffix: str) -> Path:
    workspace, name = parse_lane_id(lane_id)
    return identity_dir(secrets_dir) / workspace / f"{name}{suffix}"


def key_path(secrets_dir: Path, lane_id: str) -> Path:
    return lane_path(secrets_dir, lane_id, ".key")


def ensure_private_dirs(secrets_dir: Path, lane_id: str) -> Path:
    """secrets/, secrets/webbotauth/ and the workspace directory, each mode 700."""
    workspace, _ = parse_lane_id(lane_id)
    secrets_dir = Path(secrets_dir)
    for d in (secrets_dir, identity_dir(secrets_dir), identity_dir(secrets_dir) / workspace):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    return identity_dir(secrets_dir) / workspace


def check_private(path: Path, *, kind: str = "file") -> os.stat_result:
    """Refuse a file or directory that is a symlink, or that group or others can reach."""
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode):
        raise IdentityError(f"{path} is a symlink; identity {kind}s must be plain {kind}s")
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise IdentityError(f"{path} is accessible by other users (mode {oct(st.st_mode & 0o777)}); "
                            f"run chmod {'700' if kind == 'directory' else '600'} on it")
    return st


def _check_parents(path: Path, secrets_dir: Path) -> None:
    for d in (path.parent, identity_dir(secrets_dir)):
        check_private(d, kind="directory")


@dataclass(frozen=True)
class LaneKey:
    lane_id: str
    keyid: str
    _private: Ed25519PrivateKey

    def __repr__(self) -> str:  # never print key material
        return f"LaneKey(lane_id={self.lane_id!r}, keyid={self.keyid!r})"

    def public_key(self) -> Ed25519PublicKey:
        return self._private.public_key()

    def public_jwk(self) -> dict[str, str]:
        return {**base.public_jwk(self._private), "kid": self.keyid, "use": "sig", "alg": "EdDSA"}

    def sign_request(self, *, method: str, url: str, signature_agent: str | None, form: str = "bare",
                     components: Sequence[str] = base.COMPONENTS, window: int = base.WINDOW,
                     now: float | None = None) -> base.Signed:
        return base.sign_request(self._private, method=method, url=url, signature_agent=signature_agent,
                                 form=form, components=components, window=window, now=now)


def _load_file(path: Path, lane_id: str) -> LaneKey:
    data = b""
    for _ in range(50):  # another process may be between link() and fsync on a slow disk
        check_private(path)
        data = path.read_bytes()
        if data:
            break
        time.sleep(0.02)
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError):
        raise IdentityError(f"{path} is not a valid identity key") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise IdentityError(f"{path} is not an Ed25519 key")
    return LaneKey(lane_id, base.keyid_of(key), key)


def load_key(secrets_dir: Path, lane_id: str) -> LaneKey | None:
    """The lane's key, or None when it has none. Raises IdentityError for an unsafe file."""
    path = key_path(secrets_dir, lane_id)
    if not path.exists() and not path.is_symlink():
        return None
    _check_parents(path, secrets_dir)
    return _load_file(path, lane_id)


def create_key(secrets_dir: Path, lane_id: str, *, replace: bool = False) -> LaneKey:
    """Make the lane's key (or return the existing one unless `replace`)."""
    folder = ensure_private_dirs(secrets_dir, lane_id)
    path = key_path(secrets_dir, lane_id)
    if path.exists() and not replace:
        return _load_file(path, lane_id)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    tmp = folder / f".{path.name}.{secrets.token_hex(8)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
            f.flush()
            os.fsync(f.fileno())
        if replace:
            os.replace(tmp, path)
        else:
            try:
                os.link(tmp, path)
            except FileExistsError:
                return _load_file(path, lane_id)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    return LaneKey(lane_id, base.keyid_of(key), key)


def delete_key(secrets_dir: Path, lane_id: str) -> bool:
    path = key_path(secrets_dir, lane_id)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True
