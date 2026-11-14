"""Keys and agent grants in the controller.

Keys (`<controller home>/secrets`, mode 600, created on first use):
- `controller.key` signs `revoke`, `config_update`, `lane_control`, `mesh_freeze` and `standing`; nodes pin its public half;
- `grant-<org id>.key` is the org's grant key; nodes verify the grants it signs offline.

An agent attaches to the controller's MCP endpoint with an opaque bearer token (only its hash
is stored). Behind it is a grant definition: the authority block of an agentos.policy grant
(subject, workspaces, lanes, ceiling, specials, budget) with a stable grant id and revocation
id. For every `invoke` the controller mints a fresh token from that definition (token v2, see
src/agentos/policy/TOKENS.md: issuer `org:<org id>`, audience the target node, key id of the
org key), signed with the org key and expiring a few minutes after the call's deadline, so a
token seen on the wire is worth little, works only on the node it was minted for, and the
controller never stores a bearer credential a node would accept.
Revoking a grant (and every grant issued under it) is pushed to nodes as `revoke` with the
grant ids and revocation ids.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentos.core.errors import InvalidRequest, NotFound
from agentos.core.models import Effect
from agentos.node.channel import wire
from agentos.node.channel.identity import load_or_create_key
from agentos.policy import tokens
from agentos.policy.grants import Grant, GrantBlock, Specials
from agentos.policy.util import new_id

CALL_TOKEN_SLACK = 120          # seconds a per-call token outlives the call's deadline
MAX_GRANT_TTL = 365 * 86400
ALL_SPECIALS = Specials.everything().model_dump(mode="json", exclude_none=True)


class Keys:
    def __init__(self, secrets_dir: Path):
        self.dir = secrets_dir
        self.controller = load_or_create_key(secrets_dir / "controller.key")
        self._orgs: dict[str, Ed25519PrivateKey] = {}

    @property
    def controller_pubkey(self) -> str:
        return wire.public_b64(self.controller)

    def org(self, org_id: str) -> Ed25519PrivateKey:
        key = self._orgs.get(org_id)
        if key is None:
            key = self._orgs[org_id] = load_or_create_key(self.dir / f"grant-{org_id}.key")
        return key

    def grant_pubkey(self, org_id: str) -> str:
        return wire.public_b64(self.org(org_id))

    def sign(self, kind: str, node_id: str, body: dict) -> dict:
        return wire.sign_controller(self.controller, kind, node_id, body)


@dataclass
class AgentGrant:
    id: str
    org_id: str
    rid: str
    subject: dict
    block: dict            # the authority block, without `exp` (added per mint)
    expires_at: float | None
    revoked_at: float | None
    note: str | None = None

    @classmethod
    def from_row(cls, row: dict) -> AgentGrant:
        return cls(row["id"], row["org_id"], row["rid"], row["subject"], row["block"], row["expires_at"],
                   row["revoked_at"], row.get("note"))

    @property
    def live(self) -> bool:
        return self.revoked_at is None and (self.expires_at is None or self.expires_at > time.time())

    def policy_grant(self, now: float | None = None) -> Grant:
        """The same grant as a node will see it, for filtering the tool list."""
        exp = int((now or time.time()) + 3600)
        return Grant([GrantBlock.model_validate({**self.block, "exp": exp})])

    def public(self) -> dict:
        return {"id": self.id, "subject": self.subject, "workspaces": self.block.get("workspaces"),
                "lanes": self.block.get("lanes"), "ceiling": self.block.get("ceiling"),
                "specials": sorted((self.block.get("specials") or {}).keys()), "note": self.note,
                "expires_at": wire.iso(self.expires_at) if self.expires_at else None,
                "revoked": self.revoked_at is not None}


class AgentGrants:
    def __init__(self, store: Any, keys: Keys, *, clock=time.time):
        self.store = store
        self.keys = keys
        self.clock = clock

    def create(self, org_id: str, *, subject: str, workspaces: list[str] | None = None,
               lanes: list[str] | None = None, ceiling: str = "irreversible", specials: dict | str | None = "all",
               budget: dict | None = None, ttl_s: int | None = None, note: str | None = None,
               token: str | None = None, parent_id: str | None = None) -> tuple[AgentGrant, str]:
        """A new agent grant and its bearer token (shown once)."""
        now = self.clock()
        if ttl_s is not None and not 60 <= ttl_s <= MAX_GRANT_TTL:
            raise InvalidRequest("ttl must be between one minute and one year")
        if specials == "all":
            specials = ALL_SPECIALS
        if workspaces is None and lanes is None:
            workspaces = ["*"]
        block = {"v": 1, "id": new_id("gr"), "rid": new_id("rv"), "iat": int(now),
                 "sub": {"kind": "agent", "id": subject, "owner": "owner"}, "workspaces": workspaces,
                 "lanes": lanes, "ceiling": ceiling, "specials": specials or None, "budget": budget, "depth": 3,
                 "note": note}
        block = {k: v for k, v in block.items() if v is not None}
        try:
            GrantBlock.model_validate({**block, "exp": int(now) + 60})
        except ValueError as exc:
            raise InvalidRequest(f"invalid grant: {exc}") from None
        if Effect(ceiling).rank is None:
            raise InvalidRequest("the ceiling must be read, draft, commit, pay or irreversible")
        token = token or "aga_" + secrets.token_urlsafe(32)
        row = {"id": block["id"], "org_id": org_id, "rid": block["rid"], "parent_id": parent_id,
               "subject": block["sub"], "block": block, "token_hash": None, "note": note, "created_at": now,
               "expires_at": now + ttl_s if ttl_s else None}
        self.store.add_grant(row)
        self.store.set_grant_token(org_id, row["id"], token)
        return AgentGrant(row["id"], org_id, row["rid"], row["subject"], block, row["expires_at"], None, note), token

    def resolve(self, token: str | None) -> AgentGrant | None:
        if not token or len(token) > 200:
            return None
        row = self.store.grant_by_token(token)
        if row is None:
            return None
        grant = AgentGrant.from_row(row)
        return grant if grant.live else None

    def get(self, org_id: str, grant_id: str) -> AgentGrant:
        row = self.store.grant(org_id, grant_id)
        if row is None:
            raise NotFound(f"no grant {grant_id}")
        return AgentGrant.from_row(row)

    def list(self, org_id: str) -> list[AgentGrant]:
        return [AgentGrant.from_row(r) for r in self.store.grants(org_id)]

    def mint(self, grant: AgentGrant, *, deadline: float, thread: str | None = None,
             node_id: str | None = None) -> str:
        """A v2 token for one call on `node_id` (the whole org when None), valid until shortly
        after its deadline. Its revocation id comes from its signature, so revoking the grant
        sends the grant id (which nodes match too)."""
        exp = int(deadline + CALL_TOKEN_SLACK)
        if grant.expires_at is not None:
            exp = min(exp, int(grant.expires_at))
        key = self.keys.org(grant.org_id)
        block = {k: v for k, v in grant.block.items() if k != "rid"}
        block.update(v=2, exp=exp, iss=f"org:{grant.org_id}", kid=tokens.key_id(key),
                     aud=[f"node:{node_id}" if node_id else f"org:{grant.org_id}"])
        if thread:
            block["sub"] = {**block["sub"], "thread": thread[:200]}
        return tokens.mint(key, GrantBlock.model_validate(block).to_data())

    def revoke(self, org_id: str, grant_id: str) -> list[str]:
        """Revoke the grant and every grant issued under it; returns the revocation ids for nodes."""
        if self.store.grant(org_id, grant_id) is None:
            raise NotFound(f"no grant {grant_id}")
        rows = self.store.revoke_grant(org_id, grant_id)
        return [value for row in rows for value in (row["id"], row["rid"])]
