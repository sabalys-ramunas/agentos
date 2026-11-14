"""QA bug hunt on Teams B2 (members, invites, passkey sessions, role-based routes, step-up).

Each xfail pins one defect found in the recent merges. The
harness is the one in test_api_members.py: a real policy and panel, a software authenticator.
"""

from __future__ import annotations

from soft_authenticator import SoftAuthenticator
from test_api_members import ORIGIN, join, node, owner, pending, role

__all__ = ["node"]  # the node fixture, imported for the tests below


def test_qa_teams_1_an_empty_role_list_without_scopes_does_not_silently_keep_the_roles(node):
    boss = owner(node)
    va = join(node, boss, "va", [role("approver")])
    r = boss.req("PATCH", f"/api/members/{va.member['id']}", {"roles": []})
    assert r.status_code in (400, 422) or r.json()["roles"] == [], r.text


def test_qa_teams_2_adding_a_passkey_needs_a_recent_passkey_check(node):
    boss = owner(node)
    va = join(node, boss, "va", [role("approver")])
    settings = node.api_app.state.api.settings
    opts = va.post("/api/me/passkeys/options")  # just signed in with a passkey: allowed
    assert opts.status_code == 200, opts.text
    settings.step_up_window = 0  # now the last check is older than any window
    refused = va.post("/api/me/passkeys/options")
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "step_up_required", refused.text
    attacker_key = SoftAuthenticator(ORIGIN)  # a finder's own authenticator, not the member's
    r = va.post("/api/me/passkeys", {"credential": attacker_key.create(opts.json()["publicKey"])})
    assert r.status_code == 403, r.text
    assert len(node.api_app.state.api.members.store.credentials(va.member["id"])) == 1


def test_qa_teams_3_a_viewer_is_refused_by_the_route_for_a_batch_approve(node):
    boss = owner(node)
    viewer = join(node, boss, "vi", [role("viewer")])
    ap = pending(node)
    r = viewer.post("/api/approvals/batch", {"ids": [ap], "decision": "approve"})
    assert r.status_code == 403, r.text
