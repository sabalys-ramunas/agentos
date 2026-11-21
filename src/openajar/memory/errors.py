"""Memory refusals with stable codes (docs/design/workspace-memory.md section 4.6).

Every class is an OpenajarError, so the MCP layer turns it into `<code>: <message>` without
knowing this package. Messages never quote a secret value or the body of an entry the caller
may not read.
"""

from __future__ import annotations

from typing import Any

from openajar.core.errors import Conflict, InvalidRequest, NotFound, OpenajarError, PolicyDenied


class MemoryRefused(OpenajarError):
    """Base for the refusals listed in the design. Nothing was written."""
    code = "memory_refused"


class SecretInMemory(MemoryRefused):
    """A scrubber changed the title, body, attributes or reason: something in it looks like a
    secret. `kinds` names what was found (never the value)."""
    code = "secret_in_memory"

    def __init__(self, kinds: list[str]):
        self.kinds = sorted(set(kinds))
        super().__init__(
            f"the entry looks like it holds a secret ({', '.join(self.kinds)}). Memory never stores "
            "secrets: keep them in the vault and mention the vault entry by name instead")


class BadEvidence(MemoryRefused):
    """A cited trace does not exist, belongs to another workspace, or is not visible to the
    writer's grant family."""
    code = "bad_evidence"


class MemoryConflict(Conflict):
    """The key exists, or `supersedes` named a version that is no longer current. `current` is
    `{id, version, trust, title}` of the entry as it is now (title omitted when the caller may
    not read the entry)."""

    def __init__(self, message: str, current: dict[str, Any] | None = None):
        self.current = current
        if current:
            message = f"{message} (current: {current['id']}@{current['version']})"
        super().__init__(message)


class TooLarge(MemoryRefused):
    code = "too_large"


class RateLimited(MemoryRefused):
    code = "rate_limited"


class TrustTooLow(MemoryRefused):
    """A lower trust version tried to supersede a higher trust entry."""
    code = "trust_too_low"

    def __init__(self, mine: str, theirs: str):
        super().__init__(
            f"a {mine} write cannot replace a {theirs} entry. Write a new note that links to it "
            "with contests=[id] instead; the owner sees it and decides")


class TypeNotAllowed(MemoryRefused):
    code = "type_not_allowed"


class PrivacyRefused(MemoryRefused):
    code = "privacy"


class OutOfScope(PolicyDenied):
    """The caller's grant has no memory for this workspace (or not this kind of entry)."""
    code = "out_of_scope"


class EntryNotFound(NotFound):
    """No such entry in any workspace the caller can read (an entry the caller may not see is
    reported the same way, so its existence does not leak)."""


class OwnerTrustRequired(MemoryRefused):
    """The write needs owner trust: a decision, or a new version of an owner trust entry, from
    someone who is not the owner in person. The tools layer authorizes `draft` with
    `facts={"memory_owner_trust": True}` and `content=canonical`, then calls `write` again with
    the approval on the caller. `canonical` is what the approval must bind; `summary` is the
    node-written line the owner reads."""
    code = "owner_trust_required"

    def __init__(self, canonical: dict[str, Any], summary: str, digest: str):
        self.canonical = canonical
        self.summary = summary
        self.digest = digest
        super().__init__(f"this write needs the owner's approval: {summary}")


__all__ = [
    "BadEvidence",
    "EntryNotFound",
    "InvalidRequest",
    "MemoryConflict",
    "MemoryRefused",
    "OutOfScope",
    "OwnerTrustRequired",
    "PrivacyRefused",
    "RateLimited",
    "SecretInMemory",
    "TooLarge",
    "TrustTooLow",
    "TypeNotAllowed",
]
