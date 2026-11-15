"""Connecting a private repository from the chat (agentos.environments.connect): environment_open gives
the agent a card, the owner opens the node's own token page from it (agentos.environments.form) and
pastes a token there, the node checks the token with GitHub (mocked here) and with git, keeps it, and
approves the card. The token is in no tool result, API answer, error, card, approval, trace, log line
or file other than the sealed vault, and no API route takes one."""

from __future__ import annotations

import base64
import json
import logging
import sqlite3
from urllib.parse import quote

import httpx
import pytest
from test_environments import TITLE, node  # noqa: F401  (node is a fixture)
from test_vault_support import make_vault

from agentos.api import ApiSettings, build_api
from agentos.api.auth import PanelStore
from agentos.core.models import Principal
from agentos.environments import connect
from agentos.git import keys as keymod
from agentos.git.credentials import git_credentials, put_token
from agentos.git.service import GitService
from agentos.vault.secret import Secret

REPO = "github.com/acme/private-app"
TOKEN = "github_pat_11AAAAAAA0Secret0Token0For0Tests0Only0zz"  # what a person pastes
OLD = "ghp_" + "an0old0revoked0token0of0the0vault000"  # built, so the secret scan sees no token
ORIGIN = "http://localhost:7400"
EXPIRES = "2026-12-31 23:59:59 UTC"


def basic(token: str) -> str:
    return base64.b64encode(f"x-access-token:{token}".encode()).decode()


class Git:
    """`git ls-remote` as the node runs it: it reads the repository when the Authorization header in the
    process's own config carries `good`, and never otherwise."""

    def __init__(self, good: str | None = TOKEN):
        self.good = good
        self.seen: list[str] = []

    async def __call__(self, url, env, *, timeout_s=20, allow_file=False):
        self.seen.append(url)
        headers = [v for k, v in env.items() if k.startswith("GIT_CONFIG_VALUE") and v.startswith("Authorization")]
        if self.good is not None and headers == [f"Authorization: Basic {basic(self.good)}"]:
            return True, b""
        return False, b"fatal: could not read Username for 'https://github.com': terminal prompts disabled\n"


def github(status: int = 200, *, headers: dict | None = None, body: dict | None = None, seen: list | None = None):
    """GitHub's API for GET /repos/acme/private-app, with the token's expiry on a 200."""

    def answer(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        assert request.url.path == "/repos/acme/private-app" and request.url.host == "api.github.com"
        if status == 200 and request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"message": "Bad credentials"})
        extra = {"github-authentication-token-expiration": EXPIRES} if status == 200 else {}
        return httpx.Response(status, headers={**extra, **(headers or {})},
                              json=body if body is not None else {"full_name": "acme/private-app", "private": True})

    return httpx.MockTransport(answer)


@pytest.fixture
async def panel(node, monkeypatch):  # noqa: F811
    """The environments node with a vault, the git gateway, and the panel API on it."""
    home = node["mgr"].home
    home.chmod(0o700)
    vault = make_vault(home)
    node["rt"].register("vault", vault)
    git = GitService(node["rt"], home, host_port=0)
    node["rt"].register("git", git)
    node["mgr"].repo_probe = node["git"] = Git()
    monkeypatch.setattr(connect, "transport", github())
    node["vault"] = vault
    node["app"] = build_api(node["rt"], ApiSettings(lane_pump_seconds=0, panel_dir=home / "no-panel"))
    node["panel_store"] = PanelStore.for_home(home)
    yield node
    await git.stop()


async def signed_in(node, client: httpx.AsyncClient) -> dict:  # noqa: F811
    node["panel_store"].add_code("one-time-code-for-tests")
    r = await client.post("/api/session", json={"code": "one-time-code-for-tests"})
    assert r.status_code == 200, r.text
    return {"X-Agentos-CSRF": r.json()["csrf"], "Origin": ORIGIN}


def client(node) -> httpx.AsyncClient:  # noqa: F811
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=node["app"]), base_url=ORIGIN)


async def card(node) -> dict:  # noqa: F811
    node["git"].good = None  # the node cannot read it yet
    out = await node["call"]("environment_open", title=TITLE, repo=REPO)
    node["git"].good = TOKEN
    assert out["status"] == "approval_pending" and out["kind"] == "repo_connect", out
    return out


async def token_page(c: httpx.AsyncClient, h: dict, approval_id: str) -> dict:
    """The owner's press on the card: the link to the node's token page, and the page itself."""
    r = await c.post(f"/api/repo-connect/{approval_id}/form", headers=h)
    assert r.status_code == 200, r.text
    trip = r.json()
    assert trip["kind"] == "repo_token" and trip["stage"] == "waiting" and trip["form_url"].startswith("/api/forms/")
    page = await c.get(trip["form_url"])
    assert page.status_code == 200 and 'name="token" type="password"' in page.text, page.text
    return trip


async def paste(c: httpx.AsyncClient, trip: dict, token: str) -> httpx.Response:
    """What the page's form sends: urlencoded, from the page's own origin, with no session or CSRF header."""
    return await c.post(trip["form_url"], content=f"token={quote(token)}".encode(),
                        headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": ORIGIN})


def dump(home) -> str:
    out = []
    for path in sorted(home.rglob("*")):
        if path.is_file():
            out.append(path.read_bytes().decode("latin-1"))  # raw pages, WAL and journals included
        if path.suffix == ".db":
            conn = sqlite3.connect(path)
            try:
                out.extend(conn.iterdump())
            finally:
                conn.close()
    return "\n".join(out)


# ------------------------------------------------------------------ the card an agent gets


async def test_the_agent_gets_one_card_and_nothing_is_rented(panel):
    first = await card(panel)
    assert first["repo"] == REPO and first["can_answer_in_chat"] is False
    assert first["approval_id"] in first["review_url"] and "needs-you" in first["review_url"]
    assert "preview_open" in first["next"] and "approval_wait" in first["next"] and "same arguments" in first["next"]
    assert f"agentos environments repo add {REPO} --token" in first["fallback"]
    assert first["fallback"].startswith("At the node: ssh ") and "no admin rights" in first["fallback"]
    again = await card(panel)  # a second call for the same repository reuses the open card
    assert again["approval_id"] == first["approval_id"]
    assert len(panel["mgr"].store.requests()) == 1
    assert panel["vms"].store.recent() == [] and panel["mgr"].store.recent() == []
    ap = panel["rt"].get("approvals").get(first["approval_id"])
    assert ap.kind == "question" and ap.state == "pending" and REPO in ap.summary and "—" not in ap.summary


async def test_the_questions_list_marks_the_card(panel):
    out = await card(panel)
    async with client(panel) as c:
        h = await signed_in(panel, c)
        [q] = (await c.get("/api/questions", headers=h)).json()
    assert q["id"] == out["approval_id"] and q["repo_connect"]["repo"] == REPO
    assert q["repo_connect"]["reason"] == "not_connected" and q["repo_connect"]["host"] == "github.com"
    assert q["repo_connect"]["deploy_keys_url"] == "https://github.com/acme/private-app/settings/keys/new"
    assert q["repo_connect"]["token_url"].startswith("https://github.com/settings/")


# ------------------------------------------------------------------ the whole way, and where the token went


async def test_a_collaborator_connects_from_the_chat_and_the_token_goes_nowhere_else(panel, caplog):
    caplog.set_level(logging.DEBUG)
    seen: list[httpx.Request] = []
    connect.transport = github(seen=seen)
    results = [await card(panel)]
    approval_id = results[0]["approval_id"]
    responses = []
    async with client(panel) as c:
        h = await signed_in(panel, c)
        trip = await token_page(c, h, approval_id)
        responses.append(json.dumps(trip))
        r = await paste(c, trip, f"  {TOKEN}\n")
        responses.append(r.text)
        assert r.status_code == 200 and "Connected" in r.text and REPO in r.text, r.text
        assert "31 December 2026" in r.text  # GitHub's expiry, said on the page
        # the app waits on the trip, which says done and when the token expires, and never the token
        state = (await c.get(f"/api/forms/trips/{trip['id']}", headers=h)).json()
        responses.append(json.dumps(state))
        assert state["stage"] == "done" and state["form_url"] is None
        assert state["outcome"] == {"repo": REPO, "token_expires_at": "2026-12-31T23:59:59+00:00"}
        # a finished link is over: it neither shows the page nor takes a token
        assert (await c.get(trip["form_url"])).status_code == 410
        assert (await paste(c, trip, TOKEN)).status_code == 410
        responses.append((await c.get("/api/questions", headers=h)).text)
        responses.append((await c.get(f"/api/approvals/{approval_id}", headers=h)).text)
    # GitHub was asked with the token, and git read the repository with it as the gateway will
    assert [str(rq.url) for rq in seen] == ["https://api.github.com/repos/acme/private-app"]
    assert panel["git"].seen[-1] == f"https://{REPO}.git"
    # the card is approved, so the agent's wait returns, and the same call now opens it
    ap = await panel["rt"].get("approvals").wait(approval_id, 1)
    assert ap.state == "approved" and ap.status()["state"] == "approved"
    results.append(ap.status())
    opened = await panel["call"]("environment_open", title=TITLE, repo=REPO)
    results.append(opened)
    assert opened["state"] == "ready" and opened["repo"] == REPO, opened
    # kept as `agentos environments repo add --token` keeps it: a token for this one repository, declared for env
    [cred] = await git_credentials(panel["vault"])
    assert cred.type == "token" and cred.repos == ("acme/private-app",) and cred.host == "github.com"
    assert await panel["vault"].get_secret(cred.ref, purpose="test") == TOKEN
    assert panel["rt"].get("git").remotes.find(keymod_remote(), ["env"]) is not None
    # and nowhere else: results, answers, the trace and approval rows, every file on the node, the log
    traces = panel["policy"].traces.query(limit=500)[0]
    everything = "\n".join([json.dumps(results), *responses, json.dumps([t.public() for t in traces]),
                            dump(panel["mgr"].home), caplog.text])
    for leak in (TOKEN, basic(TOKEN), TOKEN[11:30]):
        assert leak not in everything
    assert "repo.token_form" in json.dumps([t.public() for t in traces])  # the link is traced, without the token


def keymod_remote():
    from agentos.git.wire import parse_remote

    return parse_remote(REPO)


# ------------------------------------------------------------------ who may send one


async def test_only_the_owner_in_person_opens_the_token_page(panel):
    approval_id = (await card(panel))["approval_id"]
    path = f"/api/repo-connect/{approval_id}/form"
    body = {"token": TOKEN}
    _, agent = panel["policy"].issue(subject={"id": "agent-2"}, workspaces=["*"], ceiling="commit")
    _, admin = panel["policy"].issue(subject={"id": "agent-3"}, workspaces=["*"], ceiling="commit", admin=True)
    async with client(panel) as c:
        # the agent's own grant, through any link it was given: a person decides this, never a token
        for tok in (panel["token"], agent, admin, "owner-token-for-tests"):
            r = await c.post(path, json=body, headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code in (401, 403), r.text
            assert r.json()["error"]["code"] in ("presence_required", "unauthorized"), r.text
        # the review link opens the card, and approves nothing
        r = await c.post(f"/api/approvals/{approval_id}/action", json={"decision": "approve"})
        assert r.status_code >= 400
        # an owner with a passkey confirms with it first: the panel's step-up
        panel["app"].state.api.members.store.add_credential(member_id="owner", cred_id=b"owner-passkey",
                                                            public_key=b"not-a-key", sign_count=0, label="Laptop",
                                                            aaguid=bytes(16))
        h = await signed_in(panel, c)
        r = await c.post(path, json=body, headers=h)
        assert r.status_code == 403 and r.json()["error"]["code"] == "step_up_required", r.text
        session = panel["panel_store"].get_session(c.cookies["agentos_session"])
        panel["panel_store"].verified(session)
        r = await c.post(path, headers=h)
        assert r.status_code == 200, r.text
        assert (await paste(c, r.json(), TOKEN)).status_code == 200
    assert panel["rt"].get("approvals").get(approval_id).state == "approved"
    assert TOKEN not in json.dumps([t.public() for t in panel["policy"].traces.query(limit=500)[0]])


def test_a_member_cannot_open_the_page(tmp_path):
    """The routes are the owner's alone: a member of the node, even an admin of every workspace who just
    confirmed with a passkey, is refused, and so is a grant."""
    from test_api_members import join, owner, role
    from test_node_panel import Node, bearer

    from agentos.environments.manager import EnvironmentManager

    n = Node(tmp_path)
    try:
        mgr = EnvironmentManager(n.config.home, runtime=n.runtime)
        n.runtime.register("environments", mgr)
        ap = connect.ask(mgr.store, n.runtime.get("approvals"), REPO, reason="not_connected",
                         requester=Principal(kind="agent", id="a", owner="owner"), grant_id=None, grant_path=None)
        boss = owner(n)
        ada = join(n, boss, "ada", [role("admin", "*")])
        ada.step_up()
        for path in ("form", "deploy-key", "check", "decline"):
            r = ada.post(f"/api/repo-connect/{ap.id}/{path}", {"token": TOKEN})
            assert r.status_code == 403 and r.json()["error"]["code"] == "denied", (path, r.text)
            r = n.client().post(f"/api/repo-connect/{ap.id}/{path}", json={"token": TOKEN},
                                headers=bearer(n.grant(admin=True)))
            assert r.status_code == 403, (path, r.text)
        assert n.runtime.get("approvals").get(ap.id).state == "pending"
    finally:
        n.close()


# ------------------------------------------------------------------ what GitHub says, in words


@pytest.mark.parametrize(("status", "headers", "body", "reason", "words"), [
    (401, {}, {"message": "Bad credentials"}, "bad_token", "wrong, expired or revoked"),
    (404, {}, {"message": "Not Found"}, "cannot_see", "cannot see github.com/acme/private-app"),
    (403, {"X-GitHub-SSO": "required; url=https://github.com/orgs/acme/sso?authorization_request=abc"},
     {"message": "Resource protected by organization SAML enforcement."}, "sso",
     "https://github.com/orgs/acme/sso?authorization_request=abc"),
    (403, {}, {"message": "This fine-grained personal access token is pending approval by the organization."},
     "org_approval", "waits for the acme organization's approval"),
    (403, {}, {"message": "`acme` forbids access via a personal access token (classic)."}, "org_policy",
     "does not allow this kind of token"),
    (403, {"x-ratelimit-remaining": "0"}, {"message": "API rate limit exceeded"}, "rate_limited", "rate limit"),
])
async def test_each_refusal_says_what_to_do_and_nothing_is_kept(panel, status, headers, body, reason, words):
    approval_id = (await card(panel))["approval_id"]
    connect.transport = github(status, headers=headers, body=body)
    with pytest.raises(connect.RepoConnectFailed) as e:
        await connect.connect_with_token(panel["mgr"], approval_id, Secret(TOKEN))
    assert e.value.reason == reason and words in e.value.message, e.value.message
    assert TOKEN not in e.value.message and "—" not in e.value.message
    assert await git_credentials(panel["vault"]) == []
    assert panel["rt"].get("git").remotes.find(keymod_remote(), ["env"]) is None
    assert panel["rt"].get("approvals").get(approval_id).state == "pending"  # the card stays, to try again


async def test_a_token_github_accepts_but_git_cannot_use_lacks_contents_read(panel):
    approval_id = (await card(panel))["approval_id"]
    panel["git"].good = None
    with pytest.raises(connect.RepoConnectFailed) as e:
        await connect.connect_with_token(panel["mgr"], approval_id, Secret(TOKEN))
    assert e.value.reason == "no_contents" and "Contents: Read" in e.value.message and "repo scope" in e.value.message
    assert await git_credentials(panel["vault"]) == []


async def test_the_refusals_are_said_on_the_page_and_the_same_link_tries_again(panel):
    approval_id = (await card(panel))["approval_id"]
    connect.transport = github(404, body={"message": "Not Found"})
    async with client(panel) as c:
        h = await signed_in(panel, c)
        trip = await token_page(c, h, approval_id)
        r = await paste(c, trip, TOKEN)
        assert r.status_code == 400 and "collaborator" in r.text and 'role="alert"' in r.text, r.text
        assert TOKEN not in r.text and 'name="token"' in r.text  # the field again, empty
        r = await paste(c, trip, "x")
        assert r.status_code == 400 and "one line" in r.text
        r = await paste(c, trip, "")
        assert r.status_code == 400 and "Paste the token" in r.text
        assert (await c.get(f"/api/forms/trips/{trip['id']}", headers=h)).json()["stage"] == "waiting"
        connect.transport = github()
        r = await paste(c, trip, TOKEN)  # the same link, until it runs out
        assert r.status_code == 200 and "Connected" in r.text, r.text
    assert panel["rt"].get("approvals").get(approval_id).state == "approved"


async def test_the_page_is_the_nodes_own_and_safe(panel):
    approval_id = (await card(panel))["approval_id"]
    async with client(panel) as c:
        h = await signed_in(panel, c)
        trip = await token_page(c, h, approval_id)
        page = await c.get(trip["form_url"])
        csp = page.headers["content-security-policy"]
        assert "default-src 'none'" in csp and "form-action 'self'" in csp and "frame-ancestors 'none'" in csp
        assert page.headers["cache-control"] == "no-store" and page.headers["referrer-policy"] == "no-referrer"
        text = page.text
        assert "<script" not in text and " src=" not in text and "<link" not in text  # nothing from outside
        assert "repo scope" in text and "Contents: Read" in text and "Read and write to push" in text
        assert "https://github.com/settings/personal-access-tokens/new" in text
        assert "https://github.com/settings/tokens/new?scopes=repo&amp;description=agentos" in text
        assert "—" not in text and f"Connect {REPO}" in text
        # another site cannot post a token into it, and the page only works over https or on this computer
        r = await c.post(trip["form_url"], content=b"token=x", headers={"Origin": "https://evil.example",
                                                                         "Content-Type": "application/x-www-form-urlencoded"})
        assert r.status_code >= 400 and "Connected" not in r.text
        far = httpx.AsyncClient(transport=httpx.ASGITransport(app=panel["app"], client=("203.0.113.9", 5000)),
                                base_url=ORIGIN)
        async with far:
            r = await far.get(trip["form_url"])
            assert r.status_code >= 400 and 'name="token"' not in r.text
        # the app's generic form route cannot open this kind: only the card's route does, with its step-up
        r = await c.post("/api/forms", json={"kind": "repo_token", "fields": {}}, headers=h)
        assert r.status_code == 400, r.text
        # and no API route takes a token any more
        r = await c.post(f"/api/repo-connect/{approval_id}/token", json={"token": TOKEN}, headers=h)
        assert r.status_code in (404, 405), r.text
    assert panel["rt"].get("approvals").get(approval_id).state == "pending"


async def test_a_card_closed_meanwhile_ends_its_page(panel):
    approval_id = (await card(panel))["approval_id"]
    async with client(panel) as c:
        h = await signed_in(panel, c)
        trip = await token_page(c, h, approval_id)
        assert (await c.post(f"/api/repo-connect/{approval_id}/decline", headers=h)).status_code == 200
        r = await paste(c, trip, TOKEN)
        assert r.status_code == 410 and "This card is closed" in r.text, r.text
        assert (await c.get(f"/api/forms/trips/{trip['id']}", headers=h)).json()["stage"] == "ended"
    assert await git_credentials(panel["vault"]) == []


# ------------------------------------------------------------------ a stored token that was refused


async def test_a_revoked_token_gets_the_same_card_and_a_new_one_replaces_it(panel):
    await put_token(panel["vault"], "github.com", ["acme/private-app"], Secret(OLD))
    panel["rt"].get("git").remotes.allow("env", REPO)
    panel["git"].good = None
    out = await panel["call"]("environment_open", title=TITLE, repo=REPO)
    assert out["status"] == "approval_pending" and "was refused" in out["why"] and "--replace" in out["fallback"]
    [row] = panel["mgr"].store.requests()
    assert row["reason"] == "refused"
    panel["git"].good = TOKEN
    done = await connect.connect_with_token(panel["mgr"], out["approval_id"], Secret(TOKEN))
    assert done["connected"]
    [cred] = await git_credentials(panel["vault"])
    assert await panel["vault"].get_secret(cred.ref, purpose="test") == TOKEN


# ------------------------------------------------------------------ the card closes however it is connected


async def test_the_card_closes_when_the_repository_is_connected_at_the_node(panel):
    """`agentos environments repo add --token` on the node: the reaper's sweep sees it and approves the card."""
    approval_id = (await card(panel))["approval_id"]
    assert await connect.sweep(panel["mgr"]) == []  # nothing declared yet: not even checked
    await put_token(panel["vault"], "github.com", ["acme/private-app"], Secret(TOKEN))
    panel["rt"].get("git").remotes.allow("env", REPO)
    connect._swept.clear()
    assert await connect.sweep(panel["mgr"]) == [approval_id]
    assert panel["rt"].get("approvals").get(approval_id).state == "approved"
    assert panel["mgr"].store.requests() == []


async def test_the_agents_next_open_closes_it_too(panel):
    approval_id = (await card(panel))["approval_id"]
    await put_token(panel["vault"], "github.com", ["acme/private-app"], Secret(TOKEN))
    panel["rt"].get("git").remotes.allow("env", REPO)
    out = await panel["call"]("environment_open", title=TITLE, repo=REPO)
    assert out["state"] == "ready", out
    assert panel["rt"].get("approvals").get(approval_id).state == "approved"


async def test_a_deploy_key_instead_and_the_check(panel):
    import tempfile
    from pathlib import Path

    short = Path(tempfile.mkdtemp(prefix="ag"))  # the key's agent socket needs a short path (macOS tmp is long)
    short.chmod(0o700)
    panel["rt"].get("git").gateway.agent_dir = short
    approval_id = (await card(panel))["approval_id"]
    async with client(panel) as c:
        h = await signed_in(panel, c)
        r = await c.post(f"/api/repo-connect/{approval_id}/deploy-key", headers=h)
        assert r.status_code == 200, r.text
        key = r.json()
        assert key["public_key"].startswith("ssh-ed25519 ") and key["fingerprint"].startswith("SHA256:")
        assert key["settings_url"] == "https://github.com/acme/private-app/settings/keys/new"
        again = (await c.post(f"/api/repo-connect/{approval_id}/deploy-key", headers=h)).json()
        assert again["public_key"] == key["public_key"]  # the same key, not a new one each press
        panel["git"].good = "nobody"  # not added at GitHub yet
        r = await c.post(f"/api/repo-connect/{approval_id}/check", headers=h)
        assert r.status_code == 400 and r.json()["error"]["code"] == "repo_connect_failed", r.text
        assert r.json()["error"]["message"].startswith("Not yet:")
        panel["mgr"].repo_probe = ok_probe
        r = await c.post(f"/api/repo-connect/{approval_id}/check", headers=h)
        assert r.status_code == 200 and r.json()["connected"], r.text
    assert panel["rt"].get("approvals").get(approval_id).state == "approved"
    [cred] = await git_credentials(panel["vault"])
    assert cred.kind == keymod.SSH_KIND


async def ok_probe(url, env, *, timeout_s=20, allow_file=False):
    return True, b""


async def test_not_now_declines_the_card(panel):
    approval_id = (await card(panel))["approval_id"]
    async with client(panel) as c:
        h = await signed_in(panel, c)
        r = await c.post(f"/api/repo-connect/{approval_id}/decline", headers=h)
        assert r.status_code == 200 and r.json()["declined"], r.text
        r = await c.post(f"/api/repo-connect/{approval_id}/form", headers=h)
        assert r.status_code == 404, r.text
    assert panel["rt"].get("approvals").get(approval_id).state == "denied"


def test_the_card_words_hold_no_secret_and_no_em_dash():
    for refused in (False, True):
        words = connect.card_words(REPO, refused=refused)
        assert REPO in words and "—" not in words and "--token" in words


def test_the_request_rows_ask_once_per_repository(tmp_path):
    from test_policy_support import make_service

    from agentos.environments.store import EnvironmentStore

    policy, _rt = make_service(tmp_path)
    store = EnvironmentStore(tmp_path / "environments.db")
    who = Principal(kind="agent", id="a", owner="owner")
    a = connect.ask(store, policy.approvals, REPO, reason="not_connected", requester=who, grant_id=None, grant_path=None)
    b = connect.ask(store, policy.approvals, REPO, reason="not_connected", requester=who, grant_id=None, grant_path=None)
    c = connect.ask(store, policy.approvals, "github.com/acme/other", reason="not_connected", requester=who,
                    grant_id=None, grant_path=None)
    assert a.id == b.id != c.id and len(store.requests()) == 2
    assert connect.close(store, policy.approvals, REPO) == [a.id]
    assert [r["repo"] for r in store.requests()] == ["github.com/acme/other"]
    policy.close()
