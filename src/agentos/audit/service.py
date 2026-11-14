"""The audit service: verify the action log, replay one lane's day, export a day as a bundle an
auditor checks offline, and share one trace or one lane-day read-only.

It reads the policy's trace store (runtime "traces", agentos.policy.traces) and the chain
primitives it is built on (agentos.policy.auditlog, specified in policy/AUDIT-LOG.md). Grant
labels come from runtime "policy" and runs with their proof of done from runtime "runner"; both
are optional (a replay without them says less, never fails). Share links live in their own
file, `$AGENTOS_HOME/audit.db`.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import time
import zipfile
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agentos.audit.captures import CAPTURE_KIND
from agentos.audit.errors import BundleInvalid, ShareInvalid
from agentos.core.errors import InvalidRequest, NotFound
from agentos.policy import auditlog
from agentos.policy.auditlog import BUNDLE_CTX, SHARE_CTX, Report, Scope, Verifier, canonical, sig_ok
from agentos.policy.store import DB
from agentos.policy.traces import TraceEvent
from agentos.policy.util import b64u, b64u_decode, iso, new_id, parse_duration

log = logging.getLogger("agentos.audit")

BUNDLE_FORMAT = "agentos-audit-bundle"
SHARE_PREFIX = "ash_"
SHARE_DEFAULT_S = 24 * 3600
SHARE_MAX_S = 7 * 86400
MAX_STEPS = 2000
MAX_FILE_BYTES = 20_000_000   # a screenshot bigger than this is left out of a bundle, and the manifest says so
CHAIN_ORDER = {"tampered": 3, "unchained": 2, "unprovable": 1, "ok": 0}

SCHEMA = """
CREATE TABLE IF NOT EXISTS shares (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL, screenshots INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, expires_at REAL NOT NULL, revoked_at REAL, by TEXT NOT NULL,
  views INTEGER NOT NULL DEFAULT 0, last_view_at REAL
);
"""


# ---------------------------------------------------------------- days

def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        raise InvalidRequest(f"unknown timezone {name!r}") from None


def today(tz: str | None, now: float) -> str:
    return datetime.fromtimestamp(now, zone(tz)).date().isoformat()


def day_window(day: str, tz: str | None) -> tuple[float, float]:
    """[start, end) of a calendar day in `tz`, as Unix times."""
    try:
        d = date.fromisoformat(day.strip())
    except (AttributeError, ValueError):
        raise InvalidRequest(f"day must be a date like 2026-11-03, not {day!r}") from None
    z = zone(tz)
    start = datetime(d.year, d.month, d.day, tzinfo=z)
    end = datetime.combine(d + timedelta(days=1), datetime.min.time(), tzinfo=z)
    return start.timestamp(), end.timestamp()


def _args(row: dict) -> Any:
    if not row.get("args"):
        return None
    try:
        return json.loads(row["args"])
    except ValueError:
        return row["args"]


def _sha256_file(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- the service

class AuditService:
    def __init__(self, traces: Any, *, policy: Any = None, runner: Any = None, home: Path | None = None,
                 clock: Callable[[], float] = time.time, node: str | None = None):
        self.traces = traces
        self.policy = policy
        self.runner = runner
        self.home = Path(home) if home is not None else traces.path.parent
        self.clock = clock
        self.node = node
        self._db: DB | None = None

    @classmethod
    def from_runtime(cls, runtime: Any) -> AuditService:
        traces = runtime.get("traces")
        if traces is None or not hasattr(traces, "chain_rows"):
            from agentos.core.errors import ServiceUnavailable
            raise ServiceUnavailable("The action log is not configured on this node.")
        cfg = runtime.get("config")
        policy = runtime.get("policy")
        return cls(traces, policy=policy, runner=runtime.get("runner"), home=getattr(cfg, "home", None),
                   clock=getattr(policy, "clock", time.time), node=getattr(cfg, "name", None))

    @property
    def db(self) -> DB:
        if self._db is None:
            self._db = DB(self.home / "audit.db", SCHEMA)
        return self._db

    # ---- verify ----
    def verify(self, *, since: float | None = None, until: float | None = None, lane: str | None = None) -> Report:
        return self.traces.verify(scope=Scope(since=since, until=until, lane=lane))

    def _range_report(self, lo: int, hi: int, scope: Scope) -> Report:
        """Verify from just before `lo` to the first checkpoint at or after `hi` (or the end)."""
        cover = next((json.loads(m["body"])["seq"] for m in self.traces.marks()
                      if m["kind"] == "checkpoint" and json.loads(m["body"]).get("seq", 0) >= hi), None)
        return self.traces.verify(scope=scope, from_seq=max(0, lo - 1), to_seq=cover, trust_note=False)

    # ---- labels, runs, files ----
    def grant_label(self, grant_id: str | None, cache: dict[str, str | None] | None = None) -> str | None:
        """A plain label for the grant behind a call: who, how far it may go, where, and its note."""
        if not grant_id:
            return None
        if cache is not None and grant_id in cache:
            return cache[grant_id]
        label: str | None
        if grant_id == "owner":
            label = "the owner"
        else:
            label = None
            record = getattr(self.policy, "grant_record", None)
            if record is not None:
                try:
                    label = _label(record(grant_id))
                except Exception:  # noqa: BLE001  (a grant this node no longer has: the id alone)
                    label = None
        if cache is not None:
            cache[grant_id] = label
        return label

    def run_view(self, run_id: str | None) -> dict | None:
        if not run_id or self.runner is None:
            return None
        try:
            if hasattr(self.runner, "describe") and hasattr(self.runner, "get"):
                d = self.runner.describe(self.runner.get(run_id))
            else:
                d = self.runner.get_run(run_id)
        except Exception:  # noqa: BLE001  (a run the runner no longer has)
            return None
        if not isinstance(d, dict):
            return None
        proof = d.get("proof") if isinstance(d.get("proof"), dict) else None
        error = d.get("error") if isinstance(d.get("error"), dict) else None
        return {"id": d.get("id"), "flow": d.get("flow"), "status": d.get("status"), "lane": d.get("lane"),
                "summary": d.get("summary"),
                "proof": {"url": proof.get("url"), "readbacks": proof.get("readbacks"), "note": proof.get("note"),
                          "screenshot": proof.get("screenshot")} if proof else None,
                "error_screenshot": error.get("screenshot") if error else None}

    def screenshot_path(self, run: dict | None) -> Path | None:
        """The file behind a run's proof (or failure) screenshot, if it is still on the node and
        inside the runner's own folder."""
        if not run:
            return None
        rel = (run.get("proof") or {}).get("screenshot") or run.get("error_screenshot")
        if not isinstance(rel, str) or not rel or rel.startswith("/api/"):
            return None
        root = (self.home / "runner" / "shots").resolve()
        path = (self.home / rel).resolve() if not Path(rel).is_absolute() else Path(rel).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return None
        return path

    # ---- approvals behind a step ----
    def approval_view(self, route: str, via: str | None, *, shared: bool = False) -> dict:
        """Who allowed a step that needed a person, how, when, and what they were shown: the approval
        (or standing approval) an `authorize` row names, read from the approvals table. Ids stay out
        of a shared view; the grant a decision was made with is never shown."""
        out: dict[str, Any] = {"route": route, "id": None if shared else via, "found": False}
        store = getattr(self.policy, "approvals", None)
        db = getattr(store, "db", None)
        if not via or db is None:
            return out
        from agentos.policy.approval_models import _row_to_approval, _row_to_standing

        try:
            if route == "approved":
                row = db.one("SELECT * FROM approvals WHERE id = ?", (via,))
                if row is None:
                    return out
                ap = _row_to_approval(row)
                out.update(found=True, by=ap.decided_by, at=iso(ap.decided_at), how=ap.decided_via,
                           level=ap.decided_level, requires=ap.requires, summary=ap.summary,
                           after_untrusted=ap.follows_words())
                if not shared:  # a share shows that a person said yes and how, not the card they read
                    out.update(method=ap.decided_method, preview=ap.preview, asked_at=iso(ap.created_at),
                               edited=sorted((ap.edit or {}).get("params") or {}) or None)
            elif route == "standing":
                row = db.one("SELECT * FROM standing WHERE id = ?", (via,))
                if row is None:
                    return out
                sa = _row_to_standing(row)
                out.update(found=True, by=sa.created_by, at=iso(sa.created_at), how="standing",
                           expires_at=iso(sa.expires_at))
                if shared:  # what else it covers (other lanes and workspaces) and its note stay with the owner
                    out.update(summary="A standing approval the owner made earlier")
                else:
                    out.update(summary=_standing_words(sa), note=sa.note)
        except Exception:
            log.debug("approval %s could not be read for a replay", via, exc_info=True)
        return out

    # ---- replay ----
    def replay(self, lane: str, day: str, *, tz: str | None = None, viewer_path: str | None = None,
               owner: bool = True, screenshot_url: Callable[[str], str] | None = None,
               shared: bool = False, capture_url: Callable[[str], str] | None = None) -> dict:
        """One lane's actions on one day, as steps (docs/API.md "Audit replay")."""
        _replay_lane(lane)
        since, until = day_window(day, tz)
        rows = self.traces.lane_rows(lane, since, until, viewer_path=viewer_path, limit=MAX_STEPS * 4 + 1)
        truncated = len(rows) > MAX_STEPS * 4
        rows = rows[:MAX_STEPS * 4]
        lo_day, hi_day = self.traces.seq_bounds(since, until)
        report = self._range_report(lo_day, hi_day, Scope(since=since, until=until, lane=lane)) \
            if rows and lo_day is not None else None
        steps = self._steps(rows, report, screenshot_url=screenshot_url, shared=shared, capture_url=capture_url)
        if len(steps) > MAX_STEPS:
            steps, truncated = steps[:MAX_STEPS], True
        seqs = {r["seq"] for r in rows}
        chain = _chain_summary(report, steps, seqs=None if owner else seqs)
        return {"lane": lane, "day": day, "timezone": zone(tz).key, "from": iso(since), "to": iso(until),
                "chain": chain, "steps": steps, "truncated": truncated}

    def lanes_on(self, day: str, *, tz: str | None = None, viewer_path: str | None = None,
                 allowed: Callable[[str], bool] | None = None) -> dict:
        """The lanes with actions on a day (what a replay can be asked for), with their row counts."""
        since, until = day_window(day, tz)
        lanes = [{"lane": lane, "rows": n} for lane, n in self.traces.lane_counts(since, until, viewer_path=viewer_path)
                 if allowed is None or allowed(lane)]
        return {"day": day, "timezone": zone(tz).key, "from": iso(since), "to": iso(until), "lanes": lanes}

    def _steps(self, rows: list[dict], report: Report | None, *, screenshot_url: Callable[[str], str] | None,
               shared: bool, capture_url: Callable[[str], str] | None = None) -> list[dict]:
        calls: dict[str, dict] = {}
        order: list[dict] = []
        for r in rows:
            if r["kind"] == "tool_start":
                calls[r["id"]] = item = {"start": r, "end": None, "events": []}
                order.append(item)
            elif r["kind"] == "tool_end" and r["parent_id"] in calls and calls[r["parent_id"]]["end"] is None:
                calls[r["parent_id"]]["end"] = r
            elif r["parent_id"] in calls:
                calls[r["parent_id"]]["events"].append(r)
            else:
                order.append({"start": r, "end": None, "events": []})
        labels: dict[str, str | None] = {}
        return [self._step(i, report, labels, screenshot_url, shared, capture_url) for i in order]

    def _step(self, item: dict, report: Report | None, labels: dict, screenshot_url: Callable[[str], str] | None,
              shared: bool, capture_url: Callable[[str], str] | None = None) -> dict:
        start, end, events = item["start"], item["end"], item["events"]
        main = end or start
        members = [r for r in (start, end, *events) if r is not None]
        statuses = {r["seq"]: (report.row_status(r) if report is not None else "unprovable") for r in members}
        worst = max(statuses.values(), key=lambda s: CHAIN_ORDER[s])
        run, shot = self._step_run(main, screenshot_url)
        grant_id = main.get("grant_id")
        decisions = [self.approval_view(route, via, shared=shared)
                     for route, via in (_decision(e) for e in events) if route in ("approved", "standing")]
        capture = _capture_of(events)
        if shot is None and capture is not None and capture_url is not None and self.capture_path(capture):
            shot = capture_url(capture["trace"])
        step = {
            **_call_fields(start, end, main),
            "principal": {"kind": main.get("principal_kind"), "id": main.get("principal")}
            if main.get("principal") else None,
            "grant": {"id": None if shared else grant_id, "label": self.grant_label(grant_id, labels)}
            if grant_id else None,
            "run": run, "screenshot": shot,
            "approvals": decisions, "after_untrusted": _after_untrusted(events),
            "capture": capture,
            "events": [{"seq": e["seq"], "at": iso(e["at"]), "kind": e["kind"], "status": e["status"],
                        "summary": e["summary"], "args": _args(e)} for e in events],
            "chain": {"status": worst, "rows": [{"seq": s, "status": st} for s, st in sorted(statuses.items())]},
        }
        return step

    def _step_run(self, main: dict, screenshot_url: Callable[[str], str] | None) -> tuple[dict | None, str | None]:
        """The run a step belongs to, without its screenshots, and the link to its proof screenshot."""
        run = self.run_view(main.get("run_id"))
        shot = None
        if run is not None and self.screenshot_path(run) is not None and screenshot_url is not None:
            shot = screenshot_url(run["id"])
        if run is not None:
            run = {**run, "proof": {k: v for k, v in (run.get("proof") or {}).items() if k != "screenshot"}
                   if run.get("proof") else None}
            run.pop("error_screenshot", None)
        return run, shot

    # ---- one trace ----
    def trace_view(self, trace_id: str, *, screenshot_url: Callable[[str], str] | None = None,
                   shared: bool = False) -> dict:
        row = self.traces.row(trace_id)
        if row is None:
            raise NotFound(f"No trace {trace_id}.")
        call_id = row["id"] if row["kind"] == "tool_start" or not row["parent_id"] else row["parent_id"]
        start = self.traces.row(call_id) or row
        rows = [start]
        if start["kind"] == "tool_start":
            rows += self.traces.children(start["id"])
        if all(r["id"] != row["id"] for r in rows):
            rows.append(row)
        rows.sort(key=lambda r: r["seq"])
        lo, hi = rows[0]["seq"], rows[-1]["seq"]
        report = self._range_report(lo, hi, Scope(lane=start["lane"]) if start["lane"] else Scope())
        steps = self._steps(rows, report, screenshot_url=screenshot_url, shared=shared)
        step = next((s for s in steps if s["id"] == start["id"]), steps[0])
        return {"trace": trace_id, "lane": start["lane"], "step": step,
                "chain": _chain_summary(report, [step], seqs={r["seq"] for r in rows})}

    # ---- one HTML file ----
    def replay_page(self, *, lane: str | None = None, day: str | None = None, trace: str | None = None,
                    tz: str | None = None, screenshots: bool = False, include_args: bool = False,
                    by: str = "owner") -> dict:
        """A lane's day (or one trace) as one self-contained HTML file anyone can open offline
        (agentos.audit.replay_page). It shows what a share link shows, and less: no grant ids,
        screenshots only with `screenshots` (at most replay_page.EMBED_BYTES of them in all), and
        what was typed, the arguments' values and the query of addresses only with `include_args`.
        A checkpoint is signed first, as an export does, so the steps up to now are covered. Returns
        {html, steps, images, left_out, mismatched, chain}."""
        from agentos.audit import replay_page

        self._check_replay_target(lane=lane, day=day, trace=trace, tz=tz)
        zone_key = zone(tz).key
        mark = (lambda run_id: f"run:{run_id}") if screenshots else None
        target = {"trace": trace} if trace else {"lane": lane, "day": day, "timezone": zone_key}
        self.traces.write(TraceEvent(kind="replay_export", tool="audit.replay_page", status="ok", principal=by,
                                     workspace=(lane or "").split("/", 1)[0] or None,
                                     summary="replay saved as an HTML file", args={"target": target,
                                                                                  "screenshots": screenshots,
                                                                                  "include_args": include_args}))
        self.traces.checkpoint("export")
        if trace:
            view = self.trace_view(trace, screenshot_url=mark, shared=True)
            steps = [view["step"]]
        else:
            view = self.replay(lane or "", day or "", tz=tz, owner=False, screenshot_url=mark, shared=True)
            steps = view["steps"]
        images, left_out = self._embed_images(steps, screenshots=screenshots, room=replay_page.EMBED_BYTES)
        verify = self._replay_verify(view, steps, zone_key)
        html = replay_page.render(view, steps=steps, images=images, generated_at=iso(self.clock()) or "",
                                  tz=zone_key, verify=verify, include_args=include_args)
        return {"html": html, "steps": len(steps), "images": sum(1 for i in images.values() if "data" in i),
                "left_out": left_out,
                "mismatched": sum(1 for i in images.values() if i.get("check") == "mismatch"),
                "chain": view["chain"]["status"]}

    def _check_replay_target(self, *, lane: str | None, day: str | None, trace: str | None, tz: str | None) -> None:
        """One trace, or one lane-day with both its lane and its day, each one that exists."""
        if bool(trace) == bool(lane or day):
            raise InvalidRequest("a replay page is either one trace (trace) or one lane-day (lane and day)")
        if not trace and (not lane or not day):
            raise InvalidRequest("a lane-day replay page needs both lane and day")
        if trace:
            if self.traces.row(trace) is None:
                raise NotFound(f"No trace {trace}.")
        else:
            _replay_lane(lane)
            day_window(day or "", tz)

    def _embed_images(self, steps: list[dict], *, screenshots: bool, room: int) -> tuple[dict[str, dict], int]:
        """The pictures the HTML file carries, by step id, within `room` bytes of base64, and how many
        were left out for their size. Each step loses its screenshot link and says what was left out."""
        images: dict[str, dict] = {}
        left_out = 0
        for s in steps:
            img = self._step_image(s, room=room) if screenshots else None
            if img is not None and img.get("too_big"):
                s["screenshot_left_out"], img = "size", None
                left_out += 1
            if img is not None:
                room -= _b64_len(len(img.get("data") or b""))
                images[s["id"]] = img
            elif not screenshots and s.get("run"):
                s["screenshot_left_out"] = self.screenshot_path(self.run_view(s["run"].get("id"))) is not None
            s.pop("screenshot", None)
        return images, left_out

    def _replay_verify(self, view: dict, steps: list[dict], zone_key: str) -> dict:
        """How a reader checks the file: what the chain proves, and the commands that verify it."""
        seqs = [r["seq"] for s in steps for r in s["chain"]["rows"]]
        proven = view["chain"].get("proven_through")
        on = view.get("lane")
        export = (f"agentos audit export --day {view.get('day') or _day_of(steps, zone_key)} --tz {zone_key}"
                  + (f" --lane {on}" if on else "") + " -o day.zip")
        return {"proven_through": proven, "chain": _chain_id(self.traces.marks()),
                "kid": self._mark_kid(proven.get("mark")) if proven else None,
                "seq": [min(seqs), max(seqs)] if seqs else None, "export": export,
                "verify": f"agentos audit verify --lane {on}" if on else "agentos audit verify"}

    def _mark_kid(self, n: Any) -> str | None:
        for m in self.traces.marks():
            if m["n"] == n:
                try:
                    return json.loads(m["body"]).get("kid")
                except ValueError:
                    return None
        return None

    def _step_image(self, step: dict, *, room: int | None = None) -> dict | None:
        """The picture behind a step, for the HTML file: the screenshot the node kept after the call
        (checked against the hash its row recorded), else its run's proof screenshot. One whose
        base64 would not fit in `room` is not read: {"too_big": True}."""
        def fits(path: Path) -> bool:
            try:
                return room is None or _b64_len(path.stat().st_size) <= room
            except OSError:
                return False

        cap = step.get("capture")
        if cap:
            path = self.capture_path(cap)
            if path is None:
                return {"missing": "A screenshot was kept for this step, but its file is no longer on the node."}
            if not fits(path):
                return {"too_big": True}
            data = path.read_bytes()
            mime = _image_mime(data)
            check = "ok" if _sha256_file(data) == cap["sha256"] else "mismatch"
            if mime is None:
                return {"check": "mismatch", "missing": "The screenshot kept for this step is not an image any "
                                                        "more: it was changed after it was taken."}
            return {"data": data, "mime": mime, "check": check, "alt": "The page after this step"}
        marker = step.get("screenshot")
        if isinstance(marker, str) and marker.startswith("run:"):
            path = self.screenshot_path(self.run_view(marker[4:]))
            if path is not None:
                if not fits(path):
                    return {"too_big": True}
                data = path.read_bytes()
                mime = _image_mime(data)
                if mime is not None and len(data) <= MAX_FILE_BYTES:
                    return {"data": data, "mime": mime, "check": "unrecorded", "alt": "The run's proof screenshot"}
        return None

    def capture_path(self, capture: dict | None) -> Path | None:
        """The file of a screenshot kept after a call (agentos.audit.captures), named by its hash,
        if it is still on the node."""
        from agentos.audit.captures import capture_file

        if not capture:
            return None
        path = capture_file(self.home, capture.get("sha256"), capture.get("format"))
        return path if path is not None and path.is_file() else None

    # ---- export ----
    def export(self, out: Path, *, day: str, lane: str | None = None, tz: str | None = None) -> dict:
        """Write a self-contained bundle for one day (and lane): the rows (others as stubs that keep
        the chain checkable without their content), every mark, the public keys, the screenshots
        those rows' runs kept, and a manifest signed with the audit key. A row of the lane from
        outside the day that falls inside the exported range is shipped in full, never as a stub:
        a stub of the lane could not be told from a row of the day that was left out."""
        since, until = day_window(day, tz)
        self.traces.checkpoint("export")
        lo, hi = self.traces.seq_bounds(since, until)
        if lo is None:
            raise NotFound(f"Nothing was recorded on {day} ({zone(tz).key}).")
        marks = self.traces.marks()
        cover = next((json.loads(m["body"])["seq"] for m in marks
                      if m["kind"] == "checkpoint" and json.loads(m["body"]).get("seq", 0) >= hi), None)
        lines: list[bytes] = []
        selected_ids: set[str] = set()
        runs: dict[str, dict | None] = {}
        first = last = None
        full = stubs = context = 0
        for r in self.traces.chain_rows(from_seq=lo, to_seq=cover):
            in_day = since <= (r["at"] or 0) < until
            pick = (in_day and (lane is None or r["lane"] == lane)) or (r["parent_id"] in selected_ids)
            if pick or (lane is not None and r["lane"] == lane):
                if pick:
                    selected_ids.add(r["id"])
                    full += 1
                else:
                    context += 1  # the lane on another day: in full, out of the bundle's scope
                lines.append(json.dumps(r, ensure_ascii=False, sort_keys=True).encode())
                if r["run_id"] and r["run_id"] not in runs:
                    runs[r["run_id"]] = self.run_view(r["run_id"])
            else:
                lines.append(json.dumps(auditlog.stub(r) if r["hash"] is not None else
                                        {"seq": r["seq"], "id": r["id"], "stub": True, "prev": None, "hash": None},
                                        sort_keys=True).encode())
                stubs += 1
            first = r["seq"] if first is None else first
            last = r["seq"]
        files: list[dict] = []
        blobs: dict[str, bytes] = {}
        for run_id, run in runs.items():
            path = self.screenshot_path(run)
            if path is None:
                continue
            data = path.read_bytes()
            name = f"files/{run_id}/{path.name}"
            if len(data) > MAX_FILE_BYTES:
                files.append({"path": name, "run": run_id, "left_out": "too large"})
                continue
            blobs[name] = data
            files.append({"path": name, "run": run_id, "sha256": _sha256_file(data), "size": len(data)})
        rows_blob = b"\n".join(lines) + b"\n"
        marks_blob = b"\n".join(json.dumps(m, sort_keys=True).encode() for m in marks) + b"\n"
        manifest = {
            "format": BUNDLE_FORMAT, "v": 1, "node": self.node, "chain": _chain_id(marks),
            "created_at": iso(self.clock()), "scope": {"day": day, "timezone": zone(tz).key, "lane": lane,
                                                       "since": since, "until": until},
            "rows": {"first_seq": first, "last_seq": last, "full": full, "context": context, "stubs": stubs,
                     "sha256": _sha256_file(rows_blob)},
            "marks": {"count": len(marks), "sha256": _sha256_file(marks_blob)},
            "keys": [{"kid": k, "pub": p} for k, p in self.traces.audit_keys().items()],
            "runs": [run for run in runs.values() if run],
            "files": files,
            "verify": "agentos audit verify --bundle <this file>",
        }
        manifest_bytes = canonical(manifest)
        kid, sig = self.traces.audit_sign(BUNDLE_CTX, manifest_bytes)
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", manifest_bytes)
            z.writestr("manifest.sig", json.dumps({"kid": kid, "sig": sig}))
            z.writestr("rows.jsonl", rows_blob)
            z.writestr("marks.jsonl", marks_blob)
            for name, data in blobs.items():
                z.writestr(name, data)
        out.write_bytes(buf.getvalue())
        return {"path": str(out), "rows": full, "context": context, "stubs": stubs, "first_seq": first,
                "last_seq": last,
                "files": len(blobs), "checkpoint": cover, "signed_by": kid}

    # ---- share links ----
    def share_create(self, *, trace_id: str | None = None, lane: str | None = None, day: str | None = None,
                     tz: str | None = None, ttl: str | int | None = None, screenshots: bool = False,
                     by: str = "owner") -> dict:
        """A signed, expiring, revocable read-only link to one trace or one lane-day. The token is
        returned once; only the share's id is stored."""
        if bool(trace_id) == bool(lane or day):
            raise InvalidRequest("share either one trace (trace) or one lane-day (lane and day)")
        ttl_s = SHARE_DEFAULT_S if ttl in (None, "") else parse_duration(ttl)  # type: ignore[arg-type]
        if not 0 < ttl_s <= SHARE_MAX_S:
            raise InvalidRequest("a share link lasts between 1 second and 7 days")
        if trace_id:
            if self.traces.row(trace_id) is None:
                raise NotFound(f"No trace {trace_id}.")
            kind, target = "trace", {"trace": trace_id}
        else:
            if not lane or not day:
                raise InvalidRequest("a lane-day share needs both lane and day")
            _replay_lane(lane)  # refused here, before anything is signed, as replay would refuse it
            day_window(day, tz)
            kind, target = "lane_day", {"lane": lane, "day": day, "timezone": zone(tz).key}
        sid, now = new_id("sh"), self.clock()
        exp = int(now + ttl_s)
        payload = canonical({"v": 1, "sid": sid, "kind": kind, "target": target, "exp": exp,
                             "screenshots": bool(screenshots)})
        kid, sig = self.traces.audit_sign(SHARE_CTX, payload)
        if sig is None:
            raise InvalidRequest("the audit key is not on this node, so links cannot be signed")
        self.db.execute("INSERT INTO shares (id, kind, target, screenshots, created_at, expires_at, by) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)", (sid, kind, json.dumps(target), int(bool(screenshots)),
                                                         now, exp, by))
        token = f"{SHARE_PREFIX}{b64u(payload)}.{kid.removeprefix('ed25519:') if kid else ''}.{sig}"
        return {"id": sid, "token": token, "kind": kind, "target": target, "expires_at": iso(exp),
                "screenshots": bool(screenshots)}

    def shares(self) -> list[dict]:
        now = self.clock()
        return [_share_json(r, now) for r in self.db.all("SELECT * FROM shares ORDER BY created_at DESC")]

    def share_revoke(self, sid: str, *, by: str = "owner") -> dict:
        row = self.db.one("SELECT * FROM shares WHERE id = ?", (sid,))
        if row is None:
            raise NotFound(f"No share {sid}.")
        if row["revoked_at"] is None:
            self.db.execute("UPDATE shares SET revoked_at = ? WHERE id = ?", (self.clock(), sid))
            log.info("share %s revoked by %s", sid, by)
        return _share_json(self.db.one("SELECT * FROM shares WHERE id = ?", (sid,)), self.clock())

    def share_open(self, token: str) -> dict:
        """The share a token names, after checking its signature, expiry and revocation."""
        bad = ShareInvalid("This link is not valid.")
        try:
            prefix, kid_hex, sig = str(token).removeprefix(SHARE_PREFIX).split(".")
            payload = b64u_decode(prefix)
            data = json.loads(payload)
        except (ValueError, AttributeError):
            raise bad from None
        if not str(token).startswith(SHARE_PREFIX) or not isinstance(data, dict) or canonical(data) != payload:
            raise bad
        pub = self.traces.audit_keys().get("ed25519:" + kid_hex)
        if not sig_ok(pub, SHARE_CTX, payload, sig):
            raise bad
        row = self.db.one("SELECT * FROM shares WHERE id = ?", (data.get("sid"),))
        if row is None or row["kind"] != data.get("kind") or json.loads(row["target"]) != data.get("target"):
            raise bad
        now = self.clock()
        if row["revoked_at"] is not None:
            raise ShareInvalid("This link was revoked by the owner.")
        if now >= min(float(row["expires_at"]), float(data.get("exp") or 0)):
            raise ShareInvalid("This link has expired.")
        return {"id": row["id"], "kind": row["kind"], "target": data["target"],
                "screenshots": bool(row["screenshots"]), "expires_at": iso(row["expires_at"])}

    def share_view(self, token: str, *, screenshot_url: Callable[[str, str], str] | None = None,
                   via: str = "link") -> dict:
        """What a share link shows, logged as a view (in the action log and on the share)."""
        share = self.share_open(token)
        target = share["target"]
        lane = target.get("lane")
        if share["kind"] == "trace":
            row = self.traces.row(target["trace"])
            lane = row["lane"] if row else None
        url = (lambda run_id: screenshot_url(token, run_id)) if share["screenshots"] and screenshot_url else None
        if share["kind"] == "trace":
            body = self.trace_view(target["trace"], screenshot_url=url, shared=True)
        else:
            body = self.replay(target["lane"], target["day"], tz=target.get("timezone"), owner=False,
                               screenshot_url=url, shared=True)
        # a view counts once there is something to show
        self.traces.write(TraceEvent(kind="share_view", tool="audit.share_view", status="ok",
                                     workspace=lane.split("/", 1)[0] if lane and "/" in lane else None,
                                     summary=f"share {share['id']} viewed ({share['kind']})",
                                     args={"share": share["id"], "target": target, "via": via}))
        self.db.execute("UPDATE shares SET views = views + 1, last_view_at = ? WHERE id = ?", (self.clock(), share["id"]))
        return {"share": {"id": share["id"], "kind": share["kind"], "expires_at": share["expires_at"]}, **body}

    def share_screenshot(self, token: str, run_id: str) -> Path:
        """The proof screenshot of a run inside a shared lane-day or trace (when the owner allowed
        screenshots on that link)."""
        share = self.share_open(token)
        if not share["screenshots"]:
            raise ShareInvalid("This link does not include screenshots.")
        target = share["target"]
        if share["kind"] == "trace":
            row = self.traces.row(target["trace"])
            allowed = row is not None and row["run_id"] == run_id
        else:
            since, until = day_window(target["day"], target.get("timezone"))
            allowed = any(r["run_id"] == run_id for r in self.traces.lane_rows(target["lane"], since, until))
        path = self.screenshot_path(self.run_view(run_id)) if allowed else None
        if path is None:
            raise NotFound("No screenshot here.")
        return path


# ---------------------------------------------------------------- bundles, offline

def read_bundle(path: Path) -> dict:
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            manifest_bytes = z.read("manifest.json")
            sig = json.loads(z.read("manifest.sig"))
            rows_blob = z.read("rows.jsonl")
            marks_blob = z.read("marks.jsonl")
            files = {n: z.read(n) for n in names if n.startswith("files/")}
        manifest = json.loads(manifest_bytes)
        rows = [json.loads(line) for line in rows_blob.splitlines() if line.strip()]
        marks = [json.loads(line) for line in marks_blob.splitlines() if line.strip()]
    except (OSError, KeyError, ValueError, zipfile.BadZipFile) as e:
        raise BundleInvalid(f"{path} is not an agentos audit bundle ({type(e).__name__})") from None
    if not isinstance(manifest, dict) or manifest.get("format") != BUNDLE_FORMAT:
        raise BundleInvalid(f"{path} is not an agentos audit bundle")
    return {"manifest": manifest, "manifest_bytes": manifest_bytes, "sig": sig, "rows": rows, "marks": marks,
            "files": files, "rows_blob": rows_blob, "marks_blob": marks_blob}


def verify_bundle(path: Path, *, trusted: set[str] | None = None) -> Report:
    """Check a bundle with nothing but the bundle: rows against the chain, marks against their
    keys, the manifest signature, and every file against the hash the export signed."""
    b = read_bundle(path)
    m = b["manifest"]
    scope_d = m.get("scope") or {}
    scope = Scope(since=scope_d.get("since"), until=scope_d.get("until"), lane=scope_d.get("lane"))
    v = Verifier(b["marks"], trusted=trusted, scope=scope, partial=True)
    prev_seq = None
    for r in b["rows"]:
        if prev_seq is not None and int(r.get("seq", 0)) <= prev_seq:
            v._tamper(f"the bundle's rows are out of order at seq {r.get('seq')}: it was edited after export",
                      first=r.get("seq"))
            continue
        prev_seq = int(r.get("seq", 0))
        v.feed(r)
    report = v.finish()
    extra: list[auditlog.Finding] = []
    keys = {k["kid"]: k["pub"] for k in report.keys if k.get("kid")}
    sig = b["sig"] if isinstance(b["sig"], dict) else {}
    if canonical(m) != b["manifest_bytes"] or not sig_ok(keys.get(sig.get("kid")), BUNDLE_CTX, b["manifest_bytes"],
                                                         sig.get("sig")):
        extra.append(auditlog.Finding("tampered", "the bundle's manifest signature does not verify against the "
                                                  "log's audit keys: the manifest was edited or signed elsewhere"))
    if (m.get("rows") or {}).get("sha256") != _sha256_file(b["rows_blob"]):
        extra.append(auditlog.Finding("tampered", "rows.jsonl does not match the hash the export signed"))
    if (m.get("marks") or {}).get("sha256") != _sha256_file(b["marks_blob"]):
        extra.append(auditlog.Finding("tampered", "marks.jsonl does not match the hash the export signed"))
    listed = {f["path"]: f for f in m.get("files") or [] if isinstance(f, dict) and f.get("path")}
    for name, f in listed.items():
        if f.get("left_out"):
            extra.append(auditlog.Finding("note", f"{name} was left out of the export ({f['left_out']})"))
        elif name not in b["files"]:
            extra.append(auditlog.Finding("tampered", f"{name} is listed in the manifest but missing from the bundle"))
        elif _sha256_file(b["files"][name]) != f.get("sha256"):
            extra.append(auditlog.Finding("tampered", f"{name} does not match the hash the export signed"))
    for name in sorted(set(b["files"]) - set(listed)):
        extra.append(auditlog.Finding("note", f"{name} is in the bundle but not in its manifest, so it proves nothing"))
    report.findings.extend(extra)
    if any(f.level == "tampered" for f in report.findings):
        report.status = "tampered"
    elif report.status == "ok" and any(f.level == "unprovable" for f in report.findings):
        report.status = "unverifiable"
    return report


# ---------------------------------------------------------------- helpers

def _replay_lane(lane: Any) -> None:
    if not isinstance(lane, str) or "/" not in lane:
        raise InvalidRequest("lane must look like workspace/name")


_DECISION_TEXT = re.compile(r"^(approved|standing) via (\S+)$")


def _decision(row: dict) -> tuple[str | None, str | None]:
    """(route, via) of an `authorize` row that let a call go on: from its args (`route`, `via`), or,
    for a row written before they were recorded, from its summary ("approved via ap_...")."""
    if row.get("kind") != "authorize" or row.get("status") != "ok":
        return None, None
    args = _args(row)
    if isinstance(args, dict) and isinstance(args.get("route"), str):
        via = args.get("via")
        return args["route"], via if isinstance(via, str) else None
    m = _DECISION_TEXT.match(row.get("summary") or "")
    return (m.group(1), m.group(2)) if m else (None, None)


def _capture_of(events: list[dict]) -> dict | None:
    """The screenshot the node kept after this call (agentos.audit.captures), as its row recorded it."""
    for e in events:
        if e["kind"] == CAPTURE_KIND and e["status"] == "ok":
            a = _args(e)
            if isinstance(a, dict) and isinstance(a.get("sha256"), str):
                return {"trace": e["id"], "sha256": a["sha256"], "url": a.get("url"), "title": a.get("title"),
                        "bytes": a.get("bytes"), "format": a.get("format") or "png"}
    return None


def _b64_len(n: int) -> int:
    return (n + 2) // 3 * 4


def _image_mime(data: bytes) -> str | None:
    """The type of an image from its first bytes (never from a name or a row): PNG, JPEG or WebP only,
    so nothing else can sit in the page's data: URI."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _day_of(steps: list[dict], tz: str) -> str:
    at = steps[0].get("at") if steps else None
    try:
        return datetime.fromisoformat(str(at)).astimezone(zone(tz)).date().isoformat()
    except ValueError:
        return "DAY"


def _standing_words(sa: Any) -> str:
    effects = " and ".join(sa.effects) or "nothing"
    where = ", ".join([*sa.lanes, *(f"{w}/*" for w in sa.workspaces)]) or "every lane"
    uses = f", {sa.max_uses} uses" if sa.max_uses is not None else ""
    return f"{effects} on {where}{uses}"


def _chain_id(marks: list[dict]) -> str | None:
    for m in marks:
        if m["kind"] == "genesis":
            try:
                return json.loads(m["body"]).get("chain")
            except ValueError:
                return None
    return None


def _call_fields(start: dict, end: dict | None, main: dict) -> dict:
    """A step's own fields, from its start row and (once it ended) its end row."""
    return {
        "id": start["id"], "seq": start["seq"], "at": iso(start["at"]),
        "ended_at": iso(end["at"]) if end is not None else None,
        "kind": "call" if start["kind"] == "tool_start" else start["kind"],
        "tool": main.get("tool"), "effect": main.get("effect"),
        "status": main.get("status") if end is not None or start["kind"] != "tool_start" else "unfinished",
        "summary": main.get("summary"), "args": _args(start), "error": main.get("error"),
        "duration_ms": main.get("duration_ms"),
    }


def _after_untrusted(events: list[dict]) -> str | None:
    """What untrusted text the call followed, as its authorize event recorded it."""
    return next((a["after_untrusted"] for a in (_args(e) for e in events if e["kind"] == "authorize")
                 if isinstance(a, dict) and isinstance(a.get("after_untrusted"), str)), None)


def _label(record: dict) -> str:
    sub = record.get("subject") or {}
    scope = record.get("scope") or {}
    blocks = scope.get("blocks") or []
    who = str(sub.get("id") or "someone")
    kind = sub.get("kind")
    if kind and kind != "agent":
        who = f"{who} ({kind})"
    where = None
    for b in reversed(blocks):
        where = b.get("lanes") or b.get("workspaces")
        if where:
            break
    note = next((b.get("note") for b in reversed(blocks) if b.get("note")), None)
    label = f"{who}, up to {scope.get('ceiling') or 'read'}"
    label += f" on {', '.join(where)}" if where else " on every lane"
    if record.get("state") and record["state"] != "active":
        label += f" ({record['state']})"
    return f"{label}: {note}" if note else label


def _chain_summary(report: Report | None, steps: list[dict], *, seqs: set[int] | None) -> dict:
    """The chain status of a replay: the worst of its steps, the checkpoint that proves them, and
    the findings that touch them (all findings for the owner; `seqs` limits them for others)."""
    if report is None:
        return {"status": "ok" if not steps else "unprovable", "proven_through": None, "findings": []}
    status = max((s["chain"]["status"] for s in steps), key=lambda s: CHAIN_ORDER[s], default="ok")
    findings = [f.public() for f in report.findings if f.level != "note"]
    if seqs is not None:
        findings = [f for f in findings if "seq" in f and any(f["seq"][0] <= s <= f["seq"][1] for s in seqs)]
    return {"status": status, "proven_through": report.proven_through, "findings": findings}


def _share_json(row: Any, now: float) -> dict:
    state = "revoked" if row["revoked_at"] is not None else ("expired" if row["expires_at"] <= now else "active")
    return {"id": row["id"], "kind": row["kind"], "target": json.loads(row["target"]),
            "screenshots": bool(row["screenshots"]), "state": state, "created_at": iso(row["created_at"]),
            "expires_at": iso(row["expires_at"]), "revoked_at": iso(row["revoked_at"]), "by": row["by"],
            "views": row["views"], "last_view_at": iso(row["last_view_at"])}
