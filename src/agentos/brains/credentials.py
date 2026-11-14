"""Brain credentials: crown jewels.

An OAuth token from `claude setup-token`, a Codex ChatGPT login (`auth.json`), or an API
key gives full use of the owner's AI account. They are:

- stored through the runtime `vault` service when it is configured; otherwise in a
  DEVELOPMENT FALLBACK file (`$AGENTOS_HOME/secrets/brains-credentials.DEV-ONLY.json`,
  chmod 600, not encrypted) that is clearly marked and migrated into the vault as soon as
  one appears;
- resolved only at the moment a brain process starts, and injected only into that
  process's environment (or a chmod 600 auth file inside the task's private home);
- never logged, traced, returned by a tool or the API, or put in an error message.

Subscriptions are bring-your-own for the owner's own use on the owner's node. agentos never
pools one subscription across tenants or people.

Vault contract this module uses (agentos.vault.service.VaultService; sync or async both work):
    put_secret(ref, value, *, kind, label, scope) -> None
    get_secret(ref, *, purpose) -> str | None
    delete_secret(ref) -> None
    has_secret(ref) -> bool          (optional: a presence check that needs no unseal)

`agentos brains add|auth` refuse to put a credential in the dev fallback file unless
`--insecure-dev-store` is given (see agentos.brains.cli).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from agentos.core.aio import maybe_await

DEV_FILE_NAME = "brains-credentials.DEV-ONLY.json"
DEV_WARNING = ("DEVELOPMENT FALLBACK, NOT ENCRYPTED. agentos stores brain credentials here only "
               "because no vault service is configured. Configure the vault; entries move there "
               "automatically on the next start.")




def brain_ref(name: str) -> str:
    return f"brains/{name}/credential"


def child_ref(task_id: str) -> str:
    return f"brains/tasks/{task_id}/child-token"


DEFAULT_CHILD_REF = "brains/default-child-token"


class CredentialStore:
    def __init__(self, home: Path, vault: Any = None):
        self.home = home
        self.vault = vault if self._vault_ok(vault) else None
        self.vault_incompatible = vault is not None and self.vault is None
        self.dev_path = home / "secrets" / DEV_FILE_NAME

    def __repr__(self) -> str:  # never show values
        return f"CredentialStore(backend={self.backend!r})"

    @staticmethod
    def _vault_ok(vault: Any) -> bool:
        return vault is not None and all(callable(getattr(vault, m, None))
                                         for m in ("put_secret", "get_secret", "delete_secret"))

    @property
    def backend(self) -> str:
        return "vault" if self.vault is not None else "dev-file"

    # ---- dev fallback file ----
    def _dev_load(self) -> dict[str, Any]:
        if not self.dev_path.exists():
            return {}
        try:
            return json.loads(self.dev_path.read_text()).get("entries", {})
        except (ValueError, OSError):
            return {}

    def _dev_save(self, entries: dict[str, Any]) -> None:
        self.dev_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dev_path.parent, 0o700)
        if not entries:
            self.dev_path.unlink(missing_ok=True)
            return
        tmp = self.dev_path.with_name(self.dev_path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"_warning": DEV_WARNING, "entries": entries}, fh, indent=2)
        os.replace(tmp, self.dev_path)
        os.chmod(self.dev_path, 0o600)

    # ---- API ----
    async def put(self, ref: str, value: str, *, kind: str, label: str, scope: dict[str, Any] | None = None):
        if not value:
            raise ValueError("empty credential")
        if self.vault is not None:
            await maybe_await(self.vault.put_secret(ref, value, kind=kind, label=label, scope=scope or {}))
            return
        entries = self._dev_load()
        entries[ref] = {"kind": kind, "label": label, "value": value, "stored_at": time.time()}
        self._dev_save(entries)

    async def get(self, ref: str, *, purpose: str = "brain") -> str | None:
        if self.vault is not None:
            return await maybe_await(self.vault.get_secret(ref, purpose=purpose))
        entry = self._dev_load().get(ref)
        return entry.get("value") if entry else None

    async def has(self, ref: str) -> bool:
        if self.vault is not None:
            if callable(getattr(self.vault, "has_secret", None)):
                return bool(await maybe_await(self.vault.has_secret(ref)))  # no unseal needed
            return bool(await self.get(ref, purpose="presence-check"))
        return ref in self._dev_load()

    async def delete(self, ref: str) -> None:
        if self.vault is not None:
            await maybe_await(self.vault.delete_secret(ref))
            return
        entries = self._dev_load()
        if entries.pop(ref, None) is not None:
            self._dev_save(entries)

    async def migrate_dev_entries(self) -> int:
        """Move dev-file entries into the vault once one is configured; delete the file."""
        if self.vault is None or not self.dev_path.exists():
            return 0
        entries = self._dev_load()
        for ref, entry in entries.items():
            await maybe_await(self.vault.put_secret(ref, entry["value"], kind=entry.get("kind", "secret"),
                                                     label=entry.get("label", ref), scope={}))
        self.dev_path.unlink(missing_ok=True)
        return len(entries)
