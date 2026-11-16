"""Which processes the kernel killed for memory while a workload ran on a VM.

A job whose child the kernel OOM-killed kept saying `running` (field report 2026-11-16: turbo's API
child, 4.1 GB, on an 8 GB box): the parent lived on and printed nothing. The local ssh client's cgroup
never sees that. The VM's kernel log does: each kill is two lines,

    [ 1234.56] oom-kill:constraint=CONSTRAINT_NONE,...,task_memcg=/user.slice/user-0.slice/session-7.scope,task=node,pid=4242,uid=0
    [ 1234.57] Out of memory: Killed process 4242 (node) total-vm:..kB, anon-rss:4100000kB, file-rss:..kB, shmem-rss:..kB, ...

so the node reads it over ssh (`probe_script`) and keeps the kills since the workload started.

Which kills are the workload's: every `ssh host cmd` is a login session of its own on Ubuntu (sshd with
PAM, pam_systemd), so a workload's processes live in their own session scope, and the kernel names the
victim's memory cgroup (task_memcg). The workload's shell writes its start (the VM's uptime) and that
cgroup to `start_file(wid)` before it runs the command (`record`), only when the cgroup is a session
scope with memory accounting of its own; a kill in that cgroup is `in_job`. Without one (a docker
machine, where dmesg is refused anyway) kills since the start are still listed, `in_job` unknown.
"""

from __future__ import annotations

import re
import shlex
from datetime import UTC, datetime

START_SUFFIX = ".start"
PROBE_TIMEOUT_S = 8.0
KEEP_LINES = 60          # of the kernel log's oom lines, the newest
STALE_MINUTES = 1440     # a start file nobody probed (a workload killed before its shell cleaned up)
MAX_KILLS = 20           # kept per workload

_TIME = re.compile(r"^\[\s*(\d+\.\d+)\]\s*(.*)$")
_KILLED = re.compile(r"Killed process (\d+) \((.*?)\) total-vm:\d+kB, anon-rss:(\d+)kB, file-rss:(\d+)kB, "
                     r"shmem-rss:(\d+)kB")
_OOM_KILL = re.compile(r"oom-kill:.*?task_memcg=([^,]*),task=[^,]*,pid=(\d+)")
_SESSION = re.compile(r"^/[A-Za-z0-9_./@-]*/session-[A-Za-z0-9]+\.scope$")


def start_file(pgid_dir: str, wid: str) -> str:
    return f"{pgid_dir}/{wid}{START_SUFFIX}"


def record(path: str) -> str:
    """For the workload's shell (a step of agentos.vms.ssh's group script, before the command):
    write `U <uptime>` and, for a session scope with its own memory accounting, `C <cgroup>`."""
    return ("{ read u _ < /proc/uptime; echo \"U $u\"; c=$(sed -n \"s/^0:://p\" /proc/self/cgroup); "
            "[ -e \"/sys/fs/cgroup$c/memory.events\" ] && echo \"C $c\"; } > " + path + " 2>/dev/null")


def probe_script(pgid_dir: str, wid: str, *, final: bool) -> str:
    """Prints the start file, `N <uptime> <epoch>`, and the kernel log's oom lines; `final` removes
    the start file (the workload is over and read)."""
    s = shlex.quote(start_file(pgid_dir, wid))
    lines = [f"[ -r {s} ] && cat {s}",
             'read u _ < /proc/uptime; echo "N $u $(date +%s)"',
             f"dmesg 2>/dev/null | grep -E 'oom-kill:|Killed process [0-9]+ ' | tail -n {KEEP_LINES}"]
    if final:
        lines.append(f"rm -f {s}")
    lines.append(f"find {shlex.quote(pgid_dir)} -maxdepth 1 -name '*{START_SUFFIX}' -mmin +{STALE_MINUTES} "
                 "-delete 2>/dev/null")
    lines.append("true")
    return "\n".join(lines) + "\n"


def parse(text: str, *, elapsed_s: float) -> list[dict]:
    """The kills since the workload started, oldest first: pid, name, rss_mb, at (UTC), in_job
    (True, False, or None when the workload's cgroup is unknown). Without a start file the start is
    `elapsed_s` (the node's count) before now, with a little room."""
    start: float | None = None
    cgroup: str | None = None
    now_up: float | None = None
    now_epoch: float | None = None
    memcg: dict[str, str] = {}
    kills: list[tuple[float, int, str, int]] = []
    for line in text.splitlines():
        if line.startswith("U "):
            start = _num(line[2:])
        elif line.startswith("C "):
            c = line[2:].strip()
            cgroup = c if _SESSION.match(c) else None
        elif line.startswith("N "):
            parts = line.split()
            if len(parts) >= 3:
                now_up, now_epoch = _num(parts[1]), _num(parts[2])
        else:
            m = _TIME.match(line)
            if not m:
                continue
            t, rest = float(m.group(1)), m.group(2)
            k = _OOM_KILL.search(rest)
            if k:
                memcg[k.group(2)] = k.group(1)
                continue
            k = _KILLED.search(rest)
            if k:
                rss_kb = int(k.group(3)) + int(k.group(4)) + int(k.group(5))
                kills.append((t, int(k.group(1)), k.group(2), rss_kb))
    if now_up is None or now_epoch is None:
        return []
    if start is None:
        start = now_up - max(0.0, elapsed_s) - 2.0
    out = []
    for t, pid, name, rss_kb in kills:
        if t < start - 0.5:
            continue
        where = memcg.get(str(pid))
        in_job = None if cgroup is None or where is None else where == cgroup
        at = datetime.fromtimestamp(now_epoch - (now_up - t), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append({"pid": pid, "name": name[:64], "rss_mb": round(rss_kb / 1024), "at": at, "in_job": in_job})
    return out[-MAX_KILLS:]


def merge(have: list[dict] | None, new: list[dict] | None) -> list[dict]:
    """`have` and the kills of `new` it has not got (a kill is its pid and time)."""
    out = list(have or [])
    seen = {(k.get("pid"), k.get("at")) for k in out}
    for k in new or []:
        if (k.get("pid"), k.get("at")) not in seen:
            out.append(k)
            seen.add((k.get("pid"), k.get("at")))
    return out[-MAX_KILLS:]


def note(kills: list[dict] | None, *, running: bool, what: str = "job") -> str | None:
    """One line for the agent: who was killed, when, and what to do."""
    if not kills:
        return None
    parts = []
    for k in kills[-3:]:
        who = f"pid {k['pid']} ({k['name']}, {_size(k.get('rss_mb') or 0)})"
        when = f"at {str(k.get('at', ''))[11:19]} UTC"
        if k.get("in_job") is True:
            parts.append(f"the kernel killed {who} of this {what} {when}: out of memory")
        elif k.get("in_job") is False:
            parts.append(f"the kernel killed {who}, not of this {what}, {when}: out of memory")
        else:
            parts.append(f"the kernel killed {who} on this machine {when}, while this {what} ran: out of memory")
    more = len(kills) - 3
    text = "; ".join(parts) + (f" (and {more} more, see oom)" if more > 0 else "")
    text = text[0].upper() + text[1:] + "."
    if running and any(k.get("in_job") is not False for k in kills):
        text += (" The job runs on without it and may hang: lane_job_cancel it, then run less at once or open a "
                 "bigger size.")
    else:
        text += " Run less at once, or open a bigger size."
    return text


def _size(mb: int) -> str:
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb} MB"


def _num(s: str) -> float | None:
    try:
        return float(s.strip())
    except ValueError:
        return None
