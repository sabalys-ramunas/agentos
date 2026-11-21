"""Lane specs on disk, for the lane manager (openajar.node.lanes): create, import a profile, delete
(to the trash, never removed), move to another proxy, and set the site list of the egress guard.
Every change here needs the lane cold, except the site list, which a warm lane's guard takes at
once. The profile helpers make an imported copy safe to own: no Firefox locks, no symlink that leads
outside the profile, nothing readable by group or others.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from openajar.core.errors import ConfirmRequired, InvalidRequest, LaneExists, LaneNotFound
from openajar.core.models import LaneInfo, LaneKind, LaneSpec, LaneState
from openajar.node import lanefmt
from openajar.node.lanes.base import (
    _HOLDS_SLOT,
    EXIT_IP_QUARANTINES,
    _private_dir,
    _strip_group_other,
    _who,
)

log = logging.getLogger("openajar.lanes")

PROFILE_LOCKS = ("lock", ".parentlock", "parent.lock")
NOT_RUNNING_KINDS = (LaneKind.TERMINAL, LaneKind.SERVICE, LaneKind.ANDROID)


class SpecsMixin:
    """Specs and profiles of the lane manager (openajar.node.lanes.LaneManagerImpl)."""

    def _validate(self, spec: LaneSpec) -> None:
        if spec.kind in NOT_RUNNING_KINDS:
            raise InvalidRequest(f"this lane manager runs browser lanes only; {spec.kind.value} lanes are "
                                 "not made here (terminal and service lanes: openajar env)")
        engine_name = getattr(self.engine, "name", spec.engine)
        if spec.engine != engine_name:
            raise InvalidRequest(f"unknown engine {spec.engine!r} (this node runs {engine_name!r})")
        if spec.proxy_label and spec.proxy_label not in self.proxies_loader():
            raise InvalidRequest(f"unknown proxy {spec.proxy_label!r}. Add it first with: "
                                 f"openajar proxy add {spec.proxy_label} host:port:user:pass")
        if spec.device:
            self._require_device(spec.device)

    def _prepare_lane_dir(self, lane_id: str) -> None:
        lanes_root = self.config.home / "lanes"
        lane_dir = self.config.lane_dir(lane_id)
        for d in (lanes_root, lane_dir.parent, lane_dir):
            _private_dir(d)

    async def create(self, spec: LaneSpec) -> LaneInfo:
        if spec.device:
            async with self._device_lock(spec.device):  # its pin, its company and its removal are settled
                return self._create(spec)
        return self._create(spec)

    def _create(self, spec: LaneSpec) -> LaneInfo:
        if spec.device:
            spec = self._join_new_lane(spec)  # the device's proxy
        self._validate(spec)
        if self.store.get(spec.id):
            raise LaneExists(f"lane {spec.id} already exists")
        self._prepare_lane_dir(spec.id)
        _private_dir(self.config.profile_dir(spec.id))
        lanefmt.stamp_new(self.config.lane_dir(spec.id))
        self.store.add(spec)
        if spec.device:
            self._claim_device(spec.device, spec.id)  # the device is this workspace's computer
            self.sync_device_pins(spec.device)  # a copy of the device's pin, when it has one
        log.info("created lane %s%s", spec.id, f" on device {spec.device}" if spec.device else "")
        return self.get(spec.id)

    async def import_profile(self, spec: LaneSpec, profile_src: Path, fingerprint_src: Path | None) -> LaneInfo:
        """Copy an existing profile in. The copy is made in a private staging directory and
        renamed into place, so two imports of one lane can never delete each other's profile.
        Refuses a profile a browser still has open, and a source that contains the lane's own
        directory. Firefox locks are dropped, and so is any symlink that would lead outside the
        lane's profile."""
        if spec.device:
            if fingerprint_src is not None:
                raise InvalidRequest(f"a lane on device {spec.device} launches with the device's fingerprint, so "
                                     "--fingerprint and --device do not go together. Import the lane with its "
                                     f"fingerprint, then make the device from it: openajar device create "
                                     f"<name> --from-lane {spec.id}")
            spec = self._join_new_lane(spec)
        self._validate(spec)
        if self.store.get(spec.id):
            raise LaneExists(f"lane {spec.id} already exists")
        src = Path(profile_src).expanduser()
        if not src.is_dir():
            raise InvalidRequest(f"profile directory not found: {src}")
        real = src.resolve()
        dest = self.config.profile_dir(spec.id)
        dest_real = dest.resolve()
        if dest_real.is_relative_to(real) or real.is_relative_to(dest_real):
            raise InvalidRequest(f"{src} contains this lane's own directory, so it cannot be copied into it. "
                                 "Point --profile at the browser profile itself.")
        running = _browsers_using(src)
        if running:
            raise InvalidRequest(f"a browser (pid {running[0]}) is still running with {src}. "
                                 "Close it first so the copy is consistent.")
        locked = _profile_lock_owner(src)
        if locked is not None:
            who = f" (pid {locked})" if locked else ""
            raise InvalidRequest(f"a browser{who} still has {src} open (its profile lock is held). "
                                 "Close it first so the copy is consistent.")
        fingerprint = None
        if fingerprint_src is not None:
            fingerprint = _read_fingerprint(Path(fingerprint_src).expanduser())

        fp_dest = self.config.fingerprint_path(spec.id)
        if dest.exists() and any(dest.iterdir()):
            raise InvalidRequest(f"{dest} already holds a profile; refusing to overwrite it")
        self._prepare_lane_dir(spec.id)
        staging = dest.parent / f".import-{uuid.uuid4().hex[:12]}"
        try:
            await asyncio.to_thread(shutil.copytree, src, staging, symlinks=True)
            await asyncio.to_thread(_clean_profile_copy, staging, real)
            _claim(staging, dest)  # raises OSError when another import got there first
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        try:
            if fingerprint is not None:
                from openajar.node.camoufox.engine import write_private_json
                write_private_json(fp_dest, fingerprint)
            lanefmt.stamp_new(self.config.lane_dir(spec.id))
            if spec.device:
                async with self._device_lock(spec.device):
                    spec = self._join_new_lane(spec)  # again, under the lock: the device may have changed
                    self.store.add(spec)
                    self._claim_device(spec.device or "", spec.id)
                    self.sync_device_pins(spec.device or "")
            else:
                self.store.add(spec)
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)  # ours: we renamed it into place
            if fingerprint is not None:
                fp_dest.unlink(missing_ok=True)
            raise
        log.info("imported lane %s from %s%s", spec.id, src, " with its fingerprint" if fingerprint else "")
        return self.get(spec.id)

    async def delete(self, lane_id: str, *, by: str = "owner") -> Path | None:
        """Remove a cold lane. Its directory (profile, fingerprint, files) moves to
        trash/<ws>--<name>--<UTC time>, never deleted; returns where it went (None when the lane
        had no directory). Refuses a lane that is warming, warm, in use or held by a browser."""
        await self.store.flush()  # a queued control or event write must never land after the delete
        if not self.store.exists(lane_id):
            raise LaneNotFound(f"no lane named {lane_id}")
        self._refuse_busy(lane_id, "delete")
        src = self.config.lane_dir(lane_id)
        moved: Path | None = None
        if src.exists():
            _private_dir(self.trash_dir)
            ws, name = lane_id.split("/", 1)
            stamp = self.now().strftime("%Y%m%dT%H%M%SZ")
            moved = self.trash_dir / f"{ws}--{name}--{stamp}"
            n = 1
            while moved.exists():
                n += 1
                moved = self.trash_dir / f"{ws}--{name}--{stamp}-{n}"
            os.rename(src, moved)
        try:
            self.store.delete(lane_id)
        except BaseException:
            if moved is not None:
                os.rename(moved, src)
            raise
        for table in (self._lanes, self._controls, self._denied_at, self._input_persisted, self._signals,
                      self._unsaved_quarantines):
            table.pop(lane_id, None)
        self._input_dirty.discard(lane_id)
        self.handback.forget(lane_id)
        self._event(lane_id, "deleted", {"by": _who(by), "trash": str(moved) if moved else None})
        log.info("deleted lane %s%s", lane_id, f"; its files are in {moved}" if moved else "")
        return moved

    async def set_proxy(self, lane_id: str, label: str | None, *, confirm: bool = False,
                        by: str = "owner") -> LaneInfo:
        """Move a cold lane to another proxy label (None: no proxy). Recorded as a lane event.
        Note: the lane's pinned fingerprint keeps the old location's timezone and locale, so
        prefer a proxy in the same city. A lane that has launched (its site knows its IP) needs
        confirm=True; without it this raises ConfirmRequired and nothing changes."""
        rec = self.store.require(lane_id)
        label = label or None
        self._refuse_device_proxy_change(rec.spec, label)
        self._refuse_busy(lane_id, "move to another proxy")
        if label is not None and label not in self.proxies_loader():
            raise InvalidRequest(f"unknown proxy {label!r}. Add it first with: openajar proxy add {label} "
                                 "host:port:user:pass")
        old = rec.spec.proxy_label
        if old == label:
            return self.get(lane_id)
        if not confirm and self.config.fingerprint_path(lane_id).exists():
            raise ConfirmRequired(
                f"{lane_id} has launched, so its site knows it from proxy {old or 'none'}. Moving it to "
                f"{label or 'no proxy'} is a new IP address for a logged-in account: expect a re-login or a "
                "security check, and prefer a proxy in the same city. Pass --yes to go ahead",
                details={"lane": lane_id, "proxy": {"from": old, "to": label}, "confirm": True})
        # Plain writes, no await between reading the spec and writing it back: a set_sites at
        # the same moment must not be lost, and no use may start on the old spec in between.
        self.store.update_spec(rec.spec.model_copy(update={"proxy_label": label}))
        self.store.soon(self.store.set_exit_ip, lane_id, None)
        lane = self._lanes.get(lane_id)
        if lane is not None:
            lane.exit_ip = None
            if lane.state == LaneState.QUARANTINED:  # a failed self-check through the old proxy
                lane.state, lane.quarantine_reason = LaneState.COLD, None
        self._event(lane_id, "proxy_changed", {"from": old, "to": label, "by": _who(by)})
        log.info("%s: proxy %s -> %s (by %s)", lane_id, old or "none", label or "none", _who(by))
        # A quarantine the old proxy's exit caused ends with it, record and hold alike; the next
        # open runs the self-check on the new proxy before any agent gets the page. Any other
        # quarantine, and any other hold, stays until a human hands the lane back.
        await self.store.flush()  # a quarantine still waiting for a lock lands first
        q = self.store.require(lane_id).quarantine
        if q is not None and q.kind in EXIT_IP_QUARANTINES:
            await self.store.run(self.store.set_quarantine, lane_id, None)
            self._event(lane_id, "quarantine_cleared", {"by": _who(by), "reason": q.reason, "kind": q.kind,
                                                        "proxy": {"from": old, "to": label}})
            log.info("%s: quarantine cleared by the move to proxy %s", lane_id, label or "none")
            await self._release_quarantine_hold(lane_id, q, by=by)
        return self.get(lane_id)

    async def set_sites(self, lane_id: str, sites: list[str], *, strict: bool | None = None,
                        by: str = "owner") -> LaneInfo:
        """Set the lane's site list (openajar.node.egress; empty: no lane restriction) and, when given,
        its strict egress mode. On the daemon's manager this takes effect at once on a warm lane's
        guard (the CLI calls it through POST /api/lanes/{ws}/{name}/sites while agentosd runs); a
        store written by another process is reread by the guard within SITES_TTL_S."""
        rec = self.store.require(lane_id)
        update: dict[str, Any] = {"sites": sites}
        if strict is not None:
            update["egress_strict"] = strict
        try:
            spec = LaneSpec.model_validate({**rec.spec.model_dump(), **update})
        except ValueError as e:
            raise InvalidRequest(_first_error(e)) from None
        if spec.sites == rec.spec.sites and spec.egress_strict == rec.spec.egress_strict:
            return self.get(lane_id)
        self.store.update_spec(spec)  # plain: no await between reading the spec and writing it back
        lane = self._lanes.get(lane_id)
        guard = getattr(getattr(lane, "session", None), "guard", None)
        if guard is not None:
            guard.set_sites(spec.sites, spec.egress_strict)
        self._event(lane_id, "sites_changed", {"from": rec.spec.sites, "to": spec.sites,
                                               "strict": spec.egress_strict, "by": _who(by)})
        log.info("%s: site list set to %s (strict %s, by %s)", lane_id, ", ".join(spec.sites) or "none",
                 spec.egress_strict, _who(by))
        return self.get(lane_id)

    def _refuse_busy(self, lane_id: str, what: str) -> None:
        lane = self._lanes.get(lane_id)
        if lane is not None and (lane.state in _HOLDS_SLOT or lane.session is not None or lane.users
                                 or lane.op.locked()):
            raise InvalidRequest(f"lane {lane_id} is {lane.state.value}; close it first (browser_close or "
                                 f"openajar lane close {lane_id}), then {what} it")
        running = _browsers_using(self.config.profile_dir(lane_id))
        if running:
            raise InvalidRequest(f"a browser (pid {running[0]}) is still running with lane {lane_id}'s profile; "
                                 f"close the lane first, then {what} it")


def _first_error(e: ValueError) -> str:
    """The first message of a pydantic ValidationError, without its URL and input dump."""
    errors = getattr(e, "errors", None)
    if callable(errors):
        with contextlib.suppress(Exception):
            return str(errors()[0].get("msg", e)).removeprefix("Value error, ")
    return str(e).splitlines()[0]


def _browsers_using(profile: Path) -> list[int]:
    from openajar.node.camoufox.engine import browser_pids
    return browser_pids(profile)


def _profile_lock_owner(profile: Path) -> int | None:
    """A live browser's hold on a Firefox profile: its pid from the `lock` symlink, 0 when only
    the .parentlock file lock is held, or None when nothing holds the profile."""
    from openajar.core.proc import pid_alive

    lock = profile / "lock"
    if lock.is_symlink():
        match = re.search(r":\+?(\d+)$", os.readlink(lock))
        if match and int(match.group(1)) > 1 and pid_alive(int(match.group(1))):
            return int(match.group(1))
    parent = profile / ".parentlock"
    if parent.is_file() and not parent.is_symlink():
        try:
            import fcntl
        except ImportError:  # not POSIX
            return None
        try:
            fd = os.open(parent, os.O_RDWR)
        except OSError:
            return None
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EAGAIN):
                return 0
        else:
            fcntl.lockf(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    return None


def _clean_profile_copy(root: Path, source: Path) -> None:
    """Make a copied profile safe to own: no Firefox lock files, no symlink leading outside
    the copy (one into the source tree is pointed at the same place in the copy; one that
    leaves it is dropped), nothing readable by group or others."""
    for name in PROFILE_LOCKS:
        lock = root / name
        if lock.is_symlink() or lock.exists():
            lock.unlink()
    root_real = root.resolve()
    source_real = source.resolve()
    links = [Path(d, n) for d, dirs, files in os.walk(root) for n in dirs + files if Path(d, n).is_symlink()]
    for link in links:
        if link.resolve().is_relative_to(root_real):
            continue
        meant = (source_real / link.relative_to(root)).resolve()
        link.unlink()
        if meant.is_relative_to(source_real):
            os.symlink(os.path.relpath(root_real / meant.relative_to(source_real), link.parent.resolve()), link)
            if link.resolve().is_relative_to(root_real):
                continue
            link.unlink()
        log.warning("import: dropped %s, a symlink that leads outside the profile", link.relative_to(root))
    _strip_group_other(root)


def _claim(staging: Path, dest: Path) -> None:
    """Move a finished copy into place. Raises FileExistsError when dest already holds
    something (another import of the same lane won)."""
    try:
        dest.rmdir()  # an empty placeholder
    except FileNotFoundError:
        pass
    except OSError as e:
        if e.errno in (errno.ENOTEMPTY, errno.EEXIST):
            raise FileExistsError(errno.EEXIST, f"{dest} already holds a profile") from None
        raise
    os.rename(staging, dest)


def _read_fingerprint(path: Path) -> dict:
    import json
    if not path.is_file():
        raise InvalidRequest(f"fingerprint file not found: {path}")
    try:
        data = json.loads(path.read_text())
    except ValueError as e:
        raise InvalidRequest(f"fingerprint file {path} is not valid JSON: {e}") from None
    if not isinstance(data, dict) or not data:
        raise InvalidRequest(f"fingerprint file {path} must hold a non-empty JSON object")
    return data
