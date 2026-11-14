"""Machine secrets (a brain's subscription token, a task's child grant): sealed like every vault
entry, purpose recorded on every read, never listed with entries, never in a log or an error."""

import logging
import sqlite3

import pytest
from test_vault_support import make_vault, private_home

from agentos.brains.credentials import DEV_FILE_NAME, CredentialStore, brain_ref
from agentos.core.errors import InvalidRequest
from agentos.vault.errors import VaultSealed, VaultTampered

TOKEN = "sk-ant-oat01-" + "Q" * 70


@pytest.fixture
def vault(tmp_path):
    return make_vault(private_home(tmp_path))


async def test_put_get_delete_and_purpose(vault, caplog):
    caplog.set_level(logging.DEBUG)
    meta = await vault.put_secret("brains/claude-max/credential", TOKEN, kind="oauth_token",
                                  label="brain claude-max", scope={"brain": "claude-max", "node": True})
    assert meta["ref"] == "brains/claude-max/credential" and "value" not in meta and TOKEN not in str(meta)
    assert await vault.has_secret("brains/claude-max/credential")
    assert await vault.get_secret("brains/claude-max/credential", purpose="brain claude-max attempt 1") == TOKEN
    [listed] = await vault.list_secrets()
    assert listed["use_count"] == 1 and listed["last_purpose"] == "brain claude-max attempt 1"
    assert listed["kind"] == "oauth_token" and TOKEN not in str(listed)
    assert await vault.list_entries() == []  # never mixed into the logins the panel lists
    assert vault.status()["machine_secrets"] == 1
    await vault.put_secret("brains/claude-max/credential", TOKEN + "2", kind="oauth_token", label="again")
    assert await vault.get_secret("brains/claude-max/credential", purpose="check") == TOKEN + "2"
    await vault.delete_secret("brains/claude-max/credential")
    assert await vault.get_secret("brains/claude-max/credential", purpose="check") is None
    assert not await vault.has_secret("brains/claude-max/credential")
    await vault.delete_secret("brains/never-there")  # deleting nothing is fine
    assert TOKEN not in caplog.text


async def test_sealed_at_rest_and_tamper_evident(vault):
    await vault.put_secret("brains/x/credential", TOKEN, kind="oauth_token", label="x")
    raw = vault.store.path.read_bytes()
    for wal in vault.store.path.parent.glob("vault.db-wal"):
        raw += wal.read_bytes()
    assert TOKEN.encode() not in raw and b"QQQQQQQQ" not in raw
    db = sqlite3.connect(vault.store.path)
    db.execute("UPDATE machine_secrets SET scope = '{\"brain\": \"other\"}'")  # rebind without the key
    db.commit()
    db.close()
    with pytest.raises(VaultTampered):
        await vault.get_secret("brains/x/credential", purpose="check")


async def test_a_sealed_vault_neither_stores_nor_reveals(tmp_path):
    vault = make_vault(private_home(tmp_path), mode="passphrase")
    with pytest.raises(VaultSealed):
        await vault.put_secret("brains/x/credential", TOKEN, kind="oauth_token", label="x")
    await vault.unseal("a long test passphrase")
    await vault.put_secret("brains/x/credential", TOKEN, kind="oauth_token", label="x")
    vault.seal()
    assert await vault.has_secret("brains/x/credential")  # presence needs no key
    with pytest.raises(VaultSealed) as exc:
        await vault.get_secret("brains/x/credential", purpose="check")
    assert TOKEN not in str(exc.value)


async def test_key_rotation_keeps_machine_secrets(vault):
    await vault.put_secret("brains/x/credential", TOKEN, kind="oauth_token", label="x")
    await vault.add_entry(name="ig", kind="password", domain="instagram.com", secret="hunter2-long-enough")
    master = vault._require_master()
    assert vault.store.rotate_data_key(master) == 2
    assert await vault.get_secret("brains/x/credential", purpose="after rotation") == TOKEN


@pytest.mark.parametrize("ref", ["", "../x", "a/../b", "/abs", "x" * 300, "has space", "new\nline", 7])
async def test_refs_are_validated(vault, ref):
    with pytest.raises(InvalidRequest):
        await vault.put_secret(ref, TOKEN, kind="oauth_token", label="x")


async def test_inputs_are_validated_without_echoing_the_value(vault):
    for kwargs in ({"kind": "Not A Kind"}, {"scope": {"nested": {"no": 1}}}, {"scope": {"k": "v" * 500}}):
        with pytest.raises(InvalidRequest) as exc:
            await vault.put_secret("brains/x/credential", TOKEN, **{"kind": "oauth_token", "label": "x", **kwargs})
        assert TOKEN not in exc.value.message
    with pytest.raises(InvalidRequest):
        await vault.put_secret("brains/x/credential", "", kind="oauth_token", label="x")
    with pytest.raises(InvalidRequest):
        await vault.get_secret("brains/x/credential", purpose="  ")


async def test_brains_credentials_use_the_real_vault(vault, tmp_path):
    store = CredentialStore(tmp_path, vault)
    assert store.backend == "vault"
    await store.put(brain_ref("claude-max"), TOKEN, kind="oauth_token", label="brain claude-max",
                    scope={"brain": "claude-max"})
    assert await store.has(brain_ref("claude-max"))
    assert await store.get(brain_ref("claude-max"), purpose="brain") == TOKEN
    assert not (tmp_path / "secrets" / DEV_FILE_NAME).exists()
    [listed] = await vault.list_secrets()
    assert listed["use_count"] == 1  # has() is a presence check, not a read
    await store.delete(brain_ref("claude-max"))
    assert not await store.has(brain_ref("claude-max"))


async def test_scrub_masks_machine_secrets_without_recording_a_read(vault):
    await vault.put_secret("brains/x/credential", TOKEN, kind="oauth_token", label="x")
    assert vault.scrub(f"log line with {TOKEN} in it") == "log line with [redacted] in it"
    assert vault.scrub(f"lane scoped {TOKEN}", lane_id="biz-a/x").endswith(TOKEN)  # lane scrubs cover entries only
    (row,) = await vault.list_secrets()
    assert row["use_count"] == 0 and row["last_purpose"] is None
