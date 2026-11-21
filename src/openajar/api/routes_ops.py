"""Looking after this computer on the web: its health, backups, version, environments, identity and settings.

Every route is owner-only, so a member is refused by the endpoint. Each one that changes something
also needs the owner in person at the panel (`owner_in_person`), so a bearer token never starts a
backup, restarts a service or adds a site.

A backup needs its passphrase, and this computer keeps none. So starting one (or checking one) hands
the owner a form this node serves itself, as the mailbox and AI-model forms do. The passphrase goes
from that page to this computer, is used for that one job, and is never stored or returned.

Left on the command line, and said so on the screens: restore and prune (they replace or delete
data), upgrade (it restarts the node), `health check` (a round can close an account whose exit IP is
wrong), `identity init` (it makes a key), and making or removing an environment or an image.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from openajar import __version__
from openajar.api.access import Presence
from openajar.api.app import PERSON_ONLY, Api, access_of, endpoint, owner_in_person, read_body
from openajar.api.errors import ApiError
from openajar.core import clock
from openajar.core.errors import OpenajarError
from openajar.core.layout import default_backup_dir, ops_socket
from openajar.core.models import parse_lane_id
from openajar.core.system import Host
from openajar.ops import backup
from openajar.runner.mail import form as mail_form
from openajar.webbotauth import keys, settings
from openajar.webbotauth.keys import IdentityError

BACKUP_KEEP = 7
"""How many of this node's backups are kept. The same as `openajar backup create`, which prunes to it."""
CHECKS_FILE = "checks.json"
"""Which backups were checked, and when. It holds names and times, never a passphrase."""
UPGRADE_COMMAND = "openajar upgrade --source <your openajar checkout>"
BY = "owner:panel"
JOBS_KEPT = 20
TRIP_TTL_S = 15 * 60
SECRET_WORDS = ("token", "secret", "key", "password", "passphrase")
HIDDEN = ("(set, hidden)", "(not set)")
LOG_LINES = (1, 500)
MODES = {"ok": "ok", "warn": "warn", "fail": "fail"}  # the doctor's pass, warn, fail as the app's checks


def _said(exc: BaseException, fallback: str) -> str:
    """What a failed job says: the node's own sentence when it has one, never a traceback."""
    message = getattr(exc, "message", None) if isinstance(exc, OpenajarError) else None
    return message or fallback


# ---- jobs and passphrase trips -------------------------------------------------------------

@dataclass
class Job:
    """A backup or a check, running in this process. Kept in memory: a restart starts over."""
    id: str
    kind: str                 # "backup" or "check"
    name: str | None = None   # the archive a check reads
    state: str = "running"    # running, done, failed
    message: str = ""
    started_at: str = field(default_factory=lambda: clock.iso(timespec="seconds"))
    finished_at: str | None = None

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "name": self.name, "state": self.state,
                "message": self.message, "started_at": self.started_at, "finished_at": self.finished_at}


class Jobs:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rows: list[Job] = []

    def running(self) -> Job | None:
        with self.lock:
            return next((job for job in reversed(self.rows) if job.state == "running"), None)

    def latest(self) -> Job | None:
        with self.lock:
            return self.rows[-1] if self.rows else None

    def start(self, kind: str, name: str | None) -> Job:
        job = Job(id=f"job_{secrets.token_urlsafe(8)}", kind=kind, name=name)
        with self.lock:
            self.rows = [*self.rows[-(JOBS_KEPT - 1):], job]
        return job

    def finish(self, job: Job, *, state: str, message: str) -> None:
        with self.lock:
            job.state, job.message, job.finished_at = state, message, clock.iso(timespec="seconds")


@dataclass
class Trip:
    """One passphrase form. It holds no passphrase: only the job it asked for, and when it ends."""
    id: str
    kind: str                 # "backup" or "check"
    name: str | None
    token: str = field(default_factory=lambda: secrets.token_urlsafe(24), repr=False)
    expires_at: float = field(default_factory=lambda: time.time() + TRIP_TTL_S)
    stage: str = "waiting"    # waiting, running

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "name": self.name, "stage": self.stage,
                "form_url": f"/api/backups/form/{self.token}" if self.stage == "waiting" else None}


class Trips:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rows: dict[str, Trip] = {}

    def start(self, kind: str, name: str | None) -> Trip:
        with self.lock:
            self._forget_old()
            trip = Trip(id=f"trip_{secrets.token_urlsafe(8)}", kind=kind, name=name)
            self.rows[trip.id] = trip
            return trip

    def by_token(self, token: str) -> Trip | None:
        with self.lock:
            self._forget_old()
            return next((row for row in self.rows.values() if secrets.compare_digest(row.token, token)
                         and row.stage == "waiting"), None)

    def _forget_old(self) -> None:
        now = time.time()
        self.rows = {key: row for key, row in self.rows.items() if row.expires_at > now}


# ---- backups on disk ------------------------------------------------------------------------

def _read_checks(folder: Path) -> dict[str, dict[str, Any]]:
    try:
        rows = json.loads((folder / CHECKS_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return rows if isinstance(rows, dict) else {}


def _write_check(folder: Path, name: str, *, ok: bool, error: str | None = None) -> None:
    rows = _read_checks(folder)
    rows[name] = {"at": clock.iso(timespec="seconds"), "ok": ok, **({"error": error} if error else {})}
    path = folder / CHECKS_FILE
    tmp = folder / f"{CHECKS_FILE}.tmp"
    tmp.write_text(json.dumps(rows, indent=2))
    os.replace(tmp, path)


def _backups_view(home: Path, jobs: Jobs) -> dict[str, Any]:
    folder = default_backup_dir(home)
    latest = jobs.latest()
    job = latest.public() if latest else None  # first: a job that reads as finished has written its check
    checks = _read_checks(folder)
    items = []
    for archive in reversed(backup.list_archives(folder)):
        row = checks.get(archive.path.name)
        if row is None:
            check = {"state": "not_checked", "at": None, "error": None}
        else:
            check = {"state": "verified" if row.get("ok") else "failed", "at": row.get("at"), "error": row.get("error")}
        items.append({"name": archive.path.name, "created_at": archive.created.isoformat(), "size": archive.size,
                      "check": check})
    return {"folder": str(folder), "keep": BACKUP_KEEP, "items": items, "job": job}


def _run_backup(home: Path, node: str, passphrase: str, job: Job, jobs: Jobs) -> None:
    """A thread: the backup, then a check of what was just written, with the passphrase still in hand."""
    folder = default_backup_dir(home)
    try:
        result = backup.create_backup(home, folder, passphrase, node=node, keep=BACKUP_KEEP,
                                      coordinator=backup.DaemonCoordinator(home))
    except Exception as exc:  # noqa: BLE001 - the job says what happened, and nothing else
        jobs.finish(job, state="failed", message=_said(exc, "The backup did not finish. Nothing was replaced."))
        return
    name = result.path.name
    try:
        backup.verify_backup(result.path, passphrase)
    except Exception as exc:  # noqa: BLE001
        message = _said(exc, "The backup was written, but it did not check out.")
        _write_check(folder, name, ok=False, error=message)
        jobs.finish(job, state="failed", message=message)
        return
    _write_check(folder, name, ok=True)
    jobs.finish(job, state="done", message=f"Written and checked: {name}.")


def _run_check(home: Path, name: str, passphrase: str, job: Job, jobs: Jobs) -> None:
    folder = default_backup_dir(home)
    try:
        backup.verify_backup(folder / name, passphrase)
    except Exception as exc:  # noqa: BLE001
        message = _said(exc, "The backup could not be read.")
        _write_check(folder, name, ok=False, error=message)
        jobs.finish(job, state="failed", message=message)
        return
    _write_check(folder, name, ok=True)
    jobs.finish(job, state="done", message=f"{name} checks out.")


# ---- health ---------------------------------------------------------------------------------

def _full_check(config: Any) -> dict[str, Any]:
    """Every doctor check, probing the proxies too. Read-only: the same checks as `openajar doctor`."""
    from openajar.ops import doctor as checks

    report = checks.run(checks.DoctorContext(home=config.home, config=config, network=True, timeout=8.0))
    from openajar.node.channel.reports import DOCTOR_STATUS

    rows = []
    for result in report.results:
        status = DOCTOR_STATUS.get(result.status)
        if status is None:  # a skipped check says nothing about the node
            continue
        fix = result.fix[:300] if result.fix else None
        rows.append({"id": result.id[:64], "name": result.title[:128], "status": status,
                     "detail": (result.detail or "")[:300], "fix": fix})
    return {"ran_at": report.generated_at, "checks": rows}


def _rounds(home: Path) -> dict[str, Any] | None:
    """The node's own health round, as the daemon last ran it. None when the daemon does not answer."""
    resp = Host().unix_request(ops_socket(home), {"op": "status"}, timeout=5)
    if not resp or not resp.get("ok"):
        return None
    status = resp.get("status") or {}
    level = status.get("level")  # before the first round the daemon says "unknown": the app reads that as no round
    return {"at": status.get("at"), "level": level if level in ("ok", "warn", "critical") else None,
            "findings": [{"key": f.get("key"), "kind": f.get("kind"), "level": f.get("level"),
                          "title": f.get("title"), "detail": f.get("detail")}
                         for f in status.get("findings", [])]}


# ---- environments ---------------------------------------------------------------------------

def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else None


def _word(value: Any) -> Any:
    return getattr(value, "value", value)


def _service_row(info: Any) -> dict[str, Any]:
    st = info.service
    return {"lane": info.lane.id, "name": info.lane.id.split("/", 1)[-1],
            "state": _word(st.state) if st else "stopped", "desired": _word(st.desired) if st else "stopped",
            "health": _word(st.health) if st else "none", "restarts": st.restarts if st else 0,
            "last_exit": st.last_exit if st else None, "started_at": _iso(st.started_at) if st else None}


def _lane_row(info: Any) -> dict[str, Any]:
    lane = info.lane
    return {"id": lane.id, "kind": _word(lane.kind), "preset": _word(lane.preset), "state": _word(info.state),
            "isolation": info.isolation or None, "network": lane.profile.network,
            "base_image": lane.profile.base_image, "last_used": _iso(info.last_used),
            "quarantine_reason": info.quarantine_reason}


def _environments(envs: Any) -> dict[str, Any]:
    lanes, services = [], []
    for info in envs.list():
        if _word(info.lane.kind) == "service":
            services.append(_service_row(info))
        else:
            lanes.append(_lane_row(info))
    templates = [{"name": t.name, "description": t.description, "placeholder": t.placeholder}
                 for t in envs.templates().values()]
    built = [{"ref": i.ref, "name": i.name, "version": i.version, "built_at": _iso(i.built_at),
              "size_bytes": i.size_bytes} for i in envs.images()]
    return {"lanes": lanes, "services": services, "images": {"templates": templates, "built": built}}


def _status_view(st: Any) -> dict[str, Any]:
    return {"lane": st.lane, "desired": _word(st.desired), "state": _word(st.state), "health": _word(st.health),
            "restarts": st.restarts, "last_exit": st.last_exit, "started_at": _iso(st.started_at)}


# ---- identity -------------------------------------------------------------------------------

def _account_view(cfg: Any, lane_id: str) -> dict[str, Any]:
    try:
        ident = settings.load(cfg.secrets_dir, lane_id)
        key = keys.load_key(cfg.secrets_dir, lane_id)
    except (IdentityError, OSError):
        return {"lane": lane_id, "state": "unreadable", "keyid": None, "signature_agent": None, "hosts": []}
    if ident is None or key is None:
        return {"lane": lane_id, "state": "none", "keyid": None, "signature_agent": None, "hosts": []}
    hosts = [{"origin": h.origin, "reason": h.reason, "added_by": h.added_by, "added_at": _iso(h.added_at)}
             for h in ident.hosts]
    if ident.profile is None or tuple(ident.profile) != settings.profile_binding(cfg.profile_dir(lane_id)):
        state = "stale"  # the account's profile changed since init: a new key is needed
    elif not hosts:
        state = "off"
    else:
        state = "signing"
    return {"lane": lane_id, "state": state, "keyid": key.keyid, "signature_agent": ident.signature_agent,
            "hosts": hosts}


def _loaded(cfg: Any, lane_id: str) -> settings.LaneIdentity:
    try:
        ident = settings.load(cfg.secrets_dir, lane_id)
    except (IdentityError, OSError) as exc:
        raise ApiError("invalid", f"{lane_id}'s identity cannot be read.") from exc
    if ident is None or keys.load_key(cfg.secrets_dir, lane_id) is None:
        raise ApiError("invalid", f"{lane_id} has no identity yet. Make one on the command line: "
                                  f"openajar identity init {lane_id} --agent <https URL>.")
    if not ident.signature_agent:
        raise ApiError("invalid", f"Set {lane_id}'s signature agent first, on the command line: "
                                  f"openajar identity init {lane_id} --agent <https URL>.")
    return ident


# ---- settings -------------------------------------------------------------------------------

def _settings_rows(cfg: Any) -> list[dict[str, Any]]:
    from openajar.core.config_report import config_report

    rows = []
    for row in config_report(cfg, cfg.home):
        value = row["value"]
        if any(word in row["name"] for word in SECRET_WORDS) and value not in HIDDEN:
            value = HIDDEN[0] if value else HIDDEN[1]  # never a secret's value, only whether it is set
        rows.append({**row, "value": value if isinstance(value, str | int | float | bool | list) else str(value)})
    return rows


# ---- the routes -----------------------------------------------------------------------------

def _person(request: Request, action: str) -> None:
    """Refuse anyone but the owner in a panel session, and say so in Activity."""
    if not owner_in_person(request):
        access_of(request).trace(action, status="denied", summary="needs the owner in person")
        raise ApiError("denied", PERSON_ONLY)


def _passphrase_page(trip: Trip, *, error: str | None = None, status: int = 200) -> HTMLResponse:
    make = trip.kind == "backup"
    title = "Make a backup" if make else "Check a backup"
    problem = f'<p class="note bad" role="alert">{html.escape(error)}</p>' if error else ""
    again = ('<label for="again">Type the passphrase again</label>'
             '<input id="again" name="again" type="password" required autocomplete="off">') if make else ""
    intro = ("Choose a passphrase for this backup. Keep it safe: without it no backup can be opened, and it "
             "is not kept anywhere on this computer." if make else
             "Type the passphrase this backup was made with. It is used to check the backup and then dropped.")
    what = f" {html.escape(trip.name)}" if trip.name else ""
    body = (f"<h1>{title}{what}</h1><p class=soft>This page comes from this computer. The passphrase goes only "
            f"to this computer and is never stored.</p>{problem}<p class=soft>{intro}</p>"
            f'<form method="post" autocomplete="off"><label for="value">Passphrase</label>'
            f'<input id="value" name="value" type="password" required autocomplete="off" spellcheck="false">'
            f"{again}<button type=submit>{'Make the backup' if make else 'Check it'}</button></form>")
    return mail_form.page(title, body, status=status)


def _ended() -> HTMLResponse:
    return mail_form.page("This link has ended", "<h1>This link has ended</h1><p class=soft>It was used, or it ran "
                          "out of time. Start again from the Backups tab.</p>", status=404)


class Handlers:
    """The handlers of the routes below, sharing one Api (and what they keep between calls)."""

    def __init__(self, api: Api) -> None:
        self.api = api
        self.jobs, self.trips = Jobs(), Trips()

    def this(self, request: Request) -> None:
        if request.path_params["id"] != self.api.node_name:
            raise ApiError("not_found", "This computer has no such computer.")

    # ---- health ----
    async def health_run(self, request: Request) -> dict[str, Any]:
        self.this(request)
        return await asyncio.to_thread(_full_check, self.api.config)

    async def health_rounds(self, request: Request) -> dict[str, Any] | None:
        self.this(request)
        return await asyncio.to_thread(_rounds, self.api.config.home)

    # ---- backups ----
    async def backups(self, request: Request) -> dict[str, Any]:
        self.this(request)
        return await asyncio.to_thread(_backups_view, self.api.config.home, self.jobs)

    async def backup_start(self, request: Request) -> dict[str, Any]:
        self.this(request)
        _person(request, "backups.start")
        if self.jobs.running():
            raise ApiError("busy", "A backup is already running. It takes a few minutes, and the Backups tab says "
                                   "when it is done.")
        access_of(request).trace("backups.start", summary="Asked for a backup")
        return self.trips.start("backup", None).public()

    async def backup_check(self, request: Request) -> dict[str, Any]:
        self.this(request)
        _person(request, "backups.check")
        body = await read_body(request, BackupCheck)
        name = body.name
        if self.jobs.running():
            raise ApiError("busy", "A backup is already running. Check this one when it is done.")
        if not parse_name_ok(name) or not (default_backup_dir(self.api.config.home) / name).is_file():
            raise ApiError("not_found", "There is no such backup on this computer.")
        access_of(request).trace("backups.check", summary=f"Asked to check {name}")
        return self.trips.start("check", name).public()

    async def form_get(self, request: Request) -> Response:
        trip = self.trips.by_token(request.path_params["token"])
        if trip is None:
            return _ended()
        if not mail_form.direct(request):
            return mail_form.not_direct()
        return _passphrase_page(trip)

    async def form_post(self, request: Request) -> Response:
        trip = self.trips.by_token(request.path_params["token"])
        if trip is None:
            return _ended()
        if not mail_form.direct(request) or not mail_form.same_origin(request):
            return mail_form.not_direct()
        fields = mail_form.read_form(await request.body())
        value = str(fields.get("value", ""))
        again = str(fields.get("again", ""))
        error = None
        if trip.kind == "backup" and len(value) < backup.MIN_PASSPHRASE:
            error = f"Use at least {backup.MIN_PASSPHRASE} characters."
        elif trip.kind == "backup" and value != again:
            error = "The two passphrases do not match."
        elif not value:
            error = "Type the passphrase."
        if error is not None:
            return _passphrase_page(trip, error=error, status=400)
        if self.jobs.running():
            return _passphrase_page(trip, error="A backup is already running. Try again when it is done.", status=409)
        with self.trips.lock:  # one form works once: a second post finds the trip already taken
            if trip.stage != "waiting":
                return _ended()
            trip.stage = "running"
        home, node = self.api.config.home, self.api.node_name
        if trip.kind == "backup":
            job = self.jobs.start("backup", None)
            worker = threading.Thread(target=_run_backup, args=(home, node, value, job, self.jobs), daemon=True,
                                      name="agentos-backup")
        else:
            job = self.jobs.start("check", trip.name)
            worker = threading.Thread(target=_run_check, args=(home, trip.name or "", value, job, self.jobs), daemon=True,
                                      name="agentos-check")
        worker.start()
        done = "Making the backup" if trip.kind == "backup" else "Checking the backup"
        return mail_form.page(done, f"<h1 class=good>{done} now</h1><p>This takes a few minutes. You can close this "
                                    "tab. The Backups tab says when it is done.</p>")

    # ---- upgrade ----
    async def upgrade(self, request: Request) -> dict[str, Any]:
        self.this(request)
        # A node cannot see a newer release from here, so `newer` is null: the screens say so, and do not guess.
        return {"running": __version__, "newer": None, "command": UPGRADE_COMMAND, "python": sys.version.split()[0]}

    # ---- environments ----
    async def environments(self, request: Request) -> dict[str, Any] | None:
        self.this(request)
        envs = self.api.runtime.get("envs")
        if envs is None:
            return None
        return await asyncio.to_thread(_environments, envs)

    def service_envs(self, request: Request) -> tuple[Any, str]:
        self.this(request)
        envs = self.api.runtime.get("envs")
        if envs is None:
            raise ApiError("unavailable", "Environments are not set up on this computer.")
        lane = f"{request.path_params['ws']}/{request.path_params['name']}"
        try:
            parse_lane_id(lane)
        except ValueError as exc:
            raise ApiError("invalid", "A service is named workspace/name.") from exc
        if _word(envs.get(lane).kind) != "service":
            raise ApiError("invalid", f"{lane} is not a service.")
        return envs, lane

    def env_lane(self, request: Request) -> tuple[Any, str]:
        self.this(request)
        envs = self.api.runtime.get("envs")
        if envs is None:
            raise ApiError("unavailable", "Terminal and service lanes are not set up on this computer.")
        lane = f"{request.path_params['ws']}/{request.path_params['name']}"
        try:
            parse_lane_id(lane)
        except ValueError as exc:
            raise ApiError("invalid", "A lane is named workspace/name.") from exc
        return envs, lane

    async def environment_remove(self, request: Request) -> dict[str, Any]:
        """`openajar env remove` on the node while agentosd owns the env runtime (QA 2026-11-10, finding 30):
        a person (the panel's owner, or the node's CLI with its one-time code), never a token alone."""
        envs, lane = self.env_lane(request)
        envs.get(lane)  # lane_not_found when there is none
        await envs.remove(lane)
        access_of(request).trace("environments.remove", lane=lane, summary=f"Deleted the lane {lane} and its files")
        return {"removed": lane}

    async def environments_doctor(self, request: Request) -> dict[str, Any]:
        """`openajar env doctor` while agentosd runs: what this node can do for terminal and service lanes."""
        self.this(request)
        envs = self.api.runtime.get("envs")
        if envs is None:
            raise ApiError("unavailable", "Terminal and service lanes are not set up on this computer.")
        return envs.node_report()

    async def service_start(self, request: Request) -> dict[str, Any]:
        _person(request, "services.start")
        envs, lane = self.service_envs(request)
        status = await envs.service_start(lane)
        access_of(request).trace("services.start", lane=lane, summary=f"Started the service in {lane}")
        return _status_view(status)

    async def service_stop(self, request: Request) -> dict[str, Any]:
        _person(request, "services.stop")
        envs, lane = self.service_envs(request)
        status = await envs.service_stop(lane)
        access_of(request).trace("services.stop", lane=lane, summary=f"Stopped the service in {lane}")
        return _status_view(status)

    async def service_logs(self, request: Request) -> dict[str, Any]:
        envs, lane = self.service_envs(request)
        try:
            lines = int(request.query_params.get("lines", "100"))
        except ValueError:
            lines = 100
        lines = min(max(lines, LOG_LINES[0]), LOG_LINES[1])
        text = await asyncio.to_thread(envs.service_logs, lane, lines)
        return {"lane": lane, "log": text}

    # ---- identity ----
    async def identity(self, request: Request) -> dict[str, Any]:
        self.this(request)
        cfg = self.api.config
        lanes = [row["id"] for row in await _lanes(self.api) if row.get("kind") not in ("terminal", "service")]
        return {"accounts": [_account_view(cfg, lane) for lane in lanes]}

    def account_lane(self, request: Request) -> str:
        self.this(request)
        lane = f"{request.path_params['ws']}/{request.path_params['name']}"
        try:
            parse_lane_id(lane)
        except ValueError as exc:
            raise ApiError("invalid", "An account is named workspace/name.") from exc
        return lane

    async def site_add(self, request: Request) -> dict[str, Any]:
        _person(request, "identity.site.add")
        lane = self.account_lane(request)
        body = await read_body(request, SiteAdd)
        cfg = self.api.config

        def add() -> dict[str, Any]:
            ident = _loaded(cfg, lane)
            try:
                entry = settings.add_host(ident, body.origin, reason=body.reason, by=BY)
                settings.save(cfg.secrets_dir, ident)
            except (OpenajarError, ValueError) as exc:
                raise ApiError("invalid", getattr(exc, "message", None) or str(exc)) from exc
            return {"origin": entry.origin}

        added = await asyncio.to_thread(add)
        access_of(request).trace("identity.site.add", lane=lane, summary=f"Signs for {added['origin']} on {lane}")
        return _account_view(cfg, lane)

    async def site_remove(self, request: Request) -> dict[str, Any]:
        _person(request, "identity.site.remove")
        lane = self.account_lane(request)
        origin = request.query_params.get("origin", "")
        cfg = self.api.config

        def remove() -> None:
            ident = _loaded(cfg, lane)
            try:
                removed = settings.remove_host(ident, origin, by=BY)
            except (OpenajarError, ValueError) as exc:
                raise ApiError("invalid", getattr(exc, "message", None) or str(exc)) from exc
            if not removed:
                raise ApiError("not_found", f"{lane} does not sign for {origin}.")
            settings.save(cfg.secrets_dir, ident)

        await asyncio.to_thread(remove)
        access_of(request).trace("identity.site.remove", lane=lane, summary=f"Stopped signing for {origin} on {lane}")
        return _account_view(cfg, lane)

    # ---- settings ----
    async def settings_view(self, request: Request) -> list[dict[str, Any]]:
        self.this(request)
        return await asyncio.to_thread(_settings_rows, self.api.config)


def routes(api: Api) -> list[Route]:
    h = Handlers(api)

    def route(path: str, fn: Any, methods: list[str]) -> Route:
        return Route(path, endpoint(api, fn, owner=True), methods=methods)

    return [
        route("/api/nodes/{id}/health/run", h.health_run, ["POST"]),
        route("/api/nodes/{id}/health/rounds", h.health_rounds, ["GET"]),
        route("/api/nodes/{id}/backups", h.backups, ["GET"]),
        route("/api/nodes/{id}/backups", h.backup_start, ["POST"]),
        route("/api/nodes/{id}/backups/check", h.backup_check, ["POST"]),
        Route("/api/backups/form/{token}", endpoint(api, h.form_get, public=True), methods=["GET"]),
        Route("/api/backups/form/{token}", endpoint(api, h.form_post, public=True), methods=["POST"]),
        route("/api/nodes/{id}/upgrade", h.upgrade, ["GET"]),
        route("/api/nodes/{id}/environments", h.environments, ["GET"]),
        route("/api/nodes/{id}/environments/doctor", h.environments_doctor, ["GET"]),
        Route("/api/nodes/{id}/environments/{ws}/{name}",
              endpoint(api, h.environment_remove, owner=True, presence=Presence.PERSON), methods=["DELETE"]),
        route("/api/nodes/{id}/services/{ws}/{name}/start", h.service_start, ["POST"]),
        route("/api/nodes/{id}/services/{ws}/{name}/stop", h.service_stop, ["POST"]),
        route("/api/nodes/{id}/services/{ws}/{name}/logs", h.service_logs, ["GET"]),
        route("/api/nodes/{id}/identity", h.identity, ["GET"]),
        route("/api/nodes/{id}/identity/{ws}/{name}/sites", h.site_add, ["POST"]),
        route("/api/nodes/{id}/identity/{ws}/{name}/sites", h.site_remove, ["DELETE"]),
        route("/api/nodes/{id}/settings", h.settings_view, ["GET"]),
    ]


class BackupCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)


class SiteAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=300)


def parse_name_ok(name: str) -> bool:
    """A backup's own file name, and nothing with a path in it."""
    return name == Path(name).name and backup.parse_name(name) is not None


async def _lanes(api: Api) -> list[dict[str, Any]]:
    from openajar.api.routes_core import all_lanes

    return await all_lanes(api)
