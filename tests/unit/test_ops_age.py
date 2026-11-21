"""The age v1 passphrase format: round trips, tampering, truncation, header rules."""

from __future__ import annotations

import io
import os

import pytest

from openajar.core import age

PW = "correct horse battery staple"
LOG_N = 10  # fast in tests; real archives use 18


def encrypt(data: bytes, passphrase: str = PW) -> bytes:
    raw = io.BytesIO()
    enc = age.Encryptor(raw, passphrase, log_n=LOG_N)
    for i in range(0, len(data), 10_000):
        enc.write(data[i:i + 10_000])
    enc.close()
    return raw.getvalue()


def decrypt(blob: bytes, passphrase: str = PW, chunk: int = 7_777) -> bytes:
    dec = age.Decryptor(io.BytesIO(blob), passphrase)
    out = bytearray()
    while piece := dec.read(chunk):
        out += piece
    return bytes(out)


@pytest.mark.parametrize("size", [0, 1, age.CHUNK - 1, age.CHUNK, age.CHUNK + 1, 3 * age.CHUNK, 3 * age.CHUNK + 5])
def test_round_trip(size):
    data = os.urandom(size)
    blob = encrypt(data)
    assert decrypt(blob) == data
    assert age.Decryptor(io.BytesIO(blob), PW).read() == data


def test_header_shape_is_age_v1():
    blob = encrypt(b"hello")
    lines = blob.split(b"\n")
    assert lines[0] == b"age-encryption.org/v1"
    stanza = lines[1].split(b" ")
    assert stanza[:2] == [b"->", b"scrypt"] and stanza[3] == str(LOG_N).encode()
    assert len(lines[2]) == 43  # 32-byte wrapped key, unpadded base64
    assert lines[3].startswith(b"--- ")
    # payload: 16-byte nonce + one sealed chunk of 5 bytes
    header_len = len(b"\n".join(lines[:4])) + 1
    assert len(blob) - header_len == 16 + 5 + age.TAG


def test_wrong_passphrase():
    with pytest.raises(age.WrongPassphrase):
        age.Decryptor(io.BytesIO(encrypt(b"x")), "not the passphrase")


def test_payload_tamper_detected():
    blob = bytearray(encrypt(os.urandom(2 * age.CHUNK + 10)))
    blob[-age.CHUNK] ^= 0x01
    with pytest.raises(age.CorruptArchive):
        decrypt(bytes(blob))


def test_header_tamper_detected():
    blob = encrypt(b"data")
    lines = blob.split(b"\n", 4)
    lines[1] = lines[1].replace(str(LOG_N).encode(), str(LOG_N + 1).encode())
    with pytest.raises((age.CorruptArchive, age.WrongPassphrase)):
        age.Decryptor(io.BytesIO(b"\n".join(lines)), PW)
    mac_line = blob.split(b"\n")[3]
    swapped = blob.replace(mac_line, mac_line[:-2] + (b"AA" if mac_line[-2:] != b"AA" else b"BB"))
    with pytest.raises(age.CorruptArchive):
        age.Decryptor(io.BytesIO(swapped), PW)


def test_truncation_at_a_chunk_boundary_is_named():
    data = os.urandom(3 * age.CHUNK + 100)
    blob = encrypt(data)
    header_len = blob.index(b"\n---") + 1
    header_len = blob.index(b"\n", header_len) + 1
    cut = header_len + 16 + 2 * (age.CHUNK + age.TAG)  # drop the last chunk exactly
    with pytest.raises(age.TruncatedArchive):
        decrypt(blob[:cut])


def test_truncation_mid_chunk_is_corrupt():
    blob = encrypt(os.urandom(2 * age.CHUNK))
    with pytest.raises(age.CorruptArchive):
        decrypt(blob[:-1000])


def test_trailing_data_after_last_chunk():
    blob = encrypt(b"short")
    with pytest.raises(age.CorruptArchive):
        decrypt(blob + b"x" * 40)


def test_not_an_age_file_and_work_factor_cap():
    with pytest.raises(age.NotAgeFile):
        age.Decryptor(io.BytesIO(b"PK\x03\x04 zip file"), PW)
    raw = io.BytesIO()
    age.Encryptor(raw, PW, log_n=LOG_N).close()
    blob = raw.getvalue().replace(f" {LOG_N}\n".encode(), b" 30\n", 1)
    with pytest.raises(age.CorruptArchive, match="work factor"):
        age.Decryptor(io.BytesIO(blob), PW)


def test_empty_passphrase_refused():
    with pytest.raises(ValueError):
        age.Encryptor(io.BytesIO(), "")


def test_unicode_passphrase_is_normalized():
    composed, decomposed = "café passphrase", "café passphrase"
    assert decrypt(encrypt(b"x", composed), decomposed) == b"x"


# ---- X25519 recipients (age1... / AGE-SECRET-KEY-1...) ------------------------------------
# Checked against rage (pyrage) both ways when this was written: rage opens what we write and we
# open what rage writes, for X25519 and scrypt.

def encrypt_to(data: bytes, *recipients: str) -> bytes:
    raw = io.BytesIO()
    enc = age.Encryptor(raw, recipients=list(recipients))
    enc.write(data)
    enc.close()
    return raw.getvalue()


def test_recipient_round_trip_and_header_shape():
    identity, recipient = age.generate_identity()
    assert recipient.startswith("age1") and len(recipient) == 62
    assert identity.startswith("AGE-SECRET-KEY-1") and identity == identity.upper()
    assert age.recipient_of(age.parse_identity(identity)) == recipient
    data = os.urandom(2 * age.CHUNK + 3)
    blob = encrypt_to(data, recipient)
    lines = blob.split(b"\n")
    assert lines[1].startswith(b"-> X25519 ") and len(lines[2]) == 43 and lines[3].startswith(b"--- ")
    assert age.Decryptor(io.BytesIO(blob), identities=[age.parse_identity(identity)]).read() == data


def test_any_of_several_recipients_opens_it():
    (id1, r1), (id2, r2) = age.generate_identity(), age.generate_identity()
    blob = encrypt_to(b"shared", r1, r2)
    for identity in (id1, id2):
        assert age.Decryptor(io.BytesIO(blob), identities=age.read_identities(f"# key\n{identity}\n")).read() == b"shared"


def test_wrong_identity_or_wrong_kind_of_secret():
    _, recipient = age.generate_identity()
    other, _ = age.generate_identity()
    blob = encrypt_to(b"x", recipient)
    with pytest.raises(age.NoIdentity, match="none of the given"):
        age.Decryptor(io.BytesIO(blob), identities=[age.parse_identity(other)])
    with pytest.raises(age.NoIdentity, match="age key"):
        age.Decryptor(io.BytesIO(blob), PW)
    with pytest.raises(age.NoIdentity, match="passphrase"):
        age.Decryptor(io.BytesIO(encrypt(b"x")), identities=[age.parse_identity(other)])
    assert issubclass(age.NoIdentity, age.WrongPassphrase)


def test_recipient_header_tamper_is_detected():
    identity, recipient = age.generate_identity()
    blob = encrypt_to(b"data", recipient)
    mac_line = blob.split(b"\n")[3]
    swapped = blob.replace(mac_line, mac_line[:-2] + (b"AA" if mac_line[-2:] != b"AA" else b"BB"))
    with pytest.raises(age.CorruptArchive):
        age.Decryptor(io.BytesIO(swapped), identities=[age.parse_identity(identity)])


def test_bad_keys_are_refused():
    identity, recipient = age.generate_identity()
    for bad in (recipient[:-1] + ("q" if recipient[-1] != "q" else "p"), "age1", "not a key", recipient.upper()[:10]):
        with pytest.raises(age.BadKey):
            age.parse_recipient(bad)
    with pytest.raises(age.BadKey):
        age.parse_recipient(identity)  # an identity is not a recipient
    with pytest.raises(age.BadKey):
        age.read_identities("# only a comment\n")
    with pytest.raises(ValueError):
        age.Encryptor(io.BytesIO(), PW, recipients=[recipient])
