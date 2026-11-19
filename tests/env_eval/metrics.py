"""What a run measured, from the proxy's rows and a model agent's transcript. Pure functions: no node,
no Docker (tests/unit/test_env_eval.py tests them).

A row is one tool call as the proxy recorded it (proxy.Call as a dict): tool, args, ok, code, text,
images, started, ended. `tools_call(name, arguments)` rows count as the tool they ran.

Tokens are estimates, as the toolset budget test makes them: characters / 4. `tokens_read` is the
text of every tool result the agent got back, so it is the same measure for a script, Claude Code or
Codex. A model agent's own input, output and cache tokens come from its transcript.
"""

from __future__ import annotations

import contextlib
import json
import re
import shlex
from collections import Counter
from typing import Any

from agentos.core.toolnames import current

ENV_TOOLS = {"environment_open", "environment"}
# A call refused for want of the environment's handle: the skill's first rule is to pass `on`.
NO_ON_CODES = {"which_environment", "no_environment"}
READERS = {"cat", "head", "tail", "less", "more", "nl", "bat"}
SLEEP_S = 2.0           # a sleep this long inside a command is waiting by hand (until_port waits on the machine)
POLLS = 3               # this many reads of one job with no until_exit or until_port is a poll loop
SEGMENT = re.compile(r"\|\||&&|;|\||\n")
# the name of an environment's terminal or browser (env/<id>, env/<id>-browser, <id>-browser, <id>), never its
# handle "<id>.<secret>"
LANE_NAME = re.compile(r"^(env/)?e[a-z0-9]{3,15}(-browser)?$")


def unwrap(row: dict[str, Any]) -> dict[str, Any]:
    """A tools_call row as the tool it ran, by its name today: a run from before 2026-11-19 (or an agent
    that still says lane_exec) counts as the renamed tool, so history stays comparable."""
    if row.get("tool") == "tools_call" and isinstance((row.get("args") or {}).get("name"), str):
        inner = row["args"].get("arguments")
        return {**row, "tool": current(row["args"]["name"]), "args": inner if isinstance(inner, dict) else {},
                "via": "tools_call"}
    if isinstance(row.get("tool"), str) and current(row["tool"]) != row["tool"]:
        return {**row, "tool": current(row["tool"]), "old_name": row["tool"]}
    return row


def parsed(row: dict[str, Any]) -> dict[str, Any]:
    """The result's JSON object, or {}."""
    text = str(row.get("text") or "")
    with contextlib.suppress(ValueError):
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    return {}


def tokens(chars: int) -> int:
    return chars // 4


# ---- wrong turns ----

def _segments(cmd: str) -> list[list[str]]:
    out = []
    for part in SEGMENT.split(cmd):
        try:
            words = shlex.split(part, posix=True)
        except ValueError:
            words = part.split()
        if words:
            out.append(words)
    return out


def shell_file_io(cmd: str) -> str | None:
    """'read' or 'write' when the command reads or writes a file by hand (cat, sed -n, a heredoc,
    echo > file) instead of file_read / file_edit / file_write; else None. A reader
    at the end of a pipe (`npm test | tail -5`) reads no file and is fine."""
    if "<<" in cmd:
        return "write"
    for words in _segments(cmd):
        while words and "=" in words[0] and not words[0].startswith("-"):  # FOO=1 cat x
            words = words[1:]
        if not words:
            continue
        first, rest = words[0].rsplit("/", 1)[-1], words[1:]
        files = [w for w in rest if not w.startswith("-") and not w.startswith(">") and w not in ("2>&1",)]
        if first == "sed" and any(w == "-i" or w.startswith("-i") for w in rest):
            return "write"
        if first in ("echo", "printf", "cat") and any(w in (">", ">>") or (w.startswith(">") and len(w) > 1
                                                          and w not in (">&2",)) for w in rest):
            target = next((rest[i + 1] for i, w in enumerate(rest) if w in (">", ">>") and i + 1 < len(rest)), "")
            if target != "/dev/null":
                return "write"
        if first == "sed" and "-n" in rest and len(files) >= 2:
            return "read"
        if first in READERS and files:
            return "read"
    return None


def sleeps(cmd: str) -> float:
    """The longest `sleep N` in a command (0 when none), a loop's sleep counted as long."""
    longest = 0.0
    loop = bool(re.search(r"\b(while|until|for)\b", cmd))
    for m in re.finditer(r"\bsleep\s+(\d+(?:\.\d+)?)", cmd):
        longest = max(longest, float(m.group(1)) * (10 if loop else 1))
    return longest


def _failed(row: dict[str, Any]) -> bool:
    if not row.get("ok"):
        return True
    if row.get("tool") == "terminal_exec":
        code = parsed(row).get("exit_code")
        return isinstance(code, int) and code != 0
    return False


def _key(row: dict[str, Any]) -> str:
    return row["tool"] + json.dumps(row.get("args") or {}, sort_keys=True, default=str)


def wrong_turns(rows: list[dict[str, Any]], *, new_environments: int = 1) -> list[str]:
    """What an agent following the skill would not have done, one line each. `new_environments`: how
    many new machines the task needs (a second one past that is a wrong turn)."""
    rows = [unwrap(r) for r in rows]
    out: list[str] = []
    polls: Counter[str] = Counter()
    opened_new = 0
    prev: dict[str, Any] | None = None
    for r in rows:
        tool, args = r["tool"], r.get("args") or {}
        on = args.get("on")
        if isinstance(on, str) and LANE_NAME.match(on.strip()):  # Haiku, 2026-11-18: lanes.browser as on
            out.append(f"{tool} with its terminal's or browser's name as on ({on[:40]!r}), not the on its "
                       "environment_open gave"
                       + (f" (refused: {r['code']})" if r.get("code") else ""))
        elif r.get("code") in NO_ON_CODES and tool != "environment_open":
            out.append(f"{tool} without the on its environment_open gave (refused: {r['code']})")
        if tool == "terminal_exec":
            cmd = str(args.get("cmd") or "")
            how = shell_file_io(cmd)
            if how:
                out.append(f"{'read' if how == 'read' else 'wrote'} a file through terminal_exec "
                           f"({cmd[:50]!r}), not file_{'read' if how == 'read' else 'edit'}")
            if sleeps(cmd) >= SLEEP_S:
                out.append(f"waited with sleep in terminal_exec ({cmd[:50]!r}); terminal_job_read's until_port or "
                           "until_exit waits on the machine")
        if tool == "terminal_job_read" and not args.get("until_exit") and not args.get("until_port"):
            job = str(args.get("job"))
            polls[job] += 1
            if polls[job] == POLLS:
                out.append(f"polled job {job[:12]} {POLLS} times, not until_exit or until_port")
        if tool == "environment_open" and r.get("ok") and parsed(r).get("opened") == "new":
            opened_new += 1
            if opened_new > new_environments:
                out.append(f"opened another new environment ({opened_new} where the task needs "
                           f"{new_environments})")
        if prev is not None and prev.get("session") != r.get("session"):
            prev = None  # another chat: its first call repeats nothing
        if prev is not None and _failed(prev) and _key(prev) == _key(r):
            out.append(f"repeated the failing {tool} unchanged")
        prev = r
    return out


# ---- timings ----

def _ready(row: dict[str, Any]) -> bool:
    return row["tool"] in ENV_TOOLS and bool(row.get("ok")) and parsed(row).get("state") == "ready"


def open_times(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each environment_open: how long until the machine was ready (the open's own wait, and any
    environment() calls after it until one said ready), what it opened, and the app at open."""
    rows = [unwrap(r) for r in rows]
    out = []
    for i, r in enumerate(rows):
        if r["tool"] != "environment_open" or not r.get("ok"):
            continue
        data = parsed(r)
        if not data.get("on"):
            continue
        done = r if _ready(r) else next((x for x in rows[i + 1:] if _ready(x)), None)
        app = data.get("app") or {}
        prepared = data.get("prepared") or {}
        out.append({"opened": data.get("opened"), "ready_s": round(done["ended"] - r["started"], 2) if done else None,
                    "app_listening": bool(app.get("listening")), "prepared": bool(prepared.get("used")),
                    "warm": bool(prepared.get("warm")),
                    "resumed_from": (data.get("resumed") or {}).get("from")})
    return out


def time_to_ready(rows: list[dict[str, Any]], t0: float) -> float | None:
    """Seconds from the agent's start to the first result that said its environment is ready."""
    first = next((r for r in (unwrap(x) for x in rows) if _ready(r)), None)
    return round(first["ended"] - t0, 2) if first else None


# ---- the trace's sums ----

def _seconds_by_tool(rows: list[dict[str, Any]]) -> dict[str, float]:
    """Where the time went: each tool's calls added up (the first browser call starts the browser)."""
    out: dict[str, float] = {}
    for r in rows:
        if r.get("ended") is not None and r.get("started") is not None:
            out[r["tool"]] = round(out.get(r["tool"], 0.0) + r["ended"] - r["started"], 2)
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def first_navigate(rows: list[dict[str, Any]]) -> float | None:
    """Seconds the first browser_navigate took: on a fresh machine it waited for the browser to start."""
    first = next((r for r in rows if r["tool"] == "browser_navigate" and r.get("ended") is not None
                  and r.get("started") is not None), None)
    return round(first["ended"] - first["started"], 2) if first else None


def trace_metrics(rows: list[dict[str, Any]], *, new_environments: int = 1) -> dict[str, Any]:
    rows_u = [unwrap(r) for r in rows]
    errors = [r for r in rows_u if not r.get("ok")]
    turns = wrong_turns(rows, new_environments=new_environments)
    return {
        "calls": len(rows_u),
        "calls_by_tool": dict(Counter(r["tool"] for r in rows_u)),
        "seconds_by_tool": _seconds_by_tool(rows_u),
        "tool_errors": len(errors),
        "errors_by_code": dict(Counter(r.get("code") or "error" for r in errors)),
        "tokens_read": tokens(sum(len(str(r.get("text") or "")) for r in rows_u)),
        "images": sum(int(r.get("images") or 0) for r in rows_u),
        "wrong_turns": turns,
        **({"first_navigate_s": nav} if (nav := first_navigate(rows_u)) is not None else {}),
    }


# ---- model transcripts ----

def _events(text: str) -> list[dict[str, Any]]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            with contextlib.suppress(ValueError):
                ev = json.loads(line)
                if isinstance(ev, dict):
                    out.append(ev)
    return out


def claude_metrics(text: str) -> dict[str, Any]:
    """Claude Code's stream-json: the `result` event's turns, cost, time and tokens (cache too), the
    model, and the final text (`answer_text`)."""
    events = _events(text)
    results = [e for e in events if e.get("type") == "result"]
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    if not results:
        return {"answer_text": "", **({"model": init["model"]} if init.get("model") else {})}
    r = results[-1]
    usage = r.get("usage") or {}
    out = {"turns": r.get("num_turns"), "cost_usd": r.get("total_cost_usd"), "agent_ms": r.get("duration_ms"),
           "api_ms": r.get("duration_api_ms"), "input_tokens": usage.get("input_tokens"),
           "output_tokens": usage.get("output_tokens"), "cache_read_tokens": usage.get("cache_read_input_tokens"),
           "cache_write_tokens": usage.get("cache_creation_input_tokens"), "is_error": r.get("is_error"),
           "model": init.get("model"), "answer_text": str(r.get("result") or "")}
    return {k: v for k, v in out.items() if v is not None}


def codex_metrics(text: str) -> dict[str, Any]:
    """`codex exec --json`: the tokens of each turn.completed summed, the turns, and the last agent
    message as the final text. Untested against a real Codex here (it is not installed)."""
    events = _events(text)
    totals: Counter[str] = Counter()
    turns = 0
    answer = ""
    for e in events:
        kind = e.get("type")
        if kind == "turn.completed":
            turns += 1
            u = e.get("usage") or {}
            totals["input_tokens"] += int(u.get("input_tokens") or 0)
            totals["cache_read_tokens"] += int(u.get("cached_input_tokens") or 0)
            totals["output_tokens"] += int(u.get("output_tokens") or 0)
        elif kind == "item.completed":
            item = e.get("item") or {}
            if item.get("type") in ("agent_message", "assistant_message"):
                answer = str(item.get("text") or answer)
    out: dict[str, Any] = {"answer_text": answer}
    if turns:
        out.update({"turns": turns, **totals})
    return out


def final_json(text: str) -> dict[str, Any]:
    """The last line of the agent's final words that is a JSON object ({"status": ..., "summary": ...})."""
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip().strip("`")
        if line.startswith("{"):
            with contextlib.suppress(ValueError):
                data = json.loads(line)
                if isinstance(data, dict):
                    return data
    return {}
