"""Build the exact command, environment and private files for one brain attempt.

Only flags pinned in cliprobe.PINNED appear here. The prompt goes on stdin (from a 600
file), never in argv, so it does not show up in `ps`. Secrets are returned separately as
`secret_env` and reach only the brain process (through the shim's stdin), never argv, never
launch.json.

The environment is built from an allowlist, never inherited, so whatever the daemon has in
its own environment (the owner token, other API keys) cannot leak into a brain.

How each brain kind authenticates (all bring-your-own, for the owner's own use):
  claude subscription  CLAUDE_CODE_OAUTH_TOKEN  (from `claude setup-token`, about a year)
  claude metered       ANTHROPIC_API_KEY
  codex subscription   $CODEX_HOME/auth.json    (the owner's ChatGPT login, chmod 600, written
                                                 into the task's private home, read back after
                                                 the run because Codex refreshes it)
  codex metered        CODEX_API_KEY
  codex local          a Responses-API model server on the owner's network (Ollama via --oss)
  chat metered         OPENAJAR_CHAT_API_KEY, for an OpenAI-compatible chat-completions endpoint
                       (base_url and key supplied by the owner; openajar.brains.chatcli)
The openajar MCP callback uses the task's CHILD grant token (never the owner token). Codex
reads it from OPENAJAR_CHILD_TOKEN through bearer_token_env_var. Claude expands
${OPENAJAR_CHILD_TOKEN} in the --mcp-config file, except when CLAUDE_CODE_SUBPROCESS_ENV_SCRUB
is on: then Claude scrubs its environment before expanding the config (verified on the node
with 2.1.317: the header arrives as a bare "Bearer"), so the child token is written into the
brain's private chmod 600 MCP config instead and deleted when the task ends. The scrub is
worth it: it keeps the OAuth token, the crown jewel, out of the brain's own Bash tool.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openajar.brains import chatcli
from openajar.brains.cliprobe import CHAT_ARGS
from openajar.brains.models import BrainKind, BrainSpec, Runner
from openajar.brains.settings import BrainsSettings

CHILD_TOKEN_ENV = "OPENAJAR_CHILD_TOKEN"
BRAIN_TASK_ENV = "OPENAJAR_BRAIN_TASK"  # says "a brain started this"; `openajar plugins` reads it
MCP_SERVER_NAME = "openajar"


@dataclass
class AttemptPlan:
    argv: list[str]
    env: dict[str, str]                       # non-secret
    secret_env: dict[str, str] = field(default_factory=dict)
    files: dict[str, tuple[str, int]] = field(default_factory=dict)  # path -> (content, mode), brain-owned
    reports_cost: bool = False                # the CLI reports a dollar cost we can trust


def system_note(task_id: str, work_dir: Path, has_mcp: bool) -> str:
    lines = [
        f"You are running headless on an openajar node as brain task {task_id}.",
        f"Your working directory is {work_dir}. Keep your work inside it.",
        ("Nobody is watching live. Do not wait for answers. When you finish, end with a short "
         "summary: what you did, what is verified, and what is left."),
    ]
    if has_mcp:
        lines.append("openajar tools (lanes, browser) are available through the openajar MCP server, "
                     "limited to what this task's grant allows.")
    return " ".join(lines)


def base_env(*, bin_path: str, settings: BrainsSettings, home: Path, user: str | None,
             task_id: str) -> dict[str, str]:
    path = ":".join(dict.fromkeys([str(Path(bin_path).parent), *settings.bin_dirs,
                                   "/usr/local/bin", "/usr/bin", "/bin"]))
    env = {
        "PATH": path, "HOME": str(home), "TMPDIR": str(home / "tmp"), "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8", "TERM": "dumb", "NO_COLOR": "1", "SHELL": "/bin/bash",
        "DISABLE_AUTOUPDATER": "1", BRAIN_TASK_ENV: task_id,
    }
    if user:
        env["USER"] = env["LOGNAME"] = user
    return env


def plan_claude(spec: BrainSpec, settings: BrainsSettings, *, bin_path: str, task_id: str, work: Path,
                home: Path, user: str | None, credential: str | None, mcp_url: str | None,
                child_token: str | None, max_turns: int, max_usd: float | None,
                resume_session: str | None, no_tools: bool = False) -> AttemptPlan:
    env = base_env(bin_path=bin_path, settings=settings, home=home, user=user, task_id=task_id)
    env["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
    if settings.scrub_subprocess_env and shutil.which("bwrap"):
        # Claude Code's own subprocess sandbox: keeps the token out of its Bash tool's env
        env["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] = "1"
    secret: dict[str, str] = {}
    if credential:
        key = "CLAUDE_CODE_OAUTH_TOKEN" if spec.kind == BrainKind.SUBSCRIPTION else "ANTHROPIC_API_KEY"
        secret[key] = credential
    tools = spec.allowed_tools if spec.allowed_tools is not None else settings.allowed_tools
    if not (mcp_url and child_token):
        tools = [t for t in tools if not t.startswith("mcp__")]
    # --tools "" takes the built-in tools away, which an empty allow list alone does not promise
    tool_args = ["--tools", ""] if no_tools else ["--allowedTools", ",".join(tools)]
    argv = [bin_path, "-p", "--output-format", "stream-json", "--verbose",
            "--max-turns", str(max_turns),
            "--permission-mode", spec.permission_mode or settings.permission_mode,
            *tool_args,
            "--setting-sources", settings.setting_sources,
            "--strict-mcp-config",
            "--append-system-prompt", system_note(task_id, work, bool(mcp_url and child_token))]
    files: dict[str, tuple[str, int]] = {}
    if mcp_url and child_token:
        scrubbed = env.get("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB") == "1"
        bearer = child_token if scrubbed else "${" + CHILD_TOKEN_ENV + "}"
        cfg = {"mcpServers": {MCP_SERVER_NAME: {
            "type": "http", "url": mcp_url, "headers": {"Authorization": f"Bearer {bearer}"}}}}
        mcp_path = home / "openajar-mcp.json"
        files[str(mcp_path)] = (json.dumps(cfg, indent=2), 0o600)
        argv += ["--mcp-config", str(mcp_path)]
        if not scrubbed:
            secret[CHILD_TOKEN_ENV] = child_token
    if spec.model:
        argv += ["--model", spec.model]
    if spec.effort:
        argv += ["--effort", spec.effort]
    if spec.kind == BrainKind.METERED and max_usd is not None:
        argv += ["--max-budget-usd", f"{max_usd:.2f}"]
    if resume_session:
        argv += ["--resume", resume_session]
    return AttemptPlan(argv=argv, env=env, secret_env=secret, files=files,
                       reports_cost=spec.kind == BrainKind.METERED)


def _toml_str(s: str) -> str:
    return json.dumps(s)  # a JSON string is a valid TOML basic string


def plan_codex(spec: BrainSpec, settings: BrainsSettings, *, bin_path: str, task_id: str, work: Path,
               home: Path, user: str | None, credential: str | None, mcp_url: str | None,
               child_token: str | None, resume_session: str | None) -> AttemptPlan:
    env = base_env(bin_path=bin_path, settings=settings, home=home, user=user, task_id=task_id)
    codex_home = home / ".codex"
    env["CODEX_HOME"] = str(codex_home)
    secret: dict[str, str] = {}
    files: dict[str, tuple[str, int]] = {}
    cfg = [f"# written by openajar for brain task {task_id}",
           # brain tool commands get only core env vars, never tokens or keys
           "[shell_environment_policy]", 'inherit = "core"',
           'exclude = ["*TOKEN*", "*KEY*", "*SECRET*", "OPENAJAR_*", "CODEX_*"]',
           # `set` comes after `exclude`, so the one variable that is no secret still reaches them
           f"set = {{ {BRAIN_TASK_ENV} = {_toml_str(task_id)} }}", ""]
    if spec.kind == BrainKind.SUBSCRIPTION and credential:
        files[str(codex_home / "auth.json")] = (credential, 0o600)
    elif spec.kind == BrainKind.METERED and credential:
        secret["CODEX_API_KEY"] = credential
    local_args: list[str] = []
    if spec.kind == BrainKind.LOCAL:
        if spec.provider == "ollama" and not spec.base_url:
            local_args = ["--oss", "--local-provider", "ollama"]
        else:
            cfg = [f"model_provider = {_toml_str('openajar_local')}", *cfg,
                   "[model_providers.openajar_local]", f"name = {_toml_str('openajar local ' + spec.name)}",
                   f"base_url = {_toml_str(spec.base_url or '')}", f"wire_api = {_toml_str(spec.wire_api)}"]
            if credential:
                cfg.append(f"env_key = {_toml_str('OPENAJAR_LOCAL_API_KEY')}")
                secret["OPENAJAR_LOCAL_API_KEY"] = credential
            cfg.append("")
    if mcp_url and child_token:
        cfg += [f"[mcp_servers.{MCP_SERVER_NAME}]", f"url = {_toml_str(mcp_url)}",
                f"bearer_token_env_var = {_toml_str(CHILD_TOKEN_ENV)}", "startup_timeout_sec = 20", ""]
        secret[CHILD_TOKEN_ENV] = child_token
    files[str(codex_home / "config.toml")] = ("\n".join(cfg) + "\n", 0o600)
    model = ["--model", spec.model] if spec.model else []
    sandbox = settings.codex_sandbox
    if resume_session:
        argv = [bin_path, "exec", "resume", "--json", "--skip-git-repo-check",
                "--config", f"sandbox_mode={_toml_str(sandbox)}", *model, resume_session, "-"]
    else:
        argv = [bin_path, "exec", "--json", "--skip-git-repo-check", "--cd", str(work),
                "--sandbox", sandbox, *local_args, *model, "-"]
    return AttemptPlan(argv=argv, env=env, secret_env=secret, files=files)


def plan_chat(spec: BrainSpec, settings: BrainsSettings, *, bin_path: str, task_id: str, home: Path,
              user: str | None, credential: str | None, max_usd: float | None) -> AttemptPlan:
    env = base_env(bin_path=bin_path, settings=settings, home=home, user=user, task_id=task_id)
    secret = {chatcli.API_KEY_ENV: credential} if credential else {}
    note = (f"You are running headless on an openajar node as brain task {task_id}. Nobody is watching live and "
            "you have no tools: answer from this message alone, and say what you could not verify.")
    argv = [bin_path, *CHAT_ARGS, "--base-url", spec.base_url or "", "--model", spec.model or "", "--system", note,
            "--max-output-tokens", str(settings.chat_max_output_tokens),
            "--timeout", str(settings.chat_timeout_seconds),
            "--price-in", str(spec.price_in_per_mtok or 0), "--price-out", str(spec.price_out_per_mtok or 0)]
    if max_usd is not None:
        argv += ["--max-usd", f"{max_usd:.6f}"]
    return AttemptPlan(argv=argv, env=env, secret_env=secret)


def plan(spec: BrainSpec, settings: BrainsSettings, **kw: Any) -> AttemptPlan:
    if spec.runner == Runner.SYSTEMONE:  # the router keeps tasks away; nothing below may start one
        raise ValueError(f"{spec.name} answers typed questions in process and has no process to start")
    if spec.runner == Runner.CLAUDE:
        return plan_claude(spec, settings, **kw)
    if spec.runner == Runner.CHAT:
        # one call with no tools: no MCP callback, so the child grant token stays out of its reach,
        # and no session to resume, since a fallback or a retry is a new call
        for unused in ("work", "mcp_url", "child_token", "max_turns", "resume_session", "no_tools"):
            kw.pop(unused, None)
        return plan_chat(spec, settings, **kw)
    if kw.pop("no_tools", False):
        raise ValueError(f"{spec.name} cannot run with no tools: codex has no way to promise it")
    kw.pop("max_turns", None)
    kw.pop("max_usd", None)
    return plan_codex(spec, settings, **kw)
