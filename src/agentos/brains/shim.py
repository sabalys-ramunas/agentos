"""Per-attempt supervisor: `python -m agentos.brains.shim <attempt_dir>`.

The service starts one shim per attempt, detached (own session, and on a systemd node its
own transient scope), so a task survives the MCP caller disconnecting and the daemon
restarting. The shim:

- reads `launch.json` (argv, cwd, non-secret env, limits) and, from stdin, the secret env
  (credential and child grant token) plus the values to redact; secrets never touch disk
  here and never appear in argv;
- runs the CLI in its own process group as the unprivileged brain user, with rlimits, through
  agentos.brains.launch: dropping to the user itself when it is root, or through the agentos-runas
  helper (`launcher` in launch.json) when agentosd runs as its own unprivileged user;
- streams stdout and stderr into a rotated, redacted transcript and parses accounting;
- enforces turn, token, cost, wall-clock, output and stall limits;
- cancels gracefully (SIGINT, then SIGTERM, then SIGKILL to the group), then sweeps any
  process left in its scope;
- keeps `status.json` current (heartbeat) and writes the final outcome there.

The daemon is the only SQLite writer; the shim only writes files in its attempt dir.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import sys
import time
from pathlib import Path
from typing import Any

from agentos.brains import launch as launch_mod
from agentos.brains.parsers import make_parser
from agentos.brains.transcript import Redactor, TranscriptWriter

HEARTBEAT_SECONDS = 2.0
TICK = 0.25
RESULT_KEEP = 16_000


def write_json_atomic(path: Path, data: dict[str, Any], mode: int = 0o640) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, default=str)
    os.replace(tmp, path)


class Shim:
    def __init__(self, attempt_dir: Path, launch: dict[str, Any], secrets: dict[str, Any]):
        self.dir = attempt_dir
        self.launch = launch
        self.limits = launch.get("limits", {})
        self.secret_env: dict[str, str] = dict(secrets.get("env") or {})
        redact_values = list(secrets.get("redact") or []) + list(self.secret_env.values())
        self.redact = Redactor(redact_values)
        tcfg = launch.get("transcript", {})
        self.writer = TranscriptWriter(
            attempt_dir, seq_start=int(tcfg.get("seq_start", 1)),
            segment_bytes=int(tcfg.get("segment_bytes", 8_000_000)),
            max_segments=int(tcfg.get("max_segments", 6)), redactor=self.redact)
        self.parser = make_parser(launch["runner"])
        self.status: dict[str, Any] = {
            "state": "starting", "pid": os.getpid(), "child_pid": None, "task_id": launch.get("task_id"),
            "attempt": launch.get("attempt"), "started_at": time.time(), "heartbeat_at": time.time(),
        }
        self.stop_reason: tuple[str, str] | None = None
        self.cancel_requested = False
        self.proc: launch_mod.Proc | None = None
        self.pipes: dict[str, int] = {}
        self.last_output = time.time()
        self.extra_bytes = 0
        self.raw_bytes = 0
        self._last_beat = 0.0

    # ---- status ----
    def save(self, **fields: Any) -> None:
        self.status.update(fields)
        self.status["heartbeat_at"] = time.time()
        self.status["last_seq"] = self.writer.seq
        self.status["accounting"] = self.parser.snapshot()
        self.status["cost_estimate_usd"] = self.cost_estimate()
        write_json_atomic(self.dir / "status.json", self.status)

    def note(self, text: str, **extra: Any) -> None:
        self.writer.write({"kind": "agentos", "text": text, **extra})

    def cost_estimate(self) -> float | None:
        if self.parser.cost_usd is not None and self.launch.get("reports_cost"):
            return self.parser.cost_usd
        pin = self.limits.get("price_in_per_mtok")
        pout = self.limits.get("price_out_per_mtok")
        if pin is None and pout is None:
            return self.parser.cost_usd if self.launch.get("reports_cost") else None
        return round(((self.parser.input_tokens + self.parser.cache_write_tokens) * (pin or 0)
                      + self.parser.output_tokens * (pout or 0)) / 1e6, 6)

    # ---- process ----
    def launcher(self) -> launch_mod.Launcher:
        return launch_mod.from_spec(self.launch.get("launcher"), self.launch.get("run_as"))

    def spawn(self) -> bool:
        env = {**self.launch.get("env", {}), **self.secret_env}
        spec = self.launch.get("launcher") or {}
        stdin_fd = -1
        out_r, out_w = os.pipe()
        err_r, err_w = os.pipe()
        try:
            # the prompt: on stdin, never in argv (ps)
            stdin_fd = os.open(self.launch["stdin_file"] if self.launch.get("stdin_file") else os.devnull, os.O_RDONLY)
            self.proc = self.launcher().spawn(
                self.launch["argv"], env=env, cwd=self.launch.get("cwd") or "/", stdin=stdin_fd, stdout=out_w,
                stderr=err_w, rlimits=self.launch.get("rlimits") or {}, scope=spec.get("scope"),
                limits=spec.get("limits"))
        except Exception as exc:  # noqa: BLE001 - OSError here, RunasError from the helper
            for fd in (out_r, err_r):
                os.close(fd)
            detail = getattr(exc, "message", None) or getattr(exc, "strerror", None) or exc
            reason = f"could not start {Path(self.launch['argv'][0]).name}: {detail}"
            self.note(reason)
            self.finish("spawn_failed", reason, exit_code=None)
            return False
        finally:
            for fd in (out_w, err_w, stdin_fd):
                if fd >= 0:
                    os.close(fd)
        self.pipes = {"stdout": out_r, "stderr": err_r}
        self.secret_env.clear()
        self.note(f"started {Path(self.launch['argv'][0]).name} (pid {self.proc.pid})")
        self.save(state="running", child_pid=self.proc.pid, pgid=self.proc.pid)
        return True

    def signal_group(self, sig: int) -> None:
        if self.proc:
            self.proc.signal_group(sig)

    def request_stop(self, outcome: str, reason: str) -> None:
        if self.stop_reason is None:
            self.stop_reason = (outcome, reason)
            self.note(f"stopping: {reason}")
            self._stop_started = time.time()
            self._stop_stage = 0
            self.signal_group(signal.SIGINT)

    def _advance_stop(self) -> None:
        if self.stop_reason is None or not self.proc or self.proc.poll() is not None:
            return
        grace = float(self.limits.get("grace_seconds", 10))
        elapsed = time.time() - self._stop_started
        if self._stop_stage == 0 and elapsed > grace:
            self._stop_stage = 1
            self.signal_group(signal.SIGTERM)
        elif self._stop_stage == 1 and elapsed > 2 * grace + 1:
            self._stop_stage = 2
            self.note("did not exit after SIGTERM; killing the process group")
            self.signal_group(signal.SIGKILL)

    # ---- limits ----
    def check_limits(self) -> None:
        if self.stop_reason:
            return
        lim = self.limits
        now = time.time()
        if self.cancel_requested or (self.dir / "cancel").exists():
            self.request_stop("cancelled", "cancelled by the owner or the calling agent")
            return
        if lim.get("max_seconds") and now - self.status["started_at"] > float(lim["max_seconds"]):
            self.request_stop("limit_wall", f"wall-clock limit of {int(float(lim['max_seconds']) // 60)} min reached")
        elif lim.get("max_turns") and self.parser.turns > int(lim["max_turns"]):
            self.request_stop("limit_turns", f"turn limit of {lim['max_turns']} reached")
        elif lim.get("max_tokens") and self.parser.tokens > int(lim["max_tokens"]):
            self.request_stop("limit_tokens", f"token limit of {lim['max_tokens']} reached")
        elif lim.get("max_usd") is not None and (self.cost_estimate() or 0) > float(lim["max_usd"]):
            self.request_stop("limit_cost", f"budget of ${float(lim['max_usd']):.2f} reached")
        elif lim.get("max_output_bytes") and self.raw_bytes > int(lim["max_output_bytes"]):
            self.request_stop("limit_output", "output limit reached (runaway output)")
        elif lim.get("stall_seconds") and now - self.last_output > float(lim["stall_seconds"]):
            self.request_stop("stalled", f"no output for {int(float(lim['stall_seconds']))} s (hang)")

    # ---- output ----
    def on_line(self, stream: str, raw: bytes) -> None:
        self.last_output = time.time()
        text = raw.decode("utf-8", errors="replace")
        if stream == "stdout":
            for ev in self.parser.feed(self.redact(text)):
                self.writer.write({"stream": "stdout", **ev})
        else:
            self.extra_bytes += len(raw)
            stripped = text.rstrip()
            if stripped:
                self.writer.write({"stream": "stderr", "kind": "stderr", "text": stripped[:2000]})

    def pump(self) -> int | None:
        assert self.proc and self.pipes
        sel = selectors.DefaultSelector()
        bufs: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        skipping: dict[str, bool] = {"stdout": False, "stderr": False}
        max_line = int(self.limits.get("max_line_bytes", 1_000_000))
        for name, fd in self.pipes.items():
            os.set_blocking(fd, False)
            sel.register(fd, selectors.EVENT_READ, name)
        open_streams = 2
        exited_at: float | None = None
        while True:
            for key, _ in sel.select(timeout=TICK):
                name = key.data
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    sel.unregister(key.fileobj)
                    open_streams -= 1
                    continue
                self.raw_bytes += len(chunk)
                buf = bufs[name]
                buf.extend(chunk)
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        if len(buf) > max_line:
                            if not skipping[name]:
                                self.on_line(name, bytes(buf[:max_line]) + b"...[line truncated]")
                                self.writer.write({"kind": "agentos", "text": "a line over the size cap was cut"})
                            self.extra_bytes += len(buf)
                            skipping[name] = True
                            buf.clear()
                        break
                    line = bytes(buf[:nl])
                    del buf[:nl + 1]
                    if skipping[name]:
                        skipping[name] = False
                        self.extra_bytes += len(line)
                        continue
                    self.on_line(name, line)
            now = time.time()
            self.check_limits()
            self._advance_stop()
            if now - self._last_beat > HEARTBEAT_SECONDS:
                self._last_beat = now
                self.save()
            code = self.proc.poll()
            if code is not None:
                if open_streams == 0:
                    break
                exited_at = exited_at or now
                if now - exited_at > 2.0:  # a grandchild holds the pipe open; stop reading
                    break
        for name, buf in bufs.items():
            if buf and not skipping[name]:
                self.on_line(name, bytes(buf))
        sel.close()
        for fd in self.pipes.values():
            os.close(fd)
        return self.proc.wait()

    def sweep(self) -> None:
        """Kill anything still running in the task: the process group, then (in a systemd
        scope) every other process in our own cgroup. Through the helper, the helper does both."""
        self.signal_group(signal.SIGKILL)
        if self.proc:
            self.proc.sweep()
            self.proc.close()
        unit = self.launch.get("unit")
        if not unit:
            return
        try:
            cg = Path("/proc/self/cgroup").read_text().strip().split("::", 1)[-1]
        except OSError:
            return
        if not cg.endswith(unit):
            return
        procs = Path("/sys/fs/cgroup") / cg.lstrip("/") / "cgroup.procs"
        try:
            pids = [int(p) for p in procs.read_text().split()]
        except (OSError, ValueError):
            return
        for pid in pids:
            if pid != os.getpid():
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    def finish(self, outcome: str, reason: str, exit_code: int | None) -> None:
        result = self.parser.result_text
        if result:
            result = self.redact(result)[:RESULT_KEEP]
        self.note(f"finished: {outcome} ({reason})")
        self.writer.close()
        self.save(state="exited", ended_at=time.time(), exit_code=exit_code, outcome=outcome,
                  reason=self.redact(reason)[:600], result_text=result,
                  resets_at=self.parser.resets_at, signal=self.parser.signal)

    def run(self) -> int:
        self.save()
        if self.cancel_requested or (self.dir / "cancel").exists():
            self.finish("cancelled", "cancelled before the brain started", exit_code=None)
            return 0
        if not self.spawn():
            return 0
        exit_code = self.pump()
        self.sweep()
        if self.stop_reason:
            outcome, reason = self.stop_reason
            # the agent already delivered a clean result (a leftover child kept the pipe open)
            if outcome in ("stalled", "limit_wall") and self.parser.final_seen \
                    and self.parser.result_is_error is False:
                outcome, reason = "succeeded", "finished"
        else:
            outcome, reason = self.parser.outcome(exit_code)
        if exit_code is not None and exit_code < 0 and not self.stop_reason and outcome == "crashed":
            reason = f"killed by signal {-exit_code}"
        self.finish(outcome, reason, exit_code)
        return 0


_CANCEL: dict[str, Any] = {"requested": False, "shim": None}


def _on_term(signum, frame):
    _CANCEL["requested"] = True
    if _CANCEL["shim"] is not None:
        _CANCEL["shim"].cancel_requested = True


def main(argv: list[str] | None = None) -> int:
    # first thing: a cancel that arrives while we start up must not kill us silently
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python -m agentos.brains.shim <attempt_dir>", file=sys.stderr)
        return 2
    attempt_dir = Path(argv[0])
    launch = json.loads((attempt_dir / "launch.json").read_text())
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        sys.stdin.close()
    except OSError:
        pass
    secrets = json.loads(raw) if raw.strip() else {}
    shim = Shim(attempt_dir, launch, secrets)
    del raw, secrets
    shim.cancel_requested = _CANCEL["requested"]
    _CANCEL["shim"] = shim
    return shim.run()


if __name__ == "__main__":
    sys.exit(main())
