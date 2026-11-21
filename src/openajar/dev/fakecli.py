"""Fake `claude` and `codex` CLIs for tests. Standard library only.

They mimic the real stream formats captured on the node (claude 2.1.317, codex-cli 0.166.0),
including the unauthenticated output, 429 and usage-limit messages, expired credentials,
crashes, hangs, runaway loops and huge output. Real authenticated runs need the owner's
subscription, so everything else is exercised against these.

The scenario comes from the prompt (which arrives on stdin, like the real runner sends it):
`FAKE:<scenario>[:<number>]`, for example `FAKE:rate_limit` or `FAKE:loop:500`.
Every run writes `fake-dump.json` in its working directory (argv, env, prompt, the MCP config
it was given) so tests can check what really reached the brain.

    python -m openajar.dev.fakecli --as claude --dir DIR [claude args...]
    install(dir) -> {"claude": path, "codex": path}   writes executable wrappers
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import time
import uuid
from pathlib import Path

CLAUDE_VERSION = "2.1.317 (Claude Code)"
CODEX_VERSION = "codex-cli 0.166.0"

CLAUDE_HELP = """Usage: claude [options] [command] [prompt]

Options:
  --allowedTools, --allowed-tools <tools...>
  --append-system-prompt <prompt>       Append a system prompt
  --dangerously-skip-permissions        Bypass all permission checks.
  --effort <level>                      Effort level for the current session
  --max-budget-usd <amount>             Maximum dollar amount to spend on API calls
  --mcp-config <configs...>             Load MCP servers from JSON files or strings
  --model <model>                       Model for the current session.
  --output-format <format>              (choices: "text", "json", "stream-json")
  --permission-mode <mode>              (choices: "acceptEdits", "auto", "bypassPermissions",
                                        "manual", "dontAsk", "plan")
  -p, --print                           Print response and exit
  -r, --resume [value]                  Resume a conversation by session ID
  --setting-sources <sources>           Comma-separated list of setting sources
  --strict-mcp-config                   Only use MCP servers from --mcp-config
  --tools <tools...>                    Specify the list of available tools
  --verbose                             Override verbose mode setting from config
  -v, --version                         Output the version number
"""

CODEX_EXEC_HELP = """Run Codex non-interactively

Usage: codex exec [OPTIONS] [PROMPT]

Options:
  -c, --config <key=value>
  -m, --model <MODEL>
      --oss
      --local-provider <OSS_PROVIDER>
  -s, --sandbox <SANDBOX_MODE>   [possible values: read-only, workspace-write, danger-full-access]
  -C, --cd <DIR>
      --skip-git-repo-check
      --ephemeral
      --json
  -o, --output-last-message <FILE>
"""

CODEX_RESUME_HELP = """Resume a previous session by id or pick the most recent with --last
Usage: codex exec resume [OPTIONS] [SESSION_ID] [PROMPT]
Options:
  -c, --config <key=value>
  -m, --model <MODEL>
      --skip-git-repo-check
      --json
"""


def out(obj) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _scenario(prompt: str, who: str) -> tuple[str, float | None]:
    """`FAKE:rate_limit` applies to both CLIs; `FAKE:claude_rate_limit` only to claude (the
    other CLI runs the ok path). A `fake-recover` file in the work dir forces ok."""
    m = re.search(r"FAKE:([a-z_]+)(?::([0-9.]+))?", prompt)
    if not m or Path("fake-recover").exists():
        return "ok", None
    scen = m.group(1)
    for runner in ("claude", "codex"):
        if scen.startswith(runner + "_"):
            scen = scen[len(runner) + 1:] if runner == who else "ok"
    return scen, float(m.group(2)) if m.group(2) else None


def _opt(args: list[str], name: str) -> str | None:
    if name in args:
        i = args.index(name)
        return args[i + 1] if i + 1 < len(args) else None
    return None


def _dump(extra: dict) -> None:
    data = {"argv": sys.argv, "cwd": os.getcwd(), "env": dict(os.environ), "uid": os.getuid(), **extra}
    Path("fake-dump.json").write_text(json.dumps(data, indent=2))


def _ignore_signals() -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)


def _sleep_forever() -> None:
    while True:
        time.sleep(1)


# ----------------------------------------------------------------- claude
def _usage(i: int, o: int) -> dict:
    return {"input_tokens": i, "output_tokens": o, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}


def _assistant(sid: str, text: str | None = None, *, mid: str | None = None, usage=None, tool=None,
               error: str | None = None, status: int | None = None, model: str = "claude-sonnet-5-5") -> dict:
    content = []
    if text is not None:
        content.append({"type": "text", "text": text})
    if tool:
        content.append({"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:8], "name": tool[0], "input": tool[1]})
    msg = {"id": mid or str(uuid.uuid4()), "model": model if not error else "<synthetic>", "role": "assistant",
           "type": "message", "usage": usage or _usage(0, 0), "content": content}
    obj = {"type": "assistant", "message": msg, "parent_tool_use_id": None, "session_id": sid,
           "uuid": str(uuid.uuid4())}
    if error:
        obj.update(error=error, is_api_error_message=True)
        if status:
            obj["api_error_status"] = status
    return obj


def _result(sid: str, text: str, *, is_error: bool, turns: int, cost: float = 0.0, usage=None,
            status: int | None = None, subtype: str = "success", terminal: str | None = None) -> dict:
    return {"type": "result", "subtype": subtype, "is_error": is_error, "num_turns": turns, "result": text,
            "session_id": sid, "total_cost_usd": cost, "usage": usage or _usage(0, 0),
            "api_error_status": status, "terminal_reason": terminal or ("api_error" if status else "completed"),
            "duration_ms": 10, "uuid": str(uuid.uuid4())}


def _claude_flags(args: list[str], fdir: Path) -> int | None:
    """--version, --help and a bad --max-turns answer and end, as the real CLI does (None: go on)."""
    if "--version" in args or "-v" in args:
        print(CLAUDE_VERSION)
        return 0
    if "--help" in args or "-h" in args:
        override = fdir / "claude.help"
        print(override.read_text() if override.exists() else CLAUDE_HELP)
        return 0
    mt = _opt(args, "--max-turns")
    if mt is not None:
        if (fdir / "claude.no-max-turns").exists():
            print("error: unknown option '--max-turns'", file=sys.stderr)
            return 1
        if not mt.isdigit():
            print(f"error: option '--max-turns <turns>' argument '{mt}' is invalid. must be a number",
                  file=sys.stderr)
            return 1
    return None


def _retries(sid: str, status: int, error: str, delay_ms: int, attempts: tuple[int, ...]) -> None:
    for a in attempts:
        out({"type": "system", "subtype": "api_retry", "attempt": a, "max_retries": 10, "retry_delay_ms": delay_ms,
             "error_status": status, "error": error, "session_id": sid})


def _claude_api_failure(sid: str, scen: str, n: float | None) -> int | None:
    """The API's own failures, worded as the real CLI words them (None: not one of these)."""
    if scen == "auth_expired":
        _retries(sid, 401, "authentication_failed", 500, (1, 2))
        msg = "Failed to authenticate. API Error: 401 OAuth access token has expired."
        out(_assistant(sid, msg, error="authentication_failed", status=401))
        out(_result(sid, msg, is_error=True, turns=1, status=401))
        return 1
    if scen == "rate_limit":
        _retries(sid, 429, "rate_limit", 800, (1, 2))
        msg = ('API Error: 429 {"type":"error","error":{"type":"rate_limit_error",'
               '"message":"Number of request tokens has exceeded your rate limit."}}')
        out(_assistant(sid, msg, error="rate_limit", status=429))
        out(_result(sid, msg, is_error=True, turns=1, status=429))
        return 1
    if scen == "usage_limit":
        resets = int(time.time()) + int(n or 3600)
        out({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": resets,
             "rateLimitType": "five_hour", "utilization": 1.0}, "session_id": sid})
        msg = "You've hit your limit · resets 3pm (UTC)"
        out(_assistant(sid, msg, error="rate_limit", status=429))
        out(_result(sid, msg, is_error=True, turns=1, status=429))
        return 1
    if scen == "server_error":
        _retries(sid, 529, "server_error", 500, (1,))
        msg = 'API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'
        out(_assistant(sid, msg, error="server_error", status=529))
        out(_result(sid, msg, is_error=True, turns=1, status=529))
        return 1
    if scen == "error":
        out(_result(sid, "The task could not be completed: tests keep failing.", is_error=True, turns=2, cost=0.003,
                    subtype="error_during_execution", terminal="error"))
        return 1
    return None


def _claude_misbehaves(sid: str, scen: str) -> None:
    """A CLI that never ends well: it escapes, hangs, ignores signals, crashes or runs away. These
    do not return; any other scenario does, at once."""
    if scen == "spawn_orphan":  # a tool process that escapes the process group
        import subprocess
        child = subprocess.Popen(["sleep", "300"], start_new_session=True)
        Path("orphan.pid").write_text(str(child.pid))
        out(_assistant(sid, f"spawned {child.pid}", usage=_usage(1, 1)))
        _sleep_forever()
    if scen == "hang":
        _sleep_forever()
    if scen == "ignore_term":
        _ignore_signals()  # before it says so: whoever reads this line can rely on it
        out(_assistant(sid, "working, and ignoring polite signals", usage=_usage(10, 5)))
        _sleep_forever()
    if scen == "crash":
        out(_assistant(sid, "starting", usage=_usage(100, 20)))
        sys.stdout.flush()
        os._exit(3)
    if scen == "loop":  # a runaway CLI that ignores --max-turns
        i = 0
        while True:
            i += 1
            out(_assistant(sid, f"step {i}", usage=_usage(50, 10), tool=("Bash", {"command": "true"})))
            time.sleep(0.01)
    if scen == "tokens":  # runaway token use
        while True:
            out(_assistant(sid, "thinking very hard", usage=_usage(400_000, 100_000)))
            time.sleep(0.05)


def _claude_long(sid: str, scen: str, n: float | None, max_turns: int) -> int | None:
    """Long output, and a session of many turns that honours --max-turns (None: neither)."""
    if scen in ("huge", "giantline"):
        mb = int(n or 5)
        if scen == "giantline":
            sys.stdout.write(json.dumps(_assistant(sid, "x" * (mb * 1_000_000))) + "\n")
        else:
            chunk = "y" * 100_000
            for _ in range(mb * 10):  # one long streamed message: the same id throughout
                out(_assistant(sid, chunk, mid="msg_huge", usage=_usage(1, 1)))
        out(_result(sid, "done with huge output", is_error=False, turns=2, cost=0.01))
        return 0
    if scen == "turns":  # honors --max-turns like the real CLI
        total = int(n or 5)
        for i in range(1, min(total, max_turns) + 1):
            out(_assistant(sid, f"turn {i}", usage=_usage(100, 20), tool=("Read", {"file_path": "x"})))
        if total > max_turns:
            out(_result(sid, "", is_error=True, turns=max_turns, subtype="error_max_turns", terminal="max_turns"))
            return 1
        out(_result(sid, "all turns done", is_error=False, turns=total, cost=0.02))
        return 0
    return None


def _claude_session(sid: str, scen: str, n: float | None) -> int:
    """ok (and slow, and leak): a realistic short session."""
    steps = int(n or 0) if scen == "slow" else 0
    if scen == "leak":
        secret = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY") or ""
        child = os.environ.get("OPENAJAR_CHILD_TOKEN", "")
        out(_assistant(sid, f"my env says CLAUDE_CODE_OAUTH_TOKEN={secret} and child={child}",
                       usage=_usage(10, 10)))
    out(_assistant(sid, "Looking at the repository.", mid="msg_1", usage=_usage(1200, 40)))
    out(_assistant(sid, None, mid="msg_1", usage=_usage(1200, 60), tool=("Bash", {"command": "ls"})))
    out({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_x", "content": "README.md\nsrc"}]}, "session_id": sid})
    for i in range(steps):
        time.sleep(1)
        out(_assistant(sid, f"slow step {i + 1}", usage=_usage(100, 10)))
    out(_assistant(sid, "Done. Summary: listed the files.", mid="msg_2", usage=_usage(1300, 80)))
    out(_result(sid, "Done. Summary: listed the files.", is_error=False, turns=2 + steps, cost=0.0123,
                usage=_usage(2500 + 100 * steps, 140 + 10 * steps)))
    return 0


def _read_files(prompt: str) -> dict[str, str]:
    """What this brain process can read: each path after READ: in the prompt."""
    results = {}
    for path in re.findall(r"READ:(\S+)", prompt):
        try:
            Path(path).read_bytes()
            results[path] = "readable"
        except OSError as exc:
            results[path] = type(exc).__name__
    return results


def claude(args: list[str], fdir: Path) -> int:
    ended = _claude_flags(args, fdir)
    if ended is not None:
        return ended
    mt = _opt(args, "--max-turns")
    max_turns = int(mt) if mt else 10_000
    prompt = sys.stdin.read() if not sys.stdin.isatty() else ""
    mcp_path = _opt(args, "--mcp-config")
    mcp = None
    if mcp_path and os.path.exists(mcp_path):
        mcp = Path(mcp_path).read_text()
    _dump({"prompt": prompt, "mcp_config": mcp, "runner": "claude"})
    sid = _opt(args, "--resume") or str(uuid.uuid4())
    out({"type": "system", "subtype": "init", "cwd": os.getcwd(), "session_id": sid, "tools": ["Bash", "Read"],
         "mcp_servers": [{"name": "openajar", "status": "connected"}] if mcp else [], "model": "claude-sonnet-5-5",
         "permissionMode": _opt(args, "--permission-mode"), "apiKeySource": "none",
         "claude_code_version": CLAUDE_VERSION.split()[0]})
    if not (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")):
        # the real unauthenticated output, captured on the node
        out(_assistant(sid, "Not logged in · Please run /login", error="authentication_failed"))
        out(_result(sid, "Not logged in · Please run /login", is_error=True, turns=1, terminal="api_error"))
        return 1
    said = re.search(r"FAKE_SAY:(\S+)", prompt)  # answer with exactly this and stop
    if said:
        out(_result(sid, said.group(1), is_error=False, turns=1, cost=0.0021, usage=_usage(300, 20)))
        return 0
    scen, n = _scenario(prompt, "claude")
    failed = _claude_api_failure(sid, scen, n)
    if failed is not None:
        return failed
    if scen == "readfile":  # then the ok session
        _dump({"prompt": prompt, "mcp_config": mcp, "runner": "claude", "read": _read_files(prompt)})
    _claude_misbehaves(sid, scen)
    long = _claude_long(sid, scen, n, max_turns)
    if long is not None:
        return long
    return _claude_session(sid, scen, n)


# ----------------------------------------------------------------- codex
def codex(args: list[str], fdir: Path) -> int:
    if args[:1] == ["--version"] or args[:1] == ["-V"]:
        print(CODEX_VERSION)
        return 0
    if args[:1] != ["exec"]:
        print("fake codex only supports exec", file=sys.stderr)
        return 2
    rest = args[1:]
    resume = rest[:1] == ["resume"]
    if "--help" in rest or "-h" in rest:
        override = fdir / ("codex-resume.help" if resume else "codex.help")
        print(override.read_text() if override.exists() else (CODEX_RESUME_HELP if resume else CODEX_EXEC_HELP))
        return 0
    prompt = sys.stdin.read() if (rest and rest[-1] == "-") else (rest[-1] if rest else "")
    print("Reading additional input from stdin...", file=sys.stderr)
    home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    cfg = (home / "config.toml").read_text() if (home / "config.toml").exists() else None
    auth_path = home / "auth.json"
    _dump({"prompt": prompt, "codex_config": cfg, "auth_json_present": auth_path.exists(), "runner": "codex"})
    tid = rest[1] if resume and len(rest) > 2 else str(uuid.uuid4())
    out({"type": "thread.started", "thread_id": tid})
    out({"type": "turn.started"})
    authed = auth_path.exists() or os.environ.get("CODEX_API_KEY") or "--oss" in rest or \
        (cfg and "model_provider" in cfg)
    if not authed:
        msg = ("unexpected status 401 Unauthorized: Missing bearer or basic authentication in header, "
               "url: https://api.openai.com/v1/responses")
        for a in range(1, 6):
            out({"type": "error", "message": f"Reconnecting... {a}/5 ({msg})"})
        out({"type": "error", "message": msg})
        out({"type": "turn.failed", "error": {"message": msg}})
        return 1
    scen, _ = _scenario(prompt, "codex")
    if scen in ("rate_limit", "usage_limit"):
        msg = ("You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), "
               "or try again in 2 hours 5 minutes.")
        out({"type": "error", "message": msg})
        out({"type": "turn.failed", "error": {"message": msg}})
        return 1
    if scen == "auth_expired":
        msg = ("Your access token could not be refreshed because your refresh token was already used. "
               "Please log out and sign in again.")
        out({"type": "error", "message": msg})
        out({"type": "turn.failed", "error": {"message": msg}})
        return 1
    if scen == "hang":
        _sleep_forever()
    if scen == "ignore_term":
        _ignore_signals()
        _sleep_forever()
    if scen == "crash":
        os._exit(101)
    if scen == "loop":
        i = 0
        while True:
            i += 1
            out({"type": "item.completed", "item": {"id": f"item_{i}", "type": "command_execution",
                 "command": "true", "aggregated_output": "", "exit_code": 0, "status": "completed"}})
            time.sleep(0.01)
    if scen == "refresh" and auth_path.exists():
        data = json.loads(auth_path.read_text())
        data.setdefault("tokens", {})["refresh_token"] = "rt-refreshed-" + uuid.uuid4().hex[:8]
        auth_path.write_text(json.dumps(data))
    out({"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": "Planning the change."}})
    out({"type": "item.completed", "item": {"id": "item_1", "type": "command_execution", "command": "ls",
         "aggregated_output": "README.md\n", "exit_code": 0, "status": "completed"}})
    out({"type": "item.completed", "item": {"id": "item_2", "type": "agent_message", "text": "Done. Listed files."}})
    out({"type": "turn.completed", "usage": {"input_tokens": 2000, "cached_input_tokens": 500, "output_tokens": 120}})
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    who = argv[argv.index("--as") + 1]
    fdir = Path(argv[argv.index("--dir") + 1])
    args = [a for i, a in enumerate(argv) if a not in ("--as", "--dir") and
            (i == 0 or argv[i - 1] not in ("--as", "--dir"))]
    return claude(args, fdir) if who == "claude" else codex(args, fdir)


def install(directory: Path, python: str | None = None) -> dict[str, str]:
    """Write `claude` and `codex` wrappers (and a copy of this file) into `directory`, so an
    unprivileged brain user can run them without access to the repository or its venv."""
    import shutil

    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "fakecli.py"
    shutil.copy(Path(__file__).resolve(), script)
    script.chmod(0o755)
    py = python or sys.executable
    paths = {}
    for who in ("claude", "codex"):
        p = directory / who
        p.write_text(f'#!/bin/sh\nexec "{py}" -I "{script}" --as {who} --dir "{directory}" "$@"\n')
        p.chmod(0o755)
        paths[who] = str(p)
    return paths


if __name__ == "__main__":
    sys.exit(main())
