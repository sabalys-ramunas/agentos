"""age v1 encryption (https://age-encryption.org/v1), streaming.

Backups and workspace exports use the age file format so an owner can open one without
openajar, with the stock tool: `age -d agentos-node-1-....tar.gz.age > backup.tar.gz`. Two
recipient types are implemented, the two the stock tool makes:

  scrypt   a passphrase: one "-> scrypt <salt> <log2 N>" stanza whose body is the 16-byte
           file key sealed with ChaCha20-Poly1305 under scrypt(passphrase,
           "age-encryption.org/v1/scrypt" + salt, N, r=8, p=1); it must be the only stanza
  X25519   an age public key (`age1...`, from `age-keygen`): one "-> X25519 <share>" stanza
           per recipient, the file key sealed under HKDF(X25519(ephemeral, recipient),
           share + recipient, "age-encryption.org/v1/X25519"); opened with the matching
           identity (`AGE-SECRET-KEY-1...`)

  header   "age-encryption.org/v1", the stanzas, then "--- " + HMAC-SHA256 of the header
           under HKDF(file key, "header")
  payload  a 16-byte nonce, then STREAM: 64 KiB chunks sealed with ChaCha20-Poly1305 under
           HKDF(file key, nonce, "payload"), nonce = 11-byte big-endian counter + a last-chunk
           flag, so truncation and reordering are detected

Errors carry no key material: WrongPassphrase (the stanza does not open), NoIdentity (the file
is for recipients and no given identity opens it, or it needs a passphrase and got an
identity), CorruptArchive (an authentication failure anywhere else), TruncatedArchive (the
stream stops at a chunk boundary before its final chunk: an interrupted backup or copy),
NotAgeFile.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import unicodedata
from typing import BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

INTRO = b"age-encryption.org/v1"
SCRYPT_LABEL = b"age-encryption.org/v1/scrypt"
X25519_LABEL = b"age-encryption.org/v1/X25519"
RECIPIENT_HRP = "age"
IDENTITY_HRP = "age-secret-key-"
CHUNK = 64 * 1024
TAG = 16
DEFAULT_LOG_N = 18     # what age itself uses: 256 MiB and about a second
MAX_LOG_N = 22         # refuse headers that ask for more than 4 GiB of scrypt memory
MAX_HEADER = 64 * 1024


class AgeError(Exception):
    pass


class NotAgeFile(AgeError):
    pass


class WrongPassphrase(AgeError):
    pass


class CorruptArchive(AgeError):
    pass


class TruncatedArchive(CorruptArchive):
    pass


class NoIdentity(WrongPassphrase):
    """The file is encrypted to age recipients and none of the given identities opens it (or
    it is passphrase-encrypted and only identities were given)."""


class BadKey(AgeError, ValueError):
    """An age recipient or identity string that does not parse."""


# ---- bech32 (BIP 173), the encoding of age keys -------------------------------------------

_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_GEN = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)


def _polymod(values: list[int]) -> int:
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            if (top >> i) & 1:
                chk ^= _GEN[i]
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _regroup(data: bytes | list[int], frm: int, to: int, *, pad: bool) -> list[int]:
    acc = bits = 0
    out: list[int] = []
    top = (1 << to) - 1
    for value in data:
        acc = (acc << frm) | value
        bits += frm
        while bits >= to:
            bits -= to
            out.append((acc >> bits) & top)
    if pad:
        if bits:
            out.append((acc << (to - bits)) & top)
    elif bits >= frm or (acc << (to - bits)) & top:
        raise BadKey("the key has stray bits at its end")
    return out


def bech32_encode(hrp: str, data: bytes) -> str:
    five = _regroup(data, 8, 5, pad=True)
    check = _polymod(_hrp_expand(hrp) + five + [0] * 6) ^ 1
    return hrp + "1" + "".join(_BECH32[d] for d in five + [(check >> 5 * (5 - i)) & 31 for i in range(6)])


def bech32_decode(text: str) -> tuple[str, bytes]:
    if text != text.lower() and text != text.upper():
        raise BadKey("the key mixes upper and lower case")
    text = text.lower()
    sep = text.rfind("1")
    if sep < 1 or sep + 7 > len(text) or any(not 33 <= ord(c) <= 126 for c in text):
        raise BadKey("the key is not bech32")
    hrp, values = text[:sep], [_BECH32.find(c) for c in text[sep + 1:]]
    if -1 in values or _polymod(_hrp_expand(hrp) + values) != 1:
        raise BadKey("the key's checksum does not match (a typo?)")
    return hrp, bytes(_regroup(values[:-6], 5, 8, pad=False))


# ---- X25519 recipients and identities ----------------------------------------------------

def parse_recipient(text: str) -> bytes:
    """`age1...` -> the 32-byte X25519 public key."""
    hrp, key = bech32_decode(text.strip())
    if hrp != RECIPIENT_HRP or len(key) != 32:
        raise BadKey("an age recipient looks like age1... (from age-keygen)")
    return key


def parse_identity(text: str) -> bytes:
    """`AGE-SECRET-KEY-1...` -> the 32-byte X25519 private key."""
    hrp, key = bech32_decode(text.strip())
    if hrp != IDENTITY_HRP or len(key) != 32:
        raise BadKey("an age identity looks like AGE-SECRET-KEY-1... (from age-keygen)")
    return key


def read_identities(text: str) -> list[bytes]:
    """The identities in an age identity file: one per line, `#` comments and blanks ignored."""
    keys = [parse_identity(line) for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not keys:
        raise BadKey("the identity file holds no AGE-SECRET-KEY-1... line")
    return keys


def recipient_of(identity: bytes) -> str:
    public = X25519PrivateKey.from_private_bytes(identity).public_key().public_bytes_raw()
    return bech32_encode(RECIPIENT_HRP, public)


def generate_identity() -> tuple[str, str]:
    """(identity, recipient) strings, as age-keygen prints them."""
    secret = X25519PrivateKey.generate().private_bytes_raw()
    return bech32_encode(IDENTITY_HRP, secret).upper(), recipient_of(secret)


def _x25519_wrap_key(shared: bytes, share: bytes, recipient: bytes) -> bytes:
    if shared == b"\x00" * 32:
        raise CorruptArchive("the archive header is malformed")
    return _hkdf(shared, share + recipient, X25519_LABEL)


def _b64e(data: bytes) -> bytes:
    return base64.b64encode(data).rstrip(b"=")


def _b64d(text: bytes) -> bytes:
    """Canonical unpadded base64, as age requires."""
    if b"=" in text or b"\r" in text:
        raise CorruptArchive("the archive header is malformed")
    try:
        data = base64.b64decode(text + b"=" * (-len(text) % 4), validate=True)
    except ValueError:
        raise CorruptArchive("the archive header is malformed") from None
    if _b64e(data) != text:
        raise CorruptArchive("the archive header is malformed")
    return data


def _hkdf(ikm: bytes, salt: bytes | None, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info).derive(ikm)


def _passphrase(passphrase: str) -> bytes:
    return unicodedata.normalize("NFC", passphrase).encode("utf-8")


def _scrypt(passphrase: str, salt: bytes, log_n: int) -> bytes:
    return Scrypt(salt=SCRYPT_LABEL + salt, length=32, n=2**log_n, r=8, p=1).derive(_passphrase(passphrase))


def _nonce(counter: int, last: bool) -> bytes:
    return counter.to_bytes(11, "big") + (b"\x01" if last else b"\x00")


def _stanza(args: list[bytes], body: bytes) -> list[bytes]:
    encoded = _b64e(body)
    lines = [b"-> " + b" ".join(args)]
    lines += [encoded[i:i + 64] for i in range(0, len(encoded), 64)]
    if len(encoded) % 64 == 0:
        lines.append(b"")
    return lines


def _finish_header(stanzas: list[bytes], file_key: bytes) -> bytes:
    head = b"\n".join([INTRO, *stanzas]) + b"\n---"
    mac = hmac.new(_hkdf(file_key, b"", b"header"), head, hashlib.sha256).digest()
    return head + b" " + _b64e(mac) + b"\n"


def make_header(passphrase: str, file_key: bytes, *, log_n: int = DEFAULT_LOG_N) -> bytes:
    salt = os.urandom(16)
    body = ChaCha20Poly1305(_scrypt(passphrase, salt, log_n)).encrypt(b"\x00" * 12, file_key, None)
    return _finish_header(_stanza([b"scrypt", _b64e(salt), str(log_n).encode()], body), file_key)


def make_recipients_header(recipients: list[bytes], file_key: bytes) -> bytes:
    """One X25519 stanza per recipient public key."""
    lines: list[bytes] = []
    for recipient in recipients:
        ephemeral = X25519PrivateKey.generate()
        share = ephemeral.public_key().public_bytes_raw()
        shared = ephemeral.exchange(X25519PublicKey.from_public_bytes(recipient))
        body = ChaCha20Poly1305(_x25519_wrap_key(shared, share, recipient)).encrypt(b"\x00" * 12, file_key, None)
        lines += _stanza([b"X25519", _b64e(share)], body)
    return _finish_header(lines, file_key)


class Encryptor:
    """A write-only file object: plaintext in, an age file out to `raw`. Call close().
    Encrypts to a passphrase, or (with `recipients`, age1... strings or raw keys) to age
    public keys."""

    def __init__(self, raw: BinaryIO, passphrase: str | None = None, *, recipients: list[str | bytes] | None = None,
                 log_n: int = DEFAULT_LOG_N):
        if recipients:
            if passphrase:
                raise ValueError("pick a passphrase or recipients, not both (age allows no mix)")
            keys = [r if isinstance(r, bytes) else parse_recipient(r) for r in recipients]
        elif not passphrase:
            raise ValueError("an empty passphrase cannot protect a backup")
        self._raw = raw
        file_key = os.urandom(16)
        raw.write(make_recipients_header(keys, file_key) if recipients else
                  make_header(passphrase or "", file_key, log_n=log_n))
        nonce = os.urandom(16)
        raw.write(nonce)
        self._aead = ChaCha20Poly1305(_hkdf(file_key, nonce, b"payload"))
        self._buf = bytearray()
        self._counter = 0
        self.closed = False

    def write(self, data: bytes) -> int:
        if self.closed:
            raise ValueError("write to a closed archive")
        self._buf += data
        while len(self._buf) > CHUNK:  # hold the last chunk back until close()
            self._seal(bytes(self._buf[:CHUNK]), last=False)
            del self._buf[:CHUNK]
        return len(data)

    def _seal(self, chunk: bytes, *, last: bool) -> None:
        self._raw.write(self._aead.encrypt(_nonce(self._counter, last), chunk, None))
        self._counter += 1

    def flush(self) -> None:
        self._raw.flush()

    def close(self) -> None:
        if self.closed:
            return
        self._seal(bytes(self._buf), last=True)
        self._buf.clear()
        self._raw.flush()
        self.closed = True


def _read_line(raw: BinaryIO, budget: list[int]) -> bytes:
    line = bytearray()
    while True:
        ch = raw.read(1)
        if not ch:
            raise CorruptArchive("the archive header ends early")
        budget[0] -= 1
        if budget[0] < 0:
            raise CorruptArchive("the archive header is too large")
        if ch == b"\n":
            return bytes(line)
        line += ch


def read_header(raw: BinaryIO, passphrase: str | None = None, *, identities: list[bytes] | None = None,
                max_log_n: int = MAX_LOG_N) -> bytes:
    """Parse and authenticate the header; return the file key. Opens a passphrase (scrypt)
    file with `passphrase`, a recipients (X25519) file with one of `identities`."""
    budget = [MAX_HEADER]
    first = raw.read(len(INTRO) + 1)
    if first != INTRO + b"\n":
        raise NotAgeFile("this is not an age-encrypted openajar backup")
    budget[0] -= len(first)
    head = bytearray(first)
    stanzas: list[tuple[list[bytes], bytes]] = []
    line = _read_line(raw, budget)
    while line.startswith(b"-> "):
        head += line + b"\n"
        args = line[3:].split(b" ")
        body = b""
        while True:
            chunk = _read_line(raw, budget)
            head += chunk + b"\n"
            if len(chunk) > 64:
                raise CorruptArchive("the archive header is malformed")
            body += chunk
            if len(chunk) < 64:
                break
        stanzas.append((args, _b64d(body)))
        line = _read_line(raw, budget)
    if not line.startswith(b"--- "):
        raise CorruptArchive("the archive header is malformed")
    head += b"---"
    mac = _b64d(line[4:])
    scrypt = [s for s in stanzas if s[0] and s[0][0] == b"scrypt"]
    if not scrypt:
        if not any(s[0] and s[0][0] == b"X25519" for s in stanzas):
            raise WrongPassphrase("this archive is not passphrase-encrypted (no scrypt stanza)")
        file_key = _open_x25519(stanzas, identities or [])
        _check_mac(file_key, bytes(head), mac)
        return file_key
    if len(stanzas) != 1:
        raise CorruptArchive("a passphrase archive must have exactly one recipient")
    if passphrase is None:
        raise NoIdentity("this file is encrypted with a passphrase, not to an age key; give the passphrase")
    args, body = scrypt[0]
    if len(args) != 3:
        raise CorruptArchive("the archive header is malformed")
    salt = _b64d(args[1])
    log_n_text = args[2].decode("ascii", "replace")
    if len(salt) != 16 or not log_n_text.isdigit() or log_n_text.startswith("0"):
        raise CorruptArchive("the archive header is malformed")
    log_n = int(log_n_text)
    if not 1 <= log_n <= max_log_n:
        raise CorruptArchive(f"the archive asks for scrypt work factor 2^{log_n}, above the limit 2^{max_log_n}")
    if len(body) != 32:
        raise CorruptArchive("the archive header is malformed")
    try:
        file_key = ChaCha20Poly1305(_scrypt(passphrase, salt, log_n)).decrypt(b"\x00" * 12, body, None)
    except InvalidTag:
        raise WrongPassphrase("wrong passphrase") from None
    _check_mac(file_key, bytes(head), mac)
    return file_key


def _check_mac(file_key: bytes, head: bytes, mac: bytes) -> None:
    expected = hmac.new(_hkdf(file_key, b"", b"header"), head, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, mac):
        raise CorruptArchive("the archive header was altered")


def _open_x25519(stanzas: list[tuple[list[bytes], bytes]], identities: list[bytes]) -> bytes:
    if not identities:
        raise NoIdentity("this file is encrypted to an age key, not a passphrase; give the identity file "
                         "(AGE-SECRET-KEY-1...)")
    for args, body in stanzas:
        if not args or args[0] != b"X25519":
            continue  # another recipient type: not ours to open
        if len(args) != 2 or len(body) != 32:
            raise CorruptArchive("the archive header is malformed")
        share = _b64d(args[1])
        if len(share) != 32:
            raise CorruptArchive("the archive header is malformed")
        for identity in identities:
            secret = X25519PrivateKey.from_private_bytes(identity)
            try:
                shared = secret.exchange(X25519PublicKey.from_public_bytes(share))
            except ValueError:
                raise CorruptArchive("the archive header is malformed") from None
            key = _x25519_wrap_key(shared, share, secret.public_key().public_bytes_raw())
            try:
                return ChaCha20Poly1305(key).decrypt(b"\x00" * 12, body, None)
            except InvalidTag:
                continue
    raise NoIdentity("none of the given age identities opens this file")


class Decryptor:
    """A read-only file object over an age file: read() returns authenticated plaintext."""

    def __init__(self, raw: BinaryIO, passphrase: str | None = None, *, identities: list[bytes] | None = None,
                 max_log_n: int = MAX_LOG_N):
        self._raw = raw
        file_key = read_header(raw, passphrase, identities=identities, max_log_n=max_log_n)
        nonce = raw.read(16)
        if len(nonce) != 16:
            raise TruncatedArchive("the archive ends right after its header")
        self._aead = ChaCha20Poly1305(_hkdf(file_key, nonce, b"payload"))
        self._counter = 0
        self._buf = b""
        self._pending = raw.read(CHUNK + TAG)
        self._done = False

    def _next_chunk(self) -> bytes:
        sealed = self._pending
        self._pending = self._raw.read(CHUNK + TAG) if len(sealed) == CHUNK + TAG else b""
        last = not self._pending
        if len(sealed) < TAG:
            raise TruncatedArchive("the archive is truncated")
        try:
            plain = self._aead.decrypt(_nonce(self._counter, last), sealed, None)
        except InvalidTag:
            if last:
                try:  # a full non-final chunk at EOF: the stream was cut at a chunk boundary
                    self._aead.decrypt(_nonce(self._counter, False), sealed, None)
                except InvalidTag:
                    pass
                else:
                    raise TruncatedArchive("the archive is truncated (an interrupted backup or copy)") from None
                raise CorruptArchive("the archive is damaged or cut short (its last chunk failed "
                                     "authentication)") from None
            raise CorruptArchive("the archive is damaged (a chunk failed authentication)") from None
        if last and not plain and self._counter > 0:
            raise CorruptArchive("the archive ends with an empty chunk")
        self._counter += 1
        self._done = last
        return plain

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            parts = [self._buf]
            self._buf = b""
            while not self._done:
                parts.append(self._next_chunk())
            return b"".join(parts)
        while len(self._buf) < n and not self._done:
            self._buf += self._next_chunk()
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def close(self) -> None:
        pass
