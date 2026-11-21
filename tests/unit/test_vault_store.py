"""Sealed store and crypto: encryption at rest, permissions, tampering, rotation, damage."""

import copy
import json
import os
import pickle
import sqlite3
import stat
import threading

import pytest
from test_vault_support import FAST_KDF, PASSWORD, SEED, USERNAME, private_home

from openajar.vault import crypto
from openajar.vault.errors import (
    EntryExists,
    EntryInvalid,
    EntryNotFound,
    VaultAlreadyInitialized,
    VaultCorrupt,
    VaultKeyInsecure,
    VaultKeyMissing,
    VaultNotInitialized,
    VaultTampered,
)
from openajar.vault.models import EntryKind, NewEntry, validate_new_entry
from openajar.vault.secret import Secret
from openajar.vault.service import keyfile_path, load_keyfile_master, pending_keyfile_path
from openajar.vault.store import VaultStore, WrongKey


@pytest.fixture
def home(tmp_path):
    return private_home(tmp_path)


@pytest.fixture
def store_key(home):
    key = os.urandom(32)
    crypto.write_keyfile(keyfile_path(home), key)
    store = VaultStore(home / "vault.db")
    store.initialize(mode="keyfile", master=key, kdf=None)
    return store, key


def new(name="ig", **kw):
    base = {"name": name, "kind": EntryKind.PASSWORD, "domain": "www.instagram.com", "lanes": ["biz-a/instagram"],
                "username": Secret(USERNAME), "secret": Secret(PASSWORD)}
    base.update(kw)
    return validate_new_entry(NewEntry(**base))


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


# ---- secret wrapper ------------------------------------------------------------

def test_secret_never_shows_itself():
    s = Secret(PASSWORD)
    for text in (repr(s), str(s), f"{s}", f"{s!r}", "%s" % s, json.dumps({"x": str(s)})):  # noqa: UP031
        assert PASSWORD not in text
    assert s.reveal() == PASSWORD and len(s) == len(PASSWORD) and s == Secret(PASSWORD)
    with pytest.raises(TypeError):
        pickle.dumps(s)
    assert copy.deepcopy(s) is s
    with pytest.raises(AttributeError):
        s._value = "x"  # type: ignore[misc]
    assert PASSWORD not in repr(new())


# ---- init and files ------------------------------------------------------------

def test_db_and_key_file_permissions(store_key, home):
    store, _ = store_key
    assert mode(store.path) == 0o600
    assert mode(keyfile_path(home)) == 0o600
    assert mode(keyfile_path(home).parent) == 0o700
    store.add(store_key[1], new())
    for suffix in ("-wal", "-shm"):
        p = store.path.with_name(store.path.name + suffix)
        if p.exists():
            assert mode(p) & 0o077 == 0, suffix


def test_secrets_are_encrypted_at_rest(store_key):
    store, key = store_key
    store.add(key, new(totp=Secret(SEED)))
    raw = store.path.read_bytes()
    for suffix in ("-wal",):
        p = store.path.with_name(store.path.name + suffix)
        if p.exists():
            raw += p.read_bytes()
    for needle in (PASSWORD, USERNAME, SEED):
        assert needle.encode() not in raw
    assert b"instagram.com" in raw       # metadata is plaintext (names, domains, lanes)


def test_initialize_twice_refuses(store_key):
    store, key = store_key
    with pytest.raises(VaultAlreadyInitialized):
        store.initialize(mode="keyfile", master=key, kdf=None)


def test_uninitialized_store(home):
    store = VaultStore(home / "vault.db")
    assert store.header() is None
    assert not store.path.exists()          # status checks never create the file
    with pytest.raises(VaultNotInitialized):
        store.list()


@pytest.mark.parametrize("perm", [0o644, 0o640, 0o604, 0o660])
def test_key_file_with_wide_permissions_is_refused(store_key, home, perm):
    store, _ = store_key
    os.chmod(keyfile_path(home), perm)
    with pytest.raises(VaultKeyInsecure) as exc:
        crypto.read_keyfile(keyfile_path(home))
    assert "chmod 600" in exc.value.message
    with pytest.raises(VaultKeyInsecure):
        load_keyfile_master(store, keyfile_path(home))


def test_key_dir_with_wide_permissions_is_refused(store_key, home):
    os.chmod(keyfile_path(home).parent, 0o755)
    with pytest.raises(VaultKeyInsecure):
        crypto.read_keyfile(keyfile_path(home))


def test_key_file_symlink_is_refused(store_key, home, tmp_path):
    kf = keyfile_path(home)
    real = kf.with_name("real.key")
    os.replace(kf, real)
    os.symlink(real, kf)
    with pytest.raises(VaultKeyInsecure):
        crypto.read_keyfile(kf)


def test_key_file_missing_or_malformed(store_key, home):
    kf = keyfile_path(home)
    kf.write_text("not base64 at all!!\n")
    os.chmod(kf, 0o600)
    with pytest.raises(VaultCorrupt):
        crypto.read_keyfile(kf)
    kf.unlink()
    with pytest.raises(VaultKeyMissing):
        crypto.read_keyfile(kf)


def test_wrong_key_does_not_open(store_key):
    store, _ = store_key
    with pytest.raises(WrongKey):
        store.check_master(os.urandom(32))
    with pytest.raises(WrongKey):
        store.open(os.urandom(32), "nope")


def test_passphrase_kdf_roundtrip_and_unicode(home):
    kdf = crypto.new_kdf(FAST_KDF)
    assert kdf["alg"] == "argon2id" and len(crypto.b64d(kdf["salt"])) == 16
    phrase = "Ünïcødé pässphrase 🔐"
    nfd = "Ünïcødé pässphrase 🔐".replace("Ü", "Ü")      # same text, decomposed
    assert crypto.derive_master(phrase, kdf) == crypto.derive_master(nfd, kdf)
    assert crypto.derive_master(phrase, kdf) != crypto.derive_master(phrase + " ", kdf)
    scrypt = crypto.new_kdf({"alg": "scrypt", "n": 2**10, "r": 8, "p": 1})
    assert len(crypto.derive_master("x" * 20, scrypt)) == 32


def test_tampered_kdf_parameters_are_refused():
    kdf = crypto.new_kdf(FAST_KDF)
    with pytest.raises(VaultCorrupt):
        crypto.derive_master("pw", {**kdf, "m": 10**9})
    with pytest.raises(VaultCorrupt):
        crypto.derive_master("pw", {**kdf, "alg": "md5"})


# ---- entries -------------------------------------------------------------------

def test_add_list_open_roundtrip(store_key):
    store, key = store_key
    meta = store.add(key, new(totp=Secret(SEED)))
    assert meta.domain == "instagram.com" and meta.has_username and meta.has_totp
    assert [e.name for e in store.list()] == ["ig"]
    got, values = store.open(key, "ig", ("username", "secret", "totp"))
    assert got.id == meta.id
    assert values["username"].reveal() == USERNAME and values["secret"].reveal() == PASSWORD
    assert values["totp"].reveal().startswith("otpauth://totp/")
    assert store.get(meta.id).name == "ig"
    with pytest.raises(EntryExists):
        store.add(key, new())
    with pytest.raises(EntryNotFound):
        store.get("missing")


def test_very_long_and_unicode_secrets(store_key):
    store, key = store_key
    long_pw = "Ω≈ç√∫˜µ≤≥÷" * 300 + "🔑" * 100      # well over a kilobyte, multibyte
    store.add(key, new("long", secret=Secret(long_pw[:1500])))
    assert store.open(key, "long", ("secret",))[1]["secret"].reveal() == long_pw[:1500]
    with pytest.raises(EntryInvalid) as exc:
        new("huge", secret=Secret("x" * 5000))
    assert "x" * 50 not in exc.value.message
    note = "line one\nline two\n" * 100
    store.add(key, new("n", kind=EntryKind.NOTE, username=None, secret=Secret(note), domain=None, lanes=[]))
    assert store.open(key, "n", ("secret",))[1]["secret"].reveal() == note


@pytest.mark.parametrize("kw,needle", [
    ({"domain": "com"}, "registrable"),
    ({"domain": "github.io"}, "registrable"),
    ({"domain": ""}, "domain"),
    ({"lanes": ["*"]}, "lane binding"),
    ({"lanes": ["Biz/X"]}, "lane binding"),
    ({"hosts": ["evil.io"]}, "not under"),
    ({"secret": Secret("")}, "empty"),
    ({"secret": Secret("a\nb")}, "single line"),
    ({"secret": Secret("a\x00b")}, "NUL"),
    ({"secret": Secret("pass\tword")}, "tabs"),
    ({"secret": Secret("pass\x1bword")}, "control characters"),
    ({"secret": Secret("pass\x85word")}, "control characters"),
    ({"username": Secret("me\t@x.com")}, "tabs"),
    ({"name": ""}, "name"),
    ({"name": "x\x07"}, "control"),
    ({"totp": Secret("not-base32!")}, "base32"),
])
def test_validation_errors_are_clear_and_secret_free(kw, needle):
    with pytest.raises(EntryInvalid) as exc:
        new(**kw)
    assert needle in exc.value.message
    assert PASSWORD not in exc.value.message and USERNAME not in exc.value.message


def test_a_note_keeps_tabs_and_line_breaks_but_no_other_control_characters():
    assert new(kind="note", username=None, secret=Secret("a\tb\r\nc")).secret.reveal() == "a\tb\r\nc"
    with pytest.raises(EntryInvalid) as exc:
        new(kind="note", username=None, secret=Secret("a\x1bb"))
    assert "other than tabs and line breaks" in exc.value.message


def test_metadata_tampering_is_detected(store_key):
    store, key = store_key
    store.add(key, new())
    with sqlite3.connect(store.path) as c:
        c.execute("UPDATE entries SET domain = 'evil.io', lanes = '[\"evil/*\"]'")
    with pytest.raises(VaultTampered) as exc:
        store.open(key, "ig", ("secret",))
    assert PASSWORD not in exc.value.message
    assert store.list()[0].domain == "evil.io"     # listing still works; resolution refuses
    with pytest.raises(VaultTampered):
        store.update(key, "ig", lanes=["evil/*"])


def test_ciphertext_cannot_be_moved_between_entries(store_key):
    store, key = store_key
    store.add(key, new("a"))
    store.add(key, new("b", domain="reddit.com", secret=Secret("other-secret")))
    with sqlite3.connect(store.path) as c:
        blob = c.execute("SELECT secret FROM entries WHERE name='a'").fetchone()[0]
        c.execute("UPDATE entries SET secret = ? WHERE name = 'b'", (blob,))
    with pytest.raises(VaultTampered):
        store.open(key, "b", ("secret",))


def test_update_lanes_and_remove(store_key):
    store, key = store_key
    store.add(key, new())
    meta = store.update(key, "ig", lanes=["biz-a/*", "personal/instagram"])
    assert meta.lanes == ["biz-a/*", "personal/instagram"]
    assert store.open(key, "ig", ())[0].lanes == meta.lanes      # MAC was recomputed
    store.remove("ig")
    assert store.list() == []


def test_add_many_is_all_or_nothing(store_key):
    store, key = store_key
    store.add(key, new("dup"))
    with pytest.raises(EntryExists):
        store.add_many(key, [new("fresh"), new("dup")], on_conflict="error")
    assert [e.name for e in store.list()] == ["dup"]
    r = store.add_many(key, [new("fresh"), new("dup")], on_conflict="rename")
    assert r["added"] == 2 and r["renamed"] == 1
    assert sorted(e.name for e in store.list()) == ["dup", "dup (2)", "fresh"]
    r = store.add_many(key, [new("dup")], on_conflict="skip")
    assert r["skipped"] == 1 and r["added"] == 0


def test_record_use_concurrently(store_key):
    store, key = store_key
    meta = store.add(key, new())
    threads = [threading.Thread(target=store.record_use, args=(meta.id,)) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    got = store.get("ig")
    assert got.use_count == 20 and got.last_used is not None


# ---- rotation ------------------------------------------------------------------

def test_rotate_data_key_reseals_everything(store_key):
    store, key = store_key
    store.add(key, new("a", totp=Secret(SEED)))
    store.add(key, new("b", domain="reddit.com"))
    with sqlite3.connect(store.path) as c:
        before = c.execute("SELECT secret FROM entries WHERE name='a'").fetchone()[0]
    assert store.rotate_data_key(key) == 2
    assert store.header().key_version == 2
    _, vals = store.open(key, "a", ("username", "secret", "totp"))
    assert vals["secret"].reveal() == PASSWORD and vals["totp"] is not None
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT secret FROM entries WHERE name='a'").fetchone()[0] != before


def test_rotation_refuses_and_changes_nothing_if_an_entry_is_tampered(store_key):
    store, key = store_key
    store.add(key, new("a"))
    store.add(key, new("b", domain="reddit.com"))
    with sqlite3.connect(store.path) as c:
        c.execute("UPDATE entries SET lanes='[\"x/*\"]' WHERE name='b'")
    with pytest.raises(VaultTampered):
        store.rotate_data_key(key)
    assert store.header().key_version == 1
    assert store.open(key, "a", ("secret",))[1]["secret"].reveal() == PASSWORD


def test_rewrap_to_passphrase_and_back(store_key):
    store, key = store_key
    store.add(key, new())
    kdf = crypto.new_kdf(FAST_KDF)
    pmaster = crypto.derive_master("new passphrase here", kdf)
    store.rewrap(key, pmaster, mode="passphrase", kdf=kdf)
    assert store.header().mode == "passphrase"
    with pytest.raises(WrongKey):
        store.check_master(key)
    assert store.open(pmaster, "ig", ("secret",))[1]["secret"].reveal() == PASSWORD
    with pytest.raises(WrongKey):
        store.rewrap(key, key, mode="keyfile", kdf=None)


def test_interrupted_key_file_rotation_recovers(store_key, home):
    """Crash after the vault was rewrapped under vault.key.new but before the rename."""
    store, key = store_key
    store.add(key, new())
    new_key = os.urandom(32)
    pending = pending_keyfile_path(keyfile_path(home))
    crypto.write_keyfile(pending, new_key)
    store.rewrap(key, new_key, mode="keyfile", kdf=None)
    # (crash here: vault.key still holds the old key)
    assert load_keyfile_master(store, keyfile_path(home)) == new_key
    assert not pending.exists()
    assert crypto.read_keyfile(keyfile_path(home)) == new_key


def test_stale_pending_key_file_is_ignored(store_key, home):
    """Crash after writing vault.key.new but before the rewrap: the old key still opens."""
    store, key = store_key
    crypto.write_keyfile(pending_keyfile_path(keyfile_path(home)), os.urandom(32))
    assert load_keyfile_master(store, keyfile_path(home)) == key


# ---- damage --------------------------------------------------------------------

def test_garbage_db_is_reported_as_corrupt(home):
    path = home / "vault.db"
    path.write_bytes(os.urandom(8192))
    store = VaultStore(path)
    with pytest.raises(VaultCorrupt) as exc:
        store.header()
    assert "restore it from a backup" in exc.value.message


def test_truncated_db_is_reported_as_corrupt(store_key):
    store, key = store_key
    for i in range(30):
        store.add(key, new(f"entry {i}", secret=Secret("p" * 2000)))
    with sqlite3.connect(store.path) as c:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    data = store.path.read_bytes()
    for suffix in ("-wal", "-shm"):
        store.path.with_name(store.path.name + suffix).unlink(missing_ok=True)
    store.path.write_bytes(data[: len(data) // 2])
    with pytest.raises(VaultCorrupt):
        store.quick_check()
        store.list()
        store.open(key, "entry 29", ("secret",))


def test_foreign_sqlite_file_is_not_a_vault(home):
    path = home / "vault.db"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE other(x)")
    store = VaultStore(path)
    assert store.header() is None
    with pytest.raises(VaultNotInitialized):
        store.list()


def test_newer_schema_is_refused(store_key):
    store, _ = store_key
    with sqlite3.connect(store.path) as c:
        c.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    with pytest.raises(VaultCorrupt) as exc:
        store.header()
    assert "newer openajar" in exc.value.message


def test_crash_midway_through_rotation_changes_nothing(store_key, monkeypatch):
    store, key = store_key
    for i in range(5):
        store.add(key, new(f"e{i}", domain=f"site{i}.com"))
    real_seal, calls = crypto.seal, []

    def flaky_seal(k, plaintext, aad):
        calls.append(1)
        if len(calls) == 7:
            raise OSError("power cut")
        return real_seal(k, plaintext, aad)

    monkeypatch.setattr(crypto, "seal", flaky_seal)
    with pytest.raises(OSError):
        store.rotate_data_key(key)
    monkeypatch.undo()
    assert store.header().key_version == 1
    for i in range(5):
        assert store.open(key, f"e{i}", ("secret",))[1]["secret"].reveal() == PASSWORD


def test_a_held_write_lock_gives_vault_busy_not_a_hang(store_key):
    from openajar.vault.errors import VaultBusy

    store, key = store_key
    holder = sqlite3.connect(store.path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        quick = VaultStore(store.path, timeout=0.2)
        with pytest.raises(VaultBusy):
            quick.add(key, new())
        assert [e.name for e in quick.list()] == []      # readers are not blocked (WAL)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    store.add(key, new())
