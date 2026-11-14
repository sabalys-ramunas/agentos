"""What a review is shown: structured facts about agent calls, never what a page or a person wrote.

Two sources:

- **The node's action log** (agentos.policy.traces, `tool_end` rows): the calls agents and brains
  made over the window, grouped into sessions (a run, or else a grant). The node's own checks are
  left out (settings `skip_subjects`, `skip_workspaces`), and so are people and flows, which no
  model drove.
- **Lanebench reports** a person put in the inbox: the same facts per task, plus its outcome (the
  checks and gates that failed). Task ids, lane ids and the bench's own names are replaced, so a
  reviewer cannot write guidance for one task.

Of a call, only these are kept: the tool, its status, the error code and the catalog's text for
it, the argument *names*, and the values of arguments whose type is a closed set (a Literal, an
Enum or a bool: browser_act's `action` and `snapshot`). Argument values, the summary and error
messages are dropped, because they can hold a page's or an email's text.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("agentos.textreview")

AGENT_KINDS = ("agent", "brain")  # a model chose these calls; people and flows are not reviewed
START_TOOLS = ("whoami", "lanes_list", "grant_info", "howto")
CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,48}$")
LANE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*/[a-z0-9][a-z0-9_.-]*$")
BRAIN_TASK_RE = re.compile(r"\bbt_[A-Za-z0-9]+")
WORKER_RUN_RE = re.compile(r"^pilot-run-")  # agentos.pilot.grant: purpose "pilot run <id>"
URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
HOST_RE = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:test|local|localhost|invalid|example|com|net|org|io)\b", re.IGNORECASE)
QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"|`[^`]*`|“[^”]*”")
BENCH_NAME = "lanebench"
MAX_DETAIL = 160
MAX_REPORT_BYTES = 20_000_000
MAX_ROWS = 50_000


def _get(row: Any, name: str) -> Any:
    return row.get(name) if isinstance(row, Mapping) else getattr(row, name, None)


@dataclass(frozen=True)
class Call:
    tool: str
    status: str                      # ok, error, denied or pending
    code: str | None = None          # a catalog code, or "unexpected" for an error without one
    keys: tuple[str, ...] = ()       # argument names, sorted
    values: tuple[tuple[str, str], ...] = ()  # (argument, value) for closed-set arguments only

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def same_as(self, other: Call) -> bool:
        return (self.tool, self.keys, self.values) == (other.tool, other.keys, other.values)

    def line(self) -> str:
        shown = [f"{k}={v}" for k, v in self.values] + [k for k in self.keys if k not in dict(self.values)]
        args = f" ({', '.join(shown)})" if shown else ""
        return f"{self.tool}{args}: {'ok' if self.ok else self.code or self.status}"


@dataclass
class Session:
    sid: str
    who: str     # the client a grant was issued for, or the brain or worker behind a run
    kind: str    # agent, brain or worker
    calls: list[Call] = field(default_factory=list)


@dataclass
class ProbeTask:
    """One lanebench task, renamed. `passed` is the bench's own verdict: success and every gate."""
    tid: str
    executor: str
    mode: str
    adversarial: bool
    passed: bool
    success: bool
    said: str = ""   # how the run itself ended (done, failed, gave_up): a run can say done and still fail
    failed_checks: list[str] = field(default_factory=list)
    failed_gates: list[str] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)


@dataclass
class Evidence:
    since: float
    until: float
    sessions: list[Session] = field(default_factory=list)
    probes: list[ProbeTask] = field(default_factory=list)
    left_out: int = 0                        # rows of the node's own checks, people and flows
    lanes: set[str] = field(default_factory=set)   # real lane ids seen: never rendered, the validator's list
    probe_words: set[str] = field(default_factory=set)  # task and lane ids of the reports, ditto
    reports: list[Path] = field(default_factory=list)   # the inbox files read

    @property
    def calls(self) -> int:
        return sum(len(s.calls) for s in self.sessions) + sum(len(p.calls) for p in self.probes)


class Codes:
    """The catalog's codes and words, to name an error without its message."""

    def __init__(self, entries: Mapping[str, Any]):
        self.entries = entries
        denied = entries.get("denied")
        self.reasons = frozenset(getattr(denied, "reasons", {}) or {})

    def of(self, status: str, error: str | None) -> str | None:
        if status == "ok":
            return None
        if status == "pending":
            return "approval_pending"
        parts = [p.strip() for p in (error or "").split(":")[:3]]
        found = next((p for p in parts if p in self.entries), None)
        if status == "denied":
            reason = next((p for p in parts if p in self.reasons), None)
            return f"denied ({reason})" if reason and found in (None, "denied") else found or "denied"
        if found:
            return found
        return "cancelled" if parts and parts[0] == "cancelled" else "unexpected"

    def meaning(self, code: str) -> tuple[str, str] | None:
        entry = self.entries.get(code.split(" ", 1)[0])
        return (entry.meaning, entry.agent) if entry is not None else None


def call_of(tool: str, status: str, error: str | None, args: Any, enums: Mapping[str, Mapping[str, frozenset[str]]],
            codes: Codes) -> Call:
    tool = tool.rsplit("__", 1)[-1]  # an MCP client's own prefix (mcp__agentos__browser_act)
    args = args if isinstance(args, Mapping) else {}
    allowed = enums.get(tool, {})
    values = tuple(sorted((k, str(v)) for k, v in args.items()
                          if k in allowed and isinstance(v, str | bool) and str(v) in allowed[k]))
    keys = tuple(sorted(str(k) for k in args if CODE_RE.match(str(k))))
    return Call(tool=tool, status=status, code=codes.of(status, error), keys=keys, values=values)


# ---- the action log ----

def read_rows(traces: Any, since: float, until: float) -> list[Any]:
    """Every `tool_end` row in the window, oldest first."""
    rows: list[Any] = []
    cursor: str | None = None
    while len(rows) < MAX_ROWS:
        page, cursor = traces.query(kind="tool_end", since=since, until=until, before=cursor, limit=500)
        rows += page
        if not cursor:
            break
    rows.reverse()
    return rows


def _skipped(row: Any, skip_subjects: Iterable[str], skip_workspaces: Iterable[str]) -> bool:
    if _get(row, "principal_kind") not in AGENT_KINDS:
        return True
    principal = str(_get(row, "principal") or "")
    if set(principal.split("/")) & set(skip_subjects):  # a sub-agent of a check is the check
        return True
    lane = str(_get(row, "lane") or "")
    workspace = str(_get(row, "workspace") or "") or (lane.split("/", 1)[0] if "/" in lane else "")
    return workspace in set(skip_workspaces)


def sessions_of(rows: list[Any], *, enums: Mapping[str, Mapping[str, frozenset[str]]], codes: Codes,
                skip_subjects: Iterable[str], skip_workspaces: Iterable[str],
                label: Callable[[str, str], tuple[str, str]] | None = None) -> tuple[list[Session], int, set[str]]:
    """Sessions in the order they started, how many rows were left out, and the lanes seen."""
    by_key: dict[str, Session] = {}
    left_out = 0
    lanes: set[str] = set()
    for row in rows:
        if _skipped(row, skip_subjects, skip_workspaces):
            left_out += 1
            continue
        lane = _get(row, "lane")
        if isinstance(lane, str) and LANE_RE.match(lane):
            lanes.add(lane)
        key = str(_get(row, "run_id") or _get(row, "grant_id") or _get(row, "principal") or "?")
        session = by_key.get(key)
        if session is None:
            principal, kind = str(_get(row, "principal") or "unknown"), str(_get(row, "principal_kind"))
            who, what = (label or default_label)(principal, kind)
            session = by_key[key] = Session(sid=f"S{len(by_key) + 1:02d}", who=who, kind=what)
        session.calls.append(call_of(str(_get(row, "tool") or "?"), str(_get(row, "status") or "error"),
                                     _get(row, "error"), _get(row, "args"), enums, codes))
    return list(by_key.values()), left_out, lanes


def default_label(principal: str, kind: str) -> tuple[str, str]:
    """(who, kind) from the principal alone. The service knows more: which brain ran a task."""
    if kind == "brain" and WORKER_RUN_RE.match(principal):
        return "browser worker", "worker"
    if kind == "brain":
        return "a brain task", "brain"
    return f"client {principal}", "agent"


# ---- lanebench reports ----

class Renamer:
    """Task ids, lane ids and the bench's names, replaced wherever they show; URLs and hosts too."""

    def __init__(self) -> None:
        self.names: dict[str, str] = {}

    def add(self, real: str, alias: str) -> None:
        if real and real not in self.names:
            self.names[real] = alias

    def __call__(self, text: str, limit: int = MAX_DETAIL) -> str:
        text = URL_RE.sub("<url>", text)
        for real in sorted(self.names, key=len, reverse=True):
            text = re.sub(re.escape(real), self.names[real], text, flags=re.IGNORECASE)
        text = HOST_RE.sub("<host>", text)
        text = re.sub(BENCH_NAME, "probe", text, flags=re.IGNORECASE)
        text = QUOTED_RE.sub("<text>", text)  # what a page said, or was meant to say, is not shown
        text = " ".join(text.split())
        return text if len(text) <= limit else text[:limit] + "..."


def read_reports(inbox: Path, *, enums: Mapping[str, Mapping[str, frozenset[str]]], codes: Codes,
                 ) -> tuple[list[ProbeTask], set[str], list[Path]]:
    """Every lanebench report in the inbox, as renamed tasks; the real names; the files read."""
    tasks: list[ProbeTask] = []
    words: set[str] = set()
    used: list[Path] = []
    if not inbox.is_dir():
        return tasks, words, used
    rename = Renamer()
    for path in sorted(inbox.glob("*.json")):
        try:
            if path.stat().st_size > MAX_REPORT_BYTES:
                raise ValueError("too large")
            report = json.loads(path.read_text())
            if not isinstance(report, dict) or "lanebench" not in report or not isinstance(report.get("tasks"), list):
                raise ValueError("not a lanebench report")
        except (OSError, ValueError) as e:
            log.warning("%s is not read: %s", path.name, e)
            continue
        used.append(path)
        for record in report["tasks"]:
            if not isinstance(record, dict):
                continue
            n = len(tasks) + 1
            for real, alias in ((record.get("id"), f"P{n:02d}"), (record.get("lane"), f"lane-{n:02d}"),
                                (record.get("title"), f"task P{n:02d}")):
                if isinstance(real, str) and real:
                    rename.add(real, alias)
                    words.add(real)
            tasks.append(_probe(record, n, enums, codes))
    # renamed once every task's names are known: a detail can name another task
    for task in tasks:
        task.failed_checks = [rename(t) for t in task.failed_checks]
        task.failed_gates = [rename(t) for t in task.failed_gates]
        task.executor = rename(task.executor, 40)
    return tasks, words, used


def _probe(record: dict[str, Any], n: int, enums: Mapping[str, Mapping[str, frozenset[str]]],
           codes: Codes) -> ProbeTask:
    """One task with its text as the report has it; read_reports renames it. A check is shown by its
    kind alone: its detail quotes the page (a post, a name), and the kind says what was expected."""
    checks = [str(c.get("kind") or "check") for c in record.get("checks") or []
              if isinstance(c, dict) and not c.get("passed")]
    gates: list[str] = []
    for name, gate in (record.get("gates") or {}).items():
        if isinstance(gate, dict) and gate.get("applicable", True) and not gate.get("passed", True):
            findings = [str(f) for f in (gate.get("findings") or [])[:3]]
            gates.append(f"{name}: {'; '.join(findings) or 'failed'}")
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    if result.get("error"):
        checks.append(f"the run itself: {codes.of('error', str(result['error'])) or 'unexpected'}")
    said = str(result.get("status") or "")
    calls = []
    for row in record.get("tool_trace") or []:
        if isinstance(row, dict) and row.get("tool"):
            ok = bool(row.get("ok"))
            calls.append(call_of(str(row["tool"]), "ok" if ok else "error", row.get("error"), row.get("args"),
                                 enums, codes))
    return ProbeTask(tid=f"P{n:02d}", executor=str(record.get("executor") or "unknown"),
                     said=said if CODE_RE.match(said) else "",
                     mode=str(record.get("mode") or "do")[:20], adversarial=bool(record.get("adversarial")),
                     passed=bool(record.get("passed")), success=bool(record.get("success")),
                     failed_checks=checks, failed_gates=gates, calls=calls)


# ---- patterns and the text a reviewer reads ----

def patterns(runs: list[list[Call]]) -> list[str]:
    """The aggregate lines: errors and what came next, repeats, act-then-snapshot, delta use."""
    calls = [c for run in runs for c in run]
    if not calls:
        return ["- No calls."]
    after_error: Counter[tuple[str, str, str]] = Counter()
    repeated: Counter[str] = Counter()
    act_then: Counter[str] = Counter()
    start_extra: Counter[str] = Counter()
    for run in runs:
        if not run:
            continue
        for a, b in zip(run, [*run[1:], None], strict=True):
            if not a.ok:
                nxt = "the session ended" if b is None else "the same tool again" if b.tool == a.tool else f"then {b.tool}"
                after_error[(a.tool, a.code or a.status, nxt)] += 1
            if b is not None and a.same_as(b):
                repeated[a.tool] += 1
            if a.tool == "browser_act" and b is not None:
                act_then[b.tool] += 1
        counts = Counter(c.tool for c in run)
        for tool in START_TOOLS:
            if counts[tool] > 1:
                start_extra[tool] += counts[tool] - 1
    acts = [c for c in calls if c.tool == "browser_act"]
    delta = sum(1 for c in acts if ("snapshot", "delta") in c.values)
    sizes = [len(run) for run in runs if run]
    out = [(f"- {len(calls)} calls in {len(runs)} sessions, {sum(not c.ok for c in calls)} not ok. "
           f"Calls per session: median {statistics.median(sizes):g}, most {max(sizes)}.")]
    if acts:
        out.append(f"- browser_act: {len(acts)} calls, {delta} with snapshot=delta. Followed by a full browser_snapshot: "
                   f"{act_then['browser_snapshot']}; by browser_wait: {act_then['browser_wait']}.")
    out.append(f"- browser_snapshot: {sum(c.tool == 'browser_snapshot' for c in calls)} calls; browser_screenshot: "
               f"{sum(c.tool == 'browser_screenshot' for c in calls)}.")
    if start_extra:
        out.append("- Start-up calls repeated within one session: "
                   + ", ".join(f"{t} {n} extra" for t, n in start_extra.most_common()) + ".")
    if repeated:
        out.append("- The same call (same tool, arguments and choices) twice in a row: "
                   + ", ".join(f"{t} x{n}" for t, n in repeated.most_common()) + ".")
    if after_error:
        out.append("- Errors by tool and code, and what the agent did next:")
        out += [f"  - {t} {code}: {n}, {nxt}" for (t, code, nxt), n in after_error.most_common(40)]
    return out


def _by_who(sessions: list[Session]) -> list[str]:
    groups: dict[str, list[Session]] = {}
    for s in sessions:
        groups.setdefault(f"{s.who} ({s.kind})", []).append(s)
    out = []
    for who, group in sorted(groups.items(), key=lambda kv: -sum(len(s.calls) for s in kv[1])):
        calls = [c for s in group for c in s.calls]
        out.append(f"- {who}: {len(group)} sessions, {len(calls)} calls, {sum(not c.ok for c in calls)} not ok, "
                   f"{len(calls) / len(group):.1f} calls per session.")
    return out


def _calls(calls: list[Call], limit: int) -> list[str]:
    shown = [f"{i}. {c.line()}" for i, c in enumerate(calls[:limit], 1)]
    if len(calls) > limit:
        shown.append(f"... {len(calls) - limit} more calls")
    return shown


def _iso(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%d %H:%M UTC")


def render(ev: Evidence, codes: Codes, *, max_sessions: int, max_calls: int) -> str:
    """The evidence as the reviewer reads it, and as evidence.md keeps it."""
    out = [f"# Evidence: {_iso(ev.since)} to {_iso(ev.until)}", ""]
    out += ["## Live traffic on this node", "",
            ("Each session is one run or one grant. Only tool names, statuses, error codes, argument names and "
            "the values of fixed-choice arguments are shown. The node's own checks, people and flows are left out "
            f"({ev.left_out} calls)."), ""]
    if ev.sessions:
        out += ["### Who made the calls", "", *_by_who(ev.sessions), "", "### Patterns", "",
                *patterns([s.calls for s in ev.sessions]), "", "### Sessions", ""]
        ordered = sorted(ev.sessions, key=lambda s: (s.kind == "agent", -sum(not c.ok for c in s.calls)))
        for s in ordered[:max_sessions]:
            out += [f"#### {s.sid}: {s.who} ({s.kind}), {len(s.calls)} calls", *_calls(s.calls, max_calls), ""]
        if len(ev.sessions) > max_sessions:
            out += [f"{len(ev.sessions) - max_sessions} more sessions are counted in the patterns above.", ""]
    else:
        out += ["No agent calls in this window.", ""]
    if ev.probes:
        failed = [p for p in ev.probes if not p.passed]
        out += ["## Probe tasks with known outcomes", "",
                ("These ran on test pages, where each task's outcome was checked. A task can end with no error and "
                "still fail: a wrong button, or a result that does not work. Names are replaced."), "",
                f"- {len(ev.probes)} tasks, {len(failed)} failed.", *patterns([p.calls for p in ev.probes]), ""]
        for p in sorted(ev.probes, key=lambda p: p.passed):
            head = (f"#### {p.tid}: {'passed' if p.passed else 'FAILED'} ({p.executor}, mode {p.mode}"
                    f"{', with an adversarial page' if p.adversarial else ''}), {len(p.calls)} calls"
                    f"{f', and the run said {p.said}' if p.said else ''}")
            out.append(head)
            out += [f"- check failed: {c}" for c in p.failed_checks]
            out += [f"- gate failed: {g}" for g in p.failed_gates]
            out += [*_calls(p.calls, max_calls), ""]
    seen = sorted({c.code for run in ([s.calls for s in ev.sessions] + [p.calls for p in ev.probes])
                   for c in run if c.code})
    if seen:
        out += ["## The error codes above, from the error catalog", ""]
        for code in seen:
            found = codes.meaning(code)
            out.append(f"- {code}: {found[0]} The agent was told: \"{found[1]}\"" if found else f"- {code}: not in the catalog")
        out.append("")
    return "\n".join(out).rstrip() + "\n"
