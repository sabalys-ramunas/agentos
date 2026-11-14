"""Sealing primitives.

- Fields are sealed with ChaCha20-Poly1305 (random 96-bit nonce per seal) under an
  encryption subkey of the data key, with the entry id, field name and key version as
  associated data, so a ciphertext cannot be moved to another entry or field.
- The data key is wrapped by the master key (same AEAD).
- The master key is either 32 random bytes in `secrets/vault.key` (600, owned by us,
  in a 700 directory, never a symlink) or derived from a passphrase with Argon2id
  (scrypt where the OpenSSL build lacks Argon2).
- Entry binding metadata (domain, hosts, lanes, kind, config) is authenticated with
  HMAC-SHA256 under a separate subkey, so editing vault.db cannot rebind a secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import stat
import unicodedata
from pathlib import Path

from cryptography.exceptions import InvalidTag, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from agentos.vault.errors import VaultCorrupt, VaultKeyInsecure, VaultKeyMissing

KEY_LEN = 32
NONCE_LEN = 12

DEFAULT_KDF = {"alg": "argon2id", "m": 65536, "t": 3, "p": 4}   # 64 MiB, 3 passes
SCRYPT_KDF = {"alg": "scrypt", "n": 2**15, "r": 8, "p": 1}
_KDF_LIMITS = {"m": (8, 4 * 1024 * 1024), "t": (1, 32), "p": (1, 64), "n": (2**10, 2**22), "r": (1, 32)}


class DecryptError(Exception):
    """Authentication failed (wrong key, or the ciphertext was altered). Carries no data."""


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def seal(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(NONCE_LEN)
    return nonce + ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad)


def unseal(key: bytes, blob: bytes, aad: bytes) -> bytes:
    if not isinstance(blob, bytes | bytearray) or len(blob) < NONCE_LEN + 16:
        raise DecryptError()
    try:
        return ChaCha20Poly1305(bytes(key)).decrypt(bytes(blob[:NONCE_LEN]), bytes(blob[NONCE_LEN:]), aad)
    except (InvalidTag, ValueError):
        raise DecryptError() from None


def subkeys(data_key: bytes) -> tuple[bytes, bytes]:
    """(encryption key, metadata MAC key) derived from the data key."""
    def derive(info: bytes) -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=KEY_LEN, salt=None, info=info).derive(bytes(data_key))
    return derive(b"agentos-vault enc v1"), derive(b"agentos-vault mac v1")


def mac(key: bytes, payload: bytes) -> str:
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def mac_ok(key: bytes, payload: bytes, tag: str) -> bool:
    return hmac.compare_digest(mac(key, payload), tag or "")


# ---- passphrase KDF -------------------------------------------------------------

def argon2_available() -> bool:
    try:
        from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

        Argon2id(salt=b"\0" * 16, length=32, iterations=1, lanes=1, memory_cost=8).derive(b"x")
        return True
    except (ImportError, UnsupportedAlgorithm):
        return False


def new_kdf(params: dict | None = None) -> dict:
    """KDF parameters with a fresh salt, ready to store in the vault header."""
    params = dict(params or (DEFAULT_KDF if argon2_available() else SCRYPT_KDF))
    if params["alg"] == "argon2id" and not argon2_available():
        params = dict(SCRYPT_KDF)
    params["salt"] = b64e(os.urandom(16))
    _check_kdf(params)
    return params


def _check_kdf(kdf: dict) -> None:
    if kdf.get("alg") not in ("argon2id", "scrypt"):
        raise VaultCorrupt("vault header names an unknown key derivation function")
    for k, (lo, hi) in _KDF_LIMITS.items():
        if k in kdf and not (isinstance(kdf[k], int) and lo <= kdf[k] <= hi):
            raise VaultCorrupt("vault header has out-of-range key derivation parameters")
    try:
        if len(b64d(kdf["salt"])) < 16:
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise VaultCorrupt("vault header has a malformed salt") from None


def normalize_passphrase(passphrase: str) -> bytes:
    return unicodedata.normalize("NFC", passphrase).encode("utf-8")


def derive_master(passphrase: str, kdf: dict) -> bytes:
    _check_kdf(kdf)
    salt, pw = b64d(kdf["salt"]), normalize_passphrase(passphrase)
    if kdf["alg"] == "argon2id":
        from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

        return Argon2id(salt=salt, length=KEY_LEN, iterations=kdf["t"], lanes=kdf["p"],
                        memory_cost=kdf["m"]).derive(pw)
    return Scrypt(salt=salt, length=KEY_LEN, n=kdf["n"], r=kdf["r"], p=kdf["p"]).derive(pw)


# ---- key file -------------------------------------------------------------------

def _check_private_dir(path: Path) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise VaultKeyMissing(f"{path} does not exist") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise VaultKeyInsecure(f"{path} must be a real directory, not a symlink")
    if st.st_uid != os.geteuid():
        raise VaultKeyInsecure(f"{path} is owned by another user")
    if st.st_mode & 0o077:
        raise VaultKeyInsecure(
            f"{path} has permissions {stat.S_IMODE(st.st_mode):o}; it must be 700 (run: chmod 700 {path})"
        )


def read_keyfile(path: Path) -> bytes:
    """The master key from a key file, refusing anything another user could read."""
    path = Path(path)
    _check_private_dir(path.parent)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise VaultKeyMissing(f"vault key file {path} is missing") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise VaultKeyInsecure(f"vault key file {path} must be a regular file, not a symlink")
    if st.st_uid != os.geteuid():
        raise VaultKeyInsecure(f"vault key file {path} is owned by another user")
    if st.st_mode & 0o077:
        raise VaultKeyInsecure(
            f"vault key file {path} has permissions {stat.S_IMODE(st.st_mode):o}; "
            f"refusing to use it (run: chmod 600 {path})"
        )
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        raw = os.read(fd, 4096)
    finally:
        os.close(fd)
    try:
        key = b64d(raw.decode("ascii").strip())
    except (ValueError, UnicodeDecodeError):
        key = b""
    if len(key) != KEY_LEN:
        raise VaultCorrupt(f"vault key file {path} is malformed")
    return key


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def write_keyfile(path: Path, key: bytes) -> None:
    """Write a key file atomically enough for our use: 600, fsynced, never through a symlink."""
    path = Path(path)
    ensure_private_dir(path.parent)
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise VaultKeyInsecure(f"{path} is a symlink; refusing to write a key through it")
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, (b64e(key) + "\n").encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    fsync_dir(path.parent)


def fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
