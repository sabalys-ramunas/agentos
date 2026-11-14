"""Find the brain CLIs, read their versions, and detect flag drift.

agentos drives `claude` and `codex` through specific flags. Those CLIs update often, so
before a brain runs we check that every flag we rely on still exists. A missing flag marks
the brain `down` with a reason that names the flag, the installed version and the version
we verified, instead of failing mysteriously mid-task.

Verified on the node on 2026-11-03 with claude 2.1.317 and codex-cli 0.166.0. The pinned
sets below are the single source of truth: adapters.py may only use flags listed here, and
tests/node/test_brains_real_cli.py checks them against the real CLIs.

`chat` is our own runner (agentos.brains.chatcli), not an installed CLI: its binary is a Python the
brain user can run, and CHAT_ARGS put the script in front of every command. Checking it the same
way shows that this Python runs the script at all.

`systemone` is not a program at all: decisions run inside agentosd (agentos.brains.decisions), so
there is nothing to find and no flag to drift, and it always probes as present.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from agentos.brains import chatcli, decisions

CHAT_SCRIPT = str(Path(chatcli.__file__).resolve())
CHAT_ARGS = ["-I", "-B", CHAT_SCRIPT]  # isolated: nothing from the environment, site or the working directory

PINNED: dict[str, dict] = {
    "claude": {
        "verified": "2.1.317",
        "help_cmd": ["--help"],
        "help_flags": ["--print", "--output-format", "--verbose", "--model", "--allowedTools",
                       "--permission-mode", "--mcp-config", "--strict-mcp-config", "--resume",
                       "--append-system-prompt", "--max-budget-usd", "--setting-sources", "--tools",
                       "--effort"],
        # not listed in --help but accepted; checked by argument validation (no network)
        "hidden_flags": {"--max-turns": ["-p", "probe", "--max-turns", "not-a-number"]},
        "choices": {"--permission-mode": ["dontAsk", "acceptEdits", "bypassPermissions"],
                    "--output-format": ["stream-json"]},
    },
    "codex": {
        "verified": "0.166.0",
        "help_cmd": ["exec", "--help"],
        "help_flags": ["--json", "--skip-git-repo-check", "--cd", "--sandbox", "--model", "--config",
                       "--oss", "--local-provider", "--output-last-message"],
        "subcommands": {"resume": ["exec", "resume", "--help"]},
        "resume_flags": ["--json", "--skip-git-repo-check", "--config", "--model"],
        "choices": {"--sandbox": ["workspace-write"]},
    },
    "chat": {
        "verified": chatcli.VERSION,
        "prefix": CHAT_ARGS,
        "help_cmd": ["--help"],
        "help_flags": ["--base-url", "--model", "--system", "--max-output-tokens", "--timeout", "--max-usd",
                       "--price-in", "--price-out"],
    },
    "systemone": {"verified": decisions.VERSION, "in_process": True},
}

VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


@dataclass
class CliInfo:
    runner: str
    path: str | None
    version: str | None = None
    problems: list[str] = field(default_factory=list)   # blocking: the brain is down
    warnings: list[str] = field(default_factory=list)   # non-blocking, shown in `brains test`

    @property
    def ok(self) -> bool:
        return self.path is not None and not self.problems

    def reason(self) -> str | None:
        return "; ".join(self.problems) if self.problems else None

    def public(self) -> dict:
        return {"runner": self.runner, "path": self.path, "version": self.version,
                "verified": PINNED[self.runner]["verified"], "ok": self.ok,
                "problems": self.problems, "warnings": self.warnings}


def find_bin(runner: str, *, explicit: str | None = None, bin_dirs: list[str] | None = None) -> str | None:
    if explicit:
        return explicit if os.access(explicit, os.X_OK) else None
    if runner == "chat":
        from agentos.brains.confine import probe_python

        return probe_python()
    for d in bin_dirs or []:
        cand = Path(d) / runner
        if os.access(cand, os.X_OK):
            return str(cand)
    return shutil.which(runner)


def _run(argv: list[str], *, env: dict[str, str], timeout: float = 20) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _probe_env(bin_path: str, tmp: str, bin_dirs: list[str]) -> dict[str, str]:
    path = ":".join([str(Path(bin_path).parent), *bin_dirs, "/usr/local/bin", "/usr/bin", "/bin"])
    return {"PATH": path, "HOME": tmp, "CLAUDE_CONFIG_DIR": f"{tmp}/.claude", "CODEX_HOME": f"{tmp}/.codex",
            "LANG": "C.UTF-8", "TERM": "dumb", "NO_COLOR": "1", "DISABLE_AUTOUPDATER": "1"}


def _flag_present(help_text: str, flag: str) -> bool:
    """True when `flag` is defined as an option (start of a help line, possibly after a short
    form or another alias), not merely mentioned in some other option's description."""
    pat = rf"^[ \t]*(?:-[A-Za-z], )?(?:--[\w-]+, )*{re.escape(flag)}(?=[\s,=<\[]|$)"
    return re.search(pat, help_text, re.MULTILINE) is not None


_cache: dict[tuple, CliInfo] = {}


def probe(runner: str, *, explicit: str | None = None, bin_dirs: list[str] | None = None,
          use_cache: bool = True) -> CliInfo:
    if PINNED[runner].get("in_process"):
        return CliInfo(runner, "in process", version=PINNED[runner]["verified"])
    bin_dirs = bin_dirs or []
    path = find_bin(runner, explicit=explicit, bin_dirs=bin_dirs)
    if path is None:
        where = explicit or ", ".join([*bin_dirs, "PATH"])
        return CliInfo(runner, None, problems=[
            f"{runner} CLI is not installed (looked in {where}); run src/agentos/brains/install_cli.sh"])
    try:
        st = Path(path).resolve().stat()
        key = (runner, path, st.st_mtime_ns, st.st_size)
    except OSError:
        key = (runner, path, 0, 0)
    if use_cache and key in _cache:
        return _cache[key]
    pin = PINNED[runner]
    info = CliInfo(runner, path)
    cmd = [path, *pin.get("prefix", [])]
    with tempfile.TemporaryDirectory(prefix="agentos-probe-") as tmp:
        env = _probe_env(path, tmp, bin_dirs)
        code, out = _run([*cmd, "--version"], env=env)
        m = VERSION_RE.search(out)
        info.version = m.group(0) if m else None
        if code != 0 or not m:
            info.problems.append(f"{runner} --version failed: {out.strip()[:200]}")
            _cache[key] = info
            return info
        code, help_text = _run([*cmd, *pin["help_cmd"]], env=env)
        if code != 0 and not help_text:
            info.problems.append(f"{runner} {' '.join(pin['help_cmd'])} failed")
        missing = _missing_flags(path, pin, help_text, env)
        if missing:
            info.problems.append(
                f"{runner} {info.version} no longer offers {', '.join(missing)}; agentos was verified with "
                f"{pin['verified']}. Pin it: "
                f"{'CLAUDE_CODE_VERSION' if runner == 'claude' else 'CODEX_VERSION'}={pin['verified']} "
                "install_cli.sh --upgrade")
        vm, pm = VERSION_RE.search(info.version), VERSION_RE.search(pin["verified"])
        if vm and pm and vm.group(1, 2) != pm.group(1, 2):
            info.warnings.append(f"{runner} {info.version} is untested (verified {pin['verified']}); "
                                 "flags checked and present" if not missing else "")
    info.warnings = [w for w in info.warnings if w]
    _cache[key] = info
    return info


def _missing_flags(path: str, pin: dict, help_text: str, env: dict[str, str]) -> list[str]:
    """The flags, choices and subcommands agentos uses that this CLI no longer offers."""
    missing = [f for f in pin["help_flags"] if not _flag_present(help_text, f)]
    for flag, values in pin.get("choices", {}).items():
        for v in values:
            if _flag_present(help_text, flag) and v not in help_text:
                missing.append(f"{flag} {v}")
    for flag, argv in pin.get("hidden_flags", {}).items():
        _, out2 = _run([path, *argv], env=env, timeout=20)
        if "unknown option" in out2 and flag in out2:
            missing.append(flag)
    for sub, argv in pin.get("subcommands", {}).items():
        code3, out3 = _run([path, *argv], env=env)
        if code3 != 0 or "Usage" not in out3:
            missing.append(f"exec {sub}")
        else:
            missing += [f"exec {sub} {f}" for f in pin.get(f"{sub}_flags", []) if not _flag_present(out3, f)]
    return missing


def clear_cache() -> None:
    _cache.clear()
