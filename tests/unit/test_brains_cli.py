"""`agentos brains ...` CLI: credentials only from stdin or files, never echoed."""

import json

import pytest
from typer.testing import CliRunner

from agentos.brains import cli as brains_cli
from agentos.brains.testing import OAUTH, OWNER_TOKEN, make_service


@pytest.fixture
def svc(tmp_path, monkeypatch):
    s = make_service(tmp_path)
    monkeypatch.setattr(brains_cli, "runtime_factory", lambda: s.runtime)
    return s


runner = CliRunner()


def invoke(*args, input=None):
    r = runner.invoke(brains_cli.app, list(args), input=input)
    return r.exit_code, r.output


def test_add_list_order_use_remove(svc):
    code, out = invoke("add", "claude-max", "--kind", "subscription", "--provider", "anthropic", "--stdin",
                       "--insecure-dev-store", input=OAUTH + "\n")
    assert code == 0, out
    assert "added claude-max" in out and OAUTH not in out
    code, out = invoke("add", "ollama", "--kind", "local", "--provider", "ollama", "--model", "qwen3:8b")
    assert code == 0, out
    code, out = invoke("add", "api", "--kind", "metered", "--provider", "anthropic", "--no-credential")
    assert code == 0 and "needs_auth" in out
    code, out = invoke("list")
    assert code == 0 and "claude-max" in out and "ready" in out and OAUTH not in out and "\u2014" not in out
    code, out = invoke("list", "--json")
    assert {b["name"] for b in json.loads(out)} == {"claude-max", "ollama", "api"}
    code, out = invoke("order", "ollama", "claude-max")
    assert code == 0 and out.strip().startswith("order: ollama, claude-max")
    code, out = invoke("use", "ollama", "--workspace", "personal")
    assert code == 0 and "personal: ollama" in out
    code, out = invoke("privacy", "personal", "local-only")
    assert code == 0 and "local-only" in out
    code, out = invoke("remove", "api")
    assert code == 0 and "removed api" in out


def test_auth_validates_the_token_shape(svc):
    invoke("add", "claude-max", "--kind", "subscription", "--provider", "anthropic", "--no-credential")
    code, out = invoke("auth", "claude-max", "--stdin", "--insecure-dev-store", input="not-a-token\n")
    assert code == 1 and "claude setup-token" in out
    code, out = invoke("auth", "claude-max", "--stdin", "--insecure-dev-store", input=OAUTH + "\n")
    assert code == 0 and "stored the credential" in out and OAUTH not in out


def test_no_vault_means_no_plain_file_unless_asked(svc):
    """Without a vault a subscription credential would land in a plain file: refused by default."""
    invoke("add", "claude-max", "--kind", "subscription", "--provider", "anthropic", "--no-credential")
    code, out = invoke("auth", "claude-max", "--stdin", input=OAUTH + "\n")
    assert code == 1 and "agentos vault init" in out and "--insecure-dev-store" in out and OAUTH not in out
    assert not svc.creds.dev_path.exists()
    code, out = invoke("add", "other", "--kind", "subscription", "--provider", "anthropic", "--stdin",
                       input=OAUTH + "\n")
    assert code == 1 and "--insecure-dev-store" in out and not svc.creds.dev_path.exists()
    assert "\u2014" not in out


def test_codex_auth_file(svc, tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"refresh_token": "r"}}))
    code, out = invoke("add", "codex-plus", "--kind", "subscription", "--provider", "openai", "--auth-file", str(auth),
                       "--insecure-dev-store")
    assert code == 0, out
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    code, out = invoke("auth", "codex-plus", "--auth-file", str(bad), "--insecure-dev-store")
    assert code == 1 and "codex login" in out


def test_child_token_refuses_the_owner_token(svc):
    code, out = invoke("child-token", "--stdin", input=OWNER_TOKEN + "\n")
    assert code == 1 and "owner token" in out


def test_test_command_and_tasks(svc):
    invoke("add", "claude-max", "--kind", "subscription", "--provider", "anthropic", "--stdin",
           "--insecure-dev-store", input=OAUTH)
    code, out = invoke("test", "claude-max")
    assert code == 0 and "2.1.317" in out and "state: ready" in out and "DEVELOPMENT FALLBACK" in out
    code, out = invoke("test", "claude-max", "--live", "--timeout", "30")
    assert code == 0, out
    assert "succeeded_unverified" in out and "reply: Done." in out
    code, out = invoke("tasks", "list")
    assert code == 0 and "agentos-test" in out
    tid = svc.list_tasks()[0]["id"]
    code, out = invoke("tasks", "tail", tid)
    assert code == 0 and "result" in out
    code, out = invoke("tasks", "show", tid)
    assert json.loads(out)["id"] == tid
    code, out = invoke("tasks", "cancel", tid)
    assert code == 0 and "succeeded_unverified" in out
    code, out = invoke("tasks", "resume", "bt_000000000000")
    assert code == 1 and "no brain task" in out


def test_doctor(svc):
    code, out = invoke("doctor")
    assert code == 0 and "claude: 2.1.317 (ok)" in out and "codex: 0.166.0 (ok)" in out


def test_brains_auth_stores_into_the_nodes_vault(tmp_path, monkeypatch):
    """`agentos brains auth --stdin` from the real CLI wiring: the key-file vault opens the way
    the daemon opens it, and the credential is sealed there, never in the plain dev file."""
    import asyncio

    from test_vault_support import make_vault

    from agentos import cli as agentos_cli
    from agentos.brains.credentials import DEV_FILE_NAME, brain_ref
    from agentos.brains.testing import make_service
    from agentos.core.config import NodeConfig, save_config

    home = tmp_path / "node"
    home.mkdir(mode=0o700)
    save_config(NodeConfig(home=home, mcp_token="node-token-for-cli-test"))
    make_vault(home)
    monkeypatch.setenv("AGENTOS_HOME", str(home))
    monkeypatch.setattr(brains_cli, "runtime_factory", agentos_cli.cli_runtime)
    rt = agentos_cli.cli_runtime()
    assert rt.has("vault")
    # the fake CLIs from the test helpers, on the same home and the same vault
    svc = make_service(tmp_path / "b", runtime=rt)
    assert svc.creds.backend == "vault"
    monkeypatch.setattr(brains_cli, "runtime_factory", lambda: rt)
    code, out = invoke("add", "claude-max", "--kind", "subscription", "--provider", "anthropic", "--no-credential")
    assert code == 0, out
    code, out = invoke("auth", "claude-max", "--stdin", input=OAUTH + "\n")
    assert code == 0 and "stored the credential for claude-max (vault)" in out and OAUTH not in out
    assert not svc.creds.dev_path.exists() and not (home / "secrets" / DEV_FILE_NAME).exists()
    vault = rt.get("vault")
    assert asyncio.run(vault.get_secret(brain_ref("claude-max"), purpose="test")) == OAUTH
    assert OAUTH.encode() not in (home / "vault.db").read_bytes()


def test_cli_runtime_without_a_vault_has_none(tmp_path, monkeypatch):
    from agentos import cli as agentos_cli
    from agentos.core.config import NodeConfig, save_config

    home = tmp_path / "node"
    home.mkdir(mode=0o700)
    save_config(NodeConfig(home=home, mcp_token="node-token-for-cli-test"))
    monkeypatch.setenv("AGENTOS_HOME", str(home))
    assert not agentos_cli.cli_runtime().has("vault")
    assert brains_cli.runtime_factory is not None  # wired by agentos.cli at import
