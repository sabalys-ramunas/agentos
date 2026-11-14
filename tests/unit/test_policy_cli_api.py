"""The policy CLI, and the panel API on the mounted node (agentos.api) over the real policy."""

from __future__ import annotations

import time

import pytest
from test_node_panel import TOKEN, Node, bearer
from test_policy_support import OWNER, make_service
from typer.testing import CliRunner

from agentos.core.config import NodeConfig, save_config
from agentos.core.errors import ApprovalPending
from agentos.core.models import Effect
from agentos.policy.cli import app
from agentos.policy.errors import Denied
from agentos.policy.grants import LaneCap, Specials

runner = CliRunner()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path))
    save_config(NodeConfig(home=tmp_path, mcp_token=OWNER))
    return tmp_path


def run(*args, input=None):
    res = runner.invoke(app, list(args), input=input)
    return res


def test_cli_grants_lifecycle(home):
    res = run("grants", "issue", "--subject", "claude-code", "--workspace", "biz-a", "--ceiling", "commit",
              "--special", "exec", "--budget-usd", "5", "--ttl", "2d")
    assert res.exit_code == 0, res.output
    token = res.stdout.strip().splitlines()[-1]
    assert token.startswith("agt2_")
    gid = res.stdout.split()[1]
    listed = run("grants", "list")
    assert gid in listed.stdout and token not in listed.stdout
    shown = run("grants", "show", gid)
    assert '"ceiling": "commit"' in shown.stdout and token not in shown.stdout
    (home / "tok").write_text(token)
    child = run("grants", "attenuate", "--from", str(home / "tok"), "--ceiling", "read", "--subject", "sub")
    assert child.exit_code == 0, child.output
    widen = run("grants", "attenuate", "--from", "-", "--ceiling", "pay", input=token)
    assert widen.exit_code == 1 and "widen" in widen.output and token not in widen.output
    assert run("grants", "revoke", gid).stdout.startswith(f"revoked {gid} and 1")
    assert run("grants", "revoke", gid).exit_code == 1
    assert "\u2014" not in res.output + listed.output + shown.output + child.output


def test_cli_issues_no_agent_grant_into_a_frozen_lane(home):
    """The node's CLI opens the policy without the daemon: the freeze records on disk still count."""
    from agentos.freeze.store import FreezeRecord, FreezeStore

    FreezeStore(home).put(FreezeRecord("lane", "biz-a/social", "odd post", "owner (node cli)", time.time(), via="cli"))
    refused = run("grants", "issue", "--subject", "late-agent", "--lane", "biz-a/social", "--ceiling", "commit")
    assert refused.exit_code != 0 and "lane biz-a/social is frozen" in refused.output, refused.output
    wider = run("grants", "issue", "--subject", "late-agent", "--workspace", "biz-a", "--ceiling", "commit")
    assert wider.exit_code == 0, wider.output  # it reaches more; the freeze gate refuses it on the lane
    elsewhere = run("grants", "issue", "--subject", "late-agent", "--workspace", "biz-b")
    assert elsewhere.exit_code == 0, elsewhere.output


def test_cli_rotates_the_grant_key_with_an_overlap(home):
    res = run("grants", "issue", "--subject", "claude-code", "--workspace", "biz-a")
    token = res.stdout.strip().splitlines()[-1]
    old_kid = run("grants", "keys").stdout.split()[0]
    assert run("grants", "rotate-key").exit_code == 1  # asks first; no input aborts
    rotated = run("grants", "rotate-key", "--overlap", "1h", "--yes")
    assert rotated.exit_code == 0, rotated.output
    assert f"previous key {old_kid} accepted until" in rotated.stdout
    keys = run("grants", "keys").stdout
    assert f"{old_kid}  retiring until" in keys and "active" in keys
    svc, _ = make_service(home, clock=time.time)  # the CLI used the real clock
    try:
        assert svc.resolve(token)[0].id == "claude-code"  # inside the overlap
        assert run("grants", "retire-key", old_kid).exit_code == 0
        with pytest.raises(Denied, match="retired_key"):
            svc.resolve(token)
    finally:
        svc.close()
    assert run("grants", "retire-key", "ed25519:" + "0" * 32).exit_code == 1
    assert "\u2014" not in rotated.output + keys


async def test_cli_approvals_and_standing(home):
    svc, _ = make_service(home)
    _, tok = svc.issue(subject={"id": "a"}, workspaces=["biz-a"], ceiling=Effect.COMMIT)
    ctx = svc.context(svc.resolve(tok)[1], tool="t")
    with pytest.raises(ApprovalPending) as e:
        await ctx.authorize(Effect.COMMIT, lane_id="biz-a/x", summary="Post it", content=1)
    ap = e.value.approval_id
    assert ap in run("approvals", "list").stdout
    assert run("approvals", "approve", ap, "--note", "ok").stdout.strip() == f"{ap} approved"
    again = run("approvals", "deny", ap)
    assert again.exit_code == 1 and again.output.startswith("error: conflict:")
    unbound = run("approvals", "standing", "add", "--effect", "commit", "--workspace", "biz-a", "--until", "1d")
    assert unbound.exit_code == 1 and "must name what it covers" in unbound.output  # AUD-10
    res = run("approvals", "standing", "add", "--effect", "commit", "--workspace", "biz-a", "--count", "20",
              "--until", "2099-01-01", "--target", "flow:biz-a.instagram.story@v1")
    assert res.exit_code == 0 and "20 uses" in res.stdout
    assert "20 left" in run("approvals", "standing", "list").stdout
    bad = run("approvals", "standing", "add", "--effect", "pay", "--workspace", "biz-a", "--until", "1d")
    assert bad.exit_code == 1 and "cannot cover pay" in bad.output


def test_cli_routing_traces_and_notify(home):
    out = run("routing")
    assert out.exit_code == 0 and "irreversible" in out.stdout
    assert run("routing", "--json").stdout.lstrip().startswith("{")
    assert run("traces").exit_code == 0
    assert "BEGIN PUBLIC KEY" in run("key").stdout
    nt = run("notify", "test")
    assert nt.exit_code == 0 and "log: delivered" in nt.stdout  # the log channel is on by default
    assert run("notify", "ntfy", "my-topic").exit_code != 0  # guessable (AUD-44)
    made = run("notify", "ntfy", "--token-from", "-", input="tk_hidden_value")
    assert made.exit_code == 0 and "subscribe to the topic agentos-" in made.stdout
    assert "tk_hidden_value" not in run("notify", "show").stdout


# ------------------------------------------------------------------ API (the mounted panel, on the real policy)

LANE = "biz-a/instagram"


@pytest.fixture
def node(tmp_path):
    n = Node(tmp_path)
    yield n
    n.close()


async def pending_id(coro) -> str:
    with pytest.raises(ApprovalPending) as e:
        await coro
    return e.value.approval_id


def test_api_requires_auth(node):
    grant = node.grant()
    with node.client() as c:
        r = c.get("/api/approvals")
        assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
        assert c.get("/api/approvals", headers=bearer("agt1_garbage")).status_code == 401
        assert c.get("/api/approvals", headers=bearer(grant)).status_code == 200  # a real grant gets in


async def test_api_needs_you_flow(node):
    tok = node.grant(subject={"id": "a"}, ceiling="commit")
    ctx = node.policy.context_for_token(tok, tool="t")
    ids = [await pending_id(ctx.authorize(Effect.COMMIT, lane_id=LANE, summary=f"Post {i}", content=i))
           for i in range(3)]
    with node.client() as c:
        csrf = node.sign_in(c)
        r = c.get("/api/approvals?state=pending", headers=csrf)
        assert r.status_code == 200 and {a["id"] for a in r.json()} == set(ids)
        # the agent sees its own approvals, but cannot decide them; the node token is no person either
        assert len(c.get("/api/approvals", headers=bearer(tok)).json()) == 3
        r = c.post(f"/api/approvals/{ids[0]}/approve", headers=bearer(tok), json={})
        assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"
        r = c.post(f"/api/approvals/{ids[0]}/approve", headers=bearer(TOKEN), json={})
        assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"
        r = c.post(f"/api/approvals/{ids[0]}/approve", headers=csrf, json={"note": "go"})
        assert r.status_code == 200 and r.json()["state"] == "approved" and r.json()["note"] == "go"
        assert c.post(f"/api/approvals/{ids[0]}/deny", headers=csrf, json={}).status_code == 409
        batch = c.post("/api/approvals/batch", headers=csrf, json={"ids": ids[1:], "decision": "deny"})
        assert [x["ok"] for x in batch.json()["results"]] == [True, True]
        assert c.post("/api/approvals/batch", headers=csrf, json={"ids": "x"}).status_code == 400
        assert c.get("/api/approvals/ap_missing", headers=csrf).status_code == 404


async def test_api_action_link(node):
    tok = node.grant(subject={"id": "a"}, ceiling="commit")
    ctx = node.policy.context_for_token(tok, tool="t")
    ap = await pending_id(ctx.authorize(Effect.COMMIT, lane_id=LANE, content=1))
    exp = int(node.policy.clock() + 600)
    approve = node.policy.approvals.sign_action(ap, "approve", exp)
    sig = node.policy.approvals.sign_action(ap, "deny", exp)
    with node.client() as c:
        # a phone link never approves, even with a valid signature
        assert c.post(f"/api/approvals/{ap}/action?d=approve&e={exp}&s={approve}").status_code == 403
        assert c.post(f"/api/approvals/{ap}/action?d=deny&e={exp}&s=bad").status_code == 401
        r = c.post(f"/api/approvals/{ap}/action?d=deny&e={exp}&s={sig}")
        assert r.status_code == 200 and r.json()["state"] == "denied"
        assert c.post(f"/api/approvals/{ap}/action?d=deny&e={exp}&s={sig}").status_code == 409


async def test_api_standing_needs_a_person_and_an_approved_binding(node):
    tok = node.grant(subject={"id": "a"}, ceiling="commit", specials=Specials(secret=LaneCap()))
    post = {"flow": "biz-a.instagram.story", "version": "v1", "lane": LANE, "params": {"caption": "Autumn sale"}}
    ap = await pending_id(node.policy.context_for_token(tok, tool="flow_run").authorize(
        Effect.COMMIT, lane_id=LANE, content=post))
    body = {"lane": LANE, "ttl_seconds": 7 * 86400}
    with node.client() as c:
        csrf = node.sign_in(c)
        unbound = c.post("/api/approvals/standing", headers=csrf, json=body)
        assert unbound.status_code == 400 and "must name what it covers" in unbound.text  # AUD-10
        unapproved = c.post("/api/approvals/standing", headers=csrf, json={**body, "from_approvals": [ap]})
        assert unapproved.status_code == 400 and "approve it first" in unapproved.text
        assert c.post(f"/api/approvals/{ap}/approve", headers=csrf, json={}).status_code == 200
        r = c.post("/api/approvals/standing", headers=bearer(TOKEN), json={**body, "from_approvals": [ap]})
        assert r.status_code == 403 and r.json()["error"]["code"] == "presence_required"
        r = c.post("/api/approvals/standing", headers=csrf, json={**body, "from_approvals": [ap]})
        assert r.status_code == 200 and r.json()["targets"] == ["flow:biz-a.instagram.story@v1"]
        sid = r.json()["id"]
        assert [s["id"] for s in c.get("/api/approvals/standing", headers=csrf).json()] == [sid]
        assert c.delete(f"/api/approvals/standing/{sid}", headers=csrf).status_code == 200
        assert c.get("/api/approvals/standing", headers=csrf).json() == []


def test_api_grants_are_issued_and_revoked_by_the_owner(node):
    with node.client() as c:
        r = c.post("/api/grants", headers=bearer(TOKEN), json={
            "subject": "codex", "workspaces": ["biz-a"], "ceiling": "draft", "specials": ["exec"],
            "budget": {"usd": 3}, "ttl": "1d"})
        assert r.status_code == 200, r.text
        gid, tok = r.json()["id"], r.json()["token"]
        listed = c.get("/api/grants", headers=bearer(TOKEN)).json()
        assert gid in [g["id"] for g in listed] and tok not in str(listed)  # the token is shown once
        assert c.post("/api/grants", headers=bearer(tok), json={"subject": "x", "workspaces": ["biz-a"]}).status_code == 403
        bad = c.post("/api/grants", headers=bearer(TOKEN),
                     json={"subject": "x", "workspaces": ["biz-a"], "lanes": ["NOT A LANE"]})
        assert bad.status_code == 400
        assert c.post(f"/api/grants/{gid}/revoke", headers=bearer(TOKEN)).status_code == 200
        assert c.get("/api/grants", headers=bearer(tok)).status_code == 401  # a revoked token is no one


def test_api_admin_grant_issues_only_within_itself(node):
    """The panel passes the caller's grant as `within`: an admin grant's token issues grants no wider
    than itself, and the owner's token issues any."""
    admin = node.grant(subject={"id": "boss-bot"}, workspaces=["biz-a"], ceiling="draft", admin=True)
    body = {"subject": "helper", "workspaces": ["biz-a"], "ceiling": "draft", "ttl": "1d"}
    with node.client() as c:
        lane = c.post("/api/grants", headers=bearer(admin), json={**body, "workspaces": ["biz-b"]})
        assert lane.status_code == 400 and lane.json()["error"]["message"].endswith("holds: lanes"), lane.text
        ceiling = c.post("/api/grants", headers=bearer(admin), json={**body, "ceiling": "commit"})
        assert ceiling.status_code == 400 and ceiling.json()["error"]["message"].endswith("holds: ceiling"), \
            ceiling.text
        assert c.post("/api/grants", headers=bearer(admin), json=body).status_code == 200
        owner = c.post("/api/grants", headers=bearer(TOKEN),
                       json={**body, "workspaces": ["biz-b"], "ceiling": "commit"})
        assert owner.status_code == 200, owner.text


def test_api_activity_shows_each_grant_only_its_own_events(node):
    from agentos.policy.traces import TraceEvent

    tok = node.grant(subject={"id": "a"}, workspaces=["biz-a"])
    _, g = node.policy.resolve(tok)
    for i in range(5):
        node.policy.traces.write(TraceEvent(kind="tool_end", lane=LANE, grant_id=g.id, grant_path=g.path,
                                            summary=f"mine {i}"))
    node.policy.traces.write(TraceEvent(kind="tool_end", lane="biz-b/x", grant_id="gr_other", grant_path="/gr_other/"))
    with node.client() as c:
        mine = c.get("/api/activity", headers=bearer(tok)).json()
        assert len(mine) == 5 and all(e["lane"] == LANE for e in mine)
        assert "grant_path" not in mine[0] and mine[0]["at"].endswith("Z")
        everyone = c.get("/api/activity?limit=100", headers=bearer(TOKEN)).json()
        assert {LANE, "biz-b/x"} <= {e.get("lane") for e in everyone}
        assert len(c.get("/api/activity?limit=3", headers=bearer(TOKEN)).json()) == 3


def test_api_notification_settings(node):
    import httpx

    sent: list[str] = []

    def ntfy(request: httpx.Request) -> httpx.Response:  # never the real ntfy.sh from a unit test
        sent.append(request.content.decode())
        return httpx.Response(200, json={"id": "test"})

    node.policy.notifier.transport = httpx.MockTransport(ntfy)
    with node.client() as c:
        csrf = node.sign_in(c)
        r = c.put("/api/settings/notifications", headers=csrf,
                  json={"ntfy": {"topic": "agentos-t1-Qw8eR4tY6uI2oP0aS9dFg", "token": "tk_very_secret"},
                        "public_base_url": "https://a.example"})
        assert r.status_code == 200 and r.json()["ntfy"]["token_saved"] is True and "tk_very_secret" not in r.text
        got = c.get("/api/settings/notifications", headers=csrf)
        assert got.json()["public_base_url"] == "https://a.example" and "tk_very_secret" not in got.text
        bad = c.put("/api/settings/notifications", headers=csrf, json={"ntfy": {"topic": "bad topic!"}})
        assert bad.status_code == 400
        res = c.post("/api/settings/notifications/test", headers=csrf)
        assert res.status_code == 200 and res.json()["results"]["log"] == "delivered"
        assert sent and all("agentos-t1-Qw8eR4tY6uI2oP0aS9dFg" in body for body in sent)  # to the stub only


def test_approving_a_question_without_an_answer_says_how_to_answer(home):
    """QA 2026-11-10, tester B 9: "answering a question needs an answer" named neither --answer nor the choices."""
    from agentos.core.models import Principal
    svc, _ = make_service(home)
    q = svc.approvals.create_question(question="Activate tb-books?", options=["Activate", "Leave it"],
                                      requester=Principal(kind="agent", id="a"), grant_id=None, grant_path=None)
    svc.close()
    res = run("approvals", "approve", q.id)
    assert res.exit_code == 1
    assert '--answer "Activate"' in res.output and '--answer "Leave it"' in res.output, res.output
    assert f"agentos approvals approve {q.id}" in res.output and "—" not in res.output
    res = run("approvals", "approve", q.id, "--answer", "Activate")
    assert res.exit_code == 0 and "answered: Activate" in res.output and "within a few seconds" in res.output
