"""The agents a run can use: the two scripts, Claude Code, Codex, and any other command.

Every one reaches the node through the recording proxy (proxy.py) with the agent grant's token, in
a fresh empty folder per session, with nothing else to work with:

- `perfect`, `careless`: the task's own scripts, over a Python MCP client (tasks.Agent).
- `claude`: `claude -p` with only the agentos MCP server (`--strict-mcp-config`), no built-in tools
  (`--tools ""`), the agentos-environment skill appended to its system prompt, no user settings
  (`--setting-sources project`, in an empty folder), `--output-format stream-json`, and Claude
  Code's own `--max-budget-usd` as each session's hard cap.
- `codex`: `codex exec --json` with a scratch CODEX_HOME whose config.toml names only the agentos
  server (the token in an environment variable, `bearer_token_env_var`), the skill as AGENTS.md in
  its folder, and the person's Codex sign-in linked in when there is one. Built to Codex's
  documented CLI; untested here (Codex is not installed on the machine this was written on).
- `--agent-cmd`: any command, with {prompt}, {mcp_config} (a Claude-style MCP JSON file),
  {url}, {token_file}, {skill}, {model} and {transcript} filled in. Its stdout is kept as the
  transcript and read as stream-json when it is that, else as plain text.

A session returns (final words, the transcript's metrics, an error or None).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import Any

from env_eval.metrics import claude_metrics, codex_metrics

REPO = Path(__file__).resolve().parents[2]
SKILL = "client/skills/agentos-environment/SKILL.md"
SERVER = "agentos"
FOOTER = ('\n\nWhen you are done, end with one line of JSON: {"status": "done" or "blocked", "summary": "<one or '
          'two sentences: what you did and what the human should know>"}.')


def skill_path(src: Path | None) -> Path:
    return (src or REPO / "src") / "agentos" / SKILL


def _env() -> dict[str, str]:
    env = dict(os.environ)
    for k in ("AGENTOS_URL", "AGENTOS_TOKEN", "AGENTOS_HOME"):
        env.pop(k, None)
    return env


async def _run(argv: list[str], *, cwd: Path, env: dict[str, str], timeout_s: float, transcript: Path
               ) -> tuple[str, str | None]:
    """Run a command; its stdout to `transcript` (stderr beside it). (stdout, an error or None)."""
    proc = await asyncio.create_subprocess_exec(*argv, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, start_new_session=True)
    error = None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
    except (TimeoutError, asyncio.CancelledError) as e:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, 9)
        out, err = await proc.communicate()
        if isinstance(e, asyncio.CancelledError):
            transcript.write_bytes(out or b"")
            raise
        error = f"ran past {timeout_s:.0f} s"
    transcript.write_bytes(out or b"")
    if err:
        transcript.with_suffix(".err").write_bytes(err)
    if error is None and proc.returncode not in (0, None):
        tail = (err or b"").decode(errors="replace").strip().splitlines()[-1:] or [""]
        error = f"{argv[0]} exited with {proc.returncode}: {tail[0][:200]}"
    return (out or b"").decode(errors="replace"), error


def mcp_config(folder: Path, url: str, token: str) -> Path:
    path = folder / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {SERVER: {"type": "http", "url": url,
                                                         "headers": {"Authorization": f"Bearer {token}"}}}}))
    path.chmod(0o600)
    return path


async def claude(prompt: str, *, url: str, token: str, model: str, skill: Path, transcript: Path,
                 timeout_s: float, cap_usd: float) -> tuple[str, dict[str, Any], str | None]:
    if shutil.which("claude") is None:
        return "", {}, "the claude command is not installed"
    with tempfile.TemporaryDirectory(prefix="env-eval-claude-") as tmp:
        folder = Path(tmp)
        config = mcp_config(folder, url, token)
        work = folder / "work"
        work.mkdir()
        argv = ["claude", "-p", prompt + FOOTER, "--model", model, "--mcp-config", str(config),
                "--strict-mcp-config", "--tools", "", "--allowedTools", f"mcp__{SERVER}",
                "--append-system-prompt-file", str(skill), "--setting-sources", "project",
                "--permission-mode", "dontAsk", "--no-session-persistence", "--output-format", "stream-json",
                "--verbose"]
        if cap_usd > 0:
            argv += ["--max-budget-usd", f"{cap_usd:.2f}"]
        out, error = await _run(argv, cwd=work, env=_env(), timeout_s=timeout_s, transcript=transcript)
    metrics = claude_metrics(out)
    return metrics.pop("answer_text", ""), metrics, error


def codex_config(url: str) -> str:
    return (f"[mcp_servers.{SERVER}]\nurl = {json.dumps(url)}\nbearer_token_env_var = \"AGENTOS_EVAL_TOKEN\"\n"
            "startup_timeout_sec = 30\ntool_timeout_sec = 300\n")


async def codex(prompt: str, *, url: str, token: str, model: str, skill: Path, transcript: Path,
                timeout_s: float) -> tuple[str, dict[str, Any], str | None]:
    if shutil.which("codex") is None:
        return "", {}, "the codex command is not installed"
    with tempfile.TemporaryDirectory(prefix="env-eval-codex-") as tmp:
        home, work = Path(tmp) / "codex-home", Path(tmp) / "work"
        home.mkdir()
        work.mkdir()
        (home / "config.toml").write_text(codex_config(url))
        auth = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"
        if auth.exists():
            (home / "auth.json").symlink_to(auth)  # the person's own sign-in, never copied
        (work / "AGENTS.md").write_text(skill.read_text())
        env = {**_env(), "CODEX_HOME": str(home), "AGENTOS_EVAL_TOKEN": token}
        argv = ["codex", "exec", "--json", "--skip-git-repo-check", "--sandbox", "read-only", "-C", str(work)]
        if model:
            argv += ["--model", model]
        argv.append(prompt + FOOTER)
        out, error = await _run(argv, cwd=work, env=env, timeout_s=timeout_s, transcript=transcript)
    metrics = codex_metrics(out)
    return metrics.pop("answer_text", ""), metrics, error


async def command(template: str, prompt: str, *, url: str, token: str, model: str, skill: Path, transcript: Path,
                  timeout_s: float) -> tuple[str, dict[str, Any], str | None]:
    with tempfile.TemporaryDirectory(prefix="env-eval-cmd-") as tmp:
        folder = Path(tmp)
        token_file = folder / "token"
        token_file.write_text(token)
        token_file.chmod(0o600)
        fill = {"prompt": prompt + FOOTER, "mcp_config": str(mcp_config(folder, url, token)), "url": url,
                "token_file": str(token_file), "skill": str(skill), "model": model, "transcript": str(transcript)}
        argv = [part.format(**fill) for part in shlex.split(template)]
        work = folder / "work"
        work.mkdir()
        out, error = await _run(argv, cwd=work, env=_env(), timeout_s=timeout_s, transcript=transcript)
    metrics = claude_metrics(out)
    answer = metrics.pop("answer_text", "") or out
    return answer, metrics, error
