"""Crown-jewel credentials: vault first, a marked 600 dev fallback otherwise, never shown."""

import json
import stat

from agentos.brains.credentials import DEV_FILE_NAME, CredentialStore
from agentos.brains.testing import OAUTH, FakeVault


async def test_dev_fallback_is_marked_and_private(tmp_path):
    store = CredentialStore(tmp_path)
    assert store.backend == "dev-file"
    await store.put("brains/x/credential", OAUTH, kind="oauth_token", label="x")
    path = tmp_path / "secrets" / DEV_FILE_NAME
    assert "DEV-ONLY" in path.name
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    data = json.loads(path.read_text())
    assert "NOT ENCRYPTED" in data["_warning"]
    assert await store.get("brains/x/credential") == OAUTH
    assert await store.has("brains/x/credential")
    assert OAUTH not in repr(store)
    await store.delete("brains/x/credential")
    assert not path.exists()


async def test_vault_used_when_present(tmp_path):
    vault = FakeVault()
    store = CredentialStore(tmp_path, vault)
    assert store.backend == "vault"
    await store.put("brains/x/credential", OAUTH, kind="oauth_token", label="x")
    assert vault.data["brains/x/credential"] == OAUTH
    assert not (tmp_path / "secrets" / DEV_FILE_NAME).exists()
    assert await store.get("brains/x/credential") == OAUTH


async def test_incompatible_vault_falls_back_loudly(tmp_path):
    class Odd:
        def store(self, *a):
            pass
    store = CredentialStore(tmp_path, Odd())
    assert store.backend == "dev-file" and store.vault_incompatible


async def test_dev_entries_migrate_into_the_vault(tmp_path):
    dev = CredentialStore(tmp_path)
    await dev.put("brains/a/credential", OAUTH, kind="oauth_token", label="a")
    vault = FakeVault()
    store = CredentialStore(tmp_path, vault)
    assert await store.migrate_dev_entries() == 1
    assert vault.data["brains/a/credential"] == OAUTH
    assert not (tmp_path / "secrets" / DEV_FILE_NAME).exists()
