"""People and sign-in on a node: the calls web/src/api/client.ts makes for passkeys, sessions, my details,
the organisation, members, invites, notifications, phone push and Getting started.

Each route is called the way the app calls it, and each refusal is checked for its status and words.
A passkey needs a real authenticator, so the ceremonies are checked up to the point where a fake one
would be refused, and the stored passkeys are made directly in the node's store.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from soft_authenticator import SoftAuthenticator
from starlette.testclient import TestClient

from openajar.api import create_login_code
from openajar.api.members import OWNER_ID, Role

ANA = "m_ana0001"
ORIGIN = "http://localhost:7999"  # the node fixture's port: a passkey's origin


def store_of(client: TestClient) -> Any:
    return client.app.state.api


def add_passkey(client: TestClient, label: str | None = None) -> str:
    api = store_of(client)
    cred = api.members.store.add_credential(
        member_id=OWNER_ID,
        cred_id=b"\x01" * 16,
        public_key=b"k",
        sign_count=0,
        label=label,
        aaguid=b"\x00" * 16,
    )
    return cred.id


def add_member(client: TestClient) -> None:
    """A member with a role in one workspace, as an accepted invite leaves them."""
    api = store_of(client)
    api.members.store.create_member(
        member_id=ANA,
        handle="ana",
        display_name="Ana",
        email="ana@example.com",
        timezone="UTC",
        by="owner",
        roles=[Role(scope="biz-a", role="operator", options={}, expires_at=None)],
        member_ttl_s=None,
    )
    api.members.sync(ANA, by="owner", force=True)


# ---- passkeys ----------------------------------------------------------------------------------


def test_passkeys_are_listed_and_removed_one_at_a_time(client: TestClient) -> None:
    assert client.get("/api/auth/passkeys").json() == []
    kept = add_passkey(client)
    assert client.get("/api/auth/passkeys").json()[0]["id"] == kept

    [item] = client.get("/api/auth/passkeys").json()
    assert set(item) == {"id", "name", "created_at", "last_used_at"}
    assert item["name"] == "Passkey"

    refused = client.delete(f"/api/auth/passkeys/{kept}")
    assert (refused.status_code, refused.json()["error"]["code"]) == (403, "step_up_required")
    store = client.app.state.api.store
    store.verified(store.get_session(client.cookies["agentos_session"]))
    assert client.delete(f"/api/auth/passkeys/{kept}").status_code == 204
    assert client.get("/api/auth/passkeys").json() == []
    assert client.delete(f"/api/auth/passkeys/{kept}").status_code == 404


def test_a_passkey_keeps_the_name_it_was_given(client: TestClient) -> None:
    add_passkey(client, label="Work laptop")
    assert client.get("/api/auth/passkeys").json()[0]["name"] == "Work laptop"


def test_a_new_passkey_starts_with_options_and_is_refused_without_a_real_one(client: TestClient) -> None:
    options = client.post("/api/auth/passkey/register/options")
    assert options.status_code == 200
    assert "challenge" in options.json()

    refused = client.post("/api/auth/passkey/register/verify", json={"id": "not-made-here"})
    assert refused.status_code == 401


def test_a_step_up_with_no_passkey_says_to_add_one(client: TestClient) -> None:
    # No passkey yet: the node has nothing to check, and says so. The app shows how to add one.
    options = client.post("/api/auth/step-up/passkey/options")
    assert options.status_code == 409
    assert "Add a passkey first" in options.json()["error"]["message"]


def test_a_step_up_with_a_passkey_that_is_not_real_is_refused(client: TestClient) -> None:
    add_passkey(client)
    assert "challenge" in client.post("/api/auth/step-up/passkey/options").json()
    refused = client.post("/api/auth/step-up/passkey/verify", json={"id": "not-made-here"})
    assert refused.status_code == 401


def test_a_step_up_by_email_is_refused_on_a_node(client: TestClient) -> None:
    for path, body in (
        ("/api/auth/step-up/magic-link", None),
        ("/api/auth/step-up/verify", {"token": "x" * 40}),
    ):
        reply = client.post(path, json=body) if body is not None else client.post(path)
        assert reply.status_code == 503, path
        assert "cannot send email" in reply.json()["error"]["message"]


def test_a_passkey_is_made_on_this_device_and_listed(client: TestClient) -> None:
    auth = SoftAuthenticator(ORIGIN)
    options = client.post("/api/auth/passkey/register/options")
    assert options.status_code == 200
    saved = client.post("/api/auth/passkey/register/verify", json=auth.create(options.json()))
    assert saved.status_code == 200, saved.text
    assert saved.json()["name"] == "Passkey" and saved.json()["last_used_at"] is None
    assert [item["id"] for item in client.get("/api/auth/passkeys").json()] == [saved.json()["id"]]
    assert client.get("/api/session").json()["user"]["has_passkey"] is True


def test_a_second_passkey_waits_for_a_check_with_the_first(client: TestClient) -> None:
    auth = SoftAuthenticator(ORIGIN)
    first = client.post(
        "/api/auth/passkey/register/verify",
        json=auth.create(client.post("/api/auth/passkey/register/options").json()),
    )
    assert first.status_code == 200

    # Adding the first passkey was itself a check, so the window has to lapse before a second one is refused.
    settings = store_of(client).settings
    window, settings.step_up_window = settings.step_up_window, 0
    refused = client.post("/api/auth/passkey/register/options")
    settings.step_up_window = window
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "step_up_required"

    options = client.post("/api/auth/step-up/passkey/options").json()
    checked = client.post("/api/auth/step-up/passkey/verify", json=auth.get(options))
    assert checked.status_code == 204, checked.text
    assert client.get("/api/session").json()["step_up_until"] is not None

    second = client.post(
        "/api/auth/passkey/register/verify",
        json=auth.create(client.post("/api/auth/passkey/register/options").json()),
    )
    assert second.status_code == 200, second.text
    assert len(client.get("/api/auth/passkeys").json()) == 2


def test_an_invite_is_joined_with_a_passkey_made_on_the_invitees_device(client: TestClient) -> None:
    made = make_invite(client, workspaces=["biz-a"])
    invitee = TestClient(client.app, base_url="http://localhost")
    assert invitee.get(f"/api/invites/{made['token']}").json()["join_by"] == "passkey"

    auth = SoftAuthenticator(ORIGIN)
    options = invitee.post(f"/api/invites/{made['token']}/passkey/options")
    assert options.status_code == 200 and "challenge" in options.json()
    joined = invitee.post(
        f"/api/invites/{made['token']}/passkey",
        json={"credential": auth.create(options.json()), "timezone": "Europe/Vilnius"},
    )
    assert joined.status_code == 200, joined.text
    session = joined.json()
    assert session["orgs"][0]["slug"] == "local" and session["orgs"][0]["role"] == "member"
    assert session["user"]["has_passkey"] is True and session["csrf"]
    assert invitee.get("/api/session").json()["user"]["id"] != OWNER_ID

    assert invitee.get(f"/api/invites/{made['token']}").status_code == 401
    [ana] = [
        m for m in client.get("/api/orgs/local/members").json() if m["user"]["email"] == "ana@example.com"
    ]
    assert ana["role"] == "member" and ana["workspaces"] == ["biz-a"]


# ---- sign-in by email or Google: none on a node ----------------------------------------------


def test_email_sign_in_asks_for_the_nodes_own_sign_in_first(client: TestClient) -> None:
    # Signed out, a node asks for its own sign-in, and the sign-in page has no email form at all.
    client.cookies.clear()
    assert client.post("/api/auth/magic-link", json={"email": "ana@example.com"}).status_code == 401


def test_email_links_are_refused_with_the_reason(client: TestClient) -> None:
    assert client.post("/api/auth/magic-link", json={"email": "ana@example.com"}).status_code == 503
    checked = client.post("/api/auth/magic-link/verify", json={"token": "x" * 40})
    assert checked.status_code == 503
    assert "cannot send email" in checked.json()["error"]["message"]


def test_google_sign_in_sends_the_person_back_to_sign_in_with_the_reason(client: TestClient) -> None:
    google = client.get("/api/auth/google/start", follow_redirects=False)
    assert google.status_code == 303
    assert google.headers["location"] == "/sign-in?error=google_unavailable"


# ---- sessions and my details -----------------------------------------------------------------


def test_my_sessions_say_how_each_one_signed_in(client: TestClient) -> None:
    [only] = client.get("/api/sessions").json()
    assert only["current"] is True
    assert only["label"] == "Signed in with a code"
    assert only["ip"] is None and only["location"] is None
    assert len(only["id"]) == 16 and only["last_active_at"]


def test_a_session_can_be_ended_from_another(client: TestClient, tmp_path: Path) -> None:
    link = create_login_code(tmp_path, base_url="http://localhost")
    other = TestClient(client.app, base_url="http://localhost")
    assert other.post("/api/session", json={"code": link.url.split("code=", 1)[1]}).status_code == 200

    listed = client.get("/api/sessions").json()
    assert len(listed) == 2
    elsewhere = next(item for item in listed if not item["current"])
    assert client.delete(f"/api/sessions/{elsewhere['id']}").status_code == 204
    assert other.get("/api/session").status_code == 401
    assert client.delete("/api/sessions/not-a-session").status_code == 404


def test_my_name_is_saved_and_shown_everywhere(client: TestClient) -> None:
    saved = client.patch("/api/me", json={"name": "  Jane  "})
    assert saved.status_code == 200
    assert saved.json()["name"] == "Jane"
    assert client.get("/api/session").json()["user"]["name"] == "Jane"


def test_a_node_does_not_keep_an_avatar_colour(client: TestClient) -> None:
    refused = client.patch("/api/me", json={"avatar_color": "#ff0000"})
    assert refused.status_code == 400
    assert client.patch("/api/me", json={"name": "   "}).status_code == 400


# ---- the organisation -------------------------------------------------------------------------


def test_the_node_is_one_organisation_the_app_can_name(client: TestClient) -> None:
    org = client.get("/api/orgs/local").json()
    assert (org["id"], org["slug"], org["plan"], org["goal"]) == ("local", "local", "self_hosted", None)
    assert org["name"] == store_of(client).node_name
    assert org["created_at"].endswith("Z") or "T" in org["created_at"]

    renamed = client.patch("/api/orgs/local", json={"name": "Corner shop"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "Corner shop"
    assert client.get("/api/session").json()["orgs"][0]["name"] == "Corner shop"


def test_the_first_job_is_kept_on_the_node(client: TestClient) -> None:
    saved = client.patch("/api/orgs/local", json={"goal": "post", "goal_note": "Every Tuesday"})
    assert saved.status_code == 200
    assert (saved.json()["goal"], saved.json()["goal_note"]) == ("post", "Every Tuesday")
    assert client.patch("/api/orgs/local", json={"goal": "nonsense"}).status_code == 400


def test_the_address_and_the_organisation_itself_stay(client: TestClient) -> None:
    assert client.patch("/api/orgs/local", json={"slug": "acme"}).status_code == 400
    gone = client.request("DELETE", "/api/orgs/local", json={"confirm": "local"})
    assert gone.status_code == 403
    assert "cannot be deleted from the app" in gone.json()["error"]["message"]
    made = client.post("/api/orgs", json={"name": "Acme"})
    assert made.status_code == 409


def test_getting_started_is_worked_out_from_the_node(client: TestClient) -> None:
    setup = client.get("/api/orgs/local/getting-started").json()
    keys = [item["key"] for item in setup["items"]]
    # No phone step: this computer has no phone push, so it cannot be done here and is not asked for.
    assert keys == ["goal", "computer", "account", "ai", "approval", "invite"]
    assert client.get("/api/orgs/local/push/config").json()["available"] is False
    done = {item["key"]: item["done"] for item in setup["items"]}
    assert done["computer"] and done["account"]
    assert not done["goal"]
    assert setup["goal"] is None and setup["complete"] is False
    before = setup["done"]

    client.patch("/api/orgs/local", json={"goal": "forms"})
    after = client.get("/api/orgs/local/getting-started").json()
    assert after["goal"] == {"key": "forms", "title": "Fill in forms on websites"}
    assert after["done"] == before + 1


# ---- members ----------------------------------------------------------------------------------


def test_members_are_shown_with_the_app_roles(client: TestClient) -> None:
    add_member(client)
    members = {item["id"]: item for item in client.get("/api/orgs/local/members").json()}
    assert members[OWNER_ID]["role"] == "owner" and members[OWNER_ID]["workspaces"] is None
    ana = members[ANA]
    assert ana["role"] == "member"
    assert ana["workspaces"] == ["biz-a"]
    assert ana["user"] == {"id": ANA, "email": "ana@example.com", "name": "Ana"}
    assert ana["last_active_at"] is None


def test_a_members_role_and_businesses_change_together_or_apart(client: TestClient) -> None:
    add_member(client)
    moved = client.patch(
        f"/api/orgs/local/members/{ANA}", json={"role": "viewer", "workspaces": ["biz-a", "biz-b"]}
    )
    assert moved.status_code == 200
    assert moved.json()["role"] == "viewer" and moved.json()["workspaces"] == ["biz-a", "biz-b"]

    kept = client.patch(f"/api/orgs/local/members/{ANA}", json={"workspaces": ["biz-b"]})
    assert kept.json()["role"] == "viewer" and kept.json()["workspaces"] == ["biz-b"]


def test_a_person_who_differs_by_business_changes_one_business_at_a_time(client: TestClient) -> None:
    add_member(client)  # operator in biz-a
    r = client.patch(f"/api/orgs/local/members/{ANA}", json={"workspaces": ["biz-a", "biz-b"]})
    assert r.status_code == 200
    assert r.json()["roles_by_workspace"] == [{"workspace": "biz-a", "role": "member"}, {"workspace": "biz-b", "role": "viewer"}]
    r = client.patch(f"/api/orgs/local/members/{ANA}", json={"role": "approver", "workspace": "biz-b"})
    assert r.status_code == 200, r.text
    by = {item["workspace"]: item["role"] for item in r.json()["roles_by_workspace"]}
    assert by == {"biz-a": "member", "biz-b": "approver"}  # biz-a kept its operator role
    # a business that is newly given does not take the highest role the person holds elsewhere
    r = client.patch(f"/api/orgs/local/members/{ANA}", json={"workspaces": ["biz-a", "biz-b", "biz-c"]})
    assert {item["workspace"]: item["role"] for item in r.json()["roles_by_workspace"]}["biz-c"] == "viewer"
    assert client.patch(f"/api/orgs/local/members/{ANA}", json={"workspace": "biz-b"}).status_code == 400


def test_an_auditor_keeps_the_auditor_label(client: TestClient) -> None:
    add_member(client)
    r = client.patch(f"/api/orgs/local/members/{ANA}", json={"role": "auditor", "workspaces": ["biz-a"]})
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "auditor" and r.json()["roles_by_workspace"] == [{"workspace": "biz-a", "role": "auditor"}]


def test_the_owner_is_not_given_away_or_changed(client: TestClient) -> None:
    add_member(client)
    assert client.patch(f"/api/orgs/local/members/{ANA}", json={"role": "owner"}).status_code == 400
    refused = client.patch(f"/api/orgs/local/members/{OWNER_ID}", json={"role": "viewer"})
    assert refused.status_code == 400


def test_a_member_can_be_removed(client: TestClient) -> None:
    add_member(client)
    assert client.delete(f"/api/orgs/local/members/{ANA}").status_code == 200
    assert [item["id"] for item in client.get("/api/orgs/local/members").json()] == [OWNER_ID]


# ---- invites ----------------------------------------------------------------------------------


def make_invite(
    client: TestClient, email: str = "ana@example.com", workspaces: list[str] | None = None
) -> dict:
    reply = client.post(
        "/api/orgs/local/invites", json={"email": email, "role": "member", "workspaces": workspaces}
    )
    assert reply.status_code == 200, reply.text
    return reply.json()


def test_an_invite_is_made_with_a_link_that_is_shown_once(client: TestClient) -> None:
    made = make_invite(client, workspaces=["biz-a"])
    assert made["state"] == "pending" and made["role"] == "member" and made["workspaces"] == ["biz-a"]
    assert made["email"] == "ana@example.com" and made["invited_by"]
    assert made["token"]

    listed = client.get("/api/orgs/local/invites").json()
    assert [item["id"] for item in listed] == [made["id"]]
    assert "token" not in listed[0]


def test_two_invites_to_one_address_get_different_handles(client: TestClient) -> None:
    first = make_invite(client)
    second = make_invite(client)
    handles = store_of(client).members.store
    assert handles.invite(first["id"]).handle == "ana"
    assert handles.invite(second["id"]).handle == "ana-2"


def test_an_invite_cannot_give_the_owner_role(client: TestClient) -> None:
    refused = client.post(
        "/api/orgs/local/invites", json={"email": "x@example.com", "role": "owner", "workspaces": None}
    )
    assert refused.status_code == 400


def test_an_invite_is_shown_to_the_person_it_is_for(client: TestClient) -> None:
    made = make_invite(client, workspaces=["biz-a"])
    client.cookies.clear()
    preview = client.get(f"/api/invites/{made['token']}").json()
    assert preview["join_by"] == "passkey"
    assert preview["role"] == "member"
    assert preview["workspaces"] == ["Biz A"] and preview["business"] == "Biz A"
    assert preview["org_name"] == store_of(client).node_name
    assert preview["inviter"] and preview["expires_at"]

    unknown = client.get("/api/invites/not-a-real-invite-code")
    assert unknown.status_code == 401


def test_an_invite_is_accepted_with_a_passkey_and_not_by_the_other_routes(client: TestClient) -> None:
    made = make_invite(client)
    options = client.post(f"/api/invites/{made['token']}/passkey/options")
    assert options.status_code == 200 and "challenge" in options.json()

    refused = client.post(
        f"/api/invites/{made['token']}/passkey", json={"credential": {"id": "not-made-here"}}
    )
    assert refused.status_code == 401

    # Signed in, the other ways to accept say so; a stranger is simply not signed in.
    for path in (f"/api/invites/{made['token']}/join", f"/api/invites/{made['token']}/accept"):
        reply = client.post(path, json={"aup_accepted": True})
        assert reply.status_code == 503, path
        assert "accepted with a passkey" in reply.json()["error"]["message"]
    client.cookies.clear()
    assert client.post(f"/api/invites/{made['token']}/accept").status_code == 401


def test_a_revoked_invite_leaves_the_list(client: TestClient) -> None:
    made = make_invite(client)
    assert client.delete(f"/api/orgs/local/invites/{made['id']}").status_code == 200
    assert client.get("/api/orgs/local/invites").json() == []


# ---- notifications ----------------------------------------------------------------------------


def test_my_notifications_keep_their_timing_and_email_stays_off(client: TestClient) -> None:
    settings = client.get("/api/orgs/local/settings/notifications").json()
    assert settings["email_digest"] == "off" and settings["email_urgent"] is False
    assert settings["push"] is False
    assert set(settings["quiet_hours"]) == {"enabled", "from", "to", "timezone", "urgent_breaks_through"}

    saved = client.put(
        "/api/orgs/local/settings/notifications",
        json={
            **settings,
            "timezone": "Europe/Vilnius",
            "batch_minutes": 10,
            "quiet_hours": {
                "enabled": True,
                "from": "22:00",
                "to": "07:00",
                "timezone": "Europe/Vilnius",
                "urgent_breaks_through": True,
            },
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["batch_minutes"] == 10
    assert saved.json()["quiet_hours"]["from"] == "22:00" and saved.json()["quiet_hours"]["enabled"] is True
    assert saved.json()["timezone"] == "Europe/Vilnius"


def test_a_node_cannot_turn_email_or_phone_push_on(client: TestClient) -> None:
    settings = client.get("/api/orgs/local/settings/notifications").json()
    email = client.put("/api/orgs/local/settings/notifications", json={**settings, "email_digest": "daily"})
    assert email.status_code == 400
    assert "cannot send email" in email.json()["error"]["message"]
    push = client.put("/api/orgs/local/settings/notifications", json={**settings, "push": True})
    assert push.status_code == 400


def test_the_test_notification_says_when_no_channel_answered(client: TestClient) -> None:
    node_says = client.post("/api/settings/notifications/test").json()["ok"]
    sent = client.post("/api/orgs/local/settings/notifications/test")
    assert sent.status_code == (204 if node_says else 409)


# ---- phone push: not set up on a node -------------------------------------------------------


def test_phone_push_is_not_set_up_here(client: TestClient) -> None:
    config = client.get("/api/orgs/local/push/config").json()
    assert config == {"available": False, "reason": "not_configured", "public_key": None}
    assert client.get("/api/orgs/local/push/devices").json() == []
    assert (
        client.post(
            "/api/orgs/local/push/devices", json={"endpoint": "https://push.example", "keys": {}}
        ).status_code
        == 503
    )
    assert client.request("DELETE", "/api/orgs/local/push/devices/x").status_code == 503
    assert client.post("/api/orgs/local/push/test").status_code == 503
