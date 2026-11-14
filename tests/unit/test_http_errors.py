"""One error shape and one status map across the node's API, its MCP endpoint and the controller
(agentos.core.http_errors)."""

import json

import pytest
from starlette.responses import PlainTextResponse
from starlette.testclient import TestClient

from agentos.api import errors as api_errors
from agentos.controller import web
from agentos.controller.app import Controller, build_app
from agentos.controller.config import ControllerConfig
from agentos.controller.mcp import AgentAuth
from agentos.core import catalog, http_errors
from agentos.core.errors import AgentosError, NotFound
from agentos.mcp.http import BearerAuth


def _fix_of(response) -> str:
    return response.json()["error"]["fix"]


def _names_a_token_or_reconnect(fix: str) -> bool:
    return any(word in fix.lower() for word in ("bearer", "token", "agentos connect"))


def test_the_api_and_the_controller_use_the_same_map():
    assert api_errors.STATUS_BY_CODE is http_errors.STATUS_BY_CODE
    odd = AgentosError("x")
    odd.code = "something_new"
    assert api_errors.status_for(odd) == 500
    assert web.agentos_error(odd).status_code == 500  # was 400 on the controller
    assert web.agentos_error(NotFound("gone")).status_code == 404
    assert web.agentos_error(type("Busy", (AgentosError,), {"code": "already_decided"})("x")).status_code == 409


def test_controller_401_is_the_shared_envelope_with_a_challenge():
    r = web.error(401, "unauthorized", "Sign in.")
    body = json.loads(r.body)
    assert body["error"]["code"] == "unauthorized" and body["error"]["message"] == "Sign in."
    assert "fix" in body["error"]
    assert r.headers["www-authenticate"] == 'Bearer realm="agentos"'


def test_mcp_endpoints_answer_401_in_the_envelope_with_www_authenticate():
    async def app(scope, receive, send):
        await PlainTextResponse("ok")(scope, receive, send)

    class Grants:
        def resolve(self, token):
            return None

    for wrapped in (BearerAuth(app, "node-token"), AgentAuth(app, Grants())):
        r = TestClient(wrapped).post("/mcp", json={})
        assert r.status_code == 401
        assert r.headers["www-authenticate"] == 'Bearer realm="agentos"'
        err = r.json()["error"]
        assert err["code"] == "unauthorized" and err["message"] and err["fix"]
        assert "—" not in r.text
    ok = TestClient(BearerAuth(app, "node-token")).post("/mcp", headers={"Authorization": "Bearer node-token"})
    assert ok.status_code == 200


# ---- the fix a 401 tells a person, per surface (the catalog's `unauthorized` fix is the panel's) ----

async def _plain_app(scope, receive, send):
    await PlainTextResponse("ok")(scope, receive, send)


def test_the_node_mcp_401_fix_names_the_bearer_token_not_the_panel():
    r = TestClient(BearerAuth(_plain_app, "node-token")).post("/mcp", json={})
    assert r.status_code == 401
    fix = _fix_of(r)
    assert "panel login" not in fix
    assert _names_a_token_or_reconnect(fix), fix


def test_the_controller_mcp_401_fix_does_not_send_people_to_the_panel():
    class NoGrants:
        def resolve(self, token):
            return None

    r = TestClient(AgentAuth(_plain_app, NoGrants())).post("/mcp", json={})
    assert r.status_code == 401
    fix = _fix_of(r)
    assert "panel login" not in fix
    assert _names_a_token_or_reconnect(fix), fix


@pytest.fixture
def controller_client(tmp_path):
    controller = Controller(ControllerConfig(home=tmp_path / "controller", owner_token="owner-token-401-fix",
                                             agent_token="aga_401_fix_agent_token_for_tests"))
    try:
        yield TestClient(build_app(controller), base_url="http://localhost")
    finally:
        controller.close()


def test_the_controller_401_fix_names_the_controller_sign_in(controller_client):
    r = controller_client.get("/api/nodes")
    assert r.status_code == 401
    fix = _fix_of(r)
    assert "agentos controller login" in fix
    assert "panel login" not in fix


def test_a_paused_lane_is_not_a_server_error():
    from agentos.core.errors import AgentPaused, HumanInControl

    assert web.agentos_error(AgentPaused("paused")).status_code == 409
    assert web.agentos_error(HumanInControl("a person has it")).status_code == 409


def test_the_node_routes_answer_through_the_shared_map():
    """The runner, workspace, brain and vault routes used to keep their own code-to-status maps. They answer
    through the shared one now, except where a route has always answered differently (its own map, named in
    the module): a refused export and the vault's locked-out, entry, vault and busy answers."""
    from agentos.brains import api as brains_api
    from agentos.core.errors import ApprovalPending, Conflict, EngineError, LaneBusy, LaneNotFound
    from agentos.runner import api as runner_api
    from agentos.vault import api as vault_api
    from agentos.vault.errors import VaultBusy, VaultLockedOut, VaultSealed
    from agentos.workspaces import api as workspaces_api
    from agentos.workspaces.transfer.format import ExportRefused

    for module in (runner_api, workspaces_api, brains_api, vault_api):
        assert not hasattr(module, "STATUS") and not hasattr(module, "_STATUS_BY_CODE"), module.__name__

    # the shared answers, where the old maps said nothing (runner and workspaces fell back to 400, brains to 500)
    assert runner_api._error(EngineError("engine down")).status_code == 502
    assert runner_api._error(LaneBusy("busy")).status_code == 409
    assert workspaces_api._error(Conflict("taken")).status_code == 409
    assert brains_api._err(LaneNotFound("gone")).status_code == 404
    assert brains_api._err(ApprovalPending("wait", "ap_1")).status_code == 202
    assert vault_api.status_for(EngineError("engine down")) == 502
    assert vault_api.status_for(VaultSealed("sealed")) == 503

    # the routes' own answers, kept where the shared map says otherwise
    assert workspaces_api._error(ExportRefused("no")).status_code == 409
    assert vault_api.status_for(VaultBusy("busy")) == 409
    assert vault_api.status_for(VaultLockedOut("locked")) == 429


# ---- status_of for bare codes: a refusal is never the server's 500 ----

@pytest.mark.parametrize("code", ["audit_unwritable", "secret_in_memory", "vault_wrong_key", "element_not_found"])
def test_a_refusal_code_is_a_client_or_unavailable_status_not_500(code):
    """Each of these is something a caller can act on (a full disk, a secret offered to memory, a wrong
    key, a locator that matches nothing), not a bug on the server."""
    assert http_errors.status_of(code) != 500
    assert 400 <= http_errors.status_of(code) < 600


def test_every_catalog_code_has_its_status_placed_on_purpose():
    """Only the node's own failures (SERVER_ERRORS) answer 500; every other catalog code is in the map, so a
    new code cannot fall through to 500 unnoticed."""
    codes = set(catalog.entries())
    unplaced = sorted(codes - set(http_errors.STATUS_BY_CODE) - http_errors.SERVER_ERRORS)
    assert not unplaced, f"place these codes in STATUS_BY_CODE or SERVER_ERRORS: {', '.join(unplaced)}"
    assert not http_errors.SERVER_ERRORS & set(http_errors.STATUS_BY_CODE)
    assert not http_errors.SERVER_ERRORS - codes, "SERVER_ERRORS names a code the catalog does not have"
    refusals = sorted(c for c in codes - http_errors.SERVER_ERRORS - {"internal"} if http_errors.status_of(c) == 500)
    assert not refusals, refusals


@pytest.mark.parametrize("code", ["grant_expired", "grant_not_yet_valid", "grant_revoked", "grant_unknown"])
def test_a_runs_grant_that_no_longer_counts_is_never_a_401(code):
    """A 401 tells the panel its own sign-in ended, and signs the owner out; a run's grant is not that."""
    assert http_errors.status_of(code) == 403
