"""Policy errors. They subclass the core errors, so the MCP layer maps them to stable codes.

Every denial is a `Denied` (code "denied") carrying a stable machine `reason`. The message
always starts with the reason, so a tool error reads "denied: <reason>: <detail>".

Reasons (stable, documented in ROUTING.md):
  unauthenticated  invalid_token  unknown_issuer  unknown_key  retired_key  audience
  unregistered  expired  not_yet_valid  revoked
  out_of_scope  domain  ceiling  special  provider  tier  budget  depth  fanout
  pins  unknown_effect  routing  approval_denied  too_many_pending  admin_required
  self_approval  approve_in_panel  not_approver  four_eyes  step_up_required  taint_cooldown
  controller_ceiling  (a controller asked for more than the node's owner allows it, policy/ceiling.py)
  floor  owner_only  (a change to the confirmation setting, policy/confirmation.py)
  limit_reached  (a meter's daily or hourly limit, policy/meters.py: LimitReached carries `hit`)
  (step_up_required is raised by the panel API, Brief B2: a fresh passkey check is needed)

Two refusals of a decision have codes of their own, so a panel can act on them (docs/CONFIRMATIONS.md):
`proof_too_weak` (an approve below what the approval requires) and `method_unavailable`.
"""

from __future__ import annotations

from openajar.core.errors import OpenajarError, PolicyDenied


class Denied(PolicyDenied):
    """A policy denial with a stable machine reason."""

    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class GrantFrozen(Denied):
    """A grant a freeze revoked (openajar.policy.freeze). Code `frozen`, so the agent reads what
    happened and stops, instead of a sign-in hint that sends it looking for another token."""
    code = "frozen"

    def __init__(self, detail: str):
        PolicyDenied.__init__(self, detail)
        self.reason = "frozen"
        self.detail = detail


class ProofTooWeak(Denied):
    """An approve below what the approval requires. `requires` goes in the answer, so the panel can
    step up. A `Denied`, so a batch reports it for its one approval and goes on."""
    code = "proof_too_weak"

    def __init__(self, requires: str, detail: str, approval_id: str | None = None):
        super().__init__("proof_too_weak", detail)
        self.requires = requires
        self.approval_id = approval_id  # the check the panel asks for is for this very approval


class MethodUnavailable(Denied):
    """A way to confirm that is planned, unknown or turned off on this node."""
    code = "method_unavailable"

    def __init__(self, detail: str):
        super().__init__("method_unavailable", detail)


class TokenInvalid(Denied):
    """The bearer token is malformed, truncated, tampered with or signed by another key."""

    def __init__(self, detail: str = "the token is not valid"):
        super().__init__("invalid_token", detail)


from openajar.core.errors import Conflict  # noqa: F401  (shared; re-exported for policy code)


class PolicyKeyError(OpenajarError):
    """The policy root key is missing, unreadable or has unsafe permissions."""

    code = "policy_key"


class SeeWholeCard(Denied):
    """An approve of a send card whose preview shows only part of the messages, by someone who was
    not served every page of them (ApprovalStore.send_messages). A `Denied`, so a batch reports it
    for its one approval and goes on."""
    code = "see_whole_card"

    def __init__(self, detail: str, approval_id: str | None = None):
        super().__init__("see_whole_card", detail)
        self.approval_id = approval_id
