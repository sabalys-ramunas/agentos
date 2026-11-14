"""Panel API: auth, sessions, CSRF, rate limits, error envelope, static serving."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from agentos.api import ApiSettings, PanelStore, build_api, create_login_code
from agentos.api.auth import SESSION_COOKIE, RateLimiter, origin_allowed
from agentos.api.fakes import demo_runtime
from agentos.core import catalog
from agentos.core.config import NodeConfig, save_config
from agentos.core.errors import EngineError, LaneBusy, PolicyDenied
from agentos.core.runtime import Runtime

OWNER = "owner-token-abc"


def make(runtime=None, **settings):
    store = PanelStore(None)
    runtime = runtime if runtime is not None else demo_runtime()
    app = build_api(runtime, ApiSettings(store=store, owner_token=OWNER, **settings))
    return TestClient(app), store, runtime


def sign_in(client: TestClient, store: PanelStore) -> str:
    store.add_code("code-for-tests-1")
    r = client.post("/api/session", json={"code": "code-for-tests-1"})
    assert r.status_code == 200, r.text
    return r.json()["csrf"]


def test_unauthenticated_gets_401_envelope():
    client, _, _ = make()
    r = client.get("/api/status")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"
    assert "—" not in r.json()["error"]["message"]
    assert "agentos panel login" in r.json()["error"]["fix"]  # the panel's own sign-in stays in its fix


def test_owner_bearer_token_and_wrong_token():
    client, _, _ = make()
    assert client.get("/api/status", headers={"Authorization": f"Bearer {OWNER}"}).status_code == 200
    assert client.get("/api/status", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/status", headers={"Authorization": f"Basic {OWNER}"}).status_code == 401


def test_policy_panel_token_is_accepted():
    client, _, _ = make()
    r = client.get("/api/status", headers={"Authorization": "Bearer panel-token-123"})
    assert r.status_code == 200
    # a policy that vouches without grant scopes (this fake) gets the owner's rights
    assert r.json()["principal"] == {"kind": "human", "id": "owner-phone", "owner": True}
    assert r.json()["csrf"] is None  # bearer callers do not need CSRF


def test_owner_token_defaults_to_config_mcp_token():
    rt = Runtime(config=NodeConfig(name="node-7", mcp_token="from-config"))
    app = build_api(rt, ApiSettings(store=PanelStore(None)))
    client = TestClient(app)
    r = client.get("/api/status", headers={"Authorization": "Bearer from-config"})
    assert r.status_code == 200 and r.json()["node"] == "node-7"
    assert r.json()["mcp_url"] == "http://localhost:7400/mcp"


def test_login_code_is_one_time_and_sets_strict_httponly_cookie():
    client, store, _ = make()
    store.add_code("one-time-code-xyz")
    r = client.post("/api/session", json={"code": "one-time-code-xyz"})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert f"{SESSION_COOKIE}=" in cookie
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie.replace("Strict", "strict")
    assert "Path=/" in cookie
    status = client.get("/api/status").json()
    assert status["csrf"] == r.json()["csrf"]

    other = TestClient(client.app)
    again = other.post("/api/session", json={"code": "one-time-code-xyz"})
    assert again.status_code == 401 and again.json()["error"]["code"] == "unauthorized"


def test_expired_code_is_refused():
    client, store, _ = make()
    store.add_code("expired-code-123", ttl_seconds=-1)
    assert client.post("/api/session", json={"code": "expired-code-123"}).status_code == 401


def test_secure_cookie_over_https():
    """X-Forwarded-Proto is a proxy's word: it counts from a proxy on this machine, not from anyone."""
    client, store, _ = make()
    local = TestClient(client.app, client=("127.0.0.1", 50000))
    store.add_code("https-code-1234")
    r = local.post("/api/session", json={"code": "https-code-1234"}, headers={"X-Forwarded-Proto": "https"})
    assert "Secure" in r.headers["set-cookie"]
    store.add_code("https-code-5678")
    r = client.post("/api/session", json={"code": "https-code-5678"}, headers={"X-Forwarded-Proto": "https"})
    assert "Secure" not in r.headers["set-cookie"]  # a remote client cannot claim https


def test_csrf_required_for_cookie_writes():
    client, store, _ = make()
    csrf = sign_in(client, store)
    # read is fine without the header
    assert client.get("/api/workspaces").status_code == 200
    # a write without the header is refused
    r = client.post("/api/lanes/biz-a/facebook/open")
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"
    r = client.post("/api/lanes/biz-a/facebook/open", headers={"X-Agentos-CSRF": "wrong"})
    assert r.status_code == 403
    r = client.post("/api/lanes/biz-a/facebook/open", headers={"X-Agentos-CSRF": csrf})
    assert r.status_code == 200 and r.json()["state"] == "warm"


def test_cross_site_origin_refused_even_with_csrf():
    client, store, _ = make()
    csrf = sign_in(client, store)
    r = client.post("/api/lanes/biz-a/facebook/open",
                    headers={"X-Agentos-CSRF": csrf, "Origin": "https://evil.example"})
    assert r.status_code == 403
    ok = client.post("/api/lanes/biz-a/facebook/open",
                     headers={"X-Agentos-CSRF": csrf, "Origin": "http://testserver"})
    assert ok.status_code == 200


def test_bearer_writes_need_no_csrf():
    client, _, _ = make()
    r = client.post("/api/lanes/biz-a/facebook/open", headers={"Authorization": f"Bearer {OWNER}"})
    assert r.status_code == 200


def test_logout_clears_session():
    client, store, _ = make()
    csrf = sign_in(client, store)
    assert client.delete("/api/session", headers={"X-Agentos-CSRF": csrf}).status_code == 200
    assert client.get("/api/status").status_code == 401


def test_login_rate_limit():
    client, _, _ = make(login_per_ip=3, login_window=60)
    codes = [client.post("/api/session", json={"code": f"wrong-code-{i}"}).status_code for i in range(5)]
    assert codes[:3] == [401, 401, 401]
    assert codes[3] == 429
    r = client.post("/api/session", json={"code": "wrong-code-x"})
    assert r.json()["error"]["code"] == "rate_limited" and "retry-after" in r.headers


def test_rate_limiter_global_cap():
    lim = RateLimiter(per_key=100, global_max=2, window=60)
    assert lim.check("a") is None and lim.check("b") is None
    assert lim.check("c") is not None


def test_origin_rules():
    assert origin_allowed(None, "localhost:7400")
    assert origin_allowed("http://localhost:7400", "localhost:7400")
    assert not origin_allowed("http://localhost:5173", "localhost:7400")
    assert origin_allowed("http://localhost:5173", "localhost:7400", ("http://localhost:5173",))
    assert not origin_allowed("null", "localhost:7400")


def test_create_login_code_helper_shares_panel_db(tmp_path):
    save_config(NodeConfig(home=tmp_path, mcp_port=7411))
    link = create_login_code(tmp_path)
    assert link.url.startswith("http://localhost:7411/login#code=")
    assert link.code not in (tmp_path / "panel.db").read_bytes().decode("latin-1")  # only the hash is stored
    rt = Runtime(config=NodeConfig(home=tmp_path))
    client = TestClient(build_api(rt))
    assert client.post("/api/session", json={"code": link.code}).status_code == 200
    assert client.get("/api/status").status_code == 200


def test_cli_login_prints_link(tmp_path):
    from typer.testing import CliRunner

    from agentos.api.cli import app

    save_config(NodeConfig(home=tmp_path))
    out = CliRunner().invoke(app, ["login", "--home", str(tmp_path)])
    assert out.exit_code == 0, out.output
    assert "/login#code=" in out.output and "—" not in out.output


def test_validation_errors_do_not_echo_input():
    client, _, _ = make()
    r = client.post("/api/vault", headers={"Authorization": f"Bearer {OWNER}"},
                    json={"name": "x", "kind": "nope", "domain": "d", "secret": "hunter2-very-secret"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid"
    assert "hunter2" not in r.text


def test_bad_json_is_400():
    client, _, _ = make()
    r = client.post("/api/workspaces", content=b"{not json", headers={"Authorization": f"Bearer {OWNER}"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid"


@pytest.mark.parametrize("exc,status,code", [
    (LaneBusy("all slots active"), 409, "busy"),
    (PolicyDenied("not in your grant"), 403, "denied"),
    (EngineError("browser crashed"), 502, "engine_error"),
    (RuntimeError("password=hunter2"), 500, "internal"),
])
def test_error_mapping(exc, status, code):
    rt = demo_runtime()

    async def boom(lane_id):
        raise exc

    rt.lanes.open = boom
    client, _, _ = make(rt)
    r = client.post("/api/lanes/biz-a/facebook/open", headers={"Authorization": f"Bearer {OWNER}"})
    assert r.status_code == status
    assert r.json()["error"]["code"] == code
    assert "hunter2" not in r.text


def test_missing_service_is_503():
    client, _, _ = make(Runtime())
    r = client.get("/api/vault", headers={"Authorization": f"Bearer {OWNER}"})
    assert r.status_code == 503
    assert r.json()["error"] == {"code": "unavailable", "message": "vault is not configured on this node.",
                                 "fix": catalog.person_fix("unavailable")}


def test_api_responses_are_not_cached_and_have_security_headers():
    client, _, _ = make()
    r = client.get("/api/status", headers={"Authorization": f"Bearer {OWNER}"})
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"


def test_unknown_api_route_is_json_404():
    client, _, _ = make()
    r = client.get("/api/nothing-here")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


# ---- static panel ----

def _dist(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>agentos</title><div id=root></div>")
    (dist / "assets" / "index-abc123.js").write_text("console.log(1)")
    (dist / "icon.svg").write_text("<svg/>")
    (tmp_path / "secret.txt").write_text("outside")
    return dist


def test_static_serving_with_spa_fallback_and_cache_headers(tmp_path):
    client, _, _ = make(panel_dir=_dist(tmp_path))
    index = client.get("/")
    assert index.status_code == 200 and "agentos" in index.text
    assert index.headers["cache-control"] == "no-cache"
    assert "default-src 'self'" in index.headers["content-security-policy"]
    asset = client.get("/assets/index-abc123.js")
    assert asset.status_code == 200 and "immutable" in asset.headers["cache-control"]
    deep = client.get("/lanes/biz-a/instagram")
    assert deep.status_code == 200 and "agentos" in deep.text
    assert client.get("/assets/missing-old.js").status_code == 404
    assert client.get("/icon.svg").headers["cache-control"] == "no-cache"


def test_static_refuses_path_traversal(tmp_path):
    client, _, _ = make(panel_dir=_dist(tmp_path))
    for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt"):
        r = client.get(path)
        assert "outside" not in r.text


def test_panel_not_built_page(tmp_path):
    # "not built" is for a checkout with the panel's source beside its missing dist; with no source at all
    # the page says there is no web app (agentos.api.static.placeholder, tests/unit/test_frontdoor.py)
    (tmp_path / "panel").mkdir()
    (tmp_path / "panel" / "package.json").write_text("{}")
    client, _, _ = make(panel_dir=tmp_path / "panel" / "dist")
    r = client.get("/")
    assert r.status_code == 200 and "not built" in r.text and "—" not in r.text
    r = make(panel_dir=tmp_path / "nope")[0].get("/")
    assert r.status_code == 200 and "has no web app" in r.text and "—" not in r.text
