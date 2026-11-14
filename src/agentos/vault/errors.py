"""Vault errors with stable codes.

Every message is built from metadata only (entry names, domains, lanes, file paths),
never from a username, secret, seed, one-time code or passphrase. Subclasses extend the
shared classes in agentos.core.errors so the MCP and API layers map them to the right
HTTP status without knowing about the vault.
"""

from __future__ import annotations

from agentos.core.errors import (
    AgentosError,
    InvalidRequest,
    NotFound,
    PolicyDenied,
    ServiceUnavailable,
)


class VaultError(AgentosError):
    code = "vault_error"


class VaultNotInitialized(ServiceUnavailable):
    code = "vault_not_initialized"


class VaultSealed(ServiceUnavailable):
    """The vault holds no key in memory. Unseal it with `agentos vault unseal`."""
    code = "vault_sealed"


class VaultAlreadyInitialized(VaultError):
    code = "vault_exists"


class WrongPassphrase(PolicyDenied):
    code = "vault_wrong_passphrase"


class VaultLockedOut(PolicyDenied):
    """Too many wrong passphrases in a row; unseal is refused for a while."""
    code = "vault_locked_out"


class VaultKeyInsecure(VaultError):
    """The key file or its directory is readable by others, a symlink, or not ours."""
    code = "vault_key_insecure"


class VaultKeyMissing(VaultError):
    code = "vault_key_missing"


class VaultCorrupt(VaultError):
    """vault.db is unreadable, truncated, or not a vault."""
    code = "vault_corrupt"


class VaultTampered(VaultCorrupt):
    """An entry's binding metadata no longer matches its authentication tag."""
    code = "vault_tampered"


class VaultBusy(VaultError):
    code = "vault_busy"


class EntryNotFound(NotFound):
    code = "vault_entry_not_found"


class EntryExists(VaultError):
    code = "vault_entry_exists"


class EntryInvalid(InvalidRequest):
    code = "vault_entry_invalid"


class ResolutionDenied(PolicyDenied):
    """The No-Body-Injection rule refused to resolve a secret into this sink.

    `reason` is one of the stable codes in agentos.vault.policy.Reason.
    """
    code = "vault_denied"

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.reason = reason


class TypistAborted(VaultError):
    """The page changed under the typist (focus moved, frame navigated); typing stopped."""
    code = "vault_typing_aborted"


class CatcherError(VaultError):
    code = "vault_code_catcher"


class CodeDeclined(VaultError):
    """The owner dismissed the code card: no code is coming for this login step."""
    code = "vault_code_declined"


class CodeRequestClosed(NotFound):
    """The code request was used, declined or expired, or never existed. A code is taken once."""
    code = "vault_code_request_closed"


class CodeRefused(PolicyDenied):
    """The caller may not type a code for this request (the requester's own grant family)."""
    code = "vault_code_refused"


class VaultWrongKey(VaultError):
    """The key file exists and is private, but it does not open this vault."""
    code = "vault_wrong_key"


class SecretTooLong(VaultError):
    """The secret is too long to type into a page at human rhythm."""
    code = "vault_secret_too_long"


class LoginClosed(NotFound):
    """No assisted login is running on the lane (it ended, was cancelled, or never started)."""
    code = "login_closed"


class LoginStepChanged(AgentosError):
    """The sign-in page is not the step the form showed, so nothing was typed or pressed."""
    code = "login_step_changed"

