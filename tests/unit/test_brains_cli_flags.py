"""Command builders use only pinned flags; CLI drift is detected and reported clearly."""

import json
import re
import sys
from pathlib import Path

import pytest
from testkit.brains import CHILD_TOKEN, OAUTH

from agentos.brains import adapters, cliprobe
from agentos.brains.models import BrainSpec
from agentos.brains.settings import BrainsSettings
from agentos.dev import fakecli

FLAG_RE = re.compile(r"^--?[A-Za-z]")


def flags_in(argv):
    return {a for a in argv[1:] if FLAG_RE.match(a)}


def pinned(runner):
    pin = cliprobe.PINNED[runner]
    allowed = set(pin["help_flags"]) | set(pin.get("hidden_flags", {})) | set(pin.get("resume_flags", []))
    if runner == "claude":
        allowed.add("-p")  # short form of --print
    if runner == "chat":
        allowed |= {"-I", "-B"}  # python's own, in front of the script (cliprobe.CHAT_ARGS)
    return allowed


def plan(spec, tmp, scrub=False, **kw):
    s = BrainsSettings(bin_dirs=[], scrub_subprocess_env=scrub)
    args = {"bin_path": "/x/" + spec.runner.value, "task_id": "bt_000000000001", "work": tmp / "work", "home": tmp / "home",
                "user": None, "credential": OAUTH if spec.credential != "codex_auth" else '{"tokens":{}}',
                "mcp_url": "http://127.0.0.1:7400/mcp", "child_token": CHILD_TOKEN, "max_turns": 7, "max_usd": 1.5,
                "resume_session": None}
    args.update(kw)
    return adapters.plan(spec, s, **args)


@pytest.mark.parametrize("kind,provider", [("subscription", "anthropic"), ("metered", "anthropic"),
                                           ("subscription", "openai"), ("metered", "openai")])
@pytest.mark.parametrize("resume", [None, "sess-1"])
def test_builders_use_only_pinned_flags(tmp_path, kind, provider, resume):
    spec = BrainSpec.template("b", kind, provider, model="m")
    p = plan(spec, tmp_path, resume_session=resume)
    assert flags_in(p.argv) <= pinned(spec.runner.value), flags_in(p.argv) - pinned(spec.runner.value)


def test_chat_builder_uses_only_pinned_flags(tmp_path):
    spec = BrainSpec.template("c", "metered", "openai-compatible", model="m", base_url="https://api.example.com/v1",
                              price_in_per_mtok=0.1, price_out_per_mtok=0.4)
    p = plan(spec, tmp_path)
    assert flags_in(p.argv) <= pinned("chat"), flags_in(p.argv) - pinned("chat")


def test_local_builders_use_only_pinned_flags(tmp_path):
    for spec in (BrainSpec.template("o", "local", "ollama"),
                 BrainSpec.template("l", "local", "openai-compatible", base_url="http://192.168.1.5:8000/v1")):
        p = plan(spec, tmp_path, credential=None)
        assert flags_in(p.argv) <= pinned("codex")


def test_claude_plan_keeps_secrets_out_of_argv_and_files(tmp_path):
    spec = BrainSpec.template("claude-max", "subscription", "anthropic")
    p = plan(spec, tmp_path)
    blob = json.dumps(p.argv) + json.dumps(p.env) + json.dumps(p.files)
    assert OAUTH not in blob and CHILD_TOKEN not in blob
    assert p.secret_env == {"CLAUDE_CODE_OAUTH_TOKEN": OAUTH, "AGENTOS_CHILD_TOKEN": CHILD_TOKEN}
    _mcp_file, (content, mode) = next(iter(p.files.items()))
    assert mode == 0o600 and "${AGENTOS_CHILD_TOKEN}" in content
    assert "--strict-mcp-config" in p.argv and "--max-turns" in p.argv
    assert p.argv[p.argv.index("--max-turns") + 1] == "7"
    assert "--max-budget-usd" not in p.argv  # subscription: no real money
    assert p.env["DISABLE_AUTOUPDATER"] == "1"


def test_claude_with_env_scrub_writes_the_child_token_to_its_private_config(tmp_path, monkeypatch):
    # verified on the node: with CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1 claude does not expand
    # ${VAR} in --mcp-config (the header arrives as a bare "Bearer")
    monkeypatch.setattr(adapters.shutil, "which", lambda name: "/usr/bin/bwrap")
    spec = BrainSpec.template("claude-max", "subscription", "anthropic")
    p = plan(spec, tmp_path, scrub=True)
    assert p.env["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] == "1"
    assert p.secret_env == {"CLAUDE_CODE_OAUTH_TOKEN": OAUTH}
    (content, mode), = p.files.values()
    assert mode == 0o600 and f"Bearer {CHILD_TOKEN}" in content
    assert CHILD_TOKEN not in json.dumps(p.argv) + json.dumps(p.env)


def test_claude_metered_uses_api_key_and_budget(tmp_path):
    spec = BrainSpec.template("api", "metered", "anthropic")
    p = plan(spec, tmp_path)
    assert "ANTHROPIC_API_KEY" in p.secret_env and "CLAUDE_CODE_OAUTH_TOKEN" not in p.secret_env
    assert p.argv[p.argv.index("--max-budget-usd") + 1] == "1.50"


def test_claude_without_mcp_drops_agentos_tools(tmp_path):
    spec = BrainSpec.template("claude-max", "subscription", "anthropic")
    p = plan(spec, tmp_path, child_token=None)
    tools = p.argv[p.argv.index("--allowedTools") + 1]
    assert "mcp__agentos" not in tools and "--mcp-config" not in p.argv and not p.files


def test_codex_plans(tmp_path):
    sub = BrainSpec.template("codex-plus", "subscription", "openai")
    p = plan(sub, tmp_path, credential='{"tokens":{"refresh_token":"r"}}')
    files = {Path(k).name: v for k, v in p.files.items()}
    assert files["auth.json"][1] == 0o600
    cfg = files["config.toml"][0]
    assert 'bearer_token_env_var = "AGENTOS_CHILD_TOKEN"' in cfg and CHILD_TOKEN not in cfg
    assert 'inherit = "core"' in cfg
    assert 'set = { AGENTOS_BRAIN_TASK = "' in cfg  # excluded by AGENTOS_*, so it has to be set again
    assert p.argv[-1] == "-"  # prompt on stdin
    metered = BrainSpec.template("oa", "metered", "openai")
    assert "CODEX_API_KEY" in plan(metered, tmp_path).secret_env
    local = BrainSpec.template("lan", "local", "openai-compatible", base_url="http://10.0.0.2:8000/v1")
    cfg = {Path(k).name: v for k, v in plan(local, tmp_path, credential=None).files.items()}["config.toml"][0]
    assert cfg.startswith('model_provider = "agentos_local"') and 'wire_api = "responses"' in cfg
    resume = plan(sub, tmp_path, resume_session="t-1").argv
    assert resume[1:3] == ["exec", "resume"] and "t-1" in resume and "--cd" not in resume


def test_local_brain_must_be_on_the_owners_network():
    with pytest.raises(ValueError, match="not a local brain"):
        BrainSpec.template("x", "local", "openai-compatible", base_url="https://api.example.com/v1")
    for ok in ("http://127.0.0.1:11434/v1", "http://192.168.1.9:8000/v1", "http://100.80.1.2:8000/v1",
               "http://gpu-box.local:8000/v1"):
        BrainSpec.template("x", "local", "openai-compatible", base_url=ok)


def test_probe_runs_the_chat_runner_under_the_python_it_will_use(tmp_path):
    cliprobe.clear_cache()
    info = cliprobe.probe("chat", explicit=sys.executable, use_cache=False)
    assert info.ok, info.problems
    assert info.version == cliprobe.PINNED["chat"]["verified"] and info.path == sys.executable
    missing = cliprobe.probe("chat", explicit=str(tmp_path / "nope"), use_cache=False)
    assert not missing.ok and missing.path is None


def test_probe_fake_cli_ok(tmp_path):
    cliprobe.clear_cache()
    bins = fakecli.install(tmp_path / "bin")
    for runner in ("claude", "codex"):
        info = cliprobe.probe(runner, bin_dirs=[str(tmp_path / "bin")], use_cache=False)
        assert info.ok, info.problems
        assert info.version == cliprobe.PINNED[runner]["verified"]
        assert info.path == bins[runner]


def test_probe_detects_missing_help_flag(tmp_path):
    cliprobe.clear_cache()
    fakecli.install(tmp_path / "bin")
    help_text = fakecli.CLAUDE_HELP.replace("--mcp-config <configs...>", "--mcp-servers <configs...>")
    (tmp_path / "bin" / "claude.help").write_text(help_text)
    info = cliprobe.probe("claude", bin_dirs=[str(tmp_path / "bin")], use_cache=False)
    assert not info.ok
    reason = info.reason()
    assert "--mcp-config" in reason and "2.1.317" in reason and "CLAUDE_CODE_VERSION=2.1.317" in reason


def test_probe_detects_removed_hidden_flag(tmp_path):
    cliprobe.clear_cache()
    fakecli.install(tmp_path / "bin")
    (tmp_path / "bin" / "claude.no-max-turns").write_text("")
    info = cliprobe.probe("claude", bin_dirs=[str(tmp_path / "bin")], use_cache=False)
    assert not info.ok and "--max-turns" in info.reason()


def test_probe_detects_codex_resume_drift_and_missing_cli(tmp_path):
    cliprobe.clear_cache()
    fakecli.install(tmp_path / "bin")
    (tmp_path / "bin" / "codex-resume.help").write_text("Usage: codex exec resume [OPTIONS]\n  --last\n")
    info = cliprobe.probe("codex", bin_dirs=[str(tmp_path / "bin")], use_cache=False)
    assert not info.ok and "exec resume --json" in info.reason()
    missing = cliprobe.probe("claude", explicit=str(tmp_path / "nope"), use_cache=False)
    assert not missing.ok and "not installed" in missing.reason()
