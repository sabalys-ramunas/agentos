"""The agents a run can use: the two scripts, Claude Code, Codex, and any other command.

Every one reaches the node through the recording proxy (proxy.py) with the agent grant's token, in
a fresh empty folder per session, with nothing else to work with:

- `perfect`, `careless`: the task's own scripts, over a Python MCP client (tasks.Agent).

A task with a person's folder (Task.local, files in and out) runs a model agent in that folder instead,
and its host_tools (Claude Code permission rules like "Bash(curl *)") are the only shell it gets there:
`--tools Bash` with those rules allowed and every other command refused.
- `claude`: `claude -p` with the person's own sign-in, only the openajar MCP server of the run's node
  (`--strict-mcp-config`: none of theirs), no built-in tools (`--tools ""`) and only that server's tools
  allowed, the openajar-environment skill appended to its system prompt, none of their skills, commands,
  hooks or settings (`--disable-slash-commands`, `--setting-sources project`, in an empty folder), no
  session saved (`--no-session-persistence`), `--max-turns`, `--output-format stream-json`, and Claude
  Code's own `--max-budget-usd` as each session's hard cap.
- `codex`: `codex exec --json` with a scratch CODEX_HOME whose config.toml names only the openajar
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
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from env_eval.metrics import claude_metrics, codex_metrics

REPO = Path(__file__).resolve().parents[2]
SKILL = "client/skills/openajar-environment/SKILL.md"
SERVER = "openajar"
FOOTER = ('\n\nWhen you are done, end with one line of JSON: {"status": "done" or "blocked", "summary": "<one or '
          'two sentences: what you did and what the human should know>"}.')


def skill_path(src: Path | None) -> Path:
    return (src or REPO / "src") / "openajar" / SKILL


AGENT_HOME = "OPENAJAR_EVAL_AGENT_HOME"   # the HOME a model agent runs with, when the harness's own is a scratch one
MAX_TURNS = 60                           # a session's turn limit (--max-turns): far past what any task needs


def _env() -> dict[str, str]:
    """A model agent's environment: this one without openajar's own variables, and HOME set to
    OPENAJAR_EVAL_AGENT_HOME when that is given (the person's, where `claude` is signed in, while the
    harness runs with a scratch HOME)."""
    env = dict(os.environ)
    for k in ("OPENAJAR_URL", "OPENAJAR_TOKEN", "OPENAJAR_HOME", AGENT_HOME):
        env.pop(k, None)
    if os.environ.get(AGENT_HOME):
        env["HOME"] = os.environ[AGENT_HOME]
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


def signed_in(kind: str) -> str | None:
    """None when the model agent `kind` can run here; else what to provide and where, in one line. Checked
    before a paid run starts any node, so a missing sign-in costs nothing and shows no stack trace."""
    if kind == "claude":
        if shutil.which("claude") is None:
            return "the claude command is not installed: install Claude Code (https://claude.com/claude-code)"
        try:
            done = subprocess.run(["claude", "auth", "status"], env=_env(), capture_output=True, text=True,
                                  timeout=60, check=False)
            status = json.loads(done.stdout or "{}")
        except (subprocess.TimeoutExpired, ValueError, OSError):
            status = {}
        if status.get("loggedIn"):
            return None
        return (f"Claude Code is not signed in for HOME={_env().get('HOME')} (`claude auth status`): run `claude auth login` "
                "once in this shell, or set ANTHROPIC_API_KEY to an Anthropic API key, then run the same command "
                "again. Nothing ran and nothing was spent.")
    if kind == "codex":
        if shutil.which("codex") is None:
            return "the codex command is not installed: install the Codex CLI first"
        auth = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"
        if auth.exists() or os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY"):
            return None
        return (f"Codex is not signed in ({auth} is missing): run `codex login` once, or set OPENAI_API_KEY, then "
                "run the same command again. Nothing ran and nothing was spent.")
    return None


def mcp_config(folder: Path, url: str, token: str) -> Path:
    path = folder / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {SERVER: {"type": "http", "url": url,
                                                         "headers": {"Authorization": f"Bearer {token}"}}}}))
    path.chmod(0o600)
    return path


def _work(folder: Path, cwd: Path | None) -> Path:
    """The session's folder: the person's (a task's Task.local), else a new empty one."""
    if cwd is not None:
        return cwd
    work = folder / "work"
    work.mkdir()
    return work


async def claude(prompt: str, *, url: str, token: str, model: str, skill: Path, transcript: Path,
                 timeout_s: float, cap_usd: float, cwd: Path | None = None, host_tools: tuple[str, ...] = (),
                 max_turns: int = MAX_TURNS) -> tuple[str, dict[str, Any], str | None]:
    if shutil.which("claude") is None:
        return "", {}, "the claude command is not installed"
    with tempfile.TemporaryDirectory(prefix="env-eval-claude-") as tmp:
        folder = Path(tmp)
        config = mcp_config(folder, url, token)
        work = _work(folder, cwd)
        builtin = sorted({rule.split("(", 1)[0] for rule in host_tools})
        argv = ["claude", "-p", prompt + FOOTER, "--model", model, "--mcp-config", str(config),
                "--strict-mcp-config", "--tools", ",".join(builtin),
                "--allowedTools", ",".join([f"mcp__{SERVER}", *host_tools]),
                "--append-system-prompt-file", str(skill), "--setting-sources", "project",
                "--disable-slash-commands", "--permission-mode", "dontAsk", "--no-session-persistence",
                "--max-turns", str(max_turns), "--output-format", "stream-json", "--verbose"]
        if cap_usd > 0:
            argv += ["--max-budget-usd", f"{cap_usd:.2f}"]
        out, error = await _run(argv, cwd=work, env=_env(), timeout_s=timeout_s, transcript=transcript)
    metrics = claude_metrics(out)
    return metrics.pop("answer_text", ""), metrics, error


def codex_config(url: str) -> str:
    return (f"[mcp_servers.{SERVER}]\nurl = {json.dumps(url)}\nbearer_token_env_var = \"OPENAJAR_EVAL_TOKEN\"\n"
            "startup_timeout_sec = 30\ntool_timeout_sec = 300\n")


async def codex(prompt: str, *, url: str, token: str, model: str, skill: Path, transcript: Path,
                timeout_s: float, cwd: Path | None = None, host_tools: tuple[str, ...] = ()
                ) -> tuple[str, dict[str, Any], str | None]:
    if shutil.which("codex") is None:
        return "", {}, "the codex command is not installed"
    with tempfile.TemporaryDirectory(prefix="env-eval-codex-") as tmp:
        home = Path(tmp) / "codex-home"
        home.mkdir()
        work = _work(Path(tmp), cwd)
        (home / "config.toml").write_text(codex_config(url))
        auth = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"
        if auth.exists():
            (home / "auth.json").symlink_to(auth)  # the person's own sign-in, never copied
        (work / "AGENTS.md").write_text(skill.read_text())
        env = {**_env(), "CODEX_HOME": str(home), "OPENAJAR_EVAL_TOKEN": token}
        sandbox = "workspace-write" if host_tools else "read-only"  # Codex has no per-command rules: its folder only
        argv = ["codex", "exec", "--json", "--skip-git-repo-check", "--sandbox", sandbox, "-C", str(work)]
        if model:
            argv += ["--model", model]
        argv.append(prompt + FOOTER)
        out, error = await _run(argv, cwd=work, env=env, timeout_s=timeout_s, transcript=transcript)
    metrics = codex_metrics(out)
    return metrics.pop("answer_text", ""), metrics, error


async def command(template: str, prompt: str, *, url: str, token: str, model: str, skill: Path, transcript: Path,
                  timeout_s: float, cwd: Path | None = None, host_tools: tuple[str, ...] = ()
                  ) -> tuple[str, dict[str, Any], str | None]:
    with tempfile.TemporaryDirectory(prefix="env-eval-cmd-") as tmp:
        folder = Path(tmp)
        token_file = folder / "token"
        token_file.write_text(token)
        token_file.chmod(0o600)
        fill = {"prompt": prompt + FOOTER, "mcp_config": str(mcp_config(folder, url, token)), "url": url,
                "token_file": str(token_file), "skill": str(skill), "model": model, "transcript": str(transcript)}
        argv = [part.format(**fill) for part in shlex.split(template)]
        work = _work(folder, cwd)
        out, error = await _run(argv, cwd=work, env=_env(), timeout_s=timeout_s, transcript=transcript)
    metrics = claude_metrics(out)
    answer = metrics.pop("answer_text", "") or out
    return answer, metrics, error
